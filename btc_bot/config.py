"""Configuration for the bot, loaded from environment variables / `.env`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

try:  # optional convenience — load a local .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _s(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class Config:
    # --- strategy -----------------------------------------------------------
    price_min: float = 0.84
    price_max: float = 0.99
    time_window_seconds: float = 70.0          # buy only when <= this remains
    # Only buy when BTC has moved at least this many USD away from the window's
    # open price (the "target"/strike), in the favourite's direction. 0 disables
    # the filter. A larger value avoids near-ties (the usual cause of losses).
    min_target_distance_usdc: float = 0.0
    trade_size_usdc: float = 5.0               # USDC spent per trade
    min_balance_usdc: float = 1.0              # stop if balance <= this
    poll_interval_seconds: float = 0.2
    run_duration_seconds: float = 86400.0      # default 24h
    price_source: str = "ask"                  # ask | mid | last
    trade_log_csv: str = "trades.csv"          # "" disables CSV trade logging

    # --- early/late split (time-based price rule) ----------------------------
    # With MORE than early_threshold_seconds left in the window, buy only at
    # early_price_min..price_max (default: only at 0.99) and only while the
    # outcome's price is rising. With early_threshold_seconds or less left,
    # the normal price_min..price_max band applies. 0 disables the split.
    early_threshold_seconds: float = 120.0
    early_price_min: float = 0.99
    early_require_rising: bool = True
    # "rising" = the price now is at/above where it was this many seconds ago
    # (it climbed, or is holding at the top; a falling price never qualifies).
    trend_lookback_seconds: float = 30.0

    # --- speed ---------------------------------------------------------------
    # Live order books over the CLOB WebSocket: bid/ask updates arrive in
    # milliseconds instead of polling GET /book over HTTP each tick. Needs
    # `websocket-client`; falls back to HTTP automatically when unavailable.
    book_feed_enabled: bool = True
    book_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    # How long a fetched balance stays fresh. The balance only changes when WE
    # trade (the bot refreshes it immediately after every order), so there is
    # no need to ask the exchange every tick.
    balance_refresh_seconds: float = 5.0
    # The per-tick status line is throttled to one line per this many seconds
    # (the loop itself still runs every poll_interval_seconds).
    status_log_interval_seconds: float = 1.0

    # --- hedge (optional opposite-side lottery bet) -------------------------
    # After the main buy fills, also place a tiny bet on the OPPOSITE outcome
    # at a very low price. If the favourite loses, the opposite side pays ~$1
    # per share, which can offset (or exceed) the main loss. A marketable limit
    # at hedge_price only fills if someone is selling that low.
    hedge_enabled: bool = False
    hedge_price: float = 0.01                  # limit price for the opposite bet
    hedge_size_usdc: float = 1.0               # USDC spent on the opposite bet

    # --- stop-loss (optional: sell an open position to cut losses) ----------
    # After the main buy fills, watch the position's current sell price (bid).
    # * SOFT: if it is at/below stoploss_soft_price (0.50) AND the window has
    #   stoploss_soft_secs_to_end (8s) or less left before it ends, sell to
    #   recover cash before it can expire worthless.
    # * HARD: if it falls to/below stoploss_hard_price (0.46), sell immediately,
    #   no matter how much time is left. Disabled unless stoploss_enabled.
    stoploss_enabled: bool = False
    stoploss_soft_price: float = 0.50
    stoploss_soft_secs_to_end: float = 8.0
    stoploss_hard_price: float = 0.46

    # --- market discovery ---------------------------------------------------
    asset: str = "btc"
    duration_label: str = "5m"
    window_seconds: int = 300

    # --- resilience ---------------------------------------------------------
    max_retries: int = 4
    backoff_base_seconds: float = 2.0
    max_backoff_seconds: float = 16.0
    max_consecutive_errors: int = 8
    http_timeout_seconds: float = 10.0

    # --- endpoints ----------------------------------------------------------
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    poly_host: str = "https://polymarket.com"   # exact target/strike feed
    # live Chainlink price (matches Polymarket's moving price); falls back to
    # spot if disabled or websocket-client isn't installed.
    live_price_enabled: bool = True
    live_price_ws: str = "wss://ws-live-data.polymarket.com"
    chain_id: int = 137
    btc_price_url: str = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

    # --- trading / auth -----------------------------------------------------
    dry_run: bool = True
    dry_run_balance: float = 100.0
    private_key: Optional[str] = None
    funder: Optional[str] = None
    signature_type: int = 1
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None

    @property
    def slug_prefix(self) -> str:
        """e.g. 'btc-updown-5m-' — the active market slug is this + window ts."""
        return f"{self.asset}-updown-{self.duration_label}-"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            price_min=_f("PRICE_MIN", 0.84),
            price_max=_f("PRICE_MAX", 0.99),
            time_window_seconds=_f("TIME_WINDOW_SECONDS", 70.0),
            min_target_distance_usdc=_f("MIN_TARGET_DISTANCE_USDC", 0.0),
            trade_size_usdc=_f("TRADE_SIZE_USDC", 5.0),
            min_balance_usdc=_f("MIN_BALANCE_USDC", 1.0),
            poll_interval_seconds=_f("POLL_INTERVAL_SECONDS", 0.2),
            run_duration_seconds=_f("RUN_DURATION_SECONDS", 86400.0),
            price_source=(_s("PRICE_SOURCE", "ask") or "ask").lower(),
            # unset -> default file; explicit empty string -> disabled
            trade_log_csv=(
                "trades.csv" if os.getenv("TRADE_LOG_CSV") is None
                else os.getenv("TRADE_LOG_CSV").strip()
            ),
            early_threshold_seconds=_f("EARLY_THRESHOLD_SECONDS", 120.0),
            early_price_min=_f("EARLY_PRICE_MIN", 0.99),
            early_require_rising=_b("EARLY_REQUIRE_RISING", True),
            trend_lookback_seconds=_f("TREND_LOOKBACK_SECONDS", 30.0),
            book_feed_enabled=_b("BOOK_FEED_ENABLED", True),
            book_ws=_s(
                "BOOK_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ),
            balance_refresh_seconds=_f("BALANCE_REFRESH_SECONDS", 5.0),
            status_log_interval_seconds=_f("STATUS_LOG_INTERVAL_SECONDS", 1.0),
            hedge_enabled=_b("HEDGE_ENABLED", False),
            hedge_price=_f("HEDGE_PRICE", 0.01),
            hedge_size_usdc=_f("HEDGE_SIZE_USDC", 1.0),
            stoploss_enabled=_b("STOPLOSS_ENABLED", False),
            stoploss_soft_price=_f("STOPLOSS_SOFT_PRICE", 0.50),
            stoploss_soft_secs_to_end=_f("STOPLOSS_SOFT_SECS_TO_END", 8.0),
            stoploss_hard_price=_f("STOPLOSS_HARD_PRICE", 0.46),
            asset=_s("ASSET", "btc"),
            duration_label=_s("DURATION_LABEL", "5m"),
            window_seconds=_i("WINDOW_SECONDS", 300),
            max_retries=_i("MAX_RETRIES", 4),
            backoff_base_seconds=_f("BACKOFF_BASE_SECONDS", 2.0),
            max_backoff_seconds=_f("MAX_BACKOFF_SECONDS", 16.0),
            max_consecutive_errors=_i("MAX_CONSECUTIVE_ERRORS", 8),
            http_timeout_seconds=_f("HTTP_TIMEOUT_SECONDS", 10.0),
            gamma_host=_s("GAMMA_HOST", "https://gamma-api.polymarket.com"),
            clob_host=_s("CLOB_HOST", "https://clob.polymarket.com"),
            poly_host=_s("POLY_HOST", "https://polymarket.com"),
            live_price_enabled=_b("LIVE_PRICE_ENABLED", True),
            live_price_ws=_s("LIVE_PRICE_WS", "wss://ws-live-data.polymarket.com"),
            chain_id=_i("CHAIN_ID", 137),
            btc_price_url=_s(
                "BTC_PRICE_URL", "https://api.coinbase.com/v2/prices/BTC-USD/spot"
            ),
            dry_run=_b("DRY_RUN", True),
            dry_run_balance=_f("DRY_RUN_BALANCE", 100.0),
            private_key=_s("PRIVATE_KEY"),
            funder=_s("FUNDER"),
            signature_type=_i("SIGNATURE_TYPE", 1),
            api_key=_s("CLOB_API_KEY"),
            api_secret=_s("CLOB_API_SECRET"),
            api_passphrase=_s("CLOB_API_PASSPHRASE"),
        )

    def validate(self) -> None:
        errors: List[str] = []
        if not (0 < self.price_min <= self.price_max <= 1):
            errors.append(
                f"price band invalid: need 0 < PRICE_MIN ({self.price_min}) "
                f"<= PRICE_MAX ({self.price_max}) <= 1"
            )
        if self.trade_size_usdc <= 0:
            errors.append("TRADE_SIZE_USDC must be > 0")
        if self.time_window_seconds <= 0:
            errors.append("TIME_WINDOW_SECONDS must be > 0")
        if self.min_target_distance_usdc < 0:
            errors.append("MIN_TARGET_DISTANCE_USDC must be >= 0")
        if self.early_threshold_seconds < 0:
            errors.append("EARLY_THRESHOLD_SECONDS must be >= 0 (0 disables)")
        if self.early_threshold_seconds > 0:
            if not (0 < self.early_price_min <= 1):
                errors.append("EARLY_PRICE_MIN must be in (0, 1]")
            if self.early_price_min > self.price_max:
                errors.append(
                    f"EARLY_PRICE_MIN ({self.early_price_min}) must be <= "
                    f"PRICE_MAX ({self.price_max}) or the early regime can "
                    "never buy"
                )
            if self.trend_lookback_seconds <= 0:
                errors.append("TREND_LOOKBACK_SECONDS must be > 0")
        if self.hedge_enabled:
            if not (0 < self.hedge_price < 1):
                errors.append("HEDGE_PRICE must be between 0 and 1 (e.g. 0.01)")
            if self.hedge_size_usdc <= 0:
                errors.append("HEDGE_SIZE_USDC must be > 0")
        if self.stoploss_enabled:
            if not (0 < self.stoploss_hard_price <= self.stoploss_soft_price < 1):
                errors.append(
                    "stop-loss prices invalid: need 0 < STOPLOSS_HARD_PRICE "
                    f"({self.stoploss_hard_price}) <= STOPLOSS_SOFT_PRICE "
                    f"({self.stoploss_soft_price}) < 1"
                )
            if self.stoploss_soft_secs_to_end < 0:
                errors.append("STOPLOSS_SOFT_SECS_TO_END must be >= 0")
        if self.poll_interval_seconds <= 0:
            errors.append("POLL_INTERVAL_SECONDS must be > 0")
        if self.balance_refresh_seconds < 0:
            errors.append("BALANCE_REFRESH_SECONDS must be >= 0")
        if self.status_log_interval_seconds < 0:
            errors.append("STATUS_LOG_INTERVAL_SECONDS must be >= 0")
        if self.price_source not in ("ask", "mid", "last"):
            errors.append("PRICE_SOURCE must be one of: ask, mid, last")
        if not self.dry_run and not self.private_key:
            errors.append(
                "live trading (DRY_RUN=false) requires PRIVATE_KEY. "
                "Set DRY_RUN=true to simulate without credentials."
            )
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))
