# -*- coding: utf-8 -*-
"""MA200 IC/IM 吃贴水策略 —— 实时活数据生成 (GitHub Actions 安全)

作用:
  从 data/指数行情序列.xlsx 读取 中证500/中证1000 现货收盘, 计算"仅200日均线"趋势状态,
  输出 data/ma200_live.js (window.MA200_LIVE), 供前端 ma200.html 渲染:
    - 双指数的近 ~430 个交易日收盘序列 (前端盘中拼接实时价, 重算 MA200/置信/低波动)
    - 服务器端最新快照(趋势强弱、低波动、目标权重、总敞口)

本脚本不依赖本机期货数据目录, 可在 GitHub Actions 中与 fetch_rsrs.py 一并运行;
静态回测净值曲线见 data/ma200_backtest.js (本地生成, 已经入库, 本脚本不修改)。
"""
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')

MA = 200          # 长期均线
CONF_DAYS = 5     # 连续确认天数
VOL60 = 60        # 实现波动率窗口
MED_WIN = 252     # 波动率滚动中位数窗口
MED_MIN = 126     # 中位数最少历史
ANNUAL = 242
TRAIL = 430       # 输出到前端的收盘序列长度

# 双腿: (显示名, xlsx sheet 关键词, 现货=哪个指数)
LEGS = [
    {'leg': 'IC', 'name': '中证500', 'sheet': '000905'},
    {'leg': 'IM', 'name': '中证1000', 'sheet': '000852'},
]

# 目标权重表 (趋势强=1, 弱=0; 低波动强=1)
def target_weights(ic_strong, im_strong, ic_lv, im_lv):
    if ic_strong and im_strong:
        if ic_lv and im_lv:
            return 0.60, 0.60, 1.20, '双强·低波动 · 满杠杆', '双强且低波动，最高1.2倍'
        return 0.50, 0.50, 1.00, '双强 · 标准1倍', '双强，1倍敞口'
    if ic_strong and not im_strong:
        return 0.75, 0.25, 1.00, 'IC强 IM弱 · 偏IC', 'IC强势、IM弱势，压向IC'
    if not ic_strong and im_strong:
        return 0.25, 0.75, 1.00, 'IC弱 IM强 · 偏IM', 'IC弱势、IM强势，压向IM'
    return 0.125, 0.125, 0.25, '双弱 · 降仓0.25倍', '双弱，仅保留0.25倍'


def confirmed_trend(raw):
    """与回测一致(正确确认): 新方向连续 CONF_DAYS 天才切换, 未确认前维持旧状态。返回0/1 ndarray。"""
    vals = raw.astype(float).fillna(0).values
    state = np.zeros(len(vals))
    cur = None
    pending_val, pending_cnt = None, 0
    for i, v in enumerate(vals):
        if cur is None:
            cur = v
        elif pending_val is not None:
            if v == pending_val:
                pending_cnt += 1
                if pending_cnt >= CONF_DAYS:
                    cur = pending_val
                    pending_val = None
            else:
                pending_val, pending_cnt = None, 0
                if v != cur:
                    pending_val, pending_cnt = v, 1
        elif v != cur:
            pending_val, pending_cnt = v, 1
        state[i] = cur
    return state


def load_close(sheet_key):
    df = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    sn = None
    for k in df.keys():
        if sheet_key in k:
            sn = k
            break
    if sn is None:
        return None
    d = df[sn].copy()
    d.index = pd.to_datetime(d.index)
    for c in d.columns:
        if c.upper().strip() in ('CLOSE', '收盘价', '收盘'):
            d = d.rename(columns={c: 'CLOSE'})
            break
    s = d['CLOSE'].astype(float).sort_index()
    s = s[~s.index.duplicated(keep='last')]
    s = s[~s.index.strftime('%Y-%m-%d').duplicated(keep='last')]  # 只保留每天最后一个
    return s


def main():
    if not os.path.exists(XLSX_PATH):
        print('[错误] xlsx不存在:', XLSX_PATH)
        return 1

    live = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'ma': MA, 'confirm_days': CONF_DAYS, 'vol60': VOL60, 'median_win': MED_WIN,
            'latest_date': None, 'spot': {}, 'snap': {}}

    snap = {}
    for leg_cfg in LEGS:
        leg = leg_cfg['leg']
        s = load_close(leg_cfg['sheet'])
        if s is None or len(s) < MA + 60:
            print(f'[跳过] {leg_cfg["name"]} 数据不足')
            continue

        close = s
        ret = close.pct_change()
        ma200 = close.rolling(MA).mean()
        raw = (close > ma200).astype(float)
        state = confirmed_trend(raw)
        vol60 = ret.rolling(VOL60).std() * np.sqrt(ANNUAL)
        med = vol60.rolling(MED_WIN, min_periods=MED_MIN).median()
        lowvol = (vol60 < med).fillna(False)

        tail = close.iloc[-TRAIL:]
        last_dt = close.index[-1]
        snap[leg] = {
            'date': last_dt.strftime('%Y-%m-%d'),
            'name': leg_cfg['name'], 'close': float(close.iloc[-1]),
            'ma200': float(ma200.iloc[-1]), 'strong': bool(state[-1]),
            'lowvol': bool(lowvol.iloc[-1]), 'vol60': float(vol60.iloc[-1]),
        }
        live['spot'][leg] = {
            'name': leg_cfg['name'],
            'dates': [dt.strftime('%Y-%m-%d') for dt in tail.index],
            'close': [round(float(v), 2) for v in tail.values],
        }
        if live['latest_date'] is None or last_dt > pd.to_datetime(live['latest_date']):
            live['latest_date'] = last_dt.strftime('%Y-%m-%d')
        print(f'  [{leg}] {leg_cfg["name"]}: {len(close)}条 ~ {last_dt:%Y-%m-%d} 强={snap[leg]["strong"]} 低波动={snap[leg]["lowvol"]}')

    # 综合快照 + 目标权重
    if {'IC', 'IM'} <= set(snap.keys()):
        ic_w, im_w, expo, pos_label, pos_desc = target_weights(
            snap['IC']['strong'], snap['IM']['strong'],
            snap['IC']['lowvol'], snap['IM']['lowvol'])
        live['snap'] = {
            'date': min(snap['IC']['date'], snap['IM']['date']),
            'ic': dict(snap['IC']), 'im': dict(snap['IM']),
            'ic_weight': ic_w, 'im_weight': im_w, 'total_exposure': expo,
            'position_label': pos_label, 'position_desc': pos_desc,
        }
        print(f'  组合: IC权重={ic_w} IM权重={im_w} 总敞口={expo}倍  [{pos_label}]')

    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, 'ma200_live.js')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('window.MA200_LIVE = ' + json.dumps(live, ensure_ascii=False) + ';\n')
    print('saved:', out)
    print('latest_date:', live['latest_date'])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())