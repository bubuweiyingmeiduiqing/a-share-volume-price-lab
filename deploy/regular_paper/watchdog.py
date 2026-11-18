#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收盘后运行体检（watchdog）。

它回答一个很具体的问题：**今天该有的两次推送，是不是都真的发生了？**

检查项：
  1. 盘前 09:15 推送（计数器 / reports/YYYY-MM-DD-premarket.md）
  2. 收盘 16:20 推送（计数器 / reports/YYYY-MM-DD.md）
  3. 净值记录是否缺交易日（数据源失败最容易在这里留下缺口）

处置：
  - 收盘缺失且当天还没结算 → 自动补跑一次 run_daily.py（带 flock，避免与 cron 抢跑）
  - 盘前缺失 → 无法补发，只告警；同时检查 crontab 里 09:15 那行是否还在
  - 正常日不推送；只有异常才推，并用 state/watchdog.json 去重，避免反复打扰

用法：
  python3 watchdog.py                    # 检查今天
  python3 watchdog.py --date 20261117    # 检查指定日
  python3 watchdog.py --no-retry         # 只检查，不补跑
  python3 watchdog.py --no-push          # 只检查，不推送
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import data as dm
import deploy_check
import portfolio as pf
import push as pusher
import review as rev
from config import REPORT_DIR, STATE_DIR, load_params

BASE = Path(__file__).resolve().parent
WATCHDOG_FILE = "watchdog.json"


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def _is_trade_day(date: str) -> bool:
    try:
        return date in dm.trade_dates()
    except Exception:
        try:
            d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
            return d.weekday() < 5
        except Exception:
            return True


def _load_wd(state_dir: Path) -> dict:
    p = Path(state_dir) / WATCHDOG_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_wd(d: dict, state_dir: Path) -> None:
    p = Path(state_dir) / WATCHDOG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _report_mtime(path: Path) -> str:
    if not path.exists():
        return "—"
    return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")


def _close_ok(counter: dict, date: str, report_dir: Path) -> bool:
    return (counter.get("last_close_date") == date
            and (Path(report_dir) / f"{dm.dash(date)}.md").exists())


def _pre_ok(counter: dict, date: str, report_dir: Path) -> bool:
    # 计数只在 push 成功后写入；文件存在但没计数，说明推送可能失败了。
    return counter.get("last_premarket_date") == date


def _equity_missing(acct: dict, date: str, n: int = 10) -> list[str]:
    try:
        ds = [d for d in dm.trade_dates() if d <= date][-n:]
    except Exception:
        return []
    have = {str(h.get("date")) for h in acct.get("equity_history", [])}
    if have:
        first = min(have)
        ds = [d for d in ds if d >= first]
    return [d for d in ds if d not in have]


# ---------------------------------------------------------------------------
# 补跑
# ---------------------------------------------------------------------------

def retry_close(date: str, log_path: Path, do_push: bool = True,
                state_dir: Path | None = None,
                report_dir: Path | None = None) -> int:
    """补跑收盘。返回进程退出码；75 表示 flock 被占用（原任务还在跑）。"""
    cmd = [sys.executable, str(BASE / "run_daily.py"), "--date", date, "--force"]
    if not do_push:
        cmd.append("--no-push")
    if state_dir:
        cmd += ["--state-dir", str(state_dir)]
    if report_dir:
        cmd += ["--report-dir", str(report_dir)]
    flock = shutil.which("flock")
    lock_dir = Path.home() / ".local/state/kai/locks"
    lock = lock_dir / "regular-paper.lock"
    if flock and lock_dir.exists():
        cmd = [flock, "-n", "-E", "75", str(lock),
               "/usr/bin/timeout", "-k", "15s", "20m"] + cmd
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n[watchdog] {dt.datetime.now():%Y-%m-%d %H:%M:%S} 补跑 {date}\n")
        f.flush()
        try:
            r = subprocess.run(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
                               timeout=1300)
            return r.returncode
        except subprocess.TimeoutExpired:
            f.write("[watchdog] 补跑超时\n")
            return 124


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(date: str | None = None, do_push: bool = True, retry: bool = True,
        state_dir: Path | None = None, report_dir: Path | None = None) -> int:
    date = dm.norm_date(date or dt.date.today())
    state_dir = Path(state_dir or STATE_DIR)
    report_dir = Path(report_dir or REPORT_DIR)
    params = load_params()

    if not _is_trade_day(date):
        print(f"[info] {dm.dash(date)} 非交易日，体检跳过")
        return 0

    acct = pf.load(params, state_dir / "account.json")
    counter = rev.load_counter(state_dir)
    close_path = report_dir / f"{dm.dash(date)}.md"
    pre_path = report_dir / f"{dm.dash(date)}-premarket.md"

    close_before = _close_ok(counter, date, report_dir)
    pre_before = _pre_ok(counter, date, report_dir)
    retried = False
    retry_rc = None

    # 收盘缺失、且当天还没结算 → 补跑一次。已结算但只缺推送时不重跑，避免重复成交。
    if not close_before and retry and acct.get("last_run") != date:
        print(f"[warn] {dm.dash(date)} 收盘日报缺失，尝试补跑 run_daily.py")
        retry_rc = retry_close(date, BASE / "logs" / "watchdog.log",
                               do_push=do_push, state_dir=state_dir,
                               report_dir=report_dir)
        retried = True
        acct = pf.load(params, state_dir / "account.json")
        counter = rev.load_counter(state_dir)
        close_before = _close_ok(counter, date, report_dir)

    pre_ok = _pre_ok(counter, date, report_dir)
    close_ok = _close_ok(counter, date, report_dir)
    miss_eq = _equity_missing(acct, date)

    alerts: list[tuple[str, str]] = []
    if not pre_ok:
        alerts.append((
            "premarket_missing",
            f"- 盘前推送缺失：计数器最后盘前 {dm.dash(counter.get('last_premarket_date') or '—')}，"
            f"`{pre_path.name}` {'不存在' if not pre_path.exists() else '存在但计数未记'}。"
            "\n  先查 `crontab -l | grep regular_paper` 是否还有 09:15 那行。"
        ))
    if not close_ok:
        if acct.get("last_run") == date:
            why = "账户已结算到当天，但收盘推送后计数未记（push 可能失败）"
        elif retried and retry_rc == 75:
            why = "补跑时 flock 被占用（原任务仍在运行）"
        elif retried:
            why = f"自动补跑未成功（退出码 {retry_rc}）"
        else:
            why = "未运行或运行失败"
        alerts.append((
            "close_missing",
            f"- 收盘推送缺失：{dm.dash(date)} 日报不存在或计数未记；{why}。"
            "\n  先看 `tail -100 ~/regular_paper/logs/cron.log`。"
        ))

    # 只对“上一交易日”的缺口告警；更早的缺口写进日报即可，避免每天重复提醒。
    # 收盘都缺时，净值缺口是结果不是原因，也不重复告警。
    try:
        prev_day = dm.prev_trade_date(date)
    except Exception:
        prev_day = None
    fresh_gap = [d for d in miss_eq if prev_day and d == prev_day]
    if close_ok and fresh_gap:
        alerts.append((
            "equity_gap",
            "- 上一交易日净值未记录：" + "、".join(dm.dash(x) for x in fresh_gap)
            + "。当天可能没有结算，回看 `logs/cron.log`。"
        ))

    # 部署纪律：运行目录里的代码必须与最近一次仓库部署清单一致。
    dc = deploy_check.check(state_dir.parent)
    if not dc.get("present"):
        alerts.append((
            "no_manifest",
            "- 未找到 `state/deploy_manifest.json`：当前代码可能不是从仓库部署的。"
            "\n  请用 `~/a-share-volume-price-lab/deploy/deploy_vps.sh` 从 commit 重新部署。"
        ))
    elif not dc.get("ok"):
        alerts.append((
            "code_drift",
            "- VPS 代码与部署清单不一致：" + deploy_check.summary(dc)
            + "。\n  禁止直接改 VPS；请提交到仓库后重新部署。"
        ))

    # 正常日不推；异常按 (日期, 告警码) 去重。
    wd = _load_wd(state_dir)
    sent = set(wd.get(date, {}).get("sent", []))
    new = [a for a in alerts if a[0] not in sent]

    print(f"[info] 体检 {dm.dash(date)}：盘前 {'✅' if pre_ok else '⚠️'}｜"
          f"收盘 {'✅' if close_ok else '⚠️'}"
          + (f"｜补跑 {retry_rc}" if retried else "")
          + (f"｜净值缺 {len(miss_eq)} 天" if miss_eq else ""))

    if not alerts:
        # 如果之前报过异常、现在恢复了，记一笔，避免下次重复判断。
        if wd.get(date, {}).get("sent"):
            wd[date]["recovered"] = True
            wd[date] = {k: v for k, v in wd[date].items() if k in ("sent", "recovered")}
            _save_wd(wd, state_dir)
        print("[info] 推送与数据均正常，不推送")
        return 0

    if not new:
        print(f"[info] {len(alerts)} 项异常均已告警过，去重跳过")
        return 0

    text = [f"## 常规量价模拟盘 · 运行体检 {dm.dash(date)}", ""]
    text.append(f"⚠️ 发现 **{len(new)}** 项异常：")
    text.extend(a[1] for a in new)
    text.append("")
    text.append(f"盘前：{'✅ ' + _report_mtime(pre_path) if pre_ok else '⚠️ 缺失'}｜"
                f"收盘：{'✅ ' + _report_mtime(close_path) if close_ok else '⚠️ 缺失'}")
    text.append(f"账户：净值 {pf.equity(acct):,.0f}｜持仓 {len(acct.get('positions', []))} 只｜"
                f"累计 {pf.stats(acct)['total_ret']:+.2%}")
    text.append("")
    text.append("> 体检只负责“有没有跑、有没有推”，不判断行情。"
                "正常日不推送，避免打扰。")
    body = "\n".join(text)
    print(body)

    pushed = False
    if do_push:
        title = (f"[{params.rt('push', 'title_prefix', default='常规量价模拟盘')}] "
                 f"运行体检 {dm.dash(date)[5:]}：{len(new)} 项异常")
        n = pusher.push(title, body, params)
        pushed = n > 0
        print(f"[info] 告警推送 {n} 个 token")

    if pushed:
        wd.setdefault(date, {}).setdefault("sent", [])
        wd[date]["sent"] = sorted(set(wd[date]["sent"]) | {a[0] for a in new})
        # 只保留最近 30 天，防止文件无限增长
        wd = dict(sorted(wd.items())[-30:])
        _save_wd(wd, state_dir)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-dir", default=None)
    a = ap.parse_args()
    try:
        return run(a.date, not a.no_push, not a.no_retry,
                   Path(a.state_dir) if a.state_dir else None,
                   Path(a.report_dir) if a.report_dir else None)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
