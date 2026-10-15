# -*- coding: utf-8 -*-
"""
宏观数据下载与落盘（供宏观择时回测使用）

数据源优先级：akshare 的**新浪(Sina)**宏观接口优先；
新浪无对应接口时使用东方财富/商务数据中心，并在 meta sheet 中明确标注来源。

输出: data/宏观数据序列.xlsx
      - 每个指标一个 sheet（index = 月度日期，升序）
      - sheet 'meta'：记录各指标来源、字段、最新期数、下载时间
"""
import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import akshare as ak

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "宏观数据序列.xlsx")


def parse_month(v):
    """把各种月度写法统一成该月首日 Timestamp。"""
    s = str(v).strip()
    m = re.match(r"^(\d{4})\.(\d{1,2})$", s)          # 2026.7
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{4})年(\d{1,2})月", s)          # 2026年07月份
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    m = re.match(r"^(\d{4})(\d{2})$", s)               # 202603
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    try:                                                # 2026-08-01
        t = pd.Timestamp(s)
        return pd.Timestamp(t.year, t.month, 1)
    except Exception:
        return pd.NaT


def parse_quarter(v):
    """季度写法 -> 该季度末月首日（如 2026年第1-2季度 -> 2026-06-01）。"""
    s = str(v).strip()
    m = re.match(r"^(\d{4})年第(\d)(?:-(\d))?季度", s)
    if not m:
        return pd.NaT
    y = int(m.group(1))
    q = int(m.group(3) or m.group(2))
    return pd.Timestamp(y, min(q * 3, 12), 1)


# 每个指标：sheet名 / akshare函数 / 来源 / 日期列 / 季度型? / 需要的列
SPECS = [
    dict(sheet="货币供应量", src="新浪财经", fn="macro_china_supply_of_money",
         date_col="统计时间", quarterly=False,
         cols={"货币(狭义货币M1)同比增长": "M1同比",
               "货币和准货币（广义货币M2）同比增长": "M2同比"}),
    dict(sheet="央行资产负债表", src="新浪财经", fn="macro_china_central_bank_balance",
         date_col="统计时间", quarterly=False,
         cols={"总资产": "央行总资产", "储备货币": "储备货币"}),
    dict(sheet="新增信贷", src="东方财富", fn="macro_china_new_financial_credit",
         date_col="月份", quarterly=False,
         cols={"当月": "新增信贷当月", "累计": "新增信贷累计", "累计-同比增长": "新增信贷累计同比"}),
    dict(sheet="社会融资规模", src="商务数据中心", fn="macro_china_shrzgm",
         date_col="月份", quarterly=False,
         cols={"社会融资规模增量": "社融增量"}),
    dict(sheet="PMI", src="东方财富", fn="macro_china_pmi",
         date_col="月份", quarterly=False,
         cols={"制造业-指数": "制造业PMI", "非制造业-指数": "非制造业PMI"}),
    dict(sheet="CPI", src="东方财富", fn="macro_china_cpi",
         date_col="月份", quarterly=False,
         cols={"全国-同比增长": "CPI同比"}),
    dict(sheet="PPI", src="东方财富", fn="macro_china_ppi",
         date_col="月份", quarterly=False,
         cols={"当月同比增长": "PPI同比"}),
    dict(sheet="工业增加值", src="东方财富", fn="macro_china_gyzjz",
         date_col="月份", quarterly=False,
         cols={"同比增长": "工业增加值同比", "累计增长": "工业增加值累计同比"}),
    dict(sheet="GDP", src="东方财富", fn="macro_china_gdp",
         date_col="季度", quarterly=True,
         cols={"国内生产总值-同比增长": "GDP同比"}),
]


def fetch_one(spec, old_sheet):
    """抓取单个指标；失败时回退到已有 sheet。"""
    fn = getattr(ak, spec["fn"])
    raw = fn()
    date_col = spec["date_col"]
    if date_col not in raw.columns:
        raise KeyError("返回数据缺少日期列 %s，实际列=%s" % (date_col, list(raw.columns)))

    parse = parse_quarter if spec["quarterly"] else parse_month
    idx = raw[date_col].map(parse)
    data = {}
    for src_col, name in spec["cols"].items():
        if src_col not in raw.columns:
            continue
        data[name] = pd.to_numeric(raw[src_col], errors="coerce").values
    df = pd.DataFrame(data, index=pd.DatetimeIndex(idx))
    df = df[~df.index.isna()]
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(how="all")
    return df, len(raw)


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
    for spec in SPECS:
        sheet = spec["sheet"]
        try:
            df, nraw = fetch_one(spec, old.get(sheet))
            out[sheet] = df
            last = df.index.max()
            print("  [OK] %-10s 源=%-12s 期数=%-4d 最新=%s 列=%s"
                  % (sheet, spec["src"], len(df), str(last)[:7], list(df.columns)))
            meta.append(dict(指标=sheet, 来源=spec["src"], 接口=spec["fn"],
                             期数=len(df), 起始=str(df.index.min())[:10],
                             最新=str(last)[:10], 字段=",".join(df.columns)))
        except Exception as e:
            print("  [失败] %-10s %s: %s" % (sheet, type(e).__name__, str(e)[:160]))
            if spec["sheet"] in old:
                out[sheet] = old[spec["sheet"]]
                print("         -> 回退使用旧文件中的该 sheet（%d 行）" % len(old[spec["sheet"]]))
                meta.append(dict(指标=sheet, 来源=spec["src"] + "(旧文件回退)", 接口=spec["fn"],
                                 期数=len(old[spec["sheet"]]),
                                 起始=str(old[spec["sheet"]].index.min())[:10],
                                 最新=str(old[spec["sheet"]].index.max())[:10],
                                 字段=",".join(old[spec["sheet"]].columns)))
            else:
                traceback.print_exc()

    bj = datetime.now(timezone(timedelta(hours=8)))
    meta_df = pd.DataFrame(meta)
    meta_df.loc[len(meta_df)] = ["__下载时间__", str(bj)[:19] + " (北京)", "", "", "", "", ""]
    if out:
        out["meta"] = meta_df

    if not out:
        print("[错误] 无任何数据可写")
        return 1

    with pd.ExcelWriter(OUT) as w:
        for sheet, df in out.items():
            df.to_excel(w, sheet_name=sheet)
    print("\nsaved:", OUT)
    print("sheets:", list(out.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
