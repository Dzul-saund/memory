"""Юнит-тесты чистого ядра стратегии flowbot (без сети, без ключей)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.config import FlowConfig
from flowbot.strategy import (
    ENTER, EXIT, FLIP, HOLD, NONE, FlowStrategy, MarketSnapshot,
)


def cfg(**over):
    c = FlowConfig()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def snap(**kw):
    base = dict(t=0.0, seconds_left=200.0, btc_price=100_000.0,
                burst_z=0.0, move_bps=0.0)
    base.update(kw)
    return MarketSnapshot(**base)


# --- вход -------------------------------------------------------------------

def test_no_burst_no_entry():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=1.0, move_bps=0.5, up_ask=0.60, down_ask=0.42))
    assert a.kind == NONE


def test_sharp_up_enters_up():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=3.0, move_bps=2.0,
                       up_ask=0.60, up_bid=0.59,
                       down_ask=0.42, down_bid=0.41))
    assert a.kind == ENTER
    assert a.outcome == "Up"
    assert a.size_usdc == 1.0


def test_sharp_down_enters_down():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=-3.0, move_bps=-2.0,
                       up_ask=0.42, down_ask=0.58, down_bid=0.57))
    assert a.kind == ENTER
    assert a.outcome == "Down"


def test_entry_blocked_above_band():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=3.0, move_bps=2.0, up_ask=0.95, down_ask=0.05))
    assert a.kind == NONE and "вне диапазона" in a.reason


def test_chase_within_1_2_cents_ok():
    # Down был 0.56 до скачка, сейчас 0.58 -> берём (разница 2 цента).
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=-3.0, move_bps=-2.0,
                       down_ask=0.58, down_ask_ref=0.56, down_bid=0.57))
    assert a.kind == ENTER
    assert a.limit_price == 0.58     # ref + chase_cents(0.02)


def test_chase_too_far_skips():
    # Книга ушла: ask 0.60 > 0.56 + 0.02 -> пропускаем.
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=-3.0, move_bps=-2.0,
                       down_ask=0.60, down_ask_ref=0.56, down_bid=0.59))
    assert a.kind == NONE and "книга ушла" in a.reason


def test_flow_veto_blocks_entry():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=3.0, move_bps=2.0, up_ask=0.60, up_bid=0.59,
                       up_flow=-0.9))
    assert a.kind == NONE and "поток" in a.reason


def test_no_new_entry_near_window_end():
    s = FlowStrategy(cfg())
    a = s.on_tick(snap(burst_z=3.0, move_bps=2.0, up_ask=0.60, seconds_left=3.0))
    assert a.kind == NONE and "конец окна" in a.reason


# --- удержание / выход ------------------------------------------------------

def test_hold_while_momentum_and_pct_rising():
    s = FlowStrategy(cfg())
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    a = s.on_tick(snap(t=1.0, burst_z=2.0, move_bps=1.0, up_bid=0.62, up_ask=0.63))
    assert a.kind == HOLD
    assert s.position.peak_bid == 0.62


def test_exit_when_momentum_fades_after_grace():
    c = cfg(momentum_fade_grace_s=0.8)
    s = FlowStrategy(c)
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    # momentum still up -> hold, peak 0.62
    s.on_tick(snap(t=1.0, burst_z=2.0, up_bid=0.62, up_ask=0.63))
    # momentum gone -> начинается фейд (hold)
    a1 = s.on_tick(snap(t=2.0, burst_z=0.0, up_bid=0.62, up_ask=0.63))
    assert a1.kind == HOLD
    # спустя grace -> выход
    a2 = s.on_tick(snap(t=3.0, burst_z=0.0, up_bid=0.62, up_ask=0.63))
    assert a2.kind == EXIT
    assert a2.sell_outcome == "Up"


def test_exit_on_token_retrace():
    c = cfg(momentum_fade_grace_s=0.0)   # выходим сразу по сломанному условию
    s = FlowStrategy(c)
    s.record_entry("Up", 0.60, 1.0, t=0.0)
    s.on_tick(snap(t=1.0, burst_z=2.0, up_bid=0.70, up_ask=0.71))   # peak 0.70
    # процент откатился ниже пика на > retrace(0.03), хотя burst ещё есть
    a = s.on_tick(snap(t=2.0, burst_z=2.0, up_bid=0.66, up_ask=0.67))
    assert a.kind == EXIT and "откатился" in a.reason


def test_min_hold_prevents_immediate_exit():
    s = FlowStrategy(cfg(min_hold_s=0.4))
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    a = s.on_tick(snap(t=0.1, burst_z=-1.0, up_bid=0.55, up_ask=0.56))
    assert a.kind == HOLD and "мин. удержание" in a.reason


# --- разворот (предосторожность №2) -----------------------------------------

def test_flip_on_strong_reversal():
    s = FlowStrategy(cfg(flip_burst_z=3.5, flip_size_usdc=3.5))
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    a = s.on_tick(snap(t=1.0, burst_z=-4.0, move_bps=-3.0,
                       up_bid=0.50, down_ask=0.50, down_bid=0.49))
    assert a.kind == FLIP
    assert a.outcome == "Down"          # берём противоположную
    assert a.sell_outcome == "Up"       # свою продаём
    assert a.size_usdc == 3.5


def test_no_flip_if_reversal_weak():
    s = FlowStrategy(cfg(flip_burst_z=3.5))
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    # против нас, но слабо (|z| < flip_burst_z) -> не разворот
    a = s.on_tick(snap(t=1.0, burst_z=-1.0, move_bps=-1.0, up_bid=0.57, up_ask=0.58))
    assert a.kind != FLIP


# --- крайние случаи ---------------------------------------------------------

def test_deep_itm_holds_to_settlement():
    s = FlowStrategy(cfg(take_profit_price=0.95))
    s.record_entry("Up", 0.80, 1.0, t=0.0)
    a = s.on_tick(snap(t=1.0, burst_z=-0.5, up_bid=0.96, up_ask=0.97))
    assert a.kind == HOLD and "глубоко" in a.reason


def test_window_end_winning_holds():
    s = FlowStrategy(cfg())
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    a = s.on_tick(snap(t=10.0, seconds_left=3.0, burst_z=0.0, up_bid=0.72, up_ask=0.73))
    assert a.kind == HOLD


def test_window_end_losing_exits():
    s = FlowStrategy(cfg())
    s.record_entry("Up", 0.58, 1.0, t=0.0)
    a = s.on_tick(snap(t=10.0, seconds_left=3.0, burst_z=0.0, up_bid=0.30, up_ask=0.31))
    assert a.kind == EXIT
