# -*- coding: utf-8 -*-
"""
纯函数指标层：不读文件、不联网，方便单测与复用。

所有函数默认输入按日期升序的日线 DataFrame，至少包含：
date open high low close volume amount pct turnover
"""

from __future__ import annotations

import math

import pandas as pd


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        if b is None or not math.isfinite(float(b)) or abs(float(b)) < 1e-12:
            return default
        return float(a) / float(b)
    except (TypeError, ValueError):
        return default


def last(series: pd.Series, n: int = 1, default: float = 0.0) -> float:
    if series is None or len(series) < n:
        return default
    v = series.iloc[-n]
    try:
        return float(v) if math.isfinite(float(v)) else default
    except (TypeError, ValueError):
        return default


def sma(series: pd.Series, n: int) -> float:
    if series is None or len(series) < n or n <= 0:
        return 0.0
    return float(series.tail(n).mean())


def sma_series(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def slope_pct(series: pd.Series, n: int) -> float:
    """最近 n 日的变化率（不是回归斜率），足够判断均线走平/上翘/下压。"""
    if series is None or len(series) <= n or n <= 0:
        return 0.0
    prev = float(series.iloc[-1 - n])
    if abs(prev) < 1e-12:
        return 0.0
    return float(series.iloc[-1]) / prev - 1.0


def ret(df: pd.DataFrame, n: int) -> float:
    if df is None or len(df) <= n or n <= 0:
        return 0.0
    prev = float(df["close"].iloc[-1 - n])
    if abs(prev) < 1e-12:
        return 0.0
    return float(df["close"].iloc[-1]) / prev - 1.0


def vol_ratio(df: pd.DataFrame, n: int = 20) -> float:
    """当日量 / 之前 n 日均量（不含当日），避免当日巨量把基准抬高。"""
    if df is None or len(df) < n + 1 or n <= 0:
        return 1.0
    base = float(df["volume"].iloc[-n - 1:-1].mean())
    return safe_div(float(df["volume"].iloc[-1]), base, 1.0)


def amount_ratio(df: pd.DataFrame, n: int = 20) -> float:
    if df is None or len(df) < n + 1 or n <= 0:
        return 1.0
    base = float(df["amount"].iloc[-n - 1:-1].mean())
    return safe_div(float(df["amount"].iloc[-1]), base, 1.0)


def vol_ma(df: pd.DataFrame, n: int, include_today: bool = True) -> float:
    if df is None or len(df) < n:
        return 0.0
    s = df["volume"] if include_today else df["volume"].iloc[:-1]
    return float(s.tail(n).mean())


def atr(df: pd.DataFrame, n: int = 14) -> float:
    if df is None or len(df) < n + 1:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


# ---------------------------------------------------------------------------
# K 线形态
# ---------------------------------------------------------------------------

def upper_shadow_pct(row: pd.Series, prev_close: float) -> float:
    """上影线 / 昨收：衡量冲高回落，而不是用 / 最高价（会低估长上影）。"""
    if not prev_close or prev_close <= 0:
        return 0.0
    body_top = max(float(row["open"]), float(row["close"]))
    return max(0.0, (float(row["high"]) - body_top) / prev_close)


def lower_shadow_pct(row: pd.Series, prev_close: float) -> float:
    if not prev_close or prev_close <= 0:
        return 0.0
    body_bottom = min(float(row["open"]), float(row["close"]))
    return max(0.0, (body_bottom - float(row["low"])) / prev_close)


def close_position(row: pd.Series) -> float:
    """收盘在当日振幅中的位置，0=最低，1=最高。"""
    hi, lo, c = float(row["high"]), float(row["low"]), float(row["close"])
    if hi - lo < 1e-12:
        return 0.5
    return max(0.0, min(1.0, (c - lo) / (hi - lo)))


def body_pct(row: pd.Series, prev_close: float) -> float:
    if not prev_close or prev_close <= 0:
        return 0.0
    return (float(row["close"]) - float(row["open"])) / prev_close


def count_big_up(df: pd.DataFrame, n: int, threshold: float) -> int:
    if df is None or len(df) < n:
        return 0
    return int((df["pct"].tail(n) >= threshold * 100).sum())


def consecutive_up_days(df: pd.DataFrame) -> int:
    if df is None or len(df) < 2:
        return 0
    n = 0
    for i in range(len(df) - 1, 0, -1):
        if float(df["close"].iloc[i]) > float(df["close"].iloc[i - 1]):
            n += 1
        else:
            break
    return n


def small_rise_stats(df: pd.DataFrame, window: int, min_pct: float,
                     max_pct: float) -> dict:
    """
    连续小涨统计：
      count  窗口内满足“单日涨幅在 [min, max] 且收盘高于昨收”的天数
      cum    窗口累计涨幅
      today  今天是否满足
      last_n 从今天往前连续满足的天数
    """
    if df is None or len(df) <= window:
        return {"count": 0, "cum": 0.0, "today": False, "last_n": 0}
    tail = df.tail(window)
    lo, hi = min_pct * 100, max_pct * 100
    ok = (tail["pct"] >= lo) & (tail["pct"] <= hi) & (tail["close"] > tail["close"].shift(1))
    # shift 后第一行会 NaN，用 close > open 兜底（窗口首日）
    ok = ok | ((tail["pct"] >= lo) & (tail["pct"] <= hi) & (tail["close"] > tail["open"]))
    count = int(ok.sum())
    cum = ret(df, window)
    last_n = 0
    vals = ok.tolist()
    for v in reversed(vals):
        if v:
            last_n += 1
        else:
            break
    return {
        "count": count,
        "cum": cum,
        "today": bool(vals[-1]) if vals else False,
        "last_n": last_n,
    }


# ---------------------------------------------------------------------------
# 箱体 / 坑
# ---------------------------------------------------------------------------

def box(df: pd.DataFrame, window: int) -> dict:
    if df is None or len(df) < window:
        return {"high": 0.0, "low": 0.0, "range": 1.0, "mid": 0.0}
    tail = df.tail(window)
    hi = float(tail["high"].max())
    lo = float(tail["low"].min())
    return {
        "high": hi,
        "low": lo,
        "range": safe_div(hi - lo, lo, 1.0),
        "mid": (hi + lo) / 2.0,
    }


def pit_stats(df: pd.DataFrame, lookback: int, ma_n: int = 5) -> dict:
    """
    找最近 lookback 日内的“高点 -> 低点”坑：
      peak/trough/depth  区间最高、最低、跌幅（负数）
      down_vol_ratio     下跌日平均量 / 20 日均量（不含当日）
      recover            收盘是否已站上 MA5
    """
    if df is None or len(df) < max(lookback + 1, 21):
        return {"peak": 0.0, "trough": 0.0, "depth": 0.0, "down_vol_ratio": 1.0,
                "recover": False}
    tail = df.tail(lookback)
    hi_idx = int(tail["high"].idxmax())
    # 只在最高点之后找最低点，避免把“先跌后涨”算成坑
    after = df.loc[hi_idx:]
    lo_idx = int(after["low"].idxmin())
    peak = float(df.loc[hi_idx, "high"])
    trough = float(df.loc[lo_idx, "low"])
    depth = safe_div(trough - peak, peak, 0.0)

    seg = df.loc[hi_idx:lo_idx]
    down = seg[seg["pct"] < 0]
    base = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else float(df["volume"].mean())
    down_vol = float(down["volume"].mean()) if len(down) else base
    down_vol_ratio = safe_div(down_vol, base, 1.0)

    ma_reclaim = sma(df["close"], ma_n)
    recover = float(df["close"].iloc[-1]) > ma_reclaim > 0
    return {"peak": peak, "trough": trough, "depth": depth,
            "down_vol_ratio": down_vol_ratio, "recover": bool(recover)}


# ---------------------------------------------------------------------------
# 涨跌停制度
# ---------------------------------------------------------------------------

def limit_ratio(code: str, name: str = "") -> float:
    if "ST" in str(name).upper():
        return 0.05
    c = str(code).zfill(6)
    if c.startswith("688") or c.startswith("300") or c.startswith("301"):
        return 0.20
    if c.startswith("8") or c.startswith("4") or c.startswith("920"):
        return 0.30
    return 0.10


def limit_up_price(prev_close: float, ratio: float) -> float:
    return round(float(prev_close) * (1 + ratio) + 1e-8, 2)


def limit_down_price(prev_close: float, ratio: float) -> float:
    return round(float(prev_close) * (1 - ratio) + 1e-8, 2)


def is_bse(code: str) -> bool:
    c = str(code).zfill(6)
    return c.startswith("8") or c.startswith("4") or c.startswith("920")


def is_limit_up(code: str, name: str, row: pd.Series, prev_close: float) -> bool:
    lp = limit_up_price(prev_close, limit_ratio(code, name))
    return float(row["close"]) >= lp - 0.011


def is_limit_down_locked(code: str, name: str, row: pd.Series, prev_close: float) -> bool:
    ld = limit_down_price(prev_close, limit_ratio(code, name))
    return float(row["high"]) <= ld + 0.011


def pct_change(prev_close: float, price: float) -> float:
    return safe_div(price - prev_close, prev_close, 0.0)
