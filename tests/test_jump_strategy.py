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
    return c


def snap(t=100.0, left=200.0, price=65_000.0, target=65_000.0, jump=0.0,
         up_bid=None, up_ask=None, down_bid=None, down_ask=None,
         up_flow=0.0, down_flow=0.0) -> JumpSnapshot:
    return JumpSnapshot(
        t=t, seconds_left=left, coin_price=price, target=target,
        jump_usd=jump, up_bid=up_bid, up_ask=up_ask,
        down_bid=down_bid, down_ask=down_ask,
        up_flow=up_flow, down_flow=down_flow,
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
