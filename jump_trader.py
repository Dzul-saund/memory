#!/usr/bin/env python3
"""Тонкая обёртка запуска 4-й (скачковой) системы из корня репозитория.

Эквивалентно `python -m flowbot.jump_run`. Это же имя запускает dashboard.py
в четвёртой панели.

    python jump_trader.py                 # dry-run, BTC, ставка $1
    python jump_trader.py --live          # реальные ордера (нужны креды)
    python jump_trader.py --stake 2 --max-round 30
"""
from flowbot.jump_run import main

if __name__ == "__main__":
    raise SystemExit(main())
