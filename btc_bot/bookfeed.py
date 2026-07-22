"""Real-time CLOB order books over WebSocket — millisecond price updates.

Instead of polling ``GET /book`` over HTTP every cycle (~150-400 ms per call,
two calls per tick), this module keeps a background thread subscribed to
Polymarket's public CLOB market channel
(``wss://ws-subscriptions-clob.polymarket.com/ws/market``). The server pushes
a full book snapshot on subscribe and incremental ``price_change`` events the
moment anything trades or quotes move, so the main loop reads the current best
bid/ask from memory — instantly.

The feed is optional: if ``websocket-client`` is missing, the connection
drops, or a token has no snapshot yet, callers get ``None`` and fall back to
the plain HTTP book fetch. Nothing breaks — it just gets slower.

Protocol (public, no auth):
  * connect and send ``{"assets_ids": ["<token1>", "<token2>"], "type": "market"}``
  * server replies with one ``book`` snapshot per asset (sometimes wrapped in
    a JSON array), then streams ``price_change`` / ``last_trade_price`` /
    ``tick_size_change`` events;
  * ``PING`` text keeps the connection alive (server answers ``PONG``).

When the 5-minute window rolls over the bot swaps in the new token ids via
:meth:`set_assets`; the thread reconnects with the new subscription (takes a
few hundred ms once per window — the HTTP fallback covers the gap).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Tuple

try:  # optional dependency — the bot still runs (on HTTP) without it
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None

# orjson (необязателен): разбор сообщений в ~5-10 раз быстрее stdlib.
try:
    import orjson
    _loads = orjson.loads
except Exception:  # noqa: BLE001
    _loads = json.loads


CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Connection considered dead if nothing (incl. PONGs) arrived for this long.
_CONN_STALE_SECONDS = 15.0
_PING_INTERVAL_SECONDS = 5.0


class OrderBook:
    """One token's book: price -> size maps. Pure logic, unit-testable."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_trade: Optional[float] = None
        self.ts = 0.0   # when the snapshot/last change was applied

    def apply_snapshot(self, bids, asks, now: Optional[float] = None) -> None:
        self.bids = {float(l["price"]): float(l["size"])
                     for l in (bids or []) if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"])
                     for l in (asks or []) if float(l["size"]) > 0}
        self.ts = now if now is not None else time.time()

    def apply_change(self, side: str, price, size,
                     now: Optional[float] = None) -> None:
        book = self.bids if str(side).upper() == "BUY" else self.asks
        p, s = float(price), float(size)
        if s <= 0:
            book.pop(p, None)
        else:
            book[p] = s
        self.ts = now if now is not None else time.time()

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None


class MarketBookFeed:
    """Background thread holding live order books for the current tokens."""

    def __init__(self, ws_url: str = CLOB_MARKET_WS, logger=None):
        self.ws_url = ws_url
        self.log = logger
        self.available = websocket is not None
        self._lock = threading.Lock()
        self._books: Dict[str, OrderBook] = {}
        self._assets: List[str] = []
        self._resub = threading.Event()   # asset list changed -> reconnect
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_rx = 0.0
        # Fires on every applied book update so the bot can react to the
        # market EVENT-DRIVEN (milliseconds) instead of on its poll tick.
        self.update_event = threading.Event()

    # -- public ---------------------------------------------------------------
    def start(self) -> None:
        if not self.available:
            if self.log:
                self.log.info(
                    "Order-book feed off (pip install websocket-client for "
                    "millisecond prices); using HTTP book fallback."
                )
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="clob-book-feed", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._resub.set()

    def set_assets(self, token_ids: List[str]) -> None:
        """Subscribe to a new set of tokens (called on window rollover)."""
        ids = [str(t) for t in token_ids if t]
        with self._lock:
            if ids == self._assets:
                return
            self._assets = ids
            self._books = {t: OrderBook() for t in ids}
        self._resub.set()   # the run loop reconnects with the new subscription

    def get_top(self, token_id: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
        """(best_bid, best_ask) from the live book, or None -> use HTTP."""
        if not self._healthy():
            return None
        with self._lock:
            book = self._books.get(str(token_id))
            if book is None or book.ts == 0.0:
                return None   # no snapshot received yet
            return book.best_bid(), book.best_ask()

    def get_last_trade(self, token_id: str) -> Optional[float]:
        if not self._healthy():
            return None
        with self._lock:
            book = self._books.get(str(token_id))
            return book.last_trade if book is not None else None

    def wait_update(self, timeout: float) -> bool:
        """Block until the next applied book update (or timeout). Lets the
        bot's decision loop run the instant the market moves."""
        fired = self.update_event.wait(timeout)
        if fired:
            self.update_event.clear()
        return fired

    # -- internals --------------------------------------------------------------
    def _healthy(self) -> bool:
        return (
            self.available
            and self._last_rx > 0.0
            and (time.time() - self._last_rx) < _CONN_STALE_SECONDS
        )

    def _ingest(self, raw: str) -> None:
        """Parse one WS message and update the books. Tolerant of variants."""
        if not raw:
            return
        self._last_rx = time.time()
        raw = raw.strip()
        if not raw.startswith(("{", "[")):
            return   # PONG / keepalive text
        try:
            data = _loads(raw)
        except Exception:  # noqa: BLE001
            return
        events = data if isinstance(data, list) else [data]
        with self._lock:
            for ev in events:
                if isinstance(ev, dict):
                    self._apply_event(ev)
        self.update_event.set()   # wake the decision loop right now

    def _apply_event(self, ev: dict) -> None:
        etype = ev.get("event_type") or ev.get("type")
        if etype == "book":
            book = self._books.get(str(ev.get("asset_id")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                    ev.get("asks") or ev.get("sells"))
        elif etype == "price_change":
            # Old shape: {asset_id, changes:[{price, side, size}]}
            # New shape: {price_changes:[{asset_id, price, side, size}]}
            for ch in ev.get("changes") or []:
                book = self._books.get(str(ev.get("asset_id")))
                if book is not None:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))
            for ch in ev.get("price_changes") or []:
                book = self._books.get(str(ch.get("asset_id")))
                if book is not None:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))
        elif etype == "last_trade_price":
            book = self._books.get(str(ev.get("asset_id")))
            if book is not None:
                try:
                    book.last_trade = float(ev.get("price"))
                except (TypeError, ValueError):
                    pass
        # tick_size_change and anything unknown: ignore

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            with self._lock:
                assets = list(self._assets)
            self._resub.clear()
            if not assets:
                self._resub.wait(1.0)   # nothing to subscribe to yet
                continue
            ws = None
            try:
                ws = websocket.create_connection(
                    self.ws_url, timeout=8,
                    header=["User-Agent: Mozilla/5.0"],
                    origin="https://polymarket.com",
                )
                ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                ws.settimeout(1.0)
                self._last_rx = time.time()
                backoff = 0.5
                last_ping = time.time()
                while not (self._stop.is_set() or self._resub.is_set()):
                    now = time.time()
                    if now - last_ping >= _PING_INTERVAL_SECONDS:
                        ws.send("PING")
                        last_ping = now
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    self._ingest(raw)
            except Exception as exc:  # noqa: BLE001 - keep retrying
                if self.log:
                    self.log.debug("book feed reconnect (%s)", exc)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._resub.is_set():
                continue   # reconnect immediately with the new asset list
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 10.0)
