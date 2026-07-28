#!/usr/bin/env python3
"""Проигрывание записи рынка через скачковую стратегию.

Зачем. Пороги в этом проекте несколько раз подбирались на глаз, по десятку
сделок. Так нельзя: на 9 наблюдениях любой вывод переворачивается парой
случаев. Запись (`jump_trader.py --record FILE`) хранит ВСЁ, что бот видел в
каждый такт, и здесь эта история прогоняется заново с ЛЮБЫМИ порогами —
столько раз, сколько нужно, без риска и без ожидания.

Что честно, а что нет:
  * решения принимает та же самая `JumpStrategy`, что и в бою — не копия;
  * покупка исполняется по записанному ask, продажа по записанному bid,
    то есть по ценам, которые реально стояли в книге в тот момент;
  * заглядывания вперёд нет: стратегия на каждом такте видит ровно тот
    снимок, что был записан;
  * НЕ моделируется влияние наших заявок на книгу и проскальзывание глубже
    первого уровня. На ставках в единицы долларов это мелочь, но на крупных
    результат окажется оптимистичнее реального.

Примеры:
    python replay.py market.jsonl
    python replay.py market.jsonl --max-legs 2
    python replay.py market.jsonl --sweep max-legs 0 1 2 3 4
    python replay.py market.jsonl --sweep min-shift 0 1 2 3 5
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Dict, Iterator, List, Optional

from btc_bot.util import floor2
from flowbot.config import FlowConfig
from flowbot.jump import (ENTER, LADDER, SELL, JumpSnapshot, JumpStrategy,
                          ladder_shares)


def read_records(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue          # битая строка (обрыв записи) — пропускаем


def to_snapshot(r: dict) -> JumpSnapshot:
    return JumpSnapshot(
        t=r["t"], seconds_left=r.get("left", 0.0), coin_price=r.get("price"),
        target=r.get("target"), jump_usd=r.get("jump", 0.0),
        sigma_1s=r.get("sigma"),
        up_bid=r.get("ub"), up_ask=r.get("ua"),
        down_bid=r.get("db"), down_ask=r.get("da"),
        up_flow=r.get("uf", 0.0), down_flow=r.get("df", 0.0),
    )


class Result:
    def __init__(self):
        self.pnl = 0.0
        self.spent = 0.0
        self.rounds = 0
        self.entries = 0
        self.ladders = 0
        self.sells = 0
        self.round_pnls: List[float] = []
        self.by_depth: Dict[int, List[float]] = defaultdict(list)
        self.unresolved = 0

    def summary(self) -> str:
        wins = sum(1 for p in self.round_pnls if p > 0)
        n = len(self.round_pnls)
        worst = min(self.round_pnls) if self.round_pnls else 0.0
        best = max(self.round_pnls) if self.round_pnls else 0.0
        return (f"P&L ${self.pnl:+8.2f} | раундов {n:4} "
                f"(в плюс {wins:4}, {wins/n*100 if n else 0:3.0f}%) | "
                f"вложено ${self.spent:8.2f} | "
                f"входов {self.entries:4} доборов {self.ladders:3} "
                f"фиксаций {self.sells:3} | "
                f"лучший ${best:+6.2f} худший ${worst:+7.2f}")


def replay(path: str, cfg: FlowConfig, verbose: bool = False) -> Result:
    """Прогнать запись через стратегию и посчитать, что бы вышло."""
    res = Result()
    strat = JumpStrategy(cfg)
    legs: List[dict] = []          # открытые ноги: {idx, side, shares, cost}
    round_pnl = 0.0
    cur_slug: Optional[str] = None

    def settle(winner: Optional[str], resolved: bool) -> None:
        nonlocal round_pnl, legs
        if not legs:
            legs = []
            return
        for lg in legs:
            if resolved and winner is not None:
                payout = lg["shares"] if lg["side"] == winner else 0.0
            else:
                payout = lg["shares"] * (lg.get("last_bid") or 0.0)
                res.unresolved += 1
            pnl = round(payout - lg["cost"], 4)
            res.pnl += pnl
            round_pnl += pnl
            res.by_depth[lg["idx"]].append(pnl)
        legs = []
        res.round_pnls.append(round(round_pnl, 4))
        res.rounds += 1
        round_pnl = 0.0
        strat.reset_round()

    for r in read_records(path):
        if r.get("type") == "settle":
            settle(r.get("winner"), bool(r.get("resolved", True)))
            cur_slug = None
            continue

        slug = r.get("slug")
        if cur_slug is not None and slug != cur_slug and legs:
            # Запись оборвалась без строки settle (бот убит) — раунд не
            # засчитываем как результат, иначе он соврёт статистику.
            legs = []
            strat.reset_round()
            round_pnl = 0.0
        cur_slug = slug

        snap = to_snapshot(r)
        for lg in legs:
            b = snap.bid(lg["side"])
            if b is not None:
                lg["last_bid"] = b

        act = strat.on_tick(snap)
        if act.kind in (ENTER, LADDER):
            ask = snap.ask(act.outcome)
            if ask is None or (act.limit_price is not None
                               and ask > act.limit_price + 1e-9):
                continue
            if act.shares is not None:
                shares = floor2(ladder_shares(strat.debt,
                                              cfg.jump_ladder_profit_usdc, ask))
            else:
                shares = floor2((act.size_usdc or 0.0) / ask)
            if shares <= 0:
                continue
            cost = round(ask * shares, 4)
            if strat.net_out + cost > cfg.jump_max_round_usdc + 1e-9:
                continue
            eb = snap.bid(act.outcome)
            leg = strat.record_entry(act.outcome, ask, shares, cost,
                                     act.track, snap.t,
                                     entry_bid=eb if eb is not None else ask)
            legs.append({"idx": leg.idx, "side": act.outcome,
                         "shares": shares, "cost": cost, "last_bid": ask})
            res.spent += cost
            if act.kind == ENTER:
                res.entries += 1
            else:
                res.ladders += 1
            if verbose:
                print(f"    {act.kind:6} {act.outcome:4} {shares:6.2f}шэр "
                      f"@{ask:.2f} = ${cost:5.2f}")
        elif act.kind == SELL:
            lg = next((x for x in legs if x["idx"] == act.sell_idx), None)
            if lg is None:
                continue
            bid = snap.bid(lg["side"]) or 0.0
            proceeds = round(bid * lg["shares"], 4)
            pnl = round(proceeds - lg["cost"], 4)
            res.pnl += pnl
            round_pnl += pnl
            res.by_depth[lg["idx"]].append(pnl)
            legs = [x for x in legs if x["idx"] != act.sell_idx]
            strat.record_sell(act.sell_idx, proceeds, snap.t)
            res.sells += 1
            if verbose:
                print(f"    sell   {lg['side']:4} @{bid:.2f} -> {pnl:+.2f}")

    settle(None, False)            # хвост записи
    return res


SWEEPS = {
    "max-legs": ("jump_max_ladder_legs", int),
    "min-shift": ("jump_min_shift_cents", float),
    "small": ("jump_small_usd", float),
    "big": ("jump_big_usd", float),
    "stake": ("jump_stake_usdc", float),
    "profit": ("jump_ladder_profit_usdc", float),
    "max-round": ("jump_max_round_usdc", float),
    "sigma-ref": ("jump_sigma_ref", float),
    "target-sigmas": ("jump_max_target_sigmas", float),
    "swing-lookback": ("jump_swing_lookback_s", float),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Прогнать запись рынка через скачковую стратегию")
    ap.add_argument("recording", help="файл JSONL от --record")
    ap.add_argument("--env-file", help="пресет с порогами")
    ap.add_argument("--max-legs", type=int)
    ap.add_argument("--min-shift", type=float)
    ap.add_argument("--stake", type=float)
    ap.add_argument("--no-adaptive", action="store_true")
    ap.add_argument("--sweep", nargs="+", metavar=("ПАРАМЕТР", "ЗНАЧЕНИЕ"),
                    help="перебрать значения: --sweep max-legs 0 1 2 3")
    ap.add_argument("--verbose", action="store_true", help="печатать сделки")
    args = ap.parse_args(argv)

    if args.env_file:
        try:
            from dotenv import load_dotenv
            load_dotenv(args.env_file, override=True)
        except Exception:  # pragma: no cover
            print("--env-file требует python-dotenv", file=sys.stderr)
            return 2

    def build() -> FlowConfig:
        c = FlowConfig.from_env()
        if args.max_legs is not None:
            c.jump_max_ladder_legs = args.max_legs
        if args.min_shift is not None:
            c.jump_min_shift_cents = args.min_shift
        if args.stake is not None:
            c.jump_stake_usdc = args.stake
        if args.no_adaptive:
            c.jump_adaptive = False
        return c

    if args.sweep:
        name, *values = args.sweep
        if name not in SWEEPS:
            print(f"неизвестный параметр {name!r}; доступно: "
                  + ", ".join(sorted(SWEEPS)), file=sys.stderr)
            return 2
        field, cast = SWEEPS[name]
        print(f"Перебор {name} ({field}) на {args.recording}\n")
        rows = []
        for v in values:
            cfg = build()
            setattr(cfg, field, cast(v))
            r = replay(args.recording, cfg)
            rows.append((v, r))
            print(f"  {name}={v:<6} {r.summary()}")
        best = max(rows, key=lambda kv: kv[1].pnl)
        print(f"\n  лучший по P&L: {name}={best[0]} (${best[1].pnl:+.2f})")
        print("  ВАЖНО: это подгонка под одну запись. Проверь на другой "
              "выборке, прежде чем менять настройки.")
        return 0

    cfg = build()
    res = replay(args.recording, cfg, verbose=args.verbose)
    print(res.summary())
    if res.by_depth:
        print("\nпо ступеням лестницы:")
        for d in sorted(res.by_depth):
            v = res.by_depth[d]
            w = sum(1 for x in v if x > 0)
            print(f"  ступень {d}: ног {len(v):4}  выигр {w:4}  "
                  f"P&L ${sum(v):+8.2f}")
    if res.unresolved:
        print(f"\nног закрыто по рынку (раунд не определился): {res.unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
