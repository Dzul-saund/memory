"""The buy / no-buy decision. Pure function, no I/O — easy to unit test.

Strategy (exactly as specified):
  Buy an outcome (Up or Down) when ALL of these hold:
    * the outcome's price is within [PRICE_MIN, PRICE_MAX]  (default 0.84..0.99)
    * the market ends in <= TIME_WINDOW_SECONDS               (default 70s)
    * the balance is enough for the trade
    * we have not already traded in this 5-minute window
    * (optional) the asset has moved at least MIN_TARGET_DISTANCE from the
      window's open price in the favourite's direction — a cushion that filters
      out near-ties. Disabled when min_target_distance == 0.
  Up is checked first, then Down. At most one of the two can ever be in the
  band (their prices are complementary, ~Up + Down = 1), so there is never a
  real conflict — but Up wins ties by construction.

Early/late split (optional, `early_threshold` > 0 enables it):
  * MORE than `early_threshold` seconds left (default 120s): buy only at
    `early_price_min`..PRICE_MAX (default 0.99..0.99, i.e. only at 0.99) and
    only while that outcome's price is trending UP (rising over the recent
    lookback — see the trend flags the caller passes in).
  * `early_threshold` seconds or less left: the normal PRICE_MIN..PRICE_MAX
    band applies (default 0.98..0.99) with no trend requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Decision:
    should_buy: bool
    reason: str
    outcome: Optional[str] = None      # "Up" or "Down"
    token_id: Optional[str] = None
    price: Optional[float] = None


def _fmt(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def evaluate(
    *,
    up_price: Optional[float],
    down_price: Optional[float],
    time_to_end: float,
    balance: float,
    token_up: str,
    token_down: str,
    time_window: float = 70.0,
    price_min: float = 0.84,
    price_max: float = 0.99,
    trade_size: float = 5.0,
    already_bought: bool = False,
    btc_price: Optional[float] = None,
    target_open: Optional[float] = None,
    min_target_distance: float = 0.0,
    early_threshold: float = 0.0,      # 0 disables the early/late split
    early_price_min: float = 0.99,     # early regime: buy only at >= this
    early_require_rising: bool = True, # early regime: price must be rising
    up_rising: Optional[bool] = None,  # is the Up price rising? None = unknown
    down_rising: Optional[bool] = None,
) -> Decision:
    """Return whether/what to buy for the current market snapshot."""
    if already_bought:
        return Decision(False, "already traded this window")

    if time_to_end > time_window:
        return Decision(
            False, f"too early: t-{time_to_end:.0f}s > {time_window:.0f}s window"
        )

    if balance < trade_size:
        return Decision(
            False,
            f"insufficient balance ${balance:.2f} < trade size ${trade_size:.2f}",
        )

    # Early/late split: with more than `early_threshold` seconds left the band
    # tightens to [early_price_min, price_max] (e.g. only 0.99) and the price
    # must be trending up; at/below the threshold the normal band applies.
    early = early_threshold > 0 and time_to_end > early_threshold
    eff_price_min = max(price_min, early_price_min) if early else price_min

    # Up is checked before Down (Up wins ties).
    for name, token, price in (
        ("Up", token_up, up_price),
        ("Down", token_down, down_price),
    ):
        if price is None or not (eff_price_min <= price <= price_max):
            continue

        if early and early_require_rising:
            rising = up_rising if name == "Up" else down_rising
            if rising is not True:
                return Decision(
                    False,
                    f"{name} price {price:.3f} in early band "
                    f"[{eff_price_min:.2f},{price_max:.2f}] "
                    f"(t-{time_to_end:.0f}s > {early_threshold:.0f}s) but its "
                    f"price is {'not rising' if rising is False else 'trend unknown yet'}",
                )

        # Optional cushion: BTC must have moved >= min_target_distance away from
        # the window's open price, in this outcome's favour (Up wants BTC above
        # the open, Down wants it below). This screens out near-ties at expiry.
        if min_target_distance > 0:
            if btc_price is None or target_open is None:
                return Decision(
                    False,
                    f"{name} in band but cannot check target distance "
                    f"(btc={_fmt(btc_price)}, target={_fmt(target_open)})",
                )
            cushion = (btc_price - target_open) if name == "Up" \
                else (target_open - btc_price)
            if cushion < min_target_distance:
                return Decision(
                    False,
                    f"{name} price {price:.3f} in band but cushion ${cushion:+.2f} "
                    f"< ${min_target_distance:.2f} from target ${target_open:,.2f}",
                )
            return Decision(
                True,
                f"{name} price {price:.3f} in [{eff_price_min:.2f},{price_max:.2f}], "
                f"cushion ${cushion:+.2f} >= ${min_target_distance:.2f}, "
                f"t-{time_to_end:.0f}s remaining"
                + (" [early: rising]" if early else ""),
                outcome=name,
                token_id=token,
                price=price,
            )

        return Decision(
            True,
            f"{name} price {price:.3f} in [{eff_price_min:.2f},{price_max:.2f}] "
            f"with t-{time_to_end:.0f}s remaining"
            + (" [early: rising]" if early else ""),
            outcome=name,
            token_id=token,
            price=price,
        )

    return Decision(
        False,
        "no outcome in price band "
        f"(Up={_fmt(up_price)}, Down={_fmt(down_price)}, "
        f"band [{eff_price_min:.2f},{price_max:.2f}]"
        + (f", early regime t>{early_threshold:.0f}s)" if early else ")"),
    )
