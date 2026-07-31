"""Разворот как ПОВТОРНЫЙ ВХОД, взвешенная Q, возраст импульса, книга, журнал.

Прежняя лестница молча принимала, что убыток по одной стороне —
подтверждение другой. Это неверно: рынок мог просто встать, и тогда плохи
ОБЕ стороны, а автоматический переворот удваивал число сделок и риск.
"""
from __future__ import annotations

import json
import os

import pytest

from flowbot.bookfeat import BookTracker
from flowbot.config import FlowConfig
from flowbot.jump import (ENTER, HOLD, LADDER, NONE, SELL, JumpSnapshot,
                          JumpStrategy)
from flowbot.stats import TradeJournal, read_all


def cfg_imp(**over) -> FlowConfig:
    c = FlowConfig()
    c.jump_entry_mode = "impulse"
    c.jump_adaptive = False
    c.jump_stop_loss = 0.0            # тесты про разворот, не про стоп
    c.jump_ladder_grace_s = 0.6
    c.jump_ladder_loss = 0.02
    c.jump_min_shift_cents = 0.0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def isnap(t=100.0, jump=6.0, speed=3.0, accel=1.5, hold=1.0, age=1.0,
          sigma=1.0, left=200.0, price=65_003.0, target=65_000.0,
          up_bid=0.54, up_ask=0.55, down_bid=0.45, down_ask=0.46,
          up_imb=None, down_imb=None, up_wall=None,
          entries_paused=False) -> JumpSnapshot:
    return JumpSnapshot(
        t=t, seconds_left=left, coin_price=price, target=target,
        jump_usd=jump, sigma_1s=sigma, speed=speed, accel=accel,
        imp_age_s=age, imp_hold=hold,
        up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
        up_imb=up_imb, down_imb=down_imb, up_wall=up_wall,
        entries_paused=entries_paused)


# ---------------------------------------------------------------------------
#  Разворот больше не покупает вслепую
# ---------------------------------------------------------------------------
class TestLadderReenters:
    def _in_position(self, c, entry_bid=0.54):
        s = JumpStrategy(c)
        s.record_entry("Up", 0.55, 2.0, 1.10, "Q", t=0.0, entry_bid=entry_bid)
        return s

    def test_reversal_only_sells(self):
        """Ногу закрываем, обратную сторону НЕ покупаем тем же действием."""
        s = self._in_position(cfg_imp())
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))       # взводим
        act = s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))  # выдержка
        assert act.kind == SELL
        assert act.sell_outcome == "Up"
        assert act.outcome is None, "покупки в этом действии быть не должно"
        assert "РАЗВОРОТ" in act.reason

    def test_opposite_side_must_pass_all_gates(self):
        """После разворота слабый сигнал обратной стороны НЕ покупается.

        Это и есть суть правки: раньше он покупался автоматически, просто
        потому что первая нога провалилась.
        """
        c = cfg_imp()
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        s.record_sell(0, 1.04, t=2.0)

        # Обратная сторона: движения нет вовсе — ворота не пройдены.
        act = s.on_tick(isnap(t=2.5, jump=0.3, speed=0.1, accel=0.1))
        assert act.kind == NONE
        assert s.legs == [], "позиции быть не должно"

    def test_reentry_needs_a_higher_bar(self):
        """Планка повторного входа выше обычной."""
        c = cfg_imp(jump_imp_min_score=1.0, jump_ladder_min_q=1.5,
                    jump_ladder_strict_s=30.0)
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        s.record_sell(0, 1.04, t=2.0)

        # Сигнал, которого хватило бы для ОБЫЧНОГО входа (Q около 1.2),
        # но мало для повторного.
        weak = isnap(t=3.0, jump=2.2, speed=1.1, accel=0.8, hold=0.75,
                     down_bid=0.44, down_ask=0.45)
        act = s.on_tick(weak)
        assert act.kind == NONE
        assert "планка поднята" in act.reason

    def test_bar_returns_to_normal_after_the_window(self):
        c = cfg_imp(jump_ladder_strict_s=30.0)
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        s.record_sell(0, 1.04, t=2.0)

        need_early, why_early = s.min_quality(isnap(t=5.0))
        need_late, why_late = s.min_quality(isnap(t=40.0))
        assert need_early > need_late
        assert why_early and not why_late

    def test_strong_opposite_signal_still_enters(self):
        """Строгая планка — не запрет. Сильный сигнал проходит."""
        c = cfg_imp()
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        s.record_sell(0, 1.04, t=2.0)

        act = s.on_tick(isnap(t=3.0, jump=-12.0, speed=-9.0, accel=2.5,
                              hold=1.0, price=64_990.0,
                              down_bid=0.54, down_ask=0.55))
        assert act.kind == ENTER
        assert act.outcome == "Down"

    def test_new_round_clears_the_strict_bar(self):
        c = cfg_imp()
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        s.reset_round()
        need, why = s.min_quality(isnap(t=3.0))
        assert need == c.jump_imp_min_score and not why

    def test_pause_does_not_block_the_reversal(self):
        """Разворот теперь ПРОДАЖА, а продажи пауза не держит никогда."""
        s = self._in_position(cfg_imp())
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53, entries_paused=True))
        act = s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53,
                              entries_paused=True))
        assert act.kind == SELL

    def test_old_ladder_still_available(self):
        """Прежний переворот никуда не делся — он под флагом."""
        c = cfg_imp(jump_ladder_reenters=False)
        s = self._in_position(c)
        s.on_tick(isnap(t=1.0, up_bid=0.52, up_ask=0.53))
        act = s.on_tick(isnap(t=2.0, up_bid=0.52, up_ask=0.53))
        assert act.kind == LADDER
        assert act.outcome == "Down"


# ---------------------------------------------------------------------------
#  Взвешенная оценка качества
# ---------------------------------------------------------------------------
class TestWeightedQuality:
    def _q(self, act) -> float:
        return act.feat["q"]

    def test_weights_change_the_score(self):
        """Один и тот же рынок при разных весах даёт разное Q."""
        snap = isnap(jump=8.0, speed=2.2, accel=1.5, hold=1.0)
        fast = JumpStrategy(cfg_imp(jump_w_speed=1.0, jump_w_edge=0.0,
                                    jump_w_jump=0.0, jump_w_shift=0.0))
        big = JumpStrategy(cfg_imp(jump_w_speed=0.0, jump_w_edge=0.0,
                                   jump_w_jump=1.0, jump_w_shift=0.0))
        a, b = fast.on_tick(snap), big.on_tick(snap)
        assert a.kind == ENTER and b.kind == ENTER
        # Ход 8σ против скорости 2.2σ/с — вес решает, что важнее.
        assert self._q(b) > self._q(a)

    def test_zero_weights_fall_back_to_plain_average(self):
        snap = isnap(jump=8.0, speed=2.2, accel=1.5, hold=1.0)
        zero = JumpStrategy(cfg_imp(jump_w_speed=0.0, jump_w_edge=0.0,
                                    jump_w_jump=0.0, jump_w_shift=0.0,
                                    jump_w_book=0.0))
        act = zero.on_tick(snap)
        assert act.kind == ENTER
        parts = act.feat["q_parts"]
        assert self._q(act) == pytest.approx(sum(parts.values()) / len(parts),
                                             abs=1e-6)

    def test_components_are_recorded(self):
        act = JumpStrategy(cfg_imp()).on_tick(isnap())
        assert act.kind == ENTER
        assert set(act.feat["q_parts"]) >= {"speed", "edge", "jump"}


# ---------------------------------------------------------------------------
#  Возраст импульса
# ---------------------------------------------------------------------------
class TestImpulseAge:
    def test_old_impulse_is_refused(self):
        """Движение идёт 20с: все фильтры пройдут, а покупать нечего."""
        act = JumpStrategy(cfg_imp(jump_imp_max_age_s=8.0)).on_tick(
            isnap(age=20.0))
        assert act.kind == NONE
        assert "идёт уже" in act.reason

    def test_fresh_impulse_passes(self):
        act = JumpStrategy(cfg_imp(jump_imp_max_age_s=8.0)).on_tick(
            isnap(age=2.0))
        assert act.kind == ENTER

    def test_zero_disables_the_limit(self):
        act = JumpStrategy(cfg_imp(jump_imp_max_age_s=0.0)).on_tick(
            isnap(age=60.0))
        assert act.kind == ENTER


# ---------------------------------------------------------------------------
#  Структура книги
# ---------------------------------------------------------------------------
class _FakeBook:
    have_snapshot = True

    def __init__(self, bids, asks):
        self._b, self._a = bids, asks

    def top(self, side, depth):
        src = self._b if side == "bid" else self._a
        return sorted(src.items(), reverse=(side == "bid"))[:depth]


class TestBookFeatures:
    def test_imbalance_sign(self):
        tr = BookTracker(levels=3)
        buyers = _FakeBook({0.54: 900.0, 0.53: 600.0}, {0.55: 100.0})
        st = tr.state(buyers)
        assert st.imbalance > 0.5, "бидов втрое больше — перекос вверх"

        sellers = _FakeBook({0.54: 100.0}, {0.55: 900.0, 0.56: 600.0})
        assert tr.state(sellers).imbalance < -0.5

    def test_wall_detects_one_big_order(self):
        tr = BookTracker(levels=3)
        wall = _FakeBook({0.54: 100.0}, {0.55: 5000.0, 0.56: 50.0})
        assert tr.state(wall).wall > 0.95
        spread = _FakeBook({0.54: 100.0}, {0.55: 300.0, 0.56: 300.0})
        assert tr.state(spread).wall == pytest.approx(0.5)

    def test_trend_is_zero_without_enough_history(self):
        """«Не знаю» и «не изменилось» — разные ответы."""
        tr = BookTracker(levels=3, window_s=3.0)
        b = _FakeBook({0.54: 100.0}, {0.55: 100.0})
        tr.feed(0.0, b)
        assert tr.state(b).ask_trend == 0.0

    def test_ask_liquidity_leaving_shows_as_negative_trend(self):
        """Заявки на пути движения СНЯЛИ — тренд обязан это показать.

        Сравнение идёт с точкой ровно `window_s` назад, поэтому падение
        должно попасть ВНУТРЬ окна: уход ликвидности трёхсекундной давности
        при окне в 2с — уже история, а не событие.
        """
        tr = BookTracker(levels=3, window_s=2.0)
        for i in range(20):                       # t 0.0-3.8: стоит 1000
            tr.feed(i * 0.2, _FakeBook({0.54: 100.0}, {0.55: 1000.0}))
        for i in range(20, 24):                   # t 4.0-4.6: осталось 200
            tr.feed(i * 0.2, _FakeBook({0.54: 100.0}, {0.55: 200.0}))
        st = tr.state(_FakeBook({0.54: 100.0}, {0.55: 200.0}))
        assert st.ask_trend < -0.5, "ask-ликвидность ушла"

    def test_old_change_falls_out_of_the_window(self):
        """То же падение, но давнее, трендом уже не считается."""
        tr = BookTracker(levels=3, window_s=2.0)
        for i in range(20):
            tr.feed(i * 0.2, _FakeBook({0.54: 100.0}, {0.55: 1000.0}))
        for i in range(20, 40):                   # прошло ещё 4 секунды
            tr.feed(i * 0.2, _FakeBook({0.54: 100.0}, {0.55: 200.0}))
        st = tr.state(_FakeBook({0.54: 100.0}, {0.55: 200.0}))
        assert st.ask_trend == pytest.approx(0.0)

    def test_empty_book_gives_nothing(self):
        assert BookTracker().state(_FakeBook({}, {})) is None
        assert BookTracker().state(None) is None


class TestBookGates:
    def test_gates_are_off_by_default(self):
        """Признаки считаются, но НЕ фильтруют, пока не измерены."""
        act = JumpStrategy(cfg_imp()).on_tick(isnap(up_imb=-0.9, up_wall=0.99))
        assert act.kind == ENTER

    def test_imbalance_gate_blocks_when_enabled(self):
        act = JumpStrategy(cfg_imp(jump_book_min_imbalance=0.2)).on_tick(
            isnap(up_imb=-0.5))
        assert act.kind == NONE
        assert "книга не подтверждает" in act.reason

    def test_wall_gate_blocks_when_enabled(self):
        act = JumpStrategy(cfg_imp(jump_book_max_wall=0.8)).on_tick(
            isnap(up_imb=0.5, up_wall=0.95))
        assert act.kind == NONE
        assert "стена" in act.reason

    def test_book_is_always_recorded(self):
        act = JumpStrategy(cfg_imp()).on_tick(isnap(up_imb=0.4, up_wall=0.3))
        assert act.feat["book_imb"] == pytest.approx(0.4)
        assert act.feat["book_wall"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
#  Журнал сделок
# ---------------------------------------------------------------------------
class TestJournal:
    def test_appends_and_reads_back(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        j = TradeJournal(str(p))
        assert j.append({"pnl": 0.1, "q": 1.5, "mode": "DRY"})
        assert j.append({"pnl": -0.2, "q": 2.5, "mode": "LIVE"})
        rows = read_all(str(p))
        assert len(rows) == 2
        assert rows[0]["q"] == 1.5 and rows[1]["mode"] == "LIVE"
        assert all("schema" in r and "ts" in r for r in rows)

    def test_never_overwrites(self, tmp_path):
        """Новая версия бота не имеет права стереть старую историю."""
        p = tmp_path / "trades.jsonl"
        TradeJournal(str(p)).append({"pnl": 1.0})
        TradeJournal(str(p)).append({"pnl": 2.0})   # «новая сборка»
        assert len(read_all(str(p))) == 2

    def test_creates_missing_directory(self, tmp_path):
        p = tmp_path / "deep" / "nested" / "trades.jsonl"
        assert TradeJournal(str(p)).append({"pnl": 1.0})
        assert os.path.exists(p)

    def test_broken_line_does_not_break_reading(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        TradeJournal(str(p)).append({"pnl": 1.0})
        with open(p, "a", encoding="utf-8") as f:
            f.write("{это не json\n")
        TradeJournal(str(p)).append({"pnl": 2.0})
        rows = read_all(str(p))
        assert [r["pnl"] for r in rows] == [1.0, 2.0]

    def test_unwritable_path_does_not_raise(self):
        j = TradeJournal("/proc/definitely/not/writable/trades.jsonl")
        assert j.append({"pnl": 1.0}) is False      # молча, без исключения

    def test_disabled_journal_writes_nothing(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        assert TradeJournal(str(p), enabled=False).append({"pnl": 1.0}) is False
        assert not os.path.exists(p)

    def test_default_path_is_outside_the_project(self):
        """Переезд на новую сборку не должен обнулять историю."""
        from flowbot.stats import default_path
        p = default_path()
        assert os.path.isabs(p)
        assert p.startswith(os.path.expanduser("~"))
