#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首帧：从镜头表/资产描述符拼提示词；候选采用；09b 取首帧。"""
import json
import tempfile
from pathlib import Path

import firstframe

REAL = Path('/users/huxian/short_v/有谁共鸣2')
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    d = firstframe.shots_payload(REAL, 'N001')
    check("6 镜首帧卡", len(d.get('shots') or []) == 6, len(d.get('shots') or []))
    s0 = d['shots'][0]
    check("提示词含首帧状态", "首帧状态" in s0['prompt'] and s0['firstFrame'][:4] in s0['prompt'])
    check("提示词含资产描述符", "四十三岁" in s0['prompt'], s0['prompt'][:120])
    check("参考图可选", len(s0['refs']) >= 1, s0['refs'])

    p = Path(tempfile.mkdtemp(prefix="ff-test-"))
    (p / "制作运行/首帧候选/N001/SH001").mkdir(parents=True)
    f = p / "制作运行/首帧候选/N001/SH001/x.png"
    f.write_bytes(b"fake")
    firstframe.store.state_dir = lambda project: project / "制作运行"
    rec = firstframe.adopt(p, "N001", "SH001", "制作运行/首帧候选/N001/SH001/x.png")
    check("采用记录", rec.get("shot") == "SH001" and rec.get("sha256"), rec)
    check("按单元取首帧", firstframe.first_for_unit(p, "N001", "SH001、SH002") == rec['file'],
          firstframe.first_for_unit(p, "N001", "SH001、SH002"))

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
