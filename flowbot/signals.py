"""Сигнальные движки flowbot: цена (скорость) и книга (поток заявок).

PriceEngine — «Бот 1». Живёт поверх консенсуса fast_monitor (7 бирж + якорь
Polymarket) и даёт: живую цену, σ, наукаст и — главное — ВСПЛЕСК скорости,
нормированный на волатильность (burst_z). Именно он говорит «цена резко пошла».

BookEngine — «Бот 2». Держит локальные книги Up/Down (инкрементально, из
CLOB-потока самого сайта) и даёт: лучшие bid/ask по обеим сторонам, «процент»
каждой стороны, доскачковый ask (для догона), и имбаланс потока заявок/сделок
(куда толпа двигает цену).

Оба движка — чистые по вводу-выводу: PriceEngine читает уже готовое состояние
fast_monitor (которое наполняют его же async-фиды), BookEngine наполняется
событиями CLOB-потока, которые ему передаёт движок.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import fast_monitor
from book_monitor import Book


# ---------------------------------------------------------------------------
#  Цена: скорость / всплеск (Бот 1)
# ---------------------------------------------------------------------------
class PriceEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cons = fast_monitor.Consensus()
        self.vol = fast_monitor.VolEstimator(window_s=120.0, tick_s=0.25)
        self.nc = fast_monitor.Nowcast()
        # (t_s, price) на горизонте всплеска (чуть больше horizon).
        self.hist: Deque[Tuple[float, float]] = deque()
        self._last_vol_ms = 0.0
        self._price: Optional[float] = None
        self._sigma: Optional[float] = None
        # --- отслеживание движения от локального экстремума (swing) ---------
        # Три последние сырые цены: медиана из них гасит одиночный выброс
        # консенсуса, который иначе мгновенно «нарисовал» бы новый минимум
        # и дал ложный сигнал на скачок.
        self._sm3: Deque[float] = deque(maxlen=3)
        self._smooth: Optional[float] = None
        self._raw: Optional[float] = None
        # Монотонные очереди: голова = минимум/максимум окна. Скользящий
        # экстремум за O(1) на тик — без пересчёта всей истории, поэтому
        # можно звать на КАЖДОМ обновлении книги, а не по таймеру.
        self._lo: Deque[Tuple[float, float]] = deque()
        self._hi: Deque[Tuple[float, float]] = deque()

    def update(self, t_ms: float) -> Optional[float]:
        price, _accepted, _rejected = self.cons.compute(t_ms)
        if price is None:
            return None
        self._price = price
        if t_ms - self._last_vol_ms >= 250:      # σ на сетке 0.25с
            self.vol.add(price)
            self._last_vol_ms = t_ms
        self._sigma = self.vol.sigma_1s()
        t_s = t_ms / 1000.0
        self.nc.add(t_s, price)
        self.hist.append((t_s, price))
        # Истории держим столько, сколько нужно САМОМУ длинному горизонту:
        # burst() смотрит на momentum_horizon_s, скачковая стратегия — на
        # jump_window_s (обычно длиннее). Оба выбирают точки по времени, так
        # что лишняя история никому не мешает.
        keep = max(self.cfg.momentum_horizon_s * 1.5 + 0.5,
                   getattr(self.cfg, "jump_window_s", 0.0) + 1.0)
        while self.hist and t_s - self.hist[0][0] > keep:
            self.hist.popleft()
        self._update_swing(t_s, price)
        return price

    def _update_swing(self, t_s: float, price: float) -> None:
        # В экстремумы кладём медиану трёх последних цен: одиночный выброс
        # консенсуса так не станет «дном». А вот ТЕКУЩУЮ цену держим сырой —
        # медиана отстаёт на тик, а весь смысл режима в мгновенной реакции.
        self._raw = price
        self._sm3.append(price)
        p = statistics.median(self._sm3) if len(self._sm3) == 3 else price
        self._smooth = p
        horizon = getattr(self.cfg, "jump_swing_lookback_s", 60.0)
        lo, hi = self._lo, self._hi
        while lo and t_s - lo[0][0] > horizon:
            lo.popleft()
        while hi and t_s - hi[0][0] > horizon:
            hi.popleft()
        while lo and lo[-1][1] >= p:
            lo.pop()
        lo.append((t_s, p))
        while hi and hi[-1][1] <= p:
            hi.pop()
        hi.append((t_s, p))

    def swing(self) -> Tuple[float, float]:
        """(движение от локального экстремума в ДОЛЛАРАХ, оно же в б.п.).

        Отвечает на вопрос «на сколько цена уже ушла от своего недавнего дна
        или пика», а не «на сколько она изменилась за последние N секунд».
        Разница принципиальная: окно в N секунд ПРОПУСКАЕТ движение, которое
        заняло N+1 секунду, а экстремум ловит его целиком и срабатывает ровно
        на том тике, где движение перевалило порог — ждать нечего.

        Знак: + цена выросла от дна, − упала от пика. Берём то направление,
        где движение больше по модулю.

        Дно/пик — despiked (медиана трёх), текущая цена — сырая: реагируем в
        тот же тик. Защита от одиночного выброса в САМОЙ свежей цене — это
        отсев выбросов в консенсусе fast_monitor (источник, ушедший от медианы
        семи бирж дальше 25 б.п., в цену вообще не попадает).
        """
        p = self._raw
        if p is None or not self._lo or not self._hi:
            return 0.0, 0.0
        up = p - self._lo[0][1]
        down = self._hi[0][1] - p
        move = up if up >= down else -down
        return move, (move / p * 1e4 if p > 0 else 0.0)

    def reset_swing(self) -> None:
        """Заново привязать экстремум к текущей цене.

        Зовётся после сделки и на границе раунда: движение уже отработано,
        и старое дно не должно секунду спустя открыть вторую такую же сделку.
        """
        self._lo.clear()
        self._hi.clear()
        self._sm3.clear()
        if self._price is not None:
            self._smooth = self._raw = self._price
            now = time.time()
            self._lo.append((now, self._price))
            self._hi.append((now, self._price))

    def price(self) -> Optional[float]:
        return self._price

    def sigma_1s(self) -> Optional[float]:
        return self._sigma

    def nowcast(self, lead: float = 0.4) -> Optional[float]:
        return self.nc.project(lead)

    def move_usd(self, horizon: float) -> Tuple[float, float]:
        """(Δцены в ДОЛЛАРАХ, Δ в б.п.) за horizon секунд. Знак: + вверх.

        Для скачковой стратегии, где порог задан прямо в долларах («скачок
        на 5$ / 15$»), а не в σ. Как и в burst(), концы окна берутся МЕДИАНОЙ
        по узким подокнам: один выбросной тик консенсуса (переподключение
        источника, дискретность якоря) иначе выглядел бы как скачок на
        десятки долларов и открывал бы сделку на пустом месте.

        Нет истории на горизонте -> (0, 0): «скачка не видно», а не «ошибка».
        """
        hist = self.hist
        if len(hist) < 3 or self._price is None or horizon <= 0:
            return 0.0, 0.0
        t_now = hist[-1][0]
        win = min(0.3, horizon / 3.0)      # ширина подокон на концах
        recent = [p for t, p in hist if t_now - t <= win]
        older = [p for t, p in hist
                 if horizon - win <= t_now - t <= horizon + win]
        if not recent or not older:
            return 0.0, 0.0
        p_now = statistics.median(recent)
        ref_p = statistics.median(older)
        if p_now <= 0:
            return 0.0, 0.0
        dprice = p_now - ref_p
        return dprice, dprice / p_now * 1e4

    def burst(self) -> Tuple[float, float]:
        """(burst_z, move_bps) — знаковый всплеск за горизонт.

        move = (медиана свежих цен) − (медиана цен ~horizon назад),
        нормированный на σ·√dt (это и есть z: во сколько σ уложилось движение).
        Плюс абсолютное движение в б.п. Знак: + вверх, − вниз.

        Медиана по окнам (а не разница крайних точек) ГАСИТ одиночные скачки
        консенсуса (подключение/обрыв источника, дискретность якоря) — иначе
        один выбросной тик давал бы гигантский ложный z. Нет истории/σ -> (0,0).
        """
        hist = self.hist
        if len(hist) < 3 or self._price is None:
            return 0.0, 0.0
        t_now = hist[-1][0]
        h = self.cfg.momentum_horizon_s
        rec_win = 0.3
        recent = [(t, p) for t, p in hist if t_now - t <= rec_win]
        older = [(t, p) for t, p in hist if h - rec_win <= t_now - t <= h + rec_win]
        if not recent or not older:
            return 0.0, 0.0
        p_now = statistics.median(p for _, p in recent)
        ref_p = statistics.median(p for _, p in older)
        t_now_eff = statistics.median(t for t, _ in recent)
        ref_t_eff = statistics.median(t for t, _ in older)
        dt = t_now_eff - ref_t_eff
        if dt <= 0 or p_now <= 0:
            return 0.0, 0.0
        dprice = p_now - ref_p
        move_bps = dprice / p_now * 1e4
        z = 0.0
        if self._sigma and self._sigma > 0:
            # Пол σ: не даём волатильности схлопнуться в ноль на прогреве,
            # иначе z взлетает и даёт ложный «резкий» сигнал.
            floor = p_now * self.cfg.min_sigma_bps / 1e4
            sigma = max(self._sigma, floor)
            z = dprice / (sigma * math.sqrt(dt))
        return z, move_bps


# ---------------------------------------------------------------------------
#  Книга: лучшие цены, доскачковый ask, поток заявок/сделок (Бот 2)
# ---------------------------------------------------------------------------
class BookEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        self.books: Dict[str, Book] = {}
        # (t_s, best_ask) для догона и импульса «процента».
        self.ask_hist: Dict[str, Deque[Tuple[float, float]]] = {}
        # (t_s, side, size) сделок для имбаланса потока.
        self.trades: Dict[str, Deque[Tuple[float, str, float]]] = {}

    def set_tokens(self, up_token: str, down_token: str) -> None:
        self.up_token, self.down_token = str(up_token), str(down_token)
        self.books = {self.up_token: Book(), self.down_token: Book()}
        self.ask_hist = {self.up_token: deque(), self.down_token: deque()}
        self.trades = {self.up_token: deque(), self.down_token: deque()}

    # -- наполнение из CLOB-потока -------------------------------------------
    def on_event(self, ev: dict, recv_s: Optional[float] = None) -> bool:
        """Применить одно событие потока. True, если изменился топ книги."""
        now = recv_s if recv_s is not None else time.time()
        etype = ev.get("event_type") or ev.get("type")
        changed = False
        if etype == "book":
            book = self.books.get(str(ev.get("asset_id")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                    ev.get("asks") or ev.get("sells"))
                self._record_ask(str(ev.get("asset_id")), book, now)
                changed = True
        elif etype == "price_change":
            for ch in ev.get("changes") or []:
                tok = str(ev.get("asset_id"))
                if self._apply_change(tok, ch, now):
                    changed = True
            for ch in ev.get("price_changes") or []:
                tok = str(ch.get("asset_id"))
                if self._apply_change(tok, ch, now):
                    changed = True
        elif etype == "last_trade_price":
            tok = str(ev.get("asset_id"))
            tr = self.trades.get(tok)
            if tr is not None:
                try:
                    size = float(ev.get("size") or 0.0)
                except (TypeError, ValueError):
                    size = 0.0
                side = str(ev.get("side", "")).upper() or "BUY"
                tr.append((now, side, size))
                self._trim(tr, now, self.cfg.flow_window_s * 2 + 1)
        return changed

    def _apply_change(self, tok: str, ch: dict, now: float) -> bool:
        book = self.books.get(tok)
        if book is None or not book.have_snapshot:
            return False
        top = book.apply_change(ch.get("side"), ch.get("price"), ch.get("size"))
        if top:
            self._record_ask(tok, book, now)
        return top

    def _record_ask(self, tok: str, book: Book, now: float) -> None:
        hist = self.ask_hist.get(tok)
        if hist is None:
            return
        ba = book.best_ask()
        if ba is not None:
            hist.append((now, ba))
            self._trim(hist, now, self.cfg.chase_lookback_s * 2 + 1)

    @staticmethod
    def _trim(dq: deque, now: float, horizon: float) -> None:
        while dq and now - dq[0][0] > horizon:
            dq.popleft()

    # -- чтение --------------------------------------------------------------
    def best(self, token: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
        book = self.books.get(str(token)) if token else None
        if book is None:
            return None, None
        return book.best_bid(), book.best_ask()

    def ask_ref(self, token: Optional[str], lookback: float) -> Optional[float]:
        """Лучший ask ~lookback секунд назад («до скачка»)."""
        hist = self.ask_hist.get(str(token)) if token else None
        if not hist:
            return None
        now = hist[-1][0]
        ref = hist[0][1]
        for t, a in hist:
            if now - t >= lookback:
                ref = a
            else:
                break
        return ref

    def depth(self, token: Optional[str]):
        """(объём на лучшем биде, на лучшем аске, уровней bid, уровней ask).

        Размер на топе — это то, обо что мы ударимся при входе, а число
        уровней грубо показывает, насколько книга «толстая». Восстановить
        это по цене задним числом нельзя, поэтому пишем в запись как есть.
        """
        book = self.books.get(str(token)) if token else None
        if book is None:
            return None, None, 0, 0
        bb, ba = book.best_bid(), book.best_ask()
        return (book.bids.get(bb) if bb is not None else None,
                book.asks.get(ba) if ba is not None else None,
                len(book.bids), len(book.asks))

    def trade_counts(self, token: Optional[str], window: float,
                     now: Optional[float] = None):
        """(объём BUY, объём SELL, число сделок) за окно — агрессия потока."""
        tr = self.trades.get(str(token)) if token else None
        if not tr:
            return 0.0, 0.0, 0
        now = now if now is not None else time.time()
        buy = sell = 0.0
        n = 0
        for t, side, size in tr:
            if now - t <= window:
                n += 1
                if side == "BUY":
                    buy += size
                else:
                    sell += size
        return buy, sell, n

    def flow(self, token: Optional[str], window: float,
             now: Optional[float] = None) -> float:
        """Имбаланс в [-1,1]: >0 = давят на покупку токена (бычий сигнал стороны).

        Сначала по потоку сделок за окно (BUY против SELL); если сделок нет —
        по объёму лучшего бида против лучшего аска (кто «толще»).
        """
        token = str(token) if token else None
        now = now if now is not None else time.time()
        tr = self.trades.get(token) if token else None
        if tr:
            buy = sell = 0.0
            for t, side, size in tr:
                if now - t <= window:
                    if side == "BUY":
                        buy += size
                    else:
                        sell += size
            if buy + sell > 0:
                return (buy - sell) / (buy + sell)
        book = self.books.get(token) if token else None
        if book is not None:
            bb, ba = book.best_bid(), book.best_ask()
            bs = book.bids.get(bb, 0.0) if bb is not None else 0.0
            as_ = book.asks.get(ba, 0.0) if ba is not None else 0.0
            if bs + as_ > 0:
                return (bs - as_) / (bs + as_)
        return 0.0
