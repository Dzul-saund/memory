"""Конфигурация: сеть, ордера, режимы, журналы, рантайм.

ТОРГОВЫХ ПОРОГОВ ЗДЕСЬ НЕТ. Все параметры прежней стратегии — пороги входа,
фильтры, веса, оценки, правила разворота и выхода — удалены вместе с ней.
Осталось только то, без чего не работает инфраструктура: подключение к
бирже, размер и лимиты ордера, режим DRY/LIVE, задержки, журналы, фиды.

Новая стратегия свои настройки заводит сама. Если ей нужен порог, он должен
появиться здесь только тогда, когда его кто-то действительно читает, —
иначе конфиг снова зарастёт мёртвыми ключами.

Всё грузится из окружения / `.env` (совместимо с btc_bot.config), поэтому
пресеты держат в отдельном `.env`-файле. Флаги CLI перекрывают эти значения.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:  # необязательно: подхватить локальный .env, если стоит python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv опционален
    pass


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _s(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _tuple(name: str, default: tuple) -> tuple:
    """Список чисел через запятую: CERT_STEPS=10,10,10,20.

    Молча проглотить мусор здесь нельзя: неверные ступени означают бота,
    который либо не наберёт позицию, либо перебьёт её, — и то и другое
    происходит бесшумно.
    """
    v = os.getenv(name)
    if v in (None, ""):
        return default
    try:
        return tuple(float(x) for x in v.replace(";", ",").split(",")
                     if x.strip())
    except ValueError as exc:
        raise ValueError(f"{name}: ожидались числа через запятую "
                         f"(например 10,10,10,20), получено {v!r}") from exc


@dataclass
class FlowConfig:
    # --- монета и рынок ------------------------------------------------------
    asset: str = "btc"
    window_seconds: int = 300               # 5-минутный рынок

    # --- режим ---------------------------------------------------------------
    # true = симуляция, ордера на биржу не уходят. Единственный безопасный
    # режим, пока стратегия не проверена.
    dry_run: bool = True
    dry_run_balance: float = 100.0

    # --- размер и лимиты ордера ----------------------------------------------
    # Сколько тратить на вход по умолчанию. Стратегия может запросить своё
    # значение в Action.size_usdc; это — запасное.
    stake_usdc: float = 1.0
    # Минимальный размер ордера площадки. Размер покупки округляется ВВЕРХ до
    # него: округление вниз опускало каждый ордер под доллар
    # ($1.00 / 0.53 = 1.8867 -> 1.88 -> $0.9964) и площадка его отвергала.
    min_order_usdc: float = 1.0
    # Потолок ВСЕХ вложений в один раунд. Это ограничение риска, а не правило
    # стратегии: сколько бы сделок она ни захотела, больше этого за окно не
    # уйдёт. Проверяется дважды — при решении и перед отправкой, потому что
    # за время пинга цена филла могла вырасти.
    max_round_usdc: float = 25.0
    # Пол цены продажи: на сколько ниже цены решения согласны отдать позицию.
    # ВЫКЛЮЧЕН (0), и это осознанно. Пол противоречит смыслу стопа: выходить
    # надо именно на падении, а пол запрещает продавать ровно тогда, когда
    # падает сильнее всего. В бою позиция съехала с 0.47 до 0.10 при поле,
    # стоявшем на 0.45.
    max_sell_slip: float = 0.0

    # --- задержки и таймауты -------------------------------------------------
    # Если ping_ms задан — используем его, иначе меряем RTT до CLOB вживую.
    ping_ms: Optional[float] = None
    assumed_rtt_ms: float = 90.0
    # Внутренняя обработка ордера (подпись + матчинг) сверх сети.
    order_process_ms: float = 45.0
    # Сколько ждать ответа биржи на ордер. У клиента своего таймаута НЕТ:
    # повисший запрос держал бы флаг «ордер в полёте» взведённым навсегда, и
    # бот молча переставал бы торговать до перезапуска.
    order_timeout_s: float = 10.0
    # В dry-run честно ждать пинг перед филлом.
    simulate_latency: bool = True
    # Пауза после ОТКАЗА биржи. Без неё отклонённый ордер повторяется на
    # каждом такте — десять раз в секунду. Придерживает только ПОКУПКИ:
    # продажу откладывать нельзя никогда.
    error_cooldown_s: float = 5.0
    # Сколько отказов продажи подряд терпеть, прежде чем снять позицию с
    # торговли. Устойчивый отказ иначе повторяется на каждом такте.
    # 0 = терпеть бесконечно.
    max_sell_fails: int = 5

    # --- сверка с биржей -----------------------------------------------------
    # Как часто спрашивать биржу, чем мы владеем НА САМОМ ДЕЛЕ, секунд.
    # 0 = не сверять. Учёт бота — это память о его же действиях, и она
    # расходится с реальностью при таймаутах, частичных филлах и неверно
    # разобранных ответах. Без сверки расхождение живёт вечно: позиция висит
    # на Polymarket и теряет в цене, а бот показывает «поз —».
    reconcile_s: float = 20.0
    # Как часто напоминать про остаток от закончившегося раунда, секунд.
    # Такие шэры забирают кнопкой Redeem — книги того раунда больше нет.
    leftover_warn_s: float = 120.0

    # --- расчёт раунда -------------------------------------------------------
    # Сколько ждать схлопывания книги после конца окна, прежде чем признать
    # раунд неопределившимся.
    settle_wait_s: float = 20.0
    # Насколько близко к 1.0 должна уйти сторона, чтобы считать её
    # победителем. Up 0.62 / Down 0.35 — это ставка, а не факт.
    settle_converge: float = 0.90

    # --- рантайм -------------------------------------------------------------
    run_duration_seconds: float = 86400.0
    # Как часто ПЕЧАТАТЬ строку состояния. На решения не влияет: они
    # принимаются на каждом обновлении книги.
    status_log_interval_seconds: float = 0.1
    # Запасной такт, если книга молчит. Обычно цикл будит событие фида.
    tick_max_wait_seconds: float = 0.05
    max_consecutive_errors: int = 12
    # Сколько уровней книги отдавать стратегии в снимке. Глубина — сырьё:
    # по одной лучшей цене нельзя отличить «$2000 в книге» от «$40 по нашей
    # цене и частичный филл».
    book_levels: int = 10

    # --- журналы -------------------------------------------------------------
    trade_log_csv: str = "flowbot_trades.csv"   # "" отключает
    # Журнал сделок с признаками (flowbot/stats.py). Лежит ВНЕ папки проекта
    # (`~/.flowbot/trades.jsonl`), поэтому переезд на новую сборку историю не
    # обнуляет — а именно так она и терялась. Пустая строка = путь по умолчанию.
    stats_path: str = ""
    stats_enabled: bool = True
    # Запись всего, что видел бот, в JSONL — сырьё для проигрывания новой
    # стратегии на реальной истории вместо догадок. "" отключает.
    record_path: str = ""

    # =========================================================================
    #  СТРАТЕГИЯ
    # =========================================================================
    # "none"      — чистый лист, бот смотрит рынок и не торгует;
    # "certainty" — покупка почти-факта: YES около 0.98 в уже решённом раунде.
    strategy: str = "none"

    # --- размер и лестница ---------------------------------------------------
    # Полная позиция и ступени набора. Лестница набирает ВВЕРХ ПО УВЕРЕННОСТИ,
    # а не вниз по цене: следующая ступень берётся, только если риск не вырос.
    cert_full_size: float = 50.0
    cert_steps: tuple = (10.0, 10.0, 10.0, 20.0)
    # Пауза между ступенями. Смысл не в задержке, а в НЕЗАВИСИМОСТИ данных:
    # за это время приходят новые тики и новая книга, и вторая ступень
    # опирается не на то же самое измерение, что первая.
    cert_step_interval_s: float = 3.0
    # На сколько ужесточается порог риска с каждой ступенью. Чем больше денег
    # в позиции, тем выше должна быть уверенность, чтобы добавить ещё.
    cert_risk_tighten: float = 3.0
    # Насколько запасу разрешено просесть между ступенями, в сигмах.
    cert_z_slip: float = 0.3

    # --- порог входа ---------------------------------------------------------
    # Риск считается по flowbot/risk.py и лежит в 0..100. Вход при score <= это.
    cert_risk_max: float = 30.0
    # ЗАПАС В СИГМАХ — главная величина стратегии. При z=2.05 модель даёт
    # ровно 0.98, то есть ровно порог безубытка: покупать там нечего.
    # Прибыль начинается там, где z заметно больше.
    cert_z_min: float = 4.0        # ниже — вход запрещён
    cert_z_full: float = 6.0       # выше — по запасу риска нет
    cert_max_price: float = 0.985  # дороже забирать нечего
    cert_min_price: float = 0.960  # дешевле раунд не «почти решён»
    cert_max_spread_c: float = 5.0
    cert_min_depth_usd: float = 60.0
    # Окно входа. Раньше — раунд ещё живой; позже — ордер может не успеть.
    cert_min_seconds_left: float = 10.0
    cert_max_seconds_left: float = 120.0

    # --- измерения -----------------------------------------------------------
    cert_vol_fast_s: float = 10.0
    cert_vol_slow_s: float = 60.0
    # ЗАПАС НАДЁЖНОСТИ НА σ. В этом проекте измерено: оценка по MAD занижает
    # нужную для Φ величину в 1.4-1.6 раза на скачковых данных. Заниженная σ
    # завышает z, а завышенный z — прямая дорога к потере полной ставки.
    cert_sigma_safety: float = 1.5
    cert_sigma_floor: float = 0.2
    cert_biddrop_s: float = 5.0    # за сколько секунд смотреть уход бидов

    # --- защита от потери связи ----------------------------------------------
    # Старые данные — это торговля вслепую по цене, которой уже нет. Обрыв
    # потока изнутри выглядит как «рынок замер»: цены в книге те же, что были
    # в момент разрыва, и отличить одно от другого можно только по времени.
    cert_max_stale_s: float = 2.0        # цена монеты не обновлялась
    cert_max_book_stale_s: float = 5.0   # книга не менялась

    # --- аварийный выход -----------------------------------------------------
    # Выход НЕ по проценту: он проваливается до 0.50 и возвращается на 0.98
    # за секунду. Выходим по состоянию РЫНКА и только с подтверждением.
    cert_exit_z: float = 2.5           # запас упал ниже этого
    cert_exit_jump_mult: float = 1.5   # или запас < столько худших секунд
    cert_exit_confirm_ticks: int = 5   # столько тактов подряд
    cert_exit_confirm_s: float = 1.5   # и не меньше стольких секунд
    # У самого конца окна выходить некуда: книга тонкая, и держать до
    # расчёта при большом запасе безопаснее, чем отдавать по любой цене.
    cert_no_exit_last_s: float = 8.0

    # --- дневной риск --------------------------------------------------------
    # Одна потеря стоит 49 выигрышей. Поэтому лимит по потерям, а не по
    # деньгам: считать надо события, а не сумму.
    cert_max_day_losses: int = 1
    cert_max_day_loss_usd: float = 60.0
    cert_max_day_trades: int = 40

    # --- фиды ----------------------------------------------------------------
    no_pyth: bool = False
    no_binance: bool = False
    no_okx: bool = False
    no_bybit: bool = False
    no_polymarket_anchor: bool = False      # якорь уровня = поток Polymarket

    @classmethod
    def from_env(cls) -> "FlowConfig":
        ping = os.getenv("FLOW_PING_MS")
        return cls(
            asset=(_s("ASSET", "btc") or "btc").lower(),
            window_seconds=_i("WINDOW_SECONDS", 300),
            dry_run=_b("DRY_RUN", True),
            dry_run_balance=_f("DRY_RUN_BALANCE", 100.0),
            stake_usdc=_f("FLOW_STAKE_USDC", 1.0),
            min_order_usdc=_f("FLOW_MIN_ORDER_USDC", 1.0),
            max_round_usdc=_f("FLOW_MAX_ROUND_USDC", 25.0),
            max_sell_slip=_f("FLOW_MAX_SELL_SLIP", 0.0),
            ping_ms=float(ping) if ping not in (None, "") else None,
            assumed_rtt_ms=_f("FLOW_ASSUMED_RTT_MS", 90.0),
            order_process_ms=_f("FLOW_ORDER_PROCESS_MS", 45.0),
            order_timeout_s=_f("FLOW_ORDER_TIMEOUT_S", 10.0),
            simulate_latency=_b("FLOW_SIMULATE_LATENCY", True),
            error_cooldown_s=_f("FLOW_ERROR_COOLDOWN_S", 5.0),
            max_sell_fails=_i("FLOW_MAX_SELL_FAILS", 5),
            reconcile_s=_f("FLOW_RECONCILE_S", 20.0),
            leftover_warn_s=_f("FLOW_LEFTOVER_WARN_S", 120.0),
            settle_wait_s=_f("FLOW_SETTLE_WAIT_S", 20.0),
            settle_converge=_f("FLOW_SETTLE_CONVERGE", 0.90),
            run_duration_seconds=_f("RUN_DURATION_SECONDS", 86400.0),
            status_log_interval_seconds=_f("STATUS_LOG_INTERVAL_SECONDS", 0.1),
            tick_max_wait_seconds=_f("FLOW_TICK_MAX_WAIT_S", 0.05),
            max_consecutive_errors=_i("MAX_CONSECUTIVE_ERRORS", 12),
            book_levels=_i("FLOW_BOOK_LEVELS", 10),
            trade_log_csv=_s("FLOW_TRADE_LOG_CSV", "flowbot_trades.csv") or "",
            stats_path=_s("FLOW_STATS_PATH", "") or "",
            stats_enabled=_b("FLOW_STATS_ENABLED", True),
            record_path=_s("FLOW_RECORD_PATH", "") or "",
            strategy=(_s("FLOW_STRATEGY", "none") or "none").lower(),
            cert_full_size=_f("CERT_FULL_SIZE", 50.0),
            cert_steps=_tuple("CERT_STEPS", (10.0, 10.0, 10.0, 20.0)),
            cert_step_interval_s=_f("CERT_STEP_INTERVAL_S", 3.0),
            cert_risk_tighten=_f("CERT_RISK_TIGHTEN", 3.0),
            cert_z_slip=_f("CERT_Z_SLIP", 0.3),
            cert_risk_max=_f("CERT_RISK_MAX", 30.0),
            cert_z_min=_f("CERT_Z_MIN", 4.0),
            cert_z_full=_f("CERT_Z_FULL", 6.0),
            cert_max_price=_f("CERT_MAX_PRICE", 0.985),
            cert_min_price=_f("CERT_MIN_PRICE", 0.960),
            cert_max_spread_c=_f("CERT_MAX_SPREAD_C", 5.0),
            cert_min_depth_usd=_f("CERT_MIN_DEPTH_USD", 60.0),
            cert_min_seconds_left=_f("CERT_MIN_SECONDS_LEFT", 10.0),
            cert_max_seconds_left=_f("CERT_MAX_SECONDS_LEFT", 120.0),
            cert_vol_fast_s=_f("CERT_VOL_FAST_S", 10.0),
            cert_vol_slow_s=_f("CERT_VOL_SLOW_S", 60.0),
            cert_sigma_safety=_f("CERT_SIGMA_SAFETY", 1.5),
            cert_sigma_floor=_f("CERT_SIGMA_FLOOR", 0.2),
            cert_biddrop_s=_f("CERT_BIDDROP_S", 5.0),
            cert_max_stale_s=_f("CERT_MAX_STALE_S", 2.0),
            cert_max_book_stale_s=_f("CERT_MAX_BOOK_STALE_S", 5.0),
            cert_exit_z=_f("CERT_EXIT_Z", 2.5),
            cert_exit_jump_mult=_f("CERT_EXIT_JUMP_MULT", 1.5),
            cert_exit_confirm_ticks=_i("CERT_EXIT_CONFIRM_TICKS", 5),
            cert_exit_confirm_s=_f("CERT_EXIT_CONFIRM_S", 1.5),
            cert_no_exit_last_s=_f("CERT_NO_EXIT_LAST_S", 8.0),
            cert_max_day_losses=_i("CERT_MAX_DAY_LOSSES", 1),
            cert_max_day_loss_usd=_f("CERT_MAX_DAY_LOSS_USD", 60.0),
            cert_max_day_trades=_i("CERT_MAX_DAY_TRADES", 40),
            no_pyth=_b("NO_PYTH", False),
            no_binance=_b("NO_BINANCE", False),
            no_okx=_b("NO_OKX", False),
            no_bybit=_b("NO_BYBIT", False),
            no_polymarket_anchor=_b("NO_POLYMARKET_ANCHOR", False),
        )

    def cert_max_round_ok(self) -> bool:
        return self.max_round_usdc >= self.cert_full_size - 1e-9

    def validate(self) -> None:
        """Проверка ИНФРАСТРУКТУРНЫХ настроек.

        Торговых порогов здесь нет и проверять их нечего. Когда у новой
        стратегии появятся свои — им нужна своя проверка, и лучше отдельная:
        молчаливо неверный порог означает бота, который не торгует и никак
        об этом не сообщает.
        """
        errs = []
        if self.stake_usdc <= 0:
            errs.append("FLOW_STAKE_USDC должен быть > 0")
        if self.min_order_usdc < 0:
            errs.append("FLOW_MIN_ORDER_USDC не может быть отрицательным")
        if self.strategy not in ("none", "certainty"):
            errs.append(f"FLOW_STRATEGY должен быть none или certainty, "
                        f"а не {self.strategy!r}")
        if self.strategy == "certainty":
            if not self.cert_steps:
                errs.append("CERT_STEPS пуст — набирать позицию нечем")
            elif any(x <= 0 for x in self.cert_steps):
                errs.append(f"CERT_STEPS содержит неположительную ступень: "
                            f"{self.cert_steps}")
            elif min(self.cert_steps) < self.min_order_usdc:
                errs.append(
                    f"ступень ${min(self.cert_steps):.2f} меньше минимума "
                    f"площадки ${self.min_order_usdc:.2f} — такой ордер будет "
                    f"отвергнут или молча округлён вверх")
            if abs(sum(self.cert_steps) - self.cert_full_size) > 1e-6:
                errs.append(
                    f"сумма ступеней {sum(self.cert_steps):.0f} != полной "
                    f"позиции {self.cert_full_size:.0f} — набор либо не "
                    f"дойдёт до цели, либо её перебьёт")
            if self.cert_z_min < 2.06:
                errs.append(
                    f"CERT_Z_MIN={self.cert_z_min:.2f} — при z=2.05 модель "
                    f"даёт ровно 0.98, то есть ровно порог безубытка. "
                    f"Покупать там нечего: нужен запас БОЛЬШЕ.")
            if self.cert_z_full < self.cert_z_min:
                errs.append("CERT_Z_FULL должен быть >= CERT_Z_MIN")
            if self.cert_exit_z >= self.cert_z_min:
                errs.append(
                    f"CERT_EXIT_Z={self.cert_exit_z:.1f} >= "
                    f"CERT_Z_MIN={self.cert_z_min:.1f} — бот продавал бы "
                    f"позицию сразу после покупки: условие выхода выполнено "
                    f"уже в момент входа")
            if not (0 < self.cert_max_price <= 1):
                errs.append("нужно 0 < CERT_MAX_PRICE <= 1")
            if self.cert_min_price >= self.cert_max_price:
                errs.append("CERT_MIN_PRICE должен быть < CERT_MAX_PRICE — "
                            "иначе полоса цен пуста и вход невозможен")
            if self.cert_min_seconds_left >= self.cert_max_seconds_left:
                errs.append("CERT_MIN_SECONDS_LEFT должен быть < "
                            "CERT_MAX_SECONDS_LEFT — иначе окно входа пусто")
            if self.cert_max_seconds_left > self.window_seconds:
                errs.append(
                    f"CERT_MAX_SECONDS_LEFT={self.cert_max_seconds_left:.0f} "
                    f"больше длины окна {self.window_seconds}с")
            if self.cert_exit_confirm_ticks < 1:
                errs.append("CERT_EXIT_CONFIRM_TICKS должен быть >= 1: выход "
                            "по одному измерению — это выход по шуму")
            if self.cert_max_round_ok() is False:
                errs.append(
                    f"FLOW_MAX_ROUND_USDC={self.max_round_usdc:.0f} меньше "
                    f"полной позиции ${self.cert_full_size:.0f} — лестница "
                    f"упрётся в потолок раунда и позиция останется неполной")
        if self.max_round_usdc < self.stake_usdc:
            errs.append(
                "FLOW_MAX_ROUND_USDC должен быть >= FLOW_STAKE_USDC, иначе "
                "даже первый вход не пройдёт потолок раунда")
        if self.window_seconds <= 0:
            errs.append("WINDOW_SECONDS должен быть > 0")
        if self.order_timeout_s <= 0:
            errs.append("FLOW_ORDER_TIMEOUT_S должен быть > 0")
        if not (0 < self.settle_converge <= 1):
            errs.append("нужно 0 < FLOW_SETTLE_CONVERGE <= 1")
        if errs:
            raise ValueError("Некорректная конфигурация:\n  - "
                             + "\n  - ".join(errs))
