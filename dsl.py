# -*- coding: utf-8 -*-
"""因子公式 DSL 编译器：把 'X = expr; 信号 = expr' 形态的公式翻译成可执行函数。
支持：close/open/high/low/volume/ret、.shift(n)、SMA/EMA/WMA/HMA/MA、ROC/RSI/ATR/ADX/CCI/MFI/BIAS、
std/mean/abs/sign/sqrt/exp/ln/cumsum/tanh/corr、rolling_*、HHV/LLV/MAX/MIN/SUM/ZSCORE/PCTL/HV、
linreg_slope、|x| 绝对值竖线、'20d' 类时间单位、score(权重和) 等。
用法：f = compile_formula(text, tf_bars) → f(env) 返回 np.array
env: dict(c,o,h,l,v,tb,fund,oi,ratio, day=(t//86400000))
"""
import re, math
import numpy as np

# ---------- 向量化函数库（numpy 实现） ----------
def _s(x):  # 确保 np.array float
    return np.asarray(x, dtype=float)

def shift(x, n):
    x = _s(x); out = np.full_like(x, np.nan)
    n = int(n)
    if n > 0: out[n:] = x[:-n]
    elif n < 0: out[:n] = x[-n:]
    else: out[:] = x
    return out

def roll(x, n, fn):
    x = _s(x); n = max(1, int(round(n)))
    out = np.full(len(x), np.nan)
    if len(x) < n: return out
    # 用 pandas 引擎最快
    import pandas as pd
    return pd.Series(x).rolling(n).apply(fn, raw=True).values

def SMA(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(1,int(round(n)))).mean().values

def EMA(x, n):
    import pandas as pd
    return pd.Series(_s(x)).ewm(span=max(1,int(round(n))), adjust=False).mean().values

def WMA(x, n):
    x = _s(x); n = max(1, int(round(n)))
    w = np.arange(1, n + 1, dtype=float)
    import pandas as pd
    return pd.Series(x).rolling(n).apply(lambda a: np.dot(a, w) / w.sum(), raw=True).values

def HMA(x, n):
    n = max(1, int(round(n)))
    return WMA(2 * WMA(x, n // 2) - WMA(x, n), int(round(math.sqrt(n))))

def MA(x, n): return SMA(x, n)

def ROC(x, n):
    x = _s(x)
    return x / shift(x, n) - 1

def RSI(x, n):
    x = _s(x); ch = np.diff(x, prepend=np.nan)
    up = np.where(ch > 0, ch, 0.0); dn = np.where(ch < 0, -ch, 0.0)
    import pandas as pd
    au = pd.Series(up).ewm(span=int(n), adjust=False).mean().values
    ad = pd.Series(dn).ewm(span=int(n), adjust=False).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
    return 100 - 100 / (1 + rs)

def ATR(h, l, c, n):
    h, l, c = _s(h), _s(l), _s(c)
    pc = shift(c, 1)
    tr = np.nanmax([h - l, np.abs(h - pc), np.abs(l - pc)], axis=0)
    return SMA(tr, n)

def ADX(h, l, c, n):
    h, l, c = _s(h), _s(l), _s(c)
    up = h - shift(h, 1); dn = shift(l, 1) - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = ATR(h, l, c, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * SMA(pdm, n) / atr; mdi = 100 * SMA(mdm, n) / atr
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    return SMA(dx, n)

def CCI(h, l, c, n):
    tp = (_s(h) + _s(l) + _s(c)) / 3
    ma = SMA(tp, n)
    import pandas as pd
    md = pd.Series(tp).rolling(int(n)).apply(lambda a: np.mean(np.abs(a - a.mean())), raw=True).values
    with np.errstate(divide="ignore", invalid="ignore"):
        return (tp - ma) / (0.015 * md)

def MFI(h, l, c, v, n):
    tp = (_s(h) + _s(l) + _s(c)) / 3
    mf = tp * _s(v)
    up = np.where(tp > shift(tp, 1), mf, 0.0)
    dn = np.where(tp < shift(tp, 1), mf, 0.0)
    import pandas as pd
    s = pd.Series
    with np.errstate(divide="ignore", invalid="ignore"):
        r = s(up).rolling(int(n)).sum().values / s(dn).rolling(int(n)).sum().values
    return 100 - 100 / (1 + r)

def BIAS(x, n):
    x = _s(x); ma = SMA(x, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (x - ma) / ma * 100

def HHV(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(1,int(round(n)))).max().values

def LLV(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(1,int(round(n)))).min().values

def MAX(a, b): return np.maximum(_s(a), _s(b))
def MIN(a, b): return np.minimum(_s(a), _s(b))
def SUM(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(1,int(round(n)))).sum().values
def STD(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(1,int(round(n)))).std().values
def ZSCORE(x, n):
    x = _s(x); m = SMA(x, n); s = STD(x, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (x - m) / s
def PCTL(x, n):
    import pandas as pd
    return pd.Series(_s(x)).rolling(max(2,int(round(n)))).apply(
        lambda a: (a <= a[-1]).mean(), raw=True).values * 100
def HV(x, n):
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(_s(x) / shift(x, 1))
    return STD(lr, n) * math.sqrt(252)

def rolling_std(x, n): return STD(x, n)
def rolling_sum(x, n): return SUM(x, n)
def rolling_mean(x, n): return SMA(x, n)
def rolling_max(x, n): return HHV(x, n)
def rolling_min(x, n): return LLV(x, n)
def rolling_percentile_rank(x, n): return PCTL(x, n)

def rolling_corr(a, b, n):
    import pandas as pd
    return pd.Series(_s(a)).rolling(max(2,int(round(n)))).corr(pd.Series(_s(b))).values

def corr(a, b, n=None):
    if n is None:
        a, b = _s(a), _s(b)
        m = ~(np.isnan(a) | np.isnan(b))
        if m.sum() < 3: return np.nan
        return float(np.corrcoef(a[m], b[m])[0, 1])
    return rolling_corr(a, b, n)

def linreg_slope(x, n):
    import pandas as pd
    x = _s(x); n = max(2, int(round(n)))
    xs = np.arange(n, dtype=float)
    xs = xs - xs.mean()
    den = (xs ** 2).sum()
    def f(a):
        a = a - a.mean()
        return float((a * xs).sum() / den)
    return pd.Series(x).rolling(n).apply(f, raw=True).values

def sign(x): return np.sign(_s(x))
def sqrt(x): return np.sqrt(np.abs(_s(x)))
def exp(x): return np.exp(np.clip(_s(x), -50, 50))
def ln(x):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(np.abs(_s(x)))
def log(x): return ln(x)
def tanh(x): return np.tanh(_s(x))
def cumsum(x):
    import pandas as pd
    return pd.Series(_s(x)).cumsum().values
def pow_(a, b): return np.power(_s(a), _s(b) if np.ndim(b) else float(b))
def UO(*a): return np.zeros(1)  # 占位（实际公式很少用）

def score(*terms):
    """score(a, w1, b, w2, ...) 或 score(a+b+...)：加权求和。奇数位值、偶数位权重。"""
    if len(terms) == 1:
        return _s(terms[0])
    if len(terms) % 2 == 0:
        vals = terms[0::2]; ws = [float(w) for w in terms[1::2]]
        tot = sum(float(w) for w in ws) or 1.0
        out = sum(_s(v) * w for v, w in zip(vals, ws)) / tot
        return out
    return _s(terms[0])

def OBV(c, v):
    c = _s(c); v = _s(v)
    d = np.sign(np.diff(c, prepend=np.nan))
    return np.nancumsum(np.nan_to_num(d * v))

def TODZ(c, tod, hhmm, k, sample_slots):
    """时间-季节因子：每天 hhmm(分钟) 起 k 根K线的远期收益，相对历史同一时段的 z-score。
    tod: 每根K线的日内分钟数(UTC)；sample_slots: 样本槽数(≈天数)。"""
    import pandas as pd
    c = _s(c); tod = _s(tod); k = max(1, int(round(k)))
    fwd = shift(c, -k) / c - 1
    idx = np.where(tod == hhmm)[0]
    out = np.full(len(c), np.nan)
    if len(idx) < 12: return out
    s = pd.Series(fwd[idx])
    n = max(10, int(sample_slots))
    m = s.rolling(n, min_periods=10).mean()
    sd = s.rolling(n, min_periods=10).std()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (s - m) / sd
    out[idx] = z.values
    return out

def _kdj(h, l, c, n, m1, m2):
    import pandas as pd
    h, l, c = _s(h), _s(l), _s(c)
    n = max(1, int(round(n)))
    hh = pd.Series(h).rolling(n).max().values
    ll = pd.Series(l).rolling(n).min().values
    with np.errstate(divide="ignore", invalid="ignore"):
        rsv = (c - ll) / (hh - ll) * 100
    k = pd.Series(rsv).ewm(alpha=1/max(1,int(m1)), adjust=False).mean().values
    d = pd.Series(k).ewm(alpha=1/max(1,int(m2)), adjust=False).mean().values
    return k, d, 3 * k - 2 * d

def KDJJ(h, l, c, n=9, m1=3, m2=3): return _kdj(h, l, c, n, m1, m2)[2]
def KDJK(h, l, c, n=9, m1=3, m2=3): return _kdj(h, l, c, n, m1, m2)[0]
def KDJD(h, l, c, n=9, m1=3, m2=3): return _kdj(h, l, c, n, m1, m2)[1]

def D1(x): return _s(x) - shift(x, 1)
def D2(x):
    x = _s(x)
    return x - 2 * shift(x, 1) + shift(x, 2)

def CMO(x, n):
    x = _s(x); ch = np.diff(x, prepend=np.nan)
    up = np.where(ch > 0, ch, 0.0); dn = np.where(ch < 0, -ch, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 * (SUM(up, n) - SUM(dn, n)) / (SUM(up, n) + SUM(dn, n))

def _di(h, l, c, n):
    h, l, c = _s(h), _s(l), _s(c)
    up = h - shift(h, 1); dn = shift(l, 1) - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = ATR(h, l, c, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 * SMA(pdm, n) / atr, 100 * SMA(mdm, n) / atr

def PDI(h, l, c, n=14): return _di(h, l, c, n)[0]
def MDI(h, l, c, n=14): return _di(h, l, c, n)[1]
def DIDIFF(h, l, c, n=14):
    p, m = _di(h, l, c, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (p - m) / ADX(h, l, c, n) * 100

def SARP(h, l, af0=0.02, afmax=0.2):
    """抛物线 SAR（向量化困难，用循环实现）"""
    h, l = _s(h), _s(l)
    n = len(h); sar = np.full(n, np.nan)
    if n < 3: return sar
    up = True; af = af0; ep = h[0]; s = l[0]
    sar[0] = s
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[max(0, i - 2)])
            if l[i] < s:
                up = False; s = ep; ep = l[i]; af = af0
            elif h[i] > ep:
                ep = h[i]; af = min(af + af0, afmax)
        else:
            s = max(s, h[i - 1], h[max(0, i - 2)])
            if h[i] > s:
                up = True; s = ep; ep = h[i]; af = af0
            elif l[i] < ep:
                ep = l[i]; af = min(af + af0, afmax)
        sar[i] = s
    return sar

# ---------- 中文描述型因子的专用实现 ----------
def _slot_idx(tod):
    return _s(tod)

def TODZRAW(x, tod, slot, days):
    """当前值 vs 历史同一时段(slot=分钟) 的 z-score"""
    import pandas as pd
    x = _s(x); tod = _s(tod)
    idx = np.where(tod == slot)[0]
    out = np.full(len(x), np.nan)
    if len(idx) < 12: return out
    s = pd.Series(x[idx]); n = max(10, int(days))
    m = s.rolling(n, min_periods=8).mean(); sd = s.rolling(n, min_periods=8).std()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (s - m) / sd
    out[idx] = z.values
    return out

def TODZ_ALL(x, tod, days):
    """每个bar相对其自身同时段历史的 z-score"""
    x = _s(x); tod = _s(tod)
    out = np.full(len(x), np.nan)
    for slot in np.unique(tod[~np.isnan(tod)]):
        z = TODZRAW(x, tod, slot, days)
        idx = np.where(tod == slot)[0]
        out[idx] = z[idx]
    return out

def TODREL(x, tod, slot, days):
    """当前值 / 历史同一时段均值"""
    import pandas as pd
    x = _s(x); tod = _s(tod)
    idx = np.where(tod == slot)[0]
    out = np.full(len(x), np.nan)
    if len(idx) < 12: return out
    s = pd.Series(x[idx]); n = max(10, int(days))
    m = s.rolling(n, min_periods=8).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out[idx] = (s / m).values
    return out

def TODAYH(h, day):
    """当日内（含当前）累计最高"""
    h = _s(h); day = _s(day)
    out = np.full(len(h), np.nan)
    import pandas as pd
    s = pd.Series(h).groupby(day).cummax()
    return s.values

def TODAYL(l, day):
    l = _s(l); day = _s(day)
    import pandas as pd
    return pd.Series(l).groupby(day).cummin().values

def UPV(v, o, c):
    v, o, c = _s(v), _s(o), _s(c)
    return np.where(c > o, v, 0.0)

def DNV(v, o, c):
    v, o, c = _s(v), _s(o), _s(c)
    return np.where(c < o, v, 0.0)

def FRACUP(o, c, n):
    """近n根中阳线占比"""
    o, c = _s(o), _s(c)
    return SMA((c > o).astype(float), n)

def FRACUPSH(o, h, l, c, n):
    """近n根中上影线>实体占比"""
    o, h, c = _s(o), _s(h), _s(c)
    upsh = h - np.maximum(o, c); body = np.abs(c - o)
    return SMA((upsh > body).astype(float), n)

def FRACDNSH(o, h, l, c, n):
    o, l, c = _s(o), _s(l), _s(c)
    dnsh = np.minimum(o, c) - l; body = np.abs(c - o)
    return SMA((dnsh > body).astype(float), n)

def BODYR(o, h, l, c, n):
    """实体/全幅 的均值"""
    o, h, l, c = _s(o), _s(h), _s(l), _s(c)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.abs(c - o) / (h - l)
    return SMA(r, n)

def FRACBIGBODY(o, h, l, c, n):
    """长实体(>ATR) 占比"""
    o, h, l, c = _s(o), _s(h), _s(l), _s(c)
    a = ATR(h, l, c, 14)
    return SMA((np.abs(c - o) > a).astype(float), n)

def CONSECN(o, c, n, up=1):
    """连续同向K线计数达到n后的强度（阳线为正、阴线为负，未达n为0）"""
    o, c = _s(o), _s(c)
    d = np.sign(c - o)
    streak = np.zeros(len(c))
    for i in range(1, len(c)):
        if d[i] != 0 and d[i] == d[i - 1]: streak[i] = streak[i - 1] + 1
        elif d[i] != 0: streak[i] = 1
    sgn = 1.0 if up else -1.0
    dd = d if up else -d
    st = np.where(dd > 0, streak, 0.0)
    return np.where(st >= n, sgn * st, 0.0)

def DIVG(c, x, n):
    """背离近似：价格动量 - 指标动量（正=顶背离倾向，负=底背离倾向）"""
    c = _s(c); x = _s(x)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (c / shift(c, n) - 1) - (x / shift(x, n) - 1)

def YH(h, l, day):
    """昨日最高"""
    import pandas as pd
    dh = pd.Series(_s(h)).groupby(_s(day)).max()
    prev = dh.shift(1)
    return pd.Series(_s(day)).map(prev).values

def YL(h, l, day):
    """昨日最低"""
    import pandas as pd
    dl = pd.Series(_s(l)).groupby(_s(day)).min()
    prev = dl.shift(1)
    return pd.Series(_s(day)).map(prev).values

def VWAPN(h, l, c, v, n):
    """n根K线滚动VWAP"""
    h, l, c, v = _s(h), _s(l), _s(c), _s(v)
    tp = (h + l + c) / 3
    with np.errstate(divide="ignore", invalid="ignore"):
        return SUM(tp * v, n) / SUM(v, n)

def ORBSIG(c, h, l, tod, atr, nbar):
    """开盘区间突破：区间外偏离度(ATR归一, 带方向)"""
    c, h, l, tod, atr = _s(c), _s(h), _s(l), _s(tod), _s(atr)
    nbar = max(1, int(nbar))
    out = np.zeros(len(c))
    import pandas as pd
    day = np.zeros(len(c))  # tod 已含日内位置；用 tod 翻转定日界
    d = np.zeros(len(c), dtype=int); cur = 0
    for i in range(1, len(c)):
        if tod[i] < tod[i - 1]: cur += 1
        d[i] = cur
    for dd in np.unique(d):
        idx = np.where(d == dd)[0]
        k = min(nbar, len(idx))
        hi = np.nanmax(h[idx[:k]]); lo = np.nanmin(l[idx[:k]])
        seg = c[idx]
        a = atr[idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            val = np.where(seg > hi, (seg - hi) / a, np.where(seg < lo, (seg - lo) / a, 0.0))
        out[idx] = np.nan_to_num(val)
    return out

# ---------- 公式翻译 ----------
def _bars(unit_str, per_day):
    m = re.match(r"(\d+(?:\.\d+)?)([smhd]?)", str(unit_str))
    if not m: return float(unit_str)
    x = float(m.group(1)); u = m.group(2)
    if u == "d": return x * per_day
    if u == "h": return x * per_day / 24
    if u == "m": return x * per_day / (24 * 60)
    return x

# 单参函数 → 默认价格序列补全
_1ARG_C = ["SMA", "EMA", "WMA", "HMA", "MA", "ROC", "RSI", "BIAS", "HV",
           "ZSCORE", "PCTL", "STD", "SUM", "HHV", "LLV", "rolling_std",
           "rolling_sum", "rolling_mean", "rolling_max", "rolling_min",
           "rolling_percentile_rank", "linreg_slope"]
_1ARG_HLC = ["ATR", "ADX", "CCI"]
_1ARG_HLCV = ["MFI"]

def compile_formula(text, per_day=6):
    """per_day: 该周期每天的K线数（4h=6, 1h=24, 15m=96, 5m=288, 1d=1）。
    返回 python 源码字符串；公式值变量名为 OUT。"""
    t = text.strip()

    # —— 中文描述型公式的专用模式（全公式匹配，直接返回） ——
    # 历史该UTC HH 时段收益 z-score
    m = re.search(r"历史该UTC\s*0?(\d{1,2})\s*时段收益均值的\s*z-?score\s*[（(]样本>=(\d+)天", t)
    if m:
        return f"OUT = TODZRAW(ret, tod, {int(m.group(1))*60}, {m.group(2)})"
    # 当前HH时成交量 / 过去D日同一时段均值
    m = re.search(r"当前\s*0?(\d{1,2})\s*时成交量\s*/\s*过去(\d+)日同一", t)
    if m:
        return f"OUT = TODREL(v, tod, {int(m.group(1))*60}, {m.group(2)})"
    # (volume - rolling_mean(volume, Nd同时段))/rolling_std → 同时段z
    m = re.search(r"rolling_mean\(volume,\s*(\d+)d同时段\)", t)
    if m:
        return f"OUT = TODZ_ALL(v, tod, {m.group(1)})"
    # 近N根K线中 X 占比
    m = re.match(r"近(\d+)根K线中\s*(.+?)\s*占比", t)
    if m:
        n_, what = m.group(1), m.group(2)
        if "阳线" in what: return f"OUT = FRACUP(o,c,{n_})"
        if "上影线" in what: return f"OUT = FRACUPSH(o,h,l,c,{n_})"
        if "下影线" in what: return f"OUT = FRACDNSH(o,h,l,c,{n_})"
        if "实体/全幅" in what: return f"OUT = BODYR(o,h,l,c,{n_})"
        if "长实体" in what: return f"OUT = FRACBIGBODY(o,h,l,c,{n_})"
    # 连续N根 阳线/阴线
    m = re.match(r"连续(\d+)根(?:1h|4h|1d)?(阳线|阴线)", t)
    if m:
        return f"OUT = CONSECN(o,c,{m.group(1)},{1 if m.group(2)=='阳线' else 0})"
    # RSI(n,tf) 与价格背离
    m = re.match(r"RSI\((\d+),?\s*\d*[mh]?\)\s*与价格的近(\d+)bar顶底背离", t)
    if m:
        return f"OUT = DIVG(c, RSI(c,{m.group(1)}), {m.group(2)})"
    # 裸指标描述：'RSI(5) 基于1hK线' → OUT = RSI(c,5)
    m = re.match(r"^([A-Za-z]+)\((\d+(?:,\d+)*)\)\s*基于\s*\d+\s*[mhK线\s]*$", t)
    if m:
        fn, prm = m.group(1), m.group(2)
        if fn in ("RSI", "ROC", "BIAS", "HV"): return f"OUT = {fn}(c,{prm.split(',')[0]})"
        if fn in ("ATR", "ADX", "CCI"): return f"OUT = {fn}(h,l,c,{prm.split(',')[0]})"
        if fn == "KDJ": return f"OUT = KDJK(h,l,c,{prm})"
    # 布林带 %B：'%B = (close-lower(n,kσ))/(upper-lower)'
    m = re.search(r"%B\s*=\s*\(close-lower\((\d+),([\d.]+)σ\)\)/\(upper-lower\)", t)
    if m:
        n_, k_ = m.group(1), m.group(2)
        return f"OUT = (c-(SMA(c,{n_})-{k_}*STD(c,{n_})))/(2*{k_}*STD(c,{n_}))"
    # 开盘区间突破
    m = re.match(r"取开盘后(\d+)分钟高低点", t)
    if m:
        nbar = max(1, round(int(m.group(1)) / (1440.0 / per_day)))
        return f"OUT = ORBSIG(c,h,l,tod,ATR(h,l,c,14),{nbar})"
    # 突破昨日最高/最低, N根K线确认
    m = re.match(r"突破昨日(最高|最低)", t)
    if m:
        fn = "YH" if m.group(1) == "最高" else "YL"
        return f"OUT = (c - {fn}(h,l,day)) / ATR(h,l,c,14)"
    # RSI(n) 基于Xm; 顶/底背离 → 背离近似
    m = re.match(r"RSI\((\d+)\)\s*基于\d+m", t)
    if m:
        return f"OUT = DIVG(c, RSI(c,{m.group(1)}), {m.group(1)})"
    # 短长窗波动率共振：'短(1m)与长窗波动率比值>1.5倍 且 持续放大'
    m = re.match(r"短\((\d+)m\)与长窗波动率比值>(\d+(?:\.\d+)?)倍", t)
    if m:
        b1 = max(3, round(int(m.group(1)) / (1440.0 / per_day)))
        b60 = max(6, round(60 / (1440.0 / per_day)))
        return f"OUT = HV(c,{b1}) / HV(c,{b60})"
    m_tod = re.search(r"历史每日(\d{1,2})[:：](\d{2})的(\d+)分钟收益\s*z-?score\s*[（(]样本>=(\d+)天", t)
    if m_tod:
        hh, mm, nmin, days = (int(m_tod.group(1)), int(m_tod.group(2)),
                              int(m_tod.group(3)), int(m_tod.group(4)))
        bar_min = 1440.0 / per_day
        k = max(1, round(nmin / bar_min))
        hhmm = hh * 60 + mm
        return f"OUT = TODZ(c, tod, {hhmm}, {k}, {days})"

    # 预处理
    t = t.replace("；", ";").replace("，", ",").replace("（", "(").replace("）", ")")
    t = t.replace("≥", ">=").replace("≤", "<=")
    # 先做中文变量/术语预替换（避免被括号注释剥离误伤）
    for src, dst in [("ETH/BTC汇率", "ratio"), ("上涨K线成交量", "UPV(v,o,c)"),
                     ("下跌K线成交量", "DNV(v,o,c)"),
                     ("日内最高", "TODAYH(h,day)"), ("日内最低", "TODAYL(l,day)")]:
        t = t.replace(src, dst)
    # 剥离括号内中文注释：(高点回撤) —— 括号内含中文且去掉中文/数字/标点后没有≥2字母的英文词
    def _paren_strip(m):
        inner = m.group(1)
        inner2 = re.sub(r"\d+(?:\.\d+)?[smhd]", "", inner)  # 去掉 1m/5m/1d 类时间单位
        ascii_words = re.sub(r"[一-鿿0-9\s.,:：%±><=+\-*/]", "", inner2)
        if re.search(r"[a-zA-Z]{2,}", ascii_words):
            return m.group(0)  # 含变量/函数 → 保留
        return ""
    t = re.sub(r"\(([^()]*[一-鿿][^()]*)\)", _paren_strip, t)
    # 威廉 %R：'%R = (HHV(high,n) - close)/(HHV - LLV) * -100'
    t = re.sub(r"%R\s*=\s*\(HHV\(high,(\d+)\)\s*-\s*close\)/\(HHV\s*-\s*LLV\)\s*\*\s*-100",
               r"WR = (HHV(h,\1)-c)/(HHV(h,\1)-LLV(l,\1))*-100", t)
    # 百分号：2% → 0.02
    t = re.sub(r"(\d+(?:\.\d+)?)%", lambda m: str(float(m.group(1)) / 100), t)
    # |x| → ABS(x)
    t = re.sub(r"\|([^|;]+)\|", lambda m: f"ABS({m.group(1)})", t)
    # 随机指标：'%K(8,3), %D(3)' → K 值输出
    t = re.sub(r"%K\((\d+),\s*(\d+)\)\s*,\s*%D\((\d+)\)",
               r"STOCH = KDJK(h,l,c,\1,\2,\3)", t)
    # SAR：'SAR(step=0.01,max=0.1)' → 价格与 SAR 距离（ATR 归一）
    t = re.sub(r"SAR\(step=([\d.]+),\s*max=([\d.]+)\)",
               r"SARDEV = ((c - SARP(h,l,\1,\2)) / ATR(h,l,c,14))", t)
    # CMO 定义式：'CMO(5) = 100*(Su-Sd)/(Su+Sd)' → 直接调用
    t = re.sub(r"CMO\((\d+)\)\s*=\s*100\*\(Su-Sd\)/\(Su\+Sd\)", r"CMOV = CMO(c,\1)", t)
    # DI 差：'(+DI - -DI)/ADX' → DIDIFF
    t = re.sub(r"\(\s*\+DI\s*-\s*-DI\s*\)\s*/\s*ADX", "DIDIFF(h,l,c,14)", t)
    # 差分算子：Δ²(x) → D2(x)；Δx / Δ(x) → D1(x)（允许一层嵌套括号）
    t = re.sub(r"Δ²\(((?:[^()]|\([^()]*\))*)\)", r"D2(\1)", t)
    t = re.sub(r"Δ\(((?:[^()]|\([^()]*\))*)\)", r"D1(\1)", t)
    t = re.sub(r"Δ([A-Za-z_][\w]*)", r"D1(\1)", t)
    # 时间单位：20d / 4h / 30m → K线数（前面不能是标识符字符，保护 ret_1m 等）
    t = re.sub(r"(?<![a-zA-Z0-9_])(\d+(?:\.\d+)?)([dhm])(?![a-zA-Z0-9])",
               lambda m: str(max(1, int(round(_bars(m.group(1) + m.group(2), per_day))))), t)
    # Σ(...) 求和 → SUM
    t = re.sub(r"Σ\(([^)]+),\s*i=1\.\.(\w+)\)", r"SUM(\1, \2)", t)
    # ± 通道：'MID ± k*WIDTH' → 价格相对通道的带符号偏离度 ((c)-(MID))/(WIDTH)
    def band_repl(m):
        return f"((c) - ({m.group(1)}))/({m.group(2)})"
    t = re.sub(r"([^=;]+?)\s*±\s*([^;]+)", band_repl, t)
    # KDJ 三连：'J = 3K-2D; KDJ(9,3,3); ...' → 'J = KDJJ(h,l,c,9,3,3)'
    m_kdj = re.search(r"KDJ\(([^)]*)\)", t)
    if m_kdj and re.search(r"[JD]\s*=\s*3\*?K\s*-\s*2\*?D", t):
        params = m_kdj.group(1)
        t = re.sub(r"([JD])\s*=\s*3\*?K\s*-\s*2\*?D",
                   lambda m: f"{m.group(1)} = KDJJ(h,l,c,{params})", t)
        t = re.sub(r";?\s*KDJ\([^)]*\)", "", t)
    # 连续计数：'近N根K线中 <cond> 的连续计数' → SUM(cond, N)
    t = re.sub(r"近(\d+)根K线中\s*([^;]+?)\s*的连续计数", r"SUM((\2), \1)", t)
    # 上穿/下穿：'A 上穿/下穿 B' → (A - B)；SMA(RSI,n) 用外层周期补全
    def cross_repl(m):
        a, b = m.group(1), m.group(2)
        mp = re.match(r"(\w+)\((\d+)\)", a)
        if mp and re.match(r"SMA\(RSI,", b):
            b = f"SMA(RSI(c,{mp.group(2)}),{re.match(r'SMA\(RSI,(\d+)\)', b).group(1)})"
        return f"({a} - {b})"
    t = re.sub(r"(\w+\([^()]*(?:\([^()]*\)[^()]*)*\))\s*上穿/下穿\s*(\w+\([^()]*\))", cross_repl, t)

    # 变量名归一（长名优先；含中文复合词）
    varmap = {"ETH/BTC汇率": "ratio", "funding_rate": "fund", "prev_close": "shift(c,1)",
              "open_interest": "oi_", "VWAP_session": "vwap", "VWAP": "vwap",
              "上涨K线成交量": "UPV(v,o,c)", "下跌K线成交量": "DNV(v,o,c)",
              "日内最高": "TODAYH(h,day)", "日内最低": "TODAYL(l,day)",
              "close": "c", "open": "o", "high": "h", "low": "l",
              "volume": "v", "vol": "v", "takerBuy": "tb", "funding": "fund",
              "ret_1m": "ret", "ret_0": "ret",
              "ratio": "ratio"}
    # 按语句切分
    stmts = [s.strip() for s in t.split(";") if s.strip()]
    lines = []          # (lhs_safe, rhs_raw)
    assigned = {}       # 原名 → safe 名
    last_assigned = None
    for s in stmts:
        m = re.match(r"^([\w一-鿿/]+)\s*=\s*(.+)$", s)
        if m:
            lhs, rhs = m.group(1), m.group(2)
            safe_lhs = re.sub(r"\W", "_", lhs)
            lines.append((safe_lhs, rhs))
            assigned[lhs] = safe_lhs
            last_assigned = safe_lhs
        else:
            lines.append((None, s))

    if not lines:
        raise ValueError("empty formula")

    def trans(expr):
        e = expr
        e = e.replace("^", "**")
        e = re.sub(r"([\w_]+)\.shift\(([^)]+)\)", r"shift(\1,\2)", e)
        e = re.sub(r"\bpow\(", "pow_(", e)
        e = re.sub(r"\bstd\(", "STD(", e)
        e = re.sub(r"\bmean\(", "SMA(", e)
        # 跨周期 ROC：ROC_5m / ROC_1h / ROC_4h → ROC(c, 对应K线数)
        e = re.sub(r"\bROC_(\d+)([mhd])\b",
                   lambda m: f"ROC(c,{max(1, int(round(_bars(m.group(1) + m.group(2), per_day))))})", e)
        # 跨周期收益：ret_3m / ret_15m / ret_1h → (c/shift(c,n)-1)
        e = re.sub(r"\bret_(\d+)([mh])\b",
                   lambda m: f"(c/shift(c,{max(1, int(round(_bars(m.group(1) + m.group(2), per_day))))})-1)", e)
        # VWAP_Nd → N日滚动VWAP
        e = re.sub(r"\bVWAP_(\d+)d\b",
                   lambda m: f"VWAPN(h,l,c,v,{max(1, int(round(_bars(m.group(1) + 'd', per_day))))})", e)
        # 单参函数补默认序列：ATR(14) → ATR(h,l,c,14) 等
        for fn in _1ARG_C:
            e = re.sub(rf"\b{fn}\(\s*(\d+(?:\.\d+)?)\s*\)", rf"{fn}(c,\1)", e)
        for fn in _1ARG_HLC:
            e = re.sub(rf"\b{fn}\(\s*(\d+(?:\.\d+)?)\s*\)", rf"{fn}(h,l,c,\1)", e)
            # ATR(5,6) 双数值参数（周期,周期倍数）→ 相乘
            e = re.sub(rf"\b{fn}\((\d+),(\d+)\)",
                       lambda m: f"{fn}(h,l,c,{int(m.group(1))*int(m.group(2))})", e)
        for fn in _1ARG_HLCV:
            e = re.sub(rf"\b{fn}\(\s*(\d+(?:\.\d+)?)\s*\)", rf"{fn}(h,l,c,v,\1)", e)
        # 变量替换（长名优先）
        for src, dst in sorted(varmap.items(), key=lambda kv: -len(kv[0])):
            if re.search(r"[一-鿿/]", src):
                e = e.replace(src, dst)
            else:
                e = re.sub(rf"\b{src}\b", dst, e)
        # 大写 OI → oi_（避免覆盖 KDJ 等）
        e = re.sub(r"\bOI\b", "oi_", e)
        # 已赋值中文变量名 → safe 名
        for lhs, safe in assigned.items():
            if re.search(r"[一-鿿]", lhs):
                e = e.replace(lhs, safe)
        return e

    src_lines = []
    last_kind = None      # 'out' 或 'assign'
    last_assign = None
    for lhs, rhs in lines:
        te = trans(rhs).strip()
        # 残留中文 → 该语句不可算，丢弃（注释/说明文字）
        if re.search(r"[一-鿿]", te):
            continue
        if lhs is None:
            # 裸表达式：单参形式如 ROC(4) → 作用于上一个已赋值变量
            m1 = re.match(r"^([A-Za-z_][\w]*)\((\d+(?:\.\d+)?)\)$", te)
            if m1 and last_assign:
                fn = m1.group(1)
                if fn in _1ARG_C:
                    te = f"{fn}({last_assign},{m1.group(2)})"
                elif fn in _1ARG_HLC:
                    te = f"{fn}(h,l,c,{m1.group(2)})"
            src_lines.append(f"OUT = ({te})")
            last_kind = "out"
        else:
            src_lines.append(f"{lhs} = ({te})")
            last_kind = "assign"
            last_assign = lhs
    if not src_lines:
        raise ValueError("no computable statement")
    # 最终输出以"最后一个可算语句"为准：是赋值则 OUT=该变量，是裸表达式则已写 OUT
    if last_kind == "assign":
        src_lines.append(f"OUT = {last_assign}")
    return "\n".join(src_lines)

# 警报类公式（NS）：'ALERT = cond; 触发后冻结X分钟' → 输出条件布尔
def compile_alert(text, per_day=288):
    t = text.split(";")[0]
    m = re.match(r"\s*ALERT\s*=\s*(.+)$", t)
    if m:
        return compile_formula("信号 = " + m.group(1), per_day)
    return compile_formula(text, per_day)
