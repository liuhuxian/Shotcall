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
        raw_id = aid.group(1).strip()
        # 短 ID 作为主键：不同快照里可能出现 L02-A 与 L02--L02-A 两种写法，
        # 统一按状态 ID 归并，后读到的快照覆盖先读到的（即最新绑定生效）。
        key = raw_id.split("--")[-1].strip()
        rec = {
            "id": raw_id,
            "name": name,
            "version": ver.group(1).strip() if ver else "",
            "stateId": (re.search(r"类型与状态：.+?/\s*([A-Z]\d{2}(?:-[A-Z])?)", block).group(1)
                        if re.search(r"类型与状态：.+?/\s*([A-Z]\d{2}(?:-[A-Z])?)", block) else ""),
            "image": img.group(1).strip() if img else "",
            "scenes": scenes.group(1).strip() if scenes else "",
            "exists": bool(img) and (project / img.group(1).strip()).exists(),
        }
        out[key] = rec
        out[raw_id] = rec
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
    header = "# 项目资产绑定\n\n按导演台采用记录生成的新快照；旧快照保留。\n"
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


BIND_BEGIN = "<!-- BINDINGS:BEGIN 由工作台生成，勿手改 -->"
BIND_END = "<!-- BINDINGS:END -->"


def all_cards(project: Path) -> list:
    """文档里所有 ### 卡片（不分段）+ 绑定表里的资产，取并集。

    parse_assets 只认「角色/场景/道具/辅助」四类段落，像「工作台已增辅助项」
    这类段落里的 C06 会被漏掉；绑定总表必须覆盖全部资产，所以单独扫一遍。
    """
    f = latest_asset_doc(project)
    cards = {}
    if f:
        txt = f.read_text(encoding="utf-8")
        for m in ENTRY.finditer(txt):
            aid = m.group(1).split("／")[0].strip()
            cards[aid] = m.group(2).strip()
    bmap = parse_bindings(project)
    for key, rec in bmap.items():
        aid = rec.get("stateId") or rec.get("id", "").split("--")[-1]
        if aid and aid not in cards:
            cards[aid] = rec.get("name") or ""
    return [{"id": k, "name": v} for k, v in sorted(cards.items())]


def _bindings_rows(project: Path) -> list:
    """当前绑定表：资产 -> 图 / 版本 / 来源。未绑定的资产也要出现。"""
    bmap = parse_bindings(project)
    rows = []
    for a in all_cards(project):
        rec = bmap.get(a["id"]) or {}
        rows.append({
            "id": a["id"], "name": a["name"],
            "image": rec.get("image") or "", "version": rec.get("version") or "",
            "scenes": rec.get("scenes") or "",
        })
    return rows


def bindings_block(project: Path) -> str:
    """生成「当前绑定」总表（机器写）。无图资产写 —，让 Agent 一眼看出谁没有图。"""
    rows = _bindings_rows(project)
    lines = [BIND_BEGIN, "## 当前绑定（机器生成，勿手改）", "",
             "| 资产 | 名称 | 当前图 | 版本 | 场次 |",
             "|---|---|---|---|---|"]
    for r in rows:
        img = f"`{r['image']}`" if r["image"] else "—"
        lines.append(f"| {r['id']} | {r['name']} | {img} | {r['version'] or '—'} | {r['scenes'] or '—'} |")
    lines += ["", "> 这段由工作台按绑定表生成；卡片正文里的文字规范与提示词仍由 07 阶段维护。",
              BIND_END]
    return "\n".join(lines)


def apply_bindings_block(project: Path, text: str) -> str:
    """把绑定总表插入/替换到文档标题之后。"""
    block = bindings_block(project)
    if BIND_BEGIN in text and BIND_END in text:
        start = text.index(BIND_BEGIN)
        end = text.index(BIND_END) + len(BIND_END)
        return text[:start] + block + text[end:]
    lines = text.splitlines()
    # 插在第一个标题行之后（跳过可能的状态行）
    insert_at = 0
    for i, ln in enumerate(lines[:8]):
        if ln.startswith("# "):
            insert_at = i + 1
            break
    head = lines[:insert_at]
    tail = lines[insert_at:]
    return "\n".join(head + ["", block, ""] + tail)


def doc_bindings(project: Path) -> dict:
    """解析文档里的绑定总表：资产 -> 图片路径。用于和绑定表比对。"""
    latest = latest_asset_doc(project)
    if not latest:
        return {}
    text = latest.read_text(encoding="utf-8")
    if BIND_BEGIN not in text:
        return {}
    seg = text[text.index(BIND_BEGIN): text.index(BIND_END) if BIND_END in text else len(text)]
    out = {}
    for ln in seg.splitlines():
        m = re.match(r"\|\s*([A-Z]\d{2}(?:-[A-Z])?)\s*\|[^|]*\|\s*(.*?)\s*\|", ln)
        if not m:
            continue
        img = m.group(2).strip().strip("`")
        out[m.group(1)] = "" if img in ("—", "-", "") else img
    return out


def sync_bindings_doc(project: Path) -> dict:
    """按绑定表重写文档里的绑定总表，产出新的 assets 版本（草稿）。"""
    latest = latest_asset_doc(project)
    if not latest:
        raise ValueError("没有 assets-v*.md")
    text = latest.read_text(encoding="utf-8")
    new_text = apply_bindings_block(project, text)
    if new_text.strip() == text.strip():
        return {"ok": True, "changed": False, "doc": str(latest.relative_to(project))}
    nv = _next_asset_ver(project)
    out = latest.with_name(f"assets-v{nv:03d}.md")
    new_text = re.sub(r"(?m)^状态：.*$", "状态：草稿待确认", new_text)
    out.write_text(new_text, encoding="utf-8")
    _ALL_CACHE.clear() if "_ALL_CACHE" in globals() else None
    return {"ok": True, "changed": True, "doc": str(out.relative_to(project)), "version": nv}


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
    try:
        text = apply_bindings_block(project, text)
    except Exception:
        pass
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
    cur_img = (asset.get("binding") or {}).get("image")
    if cur_img:
        same = cur_img == image_rel
        if not same:                       # 路径不同但内容相同也算同一张图
            try:
                cur_file = project / cur_img
                same = cur_file.is_file() and hashlib.sha256(cur_file.read_bytes()).hexdigest() == sha
            except Exception:
                same = False
        if same:
            return {"ok": True, "unchanged": True, "version": (asset.get("binding") or {}).get("version"),
                    "bindingFile": None, "assetDoc": None, "sha256": sha}
    proj = _read_project(project)
    slot = next((s for s in proj.get("slots") or []
                 if s.get("stateId") == asset_id or s.get("id") == asset_id
                 or str(s.get("id", "")).endswith("--" + asset_id)), None)
    # 版本号以两处记录的较大值为基准，避免绑定快照与槽位各自计数导致 v4/v7 对不上
    def _num(v):
        m = re.match(r"v(\d+)", str(v or ""))
        return int(m.group(1)) if m else 0
    base = max(_num((asset.get("binding") or {}).get("version")),
               int((slot or {}).get("bindingVersion") or 0))
    binding = write_binding(project, asset, image_rel, version=f"v{base}")
    binding_rel = str(binding.relative_to(project))
    # 外部 project.json 里没有这个槽位（例如新拆分的资产）时，只写绑定与资产文档，
    # 不再整条失败——否则这些资产永远无法直接指定图片。
    ver = base + 1
    cand = None
    if slot:
        cands = proj.setdefault("candidates", [])
        cand = next((c for c in cands if c.get("sha256") == sha), None)
        if not cand:
            cand = {"id": "wb-" + sha[:16], "path": image_rel, "mimeType": "image/png",
                    "sha256": sha, "bytes": img.stat().st_size, "source": "workbench",
                    "suggestedSlotId": slot.get("id"),
                    "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            cands.append(cand)
        slot["bindingVersion"] = ver
        slot["lastAdoptFile"] = image_rel
        slot["currentCandidateId"] = cand["id"]
        slot["preferredCandidateId"] = cand["id"]
        hist = slot.setdefault("history", [])
        hist.append({"candidateId": cand["id"], "version": ver,
                     "adoptedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")})
        proj["revision"] = int(proj.get("revision") or 0) + 1
        proj["bindingManifest"] = binding_rel
        _write_project(project, proj)
    doc = _update_asset_doc(project, asset, image_rel, binding_rel, ver, sha)
    return {"ok": True, "bindingFile": binding_rel,
            "projectRevision": proj.get("revision"),
            "assetDoc": str(doc.relative_to(project)),
            "candidateId": (cand or {}).get("id"),
            "slotFound": bool(slot), "version": ver, "sha256": sha}


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
        # 同一内容只保留最后一次采用
        dedup = {}
        for h in hist:
            k = h.get("sha256") or h.get("file") or h.get("candidateId")
            dedup[k] = h
        hist = list(dedup.values())
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
    # 候选图总数（供资产页 KPI 显示）
    cand_total = 0
    cand_root0 = project / "07-图像资产/候选图片"
    if cand_root0.is_dir():
        cand_total = sum(1 for f in cand_root0.rglob("*")
                         if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))

    # 未绑定的资产：用最近生成的一张候选当缩略图，方便看进度
    cand_root = project / "07-图像资产/候选图片"
    for a in assets:
        if a.get("type") == "voice" or a.get("bound"):
            continue
        d = cand_root / a["id"]
        if not d.is_dir():
            continue
        imgs = [f for f in d.iterdir()
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        if not imgs:
            continue
        newest = max(imgs, key=lambda f: f.stat().st_mtime)
        a["preview"] = str(newest.relative_to(project))
        a["candidates"] = len(imgs)

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
        # 显示版本：历史里同一张图的最新版本优先于绑定快照的计数
        bnd = a.get("binding") or {}
        if bnd.get("image"):
            num = lambda x: int(re.sub(r"\D", "", str(x or "0")) or 0)
            for h in (a["history"] or []):
                if h.get("file") == bnd["image"] and num(h.get("version")) > num(bnd.get("version")):
                    bnd["version"] = h.get("version")
                    break
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
            "candidates": cand_total,
        },
    }
