"""Качество импульса: трекер и режим входа `impulse`.

Старый триггер задавал один вопрос — «прошёл ли рынок достаточное
расстояние?» — и три разные ситуации выглядели для него одинаково:
сильный импульс, плавный дрейф и ложный вынос ликвидности. Здесь
проверяется, что каждая из трёх теперь различается.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.config import FlowConfig                       # noqa: E402
from flowbot.impulse import ImpulseTracker                  # noqa: E402
from flowbot.jump import (ENTER, NONE, JumpSnapshot,        # noqa: E402
                          JumpStrategy)


def feed(tr: ImpulseTracker, points):
    """points — [(t, price), …] с шагом не меньше сетки трекера."""
    for t, p in points:
        tr.feed(t, p)
    return tr.state()


def ramp(t0, p0, dur, dp, step=0.05):
    """Ровный ход цены на dp долларов за dur секунд."""
    n = max(1, int(round(dur / step)))
    return [(t0 + i * step, p0 + dp * i / n) for i in range(n + 1)]


# ---------------------------------------------------------------------------
#  Трекер: различает три ситуации, неразличимые для старого триггера
# ---------------------------------------------------------------------------
class TestTrackerTellsMovesApart:
    def test_fast_move_that_holds_is_a_strong_impulse(self):
        tr = ImpulseTracker(lookback_s=15.0)
        pts = ramp(0.0, 65_000.0, 2.0, 0.0)          # две секунды покоя
        pts += ramp(2.0, 65_000.0, 0.4, 5.0)         # $5 за 0.4с
        pts += ramp(2.4, 65_005.0, 0.6, 0.6)         # держится и ползёт вверх
        st = feed(tr, pts)
        assert st is not None
        assert st.jump_usd == pytest.approx(5.6, abs=0.3)
        assert st.speed > 3.0, "быстрый ход должен дать высокую скорость"
        assert st.hold > 0.9, "цена удержала ход"
        assert st.age_s < 1.5, "экстремум свежий"

    def test_slow_drift_has_the_same_jump_but_low_speed(self):
        """Те же $5, но за 15 секунд — это дрейф, книга его давно учла."""
        tr = ImpulseTracker(lookback_s=20.0)
        st = feed(tr, ramp(0.0, 65_000.0, 15.0, 5.0))
        assert st is not None
        assert st.jump_usd == pytest.approx(5.0, abs=0.3)
        assert st.speed < 0.6, "дрейф не должен выглядеть быстрым"
        assert st.age_s > 10.0, "экстремум старый"

    def test_liquidity_sweep_is_given_away_by_hold(self):
        """$5 вверх и почти сразу обратно: старый триггер видел тот же скачок.

        Откат берём меньше половины — иначе доминирующим станет обратное
        движение и трекер (справедливо) начнёт описывать уже его.
        """
        tr = ImpulseTracker(lookback_s=15.0)
        pts = ramp(0.0, 65_000.0, 1.0, 0.0)
        pts += ramp(1.0, 65_000.0, 0.2, 5.0)      # выброс вверх
        pts += ramp(1.2, 65_005.0, 0.4, -2.4)     # и почти весь назад
        st = feed(tr, pts)
        assert st is not None
        assert st.jump_usd > 0, "движение вверх всё ещё доминирует"
        assert st.hold < 0.6, "больше трети хода отдано назад"

    def test_hold_cannot_go_below_half_by_construction(self):
        """Откат за 50% переключает направление — hold в [0.5, 1.0] всегда."""
        tr = ImpulseTracker(lookback_s=15.0)
        pts = ramp(0.0, 65_000.0, 1.0, 0.0)
        pts += ramp(1.0, 65_000.0, 0.2, 5.0)
        pts += ramp(1.2, 65_005.0, 0.4, -4.5)     # откат 90%
        st = feed(tr, pts)
        assert st is not None
        assert st.hold >= 0.5
        assert st.jump_usd < 0, "теперь доминирует движение ВНИЗ"

    def test_fading_move_has_accel_below_one(self):
        tr = ImpulseTracker(lookback_s=15.0)
        pts = ramp(0.0, 65_000.0, 1.5, 6.0)       # быстро…
        pts += ramp(1.5, 65_006.0, 1.5, 0.2)      # …и почти встало
        st = feed(tr, pts)
        assert st is not None
        assert st.accel < 1.0, "затухание должно быть видно в ускорении"

    def test_needs_history_before_it_answers(self):
        tr = ImpulseTracker()
        assert tr.state() is None
        assert feed(tr, ramp(0.0, 65_000.0, 0.3, 1.0)) is None

    def test_reset_forgets_the_move(self):
        tr = ImpulseTracker(lookback_s=15.0)
        feed(tr, ramp(0.0, 65_000.0, 2.0, 6.0))
        tr.reset(2.0, 65_006.0)
        assert tr.state() is None


# ---------------------------------------------------------------------------
#  Режим impulse: ворота и оценка качества
# ---------------------------------------------------------------------------
def cfg_imp(**over) -> FlowConfig:
    c = FlowConfig()
    c.jump_entry_mode = "impulse"
    c.jump_adaptive = False
    for k, v in over.items():
        setattr(c, k, v)
    return c


def isnap(jump=6.0, speed=4.0, accel=1.5, hold=0.9, age=0.5, sigma=1.0,
          price=65_005.0, target=65_000.0, left=150.0,
          up_bid=0.59, up_ask=0.60, down_bid=0.39, down_ask=0.40,
          t=100.0) -> JumpSnapshot:
    return JumpSnapshot(
        t=t, seconds_left=left, coin_price=price, target=target,
        jump_usd=jump, sigma_1s=sigma,
        up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
        speed=speed, accel=accel, imp_age_s=age, imp_hold=hold,
    )


class TestImpulseGates:
    def test_good_impulse_enters(self):
        act = JumpStrategy(cfg_imp()).on_tick(isnap())
        assert act.kind == ENTER
        assert act.outcome == "Up"
        assert "ИМПУЛЬС" in act.reason

    def test_jump_is_measured_in_sigmas_not_dollars(self):
        """σ=5 -> $6 это чуть больше сигмы, оценка хода падает в ноль."""
        s = JumpStrategy(cfg_imp())
        assert s.on_tick(isnap(jump=6.0, sigma=1.0)).kind == ENTER
        act = s.on_tick(isnap(jump=6.0, sigma=5.0, speed=20.0))
        assert act.kind == NONE
        assert "jump 0.00" in act.reason

    def test_slow_move_is_refused_even_with_a_big_jump(self):
        """Скорость 0.3σ ниже дна шкалы (0.7σ) — это ноль, а ноль запрещает."""
        act = JumpStrategy(cfg_imp()).on_tick(isnap(jump=20.0, speed=0.3))
        assert act.kind == NONE
        assert "speed" in act.reason

    def test_liquidity_sweep_is_refused(self):
        """Удержание 55% ниже дна шкалы (60%) — ноль, вход отменён."""
        act = JumpStrategy(cfg_imp()).on_tick(isnap(hold=0.55))
        assert act.kind == NONE
        assert "hold" in act.reason and "запрещающий" in act.reason

    def test_fading_impulse_only_lowers_the_score(self):
        """А вот ускорение 0.2 — уже НЕ запрет, а штраф.

        Ровно то, ради чего вводился скоринг: признак, просевший ниже
        прежнего жёсткого порога (0.5), больше не отменяет сделку в
        одиночку — он лишь снижает её оценку.
        """
        s = JumpStrategy(cfg_imp())
        good = s.on_tick(isnap(accel=1.5))
        weak = s.on_tick(isnap(accel=0.45))
        assert good.kind == ENTER and weak.kind == ENTER
        assert weak.feat["q"] < good.feat["q"]
        assert weak.feat["s_accel"] < good.feat["s_accel"]

    def test_one_slightly_missed_threshold_no_longer_kills_the_trade(self):
        """Возраст 8.2с вместо 8.0 — прежде это был полный отказ."""
        act = JumpStrategy(cfg_imp()).on_tick(
            isnap(jump=12.0, speed=6.0, hold=1.0, accel=1.5, age=8.2))
        assert act.kind == ENTER, "сильный сигнал не должен пропадать"
        assert act.feat["s_age"] < 1.0, "но возраст обязан снизить оценку"

    def test_waits_for_the_tracker_to_warm_up(self):
        act = JumpStrategy(cfg_imp()).on_tick(isnap(speed=None))
        assert act.kind == NONE
        assert "прогревается" in act.reason

    def test_no_tracks_a_and_b(self):
        """Дорожек нет: одинаковый импульс оценивается одинаково при
        любой цене контракта. Цена входит только как риск."""
        s = JumpStrategy(cfg_imp())
        act = s.on_tick(isnap(up_bid=0.44, up_ask=0.45, down_bid=0.54,
                              down_ask=0.55))
        assert act.kind == ENTER
        assert act.track == "Q"


class TestDynamicEdge:
    def test_wide_spread_lowers_the_edge_score(self):
        """Спред остался в расчёте — он растягивает шкалу запаса.

        Абсолютная шкала «6¢ = отлично» неверна там, где спред 10¢: после
        круга от такого запаса не остаётся ничего. Опорный порог считается
        как max(база, множитель × спред), и вся шкала едет вместе с ним.
        """
        c = cfg_imp(jump_edge_spread_mult=2.0)
        s = JumpStrategy(c)
        tight = s.on_tick(isnap(up_bid=0.59, up_ask=0.60))
        wide = s.on_tick(isnap(up_bid=0.50, up_ask=0.60))
        assert tight.feat["s_edge"] > wide.feat["s_edge"]
        assert wide.kind == NONE, "20¢ порога против 5.9¢ запаса"

    def test_tight_spread_keeps_the_base_threshold(self):
        act = JumpStrategy(cfg_imp()).on_tick(isnap(up_bid=0.59, up_ask=0.60))
        assert act.kind == ENTER


class TestQualitySizing:
    """Ставка от качества ВЫКЛЮЧЕНА по умолчанию — проверяем оба режима."""

    def test_flat_stake_by_default(self):
        """Ступени $1/$2/$4/$8 рвали непрерывное качество на куски: Q=2.99
        брало на $4, Q=3.01 — на $8. Пока пороги не подтверждены историей,
        множитель к ставке множит и ошибку, и вдобавок ломает статистику:
        разный размер позиции смешивает «сигнал был лучше» с «мы поставили
        больше»."""
        s = JumpStrategy(cfg_imp())
        for q in (0.0, 0.5, 0.75, 0.9, 1.0):
            assert s.stake_for_quality(q) == 1.0

    def test_stake_grows_with_quality(self):
        s = JumpStrategy(cfg_imp(jump_stake_by_quality=True))
        assert s.stake_for_quality(0.70) == 1.0
        assert s.stake_for_quality(0.82) == 2.0
        assert s.stake_for_quality(0.90) == 4.0
        assert s.stake_for_quality(0.97) == 8.0

    def test_price_cap_grows_with_quality(self):
        s = JumpStrategy(cfg_imp())
        assert s.max_leg_price_for_quality(0.72) == pytest.approx(0.92)
        assert s.max_leg_price_for_quality(0.82) == pytest.approx(0.95)
        assert s.max_leg_price_for_quality(0.95) == pytest.approx(0.97)

    def test_strong_signal_buys_more(self):
        """Сильный импульс получает ставку больше базовой — если включено."""
        act = JumpStrategy(cfg_imp(jump_stake_by_quality=True)).on_tick(
            isnap(jump=12.0, speed=9.0, accel=2.5, hold=1.0))
        assert act.kind == ENTER
        assert act.size_usdc > 1.0

    def test_weak_signal_gets_less_than_a_strong_one(self):
        s = JumpStrategy(cfg_imp(jump_stake_by_quality=True))
        weak = s.on_tick(isnap(jump=2.6, speed=1.6, accel=1.0, hold=0.85,
                               up_bid=0.61, up_ask=0.62))
        strong = s.on_tick(isnap(jump=12.0, speed=9.0, accel=2.5, hold=1.0))
        assert weak.kind == ENTER and strong.kind == ENTER
        assert weak.size_usdc < strong.size_usdc

    def test_same_stake_regardless_of_quality_by_default(self):
        s = JumpStrategy(cfg_imp())
        weak = s.on_tick(isnap(jump=2.6, speed=1.6, accel=1.0, hold=0.85,
                               up_bid=0.61, up_ask=0.62))
        strong = s.on_tick(isnap(jump=12.0, speed=9.0, accel=2.5, hold=1.0))
        assert weak.kind == ENTER and strong.kind == ENTER
        assert weak.size_usdc == strong.size_usdc == 1.0


# ---------------------------------------------------------------------------
#  Адаптивный стоп и выход по смерти импульса
# ---------------------------------------------------------------------------
class TestAdaptiveStop:
    def test_quiet_market_keeps_one_cent(self):
        s = JumpStrategy(cfg_imp(jump_stop_loss=0.01, jump_sigma_ref=0.9))
        assert s.stop_threshold(isnap(sigma=0.9)) == pytest.approx(0.01)

    def test_fast_market_widens_the_stop(self):
        s = JumpStrategy(cfg_imp(jump_stop_loss=0.01, jump_sigma_ref=0.9))
        assert s.stop_threshold(isnap(sigma=2.7)) == pytest.approx(0.03)

    def test_never_beyond_the_ceiling(self):
        s = JumpStrategy(cfg_imp(jump_stop_loss=0.01, jump_sigma_ref=0.9,
                                 jump_stop_max=0.05))
        assert s.stop_threshold(isnap(sigma=50.0)) == pytest.approx(0.05)

    def test_can_be_switched_off(self):
        s = JumpStrategy(cfg_imp(jump_stop_adaptive=False, jump_stop_loss=0.01))
        assert s.stop_threshold(isnap(sigma=9.0)) == pytest.approx(0.01)


class TestSpeedDeathExit:
    def _in_position(self, **over):
        c = cfg_imp(**over)
        s = JumpStrategy(c)
        s.record_entry("Up", 0.60, 2.0, 1.20, "Q", t=100.0, entry_bid=0.59)
        return s

    def test_sells_when_speed_collapses_in_profit(self):
        """Скорость умирает раньше отката процента — трейлинг опоздал бы."""
        from flowbot.jump import SELL
        s = self._in_position()
        s.on_tick(isnap(t=101.0, speed=10.0, up_bid=0.66, up_ask=0.67))
        act = s.on_tick(isnap(t=102.0, speed=1.0, up_bid=0.66, up_ask=0.67))
        assert act.kind == SELL
        assert "ИМПУЛЬС УМЕР" in act.reason

    def test_does_not_sell_while_speed_holds(self):
        from flowbot.jump import SELL
        s = self._in_position()
        s.on_tick(isnap(t=101.0, speed=10.0, up_bid=0.66, up_ask=0.67))
        act = s.on_tick(isnap(t=102.0, speed=9.0, up_bid=0.66, up_ask=0.67))
        assert not (act.kind == SELL and "ИМПУЛЬС УМЕР" in act.reason)

    def test_does_not_sell_at_a_loss(self):
        """Выход по смерти импульса — способ ЗАБРАТЬ прибыль, а не резать
        убыток: для убытка есть стоп со своим порогом."""
        from flowbot.jump import SELL
        s = self._in_position()
        s.on_tick(isnap(t=101.0, speed=10.0, up_bid=0.59, up_ask=0.60))
        act = s.on_tick(isnap(t=102.0, speed=0.0, up_bid=0.58, up_ask=0.59))
        assert not (act.kind == SELL and "ИМПУЛЬС УМЕР" in act.reason)

    def test_noise_peak_does_not_arm_the_exit(self):
        from flowbot.jump import SELL
        s = self._in_position(jump_exit_speed_floor=5.0)
        s.on_tick(isnap(t=101.0, speed=1.0, up_bid=0.66, up_ask=0.67))
        act = s.on_tick(isnap(t=102.0, speed=0.0, up_bid=0.66, up_ask=0.67))
        assert not (act.kind == SELL and "ИМПУЛЬС УМЕР" in act.reason)

    def test_can_be_switched_off(self):
        from flowbot.jump import SELL
        s = self._in_position(jump_exit_speed_drop=0.0)
        s.on_tick(isnap(t=101.0, speed=10.0, up_bid=0.66, up_ask=0.67))
        act = s.on_tick(isnap(t=102.0, speed=0.0, up_bid=0.66, up_ask=0.67))
        assert not (act.kind == SELL and "ИМПУЛЬС УМЕР" in act.reason)
