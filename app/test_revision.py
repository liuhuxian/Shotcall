#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""退回 → 改新版 → 重新确认；下游自动标记过期后再改下游。"""
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


def status(project, path):
    d = json.loads((project / "00-项目管理/artifact-state.json").read_text(encoding="utf-8"))
    return next((a["status"] for a in d["artifacts"] if a["path"] == path), None)


def main():
    src = next(REAL.glob('.agents/skills/*/ai-film-router/scripts/artifact_guard.py')).parent
    p = Path(tempfile.mkdtemp(prefix="rev-test-"))
    for d in ("01-概念锚定", "02-角色圣经", "00-项目管理"):
        (p / d).mkdir(parents=True)
    dst = p / ".agents/skills/test/ai-film-router/scripts"
    dst.mkdir(parents=True)
    for f in src.glob("*.py"):
        shutil.copy(f, dst / f.name)
    (p / "01-概念锚定/synopsis-v001.md").write_text("# 01 v1\n", encoding="utf-8")
    (p / "02-角色圣经/characters-v001.md").write_text("# 02 v1\n", encoding="utf-8")

    flow.register(p, "01"); flow.confirm(p, "01")
    flow.register(p, "02"); flow.confirm(p, "02")
    check("基线两版已确认", status(p, "01-概念锚定/synopsis-v001.md") == "已确认-当前" and
          status(p, "02-角色圣经/characters-v001.md") == "已确认-当前")

    r = flow.return_step(p, "01", "主角目标不清，回 01 精修")
    check("退回记录已写", r.get("ok") and "主角目标" in r["record"]["note"], r)
    d = flow.steps_payload(p, "N001")
    s01 = next(x for x in d["steps"] if x["key"] == "01")
    check("流程卡显示退回", bool(s01.get("returned")), s01.get("returned"))

    (p / "01-概念锚定/synopsis-v002.md").write_text("# 01 v2\n", encoding="utf-8")
    flow.register(p, "01"); flow.confirm(p, "01")
    check("01 v002 已确认", status(p, "01-概念锚定/synopsis-v002.md") == "已确认-当前")
    check("01 v001 已替代", status(p, "01-概念锚定/synopsis-v001.md") == "历史已替代")
    check("02 自动过期", status(p, "02-角色圣经/characters-v001.md") == "已过期-禁止使用",
          status(p, "02-角色圣经/characters-v001.md"))

    (p / "02-角色圣经/characters-v002.md").write_text("# 02 v2\n", encoding="utf-8")
    flow.register(p, "02"); flow.confirm(p, "02")
    check("02 v002 已确认", status(p, "02-角色圣经/characters-v002.md") == "已确认-当前")
    check("02 v001 已过期留底", status(p, "02-角色圣经/characters-v001.md") == "已过期-禁止使用")
    check("01 v002 仍是当前", status(p, "01-概念锚定/synopsis-v002.md") == "已确认-当前")

    sk = flow.skip_step(p, "03", "测试跳过")
    check("可选阶段可跳过", sk.get("ok") and flow.steps_payload(p, "N001")["steps"][2].get("skipped"))
    try:
        flow.skip_step(p, "01")
        check("已有成果不可跳过", False, "未抛错")
    except Exception:
        check("已有成果不可跳过", True)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
