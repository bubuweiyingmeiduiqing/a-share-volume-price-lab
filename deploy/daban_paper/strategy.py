# -*- coding: utf-8 -*-
"""
L2 主线 + L3 标的 + L4 引擎触发：把「今日涨停池」变成「明日可执行计划」。

输出计划是**条件单**形式：
    明日开盘 gap ∈ [gap_min, gap_max] 且 非一字开盘 → 以开盘价买入
因为 T 日收盘时不知道 T+1 的开盘价，只能把触发条件写成区间。
"""

from __future__ import annotations

import statistics as st

import data as dm
import pandas as pd
from config import (
    BUYABLE_RANK, ENGINE, LOW_POSITION, LOW_PRICE, MAX_BOARD_TRIGGER,
    MIN_SEAL_AMOUNT_RATIO, SEAL_LATE_MINUTES, THEME_BURST, THEME_MAIN_PCTILE,
    THEME_HEAT_WINDOW, THEME_MIN_HEAT,
)

SEAL_LATE = 270  # 14:00 = 9:30 后 270 分钟


# ---------------------------------------------------------------------------
# L2 主线
# ---------------------------------------------------------------------------

def theme_main_set(hist: list[dict], date: str) -> tuple[set[str], dict]:
    """
    主线题材 = 近 10 日涨停家数分位 ≥ 0.70 且 今日仍有涨停 的板块。
    另返回今日热度表用于报告。
    """
    if not hist:
        return set(), {}
    win = hist[-THEME_HEAT_WINDOW:]
    today_heat: dict[str, int] = {}
    for h in win:
        for k, v in (h.get("themes") or {}).items():
            today_heat[k] = today_heat.get(k, 0) + int(v)

    cur = (hist[-1].get("themes") or {})
    if sum(cur.values()) == 0:
        return set(), {}

    rows = sorted(cur.items(), key=lambda x: -x[1])
    vals = [v for _, v in rows]
    main = set()
    for name, v in rows:
        if v < THEME_MIN_HEAT:
            continue
        # 分位：在今日各板块热度中的位置
        below = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        pct = (below + 0.5 * equal) / len(vals)
        # 10 日累计热度也要靠前
        w_below = sum(1 for x in today_heat.values() if x < today_heat.get(name, 0))
        w_pct = w_below / max(1, len(today_heat))
        if pct >= THEME_MAIN_PCTILE and w_pct >= 0.60:
            main.add(name)
    return main, cur


# ---------------------------------------------------------------------------
# L3 身位
# ---------------------------------------------------------------------------

def rank_identity(zt: pd.DataFrame) -> dict[str, int]:
    """题材内身位排名：连板数 desc → 首封时间 asc → 封单额 desc。"""
    out: dict[str, int] = {}
    if zt.empty:
        return out
    for _, g in zt.groupby("industry"):
        g = g.copy()
        g["fm"] = g["first_seal"].map(dm.seal_minutes)
        g = g.sort_values(["boards", "fm", "seal_amt"], ascending=[False, True, False])
        for i, code in enumerate(g["code"].tolist(), 1):
            out[code] = i
    return out


def position_pct(bar: dict) -> float:
    hi = bar.get("high250") or 0.0
    if hi <= 0 or not bar.get("close"):
        return 1.0
    return float(bar["close"]) / hi


# ---------------------------------------------------------------------------
# L4 计划生成
# ---------------------------------------------------------------------------

def build_plan(date: str, hist: list[dict], state: str, max_board: int) -> dict:
    """
    用 T 日收盘数据生成 T+1 的计划。
    返回 {"date": T, "for_date": T+1, "state":..., "candidates":[...], "skipped":[...]}
    """
    zt = dm.normalize_pool(dm.zt_pool(date))
    main, heat = theme_main_set(hist, date)
    rank = rank_identity(zt)

    cands: list[dict] = []
    skipped: list[dict] = []

    if zt.empty:
        return {"date": date, "state": state, "main_themes": sorted(main),
                "candidates": [], "skipped": [], "heat": heat}

    for _, r in zt.iterrows():
        code, name = r["code"], r["name"]
        reason = None

        if dm.is_bse(code):
            reason = "北交所"
        elif "ST" in name.upper():
            reason = "ST"
        elif r["industry"] not in main:
            reason = f"非主线（{r['industry'] or '未知'}）"
        elif r["boards"] < 1:
            reason = "无连板数据"
        else:
            bar = dm.bar_on(code, date)
            if bar is None:
                reason = "无日线"
            else:
                ratio = dm.limit_ratio(code, name)
                lp = dm.limit_up_price(bar["prev_close"], ratio)
                one_word = abs(bar["open"] - lp) < 0.011 and abs(bar["close"] - lp) < 0.011
                seal_ratio = (r["seal_amt"] / r["float_mv"]) if r["float_mv"] > 0 else 0
                late = dm.seal_minutes(r["last_seal"]) >= SEAL_LATE

                if one_word:
                    reason = "今日一字板（无换手，次日易一字买不进）"
                elif r["break_times"] >= 6:
                    reason = "炸板次数过多"
                elif seal_ratio < MIN_SEAL_AMOUNT_RATIO:
                    reason = f"封单偏弱（{seal_ratio:.2%}）"
                elif late and r["break_times"] > 0:
                    reason = "尾盘回封（弱板）"
                elif rank.get(code, 99) > max(BUYABLE_RANK) and r["boards"] >= 2:
                    reason = f"跟风身位（第 {rank.get(code)}）"

        if reason:
            skipped.append({"code": code, "name": name, "boards": int(r["boards"]),
                            "industry": r["industry"], "why": reason})
            continue

        # 引擎分配
        engine = None
        if r["boards"] >= 2 and rank.get(code, 99) <= max(BUYABLE_RANK):
            engine = "E1"
        elif r["boards"] == 1:
            if bar["close"] <= LOW_PRICE and position_pct(bar) <= LOW_POSITION:
                engine = "E3"

        if engine is None:
            skipped.append({"code": code, "name": name, "boards": int(r["boards"]),
                            "industry": r["industry"], "why": "不符合任一引擎条件"})
            continue

        # 高低切闸门
        if max_board >= MAX_BOARD_TRIGGER and r["boards"] >= 2 and engine == "E1":
            skipped.append({"code": code, "name": name, "boards": int(r["boards"]),
                            "industry": r["industry"], "why": f"最高板 {max_board}，禁买中位身位"})
            continue

        p = ENGINE[engine]
        cands.append({
            "code": code, "name": name, "engine": engine,
            "industry": r["industry"], "boards": int(r["boards"]),
            "rank": rank.get(code, 99), "close": bar["close"],
            "turnover": float(r["turnover"]), "seal_amt": float(r["seal_amt"]),
            "float_mv": float(r["float_mv"]),
            "first_seal": r["first_seal"], "break_times": int(r["break_times"]),
            "pos250": round(position_pct(bar), 3),
            "gap_min": p["gap_min"], "gap_max": p["gap_max"],
            "tp": p["tp"], "sl": p["sl"], "max_hold": p["max_hold"],
            "weight": p["weight"],
        })

    # 排序：引擎优先 E1，其次身位、封单强度
    cands.sort(key=lambda c: (0 if c["engine"] == "E1" else 1, c["rank"], -c["seal_amt"]))
    return {
        "date": date,
        "state": state,
        "main_themes": sorted(main),
        "heat": heat,
        "candidates": cands[:12],
        "skipped": skipped[:40],
        "n_zt": len(zt),
        "max_board": max_board,
    }
