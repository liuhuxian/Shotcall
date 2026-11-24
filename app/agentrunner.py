# -*- coding: utf-8 -*-
"""Agent 派发：把流程卡提示词落成任务单；可选调用本机已配置的 Agent 命令。

默认只生成任务单（不猜 Agent CLI）；配置后才真正执行。
配置优先级：
  1. 环境变量 SHOTCALL_AGENT_CMD（JSON 数组或字符串）
  2. <project>/00-项目管理/工作台/agent.json
     {"command":["claude","-p"], "cwd":"<project>", "timeout":1800}
命令参数可用 {prompt_file} 占位；没有占位时提示词从 stdin 传入。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

import flow
import store

_lock = threading.Lock()


def _dir(project: Path) -> Path:
    d = store.state_dir(project) / "agent-tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config(project: Path) -> dict:
    env = os.environ.get("SHOTCALL_AGENT_CMD")
    if env:
        try:
            v = json.loads(env)
        except Exception:
            v = shlex.split(env)
        return {"command": v if isinstance(v, list) else shlex.split(str(v)), "mode": "stdin"}
    f = project / "00-项目管理/工作台/agent.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("command"):
                return d
        except Exception:
            pass
    return {}


def list_tasks(project: Path, limit: int = 50) -> list:
    out = []
    for f in sorted(_dir(project).glob("AG-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def _update(project: Path, task: dict):
    p = _dir(project) / (task["id"] + ".json")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _run(project: Path, task: dict, prompt: str, cfg: dict):
    cmd = cfg.get("command") or []
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if not cmd:
        return
    prompt_file = task["promptFile"]
    args = [str(x).replace("{prompt_file}", prompt_file) for x in cmd]
    use_stdin = not any("{prompt_file}" in str(x) for x in cmd)
    log = _dir(project) / (task["id"] + ".log")
    task.update({"status": "running", "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _update(project, task)
    try:
        with open(log, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(args, cwd=str(cfg.get("cwd") or project), stdin=subprocess.PIPE,
                                 stdout=lf, stderr=subprocess.STDOUT, text=True)
            if use_stdin:
                p.communicate(prompt, timeout=float(cfg.get("timeout") or 1800))
            else:
                p.wait(timeout=float(cfg.get("timeout") or 1800))
        task.update({"status": "done" if p.returncode == 0 else "failed",
                     "returncode": p.returncode,
                     "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception as e:
        task.update({"status": "failed", "error": str(e)[-400:],
                     "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _update(project, task)


def dispatch(project: Path, step_key: str, scene: str = "N001") -> dict:
    data = flow.steps_payload(project, scene)
    st = next((x for x in data.get("steps") or [] if x["key"] == step_key), None)
    if not st:
        raise ValueError("未知流程")
    tid = "AG-" + time.strftime("%Y%m%d-%H%M%S") + "-" + step_key + "-" + uuid.uuid4().hex[:4]
    d = _dir(project)
    md = d / (tid + ".md")
    prompt = st["prompt"] + "\n\n完成后把成果写到：" + st["nextPath"] + "\n不要覆盖旧版；写完后回导演台点「登记成果」。\n"
    md.write_text(prompt, encoding="utf-8")
    task = {"id": tid, "step": step_key, "scene": scene, "status": "queued",
            "promptFile": str(md.relative_to(project)) if str(md).startswith(str(project)) else str(md),
            "output": st["nextPath"], "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    cfg = config(project)
    task["command"] = cfg.get("command") or []
    _update(project, task)
    started = False
    if cfg.get("command"):
        threading.Thread(target=_run, args=(project, task, prompt, cfg), daemon=True).start()
        started = True
    return {"ok": True, "task": task, "started": started,
            "config": bool(cfg.get("command"))}
