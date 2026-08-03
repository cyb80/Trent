# -*- coding: utf-8 -*-
"""RSRS择时指标数据获取与回测

数据源:
  初始行情 → data/指数行情序列.xlsx
  每日增量 → akshare (stock_zh_index_daily_em)
"""

import json
import os
import sys
from datetime import datetime, timedelta

import akshare as ak
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')

# 4个回测指数配置: (显示名, akshare代码, N, M, 各策略阈值)
TARGET_INDICES = {
    '上证50': {
        'code': 'sh000016', 'sheet_key': '000016', 'N': 16, 'M': 700,
        'ths': {'RSRS_原始': 0.6, 'RSRS_右偏修正': 0.6, 'RSRS_钝化': 0.3, 'RSRS_成交额加权钝化': 0.8}
    },
    '沪深300': {
        'code': 'sh000300', 'sheet_key': '000300', 'N': 18, 'M': 600,
        'ths': {'RSRS_原始': 0.7, 'RSRS_右偏修正': 0.7, 'RSRS_钝化': 0.7, 'RSRS_成交额加权钝化': 0.8}
    },
    '中证500': {
        'code': 'sh000905', 'sheet_key': '000905', 'N': 18, 'M': 800,
        'ths': {'RSRS_原始': 0.8, 'RSRS_右偏修正': 0.8, 'RSRS_钝化': 1.0, 'RSRS_成交额加权钝化': 0.6}
    },
    '中证1000': {
        'code': 'sh000852', 'sheet_key': '000852', 'N': 19, 'M': 800,
        'ths': {'RSRS_原始': 0.8, 'RSRS_右偏修正': 0.8, 'RSRS_钝化': 0.7, 'RSRS_成交额加权钝化': 0.5}
    },
}


def calc_ols_beta_r2(high, low):
    """计算OLS回归的Beta和R²"""
    cov_h_l = high.cov(low)
    var_l = low.var()
    if var_l == 0:
        return 0, 0
    beta = cov_h_l / var_l
    var_h = high.var()
    r_squared = (cov_h_l ** 2) / (var_l * var_h) if var_h != 0 else 0
    return beta, r_squared


def calc_wls_beta_r2(high, low, weights):
    """计算加权回归的Beta和R²"""
    w = weights / weights.sum()
    mean_h = (w * high).sum()
    mean_l = (w * low).sum()
    cov_w = (w * (high - mean_h) * (low - mean_l)).sum()
    var_l_w = (w * (low - mean_l) ** 2).sum()
    if var_l_w == 0:
        return 0, 0
    beta = cov_w / var_l_w
    var_h_w = (w * (high - mean_h) ** 2).sum()
    r_squared = (cov_w ** 2) / (var_l_w * var_h_w) if var_h_w != 0 else 0
    return beta, r_squared


def compute_rsrs_indicators(df, N=18, M=600):
    """计算全部RSRS择时指标"""
    T = len(df)
    result = pd.DataFrame(index=df.index)
    result['ret'] = df['CLOSE'].pct_change()

    # Step1: OLS
    bl, rl = [], []
    for i in range(N, T + 1):
        b, r2 = calc_ols_beta_r2(df['HIGH'].iloc[i - N:i], df['LOW'].iloc[i - N:i])
        bl.append(b)
        rl.append(r2)
    result['beta'] = pd.Series([np.nan] * (N - 1) + bl, index=df.index)
    result['rsquare'] = pd.Series([np.nan] * (N - 1) + rl, index=df.index)

    # Step2: WLS (成交额加权)
    wbl, wrl = [], []
    for i in range(N, T + 1):
        vol = df['VOLUME'].iloc[i - N:i].fillna(0).abs()
        if vol.sum() > 0:
            b, r2 = calc_wls_beta_r2(df['HIGH'].iloc[i - N:i], df['LOW'].iloc[i - N:i], vol)
        else:
            b, r2 = calc_ols_beta_r2(df['HIGH'].iloc[i - N:i], df['LOW'].iloc[i - N:i])
        wbl.append(b)
        wrl.append(r2)
    result['beta_wls'] = pd.Series([np.nan] * (N - 1) + wbl, index=df.index)
    result['rsquare_wls'] = pd.Series([np.nan] * (N - 1) + wrl, index=df.index)

    # Step3: Z-score
    roll = lambda x: x.rolling(M, min_periods=M // 2)
    result['zscore'] = ((result['beta'] - roll(result['beta']).mean()) / roll(result['beta']).std())
    result['zscore_wls'] = ((result['beta_wls'] - roll(result['beta_wls']).mean()) / roll(result['beta_wls']).std())

    # Step4: RSRS各指标
    result['RSRS_原始'] = result['zscore'] * result['rsquare']
    result['RSRS_右偏修正'] = result['zscore'] * result['rsquare'] * result['beta']

    ret_std = result['ret'].rolling(N, min_periods=N // 2).std()

    def _q(x):
        return (x.iloc[-1] - x.min()) / (x.max() - x.min()) if len(x) > 1 and x.max() != x.min() else 0.5

    ret_q = ret_std.rolling(M, min_periods=M // 2).apply(_q, raw=False).clip(0, 1)
    result['RSRS_钝化'] = result['zscore'] * (result['rsquare'] ** (4 * ret_q))
    result['RSRS_成交额加权钝化'] = result['zscore_wls'] * (result['rsquare_wls'] ** (4 * ret_q))

    return result


def run_backtest(close, signals, threshold, fee_rate=0.001):
    """运行RSRS择时回测"""
    position = pd.Series(0, index=signals.index, dtype=float)
    in_pos = False
    for i in range(len(signals)):
        sig = signals.iloc[i]
        if pd.isna(sig):
            position.iloc[i] = 1 if in_pos else 0
            continue
        if not in_pos and sig > threshold:
            position.iloc[i] = 1
            in_pos = True
        elif in_pos and sig < -threshold:
            position.iloc[i] = 0
            in_pos = False
        else:
            position.iloc[i] = 1 if in_pos else 0

    pos_p = position.shift(1).fillna(0)
    buy_sig = (position == 1) & (pos_p == 0)
    sell_sig = (position == 0) & (pos_p == 1)
    trade_cnt = int(buy_sig.sum() + sell_sig.sum())

    p_ret = close.pct_change().fillna(0)
    s_ret = position * p_ret
    for i in range(len(s_ret)):
        if buy_sig.iloc[i] or sell_sig.iloc[i]:
            s_ret.iloc[i] -= fee_rate

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

    last_pos = '满仓' if position.iloc[-1] == 1 else '空仓'

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


# ----- akshare 数据获取函数（供增量更新使用） -----

def fetch_akshare_daily(code, start_date):
    """从 akshare 获取指数日线数据（东方财富为主源，新浪为备用源）"""
    # 主源: 东方财富
    try:
        df = ak.stock_zh_index_daily_em(symbol=code)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df = df[['open', 'close', 'high', 'low', 'volume']]
        df.columns = ['OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
        df = df[df.index >= start_date]
        df = df.sort_index()
        return df
    except Exception as e:
        print(f'  OHLCV主源(东财)失败: {e}')

    # 备用源: 新浪
    try:
        code_sina = code.lower()
        if code_sina.startswith('sh') or code_sina.startswith('sz'):
            code_sina = code_sina[:2] + code_sina[2:]
        df = ak.stock_zh_index_daily(symbol=code)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df = df[['open', 'close', 'high', 'low', 'volume']]
        df.columns = ['OPEN', 'CLOSE', 'HIGH', 'LOW', 'VOLUME']
        # 新浪 volume 单位为"股"，东财为"手"(1手=100股)，除以100转为"手"以保持与东财数据一致
        df['VOLUME'] = df['VOLUME'] / 100
        df = df[df.index >= start_date]
        df = df.sort_index()
        print(f'  OHLCV备用源(新浪)成功: {len(df)}条')
        return df
    except Exception as e:
        print(f'  OHLCV备用源(新浪)失败: {e}')
        return None


def fetch_pe_ttm(name_cn, start_date):
    """从 legulegu 获取指数PE_TTM"""
    try:
        df = ak.stock_index_pe_lg(symbol=name_cn)
        df = df[['日期', '滚动市盈率']].copy()
        df.columns = ['date', 'PE_TTM']
        df['date'] = pd.to_datetime(df['date'])
        df['PE_TTM'] = pd.to_numeric(df['PE_TTM'], errors='coerce')
        df = df.dropna(subset=['PE_TTM'])
        df = df[df['PE_TTM'] > 0]
        df = df[df['date'] >= start_date]
        df = df.set_index('date')
        df = df.sort_index()
        return df
    except Exception as e:
        print(f'  PE_TTM获取失败: {e}')
        return None


def fetch_bond_yield():
    """获取10年期中国国债收益率（全历史，用于写入xlsx）"""
    try:
        df = ak.bond_zh_us_rate()
        df = df[['日期', '中国国债收益率10年']].copy()
        df.columns = ['date', 'bond_yield']
        df['date'] = pd.to_datetime(df['date'])
        df['bond_yield'] = pd.to_numeric(df['bond_yield'], errors='coerce')
        df = df.dropna(subset=['bond_yield'])
        df = df.set_index('date')
        df = df.sort_index()
        return df
    except Exception as e:
        print(f'  国债收益率获取失败: {e}')
        return None


# ----- 显示名→legulegu中文名 映射 -----
LG_NAME_MAP = {
    '上证50': '上证50',
    '沪深300': '沪深300',
    '中证500': '中证500',
    '中证1000': '中证1000',
}


def update_xlsx_from_akshare():
    """用akshare增量更新xlsx文件（OHLCV + PE_TTM + 国债收益率）"""
    if not os.path.exists(XLSX_PATH):
        print(f'[错误] xlsx文件不存在: {XLSX_PATH}')
        return False

    print('正在从akshare获取全量数据（OHLCV + PE_TTM + 国债收益率）...')
    dict_data = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    updated = False

    # ---- 1. 更新OHLCV ----
    print('  --- OHLCV增量更新 ---')
    for display_name, cfg in TARGET_INDICES.items():
        code = cfg['code']
        sheet_key = cfg['sheet_key']
        sheet_name = None
        for sn in dict_data.keys():
            if sheet_key in sn or display_name in sn:
                sheet_name = sn
                break
        if sheet_name is None:
            print(f'  [跳过] 未找到{display_name}({code})对应的sheet')
            continue

        df = dict_data[sheet_name]
        df.index = pd.to_datetime(df.index)
        last_date = df.index.max().strftime('%Y-%m-%d')
        start_date = (pd.to_datetime(last_date) - timedelta(days=5)).strftime('%Y-%m-%d')

        new_data = fetch_akshare_daily(code, start_date)
        if new_data is None or len(new_data) == 0:
            print(f'  [{display_name}] OHLCV无新数据')
        else:
            existing_dates = set(df.index.strftime('%Y-%m-%d'))
            new_rows = new_data[~new_data.index.strftime('%Y-%m-%d').isin(existing_dates)]
            if len(new_rows) > 0:
                df = pd.concat([df, new_rows])
                updated = True
                print(f'  [{display_name}] OHLCV新增 {len(new_rows)} 条')
            else:
                print(f'  [{display_name}] OHLCV已是最新')

        df = df[~df.index.duplicated(keep='last')]
        df = df.sort_index()
        dict_data[sheet_name] = df

    # ---- 2. 更新PE_TTM ----
    print('  --- PE_TTM更新 ---')
    for display_name, cfg in TARGET_INDICES.items():
        sheet_key = cfg['sheet_key']
        sheet_name = None
        for sn in dict_data.keys():
            if sheet_key in sn or display_name in sn:
                sheet_name = sn
                break
        if sheet_name is None:
            continue

        df = dict_data[sheet_name]
        lg_name = LG_NAME_MAP.get(display_name, display_name)
        start_date = df.index.min().strftime('%Y-%m-%d')

        pe_data = fetch_pe_ttm(lg_name, start_date)
        if pe_data is not None and len(pe_data) > 0:
            # 将PE_TTM合并到df中
            before_cnt = df['PE_TTM'].notna().sum() if 'PE_TTM' in df.columns else 0
            df['PE_TTM'] = pe_data['PE_TTM']
            after_cnt = df['PE_TTM'].notna().sum()
            if 'PE_TTM' not in dict_data[sheet_name].columns or before_cnt != after_cnt:
                updated = True
            print(f'  [{display_name}] PE_TTM: {before_cnt}→{after_cnt} 条有效')
        dict_data[sheet_name] = df

    # ---- 3. 更新国债收益率 ----
    print('  --- 国债收益率更新 ---')
    bond_sheet = '国债收益率'
    bond_data = fetch_bond_yield()
    if bond_data is not None and len(bond_data) > 0:
        before_cnt = len(dict_data.get(bond_sheet, pd.DataFrame()))
        if bond_sheet in dict_data:
            existing = dict_data[bond_sheet]
            existing.index = pd.to_datetime(existing.index)
            # 合并
            combined = pd.concat([existing, bond_data])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
            dict_data[bond_sheet] = combined
        else:
            dict_data[bond_sheet] = bond_data
        after_cnt = len(dict_data[bond_sheet])
        updated = True
        print(f'  国债收益率: {before_cnt}→{after_cnt} 条')

    # ---- 4. 写入xlsx ----
    if updated:
        with pd.ExcelWriter(XLSX_PATH) as writer:
            for sn, df_sheet in dict_data.items():
                df_sheet.to_excel(writer, sheet_name=sn)
        print(f'xlsx文件已更新: {XLSX_PATH}')
    else:
        print('无需更新')
    return True


def main():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('RSRS择时指标数据获取与回测')
    print(f'运行时间: {now_str}')
    print(f'pandas: {pd.__version__}')
    if hasattr(ak, '__version__'):
        print(f'akshare: {ak.__version__}')
    print('=' * 60)

    # 1. 增量更新xlsx
    print('\n[1/3] 增量更新行情数据...')
    update_xlsx_from_akshare()

    # 2. 读取xlsx并计算RSRS
    print('\n[2/3] 计算RSRS指标并回测...')
    if not os.path.exists(XLSX_PATH):
        print(f'[错误] xlsx文件不存在: {XLSX_PATH}')
        sys.exit(1)

    dict_data = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    print(f'成功读取 {len(dict_data)} 个sheet: {list(dict_data.keys())}')

    results = {}
    for display_name, cfg in TARGET_INDICES.items():
        code = cfg['code']
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
        df = df.dropna(subset=['HIGH', 'LOW', 'CLOSE'])
        print(f'    数据量: {len(df)} 条, {df.index.min().date()} ~ {df.index.max().date()}')

        # 计算RSRS
        rsrs_df = compute_rsrs_indicators(df, N=cfg['N'], M=cfg['M'])

        # 对每种RSRS策略分别回测
        strategies = {}
        signals_map = {}
        for name, th in cfg['ths'].items():
            if name not in rsrs_df.columns:
                continue
            signal = rsrs_df[name].dropna()
            signals_map[name] = signal
            result = run_backtest(df['CLOSE'].loc[signal.index], signal, threshold=th)
            if result:
                result['threshold'] = th
                strategies[name] = result
                p = result['performance']
                print(f'    {name:>14} (th={th}): 年化={p["annual_return"]}, 夏普={p["sharpe"]}, 回撤={p["max_drawdown"]}, {result["latest_position"]}')

        if not strategies:
            print(f'    无有效策略回测结果')
            continue

        # 对齐所有策略到相同的日期范围（取各策略起始日期的最大值）
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
            sig = signals_map[name].reindex(common_dates)
            sd['signal'] = [
                round(float(v), 6) if pd.notna(v) else None
                for v in sig.values
            ]

        # 基准净值
        close_aligned = df['CLOSE'].loc[common_dates]
        p_ret = close_aligned.pct_change().fillna(0)
        bm_nav = (1 + p_ret).cumprod()
        bm_nav.iloc[0] = 1.0

        idx_data = {
            'dates': common_dates_str,
            'benchmark_nav': [round(v, 6) for v in bm_nav.values],
            'strategies': strategies,
        }
        results[display_name] = idx_data

    # 3. 输出JS数据文件
    print('\n[3/3] 输出JS数据文件...')
    os.makedirs(DATA_DIR, exist_ok=True)

    js_path = os.path.join(DATA_DIR, 'rsrs_data.js')
    with open(js_path, 'w', encoding='utf-8') as f:
        f.write('window.RSRS_DATA = ')
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
