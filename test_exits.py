#!/usr/bin/env python3
"""Priority order of exit rules in the shared exit engine."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import coinbase_spot_bot as bot
import exits

CFG = {"take_profit_pct": 0.065, "stop_loss_pct": 0.035}


class ExitReasonTests(unittest.TestCase):
    def reason(self, current: float, *, scale=(None, 0.0), structural=None, trailing=None):
        with patch.object(bot, "scale_exit_plan", return_value=scale), \
                patch.object(bot, "structural_exit_reason", return_value=structural), \
                patch.object(bot, "trailing_exit_reason", return_value=trailing):
            return exits.exit_reason(bot, CFG, {"product_id": "X-USDC"}, current, 1.0, 10.0)

    def test_scale_exit_wins_and_sets_partial_size(self) -> None:
        self.assertEqual(self.reason(1.10, scale=("SCALE_TAKE_PROFIT", 5.0)), ("SCALE_TAKE_PROFIT", 5.0, False))

    def test_take_profit_and_stop_loss_thresholds(self) -> None:
        self.assertEqual(self.reason(1.065)[0], "TAKE_PROFIT")
        self.assertEqual(self.reason(0.965)[0], "STOP_LOSS")
        self.assertIsNone(self.reason(1.0)[0])

    def test_hard_stop_beats_structural_and_structural_is_flagged(self) -> None:
        self.assertEqual(self.reason(0.96, structural="SOFT_INVALIDATION")[0], "STOP_LOSS")
        self.assertEqual(self.reason(0.99, structural="SOFT_INVALIDATION"), ("SOFT_INVALIDATION", 10.0, True))

    def test_trailing_only_fires_when_nothing_else_does(self) -> None:
        self.assertEqual(self.reason(1.03, trailing="TRAILING_STOP")[0], "TRAILING_STOP")
        self.assertEqual(self.reason(1.07, trailing="TRAILING_STOP")[0], "TAKE_PROFIT")

    def test_bot_and_exit_monitor_share_one_engine(self) -> None:
        import exit_monitor

        self.assertIs(exit_monitor.exits, exits)
        self.assertIs(bot.exits, exits)


if __name__ == "__main__":
    unittest.main()
