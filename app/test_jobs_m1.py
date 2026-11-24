#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 任务状态机 / 失败传播回归测试。

不连 ComfyUI、不启 ComfyUI、不碰真实 app/run/vjobs.json：
所有落盘都指向 /tmp 下的临时目录。

覆盖：
  1. runner 返回 False / 抛异常 → 失败；返回 None/True → 成功
  2. 依赖未完成时下游阻塞；依赖失败后下游 cancelled("dependency_failed") 且不运行
  3. 依赖成功 → 下游就绪
  4. reconcile：ComfyUI 排队 = dispatched+waiting；开渲 = running+running，并落盘
  5. mark_cancelled 状态 / 原因 / 落盘
  6. _save 原子写：JSON 可读、无 .tmp 残留
  7. resume 跳过失败依赖并连带取消下游

    python3 test_jobs_m1.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

# 必须在 import vjobs 之前设置：vjobs 导入时会走 _load()，不能读真实任务文件
_TMP = Path(tempfile.mkdtemp(prefix="jobs-m1-"))
os.environ["SHOTCALL_VJOBS_FILE"] = str(_TMP / "vjobs.json")

import sched as SC
import vjobs

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


# ---------------------------------------------------------------- helpers
def job(jid, status="queued", **kw):
    base = {"id": jid, "batch": "B1", "scene": "N001", "unit": "U001", "seq": 1,
            "seconds": 13, "frames": 328, "steps": 20, "seed": 1,
            "status": status, "card": None, "promptId": None, "file": None,
            "error": None, "queuedAt": time.time(), "startedAt": None,
            "prompt": "p", "afterIds": []}
    base.update(kw)
    return base


def reset_jobs(jobs=None):
    vjobs._jobs.clear()
    vjobs._order.clear()
    for jid, rec in (jobs or {}).items():
        vjobs._jobs[jid] = dict(rec)
        vjobs._order.append(jid)


def reset_sched():
    with SC._lock:
        SC._pending[:] = []
        SC._runners.clear()
        SC._done.clear()
        SC._failed.clear()
        SC._state["jobs"].clear()
        SC._state["assign"].clear()


def queue_resp(payload):
    """伪 ComfyUI /queue 响应（只需要 .read() 与上下文管理器）。"""
    return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *x: None,
                          read=lambda: json.dumps(payload).encode())


# ---------------------------------------------------------------- tests
def test_start_explicit():
    print("= 调度线程显式启动 =")
    seen = []
    fired = threading.Event()

    def fake_worker():
        seen.append("worker")
        fired.set()

    with mock.patch.object(SC, "_worker", fake_worker), \
         mock.patch.object(SC, "_housekeeping", lambda: seen.append("housekeeping")):
        SC._started = False
        first = SC.start()
        fired.wait(1.0)
        second = SC.start()
    check("首次 start() 真的启动线程", first is True and seen.count("worker") == 1, seen)
    check("再次 start() 幂等", second is False and seen.count("worker") == 1, seen)
    check("is_started() 反映状态", SC.is_started() is True)
    SC._started = False          # 别让后续测试误以为 worker 在跑


def test_runner_return_values():
    print("\n= runner 返回值记账 =")
    with mock.patch.object(SC.comfyctl, "_alive", return_value=True), \
         mock.patch.object(SC.traceback, "print_exc"):
        reset_sched()
        with SC._lock:
            SC._runners["F"] = lambda port: False
        SC._run_one({"id": "F", "kind": "test"}, 0)
        check("返回 False → 不在 _done", "F" not in SC._done)
        check("返回 False → 在 _failed", "F" in SC._failed)

        reset_sched()

        def boom(port):
            raise RuntimeError("boom")

        with SC._lock:
            SC._runners["E"] = boom
        SC._run_one({"id": "E", "kind": "test"}, 0)
        check("抛异常 → 不在 _done", "E" not in SC._done)
        check("抛异常 → 在 _failed", "E" in SC._failed)

        reset_sched()
        with SC._lock:
            SC._runners["N"] = lambda port: None
        SC._run_one({"id": "N", "kind": "test"}, 0)
        check("返回 None（旧 runner）→ 成功", "N" in SC._done and "N" not in SC._failed,
              (SC._done, SC._failed))

        reset_sched()
        with SC._lock:
            SC._runners["T"] = lambda port: True
        SC._run_one({"id": "T", "kind": "test"}, 0)
        check("返回 True → 成功", "T" in SC._done and "T" not in SC._failed,
              (SC._done, SC._failed))


def test_dependency_failure():
    print("\n= 依赖失败连带取消 =")
    reset_sched()
    cancelled, ran = [], []
    with SC._lock:
        SC._pending[:] = [
            {"id": "A", "kind": "test", "after": [], "on_cancel": None},
            {"id": "B", "kind": "test", "after": ["A"],
             "on_cancel": lambda reason: cancelled.append((reason, "B"))},
            {"id": "C", "kind": "test", "after": ["B"],
             "on_cancel": lambda reason: cancelled.append((reason, "C"))},
        ]
        SC._runners.update({"A": lambda port: False,
                            "B": lambda port: ran.append("B"),
                            "C": lambda port: ran.append("C")})

    ready = SC._collect_ready()
    check("A 未完成时 B/C 都阻塞", [x["id"] for x in ready] == ["A"],
          [x["id"] for x in ready])
    check("B/C 仍留在待派表", [p["id"] for p in SC._pending] == ["B", "C"],
          [p["id"] for p in SC._pending])

    with mock.patch.object(SC.comfyctl, "_alive", return_value=True), \
         mock.patch.object(SC.traceback, "print_exc"):
        SC._run_one(ready[0], 0)
    check("A 失败后不在 _done", "A" not in SC._done)
    check("A 失败后在 _failed", "A" in SC._failed)

    ready2 = SC._collect_ready()          # 本轮：B 依赖 A 失败 → 取消 B
    check("A 失败后 B 不再就绪", [x["id"] for x in ready2] == [],
          [x["id"] for x in ready2])
    check("B 收到 dependency_failed", ("dependency_failed", "B") in cancelled, cancelled)
    check("B 也被记入 _failed", "B" in SC._failed)

    ready3 = SC._collect_ready()          # 下一轮：C 依赖 B（已失败）→ 一并取消
    check("C 也收到 dependency_failed", ("dependency_failed", "C") in cancelled, cancelled)
    check("C 也被记入 _failed", "C" in SC._failed)
    check("B/C 都已移出待派表", [p["id"] for p in SC._pending] == [],
          [p["id"] for p in SC._pending])
    check("B/C 的 runner 从未被调用", ran == [], ran)
    check("没有任何下游被就绪", ready2 == [] and ready3 == [], (ready2, ready3))


def test_dependency_success():
    print("\n= 依赖成功才放行 =")
    reset_sched()
    with SC._lock:
        SC._pending[:] = [
            {"id": "A2", "kind": "test", "after": [], "on_cancel": None},
            {"id": "B2", "kind": "test", "after": ["A2"], "on_cancel": None},
        ]
        SC._runners.update({"A2": lambda port: True,
                            "B2": lambda port: True})
    ready = SC._collect_ready()
    check("先只放 A2", [x["id"] for x in ready] == ["A2"], [x["id"] for x in ready])
    with mock.patch.object(SC.comfyctl, "_alive", return_value=True):
        SC._run_one(ready[0], 0)
    check("A2 成功", "A2" in SC._done and "A2" not in SC._failed,
          (SC._done, SC._failed))
    ready2 = SC._collect_ready()
    check("A2 成功后 B2 就绪", [x["id"] for x in ready2] == ["B2"],
          [x["id"] for x in ready2])


def test_submit_wires_on_cancel():
    print("\n= submit 给每条任务接 on_cancel =")
    reset_jobs()
    captured = []
    unit = {"id": "U001-A", "prompt": "p1", "seconds": 6, "refs": []}
    with mock.patch.object(SC, "submit",
                           side_effect=lambda kind, items:
                               captured.extend(items) or [i["id"] for i in items]):
        vjobs.submit(_TMP, "N001", [{"unit": unit, "count": 1}])
    check("捕获 1 条任务", len(captured) == 1, len(captured))
    item = captured[0]
    check("带 callable on_cancel", callable(item.get("on_cancel")), item.get("on_cancel"))
    item["on_cancel"]("dependency_failed")
    rec = vjobs._jobs[item["id"]]
    check("回调只取消自己的任务", rec["status"] == "cancelled", rec["status"])
    check("回调写入原因", rec.get("cancelReason") == "dependency_failed",
          rec.get("cancelReason"))


def test_reconcile():
    print("\n= reconcile 状态与 ComfyUI 队列一致 =")
    reset_jobs({
        "WAIT": job("WAIT", status="dispatched", promptId="pid-wait"),
        "RUN": job("RUN", status="dispatched", promptId="pid-run"),
    })
    q = {"queue_running": [[0, "pid-run", {}, {}]],
         "queue_pending": [[0, "pid-wait", {}, {}]]}
    units = [{"id": "U001", "prompt": "p", "seconds": 13, "refs": []}]
    with mock.patch.object(vjobs, "parse_units", return_value=units), \
         mock.patch.object(vjobs.urllib.request, "urlopen", return_value=queue_resp(q)):
        vjobs.reconcile(_TMP)
    wait, run = vjobs._jobs["WAIT"], vjobs._jobs["RUN"]
    check("排队 pid → dispatched", wait["status"] == "dispatched", wait["status"])
    check("排队 pid → queueState=waiting", wait.get("queueState") == "waiting",
          wait.get("queueState"))
    check("运行 pid → running", run["status"] == "running", run["status"])
    check("运行 pid → queueState=running", run.get("queueState") == "running",
          run.get("queueState"))
    check("运行 pid 记 execStartedAt", bool(run.get("execStartedAt")))

    reset_jobs()                      # 清内存后从磁盘读回，验证真的落盘了
    vjobs._load()
    check("排队状态已落盘",
          vjobs._jobs["WAIT"]["status"] == "dispatched"
          and vjobs._jobs["WAIT"].get("queueState") == "waiting",
          vjobs._jobs.get("WAIT"))
    check("运行状态已落盘",
          vjobs._jobs["RUN"]["status"] == "running"
          and vjobs._jobs["RUN"].get("queueState") == "running",
          vjobs._jobs.get("RUN"))


def test_mark_cancelled():
    print("\n= mark_cancelled =")
    reset_jobs({"C-1": job("C-1", status="queued"),
                "C-2": job("C-2", status="queued")})
    n = vjobs.mark_cancelled(["C-1", "C-2"], "dependency_failed")
    check("返回取消条数", n == 2, n)
    for jid in ("C-1", "C-2"):
        rec = vjobs._jobs[jid]
        check(f"{jid} 状态 cancelled", rec["status"] == "cancelled", rec["status"])
        check(f"{jid} 记录原因", rec.get("cancelReason") == "dependency_failed",
              rec.get("cancelReason"))
        check(f"{jid} 记录时间", isinstance(rec.get("cancelledAt"), float),
              rec.get("cancelledAt"))
    reset_jobs()
    vjobs._load()
    check("取消原因已落盘",
          vjobs._jobs["C-1"].get("cancelReason") == "dependency_failed",
          vjobs._jobs.get("C-1"))


def test_atomic_save():
    print("\n= 原子落盘 =")
    reset_jobs({"S-1": job("S-1")})
    vjobs._save()
    raw = vjobs.JOBS_FILE.read_text(encoding="utf-8")
    data = json.loads(raw)
    check("落盘后 JSON 可读", "jobs" in data and "S-1" in data["jobs"],
          list(data)[:4])
    check("没有 .tmp 残留", list(_TMP.glob("vjobs.json.tmp*")) == [],
          list(_TMP.glob("vjobs.json.tmp*")))

    # 同进程多线程连续写，也必须次次是完整 JSON、且不留临时文件
    def hammer():
        for _ in range(15):
            vjobs._save()

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        json.loads(vjobs.JOBS_FILE.read_text(encoding="utf-8"))
        readable = True
    except Exception:
        readable = False
    check("并发写后 JSON 仍可读", readable)
    check("并发写后无 .tmp 残留", list(_TMP.glob("vjobs.json.tmp*")) == [],
          list(_TMP.glob("vjobs.json.tmp*")))


def test_resume_skips_failed_deps():
    print("\n= 重启续跑跳过失败依赖 =")
    reset_jobs({
        "RA": job("RA", status="failed"),
        "RB": job("RB", status="queued", afterIds=["RA"]),
        "RC": job("RC", status="queued", afterIds=["RB"]),
    })
    sent = []
    with mock.patch.object(SC, "pending_ids", return_value=set()), \
         mock.patch.object(SC, "failed_ids", return_value=set()), \
         mock.patch.object(SC, "mark_failed", return_value=[]), \
         mock.patch.object(SC, "submit",
                           side_effect=lambda kind, items:
                               sent.append(items) or [i["id"] for i in items]):
        out = vjobs.resume(_TMP)
    check("没有任务被重新提交", out == [] and sent == [], (out, sent))
    check("RB 被取消", vjobs._jobs["RB"]["status"] == "cancelled",
          vjobs._jobs["RB"]["status"])
    check("RB 原因是 dependency_failed",
          vjobs._jobs["RB"].get("cancelReason") == "dependency_failed",
          vjobs._jobs["RB"].get("cancelReason"))
    check("RC 被连带取消", vjobs._jobs["RC"]["status"] == "cancelled",
          vjobs._jobs["RC"]["status"])

    print("\n= failed_ids() 只读 =")
    with SC._lock:
        SC._failed.add("FX")
    got = SC.failed_ids()
    got.add("MUTATED")
    check("failed_ids 含失败任务", "FX" in SC.failed_ids())
    check("改返回值不影响内部状态", "MUTATED" not in SC.failed_ids())


def main():
    print(f"临时任务文件：{vjobs.JOBS_FILE}")
    test_start_explicit()
    test_runner_return_values()
    test_dependency_failure()
    test_dependency_success()
    test_submit_wires_on_cancel()
    test_reconcile()
    test_mark_cancelled()
    test_atomic_save()
    test_resume_skips_failed_deps()

    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
