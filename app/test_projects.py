#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多项目：发现与非法切换保护。"""
from pathlib import Path

import os
import tempfile
from unittest import mock

import projects
import store

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    found = {x["id"]: x for x in projects.discover()}
    check("发现正式项目", "有谁共鸣2" in found, list(found))
    check("发现测试项目 _2", "有谁共鸣2_2" in found, list(found))
    check("项目路径是目录", all(Path(x["path"]).is_dir() for x in found.values()))
    try:
        projects.set_active("/definitely/not/a/project")
        check("非法路径被拒", False, "未抛错")
    except Exception:
        check("非法路径被拒", True)

    # 切换项目时，状态目录不能串到启动项目
    other = Path(tempfile.mkdtemp(prefix="other-proj-"))
    # 初始化新项目（临时根，不写真实 ~/short_v）
    tmp_root = Path(tempfile.mkdtemp(prefix="proj-root-"))
    with mock.patch.object(projects, "ROOT", tmp_root):
        made = projects.create("测试新项目")
        check("初始化目录", (tmp_root / "测试新项目/00-项目管理").is_dir() and
              (tmp_root / "测试新项目/07-图像资产/候选图片").is_dir(), made)
        check("初始化后可发现", any(x["id"] == "测试新项目" for x in projects.discover()))
        check("初始化不伪造登记", not (tmp_root / "测试新项目/00-项目管理/artifact-state.json").exists())

    p2 = Path(found["有谁共鸣2_2"]["path"])
    with mock.patch.dict(os.environ, {"WORKBENCH_PROJECT": str(p2),
                                      "WORKBENCH_STATE": "/tmp/pinned-state"}):
        store.ACTIVE_PROJECT = None
        check("启动项目用显式 state", str(store.state_dir(p2)) == "/tmp/pinned-state",
              store.state_dir(p2))
        store.ACTIVE_PROJECT = other
        check("切换后回项目 state", store.state_dir(other) == other / "制作运行",
              store.state_dir(other))
        check("切换后旧项目不串 state", store.state_dir(p2) == p2 / "制作运行",
              store.state_dir(p2))
        store.ACTIVE_PROJECT = None
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
