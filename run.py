#!/usr/bin/env python3
"""Entry point for the Polymarket BTC Up/Down 5m trading bot.

Examples:
    python run.py                       # dry-run, 24h, default strategy
    python run.py --once                # one cycle, print a snapshot, exit
    python run.py --hours 2             # run for 2 hours
    python run.py --live                # REAL trading (needs PRIVATE_KEY in .env)
    python run.py --setup-allowances    # one-time USDC allowance approval
    python run.py --env-file bot1.env   # run a named preset (see bot1/bot2.env)
"""
from __future__ import annotations

import argparse
import logging
import sys

from btc_bot.bot import Bot
from btc_bot.config import Config


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket BTC Up/Down 5m bot")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="place REAL orders (needs credentials)")
    mode.add_argument("--dry-run", action="store_true",
                      help="force simulation mode (default)")
    p.add_argument("--hours", type=float,
                   help="how long to run, in hours (default 24)")
    p.add_argument("--trade-size", type=float,
                   help="USDC to spend per trade")
    p.add_argument("--min-target-distance", type=float,
                   help="only buy when BTC is >= this many USD from the window "
                        "open (target), in the favourite's direction")
    p.add_argument("--env-file",
                   help="load configuration from this .env file (overrides .env); "
                        "use to run named presets like bot1.env / bot2.env")
    p.add_argument("--price-source", choices=["ask", "mid", "last"],
                   help="which price drives the decision (default ask)")
    p.add_argument("--once", action="store_true",
                   help="run a single cycle and exit (verify setup)")
    p.add_argument("--setup-allowances", action="store_true",
                   help="set the USDC trading allowance, then exit")
    p.add_argument("--log-level", default="INFO",
                   help="DEBUG, INFO, WARNING, ... (default INFO)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # An explicit --env-file wins over the auto-loaded .env (and the defaults).
    if args.env_file:
        try:
            from dotenv import load_dotenv
        except Exception:  # pragma: no cover - dotenv is optional
            print("--env-file needs python-dotenv (pip install python-dotenv)",
                  file=sys.stderr)
            return 2
        if not load_dotenv(args.env_file, override=True):
            print(f"env file not found: {args.env_file}", file=sys.stderr)
            return 2
        logging.getLogger("btc-bot").info("Loaded config preset: %s", args.env_file)

    cfg = Config.from_env()
    if args.hours is not None:
        cfg.run_duration_seconds = args.hours * 3600.0
    if args.trade_size is not None:
        cfg.trade_size_usdc = args.trade_size
    if args.min_target_distance is not None:
        cfg.min_target_distance_usdc = args.min_target_distance
    if args.price_source:
        cfg.price_source = args.price_source
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True

    try:
        cfg.validate()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    # Тюнинг сборщика мусора: без редких пауз в несколько мс.
    import gc
    gc.collect()
    try:
        gc.freeze()
    except Exception:
        pass
    gc.set_threshold(50000, 100, 100)

    bot = Bot(cfg)
    try:
        if args.setup_allowances:
            bot.setup_allowances()
        elif args.once:
            bot.tick_once()
        else:
            bot.run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
