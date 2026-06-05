# Changelog

## v0.2.0

- Added portable exit-monitor workflow for faster bot-managed position checks.
- Added dual-direction scoring context for bearish/downside candidates while keeping the spot executor long-only plus exits to USDC.
- Added market-orderability filtering so disabled, limit-only, cancel-only, trading-disabled, or non-online Coinbase products are excluded from market IOC watchlists.
- Kept public defaults safe: preview mode, live trading disabled, placeholder credentials only, and no local state/logs/secrets committed.

## v0.1.0

- Initial public release of the SerpentX Coinbase USDC spot bot.
