# -*- coding: utf-8 -*-
"""图像资产索引：解析 07-图像资产/assets-*.md 的资产条目与最新绑定快照。"""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
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


TEXT_PROPS = []


def parse_assets(project: Path) -> list:
    TEXT_PROPS.clear()
    f = latest_asset_doc(project)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## (.+)$", txt, re.M))
    out = []
    for i, h in enumerate(heads):
        title = h.group(1)
        # 附录/不生产段：只留文字依据，不进入出图名单
        if ("附" in title and ("不生产" in title or "仅文字" in title)) or "不生产的道具" in title:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
            for m in ENTRY.finditer(txt[h.end():end]):
                TEXT_PROPS.append({"id": m.group(1), "name": m.group(2).strip(),
                                   "section": title.strip()})
            continue
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
    out = {}
    txt = ""
    for f in files:                     # 合并全部快照，后者覆盖前者
        txt += "\n" + f.read_text(encoding="utf-8")
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


def parse_prompts(project: Path) -> dict:
    """从最新 assets-v*.md 的「完整中文生成提示词」取每项提示词；回退 project.json slot。"""
    out = {}
    f = latest_asset_doc(project)
    if f:
        txt = f.read_text(encoding="utf-8")
        heads = list(re.finditer(r"^### ([A-Z]\d{2}(?:[-／][A-Za-z]+)?)｜(.+)$", txt, re.M))
        for i, m in enumerate(heads):
            body = txt[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            pm = re.search(r"\*\*完整中文生成提示词\*\*\s*\n+(.*?)(?=\n\*\*|\n### |\n## |\Z)", body, re.S)
            if not pm:
                pm = re.search(r"\*\*完整描述符\*\*\s*\n+(.*?)(?=\n\*\*|\n### |\n## |\Z)", body, re.S)
            if pm:
                out[m.group(1)] = pm.group(1).strip()
    fp = project / WB_PROJECT
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    for slot in d.get("slots") or []:
        sid = slot.get("stateId") or slot.get("id")
        if sid and sid not in out:
            p = slot.get("prompt") or ""
            if p:
                out[sid] = p
    return out


def parse_descriptors(project: Path) -> dict:
    """完整描述符（用于首帧提示词），回退 project.json slot.descriptor。"""
    out = {}
    f = latest_asset_doc(project)
    if f:
        txt = f.read_text(encoding="utf-8")
        heads = list(re.finditer(r"^### ([A-Z]\d{2}(?:[-／][A-Za-z]+)?)｜(.+)$", txt, re.M))
        for i, m in enumerate(heads):
            body = txt[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            pm = re.search(r"\*\*完整描述符\*\*\s*\n+(.*?)(?=\n\*\*|\n### |\n## |\Z)", body, re.S)
            if pm:
                out[m.group(1)] = pm.group(1).strip()
    fp = project / WB_PROJECT
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    for slot in d.get("slots") or []:
        sid = slot.get("stateId") or slot.get("id")
        if sid and sid not in out and slot.get("descriptor"):
            out[sid] = slot["descriptor"]
    return out


def write_binding(project: Path, asset: dict, image_rel: str, version: str = None) -> Path:
    """新增一份绑定快照（合并旧快照，只覆盖本资产），不覆盖既有文件。"""
    root = project / ASSET_DIR
    bind_dir = root / "资产绑定"
    bind_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(bind_dir.glob("asset-bindings-*.md"))
    header = "# 洋子工作台资产绑定\n\n按导演台采用记录生成的新快照；旧快照保留。\n"
    blocks = []
    old_id = ""
    if files:
        txt = files[-1].read_text(encoding="utf-8")
        parts = re.split(r"(?m)^## ", txt)
        header = parts[0].rstrip() + "\n"
        blocks = ["## " + x for x in parts[1:]]
        rev = 1
        mm = re.search(r"-r(\d+)", files[-1].name)
        if mm:
            rev = int(mm.group(1)) + 1
        for b in blocks:
            if re.search(r"资产 ID：.*" + re.escape(asset["id"]), b) or re.search(r"状态ID：" + re.escape(asset["id"]), b):
                old_id = re.search(r"资产 ID：(.+)", b).group(1).strip() if re.search(r"资产 ID：(.+)", b) else asset["id"]
                break
    else:
        rev = 1
    aid = old_id or asset["id"]
    typ = {"character": "character", "scene": "scene", "prop": "prop",
           "extra": "extra", "voice": "voice"}.get(asset.get("type"), asset.get("type", ""))
    ver = version or (asset.get("binding") or {}).get("version") or "v1"
    m = re.match(r"v(\d+)", str(ver))
    ver = "v" + str((int(m.group(1)) if m else 0) + 1)
    scenes = (asset.get("binding") or {}).get("scenes") or ""
    sha = hashlib.sha256((project / image_rel).read_bytes()).hexdigest()
    block = f"""## {asset.get('name','')}

- 资产 ID：{aid}
- 类型与状态：{typ} / {asset['id']}
- 已采用版本：{ver}
- 场次：{scenes}
- 图片：{image_rel}
- SHA256：{sha}
- 来源：workbench
"""
    rep = False
    for i, b in enumerate(blocks):
        if re.search(r"资产 ID：.*" + re.escape(asset["id"]), b) or re.search(r"状态ID：" + re.escape(asset["id"]), b):
            blocks[i] = block
            rep = True
            break
    if not rep:
        blocks.append(block)
    out = bind_dir / f"asset-bindings-r{rev:06d}-{sha[:12]}.md"
    k = 2
    while out.exists():
        out = bind_dir / f"asset-bindings-r{rev:06d}-{sha[:12]}-{k}.md"
        k += 1
    out.write_text(header.rstrip() + "\n\n" + "\n".join(blocks).strip() + "\n", encoding="utf-8")
    return out


def _read_project(project: Path) -> dict:
    return json.loads((project / WB_PROJECT).read_text(encoding="utf-8"))


def _write_project(project: Path, data: dict) -> None:
    f = project / WB_PROJECT
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)


def _next_asset_ver(project: Path) -> int:
    nums = []
    for f in (project / ASSET_DIR).glob("assets-v*.md"):
        m = re.search(r"-v(\d{3})", f.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _update_asset_doc(project: Path, asset: dict, image_rel: str, binding_rel: str,
                      version: int, sha: str) -> Path:
    """从最新 assets-v*.md 复制下一版，只改本资产的“图片状态”和状态行。"""
    latest = latest_asset_doc(project)
    if not latest:
        raise ValueError("没有 assets-v*.md")
    text = latest.read_text(encoding="utf-8")
    nv = _next_asset_ver(project)
    out = latest.with_name(f"assets-v{nv:03d}.md")
    text = re.sub(r"(?m)^状态：.*$", "状态：草稿待确认", text)
    text = text.replace("# 图像资产\n", "# 图像资产\n\n采用更新：导演台采用 " + image_rel +
                        "（绑定 " + binding_rel + "，版本 v" + str(version) + "）\n", 1)
    aid = asset["id"]
    pat = re.compile(r"(?m)^### " + re.escape(aid) + r"｜.*$")
    m = pat.search(text)
    if m:
        end_m = re.search(r"(?m)^(### |## )", text[m.end():])
        end = m.end() + (end_m.start() if end_m else len(text) - m.end())
        sec = text[m.end():end]
        line = f"- 图片状态：导演台采用 v{version}；图片：{image_rel}；SHA256：{sha}；绑定快照：{binding_rel}。"
        if re.search(r"(?m)^- 图片状态：.*$", sec):
            sec = re.sub(r"(?m)^- 图片状态：.*$", line, sec, count=1)
        else:
            sec = sec.rstrip() + "\n" + line + "\n"
        text = text[:m.end()] + sec + text[end:]
    out.write_text(text, encoding="utf-8")
    return out


def adopt_image(project: Path, asset_id: str, image_rel: str) -> dict:
    """导演台采用：新 binding 快照 + project.json 候选/绑定 + 新 assets-vNNN（草稿）。"""
    img = (project / image_rel).resolve()
    if not img.is_file():
        raise ValueError("候选图不存在")
    assets = build(project).get("assets") or []
    asset = next((a for a in assets if a.get("id") == asset_id), None)
    if not asset:
        raise ValueError("未知资产")
    sha = hashlib.sha256(img.read_bytes()).hexdigest()
    binding = write_binding(project, asset, image_rel)
    binding_rel = str(binding.relative_to(project))
    proj = _read_project(project)
    slot = next((s for s in proj.get("slots") or []
                 if s.get("stateId") == asset_id or s.get("id") == asset_id
                 or str(s.get("id", "")).endswith("--" + asset_id)), None)
    if not slot:
        raise ValueError(f"project.json 找不到槽位 {asset_id}")
    cands = proj.setdefault("candidates", [])
    cand = next((c for c in cands if c.get("sha256") == sha), None)
    if not cand:
        cand = {"id": "wb-" + sha[:16], "path": image_rel, "mimeType": "image/png",
                "sha256": sha, "bytes": img.stat().st_size, "source": "workbench",
                "suggestedSlotId": slot.get("id"),
                "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        cands.append(cand)
    ver = int(slot.get("bindingVersion") or 0) + 1
    slot["bindingVersion"] = ver
    slot["currentCandidateId"] = cand["id"]
    slot["preferredCandidateId"] = cand["id"]
    hist = slot.setdefault("history", [])
    hist.append({"candidateId": cand["id"], "version": ver,
                 "adoptedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")})
    proj["revision"] = int(proj.get("revision") or 0) + 1
    proj["bindingManifest"] = binding_rel
    _write_project(project, proj)
    doc = _update_asset_doc(project, asset, image_rel, binding_rel, ver, sha)
    return {"ok": True, "bindingFile": binding_rel, "projectRevision": proj["revision"],
            "assetDoc": str(doc.relative_to(project)), "candidateId": cand["id"],
            "version": ver, "sha256": sha}


def slot_history(project: Path) -> dict:
    """project.json 里各槽位的采用历史：candidateId -> 路径/版本/时间。"""
    f = project / WB_PROJECT
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cmap = {c.get("id"): c for c in (d.get("candidates") or [])}
    out = {}
    for slot in d.get("slots") or []:
        hist = []
        for h in slot.get("history") or []:
            c = cmap.get(h.get("candidateId")) or {}
            file = c.get("path")
            hist.append({"candidateId": h.get("candidateId"), "file": file,
                         "version": "v" + str(h.get("version") or ""),
                         "at": h.get("adoptedAt") or "",
                         "exists": bool(file) and (project / file).is_file(),
                         "sha256": c.get("sha256") or ""})
        for key in {slot.get("stateId", ""), slot.get("id", "")}:
            if key:
                out[key] = hist
    return out


def set_ignored(project: Path, asset_id: str, ignored: bool) -> dict:
    """设置资产忽略（写入 project.json.trashedSlots；不删除 slots）。"""
    proj = _read_project(project)
    slots = proj.get("slots") or []
    slot = next((s for s in slots if s.get("stateId") == asset_id or s.get("id") == asset_id
                 or str(s.get("id", "")).endswith("--" + asset_id)), None)
    if not slot:
        raise ValueError(f"project.json 找不到槽位 {asset_id}")
    trash = proj.setdefault("trashedSlots", [])
    keep = [x for x in trash
            if not ((x.get("slot") or {}).get("stateId") == asset_id
                    or (x.get("slot") or {}).get("id") == slot.get("id")
                    or (x.get("slot") or {}).get("stateId") == slot.get("stateId"))]
    if ignored:
        keep.append({"slot": slot, "index": slots.index(slot),
                     "removedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")})
    proj["trashedSlots"] = keep
    proj["revision"] = int(proj.get("revision") or 0) + 1
    _write_project(project, proj)
    return {"ok": True, "asset": asset_id, "ignored": bool(ignored),
            "revision": proj["revision"], "trashed": len(keep)}


def current_ref(project: Path, path: str) -> dict:
    """把提示词里引用的旧候选图解析到该资产当前绑定图。"""
    try:
        proj = _read_project(project)
    except Exception:
        return {}
    cands = {c.get("path"): c for c in (proj.get("candidates") or []) if c.get("path")}
    c = cands.get(path)
    slots = proj.get("slots") or []
    slot = None
    if c:
        sid = c.get("suggestedSlotId") or ""
        for s in slots:
            ids = {s.get("id", ""), s.get("stateId", ""),
                   "--".join([s.get("id", ""), s.get("stateId", "")])}
            if sid in ids or s.get("stateId") in sid:
                slot = s
                break
            hist = [h.get("candidateId") for h in (s.get("history") or [])]
            if c.get("id") in hist or c.get("id") in (s.get("currentCandidateId"), s.get("preferredCandidateId")):
                slot = s
                break
    if not slot:
        return {}
    cid = slot.get("currentCandidateId") or slot.get("preferredCandidateId")
    cur = next((x for x in (proj.get("candidates") or []) if x.get("id") == cid), None)
    if not cur or not cur.get("path"):
        return {}
    cp = cur["path"]
    return {"asset": slot.get("stateId") or slot.get("id"), "currentPath": cp,
            "changed": cp != path, "promptPath": path}


def ref_asset(project: Path, path: str) -> str:
    """参考图 -> 资产 ID（用于资产审核门禁）。"""
    try:
        data = build(project)
    except Exception:
        return ""
    for a in data.get("assets") or []:
        if (a.get("binding") or {}).get("image") == path:
            return a.get("id") or ""
    try:
        proj = _read_project(project)
    except Exception:
        return ""
    cand = next((c for c in (proj.get("candidates") or []) if c.get("path") == path), None)
    if not cand:
        return ""
    sid = str(cand.get("suggestedSlotId") or "")
    base = sid.split("--")[0]
    for a in data.get("assets") or []:
        aid = a.get("id") or ""
        if aid == sid or aid == base or aid.split("／")[0] == base or aid.startswith(base + "／"):
            return aid
    return base


def build(project: Path) -> dict:
    assets = parse_assets(project)
    try:
        import voice as voice_mod
        vd = voice_mod.build(project)
        for it in vd.get("items") or []:
            assets.append({"id": it["id"], "name": it["name"], "type": "voice",
                           "section": "音色", "ignored": False,
                           "binding": {"version": "v1" if it.get("bound") else "",
                                       "image": "", "audio": (it.get("path") or ""),
                                       "duration": it.get("duration") or ""},
                           "bound": bool(it.get("bound"))})
    except Exception:
        pass
    prompts = parse_prompts(project)
    descriptors = parse_descriptors(project)
    histories = slot_history(project)
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
        if a.get("type") == "voice":      # 音色已由 voice 模块判定，不走图像绑定
            continue
        base = a["id"].split("／")[0].strip()
        ig = ignored.get(a["id"]) or ignored.get(base)
        a["ignored"] = bool(ig)
        a["ignoredAt"] = (ig or {}).get("removedAt")
        b = (short.get(a["id"]) or short.get(a["id"].replace("-", "--"))
             or short.get(base) or short.get(base + "-A"))
        a["binding"] = b
        a["bound"] = bool(b and b.get("exists"))
        a["prompt"] = prompts.get(a["id"]) or ""
        a["descriptor"] = descriptors.get(a["id"]) or a.get("descriptor") or ""
        a["history"] = histories.get(a["id"]) or histories.get(a["id"].replace("-", "--")) or []
    img = [a for a in assets if a.get("type") != "voice"]   # 图像资产口径保持 36 项
    vos = [a for a in assets if a.get("type") == "voice"]
    bound = sum(1 for a in img if a["bound"])
    ign = sum(1 for a in img if a.get("ignored"))
    return {
        "doc": str(latest_asset_doc(project).relative_to(project)) if latest_asset_doc(project) else None,
        "bindingsFile": str(sorted((project / ASSET_DIR).glob(BIND_GLOB))[-1].relative_to(project))
                        if list((project / ASSET_DIR).glob(BIND_GLOB)) else None,
        "assets": assets,
        "summary": {
            "total": len(img),
            "bound": bound,
            "ignored": ign,
            "unbound": len(img) - bound - ign,
            "character": sum(1 for a in assets if a["type"] == "character"),
            "voice": len(vos),
            "voiceBound": sum(1 for a in vos if a["bound"]),
            "scene": sum(1 for a in assets if a["type"] == "scene"),
            "prop": sum(1 for a in assets if a["type"] == "prop"),
            "extra": sum(1 for a in assets if a["type"] == "extra"),
            "textProps": len(TEXT_PROPS),
        },
    }
