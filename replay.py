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
from flowbot.signals import HISTORY_S
from flowbot.strategy import BUY, SELL, Snapshot
from flowbot.trading import build_strategy


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


class SnapshotBuilder:
    """Строит снимки из записи, восстанавливая накопленное состояние.

    В файле лежит только то, что было известно в этот такт. Но стратегия
    видит в бою ещё и НАКОПЛЕННОЕ: историю цены за две минуты и время, когда
    книга менялась в последний раз. Восстанавливать это обязательно — без
    истории не измерить волатильность, и прогон покажет ноль сделок там, где
    бот в бою торговал бы.
    """

    def __init__(self) -> None:
        self.hist: List[tuple] = []
        self._last_book = None
        self._last_book_t: Optional[float] = None

    def reset(self) -> None:
        """Граница раунда: движок здесь тоже забывает историю цены."""
        self.hist.clear()
        self._last_book = None
        self._last_book_t = None

    def build(self, r: dict) -> Snapshot:
        t = r["t"]
        px = r.get("price")
        if px is not None:
            self.hist.append((t, px))
            while self.hist and t - self.hist[0][0] > HISTORY_S:
                self.hist.pop(0)

        # Возраст книги. Новые записи хранят его по факту — это единственный
        # честный источник: при обрыве потока такты продолжают идти по
        # таймеру с теми же ценами, и по самой записи обрыв не виден.
        # Для старых записей остаётся оценка «когда уровни менялись», но она
        # завышает возраст на решённом раунде, где топ честно стоит.
        age = r.get("book_age")
        if age is None:
            levels = (r.get("up_levels"), r.get("down_levels"))
            if levels != self._last_book:
                self._last_book, self._last_book_t = levels, t
            age = (t - self._last_book_t
                   if self._last_book_t is not None else None)

        up = r.get("up_levels") or [[], []]
        dn = r.get("down_levels") or [[], []]
        return Snapshot(
            t=t, seconds_left=r.get("left", 0.0), coin_price=px,
            target=r.get("target"),
            up_bid=r.get("ub"), up_ask=r.get("ua"),
            down_bid=r.get("db"), down_ask=r.get("da"),
            pm_price=r.get("pm"), pm_age_ms=r.get("pm_age"),
            price_hist=list(self.hist),
            up_levels=(_lv(up[0]), _lv(up[1])),
            down_levels=(_lv(dn[0]), _lv(dn[1])),
            book_age_s=age,
        )


def _lv(levels) -> list:
    """Уровни книги из записи -> [(цена, объём)]."""
    out = []
    for row in levels or []:
        try:
            out.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


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
    strat = build_strategy(cfg)
    builder = SnapshotBuilder()
    legs: List[dict] = []          # открытые позиции: {idx, side, shares, cost}
    round_pnl = 0.0
    touched = False                # в этом раунде была хоть одна сделка
    cur_slug: Optional[str] = None

    def settle(winner: Optional[str], resolved: bool, t: float = 0.0) -> None:
        nonlocal round_pnl, legs, touched
        builder.reset()
        if not legs and not touched:
            return                      # в этом раунде бот вообще не торговал
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
            # Стратегия обязана узнать исход — на этом держится дневной риск.
            # Разойдись прогон с боем здесь, и лимит потерь проверить будет
            # нечем: в записи он бы никогда не срабатывал.
            slg = next((x for x in strat.legs if x.idx == lg["idx"]), None)
            if slg is not None:
                strat.record_settle(slg, payout, pnl, t)
        legs = []
        # Раунд засчитывается, даже если из него вышли досрочно и ног к
        # расчёту не осталось. Иначе его P&L утекал бы в следующий раунд, а
        # именно такие раунды — где сработал аварийный выход — и надо
        # рассматривать в первую очередь.
        res.round_pnls.append(round(round_pnl, 4))
        res.rounds += 1
        round_pnl = 0.0
        touched = False
        strat.reset_round()

    for r in read_records(path):
        if r.get("type") == "settle":
            settle(r.get("winner"), bool(r.get("resolved", True)),
                   r.get("t", 0.0))
            cur_slug = None
            continue

        slug = r.get("slug")
        if cur_slug is not None and slug != cur_slug:
            # Новое окно. Историю цены обнуляем ВСЕГДА — ровно как движок на
            # границе раунда: иначе дно прошлого окна выглядело бы скачком на
            # первой секунде этого и завышало бы измеренную σ.
            builder.reset()
            if legs or touched:
                # Запись оборвалась без строки settle (бот убит) — раунд не
                # засчитываем как результат, иначе он соврёт статистику.
                legs = []
                touched = False
                strat.reset_round()
                round_pnl = 0.0
        cur_slug = slug

        snap = builder.build(r)
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
            touched = True
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


# Ключи перебора. Каждый порог стратегии обязан быть здесь: правило проекта
# — не подбирать их на глаз. Здесь уже дважды делались выводы на 9-20
# наблюдениях, и оба разворачивались от пары случаев.
SWEEPS = {
    "stake": ("stake_usdc", float),
    "max-round": ("max_round_usdc", float),
    "min-order": ("min_order_usdc", float),
    # --- стратегия «почти-факт» ---
    "z-min": ("cert_z_min", float),
    "z-full": ("cert_z_full", float),
    "risk-max": ("cert_risk_max", float),
    "risk-tighten": ("cert_risk_tighten", float),
    "z-slip": ("cert_z_slip", float),
    "max-price": ("cert_max_price", float),
    "min-price": ("cert_min_price", float),
    "max-spread": ("cert_max_spread_c", float),
    "min-depth": ("cert_min_depth_usd", float),
    "min-secs": ("cert_min_seconds_left", float),
    "max-secs": ("cert_max_seconds_left", float),
    "sigma-safety": ("cert_sigma_safety", float),
    "sigma-floor": ("cert_sigma_floor", float),
    "vol-fast": ("cert_vol_fast_s", float),
    "vol-slow": ("cert_vol_slow_s", float),
    "step-interval": ("cert_step_interval_s", float),
    "exit-z": ("cert_exit_z", float),
    "exit-jump": ("cert_exit_jump_mult", float),
    "exit-ticks": ("cert_exit_confirm_ticks", int),
    "exit-secs": ("cert_exit_confirm_s", float),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="проиграть запись рынка через стратегию")
    ap.add_argument("recording", help="файл записи (JSONL)")
    ap.add_argument("--stake", type=float, help="ставка, USDC")
    ap.add_argument("--max-round", type=float, help="потолок вложений в раунд")
    ap.add_argument("--strategy", help="какую стратегию проигрывать "
                                       "(none | certainty)")
    ap.add_argument("--sweep", nargs="+", metavar=("ПАРАМЕТР", "ЗНАЧЕНИЕ"),
                    help="перебрать значения: --sweep z-min 3 4 5")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="печатать каждую сделку")
    args = ap.parse_args(argv)

    def build() -> FlowConfig:
        c = FlowConfig.from_env()
        if args.strategy:
            c.strategy = args.strategy.lower()
        if args.stake is not None:
            c.stake_usdc = args.stake
        if args.max_round is not None:
            c.max_round_usdc = args.max_round
        # Потолок раунда не должен молча резать лестницу в прогоне: подбор
        # порогов на урезанной позиции измерял бы не ту стратегию, что торгует.
        if c.strategy == "certainty":
            c.max_round_usdc = max(c.max_round_usdc, c.cert_full_size)
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
    if res.entries == 0 and cfg.strategy == "none":
        print("\nСделок нет — стратегия не выбрана. Прогоняй с "
              "`--strategy certainty`\nили выставь FLOW_STRATEGY=certainty "
              "в .env.")
    elif res.entries == 0:
        print("\nСделок нет: ни один такт записи не прошёл фильтры. Для этой "
              "стратегии\nэто нормальный исход — она входит редко. Посмотри "
              "`--sweep z-min 3 3.5 4`,\nчтобы увидеть, какой именно порог "
              "всё отсекает.")
    if res.unresolved:
        print(f"\nпозиций закрыто по рынку (раунд не определился): "
              f"{res.unresolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
