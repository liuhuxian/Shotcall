#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成任务构造器（默认只做 dry-run，不烧卡）。

    python3 submit.py --shots SHOT-EP002-SC001-03A --candidates 3 --tier draft
    python3 submit.py --shots ... --execute --yes     # 真正提交（需显式二次确认）

它做三件事：
1. 用与工作台相同的解析结果，按分镜拼出干净的 H3 六段式提示词
2. 组装可在本机 ComfyUI 直接提交的 API 图（R2V）
3. 把任务记录写进 run/tasks.jsonl（状态=待执行），并打印预估耗时
"""
import argparse
import json
import time
import uuid
from pathlib import Path

import dataset
import preflight as pf
from planner import plan
from store import state_dir

PROJECT = Path.home() / "short_v/有谁共鸣2"
COMFY = "http://127.0.0.1:8188"

from planner import clean_prompt, build_graph, frames_of, plan

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", required=True, help="逗号分隔的镜头号")
    ap.add_argument("--candidates", type=int, default=3)
    ap.add_argument("--tier", choices=["draft", "final"], default="draft")
    ap.add_argument("--width", type=int, default=1344)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    shot_ids = [s.strip() for s in a.shots.split(",") if s.strip()]
    data = dataset.load(PROJECT, "EP002", with_images=False)["data"]
    by_id = {s["id"]: s for s in data["shots"]}

    pfres = pf.run({"kind": "video", "items": [{"shot": i} for i in shot_ids]}, data)
    if pfres["blockers"]:
        print("[x] 预检未通过，拒绝构造任务：")
        for b in pfres["blockers"]:
            print("   -", b)
        return 1

    tasks = plan(data, shot_ids, a.candidates, a.tier, a.width, a.height, a.seed)

    est = round(sum(t["params"]["frames"] for t in tasks) * 106 / 124 / 60, 1)
    out = state_dir(PROJECT) / "tasks.jsonl"
    with open(out, "a", encoding="utf-8", newline="") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    print(f"已构造 {len(tasks)} 条任务（{a.tier} · {a.width}×{a.height}）")
    print(f"  预估 GPU ≈ {est} 分钟（按实测 106s/124 帧 线性外推）")
    print(f"  任务记录 → {out}")
    print(f"  首条：{tasks[0]['id']} · {tasks[0]['shot']} · seed {tasks[0]['params']['seed']} · "
          f"{tasks[0]['params']['frames']} 帧")
    print(f"  提示词首段：{tasks[0]['prompt'].splitlines()[0][:90]}…")
    print(f"  参考图：{', '.join(Path(r).name for r in tasks[0]['refs'])}")

    if not a.execute:
        print("\n[dry-run] 未提交。确认后加 --execute --yes。")
        return 0
    if not a.yes:
        print("\n[x] --execute 需要再加 --yes 二次确认。")
        return 2

    import urllib.request
    n = 0
    for t in tasks:
        req = urllib.request.Request(
            f"{COMFY}/prompt",
            data=json.dumps({"prompt": t["graph"], "client_id": "director-workbench"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            pid = json.loads(r.read())["prompt_id"]
        n += 1
        print(f"  已提交 {t['id']} → prompt_id {pid}")
    print(f"共提交 {n} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
