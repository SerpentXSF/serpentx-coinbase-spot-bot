#!/usr/bin/env python3
from __future__ import annotations
import json, math, os, time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BASE='https://api.coinbase.com'
ROOT=Path(os.getenv('COINBASE_BOT_ROOT', Path(__file__).resolve().parent)).resolve()
OUTDIR=ROOT / 'analysis'
OUTDIR.mkdir(parents=True, exist_ok=True)

GRAN = {'FIFTEEN_MINUTE': 900, 'ONE_HOUR': 3600}

def get(path, params=None):
    last=None
    for attempt in range(2):
        r=requests.get(BASE+path, params=params, timeout=6)
        if r.status_code<400:
            return r.json()
        last=r
        if r.status_code in {429,500,502,503,504} and attempt<1:
            time.sleep(1 + attempt)
            continue
        break
    raise RuntimeError(f'{path} {last.status_code} {last.text[:200]}')

def products_all():
    # Public market endpoint can return all visible products.
    data=get('/api/v3/brokerage/market/products', {'limit': 1000})
    return data.get('products', [])

def f(x, d=0.0):
    try: return float(x)
    except Exception: return d

def ema(vals, period):
    if not vals: return 0.0
    k=2/(period+1); out=vals[0]
    for v in vals[1:]: out=v*k + out*(1-k)
    return out

def rsi(vals, period=14):
    if len(vals)<period+1: return 50.0
    gains=[]; losses=[]
    for a,b in zip(vals[-period-1:-1], vals[-period:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    if al==0: return 100.0
    rs=ag/al
    return 100 - 100/(1+rs)

def candles(pid, gran, hours):
    end=int(time.time()); start=end-hours*3600
    try:
        data=get(f'/api/v3/brokerage/market/products/{pid}/candles', {'start': start, 'end': end, 'granularity': gran})
        cs=data.get('candles', [])
        cs.sort(key=lambda c:int(c.get('start',0)))
        return cs
    except Exception as e:
        return []

def score_product(p):
    pid=p.get('product_id')
    px=f(p.get('price'))
    chg=f(p.get('price_percentage_change_24h'))
    vol24=f(p.get('volume_24h'))
    quote_vol=px * vol24 if px > 0 else 0.0
    quote_min=f(p.get('quote_min_size'))
    status=p.get('status') or p.get('trading_disabled')
    exec_c=candles(pid, 'FIFTEEN_MINUTE', 30)
    trend_c=candles(pid, 'ONE_HOUR', 120)
    closes=[f(c.get('close')) for c in exec_c if f(c.get('close'))>0]
    highs=[f(c.get('high')) for c in exec_c if f(c.get('high'))>0]
    lows=[f(c.get('low')) for c in exec_c if f(c.get('low'))>0]
    tcloses=[f(c.get('close')) for c in trend_c if f(c.get('close'))>0]
    reasons=[]; cautions=[]; score=0
    short_reasons=[]; short_cautions=[]; short_score=0
    if len(closes)<30 or len(tcloses)<20 or px<=0:
        return {**base(p), 'score':0, 'long_score':0, 'short_score':0, 'directional_bias':'NONE', 'action':'SKIP', 'reasons':['insufficient candle history'], 'short_reasons':[], 'cautions':cautions}
    e9=ema(closes[-80:],9); e21=ema(closes[-100:],21); e50=ema(closes[-140:],50)
    t50=ema(tcloses[-140:],50); t20=ema(tcloses[-80:],20)
    rrsi=rsi(closes)
    # Volatility: average high-low pct over last 20 15m candles.
    ranges=[]
    for h,l,c in zip(highs[-20:], lows[-20:], closes[-20:]):
        if c: ranges.append((h-l)/c)
    avg_range=sum(ranges)/len(ranges) if ranges else 0
    # Spot strategy scoring: uptrend + momentum + tradability + enough volatility for 3.5% target.
    if e9>e21>e50:
        score+=2; reasons.append('15m EMA stack bullish')
    elif e9>e21:
        score+=1; reasons.append('15m short-term EMA bullish')
    if px>t50:
        score+=1; reasons.append('price above 1H EMA50')
    if t20>t50:
        score+=1; reasons.append('1H trend EMA20 > EMA50')
    if 42<=rrsi<=68:
        score+=1; reasons.append(f'RSI supportive {rrsi:.1f}')
    elif rrsi>72:
        cautions.append(f'RSI hot {rrsi:.1f}')
    elif rrsi<35 and px>t50:
        score+=1; reasons.append(f'trend pullback RSI {rrsi:.1f}')
    if chg>0:
        score+=1; reasons.append(f'24h positive {chg:.2f}%')
    elif chg < -8:
        cautions.append(f'24h drawdown {chg:.2f}%')
    if quote_vol >= 5_000_000:
        score+=2; reasons.append('24h quote volume > 5M USDC')
    elif quote_vol >= 500_000:
        score+=1; reasons.append('24h quote volume > 500k USDC')
    if avg_range >= 0.006:
        score+=1; reasons.append(f'15m avg range {avg_range*100:.2f}%')
    elif avg_range < 0.0025:
        cautions.append('low intraday range for 3.5% target')
    # Risk penalty for extreme 24h pump; still tradable but less chasey.
    if chg > 18:
        score-=1; cautions.append('extended 24h move; chase risk')

    # Directional/downside score for future futures/perps or margin modules.
    # The current Coinbase spot executor cannot short; this score is saved so the
    # overall setup can identify bear-market candidates and later route them to a
    # verified derivatives connector instead of pretending spot sells are shorts.
    if e9 < e21 < e50:
        short_score += 2; short_reasons.append('15m EMA stack bearish')
    elif e9 < e21:
        short_score += 1; short_reasons.append('15m short-term EMA bearish')
    if px < t50:
        short_score += 1; short_reasons.append('price below 1H EMA50')
    if t20 < t50:
        short_score += 1; short_reasons.append('1H trend EMA20 < EMA50')
    if 32 <= rrsi <= 58:
        short_score += 1; short_reasons.append(f'RSI downside/supportive {rrsi:.1f}')
    elif rrsi < 25:
        short_cautions.append(f'RSI oversold {rrsi:.1f}; snapback risk')
    if chg < 0:
        short_score += 1; short_reasons.append(f'24h negative {chg:.2f}%')
    elif chg > 12:
        short_cautions.append(f'24h squeeze/uptrend risk {chg:.2f}%')
    if quote_vol >= 5_000_000:
        short_score += 2; short_reasons.append('24h quote volume > 5M USDC')
    elif quote_vol >= 500_000:
        short_score += 1; short_reasons.append('24h quote volume > 500k USDC')
    if avg_range >= 0.006:
        short_score += 1; short_reasons.append(f'15m avg range {avg_range*100:.2f}%')
    elif avg_range < 0.0025:
        short_cautions.append('low intraday range for downside target')

    if p.get('trading_disabled'):
        score-=5; short_score-=5; cautions.append('trading disabled')
    directional_bias = 'LONG' if score >= short_score and score >= 5 else ('SHORT' if short_score >= 5 else 'NEUTRAL')
    action='BUY_WATCHLIST' if score>=5 else ('WATCH' if score>=3 else 'HOLD_USDC')
    return {**base(p), 'score':score, 'long_score':score, 'short_score':short_score, 'directional_bias':directional_bias, 'action':action, 'price':px, 'change_24h':chg, 'volume_24h':vol24, 'quote_volume_24h_usdc': quote_vol, 'rsi':rrsi, 'ema9':e9, 'ema21':e21, 'ema50':e50, 'trend_ema20':t20, 'trend_ema50':t50, 'avg_15m_range_pct':avg_range*100, 'reasons':reasons, 'short_reasons':short_reasons, 'cautions':cautions, 'short_cautions':short_cautions, 'quote_min_size': quote_min}

def base(p):
    return {'product_id':p.get('product_id'), 'base_currency_id':p.get('base_currency_id') or p.get('base_name'), 'quote_currency_id':p.get('quote_currency_id'), 'trading_disabled':p.get('trading_disabled'), 'limit_only':p.get('limit_only'), 'cancel_only':p.get('cancel_only'), 'is_disabled':p.get('is_disabled'), 'status':p.get('status')}

def market_orderable(p):
    """Return true only for products that should accept market orders.

    Coinbase can leave a product online but set limit_only=true. Those products
    may pass market previews but reject the live order with: "Orderbook is in
    limit only mode - please use limit order type". The rotator places market
    IOC orders only, so exclude them from the live watchlist entirely.
    """
    if p.get('trading_disabled') or p.get('is_disabled') or p.get('cancel_only') or p.get('limit_only'):
        return False
    return str(p.get('status') or '').lower() in {'', 'online'}

def main():
    ps=products_all()
    usdc=[]
    excluded_bases={'USDT','USDC','USD','EURC','DAI','PYUSD'}
    for p in ps:
        pid=p.get('product_id','')
        base=pid.split('-',1)[0]
        if pid.endswith('-USDC') and base not in excluded_bases and market_orderable(p):
            usdc.append(p)
    # Cron has a ~120s no_agent script limit. Scoring every USDC pair requires
    # two candle calls per product and can exceed that during slow Coinbase API
    # windows, so prefilter to liquid/moving candidates before candle scoring.
    # The strategy already rewards >=500k / >=5M USDC quote volume, so this keeps
    # the tradable universe while avoiding hard cron timeouts.
    usdc_total=len(usdc)
    def quote_vol(p):
        return f(p.get('price')) * f(p.get('volume_24h'))
    candidates=[p for p in usdc if quote_vol(p) >= 500_000 or abs(f(p.get('price_percentage_change_24h'))) >= 8]
    candidates.sort(key=lambda p: (quote_vol(p), abs(f(p.get('price_percentage_change_24h')))), reverse=True)
    usdc=candidates[:50]
    rows=[]
    # Keep concurrency modest: Coinbase public endpoints rate-limit bursts, and
    # a noisy scan can otherwise rotate based on partial/failed candle data.
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(score_product,p): p for p in usdc}
        for fut in as_completed(futs):
            try: rows.append(fut.result())
            except Exception as e: rows.append({**base(futs[fut]), 'score':0, 'action':'ERROR', 'reasons':[str(e)[:200]], 'cautions':[]})
    rows.sort(key=lambda r:(r.get('score',0), r.get('change_24h',-999), r.get('avg_15m_range_pct',0)), reverse=True)
    short_rows=sorted(rows, key=lambda r:(r.get('short_score',0), -r.get('change_24h',999), r.get('avg_15m_range_pct',0)), reverse=True)
    out={'generated_at':datetime.now(timezone.utc).isoformat(), 'total_products_seen':len(ps), 'usdc_pairs_total':usdc_total, 'usdc_pairs_analyzed':len(usdc), 'rows':rows, 'top5':rows[:5], 'top5_short_candidates':short_rows[:5]}
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    latest=OUTDIR/'usdc_pairs_latest.json'
    hist=OUTDIR/f'usdc_pairs_{stamp}.json'
    latest.write_text(json.dumps(out, indent=2, sort_keys=True))
    hist.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps({'generated_at':out['generated_at'], 'total_products_seen':len(ps), 'usdc_pairs_analyzed':len(usdc), 'top5':[{k:r[k] for k in ['product_id','score','long_score','short_score','directional_bias','action','price','change_24h','quote_volume_24h_usdc','rsi','avg_15m_range_pct','reasons','short_reasons','cautions','short_cautions'] if k in r} for r in rows[:5]], 'top5_short_candidates':[{k:r[k] for k in ['product_id','score','long_score','short_score','directional_bias','action','price','change_24h','quote_volume_24h_usdc','rsi','avg_15m_range_pct','reasons','short_reasons','cautions','short_cautions'] if k in r} for r in short_rows[:5]], 'latest_path':str(latest)}, indent=2))
if __name__=='__main__': main()
