# Changelog

## Unreleased

- The USDC scanner now ranks pairs by the same rules the bot trades by. It had been using 1-hour trend candles (labelled "30m") and 120h/30h lookbacks, while the bot uses `bar_trend` (30m) with 48h/24h lookbacks, and the scanner ignored the bot's 30m regime gate, so the rotator could fill the watchlist with pairs the bot would refuse. The scanner now reads the timeframes from `config.json`, scores each pair with the bot's own `technical_long_score()`, and ranks bot-eligible pairs first. Rows include `bot_score`, `bot_regime_ok`, `bot_eligible`, and `analysis/usdc_pairs_latest.json` records the `timeframes` used.
- The bot's candle scoring moved into `technical_long_score()`, a function with no network calls, with no behaviour change (verified identical on 1,300 randomized markets). The scanner's duplicate EMA/RSI code now uses the bot's.
- `logs/runs.jsonl` rotates at `runs_log_max_bytes` (default 10 MB) and keeps `runs_log_backups` files (default 5). `trades.jsonl` is never rotated.
- The dashboard reads only the end of the runs log for the latest run. On a 153 MB log: 6.4 s down to under 1 ms.
- **Fixed:** while any limit order was pending (a maker entry or a resting take-profit), the bot and exit monitor skipped exit management for every position for up to 90 minutes. Stops are now always managed; a stop-type exit cancels the position's own resting limit sell and market-sells.
- **New:** optional Coinbase-held backstop stop-limit orders (`exchange_stop_enabled`, default off). Includes handling for Coinbase balance holds, cancel-before-exit, fills while offline, re-placement, and a preview check before every placement.
- **New:** `strategy_backtest.py` replays history through the real entry rules and the shared exit engine, with fees and slippage and no lookahead.
- **New:** `config.json` validation (`config_check.py`). Errors block new entries (`CONFIG_INVALID`) but never exits. A string `"active_trading": "false"` no longer counts as an open live gate. `rsi_divergence_enabled` is now honoured.
- **New:** optional webhook alerts (`ALERT_WEBHOOK_URL`: Discord, Slack, ntfy, or JSON).
- **New:** `rsi_method: "wilder"` option (default `"simple"` keeps current behaviour).
- **Refactor:** `coinbase_spot_bot.py` split into `indicators.py` and `exits.py`, one exit engine shared by the bot and the exit monitor. Verified identical to the previous behaviour on 800 randomized exit scenarios.
- **Perf:** one keep-alive HTTP session for Coinbase calls, a 5 s product-price cache, and clock-skew checks taken from existing responses (19 requests on 19 connections down to 17 on one, per preview run).
- Fixed `rotate_and_run.py` / `analyze_usdc_pairs.py` and `candidate_forward_backtest.py` crashing on startup with `NameError: name 'os' is not defined`.
- Fixed the README quick start failing with `Coinbase private key must be PEM text` when `.env` was copied from the template but not yet filled in; placeholder credentials now count as "no credentials" and the template ships blank.
- Fixed the dashboard reading `state.json` / `trades.jsonl` from the repo root instead of the `state/` and `logs/` paths the bot writes, so positions, cooldowns, and trades now appear.
- Dashboard now binds to `127.0.0.1:8787` by default (was `0.0.0.0:2048`), matching the docs and keeping balances off the LAN.
- Relative `*_path` config entries now resolve against the bot folder, so running from another directory (systemd, Task Scheduler) no longer splits state across folders.
- One delisted or failing product no longer aborts the whole run; it is marked `ERROR` and skipped.
- `candidate_forward_backtest.py` creates `analysis/` if missing; clear error when `config.json` is missing.
- Added offline smoke tests for every documented command and a GitHub Actions CI workflow (Python 3.10-3.13).
- Rewrote README install/setup into a single verified quick start with an API key walkthrough and troubleshooting table.
- Fixed exits silently deleting other open positions from state: when the first position fired an exit that was not a full market close (preview mode, failed order, limit order, partial exit), every position after it was dropped and lost stop-loss management. Affects both the bot and the exit monitor.
- Market orders now record Coinbase's actual fill (average price, size, fees) for entry price and realized P&L instead of the signal price.
- `daily_max_loss_pct` is now enforced: new entries stop for the rest of the UTC day once realized losses reach the limit; exits keep running.
- Added a shared run lock so the rotator/bot and exit monitor never trade on the same state concurrently.
- Rotator timeouts are configurable (`rotator_analyzer_timeout_seconds`, `rotator_bot_timeout_seconds`) and fail with a clear message.
- Runs print which live gates are open, with a stderr banner when all three are.
- `coinbase_client.py` now reuses the bot's auth/request code instead of a divergent copy.
- Pinned dependency major versions; default watchlist switched to liquid pairs (BTC, ETH, SOL, LINK, DOGE).

## v0.5.0-beta

- Synced the public Spot bot with the latest live-safe strategy code, including Coinbase JWT timestamp correction for WSL/host clock drift.
- Updated public-safe tuning defaults to the current SerpentX parameters: `score_threshold=4`, `second_position_min_score=5`, `second_position_min_final_score=6`, `min_final_score_when_overheated=7`, `daily_max_trades=8`, and `max_open_positions=2`.
- Documented current scheduler cadence: live rotator every 90 minutes, exit monitor every 3 minutes, and candidate/recommendation learning jobs every 180 minutes.
- Added sanitized Hermes cron examples without Discord channel IDs, account IDs, secrets, local runtime paths, balances, state, or trade history.
- Added Alchemy Solana RPC/WSS placeholders and docs as a QuickNode replacement/fallback for optional context providers.

## v0.4.0-beta

- Added RSI divergence labels and scoring adjustments for bullish/bearish divergence.
- Added 5-minute entry confirmation and fee-aware entry guards to reduce weak/immediate entries.
- Added dynamic sizing knobs, target-bankroll sizing, overheated-entry controls, and optional trailing stop support.
- Added defensive stale duplicate-position cleanup in bot-managed state.
- Added low-call `reconcile_orders.py` to verify recent locally logged Coinbase orders against Coinbase historical order status.
- Updated dashboard documentation for the Red/Purple responsive analytics UI, snapshots, order reconciliation, and candidate forward-return data.
- Expanded README installation instructions for Linux/macOS/WSL and Windows PowerShell.

## v0.3.0-beta

- Added a local, read-only performance dashboard for reviewing bot state, run logs, and trade history.
- Added a candidate forward-backtest helper for checking scanner follow-through against later candles.
- Carried forward latest spot-safe strategy improvements while preserving the original published bot core and live-trading gates.
- Kept release posture beta/experimental for dashboard and strategy-analysis additions.

## v0.2.0

- Added portable exit-monitor workflow for faster bot-managed position checks.
- Added dual-direction scoring context for bearish/downside candidates while keeping the spot executor long-only plus exits to USDC.
- Added market-orderability filtering so disabled, limit-only, cancel-only, trading-disabled, or non-online Coinbase products are excluded from market IOC watchlists.
- Kept public defaults safe: preview mode, live trading disabled, placeholder credentials only, and no local state/logs/secrets committed.

## v0.1.0

- Initial public release of the SerpentX Coinbase USDC spot bot.
