#!/usr/bin/env python3
"""
Edge Bot — объединяет ДВА твоих бота и измеряет, есть ли в связке деньги.

Идея (ровно то, что обсуждали):
  * бот ЦЕНЫ (fast_monitor)  -> справедливая цена контракта:
        P(UP) = Phi(отрыв / (sigma*sqrt(t)))  — сколько UP/DOWN ДОЛЖНЫ стоить;
  * бот СТАКАНА (book_monitor) -> сколько UP/DOWN стоят СЕЙЧАС (лучший ask/bid)
        и как книга меняется в реальном времени.

Edge Bot держит оба потока в одном процессе, каждый тик сравнивает
справедливую цену со стаканом и, когда стакан ОТСТАЛ достаточно, чтобы
перекрыть издержки, — открывает БУМАЖНУЮ позицию и ведёт её до фиксации.
Реальные ордера НЕ отправляются: это измеритель. Он пишет CSV, по которому
через неделю-две видно честно — есть в этой гонке заработок из твоей точки
или нет.

ЧЕСТНОЕ МОДЕЛИРОВАНИЕ ГОНКИ (самое важное).
Главный враг стратегии — «проклятие победителя»: пока твой ордер летит
(~задержка сети), маркет-мейкер успевает снять отставшую заявку. Бот это
НЕ игнорирует, а измеряет по реальным данным стакана:
  1. сигнал в момент T (справедливая 0.75, в стакане ask 0.61);
  2. фактический «фил» проверяется в момент T + --order-latency-ms:
     если по нашей цене на той стороне ещё есть объём — ФИЛ;
     если ММ уже убрал/подвинул уровень — ПРОМАХ (гонку выиграл он).
Поставь --order-latency-ms равным своей реальной задержке ордера (из дома
~150-250мс; на VPS в us-east ~20-50мс) — и бумажные результаты станут
предсказанием боевых.

Скорость сохранена/улучшена: событийная реакция (просыпается в момент
обновления цены ИЛИ стакана), stream без опроса, compression=None, orjson
если установлен, async-for без оверхеда на сообщение.

Оба исходных бота импортируются как есть и НЕ изменяются.

Запуск:
    pip install websockets      (orjson по желанию — быстрее)
    python edge_bot.py --coin btc
    python edge_bot.py --coin btc --order-latency-ms 200 --edge-cents 4
(или двойной клик — спросит монету)
"""

import argparse
import asyncio
import csv
import os
import sys
import time
from collections import deque
from datetime import datetime


def _fatal(msg: str):
    """Показать понятную ошибку и НЕ закрывать окно (для двойного клика)."""
    print("\n" + "=" * 64)
    print("EDGE BOT НЕ ЗАПУСТИЛСЯ:")
    print(msg)
    print("=" * 64)
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажми Enter, чтобы закрыть окно...")
    except Exception:
        pass
    raise SystemExit(1)


# --- твои два бота: импортируем, НЕ меняем ------------------------------------
# edge_bot ДОЛЖЕН лежать в ОДНОЙ ПАПКЕ с fast_monitor.py и book_monitor.py.
# Импортируем из папки самого скрипта, чтобы двойной клик тоже работал.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fast_monitor as fm   # бот цены (Consensus, VolEstimator, p_up, фиды)
except Exception as e:          # noqa: BLE001
    _fatal("не найден бот ЦЕНЫ 'fast_monitor.py'.\n"
           "Положи edge_bot.py в ту же папку, где лежит fast_monitor.py "
           "(это монитор цены на 5 монет, НЕ v6-turbo).\n"
           f"Причина: {e}")
try:
    import book_monitor as bm   # бот стакана (Book, discover_market, CLOB_WS)
except Exception as e:          # noqa: BLE001
    _fatal("не найден бот СТАКАНА 'book_monitor.py'.\n"
           "Положи edge_bot.py в ту же папку, где лежит book_monitor.py.\n"
           f"Причина: {e}")

# Проверка, что это ПРАВИЛЬНЫЙ fast_monitor (v9, 5 монет), а не v6-turbo:
# нужны official_target_task и ROUND_TARGET, которых в турбо-версии нет.
for _need in ("Consensus", "VolEstimator", "p_up", "feed_tasks",
              "official_target_task", "ROUND_TARGET", "COINS",
              "seconds_left_in_round"):
    if not hasattr(fm, _need):
        _fatal("рядом лежит НЕ тот монитор цены.\n"
               "Нужен fast_monitor.py на 5 монет (btc/eth/sol/xrp/doge) — "
               "тот, что с автоцелью с Polymarket.\n"
               f"В нём не хватает '{_need}'. Возьми fast_monitor.py из "
               "архива memorybot_9_0_max.zip.")

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
import threading
_PRINT_Q: "_queue.Queue[str]" = _queue.Queue()
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
        threading.Thread(target=_print_worker, daemon=True).start()
    _PRINT_Q.put(line)


# ------------------------- живой стакан обоих токенов -------------------------

class BookState:
    """Стакан UP и DOWN текущего окна + событие на каждое изменение.

    Использует класс Book из твоего book_monitor (та же инкрементальная
    логика, та же скорость), только без печати — чистые данные для расчёта.
    """

    def __init__(self):
        self.books = {}          # token -> bm.Book
        self.tokens = {"up": None, "down": None}
        self.event = threading.Event()
        self._assets_version = 0

    def set_market(self, up_token: str, down_token: str):
        self.tokens = {"up": up_token, "down": down_token}
        self.books = {up_token: bm.Book(), down_token: bm.Book()}
        self._assets_version += 1

    def ask(self, side: str):
        tok = self.tokens[side]
        b = self.books.get(tok) if tok else None
        return b.best_ask() if b else None

    def bid(self, side: str):
        tok = self.tokens[side]
        b = self.books.get(tok) if tok else None
        return b.best_bid() if b else None

    def ask_size(self, side: str):
        tok = self.tokens[side]
        b = self.books.get(tok) if tok else None
        if not b:
            return None
        a = b.best_ask()
        return b.asks.get(a) if a is not None else None

    def apply(self, ev: dict):
        etype = ev.get("event_type") or ev.get("type")
        if etype == "book":
            book = self.books.get(str(ev.get("asset_id")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                    ev.get("asks") or ev.get("sells"))
                self.event.set()
        elif etype == "price_change":
            touched = False
            for ch in ev.get("changes") or []:
                book = self.books.get(str(ev.get("asset_id")))
                if book is not None and book.have_snapshot:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))
                    touched = True
            for ch in ev.get("price_changes") or []:
                book = self.books.get(str(ch.get("asset_id")))
                if book is not None and book.have_snapshot:
                    book.apply_change(ch.get("side"), ch.get("price"),
                                      ch.get("size"))
                    touched = True
            if touched:
                self.event.set()


async def book_feed(book: BookState, coin: str, connections: int):
    """Поток стакана текущего 5-мин окна. Импортирует discover_market и
    CLOB_WS из book_monitor. При смене окна — новые токены и переподписка."""
    while True:
        market = await bm.discover_market(coin)
        book.set_market(market["up"], market["down"])
        end_ts = market["window_ts"] + bm.WINDOW_SECONDS
        out(f"── окно {market['slug']} до "
            f"{datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')} | "
            f"UP={market['up'][:10]}… DOWN={market['down'][:10]}… ──")
        sub = fm.json.dumps({"assets_ids": [market["up"], market["down"]],
                             "type": "market"})
        seen = set()
        seen_q = deque(maxlen=8192)

        async def conn(idx):
            backoff = 0.5
            while time.time() < end_ts + 2:
                try:
                    async with websockets.connect(
                            bm.CLOB_WS, compression=None, open_timeout=8,
                            user_agent_header="Mozilla/5.0") as ws:
                        await ws.send(sub)

                        async def _ping():
                            while True:
                                await asyncio.sleep(10)
                                await ws.send("PING")

                        async def _close():
                            await asyncio.sleep(max(0.0, end_ts + 2 - time.time()))
                            await ws.close()

                        aux = (asyncio.create_task(_ping()),
                               asyncio.create_task(_close()))
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

        await asyncio.gather(*(conn(i) for i in range(connections)))


# ------------------------- бумажная торговля -------------------------

class PaperTrader:
    """Ведёт бумажные сделки и пишет CSV. Реальные ордера НЕ отправляются."""

    FIELDS = ["signal_utc", "slug", "side", "secs_left",
              "fair_signal", "ask_signal", "edge_signal_c",
              "filled", "fill_price", "fair_at_fill",
              "exit_reason", "exit_price", "pnl_c", "cum_pnl_c"]

    def __init__(self, args):
        self.args = args
        self.pending = None      # ждём проверку фила
        self.pos = None          # открытая бумажная позиция
        self.cum_pnl_c = 0.0     # накопленный P&L в центах на 1 контракт
        self.n_signals = 0
        self.n_fills = 0
        self.n_miss = 0
        self.wins = 0
        self.losses = 0
        # История края по сторонам — чтобы отличить СВЕЖИЙ край (заявка
        # отстала от рывка) от давнего (модель просто спорит с рынком).
        self.edge_hist = deque()   # (t_ms, edge_up_c, edge_dn_c)
        self.path = args.log or f"edge_{args.coin}_paper.csv"
        if self.path and not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def _log(self, row):
        if not self.path:
            return
        clean = {k: row.get(k, "") for k in self.FIELDS}
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(clean)

    def record_edges(self, t_ms, edge_up_c, edge_dn_c):
        """Копим край по сторонам для проверки «свежести»."""
        self.edge_hist.append((t_ms, edge_up_c, edge_dn_c))
        horizon = self.args.fresh_window_ms + 500
        while self.edge_hist and t_ms - self.edge_hist[0][0] > horizon:
            self.edge_hist.popleft()

    def _is_fresh(self, side, t_ms):
        """Край СВЕЖИЙ, если ещё fresh_window_ms назад он был мал (заявка
        только что отстала от рывка). Давний устойчивый край = спор модели
        с рынком — такой не берём (там наша модель обычно и ошибается)."""
        idx = 1 if side == "up" else 2
        cutoff = t_ms - self.args.fresh_window_ms
        old = [e[idx] for e in self.edge_hist if e[0] <= cutoff]
        if not old:
            return False    # нет истории достаточной давности — не спешим
        return max(old) < self.args.fresh_max_cents

    def maybe_signal(self, side, fair, ask, secs_left, slug, t_ms):
        """Сигнал: справедливая цена стороны минус её ask >= порога,
        И этот край СВЕЖИЙ (только что открылся, а не висит давно)."""
        if self.pending is not None or self.pos is not None:
            return
        if ask is None or fair is None:
            return
        edge_c = (fair - ask) * 100.0
        if edge_c < self.args.edge_cents:
            return
        if not self._is_fresh(side, t_ms):
            return          # давний край = модель спорит с рынком, пропуск
        self.n_signals += 1
        self.pending = {
            "side": side, "fair_signal": fair, "ask_signal": ask,
            "edge_c": edge_c, "secs_left": secs_left, "slug": slug,
            "signal_utc": utc_str(),
            "check_at": now_ms() + self.args.order_latency_ms,
        }
        out(f"{ts_str()} СИГНАЛ {side.upper():4} fair {fair:.3f} vs "
            f"ask {ask:.3f}  край {edge_c:+.1f}¢  (проверю фил через "
            f"{self.args.order_latency_ms:.0f}мс)")

    def check_fill(self, book, fair_now):
        """Через задержку: выжила ли отставшая заявка? (гонка с ММ)."""
        p = self.pending
        if p is None or now_ms() < p["check_at"]:
            return
        self.pending = None
        side = p["side"]
        ask_now = book.ask(side)
        # ФИЛ, только если по нашей цене (или лучше) ещё есть объём
        if ask_now is not None and ask_now <= p["ask_signal"] + 1e-9:
            fill = ask_now
            self.n_fills += 1
            self.pos = {
                "side": side, "entry": fill, "fair_entry": fair_now,
                "fair_signal": p["fair_signal"], "ask_signal": p["ask_signal"],
                "edge_c": p["edge_c"], "secs_left": p["secs_left"],
                "slug": p["slug"], "signal_utc": p["signal_utc"],
                "fair_at_fill": fair_now,
            }
            realized_edge = (fair_now - fill) * 100.0
            out(f"{ts_str()} ФИЛ    {side.upper():4} по {fill:.3f} | "
                f"справедл. сейчас {fair_now:.3f} -> реальный край "
                f"{realized_edge:+.1f}¢ "
                f"(было {p['edge_c']:+.1f}¢ в сигнале)")
        else:
            self.n_miss += 1
            out(f"{ts_str()} ПРОМАХ {side.upper():4} заявка "
                f"{p['ask_signal']:.3f} снята за {self.args.order_latency_ms:.0f}"
                f"мс — гонку выиграл маркет-мейкер")
            self._log({**{k: p.get(k) for k in
                          ("signal_utc", "slug", "side", "secs_left")},
                       "fair_signal": round(p["fair_signal"], 4),
                       "ask_signal": round(p["ask_signal"], 4),
                       "edge_signal_c": round(p["edge_c"], 1),
                       "filled": 0, "cum_pnl_c": round(self.cum_pnl_c, 1)})

    def manage(self, book, fair_now, secs_left, final_settle=None):
        """Ведём открытую позицию: фиксация прибыли / стоп / экспирация."""
        pos = self.pos
        if pos is None:
            return
        side = pos["side"]

        # экспирация окна: расчёт по знаку (приближённо, как исход раунда)
        if final_settle is not None:
            payout = 1.0 if final_settle == side else 0.0
            self._close(pos, payout, "экспирация")
            return

        bid = book.bid(side)     # по этой цене мы могли бы ПРОДАТЬ
        # 1) фиксация прибыли: рынок догнал справедливую — край съеден
        if bid is not None and (fair_now - bid) * 100.0 <= self.args.exit_cents:
            self._close(pos, bid, "профит: рынок догнал")
            return
        # 2) стоп: справедливая ушла против нас сильнее порога
        if (pos["fair_entry"] - fair_now) * 100.0 >= self.args.stop_cents:
            if bid is not None:
                self._close(pos, bid, "стоп: справедл. развернулась")
            return

    def _close(self, pos, exit_price, reason):
        self.pos = None
        pnl_c = (exit_price - pos["entry"]) * 100.0
        self.cum_pnl_c += pnl_c
        if pnl_c >= 0:
            self.wins += 1
        else:
            self.losses += 1
        out(f"{ts_str()} ЗАКРЫЛ {pos['side'].upper():4} {pos['entry']:.3f}"
            f"->{exit_price:.3f}  P&L {pnl_c:+.1f}¢  [{reason}]  "
            f"итого {self.cum_pnl_c:+.1f}¢ (W/L {self.wins}/{self.losses})")
        self._log({
            "signal_utc": pos["signal_utc"], "slug": pos["slug"],
            "side": pos["side"], "secs_left": pos["secs_left"],
            "fair_signal": round(pos["fair_signal"], 4),
            "ask_signal": round(pos["ask_signal"], 4),
            "edge_signal_c": round(pos["edge_c"], 1),
            "filled": 1, "fill_price": round(pos["entry"], 4),
            "fair_at_fill": round(pos["fair_at_fill"], 4),
            "exit_reason": reason, "exit_price": round(exit_price, 4),
            "pnl_c": round(pnl_c, 1), "cum_pnl_c": round(self.cum_pnl_c, 1),
        })

    def summary(self):
        fr = (100.0 * self.n_fills / max(self.n_signals, 1))
        out(f"    ИТОГ: сигналов {self.n_signals}, филов {self.n_fills} "
            f"({fr:.0f}%), промахов {self.n_miss} | закрыто "
            f"{self.wins + self.losses} (W/L {self.wins}/{self.losses}) | "
            f"суммарно {self.cum_pnl_c:+.1f}¢ на 1 контракт | лог: {self.path}")


# ------------------------- главный цикл -------------------------

async def combine(args, book: BookState):
    await asyncio.sleep(2)
    cons = fm.Consensus(outlier_bps=args.outlier_bps,
                        offset_halflife_s=60.0)
    vol = fm.VolEstimator(window_s=args.vol_window, tick_s=0.25)
    trader = PaperTrader(args)
    period = args.round_minutes * 60
    last_vol = 0.0
    last_stat = time.time()
    prev_left = fm.seconds_left_in_round(args.round_minutes)
    started = time.time()
    loop = asyncio.get_running_loop()

    while True:
        # событийно: просыпаемся на обновление цены ИЛИ стакана (или 0.1с)
        book.event.clear()
        try:
            await asyncio.wait_for(fm.UPDATE_EVENT.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        fm.UPDATE_EVENT.clear()
        # добираем изменения стакана без ожидания
        await loop.run_in_executor(None, book.event.wait, 0.05)

        t = now_ms()
        price, accepted, _ = cons.compute(t)
        left = fm.seconds_left_in_round(args.round_minutes)

        # граница окна: досчитать экспирацию открытой позиции
        new_round = left > prev_left + 1
        prev_left = left

        if price is None:
            continue
        if t - last_vol >= 250:
            vol.add(price)
            last_vol = t
        sigma = vol.sigma_1s()

        # точная цель раунда — из твоего же fast_monitor (openPrice сайта)
        rt = fm.ROUND_TARGET
        now_s = time.time()
        start = int(now_s - now_s % period)
        target = rt["value"] if (rt["exact"] and rt["start_ts"] == start) else None
        if target is None or sigma is None:
            # без точной цели или прогретой σ справедливую не считаем
            trader.check_fill(book, price)   # висящие проверки не теряем
            continue

        diff = price - target
        p_up = fm.p_up(diff, sigma, left)
        if p_up is None:
            continue
        fair_up, fair_dn = p_up, 1.0 - p_up
        slug = f"{args.coin}-updown-5m-{start}"

        # экспирация: если окно кончилось и есть позиция — расчёт по знаку
        if left <= args.settle_before and trader.pos is not None:
            winner = "up" if diff >= 0 else "down"
            trader.manage(book, None, left, final_settle=winner)

        # 1) проверить висящий «фил» (гонка с ММ)
        fair_side_now = (fair_up if (trader.pending and
                         trader.pending["side"] == "up") else fair_dn)
        trader.check_fill(book, fair_side_now if trader.pending else price)

        # 2) вести открытую позицию
        if trader.pos is not None:
            side = trader.pos["side"]
            trader.manage(book, fair_up if side == "up" else fair_dn, left)

        # 3) искать новый сигнал (не входим в самом конце окна)
        ask_up, ask_dn = book.ask("up"), book.ask("down")
        e_up = (fair_up - ask_up) * 100 if ask_up is not None else -99
        e_dn = (fair_dn - ask_dn) * 100 if ask_dn is not None else -99
        trader.record_edges(t, e_up, e_dn)   # для проверки свежести

        warm = (time.time() - started) >= args.warmup_seconds
        books_ready = ask_up is not None and ask_dn is not None
        if warm and books_ready and left > args.settle_before:
            if e_up >= e_dn:
                trader.maybe_signal("up", fair_up, ask_up, left, slug, t)
            else:
                trader.maybe_signal("down", fair_dn, ask_dn, left, slug, t)

        if time.time() - last_stat >= 15:
            last_stat = time.time()
            trader.summary()


def choose_coin():
    order = list(fm.COINS)
    print("Какую монету связывать (цена + стакан)?")
    for i, c in enumerate(order, 1):
        print(f"  {i}. {fm.COINS[c]['name'] if isinstance(fm.COINS[c], dict) else fm.COINS[c]}")
    while True:
        try:
            s = input("Номер (1-5) или имя: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nвыход")
        if s.isdigit() and 1 <= int(s) <= len(order):
            return order[int(s) - 1]
        if s in fm.COINS:
            return s
        print("Не понял. Пример: 1  или  btc")


async def main_async(args):
    # выбрать монету в обоих ботах (fast_monitor держит COIN глобально)
    fm.COIN = fm.COINS[args.coin]
    dp = fm.COIN["dp"]
    book = BookState()
    tasks = [
        combine(args, book),
        book_feed(book, args.coin, args.connections),
        fm.official_target_task(args.round_minutes, dp),
        *fm.feed_tasks(no_pyth=args.no_pyth, no_binance=args.no_binance,
                       no_okx=args.no_okx, no_bybit=args.no_bybit),
    ]
    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(
        description="Edge Bot: связка цена+стакан, бумажное измерение края")
    ap.add_argument("--coin", choices=sorted(fm.COINS), default="btc")
    ap.add_argument("--edge-cents", type=float, default=4.0,
                    help="мин. край для входа, ¢ (справедл. − ask). Должен "
                         "перекрывать издержки прохода (~2-4¢). По умолч. 4")
    ap.add_argument("--exit-cents", type=float, default=1.0,
                    help="фиксировать, когда рынок догнал справедливую в "
                         "пределах этого, ¢ (по умолч. 1)")
    ap.add_argument("--stop-cents", type=float, default=8.0,
                    help="стоп: справедливая ушла против входа на столько ¢")
    ap.add_argument("--order-latency-ms", type=float, default=200.0,
                    help="ТВОЯ реальная задержка ордера: из дома ~150-250, "
                         "на VPS us-east ~20-50. Определяет проверку фила")
    ap.add_argument("--fresh-window-ms", type=float, default=1500.0,
                    help="край считается свежим, если ещё столько мс назад "
                         "он был мал (заявка только что отстала от рывка)")
    ap.add_argument("--fresh-max-cents", type=float, default=2.0,
                    help="каким был край до рывка, чтобы считать его свежим")
    ap.add_argument("--warmup-seconds", type=float, default=25.0,
                    help="не торговать первые N сек после старта (σ и "
                         "смещения источников прогреваются)")
    ap.add_argument("--settle-before", type=float, default=3.0,
                    help="не входить в последние N сек; там же экспирация")
    ap.add_argument("--round-minutes", type=int, default=5)
    ap.add_argument("--vol-window", type=float, default=120.0)
    ap.add_argument("--outlier-bps", type=float, default=25.0)
    ap.add_argument("--connections", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--log", default=None, help="путь к CSV (по умолч. "
                    "edge_<coin>_paper.csv)")
    ap.add_argument("--no-pyth", action="store_true")
    ap.add_argument("--no-binance", action="store_true")
    ap.add_argument("--no-okx", action="store_true")
    ap.add_argument("--no-bybit", action="store_true")
    args = ap.parse_args()

    if "--coin" not in sys.argv[1:] and sys.stdin is not None \
            and sys.stdin.isatty():
        args.coin = choose_coin()

    log_path = args.log or f"edge_{args.coin}_paper.csv"
    out("=== Edge Bot (БУМАЖНЫЙ, реальные ордера НЕ шлёт) ===")
    out(f"монета {fm.COINS[args.coin]['name']} | вход при СВЕЖЕМ крае "
        f">= {args.edge_cents:.0f}¢ | задержка ордера "
        f"{args.order_latency_ms:.0f}мс | прогрев {args.warmup_seconds:.0f}с "
        f"| лог: {log_path}")
    out("совет: поставь --order-latency-ms равным своей реальной задержке "
        "ордера, тогда бумажный итог = прогноз боевого")

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
