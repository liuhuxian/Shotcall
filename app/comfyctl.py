# -*- coding: utf-8 -*-
"""按需拉起 / 关闭 ComfyUI 实例。

约定：端口 8188 + 卡号。卡 7 是默认常驻卡，随批次按需拉起，
但可以手动关闭；夜间（21:00–09:00）跑完不自动关，白天跑完自动关闭 0–6 卡。
"""
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

BASE_PORT = 8188
COMFY_DIR = Path(os.environ.get("COMFY_DIR", "/checkpoints/huxian/h3/ComfyUI"))
COMFY_PY = os.environ.get("COMFY_PY", "/checkpoints/huxian/h3/env/bin/python")
COMFY_LOG = Path(os.environ.get("COMFY_LOG_DIR", "/checkpoints/huxian/h3"))
ALWAYS_ON = 7

_CMD_TAIL = [
    "--extra-model-paths-config", "/checkpoints/huxian/h3/ComfyUI/extra_model_paths.yaml",
    "--output-directory", "/checkpoints/huxian/h3/output",
    "--input-directory", "/checkpoints/huxian/h3/input",
    "--temp-directory", "/checkpoints/huxian/h3/tmp/comfy",
    "--reserve-vram", "4", "--disable-smart-memory", "--disable-auto-launch",
]


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats", timeout=1.0):
            return True
    except Exception:
        return False


def _pids_on(port: int) -> list:
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            parts = [p for p in open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore").split("\x00") if p]
        except Exception:
            continue
        if len(parts) > 1 and parts[0].endswith("python") and parts[1] == "main.py":
            if "--port" in parts and parts[parts.index("--port") + 1] == str(port):
                out.append(int(pid))
    return out


def start(card: int, wait: float = 90.0) -> dict:
    port = BASE_PORT + card
    if _alive(port):
        return {"card": card, "port": port, "started": False, "ready": True}
    env = dict(os.environ)
    # 注意：ComfyUI 会用 --cuda-device 覆盖 CUDA_VISIBLE_DEVICES（main.py:101-102），
    # 所以卡号必须通过 --cuda-device 传，不能只靠环境变量。
    env.pop("CUDA_VISIBLE_DEVICES", None)
    log = open(COMFY_LOG / f"comfy-card{card}.log", "ab")
    subprocess.Popen(
        [COMFY_PY, "main.py", "--listen", "127.0.0.1", "--port", str(port),
         "--cuda-device", str(card), *_CMD_TAIL],
        cwd=str(COMFY_DIR), env=env, stdout=log, stderr=log, start_new_session=True,
    )
    t0 = time.time()
    while time.time() - t0 < wait:
        if _alive(port):
            return {"card": card, "port": port, "started": True, "ready": True,
                    "seconds": round(time.time() - t0, 1)}
        time.sleep(2)
    return {"card": card, "port": port, "started": True, "ready": False}


def stop(card: int) -> dict:
    port = BASE_PORT + card
    pids = _pids_on(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < 30:
        if not _alive(port):
            return {"card": card, "port": port, "stopped": True, "pids": pids}
        time.sleep(1)
    return {"card": card, "port": port, "stopped": False, "pids": pids}


def ensure(cards: list) -> list:
    """提交前把所有需要的卡拉起来（含卡 7）。"""
    return [start(c) for c in cards]


def shutdown_after_batch(cards: list, night: bool) -> list:
    """批次结束后按规则收尾：白天关掉本次用到的 0–6 卡，夜间保留。"""
    if night:
        return []
    return [stop(c) for c in cards if c != ALWAYS_ON]
