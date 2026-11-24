# -*- coding: utf-8 -*-
"""临时: 生成本地静态回测数据 -> data/ma200_backtest.js (仅本地运行, 不入库部署)
使用已校验框架 backtest_trend_methods 计算"仅200日均线"策略净值 + 中证1000基准。
"""
import json, os
import pandas as pd
import backtest_trend_methods as bt

def _w(path, txt):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(txt)
    print('saved', path)

# ---- 1. 策略净值(仅200日) ----
r = bt.backtest([200])
sub = r['net收益序列'].loc[(r['net收益序列'].index >= bt.EVAL_START) & (r['net收益序列'].index <= bt.EVAL_END)]
nav = (1 + sub).cumprod()
nav = nav / nav.iloc[0]  # 首日净值=1

# ---- 2. 基准: 中证1000(000852) 买入持有 ----
idx = bt.load_index()
bench_sub = idx['IM'].reindex(sub.index).ffill()
bench_nav = (1 + bench_sub.pct_change().fillna(0)).cumprod()
bench_nav = bench_nav / bench_nav.iloc[0]

# ---- 3. 绩效 ----
perf = {
    'annual_return': f"{r['年化收益率']*100:.2f}%",
    'cumulative_return': f"{r['累计收益率']*100:.2f}%",
    'max_drawdown': f"{r['最大回撤']*100:.2f}%",
    'annual_vol': f"{r['年化波动率']*100:.2f}%",
    'sharpe': f"{r['Sharpe']:.2f}",
    'avg_exposure': f"{r['平均敞口']:.2f}倍",
    'drawdown_window': r['回撤区'],
}

data = {
    'generated_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
    'eval_start': bt.EVAL_START.strftime('%Y-%m-%d'),
    'eval_end': bt.EVAL_END.strftime('%Y-%m-%d'),
    'trend_method': '仅200日均线',
    'dates': [d.strftime('%Y-%m-%d') for d in sub.index],
    'strat_nav': [round(float(v), 6) for v in nav.values],
    'bench_nav': [round(float(v), 6) for v in bench_nav.values],
    'performance': perf,
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'ma200_backtest.js')
_w(out, 'window.MA200_BACKTEST = ' + json.dumps(data, ensure_ascii=False) + ';\n')
print('  净值点数:', len(nav), ' 首日:', str(sub.index[0])[:10], ' 末日:', str(sub.index[-1])[:10])
print('  策略末期净值:', round(float(nav.values[-1]), 4), ' 基准末期净值:', round(float(bench_nav.values[-1]), 4))