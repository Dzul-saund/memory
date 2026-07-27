#!/usr/bin/env python3
"""Точка входа 4-й системы — скачковая лестница на 5-мин рынке Up/Down.

Примеры:
    python -m flowbot.jump_run                  # dry-run, BTC, ставка $1
    python -m flowbot.jump_run --stake 2
    python -m flowbot.jump_run --small 5 --big 15 --split 0.51
    python -m flowbot.jump_run --live           # РЕАЛЬНЫЕ ордера (нужен ключ)
    python -m flowbot.jump_run --env-file flow_jump.env

Флаги перекрывают --env-file / .env, которые перекрывают дефолты.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import FlowConfig
from .jump_engine import JumpEngine


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Скачковая система: вход по скачку цены в долларах, "
                    "лестница добора, фиксация прибыли на дешёвой стороне")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="реальные ордера (нужны креды в .env)")
    mode.add_argument("--dry-run", action="store_true",
                      help="симуляция (по умолчанию)")
    p.add_argument("--coin", "--asset", dest="coin",
                   choices=["btc", "eth", "sol", "xrp", "doge"],
                   help="монета (по умолчанию btc)")
    p.add_argument("--stake", type=float,
                   help="первая ставка лестницы, USDC (деф. 1)")
    p.add_argument("--small", type=float,
                   help="скачок для дорогой стороны, $ (деф. 5)")
    p.add_argument("--big", type=float,
                   help="скачок для дешёвой стороны, $ (деф. 15)")
    p.add_argument("--split", type=float,
                   help="граница дорого/дёшево по проценту (деф. 0.51)")
    p.add_argument("--jump-window", type=float,
                   help="за сколько секунд меряем скачок (деф. 3)")
    p.add_argument("--max-target-dist", type=float,
                   help="дешёвая сторона: макс. расстояние до таргета, $ (деф. 100)")
    p.add_argument("--max-round", type=float,
                   help="потолок вложений в один раунд, USDC (деф. 25)")
    p.add_argument("--max-legs", type=int,
                   help="сколько доборов максимум за раунд (деф. 4)")
    p.add_argument("--no-ladder", action="store_true",
                   help="выключить добор (только вход и расчёт)")
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
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.env_file:
        try:
            from dotenv import load_dotenv
        except Exception:  # pragma: no cover - dotenv опционален
            print("--env-file требует python-dotenv (pip install python-dotenv)",
                  file=sys.stderr)
            return 2
        if not load_dotenv(args.env_file, override=True):
            print(f"файл не найден: {args.env_file}", file=sys.stderr)
            return 2
        logging.getLogger("jumpbot").info("пресет загружен: %s", args.env_file)

    cfg = FlowConfig.from_env()
    if args.coin:
        cfg.asset = args.coin
    if args.stake is not None:
        cfg.jump_stake_usdc = args.stake
    if args.small is not None:
        cfg.jump_small_usd = args.small
    if args.big is not None:
        cfg.jump_big_usd = args.big
    if args.split is not None:
        cfg.jump_price_split = args.split
    if args.jump_window is not None:
        cfg.jump_window_s = args.jump_window
    if args.max_target_dist is not None:
        cfg.jump_max_target_dist_usd = args.max_target_dist
    if args.max_round is not None:
        cfg.jump_max_round_usdc = args.max_round
    if args.max_legs is not None:
        cfg.jump_max_ladder_legs = args.max_legs
    if args.no_ladder:
        cfg.jump_ladder_enabled = False
    if args.ping_ms is not None:
        cfg.ping_ms = args.ping_ms
    if args.hours is not None:
        cfg.run_duration_seconds = args.hours * 3600.0
    if args.live:
        cfg.dry_run = False
    if args.dry_run:
        cfg.dry_run = True

    try:
        cfg.validate_jump()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    JumpEngine(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
