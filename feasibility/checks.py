#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可行性算术检验（不依赖任何市场数据，纯规则/参数层面的数学自检）

检验项：
  F3 保本胜率与盈亏比 —— 这套打法需要多高的胜率才能不亏
  F4 凯利仓位检验     —— 3:3:3 到底是"保守"还是"满凯利"
  F5 成本预算         —— 周转率 × 单笔摩擦 能吃掉多少年化
  F6 自由度预算       —— 参数个数 vs 可用样本，是否必然过拟合
  F7 状态机驻留时间   —— 市场状态会不会天天跳变

用法：
  python3 checks.py                    # 打印可读报告
  python3 checks.py --json out/x.json  # 同时落盘结构化结果
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

# ----------------------------------------------------------------------------
# 0. 交易成本结构（A 股，2026 年现行费率）
# ----------------------------------------------------------------------------
COST_PARTS = {
    "佣金(双边)": 0.00025 * 2,
    "印花税(卖出)": 0.00050,
    "过户费(双边)": 0.00001 * 2,
}
SLIP = {"低": 0.0005 * 2, "基准": 0.0010 * 2, "高": 0.0020 * 2}
COSTS = {k: round(sum(COST_PARTS.values()) + v, 5) for k, v in SLIP.items()}

# ----------------------------------------------------------------------------
# 1. 保本胜率 / 凯利
# ----------------------------------------------------------------------------

def breakeven_win_rate(tp: float, sl: float, cost: float) -> float:
    """净收益口径：赢时 +tp-cost，输时 -(sl+cost)。
    p(tp-c) = (1-p)(sl+c)  =>  p = (sl+c)/(tp+sl)
    注意分母不含 cost（cost 在两边等额抵消了一项），这是标准推导结果。
    """
    return (sl + cost) / (tp + sl)


def net_payoff_ratio(tp: float, sl: float, cost: float) -> float:
    return (tp - cost) / (sl + cost)


def kelly_fraction(tp: float, sl: float, cost: float, p: float) -> float:
    b = net_payoff_ratio(tp, sl, cost)
    return p - (1 - p) / b


def win_rate_for_fraction(tp: float, sl: float, cost: float, f: float) -> float:
    """给定目标仓位比例 f，反解所需胜率。f = p - (1-p)/b => p = (f*b+1)/(b+1)"""
    b = net_payoff_ratio(tp, sl, cost)
    return (f * b + 1) / (b + 1)


def expectancy(tp: float, sl: float, cost: float, p: float) -> float:
    return p * (tp - cost) - (1 - p) * (sl + cost)


# ----------------------------------------------------------------------------
# 2. 成本预算
# ----------------------------------------------------------------------------

def cost_drag(trades_per_year: int, weight_per_slot: float, cost: float) -> float:
    """年化成本拖累 = 笔数 × 单笔权重 × 单笔往返摩擦"""
    return trades_per_year * weight_per_slot * cost


# ----------------------------------------------------------------------------
# 3. 参数自由度预算
# ----------------------------------------------------------------------------
# (层, 名称, 默认值, 是否允许调参)
PARAMS = [
    ("L1", "E-score 七因子权重", "等权", False),
    ("L1", "状态切分点 E_ice", 20, True),
    ("L1", "状态切分点 E_repair", 45, True),
    ("L1", "状态切分点 E_expand", 65, True),
    ("L1", "迟滞带宽", 5, False),
    ("L1", "最小驻留天数", 2, False),
    ("L2", "主线热度回看窗口", 10, False),
    ("L2", "主线热度分位阈值", 0.70, True),
    ("L2", "新题材爆发阈值", 8, True),
    ("L3", "低价阈值(元)", 10.0, True),
    ("L3", "位置分位阈值", 0.60, True),
    ("L3", "禁买身位下限", 3, False),
    ("L4", "E1 高开阈值", 0.03, True),
    ("L4", "E1 低开阈值", -0.05, True),
    ("L4", "E1 竞价量比", 1.2, True),
    ("L4", "E2 低开阈值", -0.03, True),
    ("L4", "E2 确认窗口(分钟)", 30, False),
    ("L4", "E3 高开下沿", 0.02, True),
    ("L4", "E3 高开上沿", 0.06, False),
    ("L4", "E3 量比", 1.2, False),
    ("L5", "换手率高位分位", 0.70, True),
    ("L5", "缩量量比阈值", 0.80, True),
    ("L5", "MA 周期", 5, False),
    ("L6", "E1 止盈", 0.08, True),
    ("L6", "E1 止损", 0.05, True),
    ("L6", "E1 最长持有", 3, False),
    ("L6", "E2 止盈", 0.05, True),
    ("L6", "E2 止损", 0.04, True),
    ("L6", "E2 最长持有", 2, False),
    ("L6", "E3 止盈", 0.06, True),
    ("L6", "E3 止损", 0.04, True),
    ("L6", "E3 最长持有", 2, False),
    ("L6", "加速态减半阈值", 0.05, False),
    ("L7", "槽位数", 3, False),
    ("L7", "单票权重", 0.333, False),
    ("L7", "同题材上限", 2, True),
    ("L7", "日亏熔断", -0.03, True),
]

# ----------------------------------------------------------------------------
# 4. 状态机
# ----------------------------------------------------------------------------
STATES = ["ICE", "REPAIR", "EXPAND", "DIVERGE", "FADE"]

P_MC = np.array([
    [0.55, 0.30, 0.05, 0.05, 0.05],
    [0.10, 0.35, 0.40, 0.10, 0.05],
    [0.03, 0.07, 0.55, 0.25, 0.10],
    [0.05, 0.15, 0.20, 0.45, 0.15],
    [0.15, 0.15, 0.05, 0.20, 0.45],
])

# 建议矩阵：真实 A 股情绪周期以"周"为单位，驻留应 ≥3 天
P_REC = np.array([
    [0.80, 0.15, 0.02, 0.01, 0.02],
    [0.08, 0.72, 0.15, 0.03, 0.02],
    [0.01, 0.04, 0.85, 0.08, 0.02],
    [0.02, 0.08, 0.10, 0.70, 0.10],
    [0.10, 0.10, 0.02, 0.10, 0.68],
])


def stationary(P: np.ndarray) -> np.ndarray:
    n = P.shape[0]
    A = np.vstack([P.T - np.eye(n), np.ones(n)])
    b = np.zeros(n + 1)
    b[-1] = 1.0
    pi, *_ = np.linalg.lstsq(A, b, rcond=None)
    return pi / pi.sum()


# ----------------------------------------------------------------------------
# 5. 设计自检清单
# ----------------------------------------------------------------------------
def scorecard(original: bool) -> list[tuple[str, bool, str]]:
    """对"原文参数组"与"v0.1 设计"跑同一套可行性准则。"""
    rows = []
    if original:
        tp, sl, cost, p = 0.03, 0.05, 0.003, 0.70
        be = breakeven_win_rate(tp, sl, cost)
        kelly = kelly_fraction(tp, sl, cost, p)
        f_req = win_rate_for_fraction(tp, sl, cost, 0.333)
        rows += [
            ("C1 保本胜率 ≤ 55%", be <= 0.55, f"保本胜率 {be:.1%}（要求≥{be:.0%}才不亏）"),
            ("C2 3:3:3 不超配凯利", kelly >= 0.333 * 0.9, f"70%胜率下凯利 f*={kelly:.1%}；33%仓位需 p≥{f_req:.1%}"),
            ("C3 成本/毛利 ≤ 30%", False, "高频周转(约300笔/年) 下成本占毛利 >50%"),
            ("C4 参数 ≤ 25 个", False, "s 全量拆解共 54 个可调参数"),
            ("C5 状态驻留 ≥ 3 天", False, "未定义状态机，情绪靠主观判断"),
            ("C6 每日唯一主导引擎", False, "S6/S7/S8 三种买点混用，原文自陈会「左右打脸」"),
            ("C7 无不可成交假设", False, "S6 假设涨停价必成交，存在严重成交选择偏差"),
            ("C8 退出规则无冲突", False, "S16 快止盈 与 S9 拿住加速 直接冲突"),
            ("C9 跨题材分散", False, "3:3:3 未限制题材集中度，易伪分散"),
        ]
    else:
        tp, sl, cost, p = 0.08, 0.05, 0.003, 0.45
        be = breakeven_win_rate(tp, sl, cost)
        kelly = kelly_fraction(tp, sl, cost, p)
        f_req = win_rate_for_fraction(tp, sl, cost, 0.333)
        rows += [
            ("C1 保本胜率 ≤ 55%", be <= 0.55, f"E1 保本胜率 {be:.1%}（45%胜率即有正期望）"),
            ("C2 3:3:3 不超配凯利", kelly >= 0.333 * 0.9, f"45%胜率下 f*={kelly:.1%}，33%仓位需 p≥{f_req:.1%}"),
            ("C3 成本/毛利 ≤ 30%", False, "仍需把年交易笔数压到 ≤100 笔（见 F5）"),
            ("C4 参数 ≤ 25 个", True, "核心可调参数 22 个，其余冻结为默认值"),
            ("C5 状态驻留 ≥ 3 天", True, "建议转移矩阵下各状态驻留 ≥3.1 天"),
            ("C6 每日唯一主导引擎", True, "引擎由市场状态单向选择，互斥"),
            ("C7 无不可成交假设", True, "fill gate 先剔除一字/秒板，只做换手板或次日竞价"),
            ("C8 退出规则无冲突", True, "退出梯度按持仓状态分支，硬止损全局优先"),
            ("C9 跨题材分散", True, "同题材上限 2 只 + 状态仓位上限"),
        ]
    return rows


# ----------------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="out/arithmetic.json")
    args = ap.parse_args()

    out: dict = {}
    L: list[str] = []

    def p(s: str = "") -> None:
        L.append(s)
        print(s)

    p("=" * 78)
    p(" 可行性算术检验  —— 纯规则层面的数学自检，不依赖任何行情数据")
    p("=" * 78)

    # ---------------- F3 ----------------
    p("\n【F3】保本胜率：这种盈亏结构到底需要多高的胜率")
    p("-" * 78)
    p(f"{'止盈':>6}{'止损':>6}{'成本':>7}{'盈亏比':>8}{'保本胜率':>10}{'期望@45%':>11}{'期望@55%':>11}")
    rows = []
    for tp, sl in [(0.03, 0.05), (0.05, 0.05), (0.05, 0.04), (0.06, 0.04), (0.08, 0.05), (0.10, 0.05)]:
        be = breakeven_win_rate(tp, sl, COSTS["基准"])
        b = net_payoff_ratio(tp, sl, COSTS["基准"])
        e45 = expectancy(tp, sl, COSTS["基准"], 0.45)
        e55 = expectancy(tp, sl, COSTS["基准"], 0.55)
        p(f"{tp:>6.0%}{sl:>6.0%}{COSTS['基准']:>7.2%}{b:>8.2f}{be:>10.1%}{e45:>10.2%}{e55:>10.2%}")
        rows.append(dict(tp=tp, sl=sl, breakeven=be, payoff=b, e45=e45, e55=e55))
    out["breakeven"] = rows
    p("\n  读法：原文的「3%止盈 + 5%止损」需要 66%+ 胜率才保本；")
    p("        改成「8%止盈 + 5%止损」后保本线降到 41%，45% 胜率就有正期望。")

    p("\n  成本敏感性（8%/5% 结构下的保本胜率）：")
    for name, c in COSTS.items():
        p(f"    成本 {name:<3}({c:.2%})  保本胜率 {breakeven_win_rate(0.08, 0.05, c):.1%}")
    p("\n  成本敏感性（3%/5% 结构下的保本胜率）：")
    for name, c in COSTS.items():
        p(f"    成本 {name:<3}({c:.2%})  保本胜率 {breakeven_win_rate(0.03, 0.05, c):.1%}")

    p("\n  T+1 折损敏感性：名义止盈不等于实际到手（次日才能卖，跳空会截断）")
    for cap in (1.00, 0.90, 0.80):
        p(f"    名义止盈捕获率 {cap:.0%}：8%/5% 保本 {breakeven_win_rate(0.08*cap, 0.05, COSTS['基准']):.1%}"
          f"   |   3%/5% 保本 {breakeven_win_rate(0.03*cap, 0.05, COSTS['基准']):.1%}")
    p("    → 小止盈被 T+1 折损得最厉害：3% 目标实际只能拿到 2.4–2.7%，保本线被进一步推高。")

    # ---------------- F4 ----------------
    p("\n【F4】凯利检验：3:3:3 是「分散」还是「满仓凯利」？")
    p("-" * 78)
    p(f"{'止盈/止损':>12}{'保本胜率':>10}{'f*@45%':>10}{'f*@55%':>10}{'f*@70%':>10}{'33%仓位需胜率':>14}")
    krows = []
    for tp, sl in [(0.03, 0.05), (0.05, 0.05), (0.06, 0.04), (0.08, 0.05)]:
        k45 = kelly_fraction(tp, sl, COSTS["基准"], 0.45)
        k55 = kelly_fraction(tp, sl, COSTS["基准"], 0.55)
        k70 = kelly_fraction(tp, sl, COSTS["基准"], 0.70)
        fre = win_rate_for_fraction(tp, sl, COSTS["基准"], 1 / 3)
        p(f"{tp:>5.0%}/{sl:<6.0%}{breakeven_win_rate(tp, sl, COSTS['基准']):>10.1%}"
          f"{k45:>10.1%}{k55:>10.1%}{k70:>10.1%}{fre:>14.1%}")
        krows.append(dict(tp=tp, sl=sl, k45=k45, k55=k55, k70=k70, p_for_third=fre))
    out["kelly"] = krows
    p("\n  关键结论：原文「3%/5% + 70% 胜率」本身是自洽的，但它只支持 11% 的单笔仓位。")
    p("  3:3:3 的单票 33% 是它的 3 倍 —— 等价于要求胜率达到 77.5%。")
    p("  所以在作者自己的假设下，3:3:3 也是超配的：这不是分散，是满凯利的 3 倍。")
    k45_85 = kelly_fraction(0.08, 0.05, COSTS["基准"], 0.45)
    k55_85 = kelly_fraction(0.08, 0.05, COSTS["基准"], 0.55)
    p(f"  改用 8%/5% 结构：45% 胜率时 f*={k45_85:.1%}（半凯利 {k45_85/2:.1%}），"
      f"55% 胜率时 f*={k55_85:.1%}（半凯利 {k55_85/2:.1%}）。")
    p("  含义：仓位不该是「设定值」，应由实测胜率反解。v0.1 因此把 33% 设为上限、")
    p("        由 L1 状态与 L8 实测胜率共同缩放 —— 这是唯一未闭合的算术缺口。")

    # ---------------- F5 ----------------
    p("\n【F5】成本预算：周转率的隐形上限")
    p("-" * 78)
    p("  年化成本拖累 = 年笔数 × 单票权重(1/3) × 单笔往返摩擦(0.30%)")
    p(f"{'年交易笔数':>12}{'成本拖累':>12}{'占25%目标年化':>16}{'占40%目标年化':>16}")
    crows = []
    for n in [50, 80, 100, 150, 200, 300, 450]:
        d = cost_drag(n, 1 / 3, COSTS["基准"])
        p(f"{n:>12}{d:>12.1%}{d / 0.25:>16.0%}{d / 0.40:>16.0%}")
        crows.append(dict(n=n, drag=d))
    out["cost_drag"] = crows
    p("\n  若把成本控制在目标年化的 1/3 以内（目标 25%）：年笔数应 ≤ 83 笔。")
    p("  这直接否定了「高周转靠复利」的路径 —— 复利的前提是单笔边际足够厚。")

    # ---------------- F6 ----------------
    p("\n【F6】自由度预算：参数个数 vs 样本量")
    p("-" * 78)
    n_all = len(PARAMS)
    n_tunable = sum(1 for x in PARAMS if x[3])
    by_layer: dict[str, int] = {}
    for layer, _, _, tun in PARAMS:
        by_layer[layer] = by_layer.get(layer, 0) + (1 if tun else 0)
    p(f"  参数总数 {n_all}，其中可调 {n_tunable}，冻结 {n_all - n_tunable}")
    p("  分布：" + "  ".join(f"{k}={v}" for k, v in sorted(by_layer.items())))
    for ratio in (20, 30, 50):
        p(f"  按每参数 {ratio} 笔样本计，最少需要 {n_tunable * ratio} 笔交易；"
          f"按 150 笔/年计需 {n_tunable * ratio / 150:.1f} 年")
    out["dof"] = {"total": n_all, "tunable": n_tunable, "by_layer": by_layer}

    # ---------------- F7 ----------------
    p("\n【F7】状态机驻留时间：情绪状态会不会天天跳？")
    p("-" * 78)
    for name, P in [("当前(蒙特卡洛用)", P_MC), ("建议(更持久)", P_REC)]:
        pi = stationary(P)
        p(f"  {name}")
        p(f"    {'状态':<9}{'驻留天数':>10}{'稳态占比':>10}{'年出现天数':>12}")
        dwell = []
        for i, s in enumerate(STATES):
            d = 1 / (1 - P[i, i])
            p(f"    {s:<9}{d:>10.1f}{pi[i]:>10.1%}{pi[i] * 244:>12.0f}")
            dwell.append(d)
        p(f"    平均驻留 {np.mean(dwell):.1f} 天，EXPAND 占比 {pi[2]:.1%}")
        out.setdefault("regime", {})[name] = dict(
            dwell=[round(x, 2) for x in dwell],
            stationary=[round(float(x), 4) for x in pi],
        )
        p("")

    p("  警示：若平均驻留 < 3 天，而持仓周期 2–3 天，则「按状态选引擎」")
    p("        会在持仓期内失效 —— 等于没做状态管理。这是组合可行性的硬约束。")

    # ---------------- 自检清单 ----------------
    p("\n【自检清单】同一套准则，分别评「原文参数组」与「v0.1 设计」")
    p("-" * 78)
    for label, orig in [("原文参数组", True), ("v0.1 设计", False)]:
        p(f"\n  ── {label} ──")
        sc = scorecard(orig)
        for name, ok, note in sc:
            p(f"    [{'通过' if ok else '不通过'}] {name:<24} {note}")
        out.setdefault("scorecard", {})[label] = [
            dict(item=n, pass_=o, note=t) for n, o, t in sc
        ]
        passed = sum(1 for _, o, _ in sc if o)
        p(f"    → {passed}/{len(sc)} 项通过")

    p("\n" + "=" * 78)
    p(" 以上均为规则层面的算术检验。它不能证明策略赚钱，但能证伪：")
    p(" 若一个设计在算术上就不可能成立，就不必再花时间去回测。")
    p("=" * 78)

    jp = Path(args.json)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("out/arithmetic_report.txt").write_text("\n".join(L), encoding="utf-8")
    print(f"\n已写出: {jp} 与 out/arithmetic_report.txt")


if __name__ == "__main__":
    main()
