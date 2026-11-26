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
    # 未显式配置时：本机默认 Agent 可用就自动执行（否则点「派给 Agent」毫无反应）
    auto_ok = agentrunner.AGENT_BY_ID["dsh"]["available"]
    check("未配置时按默认 Agent 自动执行", r["started"] is auto_ok, r)
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
    # 选择 DSH/模型：保存配置并生成模型 patch，不真正启动
    p3 = Path(tempfile.mkdtemp(prefix="agent-cfg-"))
    pub = agentrunner.save_config(p3, "dsh", "deepseek-chat", "deepseek-official")
    check("Agent 配置已保存", (pub.get("current") or {}).get("agent") == "dsh" and
          (pub.get("current") or {}).get("model") == "deepseek-chat", pub.get("current"))
    args, _ = agentrunner._command_for(p3, {"id": "AG-test"}, pub["current"])
    check("DSH 命令带模型 patch", "dsh" in args[0] and "--patch" in args, args)
    patch = Path(args[args.index("--patch") + 1])
    check("模型 patch 内容", patch.exists() and "model: deepseek-chat" in patch.read_text(encoding="utf-8"),
          patch.read_text(encoding="utf-8") if patch.exists() else None)
    # 命令成功后自动登记（只登记草稿，不确认）
    p4 = Path(tempfile.mkdtemp(prefix="agent-auto-"))
    (p4 / "01-概念锚定").mkdir(parents=True)
    with mock.patch.dict(os.environ, {"SHOTCALL_AGENT_CMD":
                         '["sh","-c","cat > 01-概念锚定/synopsis-v001.md"]'}), \
         mock.patch.object(agentrunner.flow, "register", return_value={"ok": True, "path": "01-概念锚定/synopsis-v001.md"}) as reg:
        r4 = agentrunner.dispatch(p4, "01", "N001")
        tid = r4["task"]["id"]
        for _ in range(20):
            t4 = next((x for x in agentrunner.list_tasks(p4) if x["id"] == tid), {})
            if t4.get("registered"):
                break
            time.sleep(0.2)
        check("Agent 成功后自动登记", bool(t4.get("registered")) and reg.called, t4)
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
