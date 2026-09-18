# -*- coding: utf-8 -*-
"""云端引擎晨报：每天北京时间 08:05 发送隔夜摘要（HTML，与交易邮件同一模板）。
内容：当前信号点位 → 过去24h开平仓明细 → 当前持仓（均价/浮盈/止损/补仓/止盈）→ 账本统计。
用法：python morning_report.py [--preview out.html]
环境变量：MAIL_USER / MAIL_PASS / MAIL_TO
"""
import json, os, sys, smtplib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from send_mail import (BJ, GREEN, RED, BLUE, GRAY, GOLD, BG, CARD, BORDER,
                       fmt, pct_color, dir_color, kv_table, card, MODE_ICON,
                       positions_overview, ledger_stats, hold_str, bj_now)
from email.mime.text import MIMEText
from email.header import Header

def parse_t(s):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try: return datetime.strptime(str(s), f).replace(tzinfo=BJ)
        except (ValueError, TypeError): continue
    return None

def short_t(s):
    t = parse_t(s)
    return t.strftime("%m-%d %H:%M") if t else "—"

def sig_section(state):
    sigs = state.get("signals") or {}
    rows = ""
    for sym in ("BTC", "ETH"):
        s = sigs.get(sym) or {}
        if not s.get("price"): continue
        sw, intra = s.get("S"), s.get("intraSig")
        sw_c = GREEN if (sw or 0) > 0.9 else (RED if (sw or 0) < -0.9 else GRAY)
        in_c = GREEN if (intra or 0) > 0.9 else (RED if (intra or 0) < -0.9 else GRAY)
        sw_s = f'<td style="padding:6px 10px;color:{sw_c};font-weight:600;">{sw:+.2f}</td>' if sw is not None else f'<td style="padding:6px 10px;color:{GRAY};">—</td>'
        in_s = f'<td style="padding:6px 10px;color:{in_c};font-weight:600;">{intra:+.2f}</td>' if intra is not None else f'<td style="padding:6px 10px;color:{GRAY};">—</td>'
        rows += (f'<tr><td style="padding:6px 10px;color:#e6f1ff;font-weight:700;">{sym}</td>'
                 f'<td style="padding:6px 10px;color:#e6f1ff;">${fmt(s["price"])}</td>'
                 f'{sw_s}{in_s}'
                 f'<td style="padding:6px 10px;color:{GRAY};">{fmt(s.get("atr"))}</td></tr>')
    if not rows: return ""
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {BORDER};'
            f'border-radius:12px;margin-bottom:12px;font-size:13px;">'
            f'<tr style="color:{GRAY};font-size:11px;"><td style="padding:8px 10px 2px;">币种</td>'
            f'<td style="padding:8px 10px 2px;">现价</td><td style="padding:8px 10px 2px;">波段S</td>'
            f'<td style="padding:8px 10px 2px;">日内信号</td><td style="padding:8px 10px 2px;">ATR</td></tr>{rows}</table>')

def trades_24h(state, now):
    """过去24h内开仓或平仓的交易，按时间排序输出紧凑行。"""
    since = now - timedelta(hours=24)
    rows = []
    for t in state.get("trades", []):
        et, xt = parse_t(t.get("entryTime")), parse_t(t.get("exitTime"))
        opened = et and et >= since
        closed = xt and xt >= since
        if not (opened or closed): continue
        d = t.get("dir", "?")
        pnl = t.get("pnl")
        pnl_s = f'{pnl:+.2f}%' if pnl is not None else "持仓中"
        rows.append((et or xt, f'''
<tr style="font-size:12.5px;">
  <td style="padding:5px 8px;color:{GRAY};">{short_t(t.get("entryTime"))}</td>
  <td style="padding:5px 8px;color:{BLUE};">{t.get("mode","波段")}</td>
  <td style="padding:5px 8px;color:#e6f1ff;font-weight:600;">{t.get("sym")}</td>
  <td style="padding:5px 8px;color:{dir_color(d)};font-weight:600;">{d}</td>
  <td style="padding:5px 8px;color:{GRAY};">{t.get("sizePct")}%</td>
  <td style="padding:5px 8px;color:#e6f1ff;">{fmt(t.get("entry"))} → {fmt(t.get("exitPrice")) if t.get("exitPrice") else "…"}</td>
  <td style="padding:5px 8px;color:{pct_color(pnl) if pnl is not None else GOLD};font-weight:600;">{pnl_s}</td>
  <td style="padding:5px 8px;color:{GRAY};">{t.get("reason") or ""}</td>
</tr>'''))
    rows.sort(key=lambda x: x[0])
    body = "".join(r for _, r in rows)
    if not body:
        return '<div style="color:' + GRAY + ';font-size:13px;padding:4px 2px 10px;">过去 24 小时无新开/平仓</div>', 0, 0
    n_open = sum(1 for t in state.get("trades", []) if (parse_t(t.get("entryTime")) or datetime.min.replace(tzinfo=BJ)) >= since)
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {BORDER};'
            f'border-radius:12px;margin-bottom:12px;">'
            f'<tr style="color:{GRAY};font-size:11px;"><td style="padding:8px 8px 2px;">时间</td><td style="padding:8px 8px 2px;">模式</td>'
            f'<td style="padding:8px 8px 2px;">币种</td><td style="padding:8px 8px 2px;">方向</td><td style="padding:8px 8px 2px;">仓位</td>'
            f'<td style="padding:8px 8px 2px;">开仓→平仓</td><td style="padding:8px 8px 2px;">盈亏</td><td style="padding:8px 8px 2px;">原因</td></tr>'
            f'{body}</table>'), len(rows), n_open

def render_morning(state):
    now = bj_now()
    trades_html, n_rows, n_open = trades_24h(state, now)

    pos_html = ""
    up_contrib = 0.0   # 仓位加权浮盈（权益贡献口径，与账本 realized 一致）
    for p in positions_overview(state):
        up = p["upnl"]
        if up is not None and p.get("size"):
            up_contrib += float(p["size"]) * up / 100
        hold = hold_str(p.get("entryTime"), now.strftime("%Y-%m-%d %H:%M:%S"))
        rows = [("方向", p["dir"], dir_color(p["dir"])),
                ("均价 → 现价", f'{fmt(p["entry"])} → {fmt(p["now"])}', "#e6f1ff"),
                ("浮盈", f'{up:+.2f}%' if up is not None else "—", pct_color(up or 0)),
                ("仓位", f'{p["size"]}%' if p.get("size") else "—", BLUE),
                ("已持仓", hold, GRAY),
                ("止损", fmt(p["stop"]), RED),
                ("补仓位", "已补" if p.get("addDone") else fmt(p["add"]), GOLD),
                ("止盈1/2/3", f'{fmt(p["tp1"])}/{fmt(p["tp2"])}/{fmt(p["tp3"])}', GREEN)]
        pos_html += card(f'{MODE_ICON[p["mode"]]} {p["mode"]} · {p["sym"]} 永续',
                         dir_color(p["dir"]), kv_table(rows))
    if not pos_html:
        pos_html = f'<div style="color:{GRAY};font-size:13px;padding:6px 2px;">当前无持仓</div>'

    st = ledger_stats(state.get("trades", []))
    stat_html = ""
    if st:
        stat_html = (f'<div style="text-align:center;font-size:13px;color:{GRAY};margin:10px 0 4px;">'
                     f'📒 账本：已平仓 <b style="color:#e6f1ff;">{st["n"]}</b> 笔 · '
                     f'胜率 <b style="color:{BLUE};">{st["win"]:.0f}%</b> · '
                     f'累计权益贡献 <b style="color:{pct_color(st["realized"])};">{st["realized"]:+.2f}%</b></div>')

    n_pos = len(positions_overview(state))
    subj = (f'🌅 云端晨报 {now.strftime("%m-%d")}：24h交易{n_rows}笔 · '
            f'持仓{n_pos}个 · 浮盈{up_contrib:+.2f}%')
    html_doc = f'''<!DOCTYPE html><html><body style="margin:0;padding:0;background:{BG};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:16px 8px;"><tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
  <tr><td style="padding:4px 6px 14px;">
    <div style="font-size:20px;font-weight:800;color:#e6f1ff;">🌅 云端引擎晨报</div>
    <div style="font-size:12px;color:{GRAY};margin-top:4px;">{now.strftime("%Y-%m-%d %H:%M")}（北京时间）· 过去 24 小时摘要 · 与本地页面同一本账</div>
  </td></tr>
  <tr><td style="padding:2px 6px 8px;"><div style="font-size:15px;font-weight:700;color:#e6f1ff;">📈 当前信号</div></td></tr>
  <tr><td>{sig_section(state)}</td></tr>
  <tr><td style="padding:6px 6px 8px;"><div style="font-size:15px;font-weight:700;color:#e6f1ff;">🌙 过去 24 小时交易</div></td></tr>
  <tr><td>{trades_html}</td></tr>
  <tr><td style="padding:6px 6px 8px;"><div style="font-size:15px;font-weight:700;color:#e6f1ff;">📊 当前持仓</div></td></tr>
  <tr><td>{pos_html}</td></tr>
  <tr><td>{stat_html}
    <div style="text-align:center;font-size:11px;color:#42566b;margin-top:10px;">本邮件由云端引擎每日 08:05（北京时间）自动生成 · 数据源 币安/OKX</div>
  </td></tr>
</table></td></tr></table></body></html>'''
    return subj, html_doc

def main():
    preview = None
    if "--preview" in sys.argv:
        preview = sys.argv[sys.argv.index("--preview") + 1]
    state = json.load(open("data/live_state.json", encoding="utf-8"))
    subject, html_doc = render_morning(state)
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
    print(f"晨报已发送 → {recipients}（主题：{subject}）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
