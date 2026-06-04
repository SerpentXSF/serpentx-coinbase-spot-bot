#!/usr/bin/env python3
"""
Minimal Coinbase Advanced Trade REST client.

Security defaults:
- Reads credentials only from environment variables or a local .env file.
- Never prints private keys.
- Orders are dry-run unless --live is supplied.
- Live order placement also requires COINBASE_TRADING_ENABLED=1.

Required env vars:
  COINBASE_API_KEY_NAME=organizations/.../apiKeys/...
  COINBASE_API_PRIVATE_KEY='PASTE_YOUR_COINBASE_PEM_PRIVATE_KEY_WITH_ESCAPED_NEWLINES'
"""

import argparse
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

BASE_HOST = "api.coinbase.com"
BASE_URL = f"https://{BASE_HOST}"


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def load_private_key(secret: str):
    secret = secret.replace("\\n", "\n")
    if secret.lstrip().startswith("-----BEGIN"):
        return serialization.load_pem_private_key(secret.encode("utf-8"), password=None)
    raise RuntimeError("Private key must be PEM text for this client.")


def algorithm_for(private_key) -> str:
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return "EdDSA"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ES256"
    raise RuntimeError(f"Unsupported private key type: {type(private_key).__name__}")


def build_jwt(method: str, path: str) -> str:
    key_name = require_env("COINBASE_API_KEY_NAME")
    private_key = load_private_key(require_env("COINBASE_API_PRIVATE_KEY"))
    now = int(time.time())
    uri = f"{method.upper()} {BASE_HOST}{path}"
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
    }
    headers = {"kid": key_name, "nonce": secrets.token_hex()}
    return jwt.encode(payload, private_key, algorithm=algorithm_for(private_key), headers=headers)


def request(method: str, path: str, body: dict | None = None) -> dict:
    method = method.upper()
    token = build_jwt(method, path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, BASE_URL + path, headers=headers, json=body, timeout=30)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        raise RuntimeError(json.dumps({"status_code": resp.status_code, "response": data}, indent=2))
    return data


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
    load_dotenv()
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
