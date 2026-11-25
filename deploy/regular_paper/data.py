# -*- coding: utf-8 -*-
"""
数据层：akshare（东财/新浪）日频数据 + 本地缓存。

常规版只需要三类数据：
  1. 基准指数日线（默认沪深 300）——择时
  2. 全市场快照（成交额/流通市值/涨跌幅）——构建可扫描股票池
  3. 个股日线（不复权）——横盘、坑、量价、均线

执行假设是“T 日收盘出计划，T+1 开盘成交”，所以不需要分时数据。
首次运行会拉取 max_scan 只票的一年日线，约 2-5 分钟；之后走缓存。
"""

from __future__ import annotations

import datetime as dt
import json
import pickle
import re
import socket
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import indicators as ind
from config import CACHE_DIR

warnings.filterwarnings("ignore")

# akshare 的部分接口不传 timeout；这里兜底，避免单个票的网络读挂死整轮收盘。
socket.setdefaulttimeout(20)

DAILY_DIR = CACHE_DIR / "daily"
INDEX_DIR = CACHE_DIR / "index"
SPOT_DIR = CACHE_DIR / "spot"
for _d in (DAILY_DIR, INDEX_DIR, SPOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_CAL: list[str] | None = None


class DataError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------

def norm_date(d) -> str:
    if isinstance(d, (dt.date, dt.datetime)):
        return d.strftime("%Y%m%d")
    return str(d).replace("-", "").replace("/", "")[:8]


def dash(d: str) -> str:
    d = norm_date(d)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _retry(fn, *a, retries: int = 3, **kw):
    last = None
    for i in range(max(1, retries)):
        try:
            return fn(*a, **kw)
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.2 * (i + 1))
    raise last


def trade_dates() -> list[str]:
    global _CAL
    if _CAL is None:
        import akshare as ak
        try:
            df = _retry(ak.tool_trade_date_hist_sina)
            _CAL = [norm_date(x) for x in df["trade_date"].tolist()]
        except Exception as e:  # noqa: BLE001
            raise DataError(f"无法获取交易日历：{e}") from e
    return _CAL


def latest_trade_date(ref: str | None = None) -> str:
    ref = norm_date(ref or dt.date.today())
    ds = [d for d in trade_dates() if d <= ref]
    if not ds:
        raise DataError(f"{ref} 之前没有交易日")
    return ds[-1]


def prev_trade_date(d: str, n: int = 1) -> str | None:
    ds = [x for x in trade_dates() if x < norm_date(d)]
    return ds[-n] if len(ds) >= n else None


def next_trade_date(d: str, n: int = 1) -> str | None:
    ds = [x for x in trade_dates() if x > norm_date(d)]
    return ds[n - 1] if len(ds) >= n else None


def recent_trade_dates(d: str, n: int) -> list[str]:
    ds = [x for x in trade_dates() if x <= norm_date(d)]
    return ds[-n:]


# ---------------------------------------------------------------------------
# 市场快照
# ---------------------------------------------------------------------------

def _spot_file() -> Path:
    return SPOT_DIR / "spot.json"


def _strip_exchange(code: str) -> str:
    c = str(code).strip().lower()
    for pre in ("sh", "sz", "bj"):
        if c.startswith(pre):
            c = c[len(pre):]
            break
    return c.zfill(6)


def _old_float_mv() -> dict[str, float]:
    """东财快照失败时，用旧缓存补流通市值，避免把全市场都按 0 市值处理。"""
    p = _spot_file()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(r.get("code", "")): float(r.get("float_mv", 0) or 0)
                for r in raw.get("rows", [])}
    except Exception:
        return {}


def _sina_spot_rows() -> list[dict]:
    import akshare as ak
    df = _retry(ak.stock_zh_a_spot, retries=2)
    old_mv = _old_float_mv()
    rows: list[dict] = []
    for _, r in df.iterrows():
        code = _strip_exchange(r.get("代码", ""))
        if not code or code == "000000":
            continue
        rows.append({
            "code": code,
            "name": str(r.get("名称", "")),
            "price": _f(r.get("最新价")),
            "pct": _f(r.get("涨跌幅")),
            "open": _f(r.get("今开")),
            "high": _f(r.get("最高")),
            "low": _f(r.get("最低")),
            # 新浪成交量是股，东财缓存是手；统一成手
            "volume": _f(r.get("成交量")) / 100.0,
            "amount": _f(r.get("成交额")),
            "turnover": 0.0,
            "float_mv": old_mv.get(code, 0.0),
            "total_mv": 0.0,
            "industry": "",
            "snap_date": dt.date.today().strftime("%Y%m%d"),
        })
    return rows


def _cached_rows() -> list[dict]:
    p = _spot_file()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("rows", [])
    except Exception:
        return []


def _bulk_quotes(codes: list[str], batch: int = 60) -> list[dict]:
    """新浪批量实时行情，避免全市场 70 页分页。"""
    old_rows = {str(r.get("code", "")): r for r in _cached_rows()}
    out: list[dict] = []
    for i in range(0, len(codes), batch):
        chunk = [c for c in codes[i:i + batch] if c]
        if not chunk:
            continue
        syms = ",".join(_sina_symbol(c) for c in chunk)
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=" + syms,
            headers={"Referer": "https://finance.sina.com.cn",
                     "User-Agent": "Mozilla/5.0"},
        )
        try:
            text = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 新浪批量行情失败: {e}")
            continue
        for line in text.splitlines():
            if '="' not in line:
                continue
            key, payload = line.split('="', 1)
            payload = payload.rsplit('"', 1)[0]
            f = payload.split(",")
            if len(f) < 32:
                continue
            sym = key.split("_")[-1]
            code = _strip_exchange(sym)
            try:
                price, prev, op, high, low = (float(f[3]), float(f[2]), float(f[1]),
                                              float(f[4]), float(f[5]))
                vol = float(f[8]) / 100.0       # 股 -> 手
                amt = float(f[9])
            except (ValueError, IndexError):
                continue
            if price <= 0 or prev <= 0:
                continue
            old = old_rows.get(code, {})
            out.append({
                "code": code,
                "name": f[0],
                "price": price,
                "pct": (price / prev - 1) * 100,
                "open": op, "high": high, "low": low,
                "volume": vol, "amount": amt,
                "turnover": 0.0,
                "float_mv": float(old.get("float_mv", 0) or 0),
                "total_mv": 0.0,
                "industry": old.get("industry", "") or "",
                "snap_date": norm_date(f[30]) if len(f) > 30 else dt.date.today().strftime("%Y%m%d"),
            })
    return out


def spot_snapshot(cache_minutes: int = 60) -> list[dict]:
    """
    全市场快照。优先东财；东财被限流/断连时退回新浪全市场快照。
    返回 list[dict]，字段已归一化。
    """
    p = _spot_file()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            age = time.time() - float(raw.get("ts", 0))
            today = dt.date.today().strftime("%Y%m%d")
            if (age <= cache_minutes * 60 and raw.get("rows")
                    and raw.get("date") == today):
                return raw["rows"]
        except Exception:
            pass

    import akshare as ak
    rows: list[dict] = []
    try:
        df = _retry(ak.stock_zh_a_spot_em, retries=2)
        for _, r in df.iterrows():
            code = _strip_exchange(r.get("代码", ""))
            if not code or code == "000000":
                continue
            rows.append({
                "code": code,
                "name": str(r.get("名称", "")),
                "price": _f(r.get("最新价")),
                "pct": _f(r.get("涨跌幅")),
                "open": _f(r.get("今开")),
                "high": _f(r.get("最高")),
                "low": _f(r.get("最低")),
                "volume": _f(r.get("成交量")),
                "amount": _f(r.get("成交额")),
                "turnover": _f(r.get("换手率")),
                "float_mv": _f(r.get("流通市值")),
                "total_mv": _f(r.get("总市值")),
                "industry": str(r.get("所属行业", "") or ""),
                "snap_date": dt.date.today().strftime("%Y%m%d"),
            })
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 东财快照失败（{e}），改用新浪批量行情（旧池前 800）")
        old = _cached_rows()
        if old:
            codes = [r["code"] for r in sorted(
                old, key=lambda x: -float(x.get("amount", 0) or 0))[:800]]
            rows = _bulk_quotes(codes)
            if rows:
                print(f"[info] 新浪批量行情重建快照 {len(rows)} 只")
                p.write_text(json.dumps(
                    {"ts": time.time(), "date": dt.date.today().strftime("%Y%m%d"),
                     "rows": rows}, ensure_ascii=False), encoding="utf-8")
                return rows
        # 最后才退回新浪全市场分页（慢，容易卡住）
        try:
            rows = _sina_spot_rows()
        except Exception as e2:  # noqa: BLE001
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))["rows"]
                except Exception:
                    pass
            raise DataError(f"东财/新浪快照均失败：{e2}") from e2

    if not rows:
        raise DataError("全市场快照为空")
    p.write_text(json.dumps({"ts": time.time(), "date": dt.date.today().strftime("%Y%m%d"),
                             "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows

def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 个股日线
# ---------------------------------------------------------------------------

def _daily_file(code: str) -> Path:
    return DAILY_DIR / f"{code}.pkl"


def _normalize_hist(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": raw["日期"].astype(str).str[:10].str.replace("-", "", regex=False),
        "open": pd.to_numeric(raw["开盘"], errors="coerce"),
        "close": pd.to_numeric(raw["收盘"], errors="coerce"),
        "high": pd.to_numeric(raw["最高"], errors="coerce"),
        "low": pd.to_numeric(raw["最低"], errors="coerce"),
        "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
        "amount": pd.to_numeric(raw["成交额"], errors="coerce"),
        "pct": pd.to_numeric(raw["涨跌幅"], errors="coerce"),
        "turnover": pd.to_numeric(raw["换手率"], errors="coerce"),
    })
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    out["pct"] = out["pct"].fillna(0.0)
    out["turnover"] = out["turnover"].fillna(0.0)
    out["amount"] = out["amount"].fillna(0.0)
    return out


def _sina_symbol(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith("6"):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    if c.startswith(("8", "4", "920")):
        return f"bj{c}"
    return f"sh{c}"


def _fetch_sina_daily(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    raw = _retry(ak.stock_zh_a_daily, symbol=_sina_symbol(code),
                 start_date=start, end_date=end, adjust="", retries=2)
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": raw["date"].astype(str).str[:10].str.replace("-", "", regex=False),
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        # 新浪 volume 是股，东财缓存是手；统一除以 100
        "volume": pd.to_numeric(raw["volume"], errors="coerce") / 100.0,
        "amount": pd.to_numeric(raw.get("amount", 0), errors="coerce").fillna(0.0),
    })
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    out["pct"] = out["close"].pct_change().fillna(0.0) * 100
    if "turnover" in raw.columns:
        t = pd.to_numeric(raw["turnover"], errors="coerce").fillna(0.0)
        # 新浪 turnover 是小数（0.0018），东财是百分数（0.18）
        out["turnover"] = t * 100 if t.max() <= 1.0 else t
    else:
        out["turnover"] = 0.0
    return out


def daily(code: str, end: str, need: int = 180) -> pd.DataFrame:
    """
    不复权日线，返回截至 end 的最近 need 根。
    缓存策略：缓存覆盖 end 直接用；否则增量拉取并合并。
    """
    end = norm_date(end)
    p = _daily_file(code)
    cached: pd.DataFrame | None = None
    if p.exists():
        try:
            with open(p, "rb") as f:
                cached = pickle.load(f)
            cached = cached[cached["date"] <= end].reset_index(drop=True)
        except Exception:
            cached = None

    if cached is not None and len(cached) >= need and str(cached["date"].iloc[-1]) >= end:
        return cached.tail(need).reset_index(drop=True)

    import akshare as ak
    global_start = (dt.date.today() - dt.timedelta(days=need * 2 + 120)).strftime("%Y%m%d")
    if cached is not None and len(cached) >= need:
        start = max(global_start, str(cached["date"].iloc[-1]))
    else:
        start = global_start

    try:
        raw = _retry(ak.stock_zh_a_hist, symbol=code, period="daily",
                     start_date=start, end_date=end, adjust="")
        fresh = _normalize_hist(raw)
    except Exception as e:  # noqa: BLE001
        try:
            fresh = _fetch_sina_daily(code, start, end)
        except Exception as e2:  # noqa: BLE001
            print(f"[warn] daily({code}) 东财/新浪均失败: {e2}")
            return cached.tail(need).reset_index(drop=True) if cached is not None else pd.DataFrame()

    if fresh.empty:
        try:
            fresh = _fetch_sina_daily(code, start, end)
        except Exception:
            pass
        if fresh.empty:
            return cached.tail(need).reset_index(drop=True) if cached is not None else fresh

    if cached is not None and len(cached):
        merged = pd.concat([cached, fresh], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    else:
        merged = fresh.sort_values("date")
    merged = merged.reset_index(drop=True)

    try:
        with open(p, "wb") as f:
            pickle.dump(merged, f)
    except Exception:
        pass
    merged = merged[merged["date"] <= end].tail(need).reset_index(drop=True)
    return merged


# ---------------------------------------------------------------------------
# 指数
# ---------------------------------------------------------------------------

def _index_file(symbol: str) -> Path:
    return INDEX_DIR / f"{symbol}.pkl"


# 指数行情：东财/新浪/腾讯的日线接口在 16:20 前后经常断连或只给到昨天，
# 所以除了多源降级，还在收盘后用实时行情补出当日 K 线（见 _fill_index_today）。
# 口径约定：volume 单位手；amount 列沿用缓存口径（成交量手数），regime 只比比值。
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_INDEX_SH_PREFIX = ("000", "880", "999")


def _index_code(symbol: str) -> str:
    s = str(symbol).strip().lower()
    for pre in ("sh", "sz", "bj"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    return s.zfill(6)


def _index_prefix(symbol: str) -> str:
    return "sh" if _index_code(symbol).startswith(_INDEX_SH_PREFIX) else "sz"


def _index_secid(symbol: str) -> str:
    """东财 secid：1=沪市，0=深市。"""
    return f"{'1' if _index_prefix(symbol) == 'sh' else '0'}.{_index_code(symbol)}"


def _http_text(url: str, referer: str, timeout: float = 10.0) -> str:
    """轻量 HTTP 文本读取。行情接口不带 Referer 会被拒（新浪直接返回空串）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": referer,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "ignore")


def _index_closed(clock: str | None) -> bool:
    """行情时间是否已收盘（>=15:00）。盘中拿到的是现价，不能当收盘价存。"""
    m = re.match(r"^(\d{1,2}):(\d{2})", str(clock or ""))
    if not m:
        return False
    return int(m.group(1)) * 60 + int(m.group(2)) >= 15 * 60


def _sina_quote_volume(volume: float, amount: float) -> float:
    """
    新浪指数的成交量单位不统一：沪市指数报“手”，深市指数报“股”。
    每手 100 股，所以「成交额/成交量 > 100 元」就是手，否则按股折成手。
    """
    if volume <= 0:
        return 0.0
    if amount > 0 and amount / volume < 100:
        return volume / 100.0
    return volume


def _sina_index_quote(symbol: str) -> dict | None:
    sym = f"{_index_prefix(symbol)}{_index_code(symbol)}"
    text = _http_text(f"https://hq.sinajs.cn/list={sym}", "https://finance.sina.com.cn")
    for line in text.splitlines():
        if '="' not in line:
            continue
        f = line.split('="', 1)[1].rsplit('"', 1)[0].split(",")
        if len(f) < 32:
            continue
        try:
            op, prev, close, high, low = (float(f[1]), float(f[2]), float(f[3]),
                                          float(f[4]), float(f[5]))
            volume = _sina_quote_volume(float(f[8]), float(f[9]))
        except (TypeError, ValueError):
            continue
        return {"date": norm_date(f[30]), "clock": f[31], "open": op, "prev_close": prev,
                "close": close, "high": high, "low": low, "volume": volume,
                "source": "sina"}


def _em_index_quote(symbol: str) -> dict | None:
    url = ("https://push2.eastmoney.com/api/qt/stock/get?"
           f"secid={_index_secid(symbol)}&ut={_EM_UT}"
           "&fields=f43,f44,f45,f46,f47,f48,f59,f60,f86")
    data = json.loads(_http_text(url, "https://quote.eastmoney.com/"))
    q = data.get("data") or {}
    if not q.get("f43"):
        return None
    scale = float(10 ** int(q.get("f59") or 2))

    def px(key: str) -> float:
        v = q.get(key)
        return float(v) / scale if isinstance(v, (int, float)) else 0.0

    # f86 是行情时间戳，固定按北京时间换算，避免服务器时区影响“今天”是哪天。
    when = (dt.datetime.fromtimestamp(int(q.get("f86") or 0), dt.timezone.utc)
            + dt.timedelta(hours=8))
    return {"date": when.strftime("%Y%m%d"), "clock": when.strftime("%H:%M"),
            "open": px("f46"), "prev_close": px("f60"), "close": px("f43"),
            "high": px("f44"), "low": px("f45"),
            "volume": float(q.get("f47") or 0), "source": "eastmoney"}


def _tx_index_quote(symbol: str) -> dict | None:
    sym = f"{_index_prefix(symbol)}{_index_code(symbol)}"
    text = _http_text(f"https://qt.gtimg.cn/q={sym}", "https://finance.qq.com")
    for line in text.splitlines():
        if '="' not in line:
            continue
        f = line.split('="', 1)[1].rsplit('"', 1)[0].split("~")
        if len(f) < 37:
            continue
        try:
            close, prev, op = float(f[3]), float(f[4]), float(f[5])
            volume = float(f[6])                    # 手
            high, low = float(f[33]), float(f[34])
        except (TypeError, ValueError):
            continue
        stamp = str(f[30])
        return {"date": norm_date(stamp), "clock": f"{stamp[8:10]}:{stamp[10:12]}",
                "open": op, "prev_close": prev, "close": close, "high": high,
                "low": low, "volume": volume, "source": "tencent"}


# 腾讯行情成交量固定是“手”，口径最干净，放第一个；新浪次之（单位要按成交额反推）；
# 东财最容易断连，放最后。
_INDEX_QUOTE_SOURCES = (_tx_index_quote, _sina_index_quote, _em_index_quote)


def _index_quote_bar(symbol: str, date: str) -> dict | None:
    """收盘后用实时行情拼出 date 当天的指数 K 线；日期对不上或数据不合法就返回 None。"""
    date = norm_date(date)
    for fn in _INDEX_QUOTE_SOURCES:
        name = fn.__name__.strip("_").replace("_index_quote", "")
        try:
            q = fn(symbol)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 指数实时行情（{name}）失败：{e}")
            continue
        if not q:
            continue
        if norm_date(q.get("date") or "") != date:
            print(f"[warn] 指数实时行情（{name}）是 {q.get('date')}，不是 {dash(date)}")
            continue
        if not _index_closed(q.get("clock")):
            print(f"[warn] 指数实时行情（{name}）还没收盘（{q.get('clock')}），不用现价冒充收盘")
            continue
        try:
            o, h, l, c = (float(q["open"]), float(q["high"]),
                          float(q["low"]), float(q["close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c):
            print(f"[warn] 指数实时行情（{name}）价格异常：开{o} 高{h} 低{l} 收{c}")
            continue
        prev = float(q.get("prev_close") or 0)
        vol = max(0.0, float(q.get("volume") or 0))
        return {
            "date": date, "open": o, "high": h, "low": l, "close": c,
            "volume": vol,
            # 与指数缓存同口径：amount 列存成交量(手)，不是成交额(元)。
            "amount": vol,
            "pct": (c / prev - 1) * 100 if prev > 0 else 0.0,
            "turnover": 0.0,
            "source": q.get("source", name),
        }
    return None


def _sina_index_klines(symbol: str, need: int) -> pd.DataFrame:
    """新浪历史 K 线 JSON：一次请求、不需要 JS 解密；不带成交额，amount 用成交量占位。"""
    sym = f"{_index_prefix(symbol)}{_index_code(symbol)}"
    n = max(120, min(int(need) + 40, 1023))
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    rows = json.loads(_http_text(url, "https://finance.sina.com.cn"))
    if not rows:
        return pd.DataFrame()
    vol = pd.to_numeric([r.get("volume") for r in rows], errors="coerce") / 100.0
    out = pd.DataFrame({
        "date": [norm_date(r.get("day", "")) for r in rows],
        "open": pd.to_numeric([r.get("open") for r in rows], errors="coerce"),
        "close": pd.to_numeric([r.get("close") for r in rows], errors="coerce"),
        "high": pd.to_numeric([r.get("high") for r in rows], errors="coerce"),
        "low": pd.to_numeric([r.get("low") for r in rows], errors="coerce"),
        "volume": vol, "amount": vol,
    })
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    out["pct"] = out["close"].pct_change().fillna(0.0) * 100
    out["turnover"] = 0.0
    return out


def _index_hist(symbol: str, end: str, need: int) -> pd.DataFrame:
    """指数日线多源抓取：新浪 K 线 → 东财日线 → 东财 K 线 → 腾讯，取第一个响应的源。"""
    import akshare as ak
    start = (dt.date.today() - dt.timedelta(days=need * 2 + 120)).strftime("%Y%m%d")
    code = _index_code(symbol)
    sym = f"{_index_prefix(symbol)}{code}"

    def em_hist() -> pd.DataFrame:
        raw = _retry(ak.index_zh_a_hist, symbol=code, period="daily",
                     start_date=start, end_date=end)
        return _normalize_hist(raw)

    def em_kline() -> pd.DataFrame:
        raw = _retry(ak.stock_zh_index_daily_em, symbol=sym)
        raw = raw.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                  "high": "最高", "low": "最低",
                                  "volume": "成交量", "amount": "成交额"})
        if "涨跌幅" not in raw.columns:
            raw["涨跌幅"] = raw["收盘"].pct_change() * 100
        if "换手率" not in raw.columns:
            raw["换手率"] = 0.0
        if "成交额" not in raw.columns:
            raw["成交额"] = raw["成交量"] * raw["收盘"]
        return _normalize_hist(raw)

    def sina_kline() -> pd.DataFrame:
        return _sina_index_klines(symbol, need)

    def tx_hist() -> pd.DataFrame:
        raw = _retry(ak.stock_zh_index_daily_tx, symbol=sym)
        raw = raw.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                  "high": "最高", "low": "最低", "amount": "成交额"})
        raw["成交量"] = 0.0
        raw["涨跌幅"] = raw["收盘"].pct_change() * 100
        raw["换手率"] = 0.0
        return _normalize_hist(raw)

    # 新浪 K 线放第一个：单请求、无 JS 解密，16:20 收盘后基本已含当日；
    # 东财两个接口在 VPS 上经常不是“拒连”而是“挂住”，排在前面会白等 1-3 分钟。
    for name, fn in (("新浪K线", sina_kline), ("东财日线", em_hist),
                     ("东财K线", em_kline), ("腾讯日线", tx_hist)):
        try:
            df = fn()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 指数 {symbol} 日线源「{name}」失败：{e}")
            continue
        if df is None or df.empty:
            print(f"[warn] 指数 {symbol} 日线源「{name}」返回空")
            continue
        print(f"[info] 指数 {symbol} 日线源「{name}」返回 {len(df)} 根，"
              f"最后 {df['date'].iloc[-1]}")
        return df
    return pd.DataFrame()


def _fill_index_today(df: pd.DataFrame, symbol: str, date: str) -> pd.DataFrame:
    """
    日线源还没给到 date 的 K 线时，用收盘后的实时行情补一根当日 K 线。

    只允许补“上一行就是上一交易日”的缺口，避免把多日缺口填成假 K 线；
    补出来的行与缓存同口径，并随缓存一起落盘，后续复盘 / 重跑不用再拉。
    """
    date = norm_date(date)
    if df is None or df.empty or str(df["date"].iloc[-1]) >= date:
        return df
    last = str(df["date"].iloc[-1])
    try:
        prev = prev_trade_date(date)
    except Exception:  # noqa: BLE001
        prev = None
    if not prev or last != prev:
        print(f"[warn] 指数日线停在 {dash(last)}，与上一交易日 {prev or '未知'} 不连续，"
              f"不补实时 K 线")
        return df
    bar = _index_quote_bar(symbol, date)
    if bar is None:
        print(f"[warn] 指数 {symbol} 实时行情也没拿到 {dash(date)}，当日收盘价缺失")
        return df
    print(f"[info] 指数日线停在 {dash(last)}，用 {bar['source']} 实时行情补 {dash(date)}："
          f"收 {bar['close']:.2f}（{bar['pct']:+.2f}%）")
    cols = ("date", "open", "high", "low", "close",
            "volume", "amount", "pct", "turnover")
    return pd.concat([df, pd.DataFrame([{k: bar[k] for k in cols}])],
                     ignore_index=True)


def index_daily(symbol: str, end: str, need: int = 260,
                allow_realtime: bool = True) -> pd.DataFrame:
    end = norm_date(end)
    p = _index_file(symbol)
    cached: pd.DataFrame | None = None
    if p.exists():
        try:
            with open(p, "rb") as f:
                cached = pickle.load(f)
            cached = cached[cached["date"] <= end].reset_index(drop=True)
        except Exception:
            cached = None

    if cached is not None and len(cached) >= need and str(cached["date"].iloc[-1]) >= end:
        return cached.tail(need).reset_index(drop=True)

    fresh = _index_hist(symbol, end, need)
    if fresh.empty:
        if cached is None or not len(cached):
            raise DataError(f"无法获取指数 {symbol} 日线（东财/新浪/腾讯均失败）")
        print(f"[warn] 指数 {symbol} 日线接口全部失败，先用缓存"
              f"（最后一天 {cached['date'].iloc[-1]}）")
        merged = cached
    elif cached is not None and len(cached):
        merged = pd.concat([cached, fresh], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        merged = merged.reset_index(drop=True)
    else:
        merged = fresh.sort_values("date").reset_index(drop=True)

    # 收盘后日线接口常常还停在昨天：用实时行情补一根当日 K 线，别让整轮收盘白跑。
    if allow_realtime and str(merged["date"].iloc[-1]) < end:
        merged = _fill_index_today(merged, symbol, end)

    try:
        with open(p, "wb") as f:
            pickle.dump(merged, f)
    except Exception:
        pass
    return merged[merged["date"] <= end].tail(need).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 市场数据包
# ---------------------------------------------------------------------------

@dataclass
class MarketData:
    date: str
    benchmark: str
    index: pd.DataFrame
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    meta: dict[str, dict] = field(default_factory=dict)
    source: str = "akshare"

    def bar(self, code: str) -> pd.Series | None:
        df = self.bars.get(code)
        if df is None or df.empty:
            return None
        if str(df["date"].iloc[-1]) != self.date:
            return None
        return df.iloc[-1]

    def prev_close(self, code: str) -> float:
        df = self.bars.get(code)
        if df is None or len(df) < 2:
            return 0.0
        return float(df["close"].iloc[-2])

    def breadth(self) -> dict:
        """用已扫描的股票池近似全市场宽度。"""
        n = above20 = up = 0
        pcts: list[float] = []
        for code, df in self.bars.items():
            if df is None or len(df) < 21:
                continue
            if str(df["date"].iloc[-1]) != self.date:
                continue
            close = float(df["close"].iloc[-1])
            ma20 = float(df["close"].tail(20).mean())
            n += 1
            above20 += 1 if close > ma20 else 0
            pct = float(df["pct"].iloc[-1])
            up += 1 if pct > 0 else 0
            pcts.append(pct)
        if n == 0:
            return {"n": 0, "above_ma20": 0.5, "up_ratio": 0.5, "median_pct": 0.0}
        pcts.sort()
        med = pcts[len(pcts) // 2]
        return {
            "n": n,
            "above_ma20": above20 / n,
            "up_ratio": up / n,
            "median_pct": med,
        }

    def codes(self) -> list[str]:
        return list(self.bars.keys())


def daily_cached(code: str, end: str, need: int = 180) -> pd.DataFrame:
    """只读缓存，不联网；给“当日快照可补齐”的场景提速。"""
    end = norm_date(end)
    p = _daily_file(code)
    if not p.exists():
        return pd.DataFrame()
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
        df = df[df["date"] <= end].tail(need).reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def load_market(date: str | None, params) -> MarketData:
    """拉取某交易日的指数 + 股票池 + 个股日线。"""
    date = latest_trade_date(date)
    md = int(params.get("universe.max_scan"))
    need = int(params.get("universe.min_history_days")) + 60
    hist_days = max(need, int(params.rt("data", "history_days", default=180)))
    index_need = max(260, int(params.rt("data", "index_history_days", default=260)))
    cache_min = int(params.rt("data", "spot_cache_minutes", default=60))
    workers = max(1, int(params.rt("data", "fetch_workers", default=4)))

    print(f"[info] 拉取指数 {params.get('timing.index_symbol')} @ {dash(date)}")
    idx = index_daily(str(params.get("timing.index_symbol")), date, index_need,
                      allow_realtime=bool(params.rt("data", "index_realtime_fill",
                                                    default=True)))
    if idx.empty or str(idx["date"].iloc[-1]) != date:
        got = idx["date"].iloc[-1] if len(idx) else "无"
        raise DataError(f"指数日线未更新到 {date}（最后一天 {got}）；"
                        f"东财/新浪/腾讯日线与实时行情均未拿到当日 K 线")

    print("[info] 拉取全市场快照…")
    rows = spot_snapshot(cache_min)

    cand = []
    for r in rows:
        code, name = r["code"], r["name"]
        if not code or not name:
            continue
        if params.get("universe.exclude_st") and "ST" in name.upper():
            continue
        if "退" in name:
            continue
        if params.get("universe.exclude_bse") and ind.is_bse(code):
            continue
        if r["price"] < float(params.get("universe.min_price")):
            continue
        if float(r.get("float_mv", 0) or 0) > 0 and \
                float(r["float_mv"]) < float(params.get("universe.min_float_mv")):
            continue
        if r["amount"] <= 0:
            continue
        cand.append(r)

    cand.sort(key=lambda x: -x["amount"])
    cand = cand[:md]
    print(f"[info] 股票池 {len(cand)}/{len(rows)}（按成交额取前 {md}）")

    bars: dict[str, pd.DataFrame] = {}
    meta: dict[str, dict] = {}
    today_ready = any(r.get("snap_date") == date for r in rows)
    min_hist = int(params.get("universe.min_history_days"))
    try:
        prev_day = prev_trade_date(date)
    except Exception:
        prev_day = None

    def fetch(r):
        code = r["code"]
        if today_ready:
            df = daily_cached(code, date, hist_days)
            # 只有缓存至少覆盖到“上一交易日”才走只读快路径。
            # 否则（数据源失败/多日未跑）必须补齐历史，避免“11-11 + 今天快照”
            # 这种带缺口的 K 线把 MA5/5 日涨幅/量比全部算错。
            if (len(df) >= min_hist and prev_day
                    and str(df["date"].iloc[-1]) >= prev_day):
                return code, df
        return code, daily(code, date, hist_days)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, r) for r in cand]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                code, df = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 个股拉取失败: {e}")
                continue
            if df is None or df.empty:
                continue
            if str(df["date"].iloc[-1]) != date:
                # 停牌/未更新，保留但不进入当日选股
                pass
            bars[code] = df
            r = next((x for x in cand if x["code"] == code), {})
            meta[code] = {
                "name": r.get("name", ""),
                "industry": r.get("industry") or "未分类",
                "float_mv": r.get("float_mv", 0.0),
                "amount": r.get("amount", 0.0),
                "turnover": r.get("turnover", 0.0),
            }
            if i % 50 == 0:
                print(f"[info] 日线 {i}/{len(cand)}")

    if not bars:
        raise DataError("股票池日线全部为空，检查数据源/网络")

    # 东财/新浪历史接口有时到收盘后仍未更新当日 K 线；用当日全市场快照补上。
    today_rows = {r["code"]: r for r in rows if r.get("snap_date") == date}
    appended = 0
    for code, df in list(bars.items()):
        if df is None or df.empty or str(df["date"].iloc[-1]) >= date:
            continue
        r = today_rows.get(code)
        if not r:
            continue
        o = float(r.get("open", 0) or 0)
        h = float(r.get("high", 0) or 0)
        l = float(r.get("low", 0) or 0)
        c = float(r.get("price", 0) or 0)
        if min(o, h, l, c) <= 0:
            continue
        prev = float(df["close"].iloc[-1])
        pct = float(r.get("pct", 0) or 0)
        if abs(pct) < 1e-12 and prev > 0:
            pct = (c / prev - 1) * 100
        vol = float(r.get("volume", 0) or 0)
        amt = float(r.get("amount", 0) or 0)
        if vol <= 0 and amt > 0 and c > 0:
            vol = amt / c / 100.0
        prev_last = str(df["date"].iloc[-1])
        merged = pd.concat([df, pd.DataFrame([{
            "date": date, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "amount": amt, "pct": pct,
            "turnover": float(r.get("turnover", 0) or 0),
        }])], ignore_index=True)
        bars[code] = merged
        # 只有“上一行就是上一交易日”时才回写缓存；带缺口的 K 线不允许固化。
        if prev_day and prev_last == prev_day:
            try:
                with open(_daily_file(code), "wb") as f:
                    pickle.dump(merged, f)
            except Exception:
                pass
        appended += 1
    if appended:
        print(f"[info] 用当日快照补齐 {appended} 只个股 K 线")

    return MarketData(date=date, benchmark=str(params.get("timing.index_symbol")),
                      index=idx, bars=bars, meta=meta, source="akshare")
