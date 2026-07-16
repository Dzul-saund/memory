"""Live Chainlink price feed — the same number Polymarket shows and resolves on.

Polymarket's 5m BTC Up/Down markets move off the **Chainlink BTC/USD** stream,
broadcast over its live-data WebSocket (``wss://ws-live-data.polymarket.com``).
The bot's old Coinbase spot poll lagged and (worse) carried a basis vs Chainlink,
so the displayed price never matched the site.

This module keeps a background thread connected to that WebSocket and exposes the
latest Chainlink price. The main loop reads a cached value (instant, never frozen
by per-tick HTTP latency). If ``websocket-client`` isn't installed, or the feed
can't connect, the caller falls back to spot — so this is always optional.

Protocol (reverse-engineered from the site, re-verified live):
  * subscribe: ``{"action":"subscribe","subscriptions":[
      {"topic":"crypto_prices_chainlink","type":"update",
       "filters":"{\\"symbol\\":\\"btc/usd\\"}"}]}``
  * the server replies with ONE snapshot ``{"payload":{"data":[{timestamp,value}...]}}``
    whose last point is the current price — and then sends NOTHING further
    (no streaming), so the only way to stay current is to re-subscribe.
  * Chainlink publishes ~1 point per second, and the newest point is already
    ~0.9s old when the server hands it out (Polymarket's own pipeline delay —
    the site is subject to it too).

Hence the feed re-subscribes every ``refresh_seconds`` (default 0.25s; the
server comfortably tolerates 4 snapshot requests/s) so each new Chainlink
point is picked up within ~a quarter second of appearing — the bot sees the
price no later than the site does. Points carry their own timestamps; an
out-of-order/older point never overwrites a newer one.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

try:  # optional dependency — the bot still runs (on spot) without it
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None

LIVE_DATA_WS = "wss://ws-live-data.polymarket.com"


class ChainlinkPriceFeed:
    """Background thread holding the latest Chainlink price for one symbol."""

    def __init__(
        self,
        symbol: str = "btc/usd",
        ws_url: str = LIVE_DATA_WS,
        refresh_seconds: float = 0.25,
        logger=None,
    ):
        self.symbol = symbol.lower()
        self.ws_url = ws_url
        self.refresh = max(0.2, refresh_seconds)   # be nice to the server
        self.log = logger
        self.available = websocket is not None
        self._lock = threading.Lock()
        self._value: Optional[float] = None
        self._ts = 0.0            # when we RECEIVED the value (staleness check)
        self._point_ts = 0.0      # the point's OWN timestamp (ordering guard)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public -------------------------------------------------------------
    def start(self) -> None:
        if not self.available:
            if self.log:
                self.log.info(
                    "Live price feed off (pip install websocket-client to match "
                    "Polymarket's live price); using spot fallback."
                )
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="chainlink-price-feed", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get(self, max_age: float = 8.0) -> Optional[float]:
        """Latest price, or None if missing/stale (caller falls back to spot)."""
        with self._lock:
            if self._value is None or (time.time() - self._ts) > max_age:
                return None
            return self._value

    # -- internals ----------------------------------------------------------
    def _sub_msg(self) -> str:
        return json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "filters": json.dumps({"symbol": self.symbol}),
            }],
        })

    def _ingest(self, raw: str) -> None:
        if not raw or not raw.startswith("{"):
            return
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        payload = d.get("payload") or {}
        value = None
        point_ts = None
        data = payload.get("data")
        if isinstance(data, list) and data:
            value = data[-1].get("value")
            point_ts = data[-1].get("timestamp")
        elif isinstance(payload.get("value"), (int, float)):
            value = payload["value"]
            point_ts = payload.get("timestamp")
        if not isinstance(value, (int, float)):
            return
        try:
            point_ts = float(point_ts) if point_ts is not None else None
        except (TypeError, ValueError):
            point_ts = None
        with self._lock:
            # Never let an older point overwrite a newer one (snapshots can
            # arrive out of order when polling several times per second).
            if point_ts is not None:
                if point_ts < self._point_ts:
                    return
                self._point_ts = point_ts
            self._value = float(value)
            self._ts = time.time()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    self.ws_url, timeout=8,
                    header=["User-Agent: Mozilla/5.0"],
                    origin="https://polymarket.com",
                )
                ws.send(self._sub_msg())
                ws.settimeout(0.2)   # short recv timeout -> resubscribe on time
                backoff = 1.0
                last_sub = last_ping = time.time()
                while not self._stop.is_set():
                    now = time.time()
                    # re-subscribe to refresh the snapshot (streaming is gated)
                    if now - last_sub >= self.refresh:
                        ws.send(self._sub_msg())
                        last_sub = now
                    if now - last_ping >= 30:
                        ws.send("PING")
                        last_ping = now
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    self._ingest(raw)
            except Exception as exc:  # noqa: BLE001 - keep retrying
                if self.log:
                    self.log.debug("price feed reconnect (%s)", exc)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 15.0)
