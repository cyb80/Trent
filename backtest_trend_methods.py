# -*- coding: utf-8 -*-
"""
IC/IM 吃贴水策略回测 —— 对比两种长期趋势判断方式：
A) 只用200日均线
B) 180/200/240日 三均线多数表决

框架统一（与《当前较简介的候选策略.md》一致）：
- IC/IM 近月合约，到期前提前5个自然日换月
- 双弱 0.25倍、双强低波动最高1.2倍、强势75/25
- 不使用贴水权重微调
- 单边成本 2bp
- 统一评价期 2023-05-04 至 2026-04-20
"""
import pandas as pd
import numpy as np
import glob, re, os

# 数据路径：优先使用环境变量(MA200_FUT_DIR / MA200_IDX_FILE)覆盖，
# 否则回退到本机开发路径(若存在)，最后回退到仓库相对 data/ 目录，保证跨环境可移植。
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_FUT = r"D:\ipynb\长期数据\sqlite\股指期货_期权"
_LOCAL_IDX = r"D:\ipynb\长期数据\sqlite\指数行情序列.xlsx"
_REPO_IDX  = os.path.join(_PKG_DIR, "data", "指数行情序列.xlsx")
FUT_DIR  = os.environ.get("MA200_FUT_DIR") or (_LOCAL_FUT if os.path.isdir(_LOCAL_FUT) else os.path.join(_PKG_DIR, "data"))
IDX_FILE = os.environ.get("MA200_IDX_FILE") or (_LOCAL_IDX if os.path.exists(_LOCAL_IDX) else _REPO_IDX)
EVAL_START = pd.Timestamp("2023-05-04")
EVAL_END   = pd.Timestamp("2026-04-20")
ANNUAL     = 242
SINGLE_COST = 0.0002   # 2bp 单边
EVAL_DAYS  = (EVAL_END - EVAL_START).days

CONF_DAYS   = 5        # 趋势连续确认天数
VOL60      = 60        # 实现波动率窗口
MED_WIN    = 252       # 波动率滚动中位数窗口
MED_MIN    = 126       # 中位数最少历史观测
LAG        = 2         # 信号相对收益区间滞后交易日数（文档=2，LAG=0 为对照）


# ---------- 1. 读取单一具体合约数据 ----------
def load_contracts(sym):
    pat = re.compile(r"CFFEX\.(%s)(\d{4})\.csv$" % sym)
    files = glob.glob(os.path.join(FUT_DIR, "CFFEX.%s????.csv" % sym))
    data = {}
    meta = []
    for f in files:
        m = pat.search(os.path.basename(f))
        if not m:
            continue
        code = m.group(0)[:-4]     # e.g. CFFEX.IC2506
        yymm = m.group(2)          # e.g. 2506
        yy = 2000 + int(yymm[:2]); mm = int(yymm[2:])
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        df = df[["close", "volume"]].dropna(subset=["close"])
        if df.empty:
            continue
        # 第三个星期五估算到期日
        third_fri = estimate_third_friday(yy, mm)
        data[code] = {"df": df, "month_end": pd.Timestamp(yy, mm, 1),
                      "third_fri": third_fri}
        meta.append((code, df.index.max(), third_fri))
    return data, pd.DataFrame(meta, columns=["code", "last_date", "third_fri"])


def estimate_third_friday(yy, mm):
    import calendar
    # 当月第3个星期五
    d = pd.Timestamp(yy, mm, 1)
    # 该月第一个星期五
    first_day_wd = calendar.weekday(yy, mm, 1)
    first_friday = 1 + ((4 - first_day_wd) % 7)
    return pd.Timestamp(yy, mm, first_friday + 14)  # 第3个星期五


def contract_expiry(code, rec, global_cutoff):
    """到期日：已到期历史合约=<最后一个交易日>；数据截止仍未到期=<第三个星期五>。"""
    last = rec[code]["df"].index.max()
    third = rec[code]["third_fri"]
    if last >= (global_cutoff - pd.Timedelta(days=1)):
        return third
    return last


# ---------- 2. 构建统一交易日历 ----------
def build_calendar(contracts):
    all_days = set()
    for rec in contracts.values():
        all_days.update(rec["df"].index)
    return pd.DatetimeIndex(sorted(all_days))


# ---------- 3. 近月合约选择 ----------
def select_near_month(sym, contracts, calendar, global_cutoff):
    """对每个交易日选出目标近月合约 code（若当日无满足条件则返回 NaN）。"""
    # 每月各合约的到期日
    expiry = {code: contract_expiry(code, contracts, global_cutoff)
              for code in contracts}
    # 预取价格/成交量矩阵
    close = pd.DataFrame({code: contracts[code]["df"]["close"]
                          for code in contracts}).reindex(calendar)
    vol = pd.DataFrame({code: contracts[code]["df"]["volume"]
                        for code in contracts}).reindex(calendar)
    out = pd.Series(index=calendar, dtype=object)
    for dt in calendar:
        cand = []
        for code in contracts:
            c = close.loc[dt, code]
            v = vol.loc[dt, code]
            if pd.isna(c) or v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            if v <= 0:
                continue
            if (expiry[code] - dt) <= pd.Timedelta(days=5):
                continue
            cand.append((expiry[code], code))
        if not cand:
            out.loc[dt] = None
        else:
            cand.sort()
            out.loc[dt] = cand[0][1]
    return out, close, vol, expiry


# ---------- 4. 现货指数趋势均线性 ----------
def load_index():
    def _read(sheet):
        df = pd.read_excel(IDX_FILE, sheet_name=sheet, index_col=0)
        df.index = pd.to_datetime(df.index)
        return df["CLOSE"].astype(float)
    return {"IC": _read("000905.SH"), "IM": _read("000852.SH")}


def raw_strong(close, mas):
    """三均线多数表决或单均线。mas: [200] 或 [180,200,240]。
    返回0/1序列：>=2条(或唯一一条)判断指数>均线则为强。"""
    n = len(mas)
    pos = pd.DataFrame(index=close.index, dtype=float)
    for m in mas:
        ma = close.rolling(m).mean()
        pos[m] = (close > ma).astype(float)
    if n == 1:
        return pos.iloc[:, 0]
    return (pos.sum(axis=1) >= 2).astype(float)


def confirm_trend(raw, conf_days):
    """连续确认：状态连续 conf_days 天成立才切换，否则维持原状态。"""
    vals = raw.fillna(0).values
    state = np.zeros(len(vals))
    streak = 0
    cur = None
    for i, v in enumerate(vals):
        if cur is None:
            streak = 1
            cur = v
        elif v == cur:
            streak += 1
        else:
            streak = 1
            cur = v
        if streak >= conf_days:
            cur = v  # 状态官方定为当前值
        state[i] = cur
    return pd.Series(state, index=raw.index)


def build_trend(sym, index_close, calendar, mas):
    raw = raw_strong(index_close, mas).reindex(index_close.index)
    raw = raw.where(index_close.index.isin(calendar))
    return confirm_trend(raw, CONF_DAYS)  # 已定义在日历index内


# ---------- 5. 单品种近月净收益与换月标记 ----------
def product_return(sel, close):
    """第 t 日收益使用 t-1 选出的近月合约（移动前视）。返回净收益与换月标记。"""
    held = sel.shift(1)                 # 第 t 日持有 t-1 选出的合约
    idx = sel.index
    ret = pd.Series(np.nan, index=idx)
    close_held_prev = close.reindex(held.values)  # 当日持有合约序列
    # 逐日：close[held(t), t] / close[held(t), t-1] - 1
    for dt in idx:
        h = held.loc[dt]
        if pd.isna(h):
            continue
        i = idx.get_loc(dt)
        if i == 0:
            continue
        prev_dt = idx[i - 1]
        c_t = close.loc[dt, h]
        c_prev = close.loc[prev_dt, h]
        if pd.isna(c_t) or pd.isna(c_prev):
            continue
        ret.loc[dt] = c_t / c_prev - 1
    roll = (held != held.shift(1)).astype(float)   # 当日持有合约相对前一日变化
    return ret, roll


# ---------- 6. 主回测 ----------
def backtest(trend_params):
    ic_c, ic_meta = load_contracts("IC")
    im_c, im_meta = load_contracts("IM")
    calendar = build_calendar({**ic_c, **im_c})
    global_cutoff = calendar.max()

    sel_ic, close_ic, volmat_ic, exp_ic = select_near_month("IC", ic_c, calendar, global_cutoff)
    sel_im, close_im, volmat_im, exp_im = select_near_month("IM", im_c, calendar, global_cutoff)

    idx = load_index()

    # 趋势
    tre_ic = build_trend("IC", idx["IC"], calendar, trend_params)
    tre_im = build_trend("IM", idx["IM"], calendar, trend_params)

    # 品种净收益与换月
    ret_ic, roll_ic = product_return(sel_ic, close_ic)
    ret_im, roll_im = product_return(sel_im, close_im)

    # 60日波动率（近月净收益）
    vol60_ic = ret_ic.rolling(VOL60).std() * np.sqrt(ANNUAL)
    vol60_im = ret_im.rolling(VOL60).std() * np.sqrt(ANNUAL)
    med_ic = vol60_ic.rolling(MED_WIN, min_periods=MED_MIN).median()
    med_im = vol60_im.rolling(MED_WIN, min_periods=MED_MIN).median()
    lowvol_ic = (vol60_ic < med_ic).astype(bool)
    lowvol_im = (vol60_im < med_im).astype(bool)

    # 组合权重（信号滞后2日: 用 t-2 的指标给第 t 日收益）
    def weight_from(ts, ic_s, im_s):
        ic_strong = tre_ic.reindex(ts).astype(bool).fillna(False)
        im_strong = tre_im.reindex(ts).astype(bool).fillna(False)
        lv_ic = lowvol_ic.reindex(ts).astype(bool).fillna(False)
        lv_im = lowvol_im.reindex(ts).astype(bool).fillna(False)
        w_ic = np.zeros(len(ts)); w_im = np.zeros(len(ts))
        for i, dt in enumerate(ts):
            s_ic, s_im = ic_strong.iloc[i], im_strong.iloc[i]
            lvi, lvm = lv_ic.iloc[i], lv_im.iloc[i]
            if s_ic and s_im:
                if lvi and lvm:
                    w_ic[i] = 0.60; w_im[i] = 0.60
                else:
                    w_ic[i] = 0.50; w_im[i] = 0.50
            elif s_ic and not s_im:
                w_ic[i] = 0.75; w_im[i] = 0.25
            elif not s_ic and s_im:
                w_ic[i] = 0.25; w_im[i] = 0.75
            else:
                w_ic[i] = 0.125; w_im[i] = 0.125
        return pd.Series(w_ic, index=ts), pd.Series(w_im, index=ts)

    # 信号日期取 t-LAG（LAG=2 即文档的"滞后两个交易日"，LAG=0 为同日信号作对照）
    LAG = globals().get("LAG", 2)
    sig_days = calendar[:-LAG] if LAG > 0 else calendar
    sig_ic, sig_im = weight_from(sig_days, None, None)

    # 权重应用到第 t 日（映射：t-LAG日信号 -> t日收益）
    w_ic = pd.Series(index=calendar, dtype=float)
    w_im = pd.Series(index=calendar, dtype=float)
    for k, dt in enumerate(calendar):
        if k >= LAG:
            w_ic.loc[dt] = sig_ic.loc[calendar[k - LAG]] if LAG > 0 else sig_ic.loc[dt]
            w_im.loc[dt] = sig_im.loc[calendar[k - LAG]] if LAG > 0 else sig_im.loc[dt]
    w_ic = w_ic.ffill(); w_im = w_im.ffill()

    # 组合收益
    comb = (w_ic * ret_ic + w_im * ret_im)
    # 权重调整成本（组合权重变动）
    weight_cost = SINGLE_COST * (w_ic.diff().abs() + w_im.diff().abs())
    # 换月成本（近月合约切换，2×单边，按权重名义敞口计）
    roll_cost = 2 * SINGLE_COST * (w_ic * roll_ic + w_im * roll_im)
    net = comb - weight_cost - roll_cost

    # 评价期
    ev_w_ic = w_ic.loc[EVAL_START:EVAL_END]
    ev_w_im = w_im.loc[EVAL_START:EVAL_END]
    sub = net.loc[(net.index >= EVAL_START) & (net.index <= EVAL_END)]
    sub_cum = (1 + sub).cumprod()
    total = sub_cum.values[-1] - 1
    yrs = len(sub) / ANNUAL
    ann = (sub_cum.values[-1]) ** (1 / yrs) - 1
    ann_vol = sub.std() * np.sqrt(ANNUAL)
    sharpe = ann / ann_vol if ann_vol else np.nan

    # 基准：恒定50/50、1倍（含换月成本）
    base = (0.5*ret_ic + 0.5*ret_im) - 0.5*2*SINGLE_COST*(roll_ic + roll_im)
    base = base.loc[EVAL_START:EVAL_END]
    base_cum = (1 + base).cumprod().values[-1] - 1

    # 最大回撤及区间
    rollmax = sub_cum.cummax()
    dd = sub_cum / rollmax - 1
    mdd = dd.min()
    trough = dd.idxmin()
    peak = sub_cum.loc[:trough].idxmax()
    avg_exp = (w_ic.loc[EVAL_START:EVAL_END] + w_im.loc[EVAL_START:EVAL_END]).mean()
    max_exp = (w_ic.loc[EVAL_START:EVAL_END] + w_im.loc[EVAL_START:EVAL_END]).max()

    # 年均权重换手
    w_turn = (w_ic.diff().abs() + w_im.diff().abs()).loc[EVAL_START:EVAL_END].sum() / yrs

    return {
        "参数": trend_params,
        "年化收益率": ann, "累计收益率": total, "最大回撤": mdd,
        "年化波动率": ann_vol, "Sharpe": sharpe,
        "平均敞口": avg_exp, "最大敞口": max_exp, "年均权重换手": w_turn,
        "基准累计": base_cum,
        "回撤区": f"{peak:%Y-%m-%d}~{trough:%Y-%m-%d}",
        "交易日数": len(sub),
        "net收益序列": net,
        "回撤序列": dd,
    }


# ---------- 7. 运行两种趋势判断 ----------
def main():
    print(f"评价期: {EVAL_START:%Y-%m-%d} ~ {EVAL_END:%Y-%m-%d}  单边成本 {SINGLE_COST*10000:.0f}bp  信号滞后 {LAG} 日")
    print("=" * 96)
    res = {("200日均线",): backtest([200]),
           ("180/200/240多数表决",): backtest([180, 200, 240])}
    hdr = (f"{'趋势方式':<20}{'年化收益率':>10}{'累计收益率':>11}{'最大回撤':>10}"
           f"{'年化波动率':>11}{'Sharpe':>8}{'平均敞口':>9}{'年均换手':>9}")
    print(hdr)
    print("-" * 96)
    for label, r in res.items():
        lbl = label[0]
        print(f"{lbl:<20}"
              f"{r['年化收益率']*100:>9.2f}%{r['累计收益率']*100:>10.2f}%"
              f"{r['最大回撤']*100:>9.2f}%{r['年化波动率']*100:>10.2f}%"
              f"{r['Sharpe']:>9.2f}{r['平均敞口']:>10.2f}"
              f"{r['年均权重换手']:>9.2f}")
        print(f"   回撤区间: {r['回撤区']}   基准(50/50恒定)累计: {r['基准累计']*100:.2f}%")
    print("=" * 96)
    return res


if __name__ == "__main__":
    res = main()