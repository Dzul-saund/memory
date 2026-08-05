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


# --- коды WinAPI ------------------------------------------------------------
_ERROR_INVALID_PARAMETER = 87   # нет процесса с таким PID
_WAIT_OBJECT_0 = 0              # объект процесса сигнальный => процесс завершён


def _win_verdict(handle_ok: bool, last_error: int, wait_rc: int) -> bool:
    """Решение «жив ли» по результатам WinAPI. Вынесено отдельно, чтобы его
    можно было проверить тестами на любой ОС, не поднимая настоящих процессов.

    Здесь две тонкости, каждая из которых стоила бы бага:

    1. Хэндл НЕ получен. Единственный код, который честно значит «такого
       процесса нет» — 87. Всё остальное (прежде всего отказ в доступе)
       означает, что процесс существует, просто чужой. Ошибиться можно только
       в безопасную сторону: считаем живым и замок не трогаем.
    2. Хэндл получен — этого мало. Запись о ЗАВЕРШЁННОМ процессе живёт в ядре,
       пока на неё открыт хоть один хэндл (его держит, например, родительский
       Popen), и OpenProcess её спокойно находит. Настоящий признак смерти —
       объект процесса перешёл в сигнальное состояние.
    """
    if not handle_ok:
        return last_error != _ERROR_INVALID_PARAMETER
    return wait_rc != _WAIT_OBJECT_0


def _pid_alive_nt(pid: int) -> bool:
    """Windows-ветка: OpenProcess + проверка сигнального состояния."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Без argtypes/restype хэндл (64 бита) обрезался бы до int.
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL

    synchronize = 0x00100000
    query_limited = 0x1000            # PROCESS_QUERY_LIMITED_INFORMATION
    h = k32.OpenProcess(synchronize | query_limited, False, pid)
    if not h:
        return _win_verdict(False, ctypes.get_last_error(), 0)
    try:
        return _win_verdict(True, 0, k32.WaitForSingleObject(h, 0))
    finally:
        k32.CloseHandle(h)


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID (кроссплатформенно, без psutil)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_nt(pid)
        except Exception:  # noqa: BLE001 - не смогли проверить => считаем живым
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
