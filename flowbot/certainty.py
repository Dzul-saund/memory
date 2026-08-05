"""Стратегия «покупка почти-факта»: YES около 0.98 с удержанием до расчёта.

ЧЕМ ЭТА СТРАТЕГИЯ ЗАРАБАТЫВАЕТ

Ставка $50 по цене 0.98 приносит $1.02 при выигрыше и теряет $50 при
проигрыше. Порог безубытка такой сделки — ровно 98.00%, то есть ровно цена
контракта. Это тождество, а не совпадение: справедливая цена по определению
даёт нулевое матожидание.

Значит источник прибыли ровно один: ситуации, где ИСТИННАЯ вероятность
заметно выше 0.98, а книга всё равно показывает 0.98. Такие ситуации
существуют и имеют название — премия за определённость: маркет-мейкеры не
котируют 0.999, потому что шаг цены целый цент, а держать риск ради
последней десятой процента невыгодно. Поэтому раунд, решённый на пять сигм
(истинная вероятность 0.9999997), продолжает стоить 0.98.

Отсюда единственное правило входа: **покупать только то, что уже решено**,
и уметь это доказать числом. Всё остальное в этом файле — проверки того,
что «решено» действительно решено.

ЧЕГО ЭТА СТРАТЕГИЯ НЕ ДЕЛАЕТ

Не усредняет убыток, не увеличивает ставку после потери, не отыгрывается.
Лестница здесь набирает позицию ВВЕРХ по уверенности, а не вниз по цене:
каждая следующая ступень требует, чтобы риск не вырос, — иначе набор просто
прекращается и позиция остаётся неполной.

ПОЧЕМУ ОДНА ПОТЕРЯ СТОИТ 49 ВЫИГРЫШЕЙ

$50 / $1.02 = 49. Это и определяет всю архитектуру: любая проверка, которая
уменьшает шанс потери хотя бы на процент, окупает отказ от десятков сделок.
Обратное неверно — лишняя сделка почти ничего не добавляет.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import risk
from .strategy import BUY, HOLD, NONE, SELL, Action, Leg, Snapshot, Strategy


@dataclass
class _RoundState:
    """Что происходило в текущем 5-минутном окне."""

    steps: int = 0                      # сколько ступеней лестницы взято
    spent: float = 0.0
    last_step_t: float = 0.0
    last_z: Optional[float] = None      # z на момент прошлой ступени
    side: Optional[str] = None          # какую сторону набираем
    # Подряд идущие подтверждения разворота. Один тик ничего не значит:
    # процент может провалиться до 0.50 и вернуться на 0.98 за секунду.
    alarm_ticks: int = 0
    alarm_since: Optional[float] = None
    alarm_last_t: Optional[float] = None   # такт, на котором уже считали
    stopped: bool = False               # набор в этом раунде прекращён


class CertaintyStrategy(Strategy):
    """Вход только в решённый раунд, лестницей, с единой оценкой риска."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.p = risk.RiskParams(
            z_min=cfg.cert_z_min, z_full=cfg.cert_z_full,
            max_price=cfg.cert_max_price, min_price=cfg.cert_min_price,
            max_spread_c=cfg.cert_max_spread_c,
            min_depth_usd=cfg.cert_min_depth_usd,
            min_seconds_left=cfg.cert_min_seconds_left,
            max_seconds_left=cfg.cert_max_seconds_left,
            max_stale_s=cfg.cert_max_stale_s,
            max_book_stale_s=cfg.cert_max_book_stale_s,
        )
        self.round = _RoundState()
        # --- дневной риск ---------------------------------------------------
        self._day: Optional[str] = None
        self.day_losses = 0
        self.day_pnl = 0.0
        self.day_trades = 0
        self.halted: Optional[str] = None      # причина остановки на день
        # Глубина бидов несколько секунд назад — чтобы заметить уход ММ.
        # По стороне: у Up и Down книги разные, сравнивать их между собой
        # бессмысленно.
        self._bid_hist: Dict[str, List[Tuple[float, float]]] = {
            "Up": [], "Down": []}
        self.last_report: Optional[risk.RiskReport] = None
        # Измерения одного такта. За такт `assess` зовётся до трёх раз (выход,
        # лестница, объяснение в лог), а `measure` перебирает 120 секунд
        # истории — считать её трижды на каждом событии книги незачем.
        self._tick_t: Optional[float] = None
        self._m: Optional[risk.Metrics] = None
        self._reports: Dict[Tuple[str, float], risk.RiskReport] = {}

    # ======================================================================
    #  Границы
    # ======================================================================
    def reset_round(self) -> None:
        super().reset_round()
        self.round = _RoundState()
        for v in self._bid_hist.values():
            v.clear()
        self._tick_t = None
        self._m = None
        self._reports.clear()

    def _roll_day(self, t: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if day != self._day:
            self._day = day
            self.day_losses = 0
            self.day_pnl = 0.0
            self.day_trades = 0
            self.halted = None

    def record_settle(self, leg: Leg, payout: float, pnl: float,
                      t: float) -> None:
        """Движок сообщает исход раунда. Отсюда живёт дневной риск."""
        self._roll_day(t)
        self.day_pnl += pnl
        self.day_trades += 1
        if pnl < 0:
            self.day_losses += 1
        self._check_halt()

    def record_sell(self, idx: int, proceeds: float, t: float) -> None:
        super().record_sell(idx, proceeds, t)
        # Аварийный выход — тоже событие дня: если пришлось выходить,
        # значит вход был ошибкой, и повторять её сегодня не надо.
        self.round.stopped = True

    def _check_halt(self) -> None:
        c = self.cfg
        if c.cert_max_day_losses and self.day_losses >= c.cert_max_day_losses:
            self.halted = (f"дневной лимит потерь: {self.day_losses} "
                           f"(одна потеря = {abs(50 / 1.02):.0f} выигрышей)")
        elif c.cert_max_day_loss_usd and self.day_pnl <= -c.cert_max_day_loss_usd:
            self.halted = f"дневной минус ${self.day_pnl:.2f}"
        elif c.cert_max_day_trades and self.day_trades >= c.cert_max_day_trades:
            self.halted = f"дневной лимит сделок: {self.day_trades}"

    # ======================================================================
    #  Измерения
    # ======================================================================
    def _metrics(self, s: Snapshot) -> risk.Metrics:
        """Измерения такта. Считаются один раз и переиспользуются."""
        if self._tick_t == s.t and self._m is not None:
            return self._m
        c = self.cfg
        self._m = risk.measure(
            s.price_hist, fast_s=c.cert_vol_fast_s, slow_s=c.cert_vol_slow_s,
            sigma_safety=c.cert_sigma_safety, sigma_floor=c.cert_sigma_floor)
        self._tick_t = s.t
        self._reports.clear()
        # Глубину бидов пишем ровно один раз за такт: иначе три вызова assess
        # положат в историю три одинаковые точки и «глубина 5 секунд назад»
        # станет «глубина три вызова назад».
        for side in ("Up", "Down"):
            bids, _asks = self._levels(s, side)
            self._push_bid_depth(side, s.t, risk.depth_usd(bids))
        return self._m

    def _side(self, s: Snapshot) -> Optional[str]:
        """Какая сторона сейчас выигрывает. None — таргет неизвестен."""
        if s.coin_price is None or s.target is None:
            return None
        return "Up" if s.coin_price >= s.target else "Down"

    def _levels(self, s: Snapshot, side: str):
        return s.up_levels if side == "Up" else s.down_levels

    def _push_bid_depth(self, side: str, t: float, usd: float) -> None:
        hist = self._bid_hist[side]
        hist.append((t, usd))
        keep = self.cfg.cert_biddrop_s * 3
        self._bid_hist[side] = [(x, v) for x, v in hist if t - x <= keep]

    def _bid_depth_before(self, s: Snapshot, side: str) -> Optional[float]:
        """Глубина бидов cert_biddrop_s секунд назад."""
        edge = s.t - self.cfg.cert_biddrop_s
        old = [v for t, v in self._bid_hist[side] if t <= edge]
        return old[-1] if old else None

    def assess(self, s: Snapshot, side: str,
               need_usd: float = 0.0) -> risk.RiskReport:
        m = self._metrics(s)          # он же сбрасывает кэш на новом такте
        key = (side, need_usd)
        cached = self._reports.get(key)
        if cached is not None:
            self.last_report = cached
            return cached
        bids, asks = self._levels(s, side)
        age = None
        if s.price_hist:
            age = s.t - s.price_hist[-1][0]
        rep = risk.assess(
            side=side, price=s.coin_price, target=s.target,
            seconds_left=s.seconds_left, ask=s.ask(side), bid=s.bid(side),
            m=m, our_levels=(bids, asks),
            bid_depth_before=self._bid_depth_before(s, side),
            pm_price=s.pm_price, data_age_s=age, book_age_s=s.book_age_s,
            need_usd=need_usd, p=self.p)
        self._reports[key] = rep
        self.last_report = rep
        return rep

    # ======================================================================
    #  Такт
    # ======================================================================
    def on_tick(self, s: Snapshot) -> Action:
        self._roll_day(s.t)

        # 1. Выход проверяется ВСЕГДА и первым: позиция важнее новых сделок.
        for leg in list(self.legs):
            act = self.should_exit(s, leg)
            if act is not None:
                return act

        if self.halted:
            return Action(NONE, f"остановлен на сегодня: {self.halted}")

        # 2. Добор лестницы, если позиция уже есть и ещё не полная.
        if self.legs:
            act = self._maybe_ladder(s)
            return act if act is not None else Action(
                HOLD, self._hold_reason(s))

        # 3. Первый вход.
        act = self.should_enter(s)
        return act if act is not None else Action(NONE, self._why_not(s))

    def _hold_reason(self, s: Snapshot) -> str:
        held = sum(lg.cost for lg in self.legs)
        st = self.round
        base = (f"держу ${held:.0f}/{self.cfg.cert_full_size:.0f} "
                f"({st.steps}/{len(self.cfg.cert_steps)} ступ.)")
        # Запас считаем прямо: он есть всегда, когда есть цена и таргет, и
        # именно он показывает, насколько позиция ещё в безопасности.
        m = self._metrics(s)
        _buf, z, _ = risk.buffer_z(
            side=self.legs[0].outcome, price=s.coin_price, target=s.target,
            seconds_left=s.seconds_left, sigma=m.sigma)
        if z is not None:
            base += f" | запас {z:.1f}σ (выход ниже {self.cfg.cert_exit_z:.1f}σ)"
        if st.alarm_ticks:
            base += f" | ТРЕВОГА {st.alarm_ticks}"
        if st.stopped:
            base += " | набор прекращён"
        return base

    def _why_not(self, s: Snapshot) -> str:
        if self.round.stopped:
            return "набор в этом раунде прекращён"
        side = self._side(s)
        if side is None:
            return "таргет раунда неизвестен"
        # Тот же отчёт, что и в should_enter (кэш такта) — строка состояния
        # обязана объяснять именно то решение, которое было принято.
        rep = self.assess(s, side, need_usd=self.cfg.cert_steps[0])
        if rep.vetoes:
            return f"{side}: {rep.vetoes[0]}"
        return f"{side}: {rep.summary()} > порога {self.cfg.cert_risk_max:.0f}"

    # ======================================================================
    #  ВХОД
    # ======================================================================
    def should_enter(self, s: Snapshot) -> Optional[Action]:
        if self.round.stopped:
            return None
        side = self._side(s)
        if side is None:
            return None
        rep = self.assess(s, side, need_usd=self.cfg.cert_steps[0])
        if not rep.ok or rep.score > self.cfg.cert_risk_max:
            return None
        return self._step_action(s, side, rep, step=0)

    def _maybe_ladder(self, s: Snapshot) -> Optional[Action]:
        """Следующая ступень: только если риск НЕ вырос.

        Лестница набирает вверх по уверенности, а не вниз по цене. Если
        обстановка ухудшилась, набор прекращается насовсем в этом раунде —
        добирать «раз уж начали» здесь означало бы усреднять убыток.
        """
        c, st = self.cfg, self.round
        if st.stopped or st.side is None:
            return None
        if st.steps >= len(c.cert_steps):
            return None
        if s.t - st.last_step_t < c.cert_step_interval_s:
            return None

        rep = self.assess(s, st.side, need_usd=c.cert_steps[st.steps])
        # Планка ужесточается с каждой ступенью: чем больше денег в позиции,
        # тем выше должна быть уверенность, чтобы добавить ещё.
        limit = max(0.0, c.cert_risk_max - c.cert_risk_tighten * st.steps)
        if not rep.ok:
            # НЕ ВСЯКИЙ ЗАПРЕТ — УХУДШЕНИЕ РЫНКА. «Цена устарела», «книга не
            # менялась», «до расчёта мало времени» означают, что мы на секунду
            # ослепли или окно входа кончилось, а не что позиция стала хуже.
            # Хоронить лестницу из-за моргнувшего фида нельзя: позиция
            # останется недобранной до конца раунда без всякой причины.
            # Навсегда прекращаем только тогда, когда ИЗМЕРЕННЫЙ запас
            # действительно упал ниже порога входа.
            if rep.z is None or rep.z >= self.p.z_min:
                return None
            st.stopped = True
            return None
        if rep.score > limit:
            st.stopped = True
            return None
        # Запас не имеет права уменьшаться между ступенями.
        if st.last_z is not None and rep.z is not None:
            if rep.z < st.last_z - c.cert_z_slip:
                st.stopped = True
                return None
        return self._step_action(s, st.side, rep, step=st.steps)

    def _step_action(self, s: Snapshot, side: str, rep: risk.RiskReport,
                     step: int) -> Optional[Action]:
        c = self.cfg
        ask = s.ask(side)
        if ask is None:
            return None
        size = c.cert_steps[step]
        # Потолок раунда — последняя защита от арифметической ошибки в
        # размерах ступеней.
        if self.net_out + size > c.cert_full_size + 1e-9:
            size = max(0.0, c.cert_full_size - self.net_out)
        if size <= 0:
            return None

        # СОСТОЯНИЕ ЗДЕСЬ НЕ МЕНЯЕТСЯ. Между решением и филлом ордер может не
        # уйти вовсе: движок занят предыдущим, идёт пауза после отказа биржи,
        # ask ушёл за пинг, биржа отвергла размер. Если считать ступень взятой
        # уже здесь, лестница «израсходует» все четыре ступени, не купив
        # ничего, и позиция навсегда останется неполной. Ступень засчитывает
        # record_entry — его движок зовёт только по подтверждённому филлу.
        n = step + 1
        feat = {
            "risk_score": round(rep.score, 1),
            "z": round(rep.z, 2) if rep.z is not None else None,
            "p_model": round(rep.p_model, 6) if rep.p_model else None,
            "edge_cents": (round(rep.edge_cents, 2)
                           if rep.edge_cents is not None else None),
            "buffer_usd": (round(rep.buffer_usd, 1)
                           if rep.buffer_usd is not None else None),
            "step": n,
            "secs_left": round(s.seconds_left, 1),
            "sigma": round(self._m.sigma, 3) if self._m and self._m.sigma else None,
            "max_jump_1s": (round(self._m.max_jump_1s, 1)
                            if self._m and self._m.max_jump_1s else None),
        }
        for f in rep.factors:
            feat[f"r_{f.name}"] = round(f.points, 1)

        return Action(
            BUY, outcome=side, limit_price=round(ask, 2), size_usdc=size,
            feat=feat,
            reason=(f"СТУПЕНЬ {n}/{len(c.cert_steps)} ${size:.0f} "
                    f"{side} @ {ask:.3f} | {rep.summary()} | "
                    + " ".join(f"{f.name}:{f.points:.0f}" for f in rep.factors
                               if f.points > 0)))

    def record_entry(self, outcome: str, price: float, shares: float,
                     cost: float, t: float,
                     entry_bid: Optional[float] = None,
                     feat: Optional[Dict] = None) -> Leg:
        """Филл подтверждён — вот теперь ступень считается взятой."""
        leg = super().record_entry(outcome, price, shares, cost, t,
                                   entry_bid=entry_bid, feat=feat)
        st = self.round
        st.side = outcome
        st.steps += 1
        st.last_step_t = t
        z = (feat or {}).get("z")
        if z is not None:
            st.last_z = z
        return leg

    # ======================================================================
    #  ВЫХОД
    # ======================================================================
    def should_exit(self, s: Snapshot, leg: Leg) -> Optional[Action]:
        """Аварийный выход — только по ПОДТВЕРЖДЁННОМУ развороту.

        Процент сам по себе поводом не является: он проваливается до 0.50 и
        возвращается на 0.98 в пределах секунды, и выход по нему означал бы
        фиксировать убыток на каждом шуме. Смотрим на состояние РЫНКА:
        запас в сигмах и запас против скачка. И требуем, чтобы тревога
        держалась несколько тактов подряд и не меньше заданного времени —
        одно измерение не событие.
        """
        c, st = self.cfg, self.round
        side = leg.outcome

        # У конца окна выходить некуда: книга тонкая, а держать до расчёта
        # при большом запасе безопаснее, чем отдавать позицию по любой цене.
        if s.seconds_left <= c.cert_no_exit_last_s:
            st.alarm_ticks = 0
            return None

        # Считаем запас НАПРЯМУЮ, а не через assess: у выхода не должно быть
        # зависимости от книги. assess возвращается раньше, если нет ask, — а
        # именно в такую минуту продавать и приходится.
        m = self._metrics(s)
        buf, z, _sig_t = risk.buffer_z(
            side=side, price=s.coin_price, target=s.target,
            seconds_left=s.seconds_left, sigma=m.sigma)

        danger: List[str] = []
        if z is not None and z < c.cert_exit_z:
            danger.append(f"запас {z:.1f}σ < {c.cert_exit_z:.1f}σ")
        if (buf is not None and m.max_jump_1s
                and buf < m.max_jump_1s * c.cert_exit_jump_mult):
            danger.append(f"запас ${buf:.0f} < "
                          f"{c.cert_exit_jump_mult:.1f}× худшей секунды "
                          f"(${m.max_jump_1s:.0f})")

        if not danger:
            st.alarm_ticks = 0
            st.alarm_since = None
            st.alarm_last_t = None
            return None

        # Тревога есть. Считаем подтверждения — РОВНО ОДНО НА ТАКТ. Ног может
        # быть до четырёх, и should_exit зовётся по каждой: без этой проверки
        # «пять тактов подряд» набирались бы за два такта.
        if st.alarm_since is None:
            st.alarm_since = s.t
        if st.alarm_last_t != s.t:
            st.alarm_last_t = s.t
            st.alarm_ticks += 1
        held_s = s.t - st.alarm_since
        if (st.alarm_ticks < c.cert_exit_confirm_ticks
                or held_s < c.cert_exit_confirm_s):
            return None

        bid = s.bid(side)
        if bid is None:
            return None            # продавать не по чему — держим до расчёта

        st.stopped = True
        return Action(
            SELL, sell_idx=leg.idx, sell_outcome=side,
            limit_price=round(bid, 2),
            reason=(f"АВАРИЙНЫЙ ВЫХОД {side} @ {bid:.3f}: "
                    + "; ".join(danger)
                    + f" — подтверждено {st.alarm_ticks} тактов за "
                      f"{held_s:.1f}с"))
