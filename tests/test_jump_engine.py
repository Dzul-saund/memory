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
    cfg.jump_min_edge_cents = 0.0       # и не про запас цены
    # Жёсткий стоп срабатывает на 1¢ ниже бида на входе, то есть раньше
    # лестницы. Тесты ниже про саму лестницу — там он выключается, а его
    # приоритет над ней проверяется отдельно (TestStopLoss в стратегии).
    cfg.jump_stop_loss = 0.0
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
def test_flip_sells_the_old_leg_and_buys_the_other_side():
    """Разворот закрывает провалившуюся ногу и открывает противоположную.

    Открытой всегда остаётся ровно ОДНА нога — в этом и смысл: продажа
    возвращает капитал, поэтому долг не растёт лавиной.
    """
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1 and eng.legs[0]["outcome"] == "Up"
        spent_before = eng.strategy.net_out

        _book(eng, 0.40, 0.41, 0.59, 0.60)   # Up провалился
        eng._tick()
        await _drain(eng)
        assert len(eng.legs) == 1, "старая нога должна быть продана"
        assert eng.legs[0]["outcome"] == "Down"
        # продажа вернула капитал -> вложено выросло меньше, чем на цену ноги
        assert eng.strategy.net_out < spent_before + eng.legs[0]["cost"]
    asyncio.run(scenario())


def test_flip_debt_stays_small_because_the_old_leg_was_sold():
    """Долг после разворота = реализованный убыток, а не стоимость ноги."""
    async def scenario():
        eng, cfg = _engine()
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        await _drain(eng)
        cost0 = eng.legs[0]["cost"]
        _book(eng, 0.40, 0.41, 0.59, 0.60)
        eng._tick()
        await _drain(eng)
        # вложено ~ реализованный убыток первой ноги + стоимость второй,
        # а не сумма обеих ног целиком
        assert eng.strategy.net_out < cost0 + eng.legs[0]["cost"]
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
        assert len(eng.legs) == 1, "после разворота открыта одна нога"
        dn_leg = eng.legs[0]

        # Раунд закрылся по Down: Down -> 1.0, Up -> 0.
        _book(eng, 0.00, 0.01, 0.99, 1.00)
        eng._tick()
        await _drain(eng)
        spent = eng.strategy.net_out
        eng._settle_open_position("тест")

        assert eng.legs == []
        # выплата = только шэры Down; P&L = выплата - вложено
        # P&L = выплата выжившей ноги минус ВСЁ вложенное за раунд
        # (включая уже зафиксированный убыток проданной ноги)
        expected = round(dn_leg["shares"] * 1.0, 2) - spent
        assert eng.realized_pnl == pytest.approx(expected, abs=0.05)
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


# ---------------------------------------------------------------------------
#  Минимальный размер ордера
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ask", [0.53, 0.66, 0.31, 0.95])
def test_order_never_goes_below_the_minimum(ask):
    """Ставка $1 не должна превращаться в ордер на $0.99.

    Округление шэров вниз до сотых опускало под доллар КАЖДУЮ покупку.
    Проверяем неокруглённое произведение: round(0.9964, 2) даёт ровно 1.00,
    и по округлённой сумме недобор был бы не виден.
    """
    from flowbot.jump import Action, ENTER

    eng, cfg = _engine()
    _book(eng, round(ask - 0.02, 2), ask, round(1 - ask, 2),
          round(1 - ask + 0.02, 2))
    asyncio.run(eng._buy_leg(
        Action(ENTER, outcome="Up", limit_price=ask, size_usdc=1.0,
               track="A", reason="тест")))
    leg = eng.legs[-1]
    assert leg["shares"] * ask >= cfg.jump_min_order_usdc - 1e-9


def test_size_is_always_a_whole_number_of_shares():
    """Площадка отвергает суммы длиннее двух знаков после запятой.

    При цене в целых центах два знака гарантирует только целое число шэров:
    0.57 * 1.76 = $1.0032 -> 400 "invalid amounts". Первый живой ордер упал
    именно на этом, а dry-run пропускал, потому что там сумм никто не считает.
    """
    from flowbot.jump import Action, ENTER

    for ask in (0.53, 0.57, 0.31, 0.95):
        eng, cfg = _engine()
        _book(eng, round(ask - 0.02, 2), ask, round(1 - ask, 2),
              round(1 - ask + 0.02, 2))
        asyncio.run(eng._buy_leg(
            Action(ENTER, outcome="Up", limit_price=ask, size_usdc=1.0,
                   track="A", reason="тест")))
        shares = eng.legs[-1]["shares"]
        assert shares == int(shares), f"дробный размер {shares} при цене {ask}"
        amount = round(ask * shares, 10)
        assert amount == round(amount, 2), f"сумма ${amount} длиннее 2 знаков"
        assert amount >= cfg.jump_min_order_usdc - 1e-9


def test_minimum_can_be_switched_off():
    eng, cfg = _engine(jump_min_order_usdc=0.0)
    _book(eng, 0.51, 0.53, 0.47, 0.49)
    from flowbot.jump import Action, ENTER

    asyncio.run(eng._buy_leg(
        Action(ENTER, outcome="Up", limit_price=0.53, size_usdc=1.0,
               track="A", reason="тест")))
    # Без минимума берём столько целых шэров, сколько влезает в ставку.
    assert eng.legs[-1]["shares"] == 1.0


# ---------------------------------------------------------------------------
#  Живая торговля: то, чего dry-run не проверяет
# ---------------------------------------------------------------------------
class FakeTrader:
    """Трейдер, отвечающий как настоящая биржа, — включая отказы.

    В dry-run ответ всегда «simulated_fill», поэтому весь путь обработки
    отказов и частичных филлов не выполнялся ни разу.
    """

    def __init__(self, buy_resp=None, sell_resp=None):
        self.buy_resp = buy_resp or {"status": "matched"}
        self.sell_resp = sell_resp or {"status": "matched"}
        self.buys, self.sells = [], []

    def buy(self, token, price, size, resting=False):
        self.buys.append((token, price, size))
        return self.buy_resp

    def sell(self, token, price, size, resting=False):
        self.sells.append((token, price, size))
        return self.sell_resp

    def settle(self, payout):
        pass

    def get_balance(self):
        return 100.0

    def presign_window(self, *a, **k):
        return None


def _live(buy_resp=None, sell_resp=None, **over):
    eng, cfg = _engine(**over)
    eng.trader = FakeTrader(buy_resp, sell_resp)
    return eng, cfg


def _enter(eng, ask=0.53, bid=0.51):
    from flowbot.jump import Action, ENTER

    _book(eng, bid, ask, round(1 - ask, 2), round(1 - bid, 2))
    asyncio.run(eng._buy_leg(Action(ENTER, outcome="Up", limit_price=ask,
                                    size_usdc=1.0, track="A", reason="тест")))


def test_rejected_buy_does_not_create_a_position():
    """Биржа отказала — ноги быть не должно.

    Иначе бот дальше «продаёт» несуществующие шэры и ведёт лестницу от
    выдуманного долга.
    """
    eng, _ = _live(buy_resp={"errorMsg": "not enough balance"})
    _enter(eng)
    assert eng.legs == []
    assert eng.strategy.legs == []
    assert eng.strategy.net_out == 0.0


def test_unmatched_fak_does_not_create_a_position():
    eng, _ = _live(buy_resp={"status": "unmatched"})
    _enter(eng)
    assert eng.legs == []


def test_partial_buy_records_only_what_filled():
    eng, _ = _live(buy_resp={"status": "matched", "sizeMatched": "1.00"})
    _enter(eng)
    assert len(eng.legs) == 1
    assert eng.legs[0]["shares"] == 1.00
    assert eng.legs[0]["cost"] == 0.53


def test_unknown_response_still_opens_the_leg():
    """Незнакомый ответ не должен останавливать торговлю — но должен кричать."""
    eng, _ = _live(buy_resp={"какое-то": "поле"})
    _enter(eng)
    assert len(eng.legs) == 1


def test_flip_does_not_buy_when_the_sell_was_rejected():
    """Главный денежный риск разворота.

    Если провалившуюся ногу продать не удалось, а вторую сторону мы всё
    равно взяли — открыты ОБЕ, чего конструкция лестницы не допускает, и
    долг посчитан от ноги, которая на самом деле осталась у нас.
    """
    from flowbot.jump import Action, LADDER

    eng, _ = _live(sell_resp={"errorMsg": "order failed"})
    _enter(eng)
    before = len(eng.trader.buys)
    asyncio.run(eng._execute(Action(
        LADDER, outcome="Down", limit_price=0.49, shares=3.0,
        size_usdc=1.5, track="A", sell_idx=0, sell_outcome="Up",
        reason="разворот")))
    assert len(eng.trader.buys) == before, "добор ушёл, хотя продажа провалилась"
    assert len(eng.legs) == 1, "старая нога должна остаться единственной"


def test_sell_is_refused_when_the_book_has_no_bid():
    """Раньше здесь стоял запасной ноль — ордер уходил по цене 0.00."""
    eng, _ = _live()
    _enter(eng)
    eng.book.books.clear()
    eng.legs[0]["last_bid"] = None
    ok = asyncio.run(eng._sell_leg(0, None, "тест"))
    assert ok is False
    assert eng.trader.sells == [], "продажа по нулевой цене отдала бы шэры даром"
    assert len(eng.legs) == 1


def test_sell_holds_when_the_book_fell_below_the_floor():
    """Книга просела глубже допуска — ждём, а не отдаём по любой цене."""
    eng, cfg = _live(jump_max_sell_slip=0.03)
    _enter(eng)
    _book(eng, 0.40, 0.42, 0.58, 0.60)          # бид рухнул с 0.51 до 0.40
    ok = asyncio.run(eng._sell_leg(0, 0.51, "фиксация"))
    assert ok is False
    assert eng.trader.sells == []
    assert len(eng.legs) == 1


def test_sell_goes_through_within_the_allowed_slip():
    eng, cfg = _live(jump_max_sell_slip=0.03)
    _enter(eng)
    _book(eng, 0.49, 0.51, 0.49, 0.51)          # просел на 2¢ — в допуске
    ok = asyncio.run(eng._sell_leg(0, 0.51, "фиксация"))
    assert ok is True
    assert len(eng.trader.sells) == 1
    assert eng.legs == []


def test_partial_sell_keeps_the_remainder():
    """Продалась часть — оставшиеся шэры не должны потеряться."""
    eng, _ = _live(sell_resp={"status": "matched", "sizeMatched": "1.00"})
    _enter(eng)
    total = eng.legs[0]["shares"]
    ok = asyncio.run(eng._sell_leg(0, None, "тест"))
    assert ok is False, "нога закрыта не полностью"
    assert eng.legs[0]["shares"] == round(total - 1.00, 2)
    assert eng.strategy.legs[0].shares == round(total - 1.00, 2)


# ---------------------------------------------------------------------------
#  Повисший ордер: бот не должен молча переставать торговать
# ---------------------------------------------------------------------------
class HangingTrader(FakeTrader):
    """Биржа не отвечает. У клиента нет таймаута — значит он нужен у нас."""

    def buy(self, token, price, size, resting=False):
        time.sleep(1)
        return {"status": "matched"}


def test_hung_order_times_out_and_does_not_create_a_position():
    """Ордер завис. Ногу записывать нельзя: мы не знаем, дошёл он или нет.

    Записать — значит потом «продавать» позицию, которой может не быть, и
    вести лестницу от выдуманного долга.
    """
    from flowbot.jump import Action, ENTER

    # Пауза заведомо длинная: asyncio.run дожидается спящий поток, и с
    # короткой паузой проверка «пауза ещё идёт» гонялась бы со временем.
    eng, cfg = _live(jump_error_cooldown_s=30.0)
    eng.trader = HangingTrader()
    cfg.order_timeout_s = 0.2
    _book(eng, 0.51, 0.53, 0.47, 0.49)
    asyncio.run(eng._buy_leg(Action(ENTER, outcome="Up", limit_price=0.53,
                                    size_usdc=1.0, track="A", reason="тест")))
    assert eng.legs == []
    assert eng.strategy.legs == []
    assert eng._retry_after > time.time(), "после зависания нужна пауза"


def test_watchdog_clears_a_stuck_busy_flag():
    """Без сторожа один повисший ордер убивал торговлю до перезапуска.

    Снаружи это выглядит хуже всего: строки состояния идут, решение
    «покупать» печатается, а сделок нет и причина не названа.
    """
    async def scenario():
        eng, cfg = _live()
        cfg.order_timeout_s = 0.1
        eng.busy = True
        eng._busy_since = time.time() - 999.0
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)
        _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)
        eng._tick()
        # Старый флаг снят, и тут же взведён заново — под НОВЫЙ ордер.
        assert eng._busy_since > time.time() - 5.0
        await _drain(eng)
        assert len(eng.trader.buys) == 1, "после сброса сторожа сделка прошла"

    asyncio.run(scenario())


def test_skip_is_reported_not_silent():
    """Сигнал есть, действовать нельзя — это должно быть видно в логе."""
    eng, cfg = _live()
    eng.busy = True
    eng._busy_since = time.time()
    eng._last_skip_log = 0.0
    _set_target(65_000.0)
    _book(eng, 0.59, 0.60, 0.40, 0.41)
    _set_jump(eng, price=65_010.0, dprice=8.0, horizon=cfg.jump_window_s)

    said = []
    eng.log.warning = lambda msg, *a: said.append(msg % a if a else msg)
    eng._tick()
    assert any("сигнал есть, но сделки нет" in s for s in said), said
