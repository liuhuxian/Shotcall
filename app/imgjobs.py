# -*- coding: utf-8 -*-
"""出图任务：提交、轮询、落盘。内存态，重启后不保留未完成任务。"""
import json
import random
import threading
import time
import traceback
from pathlib import Path

import imagegen
import store

_lock = threading.Lock()
_jobs = {}          # job_id -> dict
_order = []


def _job_dir(project: Path, ep: str, asset_id: str) -> Path:
    """候选图落到状态目录（项目目录只读），与视频候选一致。"""
    d = store.state_dir(project) / "img" / ep / asset_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_jobs(batch: str = None) -> list:
    with _lock:
        items = [_jobs[j] for j in _order if not batch or _jobs[j]["batch"] == batch]
    return items


def clear_finished() -> int:
    with _lock:
        kill = [j for j in _order if _jobs[j]["status"] in ("done", "failed")]
        for j in kill:
            _jobs.pop(j, None)
        _order[:] = [j for j in _order if j in _jobs]
    return len(kill)


def submit_batch(project: Path, ep: str, items: list, cards: list, ports: dict) -> dict:
    """items: [{id,name,prompt,ref,n,width,height}]；cards: 卡号列表；ports: 卡号→端口。"""
    batch = time.strftime("IMG-%Y%m%d-%H%M%S")
    created = []
    for it in items:
        n = max(1, int(it.get("n") or 4))
        for k in range(n):
            jid = f"{batch}-{it['id']}-{k+1}"
            card = cards[(len(created)) % len(cards)] if cards else None
            port = ports.get(card)
            job = {"id": jid, "batch": batch, "asset": it["id"], "name": it.get("name", ""),
                   "card": card, "port": port, "seed": random.randint(1, 2 ** 31 - 1),
                   "status": "queued", "file": None, "promptId": None, "error": None,
                   "width": it.get("width") or imagegen.DEFAULT_W,
                   "height": it.get("height") or imagegen.DEFAULT_H,
                   "prompt": it["prompt"], "ref": it.get("ref")}
            with _lock:
                _jobs[jid] = job
                _order.append(jid)
            created.append(jid)
    threading.Thread(target=_run, args=(project, ep, created), daemon=True).start()
    return {"batch": batch, "jobs": created}


def _run(project: Path, ep: str, job_ids: list):
    for jid in job_ids:
        with _lock:
            job = dict(_jobs.get(jid) or {})
        if not job or not job.get("port"):
            with _lock:
                if jid in _jobs:
                    _jobs[jid]["status"] = "failed"
                    _jobs[jid]["error"] = "没有可用的卡"
            continue
        try:
            with _lock:
                _jobs[jid]["status"] = "running"
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
                    out = _job_dir(project, ep, job["asset"]) / f"{job['seed']}.png"
                    out.write_bytes(data)
                    with _lock:
                        _jobs[jid]["status"] = "done"
                        _jobs[jid]["file"] = str(out.relative_to(store.state_dir(project)))
                        _jobs[jid]["seconds"] = round(time.time() - t0, 1)
                    break
                time.sleep(3)
            else:
                raise RuntimeError("轮询超时")
        except Exception as e:
            with _lock:
                if jid in _jobs:
                    _jobs[jid]["status"] = "failed"
                    _jobs[jid]["error"] = f"{e}"
            traceback.print_exc()


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
