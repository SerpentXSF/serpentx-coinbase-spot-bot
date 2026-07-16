# Changelog

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
