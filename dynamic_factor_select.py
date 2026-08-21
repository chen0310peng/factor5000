# -*- coding: utf-8 -*-
"""动态因子选型 v2：分类回测 + 币安/OKX 双源验证。
回测方法按类别区分：
  波段/日内/超短线 → 信号模拟交易：滚动z→方向仓位→扣除万4换手费 → 夏普率/胜率/IC
  超跌超涨防护     → 反转命中率：|z|>2 极值点后 2h 收益方向命中率
  消息面防护       → 事件研究法：警报触发后 1h 大波动概率 vs 无条件基线（提升倍数）
验证：币安全期+近期IC方向一致；OKX 近期IC同号交叉验证（不同号权重减半）。
输出：每类全部有效因子上岗（≥100个），含每因子的夏普/胜率/命中率/提升倍数。
"""
import requests, time, json, sys, os, warnings
import numpy as np
import pandas as pd
import sqlite3

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsl
from dsl import compile_formula

BNA = "https://fapi.binance.com"
OKX = "https://www.okx.com"
S = requests.Session(); S.headers.update({"User-Agent": "Mozilla/5.0"})
CACHE = "data/factor5000/cache"
os.makedirs(CACHE, exist_ok=True)

def get(base, path, params, retries=3):
    for i in range(retries):
        try:
            r = S.get(base + path, params=params, timeout=30)
            if r.status_code == 200: return r.json()
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {path} failed")

def bn_klines(symbol, interval, days):
    end = int(time.time() * 1000); start = end - days * 86400000
    rows, cur = [], start
    while cur < end:
        data = get(BNA, "/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                            "startTime": cur, "limit": 1500})
        if not data: break
        rows.extend(data); cur = data[-1][0] + 1
        if len(data) < 1500: break
        time.sleep(0.12)
    df = pd.DataFrame(rows, columns=["t","o","h","l","c","v","ct","qv","n","tb","tq","ig"])
    df = df[["t","o","h","l","c","v","tb"]].astype(float)
    df["t"] = df["t"].astype(np.int64)
    return df.drop_duplicates("t").reset_index(drop=True)

def okx_klines(inst, bar, days):
    """OKX history-candles：每页100根，newest-first，after=比该ts早的分页"""
    rows, after = [], None
    need = {"1H": 24, "4H": 6, "15m": 96, "5m": 288, "1D": 1}[bar] * days
    while len(rows) < need:
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if after: p["after"] = str(after)
        d = get(OKX, "/api/v5/market/history-candles", p)
        arr = d.get("data") or []
        if not arr: break
        rows.extend(arr); after = int(arr[-1][0]) - 1
        if len(arr) < 100: break
        time.sleep(0.1)
    if not rows: raise RuntimeError(f"OKX {inst} {bar} 无数据")
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["t","o","h","l","c","v"]
    df = df.astype(float); df["t"] = df["t"].astype(np.int64)
    df = df.sort_values("t").reset_index(drop=True)
    df["tb"] = df["v"] * 0.5      # OKX 无 taker 字段，中性占位
    return df

BYBIT = "https://api.bybit.com"

def bybit_klines(symbol, interval_min, days):
    """Bybit v5 线性永续K线：第三备用源（云端币安/OKX 都被地域限制时启用）"""
    end = int(time.time() * 1000); start = end - days * 86400000
    rows, cur = [], start
    while cur < end:
        d = get(BYBIT, "/v5/market/kline", {"category": "linear", "symbol": symbol,
                                            "interval": str(interval_min), "start": cur, "limit": 1000})
        arr = (d.get("result") or {}).get("list") or []
        if not arr: break
        arr = sorted(arr, key=lambda x: int(x[0]))
        rows.extend(arr); cur = int(arr[-1][0]) + 1
        if len(arr) < 1000: break
        time.sleep(0.12)
    if not rows: raise RuntimeError(f"Bybit {symbol} {interval_min}m 无数据")
    df = pd.DataFrame([r[:6] for r in rows], columns=["t","o","h","l","c","v"])
    df = df.astype(float); df["t"] = df["t"].astype(np.int64)
    df = df.sort_values("t").drop_duplicates("t").reset_index(drop=True)
    df["tb"] = df["v"] * 0.5      # 中性占位
    return df

def load_data():
    stamp = os.path.join(CACHE, "stamp_v2.txt")
    fresh = os.path.exists(stamp) and (time.time() - os.path.getmtime(stamp)) < 20 * 3600
    names = []
    for src in ["bn", "okx"]:
        for sym in (["BTCUSDT", "ETHUSDT"] if src == "bn" else ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]):
            for tf in (["4h","1h","15m","5m","1d"] if src == "bn" else ["4H","1H","15m","5m","1D"]):
                names.append(f"{src}_{sym}_{tf}")
    if fresh and "--refresh" not in sys.argv:
        try:
            D = {n: pd.read_csv(f"{CACHE}/{n}.csv") for n in names}
            fund = {s: pd.read_csv(f"{CACHE}/bn_{s}_fund.csv") for s in ["BTCUSDT","ETHUSDT"]}
            oi = {s: pd.read_csv(f"{CACHE}/bn_{s}_oi.csv") for s in ["BTCUSDT","ETHUSDT"]}
            print("使用缓存数据（币安+OKX）", flush=True)
            return D, fund, oi
        except Exception:
            pass
    print("拉取数据（币安→Bybit→OKX 三级容灾，约2-4分钟）...", flush=True)
    D = {}
    bn_ok = True
    try:
        for sym in ["BTCUSDT", "ETHUSDT"]:
            for tf, days in [("4h",250),("1h",250),("15m",120),("5m",60),("1d",500)]:
                D[f"bn_{sym}_{tf}"] = bn_klines(sym, tf, days)
                D[f"bn_{sym}_{tf}"].to_csv(f"{CACHE}/bn_{sym}_{tf}.csv", index=False)
            print(f"  币安 {sym} 完成", flush=True)
    except Exception as e:
        bn_ok = False
        print(f"  ⚠️ 币安不可达（{e}）", flush=True)
        D = {k: v for k, v in D.items() if not k.startswith("bn_")}
    if not bn_ok:
        # 第二备用：Bybit（云端美国服务器常被币安/OKX 同时地域限制时用它）
        try:
            imap = {"4h":240,"1h":60,"15m":15,"5m":5,"1d":"D"}
            for sym in ["BTCUSDT", "ETHUSDT"]:
                for tf, days in [("4h",250),("1h",250),("15m",120),("5m",60),("1d",500)]:
                    D[f"bn_{sym}_{tf}"] = bybit_klines(sym, imap[tf], days)
                    D[f"bn_{sym}_{tf}"].to_csv(f"{CACHE}/bn_{sym}_{tf}.csv", index=False)
                print(f"  Bybit {sym} 完成（顶替币安主源）", flush=True)
            bn_ok = True
        except Exception as e:
            print(f"  ⚠️ Bybit 也不可达（{e}）", flush=True)
            D = {k: v for k, v in D.items() if not k.startswith("bn_")}
    okx_ok = True
    try:
        for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
            for bar, days in [("4H",250),("1H",250),("15m",30),("5m",14),("1D",500)]:
                D[f"okx_{inst}_{bar}"] = okx_klines(inst, bar, days)
                D[f"okx_{inst}_{bar}"].to_csv(f"{CACHE}/okx_{inst}_{bar}.csv", index=False)
            print(f"  OKX {inst} 完成", flush=True)
    except Exception as e:
        okx_ok = False
        print(f"  ⚠️ OKX 不可达（{e}），双源验证自动降级", flush=True)
        D = {k: v for k, v in D.items() if not k.startswith("okx_")}
    if not bn_ok and okx_ok:
        # 币安/Bybit 都挂了：用 OKX 数据冒充主源，双源验证自动降级为单源自验
        tfmap = {"4h":"4H","1h":"1H","15m":"15m","5m":"5m","1d":"1D"}
        smap = {"BTCUSDT":"BTC-USDT-SWAP","ETHUSDT":"ETH-USDT-SWAP"}
        for sym, inst in smap.items():
            for tf, bar in tfmap.items():
                D[f"bn_{sym}_{tf}"] = D[f"okx_{inst}_{bar}"].copy()
                D[f"bn_{sym}_{tf}"].to_csv(f"{CACHE}/bn_{sym}_{tf}.csv", index=False)
        bn_ok = True
        print("  已用 OKX 数据顶替币安主源", flush=True)
    if not bn_ok:
        raise RuntimeError("币安/Bybit/OKX 全部不可达，本次回测无法进行")
    fund, oi = {}, {}
    for sym in ["BTCUSDT", "ETHUSDT"]:
        try:
            f = get(BNA, "/fapi/v1/fundingRate", {"symbol": sym, "limit": 1000})
            fund[sym] = pd.DataFrame({"t": [x["fundingTime"] for x in f],
                                      "r": [float(x["fundingRate"]) for x in f]})
            o = get(BNA, "/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 500})
            oi[sym] = pd.DataFrame({"t": [x["timestamp"] for x in o],
                                    "v": [float(x["sumOpenInterest"]) for x in o]})
        except Exception as e:
            print(f"  ⚠️ 币安资金费率/OI 拉取失败（{e}），用空表降级", flush=True)
            fund[sym] = pd.DataFrame({"t": [], "r": []})
            oi[sym] = pd.DataFrame({"t": [], "v": []})
        fund[sym].to_csv(f"{CACHE}/bn_{sym}_fund.csv", index=False)
        oi[sym].to_csv(f"{CACHE}/bn_{sym}_oi.csv", index=False)
    with open(stamp, "w") as fp: fp.write(str(time.time()))
    return D, fund, oi

def resample(df, rule):
    d = df.set_index(pd.to_datetime(df["t"], unit="ms"))
    g = d.resample(rule).agg({"t":"first","o":"first","h":"max","l":"min",
                              "c":"last","v":"sum","tb":"sum"}).dropna()
    return g.reset_index(drop=True)

def asof_series(base_t, aux_t, aux_v):
    if len(aux_t) == 0: return np.full(len(base_t), np.nan)
    s = pd.Series(np.asarray(aux_v, dtype=float), index=pd.to_datetime(aux_t, unit="ms"))
    return s.reindex(pd.to_datetime(base_t, unit="ms"), method="ffill").values

TF2PD = {"4h":6,"1h":24,"15m":96,"5m":288,"1d":1,"30m":48,"8h":3}
def base_pd(tf):
    for k in ["15m","5m","4h","1h","30m","8h","1d"]:
        if k in tf: return TF2PD[k]
    return 24

# 数据源键映射：因子 tf → (币安缓存键后缀, OKX缓存键后缀)
TFKEY = [("15m","15m","15m"), ("5m","5m","5m"), ("30m","15m","15m"),
         ("4h","4h","4H"), ("8h","4h","4H"), ("1h","1h","1H"), ("1d","1d","1D")]
def src_keys(tf):
    for pat, b, o in TFKEY:
        if pat in tf: return b, o
    return "1h", "1H"

def build_env(df, fund_s=None, oi_s=None, ratio_s=None):
    c = df["c"].values.astype(float); o = df["o"].values.astype(float)
    h = df["h"].values.astype(float); l = df["l"].values.astype(float)
    v = df["v"].values.astype(float); tb = df["tb"].values.astype(float)
    t = df["t"].values
    fund = asof_series(t, fund_s["t"], fund_s["r"]) if fund_s is not None and len(fund_s) else np.full(len(t), np.nan)
    oiv = asof_series(t, oi_s["t"], oi_s["v"]) if oi_s is not None and len(oi_s) else np.full(len(t), np.nan)
    ratio = asof_series(t, ratio_s["t"], ratio_s["r"]) if ratio_s is not None else np.full(len(t), np.nan)
    ret = np.concatenate([[np.nan], c[1:]/c[:-1]-1])
    tp = (h+l+c)/3; day = (t//86400000).astype(float)
    pv = pd.Series(tp*v).groupby(day).cumsum().values
    vv = pd.Series(v).groupby(day).cumsum().values
    with np.errstate(divide="ignore", invalid="ignore"): vwap = pv/vv
    tod = ((t//60000)%1440).astype(float)
    return dict(c=c,o=o,h=h,l=l,v=v,tb=tb,fund=fund,oi_=oiv,ratio=ratio,
                ret=ret,vwap=vwap,tod=tod,day=day,ABS=np.abs)

def spearman(a, b):
    a, b = pd.Series(a), pd.Series(b)
    m = a.notna() & b.notna() & np.isfinite(a) & np.isfinite(b)
    if m.sum() < 100: return np.nan, int(m.sum())
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0: return np.nan, int(m.sum())
    return float(ra.corr(rb)), int(m.sum())

def eval_factor(src, env):
    g = {k: getattr(dsl, k) for k in dir(dsl) if not k.startswith("_")}
    g.update(env); g["np"] = np
    exec(compile(src, "<f>", "exec"), g)
    out = g.get("OUT")
    if out is None: return None
    arr = np.asarray(out, dtype=float) if np.ndim(out) else np.full(len(env["c"]), float(out))
    return arr if len(arr) == len(env["c"]) else None

def roll_z(fac, w):
    s = pd.Series(fac)
    m = s.rolling(w, min_periods=max(20, w//3)).mean()
    sd = s.rolling(w, min_periods=max(20, w//3)).std()
    with np.errstate(divide="ignore", invalid="ignore"):
        return ((s-m)/sd).values

def trade_sim(fac, c, pd_, dir_):
    """连续调仓回测（与合成器实际用法一致）：仓位 = dir×clip(z,-1,1)，万4换手费
    → (年化夏普, 活跃bar胜率, 累计利润率, 活跃占比)"""
    z = roll_z(fac, 3*pd_ + 20)
    pos = np.where(np.isfinite(z), dir_ * np.clip(z, -1, 1), 0.0)
    rets = np.concatenate([[0.0], c[1:]/c[:-1]-1])
    dpos = np.abs(np.diff(pos, prepend=0))
    strat = pos*rets - 0.0004*dpos
    sd = np.nanstd(strat)
    if sd <= 0 or np.isnan(sd): return np.nan, np.nan, np.nan, 0.0
    sharpe = float(np.nanmean(strat)/sd*np.sqrt(pd_*365))
    act = strat[np.abs(pos) > 0.25]
    win = float(np.mean(act > 0)) if len(act) >= 50 else np.nan
    pnl = float(np.nansum(strat))
    return sharpe, win, pnl, float(np.mean(np.abs(pos) > 0.25))

def os_hit_rate(fac, c, pd_, dir_):
    """超跌超涨：|z|>2 极值点后 2h 收益方向命中率"""
    z = roll_z(fac, 3*pd_ + 20)
    hz = max(1, pd_//12)
    fwd = np.concatenate([c[hz:]/c[:-hz]-1, [np.nan]*hz])
    mask = (np.abs(z) > 2) & np.isfinite(fwd)
    if mask.sum() < 30: return np.nan, int(mask.sum())
    hit = np.mean(np.sign(fwd[mask]*dir_*np.sign(z[mask])) > 0)
    return float(hit), int(mask.sum())

def ns_lift(alert, c, pd_):
    """消息面事件研究：警报后 1h |收益|>1.2% 的概率 vs 基线"""
    hz = max(1, pd_//24)
    fwd = np.abs(np.concatenate([c[hz:]/c[:-hz]-1, [np.nan]*hz]))
    big = (fwd > 0.012) & np.isfinite(fwd)
    base = big[np.isfinite(fwd)].mean()
    m = (alert > 0.5) & np.isfinite(fwd)
    if m.sum() < 20 or base <= 0: return np.nan, int(m.sum()), float(base)
    return float(big[m].mean()/base), int(m.sum()), float(base)

def main():
    db = sqlite3.connect("data/factor5000/factors.db")
    rows = db.execute("""SELECT f.id, f.cat, f.name, f.formula, f.tf, r.per_day
                         FROM factors f JOIN runnable r ON f.id = r.id""").fetchall()
    print(f"待回测因子 {len(rows)} 个（币安+OKX 双源）", flush=True)
    D, fund, oi = load_data()
    # 币安/OKX 各自的 ETH/BTC 汇率序列
    ratios = {}
    for src, b, e in [("bn","BTCUSDT","ETHUSDT"), ("okx","BTC-USDT-SWAP","ETH-USDT-SWAP")]:
        for tf in ["4h","1h","15m","5m","1d"] if src=="bn" else ["4H","1H","15m","5m","1D"]:
            kb, ke = f"{src}_{b}_{tf}", f"{src}_{e}_{tf}"
            if kb not in D or ke not in D: continue          # 该数据源整体缺失时跳过
            db_ = D[kb][["t","c"]].rename(columns={"c":"b"})
            de = D[ke][["t","c"]].rename(columns={"c":"e"})
            rr = pd.merge_asof(db_.sort_values("t"), de.sort_values("t"), on="t")
            ratios[(src, tf)] = pd.DataFrame({"t": rr["t"], "r": rr["e"]/rr["b"]})

    HZN = {"波段": lambda p: max(1,p), "日内": lambda p: max(1,p//6),
           "超跌超涨防护": lambda p: max(1,p//12), "消息面防护": lambda p: max(1,p//24)}

    env_cache = {}
    def get_env(src, sym, tf):
        key = (src, sym, tf)
        if key not in env_cache:
            bsfx, osfx = src_keys(tf)
            if src == "bn":
                df = D[f"bn_{sym}_{bsfx}"]
                if "30m" in tf: df = resample(df, "30min")
                if "8h" in tf: df = resample(df, "8h")
                env_cache[key] = build_env(df, fund.get(sym), oi.get(sym), ratios.get(("bn", bsfx)))
            else:
                o_sym = "BTC-USDT-SWAP" if sym == "BTCUSDT" else "ETH-USDT-SWAP"
                df = D.get(f"okx_{o_sym}_{osfx}")
                if df is None or len(df) < 200: env_cache[key] = None; return None
                if "30m" in tf: df = resample(df, "30min")
                if "8h" in tf: df = resample(df, "8h")
                env_cache[key] = build_env(df, None, None, ratios.get(("okx", osfx)))
        return env_cache[key]

    def eval_one(src_code, tf, pd_):
        """返回 {src: {sym: (fac, env)}}"""
        out = {}
        for src in ["bn", "okx"]:
            for sym in ["BTCUSDT", "ETHUSDT"]:
                env = get_env(src, sym, tf)
                if env is None: continue
                try:
                    fac = eval_factor(src_code, env)
                except Exception:
                    continue
                if fac is None or np.isnan(fac).all(): continue
                out.setdefault(src, {})[sym] = (fac, env)
        return out

    def analyze(fid, cat, name, formula, tf, pd_, hz):
        try:
            src_code = compile_formula(formula, pd_)
        except Exception:
            return None
        if "TODZ(" in src_code: return None
        got = eval_one(src_code, tf, pd_)
        if "bn" not in got or len(got["bn"]) < 2: return None
        # —— 样本内外切分：前60%定方向，后40%出绩效（防止方向过拟合） ——
        per_sym = {}
        for sym in ["BTCUSDT", "ETHUSDT"]:
            fac, env = got["bn"][sym]
            n = len(fac); c = env["c"]
            fwd = np.concatenate([c[hz:]/c[:-hz]-1, [np.nan]*hz])
            cut = int(n*0.6)
            icE, _ = spearman(fac[:cut], fwd[:cut])                 # 前60%：定方向
            icF, _ = spearman(fac, fwd)                             # 全期（参考）
            rw = min(n//5, max(14*pd_, 200))
            icR, _ = spearman(fac[n-rw:], fwd[n-rw:])               # 近期（参考）
            per_sym[sym] = (fac, env, cut, icE, icF, icR)
        ics = [x[3] for x in per_sym.values()]
        if any(np.isnan(x) for x in ics): return None
        icF = float(np.mean([x[4] for x in per_sym.values()]))
        icR = float(np.mean([x[5] for x in per_sym.values()]))
        # —— 方向确定：方向类因子用"前60%样本模拟盈亏方向"（P&L口径，与实盘一致）——
        if cat in ("波段", "日内", "超短线"):
            dirs = []
            for sym, (fac, env, cut, *_x) in per_sym.items():
                sp = trade_sim(fac[:cut], env["c"][:cut], pd_, +1)[0]
                sm = trade_sim(fac[:cut], env["c"][:cut], pd_, -1)[0]
                if np.isnan(sp) and np.isnan(sm): return None
                sp = sp if not np.isnan(sp) else -9e9
                sm = sm if not np.isnan(sm) else -9e9
                dirs.append(1 if sp >= sm else -1)
            dir_ = int(np.sign(sum(dirs))) or 1
            agree = 1.0 if dirs[0] == dirs[1] else 0.5
        else:
            dir_ = int(np.sign(ics[0]+ics[1])) or 1     # 防御类：IC 方向
            agree = 1.0 if np.sign(ics[0]) == np.sign(ics[1]) else 0.5
        # —— OKX 交叉验证（近期IC同号） ——
        okx_note, okx_ok = "—", None
        if "okx" in got and len(got["okx"]) == 2:
            oics = []
            for sym in ["BTCUSDT", "ETHUSDT"]:
                fac, env = got["okx"][sym]
                n = len(fac); c = env["c"]
                fwd = np.concatenate([c[hz:]/c[:-hz]-1, [np.nan]*hz])
                rw = min(n//2, max(7*pd_, 150))
                ic, _ = spearman(fac[n-rw:], fwd[n-rw:])
                oics.append(ic)
            if not any(np.isnan(x) for x in oics):
                okx_ok = all(np.sign(x) == dir_ for x in oics)
                okx_note = f"{'✓' if okx_ok else '✗'}{oics[0]:+.3f}/{oics[1]:+.3f}"
        # —— 分类回测指标（后40%样本外，币安双币均值） ——
        sharpe = win = hit = lift = pnl = np.nan
        if cat in ("波段", "日内", "超短线"):
            ss = []
            for sym, (fac, env, cut, *_x) in per_sym.items():
                s1 = trade_sim(fac[cut:], env["c"][cut:], pd_, dir_)
                if not np.isnan(s1[0]): ss.append(s1)
            if ss:
                sharpe = float(np.mean([x[0] for x in ss]))
                win = float(np.mean([x[1] for x in ss if not np.isnan(x[1])] or [np.nan]))
                pnl = float(np.mean([x[2] for x in ss if not np.isnan(x[2])] or [np.nan]))
        elif cat == "超跌超涨防护":
            hh = []
            for sym, (fac, env, cut, *_x) in per_sym.items():
                h1_, n1 = os_hit_rate(fac[cut:], env["c"][cut:], pd_, dir_)
                if not np.isnan(h1_): hh.append(h1_)
            if hh: hit = float(np.mean(hh))
        elif cat == "消息面防护":
            ll = []
            for sym, (fac, env, cut, *_x) in per_sym.items():
                l1, n1, b1 = ns_lift(fac[cut:], env["c"][cut:], pd_)
                if not np.isnan(l1): ll.append(l1)
            if ll: lift = float(np.mean(ll))
        return {"id": fid, "cat": cat, "name": name, "formula": formula, "tf": tf,
                "per_day": pd_, "dir": dir_, "icF": round(icF,4), "icR": round(icR,4),
                "agree": agree, "okx": okx_note, "okx_ok": okx_ok,
                "sharpe": None if np.isnan(sharpe) else round(sharpe,2),
                "win": None if (win is None or np.isnan(win)) else round(win,3),
                "pnl": None if np.isnan(pnl) else round(pnl,4),
                "hit": None if np.isnan(hit) else round(hit,3),
                "lift": None if np.isnan(lift) else round(lift,2),
                "src": src_code}

    def tier_of(r):
        """分类有效性判定：方向类以样本外模拟夏普为主，防御类以命中率/提升倍数为主"""
        if r["cat"] in ("波段","日内","超短线"):
            s = r["sharpe"]
            if s is None: return "low" if abs(r["icR"]) >= 0.004 else None
            if s > 0.3 and (r["win"] is None or r["win"] >= 0.5) and r["agree"] >= 1.0:
                return "high"
            if s > 0: return "mid"
            if s > -2: return "low"
            return None
        cons = r["icR"] * r["dir"] > 0          # 近期仍同向
        if r["cat"] == "超跌超涨防护":
            if cons and r["hit"] is not None and r["hit"] >= 0.52 and abs(r["icR"]) >= 0.004:
                return "high"
            if cons and abs(r["icR"]) >= 0.002: return "mid"
            if abs(r["icR"]) >= 0.002: return "low"
        else:  # 消息面：事件型因子以提升倍数为主，方向IC仅参考
            if r["lift"] is not None and r["lift"] >= 1.15: return "high"
            if r["lift"] is not None and r["lift"] >= 1.0: return "mid"
            if abs(r["icR"]) >= 0.002 or (r["lift"] is not None and r["lift"] >= 0.9): return "low"
        return None

    results = []
    t0 = time.time()
    for i, (fid, cat, name, formula, tf, pd_) in enumerate(rows):
        if i % 200 == 0:
            print(f"  {i}/{len(rows)} 用时{time.time()-t0:.0f}s", flush=True)
        hz = HZN.get(cat, lambda p: max(1, p//6))(pd_)
        r = analyze(fid, cat, name, formula, tf, pd_, hz)
        if not r: continue
        conf = tier_of(r)
        if not conf: continue
        r["conf"] = conf
        w = abs(r["icR"]) * r["agree"]
        if r["okx_ok"] is False: w *= 0.5
        elif r["okx_ok"] is None: w *= 0.75
        if cat in ("波段","日内","超短线") and r["sharpe"] is not None and r["sharpe"] <= 0:
            w *= 0.2                      # 样本外模拟亏损的，权重降为1/5
        r["w"] = round(w, 5)
        results.append(r)
    print(f"\n通过有效性门槛: {len(results)} "
          f"(high={sum(1 for r in results if r['conf']=='high')}, "
          f"mid={sum(1 for r in results if r['conf']=='mid')}, "
          f"low={sum(1 for r in results if r['conf']=='low')})", flush=True)

    # ===== 超短线动态池：全库 15m/5m 短周期因子按 30分钟方法独立筛选 =====
    sc_results = []
    for fid, cat, name, formula, tf, pd_ in rows:
        if pd_ < 96: continue
        hz = max(1, pd_//48)
        r = analyze(fid, "超短线", name, formula, tf, pd_, hz)
        if not r: continue
        conf = tier_of(r)
        if not conf: continue
        r["conf"] = conf
        w = abs(r["icR"]) * r["agree"]
        if r["okx_ok"] is False: w *= 0.5
        elif r["okx_ok"] is None: w *= 0.75
        if r["sharpe"] is not None and r["sharpe"] <= 0:
            w *= 0.2
        r["w"] = round(w, 5)
        sc_results.append(r)
    print(f"超短线动态池: {len(sc_results)}", flush=True)
    results.extend(sc_results)

    pd.DataFrame([{k: v for k, v in r.items() if k != "src"} for r in results]).to_csv(
        "data/factor5000/ic_all.csv", index=False, encoding="utf-8-sig")

    selected = {}
    for cat in ["波段", "日内", "超跌超涨防护", "消息面防护", "超短线"]:
        pool = [r for r in results if r["cat"] == cat]
        pool.sort(key=lambda r: -r["w"])
        picked = pool[:100]          # 每天该类别权重 Top100 上岗（5000全库存库备用）
        if len(picked) < 100:
            print(f"  ⚠️ {cat}: 有效因子仅 {len(picked)} 个，不足100，全部上岗", flush=True)
        selected[cat] = picked
        if pool:
            s_ = [r["sharpe"] for r in picked if r["sharpe"] is not None]
            extra = f"样本外夏普均值 {np.mean(s_):.2f}" if s_ else ""
            print(f"  {cat}: 池{len(pool)} → 今日上岗{len(picked)} {extra}（榜首 {picked[0]['id']} {picked[0]['name']} sharpe={picked[0]['sharpe']}）", flush=True)
    library = [{"id": r[0], "cat": r[1], "name": r[2], "formula": r[3], "tf": r[4],
                "runnable": 1 if r[5] else 0}
               for r in db.execute("""SELECT f.id, f.cat, f.name, f.formula, f.tf,
                                      CASE WHEN r.id IS NULL THEN 0 ELSE 1 END
                                      FROM factors f LEFT JOIN runnable r ON f.id=r.id""")]
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "selected": selected,
           "counts": {k: len(v) for k, v in selected.items()},
           "method": "波段/日内/超短线=模拟交易夏普+IC；超跌超涨=极值反转命中率；消息面=事件研究提升倍数；币安+OKX双源验证",
           "library_total": len(library), "library": library}
    with open("data/factor5000/selected_top500.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("已输出 data/factor5000/selected_top500.json", flush=True)

if __name__ == "__main__":
    main()
