#!/usr/bin/env python3
"""Одна команда, чтобы понять, что происходит: `python status.py`.

Заменяет полдюжины однострочников с кавычками, в которых легко ошибиться.
Показывает три вещи, и все три нужны каждый день:

  ЗАПИСЬ  — идёт ли она вообще и на сколько часов уже набрала;
  ФИДЫ    — считаем ли мы цену сами или просто повторяем за Polymarket.
            Если «наша цена == pm» почти везде, семь бирж не подключились,
            опережения нет, и весь смысл конструкции пропал;
  СДЕЛКИ  — сколько, чем закончились, сколько денег.

Ничего не меняет и не пишет — только читает.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics as st
from collections import Counter
from typing import List, Optional

# Что означают коды в колонке result — расшифровываем, чтобы не держать в уме.
RESULTS = {
    "SOLD_TP": "фиксация прибыли (трейлинг/стоп)",
    
    "SOLD_EXIT": "закрыта по выходу",
    "WON": "досидели до расчёта, выиграли",
    "LOST": "досидели до расчёта, проиграли",
    "UNSETTLED": "раунд не определился, по последней цене",
}


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def read_records(path: str, last: int) -> tuple[List[dict], int, int]:
    """Возвращает (последние N тиков, всего строк, число раундов)."""
    rows, total, settles = [], 0, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "settle":
                settles += 1
                continue
            rows.append(r)
            if len(rows) > last:
                rows.pop(0)
    return rows, total, settles


def show_recording(path: str, last: int) -> None:
    print("\nЗАПИСЬ РЫНКА")
    print("  " + "-" * 58)
    if not os.path.exists(path):
        print(f"  файла {path} нет — бот запущен без --record")
        return

    size = os.path.getsize(path)
    rows, total, settles = read_records(path, last)
    if not rows:
        print(f"  {path}: {human(size)}, но пригодных тиков нет")
        return

    ts = [r["t"] for r in rows if r.get("t")]
    print(f"  файл             : {path}, {human(size)}")
    print(f"  строк            : {total:,}   раундов закрыто: {settles}")
    if len(ts) > 1:
        rate = len(ts) / max(ts[-1] - ts[0], 1e-9)
        print(f"  тиков в секунду  : {rate:.1f}")
        # Сутки записи при такой частоте — прикидка, хватит ли диска.
        day = rate * 86400 * (size / max(total, 1))
        print(f"  прогноз за сутки : {human(day)}")

    show_feeds(rows)


def show_feeds(rows: List[dict]) -> None:
    """Главная проверка: считаем ли мы цену сами."""
    print("\nФИДЫ  (по последним тикам)")
    print("  " + "-" * 58)
    ok = [r for r in rows if r.get("pm") and r.get("price")]
    if not ok:
        print("  цены якоря Polymarket в записи нет")
        return

    lags = [abs(r["price"] - r["pm"]) for r in ok]
    same = sum(1 for v in lags if v < 0.005)
    share = same / len(ok)
    print(f"  наша цена == pm  : {same} из {len(ok)}  ({share*100:.1f}%)")
    print(f"  |лаг| медиана    : ${st.median(lags):.3f}   макс ${max(lags):.2f}")

    sig = [r["sigma"] for r in rows if r.get("sigma")]
    if sig:
        print(f"  sigma медиана    : {st.median(sig):.2f} $/√с")

    ages = [r["pm_age"] for r in rows if r.get("pm_age") is not None]
    if ages:
        print(f"  возраст якоря    : медиана {st.median(ages):.0f} мс")

    print()
    if share > 0.9:
        print("  ⚠ ПЛОХО: наша цена почти всегда совпадает с Polymarket.")
        print("    Значит семь бирж не подключились и опережения нет —")
        print("    вся идея бота держится на этом разрыве.")
    elif st.median(lags) < 0.05:
        print("  ⚠ лаг подозрительно мал — проверь, все ли фиды живы")
    else:
        print("  ✓ консенсус считается по биржам, опережение есть")


def show_trades(path: str) -> None:
    print("\nСДЕЛКИ")
    print("  " + "-" * 58)
    if not os.path.exists(path):
        print(f"  файла {path} нет — сделок ещё не было")
        return

    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("  сделок ещё не было")
        return

    print(f"  всего            : {len(rows)}")
    for code, n in Counter(r.get("result", "") for r in rows).most_common():
        print(f"    {code:12} {n:4}   {RESULTS.get(code, '')}")

    pnls = [_f(r.get("pnl")) for r in rows]
    pnls = [v for v in pnls if v is not None]
    if pnls:
        wins = sum(1 for v in pnls if v > 0)
        print(f"\n  в плюс           : {wins} из {len(pnls)} "
              f"({wins/len(pnls)*100:.0f}%)")
        print(f"  суммарный P&L    : ${sum(pnls):+.2f}")
        print(f"  лучшая / худшая  : ${max(pnls):+.2f} / ${min(pnls):+.2f}")

    bal = _f(rows[-1].get("balance_after"))
    if bal is not None:
        print(f"  баланс сейчас    : ${bal:.2f}")

    print("\n  Напоминание: это симуляция, пока DRY_RUN=true.")
    print("  Сотня сделок ещё ничего не доказывает — нужен разбор записи")
    print("  через scan.py / features.py / replay.py.")


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Состояние бота: запись, фиды, сделки")
    ap.add_argument("--record", default="market.jsonl")
    ap.add_argument("--trades", default="jump_trades.csv")
    ap.add_argument("--last", type=int, default=2000,
                    help="сколько последних тиков смотреть (деф. 2000)")
    args = ap.parse_args(argv)

    show_recording(args.record, args.last)
    show_trades(args.trades)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
