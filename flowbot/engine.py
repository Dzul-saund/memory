"""Базовый движок: фиды, окно раунда, поток книги, цикл, баланс, лог сделок.

ЗДЕСЬ НЕТ ТОРГОВЫХ РЕШЕНИЙ. Этот класс отвечает только за то, чтобы бот жил:
поднимает фиды цены, слушает поток CLOB, отслеживает границы 5-минутного
окна, крутит главный цикл и не падает от одной ошибки. Что делать с рынком —
решает стратегия, а исполняет `flowbot.trading.TradingEngine`.

Разделение намеренное: инфраструктуру можно тестировать и чинить, не трогая
логику, а логику переписывать с нуля, не боясь сломать сеть и ордера.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import fast_monitor
from book_monitor import CLOB_WS, WINDOW_SECONDS, discover_market, loads

from btc_bot.config import Config as BtcConfig
from btc_bot.trader import build_trader
from btc_bot.tradelog import TradeLogger

from .config import FlowConfig
from .latency import LatencyModel
from .signals import BookEngine, PriceEngine

try:
    import websockets
except ImportError:  # pragma: no cover
    raise SystemExit("Установите зависимость:  pip install websockets")


def now_ms() -> float:
    return time.time() * 1000.0


class FlowEngine:
    """Инфраструктура бота. Торговую часть добавляет подкласс."""

    def __init__(self, cfg: FlowConfig, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.log = logger or logging.getLogger("flowbot")

        self.price = PriceEngine(cfg)
        self.book = BookEngine(cfg)
        self.latency = LatencyModel(cfg, self.log)

        # Трейдер (live/dry) поверх btc_bot: тот же CLOB-клиент и баланс.
        btc_cfg = BtcConfig.from_env()
        btc_cfg.dry_run = cfg.dry_run
        btc_cfg.asset = cfg.asset
        self._btc_cfg = btc_cfg
        self.trader = build_trader(btc_cfg, self.log)
        self.tradelog = TradeLogger(cfg.trade_log_csv)

        # {up, down, window_ts, end_ts, slug, question}
        self.market: Optional[dict] = None
        self.busy = False                         # ордер в полёте
        self._stop = False
        self.stop_reason: Optional[str] = None
        self.consec_err = 0

        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.n_trades = 0

        self._balance: Optional[float] = None
        self._balance_ts = 0.0
        self._last_status = 0.0

    # ======================================================================
    #  Запуск
    # ======================================================================
    def run(self) -> None:
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            self.stop_reason = "остановлено пользователем (Ctrl-C)"
            self._summary()

    async def run_async(self) -> None:
        self._banner()
        fast_monitor.COIN = fast_monitor.COINS[self.cfg.asset]

        # Пинг меряем в фоне — фиды тем временем прогреваются.
        asyncio.create_task(self._measure_ping())

        feeds = fast_monitor.feed_tasks(
            no_pyth=self.cfg.no_pyth, no_binance=self.cfg.no_binance,
            no_okx=self.cfg.no_okx, no_bybit=self.cfg.no_bybit,
            no_pm=self.cfg.no_polymarket_anchor,
        )
        bg = [asyncio.create_task(c) for c in feeds]
        bg.append(asyncio.create_task(self._book_loop()))
        for extra in self._extra_tasks():
            bg.append(asyncio.create_task(extra))

        deadline = time.time() + self.cfg.run_duration_seconds
        try:
            await self._run_loop(deadline)
        finally:
            self._stop = True
            if not self.busy:
                self._settle_open_position("остановка движка")
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
            self._summary()

    def _extra_tasks(self):
        """Дополнительные фоновые корутины подкласса (по умолчанию — нет)."""
        return []

    async def _measure_ping(self) -> None:
        await asyncio.to_thread(self.latency.measure, "clob.polymarket.com",
                                443, 4, 2.5)
        self.log.info(self.latency.describe())

    # ======================================================================
    #  Поток книги — по одному 5-минутному окну за раз
    # ======================================================================
    async def _book_loop(self) -> None:
        while not self._stop:
            try:
                market = await discover_market(self.cfg.asset)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("поиск рынка: %s; повтор через 2с", exc)
                await asyncio.sleep(2)
                continue
            self._enter_window(market)
            await self._stream_window(market)
            if not self.busy:
                self._settle_open_position("окно закончилось")

    def _enter_window(self, market: dict) -> None:
        end_ts = market["window_ts"] + WINDOW_SECONDS
        self.market = {**market, "end_ts": end_ts}
        self.book.set_tokens(market["up"], market["down"])
        self.log.info(
            "── окно %s | до %s | Up=%s… Down=%s… ──",
            market["slug"],
            time.strftime("%H:%M:%S", time.localtime(end_ts)),
            str(market["up"])[:10], str(market["down"])[:10],
        )

    def _stream_deadline(self, end_ts: float) -> float:
        """До какого момента слушать книгу. По умолчанию — секунда после окна."""
        return end_ts + 1.0

    def _book_resolved(self) -> bool:
        """Книга уже схлопнулась к 0/1? База не умеет, подкласс может."""
        return False

    async def _stream_window(self, market: dict) -> None:
        end_ts = market["window_ts"] + WINDOW_SECONDS
        deadline = self._stream_deadline(end_ts)
        sub = json.dumps({"assets_ids": [market["up"], market["down"]],
                          "type": "market"})
        backoff = 0.5
        while time.time() < deadline and not self._stop:
            try:
                async with websockets.connect(
                        CLOB_WS, compression=None, open_timeout=8,
                        user_agent_header="Mozilla/5.0") as ws:
                    await ws.send(sub)
                    backoff = 0.5
                    last_ping = time.time()
                    while time.time() < deadline and not self._stop:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if time.time() - last_ping >= 5:
                                await ws.send("PING")
                                last_ping = time.time()
                            continue
                        self._ingest(raw)
                        # Окно кончилось и книга уже показала победителя —
                        # больше ждать нечего.
                        if time.time() >= end_ts and self._book_resolved():
                            return
            except Exception:  # noqa: BLE001 - обрыв потока штатен
                if time.time() >= deadline:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _ingest(self, raw) -> None:
        if not isinstance(raw, str) or not raw.startswith(("{", "[")):
            return
        try:
            data = loads(raw)
        except Exception:  # noqa: BLE001
            return
        recv = time.time()
        events = data if isinstance(data, list) else [data]
        changed = False
        for ev in events:
            if isinstance(ev, dict) and self.book.on_event(ev, recv):
                changed = True
        if changed:
            fast_monitor.UPDATE_EVENT.set()   # разбудить цикл решений

    # ======================================================================
    #  Главный цикл
    # ======================================================================
    async def _run_loop(self, deadline: float) -> None:
        while time.time() < deadline and not self._stop:
            try:
                await asyncio.wait_for(fast_monitor.UPDATE_EVENT.wait(),
                                       timeout=self.cfg.tick_max_wait_seconds)
            except asyncio.TimeoutError:
                pass
            fast_monitor.UPDATE_EVENT.clear()
            try:
                self._tick()
                self.consec_err = 0
            except Exception as exc:  # noqa: BLE001 - цикл должен жить
                self.consec_err += 1
                self.log.warning("тик упал (%d/%d): %s", self.consec_err,
                                 self.cfg.max_consecutive_errors, exc)
                if self.consec_err >= self.cfg.max_consecutive_errors:
                    self.stop_reason = "слишком много ошибок подряд — стоп"
                    return
            if self.stop_reason:
                return
        if not self.stop_reason:
            self.stop_reason = "истекло время работы"

    # ======================================================================
    #  Точки расширения: их реализует торговый движок
    # ======================================================================
    def _tick(self) -> None:
        """Один такт решения. База не торгует и ничего не делает."""

    def _settle_open_position(self, reason: str) -> None:
        """Закрыть учёт открытой позиции на границе окна."""

    async def _sim_latency(self) -> None:
        """В dry-run честно подождать пинг, прежде чем «исполнить» ордер.

        Без этого симуляция покупает по цене, которой в момент прилёта
        ордера на биржу уже не было, и её результат систематически лучше
        боевого.
        """
        if self.cfg.dry_run and self.cfg.simulate_latency:
            await asyncio.sleep(self.latency.fill_delay_s)

    # ======================================================================
    #  Баланс, журнал, вывод
    # ======================================================================
    def _get_balance(self) -> float:
        now = time.time()
        if self._balance is None or now - self._balance_ts >= 5.0:
            try:
                self._balance = self.trader.get_balance()
            except Exception:  # noqa: BLE001
                self._balance = self._balance if self._balance is not None \
                    else self._btc_cfg.dry_run_balance
            self._balance_ts = now
        return self._balance

    def _invalidate_balance(self) -> None:
        self._balance = None

    def _log_trade_row(self, pos: dict, result: str, settle_price: float,
                       payout: float, pnl: float) -> None:
        try:
            bal = self.trader.get_balance()
        except Exception:  # noqa: BLE001
            bal = None
        self.tradelog.append({
            "settled_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
            "slug": (self.market or {}).get("slug", ""),
            "outcome": pos["outcome"],
            "entry_price": pos["entry_price"],
            "shares": pos["shares"],
            "cost": pos["cost"],
            "btc_at_entry": pos.get("coin_at_entry"),
            "secs_to_end_at_entry": pos.get("secs_at_entry"),
            "result": result,
            "settle_price": round(settle_price, 2),
            "payout": payout,
            "pnl": pnl,
            "cumulative_pnl": round(self.realized_pnl, 2),
            "balance_after": round(bal, 2) if bal is not None else "",
        })

    def _banner(self) -> None:
        c = self.cfg
        mode = "DRY-RUN (без реальных ордеров)" if c.dry_run else "*** LIVE ***"
        self.log.info("=" * 72)
        self.log.info("flowbot — %s Up/Down 5m — %s", c.asset.upper(), mode)
        self.log.info("=" * 72)

    def _summary(self) -> None:
        self.log.info("=" * 72)
        self.log.info("остановлен: %s", self.stop_reason or "—")
        settled = self.wins + self.losses
        self.log.info("сделок: %d | закрыто: %d (W/L %d/%d) | P&L: $%+.2f",
                      self.n_trades, settled, self.wins, self.losses,
                      self.realized_pnl)
        try:
            self.log.info("баланс: $%.2f", self.trader.get_balance())
        except Exception as exc:  # noqa: BLE001
            self.log.info("баланс: недоступен (%s)", exc)
        if self.tradelog.enabled and settled:
            self.log.info("лог сделок: %s", self.tradelog.path)
        self.log.info("=" * 72)


def _p(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.2f}"


def _m(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _short(resp) -> str:
    s = str(resp)
    return s if len(s) <= 80 else s[:77] + "…"
