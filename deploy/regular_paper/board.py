# -*- coding: utf-8 -*-
"""
收盘研究看板：大盘环境 / 板块资金扫描 / 核心龙头筛选。

定位：给收盘推送补上“研究上下文”，**不参与交易决策**。
明日计划仍由 strategy.py 按口诀生成；这里列的股票不是成交信号。

数据源（都能降级，任何一节失败只打印 warn 并跳过，不让收盘整轮失败）：
  - 指数：复用 data.index_daily（东财 → 新浪 → 腾讯 → 收盘后实时行情补齐）
  - 全市场：spot_snapshot 快照 + 东财涨停/跌停池（失败则用快照涨跌幅近似）
  - 板块资金：同花顺概念/行业资金流（东财板块接口在 VPS 上经常断连）
  - 个股资金：同花顺个股资金流（5 日排行 + 即时），再与扫描池、流通市值对齐

口径（和看板截图对齐，写进报告脚注）：
  - 资金单位亿元；“归一化资金”= 5 日净额 / 板块公司家数（亿元 // 家数）
  - “5 日资金/流通市值”= 5 日净额 ÷ 流通市值
  - 板块“宽度”这里用板块涨跌幅与资金方向代理，不做全成分股 MA20 宽度扫描
    （那需要 600+ 板块 × 成分股日线，收盘这轮跑不完）
"""

from __future__ import annotations

import datetime as dt

import akshare as ak
import pandas as pd

import data as dm

DEFAULT_INDICES = [["000001", "上证指数"], ["000300", "沪深300"],
                   ["000852", "中证1000"], ["000688", "科创50"]]
DEFAULT_WATCH = ["银行", "证券", "保险", "白酒", "半导体",
                 "人形机器人", "PCB", "算力租赁", "液冷服务器"]


# ---------------------------------------------------------------------------
# 数值解析
# ---------------------------------------------------------------------------

def _to_yi(v) -> float:
    """'1.25亿' / '-5602.50万' / '0.65' -> 亿元。"""
    s = str(v).strip().replace(",", "")
    if not s or s in ("--", "-", "nan", "None"):
        return 0.0
    mult = 1.0
    if s.endswith("亿"):
        s = s[:-1]
    elif s.endswith("万"):
        s, mult = s[:-1], 1e-4
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def _pct(v) -> float:
    s = str(v).strip().replace("%", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except ValueError:
        return 0.0


def _rank(series: pd.Series) -> pd.Series:
    """横截面百分位（0-100），用于拼研究评分。"""
    if series is None or len(series) == 0:
        return pd.Series(dtype=float)
    return series.astype(float).rank(pct=True) * 100.0


def _fmt_money_yi(v: float) -> str:
    return f"{v:+,.2f}亿"


# ---------------------------------------------------------------------------
# 一、大盘环境
# ---------------------------------------------------------------------------

def index_rows(date: str, params) -> list[dict]:
    """多指数收盘/5日/20日/MA20 状态。数据不足或断连的指数直接跳过。"""
    rows: list[dict] = []
    for item in params.rt("board", "indices", default=DEFAULT_INDICES) or []:
        try:
            code, name = str(item[0]), str(item[1])
        except (TypeError, IndexError):
            continue
        try:
            df = dm.index_daily(code, date, 60)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 看板指数 {code} 拉取失败：{e}")
            continue
        if df is None or df.empty or len(df) < 25:
            print(f"[warn] 看板指数 {code} 历史不足，跳过")
            continue
        if str(df["date"].iloc[-1]) != dm.norm_date(date):
            print(f"[warn] 看板指数 {code} 停在 {df['date'].iloc[-1]}，跳过")
            continue
        close = df["close"].astype(float)
        last = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        ma20_prev = float(close.iloc[-25:-5].tail(20).mean())
        rows.append({
            "name": name,
            "code": code,
            "close": last,
            "ret5": (last / float(close.iloc[-6]) - 1) * 100,
            "ret20": (last / float(close.iloc[-21]) - 1) * 100,
            "ma20": ma20,
            "above": last > ma20,
            "ext": (last / ma20 - 1) * 100,
            "ma20_chg": (ma20 / ma20_prev - 1) * 100 if ma20_prev else 0.0,
        })
    return rows


def limit_counts(date: str, rows: list[dict] | None = None) -> tuple[int, int, str]:
    """涨停/跌停家数：先东财涨跌停池，失败再用快照涨跌幅近似（会被标成“近似”）。"""
    d = dm.norm_date(date)
    zt = dt_ = 0
    ok = True
    try:
        zt = len(ak.stock_zt_pool_em(date=d))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 涨停池不可用：{e}")
        ok = False
    try:
        dt_ = len(ak.stock_zt_pool_dtgc_em(date=d))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 跌停池不可用：{e}")
        ok = False
    if ok:
        return zt, dt_, "东财涨跌停池"
    if not rows:
        return zt, dt_, "不可用"
    zt = sum(1 for r in rows if _limit_hit(r, up=True))
    dt_ = sum(1 for r in rows if _limit_hit(r, up=False))
    return zt, dt_, "快照涨跌幅近似"


def _limit_hit(r: dict, up: bool) -> bool:
    """按板块涨跌幅限制近似判断涨跌停（主板 10%、创业/科创 20%、北交所 30%、ST 5%）。"""
    code = str(r.get("code", ""))
    name = str(r.get("name", "")).upper()
    pct = float(r.get("pct", 0) or 0)
    if code.startswith(("4", "8", "920")):
        lim = 30.0
    elif code.startswith(("30", "68")):
        lim = 20.0
    else:
        lim = 5.0 if "ST" in name else 10.0
    return pct >= lim - 0.2 if up else pct <= -lim + 0.2


def market_rows(market: dm.MarketData, date: str, params) -> dict:
    """全市场统计：沪深成交额、涨跌家数、涨停跌停、扫描池 5 日线宽度。"""
    out: dict = {}
    try:
        rows = dm.spot_snapshot(int(params.rt("data", "spot_cache_minutes", default=60)))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 看板快照不可用：{e}")
        rows = []
    cur = [r for r in rows if str(r.get("snap_date")) == dm.norm_date(date)] or rows
    if cur:
        sh_sz = [r for r in cur if not str(r.get("code", "")).startswith(("4", "8", "920"))]
        amt = sum(float(r.get("amount", 0) or 0) for r in sh_sz)
        up = sum(1 for r in sh_sz if float(r.get("pct", 0) or 0) > 0)
        down = sum(1 for r in sh_sz if float(r.get("pct", 0) or 0) < 0)
        zt, dt_, src = limit_counts(date, sh_sz)
        out.update({"amount_yi": amt / 1e8, "n": len(sh_sz), "up": up, "down": down,
                    "zt": zt, "dt": dt_, "limit_src": src})
    # 扫描池 5 日线宽度（n 会写进报告，避免被当成全市场）
    bars = [df for df in market.bars.values()
            if df is not None and len(df) >= 6 and str(df["date"].iloc[-1]) == dm.norm_date(date)]
    if bars:
        above5 = sum(1 for df in bars
                     if float(df["close"].iloc[-1]) > float(df["close"].tail(5).mean()))
        out.update({"pool_n": len(bars), "pool_above5": above5 / len(bars) * 100})
    return out


def index_section(market: dm.MarketData, date: str, params, res=None) -> list[str]:
    rows = index_rows(date, params)
    if not rows:
        return []
    L = ["**大盘环境**", ""]
    L.append("| 指数 | 收盘 | 5日 | 20日 | MA20 | 距MA20 | MA20较5日前 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['name']} | {r['close']:,.2f} | {r['ret5']:+.2f}% | "
                 f"{r['ret20']:+.2f}% | {'上方' if r['above'] else '下方'} | "
                 f"{r['ext']:+.2f}% | {r['ma20_chg']:+.2f}% |")
    below = sum(1 for r in rows if not r["above"])
    ma20_down = sum(1 for r in rows if r["ma20_chg"] < 0)

    mk = market_rows(market, date, params)
    if mk.get("n"):
        scope = "全市场" if mk["n"] >= 4000 else f"截断快照 {mk['n']} 只"
        L.append(f"- {scope}：上涨 {mk['up']} / 下跌 {mk['down']}｜"
                 f"成交额 {mk['amount_yi']:,.0f} 亿元｜"
                 f"涨停 {mk['zt']} / 跌停 {mk['dt']}（{mk['limit_src']}）")
    if mk.get("pool_n"):
        L.append(f"- 扫描池 5 日线宽度 {mk['pool_above5']:.1f}%（n={mk['pool_n']}，样本不是全市场）")

    state = f"｜当前状态「{res.state_cn}」仓位上限 {res.cap:.0%}" if res is not None else ""
    if below == len(rows):
        verdict = "全部在 MA20 下方，趋势端仍受压"
    elif below == 0:
        verdict = "全部在 MA20 上方，趋势端未破"
    else:
        verdict = f"{below}/{len(rows)} 个在 MA20 下方，强弱分化"
    if ma20_down:
        verdict += f"；{ma20_down}/{len(rows)} 个的 MA20 较 5 日前下移"
    L.append(f"- 结论：{verdict}{state}")
    L.append("")
    return L


# ---------------------------------------------------------------------------
# 二、板块资金扫描
# ---------------------------------------------------------------------------

def _ths_frame(kind: str = "概念") -> pd.DataFrame:
    """同花顺板块资金：即时 + 5 日，按板块名合并（概念/行业两套口径）。"""
    fn = ak.stock_fund_flow_concept if kind == "概念" else ak.stock_fund_flow_industry
    now = fn(symbol="即时")
    d5 = fn(symbol="5日排行")
    if now is None or now.empty or d5 is None or d5.empty:
        raise RuntimeError(f"同花顺{kind}资金流返回空")
    now = now.copy()
    d5 = d5.copy()
    now["行业"] = now["行业"].astype(str)
    d5["行业"] = d5["行业"].astype(str)
    m = now.merge(d5[["行业", "净额", "阶段涨跌幅"]], on="行业", how="inner",
                  suffixes=("", "_5d"))
    m["n"] = m["公司家数"].map(_num)
    m["f1"] = m["净额"].map(_to_yi)
    m["f5"] = m["净额_5d"].map(_to_yi)
    m["chg"] = m["行业-涨跌幅"].map(_pct)
    m["chg5"] = m["阶段涨跌幅"].map(_pct)
    m["norm5"] = m.apply(lambda r: r["f5"] / r["n"] if r["n"] > 0 else 0.0, axis=1)
    m["score"] = (0.5 * _rank(m["norm5"]) + 0.3 * _rank(m["f1"])
                  + 0.2 * _rank(m["chg"])).round(1)
    return m


def sector_tag(r) -> str:
    if r["f5"] > 0 and r["f1"] > 0 and r["chg"] > 0:
        return "早期修复"
    if r["f5"] > 0 > r["f1"]:
        return "资金分歧"
    if r["f5"] < 0 and r["f1"] < 0:
        return "退潮"
    if r["f5"] < 0 < r["f1"]:
        return "短线反抽"
    return "观察"


def _watch_rows(watch: list[str], concept: pd.DataFrame) -> list[tuple[str, object]]:
    """观察清单先按概念板块匹配，匹配不到再退同花顺行业（银行/白酒这类在行业表里）。"""
    rows: list[tuple[str, object]] = []
    missing: list[str] = []
    for w in watch:
        sub = concept[concept["行业"] == w]
        if len(sub):
            rows.append((w, sub.iloc[0]))
        else:
            missing.append(w)
    if missing:
        try:
            ind = _ths_frame("行业")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 同花顺行业资金流不可用：{e}")
            return rows
        for w in missing:
            sub = ind[ind["行业"] == w]
            if len(sub):
                rows.append((w, sub.iloc[0]))
    return rows


def sector_section(date: str, params) -> list[str]:
    try:
        m = _ths_frame("概念")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 板块资金扫描不可用：{e}")
        return ["**板块资金扫描**", "- 同花顺板块资金流不可用，本节跳过（不影响交易计划）。", ""]

    m["repair"] = (m["f5"] > 0) & (m["f1"] > 0) & (m["chg"] > 0)
    m["strict"] = m["repair"] & (m["chg"] >= 1.0) & (m["chg5"] >= 3.0)
    early = int(m["repair"].sum())
    strict = int(m["strict"].sum())
    top_n = int(params.rt("board", "top_sectors", default=6))
    top = m[m["repair"]].sort_values("score", ascending=False).head(top_n)

    L = ["**板块资金扫描**（同花顺概念，按 5 日资金/强度打分）"]
    L.append(f"- 样本 {len(m)} 个概念板块：符合早期修复条件 {early} 个，"
             f"符合严格趋势延续条件 {strict} 个")
    L.append("")
    if early == 0:
        strong = m.sort_values("f1", ascending=False).head(2)
        L.append("- 今日没有板块通过早期修复条件（5 日+今日资金双流入且当日上涨），"
                 "不硬凑排名；资金最强的两个板块："
                 + "、".join(f"{r['行业']} 今日 {r['f1']:+.2f}亿 / 5日 {r['f5']:+.2f}亿"
                             for _, r in strong.iterrows()))
    else:
        L.append("| 排名 | 板块 | 5日资金 | 今日资金 | 归一化资金 | 今日涨跌 | 5日涨跌 | 标签 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for i, (_, r) in enumerate(top.iterrows(), 1):
            L.append(f"| {i} | {r['行业']} | {_fmt_money_yi(r['f5'])} | "
                     f"{_fmt_money_yi(r['f1'])} | {r['norm5']:+.2f}亿/家 | "
                     f"{r['chg']:+.2f}% | {r['chg5']:+.2f}% | {sector_tag(r)} |")

    # 新旧力量对比：固定观察清单，按资金方向分组，便于一眼看出风格切换
    watch = params.rt("board", "watch_sectors", default=DEFAULT_WATCH) or []
    hit = _watch_rows([str(w) for w in watch], m)
    if hit:
        L.append("")
        L.append("- 新旧力量对比：" + "；".join(
            f"{w} 今日 {r['f1']:+.2f}亿 / 5日 {r['f5']:+.2f}亿（{r['chg']:+.2f}%）"
            for w, r in hit[:6]))
        inflow = [w for w, r in hit if r["f1"] > 0 and r["f5"] > 0]
        outflow = [w for w, r in hit if r["f1"] < 0 and r["f5"] < 0]
        if inflow or outflow:
            L.append(f"- 资金取向：{'、'.join(inflow) or '无'} 持续性流入；"
                     f"{'、'.join(outflow) or '无'} 持续性流出")
    L.append("- 口径：资金为板块主力净额（亿元）；归一化资金 = 5日净额 ÷ 板块公司家数；"
             "评分是横截面百分位加权，未经收益回测。")
    L.append("")
    return L


# ---------------------------------------------------------------------------
# 三、核心龙头筛选
# ---------------------------------------------------------------------------

def leader_frame(market: dm.MarketData, date: str) -> pd.DataFrame:
    """个股资金流（同花顺）× 扫描池：5 日资金/流通市值、5 日涨跌。"""
    now = ak.stock_fund_flow_individual(symbol="即时")
    d5 = ak.stock_fund_flow_individual(symbol="5日排行")
    if now is None or now.empty or d5 is None or d5.empty:
        raise RuntimeError("同花顺个股资金流返回空")
    now = now.copy()
    d5 = d5.copy()
    now["code"] = now["股票代码"].astype(str).str.zfill(6)
    d5["code"] = d5["股票代码"].astype(str).str.zfill(6)
    m = now.merge(d5[["code", "阶段涨跌幅", "资金流入净额"]], on="code", how="inner")
    m = m[m["code"].isin(market.bars.keys())].copy()
    if m.empty:
        raise RuntimeError("扫描池与个股资金流无交集")
    m["name"] = [market.meta.get(c, {}).get("name", "") for c in m["code"]]
    m["industry"] = [market.meta.get(c, {}).get("industry", "") or "未分类"
                     for c in m["code"]]
    m["mv"] = [float(market.meta.get(c, {}).get("float_mv", 0) or 0) / 1e8
               for c in m["code"]]
    m["f1"] = m["净额"].map(_to_yi)
    m["f5"] = m["资金流入净额"].map(_to_yi)
    m["chg5"] = m["阶段涨跌幅"].map(_pct)
    m["chg1"] = m["涨跌幅"].map(_pct)
    m["ratio"] = [r["f5"] / r["mv"] * 100 if r["mv"] > 0 else 0.0
                  for _, r in m.iterrows()]
    m["ext"] = [float(market.bars[c]["close"].iloc[-1])
                / float(market.bars[c]["close"].tail(20).mean()) - 1
                if len(market.bars[c]) >= 20 else 0.0 for c in m["code"]]
    m["score"] = (0.5 * _rank(m["ratio"]) + 0.3 * _rank(m["chg5"])
                  + 0.2 * _rank(m["f1"])).round(1)
    return m


def leader_section(market: dm.MarketData, date: str, params, plan: dict | None = None) -> list[str]:
    try:
        m = leader_frame(market, date)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 龙头筛选不可用：{e}")
        return ["**核心龙头筛选**", "- 个股资金流不可用，本节跳过（不影响交易计划）。", ""]

    top_n = int(params.rt("board", "top_stocks", default=9))
    pool = m[(m["f5"] > 0) & (m["mv"] > 0)].sort_values("score", ascending=False)
    if pool.empty:
        pool = m.sort_values("score", ascending=False)
    top = pool.head(top_n)
    planned = {c.get("code") for c in (plan or {}).get("candidates", [])} or set()

    L = ["**核心龙头筛选**（扫描池 ∩ 个股资金流）", ""]
    L.append("| 股票 | 行业 | 5日资金 | 今日资金 | 5日资金/流通市值 | 5日涨跌 | 距MA20 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for _, r in top.iterrows():
        mark = "★计划内" if r["code"] in planned else ""
        L.append(f"| {r['code']} {r['name']} | {r.get('industry') or '—'} | "
                 f"{_fmt_money_yi(r['f5'])} | {_fmt_money_yi(r['f1'])} | "
                 f"{r['ratio']:+.2f}% | {r['chg5']:+.2f}% | {r['ext']:+.2%} | {mark} |")
    L.append("- 说明：研究候选，不是成交信号；只有「明日计划」里的候选才按开盘区间成交。")
    L.append("")
    return L


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _is_today(date: str) -> bool:
    """板块/龙头/涨跌停池只有当日接口，历史回放时不能拿今天的数据充数。"""
    return dm.norm_date(date) == dt.date.today().strftime("%Y%m%d")


def enabled_for(market, params) -> bool:
    """看板是否启用：关闭开关或合成数据演示时都不生成（避免演示跑联网）。"""
    if not bool(params.rt("board", "enabled", default=True)):
        return False
    return market is not None and getattr(market, "source", "") != "synthetic"


def build(market: dm.MarketData, date: str, params, res=None,
          plan: dict | None = None) -> list[str]:
    """返回看板 markdown 行；关闭或合成数据时返回空。"""
    if not enabled_for(market, params):
        return []
    if not _is_today(date):
        # 板块资金/龙虎、涨跌停池只有“当日”接口，历史回放只出大盘环境表
        lines = list(index_section(market, date, params, res))
        lines.append("- 历史日期回放：板块资金、龙头筛选与涨跌停池只有当日数据，本节跳过。")
        lines.append("")
        return lines
    lines: list[str] = []
    for name, fn in (("大盘环境", lambda: index_section(market, date, params, res)),
                     ("板块资金", lambda: sector_section(date, params)),
                     ("龙头筛选", lambda: leader_section(market, date, params, plan))):
        try:
            lines.extend(fn())
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 看板「{name}」生成失败：{e}")
    return lines
