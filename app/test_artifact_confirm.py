#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段成果“确认”必须更新 artifact-state，并用记录自身 scope，而不是阶段默认 scope。"""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import flow

REAL = Path('/users/huxian/short_v/有谁共鸣2')
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    src = next(REAL.glob('.agents/skills/*/ai-film-router/scripts/artifact_guard.py')).parent
    p = Path(tempfile.mkdtemp(prefix="art-confirm-"))
    (p / "00-项目管理").mkdir(parents=True)
    (p / "05-正式剧本").mkdir()
    rel = "05-正式剧本/screenplay-timing-audit-v001.md"
    (p / rel).write_text("# audit\n", encoding="utf-8")
    key = "05 成稿时长复核::E001—E003／N001—N008::"
    state = {"schema_version": "3.4", "artifacts": [{
        "kind": "05 成稿时长复核", "scope": "E001—E003／N001—N008", "model": "",
        "path": rel, "status": "草稿待确认", "depends_on": [], "affected_shots": [],
        "sha256": hashlib.sha256(b"# audit\n").hexdigest()}],
        "current": {key: {"draft": rel, "confirmed": None, "status": "草稿待确认"}}}
    (p / "00-项目管理/artifact-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    dst = p / ".agents/skills/test/ai-film-router/scripts"
    dst.mkdir(parents=True)
    for f in src.glob("*.py"):
        shutil.copy(f, dst / f.name)
    r = flow.confirm(p, "05b", "N001", path=rel)
    check("确认返回 scope", r.get("scope") == "E001—E003／N001—N008", r)
    d = json.loads((p / "00-项目管理/artifact-state.json").read_text(encoding="utf-8"))
    check("确认后状态为当前", d["artifacts"][0]["status"] == "已确认-当前", d["artifacts"][0]["status"])
    # 非 01–09b 卡的资产（如 07 音色）也必须能走通用登记确认
    p2 = Path(tempfile.mkdtemp(prefix="art-confirm2-"))
    (p2 / "00-项目管理").mkdir(parents=True)
    (p2 / "07-图像资产").mkdir()
    rel2 = "07-图像资产/音色参考-v001.md"
    (p2 / rel2).write_text("# voice\n", encoding="utf-8")
    key2 = "07 音色资产::N001—N008::"
    st2 = {"schema_version": "3.4", "artifacts": [{
        "kind": "07 音色资产", "scope": "N001—N008", "model": "",
        "path": rel2, "status": "草稿待确认", "depends_on": [], "affected_shots": [],
        "sha256": hashlib.sha256(b"# voice\n").hexdigest()}],
        "current": {key2: {"draft": rel2, "confirmed": None, "status": "草稿待确认"}}}
    (p2 / "00-项目管理/artifact-state.json").write_text(json.dumps(st2, ensure_ascii=False), encoding="utf-8")
    dst2 = p2 / ".agents/skills/test/ai-film-router/scripts"
    dst2.mkdir(parents=True)
    for f in src.glob("*.py"):
        shutil.copy(f, dst2 / f.name)
    r2 = flow.confirm_path(p2, rel2)
    d2 = json.loads((p2 / "00-项目管理/artifact-state.json").read_text(encoding="utf-8"))
    check("非流程资产可确认", r2.get("ok") and d2["artifacts"][0]["status"] == "已确认-当前",
          d2["artifacts"][0]["status"])
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
