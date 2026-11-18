# 涨停强势股短线系统 · 设计与可行性验证 v0.1

把岑若曦《短线的最高境界是不停的追逐涨停》里的 22 条经验法则，拼装成一条**可执行、无内部矛盾**的决策链，
并在**不跑回测、不查行情数据**的前提下，先判断它是否值得去回测。

---

## 三句话结论

1. **原文的参数假设是自洽的，但仓位超配 3 倍。** 「3%/5% + 70% 胜率」只支持 11% 的单笔仓位，而 3:3:3 要求 77.5% 的胜率才成立。
2. **冲突必须先消除，否则不能组合。** S6/S7/S8 三种买点互斥、S16 快止盈与 S9 拿住加速直接矛盾、两个"分歧"含义相反。解法是分层正交 + 单调收缩 + 引擎互斥。
3. **保本线是"单笔毛收益 +0.34%"。** 摩擦是第一变量：0.5% 摩擦下年化 −21.4%，0.15% 下 +3.6%。**降低滑点比选股更重要。**

---

## 目录

| 文件 | 内容 |
|---|---|
| [`docs/01_系统设计_v0.1.md`](docs/01_系统设计_v0.1.md) | 完整设计：L0–L8 分层、状态机、三引擎、退出梯度、一天运行时序、参数表 |
| [`docs/02_可行性验证报告.md`](docs/02_可行性验证报告.md) | 可行性检验 F1–F8：结构、数据、算术、凯利、成本、自由度、压力测试 |
| [`docs/figures/feasibility.png`](docs/figures/feasibility.png) | 四联图：保本胜率 / 边际扫描 / 摩擦敏感性 / 周转 |
| [`config/system_v0.1.yaml`](config/system_v0.1.yaml) | 参数表（22 个可调，15 个冻结） |
| [`feasibility/checks.py`](feasibility/checks.py) | 算术检验（保本胜率、凯利、成本、自由度、状态驻留） |
| [`feasibility/montecarlo.py`](feasibility/montecarlo.py) | 单情景合成路径压力测试（非回测） |
| [`feasibility/scenarios.py`](feasibility/scenarios.py) | 多情景对比（纪律 / 边际 / 周转 / 摩擦 / 槽位） |
| [`feasibility/charts.py`](feasibility/charts.py) | 出图 |
| [`deploy/daban_paper/`](deploy/daban_paper/) | **打板模拟盘代码**（已部署到 VPS，每交易日 16:10 自动跑 + 微信推送） |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | **部署纪律**：VPS 代码只能来自仓库 commit，统一用 `deploy_vps.sh` 部署 |
| [`deploy/deploy_vps.sh`](deploy/deploy_vps.sh) | VPS 唯一部署入口：fetch → 校验 commit → 自检 → rsync → 写部署清单 |
| [`docs/03_常规版_量价择时系统_v1.0.md`](docs/03_常规版_量价择时系统_v1.0.md) | **常规版机制**：八条口诀 → 门控/选股/持有/卖出/推送 |
| [`docs/04_常规版_参数手册.md`](docs/04_常规版_参数手册.md) | 常规版 113 个标准参数的默认值、区间、敏感度、口诀映射 |
| [`config/regular_v1.yaml`](config/regular_v1.yaml) | 常规版唯一参数源（62 个可调 / 51 个冻结，3 个风险档） |
| [`config/regular_CHANGELOG.md`](config/regular_CHANGELOG.md) | 常规版参数变更记录与待验证清单 |
| [`deploy/regular_paper/`](deploy/regular_paper/) | 常规版模拟盘代码（日线级，合成数据自检 26 项） |

---

## 复现

```bash
cd feasibility
python3 checks.py                                   # F3–F7
python3 scenarios.py --n-paths 1500 --days 500      # F8（约 2 分钟）
python3 charts.py                                   # 出图
```

依赖：Python 3 + numpy + matplotlib，无网络、无数据源。

---

## 系统一句话

> 先看情绪决定**能不能做**，再看题材决定**做哪个方向**，再看身位决定**买谁**，最后才决定**用什么价位买**。
> 四步各自一层，层与层之间只能"更严"，不能"更松"。

```
全市场 → L0 制度 → L1 状态 → L2 主线 → L3 身位 → L4 引擎 → L7 组合 → 成交
                                                                      ↓
                                            L5 持仓状态 → L6 退出 → L8 归因
```

---

## 模拟盘（已上线）

部署在 VPS `YOUR_VPS_IP`。每交易日 **09:40** 执行开盘买入（有买入才推）、**16:10** 收盘复盘推送 PushPlus。

```bash
ssh vps
cd ~/daban_paper
python3 status.py          # 查账户
python3 run_daily.py       # 手动跑当天
tail -f logs/cron.log      # 看调度日志
```

当前只启用 **E1 接力 + E3 首板** 两个引擎；E2 低吸因缺分时数据主动停用。
周末与节假日自动跳过（按交易日历判定，非简单周一到周五）。

详见 [`deploy/daban_paper/README.md`](deploy/daban_paper/README.md)。

---

## 常规量价模拟盘（已上线）

已部署在 VPS `~/regular_paper`：每交易日 **16:20** 收盘推送、**09:15** 盘前推送；
每份收盘日报内含 **「今日复盘」**（计划执行 / 盈亏 / 推送与数据体检 / 明日关注），
**17:10** 由 `watchdog.py` 检查两次推送是否齐全、缺收盘自动补跑；
每 **5 次收盘推送**再由 `review.py` 做一次参数级复盘（只给建议，不自动改参数）。

同一套门控/选股/持有/卖出/推送骨架，按八条口诀改成不追涨停的常规波段：

> 买横买坑，不买竖；卖在人声鼎沸处；连续小涨要大涨；连续大涨要离场；
> 急跌无量是洗盘；急跌放量立马撤；快速冲高要回落；不挖深坑不大买。

| | 打板盘 | 常规版 |
|---|---|---|
| 票池 | 涨停/炸板/连板 | 全市场流动性前 300 |
| 买点 | 接力/首板/低吸 | 横盘、坑反转、深坑、连续小涨 |
| 持有 | 2–3 日 | 10–20 日，趋势不破不走 |
| 仓位 | 固定权重为主 | 风险预算 ÷ 止损距离 |
| 卖出 | 固定 TP/SL + 加速 | 量价状态 + 移动止盈 |
| 门控 | 情绪 E-score | 指数趋势/宽度/量能温度 |

```bash
cd deploy/regular_paper
python3 selftest.py                 # 26 个合成场景，不联网
python3 run_daily.py --demo --force # 看一份完整日报
python3 run_daily.py                # 真实数据 + 推送
```

参数不是散落在代码里，而是集中在 [`config/regular_v1.yaml`](config/regular_v1.yaml)：
113 个参数全部有 id、单位、允许区间、敏感度、对应口诀；62 个可调、51 个冻结、3 个风险档。
改参数后跑 `python3 gen_param_doc.py` 自动同步参数手册。
每份收盘日报自带每日复盘；每 5 次收盘推送再由 `review.py` 做一次参数级复盘，只给建议、不自动改 YAML；
17:10 `watchdog.py` 做推送/数据体检，正常不打扰、异常才告警。

详见 [`docs/03_常规版_量价择时系统_v1.0.md`](docs/03_常规版_量价择时系统_v1.0.md)。

## 部署纪律

**VPS 上运行的代码，必须来源于 GitHub 仓库的某个 commit。**

统一入口：`deploy/deploy_vps.sh`。它只接受 `origin/main` 上的 commit，
先 `git archive` 到临时目录跑自检，再同步代码，并写
`state/deploy_manifest.json`（commit + 每个文件的 sha256）。

禁止：SSH 到 VPS 直接改 `.py`、`scp` 本地未提交文件、手改部署清单。
`status.py` 与 17:10 的 `watchdog.py` 会校验运行代码与清单是否一致；
发现 `code_drift` 就应从仓库重新部署，而不是在 VPS 上修。
详见 [`DEPLOYMENT.md`](DEPLOYMENT.md)。

## 下一步

**P2 影子盘（3 个月，只记录不下单）**，盯三个数：

| 指标 | 门槛 |
|---|---|
| 单笔毛收益 | **≥ 0.34%**（保本）/ ≥ 0.50%（稳健） |
| 单笔摩擦 | ≤ 0.20% |
| 年交易笔数 | ≤ 100 |

> 只要"单笔毛收益"测出来 < 0.34%，这个系统就不值得继续。
> 这是整个方案里唯一一个能在花大钱之前就拿到答案的关键指标。
