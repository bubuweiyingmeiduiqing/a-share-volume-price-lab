# -*- coding: utf-8 -*-
"""
合成行情：给 selftest / --demo 用，不联网、可复现。

它不是回测数据，只用来验证“八条口诀是否被正确编码”：
  横盘缩量 + 连续小涨   → 应该出候选
  深坑无量 + 趋势未坏   → 应该出候选
  5 日 +25% 的竖        → 必须被排除
  无量急跌              → 持仓不卖
  放量急跌              → 持仓清仓
  连续大涨              → 持仓减仓/清仓
  冲高回落              → 持仓减半
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import data as dm

END = "20261106"


def _frame(dates, closes, volumes=None, opens=None, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)
    if opens is None:
        opens = np.empty(n)
        opens[0] = closes[0]
        opens[1:] = closes[:-1] * 1.001
    opens = np.asarray(opens, dtype=float)
    if highs is None:
        highs = np.maximum(opens, closes) * 1.004
    highs = np.asarray(highs, dtype=float)
    if lows is None:
        lows = np.minimum(opens, closes) * 0.996
    lows = np.asarray(lows, dtype=float)
    pct = np.zeros(n)
    pct[1:] = (closes[1:] / closes[:-1] - 1) * 100
    return pd.DataFrame({
        "date": dates,
        "open": np.round(opens, 2),
        "high": np.round(highs, 2),
        "low": np.round(lows, 2),
        "close": np.round(closes, 2),
        "volume": volumes,
        "amount": volumes * closes * 100,
        "pct": np.round(pct, 3),
        "turnover": np.full(n, 2.5),
    })


def _dates(n: int) -> list[str]:
    return [d.strftime("%Y%m%d") for d in pd.bdate_range(end=pd.to_datetime(END), periods=n)]


def _noise(rng, n, scale=0.0015) -> np.ndarray:
    return rng.normal(0, scale, n)


def demo_market(params=None) -> dm.MarketData:
    rng = np.random.default_rng(20261106)
    dates = _dates(160)
    n = len(dates)

    # 指数：慢牛 + 末期温和放量，温度落在 TREND 而不是 HOT
    idx_close = 3500 * np.exp(np.linspace(0, 0.11, n)) + np.cumsum(rng.normal(0, 3.2, n))
    idx_close = np.maximum(idx_close, 3000)
    idx_vol = np.linspace(2.0e8, 2.6e8, n) * (1 + rng.normal(0, 0.04, n))
    idx_vol[-1] *= 1.05
    index = _frame(dates, idx_close, idx_vol)

    bars: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict] = {}

    # 1) 横盘 + 连续小涨
    c = np.empty(n)
    for i in range(n):
        if i < 100:
            c[i] = 10.0 * np.exp(0.0023 * i)
        elif i < 155:
            c[i] = 13.05 + 0.12 * np.sin(i / 2.7)
        else:
            c[i] = [13.18, 13.34, 13.45, 13.65, 13.80][i - 155]
    c += _noise(rng, n, 0.02)
    v = np.full(n, 1_200_000.0)
    v[100:155] = 1_050_000.0
    v[155:] = [760_000, 780_000, 790_000, 810_000, 830_000]
    bars["600001"] = _frame(dates, c, v)
    meta["600001"] = {"name": "横盘小涨", "industry": "行业A", "float_mv": 8e9, "amount": 2e8}

    # 2) 深坑：急涨后缩量回撤 27%，再站上 MA5
    c = np.empty(n)
    c[:36] = 8.0
    c[36:136] = 8.0 * np.exp(np.linspace(0, 0.405, 100))
    c[136:151] = 12.0 * np.exp(np.linspace(0, 0.51, 15))
    c[151:158] = np.linspace(20.0, 14.5, 7)
    c[158:] = [15.00, 15.65]
    c += _noise(rng, n, 0.015)
    v = np.full(n, 1_500_000.0)
    v[151:158] = 700_000.0
    v[158:] = [1_400_000, 1_600_000]
    bars["600002"] = _frame(dates, c, v)
    meta["600002"] = {"name": "深坑反转", "industry": "行业B", "float_mv": 9e9, "amount": 2e8}

    # 3) 竖：最后 5 天 +25%，必须排除
    c = np.full(n, 10.0)
    for i in range(155, n):
        c[i] = c[i - 1] * 1.05
    c += _noise(rng, n, 0.01)
    bars["600003"] = _frame(dates, c, np.full(n, 2_000_000.0))
    meta["600003"] = {"name": "加速竖", "industry": "行业C", "float_mv": 7e9, "amount": 3e8}

    # 4) 无量急跌：持仓不卖
    c = 10.0 * np.exp(np.linspace(0, 0.405, n))
    c[-1] = c[-2] * 0.96
    v = np.linspace(1_000_000, 1_600_000, n)
    v[-1] = v[-2] * 0.50
    bars["600004"] = _frame(dates, c, v)
    meta["600004"] = {"name": "无量洗盘", "industry": "行业D", "float_mv": 7e9, "amount": 1.8e8}

    # 5) 放量急跌：持仓清仓
    c = 10.0 * np.exp(np.linspace(0, 0.405, n))
    c[-1] = c[-2] * 0.95
    v = np.linspace(1_000_000, 1_600_000, n)
    v[-1] = v[-2] * 2.20
    bars["600005"] = _frame(dates, c, v)
    meta["600005"] = {"name": "放量急跌", "industry": "行业E", "float_mv": 7e9, "amount": 2.2e8}

    # 6) 连续大涨：持仓清仓
    c = 10.0 * np.exp(np.linspace(0, 0.26, n - 10))
    for _ in range(10):
        c = np.append(c, c[-1] * 1.05)
    v = np.linspace(1_000_000, 2_000_000, n)
    v[-5:] *= 1.6
    bars["600006"] = _frame(dates, c[:n], v[:n])
    meta["600006"] = {"name": "连续大涨", "industry": "行业F", "float_mv": 8e9, "amount": 3e8}

    # 7) 冲高回落：长上影 + 放量，持仓减半
    c = 10.0 * np.exp(np.linspace(0, 0.37, n))
    c[-1] = c[-2] * 1.005
    opens = None
    highs = None
    lows = None
    f = _frame(dates, c, np.full(n, 1_400_000.0))
    prev = float(f["close"].iloc[-2])
    last_open = prev * 1.008
    last_high = prev * 1.062
    last_low = prev * 0.995
    f.loc[f.index[-1], "open"] = round(last_open, 2)
    f.loc[f.index[-1], "high"] = round(last_high, 2)
    f.loc[f.index[-1], "low"] = round(last_low, 2)
    f.loc[f.index[-1], "close"] = round(prev * 1.005, 2)
    f.loc[f.index[-1], "pct"] = round((prev * 1.005 / prev - 1) * 100, 3)
    v2 = f["volume"].to_numpy().copy()
    v2[-1] = v2[-2] * 2.4
    f["volume"] = v2
    f["amount"] = f["volume"] * f["close"] * 100
    bars["600007"] = f
    meta["600007"] = {"name": "冲高回落", "industry": "行业G", "float_mv": 8e9, "amount": 2.5e8}

    # 8) 纯横盘（不做小涨叠加）
    c = np.empty(n)
    c[:130] = 10.0 * np.exp(np.linspace(0, 0.25, 130))
    c[130:] = 12.8 + 0.10 * np.sin(np.arange(n - 130) / 3.0)
    c += _noise(rng, n, 0.015)
    v = np.full(n, 1_000_000.0)
    v[-20:-5] *= 0.85
    v[-5:] *= 0.55
    bars["600008"] = _frame(dates, c, v)
    meta["600008"] = {"name": "纯横盘", "industry": "行业A", "float_mv": 8e9, "amount": 1.5e8}

    return dm.MarketData(date=END, benchmark="000300", index=index,
                         bars=bars, meta=meta, source="synthetic")


def seed_demo_account(params, market: dm.MarketData, state_dir: Path,
                      force: bool = False) -> None:
    """给演示账户放入 4 只持仓，分别覆盖洗盘/放量/大涨/冲高。"""
    path = Path(state_dir) / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return
    cap = float(params.get("portfolio.initial_capital"))
    positions = []
    for code in ("600004", "600005", "600006", "600007"):
        df = market.bars[code]
        entry_date = str(df["date"].iloc[-11])
        entry = float(df["close"].iloc[-11]) * 1.001
        shares = 10_000
        stop = entry * (1 - float(params.get("entry.stop_loss")))
        positions.append({
            "code": code, "name": market.meta[code]["name"],
            "setup": "PLATFORM", "industry": market.meta[code]["industry"],
            "entry_regime": "TREND",
            "entry_date": entry_date, "entry_price": round(entry, 3),
            "shares": shares, "cost_amount": round(entry * shares, 2), "fee": 0.0,
            "entry_fee_per_share": 0.0, "stop": round(stop, 3),
            "max_hold": int(params.get("holding.time_stop_days")),
            "partial_done": False, "last_close": round(float(df["close"].iloc[-2]), 3),
            "peak_price": round(float(df["high"].iloc[-11:].max()), 3),
            "below_ma_days": 0, "entry_score": 70,
            "target_weight": 0.2, "risk_amount": round((entry - stop) * shares, 2),
        })
    invested = sum(p["cost_amount"] for p in positions)
    account = {
        "initial": cap, "cash": cap - invested, "positions": positions,
        "trades": [], "equity_history": [], "peak": cap,
        "halt_until": None, "block_entries_on": [],
    }
    path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
