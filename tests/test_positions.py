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


def _book_tokens(eng, up, dn, up_bid, up_ask, dn_bid, dn_ask):
    """То же, что _book, но для произвольных токенов (следующий раунд)."""
    for token, bid, ask in ((up, up_bid, up_ask), (dn, dn_bid, dn_ask)):
        eng.book.on_event({"event_type": "book", "asset_id": token,
                           "bids": [{"price": str(bid), "size": "100"}],
                           "asks": [{"price": str(ask), "size": "100"}]})


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

    def test_leg_from_a_previous_round_is_not_mistaken_for_a_phantom(self):
        """Расчёт пропускается, если на смене окна был ордер в полёте.

        Тогда нога прошлого раунда доживает до нового. Сверять её по СТОРОНЕ
        значило бы приписать её балансу чужого токена: биржа ответила бы
        «меньше», и живая нога улетела бы как фантом.
        """
        eng, _ = _live_engine()
        eng.trader.position = lambda token: {"balance": "0"}
        eng.legs.append({
            "idx": 0, "outcome": "Down", "token": "OLD-DNTOK",
            "entry_price": 0.65, "shares": 2.0, "cost": 1.30, "track": "A",
            "last_bid": 0.64, "coin_at_entry": 65_000.0, "secs_at_entry": 10})
        eng.strategy.record_entry("Down", 0.65, 2.0, 1.30, "A", time.time(),
                                  entry_bid=0.64)
        _book(eng, 0.51, 0.52, 0.48, 0.49)

        asyncio.run(eng._reconcile())

        assert len(eng.legs) == 1
        assert eng.legs[0]["token"] == "OLD-DNTOK"

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

class TestLeftoversFromFinishedRounds:
    """Остаток от ЗАКОНЧИВШЕГОСЯ раунда.

    Боевой случай: бот держал ногу до конца окна, записал себе выплату и
    обнулил список ног. Шэры при этом остались на балансе — выплату по ним
    забирают Redeem'ом, а не продажей. Сверка спрашивала биржу только про два
    токена ТЕКУЩЕГО окна, поэтому такой остаток был невидим по построению: на
    Polymarket висит позиция, а бот показывает «поз —».
    """

    def _settled(self, **over):
        """Движок, только что закрывший раунд с одной ногой Down."""
        eng, cfg = _live_engine(**over)
        _book(eng, 0.02, 0.03, 0.97, 0.98)      # книга схлопнулась: Down взял
        eng.legs.append({
            "idx": 0, "outcome": "Down", "token": "DNTOK", "entry_price": 0.65,
            "shares": 2.0, "cost": 1.30, "track": "A", "last_bid": 0.97,
            "coin_at_entry": 65_000.0, "secs_at_entry": 10})
        eng.strategy.record_entry("Down", 0.65, 2.0, 1.30, "A", time.time(),
                                  entry_bid=0.64)
        eng._settle_open_position("конец окна")
        return eng, cfg

    def test_leg_held_to_settlement_is_remembered(self):
        eng, _ = self._settled()
        assert eng.legs == []                   # ног нет — раунд закрыт
        assert "DNTOK" in eng._leftovers        # но шэры мы не забыли
        assert eng._leftovers["DNTOK"]["shares"] == 2.0
        assert eng._leftovers["DNTOK"]["outcome"] == "Down"
        assert eng._leftovers["DNTOK"]["slug"] == "btc-updown-5m-test"

    def test_leftover_is_checked_after_the_round_moved_on(self):
        """Новый раунд — новые токены. Старый всё равно должен опрашиваться."""
        eng, _ = self._settled()
        asked = []

        def position(token):
            asked.append(token)
            return {"balance": "2000000"} if token == "DNTOK" else {"balance": "0"}

        eng.trader.position = position
        eng.market = dict(eng.market, up="UP2", down="DN2",
                          slug="btc-updown-5m-next")
        eng.book.set_tokens("UP2", "DN2")
        _book_tokens(eng, "UP2", "DN2", 0.51, 0.52, 0.48, 0.49)

        asyncio.run(eng._reconcile())

        assert "DNTOK" in asked                 # спросили про прошлый раунд
        assert eng._leftovers["DNTOK"]["shares"] == 2.0

    def test_leftover_is_not_adopted_as_a_tradable_leg(self):
        """Продавать нечем: книги закончившегося раунда больше нет.

        Заведи мы ногу — бот пытался бы выйти на каждом такте и получал бы
        отказ за отказом.
        """
        eng, _ = self._settled()
        eng.trader.position = lambda token: (
            {"balance": "2000000"} if token == "DNTOK" else {"balance": "0"})
        eng.market = dict(eng.market, up="UP2", down="DN2")
        eng.book.set_tokens("UP2", "DN2")
        _book_tokens(eng, "UP2", "DN2", 0.51, 0.52, 0.48, 0.49)

        asyncio.run(eng._reconcile())

        assert eng.legs == []
        assert eng.strategy.legs == []

    def test_leftover_is_forgotten_once_redeemed(self):
        eng, _ = self._settled()
        eng.trader.position = lambda token: {"balance": "0"}
        eng.market = dict(eng.market, up="UP2", down="DN2")
        eng.book.set_tokens("UP2", "DN2")
        _book_tokens(eng, "UP2", "DN2", 0.51, 0.52, 0.48, 0.49)

        asyncio.run(eng._reconcile())

        assert eng._leftovers == {}

    def test_unknown_answer_keeps_the_leftover(self):
        """«Не знаю» — не «получено». Забыть остаток можно только по нулю."""
        eng, _ = self._settled()
        eng.trader.position = lambda token: {"неизвестное": "поле"}
        eng.market = dict(eng.market, up="UP2", down="DN2")
        eng.book.set_tokens("UP2", "DN2")
        _book_tokens(eng, "UP2", "DN2", 0.51, 0.52, 0.48, 0.49)

        asyncio.run(eng._reconcile())

        assert "DNTOK" in eng._leftovers

    def test_warning_is_throttled(self):
        eng, cfg = self._settled(jump_leftover_warn_s=120.0)
        eng.trader.position = lambda token: (
            {"balance": "2000000"} if token == "DNTOK" else {"balance": "0"})
        eng.market = dict(eng.market, up="UP2", down="DN2")
        eng.book.set_tokens("UP2", "DN2")
        _book_tokens(eng, "UP2", "DN2", 0.51, 0.52, 0.48, 0.49)
        said = []
        eng.log.error = lambda *a, **k: said.append(a)

        asyncio.run(eng._reconcile())
        asyncio.run(eng._reconcile())

        assert len(said) == 1

    def test_leftover_shows_up_in_the_status_line(self):
        """Ровно то, чего не хватало на скриншоте: «поз —» рядом с позицией."""
        from flowbot.jump import Action, HOLD
        from flowbot.jump import JumpSnapshot

        eng, cfg = self._settled()
        cfg.status_log_interval_seconds = 0.0
        said = []
        eng.log.info = lambda *a, **k: said.append(a[0] % a[1:])
        eng._log_status(JumpSnapshot(
            t=time.time(), seconds_left=100.0, coin_price=65_000.0,
            target=65_000.0, jump_usd=0.0, sigma_1s=1.0,
            up_bid=0.51, up_ask=0.52, down_bid=0.48, down_ask=0.49),
            Action(HOLD, reason="—"))
        assert any("ост 2.0" in line for line in said)

    def test_dry_run_remembers_nothing(self):
        """В симуляции шэров нет: расчёт честно возвращает деньги в кэш."""
        eng, _ = _engine()                      # dry_run=True
        _book(eng, 0.02, 0.03, 0.97, 0.98)
        eng.legs.append({
            "idx": 0, "outcome": "Down", "token": "DNTOK", "entry_price": 0.65,
            "shares": 2.0, "cost": 1.30, "track": "A", "last_bid": 0.97,
            "coin_at_entry": 65_000.0, "secs_at_entry": 10})
        eng.strategy.record_entry("Down", 0.65, 2.0, 1.30, "A", time.time(),
                                  entry_bid=0.64)
        eng._settle_open_position("конец окна")
        assert eng._leftovers == {}


class TestDryRun:
    def test_dry_run_does_not_reconcile(self):
        """В симуляции биржа о наших ордерах не знает — сверять не с чем."""
        eng, _ = _engine()                      # dry_run=True
        called = []
        eng.trader.position = lambda token: called.append(token)
        _book(eng, 0.51, 0.52, 0.48, 0.49)
        asyncio.run(eng._reconcile())
        assert called == []
