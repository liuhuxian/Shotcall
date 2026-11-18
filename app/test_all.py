#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台回归测试：不需要 GPU、不写项目文件。

    python3 test_all.py          # 全部
    python3 test_all.py --fast   # 跳过需要连 ComfyUI 的检查

当前项目状态下 EP002 目录为空（旧内容已删除），内容类断言会自动跳过，
只跑空态、大纲、落账与接口部分。等在 EP002 重新生成文档后，
内容类断言会自动生效。
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

os.environ.setdefault("WORKBENCH_STATE", tempfile.mkdtemp(prefix="wb-test-state-"))

import dataset
import planner
import preflight
import store

PROJECT = Path.home() / "short_v/有谁共鸣2"
API = "http://127.0.0.1:8790"

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  – {name}（跳过：{why}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    a = ap.parse_args()

    data = dataset.load(PROJECT, "EP002", with_images=False)["data"]
    has_content = not data.get("empty")

    print("= 大纲 =")
    eps = data["episodes"]
    check("分集大纲 50 集", len(eps) == 50, len(eps))
    tg = [e.get("target") for e in eps if e.get("target")]
    check("单集目标在 120–360 秒", tg and all(120 <= x <= 360 for x in tg), sorted(set(tg)))
    check("集号连续", [e["id"] for e in eps] == [f"EP{i:03d}" for i in range(1, len(eps) + 1)])
    total = sum(e.get("target") or 0 for e in eps)
    check("总时长落在合理区间", 3000 <= total <= 20000, total)
    check("集名不重复", len({e["title"] for e in eps}) == len(eps))

    print("\n= 集目录 =")
    check("空态标记正确", data.get("empty") is (not (PROJECT / "剧集/EP002/剧本.md").exists()),
          data.get("empty"))
    if has_content:
        s = data["stats"]
        check("剧本台词 139 句", s["dialogue"] == 139, s["dialogue"])
        check("参考图零断链", s["brokenOcc"] == 0, s["brokenOcc"])
        check("台词 2332 字", s["dialogueChars"] == 2332, s["dialogueChars"])
    else:
        skip("内容类断言", "剧集/EP002 尚无剧本")

    print("\n= 工作流 =")
    import server
    dec, cands = store.latest(PROJECT), server.list_candidates()
    wf = server.workflow_of("EP001", dec, cands)
    check("步骤含 19 步", len(wf["steps"]) == 19, len(wf["steps"]))
    check("挂上 performance-direction",
          any("performance-direction" in (s.get("skill") or "") for s in wf["steps"]))
    check("挂上 production-design-direction",
          any("production-design-direction" in (s.get("skill") or "") for s in wf["steps"]))
    check("挂上 novel-analyzer",
          any(s.get("skill") == "novel-analyzer" for s in wf["steps"]))
    check("挂上 asset-continuity-management",
          any(s["name"] == "素材清单" for s in wf["steps"]))
    names = [s["name"] for s in wf["steps"]]
    check("四级审核齐全", all(f"审核{n} · " in " ".join(names) for n in ("一", "二", "三", "四")),
          [x for x in names if "审核" in x])

    print("\n= 预检与任务构造 =")
    if has_content:
        ok = preflight.run({"kind": "video", "items": [{"shot": data["shots"][0]["id"]}]}, data)
        check("预检可运行", "blockers" in ok)
        tasks = planner.plan(data, [data["shots"][0]["id"]], candidates=1, tier="draft")
        check("任务图为 18 节点", len(tasks[0]["graph"]) == 18, len(tasks[0]["graph"]))
    else:
        skip("预检与任务构造", "无镜头数据")

    print("\n= 落账 =")
    d1 = store.append(PROJECT, 1, {"type": "scene", "id": "EP002-SC001"}, "pass",
                      notes=[{"text": "测试 A"}])
    check("首次写入", d1["verdict"] == "pass" and "supersedes" not in d1)
    d2 = store.append(PROJECT, 1, {"type": "scene", "id": "EP002-SC001"}, "revise")
    check("改判产生 supersedes", d2.get("supersedes") == d1["id"])
    latest = store.latest(PROJECT)
    check("latest 只留 1 条", len([x for x in latest if x["unit"]["id"] == "EP002-SC001"]) == 1)
    sel = store.select(PROJECT, "SHOT-EP002-SC001-03A", "a.mp4", "adopt")
    check("选择记录写入",
          store.latest_selections(PROJECT)["SHOT-EP002-SC001-03A"]["id"] == sel["id"])
    try:
        store.append(PROJECT, 1, {"type": "scene", "id": "X"}, "nonsense")
        check("拒绝非法判定", False)
    except ValueError:
        check("拒绝非法判定", True)
    check("按集过滤生效",
          not any("EP001" in d["unit"]["id"] for d in store.latest(PROJECT)))

    print("\n= 服务接口 =")
    try:
        h = json.load(urllib.request.urlopen(f"{API}/api/health", timeout=10))
        check("/api/health", h.get("ok") is True)
        d = json.load(urllib.request.urlopen(f"{API}/api/data", timeout=60))
        for k in ("scenes", "shots", "decisions", "batches", "candidates", "stats", "workflow"):
            check(f"/api/data 含 {k}", k in d["data"])
        md = urllib.request.urlopen(f"{API}/api/export", timeout=20).read().decode()
        check("/api/export 可下载", "导演意见单" in md)
    except Exception as e:
        check("服务接口可访问", False, str(e))

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
