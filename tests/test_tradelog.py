"""Tests for the CSV trade logger."""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.tradelog import FIELDS, TradeLogger  # noqa: E402


def test_disabled_writes_nothing(tmp_path):
    tl = TradeLogger("")
    assert not tl.enabled
    tl.append({"slug": "x"})  # must not raise / create anything


def test_writes_header_and_rows(tmp_path):
    p = tmp_path / "trades.csv"
    tl = TradeLogger(str(p))
    tl.append({"slug": "btc-1", "outcome": "Up", "result": "WON", "pnl": 0.5})
    tl.append({"slug": "btc-2", "outcome": "Down", "result": "LOST", "pnl": -5.0})
    with open(p) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert set(rows[0].keys()) == set(FIELDS)
    assert rows[0]["slug"] == "btc-1" and rows[0]["result"] == "WON"
    assert rows[1]["pnl"] == "-5.0"


def test_reopen_appends_without_duplicate_header(tmp_path):
    p = tmp_path / "trades.csv"
    TradeLogger(str(p)).append({"slug": "a"})
    TradeLogger(str(p)).append({"slug": "b"})  # second instance, existing file
    with open(p) as f:
        rows = list(csv.DictReader(f))
    assert [r["slug"] for r in rows] == ["a", "b"]
