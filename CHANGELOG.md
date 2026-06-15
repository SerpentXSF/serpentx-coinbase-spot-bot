# Changelog

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
