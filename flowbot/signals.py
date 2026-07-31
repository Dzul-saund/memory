"""Сырые рыночные данные: цена монеты и книга заявок Up/Down.

ЭТО ИНФРАСТРУКТУРА, А НЕ СТРАТЕГИЯ. Здесь нет производных величин — ни
волатильности, ни скоростей, ни всплесков, ни имбалансов, ни расстояний от
экстремума. Всё это было частью прежней стратегии и удалено вместе с ней.

Остались два источника и доступ к их состоянию:

  PriceEngine — консенсусная цена монеты поверх `fast_monitor` (7 бирж плюс
  якорь Polymarket). Отдаёт текущую цену и короткую историю тиков.

  BookEngine — локальные книги Up/Down, наполняемые инкрементально из потока
  CLOB. Отдаёт лучшие bid/ask и глубину по уровням.

Если новой стратегии нужна волатильность, скорость или структура книги —
она считает их сама из этих данных и хранит у себя. Так производные
величины остаются там же, где пороги, которые их читают, и не превращаются
в общий словарь непонятного происхождения.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional, Tuple

import fast_monitor
from book_monitor import Book

# Сколько секунд сырой истории цены держать. Это не окно стратегии, а буфер:
# столько, чтобы новой логике было из чего считать свои величины, не заводя
# собственный фид.
HISTORY_S = 120.0


class PriceEngine:
    """Живая цена монеты и её недавняя история."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.cons = fast_monitor.Consensus()
        # (t_s, price) — сырые тики консенсуса.
        self.hist: Deque[Tuple[float, float]] = deque()
        self._price: Optional[float] = None

    def update(self, t_ms: float) -> Optional[float]:
        """Пересчитать консенсус и запомнить тик. Возвращает текущую цену.

        `Consensus.compute` отдаёт тройку (цена, принятые, отвергнутые) —
        распаковывать обязательно, иначе в цену уедет кортеж и это всплывёт
        только при форматировании лога.
        """
        px, _accepted, _rejected = self.cons.compute(t_ms)
        if px is None:
            return self._price
        self._price = px
        t_s = t_ms / 1000.0
        self.hist.append((t_s, px))
        while self.hist and t_s - self.hist[0][0] > HISTORY_S:
            self.hist.popleft()
        return px

    def price(self) -> Optional[float]:
        return self._price

    def history(self, seconds: Optional[float] = None):
        """Список (t, цена) за последние N секунд (по умолчанию — весь буфер).

        Копия, а не внутренняя очередь: стратегия не должна иметь возможности
        испортить историю движка.
        """
        if not self.hist:
            return []
        if seconds is None:
            return list(self.hist)
        edge = self.hist[-1][0] - seconds
        return [(t, p) for t, p in self.hist if t >= edge]

    def reset(self) -> None:
        """Забыть историю (граница раунда)."""
        self.hist.clear()


class BookEngine:
    """Локальные книги Up/Down из потока CLOB."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        self.books: Dict[str, Book] = {}

    def set_tokens(self, up_token: str, down_token: str) -> None:
        self.up_token, self.down_token = str(up_token), str(down_token)
        self.books = {self.up_token: Book(), self.down_token: Book()}

    # -- наполнение из CLOB-потока -------------------------------------------
    def on_event(self, ev: dict, recv_s: Optional[float] = None) -> bool:
        """Применить одно событие потока. True, если изменился топ книги."""
        etype = ev.get("event_type") or ev.get("type")
        changed = False
        if etype == "book":
            book = self.books.get(str(ev.get("asset_id")))
            if book is not None:
                book.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                    ev.get("asks") or ev.get("sells"))
                changed = True
        elif etype == "price_change":
            for ch in ev.get("changes") or []:
                if self._apply_change(str(ev.get("asset_id")), ch):
                    changed = True
            for ch in ev.get("price_changes") or []:
                if self._apply_change(str(ch.get("asset_id")), ch):
                    changed = True
        return changed

    def _apply_change(self, tok: str, ch: dict) -> bool:
        book = self.books.get(tok)
        if book is None or not book.have_snapshot:
            return False
        return book.apply_change(ch.get("side"), ch.get("price"),
                                 ch.get("size"))

    # -- чтение ---------------------------------------------------------------
    def best(self, token: Optional[str]) -> Tuple[Optional[float],
                                                  Optional[float]]:
        book = self.books.get(str(token)) if token else None
        if book is None:
            return None, None
        return book.best_bid(), book.best_ask()

    def depth(self, token: Optional[str], levels: int = 5):
        """Топ-N уровней с обеих сторон: (bids, asks), каждый [(цена, объём)].

        Пустые списки, если книги ещё нет — вызывающему не нужно отличать
        «нет книги» от «нет заявок», для обоих случаев ответ одинаков.
        """
        book = self.books.get(str(token)) if token else None
        if book is None or not book.have_snapshot:
            return [], []
        return book.top("bid", levels), book.top("ask", levels)

    def book(self, token: Optional[str]) -> Optional[Book]:
        """Сама книга — если новой стратегии нужен полный доступ к уровням."""
        return self.books.get(str(token)) if token else None
