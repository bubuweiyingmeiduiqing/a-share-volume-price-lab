#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账户速查：净值、持仓、逐笔、状态分布。"""

from __future__ import annotations

import json
from collections import Counter

import data as dm
import portfolio as pf
import regime as rg
from config import eng


def main() -> None:
    a = pf.load()
    s = pf.stats(a)
    eq = pf.equity(a)
    print("=" * 62)
    print(f"净值 {eq:,.2f}   累计 {s['total_ret']:+.2%}   最大回撤 {s['max_dd']:.2%}")
    print(f"现金 {a['cash']:,.0f}   持仓 {len(a['positions'])} 只   "
          f"仓位 {(eq - a['cash']) / eq:.0%}" if eq else "")
    print(f"交易 {s['n_trades']} 笔   胜率 {s['win_rate']:.1%}   "
          f"盈亏比 {s['profit_factor']:.2f}   平均单笔 {s['avg_ret']:+.2%}")
    print("-" * 62)

    if a["positions"]:
        print("持仓：")
        for p in a["positions"]:
            cur = p.get("last_close", p["entry_price"])
            print(f"  {p['code']} {p['name']:<6s} {eng(p['engine'])} 成本 {p['entry_price']:7.3f} "
                  f"现价 {cur:7.3f} {(cur / p['entry_price'] - 1):+7.2%}  {p['industry']}")

    tr = a["trades"]
    if tr:
        print("-" * 62)
        print(f"最近 15 笔：")
        for t in tr[-15:]:
            print(f"  {t['date']} {t['code']} {t['name']:<6s} {eng(t['engine'])} "
                  f"{t['ret']:+7.2%}  {t['reason']}")
        by_engine = Counter(t["engine"] for t in tr)
        print("-" * 62)
        for e, n in by_engine.items():
            sub = [t for t in tr if t["engine"] == e]
            w = sum(1 for t in sub if t["pnl"] > 0) / len(sub)
            avg = sum(t["ret"] for t in sub) / len(sub)
            print(f"  {eng(e)}: {n} 笔  胜率 {w:.0%}  平均 {avg:+.2%}")

    h = a["equity_history"]
    if h:
        print("-" * 62)
        print(f"净值曲线：{len(h)} 个交易日，{h[0]['date']} ~ {h[-1]['date']}")
        for x in h[-10:]:
            print(f"  {x['date']}  {x['equity']:>12,.0f}  {x['ret']:+6.2%}")

    r = rg.load()
    print("-" * 62)
    hist = r.get("history", [])
    print(f"市场状态：{r.get('current')} 第 {r.get('days_in_state')} 天   "
          f"历史 {len(hist)} 天  {dict(Counter(x['state'] for x in hist))}")
    print("=" * 62)


if __name__ == "__main__":
    main()
