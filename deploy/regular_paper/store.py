# -*- coding: utf-8 -*-
"""
看板数据仓：SQLite（可查询）+ 每日 JSON（可搬运/回放）。

为什么要两套：
  - SQLite 给“每周复盘 / 新功能”直接写 SQL，按 (date, key) 主键幂等，
    重跑同一天只会覆盖当天的行，不会重复；
  - 每日 JSON 保留原始结构（字段改名/加字段时还能回放），也方便直接看。

约定：
  - 日期一律 TEXT 'YYYYMMDD'；金额单位：亿元（资金）/ 元（净值）；比例一律百分数还是小数要看清列名，
    这里统一：*_pct 用百分数，day_ret 用小数（与 account.json 一致）。
  - 表结构变更走 SCHEMA_VERSION + migrate()，不要手改线上库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS days (
    date            TEXT PRIMARY KEY,
    generated_at    TEXT,
    historical      INTEGER DEFAULT 0,
    amount_yi       REAL,
    up              INTEGER,
    down            INTEGER,
    zt              INTEGER,
    dt              INTEGER,
    limit_src       TEXT,
    snap_n          INTEGER,
    pool_n          INTEGER,
    pool_above5     REAL,
    regime_state    TEXT,
    regime_heat     REAL,
    regime_cap      REAL,
    regime_allow_new INTEGER,
    regime_dwell    INTEGER,
    equity          REAL,
    day_ret         REAL,
    cash            REAL,
    mv              REAL,
    positions_n     INTEGER,
    plan_for_date   TEXT,
    plan_state      TEXT,
    plan_allow_new  INTEGER,
    plan_candidates_n INTEGER
);
CREATE TABLE IF NOT EXISTS indices (
    date     TEXT,
    code     TEXT,
    name     TEXT,
    close    REAL,
    ret5     REAL,
    ret20    REAL,
    ma20     REAL,
    above    INTEGER,
    ext      REAL,
    ma20_chg REAL,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS sectors (
    date   TEXT,
    name   TEXT,
    f5     REAL,
    f1     REAL,
    norm5  REAL,
    chg    REAL,
    chg5   REAL,
    n      INTEGER,
    score  REAL,
    tag    TEXT,
    rank   INTEGER,
    repair INTEGER,
    PRIMARY KEY (date, name)
);
CREATE TABLE IF NOT EXISTS watch (
    date TEXT,
    name TEXT,
    f1   REAL,
    f5   REAL,
    chg  REAL,
    PRIMARY KEY (date, name)
);
CREATE TABLE IF NOT EXISTS leaders (
    date     TEXT,
    code     TEXT,
    name     TEXT,
    industry TEXT,
    f5       REAL,
    f1       REAL,
    ratio    REAL,
    chg5     REAL,
    ext      REAL,
    score    REAL,
    in_plan  INTEGER,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS trades (
    date   TEXT,
    code   TEXT,
    name   TEXT,
    action TEXT,
    price  REAL,
    reason TEXT,
    ret    REAL,
    setup  TEXT,
    PRIMARY KEY (date, code, action)
);
CREATE TABLE IF NOT EXISTS positions (
    date       TEXT,
    code       TEXT,
    name       TEXT,
    setup      TEXT,
    entry_price REAL,
    last_close REAL,
    ret        REAL,
    stop       REAL,
    hold_days  INTEGER,
    PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_sectors_date_f5 ON sectors(date, f5 DESC);
CREATE INDEX IF NOT EXISTS idx_leaders_date_f5 ON leaders(date, f5 DESC);
CREATE INDEX IF NOT EXISTS idx_days_date ON days(date);
"""


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    elif int(row["value"]) > SCHEMA_VERSION:
        raise RuntimeError(f"board.db schema {row['value']} 比代码 {SCHEMA_VERSION} 新，先升级代码")
    conn.commit()
    return conn


def _upsert(conn: sqlite3.Connection, table: str, cols: list[str], row: dict) -> None:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({keys}) VALUES ({marks})",
                 [row.get(c) for c in cols])


def save_day(conn: sqlite3.Connection, data: dict, account: dict | None = None,
             plan: dict | None = None) -> None:
    """写一天的看板数据；同一天重复写 = 覆盖，不会产生重复行。"""
    date = str(data.get("date", ""))
    if not date:
        raise ValueError("save_day 需要 data['date']")
    mk = data.get("market") or {}
    rg = data.get("regime") or {}
    hist = data.get("historical", False)
    acct = account or {}
    day = {
        "date": date,
        "generated_at": data.get("generated_at"),
        "historical": 1 if hist else 0,
        "amount_yi": mk.get("amount_yi"),
        "up": mk.get("up"),
        "down": mk.get("down"),
        "zt": mk.get("zt"),
        "dt": mk.get("dt"),
        "limit_src": mk.get("limit_src"),
        "snap_n": mk.get("n"),
        "pool_n": mk.get("pool_n"),
        "pool_above5": mk.get("pool_above5"),
        "regime_state": rg.get("state"),
        "regime_heat": rg.get("heat"),
        "regime_cap": rg.get("cap"),
        "regime_allow_new": None if rg.get("allow_new") is None else int(bool(rg["allow_new"])),
        "regime_dwell": rg.get("dwell"),
        "equity": account.get("equity") if account else None,
        "day_ret": account.get("day_ret") if account else None,
        "cash": account.get("cash") if account else None,
        "mv": account.get("mv") if account else None,
        "positions_n": account.get("positions_n") if account else None,
        "plan_for_date": (plan or {}).get("for_date"),
        "plan_state": (plan or {}).get("state"),
        "plan_allow_new": None if not plan else int(bool(plan.get("allow_new"))),
        "plan_candidates_n": None if not plan else len(plan.get("candidates") or []),
    }
    _upsert(conn, "days", list(day.keys()), day)

    for r in data.get("indices") or []:
        _upsert(conn, "indices",
                ["date", "code", "name", "close", "ret5", "ret20", "ma20",
                 "above", "ext", "ma20_chg"],
                {"date": date, "code": r.get("code"), "name": r.get("name"),
                 "close": r.get("close"), "ret5": r.get("ret5"), "ret20": r.get("ret20"),
                 "ma20": r.get("ma20"), "above": int(bool(r.get("above"))),
                 "ext": r.get("ext"), "ma20_chg": r.get("ma20_chg")})

    for i, r in enumerate(data.get("sectors") or [], 1):
        _upsert(conn, "sectors",
                ["date", "name", "f5", "f1", "norm5", "chg", "chg5", "n",
                 "score", "tag", "rank", "repair"],
                {"date": date, "name": r.get("name"), "f5": r.get("f5"), "f1": r.get("f1"),
                 "norm5": r.get("norm5"), "chg": r.get("chg"), "chg5": r.get("chg5"),
                 "n": r.get("n"), "score": r.get("score"), "tag": r.get("tag"),
                 "rank": r.get("rank", i), "repair": int(bool(r.get("repair")))})

    for r in data.get("watch") or []:
        _upsert(conn, "watch", ["date", "name", "f1", "f5", "chg"],
                {"date": date, "name": r.get("name"), "f1": r.get("f1"),
                 "f5": r.get("f5"), "chg": r.get("chg")})

    for r in data.get("leaders") or []:
        _upsert(conn, "leaders",
                ["date", "code", "name", "industry", "f5", "f1", "ratio",
                 "chg5", "ext", "score", "in_plan"],
                {"date": date, "code": r.get("code"), "name": r.get("name"),
                 "industry": r.get("industry"), "f5": r.get("f5"), "f1": r.get("f1"),
                 "ratio": r.get("ratio"), "chg5": r.get("chg5"), "ext": r.get("ext"),
                 "score": r.get("score"), "in_plan": int(bool(r.get("in_plan")))})

    for r in data.get("trades") or []:
        _upsert(conn, "trades", ["date", "code", "name", "action", "price",
                                 "reason", "ret", "setup"],
                {"date": date, "code": r.get("code"), "name": r.get("name"),
                 "action": r.get("action"), "price": r.get("price"),
                 "reason": r.get("reason"), "ret": r.get("ret"), "setup": r.get("setup")})

    for r in data.get("positions") or []:
        _upsert(conn, "positions", ["date", "code", "name", "setup", "entry_price",
                                    "last_close", "ret", "stop", "hold_days"],
                {"date": date, "code": r.get("code"), "name": r.get("name"),
                 "setup": r.get("setup"), "entry_price": r.get("entry_price"),
                 "last_close": r.get("last_close"), "ret": r.get("ret"),
                 "stop": r.get("stop"), "hold_days": r.get("hold_days")})
    conn.commit()


def seed_equity(conn: sqlite3.Connection, equity_history: list[dict]) -> int:
    """把 account.json 里的历史净值补进 days 表（只补有日期、且该日还没有净值的行）。"""
    n = 0
    for h in equity_history or []:
        d = str(h.get("date") or "")
        if not d:
            continue
        row = conn.execute("SELECT equity FROM days WHERE date=?", (d,)).fetchone()
        if row is not None and row["equity"] is not None:
            continue
        if row is None:
            conn.execute("INSERT INTO days(date, equity, day_ret, cash, mv) VALUES (?,?,?,?,?)",
                         (d, h.get("equity"), h.get("ret"), h.get("cash"), h.get("mv")))
        else:
            conn.execute("UPDATE days SET equity=?, day_ret=?, cash=?, mv=? WHERE date=?",
                         (h.get("equity"), h.get("ret"), h.get("cash"), h.get("mv"), d))
        n += 1
    conn.commit()
    return n


def recent_days(conn: sqlite3.Connection, end: str, n: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM days WHERE date<=? ORDER BY date DESC LIMIT ?", (end, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def daily_sectors(conn: sqlite3.Connection, date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sectors WHERE date=? ORDER BY score DESC", (date,)).fetchall()
    return [dict(r) for r in rows]


def weekly(conn: sqlite3.Connection, end: str, n: int = 5) -> dict:
    """最近 n 个交易日的复盘底稿：净值变化、板块资金累计、强势板块、龙头命中情况。"""
    days = recent_days(conn, end, n)
    if not days:
        return {}
    dates = [d["date"] for d in days]
    first, last = days[0], days[-1]
    eq0 = next((d["equity"] for d in days if d["equity"]), None)
    eq1 = last.get("equity")
    rows = conn.execute(
        "SELECT name, SUM(f5) AS f5, AVG(score) AS score, COUNT(*) AS appear "
        "FROM sectors WHERE date IN (%s) GROUP BY name ORDER BY f5 DESC"
        % ",".join("?" * len(dates)), dates).fetchall()
    sectors = [dict(r) for r in rows]
    lead = conn.execute(
        "SELECT COUNT(*) AS n, SUM(in_plan) AS planned FROM leaders WHERE date IN (%s)"
        % ",".join("?" * len(dates)), dates).fetchone()
    return {
        "start": first["date"],
        "end": last["date"],
        "days": len(days),
        "equity_start": eq0,
        "equity_end": eq1,
        "ret": (eq1 / eq0 - 1) if (eq0 and eq1) else None,
        "top_sectors": sectors[:8],
        "bottom_sectors": sectors[-5:][::-1] if len(sectors) > 5 else [],
        "leader_rows": lead["n"] if lead else 0,
        "leader_planned": lead["planned"] if lead else 0,
    }


def dump_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(path)
