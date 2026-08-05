"""Разбор ответа биржи на ордер: исполнился он или нет.

Зачем отдельный модуль. В dry-run трейдер всегда возвращает
`{"status": "simulated_fill"}`, поэтому движок привык считать, что ордер
исполнился ровно на запрошенный объём по запрошенной цене. В бою это
неверно: ордера уходят как FAK (fill-and-kill) — то, что не свелось с
книгой немедленно, отменяется. Ликвидность могла уйти за время пинга, и
тогда мы получаем нулевой или частичный филл.

Если этого не заметить, бот считает, что владеет позицией, которой нет:
дальше он «продаёт» её, вычисляет P&L по несуществующим шэрам и ведёт
лестницу от неверного долга. Расхождение с реальностью растёт молча.

ЧЕСТНАЯ ОГОВОРКА. Точная схема ответа CLOB здесь не проверялась — она
зависит от версии клиента и может меняться. Поэтому разбор построен
консервативно: уверенно распознаём ЯВНЫЙ отказ, уверенно распознаём явный
успех, а всё остальное помечаем как UNKNOWN и оставляем решение вызывающему.
Лучше сказать «не знаю» и дать человеку посмотреть на сырой ответ, чем
угадать и тихо разойтись с биржей.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

FILLED = "filled"       # биржа подтвердила исполнение
REJECTED = "rejected"   # биржа явно отказала — позиции НЕТ
UNKNOWN = "unknown"     # разобрать не удалось; считаем исполненным, но кричим

# Значения поля status, по которым видно, что свести не удалось.
_BAD_STATUS = {"unmatched", "cancelled", "canceled", "rejected", "failed",
               "expired"}
# ... и по которым видно, что свелось.
_GOOD_STATUS = {"matched", "filled", "success", "live", "delayed",
                "simulated_fill", "simulated_sell"}


@dataclass
class Fill:
    """Что биржа сделала с нашим ордером."""
    kind: str                          # FILLED | REJECTED | UNKNOWN
    shares: Optional[float] = None     # сколько реально исполнено, если известно
    note: str = ""                     # человекочитаемая причина

    @property
    def ok(self) -> bool:
        """Можно ли считать, что позиция у нас есть."""
        return self.kind != REJECTED


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # отсеиваем NaN


def interpret(resp: Any, requested: float) -> Fill:
    """Ответ биржи + запрошенный объём -> что реально произошло.

    `requested` нужен, чтобы отличить полный филл от частичного, когда объём
    в ответе всё-таки есть.
    """
    if resp is None:
        return Fill(UNKNOWN, None, "пустой ответ биржи")

    if not isinstance(resp, dict):
        # Некоторые версии клиента возвращают объект, а не словарь.
        d = getattr(resp, "__dict__", None)
        if not isinstance(d, dict):
            return Fill(UNKNOWN, None, f"ответ нераспознанного вида: {resp!r}")
        resp = d

    # 1. Явный отказ. Проверяем ПЕРВЫМ: сообщение об ошибке важнее,
    #    чем success=true рядом с ним.
    err = resp.get("errorMsg") or resp.get("error") or resp.get("message")
    if isinstance(err, str) and err.strip():
        return Fill(REJECTED, 0.0, err.strip())

    if resp.get("success") is False:
        return Fill(REJECTED, 0.0, "success=false без текста ошибки")

    status = str(resp.get("status", "")).strip().lower()
    if status in _BAD_STATUS:
        return Fill(REJECTED, 0.0, f"status={status}")

    # 2. Сколько реально исполнено, если биржа сказала.
    #    sizeMatched — прямой ответ; makingAmount/takingAmount — суммы сторон.
    for key in ("sizeMatched", "size_matched", "filledSize", "matchedAmount"):
        got = _num(resp.get(key))
        if got is not None:
            if got <= 0:
                return Fill(REJECTED, 0.0, f"{key}=0 — ничего не свелось")
            if got < requested - 1e-9:
                return Fill(FILLED, got,
                            f"частичный филл: {got:.2f} из {requested:.2f}")
            return Fill(FILLED, got, "")

    # 3. Явный успех без объёма — считаем полным.
    if status in _GOOD_STATUS or resp.get("success") is True:
        return Fill(FILLED, requested, "")

    # 4. Ничего не поняли.
    return Fill(UNKNOWN, None, f"неизвестный ответ: {resp!r}")
