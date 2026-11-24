# -*- coding: utf-8 -*-
"""项目切换：发现 ~/short_v 下的项目，记录当前激活项目。"""
from __future__ import annotations

import json
import os
import re
import shutil
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
        state = d / "00-项目管理/artifact-state.json"
        count = 0
        try:
            count = len(json.loads(state.read_text(encoding="utf-8")).get("artifacts") or [])
        except Exception:
            pass
        out.append({"id": d.name, "name": d.name, "path": str(d),
                    "hasArtifacts": state.exists(),
                    "hasAgents": (d / ".agents/skills").is_dir(),
                    "hasInput": (d / "输入").exists(),
                    "artifactCount": count,
                    "lastModified": int((state if state.exists() else d).stat().st_mtime)})
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


def create(name: str, template: Path = None) -> dict:
    """在 ~/short_v 下新建项目目录骨架，并复制 .agents 技能模板（如可用）。"""
    name = (name or "").strip()
    if not re.fullmatch(r"[\w\u4e00-\u9fff-]{1,40}", name):
        raise ValueError("项目名只能用中文/字母/数字/下划线/短横线，1–40 字")
    target = (ROOT / name).resolve()
    if target.exists():
        raise ValueError("项目已存在")
    dirs = ["00-项目管理", "制作运行", "00-原始资料", "输入",
            "01-概念锚定", "02-角色圣经", "03-世界观", "04-分场大纲",
            "05-正式剧本", "06-剧本压力测试", "07-图像资产",
            "07-图像资产/候选图片", "07-图像资产/资产绑定",
            "08-表演设计", "09-镜头与视频提示词",
            "09-镜头与视频提示词/镜头规划", "09-镜头与视频提示词/视频提示词",
            "09-镜头与视频提示词/生成结果"]
    for d in dirs:
        (target / d).mkdir(parents=True, exist_ok=True)
    tmpl = Path(template) if template else None
    if tmpl is None:
        for cand in discover():
            p = Path(cand["path"])
            if (p / ".agents/skills").is_dir():
                tmpl = p
                break
    copied = False
    if tmpl and (tmpl / ".agents").is_dir() and not (target / ".agents").exists():
        shutil.copytree(tmpl / ".agents", target / ".agents")
        copied = True
    return {"id": name, "name": name, "path": str(target), "created": True,
            "agentsCopied": copied, "dirs": len(dirs)}
