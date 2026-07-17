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
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
sys.path.insert(0, str(ROOT))

import coinbase_spot_bot as bot  # noqa: E402

CONFIG = ROOT / "config.json"


def _round_money(x: float) -> float:
    return round(float(x), 8)


def _event(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, sort_keys=True)


def _set_product_cooldown(state: dict[str, Any], product_id: str, minutes: float) -> None:
    if not product_id or minutes <= 0:
        return
    cooldowns = state.setdefault("product_cooldowns", {})
    if isinstance(cooldowns, dict):
        cooldowns[product_id] = (bot.utcnow() + timedelta(minutes=float(minutes))).isoformat()


def _trailing_exit_reason(cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float) -> str | None:
    return bot.trailing_exit_reason(cfg, pos, current, entry)


def _structural_exit_reason(cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float) -> str | None:
    return bot.structural_exit_reason(cfg, pos, current, entry)


def run(*, live: bool = False, json_status: bool = False) -> dict[str, Any]:
    cfg = bot.load_json(CONFIG)
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
    if pending_orders:
        result["decision"] = "PENDING_ORDER_OPEN"
        bot.save_json(state_path, state)
        bot.append_jsonl(Path(cfg["runs_log_path"]), result)
        return result

    updated_positions: list[dict[str, Any]] = []
    context_cache: dict[str, Any] | None = None

    for pos in positions:
        product_id = str(pos.get("product_id"))
        base, _, _quote = product_id.partition("-")
        entry = bot.fnum(pos.get("entry_price"))
        current = bot.fnum(bot.product_info(product_id).get("price")) if product_id else 0.0
        pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0
        base_size = min(bot.fnum(pos.get("base_size_est")), balances.get(base, 0.0))

        enriched = {
            **pos,
            "current_price": _round_money(current),
            "pnl_pct": pnl_pct,
            "take_profit_price": _round_money(entry * (1 + float(cfg["take_profit_pct"]))),
            "stop_loss_price": _round_money(entry * (1 - float(cfg["stop_loss_pct"]))),
            "soft_invalidation_price": _round_money(entry * (1 - float(cfg["soft_invalidation_pct"]))),
            "available_base_balance": balances.get(base, 0.0),
            "exit_base_size": base_size,
        }

        sell_reason = None
        exit_base_size = base_size
        scale_reason, scale_size = bot.scale_exit_plan(cfg, pos, current, entry, base_size)
        if scale_reason:
            sell_reason = scale_reason
            exit_base_size = scale_size
        elif pnl_pct >= float(cfg["take_profit_pct"]):
            sell_reason = "TAKE_PROFIT"
        elif pnl_pct <= -float(cfg["stop_loss_pct"]):
            sell_reason = "STOP_LOSS"
        else:
            structural_reason = _structural_exit_reason(cfg, pos, current, entry)
            if structural_reason:
                sell_reason = structural_reason
                try:
                    if context_cache is None:
                        context_cache = bot._load_context_cache(cfg)  # noqa: SLF001
                    pos_signal = bot.apply_context_scores(cfg, bot.score_product(cfg, product_id), context_cache)
                    bot._save_context_cache(cfg, context_cache)  # noqa: SLF001
                    result["position_signal"] = pos_signal.__dict__
                except Exception as exc:  # do not convert a soft check failure into a cron error
                    result["position_signal_error"] = str(exc)[:300]

        trailing_reason = _trailing_exit_reason(cfg, pos, current, entry)
        if trailing_reason and not sell_reason:
            sell_reason = trailing_reason
            enriched = {
                **pos,
                "current_price": _round_money(current),
                "pnl_pct": pnl_pct,
                "take_profit_price": _round_money(entry * (1 + float(cfg["take_profit_pct"]))),
                "stop_loss_price": _round_money(entry * (1 - float(cfg["stop_loss_pct"]))),
                "soft_invalidation_price": _round_money(entry * (1 - float(cfg["soft_invalidation_pct"]))),
                "available_base_balance": balances.get(base, 0.0),
                "exit_base_size": exit_base_size,
            }
        enriched["exit_base_size"] = exit_base_size

        if sell_reason and exit_base_size > 0:
            result["decision"] = "EXIT_SIGNAL"
            result["open_position"] = enriched
            use_limit = bot.should_use_maker_limit(cfg, "SELL", sell_reason)
            limit_price = bot.maker_limit_price(cfg, product_id, "SELL", current) if use_limit else None
            result["proposed_order"] = {
                "product_id": product_id,
                "side": "SELL",
                "base_size": exit_base_size,
                "reason": sell_reason,
                "order_type": "LIMIT_POST_ONLY" if use_limit else "MARKET_IOC",
                "limit_price": limit_price,
            }
            result["preview"] = bot.preview_limit(product_id, "SELL", limit_price, base_size=exit_base_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else bot.preview_market(product_id, "SELL", base_size=exit_base_size)
            gate = bot.live_gates_open(cfg, live)
            if gate:
                result["decision"] = gate
                updated_positions.append(pos)
            else:
                order = bot.place_limit(product_id, "SELL", limit_price, base_size=exit_base_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else bot.place_market(product_id, "SELL", base_size=exit_base_size)
                result["order"] = order
                if order.get("success") is True:
                    if use_limit:
                        oid = bot.order_id_from_response(order)
                        result["decision"] = "LIMIT_ORDER_PLACED"
                        pending_position = dict(pos)
                        if sell_reason == str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT")):
                            pending_position["scale_exit_pending_order_id"] = oid
                        state.setdefault("pending_orders", []).append({"order_id": oid, "product_id": product_id, "side": "SELL", "reason": sell_reason, "base_size": exit_base_size, "limit_price": limit_price, "placed_at": result["ts"], "position": pending_position, "order_type": "LIMIT_POST_ONLY", "source": "exit_monitor"})
                        updated_positions.append(pending_position if sell_reason == str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT")) else pos)
                    else:
                        scale_exit_reason = str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT"))
                        if sell_reason == scale_exit_reason:
                            result["decision"] = "PARTIAL_EXIT_SENT"
                            updated = bot.apply_partial_exit_fill(pos, exit_base_size, result["ts"])
                            if updated:
                                updated_positions = [updated if p is pos else p for p in positions]
                        else:
                            result["decision"] = "ORDER_SENT"
                            state["cooldown_until"] = (bot.utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                            if sell_reason in {"STOP_LOSS", "SOFT_INVALIDATION", "TRAILING_STOP", "BREAKEVEN_LOCK"}:
                                _set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_stop", 720)))
                            else:
                                _set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_trade", 180)))
                            updated_positions = [p for p in positions if p is not pos]
                        bot.append_jsonl(Path(cfg["trades_log_path"]), {
                            "ts": result["ts"],
                            "side": "SELL",
                            "reason": sell_reason,
                            "order": order,
                            "position": pos,
                            "base_size": exit_base_size,
                            "source": "exit_monitor",
                            "order_type": "MARKET_IOC",
                        })
                else:
                    result["decision"] = "ORDER_FAILED"
                    bot.append_jsonl(Path(cfg["trades_log_path"]), {
                        "ts": result["ts"],
                        "side": "SELL",
                        "reason": sell_reason,
                        "order": order,
                        "position": pos,
                        "source": "exit_monitor",
                        "failed": True,
                    })
                    updated_positions.append(pos)
            bot.persist_positions(state, updated_positions)
            break

        updated_positions.append(pos)
        result.setdefault("checked_positions", []).append(enriched)
    else:
        bot.persist_positions(state, updated_positions)

    result["pending_orders"] = bot.normalize_pending_orders(state)
    state["last_exit_monitor_at"] = result["ts"]
    state["last_exit_monitor_decision"] = result["decision"]
    if result["decision"] != "HOLD_POSITIONS" or json_status:
        state["last_exit_monitor_result"] = result
    bot.save_json(state_path, state)
    bot.append_jsonl(Path(cfg["runs_log_path"]), result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Place exit order if all live gates are open")
    ap.add_argument("--json", action="store_true", help="Print status even when no exit fires")
    args = ap.parse_args()

    result = run(live=args.live, json_status=args.json)
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
