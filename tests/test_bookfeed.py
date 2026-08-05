"""Unit tests for the live order-book maintenance (no network needed)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.bookfeed import MarketBookFeed, OrderBook  # noqa: E402

UP = "111"
DOWN = "222"


def make_feed():
    feed = MarketBookFeed(logger=None)
    feed.set_assets([UP, DOWN])
    return feed


def snapshot(asset, bids, asks):
    return json.dumps({
        "event_type": "book",
        "asset_id": asset,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    })


# --- OrderBook (pure logic) ---------------------------------------------------
def test_snapshot_best_bid_ask():
    b = OrderBook()
    b.apply_snapshot(
        [{"price": "0.97", "size": "10"}, {"price": "0.98", "size": "5"}],
        [{"price": "0.99", "size": "7"}, {"price": "1.00", "size": "3"}],
    )
    assert b.best_bid() == 0.98
    assert b.best_ask() == 0.99


def test_change_updates_and_removes_levels():
    b = OrderBook()
    b.apply_snapshot([{"price": "0.98", "size": "5"}],
                     [{"price": "0.99", "size": "7"}])
    b.apply_change("SELL", "0.985", "4")      # tighter ask appears
    assert b.best_ask() == 0.985
    b.apply_change("SELL", "0.985", "0")      # ...and is taken out
    assert b.best_ask() == 0.99
    b.apply_change("BUY", "0.98", "0")        # best bid removed
    assert b.best_bid() is None


def test_zero_size_levels_in_snapshot_are_ignored():
    b = OrderBook()
    b.apply_snapshot([{"price": "0.98", "size": "0"}],
                     [{"price": "0.99", "size": "1"}])
    assert b.best_bid() is None and b.best_ask() == 0.99


# --- MarketBookFeed message ingestion -------------------------------------------
def test_ingest_snapshot_and_get_top():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    assert feed.get_top(UP) == (0.98, 0.99)
    assert feed.get_top(DOWN) is None      # no snapshot for DOWN yet


def test_ingest_array_of_snapshots():
    feed = make_feed()
    feed._ingest("[" + snapshot(UP, [(0.98, 5)], [(0.99, 7)]) + ","
                 + snapshot(DOWN, [(0.01, 100)], [(0.02, 50)]) + "]")
    assert feed.get_top(UP) == (0.98, 0.99)
    assert feed.get_top(DOWN) == (0.01, 0.02)


def test_ingest_price_change_old_shape():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    feed._ingest(json.dumps({
        "event_type": "price_change", "asset_id": UP,
        "changes": [{"price": "0.99", "side": "SELL", "size": "0"},
                    {"price": "0.995", "side": "SELL", "size": "2"}],
    }))
    assert feed.get_top(UP) == (0.98, 0.995)


def test_ingest_price_change_new_shape():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    feed._ingest(json.dumps({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": UP, "price": "0.985", "side": "BUY", "size": "9"}
        ],
    }))
    assert feed.get_top(UP) == (0.985, 0.99)


def test_ingest_last_trade_price():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    feed._ingest(json.dumps({
        "event_type": "last_trade_price", "asset_id": UP, "price": "0.99",
    }))
    assert feed.get_last_trade(UP) == 0.99


def test_pong_and_garbage_are_ignored():
    feed = make_feed()
    feed._ingest("PONG")
    feed._ingest("not json {")
    assert feed.get_top(UP) is None


def test_set_assets_resets_books():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    feed.set_assets(["333", "444"])     # window rolled over -> new tokens
    assert feed.get_top(UP) is None
    assert feed.get_top("333") is None  # no snapshot yet for the new ones


def test_stale_connection_returns_none():
    feed = make_feed()
    feed._ingest(snapshot(UP, [(0.98, 5)], [(0.99, 7)]))
    feed._last_rx -= 999.0              # pretend nothing arrived for ages
    assert feed.get_top(UP) is None
