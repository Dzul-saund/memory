"""Замок «один бот на рынок»: не дать случайно запустить вторую копию.

Зачем. Каждая копия ведёт СВОЮ лестницу и не знает о чужих. Три копии на
одном рынке — это тройной риск при тех же настройках: потолок «$25 за раунд»
незаметно превращается в $75, а в CSV попадают вперемешку три независимых
цепочки P&L, по которым уже не понять, что происходило. На реальных деньгах
это прямой путь потерять втрое больше, чем рассчитывал.

Механика — классический pid-файл: атомарно создаём файл флагом O_EXCL. Если
он уже есть, читаем PID и проверяем, жив ли процесс; мёртвый (например,
машина выключилась) — забираем замок себе. Работает и на Windows, и на Linux
без внешних зависимостей.
"""
from __future__ import annotations

import atexit
import os
import tempfile
from typing import Optional


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID (кроссплатформенно, без psutil)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:  # noqa: BLE001 - нет доступа => считаем живым
            return True
    try:
        os.kill(pid, 0)          # сигнал 0 ничего не делает, только проверяет
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # чужой процесс, но существует
    except OSError:
        return False


class InstanceLock:
    """Замок на (стратегия, монета, режим). Освобождается сам при выходе."""

    def __init__(self, name: str, directory: Optional[str] = None):
        base = directory or tempfile.gettempdir()
        self.path = os.path.join(base, f".{name}.lock")
        self.acquired = False
        self.holder_pid: Optional[int] = None

    def acquire(self) -> bool:
        for _ in range(2):        # вторая попытка — после снятия мёртвого замка
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self._read_pid()
                if pid is not None and _pid_alive(pid):
                    self.holder_pid = pid
                    return False
                # Замок остался от процесса, которого больше нет.
                try:
                    os.unlink(self.path)
                except OSError:
                    return False
                continue
            except OSError:
                return False      # нет прав на каталог — просто не мешаем
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self.acquired = True
            atexit.register(self.release)
            return True
        return False

    def _read_pid(self) -> Optional[int]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            if self._read_pid() == os.getpid():
                os.unlink(self.path)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
