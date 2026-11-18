#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# VPS 部署脚本：只允许从仓库的 commit 部署，不允许直接改 VPS。
#
# 在 VPS 的仓库 clone 里运行：
#   cd ~/a-share-volume-price-lab
#   ./deploy/deploy_vps.sh --app regular --commit origin/main
#   ./deploy/deploy_vps.sh --app daban   --commit origin/main
#   ./deploy/deploy_vps.sh --app all     --commit <sha>
#
# 它做四件事：
#   1. fetch + 校验 commit 在 origin/main 上；
#   2. git archive 到临时目录，先跑 py_compile / 自检；
#   3. 备份运行目录的代码，rsync 代码（保留 state/cache/logs/reports）；
#   4. 写 state/deploy_manifest.json（commit + 每个文件的 sha256），供 watchdog/status 校验。
#
# 运行时数据（state/cache/logs/reports/demo）永远不会被覆盖或删除。

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/a-share-volume-price-lab}"
BRANCH="${BRANCH:-main}"
APP=""
COMMIT="origin/main"
DO_SELFTEST=1
DRY_RUN=0

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
log() { echo "[deploy] $*"; }

usage() {
    cat <<'EOF'
用法：
  deploy_vps.sh --app regular|daban|all [--commit <sha|origin/main>] [--branch main]
                [--repo-dir DIR] [--no-selftest] [--dry-run]

示例：
  ./deploy/deploy_vps.sh --app regular --commit origin/main
  ./deploy/deploy_vps.sh --app all --commit 1a2b3c4
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --app) APP="${2:-}"; shift 2 ;;
        --commit) COMMIT="${2:-}"; shift 2 ;;
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
        --no-selftest) DO_SELFTEST=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[deploy] 未知参数：$1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$APP" ] || { usage; exit 2; }
case "$APP" in regular|daban|all) ;; *) die "--app 只能是 regular/daban/all" ;; esac
[ -d "$REPO_DIR/.git" ] || die "找不到仓库 clone：$REPO_DIR（先 git clone）"

cd "$REPO_DIR"

# 部署源必须是干净的仓库 clone；有未提交改动就拒绝，避免“从工作区部署”。
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    die "仓库 clone 有未提交改动，先 commit/push，再来部署"
fi

log "fetch origin/$BRANCH"
git fetch --prune origin

HASH="$(git rev-parse --verify "${COMMIT}^{commit}")"
git merge-base --is-ancestor "$HASH" "origin/$BRANCH" \
    || die "commit $HASH 不在 origin/$BRANCH 上；先把提交推到仓库"
SUBJECT="$(git log -1 --format=%s "$HASH")"
log "目标 commit：$HASH  $SUBJECT"

STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/deploy-vps.XXXXXX")"
trap 'rm -rf "$STAGE_ROOT"' EXIT

deploy_one() {
    local app="$1" src dst config stage backup
    case "$app" in
        regular)
            src="deploy/regular_paper"
            dst="$HOME/regular_paper"
            config="config"
            ;;
        daban)
            src="deploy/daban_paper"
            dst="$HOME/daban_paper"
            config=""
            ;;
        *) die "未知 app：$app" ;;
    esac

    stage="$STAGE_ROOT/$app"
    mkdir -p "$stage"

    local paths=("$src")
    [ -n "$config" ] && paths+=("$config")
    log "[$app] git archive $HASH"
    git archive --format=tar "$HASH" "${paths[@]}" | tar -x -C "$stage"
    [ -f "$stage/$src/run_daily.py" ] || die "[$app] 归档里没有 $src/run_daily.py"

    log "[$app] py_compile"
    ( cd "$stage/$src" && python3 -m py_compile ./*.py )

    if [ "$DO_SELFTEST" = 1 ] && [ -f "$stage/$src/selftest.py" ]; then
        log "[$app] 自检（临时目录，不碰生产 state）"
        ( cd "$stage/$src" && python3 selftest.py )
    fi

    if [ "$DRY_RUN" = 1 ]; then
        log "[$app] dry-run：会部署 $HASH 到 $dst"
        return 0
    fi

    # 备份运行目录代码（不含 state/cache/logs/reports/demo）
    mkdir -p "$HOME/deploy-backups"
    backup="$HOME/deploy-backups/${app}_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$backup"
    if [ -d "$dst" ]; then
        rsync -a \
            --exclude 'state/' --exclude 'cache/' --exclude 'logs/' \
            --exclude 'reports/' --exclude 'demo/' \
            "$dst/" "$backup/"
        log "[$app] 已备份代码到 $backup"
    fi

    # 代码同步：保留运行时目录；删除仓库里已经不存在的 .py
    log "[$app] rsync 代码到 $dst"
    rsync -a --delete \
        --exclude 'state/' --exclude 'cache/' --exclude 'logs/' \
        --exclude 'reports/' --exclude 'demo/' --exclude 'config/' \
        --exclude '__pycache__/' --exclude '*.pyc' \
        --exclude '*.bak.*' --exclude 'state.bak.*' --exclude 'config.bak.*' \
        "$stage/$src/" "$dst/"

    if [ -n "$config" ]; then
        mkdir -p "$dst/config"
        rsync -a --exclude '*.bak.*' "$stage/$config/" "$dst/config/"
    fi

    # 同步后做 import 检查；失败自动回滚代码
    log "[$app] import 检查"
    local import_check="import run_daily"
    [ "$app" = "regular" ] && import_check="import run_daily, deploy_check"
    if ! ( cd "$dst" && python3 -c "$import_check" ); then
        log "[$app] import 失败，回滚"
        if [ -d "$backup" ] && [ -n "$(ls -A "$backup" 2>/dev/null)" ]; then
            rsync -a --delete \
                --exclude 'state/' --exclude 'cache/' --exclude 'logs/' \
                --exclude 'reports/' --exclude 'demo/' \
                "$backup/" "$dst/"
            log "[$app] 已从 $backup 回滚"
        else
            log "[$app] 没有可用的代码备份；请人工检查 $dst"
        fi
        die "[$app] 部署失败"
    fi

    # 部署清单：commit + 每个文件 sha256，供 watchdog/status 校验
    python3 - "$dst" "$HASH" "$SUBJECT" "$app" "origin/$BRANCH" <<'PY'
import datetime
import hashlib
import json
import socket
import sys
from pathlib import Path

dst = Path(sys.argv[1])
commit, subject, app, branch = sys.argv[2:6]

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

files = {p.name: sha(p) for p in sorted(dst.glob("*.py"))}
config_files = {}
for rel in ("config/regular_v1.yaml", "config/regular_CHANGELOG.md"):
    p = dst / rel
    if p.exists():
        config_files[rel] = sha(p)

manifest = {
    "app": app,
    "commit": commit,
    "commit_subject": subject,
    "branch": branch,
    "deployed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "host": socket.gethostname(),
    "files": files,
    "config_files": config_files,
}
(dst / "state").mkdir(parents=True, exist_ok=True)
(dst / "state" / "deploy_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[deploy] 清单：{len(files)} 个 .py + {len(config_files)} 个 config")
PY

    log "[$app] 完成：$HASH -> $dst"
}

case "$APP" in
    all) deploy_one regular; deploy_one daban ;;
    *) deploy_one "$APP" ;;
esac

log "全部完成。状态检查：python3 ~/regular_paper/status.py"
