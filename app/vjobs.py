# -*- coding: utf-8 -*-
"""视频生成任务：按 09 的生成单元提交 H3 R2V，产物落到 生成结果/<场>/<单元>/。

与出图任务一样，只产任务，不选卡；卡由 sched 分配。
"""
import json
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import imagegen
import sched
import store

_lock = __import__("threading").Lock()
_jobs = {}
_order = []

PLAN_DIR = "09-镜头与视频提示词/镜头规划"
VP_DIR = "09-镜头与视频提示词/视频提示词"
RESULT_DIR = "09-镜头与视频提示词/生成结果"

MODELS = dict(
    unet="minimax_h3_ref2va_pruned_bf16.safetensors",
    clip="qwen3vl_32b_minimax_h3_bf16.safetensors",
    vae="minimax_h3_video_vae_fp16.safetensors",
    audio_vae="minimax_h3_audio_vae_fp32.safetensors",
)


def frames_of(sec: float) -> int:
    n = max(5, round(sec * 24))
    return n + (5 - n % 17) % 17


def parse_units(project: Path, scene: str) -> list:
    """从视频提示词 md 里解析生成单元：U001 / 时长 / 六段提示词正文。"""
    d = project / VP_DIR / scene
    if not d.is_dir():
        return []
    f = next(iter(sorted(d.glob("minimax-h3-ref2va*.md"))), None) or next(iter(sorted(d.glob("*.md"))), None)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"^### (U\d{3})｜(\d+)秒｜([^\n]+)\n(.*?)(?=^### |\Z)", txt, re.S | re.M):
        body = m.group(4)
        pm = re.search(r"```text\n(.*?)```", body, re.S)
        out.append({
            "id": m.group(1), "seconds": int(m.group(2)),
            "shots": m.group(3).strip(),
            "prompt": (pm.group(1).strip() if pm else ""),
            "source": f.name,
        })
    return out


def list_jobs(batch: str = None) -> list:
    with _lock:
        return [_jobs[j] for j in _order if not batch or _jobs[j]["batch"] == batch]


def build_graph(prompt: str, width: int, height: int, frames: int, seed: int, steps: int = 8,
                ref_images: list = None) -> dict:
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt, "width": width, "height": height,
            "length": frames, "ref_image_size": "match"}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "7": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple",
                                                         "steps": steps, "denoise": 1.0}},
        "8": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["8", 0], "sampler": ["6", 0],
            "sigmas": ["7", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": "gen/unit",
                                                     "format": "auto", "codec": "auto"}},
    }
    for i, name in enumerate(ref_images or []):
        nid = str(40 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["5"]["inputs"][f"ref_images.ref_image_{i}"] = [nid, 0]
    return g


def _stage_ref(port: int, path: Path) -> str:
    boundary = "----vjob" + str(int(time.time() * 1000))
    body = (f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode()
            + b"Content-Type: application/octet-stream\r\n\r\n" + path.read_bytes() + b"\r\n"
            + f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
            + f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode()).get("name", path.name)


def _api(port, path, payload=None, timeout=120):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def submit(project: Path, scene: str, units: list, width=1344, height=768, steps=8) -> dict:
    batch = time.strftime("VID-%Y%m%d-%H%M%S")
    tasks = []
    for u in units:
        jid = f"{batch}-{u['id']}"
        job = {"id": jid, "batch": batch, "scene": scene, "unit": u["id"],
               "seconds": u["seconds"], "frames": frames_of(u["seconds"]),
               "width": width, "height": height, "steps": steps,
               "seed": random.randint(1, 2 ** 31 - 1),
               "status": "queued", "card": None, "file": None, "error": None,
               "queuedAt": time.time(), "startedAt": None, "prompt": u["prompt"]}
        with _lock:
            _jobs[jid] = job
            _order.append(jid)
        tasks.append({"id": jid, "run": _runner(project, jid)})
    sched.submit("video", tasks)
    return {"batch": batch, "jobs": [t["id"] for t in tasks]}


def _runner(project: Path, jid: str):
    def run(port: int):
        with _lock:
            job = dict(_jobs.get(jid) or {})
            if job.get("status") == "cancelled":
                return
            _jobs[jid].update({"status": "running", "startedAt": time.time(),
                               "port": port, "card": port - 8188})
        _execute(project, jid, dict(_jobs[jid]))
    return run


def _execute(project: Path, jid: str, job: dict):
    try:
        g = build_graph(job["prompt"], job["width"], job["height"], job["frames"],
                        job["seed"], job["steps"])
        pid = _api(job["port"], "/prompt", {"prompt": g, "client_id": "shotcall-video"})["prompt_id"]
        t0 = time.time()
        while time.time() - t0 < 7200:
            time.sleep(10)
            h = _api(job["port"], f"/history/{pid}", timeout=30)
            if pid in h:
                if not h[pid].get("status", {}).get("completed"):
                    raise RuntimeError("生成失败")
                items = [i for v in h[pid].get("outputs", {}).values() for i in v.get("images", [])]
                vids = [i for i in items if i.get("filename", "").endswith(".mp4")]
                if not vids:
                    raise RuntimeError("无视频输出")
                v = vids[-1]
                q = urllib.parse.urlencode({"filename": v["filename"],
                                            "subfolder": v.get("subfolder", ""), "type": "output"})
                with urllib.request.urlopen(f"http://127.0.0.1:{job['port']}/view?{q}", timeout=600) as r:
                    data = r.read()
                out = project / RESULT_DIR / job["scene"] / job["unit"] / f"{job['seed']}.mp4"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                with _lock:
                    _jobs[jid].update({"status": "done", "file": str(out.relative_to(project)),
                                       "seconds": round(time.time() - t0, 1)})
                return
        raise RuntimeError("轮询超时")
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid].update({"status": "failed", "error": str(e)})


def cancel(ids: list) -> int:
    n = 0
    with _lock:
        for jid in ids:
            if _jobs.get(jid, {}).get("status") == "queued":
                _jobs[jid]["status"] = "cancelled"
                n += 1
    return n
