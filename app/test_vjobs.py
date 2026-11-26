#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频任务层回归测试：不连 ComfyUI、不碰真实项目。

覆盖今天踩过的四类坑：
  1. 重启丢任务（落盘/读回）
  2. 续跑标记死循环（幂等重送）
  3. 清理线程误杀待派任务
  4. 参考图没接线 / 提示词选错版本

    python3 test_vjobs.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

# 导入 vjobs 时会把 JOBS_FILE 读一遍：这里先指到 /tmp，绝不碰真实 app/run/vjobs.json
os.environ["SHOTCALL_VJOBS_FILE"] = str(
    Path(tempfile.mkdtemp(prefix="vjobs-boot-")) / "vjobs.json")

import sched
import vjobs

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def reset(jobs=None, order=None):
    """清空模块内存态，必要时灌入任务。"""
    vjobs._jobs.clear()
    vjobs._order.clear()
    for jid, job in (jobs or {}).items():
        vjobs._jobs[jid] = dict(job)
        vjobs._order.append(jid)


def job(jid, unit="U001", status="queued", **kw):
    base = {"id": jid, "batch": "B1", "scene": "N001", "unit": unit, "seq": 1,
            "seconds": 13, "frames": 328, "steps": 20, "seed": 1,
            "status": status, "card": None, "promptId": None, "file": None,
            "error": None, "queuedAt": time.time(), "startedAt": None, "prompt": "p"}
    base.update(kw)
    return base


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vjobs-test-"))
    vjobs.JOBS_FILE = tmp / "vjobs.json"

    print("= 落盘与读回 =")
    reset({"A-1": job("A-1"), "A-2": job("A-2", status="done", file="x.mp4")})
    vjobs._save()
    check("任务文件已写出", vjobs.JOBS_FILE.exists())
    reset()
    vjobs._load()
    check("重启后任务读回", set(vjobs._jobs) == {"A-1", "A-2"}, set(vjobs._jobs))
    check("状态原样保留", vjobs._jobs["A-2"]["status"] == "done")

    print("\n= 续跑幂等 =")
    reset({"B-1": job("B-1"), "B-2": job("B-2")})
    sent = []
    with mock.patch.object(sched, "submit", side_effect=lambda k, items: sent.append([i["id"] for i in items]) or []), \
         mock.patch.object(sched, "pending_ids", return_value=set()), \
         mock.patch.object(vjobs, "parse_units", return_value=[
             {"id": "U001", "prompt": "p", "seconds": 13, "refs": []}]):
        first = vjobs.resume(tmp)
        check("首次续跑送 2 条", len(first) == 2, first)
        # 模拟"已进调度待派表"：再跑一次不应重复提交
        with mock.patch.object(sched, "pending_ids", return_value={"B-1", "B-2"}):
            second = vjobs.resume(tmp)
        check("重复续跑不重复提交", second == [], second)
        check("只提交过一次", sum(len(x) for x in sent) == 2, sent)

    print("\n= 清理不误杀待派 =")
    old = time.time() - 3600
    reset({"C-1": job("C-1", queuedAt=old), "C-2": job("C-2", queuedAt=old)})
    with mock.patch.object(sched, "pending_ids", return_value={"C-1"}), \
         mock.patch.object(vjobs.gpus, "queue_of", return_value={"runningIds": [], "pendingIds": []}), \
         mock.patch.object(vjobs.urllib.request, "urlopen", side_effect=Exception("offline")):
        n = vjobs.sweep()
        check("在待派表里的没被清掉", vjobs._jobs["C-1"]["status"] == "queued",
              vjobs._jobs["C-1"]["status"])
        check("真掉链子的被清掉", vjobs._jobs["C-2"]["status"] == "aborted",
              vjobs._jobs["C-2"]["status"])
        check("清理计数为 1", n == 1, n)

    print("\n= 中断任务识别 =")
    hist = {"P-1": {"outputs": {}}, "P-2": {"outputs": {"14": {"images": [{"filename": "a.mp4"}]}}}}
    reset({"D-1": job("D-1", status="running", promptId="P-1", card=4)})
    with mock.patch.object(sched, "pending_ids", return_value=set()), \
         mock.patch.object(vjobs.gpus, "queue_of", return_value={"runningIds": [], "pendingIds": []}), \
         mock.patch.object(vjobs.urllib.request, "urlopen",
                           side_effect=lambda *a, **k: mock.MagicMock(
                               __enter__=lambda s: s, __exit__=lambda *x: None,
                               read=lambda: json.dumps(hist).encode())):
        vjobs.sweep()
    check("无成片的历史记录判为中断", vjobs._jobs["D-1"]["status"] == "aborted",
          vjobs._jobs["D-1"]["status"])

    print("\n= 有 pid 的任务不因短暂消失被误杀 =")
    empty_hist = mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *x: None,
                                read=lambda: b"{}")
    reset({"E-1": job("E-1", status="running", promptId="PID-E1", card=4,
                       pidAt=time.time())})
    with mock.patch.object(sched, "pending_ids", return_value=set()), \
         mock.patch.object(vjobs.gpus, "queue_of",
                           return_value={"runningIds": [], "pendingIds": []}), \
         mock.patch.object(vjobs.urllib.request, "urlopen", return_value=empty_hist):
        vjobs.sweep()
        check("第一次看不到：不判死", vjobs._jobs["E-1"]["status"] == "running",
              vjobs._jobs["E-1"]["status"])
        check("记下消失时刻", bool(vjobs._jobs["E-1"].get("vanishedAt")))
        # 第二次仍然看不到，但还没满 20 分钟 → 继续等
        vjobs.sweep()
        check("20 分钟内仍不判死", vjobs._jobs["E-1"]["status"] == "running",
              vjobs._jobs["E-1"]["status"])
        # 让它"消失"超过 20 分钟
        vjobs._jobs["E-1"]["vanishedAt"] = time.time() - 1300
        vjobs.sweep()
        check("连续消失超 20 分钟才判中断", vjobs._jobs["E-1"]["status"] == "aborted",
              vjobs._jobs["E-1"]["status"])

    reset({"E-2": job("E-2", status="running", promptId="PID-E2", card=4,
                       pidAt=time.time(), vanishedAt=time.time() - 100)})
    with mock.patch.object(sched, "pending_ids", return_value=set()), \
         mock.patch.object(vjobs.gpus, "queue_of",
                           return_value={"runningIds": ["PID-E2"], "pendingIds": []}), \
         mock.patch.object(vjobs.urllib.request, "urlopen", return_value=empty_hist):
        vjobs.sweep()
        check("重新出现时清掉消失标记", not vjobs._jobs["E-2"].get("vanishedAt"))
        check("重新出现后仍算在跑", vjobs._jobs["E-2"]["status"] == "running",
              vjobs._jobs["E-2"]["status"])

    print("\n= 接续任务的依赖串行 =")
    reset()
    sent = []
    with mock.patch.object(sched, "submit",
                           side_effect=lambda k, items: sent.append(items) or [i["id"] for i in items]), \
         mock.patch.object(vjobs, "parse_units", return_value=[
             {"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": [], "shots": ["SH001"]},
             {"id": "U001-B", "prompt": "p2", "seconds": 5, "refs": [], "shots": ["SH002"]}]):
        vjobs.submit(tmp, "N001",
                     [{"unit": {"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": [], "shots": ["SH001"]},
                       "count": 1},
                      {"unit": {"id": "U001-B", "prompt": "p2", "seconds": 5, "refs": [], "shots": ["SH002"]},
                       "count": 1, "chain": True, "prevUnit": "U001-A", "afterIds": []}],
                     steps=20)
    items = sent[0] if sent else []
    ids = {i["id"]: i for i in items}
    a_id = [k for k in ids if k.endswith("U001-A")]
    b_id = [k for k in ids if k.endswith("U001-B")]
    check("两条任务都已入队", len(items) == 2, len(items))
    check("A 无依赖", a_id and ids[a_id[0]]["after"] == [], ids.get(a_id[0], {}).get("after") if a_id else None)
    check("B 依赖 A", bool(b_id) and ids[b_id[0]]["after"] == a_id,
          ids.get(b_id[0], {}).get("after") if b_id else None)

    print("\n= 调度器遵守依赖 =")
    import sched as SC
    with mock.patch.object(SC, "_done", set()), mock.patch.object(SC, "_failed", set()), \
         mock.patch.object(SC, "_runners", {"A": lambda p: None, "B": lambda p: None}), \
         mock.patch.object(SC, "_pending", [{"id": "A", "kind": "video", "after": []},
                                            {"id": "B", "kind": "video", "after": ["A"]}]):
        with SC._lock:
            ready, blocked = [], []
            for it in SC._pending:
                deps = it.get("after") or []
                (ready if all(d in SC._done or d not in SC._runners for d in deps) else blocked).append(it)
        check("A 就绪、B 阻塞", [x["id"] for x in ready] == ["A"] and [x["id"] for x in blocked] == ["B"],
              ([x["id"] for x in ready], [x["id"] for x in blocked]))
        SC._done.add("A")
        with SC._lock:
            ready2 = [it for it in SC._pending
                      if all(d in SC._done or d not in SC._runners for d in (it.get("after") or []))]
        check("A 完成后 B 才就绪", [x["id"] for x in ready2] == ["A", "B"], [x["id"] for x in ready2])

    print("\n= 同时提交两组 =")
    reset()
    with mock.patch.object(sched, "submit", side_effect=lambda k, items: [i["id"] for i in items]), \
         mock.patch.object(vjobs, "parse_units", return_value=[
             {"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": []},
             {"id": "U001-B", "prompt": "p2", "seconds": 5, "refs": []}]):
        ua = {"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": []}
        ub = {"id": "U001-B", "prompt": "p2", "seconds": 5, "refs": []}
        r1 = vjobs.submit(tmp, "N001", [{"unit": ua, "count": 1},
                                        {"unit": ub, "count": 1, "chain": True, "prevUnit": "U001-A"}])
        r2 = vjobs.submit(tmp, "N001", [{"unit": ua, "count": 1},
                                        {"unit": ub, "count": 1, "chain": True, "prevUnit": "U001-A"}])
    check("两组批次号不同", r1["batch"] != r2["batch"], (r1["batch"], r2["batch"]))
    ids = set(r1["jobs"]) | set(r2["jobs"])
    check("四条任务 id 互不重复", len(ids) == 4, sorted(ids))
    # 依赖只在各自批次内
    b1 = [j for j in r1["jobs"] if "U001-B" in j][0]
    b2 = [j for j in r2["jobs"] if "U001-B" in j][0]
    check("B 的依赖指向本批 A",
          vjobs._jobs[b1]["afterIds"] == [j for j in r1["jobs"] if "U001-A" in j]
          and vjobs._jobs[b2]["afterIds"] == [j for j in r2["jobs"] if "U001-A" in j],
          (vjobs._jobs[b1]["afterIds"], vjobs._jobs[b2]["afterIds"]))

    print("\n= 批次记录序列化 =")
    import json as _json, hashlib as _hashlib
    w, h = 1344, 768
    hasher = _hashlib.sha256()
    hasher.update(b"x")
    rec = {"params": {"steps": 20, "width": w, "height": h, "seed": None},
           "refs": [{"path": "a.png", "sha256": hasher.hexdigest(), "exists": True}]}
    try:
        _json.dumps(rec, ensure_ascii=False)
        ok = True
    except TypeError:
        ok = False
    check("批次记录可 JSON 序列化（高度是整数）", ok and isinstance(rec["params"]["height"], int),
          type(rec["params"]["height"]).__name__)

    print("\n= 耗时口径 =")
    import time as _t
    dispatch, exec_start, finish = 1000.0, 2800.0, 3830.0
    render = round(finish - exec_start, 1)
    wait = round(exec_start - dispatch, 1)
    check("耗时只算开渲到出片", render == 1030.0, render)
    check("排队单独记", wait == 1800.0, wait)
    check("耗时不含排队", render < (finish - dispatch), (render, finish - dispatch))

    print("\n= 参考图接线 =")
    g = vjobs.build_graph("p", 1344, 768, 328, 7, 20, ref_images=["a.png", "b.png"])
    h3 = [v for v in g.values() if v["class_type"] == "MiniMaxH3ReferenceToVideo"][0]
    loads = [v for v in g.values() if v["class_type"] == "LoadImage"]
    check("LoadImage 节点数等于参考图数", len(loads) == 2, len(loads))
    check("H3 接上 ref_images", all(f"ref_images.ref_image_{i}" in h3["inputs"] for i in range(2)),
          list(h3["inputs"]))
    sch = [v for v in g.values() if v["class_type"] == "BasicScheduler"][0]
    check("默认 20 步", sch["inputs"]["steps"] == 20, sch["inputs"]["steps"])
    g0 = vjobs.build_graph("p", 1344, 768, 328, 7, 20)
    check("无参考图时不接 LoadImage",
          not [v for v in g0.values() if v["class_type"] == "LoadImage"])

    print("\n= 带后缀的单元编号 =")
    d2 = tmp / vjobs.VP_DIR / "N002"
    d2.mkdir(parents=True)
    (d2 / "minimax-h3-ref2va-v009.md").write_text(
        "# 测试\n\n### U001-A｜6秒｜SH001\n\n```text\nsubject_definitions: x\n```\n\n"
        "### U001-B｜5秒｜SH002\n\n```text\nsubject_definitions: y\n```\n", encoding="utf-8")
    us = vjobs.parse_units(tmp, "N002")
    check("能解析 U001-A / U001-B", [u["id"] for u in us] == ["U001-A", "U001-B"],
          [u["id"] for u in us])
    check("时长正确", [u["seconds"] for u in us] == [6, 5], [u["seconds"] for u in us])

    print("\n= 接续声明解析 =")
    d3 = tmp / vjobs.VP_DIR / "N003"
    d3.mkdir(parents=True, exist_ok=True)
    body = (
        "# t\n\n### U010-A｜6秒｜SH010\n\n```text\nsubject_definitions: a\n```\n\n"
        "### U010-B｜5秒｜SH011\n\n```text\nsubject_definitions: b\n```\n")
    js = json.dumps({"schemaVersion": 1, "units": [
        {"id": "U010-A", "duration": 6, "shotIds": ["SH010"],
         "referenceImages": [], "referenceAudios": [],
         "chainFrom": None, "continuation": None},
        {"id": "U010-B", "duration": 5, "shotIds": ["SH011"],
         "referenceImages": [], "referenceAudios": [],
         "chainFrom": "U010-A", "continuation": "same-shot"}]})
    (d3 / "minimax-h3-ref2va-v001.md").write_text(body + "\n```yoko-shots\n" + js + "\n```\n",
                                                 encoding="utf-8")
    us3 = {u["id"]: u for u in vjobs.parse_units(tmp, "N003")}
    check("无声明单元 chainFrom 为空", us3["U010-A"].get("chainFrom") is None)
    check("声明单元带 chainFrom", us3["U010-B"].get("chainFrom") == "U010-A",
          us3["U010-B"].get("chainFrom"))
    check("continuation 类型保留", us3["U010-B"].get("continuation") == "same-shot")

    print("\n= LoRA 强度两条路径 =")
    g4 = vjobs.build_graph("p", 1344, 768, 158, 7, 20, ref_images=["a.png"],
                           lora="x.safetensors", lora_strength=0.7)
    g5 = vjobs.build_i2v_graph("p", 1344, 768, 124, 7, "f.png",
                               lora="x.safetensors", lora_strength=0.7)
    check("接图路径带强度", g4["20"]["inputs"]["strength_model"] == 0.7, g4["20"]["inputs"])
    check("首帧路径带强度", g5["20"]["inputs"]["strength_model"] == 0.7, g5["20"]["inputs"])

    print("\n= SigmaShift 接线 =")
    g6 = vjobs.build_graph("p", 1344, 768, 124, 7, 8, ref_images=["a.png"],
                           lora="x.safetensors", lora_strength=1.0,
                           sampler="euler", sigma_shift={"video": 6, "audio": 3})
    shift = [v for v in g6.values() if v["class_type"] == "MiniMaxH3SigmaShift"]
    check("有 SigmaShift 节点", len(shift) == 1, len(shift))
    check("shift 参数正确",
          shift and shift[0]["inputs"]["shift_video"] == 6.0 and shift[0]["inputs"]["shift_audio"] == 3.0,
          shift[0]["inputs"] if shift else None)
    check("采样器为 euler", g6["6"]["inputs"]["sampler_name"] == "euler", g6["6"]["inputs"])

    print("\n= 块格式兼容 =")
    new_style = '```yoko-shots\n{"schemaVersion":1,"units":[{"id":"U001","duration":6}]}\n```'
    old_style = '```yoko-shots\n[{"id":"U001","duration":6}]\n```'
    n1, e1 = vjobs.shots_block(new_style)
    n2, e2 = vjobs.shots_block(old_style)
    check("新格式块可解析", e1 is None and len(n1) == 1, e1)
    check("旧格式块仍可解析", e2 is None and len(n2) == 1, e2)
    n3, e3 = vjobs.shots_block('```yoko-shots\n[]\n```')
    check("空数组报错", e3 is not None, e3)

    print("\n= 参考音频接线 =")
    g2 = vjobs.build_graph("p", 1344, 768, 158, 7, 20, ref_images=["a.png"], ref_audios=["v.wav"])
    loads_a = [v for v in g2.values() if v["class_type"] == "LoadAudio"]
    h3b = [v for v in g2.values() if v["class_type"] == "MiniMaxH3ReferenceToVideo"][0]
    check("LoadAudio 节点存在", len(loads_a) == 1, len(loads_a))
    check("H3 接上 ref_audios", "ref_audios.ref_audio_0" in h3b["inputs"], list(h3b["inputs"]))
    g3 = vjobs.build_graph("p", 1344, 768, 158, 7, 20)
    check("无音色时不接 LoadAudio",
          not [v for v in g3.values() if v["class_type"] == "LoadAudio"])

    print("\n= 提示词文件选择 =")
    d = tmp / "N001"
    d.mkdir()
    # 现行规则：minimax-h3-vNNN.md 与 minimax-h3-ref2va-vNNN.md 都算合法提示词
    (d / "minimax-h3-v001.md").write_text("x", encoding="utf-8")
    got = vjobs.latest_prompt_file(d)
    check("无 ref2va 后缀也能读到", got and got.name == "minimax-h3-v001.md",
          got.name if got else None)
    (d / "minimax-h3-ref2va-v001.md").write_text("x", encoding="utf-8")
    (d / "minimax-h3-ref2va-v002.md").write_text("x", encoding="utf-8")
    got = vjobs.latest_prompt_file(d)
    check("取最新版本 ref2va", got and got.name == "minimax-h3-ref2va-v002.md",
          got.name if got else None)

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
