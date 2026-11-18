# -*- coding: utf-8 -*-
"""PushPlus 推送（多 token 容错）。token 不写进 YAML，优先环境变量。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from config import Params

PUSH_API = "https://www.pushplus.plus/send"


def load_tokens(params: Params) -> list[str]:
    import os
    env = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]

    toks = params.rt("push", "tokens", default=[]) or []
    if isinstance(toks, str):
        toks = [x.strip() for x in toks.split(",") if x.strip()]
    if toks:
        return [str(t) for t in toks if str(t).strip()]

    # 复用同机其他模拟盘的 token（同一 PushPlus 账号）
    for cfg in (Path.home() / "price_action_paper" / "config.py",
                Path(__file__).resolve().parent.parent / "daban_paper" / "config.py"):
        if cfg.exists():
            ns: dict = {}
            try:
                exec(compile(cfg.read_text(encoding="utf-8"), str(cfg), "exec"), ns)
                got = ns.get("PUSH_TOKENS") or []
                if got:
                    return list(got)
            except Exception:
                pass
    return []


def push(title: str, content: str, params: Params) -> int:
    if not params.rt("push", "enabled", default=True):
        print("[info] push.enabled=false，跳过推送")
        return 0
    tokens = load_tokens(params)
    if not tokens:
        print("[warn] 未配置 PUSHPLUS_TOKEN / push.tokens，跳过推送")
        return 0

    timeout = float(params.rt("push", "timeout_sec", default=10))
    ok = 0
    for token in tokens:
        try:
            payload = json.dumps({
                "token": token, "title": title,
                "content": content, "template": "markdown",
            }).encode("utf-8")
            req = urllib.request.Request(
                PUSH_API, data=payload,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")
            if '"code":200' in resp:
                ok += 1
            else:
                print(f"[warn] push 返回: {resp[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] push 失败: {e}")
    return ok
