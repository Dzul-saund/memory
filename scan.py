#!/usr/bin/env python3
"""Три счётчика по записи рынка — вопросы, на которые отвечают ФАКТЫ.

Ни один из трёх ответов нельзя получить рассуждением. Зато все три уже лежат
в `market.jsonl`: бот пишет туда обе цены (нашу и якорь Polymarket), обе
стороны книги с размерами и итог каждого раунда.

    python scan.py market.jsonl              # все три
    python scan.py market.jsonl --arb        # только арбитраж
    python scan.py market.jsonl --certain
    python scan.py market.jsonl --lag --split

1. АРБИТРАЖ. Если `ask(Up) + ask(Down) < $1.00`, то купив обе стороны, ты
   платишь меньше доллара за то, что гарантированно вернёт ровно доллар.
   Без модели, без sigma, без таргета — единственная вещь на этом рынке
   вообще без рыночного риска. Вопрос ровно один: как часто книга это даёт
   и на какой размер. Считаем по верхнему уровню с учётом ЕГО объёма:
   пара стоит столько, сколько реально можно взять, а не сколько хочется.

2. ПРЕМИЯ ЗА ОПРЕДЕЛЁННОСТЬ. Сейчас `JUMP_MAX_LEG_PRICE=0.95` запрещает
   покупать дороже 95¢. Но за минуту до конца раунда на 15 сигмах от таргета
   ценового риска уже нет, а сторона стоит 98¢ — это +2% за минуту против
   не ценового, а ОПЕРАЦИОННОГО риска (расхождение оракула, авария биржи,
   спорный расчёт). Частоту таких сбоев не выведешь из формулы — её можно
   только посчитать. Берём ОДНО наблюдение на раунд, а не на такт: соседние
   такты не независимы, и по ним «статистика» получилась бы дутой.

3. ЛАГ. Наша цена минус якорь Polymarket. Предсказывает ли этот разрыв
   будущее движение книги — то есть заслуживает ли он места в решении.
"""
from __future__ import annotations

import argparse
import statistics as st
from typing import Dict, List, Optional

from features import read, mid, spearman

PAYOUT = 1.0


# ---------------------------------------------------------------------------
#  1. Арбитраж: пара дешевле доллара
# ---------------------------------------------------------------------------
def scan_arb(rows: List[dict], gap_s: float = 1.0) -> None:
    print("\n1. АРБИТРАЖ  (ask Up + ask Down < $1.00)")
    print("   " + "-" * 62)

    ticks, episodes = [], []
    prev_t = None
    for r in rows:
        if r.get("_settle"):
            continue
        ua, da = r.get("ua"), r.get("da")
        if ua is None or da is None:
            continue
        pair = ua + da
        if pair >= PAYOUT:
            prev_t = None
            continue
        # Реально взять можно лишь столько пар, сколько лежит на ТОНКОЙ
        # стороне верхнего уровня. Без этого «прибыль» — фантазия.
        size = min(r.get("uas") or 0.0, r.get("das") or 0.0)
        rec = {"t": r.get("t"), "pair": pair, "size": size,
               "profit": size * (PAYOUT - pair)}
        ticks.append(rec)
        if prev_t is None or (rec["t"] or 0) - prev_t > gap_s:
            episodes.append(rec)          # новый эпизод, а не тот же самый
        prev_t = rec["t"] or 0

    if not ticks:
        n = sum(1 for r in rows if not r.get("_settle"))
        print(f"   не встретилось ни разу за {n:,} тактов.")
        print("   Вывод: книга такого не даёт — тему можно закрыть.")
        return

    total = sum(e["profit"] for e in episodes)
    best = min(ticks, key=lambda e: e["pair"])
    span_h = _span_hours(rows)
    print(f"   тактов с арбитражем : {len(ticks):,}")
    print(f"   отдельных эпизодов  : {len(episodes):,}"
          + (f"  ({len(episodes)/span_h:.1f} в час)" if span_h else ""))
    print(f"   лучшая пара         : ${best['pair']:.4f} "
          f"(прибыль {(PAYOUT - best['pair'])*100:.2f}¢ с пары)")
    print(f"   суммарно за запись  : ${total:,.2f} "
          f"(с учётом размера верхнего уровня)")
    if total < 1.0:
        print("   Столько не окупит ни разработку, ни риск исполнения.")
    else:
        print("   ВАЖНО: обе ноги надо успеть взять. Одна нога без второй —")
        print("   это уже обычная направленная ставка, а не арбитраж.")


# ---------------------------------------------------------------------------
#  2. Премия за определённость: сторона дороже floor в конце раунда
# ---------------------------------------------------------------------------
def scan_certain(rows: List[dict], floor: float, tail_s: float) -> None:
    print(f"\n2. ПРЕМИЯ ЗА ОПРЕДЕЛЁННОСТЬ  (сторона >= {floor:.2f} "
          f"за {tail_s:.0f}с до конца)")
    print("   " + "-" * 62)

    winners, seen = {}, []
    for r in rows:
        if r.get("_settle"):
            if r.get("resolved") and r.get("winner"):
                winners[r.get("slug")] = r["winner"]
            continue
        seen.append(r)

    # По одному наблюдению на раунд: первый такт, попавший в хвост окна.
    picked: Dict[str, dict] = {}
    for r in seen:
        slug, left = r.get("slug"), r.get("left")
        if slug is None or left is None or left > tail_s:
            continue
        picked.setdefault(slug, r)

    obs = []
    for slug, r in picked.items():
        win = winners.get(slug)
        if win is None:
            continue                       # раунд не определился — не считаем
        for side, ask in (("Up", r.get("ua")), ("Down", r.get("da"))):
            if ask is not None and ask >= floor:
                obs.append({"slug": slug, "side": side, "ask": ask,
                            "won": side == win})
                break

    if not obs:
        print(f"   таких моментов в записи нет "
              f"(раундов с исходом: {len(winners)}).")
        return

    wins = sum(1 for o in obs if o["won"])
    n = len(obs)
    avg_ask = st.mean(o["ask"] for o in obs)
    # $1 в каждую такую сделку: выигрыш даёт 1/ask шэров по $1.
    pnl = sum((PAYOUT / o["ask"] - 1.0) if o["won"] else -1.0 for o in obs)

    print(f"   раундов с исходом   : {len(winners)}")
    print(f"   наблюдений          : {n}   (по одному на раунд)")
    print(f"   выиграла эта сторона: {wins}  ({wins/n*100:.1f}%)")
    print(f"   средний ask         : {avg_ask:.4f}  "
          f"(безубыток = {avg_ask*100:.2f}% побед)")
    print(f"   P&L по $1 на сделку : ${pnl:+,.2f}")

    losses = n - wins
    if losses == 0:
        # Правило трёх: ноль событий на n наблюдениях означает верхнюю границу
        # частоты около 3/n при 95% доверии. НЕ «никогда».
        upper = 3.0 / n
        print(f"\n   Ноль проигрышей на {n} наблюдениях. Это НЕ «никогда»:")
        print(f"   верхняя граница частоты сбоя ~{upper*100:.2f}% "
              f"(правило трёх, 95%).")
        need = 1.0 - avg_ask
        print(f"   Безубыток требует частоты сбоя ниже {need*100:.2f}%.")
        if upper > need:
            need_n = int(3.0 / need) + 1
            print(f"   {upper*100:.2f}% > {need*100:.2f}% — данных ПОКА "
                  f"НЕ ХВАТАЕТ, чтобы отличить прибыль от убытка.")
            print(f"   Нужно минимум ~{need_n:,} раундов "
                  f"(~{need_n/288:.0f} суток записи).")
        else:
            print("   Данных уже достаточно, чтобы граница била безубыток.")
    else:
        print(f"\n   Проигрышей: {losses}. Каждый стоит ~$1, каждый выигрыш "
              f"даёт ~${PAYOUT/avg_ask - 1:.3f}.")
        print(f"   Один проигрыш съедает {1/(PAYOUT/avg_ask - 1):.0f} "
              f"выигрышей.")


# ---------------------------------------------------------------------------
#  3. Лаг: предсказывает ли отставание якоря движение книги
# ---------------------------------------------------------------------------
def scan_lag(rows: List[dict], horizon: float, split: bool) -> None:
    print(f"\n3. ЛАГ ЯКОРЯ  (наша цена − pm, горизонт {horizon:.1f}с)")
    print("   " + "-" * 62)

    seq = [r for r in rows if not r.get("_settle")]
    lags, fwd, ages = [], [], []
    j = 0
    for i, r in enumerate(seq):
        p, pm, t = r.get("price"), r.get("pm"), r.get("t")
        m0 = mid(r)
        if None in (p, pm, t) or m0 is None:
            continue
        while j < len(seq) and (seq[j].get("t") or 0) < t + horizon:
            j += 1
        if j >= len(seq) or seq[j].get("slug") != r.get("slug"):
            continue                       # за границу раунда не заглядываем
        m1 = mid(seq[j])
        if m1 is None:
            continue
        lags.append(p - pm)
        fwd.append(m1 - m0)
        if r.get("pm_age") is not None:
            ages.append(r["pm_age"])

    if len(lags) < 100:
        print(f"   наблюдений {len(lags)} — мало, нужна запись подлиннее.")
        return

    absl = [abs(v) for v in lags]
    print(f"   наблюдений          : {len(lags):,}")
    print(f"   |лаг| медиана       : ${st.median(absl):.2f}")
    print(f"   |лаг| 90-й проц.    : ${sorted(absl)[int(len(absl)*0.9)]:.2f}")
    print(f"   |лаг| максимум      : ${max(absl):.2f}")
    if ages:
        print(f"   возраст якоря, мс   : медиана {st.median(ages):.0f}, "
              f"90-й проц. {sorted(ages)[int(len(ages)*0.9)]:.0f}")

    ic = spearman(lags, fwd)
    print(f"   IC (лаг → mid Up)   : {_ic(ic)}")

    if split:
        h = len(lags) // 2
        a = spearman(lags[:h], fwd[:h])
        b = spearman(lags[h:], fwd[h:])
        print(f"   первая половина     : {_ic(a)}")
        print(f"   вторая половина     : {_ic(b)}")
        if a is None or b is None:
            print("\n   Половину посчитать не удалось.")
        elif a * b <= 0:
            print("\n   ЗНАК НЕ УСТОЯЛ на второй половине — это была "
                  "случайность,")
            print("   строить на лаге вход нельзя.")
        else:
            print("\n   Знак сохранился на обеих половинах — признак живой.")
            print("   Это ещё не прибыль: IC меряет направление, а не то,")
            print("   переживёт ли сигнал спред и пинг.")


def _ic(v: Optional[float]) -> str:
    if v is None:
        return "не посчитать"
    if abs(v) < 0.02:
        return f"{v:+.4f}  шум"
    if abs(v) < 0.05:
        return f"{v:+.4f}  слабо"
    return f"{v:+.4f}  СИЛЬНО"


def _span_hours(rows: List[dict]) -> float:
    ts = [r.get("t") for r in rows if r.get("t")]
    return (max(ts) - min(ts)) / 3600.0 if len(ts) > 1 else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Счётчики по записи: арбитраж, премия за определённость, "
                    "лаг якоря")
    ap.add_argument("recording", help="файл JSONL от --record")
    ap.add_argument("--arb", action="store_true")
    ap.add_argument("--certain", action="store_true")
    ap.add_argument("--lag", action="store_true")
    ap.add_argument("--floor", type=float, default=0.97,
                    help="с какой цены сторона считается «решённой» (деф. 0.97)")
    ap.add_argument("--tail", type=float, default=30.0,
                    help="за сколько секунд до конца смотрим (деф. 30)")
    ap.add_argument("--horizon", type=float, default=1.0,
                    help="горизонт предсказания для лага, сек (деф. 1)")
    ap.add_argument("--split", action="store_true",
                    help="проверить лаг на второй половине выборки")
    args = ap.parse_args(argv)

    rows = read(args.recording)
    if not rows:
        print("запись пуста или не читается")
        return 1

    span = _span_hours(rows)
    print(f"\nЗапись: {args.recording} — {len(rows):,} строк, "
          f"{span:.1f} ч")

    all_of = not (args.arb or args.certain or args.lag)
    if all_of or args.arb:
        scan_arb(rows)
    if all_of or args.certain:
        scan_certain(rows, args.floor, args.tail)
    if all_of or args.lag:
        scan_lag(rows, args.horizon, args.split)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
