#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打板模拟盘 · 每日主流程

时序（全部日线级，不使用分时）：
  1. 加载账户 / 状态 / 昨日计划
  2. 推进市场状态（必要时回补历史）
  3. 结算旧持仓（T+1：entry_date < 今天）
  4. 执行昨日计划 → 以今日开盘价成交（一字开盘则判定买不进）
  5. 收盘盯市，更新净值与熔断
  6. 生成明日计划（条件单）
  7. 写报告 + PushPlus 推送

用法：
  python3 run_daily.py                    # 跑最近交易日并推送
  python3 run_daily.py --date 20261103    # 指定日
  python3 run_daily.py --no-push          # 不推送
  python3 run_daily.py --bootstrap 60     # 首次回补 60 天情绪历史
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import data as dm
import portfolio as pf
import push as pusher
import regime as rg
import strategy as stg
from config import (
    REPORT_DIR, REPORT_TITLE_PREFIX, SLOTS, STATE_CN, STATE_ENGINES, STATE_CAP,
    THEME_CAP, BOOTSTRAP_DAYS, STATE_DIR, eng, ENGINE_NAME,
)

PLAN_FILE = STATE_DIR / "plan.json"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def load_plan() -> dict:
    if PLAN_FILE.exists():
        try:
            return json.loads(PLAN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_plan(p: dict) -> None:
    tmp = PLAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PLAN_FILE)


def ensure_history(state: dict, date: str, n: int) -> dict:
    """历史不足时回补最近 n 个交易日的因子。"""
    if state.get("bootstrapped"):
        return state
    have = {h["date"] for h in state.get("history", [])}
    if len(have) >= n:
        state["bootstrapped"] = True
        return state
    need = [d for d in dm.recent_trade_dates(date, n) if d not in have]
    if not need:
        return state
    print(f"[info] 回补 {len(need)} 天情绪历史…")
    rows = [h for h in state.get("history", []) if h["date"] not in need]
    for d in need:
        try:
            f = rg.compute_factors(d)
            if f["zt"] == 0 and f["zb"] == 0:
                continue
            rows.append(f)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 回补 {d} 失败: {e}")
    rows.sort(key=lambda x: x["date"])
    rows = rg.with_momentum(rows)
    # 严格 as-of：第 i 天的分位数只能用 rows[:i+1]，不能用未来数据
    for i, r in enumerate(rows):
        e_raw, _ = rg.e_score(rows[:i + 1], r)
        r["e_raw"] = e_raw
        r["e"] = e_raw
    rows = rg.smooth(rows)
    prev = None
    for i, r in enumerate(rows):
        st_i, _ = rg.classify(r["e"], rows[:i + 1], prev, 99)
        r["state"] = st_i
        prev = st_i
    state["history"] = rows[-260:]
    tail = 0
    for r in reversed(state["history"]):
        if r.get("state") == state["history"][-1].get("state"):
            tail += 1
        else:
            break
    state["bootstrapped"] = True
    state["current"] = state["history"][-1].get("state")
    state["days_in_state"] = tail
    print(f"[info] 历史 {len(state['history'])} 天，当前 {state['current']} 第 {tail} 天")
    return state


# ---------------------------------------------------------------------------
# 执行昨日计划
# ---------------------------------------------------------------------------

def execute_plan(acct: dict, prev_plan: dict, date: str, state: str,
                 opens: dict | None = None) -> list[dict]:
    out = []
    if not prev_plan or prev_plan.get("date") == date:
        return out
    allow, cap_mult, why = pf.circuit(acct, date)
    if not allow:
        out.append({"action": "整体跳过", "reason": why})
        return out

    allowed_engines = set(STATE_ENGINES.get(state, []))
    held = {p["code"] for p in acct["positions"]}

    for c in prev_plan.get("candidates", []):
        if len(acct["positions"]) >= SLOTS:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": "槽位已满"})
            continue
        if c["engine"] not in allowed_engines:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": f"当前状态 {STATE_CN[state]} 不允许 {c['engine']}"})
            continue
        if c["code"] in held:
            continue
        same_theme = sum(1 for p in acct["positions"] if p["industry"] == c["industry"])
        if same_theme >= THEME_CAP:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": f"同题材已持 {same_theme} 只"})
            continue

        # 开盘价：盘中用实时行情；收盘后回退到日线
        if opens is not None:
            op = opens.get(c["code"])
            prev_close = c.get("close") or c.get("prev_close")
            if not op or not prev_close:
                out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                            "reason": "无实时开盘价"})
                continue
        else:
            bar = dm.bar_on(c["code"], date)
            if bar is None:
                out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                            "reason": "今日无行情（停牌？）"})
                continue
            op, prev_close = bar["open"], bar["prev_close"]

        ratio = dm.limit_ratio(c["code"], c["name"])
        lp = dm.limit_up_price(prev_close, ratio)
        if op >= lp - 0.011:
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": "一字开盘，买不进"})
            continue

        gap = op / prev_close - 1 if prev_close else 0
        if gap < c["gap_min"]:
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": f"开盘涨幅 {gap:+.2%} < {c['gap_min']:.1%}，未确认强势"})
            continue
        if gap > c["gap_max"]:
            out.append({"action": "未成交", "code": c["code"], "name": c["name"],
                        "reason": f"开盘涨幅 {gap:+.2%} > {c['gap_max']:.1%}，一致性过高"})
            continue

        r = pf.buy(acct, c, op, date, state, cap_mult)
        if r.get("ok"):
            out.append({"action": "买入", "code": c["code"], "name": c["name"],
                        "engine": c["engine"], "industry": c["industry"],
                        "price": round(op, 3), "prev_close": round(prev_close, 3),
                        "shares": r["pos"]["shares"],
                        "amount": round(r["pos"]["cost_amount"], 2),
                        "gap": round(gap, 4), "reason": f"{c['engine']} 开盘 {gap:+.1%}"})
        else:
            out.append({"action": "跳过", "code": c["code"], "name": c["name"],
                        "reason": r.get("why", "")})
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def fmt_money(x: float) -> str:
    return f"{x:,.0f}"


def build_report(date: str, res: dict, acct: dict, plan: dict,
                 settle_ev: list[dict], fills: list[dict], m: dict,
                 buys: list[dict] | None = None) -> str:
    s = pf.stats(acct)
    f = res["factors"]
    eq = m["equity"]
    total_ret = eq / acct["initial"] - 1

    L = []
    L.append(f"## {REPORT_TITLE_PREFIX} {dm.dash(date)}")
    L.append("")
    L.append(f"**市场状态**：{res['state_cn']}（E={res['e']:.1f}，第 {res['dwell']} 天）"
             f"  ")
    L.append(f"涨停 **{f['zt']}** / 炸板 {f['zb']} / 最高 **{f['maxb']}** 板 | "
             f"封板率 {f['seal']:.0%} | 晋级率 {f['promo']:.0%} | "
             f"昨涨停今均涨 {f['prevret']:+.2f}%")
    L.append("")
    L.append(f"**账户**：净值 **{fmt_money(eq)}**（当日 {m['day_ret']:+.2%}，累计 {total_ret:+.2%}）")
    L.append(f"现金 {fmt_money(m['cash'])} | 持仓市值 {fmt_money(m['mv'])} | "
             f"仓位 {m['mv'] / eq:.0%}" if eq > 0 else "")
    L.append(f"最大回撤 {s['max_dd']:.2%} | 交易 {s['n_trades']} 笔 | 胜率 {s['win_rate']:.0%} | "
             f"盈亏比 {s['profit_factor']:.2f}")
    L.append("")

    if buys is None:
        buys = [x for x in fills if x.get("action") == "买入"]
    sells = [x for x in settle_ev if x.get("action") in ("卖出", "减半")]
    L.append("**今日成交**")
    if buys:
        cnt = {}
        for b in buys:
            cnt[b["engine"]] = cnt.get(b["engine"], 0) + 1
        L.append("今日执行：" + "、".join(f"{eng(k)} ×{v}" for k, v in cnt.items()))
    if not buys and not sells:
        L.append("- 无")
    for x in buys:
        L.append(f"- 买入 {x['code']} {x['name']} @{x['price']} "
                 f"（{eng(x['engine'])}，开盘 {x['gap']:+.1%}）")
    for x in sells:
        L.append(f"- 卖出 {x['code']} {x['name']} @{x['price']:.2f} "
                 f"{x['reason']}（{x['ret']:+.2%}）")
    L.append("")

    skips = [x for x in fills if x.get("action") in ("未成交", "跳过", "整体跳过")]
    if skips:
        L.append("**未成交/跳过**")
        for x in skips[:6]:
            nm = f"{x.get('code', '')} {x.get('name', '')}".strip()
            L.append(f"- {nm or '（整体）'}：{x.get('reason', '')}")
        if len(skips) > 6:
            L.append(f"- …另有 {len(skips) - 6} 条")
        L.append("")

    L.append("**当前持仓**")
    if not acct["positions"]:
        L.append("- 空仓")
    for p in acct["positions"]:
        cur = p.get("last_close", p["entry_price"])
        r = cur / p["entry_price"] - 1
        L.append(f"- {p['code']} {p['name']}（{eng(p['engine'])}）成本 {p['entry_price']:.2f} "
                 f"现价 {cur:.2f} **{r:+.2%}** 持 {pf._hold_days(p['entry_date'], date)} 天")
    L.append("")

    _fd = plan.get("for_date")
    L.append(f"**明日计划**（{dm.dash(_fd) if _fd else '待定'}）")
    mains = plan.get("main_themes") or []
    L.append(f"主线：{'、'.join(mains[:5]) if mains else '无明确主线'}")
    eng_ok = STATE_ENGINES.get(res["state"], [])
    L.append(f"当前状态「{res['state_cn']}」→ 明日可执行："
             f"{'、'.join(eng(e) for e in eng_ok) if eng_ok else '无（空仓）'}")
    cands = plan.get("candidates", [])
    if not cands:
        L.append("- 无符合条件的候选")
    run_now = [c for c in cands if c["engine"] in eng_ok]
    watch = [c for c in cands if c["engine"] not in eng_ok]
    for c in run_now[:6]:
        L.append(f"- **可执行** {c['code']} {c['name']}（{eng(c['engine'])}，{c['boards']}板，"
                 f"{c['industry']}）开盘 {c['gap_min']:+.1%}~{c['gap_max']:+.1%}")
    for c in watch[:4]:
        need = "主升" if c["engine"] == "E1" else "修复/主升"
        L.append(f"- 观察·需{need} {c['code']} {c['name']}（{eng(c['engine'])}，{c['boards']}板）")
    if plan.get("skipped"):
        L.append(f"- 另有 {len(plan['skipped'])} 只被过滤（题材/身位/封单/一字）")

    L.append("")
    L.append("**风控**")
    allow, mult, why = pf.circuit(acct, plan.get("for_date") or date)
    L.append(f"- {'禁止开仓：' + why if not allow else ('仓位系数 ' + format(mult, '.1f') + ('（' + why + '）' if why else ''))}")
    L.append(f"- 状态仓位上限 {STATE_CAP.get(res['state'], 0):.0%}，槽位 "
             f"{len(acct['positions'])}/{SLOTS}")
    L.append("")
    L.append("> 模拟盘，非投资建议。次日开盘成交假设，不含盘中冲击成本。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(date: str | None, do_push: bool, bootstrap: int, force: bool = False) -> int:
    date = dm.latest_trade_date(date)
    print(f"[info] 交易日 {date}")

    acct = pf.load()
    if acct.get("last_run") == date and not force:
        print(f"[info] {date} 已处理过，跳过（--force 可强制重跑）")
        return 0
    reg = rg.load()
    prev_plan = load_plan()

    reg = ensure_history(reg, date, bootstrap)
    # 今日开盘的买入只能依据「昨日收盘已知的状态」，不能用今日收盘状态（未来函数）
    past = [h for h in reg.get("history", []) if h["date"] < date and h.get("state")]
    state_prev = past[-1]["state"] if past else (reg.get("current") or "REPAIR")

    reg, res = rg.update(date, reg)
    print(f"[info] 状态 {res['state_cn']} E={res['e']:.1f}（开盘门控用昨日状态 {state_prev}）")

    settle_ev = pf.settle(acct, date)

    entry_log = acct.get("entry_log") or {}
    if acct.get("last_entry_date") == date:
        # 09:40 的 run_entry.py 已经执行过，这里只取回成交记录
        buys_today = entry_log.get(date, [])
        fills = []
        print(f"[info] 今日入场已于 09:40 处理：买入 {len(buys_today)} 笔")
    else:
        print(f"[info] 昨日计划 {prev_plan.get('date')} → 候选 "
              f"{len(prev_plan.get('candidates', []))} 只，门控状态 {state_prev}（兜底执行）")
        fills = execute_plan(acct, prev_plan, date, state_prev)
        buys_today = [x for x in fills if x.get("action") == "买入"]
        entry_log[date] = buys_today
        acct["entry_log"] = dict(sorted(entry_log.items())[-30:])
        acct["last_entry_date"] = date
    m = pf.mark(acct, date)

    nxt = dm.next_trade_date(date)
    plan = stg.build_plan(date, reg["history"], res["state"], int(res["factors"]["maxb"]))
    plan["for_date"] = nxt

    acct["last_run"] = date
    rg.save(reg)
    pf.save(acct)
    save_plan(plan)

    report = build_report(date, res, acct, plan, settle_ev, fills, m, buys_today)
    (REPORT_DIR / f"{dm.dash(date)}.md").write_text(report, encoding="utf-8")
    print(report)

    if do_push:
        title = f"[{REPORT_TITLE_PREFIX}] {dm.dash(date)[5:]} 收盘 净值 {m['equity'] / acct['initial']:.4f} {m['day_ret']:+.2%}"
        n = pusher.push(title, report)
        print(f"[info] push 成功 token 数 {n}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--bootstrap", type=int, default=BOOTSTRAP_DAYS)
    ap.add_argument("--force", action="store_true", help="强制重跑已处理过的日期")
    a = ap.parse_args()
    try:
        return run(a.date, not a.no_push, a.bootstrap, a.force)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not a.no_push:
            pusher.push(f"[{REPORT_TITLE_PREFIX}] 运行失败 {datetime.now():%m-%d %H:%M}",
                        "```\n" + tb[-1200:] + "\n```")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
