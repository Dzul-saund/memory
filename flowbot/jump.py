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

┌─ ТРАКТОВКА, КОТОРУЮ ПРИШЛОСЬ ЗАФИКСИРОВАТЬ ────────────────────────────┐
│ «Наша ставка стоит меньше 51 цента» не может быть условием лестницы     │
│ буквально для дорожки B: там мы и ВХОДИМ ниже 51 (0.05), и ждём роста.  │
│ Поэтому лестница ждёт РЕАЛЬНОГО убытка: процент и ниже сплита, и ниже   │
│ цены нашего входа (`jump_ladder_loss`). Для дорожки A это то же самое,  │
│ что просил юзер: вошли выше 51 => падение ниже 51 и есть убыток.        │
└─────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from btc_bot.prob import expected_shift, fair_up_probability
from btc_bot.util import floor2

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
    sigma_1s: Optional[float] = None  # волатильность, $ за корень секунды
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    up_flow: float = 0.0              # имбаланс потока Up в [-1,1]
    down_flow: float = 0.0
    # Цена, по которой считает САМ Polymarket (якорь Chainlink с их потока).
    # В fast_monitor это якорь уровня: остальные биржи дебиасятся относительно
    # него, поэтому разрыв «coin_price − pm_price» — чистое опережение, без
    # постоянного базиса между площадками.
    pm_price: Optional[float] = None
    pm_age_ms: Optional[float] = None
    # --- качество импульса (режим "impulse"; считает flowbot/impulse.py) ---
    speed: Optional[float] = None     # $/с за последнюю секунду, знаковая
    accel: Optional[float] = None     # >1 разгон, <1 затухание, <=0 разворот
    imp_age_s: Optional[float] = None # возраст локального экстремума, с
    imp_hold: Optional[float] = None  # доля удержанного хода [0..1]
    # --- структура книги (flowbot/bookfeat.py), по каждой стороне отдельно ---
    up_imb: Optional[float] = None    # перекос глубины [−1..+1], + = биды
    down_imb: Optional[float] = None
    up_wall: Optional[float] = None   # доля крупнейшей заявки в ask, [0..1]
    down_wall: Optional[float] = None
    up_ask_trend: Optional[float] = None   # уходит ли ask-ликвидность
    down_ask_trend: Optional[float] = None
    # Движок сейчас не пропустит покупку (пауза после отказа биржи). Стратегии
    # это знать ОБЯЗАТЕЛЬНО: с приоритетом лестницы стоп стоит в стороне,
    # пока ногу способен взять разворот, — а разворот это покупка. Без этого
    # флага пауза давала дыру: разворот отложен, стоп молчит «его возьмут»,
    # и нога висит без выхода ровно тогда, когда цена падает.
    entries_paused: bool = False

    def bid(self, outcome: str) -> Optional[float]:
        return self.up_bid if outcome == "Up" else self.down_bid

    def ask(self, outcome: str) -> Optional[float]:
        return self.up_ask if outcome == "Up" else self.down_ask

    def flow(self, outcome: str) -> float:
        return self.up_flow if outcome == "Up" else self.down_flow

    def imbalance(self, outcome: str) -> Optional[float]:
        return self.up_imb if outcome == "Up" else self.down_imb

    def wall(self, outcome: str) -> Optional[float]:
        return self.up_wall if outcome == "Up" else self.down_wall

    def ask_trend(self, outcome: str) -> Optional[float]:
        return self.up_ask_trend if outcome == "Up" else self.down_ask_trend


@dataclass
class Leg:
    """Одна купленная нога лестницы."""
    outcome: str                      # "Up" | "Down"
    entry_price: float                # уплаченный ask
    shares: float
    cost: float
    track: str                        # TRACK_A | TRACK_B
    entry_t: float
    peak_bid: float
    idx: int                          # порядковый номер в лестнице (0 — вход)
    # Бид в момент входа. Спред между ним и уплаченным ask — стоимость входа,
    # а не движение рынка против нас, поэтому развороты считаются от него.
    entry_bid: float = 0.0
    peak_t: float = 0.0               # когда пик обновлялся в последний раз
    against_since: Optional[float] = None
    # Пик скорости монеты В НАШУ сторону за время позиции. По нему выход
    # «импульс умер»: трейлинг реагирует только после отката процента, а
    # скорость умирает раньше отката.
    peak_speed: float = 0.0
    # Признаки сигнала В МОМЕНТ ВХОДА. Едут с ногой до самого закрытия и
    # оттуда попадают в журнал: без них по истории нельзя ответить, какой
    # фильтр реально влияет на прибыль, а это единственное, ради чего
    # история и собирается.
    feat: Dict = field(default_factory=dict)


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
    feat: Dict = field(default_factory=dict)   # признаки сигнала (для журнала)


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
        # До какого момента вход считается ПОВТОРНЫМ (после выхода в минус) и
        # обязан пройти повышенную планку качества. None = обычные правила.
        self._strict_until: Optional[float] = None

    # -- границы раунда --------------------------------------------------------
    def reset_round(self) -> None:
        """Новое 5-минутное окно: лестница начинается с нуля."""
        self.legs = []
        self.net_out = 0.0
        self.depth = 0
        self._below_since = None
        self._last_fill_t = None
        # Новый раунд — новая история. Строгая планка не должна тянуться из
        # прошлого окна: там был другой таргет и другой рынок.
        self._strict_until = None

    # -- колбэки исполнения (зовёт движок по факту филла) ---------------------
    def record_entry(self, outcome: str, price: float, shares: float,
                     cost: float, track: str, t: float,
                     entry_bid: Optional[float] = None,
                     feat: Optional[Dict] = None) -> Leg:
        # Пик ведём по БИДУ (за него реально можно продать), стартуя от бида
        # на входе. От уплаченного ask пик показывал бы весь спред как
        # мгновенный откат.
        eb = entry_bid if entry_bid is not None else price
        leg = Leg(outcome=outcome, entry_price=price, shares=shares, cost=cost,
                  track=track, entry_t=t, peak_bid=eb, idx=len(self.legs),
                  entry_bid=eb, peak_t=t, feat=dict(feat or {}))
        self.legs.append(leg)
        self.net_out += cost
        if len(self.legs) > 1:
            self.depth += 1
        self._below_since = None
        self._last_fill_t = t
        return leg

    def record_partial_sell(self, idx: int, shares_sold: float,
                            proceeds: float, t: float) -> None:
        """Продалась ЧАСТЬ ноги — уменьшаем её, а не закрываем.

        Так бывает только в бою: ордер уходит как FAK, и если в книге на
        нашей цене лежало меньше, чем мы продаём, остаток отменяется. Нога
        при этом никуда не девается — у нас на руках остались шэры, и
        забыть про них нельзя.

        Цену входа не трогаем: она осталась той же, изменилось количество.
        Стоимость пересчитываем от неё, чтобы P&L остатка считался честно.
        """
        for lg in self.legs:
            if lg.idx != idx:
                continue
            # Вниз, не round: остаток — это «сколько ещё можно продать».
            lg.shares = max(0.0, floor2(lg.shares - shares_sold))
            lg.cost = round(lg.entry_price * lg.shares, 2)
            if lg.shares <= 0:
                self.legs = [x for x in self.legs if x.idx != idx]
            break
        self.net_out -= proceeds
        self._below_since = None
        self._last_fill_t = t

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

        if self.cfg.jump_ladder_before_stop:
            # ЛЕСТНИЦА ВЫШЕ СТОПА.
            # Порядок вызовов сам по себе ничего не решает: порог стопа (1¢)
            # МЕНЬШЕ порога лестницы (2¢ + выдержка), поэтому по цене стоп
            # наступает раньше в любом случае. Приоритет даёт не очередь, а
            # то, что стоп СТОИТ В СТОРОНЕ, пока ногу способен взять разворот
            # (см. _ladder_blocked). Стоп остаётся страховкой ровно на те
            # случаи, когда развернуться не во что.
            lad = self._maybe_ladder(s)
            # LADDER — старый переворот (продать и сразу купить обратную);
            # SELL — новый разворот, который только закрывает ногу, а вход
            # обратной стороны решается заново обычными воротами.
            if lad.kind in (LADDER, SELL):
                return lad
            act = self._stop_out(s)
            return act if act is not None else lad

        act = self._stop_out(s)
        if act is not None:
            return act
        return self._maybe_ladder(s)

    def _track_peaks(self, s: JumpSnapshot) -> None:
        for leg in self.legs:
            b = s.bid(leg.outcome)
            if b is not None and b > leg.peak_bid:
                leg.peak_bid = b
                leg.peak_t = s.t      # рост продолжается — засекаем заново
            if s.speed is not None:
                spd = s.speed * favour(leg.outcome)
                if spd > leg.peak_speed:
                    leg.peak_speed = spd

    # ======================================================================
    #  Вход
    # ======================================================================
    def vol_scale(self, s: JumpSnapshot) -> float:
        """Во сколько раз поднять долларовые пороги под текущий рынок.

        Сдвиг процента от скачка J ~ J/(sigma*sqrt(t)), значит чтобы получить
        ТОТ ЖЕ эффект при вдвое более живом рынке, нужен вдвое больший скачок.
        Отсюда масштаб = sigma_сейчас / sigma_опорная, зажатый в [min, max],
        чтобы на прогреве или во время всплеска пороги не улетели.

        1.0 = подстройка выключена или волатильность ещё не измерена.
        """
        c = self.cfg
        if not getattr(c, "jump_adaptive", False):
            return 1.0
        if not s.sigma_1s or s.sigma_1s <= 0 or c.jump_sigma_ref <= 0:
            return 1.0
        return max(c.jump_scale_min,
                   min(c.jump_scale_max, s.sigma_1s / c.jump_sigma_ref))

    def max_target_distance(self, s: JumpSnapshot) -> float:
        """Предел «далеко от таргета» для дешёвой дорожки, в долларах.

        Считаем в сигмах остатка раунда (sigma*sqrt(t)) — именно на этом
        масштабе раунд ещё может перевернуться. Фиксированные доллары тут
        врут: при живом рынке $100 — это меньше сигмы (всё решаемо), при
        тихом — восемь сигм (всё решено). Пока sigma неизвестна, работает
        старый долларовый предел.
        """
        c = self.cfg
        if s.sigma_1s and s.sigma_1s > 0 and c.jump_max_target_sigmas > 0:
            return c.jump_max_target_sigmas * s.sigma_1s * math.sqrt(
                max(s.seconds_left, 0.5))
        return c.jump_max_target_dist_usd

    def _edge(self, s: JumpSnapshot, side: str,
              ask: float) -> Optional[float]:
        """Запас цены: справедливая вероятность стороны минус её ask.

        Справедливая цена берётся из той же модели, что и P(UP) в мониторе:
        P = Phi((цена − таргет)/(sigma*sqrt(t))). Для Down это 1 − P.

        Положительный запас = сторона недооценена, покупка имеет смысл.
        Отрицательный = скачок УЖЕ в цене, и мы бы платили сверх честного.
        None = нет таргета или волатильности; тогда правило не применяется.

        Оговорка: модель считает движение цены нормальным блужданием без
        сноса. У крипты хвосты толще, поэтому у самых краёв (сторона дешевле
        ~5¢) она недооценивает шанс — там на её запас полагаться нельзя.
        """
        if self.cfg.jump_min_edge_cents <= 0:
            return None
        p_up = fair_up_probability(s.coin_price, s.target, s.sigma_1s,
                                   max(s.seconds_left, 0.5))
        if p_up is None:
            return None
        fair = p_up if side == "Up" else 1.0 - p_up
        return fair - ask

    def _expected_shift(self, s: JumpSnapshot,
                        jump_usd: float) -> Optional[float]:
        """На сколько скачок сдвинет «процент», по модели блуждания.

        None = посчитать не из чего (нет таргета или волатильности); тогда
        фильтр не применяется и решают обычные пороги.
        """
        if self.cfg.jump_min_shift_cents <= 0:
            return None
        return expected_shift(s.coin_price, s.target, s.sigma_1s,
                              max(s.seconds_left, 0.5), jump_usd)

    def _horizon(self) -> str:
        """Как подписывать движение в логе — зависит от режима детекции."""
        c = self.cfg
        if getattr(c, "jump_trigger_mode", "swing") == "swing":
            return "от экстремума"
        return f"за {c.jump_window_s:.0f}с"

    def _maybe_enter(self, s: JumpSnapshot) -> Action:
        """Общие предусловия, затем триггер выбранного режима.

        Режимы различаются РОВНО тем, что считают поводом войти. Всё, что
        дальше (размер ставки, лестница, фиксация, потолки), у них общее —
        иначе сравнение трёх запусков мерило бы сразу несколько изменений.
        """
        c = self.cfg
        if s.coin_price is None:
            return Action(NONE, "нет цены монеты")
        late_ok = getattr(c, "jump_late_entry", False)
        if s.seconds_left <= c.settle_hold_s and not late_ok:
            return Action(NONE, f"конец окна ({s.seconds_left:.0f}с) — "
                                f"новых входов не открываю")
        cooldown = c.jump_reentry_cooldown_s
        if (cooldown > 0 and self._last_fill_t is not None
                and s.t - self._last_fill_t < cooldown):
            return Action(NONE, f"пауза после сделки "
                                f"({s.t - self._last_fill_t:.1f}<"
                                f"{cooldown:.1f}с) — жду сигнал заново")

        mode = getattr(c, "jump_entry_mode", "jump")
        if mode == "edge":
            picked = self._trigger_edge(s)
        elif mode == "lag":
            picked = self._trigger_lag(s)
        elif mode == "impulse":
            picked = self._trigger_impulse(s)
        else:
            picked = self._trigger_jump(s)
        if isinstance(picked, Action):
            return picked                      # отказ с объяснением
        # Триггер отдаёт (сторона, дорожка, причина), а режим impulse ещё и
        # ставку четвёртым элементом и признаки сигнала пятым.
        side, track, why = picked[0], picked[1], picked[2]
        stake_want = picked[3] if len(picked) > 3 else c.jump_stake_usdc
        feat = picked[4] if len(picked) > 4 else {}

        ask = s.ask(side)
        # Поздний вход: в последние секунды окна берём ТОЛЬКО сторону,
        # которая уже выигрывает (процент >= сплита). Дешёвую сторону в
        # конце раунда не спасёт никакой скачок.
        if s.seconds_left <= c.settle_hold_s:
            if ask is None or ask < c.jump_price_split:
                return Action(NONE, (
                    f"конец окна ({s.seconds_left:.0f}с): поздний вход "
                    f"разрешён только от {c.jump_price_split:.2f}, а "
                    f"{side} стоит {ask if ask is not None else 0:.2f}"))

        stake = min(stake_want, c.jump_max_round_usdc)
        return Action(
            ENTER, outcome=side, limit_price=round(ask, 2), size_usdc=stake,
            track=track, reason=f"{why}, ставка ${stake:.2f}", feat=feat,
        )

    # ---- режим "edge": триггер — запас цены ---------------------------------
    def _trigger_edge(self, s: JumpSnapshot):
        """Входим туда, где справедливая цена выше запрошенной.

        Это буквально EV = p − ask, единственная величина, которая отвечает
        на вопрос «зарабатываем ли мы, если досидим до расчёта». Скачок сюда
        не входит вовсе — ни как условие, ни как поправка.

        Потолок цены ноги здесь не формальность, а защита от известной дыры
        модели: у крипты хвосты толще нормальных, поэтому у самых краёв Phi
        говорит «справедливо 100¢» там, где на деле 99.5¢, и без потолка бот
        скупал бы всё по 0.98, пока одна потеря не съест полсотни выигрышей.
        """
        c = self.cfg
        fair_up = fair_up_probability(s.coin_price, s.target, s.sigma_1s,
                                      max(s.seconds_left, 0.5))
        if fair_up is None:
            return Action(NONE, "нет таргета или σ — запас не посчитать")

        best = None
        for side in ("Up", "Down"):
            ask = s.ask(side)
            if ask is None:
                continue
            fair = fair_up if side == "Up" else 1.0 - fair_up
            if best is None or fair - ask > best[1]:
                best = (side, fair - ask, ask, fair)
        if best is None:
            return Action(NONE, "нет ask ни на одной стороне")

        side, edge, ask, fair = best
        if edge * 100 < c.jump_min_edge_cents:
            return Action(NONE, (
                f"лучший запас {side} {edge*100:+.1f}¢ < "
                f"{c.jump_min_edge_cents:.1f}¢ — брать нечего"))
        if ask > c.jump_max_leg_price:
            return Action(NONE, (
                f"{side} ask {ask:.2f} > потолка {c.jump_max_leg_price:.2f} — "
                f"у краёв модель занижает хвост, запас там ненастоящий"))

        track = TRACK_A if ask >= c.jump_price_split else TRACK_B
        return side, track, (
            f"ЗАПАС {side}: справедливо {fair*100:.0f}¢, просят "
            f"{ask*100:.0f}¢ → +{edge*100:.1f}¢ [дорожка {track}]")

    # ---- режим "lag": триггер — отставание якоря Polymarket -----------------
    def _trigger_lag(self, s: JumpSnapshot):
        """Книга считает по устаревшей цене — входим до того, как догонит.

        Отличие от edge: тот говорит «книга дешевле справедливого», но не
        знает почему. Здесь причина названа и измерена — Polymarket считает
        раунд по своему якорю, а он отстал от бирж, — поэтому у сигнала есть
        и величина (на сколько центов книга переоценится), и срок годности
        (пока якорь не обновится, ~288мс).

        Запас проверяется всё равно: отставание обещает, что цена сдвинется,
        но не обещает, что мы не переплатили уже сейчас.
        """
        c = self.cfg
        if s.pm_price is None:
            return Action(NONE, "цена якоря Polymarket неизвестна")
        if (s.pm_age_ms is not None
                and s.pm_age_ms > c.jump_lag_max_age_ms):
            return Action(NONE, (
                f"якорь Polymarket молчит {s.pm_age_ms:.0f}мс > "
                f"{c.jump_lag_max_age_ms:.0f}мс — это дырка в потоке, "
                f"а не опережение"))

        t = max(s.seconds_left, 0.5)
        ours = fair_up_probability(s.coin_price, s.target, s.sigma_1s, t)
        theirs = fair_up_probability(s.pm_price, s.target, s.sigma_1s, t)
        if ours is None or theirs is None:
            return Action(NONE, "нет таргета или σ — отставание не посчитать")

        lag = ours - theirs          # > 0 => книга недооценивает Up
        gap = s.coin_price - s.pm_price
        if abs(lag) * 100 < c.jump_lag_min_cents:
            return Action(NONE, (
                f"книга переоценится лишь на {abs(lag)*100:.1f}¢ "
                f"(< {c.jump_lag_min_cents:.1f}¢), отставание ${gap:+.2f}"))

        side = "Up" if lag > 0 else "Down"
        ask = s.ask(side)
        if ask is None:
            return Action(NONE, f"{side}: нет ask в книге")
        if ask > c.jump_max_leg_price:
            return Action(NONE, (
                f"{side} ask {ask:.2f} > потолка {c.jump_max_leg_price:.2f} — "
                f"у краёв модель занижает хвост, запас там ненастоящий"))

        fair = ours if side == "Up" else 1.0 - ours
        edge = fair - ask
        if edge * 100 < c.jump_min_edge_cents:
            return Action(NONE, (
                f"{side}: отставание {abs(lag)*100:.1f}¢ есть, но запас "
                f"{edge*100:+.1f}¢ < {c.jump_min_edge_cents:.1f}¢ — книга "
                f"уже переоценилась, мы опоздали"))

        track = TRACK_A if ask >= c.jump_price_split else TRACK_B
        age = f", {s.pm_age_ms:.0f}мс" if s.pm_age_ms is not None else ""
        return side, track, (
            f"ОТСТАВАНИЕ ЯКОРЯ: мы {s.coin_price:,.0f}, Polymarket "
            f"{s.pm_price:,.0f} (${gap:+.2f}{age}) → книге переоцениться на "
            f"{abs(lag)*100:.1f}¢ → {side} @ {ask:.2f}, запас "
            f"+{edge*100:.1f}¢ [дорожка {track}]")

    # ---- режим "impulse": качество импульса, а не голая дистанция -----------
    def stake_for_quality(self, q: float) -> float:
        """Размер ставки от качества сигнала.

        ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО (`jump_stake_by_quality`), и ставка всегда
        `jump_stake_usdc`. Причины две, и обе важнее удобства:

        1. Ступени рвут непрерывное качество на куски. Q=2.99 брал на $4,
           Q=3.01 — на $8, хотя разницы между сигналами нет.
        2. Пока ни один порог не подтверждён историей, множитель к ставке
           множит и ошибку. А ещё он ломает саму статистику: разный размер
           позиции смешивает «сигнал был лучше» с «мы поставили больше», и
           разделить их потом нечем.
        """
        c = self.cfg
        if not getattr(c, "jump_stake_by_quality", False):
            return c.jump_stake_usdc
        if q >= c.jump_q_best:
            return c.jump_stake_best
        if q >= c.jump_q_strong:
            return c.jump_stake_strong
        if q >= c.jump_q_good:
            return c.jump_stake_good
        return c.jump_stake_usdc

    def min_quality(self, s: JumpSnapshot) -> Tuple[float, str]:
        """Планка качества для входа ПРЯМО СЕЙЧАС и почему она такая.

        Обычный вход — `jump_imp_min_score`. Но сразу после выхода в минус
        планка поднимается до `jump_ladder_min_q`: повторный вход против
        только что проигранной стороны рискованнее первого, поэтому сигнал
        обязан быть сильнее среднего. Это и есть новая лестница — не
        автоматический переворот, а вход по более строгим правилам.
        """
        c = self.cfg
        base = c.jump_imp_min_score
        if not getattr(c, "jump_ladder_reenters", False):
            return base, ""
        until = self._strict_until
        if until is None or s.t >= until:
            return base, ""
        need = max(base, c.jump_ladder_min_q)
        return need, (f" (повторный вход после разворота: планка поднята "
                      f"с {base:.2f} до {need:.2f}, осталось "
                      f"{until - s.t:.0f}с)")

    def max_leg_price_for_quality(self, q: float) -> float:
        """Потолок цены ноги от качества: сильному сигналу можно дороже."""
        c = self.cfg
        if q >= c.jump_q_strong:
            return c.jump_max_leg_price_strong
        if q >= c.jump_q_good:
            return c.jump_max_leg_price
        return c.jump_max_leg_price_weak

    def _trigger_impulse(self, s: JumpSnapshot):
        """Вход по КАЧЕСТВУ импульса, а не по пройденному расстоянию.

        Жёсткие ворота (провал любых — сделки нет):
          1. скачок >= jump_imp_jump_sigmas * σ   (пороги в σ, не в долларах);
          2. скорость в сторону импульса >= jump_imp_speed_sigmas * σ;
          3. удержание хода >= jump_imp_min_hold  (иначе это вынос ликвидности);
          4. ускорение >= jump_imp_min_accel      (затухший импульс не берём);
          5. экстремум свежий (сам трекер ищет только в коротком окне);
          6. расстояние до таргета <= 3σ√t        (обе стороны, не только дешёвая);
          7. запас >= max(база, jump_edge_spread_mult * спред) — широкий
             спред сам ужесточает требования;
          8. ask <= потолок, зависящий от качества сигнала.

        Чувствительность — компонент оценки, а не ворота: она поднимает
        качество Q, от которого зависят ставка ($1/$2/$4/$8) и потолок цены.
        Дорожек A/B нет: одинаковый импульс оценивается одинаково при любой
        цене контракта, цена входит только как риск (потолок и запас).
        """
        c = self.cfg
        if s.speed is None or s.imp_hold is None or s.imp_age_s is None:
            return Action(NONE, "нет данных импульса (трекер прогревается)")
        if not s.sigma_1s or s.sigma_1s <= 0:
            return Action(NONE, "σ ещё не измерена — качество не посчитать")
        sigma = s.sigma_1s

        jump = s.jump_usd
        need_jump = c.jump_imp_jump_sigmas * sigma
        if abs(jump) < need_jump:
            return Action(NONE, (
                f"движение ${jump:+.2f} < {c.jump_imp_jump_sigmas:.1f}σ "
                f"(${need_jump:.2f}) — жду"))
        side = "Up" if jump > 0 else "Down"
        direction = favour(side)

        spd = s.speed * direction             # скорость В СТОРОНУ импульса
        need_speed = c.jump_imp_speed_sigmas * sigma
        if spd < need_speed:
            return Action(NONE, (
                f"{side}: скачок ${jump:+.2f} есть, но скорость "
                f"{spd:+.2f}$/с < {need_speed:.2f} — движение уже выдохлось"))

        if s.imp_hold < c.jump_imp_min_hold:
            return Action(NONE, (
                f"{side}: цена удержала лишь {s.imp_hold:.0%} хода "
                f"(< {c.jump_imp_min_hold:.0%}) — похоже на вынос ликвидности"))

        accel = s.accel if s.accel is not None else 1.0
        if accel < c.jump_imp_min_accel:
            return Action(NONE, (
                f"{side}: импульс затухает (ускорение {accel:.2f} < "
                f"{c.jump_imp_min_accel:.2f}) — вход отменён"))

        # ВОЗРАСТ САМОГО ИМПУЛЬСА, а не только свежесть экстремума. Движение
        # может идти двадцать секунд: все фильтры пройдут (ход большой, ход
        # держится, скорость есть), а покупать уже нечего — вход придётся на
        # самый конец хода. Ограничение отсекает ровно это.
        max_age = getattr(c, "jump_imp_max_age_s", 0.0)
        if max_age > 0 and s.imp_age_s > max_age:
            return Action(NONE, (
                f"{side}: импульс идёт уже {s.imp_age_s:.0f}с "
                f"(> {max_age:.0f}с) — вход был бы в конце движения"))

        # Близость к таргету — для ОБЕИХ сторон: далеко от таргета исход
        # решён, и проценты не сдвинет даже идеальный импульс.
        if s.target is not None:
            dist = abs(s.coin_price - s.target)
            limit = self.max_target_distance(s)
            if dist > limit:
                return Action(NONE, (
                    f"{side}: до таргета ${dist:,.0f} > ${limit:,.0f} "
                    f"({c.jump_max_target_sigmas:.0f}σ) — исход уже решён"))

        bid, ask = s.bid(side), s.ask(side)
        if ask is None:
            return Action(NONE, f"{side}: нет ask в книге")

        # Динамический запас: широкий спред сам поднимает планку.
        spread = (ask - bid) if bid is not None else 0.05
        edge_min = max(c.jump_min_edge_cents,
                       c.jump_edge_spread_mult * spread * 100)
        edge = self._edge(s, side, ask)
        if edge is None:
            return Action(NONE, "нет таргета или σ — запас не посчитать")
        if edge * 100 < edge_min:
            fair = ask + edge
            return Action(NONE, (
                f"{side}: справедливо {fair*100:.0f}¢, просят {ask*100:.0f}¢ "
                f"-> запас {edge*100:+.1f}¢ < {edge_min:.1f}¢ "
                f"(спред {spread*100:.0f}¢) — переплачиваем"))

        # --- СТРУКТУРА КНИГИ ------------------------------------------------
        # Жёсткие ворота по книге по умолчанию ВЫКЛЮЧЕНЫ (пороги 0). Признаки
        # при этом считаются всегда и попадают в журнал каждой сделки — чтобы
        # потом ответить, влияют ли они на исход, ДАННЫМИ, а не рассуждением.
        imb = s.imbalance(side)
        wall = s.wall(side)
        need_imb = getattr(c, "jump_book_min_imbalance", 0.0)
        if need_imb > 0 and imb is not None and imb < need_imb:
            return Action(NONE, (
                f"{side}: книга не подтверждает — перекос {imb:+.2f} < "
                f"{need_imb:+.2f} (продавцов больше, чем покупателей)"))
        max_wall = getattr(c, "jump_book_max_wall", 0.0)
        if max_wall > 0 and wall is not None and wall > max_wall:
            return Action(NONE, (
                f"{side}: {wall:.0%} всей ask-ликвидности в одной заявке "
                f"(> {max_wall:.0%}) — стена на пути движения"))

        # --- ВЗВЕШЕННАЯ оценка качества --------------------------------------
        # Каждый компонент нормирован на свой порог (1.0 = ровно на пороге) и
        # ограничен тройкой, чтобы один аномальный признак не покупал сделку
        # в одиночку. Раньше бралось простое среднее — то есть заявлялось,
        # что все четыре признака одинаково важны. Это заведомо неверно, а
        # проверить нечем, поэтому веса вынесены в настройки и участвуют в
        # переборе наравне с порогами.
        parts = [
            ("speed", c.jump_w_speed, min(spd / need_speed, 3.0)),
            ("edge", c.jump_w_edge, min(edge * 100 / edge_min, 3.0)),
            ("jump", c.jump_w_jump, min(abs(jump) / need_jump, 3.0)),
        ]
        shift = self._expected_shift(s, jump)
        if shift is not None and c.jump_min_shift_cents > 0:
            parts.append(("shift", c.jump_w_shift,
                          min(abs(shift) * 100 / c.jump_min_shift_cents, 3.0)))
        if imb is not None and c.jump_w_book > 0:
            # Перекос [−1..+1] переводим в ту же шкалу «доля от порога»:
            # 0 перекоса = 1.0 (нейтрально), полный перекос в нашу сторону = 2.0.
            parts.append(("book", c.jump_w_book, max(0.0, 1.0 + imb)))

        wsum = sum(w for _n, w, _v in parts)
        if wsum <= 0:                      # все веса обнулены — простое среднее
            quality = sum(v for _n, _w, v in parts) / max(len(parts), 1)
        else:
            quality = sum(w * v for _n, w, v in parts) / wsum

        need_q, why_strict = self.min_quality(s)
        if quality < need_q:
            return Action(NONE, (
                f"{side}: качество {quality:.2f} < {need_q:.2f}"
                f"{why_strict} — импульс есть, но слабый"))

        cap = self.max_leg_price_for_quality(quality)
        if ask > cap:
            return Action(NONE, (
                f"{side} ask {ask:.2f} > потолка {cap:.2f} для качества "
                f"{quality:.2f} — риск не по сигналу"))

        stake = self.stake_for_quality(quality)
        # Признаки входа едут вместе с решением: движок положит их в ногу, а
        # при закрытии — в журнал. Без этого «какой фильтр влияет на прибыль»
        # остаётся вопросом без ответа.
        feat = {
            "q": round(quality, 3),
            "jump_usd": round(jump, 2),
            "jump_sigmas": round(abs(jump) / sigma, 2),
            "speed": round(spd, 3),
            "speed_sigmas": round(spd / sigma, 2),
            "accel": round(accel, 3),
            "hold": round(s.imp_hold, 3),
            "imp_age_s": round(s.imp_age_s, 2),
            "edge_cents": round(edge * 100, 2),
            "edge_min_cents": round(edge_min, 2),
            "spread_cents": round(spread * 100, 2),
            "sigma": round(sigma, 3),
            "secs_left": round(s.seconds_left, 1),
            "dist_target": (round(abs(s.coin_price - s.target), 1)
                            if s.target is not None else None),
            "book_imb": None if imb is None else round(imb, 3),
            "book_wall": None if wall is None else round(wall, 3),
            "book_ask_trend": (None if s.ask_trend(side) is None
                               else round(s.ask_trend(side), 3)),
            "flow": round(s.flow(side), 3),
            "strict_entry": bool(why_strict),
            "q_parts": {n: round(v, 3) for n, _w, v in parts},
        }
        return (side, "Q", (
            f"ИМПУЛЬС {side}: Q={quality:.2f}{why_strict} (скачок ${jump:+.2f}"
            f"={abs(jump)/sigma:.1f}σ, скорость {spd:.1f}$/с, "
            f"удержание {s.imp_hold:.0%}, ускорение {accel:.2f}, "
            f"запас +{edge*100:.1f}¢ при пороге {edge_min:.1f}¢"
            + (f", перекос книги {imb:+.2f}" if imb is not None else "")
            + ")"), stake, feat)

    # ---- режим "jump": исходный триггер по скачку цены ----------------------
    def _trigger_jump(self, s: JumpSnapshot):
        c = self.cfg
        # Пороги подстраиваются под живость рынка: на разогнанном рынке тот же
        # скачок двигает процент во столько же раз слабее.
        scale = self.vol_scale(s)
        small = c.jump_small_usd * scale
        big = c.jump_big_usd * scale
        sc_txt = f" [x{scale:.1f} по σ]" if scale != 1.0 else ""

        jump = s.jump_usd
        if abs(jump) < small:
            return Action(NONE, f"движение ${jump:+.2f} {self._horizon()} "
                                f"< ${small:.1f}{sc_txt} — жду")

        side = "Up" if jump > 0 else "Down"
        ask = s.ask(side)
        if ask is None:
            return Action(NONE, f"{side}: нет ask в книге")

        # Дорогая сторона (>= сплита) — хватает малого скачка; дешёвая
        # требует большого И близости к таргету.
        if ask >= c.jump_price_split:
            track, need = TRACK_A, small
        else:
            track, need = TRACK_B, big

        if abs(jump) < need:
            return Action(NONE, (
                f"{side} ask {ask:.2f} < {c.jump_price_split:.2f} — дешёвой "
                f"стороне нужен скачок ${need:.1f}{sc_txt}, а он ${jump:+.2f}"))

        if track == TRACK_B:
            if s.target is None:
                return Action(NONE, f"{side} дешёвая ({ask:.2f}), но таргет "
                                    f"раунда ещё не известен — не вхожу")
            dist = abs(s.coin_price - s.target)
            limit = self.max_target_distance(s)
            if dist > limit:
                return Action(NONE, (
                    f"{side} дешёвая ({ask:.2f}): до таргета ${dist:,.0f} > "
                    f"${limit:,.0f} ({c.jump_max_target_sigmas:.0f}σ) — "
                    f"скачок не спасёт"))

        if ask > c.jump_max_leg_price:
            return Action(NONE, f"{side} ask {ask:.2f} > потолка "
                                f"{c.jump_max_leg_price:.2f} — нечего забирать")

        # ЗАПАС ЦЕНЫ. Скачок говорит, КУДА пошла цена, но молчит о том, не
        # заложен ли он уже в процент. Считаем справедливую вероятность нашей
        # стороны и сравниваем с тем, что просят: если ask выше справедливой
        # цены, мы покупаем переоценённое, каким бы сильным ни был скачок.
        edge = self._edge(s, side, ask)
        if edge is not None and edge * 100 < c.jump_min_edge_cents:
            fair = ask + edge
            return Action(NONE, (
                f"{side}: справедливо {fair*100:.0f}¢, просят {ask*100:.0f}¢ "
                f"-> запас {edge*100:+.1f}¢ < {c.jump_min_edge_cents:.1f}¢ "
                f"— движение уже в цене, переплачиваем"))

        # Сдвинется ли «процент» вообще? Далеко от таргета исход раунда уже
        # решён, и любой скачок цены оставляет проценты на месте — там вход
        # заведомо съедается спредом.
        shift = self._expected_shift(s, jump)
        if shift is not None and abs(shift) * 100 < c.jump_min_shift_cents:
            return Action(NONE, (
                f"{side}: скачок ${jump:+.2f} сдвинет процент лишь на "
                f"{abs(shift)*100:.1f}¢ (< {c.jump_min_shift_cents:.1f}¢) — "
                f"цена в ${abs(s.coin_price - s.target):,.0f} от таргета, "
                f"исход уже решён" if s.target is not None else
                f"{side}: ожидаемый сдвиг процента {abs(shift)*100:.1f}¢ мал"))

        return side, track, (
            f"СКАЧОК ${jump:+.2f} {self._horizon()} "
            f"({'вверх' if jump > 0 else 'вниз'}) → {side} @ {ask:.2f} "
            f"[дорожка {track}: порог ${need:.1f}{sc_txt}]")

    # ======================================================================
    #  Фиксация прибыли — обе дорожки
    # ======================================================================
    def _take_profit(self, s: JumpSnapshot) -> Optional[Action]:
        """В плюсе, а рост встал — забираем и идём искать следующий вход.

        Именно этот выход закрывает случай «вышли в хороший плюс, досидели
        до расчёта и ушли в минус»: как только процент перестал расти ИЛИ
        цена монеты замерла, прибыль фиксируется, а не проверяется на
        прочность оставшимися минутами раунда.
        """
        c = self.cfg
        if not c.jump_tp_enabled:
            return None
        for leg in self.legs:
            bid = s.bid(leg.outcome)
            if bid is None:
                continue

            # ── ТРЕЙЛИНГ-СТОП: не отдавать назад то, что уже заработано ──
            # Прибыль, которая была на экране, — достигнутый результат, а не
            # намерение. Проверка стоит ПЕРЕД порогом «мы в плюсе» намеренно:
            # на откате плюс тает первым, и старый порядок переставал смотреть
            # на позицию ровно в тот момент, когда защищать её нужнее всего.
            #
            # Взвод — по jump_tp_trail_arm, а НЕ по jump_tp_min_gain. Разница
            # решающая: 10¢ означали «бид выше уплаченного ask на 10¢, то есть
            # выше бида на входе на 10¢ плюс спред», и до такой планки почти
            # ни одна нога не доживала. Стоп молчал, позиция ехала вниз, и
            # выглядело это как поломка.
            # Допуск 1e-9 — цены в целых центах, а 0.63 − 0.53 в двоичной
            # дроби даёт 0.09999999999999998, и ровно на пороге не срабатывало.
            eps = 1e-9
            armed = leg.peak_bid - leg.entry_price >= c.jump_tp_trail_arm - eps
            if (c.jump_tp_trail > 0 and armed
                    and bid <= leg.peak_bid - c.jump_tp_trail + eps):
                return Action(
                    SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
                    limit_price=round(bid, 2),
                    reason=(
                        f"ФИКСИРУЮ {leg.outcome}: пик был {leg.peak_bid:.2f}, "
                        f"сейчас {bid:.2f} — откат "
                        f"{(leg.peak_bid - bid) * 100:.0f}¢ от пика "
                        f"(вошли {leg.entry_price:.2f}, итог "
                        f"{bid - leg.entry_price:+.2f})"),
                )

            gain = bid - leg.entry_price      # честный плюс: bid против ask

            # ── ИМПУЛЬС УМЕР: скорость упала, прибыль есть — забираем ──
            # Трейлинг реагирует только ПОСЛЕ отката процента, а скорость
            # монеты умирает раньше отката. Взводится, только если пик
            # скорости был осмысленным, а не шумом.
            drop = getattr(c, "jump_exit_speed_drop", 0.0)
            if (drop > 0 and s.speed is not None and gain > 0
                    and leg.peak_speed >= getattr(c, "jump_exit_speed_floor",
                                                  2.0)
                    and s.speed * favour(leg.outcome)
                        <= leg.peak_speed * (1.0 - drop)):
                return Action(
                    SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
                    limit_price=round(bid, 2),
                    reason=(
                        f"ИМПУЛЬС УМЕР {leg.outcome}: скорость "
                        f"{s.speed * favour(leg.outcome):+.1f}$/с после пика "
                        f"{leg.peak_speed:.1f}$/с (падение >{drop:.0%}) — "
                        f"забираю {gain:+.2f}, не жду отката"),
                )

            if gain < c.jump_tp_min_gain:
                continue                      # ещё не в плюсе — не о чем говорить

            deadline = s.seconds_left <= c.jump_tp_deadline_s
            mom_fav = s.jump_usd * favour(leg.outcome)
            retraced = bid <= leg.peak_bid - c.jump_tp_stall_retrace
            flow_bad = s.flow(leg.outcome) <= c.jump_tp_flow_against
            faded = mom_fav <= 0 and (retraced or flow_bad)
            # «проценты перестали подниматься»: пик стоит на месте столько
            # секунд. Прямая проверка застоя — не ждём отката.
            frozen = (c.jump_tp_stall_s > 0
                      and s.t - leg.peak_t >= c.jump_tp_stall_s)
            # «цена перестала резко двигаться и стоит на месте»
            quiet = (c.jump_tp_quiet_usd > 0
                     and abs(s.jump_usd) <= c.jump_tp_quiet_usd
                     and s.t - leg.peak_t >= c.jump_tp_stall_s / 2)

            if not (deadline or faded or frozen or quiet):
                continue
            if deadline:
                why = f"до конца {s.seconds_left:.0f}с"
            elif frozen:
                why = (f"% стоит на {leg.peak_bid:.2f} уже "
                       f"{s.t - leg.peak_t:.0f}с — рост встал")
            elif quiet:
                why = (f"цена замерла (движение {s.jump_usd:+.1f}$ <= "
                       f"{c.jump_tp_quiet_usd:.1f}$)")
            else:
                why = (f"рост кончился (движение {mom_fav:+.1f}$"
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
    #  Жёсткий стоп: процент ушёл ниже входа — выходим
    # ======================================================================
    def _stop_out(self, s: JumpSnapshot) -> Optional[Action]:
        """Ниже входа хотя бы на `jump_stop_loss` — закрываемся сразу.

        Это нижняя граница под всей позицией, независимая от трейлинга: тот
        защищает достигнутую прибыль, а этот — не даёт сделке, которая пошла
        не туда, вообще развиваться.

        ПОЧЕМУ ОТ БИДА НА ВХОДЕ, а не от уплаченного ask. Спред означает, что
        сразу после покупки бид уже ниже ask (при спреде 3¢ — на все три).
        Стоп «на цент ниже входа», отсчитанный от ask, сработал бы в ТОТ ЖЕ
        ТИК на каждой сделке и превратил бы бота в машину по выплате спреда.
        Бид на входе — это цена, по которой рынок реально готов был выкупить
        нашу ногу в момент покупки, поэтому уход ниже неё и есть движение
        против нас.

        КОГДА ОН ВООБЩЕ СРАБАТЫВАЕТ (`jump_ladder_before_stop`, по умолчанию
        включено). Порог стопа (1¢) меньше порога лестницы (2¢ + выдержка
        0.6с), поэтому по цене стоп наступает раньше ВСЕГДА, и просто
        поменять вызовы местами недостаточно — разворот всё равно не
        случился бы ни разу. Приоритет лестницы означает буквально: пока
        ногу способен взять разворот, стоп её не трогает. Стоп остаётся
        страховкой на случаи, когда развернуться не во что: лестница
        выключена, упёрлась в предел ступеней, у обратной стороны нет ask
        или она дороже потолка.

        Плата за это названа честно: между 1¢ и 2¢ нога больше не режется,
        а в течение выдержки 0.6с падение может уйти дальше. Это тот самый
        размен «выйти дёшево» против «отыграться разворотом», и теперь он
        сдвинут в сторону разворота. Вернуть прежнее —
        `JUMP_LADDER_BEFORE_STOP=false`.
        """
        c = self.cfg
        if c.jump_stop_loss <= 0:
            return None
        stop = self.stop_threshold(s)
        for leg in self.legs:
            bid = s.bid(leg.outcome)
            if bid is None:
                continue
            if bid > leg.entry_bid - stop + 1e-9:
                continue
            tail = ""
            if c.jump_ladder_before_stop:
                blocked = self._ladder_blocked(s, leg, bid)
                if blocked is None:
                    continue          # ногу возьмёт разворот — не мешаем
                tail = f", развернуться нельзя: {blocked}"
            return Action(
                SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
                limit_price=round(bid, 2),
                reason=(f"СТОП {leg.outcome}: {bid:.2f} ниже бида на входе "
                        f"{leg.entry_bid:.2f} на "
                        f"{(leg.entry_bid - bid) * 100:.0f}¢ "
                        f"(порог {stop*100:.1f}¢, вошли по "
                        f"{leg.entry_price:.2f}, итог "
                        f"{bid - leg.entry_price:+.2f}{tail})"),
            )
        return None

    def stop_threshold(self, s: JumpSnapshot) -> float:
        """Порог жёсткого стопа, адаптированный к волатильности.

        На тихом рынке (σ около опорной) это прежний 1¢. На разогнанном
        случайный тик двигает процент на цент и выбивал бы позицию при
        верном направлении — порог растёт пропорционально σ, но не выше
        потолка jump_stop_max.
        """
        c = self.cfg
        stop = c.jump_stop_loss
        if (getattr(c, "jump_stop_adaptive", False) and s.sigma_1s
                and s.sigma_1s > 0 and c.jump_sigma_ref > 0):
            mult = max(1.0, s.sigma_1s / c.jump_sigma_ref)
            stop = min(stop * mult, getattr(c, "jump_stop_max", 0.05))
            stop = max(stop, c.jump_stop_loss)
        return stop

    # ======================================================================
    #  Лестница-переворот: продаём провалившуюся ногу и берём другую сторону
    # ======================================================================
    def _ladder_blocked(self, s: JumpSnapshot, leg: Leg,
                        bid: Optional[float]) -> Optional[str]:
        """Почему разворот НЕ сможет взять эту ногу. None = сможет.

        Ответ нужен в двух местах, и он обязан быть ОДИН: сама лестница
        печатает его как причину «держу», а жёсткий стоп по нему решает,
        стоять ли ему в стороне. Если бы каждый считал сам, появилась бы
        дыра — стоп молчит «её возьмёт разворот», а разворот в тот же тик
        отказывается по своей причине, и нога не выходит вообще.
        """
        c = self.cfg
        if not c.jump_ladder_enabled:
            return "лестница выключена"
        # В режиме повторного входа разворот — это ПРОДАЖА и ничего больше.
        # Продать можно всегда: ни пауза после отказа (она держит только
        # покупки), ни цена обратной стороны, ни предел ступеней тут ни при
        # чём. Значит ногу разворот заберёт, и стопу вмешиваться незачем.
        if getattr(c, "jump_ladder_reenters", False):
            return None
        # Дальше — старый переворот, а он ПОКУПАЕТ обратную сторону.
        # Покупки сейчас не проходят, ждать нельзя: ногу должен забрать стоп.
        if s.entries_paused:
            return "пауза после отказа биржи — покупка не пройдёт"
        # Предел ступеней: 0 = без ограничений (долг всё равно сходится).
        if 0 < c.jump_max_ladder_legs <= self.depth:
            return (f"лестница на пределе "
                    f"({self.depth}/{c.jump_max_ladder_legs})")
        opp = opposite(leg.outcome)
        opp_ask = s.ask(opp)
        if opp_ask is None:
            return f"нет ask по {opp} для разворота"
        if opp_ask > c.jump_max_leg_price:
            return (f"разворот в {opp} по {opp_ask:.2f} дороже потолка "
                    f"{c.jump_max_leg_price:.2f} — шэров нужно слишком много")
        if bid is not None:
            proceeds = bid * leg.shares
            debt_after = max(0.0, self.net_out - proceeds)
            if ladder_shares(debt_after, c.jump_ladder_profit_usdc,
                             opp_ask) <= 0:
                return f"разворот {opp}: расчёт дал 0 шэров"
        return None

    def _maybe_ladder(self, s: JumpSnapshot) -> Action:
        """Проценты пошли против нас — разворачиваемся.

        Отличие от прежней лестницы: старая нога ПРОДАЁТСЯ, а не остаётся
        висеть. Это не косметика, а то, что делает лестницу конечной. Продажа
        возвращает капитал, и в долге остаётся только реализованный убыток
        ноги (спред плюс просадка), а не вся её стоимость:

            держим старую:  долг 1.00 -> 2.50 -> 5.50 -> 11.50 -> 23.50 …
            продаём старую: долг 1.00 -> 0.45 -> 0.29 ->  0.24 ->  0.22 …

        Поэтому лимит на число ступеней здесь не нужен: долг сходится сам.
        """
        c = self.cfg
        leg = self.legs[-1]                   # живая ставка — самая свежая нога
        bid = s.bid(leg.outcome)
        if bid is None:
            return Action(HOLD, f"{leg.outcome}: нет bid — держу")

        # Пошло против? Считаем от БИДА НА ВХОДЕ: спред между ним и уплаченным
        # ask — стоимость входа, а не движение рынка. Иначе разворот
        # срабатывал бы в тот же тик на каждой сделке.
        losing = bid <= leg.entry_bid - c.jump_ladder_loss
        if not losing:
            leg.against_since = None
            self._below_since = None
            return Action(HOLD, (
                f"{leg.outcome} держится {bid:.2f} "
                f"(вход {leg.entry_price:.2f}, бид на входе {leg.entry_bid:.2f})"))

        if leg.against_since is None:
            leg.against_since = s.t
        waited = s.t - leg.against_since
        if waited < c.jump_ladder_grace_s:
            return Action(HOLD, f"{leg.outcome} просел до {bid:.2f}, жду "
                                f"подтверждения {waited:.1f}/"
                                f"{c.jump_ladder_grace_s:.1f}с")

        # РАЗВОРОТ БОЛЬШЕ НЕ ПОКУПАЕТ ВСЛЕПУЮ.
        # Прежняя логика молча принимала, что убыток по одной стороне —
        # подтверждение другой. Это неверно: рынок мог просто встать, и тогда
        # плохи ОБЕ, а автоматический переворот лишь удваивал число сделок и
        # риск. Теперь разворот = ЗАКРЫТЬ убыточную ногу, а противоположная
        # сторона обязана заново пройти все ворота стратегии как новая сделка
        # (и по более высокой планке качества — см. min_quality).
        if getattr(c, "jump_ladder_reenters", False):
            self._strict_until = s.t + c.jump_ladder_strict_s
            return Action(
                SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
                limit_price=round(bid, 2),
                reason=(
                    f"РАЗВОРОТ {leg.outcome}: упал до {bid:.2f} (бид на входе "
                    f"{leg.entry_bid:.2f}) — закрываю. Обратную сторону "
                    f"куплю, только если она сама пройдёт все ворота при "
                    f"Q>={max(c.jump_imp_min_score, c.jump_ladder_min_q):.2f} "
                    f"(итог {bid - leg.entry_price:+.2f})"),
            )

        blocked = self._ladder_blocked(s, leg, bid)
        if blocked is not None:
            return Action(HOLD, (
                f"{leg.outcome} просел до {bid:.2f}, но развернуться нельзя: "
                f"{blocked}"))

        opp = opposite(leg.outcome)
        opp_ask = s.ask(opp)

        # Долг ПОСЛЕ продажи старой ноги: вернём bid*shares, останется только
        # реализованный убыток. Движок пересчитает по факту филла.
        proceeds = bid * leg.shares
        debt_after = max(0.0, self.net_out - proceeds)
        shares = ladder_shares(debt_after, c.jump_ladder_profit_usdc, opp_ask)
        cost = shares * opp_ask

        return Action(
            LADDER, outcome=opp, limit_price=round(opp_ask, 2),
            shares=shares, size_usdc=cost, track=leg.track,
            sell_idx=leg.idx, sell_outcome=leg.outcome,
            reason=(f"РАЗВОРОТ #{self.depth + 1}: {leg.outcome} упал до "
                    f"{bid:.2f} (бид на входе {leg.entry_bid:.2f}) → продаю его "
                    f"и беру {opp} {shares:.2f} шэр @ {opp_ask:.2f} = "
                    f"${cost:.2f}, чтобы перекрыть ${debt_after:.2f} "
                    f"и выйти +${c.jump_ladder_profit_usdc:.2f}"),
        )
