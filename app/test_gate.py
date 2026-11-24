#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过期成果硬门禁：视频/首帧/出图不能引用已过期登记。"""
import json
import tempfile
from pathlib import Path

import server

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    p = Path(tempfile.mkdtemp(prefix="gate-test-"))
    (p / "00-项目管理").mkdir(parents=True)
    rel = "09-镜头与视频提示词/视频提示词/N001/minimax-h3-ref2va-v001.md"
    (p / rel).parent.mkdir(parents=True)
    (p / rel).write_text("# prompt\n", encoding="utf-8")
    state = {"schema_version": "3.4", "artifacts": [
        {"kind": "09 视频提示词", "path": rel, "status": "已过期-禁止使用",
         "sha256": "0" * 64, "scope": "N001", "depends_on": []}], "current": {}}
    (p / "00-项目管理/artifact-state.json").write_text(json.dumps(state), encoding="utf-8")
    old = server.PROJECT
    server.PROJECT = p
    try:
        units = [{"unit": {"id": "U001", "source": "minimax-h3-ref2va-v001.md",
                           "shots": "SH001", "refs": []}}]
        g = server._generation_gate("N001", units, rel)
        check("视频门禁拦截过期提示词", any("成果已过期" in x for x in g["missing"]), g)
        check("门禁整体不通过", not g["ok"], g)
        units2 = [{"unit": {"id": "U002", "source": "minimax-h3-ref2va-v001.md",
                            "shots": "SH001", "refs": [], "assetIds": ["L99-A"]}}]
        g2 = server._generation_gate("N001", units2, rel)
        check("资产未通过会拦截", any("资产L99-A" in x for x in g2["missing"]), g2)
    finally:
        server.PROJECT = old
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
