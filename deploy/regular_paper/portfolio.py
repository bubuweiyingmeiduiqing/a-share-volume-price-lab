# -*- coding: utf-8 -*-
"""
L5/L6/L7：持仓状态、卖出梯度、账户与组合风控。

卖出优先级（从高到低，不可违反）：
  1. 硬止损（跳空低开按开盘价，否则按止损价）
  2. 市场急跌清仓（CRASH）
  3. 放量急跌清仓（急跌放量立马撤）
  4. 无量急跌：只要还在 MA60 上方且 MA60 不向下，按洗盘处理，不卖
  5. 趋势走坏（连续跌破 MA20 / 跌破 MA60）
  6. 人声鼎沸 / 连续大涨：减半或清仓
  7. 冲高回落：减半
  8. 移动止盈
  9. 时间止损

T+1：当日买入不可卖；跌停封死当日不可卖，顺延。
所有价格是不复权实际价，买入加滑点，卖出减滑点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import data as dm
import indicators as ind
from config import STATE_DIR

ACCOUNT_FILE = STATE_DIR / "account.json"


# ---------------------------------------------------------------------------
# 读写 / 估值
# ---------------------------------------------------------------------------

def load(params, path: Path | None = None) -> dict:
    path = path or ACCOUNT_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    cap = float(params.get("portfolio.initial_capital"))
    return {
        "initial": cap,
        "cash": cap,
        "positions": [],
        "trades": [],
        "equity_history": [],
        "peak": cap,
        "halt_until": None,
        "block_entries_on": [],
    }


def save(a: dict, path: Path | None = None) -> None:
    path = path or ACCOUNT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def position_value(p: dict, price: float | None = None) -> float:
    px = price if price is not None else p.get("last_close", p["entry_price"])
    return float(p["shares"]) * float(px)


def equity(a: dict) -> float:
    return float(a["cash"]) + sum(position_value(p) for p in a["positions"])


def invested(a: dict) -> float:
    return sum(float(p["shares"]) * float(p["entry_price"]) for p in a["positions"])


def open_risk(a: dict) -> float:
    return sum(max(0.0, (float(p["entry_price"]) - float(p.get("stop", 0.0))) * float(p["shares"]))
               for p in a["positions"])


def drawdown(a: dict) -> float:
    eq = equity(a)
    peak = max(float(a.get("peak", a["initial"])), eq)
    return (peak - eq) / peak if peak > 0 else 0.0


# ---------------------------------------------------------------------------
# 成本 / 熔断
# ---------------------------------------------------------------------------

def buy_fee(amount: float, params) -> float:
    return (max(float(params.get("portfolio.commission_rate")) * amount,
                float(params.get("portfolio.commission_min")))
            + float(params.get("portfolio.transfer_fee")) * amount)


def sell_fee(amount: float, params) -> float:
    return (max(float(params.get("portfolio.commission_rate")) * amount,
                float(params.get("portfolio.commission_min")))
            + float(params.get("portfolio.transfer_fee")) * amount
            + float(params.get("portfolio.stamp_tax")) * amount)


def circuit(a: dict, date: str, params) -> tuple[bool, float, str]:
    if a.get("halt_until") and date <= a["halt_until"]:
        return False, 0.0, f"回撤熔断中（至 {a['halt_until']}）"
    if date in (a.get("block_entries_on") or []):
        return False, 0.0, "前一日亏损超阈值，今日禁止开仓"

    dd = drawdown(a)
    if dd >= float(params.get("portfolio.dd_stop")):
        return False, 0.0, f"回撤 {dd:.1%} ≥ {params.get('portfolio.dd_stop'):.0%}，停止开仓"
    if dd >= float(params.get("portfolio.dd_halve")):
        return True, 0.5, f"回撤 {dd:.1%}，仓位减半"
    return True, 1.0, ""


def _hold_days(entry: str, cur: str) -> int:
    try:
        ds = dm.trade_dates()
        return sum(1 for d in ds if entry < d <= cur)
    except Exception:
        try:
            return len(pd.bdate_range(pd.to_datetime(entry), pd.to_datetime(cur))) - 1
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# 买入
# ---------------------------------------------------------------------------

def buy(a: dict, cand: dict, price: float, date: str, cap: float,
        cap_mult: float, params, entry_regime: str | None = None) -> dict:
    eq = equity(a)
    if eq <= 0 or price <= 0:
        return {"ok": False, "why": "净值/价格异常"}

    cap_amount = eq * float(cap) * float(cap_mult)
    room = cap_amount - invested(a)
    if room <= 0:
        return {"ok": False, "why": f"仓位已满（上限 {cap_amount:,.0f}，已用 {invested(a):,.0f}）"}

    target = min(float(params.get("portfolio.max_weight")),
                 float(cand["target_weight"]),
                 room / eq)
    if target <= 0:
        return {"ok": False, "why": "目标仓位为 0"}

    # 组合风险预算：所有持仓的“入场价-止损价”风险之和不能超过上限
    stop = float(cand["stop"])
    if stop >= price:
        return {"ok": False, "why": "止损价不低于买入价"}
    risk_new = eq * target * ((price - stop) / price)
    risk_cap = eq * float(params.get("portfolio.max_portfolio_heat"))
    risk_left = risk_cap - open_risk(a)
    if risk_left <= 0:
        return {"ok": False, "why": "组合风险预算已用完"}
    if risk_new > risk_left:
        target *= risk_left / risk_new
        if target <= 0.001:
            return {"ok": False, "why": "风险预算不足"}

    px = price * (1 + float(params.get("portfolio.slippage")))
    budget = min(room, eq * target, float(a["cash"]) * 0.98)
    shares = int(budget / px // 100) * 100
    if shares < 100:
        return {"ok": False, "why": "可用资金不足一手"}

    amount = shares * px
    fee = buy_fee(amount, params)
    while shares >= 100 and amount + fee > a["cash"]:
        shares -= 100
        amount = shares * px
        fee = buy_fee(amount, params) if shares >= 100 else 0.0
    if shares < 100:
        return {"ok": False, "why": "扣费后不足一手"}

    a["cash"] -= amount + fee
    pos = {
        "code": cand["code"], "name": cand["name"], "setup": cand["setup"],
        "industry": cand.get("industry", "未分类"),
        "entry_regime": entry_regime or "",
        "entry_date": date, "entry_price": px, "shares": shares,
        "cost_amount": amount + fee, "fee": fee,
        "entry_fee_per_share": fee / shares,
        "stop": stop, "max_hold": int(params.get("holding.time_stop_days")),
        "partial_done": False, "last_close": px, "peak_price": px,
        "below_ma_days": 0, "entry_score": cand.get("score", 0),
        "target_weight": round(target, 4),
        "risk_amount": round((px - stop) * shares, 2),
    }
    a["positions"].append(pos)
    return {"ok": True, "pos": pos}


# ---------------------------------------------------------------------------
# 卖出
# ---------------------------------------------------------------------------

def _partial_shares(pos: dict, frac: float) -> int:
    n = int(pos["shares"])
    q = (int(n * frac) // 100) * 100
    if q < 100:
        q = 100
    return q if q < n else 0


def sell(a: dict, pos: dict, price: float, date: str, reason: str,
         params, shares: int | None = None) -> dict:
    n = min(int(shares) if shares is not None else int(pos["shares"]), int(pos["shares"]))
    if n <= 0:
        return {"shares": 0, "price": 0.0, "pnl": 0.0, "ret": 0.0}
    px = float(price) * (1 - float(params.get("portfolio.slippage")))
    amount = n * px
    fee = sell_fee(amount, params)
    a["cash"] += amount - fee

    entry_fee = float(pos.get("entry_fee_per_share", 0.0)) * n
    pnl = (px - float(pos["entry_price"])) * n - fee - entry_fee
    basis = float(pos["entry_price"]) * n + entry_fee
    ret = pnl / basis if basis > 0 else 0.0

    a["trades"].append({
        "date": date, "code": pos["code"], "name": pos["name"],
        "setup": pos.get("setup", ""), "industry": pos.get("industry", ""),
        "entry_regime": pos.get("entry_regime", ""),
        "entry_date": pos["entry_date"], "entry_price": round(float(pos["entry_price"]), 3),
        "exit_price": round(px, 3), "shares": n,
        "pnl": round(pnl, 2), "ret": round(ret, 5), "reason": reason,
        "hold_days": _hold_days(pos["entry_date"], date),
    })

    pos["shares"] = int(pos["shares"]) - n
    if pos["shares"] <= 0:
        a["positions"] = [p for p in a["positions"] if p is not pos]
    return {"shares": n, "price": px, "pnl": pnl, "ret": ret}


# ---------------------------------------------------------------------------
# 每日结算
# ---------------------------------------------------------------------------

def _sell_event(a, pos, price, date, reason, params, shares=None) -> dict:
    r = sell(a, pos, price, date, reason, params, shares=shares)
    action = "减半" if pos["shares"] > 0 and shares else "卖出"
    return {"code": pos["code"], "name": pos["name"], "action": action,
            **r, "reason": reason, "date": date}


def settle(a: dict, date: str, market: dm.MarketData, regime, params) -> list[dict]:
    events: list[dict] = []
    for pos in list(a["positions"]):
        if pos["entry_date"] >= date:
            continue
        bar = market.bar(pos["code"])
        df = market.bars.get(pos["code"])
        if bar is None or df is None or len(df) < 65:
            events.append({"code": pos["code"], "name": pos["name"],
                           "action": "停牌/无数据", "date": date})
            continue

        prev_close = float(df["close"].iloc[-2])
        close = float(bar["close"])
        pct = float(bar["pct"]) / 100.0
        if (ind.is_limit_down_locked(pos["code"], pos["name"], bar, prev_close)):
            pos["last_close"] = close
            events.append({"code": pos["code"], "name": pos["name"],
                           "action": "跌停封死，卖出顺延", "date": date})
            continue

        ma5 = ind.sma(df["close"], 5)
        ma10 = ind.sma(df["close"], int(params.get("holding.ma_trail")))
        ma20 = ind.sma(df["close"], int(params.get("holding.ma_trend")))
        ma60 = ind.sma(df["close"], int(params.get("holding.ma_long")))
        ma60_s = ind.sma_series(df["close"], int(params.get("holding.ma_long")))
        sw = int(params.get("timing.slope_window"))
        ma60_slope = (ma60_s.iloc[-1] / ma60_s.iloc[-1 - sw] - 1
                      if len(ma60_s.dropna()) > sw and ma60_s.iloc[-1 - sw] else 0.0)
        vol_ratio = ind.vol_ratio(df, int(params.get("timing.vol_window")))
        upper = ind.upper_shadow_pct(bar, prev_close)
        gain = close / float(pos["entry_price"]) - 1
        hold_days = _hold_days(pos["entry_date"], date)
        big_up = ind.count_big_up(df, int(params.get("holding.climax_big_up_days")),
                                  float(params.get("holding.climax_big_up")))
        ret5 = ind.ret(df, 5)

        pos["peak_price"] = max(float(pos.get("peak_price", pos["entry_price"])),
                                float(bar["high"]))
        peak_gain = float(pos["peak_price"]) / float(pos["entry_price"]) - 1
        pos["below_ma_days"] = (int(pos.get("below_ma_days", 0)) + 1
                                if close < ma20 else 0)

        # 1) 硬止损
        stop = float(pos["stop"])
        if float(bar["open"]) <= stop:
            events.append(_sell_event(a, pos, float(bar["open"]), date,
                                      "止损-跳空低开", params))
            continue
        if float(bar["low"]) <= stop:
            events.append(_sell_event(a, pos, stop, date, "止损", params))
            continue

        # 2) 市场急跌清仓
        if regime.state == "CRASH":
            events.append(_sell_event(a, pos, close, date, "市场急跌清仓", params))
            continue

        # 3) 放量急跌：无条件撤
        if (pct <= -float(params.get("exit.volume_dump_pct"))
                and vol_ratio >= float(params.get("exit.volume_dump_vol"))):
            events.append(_sell_event(a, pos, close, date, "放量急跌", params))
            continue

        # 4) 无量急跌：趋势未坏则按洗盘处理，不触发卖出
        washout = (
            pct <= -float(params.get("holding.washout_drop"))
            and vol_ratio <= float(params.get("holding.washout_vol"))
            and close > ma60
            and ma60_slope >= 0
        )

        # 5) 趋势走坏
        if close < ma60 or int(pos["below_ma_days"]) >= int(params.get("holding.below_ma_days")):
            why = "跌破MA60" if close < ma60 else f"连续{pos['below_ma_days']}日破MA20"
            events.append(_sell_event(a, pos, close, date, f"趋势走坏-{why}", params))
            continue

        # 6) 人声鼎沸 / 连续大涨
        if regime.market_climax and gain > 0 and not pos.get("partial_done"):
            half = _partial_shares(pos, float(params.get("exit.partial_frac")))
            if half:
                ev = _sell_event(a, pos, close, date, "人声鼎沸减半", params, shares=half)
                pos["partial_done"] = True
                events.append(ev)
                continue

        climax = (
            big_up >= int(params.get("holding.climax_big_up_days"))
            or ret5 >= float(params.get("holding.climax_ret5"))
            or (upper >= float(params.get("exit.upper_shadow")) and vol_ratio >= 2.0
                and pct >= float(params.get("selection.small_rise_max")))
        )
        if climax:
            if (ret5 >= float(params.get("holding.climax_ret5_all"))
                    or big_up >= int(params.get("holding.climax_big_up_days")) + 1):
                events.append(_sell_event(a, pos, close, date, "连续大涨清仓", params))
                continue
            if not pos.get("partial_done"):
                half = _partial_shares(pos, float(params.get("exit.partial_frac")))
                if half:
                    ev = _sell_event(a, pos, close, date, "连续大涨减半", params, shares=half)
                    pos["partial_done"] = True
                    events.append(ev)
                    continue

        # 7) 冲高回落（日线代理：开盘半小时猛拉不封板，收盘留长上影）
        failed_spike = (
            float(bar["high"]) >= prev_close * (1 + float(params.get("exit.failed_spike_high")))
            and close <= prev_close * (1 + float(params.get("exit.failed_spike_close")))
            and upper >= float(params.get("exit.upper_shadow"))
            and vol_ratio >= float(params.get("exit.failed_spike_vol"))
            and not ind.is_limit_up(pos["code"], pos["name"], bar, prev_close)
        )
        if failed_spike and not pos.get("partial_done"):
            half = _partial_shares(pos, float(params.get("exit.partial_frac")))
            if half:
                ev = _sell_event(a, pos, close, date, "冲高回落减半", params, shares=half)
                pos["partial_done"] = True
                events.append(ev)
                continue

        # 8) 移动止盈
        if peak_gain >= float(params.get("holding.trail_start")):
            trail = max(float(pos["entry_price"]),
                        float(pos["peak_price"]) * (1 - float(params.get("holding.trail_pct"))))
            if close <= trail:
                events.append(_sell_event(a, pos, close, date, "移动止盈", params))
                continue
            # 浮盈后跌破 MA10，也视为趋势走弱
            if close < ma10 and gain > 0:
                events.append(_sell_event(a, pos, close, date, "破MA10止盈", params))
                continue

        # 9) 时间止损
        if (hold_days >= int(params.get("holding.time_stop_days"))
                and gain < float(params.get("holding.time_stop_min_ret"))):
            events.append(_sell_event(a, pos, close, date, "时间止损", params))
            continue

        pos["last_close"] = close
        events.append({"code": pos["code"], "name": pos["name"],
                       "action": "洗盘持有" if washout else "持有",
                       "date": date, "ret": round(gain, 4), "price": round(close, 3)})
    return events


# ---------------------------------------------------------------------------
# 盯市 / 统计
# ---------------------------------------------------------------------------

def mark(a: dict, date: str, market: dm.MarketData, params) -> dict:
    for pos in a["positions"]:
        bar = market.bar(pos["code"])
        if bar is not None:
            pos["last_close"] = float(bar["close"])
            pos["peak_price"] = max(float(pos.get("peak_price", pos["entry_price"])),
                                    float(bar["high"]))

    eq = equity(a)
    prev = a["equity_history"][-1]["equity"] if a["equity_history"] else a["initial"]
    day_ret = eq / prev - 1 if prev > 0 else 0.0

    row = {"date": date, "equity": round(eq, 2), "cash": round(a["cash"], 2),
           "mv": round(eq - a["cash"], 2), "ret": round(day_ret, 5)}
    if a["equity_history"] and a["equity_history"][-1]["date"] == date:
        a["equity_history"][-1] = row
    else:
        a["equity_history"].append(row)

    a["peak"] = max(float(a.get("peak", a["initial"])), eq)

    if day_ret <= -float(params.get("portfolio.daily_loss")):
        nxt = dm.next_trade_date(date)
        if nxt:
            be = a.setdefault("block_entries_on", [])
            if nxt not in be:
                be.append(nxt)
            a["block_entries_on"] = be[-10:]

    dd = drawdown(a)
    if dd >= float(params.get("portfolio.dd_stop")):
        try:
            ds = dm.trade_dates()
            fut = [x for x in ds if x > date][:int(params.get("portfolio.stop_days"))]
            if fut:
                a["halt_until"] = fut[-1]
        except Exception:
            pass

    return {"equity": eq, "day_ret": day_ret, "drawdown": dd,
            "cash": a["cash"], "mv": eq - a["cash"],
            "invested": invested(a), "open_risk": open_risk(a)}


def stats(a: dict) -> dict:
    tr = a["trades"]
    n = len(tr)
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    eq = equity(a)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    return {
        "n_trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win": sum(t["ret"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t["ret"] for t in losses) / len(losses) if losses else 0.0,
        "avg_ret": sum(t["ret"] for t in tr) / n if n else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else 0.0,
        "total_pnl": eq - float(a["initial"]),
        "total_ret": eq / float(a["initial"]) - 1,
        "max_dd": _max_dd(a),
    }


def _max_dd(a: dict) -> float:
    peak, mdd = -1e18, 0.0
    for h in a["equity_history"]:
        peak = max(peak, float(h["equity"]))
        if peak > 0:
            mdd = max(mdd, (peak - float(h["equity"])) / peak)
    return mdd
