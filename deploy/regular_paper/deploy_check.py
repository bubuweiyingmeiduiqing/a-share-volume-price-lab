#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署清单校验。

VPS 上运行的代码必须来自仓库的某个 commit。`deploy/deploy_vps.sh` 在部署时
会写 `state/deploy_manifest.json`，记录 commit 与每个代码文件的 sha256。
这里负责比对“运行目录里的文件”与“部署清单”，发现直接改 VPS 的情况。

用法：
  python3 deploy_check.py /home/ubuntu/regular_paper
  python3 deploy_check.py /home/ubuntu/daban_paper
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_REL = "state/deploy_manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(runtime_dir) -> Path:
    return Path(runtime_dir) / MANIFEST_REL


def check(runtime_dir) -> dict:
    """返回 {present, ok, commit, deployed_at, bad, missing, extra, error}。"""
    rt = Path(runtime_dir)
    p = manifest_path(rt)
    empty = {"present": False, "ok": False, "commit": "", "deployed_at": "",
             "bad": [], "missing": [], "extra": []}
    if not p.exists():
        return empty
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out = dict(empty)
        out.update({"present": True, "error": f"清单无法解析：{e}"})
        return out

    bad: list[str] = []
    missing: list[str] = []
    for name, digest in (m.get("files") or {}).items():
        f = rt / name
        if not f.exists():
            missing.append(name)
        elif sha256_file(f) != str(digest):
            bad.append(name)
    for name, digest in (m.get("config_files") or {}).items():
        f = rt / name
        if not f.exists():
            missing.append(name)
        elif sha256_file(f) != str(digest):
            bad.append(name)

    expected = {Path(x).name for x in (m.get("files") or {})}
    actual = {p.name for p in rt.glob("*.py")}
    extra = sorted(actual - expected)

    return {
        "present": True,
        "ok": not (bad or missing or extra),
        "commit": str(m.get("commit", "")),
        "deployed_at": str(m.get("deployed_at", "")),
        "bad": bad,
        "missing": missing,
        "extra": extra,
    }


def summary(r: dict) -> str:
    if not r.get("present"):
        return "未找到部署清单（未按仓库部署流程）"
    if r.get("error"):
        return r["error"]
    if r.get("ok"):
        return f"commit {r.get('commit', '')[:8]} 与清单一致"
    parts = []
    if r.get("bad"):
        parts.append("被修改：" + "、".join(r["bad"]))
    if r.get("missing"):
        parts.append("缺失：" + "、".join(r["missing"]))
    if r.get("extra"):
        parts.append("多出：" + "、".join(r["extra"]))
    return "；".join(parts) or "不一致"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("runtime_dir")
    a = ap.parse_args()
    res = check(a.runtime_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(summary(res))
    raise SystemExit(0 if res.get("ok") else 1)
