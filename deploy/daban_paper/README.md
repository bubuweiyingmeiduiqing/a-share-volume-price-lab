# 打板模拟盘（daban_paper）

涨停接力策略的**日线级模拟盘**，每个交易日收盘后自动跑一次，结果通过 PushPlus 推送到微信。

- 策略设计：`../../docs/01_系统设计_v0.1.md`
- 可行性依据：`../../docs/02_可行性验证报告.md`
- 部署机器：VPS `YOUR_VPS_IP`（Ubuntu 24.04，CST）

---

## 它在做什么

```
每个交易日 09:40
  ├─ 读昨晚的计划 + 昨收状态
  ├─ 拉实时今开（腾讯行情，批量）
  ├─ 判定触发：开盘涨幅区间 / 一字开盘买不进
  ├─ 有买入 → 记录仓位 + PushPlus 推送「买入」
  └─ 无买入 → 不推送

每个交易日 16:10
  ├─ 拉当日涨停池/炸板池/跌停池（东财）
  ├─ 算 E 情绪分数 → 市场状态（冰点/修复/主升/分歧/退潮）
  ├─ 结算持仓（止损/止盈/破MA5/到期）
  ├─ 生成明日计划（条件单：开盘涨幅区间 + 引擎）
  └─ 写报告 + PushPlus 推送「收盘复盘」
```

> 买入执行放在 09:40 而不是收盘回放，是为了让人**当天就知道买了什么**。
> 若 09:40 任务没跑成（宕机/漏跑），16:10 会用日线开盘价**兜底补做**，保证账本连续。

**没有分时数据依赖**：全部基于日线。买入假设为「次日开盘竞价成交」，这是设计文档里
优先级最高的执行路径（无成交假设问题），代价是放弃了盘中的打板价。

---

## 目录

| 文件 | 作用 |
|---|---|
| `config.py` | 全部参数（仓位/引擎/熔断/成本/推送） |
| `data.py` | akshare 数据层 + 本地缓存 |
| `regime.py` | E-score 与五状态机（含迟滞、最小驻留、平滑） |
| `strategy.py` | 主线判定、身位排序、E1/E3 计划生成 |
| `portfolio.py` | 账户、成本、T+1、熔断、逐笔账本 |
| `run_entry.py` | **09:40 盘中入场**（cron），仅在有买入时推送 |
| `run_daily.py` | 16:10 收盘复盘（cron） |
| `push.py` | PushPlus 推送 |
| `status.py` | 账户速查 |

---

## 部署（生产）

**不要直接改 VPS 上的文件。** 统一从仓库 commit 部署：

```bash
ssh vps
cd ~/a-share-volume-price-lab
git pull --ff-only
./deploy/deploy_vps.sh --app daban --commit origin/main
```

脚本会保留 `state/cache/logs/reports`，并写 `state/deploy_manifest.json`。
详见 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。

## 运行

```bash
cd ~/daban_paper
python3 run_entry.py                 # 09:40 入场（有买入才推）
python3 run_daily.py                 # 16:10 收盘复盘 + 推送
python3 run_daily.py --no-push       # 不推送
python3 run_daily.py --date 20261103 --force   # 指定日重跑
python3 status.py                    # 看账户
```

cron（已装）：

```
40 9 * * 1-5 cd /home/ubuntu/daban_paper && \
  flock -n -E 75 ~/.local/state/kai/locks/daban-entry.lock \
  timeout -k 15s 10m python3 run_entry.py >> ~/daban_paper/logs/cron_entry.log 2>&1

10 16 * * 1-5 cd /home/ubuntu/daban_paper && \
  flock -n -E 75 ~/.local/state/kai/locks/daban-paper.lock \
  timeout -k 15s 20m python3 run_daily.py >> ~/daban_paper/logs/cron.log 2>&1
```

---

## 规则摘要

| 层 | 规则 |
|---|---|
| L0 | 剔除 ST / 北交所 / 无连板数据；涨停价按板块 10%/20%/5% 计 |
| L1 | E = 6 因子滚动分位等权（涨停数/晋级率/封板率/最高板/昨涨停今涨幅/动量），EMA 0.5 平滑 |
| L1 | 状态：冰点 0% 仓｜修复 50%（仅 E3）｜主升 100%（E1+E3）｜分歧 50%（空仓）｜退潮 0% |
| L2 | 主线 = 今日 ≥2 只涨停 且 热度分位 ≥0.70 且 10 日累计靠前 |
| L3 | 身位 = 连板数 ↑ / 首封时间 ↑ / 封单额 ↓，只买前 2 名；最高板 ≥6 禁买中位 |
| L4 | E1：昨 ≥2 板、题材内前 2、非一字、封单 ≥0.2% 流通市值、非尾盘回封 → 次日开盘 +2.5%~+8% 买 |
| L4 | E3：昨首板、≤10 元、位置 ≤60% 分位 → 次日开盘 +2%~+6% 买 |
| L5/L6 | 硬止损 −5%/−4% → 加速态不设固定止盈（高开 +5% 卖半仓）→ 破 MA5 → 到期 3/2 日 |
| L7 | 3 槽位、单票 25%~34%、同题材 ≤2、日亏 −3% 禁开仓、回撤 8% 减半、12% 停 5 日 |

---

## 已知限制

1. **E2 低吸引擎未启用**——它依赖分时「30 分钟不创新低 + 站上均价」，日线无法复现。
   用日线近似等于未来函数，宁可不做。
2. **情绪历史只有约 15 天**——东财池接口只保留最近 ~30 个交易日，首次回补拿不满 60 天。
   随着每日运行自然累积，分位数会逐步稳定。前 1~2 个月的状态判定偏噪声。
3. **退潮期不交易**，可能出现连续多日空仓，这是设计行为而非故障。
4. **成交假设**为次日开盘价 ±0.1% 滑点，未模拟冲击成本与排队；
   一字开盘判为买不进（真实约束）。
5. 本模拟盘不构成投资建议。

---

## 故障排查

```bash
tail -50 ~/daban_paper/logs/cron.log      # cron 日志
python3 status.py                          # 账户状态
ls ~/daban_paper/reports/                  # 历史日报
```

数据接口失败时脚本会推送一条报错消息（含 traceback 尾部），不会静默失败。
缓存位于 `cache/`，可安全删除（会重新拉取，首次约 80 秒）。
