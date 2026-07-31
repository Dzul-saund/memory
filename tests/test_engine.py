"""Оффлайн тесты ИНФРАСТРУКТУРЫ движка: без сети, без ключей, без денег.

Проверяется путь исполнения, а не торговые решения: как отправляется ордер,
как разбирается ответ биржи, что происходит при отказе, частичном филле и
таймауте, как считается расчёт в конце окна.

Стратегия здесь намеренно пустая — как и в проекте. Действия подаются в
движок напрямую, поэтому эти тесты продолжат работать при любой новой
логике: они про то, что живёт ПОД ней.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fast_monitor
from flowbot.config import FlowConfig
from flowbot.strategy import BUY, SELL, Action
from flowbot.trading import TradingEngine


def _engine(**over):
    cfg = FlowConfig()
    cfg.dry_run = True
    cfg.simulate_latency = False        # тесты не ждут пинг
    cfg.trade_log_csv = ""              # не писать CSV в тестах
    cfg.stats_enabled = False           # и не писать журнал в домашнюю папку
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


def _book(eng, up_bid, up_ask, dn_bid, dn_ask):
    eng.book.on_event({"event_type": "book", "asset_id": "UPTOK",
                       "bids": [{"price": str(up_bid), "size": "100"}],
                       "asks": [{"price": str(up_ask), "size": "100"}]})
    eng.book.on_event({"event_type": "book", "asset_id": "DNTOK",
                       "bids": [{"price": str(dn_bid), "size": "100"}],
                       "asks": [{"price": str(dn_ask), "size": "100"}]})


def _set_target(value, offset=0.0):
    """Подсунуть таргет текущего раунда, как это делает fast_monitor."""
    now = time.time()
    fast_monitor.ROUND_TARGET.update(
        start_ts=int(now - now % 300), value=value, exact=True)
    return value + offset


def _buy(eng, ask=0.53, bid=0.51, size=1.0, outcome="Up"):
    return Action(BUY, outcome=outcome, limit_price=ask, size_usdc=size,
                  reason="тест")


async def _drain(eng, timeout=4.0):
    """Дождаться, пока запланированный _execute отработает."""
    for _ in range(int(timeout / 0.02)):
        if not eng.busy:
            return
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
#  Таргет раунда
# ---------------------------------------------------------------------------
def test_stale_target_is_ignored():
    """Таргет прошлого окна не должен считаться целью этого."""
    eng, _ = _engine()
    fast_monitor.ROUND_TARGET.update(start_ts=0, value=65_000.0, exact=True)
    assert eng._target() is None
    _set_target(65_000.0)
    assert eng._target() == 65_000.0


# ---------------------------------------------------------------------------
#  Размер ордера
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ask", [0.53, 0.57, 0.61, 0.75, 0.95])
def test_order_never_goes_below_the_minimum(ask):
    """Ставка $1 не должна превращаться в ордер на $0.99.

    Округление шэров вниз до сотых опускало под доллар КАЖДУЮ покупку.
    Проверяем неокруглённое произведение: round(0.9964, 2) даёт ровно 1.00,
    и по округлённой сумме недобор был бы не виден.
    """
    eng, cfg = _engine()
    _book(eng, round(ask - 0.02, 2), ask, round(1 - ask, 2),
          round(1 - ask + 0.02, 2))
    asyncio.run(eng._buy_leg(_buy(eng, ask)))
    leg = eng.legs[-1]
    assert leg["shares"] * ask >= cfg.min_order_usdc - 1e-9


def test_size_is_always_a_whole_number_of_shares():
    """Площадка отвергает суммы длиннее двух знаков после запятой.

    При цене в целых центах два знака гарантирует только целое число шэров:
    0.57 * 1.76 = $1.0032 -> 400 "invalid amounts". Первый живой ордер упал
    именно на этом, а dry-run пропускал, потому что там сумм никто не считает.
    """
    for ask in (0.53, 0.57, 0.31, 0.95):
        eng, cfg = _engine()
        _book(eng, round(ask - 0.02, 2), ask, round(1 - ask, 2),
              round(1 - ask + 0.02, 2))
        asyncio.run(eng._buy_leg(_buy(eng, ask)))
        shares = eng.legs[-1]["shares"]
        assert shares == int(shares), f"дробный размер {shares} при цене {ask}"
        amount = round(ask * shares, 10)
        assert amount == round(amount, 2), f"сумма ${amount} длиннее 2 знаков"
        assert amount >= cfg.min_order_usdc - 1e-9


def test_minimum_can_be_switched_off():
    eng, _ = _engine(min_order_usdc=0.0)
    _book(eng, 0.51, 0.53, 0.47, 0.49)
    asyncio.run(eng._buy_leg(_buy(eng, 0.53)))
    # Без минимума берём столько целых шэров, сколько влезает в ставку.
    assert eng.legs[-1]["shares"] == 1.0


def test_round_cap_blocks_the_order():
    """Потолок раунда — ограничение риска, а не правило стратегии."""
    eng, _ = _engine(max_round_usdc=1.5)
    _book(eng, 0.51, 0.53, 0.47, 0.49)
    asyncio.run(eng._buy_leg(_buy(eng, 0.53)))
    asyncio.run(eng._buy_leg(_buy(eng, 0.53)))
    assert len(eng.legs) == 1, "второй ордер обязан упереться в потолок"


def test_book_moved_past_the_limit_price():
    """За время пинга книга ушла — покупать по новой цене нельзя."""
    eng, _ = _engine()
    _book(eng, 0.60, 0.62, 0.38, 0.40)
    asyncio.run(eng._buy_leg(_buy(eng, ask=0.53)))
    assert eng.legs == []


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
    _book(eng, bid, ask, round(1 - ask, 2), round(1 - bid, 2))
    asyncio.run(eng._buy_leg(_buy(eng, ask)))


async def _enter_async(eng, ask=0.53, bid=0.51):
    _book(eng, bid, ask, round(1 - ask, 2), round(1 - bid, 2))
    await eng._buy_leg(_buy(eng, ask))


def test_rejected_buy_does_not_create_a_position():
    """Биржа отказала — позиции быть не должно.

    Иначе бот дальше «продаёт» несуществующие шэры и считает вложения от
    выдуманной суммы.
    """
    eng, _ = _live(buy_resp={"errorMsg": "not enough balance"})
    _enter(eng)
    assert eng.legs == []
    assert eng.strategy.legs == []
    assert eng.strategy.net_out == 0.0


def test_unmatched_fak_does_not_create_a_position():
    """FAK может не найти встречной заявки и вернуть нулевой филл."""
    eng, _ = _live(buy_resp={"status": "unmatched", "size_matched": "0"})
    _enter(eng)
    assert eng.legs == []


def test_partial_buy_records_only_what_filled():
    eng, _ = _live(buy_resp={"status": "matched", "size_matched": "1"})
    _enter(eng, ask=0.50)
    assert len(eng.legs) == 1
    assert eng.legs[0]["shares"] == 1.0


def test_unknown_response_still_opens_the_position():
    """Ответ не разобрали — считаем, что ордер прошёл.

    Ошибиться безопаснее в эту сторону: лишнюю позицию найдёт и уберёт
    сверка, а забытая живёт вечно и теряет в цене.
    """
    eng, _ = _live(buy_resp={"нечто": "неведомое"})
    _enter(eng)
    assert len(eng.legs) == 1


def test_sell_is_refused_when_the_book_has_no_bid():
    """Пустая книга не повод отдавать позицию по нулю."""
    eng, _ = _live()
    _enter(eng)
    eng.book.on_event({"event_type": "book", "asset_id": "UPTOK",
                       "bids": [], "asks": [{"price": "0.55", "size": "10"}]})
    # Запасной «последний бид» тоже стираем: проверяем случай, когда цены
    # нет вообще ниоткуда. Иначе движок справедливо возьмёт последнюю
    # известную — это его штатное поведение, а не ошибка.
    eng.legs[0]["last_bid"] = None
    ok = asyncio.run(eng._sell_leg(0, None, "тест"))
    assert ok is False
    assert len(eng.legs) == 1
    assert eng.trader.sells == []


def test_sell_holds_when_the_book_fell_below_the_floor():
    eng, _ = _live(max_sell_slip=0.05)
    _enter(eng)
    _book(eng, 0.30, 0.32, 0.68, 0.70)
    ok = asyncio.run(eng._sell_leg(0, 0.50, "тест"))
    assert ok is False
    assert eng.trader.sells == []


def test_sell_goes_through_within_the_allowed_slip():
    eng, _ = _live(max_sell_slip=0.05)
    _enter(eng)
    _book(eng, 0.48, 0.50, 0.50, 0.52)
    ok = asyncio.run(eng._sell_leg(0, 0.50, "тест"))
    assert ok is True
    assert eng.legs == []


def test_partial_sell_keeps_the_remainder():
    """Продалась часть — остаток всё ещё наш, забыть про него нельзя."""
    eng, _ = _live(sell_resp={"status": "matched", "size_matched": "1"})
    _enter(eng, ask=0.50)
    before = eng.legs[0]["shares"]
    ok = asyncio.run(eng._sell_leg(0, None, "тест"))
    assert ok is False
    assert len(eng.legs) == 1
    assert eng.legs[0]["shares"] == before - 1.0


class HangingTrader(FakeTrader):
    """Запрос, который не отвечает никогда — так падал живой ордер."""

    def buy(self, token, price, size, resting=False):
        time.sleep(3.0)
        return {"status": "matched"}


def test_hung_order_times_out_and_does_not_create_a_position():
    """При таймауте ногу НЕ записываем: неизвестно, дошёл ордер или нет.

    Фантомная позиция ломает и продажи, и учёт вложений.
    """
    async def scenario():
        eng, _ = _engine(order_timeout_s=0.2)
        eng.trader = HangingTrader()
        await _enter_async(eng)
        assert eng.legs == []
        assert eng.strategy.legs == []

    asyncio.run(scenario())


def test_watchdog_clears_a_stuck_busy_flag():
    """Залипший «ордер в полёте» — молчаливый отказ торговать.

    Строки состояния идут, решение печатается, а сделок нет и причина нигде
    не названа. Сторож обязан снять флаг и сказать об этом вслух.
    """
    async def scenario():
        eng, cfg = _live()
        eng.busy = True
        eng._busy_since = time.time() - (cfg.order_timeout_s * 2 + 6.0)
        _book(eng, 0.51, 0.53, 0.47, 0.49)

        class Deciding(type(eng.strategy)):
            def should_enter(self, s):
                return Action(BUY, outcome="Up", limit_price=0.53,
                              size_usdc=1.0, reason="тест")

        eng.strategy = Deciding(cfg)
        eng._tick()
        await _drain(eng)
        assert eng.legs, "сторож не снял блокировку — ордер не ушёл"

    asyncio.run(scenario())


def test_skip_is_reported_not_silent():
    """«Сигнал есть, а сделки нет» обязано попадать в лог."""
    eng, _ = _live()
    msgs = []
    eng.log.warning = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    eng._note_skip("проверка")
    assert any("сделки нет" in m for m in msgs)


def test_cooldown_never_blocks_an_exit():
    """Пауза после отказа биржи придерживает вход, но НЕ выход.

    Раньше она блокировала любое действие, включая продажу: один
    отклонённый ордер — и бот пять секунд даже не пытался выйти, пока цена
    падала.
    """
    async def scenario():
        eng, cfg = _live()
        await _enter_async(eng, ask=0.50, bid=0.49)
        eng._retry_after = time.time() + 60.0     # «идёт пауза после ошибки»
        _book(eng, 0.40, 0.42, 0.58, 0.60)

        class Exiting(eng.strategy.__class__):
            def should_exit(self, s, leg):
                return Action(SELL, sell_idx=leg.idx,
                              sell_outcome=leg.outcome, limit_price=0.40,
                              reason="тест")

        st = Exiting(cfg)
        st.legs = eng.strategy.legs
        st.net_out = eng.strategy.net_out
        eng.strategy = st

        eng._tick()
        await _drain(eng)
        assert eng.legs == [], "выход обязан пройти даже во время паузы"
        assert len(eng.trader.sells) == 1

    asyncio.run(scenario())


def test_cooldown_still_holds_back_entries():
    async def scenario():
        eng, cfg = _live()
        eng._retry_after = time.time() + 60.0
        _set_target(65_000.0)
        _book(eng, 0.59, 0.60, 0.40, 0.41)

        class Entering(eng.strategy.__class__):
            def should_enter(self, s):
                return Action(BUY, outcome="Up", limit_price=0.60,
                              size_usdc=1.0, reason="тест")

        eng.strategy = Entering(cfg)
        eng._tick()
        await _drain(eng)
        assert eng.trader.buys == [], "вход во время паузы уходить не должен"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
#  Расчёт в конце окна
# ---------------------------------------------------------------------------
def test_settle_pays_only_the_winning_side():
    eng, _ = _engine()
    _enter(eng, ask=0.50, bid=0.48)
    _book(eng, 0.99, 1.00, 0.00, 0.01)     # книга схлопнулась в пользу Up
    eng._settle_open_position("тест")
    assert eng.legs == []
    assert eng.wins == 1 and eng.losses == 0
    assert eng.realized_pnl > 0


def test_losing_side_pays_nothing():
    eng, _ = _engine()
    _enter(eng, ask=0.50, bid=0.48)
    _book(eng, 0.00, 0.01, 0.99, 1.00)     # выиграл Down
    eng._settle_open_position("тест")
    assert eng.wins == 0 and eng.losses == 1
    assert eng.realized_pnl < 0


def test_book_resolved_only_when_one_side_converged():
    eng, _ = _engine()
    _book(eng, 0.62, 0.63, 0.35, 0.36)
    assert eng._book_resolved() is False
    _book(eng, 0.99, 1.00, 0.00, 0.01)
    assert eng._book_resolved() is True


def test_unconverged_book_is_marked_unsettled_not_won():
    """Up 0.62 / Down 0.35 — это ставка, а не факт.

    Объявлять победителя по такой книге нельзя: позиция закрывается по
    последней цене и помечается UNSETTLED, чтобы не соврать статистике.
    """
    eng, _ = _engine()
    _enter(eng, ask=0.50, bid=0.48)
    _book(eng, 0.62, 0.63, 0.35, 0.36)
    rows = []
    eng._log_trade_row = lambda pos, result, *a: rows.append(result)
    eng._settle_open_position("тест")
    assert rows == ["UNSETTLED"], "победителя по такой книге объявлять нельзя"
    assert eng.legs == []


def test_settle_without_legs_is_noop():
    eng, _ = _engine()
    eng._settle_open_position("тест")
    assert eng.realized_pnl == 0.0


def test_stream_deadline_waits_past_window_end():
    eng, cfg = _engine()
    end = time.time()
    assert eng._stream_deadline(end) > end


def test_new_window_resets_the_position_ledger():
    eng, _ = _engine()
    _enter(eng)
    assert eng.strategy.net_out > 0
    eng._enter_window({"up": "U2", "down": "D2",
                       "window_ts": int(time.time()) + 300,
                       "slug": "next"})
    assert eng.strategy.legs == []
    assert eng.strategy.net_out == 0.0
