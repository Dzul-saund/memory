"""Ядро стратегии flowbot — ЧИСТАЯ логика входа/удержания/выхода, без I/O.

Ровно то, что описал пользователь:

  ВХОД. Если по цене BTC (быстрый слой бирж) произошло РЕЗКОЕ ДВИЖЕНИЕ и
  «проценты» Up/Down ещё могут измениться в нашу сторону — покупаем ту
  сторону, КУДА пошла цена (вверх => Up, вниз => Down), по текущему ask.

  Предосторожность №1 (не успели по доскачковому проценту). Если книга
  уже поехала (например, Down был 56, а сейчас дороже), догоняем, но не
  дороже, чем ref + 1-2 цента. Ушло дальше — вход пропускаем.

  УДЕРЖАНИЕ. Держим позицию, пока (а) «процент» купленной стороны растёт и
  (б) движение цены BTC, из-за которого мы вошли, ПРОДОЛЖАЕТ идти туда же.
  Как только одно из двух ломается (и это подтверждается grace-периодом) —
  выходим.

  Предосторожность №2 (резкий разворот). Если после покупки цена резко и
  СИЛЬНО пошла в противоположную сторону — сразу берём противоположную
  сторону на $3-4, а свою продаём как можно раньше.

Модуль не знает ни про сеть, ни про трейдера: он принимает снимок рынка и
возвращает действие. Реальные исполнения (с задержкой из-за пинга) делает
движок и сообщает сюда через record_entry/record_exit. Благодаря этому вся
логика решений покрывается юнит-тестами без сети и без ключей.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Виды действий
ENTER = "enter"     # купить сторону outcome по limit_price на size_usdc
EXIT = "exit"       # продать текущую позицию (sell_outcome) по sell_limit
FLIP = "flip"       # продать текущую и купить противоположную (разворот)
HOLD = "hold"       # держать
NONE = "none"       # ничего не делать (позиции нет)


@dataclass
class MarketSnapshot:
    """Снимок рынка на один такт (всё, что нужно для решения)."""
    t: float                         # монотонное время, сек
    seconds_left: float              # до конца 5-минутного окна
    btc_price: Optional[float]
    burst_z: float                   # знаковый всплеск скорости в σ (+вверх/-вниз)
    move_bps: float                  # знаковое движение за горизонт, б.п.
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    up_ask_ref: Optional[float] = None    # ask Up «до скачка» (lookback назад)
    down_ask_ref: Optional[float] = None
    up_flow: float = 0.0             # имбаланс потока Up в [-1,1] (>0 = давят вверх)
    down_flow: float = 0.0


@dataclass
class Action:
    kind: str
    reason: str = ""
    outcome: Optional[str] = None        # что КУПИТЬ (enter/flip)
    limit_price: Optional[float] = None  # потолок покупки / (для sell) пол продажи
    size_usdc: Optional[float] = None
    sell_outcome: Optional[str] = None   # что ПРОДАТЬ (exit/flip)
    sell_limit: Optional[float] = None


@dataclass
class Position:
    outcome: str                     # "Up" | "Down"
    entry_price: float
    size_usdc: float
    entry_t: float
    peak_bid: float                  # максимум «процента» с момента входа
    fade_since: Optional[float] = None
    is_flip: bool = False


def _fav_dir(outcome: str) -> float:
    """Направление движения BTC, ВЫГОДНОЕ для стороны: Up->+1, Down->-1."""
    return 1.0 if outcome == "Up" else -1.0


class FlowStrategy:
    """Стейт-машина позиции. Чистая: без сети, без трейдера."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.position: Optional[Position] = None

    # -- колбэки исполнения (зовёт движок по факту филла) ---------------------
    def record_entry(self, outcome: str, price: float, size_usdc: float,
                     t: float, is_flip: bool = False) -> None:
        self.position = Position(
            outcome=outcome, entry_price=price, size_usdc=size_usdc,
            entry_t=t, peak_bid=price, is_flip=is_flip,
        )

    def record_exit(self) -> None:
        self.position = None

    # -- основной такт ---------------------------------------------------------
    def on_tick(self, s: MarketSnapshot) -> Action:
        if self.position is None:
            return self._maybe_enter(s)
        return self._manage(s)

    # -- вход ------------------------------------------------------------------
    def _maybe_enter(self, s: MarketSnapshot) -> Action:
        c = self.cfg
        if s.btc_price is None:
            return Action(NONE, "нет цены BTC")
        if s.seconds_left <= c.settle_hold_s:
            return Action(NONE, f"конец окна {s.seconds_left:.0f}s — "
                                f"новые входы не открываю")

        sharp = (abs(s.burst_z) >= c.entry_burst_z
                 and abs(s.move_bps) >= c.entry_min_move_bps)
        if not sharp:
            return Action(NONE, f"нет резкого движения (z={s.burst_z:+.2f}<"
                                f"{c.entry_burst_z:.2f} или move="
                                f"{s.move_bps:+.2f}б.п.)")

        up = s.burst_z > 0
        if up:
            side, ask, ref, flow = "Up", s.up_ask, s.up_ask_ref, s.up_flow
        else:
            side, ask, ref, flow = "Down", s.down_ask, s.down_ask_ref, s.down_flow

        if ask is None:
            return Action(NONE, f"{side}: нет ask в книге")
        if not (c.entry_price_min <= ask <= c.entry_price_max):
            return Action(NONE, f"{side} ask {ask:.2f} вне диапазона входа "
                                f"[{c.entry_price_min:.2f},{c.entry_price_max:.2f}]")

        # Предосторожность №1: догоняем не дороже, чем доскачковый ask + chase.
        limit = ask
        if ref is not None:
            cap = ref + c.chase_cents
            if ask > cap + 1e-9:
                return Action(NONE, f"{side}: книга ушла — ask {ask:.2f} > "
                                    f"доскачкового {ref:.2f}+{c.chase_cents:.2f} "
                                    f"(вход пропущен)")
            limit = cap   # готовы платить до cap; FAK исполнит по ask (<= cap)

        # Мягкое вето по потоку заявок: не лезем против сильного давления.
        if c.flow_confirm and flow < c.flow_veto:
            return Action(NONE, f"{side}: поток заявок против нас "
                                f"({flow:+.2f} < {c.flow_veto:+.2f})")

        return Action(
            ENTER, outcome=side, limit_price=round(limit, 2),
            size_usdc=c.stake_usdc,
            reason=(f"резкое {'ВВЕРХ' if up else 'ВНИЗ'} z={s.burst_z:+.2f} "
                    f"({s.move_bps:+.2f}б.п.); {side} ask {ask:.2f} лимит "
                    f"{limit:.2f}, поток {flow:+.2f}"),
        )

    # -- управление открытой позицией -----------------------------------------
    def _manage(self, s: MarketSnapshot) -> Action:
        c = self.cfg
        pos = self.position
        assert pos is not None
        fav = _fav_dir(pos.outcome)

        if pos.outcome == "Up":
            bid, opp, opp_ask = s.up_bid, "Down", s.down_ask
        else:
            bid, opp, opp_ask = s.down_bid, "Up", s.up_ask

        if bid is not None and bid > pos.peak_bid:
            pos.peak_bid = bid
        held = s.t - pos.entry_t
        z_fav = s.burst_z * fav          # >0 = движение в нашу сторону

        # Предосторожность №2 — разворот: сильный всплеск ПРОТИВ нас.
        if (c.flip_enabled and held >= c.min_hold_s
                and z_fav <= -c.flip_burst_z
                and abs(s.move_bps) >= c.flip_min_move_bps):
            return Action(
                FLIP, outcome=opp,
                limit_price=(round(opp_ask + c.chase_cents, 2)
                             if opp_ask is not None else None),
                size_usdc=c.flip_size_usdc,
                sell_outcome=pos.outcome,
                sell_limit=(round(bid, 2) if bid is not None else None),
                reason=(f"РАЗВОРОТ: сильное движение против {pos.outcome} "
                        f"(z={s.burst_z:+.2f}, {s.move_bps:+.2f}б.п.); беру "
                        f"{opp} на ${c.flip_size_usdc:.1f}, {pos.outcome} продаю"),
            )

        # Не дёргаемся первые доли секунды (пинг мог дать шумный первый тик).
        if held < c.min_hold_s:
            return Action(HOLD, f"мин. удержание {held:.2f}<{c.min_hold_s:.2f}s")

        # Глубоко в деньгах — едем до расчёта (сеттл платит $1/шт).
        if bid is not None and bid >= c.take_profit_price:
            pos.fade_since = None
            return Action(HOLD, f"глубоко в деньгах ({pos.outcome} bid {bid:.2f})"
                                f" — держу до расчёта")

        # Конец окна: выигрываем — держим до сеттла; проигрываем — спасаем кэш.
        if s.seconds_left <= c.settle_hold_s:
            if bid is not None and bid >= 0.5:
                return Action(HOLD, f"конец окна {s.seconds_left:.0f}s, "
                                    f"{pos.outcome} bid {bid:.2f}>=0.50 — держу")
            return self._exit(pos, bid, f"конец окна {s.seconds_left:.0f}s и "
                                        f"проигрываем — выхожу спасать деньги")

        # Движение ещё идёт И «процент» ещё растёт?
        mom_ok = z_fav >= c.hold_burst_z
        token_ok = bid is not None and bid >= (pos.peak_bid - c.token_retrace_exit)
        if mom_ok and token_ok:
            pos.fade_since = None
            return Action(HOLD, f"движение идёт (z*dir={z_fav:+.2f}) и % растёт "
                                f"({pos.outcome} bid {_p(bid)} пик "
                                f"{pos.peak_bid:.2f})")

        # Фейд: подтверждаем grace-периодом, чтобы не выйти на одном тике.
        if pos.fade_since is None:
            pos.fade_since = s.t
        if s.t - pos.fade_since < c.momentum_fade_grace_s:
            return Action(HOLD, f"фейд {s.t - pos.fade_since:.2f}<"
                                f"{c.momentum_fade_grace_s:.2f}s — жду подтверждения")

        why = []
        if not mom_ok:
            why.append(f"движение выдохлось (z*dir={z_fav:+.2f}<{c.hold_burst_z:.2f})")
        if not token_ok:
            why.append(f"% откатился ({pos.outcome} bid {_p(bid)} < пик "
                       f"{pos.peak_bid:.2f}-{c.token_retrace_exit:.2f})")
        return self._exit(pos, bid, "; ".join(why))

    def _exit(self, pos: Position, bid: Optional[float], reason: str) -> Action:
        return Action(EXIT, sell_outcome=pos.outcome,
                      sell_limit=(round(bid, 2) if bid is not None else None),
                      reason=reason)


def _p(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.2f}"
