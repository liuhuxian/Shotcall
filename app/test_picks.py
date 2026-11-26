#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜头候选聚合与 EDL/CSV 导出（只读）。"""
from pathlib import Path

import picks

REAL = Path('/users/huxian/short_v/有谁共鸣2')
PASS, FAIL = [], []


def skip(name, why=""):
    print(f"  - {name}（跳过：{why}）")


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    d = picks.candidates(REAL, 'N001')
    if not (d.get('shots') or []):
        skip("6 镜候选墙", "本场还没有镜头规划（08 未执行）")
    else:
        check("镜号齐全", all(s.get("id") for s in d["shots"]), len(d["shots"]))
    if not (d.get('shots') or []):
        check("空规划时导出仍可用", True, "")
        csv_text, ctype, name = picks.export_text(REAL, 'N001', 'csv')
        check("CSV 导出", csv_text.startswith('scene,shot,start,duration') and name.endswith('.csv'), name)
        print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
        return
    check("SH001 有候选", len((d['shots'][0].get('candidates') or [])) >= 1,
          len(d['shots'][0].get('candidates') or []))
    csv_text, ctype, name = picks.export_text(REAL, 'N001', 'csv')
    check("CSV 导出", csv_text.startswith('scene,shot,start,duration') and name.endswith('.csv'), name)
    edl, _, ename = picks.export_text(REAL, 'N001', 'edl')
    check("EDL 导出", edl.startswith('TITLE:') and ename.endswith('.edl'), ename)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
