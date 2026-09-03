# -*- coding: utf-8 -*-
"""WorldQuant 101 Alphas 时序化改造 → 入库 factors.db
27 个可时序化的 alpha 族 × 3 个周期（1d/4h/15m）= 81 条记录。
改造规则：横截面 rank → rolling_percentile_rank(x,120)；correlation → rolling_corr；
decay_linear → WMA；ts_rank → PCTL；advN → SMA(v,N)；vwap → VWAPN(h,l,c,v,20)。
"""
import sqlite3, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsl import compile_formula

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'factor5000', 'factors.db')

# JS 端 DYNF 已实现的函数白名单（编译产物不得引用白名单外函数）
JS_WHITELIST = set("""shift SMA MA EMA WMA STD SUM HHV LLV ROC RSI ATR CCI CMO BIAS ZSCORE PCTL
linreg_slope D1 D2 ABS sqrt sign score MAX MIN rolling_std rolling_sum rolling_mean rolling_max
rolling_min rolling_percentile_rank HV KDJK KDJJ KDJD SARP UPV DNV FRACUP FRACDNSH FRACUPSH BODYR
FRACBIGBODY CONSECN DIVG TODAYH TODAYL YH YL VWAPN ORBSIG UO TODZRAW TODZ_ALL TODREL TODZ
rolling_corr corr ln log exp tanh cumsum pow_ ADX MFI PDI MDI DIDIFF OBV""".split())

# {alpha号: (中文名, DSL公式)}
WQ = {
 '003': ('开盘价量排名背离', "OUT = -1 * rolling_corr(rolling_percentile_rank(o,120), rolling_percentile_rank(v,120), 10)"),
 '004': ('低价时序排名反转', "OUT = -1 * PCTL(rolling_percentile_rank(l,120), 9)"),
 '005': ('开盘偏离VWAP反转', "VW = VWAPN(h,l,c,v,20); OUT = -1 * rolling_percentile_rank(o - SMA(VW,10), 120) * ABS(rolling_percentile_rank(c - VW, 120))"),
 '006': ('开盘价量负相关', "OUT = -1 * rolling_corr(o, v, 10)"),
 '008': ('开盘收益乘积反转', "R5 = SUM(o,5) * SUM(c/shift(c,1)-1, 5); OUT = -1 * rolling_percentile_rank(R5 - shift(R5,10), 120)"),
 '009': ('极小动量条件反转', "D = c - shift(c, 1); OUT = (((LLV(D, 5) > 0) + (HHV(D, 5) < 0)) * 2 - 1) * D"),
 '012': ('量增价跌反向', "OUT = sign(v - shift(v, 1)) * (-1 * (c - shift(c, 1)))"),
 '013': ('价量协变排名反转', "OUT = -1 * rolling_percentile_rank(rolling_corr(rolling_percentile_rank(c,120), rolling_percentile_rank(v,120), 5), 120)"),
 '014': ('收益加速度×开量相关', "R = c/shift(c,1)-1; OUT = (-1 * rolling_percentile_rank(R - shift(R, 3), 120)) * rolling_corr(o, v, 10)"),
 '015': ('高价量相关平滑反转', "T = rolling_corr(rolling_percentile_rank(h,120), rolling_percentile_rank(v,120), 3); OUT = -1 * SUM(rolling_percentile_rank(T,120), 3)"),
 '016': ('高价量协变反转', "OUT = -1 * rolling_percentile_rank(rolling_corr(rolling_percentile_rank(h,120), rolling_percentile_rank(v,120), 5), 120)"),
 '017': ('价排名×加速×量比', "D1v = c - shift(c,1); T1 = -1 * rolling_percentile_rank(PCTL(c,10), 120); T2 = rolling_percentile_rank(D1v - shift(D1v,1), 120); T3 = rolling_percentile_rank(PCTL(v/SMA(v,20), 5), 120); OUT = T1 * T2 * T3"),
 '018': ('实体波动相关反转', "OUT = -1 * rolling_percentile_rank(STD(ABS(c-o),5) + (c-o) + rolling_corr(c,o,10), 120)"),
 '019': ('周动量符号×长期收益', "OUT = -1 * sign(c - shift(c, 7)) * (1 + rolling_percentile_rank(1 + SUM(c / shift(c, 1) - 1, 250), 250))"),
 '020': ('隔夜跳空三连反转', "OUT = (-1 * rolling_percentile_rank(o - shift(h,1), 120)) * rolling_percentile_rank(o - shift(c,1), 120) * rolling_percentile_rank(o - shift(l,1), 120)"),
 '021': ('均值回归双条件', "M8 = SMA(c, 8); S8 = STD(c, 8); M2 = SMA(c, 2); OUT = -1*(M2 > M8 + S8) + 1*(M2 < M8 - S8) + (2*(v >= SMA(v, 20)) - 1)*((M2 <= M8 + S8) * (M2 >= M8 - S8))"),
 '022': ('价量相关变化×波动', "R5 = rolling_corr(h, v, 5); OUT = -1 * (R5 - shift(R5, 5)) * rolling_percentile_rank(STD(c, 20), 120)"),
 '023': ('新高过量回调', "OUT = (SMA(h,20) < h) * (-1 * (h - shift(h,2)))"),
 '025': ('量价VWAP乘积反转', "R = c/shift(c,1)-1; VW = VWAPN(h,l,c,v,20); OUT = rolling_percentile_rank((-1 * R) * SMA(v,20) * VW * (h - c), 120)"),
 '026': ('量价排名相关极值', "OUT = -1 * HHV(rolling_corr(PCTL(v,5), PCTL(h,5), 5), 3)"),
 '033': ('开收比排名', "OUT = rolling_percentile_rank(o/c - 1, 120)"),
 '040': ('高价波动×价量相关', "OUT = (-1 * rolling_percentile_rank(STD(h,10), 120)) * rolling_corr(h, v, 10)"),
 '041': ('几何均价偏离VWAP', "OUT = sqrt(h * l) - VWAPN(h,l,c,v,20)"),
 '043': ('量比排名×反转排名', "OUT = PCTL(v/SMA(v,20), 20) * PCTL(-1 * (c - shift(c,7)), 8)"),
 '044': ('高价量排名相关', "OUT = -1 * rolling_corr(h, rolling_percentile_rank(v,120), 5)"),
 '053': ('收盘位置变化反转', "X = ((c - l) - (h - c)) / ((c - l) + (h - c) + 0.001*c); OUT = -1 * (X - shift(X, 9))"),
 '054': ('开低收幂次比', "OUT = -1 * ((l - c) * o**5) / ((l - h) * c**5 + 0.0001)"),
 '101': ('日内实体占比', "OUT = (c - o) / ((h - l) + 0.001 * c)"),
}

TFS = [('1d', 1, '波段'), ('4h', 6, '日内'), ('15m', 96, '日内')]
USAGE = ('WorldQuant 101 Alphas 时序化改造版（原横截面因子改为单标的时序因子：'
         'rank→滚动分位、correlation→滚动相关、vwap→20周期VWAP）。'
         '信号>0偏多、<0偏空，绝对值越大信号越强；由每日动态回测决定是否上岗，'
         '仅作合成评分输入，不单独作为开平仓依据。')

def js_check(src):
    """检查编译产物里引用的函数是否都在 JS 白名单内"""
    fns = set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', src))
    bad = fns - JS_WHITELIST
    return bad

def main():
    db = sqlite3.connect(DB)
    cur = db.cursor()
    cur.execute("DELETE FROM factors WHERE id LIKE 'WQ-%'")
    cur.execute("DELETE FROM runnable WHERE id LIKE 'WQ-%'")
    n_ok, n_skip = 0, 0
    for alpha, (cname, formula) in sorted(WQ.items()):
        for tf, pd_, cat in TFS:
            fid = f"WQ-{alpha}-{tf}"
            try:
                src = compile_formula(formula, pd_)
            except Exception as e:
                print(f"[编译失败] {fid}: {e}")
                n_skip += 1
                continue
            bad = js_check(src)
            if bad:
                print(f"[JS不支持] {fid}: 函数 {sorted(bad)}")
                n_skip += 1
                continue
            cur.execute(
                "INSERT INTO factors (id,cat,family,name,formula,params,tf,datareq,datasource,usage,computable,reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,'')",
                (fid, cat, 'WorldQuant101改造', f"WQ{alpha}_{cname}_{tf}",
                 formula, f'alpha#{alpha}', tf, 'K线OHLCV', 'kline', USAGE))
            cur.execute("INSERT INTO runnable (id, per_day) VALUES (?,?)", (fid, pd_))
            n_ok += 1
    db.commit()
    cur.execute("SELECT cat, tf, COUNT(*) FROM factors WHERE id LIKE 'WQ-%' GROUP BY cat, tf")
    for r in cur.fetchall(): print(r)
    db.close()
    print(f"\n入库 {n_ok} 条，跳过 {n_skip} 条")

if __name__ == '__main__':
    main()
