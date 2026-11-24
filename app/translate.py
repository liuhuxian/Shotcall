# -*- coding: utf-8 -*-
"""用 DeepSeek 把英文提示词翻成中文审核稿。

key 从 DSH 凭据读取（~/.dsh/.credentials.yaml 的 DEEPSEEK_API_KEY），
不在仓库里保存任何密钥。
"""
import json
import re
import urllib.request
from pathlib import Path

CRED = Path.home() / ".dsh" / ".credentials.yaml"
API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"

SYSTEM = """你是影视分镜提示词的译者。把用户给的英文视频生成提示词完整译成中文，要求：
1. 保留所有结构标签原样：subject_definitions / summary / retention_analysis / detailed_description /
   overall_soundscape / non_diegetic_music、[Shot N]、<Subject N>、<Picture N>、<d>…</d>、<Audio N>。
2. 对白标签内部的中文保持原样，不要改写台词。
3. 技术参数（毫米、秒、fps、分辨率、16:9）保留数字。
4. 忠实直译，不增删内容，不做润色，不加解释。
5. 只输出译文正文，不要任何前后缀、不要代码围栏。"""


def _key() -> str:
    try:
        t = CRED.read_text(encoding="utf-8")
        m = re.search(r"^\s*DEEPSEEK_API_KEY:\s*(\S+)", t, re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def available() -> dict:
    k = _key()
    return {"ok": bool(k), "model": MODEL,
            "reason": "" if k else "没有找到 DEEPSEEK_API_KEY"}


def translate(text: str, timeout: int = 300) -> str:
    key = _key()
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY（DSH 凭据里没有）")
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text}],
        "temperature": 0.2,
        "stream": False,
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    out = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not out.strip():
        raise RuntimeError("接口没有返回译文")
    return out.strip()
