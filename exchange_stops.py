#!/usr/bin/env python3
"""Stop-loss orders held by Coinbase itself, as a backstop (opt-in).

The bot's own stops only fire when a run happens (every few minutes) and the
host is up. With ``exchange_stop_enabled: true`` each open position also gets a
Coinbase stop-limit SELL resting on the exchange, so a crash, sleep, or network
outage cannot leave a position unprotected.

The backstop sits *below* the bot's own stop (``stop_loss_pct`` +
``exchange_stop_buffer_pct``), so routine exits stay with the bot and the
exchange order only matters when the bot cannot act.

An open order puts a hold on the coins it covers, so they vanish from
``available_balance``. The exit engine therefore counts coins held by our own
stop as available, cancels the stop right before any bot exit, and treats a
stop that already filled as the exit instead of selling twice.

Like exits.py, Coinbase access goes through the ``api`` module passed in.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

OPEN_STATUSES = {"OPEN", "PENDING", "QUEUED", "UNKNOWN_ORDER_STATUS"}
DONE_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "FAILED", "REJECTED"}


def enabled(cfg: dict[str, Any]) -> bool:
    return cfg.get("exchange_stop_enabled") is True


def stop_prices(cfg: dict[str, Any], entry: float) -> tuple[float, float]:
    """(stop trigger price, limit price) for a position entered at ``entry``."""
    stop_pct = float(cfg.get("stop_loss_pct", 0.035)) + float(cfg.get("exchange_stop_buffer_pct", 0.01))
    stop = entry * (1 - stop_pct)
    limit = stop * (1 - float(cfg.get("exchange_stop_limit_offset_pct", 0.01)))
    return stop, limit


def held_size(pos: dict[str, Any]) -> float:
    """Base size currently held on the exchange by this position's own stop order."""
    stop = pos.get("exchange_stop") or {}
    try:
        return float(stop.get("base_size") or 0) if stop.get("order_id") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _event(result: dict[str, Any], kind: str, pos: dict[str, Any], **extra: Any) -> None:
    result.setdefault("exchange_stop_events", []).append({"event": kind, "product_id": pos.get("product_id"), **extra})


def place(api: ModuleType, cfg: dict[str, Any], pos: dict[str, Any], result: dict[str, Any], *, live: bool) -> bool:
    """Place the backstop for ``pos`` if enabled and the live gates are open.

    Always previews first; a preview error means nothing is placed. Returns
    True when an order now rests on the exchange.
    """
    if not enabled(cfg) or pos.get("exchange_stop", {}).get("order_id"):
        return False
    product_id = str(pos.get("product_id"))
    entry = api.fnum(pos.get("entry_price"))
    size = api.fnum(pos.get("base_size_est"))
    if entry <= 0 or size <= 0:
        return False
    try:
        stop, limit = stop_prices(cfg, entry)
        current = api.fnum(api.product_info(product_id).get("price"))
    except Exception as exc:
        pos["exchange_stop_error"] = f"price lookup failed: {str(exc)[:200]}"
        _event(result, "EXCHANGE_STOP_PLACE_FAILED", pos, error=pos["exchange_stop_error"])
        return False
    if 0 < current <= stop:
        # Already through the backstop level: the bot's own stop handles it now.
        _event(result, "EXCHANGE_STOP_SKIPPED_PRICE_BELOW_STOP", pos, stop_price=stop, current_price=current)
        return False
    try:
        preview = api.preview_stop_limit(product_id, size, stop, limit)
        errs = preview.get("errs") or preview.get("error_response")
        if errs:
            raise RuntimeError(f"preview rejected: {str(errs)[:200]}")
        if api.live_gates_open(cfg, live):
            # Preview mode: Coinbase validated the order, nothing is placed.
            _event(result, "EXCHANGE_STOP_PREVIEW_OK", pos, stop_price=stop, limit_price=limit, base_size=size)
            return False
        order = api.place_stop_limit(product_id, size, stop, limit)
        if order.get("success") is not True:
            raise RuntimeError(f"order rejected: {str(order.get('error_response') or order)[:200]}")
    except Exception as exc:
        pos["exchange_stop_error"] = str(exc)[:240]
        _event(result, "EXCHANGE_STOP_PLACE_FAILED", pos, error=str(exc)[:240])
        return False
    pos.pop("exchange_stop_error", None)
    pos["exchange_stop"] = {
        "order_id": api.order_id_from_response(order),
        "stop_price": stop,
        "limit_price": limit,
        "base_size": size,
        "placed_at": result.get("ts"),
    }
    _event(result, "EXCHANGE_STOP_PLACED", pos, **pos["exchange_stop"])
    return True


def record_stop_fill(api: ModuleType, cfg: dict[str, Any], state: dict[str, Any], pos: dict[str, Any], detail: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    """Book an exchange-stop fill: P&L, cooldowns, trade log. Returns the remaining position (None if closed).

    Idempotent per stop order: a fill already booked (e.g. by a run that then
    failed before saving the reduced position) is never booked twice.
    """
    fnum = api.fnum
    oid = str((pos.get("exchange_stop") or {}).get("order_id") or "")
    booked = state.setdefault("booked_exchange_stop_fills", [])
    remaining = dict(pos)
    remaining.pop("exchange_stop", None)
    if oid and oid in booked:
        return api.apply_partial_exit_fill(remaining, fnum(detail.get("filled_size")), result["ts"])
    filled = fnum(detail.get("filled_size"))
    price = fnum(detail.get("average_filled_price")) or fnum((pos.get("exchange_stop") or {}).get("limit_price"))
    fill = {"order_id": (pos.get("exchange_stop") or {}).get("order_id"), "status": detail.get("status"), "filled_size": filled,
            "average_filled_price": price, "filled_value": fnum(detail.get("filled_value")) or filled * price,
            "total_fees": fnum(detail.get("total_fees"))}
    realized = api.realized_pnl_quote(cfg, pos, filled, fill, price)
    api.record_realized_pnl(state, realized, result["ts"])
    if oid:
        booked.append(oid)
        del booked[:-100]  # keep the marker list small
    product_id = str(pos.get("product_id"))
    api.set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_stop", 720)))
    state["cooldown_until"] = (api.utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
    api.append_jsonl(Path(cfg["trades_log_path"]), {
        "ts": result["ts"], "side": "SELL", "reason": "EXCHANGE_STOP", "order": {"success": True, "success_response": {"order_id": fill["order_id"], "product_id": product_id, "side": "SELL"}},
        "position": pos, "base_size": filled, "order_type": "STOP_LIMIT_GTC", "fill": fill, "realized_pnl_quote": realized,
    })
    _event(result, "EXCHANGE_STOP_FILLED", pos, filled_size=filled, average_filled_price=price, realized_pnl_quote=realized)
    return api.apply_partial_exit_fill(remaining, filled, result["ts"])


def sync(api: ModuleType, cfg: dict[str, Any], state: dict[str, Any], result: dict[str, Any], *, live: bool) -> None:
    """Reconcile every position's backstop with Coinbase, then place any that are missing.

    Filled -> the position is closed (or reduced) with real P&L. Cancelled or
    expired without a fill (e.g. by hand in the Coinbase app) -> re-placed.
    """
    if not enabled(cfg):
        return
    positions = api.normalize_positions(state)
    updated: list[dict[str, Any]] = []
    for pos in positions:
        try:
            _sync_one(api, cfg, state, pos, result, updated, live=live)
        except Exception as exc:  # one position's problem must never skip exit management
            pos["exchange_stop_error"] = f"sync failed: {str(exc)[:200]}"
            _event(result, "EXCHANGE_STOP_PLACE_FAILED", pos, error=pos["exchange_stop_error"])
            if not any(p is pos for p in updated):
                updated.append(pos)
    api.persist_positions(state, updated)


def _sync_one(api: ModuleType, cfg: dict[str, Any], state: dict[str, Any], pos: dict[str, Any],
              result: dict[str, Any], updated: list[dict[str, Any]], *, live: bool) -> None:
    stop = pos.get("exchange_stop") or {}
    if stop.get("order_id"):
        try:
            detail = api.fetch_order_detail(str(stop["order_id"]))
        except Exception as exc:
            pos["exchange_stop_error"] = f"status check failed: {str(exc)[:200]}"
            updated.append(pos)
            return
        status = str(detail.get("status") or "").upper()
        if api.fnum(detail.get("filled_size")) > 0 and (status in DONE_STATUSES):
            remaining = record_stop_fill(api, cfg, state, pos, detail, result)
            if remaining:
                updated.append(remaining)  # commit the reduced position before anything else can fail
                try:
                    place(api, cfg, remaining, result, live=live)
                except Exception as exc:
                    remaining["exchange_stop_error"] = f"re-place failed: {str(exc)[:200]}"
            return
        if status in DONE_STATUSES:
            pos.pop("exchange_stop", None)
            _event(result, "EXCHANGE_STOP_MISSING", pos, status=status)
        else:
            updated.append(pos)
            return
    if not api.pending_order_active(state, str(pos.get("product_id")), "SELL"):
        # A resting limit SELL already holds these coins; re-place once it resolves.
        place(api, cfg, pos, result, live=live)
    updated.append(pos)


def cancel_for_exit(api: ModuleType, cfg: dict[str, Any], state: dict[str, Any], pos: dict[str, Any], result: dict[str, Any]) -> tuple[str, Any]:
    """Free the coins held by ``pos``'s backstop so the bot can sell them.

    The stop's fill status is always checked, even after a successful cancel:
    a stop-limit can partially fill before the cancel lands.

    Returns one of:
      ("none", pos)        no backstop to cancel
      ("cancelled", pos2)  the backstop is gone; pos2 is the position after any
                           partial stop fill was booked (sell pos2's size)
      ("filled", None)     it filled (fully) before we could cancel; the exit
                           already happened on the exchange
      ("failed", reason)   could not cancel, or could not confirm its fills;
                           do not sell this run
    """
    stop = pos.get("exchange_stop") or {}
    oid = stop.get("order_id")
    if not oid:
        return "none", pos
    cancelled, cancel_error = False, ""
    try:
        cancelled = api.cancel_succeeded(api.cancel_order(str(oid)), str(oid))
    except Exception as exc:
        cancel_error = str(exc)[:200]
    try:
        detail = api.fetch_order_detail(str(oid))
    except Exception as exc:
        # Keep the stop record either way: the next sync() reads its final
        # status and books any fill, instead of us guessing now.
        state_note = "cancelled" if cancelled else f"cancel failed ({cancel_error or 'rejected'})"
        return "failed", f"{state_note}; could not confirm stop fills: {str(exc)[:160]}"
    status = str(detail.get("status") or "").upper()
    done = cancelled or status in DONE_STATUSES
    if not done:
        return "failed", f"cancel failed ({cancel_error or 'rejected'}); stop still {status or 'unknown'}"
    if api.fnum(detail.get("filled_size")) > 0:
        remaining = record_stop_fill(api, cfg, state, pos, detail, result)
        if remaining is None:
            return "filled", None
        _event(result, "EXCHANGE_STOP_CANCELLED_FOR_EXIT", pos, order_id=oid, filled_before_cancel=api.fnum(detail.get("filled_size")))
        return "cancelled", remaining
    pos.pop("exchange_stop", None)
    _event(result, "EXCHANGE_STOP_CANCELLED_FOR_EXIT", pos, order_id=oid)
    return "cancelled", pos
