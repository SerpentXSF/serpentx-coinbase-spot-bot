#!/usr/bin/env python3
"""The USDC scanner must rank products by the same rules the bot trades by."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analyze_usdc_pairs as scanner
import coinbase_spot_bot as bot


def rising(n: int = 120, step: float = 0.002) -> list[dict]:
    out = []
    for i in range(n):
        c = 1.0 + i * step + (0.004 if i % 3 == 0 else 0.0)
        out.append({"start": str(i), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c})
    return out


def product(pid: str, change: str = "3.0") -> dict:
    return {"product_id": pid, "price": "1.3", "price_percentage_change_24h": change,
            "volume_24h": "20000000", "status": "online"}


class ScannerAlignmentTests(unittest.TestCase):
    def test_scanner_uses_the_bots_configured_timeframes(self) -> None:
        cfg = json.loads(Path("config.example.json").read_text())
        calls = []
        with patch.object(scanner, "candles", side_effect=lambda pid, g, h: calls.append((g, h)) or rising()):
            scanner.score_product(product("AAA-USDC"))
        self.assertIn((cfg["bar_exec"], cfg["lookback_exec_hours"]), calls)
        self.assertIn((cfg["bar_trend"], cfg["lookback_trend_hours"]), calls)
        self.assertNotIn("ONE_HOUR", [g for g, _ in calls])

    def test_scanner_reports_the_bots_own_technical_score(self) -> None:
        exec_c, trend_c = rising(), rising()
        with patch.object(scanner, "candles", side_effect=lambda pid, g, h: exec_c if g == scanner.BAR_EXEC else trend_c):
            row = scanner.score_product(product("AAA-USDC"))
        expected = bot.technical_long_score(scanner.CFG, exec_c, trend_c, 1.3, 3.0, 20_000_000)
        self.assertEqual(row["bot_score"], expected["score"])
        self.assertEqual(row["bot_regime_ok"], not expected["regime_block"])

    def test_bot_eligible_names_rank_ahead_of_higher_scanner_scores(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cb-scan-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        # BLOCKED has the stronger 24h move (higher scanner score) but fails the bot's regime gate.
        verdicts = {"BLOCKED-USDC": {"score": 7, "regime_block": True, "reasons": ["30m regime gate failed"]},
                    "OK-USDC": {"score": 6, "regime_block": False, "reasons": []}}
        current = {}

        def fake_tech(cfg, exec_c, trend_c, px, chg, vol):
            return verdicts[current["pid"]]

        def fake_candles(pid, g, h):
            current["pid"] = pid
            return rising()

        with patch.object(scanner, "OUTDIR", tmp), \
                patch.object(scanner, "products_all", return_value=[product("BLOCKED-USDC", "9.0"), product("OK-USDC", "1.0")]), \
                patch.object(scanner, "ThreadPoolExecutor", _SerialExecutor), \
                patch.object(scanner, "candles", side_effect=fake_candles), \
                patch.object(scanner.bot, "technical_long_score", side_effect=fake_tech), \
                patch("builtins.print"):
            scanner.main()
        out = json.loads((tmp / "usdc_pairs_latest.json").read_text())
        self.assertEqual([r["product_id"] for r in out["top5"]], ["OK-USDC", "BLOCKED-USDC"])
        self.assertTrue(out["top5"][0]["bot_eligible"])
        self.assertEqual(out["timeframes"]["trend"], scanner.BAR_TREND)


class _SerialExecutor:
    """Run scanner work inline so the per-product fakes are deterministic."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        from concurrent.futures import Future

        fut = Future()
        try:
            fut.set_result(fn(*args))
        except Exception as exc:  # pragma: no cover - surfaced by the test
            fut.set_exception(exc)
        return fut


if __name__ == "__main__":
    unittest.main()
