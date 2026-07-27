"""Скачковая стратегия (4-я система) — ЧИСТАЯ логика, без сети и без ключей.

Правила ровно те, что описал юзер, только пороги здесь в ДОЛЛАРАХ, а не в σ:

  ВХОД. Смотрим на резкий скачок цены монеты за последние `jump_window_s`
  секунд и покупаем ТУ сторону, куда скакнула цена.

    * скачок >= $5  — если «процент» этой стороны >= 51¢ (дорожка «A»);
    * скачок >= $15 — если «процент» ниже 51¢ (дорожка «B»), и только пока
      цена монеты не дальше $100 от таргета раунда: на дешёвой стороне
      далеко от таргета скачок уже ничего не решает.

  ЛЕСТНИЦА (обе дорожки). Пока «процент» купленной стороны держится выше
  51¢ — держим до расчёта. Как только наша ставка проваливается (см. ниже),
  берём ПРОТИВОПОЛОЖНУЮ сторону в таком размере, чтобы перекрыть весь
  вложенный минус и выйти в плюс, и так по кругу:

        шэров = (вложено_за_раунд + желаемый_плюс) / (1 − цена входа)

  потому что выигравший шэр платит ровно $1. Старые ноги не продаём — они
  ничего больше не стоят нам и остаются бесплатным лотерейным билетом.

  ФИКСАЦИЯ ПРИБЫЛИ (только дорожка «B»). «Купил по 0.05, выросли до 0.30,
  а до конца 5 секунд или рост кончился и люди смотрят в другую сторону —
  продаёт». Работает, только когда мы уже в плюсе.

Модуль ничего не знает про сеть: принимает снимок рынка и возвращает
действие. Факт исполнения ему сообщает движок (record_entry/record_sell),
поэтому вся логика решений покрыта тестами без сети и без денег.

┌─ ДВЕ ТРАКТОВКИ, КОТОРЫЕ ПРИШЛОСЬ ЗАФИКСИРОВАТЬ ────────────────────────┐
│ 1. «Наша ставка стоит меньше 51 цента» не может быть условием лестницы  │
│    буквально для дорожки B: там мы и ВХОДИМ ниже 51 (0.05), и ждём      │
│    роста. Поэтому лестница ждёт РЕАЛЬНОГО убытка: процент и ниже        │
│    сплита, и ниже цены нашего входа (`jump_ladder_loss`). Для дорожки A │
│    это то же самое, что просил юзер: вошли выше 51 => падение ниже 51   │
│    и есть убыток.                                                       │
│ 2. Второе правило дорожки B в задании не дошло («только 2 правила: 1.   │
│    …»), поэтому реализовано только первое. Точка расширения — метод     │
│    `_take_profit`.                                                      │
└─────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Виды действий
ENTER = "enter"     # первый вход в раунде
LADDER = "ladder"   # добор противоположной стороны, перекрывающий минус
SELL = "sell"       # продать конкретную ногу (фиксация прибыли)
HOLD = "hold"       # позиция есть, ничего не делаем
NONE = "none"       # позиции нет, входа тоже нет

TRACK_A = "A"       # дорогая сторона (>= 51¢), скачок от $5
TRACK_B = "B"       # дешёвая сторона (< 51¢), скачок от $15


@dataclass
class JumpSnapshot:
    """Снимок рынка на один такт: всё, что нужно для решения."""
    t: float                          # монотонное время, сек
    seconds_left: float               # до конца 5-минутного окна
    coin_price: Optional[float]       # живая цена монеты (консенсус бирж)
    target: Optional[float]           # openPrice раунда (как на сайте)
    jump_usd: float                   # знаковый скачок за окно, $ (+вверх)
    jump_bps: float = 0.0
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    up_flow: float = 0.0              # имбаланс потока Up в [-1,1]
    down_flow: float = 0.0

    def bid(self, outcome: str) -> Optional[float]:
        return self.up_bid if outcome == "Up" else self.down_bid

    def ask(self, outcome: str) -> Optional[float]:
        return self.up_ask if outcome == "Up" else self.down_ask

    def flow(self, outcome: str) -> float:
        return self.up_flow if outcome == "Up" else self.down_flow


@dataclass
class Leg:
    """Одна купленная нога лестницы."""
    outcome: str                      # "Up" | "Down"
    entry_price: float
    shares: float
    cost: float
    track: str                        # TRACK_A | TRACK_B
    entry_t: float
    peak_bid: float
    idx: int                          # порядковый номер в лестнице (0 — вход)


@dataclass
class Action:
    kind: str
    reason: str = ""
    outcome: Optional[str] = None         # что купить (enter/ladder)
    limit_price: Optional[float] = None   # потолок покупки / пол продажи
    size_usdc: Optional[float] = None
    shares: Optional[float] = None        # для добора считаем шэры напрямую
    track: Optional[str] = None
    sell_idx: Optional[int] = None        # какую ногу продать (sell)
    sell_outcome: Optional[str] = None


def opposite(outcome: str) -> str:
    return "Down" if outcome == "Up" else "Up"


def favour(outcome: str) -> float:
    """Направление движения монеты, ВЫГОДНОЕ стороне: Up->+1, Down->-1."""
    return 1.0 if outcome == "Up" else -1.0


def ladder_shares(debt_usdc: float, profit_usdc: float, price: float) -> float:
    """Сколько шэров надо взять, чтобы перекрыть минус и выйти в плюс.

    Выигравший шэр платит $1, поэтому чтобы после победы этой ноги остаться
    в плюсе на `profit` при уже вложенных `debt`:

        shares * 1 >= debt + shares * price + profit
        shares     >= (debt + profit) / (1 - price)

    Цена >= 1 невозможна (тогда шэров нужно бесконечно) — вызывающий обязан
    отсечь такую цену заранее; здесь просто возвращаем 0.
    """
    if price >= 1.0 or price < 0.0:
        return 0.0
    return (max(0.0, debt_usdc) + max(0.0, profit_usdc)) / (1.0 - price)


class JumpStrategy:
    """Стейт-машина лестницы на один 5-минутный раунд. Чистая: без I/O."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.legs: List[Leg] = []
        # Чистый отток кэша за раунд: куплено − продано. Именно его надо
        # перекрыть добором (если он положительный).
        self.net_out = 0.0
        self.depth = 0                       # сколько доборов уже сделали
        self._below_since: Optional[float] = None
        self._last_fill_t: Optional[float] = None

    # -- границы раунда --------------------------------------------------------
    def reset_round(self) -> None:
        """Новое 5-минутное окно: лестница начинается с нуля."""
        self.legs = []
        self.net_out = 0.0
        self.depth = 0
        self._below_since = None
        self._last_fill_t = None

    # -- колбэки исполнения (зовёт движок по факту филла) ---------------------
    def record_entry(self, outcome: str, price: float, shares: float,
                     cost: float, track: str, t: float) -> Leg:
        leg = Leg(outcome=outcome, entry_price=price, shares=shares, cost=cost,
                  track=track, entry_t=t, peak_bid=price, idx=len(self.legs))
        self.legs.append(leg)
        self.net_out += cost
        if len(self.legs) > 1:
            self.depth += 1
        self._below_since = None
        self._last_fill_t = t
        return leg

    def record_sell(self, idx: int, proceeds: float, t: float) -> None:
        self.legs = [lg for lg in self.legs if lg.idx != idx]
        self.net_out -= proceeds
        self._below_since = None
        self._last_fill_t = t
        if not self.legs and self.net_out <= 0.0:
            # Вышли из раунда в плюсе — долга нет, лестница начинается заново.
            self.depth = 0

    @property
    def debt(self) -> float:
        """Сколько кэша надо отбить (0, если раунд уже в плюсе)."""
        return max(0.0, self.net_out)

    # -- основной такт ---------------------------------------------------------
    def on_tick(self, s: JumpSnapshot) -> Action:
        self._track_peaks(s)
        if not self.legs:
            return self._maybe_enter(s)
        act = self._take_profit(s)
        if act is not None:
            return act
        return self._maybe_ladder(s)

    def _track_peaks(self, s: JumpSnapshot) -> None:
        for leg in self.legs:
            b = s.bid(leg.outcome)
            if b is not None and b > leg.peak_bid:
                leg.peak_bid = b

    # ======================================================================
    #  Вход
    # ======================================================================
    def _horizon(self) -> str:
        """Как подписывать движение в логе — зависит от режима детекции."""
        c = self.cfg
        if getattr(c, "jump_trigger_mode", "swing") == "swing":
            return "от экстремума"
        return f"за {c.jump_window_s:.0f}с"

    def _maybe_enter(self, s: JumpSnapshot) -> Action:
        c = self.cfg
        if s.coin_price is None:
            return Action(NONE, "нет цены монеты")
        if s.seconds_left <= c.settle_hold_s:
            return Action(NONE, f"конец окна ({s.seconds_left:.0f}с) — "
                                f"новых входов не открываю")
        cooldown = c.jump_reentry_cooldown_s
        if (self._last_fill_t is not None
                and s.t - self._last_fill_t < cooldown):
            return Action(NONE, f"пауза после сделки "
                                f"({s.t - self._last_fill_t:.1f}<"
                                f"{cooldown:.1f}с) — жду новый скачок")

        jump = s.jump_usd
        if abs(jump) < c.jump_small_usd:
            return Action(NONE, f"движение ${jump:+.2f} {self._horizon()} "
                                f"< ${c.jump_small_usd:.0f} — жду")

        side = "Up" if jump > 0 else "Down"
        ask = s.ask(side)
        if ask is None:
            return Action(NONE, f"{side}: нет ask в книге")

        # Дорогая сторона (>= сплита) — хватает малого скачка; дешёвая
        # требует большого И близости к таргету.
        if ask >= c.jump_price_split:
            track, need = TRACK_A, c.jump_small_usd
        else:
            track, need = TRACK_B, c.jump_big_usd

        if abs(jump) < need:
            return Action(NONE, (
                f"{side} ask {ask:.2f} < {c.jump_price_split:.2f} — дешёвой "
                f"стороне нужен скачок ${need:.0f}, а он ${jump:+.2f}"))

        if track == TRACK_B:
            if s.target is None:
                return Action(NONE, f"{side} дешёвая ({ask:.2f}), но таргет "
                                    f"раунда ещё не известен — не вхожу")
            dist = abs(s.coin_price - s.target)
            if dist > c.jump_max_target_dist_usd:
                return Action(NONE, (
                    f"{side} дешёвая ({ask:.2f}): до таргета ${dist:,.0f} > "
                    f"${c.jump_max_target_dist_usd:,.0f} — скачок не спасёт"))

        if ask > c.jump_max_leg_price:
            return Action(NONE, f"{side} ask {ask:.2f} > потолка "
                                f"{c.jump_max_leg_price:.2f} — нечего забирать")

        stake = min(c.jump_stake_usdc, c.jump_max_round_usdc)
        return Action(
            ENTER, outcome=side, limit_price=round(ask, 2), size_usdc=stake,
            track=track,
            reason=(f"СКАЧОК ${jump:+.2f} {self._horizon()} "
                    f"({'вверх' if jump > 0 else 'вниз'}) → {side} @ {ask:.2f} "
                    f"[дорожка {track}: порог ${need:.0f}], ставка ${stake:.2f}"),
        )

    # ======================================================================
    #  Фиксация прибыли — только дешёвая дорожка B
    # ======================================================================
    def _take_profit(self, s: JumpSnapshot) -> Optional[Action]:
        c = self.cfg
        if not c.jump_tp_enabled:
            return None
        for leg in self.legs:
            if leg.track != TRACK_B:
                continue
            bid = s.bid(leg.outcome)
            if bid is None:
                continue
            gain = bid - leg.entry_price
            if gain < c.jump_tp_min_gain:
                continue                      # ещё не в плюсе — не о чем говорить

            deadline = s.seconds_left <= c.jump_tp_deadline_s
            # «цена перестала расти»: скачок больше не идёт в нашу сторону.
            mom_fav = s.jump_usd * favour(leg.outcome)
            retraced = bid <= leg.peak_bid - c.jump_tp_stall_retrace
            flow_bad = s.flow(leg.outcome) <= c.jump_tp_flow_against
            stalled = mom_fav <= 0 and (retraced or flow_bad)

            if not (deadline or stalled):
                continue
            why = (f"до конца {s.seconds_left:.0f}с" if deadline else
                   f"рост кончился (движение {mom_fav:+.1f}$"
                   + (f", % откатился с {leg.peak_bid:.2f}" if retraced else "")
                   + (f", поток против {s.flow(leg.outcome):+.2f}"
                      if flow_bad else "") + ")")
            return Action(
                SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
                limit_price=round(bid, 2),
                reason=(f"ФИКСИРУЮ {leg.outcome}: вошли {leg.entry_price:.2f} → "
                        f"сейчас {bid:.2f} (+{gain:.2f}), {why}"),
            )
        return None

    # ======================================================================
    #  Лестница: добор противоположной стороны
    # ======================================================================
    def _maybe_ladder(self, s: JumpSnapshot) -> Action:
        c = self.cfg
        leg = self.legs[-1]                   # живая ставка — самая свежая нога
        bid = s.bid(leg.outcome)
        if bid is None:
            return Action(HOLD, f"{leg.outcome}: нет bid — держу")

        # Провалилась ли ставка? Нужны ОБА условия: ниже сплита и в убытке
        # относительно входа (иначе дешёвая нога, купленная по 0.05, считалась
        # бы «провалившейся» с первой же секунды — см. шапку модуля).
        under_split = bid < c.jump_price_split
        losing = bid <= leg.entry_price - c.jump_ladder_loss
        if not (under_split and losing):
            self._below_since = None
            return Action(HOLD, (
                f"{leg.outcome} держится {bid:.2f} "
                f"(вход {leg.entry_price:.2f}, сплит {c.jump_price_split:.2f}) "
                f"— веду до расчёта"))

        if self._below_since is None:
            self._below_since = s.t
        waited = s.t - self._below_since
        if waited < c.jump_ladder_grace_s:
            return Action(HOLD, f"{leg.outcome} просел до {bid:.2f}, жду "
                                f"подтверждения {waited:.1f}/"
                                f"{c.jump_ladder_grace_s:.1f}с")

        if not c.jump_ladder_enabled:
            return Action(HOLD, f"{leg.outcome} просел до {bid:.2f}, лестница "
                                f"выключена — держу до расчёта")
        if self.depth >= c.jump_max_ladder_legs:
            return Action(HOLD, (
                f"{leg.outcome} просел до {bid:.2f}, но лестница на пределе "
                f"({self.depth}/{c.jump_max_ladder_legs}) — держу до расчёта"))

        opp = opposite(leg.outcome)
        opp_ask = s.ask(opp)
        if opp_ask is None:
            return Action(HOLD, f"{opp}: нет ask для добора")
        if opp_ask > c.jump_max_leg_price:
            return Action(HOLD, (
                f"добор {opp} по {opp_ask:.2f} дороже потолка "
                f"{c.jump_max_leg_price:.2f} — шэров нужно слишком много"))

        shares = ladder_shares(self.debt, c.jump_ladder_profit_usdc, opp_ask)
        cost = shares * opp_ask
        if shares <= 0:
            return Action(HOLD, f"добор {opp}: расчёт дал 0 шэров")
        if self.net_out + cost > c.jump_max_round_usdc:
            return Action(HOLD, (
                f"добор {opp} стоил бы ${cost:.2f}, вложено ${self.net_out:.2f} "
                f"— вышли бы за потолок раунда ${c.jump_max_round_usdc:.2f}, "
                f"держу до расчёта"))

        return Action(
            LADDER, outcome=opp, limit_price=round(opp_ask, 2),
            shares=shares, size_usdc=cost, track=leg.track,
            reason=(f"ЛЕСТНИЦА #{self.depth + 1}: {leg.outcome} упал до "
                    f"{bid:.2f} (вход {leg.entry_price:.2f}) → беру {opp} "
                    f"{shares:.2f} шэр @ {opp_ask:.2f} = ${cost:.2f}, чтобы "
                    f"перекрыть ${self.debt:.2f} и выйти "
                    f"+${c.jump_ladder_profit_usdc:.2f}"),
        )
