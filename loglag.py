#!/usr/bin/env python3
"""Задержка «сигнал -> покупка» из лога бота.

Зачем отдельный инструмент. Ни CSV сделок, ни журнал никогда не писали, через
сколько после решения ордер стал позицией: там есть цена входа, результат и
время удержания, но нет момента, когда сигнал сработал. Зато в логе есть обе
точки с точностью до миллисекунды:

    18:06:05.371 WARNING >>> BUY: <причина>        <- решение принято
    18:06:05.462 WARNING КУПЛЕНО [#0] Up — ...     <- позиция записана

Разница между ними и есть ответ. Считаются также СОРВАВШИЕСЯ сигналы: те, за
которыми не последовало покупки, и причина, по которой не последовало. Это не
менее важно — сигнал, до которого не дошли руки, выглядит в статистике так же,
как сигнал, которого не было.

    python loglag.py dry.log
    python loglag.py live.log --show-misses
    python loglag.py *.log

Понимает и старые логи (там решение печаталось как `>>> ENTER` / `>>> LADDER`).
"""
from __future__ import annotations

import argparse
import glob
import re
import statistics
import sys
from typing import List, Optional, Tuple

# 18:06:05.371 WARNING >>> BUY: ...
TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s")
DECISION = re.compile(r">>>\s+(BUY|ENTER|LADDER|SELL)\s*:\s*(.*)")
FILLED = re.compile(r"КУПЛЕНО\s")
SOLD = re.compile(r"ПРОДАНО\s")
# Почему покупки не случилось. Порядок важен: сначала конкретные причины.
MISSES = [
    (re.compile(r"ОТКЛОНЕНА биржей"), "биржа отклонила ордер"),
    (re.compile(r"НЕ ЗАПИСАНА|не подтверждена"), "ответ не подтвердил филл"),
    (re.compile(r"ask [\d.]+ > лимит"), "книга ушла за пинг"),
    (re.compile(r"нет ask"), "нет ask в книге"),
    (re.compile(r"вышли бы за потолок раунда"), "потолок раунда"),
    (re.compile(r"расчёт дал .* шэров"), "размер вышел нулевым"),
    (re.compile(r"ордер BUY упал|ордер упал"), "запрос к бирже упал"),
    (re.compile(r"нет ответа за"), "таймаут ордера"),
    (re.compile(r"НЕ исполнена|не исполнена|отменена"), "не исполнена"),
]


def seconds(line: str) -> Optional[float]:
    m = TS.match(line)
    if not m:
        return None
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse(path: str) -> Tuple[List[float], List[Tuple[str, str]], int]:
    """-> (задержки покупок в мс, сорвавшиеся сигналы, число продаж)."""
    lags: List[float] = []
    misses: List[Tuple[str, str]] = []
    sells = 0
    pending: Optional[Tuple[float, str, str]] = None   # (t, вид, причина)

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = seconds(line)
            if t is None:
                continue

            m = DECISION.search(line)
            if m:
                # Предыдущее решение так и не дошло до сделки.
                if pending is not None and pending[1] != "SELL":
                    misses.append((pending[2], "следующий сигнал вытеснил"))
                pending = (t, m.group(1), m.group(2).strip())
                continue

            if FILLED.search(line) and pending is not None:
                dt = (t - pending[0]) * 1000.0
                if dt < 0:                      # лог пересёк полночь
                    dt += 24 * 3600 * 1000.0
                lags.append(dt)
                pending = None
                continue

            if SOLD.search(line):
                sells += 1
                if pending is not None and pending[1] == "SELL":
                    pending = None
                continue

            if pending is not None and pending[1] != "SELL":
                for rx, why in MISSES:
                    if rx.search(line):
                        misses.append((pending[2], why))
                        pending = None
                        break

    if pending is not None and pending[1] != "SELL":
        misses.append((pending[2], "лог оборвался"))
    return lags, misses, sells


def pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    i = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[i]


def report(paths: List[str], show_misses: bool) -> int:
    all_lags: List[float] = []
    all_misses: List[Tuple[str, str]] = []
    total_sells = 0

    for path in paths:
        try:
            lags, misses, sells = parse(path)
        except OSError as exc:
            print(f"не читается {path}: {exc}", file=sys.stderr)
            continue
        all_lags += lags
        all_misses += misses
        total_sells += sells
        print(f"{path}: покупок {len(lags)}, сорвалось {len(misses)}, "
              f"продаж {sells}")

    n = len(all_lags)
    print(f"\n{'=' * 62}")
    if not n:
        print("Покупок в логе нет — измерять нечего.\n")
        print("Так бывает по трём причинам: бот не торговал, лог не "
              "сохранялся\n(запускали без `| tee`), или все сигналы "
              "срывались — тогда смотри\nсписок ниже.")
    else:
        print(f"ЗАДЕРЖКА «СИГНАЛ -> ПОКУПКА», {n} сделок\n")
        print(f"  медиана      {statistics.median(all_lags):8.1f} мс")
        print(f"  среднее      {statistics.fmean(all_lags):8.1f} мс")
        print(f"  минимум      {min(all_lags):8.1f} мс")
        print(f"  90-й проц.   {pct(all_lags, 0.90):8.1f} мс")
        print(f"  максимум     {max(all_lags):8.1f} мс")
        if n > 1:
            print(f"  разброс      {statistics.pstdev(all_lags):8.1f} мс")

        print("\n  распределение:")
        edges = [(0, 50), (50, 100), (100, 200), (200, 500),
                 (500, 1000), (1000, float("inf"))]
        for lo, hi in edges:
            k = sum(1 for x in all_lags if lo <= x < hi)
            if not k:
                continue
            name = f"{lo:.0f}-{hi:.0f}мс" if hi != float("inf") else f">{lo:.0f}мс"
            bar = "█" * max(1, round(40 * k / n))
            print(f"    {name:>12}  {k:4} ({k / n * 100:4.1f}%)  {bar}")

    if all_misses:
        print(f"\n{'=' * 62}")
        print(f"СИГНАЛЫ БЕЗ ПОКУПКИ: {len(all_misses)}")
        tot = len(all_misses) + n
        if tot:
            print(f"  доля сорвавшихся: {len(all_misses) / tot * 100:.1f}% "
                  f"от всех сигналов\n")
        from collections import Counter
        for why, k in Counter(w for _s, w in all_misses).most_common():
            print(f"    {k:4}  {why}")
        if show_misses:
            print("\n  первые десять:")
            for sig, why in all_misses[:10]:
                print(f"    [{why}] {sig[:90]}")

    if n:
        print(f"\n{'=' * 62}")
        print("КАК ЧИТАТЬ")
        med = statistics.median(all_lags)
        print(f"  Собственная работа бота — доли миллисекунды. Значит почти "
              f"все\n  {med:.0f} мс это дорога до биржи и обратно.")
        print("  За это время книга живёт своей жизнью: цена, по которой "
              "принято\n  решение, и цена, по которой исполнился ордер, — "
              "разные цены.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="задержка «сигнал -> покупка» из лога бота")
    ap.add_argument("logs", nargs="+", help="файлы логов (можно маской)")
    ap.add_argument("--show-misses", action="store_true",
                    help="показать сорвавшиеся сигналы построчно")
    args = ap.parse_args(argv)

    paths: List[str] = []
    for pat in args.logs:
        found = sorted(glob.glob(pat))
        paths += found or [pat]
    return report(paths, args.show_misses)


if __name__ == "__main__":
    raise SystemExit(main())
