#!/usr/bin/env python3
"""The strategy backtester: no lookahead, exact cash accounting, real exit rules."""
from __future__ import annotations

import json
import math
import random
import unittest
from pathlib import Path

import coinbase_spot_bot as bot
import strategy_backtest as sb

CFG = bot.resolve_runtime_paths(json.loads((Path(__file__).resolve().parent / "config.example.json").read_text()))
T0 = 1_780_000_000 - 1_780_000_000 % 86400  # a UTC midnight


def make_market(paths: dict[str, list[float]]) -> sb.HistoricalMarket:
    """Build 5m/15m/30m candles from a 5m close path per product."""
    candles: dict[str, dict[str, list[dict]]] = {}
    for pid, closes in paths.items():
        five = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            five.append({"start": str(T0 + i * 300), "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c, "volume": "1000"})
        by = {"FIVE_MINUTE": five}
        for gran, k in (("FIFTEEN_MINUTE", 3), ("THIRTY_MINUTE", 6)):
            agg = []
            for j in range(0, len(five) - k + 1, k):
                grp = five[j:j + k]
                agg.append({"start": grp[0]["start"], "open": grp[0]["open"], "high": max(g["high"] for g in grp),
                            "low": min(g["low"] for g in grp), "close": grp[-1]["close"], "volume": "3000"})
            by[gran] = agg
        candles[pid] = by
    return sb.HistoricalMarket(candles)


def trending_path(seed: int, n: int, drift: float, vol: float, start: float = 100.0) -> list[float]:
    rnd = random.Random(seed)
    p, out = start, []
    for i in range(n):
        p *= math.exp(drift + vol * rnd.gauss(0, 1) + 0.004 * math.sin(i / 9))
        out.append(p)
    return out


class BacktestTests(unittest.TestCase):
    def test_market_never_reveals_unclosed_candles(self) -> None:
        market = make_market({"AAA-USDC": trending_path(1, 600, 0.0005, 0.003)})
        for now in range(T0 + 3600, T0 + 600 * 300, 700):
            market.now = now
            for gran in ("FIVE_MINUTE", "FIFTEEN_MINUTE", "THIRTY_MINUTE"):
                rows = market.fetch_candles("AAA-USDC", gran, 48)
                self.assertTrue(all(int(c["start"]) + sb.GRAN_SECONDS[gran] <= now for c in rows))
                if rows:
                    self.assertGreater(int(rows[-1]["start"]) + 2 * sb.GRAN_SECONDS[gran], now)  # and nothing stale

    def test_full_run_trades_and_cash_reconciles_with_bot_pnl(self) -> None:
        n = 5 * 288  # five days of 5m candles
        market = make_market({"UP-USDC": trending_path(7, n, 0.0006, 0.004), "CHOP-USDC": trending_path(11, n, 0.0, 0.006)})
        sim = sb.Simulation(CFG, market, ["UP-USDC", "CHOP-USDC"], start_balance=100.0, fee_per_side=0.012, slippage=0.001)
        start, end = T0 + 2 * 86400, T0 + n * 300
        sim.run(start, end)
        report = sb.summarize(sim, start, end, sim.products)
        self.assertGreater(report["trades"], 0, "scenario should produce trades")
        self.assertEqual(report["trades"], len(sim.buys))
        self.assertEqual(bot.normalize_positions(sim.state), [])
        realized = sum(bot.fnum(r.get("realized_pnl_quote")) for r in sim.trade_rows if r.get("side") == "SELL")
        # The simulated exchange's cash and the bot's own P&L maths must agree.
        self.assertAlmostEqual(sim.cash, 100.0 + realized, places=6)
        self.assertAlmostEqual(report["net_pnl_quote"], realized, places=6)
        self.assertGreater(report["fees_paid_quote"], 0)
        self.assertLessEqual(max(r["cost"] for r in report["round_trips"]), float(CFG["max_quote_per_trade"]) + 1e-9)

    def test_open_position_is_stopped_out_on_a_crash_with_fees(self) -> None:
        path = [100.0] * 400 + [100 - i * 0.5 for i in range(1, 30)]
        market = make_market({"DROP-USDC": path})
        sim = sb.Simulation(CFG, market, ["DROP-USDC"], start_balance=100.0, fee_per_side=0.01, slippage=0.0)
        sim.cash = 80.0
        sim.state["open_positions"] = [{"product_id": "DROP-USDC", "entry_price": 100.0, "base_size_est": 0.2, "quote_size": 20.0,
                                        "entry_fees": 0.2, "high_water_price": 100.0, "entry_candle_low": 99.0,
                                        "opened_at": "2026-01-01T00:00:00+00:00"}]
        sim.cfg["entry_rotator_cadence_minutes"] = 10**9  # exits only
        start = T0 + 400 * 300
        sim.run(start, T0 + len(path) * 300)
        sells = [r for r in sim.trade_rows if r["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertIn(sells[0]["reason"], {"STOP_LOSS", "SOFT_INVALIDATION"})
        self.assertLess(sells[0]["realized_pnl_quote"], -0.2)  # loss plus both fees

    def test_live_bot_functions_are_restored_after_a_backtest(self) -> None:
        originals = (bot.fetch_candles, bot.product_info, bot.utcnow)
        market = make_market({"AAA-USDC": trending_path(3, 700, 0.0004, 0.004)})
        sb.Simulation(CFG, market, ["AAA-USDC"], start_balance=100, fee_per_side=0.012, slippage=0.001).run(T0 + 86400, T0 + 700 * 300)
        self.assertEqual((bot.fetch_candles, bot.product_info, bot.utcnow), originals)

    def test_report_formats(self) -> None:
        market = make_market({"AAA-USDC": trending_path(5, 700, 0.0005, 0.004)})
        sim = sb.Simulation(CFG, market, ["AAA-USDC"], start_balance=100, fee_per_side=0.012, slippage=0.001)
        sim.run(T0 + 86400, T0 + 700 * 300)
        text = sb.format_report(sb.summarize(sim, T0 + 86400, T0 + 700 * 300, ["AAA-USDC"]))
        for part in ("Trades:", "win rate", "expectancy/trade", "max drawdown", "Limitations:"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
