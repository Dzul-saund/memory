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
import os
import sys

from .config import JUMP_ENTRY_MODES, FlowConfig
from .jump_engine import JumpEngine
from .singleton import InstanceLock


def tag_path(path: str, tag: str) -> str:
    """`market.jsonl` + `edge` -> `market_edge.jsonl`; пустая строка как есть."""
    if not path:
        return path
    root, ext = os.path.splitext(path)
    return f"{root}_{tag}{ext}"


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
    p.add_argument("--entry-mode", choices=list(JUMP_ENTRY_MODES),
                   help="ЧТО считать поводом войти: jump — скачок цены "
                        "(деф.); edge — запас Phi(z)−ask; lag — отставание "
                        "якоря Polymarket; impulse — КАЧЕСТВО импульса "
                        "(скорость+ускорение+удержание, ставка от качества)")
    p.add_argument("--trigger", choices=["swing", "window"],
                   help="swing (деф.) — ловим движение от локального дна/пика "
                        "сразу; window — сравниваем с ценой N секунд назад")
    p.add_argument("--swing-lookback", type=float,
                   help="как далеко назад искать экстремум, сек (деф. 60)")
    p.add_argument("--jump-window", type=float,
                   help="окно режима window, сек (деф. 3)")
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
    p.add_argument("--record", metavar="FILE",
                   help="писать всё, что видит бот, в JSONL — потом прогнать "
                        "через replay.py и подобрать пороги на реальной истории")
    p.add_argument("--env-file", help="загрузить пресет .env (перекрывает .env)")
    p.add_argument("--allow-multiple", action="store_true",
                   help="разрешить вторую копию на той же монете (по умолчанию "
                        "запрещено: каждая копия ведёт свою лестницу, и риск "
                        "складывается)")
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
    if args.entry_mode:
        cfg.jump_entry_mode = args.entry_mode
    if args.stake is not None:
        cfg.jump_stake_usdc = args.stake
    if args.small is not None:
        cfg.jump_small_usd = args.small
    if args.big is not None:
        cfg.jump_big_usd = args.big
    if args.split is not None:
        cfg.jump_price_split = args.split
    if args.trigger:
        cfg.jump_trigger_mode = args.trigger
    if args.swing_lookback is not None:
        cfg.jump_swing_lookback_s = args.swing_lookback
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

    # Режимы входа — это ПАРАЛЛЕЛЬНЫЕ эксперименты: их запускают одновременно,
    # в том числе рядом с уже работающей копией. Разводим всё, что копии могли
    # бы затоптать друг у друга. Режим jump сохраняет прежние имена файлов и
    # прежний замок — иначе уже запущенный бот потерял бы свою историю, а
    # вторая его копия смогла бы стартовать рядом с ним.
    entry = cfg.jump_entry_mode
    if entry != "jump":
        cfg.trade_log_csv = tag_path(cfg.trade_log_csv, entry)
        cfg.record_path = tag_path(cfg.record_path, entry)

    try:
        cfg.validate_jump()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    # Вторая копия НА ТОМ ЖЕ РЕЖИМЕ вела бы вторую независимую лестницу: те же
    # настройки, но удвоенный реальный риск и перемешанный CSV. Не даём.
    # Разные режимы входа — разные замки: они и должны работать бок о бок.
    log = logging.getLogger("jumpbot")
    mode = "live" if not cfg.dry_run else "dry"
    suffix = "" if entry == "jump" else f"-{entry}"
    lock = InstanceLock(f"jumpbot-{cfg.asset}-{mode}{suffix}")
    if not lock.acquire() and not args.allow_multiple:
        log.error(
            "Система %s (%s, вход по «%s») уже запущена (PID %s). Вторая "
            "копия вела бы свою лестницу — риск сложился бы, а потолок "
            "$%.0f за раунд превратился бы в $%.0f. Закрой ту копию, или "
            "запусти ДРУГОЙ режим входа (--entry-mode), или --allow-multiple, "
            "если это осознанно.",
            cfg.asset.upper(), mode, entry, lock.holder_pid,
            cfg.jump_max_round_usdc, cfg.jump_max_round_usdc * 2)
        return 3

    log.info("режим входа: %s | сделки -> %s | запись -> %s", entry,
             cfg.trade_log_csv or "(выкл)", cfg.record_path or "(выкл)")

    try:
        JumpEngine(cfg).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
