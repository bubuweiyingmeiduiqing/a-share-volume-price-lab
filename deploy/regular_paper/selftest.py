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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

import data as dm
import portfolio as pf
import regime as rg
import review as rv
import run_daily as rd
import run_premarket as rp
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
