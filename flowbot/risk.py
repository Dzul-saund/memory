"""Измерения рынка и единая оценка риска для стратегии «покупка почти-факта».

ЧТО ЗДЕСЬ СЧИТАЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО

Стратегия покупает сторону по цене около 0.98 и держит до расчёта. Порог
безубытка такой ставки равен ровно 0.98 — это не совпадение, а определение
справедливой цены. Значит прибыль возможна ровно в одном случае: когда
ИСТИННАЯ вероятность выше той, что стоит в книге.

Отсюда следует, что фильтры здесь решают не ту задачу, которую обычно
приписывают «риск-фильтрам». Отсеивание сделок само по себе не улучшает
матожидание: оно убирает и выигрыши, и проигрыши в одной пропорции. Каждый
фильтр ниже существует ровно для одного — **подтвердить, что раунд
действительно решён**, то есть что истинная вероятность заметно выше 0.98,
а не для того, чтобы «поменьше рисковать».

Поэтому центральная величина здесь одна:

    z = (цена − таргет) / (σ · √t_остаток)     в пользу купленной стороны

При z = 2.05 модель даёт ровно 0.98 — то есть ровно порог безубытка, и
покупать там нечего. Прибыль начинается там, где z заметно больше: при z = 4
модель даёт 0.99997, при z = 5 — 0.9999997. Разница между 0.98 и 0.9999 и
есть весь заработок этой стратегии.

Все остальные факторы отвечают на вопрос «а можно ли верить этому z».

ГЛАВНАЯ ОПАСНОСТЬ — НЕ ДРЕЙФ, А СКАЧОК. Модель Φ описывает непрерывное
блуждание. Реальная цена BTC ходит скачками: новость, ликвидация, каскад
стопов. Диффузионная σ такой ход не описывает вовсе, поэтому здесь рядом с
σ считается ОТДЕЛЬНО худший односекундный ход за окно — и запас проверяется
против него тоже, а не только против σ√t.

σ СЧИТАЕТСЯ КОНСЕРВАТИВНО. В этом проекте измерено: оценка σ по MAD занижает
нужную для Φ величину в 1.4-1.6 раза на скачковых данных, потому что MAD по
построению игнорирует хвосты — то есть ровно то, чем крипта и движется.
Здесь берётся СКО приращений (квадратичная вариация) и вдобавок умножается
на запас надёжности: недооценённая σ завышает z, а завышенный z — прямая
дорога к той самой потере в $50.

Модуль ничего не знает ни про сеть, ни про ордера: на вход история цены и
книга, на выход — числа и оценка. Всё покрывается тестами без денег.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Tick = Tuple[float, float]          # (время, цена)
Level = Tuple[float, float]         # (цена, объём)


def phi(z: float) -> float:
    """Функция нормального распределения Φ(z)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
#  Измерения
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    """Всё, что удалось померить по истории цены и книге.

    None означает «не хватило данных», и это НЕ то же самое, что ноль:
    отсутствие измерения обязано запрещать вход, а не проходить как
    благополучное значение.
    """

    sigma: Optional[float] = None          # консервативная, $/√с
    sigma_raw: Optional[float] = None      # без запаса надёжности
    sigma_fast: Optional[float] = None     # короткое окно
    sigma_slow: Optional[float] = None     # длинное окно
    vol_regime: Optional[float] = None     # быстрая / медленная
    max_jump_1s: Optional[float] = None    # худший ход за секунду в окне
    speed: Optional[float] = None          # $/с сейчас, знаковая
    accel: Optional[float] = None          # ускорение, $/с²
    flips: Optional[int] = None            # смен направления за окно
    hi: Optional[float] = None             # локальный максимум
    lo: Optional[float] = None             # локальный минимум
    ticks: int = 0
    span_s: float = 0.0


def _start(hist: Sequence[Tick], window_s: float) -> int:
    """Индекс первого тика внутри окна. История отсортирована по времени.

    Двоичный поиск, а не фильтр списком: measure() зовётся на каждом событии
    книги, а история — это две минуты тиков. Пересобирать её списками по
    пять раз за такт означало бы тратить на прогон записи часы.
    """
    if not hist:
        return 0
    edge = hist[-1][0] - window_s
    lo, hi = 0, len(hist)
    while lo < hi:
        mid = (lo + hi) // 2
        if hist[mid][0] < edge:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _returns(hist: Sequence[Tick], step_s: float, start: int = 0) -> List[float]:
    """Приращения цены на сетке step_s. Пустой список, если истории мало."""
    if len(hist) - start < 2:
        return []
    out: List[float] = []
    t0, p0 = hist[start]
    for i in range(start + 1, len(hist)):
        t, p = hist[i]
        if t - t0 >= step_s:
            out.append(p - p0)
            t0, p0 = t, p
    return out


def _sigma_1s(hist: Sequence[Tick], window_s: float,
              step_s: float = 1.0) -> Optional[float]:
    """σ в долларах за корень секунды по СКО приращений.

    Именно СКО, а не MAD: Φ нужна квадратичная вариация, а MAD игнорирует
    хвосты и занижает результат в полтора раза на скачковых данных.
    """
    if not hist:
        return None
    rets = _returns(hist, step_s, _start(hist, window_s))
    if len(rets) < 4:
        return None
    # Среднее НЕ вычитаем: сноса за секунды не бывает, а вычитание среднего
    # на коротком окне съедает часть настоящего движения.
    rms = math.sqrt(sum(r * r for r in rets) / len(rets))
    return rms / math.sqrt(step_s)


def measure(hist: Sequence[Tick], *, fast_s: float = 10.0,
            slow_s: float = 60.0, flip_s: float = 20.0,
            extremum_s: float = 60.0,
            sigma_safety: float = 1.5,
            sigma_floor: float = 0.2) -> Metrics:
    """Померить всё, что понадобится оценке риска."""
    m = Metrics()
    if not hist:
        return m
    m.ticks = len(hist)
    m.span_s = hist[-1][0] - hist[0][0]

    m.sigma_fast = _sigma_1s(hist, fast_s)
    m.sigma_slow = _sigma_1s(hist, slow_s)
    # Базовая σ — БОЛЬШАЯ из двух окон. Занижение σ завышает z и ведёт прямо
    # к потере полной ставки, поэтому все неоднозначности решаются в сторону
    # осторожности.
    cands = [s for s in (m.sigma_fast, m.sigma_slow) if s is not None]
    if cands:
        m.sigma_raw = max(cands)
        # РОВНО НУЛЬ — ЭТО НЕ «ТИХИЙ РЫНОК», А ЗАМЕРЗШИЙ ФИД. Цена BTC не
        # стоит на месте секундами; если приращения все до одного нулевые,
        # значит консенсус отдаёт одно и то же значение с новыми отметками
        # времени. Пропустить это как σ=пол означало бы z=огромный и покупку
        # по цене, которой давно нет, — поэтому здесь None («не знаю»), а
        # None запрещает вход.
        m.sigma = (max(m.sigma_raw * sigma_safety, sigma_floor)
                   if m.sigma_raw > 0 else None)
    if m.sigma_fast and m.sigma_slow and m.sigma_slow > 1e-9:
        m.vol_regime = m.sigma_fast / m.sigma_slow

    # Худший односекундный ход: прокси риска СКАЧКА, которого нет в σ.
    jumps = [abs(r) for r in _returns(hist, 1.0, _start(hist, slow_s))]
    if jumps:
        m.max_jump_1s = max(jumps)

    # Скорость и ускорение по последним секундам.
    m.speed = _speed(hist, 1.0)
    v_now, v_prev = m.speed, _speed(hist, 1.0, offset=1.0)
    if v_now is not None and v_prev is not None:
        m.accel = v_now - v_prev

    # Смены направления: «пила» означает, что предсказывать нечего.
    part = [p for _t, p in hist[_start(hist, flip_s):]]
    if len(part) >= 3:
        signs = [1 if b > a else (-1 if b < a else 0)
                 for a, b in zip(part, part[1:])]
        signs = [s for s in signs if s]
        m.flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)

    part = [p for _t, p in hist[_start(hist, extremum_s):]]
    if part:
        m.hi, m.lo = max(part), min(part)
    return m


def _speed(hist: Sequence[Tick], window_s: float,
           offset: float = 0.0) -> Optional[float]:
    """Средняя скорость, $/с, на окне, отстоящем на offset секунд назад."""
    if len(hist) < 2:
        return None
    t_end = hist[-1][0] - offset
    t_start = t_end - window_s
    # Ищем от конца: нужные точки почти всегда в последних секундах истории,
    # а история — это две минуты тиков.
    a = b = None
    for i in range(len(hist) - 1, -1, -1):
        t, p = hist[i]
        if b is None and t <= t_end:
            b = p
        if t <= t_start:
            a = p
            break
    if a is None or b is None:
        return None
    return (b - a) / window_s


def depth_usd(levels: Sequence[Level]) -> float:
    """Сколько долларов лежит на уровнях (цена × объём)."""
    return sum(p * s for p, s in levels)


def fillable_usd(asks: Sequence[Level], limit: float) -> float:
    """Сколько долларов реально возьмётся по цене не выше limit.

    Суммировать всю глубину нельзя: ордер уходит как FAK с лимитом, и уровни
    дороже лимита для нас не существуют. Именно поэтому «в книге $2000»
    регулярно означает $40 по нашей цене и частичный филл.
    """
    return sum(p * s for p, s in asks if p <= limit + 1e-9)


# ---------------------------------------------------------------------------
#  Оценка риска
# ---------------------------------------------------------------------------
def buffer_z(*, side: str, price: Optional[float], target: Optional[float],
             seconds_left: float,
             sigma: Optional[float]) -> Tuple[Optional[float],
                                              Optional[float], float]:
    """(запас в долларах, запас в сигмах, σ√t) в пользу стороны `side`.

    Вынесено отдельно, потому что это нужно и входу, и выходу, а выходу
    нельзя зависеть от книги: аварийная продажа обязана считаться даже когда
    в книге нет ask и `assess` не доходит до этой строки.
    """
    if price is None or target is None or not sigma or sigma <= 0:
        return None, None, 0.0
    t = max(seconds_left, 0.5)
    sig_t = sigma * math.sqrt(t)
    d = 1.0 if side == "Up" else -1.0
    buf = d * (price - target)
    return buf, (buf / sig_t if sig_t > 0 else 0.0), sig_t


@dataclass
class Factor:
    name: str
    points: float        # сколько добавил к риску
    weight: float        # максимум, который мог добавить
    detail: str


@dataclass
class RiskReport:
    score: float = 0.0                       # 0..100, больше — опаснее
    factors: List[Factor] = field(default_factory=list)
    vetoes: List[str] = field(default_factory=list)
    z: Optional[float] = None                # запас в сигмах
    p_model: Optional[float] = None          # вероятность по модели
    buffer_usd: Optional[float] = None       # запас в долларах
    edge_cents: Optional[float] = None       # p_model − ask, в центах

    @property
    def ok(self) -> bool:
        return not self.vetoes

    def summary(self) -> str:
        top = sorted(self.factors, key=lambda f: -f.points)[:3]
        who = ", ".join(f"{f.name} {f.points:.0f}" for f in top if f.points > 0)
        return (f"риск {self.score:.0f}/100"
                + (f" ({who})" if who else "")
                + (f" | z={self.z:.1f}" if self.z is not None else "")
                + (f" p={self.p_model * 100:.3f}%" if self.p_model else ""))


def _ramp(value: float, good: float, bad: float) -> float:
    """0 при value=good, 1 при value=bad, линейно между. Работает в обе
    стороны — good может быть больше bad."""
    if good == bad:
        return 0.0 if value == good else 1.0
    x = (value - good) / (bad - good)
    return max(0.0, min(1.0, x))


@dataclass
class RiskParams:
    """Пороги оценки. Все — настройки, потому что все они догадки."""

    # --- запас: главный фактор ---------------------------------------------
    z_min: float = 4.0          # ниже — вход запрещён вовсе
    z_full: float = 6.0         # выше — по этому фактору риска нет
    # --- скорость против нас ------------------------------------------------
    speed_eat_good: float = 0.10   # доля запаса, которую съест скорость
    speed_eat_bad: float = 0.50
    # --- режим волатильности -------------------------------------------------
    regime_good: float = 1.0
    regime_bad: float = 2.5
    # --- риск скачка ---------------------------------------------------------
    jump_cover_good: float = 6.0   # запас / худший скачок за секунду
    jump_cover_bad: float = 2.0
    # --- книга ---------------------------------------------------------------
    spread_good_c: float = 1.0
    spread_bad_c: float = 4.0
    depth_good_usd: float = 500.0
    depth_bad_usd: float = 100.0
    bid_drop_good: float = 1.0     # глубина сейчас / глубина раньше
    bid_drop_bad: float = 0.4
    # --- прочее --------------------------------------------------------------
    flips_good: int = 2
    flips_bad: int = 10
    anchor_good_sig: float = 0.5   # расхождение с якорем в σ
    anchor_bad_sig: float = 2.0

    # --- веса (в сумме 100) --------------------------------------------------
    w_buffer: float = 30.0
    w_speed: float = 15.0
    w_jump: float = 13.0
    w_regime: float = 12.0
    w_depth: float = 10.0
    w_spread: float = 8.0
    w_biddrop: float = 5.0
    w_flips: float = 4.0
    w_anchor: float = 3.0

    # --- жёсткие запреты -----------------------------------------------------
    max_price: float = 0.985    # дороже покупать нечего: прибыль меньше цента
    min_price: float = 0.960    # дешевле — раунд не «почти решён»
    max_spread_c: float = 5.0
    min_depth_usd: float = 60.0
    max_stale_s: float = 2.0        # цена старше — торгуем вслепую
    max_book_stale_s: float = 5.0   # книга старше — поток CLOB, похоже, умер
    min_seconds_left: float = 10.0
    max_seconds_left: float = 120.0


def assess(*, side: str, price: Optional[float], target: Optional[float],
           seconds_left: float, ask: Optional[float], bid: Optional[float],
           m: Metrics, our_levels: Tuple[Sequence[Level], Sequence[Level]],
           bid_depth_before: Optional[float] = None,
           pm_price: Optional[float] = None,
           data_age_s: Optional[float] = None,
           book_age_s: Optional[float] = None,
           need_usd: float = 0.0,
           p: Optional[RiskParams] = None) -> RiskReport:
    """Единая оценка: можно ли покупать эту сторону прямо сейчас.

    Возвращает отчёт со всеми слагаемыми — чтобы по журналу потом было видно,
    какой фактор чаще всего оказывался прав, а какой только мешал.
    """
    p = p or RiskParams()
    r = RiskReport()

    def veto(why: str) -> None:
        r.vetoes.append(why)

    def add(name: str, weight: float, frac: float, detail: str) -> None:
        pts = weight * max(0.0, min(1.0, frac))
        r.factors.append(Factor(name, pts, weight, detail))
        r.score += pts

    # ── ЖЁСТКИЕ ЗАПРЕТЫ: без этих данных решать нельзя вообще ──────────────
    if price is None or target is None:
        veto("нет цены монеты или таргета раунда")
        return r
    if m.sigma is None or m.sigma <= 0:
        veto("волатильность не измерена — z посчитать не из чего")
        return r
    if ask is None:
        veto("нет ask в книге — покупать не у кого")
        return r
    if data_age_s is not None and data_age_s > p.max_stale_s:
        veto(f"цена устарела на {data_age_s:.1f}с — торговля вслепую")
    # Мёртвый поток книги неотличим от тихого рынка по самой книге: цены в
    # ней остаются те же, что были в момент обрыва. Отличить можно только по
    # времени — и это единственная защита от покупки по цене, которой давно
    # нет.
    if book_age_s is None:
        veto("книга ещё не приходила — потока CLOB нет")
    elif book_age_s > p.max_book_stale_s:
        veto(f"книга не менялась {book_age_s:.1f}с — поток CLOB, похоже, оборван")
    if seconds_left < p.min_seconds_left:
        veto(f"до расчёта {seconds_left:.0f}с — ордер может не успеть")
    if seconds_left > p.max_seconds_left:
        veto(f"до расчёта {seconds_left:.0f}с — слишком рано, раунд ещё живой")
    if ask > p.max_price:
        veto(f"ask {ask:.3f} > {p.max_price:.3f} — забирать нечего")
    if ask < p.min_price:
        veto(f"ask {ask:.3f} < {p.min_price:.3f} — раунд НЕ почти решён")

    spread_c = (ask - bid) * 100 if bid is not None else None
    if spread_c is not None and spread_c > p.max_spread_c:
        veto(f"спред {spread_c:.1f}¢ > {p.max_spread_c:.1f}¢")

    # ── ЗАПАС: главная величина ────────────────────────────────────────────
    d = 1.0 if side == "Up" else -1.0
    t = max(seconds_left, 0.5)
    buffer_usd, z, sig_t = buffer_z(side=side, price=price, target=target,
                                    seconds_left=seconds_left, sigma=m.sigma)
    r.z, r.buffer_usd = z, buffer_usd
    r.p_model = phi(z)
    r.edge_cents = (r.p_model - ask) * 100

    if z < p.z_min:
        veto(f"запас {z:.1f}σ < {p.z_min:.1f}σ — раунд ещё может перевернуться")
    add("запас", p.w_buffer, 1.0 - _ramp(z, p.z_min, p.z_full),
        f"{z:.1f}σ (${buffer_usd:+.0f} при σ√t=${sig_t:.1f})")

    # ── СКОРОСТЬ ПРОТИВ НАС ────────────────────────────────────────────────
    # Не «быстро ли движется цена», а «сколько запаса съест эта скорость за
    # оставшееся время». Медленный ход при малом запасе опаснее быстрого при
    # большом.
    if m.speed is not None and buffer_usd > 0:
        against = max(0.0, -d * m.speed)          # $/с в сторону таргета
        eat = against * t / buffer_usd
        add("скорость", p.w_speed,
            _ramp(eat, p.speed_eat_good, p.speed_eat_bad),
            f"съест {eat * 100:.0f}% запаса ({against:.2f}$/с)")
    else:
        add("скорость", p.w_speed, 0.5, "не измерена")

    # ── РИСК СКАЧКА ────────────────────────────────────────────────────────
    # σ описывает непрерывное блуждание и про скачки не знает ничего.
    # Смотрим, сколько ХУДШИХ наблюдённых секунд укладывается в запас.
    if m.max_jump_1s and m.max_jump_1s > 0 and buffer_usd > 0:
        cover = buffer_usd / m.max_jump_1s
        add("скачок", p.w_jump,
            _ramp(cover, p.jump_cover_good, p.jump_cover_bad),
            f"запас = {cover:.1f}× худшей секунды (${m.max_jump_1s:.0f})")
    else:
        add("скачок", p.w_jump, 0.5, "не измерен")

    # ── РЕЖИМ ВОЛАТИЛЬНОСТИ ────────────────────────────────────────────────
    # Всплеск означает, что σ, посчитанная по прошлому, занижает будущее —
    # то есть z завышен именно тогда, когда ошибаться дороже всего.
    if m.vol_regime is not None:
        add("режим", p.w_regime,
            _ramp(m.vol_regime, p.regime_good, p.regime_bad),
            f"σ быстрая/медленная = {m.vol_regime:.2f}")
    else:
        add("режим", p.w_regime, 0.5, "не измерен")

    # ── ЛИКВИДНОСТЬ ────────────────────────────────────────────────────────
    bids, asks = our_levels
    bid_usd = depth_usd(bids)
    # По НАШЕЙ цене, а не «сколько всего в книге»: ордер уходит FAK с лимитом,
    # и уровни дороже лимита нам недоступны. Иначе «в книге $2000» означает
    # $40 по нашей цене и частичный филл на ровном месте.
    ask_usd = fillable_usd(asks, round(ask, 2))
    if ask_usd < p.min_depth_usd:
        veto(f"по цене {ask:.2f} в книге всего ${ask_usd:.0f} — "
             f"набрать позицию нечем")
    elif need_usd > 0 and ask_usd < need_usd:
        veto(f"по цене {ask:.2f} лежит ${ask_usd:.0f}, а ступень ${need_usd:.0f}"
             f" — филл будет частичным")
    add("глубина", p.w_depth,
        _ramp(min(ask_usd, bid_usd), p.depth_good_usd, p.depth_bad_usd),
        f"ask ${ask_usd:.0f} / bid ${bid_usd:.0f}")

    if spread_c is not None:
        add("спред", p.w_spread, _ramp(spread_c, p.spread_good_c,
                                       p.spread_bad_c), f"{spread_c:.1f}¢")
    else:
        add("спред", p.w_spread, 0.5, "нет бида")

    # ── ИСЧЕЗНОВЕНИЕ БИДОВ ─────────────────────────────────────────────────
    # Маркет-мейкер снимает свои заявки, когда знает что-то, чего не знаем мы.
    # Это единственный фактор, который смотрит на ЧУЖОЕ поведение, а не на цену.
    if bid_depth_before and bid_depth_before > 0:
        ratio = bid_usd / bid_depth_before
        add("уход бидов", p.w_biddrop,
            _ramp(ratio, p.bid_drop_good, p.bid_drop_bad),
            f"глубина бидов {ratio * 100:.0f}% от прежней")
    else:
        add("уход бидов", p.w_biddrop, 0.0, "не с чем сравнить")

    # ── «ПИЛА» ─────────────────────────────────────────────────────────────
    if m.flips is not None:
        add("пила", p.w_flips, _ramp(m.flips, p.flips_good, p.flips_bad),
            f"{m.flips} смен направления")
    else:
        add("пила", p.w_flips, 0.0, "не измерена")

    # ── РАСХОЖДЕНИЕ С ЯКОРЕМ ───────────────────────────────────────────────
    # Раунд считает Polymarket по СВОЕЙ цене. Если наша сильно расходится,
    # то либо наш фид врёт, либо их якорь застрял — в обоих случаях наш z
    # посчитан не по той цене, по которой раунд будет рассчитан.
    if pm_price is not None and m.sigma:
        diff_sig = abs(price - pm_price) / (m.sigma * math.sqrt(t))
        add("якорь", p.w_anchor,
            _ramp(diff_sig, p.anchor_good_sig, p.anchor_bad_sig),
            f"расхождение {abs(price - pm_price):.0f}$ = {diff_sig:.2f}σ")
    else:
        add("якорь", p.w_anchor, 0.0, "якорь недоступен")

    return r
