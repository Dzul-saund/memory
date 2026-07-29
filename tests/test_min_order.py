"""Минимальный размер ордера: шэры округляются вверх, а не вниз.

Polymarket не принимает ордера меньше минимального размера, а округление
шэров ВНИЗ до сотых опускало под этот минимум КАЖДУЮ покупку на ставку $1:
$1.00 / 0.53 = 1.8867 шэра -> floor2 -> 1.88 -> $0.9964.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.util import ceil2, floor2  # noqa: E402


class TestCeil2:
    def test_rounds_up_to_hundredths(self):
        assert ceil2(1.8867924528) == 1.89
        assert ceil2(3.2258064516) == 3.23

    def test_exact_values_are_left_alone(self):
        """1.89*100 в двоичной дроби = 188.99999999999997 — без округления
        перед ceil здесь появилась бы лишняя сотая."""
        assert ceil2(1.89) == 1.89
        assert ceil2(0.5) == 0.5
        assert ceil2(2.0) == 2.0

    def test_still_differs_from_floor_where_it_matters(self):
        assert floor2(1.8867) == 1.88
        assert ceil2(1.8867) == 1.89


class TestOrderReachesTheMinimum:
    @pytest.mark.parametrize("ask", [0.05, 0.31, 0.53, 0.66, 0.80, 0.95])
    def test_stake_of_one_dollar_never_lands_below_it(self, ask):
        shares = ceil2(1.00 / ask)
        assert round(shares * ask, 2) >= 1.00

    @pytest.mark.parametrize("ask", [0.31, 0.53, 0.66, 0.95])
    def test_these_prices_used_to_fall_short(self, ask):
        """Цены, на которых старое округление вниз давало < $1.

        0.05 и 0.80 делят доллар нацело, поэтому им везло и раньше — здесь
        именно те, где не везло.
        """
        assert round(floor2(1.00 / ask) * ask, 4) < 1.00

    def test_overshoot_stays_within_one_cent_of_a_share(self, ask=0.53):
        """Добиваем вверх, но не переплачиваем заметно."""
        shares = ceil2(1.00 / ask)
        assert round(shares * ask, 2) - 1.00 <= 0.01
