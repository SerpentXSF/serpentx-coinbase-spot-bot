#!/usr/bin/env python3
"""Replay recent history through the bot's real strategy, with fees and slippage.

Unlike candidate_forward_backtest.py (which only asks "did the top candidates
go up afterwards?"), this runs the actual trading rules end to end:

* entries every ``entry_rotator_cadence_minutes`` via the bot's own
  score_product() (5m confirmation, fee guard, regime gates), entry selection,
  dynamic sizing, cooldowns, daily trade cap and daily loss limit;
* exits every 5 minutes via the same exits.manage_exits() the live bot and
  exit monitor use (take-profit, stop-loss, structural soft invalidation,
  breakeven lock, trailing stop, scale-out).

How it stays honest:

* **No lookahead.** The bot's candle/price lookups are pointed at a
  historical market whose clock only ever reveals candles that had fully
  closed at that moment.
* **Costs.** Every fill pays ``--fee-per-side`` (default: the config's
  observed taker fee) and ``--slippage`` against you. Maker/limit orders are
  modelled as taker fills, which is conservative.

Known simplifications (printed with every report): prices are checked on 5m
closes, so intrabar wicks are missed; external news/social/whale context is
unavailable historically (``--context neutral`` treats it as zero); the
watchlist is the fixed product list, not the rotator's changing top 5.

Usage:
    python strategy_backtest.py --days 14
    python strategy_backtest.py --days 30 --products BTC-USDC,ETH-USDC --json
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parent))

import coinbase_spot_bot as bot  # noqa: E402
import exits  # noqa: E402

GRAN_SECONDS = dict(bot.GRANULARITY_SECONDS)
STEP_SECONDS = GRAN_SECONDS["FIVE_MINUTE"]
MAX_CANDLES_PER_REQUEST = 300  # Coinbase caps a candles request at 350


# --------------------------------------------------------------------------- data

class HistoricalMarket:
    """Serves candles and ticker data as they looked at ``self.now``.

    ``candles[product][granularity]`` are Coinbase-style dicts sorted by start.
    Only candles that had *closed* by ``now`` are ever returned.
    """

    def __init__(self, candles: dict[str, dict[str, list[dict[str, Any]]]]):
        self.candles = candles
        self._starts = {p: {g: [int(c["start"]) for c in cs] for g, cs in by_g.items()} for p, by_g in candles.items()}
        self.now = 0

    def fetch_candles(self, product_id: str, granularity: str, lookback_hours: int) -> list[dict[str, Any]]:
        starts = self._starts.get(product_id, {}).get(granularity)
        if not starts:
            return []
        gran = GRAN_SECONDS[granularity]
        lo = bisect.bisect_left(starts, self.now - int(lookback_hours) * 3600)
        hi = bisect.bisect_right(starts, self.now - gran)  # closed: start + gran <= now
        return self.candles[product_id][granularity][lo:hi]

    def product_info(self, product_id: str) -> dict[str, Any]:
        recent = self.fetch_candles(product_id, "FIVE_MINUTE", 24)
        if not recent:
            return {"product_id": product_id, "price": "0", "status": "online"}
        price = bot.fnum(recent[-1]["close"])
        first = bot.fnum(recent[0]["open"]) or price
        volume = sum(bot.fnum(c.get("volume")) for c in recent)
        return {
            "product_id": product_id,
            "price": str(price),
            "price_percentage_change_24h": str((price / first - 1) * 100 if first else 0),
            "volume_24h": str(volume),
            "status": "online",
        }

    def price(self, product_id: str) -> float:
        return bot.fnum(self.product_info(product_id)["price"])


def _cache_dir() -> Path:
    return ROOT / "analysis" / "backtest_cache"


def fetch_history(product_id: str, granularity: str, start: int, end: int) -> list[dict[str, Any]]:
    """Public Coinbase candles for [start, end), paginated and cached on disk."""
    gran = GRAN_SECONDS[granularity]
    start, end = start - start % gran, end - end % gran
    cache = _cache_dir() / f"{product_id}_{granularity}_{start}_{end}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out: dict[int, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + MAX_CANDLES_PER_REQUEST * gran)
        data = bot.public_get(
            f"/api/v3/brokerage/market/products/{product_id}/candles",
            {"start": cursor, "end": chunk_end, "granularity": granularity},
        )
        for c in data.get("candles", []):
            out[int(c["start"])] = c
        cursor = chunk_end
        time.sleep(0.12)  # stay well under public rate limits
    rows = [out[k] for k in sorted(out)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def needed_granularities(cfg: dict[str, Any]) -> dict[str, int]:
    """Granularity -> warm-up hours needed before the first simulated step."""
    need = {
        "FIVE_MINUTE": max(24, int(cfg.get("five_minute_confirmation_lookback_hours", 8))),
        cfg["bar_exec"]: int(cfg["lookback_exec_hours"]),
        cfg["bar_trend"]: int(cfg["lookback_trend_hours"]),
    }
    htf = cfg.get("higher_timeframe_regime_gate") or {}
    if htf.get("enabled"):
        g = str(htf.get("granularity", "ONE_DAY"))
        need[g] = max(need.get(g, 0), int(htf.get("lookback_hours", 720)))
    return need


def load_market(cfg: dict[str, Any], products: list[str], start: int, end: int) -> HistoricalMarket:
    candles: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for product_id in products:
        candles[product_id] = {}
        for gran, warmup_h in needed_granularities(cfg).items():
            candles[product_id][gran] = fetch_history(product_id, gran, start - warmup_h * 3600, end)
    return HistoricalMarket(candles)


# --------------------------------------------------------------------- simulation

@contextlib.contextmanager
def patched(module: Any, **attrs: Any) -> Iterator[None]:
    saved = {k: getattr(module, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(module, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


class SimExchange:
    """Stands in for Coinbase inside exits.manage_exits(): the ``api`` it calls.

    Pure strategy helpers resolve to the real bot module; anything that would
    touch Coinbase or the filesystem is simulated here.
    """

    def __init__(self, sim: "Simulation"):
        self._sim = sim

    def __getattr__(self, name: str) -> Any:
        return getattr(bot, name)

    # --- market access
    def product_info(self, product_id: str) -> dict[str, Any]:
        return self._sim.market.product_info(product_id)

    def live_gates_open(self, cfg: dict[str, Any], live: bool) -> None:
        return None  # the simulation always "trades"

    def preview_market(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    def preview_limit(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    def place_market(self, product_id: str, side: str, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
        return self._sim.fill(product_id, side, quote_size=quote_size, base_size=base_size)

    def market_fill_details(self, order_id: str, *a: Any, **k: Any) -> dict[str, Any]:
        return self._sim.fills.get(order_id, {})

    def append_jsonl(self, path: Any, row: dict[str, Any]) -> None:
        self._sim.trade_rows.append(row)

    # structural exits ask for a fresh position signal only for reporting; skip the cost
    def apply_context_scores(self, cfg: dict[str, Any], sig: Any, cache: dict[str, Any]) -> Any:
        return sig

    def score_product(self, cfg: dict[str, Any], product_id: str) -> Any:
        raise RuntimeError("position re-scoring skipped in backtest")


class Simulation:
    def __init__(self, cfg: dict[str, Any], market: HistoricalMarket, products: list[str], *,
                 start_balance: float, fee_per_side: float, slippage: float, context: str = "neutral"):
        self.cfg = dict(cfg)
        # Model every order as a taker market fill (conservative).
        self.cfg["maker_limit_enabled"] = False
        self.cfg["exchange_stop_enabled"] = False  # the bot's own exits are what we measure
        self.cfg.setdefault("fee_tracking", {})
        self.cfg["fee_tracking"] = {**self.cfg["fee_tracking"], "observed_taker_fee_pct_per_side": fee_per_side}
        self.market = market
        self.products = products
        self.cash = start_balance
        self.start_balance = start_balance
        self.fee = fee_per_side
        self.slippage = slippage
        self.context = context
        self.state: dict[str, Any] = {"open_positions": []}
        self.fills: dict[str, dict[str, Any]] = {}
        self.trade_rows: list[dict[str, Any]] = []
        self.buys: list[dict[str, Any]] = []
        self.equity: list[tuple[int, float]] = []
        self.blocked: dict[str, int] = {}
        self._oid = 0
        self.api = SimExchange(self)

    # ---- exchange
    def fill(self, product_id: str, side: str, *, quote_size: float | None = None, base_size: float | None = None) -> dict[str, Any]:
        px = self.market.price(product_id)
        if px <= 0:
            return {"success": False, "error_response": {"error": "NO_PRICE"}}
        self._oid += 1
        oid = f"sim-{self._oid}"
        if side == "BUY":
            fill_px = px * (1 + self.slippage)
            quote = min(float(quote_size or 0), self.cash)
            fee = quote * self.fee
            size = (quote - fee) / fill_px
            self.cash -= quote
            value = size * fill_px
        else:
            fill_px = px * (1 - self.slippage)
            size = float(base_size or 0)
            value = size * fill_px
            fee = value * self.fee
            self.cash += value - fee
        self.fills[oid] = {"order_id": oid, "status": "FILLED", "filled_size": size, "average_filled_price": fill_px,
                           "filled_value": value, "total_fees": fee}
        return {"success": True, "success_response": {"order_id": oid, "product_id": product_id, "side": side}}

    def balances(self) -> dict[str, float]:
        out = {self.cfg["quote_currency"]: self.cash}
        for p in self.state["open_positions"]:
            base = str(p["product_id"]).split("-")[0]
            out[base] = out.get(base, 0.0) + bot.fnum(p.get("base_size_est"))
        return out

    def mark_to_market(self) -> float:
        return self.cash + sum(bot.fnum(p.get("base_size_est")) * self.market.price(p["product_id"]) for p in self.state["open_positions"])

    # ---- one step of each loop
    def _ts(self) -> str:
        return datetime.fromtimestamp(self.market.now, tz=timezone.utc).isoformat()

    def exit_step(self) -> None:
        positions = bot.normalize_positions(self.state)
        if not positions:
            return
        result = {"ts": self._ts(), "decision": "HOLD_POSITIONS"}
        exits.manage_exits(self.api, self.cfg, self.state, positions, self.balances(), result, live=True, context_cache={})

    def _block(self, reason: str) -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1

    def entry_step(self) -> None:
        cfg, state = self.cfg, self.state
        signals = [bot.safe_score_product(cfg, p) for p in self.products]
        for s in signals:
            if s.action == "ERROR":
                continue
            s.final_score = s.score
            if self.context == "neutral" and not s.risk_block:
                # All providers neutral: final == technical score, judged against the final threshold.
                s.action = "BUY" if s.final_score >= int(cfg.get("final_score_threshold", cfg["score_threshold"])) else "HOLD_USDC"
        signals.sort(key=lambda s: (s.final_score or s.score, s.score, s.change_24h, s.quote_volume), reverse=True)
        positions = bot.normalize_positions(state)
        now = bot.utcnow()
        day_ago = self.market.now - 86400
        # Same rule as count_daily_trades(): successful orders in the last 24h.
        trades_24h = sum(1 for r in self.trade_rows if r.get("_t", 0) >= day_ago and not r.get("failed")) + sum(1 for b in self.buys if b["t"] >= day_ago)
        entry = bot.choose_entry_signal(cfg, signals, positions, state)
        if state.get("cooldown_until") and state["cooldown_until"] > now.isoformat():
            return self._block("cooldown")
        if trades_24h >= int(cfg.get("daily_max_trades", 2)):
            return self._block("daily_trade_limit")
        if bot.daily_loss_limit_status(cfg, state, self.cash, now=now):
            return self._block("daily_loss_limit")
        if len(positions) >= int(cfg.get("max_open_positions", 1)):
            return self._block("max_positions")
        if entry is None:
            return self._block("no_eligible_entry")
        if self.cash < float(cfg["min_quote_balance_to_trade"]):
            return self._block("insufficient_quote")
        quote = bot.dynamic_quote_size(cfg, self.cash, entry)
        if quote < float(cfg["min_quote_balance_to_trade"]):
            return self._block("order_too_small")
        order = self.fill(entry.product_id, "BUY", quote_size=quote)
        if not order.get("success"):
            return self._block("fill_failed")
        fill = self.fills[order["success_response"]["order_id"]]
        lows = [bot.fnum(c.get("low")) for c in self.market.fetch_candles(entry.product_id, cfg["bar_exec"], 4)[-4:] if bot.fnum(c.get("low")) > 0]
        positions.append({
            "product_id": entry.product_id,
            "entry_price": fill["average_filled_price"],
            "signal_price": entry.price,
            "quote_size": fill["filled_value"],
            "base_size_est": fill["filled_size"],
            "entry_fees": fill["total_fees"],
            "opened_at": self._ts(),
            "high_water_price": fill["average_filled_price"],
            "high_water_pnl_pct": 0.0,
            "entry_candle_low": min(lows) if lows else fill["average_filled_price"],
            "entry_score": entry.final_score,
        })
        bot.persist_positions(state, positions)
        state["cooldown_until"] = (bot.utcnow() + timedelta(minutes=float(cfg["cooldown_minutes_after_trade"]))).isoformat()
        self.buys.append({"t": self.market.now, "product_id": entry.product_id, "quote": quote, "fill": fill, "score": entry.final_score})

    def run(self, start: int, end: int) -> None:
        cadence = int(float(self.cfg.get("entry_rotator_cadence_minutes", 90)) * 60)
        clock = lambda: datetime.fromtimestamp(self.market.now, tz=timezone.utc)  # noqa: E731
        with patched(bot, fetch_candles=self.market.fetch_candles, product_info=self.market.product_info, utcnow=clock):
            t = start - start % STEP_SECONDS
            next_entry = t
            while t < end:
                self.market.now = t
                n_rows = len(self.trade_rows)
                self.exit_step()
                for r in self.trade_rows[n_rows:]:
                    r["_t"] = t
                if t >= next_entry:
                    self.entry_step()
                    next_entry = t + cadence
                self.equity.append((t, self.mark_to_market()))
                t += STEP_SECONDS
            # Close anything still open at the end so results are comparable.
            self.market.now = t
            for pos in list(bot.normalize_positions(self.state)):
                order = self.fill(pos["product_id"], "SELL", base_size=bot.fnum(pos.get("base_size_est")))
                fill = self.fills[order["success_response"]["order_id"]]
                self.trade_rows.append({"ts": self._ts(), "_t": t, "side": "SELL", "reason": "END_OF_BACKTEST", "position": pos,
                                        "base_size": fill["filled_size"], "fill": fill,
                                        "realized_pnl_quote": bot.realized_pnl_quote(self.cfg, pos, fill["filled_size"], fill, fill["average_filled_price"])})
            self.state["open_positions"] = []
            self.equity.append((t, self.cash))


# ------------------------------------------------------------------------ report

def summarize(sim: Simulation, start: int, end: int, products: list[str]) -> dict[str, Any]:
    sells = [r for r in sim.trade_rows if r.get("side") == "SELL" and not r.get("failed")]
    # Group partial + final exits into round trips by (product, opened_at).
    trips: dict[tuple[str, str], dict[str, Any]] = {}
    for r in sells:
        pos = r.get("position") or {}
        key = (str(pos.get("product_id")), str(pos.get("opened_at")))
        trip = trips.setdefault(key, {"product_id": key[0], "opened_at": key[1], "pnl": 0.0, "cost": bot.fnum(pos.get("quote_size")) + bot.fnum(pos.get("entry_fees")),
                                      "reasons": [], "closed_at": None})
        trip["pnl"] += bot.fnum(r.get("realized_pnl_quote"))
        trip["reasons"].append(r.get("reason"))
        trip["closed_at"] = r.get("ts")
    rows = list(trips.values())
    wins = [t for t in rows if t["pnl"] > 0]
    losses = [t for t in rows if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    fees = sum(b["fill"]["total_fees"] for b in sim.buys) + sum(bot.fnum((r.get("fill") or {}).get("total_fees")) for r in sells)
    peak, max_dd = sim.start_balance, 0.0
    for _, eq in sim.equity:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak else 0.0)
    reasons: dict[str, int] = {}
    for t in rows:
        final = t["reasons"][-1] if t["reasons"] else "?"
        reasons[final] = reasons.get(final, 0) + 1
    n = len(rows)
    final_equity = sim.equity[-1][1] if sim.equity else sim.cash
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(), "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat()},
        "products": products,
        "assumptions": {
            "fee_per_side": sim.fee, "slippage": sim.slippage, "context": sim.context,
            "start_balance": sim.start_balance, "fills": "all orders modelled as taker market fills",
            "limitations": ["prices checked on 5m closes (intrabar wicks missed)",
                            "external news/social/whale context unavailable historically",
                            "fixed product list, not the rotator's changing top 5"],
        },
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win_quote": gross_win / len(wins) if wins else 0.0,
        "avg_loss_quote": -gross_loss / len(losses) if losses else 0.0,
        "expectancy_per_trade_quote": (gross_win - gross_loss) / n if n else 0.0,
        "expectancy_per_trade_pct": (sum(t["pnl"] / t["cost"] for t in rows if t["cost"]) / n) if n else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0),
        "net_pnl_quote": final_equity - sim.start_balance,
        "return_pct": (final_equity / sim.start_balance - 1) if sim.start_balance else 0.0,
        "fees_paid_quote": fees,
        "max_drawdown_pct": max_dd,
        "exit_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "entries_blocked": dict(sorted(sim.blocked.items(), key=lambda kv: -kv[1])),
        "round_trips": rows,
    }


def format_report(s: dict[str, Any]) -> str:
    a = s["assumptions"]
    pf = s["profit_factor"]
    lines = [
        f"Strategy backtest {s['period']['start'][:16]} -> {s['period']['end'][:16]} UTC on {', '.join(s['products'])}",
        f"Assumptions: fee {a['fee_per_side']*100:.2f}%/side, slippage {a['slippage']*100:.2f}%, context={a['context']}, start {a['start_balance']:.2f}",
        f"Trades: {s['trades']}  wins {s['wins']}  losses {s['losses']}  win rate {s['win_rate']*100:.1f}%",
        f"Avg win {s['avg_win_quote']:+.4f}  avg loss {s['avg_loss_quote']:+.4f}  expectancy/trade {s['expectancy_per_trade_quote']:+.4f} ({s['expectancy_per_trade_pct']*100:+.2f}%)",
        f"Profit factor {'inf' if pf == float('inf') else f'{pf:.2f}'}  net P&L {s['net_pnl_quote']:+.4f} ({s['return_pct']*100:+.2f}%)  fees {s['fees_paid_quote']:.4f}  max drawdown {s['max_drawdown_pct']*100:.2f}%",
        "Exit reasons: " + (", ".join(f"{k}={v}" for k, v in s["exit_reasons"].items()) or "none"),
        "Entry checks blocked by: " + (", ".join(f"{k}={v}" for k, v in s["entries_blocked"].items()) or "none"),
        "Limitations: " + "; ".join(a["limitations"]),
    ]
    return "\n".join(lines)


def run_backtest(cfg: dict[str, Any], market: HistoricalMarket, products: list[str], start: int, end: int, *,
                 start_balance: float, fee_per_side: float, slippage: float, context: str) -> dict[str, Any]:
    sim = Simulation(cfg, market, products, start_balance=start_balance, fee_per_side=fee_per_side, slippage=slippage, context=context)
    sim.run(start, end)
    return summarize(sim, start, end, products)


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay history through the bot's real entry and exit rules.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--products", help="Comma-separated, default: allowed_products from the config")
    ap.add_argument("--start-balance", type=float, help="Default: sizing_quote_balance_target (or 100)")
    ap.add_argument("--fee-per-side", type=float, help="Default: fee_tracking.observed_taker_fee_pct_per_side")
    ap.add_argument("--slippage", type=float, default=0.001, help="Fraction per fill against you (default 0.001 = 0.1%%)")
    ap.add_argument("--context", choices=["neutral", "off"], default="neutral",
                    help="neutral: external context scores 0 and the final threshold applies (live default); off: technical threshold only")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        config_path = ROOT / "config.example.json"
    cfg = bot.load_config(config_path)
    products = [p.strip() for p in args.products.split(",")] if args.products else list(cfg["allowed_products"])
    end = int(time.time()) - int(time.time()) % STEP_SECONDS
    start = end - int(args.days * 86400)
    fee = args.fee_per_side if args.fee_per_side is not None else bot._taker_fee_pct(cfg)  # noqa: SLF001
    balance = args.start_balance or float(cfg.get("sizing_quote_balance_target") or 100)

    market = load_market(cfg, products, start, end)
    report = run_backtest(cfg, market, products, start, end, start_balance=balance, fee_per_side=fee, slippage=args.slippage, context=args.context)
    out = ROOT / "analysis" / "strategy_backtest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "round_trips"}, indent=2, default=str) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
