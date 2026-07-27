#!/usr/bin/env python3
"""Тонкая обёртка запуска flowbot из корня репозитория.

Эквивалентно `python -m flowbot.run`. Удобно для двойного клика / .bat.

    python flow_trader.py                 # dry-run, BTC, ставка $1
    python flow_trader.py --live          # реальные ордера (нужны креды)
    python flow_trader.py --ping-ms 90    # пинг Цюрих->Polymarket вручную
"""
from flowbot.run import main

if __name__ == "__main__":
    raise SystemExit(main())
