#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scenarios.py —— 在 montecarlo.py 之上做多情景对比

回答三个问题：
  Q1 不守状态门控（什么票都想做）会比守门控差多少？
  Q2 这套仓位结构需要的"单笔最低边际"是多少？（保本 edge）
  Q3 周转率、摩擦、槽位数分别把结果推向哪边？

用法：
  python3 scenarios.py --n-paths 800 --days 500
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import montecarlo as mc


# ---------------------------------------------------------------------------
# 配置构造
# ---------------------------------------------------------------------------

BASE_TABLE = dict(mc.SIGNAL_TABLE)   # 原始表（含负边际单元）


def apply_table(strict: bool) -> None:
    """strict=True 时，关闭所有 mu<=0 的（状态×引擎）单元 —— 即"守纪律"配置。"""
    for key, (allow, mu, lam) in BASE_TABLE.items():
        if strict and mu <= 0.0:
            mc.SIGNAL_TABLE[key] = (0, mu, 0.0)
        else:
            mc.SIGNAL_TABLE[key] = (allow, mu, lam)


def make_args(edge_scale, cost, lambda_scale, slots, n_paths, days, seed):
    return SimpleNamespace(
        n_paths=n_paths,
        days=days,
        seed=seed,
        edge_scale=edge_scale,
        cost=cost,
        lambda_scale=lambda_scale,
        slots=slots,
        barrier_mode="prob",
    )


def run_once(strict, edge_scale, cost, lambda_scale, slots, n_paths, days, seed):
    apply_table(strict)
    mc.N_SLOTS = slots
    payload = mc.run_monte_carlo(
        make_args(edge_scale, cost, lambda_scale, slots, n_paths, days, seed)
    )
    s = payload["summary"]
    p = payload["probabilities"]
    return {
        "final_mean": s["final_equity"]["mean"],
        "final_median": s["final_equity"]["median"],
        "final_p5": s["final_equity"]["p5"],
        "final_p95": s["final_equity"]["p95"],
        "ann_median": s["annualized"]["median"],
        "mdd_mean": s["max_drawdown"]["mean"],
        "mdd_p95": s["max_drawdown"]["p95"],
        "trades": s["n_trades"]["mean"],
        "win_rate": s["win_rate"]["mean"],
        "avg_pnl": s["avg_pnl"]["mean"],
        "avg_hold": s["avg_hold"]["mean"],
        "cost_drag": s["cost_drag_pct_of_capital"]["mean"],
        "p_final_lt_1": p["p_final_lt_1"],
        "p_dd_gt_30": p["p_dd_gt_30"],
        "exit": payload["exit_reason_share"],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-paths", type=int, default=800)
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="out/scenarios.json")
    args = ap.parse_args()

    N, D, S = args.n_paths, args.days, args.seed
    results: list[dict] = []
    lines: list[str] = []

    def p(s: str = "") -> None:
        lines.append(s)
        print(s)

    def add(tag, group, **kw):
        r = run_once(**kw, n_paths=N, days=D, seed=S)
        r.update(tag=tag, group=group)
        results.append(r)
        return r

    p("=" * 92)
    p(" 多情景可行性对比（合成路径，非回测）")
    p(f" n_paths={N}  days={D}  成本基准=0.30%  T+1 5状态马尔可夫")
    p("=" * 92)

    # ---------------- Q1 纪律 ----------------
    p("\n【Q1】违反状态门控的代价")
    p("-" * 92)
    p(f"{'配置':<26}{'期末中位':>10}{'年化中位':>10}{'笔数':>8}{'胜率':>8}{'单笔净':>9}{'P(亏)':>8}{'回撤':>8}")
    for tag, strict in [("无纪律（所有单元都做）", False), ("守纪律（只做正边际单元）", True)]:
        r = add(tag, "Q1", strict=strict, edge_scale=1.0, cost=0.003,
                lambda_scale=1.0, slots=3)
        p(f"{tag:<26}{r['final_median']:>10.3f}{r['ann_median']:>10.1%}"
          f"{r['trades']:>8.0f}{r['win_rate']:>8.1%}{r['avg_pnl']:>9.2%}"
          f"{r['p_final_lt_1']:>8.1%}{r['mdd_mean']:>8.1%}")

    # ---------------- Q2 保本边际 ----------------
    p("\n【Q2】守纪律配置下，需要多大的单笔边际才能保本")
    p("-" * 92)
    p(f"{'edge_scale':<12}{'期末中位':>10}{'年化中位':>10}{'笔数':>8}{'单笔净':>9}{'P(亏)':>8}{'回撤均值':>10}{'回撤p95':>10}")
    grid = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]
    be_row = None
    prev = None
    for es in grid:
        r = add(f"edge={es}", "Q2", strict=True, edge_scale=es, cost=0.003,
                lambda_scale=1.0, slots=3)
        p(f"{es:<12.2f}{r['final_median']:>10.3f}{r['ann_median']:>10.1%}"
          f"{r['trades']:>8.0f}{r['avg_pnl']:>9.2%}{r['p_final_lt_1']:>8.1%}"
          f"{r['mdd_mean']:>10.1%}{r['mdd_p95']:>10.1%}")
        if prev is not None and prev[1] < 1.0 <= r["final_median"]:
            be_row = (prev, (es, r["final_median"], r["avg_pnl"]))
        prev = (es, r["final_median"], r["avg_pnl"])

    if be_row:
        (e0, f0, p0), (e1, f1, p1) = be_row
        w = (1.0 - f0) / (f1 - f0) if f1 != f0 else 0.5
        es_be = e0 + w * (e1 - e0)
        pnl_be = p0 + w * (p1 - p0)
        p(f"\n  → 保本所需 edge_scale ≈ {es_be:.2f}，对应单笔净收益 ≈ {pnl_be:+.2%}")
        p(f"    换算：单笔毛收益需 ≈ {pnl_be + 0.003:+.2%}（净边际 + 0.30% 摩擦）")
        results.append(dict(tag="breakeven", group="Q2", edge_scale=es_be,
                            avg_pnl=pnl_be))

    # ---------------- Q3 周转 / 摩擦 / 槽位 ----------------
    p("\n【Q3a】周转率（信号过滤强度）")
    p("-" * 92)
    p(f"{'lambda_scale':<14}{'笔数':>8}{'成本拖累':>10}{'期末中位':>10}{'年化中位':>10}{'单笔净':>9}{'P(亏)':>8}")
    for ls in [0.2, 0.4, 0.7, 1.0]:
        r = add(f"lambda={ls}", "Q3a", strict=True, edge_scale=1.0, cost=0.003,
                lambda_scale=ls, slots=3)
        p(f"{ls:<14.2f}{r['trades']:>8.0f}{r['cost_drag']:>10.1%}"
          f"{r['final_median']:>10.3f}{r['ann_median']:>10.1%}"
          f"{r['avg_pnl']:>9.2%}{r['p_final_lt_1']:>8.1%}")

    p("\n【Q3b】单笔摩擦（滑点假设）")
    p("-" * 92)
    p(f"{'cost':<10}{'笔数':>8}{'成本拖累':>10}{'期末中位':>10}{'年化中位':>10}{'单笔净':>9}{'P(亏)':>8}")
    for c in [0.0015, 0.0030, 0.0050]:
        r = add(f"cost={c}", "Q3b", strict=True, edge_scale=1.0, cost=c,
                lambda_scale=1.0, slots=3)
        p(f"{c:<10.4f}{r['trades']:>8.0f}{r['cost_drag']:>10.1%}"
          f"{r['final_median']:>10.3f}{r['ann_median']:>10.1%}"
          f"{r['avg_pnl']:>9.2%}{r['p_final_lt_1']:>8.1%}")

    p("\n【Q3c】槽位数（集中度）")
    p("-" * 92)
    p(f"{'slots':<8}{'笔数':>8}{'期末中位':>10}{'年化中位':>10}{'回撤均值':>10}{'回撤p95':>10}{'P(回撤>30%)':>13}")
    for sl in [3, 4, 5]:
        r = add(f"slots={sl}", "Q3c", strict=True, edge_scale=1.0, cost=0.003,
                lambda_scale=1.0, slots=sl)
        p(f"{sl:<8d}{r['trades']:>8.0f}{r['final_median']:>10.3f}"
          f"{r['ann_median']:>10.1%}{r['mdd_mean']:>10.1%}{r['mdd_p95']:>10.1%}"
          f"{r['p_dd_gt_30']:>13.1%}")

    # ---------------- Q4 低周转下的保本边际 ----------------
    p("\n【Q4】把周转压到设计目标（约 80-100 笔/年）后，保本边际降到多少")
    p("-" * 92)
    p(f"{'edge_scale':<12}{'笔数':>8}{'成本拖累':>10}{'期末中位':>10}{'年化中位':>10}{'单笔净':>9}{'P(亏)':>8}")
    q4 = []
    for es in [0.5, 1.0, 1.3, 1.6, 2.0]:
        r = add(f"lowlambda-edge={es}", "Q4", strict=True, edge_scale=es,
                cost=0.003, lambda_scale=0.19, slots=3)
        q4.append((es, r))
        p(f"{es:<12.2f}{r['trades']:>8.0f}{r['cost_drag']:>10.1%}"
          f"{r['final_median']:>10.3f}{r['ann_median']:>10.1%}"
          f"{r['avg_pnl']:>9.2%}{r['p_final_lt_1']:>8.1%}")
    for (e0, r0), (e1, r1) in zip(q4, q4[1:]):
        if r0["final_median"] < 1.0 <= r1["final_median"]:
            w = (1.0 - r0["final_median"]) / (r1["final_median"] - r0["final_median"])
            es_be = e0 + w * (e1 - e0)
            pnl_be = r0["avg_pnl"] + w * (r1["avg_pnl"] - r0["avg_pnl"])
            p(f"\n  → 低周转配置保本所需 edge_scale ≈ {es_be:.2f}，"
              f"单笔净收益 ≈ {pnl_be:+.2%}（毛 ≈ {pnl_be + 0.003:+.2%}）")
            results.append(dict(tag="breakeven_lowturnover", group="Q4",
                                edge_scale=es_be, avg_pnl=pnl_be,
                                trades=r0["trades"]))
            break

    # ---------------- 反推：单笔毛边际 ----------------
    p("\n【综合】把 MC 结果翻译成「每笔需要赚多少」")
    p("-" * 92)
    r = [x for x in results if x["group"] == "Q2" and x["tag"] == "edge=1.0"][0]
    mult = 1.0 / (r["avg_pnl"] + 0.003) if (r["avg_pnl"] + 0.003) > 0 else float("nan")
    p(f"  基准假设下单笔净收益 {r['avg_pnl']:+.2%}，扣除 0.30% 摩擦后毛收益 "
      f"{r['avg_pnl'] + 0.003:+.2%}")
    p(f"  年交易 {r['trades']:.0f} 笔，平均持有 {r['avg_hold']:.2f} 天")
    p(f"  组合层面成本拖累 {r['cost_drag']:.1%}（已按 1/3 权重折算）")
    if r["avg_pnl"] < 0:
        p("  注意：基准假设本身是负期望 —— 说明该假设下的边际撑不住当前周转。")

    jp = Path(args.out)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("out/scenarios_report.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已写出 {jp} 与 out/scenarios_report.txt")


if __name__ == "__main__":
    main()
