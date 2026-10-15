# -*- coding: utf-8 -*-
"""
情绪择时回测（"情绪温度计"框架）

设计要点
--------
1) 情绪分项（均为**反向指标**：情绪越热 -> 未来收益越低）：
   杠杆情绪 LEV = 融资净买入(20日均) 的 60日 Z-score 的滚动分位    权重 0.50  [沪深两融]
   恐慌情绪 VIX = 50ETF QVIX(中国波指) 的滚动分位，取 1-分位      权重 0.50  [中国波指]
   量能情绪 TURN = 中证1000 成交量(5日/60日均) 的滚动分位          仅作诊断，不进入主策略
   每项都映射到 [0,1]（滚动分位），越接近 1 表示该维度情绪越"贪婪"。

   说明：量能情绪 的实测分组收益与其"反向"假设不符（见"分组诊断"），且单独使用Sharpe为负、
   并入后显著拖累组合，故主策略剔除，仅在稳健性表中保留以留痕。

2) 情绪温度 S = 100 × (0.5×杠杆 + 0.5×波指)，仅当两项同时有值才计算（避免对比期错配）。

3) 【防前视，关键】分位用**滚动窗口(W=750日)且只含当日及之前**的数据计算；
   两融/波指均为收盘后可得，信号再滞后 POS_LAG=1 个交易日执行。

4) 仓位（非对称温度计）：
   连续: w = clip((TH - S)/(TH - TL), 0, 1)   —— 温度越高仓位越低
   离散: S <= TL 满仓；S >= TH 空仓；其间半仓

回测标的：中证1000(000852.SH) 为主，附沪深300(000300.SH)；基准=买入持有。
输出：控制台报告 + data/sentiment_timing_result.csv（温度/分项/仓位/净值序列，便于后续做网页）
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SENT = os.path.join(HERE, "data", "情绪数据序列.xlsx")
OUT_CSV = os.path.join(HERE, "data", "sentiment_timing_result.csv")

COST = 0.0002          # 单边成本 2bp
ANNUAL = 242
POS_LAG = 1            # 信号可用后再滞后1个交易日执行（保守）
TH, TL = 70.0, 30.0    # 温度阈值（贪婪/恐慌）

W_PCT, MINP_PCT = 750, 250     # 滚动分位窗口 / 最少观测
Z_WIN = 60                     # 融资净买入 Z-score 窗口
VOL_FAST, VOL_SLOW = 5, 60     # 量能乖离的短/长期均线

W_LEV, W_VIX = 0.50, 0.50        # 主策略：杠杆 + 波指


# ---------------- 工具 ----------------
def pct_rank(s, win=W_PCT, minp=MINP_PCT):
    """滚动分位（只含当日及之前），返回 [0,1]。"""
    try:
        return s.rolling(win, min_periods=minp).rank(pct=True)
    except Exception:
        v = s.values.astype(float)
        out = np.full(len(s), np.nan)
        for i in range(len(s)):
            w = v[max(0, i - win + 1):i + 1]
            w = w[~np.isnan(w)]
            if len(w) >= minp and not np.isnan(v[i]):
                out[i] = (w <= v[i]).mean()
        return pd.Series(out, index=s.index)


def perf(ret, pos=None, name=""):
    nav = (1.0 + ret).cumprod()
    n = len(ret)
    yrs = n / ANNUAL
    ann = nav.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 and nav.iloc[-1] > 0 else np.nan
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
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
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


def ic_test(x, y, horizon):
    """非重叠取样的秩相关 IC 与 t 值。x=情绪, y=价格序列。"""
    fwd = y.shift(-horizon) / y - 1.0
    d = pd.concat([x.rename("x"), fwd.rename("y")], axis=1).dropna()
    if len(d) < horizon * 3:
        return np.nan, np.nan, 0
    d = d.iloc[::horizon]                      # 非重叠
    n = len(d)
    if n < 5:
        return np.nan, np.nan, n
    ic = d["x"].corr(d["y"], method="spearman")
    t = ic * np.sqrt((n - 2) / max(1e-12, 1 - ic ** 2))
    return ic, t, n


def bucket_fwd(x, y, horizon, nb=5):
    """把情绪分位 x∈[0,1] 分成 nb 组，计算非重叠的未来 horizon 日平均收益。"""
    fwd = y.shift(-horizon) / y - 1.0
    d = pd.concat([x.rename("x"), fwd.rename("y")], axis=1).dropna()
    if len(d) < horizon * nb * 2:
        return None
    d = d.iloc[::horizon]
    d["g"] = pd.cut(d["x"], bins=np.linspace(0, 1, nb + 1),
                    labels=["Q1(最恐慌)", "Q2", "Q3", "Q4", "Q5(最贪婪)"][:nb])
    return d.groupby("g", observed=True)["y"].agg(["mean", "count"])


# ---------------- 主流程 ----------------
def main():
    if not os.path.exists(SENT):
        print("[错误] 缺少 %s，请先运行 fetch_sentiment.py" % SENT)
        return 1

    d = pd.read_excel(SENT, index_col=0, sheet_name=None)
    d.pop("meta", None)
    for k in d:
        d[k].index = pd.to_datetime(d[k].index, errors="coerce")
        d[k] = d[k][~d[k].index.isna()].sort_index()

    mar = d["市场行情"]
    px1000 = mar["中证1000_收盘"].dropna()
    px300 = mar["沪深300_收盘"].dropna()
    cal = px1000.index
    r1000 = px1000.pct_change()
    r300 = px300.pct_change().reindex(cal)

    print("数据区间：")
    for k, v in d.items():
        print("   %-8s %s ~ %s  (%d行)" % (k, str(v.index.min())[:10], str(v.index.max())[:10], len(v)))

    # ---- 1) 三个分项（滚动分位，全部只用历史） ----
    mg = d["两融"].reindex(cal)
    net = mg["融资净买入_20日均"]
    lev_z = (net - net.rolling(Z_WIN, min_periods=Z_WIN // 2).mean()) / \
            net.rolling(Z_WIN, min_periods=Z_WIN // 2).std()
    s_lev = pct_rank(lev_z)                                   # 高=融资激进=贪婪

    qvix = d["中国波指"]["中国波指"].reindex(cal).ffill()
    s_vix = 1.0 - pct_rank(qvix)                              # 高波动=恐慌 => 贪婪分低

    vol = mar["中证1000_成交量"].reindex(cal)
    dev = vol.rolling(VOL_FAST, min_periods=VOL_FAST).mean() / \
          vol.rolling(VOL_SLOW, min_periods=VOL_SLOW // 2).mean() - 1.0
    s_turn = pct_rank(dev)                                    # 放量=过热=贪婪

    parts = pd.concat([s_lev.rename("杠杆"), s_vix.rename("恐慌"), s_turn.rename("量能")], axis=1)
    ready = parts["杠杆"].notna() & parts["恐慌"].notna()
    temp = 100.0 * (W_LEV * parts["杠杆"] + W_VIX * parts["恐慌"])
    temp = temp.where(ready)

    start = temp.dropna().index.min()
    print("\n回测起点(杠杆/波指两项分项齐备): %s   最新日期: %s" % (str(start)[:10], str(temp.index[-1])[:10]))

    temp = temp.loc[start:]
    parts = parts.loc[start:]
    # 变体：三因子等权（含量能），先算好再手工滞后
    temp_eq_raw = 100.0 * parts.mean(axis=1)

    # 信号滞后：t 日仓位由 t-POS_LAG 日收盘的情绪温度决定（两融为盘后发布，保守多滞后1日）
    # 注意只用 ffill（绝不用 bfill），避免用未来值填补缺口造成前视。
    temp_sig = temp.shift(POS_LAG).ffill()
    keep = temp_sig.notna()
    temp, parts, temp_sig = temp[keep], parts[keep], temp_sig[keep]

    pos_cont = ((TH - temp_sig) / (TH - TL)).clip(0, 1).rename("仓位_连续")
    pos_disc = pd.Series(np.where(temp_sig <= TL, 1.0, np.where(temp_sig >= TH, 0.0, 0.5)),
                         index=temp.index, name="仓位_离散")
    temp_eq = temp_eq_raw.shift(POS_LAG).ffill().reindex(temp.index)
    pos_eq = ((TH - temp_eq) / (TH - TL)).clip(0, 1)

    # ---- 2) 回测 ----
    r1 = r1000.reindex(temp.index).fillna(0.0)
    r3 = r300.reindex(temp.index).fillna(0.0)

    rows, navs = [], {}
    d0, nav0 = perf(r1, None, "中证1000 买入持有")
    rows.append(d0); navs["基准_中证1000"] = nav0
    d0b, nav0b = perf(r3, None, "沪深300 买入持有")
    rows.append(d0b); navs["基准_沪深300"] = nav0b

    for nm, pos, r in [("连续(中证1000)", pos_cont, r1),
                       ("离散(中证1000)", pos_disc, r1),
                       ("等权连续(中证1000)", pos_eq, r1),
                       ("连续(沪深300)", pos_cont, r3)]:
        net_ret = pos * r - pos.diff().abs() * COST
        dd, nav = perf(net_ret.fillna(0.0), pos, nm)
        rows.append(dd); navs[nm] = nav

    # 【关键对照】静态仓位：常量仓位不改变 Sharpe；若策略 Sharpe > 等敞口静态仓位，
    # 才说明存在真实择时能力，而非单纯"少拿股票"。
    for c in [0.25, 0.50, 0.75]:
        p = pd.Series(c, index=temp.index)
        dd, nav = perf((p * r1).fillna(0.0), p, "静态%2d%%仓位(中证1000)" % int(c * 100))
        rows.append(dd); navs["静态%d" % int(c * 100)] = nav
    p = pd.Series(pos_cont.mean(), index=temp.index)
    dd, nav = perf((p * r1).fillna(0.0), p, "静态%.0f%%(与连续等敞口)" % (pos_cont.mean() * 100))
    rows.append(dd); navs["静态等敞口"] = nav

    show(rows, "情绪择时回测（成本 %.2fbp 单边，温度阈值 %g/%g，滞后%d日）" % (COST * 1e4, TL, TH, POS_LAG))

    # ---- 3) IC 诊断：情绪温度对未来收益的预测力 ----
    print("\n" + "=" * 100)
    print("IC 诊断（非重叠取样，Spearman）：情绪温度越高应伴随未来收益越低（预期 IC<0）")
    print("=" * 100)
    ic_rows = []
    for h in [5, 10, 20, 60]:
        ic, t, n = ic_test(temp, px1000, h)
        ic_rows.append(dict(指标="情绪温度", 期限="%d日" % h, IC=ic, t值=t, 样本=n))
    for nm, s in [("杠杆情绪", s_lev), ("恐慌情绪", s_vix), ("量能情绪", s_turn)]:
        ss = s.reindex(temp.index)
        for h in [20, 60]:
            ic, t, n = ic_test(ss, px1000, h)
            ic_rows.append(dict(指标=nm, 期限="%d日" % h, IC=ic, t值=t, 样本=n))
    dfic = pd.DataFrame(ic_rows)
    dfic["IC"] = dfic["IC"].map(lambda v: "%+.3f" % v if pd.notna(v) else "-")
    dfic["t值"] = dfic["t值"].map(lambda v: "%+.2f" % v if pd.notna(v) else "-")
    print(dfic.to_string(index=False))

    # ---- 3b) 分组诊断：情绪分位五等分组的未来收益（检验信号是否单调） ----
    print("\n" + "=" * 100)
    print("分组诊断：情绪分位五等分组的未来20日平均收益（中证1000，非重叠）")
    print("=" * 100)
    for nm, s in [("情绪温度", (temp / 100.0).clip(0, 1)), ("杠杆情绪", s_lev),
                  ("恐慌情绪", s_vix), ("量能情绪", s_turn)]:
        g = bucket_fwd(s.reindex(temp.index), px1000, 20)
        if g is None:
            print("  %s: 样本不足" % nm)
            continue
        cells = " | ".join("%s %+.2f%%(n=%d)" % (i, r["mean"] * 100, r["count"])
                           for i, r in g.iterrows())
        print("  %-8s %s" % (nm, cells))

    # ---- 4) 稳健性：阈值 / 分位窗口 / 权重 ----
    rb = []
    for t_lo, t_hi in [(40, 60), (30, 70), (20, 80), (10, 90)]:
        p = pd.Series(np.where(temp_sig <= t_lo, 1.0, np.where(temp_sig >= t_hi, 0.0, 0.5)),
                      index=temp.index)
        net_ret = p * r1 - p.diff().abs() * COST
        dd, _ = perf(net_ret.fillna(0.0), p, "阈值 %d/%d(离散)" % (t_lo, t_hi))
        rb.append(dd)
        pc = ((t_hi - temp_sig) / (t_hi - t_lo)).clip(0, 1)
        net_ret = pc * r1 - pc.diff().abs() * COST
        dd, _ = perf(net_ret.fillna(0.0), pc, "阈值 %d/%d(连续)" % (t_lo, t_hi))
        rb.append(dd)
    show(rb, "阈值稳健性（中证1000）")

    rb2 = []
    for w in [500, 750, 1000, 1500, 10 ** 9]:
        pl = pct_rank(lev_z, w, min(w // 3, 250))
        pv = 1.0 - pct_rank(qvix, w, min(w // 3, 250))
        pr = pd.concat([pl, pv], axis=1)
        tt = 100.0 * (W_LEV * pr.iloc[:, 0] + W_VIX * pr.iloc[:, 1])
        tt = tt.where(pr.notna().all(axis=1)).reindex(temp.index).shift(POS_LAG).ffill()
        pc = ((TH - tt) / (TH - TL)).clip(0, 1)
        net_ret = pc * r1 - pc.diff().abs() * COST
        dd, _ = perf(net_ret.fillna(0.0), pc, "分位窗口 %s" % ("全历史" if w > 10 ** 8 else "%d日" % w))
        rb2.append(dd)
    show(rb2, "分位窗口稳健性（中证1000，连续仓位）")

    rb3 = []
    for nm, (a, b, c) in {"杠杆+波指 0.5/0.5(主)": (0.5, 0.5, 0),
                          "杠杆+波指 0.3/0.7": (0.3, 0.7, 0),
                          "杠杆+波指+量能 0.4/0.3/0.3": (0.4, 0.3, 0.3),
                          "三因子等权": (1 / 3, 1 / 3, 1 / 3),
                          "仅杠杆": (1, 0, 0),
                          "仅波指": (0, 1, 0),
                          "仅量能": (0, 0, 1)}.items():
        tot = a + b + c
        a, b, c = a / tot, b / tot, c / tot
        tt = 100.0 * (a * parts["杠杆"] + b * parts["恐慌"] + c * parts["量能"])
        tt = tt.shift(POS_LAG).ffill()
        pc = ((TH - tt) / (TH - TL)).clip(0, 1)
        net_ret = pc * r1 - pc.diff().abs() * COST
        dd, _ = perf(net_ret.fillna(0.0), pc, nm)
        rb3.append(dd)
    show(rb3, "权重稳健性（中证1000，连续仓位）")

    # ---- 5) 分段 ----
    segs = [("2016-2019", "2016-01-01", "2019-12-31"),
            ("2020-2022", "2020-01-01", "2022-12-31"),
            ("2023-至今", "2023-01-01", "2099-12-31")]
    rb4 = []
    for nm, a, b in segs:
        sub = temp_sig.loc[a:b]
        if len(sub) < 100:
            continue
        p = ((TH - sub) / (TH - TL)).clip(0, 1)
        r = r1.reindex(sub.index)
        net_ret = p * r - p.diff().abs() * COST
        dd, _ = perf(net_ret.fillna(0.0), p, "策略 " + nm)
        rb4.append(dd)
        dd2, _ = perf(r.fillna(0.0), None, "基准 " + nm)
        rb4.append(dd2)
    show(rb4, "分段表现（中证1000，连续仓位）")

    # ---- 5b) 年度收益（时序一致性检验） ----
    pos_all = ((TH - temp_sig) / (TH - TL)).clip(0, 1)
    net_all = (pos_all * r1 - pos_all.diff().abs() * COST).fillna(0.0)
    yr = pd.DataFrame({"策略": (1 + net_all).groupby(net_all.index.year).prod() - 1,
                       "基准": (1 + r1).groupby(r1.index.year).prod() - 1,
                       "平均仓位": pos_all.groupby(pos_all.index.year).mean()})
    yr["超额"] = yr["策略"] - yr["基准"]
    yr.index.name = "年份"
    yr = yr.reset_index()
    for c in ["策略", "基准", "超额", "平均仓位"]:
        yr[c] = yr[c].map(lambda v: "%+.2f%%" % (v * 100) if c != "平均仓位" else "%.2f" % v)
    print("\n" + "=" * 100)
    print("年度收益（中证1000，连续仓位）")
    print("=" * 100)
    print(yr.to_string(index=False))

    # ---- 6) 当前信号 ----
    last = temp.index[-1]
    print("\n" + "=" * 100)
    print("最新情绪信号（%s）" % str(last)[:10])
    print("=" * 100)
    print("   杠杆情绪=%.3f   恐慌情绪=%.3f   (参考 量能情绪=%.3f)" % (
        parts["杠杆"].loc[last], parts["恐慌"].loc[last], parts["量能"].loc[last]))
    print("   情绪温度 S=%.1f （0=极度恐慌, 100=极度贪婪）" % temp.loc[last])
    print("   建议仓位：连续=%.2f   离散=%s" % (
        pos_cont.loc[last],
        "满仓" if temp.loc[last] <= TL else ("空仓" if temp.loc[last] >= TH else "半仓")))
    print("   最近20日温度：%s" % " ".join("%.0f" % v for v in temp.tail(20).values))

    # ---- 7) 落盘 ----
    out = pd.DataFrame({"情绪温度": temp, "杠杆情绪": parts["杠杆"], "恐慌情绪": parts["恐慌"],
                        "量能情绪": parts["量能"], "仓位_连续": pos_cont, "仓位_离散": pos_disc})
    for k, v in navs.items():
        out[k] = v
    out.to_csv(OUT_CSV, encoding="utf-8-sig")
    print("\nsaved:", OUT_CSV, "(%d行)" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
