#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成执行器：提交 → 轮询 → 回片 → 记录。

设计原则（对应流程文件 §9）：
- 默认不提交；提交必须 --execute --yes
- 状态一律追加到 run/runs.jsonl，不覆盖历史
- 网络/损坏可重试（默认 2 次）；内容不满意不自动重抽
- 连续失败 3 次 / 超时 / 达上限 → 停止并汇总

用法：
    python3 runner.py --simulate <prompt_id> --shot SHOT-...   # 只验证回片链路
    python3 runner.py --execute --yes --tasks TASK-a,TASK-b     # 真提交
"""
import argparse
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from store import state_dir

PROJECT = Path.home() / "short_v/有谁共鸣2"

def _comfy_base():
    """优先用探测到的实例（端口即卡号），否则回落 8188。"""
    try:
        import gpus
        cards = gpus.probe()
        if cards:
            return f"http://127.0.0.1:{cards[0]['port']}"
    except Exception:
        pass
    return "http://127.0.0.1:8188"


COMFY = _comfy_base()
VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov")


def _http(path, data=None, timeout=120, raw=False):
    url = COMFY + path
    req = urllib.request.Request(
        url, data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return body if raw else json.loads(body.decode() or "{}")


def health():
    st = _http("/system_stats", timeout=10)
    return {"comfy": st["system"]["comfyui_version"], "devices": [d["name"] for d in st.get("devices", [])]}


def submit(graph, client="director-workbench"):
    r = _http("/prompt", {"prompt": graph, "client_id": client})
    return r["prompt_id"]


def history(pid):
    h = _http(f"/history/{pid}", timeout=30)
    return h.get(pid)


def outputs_of(entry):
    files = []
    for node in (entry.get("outputs") or {}).values():
        for key in ("videos", "images", "gifs"):
            for v in (node.get(key) or []):
                if str(v.get("filename", "")).lower().endswith(VIDEO_EXT):
                    files.append(v)
    return files


def download(entry, dest: Path) -> list:
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for v in outputs_of(entry):
        q = urllib.parse.urlencode({"filename": v["filename"],
                                    "subfolder": v.get("subfolder", ""),
                                    "type": v.get("type", "output")})
        data = _http(f"/view?{q}", timeout=300, raw=True)
        p = dest / v["filename"]
        p.write_bytes(data)
        saved.append({"path": str(p), "bytes": len(data), "probe": probe(p)})
    return saved


def probe(p: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", str(p)], capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out or "{}")
        streams = d.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = any(s.get("codec_type") == "audio" for s in streams)
        return {"duration": round(float(d.get("format", {}).get("duration", 0)), 2),
                "w": v.get("width"), "h": v.get("height"), "hasAudio": a}
    except Exception as e:
        return {"error": str(e)}


def wait(pid, timeout=3600, interval=5, on_tick=None):
    t0 = time.time()
    while True:
        e = history(pid)
        if e and e.get("status", {}).get("status_str") in ("success", "error"):
            return e, time.time() - t0
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{pid} 超时 {timeout}s")
        if on_tick:
            on_tick(time.time() - t0)
        time.sleep(interval)


def log(project, rec):
    p = state_dir(project) / "runs.jsonl"
    rec = dict(rec, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def human(n):
    for unit in ("s", "min", "h"):
        if n < 60 or unit == "h":
            return f"{n:.0f}{unit}" if unit != "s" else f"{n:.1f}s"
        n /= 60


def run_batch(project, tasks, dest_root, max_network_retry=2, stop_after_fail=3,
              timeout=3600, log_fn=print):
    fail_streak, done, failed = 0, [], []
    for t in tasks:
        attempt = 0
        while True:
            try:
                pid = submit(t["graph"])
                log(project, {"task": t["id"], "shot": t["shot"], "event": "submitted", "promptId": pid})
                log_fn(f"  提交 {t['id']} → {pid}")
                entry, el = wait(pid, timeout, on_tick=lambda s: log_fn(f"    [{human(s)}] 生成中…"))
                if entry["status"]["status_str"] != "success":
                    raise RuntimeError(json.dumps(entry["status"], ensure_ascii=False)[:400])
                files = download(entry, dest_root / t["shot"])
                log(project, {"task": t["id"], "shot": t["shot"], "event": "succeeded",
                              "promptId": pid, "seconds": round(el, 1), "files": files})
                done.append({"task": t["id"], "files": files, "seconds": el})
                fail_streak = 0
                break
            except Exception as e:
                attempt += 1
                log(project, {"task": t["id"], "event": "failed", "attempt": attempt, "error": str(e)})
                log_fn(f"  ✕ {t['id']} 第 {attempt} 次失败：{e}")
                if attempt > max_network_retry:
                    failed.append({"task": t["id"], "error": str(e)})
                    fail_streak += 1
                    break
                time.sleep(3)
        if fail_streak >= stop_after_fail:
            log_fn(f"  连续失败 {fail_streak} 次，停止本批")
            log(project, {"event": "batch_stopped", "reason": f"连续失败{fail_streak}次"})
            break
    return {"done": done, "failed": failed}


def simulate(project, pid, shot, out_dir):
    log_fn = print
    log_fn(f"[simulate] 用已有记录验证回片链路：{pid}")
    e = history(pid)
    if not e:
        log_fn("[x] 该 prompt_id 不在 history 里")
        return 1
    if e["status"]["status_str"] != "success":
        log_fn("[x] 该记录不是 success")
        return 1
    files = download(e, Path(out_dir) / shot)
    for f in files:
        log_fn(f"  已下载 {Path(f['path']).name} · {f['bytes']/1e6:.2f} MB · {f['probe']}")
    log(project, {"task": "SIMULATE", "shot": shot, "event": "succeeded",
                  "promptId": pid, "files": files, "simulated": True})
    log_fn(f"  记录已追加到 runs.jsonl（标记 simulated=true）")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", metavar="PROMPT_ID")
    ap.add_argument("--shot", default="SHOT-DEMO")
    ap.add_argument("--out", default=str(state_dir(PROJECT) / "media"))
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--tasks", default="")
    a = ap.parse_args()

    print("ComfyUI:", health())
    if a.simulate:
        return simulate(PROJECT, a.simulate, a.shot, a.out)

    if not a.execute:
        print("[dry] 未提交。要真跑：--execute --yes --tasks TASK-xxx")
        return 0
    if not a.yes:
        print("[x] --execute 需要再加 --yes")
        return 2

    ids = [x.strip() for x in a.tasks.split(",") if x.strip()]
    path = state_dir(PROJECT) / "tasks.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    tasks = [r for r in rows if r["id"] in ids]
    if not tasks:
        print("[x] 没有匹配的任务")
        return 1
    print(f"准备执行 {len(tasks)} 条，输出 → {state_dir(PROJECT)/'media'}")
    res = run_batch(PROJECT, tasks, state_dir(PROJECT) / "media")
    print(f"完成 {len(res['done'])} · 失败 {len(res['failed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
