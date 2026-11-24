#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜头候选聚合与 EDL/CSV 导出（只读）。"""
from pathlib import Path

import picks

REAL = Path('/users/huxian/short_v/有谁共鸣2')
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    d = picks.candidates(REAL, 'N001')
    check("6 镜候选墙", len(d.get('shots') or []) == 6, len(d.get('shots') or []))
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
