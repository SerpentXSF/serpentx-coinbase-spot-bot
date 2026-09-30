#!/usr/bin/env python3
"""Lightweight exit-only monitor for bot-managed Coinbase spot positions.

This script intentionally avoids the full all-USDC rotation scan. It only:
- loads existing bot-managed positions from state.json
- checks live Coinbase balances and current product prices
- exits one position per run if take-profit, hard stop-loss, or soft-invalidation fires

Live orders still require the same gates as coinbase_spot_bot.py:
config active_trading=true, COINBASE_TRADING_ENABLED=1, and --live.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
sys.path.insert(0, str(ROOT))

import coinbase_spot_bot as bot  # noqa: E402
import exits  # noqa: E402

CONFIG = ROOT / "config.json"


def _event(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, sort_keys=True)


def run(*, live: bool = False, json_status: bool = False) -> dict[str, Any]:
    cfg = bot.load_config(CONFIG)
    bot.load_dotenv(cfg.get("env_file", ROOT / ".env"))

    state_path = Path(cfg["state_path"])
    state = bot.load_json(state_path) if state_path.exists() else {}
    positions = bot.normalize_positions(state)

    result: dict[str, Any] = {
        "ts": bot.utcnow().isoformat(),
        "strategy": cfg.get("strategy_name", "Coinbase spot bot"),
        "monitor": "exit_only",
        "mode": cfg.get("mode", "shadow"),
        "decision": "NO_POSITIONS" if not positions else "HOLD_POSITIONS",
        "env_present": bot.env_present(),
        "open_positions_count": len(positions),
    }
    # Report config problems but keep managing exits: refusing to run here
    # would leave open positions without their stop-loss.
    config_report = bot.config_check.validate(cfg)
    if config_report["errors"] or config_report["warnings"]:
        result["config_check"] = config_report

    if not positions:
        return result

    if not (result["env_present"].get("COINBASE_API_KEY_NAME") and result["env_present"].get("COINBASE_API_PRIVATE_KEY")):
        result["decision"] = "NEEDS_CREDENTIALS_FOR_EXIT_MONITOR"
        return result

    accounts = bot.get_accounts()
    balances = bot.balance_map(accounts)
    result["balances"] = {
        k: v for k, v in balances.items()
        if v > 0 or k in {cfg.get("quote_currency", "USDC"), *(str(p.get("product_id", "")).split("-")[0] for p in positions)}
    }
    pending_events = bot.reconcile_pending_orders(cfg, state, balances)
    if pending_events:
        result["pending_order_events"] = pending_events
    pending_orders = bot.normalize_pending_orders(state)
    result["pending_orders"] = pending_orders
    positions = bot.normalize_positions(state)
    # Keep managing exits while limit orders are pending (see exits.manage_exits).

    # The same exit engine the full bot uses; this monitor just runs it more often.
    bot.exchange_stops.sync(bot, cfg, state, result, live=live)
    positions = bot.normalize_positions(state)
    if any(ev.get("event") == "EXCHANGE_STOP_FILLED" for ev in result.get("exchange_stop_events", [])):
        result["decision"] = "EXCHANGE_STOP_FILLED"  # a manage_exits() exit below overrides this
    fired = exits.manage_exits(bot, cfg, state, positions, balances, result, live=live, source="exit_monitor")
    if not fired and result["decision"] == "HOLD_POSITIONS" and bot.normalize_pending_orders(state):
        result["decision"] = "PENDING_ORDER_OPEN"

    result["pending_orders"] = bot.normalize_pending_orders(state)
    state["last_exit_monitor_at"] = result["ts"]
    state["last_exit_monitor_decision"] = result["decision"]
    if result["decision"] != "HOLD_POSITIONS" or json_status:
        state["last_exit_monitor_result"] = result
    bot.save_json(state_path, state)
    bot.append_run_log(cfg, result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Place exit order if all live gates are open")
    ap.add_argument("--json", action="store_true", help="Print status even when no exit fires")
    args = ap.parse_args()

    cfg = bot.load_config(CONFIG)
    bot.load_dotenv(cfg.get("env_file", ROOT / ".env"))  # early, so a crash can still alert
    try:
        with bot.run_lock_for(cfg):
            try:
                result = run(live=args.live, json_status=args.json)
            except Exception as exc:
                bot.alerts.notify_error(cfg, exc, source="exit_monitor")
                raise
    except bot.RunLocked as exc:
        # Another run is managing positions right now; the next tick will retry.
        if args.json:
            print(_event({"ts": bot.utcnow().isoformat(), "monitor": "exit_only", "decision": "SKIPPED_RUN_LOCKED", "reason": str(exc)}))
        return 0
    bot.alerts.notify_result(cfg, result, source="exit_monitor")
    # Quiet cron behavior: no output for ordinary holds/no positions. no_agent cron
    # sends nothing on empty stdout, but sends important exit/block/error events.
    if args.json or result.get("decision") not in {"HOLD_POSITIONS", "NO_POSITIONS"}:
        print(_event(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: exit monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
