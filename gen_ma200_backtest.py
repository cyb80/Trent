# -*- coding: utf-8 -*-
"""CI 版 MA200 回测生成 (gen_ma200_backtest.py) —— 期货历史落盘 + 每日增量更新

目标:
  期货合约历史不要在每次部署时全量重拉(约107次请求 / 40s), 改为:
    - 首次运行: 全量拉取 IC/IM 期货合约, 解析"近月提前5日换月"每日持仓序列, 落盘到
                data/ma200_futures.csv (紧凑: 每交易日 1 行, 4年约 <200KB)。
    - 后续每次运行: 只拉取最近 ~6 个月的在手合约(约 24 次请求), 增量追加缺失的最新交易日,
                再改写成 CSV 后写回 (被 deploy.yml 的 git add data/ 提交, 历史随仓库持久化)。
    - 再由落盘的近月序列 + 现货(000905/000852, 来自 xlsx)重建"仅200日均线"策略净值,
                终点=最新交易日, 输出 data/ma200_backtest.js。
现货趋势仍读 repo 的 data/指数行情序列.xlsx (由 fetch_rsrs.py 增量维护), 与期货互补。

CSV 列(每交易日一行, 已按持仓口径计算好, 回测直接消费):
  date, IC_sel, IC_close, IC_ret, IC_roll, IM_sel, IM_close, IM_ret, IM_roll
    IC_sel[t]   : 第 t 日选定的近月合约 code (sel[t])
    IC_close[t] : 近月合约当日收盘价 close(sel[t], t)
    IC_ret[t]   : 第 t 日收益率 = close(sel[t-1],t)/close(sel[t-1],t-1)-1 (持仓口径)
    IC_roll[t]  : 1=当日换月(sel[t]!=sel[t-1])  (用于换月成本)
"""
import json
import os
import time
from datetime import datetime, timedelta

import akshare as ak
import numpy as np
import pandas as pd

import backtest_trend_methods as bt  # 复用纯逻辑: estimate_third_friday / build_calendar /
                                     # select_near_month / product_return / build_trend / 常量

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')
STATE_PATH = os.path.join(DATA_DIR, 'ma200_futures.csv')
OUT_PATH = os.path.join(DATA_DIR, 'ma200_backtest.js')

SYMS = ['IC', 'IM']
BACKFILL_START = (2022, 1)   # 需覆盖 eval_start(2023-05) 之前的 vol60+波动中位数最小历史
MA = 200
ANNUAL = bt.ANNUAL
SINGLE_COST = bt.SINGLE_COST
NAV_LAG = 1  # 正确5日确认后, 次日(滞后1日)调整仓位
CSV_COLS = ['date', 'IC_sel', 'IC_close', 'IC_ret', 'IC_roll',
            'IM_sel', 'IM_close', 'IM_ret', 'IM_roll']


# ---------- 现货 ----------
def load_spot_from_xlsx():
    sheets = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    def _read(key):
        sn = [k for k in sheets if key in k][0]
        d = sheets[sn].copy()
        d.index = pd.to_datetime(d.index)
        col = [c for c in d.columns if c.upper().strip() == 'CLOSE'][0]
        s = d[col].astype(float).sort_index()
        s = s[~s.index.duplicated(keep='last')]
        return s
    return {'IC': _read('000905'), 'IM': _read('000852')}


# ---------- 期货合约拉取 ----------
def _months(y0, m0, y1, m1):
    """返回 [(yy,mm), ...] 月份序列, 含首尾。m0/m1 可用负值表示"上月"。"""
    yy, mm = y0, m0
    out = []
    while (yy, mm) < (y1, m1):
        out.append((yy, mm))
        mm += 1
        if mm > 12:
            mm = 1
            yy += 1
    out.append((y1, m1))
    return out


def fetch_contracts(sym, months):
    """拉取指定月份集合的合约日线, 返回 {code: {'df':.., 'month_end':.., 'third_fri':..}}。"""
    contracts = {}
    for yy, mm in months:
        yymm = '%02d%02d' % (yy % 100, mm)
        code = '%s%s' % (sym, yymm)
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception:
            continue
        if df is None or df.empty or not {'close', 'volume'}.issubset(df.columns):
            continue
        df = df[['date', 'close', 'volume']].copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df = df[['close', 'volume']].dropna(subset=['close'])
        if df.empty:
            continue
        c_yy = 2000 + int(yymm[:2])
        c_mm = int(yymm[2:])
        contracts[code] = {
            'df': df,
            'month_end': pd.Timestamp(c_yy, c_mm, 1),
            'third_fri': bt.estimate_third_friday(c_yy, c_mm),
        }
        time.sleep(0.3)
    return contracts


def near_month_frame(sym, contracts, calendar):
    """对给定日历用选定合约解析近月序列: 返回 DataFrame[date, sel, close, ret, roll]。"""
    sel, close_mat, _, _ = bt.select_near_month(sym, contracts, calendar, calendar.max())
    ret, roll = bt.product_return(sel, close_mat)
    rows = []
    for t in calendar:
        rows.append({
            'date': t.strftime('%Y-%m-%d'),
            'sel': ('' if pd.isna(sel.loc[t]) else str(sel.loc[t])),
            'close': (float(close_mat.loc[t, sel.loc[t]]) if not pd.isna(sel.loc[t]) and
                      not pd.isna(close_mat.loc[t, sel.loc[t]]) else float('nan')),
            'ret': (None if pd.isna(ret.loc[t]) else float(ret.loc[t])),
            'roll': (0.0 if not (not pd.isna(ret.loc[t]) and float(roll.loc[t])) else 1.0),
        })
    df = pd.DataFrame(rows)
    df['near_close'] = df['close']
    return df


# ---------- 状态文件维护 ----------
def build_full_table():
    """全量回填(仅首次): 拉取 2022-01 至今所有合约, 解析近月序列。"""
    now = datetime.now()
    months = _months(BACKFILL_START[0], BACKFILL_START[1], now.year, now.month)
    parts = {}
    for sym in SYMS:
        contracts = fetch_contracts(sym, months)
        if not contracts:
            raise RuntimeError('%s 全量合约拉取为空' % sym)
        cal = bt.build_calendar(contracts)
        df = near_month_frame(sym, contracts, cal)
        df = df.rename(columns={'sel': sym + '_sel', 'near_close': sym + '_close',
                                'ret': sym + '_ret', 'roll': sym + '_roll'})
        df = df[['date', sym + '_sel', sym + '_close', sym + '_ret', sym + '_roll']]
        parts[sym] = df
        print('  [回填 %s] 合约数=%d 序列天数=%d (%s ~ %s)'
              % (sym, len(contracts), len(df), df['date'].iloc[0], df['date'].iloc[-1]))
    merged = parts['IC'].merge(parts['IM'], on='date', how='outer').sort_values('date')
    merged['date'] = pd.to_datetime(merged['date'])
    merged = merged[merged['date'] >= pd.Timestamp(BACKFILL_START[0], BACKFILL_START[1], 1)]
    merged = merged[~merged['date'].duplicated(keep='last')].reset_index(drop=True)
    merged.to_csv(STATE_PATH, index=False, encoding='utf-8')
    print('  回填完成 ->', STATE_PATH, ' 行数:', len(merged))
    return merged


def incremental_update():
    """增量: 只拉最近约6个月在手合约, 追加缺失的最新交易日。"""
    cur = pd.read_csv(STATE_PATH, parse_dates=['date'])
    cur = cur.sort_values('date')
    last_date = cur['date'].iloc[-1]
    now = datetime.now()
    # 拉取 now-2 月 .. now+4 月(覆盖上一近月边界 + 当前/下月/就近季月)
    ny, nm = now.year, now.month
    months = _months(ny, nm - 2, ny, nm + 4)
    new_rows = []
    for sym in SYMS:
        contracts = fetch_contracts(sym, months)
        if not contracts:
            print('  [增量 %s] 最近合约拉取为空, 跳过' % sym)
            continue
        all_dates = sorted({d.strftime('%Y-%m-%d') for c in contracts.values()
                            for d in c['df'].index})
        buffer_start = (last_date - timedelta(days=12)).strftime('%Y-%m-%d')
        cal_days = [d for d in all_dates if d >= buffer_start]
        if not cal_days:
            print('  [增量 %s] 无新日期' % sym)
            continue
        cal = pd.DatetimeIndex(pd.to_datetime(cal_days))
        df = near_month_frame(sym, contracts, cal)
        df = df[df['date'] > last_date.strftime('%Y-%m-%d')]
        if len(df) == 0:
            print('  [增量 %s] %s ... %s 已是最新(%s)' % (sym, cal_days[0], cal_days[-1], last_date.date()))
            continue
        df['date'] = pd.to_datetime(df['date'])
        df = df.rename(columns={'sel': sym + '_sel', 'near_close': sym + '_close',
                                'ret': sym + '_ret', 'roll': sym + '_roll'})
        df = df[['date', sym + '_sel', sym + '_close', sym + '_ret', sym + '_roll']]
        new_rows.append(df)
        print('  [增量 %s] 新增 %d 日 (%s ~ %s)'
              % (sym, len(df), df['date'].iloc[0].date(), df['date'].iloc[-1].date()))

    if not new_rows:
        print('  数据已是最新, 无需更新; 截至 %s' % last_date.date())
        return cur

    # 合并各 symbol 的新增行(各自已按 date>last 裁剪, IC/IM 交易日对齐) -> 宽表
    app = new_rows[0]
    for df in new_rows[1:]:
        app = app.merge(df, on='date', how='outer')
    app = app.reindex(columns=CSV_COLS)
    # 新行与旧历史日期不重叠, 直接逐行拼接
    merged = pd.concat([cur, app], ignore_index=True)
    merged = merged.sort_values('date')
    merged = merged[~merged['date'].duplicated(keep='last')]
    merged.to_csv(STATE_PATH, index=False, encoding='utf-8')
    print('  增量更新完成 ->', STATE_PATH, ' 行数:', len(merged))
    return merged


# ---------- 由落盘近月序列生成净值 ----------
def build_nav(table):
    idx = load_spot_from_xlsx()
    calendar = pd.DatetimeIndex(table['date'])

    def leg(col):
        ret = table[col + '_ret'].astype(float).values
        roll = table[col + '_roll'].astype(float).fillna(0).values
        ret = pd.Series(ret, index=calendar)
        roll = pd.Series(roll, index=calendar)
        return ret, roll

    ret_ic, roll_ic = leg('IC')
    ret_im, roll_im = leg('IM')

    # 正确5日确认(未确认前维持旧状态), 之后信号滞后 NAV_LAG 日调整仓位
    bt.CONFIRM = True
    bt.CONFIRM_DELAYED = True
    tre_ic = bt.build_trend('IC', idx['IC'], calendar, [MA])
    tre_im = bt.build_trend('IM', idx['IM'], calendar, [MA])

    vol60_ic = ret_ic.rolling(bt.VOL60).std() * np.sqrt(ANNUAL)
    med_ic = vol60_ic.rolling(bt.MED_WIN, min_periods=bt.MED_MIN).median()
    lowvol_ic = (vol60_ic < med_ic).astype(bool)
    vol60_im = ret_im.rolling(bt.VOL60).std() * np.sqrt(ANNUAL)
    med_im = vol60_im.rolling(bt.MED_WIN, min_periods=bt.MED_MIN).median()
    lowvol_im = (vol60_im < med_im).astype(bool)

    def weight_from(ts):
        ic_s = tre_ic.reindex(ts).astype(bool).fillna(False)
        im_s = tre_im.reindex(ts).astype(bool).fillna(False)
        lv_ic = lowvol_ic.reindex(ts).astype(bool).fillna(False)
        lv_im = lowvol_im.reindex(ts).astype(bool).fillna(False)
        w_ic = np.zeros(len(ts)); w_im = np.zeros(len(ts))
        for i, dt in enumerate(ts):
            sci, sim = ic_s.iloc[i], im_s.iloc[i]
            lvi, lvm = lv_ic.iloc[i], lv_im.iloc[i]
            if sci and sim:
                if lvi and lvm:
                    w_ic[i] = 0.60; w_im[i] = 0.60
                else:
                    w_ic[i] = 0.50; w_im[i] = 0.50
            elif sci and not sim:
                w_ic[i] = 0.75; w_im[i] = 0.25
            elif not sci and sim:
                w_ic[i] = 0.25; w_im[i] = 0.75
            else:
                w_ic[i] = 0.125; w_im[i] = 0.125
        return pd.Series(w_ic, index=ts), pd.Series(w_im, index=ts)

    sig_ic, sig_im = weight_from(calendar[:-NAV_LAG] if NAV_LAG > 0 else calendar)
    w_ic = pd.Series(index=calendar, dtype=float)
    w_im = pd.Series(index=calendar, dtype=float)
    for k, dt in enumerate(calendar):
        if k >= NAV_LAG:
            w_ic.loc[dt] = sig_ic.loc[calendar[k - NAV_LAG]]
            w_im.loc[dt] = sig_im.loc[calendar[k - NAV_LAG]]
    w_ic = w_ic.ffill(); w_im = w_im.ffill()

    comb = w_ic * ret_ic + w_im * ret_im
    weight_cost = SINGLE_COST * (w_ic.diff().abs() + w_im.diff().abs())
    roll_cost = 2 * SINGLE_COST * (w_ic * roll_ic + w_im * roll_im)
    net = comb - weight_cost - roll_cost

    eval_start = bt.EVAL_START
    eval_end = net.index.max()
    sub = net.loc[(net.index >= eval_start) & (net.index <= eval_end)].dropna()
    strat_nav = (1 + sub).cumprod() / (1 + sub.iloc[0])

    bench_sub = idx['IM'].reindex(sub.index).ffill()
    bench_nav = (1 + bench_sub.pct_change().fillna(0)).cumprod()
    bench_nav = bench_nav / bench_nav.iloc[0]

    yrs = len(sub) / ANNUAL
    total = strat_nav.iloc[-1] - 1
    ann = strat_nav.iloc[-1] ** (1 / yrs) - 1
    ann_vol = sub.std() * np.sqrt(ANNUAL)
    sharpe = ann / ann_vol if ann_vol else float('nan')
    rollmax = strat_nav.cummax()
    dd = strat_nav / rollmax - 1
    mdd = dd.min()
    trough = dd.idxmin()
    peak = strat_nav.loc[:trough].idxmax()
    avg_exp = (w_ic.loc[sub.index] + w_im.loc[sub.index]).mean()

    return {
        'dates': [d.strftime('%Y-%m-%d') for d in sub.index],
        'strat_nav': [round(float(v), 6) for v in strat_nav.values],
        'bench_nav': [round(float(v), 6) for v in bench_nav.values],
        'exposure': [round(float(v), 3) for v in (w_ic.loc[sub.index] + w_im.loc[sub.index]).values],
        'eval_start': eval_start.strftime('%Y-%m-%d'),
        'eval_end': eval_end.strftime('%Y-%m-%d'),
        'performance': {
            'annual_return': '%.2f%%' % (ann * 100),
            'cumulative_return': '%.2f%%' % (total * 100),
            'max_drawdown': '%.2f%%' % (mdd * 100),
            'drawdown_window': '%s~%s' % (peak.strftime('%Y-%m-%d'), trough.strftime('%Y-%m-%d')),
            'annual_vol': '%.2f%%' % (ann_vol * 100),
            'sharpe': '%.2f' % sharpe,
            'avg_exposure': '%.2f倍' % avg_exp,
        },
    }


def write_js(nav):
    data = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'eval_start': nav['eval_start'],
        'eval_end': nav['eval_end'],
        'trend_method': '仅200日均线(正确5日确认,信号滞后1日)',
        'confirm_days': 5,
        'signal_lag': NAV_LAG,
        'dates': nav['dates'],
        'strat_nav': nav['strat_nav'],
        'bench_nav': nav['bench_nav'],
        'exposure': nav['exposure'],
        'performance': nav['performance'],
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('window.MA200_BACKTEST = ' + json.dumps(data, ensure_ascii=False) + ';\n')
    print('已保存:', OUT_PATH)
    print('  评价期: %s ~ %s  净值点数: %d' % (data['eval_start'], data['eval_end'], len(nav['dates'])))
    print('  策略末期净值: %.4f  基准(中证1000)末期净值: %.4f' % (nav['strat_nav'][-1], nav['bench_nav'][-1]))
    print('  绩效: %s' % data['performance'])


def main():
    if not os.path.exists(XLSX_PATH):
        raise FileNotFoundError('缺少 xlsx: %s' % XLSX_PATH)

    # 1. 维护近月序列(首次全量回填, 之后增量)
    if not os.path.exists(STATE_PATH):
        print('状态文件不存在 -> 全量回填')
        table = build_full_table()
    else:
        print('状态文件存在 -> 增量更新')
        table = incremental_update()

    # 2. 重算净值并输出
    print('基于落盘近月序列重算净值...')
    nav = build_nav(table)
    write_js(nav)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())