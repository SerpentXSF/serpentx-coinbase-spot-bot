#!/usr/bin/env python3
"""Tests for exit/state safety, fill capture, the daily loss limit, and the run lock.

Every Coinbase call is mocked, so the "live" paths here never touch a real
account; they only verify what the bot would record and decide.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import coinbase_spot_bot as bot

REPO = Path(__file__).resolve().parent


def signal(product_id: str, price: float = 1.0, action: str = "HOLD_USDC", score: int = 0) -> bot.Signal:
    s = bot.Signal(product_id, price, score, action, [], 50.0, 1.0, 10_000_000)
    s.final_score = score
    return s


class RunHarness(unittest.TestCase):
    """Runs bot.run() against a temp state dir with Coinbase fully mocked."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-bot-risk-"))
        cfg = json.loads((REPO / "config.example.json").read_text())
        for key in list(cfg):
            if key.endswith("_path"):
                cfg[key] = str(self.tmp / Path(cfg[key]).name)
        cfg.update({
            "allowed_products": ["AAA-USDC"],
            "external_context_enabled": False,
            "maker_limit_enabled": False,
            "scale_exit_enabled": False,
            "trailing_stop_enabled": False,
            "breakeven_lock_enabled": False,
            "env_file": str(self.tmp / "missing.env"),
        })
        self.cfg = cfg
        self.prices = {"AAA-USDC": 1.0, "POS1-USDC": 1.0, "POS2-USDC": 1.0}
        self.balances = {"USDC": 100.0, "POS1": 10.0, "POS2": 10.0}
        self.placed: list[tuple] = []
        self.order_details: dict[str, dict] = {}
        self.stack = ExitStack()
        p = lambda *a, **k: self.stack.enter_context(patch.object(bot, *a, **k))  # noqa: E731
        p("safe_score_product", side_effect=lambda cfg, pid: signal(pid, self.prices.get(pid, 1.0)))
        p("env_present", return_value={"COINBASE_API_KEY_NAME": True, "COINBASE_API_PRIVATE_KEY": True})
        p("get_accounts", side_effect=lambda: {"accounts": [{"currency": c, "available_balance": {"value": str(v)}} for c, v in self.balances.items()]})
        p("product_info", side_effect=lambda pid: {"product_id": pid, "price": str(self.prices[pid])})
        p("preview_market", return_value={"preview": True})
        p("structural_exit_reason", return_value=None)
        p("place_market", side_effect=self._place_market)
        p("fetch_order_detail", side_effect=lambda oid: self.order_details.get(oid, {}))
        p("fetch_candles", return_value=[])
        self.stack.enter_context(patch.object(bot.time, "sleep", lambda *_: None))

    def tearDown(self) -> None:
        self.stack.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _place_market(self, product_id, side, quote_size=None, base_size=None):
        oid = f"oid-{len(self.placed) + 1}"
        self.placed.append((product_id, side, quote_size, base_size))
        return {"success": True, "success_response": {"order_id": oid, "product_id": product_id, "side": side}}

    def write_state(self, state: dict) -> None:
        Path(self.cfg["state_path"]).write_text(json.dumps(state))

    def read_state(self) -> dict:
        return json.loads(Path(self.cfg["state_path"]).read_text())

    def two_positions(self) -> list[dict]:
        return [
            {"product_id": "POS1-USDC", "entry_price": 1.0, "base_size_est": 10.0, "quote_size": 10.0, "entry_fees": 0.12},
            {"product_id": "POS2-USDC", "entry_price": 1.0, "base_size_est": 10.0, "quote_size": 10.0, "entry_fees": 0.12},
        ]


class ExitStateSafetyTests(RunHarness):
    def test_preview_exit_signal_keeps_every_other_position(self) -> None:
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS1-USDC"] = 0.9  # -10%: stop-loss fires on the first position
        result = bot.run(self.cfg, live=False)
        self.assertEqual(result["decision"], "PREVIEW_ONLY")
        kept = [p["product_id"] for p in self.read_state()["open_positions"]]
        self.assertEqual(kept, ["POS1-USDC", "POS2-USDC"])
        self.assertEqual(self.placed, [])

    def test_failed_live_exit_keeps_every_position(self) -> None:
        self.cfg["active_trading"] = True
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS1-USDC"] = 0.9
        with patch.object(bot, "place_market", return_value={"success": False, "error_response": {"error": "INSUFFICIENT_FUND"}}), \
                patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_FAILED")
        kept = [p["product_id"] for p in self.read_state()["open_positions"]]
        self.assertEqual(kept, ["POS1-USDC", "POS2-USDC"])

    def test_live_stop_loss_uses_actual_fill_and_records_daily_pnl(self) -> None:
        self.cfg["active_trading"] = True
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS1-USDC"] = 0.9
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "0.88", "filled_value": "8.8", "total_fees": "0.1056"}
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        self.assertEqual(self.placed, [("POS1-USDC", "SELL", None, 10.0)])
        state = self.read_state()
        self.assertEqual([p["product_id"] for p in state["open_positions"]], ["POS2-USDC"])
        # (0.88 - 1.00) * 10 - 0.1056 exit fees - 0.12 entry fees
        self.assertAlmostEqual(result["realized_pnl_quote"], -1.4256, places=6)
        self.assertAlmostEqual(state["daily_realized_pnl"]["realized_quote"], -1.4256, places=6)
        trade = json.loads(Path(self.cfg["trades_log_path"]).read_text().splitlines()[-1])
        self.assertEqual(trade["fill"]["average_filled_price"], 0.88)


class ExitMonitorTests(RunHarness):
    def setUp(self) -> None:
        super().setUp()
        import exit_monitor

        self.monitor = exit_monitor
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(self.cfg))
        self.stack.enter_context(patch.object(exit_monitor, "CONFIG", config_path))

    def test_preview_exit_keeps_every_other_position(self) -> None:
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS1-USDC"] = 0.9
        result = self.monitor.run(live=False)
        self.assertEqual(result["decision"], "PREVIEW_ONLY")
        kept = [p["product_id"] for p in self.read_state()["open_positions"]]
        self.assertEqual(kept, ["POS1-USDC", "POS2-USDC"])

    def test_live_exit_closes_only_the_triggered_position(self) -> None:
        self.cfg["active_trading"] = True
        Path(self.tmp / "config.json").write_text(json.dumps(self.cfg))
        self.write_state({"open_positions": self.two_positions()})
        self.prices["POS2-USDC"] = 1.2  # +20%: take-profit on the second position
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "10", "average_filled_price": "1.19", "filled_value": "11.9", "total_fees": "0.14"}
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = self.monitor.run(live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        state = self.read_state()
        self.assertEqual([p["product_id"] for p in state["open_positions"]], ["POS1-USDC"])
        self.assertAlmostEqual(state["daily_realized_pnl"]["realized_quote"], 1.9 - 0.14 - 0.12, places=6)


class EntryTests(RunHarness):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["active_trading"] = True
        self.buy = signal("AAA-USDC", 2.0, "BUY", 9)
        self.stack.enter_context(patch.object(bot, "choose_entry_signal", return_value=self.buy))

    def test_market_buy_records_actual_fill_not_signal_price(self) -> None:
        self.order_details["oid-1"] = {"status": "FILLED", "filled_size": "7.4", "average_filled_price": "2.02", "filled_value": "14.948", "total_fees": "0.18"}
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")
        pos = self.read_state()["open_positions"][0]
        self.assertEqual(pos["entry_price"], 2.02)
        self.assertEqual(pos["signal_price"], 2.0)
        self.assertEqual(pos["base_size_est"], 7.4)
        self.assertEqual(pos["entry_fees"], 0.18)
        self.assertEqual(pos["entry_fill_source"], "coinbase_order_detail")

    def test_market_buy_falls_back_to_estimate_when_fill_unavailable(self) -> None:
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            bot.run(self.cfg, live=True)
        pos = self.read_state()["open_positions"][0]
        self.assertEqual(pos["entry_price"], 2.0)
        self.assertEqual(pos["entry_fill_source"], "signal_price_estimate")

    def test_daily_loss_limit_blocks_new_entries(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        # 2.5% of the 100 USDC sizing target = 2.50 USDC.
        self.write_state({"daily_realized_pnl": {"date": today, "realized_quote": -2.6, "exits": 2}})
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "DAILY_LOSS_LIMIT_REACHED")
        self.assertEqual(self.placed, [])

    def test_yesterdays_losses_do_not_block_today(self) -> None:
        self.write_state({"daily_realized_pnl": {"date": "2000-01-01", "realized_quote": -50, "exits": 9}})
        with patch.dict(os.environ, {"COINBASE_TRADING_ENABLED": "1"}):
            result = bot.run(self.cfg, live=True)
        self.assertEqual(result["decision"], "ORDER_SENT")


class PnlHelperTests(unittest.TestCase):
    def test_estimate_charges_taker_fees_on_both_sides_when_fill_unknown(self) -> None:
        cfg = {"fee_tracking": {"observed_taker_fee_pct_per_side": 0.01}}
        pos = {"entry_price": 1.0, "base_size_est": 10}
        # (1.1 - 1.0) * 10 = 1.0 gross, minus 0.11 exit fee and 0.10 entry fee
        self.assertAlmostEqual(bot.realized_pnl_quote(cfg, pos, 10, {}, 1.1), 0.79, places=8)

    def test_partial_exit_scales_entry_fees(self) -> None:
        updated = bot.apply_partial_exit_fill({"base_size_est": 10, "quote_size": 10, "entry_fees": 0.2}, 5)
        self.assertAlmostEqual(updated["entry_fees"], 0.1)

    def test_replace_position_removes_only_target(self) -> None:
        a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
        self.assertEqual(bot.replace_position([a, b, c], b, None), [a, c])
        self.assertEqual(bot.replace_position([a, b, c], a, {"id": "a2"}), [{"id": "a2"}, b, c])


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-bot-lock-"))
        self.path = self.tmp / "state" / "run.lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_holder_is_refused_and_lock_is_released(self) -> None:
        with bot.RunLock(self.path):
            with self.assertRaises(bot.RunLocked):
                with bot.RunLock(self.path):
                    pass
        self.assertFalse(self.path.exists())
        with bot.RunLock(self.path):
            pass

    def test_stale_lock_from_crashed_run_is_taken_over(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        with bot.RunLock(self.path, stale_seconds=900) as lock:
            self.assertTrue(lock.acquired)


if __name__ == "__main__":
    unittest.main()
