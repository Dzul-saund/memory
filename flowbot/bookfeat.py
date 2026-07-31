"""Признаки СТРУКТУРЫ книги заявок, а не только её верхней цены.

Стратегия до сих пор читала из книги ровно два числа — лучший бид и лучший
ask. Всё остальное решение строилось на цене монеты. Между тем книга часто
говорит, продолжится ли движение, раньше самой цены:

  * с одной стороны стоит втрое больше заявок, чем с другой (перекос);
  * ликвидность с пути движения ИСЧЕЗАЕТ — заявки снимают, а не выкупают;
  * встала крупная лимитка — стена, в которую движение упрётся.

Здесь эти величины СЧИТАЮТСЯ. Порогов и решений нет — они в стратегии, где
их можно перебрать через replay. Всё безразмерное: доли и отношения, а не
штуки и доллары, иначе пороги пришлось бы переподбирать под каждый рынок.

Считаемые величины (все по ОДНОМУ токену, то есть по одной стороне):

  imbalance   (bid_depth − ask_depth) / (bid_depth + ask_depth), [−1..+1].
              +1 — покупателей сильно больше, −1 — продавцов.
  ask_trend   относительное изменение глубины ask за окно. Отрицательное =
              ask-ликвидность уходит (путь наверх расчищается).
  bid_trend   то же для бидов. Отрицательное = опора уходит.
  wall        доля крупнейшего уровня в общей глубине топ-N, [0..1].
              Близко к 1 — вся ликвидность в одной заявке (стена или
              приманка, которую снимут).

ВАЖНО ПРО ЗНАК. Up и Down — одна книга с двух сторон (бид Up ≈ 1 − ask
Down), поэтому перекос по Up и перекос по Down почти зеркальны. Считаем по
токену той стороны, которую собираемся купить, и не пытаемся складывать их.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

_SAMPLE_S = 0.10          # чаще нет смысла: сглаживаем шум топовых уровней


@dataclass
class BookState:
    imbalance: float           # [−1..+1], + = перевес бидов
    ask_trend: float           # относительное изменение глубины ask за окно
    bid_trend: float
    wall: float                # доля крупнейшего уровня в топ-N, [0..1]
    bid_depth: float           # шэров в топ-N бидов
    ask_depth: float


def _depth(levels) -> float:
    return float(sum(sz for _p, sz in levels))


def _wall(levels, total: float) -> float:
    if total <= 0 or not levels:
        return 0.0
    return max(sz for _p, sz in levels) / total


class BookTracker:
    """Скользящая история глубины по одному токену. Без порогов, без I/O."""

    def __init__(self, levels: int = 5, window_s: float = 3.0):
        self.levels = int(levels)
        self.window_s = float(window_s)
        self._hist: Deque[Tuple[float, float, float]] = deque()  # t, bid, ask

    def feed(self, t_s: float, book) -> None:
        if book is None or not getattr(book, "have_snapshot", False):
            return
        h = self._hist
        if h and t_s - h[-1][0] < _SAMPLE_S:
            return
        bd = _depth(book.top("bid", self.levels))
        ad = _depth(book.top("ask", self.levels))
        h.append((t_s, bd, ad))
        while h and t_s - h[0][0] > self.window_s * 2:
            h.popleft()

    def reset(self) -> None:
        self._hist.clear()

    def state(self, book) -> Optional[BookState]:
        if book is None or not getattr(book, "have_snapshot", False):
            return None
        bids = book.top("bid", self.levels)
        asks = book.top("ask", self.levels)
        bd, ad = _depth(bids), _depth(asks)
        total = bd + ad
        if total <= 0:
            return None

        imb = (bd - ad) / total
        # Стена — по той стороне, где её ищут: крупная лимитка НА ПУТИ
        # движения стоит в asks (мы покупаем — упрёмся в неё).
        wall = _wall(asks, ad)

        # Тренды: сравниваем с самой старой точкой ВНУТРИ окна. Если истории
        # меньше окна, тренд честно равен 0 — «пока не знаю», а не «не
        # изменилось»: разница между этими ответами и есть половина смысла.
        bid_tr = ask_tr = 0.0
        if self._hist:
            t_now = self._hist[-1][0]
            old = None
            for t, b, a in self._hist:
                if t_now - t <= self.window_s:
                    old = (b, a)
                    break
            if old is not None and t_now - self._hist[0][0] >= self.window_s * 0.5:
                ob, oa = old
                if ob > 0:
                    bid_tr = (bd - ob) / ob
                if oa > 0:
                    ask_tr = (ad - oa) / oa

        return BookState(imbalance=imb, ask_trend=ask_tr, bid_trend=bid_tr,
                         wall=wall, bid_depth=bd, ask_depth=ad)


class BookFeatures:
    """Пара трекеров — по одному на каждую сторону рынка."""

    def __init__(self, levels: int = 5, window_s: float = 3.0):
        self.levels, self.window_s = int(levels), float(window_s)
        self._by_token: Dict[str, BookTracker] = {}

    def _t(self, token: str) -> BookTracker:
        tr = self._by_token.get(token)
        if tr is None:
            tr = BookTracker(self.levels, self.window_s)
            self._by_token[token] = tr
        return tr

    def feed(self, t_s: float, token: Optional[str], book) -> None:
        if token:
            self._t(str(token)).feed(t_s, book)

    def state(self, token: Optional[str], book) -> Optional[BookState]:
        if not token:
            return None
        return self._t(str(token)).state(book)

    def reset(self) -> None:
        for tr in self._by_token.values():
            tr.reset()
