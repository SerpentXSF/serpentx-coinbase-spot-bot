#!/usr/bin/env python3
"""Stop-loss management must keep running while limit orders are pending."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import coinbase_spot_bot as bot
from test_risk_controls import RunHarness


class PendingOrderExitTests(RunHarness):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["active_trading"] = True
        self.cancelled: list[str] = []
        self.cancel_ok = True

        def cancel(oid):
            if self.cancel_ok:
                self.cancelled.append(oid)
                self.order_details.setdefault(oid, {"status": "CANCELLED", "filled_size": "0"})
                if self.order_details[oid].get("status") == "OPEN":
                    self.order_details[oid]["status"] = "CANCELLED"
            return {"results": [{"order_id": oid, "success": self.cancel_ok}]}

        self.stack.enter_context(patch.object(bot, "cancel_order", side_effect=cancel))
        self.stack.enter_context(patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}))
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "0.9", "filled_value": "9", "total_fees": "0.1"}

    def pos(self, pid="POS1-USDC"):
        return {"product_id": pid, "entry_price": 1.0, "base_size_est": 10.0, "quote_size": 10.0, "entry_fees": 0.12,
                "opened_at": "2026-09-30T00:00:00+00:00"}

    def pending(self, oid, pid, side, **extra):
        self.order_details[oid] = {"status": "OPEN", "filled_size": "0"}
        return {"order_id": oid, "product_id": pid, "side": side, "placed_at": "2999-01-01T00:00:00+00:00", **extra}

    def test_pending_buy_elsewhere_no_longer_blocks_a_stop_loss(self) -> None:
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-BUY", "AAA-USDC", "BUY", quote_size=15)]})
        self.prices["POS1-USDC"] = 0.9
        result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        self.assertEqual(self.placed, [("POS1-USDC", "SELL", None, 10.0)])
        self.assertEqual([r["order_id"] for r in self.read_state()["pending_orders"]], ["L-BUY"])  # untouched

    def test_exit_monitor_also_keeps_protecting(self) -> None:
        import exit_monitor

        (self.tmp / "config.json").write_text(json.dumps(self.cfg))
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-BUY", "AAA-USDC", "BUY", quote_size=15)]})
        self.prices["POS1-USDC"] = 0.9
        with patch.object(exit_monitor, "CONFIG", self.tmp / "config.json"):
            result = exit_monitor.run(live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")

    def test_resting_take_profit_is_left_alone_on_normal_moves(self) -> None:
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-TP", "POS1-USDC", "SELL", base_size=10, reason="TAKE_PROFIT", limit_price="1.07")]})
        self.balances["POS1"] = 0.0  # held by the limit sell
        self.prices["POS1-USDC"] = 1.08  # take-profit level: its own limit handles it
        result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "PENDING_ORDER_OPEN")
        self.assertEqual((self.placed, self.cancelled), ([], []))

    def test_crash_cancels_the_resting_limit_and_market_sells(self) -> None:
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-TP", "POS1-USDC", "SELL", base_size=10, reason="TAKE_PROFIT", limit_price="1.07")]})
        self.balances["POS1"] = 0.0
        self.prices["POS1-USDC"] = 0.9
        result = bot.run(self.cfg, live=True)
        self.assertEqual(self.cancelled, ["L-TP"])
        self.assertEqual(self.placed, [("POS1-USDC", "SELL", None, 10.0)])
        self.assertEqual(result["decision"], "ORDER_SENT")
        state = self.read_state()
        self.assertEqual((state["open_positions"], state["pending_orders"]), ([], []))

    def test_partial_limit_fill_is_booked_before_selling_the_rest(self) -> None:
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-TP", "POS1-USDC", "SELL", base_size=10, reason="TAKE_PROFIT", limit_price="1.07")]})
        self.order_details["L-TP"] = {"status": "OPEN", "filled_size": "4", "average_filled_price": "1.07", "total_fees": "0.05"}
        self.balances["POS1"] = 0.0
        self.prices["POS1-USDC"] = 0.9
        bot.run(self.cfg, live=True)
        self.assertEqual(self.placed, [("POS1-USDC", "SELL", None, 6.0)])
        reasons = [json.loads(l)["reason"] for l in (self.tmp / "trades.jsonl").read_text().splitlines()]
        self.assertEqual(reasons, ["TAKE_PROFIT", "STOP_LOSS"])

    def test_failed_cancel_sends_nothing(self) -> None:
        self.cancel_ok = False
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-TP", "POS1-USDC", "SELL", base_size=10, reason="TAKE_PROFIT", limit_price="1.07")]})
        self.prices["POS1-USDC"] = 0.9
        result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "PENDING_CANCEL_FAILED")
        self.assertEqual(self.placed, [])
        self.assertEqual(len(self.read_state()["open_positions"]), 1)

    def test_preview_mode_cancels_nothing(self) -> None:
        self.write_state({"open_positions": [self.pos()], "pending_orders": [self.pending("L-TP", "POS1-USDC", "SELL", base_size=10, reason="TAKE_PROFIT", limit_price="1.07")]})
        self.prices["POS1-USDC"] = 0.9
        result = bot.run(self.cfg, live=False)
        self.assertEqual(result["decision"], "PREVIEW_ONLY")
        self.assertEqual((self.placed, self.cancelled), ([], []))

    def test_no_new_entries_while_an_order_is_working(self) -> None:
        self.write_state({"pending_orders": [self.pending("L-BUY", "AAA-USDC", "BUY", quote_size=15)]})
        with patch.object(bot, "choose_entry_signal", return_value=bot.Signal("BBB-USDC", 1.0, 9, "BUY", [], 50, 1, 1e7, final_score=9)):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "PENDING_ORDER_OPEN")
        self.assertEqual(self.placed, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
