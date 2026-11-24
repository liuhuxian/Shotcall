# -*- coding: utf-8 -*-
"""镜头候选与采用：按 SH 聚合视频结果，一镜一个采用结论，导出 CSV/EDL。"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

import scenes
import store
import vjobs

PICKS = "video-picks.jsonl"
SH = re.compile(r"SH\d{3}")


def _picks(project: Path) -> dict:
    out = {}
    f = store.state_dir(project) / PICKS
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        out[(r.get("scene"), r.get("shot"))] = r
    return out


def _provenance(project: Path, file: str, unit: dict, job: dict) -> dict:
    return {"file": file, "unit": unit.get("id"), "seed": job.get("seed"),
            "batch": job.get("batch"), "job": job.get("id"),
            "prompt": unit.get("source"), "seconds": unit.get("seconds"),
            "refs": len([r for r in (unit.get("refs") or []) if r.get("exists")]),
            "firstFrame": job.get("firstFrame")}


def candidates(project: Path, scene: str) -> dict:
    units = {u["id"]: u for u in vjobs.parse_units(project, scene)}
    jobs = [j for j in vjobs.list_jobs(project=project) if j.get("scene") == scene]
    by_file = {j.get("file"): j for j in jobs if j.get("file")}
    shot_rows = scenes.shot_details(project, scene)
    shots = []
    for sh in shot_rows:
        shots.append({"id": sh["id"], "duration": sh.get("duration"), "candidates": [], "adopted": None})
    by_id = {x["id"]: x for x in shots}
    seen = set()
    root = project / vjobs.RESULT_DIR / scene
    if root.is_dir():
        for f in sorted(root.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            rel = str(f.relative_to(project))
            if rel in seen:
                continue
            seen.add(rel)
            unit = units.get(f.parent.name) or {}
            shots_of = SH.findall(unit.get("shots") or "")
            if not shots_of:
                shots_of = [f.parent.name]
            job = by_file.get(rel) or {}
            prov = _provenance(project, rel, unit, job)
            prov.update({"time": time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                         "kb": max(1, f.stat().st_size // 1024), "mtime": int(f.stat().st_mtime)})
            for sid in shots_of:
                if sid in by_id:
                    by_id[sid]["candidates"].append(prov)
    picks = _picks(project)
    for s in shots:
        s["adopted"] = picks.get((scene, s["id"]))
        if not s["candidates"]:
            s["duration"] = s.get("duration")
    return {"scene": scene, "shots": shots}


def adopt(project: Path, scene: str, shot: str, file: str, action: str, reason: str = "") -> dict:
    if not re.fullmatch(r"SH\d{3}", shot or ""):
        raise ValueError("shot 必须是 SHxxx")
    if action not in ("adopt", "alternate", "hold", "redo"):
        raise ValueError("action 必须是 adopt/alternate/hold/redo")
    if action != "hold":
        p = (project / file).resolve()
        root = (project / vjobs.RESULT_DIR).resolve()
        if not str(p).startswith(str(root) + "/") or not p.is_file():
            raise ValueError("视频不在生成结果目录或不存在")
    rec = {"scene": scene, "shot": shot, "file": file, "action": action,
           "reason": (reason or "").strip(), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    f = store.state_dir(project) / PICKS
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def timeline(project: Path, scene: str) -> list:
    data = candidates(project, scene)
    out = []
    t = 0.0
    for s in data["shots"]:
        dur = float(s.get("duration") or 0)
        adopted = s.get("adopted") or {}
        out.append({"shot": s["id"], "start": round(t, 2), "duration": dur,
                    "file": adopted.get("file") if adopted.get("action") == "adopt" else "",
                    "action": adopted.get("action") or "none",
                    "reason": adopted.get("reason") or ""})
        t += dur
    return out


def export_text(project: Path, scene: str, fmt: str = "csv") -> tuple:
    rows = timeline(project, scene)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["scene", "shot", "start", "duration", "file", "action", "reason"])
        for r in rows:
            w.writerow([scene, r["shot"], r["start"], r["duration"], r["file"], r["action"], r["reason"]])
        return buf.getvalue(), "text/csv", f"{scene}-edl.csv"
    lines = [f"TITLE: {scene}", "FCM: NON-DROP FRAME", ""]
    for i, r in enumerate(rows, 1):
        if r["action"] != "adopt" or not r["file"]:
            lines.append(f"* {r['shot']} HOLD")
            continue
        a = int(r["start"] * 25); b = int((r["start"] + r["duration"]) * 25)
        def tc(f):
            s, fr = divmod(f, 25); m, s = divmod(s, 60); h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}:{fr:02d}"
        lines.append(f"{i:03d}  AX       V     C        {tc(a)} {tc(b)} {tc(a)} {tc(b)}")
        lines.append(f"* FROM CLIP NAME: {r['file']}")
        lines.append("")
    return "\n".join(lines), "text/plain", f"{scene}-edl.edl"
