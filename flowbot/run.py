#!/usr/bin/env python3
"""Точка входа flowbot — единый трейдер (цена + книга) на 5-мин рынке BTC.

Примеры:
    python -m flowbot.run                      # dry-run, BTC, ставка $1
    python -m flowbot.run --coin btc --stake 1
    python -m flowbot.run --ping-ms 90         # задать пинг вручную (Цюрих->PM)
    python -m flowbot.run --live               # РЕАЛЬНЫЕ ордера (нужен PRIVATE_KEY)
    python -m flowbot.run --env-file flow_btc.env

Флаги перекрывают --env-file / .env, которые перекрывают дефолты.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import FlowConfig
from .engine import FlowEngine


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="flowbot — цена + книга, 5-мин BTC")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="реальные ордера (нужны креды в .env)")
    mode.add_argument("--dry-run", action="store_true",
                      help="симуляция (по умолчанию)")
    p.add_argument("--coin", "--asset", dest="coin",
                   choices=["btc", "eth", "sol", "xrp", "doge"],
                   help="монета (по умолчанию btc)")
    p.add_argument("--stake", type=float, help="ставка на вход, USDC (деф. 1)")
    p.add_argument("--flip-size", type=float,
                   help="размер разворотной ставки, USDC (деф. 3.5)")
    p.add_argument("--entry-z", type=float,
                   help="порог всплеска для входа в σ (деф. 2.5)")
    p.add_argument("--ping-ms", type=float,
                   help="задать RTT вручную, мс (иначе меряем; ориентир 90)")
    p.add_argument("--hours", type=float, help="сколько работать, часов (деф. 24)")
    p.add_argument("--env-file", help="загрузить пресет .env (перекрывает .env)")
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    if args.env_file:
        try:
            from dotenv import load_dotenv
        except Exception:  # pragma: no cover
            print("--env-file требует python-dotenv (pip install python-dotenv)",
                  file=sys.stderr)
            return 2
        if not load_dotenv(args.env_file, override=True):
            print(f"файл не найден: {args.env_file}", file=sys.stderr)
            return 2
        logging.getLogger("flowbot").info("пресет загружен: %s", args.env_file)

    cfg = FlowConfig.from_env()
    if args.coin:
        cfg.asset = args.coin
    if args.stake is not None:
        cfg.stake_usdc = args.stake
    if args.flip_size is not None:
        cfg.flip_size_usdc = args.flip_size
    if args.entry_z is not None:
        cfg.entry_burst_z = args.entry_z
    if args.ping_ms is not None:
        cfg.ping_ms = args.ping_ms
    if args.hours is not None:
        cfg.run_duration_seconds = args.hours * 3600.0
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True

    try:
        cfg.validate()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    FlowEngine(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
