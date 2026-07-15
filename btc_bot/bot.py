"""The main loop: watch the active BTC Up/Down 5m market and trade the band."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import List, Optional, Tuple

from .config import Config
from .data import Market, MarketData
from .strategy import evaluate
from .trader import build_trader
from .tradelog import TradeLogger
from .util import floor2, fmt, fmt_money


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Bot:
    def __init__(self, cfg: Config, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.log = logger or logging.getLogger("btc-bot")
        self.data = MarketData(cfg, self.log)
        self.trader = build_trader(cfg, self.log)
        self.tradelog = TradeLogger(cfg.trade_log_csv)

        self.current_condition: Optional[str] = None
        self.bought_this_window = False
        # Recent (ts, up_price, down_price) samples for the current window —
        # used to tell whether a side's price is rising (the early-regime rule).
        self.price_history: deque = deque()
        self.strike: Optional[float] = None       # the window's open price (target)
        self.strike_is_exact = False              # True once read from Polymarket
        self.trades: List[dict] = []
        self.consecutive_errors = 0
        self.stop_reason: Optional[str] = None

        # Open position(s) in the current window + running profit/loss.
        # open_position = main bet; hedge_position = filled opposite-side bet;
        # pending_hedge = a resting hedge order still waiting to fill.
        self.open_position: Optional[dict] = None
        self.hedge_position: Optional[dict] = None
        self.pending_hedge: Optional[dict] = None
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0

        # Cached balance (refreshed every balance_refresh_seconds and force-
        # invalidated after every order) + status-line throttle timestamp.
        self._balance: Optional[float] = None
        self._balance_ts = 0.0
        self._last_status_log = 0.0

    # -- public entry points ------------------------------------------------
    def run(self) -> None:
        self._banner()
        self.data.start_feed()
        start = time.time()
        deadline = start + self.cfg.run_duration_seconds

        if not self._check_starting_balance():
            self._summary()
            return

        try:
            while True:
                if time.time() >= deadline:
                    self.stop_reason = "run duration reached"
                    break
                try:
                    self._tick()
                    self.consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 - keep the bot alive
                    self.consecutive_errors += 1
                    self.log.warning(
                        "cycle failed (%d/%d): %s",
                        self.consecutive_errors,
                        self.cfg.max_consecutive_errors,
                        exc,
                    )
                    if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                        self.stop_reason = "too many consecutive errors — safe stop"
                        break
                if self.stop_reason:
                    break
                time.sleep(self.cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            self.stop_reason = "interrupted by user (Ctrl-C)"

        self._summary()

    def tick_once(self) -> None:
        """Run a single cycle and exit — handy for verifying setup (`--once`)."""
        self._banner()
        self.data.start_feed()
        time.sleep(1.2)   # let the live price feed warm up for the snapshot
        self._tick()
        self.data.stop_feed()

    def setup_allowances(self) -> None:
        self._banner()
        self.log.info("Setting USDC allowance for trading...")
        resp = self.trader.setup_allowances()
        self.log.info("Allowance response: %s", resp)

    # -- internals ----------------------------------------------------------
    def _check_starting_balance(self) -> bool:
        try:
            bal = self.trader.get_balance()
        except Exception as exc:  # noqa: BLE001
            self.log.error("Could not read starting balance: %s", exc)
            if self.cfg.dry_run:
                return True  # dry-run can proceed on simulated balance
            self.stop_reason = "could not read starting balance"
            return False
        self.log.info("Starting balance: $%.2f", bal)
        if bal <= self.cfg.min_balance_usdc:
            self.stop_reason = (
                f"starting balance ${bal:.2f} <= minimum ${self.cfg.min_balance_usdc:.2f}"
            )
            self.log.error("%s — nothing to do.", self.stop_reason)
            return False
        return True

    def _get_balance(self, now: Optional[float] = None) -> float:
        """Balance with a small cache: it only changes when WE trade, so it is
        refreshed every balance_refresh_seconds and immediately after orders
        (see _invalidate_balance) instead of one HTTP call per tick."""
        now = now if now is not None else time.time()
        if (self._balance is None
                or now - self._balance_ts >= self.cfg.balance_refresh_seconds):
            self._balance = self.trader.get_balance()
            self._balance_ts = now
        return self._balance

    def _invalidate_balance(self) -> None:
        self._balance = None

    def _tick(self) -> None:
        cfg = self.cfg
        market = self.data.get_active_market()
        if market is None:
            self.log.info("Waiting for the active %s market to appear...",
                          cfg.slug_prefix)
            return

        self._handle_window_rollover(market)
        self._refresh_strike(market)   # upgrade approx -> exact when available

        now = time.time()
        time_to_end = market.seconds_to_end(now)

        up_bid, up_ask = self.data.get_book_top(market.token_up)
        dn_bid, dn_ask = self.data.get_book_top(market.token_down)
        up_price = self._decision_price(market.token_up, up_bid, up_ask)
        dn_price = self._decision_price(market.token_down, dn_bid, dn_ask)

        # Keep each open position's latest price for settlement (winner -> ~1,
        # loser -> ~0 near expiry). Fall back to bid when the ask is empty.
        for pos in (self.open_position, self.hedge_position):
            if pos is None:
                continue
            is_up = pos["token_id"] == market.token_up
            px = (up_price if is_up else dn_price)
            if px is None:
                px = (up_bid if is_up else dn_bid)
            if px is not None:
                pos["last_price"] = px

        # Track the recent price path so the strategy can require a rising
        # price when buying early (more than EARLY_THRESHOLD_SECONDS left).
        self._record_price_history(now, up_price, dn_price)
        up_rising, dn_rising = self._trend_flags()

        # A resting hedge fills the moment the opposite side trades down to it.
        self._try_fill_hedge(up_ask, dn_ask)

        # Stop-loss: cut a losing main position before it expires worthless.
        self._maybe_stop_loss(market, up_bid, dn_bid, time_to_end)

        btc = self.data.get_live_price()
        balance = self._get_balance(now)

        # The loop can spin several times a second; keep the log readable by
        # printing the status line at most once per status_log_interval.
        if now - self._last_status_log >= cfg.status_log_interval_seconds:
            self._last_status_log = now
            self.log.info(
                "t-%3ds | Up px=%s (bid %s/ask %s) | Down px=%s (bid %s/ask %s) | "
                "%s $%s target %s$%s | bal $%.2f | bought=%s",
                int(time_to_end),
                fmt(up_price), fmt(up_bid), fmt(up_ask),
                fmt(dn_price), fmt(dn_bid), fmt(dn_ask),
                cfg.asset.upper(), fmt_money(btc),
                "" if self.strike_is_exact else "~",
                fmt_money(self.strike), balance,
                self.bought_this_window,
            )

        # Stop if the account is depleted (requirement 9).
        if balance <= cfg.min_balance_usdc:
            self.stop_reason = (
                f"balance depleted (${balance:.2f} <= ${cfg.min_balance_usdc:.2f})"
            )
            return

        decision = evaluate(
            up_price=up_price,
            down_price=dn_price,
            time_to_end=time_to_end,
            balance=balance,
            token_up=market.token_up,
            token_down=market.token_down,
            time_window=cfg.time_window_seconds,
            price_min=cfg.price_min,
            price_max=cfg.price_max,
            trade_size=cfg.trade_size_usdc,
            already_bought=self.bought_this_window,
            btc_price=btc,
            target_open=self.strike,
            min_target_distance=cfg.min_target_distance_usdc,
            early_threshold=cfg.early_threshold_seconds,
            early_price_min=cfg.early_price_min,
            early_require_rising=cfg.early_require_rising,
            up_rising=up_rising,
            down_rising=dn_rising,
        )

        if decision.should_buy:
            self._execute(market, decision, btc, time_to_end, up_ask, dn_ask)
        else:
            self.log.debug("no trade: %s", decision.reason)

    def _handle_window_rollover(self, market: Market) -> None:
        if market.condition_id == self.current_condition:
            return
        # The previous window just ended — settle any position(s) held in it.
        self._settle_position()   # main bet (no-op if none)
        self._settle_hedge()      # opposite-side hedge (no-op if none)
        if self.pending_hedge is not None:
            self.log.warning("HEDGE expired unfilled (window ended): %s @ $%.2f",
                             self.pending_hedge["outcome"], self.pending_hedge["price"])
            self.pending_hedge = None
        self.current_condition = market.condition_id
        self.bought_this_window = False
        self.price_history.clear()   # a new window means new tokens/prices
        self.data.watch_market_books(market)   # re-point the live book feed
        # Read the EXACT target (window open price) from Polymarket's own feed;
        # fall back to Coinbase spot only if that call fails. _refresh_strike
        # keeps trying each tick until the exact value is in hand.
        self.strike = None
        self.strike_is_exact = False
        self._refresh_strike(market)
        self.log.info(
            "── New window %s | ends in %ds | target(open) %s$%s%s ──",
            market.slug,
            int(market.seconds_to_end()),
            "" if self.strike_is_exact else "~",
            fmt_money(self.strike),
            " (exact, Polymarket)" if self.strike_is_exact else " (approx)",
        )

    def _record_price_history(self, now: float, up_price, dn_price) -> None:
        """Append this tick's prices; keep only the trend lookback horizon."""
        self.price_history.append((now, up_price, dn_price))
        horizon = self.cfg.trend_lookback_seconds
        while self.price_history and now - self.price_history[0][0] > horizon:
            self.price_history.popleft()

    def _trend_flags(self) -> Tuple[Optional[bool], Optional[bool]]:
        """(up_rising, down_rising) over the lookback window.

        "Rising" means the newest price is at/above the oldest kept sample —
        the price climbed (or is holding at the top); a falling price is never
        "rising". None = not enough history yet to judge (e.g. right after a
        window rollover), which the strategy treats as "do not buy early".
        """
        hist = self.price_history
        if len(hist) < 2:
            return None, None
        ts0, up0, dn0 = hist[0]
        tsn, upn, dnn = hist[-1]
        # Need a few seconds of real history before calling a trend.
        if tsn - ts0 < min(self.cfg.trend_lookback_seconds / 2.0, 3.0):
            return None, None
        up_rising = (upn >= up0) if (upn is not None and up0 is not None) else None
        dn_rising = (dnn >= dn0) if (dnn is not None and dn0 is not None) else None
        return up_rising, dn_rising

    def _refresh_strike(self, market: Market) -> None:
        """Set self.strike to the exact Polymarket target; approximate until then."""
        if self.strike_is_exact:
            return
        exact = self.data.get_target_price(market)
        if exact is not None:
            was_approx = self.strike is not None
            self.strike = exact
            self.strike_is_exact = True
            if was_approx:
                self.log.info("Target refined to exact (Polymarket): $%s",
                              fmt_money(exact))
        elif self.strike is None:
            # endpoint not ready/available yet — show the live price meanwhile
            self.strike = self.data.get_live_price()

    def _settle_position(self) -> None:
        """Settle the just-ended window's MAIN bet (no-op if there isn't one)."""
        if self.open_position is None:
            return
        pos = self.open_position
        self.open_position = None
        self._book_settlement(pos)

    def _settle_hedge(self) -> None:
        """Settle the just-ended window's opposite-side HEDGE (no-op if none)."""
        if self.hedge_position is None:
            return
        pos = self.hedge_position
        self.hedge_position = None
        self._book_settlement(pos, hedge=True)

    def _book_settlement(self, pos: dict, hedge: bool = False) -> None:
        """Resolve one position and book the profit/loss.

        Win/loss is read from the bought outcome's last observed price: near
        expiry the winner trades at ~1.00 and the loser at ~0.00. On a win each
        share pays $1.00; on a loss the shares are worth nothing. In dry-run the
        payout is credited to the simulated balance; in live mode Polymarket has
        already done this, so trader.settle() is a no-op (this only tracks P&L).
        """
        last = pos.get("last_price")
        won = last is not None and last >= 0.5
        payout = round(pos["size"] * 1.0, 2) if won else 0.0
        profit = round(payout - pos["cost"], 2)
        self.realized_pnl += profit
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.trader.settle(payout)
        self._invalidate_balance()
        self.log.warning(
            "SETTLED%s %s %s — %s | %.2f shares | payout $%.2f | "
            "P&L %+.2f (total %+.2f, W/L %d/%d)",
            " HEDGE" if hedge else "",
            pos["slug"], pos["outcome"], "WON" if won else "LOST",
            pos["size"], payout, profit, self.realized_pnl, self.wins, self.losses,
        )
        try:
            balance_after = self.trader.get_balance()
        except Exception:  # noqa: BLE001 - logging must not break the loop
            balance_after = None
        self.tradelog.append(
            {
                "settled_at_utc": _utc_now(),
                "slug": pos["slug"],
                "outcome": ("HEDGE " if hedge else "") + pos["outcome"],
                "entry_price": pos.get("entry_price"),
                "shares": pos["size"],
                "cost": pos["cost"],
                "target_open": pos.get("target_open"),
                "btc_at_entry": pos.get("btc_at_entry"),
                "secs_to_end_at_entry": pos.get("secs_to_end_at_entry"),
                "result": "WON" if won else "LOST",
                "settle_price": last,
                "payout": payout,
                "pnl": profit,
                "cumulative_pnl": round(self.realized_pnl, 2),
                "balance_after": round(balance_after, 2) if balance_after is not None else "",
            }
        )

    def _decision_price(
        self, token: str, bid: Optional[float], ask: Optional[float]
    ) -> Optional[float]:
        src = self.cfg.price_source
        if src == "mid":
            mid = self.data.get_midpoint(token)
            if mid is not None:
                return mid
            return (bid + ask) / 2 if bid is not None and ask is not None else None
        if src == "last":
            return self.data.get_last_price(token)
        return ask  # default: the price you actually pay to buy

    def _execute(self, market: Market, decision, btc, time_to_end,
                 up_ask=None, dn_ask=None) -> None:
        cfg = self.cfg
        price = round(decision.price, 2)
        # Whole shares only. Polymarket rejects (market) buy orders whose maker
        # amount (USDC = price*size) has more than 2 decimals, e.g. 50.50 @ 0.99
        # = $49.995 -> "invalid amounts". An integer size * a 2-decimal price is
        # always <= 2 decimals, matching the orders that filled successfully.
        size = float(int(cfg.trade_size_usdc / price))

        if size < market.order_min_size:
            self.log.warning(
                "Computed size %.2f < market minimum %.2f shares. "
                "Increase TRADE_SIZE_USDC. Skipping this window.",
                size, market.order_min_size,
            )
            self.bought_this_window = True  # don't spam the same warning each second
            return

        # Mark the window done BEFORE sending, so a transient error can never
        # cause a second order in the same window (requirement 8).
        self.bought_this_window = True
        cost = round(price * size, 2)
        self.log.warning(
            "BUY %s — %.2f shares @ $%.2f (~$%.2f) on %s [%s]",
            decision.outcome, size, price, cost, market.slug, decision.reason,
        )

        try:
            resp = self.trader.buy(decision.token_id, price, size)
        except Exception as exc:  # noqa: BLE001
            self.log.error(
                "Order placement FAILED: %s "
                "(window marked done to avoid a duplicate order)", exc,
            )
            return
        self._invalidate_balance()

        self.log.warning("Order response: %s", resp)
        self.trades.append(
            {
                "slug": market.slug,
                "outcome": decision.outcome,
                "price": price,
                "size": size,
                "cost": cost,
                "response": resp,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        # Track the position so it can be settled when this window resolves.
        self.open_position = {
            "slug": market.slug,
            "outcome": decision.outcome,
            "token_id": decision.token_id,
            "size": size,
            "cost": cost,
            "last_price": price,
            "entry_price": price,
            "target_open": self.strike,
            "btc_at_entry": btc,
            "secs_to_end_at_entry": int(time_to_end),
        }

        # Optional: arm the opposite-side lottery hedge (rests at hedge_price).
        self._arm_hedge(market, decision, btc, time_to_end, up_ask, dn_ask)

    def _arm_hedge(self, market: Market, decision, btc, time_to_end,
                   up_ask, dn_ask) -> None:
        """Arm a resting hedge on the OPPOSITE outcome at hedge_price (e.g. 0.01).

        If the favourite loses, the opposite pays ~$1/share and can offset the
        loss. The order RESTS, so it fills only when the opposite side trades
        down to hedge_price:
          * live    — a real GTC order is placed now and rests on the book;
          * dry-run — it is held and filled on the first later tick where the
            opposite ask reaches hedge_price (see _try_fill_hedge).
        """
        cfg = self.cfg
        if not cfg.hedge_enabled:
            return

        opp_name, opp_token = (
            ("Down", market.token_down) if decision.outcome == "Up"
            else ("Up", market.token_up)
        )
        price = round(cfg.hedge_price, 2)
        size = floor2(cfg.hedge_size_usdc / price)
        if size < market.order_min_size:
            self.log.warning(
                "HEDGE skipped: size %.2f < market minimum %.2f shares.",
                size, market.order_min_size,
            )
            return

        order = {
            "slug": market.slug, "outcome": opp_name, "token_id": opp_token,
            "price": price, "size": size, "target_open": self.strike,
            "btc_at_entry": btc, "secs_to_end_at_entry": int(time_to_end),
        }

        if not cfg.dry_run:
            # Live: rest a GTC order on the book; it fills server-side over time.
            # (P&L tracking assumes it fills — reconcile with Polymarket.)
            self.log.warning(
                "HEDGE resting order: %s %.2f shares @ $%.2f on %s",
                opp_name, size, price, market.slug,
            )
            try:
                resp = self.trader.buy(opp_token, price, size, resting=True)
            except Exception as exc:  # noqa: BLE001
                self.log.error("Hedge order placement FAILED: %s", exc)
                return
            self._invalidate_balance()
            self.log.warning("Hedge order response: %s", resp)
            self._record_hedge_fill(order, resp)
            return

        # Dry-run: hold the resting order and try to fill it now and each tick.
        self.pending_hedge = order
        self.log.warning(
            "HEDGE armed (resting): %s %.2f shares @ $%.2f — waits for the "
            "opposite to trade down to $%.2f.",
            opp_name, size, price, price,
        )
        self._try_fill_hedge(up_ask, dn_ask)

    def _try_fill_hedge(self, up_ask, dn_ask) -> None:
        """Fill the pending (dry-run) resting hedge once the opposite reaches it."""
        ph = self.pending_hedge
        if ph is None:
            return
        opp_ask = up_ask if ph["outcome"] == "Up" else dn_ask
        if opp_ask is None or opp_ask > ph["price"]:
            return  # no seller at/below the hedge price yet — keep waiting
        try:
            resp = self.trader.buy(ph["token_id"], ph["price"], ph["size"])
        except Exception as exc:  # noqa: BLE001
            self.log.error("Hedge fill FAILED: %s", exc)
            self.pending_hedge = None
            return
        self._invalidate_balance()
        self.log.warning(
            "HEDGE FILLED %s — %.2f shares @ $%.2f (~$%.2f) on %s",
            ph["outcome"], ph["size"], ph["price"],
            round(ph["price"] * ph["size"], 2), ph["slug"],
        )
        self.log.warning("Hedge order response: %s", resp)
        self._record_hedge_fill(ph, resp)
        self.pending_hedge = None

    def _record_hedge_fill(self, order: dict, resp) -> None:
        cost = round(order["price"] * order["size"], 2)
        self.trades.append(
            {
                "slug": order["slug"],
                "outcome": "HEDGE " + order["outcome"],
                "price": order["price"],
                "size": order["size"],
                "cost": cost,
                "response": resp,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.hedge_position = {
            "slug": order["slug"],
            "outcome": order["outcome"],
            "token_id": order["token_id"],
            "size": order["size"],
            "cost": cost,
            "last_price": order["price"],
            "entry_price": order["price"],
            "target_open": order.get("target_open"),
            "btc_at_entry": order.get("btc_at_entry"),
            "secs_to_end_at_entry": order.get("secs_to_end_at_entry"),
        }

    # -- stop-loss ----------------------------------------------------------
    def _maybe_stop_loss(self, market: Market, up_bid, dn_bid,
                         time_to_end: float) -> None:
        """Sell the open MAIN position to cut losses (no-op if disabled/none).

        Watches the price we could actually SELL at (the bid):
          * hard floor — bid <= stoploss_hard_price → sell right now,
            regardless of how much time is left;
          * soft floor — bid <= stoploss_soft_price AND the window has
            stoploss_soft_secs_to_end seconds or less left → sell before it
            can expire worthless.
        Only the main position is touched; the hedge is left as-is.
        """
        cfg = self.cfg
        if not cfg.stoploss_enabled:
            return
        pos = self.open_position
        if pos is None:
            return

        is_up = pos["token_id"] == market.token_up
        bid = up_bid if is_up else dn_bid
        if bid is None:
            return  # no buyer visible — can't value or exit yet

        # Hard floor: dump immediately, whatever the time left.
        if bid <= cfg.stoploss_hard_price:
            self._stop_loss_sell(
                pos, bid,
                reason=f"hard stop: bid {bid:.2f} <= {cfg.stoploss_hard_price:.2f}",
            )
            return

        # Soft floor: only in the final seconds before the window ends.
        if (bid <= cfg.stoploss_soft_price
                and time_to_end <= cfg.stoploss_soft_secs_to_end):
            self._stop_loss_sell(
                pos, bid,
                reason=(
                    f"soft stop: bid {bid:.2f} <= {cfg.stoploss_soft_price:.2f} "
                    f"with {time_to_end:.0f}s to window end"
                ),
            )

    def _stop_loss_sell(self, pos: dict, bid: float, reason: str) -> None:
        """Sell the whole main position at the current bid and book the result."""
        price = round(bid, 2)
        size = pos["size"]
        proceeds = round(price * size, 2)
        self.log.warning(
            "STOP-LOSS SELL %s — %.2f shares @ $%.2f (~$%.2f) on %s [%s]",
            pos["outcome"], size, price, proceeds, pos["slug"], reason,
        )
        try:
            resp = self.trader.sell(pos["token_id"], price, size)
        except Exception as exc:  # noqa: BLE001 - keep the bot alive
            self.log.error(
                "Stop-loss SELL FAILED: %s (position left open to retry)", exc
            )
            return

        self.log.warning("Sell order response: %s", resp)
        # Credit the cash we recovered (dry-run uses the same path as a payout;
        # live mode already received USDC, so settle() is a no-op there).
        self.trader.settle(proceeds)
        self._invalidate_balance()

        # Close the position so the window rollover won't settle it again.
        self.open_position = None
        pnl = round(proceeds - pos["cost"], 2)
        self.realized_pnl += pnl
        self.losses += 1
        self.log.warning(
            "STOP-LOSS DONE %s %s — recovered $%.2f | P&L %+.2f "
            "(total %+.2f, W/L %d/%d)",
            pos["slug"], pos["outcome"], proceeds, pnl,
            self.realized_pnl, self.wins, self.losses,
        )

        self.trades.append(
            {
                "slug": pos["slug"],
                "outcome": "SELL " + pos["outcome"],
                "price": price,
                "size": size,
                "cost": proceeds,
                "response": resp,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        try:
            balance_after = self.trader.get_balance()
        except Exception:  # noqa: BLE001
            balance_after = None
        self.tradelog.append(
            {
                "settled_at_utc": _utc_now(),
                "slug": pos["slug"],
                "outcome": "SELL " + pos["outcome"],
                "entry_price": pos.get("entry_price"),
                "shares": size,
                "cost": pos["cost"],
                "target_open": pos.get("target_open"),
                "btc_at_entry": pos.get("btc_at_entry"),
                "secs_to_end_at_entry": pos.get("secs_to_end_at_entry"),
                "result": "STOPLOSS",
                "settle_price": price,
                "payout": proceeds,
                "pnl": pnl,
                "cumulative_pnl": round(self.realized_pnl, 2),
                "balance_after": round(balance_after, 2) if balance_after is not None else "",
            }
        )

    # -- output -------------------------------------------------------------
    def _banner(self) -> None:
        cfg = self.cfg
        mode = "DRY-RUN (no real orders)" if cfg.dry_run else "*** LIVE TRADING ***"
        self.log.info("=" * 70)
        self.log.info("Polymarket %s%s bot — %s",
                      cfg.asset.upper(), cfg.duration_label, mode)
        self.log.info(
            "Buy when: outcome price in [%.2f, %.2f] AND <= %ds to end AND "
            "balance >= $%.2f",
            cfg.price_min, cfg.price_max, int(cfg.time_window_seconds),
            cfg.trade_size_usdc,
        )
        if cfg.early_threshold_seconds > 0:
            self.log.info(
                "Early/late rule: with > %ds left buy ONLY at [%.2f, %.2f]%s; "
                "with <= %ds left the normal band [%.2f, %.2f] applies.",
                int(cfg.early_threshold_seconds),
                max(cfg.price_min, cfg.early_price_min), cfg.price_max,
                (" and only while the price is rising"
                 f" (lookback {int(cfg.trend_lookback_seconds)}s)")
                if cfg.early_require_rising else "",
                int(cfg.early_threshold_seconds),
                cfg.price_min, cfg.price_max,
            )
        if cfg.min_target_distance_usdc > 0:
            self.log.info(
                "Extra filter: %s must be >= $%.2f from the window open "
                "(target) in the favourite's direction.",
                cfg.asset.upper(), cfg.min_target_distance_usdc,
            )
        if cfg.hedge_enabled:
            self.log.info(
                "Hedge ON: after each buy, also bet $%.2f on the OPPOSITE "
                "outcome at $%.2f (fills only if a seller is that low).",
                cfg.hedge_size_usdc, cfg.hedge_price,
            )
        if cfg.stoploss_enabled:
            self.log.info(
                "Stop-loss ON: sell if bid <= $%.2f with <= %.0fs left in the "
                "window, or immediately if bid <= $%.2f.",
                cfg.stoploss_soft_price, cfg.stoploss_soft_secs_to_end,
                cfg.stoploss_hard_price,
            )
        self.log.info(
            "Trade size $%.2f | price source '%s' | poll %.1fs | runs %.1fh | "
            "stop if balance <= $%.2f",
            cfg.trade_size_usdc, cfg.price_source, cfg.poll_interval_seconds,
            cfg.run_duration_seconds / 3600.0, cfg.min_balance_usdc,
        )
        self.log.info("=" * 70)

    def _summary(self) -> None:
        self.data.stop_feed()
        self.log.info("=" * 70)
        self.log.info("Bot stopped: %s", self.stop_reason or "unknown")
        self.log.info("Trades placed: %d", len(self.trades))
        for t in self.trades:
            self.log.info(
                "  • %s %s %.2f @ $%.2f (~$%.2f) [%s]",
                t["time"], t["outcome"], t["size"], t["price"], t["cost"], t["slug"],
            )
        settled = self.wins + self.losses
        pnl_label = "simulated" if self.cfg.dry_run else "estimated"
        self.log.info(
            "Settled: %d (wins %d / losses %d) | %s realized P&L: $%+.2f",
            settled, self.wins, self.losses, pnl_label, self.realized_pnl,
        )
        for label, p in (("main", self.open_position), ("hedge", self.hedge_position)):
            if p is None:
                continue
            self.log.info(
                "Open %s (unsettled, window not finished): %s %.2f @ $%.2f on %s",
                label, p["outcome"], p["size"], p["cost"] / p["size"], p["slug"],
            )
            self.tradelog.append(
                {
                    "settled_at_utc": _utc_now(),
                    "slug": p["slug"],
                    "outcome": ("HEDGE " if label == "hedge" else "") + p["outcome"],
                    "entry_price": p.get("entry_price"),
                    "shares": p["size"],
                    "cost": p["cost"],
                    "target_open": p.get("target_open"),
                    "btc_at_entry": p.get("btc_at_entry"),
                    "secs_to_end_at_entry": p.get("secs_to_end_at_entry"),
                    "result": "UNSETTLED",
                    "settle_price": p.get("last_price"),
                }
            )
        if self.tradelog.enabled and (
            self.wins + self.losses or self.open_position or self.hedge_position
        ):
            self.log.info("Trade log written to: %s", self.tradelog.path)
        try:
            self.log.info("Final balance: $%.2f", self.trader.get_balance())
        except Exception as exc:  # noqa: BLE001
            self.log.info("Final balance: unavailable (%s)", exc)
        self.log.info("=" * 70)
