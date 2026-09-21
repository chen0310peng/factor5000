# -*- coding: utf-8 -*-
"""live_engine.py — 云端 24h 波段/日内交易引擎（GitHub Actions 每 15 分钟运行）

根治问题（2026-09-06）：
  旧架构三个引擎全部跑在浏览器里，页面关了策略就失明——止损/止盈/反转平仓都不会执行，
  8-29 追空开在全天最低点、浮盈回吐无人管，都是这个病根。
  本脚本把【波段 + 日内】两个引擎完整搬到云端：信号计算、仓位管理、止盈止损全部
  在 GitHub Actions 上每 15 分钟执行一次，状态持久化在 data/live_state.json 随仓库提交，
  网页只负责展示（超短线 5m 周期不适合 Actions 频率，仍留在浏览器端）。

与网页版完全一致的逻辑（逐行移植自 BTC_ETH实时策略信号.html）：
  computeSignal / computeIntraday / dynComputeAll / swingKelly / intraRegimeGate /
  computeDefense / macroGate / timeGate / defGate / tradeEngine / tradeEngineIntra /
  armUpdate / chaseCheck

新增（2026-09-06，根治"波段有利润不跑、持有过久"）：
  SW_TRAIL_ARM_ATR   浮盈 ≥ 0.5×ATR 激活回撤保护（此前 0~1×ATR 的浮盈完全裸奔）
  SW_TRAIL_GIVEBACK  浮盈从峰值回吐 ≥50% → 平仓落袋
  SW_TIME_STOP_DAYS  持仓 ≥5 天仍未达止盈1 → 止损上移至成本（日内早有此规则，波段漏加）
  SW_MAX_HOLD_DAYS   持仓 ≥15 天 → 市价强平（杜绝无限期僵尸持仓）
"""
import os, sys, json, time, math, argparse
import requests
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsl  # noqa: E402  复用云端因子公式函数库（HMA/RSI/ATR...）
try:
    import structure as stmod   # noqa: E402  市场结构识别（OB订单块/FVG/摆动点）
except Exception:
    stmod = None

# ===================== 配置 =====================
STATE_PATH   = os.environ.get("LIVE_STATE", "data/live_state.json")
SELECT_PATH  = "data/factor5000/selected_top500.json"
SYMS         = ["BTC", "ETH"]
BN_SYMS      = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
OKX_INST     = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP"}

BN_HOSTS = ["https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com",
            "https://fapi3.binance.com", "https://fapi4.binance.com"]
OKX = "https://www.okx.com"
S = requests.Session(); S.headers.update({"User-Agent": "Mozilla/5.0"})

# —— 波段新增保护规则（见文件头注释） ——
SW_TRAIL_ARM_ATR   = 0.5   # 浮盈达到 0.5×ATR 激活回撤保护
SW_TRAIL_GIVEBACK  = 0.5   # 从峰值回吐 50% 平仓（TAPER_GIVEBACK 开启时被递减比例替代）
SW_TIME_STOP_DAYS  = 5     # ≥5 天未达止盈1 → 止损移成本
SW_MAX_HOLD_DAYS   = 15    # ≥15 天 → 强平

# —— 结构化止盈止损（2026-09-21，structure.py；根治"有盈利保护不住"） ——
STRUCT_ON        = False   # 开仓止损锚定结构位——2026-09-21回测否定（止损次数翻倍），保留代码备用
STRUCT_TP_ALIGN  = True    # 止盈位与前方反向结构区对齐（到结构位先落袋）
LADDER_LOCK_ON   = True    # 阶梯锁利：浮盈≥1/2/3×ATR → 止损→成本/锁1ATR/吊灯(峰值-1.5ATR)
TAPER_GIVEBACK   = True    # 递减回吐：峰值<1/2/3/≥3×ATR 允许回吐 60/45/35/25%（替代一刀切50%）
STRUCT_TRAIL_ON  = True    # 结构跟随：新的同向结构位形成时止损跟上（只向盈利方向）
STRUCT_SHADOW    = True    # 影子模式：只记录"本应如何"，不实际执行（观察期后关闭）
# —— 可调参数（回测扫参与生产微调共用） ——
STOP_MIN_ATR     = 0.8     # 结构止损最近距离（防噪声扫损）
STOP_MAX_ATR     = 2.5     # 结构止损最远距离（超过则回退1.5×ATR经典止损）
LAD_T1, LAD_T2, LAD_T3 = 1.0, 2.0, 3.0   # 阶梯触发档（×ATR）
LAD_LOCK2        = 1.0     # 第二档锁定利润（×ATR）
CHAND_K          = 1.5     # 吊灯：峰值价 - CHAND_K×ATR
GB_TIERS         = (0.60, 0.45, 0.35, 0.25)   # 峰值<1/<2/<3/≥3×ATR 的允许回吐比例
GB_ARM_ATR       = 1.5     # 回撤保护启动门槛（回测定案：0.5太敏感掐死小浮盈，1.5让阶梯锁利先干活）

# 防御层灵敏度（与网页 standard 档一致）
DEFTH = dict(nr7=0.14, fundZ=2.0, pinZ=2.5, pinVol=1.5, taker=0.03,
             crush=0.40, shift=0.25, corr=0.50, oi=-0.02)

# 宏观事件日历（与网页一致，有效期至 2026 年底，UTC 毫秒）
MACRO_EVENTS = [
    ("非农就业",        "2026-09-04T12:30:00Z"),
    ("CPI",             "2026-09-11T12:30:00Z"),
    ("FOMC利率决议+SEP", "2026-09-16T18:00:00Z"),
    ("非农就业",        "2026-10-02T12:30:00Z"),
    ("CPI",             "2026-10-14T12:30:00Z"),
    ("FOMC利率决议",    "2026-10-28T18:00:00Z"),
    ("非农就业",        "2026-11-06T13:30:00Z"),
    ("CPI",             "2026-11-10T13:30:00Z"),
    ("非农就业",        "2026-12-04T13:30:00Z"),
    ("FOMC利率决议+SEP", "2026-12-09T19:00:00Z"),
    ("CPI",             "2026-12-10T13:30:00Z"),
]

# ===================== 数学工具（与网页同定义） =====================
def mean(a): return float(np.mean(a)) if len(a) else float("nan")
def std(a):  return float(np.std(a)) if len(a) else float("nan")          # 总体std，同JS
def clamp(x, a, b): return min(b, max(a, x))

def z_last(s, w=60):
    x = [v for v in s if v is not None and np.isfinite(v)][-w:]
    if not x: return 0.0
    m, sd = mean(x), std(x)
    return (s[-1] - m) / (sd or 1e-12)

def roll_mean(a, w):
    return pd.Series(a, dtype=float).rolling(w).mean().tolist()

def atr14(h, l, c):
    t = []
    for i in range(1, len(c)):
        t.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return mean(t[-14:])

def corr(a, b):
    n = min(len(a), len(b))
    if n < 3: return 0.0
    a, b = np.asarray(a[-n:], float), np.asarray(b[-n:], float)
    sa, sb = a.std(), b.std()
    if sa == 0 or sb == 0: return 0.0
    return float(np.corrcoef(a, b)[0, 1])

def bj_str(ms=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime((ms or now_ms())/1000 + 8*3600))

def now_ms(): return int(time.time()*1000)

# ===================== 数据拉取（币安5镜像 + OKX全量兜底） =====================
def _get(base, path, params=None, retries=2, timeout=20):
    last = None
    for i in range(retries):
        try:
            r = S.get(base+path, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = e
        time.sleep(1.5*(i+1))
    raise RuntimeError(f"GET {base}{path} failed: {last}")

def bn_get(path, params=None):
    last = None
    for h in BN_HOSTS:
        try: return _get(h, path, params)
        except Exception as e: last = e
    raise RuntimeError(f"币安5镜像全部不可用: {path} ({last})")

def okx_candles(inst, bar, need):
    out, after = [], ""
    while len(out) < need:
        j = _get(OKX, f"/api/v5/market/candles?instId={inst}&bar={bar}&limit=300{after}", timeout=15)
        d = j.get("data") or []
        if not d: break
        out += d
        if len(d) < 300: break
        after = "&after=" + d[-1][0]
    out.reverse()
    return out

def fetch_binance(sym):
    s = BN_SYMS[sym]
    k    = bn_get("/fapi/v1/klines", {"symbol": s, "interval": "1d", "limit": 500})
    f    = bn_get("/fapi/v1/fundingRate", {"symbol": s, "limit": 1000})
    k1h  = bn_get("/fapi/v1/klines", {"symbol": s, "interval": "1h", "limit": 1500})
    k4h  = bn_get("/fapi/v1/klines", {"symbol": s, "interval": "4h", "limit": 1500})
    k15  = bn_get("/fapi/v1/klines", {"symbol": s, "interval": "15m", "limit": 1500})
    k5   = bn_get("/fapi/v1/klines", {"symbol": s, "interval": "5m", "limit": 1500})
    try: oi   = bn_get("/futures/data/openInterestHist", {"symbol": s, "period": "5m", "limit": 100})
    except Exception: oi = []
    try: oi1h = bn_get("/futures/data/openInterestHist", {"symbol": s, "period": "1h", "limit": 500})
    except Exception: oi1h = []
    return {
        "close": [float(x[4]) for x in k], "high": [float(x[2]) for x in k],
        "low": [float(x[3]) for x in k],   "vol": [float(x[5]) for x in k],
        "open1d": [float(x[1]) for x in k], "tb1d": [float(x[9]) for x in k],
        "t1d": [int(x[0]) for x in k],
        "fund": [{"t": int(x["fundingTime"]), "r": float(x["fundingRate"])} for x in f],
        "h1": [{"t": int(x[0]), "c": float(x[4])} for x in k1h],
        "now": float(k1h[-1][4]),
        "k1h": {"t": [int(x[0]) for x in k1h], "o": [float(x[1]) for x in k1h],
                "h": [float(x[2]) for x in k1h], "l": [float(x[3]) for x in k1h],
                "c": [float(x[4]) for x in k1h], "v": [float(x[5]) for x in k1h],
                "tb": [float(x[9]) for x in k1h]},
        "h4": {"t": [int(x[0]) for x in k4h], "o": [float(x[1]) for x in k4h],
               "h": [float(x[2]) for x in k4h], "l": [float(x[3]) for x in k4h],
               "c": [float(x[4]) for x in k4h], "v": [float(x[5]) for x in k4h],
               "tb": [float(x[9]) for x in k4h]},
        "k15": [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                 "c": float(x[4]), "v": float(x[5]), "tb": float(x[9])} for x in k15],
        "k5": [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                "c": float(x[4]), "v": float(x[5]), "tb": float(x[9])} for x in k5],
        "oi":   [{"t": int(x["timestamp"]), "v": float(x["sumOpenInterest"])} for x in (oi or [])],
        "oi1h": [{"t": int(x["timestamp"]), "v": float(x["sumOpenInterest"])} for x in (oi1h or [])],
    }

def fetch_okx_full(sym):
    """币安全镜像不可达时的 OKX 兜底（tb 以 v/2 填充，taker 类因子降级中性）"""
    inst = OKX_INST[sym]; ccy = inst.split("-")[0]
    k   = okx_candles(inst, "1Dutc", 500)
    k1h = okx_candles(inst, "1H", 1500)
    k4h = okx_candles(inst, "4H", 1500)
    k15 = okx_candles(inst, "15m", 1500)
    k5  = okx_candles(inst, "5m", 1500)
    if not (k and k1h and k4h and k5): raise RuntimeError("OKX 兜底数据不完整")
    try: f = _get(OKX, f"/api/v5/public/funding-rate-history?instId={inst}&limit=100", timeout=15)
    except Exception: f = {"data": []}
    try: oi = _get(OKX, f"/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=5m", timeout=15)
    except Exception: oi = {"data": []}
    try: oi1h = _get(OKX, f"/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1H", timeout=15)
    except Exception: oi1h = {"data": []}
    A = lambda a: [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                    "c": float(x[4]), "v": float(x[5]), "tb": float(x[5])/2} for x in a]
    K1, K4, K15, K5 = A(k1h), A(k4h), A(k15), A(k5)
    return {
        "close": [float(x[4]) for x in k], "high": [float(x[2]) for x in k],
        "low": [float(x[3]) for x in k],   "vol": [float(x[5]) for x in k],
        "open1d": [float(x[1]) for x in k], "tb1d": [float(x[5])/2 for x in k],
        "t1d": [int(x[0]) for x in k],
        "fund": [{"t": int(x["fundingTime"]), "r": float(x["fundingRate"])}
                 for x in reversed(f.get("data") or [])],
        "h1": [{"t": x["t"], "c": x["c"]} for x in K1],
        "now": K1[-1]["c"] if K1 else 0,
        "k1h": {kk: [x[kk] for x in K1] for kk in ("t","o","h","l","c","v","tb")},
        "h4":  {kk: [x[kk] for x in K4] for kk in ("t","o","h","l","c","v","tb")},
        "k15": K15, "k5": K5,
        "oi":   [{"t": int(x[0]), "v": float(x[1])} for x in reversed(oi.get("data") or [])],
        "oi1h": [{"t": int(x[0]), "v": float(x[1])} for x in reversed(oi1h.get("data") or [])],
        "srcOKX": True,
    }

def fetch_one(sym):
    try: return fetch_binance(sym)
    except Exception as e:
        print(f"  ⚠️ {sym} 币安不可达({e})，切 OKX 兜底", flush=True)
        return fetch_okx_full(sym)

# ===================== 动态因子层（移植 dynComputeAll） =====================
def asof_arr(t_arr, aux_t, aux_v):
    """移植 dynAsof：把 aux 序列按时间戳前向对齐到 t_arr"""
    out = np.full(len(t_arr), np.nan)
    if not len(aux_t): return out
    s = pd.Series(np.asarray(aux_v, float), index=pd.to_datetime(aux_t, unit="ms"))
    return s.reindex(pd.to_datetime(t_arr, unit="ms"), method="ffill").values

def resample_k(k, step):
    out = []
    for i in range(0, len(k)-step+1, step):
        seg = k[i:i+step]
        out.append({"t": seg[0]["t"], "o": seg[0]["o"],
                    "h": max(x["h"] for x in seg), "l": min(x["l"] for x in seg),
                    "c": seg[-1]["c"], "v": sum(x["v"] for x in seg),
                    "tb": sum(x.get("tb", x["v"]*0.5) for x in seg)})
    return out

def mk_env(k_arr, fund, oi1h, ratio_c):
    """移植 dynMkEnv"""
    n = len(k_arr)
    t  = np.array([x["t"] for x in k_arr], dtype=np.int64)
    c  = np.array([x["c"] for x in k_arr], float)
    o  = np.array([x.get("o", x["c"]) for x in k_arr], float)
    h  = np.array([x["h"] for x in k_arr], float)
    l  = np.array([x["l"] for x in k_arr], float)
    v  = np.array([x["v"] for x in k_arr], float)
    tb = np.array([x.get("tb", x["v"]*0.5) for x in k_arr], float)
    fund_a = asof_arr(t, [x["t"] for x in (fund or [])], [x["r"] for x in (fund or [])])
    oi_a   = asof_arr(t, [x["t"] for x in (oi1h or [])], [x["v"] for x in (oi1h or [])])
    ret = np.concatenate([[np.nan], c[1:]/c[:-1]-1])
    day = (t//86400000).astype(float)
    tod = ((t//60000) % 1440).astype(float)
    tp = (h+l+c)/3
    pv = pd.Series(tp*v).groupby(day).cumsum().values
    vv = pd.Series(v).groupby(day).cumsum().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = pv/vv
    ratio = np.array(ratio_c, float) if ratio_c is not None else np.full(n, np.nan)
    return dict(c=c, o=o, h=h, l=l, v=v, tb=tb, t=t, fund=fund_a, oi_=oi_a,
                ratio=ratio, ret=ret, vwap=vwap, tod=tod, day=day, ABS=np.abs)

def tf_key(tf):
    if "15m" in tf: return "m15"
    if "5m" in tf or "1m" in tf or "3m" in tf: return "m5"
    if "30m" in tf: return "m30"
    if "8h" in tf: return "h8"
    if "4h" in tf: return "h4"
    if "1d" in tf or "日" in tf: return "d1"
    return "h1"

def eval_factor(src, env):
    g = {k: getattr(dsl, k) for k in dir(dsl) if not k.startswith("_")}
    g.update(env); g["np"] = np
    exec(compile(src, "<f>", "exec"), g)
    out = g.get("OUT")
    if out is None: return None
    arr = np.asarray(out, dtype=float) if np.ndim(out) else np.full(len(env["c"]), float(out))
    return arr if len(arr) == len(env["c"]) else None

def roll_z200(arr):
    """移植 dynRollZ(arr,200)：滑窗200、最少30个有效值、总体std、截断±4"""
    s = pd.Series(arr, dtype=float)
    m = s.rolling(200, min_periods=30).mean()
    sd = s.rolling(200, min_periods=30).std(ddof=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = ((s-m)/sd).clip(-4, 4)
    return z.values

def dyn_compute_all(bn, dyn500):
    """移植 dynComputeAll：返回 {sym: {sw,id,os,sc:{score,live,total,top}, swSeries,idSeries, ns}}"""
    out = {}
    cat_key = {"波段": "sw", "日内": "id", "超跌超涨防护": "os", "超短线": "sc"}
    selected = (dyn500 or {}).get("selected", {})
    rB, rE = bn.get("BTC"), bn.get("ETH")
    for k in SYMS:
        d = bn.get(k)
        if not d: continue
        kD1 = [{"t": d["t1d"][i], "o": d["open1d"][i], "h": d["high"][i], "l": d["low"][i],
                "c": d["close"][i], "v": d["vol"][i], "tb": d["tb1d"][i]} for i in range(len(d["close"]))]
        kH4 = [{"t": d["h4"]["t"][i], "o": d["h4"]["o"][i], "h": d["h4"]["h"][i], "l": d["h4"]["l"][i],
                "c": d["h4"]["c"][i], "v": d["h4"]["v"][i], "tb": d["h4"]["tb"][i]} for i in range(len(d["h4"]["t"]))]
        kH1 = [{"t": d["k1h"]["t"][i], "o": d["k1h"]["o"][i], "h": d["k1h"]["h"][i], "l": d["k1h"]["l"][i],
                "c": d["k1h"]["c"][i], "v": d["k1h"]["v"][i], "tb": d["k1h"]["tb"][i]} for i in range(len(d["k1h"]["t"]))]
        k15, k5 = d["k15"], d["k5"]

        def ratio_for(k_arr, tfk):
            if not (rB and rE): return None
            def get_k(dd):
                if tfk == "d1": return [{"t": dd["t1d"][i], "c": dd["close"][i]} for i in range(len(dd["close"]))]
                if tfk == "h4": return [{"t": dd["h4"]["t"][i], "c": dd["h4"]["c"][i]} for i in range(len(dd["h4"]["t"]))]
                if tfk == "h1": return [{"t": dd["k1h"]["t"][i], "c": dd["k1h"]["c"][i]} for i in range(len(dd["k1h"]["t"]))]
                if tfk == "m15": return dd["k15"]
                return dd["k5"]
            mB = {x["t"]: x["c"] for x in get_k(rB)}
            mE = {x["t"]: x["c"] for x in get_k(rE)}
            return [ (mE[x["t"]]/mB[x["t"]]) if (x["t"] in mE and mB.get(x["t"])) else np.nan for x in k_arr ]

        envs = {}
        def mk(k_arr, tfk):
            if not len(k_arr): return None
            return mk_env(k_arr, d["fund"], d["oi1h"], ratio_for(k_arr, tfk))
        envs["d1"]  = mk(kD1, "d1");  envs["h4"] = mk(kH4, "h4")
        envs["h8"]  = mk(resample_k(kH4, 2), "h4") if len(kH4) else None
        envs["h1"]  = mk(kH1, "h1") if len(kH1) else None
        envs["m30"] = mk(resample_k(k15, 2), "m15") if len(k15) else None
        envs["m15"] = mk(k15, "m15") if len(k15) else None
        envs["m5"]  = mk(k5, "m5") if len(k5) else None
        out[k] = {"envs": envs}

        for cat in ("波段", "日内", "超跌超涨防护", "超短线"):
            pool = selected.get(cat, [])
            grid = envs["m15"] if cat == "日内" else envs["m5"] if cat == "超短线" else envs["d1"] if cat == "波段" else None
            ssum = wsum = 0.0; live = 0; top = []
            acc = wacc = None
            for f in pool:
                env = envs.get(tf_key(f.get("tf", "")))
                if env is None or len(env["c"]) < 60: continue
                try:
                    arr = eval_factor(f["src"], env)
                    if arr is None or not len(arr): continue
                    last = arr[-1]
                    if not np.isfinite(last): continue
                    tail = arr[np.isfinite(arr)][-200:]
                    if len(tail) < 30: continue
                    m, sd = float(np.mean(tail)), float(np.std(tail))
                    z = clamp((last-m)/(sd or 1e-12), -4, 4)
                    if not np.isfinite(z): continue
                except Exception:
                    continue
                contrib = f["dir"]*z*f["w"]
                ssum += contrib; wsum += f["w"]; live += 1
                top.append({"id": f["id"], "name": f["name"], "contrib": contrib, "z": z})
                if grid is not None:
                    try:
                        al = asof_arr(grid["t"], env["t"], roll_z200(arr))
                        if acc is None:
                            acc = np.zeros(len(grid["t"])); wacc = np.zeros(len(grid["t"]))
                        msk = np.isfinite(al)
                        acc[msk] += f["dir"]*al[msk]*f["w"]; wacc[msk] += f["w"]
                    except Exception:
                        pass
            top.sort(key=lambda x: -abs(x["contrib"]))
            out[k][cat_key[cat]] = {"score": clamp(ssum/wsum, -3, 3) if wsum else 0.0,
                                    "live": live, "total": len(pool), "top": top[:5]}
            if grid is not None and acc is not None:
                with np.errstate(divide="ignore", invalid="ignore"):
                    ser = np.where(wacc > 0, np.clip(acc/np.where(wacc == 0, 1, wacc), -3, 3), np.nan)
                s = {"t": grid["t"].tolist(), "c": grid["c"].tolist(), "s": ser.tolist()}
                if cat == "日内": out[k]["idSeries"] = s
                elif cat == "波段": out[k]["swSeries"] = s
                elif cat == "超短线": out[k]["scSeries"] = s

        # 消息面防护：布尔警报逐条求值，触发即冻结
        trig = []; ns_live = 0
        for f in selected.get("消息面防护", []):
            env = envs.get(tf_key(f.get("tf", "")))
            if env is None or len(env["c"]) < 60: continue
            try:
                arr = eval_factor(f["src"], env)
                if arr is None or not len(arr): continue
                ns_live += 1
                last = arr[-1]
            except Exception:
                continue
            if last == 1 or last is True:
                import re
                mm = re.search(r"冻结(?:新开仓)?(\d+)分钟", f.get("formula", ""))
                trig.append({"id": f["id"], "name": f["name"], "freeze": int(mm.group(1)) if mm else 30})
        out[k]["ns"] = {"triggered": trig, "live": ns_live,
                        "total": len(selected.get("消息面防护", []))}
    return out

# ===================== 波段信号（移植 computeSignal，日K） =====================
def compute_signal(d):
    c, v = d["close"], d["vol"]; n = len(c)
    ret = [None] + [c[i]/c[i-1]-1 for i in range(1, n)]
    v20 = roll_mean(v, 20)
    vshock = [ (v[i]/v20[i]) if (v20[i] is not None and np.isfinite(v20[i]) and v20[i] != 0) else None for i in range(n) ]
    fund_daily = {}
    for x in d["fund"]:
        day = time.strftime("%Y-%m-%d", time.gmtime(x["t"]/1000))
        fund_daily.setdefault(day, []).append(x["r"])
    fund30 = [m for m in roll_mean([mean(a) for a in fund_daily.values()], 30)
              if m is not None and np.isfinite(m)]   # JS rollMean 头部为 null 会被过滤；pandas 头部是 NaN，必须按 finite 过滤
    zV = -z_last([x for x in vshock if x is not None])
    zF = -z_last(fund30) if fund30 else 0.0
    S = clamp((zV+zF)/2, -3, 3)
    dyn = d.get("dynSW")
    if dyn is not None and np.isfinite(dyn):
        S = clamp(S*0.3 + dyn*1.0, -3, 3)   # 30%静态锚 + 100%动态层
    base = 1 if S >= 1 else 0.5 if S >= 0.5 else 0 if S > -0.5 else -0.5 if S > -1 else -1
    ma200 = mean(c[-200:])
    above = c[-1] > ma200
    if base > 0 and not above: base *= 0.3
    r20 = [x for x in ret[-21:-1] if x is not None]
    vol_ann = std(r20)*math.sqrt(365) if r20 else 1e-9
    vol_mult = 0.5 if vol_ann > 0.8 else 1
    vt = clamp(0.5/(vol_ann or 1e-9), 0.2, 1.5)
    final_pos = base*vol_mult*vt
    atr = atr14(d["high"], d["low"], c)
    fund_now = d["fund"][-1]["r"] if d["fund"] else 0
    risk = clamp(vol_ann/1.2, 0, 1)*45 + clamp(abs(fund_now)/0.0005, 0, 1)*20 \
         + (0 if above else 20) + clamp(atr/c[-1]/0.05, 0, 1)*15
    risk = round(clamp(risk, 0, 100))
    # 静态锚凯利回退值（与网页同：近120日模拟胜率/赔率）
    def z_series(s, w=60):
        out = []
        for i in range(len(s)):
            if i < w: out.append(None); continue
            win = s[i-w:i]
            out.append((s[i]-mean(win))/((std(win)) or 1e-12))
        return out
    vsz = z_series([x for x in vshock if x is not None and np.isfinite(x)])
    f30z = z_series(fund30) if fund30 else []
    off = n - len(vsz)
    sim = []; prev_pos = 0
    for i in range(max(60, off+60), n-1):
        j = i - off
        if j >= len(vsz) or not len(f30z): break
        # JS 语义：null 经一元负号强制为 -0 → 这里 None/NaN 一律按 0.0 处理（与网页一致且不炸）
        vj = vsz[j]; fj = f30z[min(j, len(f30z)-1)]
        V = -(vj if (vj is not None and np.isfinite(vj)) else 0.0)
        F = -(fj if (fj is not None and np.isfinite(fj)) else 0.0)
        s_ = clamp((V+F)/2, -3, 3)
        b = 1 if s_ >= 1 else 0.5 if s_ >= 0.5 else 0 if s_ > -0.5 else -0.5 if s_ > -1 else -1
        if b > 0 and c[i] <= mean(c[max(0, i-200):i]): b *= 0.3
        rv = std([x for x in ret[max(1, i-20):i] if x is not None])*math.sqrt(365)
        b *= (0.5 if rv > 0.8 else 1)*clamp(0.5/(rv or 1e-9), 0.2, 1.5)
        sim.append({"r": b*(c[i+1]/c[i]-1) - 0.0004*abs(b-prev_pos), "pos": b})
        prev_pos = b
    rs = [x["r"] for x in sim[-120:]]
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]
    p = len(wins)/(len(rs) or 1)
    b_ratio = mean(wins)/abs(mean(losses)) if wins and losses else 1
    kelly = clamp(p-(1-p)/(b_ratio or 1e-9), 0, 1)
    return {"S": S, "zV": zV, "zF": zF, "above": above, "ma200": ma200, "volAnn": vol_ann,
            "finalPos": final_pos, "atr": atr, "risk": risk, "winRate": p, "bRatio": b_ratio,
            "kelly": kelly, "halfKelly": kelly/2, "price": d.get("now") or c[-1],
            "dayClose": c[-1], "fundNow": fund_now, "dynSW": dyn,
            "hi": d["high"], "lo": d["low"]}

# ===================== 日内信号（移植 computeIntraday，4h） =====================
def compute_intraday(d, sym, state):
    h4 = d["h4"]; c, v = h4["c"], h4["v"]; n = len(c)
    ret4 = [None] + [c[i]/c[i-1]-1 for i in range(1, n)]
    v6 = roll_mean(v, 6)
    volr = [ (v[i]/v6[i]) if v6[i] else None for i in range(n) ]
    asym = [ (ret4[i]*volr[i]) if (ret4[i] is not None and volr[i]) else None for i in range(n) ]
    mom24 = [ (c[i]/c[i-6]-1) if i >= 6 else None for i in range(n) ]
    trs = [ h4["h"][0]-h4["l"][0] ] + [ max(h4["h"][i]-h4["l"][i],
          abs(h4["h"][i]-c[i-1]), abs(h4["l"][i]-c[i-1])) for i in range(1, n) ]
    atr4 = mean(trs[-14:])
    def zW(arr):
        a = [x for x in arr if x is not None and np.isfinite(x)]
        w = a[-42:]
        return (a[-1]-mean(w))/((std(w)) or 1e-12)
    sig = clamp((-zW(asym) - zW(mom24))/2, -3, 3)
    dyn = d.get("dynID")
    if dyn is not None and np.isfinite(dyn):
        sig = clamp(sig*0.3 + dyn*1.0, -3, 3)
    # EMA 平滑（α=0.3，持久化在 state）
    sema = state.setdefault("sema", {})
    prev = sema.get(sym, sig)
    sig = clamp(prev*0.7 + sig*0.3, -3, 3)
    sema[sym] = sig
    hour_utc = time.gmtime(h4["t"][-1]/1000).tm_hour
    return {"sig": sig, "atr4": atr4, "price": c[-1], "hourUTC": hour_utc,
            "dynID": dyn, "hi": h4["h"], "lo": h4["l"]}

# ===================== 消息面防御层（移植 computeDefense / macroGate / timeGate / defGate） =====================
def compute_defense(sym, d, state):
    K = d["k5"]; n = len(K)
    c = [x["c"] for x in K]; v = [x["v"] for x in K]
    ret = [None] + [c[i]/c[i-1]-1 for i in range(1, n)]
    win = 288
    base = [x for x in ret[-win-1:-1] if x is not None]
    mu, sd = mean(base), std(base) or 1e-9
    z_now = ((ret[-1] or 0)-mu)/sd
    vol_mean = mean(v[-win:]) or 1e-9
    vol_burst = v[-1]/vol_mean
    rv2h = std([x for x in ret[-24:] if x is not None])
    vol_comp = rv2h/(sd or 1e-9)
    vol1h = sum(v[-12:])
    vol_dry = vol1h/(vol_mean*12)
    h1 = [x["c"] for x in d["h1"]]
    r1 = [h1[i]/h1[i-1]-1 for i in range(1, len(h1))]
    rv_now = std(r1[-48:]) if len(r1) >= 48 else 0
    rv_pct = 0.5
    if len(r1) > 60:
        below = cnt = 0
        for i in range(48, len(r1)):
            if std(r1[i-48:i]) <= rv_now: below += 1
            cnt += 1
        rv_pct = below/cnt if cnt else 0.5
    high_vol = rv_pct > 0.7
    # 冲击记录（持久化在 state["defense"]）
    defst = state.setdefault("defense", {})
    st = defst.get(sym, {})
    shock_ts, shock_dir = st.get("shockTs", 0), st.get("shockDir", 0)
    shock_amp, shock_price = st.get("shockAmp", 0), st.get("shockPrice", 0)
    ms = now_ms()
    if abs(z_now) >= 4 and vol_burst >= 3 and ms-shock_ts > 30*60000:
        shock_ts, shock_dir, shock_amp, shock_price = ms, (1 if z_now > 0 else -1), abs(z_now), c[-1]
    cooldown_until = shock_ts + 30*60000
    in_cooldown = ms < cooldown_until
    mins_since = (ms-shock_ts)/60000 if shock_ts else float("inf")
    # NR7 窄幅区间压缩
    dr = [ (d["high"][i]-d["low"][i])/d["close"][i] for i in range(len(d["close"])) ]
    today_r = dr[-1]; past7 = dr[-8:-1]
    nr7_pct = (len([r for r in past7 if r <= today_r])/len(past7)) if past7 else 0.5
    tbv = [ (x["tb"]/x["v"] if x["v"] > 0 else 0.5) for x in K[-12:] ]
    taker_imb = mean(tbv)
    oi = d.get("oi") or []
    oi_chg4h = None
    if len(oi) > 50:
        ob = oi[-49]["v"]
        if ob > 0: oi_chg4h = oi[-1]["v"]/ob-1
    frs = [x["r"] for x in (d.get("fund") or [])]
    fund_z = None
    if len(frs) > 30:
        fm, fs = mean(frs), std(frs) or 1e-9
        fund_z = (frs[-1]-fm)/fs
    rv_hist = [x for x in st.get("rvHist", []) if ms-x["t"] < 3600000]
    past_pt = next((x for x in rv_hist if ms-x["t"] >= 12*60000), None)
    state_shift = bool(past_pt and abs(rv_pct-past_pt["v"]) > DEFTH["shift"])
    rv_hist.append({"t": ms, "v": rv_pct}); rv_hist = rv_hist[-60:]
    defst[sym] = {"shockTs": shock_ts, "shockDir": shock_dir, "shockAmp": shock_amp,
                  "shockPrice": shock_price, "rvHist": rv_hist}
    # 目标波动率仓位：0.8×历史中位RV / 当前RV
    rvs = sorted(std(r1[i-48:i]) for i in range(48, len(r1), 4))
    med_rv = rvs[len(rvs)//2] if rvs else rv_now
    tv_mult = clamp(0.8*(med_rv/(rv_now or 1e-9)), 0.4, 1)
    return {"z": z_now, "volBurst": vol_burst, "volComp": vol_comp, "volDry": vol_dry,
            "highVol": high_vol, "rvPct": rv_pct, "inCooldown": in_cooldown,
            "cooldownUntil": cooldown_until, "minsSince": mins_since,
            "nr7Pct": nr7_pct, "takerImb": taker_imb, "oiChg4h": oi_chg4h,
            "fundZ": fund_z, "tvMult": tv_mult, "stateShift": state_shift}

def macro_gate():
    ms = now_ms()
    nxt, mins_to = None, float("inf")
    for name, iso in MACRO_EVENTS:
        ts = int(pd.Timestamp(iso).timestamp()*1000)
        m = (ts-ms)/60000
        if -120 < m < mins_to:
            mins_to, nxt = m, (name, ts)
    block_pre  = 0 < mins_to < 60
    post_shock = -120 < mins_to <= 0
    event_day  = bool(nxt and 0 < mins_to < 24*60)
    return {"minsTo": mins_to, "blocked": block_pre,
            "sizeMult": (0.5 if event_day else 1)*(0.5 if post_shock else 1)}

def time_gate():
    g = time.gmtime()
    day, mins, date = g.tm_wday, g.tm_hour*60+g.tm_min, g.tm_mday
    # JS getUTCDay: 0=周日；python tm_wday: 0=周一 → 转换
    js_day = (day+1) % 7
    cme_block = (js_day == 0 and mins >= 22*60) or (js_day == 1 and mins < 2*60)
    dim = (pd.Timestamp(g.tm_year, g.tm_mon, 1) + pd.offsets.MonthEnd(0)).day
    month_end = date >= dim-1
    last_day = pd.Timestamp(g.tm_year, g.tm_mon, 1) + pd.offsets.MonthEnd(0)
    last_fri = last_day
    while last_fri.weekday() != 4: last_fri -= pd.Timedelta(days=1)
    is_expiry = (date == last_fri.day) and (js_day == 5)
    return {"cmeBlock": cme_block, "monthEnd": month_end,
            "expiryAM": is_expiry and 4*60 <= mins < 12*60,
            "expiryPost": is_expiry and 12*60 <= mins < 14*60}

def def_gate(sym, DEFG, DYNC, MACROG, TIMEG, state):
    D = DEFG.get(sym); M = MACROG; T = TIMEG
    frz = state.setdefault("freeze", {})
    dyn_blocked = bool(frz.get(sym, 0) > now_ms())
    os_r = (DYNC.get(sym) or {}).get("os")
    os_mult = 0.5 if (os_r and os_r["score"] < -1.2) else 1
    return {"blocked": bool((D and D["inCooldown"]) or M["blocked"] or T["cmeBlock"] or dyn_blocked),
            "sizeMult": (0.5 if (D and D["highVol"]) else 1) * M["sizeMult"]
                        * (D["tvMult"] if D and D.get("tvMult") is not None else 1) * os_mult}

# ===================== 凯利与行情准入（移植 swingKelly / intraRegimeGate） =====================
def swing_kelly(sym, fallback, DYNC):
    R = DYNC.get(sym)
    if not R or "swSeries" not in R: return fallback
    t, Ss, c = R["swSeries"]["t"], R["swSeries"]["s"], R["swSeries"]["c"]
    n = len(t)
    if n < 40: return fallback
    rs = []; prev = 0
    for i in range(max(2, n-32), n):
        Si = Ss[i-1]
        if not np.isfinite(Si) or not c[i] or not c[i-1]:
            prev = 0; continue
        b = clamp(Si, -1, 1)
        rs.append(b*(c[i]/c[i-1]-1) - 0.0004*abs(b-prev))
        prev = b
    if len(rs) < 15: return fallback
    wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]
    p = len(wins)/len(rs)
    b_ratio = mean(wins)/abs(mean(losses)) if wins and losses else 1
    return clamp(p-(1-p)/(b_ratio or 1e-9), 0, 1)/2

def intra_regime_gate(sym, d, DYNC):
    R = DYNC.get(sym)
    if not R or "idSeries" not in R or not d or not d["h4"]["c"]:
        return {"blocked": False, "pnl": None}
    t, comp, cc = R["idSeries"]["t"], R["idSeries"]["s"], R["idSeries"]["c"]
    if len(t) < 300: return {"blocked": False, "pnl": None}
    c4, v4 = d["h4"]["c"], d["h4"]["v"]; n4 = len(c4)
    v6 = roll_mean(v4, 6)
    ret4 = [None] + [c4[i]/c4[i-1]-1 for i in range(1, n4)]
    asym = [ (ret4[i]*(v4[i]/v6[i])) if (ret4[i] is not None and v6[i]) else None for i in range(n4) ]
    mom24 = [ (c4[i]/c4[i-6]-1) if i >= 6 else None for i in range(n4) ]
    def zW_at(arr, i):
        a = [arr[j] for j in range(max(0, i-41), i+1) if arr[j] is not None and np.isfinite(arr[j])]
        if len(a) < 20: return float("nan")
        m = mean(a)
        return (arr[i]-m)/((std(a)) or 1e-12)
    sta4 = [float("nan")]*n4
    for i in range(n4):
        a1 = zW_at(asym, i) if asym[i] is not None else float("nan")
        a2 = zW_at(mom24, i) if mom24[i] is not None else float("nan")
        if np.isfinite(a1) and np.isfinite(a2): sta4[i] = clamp((-a1-a2)/2, -3, 3)
    sta_g = asof_arr(t, d["h4"]["t"], sta4)
    S = [ (clamp(0.3*sta_g[i]+comp[i], -3, 3) if (np.isfinite(comp[i]) and np.isfinite(sta_g[i])) else float("nan"))
          for i in range(len(t)) ]
    spacing = (t[1]-t[0]) or 900000
    bars7d = min(len(t)-2, round(7*86400000/spacing))
    bars24h = max(1, round(86400000/spacing))
    pos_d = entry = held = 0; pnl = 0.0; trades = 0
    for i in range(len(t)-bars7d, len(t)-1):
        Si = S[i]
        if not np.isfinite(Si): continue
        if pos_d == 0:
            if abs(Si) >= 0.9:
                pos_d = 1 if Si > 0 else -1; entry = cc[i]; held = 0
                pnl -= 0.0004; trades += 1
        else:
            held += 1
            rev = (1 if Si > 0 else -1) == -pos_d and abs(Si) >= 1.0
            if rev or held >= bars24h:
                pnl += pos_d*(cc[i]/entry-1)-0.0004; pos_d = 0
    if pos_d != 0: pnl += pos_d*(cc[-1]/entry-1)-0.0004
    return {"blocked": pnl < 0 and trades >= 5, "pnl": pnl, "trades": trades}

# ===================== 追单保护（移植 armUpdate / chaseCheck） =====================
def arm_update(state, mode, sym, zone, dir_, P, has_pos):
    arm = state.setdefault("arm", {})
    key, zkey = f"{mode}:{sym}", f"{mode}:{sym}_z"
    prev = arm.get(zkey)
    if has_pos:
        arm.pop(key, None); arm.pop(zkey, None); return
    if zone == "FLAT":
        arm.pop(key, None); arm[zkey] = "FLAT"; return
    if prev is not None and prev != zone:
        arm[key] = {"dir": dir_, "price": P, "ts": now_ms()}
    arm[zkey] = zone

def chase_check(state, mode, sym, dir_, P, A, highs, lows, max_ext=None):
    if not A: return {"pass": True}
    th = max_ext or 1.0
    rec = state.get("arm", {}).get(f"{mode}:{sym}")
    ref, proxy = None, False
    if rec and rec.get("dir") == dir_:
        ref = rec["price"]
    elif highs and lows and len(highs) >= 3 and len(lows) >= 3:
        ref = max(highs[-3:]) if dir_ < 0 else min(lows[-3:])
        proxy = True
    if ref is None or not np.isfinite(ref): return {"pass": True}
    ext = (ref-P)/A if dir_ < 0 else (P-ref)/A
    if ext <= th: return {"pass": True, "ext": ext}
    wait_lv = ref-th*A if dir_ < 0 else ref+th*A
    return {"pass": False, "ext": ext, "waitLv": wait_lv, "ref": ref, "proxy": proxy, "th": th}

# ===================== 账本与仓位工具 =====================
def norm_pos(cur):
    """移植 normPos：补齐旧仓位缺省字段"""
    if not cur: return None
    if cur.get("dir") is None or cur.get("entry") is None: return None
    cur.setdefault("sizePct", 15)
    atr = (abs(cur["tp1"]-cur["entry"]) if cur.get("tp1") is not None
           else abs(cur["entry"]-(cur.get("stop") or cur["entry"]))/1.5 or cur["entry"]*0.015)
    d = cur["dir"]
    if cur.get("tp1") is None: cur["tp1"] = cur["entry"]+d*1.0*atr
    if cur.get("tp2") is None: cur["tp2"] = cur["entry"]+d*2.0*atr
    if cur.get("tp3") is None: cur["tp3"] = cur["entry"]+d*3.0*atr
    if cur.get("add") is None: cur["add"] = cur["entry"]-d*1.0*atr
    if cur.get("stop") is None: cur["stop"] = cur["entry"]-d*1.5*atr
    if cur.get("addPct") is None: cur["addPct"] = cur["sizePct"]
    if cur.get("addDone") is None: cur["addDone"] = bool(cur.get("added"))
    if cur.get("tp1Done") is None: cur["tp1Done"] = False
    if cur.get("tp2Done") is None: cur["tp2Done"] = False
    if cur.get("entryTs") is None: cur["entryTs"] = now_ms()
    if cur.get("atrEntry") is None: cur["atrEntry"] = atr
    if cur.get("peakMfe") is None: cur["peakMfe"] = 0.0
    return cur

def close_trade(cur, last, P, reason):
    pnl = (P/cur["entry"]-1) if cur["dir"] > 0 else (1-P/cur["entry"])
    last["status"] = "已平仓"; last["exitPrice"] = P; last["exitTime"] = bj_str()
    last["pnl"] = pnl*100; last["reason"] = reason
    last["realized"] = (last.get("realized") or 0) + cur["sizePct"]*pnl
    return pnl

def new_trade_id(trades):
    return (trades[-1]["id"]+1) if trades else 1

# ===================== 结构化止盈止损辅助（2026-09-21 阶段1/2） =====================
def struct_snap(ohlc, price):
    """从引擎的周期数据dict构造结构快照；数据不足或模块缺失返回 None"""
    if stmod is None or not ohlc or len(ohlc.get("c", [])) < 60: return None
    try:
        return stmod.structure_levels(np.array(ohlc["o"], float), np.array(ohlc["h"], float),
                                      np.array(ohlc["l"], float), np.array(ohlc["c"], float),
                                      np.array(ohlc["v"], float), price=price)
    except Exception:
        return None

def structural_stop(P, d, A, snap):
    """开仓止损锚定结构位：多头取最近支撑（OB底/摆动低/FVG底），止损=支撑-0.2×ATR。
    距离钳制在 [0.8, 2.5]×ATR：太近防噪声扫损，太远回退经典 1.5×ATR。
    返回 (stop, risk_scale, tag)；risk_scale 用于仓位缩放保持单笔风险一致。"""
    if not snap: return None, 1.0, ""
    lv = None
    if d > 0 and snap.get("supports"): lv = snap["supports"][0][0] - 0.2*A
    if d < 0 and snap.get("resists"):  lv = snap["resists"][0][0] + 0.2*A
    if lv is None: return None, 1.0, ""
    tag = (snap["supports"][0][1] if d > 0 else snap["resists"][0][1])
    dist = abs(P-lv)
    if dist < STOP_MIN_ATR*A: return P-d*STOP_MIN_ATR*A, 1.0, tag+"·近"
    if dist > STOP_MAX_ATR*A: return None, 1.0, ""          # 结构太远，回退 1.5×ATR
    return lv, clamp(1.5*A/dist, 0.5, 1.2), tag

def align_tp(P, d, A, snap, mults=(1.0, 2.0, 3.0)):
    """止盈位与前方反向结构区对齐：结构位落在该档 ATR 窗口内时，止盈设在结构位前 0.1×ATR。
    保持 tp1<tp2<tp3（多头）单调。返回 [tp1,tp2,tp3] 与对齐标记。"""
    tps = [P+d*m*A for m in mults]
    if not snap: return tps, ""
    zones = [p for p, _ in (snap["resists"] if d > 0 else snap["supports"])]
    tags = []
    windows = [(0.55, 1.7), (1.6, 2.7), (2.6, 4.5)]
    for k, m in enumerate(mults):
        lo, hi = windows[k]
        cand = [z for z in zones if lo*A < (z-P)*d < hi*A]
        if cand:
            z = min(cand, key=lambda p: abs(p-P))           # 最近的结构位
            zt = z-d*0.1*A                                   # 落在结构位之前，先落袋
            if (zt-P)*d > 0.3*A:
                tps[k] = zt; tags.append(f"tp{k+1}→结构位")
    for k in (1, 2):                                          # 强制单调
        if (tps[k]-tps[k-1])*d < 0.3*A: tps[k] = tps[k-1]+d*0.3*A
    return tps, ("🏹" + ",".join(tags) if tags else "")

def ladder_floor(cur, d):
    """阶梯锁利：按浮盈峰值档位返回止损下限（只向盈利方向使用）。
    档位由 LAD_T1/T2/T3、LAD_LOCK2、CHAND_K 控制。"""
    A0 = cur.get("atrEntry") or 0
    if A0 <= 0: return None
    atrp = A0/cur["entry"]; pk = cur.get("peakMfe", 0.0)
    if pk >= LAD_T3*atrp:
        peakP = cur["entry"]*(1+d*pk)
        return peakP-d*CHAND_K*A0, "吊灯锁利"
    if pk >= LAD_T2*atrp: return cur["entry"]+d*LAD_LOCK2*A0, f"锁{LAD_LOCK2:g}×ATR利润"
    if pk >= LAD_T1*atrp: return cur["entry"]+d*0.05*A0, "锁成本"
    return None

def taper_giveback(peak, atrp):
    """递减回吐比例：浮盈峰值越大，允许回吐越小（GB_TIERS 四档）"""
    x = peak/atrp if atrp > 0 else 0
    g = GB_TIERS
    return g[0] if x < 1 else g[1] if x < 2 else g[2] if x < 3 else g[3]

def trail_struct(cur, d, P, snap):
    """结构跟随：出现比当前止损更优的同向结构位时返回新止损（只向盈利方向）"""
    if not snap: return None
    A0 = cur.get("atrEntry") or 0
    if A0 <= 0: return None
    if d > 0:
        cands = [p for p, _ in snap.get("supports", []) if cur["stop"]+0.2*A0 < p < P]
        return (max(cands)-0.2*A0) if cands else None
    cands = [p for p, _ in snap.get("resists", []) if P < p < cur["stop"]-0.2*A0]
    return (min(cands)+0.2*A0) if cands else None

# ===================== 波段引擎（移植 tradeEngine + 2026-09-06 新增保护规则） =====================
def trade_engine(sym, sig, state, gate, DYNC, events, snap=None):
    pos = state.setdefault("positions", {})
    trades = state.setdefault("trades", [])
    P, A, S = sig["price"], sig["atr"], sig["S"]
    cur = norm_pos(pos.get(sym)); pos[sym] = cur
    zone = "LONG" if sig["finalPos"] > 0.05 else "SHORT" if sig["finalPos"] < -0.05 else "FLAT"
    arm_update(state, "SW", sym, zone, 1 if zone == "LONG" else -1, P, bool(cur))

    if not cur:
        if zone != "FLAT" and not gate["blocked"]:
            d = 1 if zone == "LONG" else -1
            cg = chase_check(state, "SW", sym, d, P, A, sig["hi"], sig["lo"])
            if not cg["pass"]:
                events.append(f'🕐 {sym} 波段{"多" if d > 0 else "空"}信号已走出 {cg["ext"]:.1f}×ATR'
                              f'（参考{"近3日极值" if cg.get("proxy") else "信号触发价"} ${cg["ref"]:,.1f}）'
                              f'→ 不追单，{"回落至" if d > 0 else "反弹至"} ${cg["waitLv"]:,.1f} '
                              f'{"下方" if d > 0 else "上方"}自动开第一手')
                return None
            k_cap = clamp(swing_kelly(sym, sig["halfKelly"], DYNC), 0, 0.5)*100
            target = min(abs(sig["finalPos"])*100, k_cap)*gate["sizeMult"]
            # 波段连亏保护：连续2次止损后下一次仓位减半
            consec_sl = 0
            for t in reversed(trades):
                if t.get("mode", "波段") != "波段" or t.get("pnl") is None: continue
                if t.get("reason") == "触发止损": consec_sl += 1
                else: break
            half_guard = consec_sl >= 2
            if half_guard: target /= 2
            if target < 1: return None   # 凯利上限≈0 不开僵尸单
            # —— 结构化止损/止盈（2026-09-21）：结构位锚定 + 止盈对齐反向结构区 ——
            stop0, tps, risk_scale, s_note, tp_note = P-d*1.5*A, None, 1.0, "", ""
            if STRUCT_ON and snap:
                s_stop, risk_scale, s_tag = structural_stop(P, d, A, snap)
                if s_stop is not None:
                    if STRUCT_SHADOW:
                        s_note = f'｜🔮影子:结构止损应 ${s_stop:,.1f}({s_tag},仓位×{risk_scale:.2f})'
                        risk_scale = 1.0
                    else:
                        stop0 = s_stop; s_note = f"｜🏗结构止损({s_tag})"
            if STRUCT_TP_ALIGN and snap:
                tps, tp_note = align_tp(P, d, A, snap)
                if STRUCT_SHADOW and tp_note: tp_note = f'｜🔮影子:止盈应对齐 {[round(t,1) for t in tps]}'
            if STRUCT_SHADOW: tps = None                 # 影子模式不动实际止盈位
            if tps is None: tps = [P+d*1.0*A, P+d*2.0*A, P+d*3.0*A]
            first_pct = target/2*risk_scale
            cur = {"sym": sym, "dir": d, "entry": P, "entryTime": bj_str(), "entryTs": now_ms(),
                   "sizePct": first_pct, "targetPct": target*risk_scale, "addPct": target*risk_scale-first_pct,
                   "stop": stop0, "add": P-d*1.0*A,
                   "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
                   "addDone": False, "tp1Done": False, "tp2Done": False,
                   "halfGuard": half_guard, "atrEntry": A, "peakMfe": 0.0}
            pos[sym] = cur
            trades.append({"id": new_trade_id(trades), "sym": sym, "dir": "多" if d > 0 else "空",
                           "entryTime": bj_str(), "entry": P, "sizePct": round(first_pct, 1),
                           "stop": cur["stop"], "tp1": cur["tp1"], "tp2": cur["tp2"], "tp3": cur["tp3"],
                           "status": "持仓中", "exitPrice": None, "exitTime": None,
                           "pnl": None, "reason": "", "mode": "波段", "realized": 0})
            events.append(f'🆕 开{"多" if d > 0 else "空"} {sym} 第一手 {first_pct:.1f}% @ ${P:,.1f}'
                          f'｜补仓位 ${cur["add"]:,.1f} 再补 {cur["addPct"]:.1f}%'
                          f'｜止损 ${cur["stop"]:,.1f}｜止盈 {cur["tp1"]:,.0f}/{cur["tp2"]:,.0f}/{cur["tp3"]:,.0f}'
                          + s_note + tp_note
                          + (f'｜🛡 连亏{consec_sl}次保护：本次仓位减半' if half_guard else '')
                          + ('｜🛡 高波动态：仓位减半' if gate["sizeMult"] < 1 else ''))
        return cur

    # —— 持仓管理 ——
    d = cur["dir"]
    last = next((t for t in reversed(trades)   # 排除 local 合并单：只认引擎自己开的仓
                 if t["sym"] == sym and t.get("mode", "波段") == "波段" and t["exitPrice"] is None
                 and not t.get("local")), None)
    if last is None:
        pos[sym] = None; return None
    if cur["sizePct"] <= 0.05:   # 僵尸单自愈
        close_trade(cur, last, P, "仓位异常自愈平仓")
        pos[sym] = None
        events.append(f"🩹 {sym} 检测到仓位≈0 的僵尸持仓，已市价平仓 @ ${P:,.1f}，波段开仓已解除阻塞")
        return None
    hit   = lambda lv: P <= lv if d > 0 else P >= lv
    hit_up = lambda lv: P >= lv if d > 0 else P <= lv
    mfe = (P/cur["entry"]-1) if d > 0 else (1-P/cur["entry"])
    cur["peakMfe"] = max(cur.get("peakMfe", 0.0), mfe)

    if hit(cur["stop"]):
        pnl = close_trade(cur, last, P, "触发止损"); pos[sym] = None
        events.append(f"🛑 {sym} 止损 @ ${P:,.1f}（{pnl*100:+.2f}%）")
    elif hit_up(cur["tp3"]):
        pnl = close_trade(cur, last, P, "止盈3清仓"); pos[sym] = None
        events.append(f"💰 {sym} 止盈3清仓 @ ${P:,.1f}（{pnl*100:+.2f}%）")
    else:
        # ★ 阶梯锁利 + 结构跟随（2026-09-21 阶段1）：止损只向盈利方向移动
        if LADDER_LOCK_ON or STRUCT_TRAIL_ON:
            new_stop, how = None, ""
            if LADDER_LOCK_ON:
                lf = ladder_floor(cur, d)
                if lf: new_stop, how = lf
            if STRUCT_TRAIL_ON and snap:
                ts = trail_struct(cur, d, P, snap)
                if ts is not None and (new_stop is None or (ts-new_stop)*d > 0):
                    new_stop, how = ts, "结构跟随"
            if new_stop is not None and (new_stop-cur["stop"])*d > 0:
                if STRUCT_SHADOW:
                    mark = f"{how}@{round(new_stop,1)}"
                    if cur.get("_shadowStop") != mark:
                        cur["_shadowStop"] = mark
                        events.append(f"🔮影子 {sym} 阶梯/结构锁利：止损 {cur['stop']:,.1f} 本应上移至 "
                                      f"${new_stop:,.1f}（{how}，浮盈峰值 {cur['peakMfe']*100:+.2f}%）")
                else:
                    old = cur["stop"]; cur["stop"] = new_stop
                    events.append(f"🪜 {sym} {how}：止损 ${old:,.1f} → ${new_stop:,.1f}"
                                  f"（浮盈峰值 {cur['peakMfe']*100:+.2f}%）")
        # ★ 浮盈回撤保护（2026-09-21 改为递减回吐：峰值越大允许回吐越小；不再限 tp1 前）
        atr_pct = cur["atrEntry"]/cur["entry"] if cur.get("atrEntry") else 0
        gb = taper_giveback(cur["peakMfe"], atr_pct) if TAPER_GIVEBACK else SW_TRAIL_GIVEBACK
        arm = GB_ARM_ATR if TAPER_GIVEBACK else SW_TRAIL_ARM_ATR
        if (atr_pct > 0 and cur["peakMfe"] >= arm*atr_pct
                and mfe <= cur["peakMfe"]*gb
                and (TAPER_GIVEBACK or not cur["tp1Done"])):
            if STRUCT_SHADOW:
                if not cur.get("_shadowGb"):
                    cur["_shadowGb"] = True
                    events.append(f"🔮影子 {sym} 回撤保护：浮盈峰值 {cur['peakMfe']*100:+.2f}% 回吐至 "
                                  f"{mfe*100:+.2f}%（阈值{gb*100:.0f}%），本应平仓落袋 @ ${P:,.1f}")
            else:
                pnl = close_trade(cur, last, P,
                                  f"浮盈回撤保护（峰值{cur['peakMfe']*100:+.2f}%→{mfe*100:+.2f}%，阈值{gb*100:.0f}%）")
                pos[sym] = None
                events.append(f"🛡 {sym} 浮盈回撤保护平仓 @ ${P:,.1f}：浮盈峰值 {cur['peakMfe']*100:+.2f}% "
                              f"回吐至 {mfe*100:+.2f}%（{pnl*100:+.2f}%）——不再让利润坐过山车")
                return None
        # ★ 新增②：波段时间止损——≥5天未达止盈1，止损上移成本（日内早有，波段补上）
        hold_days = (now_ms()-cur["entryTs"])/86400000
        if (hold_days >= SW_TIME_STOP_DAYS and not cur["tp1Done"]
                and ((d > 0 and cur["stop"] < cur["entry"]) or (d < 0 and cur["stop"] > cur["entry"]))):
            cur["stop"] = cur["entry"]
            events.append(f"⏰ {sym} 波段持仓 {hold_days:.1f} 天未达止盈1，止损上移至成本 ${cur['entry']:,.1f}")
        # ★ 新增③：波段最长持有——≥15天市价强平，杜绝无限期僵尸持仓
        if hold_days >= SW_MAX_HOLD_DAYS:
            pnl = close_trade(cur, last, P, f"波段超时强平（≥{SW_MAX_HOLD_DAYS}天）")
            pos[sym] = None
            events.append(f"⏰ {sym} 波段持仓 {hold_days:.1f} 天到达上限，市价平仓 @ ${P:,.1f}（{pnl*100:+.2f}%）")
            return None
        if not cur["tp1Done"] and hit_up(cur["tp1"]):
            last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/3)*mfe
            cur["tp1Done"] = True; cur["sizePct"] = cur["sizePct"]*2/3
            if (cur["entry"]-cur["stop"])*d > 0: cur["stop"] = cur["entry"]   # 单调上移：不覆盖阶梯/结构的更优止损
            last["status"] = "止盈1减1/3"; last["tp1Hit"] = bj_str()
            events.append(f"💰 {sym} 止盈1平1/3 @ ${P:,.1f}，止损已移至成本")
        if cur["tp1Done"] and not cur["tp2Done"] and hit_up(cur["tp2"]):
            last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/2)*mfe
            cur["tp2Done"] = True; cur["sizePct"] = cur["sizePct"]/2
            if (cur["tp1"]-cur["stop"])*d > 0: cur["stop"] = cur["tp1"]       # 单调上移
            last["status"] = "止盈2再减半"; last["tp2Hit"] = bj_str()
            events.append(f"💰 {sym} 止盈2再减半 @ ${P:,.1f}，止损上移至止盈1 ${cur['tp1']:,.1f}")
        if not cur["addDone"] and hit(cur["add"]):
            cur["addDone"] = True
            add_pct = cur["addPct"]; new_size = cur["sizePct"]+add_pct
            cur["entry"] = (cur["entry"]*cur["sizePct"] + P*add_pct)/new_size
            cur["sizePct"] = new_size
            last["sizePct"] = round(new_size, 1); last["entry"] = cur["entry"]
            last["added"] = bj_str() + f" @ ${P:,.1f}"
            events.append(f"➕ {sym} 补仓 {add_pct:.1f}% @ ${P:,.1f}，均价 ${cur['entry']:,.1f}")
        reverse = (d > 0 and S <= -1) or (d < 0 and S >= 1)
        if pos.get(sym) and reverse:
            pnl = close_trade(cur, last, P, "信号强反转"); pos[sym] = None
            events.append(f"🔄 {sym} 信号反转平仓 @ ${P:,.1f}（{pnl*100:+.2f}%）")
    return pos.get(sym)

# ===================== 日内引擎（移植 tradeEngineIntra） =====================
def trade_engine_intra(sym, isig, state, gate, intra_gate, events, snap=None):
    pos = state.setdefault("positions_intra", {})
    trades = state.setdefault("trades", [])
    P, A, S = isig["price"], isig["atr4"], isig["sig"]
    hour_utc = time.gmtime().tm_hour
    cur = norm_pos(pos.get(sym)); pos[sym] = cur
    zone = "LONG" if S >= 0.9 else "SHORT" if S <= -0.9 else "FLAT"
    arm_update(state, "ID", sym, zone, 1 if zone == "LONG" else -1, P, bool(cur))
    FIRST, ADD = 6, 4

    if not cur:
        pause_until = state.get("intra_pause_until", 0)
        flip_cd = state.setdefault("intra_flip_cd", {}).get(sym, 0)
        if now_ms() < pause_until or now_ms() < flip_cd or gate["blocked"] or intra_gate["blocked"]:
            return None
        # 2026-09-16 修复：开仓窗口排除 UTC 0 点小时（该小时同时是"日界强平"窗口，
        # 云端24h运行后暴露——08:45北京开的单08:56就被日界强平，来回空转+邮件轰炸）
        if zone != "FLAT" and 0 < hour_utc < 20:
            d = 1 if zone == "LONG" else -1
            cg = chase_check(state, "ID", sym, d, P, A, isig["hi"], isig["lo"])
            if not cg["pass"]:
                events.append(f'🕐 {sym} 日内{"多" if d > 0 else "空"}信号已走出 {cg["ext"]:.1f}×ATR4h'
                              f' → 不追单，{"回落至" if d > 0 else "反弹至"} ${cg["waitLv"]:,.1f} '
                              f'{"下方" if d > 0 else "上方"}自动开第一手')
                return None
            f_size, a_size = FIRST*gate["sizeMult"], ADD*gate["sizeMult"]
            # —— 结构化止损/止盈（2026-09-21，与波段同规则） ——
            stop0, tps, risk_scale, s_note, tp_note = P-d*1.5*A, None, 1.0, "", ""
            if STRUCT_ON and snap:
                s_stop, risk_scale, s_tag = structural_stop(P, d, A, snap)
                if s_stop is not None:
                    if STRUCT_SHADOW:
                        s_note = f'｜🔮影子:结构止损应 ${s_stop:,.1f}({s_tag},仓位×{risk_scale:.2f})'
                        risk_scale = 1.0
                    else:
                        stop0 = s_stop; s_note = f"｜🏗结构止损({s_tag})"
            if STRUCT_TP_ALIGN and snap:
                tps, tp_note = align_tp(P, d, A, snap)
                if STRUCT_SHADOW and tp_note: tp_note = f'｜🔮影子:止盈应对齐 {[round(t,1) for t in tps]}'
            if STRUCT_SHADOW: tps = None
            if tps is None: tps = [P+d*1.0*A, P+d*2.0*A, P+d*3.0*A]
            f_size *= risk_scale; a_size *= risk_scale
            cur = {"sym": sym, "dir": d, "entry": P, "entryTime": bj_str(), "entryTs": now_ms(),
                   "sizePct": f_size, "addPct": a_size,
                   "stop": stop0, "add": P-d*1.0*A,
                   "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
                   "addDone": False, "tp1Done": False, "tp2Done": False, "intra": True,
                   "atrEntry": A, "peakMfe": 0.0}
            pos[sym] = cur
            trades.append({"id": new_trade_id(trades), "sym": sym, "dir": "多" if d > 0 else "空",
                           "entryTime": bj_str(), "entry": P, "sizePct": round(f_size, 1),
                           "stop": cur["stop"], "tp1": cur["tp1"], "tp2": cur["tp2"], "tp3": cur["tp3"],
                           "status": "持仓中", "exitPrice": None, "exitTime": None,
                           "pnl": None, "reason": "", "mode": "日内", "realized": 0})
            events.append(f'⚡日内开{"多" if d > 0 else "空"} {sym} 第一手{f_size:.1f}% @ ${P:,.1f}'
                          f'｜补仓位 ${cur["add"]:,.1f} 再补{a_size:.1f}%'
                          f'｜止损 ${cur["stop"]:,.1f}｜止盈 {cur["tp1"]:,.0f}/{cur["tp2"]:,.0f}/{cur["tp3"]:,.0f}'
                          + s_note + tp_note
                          + ('｜🛡高波动态减半' if gate["sizeMult"] < 1 else ''))
        return cur

    d = cur["dir"]
    last = next((t for t in reversed(trades)   # 排除 local 合并单：只认引擎自己开的仓
                 if t["sym"] == sym and t.get("mode") == "日内" and t["exitPrice"] is None
                 and not t.get("local")), None)
    if last is None:
        pos[sym] = None; return None
    hold_h = (now_ms()-cur["entryTs"])/3600000
    hit   = lambda lv: P <= lv if d > 0 else P >= lv
    hit_up = lambda lv: P >= lv if d > 0 else P <= lv
    reason = None
    if hit(cur["stop"]): reason = "触发止损"
    elif hit_up(cur["tp3"]): reason = "止盈3清仓"
    elif hold_h >= 24: reason = "持仓满24h强平"
    elif hour_utc == 0: reason = "UTC日界强平（不隔夜）"
    else:
        opp_key = state.setdefault("intra_opp", {})
        opp = (1 if S > 0 else -1 if S < 0 else 0) == -d and abs(S) >= 1.0
        opp_n = (opp_key.get(sym, 0)+1) if opp else 0
        opp_key[sym] = opp_n
        if opp and hold_h >= 4 and opp_n >= 3: reason = "日内信号反转（持续强反转确认）"
    if reason:
        pnl = close_trade(cur, last, P, reason); pos[sym] = None
        events.append(f"🏁 {sym} 日内平仓 @ ${P:,.1f}（{reason}，{pnl*100:+.2f}%）")
        if "反转" in reason:
            state.setdefault("intra_flip_cd", {})[sym] = now_ms()+45*60000
            state.setdefault("intra_opp", {})[sym] = 0
            events.append(f"⏸ {sym} 日内反手冷却 45 分钟，等趋势明朗再进")
        if reason == "触发止损":
            consec = 0
            for t in reversed(trades):
                if t.get("mode") != "日内" or t.get("pnl") is None: continue
                if t.get("reason") == "触发止损": consec += 1
                else: break
            if consec >= 3:
                state["intra_pause_until"] = now_ms()+2*3600000
                events.append(f"⏸ 日内连续{consec}次止损，暂停开新仓2小时，冷静一下")
    else:
        mfe = (P/cur["entry"]-1) if d > 0 else (1-P/cur["entry"])
        cur["peakMfe"] = max(cur.get("peakMfe", 0.0), mfe)
        # ★ 阶梯锁利 + 结构跟随（2026-09-21 阶段1，日内同步接入）
        if LADDER_LOCK_ON or STRUCT_TRAIL_ON:
            new_stop, how = None, ""
            if LADDER_LOCK_ON:
                lf = ladder_floor(cur, d)
                if lf: new_stop, how = lf
            if STRUCT_TRAIL_ON and snap:
                ts = trail_struct(cur, d, P, snap)
                if ts is not None and (new_stop is None or (ts-new_stop)*d > 0):
                    new_stop, how = ts, "结构跟随"
            if new_stop is not None and (new_stop-cur["stop"])*d > 0:
                if STRUCT_SHADOW:
                    mark = f"{how}@{round(new_stop,1)}"
                    if cur.get("_shadowStop") != mark:
                        cur["_shadowStop"] = mark
                        events.append(f"🔮影子 {sym} 日内阶梯/结构锁利：止损 {cur['stop']:,.1f} 本应上移至 "
                                      f"${new_stop:,.1f}（{how}，浮盈峰值 {cur['peakMfe']*100:+.2f}%）")
                else:
                    old = cur["stop"]; cur["stop"] = new_stop
                    events.append(f"🪜 {sym} 日内{how}：止损 ${old:,.1f} → ${new_stop:,.1f}"
                                  f"（浮盈峰值 {cur['peakMfe']*100:+.2f}%）")
        # ★ 日内浮盈回撤保护（2026-09-21 新增：日内此前没有回撤保护）
        atr_pct = cur["atrEntry"]/cur["entry"] if cur.get("atrEntry") else 0
        gb = taper_giveback(cur["peakMfe"], atr_pct) if TAPER_GIVEBACK else SW_TRAIL_GIVEBACK
        arm = GB_ARM_ATR if TAPER_GIVEBACK else SW_TRAIL_ARM_ATR
        if (atr_pct > 0 and cur["peakMfe"] >= arm*atr_pct and mfe <= cur["peakMfe"]*gb):
            if STRUCT_SHADOW:
                if not cur.get("_shadowGb"):
                    cur["_shadowGb"] = True
                    events.append(f"🔮影子 {sym} 日内回撤保护：峰值 {cur['peakMfe']*100:+.2f}% 回吐至 "
                                  f"{mfe*100:+.2f}%（阈值{gb*100:.0f}%），本应平仓落袋 @ ${P:,.1f}")
            else:
                pnl = close_trade(cur, last, P,
                                  f"日内浮盈回撤保护（峰值{cur['peakMfe']*100:+.2f}%→{mfe*100:+.2f}%）")
                pos[sym] = None
                events.append(f"🛡 {sym} 日内浮盈回撤保护平仓 @ ${P:,.1f}（{pnl*100:+.2f}%）")
                return None
        if (hold_h >= 12 and not cur["tp1Done"]
                and ((d > 0 and cur["stop"] < cur["entry"]) or (d < 0 and cur["stop"] > cur["entry"]))):
            cur["stop"] = cur["entry"]
            events.append(f"⏰ {sym} 日内持仓 {hold_h:.1f}h 未达止盈1，止损上移至成本 ${cur['entry']:,.1f}")
        if not cur["tp1Done"] and hit_up(cur["tp1"]):
            last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/3)*mfe
            cur["tp1Done"] = True; cur["sizePct"] = cur["sizePct"]*2/3
            if (cur["entry"]-cur["stop"])*d > 0: cur["stop"] = cur["entry"]   # 单调上移
            last["status"] = "止盈1减1/3"
            events.append(f"💰 {sym} 日内止盈1平1/3 @ ${P:,.1f}，止损移至成本")
        if cur["tp1Done"] and not cur["tp2Done"] and hit_up(cur["tp2"]):
            last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/2)*mfe
            cur["tp2Done"] = True; cur["sizePct"] = cur["sizePct"]/2
            if (cur["tp1"]-cur["stop"])*d > 0: cur["stop"] = cur["tp1"]       # 单调上移
            last["status"] = "止盈2再减半"
            events.append(f"💰 {sym} 日内止盈2再减半 @ ${P:,.1f}，止损上移至止盈1")
        if not cur["addDone"] and hit(cur["add"]):
            cur["addDone"] = True
            new_size = cur["sizePct"]+cur["addPct"]
            cur["entry"] = (cur["entry"]*cur["sizePct"] + P*cur["addPct"])/new_size
            cur["sizePct"] = new_size
            last["sizePct"] = round(new_size, 1); last["entry"] = cur["entry"]
            last["added"] = bj_str() + f" @ ${P:,.1f}"
            events.append(f"➕ {sym} 日内补仓 {cur['addPct']:.1f}% @ ${P:,.1f}，均价 ${cur['entry']:,.1f}")
    return pos.get(sym)

# ===================== 超短线引擎（2026-09-18 移植自网页 computeScalp/tradeEngineScalp，云端5分钟巡航） =====================
def compute_scalp(d, dyn_sc):
    """移植 computeScalp：动态因子池为主导，静态6均值回复因子兜底；含4h趋势过滤"""
    K = d["k5"]; n = len(K)
    c = np.array([x["c"] for x in K], float)
    h = np.array([x["h"] for x in K], float)
    l = np.array([x["l"] for x in K], float)
    v = np.array([x["v"] for x in K], float)

    def zs(a):
        w = [x for x in a[-288:] if x is not None and np.isfinite(x)]
        if len(w) < 30: return 0.0
        m, s = mean(w), std(w) or 1e-12
        return clamp((a[-1]-m)/s, -4, 4)

    # 日内 VWAP 偏离（UTC日重置；day_open 记录当天第一根收盘价，与JS一致供日内动量使用）
    cum_pv = cum_v = 0.0; cur_day = ""; day_open = c[0]
    vwap_arr = []
    for i in range(n):
        day = time.strftime("%Y-%m-%d", time.gmtime(K[i]["t"]/1000))
        if day != cur_day:
            cur_day = day; cum_pv = cum_v = 0.0; day_open = c[i]
        tp = (h[i]+l[i]+c[i])/3; cum_pv += tp*v[i]; cum_v += v[i]
        vwap_arr.append(cum_pv/cum_v if cum_v > 0 else None)
    z_vwap = zs([(c[i]-vwap_arr[i])/vwap_arr[i] if vwap_arr[i] else None for i in range(n)])
    # ATR 倍数偏离
    ema20 = []; e = c[0]; k20 = 2/21
    for p in c:
        e = p*k20+e*(1-k20); ema20.append(e)
    tr5 = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, n)]
    atr5 = mean(tr5[-14:])
    z_atr = zs([(c[i]-ema20[i])/(mean(tr5[max(0, i-13):i+1]) or 1e-9) for i in range(n)])
    # RSI-14
    rsi = [None]*n
    for i in range(15, n):
        up = dn = 0.0
        for j in range(i-13, i+1):
            ch = c[j]-c[j-1]
            if ch > 0: up += ch
            else: dn -= ch
        rsi[i] = 100 if dn == 0 else 100-100/(1+up/dn)
    z_rsi = zs(rsi)
    # 布林 %B
    pctb = [None]*n
    for i in range(19, n):
        w = c[i-19:i+1]; m = mean(w); s = std(w)
        pctb[i] = (c[i]-(m-2*s))/(4*s) if s > 0 else 0.5
    z_pctb = zs(pctb)
    # 均线 Z-Score
    pz = [None]*n
    for i in range(49, n):
        w = c[i-49:i+1]
        pz[i] = (c[i]-mean(w))/(std(w) or 1e-9)
    z_pz = zs(pz)
    # 日内累计动量
    z_mom = zs([p/day_open-1 if day_open else None for p in c])
    F = [(z_vwap, 0.070), (z_atr, 0.063), (z_rsi, 0.063), (z_pctb, 0.060), (z_pz, 0.063), (z_mom, 0.060)]
    wsum = sum(w for _, w in F)
    ssig = clamp(sum(-w*z for z, w in F)/wsum, -3, 3)   # 6因子全部反向(dir=-1)
    if dyn_sc is not None and np.isfinite(dyn_sc):
        ssig = clamp(dyn_sc, -3, 3)
    # 4h 趋势过滤：|tStr|>1.5 单边市禁止逆势单
    c4 = np.array(d["h4"]["c"], float); h4h, h4l = d["h4"]["h"], d["h4"]["l"]
    e4 = c4[0]; k4 = 2/21; ema4 = []
    for p in c4:
        e4 = p*k4+e4*(1-k4); ema4.append(e4)
    tr4 = [h4h[0]-h4l[0]] + [max(h4h[i]-h4l[i], abs(h4h[i]-c4[i-1]), abs(h4l[i]-c4[i-1])) for i in range(1, len(c4))]
    atr4v = mean(tr4[-14:])
    t_str = (c4[-1]-ema4[-1])/(atr4v or 1e-9)
    return {"ssig": ssig, "atr5": atr5, "price": float(c[-1]), "tStr": t_str,
            "trending": abs(t_str) > 1.5, "trendDir": (1 if t_str > 0 else -1),
            "hi": h.tolist(), "lo": l.tolist()}

def scalp_regime_gate(sym, dync):
    """移植 scalpRegimeGate：sc动态池近7日模拟（|S|≥1.2进、反向1.2或60分钟出、万4费），亏损且≥5笔→暂停"""
    R = (dync or {}).get(sym) or {}
    S = R.get("scSeries")
    if not S: return {"blocked": False, "pnl": None}
    t, comp, cc = S["t"], S["s"], S["c"]
    if len(t) < 600: return {"blocked": False, "pnl": None}
    spacing = (t[1]-t[0]) or 300000
    bars7d = min(len(t)-2, round(7*86400000/spacing))
    bars60m = max(1, round(3600000/spacing))
    pos_d = 0; entry = 0.0; held = 0; pnl = 0.0; trades = 0
    for i in range(len(t)-bars7d, len(t)-1):
        Si = comp[i]
        if not np.isfinite(Si): continue
        if pos_d == 0:
            if abs(Si) >= 1.2:
                pos_d = 1 if Si > 0 else -1; entry = cc[i]; held = 0
                pnl -= 0.0004; trades += 1
        else:
            held += 1
            rev = (1 if Si > 0 else -1) == -pos_d and abs(Si) >= 1.2
            if rev or held >= bars60m:
                pnl += pos_d*(cc[i]/entry-1)-0.0004; pos_d = 0
    if pos_d != 0: pnl += pos_d*(cc[-1]/entry-1)-0.0004
    return {"blocked": pnl < 0 and trades >= 5, "pnl": pnl, "trades": trades}

def trade_engine_scalp(sym, ss, state, gate, sc_gate, defg, events):
    """移植 tradeEngineScalp：第一手10%+跌1×ATR5补7%，止损-1.5×ATR5，三档止盈+1/+2/+3×ATR5，限时60分钟"""
    pos = state.setdefault("positions_scalp", {})
    trades = state.setdefault("trades", [])
    P, A, S = ss["price"], ss["atr5"], ss["ssig"]
    cur = norm_pos(pos.get(sym))
    pos[sym] = cur
    th = 1.5 if (defg or {}).get("highVol") else 1.2
    zone = "LONG" if S >= th else ("SHORT" if S <= -th else "FLAT")
    arm_update(state, "SC", sym, zone, 1 if zone == "LONG" else -1, P, cur is not None)
    FIRST, ADD = 10, 7
    if cur is None:
        dir0 = 1 if zone == "LONG" else -1
        pause_until = state.setdefault("scalp_pause", {}).get(sym, 0)
        if zone != "FLAT" and ss["trending"] and dir0 != ss["trendDir"]:
            events.append(f'🚫 {sym} 超短线{"多" if dir0 > 0 else "空"}信号被趋势过滤器拦截：'
                          f'4h 单边{"多" if ss["trendDir"] > 0 else "空"}市（{ss["tStr"]:+.1f}×ATR），逆势均值回复单禁止开仓')
        elif now_ms() < pause_until:
            pass   # 同币种连续2次止损后的60分钟冷静期
        elif zone != "FLAT" and not gate["blocked"] and not sc_gate["blocked"]:
            cg = chase_check(state, "SC", sym, dir0, P, A, ss["hi"], ss["lo"], 0.6)
            if not cg["pass"]:
                events.append(f'🕐 {sym} 超短线{"多" if dir0 > 0 else "空"}信号已走出 {cg["ext"]:.1f}×ATR5'
                              f'（参考{"近3根5m极值" if cg["proxy"] else "信号触发价"} ${cg["ref"]:,.1f}）→ 不追单，'
                              f'{"回落至" if dir0 > 0 else "反弹至"} ${cg["waitLv"]:,.1f} {"下方" if dir0 > 0 else "上方"}自动开第一手')
            else:
                f_size, a_size = FIRST*gate["sizeMult"], ADD*gate["sizeMult"]
                cur = {"sym": sym, "dir": dir0, "entry": P, "entryTime": bj_str(), "entryTs": now_ms(),
                       "sizePct": f_size, "addPct": a_size, "stop": P-dir0*1.5*A, "add": P-dir0*1.0*A,
                       "tp1": P+dir0*1.0*A, "tp2": P+dir0*2.0*A, "tp3": P+dir0*3.0*A,
                       "addDone": False, "tp1Done": False, "tp2Done": False}
                pos[sym] = cur
                trades.append({"id": new_trade_id(trades), "sym": sym, "dir": "多" if dir0 > 0 else "空",
                               "entryTime": bj_str(), "entry": P, "sizePct": round(f_size, 1),
                               "stop": cur["stop"], "tp1": cur["tp1"], "tp2": cur["tp2"], "tp3": cur["tp3"],
                               "status": "持仓中", "exitPrice": None, "exitTime": None,
                               "pnl": None, "reason": "", "mode": "超短线", "realized": 0})
                events.append(f'🔥超短线开{"多" if dir0 > 0 else "空"} {sym} 第一手{f_size:.1f}% @ ${P:,.1f}'
                              f'｜补仓位 ${cur["add"]:,.1f} 再补{a_size:.1f}%｜限时60分钟'
                              + ('｜🛡高波动态减半' if gate["sizeMult"] < 1 else ''))
        return pos.get(sym)

    d = cur["dir"]
    last = next((t for t in reversed(trades)   # 排除 local 合并单：只认引擎自己开的仓
                 if t["sym"] == sym and t.get("mode") == "超短线" and t["exitPrice"] is None
                 and not t.get("local")), None)
    if last is None:
        pos[sym] = None; return None
    hold_m = (now_ms()-cur["entryTs"])/60000
    hit_dn = lambda lv: P <= lv if d > 0 else P >= lv
    hit_up = lambda lv: P >= lv if d > 0 else P <= lv
    reason = None
    if hit_dn(cur["stop"]): reason = "触发止损"
    elif hit_up(cur["tp3"]): reason = "止盈3清仓"
    elif hold_m >= 60: reason = "超短线限时60分钟到点平仓"
    elif (1 if S > 0 else -1) == -d and abs(S) >= 0.6: reason = "超短线信号反转"
    if reason:
        pnl = close_trade(cur, last, P, reason)
        pos[sym] = None
        events.append(f"🏁 {sym} 超短线平仓 @ ${P:,.1f}（{reason}，{pnl*100:+.2f}%）")
        if reason == "触发止损":   # 同币种连续2次止损 → 暂停60分钟
            consec = 0
            for t in reversed(trades):
                if t.get("mode") != "超短线" or t["sym"] != sym or t.get("pnl") is None: continue
                if t.get("reason") == "触发止损": consec += 1
                else: break
            if consec >= 2:
                state.setdefault("scalp_pause", {})[sym] = now_ms()+60*60000
                events.append(f"⏸ {sym} 超短线连续{consec}次止损，暂停开新仓60分钟，震荡市不反复挨刀")
        return None
    if not cur["tp1Done"] and hit_up(cur["tp1"]):   # 止盈1落袋1/2，止损移到成本+0.1%费用缓冲
        last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/2)*((P/cur["entry"]-1) if d > 0 else (1-P/cur["entry"]))
        cur["tp1Done"] = True; cur["sizePct"] = cur["sizePct"]/2; cur["stop"] = cur["entry"]*(1+d*0.001)
        last["status"] = "止盈1减1/2"
        events.append(f'💰 {sym} 超短线止盈1平1/2 @ ${P:,.1f}，止损移至成本+费用 ${cur["stop"]:,.1f}（已锁定微盈）')
    if cur["tp1Done"] and not cur["tp2Done"] and hit_up(cur["tp2"]):
        last["realized"] = (last.get("realized") or 0) + (cur["sizePct"]/2)*((P/cur["entry"]-1) if d > 0 else (1-P/cur["entry"]))
        cur["tp2Done"] = True; cur["sizePct"] = cur["sizePct"]/2; cur["stop"] = cur["tp1"]
        last["status"] = "止盈2再减半"
        events.append(f"💰 {sym} 超短线止盈2再减半 @ ${P:,.1f}，止损上移至止盈1")
    if not cur["addDone"] and hit_dn(cur["add"]):
        cur["addDone"] = True
        new_size = cur["sizePct"]+cur["addPct"]
        cur["entry"] = (cur["entry"]*cur["sizePct"] + P*cur["addPct"])/new_size
        cur["sizePct"] = new_size
        last["sizePct"] = round(new_size, 1); last["entry"] = cur["entry"]
        last["added"] = bj_str() + f" @ ${P:,.1f}"
        events.append(f"➕ {sym} 超短线补仓 {cur['addPct']:.1f}% @ ${P:,.1f}，均价 ${cur['entry']:,.1f}")
    return pos.get(sym)

# ===================== 状态读写与主流程 =====================
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "positions_intra": {}, "positions_scalp": {}, "trades": [], "events": [],
                "arm": {}, "defense": {}, "sema": {}, "freeze": {}, "scalp_pause": {},
                "intra_flip_cd": {}, "intra_opp": {}, "intra_pause_until": 0}

def save_state(path, state):
    state["trades"] = state.get("trades", [])[-500:]
    state["events"] = state.get("events", [])[-100:]
    state["updated_at"] = bj_str()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def run(state_path=STATE_PATH, select_path=SELECT_PATH):
    print(f"=== live_engine {bj_str()} ===", flush=True)
    state = load_state(state_path)
    events = []
    # 1) 因子池
    try:
        with open(select_path, encoding="utf-8") as f:
            dyn500 = json.load(f)
        print(f"因子池: {dyn500.get('generated_at','?')} 波段{len(dyn500['selected'].get('波段',[]))} "
              f"日内{len(dyn500['selected'].get('日内',[]))}", flush=True)
    except Exception as e:
        print(f"⚠️ 因子池读取失败({e})，本轮只用静态锚", flush=True)
        dyn500 = {"selected": {}}
    # 2) 行情
    bn, errs = {}, []
    for k in SYMS:
        try:
            bn[k] = fetch_one(k)
            print(f"{k}: ${bn[k]['now']:,.1f}（{'OKX兜底' if bn[k].get('srcOKX') else '币安'}）", flush=True)
        except Exception as e:
            errs.append(f"{k}: {e}")
    if not bn:
        print("❌ 双币数据全部拉取失败: " + "; ".join(errs), flush=True)
        state.setdefault("events", []).append(f"[{bj_str()}] ❌ 数据拉取失败，本轮跳过")
        save_state(state_path, state)
        return 1
    # 3) 动态因子层
    try:
        DYNC = dyn_compute_all(bn, dyn500)
        for k in bn:
            if k not in DYNC: continue
            bn[k]["dynSW"] = DYNC[k]["sw"]["score"] if DYNC[k].get("sw") else None
            bn[k]["dynID"] = DYNC[k]["id"]["score"] if DYNC[k].get("id") else None
            ns = DYNC[k].get("ns")
            if ns and ns["triggered"]:
                # 2026-09-19 加强：消息市有尾巴，60分钟常常不够（9-17 案例：防御触发后1小时内行情仍单边延续），
                # 冻结期下限提至 120 分钟；因子自定义更长冻结期时仍从其长
                fz = max(120, max(x["freeze"] for x in ns["triggered"]))
                state.setdefault("freeze", {})[k] = now_ms()+fz*60000
                events.append(f"⚠️ {k} 消息面动态防御触发（{len(ns['triggered'])}条警报）→ {fz}分钟内禁止开新仓")
    except Exception as e:
        print(f"⚠️ 动态因子层异常({e})，本轮只用静态锚", flush=True)
        DYNC = {}
    # 4) 防御层与闸门
    DEFG = {}
    for k in bn:
        try: DEFG[k] = compute_defense(k, bn[k], state)
        except Exception as e: print(f"⚠️ {k} 防御层异常: {e}", flush=True)
    MACROG = macro_gate(); TIMEG = time_gate()
    # 5) 信号 + 引擎
    trade_events_before = len(events)
    for k in bn:
        gate = def_gate(k, DEFG, DYNC, MACROG, TIMEG, state)
        try:
            sig = compute_signal(bn[k])
            snap4 = struct_snap(bn[k].get("h4"), sig["price"])
            trade_engine(k, sig, state, gate, DYNC, events, snap=snap4)
            print(f"{k} 波段: S={sig['S']:+.2f} dyn={sig['dynSW'] if sig['dynSW'] is not None else '—'} "
                  f"finalPos={sig['finalPos']:+.2f} 持仓={'有' if state['positions'].get(k) else '无'}", flush=True)
        except Exception as e:
            print(f"⚠️ {k} 波段引擎异常: {e}", flush=True)
            import traceback; traceback.print_exc()
        try:
            igate = intra_regime_gate(k, bn[k], DYNC) if k in DYNC else {"blocked": False, "pnl": None}
            isig = compute_intraday(bn[k], k, state)
            snap1 = struct_snap(bn[k].get("k1h"), isig["price"])
            trade_engine_intra(k, isig, state, gate, igate, events, snap=snap1)
            print(f"{k} 日内: sig={isig['sig']:+.2f} 持仓={'有' if state['positions_intra'].get(k) else '无'}"
                  f" 闸门={'关' if igate['blocked'] else '开'}", flush=True)
        except Exception as e:
            print(f"⚠️ {k} 日内引擎异常: {e}", flush=True)
            import traceback; traceback.print_exc()
        try:
            sc_gate = scalp_regime_gate(k, DYNC)
            dyn_sc = (DYNC.get(k, {}).get("sc") or {}).get("score") if DYNC else None
            ssig = compute_scalp(bn[k], dyn_sc)
            trade_engine_scalp(k, ssig, state, gate, sc_gate, DEFG.get(k), events)
            print(f"{k} 超短线: sig={ssig['ssig']:+.2f} 持仓={'有' if state['positions_scalp'].get(k) else '无'}"
                  f" 闸门={'关' if sc_gate['blocked'] else '开'}", flush=True)
        except Exception as e:
            print(f"⚠️ {k} 超短线引擎异常: {e}", flush=True)
            import traceback; traceback.print_exc()
    # 6) 信号快照（网页展示用）
    state["signals"] = {}
    for k in bn:
        try:
            sig = compute_signal(bn[k])
            isig_ok = state.get("sema", {}).get(k)
            state["signals"][k] = {
                "price": bn[k]["now"], "S": sig["S"], "dynSW": sig["dynSW"],
                "finalPos": sig["finalPos"], "atr": sig["atr"], "risk": sig["risk"],
                "intraSig": isig_ok,
                "swLive": DYNC.get(k, {}).get("sw", {}).get("live") if DYNC else None,
                "idLive": DYNC.get(k, {}).get("id", {}).get("live") if DYNC else None,
                "scalpSig": (DYNC.get(k, {}).get("sc") or {}).get("score") if DYNC else None,
                "scLive": DYNC.get(k, {}).get("sc", {}).get("live") if DYNC else None,
            }
        except Exception:
            pass
    # 7) 落盘（追单类事件去重：与最近20条完全相同的不再重复记录，防止每15分钟刷屏）
    for ev in events:
        print("  " + ev, flush=True)
    recent = set(state.get("events", [])[-20:])
    for e in events:
        line = f"[{bj_str()}] {e}"
        if not any(e in r for r in recent):
            state.setdefault("events", []).append(line)
    save_state(state_path, state)
    TRADE_ICONS = ("🆕", "⚡", "🔥", "🛑", "💰", "🔄", "🏁", "🛡", "⏰", "🩹")
    trade_evs = [e for e in events if e.startswith(TRADE_ICONS)]
    print(f"完成：{len(events)} 条事件（{len(trade_evs)} 条交易类），状态已写入 {state_path}", flush=True)
    # 交易类事件写入标记文件，供 workflow 决定是否发邮件（只含开平仓等交易事件，不含追单提示等噪音）
    if trade_evs:
        with open("data/live_events_pending.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(trade_evs))
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--select", default=SELECT_PATH)
    a = ap.parse_args()
    sys.exit(run(a.state, a.select))
