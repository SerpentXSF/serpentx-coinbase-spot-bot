# SerpentX Coinbase USDC Spot Bot

Current version: **v0.4.0-beta**

Educational Coinbase Advanced Trade **spot** bot for scanning USDC crypto pairs, rotating a watchlist, and managing risk-gated market orders.

> **Disclaimer:** This project is for education and experimentation. It is not financial advice, does not guarantee profits, and can lose money. Run in status/preview mode first. You are responsible for your own API keys, risk settings, taxes, and compliance.

## What it does

- Scans Coinbase spot products quoted in USDC.
- Rotates the strongest candidates into a top-5 watchlist.
- Scores candidates using technical momentum signals plus optional public context.
- Filters out Coinbase products marked trading-disabled, limit-only, cancel-only, or disabled before adding them to a market-order watchlist.
- Tracks bot-managed open positions in local state.
- Exits positions with take-profit, stop-loss, or soft-invalidation logic.
- Includes a lightweight exit monitor that can run more often than the full market scanner.
- Includes RSI bullish/bearish divergence labels, 5-minute entry confirmation, fee-aware entry guards, trailing-stop support, and stale duplicate-position cleanup.
- Includes a local Red/Purple analytics dashboard with responsive cards, charts, snapshots, order reconciliation, and candidate follow-through metrics.
- Defaults to preview mode; live orders require multiple explicit gates.

## Not required

You do **not** need any specific agent, chat bot, or scheduler platform to run this bot. It is plain Python and can run on Linux, macOS, Windows/WSL, a VPS, or a local machine.

## Strategy overview

The bot computes:

```text
final_score = technical_score + context_score
context_score = news_score + market_score + social_score + whale_score + risk_penalty
```

Technical scoring considers:

- 15-minute EMA stack alignment
- price vs 1-hour EMA50
- 1-hour trend alignment
- RSI support or pullback setup
- 24-hour momentum
- Coinbase volume/liquidity

Optional public/free context can use:

- Google News RSS headline keywords
- CoinGecko market data and trending data
- Alternative.me Fear & Greed Index
- Reddit public JSON search
- Helius RPC for Solana mint activity when configured

All external sources are cached by default to reduce rate-limit pressure.

## Spot-only behavior

This bot is spot-only. It can buy qualified assets and sell back to USDC. It does **not** perform true shorting, leverage, futures, margin, or perps trading. Short/downside scoring is informational and can be used to avoid entries or hold USDC in bearish regimes.

## Safety design

Live trading requires **all** of these gates:

1. `active_trading: true` in `config.json`
2. `COINBASE_TRADING_ENABLED=1` in `.env`
3. running the command with `--live`

The default `config.example.json` ships with live trading disabled.

Additional protections include:

- daily trade cap
- max open positions
- min/max trade sizing
- percent-of-account sizing cap
- take-profit rule
- stop-loss rule
- soft-invalidation rule
- cooldown after trades
- duplicate-position prevention
- no averaging down into the same asset

## Requirements

- Python 3.10+
- Coinbase Advanced Trade API key
- API key with trading permission only if you intend to trade live
- **No withdrawal/transfer permission** recommended

## Install on Linux / macOS / WSL

```bash
git clone https://github.com/SerpentXSF/serpentx-coinbase-spot-bot.git
cd serpentx-coinbase-spot-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
chmod 600 .env
```

If your distro blocks system `pip` with PEP 668, keep using the virtual environment above, or install with `uv`:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## Install on Windows PowerShell

```powershell
git clone https://github.com/SerpentXSF/serpentx-coinbase-spot-bot.git
cd serpentx-coinbase-spot-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item .env.example .env
```

If PowerShell blocks activation scripts, run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then reopen PowerShell and activate the venv again. Windows users can also run the Linux instructions inside WSL.

## Generic dependency install

If you already cloned the repo and created `config.json` / `.env`, install dependencies with:

```bash
python -m pip install -r requirements.txt
```

## Setup

1. Clone the repository if you did not already do so.

```bash
git clone https://github.com/SerpentXSF/serpentx-coinbase-spot-bot.git
cd serpentx-coinbase-spot-bot
```

2. Create local config and environment files.

```bash
cp config.example.json config.json
cp .env.example .env
chmod 600 .env  # Linux/macOS
```

3. Edit `.env` and add your own Coinbase API credentials.

```text
COINBASE_API_KEY_NAME=organizations/.../apiKeys/...
COINBASE_API_PRIVATE_KEY="PASTE_YOUR_PEM_PRIVATE_KEY_WITH_ESCAPED_NEWLINES"
COINBASE_TRADING_ENABLED=0
```

Keep `COINBASE_TRADING_ENABLED=0` until you intentionally enable live trading.

4. Optional: add external provider keys.

```text
HELIUS_API_KEY=your_helius_key_here
# or
HELIUS_RPC_URL=optional_full_helius_rpc_url
```

## Basic usage

### Status/read-only run

Checks market data and, if credentials are present, relevant account balances. Does not place orders.

```bash
python coinbase_spot_bot.py --config config.json --status
```

### Preview-only strategy run

Builds a proposed order preview but does not place a live order.

```bash
python coinbase_spot_bot.py --config config.json
```

### Rotate watchlist and run preview

Scans Coinbase USDC pairs, writes the top 5 to `config.json`, then runs the bot in preview mode.

```bash
python rotate_and_run.py --json
```

### Lightweight exit monitor

Checks existing bot-managed positions only. It does not scan the whole market and is designed to run more frequently than the rotator.

Preview/status output:

```bash
python exit_monitor.py --json
```

Live exit monitor, after all live gates are intentionally enabled:

```bash
python exit_monitor.py --live
```

The monitor stays quiet on normal hold/no-position ticks unless `--json` is supplied.

### Local performance dashboard

Run a local, read-only dashboard from your own bot state/log files. The dashboard includes responsive dark Red/Purple styling, balances/position cards, SVG charts, recent run/trade tables, candidate snapshots, forward-return summaries, and optional order reconciliation data.

```bash
python trade_analytics_dashboard.py --host 127.0.0.1 --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

The dashboard is designed for local use. It reads files such as `state/state.json`, `logs/runs.jsonl`, `logs/trades.jsonl`, `analysis/analytics_snapshots.jsonl`, and `analysis/order_reconciliation.json` when present. Do not expose the dashboard publicly unless you understand your network, proxy, and authentication setup.

If your live bot state is in another folder, point the dashboard at it without moving secrets:

```bash
COINBASE_BOT_ROOT=/path/to/your/local/bot python trade_analytics_dashboard.py --host 127.0.0.1 --port 8787
```

### Candidate forward backtest

Replay recent scanner output against later public candles to sanity-check candidate selection:

```bash
python candidate_forward_backtest.py --help
```

### Order reconciliation

After live trading, reconcile recent locally logged Coinbase orders against Coinbase historical order status:

```bash
python reconcile_orders.py --limit 10 --json
```

This writes `analysis/order_reconciliation.json` for the dashboard. It is intentionally manual/low-frequency to protect API limits.

### Live run

Only after you understand the risks and have reviewed your config:

```bash
# 1. Set in .env:
# COINBASE_TRADING_ENABLED=1

# 2. Set in config.json:
# "active_trading": true

# 3. Run with --live:
python coinbase_spot_bot.py --config config.json --live
```

## Scheduling examples

### Linux/macOS cron

Every 6 hours, rotate the watchlist and run the strategy in preview mode:

```cron
0 */6 * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python rotate_and_run.py --json >> logs/cron.log 2>&1
```

Every 15 minutes, check exits only:

```cron
*/15 * * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python exit_monitor.py >> logs/exit-monitor.log 2>&1
```

For live mode, add `--live` only after enabling the config and env gates.

### systemd timer / Docker / Windows Task Scheduler

Any scheduler can run the same Python commands above. The project does not require any agent runtime or app platform.

## Files

```text
coinbase_spot_bot.py      Main strategy bot
analyze_usdc_pairs.py     Public Coinbase USDC market scanner
rotate_and_run.py         Refresh top-5 watchlist and run bot
exit_monitor.py           Lightweight exit-only monitor
trade_analytics_dashboard.py Local read-only performance dashboard
candidate_forward_backtest.py Candidate follow-through analysis helper
coinbase_client.py        Minimal Coinbase Advanced Trade helper
reconcile_orders.py       Low-call historical order reconciliation helper
DUAL_DIRECTION_SETUP.md    Notes on spot-safe dual-direction scoring
scripts/*.sh              Portable shell wrappers for cron/systemd
config.example.json       Safe default config template
.env.example              Secret template; copy to .env locally
requirements.txt          Python dependencies
```

## What not to commit

Never commit:

- `.env`
- API keys/private keys
- `config.json` if it contains sensitive local settings
- `state/`
- `logs/`
- `analysis/`
- trade history
- account balances

The `.gitignore` in this repo excludes those by default.

## API key guidance

Use the least permissions required:

- trading only if you intend to trade
- no withdrawals
- no transfers
- consider IP allowlisting if your exchange account supports it
- rotate/revoke keys if they are ever pasted publicly or committed

## Development checks

```bash
python -m py_compile coinbase_spot_bot.py analyze_usdc_pairs.py rotate_and_run.py exit_monitor.py coinbase_client.py trade_analytics_dashboard.py candidate_forward_backtest.py
python coinbase_spot_bot.py --config config.example.json --status
```

The second command may use public endpoints and optional providers. Private account checks require your local `.env`.
