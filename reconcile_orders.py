#!/usr/bin/env python3
"""Low-call Coinbase order/fill reconciliation helper.

Reads local successful trade-log entries, fetches a small number of Coinbase
historical order records by order_id, and writes a local reconciliation snapshot.
It is intentionally not on a frequent cron cadence; use manually or at a slow
analytics cadence to protect API limits.
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

CONFIG = ROOT / "config.json"
OUT = ROOT / "analysis" / "order_reconciliation.json"


def order_id_from_trade(row: dict[str, Any]) -> str | None:
    order = row.get("order") or {}
    if order.get("success") is not True:
        return None
    success = order.get("success_response") or {}
    return success.get("order_id") or order.get("order_id")


def iter_successful_trade_orders(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(errors="ignore").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        oid = order_id_from_trade(row)
        if not oid:
            continue
        rows.append({
            "ts": row.get("ts"),
            "side": row.get("side"),
            "reason": row.get("reason"),
            "product_id": (row.get("order", {}).get("success_response") or {}).get("product_id") or row.get("product_id"),
            "order_id": oid,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="Max recent successful orders to reconcile")
    ap.add_argument("--json", action="store_true", help="Print the snapshot")
    args = ap.parse_args()

    cfg = bot.load_json(CONFIG)
    bot.load_dotenv(cfg.get("env_file", ROOT / ".env"))
    env = bot.env_present()
    if not (env.get("COINBASE_API_KEY_NAME") and env.get("COINBASE_API_PRIVATE_KEY")):
        print("ERROR: Coinbase credentials are not visible to this runtime", file=sys.stderr)
        return 2

    trades = iter_successful_trade_orders(Path(cfg["trades_log_path"]))
    selected = list(reversed(trades))[: max(1, int(args.limit))]
    records = []
    for row in selected:
        oid = row["order_id"]
        rec = dict(row)
        try:
            data = bot.private_request("GET", f"/api/v3/brokerage/orders/historical/{oid}")
            order = data.get("order") or data
            rec["status"] = order.get("status")
            rec["filled_size"] = order.get("filled_size")
            rec["filled_value"] = order.get("filled_value")
            rec["average_filled_price"] = order.get("average_filled_price")
            rec["total_fees"] = order.get("total_fees")
            rec["settled"] = order.get("settled")
        except Exception as exc:
            rec["error"] = str(exc)[:300]
        records.append(rec)

    snapshot = {
        "generated_at": bot.utcnow().isoformat(),
        "limit": args.limit,
        "orders_checked": len(records),
        "records": records,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
