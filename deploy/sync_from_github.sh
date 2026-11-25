#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# 从 GitHub 同步代码到 VPS 运行目录。**这是 VPS 上唯一允许的代码来源。**
#
# 在 VPS 的仓库 clone 里运行（cron 也用它）：
#   cd ~/a-share-volume-price-lab
#   ./deploy/sync_from_github.sh                    # 同步 regular + daban
#   ./deploy/sync_from_github.sh --app regular      # 只同步常规版
#   ./deploy/sync_from_github.sh --dry-run          # 只报告要做什么，不动文件
#
# 逻辑：
#   1. git fetch + 校验 clone 工作区干净（clone 只做部署源，不能有本地改动）；
#   2. 运行目录清单里的 commit 已经等于 origin/main → 直接退出（幂等，
#      cron 反复跑、没新提交时不会重复 rsync 和自检）；
#   3. 落后 → 调 deploy_vps.sh --commit origin/main：py_compile + 自检 + 备份 +
#      rsync + import 检查 + 失败回滚 + 写 state/deploy_manifest.json。
#
# 日志：cron 里追加到 ~/deploy-sync.log

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/a-share-volume-price-lab}"
BRANCH="${BRANCH:-main}"
APP="all"
DRY_RUN=0

die() { echo "[sync] ERROR: $*" >&2; exit 1; }
log() { echo "[sync] $(date '+%F %T') $*"; }

usage() {
    cat <<'EOF'
用法：
  sync_from_github.sh [--app regular|daban|all] [--branch main] [--repo-dir DIR] [--dry-run]

示例：
  ./deploy/sync_from_github.sh                # 两个运行目录都跟 origin/main 对齐
  ./deploy/sync_from_github.sh --app regular   # 只同步 ~/regular_paper
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --app) APP="${2:-}"; shift 2 ;;
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[sync] 未知参数：$1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$APP" ] || { usage; exit 2; }
case "$APP" in regular|daban|all) ;; *) die "--app 只能是 regular/daban/all" ;; esac
[ -d "$REPO_DIR/.git" ] || die "找不到仓库 clone：$REPO_DIR（先 git clone）"

cd "$REPO_DIR"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    die "仓库 clone 有未提交改动，先处理：cd $REPO_DIR && git status"
fi

log "fetch origin/$BRANCH"
git fetch --prune origin >/dev/null 2>&1 || die "git fetch 失败（网络/DNS？）"
HASH="$(git rev-parse "origin/$BRANCH")"
[ -n "$HASH" ] || die "拿不到 origin/$BRANCH"
SUBJECT="$(git log -1 --format=%s "$HASH" | cut -c1-48)"
log "目标 commit：${HASH:0:7} $SUBJECT"

manifest_commit() {
    local dst="$1"
    [ -f "$dst/state/deploy_manifest.json" ] || return 1
    python3 - "$dst/state/deploy_manifest.json" <<'PY'
import json
import sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("commit", ""))
except Exception:
    print("")
PY
}

need=()
for app in regular daban; do
    case "$APP" in all|"$app") ;; *) continue ;; esac
    dst="$HOME/${app}_paper"
    cur="$(manifest_commit "$dst" 2>/dev/null || true)"
    if [ "$cur" = "$HASH" ]; then
        log "[$app] 清单已是 $HASH，跳过"
    else
        log "[$app] 清单 ${cur:-（无）} → ${HASH:0:7}，需要同步"
        need+=("$app")
    fi
done

if [ ${#need[@]} -eq 0 ]; then
    log "全部已同步，无需操作"
    exit 0
fi

extra=()
[ "$DRY_RUN" = 1 ] && extra+=(--dry-run)

fail=0
for app in "${need[@]}"; do
    log "[$app] 调 deploy_vps.sh --app $app --commit origin/$BRANCH"
    if ! ./deploy/deploy_vps.sh --app "$app" --commit "origin/$BRANCH" ${extra[@]+"${extra[@]}"}; then
        log "[$app] 同步失败（deploy_vps.sh 已尝试自动回滚）；继续处理下一个 app"
        fail=1
    fi
done

if [ "$fail" = 0 ]; then
    log "同步完成：${HASH:0:7}"
else
    log "同步有失败项，见上面的 deploy 日志"
fi
exit "$fail"
