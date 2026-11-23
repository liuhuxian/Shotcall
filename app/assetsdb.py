# -*- coding: utf-8 -*-
"""图像资产索引：解析 07-图像资产/assets-*.md 的资产条目与最新绑定快照。"""
import json
import re
from pathlib import Path

ASSET_DIR = "07-图像资产"
WB_PROJECT = "00-项目管理/工作台/project.json"
BIND_GLOB = "资产绑定/asset-bindings-*.md"

SECTION_TYPE = [("角色", "character"), ("场景", "scene"), ("道具", "prop"), ("辅助", "extra")]
ENTRY = re.compile(r"^### ([A-Z]\d{2}(?:[-／][A-Za-z]+)?)｜(.+)$", re.M)


def latest_asset_doc(project: Path) -> Path:
    d = project / ASSET_DIR
    if not d.is_dir():
        return None
    docs = sorted(d.glob("assets-v*.md"))
    return docs[-1] if docs else None


def parse_assets(project: Path) -> list:
    f = latest_asset_doc(project)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## (.+)$", txt, re.M))
    out = []
    for i, h in enumerate(heads):
        title = h.group(1)
        kind = None
        # 先按标题判断；"辅助"类必须显式命中，避免把"道具"段误归
        for key, val in SECTION_TYPE:
            if key in title:
                kind = val
                break
        if not kind:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        body = txt[h.end():end]
        for m in ENTRY.finditer(body):
            out.append({
                "id": m.group(1),
                "name": m.group(2).strip(),
                "type": kind,
                "section": title.strip(),
            })
    return out


def parse_bindings(project: Path) -> dict:
    d = project / ASSET_DIR
    if not d.is_dir():
        return {}
    files = sorted(d.glob(BIND_GLOB))
    if not files:
        return {}
    txt = files[-1].read_text(encoding="utf-8")
    out = {}
    for block in re.split(r"^## ", txt, flags=re.M)[1:]:
        name = block.split("\n")[0].strip()
        aid = re.search(r"资产 ID：(.+)", block)
        ver = re.search(r"已采用版本：(.+)", block)
        img = re.search(r"图片：(.+)", block)
        scenes = re.search(r"场次：(.+)", block)
        if not aid:
            continue
        key = aid.group(1).strip().split("/")[-1].strip()
        out[key + ("" if "--" not in aid.group(1) else "--" + aid.group(1).split("--")[1].strip())] = {
            "id": aid.group(1).strip(),
            "name": name,
            "version": ver.group(1).strip() if ver else "",
            "stateId": (re.search(r"类型与状态：.+?/\s*([A-Z]\d{2}(?:-[A-Z])?)", block).group(1)
                        if re.search(r"类型与状态：.+?/\s*([A-Z]\d{2}(?:-[A-Z])?)", block) else ""),
            "image": img.group(1).strip() if img else "",
            "scenes": scenes.group(1).strip() if scenes else "",
            "exists": bool(img) and (project / img.group(1).strip()).exists(),
        }
    return out


def parse_ignored(project: Path) -> dict:
    """工作台里被删槽（trashedSlots）= 作者明确忽略、不生成的资产。"""
    f = project / WB_PROJECT
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for item in d.get("trashedSlots") or []:
        slot = item.get("slot") or {}
        for key in {slot.get("id", ""), slot.get("stateId", "")}:
            if key:
                out[key] = {"label": slot.get("label", ""), "kind": slot.get("kind", ""),
                            "removedAt": item.get("removedAt")}
    return out


def build(project: Path) -> dict:
    assets = parse_assets(project)
    binds = parse_bindings(project)
    # 绑定键可能带 C01--C01-A，统一按短 ID 对齐
    short = {}
    for k, v in binds.items():
        for cand in (v["id"].split("--")[-1].strip(), v.get("stateId", "")):
            if cand:
                short.setdefault(cand, v)
        short.setdefault(k, v)
    ignored = parse_ignored(project)
    for a in assets:
        base = a["id"].split("／")[0].strip()
        ig = ignored.get(a["id"]) or ignored.get(base)
        a["ignored"] = bool(ig)
        a["ignoredAt"] = (ig or {}).get("removedAt")
        b = (short.get(a["id"]) or short.get(a["id"].replace("-", "--"))
             or short.get(base) or short.get(base + "-A"))
        a["binding"] = b
        a["bound"] = bool(b and b.get("exists"))
    bound = sum(1 for a in assets if a["bound"])
    ign = sum(1 for a in assets if a["ignored"])
    return {
        "doc": str(latest_asset_doc(project).relative_to(project)) if latest_asset_doc(project) else None,
        "bindingsFile": str(sorted((project / ASSET_DIR).glob(BIND_GLOB))[-1].relative_to(project))
                        if list((project / ASSET_DIR).glob(BIND_GLOB)) else None,
        "assets": assets,
        "summary": {
            "total": len(assets),
            "bound": bound,
            "ignored": ign,
            "unbound": len(assets) - bound - ign,
            "character": sum(1 for a in assets if a["type"] == "character"),
            "scene": sum(1 for a in assets if a["type"] == "scene"),
            "prop": sum(1 for a in assets if a["type"] == "prop"),
        },
    }
