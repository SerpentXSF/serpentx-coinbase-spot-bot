#!/usr/bin/env python3
"""Connection reuse, the short product-info cache, and free clock-skew checks."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import coinbase_spot_bot as bot


class HttpEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        bot._PRODUCT_INFO_CACHE.clear()
        self.addCleanup(bot._PRODUCT_INFO_CACHE.clear)
        saved = dict(bot._COINBASE_TIME_OFFSET)
        self.addCleanup(bot._COINBASE_TIME_OFFSET.update, saved)

    def test_one_session_is_reused(self) -> None:
        self.assertIs(bot.http(), bot.http())

    def test_product_info_is_fetched_once_within_the_ttl(self) -> None:
        clock = {"t": 1000.0}
        with patch.object(bot, "public_get", return_value={"price": "1"}) as get, \
                patch.object(bot.time, "monotonic", lambda: clock["t"]):
            bot.product_info("BTC-USDC")
            clock["t"] += 2
            bot.product_info("BTC-USDC")
            bot.product_info("ETH-USDC")
            self.assertEqual(get.call_count, 2)
            clock["t"] += bot.PRODUCT_INFO_TTL_SECONDS
            bot.product_info("BTC-USDC")
            self.assertEqual(get.call_count, 3)

    def test_server_date_from_any_response_corrects_clock_skew(self) -> None:
        from email.utils import formatdate

        now = 1_800_000_000.0
        with patch.object(bot.time, "time", return_value=now):
            bot._note_server_date(MagicMock(headers={"Date": formatdate(now + 120, usegmt=True)}))
            self.assertAlmostEqual(bot._COINBASE_TIME_OFFSET["value"], 120, delta=1)
            bot._note_server_date(MagicMock(headers={"Date": formatdate(now + 10, usegmt=True)}))
            self.assertEqual(bot._COINBASE_TIME_OFFSET["value"], 0.0)  # small skew ignored, as before

    def test_recent_response_means_no_extra_clock_request(self) -> None:
        from email.utils import formatdate

        now = 1_800_000_000.0
        with patch.object(bot.time, "time", return_value=now):
            bot._note_server_date(MagicMock(headers={"Date": formatdate(now + 90, usegmt=True)}))
            with patch.object(bot, "http") as http:
                self.assertEqual(bot.coinbase_epoch_now(), int(now + 90))
                http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
