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
import hashlib
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("WORKBENCH_STATE", tempfile.mkdtemp(prefix="wb-test-state-"))
os.environ.setdefault("SHOTCALL_AGENT_AUTORUN", "0")   # 测试不真的拉起模型
os.environ.setdefault("SHOTCALL_NO_BACKGROUND", "1")

import dataset
import planner
import preflight
import store

PROJECT = Path.home() / "short_v/有谁共鸣2"
API = ""

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  – {name}（跳过：{why}）")


def snap_docs():
    """M1 AC-09：创作文档只读；测试前后应完全一致。"""
    out = {}
    for base in ("02-角色圣经", "05-正式剧本", "07-图像资产",
                 "08-表演设计", "09-镜头与视频提示词"):
        d = PROJECT / base
        if not d.is_dir():
            continue
        for f in d.rglob("*.md"):
            try:
                out[str(f.relative_to(PROJECT))] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    a = ap.parse_args()

    snap0 = snap_docs()
    data = dataset.load(PROJECT, "EP002", with_images=False)["data"]
    has_content = not data.get("empty")

    print("= 阶段模型 =")
    import artifacts, stages
    st = artifacts.build(PROJECT)
    check("读得到成果登记表", st["exists"], st.get("registryPath"))
    check("阶段数为 10", len(st["stages"]) == 10, len(st["stages"]))
    check("阶段编号 00–09", [s["no"] for s in st["stages"]] == [f"{i:02d}" for i in range(10)])
    if st["exists"]:
        s = st["summary"]
        check("成果状态数守恒",
              s["total"] == s["draft"] + s["confirmed"] + s["superseded"] + s["conclusion"],
              s)
        check("每条成果都有阶段归属", all(a["stage"] for a in st["artifacts"]),
              [a["path"] for a in st["artifacts"] if not a["stage"]][:3])
        check("每条成果都核过文件", all(a["fileState"] in ("ok", "modified", "missing")
                                        for a in st["artifacts"]))
        check("依赖引用可解析",
              all(dep in {a["path"] for a in st["artifacts"]}
                  for a in st["artifacts"] for dep in a["dependsOn"]))
    else:
        skip("成果登记断言", "项目没有 artifact-state.json")

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
    global API
    import server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    API = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        h = json.load(urllib.request.urlopen(f"{API}/api/health", timeout=10))
        check("/api/health", h.get("ok") is True)
        d = json.load(urllib.request.urlopen(f"{API}/api/data", timeout=60))
        for k in ("scenes", "shots", "decisions", "batches", "candidates", "stats", "workflow"):
            check(f"/api/data 含 {k}", k in d["data"])
        ov = json.load(urllib.request.urlopen(f"{API}/api/overview", timeout=30))
        check("页头 8 场 / 6 镜", ov.get("scenes", {}).get("count") == 8 and ov.get("shots") == 6,
              (ov.get("scenes"), ov.get("shots")))
        _ast = ov.get("assets") or {}
        _list = json.load(urllib.request.urlopen(f"{API}/api/assets", timeout=30))
        _img = [a for a in (_list.get("assets") or []) if a.get("type") != "voice"]
        check("页头资产数＝出图清单条数", _ast.get("total") == len(_img),
              (_ast.get("total"), len(_img)))
        check("道具不出图", _ast.get("prop", 0) == 0, _ast)
        sg = json.load(urllib.request.urlopen(f"{API}/api/stages", timeout=30))
        check("/api/stages 返回 10 阶段", len(sg.get("stages", [])) == 10, len(sg.get("stages", [])))
        check("/api/stages 含成果", "artifacts" in sg and "summary" in sg)
        md = urllib.request.urlopen(f"{API}/api/export", timeout=20).read().decode()
        check("/api/export 可下载", "导演意见单" in md)
    except Exception as e:
        check("服务接口可访问", False, str(e))
    finally:
        srv.shutdown()

    print("\n= 视频任务层（test_vjobs.py）=")
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "test_vjobs.py")],
                       capture_output=True, text=True)
    tail = [ln for ln in r.stdout.splitlines() if ln.startswith("通过")]
    check("视频任务层测试通过", r.returncode == 0, (tail[-1] if tail else r.stdout[-200:]))

    for name in ("test_approvals.py", "test_jobs_m1.py", "test_gpu_sampler.py",
                 "test_review_fixes.py", "test_m2_reviews.py", "test_flow.py",
                 "test_image.py", "test_firstframe.py", "test_projects.py",
                 "test_revision.py", "test_gate.py", "test_guard_compat.py",
                 "test_artifact_confirm.py", "test_assets_ignore.py",
                 "test_picks.py", "test_agent.py", "test_m1_acceptance.py"):
        f = Path(__file__).parent / name
        if not f.exists():
            skip(name, "文件未提供")
            continue
        rr = subprocess.run([sys.executable, str(f)], capture_output=True, text=True)
        tail2 = [ln for ln in rr.stdout.splitlines() if ln.startswith("通过")]
        check(name + " 通过", rr.returncode == 0, (tail2[-1] if tail2 else (rr.stdout[-300:] + rr.stderr[-300:])))

    snap1 = snap_docs()
    check("创作文档零修改（AC-09）", snap1 == snap0,
          [k for k in sorted(set(snap0) | set(snap1)) if snap0.get(k) != snap1.get(k)][:3])

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
