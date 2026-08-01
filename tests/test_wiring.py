"""Тесты СВЯЗКИ движка и стратегии: доходит ли до неё то, на чём она стоит.

Отдельный файл, потому что это самый тихий класс поломок в проекте. Стратегия
может быть безупречной, а движок — не положить ей в снимок историю цены; тогда
σ не измерится, вход будет запрещён на каждом такте, и снаружи это выглядит как
«бот просто не торгует». Ровно так же незаметно пропадает `record_settle`:
дневной лимит потерь перестаёт срабатывать, и об этом никто не узнает до
второй потери подряд.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.certainty import CertaintyStrategy
from flowbot.config import FlowConfig
from flowbot.strategy import NONE, Action, Strategy
from flowbot.trading import TradingEngine, build_strategy


def _engine(**over):
    cfg = FlowConfig(max_round_usdc=50.0)
    cfg.dry_run = True
    cfg.simulate_latency = False
    cfg.trade_log_csv = ""
    cfg.stats_enabled = False
    for k, v in over.items():
        setattr(cfg, k, v)
    eng = TradingEngine(cfg)
    eng.market = {
        "up": "UPTOK", "down": "DNTOK",
        "window_ts": int(time.time()), "end_ts": time.time() + 200.0,
        "slug": "btc-updown-5m-test",
    }
    eng.book.set_tokens("UPTOK", "DNTOK")
    return eng, cfg


def _book(eng, up_bid=0.97, up_ask=0.98, dn_bid=0.02, dn_ask=0.03,
          size="1000"):
    eng.book.on_event({"event_type": "book", "asset_id": "UPTOK",
                       "bids": [{"price": str(up_bid), "size": size}],
                       "asks": [{"price": str(up_ask), "size": size}]},
                      time.time())
    eng.book.on_event({"event_type": "book", "asset_id": "DNTOK",
                       "bids": [{"price": str(dn_bid), "size": size}],
                       "asks": [{"price": str(dn_ask), "size": size}]},
                      time.time())


# ---------------------------------------------------------------------------
#  Выбор стратегии
# ---------------------------------------------------------------------------
def test_по_умолчанию_стратегии_нет():
    assert type(build_strategy(FlowConfig())) is Strategy


def test_имя_из_конфига_собирает_стратегию():
    cfg = FlowConfig(strategy="certainty", max_round_usdc=50.0)
    assert isinstance(build_strategy(cfg), CertaintyStrategy)


def test_движок_поднимает_ту_же_стратегию():
    eng, _ = _engine(strategy="certainty")
    assert isinstance(eng.strategy, CertaintyStrategy)


# ---------------------------------------------------------------------------
#  Снимок: сырьё должно доехать
# ---------------------------------------------------------------------------
def test_снимок_несёт_историю_цены_и_книгу():
    """Без этого σ не измерить, и вход будет запрещён навсегда — молча."""
    eng, _ = _engine(reconcile_s=0.0)   # сверка требует живого цикла asyncio
    _book(eng)
    seen = {}

    def spy(snap):
        seen["snap"] = snap
        return Action(NONE, "тест")

    eng.strategy.on_tick = spy
    for i in range(30):
        eng.price.hist.append((time.time() - 30 + i, 100000.0 + i))
    eng.price._price = 100030.0
    eng._tick()

    snap = seen["snap"]
    assert len(snap.price_hist) >= 30
    assert snap.up_levels[0] and snap.up_levels[1]
    assert snap.down_levels[0] and snap.down_levels[1]
    assert snap.book_age_s is not None and snap.book_age_s < 5.0


def test_возраст_книги_растёт_когда_поток_молчит():
    """Обрыв CLOB изнутри неотличим от тихого рынка — только по времени."""
    eng, _ = _engine()
    _book(eng)
    eng.book.last_event_s = time.time() - 42.0
    assert eng.book.age_s() == pytest.approx(42.0, abs=1.0)


def test_книга_без_событий_не_имеет_возраста():
    eng, _ = _engine()
    assert eng.book.age_s() is None


def test_смена_окна_обнуляет_возраст_книги():
    """Книга прошлого раунда не должна выглядеть свежей в новом."""
    eng, _ = _engine()
    _book(eng)
    assert eng.book.age_s() is not None
    eng.book.set_tokens("UP2", "DN2")
    assert eng.book.age_s() is None


def test_история_цены_отдаётся_копией():
    """Стратегия не должна иметь возможности испортить историю движка."""
    eng, _ = _engine()
    eng.price.hist.append((1.0, 100.0))
    h = eng.price.history()
    h.append((2.0, 200.0))
    assert len(eng.price.hist) == 1


# ---------------------------------------------------------------------------
#  Расчёт: стратегия обязана узнать исход
# ---------------------------------------------------------------------------
def _open_leg(eng, outcome="Up", price=0.98, shares=10.0):
    leg = eng.strategy.record_entry(outcome, price, shares,
                                    round(price * shares, 2), time.time(),
                                    entry_bid=price - 0.01)
    eng.legs.append({
        "idx": leg.idx, "outcome": outcome, "token": "UPTOK",
        "entry_price": price, "shares": shares,
        "cost": round(price * shares, 2), "last_bid": 0.99,
        "coin_at_entry": 100000.0, "secs_at_entry": 60, "feat": {},
        "entry_bid": price - 0.01, "entry_ts": time.time(),
    })
    return leg


def test_расчёт_сообщает_стратегии_исход():
    eng, _ = _engine()
    got = []
    eng.strategy.record_settle = lambda leg, payout, pnl, t: got.append(
        (leg.outcome, payout, pnl))
    _open_leg(eng)
    _book(eng, up_bid=0.99, up_ask=1.0, dn_bid=0.0, dn_ask=0.01)
    eng._settle_open_position("тест")
    assert got and got[0][0] == "Up"
    assert got[0][1] == pytest.approx(10.0)      # выигрыш: 10 шэров по $1
    assert got[0][2] == pytest.approx(0.2)       # 10.00 - 9.80


def test_проигрыш_доезжает_до_дневного_риска():
    """Именно эта дорожка включает стоп на день. Без неё он слеп."""
    eng, cfg = _engine(strategy="certainty", cert_max_day_losses=1)
    _open_leg(eng, outcome="Down", price=0.98, shares=50.0)
    eng.legs[-1]["token"] = "DNTOK"
    _book(eng, up_bid=0.99, up_ask=1.0, dn_bid=0.0, dn_ask=0.01)
    eng._settle_open_position("тест")
    assert eng.strategy.day_losses == 1
    assert eng.strategy.halted


def test_упавший_record_settle_не_роняет_расчёт():
    """Учёт стратегии не имеет права мешать закрытию раунда."""
    eng, _ = _engine()

    def boom(*a, **k):
        raise RuntimeError("тест")

    eng.strategy.record_settle = boom
    _open_leg(eng)
    _book(eng, up_bid=0.99, up_ask=1.0, dn_bid=0.0, dn_ask=0.01)
    eng._settle_open_position("тест")
    assert eng.legs == []


# ---------------------------------------------------------------------------
#  Конфигурация: молчаливо неверные настройки
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("over,fragment", [
    ({"cert_steps": (10.0, 10.0)}, "сумма ступеней"),
    ({"cert_z_min": 2.0}, "CERT_Z_MIN"),
    ({"cert_exit_z": 5.0}, "CERT_EXIT_Z"),
    ({"max_round_usdc": 25.0}, "потолок раунда"),
    ({"cert_min_price": 0.99}, "CERT_MIN_PRICE"),
    ({"cert_min_seconds_left": 200.0}, "CERT_MIN_SECONDS_LEFT"),
    ({"cert_exit_confirm_ticks": 0}, "CERT_EXIT_CONFIRM_TICKS"),
    ({"cert_steps": (0.5, 0.5, 0.5, 48.5)}, "минимума площадки"),
])
def test_неверная_настройка_ловится_проверкой(over, fragment):
    """Молчаливо неверный порог = бот, который не торгует и не говорит почему."""
    kw = {"max_round_usdc": 50.0}
    kw.update(over)
    cfg = FlowConfig(strategy="certainty", **kw)
    with pytest.raises(ValueError) as exc:
        cfg.validate()
    assert fragment in str(exc.value)


def test_рабочая_конфигурация_проходит():
    FlowConfig(strategy="certainty", max_round_usdc=50.0).validate()


def test_ступени_читаются_из_окружения(monkeypatch):
    monkeypatch.setenv("FLOW_STRATEGY", "certainty")
    monkeypatch.setenv("CERT_STEPS", "5, 5, 10, 30")
    monkeypatch.setenv("CERT_FULL_SIZE", "50")
    monkeypatch.setenv("FLOW_MAX_ROUND_USDC", "50")
    cfg = FlowConfig.from_env()
    assert cfg.cert_steps == (5.0, 5.0, 10.0, 30.0)
    cfg.validate()


def test_мусор_в_ступенях_не_проглатывается(monkeypatch):
    monkeypatch.setenv("CERT_STEPS", "десять, двадцать")
    with pytest.raises(ValueError) as exc:
        FlowConfig.from_env()
    assert "CERT_STEPS" in str(exc.value)
