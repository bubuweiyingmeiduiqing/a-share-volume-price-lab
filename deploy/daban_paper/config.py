# -*- coding: utf-8 -*-
"""
涨停接力模拟盘 v0.1 —— 参数与路径

对应设计：docs/01_系统设计_v0.1.md
策略骨架：L0 制度 → L1 市场状态 → L2 主线 → L3 身位 → L4 引擎 → L7 组合
执行假设：日线级。T 日收盘出计划，T+1 开盘竞价成交（打板/接力），不假设盘中成交。
        因此本模拟盘**不使用分时数据**，E2 低吸引擎不启用（设计文档 O3）。
"""

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
CACHE_DIR = BASE / "cache"
REPORT_DIR = BASE / "reports"
LOG_DIR = BASE / "logs"
for _d in (STATE_DIR, CACHE_DIR, REPORT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ============================================================================
# 账户
# ============================================================================
INITIAL_CAPITAL = 1_000_000.0
SLOTS = 3                       # 最多同时持仓
THEME_CAP = 2                   # 同题材最多持仓

# ============================================================================
# 交易成本（A 股）
# ============================================================================
COMMISSION_RATE = 0.00025       # 佣金 万2.5，双边
COMMISSION_MIN = 5.0            # 最低 5 元
STAMP_TAX_RATE = 0.0005         # 印花税，仅卖出
TRANSFER_FEE_RATE = 0.00001     # 过户费，双边
SLIPPAGE = 0.0010               # 单边滑点 0.10%

# ============================================================================
# L1 市场状态
# ============================================================================
E_HISTORY_MIN = 20              # E 分位最小样本
BOOTSTRAP_DAYS = 28             # 首次回补天数（东财池接口仅保留最近 ~30 个交易日）

STATE_CAP = {                   # 总仓位上限
    "ICE": 0.20,
    "REPAIR": 0.50,
    "EXPAND": 1.00,
    "DIVERGE": 0.50,
    "FADE": 0.00,
}
STATE_ENGINES = {               # 每个状态允许开新仓的引擎
    "ICE": [],
    "REPAIR": ["E3"],
    "EXPAND": ["E1", "E3"],
    "DIVERGE": [],
    "FADE": [],
}
STATE_CN = {
    "ICE": "冰点",
    "REPAIR": "修复",
    "EXPAND": "主升",
    "DIVERGE": "高位分歧",
    "FADE": "退潮",
}

E_TH_ICE = 22
E_TH_REPAIR = 50
E_TH_EXPAND = 65
HYSTERESIS = 5
MIN_DWELL = 2

# ============================================================================
# L2 主线
# ============================================================================
THEME_HEAT_WINDOW = 10
THEME_MAIN_PCTILE = 0.70
THEME_BURST = 8
THEME_MIN_HEAT = 2            # 今日该板块至少 2 只涨停，避免"1 只也算主线"

# ============================================================================
# L3 标的
# ============================================================================
LOW_PRICE = 10.0                # E3 低价
LOW_POSITION = 0.60             # E3 低位（现价 / 250 日最高）
MAX_BOARD_TRIGGER = 6           # 最高板 >= 6 触发高低切
BUYABLE_RANK = (1, 2)

# ============================================================================
# 引擎名称（贯穿报告、推送、日志）
# ============================================================================
ENGINE_NAME = {"E1": "接力", "E2": "低吸", "E3": "首板"}
ENGINE_FULL = {"E1": "接力打板", "E2": "水下低吸", "E3": "首板补涨"}
ENGINE_RULE = {
    "E1": "昨日≥2板 · 题材内前2身位 · 次日开盘 +2.5%~+8% 接力",
    "E2": "昨日炸板/大阴 · 低开 −3% 后分时止跌回升（需分时，未启用）",
    "E3": "昨日首板 · ≤10元 · 低位 · 次日开盘 +2%~+6% 打 1 进 2",
}


def eng(code: str, full: bool = False) -> str:
    """E1 -> 'E1 接力'；full=True -> 'E1 接力打板'"""
    name = (ENGINE_FULL if full else ENGINE_NAME).get(code, "")
    return f"{code} {name}".strip()


# ============================================================================
# L4 引擎
# ============================================================================
ENGINE = {
    "E1": {                     # 接力（打昨日 N 板）
        "gap_min": 0.025,
        "gap_max": 0.080,
        "tp": 0.08,
        "sl": 0.05,
        "max_hold": 3,
        "weight": 0.340,
    },
    "E3": {                     # 首板 1 进 2
        "gap_min": 0.020,
        "gap_max": 0.060,
        "tp": 0.06,
        "sl": 0.04,
        "max_hold": 2,
        "weight": 0.250,
    },
}

# 封板质量门槛
MIN_SEAL_AMOUNT_RATIO = 0.002   # 封单额 / 流通市值 >= 0.2%
SEAL_LATE_MINUTES = 240         # 14:00 之后封板视为弱板

# ============================================================================
# L6 退出
# ============================================================================
MA_SHORT = 5
ACCEL_VOL_RATIO = 0.80
ACCEL_GAP_SELL_HALF = 0.05

# ============================================================================
# L7 组合
# ============================================================================
CIRCUIT_DAILY = -0.03
CIRCUIT_DD_HALVE = 0.08
CIRCUIT_DD_STOP = 0.12
CIRCUIT_STOP_DAYS = 5

# ============================================================================
# 推送规则
# ============================================================================
PREMARKET_ONLY_WHEN_SIGNAL = False  # True=没有可执行候选就不推（安静，但难区分"空仓"和"挂了"）
PREMARKET_HEARTBEAT = True          # 无可买时推一条短心跳（推荐）
CLOSE_ALWAYS_PUSH = True            # 盘后：无论有无成交都推

PUSH_API = "https://www.pushplus.plus/send"
PUSH_TOKENS = [
    # 与 price_action_paper 共用同一组 PushPlus token（运行时从那边读取，避免重复维护）
]


def load_push_tokens():
    """优先用环境变量，其次复用 price_action_paper 的配置。"""
    env = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    if PUSH_TOKENS:
        return list(PUSH_TOKENS)
    # 复用邻居项目的 token（同一账号）
    cfg = Path.home() / "price_action_paper" / "config.py"
    if cfg.exists():
        ns = {}
        try:
            exec(compile(cfg.read_text(encoding="utf-8"), str(cfg), "exec"), ns)
            toks = ns.get("PUSH_TOKENS") or []
            if toks:
                return list(toks)
        except Exception:
            pass
    return []


# ============================================================================
# 报告
# ============================================================================
REPORT_TITLE_PREFIX = "打板模拟盘"
