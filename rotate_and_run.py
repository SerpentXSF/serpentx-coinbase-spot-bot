#!/usr/bin/env python3
"""Rotate Coinbase USDC watchlist to current top 5 and run the spot bot.

Safety:
- Defaults to preview/status only.
- Live requires --live plus existing bot gates: config active_trading=true and
  COINBASE_TRADING_ENABLED=1 in .env.
- Does not print secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.getenv("COINBASE_BOT_ROOT", Path(__file__).resolve().parent)).resolve()
CONFIG = ROOT / 'config.json'
LATEST = ROOT / 'analysis' / 'usdc_pairs_latest.json'
ANALYZER = ROOT / 'analyze_usdc_pairs.py'
BOT = ROOT / 'coinbase_spot_bot.py'


def load_json(path: Path):
    return json.loads(path.read_text())


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    tmp.replace(path)


def run(cmd, timeout=75):
    return subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='Pass --live to bot; bot gates still apply')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    analysis_proc = run([sys.executable, str(ANALYZER)])
    if analysis_proc.returncode != 0:
        print('ERROR: analyzer failed', file=sys.stderr)
        print(analysis_proc.stderr[-2000:], file=sys.stderr)
        return analysis_proc.returncode

    analysis = load_json(LATEST)
    top5 = [r['product_id'] for r in analysis.get('top5', []) if r.get('product_id')][:5]
    if not top5:
        print('ERROR: no top5 products found', file=sys.stderr)
        return 2

    cfg = load_json(CONFIG)
    old = cfg.get('allowed_products', [])
    cfg['allowed_products'] = top5
    cfg['rotation_source'] = str(LATEST)
    cfg['rotation_last_generated_at'] = analysis.get('generated_at')
    save_json(CONFIG, cfg)

    bot_cmd = [sys.executable, str(BOT), '--json']
    if args.live:
        bot_cmd.append('--live')
    bot_proc = run(bot_cmd)
    if bot_proc.returncode != 0:
        print('ERROR: bot failed', file=sys.stderr)
        print(bot_proc.stderr[-2000:], file=sys.stderr)
        return bot_proc.returncode

    result = json.loads(bot_proc.stdout)
    summary = {
        'generated_at': analysis.get('generated_at'),
        'rotated_from': old,
        'rotated_to': top5,
        'top5_short_candidates': [
            {k: r.get(k) for k in ('product_id', 'short_score', 'long_score', 'directional_bias', 'change_24h', 'rsi')}
            for r in analysis.get('top5_short_candidates', [])[:5]
        ],
        'decision': result.get('decision'),
        'mode': result.get('mode'),
        'env_present': result.get('env_present'),
        'balances': result.get('balances'),
        'top': result.get('top'),
        'proposed_order': result.get('proposed_order'),
    }
    if result.get('decision') in {'ORDER_SENT', 'PREVIEW_ONLY', 'LIVE_BLOCKED_CONFIG_ACTIVE_TRADING_FALSE', 'LIVE_BLOCKED_ENV_TRADING_DISABLED', 'INSUFFICIENT_USDC', 'ORDER_TOO_SMALL'}:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    # In quiet cron mode, print nothing for HOLD/status-only noise unless --json is set.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
