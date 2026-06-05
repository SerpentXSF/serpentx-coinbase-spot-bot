#!/usr/bin/env python3
"""Coinbase Advanced Trade spot strategy bot.

Defaults are intentionally safe:
- Loads credentials from a local .env file or environment variables.
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
ROOT = Path(os.getenv("COINBASE_BOT_HOME", Path(__file__).resolve().parent))
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
        os.environ.setdefault(key, value)


def env_present() -> dict[str, bool]:
    return {
        "COINBASE_API_KEY_NAME": bool(os.getenv("COINBASE_API_KEY_NAME")),
        "COINBASE_API_PRIVATE_KEY": bool(os.getenv("COINBASE_API_PRIVATE_KEY")),
        "COINBASE_TRADING_ENABLED": os.getenv("COINBASE_TRADING_ENABLED") == "1",
        "HELIUS_API_KEY": bool(os.getenv("HELIUS_API_KEY")),
        "HELIUS_RPC_URL": bool(os.getenv("HELIUS_RPC_URL") or os.getenv("HELIUS_GATEKEEPER_RPC_URL")),
        "UNUSUAL_WHALES_API_KEY": bool(os.getenv("UNUSUAL_WHALES_API_KEY")),
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


def build_jwt(method: str, path: str) -> str:
    if jwt is None:
        raise RuntimeError("Missing auth dependency: PyJWT")
    key_name = require_env("COINBASE_API_KEY_NAME")
    private_key = load_private_key(require_env("COINBASE_API_PRIVATE_KEY"))
    now = int(time.time())
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


def is_market_orderable_product(info: dict[str, Any]) -> bool:
    """True when Coinbase should accept market IOC orders for this product."""
    if info.get("product") and isinstance(info.get("product"), dict):
        info = info["product"]
    if info.get("trading_disabled") or info.get("is_disabled") or info.get("cancel_only") or info.get("limit_only"):
        return False
    return str(info.get("status") or "").lower() == "online"


NEGATIVE_CONTEXT_WORDS = {
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sec", "delist", "delisting",
    "outage", "halt", "insolvent", "bankrupt", "rug", "scam", "drain", "breach",
    "investigation", "charges", "sanction", "stolen", "vulnerability",
}
POSITIVE_CONTEXT_WORDS = {
    "upgrade", "partnership", "integrates", "integration", "launch", "mainnet", "listing",
    "listed", "etf", "approval", "adoption", "funding", "revenue", "record", "growth",
}
SOCIAL_POSITIVE_WORDS = {"bullish", "breakout", "accumulating", "accumulation", "strong", "support", "trend"}
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
    pos_hits = sorted(w for w in positive_words if re.search(r"\b" + re.escape(w) + r"\b", joined))
    neg_hits = sorted(w for w in negative_words if re.search(r"\b" + re.escape(w) + r"\b", joined))
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



def _get_json_cached(cfg: dict[str, Any], cache: dict[str, Any], key: str, url: str, params: dict[str, Any] | None, ttl: int, provider: str) -> dict[str, Any]:
    cached = cache.get(key)
    if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), ttl):
        return {**cached, "cached": True}
    out: dict[str, Any] = {"provider": provider, "fetched_at": utcnow().isoformat()}
    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "coinbase-spot-bot/1.0"})
        r.raise_for_status()
        out["data"] = r.json()
    except Exception as e:
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
            try:
                r = requests.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "ids": cg_id, "price_change_percentage": "24h,7d"},
                    timeout=20,
                    headers={"User-Agent": "coinbase-spot-bot/1.0"},
                )
                r.raise_for_status()
                rows = r.json()
                market["data"] = rows[0] if rows else {}
            except Exception as e:
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

    # Market-wide fear/greed is a small risk modifier, cached globally.
    fng = _get_json_cached(cfg, cache, "alternative:fear_greed", "https://api.alternative.me/fng/", {"limit": 1, "format": "json"}, ttl, "alternative_fear_greed")
    try:
        fg = int((fng.get("data", {}).get("data") or [{}])[0].get("value", 50))
        out["fear_greed"] = fg
        if fg <= 20:
            out["score"] -= 1; out["reasons"].append(f"market extreme fear {fg}")
        elif 45 <= fg <= 75:
            out["reasons"].append(f"market sentiment acceptable {fg}")
        elif fg >= 85:
            out["score"] -= 1; out["reasons"].append(f"market extreme greed {fg}; chase risk")
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
    terms = meta.get("news_terms") or [symbol, meta.get("name", "")]
    query = " OR ".join(t for t in terms if t)
    if not query:
        query = symbol
    url = "https://news.google.com/rss/search"
    params = {"q": f"({query}) crypto cryptocurrency", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    out = {"score": 0, "reasons": [], "risk_block": False, "provider": "google_news_rss", "items": []}
    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "coinbase-spot-bot/1.0"})
        r.raise_for_status()
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", r.text, flags=re.S)
        parsed = []
        for a, b in titles[1:8]:  # skip feed title, keep small for quota/noise
            title = re.sub(r"\s+", " ", (a or b)).strip()
            if title:
                parsed.append(title)
        score, pos, neg = _score_text_items(parsed, POSITIVE_CONTEXT_WORDS, NEGATIVE_CONTEXT_WORDS)
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
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "coinbase-spot-bot/1.0"})
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
    endpoint = os.getenv("HELIUS_RPC_URL") or os.getenv("HELIUS_GATEKEEPER_RPC_URL")
    api_key = os.getenv("HELIUS_API_KEY")
    if not endpoint and api_key:
        endpoint = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    if not endpoint:
        out.update({"provider": "helius_missing_key", "reasons": ["Helius RPC URL/API key not configured; neutral"]})
        cache[key] = out
        return out
    try:
        body = {"jsonrpc": "2.0", "id": "serpentx-context", "method": "getSignaturesForAddress", "params": [mint, {"limit": int(whale_cfg.get("helius_signature_limit", 20))}]}
        r = requests.post(endpoint, json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        sigs = data.get("result") or []
        recent_errs = sum(1 for x in sigs if x.get("err"))
        # Conservative: token-account activity alone is not proof of whale accumulation/distribution.
        out.update({"score": 0, "recent_signature_count": len(sigs), "recent_error_count": recent_errs, "reasons": ["Solana mint activity checked; no exchange-flow classifier yet"]})
    except Exception as e:
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
    risk_block = bool(news.get("risk_block") or market.get("risk_block") or social.get("risk_block") or whale.get("risk_block"))
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


def score_product(cfg: dict[str, Any], product_id: str) -> Signal:
    info = product_info(product_id)
    if info.get("product") and isinstance(info.get("product"), dict):
        info = info["product"]
    if not is_market_orderable_product(info):
        s = Signal(product_id, fnum(info.get("price")), 0, "HOLD_USDC", ["product not market-orderable"], 50, fnum(info.get("price_percentage_change_24h")), fnum(info.get("volume_24h")))
        s.risk_block = True
        return s
    exec_c = fetch_candles(product_id, cfg["bar_exec"], int(cfg["lookback_exec_hours"]))
    trend_c = fetch_candles(product_id, cfg["bar_trend"], int(cfg["lookback_trend_hours"]))
    closes = [fnum(c.get("close")) for c in exec_c if fnum(c.get("close")) > 0]
    trend_closes = [fnum(c.get("close")) for c in trend_c if fnum(c.get("close")) > 0]
    price = fnum(info.get("price"), closes[-1] if closes else 0)
    if len(closes) < 30 or len(trend_closes) < 20 or price <= 0:
        return Signal(product_id, price, 0, "HOLD", ["insufficient candle history"], 50, fnum(info.get("price_percentage_change_24h")), fnum(info.get("volume_24h")))
    ema9 = ema(closes[-60:], 9)
    ema21 = ema(closes[-80:], 21)
    ema50 = ema(closes[-120:], 50)
    trend50 = ema(trend_closes[-120:], 50)
    cur_rsi = rsi(closes, 14)
    chg = fnum(info.get("price_percentage_change_24h"))
    vol = fnum(info.get("volume_24h"))
    score = 0
    reasons = []

    # Spot-only: buy strength on trend alignment or controlled pullbacks; otherwise hold USDC.
    if ema9 > ema21 > ema50:
        score += 2; reasons.append("15m EMA stack bullish")
    if price > trend50:
        score += 1; reasons.append("above 1H EMA50")
    if 42 <= cur_rsi <= 68:
        score += 1; reasons.append(f"RSI supportive {cur_rsi:.1f}")
    elif cur_rsi < 35 and price > trend50:
        score += 1; reasons.append(f"trend pullback RSI {cur_rsi:.1f}")
    if chg > 0:
        score += 1; reasons.append(f"24h green {chg:.2f}%")
    if vol > 0:
        score += 1; reasons.append("active volume")
    action = "BUY" if score >= int(cfg["score_threshold"]) else "HOLD_USDC"
    return Signal(product_id, price, score, action, reasons, cur_rsi, chg, vol)


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


def preview_market(product_id: str, side: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
    if quote_size is not None:
        oc = {"market_market_ioc": {"quote_size": str(round(quote_size, 8))}}
    elif base_size is not None:
        oc = {"market_market_ioc": {"base_size": str(round(base_size, 8))}}
    else:
        raise RuntimeError("quote_size or base_size required")
    return private_request("POST", "/api/v3/brokerage/orders/preview", {"product_id": product_id, "side": side.upper(), "order_configuration": oc})


def place_market(product_id: str, side: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
    if quote_size is not None:
        oc = {"market_market_ioc": {"quote_size": str(round(quote_size, 8))}}
    elif base_size is not None:
        oc = {"market_market_ioc": {"base_size": str(round(base_size, 8))}}
    else:
        raise RuntimeError("quote_size or base_size required")
    body = {"client_order_id": str(uuid.uuid4()), "product_id": product_id, "side": side.upper(), "order_configuration": oc}
    return private_request("POST", "/api/v3/brokerage/orders", body)


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
        if (now - ts).total_seconds() <= 86400:
            count += 1
    return count


def dynamic_quote_size(cfg: dict[str, Any], quote_bal: float, top: Signal) -> float:
    """Size between min/max quote based on available balance and asset risk."""
    min_q = float(cfg.get("min_quote_per_trade", cfg.get("min_quote_balance_to_trade", 15)))
    max_q = float(cfg.get("max_quote_per_trade", 20))
    pct_cap = quote_bal * float(cfg["max_account_quote_pct_per_trade"])
    size = max_q
    effective_score = int(getattr(top, "final_score", 0) or top.score)
    threshold = int(cfg.get("final_score_threshold", cfg["score_threshold"]))
    if effective_score <= threshold:
        size = min(size, min_q)
    if top.quote_volume < 1_000_000:
        size = min(size, min_q)
    if top.change_24h > 20 or top.change_24h < -10:
        size = min(size, min_q)
    size = min(size, pct_cap, quote_bal)
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
    """Return bot-managed positions, migrating legacy single-position state."""
    positions = state.get("open_positions")
    if isinstance(positions, list):
        return [p for p in positions if isinstance(p, dict) and p.get("product_id")]
    legacy = state.get("open_position")
    if isinstance(legacy, dict) and legacy.get("product_id"):
        state["open_positions"] = [legacy]
        state.pop("open_position", None)
        return state["open_positions"]
    return []


def persist_positions(state: dict[str, Any], positions: list[dict[str, Any]]) -> None:
    state["open_positions"] = positions
    state.pop("open_position", None)


def choose_entry_signal(cfg: dict[str, Any], signals: list[Signal], positions: list[dict[str, Any]]) -> Signal | None:
    held = {str(p.get("product_id")) for p in positions}
    prevent_dup = bool(cfg.get("prevent_same_asset_duplicate", True))
    second_min = int(cfg.get("second_position_min_final_score", cfg.get("second_position_min_score", cfg.get("score_threshold", 4))))
    for sig in signals:
        if sig.action != "BUY" or sig.risk_block:
            continue
        if prevent_dup and sig.product_id in held:
            continue
        effective_score = int(sig.final_score or sig.score)
        if positions and effective_score < second_min:
            continue
        return sig
    return None


def run(cfg: dict[str, Any], *, status: bool=False, live: bool=False) -> dict[str, Any]:
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
    positions = normalize_positions(state)
    max_positions = int(cfg.get("max_open_positions", 1))
    result["daily_trades_used"] = daily_trades
    result["open_positions"] = positions

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
    if positions and not status:
        updated_positions: list[dict[str, Any]] = []
        for pos in positions:
            pos_product = str(pos.get("product_id"))
            base, _, _ = pos_product.partition("-")
            entry = fnum(pos.get("entry_price"))
            current = fnum(product_info(pos_product).get("price")) if pos_product else 0.0
            base_size = min(fnum(pos.get("base_size_est")), balances.get(base, 0.0))
            pnl_pct = ((current - entry) / entry) if entry > 0 and current > 0 else 0.0
            enriched = {**pos, "current_price": current, "pnl_pct": pnl_pct}
            sell_reason = None
            if pnl_pct >= float(cfg["take_profit_pct"]):
                sell_reason = "TAKE_PROFIT"
            elif pnl_pct <= -float(cfg["stop_loss_pct"]):
                sell_reason = "STOP_LOSS"
            elif pnl_pct <= -float(cfg["soft_invalidation_pct"]):
                try:
                    pos_signal = apply_context_scores(cfg, score_product(cfg, pos_product), context_cache)
                    if pos_signal.risk_block or int(pos_signal.final_score or pos_signal.score) < int(cfg.get("final_score_threshold", cfg["score_threshold"])):
                        sell_reason = "SOFT_INVALIDATION"
                        result["position_signal"] = pos_signal.__dict__
                except Exception as e:
                    result["position_signal_error"] = str(e)[:200]
            if sell_reason and base_size > 0:
                result["open_position"] = enriched
                result["proposed_order"] = {"product_id": pos_product, "side": "SELL", "base_size": base_size, "reason": sell_reason}
                result["preview"] = preview_market(pos_product, "SELL", base_size=base_size)
                gate = live_gates_open(cfg, live)
                if gate:
                    result["decision"] = gate
                    updated_positions.append(pos)
                else:
                    order = place_market(pos_product, "SELL", base_size=base_size)
                    result["decision"] = "ORDER_SENT"
                    result["order"] = order
                    state["cooldown_until"] = (utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                    append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "SELL", "reason": sell_reason, "order": order, "position": pos})
                updated_positions.extend(p for p in positions if p is not pos)
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
        entry_signal = choose_entry_signal(cfg, signals, positions)
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
            result["proposed_order"] = {"product_id": entry_signal.product_id, "side": "BUY", "quote_size": quote_size}
            if quote_size < float(cfg["min_quote_balance_to_trade"]):
                result["decision"] = "ORDER_TOO_SMALL"
            else:
                preview = preview_market(entry_signal.product_id, "BUY", quote_size=quote_size)
                result["preview"] = preview
                gate = live_gates_open(cfg, live)
                if gate:
                    result["decision"] = gate
                else:
                    order = place_market(entry_signal.product_id, "BUY", quote_size=quote_size)
                    result["decision"] = "ORDER_SENT"
                    result["order"] = order
                    state["cooldown_until"] = (utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
                    positions.append({
                        "product_id": entry_signal.product_id,
                        "entry_price": entry_signal.price,
                        "quote_size": quote_size,
                        "base_size_est": quote_size / entry_signal.price if entry_signal.price > 0 else 0.0,
                        "opened_at": result["ts"],
                    })
                    persist_positions(state, positions)
                    append_jsonl(Path(cfg["trades_log_path"]), {"ts": result["ts"], "side": "BUY", "order": order, "signal": entry_signal.__dict__, "quote_size": quote_size})

    if not (status and result.get("open_positions")):
        result["open_positions"] = normalize_positions(state)
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
