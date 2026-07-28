"""Конфигурация flowbot — пороги стратегии, размеры ставок, пинг, рантайм.

Всё грузится из окружения / `.env` (совместимо с btc_bot.config), поэтому
пресеты можно держать в отдельном `.env`-файле (см. flow_btc.env). CLI-флаги
в flowbot/run.py перекрывают эти значения.

Единицы:
  * burst_z — всплеск скорости цены, НОРМИРОВАННЫЙ на волатильность
    (σ-единицы). Он безразмерный, поэтому пороги не зависят от цены монеты.
  * *_bps — доля от цены в базисных пунктах (1 б.п. = 0.01%).
  * цены токенов Up/Down — это и есть «проценты» (0.56 = 56%).
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


@dataclass
class FlowConfig:
    # --- монета / рынок ------------------------------------------------------
    asset: str = "btc"
    window_seconds: int = 300               # 5-минутный рынок

    # --- размеры ставок ------------------------------------------------------
    stake_usdc: float = 1.0                 # обычный вход ($1, как просил юзер)
    flip_size_usdc: float = 3.5             # разворотная ставка ($3-4)
    # Лимита ставок в окне НЕТ: после выхода можно снова войти на новом всплеске.

    # --- сигнал скорости (всплеск движения цены BTC) -------------------------
    momentum_horizon_s: float = 1.0         # окно, на котором меряем всплеск
    entry_burst_z: float = 2.5              # |z| >= это => «резкое движение»
    entry_min_move_bps: float = 1.0         # и абсолютный пол: |Δцены| в б.п.
    # Пол волатильности (доля цены, б.п. за √сек). На «плоском» прогреве σ может
    # схлопнуться почти в ноль и z взлетит до бесконечности (ложный всплеск) —
    # поэтому σ снизу ограничена этим полом. ~0.2 б.п. от 64k BTC ≈ $1.3.
    min_sigma_bps: float = 0.2

    # --- диапазон входа по «проценту» токена --------------------------------
    # Покупаем сторону, куда пошла цена, только если её процент оставляет
    # запас для роста в нашу сторону.
    entry_price_min: float = 0.20           # не берём почти-ноль (шум)
    entry_price_max: float = 0.90           # оставляем ход до 1.0
    # Предосторожность №1 (не успели купить по доскачковому проценту):
    # готовы «догнать» ask, но не дороже, чем ref + chase_cents (1-2 цента).
    chase_cents: float = 0.02
    chase_lookback_s: float = 1.5           # «доскачковый» ask — это ask столько сек назад

    # --- удержание / выход ---------------------------------------------------
    min_hold_s: float = 0.4                 # не дёргаемся первые доли секунды
    hold_burst_z: float = 0.3               # движение «ещё идёт», пока z в нашу сторону >= это
    momentum_fade_grace_s: float = 0.8      # выходим, только если фейд держится столько
    token_retrace_exit: float = 0.03        # выход, если процент откатился на это от пика
    take_profit_price: float = 0.95         # глубоко в деньгах => держим до расчёта
    settle_hold_s: float = 6.0              # в конце окна: выигрываем => держим до сеттла

    # --- разворот (предосторожность №2) --------------------------------------
    flip_enabled: bool = True
    flip_burst_z: float = 3.5               # сильный всплеск ПРОТИВ нас
    flip_min_move_bps: float = 2.0

    # --- подтверждение книгой (поток заявок) --------------------------------
    # Мягкое вето: не входим, если по нужной стороне сильное давление ПРОТИВ.
    flow_confirm: bool = True
    flow_veto: float = -0.6                 # имбаланс стороны < это => пропуск
    flow_window_s: float = 3.0              # окно для имбаланса bid/ask и потока сделок

    # --- пинг / задержка (VPS в Цюрихе) --------------------------------------
    # Если ping_ms задан — используем его. Иначе замеряем RTT до CLOB вживую.
    ping_ms: Optional[float] = None
    # Ориентир для Цюрих -> Polymarket (инфраструктура в US-East): RTT ~90мс.
    assumed_rtt_ms: float = 90.0
    # Внутренняя обработка ордера (подпись + матчинг-движок) сверх сети.
    order_process_ms: float = 45.0
    # В dry-run симулируем задержку исполнения этой долей RTT + обработка.
    simulate_latency: bool = True

    # =========================================================================
    #  СКАЧКОВАЯ стратегия (4-я система, flowbot.jump) — пороги в ДОЛЛАРАХ
    # =========================================================================
    # ЧТО СЛУЖИТ ТРИГГЕРОМ ВХОДА. Три режима, всё остальное (лестница,
    # фиксация, потолки) у них общее — чтобы сравнение меняло РОВНО одну вещь.
    #
    #   "jump" — скачок цены в долларах. Исходная идея. Известный изъян:
    #            скачок не входит ни в один член EV = p − ask, поэтому он и
    #            не нужен (сигнал бывает без скачка), и не достаточен
    #            (скачок на 15σ от таргета не двигает процент вообще).
    #   "edge" — запас цены: Phi(z) − ask по обеим сторонам, берём лучшую.
    #            Прямо то, что стоит в EV. Скачок падает до уровня признака,
    #            который потом померяет features.py.
    #   "lag"  — отставание якоря: Phi(z по НАШЕЙ цене) − Phi(z по цене
    #            Polymarket). Отвечает не только «книга дешевле справедливого»,
    #            но и ПОЧЕМУ (она считает по устаревшей цене) и НАСКОЛЬКО
    #            это протянет (~288мс). Строже edge: требует ещё и запас.
    jump_entry_mode: str = "jump"
    # Порог режима "lag" в центах: на сколько книга обязана переоцениться,
    # чтобы вход имел смысл.
    jump_lag_min_cents: float = 3.0
    # Якорь Polymarket обновляется рывками. Если последняя его цена старше
    # этого, разрыв «наша − pm» показывает не опережение, а дырку в потоке.
    jump_lag_max_age_ms: float = 1500.0
    # Здесь «резкое движение» меряется не в σ, а прямо в долларах цены монеты,
    # как просил юзер: «скачок 5 долларов» / «скачок 15 долларов».
    # КАК ловим скачок:
    #   "swing"  — от локального дна/пика. Цена ушла на $5 от экстремума —
    #              покупаем в тот же тик, сколько бы времени движение ни
    #              заняло. Никакого окна, мониторинг непрерывный. (деф.)
    #   "window" — классика: сравниваем цену сейчас и jump_window_s назад.
    #              Движение, растянувшееся дольше окна, будет пропущено.
    jump_trigger_mode: str = "swing"
    # Как далеко назад может лежать экстремум (только для режима swing).
    # Слишком большое значение = дном считается начало раунда, и «скачком»
    # окажется медленный дрейф; слишком малое = вернётся эффект окна.
    jump_swing_lookback_s: float = 60.0
    jump_window_s: float = 3.0              # окно режима "window"
    jump_small_usd: float = 5.0             # дорогая дорожка (A): скачок >= $5
    jump_big_usd: float = 15.0              # дешёвая дорожка (B): скачок >= $15
    # Граница «дорого/дёшево» по проценту стороны. Строго: ask >= это -> A.
    jump_price_split: float = 0.51

    # --- подстройка порогов под живость рынка --------------------------------
    # Порог в фиксированных долларах верен только при той волатильности, при
    # которой его подбирали. Сдвиг процента от скачка J равен примерно
    # J*phi(z)/(sigma*sqrt(t)) — то есть ОБРАТНО пропорционален sigma. Когда
    # рынок просыпается, тот же $5 двигает процент в разы слабее, и порог надо
    # поднимать ровно во столько же раз: нужный скачок ~ линеен по sigma.
    #   при sigma 0.9  -> нужно ~$4     (тихое воскресенье)
    #   при sigma 6.0  -> нужно ~$28    (живой рынок)
    jump_adaptive: bool = True
    jump_sigma_ref: float = 0.9             # sigma, при которой калиброваны $5/$15
    jump_scale_min: float = 1.0             # ниже базовых порогов не опускаемся
    jump_scale_max: float = 8.0             # и не разгоняемся до бесконечности

    # Дорожка B (дешёвая сторона): как далеко от таргета ещё имеет смысл лезть.
    # В СИГМАХ, а не в долларах: «далеко» зависит от волатильности и времени.
    # 3 сигмы при sigma 0.9 и 200с — это $38; при sigma 6.0 — уже $255.
    jump_max_target_sigmas: float = 3.0
    # Запасной предел в долларах — работает, только пока sigma неизвестна.
    jump_max_target_dist_usd: float = 100.0
    jump_stake_usdc: float = 1.0            # первая ставка в лестнице
    # Минимальный ОЖИДАЕМЫЙ сдвиг «процента» от скачка, в центах.
    # Считается по модели случайного блуждания: P = Phi((цена-таргет)/(sigma*sqrt(t))).
    # Чем дальше цена от таргета, тем меньше этот сдвиг — на 3+ сигмах процент
    # физически не двигается, сколько бы монета ни прыгала, потому что исход
    # раунда уже решён. Вход туда гарантированно съедается спредом (~1 цент),
    # поэтому такие сигналы отсекаем. 0 — фильтр выключен.
    jump_min_shift_cents: float = 2.0
    # ЗАПАС ЦЕНЫ: насколько справедливая вероятность выше того, что просят
    # в книге. edge = P(наша сторона) − ask. Это единственная проверка,
    # которая отвечает на вопрос «а не переплачиваем ли мы»: скачок говорит
    # КУДА пошла цена, но ничего не говорит о том, не заложен ли он уже в
    # процент. Порог должен покрывать половину спреда (при спреде 5¢ это
    # 2.5¢) плюс запас на пинг. 0 — выключить.
    jump_min_edge_cents: float = 3.0
    # Пауза после сделки, чтобы одно и то же движение не открыло вторую
    # позицию. В режиме swing экстремум и так переставляется после филла,
    # поэтому паузе хватает секунды.
    jump_reentry_cooldown_s: float = 1.0

    # --- лестница добора (докупаем противоположную сторону) ------------------
    # Когда наш процент проваливается ниже jump_price_split, берём другую
    # сторону в размере, который перекрывает ВЕСЬ вложенный минус и выводит
    # в плюс: шэров = (потрачено + прибыль) / (1 - цена входа).
    jump_ladder_enabled: bool = True
    jump_ladder_profit_usdc: float = 0.50   # какой «+» закладываем в добор
    jump_ladder_grace_s: float = 0.6        # % должен пробыть под сплитом столько
    # Насколько ниже СВОЕЙ цены входа должна уйти нога, чтобы считаться
    # провалившейся. Нужно для дешёвой дорожки B: там вход по 0.05 сам по себе
    # ниже сплита, и без этого условия лестница срабатывала бы сразу.
    jump_ladder_loss: float = 0.02
    # ПРЕДОХРАНИТЕЛИ лестницы. Без них мартингейл растёт по экспоненте и один
    # неудачный раунд съедает депозит: (потрачено+прибыль)/(1-цена).
    # 0 = БЕЗ ОГРАНИЧЕНИЙ. Это безопасно только потому, что при развороте
    # старая нога продаётся: долг сходится (1.00 -> 0.45 -> 0.29 -> 0.24),
    # а не удваивается. Поставишь число — будет жёсткий предел ступеней.
    jump_max_ladder_legs: int = 0
    jump_max_round_usdc: float = 25.0       # потолок вложений в один раунд
    jump_max_leg_price: float = 0.95        # дороже — добор бессмыслен (шэры -> ∞)

    # --- расчёт в конце раунда -----------------------------------------------
    # Победителя определяем по книге, но сразу после конца окна она ещё не
    # схлопнулась: бывает Up 0.62 / Down 0.35. Объявить по такой книге
    # победителя — значит записать «выиграли $1 за шэр» по ставке 62/38.
    # Поэтому ЖДЁМ схлопывания и только тогда закрываем раунд.
    jump_settle_converge: float = 0.90      # одна сторона должна дойти до этого
    jump_settle_wait_s: float = 12.0        # сколько ждать после конца окна

    # --- фиксация прибыли ----------------------------------------------------
    # «купил по 0.05, выросли до 0.30, а рост кончился / до конца 5 секунд —
    #  продаёт». Работает только когда мы в плюсе.
    jump_tp_enabled: bool = True
    jump_tp_min_gain: float = 0.10          # в плюсе минимум на столько от входа
    # ТРЕЙЛИНГ-СТОП: на сколько разрешено откатиться от ПИКА, прежде чем
    # закрыться. Это главный выход, и раньше его не было вовсе.
    #
    # Без него защиты у достигнутой прибыли не оставалось ни одной. Остальные
    # четыре триггера требуют совпадений, которых на откате не бывает: frozen
    # ждёт 8с застоя (а процент падает за 2), quiet требует, чтобы замерла
    # МОНЕТА, faded — чтобы монета ещё и развернулась. Плюс проверка «мы в
    # плюсе» стояла ПЕРЕД всеми ними, поэтому как только прибыль истончалась
    # ниже jump_tp_min_gain, позицию переставало смотреть вообще — ровно
    # тогда, когда защищать её нужнее всего. Итог: вход 0.53, пик 0.70,
    # выход в −0.07 через лестницу.
    #
    # Взводится, только когда нога хоть раз стоила на jump_tp_min_gain дороже
    # входа: иначе обычное дрожание бида сразу после покупки закрывало бы
    # каждую сделку в убыток.
    jump_tp_trail: float = 0.03
    jump_tp_deadline_s: float = 5.0         # «до окончания 5 секунд» — фиксируем
    jump_tp_stall_retrace: float = 0.03     # откат % от пика = рост кончился
    jump_tp_flow_against: float = -0.20     # «люди смотрят в другую сторону»
    # «Проценты перестали подниматься»: пик не обновлялся столько секунд.
    jump_tp_stall_s: float = 8.0
    # «Цена перестала резко двигаться и стоит на месте»: движение монеты не
    # больше этого. Вместе с застоем пика закрывает случай «вышли в плюс и
    # досидели до разворота».
    jump_tp_quiet_usd: float = 1.5

    # --- рантайм -------------------------------------------------------------
    dry_run: bool = True
    run_duration_seconds: float = 86400.0
    # Как часто ПЕЧАТАТЬ строку состояния. На решения не влияет: они
    # принимаются на каждом обновлении книги (см. tick_max_wait_seconds).
    # Секунда была слишком медленной для глаза — панель отставала от соседних.
    status_log_interval_seconds: float = 0.1
    # Запасной такт, если книга молчит. Обычно цикл будит UPDATE_EVENT,
    # то есть решение принимается в тот же миг, когда пришла цена или заявка.
    tick_max_wait_seconds: float = 0.05
    max_consecutive_errors: int = 12
    trade_log_csv: str = "flowbot_trades.csv"   # "" отключает
    # Запись всего, что видел бот, в JSONL — сырьё для проигрывания и подбора
    # порогов на РЕАЛЬНОЙ истории вместо догадок. "" отключает.
    record_path: str = ""

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
            stake_usdc=_f("FLOW_STAKE_USDC", 1.0),
            flip_size_usdc=_f("FLOW_FLIP_SIZE_USDC", 3.5),
            momentum_horizon_s=_f("FLOW_MOMENTUM_HORIZON_S", 1.0),
            entry_burst_z=_f("FLOW_ENTRY_BURST_Z", 2.5),
            entry_min_move_bps=_f("FLOW_ENTRY_MIN_MOVE_BPS", 1.0),
            min_sigma_bps=_f("FLOW_MIN_SIGMA_BPS", 0.2),
            entry_price_min=_f("FLOW_ENTRY_PRICE_MIN", 0.20),
            entry_price_max=_f("FLOW_ENTRY_PRICE_MAX", 0.90),
            chase_cents=_f("FLOW_CHASE_CENTS", 0.02),
            chase_lookback_s=_f("FLOW_CHASE_LOOKBACK_S", 1.5),
            min_hold_s=_f("FLOW_MIN_HOLD_S", 0.4),
            hold_burst_z=_f("FLOW_HOLD_BURST_Z", 0.3),
            momentum_fade_grace_s=_f("FLOW_FADE_GRACE_S", 0.8),
            token_retrace_exit=_f("FLOW_TOKEN_RETRACE_EXIT", 0.03),
            take_profit_price=_f("FLOW_TAKE_PROFIT_PRICE", 0.95),
            settle_hold_s=_f("FLOW_SETTLE_HOLD_S", 6.0),
            flip_enabled=_b("FLOW_FLIP_ENABLED", True),
            flip_burst_z=_f("FLOW_FLIP_BURST_Z", 3.5),
            flip_min_move_bps=_f("FLOW_FLIP_MIN_MOVE_BPS", 2.0),
            flow_confirm=_b("FLOW_FLOW_CONFIRM", True),
            flow_veto=_f("FLOW_FLOW_VETO", -0.6),
            flow_window_s=_f("FLOW_FLOW_WINDOW_S", 3.0),
            jump_entry_mode=(_s("JUMP_ENTRY_MODE", "jump") or "jump").lower(),
            jump_lag_min_cents=_f("JUMP_LAG_MIN_CENTS", 3.0),
            jump_lag_max_age_ms=_f("JUMP_LAG_MAX_AGE_MS", 1500.0),
            jump_trigger_mode=(_s("JUMP_TRIGGER_MODE", "swing") or "swing").lower(),
            jump_swing_lookback_s=_f("JUMP_SWING_LOOKBACK_S", 60.0),
            jump_window_s=_f("JUMP_WINDOW_S", 3.0),
            jump_reentry_cooldown_s=_f("JUMP_REENTRY_COOLDOWN_S", 1.0),
            jump_small_usd=_f("JUMP_SMALL_USD", 5.0),
            jump_big_usd=_f("JUMP_BIG_USD", 15.0),
            jump_price_split=_f("JUMP_PRICE_SPLIT", 0.51),
            jump_adaptive=_b("JUMP_ADAPTIVE", True),
            jump_sigma_ref=_f("JUMP_SIGMA_REF", 0.9),
            jump_scale_min=_f("JUMP_SCALE_MIN", 1.0),
            jump_scale_max=_f("JUMP_SCALE_MAX", 8.0),
            jump_max_target_sigmas=_f("JUMP_MAX_TARGET_SIGMAS", 3.0),
            jump_max_target_dist_usd=_f("JUMP_MAX_TARGET_DIST_USD", 100.0),
            jump_stake_usdc=_f("JUMP_STAKE_USDC", 1.0),
            jump_min_shift_cents=_f("JUMP_MIN_SHIFT_CENTS", 2.0),
            jump_min_edge_cents=_f("JUMP_MIN_EDGE_CENTS", 3.0),
            jump_ladder_enabled=_b("JUMP_LADDER_ENABLED", True),
            jump_ladder_profit_usdc=_f("JUMP_LADDER_PROFIT_USDC", 0.50),
            jump_ladder_grace_s=_f("JUMP_LADDER_GRACE_S", 0.6),
            jump_ladder_loss=_f("JUMP_LADDER_LOSS", 0.02),
            jump_max_ladder_legs=_i("JUMP_MAX_LADDER_LEGS", 0),
            jump_max_round_usdc=_f("JUMP_MAX_ROUND_USDC", 25.0),
            jump_max_leg_price=_f("JUMP_MAX_LEG_PRICE", 0.95),
            jump_settle_converge=_f("JUMP_SETTLE_CONVERGE", 0.90),
            jump_settle_wait_s=_f("JUMP_SETTLE_WAIT_S", 12.0),
            jump_tp_enabled=_b("JUMP_TP_ENABLED", True),
            jump_tp_min_gain=_f("JUMP_TP_MIN_GAIN", 0.10),
            jump_tp_trail=_f("JUMP_TP_TRAIL", 0.03),
            jump_tp_deadline_s=_f("JUMP_TP_DEADLINE_S", 5.0),
            jump_tp_stall_retrace=_f("JUMP_TP_STALL_RETRACE", 0.03),
            jump_tp_flow_against=_f("JUMP_TP_FLOW_AGAINST", -0.20),
            jump_tp_stall_s=_f("JUMP_TP_STALL_S", 8.0),
            jump_tp_quiet_usd=_f("JUMP_TP_QUIET_USD", 1.5),
            ping_ms=(float(ping) if ping not in (None, "") else None),
            assumed_rtt_ms=_f("FLOW_ASSUMED_RTT_MS", 90.0),
            order_process_ms=_f("FLOW_ORDER_PROCESS_MS", 45.0),
            simulate_latency=_b("FLOW_SIMULATE_LATENCY", True),
            dry_run=_b("DRY_RUN", True),
            run_duration_seconds=_f("RUN_DURATION_SECONDS", 86400.0),
            status_log_interval_seconds=_f("STATUS_LOG_INTERVAL_SECONDS", 0.1),
            tick_max_wait_seconds=_f("FLOW_TICK_MAX_WAIT_S", 0.05),
            max_consecutive_errors=_i("MAX_CONSECUTIVE_ERRORS", 12),
            trade_log_csv=(
                "flowbot_trades.csv" if os.getenv("FLOW_TRADE_LOG_CSV") is None
                else os.getenv("FLOW_TRADE_LOG_CSV").strip()
            ),
            record_path=(_s("JUMP_RECORD_PATH", "") or ""),
            no_pyth=_b("FLOW_NO_PYTH", False),
            no_binance=_b("FLOW_NO_BINANCE", False),
            no_okx=_b("FLOW_NO_OKX", False),
            no_bybit=_b("FLOW_NO_BYBIT", False),
            no_polymarket_anchor=_b("FLOW_NO_PM_ANCHOR", False),
        )

    def validate(self) -> None:
        errs = []
        if self.stake_usdc <= 0:
            errs.append("FLOW_STAKE_USDC должен быть > 0")
        if self.flip_size_usdc <= 0:
            errs.append("FLOW_FLIP_SIZE_USDC должен быть > 0")
        if not (0 < self.entry_price_min <= self.entry_price_max < 1):
            errs.append(
                "нужно 0 < FLOW_ENTRY_PRICE_MIN <= FLOW_ENTRY_PRICE_MAX < 1"
            )
        if self.entry_burst_z <= 0:
            errs.append("FLOW_ENTRY_BURST_Z должен быть > 0")
        if self.momentum_horizon_s <= 0:
            errs.append("FLOW_MOMENTUM_HORIZON_S должен быть > 0")
        if self.chase_cents < 0:
            errs.append("FLOW_CHASE_CENTS должен быть >= 0")
        if self.flip_enabled and self.flip_burst_z < self.entry_burst_z:
            errs.append(
                "FLOW_FLIP_BURST_Z (разворот) должен быть >= FLOW_ENTRY_BURST_Z, "
                "иначе разворот срабатывал бы легче обычного входа"
            )
        if errs:
            raise ValueError("Некорректная конфигурация flowbot:\n  - "
                             + "\n  - ".join(errs))

    def validate_jump(self) -> None:
        """Проверка порогов СКАЧКОВОЙ стратегии (4-я система).

        Отдельно от validate(), потому что jump-стратегия не использует ни
        burst_z, ни разворот flowbot — у неё свои пороги в долларах.
        """
        errs = []
        if self.jump_stake_usdc <= 0:
            errs.append("JUMP_STAKE_USDC должен быть > 0")
        if self.jump_entry_mode not in ("jump", "edge", "lag"):
            errs.append(f"JUMP_ENTRY_MODE должен быть jump, edge или lag, "
                        f"а не {self.jump_entry_mode!r}")
        if self.jump_entry_mode == "edge" and self.jump_min_edge_cents <= 0:
            errs.append("режим edge требует JUMP_MIN_EDGE_CENTS > 0 — "
                        "запас и есть его единственный триггер")
        if self.jump_entry_mode == "lag":
            if self.jump_lag_min_cents <= 0:
                errs.append("режим lag требует JUMP_LAG_MIN_CENTS > 0")
            if self.jump_min_edge_cents <= 0:
                errs.append("режим lag требует JUMP_MIN_EDGE_CENTS > 0: "
                            "отставание якоря говорит, что книга переоценится, "
                            "но не о том, что мы не переплачиваем")
        if self.jump_trigger_mode not in ("swing", "window"):
            errs.append(f"JUMP_TRIGGER_MODE должен быть swing или window, "
                        f"а не {self.jump_trigger_mode!r}")
        if self.jump_swing_lookback_s <= 0:
            errs.append("JUMP_SWING_LOOKBACK_S должен быть > 0")
        if self.jump_window_s <= 0:
            errs.append("JUMP_WINDOW_S должен быть > 0")
        if self.jump_small_usd <= 0:
            errs.append("JUMP_SMALL_USD должен быть > 0")
        if self.jump_big_usd < self.jump_small_usd:
            errs.append(
                "JUMP_BIG_USD (дешёвая сторона) должен быть >= JUMP_SMALL_USD: "
                "на дешёвой стороне мы требуем БОЛЬШИЙ скачок, а не меньший"
            )
        if not (0 < self.jump_price_split < 1):
            errs.append("нужно 0 < JUMP_PRICE_SPLIT < 1")
        if self.jump_max_target_dist_usd <= 0:
            errs.append("JUMP_MAX_TARGET_DIST_USD должен быть > 0")
        if not (0 < self.jump_max_leg_price < 1):
            errs.append("нужно 0 < JUMP_MAX_LEG_PRICE < 1")
        if self.jump_ladder_enabled:
            if self.jump_max_ladder_legs < 0:
                errs.append("JUMP_MAX_LADDER_LEGS должен быть >= 0")
            if self.jump_max_round_usdc < self.jump_stake_usdc:
                errs.append(
                    "JUMP_MAX_ROUND_USDC должен быть >= JUMP_STAKE_USDC, "
                    "иначе даже первый вход не пройдёт потолок раунда"
                )
        if self.jump_tp_enabled and self.jump_tp_min_gain <= 0:
            errs.append("JUMP_TP_MIN_GAIN должен быть > 0")
        if errs:
            raise ValueError("Некорректная конфигурация jump-стратегии:\n  - "
                             + "\n  - ".join(errs))
