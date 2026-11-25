# -*- coding: utf-8 -*-
"""
历史上的今天 —— 相似日经验分布：未来涨跌概率与空间推算引擎

思路
----
把"当前市场状态"拆成 8 个特征块（趋势/动量/波动/估值/情绪/量能/宏观/基差），
块内特征各自做滚动 z-score 标准化后合成块距离，再按块权重加权得到两点之间的距离。
在历史中找出与"今天"最像的 TopK 个交易日，用它们之后 h 个交易日的真实走势，
构造未来收益的条件经验分布 —— 上涨概率、期末涨跌分位、期间最大涨跌（MFE/MAE）、
逐日概率锥；再用 EWMA 标准化残差自举蒙特卡洛与（中证1000）期权隐含波动率做交叉验证。

严格避免未来函数：t 时点只使用 t 及之前的数据，候选日 s <= t-h 且 s <= t-25。
"""

import os
import re
import json
import math
import datetime as dt
import calendar

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DATA = os.path.join(ROOT, 'data')
CACHE = os.path.join(BASE, 'cache')

HORIZONS = [5, 10, 20, 60, 120, 240]
INDEXES = [
    ('000016.SH', '上证50', 'IH'),
    ('000300.SH', '沪深300', 'IF'),
    ('000905.SH', '中证500', 'IC'),
    ('000852.SH', '中证1000', 'IM'),
]
SB_NAME = {'上证50': '上证50', '沪深300': '沪深300', '中证500': '中证500', '中证1000': '中证1000'}

Z_WIN = 750          # 日频特征滚动标准化窗口
M_WIN = 60           # 月频特征滚动标准化窗口
CLIP = 3.0           # z 截断
TOPK = 200           # 邻居数量
DISPLAY_K = 50       # 图上示例路径条数（仅展示，不参与概率/分位计算）
DISPLAY_POOL = 1000  # 示例路径的候选池（按距离排序取前若干个）
DISPLAY_GAP = 20     # 示例路径两两之间至少间隔的交易日数
MIN_HIST = 500       # 至少多少根K线后才开始匹配
DECAY_HALFLIFE = 2000  # 时间衰减半衰期（交易日），越近的历史权重越高
SHRINK_K = 30        # 向无条件基准概率收缩的强度
DIST_NORMALIZE = True  # 各特征块距离先按自身尺度归一，使块权重真正可比
SCALE_CLIP = (0.35, 2.5)  # 历史收益按 σ今日/σ当时 缩放时的上下限（波动率制度归一）

BLOCK_W = {
    'trend': 0.30,
    'momentum': 0.12,
    'vol': 0.18,
    'valuation': 0.15,
    'sentiment': 0.10,
    'volume': 0.05,
    'macro': 0.05,
    'basis': 0.05,
}

# 数据新鲜度容忍（自然日）。超过则判定该块不可用，避免用陈旧数据冒充"当前状态"
STALE_DAYS = {'sentiment': 12, 'macro': 80, 'vol_iv': 12, 'basis': 12}

TENCENT_KLINE = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
                 '?param={sym},day,,,{n},qfq')


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def _beijing_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)


def _market_closed_today(now=None):
    """北京时间 15:05 之后才认为当日 K 线已收盘（留 5 分钟给数据源落库）。"""
    now = now or _beijing_now()
    if now.weekday() >= 5:
        return True
    return (now.hour, now.minute) >= (15, 5)


def _roll_z(s, win, min_periods=None):
    """滚动 z-score（只用历史，不含未来）：(x - 滚动均值) / 滚动标准差。"""
    mp = min_periods or max(60, win // 4)
    mu = s.rolling(win, min_periods=mp).mean()
    sd = s.rolling(win, min_periods=mp).std()
    z = (s - mu) / sd.replace(0, np.nan)
    return z.clip(-CLIP, CLIP) / CLIP


def _squash(x):
    """对已经是比率的特征做温和压缩，避免极端值主导距离。"""
    return np.tanh(x * 3.0)


def _num(v):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


def _r(v, nd=4):
    f = _num(v)
    return None if f is None else round(f, nd)


# ----------------------------------------------------------------------------
# 行情：xlsx 主表 + 腾讯日K补洞
# ----------------------------------------------------------------------------

def _fetch_tencent(sym, n=320):
    """返回 DataFrame[date, open, close, high, low, volume]，date 为 str。"""
    import requests
    url = TENCENT_KLINE.format(sym=sym, n=n)
    r = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    js = r.json()
    node = (js.get('data') or {}).get(sym) or {}
    rows = node.get('qfqday') or node.get('day') or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append({
            'date': str(row[0])[:10],
            'open': float(row[1]),
            'close': float(row[2]),
            'high': float(row[3]),
            'low': float(row[4]),
            'volume': float(row[5]),
        })
    return pd.DataFrame(out)


def load_price_patch():
    """增量拉取腾讯日K，写入 cache/price_patch.csv，返回全量补洞表。"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, 'price_patch.csv')
    old = pd.DataFrame()
    if os.path.exists(path):
        try:
            old = pd.read_csv(path, dtype={'code': str})
        except Exception:
            old = pd.DataFrame()
    if len(old) and 'date' in old:
        old['date'] = old['date'].astype(str).str.slice(0, 10)

    frames = [old] if len(old) else []
    syms = {code: ('sh' + code[:6]) for code, _, _ in INDEXES}
    for code, sym in syms.items():
        try:
            d = _fetch_tencent(sym)
        except Exception as e:
            print('  [warn] 腾讯行情拉取失败 %s: %s' % (sym, e))
            continue
        if not len(d):
            continue
        d['code'] = code
        frames.append(d)
    if not frames:
        return old

    new = pd.concat(frames, ignore_index=True)
    new = new.dropna(subset=['date', 'close'])
    new = new.drop_duplicates(subset=['code', 'date'], keep='last')
    new = new.sort_values(['code', 'date']).reset_index(drop=True)
    new = new[['code', 'date', 'open', 'high', 'low', 'close', 'volume']]
    # 未收盘的当日 bar 一律剔除，且不落盘（避免未完成日内数据进入缓存）
    if not _market_closed_today():
        today = _beijing_now().strftime('%Y-%m-%d')
        new = new[new['date'] != today].reset_index(drop=True)
    new.to_csv(path, index=False, encoding='utf-8')
    return new


def load_index_frame(code):
    """xlsx 主表 + 腾讯补洞 → 统一列名的小写 DataFrame。"""
    raw = pd.read_excel(os.path.join(DATA, '指数行情序列.xlsx'), sheet_name=code)
    raw['date'] = pd.to_datetime(raw['Unnamed: 0'], errors='coerce')
    raw = raw.dropna(subset=['date']).sort_values('date')
    df = pd.DataFrame({
        'date': raw['date'].dt.strftime('%Y-%m-%d').values,
        'open': pd.to_numeric(raw['OPEN'], errors='coerce').values,
        'high': pd.to_numeric(raw['HIGH'], errors='coerce').values,
        'low': pd.to_numeric(raw['LOW'], errors='coerce').values,
        'close': pd.to_numeric(raw['CLOSE'], errors='coerce').values,
        'volume': pd.to_numeric(raw['VOLUME'], errors='coerce').values,
        'pb': pd.to_numeric(raw['PB_MRQ'], errors='coerce').values,
        'pe': pd.to_numeric(raw['PE_TTM'], errors='coerce').values,
    })
    return df.dropna(subset=['close']).reset_index(drop=True)


def merge_patch(df, patch, code):
    """把腾讯补洞数据并入主表；成交量按重叠期比例折算到 xlsx 口径。"""
    if patch is None or not len(patch):
        return df
    p = patch[patch['code'] == code].copy()
    if not len(p):
        return df
    p['date'] = p['date'].astype(str).str.slice(0, 10)
    last = df['date'].iloc[-1]
    # 成交量口径对齐：用最近 30 个重叠交易日的比例（xlsx 早期与近期单位可能不一致）
    ov = p.merge(df[['date', 'volume']], on='date', suffixes=('_tx', '_xl'))
    ov = ov[(ov['volume_xl'] > 0) & (ov['volume_tx'] > 0)].tail(30)
    scale = float((ov['volume_xl'] / ov['volume_tx']).median()) if len(ov) >= 5 else 1.0
    tail = p[p['date'] > last].copy()
    if not len(tail):
        return df
    if scale:
        tail['volume'] = tail['volume'] * scale
    tail['pb'] = np.nan
    tail['pe'] = np.nan
    tail = tail[['date', 'open', 'high', 'low', 'close', 'volume', 'pb', 'pe']]
    out = pd.concat([df, tail], ignore_index=True)
    out = out.drop_duplicates(subset=['date'], keep='last').sort_values('date')
    return out.reset_index(drop=True)


# ----------------------------------------------------------------------------
# 辅助数据：股债利差 / 两融 / 波指 / 宏观 / 期货基差 / 隐含波动率
# ----------------------------------------------------------------------------

def _parse_js_var(path, varname):
    txt = open(path, encoding='utf-8').read()
    m = re.search(r'window\.' + varname + r'\s*=\s*(\{.*\})\s*;?\s*$', txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def load_stock_bond():
    obj = _parse_js_var(os.path.join(DATA, 'stock_bond_data.js'), 'STOCK_BOND_DATA') or {}
    out = {}
    for name, v in obj.items():
        dates = pd.to_datetime(pd.Series(v.get('dates', [])), errors='coerce')
        s = pd.Series(pd.to_numeric(pd.Series(v.get('stock_bond_spread', [])), errors='coerce').values,
                      index=dates.values)
        out[name] = s[~s.index.isna()].sort_index()
    return out


def _pick_col(df, names, contains=()):
    """按候选列名/关键字找列，找不到返回 None。"""
    for c in df.columns:
        if str(c).strip() in names:
            return c
    for c in df.columns:
        s = str(c)
        if any(k in s for k in contains):
            return c
    return None


def load_margin():
    path = os.path.join(DATA, '情绪数据序列.xlsx')
    try:
        x = pd.read_excel(path, sheet_name='两融')
    except Exception as e:
        print('  [warn] 两融数据读取失败: %s' % e)
        return None
    dc = _pick_col(x, ('日期', 'date', 'Unnamed: 0', '交易日期'))
    bc = _pick_col(x, ('融资余额',), ('融资余额',))
    nc = _pick_col(x, ('融资净买入_20日均', '融资净买入'), ('融资净买入',))
    if dc is None or bc is None:
        print('  [warn] 两融数据缺少日期或融资余额列')
        return None
    d = pd.DataFrame({
        'date': pd.to_datetime(x[dc], errors='coerce'),
        'balance': pd.to_numeric(x[bc], errors='coerce'),
    })
    if nc is not None:
        s = pd.to_numeric(x[nc], errors='coerce')
        if '20日' not in str(nc):
            s = s.rolling(20, min_periods=5).mean()
        d['net20'] = s
    else:
        d['net20'] = np.nan
    d = d.dropna(subset=['date']).sort_values('date')
    d = d.drop_duplicates(subset=['date'], keep='last')
    return d.set_index('date')


def load_qvix():
    try:
        x = pd.read_excel(os.path.join(DATA, '情绪数据序列.xlsx'), sheet_name='中国波指')
    except Exception as e:
        print('  [warn] 波指数据读取失败: %s' % e)
        return None
    dc = _pick_col(x, ('date', '日期', 'Unnamed: 0'))
    vc = _pick_col(x, ('中国波指',), ('波指', 'QVIX'))
    if dc is None or vc is None:
        return None
    d = pd.DataFrame({
        'date': pd.to_datetime(x[dc], errors='coerce'),
        'qvix': pd.to_numeric(x[vc], errors='coerce'),
    }).dropna(subset=['date']).sort_values('date')
    # 数据源偶发同一天重复行（akshare 抓取拼接时会出现），去重后再建索引
    d = d.drop_duplicates(subset=['date'], keep='last')
    return d.set_index('date')['qvix']


def load_macro():
    path = os.path.join(DATA, '宏观数据序列.xlsx')
    try:
        m = pd.read_excel(path, sheet_name='货币供应量')
        m1 = pd.DataFrame({'date': pd.to_datetime(m['统计时间'], errors='coerce'),
                           'm1m2': pd.to_numeric(m['M1同比'], errors='coerce') -
                                   pd.to_numeric(m['M2同比'], errors='coerce')}).dropna(subset=['date'])
        pm = pd.read_excel(path, sheet_name='PMI')
        pmi = pd.DataFrame({'date': pd.to_datetime(pm['月份'], errors='coerce'),
                            'pmi': pd.to_numeric(pm['制造业PMI'], errors='coerce')}).dropna(subset=['date'])
        cp = pd.read_excel(path, sheet_name='PPI')
        ppi = pd.DataFrame({'date': pd.to_datetime(cp['月份'], errors='coerce'),
                            'ppi': pd.to_numeric(cp['PPI同比'], errors='coerce')}).dropna(subset=['date'])
        ci = pd.read_excel(path, sheet_name='CPI')
        cpi = pd.DataFrame({'date': pd.to_datetime(ci['月份'], errors='coerce'),
                            'cpi': pd.to_numeric(ci['CPI同比'], errors='coerce')}).dropna(subset=['date'])
        out = m1.merge(pmi, on='date', how='outer').merge(ppi, on='date', how='outer')
        out = out.merge(cpi, on='date', how='outer').sort_values('date')
        out['ppi_cpi'] = out['ppi'] - out['cpi']
        return out.set_index('date')[['m1m2', 'pmi', 'ppi_cpi']]
    except Exception as e:
        print('  [warn] 宏观数据读取失败: %s' % e)
        return None


def load_iv():
    obj = _parse_js_var(os.path.join(DATA, 'iv_data.js'), 'IV_DATA') or {}
    dates = pd.to_datetime(pd.Series(obj.get('dates', [])), errors='coerce')
    iv = pd.Series(pd.to_numeric(pd.Series(obj.get('iv30', [])), errors='coerce').values,
                   index=dates.values)
    return iv[~iv.index.isna()].sort_index()


def _third_friday(year, month):
    fridays = [w[calendar.FRIDAY] for w in calendar.monthcalendar(year, month)
               if w[calendar.FRIDAY]]
    return pd.Timestamp(year, month, fridays[2])


def _next_trading_day(base, trade_dates):
    """到期日遇非交易日时，顺延到交易日历中的下一个交易日。"""
    if not len(trade_dates):
        out = base
        while out.weekday() >= 5:
            out += pd.Timedelta(days=1)
        return out
    i = int(trade_dates.searchsorted(base, side='left'))
    if i < len(trade_dates):
        return trade_dates[i]
    out = base
    while out.weekday() >= 5:
        out += pd.Timedelta(days=1)
    return out


def load_futures():
    path = os.path.join(DATA, 'v6_futures.csv')
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path)
    d['product'] = d['contract'].astype(str).str[:2]
    d['ym'] = d['contract'].astype(str).str[2:]
    d['date'] = pd.to_datetime(d['date'], errors='coerce')
    d = d.dropna(subset=['date', 'close'])
    trade_dates = pd.DatetimeIndex(sorted(d['date'].dropna().unique()))
    expiry = {}
    for code in d['contract'].astype(str).dropna().unique():
        mt = re.match(r'^(?:IH|IF|IC|IM)(\d{2})(\d{2})$', code)
        if not mt:
            continue
        year, month = 2000 + int(mt.group(1)), int(mt.group(2))
        base = _third_friday(year, month)
        expiry[code] = _next_trading_day(base, trade_dates)
    d['exp'] = d['contract'].astype(str).map(expiry)
    return d.dropna(subset=['exp'])

def _lagged_daily(mser, dates, win=M_WIN, lag_days=45, max_ffill=75, min_periods=24):
    """月频 → 日频：滚动 z（60期）→ 滞后 lag_days 天 → 前值填充（超过 max_ffill 置空）。"""
    z = _roll_z(mser.astype(float), win, min_periods=min_periods)
    idx = pd.DatetimeIndex(z.index) + pd.Timedelta(days=lag_days)
    s = pd.Series(z.values, index=idx).sort_index()
    s = s[~s.index.duplicated(keep='last')].dropna()
    dd = pd.DatetimeIndex(pd.to_datetime(dates))
    if not len(s):
        return pd.Series(np.nan, index=dd)
    allidx = s.index.union(dd)
    val = s.reindex(allidx).ffill().reindex(dd)
    stamp = pd.Series(allidx.where(allidx.isin(s.index)), index=allidx)
    last = stamp.ffill().reindex(dd)
    age = (dd - pd.DatetimeIndex(last)).days.astype(float)
    return pd.Series(np.asarray(val, dtype=float), index=dd).where(age <= max_ffill)


def _daily_join(ser, dates):
    """日频源序列 → 按交易日对齐，不填充（数据结束后即 NaN）。"""
    dd = pd.DatetimeIndex(pd.to_datetime(dates))
    ser = ser[~ser.index.duplicated(keep='last')]
    return pd.Series(np.asarray(ser.reindex(ser.index.union(dd)).reindex(dd), dtype=float), index=dd)


def _daily_ffill(ser, dates, max_days=5):
    """日频源序列 → 按交易日对齐，允许最多 max_days 个自然日的前值填充。"""
    dd = pd.DatetimeIndex(pd.to_datetime(dates))
    s = ser.dropna()
    s = s[~s.index.duplicated(keep='last')]
    if not len(s):
        return pd.Series(np.nan, index=dd)
    idx = s.index.union(dd)
    val = s.reindex(idx).ffill().reindex(dd)
    # 记录"最近一次真实观测日"，超过容忍窗口就置空，防止陈旧数据冒充当前状态
    stamp = pd.Series(idx.where(idx.isin(s.index)), index=idx)
    last = stamp.ffill().reindex(dd)
    age = (dd - pd.DatetimeIndex(last)).days.astype(float)
    return pd.Series(np.asarray(val, dtype=float), index=dd).where(age <= max_days)


def build_context():
    """一次性加载所有共享数据源。"""
    ctx = {}
    ctx['patch'] = load_price_patch()
    ctx['sb'] = load_stock_bond()
    ctx['margin'] = load_margin()
    ctx['qvix'] = load_qvix()
    ctx['macro'] = load_macro()
    ctx['iv'] = load_iv()
    ctx['fut'] = load_futures()
    return ctx


def _basis_series(ctx, product, nav_dates, spot_close):
    """最近未到期合约的年化基差（%），按交易日对齐。"""
    fut = ctx.get('fut')
    if fut is None or not len(fut):
        return pd.Series(np.nan, index=pd.DatetimeIndex(pd.to_datetime(nav_dates)))
    f = fut[fut['product'] == product]
    if not len(f):
        return pd.Series(np.nan, index=pd.DatetimeIndex(pd.to_datetime(nav_dates)))
    dd = pd.DatetimeIndex(pd.to_datetime(nav_dates))
    spot = pd.Series(np.asarray(spot_close, dtype=float), index=dd)
    out = {}
    for d, grp in f.groupby('date'):
        live = grp[grp['exp'] >= d + pd.Timedelta(days=3)]
        if not len(live):
            continue
        row = live.loc[live['exp'].idxmin()]
        s = spot.get(pd.Timestamp(d), np.nan)
        if not s or not np.isfinite(s):
            continue
        days = max((row['exp'] - d).days, 5)
        out[pd.Timestamp(d)] = (float(row['close']) / float(s) - 1.0) * 365.0 / days * 100.0
    ser = pd.Series(out).sort_index()
    return _daily_join(ser, nav_dates)


def _descale_volume(v):
    """成交量单位可能在数据源切换时跳变（10/100 倍），把前段折算到后段口径。"""
    v = np.asarray(v, dtype=float).copy()
    n = len(v)
    for _ in range(3):
        best, bj = 1.0, -1
        for j in range(60, max(n - 20, 61), 5):
            a = np.nanmedian(v[j - 20:j])
            b = np.nanmedian(v[j:j + 20])
            if a > 0 and b > 0:
                r = b / a
                if (r > 5 or r < 0.2) and abs(np.log(r)) > abs(np.log(best)):
                    best, bj = r, j
        if bj < 0:
            break
        v[:bj] *= best
    return v


def build_features(code, name, product, df, ctx):
    """构造单个指数的特征块（块内特征已标准化到 [-1,1]）。"""
    d = df.reset_index(drop=True)
    dates = pd.DatetimeIndex(pd.to_datetime(d['date']))
    c = pd.Series(np.asarray(d['close'], dtype=float), index=dates)
    hi = pd.Series(np.asarray(d['high'], dtype=float), index=dates)
    lo = pd.Series(np.asarray(d['low'], dtype=float), index=dates)
    vol = pd.Series(_descale_volume(d['volume'].values), index=dates)
    pb = pd.Series(np.asarray(d['pb'], dtype=float), index=dates)
    logret = np.log(c).diff()

    def Z(s):
        return _roll_z(s.astype(float), Z_WIN)

    blocks = {}

    ma20 = c.rolling(20, min_periods=15).mean()
    ma60 = c.rolling(60, min_periods=40).mean()
    ma200 = c.rolling(200, min_periods=120).mean()
    blocks['trend'] = pd.DataFrame({
        'ma20': Z(c / ma20 - 1.0),
        'ma60': Z(c / ma60 - 1.0),
        'ma200': Z(c / ma200 - 1.0),
        'ma60_slope': Z(ma60 / ma60.shift(20) - 1.0),
    }, index=dates)

    blocks['momentum'] = pd.DataFrame({
        'r20': Z(c / c.shift(20) - 1.0),
        'r60': Z(c / c.shift(60) - 1.0),
    }, index=dates)

    rv5 = logret.rolling(5, min_periods=4).std() * np.sqrt(252)
    rv20 = logret.rolling(20, min_periods=15).std() * np.sqrt(252)
    rv60 = logret.rolling(60, min_periods=40).std() * np.sqrt(252)
    volcols = {
        'rv20': Z(rv20),
        'vol_ratio': Z(rv5 / rv60.replace(0, np.nan)),
    }
    if name == '中证1000' and ctx['iv'] is not None and len(ctx['iv']):
        volcols['iv30'] = Z(_daily_ffill(ctx['iv'], dates, 7))
    blocks['vol'] = pd.DataFrame(volcols, index=dates)

    sb = ctx['sb'].get(SB_NAME[name])
    sbz = (_lagged_daily(sb, dates, lag_days=45, max_ffill=45)
           if sb is not None and len(sb) > 24 else pd.Series(np.nan, index=dates))
    pbz = (
        _lagged_daily(pb.dropna(), dates, win=M_WIN, lag_days=45,
                      max_ffill=80, min_periods=24)
        if pb.notna().sum() > 24 else pd.Series(np.nan, index=dates)
    )
    pe = pd.Series(np.asarray(d['pe'], dtype=float), index=dates)
    # PE_TTM 为月频快照（约 260 个观测），按 60 期滚动标准化并滞后发布
    pez = (_lagged_daily(pe.dropna(), dates, win=M_WIN, lag_days=45, max_ffill=80, min_periods=24)
           if pe.notna().sum() > 24 else pd.Series(np.nan, index=dates))
    blocks['valuation'] = pd.DataFrame({'pb': pbz, 'pe': pez, 'spread': sbz}, index=dates)

    mg = ctx['margin']
    if mg is not None and len(mg):
        bal = _daily_ffill(mg['balance'], dates, 15)
        net = _daily_ffill(mg['net20'], dates, 15)
        sent = {
            'margin_chg': Z(bal / bal.shift(20) - 1.0),
            'margin_flow': Z(net / bal.replace(0, np.nan)),
        }
    else:
        sent = {}
    if ctx['qvix'] is not None and len(ctx['qvix']):
        sent['qvix'] = Z(_daily_ffill(ctx['qvix'], dates, 15))
    blocks['sentiment'] = pd.DataFrame(sent, index=dates) if sent else pd.DataFrame(index=dates)

    blocks['volume'] = pd.DataFrame({
        'vol_ratio': Z(vol.rolling(5, min_periods=4).mean() /
                       vol.rolling(60, min_periods=40).mean().replace(0, np.nan)),
    }, index=dates)

    mac = ctx['macro']
    macf = {}
    if mac is not None and len(mac):
        for col, key in (('m1m2', 'm1m2'), ('pmi', 'pmi'), ('ppi_cpi', 'ppa_cpi')):
            s = mac[col].dropna()
            if len(s) > 24:
                macf[key] = _lagged_daily(s, dates, win=M_WIN, lag_days=45, max_ffill=STALE_DAYS['macro'])
    blocks['macro'] = pd.DataFrame(macf, index=dates) if macf else pd.DataFrame(index=dates)

    blocks['basis'] = pd.DataFrame({
        'basis': Z(_daily_ffill(_basis_series(ctx, product, dates, d['close'].values), dates, 5)),
    }, index=dates)

    last_obs = {}
    for k, bdf in blocks.items():
        if bdf is None or not bdf.shape[1]:
            last_obs[k] = None
            continue
        anyv = np.isfinite(bdf.values).any(axis=1)
        last_obs[k] = dates[np.where(anyv)[0].max()].strftime('%Y-%m-%d') if anyv.any() else None
    # 用数据源的真实观测日覆盖（前值填充会把最近有效日掩盖成今天）
    if mg is not None and len(mg):
        src = [mg.index.max()]
        if ctx['qvix'] is not None and len(ctx['qvix']):
            src.append(ctx['qvix'].index.max())
        last_obs['sentiment'] = max(src).strftime('%Y-%m-%d')
    if ctx['fut'] is not None and len(ctx['fut']):
        fs = ctx['fut'][ctx['fut']['product'] == product]['date']
        if len(fs):
            last_obs['basis'] = fs.max().strftime('%Y-%m-%d')

    data_status = {
        'price_last': dates[-1].strftime('%Y-%m-%d'),
        'margin_last': mg.index.max().strftime('%Y-%m-%d') if mg is not None and len(mg) else None,
        'qvix_last': ctx['qvix'].index.max().strftime('%Y-%m-%d') if ctx['qvix'] is not None and len(ctx['qvix']) else None,
        'macro_last': mac.index.max().strftime('%Y-%m-%d') if mac is not None and len(mac) else None,
    }
    if ctx['fut'] is not None and len(ctx['fut']):
        _fs = ctx['fut'][ctx['fut']['product'] == product]['date']
        data_status['futures_last'] = _fs.max().strftime('%Y-%m-%d') if len(_fs) else None

    return {
        'code': code, 'name': name, 'product': product,
        'dates': [x.strftime('%Y-%m-%d') for x in dates],
        'close': np.asarray(c.values, dtype=float),
        'high': np.asarray(hi.values, dtype=float),
        'low': np.asarray(lo.values, dtype=float),
        'blocks': blocks,
        'block_last': last_obs,
        'data_status': data_status,
        'sigma20': np.asarray(rv20.values, dtype=float),
        'logret': np.asarray(logret.values, dtype=float),
    }

def _matrices(feat):
    out = {}
    for k, d in feat['blocks'].items():
        if d is None or d.shape[1] == 0:
            continue
        arr = np.asarray(d.values, dtype=float)
        if np.isfinite(arr).sum() == 0:
            continue
        out[k] = arr
    return out


def _block_dists(mats, t):
    """返回 {block: (d, ok)}，块内按双方都非缺失的特征取均值平方差。"""
    out = {}
    n = next(iter(mats.values())).shape[0]
    for k, X in mats.items():
        w = BLOCK_W.get(k, 0.0)
        row = X[t]
        okf = np.isfinite(row)
        if not okf.any() or w <= 0:
            continue
        sub = X[:, okf]
        v = row[okf]
        m = np.isfinite(sub)
        diff = np.where(m, sub - v, 0.0)
        cnt = m.sum(axis=1)
        d = (diff ** 2).sum(axis=1) / np.maximum(cnt, 1)
        # 至少覆盖当前可用特征的大部分，避免仅靠一个特征
        # 就进入最近邻排序，导致不同历史日的距离不可比。
        min_cnt = max(1, int(np.ceil(0.6 * int(okf.sum()))))
        ok = (cnt >= min_cnt) & np.isfinite(d)
        out[k] = (d, ok)
    return out


def _distances(mats, t, normalize=False, cut=None):
    """t 与全部历史日的加权块距离。normalize=True 时先按各块自身尺度归一。"""
    bd = _block_dists(mats, t)
    if not bd:
        n = next(iter(mats.values())).shape[0]
        return np.full(n, np.nan)
    n = len(next(iter(bd.values()))[0])
    num = np.zeros(n)
    den = np.zeros(n)
    for k, (d, ok) in bd.items():
        w = BLOCK_W.get(k, 0.0)
        if normalize:
            m = ok & (np.arange(n) <= (cut if cut is not None else t - 25))
            scale = float(np.median(d[m])) if m.any() else 1.0
            if not np.isfinite(scale) or scale <= 0:
                scale = 1.0
            d = d / scale
        num[ok] += w * d[ok]
        den[ok] += w
    dist = np.full(n, np.nan)
    good = den > 0
    dist[good] = num[good] / den[good]
    return dist


def _wquant(vals, w, qs):
    vals = np.asarray(vals, dtype=float)
    w = np.asarray(w, dtype=float)
    ok = np.isfinite(vals) & np.isfinite(w) & (w > 0)
    if ok.sum() == 0:
        return [None] * len(qs)
    v = vals[ok]
    ww = w[ok]
    o = np.argsort(v)
    v, ww = v[o], ww[o]
    cw = np.cumsum(ww)
    if cw[-1] <= 0:
        return [None] * len(qs)
    cw = cw / cw[-1]
    return [float(np.interp(q, cw, v)) for q in qs]


def _fit_weights(dist, t, h, close_len):
    """在候选历史日中取 TopK，返回 (索引, 距离, 权重, 有效样本数)。"""
    n = len(dist)
    cut = t - max(25, h)
    cand = np.arange(n)
    ok = (cand <= cut) & np.isfinite(dist) & (dist > -1)
    cand = cand[ok]
    if len(cand) == 0:
        return None
    d = dist[cand]
    order = np.argsort(d)
    cand = cand[order]
    d = d[order]
    k = min(TOPK, len(cand))
    sel = cand[:k]
    ds = d[:k]
    base = np.median(d[:min(300, len(d))])
    if not np.isfinite(base) or base <= 0:
        base = max(float(np.median(ds)), 1e-6)
    w = np.exp(-ds / (2.0 * base))
    w = w * np.power(0.5, (t - sel) / float(DECAY_HALFLIFE))
    if w.sum() <= 0:
        w = np.ones(len(sel))
    w = w / w.sum()
    n_eff = 1.0 / float(np.sum(w ** 2))
    return sel, ds, w, n_eff


def _block_neighbors(mats, t, gap=20, k=30):
    """各特征块各自的最近历史日（仅用于画图，不参与概率计算）。

    返回 {block: [(idx, 块距离), ...]}，块距离为块内特征 z 差的均方根。
    """
    out = {}
    for blk, (d, ok) in _block_dists(mats, t).items():
        cand = np.where(ok & (np.arange(len(d)) <= t - 25))[0]
        if not len(cand):
            continue
        cand = cand[np.argsort(d[cand])]
        keep = []
        for s in cand:
            if all(abs(int(s) - int(x)) >= gap for x in keep):
                keep.append(int(s))
                if len(keep) >= k:
                    break
        out[blk] = [(s, float(math.sqrt(max(d[s], 0.0)))) for s in keep]
    return out


def _display_idx(dist, t, h, n):
    """展示用示例路径：在更大的候选池里按距离排序，贪心保留两两间隔>=DISPLAY_GAP 的日期。

    只影响图上画哪几条灰线；概率、分位、赔率等仍只用 TopK 加权邻居。
    """
    cand = np.arange(n)
    ok = (cand <= t - max(25, h)) & np.isfinite(dist) & (dist > -1)
    cand = cand[ok]
    if not len(cand):
        return np.array([], dtype=int)
    d = dist[cand]
    cand = cand[np.argsort(d)][:min(DISPLAY_POOL, len(cand))]
    keep = []
    for s in cand:
        if all(abs(int(s) - int(k)) >= DISPLAY_GAP for k in keep):
            keep.append(int(s))
            if len(keep) >= DISPLAY_K:
                break
    return np.array(keep, dtype=int)


def _mc_forward(logret, h, n_sim=3000, lam=0.94, seed=20260101):
    """EWMA 标准化残差自举蒙特卡洛（零漂移）。"""
    r = np.asarray(logret, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 250:
        return None
    r = r[-1500:]
    var = np.empty(len(r))
    var[0] = np.var(r[:60]) if len(r) >= 60 else np.var(r)
    for i in range(1, len(r)):
        var[i] = lam * var[i - 1] + (1 - lam) * r[i - 1] ** 2
    sig_prev = np.sqrt(np.maximum(var, 1e-12))
    z = r / sig_prev
    z = z[np.isfinite(z)]
    sig_now = float(np.sqrt(max(var[-1], 1e-12)))
    rng = np.random.default_rng(seed)
    zs = rng.choice(z, size=(n_sim, h), replace=True)
    paths = np.cumsum(sig_now * zs, axis=1)
    tot = paths[:, -1]
    out = {
        'p_up': float(np.mean(tot > 0)),
        'q10': float(np.quantile(tot, 0.10)),
        'q50': float(np.quantile(tot, 0.50)),
        'q90': float(np.quantile(tot, 0.90)),
        'sigma': sig_now * np.sqrt(h),
        'n_sim': n_sim,
    }
    return out


def _iv_check(iv_series, dates, h, t):
    if iv_series is None or not len(iv_series):
        return None
    s = iv_series[iv_series.index <= dates[t]]
    if not len(s):
        return None
    age = (dates[t] - s.index[-1]).days
    if age > STALE_DAYS['vol_iv']:
        return None
    iv = float(s.iloc[-1])
    if not np.isfinite(iv) or iv <= 0:
        return None
    sd = iv / 100.0 * np.sqrt(h / 252.0)
    return {
        'iv': round(iv, 2),
        'sigma_h': round(sd, 4),
        'q10': round(-1.2816 * sd, 4),
        'q90': round(1.2816 * sd, 4),
        'date': s.index[-1].strftime('%Y-%m-%d'),
    }


def analyze(feat, mats, t, horizons=HORIZONS, detail=False, iv_series=None,
            do_mc=True, base_lookback=None):
    """在 t 时点给出各期限的概率/空间估计。"""
    dates = pd.DatetimeIndex(pd.to_datetime(feat['dates']))
    close = feat['close']
    high = feat['high']
    low = feat['low']
    sig = feat['sigma20']
    n = len(close)
    dist = _distances(mats, t, normalize=DIST_NORMALIZE, cut=t - 25)
    sig_t = sig[t] if np.isfinite(sig[t]) else np.nan
    out = {}

    for h in horizons:
        fw = _fit_weights(dist, t, h, n)
        if fw is None:
            continue
        sel, ds, w, n_eff = fw
        # 基准概率：截至 t 的无条件上涨频率
        hist_end = t
        base_s = np.arange(0, max(hist_end - h, 0))
        if base_lookback:
            base_s = base_s[-base_lookback:]
        if len(base_s):
            bret = close[np.minimum(base_s + h, n - 1)] / close[base_s] - 1.0
            p_base = float(np.mean(bret > 0))
        else:
            p_base = 0.5

        ret = close[np.minimum(sel + h, n - 1)] / close[sel] - 1.0
        # 波动率归一：用今天的波动率重新标定历史结果
        scale = np.where(np.isfinite(sig[sel]) & (sig[sel] > 1e-6) & np.isfinite(sig_t),
                         np.clip(sig_t / sig[sel], SCALE_CLIP[0], SCALE_CLIP[1]), 1.0)
        ret_adj = ret * scale

        p_raw = float(np.sum(w * (ret_adj > 0)))
        # 重叠窗口的邻居样本并不独立：按 h 折算独立样本量，长周期更依赖基准概率
        n_indep = max(n_eff * min(1.0, 20.0 / float(h)), 1.0)
        lam = SHRINK_K / (SHRINK_K + n_indep)
        p_up = (1 - lam) * p_raw + lam * p_base
        a = p_up * n_indep + 1.0
        b = (1 - p_up) * n_indep + 1.0
        sd_p = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))

        qs = _wquant(ret_adj, w, [0.10, 0.25, 0.50, 0.75, 0.90])
        up_mask = ret_adj > 0
        dn_mask = ret_adj < 0
        up_mean = float(np.sum(w[up_mask] * ret_adj[up_mask]) / np.sum(w[up_mask])) if up_mask.any() else None
        dn_mean = float(np.sum(w[dn_mask] * ret_adj[dn_mask]) / np.sum(w[dn_mask])) if dn_mask.any() else None
        odds = (abs(up_mean) / abs(dn_mean)) if (up_mean and dn_mean and dn_mean != 0) else None

        mfe = np.array([high[min(s + 1, n - 1):min(s + h, n - 1) + 1].max() / close[s] - 1.0 for s in sel]) * scale
        mae = np.array([low[min(s + 1, n - 1):min(s + h, n - 1) + 1].min() / close[s] - 1.0 for s in sel]) * scale
        mfe_q = _wquant(mfe, w, [0.25, 0.50, 0.75, 0.90])
        mae_q = _wquant(mae, w, [0.25, 0.50, 0.75, 0.10])

        item = {
            'p_up': round(p_up, 4),
            'p_raw': round(p_raw, 4),
            'p_base': round(p_base, 4),
            'ci90': [round(max(p_up - 1.645 * sd_p, 0.0), 4),
                     round(min(p_up + 1.645 * sd_p, 1.0), 4)],
            'n_eff': round(n_eff, 1),
            'exp_ret': round(float(np.sum(w * ret_adj)), 4),
            'q10': _r(qs[0]), 'q25': _r(qs[1]), 'q50': _r(qs[2]),
            'q75': _r(qs[3]), 'q90': _r(qs[4]),
            'up_mean': _r(up_mean), 'down_mean': _r(dn_mean), 'odds': _r(odds, 2),
            'mfe': [_r(x) for x in mfe_q], 'mae': [_r(x) for x in mae_q],
            'win_rate': round(p_raw, 4),
        }

        if detail:
            cnt, edges = np.histogram(ret_adj, bins=21, weights=w)
            item['hist'] = {
                'edges': [round(float(x), 4) for x in edges],
                'w': [round(float(x), 5) for x in cnt],
            }
            fan = []
            for j in range(1, h + 1):
                pth = close[np.minimum(sel + j, n - 1)] / close[sel] - 1.0
                fan.append({'d': j, 'q': [_r(x) for x in _wquant(pth, w, [0.10, 0.25, 0.50, 0.75, 0.90])]})
            item['fan'] = fan
            nb = np.argsort(-w)[:12]
            item['neighbors'] = [{
                'date': dates[sel[i]].strftime('%Y-%m-%d'),
                'dist': _r(ds[i]),
                'w': _r(w[i], 4),
                'ret': _r(ret[i]),
                'path': [_r(close[min(sel[i] + j, n - 1)] / close[sel[i]] - 1.0) for j in range(0, h + 1)],
            } for i in nb]
            # 图上画的灰线：从更大候选池里挑时间分散的 DISPLAY_K 条（不影响上面的数值）
            item['disp'] = [{
                'date': dates[s].strftime('%Y-%m-%d'),
                'path': [_r(close[min(s + j, n - 1)] / close[s] - 1.0) for j in range(0, h + 1)],
            } for s in _display_idx(dist, t, h, n)]

        cross = {}
        if do_mc:
            mc = _mc_forward(feat['logret'][:t + 1], h)
            if mc:
                cross['mc'] = {
                    'p_up': round(mc['p_up'], 4),
                    'q10': _r(mc['q10']), 'q50': _r(mc['q50']), 'q90': _r(mc['q90']),
                    'sigma': _r(mc['sigma']),
                }
        iv = _iv_check(iv_series, dates, h, t)
        if iv:
            cross['iv'] = iv
        if cross:
            item['cross'] = cross
        out[str(h)] = item
    return out

def _reliability(p, real, edges=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
    rows = []
    for i in range(len(edges) - 1):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < len(edges) - 2 else p <= 1.0)
        k = int(m.sum())
        rows.append({
            'range': [edges[i], edges[i + 1]],
            'n': k,
            'p': round(float(p[m].mean()), 4) if k else None,
            'real': round(float(real[m].mean()), 4) if k else None,
        })
    return rows


def backtest(feat, mats, start='2016-01-04', step=5, horizons=HORIZONS):
    """滚动回测：只用 t 时点可见信息，逐期评估概率校准与区间覆盖。"""
    dates = pd.DatetimeIndex(pd.to_datetime(feat['dates']))
    close = feat['close']
    n = len(close)
    i0 = int(np.searchsorted(dates, pd.Timestamp(start)))
    i0 = max(i0, MIN_HIST)
    idxs = list(range(i0, n, step))

    res = {'dates': [], 'h': {}}
    store = {h: {'p': [], 'real': [], 'base': [], 'lo': [], 'hi': [], 'rr': []} for h in horizons}
    for t in idxs:
        if t < MIN_HIST:
            continue
        pred = analyze(feat, mats, t, horizons=horizons, do_mc=False)
        if not pred:
            continue
        res['dates'].append(feat['dates'][t])
        for h in horizons:
            it = pred.get(str(h))
            fwd = close[min(t + h, n - 1)] / close[t] - 1.0 if t + h < n else np.nan
            store[h]['p'].append(it['p_up'] if it else np.nan)
            store[h]['base'].append(it['p_base'] if it else np.nan)
            store[h]['real'].append(float(np.isfinite(fwd) and fwd > 0) if np.isfinite(fwd) else np.nan)
            store[h]['lo'].append(it['q10'] if it else np.nan)
            store[h]['hi'].append(it['q90'] if it else np.nan)
            store[h]['rr'].append(fwd)

    for h in horizons:
        s = store[h]
        p = np.array(s['p'], dtype=float)
        base = np.array(s['base'], dtype=float)
        real = np.array(s['real'], dtype=float)
        lo = np.array([np.nan if x is None else x for x in s['lo']], dtype=float)
        hi = np.array([np.nan if x is None else x for x in s['hi']], dtype=float)
        rr = np.array(s['rr'], dtype=float)
        ok = np.isfinite(p) & np.isfinite(real)
        okb = np.isfinite(base) & np.isfinite(real)
        okc = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(rr)
        metrics = {'n': int(ok.sum())}
        if okc.any():
            metrics['cov80'] = round(float(np.mean((rr[okc] >= lo[okc]) & (rr[okc] <= hi[okc]))), 4)
            metrics['width'] = round(float(np.mean(hi[okc] - lo[okc])), 4)
        if ok.sum():
            metrics['brier'] = round(float(np.mean((p[ok] - real[ok]) ** 2)), 4)
            metrics['brier_base'] = round(float(np.mean((base[okb] - real[okb]) ** 2)), 4)
            metrics['hit_all'] = round(float(np.mean((p[ok] > 0.5) == (real[ok] > 0.5))), 4)
            hi = p >= 0.6
            lo = p <= 0.4
            metrics['n_hi'] = int((hi & ok).sum())
            metrics['hit_hi'] = round(float(real[hi & ok].mean()), 4) if (hi & ok).any() else None
            metrics['n_lo'] = int((lo & ok).sum())
            metrics['hit_lo'] = round(float(1 - real[lo & ok].mean()), 4) if (lo & ok).any() else None
            metrics['rel'] = _reliability(p[ok], real[ok])
        res['h'][str(h)] = {
            'p': [round(float(x), 4) if np.isfinite(x) else None for x in p],
            'real': [int(x) if np.isfinite(x) else None for x in real],
            'base': [round(float(x), 4) if np.isfinite(x) else None for x in base],
            'metrics': metrics,
        }
    return res


def _block_snapshot(feat, t):
    out = {}
    for k, d in feat['blocks'].items():
        if d is None or d.shape[1] == 0:
            continue
        row = d.iloc[t]
        out[k] = {str(c): _r(v, 3) for c, v in row.items()}
    return out


def run(verbose=True):
    t0 = dt.datetime.now()
    ctx = build_context()
    result = {
        'updated': _beijing_now().strftime('%Y-%m-%d %H:%M'),
        'intraday': False,
        'method': {
            'blocks': BLOCK_W,
            'topk': TOPK,
            'z_win': Z_WIN,
            'horizons': HORIZONS,
            'decay_halflife': DECAY_HALFLIFE,
        },
        'indices': {},
        'backtest': {},
    }
    for code, name, product in INDEXES:
        df = load_index_frame(code)
        df = merge_patch(df, ctx['patch'], code)
        feat = build_features(code, name, product, df, ctx)
        mats = _matrices(feat)
        t = len(feat['close']) - 1
        pred = analyze(feat, mats, t, detail=True,
                       iv_series=ctx['iv'] if name == '中证1000' else None)
        result['indices'][name] = {
            'code': code,
            'date': feat['dates'][t],
            'close': _r(feat['close'][t], 2),
            'first_date': feat['dates'][0],
            'n_days': len(feat['dates']),
            # 历史收盘价：用于在价格曲线上标注相似日位置（仅展示）
            'price': {
                'dates': list(feat['dates']),
                'close': [_r(x, 2) for x in np.asarray(feat['close'], dtype=float)],
            },
            'blocks': _block_snapshot(feat, t),
            'block_nb': {k: [{'date': feat['dates'][s], 'd': _r(v, 3)} for s, v in lst]
                         for k, lst in _block_neighbors(mats, t).items()},
            'block_last': feat['block_last'],
            'data_status': feat['data_status'],
            'horizons': pred,
        }
        if verbose:
            print('  %s %s close=%.2f' % (name, feat['dates'][t], feat['close'][t]))
        bt = backtest(feat, mats)
        result['backtest'][name] = bt
        if verbose:
            for h in HORIZONS:
                m = bt['h'].get(str(h), {}).get('metrics', {})
                if m.get('n'):
                    print('    h=%3d n=%3d brier=%.4f base=%.4f  hi=%s lo=%s'
                          % (h, m['n'], m.get('brier', 0), m.get('brier_base', 0),
                             m.get('hit_hi'), m.get('hit_lo')))
        print('  [done] %s  用时 %.1fs' % (name, (dt.datetime.now() - t0).total_seconds()))

    path = os.path.join(BASE, 'analog_data.js')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('// 历史上的今天 —— 相似日概率/空间推算，由 历史上的今天/analog_core.py 自动生成\n')
        f.write('// 生成时间: %s  数据截止: %s\n' % (result['updated'],
                                              result['indices'][INDEXES[1][1]]['date']))
        f.write('window.ANALOG_DATA = ')
        f.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        f.write(';\n')
    print('已输出 %s (%.1f KB) 总用时 %.1fs'
          % (path, os.path.getsize(path) / 1024.0, (dt.datetime.now() - t0).total_seconds()))
    return result


if __name__ == '__main__':
    run()

