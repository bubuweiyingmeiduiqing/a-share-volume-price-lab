#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账户速查：净值、持仓、逐笔、状态分布、参数摘要。"""

from __future__ import annotations

from collections import Counter

import deploy_check
import portfolio as pf
import regime as rg
from review import load_counter
from config import SETUP_CN, STATE_CN, eng_label, load_params


def main(state_dir=None) -> None:
    from pathlib import Path
    from config import STATE_DIR
    state_dir = Path(state_dir or STATE_DIR)
    params = load_params()
    a = pf.load(params, state_dir / "account.json")
    s = pf.stats(a)
    eq = pf.equity(a)
    print("=" * 70)
    print(f"{params.summary()}")
    print(f"净值 {eq:,.2f}   累计 {s['total_ret']:+.2%}   最大回撤 {s['max_dd']:.2%}")
    print(f"现金 {a['cash']:,.0f}   持仓 {len(a['positions'])} 只   "
          f"仓位 {(eq - a['cash']) / eq:.0%}" if eq else "")
    print(f"交易 {s['n_trades']} 笔   胜率 {s['win_rate']:.1%}   "
          f"盈亏比 {s['profit_factor']:.2f}   平均单笔 {s['avg_ret']:+.2%}")
    print(f"未触发风险 {pf.open_risk(a):,.0f}（{pf.open_risk(a) / eq:.1%}）" if eq else "")
    print("-" * 70)

    if a["positions"]:
        print("持仓：")
        for p in a["positions"]:
            cur = p.get("last_close", p["entry_price"])
            print(f"  {p['code']} {p['name']:<6s} {eng_label(p.get('setup', ''))} "
                  f"成本 {p['entry_price']:7.3f} 现价 {cur:7.3f} "
                  f"{(cur / p['entry_price'] - 1):+7.2%}  止损 {p['stop']:.3f}  "
                  f"{p.get('industry', '')}")

    tr = a["trades"]
    if tr:
        print("-" * 70)
        print("最近 15 笔：")
        for t in tr[-15:]:
            print(f"  {t['date']} {t['code']} {t['name']:<6s} "
                  f"{eng_label(t.get('setup', ''))} {t['ret']:+7.2%}  {t['reason']}")
        by_setup = Counter(t["setup"] for t in tr)
        print("-" * 70)
        for setup, n in by_setup.items():
            sub = [t for t in tr if t["setup"] == setup]
            w = sum(1 for t in sub if t["pnl"] > 0) / len(sub)
            avg = sum(t["ret"] for t in sub) / len(sub)
            print(f"  {SETUP_CN.get(setup, setup)}: {n} 笔  胜率 {w:.0%}  平均 {avg:+.2%}")

    h = a["equity_history"]
    if h:
        print("-" * 70)
        print(f"净值曲线：{len(h)} 个交易日，{h[0]['date']} ~ {h[-1]['date']}")
        for x in h[-10:]:
            print(f"  {x['date']}  {x['equity']:>12,.0f}  {x['ret']:+6.2%}")

    r = rg.load(state_dir / "regime.json")
    print("-" * 70)
    hist = r.get("history", [])
    print(f"市场状态：{STATE_CN.get(r.get('current'), r.get('current'))} "
          f"第 {r.get('days_in_state')} 天   历史 {len(hist)} 天   "
          f"{dict(Counter(x['state'] for x in hist))}")
    if hist:
        last = hist[-1]
        print(f"最近：{last['date']} 温度 {last.get('heat')} "
              f"仓位上限 {last.get('cap'):.0%} "
              f"人声鼎沸={last.get('market_climax')}")

    c = load_counter(state_dir)
    every = max(1, int(params.rt("review", "every_n_close_pushes", default=5)))
    nxt = ((int(c.get("close_pushes", 0)) // every) + 1) * every
    print(f"推送：收盘 {c.get('close_pushes', 0)} 次｜盘前 {c.get('premarket_pushes', 0)} 次｜"
          f"下次复盘在收盘推送 #{nxt}")
    dc = deploy_check.check(state_dir.parent)
    if dc.get("present"):
        flag = "✅" if dc.get("ok") else "⚠️"
        print(f"部署：commit {dc.get('commit', '')[:8]}｜{dc.get('deployed_at', '')}｜"
              f"代码清单 {flag} {deploy_check.summary(dc)}")
    else:
        print(f"部署：⚠️ {deploy_check.summary(dc)}")
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=None)
    a = ap.parse_args()
    main(a.state_dir)
