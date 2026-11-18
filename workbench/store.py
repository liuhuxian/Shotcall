#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策落账：追加式事件流（约定 B）。

- 只追加，不覆盖；改判 = 新事件 + supersedes 指向旧事件
- 存储：<state>/decisions.jsonl，一行一条
- state 目录默认 项目根/制作运行/，可用 WORKBENCH_STATE 覆盖（测试用）
"""
import json
import os
import time
import uuid
from pathlib import Path

VERDICTS = {"pass", "revise", "hold", "na"}
UNIT_TYPES = {"scene", "shot", "asset", "music", "batch"}


FALLBACK = Path(__file__).resolve().parent / "run"


def state_dir(project: Path) -> Path:
    """优先写项目的 制作运行/；不可写时退化到工作台自己的 run/（并说明原因）。"""
    env = os.environ.get("WORKBENCH_STATE")
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = project / "制作运行"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        FALLBACK.mkdir(parents=True, exist_ok=True)
        print(f"[warn] 无法写入 {d}，决策改存 {FALLBACK}")
        return FALLBACK


def _path(project: Path) -> Path:
    return state_dir(project) / "decisions.jsonl"


def read_all(project: Path) -> list:
    p = _path(project)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def latest(project: Path) -> list:
    """每个 (gate, unit) 取最后一条有效记录。"""
    cur = {}
    for rec in read_all(project):
        if rec.get("supersededBy"):
            continue
        cur[(rec["gate"], rec["unit"]["type"], rec["unit"]["id"])] = rec
    for rec in read_all(project):
        sup = rec.get("supersedes")
        if sup:
            for k, v in list(cur.items()):
                if v["id"] == sup:
                    cur.pop(k)
    return list(cur.values())


def append(project: Path, gate: int, unit: dict, verdict: str,
           notes=None, scope=None, evidence=None, by: str = "director") -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"未知判定: {verdict}")
    if unit.get("type") not in UNIT_TYPES:
        raise ValueError(f"未知单元类型: {unit.get('type')}")

    prev = None
    for rec in latest(project):
        if rec["gate"] == gate and rec["unit"] == unit:
            prev = rec
            break

    rec = {
        "id": "DEC-" + uuid.uuid4().hex[:10],
        "gate": gate,
        "unit": unit,
        "verdict": verdict,
        "notes": notes or [],
        "scope": scope or {},
        "evidence": evidence or {},
        "by": by,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if prev:
        rec["supersedes"] = prev["id"]

    with open(_path(project), "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


SEL = {"adopt", "backup", "redo"}


def _sel_path(project: Path) -> Path:
    return state_dir(project) / "selections.jsonl"


def read_selections(project: Path) -> list:
    p = _sel_path(project)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def latest_selections(project: Path) -> dict:
    """每个镜头取最后一条（后写覆盖前写，但历史仍留档）。"""
    cur = {}
    for rec in read_selections(project):
        cur[rec["shot"]] = rec
    return cur


def select(project: Path, shot: str, candidate: str, status: str,
           reason=None, by: str = "director") -> dict:
    if status not in SEL:
        raise ValueError(f"未知选择状态: {status}")
    prev = latest_selections(project).get(shot)
    rec = {
        "id": "SEL-" + uuid.uuid4().hex[:10],
        "shot": shot,
        "candidate": candidate,
        "status": status,
        "reason": reason or [],
        "by": by,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if prev:
        rec["supersedes"] = prev["id"]
    with open(_sel_path(project), "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def summary(project: Path) -> dict:
    allrec = read_all(project)
    live = latest(project)
    return {"total": len(allrec), "live": len(live),
            "path": str(_path(project)),
            "records": live}
