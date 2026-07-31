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
import json
import logging
import math
import time
from typing import Dict, List, Optional

import fast_monitor

from btc_bot.prob import expected_shift
from btc_bot.util import floor2, whole_shares

from .config import FlowConfig
from .engine import FlowEngine, now_ms, _m, _p, _short
from .fills import UNKNOWN, interpret
from .positions import reconcile, shares_from_balance
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
        self._rec = None
        # Пока не истечёт — новых ордеров не отправляем. Без этого один
        # отклонённый ордер повторяется на каждом такте (десять раз в
        # секунду), и площадка справедливо начинает ограничивать нас.
        self._retry_after = 0.0
        # Когда взвели self.busy. Нужен сторож: у запроса к бирже нет
        # таймаута по умолчанию, и один повисший ордер оставлял бы busy
        # взведённым НАВСЕГДА — бот молча переставал бы торговать.
        self._busy_since = 0.0
        self._last_skip_log = 0.0
        self._last_reconcile = 0.0
        # Шэры, доставшиеся нам от ЗАКОНЧИВШИХСЯ раундов. Сверка спрашивала
        # биржу только про два токена текущего окна, поэтому остаток от
        # прошлого раунда был невидим по построению: на Polymarket висит
        # позиция, а бот показывает «поз —». Продать её нельзя (книги уже
        # нет), но знать о ней и сказать про неё вслух — обязан.
        self._leftovers: Dict[str, dict] = {}
        if cfg.record_path:
            self._rec = open(cfg.record_path, "a", encoding="utf-8")
            self.log.info("запись рынка: %s", cfg.record_path)

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

        # Якорь Polymarket: цена, по которой считает раунд САМА площадка.
        # В fast_monitor остальные биржи дебиасятся относительно него, поэтому
        # разрыв «наша цена − pm» — чистое опережение, а не базис площадок.
        pm = fast_monitor.prices.get("polymarket")
        pm_age = (now_ms() - fast_monitor.last_update.get("polymarket", 0)
                  if pm is not None else None)

        # Качество импульса: скорость/ускорение/удержание/возраст экстремума.
        # None до прогрева трекера — режим impulse честно ждёт данных.
        imp = self.price.imp.state()

        snap = JumpSnapshot(
            t=time.time(),
            seconds_left=max(0.0, self.market["end_ts"] - time.time()),
            coin_price=price, target=self._target(),
            jump_usd=jump_usd, jump_bps=jump_bps,
            sigma_1s=self.price.sigma_1s(),
            up_bid=ub, up_ask=ua, down_bid=db, down_ask=da,
            up_flow=self.book.flow(up, cfg.flow_window_s),
            down_flow=self.book.flow(dn, cfg.flow_window_s),
            pm_price=pm, pm_age_ms=pm_age,
            speed=imp.speed if imp else None,
            accel=imp.accel if imp else None,
            imp_age_s=imp.age_s if imp else None,
            imp_hold=imp.hold if imp else None,
            # Покупки сейчас не пройдут — значит и разворот не пройдёт, и
            # стоп не имеет права рассчитывать на него (см. _ladder_blocked).
            entries_paused=time.time() < self._retry_after,
        )
        # В режиме impulse скачок меряется коротким окном трекера, а не
        # 60-секундным swing: старое дно не имеет отношения к движению.
        if cfg.jump_entry_mode == "impulse" and imp is not None:
            snap.jump_usd = imp.jump_usd

        # Запоминаем последний bid каждой ноги — по нему считаем расчёт, если
        # книга к моменту сеттла уже закрылась.
        for leg in self.legs:
            leg["last_bid"] = ub if leg["outcome"] == "Up" else db

        self._record(snap)

        # Периодическая сверка с биржей. Дешёвая (два запроса) и редкая, но
        # это единственное, что ловит расхождение учёта с реальностью.
        every = getattr(cfg, "jump_reconcile_s", 0.0)
        if every > 0 and snap.t - self._last_reconcile > every and not self.busy:
            self._last_reconcile = snap.t
            asyncio.create_task(self._reconcile())

        action = self.strategy.on_tick(snap)
        self._log_status(snap, action)

        if action.kind not in (ENTER, LADDER, SELL):
            return
        now = time.time()

        if self.busy:
            # СТОРОЖ. Ордер не может исполняться дольше своего таймаута с
            # запасом. Если висит дольше — запрос повис, и без сброса бот
            # больше не торгует до перезапуска, ничего об этом не сообщая.
            limit = self.cfg.order_timeout_s * 2 + 5.0
            if now - self._busy_since > limit:
                self.log.error(
                    "СТОРОЖ: ордер висит %.0fс (> %.0fс) — снимаю блокировку. "
                    "Сделка могла уйти на биржу: проверь позиции на сайте.",
                    now - self._busy_since, limit)
                self.busy = False
            else:
                self._note_skip("предыдущий ордер ещё в полёте")
                return

        # Пауза после отказа биржи придерживает ВХОДЫ, но никогда не
        # мешает ВЫХОДУ. Вход можно отложить — рынок никуда не денется.
        # Продажу откладывать нельзя: стоп существует ровно для того, чтобы
        # выйти на падении, и пять секунд молчания здесь стоят тем дороже,
        # чем быстрее падает цена.
        if action.kind != SELL and now < self._retry_after:
            self._note_skip(f"вход отложен: пауза после ошибки биржи, ещё "
                            f"{self._retry_after - now:.0f}с")
            return

        self.busy = True
        self._busy_since = now
        asyncio.create_task(self._execute(action))

    async def _reconcile(self) -> None:
        """Спросить биржу, чем мы владеем на самом деле, и поверить ЕЙ.

        Свой учёт бота — это память о его же действиях, и она расходится с
        реальностью при таймаутах, частичных филлах и неверно разобранных
        ответах. Расхождение само не чинится: позиция висит на Polymarket и
        теряет в цене, а бот показывает «поз —» и ничего не делает, потому
        что управлять он может только тем, о чём знает.

        Биржа — источник истины. Мы не пытаемся угадать, откуда взялась
        разница: если шэры есть — заводим ногу и дальше ведём её обычными
        правилами (стоп, трейлинг); если их нет — выбрасываем фантом.
        """
        if self.market is None or self.cfg.dry_run:
            return
        current = (self.market["up"], self.market["down"])
        for outcome, token in (("Up", current[0]), ("Down", current[1])):
            theirs = await self._ask_position(token, outcome)
            if theirs is None:
                continue
            # Сравниваем по ТОКЕНУ, а не по стороне. Расчёт пропускается,
            # если на смене окна был ордер в полёте (`if not self.busy`), и
            # тогда нога прошлого раунда доживает до нового. Считать её по
            # стороне значило бы приписать её балансу чужого токена: биржа
            # ответила бы «меньше», и живая нога улетела бы как фантом.
            ours = sum(lg["shares"] for lg in self.legs
                       if lg.get("token") == token)
            diff = reconcile(ours, theirs)
            if not diff:
                continue

            if diff > 0:
                self._adopt(outcome, token, diff)
            else:
                self._drop_phantom(token, outcome, -diff)

        await self._check_leftovers(current)

    async def _ask_position(self, token: str,
                            outcome: str) -> Optional[float]:
        """Сколько шэров этого токена у нас по мнению биржи. None = не знаем."""
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(self.trader.position, token),
                timeout=self.cfg.order_timeout_s)
        except Exception as exc:  # noqa: BLE001 - сверка не должна ронять бота
            self.log.debug("сверка позиции %s: %s", outcome, exc)
            return None
        return shares_from_balance(resp)

    async def _check_leftovers(self, current: tuple) -> None:
        """Остатки от закончившихся раундов: проверить, живы ли они ещё.

        Отдельная ветка, а не обычная сверка, потому что с такой позицией
        НЕЛЬЗЯ обращаться как с ногой. Раунд закончен, книги больше нет,
        продать не во что: попытка выйти дала бы отказ на каждом такте. Выплату
        по ней забирают кнопкой Redeem/Claim на сайте, и сделать это за юзера
        мы не можем — это перевод на блокчейне, а не ордер в CLOB.

        Поэтому здесь ровно две задачи: не потерять остаток из вида и сказать
        про него человеку. Как только биржа ответит «ноль» — забываем.
        """
        for token in list(self._leftovers):
            if token in current:
                continue                # текущий раунд ведёт обычная сверка
            info = self._leftovers[token]
            theirs = await self._ask_position(token, info["outcome"])
            if theirs is None:
                continue                # «не знаю» — не трогаем
            if theirs <= 0:
                self._leftovers.pop(token, None)
                self.log.info(
                    "ОСТАТОК: %.2f шэр %s из раунда %s больше не на балансе — "
                    "выплата получена, снимаю с наблюдения.",
                    info["shares"], info["outcome"], info["slug"])
                continue

            info["shares"] = theirs
            now = time.time()
            if now - info["warned"] < self.cfg.jump_leftover_warn_s:
                continue
            info["warned"] = now
            self.log.error(
                "ОСТАТОК: на бирже висят %.2f шэр %s из ЗАКОНЧИВШЕГОСЯ раунда "
                "%s. Продать их нельзя — книги этого раунда больше нет, "
                "«Market Sell» на сайте отвечает «balance: 0». Выплату надо "
                "забрать кнопкой Redeem/Claim в позициях на Polymarket.",
                theirs, info["outcome"], info["slug"])

    def _watch_leftover(self, leg: dict) -> None:
        """Запомнить ногу, дожившую до конца раунда, как остаток."""
        token = leg.get("token")
        if not token or self.cfg.dry_run:
            return
        old = self._leftovers.get(token)
        self._leftovers[token] = {
            "outcome": leg["outcome"],
            "slug": (self.market or {}).get("slug") or "—",
            "shares": leg["shares"] + (old["shares"] if old else 0.0),
            "warned": 0.0,
        }
        # Список не должен расти без предела: держим только последние.
        while len(self._leftovers) > 12:
            self._leftovers.pop(next(iter(self._leftovers)))

    def _adopt(self, outcome: str, token: str, shares: float) -> None:
        """На бирже есть шэры, о которых бот не знал — берём их под управление."""
        bid, ask = self.book.best(token)
        px = ask if ask is not None else (bid or 0.5)
        self.log.error(
            "СВЕРКА: на бирже %.2f шэр %s, о которых бот не знал — беру под "
            "управление по текущей цене %.2f. Скорее всего ордер прошёл, а "
            "ответ на него потерялся.", shares, outcome, px)
        leg = self.strategy.record_entry(outcome, px, shares,
                                         round(px * shares, 2), "A",
                                         time.time(),
                                         entry_bid=bid if bid is not None else px)
        self.legs.append({
            "idx": leg.idx, "outcome": outcome, "token": token,
            "entry_price": px, "shares": shares, "cost": round(px * shares, 2),
            "track": "A", "last_bid": bid,
            "coin_at_entry": self.price.price(),
            "secs_at_entry": int(self.market["end_ts"] - time.time()),
        })

    def _drop_phantom(self, token: str, outcome: str, shares: float) -> None:
        """Бот считал ноги своими, а на бирже их нет — выбрасываем."""
        self.log.error(
            "СВЕРКА: бот считал своими %.2f шэр %s, но на бирже их нет — "
            "выбрасываю фантом. Скорее всего ордер не исполнился, а ответ "
            "разобрался как успех.", shares, outcome)
        left = shares
        for lg in list(self.legs):
            if lg.get("token") != token or left <= 0:
                continue
            take = min(lg["shares"], left)
            lg["shares"] = floor2(lg["shares"] - take)
            left = round(left - take, 2)
            self.strategy.record_partial_sell(lg["idx"], take, 0.0, time.time())
            if lg["shares"] <= 0:
                self.legs = [x for x in self.legs if x["idx"] != lg["idx"]]

    def _note_skip(self, why: str) -> None:
        """Сигнал был, но действовать нельзя. Раньше это молчало.

        Молчание тут опаснее всего: снаружи бот выглядит работающим — строки
        состояния идут, решение «покупать» печатается, — а сделок нет и
        причина неизвестна. Пишем, но не чаще раза в 5 секунд, иначе на
        десяти тактах в секунду лог утонет.
        """
        now = time.time()
        if now - self._last_skip_log < 5.0:
            return
        self._last_skip_log = now
        self.log.warning("сигнал есть, но сделки нет: %s", why)

    # ======================================================================
    #  Исполнение
    # ======================================================================
    async def _execute(self, action) -> None:
        try:
            self.log.warning(">>> %s: %s", action.kind.upper(), action.reason)
            if action.kind == LADDER and action.sell_idx is not None:
                # Разворот: сперва закрываем провалившуюся ногу — она вернёт
                # капитал, и добор считается уже от РЕАЛИЗОВАННОГО убытка,
                # а не от полной стоимости ноги.
                #
                # Добор идёт ДАЖЕ ЕСЛИ продать не удалось — так было
                # изначально и так просил юзер. Плата за это: обе ноги
                # остаются открытыми, а долг лестницы посчитан так, будто
                # первую продали. Предупреждаем в лог, но не отменяем.
                sold = await self._sell_leg(action.sell_idx, None, "разворот",
                                            result="SOLD_FLIP")
                if not sold:
                    self.log.warning(
                        "РАЗВОРОТ: продать старую ногу не удалось, но вторую "
                        "сторону беру — открыты ОБЕ, долг считается от "
                        "непроданной")
                await self._buy_leg(action)
            elif action.kind in (ENTER, LADDER):
                await self._buy_leg(action)
            elif action.kind == SELL:
                await self._sell_leg(action.sell_idx, action.limit_price,
                                     "фиксация", result="SOLD_TP")
        except Exception as exc:  # noqa: BLE001 - движок должен жить
            self.log.error("исполнение упало: %s", exc)
        finally:
            self.busy = False

    def _back_off(self, side: str, exc) -> None:
        """Ордер отвергнут — замолчать на паузу, а не долбить биржу.

        Сигнал никуда не девается: он держится, пока держится рынок, и на
        следующем такте бот попробует снова. Без паузы это «снова» наступает
        через 60мс, и один отказ превращается в сотни запросов в минуту.
        """
        pause = getattr(self.cfg, "jump_error_cooldown_s", 5.0)
        self._retry_after = time.time() + pause
        self.log.error("ордер %s упал: %s", side, exc)
        self.log.warning("пауза %.0fс перед следующей попыткой", pause)

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
        # РАЗМЕР — ТОЛЬКО ЦЕЛЫМИ ШЭРАМИ.
        #
        # Polymarket отвергает покупку, если сумма в USDC (цена * размер)
        # имеет больше двух знаков после запятой. Цена всегда в целых центах,
        # поэтому два знака гарантированы лишь при целом числе шэров:
        # 0.57 * 1.76 = $1.0032 -> 400 "invalid amounts", 0.57 * 2 = $1.14 ок.
        # Дробные размеры проходили dry-run, потому что там никто не считает
        # суммы, и падали на первом же живом ордере.
        floor_usdc = getattr(self.cfg, "jump_min_order_usdc", 0.0)
        if action.shares is not None:
            want = ladder_shares(self.strategy.debt,
                                 self.cfg.jump_ladder_profit_usdc, fill)
        else:
            want = (action.size_usdc or 0.0) / fill
        shares = whole_shares(want * fill, fill, floor_usdc)
        if shares <= 0:
            self.log.warning("ПОКУПКА %s: расчёт дал %.0f шэров — пропуск",
                             outcome, shares)
            return
        cost = round(fill * shares, 2)
        if abs(cost - want * fill) > 0.01:
            self.log.info("размер округлён до целых шэров: %.0f @ %.2f = "
                          "$%.2f (хотели $%.2f)", shares, fill, cost,
                          want * fill)
        # Потолок раунда проверяем ещё раз ЗДЕСЬ, а не только в стратегии:
        # за время пинга цена филла могла вырасти, а вместе с ней и стоимость.
        if self.strategy.net_out + cost > self.cfg.jump_max_round_usdc + 1e-9:
            self.log.warning(
                "ПОКУПКА %s отменена: $%.2f + $%.2f вышли бы за потолок "
                "раунда $%.2f", outcome, self.strategy.net_out, cost,
                self.cfg.jump_max_round_usdc)
            return
        try:
            # Таймаут обязателен: у клиента биржи его нет, а зависший запрос
            # держал бы busy взведённым и бот бы молча замер.
            resp = await asyncio.wait_for(
                asyncio.to_thread(self.trader.buy, token, fill, shares),
                timeout=self.cfg.order_timeout_s)
        except asyncio.TimeoutError:
            self._back_off("BUY", f"нет ответа за {self.cfg.order_timeout_s:.0f}с")
            self.log.error(
                "ВНИМАНИЕ: ордер мог уйти на биржу. Ногу НЕ записываю (иначе "
                "бот считал бы своей позицию, которой может не быть) — "
                "проверь позиции на сайте вручную.")
            return
        except Exception as exc:  # noqa: BLE001
            self._back_off("BUY", exc)
            return

        # Ордер уходит как FAK: несведённый остаток отменяется. Значит филла
        # могло не быть вовсе. Записать ногу, которой нет, — худшее, что
        # может случиться: дальше бот «продаёт» её и ведёт лестницу от
        # выдуманного долга.
        got = interpret(resp, shares)
        if not got.ok:
            self.log.error("ПОКУПКА %s ОТКЛОНЕНА биржей: %s | ответ: %s",
                           outcome, got.note, resp)
            return
        if got.kind == UNKNOWN:
            self.log.warning(
                "ПОКУПКА %s: ответ биржи не разобран (%s). Считаю "
                "исполненным — СВЕРЬ позицию на сайте. Ответ: %s",
                outcome, got.note, resp)
        elif got.shares is not None and got.shares < shares - 1e-9:
            # Частичный филл: владеем меньшим, чем просили. Учитываем
            # фактическое, иначе продажа уйдёт на несуществующие шэры.
            self.log.warning("ПОКУПКА %s исполнена ЧАСТИЧНО: %s",
                             outcome, got.note)
            shares = floor2(got.shares)
            cost = round(fill * shares, 2)
            if shares <= 0:
                return

        self._invalidate_balance()
        self.n_trades += 1
        # Движение отработано — переставляем экстремум на текущую цену, иначе
        # то же самое дно секунду спустя открыло бы ещё одну такую же сделку.
        self.price.reset_swing()
        # bid на момент входа: от него считается движение против нас, иначе
        # спред выглядел бы как мгновенная просадка.
        leg = self.strategy.record_entry(outcome, fill, shares, cost,
                                         action.track, time.time(),
                                         entry_bid=bid if bid is not None else fill)
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

    @staticmethod
    def _is_short_balance(err) -> bool:
        """Биржа отказала именно из-за нехватки шэров, а не по другой причине.

        Это ДЕТЕРМИНИРОВАННЫЙ отказ: повторять тот же ордер бессмысленно,
        ответ будет тот же. Отличать его от временных отказов обязательно —
        выход не придерживается паузой (стоп должен срабатывать сразу), и
        без этой развилки один такой отказ превращается в десять запросов
        в секунду до конца раунда.
        """
        text = str(err).lower()
        return ("not enough balance" in text
                or "balance is not enough" in text
                or "insufficient balance" in text)

    async def _fix_short_balance(self, leg: dict, err) -> bool:
        """Отказ «не хватает баланса» — пересинхронизировать ногу с биржей.

        Возвращает True, если отказ распознан и обработан (звонящий не должен
        уходить в обычную паузу и повторять тот же ордер).

        Биржа — источник истины: спрашиваем фактический остаток и приводим
        ногу к нему, усекая вниз. Если продавать уже нечего — нога снимается
        и уходит в остатки, иначе бот будет пытаться продать пыль вечно.
        """
        if not self._is_short_balance(err):
            return False

        self.log.error(
            "ПРОДАЖА %s отвергнута: на бирже меньше шэров, чем считает бот "
            "(%.4f). Спрашиваю фактический остаток. Ответ: %s",
            leg["outcome"], leg["shares"], _short(err))

        theirs = await self._ask_position(leg["token"], leg["outcome"])
        if theirs is None:
            self.log.error(
                "остаток выяснить не удалось — нога оставлена, "
                "следующая попытка после паузы")
            self._back_off("SELL", "баланс не выяснен")
            return True

        # ПРОДАЁМ ТОЛЬКО ЦЕЛЫЕ ШЭРЫ — по той же причине, по которой их
        # покупаем целыми (btc_bot.util.whole_shares). Цена всегда в целых
        # центах, поэтому «цена × размер» укладывается в два знака USDC
        # только при целом размере: 0.57 × 0.06 = $0.0342 — три знака.
        # Дробный хвост меньше шэра продать нечем; он уходит в остатки и
        # забирается Redeem'ом, а бот перестаёт долбить биржу.
        sellable = float(math.floor(theirs + 1e-9))
        if sellable <= 0:
            self.log.error(
                "на бирже %.6f шэр %s — меньше одного целого шэра, продать "
                "нечем (цена в целых центах требует целый размер). Снимаю "
                "ногу с торговли, остаток заберётся Redeem'ом.",
                theirs, leg["outcome"])
            self._park_dust(leg, theirs)
            return True
        if theirs - sellable > 1e-9:
            self.log.warning(
                "дробный хвост %.6f шэр %s продать нечем — останется под "
                "Redeem", theirs - sellable, leg["outcome"])

        self.log.warning(
            "правлю ногу %s: было %.4f, на бирже %.6f — продаю %.2f",
            leg["outcome"], leg["shares"], theirs, sellable)
        leg["shares"] = sellable
        leg["cost"] = round(leg["entry_price"] * sellable, 2)
        for lg in self.strategy.legs:
            if lg.idx == leg["idx"]:
                lg.shares = sellable
                lg.cost = leg["cost"]
        return True

    def _note_sell_failure(self, leg: dict, why) -> None:
        """Считать подряд идущие отказы и снять ногу, если их слишком много.

        Предохранитель, не зависящий от ПРИЧИНЫ. Выход сознательно не
        придерживается паузой (стоп обязан срабатывать сразу на падении),
        поэтому любой устойчивый отказ — не только нехватка баланса —
        превращается в десяток запросов в секунду до конца раунда. После
        `jump_max_sell_fails` подряд нога снимается с торговли и уходит в
        остатки: продать её всё равно не выходит, а долбить биржу вредно.
        """
        leg["sell_fails"] = leg.get("sell_fails", 0) + 1
        limit = getattr(self.cfg, "jump_max_sell_fails", 5)
        if limit <= 0 or leg["sell_fails"] < limit:
            self.log.error("нога оставлена (отказ %d из %d): %s",
                           leg["sell_fails"], limit, _short(why))
            return
        self.log.error(
            "ПРОДАЖА %s не проходит %d раз подряд (%s) — снимаю ногу с "
            "торговли, чтобы не долбить биржу. %.4f шэр остаются на кошельке, "
            "забери их вручную (Redeem/Sell на сайте).",
            leg["outcome"], leg["sell_fails"], _short(why), leg["shares"])
        self._park_dust(leg, leg["shares"])

    def _park_dust(self, leg: dict, shares: float) -> None:
        """Снять непродаваемый остаток с торговли, не потеряв его из вида."""
        if shares > 0:
            self._watch_leftover({**leg, "shares": shares})
        self.legs = [x for x in self.legs if x["idx"] != leg["idx"]]
        self.strategy.legs = [lg for lg in self.strategy.legs
                              if lg.idx != leg["idx"]]

    async def _sell_leg(self, idx: Optional[int], sell_limit: Optional[float],
                        tag: str, result: str = "SOLD") -> bool:
        """Продать ногу. Возвращает True, только если продажа состоялась.

        tag — человеку в лог, result — в CSV. В колонке result должен лежать
        машиночитаемый ASCII-код рядом с WON/LOST, а не русское слово: по
        нему потом фильтруют и считают.
        """
        leg = next((lg for lg in self.legs if lg["idx"] == idx), None)
        if leg is None:
            return False
        await self._sim_latency()
        bid, _ask = self.book.best(leg["token"])
        px = bid if bid is not None else leg.get("last_bid")

        # Без цены продавать нельзя. Раньше здесь стоял запасной ноль, и
        # ордер на продажу мог уйти по цене 0.00 — то есть отдать шэры
        # даром. В dry-run это не всплывало: там филл симулированный.
        if px is None or px <= 0:
            self.log.error("ПРОДАЖА %s отменена: нет бида в книге "
                           "(нога оставлена)", leg["outcome"])
            return False

        # Пол цены. Между решением и отправкой проходит пинг, и книга могла
        # просесть. SELL уходит как FAK с полом: ниже него не исполнится.
        # Позволяем отдать не больше jump_max_sell_slip от цены решения,
        # иначе на обвале мы бы продавали по любой цене, какую покажет книга.
        floor_px = px
        slip = getattr(self.cfg, "jump_max_sell_slip", 0.0)
        if sell_limit is not None and slip > 0:
            floor_px = max(px, sell_limit - slip)
            if floor_px > px + 1e-9:
                self.log.warning(
                    "ПРОДАЖА %s: бид %.2f ниже пола %.2f (решение %.2f, "
                    "допуск %.2f) — жду, нога оставлена",
                    leg["outcome"], px, floor_px, sell_limit, slip)
                return False

        fill = round(floor_px, 2)
        shares = leg["shares"]
        proceeds = round(fill * shares, 2)
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(self.trader.sell, leg["token"], fill, shares),
                timeout=self.cfg.order_timeout_s)
        except asyncio.TimeoutError:
            self._back_off("SELL", f"нет ответа за {self.cfg.order_timeout_s:.0f}с")
            self.log.error("нога оставлена; продажа могла уйти на биржу — "
                           "проверь позиции на сайте")
            return False
        except Exception as exc:  # noqa: BLE001
            if await self._fix_short_balance(leg, exc):
                return False
            self._back_off("SELL", exc)
            self._note_sell_failure(leg, exc)
            return False

        got = interpret(resp, shares)
        if not got.ok:
            if await self._fix_short_balance(leg, resp):
                return False
            self.log.error("ПРОДАЖА %s ОТКЛОНЕНА биржей: %s | ответ: %s",
                           leg["outcome"], got.note, resp)
            self._note_sell_failure(leg, got.note)
            return False
        leg["sell_fails"] = 0
        if got.kind == UNKNOWN:
            self.log.warning(
                "ПРОДАЖА %s: ответ биржи не разобран (%s). Считаю "
                "исполненной — СВЕРЬ позицию на сайте. Ответ: %s",
                leg["outcome"], got.note, resp)
        elif got.shares is not None and got.shares < shares - 1e-9:
            # Продалась часть — остаток шэров остаётся у нас, и нога живёт
            # дальше уменьшенной. Закрывать её здесь нельзя: мы бы «забыли»
            # про то, чем всё ещё владеем.
            sold = floor2(got.shares)
            proceeds = round(fill * sold, 2)
            self.trader.settle(proceeds)
            self._invalidate_balance()
            # Усечение вниз, а не round: остаток ноги — это «сколько мы ещё
            # можем продать», и он не имеет права вырасти от арифметики.
            # round(0.0678 - 0.0, 2) давал 0.07 при фактических 0.0678, и
            # следующая продажа уходила на несуществующие шэры.
            leg["shares"] = floor2(leg["shares"] - sold)
            leg["cost"] = round(leg["entry_price"] * leg["shares"], 2)
            self.strategy.record_partial_sell(idx, sold, proceeds, time.time())
            self.log.warning(
                "ПРОДАЖА %s ЧАСТИЧНАЯ: %s — продано %.2f @ $%.2f (=$%.2f), "
                "осталось %.2f шэр", leg["outcome"], got.note, sold, fill,
                proceeds, leg["shares"])
            if leg["shares"] <= 0:
                self.legs = [lg for lg in self.legs if lg["idx"] != idx]
            return False

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
        return True

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
        self._record_settle(winner, resolved)
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
            # В бою «расчёт» — это НАША запись, а не факт. Шэры остаются на
            # балансе до Redeem, поэтому ногу, дожившую до конца окна, берём
            # под наблюдение: иначе она пропадает из вида навсегда.
            self._watch_leftover(leg)
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

    # ======================================================================
    #  Запись всего, что видел бот (сырьё для проигрывания)
    # ======================================================================
    def _record(self, snap: JumpSnapshot) -> None:
        """Одна строка JSONL на такт — ровно тот снимок, что видит стратегия.

        Это и есть «память» системы: по такой записи можно потом прогнать
        ЛЮБЫЕ пороги на реальной истории вместо того, чтобы подбирать их на
        глаз по десятку сделок. Пишем только то, что реально было известно в
        этот момент — никакого заглядывания вперёд.
        """
        if not self._rec:
            return
        try:
            up = (self.market or {}).get("up")
            dn = (self.market or {}).get("down")
            ubs, uas, ubl, ual = self.book.depth(up)
            dbs, das, _, _ = self.book.depth(dn)
            w = self.cfg.flow_window_s
            u_buy, u_sell, u_n = self.book.trade_counts(up, w)
            d_buy, d_sell, d_n = self.book.trade_counts(dn, w)
            # Якорь Polymarket берём ИЗ СНИМКА, а не читаем заново: иначе в
            # записи оказалась бы цена на миллисекунды свежее той, по которой
            # стратегия принимала решение, и replay расходился бы с боевым.
            pm, pm_age = snap.pm_price, snap.pm_age_ms
            self._rec.write(json.dumps({
                "t": round(snap.t, 3), "slug": (self.market or {}).get("slug"),
                "left": round(snap.seconds_left, 2),
                "price": snap.coin_price, "target": snap.target,
                "jump": round(snap.jump_usd, 4), "sigma": snap.sigma_1s,
                "ub": snap.up_bid, "ua": snap.up_ask,
                "db": snap.down_bid, "da": snap.down_ask,
                "uf": round(snap.up_flow, 3), "df": round(snap.down_flow, 3),
                # --- сырьё для признаков (по цене задним числом не считается)
                "pm": pm, "pm_age": round(pm_age) if pm_age is not None else None,
                "ubs": ubs, "uas": uas, "dbs": dbs, "das": das,
                "lvl": [ubl, ual],
                "tb": round(u_buy, 2), "tsl": round(u_sell, 2), "tn": u_n,
                "dtb": round(d_buy, 2), "dts": round(d_sell, 2), "dtn": d_n,
            }, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 - запись не должна ронять торговлю
            pass

    def _record_settle(self, winner: Optional[str], resolved: bool) -> None:
        """Итог раунда — без него проигрывание не знает, кто выиграл."""
        if not self._rec:
            return
        try:
            self._rec.write(json.dumps({
                "type": "settle", "slug": (self.market or {}).get("slug"),
                "winner": winner, "resolved": resolved,
                "t": round(time.time(), 3),
            }, separators=(",", ":")) + "\n")
            self._rec.flush()
        except Exception:  # noqa: BLE001
            pass

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
        # Остаток от прошлого раунда — не нога (продать нечем), но и молчать
        # про него нельзя: именно так «поз —» соседствовало с висящей на
        # Polymarket позицией.
        left = sum(i["shares"] for i in self._leftovers.values())
        if left > 0:
            pos += f" +ост {left:.1f} (Redeem)"
        tgt = (f" цель {snap.target:,.0f}" if snap.target is not None else
               " цель —")
        how = ("экстр" if self.cfg.jump_trigger_mode == "swing"
               else f"{self.cfg.jump_window_s:.0f}с")
        # «чувств» — на сколько центов сдвинет процент скачок в jump_small_usd.
        # Видно сразу, есть ли смысл вообще ждать сигнала в этой точке раунда.
        sens = expected_shift(snap.coin_price, snap.target, snap.sigma_1s,
                              max(snap.seconds_left, 0.5),
                              self.cfg.jump_small_usd)
        s_txt = f" чувств {abs(sens)*100:4.1f}¢" if sens is not None else ""
        # Отставание от якоря — то, ради чего существует режим lag. Держим его
        # в строке всегда: даже в других режимах видно, есть ли опережение.
        lag_txt = (f" лаг {snap.coin_price - snap.pm_price:+.1f}$"
                   if snap.pm_price is not None and snap.coin_price is not None
                   else "")
        self.log.info(
            "[%s] t-%3ds | %s %s%s | скачок %+.2f$/%s%s%s | Up %s/%s "
            "Down %s/%s | поз %s | вложено $%.2f | $%.2f | %s",
            self.cfg.jump_entry_mode, int(snap.seconds_left),
            self.cfg.asset.upper(), _m(snap.coin_price), tgt, snap.jump_usd,
            how, s_txt, lag_txt,
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
        if c.jump_entry_mode == "edge":
            self.log.info(
                "ВХОД ПО ЗАПАСУ: считаем Phi(z)−ask по обеим сторонам, берём "
                "лучшую при запасе >=%.1f¢. Скачок цены на вход НЕ влияет.",
                c.jump_min_edge_cents)
            self.log.info(
                "потолок цены ноги %.2f — у краёв модель занижает хвост, "
                "и «запас» там ненастоящий", c.jump_max_leg_price)
        elif c.jump_entry_mode == "impulse":
            self.log.info(
                "ВХОД ПО КАЧЕСТВУ ИМПУЛЬСА: скачок >=%.1fσ И скорость "
                ">=%.1fσ $/с И удержание >=%.0f%% И без затухания "
                "(ускорение >=%.2f); запас >= max(%.1f¢, %.1f×спред); "
                "ставка от качества: $%.0f/$%.0f/$%.0f/$%.0f",
                c.jump_imp_jump_sigmas, c.jump_imp_speed_sigmas,
                c.jump_imp_min_hold * 100, c.jump_imp_min_accel,
                c.jump_min_edge_cents, c.jump_edge_spread_mult,
                c.jump_stake_usdc, c.jump_stake_good, c.jump_stake_strong,
                c.jump_stake_best)
            self.log.info(
                "окно экстремума %.0fс; потолок цены ноги по качеству: "
                "%.2f/%.2f/%.2f", c.jump_imp_lookback_s,
                c.jump_max_leg_price_weak, c.jump_max_leg_price,
                c.jump_max_leg_price_strong)
        elif c.jump_entry_mode == "lag":
            self.log.info(
                "ВХОД ПО ОТСТАВАНИЮ ЯКОРЯ: Phi(z по нашей цене) − Phi(z по "
                "цене Polymarket) >= %.1f¢, якорь не старше %.0fмс, и при "
                "этом запас >= %.1f¢", c.jump_lag_min_cents,
                c.jump_lag_max_age_ms, c.jump_min_edge_cents)
        else:
            if c.jump_trigger_mode == "swing":
                how = (f"движение от локального дна/пика (окно поиска "
                       f"экстремума {c.jump_swing_lookback_s:.0f}с, "
                       f"срабатывает сразу)")
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
        if self._rec:
            try:
                self._rec.close()
            except Exception:  # noqa: BLE001
                pass
            self._rec = None
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
