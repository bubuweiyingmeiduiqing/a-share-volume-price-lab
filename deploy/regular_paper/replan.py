#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只重算“明日计划”，不碰账户、不结算、不推送。

用途：收盘后改了参数/规则（例如启用冰点深坑例外），需要让明天的条件单
用新规则重算，但又不想重复推送今天的日报、不想重复计数。

用法：
  python3 replan.py --date 20261112
  python3 replan.py --date 20261112 --no-report
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import data as dm
import regime as rg
import strategy as stg
from config import REPORT_DIR, STATE_DIR, load_params, eng_label


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--profile", default=None,
                    choices=["defensive", "balanced", "offensive"])
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    try:
        params = load_params(a.config, a.profile)
        market = dm.load_market(a.date, params)
        date = market.date

        reg = rg.load(STATE_DIR / "regime.json")
        hist = [h for h in reg.get("history", []) if h["date"] < date]
        prev = hist[-1] if hist else None
        prev_state = prev["state"] if prev else None
        dwell = 0
        if prev:
            for h in reversed(hist):
                if h["state"] == prev_state:
                    dwell += 1
                else:
                    break
        res = rg.evaluate(market, params, prev_state, dwell)

        plan = stg.build_plan(market, res, params)
        plan["for_date"] = dm.next_trade_date(date)
        plan["replanned_at"] = date
        plan["replan_note"] = "参数/规则变更后重算，未推送"

        path = STATE_DIR / "plan.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        lines = [f"# 重算计划 {dm.dash(date)} → {dm.dash(plan['for_date']) if plan['for_date'] else '待定'}", ""]
        lines.append(f"- 状态：{res.state_cn}（温度 {res.heat:.1f}，仓位上限 {res.cap:.0%}）")
        lines.append(f"- 允许形态：{'、'.join(eng_label(x) for x in res.allowed_setups) or '无'}")
        lines.append(f"- 候选：{len(plan.get('candidates', []))} 只")
        for c in plan.get("candidates", []):
            lines.append(f"  - {c['code']} {c['name']} {eng_label(c['setup'])} "
                         f"评分 {c['score']:.0f}，开盘 {c['gap_min']:+.1%}~{c['gap_max']:+.1%}，"
                         f"止损 {c['stop']:.2f}，目标 {c['target_weight']:.0%} 仓")
        lines.append("")
        lines.append("> 只重算计划，不执行、不结算、不推送。")
        text = "\n".join(lines)
        print(text)
        if not a.no_report:
            report_dir = Path(REPORT_DIR)
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / f"{dm.dash(date)}-replan.md").write_text(text, encoding="utf-8")
        return 0
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
