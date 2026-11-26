# -*- coding: utf-8 -*-
"""Agent 派发：流程卡提示词 → 任务单 → 可选本机 Agent 执行。

支持两种内置 Agent：
- dsh：`dsh --profile headless --patch <model.patch.yml> --approval-policy never -`
- codex：`codex exec --skip-git-repo-check --model <model> -`
模型通过每次任务生成的 DSH patch / Codex --model 指定。
默认不自动执行；用户在工作台保存 Agent/模型后才启动。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import flow
import store

_lock = threading.Lock()

AGENTS = [
    {"id": "dsh", "label": "DSH", "available": bool(shutil.which("dsh")),
     "models": ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"],
     "default_model": "deepseek-flash", "default_provider": "deepseek-official"},
    {"id": "codex", "label": "Codex", "available": bool(shutil.which("codex")),
     "models": ["gpt-5.1-codex", "gpt-5-codex", "gpt-5", "o4-mini"],
     "default_model": "gpt-5.1-codex", "default_provider": "openai"},
]
AGENT_BY_ID = {a["id"]: a for a in AGENTS}


def _dir(project: Path) -> Path:
    d = store.state_dir(project) / "agent-tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path(project: Path) -> Path:
    return project / "00-项目管理/工作台/agent.json"


def _default() -> dict:
    a = AGENT_BY_ID["dsh"]
    return {"agent": "dsh", "model": a["default_model"],
            "provider": a["default_provider"], "configured": False}


def load_config(project: Path) -> dict:
    env = os.environ.get("SHOTCALL_AGENT_CMD")
    if env:
        try:
            v = json.loads(env)
        except Exception:
            v = shlex.split(env)
        cmd = v if isinstance(v, list) else shlex.split(str(v))
        return {"agent": "custom", "model": "", "provider": "", "command": cmd,
                "configured": True, "source": "env"}
    cfg = _default()
    f = _config_path(project)
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                cfg.update({k: d[k] for k in ("agent", "model", "provider", "command")
                            if d.get(k)})
                cfg["configured"] = bool(d.get("command") or d.get("agent"))
                cfg["source"] = "project"
        except Exception:
            pass
    return cfg


def save_config(project: Path, agent: str, model: str, provider: str = "") -> dict:
    if agent not in AGENT_BY_ID:
        raise ValueError("未知 Agent；只支持 dsh / codex")
    a = AGENT_BY_ID[agent]
    model = (model or "").strip() or a["default_model"]
    provider = (provider or "").strip() or a["default_provider"]
    f = _config_path(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if f.exists():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    data = dict(old)
    data.update({"agent": agent, "model": model, "provider": provider,
                 "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    data.pop("command", None)  # 选择内置 Agent 时清掉旧的自定义命令
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    return public_config(project)


def public_config(project: Path) -> dict:
    cfg = load_config(project)
    return {"current": cfg, "agents": AGENTS,
            "configPath": str(_config_path(project)),
            "command": cfg.get("command") or []}


def _model_patch(project: Path, task: dict, cfg: dict) -> Path:
    d = _dir(project)
    f = d / (task["id"] + ".model.patch.yml")
    provider = cfg.get("provider") or "deepseek-official"
    model = cfg.get("model") or "deepseek-flash"
    f.write_text(
        "- id: agent-default-model\n"
        "  config:\n"
        f"    provider: {provider}\n"
        f"    model: {model}\n", encoding="utf-8")
    return f


def _command_for(project: Path, task: dict, cfg: dict):
    """返回 (args, use_stdin)；默认命令按内置模板生成。"""
    if cfg.get("command"):
        return [str(x) for x in cfg["command"]], True
    agent = cfg.get("agent") or "dsh"
    model = cfg.get("model") or ""
    if agent == "dsh":
        patch = _model_patch(project, task, cfg)
        args = ["dsh", "--profile", "headless", "--patch", str(patch),
                "--approval-policy", "never", "-"]
        return args, True
    if agent == "codex":
        args = ["codex", "exec", "--skip-git-repo-check"]
        if model:
            args += ["--model", model]
        args += ["-"]
        return args, True
    return [], True


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
    args, use_stdin = _command_for(project, task, cfg)
    if not args:
        return
    task.update({"status": "running", "agent": cfg.get("agent"), "model": cfg.get("model"),
                 "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _update(project, task)
    log = _dir(project) / (task["id"] + ".log")
    try:
        with open(log, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(args, cwd=str(project), stdin=subprocess.PIPE,
                                 stdout=lf, stderr=subprocess.STDOUT, text=True)
            if use_stdin:
                p.communicate(prompt, timeout=float(cfg.get("timeout") or 1800))
            else:
                p.wait(timeout=float(cfg.get("timeout") or 1800))
        task.update({"status": "done" if p.returncode == 0 else "failed",
                     "returncode": p.returncode,
                     "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        # 自动登记：只登记，不确认；导演仍要在流程页点确认
        if p.returncode == 0:
            step = flow.BY_KEY[task["step"]]
            scene = task.get("scene") or "N001" if step["scope"] == "{scene}" else None
            try:
                files = flow._new_files(project, step, scene)
            except Exception as e:
                files = []
                task["registerWarning"] = "列新文件失败：" + str(e)[-300:]
            done, failed = [], []
            for rel in files:
                try:
                    flow.register(project, task["step"], task.get("scene") or "N001", path=rel)
                    done.append(rel)
                except Exception as e:
                    failed.append({"path": rel, "error": str(e)[-300:]})
            if done:
                task["registered"] = done
            if failed:
                task["registerFailed"] = failed
            if not done and not failed:
                task["registerWarning"] = "没有发现新的 vNNN 文件，可能被写到了别的路径"
    except Exception as e:
        task.update({"status": "failed", "error": str(e)[-400:],
                     "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _update(project, task)


def dispatch(project: Path, step_key: str, scene: str = "N001") -> dict:
    data = flow.steps_payload(project, scene)
    st = next((x for x in data.get("steps") or [] if x["key"] == step_key), None)
    if not st:
        raise ValueError("未知流程")
    if st.get("blocked"):
        raise ValueError("上游还没完成，不能派单：" + "；".join(st["blocked"]))
    cfg = load_config(project)
    if not cfg.get("configured"):
        # 没有显式配置时：默认 Agent 的本机命令存在就直接跑，避免点了「派给 Agent」毫无反应
        # 测试或自动化场景可用 SHOTCALL_AGENT_AUTORUN=0 关掉自动执行，避免测试真的拉起模型
        auto = AGENT_BY_ID.get(cfg.get("agent") or "dsh") or {}
        allow_auto = os.environ.get("SHOTCALL_AGENT_AUTORUN", "1") not in ("0", "false", "no")
        if auto.get("available") and allow_auto:
            cfg = dict(cfg)
            cfg["configured"] = True
            cfg["source"] = "auto"
            cfg["model"] = cfg.get("model") or auto.get("default_model") or ""
    tid = "AG-" + time.strftime("%Y%m%d-%H%M%S") + "-" + step_key + "-" + uuid.uuid4().hex[:4]
    d = _dir(project)
    md = d / (tid + ".md")
    prompt = (st["prompt"]
              + "\n\n完成后把成果写到：" + st["nextPath"]
              + "\n不要覆盖旧版。写完后在最后逐行列出本次产出的全部文件相对路径，"
                "每行以「文件：」开头，方便导演台核对；不需要自己去登记。\n")
    md.write_text(prompt, encoding="utf-8")
    task = {"id": tid, "step": step_key, "scene": scene, "status": "queued",
            "promptFile": str(md.relative_to(project)) if str(md).startswith(str(project)) else str(md),
            "output": st["nextPath"], "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": cfg.get("agent"), "model": cfg.get("model"),
            "command": cfg.get("command") or []}
    _update(project, task)
    started = False
    reason = ""
    if cfg.get("configured"):
        args, _ = _command_for(project, task, cfg)
        if args and shutil.which(args[0]):
            threading.Thread(target=_run, args=(project, task, prompt, cfg), daemon=True).start()
            started = True
        elif args:
            task.update({"status": "unavailable", "error": f"本机找不到命令：{args[0]}"})
            _update(project, task)
            reason = task["error"]
    return {"ok": True, "task": task, "started": started,
            "config": cfg, "reason": reason}
