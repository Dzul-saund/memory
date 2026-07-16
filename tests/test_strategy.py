"""Unit tests for the pure buy/no-buy decision (no network, no credentials)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.strategy import evaluate  # noqa: E402

UP = "token-up"
DOWN = "token-down"


def base(**overrides):
    args = dict(
        up_price=0.50,
        down_price=0.50,
        time_to_end=30,
        balance=100.0,
        token_up=UP,
        token_down=DOWN,
        time_window=70.0,
        price_min=0.84,
        price_max=0.99,
        trade_size=5.0,
        already_bought=False,
    )
    args.update(overrides)
    return args


def test_buys_up_when_up_in_band():
    d = evaluate(**base(up_price=0.90, down_price=0.10))
    assert d.should_buy and d.outcome == "Up" and d.token_id == UP
    assert d.price == 0.90


def test_buys_down_when_down_in_band():
    d = evaluate(**base(up_price=0.08, down_price=0.92))
    assert d.should_buy and d.outcome == "Down" and d.token_id == DOWN


def test_no_buy_when_price_below_band():
    d = evaluate(**base(up_price=0.80, down_price=0.20))
    assert not d.should_buy


def test_no_buy_when_price_above_band():
    # 0.995 is above PRICE_MAX (0.99) — too late / too certain.
    d = evaluate(**base(up_price=0.995, down_price=0.005))
    assert not d.should_buy


def test_no_buy_when_too_early():
    d = evaluate(**base(up_price=0.90, time_to_end=71))
    assert not d.should_buy
    assert "too early" in d.reason


def test_buys_at_exact_time_boundary():
    # 70s remaining is inclusive.
    d = evaluate(**base(up_price=0.90, time_to_end=70))
    assert d.should_buy


def test_band_is_inclusive_on_both_ends():
    assert evaluate(**base(up_price=0.84, down_price=0.16)).should_buy
    assert evaluate(**base(up_price=0.99, down_price=0.01)).should_buy


def test_no_buy_when_already_bought():
    d = evaluate(**base(up_price=0.90, already_bought=True))
    assert not d.should_buy
    assert "already" in d.reason


def test_no_buy_when_balance_too_low():
    d = evaluate(**base(up_price=0.90, balance=4.0, trade_size=5.0))
    assert not d.should_buy
    assert "insufficient" in d.reason


def test_up_preferred_first():
    # Both nominally in band (not realistic, but Up must be chosen first).
    d = evaluate(**base(up_price=0.90, down_price=0.90))
    assert d.should_buy and d.outcome == "Up"


def test_handles_missing_prices():
    d = evaluate(**base(up_price=None, down_price=None))
    assert not d.should_buy


def test_buys_down_when_up_price_missing():
    d = evaluate(**base(up_price=None, down_price=0.95))
    assert d.should_buy and d.outcome == "Down"


# --- target-distance cushion filter ----------------------------------------
def test_distance_filter_allows_up_with_enough_cushion():
    # Up favourite, BTC $50 above the open, need >= $45 — allowed.
    d = evaluate(**base(up_price=0.90, down_price=0.10,
                        btc_price=100_050.0, target_open=100_000.0,
                        min_target_distance=45.0))
    assert d.should_buy and d.outcome == "Up"


def test_distance_filter_blocks_up_when_cushion_too_small():
    # Up favourite but BTC only $10 above the open, need $45 — blocked.
    d = evaluate(**base(up_price=0.90, down_price=0.10,
                        btc_price=100_010.0, target_open=100_000.0,
                        min_target_distance=45.0))
    assert not d.should_buy and "cushion" in d.reason


def test_distance_filter_allows_down_when_btc_below_target():
    # Down favourite, BTC $40 below the open, need >= $30 — allowed.
    d = evaluate(**base(up_price=0.08, down_price=0.92,
                        btc_price=99_960.0, target_open=100_000.0,
                        min_target_distance=30.0))
    assert d.should_buy and d.outcome == "Down"


def test_distance_filter_blocks_down_when_btc_on_wrong_side():
    # Down favourite by price, but BTC is ABOVE the open — negative cushion.
    d = evaluate(**base(up_price=0.08, down_price=0.92,
                        btc_price=100_020.0, target_open=100_000.0,
                        min_target_distance=30.0))
    assert not d.should_buy and "cushion" in d.reason


def test_distance_filter_blocks_when_target_unknown():
    d = evaluate(**base(up_price=0.90, down_price=0.10,
                        btc_price=100_050.0, target_open=None,
                        min_target_distance=45.0))
    assert not d.should_buy and "distance" in d.reason


def test_distance_filter_ignored_when_zero():
    # min_target_distance=0 disables the check even with no BTC/target data.
    d = evaluate(**base(up_price=0.90, down_price=0.10,
                        btc_price=None, target_open=None,
                        min_target_distance=0.0))
    assert d.should_buy and d.outcome == "Up"


# --- early/late split (time-based price rule) --------------------------------
def early(**overrides):
    """bot4-style config: band 0.98..0.99, no time limit, 120s early split."""
    args = base(time_window=600.0, price_min=0.98, price_max=0.99,
                early_threshold=120.0, early_price_min=0.99,
                early_require_rising=True)
    args.update(overrides)
    return args


def test_early_regime_rejects_098():
    # > 120s left: 0.98 is inside the normal band but NOT the early band.
    d = evaluate(**early(up_price=0.98, down_price=0.02,
                         time_to_end=200, up_rising=True))
    assert not d.should_buy


def test_early_regime_buys_099_when_rising():
    d = evaluate(**early(up_price=0.99, down_price=0.01,
                         time_to_end=200, up_rising=True))
    assert d.should_buy and d.outcome == "Up" and d.price == 0.99


def test_early_regime_blocks_099_when_not_rising():
    d = evaluate(**early(up_price=0.99, down_price=0.01,
                         time_to_end=200, up_rising=False))
    assert not d.should_buy and "not rising" in d.reason


def test_early_regime_blocks_when_trend_unknown():
    # No trend data yet (e.g. right after a window rollover) -> no early buy.
    d = evaluate(**early(up_price=0.99, down_price=0.01,
                         time_to_end=200, up_rising=None))
    assert not d.should_buy and "unknown" in d.reason


def test_early_regime_checks_the_down_side_trend():
    d = evaluate(**early(up_price=0.01, down_price=0.99,
                         time_to_end=200, down_rising=True))
    assert d.should_buy and d.outcome == "Down"
    d = evaluate(**early(up_price=0.01, down_price=0.99,
                         time_to_end=200, down_rising=False))
    assert not d.should_buy


def test_late_regime_buys_098_no_trend_needed():
    # <= 120s left: normal 0.98..0.99 band, trend is irrelevant.
    d = evaluate(**early(up_price=0.98, down_price=0.02,
                         time_to_end=119, up_rising=False))
    assert d.should_buy and d.outcome == "Up" and d.price == 0.98


def test_late_regime_boundary_is_inclusive():
    # exactly 120s left counts as the LATE regime (rule is "more than 120s").
    d = evaluate(**early(up_price=0.98, down_price=0.02,
                         time_to_end=120, up_rising=None))
    assert d.should_buy


def test_early_split_disabled_when_threshold_zero():
    d = evaluate(**early(up_price=0.98, down_price=0.02,
                         time_to_end=200, early_threshold=0.0))
    assert d.should_buy


def test_early_rising_not_required_when_flag_off():
    d = evaluate(**early(up_price=0.99, down_price=0.01,
                         time_to_end=200, early_require_rising=False,
                         up_rising=None))
    assert d.should_buy


# --- fair-probability model filter --------------------------------------------
def modelargs(**overrides):
    """bot4-style late-regime entry with the model filter on (>= 99.5%)."""
    args = base(time_window=600.0, price_min=0.98, price_max=0.99,
                up_price=0.98, down_price=0.02, time_to_end=60,
                min_model_prob=0.995, model_strict=True)
    args.update(overrides)
    return args


def test_model_filter_allows_confident_up():
    d = evaluate(**modelargs(model_prob_up=0.998))
    assert d.should_buy and d.outcome == "Up"
    assert "model P(Up)=99.80%" in d.reason


def test_model_filter_blocks_unconfident_up():
    # Market quotes 0.98 but the math says only 97% — the dangerous entry.
    d = evaluate(**modelargs(model_prob_up=0.97))
    assert not d.should_buy and "model P(Up)=97.00%" in d.reason


def test_model_filter_checks_the_down_side():
    # P(Up)=0.3% -> P(Down)=99.7% >= 99.5% — Down is allowed.
    d = evaluate(**modelargs(up_price=0.02, down_price=0.98,
                             model_prob_up=0.003))
    assert d.should_buy and d.outcome == "Down"
    d = evaluate(**modelargs(up_price=0.02, down_price=0.98,
                             model_prob_up=0.02))   # P(Down)=98% — blocked
    assert not d.should_buy


def test_model_strict_blocks_when_no_estimate():
    d = evaluate(**modelargs(model_prob_up=None))
    assert not d.should_buy and "no estimate" in d.reason


def test_model_non_strict_falls_back_to_band_rules():
    d = evaluate(**modelargs(model_prob_up=None, model_strict=False))
    assert d.should_buy and d.outcome == "Up"


def test_model_filter_disabled_when_threshold_zero():
    d = evaluate(**modelargs(model_prob_up=0.5, min_model_prob=0.0))
    assert d.should_buy


def test_model_filter_combines_with_early_rule():
    # > 120s left: needs 0.99 price, rising trend AND model confidence.
    d = evaluate(**modelargs(up_price=0.99, down_price=0.01, time_to_end=200,
                             early_threshold=120.0, up_rising=True,
                             model_prob_up=0.999))
    assert d.should_buy
    d = evaluate(**modelargs(up_price=0.99, down_price=0.01, time_to_end=200,
                             early_threshold=120.0, up_rising=True,
                             model_prob_up=0.99))
    assert not d.should_buy
