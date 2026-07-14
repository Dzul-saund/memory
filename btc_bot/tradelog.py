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
    "result",             # WON / LOST / UNSETTLED
    "settle_price",       # last observed price of the bought side
    "payout",
    "pnl",
    "cumulative_pnl",
    "balance_after",
]


class TradeLogger:
    def __init__(self, path: Optional[str]):
        self.path = path or ""
        self.enabled = bool(self.path)
        self._lock = threading.Lock()
        if self.enabled and not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def append(self, row: dict) -> None:
        if not self.enabled:
            return
        clean = {k: row.get(k, "") for k in FIELDS}
        with self._lock, open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(clean)
