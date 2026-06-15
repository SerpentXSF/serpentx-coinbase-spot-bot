#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest.mock import patch

import coinbase_spot_bot as bot


class SizingAndSentimentTests(unittest.TestCase):
    def _cfg(self) -> dict:
        return {
            "score_threshold": 4,
            "final_score_threshold": 5,
            "min_quote_balance_to_trade": 15,
            "min_quote_per_trade": 15,
            "max_quote_per_trade": 20,
            "max_account_quote_pct_per_trade": 0.15,
            "sizing_quote_balance_target": 100,
            "sizing_score_step_multiplier": 0.15,
            "sizing_max_score_multiplier": 1.30,
            "sizing_thin_volume_quote_usdc": 1_000_000,
            "overheated_size_down_pct": 20,
        }

    def _signal(self, *, final_score: int, score: int = 5, quote_volume: float = 5_000_000, change_24h: float = 4.0) -> bot.Signal:
        return bot.Signal(
            product_id="TEST-USDC",
            price=1.0,
            score=score,
            final_score=final_score,
            action="BUY",
            reasons=[],
            rsi=50.0,
            change_24h=change_24h,
            quote_volume=quote_volume,
        )

    def test_dynamic_quote_size_uses_target_bankroll_not_extra_funds(self) -> None:
        size = bot.dynamic_quote_size(self._cfg(), 250.0, self._signal(final_score=9))

        self.assertEqual(size, 19.5)

    def test_dynamic_quote_size_keeps_threshold_or_thin_volume_at_minimum_size(self) -> None:
        cfg = self._cfg()

        threshold_size = bot.dynamic_quote_size(cfg, 250.0, self._signal(final_score=5))
        thin_size = bot.dynamic_quote_size(cfg, 250.0, self._signal(final_score=9, quote_volume=500_000))

        self.assertEqual(threshold_size, 15.0)
        self.assertEqual(thin_size, 15.0)

    def test_dynamic_quote_size_does_not_force_trade_below_minimum_when_target_is_too_small(self) -> None:
        cfg = self._cfg() | {"sizing_quote_balance_target": 60}

        size = bot.dynamic_quote_size(cfg, 60.0, self._signal(final_score=9))

        self.assertEqual(size, 0.0)

    def test_fear_greed_extreme_fear_is_contrarian_boost_and_extreme_greed_is_penalty(self) -> None:
        cfg = {
            "market_context": {
                "enabled": True,
                "cache_ttl_seconds": 3600,
                "fear_extreme_threshold": 20,
                "fear_contrarian_boost": 1,
                "greed_extreme_threshold": 80,
                "greed_chase_penalty": 1,
            },
            "token_context": {},
        }

        with patch.object(bot, "_get_json_cached", return_value={"data": {"data": [{"value": "18"}]}}):
            fear = bot.fetch_market_context(cfg, "TEST-USDC", {})
        with patch.object(bot, "_get_json_cached", return_value={"data": {"data": [{"value": "88"}]}}):
            greed = bot.fetch_market_context(cfg, "TEST-USDC", {})

        self.assertEqual(fear["score"], 1)
        self.assertTrue(any("contrarian" in reason for reason in fear["reasons"]))
        self.assertEqual(greed["score"], -1)
        self.assertTrue(any("chase risk" in reason for reason in greed["reasons"]))


if __name__ == "__main__":
    unittest.main()
