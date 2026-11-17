# -*- coding: utf-8 -*-
"""
历史上的今天 —— 14:30 盘中快照

用盘中实时价（东方财富 → 腾讯 兜底）构造"今天"的临时 K 线，重算相似日概率/空间，
写入 历史上的今天/analog_snapshots.js。同日多次运行只保留最后一次。

注意：盘中数据只进入快照文件，绝不写回 xlsx / price_patch.csv，不污染训练样本。
"""

import os
import sys
import json
import datetime as dt

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analog_core as A  # noqa: E402

SNAP_PATH = os.path.join(A.BASE, 'analog_snapshots.js')
EAST_PUSH = ('https://push2.eastmoney.com/api/qt/stock/get?secid={secid}'
             '&invt=2&fltt=2&fields=f43,f44,f45,f46,f47,f57,f58,f60,f86')
TENCENT_Q = 'https://qt.gtimg.cn/q={sym}'


def _http(url, encoding='utf-8'):
    import requests
    r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    return r.content.decode(encoding, errors='ignore')


def quote_eastmoney(code):
    secid = '1.' + code[:6]
    try:
        js = json.loads(_http(EAST_PUSH.format(secid=secid)))
        d = js.get('data') or {}
        price = float(d.get('f43'))
        if not np.isfinite(price) or price <= 0:
            return None
        return {'price': price, 'open': float(d.get('f46')), 'high': float(d.get('f44')),
                'low': float(d.get('f45')), 'volume': None, 'prev': float(d.get('f60')),
                'source': '东方财富', 'time': A._beijing_now().strftime('%H:%M:%S')}
    except Exception as e:
        print('  [warn] 东财实时失败 %s: %s' % (code, e))
        return None


def quote_tencent(code):
    sym = 'sh' + code[:6]
    try:
        txt = _http(TENCENT_Q.format(sym=sym), encoding='gbk')
        seg = txt.split('="')[1].split('~')
        price = float(seg[3])
        if not np.isfinite(price) or price <= 0:
            return None
        return {'price': price, 'open': float(seg[5]), 'high': float(seg[33]),
                'low': float(seg[34]), 'volume': None, 'prev': float(seg[4]),
                'source': '腾讯', 'time': seg[30][-8:] if len(seg) > 30 else ''}
    except Exception as e:
        print('  [warn] 腾讯实时失败 %s: %s' % (code, e))
        return None


def get_quote(code):
    for fn in (quote_eastmoney, quote_tencent):
        q = fn(code)
        if q and q.get('price'):
            return q
    return None


def with_intraday_bar(df, q, today):
    """剔除今日（若已有）后追加一根由实时价构造的临时 K 线。"""
    d = df[df['date'] != today].copy()
    hi = max(q['price'], q.get('high') or q['price'])
    lo = min(q['price'], q.get('low') or q['price'])
    row = {'date': today, 'open': q.get('open') or q['price'],
           'high': hi, 'low': lo, 'close': q['price'], 'volume': np.nan, 'pb': np.nan}
    return pd.concat([d, pd.DataFrame([row])], ignore_index=True)


def main():
    now = A._beijing_now()
    today = now.strftime('%Y-%m-%d')
    print('== 14:30 快照 %s ==' % now.strftime('%Y-%m-%d %H:%M:%S'))
    ctx = A.build_context()
    out = {
        'updated': now.strftime('%Y-%m-%d %H:%M'),
        'trade_date': today,
        'intraday': now.weekday() < 5 and (now.hour, now.minute) < (15, 5),
        'indices': {},
    }
    ok = 0
    for code, name, product in A.INDEXES:
        q = get_quote(code)
        if not q:
            print('  [跳过] %s 无实时报价' % name)
            continue
        df = A.load_index_frame(code)
        df = A.merge_patch(df, ctx['patch'], code)
        df = with_intraday_bar(df, q, today)
        feat = A.build_features(code, name, product, df, ctx)
        mats = A._matrices(feat)
        t = len(feat['close']) - 1
        pred = A.analyze(feat, mats, t, detail=False,
                         iv_series=ctx['iv'] if name == '中证1000' else None)
        prev_close = q.get('prev') or feat['close'][t - 1]
        out['indices'][name] = {
            'code': code,
            'date': today,
            'close': round(float(q['price']), 2),
            'chg': round(float(q['price'] / prev_close - 1.0), 5) if prev_close else None,
            'price_time': q.get('time'),
            'source': q.get('source'),
            'blocks': A._block_snapshot(feat, t),
            'horizons': pred,
        }
        ok += 1
        print('  %s %.2f (%+.2f%%) @%s' % (name, q['price'],
                                           (q['price'] / prev_close - 1) * 100 if prev_close else 0,
                                           q.get('time')))

    if not ok:
        print('无任何实时报价，保持原有快照不变')
        return 1

    existing = {}
    if os.path.exists(SNAP_PATH):
        txt = open(SNAP_PATH, encoding='utf-8').read()
        m = txt.find('{')
        try:
            existing = json.loads(txt[m:txt.rindex(';')]) if m >= 0 else {}
        except Exception:
            existing = {}
    hist = existing.get('history') or {}
    hist[today] = {n: {'close': v['close'], 'chg': v['chg'],
                       'p': {h: it['p_up'] for h, it in v['horizons'].items()}}
                   for n, v in out['indices'].items()}
    hist = dict(sorted(hist.items())[-60:])
    out['history'] = hist

    with open(SNAP_PATH, 'w', encoding='utf-8') as f:
        f.write('// 历史上的今天 —— 14:30 盘中快照，由 历史上的今天/snapshot_1430.py 自动生成\n')
        f.write('window.ANALOG_SNAPSHOT = ')
        f.write(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
        f.write(';\n')
    print('已写入 %s (%.1f KB)' % (SNAP_PATH, os.path.getsize(SNAP_PATH) / 1024.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
