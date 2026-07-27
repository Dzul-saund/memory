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
    cfg.jump_adaptive = False           # пороги не плавают: тесты про лестницу
    cfg.jump_min_shift_cents = 0.0      # и не про фильтр чувствительности
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


def _set_jump(eng, price, dprice, horizon=3.0, steps=16):
    """Показать движение на dprice долларов за horizon секунд.

    Наполняет ОБА механизма детекции — историю для move_usd ("window") и
    экстремумы для swing — одной и той же серией цен, чтобы тест не зависел
    от того, какой режим включён.
    """
    pe = eng.price
    pe._sigma = 5.0
    pe.hist.clear()
    pe._lo.clear()
    pe._hi.clear()
    pe._sm3.clear()
    t0 = time.time() - horizon
    for i in range(steps + 1):
        p = price - dprice + dprice * i / steps
        ts = t0 + horizon * i / steps
        pe._price = p
        pe.hist.append((ts, p))
        pe._update_swing(ts, p)
    pe._price = price


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
    """Движение читается в долларах.

    На РАВНОМЕРНОМ разгоне значение чуть меньше полного размаха: концы окна
    берутся медианой узких подокон, а те лежат внутри краёв. Для детекции это
    неважно (порог сравнивается с той же величиной), поэтому проверяем полосу,
    а не точное число.
    """
    eng, cfg = _engine()
    _set_jump(eng, price=65_000.0, dprice=12.0, horizon=cfg.jump_window_s)
    usd, bps = eng.price.move_usd(cfg.jump_window_s)
    assert 10.5 <= usd <= 12.0
    assert bps == pytest.approx(usd / 65_000.0 * 1e4, abs=0.01)


def test_move_usd_without_history_is_zero():
    eng, cfg = _engine()
    assert eng.price.move_usd(cfg.jump_window_s) == (0.0, 0.0)


# ---------------------------------------------------------------------------
#  swing — движение от локального экстремума, без окна
# ---------------------------------------------------------------------------
def _feed(eng, prices, dt=0.2):
    """Прогнать серию цен через PriceEngine, как будто идут тики.

    Наполняет и hist (для move_usd), и экстремумы (для swing) — иначе
    сравнение режимов было бы нечестным: пустой hist даёт 0 всегда.
    """
    pe = eng.price
    t = time.time() - dt * len(prices)
    for i, p in enumerate(prices):
        ts = t + i * dt
        pe._price = p
        pe.hist.append((ts, p))
        pe._update_swing(ts, p)


def test_swing_measures_move_from_local_low():
    """Дно держится 2+ тика (реальное движение), затем рост на $6."""
    eng, _ = _engine()
    _feed(eng, [65_000, 64_998, 64_996, 64_996, 65_000, 65_002])
    usd, _ = eng.price.swing()
    assert usd == pytest.approx(6.0, abs=0.01)   # 65_002 − дно 64_996


def test_swing_measures_fall_from_local_high():
    eng, _ = _engine()
    _feed(eng, [65_000, 65_004, 65_008, 65_008, 65_002, 64_999])
    usd, _ = eng.price.swing()
    assert usd == pytest.approx(-9.0, abs=0.01)  # 64_999 − пик 65_008


def test_swing_catches_slow_move_that_window_misses():
    """Ключевая разница: движение растянулось на 10с, окно в 3с его теряет."""
    eng, cfg = _engine()
    prices = [65_000 + i * 0.6 for i in range(21)]   # +$12 за 10 секунд
    _feed(eng, prices, dt=0.5)
    win, _ = eng.price.move_usd(cfg.jump_window_s)
    sw, _ = eng.price.swing()
    assert abs(win) < 5.0, "окно видит только хвост движения"
    assert sw == pytest.approx(12.0, abs=0.2), "экстремум видит движение целиком"


def test_swing_ignores_single_tick_spike():
    """Одиночный выброс консенсуса не должен рисовать новое дно."""
    eng, _ = _engine()
    _feed(eng, [65_000, 65_000, 64_980, 65_000, 65_001])
    usd, _ = eng.price.swing()
    assert abs(usd) < 5.0, f"выброс просочился: {usd}"


def test_reset_swing_reanchors_to_current_price():
    eng, _ = _engine()
    _feed(eng, [65_000, 64_996, 64_996, 65_002])
    assert abs(eng.price.swing()[0]) > 5.0
    eng.price.reset_swing()
    assert eng.price.swing()[0] == pytest.approx(0.0, abs=1e-9)


def test_swing_forgets_extremum_past_lookback():
    eng, _ = _engine(jump_swing_lookback_s=2.0)
    _feed(eng, [65_000, 64_990, 65_000, 65_000, 65_000], dt=1.0)
    usd, _ = eng.price.swing()
    assert abs(usd) < 5.0, "дно старше окна поиска должно быть забыто"


def test_engine_enters_on_swing_without_waiting():
    """Движение от дна перевалило $5 — вход в тот же тик, без окна."""
    async def scenario():
        eng, _ = _engine(jump_trigger_mode="swing")
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _feed(eng, [65_000, 64_998, 64_996, 64_996, 65_000, 65_002])
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        assert eng.legs[0]["outcome"] == "Up"
    asyncio.run(scenario())


def test_entry_reanchors_so_same_move_does_not_fire_twice():
    async def scenario():
        eng, _ = _engine(jump_trigger_mode="swing")
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _feed(eng, [65_000, 64_998, 64_996, 64_996, 65_000, 65_002])
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        assert eng.price.swing()[0] == pytest.approx(0.0, abs=1e-9)
    asyncio.run(scenario())


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
    """Тихий рынок: $150 от таргета — раунд решён, дешёвую сторону не берём.

    Предел считается в сигмах, поэтому «далеко» задаём через волатильность:
    при sigma 0.9 и 200с три сигмы — это ~$38.
    """
    async def scenario():
        eng, cfg = _engine(jump_max_target_sigmas=3.0)
        _set_target(65_000.0)
        _book(eng, 0.29, 0.30, 0.70, 0.71)
        _set_jump(eng, price=65_150.0, dprice=16.0, horizon=cfg.jump_window_s)
        eng.price._sigma = 0.9          # тихий рынок
        eng._tick()
        await _drain(eng)
        assert eng.legs == []
    asyncio.run(scenario())


def test_same_distance_allowed_when_market_is_lively():
    """Тот же $150 при разогнанном рынке — всего ~2 сигмы, вход законен."""
    async def scenario():
        eng, cfg = _engine(jump_max_target_sigmas=3.0)
        _set_target(65_000.0)
        _book(eng, 0.29, 0.30, 0.70, 0.71)
        _set_jump(eng, price=65_150.0, dprice=16.0, horizon=cfg.jump_window_s)
        eng.price._sigma = 5.0          # живой рынок
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1
        assert eng.legs[0]["track"] == "B"
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


def test_book_resolved_only_when_one_side_converged():
    eng, _ = _engine()
    _book(eng, 0.62, 0.63, 0.35, 0.36)      # рынок ещё спорит
    assert eng._book_resolved() is False
    _book(eng, 0.99, 1.00, 0.00, 0.01)      # схлопнулась
    assert eng._book_resolved() is True


def test_unconverged_book_is_marked_unsettled_not_won():
    """Книга Up 0.62 / Down 0.35 — это ставка 62/38, а не факт победы.

    Раньше движок объявлял победителем сторону с большим бидом и записывал
    выплату $1 за шэр. Теперь такие ноги закрываются по последней цене и
    помечаются UNSETTLED.
    """
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        leg = eng.legs[0]

        _book(eng, 0.62, 0.63, 0.35, 0.36)   # окно кончилось, но книга спорит
        eng._tick()
        await _drain(eng)
        eng._settle_open_position("тест")

        # P&L посчитан по рынку (0.62), а не как выигрыш $1/шэр
        expected = round(leg["shares"] * 0.62, 2) - leg["cost"]
        assert eng.realized_pnl == pytest.approx(expected, abs=0.02)
        assert eng.realized_pnl < leg["shares"] - leg["cost"], \
            "не должно быть выплаты как за выигранный раунд"
    asyncio.run(scenario())


def test_stream_deadline_waits_past_window_end():
    eng, cfg = _engine()
    assert eng._stream_deadline(1000.0) == 1000.0 + cfg.jump_settle_wait_s


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
