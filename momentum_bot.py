#!/usr/bin/env python3
"""
Momentum Bot — ловит резкое движение цены BTC и входит РАНЬШЕ толпы.

Идея (ровно как ты описал):
  * непрерывно смотрит ЦЕНУ BTC (через бот 1 — fast_monitor: сводная цена
    с бирж, чуть впереди сайта Polymarket);
  * непрерывно смотрит КНИГУ ЗАЯВОК UP/DOWN 5-мин рынка (через бот 2 —
    book_monitor: живой стакан, куда толпа ставит проценты);
  * когда цена BTC делает РЕЗКОЕ движение — рынок процентов ещё не успел
    переставиться, и бот СРАЗУ покупает ту сторону, КУДА ПОШЛА ЦЕНА:
        цена рванула вверх  -> покупает UP
        цена рванула вниз   -> покупает DOWN
    (только если у этой стороны ещё есть куда расти — ask не у потолка);
  * держит ставку и продолжает следить; ФИКСИРУЕТ, когда проценты этой
    стороны сдвинулись в плюс (цена стороны выросла на --take-profit),
    либо СТОП если пошло против (--stop), либо расчёт в конце окна.
  * ставка $1 (--stake), лимита сделок в окне нет — после закрытия ждёт
    следующего резкого движения и входит снова.

БУМАЖНЫЙ режим по умолчанию: реальные ордера НЕ отправляются, всё пишется
в CSV, чтобы ты видел, как система ловит и держит, и померил результат
БЕЗ риска. Честно моделирует гонку: после сигнала «фил» проверяется через
--order-latency-ms (успел ли маркет-мейкер снять отставшую заявку). Поставь
задержку под свою (дома ~150-250мс, VPS ~20-50мс) — бумага станет прогнозом.

Оба исходных бота импортируются как есть и НЕ изменяются. Скорость
сохранена: событийно, stream без опроса, compression=None, orjson/uvloop
если стоят.

Запуск:
    pip install websockets            (orjson/uvloop по желанию — быстрее)
    python momentum_bot.py --coin btc
"""

import argparse
import asyncio
import csv
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime


def _fatal(msg: str):
    """Показать понятную ошибку и НЕ закрывать окно (для двойного клика)."""
    print("\n" + "=" * 64)
    print("MOMENTUM BOT НЕ ЗАПУСТИЛСЯ:")
    print(msg)
    print("=" * 64)
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажми Enter, чтобы закрыть окно...")
    except Exception:
        pass
    raise SystemExit(1)


# momentum_bot ДОЛЖЕН лежать в ОДНОЙ ПАПКЕ с fast_monitor.py и book_monitor.py.
# Импортируем из папки самого скрипта, чтобы двойной клик тоже работал.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fast_monitor as fm   # бот 1: цена BTC (Consensus, VolEstimator, фиды)
except Exception as e:          # noqa: BLE001
    _fatal("не найден бот ЦЕНЫ 'fast_monitor.py'.\n"
           "Положи momentum_bot.py в ту же папку, где лежит fast_monitor.py "
           "(монитор цены на 5 монет, НЕ v6-turbo).\n"
           f"Причина: {e}")
try:
    import book_monitor as bm   # бот 2: книга заявок (Book, discover_market, WS)
except Exception as e:          # noqa: BLE001
    _fatal("не найден бот СТАКАНА 'book_monitor.py'.\n"
           "Положи momentum_bot.py в ту же папку, где лежит book_monitor.py.\n"
           f"Причина: {e}")

for _need in ("Consensus", "VolEstimator", "feed_tasks", "COINS",
              "seconds_left_in_round", "UPDATE_EVENT"):
    if not hasattr(fm, _need):
        _fatal("рядом лежит НЕ тот монитор цены.\n"
               "Нужен fast_monitor.py на 5 монет (btc/eth/sol/xrp/doge) — "
               "с автоцелью с Polymarket, из архива memorybot.\n"
               f"В нём не хватает '{_need}'.")

try:
    import websockets
except ImportError:
    _fatal("не установлена библиотека websockets.\n"
           "Открой терминал в этой папке и выполни:  pip install websockets")

try:
    import orjson
    _loads = orjson.loads
except Exception:  # noqa: BLE001
    import json as _json
    _loads = _json.loads


def now_ms() -> float:
    return time.time() * 1000


def ts_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def utc_str() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------- печать без блокировок -------------------------
import queue as _queue
_PRINT_Q: "_queue.Queue[str]" = _queue.Queue()
_printer = False


def out(line: str) -> None:
    global _printer
    if not _printer:
        _printer = True
        threading.Thread(
            target=lambda: [sys.stdout.write(_PRINT_Q.get() + "\n")
                            or sys.stdout.flush() for _ in iter(int, 1)],
            daemon=True).start()
    _PRINT_Q.put(line)


# ------------------------- живой стакан UP/DOWN -------------------------

class BookState:
    """Стакан UP и DOWN текущего окна (класс Book из book_monitor, без печати)."""

    def __init__(self):
        self.books = {}
        self.tokens = {"up": None, "down": None}

    def set_market(self, up_token, down_token):
        self.tokens = {"up": up_token, "down": down_token}
        self.books = {up_token: bm.Book(), down_token: bm.Book()}

    def ask(self, side):
        b = self.books.get(self.tokens[side])
        return b.best_ask() if b else None

    def bid(self, side):
        b = self.books.get(self.tokens[side])
        return b.best_bid() if b else None

    def mid(self, side):
        b = self.books.get(self.tokens[side])
        if not b:
            return None
        bb, ba = b.best_bid(), b.best_ask()
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb if bb is not None else ba

    def apply(self, ev):
        etype = ev.get("event_type") or ev.get("type")
        if etype == "book":
            book = self.books.get(str(ev.get("asset_id")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                    ev.get("asks") or ev.get("sells"))
        elif etype == "price_change":
            for ch in ev.get("changes") or []:
                book = self.books.get(str(ev.get("asset_id")))
                if book is not None and book.have_snapshot:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))
            for ch in ev.get("price_changes") or []:
                book = self.books.get(str(ch.get("asset_id")))
                if book is not None and book.have_snapshot:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))


async def book_feed(book: BookState, coin: str, connections: int):
    """Поток стакана текущего 5-мин окна; при смене окна — новые токены."""
    while True:
        market = await bm.discover_market(coin)
        book.set_market(market["up"], market["down"])
        end_ts = market["window_ts"] + bm.WINDOW_SECONDS
        out(f"── окно {market['slug']} до "
            f"{datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')} ──")
        sub = fm.json.dumps({"assets_ids": [market["up"], market["down"]],
                             "type": "market"})
        seen, seen_q = set(), deque(maxlen=8192)

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
                                        book.apply(ev)
                        finally:
                            for t in aux:
                                t.cancel()
                except Exception:
                    if time.time() >= end_ts:
                        break
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)

        await asyncio.gather(*(conn() for _ in range(connections)))


# ------------------------- бумажная торговля моментума -------------------------

class PaperTrader:
    FIELDS = ["entry_utc", "slug", "side", "trigger_move_usd", "secs_left",
              "entry_price", "filled", "exit_reason", "exit_price",
              "pnl_usd", "cum_pnl_usd", "n_trades"]

    def __init__(self, args):
        self.args = args
        self.pending = None
        self.pos = None
        self.cum = 0.0
        self.n_sig = 0
        self.n_fill = 0
        self.n_miss = 0
        self.n_reverse = 0
        self.wins = 0
        self.losses = 0
        self.path = args.log or f"momentum_{args.coin}_paper.csv"
        if self.path and not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def _log(self, row):
        if not self.path:
            return
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(
                {k: row.get(k, "") for k in self.FIELDS})

    def busy(self):
        return self.pending is not None or self.pos is not None

    def signal(self, side, ask, move_usd, secs_left, slug):
        """Резкое движение -> вход в сторону движения (если есть куда расти)."""
        if self.busy() or ask is None:
            return
        if ask > self.args.max_entry:      # уже у потолка — расти некуда
            return
        if ask < self.args.min_entry:      # 0.01 = протухший/крайний токен,
            return                          # не реальный вход (защита от границы)
        self.n_sig += 1
        self.pending = {"side": side, "ask": ask, "move": move_usd,
                        "secs_left": secs_left, "slug": slug,
                        "entry_utc": utc_str(),
                        "check_at": now_ms() + self.args.order_latency_ms}
        out(f"{ts_str()} СИГНАЛ {side.upper():4} рывок {move_usd:+.1f}$ "
            f"-> беру по ask {ask:.2f}  (проверю фил через "
            f"{self.args.order_latency_ms:.0f}мс)")

    def check_fill(self, book):
        p = self.pending
        if p is None or now_ms() < p["check_at"]:
            return
        self.pending = None
        ask = book.ask(p["side"])
        # ПРЕДОСТОРОЖНОСТЬ 1 (догон заявки): если не успели по старой цене,
        # берём по новой, но только если она ушла не больше чем на chase_cents
        # (1-2 цента). Дальше — уже дорого, отказ.
        limit = p["ask"] + self.args.chase_cents / 100.0
        if ask is not None and ask <= limit + 1e-9:
            self.n_fill += 1
            chased = ask > p["ask"] + 1e-9
            self.pos = {"side": p["side"], "entry": ask, "stake": self.args.stake,
                        "peak": ask, "move": p["move"], "slug": p["slug"],
                        "secs_left": p["secs_left"], "entry_utc": p["entry_utc"],
                        "hedged": False, "opened_ms": now_ms()}
            tag = f" (догнал +{(ask - p['ask']) * 100:.0f}¢)" if chased else ""
            out(f"{ts_str()} ФИЛ    {p['side'].upper():4} по {ask:.2f} "
                f"на ${self.args.stake:.0f}{tag} — держу, слежу")
        else:
            self.n_miss += 1
            gone = "" if ask is None else f" (ушла на {(ask - p['ask']) * 100:+.0f}¢)"
            out(f"{ts_str()} ПРОМАХ {p['side'].upper():4} заявка {p['ask']:.2f} "
                f"ушла дальше {self.args.chase_cents:.0f}¢{gone} — отказ")
            self._log({"entry_utc": p["entry_utc"], "slug": p["slug"],
                       "side": p["side"], "trigger_move_usd": round(p["move"], 1),
                       "secs_left": round(p["secs_left"], 1), "filled": 0,
                       "cum_pnl_usd": round(self.cum, 3),
                       "n_trades": self.wins + self.losses})

    def manage(self, book, secs_left, move=0.0, settle_side=None):
        pos = self.pos
        if pos is None:
            return
        side = pos["side"]
        if settle_side is not None:            # конец окна — расчёт по знаку
            payout = 1.0 if settle_side == side else 0.0
            self._close(pos, payout, "экспирация")
            return
        px = book.mid(side)                    # текущая цена нашей стороны
        if px is None:
            return
        if px > pos["peak"]:                   # обновляем пик роста процентов
            pos["peak"] = px
        up = (px - pos["entry"]) * 100.0       # изменение от входа, ¢
        pull = (pos["peak"] - px) * 100.0      # откат от пика, ¢

        # ПРЕДОСТОРОЖНОСТЬ 2 (разворотный хедж): если цена РЕЗКО и СИЛЬНО
        # пошла против нашей стороны — тут же берём $3-4 на противоположную
        # и первую сбрасываем как можно раньше.
        our_dir = 1.0 if side == "up" else -1.0
        against = -our_dir * move              # >0 = движение против нас
        if (not pos["hedged"] and against >= self.args.reverse_usd
                and self.args.reverse_stake > 0):
            self._reverse_hedge(book, pos, px)
            return

        # ВЫХОД: держим, пока проценты РАСТУТ (цена делает новые пики) и
        # движение продолжается. Как только рост процентов остановился —
        # цена откатила от пика на trail_cents — фиксируем.
        if up > 0 and pull >= self.args.trail_cents:
            self._close(pos, px, "профит: рост процентов остановился")
            return
        # жёсткий стоп на случай, если против пошло без разворотного хеджа
        if up <= -self.args.stop_cents:
            self._close(pos, px, "стоп: против движения")
            return
        # необязательный потолок прибыли
        if up >= self.args.take_profit_cents:
            self._close(pos, px, "профит: цель")
            return
        if now_ms() - pos.get("opened_ms", now_ms()) > self.args.max_hold_ms:
            self._close(pos, px, "тайм-аут удержания")

    def _reverse_hedge(self, book, pos, px):
        """Цена развернулась против нас: сбросить первую сделку и взять
        противоположную сторону бОльшим размером."""
        # 1) сбрасываем первую как можно раньше (по текущей цене)
        self._close(pos, px, "разворот: сброс, беру противоположную")
        # 2) берём противоположную сторону на reverse_stake
        opp = "down" if pos["side"] == "up" else "up"
        ask = book.ask(opp)
        if ask is None or ask > self.args.max_entry or ask < self.args.min_entry:
            return
        self.n_reverse += 1     # это хедж, не сигнальный вход
        self.pos = {"side": opp, "entry": ask, "stake": self.args.reverse_stake,
                    "peak": ask, "move": 0.0, "slug": pos["slug"],
                    "secs_left": pos["secs_left"], "entry_utc": utc_str(),
                    "hedged": True, "opened_ms": now_ms()}
        out(f"{ts_str()} РАЗВОРОТ -> {opp.upper():4} по {ask:.2f} "
            f"на ${self.args.reverse_stake:.0f} (цена сильно пошла обратно)")

    def _close(self, pos, exit_price, reason):
        self.pos = None
        # P&L в долларах: держим stake/entry «долей», выход по exit_price
        shares = pos["stake"] / pos["entry"]
        pnl = round(shares * (exit_price - pos["entry"]), 3)
        self.cum += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        out(f"{ts_str()} ЗАКРЫЛ {pos['side'].upper():4} {pos['entry']:.2f}"
            f"->{exit_price:.2f}  P&L ${pnl:+.3f}  [{reason}]  "
            f"итого ${self.cum:+.3f} (W/L {self.wins}/{self.losses})")
        self._log({"entry_utc": pos["entry_utc"], "slug": pos["slug"],
                   "side": pos["side"], "trigger_move_usd": round(pos["move"], 1),
                   "secs_left": round(pos["secs_left"], 1),
                   "entry_price": round(pos["entry"], 4), "filled": 1,
                   "exit_reason": reason, "exit_price": round(exit_price, 4),
                   "pnl_usd": pnl, "cum_pnl_usd": round(self.cum, 3),
                   "n_trades": self.wins + self.losses})

    def summary(self):
        fr = 100.0 * self.n_fill / max(self.n_sig, 1)
        out(f"    ИТОГ: сигналов {self.n_sig}, филов {self.n_fill} "
            f"({fr:.0f}%), промахов {self.n_miss}, разворотов {self.n_reverse} "
            f"| сделок {self.wins + self.losses} (W/L {self.wins}/{self.losses}) "
            f"| суммарно ${self.cum:+.3f} | лог: {self.path}")


# ------------------------- главный цикл -------------------------

async def combine(args, book: BookState):
    await asyncio.sleep(2)
    cons = fm.Consensus(outlier_bps=args.outlier_bps, offset_halflife_s=60.0)
    vol = fm.VolEstimator(window_s=args.vol_window, tick_s=0.25)
    trader = PaperTrader(args)
    period = args.round_minutes * 60
    price_hist = deque()          # (t_ms, price) для скорости движения
    last_vol = 0.0
    last_stat = time.time()
    started = time.time()
    prev_start = None             # ts начала окна — для сброса на границе
    no_trade_until = 0.0          # пауза после смены окна (книга ещё грузится)

    out(f"=== Momentum Bot (БУМАЖНЫЙ) — {fm.COIN['name']} | ставка "
        f"${args.stake:.0f} | лог: momentum_{args.coin}_paper.csv ===")
    out(f"вход: рывок цены за {args.move_window_ms:.0f}мс сильнее порога "
        f"-> беру сторону движения; держу до +{args.take_profit_cents:.0f}¢ "
        f"или стоп -{args.stop_cents:.0f}¢")

    while True:
        try:
            await asyncio.wait_for(fm.UPDATE_EVENT.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        fm.UPDATE_EVENT.clear()

        t = now_ms()
        price, _, _ = cons.compute(t)
        if price is None:
            continue
        if t - last_vol >= 250:
            vol.add(price)
            last_vol = t
        sigma = vol.sigma_1s()
        left = fm.seconds_left_in_round(args.round_minutes)

        # граница окна: новые токены/книга — сбросить историю и выждать,
        # иначе «рывок» замеряется через стык и вход идёт по протухшему токену
        now_s0 = time.time()
        cur_start = int(now_s0 - now_s0 % period)
        if prev_start is None:
            prev_start = cur_start
        elif cur_start != prev_start:
            prev_start = cur_start
            price_hist.clear()
            no_trade_until = time.time() + 3.0   # дать книге прогрузиться

        # история цены + скорость движения за окно
        price_hist.append((t, price))
        while price_hist and t - price_hist[0][0] > args.move_window_ms:
            price_hist.popleft()
        move = 0.0
        if len(price_hist) >= 2:
            move = price - price_hist[0][1]     # Δ за move_window

        # порог «резкого» движения: авто = k×ожидаемого хода (σ·√окно), либо $
        if args.move_usd is not None:
            thresh = args.move_usd
        elif sigma is not None:
            thresh = args.move_sigma * sigma * (args.move_window_ms / 1000.0) ** 0.5
        else:
            thresh = None

        # 1) проверить висящий фил (гонка с ММ)
        trader.check_fill(book)

        # 2) вести открытую позицию
        now_s = time.time()
        start = int(now_s - now_s % period)
        if left <= args.settle_before and trader.pos is not None:
            # у нас нет собственной цели; исход по знаку хода за окно неизвестен
            # тут — расчёт по текущей стороне стакана (кто победил по книге)
            up_mid = book.mid("up")
            settle = "up" if (up_mid is not None and up_mid >= 0.5) else "down"
            trader.manage(book, left, move=move, settle_side=settle)
        elif trader.pos is not None:
            trader.manage(book, left, move=move)

        # 3) новый сигнал на резкое движение (не в конце окна, не на стыке)
        warm = (time.time() - started) >= args.warmup_seconds
        if warm and thresh is not None and not trader.busy() \
                and left > args.settle_before \
                and time.time() >= no_trade_until:
            slug = f"{args.coin}-updown-5m-{start}"
            if move >= thresh:                  # рывок вверх -> UP
                trader.signal("up", book.ask("up"), move, left, slug)
            elif move <= -thresh:               # рывок вниз -> DOWN
                trader.signal("down", book.ask("down"), move, left, slug)

        if time.time() - last_stat >= 15:
            last_stat = time.time()
            trader.summary()


async def main_async(args):
    fm.COIN = fm.COINS[args.coin]
    tasks = [
        combine(args, (book := BookState())),
        book_feed(book, args.coin, args.connections),
        *fm.feed_tasks(no_pyth=args.no_pyth, no_binance=args.no_binance,
                       no_okx=args.no_okx, no_bybit=args.no_bybit,
                       no_pm=args.no_polymarket, dupe=args.dupe),
    ]
    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(
        description="Momentum Bot: резкий рывок BTC -> вход в сторону "
                    "движения, держим пока проценты догонят (бумажный)")
    ap.add_argument("--coin", choices=sorted(fm.COINS), default="btc")
    ap.add_argument("--stake", type=float, default=1.0,
                    help="ставка в $ на сделку (по умолч. 1)")
    ap.add_argument("--move-window-ms", type=float, default=800.0,
                    help="за сколько мс мерить рывок цены (по умолч. 800)")
    ap.add_argument("--move-sigma", type=float, default=2.0,
                    help="во сколько σ должен быть рывок, чтобы считаться "
                         "резким (по умолч. 2; меньше = чаще входы)")
    ap.add_argument("--move-usd", type=float, default=None,
                    help="фикс. порог рывка в $ вместо авто-σ")
    ap.add_argument("--trail-cents", type=float, default=2.0,
                    help="ГЛАВНЫЙ выход: фиксировать, когда цена стороны "
                         "откатилась от пика на столько ¢ = рост процентов "
                         "остановился (по умолч. 2)")
    ap.add_argument("--take-profit-cents", type=float, default=20.0,
                    help="необязательный потолок прибыли, ¢ (по умолч. 20)")
    ap.add_argument("--stop-cents", type=float, default=6.0,
                    help="жёсткий стоп, если против нас на столько ¢")
    ap.add_argument("--chase-cents", type=float, default=2.0,
                    help="ПРЕДОСТОРОЖНОСТЬ 1: догнать ушедшую заявку не "
                         "дороже чем на столько ¢ (напр. было 56 -> взять "
                         "57-58; по умолч. 2)")
    ap.add_argument("--reverse-stake", type=float, default=3.5,
                    help="ПРЕДОСТОРОЖНОСТЬ 2: на сколько $ брать "
                         "противоположную сторону при развороте (по умолч. 3.5)")
    ap.add_argument("--reverse-usd", type=float, default=8.0,
                    help="насколько сильно (в $) цена должна пойти ПРОТИВ, "
                         "чтобы сработал разворотный хедж (по умолч. 8)")
    ap.add_argument("--max-entry", type=float, default=0.95,
                    help="не входить, если ask стороны уже выше (нет роста)")
    ap.add_argument("--min-entry", type=float, default=0.10,
                    help="не входить ниже этой цены (0.01 = крайний/протухший "
                         "токен на стыке окна, не реальный вход)")
    ap.add_argument("--order-latency-ms", type=float, default=200.0,
                    help="ТВОЯ задержка ордера: дом ~150-250, VPS ~20-50")
    ap.add_argument("--max-hold-ms", type=float, default=60000.0,
                    help="макс. удержание позиции, мс (по умолч. 60с)")
    ap.add_argument("--warmup-seconds", type=float, default=25.0)
    ap.add_argument("--settle-before", type=float, default=3.0)
    ap.add_argument("--round-minutes", type=int, default=5)
    ap.add_argument("--vol-window", type=float, default=120.0)
    ap.add_argument("--outlier-bps", type=float, default=25.0)
    ap.add_argument("--connections", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--dupe", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--log", default=None)
    ap.add_argument("--no-pyth", action="store_true")
    ap.add_argument("--no-binance", action="store_true")
    ap.add_argument("--no-okx", action="store_true")
    ap.add_argument("--no-bybit", action="store_true")
    ap.add_argument("--no-polymarket", action="store_true")
    args = ap.parse_args()

    if "--coin" not in sys.argv[1:] and sys.stdin is not None \
            and sys.stdin.isatty():
        order = list(fm.COINS)
        print("Какую монету торговать (моментум)?")
        for i, c in enumerate(order, 1):
            print(f"  {i}. {fm.COINS[c]['name']}")
        try:
            s = input("Номер (1-5) или имя: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nвыход")
        if s.isdigit() and 1 <= int(s) <= len(order):
            args.coin = order[int(s) - 1]
        elif s in fm.COINS:
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
