# -*- coding: utf-8 -*-
"""
L1 择时层：市场温度 → 五状态 → 总仓位上限 / 允许的选股形态。

口诀映射：
  卖在人声鼎沸处   → HOT / market_climax，停止开仓、盈利仓减半
  急跌无量是洗盘   → 指数急跌但缩量且仍在 MA60 上方，不切 CRASH
  急跌放量立马撤   → 指数急跌且放量，切 CRASH，总仓位 0

温度不是“预测”，只是把趋势、宽度、动量、量能、上涨家数压成一个 0-100 的
仓位旋钮。所有阈值来自 YAML，不在代码里写死。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import data as dm
import indicators as ind
from config import STATE_CN, STATE_DIR, STATE_SETUPS

REGIME_FILE = STATE_DIR / "regime.json"


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class RegimeResult:
    date: str
    state: str
    state_cn: str
    heat: float
    cap: float
    allow_new: bool
    allowed_setups: list[str]
    market_climax: bool
    washout: bool
    reasons: list[str]
    parts: dict
    metrics: dict
    dwell: int = 1

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "state": self.state,
            "state_cn": self.state_cn,
            "heat": round(self.heat, 2),
            "cap": self.cap,
            "allow_new": self.allow_new,
            "allowed_setups": list(self.allowed_setups),
            "market_climax": self.market_climax,
            "washout": self.washout,
            "reasons": list(self.reasons),
            "parts": {k: round(float(v), 4) for k, v in self.parts.items()},
            "metrics": {k: (round(float(v), 6) if isinstance(v, (int, float)) else v)
                        for k, v in self.metrics.items()},
            "dwell": self.dwell,
        }


def _index_metrics(market: dm.MarketData, params) -> dict:
    idx = market.index
    if idx is None or len(idx) < int(params.get("timing.ma_slow")) + 10:
        raise dm.DataError("指数历史不足，无法计算择时状态")

    close = float(idx["close"].iloc[-1])
    prev = float(idx["close"].iloc[-2])
    day_pct = close / prev - 1 if prev else 0.0
    ma_f = ind.sma(idx["close"], int(params.get("timing.ma_fast")))
    ma_s = ind.sma(idx["close"], int(params.get("timing.ma_slow")))
    sw = int(params.get("timing.slope_window"))
    ma_f_slope = ind.slope_pct(idx["close"].tail(int(params.get("timing.ma_fast")) * 2), sw)
    ma_s_slope = ind.slope_pct(idx["close"].tail(int(params.get("timing.ma_slow")) * 2), sw)
    # 用均线本身算斜率更直观：MA_t / MA_{t-sw} - 1
    if len(idx) > int(params.get("timing.ma_fast")) + sw:
        mf = idx["close"].rolling(int(params.get("timing.ma_fast"))).mean()
        ms = idx["close"].rolling(int(params.get("timing.ma_slow"))).mean()
        ma_f_slope = float(mf.iloc[-1] / mf.iloc[-1 - sw] - 1) if mf.iloc[-1 - sw] else 0.0
        if len(ms.dropna()) > sw:
            ma_s_slope = float(ms.iloc[-1] / ms.iloc[-1 - sw] - 1) if ms.iloc[-1 - sw] else 0.0

    high60 = float(idx["high"].tail(60).max()) if len(idx) >= 60 else float(idx["high"].max())
    ret20 = ind.ret(idx, 20)
    ret5 = ind.ret(idx, 5)
    amount_ratio = ind.amount_ratio(idx, int(params.get("timing.vol_window")))
    if amount_ratio == 1.0 and float(idx["amount"].tail(20).sum()) <= 0:
        # 部分指数接口没有成交额，用成交量代替
        amount_ratio = ind.vol_ratio(idx, int(params.get("timing.vol_window")))

    return {
        "close": close,
        "day_pct": day_pct,
        "ret5": ret5,
        "ret20": ret20,
        "ma_fast": ma_f,
        "ma_slow": ma_s,
        "ma_fast_slope": ma_f_slope,
        "ma_slow_slope": ma_s_slope,
        "extension": close / ma_f - 1 if ma_f else 0.0,
        "drawdown60": close / high60 - 1 if high60 else 0.0,
        "amount_ratio": amount_ratio,
    }


def evaluate(market: dm.MarketData, params, prev_state: str | None = None,
             dwell: int = 0) -> RegimeResult:
    m = _index_metrics(market, params)
    b = market.breadth()
    m.update(b)

    trend = (
        0.45 * (1.0 if m["close"] > m["ma_fast"] else 0.0)
        + 0.30 * (1.0 if m["close"] > m["ma_slow"] else 0.0)
        + 0.15 * (1.0 if m["ma_fast"] > m["ma_slow"] else 0.0)
        + 0.10 * _clip((m["ma_slow_slope"] + 0.01) / 0.03)
    )
    breadth = _clip((m["above_ma20"] - 0.20) / 0.60)
    momentum = _clip((m["ret20"] + 0.03) / 0.15)
    volume = _clip((m["amount_ratio"] - 0.75) / 0.75)
    up = _clip((m["up_ratio"] - 0.30) / 0.50)

    parts = {
        "trend": trend, "breadth": breadth, "momentum": momentum,
        "volume": volume, "up": up,
    }
    heat = 100.0 * (
        trend * float(params.get("timing.weight_trend"))
        + breadth * float(params.get("timing.weight_breadth"))
        + momentum * float(params.get("timing.weight_momentum"))
        + volume * float(params.get("timing.weight_volume"))
        + up * float(params.get("timing.weight_up"))
    )

    reasons: list[str] = []
    crash = False
    if m["day_pct"] <= -float(params.get("timing.crash_day")):
        crash = True
        reasons.append(f"指数单日 {m['day_pct']:+.2%} ≤ -{params.get('timing.crash_day'):.1%}")
    if (m["ret5"] <= -float(params.get("timing.crash_ret5"))
            and m["amount_ratio"] >= float(params.get("timing.crash_vol"))):
        crash = True
        reasons.append(f"5 日 {m['ret5']:+.2%} 且量比 {m['amount_ratio']:.2f} 放量")
    if (m["close"] < m["ma_slow"] and m["ma_slow_slope"] < 0
            and m["above_ma20"] < 0.35):
        crash = True
        reasons.append("指数在 MA60 下方、MA60 下压且宽度 <35%")

    washout = (
        m["day_pct"] <= -float(params.get("timing.washout_day"))
        and m["amount_ratio"] <= float(params.get("timing.washout_vol"))
        and m["close"] > m["ma_slow"]
        and m["ma_slow_slope"] >= 0
    )
    if washout and crash:
        crash = False
        reasons.append("急跌无量且趋势未坏 → 按洗盘处理，不切急跌")
    if washout:
        reasons.append(f"缩量急跌（量比 {m['amount_ratio']:.2f}）")

    hot = (
        heat >= float(params.get("timing.hot_heat"))
        or m["extension"] >= float(params.get("timing.hot_extension"))
        or m["ret20"] >= float(params.get("timing.hot_ret20"))
        or (m["above_ma20"] >= 0.80 and m["amount_ratio"] >= 1.50)
    )
    trend_ok = (
        heat >= float(params.get("timing.trend_heat"))
        and m["close"] > m["ma_fast"]
        and m["ma_fast_slope"] >= 0
        and m["ma_slow_slope"] >= -0.002
    )

    if crash:
        proposed = "CRASH"
        reasons.append("放量急跌 → 清仓")
    elif hot:
        proposed = "HOT"
        reasons.append(f"市场过热（温度 {heat:.0f}，20 日 {m['ret20']:+.1%}）")
    elif trend_ok:
        proposed = "TREND"
        reasons.append("趋势成立：温度达标、指数在 MA20 上、MA60 不向下")
    elif heat >= float(params.get("timing.cold_heat")) or m["close"] > m["ma_fast"]:
        proposed = "REPAIR"
        reasons.append("修复期：只做平台/坑，不开纯小涨")
    else:
        proposed = "COLD"
        reasons.append(f"冰点（温度 {heat:.0f} < {params.get('timing.cold_heat'):.0f}）")

    state = proposed
    # 迟滞：过热/趋势/修复不要因为一天的温度噪声来回跳
    if prev_state == "HOT" and proposed != "CRASH":
        if heat >= float(params.get("timing.hot_heat")) - float(params.get("timing.hysteresis")):
            state = "HOT"
            reasons.append("过热迟滞维持")
    elif prev_state == "TREND" and proposed in ("REPAIR", "COLD"):
        if heat >= float(params.get("timing.trend_heat")) - float(params.get("timing.hysteresis")) \
                and m["close"] > m["ma_fast"]:
            state = "TREND"
            reasons.append("趋势迟滞维持")
    elif prev_state == "REPAIR" and proposed == "COLD":
        if heat >= float(params.get("timing.cold_heat")) - float(params.get("timing.hysteresis")):
            state = "REPAIR"
            reasons.append("修复迟滞维持")

    if (state != "CRASH" and prev_state is not None and state != prev_state
            and state not in ("HOT", "CRASH") and dwell < int(params.get("timing.min_dwell"))):
        state = prev_state
        reasons.append(f"最小驻留未满（{dwell} < {params.get('timing.min_dwell')}）")

    cap = float(params.get(f"timing.cap_{state.lower()}"))
    market_climax = (
        heat >= float(params.get("timing.market_climax_heat"))
        or (m["above_ma20"] >= 0.85 and m["amount_ratio"] >= 1.50
            and m["extension"] >= float(params.get("timing.hot_extension")) * 0.75)
    )
    if market_climax and cap > 0.30:
        cap = 0.30
        reasons.append("人声鼎沸：总仓位上限压到 30%")

    allowed = list(STATE_SETUPS.get(state, []))
    cold_exception = state == "COLD" and bool(params.get("timing.cold_deep_pit_enabled"))
    if cold_exception:
        allowed = ["DEEP_PIT"]
        reasons.append("冰点深坑例外：只做深坑，最多 1 只，风险预算减半")
    allow_new = (state in ("COLD", "REPAIR", "TREND") and cap > 0
                 and bool(allowed) and not market_climax)
    if not allow_new and state in ("REPAIR", "TREND"):
        reasons.append("过热/人声鼎沸：停止开新仓")
    if state == "COLD" and not allowed:
        reasons.append("冰点：只留底仓，不开新仓")

    return RegimeResult(
        date=market.date,
        state=state,
        state_cn=STATE_CN.get(state, state),
        heat=heat,
        cap=cap,
        allow_new=allow_new,
        allowed_setups=allowed,
        market_climax=market_climax,
        washout=washout,
        reasons=reasons,
        parts=parts,
        metrics=m,
        dwell=dwell,
    )


# ---------------------------------------------------------------------------
# 状态存取
# ---------------------------------------------------------------------------

def load(path: Path | None = None) -> dict:
    path = path or REGIME_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"current": None, "days_in_state": 0, "history": []}


def save(s: dict, path: Path | None = None) -> None:
    path = path or REGIME_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update(market: dm.MarketData, params, state: dict | None = None) -> tuple[dict, RegimeResult]:
    state = state or load()
    date = market.date
    hist = [h for h in state.get("history", []) if h["date"] < date]
    prev = hist[-1] if hist else None
    # 重跑同一日期时，state.current 可能正是当天，不能拿它当“昨日状态”
    prev_state = prev["state"] if prev else None
    prev_dwell = 0
    if prev:
        # 连续相同状态的尾部长度
        for h in reversed(hist):
            if h["state"] == prev_state:
                prev_dwell += 1
            else:
                break

    res = evaluate(market, params, prev_state, prev_dwell)
    if prev_state == res.state:
        res.dwell = prev_dwell + 1
    else:
        res.dwell = 1

    entry = res.to_dict()
    hist.append(entry)
    hist = hist[-260:]
    new_state = dict(state)
    new_state["current"] = res.state
    new_state["days_in_state"] = res.dwell
    new_state["history"] = hist
    return new_state, res


def history_frame(state: dict) -> pd.DataFrame:
    hist = state.get("history", [])
    if not hist:
        return pd.DataFrame()
    rows = []
    for h in hist:
        rows.append({
            "date": h["date"], "state": h["state"], "heat": h.get("heat", 0),
            "cap": h.get("cap", 0), "market_climax": h.get("market_climax", False),
        })
    return pd.DataFrame(rows)
