# -*- coding: utf-8 -*-
"""
L2/L3/L4：选股 + 评分 + 次日开盘条件单。

口诀到规则的映射：
  买横              → PLATFORM：20 日箱体够紧、5/20 量比缩、MA60 不坏
  买坑              → PIT_REVERSAL：10 日内急跌、下跌无量、重新站上 MA5
  不买竖            → VERTICAL 否决：5/10 日涨幅、离 MA20 距离、连续大阳
  连续小涨要大涨    → SMALL_RISE：5 日内 ≥3 天小涨，累计 2%-10%，温和放量
  不挖深坑不大买    → DEEP_PIT：跌幅 ≥25% 且趋势未坏，才允许放大风险预算
  急跌放量立马撤    → 当日放量下跌的票不进入候选
  快速冲高要回落    → 当日长上影/冲高回落不进入候选

输出是条件单：T 日收盘出计划，T+1 开盘若 gap 在区间内、非一字/涨停开盘，
则以开盘价成交。计划里带 stop、目标权重、触发理由，盘中不改单。
"""

from __future__ import annotations

from collections import Counter

import pandas as pd

import data as dm
import indicators as ind
from config import SETUP_CN, SETUP_PRIORITY, eng_label


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _round(x: float, n: int = 4) -> float:
    return round(float(x), n)


def _avg_amount(df: pd.DataFrame, n: int = 20) -> float:
    if df is None or len(df) < n:
        return 0.0
    return float(df["amount"].tail(n).mean())


def analyze_stock(code: str, df: pd.DataFrame, meta: dict, market_date: str,
                  params, regime) -> tuple[dict | None, str | None]:
    """返回 (candidate, None) 或 (None, skip_reason)。"""
    if df is None or df.empty:
        return None, "无日线"
    if str(df["date"].iloc[-1]) != market_date:
        return None, "停牌/未更新"
    min_hist = max(int(params.get("universe.min_history_days")),
                   int(params.get("holding.ma_long")) + 5, 61)
    if len(df) < min_hist:
        return None, f"历史不足（{len(df)} < {min_hist}）"

    name = meta.get("name", "")
    if params.get("universe.exclude_st") and "ST" in name.upper():
        return None, "ST"
    if params.get("universe.exclude_bse") and ind.is_bse(code):
        return None, "北交所"

    row = df.iloc[-1]
    prev_close = float(df["close"].iloc[-2])
    close = float(row["close"])
    # akshare / 东财 pct 列统一是百分数（5.2 表示 +5.2%）
    pct = float(row["pct"]) / 100.0

    if params.get("universe.exclude_limit_up_today") and ind.is_limit_up(code, name, row, prev_close):
        return None, "当日涨停（不买竖）"

    if close <= 0 or prev_close <= 0:
        return None, "价格异常"

    ma5 = ind.sma(df["close"], 5)
    ma10 = ind.sma(df["close"], 10)
    ma20 = ind.sma(df["close"], int(params.get("holding.ma_trend")))
    ma60 = ind.sma(df["close"], int(params.get("holding.ma_long")))
    ma20_s = ind.sma_series(df["close"], int(params.get("holding.ma_trend")))
    ma60_s = ind.sma_series(df["close"], int(params.get("holding.ma_long")))
    sw = int(params.get("timing.slope_window"))
    ma20_slope = (ma20_s.iloc[-1] / ma20_s.iloc[-1 - sw] - 1
                  if len(ma20_s.dropna()) > sw and ma20_s.iloc[-1 - sw] else 0.0)
    ma60_slope = (ma60_s.iloc[-1] / ma60_s.iloc[-1 - sw] - 1
                  if len(ma60_s.dropna()) > sw and ma60_s.iloc[-1 - sw] else 0.0)

    vol_ratio = ind.vol_ratio(df, int(params.get("timing.vol_window")))
    amount20 = _avg_amount(df, 20)
    if amount20 < float(params.get("universe.min_amount_20d")):
        return None, f"20 日成交额不足（{amount20 / 1e8:.2f} 亿）"
    if float(meta.get("float_mv", 0) or 0) and \
            float(meta["float_mv"]) < float(params.get("universe.min_float_mv")):
        return None, "流通市值不足"

    ret5 = ind.ret(df, 5)
    ret10 = ind.ret(df, 10)
    ret20 = ind.ret(df, 20)
    extension = close / ma20 - 1 if ma20 else 0.0
    upper = ind.upper_shadow_pct(row, prev_close)
    cpos = ind.close_position(row)
    big_up_3 = ind.count_big_up(df, 3, float(params.get("holding.climax_big_up")))
    small = ind.small_rise_stats(
        df, int(params.get("selection.small_rise_window")),
        float(params.get("selection.small_rise_min")),
        float(params.get("selection.small_rise_max")),
    )
    box = ind.box(df, int(params.get("selection.platform_window")))
    pit = ind.pit_stats(df, int(params.get("selection.pit_lookback")),
                        int(params.get("selection.reclaim_ma")))

    buffer = float(params.get("selection.platform_ma60_buffer"))
    trend_ok = close >= ma60 * (1 - buffer) and ma60_slope >= -0.002 and ma60 > 0
    above_long = close > ma60 and ma60_slope >= -0.001

    # ---- 不买竖：任何一条命中都直接排除 --------------------------------
    vertical_hits = []
    if ret5 >= float(params.get("selection.vertical_ret5")):
        vertical_hits.append(f"5日 +{ret5:.1%}")
    if ret10 >= float(params.get("selection.vertical_ret10")):
        vertical_hits.append(f"10日 +{ret10:.1%}")
    if extension >= float(params.get("selection.vertical_ma20_dist")):
        vertical_hits.append(f"离MA20 +{extension:.1%}")
    if big_up_3 >= int(params.get("selection.vertical_big_up_days")):
        vertical_hits.append(f"3日内 {big_up_3} 根大阳")
    if pct >= float(params.get("selection.small_rise_max")) * 2 and vol_ratio >= float(params.get("exit.failed_spike_vol")):
        vertical_hits.append(f"当日 +{pct:.1%} 放量")
    if vertical_hits:
        return None, "不买竖：" + "、".join(vertical_hits[:3])

    # ---- 当日放量急跌 / 冲高回落：不接飞刀、不接诱多 --------------------
    if (pct <= -float(params.get("exit.volume_dump_pct"))
            and vol_ratio >= float(params.get("exit.volume_dump_vol"))):
        return None, f"放量急跌 -{abs(pct):.1%}/量比{vol_ratio:.2f}"
    if (upper >= float(params.get("exit.upper_shadow"))
            and vol_ratio >= float(params.get("exit.failed_spike_vol")) and pct < 0.03):
        return None, f"长上影 {upper:.1%} 且放量，冲高回落"

    # ---- 横盘 -----------------------------------------------------------
    vol5 = ind.vol_ma(df, 5)
    vol20_prev = ind.vol_ma(df, 20, include_today=False)
    vol_contraction = ind.safe_div(vol5, vol20_prev, 1.0)
    edge = float(params.get("selection.platform_edge_buffer"))
    platform = (
        box["range"] > 0
        and box["range"] <= float(params.get("selection.platform_max_range"))
        and box["low"] * (1 - edge) <= close <= box["high"] * (1 + edge)
        and vol_contraction <= float(params.get("selection.platform_vol_max"))
        and ma20_slope >= -0.002
        and trend_ok
    )

    # ---- 坑 -------------------------------------------------------------
    pit_ok = (
        pit["depth"] <= -float(params.get("selection.pit_min_depth"))
        and pit["depth"] >= -float(params.get("selection.pit_max_depth"))
        and pit["down_vol_ratio"] <= float(params.get("selection.pit_vol_max"))
        and pit["recover"]
        and close >= prev_close * 0.99
        and close > float(row["open"])
        and above_long
    )
    deep_pit = pit_ok and pit["depth"] <= -float(params.get("selection.deep_pit_depth"))

    # ---- 连续小涨 -------------------------------------------------------
    small_ok = (
        small["count"] >= int(params.get("selection.small_rise_min_count"))
        and small["today"]
        and float(params.get("selection.small_rise_cum_min")) <= small["cum"]
        <= float(params.get("selection.small_rise_cum_max"))
        and close > ma20
        and extension <= float(params.get("selection.vertical_ma20_dist")) * 0.8
        and trend_ok
        and float(params.get("selection.small_rise_vol_min")) <= vol_ratio
        <= float(params.get("selection.small_rise_vol_max"))
        and cpos >= float(params.get("selection.small_rise_close_pos"))
    )

    setup = None
    if platform and small_ok:
        setup = "PLATFORM_SMALL_RISE"
    elif deep_pit:
        setup = "DEEP_PIT"
    elif pit_ok:
        setup = "PIT_REVERSAL"
    elif platform:
        setup = "PLATFORM"
    elif small_ok:
        setup = "SMALL_RISE"
    if setup is None:
        return None, "不符合横/坑/小涨任一形态"

    # ---- 评分：趋势 30 + 结构 30 + 量能 25 + 触发 15 --------------------
    trend_score = (
        0.35 * (1.0 if close > ma20 else 0.0)
        + 0.25 * (1.0 if ma20 > ma60 else 0.0)
        + 0.20 * (1.0 if ma60_slope > 0 else 0.0)
        + 0.20 * _clip(1.0 - extension / max(1e-6, float(params.get("selection.vertical_ma20_dist"))))
    )

    if pit_ok:
        depth_norm = _clip(
            (abs(pit["depth"]) - float(params.get("selection.pit_min_depth")))
            / max(1e-6, float(params.get("selection.pit_max_depth"))
                  - float(params.get("selection.pit_min_depth")))
        )
        structure_score = 0.7 * depth_norm + 0.3 * (1.0 if deep_pit else 0.5)
    elif platform:
        tight = _clip(1.0 - box["range"] / max(1e-6, float(params.get("selection.platform_max_range"))))
        structure_score = 0.7 * tight + 0.3 * (1.0 if small_ok else 0.5)
    else:
        structure_score = 0.45

    vol_score = 0.0
    if platform:
        vol_score = max(vol_score, _clip(
            (float(params.get("selection.platform_vol_max")) - vol_contraction)
            / max(1e-6, float(params.get("selection.platform_vol_max")))
        ))
    if pit_ok:
        vol_score = max(vol_score, _clip(
            (float(params.get("selection.pit_vol_max")) - pit["down_vol_ratio"])
            / max(1e-6, float(params.get("selection.pit_vol_max")))
        ))
    if small_ok:
        vol_score = max(vol_score, _clip(1.0 - abs(vol_ratio - 1.2) / 1.0))

    trigger_score = 0.45
    if small_ok:
        trigger_score = max(trigger_score, small["count"] / max(1, int(params.get("selection.small_rise_window"))))
    if pit_ok:
        trigger_score = max(trigger_score, 0.55 + 0.45 * cpos)
    if setup == "PLATFORM":
        trigger_score = max(trigger_score, 0.55)

    penalty = 0.0
    if upper >= float(params.get("exit.upper_shadow")) and vol_ratio >= float(params.get("exit.failed_spike_vol")):
        penalty += 8.0
    if pct >= float(params.get("selection.small_rise_max")) * 2:
        penalty += 5.0

    score = 100.0 * (0.30 * trend_score + 0.30 * structure_score
                     + 0.25 * vol_score + 0.15 * trigger_score) - penalty
    score = max(0.0, min(100.0, score))
    if score < float(params.get("selection.min_score")):
        return None, f"评分不足（{score:.0f} < {params.get('selection.min_score')}）"

    # ---- 止损：支撑位与硬止损取更近者，再保证最小止损距离 --------------
    support = close * (1 - float(params.get("entry.stop_loss")))
    if platform:
        support = max(support, box["low"] * 0.99)
    if pit_ok:
        support = max(support, pit["trough"] * 0.99)
    if setup == "SMALL_RISE":
        support = max(support, ma10 * 0.99)
    min_stop_pct = float(params.get("entry.min_stop_pct"))
    stop = min(support, close * (1 - min_stop_pct))
    stop = max(stop, 0.01)
    stop_pct = (close - stop) / close
    if stop_pct <= 0.005:
        return None, "止损距离异常"

    size_mult = {
        "PLATFORM": float(params.get("entry.size_mult_platform")),
        "PIT_REVERSAL": float(params.get("entry.size_mult_pit")),
        "DEEP_PIT": float(params.get("entry.size_mult_deep_pit")),
        "SMALL_RISE": float(params.get("entry.size_mult_small_rise")),
        "PLATFORM_SMALL_RISE": float(params.get("entry.size_mult_platform_small_rise")),
    }[setup]
    cold_exception = (setup == "DEEP_PIT" and regime is not None
                      and getattr(regime, "state", None) == "COLD")
    if cold_exception:
        size_mult = float(params.get("entry.size_mult_cold_deep_pit"))
    target_weight = min(
        float(params.get("portfolio.max_weight")),
        float(params.get("portfolio.risk_per_trade")) * size_mult / stop_pct,
    )

    gap_min = (float(params.get("entry.pit_gap_min")) if setup in ("DEEP_PIT", "PIT_REVERSAL")
               else float(params.get("entry.gap_min")))

    reasons = []
    if platform:
        reasons.append(f"平台 {int(params.get('selection.platform_window'))} 日振幅 {box['range']:.1%}"
                       f" ≤ {params.get('selection.platform_max_range'):.0%}")
        reasons.append(f"5/20 量比 {vol_contraction:.2f}（缩量）")
    if pit_ok:
        reasons.append(f"坑深 {abs(pit['depth']):.1%}（下跌量比 {pit['down_vol_ratio']:.2f}）")
        reasons.append("收盘站上 MA5，趋势未破 MA60")
    if small_ok:
        reasons.append(f"{small['count']} 日小涨、累计 {small['cum']:+.1%}，收盘位置 {cpos:.0%}")
    if cold_exception:
        reasons.append(f"冰点深坑例外：风险预算 {size_mult:.2f}x，最多 1 只")
    reasons.append(f"评分 {score:.0f}；止损 {stop:.2f}（-{stop_pct:.1%}）")

    cand = {
        "code": code,
        "name": name,
        "industry": meta.get("industry", "未分类"),
        "setup": setup,
        "setup_cn": SETUP_CN.get(setup, setup),
        "score": _round(score, 1),
        "close": _round(close, 3),
        "pct": _round(pct, 4),
        "ma20": _round(ma20, 3),
        "ma60": _round(ma60, 3),
        "stop": _round(stop, 3),
        "stop_pct": _round(stop_pct, 4),
        "target_weight": _round(target_weight, 4),
        "size_mult": _round(size_mult, 2),
        "cold_exception": bool(cold_exception),
        "gap_min": _round(gap_min, 4),
        "gap_max": _round(float(params.get("entry.gap_max")), 4),
        "max_chase": _round(float(params.get("entry.max_chase")), 4),
        "risk_per_trade": _round(float(params.get("portfolio.risk_per_trade")) * size_mult, 5),
        "reasons": reasons[:4],
        "score_parts": {
            "trend": _round(trend_score, 3), "structure": _round(structure_score, 3),
            "volume": _round(vol_score, 3), "trigger": _round(trigger_score, 3),
            "penalty": _round(penalty, 1),
        },
        "metrics": {
            "ret5": _round(ret5, 4), "ret10": _round(ret10, 4), "ret20": _round(ret20, 4),
            "vol_ratio": _round(vol_ratio, 2), "box_range": _round(box["range"], 4),
            "pit_depth": _round(pit["depth"], 4), "small_count": small["count"],
            "extension": _round(extension, 4), "upper_shadow": _round(upper, 4),
        },
    }
    return cand, None


def build_plan(market: dm.MarketData, regime, params) -> dict:
    """T 日收盘生成 T+1 条件单。"""
    base = {
        "date": market.date,
        "for_date": None,
        "state": regime.state,
        "state_cn": regime.state_cn,
        "heat": round(regime.heat, 1),
        "cap": regime.cap,
        "allow_new": regime.allow_new,
        "market_climax": regime.market_climax,
        "params_digest": params.digest,
        "profile": params.profile,
        "universe": len(market.bars),
        "candidates": [],
        "skipped": [],
        "skip_summary": {},
    }
    if not regime.allow_new:
        base["skip_summary"] = {"状态不允许开新仓": len(market.bars)}
        return base

    allowed = set(regime.allowed_setups)
    cands: list[dict] = []
    skipped: list[dict] = []
    reasons = Counter()

    for code, df in market.bars.items():
        meta = market.meta.get(code, {})
        try:
            cand, why = analyze_stock(code, df, meta, market.date, params, regime)
        except Exception as e:  # noqa: BLE001
            cand, why = None, f"计算异常：{type(e).__name__}"
        if cand:
            if cand["setup"] not in allowed:
                reasons["形态不在当前状态白名单"] += 1
                continue
            cands.append(cand)
        else:
            key = (why or "未知").split("：")[0]
            reasons[key] += 1
            if len(skipped) < 60:
                skipped.append({
                    "code": code, "name": meta.get("name", ""),
                    "why": why or "未知",
                })

    cands.sort(key=lambda c: (-c["score"], -SETUP_PRIORITY.get(c["setup"], 0),
                              -float(c.get("metrics", {}).get("vol_ratio", 0))))
    # 同行业最多 2 只进入计划（"未分类"不限制，否则全市场都会被当成一个行业）
    picked: list[dict] = []
    per_ind: Counter = Counter()
    ind_cap = int(params.get("selection.max_per_industry"))
    for c in cands:
        if c["industry"] != "未分类" and per_ind[c["industry"]] >= ind_cap:
            reasons["同行业计划数已满"] += 1
            continue
        picked.append(c)
        per_ind[c["industry"]] += 1
        if len(picked) >= int(params.get("selection.max_candidates")):
            break

    base["candidates"] = picked
    base["skipped"] = skipped
    base["skip_summary"] = dict(reasons.most_common(10))
    return base


def plan_lines(plan: dict, params, limit: int = 6) -> list[str]:
    cands = plan.get("candidates", [])
    if not cands:
        return ["- 无符合条件的候选"]
    out = []
    for c in cands[:limit]:
        out.append(
            f"- **{c['code']} {c['name']}**（{eng_label(c['setup'])}，评分 {c['score']:.0f}）"
            f" 开盘 {c['gap_min']:+.1%}~{c['gap_max']:+.1%} 买入，"
            f"止损 {c['stop']:.2f}（-{c['stop_pct']:.1%}），目标 {c['target_weight']:.0%} 仓"
        )
        if c.get("reasons"):
            out.append("  - " + "；".join(c["reasons"][:3]))
    if len(cands) > limit:
        out.append(f"- …另有 {len(cands) - limit} 只候选")
    return out
