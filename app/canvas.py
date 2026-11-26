# -*- coding: utf-8 -*-
"""把 07 资产文档同步到本地画布文件（画布1.json）。

规则：
- 只同步 character / scene，且有提示词的条目
- 已有节点按 shotcallSlotId 匹配，只更新提示词与标签，保留位置、绑定、采用图
- 资产里已不存在的旧节点（例如拆分的 L02）删除
- 新资产按同类型的现有节点克隆，排在同区网格的末尾
- 写文件前备份为 <文件名>.bak-<时分>
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from pathlib import Path

CANVAS_FILE = "画布1.json"
GRID_X = 448
ROW_H = 688
PER_ROW = 5
SECTION_START = {"character": 304, "scene": 1872}


def _asset_scenes(project: Path) -> dict:
    """从资产文档里取每个状态卡的「来源场次」。"""
    out = {}
    try:
        import assetsdb
        f = assetsdb.latest_asset_doc(project)
        if not f:
            return out
        txt = f.read_text(encoding="utf-8")
    except Exception:
        return out
    heads = list(re.finditer(r"^### (\S+?)｜(.+)$", txt, re.M))
    for i, m in enumerate(heads):
        sid = m.group(1).strip()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        body = txt[m.end():end]
        g = re.search(r"来源场次：([^\n]+)", body)
        if g:
            out[sid] = g.group(1).strip().rstrip("。；; ")
    return out


def _ids(a: dict) -> list:
    """一个资产可能的多种写法：C06／default / C06 / C01-A / C01--C01-A。"""
    raw = (a.get("id") or "").strip()
    base = raw.split("／")[0].strip() if "／" in raw else raw
    stable = base.split("-")[0]
    out = [raw]
    for v in (base, stable, f"{stable}--{base}"):
        if v and v not in out:
            out.append(v)
    return out


def _slot(a: dict) -> str:
    raw = (a.get("id") or "").strip()
    base = raw.split("／")[0].strip() if "／" in raw else raw
    stable = base.split("-")[0]
    return f"{stable}--{base}"


def _matches(node_slot: str, a: dict) -> bool:
    if not node_slot:
        return False
    return node_slot in _ids(a)


def sync(project: Path, canvas_path: Path = None, dry: bool = False) -> dict:
    import assetsdb

    canvas_path = canvas_path or (project / CANVAS_FILE)
    if not canvas_path.is_file():
        raise RuntimeError(f"找不到画布文件：{canvas_path.name}")
    canvas = json.loads(canvas_path.read_text(encoding="utf-8"))
    nodes = canvas.get("nodes") or []
    doc = assetsdb.build(project)
    scenes = _asset_scenes(project)
    def kind_of(a):
        # 辅助人物（extra）在画布上按角色卡处理
        return "character" if a.get("type") in ("character", "extra") else "scene"

    targets = [a for a in doc.get("assets") or []
               if a.get("type") in ("character", "scene", "extra") and (a.get("prompt") or "").strip()]

    gen = [n for n in nodes if str(n.get("type")) == "remote-imageGenerationNode"]
    by_slot = {}
    for n in gen:
        s = _data(n).get("shotcallSlotId")
        if s:
            by_slot[s] = n

    updated, added, unchanged, removed = [], [], [], []
    used = set()
    for a in targets:
        node = None
        for s, n in by_slot.items():
            if s in used:
                continue
            if _matches(s, a):
                node = n
                used.add(s)
                break
        label = f"{'角色卡' if kind_of(a) == 'character' else '场景图'}｜{a['name']}｜{scenes.get(a['id'], '')}".rstrip("｜")
        if node:
            d = node.setdefault("data", {})
            changed = False
            if (d.get("prompt") or "") != a["prompt"]:
                d["prompt"] = a["prompt"]
                pm = d.get("promptModel")
                if isinstance(pm, list) and pm and isinstance(pm[0], list) and pm[0]:
                    pm[0][0]["text"] = a["prompt"] if isinstance(pm[0][0], dict) else pm[0][0]
                d["shotcallPromptVersion"] = int(d.get("shotcallPromptVersion") or d.get("yokoAssetPromptVersion") or 0) + 1
                d.pop("yokoAssetPromptVersion", None)
                changed = True
            if label and d.get("label") != label:
                d["label"] = label
                changed = True
            (updated if changed else unchanged).append(a["id"])
        else:
            added.append((a, label))

    # 删除资产里已经没有的旧节点：只要文档里还有这个资产就保留（哪怕本轮没有提示词）
    all_assets = doc.get("assets") or []

    def _alive(n) -> bool:
        slot = _data(n).get("shotcallSlotId") or ""
        return any(_matches(slot, a) for a in all_assets)

    drop = [n for n in gen
            if _data(n).get("shotcallKind", _data(n).get("yokoAssetKind")) in ("character", "scene")
            and not _alive(n)]
    if drop:
        nodes = [n for n in nodes if n not in drop]
        removed = [_data(n).get("shotcallSlotId") or _data(n).get("label", "")
                   for n in drop]

    # 新节点：克隆同类型模板，排在网格末尾
    for a, label in added:
        tmpl = next((n for n in nodes
                     if _data(n).get("shotcallKind", _data(n).get("yokoAssetKind")) == kind_of(a)), None)
        if not tmpl:
            continue
        node = copy.deepcopy(tmpl)
        node["id"] = str(uuid.uuid4())
        node["initialized"] = False
        d = node.setdefault("data", {})
        d["label"] = label
        d["prompt"] = a["prompt"]
        pm = d.get("promptModel")
        if isinstance(pm, list) and pm and isinstance(pm[0], list) and pm[0]:
            pm[0][0]["text"] = a["prompt"]
        d["outputs"] = {}
        d["shotcallSlotId"] = _slot(a)
        d["shotcallKind"] = kind_of(a)
        d["shotcallPromptVersion"] = 1
        for old in ("yokoAssetSlotId", "yokoAssetKind", "yokoAssetPromptVersion",
                    "yokoAdoptedPreview", "yokoMatchedPreview"):
            d.pop(old, None)
        nodes.append(node)

    # 同类型重排网格（保持 5 列，从各自起始行开始）
    for kind, start_y in SECTION_START.items():
        group = [n for n in nodes
                 if (n.get("data") or {}).get("yokoAssetKind") == kind
                 and str(n.get("type")) == "remote-imageGenerationNode"]
        group.sort(key=lambda n: (n.get("position", {}).get("y", 0), n.get("position", {}).get("x", 0)))
        for i, n in enumerate(group):
            n["position"] = {"x": (i % PER_ROW) * GRID_X, "y": start_y + (i // PER_ROW) * ROW_H}

    canvas["nodes"] = nodes
    summary = {"file": canvas_path.name,
               "updated": updated, "added": [a["id"] for a, _ in added],
               "removed": removed, "unchanged": len(unchanged),
               "totalNodes": len(nodes), "dry": dry}
    if not dry:
        backup = canvas_path.with_name(canvas_path.name + ".bak-" + time.strftime("%H%M"))
        backup.write_text(canvas_path.read_text(encoding="utf-8"), encoding="utf-8")
        tmp = canvas_path.with_name(canvas_path.name + ".tmp")
        tmp.write_text(json.dumps(canvas, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(canvas_path)
        summary["backup"] = backup.name
    return summary
