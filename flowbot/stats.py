"""Журнал сделок, который переживает смену версии бота.

Зачем отдельно от `jump_trades.csv`. Тот CSV лежит РЯДОМ С КОДОМ и пишет
только исход: сторона, цена, P&L. Из-за этого история терялась дважды подряд
и по двум разным причинам:

  * новая сборка распаковывается в новую папку (`bot16`, `bot18`, `bot20`),
    и CSV прошлой сборки остаётся в старой — фактически обнуление истории при
    каждом обновлении;
  * даже в уцелевших файлах нет ПРИЗНАКОВ сигнала (Q, скорость, удержание,
    ускорение, запас), поэтому ответить «какой фильтр реально влияет на
    прибыль» по ним нельзя в принципе. А это и есть единственный вопрос,
    ради которого история собирается.

Здесь и то и другое закрыто:

  * путь по умолчанию — `~/.flowbot/trades.jsonl`, ВНЕ папки проекта. Ставь
    сколько угодно новых сборок, журнал один и тот же;
  * пишется JSONL: одна сделка — одна строка JSON. Добавление полей в новых
    версиях НЕ ломает старые строки и не требует переписывать файл, в
    отличие от CSV с фиксированной шапкой;
  * каждая строка несёт `schema` и `mode` (DRY/LIVE), поэтому симуляция и
    реальные деньги не смешиваются, даже если лежат в одном файле;
  * файл только ДОПИСЫВАЕТСЯ. Никакой код здесь ничего не удаляет и не
    перезаписывает.

Ошибка записи никогда не роняет бота: журнал важен, но торговля важнее.
Первый сбой попадает в лог, дальнейшие молчат.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

# Версия набора полей. Растёт, когда добавляются НОВЫЕ признаки; старые
# строки остаются валидными и читаются тем же кодом.
SCHEMA = 2

_DEFAULT = os.path.join("~", ".flowbot", "trades.jsonl")


def default_path() -> str:
    return os.path.expanduser(os.getenv("FLOW_STATS_PATH", _DEFAULT))


class TradeJournal:
    """Append-only JSONL. Потокобезопасен, ошибки не пробрасывает."""

    def __init__(self, path: Optional[str] = None, enabled: bool = True):
        self.path = os.path.expanduser(path or default_path())
        self.enabled = bool(enabled and self.path)
        self._lock = threading.Lock()
        self._warned = False
        self.written = 0
        if self.enabled:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            except OSError:
                pass                     # разберёмся при первой записи

    def append(self, row: Dict[str, Any]) -> bool:
        """Дописать строку. True — записано. Исключений не бросает."""
        if not self.enabled:
            return False
        row = dict(row)
        row.setdefault("schema", SCHEMA)
        row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            line = json.dumps(row, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                # Журнал должен пережить убийство процесса: бот снимают
                # Ctrl+C посреди раунда, и буфер ОС уносил бы последние
                # сделки — как раз те, ради которых смотрят лог.
                f.flush()
                os.fsync(f.fileno())
            self.written += 1
            return True
        except OSError as exc:
            if not self._warned:
                self._warned = True
                import logging
                logging.getLogger("jumpbot").error(
                    "журнал сделок не пишется (%s): %s — торговля продолжается, "
                    "но история не сохраняется", self.path, exc)
            return False


def read_all(path: Optional[str] = None):
    """Прочитать журнал. Битые строки пропускаются, а не роняют разбор."""
    p = os.path.expanduser(path or default_path())
    rows = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows
