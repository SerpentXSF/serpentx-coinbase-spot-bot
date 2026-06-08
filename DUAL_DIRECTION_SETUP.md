# Dual-Direction Trading Setup

Goal: trade both bull and bear regimes while keeping the existing Coinbase spot bot safe.

## Current executor

The live executor currently uses Coinbase Advanced Trade **spot** markets. Spot can:

- Buy assets expected to go up.
- Sell bot-managed holdings to realize profit, stop loss, or move back to USDC.
- Hold USDC during bearish/no-edge regimes.

Spot cannot create a true short position. Selling a spot asset you already hold reduces exposure; it does not profit from further downside after the asset is sold.

## What was added

`analyze_usdc_pairs.py` now computes both:

- `long_score` / `score`: upside candidate score.
- `short_score`: downside candidate score for future futures/perps/margin routing.
- `directional_bias`: `LONG`, `SHORT`, or `NEUTRAL`.
- `top5_short_candidates`: saved in `analysis/usdc_pairs_latest.json`.

`config.example.json` documents `directional_strategy` documenting the enabled dual-direction scoring model and the required gates for true shorting.

`rotate_and_run.py` now includes `top5_short_candidates` in cron summaries when it emits output.

## Indicators

Long/upside indicators:

- 15m EMA9 > EMA21 > EMA50.
- Price above 1H EMA50.
- 1H EMA20 > EMA50.
- RSI in supportive/trend-pullback range.
- Positive 24h momentum.
- Quote volume/liquidity.
- News, market, social, and whale/on-chain context modifiers.

Downside/bear indicators:

- 15m EMA9 < EMA21 < EMA50.
- Price below 1H EMA50.
- 1H EMA20 < EMA50.
- RSI 32-58 while trending down.
- Negative 24h momentum.
- Quote volume/liquidity.
- Oversold/snapback caution when RSI is too low.

## Live trading behavior today

The current live cron can still place only spot orders:

- Long entries from the top scored USDC spot candidates.
- Exits for managed positions on take-profit, stop-loss, or soft-invalidation.
- USDC hold in bearish/no-entry conditions.

True short trades are **not live-enabled** yet because they require a verified Coinbase futures/perps or margin route plus separate risk gates.

## Required before true short execution

Before routing downside signals into live shorts:

1. Verify available Coinbase derivatives/perps/margin endpoints for this account and region.
2. Add a separate derivatives connector; do not reuse spot order code for leverage.
3. Add separate env gate, e.g. `COINBASE_DERIVATIVES_TRADING_ENABLED=1`.
4. Add risk limits for leverage, liquidation buffer, max short notional, funding, daily loss, and forced close.
5. Run read-only status and order previews first.
6. Only then enable a dedicated live short cron/job.

## Important safety note

The bot can identify downtrends now. It will not pretend a Coinbase spot SELL is a true short. Until a derivatives connector is explicitly verified and enabled, downside signals are used for exits/USDC risk-off and future candidate tracking only.
