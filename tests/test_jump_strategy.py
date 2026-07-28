"""Тесты скачковой стратегии (4-я система) — без сети, без ключей, без денег.

Проверяем ровно те правила, что заказал юзер:
  * скачок $5 берём только на дорогой стороне (>= 51¢);
  * на дешёвой (< 51¢) нужен скачок $15 И близость к таргету (<= $100);
  * лестница добора перекрывает весь вложенный минус и выводит в плюс;
  * дешёвая дорожка фиксирует прибыль по дедлайну / затуханию роста.
"""
from __future__ import annotations

import pytest

from flowbot.config import FlowConfig
from flowbot.jump import (ENTER, HOLD, LADDER, NONE, SELL, JumpSnapshot,
                          JumpStrategy, TRACK_A, TRACK_B, ladder_shares)


@pytest.fixture()
def cfg() -> FlowConfig:
    c = FlowConfig()
    c.jump_window_s = 3.0
    c.jump_small_usd = 5.0
    c.jump_big_usd = 15.0
    c.jump_price_split = 0.51
    c.jump_max_target_dist_usd = 100.0
    c.jump_stake_usdc = 1.0
    c.jump_ladder_grace_s = 0.6
    c.jump_ladder_profit_usdc = 0.50
    c.settle_hold_s = 6.0
    # Подстройку порогов под волатильность и запас цены проверяем
    # отдельно (TestAdaptiveThresholds / TestEdgeFilter); остальным
    # тестам они бы плавали пороги.
    c.jump_adaptive = False
    c.jump_min_edge_cents = 0.0
    # Активное ведение (стоп + фиксация на обеих дорожках) проверяется
    # отдельно в TestActiveManagement. Здесь оно выключено, иначе стоп
    # срабатывал бы раньше лестницы и тесты лестницы проверяли бы не то.
    c.jump_manage_enabled = False
    return c


def snap(t=100.0, left=200.0, price=65_000.0, target=65_000.0, jump=0.0,
         up_bid=None, up_ask=None, down_bid=None, down_ask=None,
         up_flow=0.0, down_flow=0.0, sigma=None) -> JumpSnapshot:
    return JumpSnapshot(
        t=t, seconds_left=left, coin_price=price, target=target,
        jump_usd=jump, up_bid=up_bid, up_ask=up_ask,
        down_bid=down_bid, down_ask=down_ask,
        up_flow=up_flow, down_flow=down_flow, sigma_1s=sigma,
    )


# ---------------------------------------------------------------------------
#  Математика лестницы
# ---------------------------------------------------------------------------
class TestLadderShares:
    def test_covers_debt_and_profit(self):
        # вложено $3, хотим +$0.5, берём по 0.40 => (3+0.5)/(1-0.4) = 5.833…
        n = ladder_shares(3.0, 0.5, 0.40)
        assert n == pytest.approx(5.8333, abs=1e-3)
        # проверяем экономику: выплата минус всё вложенное = заложенный плюс
        assert n * 1.0 - (3.0 + n * 0.40) == pytest.approx(0.5, abs=1e-9)

    def test_price_at_or_above_one_is_refused(self):
        assert ladder_shares(3.0, 0.5, 1.0) == 0.0
        assert ladder_shares(3.0, 0.5, 1.5) == 0.0

    def test_no_debt_still_buys_for_profit(self):
        assert ladder_shares(0.0, 0.5, 0.5) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
#  Вход
# ---------------------------------------------------------------------------
class TestEntry:
    def test_small_jump_enters_expensive_side(self, cfg):
        """Скачок $6 вверх, Up стоит 0.60 (>= сплита) — берём Up."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=6.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER
        assert a.outcome == "Up"
        assert a.track == TRACK_A
        assert a.limit_price == 0.60

    def test_small_jump_down_enters_down_side(self, cfg):
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=-7.0, up_ask=0.40, down_ask=0.61))
        assert a.kind == ENTER
        assert a.outcome == "Down"
        assert a.track == TRACK_A

    def test_small_jump_refused_on_cheap_side(self, cfg):
        """$6 вверх, но Up дешёвый (0.30): дешёвой стороне нужен $15."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=6.0, up_ask=0.30, down_ask=0.71))
        assert a.kind == NONE
        assert "15" in a.reason

    def test_big_jump_enters_cheap_side_near_target(self, cfg):
        """$16 вверх, Up дешёвый (0.30), до таргета $40 — входим."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=16.0, price=65_040.0, target=65_000.0,
                           up_ask=0.30, down_ask=0.71))
        assert a.kind == ENTER
        assert a.outcome == "Up"
        assert a.track == TRACK_B

    def test_big_jump_refused_far_from_target(self, cfg):
        """Тот же скачок, но до таргета $150 (> $100) — не входим."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=16.0, price=65_150.0, target=65_000.0,
                           up_ask=0.30, down_ask=0.71))
        assert a.kind == NONE
        assert "таргет" in a.reason

    def test_target_distance_ignored_on_expensive_side(self, cfg):
        """Фильтр $100 привязан к дешёвой дорожке, дорогую он не трогает."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=6.0, price=65_500.0, target=65_000.0,
                           up_ask=0.80, down_ask=0.21))
        assert a.kind == ENTER
        assert a.track == TRACK_A

    def test_no_entry_without_jump(self, cfg):
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=2.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE

    def test_no_entry_at_end_of_window(self, cfg):
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, left=3.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE
        assert "конец окна" in a.reason

    def test_no_entry_without_target_on_cheap_side(self, cfg):
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=16.0, target=None, up_ask=0.30, down_ask=0.71))
        assert a.kind == NONE

    def test_split_boundary_counts_as_expensive(self, cfg):
        """Ровно 0.51 — дорогая дорожка (сплит включительно)."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=6.0, up_ask=0.51, down_ask=0.50))
        assert a.kind == ENTER
        assert a.track == TRACK_A

    def test_cooldown_blocks_immediate_reentry(self, cfg):
        """Скользящее окно скачка ещё «горит» — второй вход не открываем."""
        s = JumpStrategy(cfg)
        s.record_entry("Up", 0.60, 1.66, 1.0, TRACK_A, t=100.0)
        s.record_sell(0, 1.20, t=100.5)
        a = s.on_tick(snap(t=101.0, jump=6.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE
        assert "пауза" in a.reason


# ---------------------------------------------------------------------------
#  Запас цены: справедливая вероятность против того, что просят
# ---------------------------------------------------------------------------
class TestEdgeFilter:
    """Скачок говорит КУДА пошла цена, но не говорит, не заложен ли он уже
    в процент. Это единственная проверка «не переплачиваем ли мы»."""

    @pytest.fixture()
    def ecfg(self, cfg):
        cfg.jump_min_edge_cents = 3.0
        cfg.jump_min_shift_cents = 0.0     # изолируем от соседнего фильтра
        return cfg

    def test_refuses_when_move_is_already_priced_in(self, ecfg):
        """Ситуация со скриншота: справедливо ~60¢, в книге просят 94¢."""
        s = JumpStrategy(ecfg)
        a = s.on_tick(snap(jump=9.0, price=63_434.31, target=63_429.71,
                           sigma=1.0, left=299.1,
                           up_ask=0.94, down_ask=0.11))
        assert a.kind == NONE
        assert "переплачиваем" in a.reason
        assert "60" in a.reason      # справедливая цена в тексте отказа

    def test_enters_when_side_is_underpriced(self, ecfg):
        """Та же цена монеты, но книга ещё не переоценила: Up просят 55¢.

        Берём ask ВЫШЕ сплита: иначе сработает правило дешёвой дорожки
        (нужен скачок $15), и тест проверял бы не то."""
        s = JumpStrategy(ecfg)
        a = s.on_tick(snap(jump=9.0, price=63_434.31, target=63_429.71,
                           sigma=1.0, left=299.1,
                           up_ask=0.55, down_ask=0.50))
        assert a.kind == ENTER
        assert a.outcome == "Up"

    def test_thin_edge_is_refused(self, ecfg):
        """Запас есть, но меньше порога — не лезем: спред съест."""
        s = JumpStrategy(ecfg)
        a = s.on_tick(snap(jump=9.0, price=63_434.31, target=63_429.71,
                           sigma=1.0, left=299.1,
                           up_ask=0.59, down_ask=0.46))
        assert a.kind == NONE
        assert "запас" in a.reason

    def test_works_for_down_side(self, ecfg):
        """Для Down справедливая цена = 1 − P(Up)."""
        s = JumpStrategy(ecfg)
        # цена НИЖЕ таргета -> Down справедливо дорогой, но просят дешевле
        a = s.on_tick(snap(jump=-9.0, price=63_425.0, target=63_429.71,
                           sigma=1.0, left=299.1,
                           up_ask=0.50, down_ask=0.55))
        assert a.kind == ENTER
        assert a.outcome == "Down"

    def test_disabled_by_zero(self, cfg):
        cfg.jump_min_edge_cents = 0.0
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, price=63_434.31, target=63_429.71,
                           sigma=1.0, left=299.1,
                           up_ask=0.94, down_ask=0.11))
        assert a.kind == ENTER      # без фильтра купил бы переоценённое

    def test_silent_without_sigma_or_target(self, ecfg):
        """Нет данных для модели — правило не применяется, а не блокирует."""
        s = JumpStrategy(ecfg)
        assert s.on_tick(snap(jump=9.0, sigma=None,
                              up_ask=0.60, down_ask=0.41)).kind == ENTER
        assert s._edge(snap(target=None, sigma=1.0), "Up", 0.60) is None


# ---------------------------------------------------------------------------
#  Подстройка порогов под живость рынка
# ---------------------------------------------------------------------------
class TestAdaptiveThresholds:
    """На разогнанном рынке тот же скачок двигает процент во столько же раз
    слабее — значит и порог входа должен вырасти во столько же раз."""

    @pytest.fixture()
    def acfg(self, cfg):
        cfg.jump_adaptive = True
        cfg.jump_sigma_ref = 0.9
        cfg.jump_min_shift_cents = 0.0     # изолируем от второго фильтра
        return cfg

    def test_scale_is_one_at_reference_vol(self, acfg):
        s = JumpStrategy(acfg)
        assert s.vol_scale(snap(sigma=0.9)) == pytest.approx(1.0)

    def test_scale_grows_linearly_with_vol(self, acfg):
        s = JumpStrategy(acfg)
        assert s.vol_scale(snap(sigma=1.8)) == pytest.approx(2.0)
        assert s.vol_scale(snap(sigma=4.5)) == pytest.approx(5.0)

    def test_scale_is_clamped(self, acfg):
        acfg.jump_scale_max = 3.0
        s = JumpStrategy(acfg)
        assert s.vol_scale(snap(sigma=90.0)) == pytest.approx(3.0)
        assert s.vol_scale(snap(sigma=0.01)) == pytest.approx(1.0)

    def test_scale_is_one_without_sigma(self, acfg):
        s = JumpStrategy(acfg)
        assert s.vol_scale(snap(sigma=None)) == 1.0

    def test_small_jump_rejected_on_lively_market(self, acfg):
        """$9 хватало в тихое воскресенье, но не при sigma 4.5."""
        s = JumpStrategy(acfg)
        a = s.on_tick(snap(jump=9.0, sigma=4.5, up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE
        assert "σ" in a.reason

    def test_same_jump_accepted_on_quiet_market(self, acfg):
        s = JumpStrategy(acfg)
        a = s.on_tick(snap(jump=9.0, sigma=0.9, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_big_jump_accepted_on_lively_market(self, acfg):
        """При sigma 4.5 порог ~$25 — скачок $30 проходит."""
        s = JumpStrategy(acfg)
        a = s.on_tick(snap(jump=30.0, sigma=4.5, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_target_distance_limit_scales_with_vol(self, acfg):
        """$100 от таргета: мертво при тихом рынке, живо при разогнанном."""
        s = JumpStrategy(acfg)
        quiet = s.max_target_distance(snap(sigma=0.9, left=200.0))
        lively = s.max_target_distance(snap(sigma=6.0, left=200.0))
        assert quiet < 100 < lively, f"тихо {quiet:.0f}, живо {lively:.0f}"

    def test_cheap_side_far_out_allowed_when_lively(self, acfg):
        s = JumpStrategy(acfg)
        # sigma 6 -> порог скачка ~$100, предел до таргета ~$255
        a = s.on_tick(snap(jump=120.0, price=65_100.0, target=65_000.0,
                           sigma=6.0, up_ask=0.30, down_ask=0.71))
        assert a.kind == ENTER
        assert a.track == TRACK_B

    def test_falls_back_to_dollar_limit_without_sigma(self, acfg):
        s = JumpStrategy(acfg)
        assert s.max_target_distance(snap(sigma=None)) == \
            acfg.jump_max_target_dist_usd


# ---------------------------------------------------------------------------
#  Фильтр «сдвинется ли процент вообще»
# ---------------------------------------------------------------------------
class TestSensitivityFilter:
    """Далеко от таргета исход раунда уже решён: процент не шелохнётся,
    сколько бы монета ни прыгала. Такие входы надо отсекать."""

    def test_blocks_entry_far_from_target(self, cfg):
        s = JumpStrategy(cfg)
        # 60$ от таргета при sigma 0.9 и 200с — это ~4.7 сигмы, процент мёртв
        a = s.on_tick(snap(jump=9.0, price=65_060.0, target=65_000.0,
                           sigma=0.9, up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE
        assert "процент" in a.reason

    def test_allows_entry_near_target(self, cfg):
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, price=65_002.0, target=65_000.0,
                           sigma=0.9, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_high_volatility_keeps_far_entries_alive(self, cfg):
        """При большой sigma те же $60 — уже меньше сигмы, процент живой."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, price=65_060.0, target=65_000.0,
                           sigma=6.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_disabled_by_zero_threshold(self, cfg):
        cfg.jump_min_shift_cents = 0.0
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, price=65_060.0, target=65_000.0,
                           sigma=0.9, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_skipped_when_sigma_unknown(self, cfg):
        """Нет волатильности — фильтр молчит, решают обычные пороги."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, price=65_060.0, target=65_000.0,
                           sigma=None, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER

    def test_end_of_round_far_out_is_dead(self, cfg):
        """10 секунд до конца и $20 от таргета — раунд кончен."""
        s = JumpStrategy(cfg)
        a = s.on_tick(snap(jump=9.0, left=10.0, price=65_020.0,
                           target=65_000.0, sigma=0.9,
                           up_ask=0.60, down_ask=0.41))
        assert a.kind == NONE


# ---------------------------------------------------------------------------
#  Удержание и лестница
# ---------------------------------------------------------------------------
class TestLadder:
    def _entered(self, cfg, price=0.60, track=TRACK_A, t=100.0):
        s = JumpStrategy(cfg)
        s.record_entry("Up", price, round(1.0 / price, 2), 1.0, track, t=t)
        return s

    def test_holds_while_above_split(self, cfg):
        s = self._entered(cfg)
        a = s.on_tick(snap(t=105.0, up_bid=0.58, up_ask=0.59,
                           down_bid=0.41, down_ask=0.42))
        assert a.kind == HOLD
        assert "до расчёта" in a.reason

    def test_grace_delays_the_ladder(self, cfg):
        """Один проваленный тик — ещё не повод добирать."""
        s = self._entered(cfg)
        a = s.on_tick(snap(t=105.0, up_bid=0.40, up_ask=0.41,
                           down_bid=0.59, down_ask=0.60))
        assert a.kind == HOLD
        assert "подтверждения" in a.reason

    def test_ladder_fires_after_grace(self, cfg):
        s = self._entered(cfg)
        s.on_tick(snap(t=105.0, up_bid=0.40, up_ask=0.41,
                       down_bid=0.59, down_ask=0.60))
        a = s.on_tick(snap(t=106.0, up_bid=0.40, up_ask=0.41,
                           down_bid=0.59, down_ask=0.60))
        assert a.kind == LADDER
        assert a.outcome == "Down"
        # вложен $1, хотим +$0.5, добираем по 0.60 => (1+0.5)/0.4 = 3.75 шэра
        assert a.shares == pytest.approx(3.75, abs=1e-6)

    def test_ladder_recovers_full_debt(self, cfg):
        """Экономика добора: если добор выигрывает, раунд в плюсе."""
        s = self._entered(cfg)
        s.on_tick(snap(t=105.0, up_bid=0.40, up_ask=0.41,
                       down_bid=0.59, down_ask=0.60))
        a = s.on_tick(snap(t=106.0, up_bid=0.40, up_ask=0.41,
                           down_bid=0.59, down_ask=0.60))
        spent_before = s.net_out
        payout = a.shares * 1.0
        assert payout - (spent_before + a.size_usdc) == pytest.approx(0.5, abs=1e-6)

    def test_ladder_stops_at_max_legs(self, cfg):
        cfg.jump_max_ladder_legs = 1
        s = self._entered(cfg)
        s.record_entry("Down", 0.60, 3.75, 2.25, TRACK_A, t=106.0)
        s.on_tick(snap(t=110.0, up_bid=0.59, up_ask=0.60,
                       down_bid=0.40, down_ask=0.41))
        a = s.on_tick(snap(t=111.0, up_bid=0.59, up_ask=0.60,
                           down_bid=0.40, down_ask=0.41))
        assert a.kind == HOLD
        assert "пределе" in a.reason

    def test_ladder_stops_at_round_cap(self, cfg):
        cfg.jump_max_round_usdc = 2.0
        s = self._entered(cfg)
        s.on_tick(snap(t=105.0, up_bid=0.40, up_ask=0.41,
                       down_bid=0.59, down_ask=0.60))
        a = s.on_tick(snap(t=106.0, up_bid=0.40, up_ask=0.41,
                           down_bid=0.59, down_ask=0.60))
        assert a.kind == HOLD
        assert "потолок раунда" in a.reason

    def test_ladder_refuses_too_expensive_leg(self, cfg):
        cfg.jump_max_leg_price = 0.90
        s = self._entered(cfg)
        s.on_tick(snap(t=105.0, up_bid=0.05, up_ask=0.06,
                       down_bid=0.94, down_ask=0.95))
        a = s.on_tick(snap(t=106.0, up_bid=0.05, up_ask=0.06,
                           down_bid=0.94, down_ask=0.95))
        assert a.kind == HOLD
        assert "потолка" in a.reason

    def test_cheap_leg_below_split_is_not_a_loss(self, cfg):
        """Дорожка B: вход по 0.05 сам ниже сплита — лестница НЕ срабатывает,
        пока нога не ушла ниже своей цены входа."""
        s = self._entered(cfg, price=0.05, track=TRACK_B)
        a = s.on_tick(snap(t=105.0, up_bid=0.05, up_ask=0.06,
                           down_bid=0.94, down_ask=0.95))
        assert a.kind == HOLD
        a = s.on_tick(snap(t=106.0, up_bid=0.05, up_ask=0.06,
                           down_bid=0.94, down_ask=0.95))
        assert a.kind == HOLD

    def test_cheap_leg_ladders_when_it_really_loses(self, cfg):
        cfg.jump_max_leg_price = 0.99
        s = self._entered(cfg, price=0.20, track=TRACK_B)
        s.on_tick(snap(t=105.0, up_bid=0.10, up_ask=0.11,
                       down_bid=0.89, down_ask=0.90))
        a = s.on_tick(snap(t=106.0, up_bid=0.10, up_ask=0.11,
                           down_bid=0.89, down_ask=0.90))
        assert a.kind == LADDER
        assert a.outcome == "Down"


# ---------------------------------------------------------------------------
#  Фиксация прибыли (дорожка B)
# ---------------------------------------------------------------------------
class TestTakeProfit:
    def _cheap(self, cfg, entry=0.05):
        s = JumpStrategy(cfg)
        s.record_entry("Up", entry, 20.0, 1.0, TRACK_B, t=100.0)
        return s

    def test_sells_near_deadline_when_in_profit(self, cfg):
        """Купил 0.05, стало 0.30, до конца 4 секунды — продаём."""
        s = self._cheap(cfg)
        a = s.on_tick(snap(t=290.0, left=4.0, up_bid=0.30, up_ask=0.31,
                           down_bid=0.69, down_ask=0.70))
        assert a.kind == SELL
        assert a.sell_outcome == "Up"
        assert a.limit_price == 0.30

    def test_no_sell_at_deadline_without_profit(self, cfg):
        s = self._cheap(cfg)
        a = s.on_tick(snap(t=290.0, left=4.0, up_bid=0.06, up_ask=0.07,
                           down_bid=0.93, down_ask=0.94))
        assert a.kind != SELL

    def test_sells_when_growth_stalls_and_flow_turns(self, cfg):
        """Рост кончился (движение не в нашу сторону) + поток против."""
        s = self._cheap(cfg)
        s.on_tick(snap(t=200.0, jump=8.0, up_bid=0.34, up_ask=0.35,
                       down_bid=0.65, down_ask=0.66))     # пик 0.34
        a = s.on_tick(snap(t=201.0, jump=-1.0, up_bid=0.30, up_ask=0.31,
                           down_bid=0.69, down_ask=0.70, up_flow=-0.5))
        assert a.kind == SELL
        assert "рост кончился" in a.reason

    def test_holds_while_still_growing(self, cfg):
        s = self._cheap(cfg)
        a = s.on_tick(snap(t=200.0, jump=8.0, up_bid=0.34, up_ask=0.35,
                           down_bid=0.65, down_ask=0.66, up_flow=0.4))
        assert a.kind == HOLD

    def test_expensive_track_is_not_taken_profit(self, cfg):
        """Дорожка A прибыль не фиксирует — она едет до расчёта."""
        s = JumpStrategy(cfg)
        s.record_entry("Up", 0.60, 1.66, 1.0, TRACK_A, t=100.0)
        a = s.on_tick(snap(t=290.0, left=4.0, up_bid=0.90, up_ask=0.91,
                           down_bid=0.09, down_ask=0.10))
        assert a.kind != SELL


# ---------------------------------------------------------------------------
#  Активное ведение: стоп, фиксация на обеих дорожках, возврат к торговле
# ---------------------------------------------------------------------------
class TestActiveManagement:
    """Нога не должна ехать до расчёта: выросла — забрать, пошла против —
    выйти, и в обоих случаях освободиться для следующей сделки."""

    @pytest.fixture()
    def mcfg(self, cfg):
        cfg.jump_manage_enabled = True
        cfg.jump_stop_cents = 1.0
        cfg.jump_stop_grace_s = 0.0
        cfg.jump_tp_min_gain = 0.10
        cfg.jump_tp_stall_s = 8.0
        return cfg

    def _bought(self, cfg, ask=0.50, bid=0.45, t=100.0, track=TRACK_A):
        s = JumpStrategy(cfg)
        s.record_entry("Up", ask, 2.0, ask * 2, track, t=t, entry_bid=bid)
        return s

    # -- стоп ---------------------------------------------------------------
    def test_stop_does_not_fire_on_the_spread(self, mcfg):
        """КЛЮЧЕВОЕ: сразу после покупки bid ниже ask на весь спред.

        Если бы стоп считался от уплаченного ask, он срабатывал бы в тот же
        тик на каждой сделке. Отсчёт идёт от бида на входе, поэтому пока
        рынок не двинулся — выхода нет.
        """
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=101.0, up_bid=0.45, up_ask=0.50,
                           down_bid=0.50, down_ask=0.55))
        assert a.kind != SELL, f"стоп сработал на спреде: {a.reason}"

    def test_stop_fires_when_bid_falls_a_cent(self, mcfg):
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=101.0, up_bid=0.44, up_ask=0.49,
                           down_bid=0.51, down_ask=0.56))
        assert a.kind == SELL
        assert "СТОП" in a.reason
        assert a.limit_price == 0.44

    def test_stop_reports_the_real_loss_including_spread(self, mcfg):
        """Стоп «в 1 цент» при спреде 5¢ стоит примерно 6 центов."""
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=101.0, up_bid=0.44, up_ask=0.49,
                           down_bid=0.51, down_ask=0.56))
        assert "-6¢" in a.reason, a.reason

    def test_stop_waits_for_grace(self, mcfg):
        mcfg.jump_stop_grace_s = 1.0
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=100.2, up_bid=0.44, up_ask=0.49))
        assert a.kind != SELL, "один тик не должен выбивать"
        a = s.on_tick(snap(t=101.5, up_bid=0.44, up_ask=0.49))
        assert a.kind == SELL

    def test_recovery_clears_the_pending_stop(self, mcfg):
        mcfg.jump_stop_grace_s = 1.0
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        s.on_tick(snap(t=100.2, up_bid=0.44, up_ask=0.49))
        s.on_tick(snap(t=100.4, up_bid=0.46, up_ask=0.51))   # отскочило
        a = s.on_tick(snap(t=100.6, up_bid=0.44, up_ask=0.49))
        assert a.kind != SELL, "отсчёт стопа должен был начаться заново"

    def test_stop_disabled_by_zero(self, mcfg):
        mcfg.jump_stop_cents = 0.0
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=101.0, up_bid=0.30, up_ask=0.35))
        assert a.kind != SELL

    # -- фиксация -----------------------------------------------------------
    def test_expensive_track_now_takes_profit(self, mcfg):
        """Раньше дорожка A ехала до расчёта. Теперь тоже фиксирует."""
        s = self._bought(mcfg, ask=0.50, bid=0.45, track=TRACK_A)
        # выросли до 0.75, движение встало, поток развернулся
        s.on_tick(snap(t=101.0, jump=5.0, up_bid=0.75, up_ask=0.80))
        a = s.on_tick(snap(t=102.0, jump=-1.0, up_bid=0.72, up_ask=0.77,
                           up_flow=-0.5))
        assert a.kind == SELL
        assert "ФИКСИРУЮ" in a.reason

    def test_sells_when_percent_stops_rising(self, mcfg):
        """«Выросло до 0.75 и стоит» — прямой случай из задачи.

        Ни отката, ни разворота потока: процент просто замер. Через
        jump_tp_stall_s секунд без нового пика — забираем.
        """
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        s.on_tick(snap(t=101.0, jump=5.0, up_bid=0.75, up_ask=0.80))
        # ещё 5с — рано
        a = s.on_tick(snap(t=106.0, jump=5.0, up_bid=0.75, up_ask=0.80))
        assert a.kind != SELL
        # 9с без нового пика — рост встал
        a = s.on_tick(snap(t=110.0, jump=5.0, up_bid=0.75, up_ask=0.80))
        assert a.kind == SELL
        assert "рост встал" in a.reason

    def test_growth_that_continues_is_not_cut(self, mcfg):
        """Пока пик обновляется, позицию не трогаем."""
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        for i, b in enumerate([0.60, 0.66, 0.72, 0.78, 0.84]):
            a = s.on_tick(snap(t=101.0 + i * 3, jump=5.0,
                               up_bid=b, up_ask=b + 0.05))
            assert a.kind != SELL, f"срезали растущую позицию на {b}"

    def test_no_take_profit_while_in_loss(self, mcfg):
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=120.0, jump=0.0, up_bid=0.46, up_ask=0.51))
        assert a.kind != SELL, "0.46 против входа 0.50 — это не прибыль"

    # -- возврат к торговле --------------------------------------------------
    def test_free_to_trade_again_after_selling(self, mcfg):
        """После выхода бот не должен простаивать до конца раунда."""
        s = self._bought(mcfg, ask=0.50, bid=0.45)
        a = s.on_tick(snap(t=101.0, up_bid=0.44, up_ask=0.49))
        assert a.kind == SELL
        s.record_sell(a.sell_idx, 0.88, t=101.0)
        assert s.legs == []
        # пауза после сделки прошла — снова ищем вход
        a = s.on_tick(snap(t=103.0, jump=6.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER, a.reason


# ---------------------------------------------------------------------------
#  Учёт денег и границы раунда
# ---------------------------------------------------------------------------
class TestBookkeeping:
    def test_net_out_tracks_buys_and_sells(self, cfg):
        s = JumpStrategy(cfg)
        s.record_entry("Up", 0.50, 2.0, 1.0, TRACK_A, t=100.0)
        assert s.net_out == pytest.approx(1.0)
        assert s.debt == pytest.approx(1.0)
        s.record_sell(0, 1.60, t=101.0)
        assert s.net_out == pytest.approx(-0.60)
        assert s.debt == 0.0            # раунд в плюсе — перекрывать нечего

    def test_depth_resets_after_profitable_flat(self, cfg):
        s = JumpStrategy(cfg)
        s.record_entry("Up", 0.50, 2.0, 1.0, TRACK_A, t=100.0)
        s.record_entry("Down", 0.50, 4.0, 2.0, TRACK_A, t=101.0)
        assert s.depth == 1
        s.record_sell(0, 0.10, t=102.0)
        s.record_sell(1, 5.00, t=103.0)
        assert s.depth == 0

    def test_reset_round_clears_everything(self, cfg):
        s = JumpStrategy(cfg)
        s.record_entry("Up", 0.60, 1.66, 1.0, TRACK_A, t=100.0)
        s.reset_round()
        assert s.legs == []
        assert s.net_out == 0.0
        assert s.depth == 0
        a = s.on_tick(snap(jump=6.0, up_ask=0.60, down_ask=0.41))
        assert a.kind == ENTER      # пауза после сделки тоже сброшена
