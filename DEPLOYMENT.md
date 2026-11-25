# 部署纪律

> **原则：VPS 上运行的代码，必须来源于 Git 仓库的某个 commit。**
>
> 不允许 SSH 到 VPS 直接改 `.py`；不允许 `scp`/`rsync` 本地未提交文件；
> 不允许在 VPS 上手改 `state/deploy_manifest.json` 来“骗过”校验。

仓库：<https://github.com/bubuweiyingmeiduiqing/a-share-volume-price-lab>

---

## 1. 目录约定

| 位置 | 作用 |
|---|---|
| `deploy/regular_paper/` | 常规量价模拟盘源码（仓库内） |
| `deploy/daban_paper/` | 打板模拟盘源码（仓库内） |
| `deploy/deploy_vps.sh` | 唯一部署入口（在 VPS 的仓库 clone 里运行） |
| `~/a-share-volume-price-lab/` | VPS 上的仓库 clone，只做部署源，不跑业务 |
| `~/regular_paper/` | 常规版运行目录（代码 + state/cache/logs/reports） |
| `~/daban_paper/` | 打板版运行目录（代码 + state/cache/logs/reports） |
| `~/regular_paper/state/deploy_manifest.json` | 最近一次部署的 commit 与文件 sha256 |
| `~/deploy-backups/` | 每次部署前的代码备份 |

**运行时数据不进入仓库，也不会被部署覆盖：**

```
state/  cache/  logs/  reports/  demo/  config/（部署时只更新 YAML/CHANGELOG）
```

> **参数/配置也属于部署清单。** 改 `config/regular_v1.yaml` 同样要
> 本地 commit → push → `deploy_vps.sh`；直接在 VPS 改 YAML 会被标成 `code_drift`。

---

## 2. 标准流程

任何改动，无论大小，都走同一条链：

```bash
# 1) 本地改代码 / 文档
cd <本地仓库>
python3 deploy/regular_paper/selftest.py      # 至少跑自检

# 2) 提交并推到 main
git add -A
git commit -m "..."
git push origin main

# 3) VPS 侧：默认不用管
#    每交易日 08:45 / 15:45，cron 跑 ~/a-share-volume-price-lab/deploy/sync_from_github.sh，
#    自动 git fetch → 比对运行目录清单 → 落后才部署（自检 + 备份 + 回滚 + 写清单）。
#
# 需要立刻生效时（例如盘中要修东西），在 VPS 的仓库 clone 里手动跑同一个脚本：
ssh vps
cd ~/a-share-volume-price-lab
./deploy/sync_from_github.sh                 # 两个运行目录都对齐 origin/main
./deploy/sync_from_github.sh --app regular   # 只同步常规版
./deploy/sync_from_github.sh --dry-run       # 只报告要做什么
```

> `sync_from_github.sh` 是幂等的：运行目录清单里的 commit 已经等于 `origin/main`
> 就只打一行日志直接退出，不会重复 rsync、不会重复自检。
> 底层仍然是 `deploy_vps.sh`，所以“只允许 origin/main 上的 commit”这条约束不变。

`deploy_vps.sh` 会：

1. `git fetch` 并校验 commit **在 `origin/main` 上**；
2. `git archive` 到临时目录，先 `py_compile`，再跑 `selftest.py`；
3. 备份运行目录代码；
4. `rsync` 代码到运行目录，保留 `state/cache/logs/reports/demo`；
5. 做 import 检查，失败自动回滚；
6. 写 `state/deploy_manifest.json`（commit + 每个 `.py` / 配置的 sha256）。

> 部署脚本本身也在仓库里。要改部署逻辑，先 commit/push，再在 VPS clone 里
> `git pull --ff-only` 后运行新脚本。**不要直接在 VPS 上改 `deploy_vps.sh`。**

---

## 3. 第一次部署（bootstrap）

如果 VPS 上还没有仓库 clone：

```bash
ssh vps
git clone https://github.com/bubuweiyingmeiduiqing/a-share-volume-price-lab.git \
  ~/a-share-volume-price-lab
cd ~/a-share-volume-price-lab
git checkout main
./deploy/deploy_vps.sh --app all --commit origin/main
```

如果 `~/regular_paper` 已有运行数据和代码，脚本会先备份再覆盖代码；
`state/cache/logs/reports/demo` 不动。

---

## 4. 回滚

回滚 = 部署旧 commit，不是手改文件：

```bash
cd ~/a-share-volume-price-lab
./deploy/deploy_vps.sh --app regular --commit <old-sha>
```

部署脚本每次都会在 `~/deploy-backups/<app>_<timestamp>/` 留一份代码备份，
但它只用于自动回滚；人工回滚优先用旧 commit，保证清单可追溯。

---

## 5. 完整性校验

部署清单写入后，以下位置会校验“运行代码 = 清单”：

- `python3 ~/regular_paper/status.py`：打印部署 commit 与清单状态；
- `python3 ~/regular_paper/deploy_check.py ~/daban_paper`：单独校验打板版；
- `python3 ~/regular_paper/watchdog.py`：17:10 体检时若发现
  `code_drift` / `no_manifest`，会推送告警；
- 清单缺失、文件被改、出现清单外的 `.py`，都算不一致。

发现不一致时，正确做法是：

```bash
cd ~/a-share-volume-price-lab
./deploy/deploy_vps.sh --app regular --commit origin/main
```

**不要**直接改 VPS 文件，也不要手工编辑 `deploy_manifest.json`。

---

## 5.1 自动同步（2026-11-25 起）

VPS 不再依赖“某人记得去 ssh 部署”，而是一个幂等的同步脚本 + 两条 cron：

```bash
# crontab -l | grep deploy-sync
45 8  * * 1-5  cd ~/a-share-volume-price-lab && flock -n ~/.local/state/kai/locks/deploy-sync.lock \
               timeout -k 15s 20m bash deploy/sync_from_github.sh >> ~/deploy-sync.log 2>&1
45 15 * * 1-5  （同上）
```

- 08:45 保证 09:15 盘前跑的是 GitHub 最新代码；15:45 保证 16:10 / 16:20 收盘跑的是同一份；
- 同步失败（网络、自检不过、import 检查失败）只写日志并保留旧代码，不会半途覆盖；
- `deploy_vps.sh` 里的 `selftest.py` 是硬门槛：自检不过就不会 rsync；
- 运行目录的清单 commit 与 `origin/main` 不一致时，`status.py` / `watchdog.py` 会暴露出来。

要“冻结”某个版本，临时 `crontab -e` 注释掉这两行，或把 clone 切到旧 commit 并暂停同步；
回滚仍按第 4 节部署旧 commit。

---

## 6. 为什么这样做

- 线上问题能精确定位到 commit，而不是“某次 ssh 改的那一行”；
- 回滚可复现；
- 部署前自检 + 部署后 import 检查 + 运行期清单校验，三层拦截；
- 运行数据与代码分离，重新部署不会丢账户、缓存、日志和报告。

---

## 7. 当前部署

- 仓库 `main` 分支为唯一来源；
- 常规版：`~/regular_paper`，由 `deploy_vps.sh --app regular` 部署；
- 打板版：`~/daban_paper`，由 `deploy_vps.sh --app daban` 部署；
- 定时任务仍由 VPS crontab 管理（09:15 盘前 / 16:20 收盘 / 17:10 体检），
  部署脚本不改 crontab。
- 代码同步：每交易日 08:45 / 15:45 由 `deploy/sync_from_github.sh` 从 GitHub 拉取
  （`~/deploy-sync.log`），日志里能看到每次的 `清单 xxx → yyy` 与跳过原因。
