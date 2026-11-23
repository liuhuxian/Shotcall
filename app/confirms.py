# -*- coding: utf-8 -*-
"""导演台的"本地确认"：对登记表之外的成果也给真实状态。

状态存在项目 制作运行/确认状态.json，按文件路径记录：
  {path: {status: confirmed|rejected, sha256, at, note}}
文件被改动后 sha 不匹配 → 自动视为"已失效"，需要重新确认。
"""
import hashlib
import json
import time
from pathlib import Path

FILE = "制作运行/确认状态.json"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(project: Path) -> dict:
    f = project / FILE
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(project: Path, data: dict) -> None:
    f = project / FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def record(project: Path, rel: str, verdict: str, note: str = "") -> dict:
    """verdict: confirm | reject"""
    f = project / rel
    if not f.exists():
        raise ValueError("文件不存在")
    data = _load(project)
    rec = {"status": "confirmed" if verdict == "confirm" else "rejected",
           "sha256": _sha(f), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "note": note}
    data[rel] = rec
    _save(project, data)
    return rec


def state_of(project: Path, rel: str) -> dict:
    """返回 {local: confirmed|rejected|stale|none, at, note}"""
    rec = _load(project).get(rel)
    if not rec:
        return {"local": "none"}
    f = project / rel
    if not f.exists():
        return {"local": "stale", "at": rec.get("at"), "note": rec.get("note"),
                "reason": "文件已不存在"}
    if _sha(f) != rec.get("sha256"):
        return {"local": "stale", "at": rec.get("at"), "note": rec.get("note"),
                "reason": "确认后文件被改动"}
    return {"local": rec["status"], "at": rec.get("at"), "note": rec.get("note")}


def all_states(project: Path) -> dict:
    return {k: state_of(project, k) for k in _load(project)}
