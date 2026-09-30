#!/usr/bin/env python3
"""Push alerts: what triggers them, payload formats, de-duplication, failure safety."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import alerts
import coinbase_spot_bot as bot

SELL = {
    "ts": "2026-09-30T12:00:00+00:00", "decision": "ORDER_SENT",
    "proposed_order": {"product_id": "SOL-USDC", "side": "SELL", "base_size": 0.1, "reason": "STOP_LOSS"},
    "order": {"success": True, "success_response": {"order_id": "abc"}},
    "fill": {"average_filled_price": 140.5}, "realized_pnl_quote": -0.7321,
}


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-alerts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"state_path": str(self.tmp / "state" / "state.json")}
        self.post = MagicMock(return_value=MagicMock(status_code=204))
        p = patch.object(alerts.requests, "post", self.post)
        p.start()
        self.addCleanup(p.stop)

    def env(self, url: str, fmt: str = ""):
        return patch.dict(os.environ, {"ALERT_WEBHOOK_URL": url, "ALERT_WEBHOOK_FORMAT": fmt})

    def test_live_order_message_has_the_essentials(self) -> None:
        [(key, text)] = alerts.messages_for(SELL, source="exit_monitor")
        self.assertEqual(key, "order:ORDER_SENT:abc")
        for part in ("[exit_monitor]", "SELL SOL-USDC", "STOP_LOSS", "140.5", "-0.7321"):
            self.assertIn(part, text)

    def test_preview_and_blocked_runs_stay_quiet(self) -> None:
        for decision in ("PREVIEW_ONLY", "LIVE_BLOCKED_ENV_TRADING_DISABLED", "HOLD_POSITIONS", "NO_ELIGIBLE_ENTRY"):
            self.assertEqual(alerts.messages_for({**SELL, "decision": decision}), [])

    def test_pending_limit_fill_and_conditions_alert(self) -> None:
        result = {"decision": "DAILY_LOSS_LIMIT_REACHED", "daily_loss_limit": {"date": "2026-09-30", "realized_quote": -2.6, "limit_quote": -2.5},
                  "pending_order_events": [{"decision": "PENDING_SELL_FILLED_POSITION_CLOSED", "filled_size": 3, "avg_price": 1.1,
                                            "realized_pnl_quote": 0.2, "pending_order": {"order_id": "L1", "side": "SELL", "product_id": "ETH-USDC"}}]}
        texts = [t for _, t in alerts.messages_for(result)]
        self.assertTrue(any("Daily loss limit reached" in t for t in texts))
        self.assertTrue(any("PENDING_SELL_FILLED_POSITION_CLOSED: SELL ETH-USDC" in t for t in texts))

    def test_no_url_means_no_network_calls(self) -> None:
        with self.env(""):
            self.assertEqual(alerts.notify_result(self.cfg, SELL), 0)
        self.post.assert_not_called()

    def test_payload_formats(self) -> None:
        with self.env("https://discord.com/api/webhooks/1/x"):
            alerts.notify_result(self.cfg, SELL)
        self.assertIn("content", self.post.call_args.kwargs["json"])
        with self.env("https://hooks.slack.com/services/x"):
            alerts.notify_result(self.cfg, {**SELL, "order": {"success": True, "success_response": {"order_id": "s2"}}})
        self.assertIn("text", self.post.call_args.kwargs["json"])
        with self.env("https://ntfy.sh/my-topic"):
            alerts.notify_result(self.cfg, {**SELL, "order": {"success": True, "success_response": {"order_id": "n3"}}})
        self.assertIn(b"SELL SOL-USDC", self.post.call_args.kwargs["data"])

    def test_repeating_conditions_are_suppressed_but_new_orders_are_not(self) -> None:
        cond = {"decision": "CONFIG_INVALID", "config_check": {"errors": ["'stop_loss_pct' is 3.5"]}}
        with self.env("https://example.test/hook"):
            self.assertEqual(alerts.notify_result(self.cfg, cond), 1)
            self.assertEqual(alerts.notify_result(self.cfg, cond), 0)
            self.assertEqual(alerts.notify_result(self.cfg, SELL), 1)
            self.assertEqual(alerts.notify_result(self.cfg, {**SELL, "order": {"success": True, "success_response": {"order_id": "def"}}}), 1)

    def test_webhook_failure_never_raises_and_is_retried(self) -> None:
        self.post.side_effect = ConnectionError("down")
        with self.env("https://example.test/hook"):
            self.assertEqual(alerts.notify_result(self.cfg, SELL), 0)
        self.post.side_effect = None
        with self.env("https://example.test/hook"):
            self.assertEqual(alerts.notify_result(self.cfg, SELL), 1)

    def test_bot_crash_sends_an_alert_and_still_fails(self) -> None:
        cfg_path = self.tmp / "config.json"
        example = json.loads((Path(__file__).resolve().parent / "config.example.json").read_text())
        example.update({"state_path": str(self.tmp / "state" / "state.json"), "env_file": str(self.tmp / "none.env")})
        cfg_path.write_text(json.dumps(example))
        with self.env("https://example.test/hook"), \
                patch.object(bot, "run", side_effect=RuntimeError("coinbase 503")), \
                patch.object(sys, "argv", ["coinbase_spot_bot.py", "--config", str(cfg_path)]):
            with self.assertRaises(RuntimeError):
                bot.main()
        self.assertIn("run failed: RuntimeError: coinbase 503", self.post.call_args.kwargs["json"]["text"])


if __name__ == "__main__":
    unittest.main()


class ReviewFixAlertTests(AlertTests):
    def test_blocked_stop_exit_behind_a_resting_limit_alerts(self) -> None:
        result = {"decision": "PENDING_CANCEL_FAILED", "proposed_order": {"product_id": "SOL-USDC"}, "pending_cancel_error": "could not cancel L1"}
        [(_, text)] = alerts.messages_for(result)
        self.assertIn("SOL-USDC", text)
        self.assertIn("could not cancel", text)

    def test_broken_config_json_alerts_before_failing(self) -> None:
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text('{"active_trading": false,}')  # trailing comma
        with self.env("https://example.test/hook"), \
                patch.object(sys, "argv", ["coinbase_spot_bot.py", "--config", str(cfg_path)]), \
                patch.object(bot, "ROOT", self.tmp):
            with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
                bot.main()
        self.assertIn("not valid JSON", self.post.call_args.kwargs["json"]["text"])


class FormatDetectionTests(unittest.TestCase):
    def test_detection_uses_the_real_host(self) -> None:
        self.assertEqual(alerts.detect_format("https://discord.com/api/webhooks/1/abc"), "discord")
        self.assertEqual(alerts.detect_format("https://hooks.slack.com/services/T/B/x"), "slack")
        self.assertEqual(alerts.detect_format("https://ntfy.sh/my-topic"), "ntfy")
        self.assertEqual(alerts.detect_format("https://example.test/hook"), "json")

    def test_lookalike_urls_are_not_misdetected(self) -> None:
        for url in ("https://evil.test/?u=hooks.slack.com", "https://hooks.slack.com.evil.test/x",
                    "https://evil.test/discord.com/api/webhooks/1", "https://my-ntfy.example/topic"):
            self.assertEqual(alerts.detect_format(url), "json", url)
