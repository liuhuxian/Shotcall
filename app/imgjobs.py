# -*- coding: utf-8 -*-
"""出图任务：提交、轮询、落盘。内存态，重启后不保留未完成任务。"""
import json
import random
import re
import threading
import time
import traceback
from pathlib import Path

import imagegen
import sched
import store

_lock = threading.Lock()
_jobs = {}          # job_id -> dict
_order = []


def _safe_name(name: str) -> str:
    """目录名里不放括号、斜杠等字符。"""
    s = re.sub(r"[（(].*?[)）]", "", name or "")          # 去掉括号说明
    s = re.sub(r"[\\/:*?\"<>|\s]+", "", s)             # 去掉非法字符与空白
    return s[:24] or "未命名"


def _job_dir(project: Path, ep: str, asset_id: str) -> Path:
    """候选图落在项目内：剧集/<集号>/制作成果/图片候选/<资产>/
    写不进去时退回状态目录（项目目录只读的沙箱环境）。"""
    rel = Path("剧集") / ep / "制作成果" / "图片候选" / asset_id
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


def list_jobs(batch: str = None) -> list:
    with _lock:
        items = [_jobs[j] for j in _order if not batch or _jobs[j]["batch"] == batch]
    return items


def cancel(ids: list) -> dict:
    """取消排队中的任务；已开始的不动。"""
    done = []
    with _lock:
        for jid in ids:
            j = _jobs.get(jid)
            if j and j["status"] == "queued":
                j["status"] = "cancelled"
                done.append(jid)
    return {"cancelled": done, "count": len(done)}


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
    return len(kill)


def submit_batch(project: Path, ep: str, items: list) -> dict:
    """items: [{id,name,prompt,ref,n,width,height}]；卡由调度后端决定。"""
    batch = time.strftime("IMG-%Y%m%d-%H%M%S")
    tasks = []
    for it in items:
        n = max(1, int(it.get("n") or 4))
        for k in range(n):
            jid = f"{batch}-{it['id']}-{k+1}"
            job = {"id": jid, "batch": batch, "asset": it["id"],
                   "dir": f"{it['id']}-{_safe_name(it.get('name',''))}",
                   "name": it.get("name", ""), "card": None, "port": None,
                   "seed": random.randint(1, 2 ** 31 - 1),
                   "status": "queued", "file": None, "promptId": None, "error": None,
                   "queuedAt": time.time(), "startedAt": None,
                   "width": it.get("width") or imagegen.DEFAULT_W,
                   "height": it.get("height") or imagegen.DEFAULT_H,
                   "prompt": it["prompt"], "ref": it.get("ref")}
            with _lock:
                _jobs[jid] = job
                _order.append(jid)
            tasks.append({"id": jid, "run": _make_runner(project, ep, jid)})
    sched.submit("image", tasks)
    return {"batch": batch, "jobs": [t["id"] for t in tasks]}


def _make_runner(project: Path, ep: str, jid: str):
    def run(port: int):
        with _lock:
            job = dict(_jobs.get(jid) or {})
            if job.get("status") == "cancelled":
                return
            _jobs[jid]["status"] = "running"
            _jobs[jid]["startedAt"] = time.time()
            _jobs[jid]["port"] = port
            _jobs[jid]["card"] = port - 8188
        _execute(project, ep, jid, dict(_jobs[jid]))
    return run


def _execute(project: Path, ep: str, jid: str, job: dict):
    try:
        ref = None
        if job.get("ref") and (project / job["ref"]).exists():
            ref = _stage_ref(job["port"], project / job["ref"])
        g = imagegen.build_graph(job["prompt"], job["width"], job["height"],
                                 job["seed"], ref_image=ref)
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
                out = _job_dir(project, ep, job["dir"]) / f"{job['seed']}.png"
                out.write_bytes(data)
                with _lock:
                    _jobs[jid]["status"] = "done"
                    _jobs[jid]["file"] = _rel(project, out)
                    _jobs[jid]["seconds"] = round(time.time() - t0, 1)
                return
            time.sleep(3)
        raise RuntimeError("轮询超时")
    except Exception as e:
        with _lock:
            if jid in _jobs:
                _jobs[jid]["status"] = "failed"
                _jobs[jid]["error"] = f"{e}"


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
