"""Fair-probability model: what SHOULD Up/Down cost right now?

The market's quoted price (e.g. Up at 0.99) is just what the crowd pays — it
is not always the real probability. This module computes an independent
estimate from first principles, so the bot can refuse entries where the quoted
0.98/0.99 is NOT backed by the math (those are exactly the trades that reverse
in the last seconds and eat 100 wins in one loss).

Model
-----
Over the few remaining minutes of a window the price change is well
approximated by a zero-drift random walk. If the coin is ``d`` dollars above
the strike with ``t`` seconds left, and moves ``sigma`` dollars per sqrt-second,
then the close lands above the strike with probability

    P(Up) = Phi( d / (sigma * sqrt(t)) )

where Phi is the standard normal CDF. ``sigma`` is measured live from the
Chainlink stream (RMS of ~1-second returns over a rolling lookback), so the
model reacts to the market speeding up or calming down within ~a minute.

Honest caveats: crypto returns have fatter tails than the normal distribution
and news spikes raise sigma faster than a trailing estimate can see. The model
is therefore used as a conservative FILTER with a threshold safely above the
break-even probability — not as an oracle.

Everything here is pure computation (no I/O) and unit-tested.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from typing import Deque, Optional, Tuple


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_up_probability(
    price: Optional[float],
    strike: Optional[float],
    sigma_1s: Optional[float],
    seconds_left: float,
) -> Optional[float]:
    """P(close > strike) under the random-walk model; None if not computable.

    ``sigma_1s`` is the dollar move per sqrt-second. With no time left the
    outcome is already decided by the sign of (price - strike).
    """
    if price is None or strike is None:
        return None
    if seconds_left <= 0:
        if price > strike:
            return 1.0
        if price < strike:
            return 0.0
        return 0.5
    if sigma_1s is None or sigma_1s <= 0:
        return None
    z = (price - strike) / (sigma_1s * math.sqrt(seconds_left))
    return norm_cdf(z)


def norm_pdf(x: float) -> float:
    """Плотность стандартного нормального распределения."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def price_sensitivity(
    price: Optional[float],
    strike: Optional[float],
    sigma_1s: Optional[float],
    seconds_left: float,
) -> Optional[float]:
    """Насколько сдвинется «процент» Up при движении монеты на $1.

    Это производная dP/d(цена) от той же модели, что и fair_up_probability:

        P = Phi(z),  z = (цена − таргет) / (sigma * sqrt(t))
        dP/dцена = phi(z) / (sigma * sqrt(t))

    Отсюда сразу видно, почему процент иногда НЕ реагирует на скачок цены:
    phi(z) падает экспоненциально по мере удаления от таргета. При z=6
    (цена ушла на шесть «сигм») phi(z) ~ 6e-9 — рынок уже всё решил, и любое
    движение цены ничего не меняет. Максимум чувствительности — ровно на
    таргете (z=0), где phi(0)=0.399.

    Возвращает долю вероятности на доллар (0.01 = 1 цент на $1). None, если
    посчитать не из чего.
    """
    if price is None or strike is None or seconds_left <= 0:
        return None
    if sigma_1s is None or sigma_1s <= 0:
        return None
    denom = sigma_1s * math.sqrt(seconds_left)
    if denom <= 0:
        return None
    z = (price - strike) / denom
    return norm_pdf(z) / denom


def expected_shift(
    price: Optional[float],
    strike: Optional[float],
    sigma_1s: Optional[float],
    seconds_left: float,
    move_usd: float,
) -> Optional[float]:
    """На сколько изменится «процент» Up, если монета сходит на move_usd.

    Считаем ЧЕСТНО, через разность двух вероятностей, а не через производную:
    на больших скачках линейное приближение сильно врёт (кривая Phi изгибается).
    Знак результата совпадает со знаком move_usd.
    """
    p0 = fair_up_probability(price, strike, sigma_1s, seconds_left)
    if p0 is None or price is None:
        return None
    p1 = fair_up_probability(price + move_usd, strike, sigma_1s, seconds_left)
    if p1 is None:
        return None
    return p1 - p0


class VolEstimator:
    """Rolling estimate of the per-sqrt-second dollar volatility.

    Feed it the live price (``add``); it keeps ~1-second samples over a
    lookback window and returns the RMS of sqrt(dt)-normalised returns.
    RMS (zero-mean) is the standard choice at this horizon — drift over a few
    minutes is negligible next to the noise. Thread-safe; ``sigma_1s`` returns
    None until there is enough history (a cold start never fakes confidence).
    """

    def __init__(self, lookback_seconds: float = 120.0,
                 sample_interval: float = 1.0, min_samples: int = 10):
        self.lookback = float(lookback_seconds)
        self.sample_interval = float(sample_interval)
        self.min_samples = int(min_samples)
        self._lock = threading.Lock()
        self._samples: Deque[Tuple[float, float]] = deque()   # (ts, price)

    def add(self, ts: float, price: Optional[float]) -> None:
        if price is None:
            return
        with self._lock:
            if self._samples and ts - self._samples[-1][0] < self.sample_interval:
                return   # keep a steady ~1 s cadence regardless of tick rate
            self._samples.append((ts, float(price)))
            while self._samples and ts - self._samples[0][0] > self.lookback:
                self._samples.popleft()

    def sigma_1s(self) -> Optional[float]:
        """Dollar volatility per sqrt-second, or None if history is too short."""
        with self._lock:
            samples = list(self._samples)
        if len(samples) < self.min_samples + 1:
            return None
        acc = 0.0
        n = 0
        for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            r = (p1 - p0) / math.sqrt(dt)
            acc += r * r
            n += 1
        if n < self.min_samples:
            return None
        sigma = math.sqrt(acc / n)
        return sigma if sigma > 0 else None

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
