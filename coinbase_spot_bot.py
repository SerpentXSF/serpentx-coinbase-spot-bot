#!/usr/bin/env python3
"""Coinbase Advanced Trade spot strategy bot for SerpentX/Hermes.

Defaults are intentionally safe:
- Loads credentials from .env or environment.
- Public market analysis works without credentials.
- Private account verification requires COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY.
- Live orders require all three gates: config active_trading=true, COINBASE_TRADING_ENABLED=1, and --live.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
except Exception:  # keep public/shadow analysis usable if auth libs are missing
    jwt = None
    serialization = None
    ec = None
    ed25519 = None

BASE_HOST = "api.coinbase.com"
BASE_URL = f"https://{BASE_HOST}"
ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
DEFAULT_CONFIG = ROOT / "config.json"

GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _provider_budget_path(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or {}
    return Path(cfg.get("provider_budget_path") or str(ROOT / "state" / "provider_budget.json"))


def _provider_state(cfg: dict[str, Any], provider: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    path = _provider_budget_path(cfg)
    try:
        data = json.loads(path.read_text()) if path.exists() else {"providers": {}}
        if not isinstance(data, dict):
            data = {"providers": {}}
    except Exception:
        data = {"providers": {}}
    providers = data.setdefault("providers", {})
    state = providers.setdefault(provider, {})
    return state, path, data


def _save_provider_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data["updated_at"] = time.time()
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _provider_can_request(cfg: dict[str, Any], provider: str) -> tuple[bool, str]:
    state, path, data = _provider_state(cfg, provider)
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0)
    if cooldown_until > now:
        return False, "cooldown"
    min_interval = float(cfg.get("provider_min_interval_seconds", {}).get(provider, 1.0)) if isinstance(cfg.get("provider_min_interval_seconds"), dict) else 1.0
    last_request = float(state.get("last_request_at") or 0)
    if (last_request + min_interval) > now:
        return False, "min_interval"
    state["last_request_at"] = now
    _save_provider_state(path, data)
    return True, ""


def _provider_record(cfg: dict[str, Any], provider: str, *, ok: bool, status_code: int | None = None, error: str = "") -> None:
    state, path, data = _provider_state(cfg, provider)
    now = time.time()
    if ok:
        state["last_success_at"] = now
        state["success_count"] = int(state.get("success_count") or 0) + 1
        state["consecutive_errors"] = 0
    else:
        state["last_error_at"] = now
        state["error_count"] = int(state.get("error_count") or 0) + 1
        state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
        if status_code is not None:
            state["last_status_code"] = int(status_code)
        if error:
            state["last_error"] = str(error)[:80]
        if status_code == 429 or "rate" in error.lower() or "quota" in error.lower() or state["consecutive_errors"] >= 3:
            state["cooldown_until"] = now + float(cfg.get("provider_cooldown_seconds", 300))
    _save_provider_state(path, data)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, sort_keys=True) + "\n")


def load_dotenv(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        return
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    for raw in p.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        # Shared provider templates intentionally contain blank placeholders.
        # Do not let an empty centralized value mask a project-local fallback.
        if value == "":
            continue
        os.environ.setdefault(key, value)


def env_present() -> dict[str, bool]:
    return {
        "COINBASE_API_KEY_NAME": bool(os.getenv("COINBASE_API_KEY_NAME")),
        "COINBASE_API_PRIVATE_KEY": bool(os.getenv("COINBASE_API_PRIVATE_KEY")),
        "COINBASE_TRADING_ENABLED": os.getenv("COINBASE_TRADING_ENABLED") == "1",
        "HELIUS_API_KEY": bool(os.getenv("HELIUS_API_KEY")),
        "HELIUS_RPC_URL": bool(os.getenv("HELIUS_RPC_URL") or os.getenv("HELIUS_GATEKEEPER_RPC_URL")),
        "ALCHEMY_SOLANA_RPC_URL": bool(os.getenv("ALCHEMY_SOLANA_RPC_URL")),
        "ALCHEMY_SOLANA_WSS_URL": bool(os.getenv("ALCHEMY_SOLANA_WSS_URL")),
        "UNUSUAL_WHALES_API_KEY": bool(os.getenv("UNUSUAL_WHALES_API_KEY")),
        "COINGECKO_API_KEY": bool(os.getenv("COINGECKO_API_KEY") or os.getenv("COINGECKO_DEMO_API_KEY") or os.getenv("COINGECKO_PRO_API_KEY")),
        "COINMARKETCAP_API_KEY": bool(os.getenv("COINMARKETCAP_API_KEY")),
        "BIRDEYE_API_KEY": bool(os.getenv("BIRDEYE_API_KEY")),
    }


def public_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    last_status = None
    last_data: dict[str, Any] = {}
    for attempt in range(4):
        r = requests.get(BASE_URL + path, params=params, timeout=30)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code < 400:
            return data
        last_status, last_data = r.status_code, data
        if r.status_code in {429, 500, 502, 503, 504} and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        break
    raise RuntimeError(f"public GET {path} failed {last_status}: {last_data}")


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def load_private_key(secret: str):
    if serialization is None:
        raise RuntimeError("Missing auth dependency: cryptography/PyJWT")
    secret = secret.replace("\\n", "\n")
    if not secret.lstrip().startswith("-----BEGIN"):
        raise RuntimeError("Coinbase private key must be PEM text")
    return serialization.load_pem_private_key(secret.encode(), password=None)


def algorithm_for(private_key) -> str:
    if ed25519 is not None and isinstance(private_key, ed25519.Ed25519PrivateKey):
        return "EdDSA"
    if ec is not None and isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ES256"
    raise RuntimeError(f"Unsupported private key type: {type(private_key).__name__}")


_COINBASE_TIME_OFFSET = {"value": 0.0, "checked_at": 0.0}


def coinbase_epoch_now() -> int:
    """Return Coinbase-aligned epoch seconds for JWT auth.

    WSL clocks can drift a few minutes after host sleep/restart. Coinbase JWTs
    only live for 120 seconds, so a local clock that is behind the API server
    causes otherwise-valid credentials to return 401 Unauthorized. Cache the
    public Coinbase Date-header offset briefly and use it for private JWTs.
    """
    now = time.time()
    if now - float(_COINBASE_TIME_OFFSET.get("checked_at") or 0) < 300:
        return int(now + float(_COINBASE_TIME_OFFSET.get("value") or 0))
    try:
        r = requests.get(BASE_URL + "/api/v3/brokerage/market/products/BTC-USDC", timeout=10)
        server_date = r.headers.get("Date")
        if server_date:
            server_dt = parsedate_to_datetime(server_date)
            offset = server_dt.timestamp() - now
            # Ignore tiny skew but correct material drift that would expire JWTs.
            _COINBASE_TIME_OFFSET["value"] = offset if abs(offset) > 30 else 0.0
            _COINBASE_TIME_OFFSET["checked_at"] = now
    except Exception:
        _COINBASE_TIME_OFFSET["checked_at"] = now
    return int(time.time() + float(_COINBASE_TIME_OFFSET.get("value") or 0))


def build_jwt(method: str, path: str) -> str:
    if jwt is None:
        raise RuntimeError("Missing auth dependency: PyJWT")
    key_name = require_env("COINBASE_API_KEY_NAME")
    private_key = load_private_key(require_env("COINBASE_API_PRIVATE_KEY"))
    now = coinbase_epoch_now()
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method.upper()} {BASE_HOST}{path}",
    }
    headers = {"kid": key_name, "nonce": secrets.token_hex()}
    return jwt.encode(payload, private_key, algorithm=algorithm_for(private_key), headers=headers)


def private_request(method: str, path: str, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    method = method.upper()
    token = build_jwt(method, path)
    r = requests.request(
        method,
        BASE_URL + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        params=params,
        timeout=30,
    )
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 400:
        raise RuntimeError(json.dumps({"status_code": r.status_code, "response": data}, indent=2))
    return data


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def ema(vals: list[float], period: int) -> float:
    if not vals:
        return 0.0
    k = 2 / (period + 1)
    out = vals[0]
    for v in vals[1:]:
        out = v * k + out * (1 - k)
    return out


def rsi(vals: list[float], period: int = 14) -> float:
    if len(vals) < period + 1:
        return 50.0
    gains, losses = [], []
    for a, b in zip(vals[-period-1:-1], vals[-period:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _rsi_series(vals: list[float], period: int = 14) -> list[float | None]:
    """Return simple rolling RSI values aligned to vals, using no paid APIs."""
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period + 1:
        return out
    for i in range(period, len(vals)):
        out[i] = rsi(vals[: i + 1], period)
    return out


def _swing_points(values: list[float], *, mode: str, window: int) -> list[int]:
    idxs: list[int] = []
    if window < 1 or len(values) < (window * 2 + 1):
        return idxs
    for i in range(window, len(values) - window):
        segment = values[i - window : i + window + 1]
        v = values[i]
        if mode == "low":
            if v == min(segment) and v < values[i - 1] and v <= values[i + 1]:
                idxs.append(i)
        else:
            if v == max(segment) and v > values[i - 1] and v >= values[i + 1]:
                idxs.append(i)
    return idxs


def rsi_divergence(candles: list[dict[str, Any]], period: int = 14, swing_window: int = 2, lookback: int = 80) -> dict[str, Any]:
    """Detect classic RSI divergence from local candle closes.

    Bullish Divergence: price makes a lower swing low while RSI makes a higher low.
    Bearish Divergence: price makes a higher swing high while RSI makes a lower high.
    This is a free/local technical metric; it uses only the candles already fetched
    from Coinbase and never calls an extra provider.
    """
    closes = [fnum(c.get("close")) for c in candles if fnum(c.get("close")) > 0]
    closes = closes[-lookback:]
    neutral = {
        "signal": "none",
        "label": "None",
        "previous_price": None,
        "latest_price": None,
        "previous_rsi": None,
        "latest_rsi": None,
        "previous_index": None,
        "latest_index": None,
    }
    if len(closes) < max(period + 3, swing_window * 2 + 3):
        return neutral
    rsis = _rsi_series(closes, period)

    def pair_payload(signal: str, label: str, a: int, b: int) -> dict[str, Any]:
        return {
            "signal": signal,
            "label": label,
            "previous_price": closes[a],
            "latest_price": closes[b],
            "previous_rsi": rsis[a],
            "latest_rsi": rsis[b],
            "previous_index": a,
            "latest_index": b,
        }

    lows = [i for i in _swing_points(closes, mode="low", window=swing_window) if rsis[i] is not None]
    highs = [i for i in _swing_points(closes, mode="high", window=swing_window) if rsis[i] is not None]
    recent_lows = lows[-4:]
    recent_highs = highs[-4:]
    for a, b in zip(recent_lows, recent_lows[1:]):
        if closes[b] < closes[a] and (rsis[b] or 0) > (rsis[a] or 0):
            return pair_payload("bullish", "Bullish Divergence", a, b)
    for a, b in zip(recent_highs, recent_highs[1:]):
        if closes[b] > closes[a] and (rsis[b] or 0) < (rsis[a] or 0):
            return pair_payload("bearish", "Bearish Divergence", a, b)
    return neutral


def fetch_candles(product_id: str, granularity: str, lookback_hours: int) -> list[dict[str, Any]]:
    end = int(time.time())
    start = end - lookback_hours * 3600
    data = public_get(
        f"/api/v3/brokerage/market/products/{product_id}/candles",
        {"start": start, "end": end, "granularity": granularity},
    )
    candles = data.get("candles", [])
    candles.sort(key=lambda c: int(c.get("start", 0)))
    return candles


def product_info(product_id: str) -> dict[str, Any]:
    return public_get(f"/api/v3/brokerage/market/products/{product_id}")


def product_metadata(product_id: str, ttl_seconds: int = 86400) -> dict[str, Any]:
    """Cached Coinbase product metadata for sizing/preflight checks.

    Prices stay live via product_info(); this cache is only for relatively stable
    order constraints such as base_increment, quote_increment, min sizes, and
    orderability flags so we avoid spending an extra public API call on every
    order preview/place.
    """
    cache_path = ROOT / "analysis" / "product_metadata_cache.json"
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except Exception:
        cache = {}
    row = cache.get(product_id)
    if isinstance(row, dict) and _fresh(row.get("fetched_at"), ttl_seconds):
        return row.get("data", row)
    info = product_info(product_id)
    wanted = {
        "product_id": info.get("product_id", product_id),
        "base_increment": info.get("base_increment"),
        "quote_increment": info.get("quote_increment"),
        "base_min_size": info.get("base_min_size"),
        "quote_min_size": info.get("quote_min_size"),
        "trading_disabled": info.get("trading_disabled"),
        "is_disabled": info.get("is_disabled"),
        "cancel_only": info.get("cancel_only"),
        "limit_only": info.get("limit_only"),
        "status": info.get("status"),
    }
    cache[product_id] = {"fetched_at": utcnow().isoformat(), "data": wanted}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
        tmp.replace(cache_path)
    except Exception:
        pass
    return wanted


def is_market_orderable_product(info: dict[str, Any]) -> bool:
    """True when Coinbase should accept market IOC orders for this product."""
    if info.get("product") and isinstance(info.get("product"), dict):
        info = info["product"]
    if info.get("trading_disabled") or info.get("is_disabled") or info.get("cancel_only") or info.get("limit_only"):
        return False
    return str(info.get("status") or "").lower() in {"", "online"}



NEGATIVE_CONTEXT_WORDS = {
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sec", "delist", "delisting",
    "outage", "halt", "insolvent", "bankrupt", "rug", "scam", "drain", "breach",
    "investigation", "charges", "sanction", "stolen", "vulnerability",
}
POSITIVE_CONTEXT_WORDS = {
    "upgrade", "partnership", "integrates", "integration", "launch", "mainnet", "listing",
    "listed", "etf", "approval", "adoption", "funding", "revenue", "record", "growth",
    # Institutional/tokenization catalysts. These are bounded context boosts only;
    # they cannot bypass technical, regime, fee, sizing, or risk gates.
    "tokenization", "tokenized", "tokenize", "rwa", "real world asset", "real-world asset",
    "dtcc", "dtc", "franklin", "franklin templeton", "benji", "mastercard",
    "stablecoin", "stablecoins", "payments", "payfi", "institutional", "money market",
    "treasury", "settlement", "public blockchain",
}
SOCIAL_POSITIVE_WORDS = {"bullish", "breakout", "accumulating", "accumulation", "strong", "support", "trend", "tokenization", "tokenized", "rwa", "payfi", "institutional"}
SOCIAL_NEGATIVE_WORDS = {"bearish", "dump", "scam", "rug", "panic", "exploit", "hack", "selloff"}


def _context_cache_path(cfg: dict[str, Any]) -> Path:
    return Path(cfg.get("context_cache_path", ROOT / "analysis" / "context_scores.json"))


def _load_context_cache(cfg: dict[str, Any]) -> dict[str, Any]:
    path = _context_cache_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_context_cache(cfg: dict[str, Any], cache: dict[str, Any]) -> None:
    save_json(_context_cache_path(cfg), cache)


def _fresh(ts: str | None, ttl_seconds: int) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (utcnow() - dt).total_seconds() < ttl_seconds
    except Exception:
        return False


def _base_symbol(product_id: str) -> str:
    return product_id.split("-", 1)[0].upper()


def _token_meta(cfg: dict[str, Any], product_id: str) -> dict[str, Any]:
    return dict(cfg.get("token_context", {}).get(product_id, {}))


def _score_text_items(items: list[str], positive_words: set[str], negative_words: set[str], *, strong_negative=-2, mild_negative=-1, mild_positive=1, strong_positive=2) -> tuple[int, list[str], list[str]]:
    joined = " ".join(items).lower()
    def hits(words: set[str]) -> list[str]:
        out = []
        for raw in words:
            w = str(raw or "").strip().lower()
            if not w:
                continue
            if re.search(r"\b" + re.escape(w) + r"\b", joined):
                out.append(w)
        return sorted(set(out))
    pos_hits = hits(positive_words)
    neg_hits = hits(negative_words)
    score = 0
    if len(neg_hits) >= 2:
        score = strong_negative
    elif neg_hits:
        score = mild_negative
    elif len(pos_hits) >= 2:
        score = strong_positive
    elif pos_hits:
        score = mild_positive
    return score, pos_hits, neg_hits



def _market_headers(provider: str = "generic") -> dict[str, str]:
    headers = {"User-Agent": "SerpentXHermesBot/1.0"}
    if provider.startswith("coingecko"):
        cg_key = os.getenv("COINGECKO_API_KEY") or os.getenv("COINGECKO_DEMO_API_KEY") or os.getenv("COINGECKO_PRO_API_KEY")
        if cg_key:
            headers["x-cg-demo-api-key"] = cg_key
            headers["x-cg-pro-api-key"] = cg_key
    if provider.startswith("coinmarketcap") and os.getenv("COINMARKETCAP_API_KEY"):
        headers["X-CMC_PRO_API_KEY"] = os.getenv("COINMARKETCAP_API_KEY", "")
    return headers


def _get_json_cached(cfg: dict[str, Any], cache: dict[str, Any], key: str, url: str, params: dict[str, Any] | None, ttl: int, provider: str) -> dict[str, Any]:
    cached = cache.get(key)
    if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
        return {**cached, "cached": True}
    out: dict[str, Any] = {"provider": provider, "fetched_at": utcnow().isoformat()}
    allowed, reason = _provider_can_request(cfg, provider)
    if not allowed:
        out.update({"error": f"provider_backoff:{reason}"})
        return out
    try:
        r = requests.get(url, params=params, timeout=20, headers=_market_headers(provider))
        if r.status_code == 429:
            _provider_record(cfg, provider, ok=False, status_code=429, error="rate_limited")
            out["error"] = "rate_limited"
        else:
            r.raise_for_status()
            out["data"] = r.json()
            _provider_record(cfg, provider, ok=True, status_code=r.status_code)
    except Exception as e:
        _provider_record(cfg, provider, ok=False, error=type(e).__name__)
        out["error"] = str(e)[:200]
    cache[key] = out
    return out


def fetch_market_context(cfg: dict[str, Any], product_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    market_cfg = cfg.get("market_context", {})
    if not market_cfg.get("enabled", False):
        return {"score": 0, "reasons": ["market context disabled"], "risk_block": False, "provider": "disabled"}
    ttl = int(market_cfg.get("cache_ttl_seconds", 3600))
    meta = _token_meta(cfg, product_id)
    cg_id = meta.get("coingecko_id")
    out = {"score": 0, "reasons": [], "risk_block": False, "provider": "coingecko_altfng", "fetched_at": utcnow().isoformat()}
    if cg_id:
        key = f"coingecko:market:{cg_id}"
        cached = cache.get(key)
        if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
            market = cached
        else:
            market = {"provider": "coingecko_markets", "fetched_at": utcnow().isoformat()}
            allowed, reason = _provider_can_request(cfg, "coingecko_markets")
            if not allowed:
                market["error"] = f"provider_backoff:{reason}"
            else:
                try:
                    r = requests.get(
                        "https://api.coingecko.com/api/v3/coins/markets",
                        params={"vs_currency": "usd", "ids": cg_id, "price_change_percentage": "24h,7d"},
                        timeout=20,
                        headers=_market_headers("coingecko_markets"),
                    )
                    if r.status_code == 429:
                        _provider_record(cfg, "coingecko_markets", ok=False, status_code=429, error="rate_limited")
                        market["error"] = "rate_limited"
                    else:
                        r.raise_for_status()
                        rows = r.json()
                        market["data"] = rows[0] if rows else {}
                        _provider_record(cfg, "coingecko_markets", ok=True, status_code=r.status_code)
                except Exception as e:
                    _provider_record(cfg, "coingecko_markets", ok=False, error=type(e).__name__)
                    market["error"] = str(e)[:200]
            cache[key] = market
        row = market.get("data") or {}
        if row:
            chg24 = fnum(row.get("price_change_percentage_24h"))
            chg7d = fnum(row.get("price_change_percentage_7d_in_currency"))
            vol = fnum(row.get("total_volume"))
            mcap = fnum(row.get("market_cap"))
            vol_mcap = (vol / mcap) if mcap > 0 else 0.0
            out.update({"coingecko_id": cg_id, "cg_24h": chg24, "cg_7d": chg7d, "volume_to_mcap": vol_mcap})
            if chg24 > 0 and chg7d > 0:
                out["score"] += 1; out["reasons"].append("CoinGecko 24h and 7d trend positive")
            elif chg24 < -8 or chg7d < -15:
                out["score"] -= 1; out["reasons"].append("CoinGecko drawdown risk")
            if vol_mcap >= 0.08:
                out["score"] += 1; out["reasons"].append("healthy CoinGecko volume/market-cap")
            elif mcap > 0 and vol_mcap < 0.01:
                out["score"] -= 1; out["reasons"].append("thin CoinGecko volume/market-cap")
        elif market.get("error"):
            out["reasons"].append("CoinGecko unavailable; neutral")
    else:
        out["reasons"].append("no CoinGecko id configured; neutral")

    # CoinGecko trending: public endpoint, cached globally. Treat as a small boost only.
    trending = _get_json_cached(cfg, cache, "coingecko:trending", "https://api.coingecko.com/api/v3/search/trending", None, ttl, "coingecko_trending")
    try:
        trend_ids = {c.get("item", {}).get("id") for c in trending.get("data", {}).get("coins", [])}
        if cg_id and cg_id in trend_ids:
            out["score"] += 1
            out["reasons"].append("CoinGecko trending list")
    except Exception:
        pass


    # CoinMarketCap quote context if a key is configured. Symbol lookup can be ambiguous,
    # so it is a bounded +/-1 cross-check, never a standalone trade trigger.
    cmc_key = os.getenv("COINMARKETCAP_API_KEY")
    if cmc_key:
        symbol = _base_symbol(product_id).upper()
        cmc = _get_json_cached(
            cfg, cache, f"coinmarketcap:quotes:{symbol}",
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            {"symbol": symbol, "convert": "USD"}, ttl, "coinmarketcap_quotes"
        )
        try:
            data = (cmc.get("data") or {}).get(symbol)
            if isinstance(data, list):
                data = data[0] if data else {}
            quote = ((data or {}).get("quote") or {}).get("USD") or {}
            cmc_chg24 = fnum(quote.get("percent_change_24h"))
            cmc_vol = fnum(quote.get("volume_24h"))
            cmc_mcap = fnum(quote.get("market_cap"))
            cmc_vol_mcap = (cmc_vol / cmc_mcap) if cmc_mcap > 0 else 0.0
            if data:
                out["cmc_24h"] = cmc_chg24
                out["cmc_volume_to_mcap"] = cmc_vol_mcap
                if cmc_chg24 > 0 and cmc_vol_mcap >= 0.03:
                    out["score"] += 1; out["reasons"].append("CoinMarketCap confirms positive/liquid momentum")
                elif cmc_chg24 < -8 or (cmc_mcap > 0 and cmc_vol_mcap < 0.005):
                    out["score"] -= 1; out["reasons"].append("CoinMarketCap weak momentum/liquidity warning")
        except Exception:
            pass

    # Market-wide fear/greed is a bounded contrarian modifier, cached globally.
    # Extreme fear can create spot mean-reversion opportunities for already-qualified
    # technical entries, while extreme greed is still treated as chase risk.
    fng = _get_json_cached(cfg, cache, "alternative:fear_greed", "https://api.alternative.me/fng/", {"limit": 1, "format": "json"}, ttl, "alternative_fear_greed")
    try:
        fg = int((fng.get("data", {}).get("data") or [{}])[0].get("value", 50))
        out["fear_greed"] = fg
        fear_threshold = int(market_cfg.get("fear_extreme_threshold", 20))
        greed_threshold = int(market_cfg.get("greed_extreme_threshold", 80))
        fear_boost = int(market_cfg.get("fear_contrarian_boost", 1))
        greed_penalty = int(market_cfg.get("greed_chase_penalty", 1))
        if fg <= fear_threshold:
            out["score"] += fear_boost; out["reasons"].append(f"market extreme fear {fg}; contrarian spot boost")
        elif 45 <= fg <= 75:
            out["reasons"].append(f"market sentiment acceptable {fg}")
        elif fg >= greed_threshold:
            out["score"] -= greed_penalty; out["reasons"].append(f"market extreme greed {fg}; chase risk")
    except Exception:
        pass
    out["score"] = max(-2, min(2, int(out["score"])))
    if not out["reasons"]:
        out["reasons"].append("market context neutral")
    return out

def fetch_news_context(cfg: dict[str, Any], product_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    news_cfg = cfg.get("news_context", {})
    if not news_cfg.get("enabled", False):
        return {"score": 0, "reasons": ["news disabled"], "risk_block": False, "provider": "disabled"}
    ttl = int(news_cfg.get("cache_ttl_seconds", 3600))
    key = f"news:{product_id}"
    cached = cache.get(key)
    if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
        return {**cached, "cached": True}
    meta = _token_meta(cfg, product_id)
    symbol = _base_symbol(product_id)
    base_terms = list(meta.get("news_terms") or [symbol, meta.get("name", "")])
    narrative_terms = [str(t) for t in (meta.get("narrative_terms") or []) if str(t).strip()]
    terms = base_terms + narrative_terms[:8]
    query = " OR ".join(t for t in terms if t)
    if not query:
        query = symbol
    url = "https://news.google.com/rss/search"
    params = {"q": f"({query}) crypto cryptocurrency", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    out = {"score": 0, "reasons": [], "risk_block": False, "provider": "google_news_rss", "items": []}
    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "SerpentXHermesBot/1.0"})
        r.raise_for_status()
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", r.text, flags=re.S)
        parsed = []
        for a, b in titles[1:8]:  # skip feed title, keep small for quota/noise
            title = re.sub(r"\s+", " ", (a or b)).strip()
            if title:
                parsed.append(title)
        product_positive_words = set(POSITIVE_CONTEXT_WORDS)
        product_positive_words.update(str(t).lower() for t in (meta.get("narrative_terms") or []) if str(t).strip())
        score, pos, neg = _score_text_items(parsed, product_positive_words, NEGATIVE_CONTEXT_WORDS)
        out.update({"score": max(-2, min(2, score)), "items": parsed[:5], "positive_hits": pos, "negative_hits": neg})
        if neg:
            out["reasons"].append("negative news keywords: " + ", ".join(neg[:5]))
        if pos:
            out["reasons"].append("positive news keywords: " + ", ".join(pos[:5]))
        if out["score"] <= int(news_cfg.get("block_if_score_lte", -2)):
            out["risk_block"] = True
            out["reasons"].append("news risk block")
        if not out["reasons"]:
            out["reasons"].append("no material news keywords")
    except Exception as e:
        out.update({"score": 0, "error": str(e)[:200], "reasons": ["news unavailable; neutral"]})
    out["fetched_at"] = utcnow().isoformat()
    cache[key] = out
    return out


def social_context(cfg: dict[str, Any], product_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    soc_cfg = cfg.get("social_context", {})
    if not soc_cfg.get("enabled", False):
        return {"score": 0, "reasons": ["social disabled"], "risk_block": False, "provider": "disabled"}
    ttl = int(soc_cfg.get("cache_ttl_seconds", 3600))
    key = f"social:{product_id}"
    cached = cache.get(key)
    if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
        return {**cached, "cached": True}
    meta = _token_meta(cfg, product_id)
    symbol = _base_symbol(product_id)
    texts = list(meta.get("social_notes", []))
    provider = "manual_reddit_public"
    try:
        subreddits = soc_cfg.get("reddit_subreddits", "CryptoCurrency+CryptoMarkets+altcoin")
        query_terms = meta.get("social_terms") or [symbol, meta.get("name", "")]
        q = " OR ".join(t for t in query_terms if t) or symbol
        url = f"https://www.reddit.com/r/{subreddits}/search.json"
        params = {"q": q, "restrict_sr": "on", "sort": "new", "t": "day", "limit": int(soc_cfg.get("reddit_limit", 8))}
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "SerpentXHermesBot/1.0"})
        if r.status_code < 400:
            posts = r.json().get("data", {}).get("children", [])
            for post in posts:
                d = post.get("data", {})
                title = d.get("title")
                if title:
                    texts.append(title)
        else:
            provider += f"_reddit_status_{r.status_code}"
    except Exception as e:
        provider += "_reddit_unavailable"
        texts.append(f"reddit unavailable neutral {str(e)[:60]}")
    score, pos, neg = _score_text_items(texts, SOCIAL_POSITIVE_WORDS, SOCIAL_NEGATIVE_WORDS, strong_negative=-1, mild_negative=-1, mild_positive=1, strong_positive=2)
    out = {"score": max(-1, min(2, score)), "reasons": [], "risk_block": False, "provider": provider, "items": texts[:8], "positive_hits": pos, "negative_hits": neg, "fetched_at": utcnow().isoformat()}
    if neg:
        out["reasons"].append("negative social keywords: " + ", ".join(neg[:5]))
    if pos:
        out["reasons"].append("positive social keywords: " + ", ".join(pos[:5]))
    if len(texts) >= int(soc_cfg.get("reddit_activity_boost_min_posts", 5)) and not neg:
        out["score"] = max(out["score"], 1)
        out["reasons"].append("public Reddit activity detected")
    if not out["reasons"]:
        out["reasons"].append("social context neutral")
    cache[key] = out
    return out


def whale_context(cfg: dict[str, Any], product_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    whale_cfg = cfg.get("whale_context", {})
    if not whale_cfg.get("enabled", False):
        return {"score": 0, "reasons": ["whale disabled"], "risk_block": False, "provider": "disabled"}
    ttl = int(whale_cfg.get("cache_ttl_seconds", 3600))
    key = f"whale:{product_id}"
    cached = cache.get(key)
    if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
        return {**cached, "cached": True}
    meta = _token_meta(cfg, product_id)
    mint = meta.get("solana_mint")
    out = {"score": 0, "reasons": [], "risk_block": False, "provider": "helius_rpc", "fetched_at": utcnow().isoformat()}
    if not mint:
        out.update({"provider": "not_applicable", "reasons": ["no Solana mint configured for this product; neutral"]})
        cache[key] = out
        return out
    endpoint = (
        os.getenv("ALCHEMY_SOLANA_RPC_URL")
        or os.getenv("SOLANA_RPC_URL")
        or os.getenv("HELIUS_RPC_URL")
        or os.getenv("HELIUS_GATEKEEPER_RPC_URL")
        or os.getenv("QUICKNODE_SOLANA_RPC_URL")
    )
    if os.getenv("ALCHEMY_SOLANA_RPC_URL") or "alchemy" in str(os.getenv("SOLANA_RPC_URL") or "").lower():
        provider = "alchemy_solana_rpc"
    elif os.getenv("HELIUS_RPC_URL") or os.getenv("HELIUS_GATEKEEPER_RPC_URL"):
        provider = "helius_rpc"
    elif os.getenv("QUICKNODE_SOLANA_RPC_URL"):
        provider = "quicknode_rpc"
    else:
        provider = "solana_rpc"
    api_key = os.getenv("HELIUS_API_KEY")
    if not endpoint and api_key:
        endpoint = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        provider = "helius_rpc"
    if not endpoint:
        out.update({"provider": "solana_rpc_missing", "reasons": ["Solana RPC URL/API key not configured; neutral"]})
        cache[key] = out
        return out
    try:
        allowed, reason = _provider_can_request(cfg, provider)
        if not allowed:
            out.update({"provider": provider, "score": 0, "error": f"provider_backoff:{reason}", "reasons": ["whale data provider cooling down; neutral"]})
            cache[key] = out
            return out
        body = {"jsonrpc": "2.0", "id": "serpentx-context", "method": "getSignaturesForAddress", "params": [mint, {"limit": int(whale_cfg.get("helius_signature_limit", 20))}]}
        r = requests.post(endpoint, json=body, timeout=20)
        if r.status_code == 429:
            _provider_record(cfg, provider, ok=False, status_code=429, error="rate_limited")
            out.update({"provider": provider, "score": 0, "error": "rate_limited", "reasons": ["whale data rate-limited; neutral"]})
        else:
            r.raise_for_status()
            data = r.json()
            _provider_record(cfg, provider, ok=True, status_code=r.status_code)
            sigs = data.get("result") or []
            recent_errs = sum(1 for x in sigs if x.get("err"))
            # Conservative: token-account activity alone is not proof of whale accumulation/distribution.
            out.update({"provider": provider, "score": 0, "recent_signature_count": len(sigs), "recent_error_count": recent_errs, "reasons": ["Solana mint activity checked; no exchange-flow classifier yet"]})
    except Exception as e:
        _provider_record(cfg, provider, ok=False, error=type(e).__name__)
        out.update({"score": 0, "error": str(e)[:200], "reasons": ["whale data unavailable; neutral"]})
    cache[key] = out
    return out


def apply_context_scores(cfg: dict[str, Any], sig: "Signal", cache: dict[str, Any]) -> "Signal":
    if not cfg.get("external_context_enabled", False):
        sig.final_score = sig.score
        sig.context = {"enabled": False, "total_context_score": 0, "risk_block": False, "reasons": ["external context disabled"]}
        return sig
    news = fetch_news_context(cfg, sig.product_id, cache)
    market = fetch_market_context(cfg, sig.product_id, cache)
    social = social_context(cfg, sig.product_id, cache)
    whale = whale_context(cfg, sig.product_id, cache)
    context_total = int(news.get("score", 0)) + int(market.get("score", 0)) + int(social.get("score", 0)) + int(whale.get("score", 0))
    existing_risk_block = bool(sig.risk_block)
    risk_block = bool(existing_risk_block or news.get("risk_block") or market.get("risk_block") or social.get("risk_block") or whale.get("risk_block"))
    # Overheated/hype penalty: don't chase very extended daily moves when external context is weak/noisy.
    risk_penalty = 0
    penalty_reasons = []
    if sig.change_24h > float(cfg.get("context_overheated_24h_pct", 18)) and context_total <= 0:
        risk_penalty -= 1
        penalty_reasons.append("overheated 24h move without positive context")
    sig.context_score = context_total + risk_penalty
    sig.final_score = sig.score + sig.context_score
    sig.risk_block = risk_block
    sig.context = {"enabled": True, "news": news, "market": market, "social": social, "whale": whale, "total_context_score": context_total, "risk_penalty": risk_penalty, "risk_block": risk_block, "reasons": penalty_reasons}
    if risk_block:
        sig.action = "BLOCKED_CONTEXT"
    elif sig.final_score >= int(cfg.get("final_score_threshold", cfg.get("score_threshold", 4))):
        sig.action = "BUY"
    else:
        sig.action = "HOLD_USDC"
    return sig

@dataclass
class Signal:
    product_id: str
    price: float
    score: int
    action: str
    reasons: list[str]
    rsi: float
    change_24h: float
    quote_volume: float
    final_score: int = 0
    context_score: int = 0
    risk_block: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    avg_exec_range_pct: float = 0.0
    five_minute_confirmed: bool | None = None
    five_minute_reason: str | None = None
    fee_guard: dict[str, Any] = field(default_factory=dict)
    rsi_divergence: dict[str, Any] = field(default_factory=dict)
    rsi_divergence_label: str = "None"
    rsi_divergence_signal: str = "none"


def _avg_range_pct(candles: list[dict[str, Any]], limit: int = 20) -> float:
    ranges = []
    for c in candles[-limit:]:
        close = fnum(c.get("close"))
        high = fnum(c.get("high"))
        low = fnum(c.get("low"))
        if close > 0 and high > 0 and low > 0:
            ranges.append((high - low) / close)
    return sum(ranges) / len(ranges) if ranges else 0.0


def _apply_five_minute_confirmation(cfg: dict[str, Any], sig: Signal) -> Signal:
    if not cfg.get("five_minute_confirmation_enabled", False):
        return sig
    lookback = int(cfg.get("five_minute_confirmation_lookback_hours", 8))
    min_closes = int(cfg.get("five_minute_confirmation_min_candles", 24))
    candles = fetch_candles(sig.product_id, "FIVE_MINUTE", lookback)
    closes = [fnum(c.get("close")) for c in candles if fnum(c.get("close")) > 0]
    if len(closes) < min_closes:
        sig.five_minute_confirmed = False
        sig.five_minute_reason = "insufficient 5m candle history"
        sig.risk_block = True
        sig.action = "HOLD_USDC"
        sig.reasons.append(sig.five_minute_reason)
        return sig
    ema9_5m = ema(closes[-60:], 9)
    ema21_5m = ema(closes[-80:], 21)
    latest = closes[-1]
    prior = closes[-2] if len(closes) >= 2 else latest
    recent_pullback = min(closes[-6:]) <= ema9_5m if len(closes) >= 6 else prior <= ema9_5m
    reclaim_ema9 = latest >= ema9_5m and prior <= latest
    if latest >= ema21_5m and ema9_5m >= ema21_5m and reclaim_ema9 and (recent_pullback or not cfg.get("five_minute_reclaim_ema9_enabled", False)):
        sig.five_minute_confirmed = True
        sig.five_minute_reason = "5m reclaimed EMA9 after pullback; bullish/steady"
        sig.reasons.append(sig.five_minute_reason)
    else:
        sig.five_minute_confirmed = False
        sig.five_minute_reason = "5m EMA9 reclaim trigger failed; avoiding breakout-pop entry"
        sig.risk_block = True
        sig.action = "HOLD_USDC"
        sig.reasons.append(sig.five_minute_reason)
    return sig


def _apply_fee_guard(cfg: dict[str, Any], sig: Signal) -> Signal:
    if not cfg.get("fee_aware_entry_enabled", False):
        return sig
    roundtrip = float(cfg.get("fee_tracking", {}).get("estimated_roundtrip_fee_pct", cfg.get("estimated_roundtrip_fee_pct", 0.0)))
    min_net = float(cfg.get("minimum_net_edge_pct", 0.0))
    required = roundtrip + min_net
    target = float(cfg.get("take_profit_pct", 0.0))
    range_mult = float(cfg.get("expected_move_range_multiplier", 4.0))
    volatility_estimate = sig.avg_exec_range_pct * range_mult
    expected_move = max(target, volatility_estimate)
    sig.fee_guard = {
        "estimated_roundtrip_fee_pct": roundtrip,
        "minimum_net_edge_pct": min_net,
        "required_gross_move_pct": required,
        "take_profit_pct": target,
        "avg_exec_range_pct": sig.avg_exec_range_pct,
        "expected_move_pct": expected_move,
    }
    if expected_move < required or target < required:
        sig.risk_block = True
        sig.action = "HOLD_USDC"
        sig.reasons.append(f"fee guard blocked: expected/target move below {required*100:.2f}% gross requirement")
    else:
        sig.reasons.append(f"fee guard ok: target {target*100:.2f}% vs gross requirement {required*100:.2f}%")
    return sig


def score_product(cfg: dict[str, Any], product_id: str) -> Signal:
    info = product_info(product_id)
    if not is_market_orderable_product(info):
        s = Signal(product_id, fnum(info.get("price")), 0, "HOLD_USDC", ["product not market-orderable"], 50, fnum(info.get("price_percentage_change_24h")), fnum(info.get("volume_24h")))
        s.risk_block = True
        return s
    exec_c = fetch_candles(product_id, cfg["bar_exec"], int(cfg["lookback_exec_hours"]))
    trend_c = fetch_candles(product_id, cfg["bar_trend"], int(cfg["lookback_trend_hours"]))
    closes = [fnum(c.get("close")) for c in exec_c if fnum(c.get("close")) > 0]
    trend_closes = [fnum(c.get("close")) for c in trend_c if fnum(c.get("close")) > 0]
    price = fnum(info.get("price"), closes[-1] if closes else 0)
    avg_exec_range = _avg_range_pct(exec_c)
    if len(closes) < 30 or len(trend_closes) < 20 or price <= 0:
        return Signal(product_id, price, 0, "HOLD", ["insufficient candle history"], 50, fnum(info.get("price_percentage_change_24h")), fnum(info.get("volume_24h")), avg_exec_range_pct=avg_exec_range)
    ema9 = ema(closes[-60:], 9)
    ema21 = ema(closes[-80:], 21)
    ema50 = ema(closes[-120:], 50)
    trend20 = ema(trend_closes[-80:], 20)
    trend50 = ema(trend_closes[-120:], 50)
    cur_rsi = rsi(closes, 14)
    divergence = rsi_divergence(
        exec_c,
        period=int(cfg.get("rsi_divergence_period", 14)),
        swing_window=int(cfg.get("rsi_divergence_swing_window", 2)),
        lookback=int(cfg.get("rsi_divergence_lookback_candles", 80)),
    )
    chg = fnum(info.get("price_percentage_change_24h"))
    vol = fnum(info.get("volume_24h"))
    score = 0
    reasons = []

    # Spot-only: buy strength on trend alignment or controlled pullbacks; otherwise hold USDC.
    if ema9 > ema21 > ema50:
        score += 2; reasons.append("15m EMA stack bullish")
    if cfg.get("thirty_minute_regime_gate_enabled", cfg.get("one_hour_regime_gate_enabled", False)):
        if price > trend50 and trend20 > trend50:
            score += 1; reasons.append("30m regime bullish: price>EMA50 and EMA20>EMA50")
        else:
            reasons.append("30m regime gate failed")
    elif price > trend50:
        score += 1; reasons.append("above 30m EMA50")
    if 42 <= cur_rsi <= 68:
        score += 1; reasons.append(f"RSI supportive {cur_rsi:.1f}")
    elif cur_rsi < 35 and price > trend50:
        score += 1; reasons.append(f"trend pullback RSI {cur_rsi:.1f}")
    div_signal = divergence.get("signal", "none")
    div_label = divergence.get("label", "None")
    if div_signal == "bullish":
        score += int(cfg.get("rsi_bullish_divergence_score", 1))
        reasons.append(div_label)
    elif div_signal == "bearish":
        score -= int(cfg.get("rsi_bearish_divergence_penalty", 1))
        reasons.append(div_label)
    if chg > 0:
        score += 1; reasons.append(f"24h green {chg:.2f}%")
    if vol > 0:
        score += 1; reasons.append("active volume")
    if avg_exec_range >= float(cfg.get("min_avg_exec_range_pct", 0.0)):
        reasons.append(f"avg exec range {avg_exec_range*100:.2f}%")
    htf = cfg.get("higher_timeframe_regime_gate", {})
    if htf.get("enabled"):
        try:
            htf_c = fetch_candles(product_id, str(htf.get("granularity", "ONE_DAY")), int(htf.get("lookback_hours", 720)))
            htf_closes = [fnum(c.get("close")) for c in htf_c if fnum(c.get("close")) > 0]
            htf20 = ema(htf_closes[-80:], 20)
            htf50 = ema(htf_closes[-120:], 50)
            htf_price = htf_closes[-1] if htf_closes else price
            htf_ok = len(htf_closes) >= 50
            if htf.get("require_price_above_ema50", True):
                htf_ok = htf_ok and htf_price > htf50
            if htf.get("require_ema20_above_ema50", True):
                htf_ok = htf_ok and htf20 > htf50
            if htf_ok:
                reasons.append(f"{htf.get('granularity', 'ONE_DAY')} regime bullish")
            else:
                reasons.append(f"{htf.get('granularity', 'ONE_DAY')} regime gate failed")
                score -= 2
        except Exception as exc:
            reasons.append(f"higher timeframe gate unavailable: {str(exc)[:80]}")
    regime_block = any("regime gate failed" in r for r in reasons)
    action = "BUY" if score >= int(cfg["score_threshold"]) and not regime_block else "HOLD_USDC"
    sig = Signal(product_id, price, score, action, reasons, cur_rsi, chg, vol, avg_exec_range_pct=avg_exec_range)
    if regime_block:
        sig.risk_block = True
        sig.reasons.append("regime gate blocked long entry")
    sig.rsi_divergence = divergence
    sig.rsi_divergence_label = div_label
    sig.rsi_divergence_signal = div_signal
    if sig.action == "BUY":
        sig = _apply_five_minute_confirmation(cfg, sig)
        sig = _apply_fee_guard(cfg, sig)
    return sig


def get_accounts() -> dict[str, Any]:
    # Request a larger page and keep query params out of the JWT URI. Coinbase
    # rejects tokens signed with the query string included in the uri claim.
    return private_request("GET", "/api/v3/brokerage/accounts", params={"limit": 250})


def balance_map(accounts: dict[str, Any]) -> dict[str, float]:
    out = {}
    for acct in accounts.get("accounts", []):
        cur = acct.get("currency")
        bal = acct.get("available_balance", {}).get("value")
        if cur:
            out[cur] = out.get(cur, 0.0) + fnum(bal)
    return out


def _decimal_from(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Invalid order size: {value!r}") from exc


def _format_decimal(d: Decimal) -> str:
    # Coinbase rejects scientific notation and excess trailing zeros in order sizes.
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def quantize_order_size(product_id: str, field: str, size: float | str | Decimal) -> str:
    """Floor order size to Coinbase product increment to avoid precision rejects.

    Coinbase product metadata defines base_increment/quote_increment per market.
    Using a blanket round(..., 8) can over-specify products like NEAR-USDC
    (base_increment=0.001), causing PREVIEW_INVALID_SIZE_PRECISION on sells.
    """
    info = product_metadata(product_id)
    inc_key = "quote_increment" if field == "quote_size" else "base_increment"
    min_key = "quote_min_size" if field == "quote_size" else "base_min_size"
    inc = _decimal_from(info.get(inc_key) or "0.00000001")
    raw = _decimal_from(size)
    if raw <= 0:
        return "0"
    if inc <= 0:
        return _format_decimal(raw)
    quantized = (raw / inc).to_integral_value(rounding=ROUND_DOWN) * inc
    min_size = _decimal_from(info.get(min_key) or "0")
    if min_size > 0 and quantized < min_size:
        return "0"
    return _format_decimal(quantized)


def market_order_config(product_id: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
    info = product_metadata(product_id)
    if not is_market_orderable_product(info):
        raise RuntimeError(f"{product_id} is not market-orderable")
    if quote_size is not None:
        q = quantize_order_size(product_id, "quote_size", quote_size)
        if _decimal_from(q) <= 0:
            raise RuntimeError(f"{product_id} quote_size is below Coinbase minimum after quantization")
        return {"market_market_ioc": {"quote_size": q}}
    if base_size is not None:
        b = quantize_order_size(product_id, "base_size", base_size)
        if _decimal_from(b) <= 0:
            raise RuntimeError(f"{product_id} base_size is below Coinbase minimum after quantization")
        return {"market_market_ioc": {"base_size": b}}
    raise RuntimeError("quote_size or base_size required")


def preview_market(product_id: str, side: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
    oc = market_order_config(product_id, quote_size=quote_size, base_size=base_size)
    return private_request("POST", "/api/v3/brokerage/orders/preview", {"product_id": product_id, "side": side.upper(), "order_configuration": oc})


def place_market(product_id: str, side: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
    oc = market_order_config(product_id, quote_size=quote_size, base_size=base_size)
    body = {"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": side.upper(), "order_configuration": oc}
    return private_request("POST", "/api/v3/brokerage/orders", body)


def _quantize_price(product_id: str, price: float | str | Decimal) -> str:
    info = product_metadata(product_id)
    inc = _decimal_from(info.get("quote_increment") or "0.00000001")
    raw = _decimal_from(price)
    if raw <= 0:
        return "0"
    if inc <= 0:
        return _format_decimal(raw)
    return _format_decimal((raw / inc).to_integral_value(rounding=ROUND_DOWN) * inc)


def maker_limit_price(cfg: dict[str, Any], product_id: str, side: str, reference_price: float | None = None) -> str:
    """Return a conservative post-only limit price that should rest, not cross."""
    ref = fnum(reference_price) or fnum(product_info(product_id).get("price"))
    if ref <= 0:
        raise RuntimeError(f"No reference price for maker limit {product_id}")
    offset = float(cfg.get("maker_limit_price_offset_pct", 0.001))
    if side.upper() == "BUY":
        px = ref * (1 - offset)
    else:
        px = ref * (1 + offset)
    q = _quantize_price(product_id, px)
    if _decimal_from(q) <= 0:
        raise RuntimeError(f"{product_id} limit price quantized to zero")
    return q


def limit_order_config(product_id: str, side: str, limit_price: float | str | Decimal, quote_size: float | None = None, base_size: float | None = None, post_only: bool = True) -> dict[str, Any]:
    """Coinbase Advanced Trade limit GTC config.

    Limit GTC requires base_size, so BUY quote_size is converted at limit_price
    and floored to the product base increment. post_only=true avoids taker fills;
    if Coinbase rejects a crossing order we treat it as no trade instead of
    silently paying taker fees.
    """
    price_s = _quantize_price(product_id, limit_price)
    price_d = _decimal_from(price_s)
    if price_d <= 0:
        raise RuntimeError(f"{product_id} invalid limit price {limit_price!r}")
    if base_size is None:
        if quote_size is None:
            raise RuntimeError("quote_size or base_size required")
        base_size = float(_decimal_from(str(quote_size)) / price_d)
    b = quantize_order_size(product_id, "base_size", base_size)
    if _decimal_from(b) <= 0:
        raise RuntimeError(f"{product_id} base_size is below Coinbase minimum after quantization")
    return {"limit_limit_gtc": {"base_size": b, "limit_price": price_s, "post_only": bool(post_only)}}


def preview_limit(product_id: str, side: str, limit_price: float | str | Decimal, quote_size: float | None = None, base_size: float | None = None, post_only: bool = True) -> dict[str, Any]:
    oc = limit_order_config(product_id, side, limit_price, quote_size=quote_size, base_size=base_size, post_only=post_only)
    return private_request("POST", "/api/v3/brokerage/orders/preview", {"product_id": product_id, "side": side.upper(), "order_configuration": oc})


def place_limit(product_id: str, side: str, limit_price: float | str | Decimal, quote_size: float | None = None, base_size: float | None = None, post_only: bool = True) -> dict[str, Any]:
    oc = limit_order_config(product_id, side, limit_price, quote_size=quote_size, base_size=base_size, post_only=post_only)
    body = {"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": side.upper(), "order_configuration": oc}
    return private_request("POST", "/api/v3/brokerage/orders", body)


def order_id_from_response(order: dict[str, Any]) -> str:
    return str(((order.get("success_response") or {}).get("order_id") or order.get("order_id") or ""))


def fetch_order_detail(order_id: str) -> dict[str, Any]:
    detail = private_request("GET", f"/api/v3/brokerage/orders/historical/{order_id}")
    return detail.get("order") or detail


def cancel_order(order_id: str) -> dict[str, Any]:
    return private_request("POST", "/api/v3/brokerage/orders/batch_cancel", {"order_ids": [order_id]})


def normalize_pending_orders(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("pending_orders")
    if not isinstance(rows, list):
        state["pending_orders"] = []
        return []
    clean = [r for r in rows if isinstance(r, dict) and r.get("order_id") and r.get("product_id")]
    if clean != rows:
        state["pending_orders"] = clean
    return clean


def pending_order_active(state: dict[str, Any], product_id: str | None = None, side: str | None = None) -> bool:
    for row in normalize_pending_orders(state):
        if product_id and row.get("product_id") != product_id:
            continue
        if side and str(row.get("side", "")).upper() != side.upper():
            continue
        return True
    return False


def _parse_iso(ts: str) -> datetime | None:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        out = datetime.fromisoformat(ts)
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def reconcile_pending_orders(cfg: dict[str, Any], state: dict[str, Any], balances: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Refresh pending limit orders and apply filled orders to local position state."""
    pending = normalize_pending_orders(state)
    if not pending:
        return []
    remaining: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    positions = normalize_positions(state)
    timeout_min = float(cfg.get("maker_limit_pending_timeout_minutes", 90))
    for row in pending:
        event = {"pending_order": row, "ts": utcnow().isoformat()}
        try:
            detail = fetch_order_detail(str(row["order_id"]))
            event["order_detail"] = detail
        except Exception as exc:
            row["last_reconcile_error"] = str(exc)[:240]
            remaining.append(row)
            events.append({**event, "decision": "PENDING_RECONCILE_ERROR"})
            continue
        status = str(detail.get("status") or "").upper()
        filled_size = fnum(detail.get("filled_size"))
        avg_price = fnum(detail.get("average_filled_price")) or fnum(row.get("limit_price"))
        side = str(row.get("side", detail.get("side", ""))).upper()
        product_id = str(row.get("product_id") or detail.get("product_id"))
        event.update({"status": status, "filled_size": filled_size, "avg_price": avg_price})
        if status == "FILLED" and filled_size > 0 and avg_price > 0:
            if side == "BUY":
                if not any(p.get("entry_order_id") == row["order_id"] for p in positions):
                    positions.append({
                        "product_id": product_id,
                        "entry_price": avg_price,
                        "quote_size": fnum(detail.get("total_value_after_fees")) or fnum(detail.get("filled_value")) or fnum(row.get("quote_size")),
                        "base_size_est": filled_size,
                        "opened_at": detail.get("last_fill_time") or detail.get("created_time") or event["ts"],
                        "high_water_price": avg_price,
                        "high_water_pnl_pct": 0.0,
                        "entry_candle_low": fnum(row.get("entry_candle_low")) or avg_price,
                        "entry_order_id": row["order_id"],
                        "entry_order_type": "LIMIT_POST_ONLY",
                    })
                state["cooldown_until"] = (utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                append_jsonl(Path(cfg["trades_log_path"]), {"ts": event["ts"], "side": "BUY", "order": {"success": True, "success_response": {"order_id": row["order_id"], "product_id": product_id, "side": "BUY"}}, "order_detail": detail, "signal": row.get("signal", {}), "quote_size": row.get("quote_size"), "source": "maker_limit_reconcile"})
                event["decision"] = "PENDING_BUY_FILLED_POSITION_OPENED"
            elif side == "SELL":
                reason = row.get("reason") or "LIMIT_EXIT_FILLED"
                scale_exit_reason = str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT"))
                if reason == scale_exit_reason:
                    updated_positions = []
                    for p in positions:
                        if p.get("product_id") == product_id:
                            updated = apply_partial_exit_fill(p, filled_size, event["ts"])
                            if updated:
                                updated_positions.append(updated)
                        else:
                            updated_positions.append(p)
                    positions = updated_positions
                    append_jsonl(Path(cfg["trades_log_path"]), {"ts": event["ts"], "side": "SELL", "reason": reason, "order": {"success": True, "success_response": {"order_id": row["order_id"], "product_id": product_id, "side": "SELL"}}, "order_detail": detail, "position": row.get("position", {}), "base_size": filled_size, "source": "maker_limit_reconcile"})
                    event["decision"] = "PENDING_PARTIAL_SELL_FILLED_POSITION_REDUCED"
                else:
                    positions = [p for p in positions if p.get("product_id") != product_id]
                    if reason in {"STOP_LOSS", "SOFT_INVALIDATION", "TRAILING_STOP", "BREAKEVEN_LOCK"}:
                        set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_stop", 720)))
                    else:
                        set_product_cooldown(state, product_id, float(cfg.get("per_product_cooldown_minutes_after_trade", 180)))
                    append_jsonl(Path(cfg["trades_log_path"]), {"ts": event["ts"], "side": "SELL", "reason": reason, "order": {"success": True, "success_response": {"order_id": row["order_id"], "product_id": product_id, "side": "SELL"}}, "order_detail": detail, "position": row.get("position", {}), "source": "maker_limit_reconcile"})
                    event["decision"] = "PENDING_SELL_FILLED_POSITION_CLOSED"
            events.append(event)
            continue
        if status in {"CANCELLED", "EXPIRED", "FAILED", "REJECTED"}:
            event["decision"] = f"PENDING_{status}"
            events.append(event)
            continue
        created = _parse_iso(str(row.get("placed_at") or detail.get("created_time") or ""))
        age_min = ((utcnow() - created).total_seconds() / 60) if created else 0
        if timeout_min > 0 and age_min > timeout_min:
            try:
                event["cancel"] = cancel_order(str(row["order_id"]))
                event["decision"] = "PENDING_CANCEL_REQUESTED_TIMEOUT"
            except Exception as exc:
                row["last_cancel_error"] = str(exc)[:240]
                remaining.append(row)
                event["decision"] = "PENDING_CANCEL_ERROR"
            events.append(event)
            continue
        row.update({"last_status": status, "last_reconciled_at": event["ts"], "filled_size": filled_size, "average_filled_price": avg_price})
        remaining.append(row)
        events.append({**event, "decision": "PENDING_STILL_OPEN"})
    state["pending_orders"] = remaining
    persist_positions(state, positions)
    if events:
        state["last_pending_order_events"] = events[-5:]
    return events


def should_use_maker_limit(cfg: dict[str, Any], side: str, reason: str | None = None) -> bool:
    if not cfg.get("maker_limit_enabled", False):
        return False
    if side.upper() == "SELL" and cfg.get("maker_limit_use_market_for_risk_exits", True):
        if reason in set(cfg.get("maker_limit_risk_exit_reasons", ["STOP_LOSS", "SOFT_INVALIDATION"])):
            return False
    return True


def count_daily_trades(path: Path, now: datetime | None = None) -> int:
    """Count locally logged ORDER_SENT events in the last 24h."""
    now = now or utcnow()
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(errors="ignore").splitlines()[-200:]:
        try:
            row = json.loads(raw)
            ts = datetime.fromisoformat(str(row.get("ts") or ""))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (now - ts).total_seconds() <= 86400 and row.get("order", {}).get("success") is True:
            count += 1
    return count


def dynamic_quote_size(cfg: dict[str, Any], quote_bal: float, top: Signal) -> float:
    """Risk-adjusted BUY size for a small target bankroll.

    Only the configured target bankroll participates in sizing, so extra USDC can
    sit in the account for separate experiments without increasing this bot's spot
    exposure. Within that target, stronger final scores can scale above the base
    percent cap, while threshold-only, thin-liquidity, or overheated names are kept
    at the minimum ticket size.
    """
    min_q = float(cfg.get("min_quote_per_trade", cfg.get("min_quote_balance_to_trade", 15)))
    max_q = float(cfg.get("max_quote_per_trade", 20))
    target_bal = float(cfg.get("sizing_quote_balance_target", cfg.get("trading_quote_balance_target", quote_bal)) or quote_bal)
    sizing_bal = max(0.0, min(quote_bal, target_bal))
    pct_cap = sizing_bal * float(cfg["max_account_quote_pct_per_trade"])
    effective_score = int(getattr(top, "final_score", 0) or top.score)
    threshold = int(cfg.get("final_score_threshold", cfg["score_threshold"]))
    score_edge = max(0, effective_score - threshold)
    step_multiplier = float(cfg.get("sizing_score_step_multiplier", 0.15))
    max_multiplier = float(cfg.get("sizing_max_score_multiplier", 1.35))
    multiplier = min(max_multiplier, 1.0 + (score_edge * step_multiplier))
    size = pct_cap * multiplier
    thin_volume = float(cfg.get("sizing_thin_volume_quote_usdc", 1_000_000))
    if effective_score <= threshold:
        size = min(size, min_q)
    if top.quote_volume < thin_volume:
        size = min(size, min_q)
    if top.change_24h > float(cfg.get("overheated_size_down_pct", 20)) or top.change_24h < -10:
        size = min(size, min_q)
    size = min(size, max_q, quote_bal)
    if size < min_q:
        return 0.0
    return max(0.0, math.floor(size * 100) / 100)


def live_gates_open(cfg: dict[str, Any], live: bool) -> str | None:
    if not live:
        return "PREVIEW_ONLY"
    if not cfg.get("active_trading"):
        return "LIVE_BLOCKED_CONFIG_ACTIVE_TRADING_FALSE"
    if os.getenv("COINBASE_TRADING_ENABLED") != "1":
        return "LIVE_BLOCKED_ENV_TRADING_DISABLED"
    return None


def normalize_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bot-managed positions, migrating legacy single-position state.

    State has historically moved from a single ``open_position`` object to an
    ``open_positions`` list.  Keep this normalizer defensive: drop malformed
    rows and collapse exact duplicate product/open-time entries so stale state
    cannot block new entries or cause duplicate exit attempts for the same
    bot-managed fill.
    """
    positions = state.get("open_positions")
    if isinstance(positions, list):
        cleaned: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for p in positions:
            if not isinstance(p, dict) or not p.get("product_id"):
                continue
            key = (
                str(p.get("product_id")),
                str(p.get("opened_at", "")),
                str(p.get("entry_price", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(p)
        if cleaned != positions:
            state["open_positions"] = cleaned
            state.pop("open_position", None)
        return cleaned
    legacy = state.get("open_position")
    if isinstance(legacy, dict) and legacy.get("product_id"):
        state["open_positions"] = [legacy]
        state.pop("open_position", None)
        return state["open_positions"]
    return []


def persist_positions(state: dict[str, Any], positions: list[dict[str, Any]]) -> None:
    state["open_positions"] = positions
    state.pop("open_position", None)


def product_cooldown_active(state: dict[str, Any], product_id: str, now: datetime | None = None) -> str | None:
    "Return cooldown-until ISO string when this product is temporarily blocked."
    now_s = (now or utcnow()).isoformat()
    cooldowns = state.get("product_cooldowns", {})
    if isinstance(cooldowns, dict):
        until = cooldowns.get(product_id)
        if isinstance(until, str) and until > now_s:
            return until
    return None


def set_product_cooldown(state: dict[str, Any], product_id: str, minutes: float) -> None:
    if not product_id or minutes <= 0:
        return
    cooldowns = state.setdefault("product_cooldowns", {})
    if isinstance(cooldowns, dict):
        cooldowns[product_id] = (utcnow() + timedelta(minutes=float(minutes))).isoformat()


def trailing_exit_reason(cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float) -> str | None:
    """Update high-water marks and return breakeven/trailing exit reasons.

    Fee-aware profile:
    - once price reaches +2.0% gross, lock a +1.2% gross floor
    - once price reaches +4.0% gross, trail by 2.0%
    """
    if not cfg.get("trailing_stop_enabled", False) or entry <= 0 or current <= 0:
        return None
    pnl_pct = (current - entry) / entry
    high = max(fnum(pos.get("high_water_price")), current)
    if high > fnum(pos.get("high_water_price")):
        pos["high_water_price"] = high
        pos["high_water_pnl_pct"] = (high - entry) / entry
    high_pnl = fnum(pos.get("high_water_pnl_pct"), pnl_pct)
    if cfg.get("breakeven_lock_enabled", False):
        be_activation = float(cfg.get("breakeven_activation_pct", 0.02))
        be_lock = float(cfg.get("breakeven_lock_pct", 0.012))
        if high_pnl >= be_activation and pnl_pct <= be_lock:
            return "BREAKEVEN_LOCK"
    activation = float(cfg.get("trailing_activation_pct", 0.04))
    drawdown = float(cfg.get("trailing_drawdown_pct", 0.02))
    if high_pnl >= activation and pnl_pct <= high_pnl - drawdown:
        return "TRAILING_STOP"
    return None


def structural_exit_reason(cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float) -> str | None:
    """Return SOFT_INVALIDATION for structural 15m breakdown or hard cap.

    This replaces a naked fixed soft-stop: exit if the latest 15m close loses
    the entry candle low or EMA21 while the trade is red, with a -2% hard cap.
    """
    if entry <= 0 or current <= 0:
        return None
    pnl_pct = (current - entry) / entry
    soft_cfg = cfg.get("soft_invalidation", {}) if isinstance(cfg.get("soft_invalidation"), dict) else {}
    hard_cap = float(soft_cfg.get("hard_cap_pct", cfg.get("soft_invalidation_pct", 0.02)))
    if pnl_pct <= -hard_cap:
        return "SOFT_INVALIDATION"
    if pnl_pct >= 0:
        return None
    try:
        candles = fetch_candles(str(pos.get("product_id")), cfg.get("bar_exec", "FIFTEEN_MINUTE"), 8)
        closes = [fnum(c.get("close")) for c in candles if fnum(c.get("close")) > 0]
        lows = [fnum(c.get("low")) for c in candles if fnum(c.get("low")) > 0]
        if len(closes) < 21:
            return None
        latest_close = closes[-1]
        entry_low = fnum(pos.get("entry_candle_low"))
        if not entry_low and lows:
            entry_low = min(lows[-4:])
        ema21 = ema(closes[-80:], 21)
        if soft_cfg.get("exit_on_15m_close_below_entry_candle_low", True) and entry_low > 0 and latest_close < entry_low:
            pos["last_structural_invalidation"] = {"latest_close": latest_close, "entry_candle_low": entry_low, "ema21": ema21}
            return "SOFT_INVALIDATION"
        if soft_cfg.get("exit_on_15m_loss_of_ema21", True) and ema21 > 0 and latest_close < ema21:
            pos["last_structural_invalidation"] = {"latest_close": latest_close, "entry_candle_low": entry_low, "ema21": ema21}
            return "SOFT_INVALIDATION"
    except Exception as exc:
        pos["last_structural_invalidation_error"] = str(exc)[:160]
    return None


def scale_exit_plan(cfg: dict[str, Any], pos: dict[str, Any], current: float, entry: float, base_size: float) -> tuple[str | None, float]:
    """Return a one-time partial take-profit exit when staged exits are enabled.

    The first scale exit sells a configured fraction at +N% and leaves the
    remaining position open for trailing/breakeven/soft/full-take-profit exits.
    """
    if not cfg.get("scale_exit_enabled", False) or entry <= 0 or current <= 0 or base_size <= 0:
        return None, base_size
    if pos.get("scale_exit_done") or pos.get("scale_exit_pending_order_id"):
        return None, base_size
    trigger = float(cfg.get("scale_exit_take_profit_pct", 0.04))
    pnl_pct = (current - entry) / entry
    if pnl_pct < trigger:
        return None, base_size
    fraction = max(0.0, min(float(cfg.get("scale_exit_fraction", 0.5)), 1.0))
    exit_size = base_size * fraction
    if exit_size <= 0 or exit_size >= base_size:
        return None, base_size
    return str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT")), exit_size


def apply_partial_exit_fill(pos: dict[str, Any], filled_size: float, ts: str | None = None) -> dict[str, Any] | None:
    """Reduce local position size after a filled staged/partial exit."""
    current_size = fnum(pos.get("base_size_est"))
    if current_size <= 0:
        return None
    remaining_size = max(0.0, current_size - max(0.0, filled_size))
    if remaining_size <= current_size * 0.05:
        return None
    updated = dict(pos)
    ratio = remaining_size / current_size
    updated["base_size_est"] = remaining_size
    if fnum(updated.get("quote_size")) > 0:
        updated["quote_size"] = fnum(updated.get("quote_size")) * ratio
    updated["scale_exit_done"] = True
    updated["scale_exit_filled_at"] = ts or utcnow().isoformat()
    updated.pop("scale_exit_pending_order_id", None)
    return updated


def choose_entry_signal(cfg: dict[str, Any], signals: list[Signal], positions: list[dict[str, Any]], state: dict[str, Any] | None = None) -> Signal | None:
    state = state or {}
    held = {str(p.get("product_id")) for p in positions}
    prevent_dup = bool(cfg.get("prevent_same_asset_duplicate", True))
    second_min = int(cfg.get("second_position_min_final_score", cfg.get("second_position_min_score", cfg.get("score_threshold", 4))))
    for sig in signals:
        if sig.action != "BUY" or sig.risk_block:
            continue
        if product_cooldown_active(state, sig.product_id):
            continue
        if prevent_dup and sig.product_id in held:
            continue
        effective_score = int(sig.final_score or sig.score)
        if positions and effective_score < second_min:
            continue
        if sig.change_24h >= float(cfg.get("overheated_require_context_pct", 50)):
            if sig.change_24h >= float(cfg.get("overheated_block_pct", 100)):
                continue
            if sig.context_score <= 0 or effective_score < int(cfg.get("min_final_score_when_overheated", 7)):
                continue
        return sig
    return None


def run(cfg: dict[str, Any], *, status: bool=False, live: bool=False) -> dict[str, Any]:
    load_dotenv(str(ROOT / "secrets" / "rpc_providers.env"))
    load_dotenv(cfg.get("env_file", ROOT / ".env"))
    context_cache = _load_context_cache(cfg)
    signals = [score_product(cfg, p) for p in cfg["allowed_products"]]
    signals = [apply_context_scores(cfg, s, context_cache) for s in signals]
    _save_context_cache(cfg, context_cache)
    signals.sort(key=lambda s: (s.final_score or s.score, s.score, s.change_24h, s.quote_volume), reverse=True)
    top = signals[0]
    state_path = Path(cfg["state_path"])
    state = load_json(state_path) if state_path.exists() else {}

    result: dict[str, Any] = {
        "ts": utcnow().isoformat(),
        "strategy": cfg["strategy_name"],
        "mode": cfg.get("mode", "shadow"),
        "env_present": env_present(),
        "signals": [s.__dict__ for s in signals],
        "top": top.__dict__,
        "decision": "SHADOW_ONLY",
    }

    balances = {}
    if env_present()["COINBASE_API_KEY_NAME"] and env_present()["COINBASE_API_PRIVATE_KEY"]:
        accounts = get_accounts()
        balances = balance_map(accounts)
        relevant = {"USDC", "USD"}
        for product_id in cfg.get("allowed_products", []):
            base, _, quote = str(product_id).partition("-")
            if base:
                relevant.add(base)
            if quote:
                relevant.add(quote)
        for pos in normalize_positions(state):
            base, _, quote = str(pos.get("product_id", "")).partition("-")
            if base:
                relevant.add(base)
            if quote:
                relevant.add(quote)
        result["balances"] = {k: v for k, v in balances.items() if k in relevant or v > 0}
    else:
        result["auth_status"] = "missing_coinbase_credentials"

    quote = cfg["quote_currency"]
    quote_bal = balances.get(quote, 0.0)
    cooldown_until = state.get("cooldown_until")
    in_cooldown = cooldown_until and cooldown_until > utcnow().isoformat()
    daily_trades = count_daily_trades(Path(cfg["trades_log_path"]))
    pending_events = reconcile_pending_orders(cfg, state, balances) if balances else []
    if pending_events:
        result["pending_order_events"] = pending_events
    positions = normalize_positions(state)
    pending_orders = normalize_pending_orders(state)
    max_positions = int(cfg.get("max_open_positions", 1))
    result["daily_trades_used"] = daily_trades
    result["open_positions"] = positions
    result["pending_orders"] = pending_orders

    if pending_orders and not status:
        result["decision"] = "PENDING_ORDER_OPEN"

    if status and positions:
        enriched_positions = []
        for pos in positions:
            pos_product = str(pos.get("product_id"))
            entry = fnum(pos.get("entry_price"))
            current = fnum(product_info(pos_product).get("price")) if pos_product else 0.0
            pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0
            enriched_positions.append({
                **pos,
                "current_price": current,
                "pnl_pct": pnl_pct,
                "take_profit_price": entry * (1 + float(cfg["take_profit_pct"])),
                "stop_loss_price": entry * (1 - float(cfg["stop_loss_pct"])),
                "soft_invalidation_price": entry * (1 - float(cfg["soft_invalidation_pct"])),
            })
        result["open_positions"] = enriched_positions

    # First priority: manage exits for existing bot-opened positions. Only one
    # live order is sent per run, so exits take precedence over new entries.
    if positions and not status and result["decision"] != "PENDING_ORDER_OPEN":
        updated_positions: list[dict[str, Any]] = []
        for pos in positions:
            pos_product = str(pos.get("product_id"))
            base, _, _ = pos_product.partition("-")
            entry = fnum(pos.get("entry_price"))
            current = fnum(product_info(pos_product).get("price")) if pos_product else 0.0
            base_size = min(fnum(pos.get("base_size_est")), balances.get(base, 0.0))
            pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0
            sell_reason = None
            exit_base_size = base_size
            scale_reason, scale_size = scale_exit_plan(cfg, pos, current, entry, base_size)
            if scale_reason:
                sell_reason = scale_reason
                exit_base_size = scale_size
            elif pnl_pct >= float(cfg["take_profit_pct"]):
                sell_reason = "TAKE_PROFIT"
            elif pnl_pct <= -float(cfg["stop_loss_pct"]):
                sell_reason = "STOP_LOSS"
            else:
                structural_reason = structural_exit_reason(cfg, pos, current, entry)
                if structural_reason:
                    sell_reason = structural_reason
                    try:
                        pos_signal = apply_context_scores(cfg, score_product(cfg, pos_product), context_cache)
                        result["position_signal"] = pos_signal.__dict__
                    except Exception as e:
                        result["position_signal_error"] = str(e)[:200]
            trailing_reason = trailing_exit_reason(cfg, pos, current, entry)
            if trailing_reason and not sell_reason:
                sell_reason = trailing_reason
            enriched = {**pos, "current_price": current, "pnl_pct": pnl_pct}
            if sell_reason and exit_base_size > 0:
                result["open_position"] = enriched
                use_limit = should_use_maker_limit(cfg, "SELL", sell_reason)
                limit_price = maker_limit_price(cfg, pos_product, "SELL", current) if use_limit else None
                result["proposed_order"] = {"product_id": pos_product, "side": "SELL", "base_size": exit_base_size, "reason": sell_reason, "order_type": "LIMIT_POST_ONLY" if use_limit else "MARKET_IOC", "limit_price": limit_price}
                result["preview"] = preview_limit(pos_product, "SELL", limit_price, base_size=exit_base_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else preview_market(pos_product, "SELL", base_size=exit_base_size)
                gate = live_gates_open(cfg, live)
                if gate:
                    result["decision"] = gate
                    updated_positions.append(pos)
                else:
                    order = place_limit(pos_product, "SELL", limit_price, base_size=exit_base_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else place_market(pos_product, "SELL", base_size=exit_base_size)
                    result["order"] = order
                    if order.get("success") is True:
                        oid = order_id_from_response(order)
                        if use_limit:
                            result["decision"] = "LIMIT_ORDER_PLACED"
                            pending_position = dict(pos)
                            if sell_reason == str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT")):
                                pending_position["scale_exit_pending_order_id"] = oid
                            state.setdefault("pending_orders", []).append({"order_id": oid, "product_id": pos_product, "side": "SELL", "reason": sell_reason, "base_size": exit_base_size, "limit_price": limit_price, "placed_at": result["ts"], "position": pending_position, "order_type": "LIMIT_POST_ONLY"})
                            updated_positions.append(pending_position if sell_reason == str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT")) else pos)
                        else:
                            scale_exit_reason = str(cfg.get("scale_exit_reason", "SCALE_TAKE_PROFIT"))
                            if sell_reason == scale_exit_reason:
                                result["decision"] = "PARTIAL_EXIT_SENT"
                                updated = apply_partial_exit_fill(pos, exit_base_size, result["ts"])
                                if updated:
                                    updated_positions = [updated if p is pos else p for p in positions]
                            else:
                                result["decision"] = "ORDER_SENT"
                                state["cooldown_until"] = (utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                                if sell_reason in {"STOP_LOSS", "SOFT_INVALIDATION", "TRAILING_STOP", "BREAKEVEN_LOCK"}:
                                    set_product_cooldown(state, pos_product, float(cfg.get("per_product_cooldown_minutes_after_stop", 720)))
                                else:
                                    set_product_cooldown(state, pos_product, float(cfg.get("per_product_cooldown_minutes_after_trade", 180)))
                                updated_positions = [p for p in positions if p is not pos]
                            append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "SELL", "reason": sell_reason, "order": order, "position": pos, "base_size": exit_base_size, "order_type": "MARKET_IOC"})
                    else:
                        result["decision"] = "ORDER_FAILED"
                        updated_positions.append(pos)
                        append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "SELL", "reason": sell_reason, "order": order, "position": pos, "failed": True})
                persist_positions(state, updated_positions)
                break
            updated_positions.append(pos)
        else:
            persist_positions(state, updated_positions)
            # No exits; continue below only if another entry slot is available.
            if len(updated_positions) >= max_positions:
                result["decision"] = "HOLD_POSITIONS"

    if result["decision"] in {"SHADOW_ONLY", "HOLD_POSITIONS"} and status:
        result["decision"] = "STATUS_ONLY"
    elif result["decision"] == "SHADOW_ONLY":
        positions = normalize_positions(state)
        entry_signal = choose_entry_signal(cfg, signals, positions, state)
        if not balances:
            result["decision"] = "NEEDS_CREDENTIALS_FOR_PREVIEW_OR_TRADE"
        elif in_cooldown:
            result["decision"] = "COOLDOWN"
            result["cooldown_until"] = cooldown_until
        elif daily_trades >= int(cfg.get("daily_max_trades", 2)):
            result["decision"] = "DAILY_TRADE_LIMIT_REACHED"
        elif len(positions) >= max_positions:
            result["decision"] = "HOLD_POSITIONS"
        elif entry_signal is None:
            result["decision"] = "NO_ELIGIBLE_ENTRY"
        elif quote_bal < float(cfg["min_quote_balance_to_trade"]):
            result["decision"] = "INSUFFICIENT_USDC"
        else:
            quote_size = dynamic_quote_size(cfg, quote_bal, entry_signal)
            result["entry_signal"] = entry_signal.__dict__
            use_limit = should_use_maker_limit(cfg, "BUY")
            limit_price = maker_limit_price(cfg, entry_signal.product_id, "BUY", entry_signal.price) if use_limit else None
            result["proposed_order"] = {"product_id": entry_signal.product_id, "side": "BUY", "quote_size": quote_size, "order_type": "LIMIT_POST_ONLY" if use_limit else "MARKET_IOC", "limit_price": limit_price}
            if quote_size < float(cfg["min_quote_balance_to_trade"]):
                result["decision"] = "ORDER_TOO_SMALL"
            else:
                preview = preview_limit(entry_signal.product_id, "BUY", limit_price, quote_size=quote_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else preview_market(entry_signal.product_id, "BUY", quote_size=quote_size)
                result["preview"] = preview
                gate = live_gates_open(cfg, live)
                if gate:
                    result["decision"] = gate
                else:
                    order = place_limit(entry_signal.product_id, "BUY", limit_price, quote_size=quote_size, post_only=bool(cfg.get("maker_limit_post_only", True))) if use_limit else place_market(entry_signal.product_id, "BUY", quote_size=quote_size)
                    result["order"] = order
                    if order.get("success") is True:
                        if use_limit:
                            oid = order_id_from_response(order)
                            entry_low = min([fnum(c.get("low")) for c in fetch_candles(entry_signal.product_id, cfg.get("bar_exec", "FIFTEEN_MINUTE"), 4)[-4:] if fnum(c.get("low")) > 0] or [entry_signal.price])
                            result["decision"] = "LIMIT_ORDER_PLACED"
                            state.setdefault("pending_orders", []).append({"order_id": oid, "product_id": entry_signal.product_id, "side": "BUY", "quote_size": quote_size, "limit_price": limit_price, "placed_at": result["ts"], "signal": entry_signal.__dict__, "entry_candle_low": entry_low, "order_type": "LIMIT_POST_ONLY"})
                        else:
                            result["decision"] = "ORDER_SENT"
                            state["cooldown_until"] = (utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                            positions.append({
                                "product_id": entry_signal.product_id,
                                "entry_price": entry_signal.price,
                                "quote_size": quote_size,
                                "base_size_est": quote_size / entry_signal.price if entry_signal.price > 0 else 0.0,
                                "opened_at": result["ts"],
                                "high_water_price": entry_signal.price,
                                "high_water_pnl_pct": 0.0,
                                "entry_candle_low": min([fnum(c.get("low")) for c in fetch_candles(entry_signal.product_id, cfg.get("bar_exec", "FIFTEEN_MINUTE"), 4)[-4:] if fnum(c.get("low")) > 0] or [entry_signal.price]),
                            })
                            persist_positions(state, positions)
                            append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "BUY", "order": order, "signal": entry_signal.__dict__, "quote_size": quote_size, "order_type": "MARKET_IOC"})
                    else:
                        result["decision"] = "ORDER_FAILED"
                        append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "BUY", "order": order, "signal": entry_signal.__dict__, "quote_size": quote_size, "failed": True})

    if not (status and result.get("open_positions")):
        result["open_positions"] = normalize_positions(state)
    result["pending_orders"] = normalize_pending_orders(state)
    state["last_run_at"] = result["ts"]
    state["last_decision"] = result["decision"]
    state["last_top"] = top.__dict__
    save_json(state_path, state)
    append_jsonl(Path(cfg["runs_log_path"]), result)
    return result


def summarize(result: dict[str, Any]) -> str:
    top = result["top"]
    parts = [
        f"Coinbase spot bot {result['ts']}",
        f"Mode: {result.get('mode')} | Decision: {result['decision']}",
        f"Top: {top['product_id']} {top['action']} tech={top['score']} final={top.get('final_score', top['score'])} ctx={top.get('context_score', 0)} price={top['price']} 24h={top['change_24h']:.2f}% RSI={top['rsi']:.1f}",
        "Reasons: " + "; ".join(top.get("reasons", [])),
    ]
    if "balances" in result:
        parts.append("Balances checked: " + ", ".join(f"{k}={v:.6g}" for k, v in sorted(result["balances"].items())))
    if "auth_status" in result:
        parts.append(f"Auth: {result['auth_status']}")
    if "proposed_order" in result:
        parts.append("Proposed: " + json.dumps(result["proposed_order"], sort_keys=True))
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--status", action="store_true", help="Read status/analyze only")
    ap.add_argument("--live", action="store_true", help="Attempt live order if all gates allow it")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_json(Path(args.config))
    result = run(cfg, status=args.status, live=args.live)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else summarize(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
