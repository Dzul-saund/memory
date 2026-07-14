"""Tests for the dry-run settlement / profit-and-loss simulation."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.config import Config           # noqa: E402
from btc_bot.trader import DryRunTrader      # noqa: E402
from btc_bot.bot import Bot                  # noqa: E402

logging.disable(logging.CRITICAL)


def _cfg():
    return Config(dry_run=True, dry_run_balance=100.0, private_key=None)


# --- DryRunTrader: buy debits, winning settlement credits -------------------
def test_buy_debits_balance():
    t = DryRunTrader(_cfg(), logging.getLogger("t"))
    t.buy("x", 0.80, 5.0)            # cost 4.00
    assert round(t.get_balance(), 2) == 96.00


def test_winning_settlement_credits_payout():
    t = DryRunTrader(_cfg(), logging.getLogger("t"))
    t.buy("x", 0.80, 5.0)            # spend 4.00  -> 96.00
    t.settle(5.0)                    # win: 5 shares * $1 = 5.00 -> 101.00
    assert round(t.get_balance(), 2) == 101.00


def test_losing_settlement_credits_nothing():
    t = DryRunTrader(_cfg(), logging.getLogger("t"))
    t.buy("x", 0.80, 5.0)            # spend 4.00 -> 96.00
    t.settle(0.0)                    # loss
    assert round(t.get_balance(), 2) == 96.00


# --- Bot._settle_position: win/loss accounting ------------------------------
def _bot_with_position(last_price):
    bot = Bot(_cfg())
    bot.open_position = {
        "slug": "btc-updown-5m-1",
        "outcome": "Up",
        "token_id": "x",
        "size": 5.0,
        "cost": 4.0,
        "last_price": last_price,
    }
    return bot


def test_settle_win():
    bot = _bot_with_position(last_price=0.99)
    bot._settle_position()
    assert bot.wins == 1 and bot.losses == 0
    assert round(bot.realized_pnl, 2) == 1.00     # payout 5 - cost 4
    assert bot.open_position is None


def test_settle_loss():
    bot = _bot_with_position(last_price=0.01)
    bot._settle_position()
    assert bot.wins == 0 and bot.losses == 1
    assert round(bot.realized_pnl, 2) == -4.00    # lost the whole stake
    assert bot.open_position is None


def test_settle_win_credits_trader_balance():
    bot = Bot(_cfg())
    bot.trader.buy("x", 0.80, 5.0)                # spend 4.00 -> 96.00
    bot.open_position = {
        "slug": "s", "outcome": "Up", "token_id": "x",
        "size": 5.0, "cost": 4.0, "last_price": 0.99,
    }
    bot._settle_position()
    assert round(bot.trader.get_balance(), 2) == 101.00


# --- hedge: opposite-side lottery bet ---------------------------------------
def test_hedge_opposite_win_offsets_main_loss():
    bot = Bot(_cfg())
    # main: bought Up @ 0.98 for $50 (51.02 sh) — loses (last 0.01)
    bot.open_position = {
        "slug": "s", "outcome": "Up", "token_id": "u",
        "size": 51.02, "cost": 50.0, "last_price": 0.01,
    }
    # hedge: bought Down @ 0.01 for $1 (100 sh) — wins (last 0.99)
    bot.hedge_position = {
        "slug": "s", "outcome": "Down", "token_id": "d",
        "size": 100.0, "cost": 1.0, "last_price": 0.99,
    }
    bot._settle_position()
    bot._settle_hedge()
    # main: -50 ; hedge: +99 (payout 100 - cost 1) ; net +49
    assert round(bot.realized_pnl, 2) == 49.00
    assert bot.wins == 1 and bot.losses == 1
    assert bot.open_position is None and bot.hedge_position is None


def test_hedge_loses_when_main_wins():
    bot = Bot(_cfg())
    bot.open_position = {
        "slug": "s", "outcome": "Up", "token_id": "u",
        "size": 51.02, "cost": 50.0, "last_price": 0.99,
    }
    bot.hedge_position = {
        "slug": "s", "outcome": "Down", "token_id": "d",
        "size": 100.0, "cost": 1.0, "last_price": 0.01,
    }
    bot._settle_position()
    bot._settle_hedge()
    # main: payout 51.02 - cost 50 = +1.02 ; hedge: -1.00 ; net +0.02
    assert round(bot.realized_pnl, 2) == 0.02
    assert bot.wins == 1 and bot.losses == 1
