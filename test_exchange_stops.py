#!/usr/bin/env python3
"""Coinbase-held backstop stops: placement, the balance-hold trap, fills, and recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import alerts
import coinbase_spot_bot as bot
import exchange_stops
from test_risk_controls import RunHarness, signal


class FakeStops:
    """Minimal Coinbase stop-order book for the patched bot functions."""

    def __init__(self, harness: "StopHarness"):
        self.h = harness
        self.previews: list[tuple] = []
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []
        self.preview_errs: object = None
        self.cancel_ok = True

    def preview(self, product_id, base_size, stop_price, limit_price):
        self.previews.append((product_id, base_size, stop_price, limit_price))
        return {"errs": self.preview_errs} if self.preview_errs else {"preview_id": "p"}

    def place(self, product_id, base_size, stop_price, limit_price):
        oid = f"stop-{len(self.placed) + 1}"
        self.placed.append((product_id, base_size, stop_price, limit_price))
        self.h.order_details[oid] = {"status": "OPEN", "filled_size": "0"}
        return {"success": True, "success_response": {"order_id": oid}}

    def cancel(self, oid):
        if self.cancel_ok:
            self.cancelled.append(oid)
            self.h.order_details[oid] = {"status": "CANCELLED", "filled_size": "0"}
        return {"results": [{"order_id": oid, "success": self.cancel_ok}]}


class StopHarness(RunHarness):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.update({"exchange_stop_enabled": True, "active_trading": True})
        self.stops = FakeStops(self)
        for name, fn in (("preview_stop_limit", self.stops.preview), ("place_stop_limit", self.stops.place), ("cancel_order", self.stops.cancel)):
            self.stack.enter_context(patch.object(bot, name, side_effect=fn))
        self.stack.enter_context(patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}))

    def position(self, pid="POS1-USDC", stop_oid=None, **extra):
        pos = {"product_id": pid, "entry_price": 1.0, "base_size_est": 10.0, "quote_size": 10.0, "entry_fees": 0.12,
               "opened_at": "2026-09-30T00:00:00+00:00", **extra}
        if stop_oid:
            pos["exchange_stop"] = {"order_id": stop_oid, "stop_price": 0.955, "limit_price": 0.94545, "base_size": 10.0}
            self.order_details[stop_oid] = {"status": "OPEN", "filled_size": "0"}
        return pos

    def trades(self) -> list[dict]:
        path = Path(self.cfg["trades_log_path"])
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class PlacementTests(StopHarness):
    def test_stop_prices_sit_below_the_bots_own_stop(self) -> None:
        stop, limit = exchange_stops.stop_prices(self.cfg, 100.0)
        self.assertAlmostEqual(stop, 100 * (1 - 0.035 - 0.01))
        self.assertAlmostEqual(limit, stop * 0.99)

    def test_order_config_shape_matches_coinbase_sdk(self) -> None:
        meta = {"base_increment": "0.001", "quote_increment": "0.01", "base_min_size": "0.001"}
        with patch.object(bot, "product_metadata", return_value=meta):
            oc = bot.stop_limit_order_config("SOL-USDC", 0.12345, 140.1234, 138.7777)
        self.assertEqual(oc, {"stop_limit_stop_limit_gtc": {"base_size": "0.123", "limit_price": "138.77",
                                                           "stop_price": "140.12", "stop_direction": "STOP_DIRECTION_STOP_DOWN"}})

    def test_market_buy_places_a_previewed_backstop(self) -> None:
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "7.4", "average_filled_price": "2.0", "filled_value": "14.8", "total_fees": "0.18"}
        self.prices["AAA-USDC"] = 2.0
        with patch.object(bot, "choose_entry_signal", return_value=signal("AAA-USDC", 2.0, "BUY", 9)):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        self.assertEqual(len(self.stops.previews), 1)
        pid, size, stop, limit = self.stops.placed[0]
        self.assertEqual((pid, size), ("AAA-USDC", 7.4))
        self.assertAlmostEqual(stop, 2.0 * 0.955)
        pos = self.read_state()["open_positions"][0]
        self.assertEqual(pos["exchange_stop"]["order_id"], "stop-1")

    def test_preview_mode_only_validates(self) -> None:
        self.write_state({"open_positions": [self.position()]})
        result = bot.run(self.cfg, live=False)
        self.assertEqual(len(self.stops.previews), 1)
        self.assertEqual(self.stops.placed, [])
        self.assertIn("EXCHANGE_STOP_PREVIEW_OK", [e["event"] for e in result["exchange_stop_events"]])

    def test_rejected_preview_places_nothing_and_alerts(self) -> None:
        self.stops.preview_errs = ["INVALID_STOP_DIRECTION"]
        self.write_state({"open_positions": [self.position()]})
        result = bot.run(self.cfg, live=True)
        self.assertEqual(self.stops.placed, [])
        self.assertIn("INVALID_STOP_DIRECTION", self.read_state()["open_positions"][0]["exchange_stop_error"])
        self.assertTrue(any("EXCHANGE_STOP_PLACE_FAILED" in text for _, text in alerts.messages_for(result)))

    def test_disabled_by_default_means_no_stop_calls(self) -> None:
        self.cfg["exchange_stop_enabled"] = False
        self.write_state({"open_positions": [self.position()]})
        bot.run(self.cfg, live=True)
        self.assertEqual((self.stops.previews, self.stops.placed), ([], []))

    def test_no_backstop_when_price_is_already_below_it(self) -> None:
        self.prices["POS1-USDC"] = 0.99  # held (not an exit), but check the guard directly:
        pos = self.position()
        self.prices["POS1-USDC"] = 0.95
        result = {"ts": "2026-09-30T12:00:00+00:00"}
        self.assertFalse(exchange_stops.place(bot, self.cfg, pos, result, live=True))
        self.assertEqual(result["exchange_stop_events"][0]["event"], "EXCHANGE_STOP_SKIPPED_PRICE_BELOW_STOP")


class ExitInteractionTests(StopHarness):
    def test_held_coins_are_still_sold_on_take_profit(self) -> None:
        # The stop holds all 10 coins, so Coinbase reports 0 available.
        self.balances["POS1"] = 0.0
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.prices["POS1-USDC"] = 1.2
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "1.2", "filled_value": "12", "total_fees": "0.14"}
        result = bot.run(self.cfg, live=True)
        self.assertEqual(self.stops.cancelled, ["stop-9"])
        self.assertEqual(self.placed, [("POS1-USDC", "SELL", None, 10.0)])
        self.assertEqual(result["decision"], "ORDER_SENT")
        self.assertEqual(self.read_state()["open_positions"], [])

    def test_stop_that_already_filled_is_not_sold_twice(self) -> None:
        self.stops.cancel_ok = False
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.prices["POS1-USDC"] = 0.9
        # sync() runs first and sees the fill; make it land between sync and exit by filling on cancel.
        original_cancel = self.stops.cancel

        def fill_then_refuse(oid):
            self.order_details[oid] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "0.945", "total_fees": "0.11"}
            return original_cancel(oid)

        self.order_details["stop-9"] = {"status": "OPEN", "filled_size": "0"}
        with patch.object(bot, "cancel_order", side_effect=fill_then_refuse):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "EXCHANGE_STOP_FILLED")
        self.assertEqual(self.placed, [])  # no market sell on top
        self.assertEqual(self.read_state()["open_positions"], [])
        self.assertEqual(self.trades()[-1]["reason"], "EXCHANGE_STOP")

    def test_cancel_failure_skips_the_exit_and_keeps_the_stop(self) -> None:
        self.stops.cancel_ok = False
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.prices["POS1-USDC"] = 1.2
        result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "EXCHANGE_STOP_CANCEL_FAILED")
        self.assertEqual(self.placed, [])
        self.assertEqual(self.read_state()["open_positions"][0]["exchange_stop"]["order_id"], "stop-9")

    def test_partial_exit_re_places_the_stop_for_the_remainder(self) -> None:
        self.cfg["scale_exit_enabled"] = True
        self.balances["POS1"] = 0.0
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.prices["POS1-USDC"] = 1.05  # past scale_exit_take_profit_pct, below take_profit_pct
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "5", "average_filled_price": "1.05", "filled_value": "5.25", "total_fees": "0.06"}
        result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "PARTIAL_EXIT_SENT")
        self.assertEqual(self.stops.cancelled, ["stop-9"])
        self.assertEqual(self.stops.placed[-1][:2], ("POS1-USDC", 5.0))
        pos = self.read_state()["open_positions"][0]
        self.assertEqual((pos["base_size_est"], pos["exchange_stop"]["base_size"]), (5.0, 5.0))


class SyncTests(StopHarness):
    def test_stop_filled_while_bot_was_offline_closes_the_position(self) -> None:
        self.write_state({"open_positions": [self.position(stop_oid="stop-9"), self.position("POS2-USDC", stop_oid="stop-10")]})
        self.order_details["stop-9"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "0.945", "total_fees": "0.11"}
        result = bot.run(self.cfg, live=True)
        state = self.read_state()
        self.assertEqual([p["product_id"] for p in state["open_positions"]], ["POS2-USDC"])
        self.assertAlmostEqual(state["daily_realized_pnl"]["realized_quote"], (0.945 - 1.0) * 10 - 0.11 - 0.12, places=6)
        self.assertIn("POS1-USDC", state["product_cooldowns"])
        self.assertEqual(self.trades()[-1]["reason"], "EXCHANGE_STOP")
        self.assertTrue(any("EXCHANGE_STOP_FILLED" in t for _, t in alerts.messages_for(result)))

    def test_stop_cancelled_by_hand_is_re_placed(self) -> None:
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.order_details["stop-9"] = {"status": "CANCELLED", "filled_size": "0"}
        bot.run(self.cfg, live=True)
        self.assertEqual(self.read_state()["open_positions"][0]["exchange_stop"]["order_id"], "stop-1")

    def test_no_backstop_while_a_limit_sell_holds_the_coins(self) -> None:
        self.write_state({"open_positions": [self.position()],
                          "pending_orders": [{"order_id": "L1", "product_id": "POS1-USDC", "side": "SELL"}]})
        state = self.read_state()
        exchange_stops.sync(bot, self.cfg, state, {"ts": "2026-09-30T12:00:00+00:00"}, live=True)
        self.assertEqual(self.stops.previews, [])

    def test_exit_monitor_reports_a_backstop_fill(self) -> None:
        import exit_monitor

        (self.tmp / "config.json").write_text(json.dumps(self.cfg))
        self.write_state({"open_positions": [self.position(stop_oid="stop-9")]})
        self.order_details["stop-9"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "0.945", "total_fees": "0.11"}
        with patch.object(exit_monitor, "CONFIG", self.tmp / "config.json"):
            result = exit_monitor.run(live=True)
        self.assertEqual(result["decision"], "EXCHANGE_STOP_FILLED")
        self.assertEqual(self.read_state()["open_positions"], [])


if __name__ == "__main__":
    import unittest

    unittest.main()
