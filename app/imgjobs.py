# -*- coding: utf-8 -*-
"""出图任务：提交、轮询、落盘；M3 起 JSON 持久化 + queued 续跑。"""
import fcntl
import json
import os
import random
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

import gpus
import imagegen
import sched
import store

_lock = threading.Lock()
_jobs = {}          # job_id -> dict
_order = []
JOBS_FILE = Path(os.environ.get("SHOTCALL_IMGJOBS_FILE")
                 or (Path(__file__).parent / "run" / "imgjobs.json"))
TERMINAL = ("done", "failed", "cancelled")
LEGACY_PROJECT = str(Path.home() / "short_v/有谁共鸣2")


def _save():
    try:
        with _lock:
            text = json.dumps({"order": list(_order),
                               "jobs": {k: dict(v) for k, v in _jobs.items()}},
                              ensure_ascii=False)
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_name(f"{JOBS_FILE.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, JOBS_FILE)
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


def _safe_name(name: str) -> str:
    """目录名里不放括号、斜杠等字符。"""
    s = re.sub(r"[（(].*?[)）]", "", name or "")          # 去掉括号说明
    s = re.sub(r"[\\/:*?\"<>|\s]+", "", s)             # 去掉非法字符与空白
    return s[:24] or "未命名"


def _job_dir(project: Path, ep: str, asset_id: str, out_root: str = None) -> Path:
    """候选图：出图页走 07-图像资产/候选图片/<资产>；旧链路走 剧集/<集>/制作成果。"""
    rel = (Path(out_root) / asset_id) if out_root else (Path("剧集") / ep / "制作成果" / "图片候选" / asset_id)
    for base in (project, store.state_dir(project), Path(__file__).parent / "run"):
        d = base / rel if base == project else base / "img" / ep / asset_id
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".w"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except OSError:
            continue
    raise RuntimeError("没有任何可写目录用于保存候选图")


def list_jobs(batch: str = None, project=None) -> list:
    want = str(Path(project).expanduser().resolve()) if project else None
    with _lock:
        items = [dict(_jobs[j]) for j in _order if not batch or _jobs[j]["batch"] == batch]
    if want:
        items = [j for j in items if (j.get("project") or LEGACY_PROJECT) == want]
    return items


def cancel(ids: list) -> dict:
    """停止指定图片任务：未派的撤下，已在 ComfyUI 队列的删掉，正在跑的才中断。"""
    ids = list(ids)
    out = {"cancelled": [], "interrupted": [], "notFound": []}
    dropped = set(sched.cancel_pending(ids))
    for jid in ids:
        with _lock:
            j = _jobs.get(jid)
            if j is None:
                out["notFound"].append(jid)
                continue
            pid = j.get("promptId")
            card = j.get("card")
        if jid in dropped or not pid or card is None:
            with _lock:
                if _jobs.get(jid):
                    _jobs[jid]["status"] = "cancelled"
            out["cancelled"].append(jid)
            continue
        port = 8188 + int(card)
        try:
            q = gpus.queue_of(port)
            if pid in (q.get("pendingIds") or []):
                imagegen._post(port, "/queue", {"delete": [pid]})
                with _lock:
                    _jobs[jid]["status"] = "cancelled"
                out["cancelled"].append(jid)
            elif pid in (q.get("runningIds") or []):
                imagegen._post(port, "/interrupt", {})
                with _lock:
                    _jobs[jid]["status"] = "interrupted"
                out["interrupted"].append(jid)
            else:
                with _lock:
                    _jobs[jid]["status"] = "cancelled"
                out["cancelled"].append(jid)
        except Exception as e:
            out.setdefault("errors", []).append(f"{jid}: {e}")
    _save()
    out["count"] = len(out["cancelled"]) + len(out["interrupted"])
    return out


def cancel_batch(batch: str) -> dict:
    with _lock:
        ids = [j for j in _order if _jobs[j]["batch"] == batch and _jobs[j]["status"] == "queued"]
    return cancel(ids)


def clear_finished() -> int:
    with _lock:
        kill = [j for j in _order if _jobs[j]["status"] in ("done", "failed")]
        for j in kill:
            _jobs.pop(j, None)
        _order[:] = [j for j in _order if j in _jobs]
    _save()
    return len(kill)


def submit_batch(project: Path, ep: str, items: list, out_root: str = None) -> dict:
    """items: [{id,name,prompt,ref,n,width,height}]；卡由调度后端决定。

    out_root: 例如 07-图像资产/候选图片；None 时走旧 剧集/<ep>/制作成果。
    """
    batch = time.strftime("IMG-%Y%m%d-%H%M%S")
    tasks = []
    all_ids = []
    for it in items:
        n = max(1, int(it.get("n") or 4))
        for k in range(n):
            jid = f"{batch}-{it['id']}-{k+1}"
            job = {"id": jid, "batch": batch, "asset": it["id"], "project": str(project),
                   "dir": f"{it['id']}-{_safe_name(it.get('name',''))}",
                   "name": it.get("name", ""), "card": None, "port": None,
                   "seed": (int(it["seed"]) + k) if it.get("seed") is not None
                           else random.randint(1, 2 ** 31 - 1),
                   "status": "queued", "file": None, "promptId": None, "error": None,
                   "queuedAt": time.time(), "startedAt": None,
                   "width": it.get("width") or imagegen.DEFAULT_W,
                   "height": it.get("height") or imagegen.DEFAULT_H,
                   "steps": int(it.get("steps") or imagegen.DEFAULT_STEPS),
                   "prompt": it["prompt"],
                   "ref": it.get("ref") or ((it.get("refs") or [None])[0]),
                   "refs": list(it.get("refs") or ([] if not it.get("ref") else [it.get("ref")])),
                   "ep": ep, "outRoot": out_root, "channel": it.get("channel") or "local"}
            with _lock:
                _jobs[jid] = job
                _order.append(jid)
            all_ids.append(jid)
            if job.get("channel") == "codex":
                with _lock:
                    _jobs[jid]["status"] = "running"
                    _jobs[jid]["startedAt"] = time.time()
                threading.Thread(target=_execute_codex, args=(project, ep, jid, out_root),
                                 daemon=True).start()
            else:
                tasks.append({"id": jid, "run": _make_runner(project, ep, jid, out_root)})
    _save()
    if tasks:
        sched.submit("image", tasks)
    return {"batch": batch, "jobs": all_ids}


def _make_runner(project: Path, ep: str, jid: str, out_root: str = None):
    def run(port: int):
        with _lock:
            job = dict(_jobs.get(jid) or {})
            if job.get("status") == "cancelled":
                return
            _jobs[jid]["status"] = "running"
            _jobs[jid]["startedAt"] = time.time()
            _jobs[jid]["port"] = port
            _jobs[jid]["card"] = port - 8188
        _execute(project, ep, jid, dict(_jobs[jid]), out_root)
    return run


def _execute_codex(project: Path, ep: str, jid: str, out_root: str = None):
    """用 Codex 订阅通道出图；失败写进任务错误。"""
    import codexchan
    job = dict(_jobs.get(jid) or {})
    try:
        t0 = time.time()
        imgs = []
        for rp in (job.get("refs") or []):
            f = project / rp
            if f.is_file():
                imgs.append(f.read_bytes())
        data, meta = codexchan.generate(job["prompt"],
                                        size="1536x1024" if (job.get("width") or 0) > (job.get("height") or 0) else "1024x1024",
                                        quality=os.environ.get("CODEX_IMG_QUALITY", "high"),
                                        images=imgs)
        out_asset = job.get("asset")
        out = _job_dir(project, ep, out_asset, job.get("outRoot") or out_root) / f"{job['seed']}.png"
        out.write_bytes(data)
        with _lock:
            _jobs[jid].update({"status": "done", "file": _rel(project, out),
                               "seconds": round(time.time() - t0, 1), "meta": meta})
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid]["status"] = "failed"
                _jobs[jid]["error"] = f"{type(e).__name__}: {e}"[:300]
    _save()


def _execute(project: Path, ep: str, jid: str, job: dict, out_root: str = None):
    try:
        ref_files = []
        ref_paths = job.get("refs") or ([job["ref"]] if job.get("ref") else [])
        for rp in ref_paths:
            if rp and (project / rp).exists():
                ref_files.append(_stage_ref(job["port"], project / rp))
        g = imagegen.build_graph(job["prompt"], job["width"], job["height"],
                                 job["seed"], ref_images=ref_files,
                                 steps=int(job.get("steps") or imagegen.DEFAULT_STEPS))
        pid = imagegen.submit(job["port"], g)
        with _lock:
            _jobs[jid]["promptId"] = pid
        t0 = time.time()
        while time.time() - t0 < 900:
            h = imagegen.history(job["port"], pid)
            if pid in h and h[pid].get("status", {}).get("completed"):
                imgs = [i for v in h[pid].get("outputs", {}).values() for i in v.get("images", [])]
                if not imgs:
                    raise RuntimeError("任务完成但没有图")
                data = imagegen.download(job["port"], imgs[0])
                out_asset = (job.get("asset") if (job.get("outRoot") or out_root) else job["dir"])
                out = _job_dir(project, ep, out_asset, job.get("outRoot") or out_root) / f"{job['seed']}.png"
                out.write_bytes(data)
                with _lock:
                    _jobs[jid]["status"] = "done"
                    _jobs[jid]["file"] = _rel(project, out)
                    _jobs[jid]["seconds"] = round(time.time() - t0, 1)
                _save()
                return
            time.sleep(3)
        raise RuntimeError("轮询超时")
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid]["status"] = "failed"
                _jobs[jid]["error"] = f"{e}"
        _save()


def _rel(project: Path, out: Path) -> str:
    for base in (project, Path(__file__).parent / "run"):
        try:
            return str(out.relative_to(base))
        except ValueError:
            continue
    return str(out)


def _stage_ref(port: int, path: Path):
    """把参考图通过 ComfyUI 的上传接口送进去，返回它分配的文件名。"""
    import urllib.request
    boundary = "----shotcall" + str(int(time.time() * 1000))
    name = f"shotcall_ref_{path.name}"
    body = b""
    body += f'--{boundary}\r\n'.encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += path.read_bytes() + b"\r\n"
    body += f'--{boundary}\r\n'.encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    return d.get("name") or name


def resume(project: Path) -> list:
    """重启后把 queued 图片任务重新交给调度；running 不重提（避免重复扣卡）。"""
    todo = []
    with _lock:
        for jid in _order:
            j = _jobs.get(jid) or {}
            if j.get("status") == "queued" and jid not in sched.pending_ids():
                todo.append(dict(j))
    if not todo:
        return []
    tasks = []
    for j in todo:
        with _lock:
            _jobs[j["id"]].update({"status": "queued", "error": None})
        tasks.append({"id": j["id"], "run": _make_runner(project, j.get("ep") or "EP002",
                                                          j["id"], j.get("outRoot")),
                      "on_cancel": lambda reason, ids=[j["id"]]: cancel(ids)})
    sched.submit("image", tasks)
    return [t["id"] for t in tasks]


_load()
