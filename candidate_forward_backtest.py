#!/usr/bin/env python3
"""Small forward-return backtest for Coinbase USDC scan candidates.

Reads saved analysis/usdc_pairs_*.json snapshots, samples top-ranked candidates,
and estimates forward 1h/3h/6h returns from Coinbase public candles. This is an
edge-research tool only; it does not trade.
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
ROOT=Path(os.getenv('COINBASE_BOT_ROOT', Path(__file__).resolve().parent)).resolve()
sys.path.insert(0,str(ROOT))
import coinbase_spot_bot as bot  # noqa: E402
OUT=ROOT/'analysis'/'candidate_forward_returns.json'

def dtparse(s:str)->datetime:
    return datetime.fromisoformat(str(s).replace('Z','+00:00')).astimezone(timezone.utc)

def f(x, d=0.0):
    try: return float(x)
    except Exception: return d

def price_near(product:str, target:datetime)->float:
    start=int((target-timedelta(minutes=30)).timestamp())
    end=int((target+timedelta(minutes=30)).timestamp())
    data=bot.public_get(f'/api/v3/brokerage/market/products/{product}/candles', {'granularity':'FIFTEEN_MINUTE','start':start,'end':end})
    candles=data.get('candles') or []
    best=None; bestdist=10**18
    for c in candles:
        ts=datetime.fromtimestamp(int(c.get('start',0)), tz=timezone.utc)
        dist=abs((ts-target).total_seconds())
        if dist<bestdist:
            best=c; bestdist=dist
    return f((best or {}).get('close'))

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--snapshots',type=int,default=12); ap.add_argument('--top',type=int,default=5)
    args=ap.parse_args()
    files=sorted([p for p in (ROOT/'analysis').glob('usdc_pairs_*.json') if p.name!='usdc_pairs_latest.json'])[-args.snapshots:]
    rows=[]
    cache={}
    for path in files:
        data=json.loads(path.read_text())
        gen=dtparse(data.get('generated_at'))
        if gen+timedelta(hours=6)>datetime.now(timezone.utc):
            continue
        for r in (data.get('rows') or [])[:args.top]:
            product=r.get('product_id'); entry=f(r.get('price'))
            if not product or entry<=0: continue
            rec={'snapshot':path.name,'generated_at':gen.isoformat(),'product_id':product,'entry_price':entry,'score':r.get('score') or r.get('long_score'), 'change_24h':r.get('change_24h'), 'rsi':r.get('rsi')}
            for h in [1,3,6]:
                key=(product, int((gen+timedelta(hours=h)).timestamp()))
                try:
                    px=cache.get(key) or price_near(product, gen+timedelta(hours=h)); cache[key]=px
                    rec[f'return_{h}h_pct']=((px-entry)/entry*100) if px>0 else None
                    rec[f'price_{h}h']=px
                    time.sleep(0.05)
                except Exception as e:
                    rec[f'error_{h}h']=str(e)[:120]
            rows.append(rec)
    def avg(vals):
        vals=[v for v in vals if isinstance(v,(int,float))]
        return sum(vals)/len(vals) if vals else None
    by_product={}
    for r in rows:
        p=r['product_id']; by_product.setdefault(p,[]).append(r)
    summary=[]
    for p,rs in by_product.items():
        summary.append({'product_id':p,'samples':len(rs),'avg_1h_pct':avg([x.get('return_1h_pct') for x in rs]),'avg_3h_pct':avg([x.get('return_3h_pct') for x in rs]),'avg_6h_pct':avg([x.get('return_6h_pct') for x in rs])})
    summary.sort(key=lambda x:(x.get('avg_6h_pct') is not None, x.get('avg_6h_pct') or -999), reverse=True)
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'snapshots_analyzed':len(files),'rows':rows,'by_product':summary}
    OUT.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({'generated_at':out['generated_at'],'samples':len(rows),'top_by_6h':summary[:5]},indent=2))
    return 0
if __name__=='__main__': raise SystemExit(main())
