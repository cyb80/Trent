# -*- coding: utf-8 -*-
"""V6 股指期货基差轮动 —— 回测与前端数据生成

策略（严格按《V6策略说明文档.md》）：
  每月最后一个交易日调仓；在 IH/IF/IC/IM **全部在挂月份合约**中跨品种统一排名，
  做多年化基差率最小（贴水最深，<= V6_LO）者、做空年化基差率最大（升水最高，>= V6_SH）者；
  不达阈值的腿空仓（当期收益记 0）；两腿各 100% 名义。

输入: data/v6_futures.csv            (date, contract, close)  ← fetch_v6_futures.py
      data/指数行情序列.xlsx          (000016/000300/000905/000852 现货收盘)
输出: data/v6_backtest.js            (回测全量结果, 供 v6_basis.html 渲染 + 阈值可调客户端重算)
      data/v6_live.js                (当前在挂合约/最新收盘/下个调仓日, 供盘中实时计算)
"""
import calendar
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
FUT_CSV = os.path.join(DATA_DIR, "v6_futures.csv")
XLSX = os.path.join(DATA_DIR, "指数行情序列.xlsx")
OUT_BT = os.path.join(DATA_DIR, "v6_backtest.js")
OUT_LIVE = os.path.join(DATA_DIR, "v6_live.js")

PROD2SPOT = {"IH": "000016", "IF": "000300", "IC": "000905", "IM": "000852"}
SPOT_NAME = {"IH": "上证50", "IF": "沪深300", "IC": "中证500", "IM": "中证1000"}
SPOT_SECID = {"IH": "1.000016", "IF": "1.000300", "IC": "1.000905", "IM": "1.000852"}
FACTOR, ANN = 365, 252
START = "2018-01-01"
V6_LO, V6_SH = -0.05, 0.01


# ---------------- 数据 ----------------
def load_spot():
    sheets = pd.read_excel(XLSX, index_col=0, sheet_name=None)
    out = {}
    for prod, key in PROD2SPOT.items():
        sn = [k for k in sheets if key in str(k)]
        if not sn:
            raise KeyError("xlsx 缺少 %s 现货 sheet" % key)
        d = sheets[sn[0]].copy()
        d.index = pd.to_datetime(d.index, errors="coerce")
        d = d[~d.index.isna()].sort_index()
        col = [c for c in d.columns if str(c).upper().strip() == "CLOSE"][0]
        s = pd.to_numeric(d[col], errors="coerce").dropna()
        out[prod] = s[~s.index.duplicated(keep="last")]
    return out


def load_futures():
    df = pd.read_csv(FUT_CSV, parse_dates=["date"])
    df["contract"] = df["contract"].astype(str).str.upper()
    df = df[df["contract"].str.match(r"^(IH|IF|IC|IM)\d{4}$")]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    return df[df["close"] > 0]


# ---------------- 到期日 / 调仓日 ----------------
def make_expiry(cal):
    def expiry_of(code):
        y, m = 2000 + int(code[2:4]), int(code[4:6])
        fridays = [w[calendar.FRIDAY] for w in calendar.monthcalendar(y, m) if w[calendar.FRIDAY]]
        base = pd.Timestamp(y, m, fridays[2])
        i = int(cal.searchsorted(base))
        return base if i >= len(cal) else cal[i]
    return expiry_of


def month_ends(cal, start):
    d = cal[cal >= pd.Timestamp(start)]
    return pd.DatetimeIndex(pd.Series(d, index=d).groupby([d.year, d.month]).max().values)


# ---------------- 回测主体 ----------------
def run_v6(panel_f, panel_s, cal):
    expiry_of = make_expiry(cal)
    expiry = {c: expiry_of(c) for c in panel_f.columns}
    rebal = month_ends(cal, START)

    recs, r_long, r_short = [], {}, {}
    for i in range(len(rebal) - 1):
        T, Tn = rebal[i], rebal[i + 1]
        cands = []
        for c in panel_f.columns:
            if pd.isna(panel_f.at[T, c]) or expiry[c] < Tn:
                continue
            dd = (expiry[c] - T).days
            if dd <= 0:
                continue
            b = (panel_f.at[T, c] / panel_s.at[T, c[:2]] - 1) * FACTOR / dd
            cands.append((c, b))
        if len(cands) < 2:
            continue
        L = min(cands, key=lambda x: x[1])
        S = max(cands, key=lambda x: x[1])

        idx = panel_f.index[(panel_f.index > T) & (panel_f.index <= Tn)]
        seg = {}
        for tag, c in (("L", L[0]), ("S", S[0])):
            col = pd.concat([panel_f.loc[[T], c], panel_f.loc[idx, c].ffill()])
            seg[tag] = col.pct_change().iloc[1:]
        r_long.update(seg["L"].to_dict())
        r_short.update(seg["S"].to_dict())

        open_l = bool(L[1] <= V6_LO)
        open_s = bool(S[1] >= V6_SH)
        rp = pd.Series(0.0, index=idx)
        if open_l:
            rp = rp + seg["L"].reindex(idx).fillna(0.0)
        if open_s:
            rp = rp - seg["S"].reindex(idx).fillna(0.0)
        recs.append(dict(T=T, Tn=Tn, long_c=L[0], long_b=L[1], short_c=S[0], short_b=S[1],
                         open_l=open_l, open_s=open_s, ret=float((1 + rp).prod() - 1)))
    return pd.DataFrame(recs), pd.Series(r_long).sort_index(), pd.Series(r_short).sort_index()


# ---------------- 指标 ----------------
def metrics(r, nav):
    yrs = (nav.index[-1] - nav.index[0]).days / 365
    me = nav.resample("ME").last().pct_change().dropna()
    rollmax = nav.cummax()
    dd = nav / rollmax - 1
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    return dict(
        nv=float(nav.iloc[-1]),
        years=round(float(yrs), 3),
        annual=float(nav.iloc[-1] ** (1 / yrs) - 1),
        vol=float(r.std() * np.sqrt(ANN)),
        sharpe=float(r.mean() / r.std() * np.sqrt(ANN)),
        mdd=float(dd.min()),
        mdd_peak=str(peak.date()), mdd_trough=str(trough.date()),
        # 加 1e-9 容差: 累乘浮点噪声会把"当月零收益"算成上涨(如 2021-09 的 2.2e-16)
        win_rate=float((me > 1e-9).mean()),
    )


def build(rec, r_long, r_short, lo=V6_LO, sh=V6_SH):
    """按给定阈值重建净值序列与全部指标。

    lo/sh 为 None 表示该腿**始终开仓**(V0 基准对照)。
    空头腿对组合的贡献为 -该合约收益率（做空）。
    """
    if lo is None:
        wl = pd.Series(True, index=rec.index)
    else:
        wl = pd.Series((rec["long_b"] <= lo).values, index=rec.index)
    if sh is None:
        ws = pd.Series(True, index=rec.index)
    else:
        ws = pd.Series((rec["short_b"] >= sh).values, index=rec.index)
    rl = pd.Series(0.0, index=r_long.index)
    rs = pd.Series(0.0, index=r_short.index)
    for i, row in rec.iterrows():
        idx = r_long.index[(r_long.index > row["T"]) & (r_long.index <= row["Tn"])]
        if wl[i]:
            rl.loc[idx] = r_long.reindex(idx).fillna(0.0)
        if ws[i]:
            rs.loc[idx] = r_short.reindex(idx).fillna(0.0)
    comb = rl - rs
    nav = pd.concat([pd.Series([1.0], index=[rec["T"].iloc[0]]), (1 + comb).cumprod()])
    return comb, nav, wl, ws


def leg_nav(rec, raw, mask, sign):
    """单腿净值序列（sign: 多头 +1 / 空头 -1），空仓期记 0。"""
    parts = []
    for i, row in rec.iterrows():
        idx = raw.index[(raw.index > row["T"]) & (raw.index <= row["Tn"])]
        seg = (raw.reindex(idx).fillna(0.0) * sign) if mask[i] else pd.Series(0.0, index=idx)
        parts.append(seg)
    r = pd.concat(parts)
    return pd.concat([pd.Series([1.0], index=[rec["T"].iloc[0]]), (1 + r).cumprod()])


def attribution(rec, r_long, r_short, wl, ws):
    rows = []
    for leg, raw, mask, sign in (("多头", r_long, wl, 1.0), ("空头", r_short, ws, -1.0)):
        sub = []
        for i, row in rec.iterrows():
            if not mask[i]:
                continue
            idx = raw.index[(raw.index > row["T"]) & (raw.index <= row["Tn"])]
            pr = float((1 + raw.reindex(idx).fillna(0.0) * sign).prod() - 1)
            prod = (row["long_c"] if leg == "多头" else row["short_c"])[:2]
            sub.append((prod, pr))
        if not sub:
            continue
        for prod in ["IH", "IF", "IC", "IM"]:
            v = [x for p, x in sub if p == prod]
            if not v:
                continue
            v = np.array(v)
            rows.append(dict(leg=leg, prod=prod, n=len(v),
                             win=float((v > 0).mean()),
                             avg=float(v.mean()),
                             cum=float(np.prod(1 + v) - 1)))
    return rows


def yearly(nav):
    y = nav.groupby(nav.index.year).last()
    y = pd.concat([pd.Series([1.0], index=[y.index[0] - 1]), y])
    ret = y.pct_change().dropna()
    return [dict(year=int(k), nav=float(y.loc[k]), ret=float(v)) for k, v in ret.items()]


# ---------------- 主流程 ----------------
def main():
    if not os.path.exists(FUT_CSV):
        print("[错误] 缺少 %s，请先运行 fetch_v6_futures.py" % FUT_CSV)
        return 1

    spot = load_spot()
    fut = load_futures()
    panel_f = fut.pivot_table(index="date", columns="contract", values="close").sort_index()
    panel_s = pd.DataFrame({p: s for p, s in spot.items()})
    cal_all = pd.DatetimeIndex(sorted(set(panel_s.dropna(how="all").index)))
    cal = cal_all[(cal_all >= panel_f.index.min()) & (cal_all <= panel_f.index.max())]
    panel_f = panel_f.reindex(cal)
    panel_s = panel_s.reindex(cal).ffill()
    print("期货: %s ~ %s  %d 交易日  %d 合约" % (panel_f.index.min().date(), panel_f.index.max().date(),
                                                len(panel_f), panel_f.shape[1]))
    print("现货: %s ~ %s" % (panel_s.index.min().date(), panel_s.index.max().date()))

    rec, r_long, r_short = run_v6(panel_f, panel_s, cal)
    comb, nav, wl, ws = build(rec, r_long, r_short)
    m = metrics(comb, nav)

    print("\n" + "=" * 88)
    print("V6 基差轮动回测（阈值 %+.0f%% / %+.0f%%）" % (V6_LO * 100, V6_SH * 100))
    print("=" * 88)
    print("  持仓期数: %d   区间: %s ~ %s" % (len(rec), comb.index.min().date(), comb.index.max().date()))
    print("  累计净值: %.4f   年化: %.2f%%   年化波动: %.2f%%   夏普: %.2f   最大回撤: %.2f%% (%s~%s)   月度胜率: %.1f%%"
          % (m["nv"], m["annual"] * 100, m["vol"] * 100, m["sharpe"], m["mdd"] * 100,
             m["mdd_peak"], m["mdd_trough"], m["win_rate"] * 100))
    st = dict(only_long=int(((wl) & (~ws)).sum()), only_short=int(((~wl) & (ws)).sum()),
              both=int((wl & ws).sum()), none=int(((~wl) & (~ws)).sum()))
    print("  开仓结构: 仅多头 %d / 仅空头 %d / 双腿 %d / 全空仓 %d" % (
        st["only_long"], st["only_short"], st["both"], st["none"]))
    ln = leg_nav(rec, r_long, wl, 1.0)
    sn = leg_nav(rec, r_short, ws, -1.0)
    print("  多头腿净值: %.3f   空头腿净值: %.3f" % (ln.iloc[-1], sn.iloc[-1]))
    att = attribution(rec, r_long, r_short, wl, ws)
    print("\n  品种归因:")
    for a in att:
        print("    %s %-2s 次数=%-3d 单期胜率=%.3f 平均单期=%+.3f%% 累计=%+.2f%%"
              % (a["leg"], a["prod"], a["n"], a["win"], a["avg"] * 100, a["cum"] * 100))
    yr = yearly(nav)
    print("\n  分年度: %s" % "  ".join("%d %+.2f%%" % (y["year"], y["ret"] * 100) for y in yr))

    # V0 对照（两腿始终开仓）
    comb0, nav0, _, _ = build(rec, r_long, r_short, lo=None, sh=None)
    m0 = metrics(comb0, nav0)
    print("  [V0 对照·始终开仓] 累计 %.3f 年化 %.2f%% 夏普 %.2f 回撤 %.2f%%"
          % (m0["nv"], m0["annual"] * 100, m0["sharpe"], m0["mdd"] * 100))

    # ---- 输出 v6_backtest.js ----
    dates = [d.strftime("%Y-%m-%d") for d in comb.index]
    pos = {d: i for i, d in enumerate(comb.index)}
    periods = []
    for _, row in rec.iterrows():
        i0 = int(comb.index.searchsorted(row["T"], side="right"))
        i1 = int(comb.index.searchsorted(row["Tn"], side="right"))
        periods.append(dict(T=str(row["T"].date()), Tn=str(row["Tn"].date()), i0=i0, i1=i1,
                            long_c=row["long_c"], long_b=round(float(row["long_b"]) * 100, 4),
                            short_c=row["short_c"], short_b=round(float(row["short_b"]) * 100, 4)))
    data = dict(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        eval_start=dates[0], eval_end=dates[-1], lo=V6_LO * 100, sh=V6_SH * 100,
        factor=FACTOR, ann=ANN,
        dates=dates,
        r_long=[round(float(v), 8) for v in r_long.reindex(comb.index).fillna(0.0).values],
        r_short=[round(float(v), 8) for v in r_short.reindex(comb.index).fillna(0.0).values],
        periods=periods,
        kpis={"nv": round(m["nv"], 4), "annual": round(m["annual"] * 100, 2),
              "vol": round(m["vol"] * 100, 2), "sharpe": round(m["sharpe"], 2),
              "mdd": round(m["mdd"] * 100, 2), "win_rate": round(m["win_rate"] * 100, 1),
              "years": m["years"], "n_period": len(rec),
              "mdd_peak": m["mdd_peak"], "mdd_trough": m["mdd_trough"]},
        yearly=yr, attribution=att, structure=st,
        v0={"nv": round(m0["nv"], 4), "annual": round(m0["annual"] * 100, 2),
            "vol": round(m0["vol"] * 100, 2), "sharpe": round(m0["sharpe"], 2),
            "mdd": round(m0["mdd"] * 100, 2), "win_rate": round(m0["win_rate"] * 100, 1)},
    )
    with open(OUT_BT, "w", encoding="utf-8") as f:
        f.write("window.V6_BACKTEST = " + json.dumps(data, ensure_ascii=False) + ";\n")
    print("\nsaved:", OUT_BT)

    # ---- 输出 v6_live.js ----
    last_date = panel_f.index.max()
    exp_of = make_expiry(cal)
    recent = panel_f.loc[panel_f.index[-20:]]
    live_contracts = []
    for c in sorted(recent.columns):
        s = recent[c].dropna()
        if s.empty:
            continue
        e = exp_of(c)
        if e < last_date:      # 已到期合约不再列入在挂清单
            continue
        live_contracts.append(dict(c=c, prod=c[:2], expiry=str(e.date()), close=float(s.iloc[-1])))
    # 月度末交易日：数据末端**之前**的月份由现货日历推导；
    # 数据末端所在月及以后必须用真实交易日历（或工作日）推算，
    # 否则会把"数据截断日"误当成月末换月日。
    prev_end = last_date.replace(day=1) - pd.Timedelta(days=1)
    me_hist = month_ends(cal, "2025-01-01")
    month_ends_map = {d.strftime("%Y-%m"): str(d.date()) for d in me_hist if d <= prev_end}
    try:
        import akshare as ak
        tc = pd.to_datetime(ak.tool_trade_date_hist_sina()["trade_date"]).sort_values()
        fut = tc[tc > prev_end]
        for k, v in fut.groupby([fut.dt.year, fut.dt.month]).max().items():
            month_ends_map["%04d-%02d" % k] = str(pd.Timestamp(v).date())
    except Exception as e:
        print("  [提示] 交易日历获取失败(%s)，未来月末用工作日估算" % str(e)[:60])
    # 交易日历未覆盖的月份：用最后一个工作日估算
    for k in range(7):
        yy, mm = last_date.year + (last_date.month - 1 + k) // 12, (last_date.month - 1 + k) % 12 + 1
        key = "%04d-%02d" % (yy, mm)
        if key not in month_ends_map:
            d = pd.Timestamp(yy, mm, calendar.monthrange(yy, mm)[1])
            while d.weekday() >= 5:
                d -= pd.Timedelta(days=1)
            month_ends_map[key] = str(d.date())

    # 当前持仓期取最后一期"有收益产生"的区间（跳过 T==Tn 的退化期）
    valid = rec[rec["Tn"] > rec["T"]]
    last_row = valid.iloc[-1] if len(valid) else rec.iloc[-1]
    # 当前持仓期浮盈：自调仓日 T 收盘起，至数据文件最新交易日收盘的累计倍数
    T = last_row["T"]
    Tn_disp = last_row["Tn"]
    if Tn_disp == last_date:      # 数据末端落在月中 -> 换月日应取真实的月末交易日
        nxt = (T.replace(day=1) + pd.DateOffset(months=1)).strftime("%Y-%m")
        Tn_disp = pd.Timestamp(month_ends_map.get(nxt, str(Tn_disp.date())))
    seg_idx = pd.DatetimeIndex([T]).append(panel_f.index[panel_f.index > T])
    seg = pd.DataFrame({c: panel_f.reindex(seg_idx)[c].ffill()
                        for c in (last_row["long_c"], last_row["short_c"])}, index=seg_idx)
    ret = seg.pct_change().fillna(0.0)
    rl_seg = ret[last_row["long_c"]]
    rs_seg = ret[last_row["short_c"]]
    contrib = (rl_seg if last_row["open_l"] else 0.0) - (rs_seg if last_row["open_s"] else 0.0)
    nav_long = float((1 + rl_seg).prod())
    nav_short = float((1 - rs_seg).prod())
    live = dict(
        generated_at=data["generated_at"], trade_date=str(last_date.date()),
        lo=V6_LO * 100, sh=V6_SH * 100, factor=FACTOR, ann=ANN,
        spot={p: dict(name=SPOT_NAME[p], code=PROD2SPOT[p], secid=SPOT_SECID[p],
                      close=float(panel_s[p].loc[last_date])) for p in PROD2SPOT},
        contracts=live_contracts,
        month_ends={k: month_ends_map[k] for k in sorted(month_ends_map)},
        last_rebalance=dict(T=str(T.date()), Tn=str(Tn_disp.date()),
                            long_c=last_row["long_c"], long_b=round(float(last_row["long_b"]) * 100, 4),
                            short_c=last_row["short_c"], short_b=round(float(last_row["short_b"]) * 100, 4),
                            open_l=bool(last_row["open_l"]), open_s=bool(last_row["open_s"]),
                            long_close0=float(seg[last_row["long_c"]].iloc[0]),
                            short_close0=float(seg[last_row["short_c"]].iloc[0]),
                            long_close=float(seg[last_row["long_c"]].dropna().iloc[-1]),
                            short_close=float(seg[last_row["short_c"]].dropna().iloc[-1]),
                            nav_long=round(nav_long, 6), nav_short=round(nav_short, 6),
                            nav_comb=round(float((1 + contrib).prod()), 6),
                            n_days=int(len(seg) - 1)),
    )
    with open(OUT_LIVE, "w", encoding="utf-8") as f:
        f.write("window.V6_LIVE = " + json.dumps(live, ensure_ascii=False) + ";\n")
    print("saved:", OUT_LIVE)
    print("  最新交易日 %s 在挂合约 %d 个" % (last_date.date(), len(live_contracts)))
    print("  最近一期调仓 %s -> %s  多%s(%.2f%%) 空%s(%.2f%%)  开仓: 多=%s 空=%s"
          % (live["last_rebalance"]["T"], live["last_rebalance"]["Tn"],
             last_row["long_c"], last_row["long_b"] * 100,
             last_row["short_c"], last_row["short_b"] * 100,
             live["last_rebalance"]["open_l"], live["last_rebalance"]["open_s"]))

    # ---- 与文档金标准对比 ----
    print("\n" + "=" * 88)
    print("与《V6策略说明文档.md》第 8 节金标准对比")
    print("=" * 88)
    gold = {"累计净值": (5.187, m["nv"]), "年化收益%": (20.96, m["annual"] * 100),
            "年化波动%": (23.88, m["vol"] * 100), "夏普": (0.95, m["sharpe"]),
            "最大回撤%": (-35.26, m["mdd"] * 100), "月度胜率%": (66.3, m["win_rate"] * 100)}
    print("  %-12s %10s %10s %10s" % ("指标", "文档", "本实现", "偏差"))
    for k, (g, o) in gold.items():
        dev = (o - g) / abs(g) * 100 if g else float("nan")
        print("  %-12s %10.2f %10.2f %9.1f%%" % (k, g, o, dev))
    print("  开仓结构  文档 仅多70/仅空16/双腿16/全空2   本实现 %d/%d/%d/%d"
          % (st["only_long"], st["only_short"], st["both"], st["none"]))
    ref = nav.loc[: "2026-09-23"]
    if len(ref):
        print("  注: 文档金标准数据末端为 2026-09-23; 本实现截至该日累计净值 %.4f（对文档 5.187 的偏差 %.2f%%），"
              % (ref.iloc[-1], (ref.iloc[-1] - 5.187) / 5.187 * 100))
        print("      其后的差异全部来自 2026-09-23 之后的新增行情。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
