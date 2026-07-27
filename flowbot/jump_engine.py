"""Движок 4-й системы: скачковая лестница поверх трёх существующих мониторов.

Ничего не дублирует. Берёт готовое:

  * ЦЕНА  — фиды `fast_monitor` (7 бирж + якорь Polymarket) через PriceEngine;
    здесь же таргет раунда (тот самый openPrice, что показывает сайт);
  * КНИГА — поток CLOB через BookEngine: bid/ask обеих сторон и поток заявок;
  * ТРЕЙДЕР — `btc_bot.trader` (LiveTrader / DryRunTrader), тот же клиент и
    тот же баланс, что у остальных ботов.

Отличие от базового :class:`FlowEngine` — позиция не одна. Скачковая логика
строит ЛЕСТНИЦУ из нескольких ног (после добора мы держим обе стороны сразу),
поэтому здесь переопределены учёт позиции, исполнение и расчёт в конце окна.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import fast_monitor

from btc_bot.util import floor2

from .config import FlowConfig
from .engine import FlowEngine, now_ms, _m, _p, _short
from .jump import (ENTER, LADDER, SELL, JumpSnapshot, JumpStrategy,
                   ladder_shares)


class JumpEngine(FlowEngine):
    """Скачковая стратегия: вход по скачку в $, лестница добора, фиксация."""

    def __init__(self, cfg: FlowConfig, logger: Optional[logging.Logger] = None):
        super().__init__(cfg, logger or logging.getLogger("jumpbot"))
        # Базовый FlowEngine собрал σ-стратегию и позицию-одиночку — они здесь
        # не нужны: своя стратегия и свой список ног.
        self.strategy = JumpStrategy(cfg)
        self.pos = None
        self.legs: List[dict] = []          # фактические ноги (шэры, стоимость)
        self._window_key: Optional[float] = None
        self._round_pnl = 0.0
        self.rounds = 0

    # ======================================================================
    #  Фоновые задачи: сверх фидов нужен ещё таргет раунда
    # ======================================================================
    def _extra_tasks(self):
        return [fast_monitor.official_target_task(5, fast_monitor.COIN["dp"])]

    def _target(self) -> Optional[float]:
        """Таргет текущего раунда (openPrice с Polymarket), если он уже есть.

        Значение из fast_monitor помечено временем начала своего раунда —
        проверяем, что оно относится к ТЕКУЩЕМУ окну, иначе на первых секундах
        нового раунда мы бы фильтровали дистанцию по вчерашней цели.
        """
        rt = fast_monitor.ROUND_TARGET
        if not rt.get("exact") or rt.get("value") is None:
            return None
        now = time.time()
        start = int(now - now % 300)
        return rt["value"] if rt.get("start_ts") == start else None

    # ======================================================================
    #  Границы окна: лестница живёт ровно один раунд
    # ======================================================================
    def _enter_window(self, market: dict) -> None:
        super()._enter_window(market)
        key = market["window_ts"]
        if key != self._window_key:
            self._window_key = key
            self.strategy.reset_round()
            # Новый раунд — новый отсчёт: дно прошлого окна не должно
            # выглядеть «скачком» на первой же секунде этого.
            self.price.reset_swing()
            self._round_pnl = 0.0
            self.rounds += 1

    # ======================================================================
    #  Такт решения
    # ======================================================================
    def _tick(self) -> None:
        cfg = self.cfg
        self.price.update(now_ms())
        price = self.price.price()
        if self.market is None:
            return
        up, dn = self.market["up"], self.market["down"]
        ub, ua = self.book.best(up)
        db, da = self.book.best(dn)
        # swing: движение от локального дна/пика — срабатывает в тот же тик,
        # как только порог пройден. window: старое сравнение «сейчас против
        # N секунд назад».
        if cfg.jump_trigger_mode == "swing":
            jump_usd, jump_bps = self.price.swing()
        else:
            jump_usd, jump_bps = self.price.move_usd(cfg.jump_window_s)

        snap = JumpSnapshot(
            t=time.time(),
            seconds_left=max(0.0, self.market["end_ts"] - time.time()),
            coin_price=price, target=self._target(),
            jump_usd=jump_usd, jump_bps=jump_bps,
            up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
            up_flow=self.book.flow(up, cfg.flow_window_s),
            down_flow=self.book.flow(dn, cfg.flow_window_s),
        )

        # Запоминаем последний bid каждой ноги — по нему считаем расчёт, если
        # книга к моменту сеттла уже закрылась.
        for leg in self.legs:
            leg["last_bid"] = ub if leg["outcome"] == "Up" else db

        action = self.strategy.on_tick(snap)
        self._log_status(snap, action)

        if action.kind in (ENTER, LADDER, SELL) and not self.busy:
            self.busy = True
            asyncio.create_task(self._execute(action))

    # ======================================================================
    #  Исполнение
    # ======================================================================
    async def _execute(self, action) -> None:
        try:
            self.log.warning(">>> %s: %s", action.kind.upper(), action.reason)
            if action.kind in (ENTER, LADDER):
                await self._buy_leg(action)
            elif action.kind == SELL:
                await self._sell_leg(action.sell_idx, action.limit_price,
                                     "фиксация", result="SOLD_TP")
        except Exception as exc:  # noqa: BLE001 - движок должен жить
            self.log.error("исполнение упало: %s", exc)
        finally:
            self.busy = False

    async def _buy_leg(self, action) -> None:
        if self.market is None:
            return
        outcome = action.outcome
        token = self.market["up"] if outcome == "Up" else self.market["down"]
        await self._sim_latency()          # книга могла уйти за время пинга
        bid, ask = self.book.best(token)
        if ask is None:
            self.log.warning("ПОКУПКА %s не исполнена: нет ask", outcome)
            return
        if action.limit_price is not None and ask > action.limit_price + 1e-9:
            self.log.warning(
                "ПОКУПКА %s НЕ исполнена: ask %.2f > лимит %.2f — книга ушла "
                "за пинг ~%.0fмс", outcome, ask, action.limit_price,
                self.latency.fill_delay_ms)
            return
        fill = round(ask, 2)

        # Добор считается в ШЭРАХ (их число решает уравнение перекрытия
        # минуса), обычный вход — в долларах ставки. И то и другое надо
        # пересчитать по РЕАЛЬНОЙ цене филла, а не по той, что была в решении.
        if action.shares is not None:
            shares = floor2(ladder_shares(self.strategy.debt,
                                          self.cfg.jump_ladder_profit_usdc,
                                          fill))
        else:
            shares = floor2((action.size_usdc or 0.0) / fill)
        if shares <= 0:
            self.log.warning("ПОКУПКА %s: расчёт дал %.2f шэров — пропуск",
                             outcome, shares)
            return
        cost = round(fill * shares, 2)
        # Потолок раунда проверяем ещё раз ЗДЕСЬ, а не только в стратегии:
        # за время пинга цена филла могла вырасти, а вместе с ней и стоимость.
        if self.strategy.net_out + cost > self.cfg.jump_max_round_usdc + 1e-9:
            self.log.warning(
                "ПОКУПКА %s отменена: $%.2f + $%.2f вышли бы за потолок "
                "раунда $%.2f", outcome, self.strategy.net_out, cost,
                self.cfg.jump_max_round_usdc)
            return
        try:
            resp = await asyncio.to_thread(self.trader.buy, token, fill, shares)
        except Exception as exc:  # noqa: BLE001
            self.log.error("ордер BUY упал: %s", exc)
            return
        self._invalidate_balance()
        self.n_trades += 1
        # Движение отработано — переставляем экстремум на текущую цену, иначе
        # то же самое дно секунду спустя открыло бы ещё одну такую же сделку.
        self.price.reset_swing()
        leg = self.strategy.record_entry(outcome, fill, shares, cost,
                                         action.track, time.time())
        self.legs.append({
            "idx": leg.idx, "outcome": outcome, "token": token,
            "entry_price": fill, "shares": shares, "cost": cost,
            "track": action.track, "last_bid": bid,
            "coin_at_entry": self.price.price(),
            "secs_at_entry": int(self.market["end_ts"] - time.time()),
        })
        self.log.warning(
            "КУПЛЕНО [%s#%d] %s — %.2f шэр @ $%.2f (=$%.2f) | вложено за раунд "
            "$%.2f | ответ: %s", action.track, leg.idx, outcome, shares, fill,
            cost, self.strategy.net_out, _short(resp))

    async def _sell_leg(self, idx: Optional[int], sell_limit: Optional[float],
                        tag: str, result: str = "SOLD") -> None:
        """tag — человеку в лог, result — в CSV.

        В колонке result должен лежать машиночитаемый ASCII-код рядом с
        WON/LOST, а не русское слово: по нему потом фильтруют и считают.
        """
        leg = next((lg for lg in self.legs if lg["idx"] == idx), None)
        if leg is None:
            return
        await self._sim_latency()
        bid, _ask = self.book.best(leg["token"])
        px = bid if bid is not None else (leg.get("last_bid") or 0.0)
        fill = round(px, 2)
        shares = leg["shares"]
        proceeds = round(fill * shares, 2)
        try:
            resp = await asyncio.to_thread(self.trader.sell, leg["token"],
                                           fill, shares)
        except Exception as exc:  # noqa: BLE001
            self.log.error("ордер SELL упал: %s (нога оставлена)", exc)
            return
        self.trader.settle(proceeds)       # dry-run: вернуть кэш; live: no-op
        self._invalidate_balance()
        pnl = round(proceeds - leg["cost"], 2)
        self._book_pnl(pnl)
        self.legs = [lg for lg in self.legs if lg["idx"] != idx]
        self.strategy.record_sell(idx, proceeds, time.time())
        self.log.warning(
            "ПРОДАНО (%s) [#%d] %s — %.2f шэр @ $%.2f (=$%.2f) | P&L %+.2f "
            "(раунд %+.2f, итого %+.2f) | ответ: %s", tag, idx, leg["outcome"],
            shares, fill, proceeds, pnl, self._round_pnl, self.realized_pnl,
            _short(resp))
        self._log_trade_row(_row(leg), result, fill, proceeds, pnl)

    # ======================================================================
    #  Расчёт в конце окна: платит только выигравшая сторона
    # ======================================================================
    def _settle_open_position(self, reason: str) -> None:
        if not self.legs:
            return
        # Кто выиграл, определяем по последнему биду: сторона, чей «процент»
        # ушёл к 1.0, и есть победитель раунда.
        winner = self._winning_side()
        resolved = self._book_resolved()
        if not resolved:
            # Книга не схлопнулась: например Up 0.62 / Down 0.35. Объявлять по
            # ней победителя нельзя — это ставка 62/38, а не факт. Считаем ноги
            # по последней цене (сколько реально стоят), и помечаем UNSETTLED,
            # чтобы такие строки не портили статистику как «выиграно».
            top = self._top_bids()
            self.log.warning(
                "РАСЧЁТ (%s): книга не схлопнулась за %.0fс после конца окна "
                "(Up %s / Down %s) — ноги закрываю по последней цене и помечаю "
                "UNSETTLED, а не WON/LOST", reason, self.cfg.jump_settle_wait_s,
                _p(top[0]) if top else "—", _p(top[1]) if top else "—")

        payout_total = 0.0
        n_legs = len(self.legs)
        for leg in list(self.legs):
            if resolved:
                won = winner is not None and leg["outcome"] == winner
                payout = round(leg["shares"] * 1.0, 2) if won else 0.0
                result = "WON" if won else "LOST"
                mark = leg.get("last_bid") or 0.0
                tail = "ВЫИГРЫШ" if won else "проигрыш"
            else:
                mark = leg.get("last_bid") or 0.0
                payout = round(leg["shares"] * mark, 2)
                result = "UNSETTLED"
                tail = f"не определён (по {mark:.2f})"
            pnl = round(payout - leg["cost"], 2)
            payout_total += payout
            self._book_pnl(pnl)
            self.trader.settle(payout)
            self.log.warning(
                "СЕТТЛ (%s) [%s#%d] %s %s — payout $%.2f | P&L %+.2f",
                reason, leg["track"], leg["idx"], leg["outcome"],
                tail, payout, pnl)
            self._log_trade_row(_row(leg), result, mark, payout, pnl)
        self._invalidate_balance()
        self.log.warning(
            "── раунд закрыт: ног %d, выплата $%.2f, P&L раунда %+.2f "
            "(итого %+.2f, W/L %d/%d) ──", n_legs, payout_total,
            self._round_pnl, self.realized_pnl, self.wins, self.losses)
        self.legs = []
        self.strategy.reset_round()
        self._round_pnl = 0.0

    # -- ждём, пока книга схлопнется к 0/1 ------------------------------------
    def _stream_deadline(self, end_ts: float) -> float:
        return end_ts + self.cfg.jump_settle_wait_s

    def _book_resolved(self) -> bool:
        """Победитель уже виден: одна из сторон дошла до порога схлопывания."""
        top = self._top_bids()
        return top is not None and max(top) >= self.cfg.jump_settle_converge

    def _top_bids(self):
        """(bid Up, bid Down) — из книги, иначе последние виденные ногами."""
        if self.market is None:
            return None
        ub, _ = self.book.best(self.market["up"])
        db, _ = self.book.best(self.market["down"])
        if ub is None and db is None:
            marks = {lg["outcome"]: lg.get("last_bid") for lg in self.legs}
            ub, db = marks.get("Up"), marks.get("Down")
        if ub is None and db is None:
            return None
        return (ub or 0.0), (db or 0.0)

    def _winning_side(self) -> Optional[str]:
        """Сторона-победитель раунда по последним котировкам книги.

        К моменту расчёта «процент» победителя уходит к 1.0, проигравшего —
        к 0. Берём сторону с большим значением; если книга уже закрылась —
        последние биды, которые успели увидеть ноги.
        """
        if self.market is None:
            return None
        ub, _ = self.book.best(self.market["up"])
        db, _ = self.book.best(self.market["down"])
        if ub is None and db is None:
            marks = {lg["outcome"]: lg.get("last_bid") for lg in self.legs}
            ub, db = marks.get("Up"), marks.get("Down")
        if ub is None and db is None:
            return None
        if ub is None:
            return "Down" if db >= 0.5 else "Up"
        if db is None:
            return "Up" if ub >= 0.5 else "Down"
        return "Up" if ub >= db else "Down"

    def _book_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self._round_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1

    # ======================================================================
    #  Вывод
    # ======================================================================
    def _log_status(self, snap, action) -> None:
        now = time.time()
        if now - self._last_status < self.cfg.status_log_interval_seconds:
            return
        self._last_status = now
        if self.legs:
            pos = " ".join(f"{lg['track']}{lg['idx']}:{lg['outcome']}"
                           f"@{lg['entry_price']:.2f}×{lg['shares']:.1f}"
                           for lg in self.legs)
        else:
            pos = "—"
        tgt = (f" цель {snap.target:,.0f}" if snap.target is not None else
               " цель —")
        how = ("экстр" if self.cfg.jump_trigger_mode == "swing"
               else f"{self.cfg.jump_window_s:.0f}с")
        self.log.info(
            "t-%3ds | %s %s%s | скачок %+.2f$/%s | Up %s/%s Down %s/%s | "
            "поз %s | вложено $%.2f | $%.2f | %s",
            int(snap.seconds_left), self.cfg.asset.upper(),
            _m(snap.coin_price), tgt, snap.jump_usd, how,
            _p(snap.up_bid), _p(snap.up_ask), _p(snap.down_bid),
            _p(snap.down_ask), pos, self.strategy.net_out,
            self._get_balance(), action.reason,
        )

    def _banner(self) -> None:
        c = self.cfg
        mode = "DRY-RUN (без реальных ордеров)" if c.dry_run else "*** LIVE ***"
        self.log.info("=" * 72)
        self.log.info("СКАЧКОВАЯ система — %s Up/Down 5m — %s",
                      c.asset.upper(), mode)
        if c.jump_trigger_mode == "swing":
            how = (f"движение от локального дна/пика (окно поиска экстремума "
                   f"{c.jump_swing_lookback_s:.0f}с, срабатывает сразу)")
        else:
            how = f"движение за {c.jump_window_s:.0f}с"
        self.log.info("детекция: %s", how)
        self.log.info(
            "вход: скачок >=$%.0f, если %% стороны >= %.2f; иначе нужен "
            "скачок >=$%.0f и не дальше $%.0f от таргета",
            c.jump_small_usd, c.jump_price_split,
            c.jump_big_usd, c.jump_max_target_dist_usd)
        self.log.info(
            "ставка $%.2f; провал ниже %.2f → добор противоположной стороны на "
            "(вложено+%.2f)/(1-цена); максимум %d доборов и $%.0f за раунд",
            c.jump_stake_usdc, c.jump_price_split, c.jump_ladder_profit_usdc,
            c.jump_max_ladder_legs, c.jump_max_round_usdc)
        if c.jump_tp_enabled:
            self.log.info(
                "дешёвая дорожка: фиксируем прибыль (+%.2f от входа), если до "
                "конца <=%.0fс или рост кончился (откат %% %.2f / поток %.2f)",
                c.jump_tp_min_gain, c.jump_tp_deadline_s,
                c.jump_tp_stall_retrace, c.jump_tp_flow_against)
        self.log.info("=" * 72)

    def _summary(self) -> None:
        self.log.info("=" * 72)
        self.log.info("скачковая система остановлена: %s",
                      self.stop_reason or "—")
        self.log.info(
            "раундов: %d | покупок: %d | закрыто ног: %d (W/L %d/%d) | "
            "P&L: $%+.2f", self.rounds, self.n_trades,
            self.wins + self.losses, self.wins, self.losses, self.realized_pnl)
        if self.legs:
            self.log.info("открытых ног (без расчёта): %d", len(self.legs))
        try:
            self.log.info("баланс: $%.2f", self.trader.get_balance())
        except Exception as exc:  # noqa: BLE001
            self.log.info("баланс: недоступен (%s)", exc)
        if self.tradelog.enabled and (self.wins + self.losses):
            self.log.info("лог сделок: %s", self.tradelog.path)
        self.log.info("=" * 72)


def _row(leg: dict) -> dict:
    """Нога -> словарь в формате, который ждёт FlowEngine._log_trade_row.

    Дорожка и номер ноги уезжают прямо в поле outcome ("A0 Up", "B1 Down"),
    чтобы по CSV было видно, какая ветка логики её открыла. is_flip=False:
    префикс "FLIP " базового логгера здесь только мешал бы.
    """
    return {
        "outcome": f"{leg['track']}{leg['idx']} {leg['outcome']}",
        "entry_price": leg["entry_price"],
        "shares": leg["shares"],
        "cost": leg["cost"],
        "btc_at_entry": leg.get("coin_at_entry"),
        "secs_at_entry": leg.get("secs_at_entry"),
        "is_flip": False,
        "token": leg["token"],
    }
