#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shotcall 验收用的假 ComfyUI（stdlib only）。

只用于测试：监听 127.0.0.1 的**随机端口**（显式拒绝 8188–8195），
不加载模型、不碰 GPU、不读写真实项目。

支持的最小 API（够 vjobs/reconcile/取消/回片测试用）：

    GET  /system_stats          devices = 1 张假卡
    GET  /queue                 queue_running / queue_pending（测试可注入）
    GET  /history/{prompt_id}   单条历史
    GET  /history               全部历史
    POST /prompt                立即成功 / 立即失败 / 留在队列 / 延迟
    POST /upload/image          multipart 或 JSON，内存里记文件名与字节
    GET  /view                  返回一段假 mp4 字节
    POST /interrupt             记录中断次数并清空 running
    POST /queue                 {"delete":[pid]} 或 {"clear":true}（顺带支持）

用法（测试代码）：

    from fake_comfy import FakeComfy
    with FakeComfy() as fake:
        fake.stage("pid-1", graph, state="pending")
        ...

手动起一个：

    python3 app/fake_comfy.py --port 0
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 一小段带 ftyp box 的假 mp4，足够让调用方把它当视频字节落盘。
FAKE_MP4 = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
            b"\x00\x00\x00\x08free" + b"fake-comfy-video" * 16)

REAL_PORTS = range(8188, 8196)


class FakeComfy:
    """线程安全、可注入队列状态的假 ComfyUI。"""

    def __init__(self, host="127.0.0.1", port=0, mode="success", delay=0.0,
                 output_name=None):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("fake ComfyUI 只允许监听 127.0.0.1")
        if port and port in REAL_PORTS:
            raise ValueError(f"{port} 是真实 ComfyUI 端口段，拒绝使用")
        self.host = "127.0.0.1"
        self._requested_port = int(port or 0)
        self.mode = mode          # success | pending | running | fail | fail_history
        self.delay = float(delay)
        self.output_name = output_name or "fake-unit_00001_.mp4"

        self.lock = threading.RLock()
        self.prompts = {}         # prompt_id -> graph
        self.running_ids = []
        self.pending_ids = []
        self.history = {}         # prompt_id -> history entry
        self.interrupts = 0
        self.prompt_requests = []
        self.uploaded = {}        # name -> bytes
        self.view_requests = []
        self.deleted_ids = []
        self._seq = 0

        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------ 生命周期

    @property
    def port(self):
        if self._httpd is not None:
            return self._httpd.server_address[1]
        return self._requested_port

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def start(self):
        if self._httpd is not None:
            return self
        for _ in range(20):
            httpd = ThreadingHTTPServer((self.host, self._requested_port),
                                        _Handler)
            httpd.daemon_threads = True
            p = httpd.server_address[1]
            if p in REAL_PORTS or p == 0:
                httpd.server_close()
                self._requested_port = 0
                continue
            if self._requested_port == 0:
                # 随机端口一旦命中真实端口段，重新绑定
                self._requested_port = 0
            self._httpd = httpd
            self._httpd.fake = self
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            kwargs={"poll_interval": 0.05},
                                            daemon=True, name="fake-comfy")
            self._thread.start()
            return self
        raise RuntimeError("无法为 fake ComfyUI 申请到安全端口")

    def stop(self):
        if self._httpd is None:
            return
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    # ------------------------------------------------------------ 测试注入

    def stage(self, prompt_id, graph, state="pending"):
        """登记一个 ComfyUI 里已经存在的 prompt（不经过 POST /prompt）。"""
        if state not in ("pending", "running", "completed", "failed"):
            raise ValueError("state 只能是 pending/running/completed/failed")
        with self.lock:
            self.prompts[prompt_id] = graph or {}
            self.running_ids = [x for x in self.running_ids if x != prompt_id]
            self.pending_ids = [x for x in self.pending_ids if x != prompt_id]
            if state == "pending":
                self.pending_ids.append(prompt_id)
            elif state == "running":
                self.running_ids.append(prompt_id)
            elif state == "completed":
                self.history[prompt_id] = self._history_entry(prompt_id, True)
            else:
                self.history[prompt_id] = self._history_entry(prompt_id, False)
        return prompt_id

    def set_queue(self, running=None, pending=None):
        with self.lock:
            if running is not None:
                self.running_ids = list(running)
            if pending is not None:
                self.pending_ids = list(pending)

    def complete(self, prompt_id):
        with self.lock:
            self.running_ids = [x for x in self.running_ids if x != prompt_id]
            self.pending_ids = [x for x in self.pending_ids if x != prompt_id]
            self.history[prompt_id] = self._history_entry(prompt_id, True)
        return prompt_id

    def fail(self, prompt_id):
        with self.lock:
            self.running_ids = [x for x in self.running_ids if x != prompt_id]
            self.pending_ids = [x for x in self.pending_ids if x != prompt_id]
            self.history[prompt_id] = self._history_entry(prompt_id, False)
        return prompt_id

    def queue_of(self):
        """返回与 gpus.queue_of 同形的 dict（含 id 列表）。"""
        with self.lock:
            run = list(self.running_ids)
            pend = list(self.pending_ids)
        return {"running": len(run), "pending": len(pend),
                "promptId": run[0] if run else None,
                "runningIds": run, "pendingIds": pend}

    def queue_rows(self):
        with self.lock:
            run = [(i, pid, self.prompts.get(pid) or {})
                   for i, pid in enumerate(self.running_ids)]
            pend = [(i, pid, self.prompts.get(pid) or {})
                    for i, pid in enumerate(self.pending_ids)]
        return ([[i, pid, graph, {}, []] for i, pid, graph in run],
                [[i, pid, graph, {}, []] for i, pid, graph in pend])

    def _history_entry(self, prompt_id, completed):
        entry = {"prompt": [0, prompt_id, self.prompts.get(prompt_id) or {}],
                 "outputs": {}, "status": {"status_str": "success" if completed else "error",
                                           "completed": bool(completed)}}
        if completed:
            entry["outputs"] = {"14": {"images": [
                {"filename": self.output_name, "subfolder": "fake",
                 "type": "output"}]}}
        return entry

    # ------------------------------------------------------------ 内部工具

    def _new_prompt_id(self):
        with self.lock:
            self._seq += 1
            return f"fake-{uuid.uuid4().hex[:8]}-{self._seq}"

    def record_prompt(self, graph):
        with self.lock:
            self.prompt_requests.append(graph)
        pid = self._new_prompt_id()
        with self.lock:
            self.prompts[pid] = graph
            mode = self.mode
            if mode == "success":
                self.history[pid] = self._history_entry(pid, True)
            elif mode == "fail_history":
                self.history[pid] = self._history_entry(pid, False)
            elif mode == "pending":
                self.pending_ids.append(pid)
            elif mode == "running":
                self.running_ids.append(pid)
            elif mode == "fail":
                pass
        return pid


# ------------------------------------------------------------------ HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeComfyUI/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def fake(self):
        return self.server.fake

    def log_message(self, fmt, *args):   # 安静，测试输出不被刷屏
        pass

    # -- helpers ------------------------------------------------------
    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # -- GET ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/system_stats":
            total = 24 * 1024 ** 3
            self._json(200, {
                "system": {"os": "fake", "comfyui_version": "0.0-fake",
                           "python_version": "3.11", "pytorch_version": "fake"},
                "devices": [{"name": "Fake H3 GPU : cuda:0", "type": "cuda",
                             "index": 0, "vram_total": total,
                             "vram_free": int(total * 0.8),
                             "torch_vram_total": int(total * 0.8),
                             "torch_vram_free": int(total * 0.7)}],
            })
            return
        if path == "/queue":
            run, pend = self.fake.queue_rows()
            self._json(200, {"queue_running": run, "queue_pending": pend,
                             "queue_remaining": len(run) + len(pend),
                             "runningIds": list(self.fake.running_ids),
                             "pendingIds": list(self.fake.pending_ids)})
            return
        if path == "/history":
            with self.fake.lock:
                hist = {k: json.loads(json.dumps(v))
                        for k, v in self.fake.history.items()}
            q = parse_qs(parsed.query)
            try:
                limit = int((q.get("max_items") or ["0"])[0])
            except ValueError:
                limit = 0
            if limit > 0:
                hist = dict(list(hist.items())[-limit:])
            self._json(200, hist)
            return
        if path.startswith("/history/"):
            pid = path[len("/history/"):]
            with self.fake.lock:
                entry = self.fake.history.get(pid)
                out = {pid: json.loads(json.dumps(entry))} if entry else {}
            self._json(200, out)
            return
        if path == "/view":
            q = parse_qs(parsed.query)
            name = (q.get("filename") or [""])[0]
            with self.fake.lock:
                self.fake.view_requests.append(name)
            data = FAKE_MP4
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found", "path": path})

    # -- POST ---------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/prompt":
            raw = self._body()
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._json(400, {"error": "invalid json"})
                return
            graph = payload.get("prompt")
            if not isinstance(graph, dict) or not graph:
                self._json(400, {"error": "prompt 必须是对象"})
                return
            if self.fake.delay:
                time.sleep(self.fake.delay)
            if self.fake.mode == "fail":
                with self.fake.lock:
                    self.fake.prompt_requests.append(graph)
                self._json(400, {"error": "fake prompt rejected",
                                 "node_errors": {}, "prompt_id": None})
                return
            pid = self.fake.record_prompt(graph)
            self._json(200, {"prompt_id": pid, "number": 1,
                             "node_errors": {}})
            return
        if path == "/upload/image":
            raw = self._body()
            ctype = self.headers.get("Content-Type") or ""
            name, data = "upload.bin", raw
            if "multipart/form-data" in ctype:
                m = re.search(r"boundary=([^;]+)", ctype)
                boundary = m.group(1).strip().strip('"') if m else ""
                if boundary:
                    for part in raw.split(("--" + boundary).encode()):
                        head, sep, body = part.partition(b"\r\n\r\n")
                        if not sep:
                            continue
                        fm = re.search(rb'filename="([^"]*)"', head)
                        if fm:
                            name = fm.group(1).decode("utf-8", "replace")
                            data = body[:-2] if body.endswith(b"\r\n") else body
                            break
            else:
                try:
                    obj = json.loads(raw.decode("utf-8") or "{}")
                    name = obj.get("name") or name
                except Exception:
                    pass
            with self.fake.lock:
                self.fake.uploaded[name] = data
            self._json(200, {"name": name, "subfolder": "", "type": "input"})
            return
        if path == "/interrupt":
            self._body()
            with self.fake.lock:
                self.fake.interrupts += 1
                for pid in list(self.fake.running_ids):
                    self.fake.history[pid] = self.fake._history_entry(pid, False)
                self.fake.running_ids = []
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/queue":
            raw = self._body()
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            with self.fake.lock:
                if obj.get("clear"):
                    self.fake.deleted_ids.extend(self.fake.running_ids +
                                                 self.fake.pending_ids)
                    self.fake.running_ids = []
                    self.fake.pending_ids = []
                for pid in obj.get("delete") or []:
                    self.fake.deleted_ids.append(pid)
                    self.fake.running_ids = [x for x in self.fake.running_ids
                                             if x != pid]
                    self.fake.pending_ids = [x for x in self.fake.pending_ids
                                             if x != pid]
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"error": "not found", "path": path})


def main():
    ap = argparse.ArgumentParser(description="Shotcall fake ComfyUI")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--mode", default="success",
                    choices=("success", "pending", "running", "fail", "fail_history"))
    ap.add_argument("--delay", type=float, default=0.0)
    a = ap.parse_args()
    if a.port in REAL_PORTS:
        raise SystemExit(f"{a.port} 是真实 ComfyUI 端口段，fake 拒绝使用")
    fake = FakeComfy(port=a.port, mode=a.mode, delay=a.delay).start()
    print(f"FAKE_COMFY {fake.base_url}", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
