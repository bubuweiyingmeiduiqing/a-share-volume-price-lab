#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""charts.py —— 把可行性检验结果画成一张四联图（数据来自 out/*.json）"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplc-"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Songti SC", "STHeiti"]
plt.rcParams["axes.unicode_minus"] = False

HERE = Path(__file__).parent
OUT = HERE.parent / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

sc = json.loads((HERE / "out" / "scenarios.json").read_text(encoding="utf-8"))
ar = json.loads((HERE / "out" / "arithmetic.json").read_text(encoding="utf-8"))


def pick(group, tag=None):
    rows = [r for r in sc if r.get("group") == group]
    if tag:
        rows = [r for r in rows if r.get("tag") == tag]
    return rows[0]


fig, axes = plt.subplots(2, 2, figsize=(13, 9))
fig.suptitle("涨停接力系统 · 可行性检验（合成路径，非回测）", fontsize=15, fontweight="bold")

# ---- (1) 盈亏结构 vs 保本胜率 ----
ax = axes[0][0]
rows = ar["breakeven"]
labels = [f"{r['tp']:.0%}/{r['sl']:.0%}" for r in rows]
be = [r["breakeven"] * 100 for r in rows]
bars = ax.bar(labels, be, color=["#d9534f" if b > 55 else "#5cb85c" for b in be])
ax.axhline(45, ls="--", lw=1.2, color="#333")
ax.text(len(labels) - 0.6, 46.5, "现实胜率 45%", ha="right", fontsize=9)
for b, v in zip(bars, be):
    ax.text(b.get_x() + b.get_width() / 2, v + 1, f"{v:.0f}%", ha="center", fontsize=9)
ax.set_ylim(0, 80)
ax.set_ylabel("保本所需胜率 (%)")
ax.set_xlabel("止盈/止损")
ax.set_title("① 3%/5% 需 66% 胜率才保本（成本 0.30%）", fontsize=11)
ax.grid(axis="y", alpha=0.3)

# ---- (2) edge_scale 扫描 ----
ax = axes[0][1]
q2 = [r for r in sc if r.get("group") == "Q2" and r.get("tag", "").startswith("edge=")]
q2.sort(key=lambda r: float(r["tag"].split("=")[1]))
x = [float(r["tag"].split("=")[1]) for r in q2]
y = [r["ann_median"] * 100 for r in q2]
ax.plot(x, y, "o-", color="#2c7fb8", lw=2, label="年化收益中位数")
ax.axhline(0, color="#333", lw=1)
ax.fill_between(x, 0, y, where=[v < 0 for v in y], color="#d9534f", alpha=0.15)
ax.fill_between(x, 0, y, where=[v >= 0 for v in y], color="#5cb85c", alpha=0.15)
be_row = [r for r in sc if r.get("tag") == "breakeven"]
if be_row:
    ax.axvline(be_row[0]["edge_scale"], ls="--", color="#d9534f", lw=1.2)
    ax.annotate(
        f"保本点 edge≈{be_row[0]['edge_scale']:.2f}\n单笔毛收益 ≈ +0.34%",
        xy=(be_row[0]["edge_scale"], 0),
        xytext=(0.02, -15),
        fontsize=9,
        arrowprops=dict(arrowstyle="->", color="#d9534f"),
    )
ax.set_xlabel("edge_scale（假设边际的缩放）")
ax.set_ylabel("年化收益中位数 (%)")
ax.set_title("② 边际扫描：必须超过摩擦才有正期望", fontsize=11)
ax.grid(alpha=0.3)

# ---- (3) 摩擦敏感性 ----
ax = axes[1][0]
q3b = sorted([r for r in sc if r.get("group") == "Q3b"], key=lambda r: float(r["tag"].split("=")[1]))
c = [float(r["tag"].split("=")[1]) * 100 for r in q3b]
y = [r["ann_median"] * 100 for r in q3b]
cols = ["#5cb85c" if v > 0 else "#d9534f" for v in y]
bars = ax.bar([f"{v:.2f}%" for v in c], y, color=cols)
for b, v in zip(bars, y):
    ax.text(b.get_x() + b.get_width() / 2, v + (0.8 if v > 0 else -1.8),
            f"{v:+.1f}%", ha="center", fontsize=9)
ax.axhline(0, color="#333", lw=1)
ax.set_ylabel("年化收益中位数 (%)")
ax.set_ylim(-26, 8)
ax.set_xlabel("单笔往返摩擦")
ax.set_title("③ 摩擦是第一变量：0.5%→0.15% 差 25 个百分点", fontsize=11)
ax.grid(axis="y", alpha=0.3)

# ---- (4) 周转与槽位 ----
ax = axes[1][1]
q3a = sorted([r for r in sc if r.get("group") == "Q3a"], key=lambda r: r["trades"])
ax.plot([r["trades"] for r in q3a], [r["cost_drag"] * 100 for r in q3a],
        "s-", color="#d9534f", lw=2, label="成本拖累 / 资金")
ax.set_xlabel("交易笔数（500 交易日窗口，约 2 年）")
ax.set_ylabel("成本拖累 (%)", color="#d9534f")
ax.tick_params(axis="y", labelcolor="#d9534f")
ax.grid(alpha=0.3)
ax2 = ax.twinx()
ax2.plot([r["trades"] for r in q3a], [r["ann_median"] * 100 for r in q3a],
         "o--", color="#2c7fb8", lw=2, label="年化收益")
ax2.set_ylabel("年化收益中位数 (%)", color="#2c7fb8")
ax2.tick_params(axis="y", labelcolor="#2c7fb8")
ax.set_title("④ 周转越高，成本吃掉越多；但降周转救不了负边际", fontsize=11)

fig.tight_layout(rect=(0, 0, 1, 0.96))
p = OUT / "feasibility.png"
fig.savefig(p, dpi=150)
print("saved", p)
