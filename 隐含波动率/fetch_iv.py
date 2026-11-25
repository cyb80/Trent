# -*- coding: utf-8 -*-
"""中证1000 隐含波动率反算（MO 股指期权 + Black-76）

数据源：中金所官方月度归档 zip（与 fetch_v6_futures.py 同源，无需 key）
    http://www.cffex.com.cn/sj/historysj/{yyyymm}/zip/{yyyymm}.zip
    内部为 {yyyymmdd}_1.csv（gb2312），含 IM 期货与 MO 期权日行情（今结算/Delta）。

方法：
    1. 以同月 IM 期货“今结算”作为远期价格 F（欧式期权，到期日与期货同日）。
    2. 每个 MO 合约用 Black-76 反解隐含波动率（二分法）。
    3. 近月 ATM 波动率：认购取 K>=F、认沽取 K<=F，按行权价线性插值到 K=F。
    4. 取最近两个到期日的 ATM 方差做时间插值，得到 30 天恒定到期 IV（类 VIX 口径）。

输出：
    隐含波动率/iv_history.csv  增量缓存（首次运行自动回填 2022-07 至今）
    data/iv_data.js            window.IV_DATA（页面直接 <script> 引入）
"""
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_JS = os.path.join(ROOT, "data", "iv_data.js")
CACHE = os.path.join(HERE, "iv_history.csv")

START_YM = (2022, 7)          # MO 期权 2022-07-22 上市
TAIL_MONTHS = 3               # 增量运行时重算最近几个月（覆盖归档修订/漏更）
RATE = float(os.environ.get("IV_RATE", "0.015"))   # 无风险利率（Black-76 仅影响贴现项）
URL = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/108.0.0.0 Safari/537.36"}

MO_PAT = re.compile(r"^MO(\d{2})(\d{2})-([CP])-(\d+)$")
IM_PAT = re.compile(r"^IM(\d{2})(\d{2})$")


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _third_friday(y, m):
    d = date(y, m, 1)
    first_friday = 1 + (4 - d.weekday()) % 7
    return date(y, m, first_friday + 14)


def _black76(F, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0 or F <= 0 or K <= 0:
        return 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sq
    d2 = d1 - sq
    if is_call:
        return math.exp(-r * T) * (F * _ncdf(d1) - K * _ncdf(d2))
    return math.exp(-r * T) * (K * _ncdf(-d2) - F * _ncdf(-d1))


def implied_vol(F, K, T, r, price, is_call):
    """Black-76 反解，返回小数波动率；价格不在无套利区间内返回 None。"""
    disc = math.exp(-r * T)
    if is_call:
        lo = max(disc * (F - K), 0.0)
        hi = disc * F
    else:
        lo = max(disc * (K - F), 0.0)
        hi = disc * K
    if not (price > lo + 1e-6 and price < hi - 1e-6):
        return None
    a, b = 1e-4, 3.0
    if _black76(F, K, T, r, b, is_call) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (a + b)
        if _black76(F, K, T, r, mid, is_call) > price:
            b = mid
        else:
            a = mid
    return 0.5 * (a + b)


def _download_month(y, m):
    ym = "%04d%02d" % (y, m)
    for attempt in range(3):
        try:
            r = requests.get(URL.format(ym=ym), headers=HEADERS, timeout=60)
            if r.status_code == 200 and r.content[:2] == b"PK":
                return zipfile.ZipFile(io.BytesIO(r.content))
        except Exception:
            pass
        time.sleep(1.0 + attempt)
    return None


def _load_month(y, m):
    """返回 [(date, DataFrame[code, settle])]，按月内交易日升序。"""
    zf = _download_month(y, m)
    if zf is None:
        print("  [warn] %04d-%02d 归档下载失败" % (y, m))
        return []
    out = []
    for name in sorted(zf.namelist()):
        mt = re.match(r"^(\d{8})_1\.csv$", os.path.basename(name))
        if not mt:
            continue
        try:
            txt = zf.read(name).decode("gb2312", errors="ignore")
            df = pd.read_csv(io.StringIO(txt), dtype=str)
        except Exception:
            continue
        df.columns = [str(c).strip() for c in df.columns]
        if "合约代码" not in df.columns:
            continue
        df = df.rename(columns={"合约代码": "code"})
        settle = df["今结算"] if "今结算" in df.columns else df.get("今收盘")
        tmp = pd.DataFrame({
            "code": df["code"].astype(str).str.strip(),
            "settle": _num(settle),
        })
        out.append((datetime.strptime(mt.group(1), "%Y%m%d").date(), tmp))
    return out


def _atm_iv(points, F):
    """points: {行权价: [iv,...]}，按行权价线性插值到 F。"""
    ks = sorted(points)
    if not ks:
        return None
    if F <= ks[0]:
        return sum(points[ks[0]]) / len(points[ks[0]])
    if F >= ks[-1]:
        return sum(points[ks[-1]]) / len(points[ks[-1]])
    for i in range(len(ks) - 1):
        if ks[i] <= F <= ks[i + 1]:
            v1 = sum(points[ks[i]]) / len(points[ks[i]])
            v2 = sum(points[ks[i + 1]]) / len(points[ks[i + 1]])
            w = (F - ks[i]) / (ks[i + 1] - ks[i])
            return v1 * (1 - w) + v2 * w
    return None


def _iv30(curve):
    """curve: [(T, iv)] 升序；用方差线性插值到 30 天。"""
    t30 = 30.0 / 365.0
    if not curve:
        return None
    if t30 <= curve[0][0]:
        return curve[0][1]
    if t30 >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t1, v1 = curve[i]
        t2, v2 = curve[i + 1]
        if t1 <= t30 <= t2:
            w = (t2 - t30) / (t2 - t1)
            var = w * v1 * v1 * t1 + (1 - w) * v2 * v2 * t2
            return math.sqrt(max(var, 0.0) / t30)
    return curve[-1][1]


def _compute_day(d, df, futures_last, latest_date):
    """单个交易日的最近月 + 30 天 ATM IV。"""
    im = {}
    for code, settle in zip(df["code"], df["settle"]):
        mt = IM_PAT.match(code)
        if mt and pd.notna(settle):
            im[mt.group(1) + mt.group(2)] = float(settle)

    opts = {}
    for code, settle in zip(df["code"], df["settle"]):
        mt = MO_PAT.match(code)
        if not mt or pd.isna(settle) or settle <= 0:
            continue
        ym = (2000 + int(mt.group(1)), int(mt.group(2)))
        opts.setdefault(ym, []).append((int(mt.group(4)), mt.group(3) == "C", float(settle)))

    curve, front = [], None
    for (y, m), rows in sorted(opts.items()):
        # 到期日：已到期合约用期货最后出现日（含节假日顺延），未到期用第三个星期五
        month_end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
        if month_end < latest_date:
            exp = futures_last.get((y, m))
            if exp is None:
                continue
        else:
            exp = _third_friday(y, m)
        dte = (exp - d).days
        if dte < 1:
            continue
        T = dte / 365.0
        F = im.get("%02d%02d" % (y % 100, m))
        if not F:
            continue

        points = {}
        for K, is_call, px in rows:
            if not (0.85 * F <= K <= 1.15 * F) or px < 1.0:
                continue
            if is_call and K < F:
                continue
            if (not is_call) and K > F:
                continue
            iv = implied_vol(F, K, T, RATE, px, is_call)
            if iv is not None and 0.01 < iv < 2.5:
                points.setdefault(K, []).append(iv)

        # 近 ATM 无有效报价时放宽至全部行权价
        if len(points) < 2:
            for K, is_call, px in rows:
                if not (0.85 * F <= K <= 1.15 * F):
                    continue
                iv = implied_vol(F, K, T, RATE, px, is_call)
                if iv is not None and 0.01 < iv < 2.5:
                    points.setdefault(K, []).append(iv)

        atm = _atm_iv(points, F)
        if atm is None:
            continue
        curve.append((T, atm))
        if front is None:
            front = (atm, dte, F, exp)

    if front is None:
        return None
    return {
        "date": d.isoformat(),
        "iv": round(front[0] * 100, 2),
        "iv30": round((_iv30(curve) or front[0]) * 100, 2),
        "dte": front[1],
        "fwd": round(front[2], 2),
        "expiry": front[3].isoformat(),
    }


def _months(start, end):
    y, m = start
    out = []
    while (y, m) <= end:
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def main():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    end = (today.year, today.month)

    cache = None
    if os.path.exists(CACHE):
        try:
            cache = pd.read_csv(CACHE, dtype={"date": str})
        except Exception:
            cache = None

    force = os.environ.get("IV_FULL", "") in ("1", "true", "yes")
    if cache is None or cache.empty or force:
        start = START_YM
    else:
        # 重算最近 TAIL_MONTHS 个月
        y, m = end
        for _ in range(TAIL_MONTHS - 1):
            m -= 1
            if m < 1:
                m, y = 12, y - 1
        start = (y, m)

    print("抓取区间: %04d-%02d ~ %04d-%02d" % (start[0], start[1], end[0], end[1]))
    frames = []
    for (y, m) in _months(start, end):
        days = _load_month(y, m)
        if days:
            print("  %04d-%02d: %d 个交易日" % (y, m, len(days)))
        frames.extend(days)
    if not frames:
        print("[error] 未取得任何中金所数据，保留原文件")
        return 1

    latest_date = max(d for d, _ in frames)

    # 期货最后出现日（用于已到期月份的真实到期日）
    futures_last = {}
    for d, df in frames:
        for code in df["code"]:
            mt = IM_PAT.match(code)
            if mt:
                key = (2000 + int(mt.group(1)), int(mt.group(2)))
                if key not in futures_last or d > futures_last[key]:
                    futures_last[key] = d

    rows = []
    for d, df in frames:
        rec = _compute_day(d, df, futures_last, latest_date)
        if rec:
            rows.append(rec)
    if not rows:
        print("[error] 未反算出任何 IV，保留原文件")
        return 1

    new = pd.DataFrame(rows)
    if cache is not None and not cache.empty:
        # 新结果只覆盖成功重算的日期；失败月份不能把旧缓存一起删掉。
        merged = pd.concat([cache, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    merged["date"] = merged["date"].astype(str)
    merged.to_csv(CACHE, index=False, encoding="utf-8")

    latest = merged.iloc[-1].to_dict()
    payload = {
        "updated": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "source": "中金所 MO 股指期权 / Black-76 反算",
        "unit": "%",
        "start": merged["date"].iloc[0],
        "count": int(len(merged)),
        "dates": merged["date"].tolist(),
        "iv": merged["iv"].tolist(),
        "iv30": merged["iv30"].tolist(),
        "dte": merged["dte"].astype(int).tolist(),
        "fwd": merged["fwd"].tolist(),
        "expiry": merged["expiry"].tolist(),
        "latest": {
            "date": latest["date"],
            "iv": float(latest["iv"]),
            "iv30": float(latest["iv30"]),
            "dte": int(latest["dte"]),
            "fwd": float(latest["fwd"]),
            "expiry": latest["expiry"],
        },
    }

    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("// 中证1000 隐含波动率（MO 期权反算），由 隐含波动率/fetch_iv.py 自动生成\n")
        f.write("window.IV_DATA = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print("最新: %s  近月IV=%.2f%%  30天IV=%.2f%%  剩余%d天  远期=%.0f"
          % (latest["date"], latest["iv"], latest["iv30"], latest["dte"], latest["fwd"]))
    print("已写入 %s（%d 条）" % (OUT_JS, len(merged)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
