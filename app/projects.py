# -*- coding: utf-8 -*-
"""项目切换：发现 ~/short_v 下的项目，记录当前激活项目。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path.home() / "short_v"
DEFAULT = ROOT / "有谁共鸣2"
ACTIVE = Path(__file__).resolve().parent / "run" / "active_project.json"


def discover() -> list:
    out = []
    if not ROOT.is_dir():
        return out
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not ((d / ".agents").exists() or (d / "00-项目管理").exists() or (d / "输入").exists()):
            continue
        out.append({"id": d.name, "name": d.name, "path": str(d),
                    "hasArtifacts": (d / "00-项目管理/artifact-state.json").exists(),
                    "hasInput": (d / "输入").exists()})
    return out


def active_path() -> Path:
    env = os.environ.get("WORKBENCH_PROJECT")
    if env:
        return Path(env).expanduser().resolve()
    try:
        p = Path(json.loads(ACTIVE.read_text(encoding="utf-8"))["path"]).expanduser().resolve()
        if p.is_dir():
            return p
    except Exception:
        pass
    return DEFAULT


def set_active(path) -> Path:
    raw = str(path)
    if not Path(raw).expanduser().exists():
        hit = next((x for x in discover() if x["id"] == raw or x["name"] == raw), None)
        if hit:
            raw = hit["path"]
    p = Path(raw).expanduser().resolve()
    names = {x["path"] for x in discover()}
    if str(p) not in names:
        raise ValueError("未登记的项目")
    if not os.environ.get("WORKBENCH_PROJECT"):
        ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ACTIVE.with_name(ACTIVE.name + ".tmp")
        tmp.write_text(json.dumps({"path": str(p)}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ACTIVE)
    return p
