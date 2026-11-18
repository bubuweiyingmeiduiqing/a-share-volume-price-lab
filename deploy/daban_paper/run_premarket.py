#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘前推送（09:15）：今日可买清单。

数据来自昨晚生成的计划 + 昨日收盘状态。
此时集合竞价还没结束，所以只给「触发区间」，不给确定成交价。

推送规则：
  - 非交易日：不推
  - 没有可执行候选（退潮/分歧/空仓）：不推（PREMARKET_ONLY_WHEN_SIGNAL）
  - 有候选：推，标题带引擎名
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

import data as dm
import portfolio as pf
import push as pusher
import regime as rg
from config import (
    ENGINE, PREMARKET_HEARTBEAT, PREMARKET_ONLY_WHEN_SIGNAL,
    REPORT_TITLE_PREFIX, SLOTS,
    STATE_CAP, STATE_CN, STATE_ENGINES, STATE_DIR, eng,
)

PLAN_FILE = STATE_DIR / "plan.json"


def load_plan() -> dict:
    if PLAN_FILE.exists():
        try:
            return json.loads(PLAN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def build(date: str, plan: dict, state: str, eq: float, cash: float,
          n_pos: int) -> tuple[str, str]:
    eng_ok = STATE_ENGINES.get(state, [])
    cands = plan.get("candidates", [])
    run_now = [c for c in cands if c["engine"] in eng_ok]
    watch = [c for c in cands if c["engine"] not in eng_ok]

    cap = STATE_CAP.get(state, 0.0)
    slots_left = max(0, SLOTS - n_pos)

    L = [f"## {REPORT_TITLE_PREFIX} · 盘前 {dm.dash(date)}", ""]
    L.append(f"**昨收状态**：{STATE_CN.get(state, state)} → 今日可执行："
             f"{'、'.join(eng(e) for e in eng_ok) if eng_ok else '无（空仓）'}")
    L.append(f"账户 **{eq:,.0f}** ｜ 空槽位 {slots_left}/{SLOTS} ｜ 状态仓位上限 {cap:.0%}")
    L.append("")

    if run_now:
        L.append("**今日可买**（开盘价触发）")
        for c in run_now[:6]:
            w = min(ENGINE[c["engine"]]["weight"], cap)
            L.append(f"- **{c['code']} {c['name']}**（{eng(c['engine'])} · {c['boards']}板 · "
                     f"{c['industry']}）")
            L.append(f"  开盘 {c['gap_min']:+.1%} ~ {c['gap_max']:+.1%} 买入 · "
                     f"计划 {w:.0%} 仓位")
    else:
        L.append("**今日无可买**：当前状态不允许开新仓。")
        L.append("")

    if watch:
        L.append("")
        L.append("**观察**（需状态切换）")
        for c in watch[:4]:
            need = "主升" if c["engine"] == "E1" else "修复/主升"
            L.append(f"- {c['code']} {c['name']}（{eng(c['engine'])} · {c['boards']}板）"
                     f" 需{need}")

    L.append("")
    L.append("**规则**")
    L.append("- 开盘价不在区间 → 不买")
    L.append("- 一字开盘 → 买不进，跳过")
    L.append(f"- 同题材最多 2 只，最多 {SLOTS} 个槽位")
    L.append("")
    L.append("> 模拟盘，非投资建议。")
    return "\n".join(L), (run_now[0]["engine"] if run_now else "")


def build_heartbeat(date: str, state: str, eq: float, n_pos: int,
                    plan: dict) -> str:
    """无可买时的短消息：只为证明「系统活着、今天不做」。"""
    eng_ok = STATE_ENGINES.get(state, [])
    watch = [c for c in plan.get("candidates", []) if c["engine"] not in eng_ok]
    L = [f"## {REPORT_TITLE_PREFIX} · 盘前 {dm.dash(date)}", ""]
    L.append(f"**{STATE_CN.get(state, state)}** → 今日不交易（状态不允许开仓）")
    L.append(f"账户 {eq:,.0f} ｜ 空槽位 {SLOTS - n_pos}/{SLOTS}")
    if watch:
        L.append("观察：" + "、".join(
            f"{c['name']}（{eng(c['engine'])} {c['boards']}板）" for c in watch[:5]))
    L.append("")
    L.append("> 模拟盘，非投资建议。")
    return "\n".join(L)


def run(date: str | None, do_push: bool) -> int:
    today = dm.norm_date(date) if date else dt.date.today().strftime("%Y%m%d")
    if date is None:
        cal = dm.trade_dates()
        if today not in cal:
            print(f"[info] {today} 非交易日，跳过")
            return 0
        date = today
    date = dm.latest_trade_date(date)

    plan = load_plan()
    if plan.get("for_date") != date:
        print(f"[info] 计划日期 {plan.get('for_date')} ≠ {date}，跳过")
        return 0

    reg = rg.load()
    state = reg.get("current") or "REPAIR"
    acct = pf.load()
    eq = pf.equity(acct)

    eng_ok = STATE_ENGINES.get(state, [])
    run_now = [c for c in plan.get("candidates", []) if c["engine"] in eng_ok]

    if run_now or not PREMARKET_HEARTBEAT:
        text, _ = build(date, plan, state, eq, acct["cash"], len(acct["positions"]))
    else:
        text = build_heartbeat(date, state, eq, len(acct["positions"]), plan)
    print(text)
    try:
        from config import REPORT_DIR
        (REPORT_DIR / f"{dm.dash(date)}-premarket.md").write_text(text, encoding="utf-8")
    except Exception:
        pass

    if do_push:
        if not run_now:
            if PREMARKET_ONLY_WHEN_SIGNAL or not PREMARKET_HEARTBEAT:
                print("[info] 无可执行候选，按规则不推送")
                return 0
            hb = build_heartbeat(date, state, eq, len(acct["positions"]), plan)
            print(hb)
            title = (f"[{REPORT_TITLE_PREFIX}] {dm.dash(date)[5:]} 盘前 "
                     f"{STATE_CN.get(state, state)}·不交易")
            n = pusher.push(title, hb)
            print(f"[info] 心跳已推送，token 数 {n}")
            return 0

        title = (f"[{REPORT_TITLE_PREFIX}] {dm.dash(date)[5:]} 盘前 "
                 f"{eng(run_now[0]['engine'])} {len(run_now)} 只")
        n = pusher.push(title, text)
        print(f"[info] push 成功 token 数 {n}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    try:
        return run(a.date, not a.no_push)
    except Exception:
        import sys
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not a.no_push:
            pusher.push(f"[{REPORT_TITLE_PREFIX}] 盘前运行失败",
                        "```\n" + tb[-1000:] + "\n```")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
