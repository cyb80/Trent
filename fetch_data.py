# -*- coding: utf-8 -*-
"""量化择时数据获取脚本

数据源 (均通过 akshare):
  指数 PE_TTM & 收盘价 → legulegu.com (stock_index_pe_lg)
  国债收益率          → investing.com (bond_zh_us_rate)
"""

import json
import os
import sys
from datetime import datetime

import akshare as ak
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

# legulegu 支持的中文名称
LG_NAMES = ['上证50', '沪深300', '中证500', '中证1000']


def fetch_bond_yield():
    """获取10年期中国国债收益率"""
    print('  正在获取国债收益率数据...')
    df = ak.bond_zh_us_rate()
    df = df[['日期', '中国国债收益率10年']].copy()
    df.columns = ['date', 'bond_yield']
    df['date'] = pd.to_datetime(df['date'])
    df['bond_yield'] = pd.to_numeric(df['bond_yield'], errors='coerce')
    df = df.dropna(subset=['bond_yield'])
    df = df.sort_values('date').reset_index(drop=True)
    print(f'  获取到 {len(df)} 条, 日期: {df["date"].min().date()} ~ {df["date"].max().date()}')
    return df


def fetch_index_pe_close(name_cn):
    """从 legulegu 获取指数 PE_TTM 与收盘价"""
    print(f'  正在获取 {name_cn} 数据...')
    df = ak.stock_index_pe_lg(symbol=name_cn)
    # 列: 日期, 指数(收盘价), 等权静态市盈率, 静态市盈率, 静态市盈率中位数,
    #     等权滚动市盈率, 滚动市盈率(PE_TTM), 滚动市盈率中位数
    df = df[['日期', '指数', '滚动市盈率']].copy()
    df.columns = ['date', 'close', 'pe_ttm']
    df['date'] = pd.to_datetime(df['date'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['pe_ttm'] = pd.to_numeric(df['pe_ttm'], errors='coerce')
    df = df.dropna(subset=['pe_ttm', 'close'])
    df = df[df['pe_ttm'] > 0]
    df = df.sort_values('date').reset_index(drop=True)
    print(f'    {len(df)} 条, {df["date"].min().date()} ~ {df["date"].max().date()}')
    return df


def calc_rolling_percentile(series, window_years=5):
    """计算滚动N年历史分位数

    分位数 = (当前值在窗口内的排名 - 1) / (窗口内有效值个数 - 1)
    排名按升序（值越大排名越大）。
    """
    result = pd.Series(np.nan, index=series.index)
    for i in range(len(series)):
        current_date = series.index[i]
        start_date = current_date - pd.DateOffset(years=window_years)
        window = series[(series.index >= start_date) & (series.index <= current_date)]
        if len(window) >= 2:
            rank = window.rank(ascending=True, method='min').iloc[-1]
            result.iloc[i] = (rank - 1) / (len(window) - 1)
    return result


def main():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('量化择时数据获取')
    print(f'运行时间: {now_str}')
    print(f'pandas: {pd.__version__}, akshare: {ak.__version__}')
    print('=' * 60)

    # 1. 国债收益率
    print('\n[1/3] 获取国债收益率...')
    bond = fetch_bond_yield()

    # 2. 指数 PE & 收盘价
    print('\n[2/3] 获取指数 PE 及收盘价...')
    all_data = {}
    for name_cn in LG_NAMES:
        print(f'\n  {name_cn}:')
        pe_close = fetch_index_pe_close(name_cn)
        df = pd.merge(pe_close, bond, on='date', how='inner')
        df = df.sort_values('date').reset_index(drop=True)
        print(f'    合并后: {len(df)} 条')

        # 股债性价比 = 1/PE_TTM * 100 - 国债收益率(%)
        df['stock_bond_spread'] = (1.0 / df['pe_ttm']) * 100 - df['bond_yield']
        # 格雷厄姆指数 = 国债收益率(%) / PE_TTM
        df['graham_index'] = df['bond_yield'] / df['pe_ttm']

        df = df.set_index('date')
        df['stock_bond_percentile'] = calc_rolling_percentile(df['stock_bond_spread'], 5)
        df['graham_percentile'] = calc_rolling_percentile(df['graham_index'], 5)
        df = df.reset_index()

        all_data[name_cn] = df

    # 3. 保存两份 JSON
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
            'data_source': '指数PE及收盘价: legulegu.com; 国债收益率: investing.com (均通过 akshare)',
        }

    stock_bond = {}
    graham = {}
    for name_cn, df in all_data.items():
        stock_bond[name_cn] = _build_json(df, 'stock_bond_spread', 'stock_bond_spread', 'stock_bond_percentile')
        graham[name_cn] = _build_json(df, 'graham_index', 'graham_index', 'graham_percentile')

    path_sb = os.path.join(DATA_DIR, 'stock_bond.json')
    with open(path_sb, 'w', encoding='utf-8') as f:
        json.dump(stock_bond, f, ensure_ascii=False, indent=2)
    print(f'  已保存: {path_sb}')

    path_gr = os.path.join(DATA_DIR, 'graham.json')
    with open(path_gr, 'w', encoding='utf-8') as f:
        json.dump(graham, f, ensure_ascii=False, indent=2)
    print(f'  已保存: {path_gr}')

    # 同时输出 JS 数据文件（内嵌全局变量，避免 fetch 跨域问题）
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
    for name_cn in LG_NAMES:
        d = stock_bond[name_cn]
        dg = graham[name_cn]
        print(f'\n{name_cn} ({d["latest_date"]}):')
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
