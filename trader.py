#!/usr/bin/env python3
"""Запуск бота одной командой: `python trader.py`.

Тонкая обёртка над `flowbot.cli` — чтобы не набирать `python -m flowbot.cli`.
"""
from flowbot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
