#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常规量价模拟盘 · 合成数据自检

不联网、不写生产 state。每个测试对应一条口诀或一个结构约束：
  - 横盘 + 小涨 → 候选
  - 深坑 + 趋势未坏 → 候选，且风险预算放大
  - 5 日 +25% 的竖 → 排除
  - 无量急跌 → 持仓不卖
  - 放量急跌 → 清仓
  - 连续大涨 → 清仓/减半
  - 冲高回落 → 减半
  - 市场过热 / 急跌 → 仓位门控
  - T+1 → 当日买入不可卖
  - 参数越界 → 拒绝启动

运行：python3 selftest.py
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

import board as bd
import data as dm
import deploy_check as dc
import portfolio as pf
import regime as rg
import review as rv
import store
import run_daily as rd
import run_premarket as rp
import webboard as wb
import strategy as stg
import synthetic as sy
from config import ConfigError, Params, load_params


def _with_index(base: dm.MarketData, index: pd.DataFrame) -> dm.MarketData:
    return dm.MarketData(date=base.date, benchmark=base.benchmark, index=index,
                         bars=copy.copy(base.bars), meta=copy.copy(base.meta),
                         source="synthetic-test")


def _hot_index(base: dm.MarketData) -> pd.DataFrame:
    idx = base.index.copy()
    # 最后 20 天每天 +1.1%，把指数推到 MA20 之上 10% 左右，并放量
    start = float(idx["close"].iloc[-21])
    for i in range(20):
        idx.loc[idx.index[-20 + i], "close"] = round(start * (1.011 ** (i + 1)), 2)
    idx["close"] = idx["close"].astype(float)
    idx["high"] = idx[["open", "close"]].max(axis=1) * 1.004
    idx["low"] = idx[["open", "close"]].min(axis=1) * 0.996
    idx["pct"] = idx["close"].pct_change().fillna(0) * 100
    idx.loc[idx.index[-1], "amount"] = float(idx["amount"].iloc[-21:-1].mean()) * 2.2
    return idx


def _crash_index(base: dm.MarketData) -> pd.DataFrame:
    idx = base.index.copy()
    prev = float(idx["close"].iloc[-2])
    idx.loc[idx.index[-1], "close"] = round(prev * 0.955, 2)
    idx.loc[idx.index[-1], "open"] = round(prev * 0.985, 2)
    idx.loc[idx.index[-1], "high"] = round(prev * 0.99, 2)
    idx.loc[idx.index[-1], "low"] = round(prev * 0.95, 2)
    idx.loc[idx.index[-1], "pct"] = -4.5
    idx.loc[idx.index[-1], "volume"] = float(idx["volume"].iloc[-2]) * 2.2
    idx.loc[idx.index[-1], "amount"] = float(idx["amount"].iloc[-2]) * 2.2
    return idx


class RegularPaperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.params = load_params(profile="balanced")
        # 测试里用固定交易日历，避免依赖网络
        cls.cal = [d.strftime("%Y%m%d") for d in pd.bdate_range("2024-01-01", "2027-12-31")]
        dm._CAL = cls.cal
        cls.market = sy.demo_market(cls.params)
        cls.regime = rg.evaluate(cls.market, cls.params, None, 0)

    # ------------------------------------------------------------------
    # 参数
    # ------------------------------------------------------------------
    def test_config_loads(self):
        self.assertGreater(len(self.params.values), 80)
        self.assertEqual(self.params.profile, "balanced")
        self.assertAlmostEqual(
            sum(self.params.get(k) for k in (
                "timing.weight_trend", "timing.weight_breadth", "timing.weight_momentum",
                "timing.weight_volume", "timing.weight_up")), 1.0, places=6)
        self.assertTrue(self.params.digest)

    def test_profile_changes_values_and_digest(self):
        d = load_params(profile="defensive")
        o = load_params(profile="offensive")
        self.assertLess(d.get("portfolio.risk_per_trade"), o.get("portfolio.risk_per_trade"))
        self.assertNotEqual(d.digest, o.digest)
        self.assertNotEqual(self.params.digest, d.digest)

    def test_bad_value_rejected(self):
        raw = yaml.safe_load(self.params.path.read_text(encoding="utf-8"))
        raw["parameters"]["timing.cap_trend"]["value"] = 1.5
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.yaml"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigError):
                Params(path=p)

    def test_bad_cross_check_rejected(self):
        raw = yaml.safe_load(self.params.path.read_text(encoding="utf-8"))
        raw["parameters"]["selection.deep_pit_depth"]["value"] = 0.40
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.yaml"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigError):
                Params(path=p)

    # ------------------------------------------------------------------
    # 择时门控
    # ------------------------------------------------------------------
    def test_regime_trend(self):
        self.assertEqual(self.regime.state, "TREND")
        self.assertTrue(self.regime.allow_new)
        self.assertGreaterEqual(self.regime.cap, 0.5)

    def test_regime_hot_blocks_new(self):
        hot = _with_index(self.market, _hot_index(self.market))
        r = rg.evaluate(hot, self.params, "TREND", 5)
        self.assertEqual(r.state, "HOT")
        self.assertFalse(r.allow_new)
        self.assertLessEqual(r.cap, 0.5)

    def test_regime_crash_clears(self):
        crash = _with_index(self.market, _crash_index(self.market))
        r = rg.evaluate(crash, self.params, "TREND", 5)
        self.assertEqual(r.state, "CRASH")
        self.assertFalse(r.allow_new)
        self.assertEqual(r.cap, 0.0)
        # 文案必须指出真正的触发条件，不能一律写“放量急跌”
        self.assertTrue(any("清仓：" in x for x in r.reasons), r.reasons)
        self.assertTrue(any("单日大跌" in x for x in r.reasons), r.reasons)

    def test_index_washout_not_crash(self):
        idx = self.market.index.copy()
        # 先把前一日抬高 5%，再做 -2.5% 的缩量急跌：仍站在 MA60 上方
        idx.loc[idx.index[-2], "close"] = round(float(idx["close"].iloc[-2]) * 1.05, 2)
        prev = float(idx["close"].iloc[-2])
        idx.loc[idx.index[-1], "close"] = round(prev * 0.975, 2)
        idx.loc[idx.index[-1], "open"] = round(prev * 0.998, 2)
        idx.loc[idx.index[-1], "high"] = round(prev * 1.001, 2)
        idx.loc[idx.index[-1], "low"] = round(prev * 0.972, 2)
        idx.loc[idx.index[-1], "pct"] = -2.5
        idx.loc[idx.index[-1], "volume"] = float(idx["volume"].iloc[-2]) * 0.5
        idx.loc[idx.index[-1], "amount"] = float(idx["amount"].iloc[-2]) * 0.5
        r = rg.evaluate(_with_index(self.market, idx), self.params, "TREND", 5)
        self.assertNotEqual(r.state, "CRASH")
        self.assertTrue(r.washout)

    # ------------------------------------------------------------------
    # 选股 / 买点
    # ------------------------------------------------------------------
    def test_platform_small_rise_candidate(self):
        c, why = stg.analyze_stock("600001", self.market.bars["600001"],
                                   self.market.meta["600001"], self.market.date,
                                   self.params, self.regime)
        self.assertIsNotNone(c, why)
        self.assertIn(c["setup"], ("PLATFORM", "PLATFORM_SMALL_RISE"))
        self.assertGreaterEqual(c["score"], self.params.get("selection.min_score"))

    def test_deep_pit_candidate_and_size(self):
        c, why = stg.analyze_stock("600002", self.market.bars["600002"],
                                   self.market.meta["600002"], self.market.date,
                                   self.params, self.regime)
        self.assertIsNotNone(c, why)
        self.assertEqual(c["setup"], "DEEP_PIT")
        self.assertGreaterEqual(c["size_mult"], 1.0)

    def test_vertical_veto(self):
        c, why = stg.analyze_stock("600003", self.market.bars["600003"],
                                   self.market.meta["600003"], self.market.date,
                                   self.params, self.regime)
        self.assertIsNone(c)
        self.assertIn("不买竖", why)

    def test_regime_cold_exception_toggle(self):
        idx = self.market.index.copy()
        n = len(idx)
        idx["close"] = (4200 - (4200 - 3600) * pd.Series(range(n), index=idx.index) / (n - 1)).round(2)
        idx["open"] = idx["close"].shift(1).fillna(idx["close"])
        idx["high"] = idx[["open", "close"]].max(axis=1) * 1.002
        idx["low"] = idx[["open", "close"]].min(axis=1) * 0.998
        idx["pct"] = idx["close"].pct_change().fillna(0) * 100
        cold = _with_index(self.market, idx)
        r = rg.evaluate(cold, self.params, None, 0)
        self.assertEqual(r.state, "COLD")
        self.assertTrue(r.allow_new)
        self.assertEqual(r.allowed_setups, ["DEEP_PIT"])
        p2 = copy.deepcopy(self.params)
        p2.values["timing.cold_deep_pit_enabled"] = False
        r2 = rg.evaluate(cold, p2, None, 0)
        self.assertEqual(r2.state, "COLD")
        self.assertFalse(r2.allow_new)
        self.assertEqual(r2.allowed_setups, [])

    def test_cold_deep_pit_exception(self):
        r = copy.copy(self.regime)
        r.state = "COLD"
        r.state_cn = "冰点"
        r.cap = 0.2
        r.allow_new = True
        r.allowed_setups = ["DEEP_PIT"]
        r.market_climax = False
        c, why = stg.analyze_stock("600002", self.market.bars["600002"],
                                   self.market.meta["600002"], self.market.date,
                                   self.params, r)
        self.assertIsNotNone(c, why)
        self.assertEqual(c["setup"], "DEEP_PIT")
        self.assertTrue(c["cold_exception"])
        self.assertAlmostEqual(c["size_mult"], 0.5, places=2)
        plan = stg.build_plan(self.market, r, self.params)
        self.assertTrue(plan["allow_new"])
        self.assertEqual({x["setup"] for x in plan["candidates"]}, {"DEEP_PIT"})

    def test_cold_exception_respects_one_position(self):
        r = copy.copy(self.regime)
        r.state = "COLD"
        r.cap = 0.2
        r.allow_new = True
        r.allowed_setups = ["DEEP_PIT"]
        c, why = stg.analyze_stock("600002", self.market.bars["600002"],
                                   self.meta("600002"), self.market.date,
                                   self.params, r)
        self.assertIsNotNone(c, why)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        acct = pf.load(self.params, Path(td.name) / "account.json")
        acct["positions"].append({
            "code": "600004", "name": "dummy", "setup": "PLATFORM",
            "industry": "X", "entry_date": "20261101", "entry_price": 10.0,
            "shares": 100, "cost_amount": 1000.0, "fee": 0.0,
            "entry_fee_per_share": 0.0, "stop": 9.0, "max_hold": 15,
            "partial_done": False, "last_close": 10.0, "peak_price": 10.0,
            "below_ma_days": 0, "entry_score": 60, "target_weight": 0.1,
            "risk_amount": 100.0, "entry_regime": "TREND",
        })
        plan = {"date": "20261111", "for_date": self.market.date, "state": "COLD",
                "cap": 0.2, "allow_new": True, "candidates": [c]}
        fills = rd.execute_plan(acct, plan, self.market.date, self.market, self.params)
        self.assertTrue(any("最多 1 只" in str(x.get("reason", "")) for x in fills), fills)

    def test_plan_contains_only_allowed_setups(self):
        plan = stg.build_plan(self.market, self.regime, self.params)
        self.assertTrue(plan["allow_new"])
        for c in plan["candidates"]:
            self.assertIn(c["setup"], self.regime.allowed_setups)

    # ------------------------------------------------------------------
    # 持仓 / 卖出
    # ------------------------------------------------------------------
    def _seed_and_settle(self, regime=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        state = Path(td.name)
        sy.seed_demo_account(self.params, self.market, state, force=True)
        acct = pf.load(self.params, state / "account.json")
        events = pf.settle(acct, self.market.date, self.market,
                           regime or self.regime, self.params)
        return acct, events

    def test_washout_hold(self):
        _, events = self._seed_and_settle()
        e = next(x for x in events if x["code"] == "600004")
        self.assertEqual(e["action"], "洗盘持有")

    def test_volume_dump_exit(self):
        _, events = self._seed_and_settle()
        e = next(x for x in events if x["code"] == "600005")
        self.assertEqual(e["action"], "卖出")
        self.assertIn("放量急跌", e["reason"])

    def test_climax_exit(self):
        _, events = self._seed_and_settle()
        e = next(x for x in events if x["code"] == "600006")
        self.assertEqual(e["action"], "卖出")
        self.assertIn("连续大涨", e["reason"])

    def test_failed_spike_reduce(self):
        _, events = self._seed_and_settle()
        e = next(x for x in events if x["code"] == "600007")
        self.assertEqual(e["action"], "减半")
        self.assertIn("冲高回落", e["reason"])

    def test_market_climax_reduces_profitable_position(self):
        r = copy.copy(self.regime)
        r.market_climax = True
        _, events = self._seed_and_settle(regime=r)
        e = next(x for x in events if x["code"] == "600007")
        self.assertEqual(e["action"], "减半")
        self.assertIn("人声鼎沸", e["reason"])

    # ------------------------------------------------------------------
    # 执行 / T+1 / 全流程
    # ------------------------------------------------------------------
    def test_buy_and_t_plus_one(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        state = Path(td.name) / "account.json"
        acct = pf.load(self.params, state)
        c, why = stg.analyze_stock("600001", self.market.bars["600001"],
                                   self.market.meta["600001"], self.market.date,
                                   self.params, self.regime)
        self.assertIsNotNone(c, why)
        r = pf.buy(acct, c, c["close"], self.market.date, self.regime.cap, 1.0, self.params)
        self.assertTrue(r["ok"])
        self.assertEqual(len(acct["positions"]), 1)
        events = pf.settle(acct, self.market.date, self.market, self.regime, self.params)
        # 当日买入，T+1 不卖；新仓不会出现在结算事件里
        self.assertEqual(len(acct["positions"]), 1)
        self.assertFalse(any(e.get("code") == "600001" for e in events))

    def test_execute_plan_buys(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        state = Path(td.name) / "account.json"
        acct = pf.load(self.params, state)
        c, why = stg.analyze_stock("600001", self.market.bars["600001"],
                                   self.meta("600001"), self.market.date,
                                   self.params, self.regime)
        self.assertIsNotNone(c, why)
        plan = {
            "date": "20261105", "for_date": self.market.date, "state": "TREND",
            "cap": 0.9, "allow_new": True, "candidates": [c],
        }
        fills = rd.execute_plan(acct, plan, self.market.date, self.market, self.params)
        self.assertTrue(any(x.get("action") == "买入" for x in fills), fills)
        self.assertEqual(len(acct["positions"]), 1)

    def meta(self, code):
        return self.market.meta[code]

    def test_push_counter_triggers_review_every_five(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            with mock.patch.object(rv, "run_review",
                                   return_value={"path": "x", "pushed": False}) as m:
                for i in range(1, 6):
                    rv.note_push("close", f"2026110{i}", self.params, state, reports)
                self.assertEqual(m.call_count, 1)
                c = rv.load_counter(state)
                self.assertEqual(c["close_pushes"], 5)
                self.assertEqual(c["last_review_close_count"], 5)
                # 同一天重复推送不重复计数
                rv.note_push("close", "20261105", self.params, state, reports)
                self.assertEqual(rv.load_counter(state)["close_pushes"], 5)

    def test_deploy_manifest_check(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            (rt / "state").mkdir()
            (rt / "a.py").write_text("print(1)\n", encoding="utf-8")
            manifest = {
                "commit": "abc123", "deployed_at": "now",
                "files": {"a.py": dc.sha256_file(rt / "a.py")},
            }
            (rt / "state" / "deploy_manifest.json").write_text(
                __import__("json").dumps(manifest), encoding="utf-8")
            self.assertTrue(dc.check(rt)["ok"])
            (rt / "a.py").write_text("print(2)\n", encoding="utf-8")
            self.assertFalse(dc.check(rt)["ok"])
            (rt / "b.py").write_text("print(3)\n", encoding="utf-8")
            res = dc.check(rt)
            self.assertIn("a.py", res["bad"])
            self.assertIn("b.py", res["extra"])

    def test_premarket_stale_plan_pushes_notice(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            sy.seed_demo_account(self.params, self.market, state, force=True)
            (state / "plan.json").write_text(
                '{"for_date": "20260101", "candidates": []}', encoding="utf-8")
            with mock.patch.object(rp.pusher, "push", return_value=2):
                rc = rp.run(self.market.date, True, self.params, state, reports)
            self.assertEqual(rc, 0)
            text = (reports / f"{dm.dash(self.market.date)}-premarket.md").read_text(encoding="utf-8")
            self.assertIn("昨日计划未更新", text)
            c = rv.load_counter(state)
            self.assertEqual(c["premarket_pushes"], 1)
            self.assertEqual(c["last_premarket_date"], self.market.date)

    def test_manual_review_label_and_next_count(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            sy.seed_demo_account(self.params, self.market, state, force=True)
            text, meta = rv.build_review(self.params, state, reports,
                                         close_count=7, manual=True)
            self.assertIn("手动复盘", text)
            self.assertIn("收盘推送第 7 次", text)
            self.assertIn("第 **10** 次", text)

    def test_review_holds_params_when_sample_small(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            sy.seed_demo_account(self.params, self.market, state, force=True)
            text, meta = rv.build_review(self.params, state, reports, close_count=5)
            self.assertEqual(meta["decision"], "保持")
            self.assertIn("样本不足", text)
            self.assertIn("参数决策", text)
            self.assertIn("不自动修改", text)

    def test_run_loads_market_when_none(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(dm, "load_market", return_value=self.market):
                rc = rd.run(self.market.date, False, self.params, force=True,
                            state_dir=Path(td) / "state",
                            report_dir=Path(td) / "reports")
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "reports" / "2026-11-06.md").exists())

    def test_full_demo_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            sy.seed_demo_account(self.params, self.market, state, force=True)
            rc = rd.run(self.market.date, False, self.params, force=True,
                        market=self.market, state_dir=state, report_dir=reports)
            self.assertEqual(rc, 0)
            report = (reports / "2026-11-06.md").read_text(encoding="utf-8")
            self.assertIn("常规量价模拟盘", report)
            self.assertIn("急跌无量是洗盘", report)
            self.assertIn("明日计划", report)
            self.assertIn("今日复盘", report)
            self.assertIn("体检", report)
            self.assertTrue((state / "plan.json").exists())


class IndexFallbackTest(unittest.TestCase):
    """指数日线滞后 / 断连时的多源降级与实时行情补齐（全离线，不写生产缓存）。"""

    @staticmethod
    def _frame(dates: list[str]) -> pd.DataFrame:
        n = len(dates)
        return pd.DataFrame({
            "date": dates,
            "open": [3900.0] * n,
            "high": [3950.0] * n,
            "low": [3850.0] * n,
            "close": [3900.0 + i for i in range(n)],
            "volume": [1e8] * n,
            "amount": [1e8] * n,
            "pct": [0.0] * n,
            "turnover": [0.0] * n,
        })

    @staticmethod
    def _quote(**kw) -> dict:
        base = {"date": "20261124", "clock": "15:35", "open": 4004.04,
                "prev_close": 3958.29, "close": 3963.82, "high": 4011.69,
                "low": 3957.92, "volume": 197381230.0, "source": "sina"}
        base.update(kw)
        return base

    def test_clock_must_be_after_close(self):
        self.assertTrue(dm._index_closed("15:00"))
        self.assertTrue(dm._index_closed("15:35:39"))
        self.assertFalse(dm._index_closed("14:59"))
        self.assertFalse(dm._index_closed(""))
        self.assertFalse(dm._index_closed(None))

    def test_quote_bar_rejects_intraday_wrong_date_and_bad_ohlc(self):
        for bad in (
            self._quote(clock="10:30"),
            self._quote(date="20261123"),
            self._quote(low=4000.0),          # 最低价高于收盘价
            self._quote(close=0.0),
        ):
            with mock.patch.object(dm, "_INDEX_QUOTE_SOURCES", (lambda s, q=bad: q,)):
                self.assertIsNone(dm._index_quote_bar("000300", "20261124"))

    def test_quote_bar_units_match_cache(self):
        with mock.patch.object(dm, "_INDEX_QUOTE_SOURCES", (lambda s: self._quote(),)):
            bar = dm._index_quote_bar("000300", "20261124")
        self.assertIsNotNone(bar)
        self.assertEqual(bar["date"], "20261124")
        self.assertAlmostEqual(bar["close"], 3963.82)
        self.assertAlmostEqual(bar["pct"], (3963.82 / 3958.29 - 1) * 100, places=6)
        # amount 沿用缓存口径（成交量手数），不能写成成交额，否则势能比值会差几个数量级
        self.assertAlmostEqual(bar["amount"], bar["volume"])

    def test_sina_quote_parsing_unit_per_market(self):
        def quote_text(name, vol, amt):
            fields = [name, "4004.0355", "3958.2938", "3963.8232", "4011.6853",
                      "3957.9221", "0", "0", str(vol), str(amt)] + ["0"] * 20 + \
                     ["2026-11-24", "15:35:39", "00", ""]
            return 'var hq_str_x="' + ",".join(fields) + '";'

        # 沪市指数报“手”：197,381,230 手 ≈ 成交额 5068.7 亿元
        with mock.patch.object(dm, "_http_text",
                               return_value=quote_text("沪深300", 197381230,
                                                       506865912791)):
            q = dm._sina_index_quote("000300")
        self.assertAlmostEqual(q["close"], 3963.8232)
        self.assertAlmostEqual(q["volume"], 197381230.0)

        # 深市指数报“股”：59,066,319,644 股要折成 590,663,196 手
        with mock.patch.object(dm, "_http_text",
                               return_value=quote_text("深证成指", 59066319644,
                                                       883224915525)):
            q = dm._sina_index_quote("399001")
        self.assertAlmostEqual(q["volume"], 590663196.44)
        self.assertEqual(q["date"], "20261124")

    def test_tx_quote_parsing(self):
        fields = ["1", "沪深300", "000300", "3963.82", "3958.29", "4004.04",
                  "197381230"] + ["0"] * 23 + ["20261124161409"] + ["0"] * 7
        fields[33], fields[34] = "4011.69", "3957.92"
        text = 'v_sh000300="' + "~".join(fields) + '";'
        with mock.patch.object(dm, "_http_text", return_value=text):
            q = dm._tx_index_quote("000300")
        self.assertIsNotNone(q)
        self.assertEqual(q["date"], "20261124")
        self.assertEqual(q["clock"], "16:14")
        self.assertAlmostEqual(q["high"], 4011.69)
        self.assertAlmostEqual(q["volume"], 197381230.0)

    def test_em_quote_parsing_uses_beijing_time(self):
        ts = int(dt.datetime(2026, 11, 24, 7, 35, tzinfo=dt.timezone.utc).timestamp())
        payload = json.dumps({"data": {"f43": 396382, "f44": 401169, "f45": 395792,
                                       "f46": 400404, "f47": 197381230,
                                       "f48": 506865912791, "f59": 2,
                                       "f60": 395829, "f86": ts}})
        with mock.patch.object(dm, "_http_text", return_value=payload):
            q = dm._em_index_quote("000300")
        self.assertIsNotNone(q)
        self.assertEqual(q["date"], "20261124")
        self.assertEqual(q["clock"], "15:35")
        self.assertAlmostEqual(q["close"], 3963.82)

    def test_sina_kline_parsing_scales_volume(self):
        payload = json.dumps([
            {"day": "2026-11-23", "open": "3931.465", "high": "3968.448",
             "low": "3901.493", "close": "3958.294", "volume": "20394325600"},
            {"day": "2026-11-24", "open": "4004.036", "high": "4011.685",
             "low": "3957.922", "close": "3963.823", "volume": "19738123000"},
        ])
        with mock.patch.object(dm, "_http_text", return_value=payload):
            df = dm._sina_index_klines("000300", 120)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df["date"]), ["20261123", "20261124"])
        self.assertAlmostEqual(float(df["volume"].iloc[-1]), 197381230.0)
        self.assertAlmostEqual(float(df["pct"].iloc[-1]),
                               (3963.823 / 3958.294 - 1) * 100, places=6)

    def test_index_daily_fills_today_and_persists_cache(self):
        dates = ["20261118", "20261119", "20261120", "20261123"]
        with tempfile.TemporaryDirectory() as td:
            idx_dir = Path(td) / "index"
            idx_dir.mkdir()
            with mock.patch.object(dm, "INDEX_DIR", idx_dir), \
                    mock.patch.object(dm, "_index_hist",
                                      return_value=self._frame(dates)), \
                    mock.patch.object(dm, "prev_trade_date", return_value="20261123"), \
                    mock.patch.object(dm, "_INDEX_QUOTE_SOURCES",
                                      (lambda s: self._quote(),)):
                df = dm.index_daily("000300", "20261124", 60)
            self.assertEqual(str(df["date"].iloc[-1]), "20261124")
            self.assertAlmostEqual(float(df["close"].iloc[-1]), 3963.82)
            cached = pd.read_pickle(idx_dir / "000300.pkl")
            self.assertEqual(str(cached["date"].iloc[-1]), "20261124")

    def test_index_daily_refuses_gap_fill(self):
        dates = ["20261118", "20261119", "20261120", "20261123"]
        with tempfile.TemporaryDirectory() as td:
            idx_dir = Path(td) / "index"
            idx_dir.mkdir()
            with mock.patch.object(dm, "INDEX_DIR", idx_dir), \
                    mock.patch.object(dm, "_index_hist",
                                      return_value=self._frame(dates)), \
                    mock.patch.object(dm, "prev_trade_date", return_value="20261120"), \
                    mock.patch.object(dm, "_INDEX_QUOTE_SOURCES",
                                      (lambda s: self._quote(),)):
                df = dm.index_daily("000300", "20261124", 60)
        # 缺口超过一天时宁可用昨天收盘，也不拼一根“假”K 线
        self.assertEqual(str(df["date"].iloc[-1]), "20261123")


class BoardTest(unittest.TestCase):
    """收盘研究看板：只读上下文，必须先保证“断连不炸收盘”。"""

    @classmethod
    def setUpClass(cls):
        cls.params = load_params(profile="balanced")
        demo = sy.demo_market(cls.params)
        cls.market = dm.MarketData(date=demo.date, benchmark="000300",
                                   index=demo.index, bars=demo.bars,
                                   meta=demo.meta, source="akshare")

    @staticmethod
    def _concept_frames():
        now = pd.DataFrame({
            "行业": ["物理AI", "PCB板", "银行"],
            "行业-涨跌幅": [3.2, 1.1, -0.6],
            "净额": ["12.5", "3.2", "-2.3"],
            "公司家数": [45, 60, 42],
        })
        d5 = pd.DataFrame({
            "行业": ["物理AI", "PCB板", "银行"],
            "阶段涨跌幅": [8.5, 4.2, -3.2],
            "净额": ["38.2", "-7.29", "-15.0"],
            "公司家数": [45, 60, 42],
        })
        return now, d5

    @staticmethod
    def _stock_frames(codes):
        n = len(codes)
        now = pd.DataFrame({
            "股票代码": codes, "股票简称": [f"股{i}" for i in range(n)],
            "最新价": [10.0] * n, "涨跌幅": ["1.2%"] * n, "换手率": ["3.5%"] * n,
            "净额": [f"{i * 0.3:.2f}亿" for i in range(n)],
        })
        d5 = pd.DataFrame({
            "股票代码": codes, "股票简称": [f"股{i}" for i in range(n)],
            "阶段涨跌幅": [f"{i * 1.5:.2f}%" for i in range(n)],
            "资金流入净额": [f"{i * 1.1:.2f}亿" for i in range(n)],
        })
        return now, d5

    def test_fund_flow_units(self):
        self.assertAlmostEqual(bd._to_yi("1.25亿"), 1.25)
        self.assertAlmostEqual(bd._to_yi("-5602.50万"), -0.56025, places=6)
        self.assertAlmostEqual(bd._to_yi("0.65"), 0.65)
        self.assertEqual(bd._to_yi("--"), 0.0)
        self.assertAlmostEqual(bd._pct("-1.2%"), -1.2)
        self.assertEqual(bd._pct("--"), 0.0)

    def test_limit_hit_by_board(self):
        hit = bd._limit_hit
        self.assertTrue(hit({"code": "600000", "name": "浦发银行", "pct": 9.9}, True))
        self.assertFalse(hit({"code": "600000", "name": "浦发银行", "pct": 9.0}, True))
        self.assertTrue(hit({"code": "300001", "name": "特锐德", "pct": 19.9}, True))
        self.assertTrue(hit({"code": "688001", "name": "华兴源创", "pct": -19.9}, False))
        self.assertTrue(hit({"code": "830001", "name": "某北交", "pct": 29.9}, True))
        self.assertTrue(hit({"code": "600001", "name": "ST某股", "pct": 4.9}, True))
        self.assertFalse(hit({"code": "600001", "name": "ST某股", "pct": 4.9}, False))

    def test_sector_tags(self):
        tag = board_sector_tag = bd.sector_tag
        self.assertEqual(tag({"f5": 10, "f1": 2, "chg": 3}), "早期修复")
        self.assertEqual(tag({"f5": 10, "f1": -2, "chg": 1}), "资金分歧")
        self.assertEqual(tag({"f5": -10, "f1": -2, "chg": -1}), "退潮")
        self.assertEqual(tag({"f5": -10, "f1": 2, "chg": 1}), "短线反抽")
        self.assertEqual(tag({"f5": 0, "f1": 0, "chg": 0}), "观察")
        del board_sector_tag

    def test_sections_render_with_sources(self):
        codes = list(self.market.bars.keys())
        c_now, c_d5 = self._concept_frames()
        s_now, s_d5 = self._stock_frames(codes)
        idx = self.market.index.tail(60).reset_index(drop=True).copy()
        idx["date"] = idx["date"].astype(str)
        rows = [{"code": c, "name": "", "pct": 1.0, "amount": 1e9,
                 "snap_date": self.market.date} for c in codes]
        with mock.patch.object(bd.ak, "stock_fund_flow_concept",
                               side_effect=[c_now, c_d5]), \
                mock.patch.object(bd.ak, "stock_fund_flow_individual",
                                  side_effect=[s_now, s_d5]), \
                mock.patch.object(bd.dm, "index_daily", return_value=idx), \
                mock.patch.object(bd.dm, "spot_snapshot", return_value=rows), \
                mock.patch.object(bd, "limit_counts", return_value=(64, 4, "东财涨跌停池")), \
                mock.patch.object(bd, "_is_today", return_value=True):
            text = "\n".join(bd.build(self.market, self.market.date, self.params,
                                       plan={"candidates": [{"code": codes[2]}]}))
        self.assertIn("大盘环境", text)
        self.assertIn("板块资金扫描", text)
        self.assertIn("核心龙头筛选", text)
        self.assertIn("早期修复", text)
        self.assertIn("★计划内", text)
        self.assertIn("研究候选，不是成交信号", text)

    def test_sector_empty_state_when_nothing_qualifies(self):
        now = pd.DataFrame({
            "行业": ["银行", "白酒"],
            "行业-涨跌幅": [-0.6, -2.1],
            "净额": ["-2.3", "-1.1"],
            "公司家数": [42, 20],
        })
        d5 = pd.DataFrame({
            "行业": ["银行", "白酒"],
            "阶段涨跌幅": [-3.2, -4.5],
            "净额": ["-15.0", "-9.0"],
            "公司家数": [42, 20],
        })
        with mock.patch.object(bd.ak, "stock_fund_flow_concept",
                               side_effect=[now, d5]):
            text = "\n".join(bd.sector_section("20261125", self.params))
        self.assertIn("没有板块通过早期修复条件", text)
        self.assertNotIn("| 排名 |", text)

    def test_truncated_snapshot_is_labelled(self):
        codes = list(self.market.bars.keys())
        idx = self.market.index.tail(60).reset_index(drop=True).copy()
        idx["date"] = idx["date"].astype(str)
        rows = [{"code": c, "name": "", "pct": 1.0, "amount": 1e9,
                 "snap_date": self.market.date} for c in codes]
        with mock.patch.object(bd.dm, "index_daily", return_value=idx), \
                mock.patch.object(bd.dm, "spot_snapshot", return_value=rows), \
                mock.patch.object(bd, "limit_counts", return_value=(64, 4, "东财涨跌停池")):
            text = "\n".join(bd.index_section(self.market, self.market.date, self.params))
        self.assertIn("截断快照", text)
        self.assertNotIn("全市场：", text)

    def test_section_degrades_when_source_fails(self):
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        with mock.patch.object(bd.dm, "index_daily", side_effect=boom), \
                mock.patch.object(bd.ak, "stock_fund_flow_concept", side_effect=boom), \
                mock.patch.object(bd.ak, "stock_fund_flow_individual", side_effect=boom):
            lines = bd.build(self.market, self.market.date, self.params)
        text = "\n".join(lines)
        self.assertIn("本节跳过", text)
        self.assertNotIn("Traceback", text)

    def test_skips_synthetic_and_historical(self):
        syn = dm.MarketData(date=self.market.date, benchmark="000300",
                            index=self.market.index, bars=self.market.bars,
                            meta=self.market.meta, source="synthetic")
        self.assertEqual(bd.build(syn, syn.date, self.params), [])
        with mock.patch.object(bd, "index_section", return_value=["**大盘环境**", ""]), \
                mock.patch.object(bd, "_is_today", return_value=False):
            lines = bd.build(self.market, "20261116", self.params)
        self.assertIn("历史日期回放", "\n".join(lines))

    def test_report_includes_board_when_enabled(self):
        url = "https://example.test/board-x/d/20261106.html"
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            reports = Path(td) / "reports"
            sy.seed_demo_account(self.params, self.market, state, force=True)
            with mock.patch.object(bd, "enabled_for", return_value=True), \
                    mock.patch.object(bd, "collect",
                                      return_value={"date": self.market.date}), \
                    mock.patch.object(bd, "render", return_value={
                        "index": ["**大盘环境**", "- 看板占位", ""],
                        "sectors": [], "leaders": []}), \
                    mock.patch.object(rd.wb, "publish", return_value=url):
                rc = rd.run(self.market.date, False, self.params, force=True,
                            market=self.market, state_dir=state, report_dir=reports)
            self.assertEqual(rc, 0)
            text = (reports / "2026-11-06.md").read_text(encoding="utf-8")
            self.assertIn("看板占位", text)
            self.assertIn(url, text)      # 链接必须进日报（= 进推送）


class BoardStoreTest(unittest.TestCase):
    """看板数据落盘（store.py）与网页渲染（webboard.py）：离线、临时目录。"""

    @classmethod
    def setUpClass(cls):
        cls.params = load_params(profile="balanced")

    @staticmethod
    def _data(date="20261125") -> dict:
        return {
            "date": date,
            "generated_at": "2026-11-25T16:00:00+08:00",
            "historical": False,
            "regime": {"state": "CRASH", "cn": "急跌", "heat": 7.8, "cap": 0.0,
                       "allow_new": False, "dwell": 1},
            "indices": [
                {"name": "上证指数", "code": "000001", "close": 3476.47, "ret5": -3.1,
                 "ret20": -5.57, "ma20": 3613.0, "above": False, "ext": -3.79,
                 "ma20_chg": -0.96},
            ],
            "market": {"amount_yi": 8988.0, "n": 799, "up": 105, "down": 689,
                       "zt": 51, "dt": 45, "limit_src": "快照", "pool_n": 300,
                       "pool_above5": 30.0},
            "sectors": [{"name": "固体电池", "f5": 12.5, "f1": 3.2, "norm5": 0.28,
                         "chg": 1.2, "chg5": 4.4, "n": 45, "score": 88.0,
                         "tag": "早期修复", "repair": True, "rank": 1}],
            "sectors_all": [{"name": "固体电池", "f5": 12.5, "f1": 3.2, "norm5": 0.28,
                             "chg": 1.2, "chg5": 4.4, "n": 45, "score": 88.0,
                             "tag": "早期修复", "repair": True}],
            "watch": [{"name": "银行", "f1": 23.6, "f5": -15.3, "chg": -0.02}],
            "leaders": [{"code": "300308", "name": "中际旭创", "industry": "通信",
                         "f5": 22.2, "f1": 4.0, "ratio": 0.51, "chg5": 3.2,
                         "ext": -0.12, "score": 91.0, "in_plan": True}],
            "leaders_all": [{"code": "300308", "name": "中际旭创", "industry": "通信",
                             "f5": 22.2, "f1": 4.0, "ratio": 0.51, "chg5": 3.2,
                             "ext": -0.12, "score": 91.0, "in_plan": True}],
            "trades": [{"code": "601988", "name": "中国银行", "action": "卖出",
                        "price": 7.49, "reason": "市场急跌清仓", "ret": 0.068, "setup": ""}],
            "positions": [],
        }

    def test_save_day_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = store.connect(Path(td) / "board.db")
            try:
                data = self._data()
                store.save_day(conn, data, {"equity": 1034576, "day_ret": 0.0007,
                                            "cash": 1034576, "mv": 0, "positions_n": 0},
                               {"for_date": "20261126", "state": "CRASH",
                                "allow_new": False, "candidates": []})
                store.save_day(conn, data, {"equity": 1034576, "day_ret": 0.0007,
                                            "cash": 1034576, "mv": 0, "positions_n": 0},
                               {"for_date": "20261126", "state": "CRASH",
                                "allow_new": False, "candidates": []})
                n_days = conn.execute("SELECT COUNT(*) c FROM days").fetchone()["c"]
                n_ind = conn.execute("SELECT COUNT(*) c FROM indices").fetchone()["c"]
                n_sec = conn.execute("SELECT COUNT(*) c FROM sectors").fetchone()["c"]
                n_lea = conn.execute("SELECT COUNT(*) c FROM leaders").fetchone()["c"]
                trade = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
                row = conn.execute("SELECT * FROM days WHERE date='20261125'").fetchone()
            finally:
                conn.close()
        self.assertEqual((n_days, n_ind, n_sec, n_lea, trade), (1, 1, 1, 1, 1))
        self.assertEqual(row["regime_state"], "CRASH")
        self.assertEqual(row["plan_candidates_n"], 0)
        self.assertAlmostEqual(row["equity"], 1034576)

    def test_seed_equity_and_weekly(self):
        with tempfile.TemporaryDirectory() as td:
            conn = store.connect(Path(td) / "board.db")
            try:
                store.seed_equity(conn, [
                    {"date": "20261123", "equity": 1000000, "ret": 0.0, "cash": 1, "mv": 0},
                    {"date": "20261124", "equity": 1034576, "ret": 0.03, "cash": 1, "mv": 0},
                ])
                store.save_day(conn, self._data("20261125"),
                               {"equity": 1034576, "day_ret": 0.0, "cash": 1, "mv": 0,
                                "positions_n": 0}, None)
                days = store.recent_days(conn, "20261125", 5)
                w = store.weekly(conn, "20261125", 5)
            finally:
                conn.close()
        self.assertEqual([d["date"] for d in days], ["20261123", "20261124", "20261125"])
        self.assertEqual(w["days"], 3)
        self.assertAlmostEqual(w["ret"], 0.034576, places=6)
        self.assertEqual(w["top_sectors"][0]["name"], "固体电池")

    def test_render_html_has_charts_and_url(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "board.db"
            site = Path(td) / "site"
            site.mkdir()
            params = load_params(profile="balanced")
            params.runtime["board"] = {"enabled": True, "store_db": str(db),
                                       "site_dir": str(site),
                                       "base_url": "https://example.test/board-x/"}
            conn = store.connect(db)
            try:
                store.seed_equity(conn, [{"date": "20261124", "equity": 1000000,
                                          "ret": 0, "cash": 1, "mv": 0}])
            finally:
                conn.close()
            url = wb.publish(self._data(), {"equity": 1034576, "day_ret": 0.0007,
                                            "cash": 1034576, "mv": 0, "positions_n": 0,
                                            "equity_history": [
                                                {"date": "20261124", "equity": 1000000,
                                                 "ret": 0, "cash": 1, "mv": 0}]},
                             {"for_date": "20261126", "state": "CRASH",
                              "allow_new": False, "candidates": []}, params)
            self.assertEqual(url, "https://example.test/board-x/d/20261125.html")
            page = (site / "d" / "20261125.html").read_text(encoding="utf-8")
            self.assertTrue((site / "index.html").exists())
            self.assertTrue((site / "weekly.html").exists())
            self.assertTrue((Path(td) / "board" / "20261125.json").exists())
        self.assertIn("常规量价模拟盘", page)
        self.assertIn("<svg", page)              # 净值/宽度曲线
        self.assertIn("bar-fill", page)          # 资金条形图
        self.assertIn("固体电池", page)
        self.assertIn("viewport", page)

    def test_spark_and_bars_helpers(self):
        self.assertIn("<polyline", wb.svg_spark([1, 2, 3]))
        self.assertIn("暂无数据", wb.css_bars([]))
        out = wb.css_bars([{"label": "A", "value": 1, "text": "+1"}])
        self.assertIn("bar-fill", out)
        self.assertIn("A", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
