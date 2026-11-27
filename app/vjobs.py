# -*- coding: utf-8 -*-
"""视频生成任务：按 09 的生成单元提交 H3 R2V，产物落到 生成结果/<场>/<单元>/。

与出图任务一样，只产任务，不选卡；卡由 sched 分配。
"""
import fcntl
import json
import os
import random
import subprocess
import re
import time
import uuid
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
# M1 仍在 app/run/vjobs.json；环境变量只给测试隔离用（指向 /tmp），生产不设。
LEGACY_PROJECT = str(Path.home() / "short_v/有谁共鸣2")
JOBS_FILE = Path(os.environ.get("SHOTCALL_VJOBS_FILE")
                 or (Path(__file__).parent / "run" / "vjobs.json"))
# 终态：reconcile 不再把它们拉回 running/waiting
TERMINAL_STATUSES = ("done", "succeeded", "failed", "cancelled",
                     "aborted", "interrupted", "deleted")


def _lock_path() -> Path:
    """独立锁文件：不能锁 vjobs.json 本身——它会被 os.replace 换掉 inode。"""
    return JOBS_FILE.with_name(JOBS_FILE.name + ".lock")


def _save():
    """原子落盘：进程间 flock 排他 + 唯一临时文件 + os.replace。

    取快照、写盘、replace 全在同一把 _lock 内完成，避免“旧快照后写”
    覆盖新状态。调用方**不要**在持有 _lock 时调用本函数。
    """
    try:
        with _lock:
            text = json.dumps({"order": list(_order),
                               "jobs": {k: dict(v) for k, v in _jobs.items()}},
                              ensure_ascii=False)
            JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _lock_path().open("a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                tmp = JOBS_FILE.with_name(
                    f"{JOBS_FILE.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(text)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, JOBS_FILE)
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
    except Exception:
        pass


def _load():
    """读锁 + 一次性读全文：不会读到另一个进程写了一半的文件。"""
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock_path().open("a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            text = JOBS_FILE.read_text(encoding="utf-8")
    except Exception:
        return
    try:
        data = json.loads(text)
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
    "base12":   {"label": "原模型 12 步（快测）",     "lora": None, "steps": 12, "hidden": True},
    "turbo4":   {"label": "turbo_4step LoRA · 4 步",  "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "steps": 4},
    "turbo8":   {"label": "turbo_4step LoRA · 8 步",  "lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "steps": 8},
    "progressive": {
        "hidden": True,
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
                "lora": saved.get("lora"), "loraStrength": saved.get("loraStrength"),
                "steps": saved["steps"],
                "sigmas": saved.get("sigmas"), "sampler": saved.get("sampler"),
                "attention": saved.get("attention"),
                "sigmaShift": saved.get("sigmaShift"),
                "res": saved.get("res"), "refSize": saved.get("refSize"),
                "source": saved.get("source")}
    return {"name": "base20", **RECIPES["base20"]}


RECIPES_EXTRA = Path(__file__).with_name("run") / "gen_recipes_extra.json"


def load_extra() -> list:
    """导入过的 ComfyUI 工作流，作为常驻卡片。"""
    try:
        data = json.loads(RECIPES_EXTRA.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_recipe(spec: dict) -> None:
    items = [x for x in load_extra() if x.get("name") != spec.get("name")]
    items.insert(0, {k: spec.get(k) for k in
                     ("name", "label", "steps", "lora", "loraStrength", "sampler",
                      "sigmaShift", "sigmas", "attention", "res", "refSize", "source")})
    RECIPES_EXTRA.parent.mkdir(parents=True, exist_ok=True)
    RECIPES_EXTRA.write_text(json.dumps(items[:24], ensure_ascii=False, indent=1),
                             encoding="utf-8")


def save_recipe(spec: dict) -> dict:
    RECIPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECIPE_FILE.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    if spec.get("source") == "comfyui":
        _remember_recipe(spec)
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


def _prompt_files(d: Path) -> list:
    """H3 提示词两种合法命名：minimax-h3-ref2va-vNNN.md（旧）与 minimax-h3-vNNN.md（现行）。

    同一版本号两种都在时优先 ref2va（历史文件），不重复列出。
    """
    seen, out = set(), []
    for pat in ("minimax-h3-ref2va*.md", "minimax-h3-v*.md"):
        for f in sorted(d.glob(pat)):
            m = re.search(r"-v(\d{3})(?:-\d+)?\.md$", f.name)
            key = int(m.group(1)) if m else 0
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def list_versions(d: Path) -> list:
    out = []
    for f in _prompt_files(d):
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


def _zh_from_companion(project_scene_dir, prompt_file):
    """从 <主文件>.zh.md 读逐单元中文；返回 (map, stale)。"""
    if prompt_file is None:
        return {}, False
    zh_file = prompt_file.with_suffix(".zh.md")
    if not zh_file.is_file():
        return {}, False
    try:
        txt = zh_file.read_text(encoding="utf-8")
    except Exception:
        return {}, False
    out = {}
    for m in re.finditer(r"\*\*(U\d{3})[^\n]*\n(.*?)(?=\n\*\*U|\n## |\Z)", txt, re.S):
        out[m.group(1)] = m.group(2).strip()
    stale = zh_file.stat().st_mtime < prompt_file.stat().st_mtime
    return out, stale


def attach_zh(units: list, scene: str, prompt_file=None) -> list:
    """给每个单元挂中文审核稿；英文原文变了就标 zhStale。"""
    table = (load_zh() or {}).get(scene, {})
    comp, comp_stale = _zh_from_companion(None, prompt_file)
    for u in units:
        rec = table.get(u["id"])
        # 配套 .zh.md 优先（新流程）；没有才退回 prompt_zh.json（旧机制）
        if comp.get(u["id"]):
            u["zh"], u["zhStale"] = comp[u["id"]] + "\n", comp_stale
            continue
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
    files = _prompt_files(d)                        # 两种命名都认，按版本取最大
    if not files:
        return None

    def ver(f: Path):
        m = re.search(r"-v(\d{3})(?:-\d+)?\.md$", f.name)
        return (int(m.group(1)) if m else 0, f.stat().st_mtime)

    return max(files, key=ver)


BLOCK_NAMES = ("shotcall-units", "yoko-shots")   # 第二项是历史标记，仅为读旧文件保留


def shots_block(txt: str):
    """解析 shotcall-units 块（兼容历史标记名）。

    只认新格式 {"schemaVersion":1,"units":[...]}；裸数组按旧格式继续读。
    返回 (units, error)；error 为 None 表示成功。
    """
    m = None
    for name in BLOCK_NAMES:
        m = re.search(r"```" + name + r"\n(.*?)\n```", txt, re.S)
        if m:
            break
    if not m:
        return [], "缺少 shotcall-units 块"
    try:
        raw = json.loads(m.group(1))
    except Exception as e:
        return [], f"shotcall-units 块不是合法 JSON：{e}"
    if isinstance(raw, list):          # 旧格式：裸数组，继续读
        return (raw, None) if raw else ([], "shotcall-units 块是空数组")
    units = raw.get("units")
    if not isinstance(units, list) or not units:
        return [], "shotcall-units 块里没有 units 数组"
    return units, None


def parse_sync_refs(project: Path, scene: str) -> dict:
    """从视频提示词 md 的 shotcall-units 块里取每单元的参考图与资产 ID。"""
    d = project / VP_DIR / scene
    if not d.is_dir():
        return {}
    f = latest_prompt_file(d)
    if not f:
        return {}
    txt = f.read_text(encoding="utf-8")
    units, err = shots_block(txt)
    if err:
        return {}
    out = {}
    for u in units:
        refs = []
        for i, path in enumerate(u.get("referenceImages") or [], 1):
            refs.append({"n": i, "file": path, "exists": (project / path).exists()})
        audios = []
        for i, path in enumerate(u.get("referenceAudios") or [], 1):
            audios.append({"n": i, "file": path, "exists": (project / path).exists()})
        out[u.get("id")] = {"refs": refs, "assetIds": u.get("assetIds") or [],
                            "chainFrom": u.get("chainFrom"),
                            "continuation": u.get("continuation"),
                            "audios": audios,
                            # roles 可能是对象（契约）也可能是旧文件里的字符串，两种都容忍
                            "audioAssetIds": [r.get("assetId") if isinstance(r, dict) else None
                                              for r in (u.get("referenceAudioRoles") or [])],
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
    # 标题格式允许小数秒与附加列，例如：### U001｜8.00秒｜SH001｜192帧
    for m in re.finditer(r"^### (U\d{3}(?:-[A-Z])?)｜(\d+(?:\.\d+)?)秒｜([^｜\n]+)(?:｜[^\n]*)?\n(.*?)(?=^### |\Z)",
                         txt, re.S | re.M):
        body = m.group(4)
        pm = re.search(r"```text\n(.*?)```", body, re.S)
        out.append({
            "id": m.group(1), "seconds": float(m.group(2)),
            "shots": m.group(3).strip(),
            "prompt": (pm.group(1).strip() if pm else ""),
            "source": f.name,
        })
    sync = parse_sync_refs(project, scene)
    try:
        import assetsdb
    except Exception:
        assetsdb = None
    for u in out:
        s = sync.get(u["id"]) or {}
        u["refs"] = s.get("refs") or []
        u["audios"] = s.get("audios") or []
        u["chainFrom"] = s.get("chainFrom")
        u["continuation"] = s.get("continuation")
        u["audioAssetIds"] = s.get("audioAssetIds") or []
        u["assetIds"] = [a.split("--")[-1] for a in (s.get("assetIds") or [])]
        if assetsdb:
            for r in u.get("refs") or []:
                cur = assetsdb.current_ref(project, r.get("file") or "")
                if cur and cur.get("currentPath"):
                    r["promptFile"] = r.get("file")
                    r["file"] = cur["currentPath"]
                    bid = cur.get("asset") or ""
                    if bid in ("", "default"):
                        bid = assetsdb.ref_asset(project, r["file"]) or bid
                    r["bindingAsset"] = bid
                    r["changed"] = bool(cur.get("changed"))
                    r["exists"] = (project / r["file"]).exists()
                elif not r.get("bindingAsset"):
                    r["bindingAsset"] = assetsdb.ref_asset(project, r.get("file") or "")
        sb = s.get("storyboard") or {}
        u["storyboard"] = ({
            "path": sb.get("path"),
            "pictureIndex": sb.get("pictureIndex"),
            "row": sb.get("row"),
            "panels": sb.get("panels") or [],
        } if sb.get("path") else None)
    return attach_zh(out, scene, f)


def resume(project: Path) -> list:
    """进程重启后，把"还在排队、从没进过 ComfyUI"的任务重新交给调度。

    依赖已经失败/取消的任务不再重排，直接标 cancelled（dependency_failed）；
    连带取消会沿 afterIds 继续传播（含重启后只存在于 vjobs 里的失败状态）。
    """
    todo = []
    with _lock:
        for jid in _order:
            j = _jobs.get(jid) or {}
            # 只要还停在"排队"且不在调度待派表里，就重新送一遍（防止重启丢任务）
            if j.get("status") == "queued" and jid not in sched.pending_ids():
                todo.append(dict(j))
        status_of = {jid: (j.get("status") or "") for jid, j in _jobs.items()}
    if not todo:
        return []
    dead_status = {"failed", "cancelled", "aborted", "interrupted", "deleted"}
    failed = set(sched.failed_ids())
    skipped = []
    while todo:
        keep = []
        for j in todo:
            deps = j.get("afterIds") or []
            if any(d in failed or status_of.get(d) in dead_status for d in deps):
                skipped.append(j)
                failed.add(j["id"])
                status_of[j["id"]] = "cancelled"
            else:
                keep.append(j)
        if len(keep) == len(todo):
            todo = keep
            break
        todo = keep
    if skipped:
        ids = [j["id"] for j in skipped]
        mark_cancelled(ids, "dependency_failed")
        sched.mark_failed(ids)          # 让仍在调度表里的下游也一起取消
    if not todo:
        return []
    by_scene = {}
    for j in todo:
        by_scene.setdefault(j.get("scene") or "N001", []).append(j)
    tasks = []
    for sc, rows in by_scene.items():
        units = {u["id"]: u for u in parse_units(project, sc)}
        for j in rows:
            u = units.get(j.get("unit"))
            if not u:
                with _lock:
                    if j["id"] in _jobs:
                        _jobs[j["id"]].update({"status": "failed",
                                               "error": f"重启后找不到单元 {j.get('unit')}"})
                continue
            with _lock:
                _jobs[j["id"]].update({"status": "queued", "prompt": u["prompt"]})
            tasks.append({"id": j["id"], "run": _runner(project, j["id"]),
                          # 重启后必须保留依赖边，否则接续 B 会不等 A 就开跑
                          "after": list(j.get("afterIds") or []),
                          "on_cancel": lambda reason, ids=[j["id"]]:
                              mark_cancelled(ids, reason)})
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
            if j.get("status") in ("running", "waiting", "dispatched", "queued") and not j.get("frames"):
                hit = units.get((j.get("prompt") or "").strip())
                if hit:
                    j["seconds"] = hit[1].get("seconds")
                    j["frames"] = frames_of(hit[1].get("seconds") or 10)
                    j.setdefault("steps", 20)
    # 老记录补参考图（之前认领时没存）
    with _lock:
        for jid, j in _jobs.items():
            if j.get("status") in ("running", "waiting", "dispatched", "queued") and not j.get("refs"):
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
            # 以 ComfyUI 实际队列为准：排队=dispatched+queueState=waiting，
            # 真正开渲才 running；终态任务不回落。
            with _lock:
                for jj in _jobs.values():
                    pid2 = jj.get("promptId")
                    if not pid2 or jj.get("status") in TERMINAL_STATUSES:
                        continue
                    if pid2 in run_now:
                        if jj.get("status") != "running" or jj.get("queueState") != "running":
                            jj["status"] = "running"
                            jj["queueState"] = "running"
                            jj["error"] = None
                            changed = True
                        if not jj.get("execStartedAt"):
                            jj["execStartedAt"] = time.time()
                            changed = True
                    elif pid2 in pend_now:
                        if jj.get("status") != "dispatched" or jj.get("queueState") != "waiting":
                            jj["status"] = "dispatched"
                            jj["queueState"] = "waiting"
                            jj["error"] = None
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
                # ComfyUI 原始 /queue 只有 queue_running / queue_pending，没有 runningIds；
                # 用 run_now / pend_now 判定，别把正在渲染的又写成 waiting。
                running_now = pid in run_now
                st = "running" if running_now else "dispatched"
                qs = "running" if running_now else "waiting"
                exec_at = time.time() if running_now else None
                if running_now:
                    with _lock:
                        for jj in _jobs.values():
                            if jj.get("promptId") == pid and not jj.get("execStartedAt"):
                                jj["execStartedAt"] = time.time()
                                changed = True
                with _lock:
                    used = {(_jobs.get(x) or {}).get("promptId") for x in _order}
                    if pid in used:          # 这个 pid 已经有任务认领了，别再重复分配
                        continue
                    matched = False
                    for jid in _order:
                        j = _jobs.get(jid) or {}
                        if (j.get("scene") == sc and j.get("unit") == uid
                                and j.get("status") in ("running", "waiting", "queued", "dispatched")
                                and not j.get("promptId")):
                            _jobs[jid].update({"promptId": pid, "card": card,
                                               "status": st, "queueState": qs,
                                               "pidAt": time.time()})
                            if exec_at and not _jobs[jid].get("execStartedAt"):
                                _jobs[jid]["execStartedAt"] = exec_at
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
                                      "seed": seed, "status": st, "queueState": qs,
                                      "card": card,
                                      "promptId": pid, "pidAt": time.time(),
                                      "refs": [r["file"] for r in (unit_obj.get("refs") or [])
                                               if r.get("exists")],
                                      "file": None, "error": None,
                                      "queuedAt": time.time(), "startedAt": None,
                                      "prompt": text, "source": "adopted"}
                        if exec_at:
                            _jobs[jid]["execStartedAt"] = exec_at
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


def list_jobs(batch: str = None, project=None) -> list:
    want = str(Path(project).expanduser().resolve()) if project else None
    with _lock:
        # 返回浅拷贝，避免 HTTP 线程 json.dumps 时与 _execute/reconcile 的 update 竞态
        items = [dict(_jobs[j]) for j in _order if not batch or _jobs[j]["batch"] == batch]
    if want:
        items = [j for j in items if (j.get("project") or LEGACY_PROJECT) == want]
    return items


def build_graph(prompt: str, width: int, height: int, frames: int, seed: int, steps: int = 20,
                ref_images: list = None, lora: str = None, sigmas: str = None,
                sampler: str = None, attention: dict = None, ref_size: str = "match",
                ref_audios: list = None, lora_strength: float = None,
                sigma_shift: dict = None) -> dict:
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
                   "inputs": {"model": ["1", 0], "lora_name": lora,
                              "strength_model": 1.0 if lora_strength is None else float(lora_strength)}}
        model_src = ["20", 0]
    if sigma_shift:
        g["24"] = {"class_type": "MiniMaxH3SigmaShift",
                   "inputs": {"model": model_src,
                              "shift_video": float(sigma_shift.get("video", 6)),
                              "shift_audio": float(sigma_shift.get("audio", 3))}}
        model_src = ["24", 0]
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
    for j, name in enumerate(ref_audios or []):
        nid = str(60 + j)
        g[nid] = {"class_type": "LoadAudio", "inputs": {"audio": name}}
        g["5"]["inputs"][f"ref_audios.ref_audio_{j}"] = [nid, 0]
    return g


def build_i2v_graph(prompt: str, width: int, height: int, frames: int, seed: int,
                    first_frame: str, steps: int = 20, lora: str = None,
                    sigmas: str = None, sampler: str = None, attention: dict = None,
                    lora_strength: float = None, sigma_shift: dict = None) -> dict:
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
                   "inputs": {"model": ["1", 0], "lora_name": lora,
                              "strength_model": 1.0 if lora_strength is None else float(lora_strength)}}
        model_src = ["20", 0]
    if sigma_shift:
        g["24"] = {"class_type": "MiniMaxH3SigmaShift",
                   "inputs": {"model": model_src,
                              "shift_video": float(sigma_shift.get("video", 6)),
                              "shift_audio": float(sigma_shift.get("audio", 3))}}
        model_src = ["24", 0]
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
           width=1344, height=768, steps=20, batch: str = None) -> dict:
    """units: [{"unit": {...}, "count": n, "chain": bool, "prevUnit": id}]

    带 chain 的任务会自动依赖同批上一单元的最后一条任务，保证串行。
    """
    batch = batch or (time.strftime("VID-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
    recipe = current_recipe()
    plan = []            # (item, unit, k, jid, job)
    by_unit = {}         # unit_id -> [jid, ...]（按提交顺序）
    for item in units:
        u = item["unit"]
        count = max(1, int(item.get("count") or 1))
        by_unit.setdefault(u["id"], [])
        for k in range(count):
            jid = f"{batch}-{u['id']}-{k+1}" if count > 1 else f"{batch}-{u['id']}"
            seed_i = (seed + k) if seed is not None else random.randint(1, 2 ** 31 - 1)
            job = {"id": jid, "batch": batch, "scene": scene, "unit": u["id"],
                   "project": str(project),
                   "chain": bool(item.get("chain")),
                   "prevUnit": item.get("prevUnit"),
                   "seq": k + 1, "seconds": u["seconds"], "frames": frames_of(u["seconds"]),
                   "width": width, "height": height, "steps": steps,
                   "lora": recipe.get("lora"), "loraStrength": recipe.get("loraStrength"),
                   "sigmas": recipe.get("sigmas"),
                   "sampler": recipe.get("sampler"), "attention": recipe.get("attention"),
                   "sigmaShift": recipe.get("sigmaShift"),
                   "refSize": recipe.get("refSize"), "recipe": recipe.get("name"),
                   "seed": seed_i,
                   "refs": [r["file"] for r in (u.get("refs") or []) if r.get("exists")],
                   "audios": [a["file"] for a in (u.get("audios") or []) if a.get("exists")],
                   "status": "queued", "card": None, "file": None, "error": None,
                   "queuedAt": time.time(), "startedAt": None, "prompt": u["prompt"],
                   "afterIds": []}
            try:
                import firstframe
                ff = firstframe.first_for_unit(project, scene, u.get("shots") or "")
                if ff:
                    job["firstFrame"] = ff
            except Exception:
                pass
            plan.append((item, u, k, jid, job))
            by_unit[u["id"]].append(jid)

    # 依赖：接续任务等同批上一单元全部完成
    for item, u, k, jid, job in plan:
        prev = job.get("prevUnit")
        if job.get("chain") and prev and by_unit.get(prev):
            job["afterIds"] = list(by_unit[prev])

    tasks = []
    for item, u, k, jid, job in plan:
        with _lock:
            _jobs[jid] = job
            _order.append(jid)
        tasks.append({"id": jid, "run": _runner(project, jid),
                      "after": list(job["afterIds"]),
                      # 默认参数固定 jid，避免闭包晚绑定把取消记到最后一条任务上
                      "on_cancel": lambda reason, ids=[jid]:
                          mark_cancelled(ids, reason)})
    _save()
    sched.submit("video", tasks)
    return {"batch": batch, "jobs": [t["id"] for t in tasks]}


def _runner(project: Path, jid: str):
    def run(port: int) -> bool:
        with _lock:
            job = _jobs.get(jid)
            if job is None or job.get("status") == "cancelled":
                return False
            # 派发≠生成中：真正开渲由 reconcile 按 ComfyUI running 队列改写
            job.update({"status": "dispatched", "startedAt": time.time(),
                        "port": port, "card": port - 8188, "error": None})
            snapshot = dict(job)
        _save()
        return _execute(project, jid, snapshot)
    return run


def _execute(project: Path, jid: str, job: dict) -> bool:
    """执行一条视频任务。成功返回 True；失败写 status=failed 并返回 False。"""
    try:
        with _lock:
            if (_jobs.get(jid) or {}).get("status") in TERMINAL_STATUSES:
                return False
        ref_names = []
        for i, rel in enumerate(job.get("refs") or []):
            f = project / rel
            if not f.exists():
                raise RuntimeError(f"参考图缺失：{rel}")
            ref_names.append(_stage_ref(job["port"], f, as_name=f"u_{jid[-10:]}_{i}{f.suffix}"))
        audio_names = []
        for i, rel in enumerate(job.get("audios") or []):
            f = project / rel
            if not f.exists():
                raise RuntimeError(f"参考音频缺失：{rel}")
            audio_names.append(_stage_ref(job["port"], f, as_name=f"a_{jid[-10:]}_{i}{f.suffix}"))
        with _lock:
            if jid in _jobs:
                _jobs[jid]["usedRefs"] = ref_names
                _jobs[jid]["usedAudios"] = audio_names
        _save()
        ff_rel = job.get("firstFrame")
        if ff_rel and not job.get("chain") and (project / ff_rel).is_file():
            # 分镜首帧已由导演台采用：直接走 I2V 首帧驱动
            ff_path = project / ff_rel
            first_name = _stage_ref(job["port"], ff_path, as_name=f"first_{jid[-10:]}{ff_path.suffix}")
            g = build_i2v_graph(job["prompt"], job["width"], job["height"], job["frames"],
                                job["seed"], first_name, steps=job["steps"],
                                lora=job.get("lora"), sigmas=job.get("sigmas"),
                                sampler=job.get("sampler"), attention=job.get("attention"),
                                lora_strength=job.get("loraStrength"),
                                sigma_shift=job.get("sigmaShift"))
        elif job.get("chain") and job.get("prevUnit"):
            prev = None
            with _lock:
                dep = (_jobs.get((job.get("afterIds") or [None])[0]) or {})
                if dep.get("file"):
                    prev = dep["file"]
            prev = prev or latest_result(project, job["scene"], job["prevUnit"])
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
                                sampler=job.get("sampler"), attention=job.get("attention"),
                                lora_strength=job.get("loraStrength"),
                                sigma_shift=job.get("sigmaShift"))
        else:
            g = build_graph(job["prompt"], job["width"], job["height"], job["frames"],
                            job["seed"], job["steps"], ref_images=ref_names,
                            lora=job.get("lora"), sigmas=job.get("sigmas"),
                            sampler=job.get("sampler"), attention=job.get("attention"),
                            ref_size=job.get("refSize") or "match",
                            ref_audios=audio_names, lora_strength=job.get("loraStrength"),
                            sigma_shift=job.get("sigmaShift"))
        pid = _api(job["port"], "/prompt", {"prompt": g, "client_id": "shotcall-video"})["prompt_id"]
        with _lock:
            if jid in _jobs:
                j = _jobs[jid]
                j["promptId"] = pid
                # 拿到 prompt_id 仍算 dispatched；排队/开渲由 reconcile 对账。
                # 若对账线程已经先一步写成 running，不要把它降级。
                if j.get("status") in (None, "queued", "starting"):
                    j["status"] = "dispatched"
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
                k = 2
                while out.exists():
                    out = out.with_name(f"{job['seed']}_{k}.mp4")
                    k += 1
                out.write_bytes(data)
                with _lock:
                    if jid in _jobs:
                        if _jobs[jid].get("status") in ("cancelled", "interrupted", "deleted"):
                            return False
                        now = time.time()
                        started_exec = _jobs[jid].get("execStartedAt") or t0
                        _jobs[jid].update({"status": "done",
                                           "queueState": None, "error": None,
                                           "file": str(out.relative_to(project)),
                                           # 只计真正开渲到出片，不含排队
                                           "renderSec": round(now - started_exec, 1),
                                           "queueWaitSec": round(started_exec - t0, 1) if t0 else 0,
                                           "finishedAt": now})
                _save()
                return True
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
            j = _jobs.get(jid)
            if j is not None and j.get("status") not in TERMINAL_STATUSES:
                j.update({"status": "failed", "error": str(e),
                          "finishedAt": time.time()})
        _save()
        return False


def mark_cancelled(ids, reason: str) -> int:
    """把任务置 cancelled，写 cancelReason / cancelledAt 并落盘。

    用于依赖失败连带取消；只改状态记录，不碰 ComfyUI（下发时已不在队列）。
    """
    ids = [ids] if isinstance(ids, str) else list(ids)
    now = time.time()
    n = 0
    with _lock:
        for jid in ids:
            j = _jobs.get(jid)
            if not j:
                continue
            j["status"] = "cancelled"
            j["cancelReason"] = reason
            j["cancelledAt"] = now
            j["queueState"] = None
            j.pop("vanishedAt", None)
            n += 1
    if n:
        _save()
    return n


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
