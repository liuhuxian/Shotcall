#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出图/改图：图构造、资产提示词、绑定快照、任务落点。"""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("SHOTCALL_IMGJOBS_FILE", str(Path(tempfile.mkdtemp()) / "imgjobs.json"))

import assetsdb
import imagegen
import imgjobs

REAL = Path('/users/huxian/short_v/有谁共鸣2')
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    g = imagegen.build_graph("test", 1536, 864, 42, steps=6)
    check("T2I 图节点", "MiniMaxH3ImageConditioning" in str(g) and "KSampler" in str(g))
    ge = imagegen.build_graph("edit", 1536, 864, 42, ref_image="ref.png", steps=6)
    check("改图接 image_1", ge["5"]["inputs"].get("images.image_1") == ["41", 0], ge["5"]["inputs"])

    d = assetsdb.build(REAL)
    with_prompt = [a for a in d["assets"] if a.get("prompt")]
    check("资产提示词可解析", len(with_prompt) >= 20, len(with_prompt))
    check("C01-A 有提示词", bool(next((a for a in d["assets"] if a["id"] == "C01-A"), {}).get("prompt")))

    p = Path(tempfile.mkdtemp(prefix="img-test-"))
    (p / "07-图像资产/资产绑定").mkdir(parents=True)
    (p / "07-图像资产/候选图片/C01-A").mkdir(parents=True)
    img = p / "07-图像资产/候选图片/C01-A/x.png"
    img.write_bytes(b"fake-png")
    out = assetsdb.write_binding(p, {"id": "C01-A", "name": "陈铭", "type": "character",
                                     "binding": {"version": "v5", "scenes": "N001"}},
                                 "07-图像资产/候选图片/C01-A/x.png")
    check("绑定快照已新增", out.exists() and out.name.startswith("asset-bindings-r"), out.name)
    binds = assetsdb.parse_bindings(p)
    check("新绑定可解析", any(v.get("image", "").endswith("x.png") for v in binds.values()), binds)

    with mock.patch.object(imgjobs.sched, "submit") as sub:
        res = imgjobs.submit_batch(p, "EP002", [{"id": "C01-A", "name": "陈铭",
                                                "prompt": "p", "n": 1,
                                                "width": 1536, "height": 864,
                                                "steps": 6}], out_root="07-图像资产/候选图片")
    check("图片任务入队", len(res.get("jobs") or []) == 1, res)
    j = imgjobs._jobs[res["jobs"][0]]
    check("候选落 07", j.get("outRoot") == "07-图像资产/候选图片", j.get("outRoot"))
    check("sched 收到图片任务", sub.called)


    # 采用闭环：binding + project.json + 新 assets-vNNN
    p2 = Path(tempfile.mkdtemp(prefix="img-adopt-"))
    (p2 / "00-项目管理/工作台").mkdir(parents=True)
    (p2 / "07-图像资产/资产绑定").mkdir(parents=True)
    (p2 / "07-图像资产/候选图片/C01-A").mkdir(parents=True)
    (p2 / "07-图像资产/assets-v001.md").write_text(
        "# 图像资产\n\n状态：已确认-当前\n\n## 角色\n\n### C01-A｜陈铭\n\n- 图片状态：旧\n",
        encoding="utf-8")
    (p2 / "07-图像资产/候选图片/C01-A/y.png").write_bytes(b"fake2")
    (p2 / "00-项目管理/工作台/project.json").write_text(json.dumps({
        "revision": 10,
        "slots": [{"id": "C01--C01-A", "stateId": "C01-A", "label": "陈铭",
                   "bindingVersion": 5, "currentCandidateId": "old",
                   "preferredCandidateId": "old", "history": []}],
        "candidates": [], "bindingManifest": ""}, ensure_ascii=False), encoding="utf-8")
    ad = assetsdb.adopt_image(p2, "C01-A", "07-图像资产/候选图片/C01-A/y.png")
    check("采用写新 assets 版本", ad.get("assetDoc", "").endswith("assets-v002.md"), ad)
    pr = json.loads((p2 / "00-项目管理/工作台/project.json").read_text(encoding="utf-8"))
    check("project.json 绑定版本+1", pr["slots"][0]["bindingVersion"] == 6, pr["slots"][0])
    check("project.json 候选已登记", len(pr.get("candidates") or []) == 1, pr.get("candidates"))

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
