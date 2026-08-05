#!/usr/bin/env python3
"""Точка входа: запуск бота на 5-минутном рынке Up/Down.

    python trader.py                       # dry-run, BTC
    python trader.py --coin eth --stake 2
    python trader.py --live                # РЕАЛЬНЫЕ ордера (нужен ключ)
    python trader.py --env-file flow.env
    python trader.py --record market.jsonl # писать рынок для проигрывания

Флаги перекрывают --env-file / .env, которые перекрывают значения по
умолчанию. Торговых порогов среди флагов нет: стратегия — чистый лист
(`flowbot/strategy.py`), и её будущие настройки появятся здесь вместе с ней.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import FlowConfig
from .singleton import InstanceLock
from .trading import TradingEngine


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="бот на 5-минутных рынках Polymarket Up/Down")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="реальные ордера (нужны креды в .env)")
    mode.add_argument("--dry-run", action="store_true",
                      help="симуляция (по умолчанию)")
    p.add_argument("--coin", "--asset", dest="coin",
                   choices=["btc", "eth", "sol", "xrp", "doge"],
                   help="монета (по умолчанию btc)")
    p.add_argument("--stake", type=float,
                   help="размер входа по умолчанию, USDC (деф. 1)")
    p.add_argument("--max-round", type=float,
                   help="потолок вложений в один раунд, USDC (деф. 25)")
    p.add_argument("--ping-ms", type=float,
                   help="задать RTT вручную, мс (иначе меряем)")
    p.add_argument("--hours", type=float,
                   help="сколько работать, часов (деф. 24)")
    p.add_argument("--record", metavar="FILE",
                   help="писать всё, что видит бот, в JSONL — потом прогнать "
                        "через replay.py на реальной истории")
    p.add_argument("--env-file", help="загрузить пресет .env (перекрывает .env)")
    p.add_argument("--allow-multiple", action="store_true",
                   help="разрешить вторую копию на той же монете (по "
                        "умолчанию запрещено: риск складывается)")
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("flowbot")

    if args.env_file:
        try:
            from dotenv import load_dotenv
        except Exception:  # pragma: no cover - dotenv опционален
            print("--env-file требует python-dotenv "
                  "(pip install python-dotenv)", file=sys.stderr)
            return 2
        if not load_dotenv(args.env_file, override=True):
            print(f"файл не найден: {args.env_file}", file=sys.stderr)
            return 2
        log.info("пресет загружен: %s", args.env_file)

    cfg = FlowConfig.from_env()
    if args.coin:
        cfg.asset = args.coin
    if args.stake is not None:
        cfg.stake_usdc = args.stake
    if args.max_round is not None:
        cfg.max_round_usdc = args.max_round
    if args.record:
        cfg.record_path = args.record
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

    # Вторая копия на той же монете и в том же режиме вела бы свой учёт и
    # свои позиции: риск сложился бы, а потолок раунда незаметно удвоился.
    mode = "live" if not cfg.dry_run else "dry"
    lock = InstanceLock(f"flowbot-{cfg.asset}-{mode}")
    if not lock.acquire() and not args.allow_multiple:
        log.error(
            "Бот %s (%s) уже запущен (PID %s). Вторая копия вела бы свой "
            "учёт — риск сложился бы, а потолок $%.0f за раунд превратился "
            "бы в $%.0f. Закрой ту копию или запусти с --allow-multiple, "
            "если это осознанно.",
            cfg.asset.upper(), mode, lock.holder_pid,
            cfg.max_round_usdc, cfg.max_round_usdc * 2)
        return 3

    log.info("сделки -> %s | запись -> %s",
             cfg.trade_log_csv or "(выкл)", cfg.record_path or "(выкл)")

    try:
        TradingEngine(cfg).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
