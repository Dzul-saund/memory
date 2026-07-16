"""Unit tests for the fair-probability model (pure math, no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.prob import VolEstimator, fair_up_probability, norm_cdf  # noqa: E402


# --- norm_cdf -----------------------------------------------------------------
def test_norm_cdf_basics():
    assert abs(norm_cdf(0.0) - 0.5) < 1e-9
    assert norm_cdf(3.0) > 0.99
    assert norm_cdf(-3.0) < 0.01
    assert abs(norm_cdf(1.0) + norm_cdf(-1.0) - 1.0) < 1e-9   # symmetry


# --- fair_up_probability --------------------------------------------------------
def test_at_the_strike_is_fifty_fifty():
    assert abs(fair_up_probability(100.0, 100.0, 1.0, 60.0) - 0.5) < 1e-9


def test_far_above_strike_is_near_certain():
    # $50 above with sigma*sqrt(t) = $1*sqrt(25s) = $5 -> 10 sigma -> ~100%
    p = fair_up_probability(100_050.0, 100_000.0, 1.0, 25.0)
    assert p > 0.9999


def test_below_strike_favours_down():
    p = fair_up_probability(99_990.0, 100_000.0, 1.0, 100.0)
    assert p < 0.5


def test_probability_decays_with_more_time_left():
    # Same $10 cushion is worth less with more time on the clock.
    p_short = fair_up_probability(100_010.0, 100_000.0, 1.0, 9.0)
    p_long = fair_up_probability(100_010.0, 100_000.0, 1.0, 900.0)
    assert p_short > p_long > 0.5


def test_no_time_left_is_decided_by_sign():
    assert fair_up_probability(100_001.0, 100_000.0, 1.0, 0.0) == 1.0
    assert fair_up_probability(99_999.0, 100_000.0, 1.0, 0.0) == 0.0
    assert fair_up_probability(100_000.0, 100_000.0, 1.0, 0.0) == 0.5


def test_unknown_inputs_return_none():
    assert fair_up_probability(None, 100.0, 1.0, 60.0) is None
    assert fair_up_probability(100.0, None, 1.0, 60.0) is None
    assert fair_up_probability(100.0, 100.0, None, 60.0) is None
    assert fair_up_probability(100.0, 100.0, 0.0, 60.0) is None


# --- VolEstimator ---------------------------------------------------------------
def test_vol_estimator_needs_enough_history():
    v = VolEstimator(lookback_seconds=120, min_samples=10)
    for i in range(5):
        v.add(float(i), 100.0 + i)
    assert v.sigma_1s() is None


def test_vol_estimator_measures_known_volatility():
    # Price alternates +1/-1 every second -> |1s return| = $1 -> sigma ~= 1.
    v = VolEstimator(lookback_seconds=120, min_samples=10)
    price = 100.0
    for i in range(60):
        price += 1.0 if i % 2 == 0 else -1.0
        v.add(float(i), price)
    sigma = v.sigma_1s()
    assert sigma is not None and abs(sigma - 1.0) < 1e-6


def test_vol_estimator_flat_price_gives_none():
    v = VolEstimator(lookback_seconds=120, min_samples=10)
    for i in range(60):
        v.add(float(i), 100.0)     # never moves -> sigma 0 -> None (unusable)
    assert v.sigma_1s() is None


def test_vol_estimator_keeps_one_second_cadence():
    v = VolEstimator(lookback_seconds=120, sample_interval=1.0, min_samples=2)
    v.add(0.0, 100.0)
    v.add(0.2, 500.0)   # same second — ignored, not a real 1s move
    v.add(1.0, 101.0)
    v.add(2.0, 100.0)
    v.add(3.0, 101.0)
    sigma = v.sigma_1s()
    assert sigma is not None and abs(sigma - 1.0) < 1e-6


def test_vol_estimator_prunes_old_samples():
    v = VolEstimator(lookback_seconds=10, min_samples=3)
    for i in range(100):
        v.add(float(i), 100.0 + (i % 2))
    assert len(v._samples) <= 12   # only ~lookback seconds retained
