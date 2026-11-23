# -*- coding: utf-8 -*-
"""成果登记：只读解析项目的 00-项目管理/artifact-state.json，并核对文件现状。

约定：**本模块不写项目文件**。登记表由项目自己的工具维护，
我们只读取 + 计算"文件是否被改过 / 依赖是否失效"。
"""
import hashlib
import json
from pathlib import Path

import stages

STATE_FILE = "00-项目管理/artifact-state.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_registry(project: Path) -> dict:
    f = project / STATE_FILE
    if not f.exists():
        return {"exists": False, "artifacts": [], "current": {}}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "error": f"{e}", "artifacts": [], "current": {}}
    return {"exists": True, "registryPath": STATE_FILE,
            "artifacts": d.get("artifacts") or [], "current": d.get("current") or {},
            "schemaVersion": d.get("schema_version")}


SKIP_DIRS = ("生成结果", "候选图片", "资产绑定", "历史版本", "工作台", "_develop-work",
             "trailer", "废图", "旧版本", "聊天附件")
DOC_EXT = (".md",)
VERSIONED = __import__("re").compile(r"-v(\d{3})(-\d+)?\.md$")


def family_of(path: str) -> tuple:
    """把一个文件归到"同一份文档的第几版"：返回 (家族键, 版本号)。"""
    m = VERSIONED.search(path)
    if not m:
        return (path, 0)
    return (VERSIONED.sub("", path), int(m.group(1)))


def scan_unregistered(project: Path, known: set, known_versions: dict = None) -> list:
    """目录里有、登记表里没有的文档。

    只保留"比该文档当前最新登记版本更新"的文件；历史版本不再列出。
    家族从未登记过的（全新文档）一律保留。
    """
    known_versions = known_versions or {}
    out = []
    for s in stages.STAGES:
        d = project / s["dir"]
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in DOC_EXT:
                continue
            rel = str(f.relative_to(project))
            if rel in known:
                continue
            if any(part in rel for part in SKIP_DIRS):
                continue
            if f.name.startswith(("原生草稿", "manualDraft")):
                continue
            if not VERSIONED.search(f.name):      # 只认带版本号的成果文档
                continue
            fam, ver = family_of(rel)
            top = known_versions.get(fam)
            if top is not None and ver <= top:    # 比已登记的最新版旧或相同 → 不列
                continue
            out.append({
                "path": rel, "stage": s["no"], "name": f.name,
                "sizeKB": round(f.stat().st_size / 1024),
                "mtime": int(f.stat().st_mtime),
                "time": __import__("time").strftime("%m-%d %H:%M", __import__("time").localtime(f.stat().st_mtime)),
            })
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def build(project: Path) -> dict:
    """返回 {stages:[...], artifacts:[...], summary:{...}}"""
    reg = load_registry(project)
    raw = reg["artifacts"]

    known = {}
    for a in raw:
        p = a.get("path") or ""
        f = project / p
        now_sha = None
        exists = f.exists()
        if exists:
            try:
                now_sha = _sha256(f)
            except Exception:
                now_sha = None
        recorded = a.get("sha256")
        if not exists:
            file_state = "missing"
        elif now_sha and recorded and now_sha != recorded:
            file_state = "modified"
        else:
            file_state = "ok"

        known[p] = {
            "path": p,
            "kind": a.get("kind", ""),
            "scope": a.get("scope", ""),
            "status": stages.STATUS_MAP.get(a.get("status", ""), "unknown"),
            "statusText": a.get("status", ""),
            "sha256": recorded,
            "shaNow": now_sha,
            "fileState": file_state,
            "exists": exists,
            "sizeKB": round(f.stat().st_size / 1024) if exists else 0,
            "mtime": f.stat().st_mtime if exists else None,
            "dependsOn": a.get("depends_on") or [],
            "createdAt": a.get("created_at"),
            "confirmedAt": a.get("confirmed_at"),
            "stage": stages.stage_of_kind(a.get("kind", "")) or stages.stage_of_path(p),
        }

    # 依赖失效判定：上游被改过 / 缺失 / 尚未确认
    for item in known.values():
        stale = []
        for dep_path in item["dependsOn"]:
            dep = known.get(dep_path)
            if not dep:
                stale.append({"path": dep_path, "reason": "未登记"})
            elif not dep["exists"]:
                stale.append({"path": dep_path, "reason": "文件缺失"})
            elif dep["fileState"] == "modified":
                stale.append({"path": dep_path, "reason": "上游已改动"})
            elif dep["status"] == "draft":
                stale.append({"path": dep_path, "reason": "上游仍是草稿"})
        item["staleDeps"] = stale

    arts = sorted(known.values(), key=lambda x: (x["stage"], x["path"]))
    known_versions = {}
    for pth in known:
        fam, ver = family_of(pth)
        known_versions[fam] = max(known_versions.get(fam, 0), ver)
    unreg = scan_unregistered(project, set(known.keys()), known_versions)

    out_stages = []
    for s in stages.STAGES:
        items = [a for a in arts if a["stage"] == s["no"]]
        counts = {"draft": 0, "confirmed": 0, "superseded": 0, "conclusion": 0}
        for a in items:
            if a["status"] in counts:
                counts[a["status"]] += 1
        d = project / s["dir"]
        out_stages.append({
            "no": s["no"], "name": s["name"], "dir": s["dir"], "desc": s["desc"],
            "exists": d.exists(),
            "fileCount": sum(1 for _ in d.rglob("*") if _.is_file()) if d.exists() else 0,
            "artifacts": len(items),
            "counts": counts,
            "staleDeps": sum(1 for a in items if a["staleDeps"]),
            "modifiedFiles": sum(1 for a in items if a["fileState"] == "modified"),
            "missingFiles": sum(1 for a in items if a["fileState"] == "missing"),
            # 阶段状态：有确认就算推进过；有草稿未确认则"待确认"
            "state": ("blocked" if any(a["fileState"] == "missing" for a in items)
                      else "draft" if counts["draft"]
                      else "confirmed" if counts["confirmed"]
                      else "conclusion" if counts["conclusion"]
                      else "empty" if not items
                      else "superseded"),
        })

    summary = {
        "unregistered": len(unreg),
        "total": len(arts),
        "draft": sum(1 for a in arts if a["status"] == "draft"),
        "confirmed": sum(1 for a in arts if a["status"] == "confirmed"),
        "superseded": sum(1 for a in arts if a["status"] == "superseded"),
        "conclusion": sum(1 for a in arts if a["status"] == "conclusion"),
        "modified": sum(1 for a in arts if a["fileState"] == "modified"),
        "missing": sum(1 for a in arts if a["fileState"] == "missing"),
        "staleDeps": sum(1 for a in arts if a["staleDeps"]),
    }
    return {"exists": reg["exists"], "registryPath": STATE_FILE,
            "stages": out_stages, "artifacts": arts,
            "unregistered": unreg, "summary": summary}
