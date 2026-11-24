#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流程页：提示词/登记/确认（调用项目 artifact_guard.py）。"""
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
    data = flow.steps_payload(REAL, 'N001')
    check("11 个流程卡", len(data['steps']) == 11, len(data['steps']))
    s01 = data['steps'][0]
    check("01 已确认", s01['status'] == 'confirmed', s01['status'])
    check("提示词含项目根", str(REAL) in s01['prompt'], s01['prompt'][:80])
    check("09b 下一版 v012", data['steps'][-1]['nextVersion'] == 'v012',
          data['steps'][-1]['nextVersion'])

    src = next(REAL.glob('.agents/skills/*/ai-film-router/scripts/artifact_guard.py')).parent
    p = Path(tempfile.mkdtemp(prefix="flow-test-"))
    (p / "01-概念锚定").mkdir(parents=True)
    (p / "00-项目管理").mkdir(parents=True)
    (p / "01-概念锚定/synopsis-v001.md").write_text("# v1\n", encoding="utf-8")
    dst = p / ".agents/skills/test/ai-film-router/scripts"
    dst.mkdir(parents=True)
    for f in src.glob("*.py"):
        shutil.copy(f, dst / f.name)
    check("找到 guard 脚本（嵌套）", flow.guard_script(p) is not None)
    p2 = Path(tempfile.mkdtemp(prefix="flow-flat-"))
    flat = p2 / ".agents/skills/ai-film-router/scripts"
    flat.mkdir(parents=True)
    for f in src.glob("*.py"):
        shutil.copy(f, flat / f.name)
    check("找到 guard 脚本（扁平）", flow.guard_script(p2) is not None and
          flow.guard_script(p2).parent == flat, flow.guard_script(p2))

    r = flow.register(p, "01")
    check("登记 v001", r.get("ok") and r.get("path", "").endswith("synopsis-v001.md"), r)
    state = json.loads((p / "00-项目管理/artifact-state.json").read_text(encoding="utf-8"))
    check("状态为草稿", state["artifacts"][0]["status"] == "草稿待确认", state["artifacts"][0]["status"])
    c = flow.confirm(p, "01")
    check("确认 v001", c.get("ok"), c)
    state2 = json.loads((p / "00-项目管理/artifact-state.json").read_text(encoding="utf-8"))
    check("状态为已确认", state2["artifacts"][0]["status"] == "已确认-当前",
          state2["artifacts"][0]["status"])

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
