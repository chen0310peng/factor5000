# -*- coding: utf-8 -*-
"""云端引擎交易邮件渲染与发送（HTML 版，排版对齐本地页面云端面板）。
输入：data/live_events_pending.txt（本次交易事件）+ data/live_state.json（账本/持仓/信号）
用法：
    python send_mail.py                  # 正常发送（workflow 调用）
    python send_mail.py --preview out.html   # 只生成预览文件不发送
环境变量：MAIL_USER / MAIL_PASS / MAIL_TO（QQ 邮箱授权码体系，多个收件人用逗号分隔）
"""
import json, os, re, sys, smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
GREEN, RED, BLUE, GRAY, GOLD = "#3ddc84", "#ff6b6b", "#5ec1ff", "#9fb4c8", "#ffd27d"
BG, CARD, BORDER = "#0b1220", "#0f1a26", "#1c2a3a"

def bj_now():
    return datetime.now(BJ)

def num(s):
    return float(str(s).replace(",", ""))

def fmt(p):
    try: return f"{float(p):,.1f}"
    except (TypeError, ValueError): return "—"

def pct_color(v):
    return GREEN if v >= 0 else RED

def dir_color(d):
    return GREEN if d == "多" else RED

# ================= 事件解析 =================
def parse_events(lines):
    """把事件文本解析为结构化卡片数据；解析不了的走原文兜底卡片。"""
    items = []
    for e in lines:
        e = e.strip()
        if not e: continue
        it = None
        m = re.match(r"🆕 开(多|空) (\w+) 第一手 ([\d.]+)% @ \$([\d,.]+)(.*)", e)
        if m: it = {"kind": "open", "mode": "波段", "dir": m.group(1), "sym": m.group(2),
                    "size": num(m.group(3)), "price": num(m.group(4)), "tail": m.group(5)}
        m2 = re.match(r"⚡日内开(多|空) (\w+) 第一手([\d.]+)% @ \$([\d,.]+)(.*)", e)
        if m2: it = {"kind": "open", "mode": "日内", "dir": m2.group(1), "sym": m2.group(2),
                     "size": num(m2.group(3)), "price": num(m2.group(4)), "tail": m2.group(5)}
        m2b = re.match(r"🔥超短线开(多|空) (\w+) 第一手([\d.]+)% @ \$([\d,.]+)(.*)", e)
        if m2b: it = {"kind": "open", "mode": "超短线", "dir": m2b.group(1), "sym": m2b.group(2),
                      "size": num(m2b.group(3)), "price": num(m2b.group(4)), "tail": m2b.group(5)}
        m3 = re.match(r"➕ (\w+) (日内|超短线)?补仓 ([\d.]+)% @ \$([\d,.]+)，均价 \$([\d,.]+)", e)
        if m3: it = {"kind": "add", "mode": m3.group(2) or "波段", "sym": m3.group(1),
                     "size": num(m3.group(3)), "price": num(m3.group(4)), "avg": num(m3.group(5))}
        m4 = re.match(r"([🛑💰🔄🩹]) ?(\w+) (.+?) @ \$([\d,.]+)（([+-][\d.]+)%）", e)
        if m4: it = {"kind": "close", "icon": m4.group(1), "sym": m4.group(2),
                     "reason": m4.group(3), "price": num(m4.group(4)), "pnl": num(m4.group(5))}
        m5 = re.match(r"🏁 (\w+) (日内|超短线)平仓 @ \$([\d,.]+)（(.+?)，([+-][\d.]+)%）", e)
        if m5: it = {"kind": "close", "icon": "🏁", "sym": m5.group(1), "mode": m5.group(2),
                     "reason": m5.group(4), "price": num(m5.group(3)), "pnl": num(m5.group(5))}
        m6 = re.match(r"💰 (\w+) (日内|超短线)?止盈([12])平?(.+?) @ \$([\d,.]+)，(.+)", e)
        if m6: it = {"kind": "tp", "mode": m6.group(2) or "波段", "sym": m6.group(1),
                     "stage": m6.group(3), "portion": m6.group(4), "price": num(m6.group(5)), "note": m6.group(6)}
        m7 = re.match(r"⏰ (\w+) (波段|日内)持仓 ?([\d.]+) ?(天|h) ?未达止盈1，止损上移至成本 \$([\d,.]+)", e)
        if m7: it = {"kind": "movestop", "mode": m7.group(2), "sym": m7.group(1),
                     "hold": m7.group(3) + m7.group(4), "stop": num(m7.group(5))}
        m8 = re.match(r"⏰ (\w+) 波段持仓 ([\d.]+) 天到达上限，市价平仓 @ \$([\d,.]+)（([+-][\d.]+)%）", e)
        if m8: it = {"kind": "close", "icon": "⏰", "sym": m8.group(1), "mode": "波段",
                     "reason": "持仓到达上限", "price": num(m8.group(3)), "pnl": num(m8.group(4))}
        m9 = re.match(r"🛡 (\w+) 浮盈回撤保护平仓 @ \$([\d,.]+)：(.+)", e)
        if m9: it = {"kind": "close", "icon": "🛡", "sym": m9.group(1), "reason": "浮盈回撤保护平仓",
                     "price": num(m9.group(2)), "pnl": None, "note": m9.group(3)}
        if it is None:
            it = {"kind": "raw", "text": e}
        it["raw"] = e
        items.append(it)
    return items

# ================= 状态富化 =================
def find_trade(trades, sym, mode=None, open_only=False, closed_only=False):
    for t in reversed(trades):
        if t.get("sym") != sym: continue
        if mode and t.get("mode", "波段") != mode: continue
        if open_only and t.get("exitPrice") is not None: continue
        if closed_only and t.get("exitPrice") is None: continue
        return t
    return None

def hold_str(t0, t1):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            a = datetime.strptime(t0, f); b = datetime.strptime(t1, f)
            h = (b - a).total_seconds() / 3600
            return f"{h/24:.1f} 天" if h >= 24 else f"{h:.1f} 小时"
        except (ValueError, TypeError):
            continue
    return "—"

POS_KEY = {"波段": "positions", "日内": "positions_intra", "超短线": "positions_scalp"}
MODE_ICON = {"波段": "🌊", "日内": "⚡", "超短线": "🔥"}

def positions_overview(state):
    out = []
    price = {k: (v or {}).get("price") for k, v in (state.get("signals") or {}).items()}
    for key, mode in (("positions", "波段"), ("positions_intra", "日内"), ("positions_scalp", "超短线")):
        for sym, p in (state.get(key) or {}).items():
            if not p: continue
            d = "多" if p.get("dir", 0) > 0 else "空"
            now_p = price.get(sym)
            upnl = None
            if now_p and p.get("entry"):
                upnl = ((now_p / p["entry"] - 1) if d == "多" else (1 - now_p / p["entry"])) * 100
            out.append({"mode": mode, "sym": sym, "dir": d, "entry": p.get("entry"),
                        "now": now_p, "upnl": upnl, "size": p.get("sizePct"),
                        "stop": p.get("stop"), "add": p.get("add"), "addPct": p.get("addPct"),
                        "addDone": p.get("addDone"), "tp1": p.get("tp1"), "tp2": p.get("tp2"),
                        "tp3": p.get("tp3"), "entryTime": p.get("entryTime")})
    return out

def ledger_stats(trades):
    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed: return None
    wins = sum(1 for t in closed if t["pnl"] > 0)
    total_r = sum(t.get("realized") or 0 for t in closed)
    return {"n": len(closed), "win": wins / len(closed) * 100, "realized": total_r * 100}

# ================= HTML 渲染 =================
def kv_table(rows):
    tds = "".join(
        f'<td style="padding:6px 10px;background:#0b1622;border-radius:8px;">'
        f'<div style="font-size:11px;color:{GRAY};">{k}</div>'
        f'<div style="font-size:15px;font-weight:600;color:{c};">{v}</div></td>'
        for k, v, c in rows)
    return f'<table width="100%" cellpadding="0" cellspacing="4" style="border-collapse:separate;"><tr>{tds}</tr></table>'

def card(title, title_color, body_html, badges=""):
    return f'''
<table width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {BORDER};
  border-left:4px solid {title_color};border-radius:12px;margin:0 0 12px 0;">
  <tr><td style="padding:12px 14px;">
    <div style="font-size:16px;font-weight:700;color:{title_color};margin-bottom:8px;">{title}
      <span style="font-size:11px;font-weight:400;color:{GOLD};">{badges}</span></div>
    {body_html}
  </td></tr>
</table>'''

def render(items, state):
    trades = state.get("trades", [])
    cards = []
    for it in items:
        k = it["kind"]
        if k == "open":
            pos = (state.get(POS_KEY[it["mode"]]) or {}).get(it["sym"]) or {}
            badges = " ".join(f"〔{b.strip()}〕" for b in it["tail"].split("｜") if b.strip())
            rows = [("方向", f'{it["dir"]} {"📈" if it["dir"]=="多" else "📉"}', dir_color(it["dir"])),
                    ("开仓价", f'${fmt(it["price"])}', "#e6f1ff"),
                    ("第一手仓位", f'{it["size"]:.1f}%', BLUE),
                    ("止损", f'${fmt(pos.get("stop"))}', RED),
                    ("补仓位", f'${fmt(pos.get("add"))}' + (f'（再补 {pos.get("addPct"):.1f}%）' if pos.get("addPct") else ""), GOLD),
                    ("止盈1 / 2 / 3", f'{fmt(pos.get("tp1"))} / {fmt(pos.get("tp2"))} / {fmt(pos.get("tp3"))}', GREEN)]
            open_icon = {"波段": "🆕", "日内": "⚡", "超短线": "🔥"}[it["mode"]]
            cards.append(card(f'{open_icon} {it["mode"]}开{it["dir"]} · {it["sym"]} 永续',
                              dir_color(it["dir"]), kv_table(rows), badges))
        elif k == "add":
            pos = (state.get(POS_KEY[it["mode"]]) or {}).get(it["sym"]) or {}
            rows = [("补仓价", f'${fmt(it["price"])}', "#e6f1ff"),
                    ("新均价", f'${fmt(it["avg"])}', BLUE),
                    ("本笔补仓", f'{it["size"]:.1f}%', GOLD),
                    ("止损", f'${fmt(pos.get("stop"))}', RED),
                    ("止盈1", f'${fmt(pos.get("tp1"))}', GREEN)]
            cards.append(card(f'➕ {it["mode"]}补仓 · {it["sym"]}', GOLD, kv_table(rows)))
        elif k == "close":
            t = find_trade(trades, it["sym"], it.get("mode"), closed_only=True)
            entry = t.get("entry") if t else None
            hold = hold_str(t.get("entryTime"), t.get("exitTime")) if t else "—"
            d = t.get("dir", "?") if t else "?"
            pnl = it["pnl"]
            rows = [("方向", d, dir_color(d)),
                    ("开仓价 → 平仓价", f'{fmt(entry)} → {fmt(it["price"])}', "#e6f1ff"),
                    ("盈亏", f'{pnl:+.2f}%' if pnl is not None else "—", pct_color(pnl or 0)),
                    ("持仓时长", hold, GRAY)]
            note = f'<div style="font-size:12px;color:{GRAY};margin-top:4px;">{it.get("note","")}</div>' if it.get("note") else ""
            cards.append(card(f'{it["icon"]} {it["reason"]} · {it["sym"]}', pct_color(pnl or 0), kv_table(rows) + note))
        elif k == "tp":
            pos = (state.get(POS_KEY[it["mode"]]) or {}).get(it["sym"]) or {}
            rows = [("止盈档位", f'止盈{it["stage"]}（{it["portion"]}）', GREEN),
                    ("成交价", f'${fmt(it["price"])}', "#e6f1ff"),
                    ("剩余仓位", f'{pos.get("sizePct")}%' if pos.get("sizePct") else "—", BLUE),
                    ("新止损", f'${fmt(pos.get("stop"))}', GOLD)]
            cards.append(card(f'💰 {it["mode"]}止盈{it["stage"]} · {it["sym"]}（{it["note"]}）', GREEN, kv_table(rows)))
        elif k == "movestop":
            rows = [("已持仓", it["hold"], BLUE), ("新止损（成本价）", f'${fmt(it["stop"])}', GOLD),
                    ("含义", "此后最坏保本离场", GRAY)]
            cards.append(card(f'⏰ {it["mode"]}止损上移 · {it["sym"]}', GOLD, kv_table(rows)))
        else:
            cards.append(card("📋 引擎事件", BLUE,
                              f'<div style="font-size:13px;color:#e6f1ff;line-height:1.6;">{it["raw"]}</div>'))

    # —— 当前持仓总览 ——
    pos_html = ""
    for p in positions_overview(state):
        up = p["upnl"]
        up_s = f'{up:+.2f}%' if up is not None else "—"
        hold = hold_str(p.get("entryTime"), bj_now().strftime("%Y-%m-%d %H:%M:%S"))
        rows = [("方向", p["dir"], dir_color(p["dir"])),
                ("均价 → 现价", f'{fmt(p["entry"])} → {fmt(p["now"])}', "#e6f1ff"),
                ("浮盈", up_s, pct_color(up or 0)),
                ("仓位", f'{p["size"]}%' if p.get("size") else "—", BLUE),
                ("已持仓", hold, GRAY),
                ("止损", fmt(p["stop"]), RED),
                ("补仓位", "已补" if p.get("addDone") else fmt(p["add"]), GOLD),
                ("止盈1/2/3", f'{fmt(p["tp1"])}/{fmt(p["tp2"])}/{fmt(p["tp3"])}', GREEN)]
        pos_html += card(f'{MODE_ICON[p["mode"]]} {p["mode"]} · {p["sym"]} 永续',
                         dir_color(p["dir"]), kv_table(rows))
    if not pos_html:
        pos_html = f'<div style="color:{GRAY};font-size:13px;padding:6px 2px;">当前无持仓</div>'

    # —— 账本统计 ——
    st = ledger_stats(trades)
    stat_html = ""
    if st:
        stat_html = (f'<div style="text-align:center;font-size:13px;color:{GRAY};margin:10px 0 4px;">'
                     f'📒 账本：已平仓 <b style="color:#e6f1ff;">{st["n"]}</b> 笔 · '
                     f'胜率 <b style="color:{BLUE};">{st["win"]:.0f}%</b> · '
                     f'累计权益贡献 <b style="color:{pct_color(st["realized"])};">{st["realized"]:+.2f}%</b></div>')

    now_s = bj_now().strftime("%Y-%m-%d %H:%M")
    return f'''<!DOCTYPE html><html><body style="margin:0;padding:0;background:{BG};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:16px 8px;"><tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
  <tr><td style="padding:4px 6px 14px;">
    <div style="font-size:20px;font-weight:800;color:#e6f1ff;">🔔 云端引擎交易信号</div>
    <div style="font-size:12px;color:{GRAY};margin-top:4px;">{now_s}（北京时间）· 每 15 分钟巡航 · 与本地页面同一本账</div>
  </td></tr>
  <tr><td>{''.join(cards)}</td></tr>
  <tr><td style="padding:6px 6px 10px;"><div style="font-size:15px;font-weight:700;color:#e6f1ff;">📊 当前持仓</div></td></tr>
  <tr><td>{pos_html}</td></tr>
  <tr><td>{stat_html}
    <div style="text-align:center;font-size:11px;color:#42566b;margin-top:10px;">本邮件由云端引擎自动生成 · 数据源 币安/OKX · 本地页面打开后自动同步此账本</div>
  </td></tr>
</table></td></tr></table></body></html>'''

def make_subject(items):
    acts = []
    for it in items[:3]:
        if it["kind"] == "open": acts.append(f'{it["sym"]}{it["mode"]}开{it["dir"]}@{fmt(it["price"])}')
        elif it["kind"] == "close": acts.append(f'{it["sym"]}{it["reason"]}{it["pnl"]:+.2f}%' if it.get("pnl") is not None else f'{it["sym"]}{it["reason"]}')
        elif it["kind"] == "add": acts.append(f'{it["sym"]}补仓@{fmt(it["price"])}')
        elif it["kind"] == "tp": acts.append(f'{it["sym"]}止盈{it["stage"]}')
        elif it["kind"] == "movestop": acts.append(f'{it["sym"]}止损上移')
    more = f' 等{len(items)}条' if len(items) > 3 else ""
    return f'🔔 云端交易：{"｜".join(acts)}{more}' if acts else "🔔 云端引擎交易信号"

# ================= 主流程 =================
def main():
    preview = None
    if "--preview" in sys.argv:
        preview = sys.argv[sys.argv.index("--preview") + 1]
    if not os.path.exists("data/live_events_pending.txt"):
        print("无待发交易事件，跳过"); return 0
    lines = open("data/live_events_pending.txt", encoding="utf-8").read().splitlines()
    state = json.load(open("data/live_state.json", encoding="utf-8"))
    items = parse_events(lines)
    html_doc = render(items, state)
    subject = make_subject(items)
    if preview:
        open(preview, "w", encoding="utf-8").write(html_doc)
        print(f"预览已写入 {preview}（主题：{subject}）"); return 0

    user, pwd, to = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO", "")
    if not (user and pwd and to):
        print("MAIL_USER/MAIL_PASS/MAIL_TO 未配置完整", file=sys.stderr); return 1
    msg = MIMEText(html_doc, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"{Header('云端引擎', 'utf-8').encode()} <{user}>"
    recipients = [x.strip() for x in to.split(",") if x.strip()]
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())
    print(f"邮件已发送 → {recipients}（主题：{subject}）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
