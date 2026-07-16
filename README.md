# Polymarket Crypto Up/Down 5m trading bots (BTC / ETH / SOL / XRP)

An automated bot that watches the **Up or Down — 5 minute** markets on
[Polymarket](https://polymarket.com) and buys the `Up` or `Down` outcome when a
simple "buy-the-favourite" condition is met.

It does not care about direction. It buys whichever side (Up *or* Down)
satisfies the strategy. The coin is set per preset (`ASSET=btc/eth/sol/xrp`),
so the four shipped presets run the same strategy on four different coins.

---

## Strategy

Every second the bot looks at the current 5-minute market and buys an outcome
**only when all of these are true**:

| Condition | Preset default (bot1–bot4) |
|---|---|
| Time-based price band (see below) | `> 120s` left → only `0.99`; `≤ 120s` left → `0.98 … 0.99` |
| Early trend rule | `> 120s` left → the side's price must be **rising** |
| Time left until the market closes | no limit (`TIME_WINDOW_SECONDS=600` ≥ window) |
| Balance is enough for the trade | `≥ $50` |
| It has **not** already traded in this window | one trade per 5-min market |
| *(optional)* distance from the window's open | off (`MIN_TARGET_DISTANCE_USDC=0`) |

`Up` is checked first, then `Down`. Because the two prices are complementary
(`Up + Down ≈ 1`) at most one side can ever be in the band, so there is never a
real conflict.

### The early/late price rule (new)

The buy band depends on **how much time is left** in the 5-minute window:

* **More than `EARLY_THRESHOLD_SECONDS` (120s) left** — the bot buys **only at
  `EARLY_PRICE_MIN`..`PRICE_MAX` (only 0.99)**, and only while that side's
  price is **trending up**: the price now must be at/above where it was
  `TREND_LOOKBACK_SECONDS` (30s) ago. A falling price, or a side with no
  history yet (right after the window opens), is never bought early.
* **120 seconds or less left** — the normal `PRICE_MIN`..`PRICE_MAX` band
  (0.98..0.99) applies, with no trend requirement.

Set `EARLY_THRESHOLD_SECONDS=0` to disable the split, or
`EARLY_REQUIRE_RISING=false` to keep the tighter early band without the trend
check.

### Stop-loss (on in all presets)

After a buy fills, the bot watches the price it could sell at (the bid):

* **soft** — bid ≤ `0.50` with ≤ `8s` left in the window → sell to recover cash;
* **hard** — bid ≤ `0.46` at any time → sell immediately.

When the current 5-minute window ends, the bot automatically rolls over to the
next `<asset>-updown-5m-*` market and the per-window trade flag resets.

### When it stops

- the run duration elapses (default **24h**, configurable);
- balance drops to/below `MIN_BALANCE_USDC` (default `$1`);
- too many consecutive API failures in a row (default `8`) — a *safe* stop;
- `Ctrl-C`.

---

## What it reads each cycle

- the current `Up/Down 5m` market for the preset's coin (deterministic slug
  `<asset>-updown-5m-<window_start_ts>`);
- time remaining until the market closes;
- `Up` price and `Down` price (best ask from the CLOB order book by default);
- target price (the coin's price at the window's open — see note below);
- the coin's current price (Chainlink via Polymarket, Coinbase spot fallback);
- your Polymarket USDC balance.

### Fair-probability model (the bot's own percentage)

The quoted 0.98/0.99 is just what the crowd pays — not always the real
probability. Each tick the bot computes its **own** estimate from first
principles:

```
P(Up) = Φ( (price − target) / (σ · √seconds_left) )
```

* **price** — live Chainlink price (streamed);
* **target** — the exact window-open price from Polymarket;
* **σ** — the coin's live volatility: RMS of ~1-second moves over the last
  `MODEL_VOL_LOOKBACK_SECONDS` (120s), so the model notices the market
  speeding up or calming down within a minute or two;
* **Φ** — standard normal CDF.

With the filter on (`MODEL_FILTER_ENABLED=true`) a side is bought **only when
the model's probability for it is ≥ `MODEL_MIN_PROB`** (0.995). Break-even at
an entry price of 0.99 is a 99.0% win rate, so the threshold keeps a safety
margin above it — entries where the quoted 0.99 is *not* backed by the math
(the ones that reverse in the last seconds) are skipped. The estimate is shown
every second in the status line (`P(Up)=99.3%`), included in every BUY reason,
and written to the trade CSV (`model_prob_at_entry`) so you can verify the
model against actual outcomes.

`MODEL_STRICT=true` (default) also blocks buying while the model has no
estimate yet — the first ~30 seconds after startup while σ history
accumulates. Set `MODEL_FILTER_ENABLED=false` to turn the whole thing off.

### Speed: how fast the bot reacts

The data path is fully event-driven, so a price move is seen in **milliseconds**
and the decision loop runs **5× per second** (`POLL_INTERVAL_SECONDS=0.2`):

* **Order books over WebSocket** (`BOOK_FEED_ENABLED=true`) — the CLOB pushes
  every bid/ask change for the current window's Up/Down tokens the moment it
  happens; no more `GET /book` HTTP polling (~300-700 ms per cycle before).
  If the socket drops or `websocket-client` is missing, the bot silently falls
  back to HTTP — slower, but nothing breaks.
* **Market lookup cached per window** — the slug is deterministic, so Gamma is
  asked once per 5-minute window, not every second.
* **Balance cached** (`BALANCE_REFRESH_SECONDS=5`) — it only changes when the
  bot trades, and it is force-refreshed right after every order.
* **Chainlink live price** streams in a background thread (as before).

Net effect: a tick that used to spend 0.7-2 s on sequential HTTP now completes
in well under a millisecond, and the only remaining latency on a buy is the
order POST itself. The status line is throttled to one per second
(`STATUS_LOG_INTERVAL_SECONDS`) so the log stays readable at 5 ticks/second.

> **Note on "target price" and the live price.** The market resolves off the
> **Chainlink** price stream (price at window open vs. close). The bot reads
> the **exact** target (window open price) from Polymarket's own feed
> (`/api/crypto/crypto-price`) — the same number shown on the site — so it
> matches **1:1**. The **live** price shown each tick is the **Chainlink**
> price streamed from Polymarket's live-data WebSocket
> (`wss://ws-live-data.polymarket.com`), updated ~1×/s by a background thread.
> This needs `websocket-client`; if it's missing or can't connect, the bot
> falls back to Coinbase spot and the target falls back to `(approx)`.

---

## Install

Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

> Only `requests` + `python-dotenv` are needed to run in **dry-run**.
> `py-clob-client` is required for real balance reads and live trading.

---

## The four presets (one coin each)

| Preset | Coin | Strategy |
|---|---|---|
| `bot1.env` | **Bitcoin** (BTC) | identical (see below) |
| `bot2.env` | **Ethereum** (ETH) | identical |
| `bot3.env` | **Solana** (SOL) | identical |
| `bot4.env` | **XRP** | identical |

All four use the **same conditions** (taken from the old bot4):

* stake **$50** per trade, one trade per 5-minute window;
* **> 120s left** → buy only at **0.99** and only while the price is rising;
* **≤ 120s left** → buy at **0.98–0.99**;
* no time restriction otherwise, no target-distance cushion, no hedge;
* stop-loss on (soft ≤ 0.50 in the last 8s; hard ≤ 0.46 anytime);
* each writes its own log: `bot1_trades.csv` … `bot4_trades.csv`.

Run them side by side in separate terminals:

```bash
python run.py --env-file bot1.env   # Bitcoin
python run.py --env-file bot2.env   # Ethereum
python run.py --env-file bot3.env   # Solana
python run.py --env-file bot4.env   # XRP
```

> ⚠️ The presets ship with `DRY_RUN=false` (live trading). They will refuse to
> start until you fill in `PRIVATE_KEY` / `FUNDER` in each file. To simulate
> first, add `--dry-run` to the command line (it overrides the preset):
> `python run.py --env-file bot1.env --dry-run --once`.

---

## Going live (real money)

1. In each `botN.env` set:
   - `PRIVATE_KEY` — from Polymarket → **Settings → Export Private Key**
   - `FUNDER` — the address that holds your funds (your Polymarket deposit
     address)
   - `SIGNATURE_TYPE` — `3` for the new deposit-wallet accounts (preset
     default), `1` for email/Magic logins, `2` for a browser wallet (MetaMask),
     `0` for a raw EOA wallet.

2. (One-time, usually only needed for EOA wallets) approve the USDC allowance:

   ```bash
   python run.py --setup-allowances
   ```

3. Run each bot (see above).

Orders are placed as **marketable limit Fill-And-Kill** orders capped at the
observed price — you never pay more than the band's price, and nothing is left
resting on the book.

---

## CLI

```
python run.py [options]

  --once                 run a single cycle and exit (verify setup)
  --live                 place REAL orders (needs credentials)
  --dry-run              force simulation mode (overrides the preset)
  --hours FLOAT          how long to run, in hours (default 24)
  --trade-size FLOAT     USDC to spend per trade
  --min-target-distance FLOAT
                         only buy when the coin is >= this many USD from the
                         window open (target), in the favourite's direction
  --env-file PATH        load config from a named preset (e.g. bot1.env)
  --price-source {ask,mid,last}
                         which price drives the decision (default: ask)
  --setup-allowances     set the USDC trading allowance, then exit
  --log-level LEVEL      DEBUG / INFO / WARNING (default INFO)
```

CLI flags override `--env-file` / `.env`, which override the built-in defaults.

---

## Configuration reference

All settings live in the preset `.env` files (see `.env.example` for the full
annotated list). The most important ones:

| Variable | Preset value | Meaning |
|---|---|---|
| `ASSET` | `btc`/`eth`/`sol`/`xrp` | which coin's Up/Down 5m market to trade |
| `DRY_RUN` | `false` | `true` = simulation only |
| `PRICE_MIN` / `PRICE_MAX` | `0.98` / `0.99` | the normal (late) buy band |
| `EARLY_THRESHOLD_SECONDS` | `120` | with more time left than this, the early band applies (0 = off) |
| `EARLY_PRICE_MIN` | `0.99` | early band lower bound (only 0.99 by default) |
| `EARLY_REQUIRE_RISING` | `true` | early buys also need a rising price |
| `TREND_LOOKBACK_SECONDS` | `30` | "rising" = now at/above the price this many seconds ago |
| `MODEL_FILTER_ENABLED` | `true` | buy only when the bot's own P(side) confirms the price |
| `MODEL_MIN_PROB` | `0.995` | minimum model probability for the bought side |
| `MODEL_VOL_LOOKBACK_SECONDS` | `120` | window for the live σ (volatility) estimate |
| `MODEL_STRICT` | `true` | no model estimate yet → no buy (cold-start safety) |
| `TIME_WINDOW_SECONDS` | `600` | only buy when ≤ this many seconds remain (600 ≥ window = no limit) |
| `MIN_TARGET_DISTANCE_USDC` | `0` | only buy when the coin is ≥ this many USD from the open (0 = off) |
| `TRADE_SIZE_USDC` | `50.0` | spend per trade (Polymarket min ≈ $5) |
| `STOPLOSS_ENABLED` | `true` | sell a losing position (soft 0.50/8s, hard 0.46) |
| `MIN_BALANCE_USDC` | `1.0` | stop the bot at/below this balance |
| `RUN_DURATION_SECONDS` | `86400` | total runtime (24h) |
| `POLL_INTERVAL_SECONDS` | `0.2` | decision frequency (5× per second) |
| `BOOK_FEED_ENABLED` | `true` | millisecond bid/ask over WebSocket (HTTP fallback) |
| `BALANCE_REFRESH_SECONDS` | `5` | balance cache TTL (refreshed after each order) |
| `STATUS_LOG_INTERVAL_SECONDS` | `1.0` | status line at most once per this many seconds |
| `PRICE_SOURCE` | `ask` | `ask` / `mid` / `last` |
| `TRADE_LOG_CSV` | `botN_trades.csv` | CSV trade log path (`""` disables) |
| `BTC_PRICE_URL` | per coin | Coinbase spot fallback URL, must match `ASSET` |
| `MAX_RETRIES` | `4` | per-request retries (backoff 2s,4s,8s,16s) |
| `MAX_CONSECUTIVE_ERRORS` | `8` | safe-stop threshold |

---

## Trade log (CSV)

Every settled trade is appended to the preset's CSV (configurable via
`TRADE_LOG_CSV`, set to `""` to disable). One row per trade with the entry
price, outcome, payout, P&L and running totals — open it in Excel/Sheets to
review performance. Columns:

```
settled_at_utc, slug, outcome, entry_price, shares, cost, target_open,
btc_at_entry, secs_to_end_at_entry, result, settle_price, payout, pnl,
cumulative_pnl, balance_after
```

---

## Backtesting

`backtest.py` replays the *classic* band strategy on **real historical data**
— actual resolutions from Gamma and real entry prices from the CLOB
`prices-history` endpoint (no look-ahead). Note: it models the price band and
entry timing, but not the new early-trend rule or the stop-loss.

```bash
python backtest.py --windows 300                 # default band
python backtest.py --windows 300 --price-max 0.92
python backtest.py --windows 300 --sweep         # compare price caps
python backtest.py --windows 300 --slippage 0.01 # model paying the ask
```

---

## How it works (architecture)

```
btc_bot/
  config.py    configuration from env / .env
  util.py      retry/backoff + helpers
  data.py      read-only market data (Gamma + CLOB + coin price) — no credentials
  bookfeed.py  live order books over the CLOB WebSocket (millisecond bid/ask)
  prob.py      fair-probability model: live σ estimate + P(Up) (pure math)
  strategy.py  pure buy/no-buy decision  — fully unit-tested, no I/O
  trader.py    live + dry-run order placement & balance (py-clob-client)
  tradelog.py  append-only CSV log of settled trades
  bot.py       the main loop (incl. price-trend tracking for the early rule)
run.py         CLI entry point
backtest.py    historical backtest harness (Gamma + CLOB)
tests/         unit tests (strategy, settlement, trade log)
```

**Resilience.** Every network call retries with exponential backoff
(`2s → 4s → 8s → 16s`). A failed cycle (after retries) increments a counter; the
bot stops safely after `MAX_CONSECUTIVE_ERRORS` consecutive failures instead of
crashing or hammering the API. A window is marked "traded" *before* the order is
sent, so a transient error can never produce a duplicate order in the same
window.

---

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The strategy is a pure function, so the tests run with no network and no
credentials.

---

## ⚠️ Disclaimer

This software is for educational purposes. Trading prediction markets carries
real financial risk and you can lose money. **Always run in dry-run first**,
start with a small `TRADE_SIZE_USDC`, and never trade funds you cannot afford to
lose. You are solely responsible for use of this bot and for complying with
Polymarket's terms and the laws of your jurisdiction. No warranty of any kind.
