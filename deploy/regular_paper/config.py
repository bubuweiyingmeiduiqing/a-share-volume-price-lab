# -*- coding: utf-8 -*-
"""
常规量价模拟盘 · 参数加载与标准化

唯一参数源：config/regular_v1.yaml
- 代码里不写死策略阈值，全部通过 Params.get("模块.对象.指标") 读取；
- 加载时做类型、区间、枚举、交叉一致性校验；
- 支持 defensive / balanced / offensive 三个风险档；
- 参数摘要带 hash，报告里可追溯“这次跑的是哪套参数”。

路径解析顺序：
1. 环境变量 REGULAR_CONFIG
2. deploy/regular_paper/config/regular_v1.yaml（独立部署时的副本）
3. <workspace>/config/regular_v1.yaml（仓库内唯一源）
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
CACHE_DIR = BASE / "cache"
REPORT_DIR = BASE / "reports"
LOG_DIR = BASE / "logs"
DEMO_DIR = BASE / "demo"
for _d in (STATE_DIR, CACHE_DIR, REPORT_DIR, LOG_DIR, DEMO_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_PROFILE = "balanced"
VALID_PROFILES = ("defensive", "balanced", "offensive")

STATE_CN = {
    "COLD": "冰点",
    "REPAIR": "修复",
    "TREND": "趋势",
    "HOT": "过热",
    "CRASH": "急跌",
}
STATE_ORDER = ("COLD", "REPAIR", "TREND", "HOT", "CRASH")

SETUP_CN = {
    "PLATFORM": "横盘",
    "PIT_REVERSAL": "坑反转",
    "DEEP_PIT": "深坑",
    "SMALL_RISE": "连续小涨",
    "PLATFORM_SMALL_RISE": "横盘+小涨",
}
SETUP_PRIORITY = {
    "DEEP_PIT": 4,
    "PIT_REVERSAL": 3,
    "PLATFORM_SMALL_RISE": 2,
    "PLATFORM": 1,
    "SMALL_RISE": 0,
}

# 每个状态允许的新开仓形态（结构约束，不放进 YAML 让用户随便打开）
STATE_SETUPS = {
    "COLD": [],
    "REPAIR": ["PLATFORM", "PLATFORM_SMALL_RISE", "PIT_REVERSAL", "DEEP_PIT"],
    "TREND": ["PLATFORM", "PLATFORM_SMALL_RISE", "PIT_REVERSAL", "DEEP_PIT", "SMALL_RISE"],
    "HOT": [],
    "CRASH": [],
}


class ConfigError(ValueError):
    """参数表不合法时直接拒绝启动，避免带着错参数跑一天。"""


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("REGULAR_CONFIG", "").strip()
    if env:
        paths.append(Path(env).expanduser())
    paths.append(BASE / "config" / "regular_v1.yaml")
    paths.append(BASE.parents[1] / "config" / "regular_v1.yaml")
    return paths


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"参数文件不存在：{p}")
        return p
    for p in _candidate_paths():
        if p.exists():
            return p
    raise ConfigError(
        "找不到 regular_v1.yaml。请设置 REGULAR_CONFIG，或把参数文件放到 "
        f"{BASE / 'config' / 'regular_v1.yaml'}"
    )


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _same_type(value: Any, default: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int) and not isinstance(default, bool):
        return isinstance(value, int)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (list, tuple)):
        return isinstance(value, (list, tuple))
    return True


class Params:
    """加载后的参数集。所有值仍可通过 .values 查看，但业务代码应使用 get()。"""

    def __init__(self, path: str | Path | None = None, profile: str | None = None):
        self.path = resolve_config_path(path)
        self.raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.raw, dict) or "parameters" not in self.raw:
            raise ConfigError(f"{self.path} 缺少 parameters 段")

        self.meta = self.raw.get("meta", {})
        self.runtime = copy.deepcopy(self.raw.get("runtime", {}))
        self.specs: dict[str, dict] = self.raw["parameters"]
        self.profile = (profile or os.environ.get("REGULAR_PROFILE") or DEFAULT_PROFILE).strip()
        if self.profile not in VALID_PROFILES:
            raise ConfigError(f"未知 profile：{self.profile}，可选 {VALID_PROFILES}")

        self.values: dict[str, Any] = {}
        for key, spec in self.specs.items():
            if not isinstance(spec, dict) or "value" not in spec:
                raise ConfigError(f"参数 {key} 缺少 value")
            self.values[key] = copy.deepcopy(spec["value"])

        self._apply_profile()
        self._validate()
        self.digest = self._digest()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if key not in self.values:
            if default is not None:
                return default
            raise KeyError(f"参数未定义：{key}")
        return self.values[key]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def spec(self, key: str) -> dict:
        return self.specs[key]

    def rt(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.runtime
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # ------------------------------------------------------------------
    # profile
    # ------------------------------------------------------------------
    def _apply_profile(self) -> None:
        profiles = self.raw.get("profiles", {}) or {}
        if self.profile not in profiles:
            raise ConfigError(f"profile 段缺少 {self.profile}")
        overrides = (profiles[self.profile] or {}).get("overrides", {}) or {}
        for key, value in overrides.items():
            if key not in self.specs:
                raise ConfigError(f"profile {self.profile} 覆盖了不存在的参数：{key}")
            if not self.specs[key].get("tunable", False):
                raise ConfigError(f"profile {self.profile} 试图覆盖冻结参数：{key}")
            self.values[key] = copy.deepcopy(value)

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        for key, spec in self.specs.items():
            value = self.values[key]
            default = spec.get("value")
            if not _same_type(value, default):
                raise ConfigError(
                    f"{key} 类型错误：期望 {type(default).__name__}，实际 "
                    f"{type(value).__name__}（{value!r}）"
                )
            unit = spec.get("unit")
            if unit in ("pct", "x", "weight", "score", "day", "count", "yuan", "frac"):
                if not _is_number(value):
                    raise ConfigError(f"{key} 应为数值，实际 {value!r}")
            if "range" in spec:
                lo, hi = spec["range"]
                if not (lo <= float(value) <= hi):
                    raise ConfigError(
                        f"{key}={value} 超出允许区间 [{lo}, {hi}]；"
                        "如确需越界，先修改 YAML 的 range 并在 CHANGELOG 说明理由"
                    )
            if "choices" in spec and value not in spec["choices"]:
                raise ConfigError(f"{key}={value!r} 不在 {spec['choices']} 内")
            if "tunable" not in spec:
                raise ConfigError(f"{key} 缺少 tunable 标记")

        self._cross_check()

    def _num(self, key: str) -> float:
        return float(self.values[key])

    def _cross_check(self) -> None:
        wsum = sum(
            self._num(k) for k in (
                "timing.weight_trend", "timing.weight_breadth",
                "timing.weight_momentum", "timing.weight_volume",
                "timing.weight_up",
            )
        )
        if abs(wsum - 1.0) > 1e-6:
            raise ConfigError(f"timing 五个权重之和应为 1.0，当前 {wsum:.4f}")

        if not (self._num("timing.cold_heat") < self._num("timing.trend_heat")
                < self._num("timing.hot_heat")):
            raise ConfigError("温度阈值必须满足 cold_heat < trend_heat < hot_heat")

        if not (self._num("selection.pit_min_depth")
                < self._num("selection.deep_pit_depth")
                < self._num("selection.pit_max_depth")):
            raise ConfigError("坑深度必须满足 pit_min_depth < deep_pit_depth < pit_max_depth")

        if self._num("entry.gap_min") >= self._num("entry.gap_max"):
            raise ConfigError("entry.gap_min 必须小于 entry.gap_max")
        if self._num("entry.pit_gap_min") >= self._num("entry.gap_max"):
            raise ConfigError("entry.pit_gap_min 必须小于 entry.gap_max")
        if self._num("entry.max_chase") <= self._num("entry.gap_max"):
            raise ConfigError("entry.max_chase 应大于 entry.gap_max，否则硬性追高线没有意义")
        if self._num("entry.min_stop_pct") >= self._num("entry.stop_loss"):
            raise ConfigError("entry.min_stop_pct 必须小于 entry.stop_loss")

        if self._num("selection.small_rise_min") >= self._num("selection.small_rise_max"):
            raise ConfigError("small_rise_min 必须小于 small_rise_max")
        if self._num("selection.small_rise_cum_min") >= self._num("selection.small_rise_cum_max"):
            raise ConfigError("small_rise_cum_min 必须小于 small_rise_cum_max")
        if self._num("selection.small_rise_vol_min") >= self._num("selection.small_rise_vol_max"):
            raise ConfigError("small_rise_vol_min 必须小于 small_rise_vol_max")

        if self._num("holding.trail_start") <= 0:
            raise ConfigError("holding.trail_start 必须为正")
        if self._num("holding.trail_pct") >= self._num("holding.trail_start"):
            raise ConfigError("trail_pct 应小于 trail_start，否则刚启动就容易被扫掉")

        if self._num("portfolio.max_weight") > 1.0:
            raise ConfigError("portfolio.max_weight 不能大于 1")
        if self._num("portfolio.risk_per_trade") <= 0:
            raise ConfigError("portfolio.risk_per_trade 必须为正")
        if self._num("portfolio.max_portfolio_heat") <= self._num("portfolio.risk_per_trade"):
            raise ConfigError("max_portfolio_heat 应至少大于单笔风险，否则只能开一笔")

        for key in ("timing.cap_cold", "timing.cap_repair", "timing.cap_trend",
                    "timing.cap_hot", "timing.cap_crash"):
            if not (0.0 <= self._num(key) <= 1.0):
                raise ConfigError(f"{key} 必须在 [0, 1]")

        # 风险预算若大于 单票上限×止损距离，会被权重上限反复截断；给出明确警告
        implied = self._num("portfolio.max_weight") * self._num("entry.stop_loss")
        if self._num("portfolio.risk_per_trade") > implied:
            raise ConfigError(
                "portfolio.risk_per_trade 大于 max_weight × stop_loss，"
                f"单笔风险预算永远无法用满（{self._num('portfolio.risk_per_trade'):.3%} > "
                f"{implied:.3%}）。请调低风险或提高单票上限。"
            )

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    def _digest(self) -> str:
        canonical = json.dumps(self.values, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    def summary(self) -> str:
        return (f"{self.meta.get('name', 'regular_paper')} "
                f"v{self.meta.get('version', '?')} / {self.profile} / #{self.digest}")

    def parameter_rows(self) -> list[dict]:
        rows = []
        for key, spec in self.specs.items():
            rows.append({
                "id": key,
                "value": self.values[key],
                "unit": spec.get("unit", ""),
                "range": spec.get("range", ""),
                "tunable": bool(spec.get("tunable")),
                "sensitivity": spec.get("sensitivity", ""),
                "group": spec.get("group", ""),
                "aphorism": spec.get("aphorism", ""),
                "note": spec.get("note", ""),
            })
        return rows

    def export(self) -> dict:
        return {
            "meta": self.meta,
            "profile": self.profile,
            "digest": self.digest,
            "values": self.values,
            "runtime": self.runtime,
        }


def load_params(path: str | Path | None = None, profile: str | None = None) -> Params:
    return Params(path=path, profile=profile)


def eng_label(setup: str) -> str:
    """'PIT_REVERSAL' -> '坑反转'"""
    return SETUP_CN.get(setup, setup)


if __name__ == "__main__":
    p = load_params()
    print(p.summary())
    print(f"参数 {len(p.values)} 个，可调 {sum(1 for s in p.specs.values() if s.get('tunable'))} 个")
    for k in ("timing.cap_trend", "selection.min_score", "entry.stop_loss",
              "portfolio.risk_per_trade"):
        print(f"  {k} = {p.get(k)}")
