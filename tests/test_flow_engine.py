"""Оффлайн интеграционные тесты движка flowbot (без сети, без ключей).

Проверяем связку: снимок книги + всплеск цены -> вход -> удержание -> выход,
а также сеттл на границе окна. Трейдер — DryRunTrader, фиды не запускаем;
книгу и цену наполняем вручную.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.config import FlowConfig
from flowbot.engine import FlowEngine


def _engine():
    cfg = FlowConfig()
    cfg.dry_run = True
    cfg.simulate_latency = False        # тесты не ждут пинг
    cfg.trade_log_csv = ""              # не писать CSV в тестах
    eng = FlowEngine(cfg)
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


def _set_burst(eng, price, sigma, dprice, horizon=1.0):
    """Заставить PriceEngine показать всплеск dprice за horizon сек."""
    pe = eng.price
    pe._price = price
    pe._sigma = sigma
    t = time.time()
    pe.hist.clear()
    pe.hist.append((t - horizon, price - dprice))
    pe.hist.append((t - horizon / 2, price - dprice / 2))
    pe.hist.append((t, price))


def test_buy_then_sell_roundtrip():
    async def scenario():
        eng, _ = _engine()
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        await eng._buy("Up", 0.60, 1.0, is_flip=False)
        assert eng.pos is not None and eng.pos["outcome"] == "Up"
        assert eng.strategy.position is not None
        assert eng.pos["shares"] == round(int(1.0 / 0.60 * 100) / 100, 2)
        await eng._sell_current(0.59, "выход")
        assert eng.pos is None and eng.strategy.position is None
    asyncio.run(scenario())


def test_tick_enters_on_sharp_up_burst():
    async def scenario():
        eng, cfg = _engine()
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_burst(eng, price=100_000.0, sigma=5.0, dprice=60.0)  # z ~ 12
        eng._tick()
        # ждём завершения запланированного _execute
        for _ in range(200):
            if not eng.busy and eng.pos is not None:
                break
            await asyncio.sleep(0.02)
        assert eng.pos is not None, "должна была открыться позиция"
        assert eng.pos["outcome"] == "Up"
    asyncio.run(scenario())


def test_settle_open_position_win_and_loss():
    async def scenario():
        eng, _ = _engine()
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        await eng._buy("Up", 0.60, 1.0, is_flip=False)
        eng.pos["last_bid"] = 0.98            # выигрыш
        cost = eng.pos["cost"]
        shares = eng.pos["shares"]
        eng._settle_open_position("тест")
        assert eng.pos is None
        assert eng.wins == 1
        assert abs(eng.realized_pnl - round(shares - cost, 2)) < 1e-6

        # теперь проигрыш
        _book(eng, 0.30, 0.31, 0.69, 0.70)
        await eng._buy("Up", 0.31, 1.0, is_flip=False)
        eng.pos["last_bid"] = 0.05
        eng._settle_open_position("тест")
        assert eng.losses == 1
    asyncio.run(scenario())


def test_flip_switches_side():
    async def scenario():
        eng, cfg = _engine()
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        await eng._buy("Up", 0.60, 1.0, is_flip=False)
        assert eng.pos["outcome"] == "Up"
        # разворот: продать Up и купить Down на $3.5
        await eng._sell_current(0.58, "разворот-выход")
        assert eng.pos is None
        _book(eng, 0.55, 0.56, 0.44, 0.45)
        await eng._buy("Down", 0.47, 3.5, is_flip=True)
        assert eng.pos is not None and eng.pos["outcome"] == "Down"
        assert eng.pos["is_flip"] is True
    asyncio.run(scenario())


def test_execute_flip_action_buys_opposite_first():
    """FLIP через _execute: сначала куплена противоположная, старая продана."""
    from flowbot.strategy import Action, FLIP

    async def scenario():
        eng, _ = _engine()
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        await eng._buy("Up", 0.60, 1.0, is_flip=False)
        assert eng.pos["outcome"] == "Up"
        act = Action(FLIP, outcome="Down", limit_price=0.43, size_usdc=3.5,
                     sell_outcome="Up", sell_limit=0.58, reason="тест разворота")
        eng.busy = True
        await eng._execute(act)
        assert eng.busy is False
        assert eng.pos is not None
        assert eng.pos["outcome"] == "Down" and eng.pos["is_flip"] is True
        assert eng.strategy.position.outcome == "Down"
    asyncio.run(scenario())
