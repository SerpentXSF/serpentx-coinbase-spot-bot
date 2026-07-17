# Hermes cron examples for the Coinbase Spot bot

These examples document the current SerpentX Spot cadence without embedding any
local Discord channel IDs, account IDs, API keys, or host-specific paths. Create
jobs with `deliver="local"` first, then change delivery to your own channel.

Recommended current cadence:

| Job | Script | Schedule | Notes |
| --- | --- | --- | --- |
| Coinbase USDC top-5 LIVE rotator | `coinbase_usdc_rotator.py` or `scripts/run_rotator.sh --live` | `every 90m` | Entry/rotation scan. Requires live gates for real orders. |
| Coinbase LIVE exit monitor | `coinbase_exit_monitor.py` or `scripts/run_exit_monitor.sh --live` | `every 3m` | Lightweight safety loop for bot-managed positions. |
| Coinbase analytics snapshot | project-specific wrapper for `trade_analytics_dashboard.py --snapshot` | `every 30m` | Silent dashboard metric snapshots. |
| Coinbase candidate forward backtest | `candidate_forward_backtest.py --snapshots 12 --top 3` | `every 180m` | Candidate hindsight/forward-return learning. |
| traderJoe Coinbase recommendation agent | optional local recommendation wrapper | `every 180m` | Observe/recommend-only; does not trade. |
| traderJoe free crypto signal collector | optional local signal wrapper | `every 30m` | Fail-neutral context collection. |

Safety notes:

- Keep `.env` local and uncommitted.
- `config.example.json` defaults to `active_trading=false` and `mode=preview`.
- Live trading still requires config, environment, and runtime live gates.
- Use `COINBASE_BOT_ROOT=/path/to/runtime` if running wrappers from outside the repo.
