#!/usr/bin/env python3
"""Проверка признаков на записи рынка: какой из них реально предсказывает.

Зачем отдельный инструмент. Признаков можно придумать десятки, и каждый
выглядит убедительно на словах. Но пока не измерено, предсказывает ли он
хоть что-нибудь, это домыслы. Здесь каждый признак проверяется одинаково и
честно, на одних и тех же данных.

ЧТО ПРЕДСКАЗЫВАЕМ. Не исход раунда, а изменение СЕРЕДИНЫ КНИГИ стороны Up
через `--horizon` секунд. Причина практическая: исходов раунда бывает 12 в
час, а таких наблюдений — тысячи в час. На исходах любая модель с десятком
признаков переобучится раньше, чем наберёт статистику.

ЧЕМ МЕРЯЕМ. Ранговая корреляция Спирмена между признаком и будущим
изменением цены (в трейдинге её зовут information coefficient, IC). Ранговая,
а не обычная: у признаков тяжёлые хвосты, и одна аномалия иначе нарисует
корреляцию там, где её нет.

ОРИЕНТИРЫ ДЛЯ IC. |IC| < 0.02 — шум. 0.02-0.05 — слабо, но при большом числе
сделок может работать. > 0.05 на честных внесэмпловых данных — сильно.

Примеры:
    python features.py market.jsonl
    python features.py market.jsonl --horizon 1.0
    python features.py market.jsonl --split          # проверка на 2-й половине
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from typing import Dict, List, Optional, Tuple


def read(path: str) -> List[dict]:
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") == "settle":
            r["_settle"] = True
        rows.append(r)
    return rows


def mid(r: dict) -> Optional[float]:
    b, a = r.get("ub"), r.get("ua")
    if b is None or a is None:
        return None
    return (b + a) / 2.0


def build(rows: List[dict], horizon: float) -> Tuple[List[Dict], List[float]]:
    """Признаки в момент t и будущее изменение mid(Up) через horizon секунд."""
    ticks = [r for r in rows if not r.get("_settle") and r.get("price")]
    by_round: Dict[str, List[dict]] = {}
    for r in ticks:
        by_round.setdefault(r.get("slug"), []).append(r)

    feats: List[Dict] = []
    fwd: List[float] = []
    for slug, seq in by_round.items():
        seq.sort(key=lambda r: r["t"])
        for i, r in enumerate(seq):
            m0 = mid(r)
            if m0 is None:
                continue
            # будущее — внутри ТОГО ЖЕ раунда, иначе поймали бы сброс к 0/1
            fut = None
            for r2 in seq[i + 1:]:
                if r2["t"] - r["t"] >= horizon:
                    fut = mid(r2)
                    break
            if fut is None:
                continue

            price, tgt, sig = r.get("price"), r.get("target"), r.get("sigma")
            left = max(r.get("left") or 0.0, 0.5)
            f: Dict[str, float] = {}

            # 1. Опережение: наш консенсус против цены, которую видит сам
            #    Polymarket. Это и есть преимущество по скорости.
            if r.get("pm"):
                f["опережение $"] = price - r["pm"]
            # 2. Скорость и ускорение — считаем из ряда цен записи
            prev1 = _back(seq, i, 1.0)
            prev2 = _back(seq, i, 2.0)
            if prev1 is not None:
                f["скорость $/с"] = (price - prev1) / 1.0
                if prev2 is not None:
                    v_prev = (prev1 - prev2) / 1.0
                    f["ускорение"] = (price - prev1) - v_prev
            # 3. Дисбаланс книги по объёму на топе
            ubs, uas = r.get("ubs"), r.get("uas")
            if ubs is not None and uas is not None and (ubs + uas) > 0:
                f["дисбаланс книги"] = (ubs - uas) / (ubs + uas)
            # 4. Агрессия сделок
            tb, ts = r.get("tb"), r.get("tsl")
            if tb is not None and ts is not None and (tb + ts) > 0:
                f["агрессия сделок"] = (tb - ts) / (tb + ts)
            # 5. Поток заявок, который уже считает бот
            if r.get("uf") is not None:
                f["поток (bookengine)"] = r["uf"]
            # 6. Расхождение модели и рынка — главный кандидат
            if price and tgt and sig:
                z = (price - tgt) / (sig * math.sqrt(left))
                fair = 0.5 * (1 + math.erf(z / math.sqrt(2)))
                f["модель − рынок"] = fair - m0
                f["z (в сигмах)"] = z
            # 7. Время
            f["осталось с"] = left
            # 8. Ширина спреда
            if r.get("ua") is not None and r.get("ub") is not None:
                f["спред"] = r["ua"] - r["ub"]

            feats.append(f)
            fwd.append(fut - m0)
    return feats, fwd


def _back(seq: List[dict], i: int, dt: float) -> Optional[float]:
    """Цена примерно dt секунд назад внутри раунда."""
    t0 = seq[i]["t"]
    for j in range(i, -1, -1):
        if t0 - seq[j]["t"] >= dt:
            return seq[j].get("price")
    return None


def spearman(x: List[float], y: List[float]) -> Optional[float]:
    n = len(x)
    if n < 30:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def _ranks(v: List[float]) -> List[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    out = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def report(feats: List[Dict], fwd: List[float], title: str) -> None:
    names = sorted({k for f in feats for k in f})
    print(f"\n{title}  (наблюдений {len(fwd)})")
    print(f"  {'признак':<22} {'n':>6} {'IC':>8}  оценка")
    print("  " + "-" * 58)
    rows = []
    for nm in names:
        xs, ys = [], []
        for f, y in zip(feats, fwd):
            if nm in f:
                xs.append(f[nm])
                ys.append(y)
        ic = spearman(xs, ys)
        if ic is not None:
            rows.append((abs(ic), nm, len(xs), ic))
    for _, nm, n, ic in sorted(rows, reverse=True):
        verdict = ("СИЛЬНО" if abs(ic) >= 0.05 else
                   "слабо" if abs(ic) >= 0.02 else "шум")
        print(f"  {nm:<22} {n:>6} {ic:>+8.4f}  {verdict}")
    if not rows:
        print("  (мало данных — нужна запись подлиннее)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Измерить предсказательную силу признаков на записи")
    ap.add_argument("recording")
    ap.add_argument("--horizon", type=float, default=1.0,
                    help="на сколько секунд вперёд предсказываем (деф. 1)")
    ap.add_argument("--split", action="store_true",
                    help="разбить пополам: подобрать на первой, проверить "
                         "на второй (единственная честная проверка)")
    args = ap.parse_args(argv)

    rows = read(args.recording)
    feats, fwd = build(rows, args.horizon)
    if not fwd:
        print("В записи нет пригодных наблюдений. Нужен файл от "
              "jump_trader.py --record за достаточное время.")
        return 1

    if args.split:
        h = len(fwd) // 2
        report(feats[:h], fwd[:h], "ПЕРВАЯ ПОЛОВИНА (на ней «подбираем»)")
        report(feats[h:], fwd[h:], "ВТОРАЯ ПОЛОВИНА (проверка)")
        print("\n  Признак имеет смысл, только если IC сохранил знак и порядок")
        print("  на второй половине. Если развалился — это была подгонка.")
    else:
        report(feats, fwd, f"ВСЯ ЗАПИСЬ, горизонт {args.horizon}с")
        print("\n  IC = ранговая корреляция признака с будущим движением цены.")
        print("  Прогони с --split, прежде чем чему-то верить.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
