# -*- coding: utf-8 -*-
"""
数据层：akshare（东财）日频数据 + 本地缓存。

只用日线级接口，不需要分时：
  - stock_zt_pool_em            涨停股池（含连板数/封单额/首封时间/炸板次数/行业）
  - stock_zt_pool_zbgc_em       炸板股池
  - stock_zt_pool_dtgc_em       跌停股池
  - stock_zt_pool_previous_em   昨日涨停股今日表现（赚钱效应）
  - stock_market_activity_legu  市场活跃度（上涨/下跌/涨停/跌停家数）
  - stock_zh_a_hist             个股日线（不复权，用于实际成交价与涨停价判定）
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pickle
import time
import urllib.request
import warnings
from pathlib import Path

import pandas as pd

from config import CACHE_DIR

warnings.filterwarnings("ignore")

_ZCACHE = CACHE_DIR / "zt"
_DCACHE = CACHE_DIR / "daily"
_ZCACHE.mkdir(parents=True, exist_ok=True)
_DCACHE.mkdir(parents=True, exist_ok=True)

_CAL = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _retry(fn, *a, retries: int = 3, **kw):
    last = None
    for i in range(max(1, retries)):
        try:
            return fn(*a, **kw)
        except ValueError:
            raise                      # 参数性错误（如超出数据窗口）重试无意义
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def norm_date(d) -> str:
    """统一成 20261103 形式。"""
    if isinstance(d, (dt.date, dt.datetime)):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "").replace("/", "")[:8]


def dash(d: str) -> str:
    d = norm_date(d)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def trade_dates() -> list[str]:
    global _CAL
    if _CAL is None:
        import akshare as ak
        df = _retry(ak.tool_trade_date_hist_sina)
        _CAL = [norm_date(x) for x in df["trade_date"].tolist()]
    return _CAL


def latest_trade_date(ref: str | None = None) -> str:
    """不晚于 ref（默认今天）的最近交易日。"""
    ref = norm_date(ref or dt.date.today())
    ds = [d for d in trade_dates() if d <= ref]
    return ds[-1]


def prev_trade_date(d: str, n: int = 1) -> str:
    ds = [x for x in trade_dates() if x < norm_date(d)]
    return ds[-n] if len(ds) >= n else ds[0]


def next_trade_date(d: str, n: int = 1) -> str | None:
    ds = [x for x in trade_dates() if x > norm_date(d)]
    return ds[n - 1] if len(ds) >= n else None


def recent_trade_dates(d: str, n: int) -> list[str]:
    ds = [x for x in trade_dates() if x <= norm_date(d)]
    return ds[-n:]


# ---------------------------------------------------------------------------
# 涨停 / 炸板 / 跌停 池（按日缓存，永不失效）
# ---------------------------------------------------------------------------

def _pool_cache(kind: str, date: str) -> Path:
    return _ZCACHE / f"{kind}_{norm_date(date)}.json.gz"


def _load_pool(kind: str, date: str) -> pd.DataFrame | None:
    p = _pool_cache(kind, date)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return pd.DataFrame(json.load(f))
    except Exception:
        return None


def _save_pool(kind: str, date: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return                      # 不缓存空结果，避免把临时失败永久固化
    p = _pool_cache(kind, date)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(df.to_dict("records"), f, ensure_ascii=False, default=str)
    tmp.replace(p)


def _fetch_pool(kind: str, date: str, fn_name: str) -> pd.DataFrame:
    import akshare as ak
    cached = _load_pool(kind, date)
    if cached is not None:
        return cached
    try:
        df = _retry(getattr(ak, fn_name), date=norm_date(date), retries=2)
    except ValueError as e:
        # 超出东财数据窗口（池类接口仅保留约 30 个交易日），静默跳过
        return pd.DataFrame()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {fn_name}({date}) 失败: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        df = pd.DataFrame()
    _save_pool(kind, date, df)
    return df


def zt_pool(date: str) -> pd.DataFrame:
    """涨停股池。空表示当日无涨停或数据缺失。"""
    return _fetch_pool("zt", date, "stock_zt_pool_em")


def zb_pool(date: str) -> pd.DataFrame:
    return _fetch_pool("zb", date, "stock_zt_pool_zbgc_em")


def dt_pool(date: str) -> pd.DataFrame:
    return _fetch_pool("dt", date, "stock_zt_pool_dtgc_em")


def prev_zt_pool(date: str) -> pd.DataFrame:
    return _fetch_pool("prevzt", date, "stock_zt_pool_previous_em")


def market_activity(date: str) -> dict:
    """市场活跃度。接口只提供“当前”数据，历史日不可回补。"""
    p = _ZCACHE / f"act_{norm_date(date)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    import akshare as ak
    try:
        df = _retry(ak.stock_market_activity_legu)
        d = {str(r["item"]): float(r["value"]) for _, r in df.iterrows()
             if str(r["value"]).replace(".", "").replace("-", "").isdigit()}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] market_activity 失败: {e}")
        d = {}
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 个股日线
# ---------------------------------------------------------------------------

def _dpath(code: str) -> Path:
    return _DCACHE / f"{code}.pkl"


def daily(code: str, end: str, need: int = 300) -> pd.DataFrame:
    """
    不复权日线，列：date open close high low volume amount pct turnover
    带缓存：若缓存已覆盖 end 且长度足够，直接返回。
    """
    end = norm_date(end)
    p = _dpath(code)
    df = None
    if p.exists():
        try:
            with open(p, "rb") as f:
                df = pickle.load(f)
        except Exception:
            df = None
    if df is not None and len(df) >= need and str(df["date"].iloc[-1]) >= end:
        return df.tail(need).reset_index(drop=True)

    import akshare as ak
    start = (dt.date.today() - dt.timedelta(days=need * 2 + 90)).strftime("%Y%m%d")
    try:
        raw = _retry(ak.stock_zh_a_hist, symbol=code, period="daily",
                     start_date=start, end_date=end, adjust="")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] daily({code}) 失败: {e}")
        return df.tail(need).reset_index(drop=True) if df is not None else pd.DataFrame()

    if raw is None or raw.empty:
        return df.tail(need).reset_index(drop=True) if df is not None else pd.DataFrame()

    out = pd.DataFrame({
        "date": raw["日期"].astype(str).str.replace("-", "", regex=False),
        "open": pd.to_numeric(raw["开盘"], errors="coerce"),
        "close": pd.to_numeric(raw["收盘"], errors="coerce"),
        "high": pd.to_numeric(raw["最高"], errors="coerce"),
        "low": pd.to_numeric(raw["最低"], errors="coerce"),
        "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
        "amount": pd.to_numeric(raw["成交额"], errors="coerce"),
        "pct": pd.to_numeric(raw["涨跌幅"], errors="coerce"),
        "turnover": pd.to_numeric(raw["换手率"], errors="coerce"),
    }).dropna(subset=["close"]).reset_index(drop=True)

    try:
        with open(p, "wb") as f:
            pickle.dump(out, f)
    except Exception:
        pass
    return out.tail(need).reset_index(drop=True)


def bar_on(code: str, date: str, need: int = 300) -> dict | None:
    """取某只票某日的 K 线。"""
    df = daily(code, date, need)
    if df.empty:
        return None
    hit = df[df["date"] == norm_date(date)]
    if hit.empty:
        return None
    r = hit.iloc[-1]
    return {k: float(r[k]) for k in ("open", "close", "high", "low", "volume", "amount")} | {
        "pct": float(r["pct"]) if pd.notna(r["pct"]) else 0.0,
        "turnover": float(r["turnover"]) if pd.notna(r["turnover"]) else 0.0,
        "prev_close": float(df[df["date"] < norm_date(date)]["close"].iloc[-1])
        if (df["date"] < norm_date(date)).any() else float(r["close"]),
        "ma5": float(df[df["date"] <= norm_date(date)]["close"].tail(5).mean()),
        "vol_ma5": float(df[df["date"] <= norm_date(date)]["volume"].tail(6).head(5).mean()),
        "high250": float(df[df["date"] <= norm_date(date)]["high"].tail(250).max()),
    }


# ---------------------------------------------------------------------------
# 实时行情（盘中取今开，腾讯批量）
# ---------------------------------------------------------------------------

def qt_code(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith("6"):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    return "bj" + c


def realtime_open(codes: list[str]) -> dict[str, float]:
    """批量取今开。返回 {6位代码: 今开}。一次请求可带 40 只。"""
    out: dict[str, float] = {}
    for i in range(0, len(codes), 40):
        part = codes[i:i + 40]
        url = "https://qt.gtimg.cn/q=" + ",".join(qt_code(c) for c in part)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="ignore")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 实时行情失败: {e}")
            continue
        for line in raw.strip().split("\n"):
            if '="' not in line:
                continue
            body = line.split('="', 1)[1].rstrip('";')
            f = body.split("~")
            if len(f) < 6:
                continue
            try:
                code6 = str(f[2]).zfill(6)
                op = float(f[5])
                if op > 0:
                    out[code6] = op
            except Exception:
                continue
    return out


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
    return round(prev_close * (1 + ratio) + 1e-8, 2)


def is_bse(code: str) -> bool:
    c = str(code).zfill(6)
    return c.startswith("8") or c.startswith("4") or c.startswith("920")


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series([default] * len(df), index=df.index, dtype=float)


def _str(df: pd.DataFrame, col: str, default: str = "") -> pd.Series:
    if col in df.columns:
        return df[col].astype(str)
    return pd.Series([default] * len(df), index=df.index, dtype=object)


def normalize_pool(df: pd.DataFrame) -> pd.DataFrame:
    """统一涨停/炸板/跌停池的列名与类型。缺失字段补默认值（不同池字段不同）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=[
            "code", "name", "pct", "price", "amount", "float_mv",
            "turnover", "seal_amt", "first_seal", "last_seal",
            "break_times", "boards", "stat", "industry"])
    d = pd.DataFrame(index=df.index)
    d["code"] = _str(df, "代码").str.zfill(6)
    d["name"] = _str(df, "名称")
    d["pct"] = _num(df, "涨跌幅")
    d["price"] = _num(df, "最新价")
    d["amount"] = _num(df, "成交额")
    d["float_mv"] = _num(df, "流通市值")
    d["turnover"] = _num(df, "换手率")
    d["seal_amt"] = _num(df, "封板资金")
    d["first_seal"] = _str(df, "首次封板时间").str.zfill(6)
    d["last_seal"] = _str(df, "最后封板时间").str.zfill(6)
    d["break_times"] = _num(df, "炸板次数").astype(int)
    d["boards"] = _num(df, "连板数").astype(int)
    d["stat"] = _str(df, "涨停统计")
    d["industry"] = _str(df, "所属行业")
    return d.reset_index(drop=True)


def seal_minutes(hhmmss: str) -> int:
    """093000 -> 从 9:30 起算的分钟数。"""
    s = str(hhmmss).zfill(6)
    h, m = int(s[:2]), int(s[2:4])
    return max(0, (h - 9) * 60 + m - 30)
