#!/usr/bin/env python3
"""launch_all.py — четыре системы в отдельных окнах, но как ЕДИНЫЙ организм.

Одной командой открывает в отдельных окнах/экранах, на ОДНОЙ монете:

  1) fast_monitor.py  — цена монеты чуть раньше сайта (Бот 1)
  2) book_monitor.py  — книга заявок Up/Down (Бот 2)
  3) pm_view.py       — ЗЕРКАЛО POLYMARKET: цена, Целевая цена и UP/DOWN
                        в центах ровно как на сайте, БЕЗ торговли
                        (старый торговый бот run.py: флаг --trader-bot)
  4) jump_trader.py   — СКАЧКОВАЯ СИСТЕМА: единственная, кто реально
                        торгует. Подключена к тем же данным, что и первые
                        три. По умолчанию dry-run; правила — в JUMP.md,
                        выключить — флаг --no-jump.

╔══ ГЛАВНОЕ: скорость и точность НЕ трогаем ═══════════════════════════════╗
║ Каждая — ОТДЕЛЬНЫЙ процесс операционной системы. Мы НИЧЕГО в них не      ║
║ меняем и НЕ сливаем в один цикл — просто запускаем вместе. Поэтому       ║
║ каждая работает ровно так же быстро и точно, как если бы ты запустил её  ║
║ вручную в своём окне. Никакого общего event-loop, никакой конкуренции.   ║
╚══════════════════════════════════════════════════════════════════════════╝

«Единый организм» = один старт, ОДНА монета во всех, и согласованная
остановка: Ctrl-C в этом окне гасит все разом. С флагом --restart
упавшую систему поднимаем автоматически (организм сам себя лечит).

Экраны — под твою платформу:
  * Windows           — отдельные консольные окна;
  * Linux (VPS/Цюрих) — сессия tmux `polymarket` с панелями
                        (подключиться: tmux attach -t polymarket);
  * macOS             — окна Terminal;
  * запасной вариант  — фоновые процессы + лог-файлы в logs/.

Примеры:
    python launch_all.py                     # btc, торговля в dry-run
    python launch_all.py --coin eth          # то же для ETH (пресет bot2.env)
    python launch_all.py --restart           # + авто-подъём упавшей системы
    python launch_all.py --no-jump           # без торговли, только мониторы
    python launch_all.py --no-trader         # без зеркала Polymarket
    python launch_all.py --trader-bot        # вместо зеркала — старый бот run.py
    python launch_all.py --live --stake 2    # РЕАЛЬНЫЕ деньги, ставка $2
    python launch_all.py --background        # без окон: фоном + логи в logs/
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"
COINS = ["btc", "eth", "sol", "xrp", "doge"]
# Пресеты трейдера по монете (bot1..4 = btc/eth/sol/xrp). Для doge пресета нет —
# трейдер запускается с ASSET через окружение.
PRESETS = {"btc": "bot1.env", "eth": "bot2.env",
           "sol": "bot3.env", "xrp": "bot4.env"}


# ---------------------------------------------------------------------------
#  Команды трёх систем (единая монета)
# ---------------------------------------------------------------------------
TRADER_BOT = False   # --trader-bot: вернуть старую панель run.py


def build_commands(coin: str, live: bool, include_trader: bool,
                   include_jump: bool = True, stake=None, max_round=None):
    """Список (title, argv, env_overrides|None) — по одному на окно."""
    cmds = [
        ("Fast Monitor — цена",
         [PY, "fast_monitor.py", "--coin", coin, "--auto-target"], None),
        ("Book Monitor — книга",
         [PY, "book_monitor.py", "--coin", coin], None),
    ]
    if include_jump:
        # 4-я система — единственная, кто реально торгует (см. JUMP.md).
        jargv = [PY, "jump_trader.py", "--coin", coin]
        jargv += ["--live"] if live else ["--dry-run"]
        if stake is not None:
            jargv += ["--stake", str(stake)]
        if max_round is not None:
            jargv += ["--max-round", str(max_round)]
        cmds.append((f"Сделки — скачковая система "
                     f"({'LIVE' if live else 'dry-run'})", jargv, None))
    if include_trader:
        # Первая панель — ЗЕРКАЛО POLYMARKET (pm_view.py): цена, Целевая цена,
        # UP/DOWN в центах ровно как на сайте, из тех же источников. Никакой
        # торговли и никакой имитации — только просмотр.
        # Нужен старый торговый бот (run.py, dry-run/LIVE)? -> --trader-bot
        if TRADER_BOT:
            argv = [PY, "run.py"]
            env = None
            preset = PRESETS.get(coin)
            preset_path = os.path.join(HERE, preset) if preset else ""
            if preset and os.path.exists(preset_path):
                # run.py --env-file требует python-dotenv; если пакета нет,
                # разбираем пресет сами и передаём через окружение
                from dashboard import _has_dotenv, parse_env_file
                if _has_dotenv():
                    argv += ["--env-file", preset]
                else:
                    env = parse_env_file(preset_path)
            else:
                # монета без пресета (doge): монету задаём через ASSET
                env = {
                    "ASSET": coin,
                    "BTC_PRICE_URL":
                        f"https://api.coinbase.com/v2/prices/"
                        f"{coin.upper()}-USD/spot",
                }
            argv += ["--live"] if live else ["--dry-run"]
            cmds.insert(0, (f"Трейдер run.py ({'LIVE' if live else 'dry-run'})",
                            argv, env))
        else:
            cmds.insert(0, ("Polymarket — рынок как на сайте",
                            [PY, "pm_view.py", "--coin", coin], None))
    return cmds


def _env_for(overrides):
    e = os.environ.copy()
    if overrides:
        e.update(overrides)
    return e


def _shell_line(argv, overrides, keep_open=True) -> str:
    """Единая shell-строка: cd в папку + env + команда (+ не закрывать панель)."""
    prefix = "".join(f"{k}={shlex.quote(str(v))} " for k, v in (overrides or {}).items())
    cmd = prefix + " ".join(shlex.quote(a) for a in argv)
    line = f"cd {shlex.quote(HERE)} && {cmd}"
    if keep_open:                       # после выхода — оставить панель с shell'ом
        line += "; echo; echo '--- процесс завершён; Enter/закрой ---'; " \
                "exec ${SHELL:-sh}"
    return line


# ---------------------------------------------------------------------------
#  Бэкенд: Windows — три отдельных консольных окна + супервизор
# ---------------------------------------------------------------------------
def launch_windows(cmds, restart: bool) -> int:
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    procs = []
    for title, argv, env in cmds:
        p = subprocess.Popen(argv, cwd=HERE, env=_env_for(env),
                             creationflags=flags)
        print(f"[launch] окно: {title}  (pid {p.pid})")
        procs.append([title, argv, env, p])
    print("\nТри окна открыты. Это окно — «мозг» организма: Ctrl-C здесь "
          "остановит все три." + ("  Авто-рестарт: ВКЛ." if restart else ""))
    return _supervise(procs, restart, windows=True)


# ---------------------------------------------------------------------------
#  Бэкенд: Linux/VPS — tmux (три панели). Организмом управляет сессия tmux.
# ---------------------------------------------------------------------------
def launch_tmux(cmds, session: str, attach: bool) -> bool:
    if not shutil.which("tmux"):
        return False
    subprocess.run(["tmux", "kill-session", "-t", session],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    for i, (title, argv, env) in enumerate(cmds):
        line = _shell_line(argv, env)
        if i == 0:
            subprocess.run(["tmux", "new-session", "-d", "-s", session,
                            "-n", "polymarket", line], check=True)
        else:
            subprocess.run(["tmux", "split-window", "-t", session, line],
                           check=True)
        subprocess.run(["tmux", "select-layout", "-t", session, "tiled"],
                       stdout=subprocess.DEVNULL)
    # заголовки панелей + не синхронизировать ввод
    subprocess.run(["tmux", "set-option", "-t", session, "-g",
                    "pane-border-status", "top"], stderr=subprocess.DEVNULL)
    print(f"[launch] tmux-сессия '{session}' с {len(cmds)} панелями создана.")
    print(f"  подключиться:  tmux attach -t {session}")
    print(f"  остановить всё: tmux kill-session -t {session}")
    if attach and sys.stdout.isatty():
        os.execvp("tmux", ["tmux", "attach", "-t", session])   # заменяет процесс
    return True


# ---------------------------------------------------------------------------
#  Бэкенд: macOS — три окна Terminal через osascript
# ---------------------------------------------------------------------------
def launch_macos(cmds) -> bool:
    if not shutil.which("osascript"):
        return False
    for title, argv, env in cmds:
        line = _shell_line(argv, env, keep_open=False)
        script = ('tell application "Terminal" to do script '
                  f'{_applescript_quote(line)}')
        subprocess.run(["osascript", "-e", script],
                       stdout=subprocess.DEVNULL)
    subprocess.run(["osascript", "-e",
                    'tell application "Terminal" to activate'],
                   stdout=subprocess.DEVNULL)
    print(f"[launch] открыто {len(cmds)} окна Terminal (macOS).")
    return True


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
#  Бэкенд: фон + лог-файлы (любая ОС, headless) + супервизор
# ---------------------------------------------------------------------------
def launch_background(cmds, restart: bool) -> int:
    logs = os.path.join(HERE, "logs")
    os.makedirs(logs, exist_ok=True)
    procs = []
    for title, argv, env in cmds:
        name = os.path.splitext(os.path.basename(argv[1]))[0]
        logf = open(os.path.join(logs, f"{name}.log"), "a", buffering=1)
        logf.write(f"\n===== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"({title}) =====\n")
        logf.flush()
        p = subprocess.Popen(argv, cwd=HERE, env=_env_for(env),
                             stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        print(f"[launch] фон: {title}  (pid {p.pid})  -> logs/{name}.log")
        procs.append([title, argv, env, p])
    print("\nЛоги: logs/*.log  (смотреть:  tail -f logs/fast_monitor.log)")
    print("Ctrl-C здесь остановит все три."
          + ("  Авто-рестарт: ВКЛ." if restart else ""))
    return _supervise(procs, restart, windows=False)


# ---------------------------------------------------------------------------
#  Супервизор: согласованная остановка + опциональный авто-рестарт
# ---------------------------------------------------------------------------
def _relaunch(entry, windows):
    title, argv, env, _ = entry
    if windows:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        entry[3] = subprocess.Popen(argv, cwd=HERE, env=_env_for(env),
                                   creationflags=flags)
    else:
        logs = os.path.join(HERE, "logs")
        name = os.path.splitext(os.path.basename(argv[1]))[0]
        logf = open(os.path.join(logs, f"{name}.log"), "a", buffering=1)
        entry[3] = subprocess.Popen(argv, cwd=HERE, env=_env_for(env),
                                   stdout=logf, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    print(f"[launch] перезапустил: {title}  (pid {entry[3].pid})")


def _stop_all(procs):
    for title, argv, env, p in procs:
        if p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                else:
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass
    # добить, если кто-то не закрылся
    deadline = time.time() + 5
    for title, argv, env, p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass


def _supervise(procs, restart: bool, windows: bool) -> int:
    try:
        while True:
            time.sleep(1.0)
            alive = 0
            for entry in procs:
                p = entry[3]
                if p.poll() is None:
                    alive += 1
                elif restart:
                    print(f"[launch] '{entry[0]}' упал (код {p.returncode}) — "
                          f"поднимаю")
                    _relaunch(entry, windows)
                    alive += 1
            if alive == 0 and not restart:
                print("[launch] все три завершились. Выход.")
                return 0
    except KeyboardInterrupt:
        print("\n[launch] Ctrl-C — останавливаю все три системы...")
        _stop_all(procs)
        print("[launch] остановлено.")
        return 0


# ---------------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Запуск трёх систем в трёх окнах как единый организм "
                    "(процессы раздельные — скорость/точность не меняются).")
    ap.add_argument("--coin", choices=COINS, default="btc",
                    help="монета для всех трёх (по умолчанию btc)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="трейдер в симуляции (по умолчанию)")
    mode.add_argument("--live", action="store_true",
                      help="трейдер реальными деньгами (нужны креды в пресете)")
    ap.add_argument("--trader-bot", action="store_true",
                    help="в первой панели показать старый торговый бот "
                         "run.py вместо зеркала Polymarket")
    ap.add_argument("--no-trader", action="store_true",
                    help="запустить только два монитора (просто смотреть)")
    ap.add_argument("--no-jump", action="store_true",
                    help="не запускать 4-ю (торгующую) систему — только показ")
    ap.add_argument("--stake", type=float,
                    help="ставка скачковой системы, USDC (по умолч. 1)")
    ap.add_argument("--max-round", type=float,
                    help="потолок вложений скачковой системы за раунд, USDC")
    ap.add_argument("--restart", action="store_true",
                    help="авто-подъём упавшей системы (организм лечит сам себя)")
    backend = ap.add_mutually_exclusive_group()
    backend.add_argument("--windows", action="store_true",
                         help="форсировать отдельные консольные окна")
    backend.add_argument("--tmux", action="store_true",
                         help="форсировать tmux (Linux/VPS)")
    backend.add_argument("--background", action="store_true",
                         help="без окон: фон + логи в logs/")
    ap.add_argument("--session", default="polymarket",
                    help="имя tmux-сессии (по умолчанию polymarket)")
    ap.add_argument("--no-attach", action="store_true",
                    help="tmux: не подключаться автоматически")
    args = ap.parse_args(argv)

    global TRADER_BOT
    TRADER_BOT = args.trader_bot
    cmds = build_commands(args.coin, live=args.live,
                          include_trader=not args.no_trader,
                          include_jump=not args.no_jump,
                          stake=args.stake, max_round=args.max_round)

    print(f"=== launch_all: {args.coin.upper()} | "
          f"{'ТОЛЬКО МОНИТОРЫ' if args.no_trader else ('LIVE' if args.live else 'dry-run')}"
          f" | {len(cmds)} окна ===")
    if args.live:
        print("⚠️  LIVE: трейдер будет ставить РЕАЛЬНЫЕ ордера. Убедись, что в "
              "пресете заполнены PRIVATE_KEY/FUNDER.")

    # Выбор бэкенда: явный флаг > авто по платформе.
    if args.background:
        return launch_background(cmds, args.restart)
    if args.windows or (os.name == "nt" and not args.tmux):
        if os.name != "nt":
            print("[launch] --windows вне Windows не поддержан; "
                  "использую tmux/фон.")
        else:
            return launch_windows(cmds, args.restart)
    if args.tmux or os.name != "nt":
        if launch_tmux(cmds, args.session, attach=not args.no_attach):
            return 0
        if sys.platform == "darwin" and launch_macos(cmds):
            return 0
        print("[launch] tmux не найден — падаю в фоновый режим (logs/).")
        return launch_background(cmds, args.restart)

    # запасной путь
    return launch_background(cmds, args.restart)


if __name__ == "__main__":
    raise SystemExit(main())
