# -*- coding: utf-8 -*-
"""音色资产：读 07-图像资产/音色参考-v*.md 与绑定快照，只读。"""
import re
from pathlib import Path

DOC_GLOB = "07-图像资产/音色参考-v*.md"
BIND_GLOB = "07-图像资产/资产绑定/asset-bindings-*.md"
ENTRY = re.compile(r"^### (.+)$", re.M)
FIELD = lambda k: re.compile(rf"^- {k}：(.+)$", re.M)


def latest_doc(project: Path):
    files = sorted((project).glob(DOC_GLOB))
    return files[-1] if files else None


def parse_doc(project: Path) -> list:
    f = latest_doc(project)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    heads = list(ENTRY.finditer(txt))
    out = []
    for i, m in enumerate(heads):
        body_head = m.group(1).strip()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(txt)
        body = txt[m.end():end]
        def grab(label):
            g = FIELD(label).search(body)
            return g.group(1).strip() if g else ""
        path = grab("文件").strip("`")
        aid = grab("资产 ID") or ""
        aid = aid.split("--")[-1].strip() or aid.strip()
        if "-V" not in aid:            # 只认音色条目
            continue
        out.append({
            "id": aid,
            "name": body_head,
            "type": "voice",
            "role": grab("对应角色"),
            "path": path,
            "sha256": grab("SHA256"),
            "duration": grab("时长"),
            "format": grab("格式"),
            "scenes": grab("适用场次"),
            "usage": grab("使用说明"),
            "exists": bool(path) and (project / path).exists(),
            "doc": str(f.relative_to(project)),
        })
    return out


def parse_bindings(project: Path) -> dict:
    """合并所有绑定快照，取每个资产最新一次（按文件名排序）登记。"""
    out = {}
    for f in sorted(project.glob(BIND_GLOB)):
        txt = f.read_text(encoding="utf-8")
        for block in re.split(r"^## ", txt, flags=re.M)[1:]:
            aid = re.search(r"资产 ID：(.+)", block)
            if not aid:
                continue
            key = aid.group(1).strip()
            audio = re.search(r"音频：(.+)", block)
            ver = re.search(r"已采用版本：(.+)", block)
            scenes = re.search(r"场次：(.+)", block)
            dur = re.search(r"时长：(.+)", block)
            out[key] = {"id": key, "audio": audio.group(1).strip() if audio else "",
                        "version": ver.group(1).strip() if ver else "",
                        "scenes": scenes.group(1).strip() if scenes else "",
                        "duration": dur.group(1).strip() if dur else "",
                        "snapshot": f.name}
    return out


def build(project: Path) -> dict:
    items = parse_doc(project)
    binds = parse_bindings(project)
    for it in items:
        b = binds.get(f"C01--{it['id']}" if it["id"].startswith("C01") else f"C02--{it['id']}", None)
        if not b:
            base = it["id"].split("-")[0]
            b = next((v for k, v in binds.items() if k.startswith(base) and k.endswith(it["id"].split("-")[-1])), None)
        it["binding"] = b
        it["bound"] = bool(b and b.get("audio") and (project / b["audio"]).exists())
    return {
        "doc": str(latest_doc(project).relative_to(project)) if latest_doc(project) else None,
        "items": items,
        "summary": {"total": len(items), "bound": sum(1 for i in items if i["bound"])},
    }
