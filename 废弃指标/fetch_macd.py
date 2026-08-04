# -*- coding: utf-8 -*-
"""MACD择时指标数据获取与回测

参考研报: 金圆统一证券《策略专题: 指数趋势投资之指标策略MACD》(2025-06-23)

策略M (单一指标策略):
  - 调小MACD默认参数(12,26,9)提升信号灵敏度
  - 信号采用DIF的0轴判定(研报2.1节: DIF在0轴上方为多头市场, 下方为空头市场)
  - 交易规则: DIF(N1,N2) > 0 → 次日开盘做多; DIF < 0 → 次日开盘做空; 无止损
策略ME (指标组合策略):
  - 在策略M基础上增加双重移动平均线过滤器
  - 交易规则: DIF>0 且 EMA(S)>EMA(L) → 次日开盘做多; DIF<0 且 EMA(S)<EMA(L) → 次日开盘做空; 其他状态空仓; 无止损

说明: 研报未公开具体参数值, 本实现采用调小参考参数 N1=8, N2=17, 过滤器 EMA(5)/EMA(20)
说明: 采用DIF而非MACD柱线作为信号: MACD柱线=2*(DIF-DEA)在0轴附近频繁穿越产生大量假信号(20年1100+次交易),
      DIF直接反映短长均线差方向、穿越0轴次数少(20年约480次), 各指数回测年化显著更优
说明: 本回测采用研报原版多空双向(多头/空头/空仓), 与研报口径一致
"""

import json
import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')

# 策略参数 (研报调小参考值)
MACD_N1, MACD_N2 = 8, 17       # DIF的短长EMA参数 (由默认12/26调小)
EMA_S, EMA_L = 5, 20           # 双重移动平均线过滤器参数

# 4个回测指数: 显示名 → akshare代码/sheet关键字
TARGET_INDICES = {
    '上证50':   {'code': 'sh000016', 'sheet_key': '000016'},
    '沪深300':  {'code': 'sh000300', 'sheet_key': '000300'},
    '中证500':  {'code': 'sh000905', 'sheet_key': '000905'},
    '中证1000': {'code': 'sh000852', 'sheet_key': '000852'},
}


def ema(series, span):
    """指数移动平均 EMA"""
    return series.ewm(span=span, adjust=False).mean()


def compute_dif(close, n1=MACD_N1, n2=MACD_N2):
    """计算DIF指标: DIF=EMA(N1)-EMA(N2), 即短长EMA之差(调小后的MACD离差值)"""
    dif = ema(close, n1) - ema(close, n2)
    return dif


def run_backtest(close, position_signal, fee_rate=0.0002):
    """运行多空双向择时回测 (多头/空头/空仓)
    fee_rate: 单边手续费率, 默认0.02%(万2), 研报为股指期货策略, 期货手续费远低于股票
    """
    # position_signal: 1=多头, 0=空仓, -1=空头 (已考虑次日生效, 由调用方传入)
    position = position_signal.copy()
    pos_p = position.shift(1).fillna(0)
    # 交易判定: 持仓状态发生变化时计一次交易 (1<->-1 反手算两次)
    chg = (position != pos_p)
    reverse = (position * pos_p) == -1
    trade_cnt = int(chg.sum() + reverse.sum())

    p_ret = close.pct_change().fillna(0)
    s_ret = position * p_ret
    for i in range(len(s_ret)):
        if chg.iloc[i]:
            s_ret.iloc[i] -= fee_rate * (2 if reverse.iloc[i] else 1)

    nav = (1 + s_ret).cumprod()
    nav.iloc[0] = 1.0
    bm_nav = (1 + p_ret).cumprod()
    bm_nav.iloc[0] = 1.0

    fi = nav.first_valid_index()
    if fi is None:
        return None
    nv = nav.loc[fi:]
    rv = s_ret.loc[fi:]
    bv = bm_nav.loc[fi:]
    yrs = len(nv) / 252
    ann_ret = nv.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    bench_ann = bv.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    ann_vol = rv.std() * np.sqrt(252)
    sharpe = (ann_ret - 0.03) / ann_vol if ann_vol > 0 else 0
    max_dd = ((nv - nv.expanding().max()) / nv.expanding().max()).min()

    last_pos = {1: '多头', 0: '空仓', -1: '空头'}.get(int(position.iloc[-1]), '空仓')

    return {
        'dates': [d.strftime('%Y-%m-%d') for d in nv.index],
        'nav': [round(v, 6) for v in nv.values],
        'latest_position': last_pos,
        'performance': {
            'annual_return': f'{ann_ret * 100:.2f}%',
            'benchmark_return': f'{bench_ann * 100:.2f}%',
            'sharpe': f'{sharpe:.2f}',
            'max_drawdown': f'{max_dd * 100:.2f}%',
            'trade_count': trade_cnt,
        }
    }


def main():
    now_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('MACD择时指标数据获取与回测')
    print(f'运行时间: {now_str}')
    print('=' * 60)

    # 1. 读取xlsx (数据已由fetch_rsrs.py更新)
    if not os.path.exists(XLSX_PATH):
        print(f'[错误] xlsx文件不存在: {XLSX_PATH}')
        sys.exit(1)

    dict_data = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    print(f'成功读取 {len(dict_data)} 个sheet: {list(dict_data.keys())}')

    # 2. 计算MACD并回测
    results = {}
    for display_name, cfg in TARGET_INDICES.items():
        sheet_key = cfg['sheet_key']
        sheet_name = None
        for sn in dict_data.keys():
            if sheet_key in sn or display_name in sn:
                sheet_name = sn
                break
        if sheet_name is None:
            print(f'  [跳过] 未找到{display_name}')
            continue

        print(f'\n  {display_name} (sheet="{sheet_name}"):')
        df = dict_data[sheet_name].copy()

        col_map = {}
        for c in df.columns:
            cu = c.upper().strip()
            if cu in ('OPEN', '开盘价', '开盘'):
                col_map[c] = 'OPEN'
            elif cu in ('HIGH', '最高价', '最高'):
                col_map[c] = 'HIGH'
            elif cu in ('LOW', '最低价', '最低'):
                col_map[c] = 'LOW'
            elif cu in ('CLOSE', '收盘价', '收盘'):
                col_map[c] = 'CLOSE'
            elif 'VOL' in cu or '成交' in cu:
                col_map[c] = 'VOLUME'
        df = df.rename(columns=col_map)
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        df = df.dropna(subset=['CLOSE'])
        print(f'    数据量: {len(df)} 条, {df.index.min().date()} ~ {df.index.max().date()}')

        close = df['CLOSE']
        dif = compute_dif(close)
        ema_s = ema(close, EMA_S)
        ema_l = ema(close, EMA_L)

        # 次日生效: 信号shift(1), 空值填0
        # ---- 策略M: DIF>0 多头, DIF<0 空头 (多空双向) ----
        pos_m = (dif > 0).astype(int) - (dif < 0).astype(int)
        pos_m = pos_m.shift(1).fillna(0)
        sig_m = dif
        result_m = run_backtest(close, pos_m)
        if result_m:
            result_m['threshold'] = 0.0
            p = result_m['performance']
            print(f'    策略M : 年化={p["annual_return"]}, 夏普={p["sharpe"]}, 回撤={p["max_drawdown"]}, {result_m["latest_position"]}')

        # ---- 策略ME: DIF>0且EMA(S)>EMA(L) 多头, DIF<0且EMA(S)<EMA(L) 空头, 其他空仓 ----
        cond_long = (dif > 0) & (ema_s > ema_l)
        cond_short = (dif < 0) & (ema_s < ema_l)
        pos_me = pd.Series(0, index=dif.index, dtype=float)
        pos_me[cond_long] = 1.0
        pos_me[cond_short] = -1.0
        pos_me = pos_me.shift(1).fillna(0)
        sig_me = dif
        result_me = run_backtest(close, pos_me)
        if result_me:
            result_me['threshold'] = 0.0
            p = result_me['performance']
            print(f'    策略ME: 年化={p["annual_return"]}, 夏普={p["sharpe"]}, 回撤={p["max_drawdown"]}, {result_me["latest_position"]}')

        strategies = {}
        if result_m:
            strategies['策略M'] = result_m
        if result_me:
            strategies['策略ME'] = result_me

        if not strategies:
            print(f'    无有效策略回测结果')
            continue

        # 对齐所有策略到相同的日期范围
        first_key = list(strategies.keys())[0]
        common_dates = pd.to_datetime(strategies[first_key]['dates'])
        for name in strategies:
            sd = pd.to_datetime(strategies[name]['dates'])
            common_dates = common_dates.intersection(sd)
        common_dates = sorted(common_dates)
        if len(common_dates) < 2:
            print(f'    日期对齐后数据不足')
            continue

        common_dates_str = [d.strftime('%Y-%m-%d') for d in common_dates]
        date_set = set(common_dates_str)

        # 按对齐后的日期重采样每个策略的净值与指标信号
        for name in strategies:
            sd = strategies[name]
            date_nav = dict(zip(sd['dates'], sd['nav']))
            sd['nav'] = [date_nav.get(d, None) for d in common_dates_str]
            sig = sig_m.reindex(common_dates)
            sd['signal'] = [
                round(float(v), 6) if pd.notna(v) else None
                for v in sig.values
            ]

        # 基准净值
        close_aligned = close.loc[common_dates]
        p_ret = close_aligned.pct_change().fillna(0)
        bm_nav = (1 + p_ret).cumprod()
        bm_nav.iloc[0] = 1.0

        idx_data = {
            'dates': common_dates_str,
            'benchmark_nav': [round(v, 6) for v in bm_nav.values],
            # 下方面板: DIF指标线(0轴为多空分界) + EMA过滤器线
            'dif': [round(float(v), 6) if pd.notna(v) else None for v in dif.reindex(common_dates).values],
            'ema_s': [round(float(v), 4) if pd.notna(v) else None for v in ema_s.reindex(common_dates).values],
            'ema_l': [round(float(v), 4) if pd.notna(v) else None for v in ema_l.reindex(common_dates).values],
            'strategies': strategies,
        }
        results[display_name] = idx_data

    # 3. 输出JS数据文件
    print('\n[3/3] 输出JS数据文件...')
    os.makedirs(DATA_DIR, exist_ok=True)

    js_path = os.path.join(DATA_DIR, 'macd_data.js')
    with open(js_path, 'w', encoding='utf-8') as f:
        f.write('window.MACD_DATA = ')
        json.dump(results, f, ensure_ascii=False)
        f.write(';\n')
    print(f'  已保存: {js_path}')
    print(f'  包含指数: {list(results.keys())}')

    print('\n完成!')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n运行出错: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
