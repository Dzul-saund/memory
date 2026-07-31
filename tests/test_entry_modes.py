"""Три режима входа: jump (исходный), edge (запас), lag (отставание якоря).

Режимы отличаются РОВНО тем, что считают поводом войти. Всё, что дальше —
ставка, лестница, фиксация, потолки — у них общее, иначе запуски нельзя было
бы сравнивать между собой.

Числа подобраны так, чтобы модель считалась в уме: sigma=1.0 и 100 секунд
до конца дают sigma*sqrt(t) = $10, поэтому отрыв в $5 — это ровно 0.5 сигмы,
а Phi(0.5) = 0.6915.
"""
from __future__ import annotations

import pytest

from flowbot.config import FlowConfig
from flowbot.jump import ENTER, NONE, JumpSnapshot, JumpStrategy, TRACK_A
from flowbot.jump_run import tag_path


def base_cfg(mode: str) -> FlowConfig:
    c = FlowConfig()
    c.jump_entry_mode = mode
    c.jump_stake_usdc = 1.0
    c.jump_min_edge_cents = 3.0
    c.jump_lag_min_cents = 3.0
    c.jump_lag_max_age_ms = 1500.0
    c.jump_max_leg_price = 0.95
    c.jump_price_split = 0.51
    c.settle_hold_s = 6.0
    return c


def snap(price=65_005.0, pm=65_000.0, pm_age=100.0, jump=0.0,
         up_bid=0.59, up_ask=0.60, down_bid=0.40, down_ask=0.41,
         left=100.0, sigma=1.0, target=65_000.0, t=100.0) -> JumpSnapshot:
    return JumpSnapshot(
        t=t, seconds_left=left, coin_price=price, target=target,
        jump_usd=jump, sigma_1s=sigma,
        up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
        pm_price=pm, pm_age_ms=pm_age,
    )


# ---------------------------------------------------------------------------
#  Режим edge: триггер — запас Phi(z) − ask
# ---------------------------------------------------------------------------
class TestEdgeMode:
    def test_enters_on_undervalued_side(self):
        # Phi(0.5) = 0.6915, просят 0.60 => запас +9.2¢ >= 3¢
        act = JumpStrategy(base_cfg("edge")).on_tick(snap())
        assert act.kind == ENTER
        assert act.outcome == "Up"
        assert act.track == TRACK_A
        assert "справедливо 69" in act.reason

    def test_jump_is_irrelevant(self):
        """Главное отличие от режима jump: скачка нет, а вход есть.

        Ровно те сделки, которые исходная конструкция пропускала: книга
        отстала, но цена монеты в этот момент никуда не прыгала.
        """
        act = JumpStrategy(base_cfg("edge")).on_tick(snap(jump=0.0))
        assert act.kind == ENTER

    def test_picks_the_better_of_two_sides(self):
        # Цена УПАЛА на 0.5 сигмы: справедливо Down = 0.6915, просят 0.41
        act = JumpStrategy(base_cfg("edge")).on_tick(
            snap(price=64_995.0, up_bid=0.59, up_ask=0.60,
                 down_bid=0.40, down_ask=0.41))
        assert act.kind == ENTER
        assert act.outcome == "Down"

    def test_no_edge_no_trade(self):
        # Цена на таргете: справедливо 50/50, а просят дороже с обеих сторон
        act = JumpStrategy(base_cfg("edge")).on_tick(
            snap(price=65_000.0, up_bid=0.49, up_ask=0.52,
                 down_bid=0.48, down_ask=0.51))
        assert act.kind == NONE
        assert "брать нечего" in act.reason

    def test_price_cap_blocks_the_fat_tail_trap(self):
        """У краёв Phi говорит «справедливо 100¢» — там ей верить нельзя.

        Без этого потолка режим edge скупал бы всё по 0.96, потому что
        модель считает хвост нулевым, а он не нулевой.
        """
        act = JumpStrategy(base_cfg("edge")).on_tick(
            snap(price=65_100.0, up_bid=0.95, up_ask=0.96))
        assert act.kind == NONE
        assert "потолка" in act.reason

    def test_no_sigma_no_decision(self):
        act = JumpStrategy(base_cfg("edge")).on_tick(snap(sigma=None))
        assert act.kind == NONE
        assert "запас не посчитать" in act.reason


# ---------------------------------------------------------------------------
#  Режим lag: триггер — отставание якоря Polymarket
# ---------------------------------------------------------------------------
class TestLagMode:
    def test_enters_when_anchor_lags_behind(self):
        # мы видим 65_005, Polymarket всё ещё считает по 65_000
        # книге переоцениться на 0.6915 − 0.50 = 19.2¢
        act = JumpStrategy(base_cfg("lag")).on_tick(
            snap(price=65_005.0, pm=65_000.0, up_ask=0.55, up_bid=0.54))
        assert act.kind == ENTER
        assert act.outcome == "Up"
        assert "ОТСТАВАНИЕ ЯКОРЯ" in act.reason

    def test_direction_follows_the_lag(self):
        act = JumpStrategy(base_cfg("lag")).on_tick(
            snap(price=64_995.0, pm=65_000.0, down_ask=0.55, down_bid=0.54))
        assert act.kind == ENTER
        assert act.outcome == "Down"

    def test_stale_anchor_is_not_a_signal(self):
        """Молчащий якорь — дырка в потоке, а не опережение."""
        act = JumpStrategy(base_cfg("lag")).on_tick(
            snap(pm_age=3000.0, up_ask=0.55))
        assert act.kind == NONE
        assert "дырка в потоке" in act.reason

    def test_small_lag_is_ignored(self):
        act = JumpStrategy(base_cfg("lag")).on_tick(
            snap(price=65_000.2, pm=65_000.0, up_ask=0.50, up_bid=0.49))
        assert act.kind == NONE
        assert "переоценится лишь" in act.reason

    def test_lag_without_edge_means_we_are_late(self):
        """Книга уже успела переоцениться — отставание есть, а денег нет."""
        act = JumpStrategy(base_cfg("lag")).on_tick(
            snap(price=65_005.0, pm=65_000.0, up_ask=0.69, up_bid=0.68))
        assert act.kind == NONE
        assert "опоздали" in act.reason

    def test_missing_anchor_price(self):
        act = JumpStrategy(base_cfg("lag")).on_tick(snap(pm=None))
        assert act.kind == NONE
        assert "якоря" in act.reason


# ---------------------------------------------------------------------------
#  Общие предусловия одинаковы во всех режимах
# ---------------------------------------------------------------------------
class TestSharedGates:
    @pytest.mark.parametrize("mode", ["jump", "edge", "lag"])
    def test_no_late_entry_on_the_cheap_side(self, mode):
        """В конце окна берём только сторону, которая уже выигрывает.

        Сторона дешевле сплита в последние секунды безнадёжна: её не
        вытянет никакой импульс, времени на переоценку не осталось.
        """
        c = base_cfg(mode)
        c.jump_late_entry = True
        act = JumpStrategy(c).on_tick(snap(
            left=3.0, jump=50.0, price=65_005.0,
            up_bid=0.29, up_ask=0.30, down_bid=0.70, down_ask=0.71))
        assert act.kind == NONE

    def test_late_entry_refusal_names_the_reason(self):
        """Отказ должен объяснять, что дело в позднем входе, а не в чём-то ещё."""
        c = base_cfg("edge")
        c.jump_late_entry = True
        act = JumpStrategy(c).on_tick(snap(
            left=3.0, price=65_005.0,
            up_bid=0.29, up_ask=0.30, down_bid=0.70, down_ask=0.71))
        assert act.kind == NONE
        assert "поздний вход" in act.reason

    @pytest.mark.parametrize("mode", ["jump", "edge", "lag"])
    def test_late_entry_switched_off_blocks_everything(self, mode):
        c = base_cfg(mode)
        c.jump_late_entry = False
        act = JumpStrategy(c).on_tick(snap(left=3.0, jump=50.0, up_ask=0.55))
        assert act.kind == NONE
        assert "конец окна" in act.reason

    @pytest.mark.parametrize("mode", ["jump", "edge", "lag"])
    def test_cooldown_applies_everywhere(self, mode):
        c = base_cfg(mode)
        c.jump_reentry_cooldown_s = 1.0
        s = JumpStrategy(c)
        s._last_fill_t = 100.0
        act = s.on_tick(snap(t=100.5, jump=50.0, up_ask=0.55))
        assert act.kind == NONE
        assert "пауза после сделки" in act.reason


# ---------------------------------------------------------------------------
#  Разведение файлов и проверка конфигурации
# ---------------------------------------------------------------------------
class TestIsolation:
    def test_tag_path_keeps_extension(self):
        assert tag_path("market.jsonl", "edge") == "market_edge.jsonl"
        assert tag_path("jump_trades.csv", "lag") == "jump_trades_lag.csv"
        assert tag_path("a/b/rec.jsonl", "edge") == "a/b/rec_edge.jsonl"

    def test_tag_path_leaves_empty_alone(self):
        assert tag_path("", "edge") == ""

    def test_unknown_mode_is_rejected(self):
        c = base_cfg("нечто")
        with pytest.raises(ValueError, match="JUMP_ENTRY_MODE"):
            c.validate_jump()

    def test_edge_mode_needs_edge_threshold(self):
        c = base_cfg("edge")
        c.jump_min_edge_cents = 0.0
        with pytest.raises(ValueError, match="edge"):
            c.validate_jump()

    def test_lag_mode_needs_both_thresholds(self):
        c = base_cfg("lag")
        c.jump_min_edge_cents = 0.0
        with pytest.raises(ValueError, match="lag"):
            c.validate_jump()

    def test_default_mode_is_the_original(self):
        """Уже запущенный бот не должен ничего заметить."""
        assert FlowConfig().jump_entry_mode == "jump"
