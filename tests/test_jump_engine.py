"""Оффлайн тесты движка скачковой системы (без сети, без ключей, без денег).

Проверяем реальный путь исполнения: скачок цены -> покупка ноги -> просадка
-> добор лестницы -> расчёт в конце окна. Трейдер — DryRunTrader, фиды не
поднимаем: цену и книгу наполняем вручную.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fast_monitor
from flowbot.config import FlowConfig
from flowbot.jump_engine import JumpEngine


def _engine(**over):
    cfg = FlowConfig()
    cfg.dry_run = True
    cfg.simulate_latency = False        # тесты не ждут пинг
    cfg.trade_log_csv = ""              # не писать CSV в тестах
    cfg.jump_ladder_grace_s = 0.0       # без ожидания подтверждения
    for k, v in over.items():
        setattr(cfg, k, v)
    eng = JumpEngine(cfg)
    eng.market = {
        "up": "UPTOK", "down": "DNTOK",
        "window_ts": int(time.time()), "end_ts": time.time() + 200.0,
        "slug": "btc-updown-5m-test",
    }
    eng.book.set_tokens("UPTOK", "DNTOK")
    return eng, cfg


def _book(eng, up_bid, up_ask, dn_bid, dn_ask):
    eng.book.on_event({"event_type": "book", "asset_id": "UPTOK",
                       "bids": [{"price": str(up_bid), "size": "100"}],
                       "asks": [{"price": str(up_ask), "size": "100"}]})
    eng.book.on_event({"event_type": "book", "asset_id": "DNTOK",
                       "bids": [{"price": str(dn_bid), "size": "100"}],
                       "asks": [{"price": str(dn_ask), "size": "100"}]})


def _set_jump(eng, price, dprice, horizon=3.0):
    """Заставить PriceEngine показать скачок dprice долларов за horizon сек."""
    pe = eng.price
    pe._price = price
    pe._sigma = 5.0
    t = time.time()
    pe.hist.clear()
    pe.hist.append((t - horizon, price - dprice))
    pe.hist.append((t - horizon / 2, price - dprice / 2))
    pe.hist.append((t, price))


def _set_target(value, offset=0.0):
    """Подсунуть таргет текущего раунда, как это делает fast_monitor."""
    now = time.time()
    fast_monitor.ROUND_TARGET.update(
        start_ts=int(now - now % 300), value=value, exact=True)
    return value + offset


async def _drain(eng, timeout=4.0):
    """Дождаться, пока запланированный _execute отработает."""
    for _ in range(int(timeout / 0.02)):
        if not eng.busy:
            return
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
#  move_usd — долларовый скачок вместо σ
# ---------------------------------------------------------------------------
def test_move_usd_reads_dollar_jump():
    eng, cfg = _engine()
    _set_jump(eng, price=65_000.0, dprice=12.0, horizon=cfg.jump_window_s)
    usd, bps = eng.price.move_usd(cfg.jump_window_s)
    assert usd == pytest.approx(12.0, abs=0.01)
    assert bps == pytest.approx(12.0 / 65_000.0 * 1e4, abs=0.01)


def test_move_usd_without_history_is_zero():
    eng, cfg = _engine()
    assert eng.price.move_usd(cfg.jump_window_s) == (0.0, 0.0)


# ---------------------------------------------------------------------------
#  Вход
# ---------------------------------------------------------------------------
def test_tick_enters_expensive_side_on_small_jump():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1, "должна была открыться нога"
        leg = eng.legs[0]
        assert leg["outcome"] == "Up"
        assert leg["track"] == "A"
        assert leg["entry_price"] == 0.60
        assert eng.strategy.net_out == pytest.approx(leg["cost"])
    asyncio.run(scenario())


def test_tick_skips_cheap_side_far_from_target():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.29, 0.30, 0.70, 0.71)
        # скачок $16 вверх, но цена уже в $150 от таргета
        _set_jump(eng, price=65_150.0, dprice=16.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert eng.legs == []
    asyncio.run(scenario())


def test_tick_enters_cheap_side_near_target():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.29, 0.30, 0.70, 0.71)
        _set_jump(eng, price=65_040.0, dprice=16.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        assert eng.legs[0]["track"] == "B"
        assert eng.legs[0]["outcome"] == "Up"
    asyncio.run(scenario())


def test_stale_target_is_ignored():
    """Таргет от прошлого раунда не должен проходить фильтр дистанции."""
    eng, _ = _engine()
    fast_monitor.ROUND_TARGET.update(start_ts=0, value=65_000.0, exact=True)
    assert eng._target() is None


# ---------------------------------------------------------------------------
#  Лестница
# ---------------------------------------------------------------------------
def test_ladder_buys_opposite_side_to_cover_loss():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        spent = eng.strategy.net_out

        # Up рухнул ниже сплита — движок должен добрать Down.
        _book(eng, 0.40, 0.41, 0.59, 0.60)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 2, "лестница должна была добрать"
        add = eng.legs[1]
        assert add["outcome"] == "Down"
        # Если добор выигрывает, раунд в плюсе: шэры * $1 > всё вложенное.
        assert add["shares"] * 1.0 > eng.strategy.net_out
        assert eng.strategy.net_out > spent
    asyncio.run(scenario())


def test_ladder_respects_round_cap():
    async def scenario():
        eng, cfg = _engine(jump_max_round_usdc=1.5)
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        _book(eng, 0.40, 0.41, 0.59, 0.60)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1, "потолок раунда должен был остановить добор"
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
#  Расчёт в конце окна
# ---------------------------------------------------------------------------
def test_settle_pays_only_the_winning_side():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        _book(eng, 0.40, 0.41, 0.59, 0.60)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 2
        up_leg = eng.legs[0]
        dn_leg = eng.legs[1]

        # Раунд закрылся по Down: Down -> 1.0, Up -> 0.
        _book(eng, 0.00, 0.01, 0.99, 1.00)
        eng._tick()
        await _drain(eng)
        spent = eng.strategy.net_out
        eng._settle_open_position("тест")

        assert eng.legs == []
        # выплата = только шэры Down; P&L = выплата - вложено
        expected = round(dn_leg["shares"] * 1.0, 2) - spent
        assert eng.realized_pnl == pytest.approx(expected, abs=0.02)
        assert expected > 0, "добор обязан вытащить раунд в плюс"
        assert up_leg["cost"] > 0
    asyncio.run(scenario())


def test_settle_without_legs_is_noop():
    eng, _ = _engine()
    eng._settle_open_position("пусто")
    assert eng.realized_pnl == 0.0


def test_new_window_resets_the_ladder():
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert eng.strategy.net_out > 0
        eng._enter_window({"up": "UPTOK", "down": "DNTOK",
                           "window_ts": int(time.time()) + 300,
                           "slug": "next-window"})
        assert eng.strategy.net_out == 0.0
        assert eng.strategy.legs == []
    asyncio.run(scenario())
