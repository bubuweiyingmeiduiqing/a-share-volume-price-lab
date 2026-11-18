# -*- coding: utf-8 -*-
"""
L1 市场状态层：E-score 情绪温度计 + 五状态机（含迟滞与最小驻留）。

因子（全部可由日频数据重建，不依赖分时/全市场快照）：
  f1 zt      涨停家数
  f2 promo   连板晋级率（昨日涨停今日仍涨停）
  f3 seal    封板成功率 = 1 - 炸板率
  f4 maxb    最高连板高度
  f5 prevret 昨日涨停股今日平均涨幅（赚钱效应）
  f6 mom     涨停家数 5 日动量

说明：设计文档里的「涨跌家数比」和「全市场成交额占比」需要全市场快照
（akshare 该接口约 130 秒，不适合每日跑），本实现以 f2/f5 替代其信息量。
这一替代已记入报告，不影响状态机的结构。
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

import pandas as pd

import data as dm
from config import (
    E_TH_ICE, E_TH_REPAIR, E_TH_EXPAND, HYSTERESIS, MIN_DWELL,
    STATE_DIR, STATE_CN,
)

REGIME_FILE = STATE_DIR / "regime.json"


# ---------------------------------------------------------------------------
# 因子
# ---------------------------------------------------------------------------

def compute_factors(date: str) -> dict:
    """返回某交易日的六个原始因子。"""
    zt = dm.normalize_pool(dm.zt_pool(date))
    zb = dm.normalize_pool(dm.zb_pool(date))
    pz = dm.prev_zt_pool(date)

    n_zt, n_zb = len(zt), len(zb)
    promo = None
    prevret = None
    if pz is not None and not pz.empty:
        price = pd.to_numeric(pz["最新价"], errors="coerce")
        lmt = pd.to_numeric(pz["涨停价"], errors="coerce")
        still = (price >= lmt - 0.011) & lmt.notna()
        promo = float(still.sum()) / float(len(pz))
        prevret = float(pd.to_numeric(pz["涨跌幅"], errors="coerce").fillna(0).mean())

    seal = n_zt / (n_zt + n_zb) if (n_zt + n_zb) > 0 else 0.0
    maxb = int(zt["boards"].max()) if n_zt else 0

    themes = {}
    if n_zt:
        themes = {str(k): int(v) for k, v in zt["industry"].value_counts().items() if str(k).strip()}

    return {
        "date": date,
        "zt": n_zt,
        "zb": n_zb,
        "promo": promo if promo is not None else 0.5,
        "seal": seal,
        "maxb": maxb,
        "prevret": prevret if prevret is not None else 0.0,
        "themes": themes,
    }


def with_momentum(rows: list[dict]) -> list[dict]:
    """补上 f6 = 涨停家数 / 前 5 日均值。"""
    out = []
    for i, r in enumerate(rows):
        prev = [x["zt"] for x in rows[max(0, i - 5):i]]
        base = st.mean(prev) if prev else r["zt"]
        r = dict(r)
        r["mom"] = (r["zt"] / base) if base > 0 else 1.0
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------

def _pct_rank(values: list[float], v: float) -> float:
    if not values:
        return 0.5
    n = len(values)
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (below + 0.5 * equal) / n


FACTOR_KEYS = ("zt", "promo", "seal", "maxb", "prevret", "mom")


def e_score(history: list[dict], today: dict) -> tuple[float, dict]:
    """
    E = 六因子滚动分位数的等权平均 × 100。
    history 含 today。返回 (E, 各因子分位)。
    """
    parts = {}
    for k in FACTOR_KEYS:
        vals = [float(x.get(k, 0.0)) for x in history]
        parts[k] = _pct_rank(vals, float(today.get(k, 0.0)))
    e = 100.0 * sum(parts.values()) / len(parts)
    return e, parts


# ---------------------------------------------------------------------------
# 平滑
# ---------------------------------------------------------------------------

def smooth(rows: list[dict], alpha: float = 0.5) -> list[dict]:
    """E 的指数平滑：E_t = alpha*E_raw + (1-alpha)*E_{t-1}。
    短窗口分位数天然跳动，不平滑会让状态机在持仓期内反复翻转。"""
    prev = None
    for r in rows:
        raw = float(r.get("e_raw", r.get("e", 50.0)))
        r["e_raw"] = raw
        r["e"] = raw if prev is None else alpha * raw + (1 - alpha) * prev
        prev = r["e"]
    return rows


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

def _mean(rows, key, n):
    vals = [float(x.get(key, 0.0)) for x in rows[-n:]]
    return st.mean(vals) if vals else 0.0


def classify(e: float, hist: list[dict], cur: str | None, dwell: int) -> tuple[str, str]:
    """返回 (状态, 理由)。priority 顺序：FADE > ICE > EXPAND > DIVERGE > REPAIR。"""
    today = hist[-1]
    de5 = e - (hist[-6]["e"] if len(hist) >= 6 else e)

    zb_rate = 1.0 - float(today.get("seal", 1.0))
    zb5 = _mean(hist[:-1], "seal", 5)
    # 用中位数而非均值：个别 10+ 板妖股会把均值拉飞，导致"高度回落"误判
    maxb_hist = [float(x.get("maxb", 0.0)) for x in hist[:-1][-5:]]
    maxb5 = st.median(maxb_hist) if maxb_hist else 0.0
    promo = float(today.get("promo", 0.0))
    maxb = float(today.get("maxb", 0.0))

    proposed, why = "REPAIR", "其余情形"

    if cur in ("EXPAND", "DIVERGE") and (
        e < 40
        or (1.0 - zb5) > 0 and zb_rate > (1.0 - zb5) * 1.25 + 0.10
        or (maxb5 > 0 and maxb <= maxb5 - 2 and de5 < -10)
    ):
        proposed, why = "FADE", "赚钱效应崩塌/炸板率骤升/高度回落"
    elif e <= E_TH_ICE and de5 <= 5:
        proposed, why = "ICE", "情绪冰点"
    elif e >= E_TH_EXPAND and maxb >= 5:
        proposed, why = "EXPAND", "主升：E 高分位且存在 5 板以上高度"
    elif e >= E_TH_REPAIR and (
        (1.0 - zb5) > 0 and (1.0 - float(today.get("seal", 1.0))) > (1.0 - zb5) * 1.2
        or (maxb5 > 0 and maxb < maxb5 * 0.8)
    ):
        proposed, why = "DIVERGE", "高位分歧：封板率下滑/高度松动"
    elif e >= E_TH_REPAIR:
        proposed, why = "REPAIR", "修复期"

    if cur is None:
        return proposed, why

    # 迟滞：离开 EXPAND 需跌破 E_TH_EXPAND - HYSTERESIS
    if cur == "EXPAND" and proposed != "EXPAND" and proposed != "FADE":
        if e >= E_TH_EXPAND - HYSTERESIS:
            return "EXPAND", f"迟滞维持（E={e:.0f} 仍在带内）"
    if cur == "ICE" and proposed == "REPAIR" and e < E_TH_ICE + HYSTERESIS:
        return "ICE", f"迟滞维持（E={e:.0f} 未突破冰点带）"

    # 最小驻留：不足 MIN_DWELL 天不切（FADE 例外，安全优先）
    if proposed != cur and proposed != "FADE" and dwell < MIN_DWELL:
        return cur, f"最小驻留未满（{dwell} < {MIN_DWELL}）"

    return proposed, why


# ---------------------------------------------------------------------------
# 状态存取
# ---------------------------------------------------------------------------

def load() -> dict:
    if REGIME_FILE.exists():
        try:
            return json.loads(REGIME_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"current": None, "days_in_state": 0, "history": []}


def save(s: dict) -> None:
    tmp = REGIME_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGIME_FILE)


def update(date: str, state: dict) -> tuple[dict, dict]:
    """
    推进一天。返回 (新 state, 当日结果)。
    幂等：若 history 里已有该日，只重算 E 与状态，不重复追加。
    """
    hist = [h for h in state.get("history", []) if h["date"] < date]
    hist.append(compute_factors(date))
    hist = with_momentum(hist)

    today = hist[-1]
    e_raw, parts = e_score(hist, today)
    today["e_raw"] = e_raw
    prev_e = hist[-2]["e"] if len(hist) >= 2 else None
    e = e_raw if prev_e is None else 0.5 * e_raw + 0.5 * prev_e
    today["e"] = e

    prev_state = state.get("current")
    dwell = state.get("days_in_state", 0)

    # 若历史已覆盖当天（补跑），用历史里的状态做参照
    if hist[:-1] and "state" in hist[-2]:
        prev_state = hist[-2]["state"]

    st_new, why = classify(e, hist, prev_state, dwell)

    if prev_state == st_new:
        new_dwell = dwell + 1
    else:
        new_dwell = 1

    today["state"] = st_new

    new_state = dict(state)          # 保留 bootstrapped 等外部键
    new_state["current"] = st_new
    new_state["days_in_state"] = new_dwell
    new_state["history"] = hist[-260:]

    return new_state, {
        "date": date,
        "e": e,
        "parts": parts,
        "state": st_new,
        "state_cn": STATE_CN[st_new],
        "why": why,
        "dwell": new_dwell,
        "factors": today,
    }
