#!/usr/bin/env python3
"""
PM View — зеркало Polymarket: 5-минутный рынок как на сайте, без торговли.

Показывает ровно то, что видно на странице Polymarket, и берёт это из ТЕХ ЖЕ
источников, что использует сам сайт (поэтому «абсолютная точность»):

  1. ЦЕНА монеты — поток Chainlink с ws-live-data.polymarket.com. Это та же
     цифра, что рисует график сайта и по которой раунд закрывается.
  2. ЦЕЛЬ раунда (Целевая цена) — openPrice из API сайта
     /api/crypto/crypto-price. Точно как в шапке рынка.
  3. UP / DOWN в центах — из CLOB WebSocket, того же push-потока, из
     которого сайт рисует книгу заявок: цена покупки (ask — столько стоит
     купить, как в кнопках «Up 19¢ / Down 82¢»), цена продажи (bid),
     спред и последняя сделка.

Скорость: всё push-потоками, без опроса; цена Chainlink забирается 4 раза/с
(сайт делает ~1 раз/с), книга — каждое изменение по мере события. То есть
экран обновляется НЕ МЕДЛЕННЕЕ сайта, обычно раньше.

Никакой торговли, никаких ставок, никакой симуляции — только просмотр.

Требуется: websockets (orjson по желанию).
Запуск:
    python pm_view.py --coin btc
    python pm_view.py --coin doge --depth 3
(или двойной клик — спросит монету)
"""

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime


def _fatal(msg: str):
    print("\n" + "=" * 64)
    print("PM VIEW НЕ ЗАПУСТИЛСЯ:")
    print(msg)
    print("=" * 64)
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажми Enter, чтобы закрыть окно...")
    except Exception:
        pass
    raise SystemExit(1)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import book_monitor as bm      # Book, discover_market, CLOB_WS
except Exception as e:             # noqa: BLE001
    _fatal("не найден 'book_monitor.py' — положи pm_view.py в ту же папку.\n"
           f"Причина: {e}")

try:
    import websockets
except ImportError:
    _fatal("не установлена библиотека websockets.\n"
           "Выполни в этой папке:  pip install websockets")

try:
    import orjson
    _loads = orjson.loads
except Exception:  # noqa: BLE001
    _loads = json.loads

PM_WS = "wss://ws-live-data.polymarket.com"
COINS = {"btc": ("BTC", 2), "eth": ("ETH", 2), "sol": ("SOL", 3),
         "xrp": ("XRP", 5), "doge": ("DOGE", 6)}
WINDOW = 300


def ts_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------- печать без блокировок ----------------
_Q: "queue.Queue[str]" = queue.Queue()
_started = False


def _worker():
    while True:
        line = _Q.get()
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def out(line: str) -> None:
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_worker, daemon=True).start()
    _Q.put(line)


# ---------------- состояние (то, что показывает сайт) ----------------
STATE = {
    "price": None,        # цена монеты (Chainlink, как на графике сайта)
    "price_ts": 0.0,
    "target": None,       # Целевая цена раунда (openPrice)
    "target_start": None,
    "up": None,           # Book UP
    "down": None,         # Book DOWN
    "last_up": None,      # последняя сделка UP
    "last_down": None,
}


def secs_left(now=None) -> float:
    t = now if now is not None else time.time()
    return WINDOW - (t % WINDOW)


def window_start(now=None) -> int:
    t = int(now if now is not None else time.time())
    return t - t % WINDOW


# ---------------- 1. цена Chainlink (как на сайте) ----------------

async def price_feed(coin_name: str):
    """Тот же поток, из которого сайт берёт движущуюся цену."""
    sub = json.dumps({"action": "subscribe", "subscriptions": [{
        "topic": "crypto_prices_chainlink", "type": "update",
        "filters": json.dumps({"symbol": f"{coin_name.lower()}/usd"})}]})
    last_pt = 0.0
    said = False
    while True:
        try:
            async with websockets.connect(
                    PM_WS, compression=None, open_timeout=8,
                    user_agent_header="Mozilla/5.0",
                    origin="https://polymarket.com") as ws:
                out(f"[polymarket] цена подключена (Chainlink {coin_name}/USD "
                    f"— как на графике сайта)")
                said = False
                last_sub = 0.0
                while True:
                    now = time.time()
                    if now - last_sub >= 0.25:      # сайт делает ~1/с
                        await ws.send(sub)
                        last_sub = now
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                    if not isinstance(raw, str) or not raw.startswith("{"):
                        continue
                    try:
                        d = _loads(raw)
                    except Exception:
                        continue
                    payload = d.get("payload") or {}
                    data = payload.get("data")
                    val = pt = None
                    if isinstance(data, list) and data:
                        val, pt = data[-1].get("value"), data[-1].get("timestamp")
                    elif isinstance(payload.get("value"), (int, float)):
                        val, pt = payload["value"], payload.get("timestamp")
                    if not isinstance(val, (int, float)):
                        continue
                    try:
                        pt = float(pt) if pt is not None else None
                    except (TypeError, ValueError):
                        pt = None
                    if pt is not None:
                        if pt < last_pt:
                            continue        # снимок старее принятого
                        last_pt = pt
                    STATE["price"] = float(val)
                    STATE["price_ts"] = time.time()
        except Exception as e:
            if not said:
                out(f"[polymarket] цена: обрыв ({e}); переподключаюсь")
                said = True
            await asyncio.sleep(1.0)


# ---------------- 2. цель раунда (Целевая цена) ----------------

def _fetch_target(coin_name: str, start_ts: int):
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ts))
    url = (f"https://polymarket.com/api/crypto/crypto-price"
           f"?symbol={coin_name}&eventStartTime={iso}&variant=fiveminute")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=4) as r:
        d = json.loads(r.read())
    op = d.get("openPrice")
    return float(op) if op is not None else None


async def target_feed(coin_name: str, dp: int):
    while True:
        start = window_start()
        if STATE["target_start"] == start and STATE["target"] is not None:
            await asyncio.sleep(min(2.0, secs_left() + 0.2))
            continue
        v = None
        try:
            v = await asyncio.to_thread(_fetch_target, coin_name, start)
        except Exception:
            pass
        if v is not None and window_start() == start:
            STATE["target"] = v
            STATE["target_start"] = start
            out(f"--- Целевая цена раунда: {v:,.{dp}f} (как на сайте) ---")
        else:
            await asyncio.sleep(1.5)


# ---------------- 3. книга UP/DOWN (как на сайте) ----------------

async def book_feed(coin: str, connections: int):
    while True:
        market = await bm.discover_market(coin)
        up_t, dn_t = market["up"], market["down"]
        books = {up_t: bm.Book(), dn_t: bm.Book()}
        STATE["up"], STATE["down"] = books[up_t], books[dn_t]
        STATE["last_up"] = STATE["last_down"] = None
        end_ts = market["window_ts"] + WINDOW
        out(f"── {market['question']} | до "
            f"{datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')} ──")
        sub = json.dumps({"assets_ids": [up_t, dn_t], "type": "market"})
        seen, seen_q = set(), deque(maxlen=8192)

        def apply(ev):
            etype = ev.get("event_type") or ev.get("type")
            if etype == "book":
                b = books.get(str(ev.get("asset_id")))
                if b is not None:
                    b.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                     ev.get("asks") or ev.get("sells"))
            elif etype == "price_change":
                for ch in ev.get("changes") or []:
                    b = books.get(str(ev.get("asset_id")))
                    if b is not None and b.have_snapshot:
                        b.apply_change(ch.get("side"), ch.get("price"),
                                       ch.get("size"))
                for ch in ev.get("price_changes") or []:
                    b = books.get(str(ch.get("asset_id")))
                    if b is not None and b.have_snapshot:
                        b.apply_change(ch.get("side"), ch.get("price"),
                                       ch.get("size"))
            elif etype == "last_trade_price":
                tok = str(ev.get("asset_id"))
                try:
                    px = float(ev.get("price"))
                except (TypeError, ValueError):
                    return
                if tok == up_t:
                    STATE["last_up"] = px
                elif tok == dn_t:
                    STATE["last_down"] = px

        async def conn():
            backoff = 0.5
            while time.time() < end_ts + 2:
                try:
                    async with websockets.connect(
                            bm.CLOB_WS, compression=None, open_timeout=8,
                            user_agent_header="Mozilla/5.0") as ws:
                        await ws.send(sub)

                        async def _p():
                            while True:
                                await asyncio.sleep(10)
                                await ws.send("PING")

                        async def _c():
                            await asyncio.sleep(max(0.0, end_ts + 2 - time.time()))
                            await ws.close()
                        aux = (asyncio.create_task(_p()),
                               asyncio.create_task(_c()))
                        try:
                            async for raw in ws:
                                if not isinstance(raw, str) \
                                        or not raw.startswith(("{", "[")):
                                    continue
                                h = hash(raw)
                                if h in seen:
                                    continue
                                if len(seen_q) == seen_q.maxlen:
                                    seen.discard(seen_q[0])
                                seen.add(h)
                                seen_q.append(h)
                                try:
                                    data = _loads(raw)
                                except Exception:
                                    continue
                                for ev in (data if isinstance(data, list)
                                           else [data]):
                                    if isinstance(ev, dict):
                                        apply(ev)
                        finally:
                            for t in aux:
                                t.cancel()
                except Exception:
                    if time.time() >= end_ts:
                        break
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)

        await asyncio.gather(*(conn() for _ in range(connections)))


# ---------------- экран (как страница рынка) ----------------

def _cents(x):
    return "—" if x is None else f"{x * 100:.0f}¢"


def _side_line(name, book, last, depth, width=5):
    if book is None or not book.have_snapshot:
        return f"{name:4} —"
    bid, ask = book.best_bid(), book.best_ask()
    bsz = book.bids.get(bid, 0) if bid is not None else 0
    asz = book.asks.get(ask, 0) if ask is not None else 0
    spread = (ask - bid) * 100 if (bid is not None and ask is not None) else None
    def _sz(v):
        return f"{v:,.1f}" if 0 < v < 10 else f"{v:,.0f}"
    s = (f"{name:4} купить {_cents(ask):>4} (×{_sz(asz)})   "
         f"продать {_cents(bid):>4} (×{_sz(bsz)})")
    if spread is not None:
        s += f"   спред {spread:.0f}¢"
    if last is not None:
        s += f"   посл. {_cents(last)}"
    if depth > 1:
        lv = " ".join(f"{p * 100:.0f}¢×{q:,.0f}"
                      for p, q in book.top("ask", depth))
        s += f"\n         стакан продажи: {lv}"
    return s


async def screen(args, coin_name, dp):
    await asyncio.sleep(1.5)
    prev_price = None
    while True:
        await asyncio.sleep(args.interval)
        p = STATE["price"]
        if p is None:
            out(f"{ts_str()}  жду цену с Polymarket...")
            continue
        arrow = " "
        if prev_price is not None:
            arrow = "^" if p > prev_price else ("v" if p < prev_price else "=")
        prev_price = p
        left = secs_left()
        tgt = (STATE["target"]
               if STATE["target_start"] == window_start() else None)
        head = f"{ts_str()}  {coin_name} {p:,.{dp}f}{arrow}"
        if tgt is not None:
            diff = p - tgt
            head += (f"  | цель {tgt:,.{dp}f}  diff {diff:+.{dp}f}"
                     f"  ({'выше' if diff >= 0 else 'ниже'})")
        head += f"  | до конца {left:5.1f}с"
        out(head)
        out("   " + _side_line("UP", STATE["up"], STATE["last_up"], args.depth))
        out("   " + _side_line("DOWN", STATE["down"], STATE["last_down"],
                               args.depth))


async def main_async(args):
    coin_name, dp = COINS[args.coin]
    out(f"=== PM View — {coin_name} Up/Down 5m (зеркало Polymarket, "
        f"БЕЗ торговли) ===")
    out("источники: цена — Chainlink с ws-live-data.polymarket.com; "
        "цель — openPrice сайта; UP/DOWN — CLOB WebSocket (книга сайта)")
    await asyncio.gather(
        price_feed(coin_name),
        target_feed(coin_name, dp),
        book_feed(args.coin, args.connections),
        screen(args, coin_name, dp),
    )


def main():
    ap = argparse.ArgumentParser(
        description="PM View: 5-минутный рынок Polymarket как на сайте, "
                    "без торговли")
    ap.add_argument("--coin", choices=sorted(COINS), default="btc")
    ap.add_argument("--interval", type=float, default=0.25,
                    help="как часто перерисовывать экран, сек (по умолч. 0.25 "
                         "= 4 раза/с; данные приходят быстрее)")
    ap.add_argument("--depth", type=int, default=1,
                    help="сколько уровней стакана показывать (по умолч. 1)")
    ap.add_argument("--connections", type=int, default=2, choices=(1, 2, 3))
    args = ap.parse_args()

    if "--coin" not in sys.argv[1:] and sys.stdin is not None \
            and sys.stdin.isatty():
        order = list(COINS)
        print("Какой рынок смотреть (как на Polymarket)?")
        for i, c in enumerate(order, 1):
            print(f"  {i}. {COINS[c][0]}")
        try:
            s = input("Номер (1-5) или имя: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nвыход")
        if s.isdigit() and 1 <= int(s) <= len(order):
            args.coin = order[int(s) - 1]
        elif s in COINS:
            args.coin = s

    import gc
    gc.collect()
    try:
        gc.freeze()
    except Exception:
        pass
    gc.set_threshold(50000, 100, 100)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено")
    except SystemExit as e:
        if e.code not in (0, None):
            print(e)
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("\nНажми Enter, чтобы закрыть окно...")
            except (EOFError, KeyboardInterrupt):
                pass
