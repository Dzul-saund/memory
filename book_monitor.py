#!/usr/bin/env python3
"""
Polymarket Book Monitor v1 — книга заявок Up/Down с минимальной задержкой.

ВЫБОР ИСТОЧНИКА (почему именно этот).
У Polymarket есть три способа читать стакан:

  1. REST CLOB (`GET https://clob.polymarket.com/book?token_id=...`) —
     опрос: каждый запрос это TLS-соединение/HTTP-раунд ~150-400мс, между
     запросами стакан слепой. Для HFT не годится.
  2. Сайт (Selenium/Playwright) — тот же WebSocket, что и п.3, но поверх
     браузера: +рендер, +JS, сотни мс лишних и хрупко. Не годится.
  3. CLOB Market WebSocket  wss://ws-subscriptions-clob.polymarket.com/ws/market
     — ИМЕННО ЕГО использует сам сайт для отрисовки «Книги заявок».
     Push-модель: при подписке сервер шлёт полный снимок книги (`book`),
     дальше — КАЖДОЕ появление/изменение/удаление уровня (`price_change`,
     size=0 означает удаление уровня) и каждую сделку (`last_trade_price`),
     в момент события, без какого-либо опроса. Быстрее физически не бывает:
     любой другой путь — производная от этого же потока.

Этот бот подключается к п.3 и:
  * отслеживает ОБЕ стороны рынка (токены Up и Down) текущего 5-минутного
    окна выбранной монеты (btc/eth/sol/xrp/doge);
  * держит ЛОКАЛЬНУЮ копию обеих книг и обновляет её ИНКРЕМЕНТАЛЬНО
    (снимок один раз, дальше только дельты);
  * для каждого события знает цену, объём, сторону (bid/ask, BUY/SELL)
    и серверное время события;
  * печатает ЗАДЕРЖКУ каждого события (recv_time − server_timestamp) и
    раз в ~10с сводку: медиана / p95 / максимум, сообщений в секунду;
  * событийный вывод: строка печатается В МОМЕНТ изменения топа книги или
    сделки (не по таймеру); печать вынесена в отдельный поток, чтобы
    медленная консоль Windows не тормозила приём (см. fast_monitor v8);
  * асинхронная архитектура (asyncio + websockets), сжатие WebSocket
    отключено (permessage-deflate добавляет распаковку и пакетирование);
  * автоматически переподключается при обрыве (пауза 0.5с -> 10с) и сам
    перекатывается на следующее 5-минутное окно (новые токены);
  * оптимизации: книги на dict[price]->size с кэшем лучших цен, разбор
    только нужных полей, ноль опросов, ноль лишних аллокаций в горячем
    цикле.

Требуется Python 3.10+ и websockets:
    pip install websockets
Запуск:
    python book_monitor.py --coin btc
    python book_monitor.py --coin doge --depth 3
(или двойной клик — спросит монету)
"""

import argparse
import asyncio
import json
import queue
import statistics
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime

try:
    import websockets
except ImportError:
    raise SystemExit("Установите зависимость:  pip install websockets")

GAMMA = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WINDOW_SECONDS = 300

COINS = {
    "btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP", "doge": "DOGE",
}


def now_ms() -> float:
    return time.time() * 1000


def ts_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ------------------------- печать без блокировок -------------------------

_PRINT_Q: "queue.Queue[str]" = queue.Queue()
_printer_started = False


def _print_worker():
    while True:
        line = _PRINT_Q.get()
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def out(line: str) -> None:
    global _printer_started
    if not _printer_started:
        _printer_started = True
        threading.Thread(target=_print_worker, name="printer",
                         daemon=True).start()
    _PRINT_Q.put(line)


# ------------------------- локальная книга -------------------------

class Book:
    """Одна сторона рынка (один токен): bids/asks c кэшем лучших цен."""

    __slots__ = ("bids", "asks", "_bb", "_ba", "have_snapshot", "last_trade",
                 "last_trade_side", "last_trade_size", "last_top_print")

    def __init__(self):
        self.bids: dict = {}
        self.asks: dict = {}
        self._bb = None          # кэш best bid
        self._ba = None          # кэш best ask
        self.have_snapshot = False
        self.last_trade = None
        self.last_trade_side = ""
        self.last_trade_size = None
        self.last_top_print = 0.0

    def apply_snapshot(self, bids, asks):
        self.bids = {float(l["price"]): float(l["size"])
                     for l in (bids or []) if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"])
                     for l in (asks or []) if float(l["size"]) > 0}
        self._bb = max(self.bids) if self.bids else None
        self._ba = min(self.asks) if self.asks else None
        self.have_snapshot = True

    def apply_change(self, side: str, price, size) -> bool:
        """Одно изменение уровня. size=0 -> уровень удалён.
        Возвращает True, если изменился ТОП книги (лучшая цена/объём)."""
        p, s = float(price), float(size)
        if str(side).upper() == "BUY":
            book, best = self.bids, self._bb
            if s <= 0:
                if book.pop(p, None) is None:
                    return False
                if p == best:                      # сняли лучший бид
                    self._bb = max(book) if book else None
                    return True
                return False
            was = book.get(p)
            book[p] = s
            if best is None or p > best:           # новый лучший бид
                self._bb = p
                return True
            return p == best and was != s          # объём на топе
        else:
            book, best = self.asks, self._ba
            if s <= 0:
                if book.pop(p, None) is None:
                    return False
                if p == best:
                    self._ba = min(book) if book else None
                    return True
                return False
            was = book.get(p)
            book[p] = s
            if best is None or p < best:
                self._ba = p
                return True
            return p == best and was != s

    def best_bid(self):
        return self._bb

    def best_ask(self):
        return self._ba

    def top(self, side: str, depth: int):
        """Топ-N уровней: [(цена, объём), ...]"""
        if side == "bid":
            return sorted(self.bids.items(), reverse=True)[:depth]
        return sorted(self.asks.items())[:depth]


# ------------------------- рынок текущего окна -------------------------

def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _loads_list(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def window_ts(now=None) -> int:
    t = int(now if now is not None else time.time())
    return t - t % WINDOW_SECONDS


async def discover_market(asset: str):
    """Токены Up/Down текущего 5-минутного окна (Gamma, 1 запрос за окно)."""
    while True:
        slug = f"{asset}-updown-5m-{window_ts()}"
        try:
            data = await asyncio.to_thread(
                _http_json, f"{GAMMA}/markets?slug={slug}")
        except Exception as e:
            out(f"[market] Gamma недоступна ({e}); повтор через 2с")
            await asyncio.sleep(2)
            continue
        if data:
            m = data[0]
            outcomes = _loads_list(m.get("outcomes"))
            tokens = _loads_list(m.get("clobTokenIds"))
            if len(tokens) >= 2:
                iu = outcomes.index("Up") if "Up" in outcomes else 0
                idn = outcomes.index("Down") if "Down" in outcomes else 1
                return {"slug": slug, "window_ts": window_ts(),
                        "up": str(tokens[iu]), "down": str(tokens[idn]),
                        "question": m.get("question", slug)}
        # окно только что открылось — рынок появляется через секунду-другую
        await asyncio.sleep(0.5)


# ------------------------- поток книги -------------------------

class LatencyStats:
    def __init__(self):
        self.samples = deque(maxlen=1000)
        self.events = 0
        self.msgs = 0

    def add(self, lat_ms: float):
        self.samples.append(lat_ms)

    def line(self, span_s: float) -> str:
        if not self.samples:
            return "лаг: нет данных"
        xs = sorted(self.samples)
        med = xs[len(xs) // 2]
        p95 = xs[int(len(xs) * 0.95) - 1] if len(xs) >= 20 else xs[-1]
        return (f"сводка за {span_s:.0f}с: {self.msgs / span_s:.1f} сообщ/с, "
                f"{self.events} событий | лаг мс: медиана {med:.0f}, "
                f"p95 {p95:.0f}, макс {xs[-1]:.0f}, мин {xs[0]:.0f}")


def fmt_top(book: Book, depth: int, dp: int = 2):
    def lv(levels):
        return " ".join(f"{p:.{dp}f}×{s:.0f}" for p, s in levels) or "—"
    if depth <= 1:
        bb, ba = book.best_bid(), book.best_ask()
        b = f"{bb:.{dp}f}×{book.bids.get(bb, 0):.0f}" if bb is not None else "—"
        a = f"{ba:.{dp}f}×{book.asks.get(ba, 0):.0f}" if ba is not None else "—"
        return f"bid {b}  ask {a}"
    return (f"bid[{lv(book.top('bid', depth))}]  "
            f"ask[{lv(book.top('ask', depth))}]")


async def run_market(market, args, stats: LatencyStats):
    """Одно 5-минутное окно: подписка, инкрементальные обновления, вывод.
    Возвращается, когда окно закончилось (пора перекатываться)."""
    names = {market["up"]: "Up  ", market["down"]: "Down"}
    books = {market["up"]: Book(), market["down"]: Book()}
    sub = json.dumps({"assets_ids": list(books), "type": "market"})
    end_ts = market["window_ts"] + WINDOW_SECONDS
    backoff = 0.5
    said_err = False

    out(f"── {market['question']} | окно до "
        f"{datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')} ──")

    while time.time() < end_ts + 2:
        try:
            async with websockets.connect(
                    CLOB_WS, compression=None, open_timeout=8,
                    user_agent_header="Mozilla/5.0") as ws:
                await ws.send(sub)
                out(f"[clob-ws] подписка отправлена (Up + Down), жду снимок")
                backoff = 0.5
                said_err = False
                last_ping = time.time()
                while time.time() < end_ts + 2:
                    if time.time() - last_ping >= 10:
                        await ws.send("PING")
                        last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    recv = now_ms()
                    if not isinstance(raw, str) or not raw.startswith(("{", "[")):
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    stats.msgs += 1
                    events = data if isinstance(data, list) else [data]
                    for ev in events:
                        if isinstance(ev, dict):
                            _handle(ev, recv, books, names, stats, args)
        except Exception as e:
            if time.time() >= end_ts:
                break
            if not said_err:
                out(f"[clob-ws] обрыв: {e}; переподключаюсь (молча)")
                said_err = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)


def _handle(ev: dict, recv: float, books, names, stats: LatencyStats, args):
    etype = ev.get("event_type") or ev.get("type")
    lat = None
    ts = ev.get("timestamp")
    if ts is not None:
        try:
            lat = recv - float(ts)
            stats.add(lat)
        except (TypeError, ValueError):
            pass
    lat_s = f" | лаг {lat:4.0f}мс" if lat is not None else ""

    if etype == "book":
        book = books.get(str(ev.get("asset_id")))
        if book is None:
            return
        book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                            ev.get("asks") or ev.get("sells"))
        stats.events += 1
        nm = names[str(ev["asset_id"])]
        out(f"{ts_str()} СНИМОК {nm} {fmt_top(book, args.depth)} "
            f"(bid-уровней {len(book.bids)}, ask-уровней {len(book.asks)})"
            f"{lat_s}")

    elif etype == "price_change":
        # старый формат: {asset_id, changes:[{price,side,size}]}
        # новый формат: {price_changes:[{asset_id,price,side,size}]}
        changed = []
        for ch in ev.get("changes") or []:
            tok = str(ev.get("asset_id"))
            book = books.get(tok)
            if book is not None and book.have_snapshot:
                if book.apply_change(ch.get("side"), ch.get("price"),
                                     ch.get("size")):
                    changed.append(tok)
        for ch in ev.get("price_changes") or []:
            tok = str(ch.get("asset_id"))
            book = books.get(tok)
            if book is not None and book.have_snapshot:
                if book.apply_change(ch.get("side"), ch.get("price"),
                                     ch.get("size")):
                    changed.append(tok)
        stats.events += 1
        if changed and not args.trades_only:
            # Троттлинг печати (НЕ данных): книга обновлена полностью, но
            # строка с топом выводится не чаще --interval на сторону —
            # иначе сотни строк/с утопят консоль. Печатается всегда
            # ТЕКУЩЕЕ состояние, так что пропущенных данных нет.
            now = time.time()
            for tok in dict.fromkeys(changed):     # уникально, в порядке
                b = books[tok]
                if now - b.last_top_print >= args.interval:
                    b.last_top_print = now
                    out(f"{ts_str()} Δтоп   {names[tok]} "
                        f"{fmt_top(b, args.depth)}{lat_s}")

    elif etype == "last_trade_price":
        tok = str(ev.get("asset_id"))
        book = books.get(tok)
        if book is None:
            return
        try:
            book.last_trade = float(ev.get("price"))
        except (TypeError, ValueError):
            return
        side = str(ev.get("side", "")).upper()
        size = ev.get("size")
        stats.events += 1
        sz = ""
        try:
            sz = f" × {float(size):.0f}"
        except (TypeError, ValueError):
            pass
        out(f"{ts_str()} СДЕЛКА {names[tok]} {book.last_trade:.2f}{sz}"
            f" {side}{lat_s}")

    # tick_size_change и незнакомые события — молча игнорируем


async def stats_task(stats: LatencyStats, every: float = 10.0):
    while True:
        await asyncio.sleep(every)
        out(f"    {stats.line(every)}")
        stats.msgs = 0
        stats.events = 0


async def main_async(args):
    stats = LatencyStats()
    asyncio.create_task(stats_task(stats))
    while True:
        market = await discover_market(args.coin)
        await run_market(market, args, stats)   # вернулся = окно закончилось


def choose_coin_interactively() -> str:
    order = list(COINS)
    print("Стакан какой монеты смотреть?")
    for i, c in enumerate(order, 1):
        print(f"  {i}. {COINS[c]}")
    while True:
        try:
            s = input("Введи номер (1-5) или имя и нажми Enter: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nвыход")
        if s.isdigit() and 1 <= int(s) <= len(order):
            return order[int(s) - 1]
        if s in COINS:
            return s
        print("Не понял. Примеры: 1  или  btc")


def main():
    ap = argparse.ArgumentParser(
        description="Polymarket Up/Down: книга заявок в реальном времени "
                    "(CLOB WebSocket, push без опроса)")
    ap.add_argument("--coin", choices=sorted(COINS), default="btc")
    ap.add_argument("--depth", type=int, default=1,
                    help="сколько уровней стакана показывать (по умолч. 1)")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="мин. пауза между строками топа на сторону, сек "
                         "(данные обновляются всегда; 0 = печатать всё)")
    ap.add_argument("--trades-only", action="store_true",
                    help="печатать только сделки (без изменений топа)")
    args = ap.parse_args()

    if "--coin" not in sys.argv[1:] and sys.stdin is not None \
            and sys.stdin.isatty():
        args.coin = choose_coin_interactively()

    out(f"=== Book Monitor v1 — {COINS[args.coin]} Up/Down 5m ===")
    out("источник: wss://ws-subscriptions-clob.polymarket.com/ws/market "
        "(push-поток самого сайта; снимок + каждая дельта + каждая сделка)")
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
