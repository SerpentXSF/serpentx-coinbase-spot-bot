#!/usr/bin/env python3
"""Optional push alerts for events that need a human's attention.

Set ``ALERT_WEBHOOK_URL`` in .env to enable. The payload format is picked from
the URL (Discord, Slack, ntfy) or forced with ``ALERT_WEBHOOK_FORMAT``
(discord | slack | ntfy | json). With no URL every function here is a no-op.

Alerts go out for real orders (sent, partial exit, limit placed/filled,
failed), the daily loss limit, an invalid config, and crashed runs -- not for
preview runs or closed live gates. Conditions that repeat every run (loss
limit, invalid config, the same crash) are sent at most once per
``alert_repeat_minutes`` (default 360). Sending never raises: an alert
failure must not affect trading.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

ORDER_DECISIONS = {"ORDER_SENT", "PARTIAL_EXIT_SENT", "LIMIT_ORDER_PLACED", "ORDER_FAILED"}
STOP_EVENTS = {"EXCHANGE_STOP_FILLED", "EXCHANGE_STOP_PLACE_FAILED", "EXCHANGE_STOP_MISSING"}
CONDITION_DECISIONS = {"DAILY_LOSS_LIMIT_REACHED", "CONFIG_INVALID"}
PENDING_ALERTS = {
    "PENDING_BUY_FILLED_POSITION_OPENED", "PENDING_SELL_FILLED_POSITION_CLOSED",
    "PENDING_PARTIAL_SELL_FILLED_POSITION_REDUCED", "PENDING_CANCEL_ERROR",
}


def _fmt_num(v: Any, places: int = 6) -> str:
    try:
        return f"{float(v):.{places}g}"
    except (TypeError, ValueError):
        return str(v)


def messages_for(result: dict[str, Any], *, source: str = "bot") -> list[tuple[str, str]]:
    """Return (dedupe_key, text) pairs worth alerting on for one run result."""
    out: list[tuple[str, str]] = []
    decision = str(result.get("decision") or "")
    order = result.get("proposed_order") or {}
    product = order.get("product_id", "?")
    side = order.get("side", "?")
    oid = ((result.get("order") or {}).get("success_response") or {}).get("order_id") or result.get("ts", "")

    if decision in ORDER_DECISIONS:
        size = f"{_fmt_num(order['quote_size'])} quote" if order.get("quote_size") else f"{_fmt_num(order.get('base_size'))} base"
        fill = result.get("fill") or {}
        price = fill.get("average_filled_price") or order.get("limit_price")
        parts = [f"{decision}: {side} {product} {size}"]
        if order.get("reason"):
            parts.append(f"reason {order['reason']}")
        if price:
            parts.append(f"price {_fmt_num(price)}")
        if result.get("realized_pnl_quote") is not None:
            parts.append(f"realized P&L {float(result['realized_pnl_quote']):+.4f}")
        if decision == "ORDER_FAILED":
            err = (result.get("order") or {}).get("error_response") or (result.get("order") or {}).get("failure_reason")
            if err:
                parts.append(f"error {json.dumps(err)[:200]}")
        out.append((f"order:{decision}:{oid}", " | ".join(parts)))

    if decision == "DAILY_LOSS_LIMIT_REACHED":
        info = result.get("daily_loss_limit") or {}
        out.append((f"cond:loss:{info.get('date')}", f"Daily loss limit reached: realized {_fmt_num(info.get('realized_quote'))} "
                     f"vs limit {_fmt_num(info.get('limit_quote'))}. New entries paused until 00:00 UTC; exits continue."))
    if decision == "PENDING_CANCEL_FAILED":
        out.append((f"cond:pendingcancel:{product}", f"Stop-type exit for {product} is blocked: could not cancel its resting limit sell; "
                     f"nothing sold this run: {str(result.get('pending_cancel_error'))[:300]}"))
    if decision == "EXCHANGE_STOP_CANCEL_FAILED":
        out.append((f"cond:stopcancel:{product}", f"Could not cancel the exchange stop for {product} before exiting; "
                     f"exit skipped this run: {str(result.get('exchange_stop_error'))[:300]}"))
    for ev in result.get("exchange_stop_events") or []:
        kind = ev.get("event")
        if kind in STOP_EVENTS:
            detail = ", ".join(f"{k} {_fmt_num(v) if isinstance(v, (int, float)) else v}" for k, v in ev.items() if k not in {"event", "product_id"})
            key = f"stop:{kind}:{ev.get('product_id')}:{ev.get('average_filled_price') or ev.get('error') or ev.get('status')}"
            out.append((key, f"{kind}: {ev.get('product_id')} {detail}"[:600]))
    if decision == "CONFIG_INVALID":
        errs = (result.get("config_check") or {}).get("errors", [])
        out.append(("cond:config", "config.json is invalid, new entries blocked: " + "; ".join(errs)[:500]))

    for ev in result.get("pending_order_events") or []:
        d = ev.get("decision")
        if d in PENDING_ALERTS:
            row = ev.get("pending_order") or {}
            pnl = ev.get("realized_pnl_quote")
            text = f"{d}: {row.get('side', '?')} {row.get('product_id', '?')} filled {_fmt_num(ev.get('filled_size'))} @ {_fmt_num(ev.get('avg_price'))}"
            if pnl is not None:
                text += f" | realized P&L {float(pnl):+.4f}"
            out.append((f"pending:{d}:{row.get('order_id')}", text))
    return [(key, f"[{source}] {text}") for key, text in out]


def _payload(url: str, text: str) -> tuple[dict[str, Any] | None, str | None]:
    fmt = (os.getenv("ALERT_WEBHOOK_FORMAT") or "").lower()
    if not fmt:
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            fmt = "discord"
        elif "hooks.slack.com" in url:
            fmt = "slack"
        elif "ntfy" in url:
            fmt = "ntfy"
        else:
            fmt = "json"
    if fmt == "discord":
        return {"content": text[:1900]}, None
    if fmt == "slack":
        return {"text": text}, None
    if fmt == "ntfy":
        return None, text
    return {"text": text, "source": "serpentx-coinbase-spot-bot"}, None


def _sent_path(cfg: dict[str, Any]) -> Path:
    return Path(cfg.get("state_path", "state/state.json")).parent / "alerts_sent.json"


def send(cfg: dict[str, Any], items: list[tuple[str, str]]) -> int:
    """Send alert items, skipping recent repeats. Returns how many were sent."""
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url or not items:
        return 0
    path = _sent_path(cfg)
    try:
        sent = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        sent = {}
    now = time.time()
    repeat_s = float(cfg.get("alert_repeat_minutes", 360)) * 60
    count = 0
    for key, text in items:
        if now - float(sent.get(key, 0)) < repeat_s:
            continue
        body, raw = _payload(url, text)
        try:
            if raw is not None:
                r = requests.post(url, data=raw.encode("utf-8"), timeout=5)
            else:
                r = requests.post(url, json=body, timeout=5)
            if r.status_code < 400:
                sent[key] = now
                count += 1
        except Exception:
            continue  # never let alerting break a trading run
    # Drop old keys so the file stays small.
    sent = {k: v for k, v in sent.items() if now - float(v) < max(repeat_s, 7 * 86400)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sent, indent=2, sort_keys=True))
    except Exception:
        pass
    return count


def notify_result(cfg: dict[str, Any], result: dict[str, Any], *, source: str = "bot") -> int:
    try:
        return send(cfg, messages_for(result, source=source))
    except Exception:
        return 0


def notify_error(cfg: dict[str, Any], exc: BaseException, *, source: str = "bot") -> int:
    text = f"[{source}] run failed: {type(exc).__name__}: {str(exc)[:400]}"
    try:
        return send(cfg, [(f"error:{source}:{type(exc).__name__}:{str(exc)[:80]}", text)])
    except Exception:
        return 0
