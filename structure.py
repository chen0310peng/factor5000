# -*- coding: utf-8 -*-
"""structure.py — 市场结构识别模块（订单块 OB / FVG 公允价值缺口 / 摆动点）

设计目标：给止盈止损提供"结构锚点"，替代纯 ATR 倍数。
  - 止损：放在最近同向结构位外侧（OB 边沿/摆动点 ± buffer），减少被猎杀
  - 移动止损：新结构形成时随之上移（只向盈利方向动）
  - 止盈：与前方反向结构区对齐，到结构位先落袋

全部函数只依赖 OHLCV numpy 数组，云端 5 分钟一巡算得起（500 根 4h 全量重算 <10ms）。
"""
import numpy as np


def atr(h, l, c, n=14):
    """Wilder ATR（简单均值版，与引擎内现有 ATR 口径一致）"""
    if len(c) < 2: return float("nan")
    tr = [h[0]-l[0]] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
                        for i in range(1, len(c))]
    w = tr[-n:]
    return float(np.mean(w)) if w else float("nan")


def swing_points(h, l, left=2, right=2):
    """分形摆动点。返回 (swing_highs, swing_lows)，各为 [(idx, price), ...] 按时间升序。
    right>0 意味着最后 right 根K线内不可能产生新摆动点（未来函数防护：只用 idx <= len-1-right 的点）。"""
    n = len(h)
    sh, sl = [], []
    for i in range(left, n-right):
        if h[i] == max(h[i-left:i+right+1]) and h[i] > max(h[i-left:i]) and h[i] > max(h[i+1:i+right+1]):
            sh.append((i, float(h[i])))
        if l[i] == min(l[i-left:i+right+1]) and l[i] < min(l[i-left:i]) and l[i] < min(l[i+1:i+right+1]):
            sl.append((i, float(l[i])))
    return sh, sl


def find_order_blocks(o, h, l, c, v=None, max_keep=12):
    """识别订单块（ICT 简化版，确定性规则）：

    看涨OB = 向上BOS（收盘价突破最近一个摆动高点）发生前、该摆动高点之后成立的
             最后一根阴线，区间取 [low, high]。
    看跌OB = 镜像。
    每个 OB 记录：dir(+1涨/-1跌), top, bottom, idx(形成K线), bos_idx(突破确认K线),
                 strength(位移强度=突破后3根最大位移/ATR), vol_x(成交量倍数),
                 mitigated(后续收盘是否已跌穿/突破区间→失效), tapped(价格是否回过区内)
    返回未被完全消耗的 OB 列表，按形成时间近→远排序，最多 max_keep 个/方向。
    """
    n = len(c)
    a = atr(h, l, c)
    sh, sl = swing_points(h, l)
    vmean = float(np.mean(v[-20:])) if v is not None and len(v) >= 20 else None
    obs = []

    for i in range(3, n):
        # —— 向上 BOS：收盘价突破 i 之前最近的摆动高点 ——
        prev_sh = [s for s in sh if s[0] < i-1]
        if prev_sh:
            si, sp = prev_sh[-1]
            if c[i] > sp and c[i-1] <= sp:                    # 本根确认突破
                # 突破前的最后一根阴线（在摆动高点形成之后找）
                for j in range(i-1, si-1, -1):
                    if c[j] < o[j]:
                        disp = (max(c[i:i+4]) - h[j]) / (a or 1e-9)   # 位移强度
                        vx = (v[j]/vmean) if vmean else 1.0
                        obs.append({"dir": 1, "top": float(h[j]), "bottom": float(l[j]),
                                    "idx": j, "bos_idx": i,
                                    "strength": round(float(disp), 2),
                                    "vol_x": round(float(vx), 2)})
                        break
        # —— 向下 BOS：镜像 ——
        prev_sl = [s for s in sl if s[0] < i-1]
        if prev_sl:
            si, sp = prev_sl[-1]
            if c[i] < sp and c[i-1] >= sp:
                for j in range(i-1, si-1, -1):
                    if c[j] > o[j]:
                        disp = (l[j] - min(c[i:i+4])) / (a or 1e-9)
                        vx = (v[j]/vmean) if vmean else 1.0
                        obs.append({"dir": -1, "top": float(h[j]), "bottom": float(l[j]),
                                    "idx": j, "bos_idx": i,
                                    "strength": round(float(disp), 2),
                                    "vol_x": round(float(vx), 2)})
                        break

    # —— 消耗判定：之后出现收盘穿破 OB 远端边沿 → 失效；价格回过区间内 → tapped ——
    alive = []
    for ob in obs:
        k0 = ob["bos_idx"] + 1
        if k0 >= n: k0 = n
        if ob["dir"] == 1:
            ob["mitigated"] = bool(np.any(c[k0:] < ob["bottom"])) if k0 < n else False
            ob["tapped"] = bool(np.any(l[k0:] <= ob["top"])) if k0 < n else False
        else:
            ob["mitigated"] = bool(np.any(c[k0:] > ob["top"])) if k0 < n else False
            ob["tapped"] = bool(np.any(h[k0:] >= ob["bottom"])) if k0 < n else False
        if not ob["mitigated"]:
            alive.append(ob)
    # 每个方向最多保留最近的 max_keep 个
    out = []
    for d in (1, -1):
        pool = [x for x in alive if x["dir"] == d]
        pool.sort(key=lambda x: -x["bos_idx"])
        out.extend(pool[:max_keep])
    return out


def find_fvgs(h, l, min_gap_atr=0.1):
    """FVG 公允价值缺口：三根K线，第1根高点 < 第3根低点 → 看涨缺口 [h[i], l[i+2]]；镜像看跌。
    过滤太小的缺口（<0.1×ATR 视为噪声）。返回未完全回填的缺口列表。"""
    n = len(h)
    # 用近14根平均波幅近似 ATR（此函数拿不到收盘价序列时足够用）
    rng = [h[i]-l[i] for i in range(max(0, n-14), n)]
    a = float(np.mean(rng)) if rng else 0.0
    fvgs = []
    for i in range(0, n-2):
        if l[i+2] > h[i] and (l[i+2]-h[i]) >= min_gap_atr*(a or 1e-9):
            top, bot = float(l[i+2]), float(h[i])
            filled = bool(np.any(l[i+3:] <= bot)) if i+3 < n else False
            if not filled:
                fvgs.append({"dir": 1, "top": top, "bottom": bot, "idx": i+1})
        if h[i+2] < l[i] and (l[i]-h[i+2]) >= min_gap_atr*(a or 1e-9):
            top, bot = float(l[i]), float(h[i+2])
            filled = bool(np.any(h[i+3:] >= top)) if i+3 < n else False
            if not filled:
                fvgs.append({"dir": -1, "top": top, "bottom": bot, "idx": i+1})
    return fvgs


def structure_levels(o, h, l, c, v=None, price=None):
    """一站式输出当前结构图谱：
    { support: 价格下方最近支撑（OB底/摆动低/FVG底中最高者）,
      resist:  价格上方最近阻力（OB顶/摆动高/FVG顶中最低者）,
      supports: 下方全部结构位（高→低）, resists: 上方全部（低→高）,
      obs, fvgs, swing_highs, swing_lows, atr }
    """
    price = float(price if price is not None else c[-1])
    obs = find_order_blocks(o, h, l, c, v)
    fvgs = find_fvgs(h, l)
    sh, sl = swing_points(h, l)

    sup, res = [], []
    for ob in obs:
        if ob["dir"] == 1:   # 看涨OB是支撑
            lv = ob["bottom"]
            if lv < price: sup.append((lv, f"OB{'鲜' if not ob['tapped'] else '回'}×{ob['strength']:.1f}"))
        else:
            lv = ob["top"]
            if lv > price: res.append((lv, f"OB{'鲜' if not ob['tapped'] else '回'}×{ob['strength']:.1f}"))
    for f in fvgs:
        if f["dir"] == 1 and f["bottom"] < price: sup.append((f["bottom"], "FVG"))
        if f["dir"] == -1 and f["top"] > price: res.append((f["top"], "FVG"))
    for _, p in sl:
        if p < price: sup.append((p, "摆动低"))
    for _, p in sh:
        if p > price: res.append((p, "摆动高"))

    sup.sort(key=lambda x: -x[0]); res.sort(key=lambda x: x[0])
    return {"price": price, "atr": atr(h, l, c),
            "support": sup[0] if sup else None, "resist": res[0] if res else None,
            "supports": sup[:6], "resists": res[:6],
            "obs": obs, "fvgs": fvgs, "swing_highs": sh, "swing_lows": sl}
