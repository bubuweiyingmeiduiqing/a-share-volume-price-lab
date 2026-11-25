# -*- coding: utf-8 -*-
"""
收盘看板网页：把涨跌、资金、宽度这些“数字表格”变成手机上能看的图。

产物（静态文件，nginx 直接出）：
  <site_dir>/index.html          最新一天
  <site_dir>/d/<YYYYMMDD>.html   每天归档（推送里给的链接就是这个，回看不会变）
  <site_dir>/weekly.html         最近 5 个交易日的复盘底稿

数据落盘：state/board.db（SQLite，可查询）+ state/board/<date>.json（原始快照）。
不引第三方库、不用 CDN：图表都是内联 SVG / CSS，微信内置浏览器直接打开。

页面宽度按手机竖屏（≈390px）设计，max-width 440px，桌面打开就是一条窄卡片。
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

import data as dm
import store

UP = "#d93a3a"       # 涨：红
DN = "#0f9d58"       # 跌：绿
MUTED = "#8b8f97"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _num(v, digits: int = 2, dash: str = "—") -> str:
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


def _signed(v, digits: int = 2, suffix: str = "") -> str:
    try:
        return f"{float(v):+,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _pct(v, digits: int = 2) -> str:
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _tone(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "na"
    return "up" if x > 0 else ("dn" if x < 0 else "na")


def svg_spark(values: list[float], width: int = 360, height: int = 64,
              color: str = "#2f6fed", area: bool = True) -> str:
    """等宽折线（无坐标轴），数据不足 2 个点时给一条平线占位。"""
    pts = [float(v) for v in values if v is not None]
    if not pts:
        return f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none"></svg>'
    if len(pts) == 1:
        pts = pts * 2
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    pad = 6
    step = (width - 2 * pad) / (len(pts) - 1)
    xy = [(pad + i * step, height - pad - (v - lo) / span * (height - 2 * pad))
          for i, v in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    if area:
        fill = (f'<polygon points="{xy[0][0]:.1f},{height} ' + line
                + f' {xy[-1][0]:.1f},{height}" fill="{color}" opacity="0.12"/>')
    else:
        fill = ""
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
            f'{fill}<polyline points="{line}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def css_bars(items: list[dict], width_pct: float = 100.0) -> str:
    """
    横向条形（CSS 宽度），支持正负分叉：item = {label, value, text, tone, sub}
    零值居中，正值向右红、负值向左绿。
    """
    if not items:
        return '<p class="hint">暂无数据</p>'
    mx = max((abs(float(i.get("value") or 0)) for i in items), default=0.0) or 1.0
    out = ['<div class="bars">']
    for it in items:
        v = float(it.get("value") or 0)
        pct = min(100.0, abs(v) / mx * width_pct)
        tone = it.get("tone") or _tone(v)
        side = "pos" if v >= 0 else "neg"
        out.append(
            f'<div class="bar-row"><div class="bar-label">{_esc(it.get("label"))}'
            f'<span class="bar-sub">{_esc(it.get("sub") or "")}</span></div>'
            f'<div class="bar-track"><div class="bar-fill {side} {tone}" '
            f'style="width:{pct:.1f}%"></div></div>'
            f'<div class="bar-val {tone}">{_esc(it.get("text") or "")}</div></div>')
    out.append("</div>")
    return "".join(out)


STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c1f23;--muted:%(muted)s;--line:#eceef2;
--up:%(up)s;--dn:%(dn)s}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;
-webkit-text-size-adjust:100%%}
.wrap{max-width:440px;margin:0 auto;padding:12px 12px 28px}
.card{background:var(--card);border-radius:14px;padding:14px;margin:10px 0;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
h1{font-size:19px;margin:2px 0 6px}
h2{font-size:15px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:baseline}
h2 .h2r{font-size:12px;color:var(--muted);font-weight:400}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.chip{font-size:12px;padding:3px 9px;border-radius:999px;background:#eef2ff;color:#3452c8}
.chip.warn{background:#fff3e6;color:#a35200}
.chip.bad{background:#ffeceb;color:#b42318}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.kpi{background:#fbfbfd;border-radius:10px;padding:9px 10px}
.kpi .k{font-size:12px;color:var(--muted)}
.kpi .v{font-size:18px;font-weight:600;letter-spacing:-.2px}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 2px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:none}
.up{color:var(--up)}.dn{color:var(--dn)}.na{color:var(--muted)}
.spark{width:100%%;height:64px;display:block}
.spark.sm{height:44px}
.hint{color:var(--muted);font-size:13px;margin:6px 0 0}
.bars{margin-top:2px}
.bar-row{display:grid;grid-template-columns:96px 1fr 74px;gap:6px;align-items:center;
padding:3px 0;font-size:13px}
.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-sub{display:block;font-size:11px;color:var(--muted)}
.bar-track{background:#f0f1f5;border-radius:4px;height:12px;position:relative;overflow:hidden}
.bar-fill{height:100%%;border-radius:4px}
.bar-fill.up{background:var(--up)}.bar-fill.dn{background:var(--dn)}
.bar-fill.pos{float:left}.bar-fill.neg{float:right}
.bar-val{text-align:right;font-variant-numeric:tabular-nums;font-size:12px}
.foot{color:var(--muted);font-size:11px;line-height:1.6;margin-top:14px}
.tabs{display:flex;gap:8px;margin:6px 0 0}
.tabs a{font-size:12px;color:#3452c8;text-decoration:none;padding:4px 10px;
border:1px solid #dbe1f7;border-radius:999px}
.stat{display:flex;justify-content:space-between;font-size:13px;padding:4px 0;
border-bottom:1px dashed var(--line)}
.stat:last-child{border-bottom:none}
"""


def _page(title: str, body: str) -> str:
    style = STYLE % {"up": UP, "dn": DN, "muted": MUTED}
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="format-detection" content="telephone=no">
<title>{_esc(title)}</title>
<style>{style}</style>
</head><body><div class="wrap">{body}</div></body></html>
"""


# ---------------------------------------------------------------------------
# 各区块
# ---------------------------------------------------------------------------

def _header(data: dict, account: dict | None) -> str:
    rg = data.get("regime") or {}
    state = rg.get("cn") or rg.get("state") or "—"
    tone = "bad" if rg.get("state") == "CRASH" else ("warn" if rg.get("state") == "COLD" else "")
    eq = (account or {}).get("equity")
    day = (account or {}).get("day_ret")
    chips = [f'<span class="chip {tone}">{_esc(state)}</span>']
    if rg.get("cap") is not None:
        chips.append(f'<span class="chip">仓位上限 {float(rg["cap"]):.0%}</span>')
    if rg.get("dwell"):
        chips.append(f'<span class="chip">第 {rg["dwell"]} 天</span>')
    eq_html = ""
    if eq:
        eq_html = (f'<div class="kpis" style="margin-top:10px">'
                   f'<div class="kpi"><div class="k">净值</div>'
                   f'<div class="v">{_num(eq, 0)}</div></div>'
                   f'<div class="kpi"><div class="k">当日</div>'
                   f'<div class="v {_tone(day * 100 if day is not None else None)}">'
                   f'{_pct(day * 100 if day is not None else None)}</div></div></div>')
    return (f'<div class="card"><h1>常规量价模拟盘 · {dm.dash(data["date"])}</h1>'
            f'<div class="chips">{"".join(chips)}</div>{eq_html}</div>')


def _equity_card(days: list[dict]) -> str:
    eq = [d["equity"] for d in days if d.get("equity")]
    if len(eq) < 2:
        return ""
    ret = eq[-1] / eq[0] - 1
    return (f'<div class="card"><h2>净值曲线<span class="h2r">近 {len(eq)} 个交易日 '
            f'<span class="{_tone(ret * 100)}">{_pct(ret * 100)}</span></span></h2>'
            f'{svg_spark(eq, color="#2f6fed")}</div>')


def _market_card(data: dict, days: list[dict]) -> str:
    mk = data.get("market") or {}
    if not mk:
        return ""
    kpis = [
        ("沪深成交额", f'{_num(mk.get("amount_yi"), 0)} 亿', ""),
        ("涨跌家数", f'{mk.get("up") or 0} : {mk.get("down") or 0}', ""),
        ("涨停 / 跌停", f'{mk.get("zt") or 0} / {mk.get("dt") or 0}', ""),
        ("池内 5 日线宽度", f'{_num(mk.get("pool_above5"), 1)}%',
         f'n={mk.get("pool_n") or 0}'),
    ]
    cards = "".join(
        f'<div class="kpi"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>'
        f'<div class="k">{_esc(sub)}</div></div>' for k, v, sub in kpis)
    width = [d.get("pool_above5") for d in days if d.get("pool_above5") is not None]
    trend = ""
    if len(width) >= 2:
        trend = ('<h2 style="margin-top:14px">5 日线宽度<span class="h2r">'
                 f'近 {len(width)} 个交易日</span></h2>' + svg_spark(width, color="#7a5af8"))
    scope = "" if (mk.get("n") or 0) >= 4000 else f'（截断快照 {mk.get("n")} 只）'
    return (f'<div class="card"><h2>全市场<span class="h2r">{_esc(mk.get("limit_src") or "")}'
            f'{scope}</span></h2><div class="kpis">{cards}</div>{trend}</div>')


def _indices_card(data: dict) -> str:
    rows = data.get("indices") or []
    if not rows:
        return ""
    tr = "".join(
        f'<tr><td>{_esc(r["name"])}</td><td>{_num(r["close"])}</td>'
        f'<td class="{_tone(r["ret5"])}">{_pct(r["ret5"])}</td>'
        f'<td class="{_tone(r["ret20"])}">{_pct(r["ret20"])}</td>'
        f'<td>{"上方" if r["above"] else "下方"}</td></tr>' for r in rows)
    bars = css_bars([{"label": r["name"], "value": r["ext"],
                      "text": _pct(r["ext"]), "tone": _tone(r["ext"])}
                     for r in rows])
    return (f'<div class="card"><h2>大盘环境<span class="h2r">距 MA20</span></h2>'
            f'<table><thead><tr><th>指数</th><th>收盘</th><th>5日</th><th>20日</th>'
            f'<th>MA20</th></tr></thead><tbody>{tr}</tbody></table>'
            f'<div style="margin-top:10px">{bars}</div></div>')


def _sector_card(data: dict) -> str:
    stats = data.get("sector_stats") or {}
    rows = data.get("sectors") or []
    if not rows:
        note = stats.get("note") or "今日没有板块通过早期修复条件"
        return (f'<div class="card"><h2>板块资金<span class="h2r">'
                f'{_esc(stats.get("n") or 0)} 个概念板块</span></h2>'
                f'<p class="hint">{_esc(note)}</p></div>')
    items = [{"label": r["name"], "value": r["f1"],
              "text": _signed(r["f1"], 1, "亿"), "tone": _tone(r["f1"]),
              "sub": f'5日 {_signed(r["f5"], 1)} 亿 · {r["tag"]}'}
             for r in rows[:10]]
    return (f'<div class="card"><h2>板块资金<span class="h2r">今日净流入 · '
            f'早期修复 {stats.get("early", 0)} 个</span></h2>{css_bars(items)}'
            f'<p class="hint">归一化资金 = 5日净额 ÷ 板块公司家数；标签由资金与涨跌组合判定。</p></div>')


def _watch_card(data: dict) -> str:
    rows = data.get("watch") or []
    if not rows:
        return ""
    items = [{"label": r["name"], "value": r["f1"],
              "text": _signed(r["f1"], 1, "亿"), "tone": _tone(r["f1"]),
              "sub": f'5日 {_signed(r["f5"], 1)} 亿 · {_pct(r.get("chg"))}'}
             for r in rows[:8]]
    return f'<div class="card"><h2>新旧力量对比<span class="h2r">今日净流入</span></h2>{css_bars(items)}</div>'


def _leader_card(data: dict) -> str:
    rows = data.get("leaders") or []
    if not rows:
        return ""
    items = [{"label": f'{r["name"]}',
              "value": r["ratio"], "text": _signed(r["ratio"], 2, "%"),
              "tone": _tone(r["ratio"]),
              "sub": f'5日 {_signed(r["f5"], 1)}亿 · 涨 {_pct(r["chg5"])}'}
             for r in rows[:8]]
    return (f'<div class="card"><h2>核心龙头<span class="h2r">5 日资金 / 流通市值</span></h2>'
            f'{css_bars(items)}<p class="hint">研究候选，不是成交信号；只有「明日计划」里的候选才成交。</p></div>')


def _plan_card(data: dict, plan: dict | None) -> str:
    plan = plan or {}
    cands = plan.get("candidates") or []
    state = data.get("regime") or {}
    head = (f'<div class="stat"><span>明日（{dm.dash(plan.get("for_date") or "")}）状态</span>'
            f'<b>{_esc(plan.get("state") or state.get("cn") or "—")}</b></div>')
    if plan.get("allow_new") is False:
        body = '<div class="stat"><span>开新仓</span><b>否</b></div>'
    if not cands:
        body = (body if plan.get("allow_new") is False else "") + \
            '<div class="stat"><span>候选</span><b>无</b></div>'
    else:
        body = "".join(
            f'<div class="stat"><span>{_esc(c.get("code"))} {_esc(c.get("name"))}</span>'
            f'<b>{_pct((c.get("gap_min") or 0) * 100)} ~ {_pct((c.get("gap_max") or 0) * 100)}</b></div>'
            for c in cands[:6])
    return f'<div class="card"><h2>明日计划</h2>{head}{body}</div>'


def _trades_card(data: dict) -> str:
    trades = data.get("trades") or []
    pos = data.get("positions") or []
    if not trades and not pos:
        return ('<div class="card"><h2>今日成交</h2>'
                '<p class="hint">无成交，空仓。</p></div>')
    rows = "".join(
        f'<div class="stat"><span>{_esc(t.get("action"))} {_esc(t.get("name"))}</span>'
        f'<b class="{_tone((t.get("ret") or 0) * 100)}">{_pct((t.get("ret") or 0) * 100)}</b></div>'
        for t in trades[:8])
    holds = "".join(
        f'<div class="stat"><span>{_esc(p.get("name"))} 持 {p.get("hold_days") or 0} 天</span>'
        f'<b class="{_tone((p.get("ret") or 0) * 100)}">{_pct((p.get("ret") or 0) * 100)}</b></div>'
        for p in pos[:8])
    return (f'<div class="card"><h2>今日成交 / 持仓</h2>{rows}{holds}</div>')


def render_html(data: dict, days: list[dict], plan: dict | None,
                account: dict | None) -> str:
    body = "".join([
        _header(data, account),
        _equity_card(days),
        _market_card(data, days),
        _indices_card(data),
        _sector_card(data),
        _watch_card(data),
        _leader_card(data),
        _trades_card(data),
        _plan_card(data, plan),
        f'<div class="foot">数据源：akshare（指数/快照）+ 同花顺（板块/个股资金）+ 东财（涨跌停池）。'
        f'生成于 {_esc(data.get("generated_at") or "")}，仅供参考，不构成投资建议。<br>'
        f'<div class="tabs"><a href="./">最新</a><a href="../weekly.html">周复盘底稿</a></div></div>',
    ])
    return _page(f'常规量价模拟盘 {dm.dash(data["date"])}', body)


def render_weekly(db: Path, end: str, params=None) -> str:
    conn = store.connect(db)
    try:
        w = store.weekly(conn, end, int(params.rt("board", "weekly_days", default=5)) if params else 5)
    finally:
        conn.close()
    if not w:
        body = '<div class="card"><h1>周复盘</h1><p class="hint">还没有足够的数据。</p></div>'
    else:
        tops = css_bars([{"label": s["name"], "value": s["f5"],
                          "text": _signed(s["f5"], 1, "亿"),
                          "sub": f'上榜 {s["appear"]} 次'} for s in w["top_sectors"]])
        bots = css_bars([{"label": s["name"], "value": s["f5"],
                          "text": _signed(s["f5"], 1, "亿"),
                          "sub": f'上榜 {s["appear"]} 次'} for s in w["bottom_sectors"]])
        body = "".join([
            f'<div class="card"><h1>周复盘 · {dm.dash(w["start"])} ~ {dm.dash(w["end"])}</h1>'
            f'<div class="stat"><span>交易日</span><b>{w["days"]}</b></div>'
            f'<div class="stat"><span>净值</span><b>{_num(w["equity_start"], 0)} → '
            f'{_num(w["equity_end"], 0)}（<span class="{_tone((w["ret"] or 0) * 100)}">'
            f'{_pct((w["ret"] or 0) * 100)}</span>）</b></div>'
            f'<div class="stat"><span>龙头候选</span><b>{w["leader_rows"]} 行 / 计划内 '
            f'{w["leader_planned"]}</b></div></div>',
            f'<div class="card"><h2>资金流入前列<span class="h2r">按 5 日净额累计</span></h2>{tops}</div>',
            f'<div class="card"><h2>资金流出前列</h2>{bots}</div>',
            '<div class="foot">口径：板块 5 日净额按日累加；净值取 account.json 的 equity_history。</div>',
        ])
    return _page("周复盘底稿", body)


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------

def site_dir(params) -> Path | None:
    raw = str(params.rt("board", "site_dir", default="") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def publish(data: dict, account: dict | None, plan: dict | None, params,
            db_path: Path | None = None) -> str | None:
    """落库 + 写 HTML，返回当天看板 URL（未配置站点返回 None）。"""
    if not data or not data.get("date"):
        return None
    db = Path(db_path) if db_path else Path(
        str(params.rt("board", "store_db", default="state/board.db")))
    if not db.is_absolute():
        db = Path(__file__).resolve().parent / db

    json_dir = Path(__file__).resolve().parent / "state" / "board" \
        if db.parent.name == "state" else db.parent / "board"
    store.dump_json(data, json_dir / f"{data['date']}.json")

    conn = store.connect(db)
    try:
        n = store.seed_equity(conn, (account or {}).get("equity_history") or [])
        if n:
            print(f"[info] 看板补录净值 {n} 天")
        store.save_day(conn, data, account, plan)
        days = store.recent_days(conn, data["date"],
                                 int(params.rt("board", "history_days", default=20)))
    finally:
        conn.close()

    out = site_dir(params)
    if out is None:
        return None
    if not out.parent.exists():
        print(f"[warn] 看板站点目录不存在，跳过写网页：{out}")
        return None
    try:
        (out / "d").mkdir(parents=True, exist_ok=True)
        page = render_html(data, days, plan, account)
        (out / "index.html").write_text(page, encoding="utf-8")
        (out / "d" / f"{data['date']}.html").write_text(page, encoding="utf-8")
        (out / "weekly.html").write_text(render_weekly(db, data["date"], params),
                                         encoding="utf-8")
    except OSError as e:
        print(f"[warn] 看板网页写入失败（{e}），数据已入库：{db}")
        return None

    base = str(params.rt("board", "base_url", default="") or "").rstrip("/")
    if not base:
        return None
    return f"{base}/d/{data['date']}.html"


def main(argv: list[str] | None = None) -> int:
    """单独发布某一天的看板（不动交易 state）：python3 webboard.py --date 20261125"""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-fetch", action="store_true", help="不拉行情，只重渲染已有数据")
    a = ap.parse_args(argv)

    import board as bd
    import portfolio as pf
    import regime as rg
    from config import load_params

    params = load_params()
    date = dm.norm_date(a.date) if a.date else dm.latest_trade_date(None)

    if a.no_fetch:
        js = Path(__file__).resolve().parent / "state" / "board" / f"{date}.json"
        import json
        data = json.loads(js.read_text(encoding="utf-8"))
        acct = pf.load(params)
        plan = None
        url = publish(data, acct, plan, params)
        print(data and (url or "已渲染（未配置看板 URL）"))
        return 0

    market = dm.load_market(date, params)
    reg = rg.load(Path(__file__).resolve().parent / "state" / "regime.json")
    res = rg.evaluate(market, params, reg.get("state"), int(reg.get("dwell") or 0))
    acct = pf.load(params)
    import json
    plan_p = Path(__file__).resolve().parent / "state" / "plan.json"
    plan = json.loads(plan_p.read_text(encoding="utf-8")) if plan_p.exists() else None
    data = bd.collect(market, date, params, res, plan)
    data["regime"] = {"state": res.state, "cn": res.state_cn, "heat": res.heat,
                      "cap": res.cap, "allow_new": res.allow_new, "dwell": res.dwell}
    data["positions"] = [
        {"code": p["code"], "name": p["name"], "setup": p.get("setup"),
         "entry_price": p["entry_price"], "last_close": p.get("last_close"),
         "ret": (p.get("last_close", p["entry_price"]) / p["entry_price"] - 1),
         "stop": p.get("stop"), "hold_days": pf._hold_days(p["entry_date"], date)}
        for p in acct["positions"]]
    url = publish(data, acct, plan, params)
    print(f"看板：{url}" if url else "数据已保存（未配置看板 URL）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
