#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘前推送（09:15）：今日条件单 + 持仓卖出关注。

数据来自昨晚收盘生成的 plan.json，不重新拉行情。
集合竞价还没结束，所以只给“开盘触发区间”，不给确定成交价。

推送规则：
  - 非交易日/计划日期不匹配：不推
  - 没有可买候选：推一条短心跳（证明系统活着、今天不做）
  - 有候选：推完整清单
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

import data as dm
import portfolio as pf
import push as pusher
import regime as rg
import review as rev
from config import (
    REPORT_DIR, STATE_CN, STATE_DIR, eng_label, load_params,
)


def load_plan(path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def build(date: str, plan: dict, reg_state: str, acct: dict, params) -> str:
    cands = plan.get("candidates", [])
    slots_left = max(0, int(params.get("portfolio.slots")) - len(acct["positions"]))
    eq = pf.equity(acct)
    cap = float(plan.get("cap", 0.0))

    L = [f"## {params.rt('push', 'title_prefix', default='常规量价模拟盘')} · 盘前 {dm.dash(date)}", ""]
    L.append(f"**昨收状态**：{STATE_CN.get(reg_state, reg_state)} → "
             f"{'可开仓' if plan.get('allow_new') else '不开新仓'}｜仓位上限 {cap:.0%}")
    L.append(f"账户 **{eq:,.0f}**｜空槽位 {slots_left}/{params.get('portfolio.slots')}｜"
             f"参数 {params.profile} #{params.digest}")
    L.append("")

    if cands:
        L.append("**今日可买**（开盘价触发，不追高）")
        for c in cands[:6]:
            L.append(f"- **{c['code']} {c['name']}**（{eng_label(c['setup'])} · 评分 {c['score']:.0f}）")
            L.append(f"  开盘 {c['gap_min']:+.1%} ~ {c['gap_max']:+.1%} 买入；"
                     f"止损 {c['stop']:.2f}；目标 {c['target_weight']:.0%} 仓")
            if c.get("reasons"):
                L.append("  理由：" + "；".join(c["reasons"][:3]))
    else:
        L.append("**今日无可买**：当前状态空仓/无符合横、坑、小涨的标的。")
    L.append("")

    # 持仓卖出关注（盘前只能提示，不能预知今日价格）
    if acct["positions"]:
        L.append("**持仓卖出关注**")
        for p in acct["positions"]:
            cur = p.get("last_close", p["entry_price"])
            r = cur / p["entry_price"] - 1
            tags = []
            if r >= float(params.get("holding.trail_start")):
                tags.append("已到移动止盈区")
            if p.get("partial_done"):
                tags.append("已减半")
            note = ("｜" + "、".join(tags)) if tags else ""
            L.append(f"- {p['code']} {p['name']}（{eng_label(p.get('setup', ''))}）"
                     f"成本 {p['entry_price']:.2f} 昨收 {cur:.2f} **{r:+.2%}**"
                     f"｜止损 {p['stop']:.2f}{note}")
        L.append("")
    else:
        L.append("**持仓**：空仓")
        L.append("")

    L.append("**执行规则**")
    L.append("- 开盘不在区间 → 不买；高开超过硬性追高线 → 不买")
    L.append("- 一字/涨停开盘 → 买不进，跳过")
    L.append("- 买入后 T+1 才能卖；盘中止损按计划价，不临时改单")
    L.append("")
    L.append("> 模拟盘，非投资建议。")
    return "\n".join(L)


def heartbeat(date: str, plan: dict, reg_state: str, acct: dict, params) -> str:
    eq = pf.equity(acct)
    L = [f"## {params.rt('push', 'title_prefix', default='常规量价模拟盘')} · 盘前 {dm.dash(date)}", ""]
    L.append(f"**{STATE_CN.get(reg_state, reg_state)}** → 今日不交易（按门控空仓/无信号）")
    L.append(f"账户 {eq:,.0f}｜持仓 {len(acct['positions'])}/{params.get('portfolio.slots')}")
    L.append("")
    L.append("> 不交易也是仓位。模拟盘，非投资建议。")
    return "\n".join(L)


def run(date: str | None, do_push: bool, params,
        state_dir=None, report_dir=None) -> int:
    today = dm.norm_date(date) if date else dt.date.today().strftime("%Y%m%d")
    if date is None:
        try:
            if today not in dm.trade_dates():
                print(f"[info] {today} 非交易日，跳过")
                return 0
        except Exception:
            pass
    date = dm.latest_trade_date(date)

    from pathlib import Path
    state_dir = Path(state_dir or STATE_DIR)
    report_dir = Path(report_dir or REPORT_DIR)
    plan = load_plan(state_dir / "plan.json")
    if plan.get("for_date") != date:
        print(f"[info] 计划日期 {plan.get('for_date')} ≠ {date}，跳过")
        return 0

    reg = rg.load(state_dir / "regime.json")
    reg_state = reg.get("current") or plan.get("state") or "COLD"
    acct = pf.load(params, state_dir / "account.json")
    cands = plan.get("candidates", [])

    text = build(date, plan, reg_state, acct, params) if cands else heartbeat(
        date, plan, reg_state, acct, params)
    print(text)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{dm.dash(date)}-premarket.md").write_text(text, encoding="utf-8")
    except Exception:
        pass

    if do_push:
        if not cands and not params.rt("push", "premarket_heartbeat", default=True):
            print("[info] 无可执行候选且未开心跳，跳过推送")
            return 0
        tail = f"{len(cands)} 只候选" if cands else "不交易"
        title = (f"[{params.rt('push', 'title_prefix', default='常规量价模拟盘')}] "
                 f"{dm.dash(date)[5:]} 盘前 "
                 f"{STATE_CN.get(reg_state, reg_state)}·{tail}")
        n = pusher.push(title, text, params)
        print(f"[info] push 成功 token 数 {n}")
        if n > 0:
            try:
                rev.note_push("premarket", date, params, state_dir, report_dir)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 盘前推送计数失败：{e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--profile", default=None,
                    choices=["defensive", "balanced", "offensive"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--state-dir", default=None)
    a = ap.parse_args()
    try:
        params = load_params(a.config, a.profile)
        return run(a.date, not a.no_push, params, a.state_dir)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not a.no_push:
            try:
                pusher.push(f"[常规量价模拟盘] 盘前运行失败",
                            "```\n" + tb[-1000:] + "\n```", load_params(a.config, a.profile))
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
