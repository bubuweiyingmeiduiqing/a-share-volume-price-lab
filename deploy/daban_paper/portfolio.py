# -*- coding: utf-8 -*-
"""
L7 组合层 + 账户：槽位、权重、成本、T+1、熔断、逐笔账本。

所有价格都是**不复权实际成交价**，买入按次日开盘价 + 滑点，卖出按触发价 - 滑点。
T+1：当日买入不可卖；跌停封死当日不可卖，顺延。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import data as dm
from config import (
    CIRCUIT_DAILY, CIRCUIT_DD_HALVE, CIRCUIT_DD_STOP, CIRCUIT_STOP_DAYS,
    COMMISSION_MIN, COMMISSION_RATE, INITIAL_CAPITAL, SLOTS, STAMP_TAX_RATE,
    STATE_CAP, STATE_DIR, THEME_CAP, TRANSFER_FEE_RATE, SLIPPAGE,
    ACCEL_VOL_RATIO,
)

ACCOUNT_FILE = STATE_DIR / "account.json"


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def load() -> dict:
    if ACCOUNT_FILE.exists():
        try:
            return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "initial": INITIAL_CAPITAL,
        "cash": INITIAL_CAPITAL,
        "positions": [],
        "trades": [],
        "equity_history": [],
        "peak": INITIAL_CAPITAL,
        "halt_until": None,
        "block_entries_on": [],
    }


def save(a: dict) -> None:
    tmp = ACCOUNT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNT_FILE)


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------

def buy_fee(amount: float) -> float:
    return max(COMMISSION_RATE * amount, COMMISSION_MIN) + TRANSFER_FEE_RATE * amount


def sell_fee(amount: float) -> float:
    return (max(COMMISSION_RATE * amount, COMMISSION_MIN)
            + TRANSFER_FEE_RATE * amount + STAMP_TAX_RATE * amount)


# ---------------------------------------------------------------------------
# 估值
# ---------------------------------------------------------------------------

def position_value(p: dict, price: float | None = None) -> float:
    px = price if price is not None else p.get("last_close", p["entry_price"])
    return p["shares"] * px


def equity(a: dict) -> float:
    return a["cash"] + sum(position_value(p) for p in a["positions"])


def invested(a: dict) -> float:
    return sum(p["shares"] * p["entry_price"] for p in a["positions"])


def drawdown(a: dict) -> float:
    eq = equity(a)
    peak = max(a.get("peak", a["initial"]), eq)
    return (peak - eq) / peak if peak > 0 else 0.0


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def circuit(a: dict, date: str) -> tuple[bool, float, str]:
    """返回 (是否允许开仓, 仓位系数, 说明)。"""
    if a.get("halt_until") and date <= a["halt_until"]:
        return False, 0.0, f"回撤熔断中（至 {a['halt_until']}）"
    if date in (a.get("block_entries_on") or []):
        return False, 0.0, "昨日亏损超 3%，今日禁止开仓"

    dd = drawdown(a)
    if dd >= CIRCUIT_DD_STOP:
        return False, 0.0, f"回撤 {dd:.1%} ≥ {CIRCUIT_DD_STOP:.0%}，停 {CIRCUIT_STOP_DAYS} 日"
    if dd >= CIRCUIT_DD_HALVE:
        return True, 0.5, f"回撤 {dd:.1%}，仓位减半"
    return True, 1.0, ""


# ---------------------------------------------------------------------------
# 买入
# ---------------------------------------------------------------------------

def buy(a: dict, cand: dict, price: float, date: str, state: str,
        cap_mult: float = 1.0) -> dict | None:
    eq = equity(a)
    cap = eq * STATE_CAP.get(state, 0.0) * cap_mult
    room = cap - invested(a)
    if room <= 0:
        return {"ok": False, "why": f"仓位已满（上限 {cap:,.0f}，已用 {invested(a):,.0f}）"}

    budget = min(room, eq * cand["weight"], a["cash"] * 0.98)
    px = price * (1 + SLIPPAGE)
    shares = int(budget / px // 100) * 100
    if shares < 100:
        return {"ok": False, "why": "可用资金不足一手"}

    amount = shares * px
    fee = buy_fee(amount)
    while shares >= 100 and amount + fee > a["cash"]:
        shares -= 100
        amount = shares * px
        fee = buy_fee(amount) if shares >= 100 else 0
    if shares < 100:
        return {"ok": False, "why": "扣费后不足一手"}

    a["cash"] -= amount + fee
    pos = {
        "code": cand["code"], "name": cand["name"], "engine": cand["engine"],
        "industry": cand["industry"], "entry_date": date, "entry_price": px,
        "shares": shares, "cost_amount": amount + fee, "fee": fee,
        "tp": cand["tp"], "sl": cand["sl"], "max_hold": cand["max_hold"],
        "partial_done": False, "last_close": px, "boards_at_entry": cand["boards"],
    }
    a["positions"].append(pos)
    return {"ok": True, "pos": pos}


# ---------------------------------------------------------------------------
# 卖出
# ---------------------------------------------------------------------------

def sell(a: dict, pos: dict, price: float, date: str, reason: str,
         shares: int | None = None) -> dict:
    n = shares if shares is not None else pos["shares"]
    n = min(n, pos["shares"])
    px = price * (1 - SLIPPAGE)
    amount = n * px
    fee = sell_fee(amount)
    a["cash"] += amount - fee

    pnl = (px - pos["entry_price"]) * n - fee
    ret = pnl / (pos["entry_price"] * n) if pos["entry_price"] * n > 0 else 0.0

    a["trades"].append({
        "date": date, "code": pos["code"], "name": pos["name"],
        "engine": pos["engine"], "industry": pos["industry"],
        "entry_date": pos["entry_date"], "entry_price": round(pos["entry_price"], 3),
        "exit_price": round(px, 3), "shares": n,
        "pnl": round(pnl, 2), "ret": round(ret, 5), "reason": reason,
        "hold_days": _hold_days(pos["entry_date"], date),
    })

    pos["shares"] -= n
    if pos["shares"] <= 0:
        a["positions"] = [p for p in a["positions"] if p is not pos]
    return {"shares": n, "price": px, "pnl": pnl, "ret": ret}


def _hold_days(entry: str, cur: str) -> int:
    try:
        ds = dm.trade_dates()
        return sum(1 for d in ds if entry < d <= cur)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 每日结算
# ---------------------------------------------------------------------------

def settle(a: dict, date: str) -> list[dict]:
    """对 entry_date < date 的持仓执行退出规则。T+1 由日期自然满足。"""
    events = []
    for pos in list(a["positions"]):
        if pos["entry_date"] >= date:
            continue
        bar = dm.bar_on(pos["code"], date)
        if bar is None:
            events.append({"code": pos["code"], "action": "停牌/无数据", "date": date})
            continue

        ratio = dm.limit_ratio(pos["code"], pos["name"])
        limit_dn = round(bar["prev_close"] * (1 - ratio) + 1e-8, 2)
        locked_down = bar["high"] <= limit_dn + 0.011
        if locked_down:
            pos["last_close"] = bar["close"]
            events.append({"code": pos["code"], "action": "跌停封死，卖出顺延", "date": date})
            continue

        entry = pos["entry_price"]
        stop = entry * (1 - pos["sl"])
        take = entry * (1 + pos["tp"])
        d = _hold_days(pos["entry_date"], date)
        by_lim = ratio * 100

        vol_ma5 = bar.get("vol_ma5") or 0.0
        accel = bool(bar["pct"] >= by_lim - 0.35 and vol_ma5 > 0
                     and bar["volume"] < ACCEL_VOL_RATIO * vol_ma5)

        # 1) 硬止损（最高优先级）
        if bar["open"] <= stop:
            r = sell(a, pos, bar["open"], date, "止损-跳空低开")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "止损-跳空低开", "date": date})
            continue
        if bar["low"] <= stop:
            r = sell(a, pos, stop, date, "止损")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "止损", "date": date})
            continue

        # 2) 加速态：不设固定止盈，高开卖半仓 + 跟踪
        if accel:
            if not pos["partial_done"] and bar["open"] / bar["prev_close"] - 1 >= 0.05:
                half = max(100, (pos["shares"] // 200) * 100)
                if half < pos["shares"]:
                    r = sell(a, pos, bar["open"], date, "加速态高开卖半仓", shares=half)
                    pos["partial_done"] = True
                    events.append({"code": pos["code"], "name": pos["name"], "action": "减半",
                                   **r, "reason": "加速态高开卖半仓", "date": date})
                    continue
            if d > 1 and bar["close"] < bar["prev_close"]:
                r = sell(a, pos, bar["close"], date, "加速态跌回前收")
                events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                               "reason": "加速态跌回前收", "date": date})
                continue
            pos["last_close"] = bar["close"]
            events.append({"code": pos["code"], "action": "加速态持有", "date": date})
            continue

        # 3) 固定止盈
        if bar["open"] >= take:
            r = sell(a, pos, bar["open"], date, "止盈-高开")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "止盈-高开", "date": date})
            continue
        if bar["high"] >= take:
            r = sell(a, pos, take, date, "止盈")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "止盈", "date": date})
            continue

        # 4) 走弱 / 到期
        if bar["close"] < bar["ma5"]:
            r = sell(a, pos, bar["close"], date, "破MA5")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "破MA5", "date": date})
            continue
        if d >= pos["max_hold"]:
            r = sell(a, pos, bar["close"], date, "到期")
            events.append({"code": pos["code"], "name": pos["name"], "action": "卖出", **r,
                           "reason": "到期", "date": date})
            continue

        pos["last_close"] = bar["close"]
        events.append({"code": pos["code"], "action": "持有", "date": date})
    return events


def mark(a: dict, date: str) -> dict:
    """收盘盯市，写净值曲线。"""
    for pos in a["positions"]:
        bar = dm.bar_on(pos["code"], date)
        if bar is not None:
            pos["last_close"] = bar["close"]

    eq = equity(a)
    prev = a["equity_history"][-1]["equity"] if a["equity_history"] else a["initial"]
    day_ret = eq / prev - 1 if prev > 0 else 0.0

    if a["equity_history"] and a["equity_history"][-1]["date"] == date:
        a["equity_history"][-1] = {"date": date, "equity": round(eq, 2),
                                   "cash": round(a["cash"], 2),
                                   "mv": round(eq - a["cash"], 2),
                                   "ret": round(day_ret, 5)}
    else:
        a["equity_history"].append({"date": date, "equity": round(eq, 2),
                                    "cash": round(a["cash"], 2),
                                    "mv": round(eq - a["cash"], 2),
                                    "ret": round(day_ret, 5)})

    a["peak"] = max(a.get("peak", a["initial"]), eq)

    if day_ret <= CIRCUIT_DAILY:
        nxt = dm.next_trade_date(date)
        if nxt:
            be = a.setdefault("block_entries_on", [])
            if nxt not in be:
                be.append(nxt)
            a["block_entries_on"] = be[-10:]

    dd = drawdown(a)
    if dd >= CIRCUIT_DD_STOP and not (a.get("halt_until") and date <= a["halt_until"]):
        try:
            ds = dm.trade_dates()
            fut = [x for x in ds if x > date][:CIRCUIT_STOP_DAYS]
            if fut:
                a["halt_until"] = fut[-1]
        except Exception:
            pass

    return {"equity": eq, "day_ret": day_ret, "drawdown": dd,
            "cash": a["cash"], "mv": eq - a["cash"]}


def stats(a: dict) -> dict:
    tr = a["trades"]
    n = len(tr)
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    eq = equity(a)
    return {
        "n_trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win": sum(t["ret"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t["ret"] for t in losses) / len(losses) if losses else 0.0,
        "avg_ret": sum(t["ret"] for t in tr) / n if n else 0.0,
        "profit_factor": (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)))
        if losses and sum(t["pnl"] for t in losses) != 0 else 0.0,
        "total_pnl": eq - a["initial"],
        "total_ret": eq / a["initial"] - 1,
        "max_dd": _max_dd(a),
    }


def _max_dd(a: dict) -> float:
    peak, mdd = -1e18, 0.0
    for h in a["equity_history"]:
        peak = max(peak, h["equity"])
        if peak > 0:
            mdd = max(mdd, (peak - h["equity"]) / peak)
    return mdd
