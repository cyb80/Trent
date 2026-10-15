# -*- coding: utf-8 -*-
"""V6 股指期货基差轮动 —— 期货全月份合约数据获取与落盘

V6 策略需要 IH/IF/IC/IM 四个品种的**全部在挂月份合约**日收盘价（候选池跨品种统一排名）。
本脚本负责把这份数据落盘并在每次运行时增量更新，供 gen_v6_backtest.py 重建回测。

数据源：中金所官方月度归档 zip（一次请求拿到该月全部交易日、全部合约，含远月与期权）
  http://www.cffex.com.cn/sj/historysj/{yyyymm}/zip/{yyyymm}.zip
  内部为 {yyyymmdd}_1.csv（gb2312 编码，列名含"合约代码/今收盘"），当月归档会实时更新。
  只保留 ^(IH|IF|IC|IM)\\d{4}$ 的股指期货月份合约，剔除期权(HO/IO/MO)与"小计/合计"行。

落盘: data/v6_futures.csv  (长表: date,contract,close)  每交易日约 16 行（4品种×4在挂月份）
"""
import io
import os
import re
import sys
import time
import zipfile
from datetime import datetime

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT = os.path.join(DATA_DIR, "v6_futures.csv")

BACKFILL_FROM = (2017, 12)      # 首次回填起点(需覆盖首个调仓日 2018-01-31 的收盘价)
TAIL_MONTHS = 3                # 增量运行时回补的最近月份数(防止归档修订/漏更)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/108.0.0.0 Safari/537.36"}
URL = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
PAT = re.compile(r"^(IH|IF|IC|IM)\d{4}$")

EMPTY = pd.DataFrame(columns=["date", "contract", "close"])


def months_between(y0, m0, y1, m1):
    """含首尾的 (year, month) 序列。"""
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def fetch_month(y, m):
    """下载某月归档 zip，解析出该月每个交易日的 IH/IF/IC/IM 月份合约收盘价。"""
    ym = "%04d%02d" % (y, m)
    raw = None
    for attempt in range(3):
        try:
            r = requests.get(URL.format(ym=ym), headers=HEADERS, timeout=60)
            if r.status_code == 200 and r.content[:2] == b"PK":
                raw = r.content
                break
        except Exception:
            pass
        time.sleep(1.0 + attempt)
    if raw is None:
        return EMPTY, 0

    frames = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        return EMPTY, 0
    for name in zf.namelist():
        mt = re.match(r"^(\d{8})_1\.csv$", os.path.basename(name))
        if not mt:
            continue
        try:
            txt = zf.read(name).decode("gb2312", errors="ignore")
            df = pd.read_csv(io.StringIO(txt), dtype=str)
        except Exception:
            continue
        df.columns = [str(c).strip() for c in df.columns]
        if "合约代码" not in df.columns or "今收盘" not in df.columns:
            continue
        sub = df[["合约代码", "今收盘"]].copy()
        sub.columns = ["contract", "close"]
        sub["contract"] = sub["contract"].astype(str).str.strip().str.upper()
        sub = sub[sub["contract"].str.match(PAT)]
        sub["close"] = pd.to_numeric(sub["close"], errors="coerce")
        sub = sub.dropna(subset=["close"])
        sub = sub[sub["close"] > 0]
        if sub.empty:
            continue
        sub["date"] = pd.Timestamp(mt.group(1))
        frames.append(sub)
    if not frames:
        return EMPTY, 0
    return pd.concat(frames, ignore_index=True), len(frames)


def fetch_range(y0, m0, y1, m1, tag):
    frames, ok = [], 0
    for y, m in months_between(y0, m0, y1, m1):
        d, n = fetch_month(y, m)
        if n:
            ok += 1
            frames.append(d)
        print("  [%s] %04d-%02d -> %d 个交易日 %d 行" % (tag, y, m, n, len(d)))
    out = pd.concat(frames, ignore_index=True) if frames else EMPTY.copy()
    return out, ok


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now()

    if os.path.exists(OUT):
        cur = pd.read_csv(OUT, parse_dates=["date"])
        last = cur["date"].max()
        print("状态文件存在 -> 增量更新（截至 %s）" % str(last.date()))
        # 回补最近月份（归档修订 / 上月月末数据补全）
        y0, m0 = last.year, last.month - TAIL_MONTHS + 1
        while m0 <= 0:
            m0 += 12
            y0 -= 1
        new, ok = fetch_range(y0, m0, now.year, now.month, "增量")
        if ok == 0:
            print("  [警告] 本月归档不可用，保留原数据")
            return 0
        add = new[new["date"] > last]
        print("  新增 %d 行 (%s ~ %s)" % (len(add), str(add["date"].min())[:10] if len(add) else "-",
                                        str(add["date"].max())[:10] if len(add) else "-"))
        df = pd.concat([cur, add], ignore_index=True)
    else:
        print("状态文件不存在 -> 全量回填 %d-%02d ~ %d-%02d"
              % (BACKFILL_FROM[0], BACKFILL_FROM[1], now.year, now.month))
        df, ok = fetch_range(BACKFILL_FROM[0], BACKFILL_FROM[1], now.year, now.month, "回填")
        if ok == 0:
            print("[错误] 未取到任何数据")
            return 1

    df = df.dropna(subset=["date", "contract", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[(df["close"] > 0) & (df["date"] >= pd.Timestamp(BACKFILL_FROM[0], BACKFILL_FROM[1], 1))]
    df = df.drop_duplicates(subset=["date", "contract"], keep="last").sort_values(["date", "contract"])
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df.to_csv(OUT, index=False, encoding="utf-8")

    d = pd.to_datetime(df["date"])
    print("\nsaved: %s  (%d 行)" % (OUT, len(df)))
    print("  区间: %s ~ %s   交易日数: %d" % (d.min().date(), d.max().date(), d.nunique()))
    print("  品种: %s   合约数: %d" % (sorted(df["contract"].str[:2].unique()), df["contract"].nunique()))
    print("  最新日期在挂合约: %s" % sorted(df[df["date"] == df["date"].max()]["contract"].tolist()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
