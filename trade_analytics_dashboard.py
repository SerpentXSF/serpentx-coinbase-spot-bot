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

ROOT = Path(os.getenv('COINBASE_BOT_ROOT', Path(__file__).resolve().parent)).resolve()
sys.path.insert(0, str(ROOT))
import coinbase_spot_bot as bot  # noqa: E402

CONFIG = ROOT / os.getenv('COINBASE_BOT_CONFIG', 'config.json')
STATE = ROOT / 'state.json' if (ROOT / 'state.json').exists() else ROOT / 'state' / 'state.json'
TRADES = next((p for p in [ROOT / 'trades.jsonl', ROOT / 'logs' / 'trades.jsonl', ROOT / 'logs' / 'runs.jsonl'] if p.exists()), ROOT / 'trades.jsonl')
ANALYSIS = ROOT / 'analysis' / 'usdc_pairs_latest.json'
CACHE = ROOT / 'analysis' / 'order_cache.json'
SNAPSHOTS = ROOT / 'analysis' / 'analytics_snapshots.jsonl'


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
            queues[product].append({**o, 'remaining_size': size, 'remaining_cost': fnum(o['net_cash'])})
            continue
        remaining = size
        proceeds_total = fnum(o['net_cash'])
        proceeds_per_unit = proceeds_total / size if size > 0 else 0.0
        while remaining > 1e-12 and queues[product]:
            buy = queues[product][0]
            qty = min(remaining, fnum(buy['remaining_size']))
            buy_unit_cost = fnum(buy['remaining_cost']) / fnum(buy['remaining_size']) if fnum(buy['remaining_size']) > 0 else 0.0
            cost = qty * buy_unit_cost
            proceeds = qty * proceeds_per_unit
            pnl = proceeds - cost
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
                'fees': fnum(buy.get('fees')) * (qty / size if size > 0 else 1.0) + fnum(o.get('fees')) * (qty / size if size > 0 else 1.0),
            })
            buy['remaining_size'] = fnum(buy['remaining_size']) - qty
            buy['remaining_cost'] = fnum(buy['remaining_cost']) - cost
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
            'action': r.get('action'),
            'price': fnum(r.get('price')),
            'change_24h': change,
            'rsi': fnum(r.get('rsi')),
            'quote_volume_24h_usdc': fnum(r.get('quote_volume_24h_usdc')),
            'avg_15m_range_pct': fnum(r.get('avg_15m_range_pct')),
            'reasons': reasons[:5],
            'cautions': cautions[:5],
            'blocked': blocked,
            'cooldown': cooldown,
        })
    out.sort(key=lambda x: (not x['blocked'], not x['cooldown'], x['score'], x['change_24h'], x['quote_volume_24h_usdc']), reverse=True)
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
    open_positions = enrich_open_positions(state, cfg)
    equity_curve = []
    running = 0.0
    for t in sorted(closed, key=lambda x: x.get('exit_ts') or ''):
        running += fnum(t.get('pnl'))
        equity_curve.append({'ts': t.get('exit_ts'), 'pnl': running, 'trade_pnl': fnum(t.get('pnl')), 'product_id': t.get('product_id')})

    # Provider/API presence only; never values.
    env = bot.env_present()
    summary = {
        'generated_at': utc_now(),
        'strategy': cfg.get('strategy_name'),
        'risk': {
            'take_profit_pct': fnum(cfg.get('take_profit_pct')),
            'stop_loss_pct': fnum(cfg.get('stop_loss_pct')),
            'soft_invalidation_pct': fnum(cfg.get('soft_invalidation_pct')),
            'trailing_stop_enabled': bool(cfg.get('trailing_stop_enabled')),
            'trailing_activation_pct': fnum(cfg.get('trailing_activation_pct')),
            'trailing_drawdown_pct': fnum(cfg.get('trailing_drawdown_pct')),
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
        'total_fees': sum(fnum(o.get('fees')) for o in order_rows),
        'best_asset': max(assets, key=lambda x: x['pnl']) if assets else None,
        'worst_asset': min(assets, key=lambda x: x['pnl']) if assets else None,
        'asset_stats': sorted(assets, key=lambda x: x['pnl'], reverse=True),
        'open_positions': open_positions,
        'open_lots_from_orders': open_lots,
        'top_next_assets': top_next_assets(cfg, 5),
        'closed_round_trips': closed[-50:],
        'equity_curve': equity_curve,
        'latest_analysis_at': load_json(ANALYSIS, {}).get('generated_at'),
    }
    return summary


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SerpentX Coinbase Trade Analytics</title>
<style>
:root{--bg:#070b12;--panel:#101827;--panel2:#162235;--text:#e8f0ff;--muted:#8fa3bf;--green:#26d07c;--red:#ff5b6e;--yellow:#ffd166;--blue:#59a6ff;--border:#243247}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -10%,#162a52 0,#070b12 32%),var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}.wrap{max-width:1280px;margin:0 auto;padding:24px}h1{margin:0 0 4px;font-size:28px}.sub{color:var(--muted);margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{background:linear-gradient(180deg,var(--panel),#0c1320);border:1px solid var(--border);border-radius:16px;padding:16px;box-shadow:0 12px 30px #0005}.metric .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.metric .val{font-size:28px;font-weight:800;margin-top:8px}.green{color:var(--green)}.red{color:var(--red)}.yellow{color:var(--yellow)}.blue{color:var(--blue)}.span2{grid-column:span 2}.span4{grid-column:span 4}table{width:100%;border-collapse:collapse}th,td{padding:9px 8px;border-bottom:1px solid #233047;text-align:left}th{color:var(--muted);font-size:12px;text-transform:uppercase}tr:hover{background:#ffffff08}.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#22304a;color:#b7c8e5;font-size:12px}.blocked{background:#3a1f2a;color:#ffb3bf}.ok{background:#173527;color:#9af2c0}.svgbox{width:100%;height:260px;background:#08101d;border:1px solid #223047;border-radius:12px}.note{color:var(--muted);font-size:12px}.small{font-size:12px;color:var(--muted)}@media(max-width:900px){.grid{grid-template-columns:1fr}.span2,.span4{grid-column:span 1}}
</style></head><body><div class="wrap"><h1>SerpentX Coinbase Trade Analytics</h1><div class="sub" id="sub">Loading…</div>
<div class="grid">
 <div class="card metric"><div class="label">Realized P/L</div><div class="val" id="pnl">—</div><div class="small" id="roi"></div></div>
 <div class="card metric"><div class="label">Closed trades</div><div class="val" id="closed">—</div><div class="small" id="fees"></div></div>
 <div class="card metric"><div class="label">Win rate</div><div class="val green" id="winrate">—</div><div class="small" id="wins"></div></div>
 <div class="card metric"><div class="label">Loss rate</div><div class="val red" id="lossrate">—</div><div class="small" id="losses"></div></div>
 <div class="card span2"><h3>Equity Curve</h3><svg id="chart" class="svgbox" viewBox="0 0 800 260" preserveAspectRatio="none"></svg><div class="note">Cumulative realized P/L after actual Coinbase fees where available.</div></div>
 <div class="card"><h3>Best Asset</h3><div id="best">—</div></div>
 <div class="card"><h3>Worst Asset</h3><div id="worst">—</div></div>
 <div class="card span2"><h3>Open Positions</h3><div id="openpos">—</div></div>
 <div class="card span2"><h3>Risk Settings</h3><div id="risk">—</div></div>
 <div class="card span4"><h3>Top 5 Next Possible Trade Assets</h3><table><thead><tr><th>Asset</th><th>Score</th><th>Price</th><th>24h</th><th>RSI</th><th>Vol</th><th>Status</th><th>Reasons</th></tr></thead><tbody id="top5"></tbody></table><div class="note">These are candidates from the latest scan/context filters — not guaranteed profitable trades and not live order instructions.</div></div>
 <div class="card span2"><h3>Asset Performance</h3><table><thead><tr><th>Asset</th><th>P/L</th><th>Trades</th><th>W/L</th><th>Fees</th></tr></thead><tbody id="assets"></tbody></table></div>
 <div class="card span2"><h3>Recent Closed Round Trips</h3><table><thead><tr><th>Exit</th><th>Asset</th><th>P/L</th><th>%</th><th>Reason</th></tr></thead><tbody id="recent"></tbody></table></div>
</div></div><script>
const fmt=(n,d=2)=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d});
const pct=n=>fmt((n||0)*100,1)+'%'; const cls=n=>(n||0)>=0?'green':'red';
function money(n){return (n||0)>=0?'+$'+fmt(n):'-$'+fmt(Math.abs(n));}
function drawCurve(points){const svg=document.getElementById('chart'); svg.innerHTML=''; const W=800,H=260,P=24;if(!points.length){svg.innerHTML='<text x="30" y="130" fill="#8fa3bf">No closed trades yet</text>';return;}const vals=points.map(p=>p.pnl);let min=Math.min(...vals,0),max=Math.max(...vals,0);if(max===min){max+=1;min-=1;}const x=i=>P+(points.length===1?0:i*(W-2*P)/(points.length-1));const y=v=>H-P-(v-min)*(H-2*P)/(max-min);let d=points.map((p,i)=>(i?'L':'M')+x(i)+','+y(p.pnl)).join(' ');svg.innerHTML=`<line x1="${P}" y1="${y(0)}" x2="${W-P}" y2="${y(0)}" stroke="#33445f"/><path d="${d}" fill="none" stroke="#59a6ff" stroke-width="3"/><text x="${P}" y="20" fill="#8fa3bf">${money(max)}</text><text x="${P}" y="${H-8}" fill="#8fa3bf">${money(min)}</text>`;}
function assetBox(a){if(!a)return '—'; return `<div class="val ${cls(a.pnl)}">${money(a.pnl)}</div><div>${a.product_id}</div><div class="small">${a.trades} trades · ${a.wins}W/${a.losses}L · fees $${fmt(a.fees)}</div>`}
async function load(){const r=await fetch('/api/summary');const s=await r.json();document.getElementById('sub').textContent=`Updated ${s.generated_at} · latest scan ${s.latest_analysis_at||'n/a'} · APIs: CG ${s.api_presence.COINGECKO_API_KEY?'✓':'—'} / CMC ${s.api_presence.COINMARKETCAP_API_KEY?'✓':'—'} / Birdeye ${s.api_presence.BIRDEYE_API_KEY?'✓':'—'}`;document.getElementById('pnl').className='val '+cls(s.realized_pnl);document.getElementById('pnl').textContent=money(s.realized_pnl);document.getElementById('roi').textContent='Return on deployed: '+pct(s.return_on_deployed);document.getElementById('closed').textContent=s.closed_trades;document.getElementById('fees').textContent='Fees: $'+fmt(s.total_fees);document.getElementById('winrate').textContent=pct(s.win_rate);document.getElementById('wins').textContent=s.wins+' wins';document.getElementById('lossrate').textContent=pct(s.loss_rate);document.getElementById('losses').textContent=s.losses+' losses';document.getElementById('best').innerHTML=assetBox(s.best_asset);document.getElementById('worst').innerHTML=assetBox(s.worst_asset);drawCurve(s.equity_curve||[]);
 const rsk=s.risk;document.getElementById('risk').innerHTML=`TP <b>${pct(rsk.take_profit_pct)}</b><br>Stop <b>${pct(rsk.stop_loss_pct)}</b><br>Soft invalidation <b>${pct(rsk.soft_invalidation_pct)}</b><br>Trailing: <b>${rsk.trailing_stop_enabled?'on':'off'}</b> after ${pct(rsk.trailing_activation_pct)}, drawdown ${pct(rsk.trailing_drawdown_pct)}`;
 document.getElementById('openpos').innerHTML=(s.open_positions||[]).length?(s.open_positions||[]).map(p=>`<div><b>${p.product_id}</b> entry ${fmt(p.entry_price,6)} current ${fmt(p.current_price,6)} <span class="${cls(p.unrealized_pnl_pct)}">${pct(p.unrealized_pnl_pct)}</span></div>`).join(''):'No bot-managed open positions';
 document.getElementById('top5').innerHTML=(s.top_next_assets||[]).map(a=>`<tr><td><b>${a.product_id}</b></td><td>${a.score}</td><td>${fmt(a.price,6)}</td><td class="${cls(a.change_24h)}">${fmt(a.change_24h,1)}%</td><td>${fmt(a.rsi,1)}</td><td>$${fmt(a.quote_volume_24h_usdc,0)}</td><td><span class="pill ${a.blocked||a.cooldown?'blocked':'ok'}">${a.blocked?'blocked':a.cooldown?'cooldown':'candidate'}</span></td><td>${(a.reasons||[]).slice(0,2).join('; ')} ${(a.cautions||[]).length?'<span class="yellow">⚠ '+a.cautions[0]+'</span>':''}</td></tr>`).join('');
 document.getElementById('assets').innerHTML=(s.asset_stats||[]).map(a=>`<tr><td>${a.product_id}</td><td class="${cls(a.pnl)}">${money(a.pnl)}</td><td>${a.trades}</td><td>${a.wins}/${a.losses}</td><td>$${fmt(a.fees)}</td></tr>`).join('');
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
        f.write(json.dumps({k: data[k] for k in ['generated_at','realized_pnl','closed_trades','wins','losses','win_rate','loss_rate','best_asset','worst_asset','top_next_assets']}, default=str) + '\n')
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
