# -*- coding: utf-8 -*-
"""ComfyUI 实例与显卡状态探测。

约定：**端口即卡号**——8188 对应卡 0，8189 对应卡 1，……，8195 对应卡 7。
ComfyUI 在 /system_stats 里报的 index 是相对自己可见设备的（用 CUDA_VISIBLE_DEVICES
隔离后恒为 0），不能用来判断物理卡，所以以端口为准。
"""
import json
import time
import urllib.request

PORTS = list(range(8188, 8196))   # 8188..8195，最多 8 张卡

# 能装下 H3 Image 的剩余显存下限（GB）。
# 4MP 出图时 UNet+CLIP+VAE 常驻约 24–28 GB，留一点余量。
LOAD_MIN_GB = 30.0
GREEN_PCT = 10.0                   # 占用低于 10% 视为完全空闲


def _get(port: int, path: str, timeout: float = 1.2):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


_PROBE_CACHE = {"at": 0.0, "data": None}


def probe(ttl: float = 0.0) -> list:
    """返回所有能连上的实例所对应的卡，按卡号排序。ttl>0 时复用缓存。"""
    if ttl and _PROBE_CACHE["data"] is not None and time.time() - _PROBE_CACHE["at"] < ttl:
        return _PROBE_CACHE["data"]
    found = {}
    for port in PORTS:
        try:
            st = _get(port, "/system_stats")
        except Exception:
            continue
        qlen = 0
        try:
            q = _get(port, "/queue")
            qlen = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
        except Exception:
            pass
        for dev in st.get("devices", []):
            idx = port - PORTS[0]        # 端口即卡号
            if idx < 0:
                continue
            total = dev.get("vram_total") or 0
            free = dev.get("vram_free") or 0
            used_pct = round((total - free) / total * 100, 1) if total else 0.0
            free_gb = round(free / 2 ** 30, 1)
            if qlen > 0:
                state = "busy"          # 有 ComfyUI 任务在跑/排队，不能再当“可加载”
            elif used_pct < GREEN_PCT:
                state = "free"          # 绿色：完全空闲
            elif free_gb >= LOAD_MIN_GB:
                state = "loadable"      # 橙色：有占用但装得下
            else:
                state = "busy"          # 红色：装不下
            found[idx] = {
                "index": idx, "port": port, "state": state,
                "name": (dev.get("name") or "").split(" :")[0],
                "vramTotalGB": round(total / 2 ** 30, 1),
                "vramFreeGB": free_gb, "usedPct": used_pct, "queue": qlen,
            }
    # 8 张卡全部返回，没有实例的标 offline
    out = []
    for idx in range(8):
        out.append(found.get(idx) or {
            "index": idx, "port": PORTS[0] + idx, "state": "offline",
            "name": "（无实例）", "vramTotalGB": 0, "vramFreeGB": 0,
            "usedPct": 0, "queue": 0,
        })
    _PROBE_CACHE["data"] = out
    _PROBE_CACHE["at"] = time.time()
    return out


def queue_of(port: int) -> dict:
    """返回 {running: 数量, pending: 数量, promptId: 当前任务}"""
    try:
        q = _get(port, "/queue", timeout=0.8)
    except Exception:
        return {"running": 0, "pending": 0, "promptId": None}
    run = q.get("queue_running") or []
    pend = q.get("queue_pending") or []
    def _pid(x):
        try:
            return x[1] if isinstance(x, (list, tuple)) and len(x) > 1 else None
        except Exception:
            return None
    pid = _pid(run[0]) if run else None
    return {"running": len(run), "pending": len(pend), "promptId": pid,
            "runningIds": [p for p in (_pid(x) for x in run) if p],
            "pendingIds": [p for p in (_pid(x) for x in pend) if p]}


def is_night(now=None) -> bool:
    """21:00–09:00 为夜间时段（本机时区）。"""
    now = now or time.localtime()
    return now.tm_hour >= 21 or now.tm_hour < 9


def default_selection(cards: list, now=None) -> list:
    """夜间默认选所有空闲/可加载的卡；其余时间默认只选卡 7。"""
    if is_night(now):
        return [c["index"] for c in cards if c["state"] in ("free", "loadable")]
    return [c["index"] for c in cards if c["index"] == 7]


def pick_port(cards: list, index: int):
    for c in cards:
        if c["index"] == index:
            return c["port"]
    return None
