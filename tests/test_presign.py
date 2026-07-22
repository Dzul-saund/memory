"""Тесты предподписания BUY-ордеров (без сети и без реальных ключей).

Проверяем ПЛОМБИРОВКУ логики: заготовленный ордер используется при точном
совпадении (токен, цена, размер) и отправляется БЕЗ повторной подписи; при
несовпадении — обычный путь (подпись на месте). Реальную ЭЦП проверяет
пользователь на боевом микро-тесте — здесь клиент фейковый.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_bot.config import Config          # noqa: E402
from btc_bot.trader import LiveTrader       # noqa: E402


class FakeClient:
    """Считает вызовы create_order (подпись) и post_order (отправка)."""

    def __init__(self):
        self.create_calls = 0
        self.post_calls = 0

    def create_order(self, order):
        self.create_calls += 1
        # возвращаем «подписанный» объект, помнящий свои параметры
        return {"token": order.token_id, "price": order.price,
                "size": order.size, "side": order.side, "signed": True}

    def post_order(self, signed, order_type):
        self.post_calls += 1
        return {"ok": True, "sent": signed, "type": str(order_type)}


def _trader(presign=True):
    cfg = Config()
    cfg.presign_enabled = presign
    # обойти реальный keep-alive (нет httpx-пула у фейка) — не критично
    t = LiveTrader.__new__(LiveTrader)
    t.client = FakeClient()
    t.cfg = cfg
    import logging
    t.log = logging.getLogger("test")
    import threading
    t._presigned = {}
    t._presign_lock = threading.Lock()
    return t


def _wait_presign(t, expected, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        with t._presign_lock:
            if len(t._presigned) >= expected:
                return
        time.sleep(0.01)


def test_presign_fills_cache_for_whole_band():
    t = _trader()
    t.presign_window(["TOKUP", "TOKDN"], 0.98, 0.99, 50.0)
    _wait_presign(t, 4)
    # 2 токена × {0.98, 0.99} = 4 ордера
    assert len(t._presigned) == 4
    assert t.client.create_calls == 4
    assert ("TOKUP", 0.98, float(int(50 / 0.98))) in t._presigned


def test_buy_uses_presigned_without_resigning():
    t = _trader()
    t.presign_window(["TOKUP", "TOKDN"], 0.98, 0.99, 50.0)
    _wait_presign(t, 4)
    signs_before = t.client.create_calls          # 4 (фоновые)
    size = float(int(50 / 0.99))
    resp = t.buy("TOKUP", 0.99, size)
    assert resp["ok"] is True
    # НЕ было новой подписи — отправлен заготовленный ордер
    assert t.client.create_calls == signs_before
    assert t.client.post_calls == 1
    # ордер израсходован (salt не переиспользуется)
    assert ("TOKUP", 0.99, size) not in t._presigned


def test_buy_falls_back_when_not_presigned():
    t = _trader()
    # кэш пуст — цена вне заготовленного набора
    resp = t.buy("TOKUP", 0.87, 57.0)
    assert resp["ok"] is True
    assert t.client.create_calls == 1      # подписали на месте
    assert t.client.post_calls == 1


def test_resting_order_never_uses_presign():
    t = _trader()
    t.presign_window(["TOKUP", "TOKDN"], 0.98, 0.99, 50.0)
    _wait_presign(t, 4)
    size = float(int(50 / 0.99))
    before = t.client.create_calls
    t.buy("TOKUP", 0.99, size, resting=True)   # хедж — всегда живая подпись
    assert t.client.create_calls == before + 1
    # заготовленный ордер на месте, не тронут
    assert ("TOKUP", 0.99, size) in t._presigned


def test_presign_disabled_does_nothing():
    t = _trader(presign=False)
    t.presign_window(["TOKUP", "TOKDN"], 0.98, 0.99, 50.0)
    time.sleep(0.2)
    assert len(t._presigned) == 0
    # buy всё равно работает — обычной подписью
    resp = t.buy("TOKUP", 0.99, 50.0)
    assert resp["ok"] is True and t.client.create_calls == 1
