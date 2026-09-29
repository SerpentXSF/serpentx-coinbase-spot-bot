#!/usr/bin/env python3
"""
Minimal Coinbase Advanced Trade REST client for Hermes/Umbrel.

Security defaults:
- Reads credentials only from environment variables or a local .env file.
- Never prints private keys.
- Orders are dry-run unless --live is supplied.
- Live order placement also requires COINBASE_TRADING_ENABLED=1.

Required env vars:
  COINBASE_API_KEY_NAME=organizations/.../apiKeys/...
  COINBASE_API_PRIVATE_KEY='<YOUR_COINBASE_PEM_WITH_ESCAPED_NEWLINES>'
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

# Reuse the bot's single implementation of .env loading, JWT signing, clock
# drift correction, and request handling so the two never drift apart.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coinbase_spot_bot as bot  # noqa: E402

BASE_HOST = bot.BASE_HOST
BASE_URL = bot.BASE_URL
load_dotenv = bot.load_dotenv
build_jwt = bot.build_jwt


def request(method: str, path: str, body: dict | None = None) -> dict:
    return bot.private_request(method, path, body)


def accounts(args):
    return request("GET", "/api/v3/brokerage/accounts")


def products(args):
    return request("GET", "/api/v3/brokerage/products")


def preview_market_order(args):
    side = args.side.upper()
    if side not in {"BUY", "SELL"}:
        raise RuntimeError("--side must be BUY or SELL")
    if not args.quote_size and not args.base_size:
        raise RuntimeError("Provide either --quote-size or --base-size")
    if args.quote_size and args.base_size:
        raise RuntimeError("Provide only one of --quote-size or --base-size")

    if args.quote_size:
        order_config = {"market_market_ioc": {"quote_size": str(args.quote_size)}}
    else:
        order_config = {"market_market_ioc": {"base_size": str(args.base_size)}}

    body = {"product_id": args.product_id, "side": side, "order_configuration": order_config}
    return request("POST", "/api/v3/brokerage/orders/preview", body)


def place_market_order(args):
    preview = preview_market_order(args)
    if not args.live:
        return {"dry_run": True, "message": "No order placed. Re-run with --live and COINBASE_TRADING_ENABLED=1 to execute.", "preview": preview}
    if os.getenv("COINBASE_TRADING_ENABLED") != "1":
        raise RuntimeError("Refusing live order: set COINBASE_TRADING_ENABLED=1 in the environment to enable trading.")

    side = args.side.upper()
    if args.quote_size:
        order_config = {"market_market_ioc": {"quote_size": str(args.quote_size)}}
    else:
        order_config = {"market_market_ioc": {"base_size": str(args.base_size)}}
    body = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": args.product_id,
        "side": side,
        "order_configuration": order_config,
    }
    return request("POST", "/api/v3/brokerage/orders", body)


def main():
    load_dotenv(bot.ROOT / ".env")
    parser = argparse.ArgumentParser(description="Coinbase Advanced Trade REST helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("accounts", help="List brokerage accounts")
    p.set_defaults(func=accounts)

    p = sub.add_parser("products", help="List available products")
    p.set_defaults(func=products)

    for name, func, help_text in [
        ("preview-market", preview_market_order, "Preview a market order without placing it"),
        ("market", place_market_order, "Dry-run a market order by default; add --live to place"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--product-id", required=True, help="Example: BTC-USD")
        p.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
        p.add_argument("--quote-size", help="Quote currency amount, e.g. USD amount for BTC-USD")
        p.add_argument("--base-size", help="Base asset amount, e.g. BTC amount for BTC-USD")
        if name == "market":
            p.add_argument("--live", action="store_true", help="Actually place the order; otherwise dry-run only")
        p.set_defaults(func=func)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
