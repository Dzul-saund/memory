#!/usr/bin/env python3
"""Сколько долларов реально лежит в книге — по ЗАПИСИ рынка, а не на глаз.

ЗАЧЕМ. Вопрос «пройдёт ли ставка $50» — это НЕ вопрос про правила площадки.
Минимум у Polymarket есть ($1), потолка нет. Упереться можно только в одно:
сколько шэров лежит по той цене, по которой мы покупаем. FAK-ордер заберёт
что лежит и остаток отменит — то есть слишком крупная ставка не отвергается,
она исполняется ЧАСТИЧНО и молча.

Поэтому «$50 пройдёт?» = «как часто по лучшей цене лежало больше $50?».
Ответ есть в записи (`--record`), и здесь он считается.

    python depth_check.py market.jsonl
    python depth_check.py market.jsonl --sizes 5 25 50 100

Скрипт понимает ОБА формата записи:
  * сборка jump  — `uas`/`das` (объём на лучшем аске) + `lvl`;
  * сборка certainty — `up_levels`/`down_levels` (полные уровни книги).

ЧТО СЧИТАЕТСЯ ЛУЧШЕЙ ЦЕНОЙ. Только верхний уровень: пройти вглубь книги
можно, но каждая следующая ступень дороже, а вся арифметика этого проекта
завязана на цену входа. Взять $50 по 0.98 и по 0.99 — разные сделки.

ОГРАНИЧЕНИЕ, КОТОРОЕ НАДО ДЕРЖАТЬ В ГОЛОВЕ. Запись показывает книгу ДО
нашего ордера. Она отвечает на «хватило ли бы объёма», но не на «остался ли
бы он на месте, пока летят наши 310 мс». Верхняя оценка, не гарантия.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# Полосы цены. Разделять обязательно: книга около таргета и книга решённого
# раунда — это два разных рынка. В проекте уже замечено, что спред 5¢ около
# таргета и 1¢ на решённом раунде, и глубина расходится так же.
BANDS: Sequence[Tuple[str, float, float]] = (
    ("около таргета 0.40-0.60", 0.40, 0.60),
    ("склон       0.60-0.90", 0.60, 0.90),
    ("почти-факт  0.90-0.96", 0.90, 0.96),
    ("вход cert   0.96-0.985", 0.96, 0.9851),
)


def _levels_usd(levels, limit: Optional[float]) -> Optional[float]:
    """Доллары на ЛУЧШЕЙ цене списка уровней `[[цена, размер], ...]`."""
    if not levels:
        return None
    best = min(levels, key=lambda lv: lv[0])
    price, size = float(best[0]), float(best[1])
    if limit is not None and price > limit + 1e-9:
        return None
    return price * size


def _tick_asks(rec: dict) -> List[Tuple[str, float, float]]:
    """[(сторона, цена аска, доллары на нём)] — из любого формата записи."""
    out: List[Tuple[str, float, float]] = []
    for side, ask_key, size_key, lv_key in (
            ("Up", "ua", "uas", "up_levels"),
            ("Down", "da", "das", "down_levels")):
        ask = rec.get(ask_key)
        if ask is None:
            continue
        ask = float(ask)
        usd: Optional[float] = None
        lv = rec.get(lv_key)
        if isinstance(lv, list) and len(lv) == 2:
            # формат certainty: [биды, аски]
            usd = _levels_usd(lv[1], ask)
        if usd is None:
            size = rec.get(size_key)
            # формат jump: объём ровно на лучшем аске
            if size is not None:
                usd = ask * float(size)
        if usd is not None:
            out.append((side, ask, usd))
    return out


def _pct(vals: List[float], q: float) -> float:
    if not vals:
        return float("nan")
    k = (len(vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


class Bucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.usd: List[float] = []

    def add(self, usd: float) -> None:
        self.usd.append(usd)

    def report(self, sizes: Sequence[float]) -> str:
        if not self.usd:
            return f"{self.name:24s}  нет тиков"
        v = sorted(self.usd)
        n = len(v)
        head = (f"{self.name:24s}  тиков {n:7d}  "
                f"медиана ${_pct(v, .5):8.0f}  "
                f"10% ${_pct(v, .10):7.0f}  "
                f"1% ${_pct(v, .01):6.0f}")
        fits = []
        for s in sizes:
            ok = sum(1 for x in v if x >= s) / n * 100.0
            fits.append(f"${s:g}:{ok:5.1f}%")
        return head + "\n" + " " * 26 + "влезает целиком — " + "  ".join(fits)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="запись рынка (JSONL от --record)")
    ap.add_argument("--sizes", nargs="+", type=float,
                    default=[5, 10, 25, 50, 100],
                    help="размеры ставки в долларах для проверки")
    ap.add_argument("--last", type=float, default=None,
                    help="только тики, где до расчёта осталось <= столько секунд")
    a = ap.parse_args(argv)

    overall = Bucket("ВСЕ тики")
    bands: Dict[str, Bucket] = {b[0]: Bucket(b[0]) for b in BANDS}
    lines = bad = skipped = 0
    have_levels = have_topsize = 0

    with open(a.path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            lines += 1
            try:
                rec = json.loads(raw)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            if rec.get("type") == "settle":
                continue
            if a.last is not None:
                left = rec.get("left")
                if left is None or float(left) > a.last:
                    continue
            if isinstance(rec.get("up_levels"), list):
                have_levels += 1
            elif rec.get("uas") is not None:
                have_topsize += 1
            asks = _tick_asks(rec)
            if not asks:
                skipped += 1
                continue
            for _side, price, usd in asks:
                overall.add(usd)
                for name, lo, hi in BANDS:
                    if lo <= price < hi:
                        bands[name].add(usd)
                        break

    print(f"файл: {a.path}")
    print(f"строк {lines}, битых {bad}, без книги {skipped}")
    src = ("полные уровни книги" if have_levels >= have_topsize
           else "объём на лучшей цене")
    print(f"источник глубины: {src}"
          f"  (уровни {have_levels}, топ {have_topsize})")
    if a.last is not None:
        print(f"фильтр: только последние {a.last:g}с раунда")
    print()
    print("ДОЛЛАРЫ НА ЛУЧШЕМ АСКЕ (обе стороны считаются отдельными тиками)")
    print("-" * 78)
    print(overall.report(a.sizes))
    print("-" * 78)
    for name, _lo, _hi in BANDS:
        print(bands[name].report(a.sizes))
    print("-" * 78)
    print("Проценты — доля тиков, где ставка влезла бы ЦЕЛИКОМ по лучшей цене.")
    print("Остальные исполнились бы ЧАСТИЧНО: FAK берёт что есть, остаток гасит.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
