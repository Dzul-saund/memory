"""Модель пинга — реалистичная задержка VPS в Цюрихе до Polymarket.

Зачем это нужно. Между тем, как МЫ увидели резкое движение (по биржам, которые
опережают сайт), и тем, как наш ордер реально ляжет в матчинг-движок Polymarket,
проходит время: сеть Цюрих -> США + подпись ордера + обработка на бирже. За эти
десятки миллисекунд книга Up/Down уже частично переоценится. Поэтому:

  * на старте МЕРЯЕМ реальный RTT до `clob.polymarket.com` (TCP-connect) и
    показываем его;
  * если замер недоступен (или задан `--ping-ms`) — берём ориентир для
    Цюрих -> Polymarket (инфраструктура в US-East): RTT ~90мс;
  * в dry-run ЗАДЕРЖИВАЕМ симулированное исполнение на реалистичное время и
    берём цену уже на момент «прилёта» ордера — так симуляция честно
    показывает проскальзывание из-за пинга;
  * стратегия использует эту же задержку в «догоне» (предосторожность №1):
    если книга за время полёта ушла дальше, чем на 1-2 цента, — вход пропускаем.

Инфраструктура Polymarket (CLOB/Gamma) отвечает из США. Типичные наземные RTT
из Цюриха: до восточного побережья ~85-95мс, до западного ~150мс. По умолчанию
берём 90мс как консервативный ориентир для восточного побережья.
"""
from __future__ import annotations

import socket
import statistics
import time
from typing import List, Optional
from urllib.parse import urlparse


class LatencyModel:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger
        self._measured_rtt_ms: Optional[float] = None
        self._samples_ms: List[float] = []

    # -- замер -----------------------------------------------------------------
    def measure(self, host: str = "clob.polymarket.com", port: int = 443,
                samples: int = 5, timeout: float = 3.0) -> Optional[float]:
        """Медианный RTT до host:port по времени TCP-handshake (мс).

        Это честная оценка сетевого пути «мы -> Polymarket»: установка TCP —
        один round-trip. Замер синхронный и быстрый; в движке его удобно
        звать через asyncio.to_thread. Любая ошибка -> None (берём ориентир).
        """
        rtts: List[float] = []
        for _ in range(max(1, samples)):
            try:
                addr = (host, port)
                t0 = time.perf_counter()
                with socket.create_connection(addr, timeout=timeout):
                    pass
                rtts.append((time.perf_counter() - t0) * 1000.0)
            except Exception:  # noqa: BLE001 - сеть недоступна -> ориентир
                continue
            time.sleep(0.05)
        if not rtts:
            if self.log:
                self.log.info(
                    "пинг: замер до %s не удался (сеть/прокси) — беру "
                    "ориентир RTT %.0fмс (Цюрих->Polymarket)",
                    host, self.cfg.assumed_rtt_ms,
                )
            return None
        self._samples_ms = rtts
        self._measured_rtt_ms = statistics.median(rtts)
        if self.log:
            self.log.info(
                "пинг: RTT до %s = медиана %.0fмс (мин %.0f / макс %.0f, "
                "%d проб)", host, self._measured_rtt_ms,
                min(rtts), max(rtts), len(rtts),
            )
        return self._measured_rtt_ms

    def measure_url(self, url: str, **kw) -> Optional[float]:
        p = urlparse(url)
        host = p.hostname or url
        port = p.port or (443 if p.scheme in ("https", "wss") else 80)
        return self.measure(host=host, port=port, **kw)

    # -- эффективные величины --------------------------------------------------
    @property
    def rtt_ms(self) -> float:
        """RTT, который используем: ручной override > замер > ориентир."""
        if self.cfg.ping_ms is not None:
            return float(self.cfg.ping_ms)
        if self._measured_rtt_ms is not None:
            return self._measured_rtt_ms
        return float(self.cfg.assumed_rtt_ms)

    @property
    def one_way_ms(self) -> float:
        return self.rtt_ms / 2.0

    @property
    def order_latency_ms(self) -> float:
        """От решения до полного подтверждения (round-trip + обработка)."""
        return self.rtt_ms + self.cfg.order_process_ms

    @property
    def fill_delay_ms(self) -> float:
        """От решения до момента, когда ордер реально коснётся книги и
        исполнится: путь «туда» + обработка на бирже."""
        return self.one_way_ms + self.cfg.order_process_ms

    @property
    def fill_delay_s(self) -> float:
        return self.fill_delay_ms / 1000.0

    def describe(self) -> str:
        src = ("задан вручную" if self.cfg.ping_ms is not None
               else "замерен" if self._measured_rtt_ms is not None
               else "ориентир")
        return (f"пинг [{src}]: RTT {self.rtt_ms:.0f}мс, в одну сторону "
                f"{self.one_way_ms:.0f}мс, задержка исполнения "
                f"~{self.fill_delay_ms:.0f}мс (сеть->матчинг), полное "
                f"подтверждение ~{self.order_latency_ms:.0f}мс")
