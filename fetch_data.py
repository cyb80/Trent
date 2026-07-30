# -*- coding: utf-8 -*-
"""量化择时数据获取脚本

数据源:
  指数 PE_TTM & 收盘价 & 国债收益率 → data/指数行情序列.xlsx
  （xlsx由 fetch_rsrs.py 每日统一更新）
"""

import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')

# 指数配置: (sheet名关键词, 显示名)
INDEX_CONFIG = [
    ('000016', '上证50'),
    ('000300', '沪深300'),
    ('000905', '中证500'),
    ('000852', '中证1000'),
]


def calc_rolling_percentile(series, window_years=5):
    """计算滚动N年历史分位数"""
    result = pd.Series(np.nan, index=series.index)
    for i in range(len(series)):
        current_date = series.index[i]
        start_date = current_date - pd.DateOffset(years=window_years)
        window = series[(series.index >= start_date) & (series.index <= current_date)]
        if len(window) >= 2:
            rank = window.rank(ascending=True, method='min').iloc[-1]
            result.iloc[i] = (rank - 1) / (len(window) - 1)
    return result


def read_index_data(dict_data, sheet_key):
    """从xlsx读取指定指数的 CLOSE 和 PE_TTM"""
    sheet_name = None
    for sn in dict_data.keys():
        if sheet_key in sn:
            sheet_name = sn
            break
    if sheet_name is None:
        return None

    df = dict_data[sheet_name].copy()
    df.index = pd.to_datetime(df.index)

    # 标准化列名
    col_map = {}
    for c in df.columns:
        cu = c.upper().strip()
        if cu in ('CLOSE', '收盘价', '收盘'):
            col_map[c] = 'close'
        elif cu in ('PE_TTM', '滚动市盈率', '滚动市盈率'):
            col_map[c] = 'pe_ttm'
    df = df.rename(columns=col_map)

    if 'close' not in df.columns or 'pe_ttm' not in df.columns:
        return None

    df = df[['close', 'pe_ttm']].dropna()
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['pe_ttm'] = pd.to_numeric(df['pe_ttm'], errors='coerce')
    df = df.dropna()
    df = df[df['pe_ttm'] > 0]
    df = df.sort_index()
    return df


def main():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('量化择时数据获取（从xlsx读取）')
    print(f'运行时间: {now_str}')
    print(f'pandas: {pd.__version__}')
    print('=' * 60)

    if not os.path.exists(XLSX_PATH):
        print(f'\n[错误] xlsx文件不存在: {XLSX_PATH}')
        print('请先运行 python fetch_rsrs.py 生成/更新数据文件')
        sys.exit(1)

    # 1. 读取xlsx
    print('\n[1/3] 从xlsx读取数据...')
    dict_data = pd.read_excel(XLSX_PATH, index_col=0, sheet_name=None)
    print(f'  读取到 {len(dict_data)} 个sheet: {list(dict_data.keys())}')

    # 2. 提取并计算
    print('\n[2/3] 计算指标...')
    bond_sheet = '国债收益率'
    if bond_sheet in dict_data:
        bond_df = dict_data[bond_sheet].copy()
        bond_df.index = pd.to_datetime(bond_df.index)
        # 列名可能是 'bond_yield' 或 '中国国债收益率10年'，统一处理
        bond_col = None
        for c in bond_df.columns:
            cu = c.upper().strip()
            if '10' in cu or '国债' in cu or 'BOND' in cu or 'YIELD' in cu:
                bond_col = c
                break
        if bond_col is None:
            print(f'  [错误] 国债收益率sheet中未找到收益率列')
            bond_df = None
        else:
            bond_df = bond_df[[bond_col]].rename(columns={bond_col: 'bond_yield'})
            bond_df['bond_yield'] = pd.to_numeric(bond_df['bond_yield'], errors='coerce')
            bond_df = bond_df.dropna()
            print(f'  国债收益率: {len(bond_df)} 条, {bond_df.index.min().date()} ~ {bond_df.index.max().date()}')
    else:
        print(f'  [错误] xlsx中未找到"{bond_sheet}" sheet')
        bond_df = None

    if bond_df is None or len(bond_df) == 0:
        print('\n国债收益率数据不可用，股债性价比和格雷厄姆指数无法计算')
        sys.exit(1)

    all_data = {}
    for sheet_key, display_name in INDEX_CONFIG:
        print(f'\n  {display_name}:')
        df = read_index_data(dict_data, sheet_key)
        if df is None:
            print(f'    未找到对应sheet或缺少close/pe_ttm列')
            continue

        print(f'    指数数据: {len(df)} 条, {df.index.min().date()} ~ {df.index.max().date()}')

        # 与国债收益率合并
        merged = pd.merge(
            df, bond_df, left_index=True, right_index=True, how='inner'
        ).sort_index().reset_index()
        merged = merged.rename(columns={'index': 'date'})
        print(f'    合并后: {len(merged)} 条')

        if len(merged) < 2:
            print(f'    数据不足，跳过')
            continue

        # 股债性价比 = 1/PE_TTM * 100 - 国债收益率(%)
        merged['stock_bond_spread'] = (1.0 / merged['pe_ttm']) * 100 - merged['bond_yield']
        # 格雷厄姆指数 = (1/PE_TTM * 100) / 国债收益率(%)
        merged['graham_index'] = (1.0 / merged['pe_ttm']) * 100 / merged['bond_yield']

        merged = merged.set_index('date')
        merged['stock_bond_percentile'] = calc_rolling_percentile(merged['stock_bond_spread'], 5)
        merged['graham_percentile'] = calc_rolling_percentile(merged['graham_index'], 5)
        merged = merged.reset_index()

        all_data[display_name] = merged

    if not all_data:
        print('\n无有效数据，退出')
        sys.exit(1)

    # 3. 输出JSON+JS
    print('\n[3/3] 保存数据文件...')
    os.makedirs(DATA_DIR, exist_ok=True)

    def _build_json(df, metric_key, metric_col, percentile_col):
        return {
            'dates': df['date'].dt.strftime('%Y-%m-%d').tolist(),
            metric_key: df[metric_col].round(4).tolist(),
            'percentile': df[percentile_col].round(4).tolist(),
            'close': df['close'].round(2).tolist(),
            'latest_date': df['date'].max().strftime('%Y-%m-%d'),
            f'latest_{metric_key}': round(float(df[metric_col].iloc[-1]), 4),
            'latest_percentile': round(float(df[percentile_col].iloc[-1]), 4),
            'latest_close': float(df['close'].iloc[-1].round(2)),
            'latest_pe': round(float(df['pe_ttm'].iloc[-1]), 2),
            'latest_bond_yield': round(float(df['bond_yield'].iloc[-1]), 4),
            'data_source': '数据来源: 指数行情序列.xlsx (OHLCV:东方财富; PE:乐股乐股; 国债:investing.com)',
        }

    stock_bond = {}
    graham = {}
    for display_name, df in all_data.items():
        stock_bond[display_name] = _build_json(df, 'stock_bond_spread', 'stock_bond_spread', 'stock_bond_percentile')
        graham[display_name] = _build_json(df, 'graham_index', 'graham_index', 'graham_percentile')

    path_sb_js = os.path.join(DATA_DIR, 'stock_bond_data.js')
    with open(path_sb_js, 'w', encoding='utf-8') as f:
        f.write('window.STOCK_BOND_DATA = ')
        json.dump(stock_bond, f, ensure_ascii=False)
        f.write(';\n')
    print(f'  已保存: {path_sb_js}')

    path_gr_js = os.path.join(DATA_DIR, 'graham_data.js')
    with open(path_gr_js, 'w', encoding='utf-8') as f:
        f.write('window.GRAHAM_DATA = ')
        json.dump(graham, f, ensure_ascii=False)
        f.write(';\n')
    print(f'  已保存: {path_gr_js}')

    # 汇总
    print('\n' + '=' * 60)
    print('数据汇总')
    print('=' * 60)
    for display_name in [n for _, n in INDEX_CONFIG]:
        d = stock_bond.get(display_name)
        dg = graham.get(display_name)
        if d and dg:
            print(f'\n{display_name} ({d["latest_date"]}):')
            print(f'  PE_TTM={d["latest_pe"]}, 国债={d["latest_bond_yield"]}%, 收盘={d["latest_close"]}')
            print(f'  股债性价比={d["latest_stock_bond_spread"]}  分位数={d["latest_percentile"]}')
            print(f'  格雷厄姆指数={dg["latest_graham_index"]}  分位数={dg["latest_percentile"]}')

    print('\n完成!')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n运行出错: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
