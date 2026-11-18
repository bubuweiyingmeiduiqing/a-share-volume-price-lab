#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 config/regular_v1.yaml 生成参数手册，避免文档和参数表漂移。"""

from __future__ import annotations

from pathlib import Path

from config import load_params

GROUP_TITLE = {
    "universe": "股票池",
    "timing": "择时 / 门控",
    "selection": "选股 / 买点",
    "entry": "买入执行",
    "holding": "持有",
    "exit": "卖出",
    "portfolio": "组合 / 风控",
    "cost": "交易成本",
}
GROUP_ORDER = list(GROUP_TITLE)


def fmt(v, unit: str) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if unit == "pct":
        x = float(v) * 100
        return f"{x:.4f}%" if abs(x) < 0.01 else f"{x:.2f}%"
    if unit == "x":
        return f"{float(v):.2f}x"
    if unit == "weight":
        return f"{float(v):.0%}"
    if unit == "score":
        return f"{float(v):.0f}"
    if unit == "day":
        return f"{float(v):g} 日"
    if unit == "yuan":
        return f"{float(v):,.0f}"
    if unit == "count":
        return f"{float(v):g}"
    if unit == "frac":
        return f"{float(v):.0%}"
    return str(v)


def fmt_range(r, unit: str) -> str:
    if not r:
        return "—"
    if isinstance(r, (list, tuple)) and len(r) == 2:
        return f"{fmt(r[0], unit)} ~ {fmt(r[1], unit)}"
    return str(r)


def main() -> None:
    params = load_params(profile="balanced")
    out = Path(__file__).resolve().parents[2] / "docs" / "04_常规版_参数手册.md"
    specs = params.specs
    values = params.values
    L: list[str] = []
    L.append(f"# 常规量价模拟盘 · 参数手册 v{params.meta.get('version', '')}")
    L.append("")
    L.append("> 本文件由 `deploy/regular_paper/gen_param_doc.py` 从 "
             "`config/regular_v1.yaml` 自动生成。改参数后请重新生成，不要手改本文件。")
    L.append("")
    L.append(f"- 系统：{params.meta.get('name')} v{params.meta.get('version')}")
    L.append(f"- 参数总数：{len(specs)}；可调："
             f"{sum(1 for s in specs.values() if s.get('tunable'))}；"
             f"冻结：{sum(1 for s in specs.values() if not s.get('tunable'))}")
    L.append(f"- 当前档位：`{params.profile}`；参数指纹：`#{params.digest}`")
    L.append("- 约定：`pct` 是小数（0.05 = 5%）；`x` 是倍数；`day` 是交易日；"
             "`score` 是 0–100。")
    L.append("")

    L.append("## 0. 怎么改参数（先读这一节）")
    L.append("")
    L.append("1. **先改第一线参数，不要一次改多个。** 第一线参数见 §2；每季度最多改 1 个。")
    L.append("2. **回撤期不调参。** 先区分“规则失效”和“风格不适”，前者停用，后者降仓。")
    L.append("3. **改完必须留下样本。** 至少 30 笔同状态样本，或用合成场景验证规则没有反向。")
    L.append("4. **不要改 `tunable: false` 的参数。** 它们是结构/执行假设，改了就不是同一套系统。")
    L.append("5. **改完跑 `python3 selftest.py`**，再跑 `python3 run_daily.py --demo --force`，"
             "确认八条口诀仍然按预期触发。")
    L.append("")

    L.append("## 1. 风险档位")
    L.append("")
    L.append("| 档位 | 说明 | 覆盖参数 |")
    L.append("|---|---|---|")
    profiles = params.raw.get("profiles", {})
    for name in ("defensive", "balanced", "offensive"):
        p = profiles.get(name, {})
        ov = p.get("overrides", {}) or {}
        ovs = "；".join(f"`{k}`={v}" for k, v in ov.items()) or "无"
        L.append(f"| `{name}` | {p.get('description', '')} | {ovs} |")
    L.append("")

    L.append("## 2. 第一线参数（复盘优先只看这些）")
    L.append("")
    L.append("| 参数 | 当前值 | 允许区间 | 敏感度 | 口诀 | 说明 |")
    L.append("|---|---:|---|---|---|---|")
    for key in params.meta.get("tuning_priority", []):
        s = specs[key]
        L.append(f"| `{key}` | {fmt(values[key], s.get('unit'))} | "
                 f"{fmt_range(s.get('range'), s.get('unit'))} | "
                 f"{s.get('sensitivity', '')} | {s.get('aphorism', '')} | "
                 f"{str(s.get('note', '')).replace('|', '/')} |")
    L.append("")

    for group in GROUP_ORDER:
        keys = [k for k, s in specs.items() if s.get("group") == group]
        if not keys:
            continue
        L.append(f"## {GROUP_TITLE[group]}")
        L.append("")
        L.append("| 参数 ID | 默认 | 单位 | 允许区间 | 可调 | 敏感度 | 对应口诀 | 说明 |")
        L.append("|---|---:|---|---|:---:|---|---|---|")
        for key in keys:
            s = specs[key]
            L.append(f"| `{key}` | {fmt(values[key], s.get('unit'))} | "
                     f"{s.get('unit', '')} | {fmt_range(s.get('range'), s.get('unit'))} | "
                     f"{'✓' if s.get('tunable') else '✗'} | "
                     f"{s.get('sensitivity', '')} | {s.get('aphorism', '')} | "
                     f"{str(s.get('note', '')).replace('|', '/')} |")
        L.append("")

    L.append("## 3. 参数变更记录")
    L.append("")
    L.append("| 日期 | 参数 | 改前 | 改后 | 假设/样本 | 结果 |")
    L.append("|---|---|---|---|---|---|")
    L.append("| — | — | — | — | 首次建立，无变更 | — |")
    L.append("")
    L.append("> 变更记录建议同时写入 `docs/04_常规版_参数手册.md` 与版本库 commit message。"
             "没有记录的改动视为规则外交易。")
    L.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"written {out} ({len(L)} lines)")


if __name__ == "__main__":
    main()
