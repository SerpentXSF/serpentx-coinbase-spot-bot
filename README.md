# SerpentX Coinbase USDC Spot Bot

Current version: **v0.5.0-beta**

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
- Runs in public, read-only mode with no API keys, so you can try it before creating credentials.

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
- price vs 30-minute EMA50
- 30-minute trend alignment
- RSI support or pullback setup
- 24-hour momentum
- Coinbase volume/liquidity

RSI defaults to the bot's original simple 14-period average (`"rsi_method": "simple"`). Set `"rsi_method": "wilder"` to use Wilder's smoothed RSI, which is what TradingView and most charting sites show. The two can differ by several points on the same data.

**Sizing note:** each entry is capped at `max_account_quote_pct_per_trade` (15%) of min(available USDC, `sizing_quote_balance_target` = 100), and must be at least `min_quote_per_trade` (15). With the example values, as soon as available USDC drops below 100 (after a small loss, or while 15 USDC sits in a first position), a threshold-score entry sizes below 15 and is skipped as `ORDER_TOO_SMALL`. Only stronger scores, which get a size multiplier, still trade. Keep more than `sizing_quote_balance_target` in USDC, or lower `min_quote_per_trade`, if you want threshold entries and a second position to go through.

Optional public/free context can use:

- Google News RSS headline keywords
- CoinGecko market data and trending data
- Alternative.me Fear & Greed Index
- Reddit public JSON search
- Helius RPC for Solana mint activity when configured
- Alchemy Solana RPC/WSS as a QuickNode replacement/fallback when configured

All external sources are cached by default to reduce rate-limit pressure.

## Spot-only behavior

This bot is spot-only. It can buy qualified assets and sell back to USDC. It does **not** perform true shorting, leverage, futures, margin, or perps trading. Short/downside scoring is informational and can be used to avoid entries or hold USDC in bearish regimes.

## Safety design

Live trading requires **all** of these gates:

1. `active_trading: true` in `config.json`
2. `COINBASE_TRADING_ENABLED=1` in `.env`
3. running the command with `--live`

The default `config.example.json` ships with live trading disabled. Every run prints a `Live gates:` line showing which gates are open, and a `*** LIVE TRADING ENABLED ***` banner goes to stderr when all three are.

Additional protections include:

- **daily loss limit** — once today's (UTC) realized loss reaches `daily_max_loss_pct` of `sizing_quote_balance_target` (default 2.5% of 100 USDC = 2.50 USDC), new entries stop with `DAILY_LOSS_LIMIT_REACHED` until the next UTC day. Exits keep running.
- **run lock** — the rotator/bot and the exit monitor share `state/run.lock`, so overlapping cron jobs skip (`SKIPPED_RUN_LOCKED`) instead of trading on the same state twice. A lock older than `run_lock_stale_seconds` (default 900) from a crashed run is taken over automatically.
- **actual fill tracking** — after a market order the bot reads Coinbase's fill (average price, size, fees) and uses it for the position entry price and realized P&L, falling back to an estimate only if the fill is not reported yet.
- **stop-losses keep working while orders are pending** — a resting maker entry or take-profit never pauses stop-loss management for the other positions. If a position's own resting limit sell is in the way of a stop-type exit, the bot cancels it and market-sells.
- **optional Coinbase-held backstop** — with `exchange_stop_enabled: true`, each position also gets a stop-limit sell resting on Coinbase, so a crash, sleep, or network outage can't leave it unprotected. See [Coinbase-held backstop stops](#coinbase-held-backstop-stops).
- **config validation** — `config.json` is checked on every run. Typos get a "did you mean" warning; dangerous values (such as `stop_loss_pct: 3.5` instead of `0.035`) block new entries (`CONFIG_INVALID`) while exits keep running. Check a file by hand with `python config_check.py`.
- **alerts** — optional push notifications for real orders, the daily loss limit, config errors, and crashed runs. See [Alerts](#alerts).
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

- Python 3.10 – 3.13 (tested in CI)
- Git
- A Coinbase Advanced Trade / Coinbase Developer Platform (CDP) API key — **optional** for the first run; public market analysis works without one
- API key with trading permission only if you intend to trade live
- **No withdrawal/transfer permission** recommended

## Quick start

Every command below was verified against a fresh clone. The bot runs in public, read-only mode until you add credentials, so you can confirm the install works before touching any keys.

### 1. Install (Linux / macOS / WSL)

```bash
git clone https://github.com/SerpentXSF/serpentx-coinbase-spot-bot.git
cd serpentx-coinbase-spot-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
chmod 600 .env
```

If your distro blocks `pip` with PEP 668 ("externally-managed-environment"), you are not inside the venv — run `source .venv/bin/activate` again. On Debian/Ubuntu, `python3 -m venv` may first need `sudo apt install python3-venv`. You can also use `uv`:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### 1. Install (Windows PowerShell)

```powershell
git clone https://github.com/SerpentXSF/serpentx-coinbase-spot-bot.git
cd serpentx-coinbase-spot-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Copy-Item .env.example .env
```

If PowerShell blocks activation scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, reopen PowerShell, and activate the venv again. Windows users can also follow the Linux steps inside WSL. The `scripts/*.sh` wrappers are bash-only; on Windows call the Python scripts directly.

### 2. Verify the install (no API keys needed)

```bash
python -m unittest -v
python coinbase_spot_bot.py --status
```

The unit tests run fully offline against a fake Coinbase API. The status run fetches live public market data and should end with `Decision: STATUS_ONLY` and `Auth: missing_coinbase_credentials` — that is expected until you add keys. State, logs, and caches are written to `state/`, `logs/`, and `analysis/` inside the repo folder regardless of which directory you run from.

### 3. Add your Coinbase API key

1. Create a **Secret API key** in the [Coinbase Developer Platform portal](https://portal.cdp.coinbase.com/). Choose the **ECDSA** signature algorithm — this bot loads PEM keys, and Coinbase issues Ed25519 keys in a non-PEM format. Grant *View*; add *Trade* only if you plan to go live. Never grant *Transfer*.
2. Edit `.env`:

```text
COINBASE_API_KEY_NAME=organizations/<org-id>/apiKeys/<key-id>
COINBASE_API_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----\nMHc...\n-----END EC PRIVATE KEY-----\n"
COINBASE_TRADING_ENABLED=0
```

The private key must be on **one line**, wrapped in double quotes, with each line break written as `\n`. Keep `COINBASE_TRADING_ENABLED=0` until you intentionally enable live trading.

3. Re-run `python coinbase_spot_bot.py --status`. You should now see a `Balances checked:` line instead of `Auth: missing_coinbase_credentials`.

### 4. Optional: external context providers

```text
HELIUS_API_KEY=
HELIUS_RPC_URL=
ALCHEMY_SOLANA_RPC_URL=
ALCHEMY_SOLANA_WSS_URL=
QUICKNODE_SOLANA_RPC_URL=
COINGECKO_API_KEY=
```

Leave blank if unused. The bot treats optional provider failures as fail-neutral context by default; Alchemy can be used instead of QuickNode for Solana RPC/WSS context.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Config not found: .../config.json` | `cp config.example.json config.json` (PowerShell: `Copy-Item`). |
| `Coinbase private key must be PEM text` | The key in `.env` must start with `-----BEGIN` and use `\n` for line breaks, all on one line in double quotes. |
| `401 Unauthorized` on private calls | Check the key name is the full `organizations/.../apiKeys/...` string, the key is not revoked/IP-restricted, and your system clock is correct (the bot auto-corrects drift over 30 s). |
| `Decision: NEEDS_CREDENTIALS_FOR_PREVIEW_OR_TRADE` | Normal without keys: order previews use a private Coinbase endpoint. |
| `LIVE_BLOCKED_...` decisions | A live gate is still closed — see [Safety design](#safety-design). This is working as intended. |
| `DAILY_LOSS_LIMIT_REACHED` | Today's realized loss hit `daily_max_loss_pct`. Entries resume at 00:00 UTC; open positions are still managed. The running total is in `state/state.json` → `daily_realized_pnl`. |
| `SKIPPED_RUN_LOCKED` | Another bot/exit-monitor run was active. The next scheduled run proceeds normally. If a run crashed, the lock is cleared after `run_lock_stale_seconds`. |
| `ERROR: analyzer timed out` / `bot run timed out` | Coinbase was slow. Raise `rotator_analyzer_timeout_seconds` / `rotator_bot_timeout_seconds` in `config.json`. |
| `CONFIG_INVALID` | `config.json` has an error that blocks new entries (exits keep running). Run `python config_check.py` to see what's wrong. |
| `PENDING_CANCEL_FAILED` / `EXCHANGE_STOP_CANCEL_FAILED` | A stop-type exit needed to cancel a resting order first and Coinbase refused. Nothing was sold; the next run retries. Check the order in the Coinbase app if it repeats. |
| `EXCHANGE_STOP_PLACE_FAILED` | Coinbase rejected the backstop order preview. The error is in the position's `exchange_stop_error` in `state/state.json`, and in your alert if enabled. |
| One product shows `action: ERROR` | That product was delisted/renamed or its candles failed; the rest of the watchlist still runs. `rotate_and_run.py` refreshes the list. |

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

Scans Coinbase USDC pairs, writes the top 5 to `allowed_products` in your `config.json` (other settings are preserved), then runs the bot in preview mode.

The scanner uses the same candle timeframes as the bot (`bar_exec` / `bar_trend` and their lookbacks in `config.json`) and scores every pair with the bot's own technical scoring. Pairs the bot would actually accept (the 30m regime gate passes and the score is at least `score_threshold`) rank first. The scanner's liquidity/momentum score orders pairs within each group. Each row in `analysis/usdc_pairs_latest.json` shows `bot_score`, `bot_regime_ok` and `bot_eligible`.

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
python trade_analytics_dashboard.py
```

It binds to `127.0.0.1:8787` by default (override with `--host` / `--port`). Then open:

```text
http://127.0.0.1:8787
```

The dashboard is designed for local use. It reads files such as `state/state.json`, `logs/runs.jsonl`, `logs/trades.jsonl`, `analysis/analytics_snapshots.jsonl`, and `analysis/order_reconciliation.json` when present. Do not expose the dashboard publicly unless you understand your network, proxy, and authentication setup.

If your live bot state is in another folder, point the dashboard at it without moving secrets:

```bash
COINBASE_BOT_ROOT=/path/to/your/local/bot python trade_analytics_dashboard.py --host 127.0.0.1 --port 8787
```

### Strategy backtest

Replay recent history through the bot's real entry and exit rules, with fees and slippage, before trusting a config with money:

```bash
python strategy_backtest.py --days 14
python strategy_backtest.py --days 30 --products BTC-USDC,ETH-USDC --fee-per-side 0.006
```

It uses the bot's own scoring (5m confirmation, fee guard, regime gates), entry selection, sizing, cooldowns, daily limits, and the same exit engine the live bot uses. It only ever sees candles that had closed at each simulated moment, so there is no lookahead. Every fill pays `--fee-per-side` (default: your config's observed taker fee) plus `--slippage` (default 0.1%). The report covers win rate, average win/loss, expectancy per trade, profit factor, net P&L, fees, max drawdown, exit reasons, and why entries were blocked. It is saved to `analysis/strategy_backtest.json`, and candles are cached under `analysis/backtest_cache/`.

Limitations, printed with every report: prices are checked on 5-minute closes (intrabar wicks are missed), historical news/social context isn't available (`--context neutral` scores it as zero), and it trades the fixed product list rather than the rotator's changing top 5.

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

### Alerts

Set `ALERT_WEBHOOK_URL` in `.env` to get a push message for:

- real orders: sent, partial exit, limit placed, limit filled, failed (with side, product, size, reason, fill price, realized P&L)
- the daily loss limit pausing entries, an invalid `config.json`, and Coinbase backstop fills or failures
- a bot or exit-monitor run crashing

Paste a Discord or Slack incoming-webhook URL, or an [ntfy](https://ntfy.sh) topic URL such as `https://ntfy.sh/your-secret-topic` (free phone push; use a hard-to-guess topic name). The format is detected from the host (`discord.com`, `hooks.slack.com`, `ntfy.sh`); for anything else, including a self-hosted ntfy server, set `ALERT_WEBHOOK_FORMAT` (`discord`, `slack`, `ntfy`, `json`). Preview runs never alert. Conditions that repeat every run are sent at most once per `alert_repeat_minutes` (default 360). A failing webhook never affects trading.

### Coinbase-held backstop stops

The bot's own stops only fire when a run happens and your machine is up. Setting `"exchange_stop_enabled": true` also places a Coinbase **stop-limit sell** for every open position, which Coinbase executes even if the bot is offline:

- The backstop sits **below** the bot's own stop, at `stop_loss_pct` + `exchange_stop_buffer_pct` (default 3.5% + 1% = 4.5% below entry), with its limit `exchange_stop_limit_offset_pct` (1%) lower. Routine exits stay with the bot.
- Before any bot exit, the backstop is cancelled so its coins can be sold. If it already filled, that fill is recorded as the exit instead of selling twice.
- Each run checks every backstop. One that filled while the bot was down closes the position with real P&L. One that was cancelled or expired is re-placed.
- Every order is **previewed with Coinbase first**. In preview mode the preview still runs, so check your run output for `EXCHANGE_STOP_PREVIEW_OK` before relying on it live.

A stop-limit only sells at the limit price or better. In a very fast crash the price can gap through the limit, leaving the order unfilled. The bot's own market-order stop still applies whenever it is running.

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

Before scheduling, create the local runtime folders. They are ignored by git so your state, logs, account-derived analytics, and reconciliation output do not get published accidentally.

```bash
mkdir -p state logs analysis
```

`logs/runs.jsonl` gets a detailed entry on every run and exit-monitor tick, so it rotates automatically at `runs_log_max_bytes` (default 10 MB) and keeps `runs_log_backups` old files (default 5, named `runs.jsonl.1` ... `.5`). `logs/trades.jsonl` is your trade history and is never rotated.

### Linux/macOS cron

Current SerpentX tuning uses a faster entry cadence plus a separate fast exit monitor:

```cron
# Every 90 minutes, rotate the watchlist and run the strategy in preview mode.
0 0-23/3 * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python rotate_and_run.py --json >> logs/cron.log 2>&1
30 1-23/3 * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python rotate_and_run.py --json >> logs/cron.log 2>&1

# Every 3 minutes, check exits only.
*/3 * * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python exit_monitor.py >> logs/exit-monitor.log 2>&1

# Every 3 hours, refresh candidate forward-return stats.
0 */3 * * * cd /path/to/serpentx-coinbase-spot-bot && /path/to/serpentx-coinbase-spot-bot/.venv/bin/python candidate_forward_backtest.py --snapshots 12 --top 3 >> logs/candidate-backtest.log 2>&1
```

For live mode, add `--live` only after enabling the config and env gates. See `scripts/hermes-cron.example.md` for Hermes/no-agent schedule examples without any personal channel IDs.

### systemd timer / Docker / Windows Task Scheduler

Any scheduler can run the same Python commands above. The project does not require any agent runtime or app platform.

## Files

```text
coinbase_spot_bot.py      Main strategy bot (entry point; Coinbase access, scoring, entries)
exits.py                  Exit engine shared by the bot and the exit monitor
exchange_stops.py         Optional Coinbase-held backstop stop orders
indicators.py             EMA / RSI / divergence math (no network)
config_check.py           config.json validation (also runnable by hand)
alerts.py                 Optional webhook alerts
strategy_backtest.py      Replay history through the real entry/exit rules
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
test_*.py                 Unit tests + offline smoke tests (python -m unittest)
.github/workflows/ci.yml  CI: runs the tests on Python 3.10-3.13
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
python -m unittest -v
python coinbase_spot_bot.py --config config.example.json --status
```

The unit tests include offline smoke tests (`test_offline_smoke.py`) that run every documented command against a fake Coinbase API, so they need no network or keys. Changes to exit logic are also checked by comparing old and new behaviour across hundreds of randomized scenarios. GitHub Actions runs them on Python 3.10–3.13 for every push and pull request. The second command uses live public endpoints and optional providers; private account checks require your local `.env`.
