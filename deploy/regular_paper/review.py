#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推送计数 + 每 5 次收盘推送复盘。

约定：
  - 只把“收盘日报推送成功”计入复盘周期；09:15 盘前心跳不计入。
    否则一天两次推送，2.5 天就复盘一次，样本太少，只会把噪声当信号。
  - 复盘只做“是否调整参数”的判断，不自动改 YAML、不自动下单。
  - 少于 min_trades_for_param_change 笔已平仓样本时，默认结论是“不调整”。

文件：
  state/push_counter.json   推送计数与复盘历史
  reports/review_YYYY-MM-DD.md  复盘报告
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

import data as dm
import portfolio as pf
import push as pusher
import regime as rg
from config import (
    REPORT_DIR, SETUP_CN, STATE_CN, STATE_DIR, load_params,
)

COUNTER_FILE = "push_counter.json"


# ---------------------------------------------------------------------------
# 计数
# ---------------------------------------------------------------------------

def counter_path(state_dir) -> Path:
    return Path(state_dir) / COUNTER_FILE


def load_counter(state_dir) -> dict:
    p = counter_path(state_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "close_pushes": 0,
        "premarket_pushes": 0,
        "last_close_date": None,
        "last_premarket_date": None,
        "last_review_close_count": 0,
        "reviews": [],
        "history": [],
    }


def save_counter(c: dict, state_dir) -> None:
    p = counter_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def note_push(kind: str, date: str, params, state_dir=None, report_dir=None) -> dict:
    """
    在“推送成功”之后调用。返回计数状态。
    kind: close | premarket
    """
    state_dir = Path(state_dir or STATE_DIR)
    report_dir = Path(report_dir or REPORT_DIR)
    c = load_counter(state_dir)
    date = dm.norm_date(date)

    if kind == "close":
        if c.get("last_close_date") == date:
            return c
        c["close_pushes"] = int(c.get("close_pushes", 0)) + 1
        c["last_close_date"] = date
    elif kind == "premarket":
        if c.get("last_premarket_date") == date:
            return c
        c["premarket_pushes"] = int(c.get("premarket_pushes", 0)) + 1
        c["last_premarket_date"] = date
    else:
        raise ValueError(f"未知推送类型：{kind}")

    c.setdefault("history", []).append({"date": date, "kind": kind})
    c["history"] = c["history"][-300:]
    save_counter(c, state_dir)

    every = max(1, int(params.rt("review", "every_n_close_pushes", default=5)))
    enabled = bool(params.rt("review", "enabled", default=True))
    if (kind == "close" and enabled
            and c["close_pushes"] % every == 0
            and int(c.get("last_review_close_count", 0)) < c["close_pushes"]):
        try:
            result = run_review(params, state_dir=state_dir, report_dir=report_dir,
                                close_count=c["close_pushes"], do_push=True)
            c = load_counter(state_dir)
            c["last_review_close_count"] = c["close_pushes"]
            c.setdefault("reviews", []).append({
                "date": date,
                "close_pushes": c["close_pushes"],
                "report": str(result.get("path", "")),
                "pushed": bool(result.get("pushed", False)),
            })
            c["reviews"] = c["reviews"][-100:]
            save_counter(c, state_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 复盘生成失败：{e}")
            traceback.print_exc()
            c = load_counter(state_dir)
            c["last_review_close_count"] = c["close_pushes"]
            save_counter(c, state_dir)
    return c


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if unit == "pct":
        x = float(v) * 100
        return f"{x:.4f}%" if abs(x) < 0.01 else f"{x:.2f}%"
    if unit == "x":
        return f"{float(v):.2f}x"
    if unit == "weight":
        return f"{float(v):.0%}"
    if unit == "score":
        return f"{float(v):.0f}"
    if unit == "day":
        return f"{float(v):g} 日"
    if unit == "yuan":
        return f"{float(v):,.0f}"
    if unit == "count":
        return f"{float(v):g}"
    if unit == "frac":
        return f"{float(v):.0%}"
    return str(v)


def _bucket(trades: list[dict], key: str, default: str = "未知") -> list[tuple[str, list[dict]]]:
    d: dict[str, list[dict]] = {}
    for t in trades:
        k = str(t.get(key) or default)
        d.setdefault(k, []).append(t)
    return sorted(d.items(), key=lambda x: -len(x[1]))


def _stats_line(sub: list[dict]) -> str:
    n = len(sub)
    if n == 0:
        return "0 笔"
    w = sum(1 for t in sub if float(t.get("ret", 0)) > 0) / n
    avg = sum(float(t.get("ret", 0)) for t in sub) / n
    pnl = sum(float(t.get("pnl", 0)) for t in sub)
    return f"{n} 笔｜胜率 {w:.0%}｜平均 {avg:+.2%}｜累计 {pnl:+,.0f}"


# ---------------------------------------------------------------------------
# 复盘
# ---------------------------------------------------------------------------

def build_review(params, state_dir, report_dir, close_count: int,
                 manual: bool = False) -> tuple[str, dict]:
    state_dir = Path(state_dir)
    report_dir = Path(report_dir)
    acct = pf.load(params, state_dir / "account.json")
    reg = rg.load(state_dir / "regime.json")
    counter = load_counter(state_dir)
    plan = {}
    try:
        plan = json.loads((state_dir / "plan.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    s = pf.stats(acct)
    eq = pf.equity(acct)
    tr = acct.get("trades", [])
    n = len(tr)
    min_n = int(params.rt("review", "min_trades_for_param_change", default=30))
    every = max(1, int(params.rt("review", "every_n_close_pushes", default=5)))
    review_no = max(1, close_count // every)

    hist_dates = [h["date"] for h in acct.get("equity_history", [])]
    period = f"{hist_dates[0]} ~ {hist_dates[-1]}" if hist_dates else "尚无净值记录"
    state_counts = Counter(h.get("state", "?") for h in reg.get("history", []))

    # 结论
    if s["max_dd"] >= float(params.get("portfolio.dd_stop")):
        conclusion = ("**触发回撤停止线：先停用/降档并人工复核，不调参。**"
                      " 回撤期调参是最常见的自毁方式。")
        decision = "暂停/降档"
    elif n < min_n:
        conclusion = (f"**样本不足（已平仓 {n} 笔 < {min_n} 笔）：本期不调整参数。**"
                      " 只记录执行质量与状态分布；继续积累样本。")
        decision = "保持"
    elif s["win_rate"] < 0.40 and s["avg_ret"] < 0:
        conclusion = ("**样本已够但期望为负：优先降档或暂停该形态，而不是改阈值救它。**"
                      " 先判断是规则失效还是风格不适。")
        decision = "降档/暂停"
    elif s["max_dd"] >= float(params.get("portfolio.dd_halve")):
        conclusion = "**回撤已触发减半线：维持减半，检查仓位与止损，暂不调参。**"
        decision = "降仓"
    else:
        conclusion = ("**本期不调整参数。** 让规则继续跑，"
                      " 直到同一参数组有足够样本再讨论。")
        decision = "保持"

    L: list[str] = []
    if manual:
        L.append("## 常规量价模拟盘 · 手动复盘")
    else:
        L.append(f"## 常规量价模拟盘 · 第 {review_no} 次复盘")
    L.append("")
    if manual:
        trigger = (f"手动触发（收盘推送第 {close_count} 次；"
                   f"自动复盘每 {every} 次一次）")
    elif close_count > 0:
        trigger = f"收盘推送第 **{close_count}** 次（每 {every} 次复盘一次）"
    else:
        trigger = "手动复盘（尚无收盘推送计数）"
    L.append(f"**触发**：{trigger}")
    L.append(f"**参数**：{params.summary()}")
    L.append(f"**区间**：{period}｜已平仓 **{n}** 笔｜持仓 {len(acct.get('positions', []))} 只")
    L.append("")
    L.append("### 结论")
    L.append("")
    L.append(conclusion)
    L.append("")
    L.append(f"本期决策：**{decision}**。不自动修改 `config/regular_v1.yaml`。")
    L.append("")

    L.append("### 1. 绩效")
    L.append("")
    L.append(f"- 净值 **{eq:,.0f}**（累计 {s['total_ret']:+.2%}）")
    L.append(f"- 最大回撤 **{s['max_dd']:.2%}**｜胜率 {s['win_rate']:.0%}｜"
             f"盈亏比 {s['profit_factor']:.2f}｜平均单笔 {s['avg_ret']:+.2%}")
    L.append(f"- 平均盈利 {s['avg_win']:+.2%}｜平均亏损 {s['avg_loss']:+.2%}｜"
             f"未触发风险 {pf.open_risk(acct) / eq:.1%}" if eq else "")
    L.append("")

    L.append("### 2. 分形态 / 退出 / 入场状态")
    L.append("")
    if not tr:
        L.append("- 暂无已平仓交易。")
    else:
        L.append("**按形态**")
        for k, sub in _bucket(tr, "setup"):
            L.append(f"- {SETUP_CN.get(k, k)}：{_stats_line(sub)}")
        L.append("")
        L.append("**按退出原因**")
        for k, sub in _bucket(tr, "reason"):
            L.append(f"- {k}：{_stats_line(sub)}")
        L.append("")
        L.append("**按入场状态**")
        for k, sub in _bucket(tr, "entry_regime"):
            L.append(f"- {STATE_CN.get(k, k)}：{_stats_line(sub)}")
    L.append("")

    L.append("### 3. 市场状态与执行质量")
    L.append("")
    L.append("- 状态分布：" + ("、".join(
        f"{STATE_CN.get(k, k)} {v} 天" for k, v in state_counts.most_common()) or "尚无记录"))
    if plan:
        L.append(f"- 最新计划：股票池 {plan.get('universe', 0)} 只｜"
                 f"候选 {len(plan.get('candidates', []))} 只｜"
                 f"状态 {STATE_CN.get(plan.get('state'), plan.get('state'))}")
        skip = plan.get("skip_summary") or {}
        if skip:
            L.append("- 过滤摘要：" + "；".join(f"{k} {v}" for k, v in list(skip.items())[:6]))
    L.append(f"- 推送计数：收盘 {counter.get('close_pushes', 0)} 次｜"
             f"盘前 {counter.get('premarket_pushes', 0)} 次")
    L.append(f"- 当前仓位 {len(acct.get('positions', []))}/{params.get('portfolio.slots')}｜"
             f"现金 {acct.get('cash', 0):,.0f}")
    L.append("")

    L.append("### 4. 参数决策")
    L.append("")
    L.append("| 参数 | 当前值 | 本期决策 | 依据 |")
    L.append("|---|---:|---|---|")
    for key in params.meta.get("tuning_priority", []):
        spec = params.specs.get(key, {})
        val = params.get(key)
        reason = f"已平仓 {n} 笔"
        d = "保持"
        if n >= min_n:
            if key == "portfolio.risk_per_trade" and s["max_dd"] >= float(params.get("portfolio.dd_halve")):
                d, reason = "观察/降档", f"回撤 {s['max_dd']:.1%} 已触发减半"
            elif key == "entry.stop_loss" and tr:
                stop_share = sum(1 for t in tr if "止损" in str(t.get("reason", ""))) / n
                if stop_share >= 0.5:
                    d, reason = "观察", f"止损占比 {stop_share:.0%}，先查买点而不是放宽止损"
            elif key == "holding.time_stop_days" and tr:
                time_share = sum(1 for t in tr if "时间止损" in str(t.get("reason", ""))) / n
                if time_share >= 0.3:
                    d, reason = "观察", f"时间止损占比 {time_share:.0%}，先查入场质量"
            else:
                reason = f"样本 {n} 笔，未见需要调整的证据"
        L.append(f"| `{key}` | {_fmt(val, spec.get('unit'))} | {d} | {reason} |")
    L.append("")

    L.append("### 5. 下期要盯什么")
    L.append("")
    if n < min_n:
        L.append(f"- 先攒到 **{min_n} 笔已平仓样本**；在此之前不改任何阈值。")
        L.append("- 重点记录：止损占比、时间止损占比、放量急跌/冲高回落是否按规则触发。")
        L.append("- 检查数据质量：候选数是否为 0、是否大量“停牌/未更新”、状态是否长期不变。")
    else:
        L.append("- 若胜率 <40% 且平均单笔为负：降档/暂停，不调阈值。")
        L.append("- 若止损占比 >50%：先查买点是否太早，而不是放宽止损。")
        L.append("- 若时间止损占比 >30%：先查入场时机，而不是延长持有。")
    next_n = ((close_count // every) + 1) * every
    L.append(f"- 下一次复盘：收盘推送第 **{next_n}** 次。")
    L.append("")
    L.append("> 复盘只给建议，不自动改参数；参数变更需人工写入 "
             "`config/regular_v1.yaml` 与 `config/regular_CHANGELOG.md`。")
    return "\n".join(L), {
        "review_no": review_no,
        "close_count": close_count,
        "n_trades": n,
        "decision": decision,
        "conclusion": conclusion,
    }


def run_review(params, state_dir=None, report_dir=None, close_count: int | None = None,
               do_push: bool = True, manual: bool = False) -> dict:
    state_dir = Path(state_dir or STATE_DIR)
    report_dir = Path(report_dir or REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    if close_count is None:
        c = load_counter(state_dir)
        close_count = int(c.get("close_pushes", 0))
    text, meta = build_review(params, state_dir, report_dir, close_count, manual=manual)
    date = datetime.now().strftime("%Y%m%d")
    path = report_dir / f"review_{dm.dash(date)}.md"
    path.write_text(text, encoding="utf-8")

    pushed = False
    if do_push:
        tag = "手动复盘" if manual else f"第 {meta['review_no']} 次复盘"
        title = (f"[{params.rt('push', 'title_prefix', default='常规量价模拟盘')}] "
                 f"{tag}（收盘推送 #{close_count}）")
        n = pusher.push(title, text, params)
        pushed = n > 0
    return {"path": str(path), "text": text, "pushed": pushed, **meta}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force", action="store_true", help="不等 5 次，立即生成一次复盘")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-dir", default=None)
    ap.add_argument("--profile", default=None,
                    choices=["defensive", "balanced", "offensive"])
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    try:
        params = load_params(a.config, a.profile)
        c = load_counter(a.state_dir or STATE_DIR)
        close_count = int(c.get("close_pushes", 0))
        if not a.force and close_count == 0:
            print("[info] 还没有收盘推送，跳过复盘")
            return 0
        every = max(1, int(params.rt("review", "every_n_close_pushes", default=5)))
        manual = bool(a.force and close_count % every != 0)
        res = run_review(params, a.state_dir, a.report_dir, close_count,
                         do_push=not a.no_push, manual=manual)
        print(res["text"])
        print(f"[info] 复盘写入 {res['path']}，推送 {res['pushed']}")
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
