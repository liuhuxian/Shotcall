# -*- coding: utf-8 -*-
"""视频生成任务：按 09 的生成单元提交 H3 R2V，产物落到 生成结果/<场>/<单元>/。

与出图任务一样，只产任务，不选卡；卡由 sched 分配。
"""
import json
import random
import subprocess
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

RECIPE_FILE = Path(__file__).parent / "run" / "gen_recipe.json"

RECIPES = {
    "base20":   {"label": "原模型 20 步（验收基准）", "lora": None, "steps": 20},
    "base12":   {"label": "原模型 12 步（快测）",     "lora": None, "steps": 12},
    "turbo4":   {"label": "turbo_4step LoRA · 4 步",  "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "steps": 4},
    "turbo8":   {"label": "turbo_4step LoRA · 8 步",  "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "steps": 8},
    "progressive": {
        "label": "渐进采样 · er_sde · 15 段 sigma",
        "lora": None, "steps": 15,
        "sigmas": "1, 0.99, 0.975, 0.95, 0.915, 0.87, 0.81, 0.735, 0.645, 0.545, 0.435, 0.325, 0.22, 0.13, 0.055, 0",
        "sampler": "er_sde",
        "attention": {"backend": "pytorch attention", "selection": "sol-attn", "tau": 1.3,
                      "start_percent": 0.2, "end_percent": 1.0, "dense_blocks": "",
                      "min_tokens": 12288, "extra_tokens": 256,
                      "sink_conditioning": "exact_kv_and_rows", "verbose": True},
        "refSize": "max",
    },
}


def current_recipe() -> dict:
    try:
        saved = json.loads(RECIPE_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    name = saved.get("name") or "base20"
    if name in RECIPES:
        return {"name": name, **RECIPES[name]}
    if saved.get("steps"):                       # 从 ComfyUI 工作流导入的自定义配方
        return {"name": name, "label": saved.get("label") or name,
                "lora": saved.get("lora"), "steps": saved["steps"],
                "sigmas": saved.get("sigmas"), "sampler": saved.get("sampler"),
                "attention": saved.get("attention"),
                "res": saved.get("res"), "refSize": saved.get("refSize"),
                "source": saved.get("source")}
    return {"name": "base20", **RECIPES["base20"]}


def save_recipe(spec: dict) -> dict:
    RECIPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECIPE_FILE.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    return current_recipe()


def set_recipe(name: str) -> dict:
    if name not in RECIPES:
        raise ValueError("未知工作流")
    RECIPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECIPE_FILE.write_text(json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
    return current_recipe()


MODELS_FL2V = "minimax_h3_fl2va_pruned_bf16.safetensors"

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
PIN_FILE = Path(__file__).parent / "run" / "prompt_pins.json"


def load_pins() -> dict:
    try:
        return json.loads(PIN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_pin(scene: str, name) -> dict:
    pins = load_pins()
    if name:
        pins[scene] = name
    else:
        pins.pop(scene, None)
    PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIN_FILE.write_text(json.dumps(pins, ensure_ascii=False, indent=1), encoding="utf-8")
    return pins


def list_versions(d: Path) -> list:
    out = []
    for f in sorted(d.glob("minimax-h3-ref2va*.md")):
        m = re.search(r"-v(\d{3})(?:-\d+)?\.md$", f.name)
        out.append({"name": f.name, "version": int(m.group(1)) if m else 0,
                    "sizeKB": round(f.stat().st_size / 1024),
                    "mtime": int(f.stat().st_mtime)})
    return sorted(out, key=lambda x: x["version"])


def load_zh() -> dict:
    try:
        return json.loads(ZH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_zh(scene: str, unit_id: str, source: str, zh: str) -> dict:
    """把译文写进 prompt_zh.json，绑定原文 sha1。"""
    table = load_zh()
    table.setdefault(scene, {})[unit_id] = {
        "srcSha1": __import__("hashlib").sha1(source.encode("utf-8")).hexdigest(),
        "zh": zh.strip() + "\n",
    }
    ZH_FILE.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    return table[scene][unit_id]


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
    """默认取版本号最大的；若该场被锁定到某一版，则用锁定的那一版。"""
    pinned = (load_pins() or {}).get(d.name)
    if pinned and (d / pinned).exists():
        return d / pinned
    files = list(d.glob("minimax-h3-ref2va*.md"))   # 只认 Ref2VA 提示词，不做兜底
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
        out[u.get("id")] = {"refs": refs, "assetIds": u.get("assetIds") or [],
                            "storyboard": u.get("storyboardReference") or None}
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
    for m in re.finditer(r"^### (U\d{3}(?:-[A-Z])?)｜(\d+)秒｜([^\n]+)\n(.*?)(?=^### |\Z)", txt, re.S | re.M):
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
        sb = s.get("storyboard") or {}
        u["storyboard"] = ({
            "path": sb.get("path"),
            "pictureIndex": sb.get("pictureIndex"),
            "row": sb.get("row"),
            "panels": sb.get("panels") or [],
        } if sb.get("path") else None)
    return attach_zh(out, scene)


def resume(project: Path) -> list:
    """进程重启后，把"还在排队、从没进过 ComfyUI"的任务重新交给调度。"""
    todo = []
    with _lock:
        for jid in _order:
            j = _jobs.get(jid) or {}
            # 只要还停在"排队"且不在调度待派表里，就重新送一遍（防止重启丢任务）
            if j.get("status") == "queued" and jid not in sched.pending_ids():
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
            units[u["prompt"].strip()] = (sc, u)
    if not units:
        return 0
    n = 0
    changed = False
    # 老记录补规格与参考图
    with _lock:
        for j in _jobs.values():
            if j.get("status") in ("running", "waiting") and not j.get("frames"):
                hit = units.get((j.get("prompt") or "").strip())
                if hit:
                    j["seconds"] = hit[1].get("seconds")
                    j["frames"] = frames_of(hit[1].get("seconds") or 10)
                    j.setdefault("steps", 20)
    # 老记录补参考图（之前认领时没存）
    with _lock:
        for jid, j in _jobs.items():
            if j.get("status") in ("running", "waiting") and not j.get("refs"):
                hit = units.get((j.get("prompt") or "").strip())
                if hit:
                    j["refs"] = [r["file"] for r in (hit[1].get("refs") or []) if r.get("exists")]
                    n += 1
    for card in range(8):
        port = 8188 + card
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/queue", timeout=3) as r:
                q = json.loads(r.read().decode())
        except Exception:
            continue
        # 先记"真正开始渲染"的时间：只要出现在 running 里就记一次
        def _ids(rows):
            out = set()
            for x in rows or []:
                try:
                    if isinstance(x, (list, tuple)) and len(x) > 1:
                        out.add(x[1])
                except Exception:
                    pass
            return out
        run_now = _ids(q.get("queue_running"))
        pend_now = _ids(q.get("queue_pending"))
        if run_now or pend_now:
            with _lock:
                for jj in _jobs.values():
                    pid2 = jj.get("promptId")
                    if pid2 in run_now and jj.get("status") not in ("running", "done"):
                        jj["status"] = "running"; jj["error"] = None; changed = True
                    elif pid2 in pend_now and jj.get("status") not in ("waiting", "running", "done"):
                        jj["status"] = "waiting"; jj["error"] = None; changed = True
        if run_now:
            with _lock:
                for jj in _jobs.values():
                    if jj.get("promptId") in run_now and not jj.get("execStartedAt"):
                        jj["execStartedAt"] = time.time()
                        changed = True
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
                sc, unit_obj = units[text]
                uid = unit_obj["id"]
                running_ids = set(q.get("runningIds") or [])
                st = "running" if pid in running_ids else "waiting"
                if st == "running":
                    with _lock:
                        for jj in _jobs.values():
                            if jj.get("promptId") == pid and not jj.get("execStartedAt"):
                                jj["execStartedAt"] = time.time()
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
                            _jobs[jid].update({"promptId": pid, "card": card,
                                               "status": st, "pidAt": time.time()})
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
                                      "seq": None,
                                      "seconds": unit_obj.get("seconds"),
                                      "frames": frames_of(unit_obj.get("seconds") or 10),
                                      "steps": 20,
                                      "seed": seed, "status": st, "card": card,
                                      "promptId": pid, "pidAt": time.time(),
                                      "refs": [r["file"] for r in (unit_obj.get("refs") or [])
                                               if r.get("exists")],
                                      "file": None, "error": None,
                                      "queuedAt": time.time(), "startedAt": None,
                                      "prompt": text, "source": "adopted"}
                        _order.append(jid)
                        n += 1
            except Exception:
                continue
    if n or changed:
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
                # pid 是 ComfyUI 已经接收的凭证：只要还没在队列/历史里连续消失 20 分钟，就别判死
                if pid in alive or pid in hist_ids:
                    _jobs[jid].pop("vanishedAt", None)
                    continue
                if not _jobs[jid].get("vanishedAt"):
                    _jobs[jid]["vanishedAt"] = time.time()
                    continue
                gone = time.time() - _jobs[jid]["vanishedAt"]
                if gone > 1200:
                    _jobs[jid]["status"] = "aborted"
                    _jobs[jid]["error"] = "已中断或丢失"
                    n += 1
            elif (jid not in sched.pending_ids()
                  and (time.time() - (j.get("queuedAt") or 0)) > 900):
                # 还在本台待派表里的不算丢，只有真正掉链子的才清
                _jobs[jid]["status"] = "aborted"
                _jobs[jid]["error"] = "长时间未送达 ComfyUI"
                n += 1
    if n:
        _save()
    return n


def _save_video(project: Path, port: int, item: dict, scene: str, unit: str, seed) -> Path:
    q = urllib.parse.urlencode({"filename": item["filename"],
                                "subfolder": item.get("subfolder", ""), "type": "output"})
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/view?{q}", timeout=900) as r:
        data = r.read()
    out = project / RESULT_DIR / scene / unit / f"{seed}.mp4"
    k = 2
    while out.exists():
        out = project / RESULT_DIR / scene / unit / f"{seed}_{k}.mp4"
        k += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def recover(project: Path) -> list:
    """把 ComfyUI 历史里的成片收回项目。

    两种来源：
      1. 我们自己派出去的任务（有 promptId）→ 补文件、标完成
      2. 完全没人认领的（重启后遗留）→ 按提示词匹配单元，建 RECOVER 记录
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
                outs = [i for v in (entry.get("outputs") or {}).values()
                        for i in v.get("images", []) if i.get("filename", "").endswith(".mp4")]
                if not outs:
                    continue
                nodes = (entry.get("prompt") or [None, None, {}])[2] or {}
                text, seed = None, None
                for node in nodes.values():
                    ins = node.get("inputs") or {}
                    if node.get("class_type") == "MiniMaxH3ReferenceToVideo" and isinstance(ins.get("prompt"), str):
                        text = ins["prompt"].strip()
                    if node.get("class_type") == "RandomNoise":
                        seed = ins.get("noise_seed")

                # ① 已登记的任务：按 promptId 命中
                owner = next((j for j in _jobs.values() if j.get("promptId") == pid), None)
                if owner is not None:
                    if owner.get("file"):
                        continue
                    scene = owner.get("scene") or "N001"
                    unit = owner.get("unit") or "X"
                    f = _save_video(project, port, outs[-1], scene, unit, owner.get("seed") or seed)
                    with _lock:
                        fin = time.time()
                        owner.update({"status": "done", "file": str(f.relative_to(project)),
                                      "finishedAt": fin})
                        if owner.get("execStartedAt"):
                            owner["renderSec"] = round(fin - owner["execStartedAt"], 1)
                        known.add((scene, unit, owner.get("seed") or seed))
                    _save()
                    saved.append(str(f.relative_to(project)))
                    continue

                # ② 无名产物：按提示词匹配单元
                if not text or text not in units:
                    continue
                sc, unit_obj = units[text]
                uid = unit_obj["id"]
                if (sc, uid, seed) in known:
                    continue
                f = _save_video(project, port, outs[-1], sc, uid, seed)
                jid = f"RECOVER-{uid}-{seed}"
                with _lock:
                    _jobs[jid] = {"id": jid, "batch": "recovered", "scene": sc, "unit": uid,
                                  "seq": None, "seconds": unit_obj["seconds"],
                                  "frames": frames_of(unit_obj["seconds"]),
                                  "seed": seed, "status": "done", "card": card,
                                  "file": str(f.relative_to(project)), "source": "recovered",
                                  "queuedAt": time.time(), "startedAt": None, "prompt": text,
                                  "refs": [r["file"] for r in (unit_obj.get("refs") or []) if r.get("exists")]}
                    _order.append(jid)
                    known.add((sc, uid, seed))
                _save()
                saved.append(str(f.relative_to(project)))
            except Exception as e:
                print(f"[recover] 卡{card} {pid[:8]} 失败：{type(e).__name__} {e}", flush=True)
                continue
    return saved


def mark_deleted(rel: str) -> int:
    """文件被删后，把对应任务记录标成 deleted，避免历史里指向空文件。"""
    n = 0
    with _lock:
        for j in _jobs.values():
            if j.get("file") == rel:
                j["status"] = "deleted"
                j["deletedAt"] = time.time()
                n += 1
    if n:
        _save()
    return n


def list_jobs(batch: str = None) -> list:
    with _lock:
        return [_jobs[j] for j in _order if not batch or _jobs[j]["batch"] == batch]


def build_graph(prompt: str, width: int, height: int, frames: int, seed: int, steps: int = 20,
                ref_images: list = None, lora: str = None, sigmas: str = None,
                sampler: str = None, attention: dict = None, ref_size: str = "match") -> dict:
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
            "prompt": prompt, "width": width, "height": height,
            "length": frames, "ref_image_size": ref_size}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler or "res_multistep"}},
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
    model_src = ["1", 0]
    if lora:
        # 加速 LoRA：接在 UNET 之后，模型输入改指向 LoRA 输出
        g["20"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": lora, "strength_model": 1.0}}
        model_src = ["20", 0]
    if attention:
        g["21"] = {"class_type": "ModelAttentionBackend",
                   "inputs": {"model": model_src, "attention": attention.get("backend", "pytorch attention")}}
        g["22"] = {"class_type": "BlockSparseAttention", "inputs": {
            "model": ["21", 0],
            "selection": attention.get("selection", "sol-attn"),
            "tau": attention.get("tau", 1.3),
            "start_percent": attention.get("start_percent", 0.2),
            "end_percent": attention.get("end_percent", 1.0),
            "dense_blocks": attention.get("dense_blocks", ""),
            "min_tokens": attention.get("min_tokens", 12288),
            "extra_tokens": attention.get("extra_tokens", 256),
            "sink_conditioning": attention.get("sink_conditioning", "exact_kv_and_rows"),
            "verbose": attention.get("verbose", True)}}
        model_src = ["22", 0]
    if sigmas:                      # 手写 sigma 曲线：替换 BasicScheduler
        g["23"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}}
        g["10"]["inputs"]["sigmas"] = ["23", 0]
        g.pop("7", None)
    if "7" in g:
        g["7"]["inputs"]["model"] = model_src
    g["8"]["inputs"]["model"] = model_src
    for i, name in enumerate(ref_images or []):
        nid = str(40 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["5"]["inputs"][f"ref_images.ref_image_{i}"] = [nid, 0]
    return g


def build_i2v_graph(prompt: str, width: int, height: int, frames: int, seed: int,
                    first_frame: str, steps: int = 20, lora: str = None,
                    sigmas: str = None, sampler: str = None, attention: dict = None) -> dict:
    """首帧驱动（fl2va）：用上一镜的最后一帧作为本镜第一帧，接续最稳。"""
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS_FL2V, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["clip"], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0],
            "prompt": prompt, "width": width, "height": height, "length": frames}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler or "res_multistep"}},
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
        "40": {"class_type": "LoadImage", "inputs": {"image": first_frame}},
    }
    g["5"]["inputs"]["first_frame"] = ["40", 0]
    model_src = ["1", 0]
    if lora:
        g["20"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": lora, "strength_model": 1.0}}
        model_src = ["20", 0]
    if attention:
        g["21"] = {"class_type": "ModelAttentionBackend",
                   "inputs": {"model": model_src, "attention": attention.get("backend", "pytorch attention")}}
        g["22"] = {"class_type": "BlockSparseAttention", "inputs": {
            "model": ["21", 0], "selection": attention.get("selection", "sol-attn"),
            "tau": attention.get("tau", 1.3),
            "start_percent": attention.get("start_percent", 0.2),
            "end_percent": attention.get("end_percent", 1.0),
            "dense_blocks": attention.get("dense_blocks", ""),
            "min_tokens": attention.get("min_tokens", 12288),
            "extra_tokens": attention.get("extra_tokens", 256),
            "sink_conditioning": attention.get("sink_conditioning", "exact_kv_and_rows"),
            "verbose": attention.get("verbose", True)}}
        model_src = ["22", 0]
    if sigmas:
        g["23"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}}
        g["10"]["inputs"]["sigmas"] = ["23", 0]
        g.pop("7", None)
    if "7" in g:
        g["7"]["inputs"]["model"] = model_src
    g["8"]["inputs"]["model"] = model_src
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
        raw = r.read().decode()
    # ComfyUI 的 /queue、/interrupt 成功时返回空体，不能当 JSON 解析
    return json.loads(raw) if raw.strip() else {}


def submit(project: Path, scene: str, units: list, seed: int = None,
           width=768, height=1024, steps=20) -> dict:
    """units: [{"unit": {...}, "count": n}]；seed 给了就按 seed, seed+1, ... 递增。"""
    batch = time.strftime("VID-%Y%m%d-%H%M%S")
    tasks = []
    for item in units:
        u = item["unit"]
        count = max(1, int(item.get("count") or 1))
        for k in range(count):
            jid = f"{batch}-{u['id']}-{k+1}" if count > 1 else f"{batch}-{u['id']}"
            job = {"id": jid, "batch": batch, "scene": scene, "unit": u["id"],
                   "chain": bool(item.get("chain")),
                   "prevUnit": item.get("prevUnit"),
                   "seq": k + 1, "seconds": u["seconds"], "frames": frames_of(u["seconds"]),
                   "width": width, "height": height, "steps": steps,
                   "lora": current_recipe().get("lora"),
                   "sigmas": current_recipe().get("sigmas"),
                   "sampler": current_recipe().get("sampler"),
                   "attention": current_recipe().get("attention"),
                   "refSize": current_recipe().get("refSize"),
                   "recipe": current_recipe().get("name"),
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
        if job.get("chain") and job.get("prevUnit"):
            prev = latest_result(project, job["scene"], job["prevUnit"])
            if not prev:
                raise RuntimeError(f"接续失败：找不到 {job['prevUnit']} 的成片")
            last_png = project / "制作运行" / "接续帧" / f"{job['unit']}_from_{job['prevUnit']}.png"
            last_png.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.1", "-i", str(project / prev),
                            "-frames:v", "1", str(last_png)], check=True, timeout=120)
            first_name = _stage_ref(job["port"], last_png, as_name=f"first_{jid[-10:]}{last_png.suffix}")
            with _lock:
                if jid in _jobs:
                    _jobs[jid]["firstFrame"] = str(last_png.relative_to(project))
            _save()
            g = build_i2v_graph(job["prompt"], job["width"], job["height"], job["frames"],
                                job["seed"], first_name, steps=job["steps"],
                                lora=job.get("lora"), sigmas=job.get("sigmas"),
                                sampler=job.get("sampler"), attention=job.get("attention"))
        else:
            g = build_graph(job["prompt"], job["width"], job["height"], job["frames"],
                            job["seed"], job["steps"], ref_images=ref_names,
                            lora=job.get("lora"), sigmas=job.get("sigmas"),
                            sampler=job.get("sampler"), attention=job.get("attention"),
                            ref_size=job.get("refSize") or "match")
        pid = _api(job["port"], "/prompt", {"prompt": g, "client_id": "shotcall-video"})["prompt_id"]
        with _lock:
            if jid in _jobs:
                _jobs[jid]["promptId"] = pid
        _save()                      # 立刻落盘：重启后还能认出这条任务在 ComfyUI 里的位置
        t0 = time.time()
        gone_since = None          # 只要任务还在 ComfyUI 队列里就一直等，不设总时长上限
        while True:
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
                                       "renderSec": round(time.time() - t0, 1),
                                       "finishedAt": time.time()})
                _save()
                return
            # 不在队列里也不在历史里：连续 10 分钟才认为真的丢了
            try:
                q = gpus.queue_of(job["port"])
                live = (pid in (q.get("runningIds") or [])) or (pid in (q.get("pendingIds") or []))
            except Exception:
                live = True
            if live:
                gone_since = None
                continue
            gone_since = gone_since or time.time()
            if time.time() - gone_since > 600:
                raise RuntimeError("任务从 ComfyUI 队列消失超过 10 分钟")
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid].update({"status": "failed", "error": str(e)})
        _save()


def latest_result(project: Path, scene: str, unit: str):
    """取某单元最新的成片（项目相对路径）。"""
    d = project / RESULT_DIR / scene / unit
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    return str(files[0].relative_to(project)) if files else None


def cancel_jobs(ids: list) -> dict:
    """停掉指定任务：排队中的从 ComfyUI 队列删掉，正在跑的才中断。"""
    ids = list(ids)
    out = {"cancelled": [], "interrupted": [], "notFound": []}
    dropped = set(sched.cancel_pending(ids))
    for jid in ids:
        j = _jobs.get(jid)
        if not j:
            out["notFound"].append(jid)
            continue
        pid = j.get("promptId")
        card = j.get("card")
        if jid in dropped:
            with _lock:
                j["status"] = "cancelled"
            out["cancelled"].append(jid)
            continue
        if not pid or card is None:
            with _lock:
                j["status"] = "cancelled"
            out["cancelled"].append(jid)
            continue
        port = 8188 + int(card)
        try:
            q = gpus.queue_of(port)
            if pid in (q.get("pendingIds") or []):
                _api(port, "/queue", {"delete": [pid]})     # 只删这一条
                with _lock:
                    j["status"] = "cancelled"
                out["cancelled"].append(jid)
            elif pid in (q.get("runningIds") or []):
                _api(port, "/interrupt", {})                # 中断当前
                with _lock:
                    j["status"] = "interrupted"
                out["interrupted"].append(jid)
            else:
                with _lock:
                    j["status"] = "cancelled"
                out["cancelled"].append(jid)
        except Exception as e:
            out.setdefault("errors", []).append(f"{jid}: {e}")
    _save()
    return out


def cancel(ids: list) -> int:
    n = 0
    with _lock:
        for jid in ids:
            if _jobs.get(jid, {}).get("status") == "queued":
                _jobs[jid]["status"] = "cancelled"
                n += 1
    return n


_load()
