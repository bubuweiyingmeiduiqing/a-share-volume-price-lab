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

# 3) 在 VPS 的仓库 clone 里部署指定 commit
ssh vps
cd ~/a-share-volume-price-lab
git pull --ff-only
./deploy/deploy_vps.sh --app regular --commit origin/main
# 打板版：
./deploy/deploy_vps.sh --app daban --commit origin/main
# 或两个一起：
./deploy/deploy_vps.sh --app all --commit origin/main
```

部署脚本会：

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
