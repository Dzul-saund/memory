"""Unit tests for Chainlink price-feed message handling (no network)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.pricefeed import ChainlinkPriceFeed  # noqa: E402


def snapshot(points):
    return json.dumps({
        "topic": "crypto_prices",
        "type": "subscribe",
        "payload": {"data": [{"timestamp": ts, "value": v} for ts, v in points]},
    })


def test_ingest_snapshot_takes_last_point():
    f = ChainlinkPriceFeed()
    f._ingest(snapshot([(1000, 64000.0), (2000, 64010.0)]))
    assert f.get() == 64010.0


def test_older_snapshot_never_overwrites_newer_point():
    # Polling several times per second can deliver snapshots out of order.
    f = ChainlinkPriceFeed()
    f._ingest(snapshot([(2000, 64010.0)]))
    f._ingest(snapshot([(1000, 64000.0)]))   # stale — must be ignored
    assert f.get() == 64010.0
    f._ingest(snapshot([(3000, 64020.0)]))   # newer — accepted
    assert f.get() == 64020.0


def test_ingest_incremental_value_payload():
    f = ChainlinkPriceFeed()
    f._ingest(json.dumps({"payload": {"value": 64005.5, "timestamp": 5000}}))
    assert f.get() == 64005.5


def test_garbage_and_empty_messages_are_ignored():
    f = ChainlinkPriceFeed()
    f._ingest("")
    f._ingest("PONG")
    f._ingest("{broken json")
    f._ingest(json.dumps({"payload": {"data": []}}))
    assert f.get() is None


def test_stale_value_expires():
    f = ChainlinkPriceFeed()
    f._ingest(snapshot([(1000, 64000.0)]))
    f._ts -= 100.0    # pretend it arrived long ago
    assert f.get(max_age=8.0) is None
