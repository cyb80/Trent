# -*- coding: utf-8 -*-
"""
Hurst 指数诊断实验（不改动现有策略，仅判断值不值得引入）
回答三个问题：
  Q1 滚动 Hurst 相对 200日均线信号是否有增量信息？(相关性 / 按趋势态分组的 H 分布)
  Q2 两次最大回撤(2025-03、2026-06)之前，Hurst 是否已提前回落？
  Q3 Hurst 能否区分"假突破(whipsaw)"与"真突破"？(低 H 是否为震荡/假信号区)

方法：R/S 法(Anis-Lloyd 短样本偏差校正) + DFA-1 交叉验证；滚动窗口 W=250 / 150。
数据：data/指数行情序列.xlsx 的 000852(中证1000) 收盘。
输出：控制台文字报告 + data/hurst_diag.csv（留作画图）。
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.path.join(HERE, "data", "指数行情序列.xlsx")

MA = 200
CONF_DAYS = 5
W_MAIN, W_ALT = 250, 150
SCALES = [8, 16, 32, 64]
# 来自策略净值的两次最大回撤窗口（gen_ma200_backtest.py 输出）
DD_WINDOWS = [("2025-03-18", "2025-04-08"), ("2026-06-30", "2026-07-20")]
ANNUAL = 242


def load_close(key="000852"):
    sheets = pd.read_excel(XLSX, index_col=0, sheet_name=None)
    sn = [k for k in sheets if key in k][0]
    d = sheets[sn].copy()
    d.index = pd.to_datetime(d.index)
    col = [c for c in d.columns if str(c).upper().strip() == "CLOSE"][0]
    s = d[col].astype(float).sort_index()
    return s[~s.index.duplicated(keep="last")]


def _expected_rs(n):
    """Anis-Lloyd/Peters 短样本 iid 期望 R/S (n<=340)；n>340 用 n**0.5 近似基线。"""
    if n <= 340:
        k = np.arange(1, n)
        s = np.sum(np.sqrt((n - k) / k))
        return ((n - 0.5) / n) * (1.0 / np.sqrt(n * np.pi / 2.0)) * s
    return None


def hurst_rs(x, scales=SCALES):
    """校正 R/S：对 log((R/S)/E(n)) ~ log(n) 回归，斜率+0.5（随机=0.5）。"""
    x = np.asarray(x, float)
    x = x - x.mean()
    pts = []
    for n in scales:
        if n > len(x) // 2:
            continue
        nb = len(x) // n
        rss = []
        for b in range(nb):
            seg = x[b * n:(b + 1) * n]
            y = np.cumsum(seg)
            r = y.max() - y.min()
            s = seg.std(ddof=1)
            if s > 0:
                rss.append(r / s)
        e = _expected_rs(n)
        if rss and e and e > 0:
            pts.append((np.log(n), np.log(np.mean(rss) / e)))
    if len(pts) < 2:
        return np.nan
    xs = np.array([p[0] for p in pts]); ys = np.array([p[1] for p in pts])
    return 0.5 + np.polyfit(xs, ys, 1)[0]


def hurst_dfa(x, scales=SCALES):
    """DFA-1：随机=0.5，与 R/S 交叉验证。"""
    x = np.asarray(x, float)
    y = np.cumsum(x - x.mean())
    fs, ns = [], []
    for s0 in scales:
        s = s0
        if s > len(x) // 4:
            continue
        nb = len(x) // s
        if nb < 1:
            continue
        t = np.arange(s)
        rms = []
        for b in range(nb):
            seg = y[b * s:(b + 1) * s]
            fit = np.polyval(np.polyfit(t, seg, 1), t)
            rms.append(np.mean((seg - fit) ** 2))
        f = np.sqrt(np.mean(rms))
        if f > 0:
            fs.append(np.log(f)); ns.append(np.log(s))
    if len(fs) < 2:
        return np.nan
    return float(np.polyfit(ns, fs, 1)[0])


def rolling_hurst(ret, W, fn):
    out = np.full(len(ret), np.nan)
    v = ret.values
    for i in range(W, len(v) + 1):
        out[i - 1] = fn(v[i - W:i])
    return pd.Series(out, index=ret.index)


def trend_state(close, ma=MA, conf=CONF_DAYS):
    m = close.rolling(ma).mean()
    raw = (close > m).astype(float)
    v = raw.fillna(0).values
    st = np.zeros(len(v)); cur = v[0]; pv = None; pc = 0
    for i in range(1, len(v)):
        x = v[i]
        if pv is not None:
            if x == pv:
                pc += 1
                if pc >= conf:
                    cur = pv; pv = None
            else:
                pv = None; pc = 0
                if x != cur:
                    pv = x; pc = 1
        elif x != cur:
            pv = x; pc = 1
        st[i] = cur
    return m, raw, pd.Series(st, index=close.index)


def flip_stats(close, H, ma=MA, conf=CONF_DAYS):
    """200MA 原始翻转点：conf 日内反向=假突破，否则真突破；比较两类前一日 H。"""
    raw = (close > close.rolling(ma).mean()).values
    flips = np.where(raw[1:] != raw[:-1])[0] + 1
    hf, ht = [], []
    for i in flips:
        j = i + conf
        if j >= len(raw) or np.isnan(H.iloc[i]):
            continue
        (hf if (raw[i:j + 1] != raw[i]).any() else ht).append(H.iloc[i])
    return np.array(hf), np.array(ht)


def dd_report(H, state, idx, windows):
    print("\n【Q2】最大回撤窗口前后的 Hurst（检验是否提前回落）")
    for a, b in windows:
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        ia = idx.get_indexer([a], method="nearest")[0]
        ib = idx.get_indexer([b], method="nearest")[0]
        pre = H.iloc[max(0, ia - 20):ia].dropna()
        dur = H.iloc[ia:ib + 1].dropna()
        if len(pre) == 0 or len(dur) == 0:
            print("  %s~%s: 样本不足" % (a.date(), b.date())); continue
        print("  %s ~ %s" % (a.date(), b.date()))
        print("    前20日均H=%.3f  窗口内均H=%.3f  窗口起点H=%.3f  前20日起点H=%.3f  Δ(窗口-前20)=%+.3f"
              % (pre.mean(), dur.mean(), dur.iloc[0], pre.iloc[0], dur.mean() - pre.mean()))
        print("    窗口内 H<0.5 占比=%.0f%%   强势天数占比=%.0f%%"
              % (100 * (dur < 0.5).mean(), 100 * state.loc[dur.index].mean()))


def main():
    close = load_close("000852")
    ret = np.log(close / close.shift(1)).dropna()
    print("数据: 中证1000(000852)  %d条  %s ~ %s" % (len(close), close.index[0].date(), close.index[-1].date()))

    H250 = rolling_hurst(ret, W_MAIN, hurst_rs)
    H250d = rolling_hurst(ret, W_MAIN, hurst_dfa)
    H150 = rolling_hurst(ret, W_ALT, hurst_rs)
    m, raw, state = trend_state(close)

    # 评价期(与策略一致) 起于 2023-05-04
    ev = slice(pd.Timestamp("2023-05-04"), None)
    he = H250.loc[ev].dropna()
    print("\n【Q1】Hurst 分布 (W=250, 评价期, n=%d): 均值=%.3f 中位=%.3f  最小=%.3f 最大=%.3f"
          % (len(he), he.mean(), he.median(), he.min(), he.max()))
    print("   H>0.55(趋势)占比=%.0f%%  0.45~0.55占比=%.0f%%  H<0.45(震荡)占比=%.0f%%"
          % (100 * (he > 0.55).mean(), 100 * ((he >= 0.45) & (he <= 0.55)).mean(), 100 * (he < 0.45).mean()))
    print("   两种估计相关性 corr(H_RS, H_DFA)=%.3f" % H250.corr(H250d))

    feats = pd.DataFrame({
        "H250": H250, "H150": H150,
        "dist": close / m - 1.0,
        "slope20": m.pct_change(20),
        "vol60": ret.rolling(60).std() * np.sqrt(ANNUAL),
        "state": state,
    }).loc[ev]
    print("\n   Hurst 与趋势类变量的相关性(评价期):")
    for c in ["dist", "slope20", "vol60"]:
        print("     corr(H250, %-7s)=%+.3f   corr(H150, %-7s)=%+.3f"
              % (c, feats["H250"].corr(feats[c]), c, feats["H150"].corr(feats[c])))
    g = feats.groupby("state")["H250"].agg(["mean", "median", "count"])
    print("   Hurst 按趋势态分组:")
    for s, row in g.iterrows():
        print("     %s(%s): 均H=%.3f 中位=%.3f n=%d" % (("强势" if s else "弱势"), "close>MA" if s else "close<=MA", row["mean"], row["median"], row["count"]))

    print("\n【Q3】假突破(whipsaw) vs 真突破 的前一日 Hurst (W=250)")
    hf, ht = flip_stats(close, H250)
    if len(hf):
        print("   假突破 n=%d: 均H=%.3f 中位=%.3f" % (len(hf), hf.mean(), np.median(hf)))
    if len(ht):
        print("   真突破 n=%d: 均H=%.3f 中位=%.3f" % (len(ht), ht.mean(), np.median(ht)))
    if len(hf) and len(ht):
        print("   差值(假-真)=%+.3f  →  负值说明假突破多发生在低 H(震荡)区" % (hf.mean() - ht.mean()))

    dd_report(H250, state, close.index, DD_WINDOWS)

    # 稳健性：W=150 的 Q3 结论是否一致
    hf2, ht2 = flip_stats(close, H150)
    if len(hf2) and len(ht2):
        print("\n[稳健性] W=150: 假突破均H=%.3f  真突破均H=%.3f  差值=%+.3f" % (hf2.mean(), ht2.mean(), hf2.mean() - ht2.mean()))

    out = pd.DataFrame({"close": close, "H250_rs": H250, "H250_dfa": H250d,
                        "H150_rs": H150, "state": state}).dropna(subset=["H250_rs"])
    csv = os.path.join(HERE, "data", "hurst_diag.csv")
    out.to_csv(csv, encoding="utf-8-sig")
    print("\n已保存: %s (%d行)" % (csv, len(out)))


if __name__ == "__main__":
    main()