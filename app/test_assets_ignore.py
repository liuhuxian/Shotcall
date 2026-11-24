#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产忽略：写 project.json.trashedSlots，可恢复。"""
import json
import tempfile
from pathlib import Path

import assetsdb

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    p = Path(tempfile.mkdtemp(prefix="ignore-test-"))
    (p / "00-项目管理/工作台").mkdir(parents=True)
    proj = {"revision": 10, "slots": [{"id": "P01--P01-A", "stateId": "P01-A",
                                       "label": "眼镜", "kind": "prop"}],
            "trashedSlots": []}
    (p / "00-项目管理/工作台/project.json").write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
    r = assetsdb.set_ignored(p, "P01-A", True)
    check("忽略写入", r.get("ignored") and r.get("trashed") == 1, r)
    d = json.loads((p / "00-项目管理/工作台/project.json").read_text(encoding="utf-8"))
    check("revision 递增", d["revision"] == 11, d["revision"])
    check("可解析忽略", bool(assetsdb.parse_ignored(p).get("P01-A")))
    r2 = assetsdb.set_ignored(p, "P01-A", False)
    check("取消忽略", r2.get("ignored") is False and len(assetsdb.parse_ignored(p)) == 0, r2)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
