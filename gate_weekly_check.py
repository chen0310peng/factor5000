# -*- coding: utf-8 -*-
"""行情准入闸门·每周稳健性复核（GitHub Actions 每周一运行）
目的：监控浏览器端固定参数（7日窗口/0.9进场线）是否持续有效。
原则：只监控、只报警，绝不自动改参数 —— 连续3周失效才标记 review_needed，由人工决定是否调整。
输出：data/factor5000/gate_check.json（含每周历史）
"""
import json, os, sqlite3, sys, time
import numpy as np
import pandas as pd
import dynamic_factor_select as dfs
from dsl import compile_formula

CACHE = "data/factor5000/cache"
FEE = 0.0004
ALPHA_BAR = 1-0.7**5
CUR_W, CUR_GE = 7, 0.9          # 线上固定参数
OUT = "data/factor5000/gate_check.json"

j = json.load(open("data/factor5000/selected_top500.json", encoding="utf-8"))
print(f"因子池 {j['generated_at']}", flush=True)
pool = j["selected"]["日内"]
db = sqlite3.connect("data/factor5000/factors.db")
pdmap = dict(db.execute("SELECT id, per_day FROM runnable").fetchall())
db.close()

# 拉新数据（每周运行缓存必然过期，自动走 币安→Bybit→OKX 三级容灾）
D, fund, oi = dfs.load_data()
RAW = {s: {tf: D[f"bn_{s}_{tf}"] for tf in ["4h","1h","15m","5m","1d"]} for s in ["BTCUSDT","ETHUSDT"]}

def ratio_for(tf):
    b = RAW["BTCUSDT"][tf][["t","c"]].rename(columns={"c":"b"})
    e = RAW["ETHUSDT"][tf][["t","c"]].rename(columns={"c":"e"})
    rr = pd.merge_asof(b.sort_values("t"), e.sort_values("t"), on="t")
    return pd.DataFrame({"t": rr["t"], "r": rr["e"]/rr["b"]})

def tf_env_key(tf):
    if "15m" in tf: return "m15"
    if "5m" in tf or "1m" in tf or "3m" in tf: return "m5"
    if "30m" in tf: return "m30"
    if "8h" in tf: return "h8"
    if "4h" in tf: return "h4"
    if "1d" in tf or "日" in tf: return "d1"
    return "h1"

def build_all_envs(sym):
    R = RAW[sym]; envs = {}
    def pack(df, fd, o, rt): e = dfs.build_env(df, fd, o, rt); e["t"] = df["t"].values; return e
    envs["d1"] = pack(R["1d"], fund[sym], oi[sym], ratio_for("1d"))
    envs["h4"] = pack(R["4h"], fund[sym], oi[sym], ratio_for("4h"))
    envs["h8"] = pack(dfs.resample(R["4h"], "8h"), fund[sym], oi[sym], ratio_for("4h"))
    envs["h1"] = pack(R["1h"], fund[sym], oi[sym], ratio_for("1h"))
    envs["m30"] = pack(dfs.resample(R["15m"], "30min"), None, None, ratio_for("15m"))
    envs["m15"] = pack(R["15m"], None, None, ratio_for("15m"))
    envs["m5"] = pack(R["5m"], None, None, ratio_for("5m"))
    return envs

def dyn_composite(sym):
    envs = build_all_envs(sym)
    grid = RAW[sym]["15m"]["t"].values
    comp = np.zeros(len(grid)); wsum_t = np.zeros(len(grid)); live = 0
    for f in pool:
        env = envs.get(tf_env_key(f["tf"]))
        if env is None or len(env["c"]) < 60: continue
        pd_ = pdmap.get(f["id"], 24)
        try: arr = dfs.eval_factor(compile_formula(f["formula"], pd_), env)
        except Exception: continue
        if arr is None or np.isnan(arr).all(): continue
        s = pd.Series(arr)
        m = s.rolling(200, min_periods=30).mean(); sd = s.rolling(200, min_periods=30).std()
        z = np.clip(((s-m)/sd.replace(0,np.nan)).values, -4, 4)
        zg = dfs.asof_series(grid, env["t"], z)
        comp += f["dir"]*np.nan_to_num(zg)*f["w"]; wsum_t += f["w"]*(~np.isnan(zg)); live += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(wsum_t>0, comp/np.where(wsum_t==0,1,wsum_t), np.nan)
    print(f"  {sym} 日内池可复算 {live}/{len(pool)}", flush=True)
    return np.clip(out, -3, 3)

def static_anchor(sym):
    R = RAW[sym]["4h"]; c = R["c"].values; v = R["v"].values
    ret = np.concatenate([[np.nan], c[1:]/c[:-1]-1])
    v6 = pd.Series(v).rolling(6).mean().values
    volr = v/np.where(v6==0,np.nan,v6)
    asym = ret*volr
    mom24 = np.concatenate([np.full(6,np.nan), c[6:]/c[:-6]-1])
    def zW(a, w=42):
        s = pd.Series(a); m = s.rolling(w).mean(); sd = s.rolling(w).std()
        return ((s-m)/sd.replace(0,np.nan)).values
    sig = np.clip((-zW(asym)-zW(mom24))/2, -3, 3)
    return dfs.asof_series(RAW[sym]["15m"]["t"].values, R["t"].values, sig)

def gate_series(Sg, CC, W_days, g_entry):
    n = len(Sg); barsW = W_days*96; bars24h = 96
    out = np.zeros(n, dtype=bool)
    for i in range(barsW, n, 4):
        seg = Sg[i-barsW:i]
        posD=0; entry=0.0; held=0; pnl=0.0; trades=0
        for k2 in range(len(seg)):
            Si = seg[k2]
            if not np.isfinite(Si): continue
            if posD==0:
                if abs(Si)>=g_entry: posD=np.sign(Si); entry=CC[i-barsW+k2]; held=0; pnl-=FEE; trades+=1
            else:
                held+=1
                if (np.sign(Si)==-posD and abs(Si)>=1.0) or held>=bars24h:
                    pnl+=posD*(CC[i-barsW+k2]/entry-1)-FEE; posD=0
        if posD!=0: pnl+=posD*(CC[i-1]/entry-1)-FEE
        out[i:min(n,i+4)] = (pnl<0 and trades>=5)
    return out

def run_engine(sym, S_raw, gate=None):
    R = RAW[sym]["15m"].tail(96*60).reset_index(drop=True)
    t = R["t"].values; P = R["c"].values; H = R["h"].values; L = R["l"].values
    Sg = dfs.asof_series(t, RAW[sym]["15m"]["t"].values, S_raw)
    g = gate if gate is not None else np.zeros(len(t), dtype=bool)
    ATR4 = pd.Series(RAW[sym]["4h"]["h"].values-RAW[sym]["4h"]["l"].values).rolling(14).mean().values
    Ag = dfs.asof_series(t, RAW[sym]["4h"]["t"].values, ATR4)
    S_ema = np.nan; pos = None; cd_until = -1; pause_until = -1
    opp_run = 0; trades = []; eq = 0.0
    for i in range(len(t)):
        if not np.isfinite(Sg[i]): continue
        S_ema = Sg[i] if not np.isfinite(S_ema) else S_ema*(1-ALPHA_BAR)+Sg[i]*ALPHA_BAR
        S = S_ema; price = P[i]; A = Ag[i]
        hour = (t[i]//3600000)%24
        if not np.isfinite(A) or A<=0: continue
        if pos is None:
            if g[i] or t[i] < cd_until or t[i] < pause_until: continue
            zone = 1 if S>=0.9 else (-1 if S<=-0.9 else 0)
            if zone and hour < 20:
                d = zone
                pos = dict(dir=d, entry=price, size=6.0, stop=price-d*1.5*A,
                           add=price-d*1.0*A, tp1=price+d*1.0*A, tp2=price+d*2.0*A,
                           tp3=price+d*3.0*A, i0=i, tp1d=False, tp2d=False, addd=False)
                eq -= FEE*0.06; opp_run = 0
        else:
            d = pos["dir"]; hold_h = (t[i]-t[pos["i0"]])/3600000
            def close(px, reason, frac=1.0):
                nonlocal eq, pos
                pnl_u = d*(px/pos["entry"]-1)
                eq += pnl_u*(pos["size"]*frac/100) - FEE*(pos["size"]*frac/100)
                trades.append(dict(pnl=pnl_u, reason=reason)); pos["size"] *= (1-frac)
                if frac >= 0.999: pos = None
            hit_dn = lambda lv: L[i]<=lv if d>0 else H[i]>=lv
            hit_up = lambda lv: H[i]>=lv if d>0 else L[i]<=lv
            closed = False
            if hit_dn(pos["stop"]):
                close(pos["stop"], "止损"); closed=True
                if len(trades)>=3 and all(x["reason"]=="止损" for x in trades[-3:]): pause_until = t[i]+2*3600000
            elif hit_up(pos["tp3"]): close(pos["tp3"], "止盈3"); closed=True
            elif hold_h>=24: close(price, "满24h"); closed=True
            elif hour==0: close(price, "日界"); closed=True
            else:
                opp = np.sign(S)==-d and abs(S)>=1.0
                opp_run = opp_run+1 if opp else 0
                if opp and hold_h>=4 and opp_run>0:
                    close(price, "反转"); closed=True; cd_until = t[i]+45*60000
            if not closed and pos is not None:
                if not pos["tp1d"] and hit_up(pos["tp1"]):
                    pos["tp1d"]=True; eq += d*(pos["tp1"]/pos["entry"]-1)*(pos["size"]/3/100) - FEE*(pos["size"]/3/100)
                    pos["size"]*=2/3; pos["stop"]=pos["entry"]
                if pos["tp1d"] and not pos["tp2d"] and hit_up(pos["tp2"]):
                    pos["tp2d"]=True; eq += d*(pos["tp2"]/pos["entry"]-1)*(pos["size"]/2/100) - FEE*(pos["size"]/2/100)
                    pos["size"]/=2; pos["stop"]=pos["tp1"]
                if not pos["addd"] and hit_dn(pos["add"]):
                    pos["addd"]=True; ns = pos["size"]+4.0
                    pos["entry"] = (pos["entry"]*pos["size"]+pos["add"]*4.0)/ns
                    pos["size"] = ns; eq -= FEE*0.04
    if pos is not None:
        d=pos["dir"]; eq += d*(P[-1]/pos["entry"]-1)*(pos["size"]/100)
        trades.append(dict(pnl=d*(P[-1]/pos["entry"]-1), reason="期末"))
    wins = [x for x in trades if x["pnl"]>0]
    return dict(total=eq*100, n=len(trades), win=len(wins)/max(1,len(trades))*100)

# ---------- 主流程 ----------
S = {}
for sym in ["BTCUSDT","ETHUSDT"]:
    dyn = dyn_composite(sym); sta = static_anchor(sym)
    S_raw = np.clip(0.3*np.nan_to_num(sta) + dyn, -3, 3)
    S_raw[~np.isfinite(dyn)] = np.nan
    S[sym] = S_raw

base_tot = sum(run_engine(sym, S[sym])["total"] for sym in S)
print(f"无闸门基线: {base_tot:+.2f}%", flush=True)

grid = []
for W in [3, 5, 7, 10]:
    for ge in [0.7, 0.9, 1.1]:
        tot = 0
        for sym in S:
            CC = RAW[sym]["15m"]["c"].values
            gs = gate_series(S[sym], CC, W, ge)[-96*60:]
            tot += run_engine(sym, S[sym], gate=gs)["total"]
        grid.append({"W": W, "ge": ge, "total": round(float(tot), 2), "improved": bool(tot > base_tot)})
        print(f"  W={W} ge={ge}: {tot:+.2f}% ({'改善' if tot>base_tot else '未改善'})", flush=True)

cur = [g for g in grid if g["W"]==CUR_W and g["ge"]==CUR_GE][0]
n_improved = sum(1 for g in grid if g["improved"])

hist = []
if os.path.exists(OUT):
    try: hist = json.load(open(OUT, encoding="utf-8")).get("history", [])
    except Exception: hist = []
hist.append({"week": time.strftime("%Y-%m-%d"), "pool": j["generated_at"],
             "baseline": round(float(base_tot), 2), "current_params": float(cur["total"]),
             "current_improved": bool(cur["improved"]), "grid_improved": int(n_improved)})
hist = hist[-12:]   # 保留最近12周
review = len(hist) >= 3 and all(not h["current_improved"] for h in hist[-3:])

result = {"generated_at": time.strftime("%Y-%m-%d %H:%M"),
          "params_locked": {"W": CUR_W, "ge": CUR_GE},
          "baseline_60d": round(float(base_tot), 2),
          "current_params_60d": float(cur["total"]),
          "grid": grid, "history": hist,
          "review_needed": bool(review),
          "note": "只监控不自动改参数；连续3周未改善才标记review_needed，由人工复核"}
json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n本周：当前参数(7日/0.9) {cur['total']:+.2f}% vs 基线 {base_tot:+.2f}% ｜ 网格{n_improved}/12改善 ｜ review_needed={review}", flush=True)
