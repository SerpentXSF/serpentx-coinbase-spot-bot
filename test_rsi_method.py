#!/usr/bin/env python3
"""rsi_method: the default keeps the bot's historical RSI; "wilder" matches charting sites."""
from __future__ import annotations

import unittest

import coinbase_spot_bot as bot
import config_check
import indicators

# StockCharts' published Wilder RSI worked example (14 periods).
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
          46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
          43.42, 42.66, 43.13]


class RsiMethodTests(unittest.TestCase):
    def test_wilder_matches_the_published_example(self) -> None:
        # StockCharts rounds intermediate averages to 4 decimals, hence the small tolerance.
        self.assertAlmostEqual(indicators.rsi_wilder(CLOSES[:15]), 70.53, delta=0.1)
        self.assertAlmostEqual(indicators.rsi_wilder(CLOSES[:16]), 66.32, delta=0.1)
        self.assertAlmostEqual(indicators.rsi_wilder(CLOSES), 37.77, delta=0.1)

    def test_default_is_the_historical_simple_rsi(self) -> None:
        self.assertEqual(indicators.rsi_for({}, CLOSES), indicators.rsi(CLOSES))
        self.assertEqual(indicators.rsi_for({"rsi_method": "simple"}, CLOSES), indicators.rsi(CLOSES))
        self.assertEqual(indicators.rsi_for({"rsi_method": "Wilder"}, CLOSES), indicators.rsi_wilder(CLOSES))

    def test_scoring_uses_the_configured_method(self) -> None:
        candles = [{"start": str(i), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c} for i, c in enumerate(CLOSES * 4)]
        simple = bot.technical_long_score({"rsi_method": "simple"}, candles, candles, None, 1.0, 1.0)["rsi"]
        wilder = bot.technical_long_score({"rsi_method": "wilder"}, candles, candles, None, 1.0, 1.0)["rsi"]
        self.assertNotAlmostEqual(simple, wilder, places=3)

    def test_invalid_method_is_a_config_error(self) -> None:
        self.assertTrue(any("'rsi_method'" in e for e in config_check.validate({"rsi_method": "ema"})["errors"]))


if __name__ == "__main__":
    unittest.main()


class DivergenceMethodTests(unittest.TestCase):
    def test_divergence_uses_the_configured_rsi_method(self) -> None:
        candles = [{"start": str(i), "close": c} for i, c in enumerate(CLOSES * 3)]
        simple = indicators.rsi_divergence(candles, period=14, swing_window=1, lookback=80)
        wilder = indicators.rsi_divergence(candles, period=14, swing_window=1, lookback=80, method="wilder")
        self.assertEqual(indicators._rsi_series(CLOSES, 14, "wilder")[-1], indicators.rsi_wilder(CLOSES))
        self.assertEqual(indicators._rsi_series(CLOSES, 14)[-1], indicators.rsi(CLOSES))
        if simple.get("latest_rsi") is not None and wilder.get("latest_rsi") is not None:
            self.assertNotEqual(simple["latest_rsi"], wilder["latest_rsi"])
