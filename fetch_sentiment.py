# -*- coding: utf-8 -*-
"""
情绪择时数据下载与落盘（供情绪择时回测/网页使用）

可用性说明（已实测 akshare 1.18.58）：
  [可用] 两融余额/融资买入额   macro_china_market_margin_sh / _sz   2010-03 起 日频
  [可用] 中国波指(50ETF QVIX) index_option_50etf_qvix                2015-02 起 日频
  [可用] 指数收盘/成交量       data/指数行情序列.xlsx（本地已落盘，派生为"市场行情"sheet）
  [不可用] 北向资金净买入      stock_hsgt_hist_em 自 2024-08 起净买额全为 NaN
  [不可用] 新增投资者          stock_account_statistics_em 2023-08 停更
  [不易用] 期权 PCR            option_daily_stats_sse 需逐日拉取（约 2800 次请求）
  [不易用] 换手率              stock_sse_deal_daily 需逐日拉取

输出: data/情绪数据序列.xlsx
      - sheet '两融'      : 沪深两融融资买入额/融资余额/融资融券余额/融资净买入（合计 + 分市场）
      - sheet '中国波指'  : 50ETF 期权隐含波动率(QVIX) 收盘
      - sheet '市场行情'  : 主要宽基指数 收盘价 与 成交量（从 指数行情序列.xlsx 派生）
      - sheet 'meta'      : 各指标来源、字段、最新日期、下载时间
"""
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "情绪数据序列.xlsx")
IDX_XLSX = os.path.join(HERE, "data", "指数行情序列.xlsx")

# 需要进入"市场行情" sheet 的指数
MARKET_INDEX = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000"}


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def fetch_margin():
    """沪深两融：融资买入额、融资余额、融资融券余额、融资净买入。"""
    sh = ak.macro_china_market_margin_sh()
    sz = ak.macro_china_market_margin_sz()
    for df in (sh, sz):
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    sh = sh.dropna(subset=["日期"]).set_index("日期").sort_index()
    sz = sz.dropna(subset=["日期"]).set_index("日期").sort_index()

    out = pd.DataFrame(index=sh.index.union(sz.index).sort_values())
    out["沪融资买入额"] = _num(sh["融资买入额"])
    out["深融资买入额"] = _num(sz["融资买入额"])
    out["沪融资余额"] = _num(sh["融资余额"])
    out["深融资余额"] = _num(sz["融资余额"])
    out["融资买入额"] = _num(sh["融资买入额"]).reindex(out.index).fillna(0) + \
                        _num(sz["融资买入额"]).reindex(out.index).fillna(0)
    out["融资余额"] = _num(sh["融资余额"]).reindex(out.index).fillna(0) + \
                      _num(sz["融资余额"]).reindex(out.index).fillna(0)
    out = out.replace(0, np.nan)
    # 融资净买入 ≈ 融资余额日变化（口径：余额=买入-偿还）
    out["融资净买入"] = out["融资余额"].diff()
    out["融资净买入_20日均"] = out["融资净买入"].rolling(20, min_periods=10).mean()
    out = out.dropna(how="all")
    return out


def fetch_qvix():
    """中国波指：50ETF 期权隐含波动率（QVIX）。"""
    df = ak.index_option_50etf_qvix()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    out["中国波指"] = _num(df["close"])
    return out.dropna(how="all")


def fetch_market():
    """从 指数行情序列.xlsx 派生宽基指数收盘价与成交量。"""
    xl = pd.ExcelFile(IDX_XLSX)
    out = pd.DataFrame()
    for sheet, name in MARKET_INDEX.items():
        if sheet not in xl.sheet_names:
            print("  [警告] %s 缺失，跳过" % sheet)
            continue
        df = xl.parse(sheet)
        df["Unnamed: 0"] = pd.to_datetime(df["Unnamed: 0"], errors="coerce")
        df = df.dropna(subset=["Unnamed: 0"]).set_index("Unnamed: 0").sort_index()
        out["%s_收盘" % name] = _num(df["CLOSE"])
        out["%s_成交量" % name] = _num(df["VOLUME"])
    return out.dropna(how="all")


SPECS = [
    ("两融", "akshare 新浪财经(margin_sh/sz)", fetch_margin),
    ("中国波指", "akshare 新浪财经(50ETF QVIX)", fetch_qvix),
    ("市场行情", "本地 指数行情序列.xlsx 派生", fetch_market),
]


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    old = {}
    if os.path.exists(OUT):
        try:
            old = pd.read_excel(OUT, index_col=0, sheet_name=None)
            print("已存在文件，读取到 %d 个 sheet" % len(old))
        except Exception as e:
            print("[警告] 旧文件读取失败: %s" % e)

    out, meta = {}, []
    for sheet, src, fn in SPECS:
        try:
            df = fn()
            out[sheet] = df
            print("  [OK] %-8s 源=%-32s 期数=%-5d 区间=%s ~ %s 列=%s"
                  % (sheet, src, len(df), str(df.index.min())[:10],
                     str(df.index.max())[:10], list(df.columns)))
            meta.append(dict(指标=sheet, 来源=src, 期数=len(df),
                             起始=str(df.index.min())[:10], 最新=str(df.index.max())[:10],
                             字段=",".join(df.columns)))
        except Exception as e:
            print("  [失败] %-8s %s: %s" % (sheet, type(e).__name__, str(e)[:160]))
            if sheet in old:
                out[sheet] = old[sheet]
                print("         -> 回退使用旧文件中的该 sheet（%d 行）" % len(old[sheet]))
                meta.append(dict(指标=sheet, 来源=src + "(旧文件回退)", 期数=len(old[sheet]),
                                 起始=str(old[sheet].index.min())[:10],
                                 最新=str(old[sheet].index.max())[:10],
                                 字段=",".join(old[sheet].columns)))
            else:
                traceback.print_exc()

    if not out:
        print("[错误] 无任何数据可写")
        return 1

    bj = datetime.now(timezone(timedelta(hours=8)))
    meta_df = pd.DataFrame(meta)
    meta_df.loc[len(meta_df)] = ["__下载时间__", str(bj)[:19] + " (北京)", "", "", "", ""]
    out["meta"] = meta_df

    with pd.ExcelWriter(OUT) as w:
        for sheet, df in out.items():
            df.to_excel(w, sheet_name=sheet)
    print("\nsaved:", OUT)
    print("sheets:", list(out.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
