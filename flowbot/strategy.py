"""ЧИСТЫЙ ЛИСТ: сюда пишется новая торговая стратегия.

Здесь нет ни одного торгового правила. Ни порогов, ни фильтров, ни оценок,
ни условий входа и выхода. Всё это удалено вместе с прежней стратегией.

Что здесь есть и почему это НЕ стратегия:

  * `Snapshot` — форма, в которой движок отдаёт СЫРОЙ рынок: цены, книга,
    таргет, время. Никаких производных величин (волатильности, скоростей,
    импульсов, перекосов) — если новой стратегии что-то из этого нужно, она
    считает это сама и хранит у себя.
  * `Leg` и учёт позиции — это память о СОБСТВЕННЫХ действиях, а не решение.
    Движку нужно знать, чем он владеет и сколько потратил, чтобы продавать,
    сверяться с биржей и считать P&L.
  * `Action` — словарь того, что движок умеет исполнить.

Решения принимают три метода: `should_enter`, `should_exit` и `on_tick`.
Сейчас они возвращают «ничего не делать». Это единственное место, куда
нужно писать новую логику.

Контракт с движком:

    act = strategy.on_tick(snapshot)     # каждое событие книги
    -> Action(NONE)                      # ничего не делать
    -> Action(BUY,  outcome=..., limit_price=..., size_usdc=...)
    -> Action(SELL, sell_idx=..., sell_outcome=..., limit_price=...)

    strategy.record_entry(...)           # движок сообщает о факте покупки
    strategy.record_sell(...)            # и о факте продажи
    strategy.reset_round()               # начало нового 5-минутного окна

Движок сам следит за исполнением, таймаутами, частичными филлами и сверкой
с биржей. Стратегии об этом знать не нужно — она видит рынок и свою
позицию, и отвечает, что делать.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# --- что движок умеет исполнить ---------------------------------------------
NONE = "none"       # ничего не делать
BUY = "buy"         # купить сторону
SELL = "sell"       # продать конкретную ногу
HOLD = "hold"       # позиция есть, трогать её не надо


@dataclass
class Snapshot:
    """Сырой рынок на один такт. Производных величин здесь нет намеренно."""

    t: float                              # монотонное время, секунды
    seconds_left: float                   # до конца 5-минутного окна
    coin_price: Optional[float]           # консенсусная цена монеты
    target: Optional[float]               # openPrice раунда
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    # Цена, по которой считает раунд САМА площадка (якорь Chainlink).
    pm_price: Optional[float] = None
    pm_age_ms: Optional[float] = None
    # Движок сейчас не пропустит покупку (пауза после отказа биржи).
    # Стратегия обязана это учитывать, если её выход рассчитывает на покупку.
    entries_paused: bool = False
    # СЫРЬЁ, а не производные величины. История цены и уровни книги — это
    # те же данные, только за больший промежуток: волатильность, скорость,
    # глубину и перекос стратегия считает из них сама и хранит у себя.
    price_hist: Sequence[Tuple[float, float]] = ()      # [(время, цена), …]
    up_levels: Tuple[Sequence, Sequence] = ((), ())     # (биды, аски)
    down_levels: Tuple[Sequence, Sequence] = ((), ())
    # Сколько секунд книга не менялась. None — событий ещё не было. Обрыв
    # потока CLOB изнутри выглядит как «рынок замер»: цены остаются те же,
    # что были в момент разрыва, и отличить тихий рынок от мёртвого сокета
    # можно только по времени.
    book_age_s: Optional[float] = None

    def bid(self, outcome: str) -> Optional[float]:
        return self.up_bid if outcome == "Up" else self.down_bid

    def ask(self, outcome: str) -> Optional[float]:
        return self.up_ask if outcome == "Up" else self.down_ask


@dataclass
class Leg:
    """Одна купленная позиция: память о собственной сделке, не решение."""

    outcome: str                          # "Up" | "Down"
    entry_price: float                    # уплаченный ask
    shares: float
    cost: float
    entry_t: float
    idx: int                              # порядковый номер в раунде
    # Бид в момент входа. Спред между ним и уплаченным ask — стоимость входа,
    # а не движение рынка против нас. Любой расчёт «пошло против» должен
    # считаться от него, иначе сработает в тот же тик на каждой сделке.
    entry_bid: float = 0.0
    # Свободное место под признаки сигнала: что стратегия положит сюда при
    # входе, то и попадёт в журнал при закрытии. Движок в содержимое не
    # заглядывает.
    feat: Dict = field(default_factory=dict)


@dataclass
class Action:
    kind: str
    reason: str = ""
    outcome: Optional[str] = None         # что купить
    limit_price: Optional[float] = None   # потолок покупки / пол продажи
    size_usdc: Optional[float] = None     # сколько купить, в долларах
    sell_idx: Optional[int] = None        # какую ногу продать
    sell_outcome: Optional[str] = None
    feat: Dict = field(default_factory=dict)   # признаки сигнала для журнала


def opposite(outcome: str) -> str:
    return "Down" if outcome == "Up" else "Up"


class Strategy:
    """Пустая стратегия: смотрит рынок и не делает ничего.

    Учёт позиции (`legs`, `net_out`) остался — он нужен движку, чтобы
    продавать и сверяться с биржей. Решения — ниже, в трёх методах, и они
    пустые.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.legs: List[Leg] = []
        # Чистый отток кэша за раунд: куплено минус продано.
        self.net_out = 0.0
        self._next_idx = 0

    # ======================================================================
    #  РЕШЕНИЯ — сюда пишется новая стратегия
    # ======================================================================
    def should_enter(self, s: Snapshot) -> Optional[Action]:
        """Открывать ли позицию. Зовётся, только когда позиции нет.

        Вернуть Action(BUY, outcome="Up"|"Down", limit_price=ask,
        size_usdc=...) чтобы купить, или None чтобы пропустить такт.
        """
        return None

    def should_exit(self, s: Snapshot, leg: Leg) -> Optional[Action]:
        """Закрывать ли эту ногу. Зовётся по каждой открытой ноге.

        Вернуть Action(SELL, sell_idx=leg.idx, sell_outcome=leg.outcome,
        limit_price=bid) чтобы продать, или None чтобы держать.
        """
        return None

    def on_tick(self, s: Snapshot) -> Action:
        """Один такт. Только диспетчер: сам ничего не решает.

        Порядок намеренно простой — сначала выходы по открытым ногам, потом
        вход, если позиции нет. Новая стратегия вольна переопределить этот
        метод целиком, если ей нужен другой порядок.
        """
        for leg in list(self.legs):
            act = self.should_exit(s, leg)
            if act is not None:
                return act
        if not self.legs:
            act = self.should_enter(s)
            if act is not None:
                return act
            return Action(NONE, "стратегия не задана")
        return Action(HOLD, "стратегия не задана")

    # ======================================================================
    #  Учёт позиции — движок сообщает сюда о фактах исполнения
    # ======================================================================
    def reset_round(self) -> None:
        """Новое 5-минутное окно: позиция и счётчики с нуля."""
        self.legs = []
        self.net_out = 0.0
        self._next_idx = 0

    def record_entry(self, outcome: str, price: float, shares: float,
                     cost: float, t: float,
                     entry_bid: Optional[float] = None,
                     feat: Optional[Dict] = None) -> Leg:
        leg = Leg(outcome=outcome, entry_price=price, shares=shares,
                  cost=cost, entry_t=t, idx=self._next_idx,
                  entry_bid=entry_bid if entry_bid is not None else price,
                  feat=dict(feat or {}))
        self._next_idx += 1
        self.legs.append(leg)
        self.net_out += cost
        return leg

    def record_partial_sell(self, idx: int, shares_sold: float,
                            proceeds: float, t: float) -> None:
        """Продалась ЧАСТЬ ноги — уменьшаем её, а не закрываем.

        Так бывает только в бою: ордер уходит как FAK, и если в книге на
        нашей цене лежало меньше, чем мы продаём, остаток отменяется. Нога
        никуда не девается — шэры всё ещё у нас, и забыть про них нельзя.
        """
        for lg in self.legs:
            if lg.idx != idx:
                continue
            # Усечение ВНИЗ, а не round: остаток — это «сколько ещё можно
            # продать», и он не имеет права вырасти от арифметики.
            lg.shares = max(0.0, floor2(lg.shares - shares_sold))
            lg.cost = round(lg.entry_price * lg.shares, 2)
            if lg.shares <= 0:
                self.legs = [x for x in self.legs if x.idx != idx]
            break
        self.net_out -= proceeds

    def record_sell(self, idx: int, proceeds: float, t: float) -> None:
        self.legs = [lg for lg in self.legs if lg.idx != idx]
        self.net_out -= proceeds

    def record_settle(self, leg: Leg, payout: float, pnl: float,
                      t: float) -> None:
        """Раунд рассчитан: движок сообщает исход позиции.

        Без этого стратегия не знает, выиграла она или проиграла, — а
        значит не может вести дневной риск. Продажа (`record_sell`) отвечает
        только за те позиции, которые закрыли сами.
        """

    @property
    def debt(self) -> float:
        """Сколько кэша надо отбить (0, если раунд уже в плюсе)."""
        return max(0.0, self.net_out)


def floor2(x: float) -> float:
    """Усечение вниз до сотых. Продать больше, чем лежит, физически нельзя."""
    return int(x * 100) / 100.0
