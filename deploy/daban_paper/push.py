# -*- coding: utf-8 -*-
"""PushPlus 推送（多 token 容错）。"""

import json
import urllib.request

from config import PUSH_API, load_push_tokens


def push(title: str, content: str) -> int:
    tokens = load_push_tokens()
    if not tokens:
        print("[warn] 未配置 PUSHPLUS token")
        return 0
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
            resp = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
            if '"code":200' in resp:
                ok += 1
            else:
                print(f"[warn] push 返回: {resp[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] push 失败: {e}")
    return ok
