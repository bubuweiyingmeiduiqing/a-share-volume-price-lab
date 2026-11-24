# 常规量价模拟盘（regular_paper）

把打板模拟盘的**门控 → 选股 → 持有 → 卖出 → 推送**骨架，换成一套以
**择时 + 量价**为核心的常规波段系统。

> 已部署在 VPS `~/regular_paper`：16:20 收盘推送、09:15 盘前推送；
> 每份收盘日报自带 **「今日复盘」**（计划执行 / 盈亏 / 推送与数据体检 / 明日关注）；
> 每 5 次收盘推送再由 `review.py` 做一次参数级复盘（只给建议、不自动改 YAML）；
> 17:10 `watchdog.py` 检查两次推送是否都发生，缺收盘会自动补跑，异常才告警。

- 设计文档：[`../../docs/03_常规版_量价择时系统_v1.0.md`](../../docs/03_常规版_量价择时系统_v1.0.md)
- 参数标准表：[`../../config/regular_v1.yaml`](../../config/regular_v1.yaml)
- 参数手册：[`../../docs/04_常规版_参数手册.md`](../../docs/04_常规版_参数手册.md)
- 参数变更记录：[`../../config/regular_CHANGELOG.md`](../../config/regular_CHANGELOG.md)

---

## 它在做什么

```
每个交易日 16:20
  ├─ 拉指数 + 流动性前 300 只个股日线
  ├─ 算市场温度 → COLD / REPAIR / TREND / HOT / CRASH
  ├─ 执行昨日计划（T+1 开盘条件单；一字/涨停开盘买不进）
  ├─ 结算持仓（硬止损 → 放量急跌 → 无量洗盘 → 趋势走坏 → 人声鼎沸
  │            → 连续大涨 → 冲高回落 → 移动止盈 → 时间止损）
  ├─ 生成明日计划（横 / 坑 / 深坑 / 连续小涨）
  └─ 写报告 + PushPlus 推送

每个交易日 09:15
  └─ 盘前推送：今日 gap 区间、止损、目标仓位；无候选推短心跳；
     计划未更新（昨日收盘失败）推警示，不静默跳过
```

**不使用分时数据。** 所有信号基于日线，执行假设是“T 日收盘出计划，T+1 开盘成交”。
“开盘半小时猛拉不封板”用日线长上影代理，报告里明确标注，不假装是分时。

---

## 目录

| 文件 | 作用 |
|---|---|
| `config.py` | YAML 参数加载、profile 覆盖、区间/交叉校验、参数指纹 |
| `data.py` | akshare 数据层 + 日线/指数/快照缓存 |
| `indicators.py` | 纯函数指标：均线、量比、箱体、坑、上影线、涨跌停 |
| `regime.py` | 市场温度、五状态机、迟滞/驻留、洗盘例外 |
| `strategy.py` | 横/坑/深坑/小涨识别、竖否决、评分、次日条件单 |
| `portfolio.py` | 账户、成本、T+1、风险预算、卖出梯度、熔断 |
| `run_daily.py` | 收盘主流程 + Markdown 日报 + 推送 |
| `run_premarket.py` | 盘前 09:15 条件单/持仓提醒 |
| `push.py` | PushPlus 多 token 容错 |
| `review.py` | 推送计数 + 每 5 次收盘推送复盘 + 参数调整建议 |
| `watchdog.py` | 收盘后体检：盘前/收盘推送是否齐全、净值是否缺日；缺收盘自动补跑，异常才推送 |
| `deploy_check.py` | 校验运行代码与 `state/deploy_manifest.json` 是否一致（防止直接改 VPS） |
| `replan.py` | 改参数/规则后只重算明日计划，不结算、不推送、不重复计数 |
| `status.py` | 账户/持仓/逐笔/状态速查 |
| `synthetic.py` | 合成行情，供 `--demo` 和自检使用 |
| `selftest.py` | 26 个合成场景自检（不联网） |
| `gen_param_doc.py` | 从 YAML 生成参数手册 |

---

## 部署（生产）

**不要直接改 VPS 上的文件。** 生产部署只从仓库 commit 走统一脚本：

```bash
ssh vps
cd ~/a-share-volume-price-lab
git pull --ff-only
./deploy/deploy_vps.sh --app regular --commit origin/main
```

脚本会先自检，再同步代码；`state/cache/logs/reports/demo` 不会被覆盖，
并写 `state/deploy_manifest.json`。`status.py` 显示部署 commit，
`watchdog.py` 会告警 `code_drift`。详见 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。

## 安装

```bash
cd deploy/regular_paper
python3 -m pip install -r requirements.txt
```

要求 Python 3.10+。数据源是 akshare（东财/新浪），首次运行需要网络。

---

## 运行

```bash
# 1) 先跑自检（不联网）
python3 selftest.py

# 2) 合成数据演示（不联网、不推送）
python3 run_daily.py --demo --force

# 3) 真实数据：最近交易日 + 推送
python3 run_daily.py

# 常用参数
python3 run_daily.py --no-push
python3 run_daily.py --date 20261106 --force
python3 run_daily.py --profile defensive      # 低风险档
python3 run_daily.py --config /path/to/regular_v1.yaml

python3 run_premarket.py                      # 09:15 盘前
python3 status.py                             # 账户速查
python3 review.py --force                     # 手动补一次复盘（会推送）
python3 review.py --force --no-push           # 只看复盘，不推送
python3 replan.py --date 20261112             # 改规则后重算明日计划（不推送）
```

> 参数文件默认从 `../../config/regular_v1.yaml` 读取；独立部署时把该文件复制到
> `deploy/regular_paper/config/regular_v1.yaml`，或用 `REGULAR_CONFIG` 指定。

---

## 定时任务

```cron
# 收盘：每个交易日 16:20
20 16 * * 1-5 cd /home/ubuntu/regular_paper && \
  flock -n -E 75 ~/.local/state/kai/locks/regular-paper.lock \
  timeout -k 15s 20m python3 run_daily.py >> logs/cron.log 2>&1

# 盘前：每个交易日 09:15
15 9 * * 1-5 cd /home/ubuntu/regular_paper && \
  flock -n -E 75 ~/.local/state/kai/locks/regular-premarket.lock \
  timeout -k 10s 5m python3 run_premarket.py >> logs/premarket.log 2>&1

# 收盘后体检：每个交易日 17:10（正常不推送，异常才告警）
10 17 * * 1-5 cd /home/ubuntu/regular_paper && \
  flock -n -E 75 ~/.local/state/kai/locks/regular-watchdog.lock \
  timeout -k 15s 10m python3 watchdog.py >> logs/watchdog.log 2>&1
```

脚本内部会按交易日历判断非交易日（不是简单周一到周五）；指数直到收盘后用实时行情也补不出当日 K 线，
才会报错并推送。

---

## 推送 token

优先级：

1. 环境变量 `PUSHPLUS_TOKEN`（多个用逗号分隔）
2. YAML `runtime.push.tokens`
3. 复用同机 `~/price_action_paper/config.py` 或 `../daban_paper/config.py` 的 `PUSH_TOKENS`

**不要把 token 写进仓库。** 运行失败会推送 traceback 尾部，不会静默失败。

---

## 每日复盘与推送体检

每份收盘日报的 **「今日复盘」** 段固定回答四件事：

1. 昨日计划执行了多少、哪些没成交/跳过；
2. 今日持仓涨跌与账户盈亏；
3. 盘前/收盘推送、指数日期、净值记录是否完整（缺日会直接标出）；
4. 明日关注什么。

`watchdog.py` 在 17:10 再做一次独立检查：

- 收盘日报缺失且当天未结算 → 自动补跑一次（带 flock，不会与 cron 抢跑）；
- 盘前缺失 → 无法补发，只告警并提示检查 09:15 的 cron 行；
- 正常日不推送；异常按“日期 + 告警码”去重，避免反复打扰；
- 记录在 `state/watchdog.json`，日志 `logs/watchdog.log`。

## 推送计数与参数复盘

- `run_daily.py` 收盘推送成功后，把当天计入 `state/push_counter.json`。
- 每 **5 次收盘推送**，自动调用 `review.py` 生成 `reports/review_YYYY-MM-DD.md` 并推送。
- 09:15 盘前推送只计数、不触发复盘；否则 2.5 天就复盘一次，样本太少。
- 若昨日收盘失败导致 `plan.json` 目标日不是今天，盘前仍会推一条「计划未更新」警示，并计入盘前推送。
- 复盘只给“保持/观察/降档/暂停”的建议，**不自动改参数**；已平仓样本 <30 笔时默认“不调整”。
- 计数按日期去重，`--force` 重跑同一天不会重复计数。
- 想调整周期：改 `config/regular_v1.yaml` 的 `runtime.review.every_n_close_pushes`。

## 改规则后怎么让明天生效

改完参数/规则、确认不重复推送后：

```bash
python3 replan.py --date 20261112
```

它只重新生成 `state/plan.json`，不碰账户、不结算、不推送、不增加推送计数。
写出的报告是 `reports/YYYY-MM-DD-replan.md`。

## 参数怎么改

> **参数也走仓库。** 改本地 `config/regular_v1.yaml` → commit/push → `deploy_vps.sh`。
> 直接在 VPS 改 YAML 会被 `state/deploy_manifest.json` 标成 `code_drift`。

1. 先读 [`docs/04_常规版_参数手册.md`](../../docs/04_常规版_参数手册.md) 的 §0 和 §2。
2. 只改“第一线参数”，一次一个；`tunable: false` 不要动。
3. 改完跑：
   ```bash
   python3 selftest.py
   python3 run_daily.py --demo --force
   python3 gen_param_doc.py     # 同步参数手册
   ```
4. 记录日期、改前/改后、假设、样本、结果。

参数表里 113 个参数、62 个可调，但真正建议动的只有 25 个。
**参数越多，越容易把噪声拟合进去；纪律比参数重要。**

---

## 目录与状态

```
deploy/regular_paper/
  state/        # account.json / regime.json / plan.json
  cache/        # 个股/指数/快照缓存，可安全删除（会重新拉取）
  reports/      # 日报与盘前
  logs/         # cron 日志
  demo/         # --demo 的 state / reports
```

`state/` 和 `cache/` 不应提交到版本库。

---

## 数据源容错（2026-11-12 增加，2026-11-24 补强）

东财接口在收盘后可能断连或日线延迟；现在按以下顺序降级：

1. 指数日线：东财 `index_zh_a_hist` → 东财 K 线 → **新浪 K 线 JSON** → 腾讯 `stock_zh_index_daily_tx` → 缓存
2. 全市场快照：东财 → **旧池前 800 只 + 新浪批量实时行情** → 新浪全市场分页 → 缓存
3. 个股日线：东财 → 新浪 `stock_zh_a_daily` → 缓存
4. 若日线只到昨天：个股用**当日快照**补齐今天 K 线（开高低收/量/额）；
   指数用**收盘后的实时行情**补一根当日 K 线（腾讯 → 新浪 → 东财）
5. 若缓存落后于**上一交易日**，不再走只读快路径，强制补拉历史；补齐后回写缓存。
   否则“旧缓存 + 今天快照”会形成缺口，把 MA5/5 日涨幅/量比全部算错（2026-11-17 修复）
6. 指数实时补齐有三道闸：行情日期必须等于目标交易日、行情时间必须 ≥15:00（盘中现价不能当收盘价）、
   缓存最后一天必须正好是上一交易日（缺口超过一天宁可用昨天收盘，也不拼假 K 线）
7. 网络 socket 默认 20s 超时；单个票的接口挂死不会拖死整轮收盘

> 口径：指数缓存里 `amount` 列存的是**成交量(手)**，不是成交额(元)。
> 腾讯日线、新浪 K 线、实时行情都没有可靠的指数成交额，regime 只比量能比值，比值不受单位影响；
> 补齐当日 K 线时必须沿用同一口径，否则量比会差几个数量级（2026-11-24 修复）。

这样收盘后东财断连、指数日线停在昨天时，仍能跑出当天日报，不会只推一条 traceback
（2026-11-24 16:20 就是这种情形）。

## 已知限制

1. **日线代理“冲高回落”**：抓不到 9:45 前的分时诱多，只能用长上影+放量+收盘回落代理。
2. **行业字段可能缺失**：缺失记为“未分类”，同行业上限不生效。
3. **宽度是扫描池样本**：不是全市场，报告会写 `n=...`。
4. **没有历史回测**：先跑 3 个月影子盘，只记录不下单。
5. **收盘信号按收盘价成交**：卖出价略乐观；实盘会更保守。
6. 本模拟盘不构成投资建议。

---

## 故障排查

```bash
tail -50 logs/cron.log
tail -50 logs/premarket.log
python3 status.py
python3 run_daily.py --date 20261106 --no-push --force
```

数据接口失败时：
- 指数日线失败 → 东财 → 新浪 K 线 → 腾讯 → 缓存 → 收盘后用实时行情补当日 K 线；
- 个股失败 → 保留旧缓存并跳过该票；
- 全市场快照失败 → 退旧缓存（报告会暴露数据日期）；
- 账户/计划损坏 → 备份 `state/` 后重跑，不要手改 JSON 里的持仓。
