"""Тесты измерений и единой оценки риска (flowbot/risk.py).

Проверяется то, на чём стратегия «почти-факт» стоит целиком: правильно ли
считается запас в сигмах, не занижается ли σ, срабатывают ли запреты. Цена
ошибки здесь несимметрична — одна пропущенная опасная сделка стоит 49
выигрышей, — поэтому каждый запрет проверяется отдельно.
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot import risk


# ---------------------------------------------------------------------------
#  Измерения
# ---------------------------------------------------------------------------
def _hist(step=0.1, n=1200, fn=lambda i: 100000.0):
    """История цены: n тиков через step секунд, цена задаётся функцией."""
    return [(1000.0 + i * step, fn(i)) for i in range(n)]


def test_пустая_история_ничего_не_меряет():
    m = risk.measure([])
    assert m.sigma is None and m.max_jump_1s is None
    assert m.ticks == 0


def test_недостаток_данных_даёт_none_а_не_ноль():
    """None значит «не знаю». Ноль означал бы «рынок стоит» — и пропустил бы
    вход с бесконечным z."""
    m = risk.measure(_hist(n=5))
    assert m.sigma is None


def test_сигма_считается_по_ско_а_не_по_mad():
    """Смесь со скачками: MAD игнорирует хвосты и занижает σ в полтора раза.

    Ряд ниже — ровный шум ±1 с редким скачком на 20. MAD такого ряда почти не
    замечает, СКО — обязано.
    """
    def fn(i):
        base = 100000.0 + (1.0 if i % 2 else -1.0)
        return base + (20.0 if i % 100 == 0 else 0.0)

    m = risk.measure(_hist(step=1.0, n=120, fn=fn))
    assert m.sigma_raw is not None
    # Одна двадцатка на сто наблюдений даёт вклад в СКО порядка √(400/100)=2.
    assert m.sigma_raw > 1.5


def test_запас_надёжности_умножает_сигму():
    h = _hist(step=1.0, n=120, fn=lambda i: 100000.0 + (i % 7) * 3.0)
    a = risk.measure(h, sigma_safety=1.0, sigma_floor=0.0)
    b = risk.measure(h, sigma_safety=2.0, sigma_floor=0.0)
    assert b.sigma == pytest.approx(a.sigma * 2.0)


def test_сигма_не_опускается_ниже_пола():
    """Слишком тихое окно занижает σ и завышает z — пол это ограничивает."""
    m = risk.measure(_hist(step=1.0, n=120,
                           fn=lambda i: 100000.0 + (i % 2) * 0.01),
                     sigma_safety=1.0, sigma_floor=0.7)
    assert m.sigma_raw < 0.7
    assert m.sigma == pytest.approx(0.7)


def test_замерзший_фид_не_даёт_нулевую_сигму():
    """Ровно ноль — это не «тихий рынок», а фид, отдающий одно и то же.

    Пропустить его как σ=пол означало бы огромный z и покупку по цене,
    которой давно нет.
    """
    m = risk.measure(_hist(step=1.0, n=120), sigma_floor=0.7)
    assert m.sigma_raw == 0.0
    assert m.sigma is None


def test_берётся_большая_из_двух_сигм():
    """Всплеск в последние секунды не должен растворяться в спокойной минуте."""
    def fn(i):
        # 110 спокойных секунд, затем 10 бурных.
        return 100000.0 + (0.0 if i < 110 else (i - 110) % 2 * 50.0)

    m = risk.measure(_hist(step=1.0, n=121, fn=fn),
                     fast_s=10.0, slow_s=60.0, sigma_safety=1.0,
                     sigma_floor=0.0)
    assert m.sigma_fast > m.sigma_slow
    assert m.sigma == pytest.approx(m.sigma_fast)


def test_худший_скачок_за_секунду():
    def fn(i):
        return 100000.0 + (37.0 if i >= 60 else 0.0)

    m = risk.measure(_hist(step=1.0, n=120, fn=fn))
    assert m.max_jump_1s == pytest.approx(37.0, abs=0.5)


def test_смены_направления_считаются():
    m = risk.measure(_hist(step=0.5, n=200,
                           fn=lambda i: 100000.0 + (i % 2) * 5.0))
    assert m.flips is not None and m.flips > 10


def test_ликвидность_считается_только_по_нашей_цене():
    """«В книге $2000» регулярно означает $40 по цене нашего лимита."""
    asks = [(0.98, 40.0), (0.99, 1000.0), (1.00, 1000.0)]
    assert risk.depth_usd(asks) == pytest.approx(0.98 * 40 + 0.99 * 1000
                                                 + 1000.0)
    assert risk.fillable_usd(asks, 0.98) == pytest.approx(39.2)


# ---------------------------------------------------------------------------
#  Запас в сигмах
# ---------------------------------------------------------------------------
def test_z_считается_в_пользу_стороны():
    buf_up, z_up, _ = risk.buffer_z(side="Up", price=100050.0,
                                    target=100000.0, seconds_left=100.0,
                                    sigma=1.0)
    buf_dn, z_dn, _ = risk.buffer_z(side="Down", price=100050.0,
                                    target=100000.0, seconds_left=100.0,
                                    sigma=1.0)
    assert buf_up == pytest.approx(50.0)
    assert z_up == pytest.approx(5.0)          # 50 / (1 * √100)
    assert buf_dn == pytest.approx(-50.0)
    assert z_dn == pytest.approx(-5.0)


def test_z_без_сигмы_не_считается():
    assert risk.buffer_z(side="Up", price=1.0, target=0.0,
                         seconds_left=10.0, sigma=None) == (None, None, 0.0)


def test_порог_безубытка_ровно_на_2_05_сигмы():
    """Ключевое тождество стратегии: при z=2.05 модель даёт ровно 0.98."""
    assert risk.phi(2.054) == pytest.approx(0.98, abs=0.001)


# ---------------------------------------------------------------------------
#  Оценка: жёсткие запреты
# ---------------------------------------------------------------------------
def _m(sigma=1.0, jump=5.0, speed=0.0, regime=1.0, flips=1):
    return risk.Metrics(sigma=sigma, sigma_raw=sigma, sigma_fast=sigma,
                        sigma_slow=sigma, vol_regime=regime,
                        max_jump_1s=jump, speed=speed, accel=0.0,
                        flips=flips, ticks=500, span_s=120.0)


def _assess(**over):
    """Заведомо ХОРОШИЙ вход; тесты портят его по одному параметру."""
    kw = dict(
        side="Up", price=100060.0, target=100000.0, seconds_left=60.0,
        ask=0.98, bid=0.97, m=_m(),
        our_levels=([(0.97, 1000.0)], [(0.98, 1000.0)]),
        book_age_s=0.2, data_age_s=0.1, pm_price=100060.0,
    )
    kw.update(over)
    return risk.assess(**kw)


def test_хороший_вход_проходит():
    r = _assess()
    assert r.ok, r.vetoes
    assert r.z == pytest.approx(60.0 / math.sqrt(60.0), abs=0.01)
    assert r.p_model > 0.9999


def test_нет_цены_или_таргета_запрет():
    assert not _assess(price=None).ok
    assert not _assess(target=None).ok


def test_неизмеренная_волатильность_запрет():
    """Отсутствие измерения обязано ЗАПРЕЩАТЬ, а не проходить как ноль."""
    r = _assess(m=_m(sigma=None))
    assert not r.ok
    assert "волатильность" in r.vetoes[0]


def test_нет_ask_запрет():
    assert not _assess(ask=None).ok


def test_малый_запас_запрет():
    """z=2.05 — ровно порог безубытка. Покупать там нечего."""
    r = _assess(price=100002.0)
    assert not r.ok
    assert any("запас" in v for v in r.vetoes)


def test_слишком_дорого_запрет():
    r = _assess(ask=0.99, bid=0.98)
    assert any("забирать нечего" in v for v in r.vetoes)


def test_слишком_дёшево_запрет():
    r = _assess(ask=0.90, bid=0.89)
    assert any("НЕ почти решён" in v for v in r.vetoes)


def test_широкий_спред_запрет():
    r = _assess(bid=0.90)
    assert any("спред" in v for v in r.vetoes)


def test_рано_и_поздно_запрет():
    assert any("слишком рано" in v for v in _assess(seconds_left=200.0).vetoes)
    assert any("не успеть" in v for v in _assess(seconds_left=5.0).vetoes)


def test_устаревшая_цена_запрет():
    r = _assess(data_age_s=9.0)
    assert any("устарела" in v for v in r.vetoes)


def test_мёртвый_поток_книги_запрет():
    """Обрыв CLOB изнутри выглядит как тихий рынок — ловится только временем."""
    assert any("оборван" in v for v in _assess(book_age_s=30.0).vetoes)
    assert any("потока CLOB нет" in v for v in _assess(book_age_s=None).vetoes)


def test_нет_ликвидности_запрет():
    r = _assess(our_levels=([(0.97, 1000.0)], [(0.98, 10.0)]))
    assert any("набрать позицию нечем" in v for v in r.vetoes)


def test_ликвидности_меньше_ступени_запрет():
    """Глубина есть, но её не хватит на ЭТУ ступень — филл будет частичным."""
    r = _assess(our_levels=([(0.97, 1000.0)], [(0.98, 100.0)]), need_usd=200.0)
    assert any("частичным" in v for v in r.vetoes)


def test_глубина_за_лимитом_не_считается():
    """Уровни дороже нашего лимита для FAK-ордера не существуют."""
    r = _assess(our_levels=([(0.97, 1000.0)],
                            [(0.98, 20.0), (0.99, 5000.0)]))
    assert any("набрать позицию нечем" in v for v in r.vetoes)


# ---------------------------------------------------------------------------
#  Оценка: слагаемые риска
# ---------------------------------------------------------------------------
def test_риск_растёт_когда_скорость_ест_запас():
    calm = _assess(m=_m(speed=0.0))
    fast = _assess(m=_m(speed=-0.5))          # $0.5/с в сторону таргета
    assert fast.score > calm.score
    assert _points(fast, "скорость") > _points(calm, "скорость")


def test_риск_растёт_при_большом_скачке():
    small = _assess(m=_m(jump=5.0))
    big = _assess(m=_m(jump=40.0))
    assert _points(big, "скачок") > _points(small, "скачок")


def test_риск_растёт_при_всплеске_волатильности():
    calm = _assess(m=_m(regime=1.0))
    spike = _assess(m=_m(regime=3.0))
    assert _points(spike, "режим") > _points(calm, "режим")


def test_уход_бидов_замечается():
    stay = _assess(bid_depth_before=1000.0)
    gone = _assess(bid_depth_before=5000.0)   # было 5000, стало 970
    assert _points(gone, "уход бидов") > _points(stay, "уход бидов")


def test_расхождение_с_якорем_замечается():
    close = _assess(pm_price=100060.0)
    far = _assess(pm_price=100000.0)
    assert _points(far, "якорь") > _points(close, "якорь")


def test_неизмеренный_фактор_не_проходит_как_благополучный():
    """Не «0 риска», а половина веса: незнание — не хорошая новость."""
    r = _assess(m=_m(speed=None, jump=None, regime=None))
    assert _points(r, "скорость") > 0
    assert _points(r, "скачок") > 0
    assert _points(r, "режим") > 0


def test_веса_в_сумме_дают_сто():
    p = risk.RiskParams()
    total = (p.w_buffer + p.w_speed + p.w_jump + p.w_regime + p.w_depth
             + p.w_spread + p.w_biddrop + p.w_flips + p.w_anchor)
    assert total == pytest.approx(100.0)


def test_оценка_не_выходит_за_сто():
    r = _assess(price=100200.0, ask=0.98, bid=0.94,
                m=_m(sigma=1.0, jump=200.0, speed=-5.0, regime=9.0, flips=50),
                our_levels=([(0.94, 100.0)], [(0.98, 100.0)]),
                bid_depth_before=9999.0, pm_price=99000.0)
    assert 0.0 <= r.score <= 100.0


def _points(report, name):
    return next(f.points for f in report.factors if f.name == name)
