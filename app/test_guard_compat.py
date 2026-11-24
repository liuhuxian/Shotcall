#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目 artifact_guard 对 ref2va 提示词的依赖兼容。"""
import importlib.util
import sys
from pathlib import Path

TEST_PROJECT = Path('/users/huxian/short_v/有谁共鸣2_2')
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    script = next(TEST_PROJECT.glob('.agents/skills/**/ai-film-router/scripts/artifact_guard.py'), None)
    if not script:
        check("找到测试项目 guard", False)
        return 1
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("ag", script)
    ag = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ag)
    base = {"status": "已确认-当前", "model": "", "scope": "N001"}
    shot = dict(base, kind="09 镜头规划", path="09-镜头与视频提示词/镜头规划/N001/shot-plan-v001.md")
    units = dict(base, kind="09 generation-units-minimax-h3",
                 path="09-镜头与视频提示词/镜头规划/N001/generation-units-minimax-h3-v001.md")
    data = {"artifacts": [shot, units]}
    rel = "09-镜头与视频提示词/视频提示词/N001/minimax-h3-ref2va-v012.md"
    try:
        ag.verify_dependency_contract(data, rel, "09 视频提示词", "N001", "", [shot["path"]])
        check("ref2va 可直接依赖镜头表", True)
    except Exception as e:
        check("ref2va 可直接依赖镜头表", False, e)
    try:
        ag.verify_dependency_contract(data, rel, "09 视频提示词", "N001", "", [units["path"]])
        check("ref2va 可依赖生成单元", True)
    except Exception as e:
        check("ref2va 可依赖生成单元", False, e)
    try:
        ag.verify_dependency_contract(data, rel, "09 视频提示词", "N001", "", [])
        check("ref2va 无依据被拒", False, "未抛错")
    except Exception:
        check("ref2va 无依据被拒", True)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
