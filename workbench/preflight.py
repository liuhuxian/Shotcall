#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前预检（纯计算，不提交任何任务）。工作台与服务端共用。"""


def frames_of(sec: float) -> int:
    n = max(5, round(sec * 24))
    return n + (5 - n % 17) % 17


def run(body: dict, data: dict) -> dict:
    kind = body.get("kind", "video")
    items = body.get("items") or []
    shots = {s["id"]: s for s in data["shots"]}

    blockers, warnings, rows = [], [], []
    for it in items:
        sid = it.get("shot")
        s = shots.get(sid)
        if not s:
            blockers.append(f"{sid}：镜头不存在")
            continue
        fr = frames_of(s["dur"])
        rows.append({"shot": sid, "dur": s["dur"], "frames": fr, "genSec": round(fr / 24, 2)})
        if kind == "video":
            if not s.get("motion"):
                blockers.append(f"{sid}：缺少视频提示词")
            missing = [r["path"] for r in s["refs"] if not r.get("exists", True)]
            if missing:
                blockers.append(f"{sid}：参考图缺失 {missing}")
            if not (124 <= fr <= 362):
                warnings.append(f"{sid}：{fr} 帧不在训练区间 124–362")

    if kind == "video":
        frames = sum(r["frames"] for r in rows)
        est = round(frames * 106 / 124 / 60, 1)
        note = f"按实测 106s/124 帧 线性外推，约 {est} 分钟"
    else:
        est, note = None, "出图耗时未标定，需先出一张测时间"

    return {
        "ok": not blockers, "kind": kind, "items": len(items),
        "blockers": blockers, "warnings": warnings,
        "estMinutes": est, "estNote": note, "rows": rows,
    }
