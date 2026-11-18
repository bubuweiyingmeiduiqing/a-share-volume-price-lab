#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
montecarlo.py —— 系统组合可行性压力测试（合成路径蒙特卡洛）

这不是回测。
本脚本不读取、不下载、也不重采样任何真实市场数据；所有市场状态转移、
信号到达、题材分配、单笔价格路径和收益分布都是显式写死的假设。它只用于
在给定假设下检验一套 3 槽位、T+1 的 A 股涨停接力交易组合是否数学上可行，
并通过 --edge-scale 与 --cost 做可行性/灵敏度分析。任何输出都不构成投资建议。

运行示例：
    python3 montecarlo.py --n-paths 2000 --days 500 --seed 42 \\
        --edge-scale 1.0 --cost 0.003 --out out/mc.json
"""

import argparse
import json
import math
import os
import time

import numpy as np


# =============================================================================
# 模型常量（对应需求第 1、2、3 节）
# =============================================================================

# 市场状态顺序：冰点 / 修复 / 主升 / 分歧 / 退潮
REGIMES = ["ICE", "REPAIR", "EXPAND", "DIVERGE", "FADE"]

# 三个交易引擎：E1=接力/打板，E2=低吸，E3=首板补涨
ENGINES = ["E1", "E2", "E3"]

# 每日状态转移矩阵。行 = 今日状态，列 = 明日状态，顺序与 REGIMES 一致。
# 例如第一行 [0.55, 0.30, 0.05, 0.05, 0.05] 表示：
# 今日 ICE，明日仍为 ICE 的概率 0.55，转 REPAIR 0.30，其余各 0.05。
# 该矩阵为硬编码假设，不是历史统计结果。
TRANSITION = np.array(
    [
        [0.55, 0.30, 0.05, 0.05, 0.05],  # ICE
        [0.10, 0.35, 0.40, 0.10, 0.05],  # REPAIR
        [0.03, 0.07, 0.55, 0.25, 0.10],  # EXPAND
        [0.05, 0.15, 0.20, 0.45, 0.15],  # DIVERGE
        [0.15, 0.15, 0.05, 0.20, 0.45],  # FADE
    ],
    dtype=np.float64,
)

# 每个路径的初始状态分布，五个状态等概率。
INITIAL_DISTRIBUTION = np.array([0.2, 0.2, 0.2, 0.2, 0.2], dtype=np.float64)

# “状态 × 引擎”条件参数表：(allow, mu, lambda)
#   allow  : 1=允许开仓，0=不允许
#   mu     : 该笔交易在持有期内的单位漂移基数，实际使用 mu * edge_scale
#   lambda : 当日该引擎的候选信号数（泊松均值）
# 说明：E1 只在主升期开仓、分歧期为负边际；E2 主要在冰点/修复期；
#       E3 在修复/主升期补涨；退潮 FADE 全部停止。
SIGNAL_TABLE = {
    ("ICE", "E1"): (0, 0.000, 0.0),
    ("ICE", "E2"): (1, -0.002, 0.8),
    ("ICE", "E3"): (0, 0.000, 0.0),
    ("REPAIR", "E1"): (0, 0.000, 0.0),
    ("REPAIR", "E2"): (1, 0.006, 1.5),
    ("REPAIR", "E3"): (1, 0.004, 1.2),
    ("EXPAND", "E1"): (1, 0.012, 2.5),
    ("EXPAND", "E2"): (0, 0.000, 0.0),
    ("EXPAND", "E3"): (1, 0.005, 1.5),
    ("DIVERGE", "E1"): (1, -0.004, 1.5),
    ("DIVERGE", "E2"): (1, 0.002, 1.0),
    ("DIVERGE", "E3"): (0, 0.000, 0.0),
    ("FADE", "E1"): (0, 0.000, 0.0),
    ("FADE", "E2"): (0, 0.000, 0.0),
    ("FADE", "E3"): (0, 0.000, 0.0),
}

# 单笔交易的出场参数：止盈、止损、最长持有交易日数。
# 开仓价固定记为 1.0；止损/止盈价也固定以 1.0 为基准，不做移动止损。
EXIT_PARAMS = {
    "E1": {"tp": 0.08, "sl": -0.05, "max_hold": 3},
    "E2": {"tp": 0.05, "sl": -0.04, "max_hold": 2},
    "E3": {"tp": 0.06, "sl": -0.04, "max_hold": 2},
}

# 组合参数
N_SLOTS = 3                     # 最多同时持仓 3 个槽位
N_THEMES = 6                    # 题材编号 0..5
THEME_CAP = 2                   # 同一题材当前持仓 + 当日已开仓 <= 2
CIRCUIT_BREAKER = -0.03         # 组合净值单日跌幅 < -3%，次日禁止开新仓

# 单笔价格路径参数（对应第 3 节）
GAP_SIGMA = 0.02                # 开盘跳空标准差
INTRADAY_VOL = 0.045            # 日内对数收益的随机项系数
INITIAL_CAPITAL = 1.0


# =============================================================================
# 持仓对象
# =============================================================================

class Position:
    """一笔尚未平仓的交易。开仓价固定为 1.0，notional 为投入资金。"""

    __slots__ = (
        "engine",
        "entry_regime",
        "theme",
        "notional",
        "entry_day",
        "last_price",
    )

    def __init__(self, engine, entry_regime, theme, notional, entry_day):
        self.engine = engine
        self.entry_regime = entry_regime
        self.theme = theme
        self.notional = float(notional)
        self.entry_day = int(entry_day)
        # 开仓当日按 1.0 盯市；后续每日更新为当日收盘价。
        self.last_price = 1.0


# =============================================================================
# 工具函数
# =============================================================================

def summarize(values):
    """对一条指标的所有路径值，输出 mean/median/p5/p25/p75/p95。"""
    arr = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p5": float(np.percentile(arr, 5)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p95": float(np.percentile(arr, 95)),
    }


def max_drawdown_and_duration(equity):
    """
    基于每日盯市净值计算：
    - 最大回撤（正数，例如 0.25 表示 25%）
    - 最长连续“低于前高”的天数（未恢复则算到路径结束）
    """
    eq = np.asarray(equity, dtype=np.float64)
    running_peak = np.maximum.accumulate(eq)
    drawdowns = (running_peak - eq) / running_peak
    max_dd = float(np.max(drawdowns))

    # 最长水下持续时间：净值低于此前峰值时累计，创新高时清零。
    peak = eq[0]
    current_duration = 0
    max_duration = 0
    for value in eq:
        if value >= peak:
            peak = value
            current_duration = 0
        else:
            current_duration += 1
            if current_duration > max_duration:
                max_duration = current_duration
    return max_dd, int(max_duration)


def build_signal_table_config():
    """把硬编码参数整理成可写入 JSON 的字典。"""
    table = {}
    for regime in REGIMES:
        table[regime] = {}
        for engine in ENGINES:
            allow, mu, lam = SIGNAL_TABLE[(regime, engine)]
            table[regime][engine] = {
                "allow": bool(allow),
                "mu": float(mu),
                "lambda": float(lam),
            }
    return table


# =============================================================================
# 单条路径模拟（对应第 1～4 节）
# =============================================================================

def simulate_path(rng, days, edge_scale, cost, lambda_scale=1.0, barrier_mode="prob"):
    """
    模拟一条组合路径，返回：
    - equity_curve: 每个交易日结束后的组合净值，长度 days
    - trades: 每笔交易元组 (engine, 入场regime, 持有天数, 净收益, 出场原因)
    """
    # 初始市场状态按给定分布抽样；之后每日按转移矩阵演化。
    state = int(rng.choice(len(REGIMES), p=INITIAL_DISTRIBUTION))

    cash = INITIAL_CAPITAL
    positions = []
    equity_curve = []
    trades = []

    prev_equity = INITIAL_CAPITAL
    ban_new_entries = False

    for day in range(days):
        regime = REGIMES[state]

        # ------------------------------------------------------------------
        # 第 4 节：先结算已有持仓的当日盈亏，释放已平仓槽位。
        # T+1 体现在：当日新开仓位 entry_day=day，下一交易日 d=1 才检查出场。
        # ------------------------------------------------------------------
        still_held = []
        for pos in positions:
            d = day - pos.entry_day  # 1 = 开仓后的第一个交易日
            exit_params = EXIT_PARAMS[pos.engine]
            tp = float(exit_params["tp"])
            sl = float(exit_params["sl"])
            max_hold = int(exit_params["max_hold"])

            # 该笔交易入场时的 regime 决定其漂移假设，并乘以 edge_scale。
            _, mu_base, _ = SIGNAL_TABLE[(pos.entry_regime, pos.engine)]
            mu = mu_base * edge_scale

            # 第 3 节：跳空开盘、日内对数收益、近似高低点。
            gap = float(rng.normal(0.0, GAP_SIGMA))
            open_price = pos.last_price * (1.0 + gap)
            eps = float(rng.normal(0.0, 1.0))
            log_return = mu / max_hold + INTRADAY_VOL * eps
            close_price = open_price * math.exp(log_return)
            high_price = max(open_price, close_price) * (1.0 + 0.5 * abs(log_return))
            low_price = min(open_price, close_price) * (1.0 - 0.5 * abs(log_return))

            # 止损/止盈固定按开仓价 1.0 计算。
            stop_price = 1.0 + sl
            take_profit_price = 1.0 + tp

            exit_price = None
            exit_reason = None

            # 第 3 节出场顺序：跳空止损 -> 盘中止损 -> 盘中止盈 -> 到期。
            # 修正：同一根日 K 同时触及止损与止盈时，日线数据无法知道先后。
            # 若一律按止损处理会系统性低估（实测每笔约 -0.29%），
            # 故默认用布朗桥的首次触达概率决定：更近的一侧更先被触及。
            hit_stop = (open_price <= stop_price) or (low_price <= stop_price)
            hit_tp = high_price >= take_profit_price
            if open_price <= stop_price:
                exit_price = open_price
                exit_reason = "stop_gap"
            elif hit_stop and hit_tp and barrier_mode == "prob":
                d_sl = max(open_price - stop_price, 1e-9)
                d_tp = max(take_profit_price - open_price, 1e-9)
                p_stop = d_tp / (d_sl + d_tp)
                if rng.random() < p_stop:
                    exit_price = stop_price
                    exit_reason = "stop"
                else:
                    exit_price = take_profit_price
                    exit_reason = "take_profit"
            elif hit_stop:
                exit_price = stop_price
                exit_reason = "stop"
            elif hit_tp:
                exit_price = take_profit_price
                exit_reason = "take_profit"
            elif d >= max_hold:
                exit_price = close_price
                exit_reason = "time"

            if exit_price is not None:
                # 单笔净收益 = 毛收益 - 一次性 cost；成本在平仓时从资金中扣除。
                net_pnl = exit_price - 1.0 - cost
                cash += pos.notional * (exit_price - cost)
                trades.append((pos.engine, pos.entry_regime, d, net_pnl, exit_reason))
            else:
                # 继续持有，按当日收盘价盯市。
                pos.last_price = close_price
                still_held.append(pos)

        positions = still_held

        # ------------------------------------------------------------------
        # 第 4 节：先得到结算后的组合净值，再在剩余槽位内开新仓。
        # 每仓使用当前净值的 1/3；不足 3 仓时剩余部分为现金（日收益 0）。
        # ------------------------------------------------------------------
        equity_before_entries = cash + sum(
            pos.notional * pos.last_price for pos in positions
        )

        if not ban_new_entries and len(positions) < N_SLOTS:
            # 第 4 节：按当日 regime 各引擎的 lambda 生成候选。
            candidates = []
            for engine in ENGINES:
                allow, _, lam = SIGNAL_TABLE[(regime, engine)]
                if not allow:
                    continue
                n_candidates = int(rng.poisson(lam * lambda_scale))
                if n_candidates > 0:
                    # 每个候选均匀随机分配题材 0..5。
                    themes = rng.integers(0, N_THEMES, size=n_candidates)
                    for theme in themes:
                        candidates.append((engine, int(theme)))

            if candidates:
                # 同一题材当前持仓 + 当日已开仓的计数。
                theme_usage = [0] * N_THEMES
                for pos in positions:
                    theme_usage[pos.theme] += 1

                # 候选数多于空槽位时，随机顺序处理，槽位满后的候选自动丢弃；
                # 题材超限的候选也按规则丢弃。
                order = rng.permutation(len(candidates))
                notional = equity_before_entries / N_SLOTS
                for idx in order:
                    if len(positions) >= N_SLOTS:
                        break
                    engine, theme = candidates[int(idx)]
                    if theme_usage[theme] >= THEME_CAP:
                        continue
                    # 资金约束（正常仓位管理下不会触发，保留以防数值异常）。
                    if notional <= 0.0 or cash + 1e-12 < notional:
                        continue
                    positions.append(
                        Position(engine, regime, theme, notional, day)
                    )
                    cash -= notional
                    theme_usage[theme] += 1

        # 当日收盘后的组合净值（新开仓按成本价 1.0 计入，因此开仓本身不产生盈亏）。
        equity_end = cash + sum(pos.notional * pos.last_price for pos in positions)
        equity_curve.append(float(equity_end))

        # 第 4 节熔断：当日组合净值日收益 < -3%，则次日禁止开新仓。
        daily_return = equity_end / prev_equity - 1.0 if prev_equity > 0.0 else 0.0
        ban_new_entries = daily_return < CIRCUIT_BREAKER
        prev_equity = equity_end

        # 第 1 节：交易日结束后做状态转移。
        state = int(rng.choice(len(REGIMES), p=TRANSITION[state]))

    return equity_curve, trades


# =============================================================================
# 汇总统计与输出（对应第 5、6 节）
# =============================================================================

def run_monte_carlo(args):
    """运行全部路径并组装最终 JSON 数据结构。"""
    rng = np.random.default_rng(args.seed)

    # 每条路径的指标容器
    final_equities = []
    annualizeds = []
    max_drawdowns = []
    max_dd_durations = []
    n_trades_list = []
    win_rates = []
    avg_holds = []
    avg_pnls = []
    cost_drags = []

    # 全路径出场原因计数与分状态交易统计
    exit_reason_counts = {
        "take_profit": 0,
        "stop": 0,
        "stop_gap": 0,
        "time": 0,
    }
    regime_n = {regime: 0 for regime in REGIMES}
    regime_wins = {regime: 0 for regime in REGIMES}
    regime_pnl_sum = {regime: 0.0 for regime in REGIMES}

    equity_curves_sample = []

    for path_idx in range(args.n_paths):
        equity_curve, trades = simulate_path(
            rng=rng,
            days=args.days,
            edge_scale=args.edge_scale,
            cost=args.cost,
            lambda_scale=getattr(args, "lambda_scale", 1.0),
            barrier_mode=getattr(args, "barrier_mode", "prob"),
        )

        final_equity = float(equity_curve[-1])
        if not math.isfinite(final_equity) or final_equity <= 0.0:
            raise RuntimeError(f"路径 {path_idx} 出现非法净值: {final_equity!r}")

        # 年化：按 250 个交易日/年折算。
        annualized = final_equity ** (250.0 / args.days) - 1.0
        max_dd, max_dd_duration = max_drawdown_and_duration(equity_curve)

        n_trades = len(trades)
        if n_trades > 0:
            wins = sum(1 for trade in trades if trade[3] > 0.0)
            win_rate = wins / n_trades
            avg_hold = sum(trade[2] for trade in trades) / n_trades
            avg_pnl = sum(trade[3] for trade in trades) / n_trades
        else:
            win_rate = 0.0
            avg_hold = 0.0
            avg_pnl = 0.0

        final_equities.append(final_equity)
        annualizeds.append(float(annualized))
        max_drawdowns.append(max_dd)
        max_dd_durations.append(max_dd_duration)
        n_trades_list.append(n_trades)
        win_rates.append(win_rate)
        avg_holds.append(avg_hold)
        avg_pnls.append(avg_pnl)
        # 组合口径：每笔只作用在 1/N_SLOTS 的资金上。
        cost_drags.append(n_trades * args.cost / N_SLOTS)

        # 出场原因与分状态统计（跨全部路径累计）。
        for engine, entry_regime, hold_days, net_pnl, reason in trades:
            exit_reason_counts[reason] += 1
            regime_n[entry_regime] += 1
            regime_pnl_sum[entry_regime] += net_pnl
            if net_pnl > 0.0:
                regime_wins[entry_regime] += 1

        # 只保留前 3 条路径的逐日净值用于画图。
        if path_idx < 3:
            equity_curves_sample.append([float(value) for value in equity_curve])

    summary = {
        "final_equity": summarize(final_equities),
        "annualized": summarize(annualizeds),
        "max_drawdown": summarize(max_drawdowns),
        "max_dd_duration_days": summarize(max_dd_durations),
        "n_trades": summarize(n_trades_list),
        "win_rate": summarize(win_rates),
        "avg_hold": summarize(avg_holds),
        "avg_pnl": summarize(avg_pnls),
        "cost_drag_pct_of_capital": summarize(cost_drags),
    }

    total_reasons = sum(exit_reason_counts.values())
    exit_reason_share = {}
    for reason in ("take_profit", "stop", "stop_gap", "time"):
        exit_reason_share[reason] = (
            float(exit_reason_counts[reason] / total_reasons)
            if total_reasons > 0
            else 0.0
        )

    per_regime_trade_stats = {}
    for regime in REGIMES:
        n = regime_n[regime]
        per_regime_trade_stats[regime] = {
            "n": int(n),
            "win_rate": float(regime_wins[regime] / n) if n > 0 else 0.0,
            "avg_pnl": float(regime_pnl_sum[regime] / n) if n > 0 else 0.0,
        }

    finals_arr = np.asarray(final_equities, dtype=np.float64)
    dds_arr = np.asarray(max_drawdowns, dtype=np.float64)
    probabilities = {
        "p_final_lt_1": float(np.mean(finals_arr < 1.0)),
        "p_dd_gt_20": float(np.mean(dds_arr > 0.20)),
        "p_dd_gt_30": float(np.mean(dds_arr > 0.30)),
        "p_dd_gt_50": float(np.mean(dds_arr > 0.50)),
    }

    config = {
        "n_paths": int(args.n_paths),
        "days": int(args.days),
        "seed": int(args.seed),
        "edge_scale": float(args.edge_scale),
        "cost": float(args.cost),
        "slots": N_SLOTS,
        "lambda_scale": float(getattr(args, "lambda_scale", 1.0)),
        "barrier_mode": getattr(args, "barrier_mode", "prob"),
        "t_plus_one": True,
        "initial_capital": INITIAL_CAPITAL,
        "initial_distribution": {
            regime: float(prob)
            for regime, prob in zip(REGIMES, INITIAL_DISTRIBUTION)
        },
        "transition_matrix": TRANSITION.tolist(),
        "signal_table": build_signal_table_config(),
        "exit_params": {
            engine: {
                "tp": float(params["tp"]),
                "sl": float(params["sl"]),
                "max_hold": int(params["max_hold"]),
            }
            for engine, params in EXIT_PARAMS.items()
        },
        "theme_count": N_THEMES,
        "theme_cap": THEME_CAP,
        "circuit_breaker": CIRCUIT_BREAKER,
        "gap_sigma": GAP_SIGMA,
        "intraday_vol": INTRADAY_VOL,
    }

    return {
        "config": config,
        "summary": summary,
        "probabilities": probabilities,
        "exit_reason_share": exit_reason_share,
        "per_regime_trade_stats": per_regime_trade_stats,
        "equity_curves_sample": equity_curves_sample,
    }


def print_summary(payload, out_path, elapsed):
    """在 stdout 打印便于人工核对的摘要。"""
    cfg = payload["config"]
    summary = payload["summary"]
    probs = payload["probabilities"]

    print("=" * 78)
    print("系统组合可行性压力测试（合成路径，不是回测）")
    print("=" * 78)
    print(
        f"n_paths={cfg['n_paths']}  days={cfg['days']}  seed={cfg['seed']}  "
        f"edge_scale={cfg['edge_scale']:.3f}  cost={cfg['cost']:.4f}"
    )
    print("-" * 78)
    print("跨路径汇总（mean / median / p5 / p25 / p75 / p95）")
    print("-" * 78)

    display = [
        ("final_equity", "期末净值", "number"),
        ("annualized", "年化收益", "pct"),
        ("max_drawdown", "最大回撤", "pct"),
        ("max_dd_duration_days", "最长回撤持续天数", "days"),
        ("n_trades", "交易笔数", "number"),
        ("win_rate", "胜率", "pct"),
        ("avg_hold", "平均持有天数", "days"),
        ("avg_pnl", "单笔平均净收益", "pct"),
        ("cost_drag_pct_of_capital", "总成本拖累/资金", "pct"),
    ]
    for key, label, kind in display:
        item = summary[key]
        if kind == "pct":
            values = [item[k] * 100.0 for k in ("mean", "median", "p5", "p25", "p75", "p95")]
            suffix = "%"
            pattern = "{:9.2f}"
        else:
            values = [item[k] for k in ("mean", "median", "p5", "p25", "p75", "p95")]
            suffix = "天" if kind == "days" else ""
            pattern = "{:9.3f}"
        rendered = " / ".join(pattern.format(v) for v in values)
        print(f"{label:<20s} {rendered}{(' ' + suffix) if suffix else ''}")

    print("-" * 78)
    print(
        "亏损概率 P(final<1)={:.2%}   "
        "P(MDD>20%)={:.2%}   P(MDD>30%)={:.2%}   P(MDD>50%)={:.2%}".format(
            probs["p_final_lt_1"],
            probs["p_dd_gt_20"],
            probs["p_dd_gt_30"],
            probs["p_dd_gt_50"],
        )
    )
    print(
        "出场原因占比：take_profit={:.1%}  stop={:.1%}  stop_gap={:.1%}  time={:.1%}".format(
            payload["exit_reason_share"]["take_profit"],
            payload["exit_reason_share"]["stop"],
            payload["exit_reason_share"]["stop_gap"],
            payload["exit_reason_share"]["time"],
        )
    )
    print("-" * 78)
    print("分入场状态交易统计：")
    for regime in REGIMES:
        stats = payload["per_regime_trade_stats"][regime]
        print(
            f"  {regime:<8s} n={stats['n']:<7d} "
            f"win_rate={stats['win_rate']:.2%}  avg_pnl={stats['avg_pnl']:.2%}"
        )
    print("-" * 78)
    print(f"JSON 已写入：{out_path}")
    print(f"耗时：{elapsed:.2f} 秒")
    print("=" * 78)


def parse_args():
    parser = argparse.ArgumentParser(
        description="3 槽位 T+1 涨停接力组合的合成路径可行性压力测试（非回测）"
    )
    parser.add_argument("--n-paths", type=int, default=2000, help="模拟路径数")
    parser.add_argument("--days", type=int, default=500, help="每条路径的交易日数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument(
        "--edge-scale", type=float, default=1.0, help="潜在漂移 mu 的缩放倍数"
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=0.003,
        help="单笔往返总摩擦（佣金+印花税+滑点），一次性扣除",
    )
    parser.add_argument(
        "--lambda-scale",
        type=float,
        default=1.0,
        help="信号到达强度 lambda 的缩放倍数（<1 表示更严格的信号过滤、更低周转）",
    )
    parser.add_argument(
        "--slots", type=int, default=3, help="同时持仓槽位数（默认 3）"
    )
    parser.add_argument(
        "--barrier-mode",
        choices=["prob", "stop"],
        default="prob",
        help="同一日 K 同时触及止盈与止损时的判定：prob=布朗桥概率解（默认），stop=一律止损（有偏，仅用于对照）",
    )
    parser.add_argument("--out", default="out/mc.json", help="JSON 输出路径")
    args = parser.parse_args()

    if args.n_paths < 1:
        parser.error("--n-paths 必须 >= 1")
    if args.days < 1:
        parser.error("--days 必须 >= 1")
    global N_SLOTS
    N_SLOTS = max(1, int(args.slots))
    if args.lambda_scale < 0.0:
        parser.error("--lambda-scale 必须 >= 0")
    if args.cost < 0.0:
        parser.error("--cost 必须 >= 0")
    return args


def main():
    args = parse_args()
    started = time.perf_counter()

    payload = run_monte_carlo(args)

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        # allow_nan=False：若模型产生 NaN/Inf，直接报错而不是写出坏 JSON。
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")

    elapsed = time.perf_counter() - started
    print_summary(payload, out_path, elapsed)


if __name__ == "__main__":
    main()
