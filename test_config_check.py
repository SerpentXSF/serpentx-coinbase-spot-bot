#!/usr/bin/env python3
"""Config validation and how the bot reacts to a bad config."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import coinbase_spot_bot as bot
import config_check
from test_risk_controls import RunHarness, signal

EXAMPLE = json.loads((Path(__file__).resolve().parent / "config.example.json").read_text())


def check(**overrides) -> dict:
    cfg = {k: v for k, v in (EXAMPLE | overrides).items() if v is not None}
    return config_check.validate(cfg)


class ValidateTests(unittest.TestCase):
    def test_shipped_example_is_clean(self) -> None:
        self.assertEqual(check(), {"errors": [], "warnings": []})

    def test_typo_is_flagged_with_suggestion(self) -> None:
        report = check(stop_los_pct=0.02)
        self.assertIn("unknown key 'stop_los_pct' is ignored -- did you mean 'stop_loss_pct'?", report["warnings"])

    def test_percent_typed_as_whole_number_is_an_error(self) -> None:
        self.assertTrue(any("'stop_loss_pct' is 3.5" in e for e in check(stop_loss_pct=3.5)["errors"]))

    def test_string_boolean_is_an_error(self) -> None:
        self.assertTrue(any("'active_trading' must be true or false" in e for e in check(active_trading="false")["errors"]))

    def test_missing_required_key_is_an_error(self) -> None:
        self.assertIn("missing required key 'take_profit_pct'", check(take_profit_pct=None)["errors"])

    def test_risk_geometry_warnings(self) -> None:
        w = check(stop_loss_pct=0.08)["warnings"]
        self.assertTrue(any("stop_loss_pct (0.08) >= take_profit_pct" in x for x in w))
        w = check(take_profit_pct=0.01)["warnings"]
        self.assertTrue(any("a take-profit exit loses money" in x for x in w))

    def test_bad_granularity_and_sizing(self) -> None:
        errors = check(bar_trend="30m", min_quote_per_trade=50, max_quote_per_trade=20)["errors"]
        self.assertTrue(any("'bar_trend' is '30m'" in e for e in errors))
        self.assertTrue(any("min_quote_per_trade (50) > max_quote_per_trade (20)" in e for e in errors))

    def test_every_example_key_is_known(self) -> None:
        self.assertEqual(sorted(set(EXAMPLE) - config_check.KNOWN), [])


class BotReactionTests(RunHarness):
    def test_invalid_config_blocks_entries_but_not_exits(self) -> None:
        self.cfg["active_trading"] = True
        self.cfg["max_account_quote_pct_per_trade"] = 15  # meant 15%, typed as 15.0 (1500%)
        with patch.object(bot, "choose_entry_signal", return_value=signal("AAA-USDC", 2.0, "BUY", 9)), \
                patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "CONFIG_INVALID")
        self.assertEqual(self.placed, [])

        # With an open position at its stop, the same bad config still exits.
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS1-USDC"] = 0.9
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        self.assertEqual(self.placed[0][:2], ("POS1-USDC", "SELL"))

    def test_string_active_trading_does_not_open_the_live_gate(self) -> None:
        self.assertEqual(bot.live_gates_open({"active_trading": "false"}, True), "LIVE_BLOCKED_CONFIG_ACTIVE_TRADING_FALSE")
        self.assertEqual(bot.live_gates_open({"active_trading": "true"}, True), "LIVE_BLOCKED_CONFIG_ACTIVE_TRADING_FALSE")

    def test_missing_required_key_fails_fast_with_a_clear_message(self) -> None:
        path = self.tmp / "config.json"
        path.write_text(json.dumps({k: v for k, v in EXAMPLE.items() if k != "stop_loss_pct"}))
        with self.assertRaisesRegex(RuntimeError, "missing required key 'stop_loss_pct'"):
            bot.load_config(path)

    def test_divergence_can_be_disabled(self) -> None:
        candles = [{"start": str(i), "open": 1, "high": 1.01, "low": 0.99, "close": 1 + (i % 7) * 0.01} for i in range(120)]
        off = bot.technical_long_score(EXAMPLE | {"rsi_divergence_enabled": False}, candles, candles, 1.0, 1.0, 1.0)
        self.assertEqual(off["rsi_divergence_signal"], "none")
        self.assertTrue(off["rsi_divergence"].get("disabled"))


if __name__ == "__main__":
    unittest.main()
