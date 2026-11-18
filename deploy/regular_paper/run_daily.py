#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常规量价模拟盘 · 每日收盘主流程

时序（全部日线级）：
  1. 读取账户 / 市场状态 / 昨日计划
  2. 用昨日计划，在今日开盘执行条件单（一字/涨停开盘、gap 越界都不成交）
  3. 用今日收盘数据更新市场状态
  4. 结算持仓：硬止损 → 放量急跌 → 无量洗盘 → 趋势走坏 → 人声鼎沸/连续大涨
     → 冲高回落 → 移动止盈 → 时间止损
  5. 收盘盯市、熔断检查
  6. 生成明日计划（条件单：开盘 gap 区间 + 止损价 + 目标仓位）
  7. 写 Markdown 日报 + PushPlus 推送

用法：
  python3 run_daily.py                       # 跑最近交易日 + 推送
  python3 run_daily.py --no-push             # 不推送
  python3 run_daily.py --date 20261106       # 指定日
  python3 run_daily.py --profile defensive   # 风险档
  python3 run_daily.py --demo                # 合成数据演示，不联网、不推送
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

import data as dm
import indicators as ind
import portfolio as pf
import push as pusher
import regime as rg
import review as rev
import strategy as stg
from config import (
    DEMO_DIR, REPORT_DIR, STATE_DIR, SETUP_CN, eng_label, load_params,
)


def _next_date(date: str) -> str | None:
    try:
        return dm.next_trade_date(date)
    except Exception:
        try:
            nxt = pd.bdate_range(pd.to_datetime(date), periods=2)[1]
            return nxt.strftime("%Y%m%d")
        except Exception:
            return None


def load_plan(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_plan(plan: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 执行昨日计划
# ---------------------------------------------------------------------------

def execute_plan(acct: dict, plan: dict, date: str, market: dm.MarketData,
                 params) -> list[dict]:
    out: list[dict] = []
    if not plan or plan.get("for_date") != date:
        return out
    if not plan.get("allow_new", False):
        out.append({"action": "整体跳过", "reason": f"计划状态 {plan.get('state')} 不允许开新仓"})
        return out

    allow, cap_mult, why = pf.circuit(acct, date, params)
    if not allow:
        out.append({"action": "整体跳过", "reason": why})
        return out

    held = {p["code"] for p in acct["positions"]}
    cap = float(plan.get("cap", 0.0))

    for c in plan.get("candidates", []):
        if len(acct["positions"]) >= int(params.get("portfolio.slots")):
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": "槽位已满"})
            continue
        if plan.get("state") == "COLD":
            cold_cap = int(params.get("portfolio.cold_max_positions"))
            if len(acct["positions"]) >= cold_cap:
                out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                            "reason": f"冰点深坑例外最多 {cold_cap} 只"})
                continue
        if c["code"] in held:
            continue
        if c.get("industry") and c["industry"] != "未分类":
            same = sum(1 for p in acct["positions"] if p.get("industry") == c["industry"])
            if same >= int(params.get("portfolio.theme_cap")):
                out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                            "reason": f"同行业已持 {same} 只"})
                continue

        bar = market.bar(c["code"])
        if bar is None:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": "今日无行情（停牌？）"})
            continue

        prev_close = market.prev_close(c["code"])
        if prev_close <= 0:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": "无昨收"})
            continue

        ratio = ind.limit_ratio(c["code"], c["name"])
        lp = ind.limit_up_price(prev_close, ratio)
        if float(bar["open"]) >= lp - 0.011:
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": "一字/涨停开盘，买不进"})
            continue

        gap = float(bar["open"]) / prev_close - 1
        if gap < float(c["gap_min"]):
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": f"开盘 {gap:+.2%} < {c['gap_min']:+.1%}，低开过度"})
            continue
        if gap > float(c["gap_max"]):
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": f"开盘 {gap:+.2%} > {c['gap_max']:+.1%}，不追高"})
            continue
        if gap > float(c.get("max_chase", params.get("entry.max_chase"))):
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": f"开盘 {gap:+.2%} > 硬性追高线"})
            continue
        if float(bar["open"]) <= float(c["stop"]):
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": "开盘已跌破计划止损"})
            continue

        r = pf.buy(acct, c, float(bar["open"]), date, cap, cap_mult, params,
                       entry_regime=plan.get("state"))
        if r.get("ok"):
            pos = r["pos"]
            out.append({"action": "买入", "code": c["code"], "name": c["name"],
                        "setup": c["setup"], "price": round(pos["entry_price"], 3),
                        "shares": pos["shares"], "amount": round(pos["cost_amount"], 2),
                        "gap": round(gap, 4),
                        "reason": f"{eng_label(c['setup'])} 开盘 {gap:+.1%}"})
        else:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": r.get("why", "")})
    return out


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------

def _fmt_money(x: float) -> str:
    return f"{x:,.0f}"


def _state_metrics(res) -> list[str]:
    m = res.metrics
    return [
        f"指数 {m.get('close', 0):,.2f}（{m.get('day_pct', 0):+.2%}）"
        f" | 5日 {m.get('ret5', 0):+.2%} | 20日 {m.get('ret20', 0):+.2%}"
        f" | 量比 {m.get('amount_ratio', 0):.2f}",
        f"宽度：MA20 上方 {m.get('above_ma20', 0):.0%}"
        f" | 上涨家数 {m.get('up_ratio', 0):.0%}"
        f" | 中位涨幅 {m.get('median_pct', 0):+.2f}%"
        f" | 距 MA20 {m.get('extension', 0):+.1%}",
    ]


def _rule_section(settle_ev: list[dict], plan: dict) -> list[str]:
    L = []
    ev = [e for e in settle_ev if e.get("action") in ("卖出", "减半")]
    by_reason = {}
    for e in ev:
        by_reason.setdefault(e.get("reason", ""), []).append(e)

    def add(label, keys):
        hits = [e for e in ev if any(k in e.get("reason", "") for k in keys)]
        if hits:
            L.append(f"- {label}：{len(hits)} 笔（" + "、".join(
                f"{e['name']} {e.get('ret', 0):+.1%}" for e in hits[:3]) + "）")

    add("急跌放量立马撤", ["放量急跌", "市场急跌"])
    wash = [e for e in settle_ev if e.get("action") == "洗盘持有"]
    if wash:
        L.append(f"- 急跌无量是洗盘：{len(wash)} 只未卖（" +
                 "、".join(e["name"] for e in wash[:4]) + "）")
    add("卖在人声鼎沸处", ["人声鼎沸", "连续大涨"])
    add("快速冲高要回落", ["冲高回落"])
    add("移动止盈 / 时间止损", ["移动止盈", "破MA10", "时间止损"])

    setups = {}
    for c in plan.get("candidates", []):
        setups[c["setup"]] = setups.get(c["setup"], 0) + 1
    if setups:
        L.append("- 明日形态：" + "、".join(
            f"{SETUP_CN.get(k, k)} ×{v}" for k, v in setups.items()))
    if not L:
        L.append("- 今日无口诀触发；没有信号也是一种信号。")
    return L


def _equity_gap(acct: dict, market: dm.MarketData, n: int = 10) -> tuple[list[str], list[str]]:
    """最近 n 个指数交易日的净值记录完整性。只从账户第一天开始比较，避免首次运行误报。"""
    if market is None or market.index is None or market.index.empty:
        return [], []
    dates = [str(x) for x in market.index["date"].tolist()][-n:]
    have = {str(h.get("date")) for h in acct.get("equity_history", [])}
    if have:
        first = min(have)
        dates = [d for d in dates if d >= first]
    return dates, [d for d in dates if d not in have]


def _review_section(date: str, market: dm.MarketData, params, res, acct: dict,
                    plan: dict, fills: list[dict], prev_plan: dict | None,
                    m: dict, state_dir: Path | None,
                    report_dir: Path | None) -> list[str]:
    """每日复盘：计划执行 → 今日盈亏 → 运行体检 → 明日关注。

    这是“每天”的复盘；参数级复盘仍由 review.py 每 5 次收盘推送做一次。
    """
    L: list[str] = []
    prev_ok = bool(prev_plan and prev_plan.get("for_date") == date)
    prev_cands = prev_plan.get("candidates", []) if prev_ok else []
    buys = [x for x in fills if x.get("action") == "买入"]
    skips = [x for x in fills if x.get("action") in ("未成交", "跳过", "整体跳过")]
    plan_problem: str | None = None

    if prev_ok:
        line = f"- 计划执行：昨日计划 {len(prev_cands)} 只候选 → 买入 {len(buys)} 只"
        if skips:
            reasons: dict[str, int] = {}
            for x in skips:
                key = str(x.get("reason", "")).split("（")[0][:18] or "未成交"
                reasons[key] = reasons.get(key, 0) + 1
            top = "、".join(f"{k}×{v}" for k, v in list(reasons.items())[:3])
            line += f"；未成交/跳过 {len(skips)} 条（{top}）"
        L.append(line)
    elif prev_plan:
        L.append(f"- 计划执行：昨日计划目标日 {prev_plan.get('for_date')} 与今日不符，未执行")
        plan_problem = f"昨日计划未更新（目标日 {prev_plan.get('for_date')}）"
    else:
        L.append("- 计划执行：无昨日计划（首次运行或计划缺失）")
        if acct.get("equity_history"):
            plan_problem = "无昨日计划（plan.json 缺失）"

    parts: list[str] = []
    for p in acct.get("positions", []):
        bar = market.bar(p["code"]) if market is not None else None
        prev_close = market.prev_close(p["code"]) if market is not None else 0.0
        if bar is not None and prev_close > 0:
            day = float(bar["close"]) / prev_close - 1
            parts.append(f"{p['name']} {day:+.1%}")
    L.append("- 持仓今日：" + ("、".join(parts) if parts else "空仓"))

    eq = m.get("equity", 0.0)
    if eq:
        L.append(f"- 账户：今日 {m['day_ret']:+.2%}｜仓位 "
                 f"{(m['mv'] / eq):.0%}｜未触发风险 {m['open_risk'] / eq:.1%}")
    else:
        L.append("- 账户：—")

    # 运行体检：推送是否齐全、指数日期、净值记录是否连续
    checks: list[str] = []
    problems: list[str] = [plan_problem] if plan_problem else []
    c: dict = {}
    if state_dir is not None:
        try:
            c = rev.load_counter(state_dir)
        except Exception:
            c = {}

    pre_file = bool(report_dir) and (
        Path(report_dir) / f"{dm.dash(date)}-premarket.md").exists()
    # 以“推送成功计数”为准；文件存在只说明脚本跑过，不代表推送成功。
    pre_ok = c.get("last_premarket_date") == date
    if market is not None and market.source == "synthetic":
        pre_ok = True  # 演示数据没有真实调度，体检不参与判断
    if pre_ok:
        checks.append("盘前 ✅")
    elif pre_file:
        checks.append("盘前 ⚠️ 有报告但未计数")
        problems.append("盘前推送未计数（push 可能失败）")
    else:
        checks.append("盘前 ⚠️ 缺")
        problems.append("盘前推送缺失")
    checks.append("收盘 ✅（本条）")

    idx_last = str(market.index["date"].iloc[-1]) \
        if market is not None and len(market.index) else ""
    idx_ok = idx_last == date
    checks.append(f"指数 {dm.dash(idx_last) if idx_last else '—'} "
                  f"{'✅' if idx_ok else '⚠️'}")
    if not idx_ok:
        problems.append(f"指数未更新到 {dm.dash(date)}")

    dates, missing = _equity_gap(acct, market, 10)
    if dates:
        checks.append(f"净值 {len(dates) - len(missing)}/{len(dates)} 个交易日连续"
                      + (f"，缺 {'、'.join(dm.dash(x) for x in missing)}" if missing else " ✅"))
        if missing:
            problems.append("净值记录缺 " + "、".join(dm.dash(x) for x in missing))
    L.append("- 体检：" + "｜".join(checks))

    if problems:
        L.append("- ⚠️ 异常：" + "；".join(problems)
                 + "。先查 `logs/cron.log` 与 `crontab -l`，确认是数据源还是调度缺失。")
    else:
        L.append("- ✅ 推送、数据、净值记录均正常。")

    cands = plan.get("candidates", []) if plan else []
    if not res.allow_new:
        watch = "不开新仓，持仓按止损/移动止盈执行，不补仓不摊薄。"
    elif cands:
        watch = ("、".join(
            f"{c['name']}({c['gap_min']:+.1%}~{c['gap_max']:+.1%})"
            for c in cands[:4]) + " 的开盘触发区间。")
    else:
        watch = "无候选，按纪律空仓/持有，不追高、不临时加仓。"
    L.append(f"- 明日关注：{watch}")
    return L


def build_report(date: str, market: dm.MarketData, params, res, acct: dict,
                 plan: dict, fills: list[dict], settle_ev: list[dict], m: dict,
                 prev_plan: dict | None = None, state_dir: Path | None = None,
                 report_dir: Path | None = None) -> str:
    s = pf.stats(acct)
    total_ret = m["equity"] / float(acct["initial"]) - 1
    eq = m["equity"]

    L = [f"## {params.rt('push', 'title_prefix', default='常规量价模拟盘')} {dm.dash(date)}", ""]
    if market.source == "synthetic":
        L.append("> ⚠️ 合成数据演示，不是真实行情；只用于验证规则。")
        L.append("")
    L.append(f"**市场状态**：{res.state_cn}（温度 {res.heat:.1f}，第 {res.dwell} 天）"
             f" → 总仓位上限 {res.cap:.0%}")
    L.extend(_state_metrics(res))
    if res.reasons:
        L.append("状态理由：" + "；".join(res.reasons[:4]))
    L.append("温度分解：" + " / ".join(
        f"{k} {v:.0%}" for k, v in res.parts.items()))
    L.append("")

    L.append(f"**账户**：净值 **{_fmt_money(eq)}**"
             f"（当日 {m['day_ret']:+.2%}，累计 {total_ret:+.2%}）")
    L.append(f"现金 {_fmt_money(m['cash'])} | 持仓市值 {_fmt_money(m['mv'])} | "
             f"仓位 {(m['mv'] / eq if eq else 0):.0%} | 未触发风险 {m['open_risk'] / eq:.1%}"
             if eq else "")
    L.append(f"最大回撤 {s['max_dd']:.2%} | 交易 {s['n_trades']} 笔 | "
             f"胜率 {s['win_rate']:.0%} | 盈亏比 {s['profit_factor']:.2f} | "
             f"平均单笔 {s['avg_ret']:+.2%}")
    L.append("")

    buys = [x for x in fills if x.get("action") == "买入"]
    sells = [x for x in settle_ev if x.get("action") in ("卖出", "减半")]
    L.append("**今日成交**")
    if not buys and not sells:
        L.append("- 无")
    for x in buys:
        L.append(f"- 买入 {x['code']} {x['name']} @{x['price']} "
                 f"（{eng_label(x.get('setup', ''))}，开盘 {x['gap']:+.1%}）")
    for x in sells:
        L.append(f"- {x['action']} {x['code']} {x['name']} @{x['price']:.2f} "
                 f"{x['reason']}（{x['ret']:+.2%}）")
    L.append("")

    skips = [x for x in fills if x.get("action") in ("未成交", "跳过", "整体跳过")]
    if skips:
        L.append("**未成交/跳过**")
        for x in skips[:8]:
            nm = f"{x.get('code', '')} {x.get('name', '')}".strip()
            L.append(f"- {nm or '（整体）'}：{x.get('reason', '')}")
        if len(skips) > 8:
            L.append(f"- …另有 {len(skips) - 8} 条")
        L.append("")

    L.append("**当前持仓**")
    if not acct["positions"]:
        L.append("- 空仓")
    for p in acct["positions"]:
        cur = p.get("last_close", p["entry_price"])
        r = cur / p["entry_price"] - 1
        L.append(f"- {p['code']} {p['name']}（{eng_label(p.get('setup', ''))}）"
                 f"成本 {p['entry_price']:.2f} 现价 {cur:.2f} **{r:+.2%}** "
                 f"持 {pf._hold_days(p['entry_date'], date)} 天 | 止损 {p['stop']:.2f}")
    L.append("")

    L.append(f"**明日计划**（{dm.dash(plan.get('for_date')) if plan.get('for_date') else '待定'}）")
    L.append(f"当前状态「{res.state_cn}」→ "
             f"{'可开仓：' + '、'.join(SETUP_CN.get(x, x) for x in res.allowed_setups) if res.allow_new else '不开新仓'}")
    L.extend(stg.plan_lines(plan, params, limit=6))
    L.append("")

    L.append("**口诀执行**")
    L.extend(_rule_section(settle_ev, plan))
    L.append("")

    L.append("**风控**")
    allow, mult, why = pf.circuit(acct, plan.get("for_date") or date, params)
    L.append(f"- {'禁止开仓：' + why if not allow else ('仓位系数 ' + format(mult, '.1f') + ('（' + why + '）' if why else ''))}")
    L.append(f"- 状态仓位上限 {res.cap:.0%} | 槽位 {len(acct['positions'])}/"
             f"{params.get('portfolio.slots')} | 单票上限 {params.get('portfolio.max_weight'):.0%}")
    L.append(f"- 单笔风险 {params.get('portfolio.risk_per_trade'):.2%} | "
             f"组合风险上限 {params.get('portfolio.max_portfolio_heat'):.1%}")
    L.append("")
    L.append("**今日复盘**")
    L.extend(_review_section(date, market, params, res, acct, plan, fills,
                             prev_plan, m, state_dir, report_dir))
    L.append("")
    L.append(f"> 参数：{params.summary()}。模拟盘，非投资建议。"
             f"次日开盘成交假设，不含盘中冲击成本。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(date: str | None = None, do_push: bool = True, params=None,
        force: bool = False, market: dm.MarketData | None = None,
        state_dir: Path | None = None, report_dir: Path | None = None) -> int:
    params = params or load_params()
    date = market.date if market is not None else dm.latest_trade_date(date)
    state_dir = Path(state_dir or STATE_DIR)
    report_dir = Path(report_dir or REPORT_DIR)
    state_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    acct_path = state_dir / "account.json"
    reg_path = state_dir / "regime.json"
    plan_path = state_dir / "plan.json"

    print(f"[info] 交易日 {date} | {params.summary()}")
    acct = pf.load(params, acct_path)
    if acct.get("last_run") == date and not force:
        print(f"[info] {date} 已处理过，跳过（--force 可强制重跑）")
        return 0

    if market is None:
        market = dm.load_market(date, params)
        date = market.date

    reg = rg.load(reg_path)
    prev_plan = load_plan(plan_path)

    # 1) 昨日计划在今日开盘执行。只能用“昨天收盘已知”的状态与仓位上限。
    if prev_plan.get("for_date") == date:
        print(f"[info] 执行 {prev_plan.get('date')} 的计划："
              f"{len(prev_plan.get('candidates', []))} 只候选，状态 {prev_plan.get('state')}")
    fills = execute_plan(acct, prev_plan, date, market, params)

    # 2) 今日收盘状态
    reg, res = rg.update(market, params, reg)
    print(f"[info] 状态 {res.state_cn} 温度 {res.heat:.1f} "
          f"（{res.cap:.0%} 上限，{'可开仓' if res.allow_new else '不开仓'}）")

    # 3) 结算持仓
    settle_ev = pf.settle(acct, date, market, res, params)
    for e in settle_ev:
        if e.get("action") in ("卖出", "减半", "洗盘持有", "跌停封死，卖出顺延"):
            print(f"[info] {e['code']} {e['name']} {e['action']} "
                  f"{e.get('reason', '')} {e.get('ret', 0):+.2%}")

    # 4) 盯市
    m = pf.mark(acct, date, market, params)

    # 5) 明日计划
    nxt = _next_date(date)
    plan = stg.build_plan(market, res, params)
    plan["for_date"] = nxt

    acct["last_run"] = date
    rg.save(reg, reg_path)
    pf.save(acct, acct_path)
    save_plan(plan, plan_path)

    report = build_report(date, market, params, res, acct, plan, fills, settle_ev, m,
                          prev_plan=prev_plan, state_dir=state_dir,
                          report_dir=report_dir)
    (report_dir / f"{dm.dash(date)}.md").write_text(report, encoding="utf-8")
    print(report)

    if do_push:
        title = (f"[{params.rt('push', 'title_prefix', default='常规量价模拟盘')}] "
                 f"{dm.dash(date)[5:]} {res.state_cn} 净值 "
                 f"{m['equity'] / acct['initial']:.4f} {m['day_ret']:+.2%}")
        n = pusher.push(title, report, params)
        print(f"[info] push 成功 token 数 {n}")
        if n > 0:
            try:
                rev.note_push("close", date, params, state_dir, report_dir)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 推送计数/复盘触发失败：{e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--profile", default=None,
                    choices=["defensive", "balanced", "offensive"])
    ap.add_argument("--config", default=None, help="参数 YAML 路径")
    ap.add_argument("--force", action="store_true", help="强制重跑已处理日期")
    ap.add_argument("--demo", action="store_true", help="合成数据演示，不联网")
    ap.add_argument("--state-dir", default=None, help="覆盖 state 目录（测试/回放用）")
    ap.add_argument("--report-dir", default=None, help="覆盖 reports 目录（测试/回放用）")
    a = ap.parse_args()

    try:
        if a.demo:
            from synthetic import demo_market, seed_demo_account
            params = load_params(a.config, a.profile)
            market = demo_market(params)
            demo_state = DEMO_DIR / "state"
            demo_reports = DEMO_DIR / "reports"
            demo_state.mkdir(parents=True, exist_ok=True)
            demo_reports.mkdir(parents=True, exist_ok=True)
            seed_demo_account(params, market, demo_state, force=a.force)
            return run(market.date, False, params, force=True, market=market,
                       state_dir=demo_state, report_dir=demo_reports)

        params = load_params(a.config, a.profile)
        return run(a.date, not a.no_push, params, a.force,
                   state_dir=Path(a.state_dir) if a.state_dir else None,
                   report_dir=Path(a.report_dir) if a.report_dir else None)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not a.no_push and not a.demo:
            try:
                pusher.push(f"[常规量价模拟盘] 运行失败 {datetime.now():%m-%d %H:%M}",
                            "```\n" + tb[-1200:] + "\n```", load_params(a.config, a.profile))
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
