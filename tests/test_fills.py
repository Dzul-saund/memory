"""Разбор ответа биржи: исполнился ордер или нет.

В dry-run трейдер всегда отвечает «simulated_fill», поэтому движок привык
считать любой ордер исполненным целиком. В бою ордера уходят как FAK, и
несведённый остаток отменяется — филл может быть нулевым или частичным.
Записать ногу, которой нет, значит дальше продавать её и вести лестницу от
выдуманного долга.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.fills import FILLED, REJECTED, UNKNOWN, interpret  # noqa: E402


class TestRejected:
    """Явный отказ — позиции НЕТ, записывать нечего."""

    def test_error_message(self):
        f = interpret({"errorMsg": "not enough balance"}, 2.0)
        assert f.kind == REJECTED
        assert f.ok is False
        assert "not enough balance" in f.note

    def test_error_message_beats_success_flag(self):
        """Текст ошибки важнее, чем success=true рядом с ним."""
        f = interpret({"success": True, "errorMsg": "invalid order inputs"}, 2.0)
        assert f.kind == REJECTED

    def test_success_false(self):
        assert interpret({"success": False}, 2.0).kind == REJECTED

    def test_unmatched_status(self):
        """FAK не нашёл встречной ликвидности — ордер отменён."""
        assert interpret({"status": "unmatched"}, 2.0).kind == REJECTED
        assert interpret({"status": "CANCELED"}, 2.0).kind == REJECTED

    def test_zero_matched_size(self):
        f = interpret({"status": "matched", "sizeMatched": "0"}, 2.0)
        assert f.kind == REJECTED
        assert f.shares == 0.0


class TestFilled:
    def test_dry_run_response(self):
        f = interpret({"dry_run": True, "status": "simulated_fill"}, 1.89)
        assert f.kind == FILLED
        assert f.shares == 1.89

    def test_matched(self):
        f = interpret({"success": True, "status": "matched",
                       "orderID": "0xabc"}, 1.89)
        assert f.kind == FILLED
        assert f.shares == 1.89

    def test_full_size_reported(self):
        f = interpret({"status": "matched", "sizeMatched": "1.89"}, 1.89)
        assert f.kind == FILLED
        assert f.shares == 1.89
        assert f.note == ""

    def test_partial_size_reported(self):
        """Свелась половина — владеем половиной, а не тем, что просили."""
        f = interpret({"status": "matched", "sizeMatched": "0.90"}, 1.89)
        assert f.kind == FILLED
        assert f.shares == 0.90
        assert "частичный" in f.note


class TestUnknown:
    """Схему ответа CLOB мы не проверяли — непонятное честно помечаем."""

    def test_empty(self):
        f = interpret(None, 2.0)
        assert f.kind == UNKNOWN
        assert f.ok is True          # не блокируем торговлю, но предупреждаем

    def test_unrecognised_dict(self):
        assert interpret({"foo": "bar"}, 2.0).kind == UNKNOWN

    def test_object_with_attributes(self):
        class Resp:
            def __init__(self):
                self.status = "matched"

        assert interpret(Resp(), 2.0).kind == FILLED

    def test_garbage_is_not_a_crash(self):
        assert interpret(12345, 2.0).kind == UNKNOWN
        assert interpret("ok", 2.0).kind == UNKNOWN
