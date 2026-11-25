# -*- coding: utf-8 -*-
"""Codex 订阅通道：用本机 Codex 的 ChatGPT 登录态出图，不花 API 费用。

依赖：
  ~/.codex/auth.json 的 tokens.access_token / account_id（codex login 产生）
  一个能访问 chatgpt.com 的代理（默认 socks5h://127.0.0.1:1080）

协议：POST https://chatgpt.com/backend-api/codex/responses（SSE），
      带 tools=[{"type":"image_generation"}]，结果在 image_generation_call.result（base64）。
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

AUTH = Path(os.path.expanduser("~/.codex/auth.json"))
API = "https://chatgpt.com/backend-api/codex/responses"
PROXY = os.environ.get("CODEX_PROXY", "socks5h://127.0.0.1:1080")
MODEL = os.environ.get("CODEX_IMG_MODEL", "gpt-6.1-sol")


def auth() -> dict:
    try:
        d = json.loads(AUTH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    t = d.get("tokens") or {}
    return {"token": t.get("access_token") or "", "account": t.get("account_id") or "",
            "mode": d.get("auth_mode") or ""}


def status() -> dict:
    a = auth()
    ok = bool(a.get("token"))
    return {"configured": ok, "mode": a.get("mode"), "model": MODEL, "proxy": PROXY,
            "account": (a.get("account") or "")[:8],
            "reason": "" if ok else "未找到 ~/.codex/auth.json 的 ChatGPT 登录态（先运行 codex login）"}


def generate(prompt: str, size: str = "1024x1024", quality: str = "high",
             timeout: int = 420, images: list = None) -> tuple:
    """返回 (png 字节, 元信息)；失败抛异常。"""
    a = auth()
    if not a.get("token"):
        raise RuntimeError("Codex 未登录：找不到 ~/.codex/auth.json")
    imgs = list(images or [])
    text = prompt
    if imgs:
        text = ("参考图按顺序为 <Picture 1>…<Picture %d>，请按提示词中的编号对应。\n\n" % len(imgs)) + prompt
    content = [{"type": "input_text", "text": text}]
    for raw in imgs[:4]:
        content.append({"type": "input_image",
                        "image_url": "data:image/png;base64," + base64.b64encode(raw).decode()})
    body = {"model": MODEL, "store": False, "stream": True,
            "tools": [{"type": "image_generation", "size": size, "quality": quality}],
            "input": [{"type": "message", "role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {a['token']}", "Content-Type": "application/json",
               "originator": "codex_cli_rs", "Accept": "text/event-stream"}
    if a.get("account"):
        headers["chatgpt-account-id"] = a["account"]
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    t0 = time.time()
    r = requests.post(API, headers=headers, json=body, stream=True,
                      timeout=(15, timeout), proxies=proxies)
    if r.status_code != 200:
        raise RuntimeError(f"Codex 返回 {r.status_code}：{r.text[:200]}")
    meta = {}
    for raw in r.iter_lines(decode_unicode=False):
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        item = ev.get("item")
        if ev.get("type") == "response.image_generation_call.completed":
            item = ev.get("item") or item
        if not item or item.get("type") != "image_generation_call":
            continue
        if not item.get("result"):
            continue
        data = base64.b64decode(item["result"])
        meta = {"size": item.get("size"), "quality": item.get("quality"),
                "revised": (item.get("revised_prompt") or "")[:200],
                "seconds": round(time.time() - t0, 1), "bytes": len(data)}
        return data, meta
    raise RuntimeError("Codex 没有返回图片（可能被限流或提示词被拒）")
