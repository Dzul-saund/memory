"""Automated trading bot for Polymarket BTC Up/Down 5-minute markets.

The package is split so that the parts you can test without any credentials
(`strategy`, `config`) never import the trading SDK:

    config.py    - configuration loaded from environment / .env
    util.py      - retry/backoff + small helpers
    data.py      - read-only market data (Gamma + CLOB + BTC price), no auth
    strategy.py  - pure buy/no-buy decision (no I/O, fully unit-tested)
    trader.py    - live + dry-run order placement and balance (py-clob-client)
    bot.py       - the main loop tying it all together
"""

__version__ = "1.0.0"
