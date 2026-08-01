#!/usr/bin/env python3
"""Настоящая задержка ордера — без единой настоящей покупки.

ЗАЧЕМ. В модели задержки (`flowbot/latency.py`) есть два слагаемых:

    задержка_исполнения = RTT/2 + FLOW_ORDER_PROCESS_MS

Первое измеряется по-настоящему даже в dry-run: TCP-рукопожатие до
`clob.polymarket.com` — это реальный пакет. Второе — 45 мс «подпись + матчинг»
— НИКТО НИКОГДА НЕ МЕРИЛ. Это догадка, и при RTT 90 мс она составляет ровно
половину модели.

Dry-run её проверить не может: симулированный трейдер отвечает мгновенным
полным филлом, ордер до биржи не доходит.

КАК ЭТО МЕРЯЕТСЯ ЗДЕСЬ, ПОЧТИ НЕ ТРАТЯ ДЕНЕГ. Отправляется настоящий
подписанный FAK-ордер, но с ЗАВЕДОМО НЕИСПОЛНИМОЙ ценой: покупка по 0.01
стороны, которая стоит 0.74-0.98. Никто не продаёт в десятки раз ниже рынка,
поэтому FAK не находит встречной заявки и мгновенно отменяется.

Размер при этом НЕ минимальный: у площадки минимум ордера $1, и заявка на
один цент отвергается ещё до матчинга («invalid amount for a marketable BUY
order ($0.01), min size: 1»). Поэтому 0.01 x 150 = $1.50.

При этом путь пройден ЦЕЛИКОМ и по-настоящему: подпись ECDSA, сеть до США,
матчинг-движок биржи, ответ обратно, разбор ответа. Ровно то, что происходит
с боевым ордером — минус сам факт покупки.

Худший исход, если книга окажется невероятной и ордер всё-таки исполнится:
150 шэров по 0.01 = $1.50. Не $50.

ЧТО ЕЩЁ ЭТО ПОКАЗЫВАЕТ. Подпись и отправка меряются ОТДЕЛЬНО, потому что
первый ордер на новый токен дороже остальных: `py_clob_client_v2` при первой
подписи ходит в сеть за `neg_risk` и `tick_size` (+100-300 мс). В боевом коде
это лечится предподписанием (`LiveTrader.presign_window`), но flowbot его не
вызывает — значит первый ордер КАЖДОГО раунда платит эту задержку. Здесь она
будет видна как разрыв между первой пробой и остальными.

ОТКАЗ БИРЖИ — ТОЖЕ ВАЛИДНЫЙ ЗАМЕР. Ответ «400» проходит ровно тот же путь,
что и принятый ордер. Такие пробы засчитываются, а не выбрасываются.

    python pingorder.py --env-file cert.env
    python pingorder.py --env-file cert.env -n 20
    python pingorder.py --env-file cert.env --price 0.02 --size 100

НУЖНЫ КРЕДЫ: PRIVATE_KEY и FUNDER в env-файле. Ордер настоящий и подписанный,
просто неисполнимый.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import statistics
import sys
import time
from typing import List

# Заведомо неисполнимая цена покупки: продавца в десятки раз ниже рынка не
# существует. Но размер выбран НЕ минимальный: у площадки минимум ордера $1
# ("invalid amount for a marketable BUY order ($0.01), min size: 1"), поэтому
# 0.01 x 150 = $1.50 — выше минимума и всё ещё неисполнимо.
DEAD_PRICE = 0.01
DEAD_SIZE = 150.0        # 0.01 x 150 = $1.50; максимальный риск — эти $1.50


def _tcp_rtt(host: str = "clob.polymarket.com", port: int = 443,
             n: int = 7) -> List[float]:
    """TCP-рукопожатие, мс. То же самое, что меряет бот при старте."""
    out: List[float] = []
    for _ in range(n):
        try:
            t0 = time.perf_counter()
            socket.create_connection((host, port), 5).close()
            out.append((time.perf_counter() - t0) * 1000.0)
        except OSError:
            continue
        time.sleep(0.05)
    return out


def _pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def _report(name: str, vals: List[float]) -> None:
    if not vals:
        print(f"  {name:<28} — не измерено")
        return
    line = (f"  {name:<28} медиана {statistics.median(vals):7.1f} мс"
            f"   мин {min(vals):6.1f}   90% {_pct(vals, 0.9):6.1f}"
            f"   макс {max(vals):7.1f}")
    print(line)


async def _find_market(asset: str) -> dict:
    from book_monitor import discover_market
    return await discover_market(asset)


def _best_side(market: dict) -> tuple:
    """(токен, имя стороны) — берём ту, что дороже: по ней 0.01 точно не филлится."""
    import asyncio as _a
    from book_monitor import Book, CLOB_WS, loads
    import json

    import websockets

    async def _peek():
        sub = json.dumps({"assets_ids": [market["up"], market["down"]],
                          "type": "market"})
        books = {str(market["up"]): Book(), str(market["down"]): Book()}
        async with websockets.connect(CLOB_WS, compression=None,
                                      open_timeout=8,
                                      user_agent_header="Mozilla/5.0") as ws:
            await ws.send(sub)
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    raw = await _a.wait_for(ws.recv(), timeout=2.0)
                except _a.TimeoutError:
                    continue
                if not isinstance(raw, str) or not raw.startswith(("{", "[")):
                    continue
                data = loads(raw)
                for ev in (data if isinstance(data, list) else [data]):
                    if not isinstance(ev, dict):
                        continue
                    if (ev.get("event_type") or ev.get("type")) != "book":
                        continue
                    b = books.get(str(ev.get("asset_id")))
                    if b is not None:
                        b.apply_snapshot(ev.get("bids") or ev.get("buys"),
                                         ev.get("asks") or ev.get("sells"))
                if all(b.have_snapshot for b in books.values()):
                    break
        return {t: b.best_ask() for t, b in books.items()}

    asks = _a.run(_peek())
    up, dn = str(market["up"]), str(market["down"])
    ua, da = asks.get(up), asks.get(dn)
    if ua is None and da is None:
        raise RuntimeError("книга не пришла — повтори через минуту")
    if (ua or 0) >= (da or 0):
        return up, "Up", ua
    return dn, "Down", da


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="настоящая задержка ордера без настоящей покупки")
    ap.add_argument("--env-file", help="файл настроек (например cert.env)")
    ap.add_argument("-n", "--samples", type=int, default=10,
                    help="сколько проб отправить (по умолчанию 10)")
    ap.add_argument("--asset", default=None, help="монета (по умолчанию из env)")
    ap.add_argument("--price", type=float, default=DEAD_PRICE,
                    help=f"цена неисполнимой заявки (по умолчанию {DEAD_PRICE})")
    ap.add_argument("--size", type=float, default=DEAD_SIZE,
                    help=f"размер в шэрах (по умолчанию {DEAD_SIZE:.0f}; "
                         f"цена x размер должно быть >= $1 — минимум площадки)")
    args = ap.parse_args(argv)

    price, size = round(args.price, 2), float(int(args.size))
    if price * size < 1.0:
        print(f"цена {price} x размер {size:.0f} = ${price * size:.2f} — "
              f"площадка отвергнет: минимум ордера $1.\n"
              f"Увеличь --size (например {int(1.5 / price) + 1}).",
              file=sys.stderr)
        return 2

    if args.env_file:
        if not os.path.exists(args.env_file):
            print(f"файл не найден: {args.env_file}", file=sys.stderr)
            return 2
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log = logging.getLogger("pingorder")

    from btc_bot.config import Config as BtcConfig
    from btc_bot.trader import build_trader
    from flowbot.config import FlowConfig

    fcfg = FlowConfig.from_env()
    asset = (args.asset or fcfg.asset).lower()

    btc_cfg = BtcConfig.from_env()
    btc_cfg.asset = asset
    # ЗОНДУ НУЖЕН ЖИВОЙ КЛИЕНТ. Симулированный трейдер отвечает мгновенно и
    # измерять в нём нечего — ровно поэтому 45 мс до сих пор не проверены.
    btc_cfg.dry_run = False

    if not (os.getenv("PRIVATE_KEY") and os.getenv("FUNDER")):
        print("нет PRIVATE_KEY / FUNDER — зонд отправляет НАСТОЯЩИЙ "
              "подписанный ордер,\nбез кредов это невозможно. Укажи "
              "--env-file с боевым файлом.", file=sys.stderr)
        return 2

    print("=" * 70)
    print("ЗОНД ЗАДЕРЖКИ ОРДЕРА")
    print("Отправляются НАСТОЯЩИЕ подписанные FAK-ордера по цене "
          f"{price:.2f} x {size:.0f} = ${price * size:.2f} на сторону,")
    print("которая стоит около 0.98. Встречной заявки по такой цене не "
          "существует,")
    print(f"поэтому ордер не исполняется. Максимальный риск, если "
          f"исполнится: ${price * size:.2f}.")
    print("=" * 70)

    print("\nищу текущий рынок…")
    market = asyncio.run(_find_market(asset))
    token, side, ask = _best_side(market)
    print(f"  раунд {market['slug']}")
    print(f"  сторона {side}: ask {ask:.2f} — покупаем по {price:.2f}, "
          f"филла быть не может")

    print("\nмеряю TCP-рукопожатие (это и есть «пинг» из лога бота)…")
    tcp = _tcp_rtt()
    _report("TCP-рукопожатие (RTT)", tcp)

    print(f"\nотправляю {args.samples} проб…")
    trader = build_trader(btc_cfg, log)
    sign_ms: List[float] = []
    send_ms: List[float] = []
    total_ms: List[float] = []
    filled = 0
    errors = 0

    try:
        from py_clob_client_v2.clob_types import OrderType
        fak = OrderType.FAK
    except Exception:  # noqa: BLE001
        fak = None

    rejected: List[str] = []
    for i in range(args.samples):
        resp = None
        # ОТКАЗ БИРЖИ — ТОЖЕ ВАЛИДНЫЙ ЗАМЕР. Ответ «400 invalid amount»
        # проходит ровно тот же путь, что и принятый ордер: подпись, сеть,
        # матчинг-движок, ответ обратно. Выбрасывать такие пробы значит
        # выбрасывать измерение из-за того, что оно не понравилось бирже.
        try:
            t0 = time.perf_counter()
            if fak is not None and hasattr(trader, "_build_signed"):
                signed = trader._build_signed(token, price, size, "BUY")
                t1 = time.perf_counter()
                try:
                    resp = trader.client.post_order(signed, fak)
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "status_code", None) is None:
                        raise          # сети не было — мерить нечего
                    rejected.append(str(getattr(exc, "error_message", exc))[:120])
                t2 = time.perf_counter()
                sign_ms.append((t1 - t0) * 1000.0)
                send_ms.append((t2 - t1) * 1000.0)
                total_ms.append((t2 - t0) * 1000.0)
            else:
                resp = trader.buy(token, price, size)
                total_ms.append((time.perf_counter() - t0) * 1000.0)
        except Exception as exc:  # noqa: BLE001 - зонд не должен падать
            errors += 1
            print(f"  проба {i + 1}: сеть/подпись упали — {exc}")
            time.sleep(0.5)
            continue

        if resp is not None:
            from flowbot.fills import interpret
            got = interpret(resp, size)
            if got.ok and got.shares:
                filled += 1
        tail = "" if resp is not None else "  (отказ биржи — замер валиден)"
        mark = "" if i else "   <- ПЕРВАЯ (подпись идёт в сеть за neg_risk/tick_size)"
        print(f"  проба {i + 1:2}: {total_ms[-1]:7.1f} мс{tail}{mark}")
        time.sleep(0.3)

    if rejected:
        uniq = sorted(set(rejected))
        print(f"\n  биржа отклонила {len(rejected)} из {args.samples} — это "
              f"НОРМАЛЬНО для зонда,\n  замеры остаются валидными. Причина:")
        for r in uniq[:3]:
            print(f"    {r}")

    print("\n" + "=" * 70)
    print("РЕЗУЛЬТАТ")
    _report("TCP-рукопожатие (RTT)", tcp)
    if sign_ms:
        _report("подпись ордера", sign_ms)
        _report("отправка + матчинг", send_ms)
    _report("ВСЁ вместе (решение->ответ)", total_ms)

    # Первая проба почти всегда дороже — считаем и без неё.
    if len(total_ms) > 1:
        rest = total_ms[1:]
        print()
        _report("без первой пробы", rest)
        delta = total_ms[0] - statistics.median(rest)
        if delta > 30:
            print(f"\n  ПЕРВЫЙ ОРДЕР ДОРОЖЕ ОСТАЛЬНЫХ НА {delta:.0f} мс.")
            print("  Это те самые neg_risk/tick_size. В боевом коде их "
                  "убирает\n  предподписание (LiveTrader.presign_window), но "
                  "flowbot его НЕ зовёт —\n  значит первый ордер каждого "
                  "5-минутного раунда платит эту задержку.")

    # Сверка с моделью.
    if total_ms and tcp:
        rtt = statistics.median(tcp)
        model = rtt / 2 + fcfg.order_process_ms
        real = statistics.median(total_ms[1:] or total_ms)
        print("\n" + "-" * 70)
        print(f"  модель бота:   RTT/2 ({rtt / 2:.0f}) + "
              f"FLOW_ORDER_PROCESS_MS ({fcfg.order_process_ms:.0f}) "
              f"= {model:.0f} мс")
        print(f"  измерено:      {real:.0f} мс")
        if real > model * 1.2:
            need = max(0.0, real - rtt / 2)
            print("\n  МОДЕЛЬ ЗАНИЖАЕТ. Симуляция сейчас оптимистичнее боя.")
            print(f"  Поставь в env-файл:  FLOW_ORDER_PROCESS_MS={need:.0f}")
        elif real < model * 0.8:
            need = max(0.0, real - rtt / 2)
            print("\n  Модель завышает — симуляция строже боя (это безопасно).")
            print(f"  Можно уточнить:  FLOW_ORDER_PROCESS_MS={need:.0f}")
        else:
            print("\n  Модель совпадает с реальностью — править нечего.")

    if filled:
        print(f"\n  ВНИМАНИЕ: {filled} проб(ы) ИСПОЛНИЛИСЬ по {price:.2f}. "
              f"Это ~${filled * price * size:.2f}.")
        print("  Забери их Redeem'ом на сайте, если раунд выиграет.")
    else:
        print("\n  Ни одна проба не исполнилась — потрачено $0.00.")
    if errors:
        print(f"  Ошибок: {errors}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
