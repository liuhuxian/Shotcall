#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 派发：无命令时只落任务单；任务单可列回。"""
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import agentrunner

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  ← {detail}"))


def main():
    p = Path(tempfile.mkdtemp(prefix="agent-test-"))
    r = agentrunner.dispatch(p, "01", "N001")
    t = r["task"]
    check("任务单已生成", (p / t["promptFile"]).is_file(), t)
    check("未配置命令不自动执行", r["started"] is False, r)
    check("任务单含下一版路径", t["output"].endswith("synopsis-v001.md"), t)
    items = agentrunner.list_tasks(p)
    check("任务可列出", any(x["id"] == t["id"] for x in items), items)
    # 配置命令时真的启动并完成（用 sh 接收 stdin 验证）
    out = p / "agent-stdin.txt"
    with mock.patch.dict(os.environ, {"SHOTCALL_AGENT_CMD": '["sh","-c","cat > ' + str(out) + '"]'}):
        r2 = agentrunner.dispatch(p, "02", "N001")
        check("配置命令会启动", r2["started"] is True, r2)
        tid = r2["task"]["id"]
        for _ in range(20):
            task = next((x for x in agentrunner.list_tasks(p) if x["id"] == tid), {})
            if task.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        check("Agent 命令可用", task.get("status") == "done" and out.exists(),
              (task.get("status"), out.exists(), task.get("error")))
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
