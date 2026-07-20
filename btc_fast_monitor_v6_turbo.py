#!/usr/bin/env python3
"""
BTC Fast Monitor v6-TURBO — тот же v6 (та же математика, тот же якорь,
те же сигналы), но с выжатой скоростью ДОБЫЧИ данных. Ни одна формула
не изменена — если v6 тебя устраивал по точности, эта версия показывает
то же самое, только раньше.

Что ускорено относительно v6 (только скорость, ничего больше):

  1. ПЕЧАТЬ В ОТДЕЛЬНОМ ПОТОКЕ. В v6 print() стоял в главном цикле:
     медленная консоль Windows тормозила приём данных, а выделение текста
     мышью ЗАМОРАЖИВАЛО бота целиком. Теперь строки уходят в очередь,
     пишет фоновый поток — приём с бирж не ждёт консоль никогда.

  2. СЖАТИЕ WEBSOCKET ВЫКЛЮЧЕНО (compression=None) на всех биржах.
     По умолчанию websockets договаривается о permessage-deflate: каждая
     цитата распаковывается, а биржа ещё и копит сообщения в пачки перед
     сжатием. Без сжатия цитаты летят сырыми — минус миллисекунды на
     каждом обновлении.

  3. OKX ПЕРЕВЕДЁН НА bbo-tbt — лучший bid/ask «тик-в-тик» каждые ~10мс
     вместо снимков books5 раз в ~100мс. Если канал недоступен —
     автоматический откат на старый books5.

  4. НОВЫЙ ИСТОЧНИК Bybit (spot orderbook.1) — пуш каждые ~10-40мс,
     самый быстрый из доступных фидов. Входит в быстрый слой через
     EMA-смещение, как Binance и OKX. Отключается флагом --no-bybit.

Запуск — как у v6:
    pip install websockets
    python btc_fast_monitor_v6_turbo.py --auto-target
"""

import asyncio
import json
import math
import queue
import sys
import time
import argparse
import statistics
import threading
import urllib.request
import http.client
from collections import deque
from datetime import datetime

try:
    import websockets
except ImportError:
    raise SystemExit("Установите зависимость:  pip install websockets")

# ------------------------- состояние -------------------------

USD_SOURCES = ("coinbase", "kraken", "bitstamp", "pyth")  # формируют якорь
FAST_EXTRA = ("binance", "okx", "bybit")                  # USDT, через смещение
ALL_SOURCES = USD_SOURCES + FAST_EXTRA

prices = {ex: None for ex in ALL_SOURCES}
last_update = {ex: 0.0 for ex in prices}

SHORT = {"coinbase": "cb", "kraken": "kr", "bitstamp": "bs",
         "pyth": "py", "binance": "bn", "okx": "ok", "bybit": "bb"}

UPDATE_EVENT = asyncio.Event()   # будит вывод при любом обновлении цены


def now_ms() -> float:
    return time.time() * 1000


def set_price(ex, px):
    prices[ex] = px
    last_update[ex] = now_ms()
    UPDATE_EVENT.set()


# --------- печать без блокировок (консоль не тормозит приём) ---------

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


# ------------------------- подключения к источникам -------------------------

async def coinbase_ws():
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = json.dumps({"type": "subscribe",
                      "product_ids": ["BTC-USD"],
                      "channels": ["ticker"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20,
                                          compression=None) as ws:
                await ws.send(sub)
                out("[coinbase] подключено (BTC/USD, mid bid/ask)")
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("type") == "ticker":
                        bid, ask = d.get("best_bid"), d.get("best_ask")
                        if bid and ask:
                            set_price("coinbase", (float(bid) + float(ask)) / 2)
                        elif d.get("price"):
                            set_price("coinbase", float(d["price"]))
        except Exception as e:
            out(f"[coinbase] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


async def kraken_ws():
    url = "wss://ws.kraken.com/v2"
    sub = json.dumps({"method": "subscribe",
                      "params": {"channel": "ticker",
                                 "symbol": ["BTC/USD"],
                                 "event_trigger": "bbo"}})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20,
                                          compression=None) as ws:
                await ws.send(sub)
                out("[kraken] подключено (BTC/USD, bbo-триггер)")
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("channel") == "ticker" and d.get("data"):
                        t = d["data"][-1]
                        bid, ask = t.get("bid"), t.get("ask")
                        if bid and ask:
                            set_price("kraken", (float(bid) + float(ask)) / 2)
                        elif t.get("last"):
                            set_price("kraken", float(t["last"]))
        except Exception as e:
            out(f"[kraken] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


async def bitstamp_ws():
    url = "wss://ws.bitstamp.net"
    sub = json.dumps({"event": "bts:subscribe",
                      "data": {"channel": "order_book_btcusd"}})
    while True:
        try:
            async with websockets.connect(url, ping_interval=20,
                                          compression=None) as ws:
                await ws.send(sub)
                out("[bitstamp] подключено (BTC/USD, mid стакана)")
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("event") == "data":
                        data = d.get("data", {})
                        bids, asks = data.get("bids"), data.get("asks")
                        if bids and asks:
                            set_price("bitstamp",
                                      (float(bids[0][0]) + float(asks[0][0])) / 2)
        except Exception as e:
            out(f"[bitstamp] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


async def binance_ws():
    url = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20,
                                          compression=None) as ws:
                out("[binance] подключено (BTC/USDT bookTicker)")
                async for msg in ws:
                    d = json.loads(msg)
                    bid, ask = d.get("b"), d.get("a")
                    if bid and ask:
                        set_price("binance", (float(bid) + float(ask)) / 2)
        except Exception as e:
            out(f"[binance] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


async def okx_ws():
    """OKX bbo-tbt: лучший bid/ask тик-в-тик (~10мс). Откат на books5."""
    url = "wss://ws.okx.com:8443/ws/v5/public"
    channel = "bbo-tbt"
    while True:
        sub = json.dumps({"op": "subscribe",
                          "args": [{"channel": channel,
                                    "instId": "BTC-USDT"}]})
        try:
            async with websockets.connect(url, ping_interval=None,
                                          compression=None) as ws:
                await ws.send(sub)
                out(f"[okx] подключено (BTC-USDT {channel})")
                silent_deadline = time.time() + 12
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        await ws.send("ping")     # OKX требует ping < 30с
                        if silent_deadline and time.time() > silent_deadline:
                            raise RuntimeError(f"нет данных по {channel}")
                        continue
                    if msg == "pong":
                        continue
                    d = json.loads(msg)
                    if d.get("event") == "error":
                        raise RuntimeError(d.get("msg") or "subscribe error")
                    if d.get("arg", {}).get("channel") == channel \
                            and d.get("data"):
                        bk = d["data"][0]
                        if bk.get("bids") and bk.get("asks"):
                            set_price("okx", (float(bk["bids"][0][0]) +
                                              float(bk["asks"][0][0])) / 2)
                            silent_deadline = None
                    if silent_deadline and time.time() > silent_deadline:
                        raise RuntimeError(f"нет данных по {channel}")
        except RuntimeError as e:
            if channel == "bbo-tbt":
                out(f"[okx] {e}; переключаюсь на books5")
                channel = "books5"
                continue
            out(f"[okx] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)
        except Exception as e:
            out(f"[okx] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


async def bybit_ws():
    """Bybit spot orderbook.1: лучший bid/ask, пуш каждые ~10-40мс."""
    url = "wss://stream.bybit.com/v5/public/spot"
    sub = json.dumps({"op": "subscribe", "args": ["orderbook.1.BTCUSDT"]})
    while True:
        try:
            async with websockets.connect(url, ping_interval=None,
                                          compression=None) as ws:
                await ws.send(sub)
                out("[bybit] подключено (BTCUSDT orderbook.1)")
                bid = ask = None
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"op": "ping"}))
                        continue
                    d = json.loads(msg)
                    data = d.get("data")
                    if not (isinstance(data, dict) and d.get("topic", "")
                            .startswith("orderbook.1.")):
                        continue
                    # массив пуст, если сторона не менялась — держим прежнюю
                    if data.get("b"):
                        bid = float(data["b"][0][0])
                    if data.get("a"):
                        ask = float(data["a"][0][0])
                    if bid is not None and ask is not None:
                        set_price("bybit", (bid + ask) / 2)
        except Exception as e:
            out(f"[bybit] обрыв: {e}; переподключение через 2с")
            await asyncio.sleep(2)


# ---- Pyth: поток SSE (мгновенные обновления), при неудаче — опрос ----

PYTH_BTC_ID = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
PYTH_HOST = "hermes.pyth.network"
PYTH_STREAM = f"/v2/updates/price/stream?ids[]={PYTH_BTC_ID}&parsed=true"
PYTH_LATEST = (f"https://{PYTH_HOST}/v2/updates/price/latest"
               f"?ids[]={PYTH_BTC_ID}&parsed=true")


def _pyth_parse(d):
    p = d["parsed"][0]["price"]
    return float(p["price"]) * 10 ** int(p["expo"])


def _pyth_worker(loop):
    """Фоновый поток: сначала SSE-стрим, после 3 неудач — откат на опрос."""
    def push(px):
        prices["pyth"] = px
        last_update["pyth"] = now_ms()
        loop.call_soon_threadsafe(UPDATE_EVENT.set)

    sse_fails = 0
    announced = False
    while sse_fails < 3:
        try:
            conn = http.client.HTTPSConnection(PYTH_HOST, timeout=15)
            conn.request("GET", PYTH_STREAM,
                         headers={"Accept": "text/event-stream",
                                  "User-Agent": "btc-monitor"})
            r = conn.getresponse()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            if not announced:
                out("[pyth] подключено (BTC/USD агрегат, поток)")
                announced = True
            while True:
                line = r.readline()
                if not line:
                    raise RuntimeError("поток закрыт")
                if line.startswith(b"data:"):
                    push(_pyth_parse(json.loads(line[5:])))
                    sse_fails = 0
        except Exception as e:
            sse_fails += 1
            if sse_fails >= 3:
                out(f"[pyth] поток недоступен ({e}); перехожу на опрос")
            time.sleep(2)

    last_err = None
    while True:
        try:
            with urllib.request.urlopen(PYTH_LATEST, timeout=3) as r:
                push(_pyth_parse(json.loads(r.read())))
            if not announced:
                out("[pyth] подключено (BTC/USD агрегат, опрос)")
                announced = True
            last_err = None
            time.sleep(0.3)
        except Exception as e:
            if str(e) != last_err:
                out(f"[pyth] ошибка: {e}; продолжаю без Pyth, повторяю фоном")
                last_err = str(e)
            time.sleep(2)


async def pyth_feed():
    loop = asyncio.get_running_loop()
    threading.Thread(target=_pyth_worker, args=(loop,), daemon=True).start()
    while True:
        await asyncio.sleep(3600)


pyth_poll = pyth_feed          # совместимость со старым именем


def feed_tasks(no_pyth=False, no_binance=False, no_okx=False, no_bybit=False):
    """Все фоновые задачи ценовых фидов (для импорта ботом)."""
    tasks = [coinbase_ws(), kraken_ws(), bitstamp_ws()]
    if not no_binance:
        tasks.append(binance_ws())
    if not no_okx:
        tasks.append(okx_ws())
    if not no_bybit:
        tasks.append(bybit_ws())
    if not no_pyth:
        tasks.append(pyth_feed())
    return tasks


# ------------------------- цена: якорь + быстрый слой -------------------------

class Consensus:
    """Живая цена = быстрые фиды, дебиасированные к устойчивому якорю.

    Якорь: медиана свежих USD-источников с отсевом выбросов (уровень цены).
    Для каждого источника ведётся EMA-смещение к якорю; живая цена —
    взвешенное среднее (цена − смещение), вес = exp(−возраст/тау).
    Математика идентична v6.
    """

    def __init__(self, outlier_bps=25.0, stale_ms=3000, fast_ms=1500.0,
                 weight_tau_ms=400.0, offset_halflife_s=60.0,
                 warmup_s=5.0, tick_s=0.25):
        self.outlier = outlier_bps / 10_000
        self.stale_ms = stale_ms
        self.fast_ms = fast_ms
        self.tau = weight_tau_ms
        self.halflife_ms = offset_halflife_s * 1000
        self.warmup_ms = warmup_s * 1000
        self.anchor = None
        # ex -> [offset, ts последнего апдейта, ts первого апдейта]
        self.off = {}

    def _update_offset(self, ex, gap, t):
        st = self.off.get(ex)
        if st is None:
            self.off[ex] = [gap, t, t]
            return
        dt = max(t - st[1], 1.0)
        alpha = 1 - 0.5 ** (dt / self.halflife_ms)
        st[0] += alpha * (gap - st[0])
        st[1] = t

    def compute(self, t):
        fresh = {ex: (prices[ex], t - last_update[ex]) for ex in ALL_SOURCES
                 if prices[ex] is not None
                 and t - last_update[ex] < self.stale_ms}
        if not fresh:
            return (self.anchor, {}, {}) if self.anchor else (None, {}, {})

        # 1) якорь: медиана USD-источников с отсевом выбросов
        usd = {ex: p for ex, (p, _) in fresh.items() if ex in USD_SOURCES}
        if usd:
            med = statistics.median(usd.values())
            good = [p for p in usd.values() if abs(p - med) / med <= self.outlier]
            self.anchor = statistics.median(good) if good else med
        if self.anchor is None:
            return None, {}, {}
        anchor = self.anchor

        # 2) смещения источников к якорю
        for ex, (p, _) in fresh.items():
            self._update_offset(ex, p - anchor, t)

        # 3) быстрый слой: взвешенное среднее дебиасированных цен
        accepted, rejected = {}, {}
        num = den = 0.0
        for ex, (p, age) in fresh.items():
            st = self.off[ex]
            if t - st[2] < self.warmup_ms:      # смещение ещё не прогрето
                continue
            deb = p - st[0]
            if abs(deb - anchor) / anchor > self.outlier:
                rejected[ex] = deb
                continue
            accepted[ex] = deb
            if age <= self.fast_ms:
                w = math.exp(-age / self.tau)
                num += w * deb
                den += w

        live = num / den if den > 0 else anchor
        if not accepted:
            accepted = {ex: p for ex, (p, _) in fresh.items()
                        if ex in USD_SOURCES}
        return live, accepted, rejected


# ------------------------- наукаст (прогноз на lead секунд) -------------------------

class Nowcast:
    """Скорость живой цены за последние ~1.5с → цена через lead секунд."""

    def __init__(self, window_s=1.5):
        self.window_s = window_s
        self.pts = deque()

    def add(self, t, p):
        self.pts.append((t, p))
        while self.pts and t - self.pts[0][0] > self.window_s:
            self.pts.popleft()

    def project(self, lead_s):
        if len(self.pts) < 5:
            return None
        t0 = self.pts[0][0]
        xs = [t - t0 for t, _ in self.pts]
        ys = [p for _, p in self.pts]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        var = sum((x - mx) ** 2 for x in xs)
        if var <= 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
        return self.pts[-1][1] + slope * lead_s


# ------------------------- волатильность и вероятность -------------------------

class VolEstimator:
    """Робастная σ консенсус-цены: MAD лог-доходностей за окно."""

    def __init__(self, window_s=120.0, tick_s=0.25):
        self.tick_s = tick_s
        self.hist = deque(maxlen=max(8, int(window_s / tick_s)))

    def add(self, price):
        self.hist.append(price)

    def sigma_1s(self):
        if len(self.hist) < 8:
            return None
        rets = [math.log(b / a) for a, b in zip(self.hist, list(self.hist)[1:])
                if a > 0]
        if len(rets) < 7:
            return None
        med = statistics.median(rets)
        mad = statistics.median(abs(r - med) for r in rets)
        sigma_tick = 1.4826 * mad
        if sigma_tick <= 0:
            sigma_tick = statistics.pstdev(rets)
        if sigma_tick <= 0:
            return None
        return sigma_tick * math.sqrt(1 / self.tick_s) * self.hist[-1]


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_up(diff, sigma_1s, seconds_left):
    if sigma_1s is None or sigma_1s <= 0:
        return None
    horizon = max(seconds_left, 0.5)
    return phi(diff / (sigma_1s * math.sqrt(horizon)))


def seconds_left_in_round(round_minutes=5) -> float:
    period = round_minutes * 60
    return period - (time.time() % period)


# ------------------------- основной цикл -------------------------

async def monitor(args):
    await asyncio.sleep(2)
    cons = Consensus(outlier_bps=args.outlier_bps,
                     offset_halflife_s=args.offset_halflife)
    vol = VolEstimator(window_s=args.vol_window, tick_s=0.25)
    nc = Nowcast()
    target = args.target
    prev_price = None
    prev_signal = None
    prev_left = seconds_left_in_round(args.round_minutes)
    last_print = 0.0
    last_vol = 0.0
    out(f"nc = прогноз цены через {args.lead:.1f}с (наукаст)")

    while True:
        # просыпаемся сразу при новых данных, максимум ждём 0.25с
        try:
            await asyncio.wait_for(UPDATE_EVENT.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
        UPDATE_EVENT.clear()

        t = now_ms()
        if t - last_print < args.interval * 1000:
            continue
        last_print = t

        price, accepted, rejected = cons.compute(t)
        left = seconds_left_in_round(args.round_minutes)
        new_round = left > prev_left + 1
        prev_left = left

        if price is None:
            out(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
                f"нет свежих данных...")
            continue

        if t - last_vol >= 250:                # σ считаем на сетке 0.25с
            vol.add(price)
            last_vol = t
        sigma = vol.sigma_1s()
        nc.add(t / 1000, price)
        forecast = nc.project(args.lead)

        if new_round:
            if args.auto_target:
                target = round(price, 2)
                out(f"--- новый раунд: цель зафиксирована {target:,.2f} ---")
            elif args.target is not None:
                out("--- новый раунд: обнови --target с Polymarket "
                    "(или запусти с --auto-target) ---")

        arrow = " "
        if prev_price is not None:
            arrow = "^" if price > prev_price else \
                    ("v" if price < prev_price else "=")
        prev_price = price

        band = 0.0
        if accepted:
            band = (max(accepted.values()) - min(accepted.values())) / 2
        agree = band / price <= args.agree_bps / 10_000

        ages = " ".join(
            f"{SHORT[ex]}{(t - last_update[ex]):3.0f}" for ex in ALL_SOURCES
            if prices[ex] is not None and t - last_update[ex] < 5000)

        line = (f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
                f"BTC {price:,.2f}{arrow}")
        if forecast is not None:
            line += f" nc={forecast:,.2f}"
        line += f" ±{band:.2f} возр.мс[{ages}]"
        if rejected:
            line += "  выброс:" + ",".join(SHORT[ex] for ex in rejected)
        if sigma is not None:
            line += f"  σ1s={sigma:.2f}$"

        if target is not None:
            diff = price - target
            line += (f"  | цель {target:,.2f}  diff {diff:+.2f}"
                     f"  осталось {left:5.1f}с")
            prob = p_up(diff, sigma, left)
            signal = None
            if prob is not None:
                line += f"  P(UP)={prob:5.1%}"
                if left <= args.window and abs(diff) >= args.min_diff:
                    if prob >= args.conf:
                        signal = "UP"
                    elif prob <= 1 - args.conf:
                        signal = "DOWN"
                if signal and not agree:
                    line += "  [биржи расходятся — сигнал не даю]"
                    signal = None
            if signal and signal != prev_signal:
                mark = "^" if signal == "UP" else "v"
                line += (f"   >>> СИГНАЛ: {signal} {mark} "
                         f"(P={prob:.0%}, ${abs(diff):.2f})")
                line += "\a"
            elif signal:
                line += f"   [сигнал {signal} активен]"
            elif prev_signal and signal is None:
                line += "   [сигнал снят]"
            prev_signal = signal

        out(line)


async def main():
    ap = argparse.ArgumentParser(
        description="BTC/USD монитор v6-TURBO: математика v6, скорость макс")
    ap.add_argument("--target", type=float, default=None,
                    help="цель текущего раунда (цена с Polymarket)")
    ap.add_argument("--auto-target", action="store_true",
                    help="фиксировать цель по консенсусу на границе раунда")
    ap.add_argument("--round-minutes", type=int, default=5)
    ap.add_argument("--conf", type=float, default=0.80,
                    help="порог вероятности для сигнала (0.5..1)")
    ap.add_argument("--min-diff", type=float, default=3.0,
                    help="минимальное |отклонение| в $ для сигнала")
    ap.add_argument("--window", type=float, default=25.0,
                    help="сигналить только в последние N секунд раунда")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="мин. пауза между строками, сек (0.1 = до 10/с)")
    ap.add_argument("--lead", type=float, default=0.4,
                    help="горизонт наукаста nc=, сек")
    ap.add_argument("--vol-window", type=float, default=120.0)
    ap.add_argument("--outlier-bps", type=float, default=25.0)
    ap.add_argument("--agree-bps", type=float, default=8.0)
    ap.add_argument("--offset-halflife", type=float, default=60.0,
                    help="полупериод EMA смещений источников, сек")
    ap.add_argument("--no-pyth", action="store_true")
    ap.add_argument("--no-binance", action="store_true")
    ap.add_argument("--no-okx", action="store_true")
    ap.add_argument("--no-bybit", action="store_true")
    args = ap.parse_args()

    out("=== BTC Fast Monitor v6-TURBO (математика v6, добыча быстрее) ===")
    await asyncio.gather(
        monitor(args),
        *feed_tasks(no_pyth=args.no_pyth, no_binance=args.no_binance,
                    no_okx=args.no_okx, no_bybit=args.no_bybit))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nостановлено")
