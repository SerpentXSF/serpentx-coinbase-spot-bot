#!/usr/bin/env python3
from __future__ import annotations

import unittest

import coinbase_spot_bot as bot


def candles_from_closes(closes: list[float]) -> list[dict[str, float]]:
    return [
        {"start": str(i * 900), "open": c, "high": c + 0.2, "low": c - 0.2, "close": c}
        for i, c in enumerate(closes)
    ]


class RsiDivergenceTests(unittest.TestCase):
    def test_detects_bullish_divergence_when_price_makes_lower_low_but_rsi_higher_low(self) -> None:
        # First swing low: sharp selloff gives weak RSI. Second swing low is lower
        # in price, but downside momentum has faded, so RSI should be higher.
        closes = [
            100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100,
            98, 96, 94, 92, 90, 88, 86, 85, 86, 87, 88, 89, 90,
            91, 92, 93, 92, 91, 90, 89, 88, 86, 84, 83, 84, 85, 86,
        ]

        result = bot.rsi_divergence(candles_from_closes(closes), period=14, swing_window=1, lookback=80)

        self.assertEqual(result["label"], "Bullish Divergence")
        self.assertEqual(result["signal"], "bullish")
        self.assertGreater(result["latest_rsi"], result["previous_rsi"])
        self.assertLess(result["latest_price"], result["previous_price"])

    def test_detects_bearish_divergence_when_price_makes_higher_high_but_rsi_lower_high(self) -> None:
        # First swing high: strong rally gives high RSI. Second swing high is
        # higher in price, but momentum has weakened, so RSI should be lower.
        closes = [
            100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100,
            102, 104, 106, 108, 110, 112, 114, 115, 114, 113, 112,
            111, 110, 109, 110, 111, 112, 113, 114, 115, 116, 117, 116,
            115, 114,
        ]

        result = bot.rsi_divergence(candles_from_closes(closes), period=14, swing_window=1, lookback=80)

        self.assertEqual(result["label"], "Bearish Divergence")
        self.assertEqual(result["signal"], "bearish")
        self.assertLess(result["latest_rsi"], result["previous_rsi"])
        self.assertGreater(result["latest_price"], result["previous_price"])

    def test_returns_none_when_no_divergence_is_present(self) -> None:
        closes = [100 + i for i in range(30)]

        result = bot.rsi_divergence(candles_from_closes(closes), period=5, swing_window=1, lookback=30)

        self.assertEqual(result["label"], "None")
        self.assertEqual(result["signal"], "none")


if __name__ == "__main__":
    unittest.main()
