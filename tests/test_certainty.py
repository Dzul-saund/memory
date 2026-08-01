"""Тесты стратегии «покупка почти-факта» (flowbot/certainty.py).

Проверяется поведение решений, а не исполнения: когда бот входит, как
набирает лестницу, когда прекращает набор, когда выходит и когда
останавливается на день. Денег и сети здесь нет — стратегии подаётся
`Snapshot` напрямую, ровно как это делает движок.

Главное, что здесь защищается: одна потеря стоит 49 выигрышей, поэтому
каждый запрет обязан срабатывать, а каждый выход — требовать подтверждения.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.certainty import CertaintyStrategy
from flowbot.config import FlowConfig
from flowbot.strategy import BUY, HOLD, NONE, SELL, Snapshot


def _cfg(**over):
    cfg = FlowConfig(strategy="certainty", max_round_usdc=50.0)
    for k, v in over.items():
        setattr(cfg, k, v)
    cfg.validate()
    return cfg


def _hist(t_end, sigma_step=1.0, n=120, price=100060.0):
    """История с ненулевой, но небольшой σ и известной последней ценой."""
    out = []
    for i in range(n):
        out.append((t_end - (n - 1 - i), price + (i % 2) * sigma_step))
    out[-1] = (t_end, price)
    return out


def _snap(t=10000.0, price=100060.0, target=100000.0, left=60.0,
          up_ask=0.98, up_bid=0.97, depth=1000.0, hist=None, **over):
    # Одна книга с двух сторон: Up 0.97/0.98 и Down 0.02/0.03.
    dn_bid = round(1 - up_ask, 2) if up_ask is not None else None
    dn_ask = round(1 - up_bid, 2) if up_bid is not None else None
    levels = ([(up_bid, depth)] if up_bid else [],
              [(up_ask, depth)] if up_ask else [])
    down = ([(dn_bid, depth)] if dn_bid else [],
            [(dn_ask, depth)] if dn_ask else [])
    kw = dict(
        t=t, seconds_left=left, coin_price=price, target=target,
        up_bid=up_bid, up_ask=up_ask, down_bid=dn_bid, down_ask=dn_ask,
        pm_price=price, price_hist=hist if hist is not None else _hist(t),
        up_levels=levels, down_levels=down, book_age_s=0.2,
    )
    kw.update(over)
    return Snapshot(**kw)


def _fill(strat, act, t, shares=None):
    """Изобразить подтверждённый филл, как это делает движок."""
    px = act.limit_price
    sh = shares if shares is not None else round(act.size_usdc / px)
    return strat.record_entry(act.outcome, px, sh, round(px * sh, 2), t,
                              entry_bid=px - 0.01, feat=act.feat)


# ---------------------------------------------------------------------------
#  Вход
# ---------------------------------------------------------------------------
def test_решённый_раунд_покупается():
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap())
    assert act.kind == BUY
    assert act.outcome == "Up"
    assert act.size_usdc == 10.0            # первая ступень
    assert act.limit_price == 0.98


def test_покупается_сторона_которая_выигрывает():
    """Цена ниже таргета — покупать надо Down, а не Up."""
    s = CertaintyStrategy(_cfg())
    # Up 0.02/0.03 — это та же книга с другой стороны: Down 0.97/0.98.
    snap = _snap(price=99940.0, up_ask=0.03, up_bid=0.02,
                 hist=_hist(10000.0, price=99940.0))
    act = s.on_tick(snap)
    assert act.kind == BUY and act.outcome == "Down"
    assert act.limit_price == 0.98


def test_нерешённый_раунд_не_покупается():
    """Цена в двух долларах от таргета — это ставка, а не факт."""
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap(price=100002.0, hist=_hist(10000.0,
                                                     price=100002.0)))
    assert act.kind == NONE
    assert "запас" in act.reason


def test_без_таргета_не_покупается():
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap(target=None))
    assert act.kind == NONE


def test_дорогая_цена_не_покупается():
    """0.99 при выплате $1: забирать нечего, а рисковать всё так же $50."""
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap(up_ask=0.99, up_bid=0.98))
    assert act.kind == NONE


def test_обрыв_потока_книги_блокирует_вход():
    s = CertaintyStrategy(_cfg())
    assert s.on_tick(_snap(book_age_s=60.0)).kind == NONE
    assert s.on_tick(_snap(t=10001.0, book_age_s=None)).kind == NONE


def test_устаревшая_цена_блокирует_вход():
    s = CertaintyStrategy(_cfg())
    # Последний тик истории на 30 секунд старше текущего времени.
    act = s.on_tick(_snap(t=10030.0, hist=_hist(10000.0)))
    assert act.kind == NONE


def test_нет_ликвидности_на_ступень_блокирует_вход():
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap(depth=5.0))
    assert act.kind == NONE


# ---------------------------------------------------------------------------
#  Лестница
# ---------------------------------------------------------------------------
def test_ступень_засчитывается_только_по_филлу():
    """Между решением и филлом ордер может не уйти вовсе.

    Если считать ступень взятой в момент решения, лестница израсходует все
    четыре ступени, не купив ничего, и позиция навсегда останется неполной.
    """
    s = CertaintyStrategy(_cfg())
    for i in range(5):
        act = s.on_tick(_snap(t=10000.0 + i))
        assert act.kind == BUY
        assert act.feat["step"] == 1          # всё ещё ПЕРВАЯ ступень
    assert s.round.steps == 0


def test_лестница_набирает_по_порядку():
    c = _cfg(cert_step_interval_s=1.0)
    s = CertaintyStrategy(c)
    t = 10000.0
    sizes = []
    for _ in range(4):
        act = s.on_tick(_snap(t=t))
        assert act.kind == BUY, act.reason
        sizes.append(act.size_usdc)
        _fill(s, act, t)
        t += 2.0
    assert sizes == [10.0, 10.0, 10.0, 20.0]
    assert s.net_out == pytest.approx(sum(sizes), abs=1.0)
    # Позиция полная — больше не добираем.
    assert s.on_tick(_snap(t=t)).kind == HOLD


def test_пауза_между_ступенями_соблюдается():
    c = _cfg(cert_step_interval_s=3.0)
    s = CertaintyStrategy(c)
    act = s.on_tick(_snap(t=10000.0))
    _fill(s, act, 10000.0)
    assert s.on_tick(_snap(t=10001.0)).kind == HOLD    # рано
    assert s.on_tick(_snap(t=10004.0)).kind == BUY     # пора


def test_набор_прекращается_если_запас_просел():
    """Лестница набирает ВВЕРХ по уверенности, а не вниз по цене."""
    c = _cfg(cert_step_interval_s=1.0, cert_z_slip=0.3)
    s = CertaintyStrategy(c)
    act = s.on_tick(_snap(t=10000.0))
    _fill(s, act, 10000.0)
    # Цена подошла к таргету: запас упал, но ещё выше порога входа.
    s.on_tick(_snap(t=10002.0, price=100035.0,
                    hist=_hist(10002.0, price=100035.0)))
    assert s.round.stopped
    # И больше не возобновляется, даже если стало снова хорошо.
    assert s.on_tick(_snap(t=10004.0)).kind == HOLD


def test_моргнувший_фид_не_хоронит_лестницу():
    """Ослепнуть на секунду — не то же самое, что ухудшение рынка.

    Иначе один пропавший тик оставлял бы позицию недобранной до конца
    раунда, и причины этого нигде бы не было видно.
    """
    c = _cfg(cert_step_interval_s=1.0)
    s = CertaintyStrategy(c)
    act = s.on_tick(_snap(t=10000.0))
    _fill(s, act, 10000.0)
    # Поток книги моргнул: запрет есть, но рынок тот же.
    assert s.on_tick(_snap(t=10002.0, book_age_s=60.0)).kind == HOLD
    assert not s.round.stopped
    # Связь вернулась — набор продолжается.
    assert s.on_tick(_snap(t=10003.0)).kind == BUY


def test_порог_ужесточается_с_каждой_ступенью():
    c = _cfg(cert_step_interval_s=1.0, cert_risk_max=30.0,
             cert_risk_tighten=30.0)
    s = CertaintyStrategy(c)
    act = s.on_tick(_snap(t=10000.0))
    _fill(s, act, 10000.0)
    # После первой ступени планка 30-30 = 0: пройти её почти невозможно.
    assert s.on_tick(_snap(t=10002.0)).kind == HOLD
    assert s.round.stopped


def test_потолок_позиции_не_превышается():
    c = _cfg(cert_step_interval_s=0.5, cert_full_size=50.0)
    s = CertaintyStrategy(c)
    t = 10000.0
    for _ in range(4):
        act = s.on_tick(_snap(t=t))
        if act.kind != BUY:
            break
        _fill(s, act, t)
        t += 1.0
    assert s.net_out <= 50.0 + 1e-6


# ---------------------------------------------------------------------------
#  Выход
# ---------------------------------------------------------------------------
def _open(cfg=None, t=10000.0):
    s = CertaintyStrategy(cfg or _cfg())
    act = s.on_tick(_snap(t=t))
    _fill(s, act, t)
    return s


def test_один_плохой_такт_не_вызывает_выхода():
    """Процент проваливается до 0.50 и возвращается на 0.98 за секунду."""
    s = _open()
    act = s.on_tick(_snap(t=10001.0, price=100005.0, up_bid=0.55, up_ask=0.60,
                          hist=_hist(10001.0, price=100005.0)))
    assert act.kind != SELL
    assert s.round.alarm_ticks == 1


def test_подтверждённый_разворот_вызывает_выход():
    c = _cfg(cert_exit_confirm_ticks=3, cert_exit_confirm_s=1.0)
    s = _open(c)
    act = None
    for i in range(1, 6):
        t = 10000.0 + i
        act = s.on_tick(_snap(t=t, price=100005.0, up_bid=0.55, up_ask=0.60,
                              hist=_hist(t, price=100005.0)))
        if act.kind == SELL:
            break
    assert act.kind == SELL
    assert act.sell_outcome == "Up"
    assert act.limit_price == 0.55
    assert "подтверждено" in act.reason


def test_тревога_сбрасывается_когда_опасность_ушла():
    c = _cfg(cert_exit_confirm_ticks=5, cert_exit_confirm_s=1.0)
    s = _open(c)
    s.on_tick(_snap(t=10001.0, price=100005.0,
                    hist=_hist(10001.0, price=100005.0)))
    s.on_tick(_snap(t=10002.0, price=100005.0,
                    hist=_hist(10002.0, price=100005.0)))
    assert s.round.alarm_ticks == 2
    s.on_tick(_snap(t=10003.0))              # снова хорошо
    assert s.round.alarm_ticks == 0


def test_подтверждения_считаются_раз_за_такт():
    """Ног может быть четыре, и should_exit зовётся по каждой.

    Без защиты «пять тактов подряд» набирались бы за два такта — то есть за
    ту самую секунду шума, ради которой подтверждение и вводилось.
    """
    c = _cfg(cert_step_interval_s=0.5, cert_exit_confirm_ticks=5,
             cert_exit_confirm_s=1.0)
    s = CertaintyStrategy(c)
    t = 10000.0
    for _ in range(3):
        act = s.on_tick(_snap(t=t))
        if act.kind != BUY:
            break
        _fill(s, act, t)
        t += 1.0
    assert len(s.legs) >= 2
    s.on_tick(_snap(t=t, price=100005.0, hist=_hist(t, price=100005.0)))
    assert s.round.alarm_ticks == 1


def test_у_конца_окна_не_выходим():
    """Книга тонкая; держать до расчёта безопаснее, чем отдавать по любой."""
    c = _cfg(cert_no_exit_last_s=8.0)
    s = _open(c)
    for i in range(1, 12):
        t = 10000.0 + i
        act = s.on_tick(_snap(t=t, left=5.0, price=100005.0, up_bid=0.30,
                              up_ask=0.35, hist=_hist(t, price=100005.0)))
        assert act.kind != SELL


def test_без_бида_не_продаём():
    """Продавать не по чему — держим до расчёта, а не отдаём за ноль."""
    c = _cfg(cert_exit_confirm_ticks=1, cert_exit_confirm_s=0.0)
    s = _open(c)
    for i in range(1, 5):
        t = 10000.0 + i
        act = s.on_tick(_snap(t=t, price=100005.0, up_bid=None, up_ask=0.60,
                              hist=_hist(t, price=100005.0)))
        assert act.kind != SELL


def test_выход_работает_без_ask_в_книге():
    """У выхода не должно быть зависимости от книги покупателя.

    Именно в минуту, когда продавать приходится, ask пропадает первым.
    """
    c = _cfg(cert_exit_confirm_ticks=2, cert_exit_confirm_s=0.0)
    s = _open(c)
    act = None
    for i in range(1, 6):
        t = 10000.0 + i
        act = s.on_tick(_snap(t=t, price=100005.0, up_bid=0.55, up_ask=None,
                              hist=_hist(t, price=100005.0)))
        if act.kind == SELL:
            break
    assert act.kind == SELL


# ---------------------------------------------------------------------------
#  Дневной риск
# ---------------------------------------------------------------------------
def test_потеря_останавливает_на_день():
    s = _open(_cfg(cert_max_day_losses=1))
    leg = s.legs[0]
    s.record_settle(leg, 0.0, -10.0, 10100.0)
    s.reset_round()
    act = s.on_tick(_snap(t=10200.0))
    assert act.kind == NONE
    assert "остановлен на сегодня" in act.reason


def test_выигрыш_не_останавливает():
    s = _open(_cfg(cert_max_day_losses=1))
    leg = s.legs[0]
    s.record_settle(leg, 10.2, 0.2, 10100.0)
    s.reset_round()
    assert s.on_tick(_snap(t=10200.0)).kind == BUY


def test_лимит_сделок_за_день():
    s = _open(_cfg(cert_max_day_trades=2, cert_max_day_losses=0,
                   cert_max_day_loss_usd=0))
    leg = s.legs[0]
    s.record_settle(leg, 10.2, 0.2, 10100.0)
    s.record_settle(leg, 10.2, 0.2, 10101.0)
    s.reset_round()
    assert s.on_tick(_snap(t=10200.0)).kind == NONE


def test_выход_проверяется_даже_на_остановленном_боте():
    """Стоп по дневному риску запрещает ВХОДЫ. Выход — никогда."""
    c = _cfg(cert_max_day_losses=1, cert_exit_confirm_ticks=1,
             cert_exit_confirm_s=0.0)
    s = _open(c)
    s.halted = "тест"
    act = None
    for i in range(1, 4):
        t = 10000.0 + i
        act = s.on_tick(_snap(t=t, price=100005.0, up_bid=0.55, up_ask=0.60,
                              hist=_hist(t, price=100005.0)))
        if act.kind == SELL:
            break
    assert act.kind == SELL


def test_новый_день_сбрасывает_счётчики():
    s = _open(_cfg(cert_max_day_losses=1))
    s.record_settle(s.legs[0], 0.0, -10.0, 10100.0)
    assert s.halted
    s._roll_day(10100.0 + 86400 * 2)
    assert s.halted is None and s.day_losses == 0


# ---------------------------------------------------------------------------
#  Журнал
# ---------------------------------------------------------------------------
def test_все_слагаемые_риска_попадают_в_журнал():
    """Без этого нельзя понять, какой фильтр оказывался прав, а какой мешал."""
    s = CertaintyStrategy(_cfg())
    act = s.on_tick(_snap())
    for name in ("запас", "скорость", "скачок", "режим", "глубина", "спред"):
        assert f"r_{name}" in act.feat
    for key in ("risk_score", "z", "p_model", "edge_cents", "buffer_usd",
                "step", "secs_left", "sigma", "max_jump_1s"):
        assert key in act.feat


def test_сброс_раунда_очищает_состояние():
    s = _open()
    s.round.stopped = True
    s.reset_round()
    assert s.round.steps == 0 and not s.round.stopped
    assert s.legs == [] and s.net_out == 0.0
