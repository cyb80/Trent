# -*- coding: utf-8 -*-
"""
宏观择时回测（货币信用 + 景气框架）

设计要点
--------
1) 信号分四组：
   货币 = mean(M1同比, M2同比, 央行总资产同比)        权重 0.40   [新浪源]
   信用 = 信贷脉冲(新增信贷12M滚动求和的同比)          权重 0.40   [东方财富]
   景气 = mean(制造业PMI(含水平分), 工业增加值同比)     权重 0.20   [东方财富]
2) 每个指标先化为"方向分"：3个月平滑后的3个月变化，取其过去60个月的滚动分位，
   映射到 [-1, 1]。PMI 额外叠加"水平分" (PMI-50)/3。
3) 【防前视，关键】月度数据按实际发布时间对齐到交易日：
   - PMI：当月最后一个自然日
   - 其余月度指标：次月15日
   再映射到"该日或之后的首个交易日"，并额外滞后 POS_LAG 个交易日执行。
4) 仓位：composite >= +TH 满仓；<= -TH 空仓；其间半仓。
   另给出"连续仓位"变体 (composite+1)/2。

回测标的：沪深300(000300.SH)，另附中证1000(000852.SH)；基准=买入持有。
输出：控制台报告 + data/macro_timing_result.csv（净值/仓位序列，便于后续做网页）
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MACRO = os.path.join(HERE, "data", "宏观数据序列.xlsx")
IDX = os.path.join(HERE, "data", "指数行情序列.xlsx")
OUT_CSV = os.path.join(HERE, "data", "macro_timing_result.csv")

COST = 0.0002          # 单边成本 2bp
ANNUAL = 242
POS_LAG = 1            # 信号可用后再滞后1个交易日执行（保守）
TH = 0.30              # 满仓/空仓阈值
ROLL_WIN, ROLL_MINP = 60, 24
SMOOTH, CHG = 3, 3

W_MONEY, W_CREDIT, W_CLIMATE = 0.40, 0.40, 0.20


# ---------------- 工具 ----------------
def avail_date(period, kind):
    """月度(或季度)观测 -> 实际可获得的自然日。"""
    if kind == "pmi":
        return period + pd.offsets.MonthEnd(0)                 # 当月最后一天
    if kind == "gdp":
        return period + pd.DateOffset(months=1) + pd.Timedelta(days=19)  # 次季首月20日
    return period + pd.DateOffset(months=1) + pd.Timedelta(days=14)      # 次月15日


def dir_score(s, win=ROLL_WIN, minp=ROLL_MINP, smooth=SMOOTH, chg=CHG):
    """方向分：3M平滑的3M变化的滚动分位 -> [-1,1]。"""
    sm = s.rolling(smooth).mean()
    d = sm.diff(chg)
    r = d.rolling(win, min_periods=minp).apply(lambda w: (w <= w[-1]).mean(), raw=True)
    return 2.0 * r - 1.0


def to_daily(monthly, cal, kind, lag=POS_LAG):
    """把月度序列按其可用日期对齐到交易日，并滞后 lag 个交易日。"""
    out = pd.Series(np.nan, index=cal, dtype=float)
    avails = [avail_date(p, kind) for p in monthly.index]
    pos = cal.searchsorted(pd.DatetimeIndex(avails), side="left")
    for p, v in zip(pos, monthly.values):
        if p < len(cal):
            out.iloc[p] = v
    out = out.ffill()
    return out.shift(lag) if lag else out


def load_indices():
    d = pd.read_excel(IDX, index_col=0, sheet_name=None)
    out = {}
    for k in ["000300.SH", "000852.SH"]:
        v = d[k].copy()
        v.index = pd.to_datetime(v.index, errors="coerce")
        v = v[~v.index.isna()]
        out[k] = pd.to_numeric(v["CLOSE"], errors="coerce").dropna().sort_index()
    return out


def perf(ret, pos=None, name=""):
    nav = (1.0 + ret).cumprod()
    n = len(ret)
    yrs = n / ANNUAL
    ann = nav.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    vol = ret.std() * np.sqrt(ANNUAL)
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = (ret.mean() * ANNUAL) / vol if vol > 0 else np.nan
    calmar = ann / abs(mdd) if mdd < 0 else np.nan
    d = dict(名称=name, 年化=ann, 累计=nav.iloc[-1] - 1, 最大回撤=mdd,
             年化波动=vol, Sharpe=sharpe, Calmar=calmar, 期数=n)
    if pos is not None:
        d["平均仓位"] = pos.mean()
        d["年换手"] = pos.diff().abs().sum() / yrs
    return d, nav


def show(rows, title):
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)
    df = pd.DataFrame(rows)
    fmt = {"年化": "{:.2%}".format, "累计": "{:.2%}".format, "最大回撤": "{:.2%}".format,
           "年化波动": "{:.2%}".format, "Sharpe": "{:.2f}".format, "Calmar": "{:.2f}".format,
           "平均仓位": "{:.2f}".format, "年换手": "{:.2f}".format}
    out = df.copy()
    for c, f in fmt.items():
        if c in out.columns:
            out[c] = out[c].map(lambda v: f(v) if pd.notna(v) else "-")
    print(out.to_string(index=False))
    return df


# ---------------- 主流程 ----------------
def main():
    if not os.path.exists(MACRO):
        print("[错误] 缺少 %s，请先运行 fetch_macro.py" % MACRO)
        return 1

    m = pd.read_excel(MACRO, index_col=0, sheet_name=None)
    for k in list(m.keys()):
        if k == "meta":
            m.pop(k)
            continue
        m[k].index = pd.to_datetime(m[k].index)
        m[k] = m[k].sort_index()

    idxs = load_indices()
    cal = idxs["000300.SH"].index

    print("宏观数据最新期：")
    for k, v in m.items():
        print("   %-12s %s ~ %s  (%d期)" % (k, str(v.index.min())[:7], str(v.index.max())[:7], len(v)))

    # ---- 1) 月度指标与方向分 ----
    md = {}
    md["M1同比"] = m["货币供应量"]["M1同比"]
    md["M2同比"] = m["货币供应量"]["M2同比"]
    md["央行总资产同比"] = m["央行资产负债表"]["央行总资产"].pct_change(12) * 100

    lc = m["新增信贷"]["新增信贷当月"]
    md["信贷脉冲"] = (lc.rolling(12).sum() / lc.rolling(12).sum().shift(12) - 1) * 100
    sr = m["社会融资规模"]["社融增量"]
    md["社融脉冲"] = (sr.rolling(12).sum() / sr.rolling(12).sum().shift(12) - 1) * 100

    md["制造业PMI"] = m["PMI"]["制造业PMI"]
    md["工业增加值同比"] = m["工业增加值"]["工业增加值同比"]
    md["PPI同比"] = m["PPI"]["PPI同比"]
    md["CPI同比"] = m["CPI"]["CPI同比"]

    # PMI 水平分
    pmi_lvl = ((md["制造业PMI"] - 50.0) / 3.0).clip(-1, 1)
    pmi_score = (0.5 * dir_score(md["制造业PMI"]) + 0.5 * pmi_lvl)

    scores_month = {
        "M1同比": dir_score(md["M1同比"]),
        "M2同比": dir_score(md["M2同比"]),
        "央行总资产同比": dir_score(md["央行总资产同比"]),
        "信贷脉冲": dir_score(md["信贷脉冲"]),
        "社融脉冲": dir_score(md["社融脉冲"]),
        "制造业PMI": pmi_score,
        "工业增加值同比": dir_score(md["工业增加值同比"]),
        "PPI同比": dir_score(md["PPI同比"]),
    }

    # ---- 2) 对齐到交易日 ----
    kind_of = {"制造业PMI": "pmi"}
    daily = {}
    for k, s in scores_month.items():
        daily[k] = to_daily(s, cal, kind_of.get(k, "month"))

    # ---- 3) 合成 ----
    money = pd.concat([daily["M1同比"], daily["M2同比"], daily["央行总资产同比"]], axis=1).mean(axis=1)
    credit = daily["信贷脉冲"]
    climate = pd.concat([daily["制造业PMI"], daily["工业增加值同比"]], axis=1).mean(axis=1)
    comp = W_MONEY * money + W_CREDIT * credit + W_CLIMATE * climate

    start = comp.dropna().index.min()
    # 三个分项都齐备后再开始
    ready = pd.concat([money, credit, climate], axis=1).dropna().index
    if len(ready):
        start = max(start, ready.min())
    comp = comp.loc[start:]
    print("\n回测起点(三项分项齐备): %s   最新信号日: %s" % (str(start)[:10], str(comp.index[-1])[:10]))

    # ---- 4) 仓位 ----
    pos_disc = pd.Series(np.where(comp >= TH, 1.0, np.where(comp <= -TH, 0.0, 0.5)), index=comp.index)
    pos_cont = ((comp + 1) / 2).clip(0, 1)

    # 变体：仅货币信用（无景气）
    comp2 = (W_MONEY * money + W_CREDIT * credit) / (W_MONEY + W_CREDIT)
    pos_mc = pd.Series(np.where(comp2.loc[start:] >= TH, 1.0, np.where(comp2.loc[start:] <= -TH, 0.0, 0.5)),
                       index=comp.index)

    r300 = idxs["000300.SH"].pct_change().reindex(comp.index).fillna(0.0)

    rows = []
    navs = {}
    for code, px in idxs.items():
        r = px.pct_change().reindex(comp.index).fillna(0.0)
        label = "沪深300" if code.startswith("000300") else "中证1000"

        d0, nav0 = perf(r, None, "%s 买入持有" % label)
        rows.append(d0); navs["%s_基准" % label] = nav0

        for nm, pos in [("离散(%s)" % label, pos_disc),
                        ("连续(%s)" % label, pos_cont),
                        ("仅货币信用(%s)" % label, pos_mc)]:
            gross = pos.shift(0) * r
            turn = pos.diff().abs()
            net = gross - turn * COST
            d, nav = perf(net.fillna(0.0), pos, nm)
            rows.append(d); navs[nm] = nav

    # 【关键对照】静态仓位：检验"回撤/收益改善"是否只是降低平均敞口所致。
    # 常数值仓位不改变 Sharpe（均值与波动同比例缩放），若策略 Sharpe 高于静态仓位，
    # 才说明存在真正的择时能力；否则只是"少拿股票"。
    for c in [0.25, 0.50, 0.75]:
        p = pd.Series(c, index=comp.index)
        d, nav = perf((p * r300).fillna(0.0), p, "静态%2d%%仓位(沪深300)" % int(c * 100))
        rows.append(d); navs["静态%d" % int(c * 100)] = nav
    p = pd.Series(pos_cont.mean(), index=comp.index)
    d, nav = perf((p * r300).fillna(0.0), p, "静态%.0f%%(与连续等敞口)" % (pos_cont.mean() * 100))
    rows.append(d); navs["静态等敞口"] = nav

    df = show(rows, "宏观择时回测（成本 %.2fbp 单边，阈值±%.2f，滞后%d日）" % (COST * 1e4, TH, POS_LAG))

    # ---- 5) 稳健性 ----
    rb = []
    for th in [0.15, 0.25, 0.30, 0.40, 0.50]:
        p = pd.Series(np.where(comp >= th, 1.0, np.where(comp <= -th, 0.0, 0.5)), index=comp.index)
        net = p * r300 - p.diff().abs() * COST
        d, _ = perf(net.fillna(0.0), p, "阈值±%.2f" % th)
        rb.append(d)
    show(rb, "阈值稳健性（沪深300，离散仓位）")

    rb2 = []
    for lag in [0, 1, 2, 5, 10]:
        dly = {k: to_daily(scores_month[k], cal, kind_of.get(k, "month"), lag=lag) for k in scores_month}
        mo = pd.concat([dly["M1同比"], dly["M2同比"], dly["央行总资产同比"]], axis=1).mean(axis=1)
        cr = dly["信贷脉冲"]
        cl = pd.concat([dly["制造业PMI"], dly["工业增加值同比"]], axis=1).mean(axis=1)
        c = (W_MONEY * mo + W_CREDIT * cr + W_CLIMATE * cl).reindex(comp.index)
        p = pd.Series(np.where(c >= TH, 1.0, np.where(c <= -TH, 0.0, 0.5)), index=comp.index)
        net = p * r300 - p.diff().abs() * COST
        d, _ = perf(net.fillna(0.0), p, "滞后%d日" % lag)
        rb2.append(d)
    show(rb2, "执行滞后稳健性（沪深300，离散仓位）")

    # ---- 6) 分段 ----
    segs = [("2010-2015", "2010-01-01", "2015-12-31"),
            ("2016-2020", "2016-01-01", "2020-12-31"),
            ("2021-至今", "2021-01-01", "2099-12-31")]
    rb3 = []
    for nm, a, b in segs:
        sub = comp.loc[a:b]
        if len(sub) < 100:
            continue
        p = pd.Series(np.where(sub >= TH, 1.0, np.where(sub <= -TH, 0.0, 0.5)), index=sub.index)
        r = r300.reindex(sub.index)
        net = p * r - p.diff().abs() * COST
        d, _ = perf(net.fillna(0.0), p, "策略 " + nm)
        rb3.append(d)
        d2, _ = perf(r.fillna(0.0), None, "基准 " + nm)
        rb3.append(d2)
    show(rb3, "分段表现（沪深300）")

    # ---- 7) 当前信号 ----
    last = comp.index[-1]
    print("\n" + "=" * 96)
    print("最新宏观信号（%s）" % str(last)[:10])
    print("=" * 96)
    for k in ["M1同比", "M2同比", "央行总资产同比", "信贷脉冲", "制造业PMI", "工业增加值同比"]:
        print("   %-14s 方向分=%+.2f" % (k, daily[k].loc[last] if pd.notna(daily[k].loc[last]) else float("nan")))
    print("   货币=%.3f  信用=%.3f  景气=%.3f  => 综合=%.3f" % (money.loc[last], credit.loc[last], climate.loc[last], comp.loc[last]))
    print("   建议仓位(离散)=%s   连续=%.2f" % (
        "满仓" if comp.loc[last] >= TH else ("空仓" if comp.loc[last] <= -TH else "半仓"), pos_cont.loc[last]))

    # ---- 8) 落盘 ----
    out = pd.DataFrame({"综合信号": comp, "货币": money, "信用": credit, "景气": climate,
                        "仓位_离散": pos_disc, "仓位_连续": pos_cont})
    for k, v in navs.items():
        out[k] = v
    out.to_csv(OUT_CSV, encoding="utf-8-sig")
    print("\nsaved:", OUT_CSV, "(%d行)" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
