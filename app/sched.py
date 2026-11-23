# -*- coding: utf-8 -*-
"""显卡后端：唯一的显卡持有者。

页面（出图/出视频）只负责把任务丢进队列，不再指定用哪张卡；
这里负责分配、启动实例、并发上限、空闲回收。
"""
import threading
import time
import traceback

import comfyctl
import gpus

_lock = threading.Lock()
_state = {
    "enabled": None,      # 允许使用的卡号集合；None 表示按默认初始化
    "draining": set(),    # 队列清空后关闭
    "assign": {},         # 卡号 -> 当前任务的 kind（仅用于展示）
    "jobs": {},           # 卡号 -> 正在跑的 job id 集合
    "manual": set(),      # 用户手动开启/指定的卡，白天不自动回收
    "disabled": set(),    # 被显式停用的卡（即使实例在线也不派活）
    "autoscaled": set(),  # 由调度器自己扩出来的卡，白天闲时可以回收
}
_pending = []             # 等待分配的任务（dict: id, kind, run()）
_runners = {}             # job_id -> callable(port) 真正干活的函数


def _enabled() -> set:
    """有实例在线就默认可用；只有一张都没开时，才按夜间/白天策略给默认卡。"""
    online = {c["index"] for c in gpus.probe() if c["state"] != "offline"}
    if _state["enabled"] is None:
        _state["enabled"] = set(online) or set(gpus.default_selection(gpus.probe()))
    _state["enabled"] |= (online - _state["disabled"])      # 新上线的自动纳入
    _state["enabled"] -= _state["disabled"]
    return _state["enabled"]


def snapshot() -> dict:
    cards = gpus.probe()
    with _lock:
        enabled = sorted(_enabled())
        draining = sorted(_state["draining"])
        busy = {k: sorted(v) for k, v in _state["jobs"].items() if v}
    for c in cards:
        c["enabled"] = c["index"] in enabled
        c["draining"] = c["index"] in draining
        c["running"] = len(busy.get(c["index"], []))
        if c["state"] != "offline":
            q = gpus.queue_of(c["port"])
            c["comfyRunning"] = q["running"]
            c["comfyPending"] = q["pending"]
    return {"cards": cards, "enabled": enabled, "draining": draining,
            "manual": sorted(_state["manual"]),
            "night": gpus.is_night(), "loadMinGB": gpus.LOAD_MIN_GB,
            "pending": len(_pending)}


def set_cards(want: list) -> dict:
    """页面不再调用；保留给管理接口与状态栏用。"""
    want = set(int(x) for x in want)
    cur = set(_enabled())
    probes = {c["index"]: c for c in gpus.probe()}
    stopped, draining = [], []
    for idx in cur - want:
        c = probes.get(idx)
        if c and c["state"] != "offline":
            q = gpus.queue_of(c["port"])
            mine = _state["jobs"].get(idx) or set()
            if q["running"] or q["pending"] or mine:
                _state["draining"].add(idx)
                draining.append(idx)
                continue
            if comfyctl.stop(idx).get("stopped"):
                stopped.append(idx)
        cur.discard(idx)
    _state["manual"] |= want                  # 确认过的卡视为手动指定，白天不回收
    _state["disabled"] -= want                # 重新启用的从停用集合里移除
    _state["disabled"] |= (set(cur) - want)   # 被移出的记下来，避免自动纳入
    for idx in want - cur:
        cur.add(idx)
    with _lock:
        _state["enabled"] = cur
    return {"enabled": sorted(cur), "stopped": stopped, "draining": draining}


def _least_loaded(cards: list, enabled: set) -> int:
    cand = [c for c in cards
            if c["index"] in enabled and c["index"] not in _state["draining"]]
    if not cand:
        return None
    return min(cand, key=lambda c: (len(_state["jobs"].get(c["index"], ())),
                                    c["state"] == "offline"))["index"]


def submit(kind: str, items: list) -> list:
    """items: [{'id':..., 'run': callable(port)->bool}]，返回任务 id 列表。"""
    ids = []
    with _lock:
        for it in items:
            _runners[it["id"]] = it["run"]
            _pending.append({"id": it["id"], "kind": kind})
            ids.append(it["id"])
    _wake()
    return ids


_wakeup = threading.Event()


def _wake():
    _wakeup.set()


def cancel(ids: list) -> int:
    n = 0
    ids = set(ids)
    with _lock:
        keep = []
        for p in _pending:
            if p["id"] in ids:
                n += 1
                _runners.pop(p["id"], None)
                continue
            keep.append(p)
        _pending[:] = keep
    return n


def _busy_cards() -> set:
    with _lock:
        return {k for k, v in _state["jobs"].items() if v}


def _autoscale(cards: list) -> None:
    """队列积压时自动扩卡：夜间不限，白天积压 ≥8 项才扩。"""
    with _lock:
        pending = len(_pending)
        enabled = set(_enabled())
    if pending <= 0:
        return
    busy = _busy_cards()
    free = [c for c in cards
            if c["index"] not in enabled and c["index"] not in _state["draining"]
            and c["index"] not in busy]
    if not free:
        return
    if not gpus.is_night() and pending < 8:
        return
    need = min(len(free), max(1, pending - len(busy)))
    with _lock:
        for c in free[:need]:
            _enabled().add(c["index"])
            _state["autoscaled"].add(c["index"])


def _run_one(item: dict, idx: int) -> None:
    port = comfyctl.BASE_PORT + idx
    with _lock:
        _state["jobs"].setdefault(idx, set()).add(item["id"])
        _state["assign"][idx] = item["kind"]
    try:
        if not comfyctl._alive(port):
            comfyctl.start(idx)
        _runners[item["id"]](port)
    except Exception:
        traceback.print_exc()
    finally:
        with _lock:
            _state["jobs"].get(idx, set()).discard(item["id"])
            _runners.pop(item["id"], None)


def _worker():
    """调度器：每张空闲卡各跑一个任务，任务结束立刻补位。"""
    while True:
        try:
            cards = gpus.probe()
            _autoscale(cards)
            busy = _busy_cards()
            with _lock:
                enabled = set(_enabled())
            free = [c for c in cards
                    if c["index"] in enabled and c["index"] not in busy
                    and c["index"] not in _state["draining"]]
            with _lock:
                batch = [_pending.pop(0) for _ in range(min(len(free), len(_pending)))]
            for item, card in zip(batch, free):
                threading.Thread(target=_run_one, args=(item, card["index"]), daemon=True).start()
            if not batch:
                time.sleep(1.5)
        except Exception:
            traceback.print_exc()
            time.sleep(2)


def _housekeeping():
    """待关闭的卡：队列空了就关；白天批次结束后收掉自动扩出来的卡。"""
    while True:
        try:
            probes = {c["index"]: c for c in gpus.probe()}
            # 白天：队列清空且没有在跑的任务时，把非默认卡标记为待关闭
            if not gpus.is_night():
                with _lock:
                    idle = not _pending and not any(_state["jobs"].values())
                if idle:
                    # 只回收"调度器自己扩出来的卡"，用户手动开的实例不动
                    for idx in (_state["autoscaled"] - _state["manual"]):
                        _state["draining"].add(idx)
            for idx in list(_state["draining"]):
                c = probes.get(idx)
                if not c or c["state"] == "offline":
                    _state["draining"].discard(idx)
                    _enabled().discard(idx)
                    continue
                q = gpus.queue_of(c["port"])
                if q["running"] == 0 and q["pending"] == 0 and not _state["jobs"].get(idx):
                    comfyctl.stop(idx)
                    _state["draining"].discard(idx)
                    _enabled().discard(idx)
                    _state["autoscaled"].discard(idx)
        except Exception:
            traceback.print_exc()
        time.sleep(5)


threading.Thread(target=_worker, daemon=True).start()
threading.Thread(target=_housekeeping, daemon=True).start()
