#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
09:40 盘中入场：按昨晚的计划，用**今日开盘价**模拟成交。

为什么要单独一个 09:40 的任务：
    原来的设计是 16:10 收盘后「回放」当日开盘买入，虽然价格正确，但人要等到晚上
    才知道今天买了什么。改成盘中执行后，买入事实在 09:40 就能确认并推送。

推送规则（按需求）：
    - 有买入 → 推「买入」消息
    - 无买入 → 不推（安静）
    - 16:10 的收盘复盘不受影响，照常推送

幂等：同一交易日只执行一次（last_entry_date 标记）。
兜底：若本任务没跑成，16:10 的 run_daily.py 会用日线开盘价补做。
"""

from __future__ import annotations

import argparse
import datetime as dt

import data as dm
import portfolio as pf
import push as pusher
import regime as rg
import run_daily as rd
from config import (
    ENGINE, REPORT_DIR, REPORT_TITLE_PREFIX, SLOTS, STATE_CN, STATE_ENGINES, eng,
)


def build_buy_msg(date: str, buys: list[dict], acct: dict, state: str) -> str:
    eq = pf.equity(acct)
    L = [f"## {REPORT_TITLE_PREFIX} · 买入 {dm.dash(date)}", ""]
    L.append(f"**{STATE_CN.get(state, state)}** ｜ 开盘触发，已买入 **{len(buys)}** 笔")
    L.append("")
    for b in buys:
        tp = ENGINE.get(b["engine"], {}).get("tp", 0)
        sl = ENGINE.get(b["engine"], {}).get("sl", 0)
        L.append(f"- **{b['code']} {b['name']}**（{eng(b['engine'])} · "
                 f"{b.get('industry', '')}）")
        L.append(f"  买入 {b['price']:.2f}（开盘 {b.get('gap', 0):+.1%}）｜ "
                 f"{b['shares']:,} 股 ｜ {b['amount'] / 10000:.1f} 万 ｜ "
                 f"止盈 +{tp:.0%} / 止损 −{sl:.0%}")
    L.append("")
    pos_pct = (eq - acct["cash"]) / eq if eq > 0 else 0
    L.append(f"账户 {eq:,.0f} ｜ 持仓 {len(acct['positions'])}/{SLOTS} ｜ 仓位 {pos_pct:.0%}")
    L.append("")
    L.append("> 模拟盘（按开盘价成交），非投资建议。")
    return "\n".join(L)


def run(date: str | None, do_push: bool) -> int:
    today = dm.norm_date(date) if date else dt.date.today().strftime("%Y%m%d")
    if today not in dm.trade_dates():
        print(f"[info] {today} 非交易日，跳过")
        return 0
    date = dm.latest_trade_date(today)

    acct = pf.load()
    if acct.get("last_entry_date") == date:
        print(f"[info] {date} 入场已处理过，跳过")
        return 0

    plan = rd.load_plan()
    if plan.get("for_date") != date:
        print(f"[info] 计划执行日 {plan.get('for_date')} ≠ {date}，跳过")
        return 0

    reg = rg.load()
    past = [h for h in reg.get("history", []) if h["date"] < date and h.get("state")]
    state_prev = past[-1]["state"] if past else (reg.get("current") or "REPAIR")

    cands = plan.get("candidates", [])
    codes = [c["code"] for c in cands]
    opens = dm.realtime_open(codes) if codes else {}
    print(f"[info] {date} 计划 {len(cands)} 只，取到实时开盘价 {len(opens)} 只，"
          f"门控状态 {state_prev}")

    fills = rd.execute_plan(acct, plan, date, state_prev, opens=opens)
    buys = [x for x in fills if x.get("action") == "买入"]

    log = acct.setdefault("entry_log", {})
    log[date] = buys
    acct["entry_log"] = dict(sorted(log.items())[-30:])
    acct["last_entry_date"] = date
    pf.save(acct)

    for x in fills:
        print(f"  [{x.get('action')}] {x.get('code', '')} {x.get('name', '')} "
              f"{x.get('reason', '')}")

    if not buys:
        print("[info] 今日无买入，不推送")
        return 0

    text = build_buy_msg(date, buys, acct, state_prev)
    print(text)
    (REPORT_DIR / f"{dm.dash(date)}-buy.md").write_text(text, encoding="utf-8")

    if do_push:
        n = pusher.push(
            f"[{REPORT_TITLE_PREFIX}] {dm.dash(date)[5:]} 买入 {len(buys)} 只", text)
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
            pusher.push(f"[{REPORT_TITLE_PREFIX}] 买入任务失败",
                        "```\n" + tb[-1000:] + "\n```")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
