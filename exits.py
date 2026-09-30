#!/usr/bin/env python3
"""Exit management shared by coinbase_spot_bot.run() and exit_monitor.run().

Both entry points used to carry their own copy of this loop; the copies drifted
and a state-loss bug had to be fixed twice. There is now exactly one.

Coinbase access (prices, previews, orders, fills) and state helpers are reached
through ``api`` -- the coinbase_spot_bot module object the caller already
has -- rather than a top-level import. When the bot runs as ``__main__`` a
plain ``import coinbase_spot_bot`` here would load a second copy of the bot;
passing the module keeps one set of globals and lets tests patch it in one
place.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import exchange_stops

STOP_LIKE_REASONS = {"STOP_LOSS", "SOFT_INVALIDATION", "TRAILING_STOP", "BREAKEVEN_LOCK"}


def exit_reason(api: ModuleType, cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float, base_size: float) -> tuple[str | None, float, bool]:
    """Decide whether ``pos`` should exit now.

    Returns (reason, base size to sell, structural) -- ``structural`` is True
    when the reason came from structural_exit_reason, so the caller can attach
    a fresh position signal for context.
    """
    pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0
    sell_reason = None
    exit_base_size = base_size
    structural = False
    scale_reason, scale_size = api.scale_exit_plan(cfg, pos, current, entry, base_size)
    if scale_reason:
        sell_reason = scale_reason
        exit_base_size = scale_size
    elif pnl_pct >= float(cfg["take_profit_pct"]):
        sell_reason = "TAKE_PROFIT"
    elif pnl_pct <= -float(cfg["stop_loss_pct"]):
        sell_reason = "STOP_LOSS"
    else:
        structural_reason = api.structural_exit_reason(cfg, pos, current, entry)
        if structural_reason:
            sell_reason = structural_reason
            structural = True
    trailing_reason = api.trailing_exit_reason(cfg, pos, current, entry)
    if trailing_reason and not sell_reason:
        sell_reason = trailing_reason
    return sell_reason, exit_base_size, structural


def pending_sell_for(api: ModuleType, state: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    for row in api.normalize_pending_orders(state):
        if row.get("product_id") == product_id and str(row.get("side", "")).upper() == "SELL":
            return row
    return None


def cancel_pending_sell(api: ModuleType, cfg: dict[str, Any], state: dict[str, Any], pos: dict[str, Any],
                        row: dict[str, Any], result: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Cancel a resting limit SELL so a stop-type exit can market-sell instead.

    Returns ("cancelled", position) with any partial limit fill already booked
    and the position reduced, or ("failed", reason). A limit that filled
    completely is left for reconcile_pending_orders() on the next run.
    """
    fnum = api.fnum
    oid = str(row["order_id"])
    try:
        ok = api.cancel_succeeded(api.cancel_order(oid), oid)
    except Exception as exc:
        ok, err = False, str(exc)[:200]
    else:
        err = "rejected"
    try:
        detail = api.fetch_order_detail(oid)
    except Exception:
        detail = {}
    status = str(detail.get("status") or "").upper()
    if not ok and status not in exchange_stops.DONE_STATUSES:
        return "failed", f"could not cancel resting limit sell {oid}: {err}"
    if status == "FILLED":
        return "failed", f"resting limit sell {oid} already filled; booked on the next reconcile"
    state["pending_orders"] = [r for r in api.normalize_pending_orders(state) if str(r.get("order_id")) != oid]
    filled = fnum(detail.get("filled_size"))
    updated: dict[str, Any] | None = dict(pos)
    updated.pop("scale_exit_pending_order_id", None)
    if filled > 0:
        price = fnum(detail.get("average_filled_price")) or fnum(row.get("limit_price"))
        fill = {"order_id": oid, "filled_size": filled, "average_filled_price": price, "total_fees": fnum(detail.get("total_fees"))}
        realized = api.realized_pnl_quote(cfg, pos, filled, fill, price)
        api.record_realized_pnl(state, realized, result["ts"])
        api.append_jsonl(Path(cfg["trades_log_path"]), {
            "ts": result["ts"], "side": "SELL", "reason": row.get("reason") or "LIMIT_EXIT_PARTIAL",
            "order": {"success": True, "success_response": {"order_id": oid, "product_id": pos.get("product_id"), "side": "SELL"}},
            "order_detail": detail, "position": pos, "base_size": filled, "realized_pnl_quote": realized, "source": "limit_cancelled_for_risk_exit",
        })
        updated = api.apply_partial_exit_fill(updated, filled, result["ts"])
    result.setdefault("cancelled_pending_orders", []).append({"order_id": oid, "filled_before_cancel": filled})
    return "cancelled", updated


def manage_exits(
    api: ModuleType,
    cfg: dict[str, Any],
    state: dict[str, Any],
    positions: list[dict[str, Any]],
    balances: dict[str, float],
    result: dict[str, Any],
    *,
    live: bool,
    context_cache: dict[str, Any] | None = None,
    source: str | None = None,
) -> bool:
    """Check every bot-managed position and act on the first one that should exit.

    At most one exit order is sent per call. Updates ``result`` (decision,
    proposed_order, preview, order, fill, realized P&L, checked_positions) and
    ``state`` (positions, pending orders, cooldowns, daily P&L ledger), and
    appends SELL rows to the trades log. Returns True when an exit fired
    (order sent, placed, failed, or blocked by a live gate), False when every
    position is held -- in which case positions are persisted unchanged.
    """
    fnum = api.fnum
    ts = result["ts"]
    trades_log = Path(cfg["trades_log_path"])
    post_only = bool(cfg.get("maker_limit_post_only", True))
    scale_exit_reason = str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT"))
    owns_cache = context_cache is None  # the exit monitor has no cache loaded; the bot passes its own
    loaded_cache = context_cache
    held: list[dict[str, Any]] = []

    for pos in positions:
        product_id = str(pos.get("product_id"))
        base, _, _ = product_id.partition("-")
        entry = fnum(pos.get("entry_price"))
        current = fnum(api.product_info(product_id).get("price")) if product_id else 0.0
        # Coins held by this position's own exchange stop or resting limit sell are still ours to sell.
        pending_sell = pending_sell_for(api, state, product_id)
        held_by_limit = fnum(pending_sell.get("base_size")) if pending_sell else 0.0
        base_size = min(fnum(pos.get("base_size_est")), balances.get(base, 0.0) + exchange_stops.held_size(pos) + held_by_limit)
        pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0

        sell_reason, exit_base_size, structural = exit_reason(api, cfg, pos, current, entry, base_size)
        if structural:
            try:
                if loaded_cache is None:
                    loaded_cache = api._load_context_cache(cfg)  # noqa: SLF001
                pos_signal = api.apply_context_scores(cfg, api.score_product(cfg, product_id), loaded_cache)
                if owns_cache:
                    api._save_context_cache(cfg, loaded_cache)  # noqa: SLF001
                result["position_signal"] = pos_signal.__dict__
            except Exception as exc:  # a context lookup must never block an exit
                result["position_signal_error"] = str(exc)[:300]

        enriched = {
            **pos,
            "current_price": round(current, 8),
            "pnl_pct": pnl_pct,
            "take_profit_price": round(entry * (1 + float(cfg["take_profit_pct"])), 8),
            "stop_loss_price": round(entry * (1 - float(cfg["stop_loss_pct"])), 8),
            "soft_invalidation_price": round(entry * (1 - float(cfg["soft_invalidation_pct"])), 8),
            "available_base_balance": balances.get(base, 0.0),
            "exit_base_size": exit_base_size,
        }

        urgent_over_limit = bool(pending_sell and sell_reason in STOP_LIKE_REASONS)
        if pending_sell and not urgent_over_limit:
            # Its own resting limit sell is already exiting it; only a stop-type exit overrides that.
            sell_reason = None
            enriched["pending_sell_order_id"] = pending_sell.get("order_id")
        if not (sell_reason and exit_base_size > 0):
            held.append(pos)
            result.setdefault("checked_positions", []).append(enriched)
            continue

        result["open_position"] = enriched
        use_limit = False if urgent_over_limit else api.should_use_maker_limit(cfg, "SELL", sell_reason)
        limit_price = api.maker_limit_price(cfg, product_id, "SELL", current) if use_limit else None
        result["proposed_order"] = {
            "product_id": product_id,
            "side": "SELL",
            "base_size": exit_base_size,
            "reason": sell_reason,
            "order_type": "LIMIT_POST_ONLY" if use_limit else "MARKET_IOC",
            "limit_price": limit_price,
        }
        result["preview"] = (
            api.preview_limit(product_id, "SELL", limit_price, base_size=exit_base_size, post_only=post_only)
            if use_limit else api.preview_market(product_id, "SELL", base_size=exit_base_size)
        )
        replacement: dict[str, Any] | None = pos
        gate = api.live_gates_open(cfg, live)
        if not gate and urgent_over_limit:
            outcome, info = cancel_pending_sell(api, cfg, state, pos, pending_sell, result)
            if outcome == "failed":
                result["decision"] = "PENDING_CANCEL_FAILED"
                result["pending_cancel_error"] = info
                api.persist_positions(state, positions)
                return True
            if info is None:
                # The limit filled almost everything before we cancelled it.
                result["decision"] = "LIMIT_EXIT_FILLED_BEFORE_CANCEL"
                api.persist_positions(state, api.replace_position(positions, pos, None))
                return True
            positions = api.replace_position(positions, pos, info)
            pos = info
            exit_base_size = min(exit_base_size, fnum(pos.get("base_size_est")))
            result["proposed_order"]["base_size"] = exit_base_size
            replacement = pos
        if not gate and exchange_stops.held_size(pos) > 0:
            # Free the coins held by the exchange backstop before selling them.
            outcome, info = exchange_stops.cancel_for_exit(api, cfg, state, pos, result)
            if outcome == "filled":
                result["decision"] = "EXCHANGE_STOP_FILLED"
                api.persist_positions(state, api.replace_position(positions, pos, None))
                return True
            if outcome == "failed":
                result["decision"] = "EXCHANGE_STOP_CANCEL_FAILED"
                result["exchange_stop_error"] = info
                api.persist_positions(state, positions)
                return True
            if info is not pos:
                # Part of the stop filled before the cancel landed: sell only what is left.
                positions = api.replace_position(positions, pos, info)
                pos = info
                exit_base_size = min(exit_base_size, fnum(pos.get("base_size_est")))
                result["proposed_order"]["base_size"] = exit_base_size
                replacement = pos
        if gate:
            result["decision"] = gate
        else:
            order = (
                api.place_limit(product_id, "SELL", limit_price, base_size=exit_base_size, post_only=post_only)
                if use_limit else api.place_market(product_id, "SELL", base_size=exit_base_size)
            )
            result["order"] = order
            tagged = {"source": source} if source else {}
            if order.get("success") is True:
                oid = api.order_id_from_response(order)
                if use_limit:
                    result["decision"] = "LIMIT_ORDER_PLACED"
                    pending_position = dict(pos)
                    if sell_reason == scale_exit_reason:
                        pending_position["scale_exit_pending_order_id"] = oid
                        replacement = pending_position
                    state.setdefault("pending_orders", []).append({
                        "order_id": oid, "product_id": product_id, "side": "SELL", "reason": sell_reason,
                        "base_size": exit_base_size, "limit_price": limit_price, "placed_at": ts,
                        "position": pending_position, "order_type": "LIMIT_POST_ONLY", **tagged,
                    })
                else:
                    fill = api.market_fill_details(oid)
                    filled_size = fnum(fill.get("filled_size")) or exit_base_size
                    realized = api.realized_pnl_quote(cfg, pos, filled_size, fill, current)
                    api.record_realized_pnl(state, realized, ts)
                    result["fill"] = fill
                    result["realized_pnl_quote"] = realized
                    if sell_reason == scale_exit_reason:
                        result["decision"] = "PARTIAL_EXIT_SENT"
                        replacement = api.apply_partial_exit_fill(pos, filled_size, ts)
                        if replacement:
                            replacement.pop("exchange_stop", None)
                            exchange_stops.place(api, cfg, replacement, result, live=live)
                    else:
                        result["decision"] = "ORDER_SENT"
                        state["cooldown_until"] = (api.utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                        if sell_reason in STOP_LIKE_REASONS:
                            api.set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_stop", 720)))
                        else:
                            api.set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_trade", 180)))
                        replacement = None
                    api.append_jsonl(trades_log, {
                        "ts": ts, "side": "SELL", "reason": sell_reason, "order": order, "position": pos,
                        "base_size": filled_size, "order_type": "MARKET_IOC", "fill": fill,
                        "realized_pnl_quote": realized, **tagged,
                    })
            else:
                result["decision"] = "ORDER_FAILED"
                api.append_jsonl(trades_log, {
                    "ts": ts, "side": "SELL", "reason": sell_reason, "order": order, "position": pos,
                    "failed": True, **tagged,
                })
        api.persist_positions(state, api.replace_position(positions, pos, replacement))
        return True

    api.persist_positions(state, held)
    return False
