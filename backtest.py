#!/usr/bin/env python3
"""Backtest the BTC Up/Down 5m strategy on REAL historical data.

For each past 5-minute window it pulls:
  * the actual resolution (winner) from the Gamma API, and
  * the real price of each outcome near entry time from the CLOB
    `prices-history` endpoint (no look-ahead: the most recent price at or
    before the entry moment is used).

It then replays the "buy the favourite in [price_min, price_max]" rule and
reports win rate, P&L and ROI. Window data is cached to disk so re-runs (and
parameter sweeps) are instant.

Examples:
    python backtest.py --windows 300
    python backtest.py --windows 300 --price-max 0.92
    python backtest.py --windows 300 --sweep
    python backtest.py --windows 300 --entry-seconds 40 --slippage 0.01
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from typing import List, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WINDOW = 300

session = requests.Session()
session.headers.update({"User-Agent": "btc-updown-backtest/1.0"})


# ---------------------------------------------------------------------------
#  Data collection
# ---------------------------------------------------------------------------
def _get(url: str, params: Optional[dict] = None, retries: int = 4):
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)


def fetch_window(slug_prefix: str, ws: int) -> Optional[dict]:
    """Return {ws, winner, up:[[t,p]...], down:[[t,p]...]} for a window, or None."""
    slug = f"{slug_prefix}{ws}"
    data = _get(f"{GAMMA}/markets", {"slug": slug, "closed": "true"}) or \
        _get(f"{GAMMA}/markets", {"slug": slug})
    if not data:
        return None
    m = data[0]
    try:
        outcomes = json.loads(m["outcomes"])
        prices = json.loads(m["outcomePrices"])
        tokens = json.loads(m["clobTokenIds"])
    except (KeyError, ValueError):
        return None
    if not m.get("closed"):
        return None
    winner = None
    for o, p in zip(outcomes, prices):
        if float(p) >= 0.99:
            winner = o
    if winner is None:
        return None  # void / unresolved

    up_i = outcomes.index("Up") if "Up" in outcomes else 0
    dn_i = outcomes.index("Down") if "Down" in outcomes else 1
    end = ws + WINDOW

    def hist(token):
        d = _get(f"{CLOB}/prices-history",
                 {"market": token, "startTs": ws - 60, "endTs": end + 60, "fidelity": 1})
        return [[int(x["t"]), float(x["p"])] for x in (d.get("history") or [])]

    return {
        "ws": ws,
        "winner": winner,
        "up": hist(str(tokens[up_i])),
        "down": hist(str(tokens[dn_i])),
    }


def collect(slug_prefix: str, n_windows: int, cache_path: str, use_cache: bool) -> List[dict]:
    cache = {}
    if use_cache and os.path.exists(cache_path):
        with open(cache_path) as f:
            for w in json.load(f):
                cache[w["ws"]] = w

    now = int(time.time())
    latest_closed = now - (now % WINDOW) - WINDOW  # last fully-finished window
    wanted = [latest_closed - i * WINDOW for i in range(n_windows)]

    missing = [ws for ws in wanted if ws not in cache]
    if missing:
        print(f"Fetching {len(missing)} new windows "
              f"({len(wanted) - len(missing)} already cached)...")
    for i, ws in enumerate(missing, 1):
        try:
            w = fetch_window(slug_prefix, ws)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {ws}: {exc}")
            w = None
        if w:
            cache[ws] = w
        if i % 25 == 0:
            print(f"  ...{i}/{len(missing)}")
        time.sleep(0.05)

    if use_cache:
        with open(cache_path, "w") as f:
            json.dump(list(cache.values()), f)

    return [cache[ws] for ws in wanted if ws in cache]


# ---------------------------------------------------------------------------
#  Simulation
# ---------------------------------------------------------------------------
def price_at(hist, target_ts) -> Optional[float]:
    """Most recent price at or before target_ts (no look-ahead)."""
    best = None
    for t, p in hist:
        if t <= target_ts and (best is None or t > best[0]):
            best = (t, p)
    if best is None and hist:
        best = min(hist, key=lambda tp: tp[0])
    return best[1] if best else None


def simulate(windows, *, entry_seconds, price_min, price_max, trade_size, slippage):
    rows = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    wins = losses = trades = 0
    pnl_sum = cost_sum = 0.0
    entry_px_sum = 0.0

    for w in windows:
        target = w["ws"] + WINDOW - entry_seconds
        up = price_at(w["up"], target)
        dn = price_at(w["down"], target)
        # model the price actually paid (the ask ≈ mid + slippage)
        up_ask = None if up is None else round(up + slippage, 4)
        dn_ask = None if dn is None else round(dn + slippage, 4)

        side = entry = token_won = None
        if up_ask is not None and price_min <= up_ask <= price_max:
            side, entry = "Up", up_ask
        elif dn_ask is not None and price_min <= dn_ask <= price_max:
            side, entry = "Down", dn_ask

        if side is None:
            rows.append({"ws": w["ws"], "side": "", "entry": "", "winner": w["winner"],
                         "result": "no-trade", "pnl": 0.0})
            continue

        trades += 1
        shares = trade_size / entry
        won = side == w["winner"]
        payout = shares if won else 0.0
        pnl = round(payout - trade_size, 4)
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        wins += won
        losses += not won
        pnl_sum += pnl
        cost_sum += trade_size
        entry_px_sum += entry
        rows.append({"ws": w["ws"], "side": side, "entry": round(entry, 3),
                     "winner": w["winner"], "result": "WON" if won else "LOST",
                     "pnl": pnl})

    avg_entry = entry_px_sum / trades if trades else 0.0
    return {
        "scanned": len(windows), "trades": trades, "wins": wins, "losses": losses,
        "winrate": (wins / trades) if trades else 0.0,
        "pnl": pnl_sum, "cost": cost_sum,
        "roi": (pnl_sum / cost_sum) if cost_sum else 0.0,
        "avg_pnl": (pnl_sum / trades) if trades else 0.0,
        "avg_entry": avg_entry, "max_dd": max_dd,
        "breakeven_winrate": avg_entry,  # need to win >= entry price to break even
    }, rows


def print_summary(p, s):
    print("=" * 64)
    print(f"Windows scanned : {s['scanned']}")
    print(f"Trades taken    : {s['trades']}  (no-trade: {s['scanned'] - s['trades']})")
    print(f"Wins / Losses   : {s['wins']} / {s['losses']}")
    print(f"Win rate        : {s['winrate']*100:.1f}%")
    print(f"Avg entry price : {s['avg_entry']:.3f}  "
          f"(break-even win rate ≈ {s['breakeven_winrate']*100:.1f}%)")
    print(f"Net P&L         : ${s['pnl']:+.2f}  on ${s['cost']:.0f} deployed")
    print(f"ROI             : {s['roi']*100:+.2f}%   (avg ${s['avg_pnl']:+.3f}/trade)")
    print(f"Max drawdown    : ${s['max_dd']:.2f}")
    edge = s['winrate'] - s['breakeven_winrate']
    print(f"Edge vs market  : {edge*100:+.1f} pts "
          f"({'positive — favourites beat their price' if edge > 0 else 'negative — no edge'})")
    print("=" * 64)


def run_sweep(windows, args):
    print("\nParameter sweep (price_min fixed at "
          f"{args.price_min}, entry {args.entry_seconds}s, slippage {args.slippage}):\n")
    print(f"{'price_max':>9} {'trades':>7} {'winrate':>8} {'net P&L':>9} "
          f"{'ROI':>8} {'avg/trade':>10} {'maxDD':>7}")
    for pmax in (0.88, 0.90, 0.92, 0.95, 0.97, 0.99):
        if pmax < args.price_min:
            continue
        s, _ = simulate(windows, entry_seconds=args.entry_seconds,
                        price_min=args.price_min, price_max=pmax,
                        trade_size=args.trade_size, slippage=args.slippage)
        print(f"{pmax:>9.2f} {s['trades']:>7} {s['winrate']*100:>7.1f}% "
              f"${s['pnl']:>+8.2f} {s['roi']*100:>+7.2f}% "
              f"${s['avg_pnl']:>+9.3f} ${s['max_dd']:>6.2f}")
    print()


def main():
    p = argparse.ArgumentParser(description="Backtest BTC Up/Down 5m strategy")
    p.add_argument("--windows", type=int, default=200)
    p.add_argument("--entry-seconds", type=float, default=70)
    p.add_argument("--price-min", type=float, default=0.84)
    p.add_argument("--price-max", type=float, default=0.99)
    p.add_argument("--trade-size", type=float, default=5.0)
    p.add_argument("--slippage", type=float, default=0.0,
                   help="added to history price to model paying the ask (e.g. 0.01)")
    p.add_argument("--asset", default="btc")
    p.add_argument("--duration", default="5m")
    p.add_argument("--cache", default="backtest_cache.json")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--csv", default="backtest_results.csv")
    p.add_argument("--sweep", action="store_true")
    args = p.parse_args()

    slug_prefix = f"{args.asset}-updown-{args.duration}-"
    windows = collect(slug_prefix, args.windows, args.cache, not args.no_cache)
    if not windows:
        print("No window data collected (markets may have aged out, or no network).")
        return 1
    print(f"Collected {len(windows)} resolved windows.\n")

    summary, rows = simulate(
        windows, entry_seconds=args.entry_seconds,
        price_min=args.price_min, price_max=args.price_max,
        trade_size=args.trade_size, slippage=args.slippage,
    )
    print(f"Strategy: buy favourite in [{args.price_min}, {args.price_max}] "
          f"at ~t-{int(args.entry_seconds)}s, ${args.trade_size}/trade")
    print_summary(args, summary)

    with open(args.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ws", "side", "entry", "winner", "result", "pnl"])
        w.writeheader()
        w.writerows(rows)
    print(f"Per-window results written to {args.csv}")

    if args.sweep:
        run_sweep(windows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
