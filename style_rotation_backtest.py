# -*- coding: utf-8 -*-
"""
大小盘风格轮动策略与回测

框架（参考兴业/华泰金工/国联民生/国泰海通的大小盘轮动研究）
----------------------------------------------------------
三维打分，每个维度 ∈ [-1, 1]，正 = 偏向小盘：
  1) 趋势/相对动量 (权重 0.40)
     ratio = ln(小盘/大盘)；60日相对动量的 z-score(tanh 压缩)
     小盘相对走强 → 正分
  2) 估值 (权重 0.30)
     val = PB_小盘 / PB_大盘；其滚动分位
     小盘估值处于相对高位(贵) → 负分（偏大盘，均值回归）
  3) 拥挤度 (权重 0.30)
     crowd = 小盘成交量 / (小盘+大盘成交量)；其滚动分位
     小盘成交过度拥挤 → 负分（偏大盘）

仓位：w_small = clip(0.5 + 0.5*综合分, 0, 1)，始终满仓，只在小盘/大盘间切换。
调仓：带"无交易带"(BAND)，目标偏离当前超过阈值才动，降换手。
防前视：所有滚动统计只用历史；信号滞后1个交易日执行。

标的：小盘=中证1000(000852.SH)，大盘=沪深300(000300.SH)
基准：50/50月度再平衡、纯大盘、纯小盘
输出：控制台报告 + data/style_rotation_result.csv
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
IDX = os.path.join(HERE, "data", "指数行情序列.xlsx")
OUT_CSV = os.path.join(HERE, "data", "style_rotation_result.csv")

SMALL, BIG = "000852.SH", "000300.SH"
COST = 0.0002
ANNUAL = 242
LAG = 1
BAND = 0.05

W_TREND, W_VAL, W_CROWD = 0.40, 0.30, 0.30
MOM_WIN = 60          # 相对动量窗口
Z_WIN = 500           # 标准化窗口
VAL_WIN = 750         # 估值分位窗口(约3年)
CROWD_WIN = 250       # 拥挤度分位窗口(约1年)
TH = 0.25             # 离散仓位阈值


def rollrank(s, win, minp=None):
    minp = minp or max(20, win // 4)
    return s.rolling(win, min_periods=minp).apply(lambda w: (w <= w[-1]).mean(), raw=True)


def load():
    d = pd.read_excel(IDX, index_col=0, sheet_name=None)
    out = {}
    for k in [SMALL, BIG]:
        v = d[k].copy()
        v.index = pd.to_datetime(v.index, errors="coerce")
        v = v[~v.index.isna()].sort_index()
        df = pd.DataFrame({
            "close": pd.to_numeric(v["CLOSE"], errors="coerce"),
            "vol": pd.to_numeric(v["VOLUME"], errors="coerce"),
            "pb": pd.to_numeric(v["PB_MRQ"], errors="coerce"),
        })
        out[k] = df
    return out


def perf(ret, w=None, name=""):
    ret = ret.fillna(0.0)
    nav = (1 + ret).cumprod()
    yrs = len(ret) / ANNUAL
    ann = nav.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    vol = ret.std() * np.sqrt(ANNUAL)
    mdd = (nav / nav.cummax() - 1).min()
    d = dict(名称=name, 年化=ann, 累计=nav.iloc[-1] - 1, 最大回撤=mdd, 年化波动=vol,
             Sharpe=(ret.mean() * ANNUAL / vol) if vol > 0 else np.nan,
             Calmar=ann / abs(mdd) if mdd < 0 else np.nan, 期数=len(ret))
    if w is not None:
        d["平均小盘仓位"] = w.mean()
        d["年换手"] = w.diff().abs().sum() / yrs
    return d, nav


def show(rows, title):
    print("\n" + "=" * 106)
    print(title)
    print("=" * 106)
    df = pd.DataFrame(rows)
    fmt = {"年化": "{:.2%}", "累计": "{:.2%}", "最大回撤": "{:.2%}", "年化波动": "{:.2%}",
           "Sharpe": "{:.2f}", "Calmar": "{:.2f}", "平均小盘仓位": "{:.2f}", "年换手": "{:.2f}"}
    o = df.copy()
    for c, f in fmt.items():
        if c in o.columns:
            o[c] = o[c].map(lambda v: f.format(v) if pd.notna(v) else "-")
    print(o.to_string(index=False))
    return df


def apply_band(target, band):
    """无交易带：目标偏离当前仓位超过 band 才调仓。"""
    out = np.empty(len(target))
    cur = 0.5
    for i, t in enumerate(np.asarray(target, dtype=float)):
        if np.isnan(t):
            out[i] = cur
            continue
        if abs(t - cur) > band:
            cur = t
        out[i] = cur
    return pd.Series(out, index=target.index)


def run(idxs, start=None, use_val=True, w=(W_TREND, W_VAL, W_CROWD), band=BAND, lag=LAG):
    small, big = idxs[SMALL], idxs[BIG]
    cal = small.index.intersection(big.index)
    sc, bc = small.loc[cal, "close"], big.loc[cal, "close"]

    # 1) 趋势
    ratio = np.log(sc / bc)
    mom = ratio.pct_change(MOM_WIN)
    z = (mom - mom.rolling(Z_WIN).mean()) / mom.rolling(Z_WIN).std()
    trend = np.tanh(z / 1.5).clip(-1, 1)

    # 2) 估值
    pb_r = small.loc[cal, "pb"] / big.loc[cal, "pb"]
    val = (0.5 - rollrank(pb_r, VAL_WIN)) * 2

    # 3) 拥挤度
    vs, vb = small.loc[cal, "vol"], big.loc[cal, "vol"]
    crowd_r = vs / (vs + vb)
    crowd = (0.5 - rollrank(crowd_r, CROWD_WIN)) * 2

    parts, ws = [], []
    parts.append(trend); ws.append(w[0])
    if use_val:
        parts.append(val); ws.append(w[1])
    parts.append(crowd); ws.append(w[2])
    W = np.array(ws, dtype=float)
    if W.sum() <= 0:
        raise ValueError("权重全为0")
    W = W / W.sum()

    M = pd.concat(parts, axis=1)
    avail = M.notna().all(axis=1)                 # 要求所选因子同时有值
    comp = (M * W).sum(axis=1).where(avail)

    if start:
        comp = comp.loc[start:]
    comp = comp.dropna()

    w_target = (0.5 + 0.5 * comp).clip(0, 1)
    w_eff = apply_band(w_target, band)
    w_exec = w_eff.shift(lag)

    rs = sc.pct_change().reindex(comp.index)
    rb = bc.pct_change().reindex(comp.index)
    turn = w_exec.diff().abs()
    net = (w_exec * rs + (1 - w_exec) * rb - turn * COST)

    return dict(comp=comp, trend=trend.reindex(comp.index), val=val.reindex(comp.index),
                crowd=crowd.reindex(comp.index), w_target=w_target, w_eff=w_eff,
                w_exec=w_exec, net=net, rs=rs, rb=rb, cal=comp.index)


def main():
    idxs = load()
    print("小盘=%s  大盘=%s" % (SMALL, BIG))
    print("PB 有效区间: 小盘 %s~%s | 大盘 %s~%s" % (
        str(idxs[SMALL]["pb"].first_valid_index())[:10], str(idxs[SMALL]["pb"].last_valid_index())[:10],
        str(idxs[BIG]["pb"].first_valid_index())[:10], str(idxs[BIG]["pb"].last_valid_index())[:10]))

    # ---- 主策略：三维（估值可用期）----
    R = run(idxs, use_val=True)
    R["comp"] = R["comp"].dropna()
    start = R["comp"].index.min()
    print("三维(趋势+估值+拥挤)回测起点: %s  末日: %s" % (str(start)[:10], str(R["comp"].index[-1])[:10]))

    rows, navs = [], {}

    def add(net, w, nm, key=None):
        d, nav = perf(net, w, nm)
        rows.append(d)
        navs[key or nm] = nav

    # 基准：50/50 月度再平衡
    idx = R["cal"]
    mth = pd.Series(idx.to_period("M"), index=idx)
    first_of_month = ~mth.duplicated()
    w50 = pd.Series(np.where(first_of_month, 0.5, np.nan), index=idx).ffill().shift(1).fillna(0.5)
    add(w50 * R["rs"] + (1 - w50) * R["rb"], w50, "50/50月度再平衡(基准)")
    add(R["rs"], pd.Series(1.0, index=idx), "纯小盘(中证1000)")
    add(R["rb"], pd.Series(0.0, index=idx), "纯大盘(沪深300)")

    # 策略
    add(R["net"], R["w_exec"], "风格轮动-连续")
    w_disc = apply_band(pd.Series(np.where(R["comp"] >= TH, 1.0, np.where(R["comp"] <= -TH, 0.0, 0.5)),
                                  index=idx), BAND).shift(LAG)
    add(w_disc * R["rs"] + (1 - w_disc) * R["rb"] - w_disc.diff().abs() * COST, w_disc, "风格轮动-离散(1/0/0.5)")

    # 单因子归因（分别只用 趋势 / 估值 / 拥挤）
    for nm, use_v, ww in [("仅趋势", False, (1, 0, 0)),
                          ("趋势+拥挤", False, (W_TREND, 0, W_CROWD)),
                          ("仅估值(PB)", False, (0, 1, 0)),
                          ("仅拥挤度", False, (0, 0, 1))]:
        if nm == "仅趋势":
            Rr = run(idxs, use_val=False, w=(1, 0, 0))
        elif nm == "趋势+拥挤":
            Rr = run(idxs, use_val=False, w=(W_TREND, 0, W_CROWD))
        elif nm == "仅估值(PB)":
            Rr = run(idxs, use_val=True, w=(0, 1, 0))
        else:
            Rr = run(idxs, use_val=True, w=(0, 0, 1))
        c = Rr["comp"].dropna().index
        add(Rr["net"].reindex(idx), Rr["w_exec"].reindex(idx), nm)

    show(rows, "大小盘风格轮动回测（%s ~ %s，成本2bp，无交易带%.0f%%，滞后%d日）"
         % (str(start)[:10], str(idx[-1])[:10], BAND * 100, LAG))

    # ---- 长样本：二维(趋势+拥挤)，2007起 ----
    R2 = run(idxs, use_val=False, w=(W_TREND, 0, W_CROWD))
    c2 = R2["comp"].dropna()
    c2 = c2.loc["2010-01-01":]
    idx2 = c2.index
    rows2 = []
    rs2, rb2 = R2["rs"].reindex(idx2), R2["rb"].reindex(idx2)
    mth2 = pd.Series(idx2.to_period("M"), index=idx2)
    fom2 = ~mth2.duplicated()
    w50b = pd.Series(np.where(fom2, 0.5, np.nan), index=idx2).ffill().shift(1).fillna(0.5)
    d, _ = perf(w50b * rs2 + (1 - w50b) * rb2, w50b, "50/50月度再平衡(基准)"); rows2.append(d)
    d, _ = perf(rs2, pd.Series(1.0, index=idx2), "纯小盘(中证1000)"); rows2.append(d)
    d, _ = perf(rb2, pd.Series(0.0, index=idx2), "纯大盘(沪深300)"); rows2.append(d)
    d, _ = perf(R2["net"].reindex(idx2), R2["w_exec"].reindex(idx2), "二维轮动(趋势+拥挤)"); rows2.append(d)
    show(rows2, "长样本对照：二维(趋势+拥挤) %s ~ %s" % (str(idx2[0])[:10], str(idx2[-1])[:10]))

    # ---- 稳健性：权重 / 无交易带 / 滞后 ----
    rb3 = []
    for ww, nm in [((0.6, 0.2, 0.2), "趋势0.6/估值0.2/拥挤0.2"),
                   ((0.4, 0.3, 0.3), "趋势0.4/估值0.3/拥挤0.3"),
                   ((0.2, 0.4, 0.4), "趋势0.2/估值0.4/拥挤0.4"),
                   ((0.34, 0.33, 0.33), "等权")]:
        Rr = run(idxs, use_val=True, w=ww)
        d, _ = perf(Rr["net"].reindex(idx), Rr["w_exec"].reindex(idx), nm)
        rb3.append(d)
    show(rb3, "权重稳健性")

    rb4 = []
    for b in [0.0, 0.02, 0.05, 0.10, 0.20]:
        Rr = run(idxs, use_val=True, w=(W_TREND, W_VAL, W_CROWD), band=b)
        d, _ = perf(Rr["net"].reindex(idx), Rr["w_exec"].reindex(idx), "无交易带%.0f%%" % (b * 100))
        rb4.append(d)
    for lg in [0, 1, 2, 5]:
        Rr = run(idxs, use_val=True, w=(W_TREND, W_VAL, W_CROWD), lag=lg)
        d, _ = perf(Rr["net"].reindex(idx), Rr["w_exec"].reindex(idx), "滞后%d日" % lg)
        rb4.append(d)
    show(rb4, "无交易带 / 执行滞后 稳健性")

    # ---- 分段 ----
    rb5 = []
    for nm, a, b in [("2015-2018", "2015-01-01", "2018-12-31"),
                     ("2019-2021", "2019-01-01", "2021-12-31"),
                     ("2022-2024", "2022-01-01", "2024-12-31"),
                     ("2025-至今", "2025-01-01", "2099-12-31")]:
        sub = idx[(idx >= a) & (idx <= b)]
        if len(sub) < 100:
            continue
        rs_, rb_ = R["rs"].reindex(sub), R["rb"].reindex(sub)
        w50c = pd.Series(np.where(~pd.Series(sub.to_period("M"), index=sub).duplicated(), 0.5, np.nan),
                         index=sub).ffill().shift(1).fillna(0.5)
        d, _ = perf(R["net"].reindex(sub), R["w_exec"].reindex(sub), "轮动 " + nm); rb5.append(d)
        d, _ = perf(w50c * rs_ + (1 - w50c) * rb_, w50c, "50/50 " + nm); rb5.append(d)
    show(rb5, "分段表现")

    # ---- 信号预测力诊断：IC（因子 vs 未来小盘-大盘相对收益）----
    # 注意：重叠的滚动窗口会严重虚增显著性，故 t 值用"非重叠取样"计算。
    print("\n" + "=" * 106)
    print("信号预测力诊断：因子与未来相对收益(小盘-大盘)的秩相关 IC")
    print("=" * 106)
    spread = (R["rs"] - R["rb"]).dropna()
    diag = []
    for nm, sig in [("综合", R["comp"]), ("趋势", R["trend"]), ("估值(PB)", R["val"]), ("拥挤度", R["crowd"])]:
        s = sig.reindex(spread.index)
        row = {"因子": nm}
        for h in [5, 20, 60]:
            fwd = spread.rolling(h).sum().shift(-h)
            row["IC_%d日" % h] = s.rank().corr(fwd.rank())
        # 20日IC的非重叠t值
        h = 20
        fwd = spread.rolling(h).sum().shift(-h)
        sl = slice(None, None, h)
        a, b = s.iloc[sl], fwd.iloc[sl]
        ok = a.notna() & b.notna()
        icn = a[ok].rank().corr(b[ok].rank())
        row["IC20非重叠t"] = icn * np.sqrt(int(ok.sum())) if ok.sum() > 2 else np.nan
        diag.append(row)
    o = pd.DataFrame(diag)
    for c in ["IC_5日", "IC_20日", "IC_60日"]:
        o[c] = o[c].map(lambda v: "%+.3f" % v if pd.notna(v) else "-")
    o["IC20非重叠t"] = o["IC20非重叠t"].map(lambda v: "%+.2f" % v if pd.notna(v) else "-")
    print(o.to_string(index=False))
    print("  解读：|IC|<0.05 或 t 绝对值<2 → 该因子对未来大小盘相对收益无可靠预测力。")

    # ---- 最新信号 ----
    last = R["cal"][-1]
    print("\n" + "=" * 106)
    print("最新风格信号（%s）" % str(last)[:10])
    print("=" * 106)
    print("   趋势分=%+.2f   估值分=%+.2f   拥挤度分=%+.2f  =>  综合=%+.2f"
          % (R["trend"].loc[last], R["val"].loc[last], R["crowd"].loc[last], R["comp"].loc[last]))
    print("   目标小盘仓位=%.2f  当前执行仓位=%.2f  %s"
          % (R["w_target"].loc[last], R["w_exec"].loc[last],
             "偏小盘" if R["comp"].loc[last] > 0.1 else ("偏大盘" if R["comp"].loc[last] < -0.1 else "均衡")))
    print("   注: PB 数据末次更新于 %s，最新估值分含前值延续" % str(idxs[SMALL]["pb"].last_valid_index())[:10])

    out = pd.DataFrame({"综合": R["comp"], "趋势分": R["trend"], "估值分": R["val"], "拥挤度分": R["crowd"],
                        "目标小盘仓位": R["w_target"], "执行小盘仓位": R["w_exec"]})
    for k, v in navs.items():
        out["净值_" + str(k)] = v
    out.to_csv(OUT_CSV, encoding="utf-8-sig")
    print("\nsaved:", OUT_CSV, "(%d行)" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
