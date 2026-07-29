"""Сверка позиций с биржей: бот обязан знать, чем владеет на самом деле.

Свой учёт — это память бота о его же действиях, и она расходится с биржей
при таймаутах, частичных филлах и неверно разобранных ответах. Расхождение
само не чинится и выглядит одинаково скверно: на Polymarket позиция висит и
теряет в цене, а бот показывает «поз —» и ничего не делает, потому что
управлять он может только тем, о чём знает.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.positions import reconcile, shares_from_balance  # noqa: E402
from test_jump_engine import FakeTrader, _book, _engine  # noqa: E402


class TestParsing:
    def test_raw_six_decimals(self):
        """Биржа отдаёт сырые единицы: 2 шэра = 2000000."""
        assert shares_from_balance({"balance": "2000000"}) == 2.0
        assert shares_from_balance({"balance": 1_500_000}) == 1.5

    def test_already_normalised_small_value(self):
        assert shares_from_balance({"balance": "0.5"}) == 0.5

    def test_unknown_is_none_not_zero(self):
        """None значит «не знаю». Ноль означал бы «позиций нет» и заставил
        бы бота выбросить настоящую ногу."""
        assert shares_from_balance(None) is None
        assert shares_from_balance({"что-то": "иное"}) is None
        assert shares_from_balance("мусор") is None
        assert shares_from_balance({"balance": "не-число"}) is None


class TestReconcile:
    def test_exchange_has_more_than_we_think(self):
        assert reconcile(0.0, 2.0) == 2.0

    def test_we_think_we_have_more(self):
        assert reconcile(2.0, 0.0) == -2.0

    def test_small_difference_is_ignored(self):
        assert reconcile(2.0, 2.005) == 0.0

    def test_nothing_to_compare_with(self):
        assert reconcile(2.0, None) is None


def _live_engine(**over):
    """Движок «как в бою», но без настоящего клиента биржи.

    Собираем в dry-run (иначе понадобился бы приватный ключ), затем снимаем
    флаг и подменяем трейдера — сверка идёт именно по боевой ветке.
    """
    eng, cfg = _engine(**over)
    cfg.dry_run = False
    eng.trader = FakeTrader()
    return eng, cfg


class TestEngineReconciliation:
    def test_adopts_a_position_the_bot_did_not_know_about(self):
        """Ровно случай со скриншота: на бирже 2 шэра, у бота «поз —»."""
        eng, _ = _live_engine()
        eng.trader.position = lambda token: (
            {"balance": "2000000"} if token == "DNTOK" else {"balance": "0"})
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        assert eng.legs == []

        asyncio.run(eng._reconcile())

        assert len(eng.legs) == 1
        assert eng.legs[0]["outcome"] == "Down"
        assert eng.legs[0]["shares"] == 2.0
        # Нога попала и в стратегию — иначе её никто не стал бы продавать.
        assert len(eng.strategy.legs) == 1

    def test_adopted_position_is_then_managed_normally(self):
        """Взяли под управление — дальше её должен закрыть обычный стоп."""
        from flowbot.jump import SELL

        eng, cfg = _live_engine(jump_stop_loss=0.01)
        eng.trader.position = lambda token: (
            {"balance": "2000000"} if token == "DNTOK" else {"balance": "0"})
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        asyncio.run(eng._reconcile())

        # Цена ушла против нас — стоп обязан сработать по принятой ноге.
        _book(eng, 0.60, 0.61, 0.39, 0.40)
        from flowbot.jump import JumpSnapshot

        act = eng.strategy.on_tick(JumpSnapshot(
            t=time.time() + 5, seconds_left=200.0, coin_price=65_000.0,
            target=65_000.0, jump_usd=0.0, sigma_1s=1.0,
            up_bid=0.60, up_ask=0.61, down_bid=0.39, down_ask=0.40))
        assert act.kind == SELL

    def test_drops_a_phantom_the_exchange_does_not_have(self):
        eng, _ = _live_engine()
        eng.trader.position = lambda token: {"balance": "0"}
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        eng.legs.append({
            "idx": 0, "outcome": "Up", "token": "UPTOK", "entry_price": 0.52,
            "shares": 2.0, "cost": 1.04, "track": "A", "last_bid": 0.51,
            "coin_at_entry": 65_000.0, "secs_at_entry": 100})
        eng.strategy.record_entry("Up", 0.52, 2.0, 1.04, "A", time.time(),
                                  entry_bid=0.51)

        asyncio.run(eng._reconcile())

        assert eng.legs == []
        assert eng.strategy.legs == []

    def test_unknown_answer_changes_nothing(self):
        """Не разобрали ответ — трогать позиции нельзя."""
        eng, _ = _live_engine()
        eng.trader.position = lambda token: {"неизвестное": "поле"}
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        eng.legs.append({
            "idx": 0, "outcome": "Up", "token": "UPTOK", "entry_price": 0.52,
            "shares": 2.0, "cost": 1.04, "track": "A", "last_bid": 0.51,
            "coin_at_entry": 65_000.0, "secs_at_entry": 100})
        asyncio.run(eng._reconcile())
        assert len(eng.legs) == 1

    def test_dry_run_does_not_reconcile(self):
        """В симуляции биржа о наших ордерах не знает — сверять не с чем."""
        eng, _ = _engine()                      # dry_run=True
        called = []
        eng.trader.position = lambda token: called.append(token)
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        asyncio.run(eng._reconcile())
        assert called == []
