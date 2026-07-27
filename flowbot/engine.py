"""Асинхронный движок flowbot: фиды цены + книга + стратегия + трейдер.

Собирает всё вместе:

  * фиды цены из fast_monitor (7 бирж + якорь Polymarket) наполняют
    консенсус; PriceEngine считает всплеск скорости;
  * поток CLOB (тот же, что рисует «Книгу заявок» на сайте) наполняет
    BookEngine — обе стороны Up/Down, доскачковый ask, поток заявок;
  * каждый такт строится снимок рынка и отдаётся чистой стратегии;
  * действия (вход/выход/разворот) исполняются трейдером через пул потоков,
    чтобы не блокировать событийный цикл; в dry-run исполнение ЗАДЕРЖИВАЕТСЯ
    на реалистичный пинг и цена берётся уже на момент «прилёта» ордера.

Движок владеет ФАКТИЧЕСКОЙ позицией (шэры, стоимость, P&L); стратегия владеет
логикой решений. По факту филла движок сообщает стратегии record_entry/exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import fast_monitor
import book_monitor
from book_monitor import CLOB_WS, WINDOW_SECONDS, discover_market, loads

from btc_bot.config import Config as BtcConfig
from btc_bot.trader import build_trader
from btc_bot.tradelog import TradeLogger
from btc_bot.util import floor2

from .config import FlowConfig
from .latency import LatencyModel
from .signals import BookEngine, PriceEngine
from .strategy import ENTER, EXIT, FLIP, FlowStrategy, MarketSnapshot

try:
    import websockets
except ImportError:  # pragma: no cover
    raise SystemExit("Установите зависимость:  pip install websockets")


def now_ms() -> float:
    return time.time() * 1000.0


class FlowEngine:
    def __init__(self, cfg: FlowConfig, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.log = logger or logging.getLogger("flowbot")

        self.price = PriceEngine(cfg)
        self.book = BookEngine(cfg)
        self.latency = LatencyModel(cfg, self.log)
        self.strategy = FlowStrategy(cfg)

        # Трейдер (live/dry) поверх btc_bot: тот же CLOB-клиент и баланс.
        btc_cfg = BtcConfig.from_env()
        btc_cfg.dry_run = cfg.dry_run
        btc_cfg.asset = cfg.asset
        self._btc_cfg = btc_cfg
        self.trader = build_trader(btc_cfg, self.log)
        self.tradelog = TradeLogger(cfg.trade_log_csv)

        self.market: Optional[dict] = None       # {up,down,window_ts,end_ts,slug,question}
        self.pos: Optional[dict] = None           # фактическая позиция движка
        self.busy = False                         # ордер в полёте
        self._stop = False
        self.stop_reason: Optional[str] = None
        self.consec_err = 0

        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.n_trades = 0

        self._balance: Optional[float] = None
        self._balance_ts = 0.0
        self._last_status = 0.0

    # ======================================================================
    #  Запуск
    # ======================================================================
    def run(self) -> None:
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            self.stop_reason = "остановлено пользователем (Ctrl-C)"
            self._summary()

    async def run_async(self) -> None:
        self._banner()
        fast_monitor.COIN = fast_monitor.COINS[self.cfg.asset]

        # Пинг меряем в фоне — фиды тем временем прогреваются.
        asyncio.create_task(self._measure_ping())

        feeds = fast_monitor.feed_tasks(
            no_pyth=self.cfg.no_pyth, no_binance=self.cfg.no_binance,
            no_okx=self.cfg.no_okx, no_bybit=self.cfg.no_bybit,
            no_pm=self.cfg.no_polymarket_anchor,
        )
        bg = [asyncio.create_task(c) for c in feeds]
        bg.append(asyncio.create_task(self._book_loop()))
        for extra in self._extra_tasks():
            bg.append(asyncio.create_task(extra))

        deadline = time.time() + self.cfg.run_duration_seconds
        try:
            await self._run_loop(deadline)
        finally:
            self._stop = True
            if not self.busy:
                self._settle_open_position("остановка движка")
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
            self._summary()

    def _extra_tasks(self):
        """Дополнительные фоновые корутины подкласса (по умолчанию — нет).

        Скачковой стратегии нужен ещё и таргет раунда с Polymarket, обычному
        flowbot — нет, поэтому лишний HTTP-опрос не поднимаем без нужды.
        """
        return []

    async def _measure_ping(self) -> None:
        await asyncio.to_thread(self.latency.measure, "clob.polymarket.com",
                                443, 4, 2.5)
        self.log.info(self.latency.describe())

    # ======================================================================
    #  Поток книги (Бот 2) — по одному 5-мин окну за раз
    # ======================================================================
    async def _book_loop(self) -> None:
        while not self._stop:
            try:
                market = await discover_market(self.cfg.asset)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("поиск рынка: %s; повтор через 2с", exc)
                await asyncio.sleep(2)
                continue
            self._enter_window(market)
            await self._stream_window(market)
            if not self.busy:
                self._settle_open_position("окно закончилось")

    def _enter_window(self, market: dict) -> None:
        end_ts = market["window_ts"] + WINDOW_SECONDS
        self.market = {**market, "end_ts": end_ts}
        self.book.set_tokens(market["up"], market["down"])
        self.log.info(
            "── окно %s | до %s | Up=%s… Down=%s… ──",
            market["slug"],
            time.strftime("%H:%M:%S", time.localtime(end_ts)),
            str(market["up"])[:10], str(market["down"])[:10],
        )

    async def _stream_window(self, market: dict) -> None:
        end_ts = market["window_ts"] + WINDOW_SECONDS
        sub = json.dumps({"assets_ids": [market["up"], market["down"]],
                          "type": "market"})
        backoff = 0.5
        while time.time() < end_ts + 1 and not self._stop:
            try:
                async with websockets.connect(
                        CLOB_WS, compression=None, open_timeout=8,
                        user_agent_header="Mozilla/5.0") as ws:
                    await ws.send(sub)
                    backoff = 0.5
                    last_ping = time.time()
                    while time.time() < end_ts + 1 and not self._stop:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if time.time() - last_ping >= 5:
                                await ws.send("PING")
                                last_ping = time.time()
                            continue
                        self._ingest(raw)
            except Exception as exc:  # noqa: BLE001
                if time.time() >= end_ts:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _ingest(self, raw) -> None:
        if not isinstance(raw, str) or not raw.startswith(("{", "[")):
            return
        try:
            data = loads(raw)
        except Exception:  # noqa: BLE001
            return
        recv = time.time()
        events = data if isinstance(data, list) else [data]
        changed = False
        for ev in events:
            if isinstance(ev, dict) and self.book.on_event(ev, recv):
                changed = True
        if changed:
            fast_monitor.UPDATE_EVENT.set()   # разбудить цикл решений

    # ======================================================================
    #  Главный цикл решений
    # ======================================================================
    async def _run_loop(self, deadline: float) -> None:
        while time.time() < deadline and not self._stop:
            try:
                await asyncio.wait_for(fast_monitor.UPDATE_EVENT.wait(),
                                       timeout=self.cfg.tick_max_wait_seconds)
            except asyncio.TimeoutError:
                pass
            fast_monitor.UPDATE_EVENT.clear()
            try:
                self._tick()
                self.consec_err = 0
            except Exception as exc:  # noqa: BLE001 - цикл должен жить
                self.consec_err += 1
                self.log.warning("тик упал (%d/%d): %s", self.consec_err,
                                 self.cfg.max_consecutive_errors, exc)
                if self.consec_err >= self.cfg.max_consecutive_errors:
                    self.stop_reason = "слишком много ошибок подряд — стоп"
                    return
            if self.stop_reason:
                return
        if not self.stop_reason:
            self.stop_reason = "истекло время работы"

    def _tick(self) -> None:
        cfg = self.cfg
        t_ms = now_ms()
        self.price.update(t_ms)
        price = self.price.price()
        if self.market is None:
            return
        up, dn = self.market["up"], self.market["down"]
        ub, ua = self.book.best(up)
        db, da = self.book.best(dn)
        burst_z, move_bps = self.price.burst()

        # запоминаем последний бид нашей стороны для сеттла
        if self.pos is not None:
            self.pos["last_bid"] = ub if self.pos["outcome"] == "Up" else db

        snap = MarketSnapshot(
            t=time.time(),
            seconds_left=max(0.0, self.market["end_ts"] - time.time()),
            btc_price=price, burst_z=burst_z, move_bps=move_bps,
            up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
            up_ask_ref=self.book.ask_ref(up, cfg.chase_lookback_s),
            down_ask_ref=self.book.ask_ref(dn, cfg.chase_lookback_s),
            up_flow=self.book.flow(up, cfg.flow_window_s),
            down_flow=self.book.flow(dn, cfg.flow_window_s),
        )

        action = self.strategy.on_tick(snap)
        self._log_status(snap, action)

        if action.kind in (ENTER, EXIT, FLIP) and not self.busy:
            self.busy = True
            asyncio.create_task(self._execute(action))

    # ======================================================================
    #  Исполнение (с реалистичным пингом)
    # ======================================================================
    async def _execute(self, action) -> None:
        try:
            self.log.warning(">>> %s: %s", action.kind.upper(), action.reason)
            if action.kind == ENTER:
                await self._buy(action.outcome, action.limit_price,
                                action.size_usdc, is_flip=False)
            elif action.kind == EXIT:
                await self._sell_current(action.sell_limit, "выход")
            elif action.kind == FLIP:
                # Предосторожность №2: сначала «тут же» берём противоположную
                # сторону (ловим разворот), ПОТОМ сбрасываем старую позицию.
                old = self.pos
                await self._buy(action.outcome, action.limit_price,
                                action.size_usdc, is_flip=True)
                await self._sell_position(old, action.sell_limit,
                                          "разворот-выход")
        except Exception as exc:  # noqa: BLE001
            self.log.error("исполнение упало: %s", exc)
        finally:
            self.busy = False

    async def _sim_latency(self) -> None:
        if self.cfg.dry_run and self.cfg.simulate_latency:
            await asyncio.sleep(self.latency.fill_delay_s)

    async def _buy(self, outcome: str, limit_price: Optional[float],
                   size_usdc: float, is_flip: bool) -> None:
        if self.market is None:
            return
        token = self.market["up"] if outcome == "Up" else self.market["down"]
        await self._sim_latency()   # книга могла уйти за время пинга
        bid, ask = self.book.best(token)
        if ask is None:
            self.log.warning("ПОКУПКА %s не исполнена: нет ask", outcome)
            return
        if limit_price is not None and ask > limit_price + 1e-9:
            self.log.warning(
                "ПОКУПКА %s НЕ исполнена: ask %.2f > лимит %.2f — книга ушла "
                "за пинг ~%.0fмс (предосторожность №1: не гонимся)",
                outcome, ask, limit_price, self.latency.fill_delay_ms)
            return
        fill = round(ask, 2)
        shares = floor2(size_usdc / fill)
        if shares <= 0:
            self.log.warning("ПОКУПКА %s: размер %.4f шэров <= 0, пропуск",
                             outcome, shares)
            return
        cost = round(fill * shares, 2)
        try:
            resp = await asyncio.to_thread(self.trader.buy, token, fill, shares)
        except Exception as exc:  # noqa: BLE001
            self.log.error("ордер BUY упал: %s", exc)
            return
        self._invalidate_balance()
        self.n_trades += 1
        self.pos = {
            "outcome": outcome, "token": token, "entry_price": fill,
            "shares": shares, "cost": cost, "last_bid": bid,
            "is_flip": is_flip, "btc_at_entry": self.price.price(),
            "secs_at_entry": int(self.market["end_ts"] - time.time()),
        }
        self.strategy.record_entry(outcome, fill, size_usdc, time.time(),
                                   is_flip=is_flip)
        self.log.warning(
            "КУПЛЕНО%s %s — %.2f шэр @ $%.2f (=$%.2f) | ответ: %s",
            " (разворот)" if is_flip else "", outcome, shares, fill, cost,
            _short(resp))

    async def _sell_current(self, sell_limit: Optional[float],
                            tag: str) -> None:
        await self._sell_position(self.pos, sell_limit, tag)

    async def _sell_position(self, pos: Optional[dict],
                             sell_limit: Optional[float], tag: str) -> None:
        """Продать конкретную позицию. Если это ТЕКУЩАЯ позиция движка —
        обнулить её и сообщить стратегии о выходе; если старая (при флипе,
        когда текущая уже стала противоположной) — только зафиксировать P&L."""
        if pos is None:
            return
        token = pos["token"]
        await self._sim_latency()
        bid, ask = self.book.best(token)
        px = bid if bid is not None else (pos.get("last_bid") or 0.0)
        fill = round(px, 2)
        shares = pos["shares"]
        proceeds = round(fill * shares, 2)
        try:
            resp = await asyncio.to_thread(self.trader.sell, token, fill, shares)
        except Exception as exc:  # noqa: BLE001
            self.log.error("ордер SELL упал: %s (позиция оставлена)", exc)
            return
        self.trader.settle(proceeds)     # dry-run: вернуть кэш; live: no-op
        self._invalidate_balance()
        pnl = round(proceeds - pos["cost"], 2)
        self.realized_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.log.warning(
            "ПРОДАНО (%s) %s — %.2f шэр @ $%.2f (=$%.2f) | P&L %+.2f "
            "(итого %+.2f) | ответ: %s",
            tag, pos["outcome"], shares, fill, proceeds, pnl,
            self.realized_pnl, _short(resp))
        self._log_trade_row(pos, tag.upper(), fill, proceeds, pnl)
        if self.pos is pos:                 # продали текущую -> выходим во flat
            self.pos = None
            self.strategy.record_exit()

    def _settle_open_position(self, reason: str) -> None:
        """Сеттл открытой позиции на границе окна (держали до расчёта)."""
        pos = self.pos
        if pos is None:
            return
        last = pos.get("last_bid")
        won = last is not None and last >= 0.5
        payout = round(pos["shares"] * 1.0, 2) if won else 0.0
        pnl = round(payout - pos["cost"], 2)
        self.realized_pnl += pnl
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.trader.settle(payout)
        self._invalidate_balance()
        self.log.warning(
            "СЕТТЛ (%s) %s %s — payout $%.2f | P&L %+.2f (итого %+.2f, W/L %d/%d)",
            reason, pos["outcome"], "ВЫИГРЫШ" if won else "проигрыш",
            payout, pnl, self.realized_pnl, self.wins, self.losses)
        self._log_trade_row(pos, "WON" if won else "LOST",
                            last if last is not None else 0.0, payout, pnl)
        self.pos = None
        self.strategy.record_exit()

    # ======================================================================
    #  Баланс / лог / вывод
    # ======================================================================
    def _get_balance(self) -> float:
        now = time.time()
        if self._balance is None or now - self._balance_ts >= 5.0:
            try:
                self._balance = self.trader.get_balance()
            except Exception:  # noqa: BLE001
                self._balance = self._balance if self._balance is not None \
                    else self._btc_cfg.dry_run_balance
            self._balance_ts = now
        return self._balance

    def _invalidate_balance(self) -> None:
        self._balance = None

    def _log_trade_row(self, pos: dict, result: str, settle_price: float,
                       payout: float, pnl: float) -> None:
        try:
            bal = self.trader.get_balance()
        except Exception:  # noqa: BLE001
            bal = None
        self.tradelog.append({
            "settled_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "slug": (self.market or {}).get("slug", ""),
            "outcome": ("FLIP " if pos.get("is_flip") else "") + pos["outcome"],
            "entry_price": pos["entry_price"],
            "shares": pos["shares"],
            "cost": pos["cost"],
            "btc_at_entry": pos.get("btc_at_entry"),
            "secs_to_end_at_entry": pos.get("secs_at_entry"),
            "result": result,
            "settle_price": round(settle_price, 2),
            "payout": payout,
            "pnl": pnl,
            "cumulative_pnl": round(self.realized_pnl, 2),
            "balance_after": round(bal, 2) if bal is not None else "",
        })

    def _log_status(self, snap: MarketSnapshot, action) -> None:
        now = time.time()
        if now - self._last_status < self.cfg.status_log_interval_seconds:
            return
        self._last_status = now
        nc = self.price.nowcast()
        pos = ("—" if self.pos is None
               else f"{self.pos['outcome']}@{self.pos['entry_price']:.2f}")
        self.log.info(
            "t-%3ds | BTC %s%s z=%+.2f (%+.2fб.п.) | Up %s/%s(%+.2f) "
            "Down %s/%s(%+.2f) | поз %s | $%.2f | %s",
            int(snap.seconds_left),
            _m(snap.btc_price), (f" nc={nc:,.0f}" if nc else ""),
            snap.burst_z, snap.move_bps,
            _p(snap.up_bid), _p(snap.up_ask), snap.up_flow,
            _p(snap.down_bid), _p(snap.down_ask), snap.down_flow,
            pos, self._get_balance(), action.reason,
        )

    def _banner(self) -> None:
        c = self.cfg
        mode = "DRY-RUN (без реальных ордеров)" if c.dry_run else "*** LIVE ***"
        self.log.info("=" * 72)
        self.log.info("flowbot — %s Up/Down 5m — %s", c.asset.upper(), mode)
        self.log.info(
            "вход: резкое движение |z|>=%.1f (>=%.1fб.п.), сторона по движению, "
            "ask в [%.2f,%.2f], догон <=+%.0f центов; ставка $%.2f",
            c.entry_burst_z, c.entry_min_move_bps, c.entry_price_min,
            c.entry_price_max, c.chase_cents * 100, c.stake_usdc)
        self.log.info(
            "держим, пока идёт движение (z*dir>=%.1f) и растёт %% (откат <=%.2f); "
            "разворот при |z|>=%.1f -> $%.2f в противоход",
            c.hold_burst_z, c.token_retrace_exit, c.flip_burst_z, c.flip_size_usdc)
        self.log.info("=" * 72)

    def _summary(self) -> None:
        self.log.info("=" * 72)
        self.log.info("flowbot остановлен: %s", self.stop_reason or "—")
        settled = self.wins + self.losses
        self.log.info("сделок: %d | закрыто: %d (W/L %d/%d) | P&L: $%+.2f",
                      self.n_trades, settled, self.wins, self.losses,
                      self.realized_pnl)
        if self.pos is not None:
            self.log.info("открытая позиция (не сеттлилась): %s %.2f шэр @ $%.2f",
                          self.pos["outcome"], self.pos["shares"],
                          self.pos["entry_price"])
        try:
            self.log.info("баланс: $%.2f", self.trader.get_balance())
        except Exception as exc:  # noqa: BLE001
            self.log.info("баланс: недоступен (%s)", exc)
        if self.tradelog.enabled and settled:
            self.log.info("лог сделок: %s", self.tradelog.path)
        self.log.info("=" * 72)


def _p(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.2f}"


def _m(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _short(resp) -> str:
    s = str(resp)
    return s if len(s) <= 80 else s[:77] + "…"
