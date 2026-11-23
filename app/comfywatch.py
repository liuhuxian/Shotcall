# -*- coding: utf-8 -*-
"""实例状态监听：WebSocket 实时进度 + 队列轮询 + 显存采样。

只监听**已启动**的实例（gpus.probe() 里非 offline 的卡）。
本模块不启停实例，只读；启停由 comfyctl 负责。
"""
import json
import subprocess
import threading
import time

import gpus

_lock = threading.Lock()
_state = {}          # port -> {"progress":{value,max}, "executing":pid, "queue":{run,pend}, "error":None}
_seen = set()
_vram = {}           # card -> {"usedGB","totalGB","ts"}
_vram_busy = False

H3_PY = "/checkpoints/huxian/h3/env/bin/python"


def _s(port):
    with _lock:
        return _state.setdefault(port, {"progress": None, "executing": None,
                                        "queue": {"run": 0, "pend": 0}, "error": None,
                                        "updated": 0})


def _watch_ws(port: int):
    from websockets.sync.client import connect
    cid = f"watch-{port}"
    while True:
        try:
            with connect(f"ws://127.0.0.1:{port}/ws?clientId={cid}", open_timeout=5,
                         close_timeout=2, max_size=None) as ws:
                _s(port)["error"] = None
                while True:
                    raw = ws.recv(timeout=30)
                    if isinstance(raw, bytes):
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    t = msg.get("type")
                    d = msg.get("data") or {}
                    st = _s(port)
                    if t == "progress":
                        st["progress"] = {"value": d.get("value"), "max": d.get("max")}
                        st["updated"] = time.time()
                    elif t == "executing":
                        st["executing"] = d.get("prompt_id")
                        if d.get("node") is None:
                            st["progress"] = None
                        st["updated"] = time.time()
                    elif t == "execution_error":
                        st["error"] = (d.get("exception_message") or "执行失败")[:200]
                        st["updated"] = time.time()
                    elif t == "execution_success":
                        st["progress"] = None
                        st["executing"] = None
                        st["updated"] = time.time()
        except Exception as e:
            st = _s(port)
            st["error"] = f"ws 断开：{str(e)[:60]}"
            st["executing"] = None
            st["progress"] = None
            time.sleep(3)


def _poll_queues():
    while True:
        try:
            cards = [c for c in gpus.probe() if c["state"] != "offline"]
            for c in cards:
                port = c["port"]
                if port not in _seen:
                    _seen.add(port)
                    threading.Thread(target=_watch_ws, args=(port,), daemon=True).start()
                q = gpus.queue_of(port)
                st = _s(port)
                st["queue"] = {"run": q["running"], "pend": q["pending"]}
                if q.get("promptId") and not st.get("executing"):
                    st["executing"] = q["promptId"]
        except Exception:
            pass
        time.sleep(3)


def _sample_vram():
    global _vram_busy
    code = ("import torch,json\n"
            "o={}\n"
            "for i in range(torch.cuda.device_count()):\n"
            "    try:\n"
            "        f,t=torch.cuda.mem_get_info(i); o[str(i)]={'used':(t-f)/2**30,'total':t/2**30}\n"
            "    except Exception as e: o[str(i)]={'err':str(e)[:40]}\n"
            "print(json.dumps(o))\n")
    while True:
        if not _vram_busy:
            _vram_busy = True
            try:
                out = subprocess.run([H3_PY, "-c", code], capture_output=True, timeout=90)
                data = json.loads(out.stdout.decode() or "{}")
                with _lock:
                    _vram.clear()
                    for k, v in data.items():
                        if "used" in v:
                            _vram[int(k)] = {"usedGB": round(v["used"], 1),
                                             "totalGB": round(v["total"], 1), "ts": time.time()}
            except Exception:
                pass
            _vram_busy = False
        time.sleep(8)


def instance_state(port: int) -> dict:
    st = _s(port)
    return {"progress": st.get("progress"), "executing": st.get("executing"),
            "queue": st.get("queue"), "error": st.get("error")}


def vram(card: int) -> dict:
    with _lock:
        return _vram.get(card, {})


def running() -> dict:
    with _lock:
        return {p: dict(v) for p, v in _state.items()}


threading.Thread(target=_poll_queues, daemon=True).start()
threading.Thread(target=_sample_vram, daemon=True).start()
