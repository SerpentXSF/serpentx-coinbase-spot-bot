#!/usr/bin/env python3
"""Local Coinbase USDC rotator trade analytics dashboard.

Serves a zero-dependency dashboard/API on a local port. It reads local bot logs/state
and, when Coinbase credentials are present, reconciles order details from Coinbase
historical orders without printing secrets.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
sys.path.insert(0, str(ROOT))
import coinbase_spot_bot as bot  # noqa: E402

CONFIG = ROOT / 'config.json'
STATE = ROOT / 'state.json'
TRADES = ROOT / 'trades.jsonl'
ANALYSIS = ROOT / 'analysis' / 'usdc_pairs_latest.json'
CACHE = ROOT / 'analysis' / 'order_cache.json'
SNAPSHOTS = ROOT / 'analysis' / 'analytics_snapshots.jsonl'
RUNS = ROOT / 'logs' / 'runs.jsonl'
FORWARD_RETURNS = ROOT / 'analysis' / 'candidate_forward_returns.json'
CRON_JOBS = Path(os.getenv("HERMES_CRON_JOBS", ROOT / "cron" / "jobs.json"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == '':
            return default
        return float(x)
    except Exception:
        return default


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(errors='ignore'))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for raw in path.read_text(errors='ignore').splitlines():
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except Exception:
            continue
    return rows


def latest_jsonl(path: Path) -> dict[str, Any]:
    rows = read_jsonl(path)
    return rows[-1] if rows else {}


def _context_score(ctx: dict[str, Any], name: str) -> int:
    try:
        return int(((ctx.get(name) or {}).get('score') or 0))
    except Exception:
        return 0


def cron_health(jobs_path: Path = CRON_JOBS) -> dict[str, Any]:
    data = load_json(jobs_path, {})
    tracked = []
    wanted = {
        'coinbase_usdc_rotator.py',
        'coinbase_exit_monitor.py',
        'coinbase_dashboard_watchdog.py',
        'coinbase_analytics_snapshot.py',
        'coinbase_candidate_backtest.py',
        'traderjoe_coinbase_recommendations.py',
        'traderjoe_free_signal_collector.py',
    }
    for job in data.get('jobs') or []:
        script = job.get('script') or ''
        name = job.get('name') or ''
        if script not in wanted and 'Coinbase' not in name and 'traderJoe' not in name:
            continue
        tracked.append({
            'name': name,
            'script': script,
            'enabled': bool(job.get('enabled')),
            'state': job.get('state') or '',
            'schedule': job.get('schedule_display') or ((job.get('schedule') or {}).get('display')) or '',
            'last_run_at': job.get('last_run_at'),
            'next_run_at': job.get('next_run_at'),
            'last_status': job.get('last_status') or '',
            'last_error': job.get('last_error') or job.get('last_delivery_error'),
        })
    unhealthy = [j for j in tracked if not j['enabled'] or j['state'] not in {'scheduled', 'running'} or j['last_status'] not in {'', 'ok'}]
    return {'status': 'attention' if unhealthy else 'ok', 'jobs': tracked, 'unhealthy_count': len(unhealthy)}


def decision_snapshot(cfg: dict[str, Any], state: dict[str, Any], latest_run: dict[str, Any]) -> dict[str, Any]:
    positions = bot.normalize_positions(state) if isinstance(state, dict) else []
    env = latest_run.get('env_present') or bot.env_present()
    balances = latest_run.get('balances') or {}
    quote = fnum(balances.get('USDC'))
    daily_used = int(fnum(latest_run.get('daily_trades_used')))
    daily_max = int(fnum(cfg.get('daily_max_trades')))
    max_positions = int(fnum(cfg.get('max_open_positions')))
    blockers: list[str] = []
    decision = str(latest_run.get('decision') or 'unknown')
    if decision and decision not in {'STATUS_ONLY', 'PREVIEW_ONLY', 'ORDER_SENT'}:
        blockers.append(decision.lower().replace('_', ' '))
    if latest_run.get('cooldown_until'):
        blockers.append('cooldown active')
    if daily_max and daily_used >= daily_max:
        blockers.append('daily trade limit reached')
    if max_positions and len(positions) >= max_positions:
        blockers.append('max open positions reached')
    if quote and quote < fnum(cfg.get('min_quote_balance_to_trade')):
        blockers.append('insufficient USDC')
    top = latest_run.get('top') or {}
    action = str(top.get('action') or '')
    if action.startswith('BLOCKED'):
        blockers.append(action.lower().replace('_', ' '))
    for reason in top.get('reasons') or []:
        text = str(reason)
        low = text.lower()
        if any(k in low for k in ['blocked', 'failed', 'limit', 'insufficient', 'overheated', 'fee guard']):
            blockers.append(text)
    blockers = list(dict.fromkeys(blockers))[:10]
    return {
        'latest_run_at': latest_run.get('ts'),
        'decision': decision,
        'top_product': top.get('product_id'),
        'top_action': action,
        'blockers': blockers,
        'daily_trades_used': daily_used,
        'daily_max_trades': daily_max,
        'open_positions_count': len(positions),
        'max_open_positions': max_positions,
        'quote_balance_usdc': quote,
        'live_gates': {
            'active_trading': bool(cfg.get('active_trading')),
            'coinbase_trading_env': bool(env.get('COINBASE_TRADING_ENABLED')),
            'mode': latest_run.get('mode') or '',
        },
    }


def forward_return_summary(path: Path = FORWARD_RETURNS) -> dict[str, Any]:
    data = load_json(path, {})
    rows = [r for r in (data.get('rows') or []) if isinstance(r.get('return_6h_pct'), (int, float))]
    missed = sorted(rows, key=lambda r: fnum(r.get('return_6h_pct')), reverse=True)[:8]
    return {
        'generated_at': data.get('generated_at'),
        'snapshots_analyzed': data.get('snapshots_analyzed'),
        'top_by_6h': (data.get('by_product') or [])[:8],
        'missed_candidates': missed,
    }


def order_id_from_log(row: dict[str, Any]) -> str | None:
    return (((row.get('order') or {}).get('success_response') or {}).get('order_id'))


def fetch_order(order_id: str, cache: dict[str, Any]) -> dict[str, Any]:
    if order_id in cache:
        return cache[order_id]
    try:
        detail = bot.private_request('GET', f'/api/v3/brokerage/orders/historical/{order_id}')
        order = detail.get('order') or detail
        cache[order_id] = {
            'fetched_at': utc_now(),
            'order': order,
        }
        return cache[order_id]
    except Exception as exc:
        cache[order_id] = {'fetched_at': utc_now(), 'error': str(exc)[:300]}
        return cache[order_id]


def normalize_order(log_row: dict[str, Any], cached_detail: dict[str, Any] | None = None) -> dict[str, Any]:
    order = (cached_detail or {}).get('order') or {}
    success = ((log_row.get('order') or {}).get('success_response') or {})
    product = order.get('product_id') or success.get('product_id') or log_row.get('product_id') or (log_row.get('signal') or {}).get('product_id') or (log_row.get('position') or {}).get('product_id') or ''
    side = (order.get('side') or success.get('side') or log_row.get('side') or '').upper()
    order_id = order.get('order_id') or success.get('order_id') or order_id_from_log(log_row) or ''
    filled_value = fnum(order.get('filled_value'))
    fees = fnum(order.get('total_fees'))
    after = fnum(order.get('total_value_after_fees'))
    size = fnum(order.get('filled_size'))
    avg = fnum(order.get('average_filled_price'))

    # Fallback to local estimate when Coinbase detail is absent.
    if not filled_value:
        if side == 'BUY':
            filled_value = fnum(log_row.get('quote_size')) or fnum((log_row.get('position') or {}).get('quote_size'))
        elif side == 'SELL':
            pos = log_row.get('position') or {}
            filled_value = fnum(pos.get('base_size_est')) * fnum(pos.get('entry_price'))
    if not size and avg > 0 and filled_value > 0:
        size = filled_value / avg
    if not avg and size > 0 and filled_value > 0:
        avg = filled_value / size

    if side == 'BUY':
        net_cash = after if after > 0 else filled_value + fees  # cost including fees
    elif side == 'SELL':
        net_cash = after if after > 0 else max(0.0, filled_value - fees)  # proceeds after fees
    else:
        net_cash = after or filled_value

    oc = order.get('order_configuration') or {}
    order_type = order.get('order_type') or log_row.get('order_type') or ('LIMIT' if 'limit_limit_gtc' in oc else ('MARKET' if 'market_market_ioc' in oc else ''))
    post_only = bool(((oc.get('limit_limit_gtc') or {}).get('post_only')))
    return {
        'ts': log_row.get('ts') or order.get('created_time') or '',
        'order_id': order_id,
        'product_id': product,
        'side': side,
        'reason': log_row.get('reason') or '',
        'avg_price': avg,
        'filled_size': size,
        'filled_value': filled_value,
        'fees': fees,
        'net_cash': net_cash,
        'status': order.get('status') or '',
        'source': log_row.get('source') or '',
        'order_type': order_type,
        'time_in_force': order.get('time_in_force') or '',
        'post_only': post_only,
        'number_of_fills': fnum(order.get('number_of_fills')),
        'detail_error': (cached_detail or {}).get('error'),
    }


def pair_round_trips(orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []
    for o in sorted(orders, key=lambda x: x.get('ts') or ''):
        product = o['product_id']
        if not product or o['side'] not in {'BUY', 'SELL'}:
            continue
        size = fnum(o['filled_size'])
        if size <= 0:
            # If no size available, treat one whole logged order as unit size.
            size = 1.0
        if o['side'] == 'BUY':
            queues[product].append({**o, 'remaining_size': size, 'remaining_cost': fnum(o['net_cash']), 'remaining_fees': fnum(o.get('fees'))})
            continue
        remaining = size
        proceeds_total = fnum(o['net_cash'])
        proceeds_per_unit = proceeds_total / size if size > 0 else 0.0
        sell_fee_per_unit = fnum(o.get('fees')) / size if size > 0 else 0.0
        while remaining > 1e-12 and queues[product]:
            buy = queues[product][0]
            buy_remaining_size = fnum(buy['remaining_size'])
            qty = min(remaining, buy_remaining_size)
            buy_unit_cost = fnum(buy['remaining_cost']) / buy_remaining_size if buy_remaining_size > 0 else 0.0
            buy_fee = fnum(buy.get('remaining_fees')) * (qty / buy_remaining_size) if buy_remaining_size > 0 else 0.0
            cost = qty * buy_unit_cost
            proceeds = qty * proceeds_per_unit
            pnl = proceeds - cost
            fees = buy_fee + (qty * sell_fee_per_unit)
            entry_px = fnum(buy.get('avg_price'))
            exit_px = fnum(o.get('avg_price'))
            gross_move_pct = ((exit_px - entry_px) / entry_px) if entry_px > 0 and exit_px > 0 else 0.0
            fee_roundtrip_pct = (fees / cost) if cost > 0 else 0.0
            closed.append({
                'product_id': product,
                'entry_ts': buy.get('ts'),
                'exit_ts': o.get('ts'),
                'buy_order_id': buy.get('order_id'),
                'sell_order_id': o.get('order_id'),
                'reason': o.get('reason'),
                'qty': qty,
                'cost': cost,
                'proceeds': proceeds,
                'pnl': pnl,
                'pnl_pct': (pnl / cost) if cost > 0 else 0.0,
                'gross_move_pct': gross_move_pct,
                'roundtrip_fee_pct': fee_roundtrip_pct,
                'realized_roundtrip_cost_pct': fee_roundtrip_pct,
                'fees': fees,
                'entry_order_type': buy.get('order_type') or '',
                'exit_order_type': o.get('order_type') or '',
                'entry_post_only': bool(buy.get('post_only')),
                'exit_post_only': bool(o.get('post_only')),
                'entry_fills': fnum(buy.get('number_of_fills')),
                'exit_fills': fnum(o.get('number_of_fills')),
            })
            buy['remaining_size'] = fnum(buy['remaining_size']) - qty
            buy['remaining_cost'] = fnum(buy['remaining_cost']) - cost
            buy['remaining_fees'] = max(0.0, fnum(buy.get('remaining_fees')) - buy_fee)
            remaining -= qty
            if fnum(buy['remaining_size']) <= 1e-12:
                queues[product].popleft()
    open_lots = []
    for product, q in queues.items():
        for lot in q:
            if fnum(lot.get('remaining_size')) > 1e-12:
                open_lots.append({**lot, 'product_id': product})
    return closed, open_lots


def enrich_open_positions(state: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    positions = bot.normalize_positions(state) if isinstance(state, dict) else []
    out = []
    for pos in positions:
        product = str(pos.get('product_id') or '')
        entry = fnum(pos.get('entry_price'))
        current = 0.0
        try:
            current = fnum(bot.product_info(product).get('price')) if product else 0.0
        except Exception:
            current = 0.0
        pnl_pct = ((current - entry) / entry) if current > 0 and entry > 0 else 0.0
        out.append({
            **pos,
            'current_price': current,
            'unrealized_pnl_pct': pnl_pct,
            'take_profit_price': entry * (1 + fnum(cfg.get('take_profit_pct'))),
            'stop_loss_price': entry * (1 - fnum(cfg.get('stop_loss_pct'))),
            'soft_invalidation_price': entry * (1 - fnum(cfg.get('soft_invalidation_pct'))),
        })
    return out


def top_next_assets(cfg: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    data = load_json(ANALYSIS, {})
    rows = data.get('rows') or []
    product_cooldowns = load_json(STATE, {}).get('product_cooldowns', {})
    now_s = utc_now()
    out = []
    for r in rows:
        product = r.get('product_id') or ''
        if product_cooldowns.get(product, '') > now_s:
            cooldown = True
        else:
            cooldown = False
        score = int(r.get('score') or r.get('long_score') or 0)
        context = r.get('context') or {}
        context_breakdown = {
            'news': _context_score(context, 'news'),
            'market': _context_score(context, 'market'),
            'social': _context_score(context, 'social'),
            'whale': _context_score(context, 'whale'),
        }
        context_score = int(fnum(r.get('context_score'), sum(context_breakdown.values())))
        final_score = int(fnum(r.get('final_score'), score + context_score))
        change = fnum(r.get('change_24h'))
        blocked = bool(r.get('trading_disabled') or r.get('limit_only') or r.get('cancel_only') or r.get('is_disabled'))
        reasons = list(r.get('reasons') or [])
        cautions = list(r.get('cautions') or [])
        if change >= fnum(cfg.get('overheated_block_pct', 100)):
            cautions.append('blocked by overheated >=100% 24h filter')
            blocked = True
        elif change >= fnum(cfg.get('overheated_require_context_pct', 50)) and score < int(cfg.get('min_final_score_when_overheated', 7)):
            cautions.append('needs stronger score/context due overheated 24h move')
            blocked = True
        out.append({
            'product_id': product,
            'score': score,
            'technical_score': score,
            'context_score': context_score,
            'final_score': final_score,
            'context_breakdown': context_breakdown,
            'risk_block': bool(context.get('risk_block') or r.get('risk_block')),
            'action': r.get('action'),
            'price': fnum(r.get('price')),
            'change_24h': change,
            'rsi': fnum(r.get('rsi')),
            'rsi_divergence_label': r.get('rsi_divergence_label') or 'None',
            'rsi_divergence_signal': r.get('rsi_divergence_signal') or 'none',
            'quote_volume_24h_usdc': fnum(r.get('quote_volume_24h_usdc')),
            'avg_15m_range_pct': fnum(r.get('avg_15m_range_pct')),
            'reasons': reasons[:5],
            'cautions': cautions[:5],
            'blocked': blocked,
            'cooldown': cooldown,
        })
    out.sort(key=lambda x: (not x['blocked'], not x['cooldown'], x['final_score'], x['score'], x['change_24h'], x['quote_volume_24h_usdc']), reverse=True)
    return out[:limit]


def build_summary(refresh_orders: bool = True) -> dict[str, Any]:
    cfg = load_json(CONFIG, {})
    bot.load_dotenv(cfg.get('env_file', ROOT / '.env'))
    cache = load_json(CACHE, {})
    raw_logs = read_jsonl(TRADES)
    order_rows = []
    for row in raw_logs:
        oid = order_id_from_log(row)
        detail = fetch_order(oid, cache) if (oid and refresh_orders and bot.env_present().get('COINBASE_API_KEY_NAME')) else (cache.get(oid) if oid else {})
        order_rows.append(normalize_order(row, detail))
    save_json(CACHE, cache)
    closed, open_lots = pair_round_trips(order_rows)

    total_pnl = sum(fnum(t.get('pnl')) for t in closed)
    total_cost = sum(fnum(t.get('cost')) for t in closed)
    wins = [t for t in closed if fnum(t.get('pnl')) > 0]
    losses = [t for t in closed if fnum(t.get('pnl')) <= 0]
    by_asset: dict[str, dict[str, Any]] = {}
    for t in closed:
        p = t['product_id']
        rec = by_asset.setdefault(p, {'product_id': p, 'pnl': 0.0, 'trades': 0, 'wins': 0, 'losses': 0, 'fees': 0.0})
        rec['pnl'] += fnum(t.get('pnl'))
        rec['fees'] += fnum(t.get('fees'))
        rec['trades'] += 1
        if fnum(t.get('pnl')) > 0:
            rec['wins'] += 1
        else:
            rec['losses'] += 1
    assets = sorted(by_asset.values(), key=lambda x: x['pnl'])
    state = load_json(STATE, {})
    latest_run = latest_jsonl(RUNS)
    open_positions = enrich_open_positions(state, cfg)
    equity_curve = []
    running = 0.0
    daily_pnl_map: dict[str, float] = defaultdict(float)
    for t in sorted(closed, key=lambda x: x.get('exit_ts') or ''):
        trade_pnl = fnum(t.get('pnl'))
        running += trade_pnl
        equity_curve.append({'ts': t.get('exit_ts'), 'pnl': running, 'trade_pnl': trade_pnl, 'product_id': t.get('product_id')})
        day = str(t.get('exit_ts') or '')[:10]
        if day:
            daily_pnl_map[day] += trade_pnl
    daily_pnl = [{'day': day, 'pnl': pnl} for day, pnl in sorted(daily_pnl_map.items())[-14:]]
    gross_profit = sum(fnum(t.get('pnl')) for t in wins)
    gross_loss = abs(sum(fnum(t.get('pnl')) for t in losses))
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (total_pnl / len(closed)) if closed else 0.0
    total_fees = sum(fnum(o.get('fees')) for o in order_rows)
    fee_drag_pct = (total_fees / total_cost) if total_cost > 0 else 0.0
    limit_roundtrips = [t for t in closed if str(t.get('entry_order_type')).upper() == 'LIMIT' or str(t.get('exit_order_type')).upper() == 'LIMIT' or t.get('entry_post_only') or t.get('exit_post_only')]
    recent_limit_roundtrips = limit_roundtrips[-20:]
    fee_verify_cfg = cfg.get('maker_fee_verification') or {}
    min_verify = int(fee_verify_cfg.get('min_roundtrips_before_trusting_geometry', 5)) if isinstance(fee_verify_cfg, dict) else 5
    target_fee = fnum(fee_verify_cfg.get('target_roundtrip_cost_pct')) if isinstance(fee_verify_cfg, dict) else 0.01
    warn_fee = fnum(fee_verify_cfg.get('warning_roundtrip_cost_pct')) if isinstance(fee_verify_cfg, dict) else 0.014
    avg_limit_cost = (sum(fnum(t.get('realized_roundtrip_cost_pct')) for t in recent_limit_roundtrips) / len(recent_limit_roundtrips)) if recent_limit_roundtrips else 0.0
    maker_fee_verification = {
        'enabled': bool(fee_verify_cfg.get('enabled')) if isinstance(fee_verify_cfg, dict) else False,
        'target_roundtrip_cost_pct': target_fee,
        'warning_roundtrip_cost_pct': warn_fee,
        'min_roundtrips_before_trusting_geometry': min_verify,
        'limit_roundtrips': len(limit_roundtrips),
        'recent_limit_roundtrips': len(recent_limit_roundtrips),
        'avg_recent_roundtrip_cost_pct': avg_limit_cost,
        'trusted_geometry': bool(len(limit_roundtrips) >= min_verify and avg_limit_cost > 0 and avg_limit_cost <= warn_fee),
        'status': 'insufficient_limit_roundtrips' if len(limit_roundtrips) < min_verify else ('within_fee_assumption' if avg_limit_cost <= warn_fee else 'fee_too_high_for_geometry'),
    }

    # Provider/API presence only; never values.
    env = bot.env_present()
    summary = {
        'generated_at': utc_now(),
        'strategy': cfg.get('strategy_name'),
        'risk': {
            'take_profit_pct': fnum(cfg.get('take_profit_pct')),
            'stop_loss_pct': fnum(cfg.get('stop_loss_pct')),
            'soft_invalidation_pct': fnum(cfg.get('soft_invalidation_pct')),
            'soft_invalidation_mode': (cfg.get('soft_invalidation') or {}).get('mode') if isinstance(cfg.get('soft_invalidation'), dict) else '',
            'soft_invalidation_hard_cap_pct': fnum((cfg.get('soft_invalidation') or {}).get('hard_cap_pct')) if isinstance(cfg.get('soft_invalidation'), dict) else fnum(cfg.get('soft_invalidation_pct')),
            'trailing_stop_enabled': bool(cfg.get('trailing_stop_enabled')),
            'breakeven_lock_enabled': bool(cfg.get('breakeven_lock_enabled')),
            'breakeven_activation_pct': fnum(cfg.get('breakeven_activation_pct')),
            'breakeven_lock_pct': fnum(cfg.get('breakeven_lock_pct')),
            'trailing_activation_pct': fnum(cfg.get('trailing_activation_pct')),
            'trailing_drawdown_pct': fnum(cfg.get('trailing_drawdown_pct')),
            'estimated_roundtrip_fee_pct': fnum((cfg.get('fee_tracking') or {}).get('estimated_roundtrip_fee_pct')) if isinstance(cfg.get('fee_tracking'), dict) else 0.0,
        },
        'api_presence': env,
        'realized_pnl': total_pnl,
        'return_on_deployed': (total_pnl / total_cost) if total_cost > 0 else 0.0,
        'total_deployed_cost': total_cost,
        'closed_trades': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins) / len(closed)) if closed else 0.0,
        'loss_rate': (len(losses) / len(closed)) if closed else 0.0,
        'total_fees': total_fees,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'expectancy_per_trade': expectancy,
        'fee_drag_pct': fee_drag_pct,
        'maker_fee_verification': maker_fee_verification,
        'recent_limit_roundtrips': recent_limit_roundtrips,
        'best_asset': max(assets, key=lambda x: x['pnl']) if assets else None,
        'worst_asset': min(assets, key=lambda x: x['pnl']) if assets else None,
        'asset_stats': sorted(assets, key=lambda x: x['pnl'], reverse=True),
        'open_positions': open_positions,
        'open_lots_from_orders': open_lots,
        'top_next_assets': top_next_assets(cfg, 5),
        'decision_snapshot': decision_snapshot(cfg, state, latest_run),
        'cron_health': cron_health(),
        'forward_returns': forward_return_summary(),
        'closed_round_trips': closed[-50:],
        'equity_curve': equity_curve,
        'daily_pnl': daily_pnl,
        'latest_analysis_at': load_json(ANALYSIS, {}).get('generated_at'),
    }
    return summary


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>SerpentX Coinbase Trade Analytics</title>
<style>
:root{color-scheme:dark;--bg:#07050a;--bg2:#15070d;--panel:#121016;--panel2:#19131d;--glass:#ffffff0b;--text:#fff3f6;--muted:#b996a2;--accent:#ff3158;--accent2:#ff784f;--green:#31e097;--red:#ff4e68;--yellow:#ffd166;--blue:#79b7ff;--border:#3a1e28;--shadow:#0008;--soft:#ffffff10}
:root[data-theme="purple"]{--bg:#070713;--bg2:#120825;--panel:#111025;--panel2:#1a1433;--glass:#ffffff0c;--text:#f2edff;--muted:#a99ace;--accent:#9b5cff;--accent2:#00d4ff;--green:#35e6a3;--red:#ff5d8f;--yellow:#ffe083;--blue:#7cd7ff;--border:#2d2452;--shadow:#0009;--soft:#ffffff12}
:root[data-theme="blue"]{--bg:#03101f;--bg2:#071a36;--panel:#0b1728;--panel2:#10213a;--glass:#78bfff12;--text:#eef7ff;--muted:#9fc2e8;--accent:#2f8cff;--accent2:#00d4ff;--green:#32e6a0;--red:#ff607d;--yellow:#ffe083;--blue:#7bd2ff;--border:#1c3f68;--shadow:#0009;--soft:#87ceff16}
:root[data-theme="green"]{--bg:#04150d;--bg2:#082414;--panel:#0a1c13;--panel2:#102b1d;--glass:#57ffac10;--text:#edfff5;--muted:#9ed7b8;--accent:#2fe883;--accent2:#b6ff4f;--green:#42f59b;--red:#ff647b;--yellow:#ffe56f;--blue:#7fd9ff;--border:#1d5435;--shadow:#0009;--soft:#57ffac14}
:root[data-theme="yellow"]{--bg:#191100;--bg2:#2a1b02;--panel:#201704;--panel2:#31230a;--glass:#ffd84d12;--text:#fff8df;--muted:#dfc875;--accent:#ffd233;--accent2:#ff9f1c;--green:#3ee092;--red:#ff5b62;--yellow:#ffe27a;--blue:#74c9ff;--border:#66501c;--shadow:#0009;--soft:#fff1a314}
:root[data-theme="orange"]{--bg:#180900;--bg2:#2b1003;--panel:#211006;--panel2:#341907;--glass:#ff8c3d12;--text:#fff2e9;--muted:#e0aa82;--accent:#ff7a1a;--accent2:#ff3158;--green:#39df8f;--red:#ff5069;--yellow:#ffd166;--blue:#79cfff;--border:#653117;--shadow:#0009;--soft:#ffb06b14}
:root[data-theme="pink"]{--bg:#190712;--bg2:#2a0a22;--panel:#22101b;--panel2:#34142b;--glass:#ff7ad912;--text:#fff0fa;--muted:#dfa5cb;--accent:#ff4fb8;--accent2:#ff7a59;--green:#37e69a;--red:#ff5d86;--yellow:#ffe083;--blue:#91c9ff;--border:#61244e;--shadow:#0009;--soft:#ff8bdd14}
:root[data-theme="black"]{--bg:#000;--bg2:#050505;--panel:#090909;--panel2:#111;--glass:#ffffff0a;--text:#f7f7f7;--muted:#a7a7a7;--accent:#f5f5f5;--accent2:#777;--green:#2fe08c;--red:#ff4e68;--yellow:#ffd166;--blue:#79b7ff;--border:#2b2b2b;--shadow:#000;--soft:#ffffff0f}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(circle at 0 -10%,color-mix(in srgb,var(--accent) 35%,transparent),transparent 35%),radial-gradient(circle at 100% 0,color-mix(in srgb,var(--accent2) 24%,transparent),transparent 35%),linear-gradient(135deg,var(--bg),var(--bg2));color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}.wrap{max-width:1440px;margin:0 auto;padding:22px}.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.brand h1{margin:0;font-size:clamp(24px,4vw,38px);line-height:1;font-weight:900;letter-spacing:-.04em}.brand .sub{color:var(--muted);margin-top:8px;max-width:920px}.themebox{position:sticky;top:10px;z-index:8}.themebox details{position:relative}.themebox summary{list-style:none;display:flex;gap:8px;align-items:center;background:var(--glass);border:1px solid var(--border);border-radius:999px;padding:8px 12px;color:var(--text);font-weight:900;cursor:pointer;backdrop-filter:blur(14px);box-shadow:0 12px 32px var(--shadow)}.themebox summary::-webkit-details-marker{display:none}.themeIcon{font-size:18px;line-height:1}.themeLabel{font-size:12px;text-transform:uppercase;letter-spacing:.09em}.themeMenu{position:absolute;right:0;top:calc(100% + 8px);display:grid;grid-template-columns:repeat(2,minmax(104px,1fr));gap:7px;min-width:240px;padding:10px;background:color-mix(in srgb,var(--panel2) 92%,black 8%);border:1px solid var(--border);border-radius:18px;box-shadow:0 20px 55px var(--shadow);backdrop-filter:blur(18px)}.themeMenu button{border:1px solid var(--border);border-radius:999px;padding:9px 11px;color:var(--text);background:var(--glass);font-weight:850;cursor:pointer;text-align:left}.themeMenu button:before{content:"";display:inline-block;width:10px;height:10px;margin-right:8px;border-radius:50%;background:linear-gradient(135deg,var(--sw1),var(--sw2));box-shadow:0 0 16px color-mix(in srgb,var(--sw1) 45%,transparent)}.themeMenu button.active{background:linear-gradient(135deg,var(--accent),var(--accent2));color:var(--text);box-shadow:0 8px 24px color-mix(in srgb,var(--accent) 30%,transparent)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(180deg,color-mix(in srgb,var(--panel) 96%,white 4%),var(--panel));border:1px solid var(--border);border-radius:22px;padding:18px;box-shadow:0 16px 40px var(--shadow);overflow:hidden}.metric{min-height:136px;position:relative}.metric:after{content:"";position:absolute;right:-25px;top:-35px;width:120px;height:120px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--accent) 28%,transparent),transparent 65%)}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.13em;font-weight:800}.val{font-size:clamp(25px,4vw,34px);font-weight:950;letter-spacing:-.04em;margin-top:8px}.small,.note{font-size:12px;color:var(--muted)}h3{margin:0 0 12px;font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:color-mix(in srgb,var(--text) 88%,var(--muted))}.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.blue{color:var(--blue)}.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span6{grid-column:span 6}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}.svgbox{width:100%;height:275px;background:linear-gradient(180deg,#00000020,#00000008);border:1px solid var(--border);border-radius:16px}.miniSvg{width:100%;height:210px}.tablewrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--border);border-radius:16px}table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:10px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}tr:hover{background:var(--soft)}.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;background:var(--soft);color:var(--text);font-size:12px;font-weight:800;white-space:nowrap}.blocked{background:color-mix(in srgb,var(--red) 22%,transparent);color:color-mix(in srgb,var(--red) 85%,white)}.ok{background:color-mix(in srgb,var(--green) 18%,transparent);color:color-mix(in srgb,var(--green) 88%,white)}.candidateGrid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.candidate{border:1px solid var(--border);background:var(--glass);border-radius:16px;padding:12px}.candidate b{font-size:16px}.barrow{display:grid;grid-template-columns:96px 1fr 72px;gap:10px;align-items:center;margin:8px 0}.barrow b{white-space:nowrap;font-size:13px}.bar{height:10px;border-radius:999px;background:#ffffff12;overflow:hidden}.bar>span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2))}.bar.neg>span{background:linear-gradient(90deg,var(--red),#ff9ab0)}.donut{display:grid;place-items:center;min-height:210px}.donut svg{max-width:220px;width:100%;height:auto}.riskgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.riskitem{background:var(--glass);border:1px solid var(--border);border-radius:14px;padding:12px}.mobileCards{display:none}.spark{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 18px var(--accent)}@media(max-width:1050px){.grid{grid-template-columns:repeat(6,1fr)}.span3,.span4{grid-column:span 3}.span5,.span6,.span7,.span8,.span12{grid-column:span 6}.candidateGrid{grid-template-columns:repeat(2,1fr)}}@media(max-width:720px){.wrap{padding:14px}.topbar{flex-direction:column}.themebox{align-self:stretch;justify-content:flex-end;position:static}.grid{grid-template-columns:1fr}.span3,.span4,.span5,.span6,.span7,.span8,.span12{grid-column:span 1}.card{border-radius:18px;padding:14px}.metric{min-height:112px}.candidateGrid{grid-template-columns:1fr}.riskgrid{grid-template-columns:1fr}.desktopOnly{display:none}.mobileCards{display:grid;grid-template-columns:1fr;gap:10px}.svgbox{height:225px}.barrow{grid-template-columns:70px 1fr 60px}body{font-size:13px}}
</style></head><body><div class="wrap">
<header class="topbar"><div class="brand"><h1><span class="spark"></span> SerpentX Coinbase Analytics</h1><div class="sub" id="sub">Loading live summary…</div></div><div class="themebox" aria-label="Theme selector"><details id="themeDetails"><summary><span class="themeIcon">☰</span><span class="themeLabel" id="themeLabel">Red</span></summary><div class="themeMenu" id="themeMenu"><button type="button" data-theme-choice="red" style="--sw1:#ff3158;--sw2:#ff784f">Red</button><button type="button" data-theme-choice="purple" style="--sw1:#9b5cff;--sw2:#00d4ff">Purple</button><button type="button" data-theme-choice="blue" style="--sw1:#2f8cff;--sw2:#00d4ff">Blue</button><button type="button" data-theme-choice="green" style="--sw1:#2fe883;--sw2:#b6ff4f">Green</button><button type="button" data-theme-choice="yellow" style="--sw1:#ffd233;--sw2:#ff9f1c">Yellow</button><button type="button" data-theme-choice="orange" style="--sw1:#ff7a1a;--sw2:#ff3158">Orange</button><button type="button" data-theme-choice="pink" style="--sw1:#ff4fb8;--sw2:#ff7a59">Pink</button><button type="button" data-theme-choice="black" style="--sw1:#f5f5f5;--sw2:#777">Black</button></div></details></div></header>
<section class="grid">
 <div class="card metric span3"><div class="label">Realized P/L</div><div class="val" id="pnl">—</div><div class="small" id="roi"></div></div>
 <div class="card metric span3"><div class="label">Profit Factor</div><div class="val blue" id="pf">—</div><div class="small" id="expectancy"></div></div>
 <div class="card metric span3"><div class="label">Win Rate</div><div class="val green" id="winrate">—</div><div class="small" id="wins"></div></div>
 <div class="card metric span3"><div class="label">Fees / Drag</div><div class="val yellow" id="feesBig">—</div><div class="small" id="feeDrag"></div></div>
 <div class="card span7"><h3>Equity Curve</h3><svg id="chart" class="svgbox" viewBox="0 0 800 275" preserveAspectRatio="none"></svg><div class="note">Cumulative realized P/L after actual Coinbase fees where available.</div></div>
 <div class="card span5"><h3>Win / Loss Mix</h3><div class="donut" id="donut"></div><div class="riskgrid"><div class="riskitem"><div class="label">Avg Win</div><b class="green" id="avgwin">—</b></div><div class="riskitem"><div class="label">Avg Loss</div><b class="red" id="avgloss">—</b></div></div></div>
 <div class="card span4"><h3>Best Asset</h3><div id="best">—</div></div>
 <div class="card span4"><h3>Worst Asset</h3><div id="worst">—</div></div>
 <div class="card span4"><h3>Risk Settings</h3><div id="risk" class="riskgrid">—</div></div>
 <div class="card span6"><h3>Daily P/L — Last 14 Trading Days</h3><svg id="daily" class="miniSvg" viewBox="0 0 700 210" preserveAspectRatio="none"></svg></div>
 <div class="card span6"><h3>Asset P/L Leaderboard</h3><div id="assetBars">—</div></div>
 <div class="card span12"><h3>Top 5 Next Possible Trade Assets</h3><div id="candidateCards" class="candidateGrid"></div><div class="tablewrap desktopOnly"><table><thead><tr><th>Asset</th><th>Score</th><th>Context</th><th>Price</th><th>24h</th><th>RSI</th><th>RSI Div</th><th>Vol</th><th>Status</th><th>Reasons</th></tr></thead><tbody id="top5"></tbody></table></div><div class="note">Candidates from latest scan/context filters — not guaranteed profitable trades and not live order instructions.</div></div>
 <div class="card span6"><h3>Open Positions</h3><div id="openpos">—</div></div>
 <div class="card span6"><h3>Recent Closed Round Trips</h3><div class="tablewrap"><table><thead><tr><th>Exit</th><th>Asset</th><th>P/L</th><th>%</th><th>Reason</th></tr></thead><tbody id="recent"></tbody></table></div></div>
 <div class="card span6"><h3>Bot Health / Cron</h3><div id="bothealth">—</div></div>
 <div class="card span6"><h3>Why No Trade?</h3><div id="decisionbox">—</div></div>
 <div class="card span6"><h3>Maker Fee Verification</h3><div id="feeverify" class="riskgrid">—</div></div>
 <div class="card span6"><h3>Missed Opportunity / Forward Returns</h3><div id="forwardreturns">—</div></div>
</section></div><script>
const fmt=(n,d=2)=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d});
const pct=n=>fmt((n||0)*100,1)+'%'; const cls=n=>(n||0)>=0?'green':'red';
function money(n){return (n||0)>=0?'+$'+fmt(n):'-$'+fmt(Math.abs(n));}
const THEMES=['red','purple','blue','green','yellow','orange','pink','black'];
function titleCase(t){return t.charAt(0).toUpperCase()+t.slice(1)}
function setTheme(t){if(!THEMES.includes(t))t='red';document.documentElement.dataset.theme=t;localStorage.setItem('sxTheme',t);document.getElementById('themeLabel').textContent=titleCase(t);document.querySelectorAll('[data-theme-choice]').forEach(b=>b.classList.toggle('active',b.dataset.themeChoice===t))}
document.querySelectorAll('[data-theme-choice]').forEach(b=>b.onclick=()=>{setTheme(b.dataset.themeChoice);document.getElementById('themeDetails').open=false});setTheme(localStorage.getItem('sxTheme')||'red');
function drawCurve(points){const svg=document.getElementById('chart'); svg.innerHTML=''; const W=800,H=275,P=26;if(!points.length){svg.innerHTML='<text x="30" y="140" fill="currentColor" opacity=".65">No closed trades yet</text>';return;}const vals=points.map(p=>p.pnl);let min=Math.min(...vals,0),max=Math.max(...vals,0);if(max===min){max+=1;min-=1;}const x=i=>P+(points.length===1?0:i*(W-2*P)/(points.length-1));const y=v=>H-P-(v-min)*(H-2*P)/(max-min);let d=points.map((p,i)=>(i?'L':'M')+x(i)+','+y(p.pnl)).join(' ');let area=d+` L${x(points.length-1)},${y(0)} L${P},${y(0)} Z`;svg.innerHTML=`<defs><linearGradient id="eqg" x1="0" x2="0" y1="0" y2="1"><stop stop-color="var(--accent)" stop-opacity=".38"/><stop offset="1" stop-color="var(--accent2)" stop-opacity=".04"/></linearGradient></defs><line x1="${P}" y1="${y(0)}" x2="${W-P}" y2="${y(0)}" stroke="var(--border)"/><path d="${area}" fill="url(#eqg)"/><path d="${d}" fill="none" stroke="var(--accent2)" stroke-width="3.5"/><text x="${P}" y="20" fill="var(--muted)">${money(max)}</text><text x="${P}" y="${H-8}" fill="var(--muted)">${money(min)}</text>`;}
function drawDaily(days){const svg=document.getElementById('daily');svg.innerHTML='';const W=700,H=210,P=24;if(!days.length){svg.innerHTML='<text x="24" y="110" fill="var(--muted)">No daily P/L yet</text>';return;}const vals=days.map(d=>d.pnl);const max=Math.max(...vals.map(Math.abs),1);const base=H/2;const gap=6;const bw=(W-P*2)/days.length-gap;days.forEach((d,i)=>{const h=Math.abs(d.pnl)/max*(H/2-P);const x=P+i*((W-P*2)/days.length)+gap/2;const y=d.pnl>=0?base-h:base;const color=d.pnl>=0?'var(--green)':'var(--red)';svg.innerHTML+=`<rect x="${x}" y="${y}" width="${Math.max(3,bw)}" height="${Math.max(2,h)}" rx="4" fill="${color}"/><text x="${x}" y="${H-6}" fill="var(--muted)" font-size="10">${(d.day||'').slice(5)}</text>`});svg.innerHTML+=`<line x1="${P}" y1="${base}" x2="${W-P}" y2="${base}" stroke="var(--border)"/>`;}
function drawDonut(s){const wins=s.wins||0,losses=s.losses||0,total=Math.max(1,wins+losses);const wp=wins/total,lp=losses/total;const C=2*Math.PI*44;document.getElementById('donut').innerHTML=`<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="44" fill="none" stroke="var(--red)" stroke-width="14" opacity=".45"/><circle cx="60" cy="60" r="44" fill="none" stroke="var(--green)" stroke-width="14" stroke-dasharray="${C*wp} ${C}" transform="rotate(-90 60 60)" stroke-linecap="round"/><text x="60" y="56" text-anchor="middle" fill="var(--text)" font-size="20" font-weight="900">${pct(s.win_rate)}</text><text x="60" y="76" text-anchor="middle" fill="var(--muted)" font-size="10">${wins}W / ${losses}L</text></svg>`;}
function assetBox(a){if(!a)return '—'; return `<div class="val ${cls(a.pnl)}">${money(a.pnl)}</div><div><b>${a.product_id}</b></div><div class="small">${a.trades} trades · ${a.wins}W/${a.losses}L · fees $${fmt(a.fees)}</div>`}
function assetBars(rows){const top=(rows||[]).slice(0,8);if(!top.length)return '—';const max=Math.max(...top.map(a=>Math.abs(a.pnl)),1);return top.map(a=>`<div class="barrow"><b>${a.product_id}</b><div class="bar ${a.pnl<0?'neg':''}"><span style="width:${Math.max(3,Math.abs(a.pnl)/max*100)}%"></span></div><span class="${cls(a.pnl)}">${money(a.pnl)}</span></div>`).join('')}
function riskHtml(r){return `<div class="riskitem"><div class="label">Take Profit</div><b>${pct(r.take_profit_pct)}</b></div><div class="riskitem"><div class="label">Stop Loss</div><b>${pct(r.stop_loss_pct)}</b></div><div class="riskitem"><div class="label">Soft Invalid</div><b>${pct(r.soft_invalidation_pct)}</b></div><div class="riskitem"><div class="label">Trailing</div><b>${r.trailing_stop_enabled?'On':'Off'}</b><div class="small">after ${pct(r.trailing_activation_pct)} / drawdown ${pct(r.trailing_drawdown_pct)}</div></div>`}
function healthHtml(h){const jobs=(h.jobs||[]).slice(0,7);return `<div class="pill ${h.status==='ok'?'ok':'blocked'}">${h.status||'unknown'}</div><div class="small">${h.unhealthy_count||0} job(s) need attention</div>`+jobs.map(j=>`<div class="riskitem"><b>${j.name||j.script}</b><div class="small">${j.last_status||'n/a'} · last ${(j.last_run_at||'').slice(5,16).replace('T',' ')} · next ${(j.next_run_at||'').slice(5,16).replace('T',' ')}</div></div>`).join('')}
function decisionHtml(d){const gates=d.live_gates||{};const blockers=d.blockers||[];return `<div class="riskgrid"><div class="riskitem"><div class="label">Decision</div><b>${d.decision||'unknown'}</b><div class="small">${d.top_product||''} ${d.top_action||''}</div></div><div class="riskitem"><div class="label">USDC</div><b>$${fmt(d.quote_balance_usdc)}</b><div class="small">daily ${d.daily_trades_used||0}/${d.daily_max_trades||0} · positions ${d.open_positions_count||0}/${d.max_open_positions||0}</div></div><div class="riskitem"><div class="label">Live Gates</div><b>${gates.active_trading&&gates.coinbase_trading_env?'Ready':'Check'}</b><div class="small">active ${gates.active_trading?'✓':'—'} · env ${gates.coinbase_trading_env?'✓':'—'} · ${gates.mode||''}</div></div></div><div class="note">${blockers.length?blockers.map(b=>'• '+b).join('<br>'):'No current blocker found in latest run log.'}</div>`}
function feeVerifyHtml(f){return `<div class="riskitem"><div class="label">Status</div><b>${f.status||'n/a'}</b><div class="small">trusted: ${f.trusted_geometry?'yes':'no'}</div></div><div class="riskitem"><div class="label">Limit Round Trips</div><b>${f.limit_roundtrips||0}</b><div class="small">need ${f.min_roundtrips_before_trusting_geometry||0}</div></div><div class="riskitem"><div class="label">Avg Recent Cost</div><b>${pct(f.avg_recent_roundtrip_cost_pct||0)}</b><div class="small">warn above ${pct(f.warning_roundtrip_cost_pct||0)}</div></div>`}
function forwardHtml(fr){const missed=(fr.missed_candidates||[]).slice(0,5);const top=(fr.top_by_6h||[]).slice(0,3);return `<div class="small">Updated ${fr.generated_at||'n/a'}</div>`+missed.map(r=>`<div class="barrow"><b>${r.product_id}</b><div class="bar ${r.return_6h_pct<0?'neg':''}"><span style="width:${Math.min(100,Math.max(3,Math.abs(r.return_6h_pct||0)*4))}%"></span></div><span class="${cls(r.return_6h_pct)}">${fmt(r.return_6h_pct,1)}% 6h</span></div>`).join('')+(top.length?`<div class="note">Best recurring: ${top.map(x=>`${x.product_id} ${fmt(x.avg_6h_pct,1)}%`).join(' · ')}</div>`:'')}
function ctxText(a){const c=a.context_breakdown||{};return `N ${c.news||0} / M ${c.market||0} / S ${c.social||0} / W ${c.whale||0}`}
function candidateCard(a){const div=a.rsi_divergence_label&&a.rsi_divergence_label!=='None'?a.rsi_divergence_label:'No divergence';const divCls=a.rsi_divergence_signal==='bullish'?'green':(a.rsi_divergence_signal==='bearish'?'red':'small');return `<div class="candidate"><b>${a.product_id}</b><div class="small">Final ${a.final_score??a.score} · tech ${a.technical_score??a.score} · ctx ${a.context_score||0}</div><div class="${cls(a.change_24h)}">24h ${fmt(a.change_24h,1)}%</div><div class="small">${ctxText(a)}</div><div class="${divCls}">${div}</div><div class="pill ${a.blocked||a.cooldown||a.risk_block?'blocked':'ok'}">${a.blocked?'blocked':a.cooldown?'cooldown':a.risk_block?'risk block':'candidate'}</div></div>`}
async function load(){const r=await fetch('/api/summary');const s=await r.json();document.getElementById('sub').textContent=`Updated ${s.generated_at} · latest scan ${s.latest_analysis_at||'n/a'} · APIs: CG ${s.api_presence.COINGECKO_API_KEY?'✓':'—'} / CMC ${s.api_presence.COINMARKETCAP_API_KEY?'✓':'—'} / Birdeye ${s.api_presence.BIRDEYE_API_KEY?'✓':'—'}`;document.getElementById('pnl').className='val '+cls(s.realized_pnl);document.getElementById('pnl').textContent=money(s.realized_pnl);document.getElementById('roi').textContent='Return on deployed: '+pct(s.return_on_deployed);document.getElementById('pf').textContent=fmt(s.profit_factor,2);document.getElementById('expectancy').textContent='Expectancy/trade: '+money(s.expectancy_per_trade);document.getElementById('feesBig').textContent='$'+fmt(s.total_fees);document.getElementById('feeDrag').textContent='Fee drag: '+pct(s.fee_drag_pct);document.getElementById('winrate').textContent=pct(s.win_rate);document.getElementById('wins').textContent=`${s.wins} wins · ${s.losses} losses · ${s.closed_trades} closed`;document.getElementById('avgwin').textContent=money(s.avg_win);document.getElementById('avgloss').textContent='-'+money(s.avg_loss).replace('+','');document.getElementById('best').innerHTML=assetBox(s.best_asset);document.getElementById('worst').innerHTML=assetBox(s.worst_asset);drawCurve(s.equity_curve||[]);drawDaily(s.daily_pnl||[]);drawDonut(s);document.getElementById('assetBars').innerHTML=assetBars(s.asset_stats||[]);
 const rsk=s.risk;document.getElementById('risk').innerHTML=riskHtml(rsk);
 document.getElementById('bothealth').innerHTML=healthHtml(s.cron_health||{});
 document.getElementById('decisionbox').innerHTML=decisionHtml(s.decision_snapshot||{});
 document.getElementById('feeverify').innerHTML=feeVerifyHtml(s.maker_fee_verification||{});
 document.getElementById('forwardreturns').innerHTML=forwardHtml(s.forward_returns||{});
 document.getElementById('openpos').innerHTML=(s.open_positions||[]).length?(s.open_positions||[]).map(p=>`<div class="riskitem"><b>${p.product_id}</b><div class="small">entry ${fmt(p.entry_price,6)} · current ${fmt(p.current_price,6)}</div><span class="${cls(p.unrealized_pnl_pct)}">${pct(p.unrealized_pnl_pct)}</span></div>`).join(''):'<div class="note">No bot-managed open positions</div>';
 document.getElementById('candidateCards').innerHTML=(s.top_next_assets||[]).map(candidateCard).join('');
 document.getElementById('top5').innerHTML=(s.top_next_assets||[]).map(a=>{const div=a.rsi_divergence_label&&a.rsi_divergence_label!=='None'?a.rsi_divergence_label:'—';const divCls=a.rsi_divergence_signal==='bullish'?'green':(a.rsi_divergence_signal==='bearish'?'red':'');return `<tr><td><b>${a.product_id}</b></td><td>${a.final_score??a.score}<div class="small">tech ${a.technical_score??a.score}</div></td><td>${ctxText(a)}</td><td>${fmt(a.price,6)}</td><td class="${cls(a.change_24h)}">${fmt(a.change_24h,1)}%</td><td>${fmt(a.rsi,1)}</td><td class="${divCls}">${div}</td><td>$${fmt(a.quote_volume_24h_usdc,0)}</td><td><span class="pill ${a.blocked||a.cooldown||a.risk_block?'blocked':'ok'}">${a.blocked?'blocked':a.cooldown?'cooldown':a.risk_block?'risk block':'candidate'}</span></td><td>${(a.reasons||[]).slice(0,2).join('; ')} ${(a.cautions||[]).length?'<span class="yellow">⚠ '+a.cautions[0]+'</span>':''}</td></tr>`}).join('');
 document.getElementById('recent').innerHTML=(s.closed_round_trips||[]).slice(-10).reverse().map(t=>`<tr><td>${(t.exit_ts||'').slice(5,16).replace('T',' ')}</td><td>${t.product_id}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td>${t.reason||''}</td></tr>`).join('');}
load(); setInterval(load,60000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep logs quiet unless debugging.
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {'/', '/index.html'}:
            self._send(200, HTML.encode(), 'text/html; charset=utf-8')
            return
        if path == '/api/summary':
            try:
                data = build_summary(refresh_orders=True)
                self._send(200, json.dumps(data, default=str).encode(), 'application/json')
            except Exception as exc:
                self._send(500, json.dumps({'error': str(exc), 'generated_at': utc_now()}).encode(), 'application/json')
            return
        if path == '/healthz':
            self._send(200, b'ok', 'text/plain')
            return
        self._send(404, b'not found', 'text/plain')


def write_snapshot() -> dict[str, Any]:
    data = build_summary(refresh_orders=True)
    SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOTS.open('a') as f:
        f.write(json.dumps({k: data[k] for k in ['generated_at','realized_pnl','closed_trades','wins','losses','win_rate','loss_rate','total_fees','fee_drag_pct','maker_fee_verification','best_asset','worst_asset','top_next_assets']}, default=str) + '\n')
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=2048)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--snapshot', action='store_true')
    args = ap.parse_args()
    if args.json:
        print(json.dumps(build_summary(refresh_orders=True), indent=2, default=str))
        return 0
    if args.snapshot:
        s = write_snapshot()
        print(f"snapshot {s['generated_at']} pnl={s['realized_pnl']:.4f} closed={s['closed_trades']}")
        return 0
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SerpentX Coinbase analytics dashboard listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
