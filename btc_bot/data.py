"""Read-only market data: active market discovery, prices, and BTC spot.

None of this needs credentials, so the bot can watch markets (and run in
dry-run) with nothing but `requests` installed. Every network call goes
through :func:`util.with_retry` for transient-error resilience.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

from .config import Config
from .pricefeed import ChainlinkPriceFeed
from .util import iso_to_epoch, with_retry


@dataclass
class Market:
    condition_id: str
    slug: str
    question: str
    window_start_ts: int
    end_ts: int
    token_up: str
    token_down: str
    order_min_size: float
    tick_size: float
    gamma_up_price: Optional[float]
    gamma_down_price: Optional[float]

    def seconds_to_end(self, now: Optional[float] = None) -> float:
        return max(0.0, self.end_ts - (now if now is not None else time.time()))


class MarketData:
    def __init__(self, cfg: Config, logger):
        self.cfg = cfg
        self.log = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "btc-updown-5m-bot/1.0"})
        # Live Chainlink price feed (created, not started — start_feed() begins
        # the background thread; left unstarted in unit tests, which never call
        # the run loop, so nothing touches the network there).
        self.feed = (
            ChainlinkPriceFeed(
                symbol=f"{cfg.asset.lower()}/usd",
                ws_url=cfg.live_price_ws,
                logger=logger,
            )
            if cfg.live_price_enabled
            else None
        )

    def start_feed(self) -> None:
        if self.feed is not None:
            self.feed.start()

    def stop_feed(self) -> None:
        if self.feed is not None:
            self.feed.stop()

    # -- low level ----------------------------------------------------------
    def _get_json(self, url: str, params: Optional[dict] = None):
        def _do():
            r = self.session.get(
                url, params=params, timeout=self.cfg.http_timeout_seconds
            )
            r.raise_for_status()
            return r.json()

        return with_retry(
            _do,
            retries=self.cfg.max_retries,
            base=self.cfg.backoff_base_seconds,
            max_backoff=self.cfg.max_backoff_seconds,
            what=f"GET {url}",
            logger=self.log,
        )

    # -- market discovery ---------------------------------------------------
    def current_window_ts(self, now: Optional[int] = None) -> int:
        now = int(now if now is not None else time.time())
        return now - (now % self.cfg.window_seconds)

    def get_active_market(self) -> Optional[Market]:
        """Return the market for the *current* 5-minute window, or None.

        The slug is deterministic: ``<asset>-updown-<dur>-<window_start_ts>``
        where ``window_start_ts = now - (now % 300)``. Right at a window
        rollover the next market can take a second to appear; we return None in
        that case and the caller simply waits and tries again.
        """
        slug = f"{self.cfg.slug_prefix}{self.current_window_ts()}"
        data = self._get_json(f"{self.cfg.gamma_host}/markets", params={"slug": slug})
        if not data:
            return None
        m = data[0]
        if m.get("closed") is True:
            return None
        return self._parse_market(m)

    def _parse_market(self, m: dict) -> Market:
        outcomes = _loads_list(m.get("outcomes"))           # ["Up", "Down"]
        token_ids = _loads_list(m.get("clobTokenIds"))      # [up_id, down_id]
        prices = _loads_list(m.get("outcomePrices"))        # ["0.2", "0.8"]

        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        down_idx = outcomes.index("Down") if "Down" in outcomes else 1

        slug = m.get("slug", "")
        window_start = _slug_ts(slug)
        end_ts = (
            iso_to_epoch(m["endDate"])
            if m.get("endDate")
            else window_start + self.cfg.window_seconds
        )

        return Market(
            condition_id=m.get("conditionId", slug),
            slug=slug,
            question=m.get("question", ""),
            window_start_ts=window_start,
            end_ts=end_ts,
            token_up=str(token_ids[up_idx]),
            token_down=str(token_ids[down_idx]),
            order_min_size=float(m.get("orderMinSize", 5) or 5),
            tick_size=float(m.get("orderPriceMinTickSize", 0.01) or 0.01),
            gamma_up_price=_to_float(prices[up_idx]) if prices else None,
            gamma_down_price=_to_float(prices[down_idx]) if prices else None,
        )

    # -- CLOB prices --------------------------------------------------------
    def get_book_top(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Return (best_bid, best_ask) for a token from the order book."""
        book = self._get_json(
            f"{self.cfg.clob_host}/book", params={"token_id": token_id}
        )
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        return best_bid, best_ask

    def get_midpoint(self, token_id: str) -> Optional[float]:
        d = self._get_json(
            f"{self.cfg.clob_host}/midpoint", params={"token_id": token_id}
        )
        mid = d.get("mid") if isinstance(d, dict) else None
        return float(mid) if mid is not None else None

    def get_last_price(self, token_id: str) -> Optional[float]:
        d = self._get_json(
            f"{self.cfg.clob_host}/last-trade-price", params={"token_id": token_id}
        )
        p = d.get("price") if isinstance(d, dict) else None
        return float(p) if p is not None else None

    # -- exact target / strike ---------------------------------------------
    def get_target_price(self, market: "Market") -> Optional[float]:
        """The EXACT strike (window open price) straight from Polymarket.

        Polymarket resolves these markets off the Chainlink BTC/USD stream and
        publishes the open price at
        ``/api/crypto/crypto-price?symbol=BTC&eventStartTime=<openISO>&variant=fiveminute``.
        This is the number shown on the site as the target ("Целевая цена"), so
        it matches 1:1 — unlike the Coinbase spot approximation. The value is
        available as soon as the window opens (``closePrice`` stays null until
        the window completes, so it is not usable as a live price). Returns None
        on any error or unsupported duration, and the caller falls back.
        """
        variant = _VARIANT.get(self.cfg.duration_label)
        if variant is None:
            return None
        event_start = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(market.window_start_ts)
        )
        params = {
            "symbol": self.cfg.asset.upper(),
            "eventStartTime": event_start,
            "variant": variant,
        }
        # Single attempt (non-critical): never stall the loop on backoff.
        try:
            r = self.session.get(
                f"{self.cfg.poly_host}/api/crypto/crypto-price",
                params=params,
                timeout=self.cfg.http_timeout_seconds,
            )
            r.raise_for_status()
            d = r.json()
        except Exception as exc:  # noqa: BLE001 - target is non-critical
            self.log.debug("exact target fetch failed: %s", exc)
            return None
        if isinstance(d, dict) and d.get("openPrice") is not None:
            return _to_float(d["openPrice"])
        return None

    # -- live price ---------------------------------------------------------
    def get_live_price(self) -> Optional[float]:
        """Live price for display + the distance filter.

        Prefers the Chainlink feed (the exact number Polymarket shows and
        resolves on, refreshed ~1×/s by a background thread). Falls back to
        spot (Coinbase) when the feed is off, not yet warm, or stale.
        """
        if self.feed is not None:
            v = self.feed.get()
            if v is not None:
                return v
        return self.get_btc_price()

    # -- BTC spot -----------------------------------------------------------
    def get_btc_price(self) -> Optional[float]:
        """Current BTC price from the configured source (Coinbase by default).

        Used for display/logging (target price + live price). The market itself
        resolves off the Chainlink BTC/USD stream; this spot value tracks it
        closely but is not the exact oracle value.
        """
        try:
            d = self._get_json(self.cfg.btc_price_url)
        except Exception as exc:  # noqa: BLE001 - BTC price is non-critical
            self.log.debug("BTC price fetch failed: %s", exc)
            return None
        # Coinbase: {"data": {"amount": "63000.00"}}
        if isinstance(d, dict):
            if isinstance(d.get("data"), dict) and "amount" in d["data"]:
                return _to_float(d["data"]["amount"])
            for key in ("price", "amount", "USD", "usd"):
                if key in d:
                    return _to_float(d[key])
        return None


# Polymarket "variant" names for the crypto-price feed, keyed by duration label.
_VARIANT = {"5m": "fiveminute", "15m": "fifteen", "4h": "fourhour", "1h": "hourly"}


def _loads_list(v):
    """Gamma returns some fields as JSON-encoded strings; tolerate both."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return []


def _slug_ts(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
