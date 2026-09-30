#!/usr/bin/env python3
"""Pure technical indicators shared by the bot, scanner, and backtests.

No network calls and no config: every function takes plain lists of prices
or Coinbase candle dicts and returns numbers, so results are identical
wherever they are computed.
"""
from __future__ import annotations

from typing import Any


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


def _avg_range_pct(candles: list[dict[str, Any]], limit: int = 20) -> float:
    ranges = []
    for c in candles[-limit:]:
        close = fnum(c.get("close"))
        high = fnum(c.get("high"))
        low = fnum(c.get("low"))
        if close > 0 and high > 0 and low > 0:
            ranges.append((high - low) / close)
    return sum(ranges) / len(ranges) if ranges else 0.0
