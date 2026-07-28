"""Тесты замка «один бот на рынок».

Три копии бота на одном рынке ведут три независимые лестницы: риск
складывается, а CSV перемешивается. Замок должен это ловить.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowbot.singleton import (  # noqa: E402
    InstanceLock, _pid_alive, _win_verdict,
)


def test_first_acquires_second_refused(tmp_path):
    a = InstanceLock("t1", str(tmp_path))
    b = InstanceLock("t1", str(tmp_path))
    assert a.acquire() is True
    assert b.acquire() is False
    assert b.holder_pid == os.getpid()
    a.release()


def test_release_lets_next_in(tmp_path):
    a = InstanceLock("t2", str(tmp_path))
    assert a.acquire()
    a.release()
    b = InstanceLock("t2", str(tmp_path))
    assert b.acquire() is True
    b.release()


def test_different_names_do_not_collide(tmp_path):
    """btc и eth — разные рынки, обе копии имеют право работать."""
    a = InstanceLock("jumpbot-btc-dry", str(tmp_path))
    b = InstanceLock("jumpbot-eth-dry", str(tmp_path))
    assert a.acquire() and b.acquire()
    a.release()
    b.release()


def test_dry_and_live_are_separate_locks(tmp_path):
    a = InstanceLock("jumpbot-btc-dry", str(tmp_path))
    b = InstanceLock("jumpbot-btc-live", str(tmp_path))
    assert a.acquire() and b.acquire()
    a.release()
    b.release()


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    """Машина выключилась, файл остался — замок не должен блокировать навсегда."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = InstanceLock("t3", str(tmp_path))
    with open(lock.path, "w", encoding="utf-8") as f:
        f.write(str(dead.pid))
    assert lock.acquire() is True, "мёртвый замок должен переходить новому боту"
    lock.release()


def test_garbage_lock_file_is_taken_over(tmp_path):
    lock = InstanceLock("t4", str(tmp_path))
    with open(lock.path, "w", encoding="utf-8") as f:
        f.write("не-число")
    assert lock.acquire() is True
    lock.release()


def test_release_does_not_delete_someone_elses_lock(tmp_path):
    """Чужой PID в файле — удалять не наше дело."""
    lock = InstanceLock("t5", str(tmp_path))
    assert lock.acquire()
    with open(lock.path, "w", encoding="utf-8") as f:
        f.write("999999")
    lock.release()
    assert os.path.exists(lock.path)
    os.unlink(lock.path)


def test_pid_alive_true_for_self_false_for_dead():
    assert _pid_alive(os.getpid()) is True
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert _pid_alive(dead.pid) is False
    assert _pid_alive(-1) is False


# --- Windows-ветка ----------------------------------------------------------
# Решение вынесено в чистую функцию, поэтому проверяется на любой ОС: поднять
# на Linux процесс-зомби, который ловил бы этот баг, всё равно нельзя.

def test_win_zombie_with_open_handle_is_dead():
    """Главный случай. Процесс завершился, но родитель держит хэндл, поэтому
    OpenProcess его находит. Живым он от этого не становится."""
    assert _win_verdict(True, 0, 0) is False          # WAIT_OBJECT_0


def test_win_running_process_is_alive():
    assert _win_verdict(True, 0, 0x102) is True       # WAIT_TIMEOUT


def test_win_no_such_process_is_dead():
    assert _win_verdict(False, 87, 0) is False        # ERROR_INVALID_PARAMETER


def test_win_access_denied_counts_as_alive():
    """Чужой процесс существует — замок у него отбирать нельзя."""
    assert _win_verdict(False, 5, 0) is True          # ERROR_ACCESS_DENIED
