# -*- coding: utf-8 -*-
"""只验证 WQ- 因子：样本内外 IC + 模拟夏普（币安 BTC/ETH，用本地缓存数据）"""
import sys, os, sqlite3, numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'cloud_deploy'))
os.chdir(ROOT)
from dynamic_factor_select import (load_data, build_env, eval_factor,
                                   spearman, trade_sim, src_keys)
from dsl import compile_formula

def main():
    db = sqlite3.connect('data/factor5000/factors.db')
    rows = db.execute("""SELECT f.id, f.cat, f.name, f.formula, f.tf, r.per_day
                         FROM factors f JOIN runnable r ON f.id=r.id
                         WHERE f.id LIKE 'WQ-%'""").fetchall()
    print(f"WQ 因子 {len(rows)} 个")
    D, fund, oi = load_data()
    ratios = {}
    for tf in ["4h", "15m", "1d"]:
        kb, ke = f"bn_BTCUSDT_{tf}", f"bn_ETHUSDT_{tf}"
        if kb not in D or ke not in D: continue
        b = D[kb][["t","c"]].rename(columns={"c":"b"}); e = D[ke][["t","c"]].rename(columns={"c":"e"})
        import pandas as pd
        rr = pd.merge_asof(b.sort_values("t"), e.sort_values("t"), on="t")
        ratios[tf] = __import__('pandas').DataFrame({"t": rr["t"], "r": rr["e"]/rr["b"]})
    envs = {}
    for tf in ["4h", "15m", "1d"]:
        for sym in ["BTCUSDT", "ETHUSDT"]:
            df = D.get(f"bn_{sym}_{tf}")
            if df is None: continue
            envs[(sym, tf)] = build_env(df, fund.get(sym), oi.get(sym), ratios.get(tf))
    HZN = {"波段": lambda p: max(1,p), "日内": lambda p: max(1,p//6)}
    out = []
    for fid, cat, name, formula, tf, pd_ in rows:
        try: src = compile_formula(formula, pd_)
        except Exception as e:
            print(f"[编译失败] {fid} {e}"); continue
        hz = HZN[cat](pd_)
        res = {}
        for sym in ["BTCUSDT", "ETHUSDT"]:
            env = envs.get((sym, tf))
            if env is None: continue
            try: fac = eval_factor(src, env)
            except Exception as e:
                res[sym] = ('ERR', str(e)[:40]); continue
            if fac is None or np.isnan(fac).all():
                res[sym] = ('NaN', ''); continue
            c = env["c"]; n = len(fac)
            fwd = np.concatenate([c[hz:]/c[:-hz]-1, [np.nan]*hz])
            cut = int(n*0.6)
            icE, _ = spearman(fac[:cut], fwd[:cut])
            icO, _ = spearman(fac[cut:], fwd[cut:])
            # 方向用前60% P&L，绩效看后40%
            sp = trade_sim(fac[:cut], c[:cut], pd_, +1)[0]
            sm = trade_sim(fac[:cut], c[:cut], pd_, -1)[0]
            sp = sp if not np.isnan(sp) else -9e9
            sm = sm if not np.isnan(sm) else -9e9
            d = 1 if sp >= sm else -1
            sh, win, pnl, _act = trade_sim(fac[cut:], c[cut:], pd_, d)
            res[sym] = (icE, icO, sh, win, d)
        if len(res) < 2 or any(isinstance(v[0], str) for v in res.values()):
            stat = {s: v for s, v in res.items()}
            print(f"[评估失败] {fid} {name} {stat}"); continue
        b, e = res["BTCUSDT"], res["ETHUSDT"]
        agree = (b[4] == e[4])
        out.append((fid, name, tf, (b[0]+e[0])/2, (b[1]+e[1])/2,
                    (b[2]+e[2])/2, (b[3]+e[3])/2, agree))
    out.sort(key=lambda x: -abs(x[4]))
    print(f"\n{'id':<14}{'tf':<5}{'IC内':>8}{'IC外':>8}{'夏普外':>8}{'胜率外':>8}  双币同向  名称")
    for r in out:
        print(f"{r[0]:<14}{r[2]:<5}{r[3]:>+8.4f}{r[4]:>+8.4f}{r[5]:>8.2f}{r[6]*100:>7.1f}%  {'✓' if r[7] else '✗':^6}  {r[1]}")
    good = [r for r in out if abs(r[4]) >= 0.02 and r[7]]
    print(f"\n样本外|IC|≥0.02 且双币同向: {len(good)}/{len(out)}")

if __name__ == '__main__':
    main()
