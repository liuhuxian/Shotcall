#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 审查问题回归：P0-01 / P1-01 / P1-02 / P1-05。

    python3 app/test_review_fixes.py
"""
import json
import os
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("SHOTCALL_NO_BACKGROUND", "1")
os.environ.setdefault("SHOTCALL_VJOBS_FILE", str(Path(tempfile.mkdtemp()) / "vjobs.json"))

import approvals
import sched
import server
import vjobs

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def test_p0_string_scope():
    print("= P0-01 真实 artifact-state 的 string scope =")
    p = Path(tempfile.mkdtemp(prefix="fix-scope-"))
    (p / "05").mkdir()
    (p / "05/test-v001.md").write_text("hello", encoding="utf-8")
    old = server.PROJECT
    server.PROJECT = p
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"path": "05/test-v001.md", "verdict": "pass",
                           "scope": "全项目", "stage": "05"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/artifact/confirm",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read().decode())
        rec = out.get("record") or {}
        check("string scope 确认返回 200", out.get("ok") is True, out)
        check("scope 归一为对象", rec.get("scope") == {"label": "全项目"}, rec.get("scope"))
    finally:
        srv.shutdown()
        server.PROJECT = old


def test_p1_resume_after():
    print("\n= P1-01 重启重排保留依赖边 ==")
    tmp = Path(tempfile.mkdtemp(prefix="fix-resume-"))
    vjobs.JOBS_FILE = tmp / "vjobs.json"
    with vjobs._lock:
        vjobs._jobs.clear(); vjobs._order.clear()
        vjobs._jobs.update({
            "J-A": {"id": "J-A", "batch": "B", "scene": "N001", "unit": "U001-A",
                    "status": "queued", "afterIds": [], "prompt": "p1", "seconds": 6},
            "J-B": {"id": "J-B", "batch": "B", "scene": "N001", "unit": "U001-B",
                    "status": "queued", "afterIds": ["J-A"], "prompt": "p2", "seconds": 5},
        })
        vjobs._order.extend(["J-A", "J-B"])
    sent = []
    units = [{"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": []},
             {"id": "U001-B", "prompt": "p2", "seconds": 5, "refs": []}]
    with mock.patch.object(vjobs, "parse_units", return_value=units), \
         mock.patch.object(sched, "pending_ids", return_value=set()), \
         mock.patch.object(sched, "submit", side_effect=lambda k, items: sent.append(items) or []):
        vjobs.resume(tmp)
    items = sent[0] if sent else []
    by = {x["id"]: x for x in items}
    check("A 无依赖", by.get("J-A", {}).get("after") == [], by.get("J-A"))
    check("B 保留 after=J-A", by.get("J-B", {}).get("after") == ["J-A"], by.get("J-B"))


def test_p1_file_target_path():
    print("\n= P1-02 file target/path 必须同一文件 ==")
    p = Path(tempfile.mkdtemp(prefix="fix-file-"))
    (p / "09-a").mkdir(); (p / "02-b").mkdir()
    (p / "09-a/prompt-v009.md").write_text("A", encoding="utf-8")
    (p / "02-b/bible.md").write_text("B", encoding="utf-8")
    rejected = False
    try:
        approvals.append(p, "file", "09-a/prompt-v009.md", "pass",
                         path="02-b/bible.md")
    except ValueError:
        rejected = True
    check("target/path 不一致被拒绝", rejected)
    approvals.append(p, "file", "09-a/prompt-v009.md", "pass",
                     path="09-a/prompt-v009.md")
    rec = approvals.file_ok(p, "09-a/prompt-v009.md")
    check("同一文件可通过", bool(rec) and rec.get("sha256") ==
          __import__("hashlib").sha256(b"A").hexdigest(), rec)


def test_p1_shot_sha():
    print("\n= P1-05 镜头审批绑定 09 文件 SHA ==")
    p = Path(tempfile.mkdtemp(prefix="fix-shot-"))
    (p / "09").mkdir(); f = p / "09/shot-plan-v001.md"
    f.write_text("plan1", encoding="utf-8")
    approvals.append(p, "shot", "N001/SH001", "pass", path="09/shot-plan-v001.md")
    st = approvals.state(p, "shot", "N001/SH001")
    check("未改动不 stale", st.get("stale") is False and bool(st.get("record")), st)
    f.write_text("plan2", encoding="utf-8")
    st2 = approvals.state(p, "shot", "N001/SH001")
    check("改 shot-plan 后 stale", st2.get("stale") is True, st2)


def test_p1_batch_before_submit():
    print("\n= P1-06 先落批次/授权，再入队 ==")
    p = Path(tempfile.mkdtemp(prefix="fix-batch-"))
    (p / "09-镜头与视频提示词/视频提示词/N001").mkdir(parents=True)
    (p / "09-镜头与视频提示词/视频提示词/N001/minimax-h3-ref2va-v009.md").write_text(
        "# t\n\n### U001-A｜6秒｜SH001\n\n```text\nsubject_definitions: x\n```\n",
        encoding="utf-8")
    rel = "09-镜头与视频提示词/视频提示词/N001/minimax-h3-ref2va-v009.md"
    approvals.append(p, "file", rel, "pass", path=rel)
    approvals.append(p, "shot", "N001/SH001", "pass")
    old = server.PROJECT
    server.PROJECT = p
    seen = []

    def fake_submit(project, scene, units, **kw):
        batch = kw.get("batch")
        seen.append(batch)
        f = server.store.state_dir(project) / "generation-batches" / f"{batch}.json"
        check("submit 前批次文件已存在", f.exists(), str(f))
        check("submit 前状态为 pending", json.loads(f.read_text()).get("status") == "pending")
        raise RuntimeError("模拟入队失败")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"scene": "N001", "units": [{"id": "U001-A", "count": 1}],
                           "steps": 20, "size": "1344x768"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/gen/submit",
            data=body, headers={"Content-Type": "application/json"})
        code = 0
        with mock.patch.object(server.vjobs, "submit", side_effect=fake_submit):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
        check("入队失败返回 400", code == 400, code)
        files = list((server.store.state_dir(p) / "generation-batches").glob("*.json"))
        check("批次文件标记 failed", bool(files) and json.loads(files[0].read_text()).get("status") == "failed",
              [f.name for f in files])
        check("没有产生任务", seen == [json.loads(files[0].read_text())["id"]] if files else False, seen)
    finally:
        srv.shutdown()
        server.PROJECT = old


def test_p1_save_lock():
    print("\n= P1-03 _save 持锁写盘（旧快照不会后写覆盖）=")
    tmp = Path(tempfile.mkdtemp(prefix="fix-save-"))
    vjobs.JOBS_FILE = tmp / "vjobs.json"
    with vjobs._lock:
        vjobs._jobs.clear(); vjobs._order.clear()
        vjobs._jobs["A"] = {"id": "A", "batch": "B", "status": "queued"}
        vjobs._order.append("A")
    started = threading.Event(); release = threading.Event()
    real_replace = vjobs.os.replace

    def slow_replace(src, dst):
        started.set()
        release.wait(3)
        return real_replace(src, dst)

    def writer():
        vjobs._save()

    import unittest.mock as _m
    with _m.patch.object(vjobs.os, "replace", side_effect=slow_replace):
        t = threading.Thread(target=writer); t.start()
        started.wait(2)
        held = vjobs._lock.acquire(timeout=0.2)
        if held:
            vjobs._lock.release()
        release.set(); t.join(3)
    check("写盘期间 _lock 被持有", not held)
    check("最终 JSON 可读", json.loads((tmp / "vjobs.json").read_text())["jobs"]["A"]["id"] == "A")


def test_p2_migration_idempotent():
    print("\n= P2-04 确认缓存迁移幂等 ==")
    import confirms
    p = Path(tempfile.mkdtemp(prefix="fix-mig-"))
    (p / "05").mkdir(); (p / "05/x-v001.md").write_text("x", encoding="utf-8")
    confirms.record(p, "05/x-v001.md", "confirm", "")
    old = server.PROJECT
    server.PROJECT = p
    try:
        n1 = server._migrate_confirms()
        n2 = server._migrate_confirms()
        check("首次迁移 1 条", n1 == 1, n1)
        check("再次迁移 0 条", n2 == 0, n2)
        check("账本可 file_ok", bool(approvals.file_ok(p, "05/x-v001.md")))
    finally:
        server.PROJECT = old


def main():
    test_p0_string_scope()
    test_p1_resume_after()
    test_p1_file_target_path()
    test_p1_shot_sha()
    test_p1_batch_before_submit()
    test_p1_save_lock()
    test_p2_migration_idempotent()
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for x in FAIL:
            print("  -", x)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
