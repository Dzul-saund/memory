#!/usr/bin/env python3
"""Разбор журнала сделок: что реально влияет на прибыль.

Журнал (`~/.flowbot/trades.jsonl`) пишется на каждой закрытой ноге и несёт
не только исход, но и ВСЕ признаки сигнала: Q, скорость, ход в σ, удержание,
ускорение, запас, спред, структуру книги. Здесь по нему отвечают на
единственный вопрос, ради которого он и собирается: какие фильтры делают
деньги, а какие только кажутся полезными.

    python stats.py                      # сводка
    python stats.py --mode LIVE          # только реальные деньги
    python stats.py --split q            # P&L по квартилям качества
    python stats.py --split speed_sigmas
    python stats.py --csv out.csv        # выгрузить в таблицу
    python stats.py --file другой.jsonl

ЧТО ЗДЕСЬ НЕ ДЕЛАЕТСЯ. Никакого автоподбора порогов по этой же выборке:
подогнать пороги под сотню сделок и обрадоваться — ровно та ошибка, на
которой проект уже обжигался дважды. Разбиение на половины (`--split`
печатает обе) существует затем, чтобы признак, не переживший вторую
половину, был виден сразу.
"""
from __future__ import annotations

import argparse
import csv
import sys
from typing import List, Optional

from flowbot.stats import default_path, read_all

# Признаки, по которым осмысленно резать выборку.
FEATURES = ["q", "speed_sigmas", "jump_sigmas", "hold", "accel",
            "edge_cents", "spread_cents", "imp_age_s", "book_imb",
            "book_wall", "secs_left", "sigma"]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # NaN отбрасываем


def _pnl(rows) -> float:
    return sum(_num(r.get("pnl")) or 0.0 for r in rows)


def _line(name: str, rows: List[dict]) -> str:
    n = len(rows)
    if not n:
        return f"  {name:22} —"
    pnl = _pnl(rows)
    wins = sum(1 for r in rows if (_num(r.get("pnl")) or 0.0) > 0)
    return (f"  {name:22} сделок {n:5}  в плюс {wins:5} "
            f"({wins / n * 100:3.0f}%)  P&L ${pnl:+9.2f}  "
            f"средняя ${pnl / n:+6.3f}")


def summary(rows: List[dict]) -> None:
    print(f"\nвсего сделок: {len(rows)}")
    if not rows:
        print("\nЖурнал пуст. Он пишется только на ЗАКРЫТЫХ ногах — если бот "
              "работал недолго\nи ни один раунд не завершился, строк не будет.")
        return

    print("\nпо режиму:")
    for mode in ("DRY", "LIVE"):
        print(_line(mode, [r for r in rows if r.get("mode") == mode]))

    print("\nпо исходу:")
    codes = sorted({str(r.get("result", "?")) for r in rows})
    for code in codes:
        print(_line(code, [r for r in rows if str(r.get("result")) == code]))

    print("\nпо стороне:")
    for side in ("Up", "Down"):
        print(_line(side, [r for r in rows if r.get("outcome") == side]))

    strict = [r for r in rows if r.get("strict_entry")]
    if strict:
        print("\nповторные входы после разворота (строгая планка):")
        print(_line("повторные", strict))
        print(_line("обычные", [r for r in rows if not r.get("strict_entry")]))

    print("\nсамые дорогие ошибки:")
    worst = sorted(rows, key=lambda r: _num(r.get("pnl")) or 0.0)[:5]
    for r in worst:
        q = _num(r.get("q"))
        print(f"  ${_num(r.get('pnl')) or 0:+6.2f}  {r.get('outcome','?'):4} "
              f"@ {r.get('entry_price')}  Q={q if q is None else round(q, 2)}  "
              f"{r.get('result','?'):10} {r.get('ts','')}")


def split(rows: List[dict], feature: str, buckets: int = 4) -> None:
    """P&L по квантилям признака — и то же самое на двух половинах выборки.

    Половины печатаются рядом намеренно. Признак, который «работает» только
    в первой половине, — это подгонка, и увидеть это надо сразу, а не после
    того, как пороги уже поставлены в бой.
    """
    vals = [(v, r) for r in rows if (v := _num(r.get(feature))) is not None]
    if len(vals) < buckets * 2:
        print(f"\n{feature}: данных мало ({len(vals)} сделок с этим "
              f"признаком) — резать нечего")
        return
    vals.sort(key=lambda x: x[0])

    def show(title: str, sub):
        if len(sub) < buckets:
            print(f"  {title}: мало данных")
            return
        print(f"  {title}:")
        step = len(sub) / buckets
        for i in range(buckets):
            part = sub[int(i * step):int((i + 1) * step)]
            if not part:
                continue
            lo, hi = part[0][0], part[-1][0]
            print("  " + _line(f"{lo:.2f}..{hi:.2f}", [r for _v, r in part]))

    print(f"\n=== {feature} ===")
    show("вся выборка", vals)
    half = len(rows) // 2
    first = [(v, r) for r in rows[:half]
             if (v := _num(r.get(feature))) is not None]
    second = [(v, r) for r in rows[half:]
              if (v := _num(r.get(feature))) is not None]
    first.sort(key=lambda x: x[0])
    second.sort(key=lambda x: x[0])
    show("первая половина", first)
    show("вторая половина", second)


def export_csv(rows: List[dict], path: str) -> None:
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys and k != "q_parts":
                keys.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"выгружено {len(rows)} строк -> {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="разбор журнала сделок")
    ap.add_argument("--file", help=f"путь к журналу (деф. {default_path()})")
    ap.add_argument("--mode", choices=["DRY", "LIVE"],
                    help="только симуляция или только реальные деньги")
    ap.add_argument("--entry-mode", help="только этот режим входа")
    ap.add_argument("--split", metavar="ПРИЗНАК",
                    help="P&L по квантилям признака: " + ", ".join(FEATURES))
    ap.add_argument("--buckets", type=int, default=4)
    ap.add_argument("--csv", metavar="FILE", help="выгрузить в CSV")
    args = ap.parse_args(argv)

    path = args.file or default_path()
    rows = read_all(path)
    print(f"журнал: {path}")
    if args.mode:
        rows = [r for r in rows if r.get("mode") == args.mode]
    if args.entry_mode:
        rows = [r for r in rows if r.get("entry_mode") == args.entry_mode]

    if args.csv:
        export_csv(rows, args.csv)
        return 0
    if args.split:
        if args.split not in FEATURES:
            print(f"неизвестный признак {args.split!r}; доступно: "
                  + ", ".join(FEATURES), file=sys.stderr)
            return 2
        summary(rows)
        split(rows, args.split, args.buckets)
        print("\nВАЖНО: разница между квантилями на сотне сделок — это шум. "
              "Смотри,\nповторяется ли она во ВТОРОЙ половине; если нет — "
              "это подгонка.")
        return 0

    summary(rows)
    if rows:
        print("\nдальше: python stats.py --split q     (или speed_sigmas, "
              "hold, edge_cents…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
