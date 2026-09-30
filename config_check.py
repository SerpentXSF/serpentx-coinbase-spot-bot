#!/usr/bin/env python3
"""Validate config.json before the bot trades with it.

A typo such as ``stop_los_pct`` is otherwise silently ignored and the code
falls back to a default -- for a trading bot, a silently wrong risk setting.

* **errors** -- missing required keys, wrong types, or values that make the
  risk rules nonsensical. The bot blocks new entries (CONFIG_INVALID) but keeps
  managing exits, so open positions stay protected.
* **warnings** -- unknown keys (with a "did you mean"), and settings that are
  legal but likely unintended.

Run directly to check a file: ``python config_check.py [config.json]``
(exit code 1 when there are errors).
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

GRANULARITIES = {"ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "TWO_HOUR", "SIX_HOUR", "ONE_DAY"}

# Keys the code reads with cfg["..."] -- a missing one crashes a run.
REQUIRED = {
    "active_trading", "allowed_products", "bar_exec", "bar_trend", "cooldown_minutes_after_trade",
    "lookback_exec_hours", "lookback_trend_hours", "max_account_quote_pct_per_trade",
    "min_quote_balance_to_trade", "quote_currency", "runs_log_path", "score_threshold",
    "soft_invalidation_pct", "state_path", "stop_loss_pct", "strategy_name", "take_profit_pct",
    "trades_log_path",
}

# Optional keys read with cfg.get(...), plus documentation-only keys shipped in
# config.example.json. Anything else is reported as unknown.
OPTIONAL = {
    "base_url", "breakeven_activation_pct", "breakeven_lock_enabled", "breakeven_lock_pct",
    "candidate_backtest_cadence_minutes", "context_cache_path", "context_overheated_24h_pct",
    "daily_max_loss_pct", "daily_max_trades", "directional_strategy", "entry_rotator_cadence_minutes",
    "entry_trigger_mode", "env_file", "estimated_roundtrip_fee_pct", "execution_mode",
    "exit_monitor_cadence_minutes", "expected_move_range_multiplier", "external_context_enabled",
    "fee_aware_entry_enabled", "fee_tracking", "final_score_threshold", "five_minute_confirmation_enabled",
    "five_minute_confirmation_lookback_hours", "five_minute_confirmation_min_candles",
    "five_minute_reclaim_ema9_enabled", "higher_timeframe_regime_gate", "maker_fee_verification",
    "maker_limit_enabled", "maker_limit_pending_timeout_minutes", "maker_limit_post_only",
    "maker_limit_price_offset_pct", "maker_limit_risk_exit_reasons", "maker_limit_use_market_for_risk_exits",
    "market_context", "max_open_positions", "max_quote_per_trade", "min_avg_exec_range_pct",
    "min_final_score_when_overheated", "min_quote_per_trade", "minimum_net_edge_pct", "mode",
    "news_context", "no_averaging_down", "one_hour_regime_gate_enabled", "order_cache_path",
    "overheated_block_pct", "overheated_require_context_pct", "overheated_size_down_pct",
    "per_product_cooldown_minutes_after_stop", "per_product_cooldown_minutes_after_trade",
    "prevent_same_asset_duplicate", "product_metadata_cache_path", "provider_budget_path",
    "provider_cooldown_seconds", "provider_min_interval_seconds", "rotation_last_generated_at",
    "rotation_source", "rotator_analyzer_timeout_seconds", "rotator_bot_timeout_seconds",
    "rsi_bearish_divergence_penalty", "rsi_bullish_divergence_score", "rsi_divergence_enabled",
    "rsi_divergence_lookback_candles", "rsi_divergence_period", "rsi_divergence_swing_window",
    "run_lock_stale_seconds", "runs_log_backups", "runs_log_max_bytes", "scale_exit_enabled",
    "scale_exit_fraction", "scale_exit_reason", "scale_exit_take_profit_pct",
    "second_position_min_final_score", "second_position_min_score", "sizing_max_score_multiplier",
    "sizing_quote_balance_target", "sizing_score_step_multiplier", "sizing_thin_volume_quote_usdc",
    "social_context", "soft_invalidation", "strategy_scope", "thirty_minute_regime_gate_enabled",
    "token_context", "traderjoe_recommendation_cadence_minutes", "trading_quote_balance_target",
    "trailing_activation_pct", "trailing_drawdown_pct", "trailing_stop_enabled", "whale_context",
}
KNOWN = REQUIRED | OPTIONAL

# Fractions of price or balance: 0.035 means 3.5%. Values >= 1 are almost
# certainly percentages typed as whole numbers.
FRACTION_KEYS = {
    "stop_loss_pct", "take_profit_pct", "soft_invalidation_pct", "daily_max_loss_pct",
    "max_account_quote_pct_per_trade", "trailing_activation_pct", "trailing_drawdown_pct",
    "breakeven_activation_pct", "breakeven_lock_pct", "scale_exit_take_profit_pct", "scale_exit_fraction",
    "maker_limit_price_offset_pct", "minimum_net_edge_pct", "min_avg_exec_range_pct",
    "estimated_roundtrip_fee_pct",
}
POSITIVE_INT_KEYS = {"max_open_positions", "daily_max_trades", "lookback_exec_hours", "lookback_trend_hours"}
NON_NEGATIVE_NUMBER_KEYS = {
    "min_quote_per_trade", "max_quote_per_trade", "min_quote_balance_to_trade", "sizing_quote_balance_target",
    "cooldown_minutes_after_trade", "per_product_cooldown_minutes_after_stop",
    "per_product_cooldown_minutes_after_trade", "maker_limit_pending_timeout_minutes",
    "rotator_analyzer_timeout_seconds", "rotator_bot_timeout_seconds", "run_lock_stale_seconds",
    "runs_log_max_bytes", "runs_log_backups", "score_threshold", "final_score_threshold",
}
BOOL_KEYS = {
    "active_trading", "external_context_enabled", "fee_aware_entry_enabled", "five_minute_confirmation_enabled",
    "maker_limit_enabled", "maker_limit_post_only", "scale_exit_enabled", "trailing_stop_enabled",
    "breakeven_lock_enabled", "prevent_same_asset_duplicate", "rsi_divergence_enabled",
    "thirty_minute_regime_gate_enabled",
}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(cfg: dict[str, Any]) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for key in sorted(REQUIRED - set(cfg)):
        errors.append(f"missing required key '{key}'")
    for key in sorted(set(cfg) - KNOWN):
        hint = difflib.get_close_matches(key, sorted(KNOWN), n=1, cutoff=0.75)
        warnings.append(f"unknown key '{key}' is ignored" + (f" -- did you mean '{hint[0]}'?" if hint else ""))

    for key in sorted(BOOL_KEYS & set(cfg)):
        if not isinstance(cfg[key], bool):
            errors.append(f"'{key}' must be true or false, got {cfg[key]!r}")
    for key in sorted(FRACTION_KEYS & set(cfg)):
        v = cfg[key]
        if not _is_number(v):
            errors.append(f"'{key}' must be a number, got {v!r}")
        elif not 0 <= v < 1:
            errors.append(f"'{key}' is {v}; it is a fraction (0.035 = 3.5%), so it must be between 0 and 1")
    for key in sorted(POSITIVE_INT_KEYS & set(cfg)):
        v = cfg[key]
        if not _is_number(v) or v < 1 or int(v) != v:
            errors.append(f"'{key}' must be a whole number >= 1, got {v!r}")
    for key in sorted(NON_NEGATIVE_NUMBER_KEYS & set(cfg)):
        v = cfg[key]
        if not _is_number(v) or v < 0:
            errors.append(f"'{key}' must be a number >= 0, got {v!r}")

    for key in ("bar_exec", "bar_trend"):
        if key in cfg and cfg[key] not in GRANULARITIES:
            errors.append(f"'{key}' is {cfg[key]!r}; expected one of {', '.join(sorted(GRANULARITIES))}")

    quote = cfg.get("quote_currency")
    products = cfg.get("allowed_products")
    if "allowed_products" in cfg:
        if not isinstance(products, list) or not all(isinstance(p, str) and "-" in p for p in products):
            errors.append("'allowed_products' must be a list like [\"BTC-USDC\", \"ETH-USDC\"]")
        elif not products:
            errors.append("'allowed_products' is empty; run rotate_and_run.py or add products")
        elif isinstance(quote, str):
            wrong = [p for p in products if not p.endswith(f"-{quote}")]
            if wrong:
                warnings.append(f"products not quoted in {quote}: {', '.join(wrong)}")

    def num(key: str) -> float | None:
        v = cfg.get(key)
        return float(v) if _is_number(v) else None

    stop, tp = num("stop_loss_pct"), num("take_profit_pct")
    if stop is not None and tp is not None and stop >= tp:
        warnings.append(f"stop_loss_pct ({cfg['stop_loss_pct']}) >= take_profit_pct ({cfg['take_profit_pct']}): losses are at least as large as wins")
    soft = num("soft_invalidation_pct")
    if soft is not None and stop is not None and soft > stop:
        warnings.append(f"soft_invalidation_pct ({cfg['soft_invalidation_pct']}) > stop_loss_pct ({cfg['stop_loss_pct']}): the soft exit can never fire before the hard stop")
    lo, hi = num("min_quote_per_trade"), num("max_quote_per_trade")
    if lo is not None and hi is not None and lo > hi:
        errors.append(f"min_quote_per_trade ({cfg['min_quote_per_trade']}) > max_quote_per_trade ({cfg['max_quote_per_trade']})")
    fees = cfg.get("fee_tracking") if isinstance(cfg.get("fee_tracking"), dict) else {}
    roundtrip = fees.get("estimated_roundtrip_fee_pct", cfg.get("estimated_roundtrip_fee_pct"))
    if _is_number(roundtrip) and tp is not None and tp <= roundtrip:
        warnings.append(f"take_profit_pct ({cfg['take_profit_pct']}) <= estimated round-trip fees ({roundtrip}): a take-profit exit loses money")
    act, drop = num("trailing_activation_pct"), num("trailing_drawdown_pct")
    if cfg.get("trailing_stop_enabled") and act is not None and drop is not None and drop >= act:
        warnings.append(f"trailing_drawdown_pct ({cfg['trailing_drawdown_pct']}) >= trailing_activation_pct ({cfg['trailing_activation_pct']}): the trailing stop can exit at a loss")
    if cfg.get("active_trading") and cfg.get("mode") == "preview":
        warnings.append("active_trading is true but mode is 'preview'; mode is a label only -- live orders depend on the three live gates")
    return {"errors": errors, "warnings": warnings}


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"ERROR: {path} not found. Create it with: cp config.example.json config.json")
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}")
        return 1
    report = validate(cfg)
    for e in report["errors"]:
        print(f"ERROR: {e}")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    if not report["errors"] and not report["warnings"]:
        print(f"OK: {path} looks valid")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
