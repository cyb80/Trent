# -*- coding: utf-8 -*-
"""ETF 国家队看板 —— 数据采集 + 预计算 + 静态化输出 (GitHub Pages 版)

原项目 (kitaki-Ciallo/etf-national-tracker) 依赖 Flask + PostgreSQL 在线聚合;
本项目改为: 采集结果落增量 CSV -> 在这里完成全部聚合 -> 生成
data/etf_nt_data.js (window.ETF_NT_DATA), 前端纯静态渲染。

用法:
    python fetch_etf_nt.py --init      # 首次: 回溯全部历史份额(2012-05起) + 全历史价格 + 持有人
    python fetch_etf_nt.py             # 日常: 补齐最新份额/价格/持有人并重建 JS
    python fetch_etf_nt.py --rescan    # 重扫全市场 ETF, 重建国家队标的池
    python fetch_etf_nt.py --no-fetch  # 不抓数据, 只用现有 CSV 重建 JS
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UNIVERSE_PATH = os.path.join(DATA_DIR, "etf_nt_universe.json")
SHARE_PATH = os.path.join(DATA_DIR, "etf_nt_share.csv")
PRICE_PATH = os.path.join(DATA_DIR, "etf_nt_price.csv")
HOLDER_PATH = os.path.join(DATA_DIR, "etf_nt_holder.json")
OUT_PATH = os.path.join(DATA_DIR, "etf_nt_data.js")

# 国家队持有人关键词 (精确匹配, 避免 "中证金融科技"、"深圳证金私募" 之类误判)
NT_KEYWORDS = ["中央汇金", "汇金资管", "全国社保", "社保基金", "基本养老",
               "中国证券金融", "证金公司", "证券金融"]
# 主页叠加的指数 ETF
INDEX_ETFS = {"510300": "沪深300ETF", "510500": "中证500ETF", "512100": "中证1000ETF"}

LOOKBACK_DAYS = 400     # 增量模式下无历史时默认回看的日历天数
HISTORY_START = "2012-01-04"   # 上交所占份额接口最早可查日 (2011-12-30 及更早返回空)
PRICE_HISTORY_BARS = 6000      # 初始化时东方财富取全历史(足够覆盖全部上市期)
REQUEST_DELAY = 0.25
MAX_RETRIES = 3
SSE_WORKERS = 6         # 上交所历史份额并发线程数
HOLDER_WORKERS = 4      # 持有人并发线程数
BJT = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def now_bjt():
    return datetime.now(BJT)


def is_sh(code):
    return not str(code).startswith("159")


def market_prefix(code):
    return "sz" if str(code).startswith("159") else "sh"


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


SESSION = new_session()


def http_get(url, params=None, headers=None, timeout=30, retries=MAX_RETRIES):
    """带重试的 GET, 全部失败返回 None。"""
    for i in range(1, retries + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        if i < retries:
            time.sleep(2 * i)
    return None


# ─────────────────────────── 数据源: 份额 ───────────────────────────
def fetch_sse_etfs(date_str):
    """上交所 ETF 份额 (TOT_VOL 单位为万份), 返回 {code: (name, share_份)}."""
    d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    url = ("https://query.sse.com.cn/commonQuery.do?"
           "isPagination=true&pageHelp.pageSize=10000&pageHelp.pageNo=1"
           "&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
           f"&STAT_DATE={d}")
    headers = {"Referer": "https://www.sse.com.cn/"}
    text = None
    # 偶发超时/返回空正文会导致当天份额漏采, 失败时重试一次
    for attempt in range(2):
        try:
            r = SESSION.get(url, headers=headers, timeout=60, verify=False)
            if r.status_code == 200 and r.text:
                text = r.text
                break
        except Exception as e:
            if attempt == 1:
                print(f"  [份额] requests 失败: {type(e).__name__}: {e}")
        time.sleep(1.5)
    if not text:
        # 云服务器/海外 IP 有时需要 curl 通道
        try:
            p = subprocess.run(["curl", "-s", "--max-time", "60",
                                "-H", "Referer: https://www.sse.com.cn/",
                                "-H", f"User-Agent: {UA}", url],
                               capture_output=True, timeout=65)
            if p.returncode == 0:
                text = p.stdout.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [份额] curl 通道失败: {e}")
    if not text:
        return {}
    try:
        rows = json.loads(text).get("result") or []
    except Exception:
        return {}
    out = {}
    for item in rows:
        code = str(item.get("SEC_CODE", "")).strip()
        try:
            share = float(item.get("TOT_VOL", 0)) * 10000
        except (TypeError, ValueError):
            continue
        if code and share > 0:
            out[code] = (str(item.get("SEC_NAME", "")).strip(), share)
    return out


def fetch_sse_shares(date_str):
    return {k: v[1] for k, v in fetch_sse_etfs(date_str).items()}


def fetch_szse_snapshot():
    """深交所 ETF 当日快照, 返回 (日期, {code: (name, share_份)})。"""
    url = "https://www.szse.cn/api/report/ShowReport/data"
    headers = {"Referer": "https://www.szse.cn/market/product/list/etfList/index.html",
               "Accept": "application/json, text/javascript, */*; q=0.01"}
    first = http_get(url, params={"SHOWTYPE": "JSON", "CATALOGID": "1945",
                                  "TABKEY": "tab1", "PAGENO": "1"},
                     headers=headers, timeout=20)
    if first is None:
        return None, {}
    try:
        meta = first.json()[0].get("metadata", {})
        pages = int(meta.get("pagecount", 1))
    except Exception:
        return None, {}
    records = list(first.json()[0].get("data") or [])
    for page in range(2, pages + 1):
        r = http_get(url, params={"SHOWTYPE": "JSON", "CATALOGID": "1945",
                                  "TABKEY": "tab1", "PAGENO": str(page)},
                     headers=headers, timeout=20)
        if r is None:
            continue
        try:
            records.extend(r.json()[0].get("data") or [])
        except Exception:
            continue
        time.sleep(REQUEST_DELAY)

    out = {}
    for rec in records:
        m = re.search(r"code=(\d{6})", rec.get("sys_key", ""))
        if not m:
            continue
        name_m = re.search(r"<u>(.*?)</u>", rec.get("kzjcurl", ""))
        name = name_m.group(1).strip() if name_m else ""
        try:
            share = float(str(rec.get("dqgm", "0")).replace(",", "").strip()) * 10000
        except ValueError:
            continue
        out[m.group(1)] = (name, share)
    return meta.get("subname"), out


# ─────────────────────────── 数据源: 价格 ───────────────────────────
def fetch_sina_kline(code, datalen):
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/"
           "CN_MarketDataService.getKLineData"
           f"?symbol={market_prefix(code)}{code}&scale=240&ma=no&datalen={datalen}")
    r = http_get(url, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=20)
    if r is None:
        return []
    m = re.search(r"var\s+_data\s*=\s*\((\[.*?\])\)", r.text, re.DOTALL)
    if not m:
        return []
    try:
        rows = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for k in rows:
        try:
            out.append((k["day"], float(k["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_em_kline(code, datalen):
    """东方财富日 K 备源。"""
    secid = ("1." if is_sh(code) else "0.") + str(code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    r = http_get(url, params={"secid": secid, "klt": "101", "fqt": "1",
                              "fields1": "f1,f2,f3,f4,f5,f6",
                              "fields2": "f51,f53", "beg": "0", "end": "20500101",
                              "lmt": str(datalen)}, timeout=20)
    if r is None:
        return []
    try:
        klines = r.json().get("data", {}).get("klines") or []
    except Exception:
        return []
    out = []
    for line in klines:
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                out.append((parts[0], float(parts[1])))
            except ValueError:
                continue
    return out


def fetch_prices(code, datalen):
    """东方财富可取全历史(含上市首日), 新浪作为备源 (其 datalen 上限约 1500)."""
    rows = fetch_em_kline(code, datalen)
    if not rows:
        print(f"  [价格] {code} 东方财富失败, 切新浪")
        rows = fetch_sina_kline(code, min(datalen, 1500))
    return rows


# ─────────────────────────── 数据源: 十大持有人 ───────────────────────────
def fetch_report_dates(code):
    url = f"https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJCYR.php?symbol={code}"
    r = http_get(url, timeout=20)
    if r is None:
        return []
    r.encoding = "gbk"
    return re.findall(r'<option value="(\d{4}-\d{2}-\d{2})"', r.text)


def fetch_holders(code, report_date):
    url = ("https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/"
           "CaihuiFundInfoService.getFundHolder")
    headers = {"Referer": ("https://stock.finance.sina.com.cn/fundInfo/view/"
                           f"FundInfo_JJCYR.php?symbol={code}")}
    r = http_get(url, params={"symbol": code, "date": report_date},
                 headers=headers, timeout=20)
    if r is None:
        return []
    try:
        raw = r.json().get("result", {}).get("data") or []
    except Exception:
        return []
    out = []
    for h in raw:
        try:
            out.append({"name": h.get("cyrmc", ""),
                        "amount": float(h.get("cyfe", 0)),
                        "ratio": float(h.get("zfeb", 0))})
        except (TypeError, ValueError):
            continue
    return out


def is_nt_holder(name):
    return any(k in name for k in NT_KEYWORDS)


def fetch_latest_holders_html(code):
    """持有人页面直接解析最新一期 (用于全市场重扫, 单请求)."""
    url = f"https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJCYR.php?symbol={code}"
    r = http_get(url, timeout=20)
    if r is None:
        return []
    r.encoding = "gbk"
    html = r.text
    pos = html.find("持有人名称")
    if pos < 0:
        return []
    chunk = html[pos:]
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", chunk, re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        texts = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip() for c in cells]
        if len(texts) >= 4 and texts[0].isdigit():
            out.append({"name": texts[1], "amount": texts[2], "ratio": texts[3]})
    return out


# ─────────────────────────── 标的池 ───────────────────────────
def load_universe():
    if not os.path.exists(UNIVERSE_PATH):
        return []
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_universe(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(UNIVERSE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def build_universe():
    """全市场扫描: 沪深全部 ETF 的十大持有人 -> 国家队持仓标的."""
    print("[重扫] 拉取沪深 ETF 列表 ...")
    sh = fetch_sse_etfs(now_bjt().strftime("%Y%m%d"))
    if not sh:
        # 当天可能未披露, 往前找最近一个交易日
        for back in range(1, 8):
            d = (now_bjt() - timedelta(days=back)).strftime("%Y%m%d")
            sh = fetch_sse_etfs(d)
            if sh:
                break
    date_str, sz = fetch_szse_snapshot()
    names = {code: name for code, (name, _) in sh.items()}
    for code, (name, _) in sz.items():
        names[code] = name
    print(f"[重扫] 沪 {len(sh)} 只, 深 {len(sz)} 只, 共 {len(names)} 只; 逐一检查持有人 ...")

    found = []
    for i, (code, name) in enumerate(sorted(names.items()), 1):
        holders = fetch_latest_holders_html(code)
        nt = [h["name"] for h in holders if is_nt_holder(h["name"])]
        if nt:
            found.append({"code": code, "name": name})
            print(f"  [{i}/{len(names)}] {code} {name} <- {nt[0]}")
        time.sleep(REQUEST_DELAY)
        if i % 50 == 0:
            print(f"  ... {i}/{len(names)}")
    print(f"[重扫] 完成, 国家队 ETF {len(found)} 只")
    save_universe(found)
    return found


# ─────────────────────────── CSV 读写 ───────────────────────────
SHARE_COLS = ["code", "date", "share_yi"]
PRICE_COLS = ["code", "date", "close"]


def read_csv(path, cols):
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(path, dtype={"code": str})
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols]


def write_csv(df, path, cols):
    df = df.drop_duplicates(["code", "date"], keep="last").sort_values(["date", "code"])
    df.to_csv(path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL)


def append_rows(df, rows, cols):
    if not rows:
        return df
    return pd.concat([df, pd.DataFrame(rows, columns=cols)], ignore_index=True)


# ─────────────────────────── 采集流程 ───────────────────────────
def collect_shares_init(universe):
    """从 HISTORY_START 起回溯全部历史份额 (上交所最早只到 2012-05)."""
    df = read_csv(SHARE_PATH, SHARE_COLS)
    codes = {u["code"] for u in universe if is_sh(u["code"])}
    today = now_bjt().date()
    start = datetime.strptime(HISTORY_START, "%Y-%m-%d").date()
    dates = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    # 只补 CSV 里缺的日期 (可中断续跑)
    have = set(df["date"]) if not df.empty else set()
    todo = [x for x in dates if f"{x[:4]}-{x[4:6]}-{x[6:]}" not in have]
    print(f"[份额] 回溯 {len(todo)} 个工作日 ({HISTORY_START} 起, 已存在 {len(dates) - len(todo)} 天)")
    rows, ok = [], 0
    with ThreadPoolExecutor(max_workers=SSE_WORKERS) as ex:
        for i, (ds, shares) in enumerate(zip(todo, ex.map(fetch_sse_shares, todo)), 1):
            if shares:
                ok += 1
                d = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
                for code in codes:
                    if code in shares:
                        rows.append((code, d, round(shares[code] / 1e8, 4)))
            if i % 100 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} 天, 成功 {ok}")
            if i % 500 == 0:      # 中途落盘, 中断后可续跑
                write_csv(append_rows(df, rows, SHARE_COLS), SHARE_PATH, SHARE_COLS)
    write_csv(append_rows(df, rows, SHARE_COLS), SHARE_PATH, SHARE_COLS)
    print(f"[份额] 写入 {len(rows)} 条")


def collect_shares_daily(universe):
    df = read_csv(SHARE_PATH, SHARE_COLS)
    codes = {u["code"] for u in universe if is_sh(u["code"])}
    last = df["date"].max() if not df.empty else None
    today = now_bjt().date()
    start = (pd.to_datetime(last).date() - timedelta(days=2)) if last else (today - timedelta(days=10))
    rows = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            ds = d.strftime("%Y%m%d")
            shares = fetch_sse_shares(ds)
            if shares:
                for code in codes:
                    if code in shares:
                        rows.append((code, d.strftime("%Y-%m-%d"), round(shares[code] / 1e8, 4)))
                print(f"  [份额] {ds} 写入 {sum(1 for r in rows if r[1] == d.strftime('%Y-%m-%d'))} 只")
            time.sleep(REQUEST_DELAY)
        d += timedelta(days=1)
    if rows:
        write_csv(append_rows(df, rows, SHARE_COLS), SHARE_PATH, SHARE_COLS)
    else:
        print("[份额] 无新增 (接口可能尚未披露当日数据)")


def collect_szse_today(universe):
    """深市只有当日快照: 从部署日起逐日累积."""
    date_str, snap = fetch_szse_snapshot()
    if not snap:
        print("[深市] 快照获取失败")
        return
    # 盘后(15:05)且为工作日才落库, 避免写入不完整数据
    now = now_bjt()
    if now.hour < 15 or (now.hour == 15 and now.minute < 5):
        print(f"[深市] 当前 {now:%H:%M} 未收盘, 跳过")
        return
    d = date_str if re.match(r"\d{4}-\d{2}-\d{2}", str(date_str or "")) else now.strftime("%Y-%m-%d")
    df = read_csv(SHARE_PATH, SHARE_COLS)
    rows = []
    for u in universe:
        code = u["code"]
        if is_sh(code):
            continue
        item = snap.get(code)
        if item and item[1] > 0:
            rows.append((code, d, round(item[1] / 1e8, 4)))
    if rows:
        write_csv(append_rows(df, rows, SHARE_COLS), SHARE_PATH, SHARE_COLS)
        print(f"[深市] {d} 写入 {len(rows)} 只")


def collect_prices(universe, datalen=None):
    df = read_csv(PRICE_PATH, PRICE_COLS)
    last = df["date"].max() if not df.empty else None
    gap = (now_bjt().date() - pd.to_datetime(last).date()).days if last else LOOKBACK_DAYS
    n = datalen or max(gap + 10, 15)
    print(f"[价格] 每只取最近 {n} 条日K (CSV 最新 {last})")
    rows = []
    for i, u in enumerate(universe, 1):
        code = u["code"]
        for d, close in fetch_prices(code, n):
            # 15:05 前丢弃当日未完成数据
            now = now_bjt()
            if d == now.strftime("%Y-%m-%d") and now.hour * 60 + now.minute < 15 * 60 + 5:
                continue
            rows.append((code, d, round(close, 3)))
        if i % 10 == 0:
            print(f"  {i}/{len(universe)}")
        time.sleep(REQUEST_DELAY)
    write_csv(append_rows(df, rows, PRICE_COLS), PRICE_PATH, PRICE_COLS)
    print(f"[价格] 写入 {len(rows)} 条")


def collect_holders(universe):
    """保留全部报告期历史, 只补抓缺失的期次."""
    data = {}
    if os.path.exists(HOLDER_PATH):
        try:
            with open(HOLDER_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    print(f"[持有人] 检查 {len(universe)} 只 ETF 的全部报告期")

    def refresh(u):
        code = u["code"]
        dates = fetch_report_dates(code)
        if not dates:
            return code, None
        periods = [p for p in data.get(code, []) if p.get("holders")]
        have = {p["report_date"] for p in periods}
        new = 0
        for d in dates:
            if d in have:
                continue
            holders = fetch_holders(code, d)
            if holders:
                periods.append({"report_date": d, "holders": holders})
                new += 1
            time.sleep(REQUEST_DELAY)
        periods.sort(key=lambda p: p["report_date"], reverse=True)
        return code, (periods, new)

    results = []
    with ThreadPoolExecutor(max_workers=HOLDER_WORKERS) as ex:
        for i, (code, item) in enumerate(ex.map(refresh, universe), 1):
            if item:
                results.append((code, item[0]))
                if item[1]:
                    print(f"  {code} 新增 {item[1]} 期")
            if i % 10 == 0:
                print(f"  {i}/{len(universe)}")
    for code, periods in results:
        data[code] = periods
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HOLDER_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[持有人] 保存 {len(data)} 只 ETF 的全部报告期")


# ─────────────────────────── 预计算 + 输出 JS ───────────────────────────
def chg_pct(latest, prev):
    if latest is None or prev in (None, 0):
        return None
    return round((latest - prev) / prev * 100, 2)


def build_data():
    universe = load_universe()
    if not universe:
        print("[错误] 标的池为空, 请先运行 --rescan")
        return 1
    share_df = read_csv(SHARE_PATH, SHARE_COLS)
    price_df = read_csv(PRICE_PATH, PRICE_COLS)
    holders = {}
    if os.path.exists(HOLDER_PATH):
        with open(HOLDER_PATH, encoding="utf-8") as f:
            holders = json.load(f)

    share_df["share_yi"] = pd.to_numeric(share_df["share_yi"], errors="coerce")
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    if share_df.empty:
        print("[错误] 无份额数据")
        return 1

    sp = share_df.pivot_table(index="date", columns="code", values="share_yi",
                              aggfunc="last").sort_index()
    pp = price_df.pivot_table(index="date", columns="code", values="close",
                              aggfunc="last").sort_index()
    share_dates = list(sp.index)
    price_dates = list(pp.index)

    # 总份额: 沪市口径 (上交所逐日披露, 历史连续); 深市自部署日起单独累积
    sh_cols = [c for c in sp.columns if is_sh(c)]
    total = sp[sh_cols].sum(axis=1, min_count=1)
    total_series = [None if pd.isna(v) else round(float(v), 2) for v in total]
    last_total_idx = max((i for i, v in enumerate(total_series) if v is not None), default=-1)
    latest_share_date = share_dates[last_total_idx] if last_total_idx >= 0 else None

    # 指数 ETF 价格 (归一化由前端完成); 必须对齐到 share_dates, 否则前端按份额日切片会错位
    index_prices = {}
    for code, name in INDEX_ETFS.items():
        s = (pp[code].reindex(share_dates) if code in pp.columns
             else pd.Series([None] * len(share_dates), index=share_dates))
        vals = [None if pd.isna(v) else round(float(v), 3) for v in s]
        index_prices[code] = {"name": name, "values": vals}

    def series(col, frame, dates, ndigits=4):
        if col not in frame.columns:
            return [None] * len(dates)
        return [None if pd.isna(v) else round(float(v), ndigits) for v in frame[col]]

    def trim_series(vals):
        """裁掉首尾空值, 只保留有效区间 (全历史下可省下大量 null 体积)."""
        idx = [i for i, v in enumerate(vals) if v is not None]
        if not idx:
            return {"start": 0, "values": []}
        return {"start": idx[0], "values": vals[idx[0]:idx[-1] + 1]}

    etfs = []
    for u in universe:
        code, name = u["code"], u["name"]
        shares = series(code, sp, share_dates, 2)
        prices = series(code, pp, price_dates, 3)
        # 最新有效份额
        latest_share, latest_date = None, None
        for d, v in zip(reversed(share_dates), reversed(shares)):
            if v is not None:
                latest_share, latest_date = v, d
                break
        chg = {}
        for label, days in [("1d", 1), ("1w", 7), ("1m", 30), ("3m", 90), ("1y", 365)]:
            chg[label] = None
            if latest_share is None:
                continue
            target = (pd.to_datetime(latest_date) - timedelta(days=days)).strftime("%Y-%m-%d")
            prev = None
            for d, v in zip(reversed(share_dates), reversed(shares)):
                if d <= target and v is not None:
                    prev = v
                    break
            chg[label] = chg_pct(latest_share, prev)

        periods = holders.get(code) or []
        latest_p = periods[0] if periods else None
        prev_p = periods[1] if len(periods) > 1 else None
        latest_h = latest_p["holders"] if latest_p else []
        prev_h = prev_p["holders"] if prev_p else []
        prev_map = {h["name"]: h for h in prev_h}

        def pack(h, with_flag=True):
            item = {"name": h["name"], "amount": h["amount"], "ratio": h["ratio"]}
            if with_flag:
                item["is_nt"] = is_nt_holder(h["name"])
            p = prev_map.get(h["name"])
            item["prev_amount"] = p["amount"] if p else None
            item["chg"] = chg_pct(h["amount"], p["amount"] if p else None)
            return item

        nt_latest = [h for h in latest_h if is_nt_holder(h["name"])]
        nt_prev = [h for h in prev_h if is_nt_holder(h["name"])]
        nt_ratio = round(sum(h["ratio"] for h in nt_latest), 2) if nt_latest else None
        nt_amount = sum(h["amount"] for h in nt_latest) if nt_latest else None
        prev_nt_amount = sum(h["amount"] for h in nt_prev) if nt_prev else None
        nt_chg = chg_pct(nt_amount, prev_nt_amount)
        prev_names = {h["name"] for h in prev_h}
        nt_is_new = any(h["name"] not in prev_names for h in nt_latest) if nt_latest else False

        etfs.append({
            "code": code, "name": name, "market": "sh" if is_sh(code) else "sz",
            "latest_share": latest_share, "share_date": latest_date,
            "chg": chg,
            "nt_hold_ratio": nt_ratio, "nt_hold_change": nt_chg, "nt_is_new": nt_is_new,
            "latest_report_date": latest_p["report_date"] if latest_p else None,
            "prev_report_date": prev_p["report_date"] if prev_p else None,
            "shares": trim_series(shares), "prices": trim_series(prices),
            "holders_latest": [pack(h) for h in latest_h],
            "holders_prev": [pack(h, False) for h in prev_h],
        })

    # ── 国家队持仓占比 (半年报/年报口径) ──
    # 分子: 十大持有人中国家队合计持有份额; 分母: 该报告期对应的 ETF 总份额
    nt_by_code = {}
    for u in universe:
        code = u["code"]
        for p in holders.get(code) or []:
            amt = sum(h["amount"] for h in p.get("holders", []) if is_nt_holder(h["name"]))
            if amt > 0:
                nt_by_code.setdefault(code, {})[p["report_date"]] = amt

    def shares_at(code, d):
        """报告期当日的 ETF 总份额 (取 <= 报告期最近一个披露日)."""
        if code not in sp.columns:
            return None
        col = sp[code]
        i = sp.index.searchsorted(d, side="right") - 1
        while i >= 0:
            v = col.iloc[i]
            if not pd.isna(v):
                return float(v)
            i -= 1
        return None

    # 只保留标准半年报/年报期次, 去掉基金成立首期等非标准日期, 避免合计占比出现假跳变
    report_dates = sorted(d for d in {d for m in nt_by_code.values() for d in m}
                          if d[5:] in ("06-30", "12-31"))
    nt_ratio = {"dates": report_dates, "total": [], "index": {c: [] for c in INDEX_ETFS}}
    for d in report_dates:
        num = den = 0.0
        for code in sp.columns:
            tot = shares_at(code, d)
            if tot is None:
                continue
            den += tot * 1e8
            num += nt_by_code.get(code, {}).get(d, 0.0)
        nt_ratio["total"].append(round(num / den * 100, 2) if den else None)
        for c in INDEX_ETFS:
            tot = shares_at(c, d)
            amt = nt_by_code.get(c, {}).get(d)
            nt_ratio["index"][c].append(
                round(amt / (tot * 1e8) * 100, 2) if (amt and tot) else None)

    # 去掉全空期次 (2012 年前无份额数据可算分母)
    keep = [i for i, t in enumerate(nt_ratio["total"])
            if t is not None or any(nt_ratio["index"][c][i] is not None for c in INDEX_ETFS)]
    nt_ratio = {
        "dates": [nt_ratio["dates"][i] for i in keep],
        "total": [nt_ratio["total"][i] for i in keep],
        "index": {c: [nt_ratio["index"][c][i] for i in keep] for c in INDEX_ETFS},
    }

    data = {
        "generated_at": now_bjt().strftime("%Y-%m-%d %H:%M"),
        "latest_share_date": latest_share_date,
        "latest_price_date": price_dates[-1] if price_dates else None,
        "share_dates": share_dates,
        "share_total": total_series,
        "price_dates": price_dates,
        "index_prices": index_prices,
        "nt_ratio": nt_ratio,
        "etfs": etfs,
        "notes": {
            "total": "总份额为沪市口径（上交所逐日披露）；深市ETF份额自部署日起累积，表中单独展示变化",
            "holders": "十大持有人仅在半年报/年报披露，最新占比最长可能滞后 6 个月",
        },
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("window.ETF_NT_DATA = " + json.dumps(data, ensure_ascii=False,
                                                     separators=(",", ":")) + ";\n")
    size = os.path.getsize(OUT_PATH) / 1024
    print(f"[输出] {OUT_PATH}  {size:.0f} KB, {len(etfs)} 只 ETF, "
          f"份额 {len(share_dates)} 天, 价格 {len(price_dates)} 天")
    return 0


# ─────────────────────────── 入口 ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="首次回溯历史份额/价格/持有人")
    ap.add_argument("--rescan", action="store_true", help="全市场重扫国家队标的池")
    ap.add_argument("--skip-holders", action="store_true", help="跳过持有人刷新")
    ap.add_argument("--holders", action="store_true", help="强制刷新十大持有人")
    ap.add_argument("--no-fetch", action="store_true", help="只用现有 CSV 重建 JS")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    universe = load_universe()

    if args.rescan:
        universe = build_universe()
    elif not universe:
        print("[提示] 标的池不存在, 先执行全市场重扫 ...")
        universe = build_universe()

    if not args.no_fetch:
        if args.init:
            collect_shares_init(universe)
            collect_prices(universe, datalen=PRICE_HISTORY_BARS)
            collect_holders(universe)
        else:
            collect_shares_daily(universe)
            collect_szse_today(universe)
            collect_prices(universe)
            # 持有人半年才更新一次, 30 天内不重复抓取
            stale = (not os.path.exists(HOLDER_PATH) or
                     time.time() - os.path.getmtime(HOLDER_PATH) > 30 * 86400)
            if args.holders or (stale and not args.skip_holders):
                collect_holders(universe)
            else:
                print("[持有人] 缓存未满 30 天, 跳过 (--holders 可强制刷新)")

    return build_data()


if __name__ == "__main__":
    sys.exit(main())
