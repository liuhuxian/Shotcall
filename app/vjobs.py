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

import gpus
import imagegen
import sched
import store

_lock = __import__("threading").Lock()
_jobs = {}
_order = []
JOBS_FILE = Path(__file__).parent / "run" / "vjobs.json"


def _save():
    with _lock:
        data = {"order": list(_order), "jobs": {k: dict(v) for k, v in _jobs.items()}}
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(JOBS_FILE)
    except Exception:
        pass


def _load():
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    with _lock:
        _jobs.update({k: v for k, v in (data.get("jobs") or {}).items()})
        _order.extend(x for x in (data.get("order") or []) if x in _jobs)

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


ZH_FILE = Path(__file__).parent / "prompt_zh.json"


def load_zh() -> dict:
    try:
        return json.loads(ZH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def attach_zh(units: list, scene: str) -> list:
    """给每个单元挂中文审核稿；英文原文变了就标 zhStale。"""
    table = (load_zh() or {}).get(scene, {})
    for u in units:
        rec = table.get(u["id"])
        if not rec:
            u["zh"], u["zhStale"] = "", False
            continue
        cur = __import__("hashlib").sha1(u["prompt"].encode("utf-8")).hexdigest()
        u["zh"] = rec.get("zh", "")
        u["zhStale"] = rec.get("srcSha1") != cur
    return units


def latest_prompt_file(d: Path):
    """同一目录可能有 v001/v002…，取版本号最大的那份。"""
    files = list(d.glob("minimax-h3-ref2va*.md")) or list(d.glob("minimax-h3*.md")) or list(d.glob("*.md"))
    if not files:
        return None

    def ver(f: Path):
        m = re.search(r"-v(\d{3})(?:-\d+)?\.md$", f.name)
        return (int(m.group(1)) if m else 0, f.stat().st_mtime)

    return max(files, key=ver)


def parse_sync_refs(project: Path, scene: str) -> dict:
    """从视频提示词 md 的 yoko-shots 块里取每单元的参考图与资产 ID。"""
    d = project / VP_DIR / scene
    if not d.is_dir():
        return {}
    f = latest_prompt_file(d)
    if not f:
        return {}
    txt = f.read_text(encoding="utf-8")
    m = re.search(r"```yoko-shots\n(.*?)\n```", txt, re.S)
    if not m:
        return {}
    try:
        units = json.loads(m.group(1))
    except Exception:
        return {}
    out = {}
    for u in units:
        refs = []
        for i, path in enumerate(u.get("referenceImages") or [], 1):
            refs.append({"n": i, "file": path, "exists": (project / path).exists()})
        out[u.get("id")] = {"refs": refs, "assetIds": u.get("assetIds") or []}
    return out


def parse_units(project: Path, scene: str) -> list:
    """从视频提示词 md 里解析生成单元：U001 / 时长 / 六段提示词正文。"""
    d = project / VP_DIR / scene
    if not d.is_dir():
        return []
    f = latest_prompt_file(d)
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
    sync = parse_sync_refs(project, scene)
    for u in out:
        s = sync.get(u["id"]) or {}
        u["refs"] = s.get("refs") or []
        u["assetIds"] = [a.split("--")[-1] for a in (s.get("assetIds") or [])]
    return attach_zh(out, scene)


def resume(project: Path) -> list:
    """进程重启后，把"还在排队、从没进过 ComfyUI"的任务重新交给调度。"""
    todo = []
    with _lock:
        for jid in _order:
            j = _jobs.get(jid) or {}
            if j.get("status") == "queued" and not j.get("resubmitted"):
                _jobs[jid]["resubmitted"] = True
                todo.append(dict(j))
    if not todo:
        return []
    units = {u["id"]: u for u in parse_units(project, todo[0].get("scene") or "N001")}
    tasks = []
    for j in todo:
        u = units.get(j.get("unit"))
        if not u:
            continue
        with _lock:
            _jobs[j["id"]].update({"status": "queued", "prompt": u["prompt"]})
        tasks.append({"id": j["id"], "run": _runner(project, j["id"])})
    _save()
    if tasks:
        sched.submit("video", tasks)
    return [t["id"] for t in tasks]


def reconcile(project: Path = None) -> int:
    """按**提示词全文**把任务对回 ComfyUI 队列里的 prompt_id（不靠顺序猜）。"""
    units = {}
    for sc in {j.get("scene") for j in _jobs.values() if j.get("scene")} or {"N001"}:
        if project is None:
            continue
        for u in parse_units(project, sc):
            units[u["prompt"].strip()] = (sc, u["id"])
    if not units:
        return 0
    n = 0
    for card in range(8):
        port = 8188 + card
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/queue", timeout=3) as r:
                q = json.loads(r.read().decode())
        except Exception:
            continue
        entries = (q.get("queue_running") or []) + (q.get("queue_pending") or [])
        for e in entries:
            try:
                pid = e[1]
                graph = e[2] or {}
                text = None
                for node in graph.values():
                    ins = (node or {}).get("inputs") or {}
                    if (node or {}).get("class_type") == "MiniMaxH3ReferenceToVideo" and isinstance(ins.get("prompt"), str):
                        text = ins["prompt"].strip()
                if text not in units:
                    continue
                sc, uid = units[text]
                running_ids = set(q.get("runningIds") or [])
                st = "running" if pid in running_ids else "waiting"
                with _lock:
                    used = {(_jobs.get(x) or {}).get("promptId") for x in _order}
                    if pid in used:          # 这个 pid 已经有任务认领了，别再重复分配
                        continue
                    matched = False
                    for jid in _order:
                        j = _jobs.get(jid) or {}
                        if (j.get("scene") == sc and j.get("unit") == uid
                                and j.get("status") in ("running", "queued", "dispatched")
                                and not j.get("promptId")):
                            _jobs[jid].update({"promptId": pid, "card": card, "status": st})
                            n += 1
                            matched = True
                            break
                    if not matched and not any((_jobs.get(x) or {}).get("promptId") == pid
                                               for x in _order):
                        # ComfyUI 里有、我们没登记的任务：直接认领，别让它隐形
                        jid = f"ADOPT-{pid[:8]}"
                        seed = None
                        for node in graph.values():
                            if (node or {}).get("class_type") == "RandomNoise":
                                seed = ((node or {}).get("inputs") or {}).get("noise_seed")
                        _jobs[jid] = {"id": jid, "batch": "adopted", "scene": sc, "unit": uid,
                                      "seq": None, "seconds": None, "frames": None,
                                      "seed": seed, "status": st, "card": card,
                                      "promptId": pid, "file": None, "error": None,
                                      "queuedAt": time.time(), "startedAt": None,
                                      "prompt": text, "source": "adopted"}
                        _order.append(jid)
                        n += 1
            except Exception:
                continue
    if n:
        _save()
    return n


def sweep() -> int:
    """清掉已经不存在的任务：既不在 ComfyUI 队列里，也没有产出。"""
    alive = set()
    hist_ids = set()
    dead = set()
    for card in range(8):
        port = 8188 + card
        try:
            q = gpus.queue_of(port)
            alive |= set(q.get("runningIds") or []) | set(q.get("pendingIds") or [])
        except Exception:
            pass
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/history?max_items=200", timeout=8) as r:
                hist = json.loads(r.read().decode())
            for hp, entry in (hist or {}).items():
                outs = [i for v in (entry.get("outputs") or {}).values()
                        for i in v.get("images", []) if i.get("filename", "").endswith(".mp4")]
                if outs:
                    hist_ids.add(hp)          # 有成片 → 交给 recover 认领
                else:
                    dead.add(hp)              # 有记录但没成片 → 中断或失败
        except Exception:
            pass
    n = 0
    with _lock:
        for jid in list(_order):
            j = _jobs.get(jid) or {}
            if j.get("status") not in ("running", "waiting", "queued", "dispatched", "starting"):
                continue
            pid = j.get("promptId")
            if pid:
                if pid in dead:
                    _jobs[jid]["status"] = "aborted"
                    _jobs[jid]["error"] = "执行被中断"
                    n += 1
                    continue
                if pid not in alive and pid not in hist_ids:
                    _jobs[jid]["status"] = "aborted"
                    _jobs[jid]["error"] = "已中断或丢失"
                    n += 1
            elif (time.time() - (j.get("queuedAt") or 0)) > 900:
                _jobs[jid]["status"] = "aborted"
                _jobs[jid]["error"] = "长时间未送达 ComfyUI"
                n += 1
    if n:
        _save()
    return n


def recover(project: Path) -> list:
    """从 ComfyUI 的 /history 里认领"没人认领"的产出（进程重启后的孤儿任务）。

    按提示词全文匹配单元，命中就下载并登记为 done。
    """
    units = {}
    for sc in {j.get("scene") for j in _jobs.values() if j.get("scene")} or {"N001"}:
        for u in parse_units(project, sc):
            units[u["prompt"].strip()] = (sc, u)
    if not units:
        return []
    known = {(j.get("scene"), j.get("unit"), j.get("seed")) for j in _jobs.values()}
    saved = []
    for card in range(8):
        port = 8188 + card
        try:
            hist = _api(port, "/history", timeout=20)
        except Exception:
            continue
        for pid, entry in (hist or {}).items():
            try:
                nodes = (entry.get("prompt") or [None, None, {}])[2] or {}
                text = seed = None
                for node in nodes.values():
                    ins = node.get("inputs") or {}
                    if node.get("class_type") == "MiniMaxH3ReferenceToVideo" and isinstance(ins.get("prompt"), str):
                        text = ins["prompt"].strip()
                    if node.get("class_type") == "RandomNoise":
                        seed = ins.get("noise_seed")
                if not text or text not in units:
                    continue
                sc, u = units[text]
                if (sc, u["id"], seed) in known:
                    continue
                items = [i for v in (entry.get("outputs") or {}).values() for i in v.get("images", [])]
                vids = [i for i in items if i.get("filename", "").endswith(".mp4")]
                if not vids:
                    continue
                v = vids[-1]
                q = urllib.parse.urlencode({"filename": v["filename"],
                                            "subfolder": v.get("subfolder", ""), "type": "output"})
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/view?{q}", timeout=600) as r:
                    data = r.read()
                out = project / RESULT_DIR / sc / u["id"] / f"{seed}.mp4"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                jid = f"RECOVER-{u['id']}-{seed}"
                with _lock:
                    _jobs[jid] = {"id": jid, "batch": "recovered", "scene": sc, "unit": u["id"],
                                  "seq": None, "seconds": u["seconds"], "frames": frames_of(u["seconds"]),
                                  "seed": seed, "status": "done", "card": card,
                                  "file": str(out.relative_to(project)), "source": "recovered",
                                  "queuedAt": time.time(), "startedAt": None, "prompt": text}
                    _order.append(jid)
                    known.add((sc, u["id"], seed))
                _save()
                saved.append(str(out.relative_to(project)))
            except Exception:
                continue
    return saved


def list_jobs(batch: str = None) -> list:
    with _lock:
        return [_jobs[j] for j in _order if not batch or _jobs[j]["batch"] == batch]


def build_graph(prompt: str, width: int, height: int, frames: int, seed: int, steps: int = 20,
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


def _stage_ref(port: int, path: Path, as_name: str = None) -> str:
    name = as_name or path.name
    boundary = "----vjob" + str(int(time.time() * 1000))
    body = (f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode()
            + b"Content-Type: application/octet-stream\r\n\r\n" + path.read_bytes() + b"\r\n"
            + f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
            + f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode()).get("name", name)


def _api(port, path, payload=None, timeout=120):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def submit(project: Path, scene: str, units: list, seed: int = None,
           width=1344, height=768, steps=20) -> dict:
    """units: [{"unit": {...}, "count": n}]；seed 给了就按 seed, seed+1, ... 递增。"""
    batch = time.strftime("VID-%Y%m%d-%H%M%S")
    tasks = []
    for item in units:
        u = item["unit"]
        count = max(1, int(item.get("count") or 1))
        for k in range(count):
            jid = f"{batch}-{u['id']}-{k+1}" if count > 1 else f"{batch}-{u['id']}"
            job = {"id": jid, "batch": batch, "scene": scene, "unit": u["id"],
                   "seq": k + 1, "seconds": u["seconds"], "frames": frames_of(u["seconds"]),
                   "width": width, "height": height, "steps": steps,
                   "seed": (seed + k) if seed is not None else random.randint(1, 2 ** 31 - 1),
                   "refs": [r["file"] for r in (u.get("refs") or []) if r.get("exists")],
                   "status": "queued", "card": None, "file": None, "error": None,
                   "queuedAt": time.time(), "startedAt": None, "prompt": u["prompt"]}
            with _lock:
                _jobs[jid] = job
                _order.append(jid)
            tasks.append({"id": jid, "run": _runner(project, jid)})
    _save()
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
        _save()
        _execute(project, jid, dict(_jobs[jid]))
    return run


def _execute(project: Path, jid: str, job: dict):
    try:
        ref_names = []
        for i, rel in enumerate(job.get("refs") or []):
            f = project / rel
            if not f.exists():
                raise RuntimeError(f"参考图缺失：{rel}")
            ref_names.append(_stage_ref(job["port"], f, as_name=f"u_{jid[-10:]}_{i}{f.suffix}"))
        with _lock:
            if jid in _jobs:
                _jobs[jid]["usedRefs"] = ref_names
        _save()
        g = build_graph(job["prompt"], job["width"], job["height"], job["frames"],
                        job["seed"], job["steps"], ref_images=ref_names)
        pid = _api(job["port"], "/prompt", {"prompt": g, "client_id": "shotcall-video"})["prompt_id"]
        with _lock:
            if jid in _jobs:
                _jobs[jid]["promptId"] = pid
        _save()                      # 立刻落盘：重启后还能认出这条任务在 ComfyUI 里的位置
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
                _save()
                return
        raise RuntimeError("轮询超时")
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid].update({"status": "failed", "error": str(e)})
        _save()


def cancel(ids: list) -> int:
    n = 0
    with _lock:
        for jid in ids:
            if _jobs.get(jid, {}).get("status") == "queued":
                _jobs[jid]["status"] = "cancelled"
                n += 1
    return n


_load()
