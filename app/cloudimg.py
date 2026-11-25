# -*- coding: utf-8 -*-
"""云端出图渠道：OpenAI 兼容的 /images/generations 接口。

配置来源（按优先级）：
  环境变量  CLOUD_IMG_BASE / CLOUD_IMG_KEY / CLOUD_IMG_MODEL
  配置文件  app/cloud-image.json  {"base": "...", "key": "...", "model": "..."}

不配置就不启用；本模块不写项目文件，产物由调用方决定落点。
"""
import base64
import json
import os
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "cloud-image.json"


def config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    base = os.environ.get("CLOUD_IMG_BASE") or cfg.get("base") or ""
    key = os.environ.get("CLOUD_IMG_KEY") or cfg.get("key") or ""
    model = os.environ.get("CLOUD_IMG_MODEL") or cfg.get("model") or ""
    return {"base": base.rstrip("/"), "key": key, "model": model,
            "configured": bool(base and key and model),
            "source": "env" if os.environ.get("CLOUD_IMG_KEY") else ("file" if cfg else "none")}


def save_config(base: str, key: str, model: str) -> dict:
    """写入 app/cloud-image.json；key 传空表示保留原值。"""
    cur = {}
    if CONFIG_FILE.exists():
        try:
            cur = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    data = {"base": (base or cur.get("base") or "").strip().rstrip("/"),
            "key": (key or cur.get("key") or "").strip(),
            "model": (model or cur.get("model") or "").strip()}
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass
    return status()


def check(timeout: int = 15) -> dict:
    """用 GET {base}/models 验证 base 与 key 是否可用（不产生出图费用）。"""
    c = config()
    if not c["configured"]:
        return {"ok": False, "error": status()["reason"] or "未配置"}
    req = urllib.request.Request(f"{c['base']}/models",
                                 headers={"Authorization": f"Bearer {c['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4000).decode("utf-8", "ignore")
        ids = []
        try:
            data = json.loads(raw).get("data") or []
            ids = [x.get("id") for x in data if isinstance(x, dict)][:40]
        except Exception:
            pass
        has_model = (c["model"] in ids) if ids else None
        return {"ok": True, "models": ids, "modelFound": has_model}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def status() -> dict:
    c = config()
    reason = ""
    if not c["configured"]:
        if not c["base"]:
            reason = "未配置 base（如 https://api.openai.com/v1）"
        elif not c["key"]:
            reason = "未配置 key"
        else:
            reason = "未配置 model"
    key = c["key"]
    masked = (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")
    return {"configured": c["configured"], "base": c["base"], "model": c["model"], "keyMask": masked,
            "source": c["source"], "reason": reason,
            "hint": "Codex 没有出图 API（~/.codex 只有 ChatGPT 登录态）。"
                    "要接云端出图，填 app/cloud-image.json：{base, key, model}"}


def generate(prompt: str, size: str = "1024x1024", n: int = 1, timeout: int = 300) -> list:
    """返回图片字节列表；失败抛异常。"""
    c = config()
    if not c["configured"]:
        raise RuntimeError("云端出图未配置：" + (status()["reason"] or "缺少 base/key/model"))
    payload = {"model": c["model"], "prompt": prompt, "n": n, "size": size}
    req = urllib.request.Request(f"{c['base']}/images/generations",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {c['key']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    out = []
    for item in data.get("data", []):
        if item.get("b64_json"):
            out.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            with urllib.request.urlopen(item["url"], timeout=timeout) as r2:
                out.append(r2.read())
    if not out:
        raise RuntimeError("接口没有返回图片")
    return out
