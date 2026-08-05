"""Append-only CSV log of trades and their settled outcomes.

One row is written per position when it settles (and one final row for an
unsettled position at shutdown). The file is safe to open in Excel/Sheets and
is the raw material for judging the strategy over many windows.
"""
from __future__ import annotations

import csv
import os
import threading
from typing import Optional

FIELDS = [
    "settled_at_utc",
    "slug",
    "outcome",            # Up / Down
    "entry_price",
    "shares",
    "cost",
    "target_open",        # approx BTC strike at window open
    "btc_at_entry",
    "secs_to_end_at_entry",
    "model_prob_at_entry",  # model's own P(bought side) at entry, if available
    "result",             # WON / LOST / UNSETTLED / STOPLOSS
    "settle_price",       # last observed price of the bought side
    "payout",
    "pnl",
    "cumulative_pnl",
    "balance_after",
]


class TradeLogger:
    """Пишет строго в UTF-8, без BOM.

    Без явной кодировки Python берёт локальную (на русской Windows — cp1251),
    и любой не-ASCII текст потом читается как «????????». BOM намеренно НЕ
    ставим: он превратил бы имя первой колонки в «﻿settled_at_utc» для
    обычного csv.DictReader. Значения во всех колонках держим ASCII (коды
    WON/LOST/SOLD_TP, а не русские слова), поэтому Excel открывает файл
    правильно и без BOM.
    """

    def __init__(self, path: Optional[str]):
        self.path = path or ""
        self.enabled = bool(self.path)
        self._lock = threading.Lock()
        if self.enabled and not os.path.exists(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def append(self, row: dict) -> None:
        if not self.enabled:
            return
        clean = {k: row.get(k, "") for k in FIELDS}
        with self._lock, open(self.path, "a", newline="",
                              encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(clean)
