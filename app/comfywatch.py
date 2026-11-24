# -*- coding: utf-8 -*-
"""实例状态监听：WebSocket 实时进度 + 队列轮询 + 显存采样。

只监听**已启动**的实例（gpus.probe() 里非 offline 的卡）。
本模块不启停实例，只读；启停由 comfyctl 负责。

显存通过 ``nvidia-smi`` 读取（每 3 秒一次，timeout=2），不加载任何 GPU 计算库、
不起额外的 Python 子进程，避免采样本身和生成抢 GPU/CPU；采样失败时保留上一次
的值并记录 ``_vram_error``。后台线程不随 import 启动，由服务进程调用
``start()``（幂等）后才开始轮询/采样。
"""
import json
import subprocess
import threading
import time

import gpus

_lock = threading.Lock()
_state = {}          # port -> {"progress":{value,max}, "executing":pid, "queue":{run,pend}, "error":None}
_seen = set()
_vram = {}           # card -> {"usedGB","totalGB","pct","util","ts"}（pct=显存占用%，util=GPU 利用率%）
_vram_error = None   # 最近一次显存采样失败原因；成功时清空
_vram_busy = False
_started = False
_start_lock = threading.Lock()

VRAM_INTERVAL = 3.0  # 与 UI 3 秒轮询一致
VRAM_CMD = ["nvidia-smi",
            "--query-gpu=index,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits"]


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


def _parse_vram_csv(text: str) -> dict:
    """解析 ``nvidia-smi --query-gpu`` 的 CSV 输出（index, usedMiB, totalMiB, util%）。

    纯解析函数：不读进程、不调子进程、不碰 GPU。无法解析的行（如 ``[N/A]``）跳过。
    """
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            card = int(float(parts[0]))
            used = float(parts[1])
            total = float(parts[2])
            util = float(parts[3])
        except (TypeError, ValueError):
            continue
        if total <= 0 or used < 0:
            continue
        out[card] = {
            "usedGB": round(used / 1024.0, 1),
            "totalGB": round(total / 1024.0, 1),
            "pct": round(used / total * 100.0, 1),
            "util": round(util, 1),
        }
    return out


def _sample_vram_once() -> bool:
    """执行一次 ``nvidia-smi`` 采样并更新 ``_vram``。

    成功返回 True；任何失败（超时/无命令/退出码非 0/输出不可解析）都只记录
    ``_vram_error`` 并返回 False，保留上一次的有效值，不抛异常。
    """
    global _vram_error
    try:
        out = subprocess.run(VRAM_CMD, capture_output=True, timeout=2)
        if out.returncode != 0:
            err = out.stderr or b""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            raise RuntimeError("nvidia-smi 退出码 %s：%s"
                               % (out.returncode, (err or "").strip()[:120]))
        raw = out.stdout or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        parsed = _parse_vram_csv(raw)
        if not parsed:
            raise ValueError("nvidia-smi 输出无有效行")
        now = time.time()
        for v in parsed.values():
            v["ts"] = now
        with _lock:
            _vram.clear()
            _vram.update(parsed)
            _vram_error = None
        return True
    except Exception as e:
        with _lock:
            _vram_error = "%s: %s" % (type(e).__name__, str(e)[:160])
        return False


def _sample_vram():
    global _vram_busy
    while True:
        t0 = time.time()
        if not _vram_busy:
            _vram_busy = True
            try:
                _sample_vram_once()
            except Exception:
                pass          # _sample_vram_once 已兜底；这里保证线程不死
            finally:
                _vram_busy = False
        # 按「开始到开始」保持 3 秒节拍，nvidia-smi 慢时不额外拉长间隔
        time.sleep(max(0.1, VRAM_INTERVAL - (time.time() - t0)))


def instance_state(port: int) -> dict:
    st = _s(port)
    return {"progress": st.get("progress"), "executing": st.get("executing"),
            "queue": st.get("queue"), "error": st.get("error")}


def vram(card: int) -> dict:
    with _lock:
        return _vram.get(card, {})


def vram_error():
    """最近一次显存采样失败原因；成功时为 None。只读。"""
    with _lock:
        return _vram_error


def running() -> dict:
    with _lock:
        return {p: dict(v) for p, v in _state.items()}


def start() -> bool:
    """幂等启动后台线程（队列轮询 + 显存采样）；import 本模块不再自动启动。

    返回是否真的启动了线程：第一次 True，之后 False。
    """
    global _started
    with _start_lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_poll_queues, daemon=True).start()
    threading.Thread(target=_sample_vram, daemon=True).start()
    return True
