# Coinbase USDC Spot Algo Trading Bot

Educational Coinbase Advanced Trade spot bot for scanning USDC crypto pairs, scoring trade candidates, and managing risk-gated market orders.

> **Disclaimer:** This project is for education and experimentation. It is not financial advice, does not guarantee profits, and can lose money. Run in status/preview mode first. You are responsible for your own API keys, risk settings, taxes, and compliance.

## What it does

- Scans Coinbase spot products quoted in USDC.
- Rotates the strongest candidates into a top-5 watchlist.
- Scores candidates using technical momentum signals.
- Adds optional external context from public/free data sources.
- Manages bot-opened positions with take-profit, stop-loss, soft invalidation, cooldowns, and trade limits.
- Defaults to safe/preview mode; live orders require multiple explicit gates.

## Strategy overview

The bot computes:

```text
final_score = technical_score + context_score
context_score = news_score + market_score + social_score + whale_score + risk_penalty
```

Technical scoring considers:

- 15-minute EMA stack alignment
- price above 1-hour EMA50
- RSI support or pullback setup
- 24-hour green/red movement
- active Coinbase volume

Context scoring can use:

- Google News RSS for positive/negative headline keywords
- CoinGecko market data and trending data
- Alternative.me Fear & Greed Index
- Reddit public JSON search for social sentiment
- Helius RPC for Solana mint activity when you configure Solana mint addresses

All external public sources are cached, defaulting to 1 hour, to reduce rate-limit pressure.

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

Install Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Setup

1. Clone or download this repository.

```bash
git clone https://github.com/YOUR_USERNAME/coinbase-usdc-spot-bot.git
cd coinbase-usdc-spot-bot
```

2. Create your local config and environment files.

```bash
cp config.example.json config.json
cp .env.example .env
chmod 600 .env
```

3. Edit `.env` and add your own Coinbase API credentials.

```text
COINBASE_API_KEY_NAME=organizations/.../apiKeys/...
COINBASE_API_PRIVATE_KEY="[PASTE_YOUR_PEM_PRIVATE_KEY_WITH_ESCAPED_NEWLINES]"
COINBASE_TRADING_ENABLED=0
```

Keep `COINBASE_TRADING_ENABLED=0` until you intentionally enable live trading.

4. Optional: add external provider keys.

```text
HELIUS_API_KEY=your_helius_key_here
# or
HELIUS_RPC_URL=[optional_full_helius_rpc_url]
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

Scans all Coinbase USDC pairs, writes the top 5 to `config.json`, then runs the bot in preview mode.

```bash
python rotate_and_run.py --json
```

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

## Optional cron scheduling

Linux/macOS example, every 6 hours:

```cron
0 */6 * * * cd /path/to/coinbase-usdc-spot-bot && /path/to/coinbase-usdc-spot-bot/.venv/bin/python rotate_and_run.py --json >> logs/cron.log 2>&1
```

Start with preview/status scheduling before enabling live mode.

## Files

```text
coinbase_spot_bot.py      Main strategy bot
analyze_usdc_pairs.py     Public Coinbase USDC market scanner
rotate_and_run.py         Refresh top-5 watchlist and run bot
coinbase_client.py        Minimal Coinbase Advanced Trade helper
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
python -m py_compile coinbase_spot_bot.py analyze_usdc_pairs.py rotate_and_run.py coinbase_client.py
python coinbase_spot_bot.py --config config.example.json --status
```

The second command may use public endpoints and optional providers. Private account checks require a local `.env`.

## License

MIT
