#!/usr/bin/env python3
"""Проигрывание записи рынка через стратегию.

Зачем. Пороги в этом проекте несколько раз подбирались на глаз, по десятку
сделок. Так нельзя: на девяти наблюдениях любой вывод переворачивается парой
случаев. Запись (`trader.py --record FILE`) хранит всё, что бот видел в
каждый такт, и здесь эта история прогоняется заново — столько раз, сколько
нужно, без риска и без ожидания.

Что честно, а что нет:
  * решения принимает ТА ЖЕ САМАЯ `Strategy`, что и в бою — не копия;
  * покупка исполняется по записанному ask, продажа по записанному bid,
    то есть по ценам, которые реально стояли в книге в тот момент;
  * заглядывания вперёд нет: стратегия на каждом такте видит ровно тот
    снимок, что был записан;
  * НЕ моделируется влияние наших заявок на книгу и проскальзывание глубже
    первого уровня. На ставках в единицы долларов это мелочь, на крупных
    результат окажется оптимистичнее реального.

Сейчас стратегия пустая, поэтому прогон честно покажет ноль сделок. Это и
есть проверка, что харнесс работает: как только в `flowbot/strategy.py`
появится логика, здесь сразу станет видно, что она делала бы на истории.

    python replay.py market.jsonl
    python replay.py market.jsonl --sweep stake 1 2 5
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Dict, Iterator, List, Optional

from btc_bot.util import whole_shares
from flowbot.config import FlowConfig
from flowbot.strategy import BUY, SELL, Snapshot, Strategy


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


def to_snapshot(r: dict) -> Snapshot:
    return Snapshot(
        t=r["t"], seconds_left=r.get("left", 0.0), coin_price=r.get("price"),
        target=r.get("target"),
        up_bid=r.get("ub"), up_ask=r.get("ua"),
        down_bid=r.get("db"), down_ask=r.get("da"),
        pm_price=r.get("pm"), pm_age_ms=r.get("pm_age"),
    )


class Result:
    def __init__(self):
        self.pnl = 0.0
        self.spent = 0.0
        self.rounds = 0
        self.entries = 0
        self.sells = 0
        self.round_pnls: List[float] = []
        self.by_idx: Dict[int, List[float]] = defaultdict(list)
        self.unresolved = 0

    def summary(self) -> str:
        wins = sum(1 for p in self.round_pnls if p > 0)
        n = len(self.round_pnls)
        worst = min(self.round_pnls) if self.round_pnls else 0.0
        best = max(self.round_pnls) if self.round_pnls else 0.0
        return (f"P&L ${self.pnl:+8.2f} | раундов {n:4} "
                f"(в плюс {wins:4}, {wins / n * 100 if n else 0:3.0f}%) | "
                f"вложено ${self.spent:8.2f} | "
                f"входов {self.entries:4} выходов {self.sells:3} | "
                f"лучший ${best:+6.2f} худший ${worst:+7.2f}")


def replay(path: str, cfg: FlowConfig, verbose: bool = False) -> Result:
    """Прогнать запись через стратегию и посчитать, что бы вышло."""
    res = Result()
    strat = Strategy(cfg)
    legs: List[dict] = []          # открытые позиции: {idx, side, shares, cost}
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
            res.by_idx[lg["idx"]].append(pnl)
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
        if act.kind == BUY:
            ask = snap.ask(act.outcome)
            if ask is None or (act.limit_price is not None
                               and ask > act.limit_price + 1e-9):
                continue
            # Размер считается ТОЧНО так же, как в бою: целыми шэрами.
            # Площадка отвергает суммы с более чем двумя знаками, а при цене
            # в целых центах это гарантирует только целое число шэров.
            # Разойдись здесь с движком — и подбор порогов по записи будет
            # мерить не ту стратегию, что торгует.
            want = (act.size_usdc or cfg.stake_usdc) / ask
            shares = whole_shares(want * ask, ask, cfg.min_order_usdc)
            if shares <= 0:
                continue
            cost = round(ask * shares, 4)
            if strat.net_out + cost > cfg.max_round_usdc + 1e-9:
                continue
            # Бид на входе обязателен: любой расчёт «пошло против» считается
            # от него, а не от уплаченного ask, иначе спред выглядел бы как
            # мгновенная просадка.
            eb = snap.bid(act.outcome)
            leg = strat.record_entry(act.outcome, ask, shares, cost, snap.t,
                                     entry_bid=eb if eb is not None else ask,
                                     feat=act.feat)
            legs.append({"idx": leg.idx, "side": act.outcome,
                         "shares": shares, "cost": cost, "last_bid": eb})
            res.entries += 1
            res.spent += cost
            if verbose:
                print(f"  ВХОД  {act.outcome} {shares:.0f} @ {ask:.2f} — "
                      f"{act.reason}")

        elif act.kind == SELL and act.sell_idx is not None:
            for lg in list(legs):
                if lg["idx"] != act.sell_idx:
                    continue
                bid = snap.bid(lg["side"])
                if bid is None:
                    break
                proceeds = round(bid * lg["shares"], 4)
                pnl = round(proceeds - lg["cost"], 4)
                res.pnl += pnl
                round_pnl += pnl
                res.by_idx[lg["idx"]].append(pnl)
                res.sells += 1
                legs.remove(lg)
                strat.record_sell(lg["idx"], proceeds, snap.t)
                if verbose:
                    print(f"  ВЫХОД {lg['side']} @ {bid:.2f} — P&L {pnl:+.2f}")
                break

    settle(None, False)
    return res


# Ключи перебора. Здесь остались только ИНФРАСТРУКТУРНЫЕ величины: пороги
# прежней стратегии удалены вместе с ней. Когда у новой появятся свои —
# добавлять их сюда, иначе подобрать их будет нечем.
SWEEPS = {
    "stake": ("stake_usdc", float),
    "max-round": ("max_round_usdc", float),
    "min-order": ("min_order_usdc", float),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="проиграть запись рынка через стратегию")
    ap.add_argument("recording", help="файл записи (JSONL)")
    ap.add_argument("--stake", type=float, help="ставка, USDC")
    ap.add_argument("--max-round", type=float, help="потолок вложений в раунд")
    ap.add_argument("--sweep", nargs="+", metavar=("ПАРАМЕТР", "ЗНАЧЕНИЕ"),
                    help="перебрать значения: --sweep stake 1 2 5")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="печатать каждую сделку")
    args = ap.parse_args(argv)

    def build() -> FlowConfig:
        c = FlowConfig.from_env()
        if args.stake is not None:
            c.stake_usdc = args.stake
        if args.max_round is not None:
            c.max_round_usdc = args.max_round
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

    res = replay(args.recording, build(), verbose=args.verbose)
    print(res.summary())
    if res.entries == 0:
        print("\nСделок нет — стратегия пустая (flowbot/strategy.py: "
              "should_enter возвращает None).\nЭто ожидаемо до тех пор, пока "
              "новая логика не написана.")
    if res.unresolved:
        print(f"\nпозиций закрыто по рынку (раунд не определился): "
              f"{res.unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
