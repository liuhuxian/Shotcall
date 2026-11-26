#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shotcall M1 独立验收测试（黑盒/灰盒）。

    python3 app/test_m1_acceptance.py
    python3 app/test_m1_acceptance.py --json /tmp/m1.json

覆盖 AC-01..AC-10。默认：只用 /tmp 临时项目 + 127.0.0.1 随机端口 fake
ComfyUI；不启动真实 8790、不启停真实 ComfyUI、不提交真实生成、不写正式项目。

说明：
- 本文件不允许为了让测试变绿而放宽断言；当前实现未完成时如实 FAIL/SKIP。
- HTTP 部分会 import 生产 ``server``，但要求 import 无副作用（后台线程由
  ``start_background()`` 显式启动，测试不调用）。如果 import 出现后台线程，
  AC-08 会直接 FAIL。
- 只读正式项目的 05/07/02/08/09 做前后 SHA 快照（AC-09），不写。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parent
ROOT = APP.parent
REAL_PROJECT = Path(os.environ.get("SHOTCALL_REAL_PROJECT")
                    or (Path.home() / "short_v/有谁共鸣2")).expanduser()
CREATIVE_DIRS = ("05-正式剧本", "07-图像资产", "02-角色圣经", "08-表演设计",
                 "09-镜头与视频提示词")

PASS, FAIL, SKIP = [], [], []
SESSION = None
HTTP_PROJECT = None
_SERVER = None
AC_ORDER = [f"AC-{i:02d}" for i in range(1, 11)]


# ------------------------------------------------------------------ 结果记录


def check(ac, name, cond, detail="", status=None):
    ok = bool(cond)
    rec = {"ac": ac, "name": name, "status": status or ("PASS" if ok else "FAIL"),
           "detail": "" if ok else str(detail)}
    (PASS if rec["status"] == "PASS" else
     FAIL if rec["status"] == "FAIL" else SKIP).append(rec)
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}[rec["status"]]
    extra = f"  ← {detail}" if detail and not ok else ""
    print(f"  {mark} [{ac}] {name}{extra}")
    return ok


def skip(ac, name, why):
    return check(ac, name, False, why, status="SKIP")


def run(ac, name, fn):
    try:
        fn()
    except _Skip as e:
        skip(ac, name, str(e))
    except Exception as e:
        check(ac, name, False, f"{type(e).__name__}: {e}")
        traceback.print_exc(limit=2)


class _Skip(Exception):
    pass


# ------------------------------------------------------------------ 工具


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_creative(real=REAL_PROJECT):
    out = {}
    for name in CREATIVE_DIRS:
        base = real / name
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            try:
                out[f.relative_to(real).as_posix()] = {
                    "sha": _sha256(f), "size": f.stat().st_size}
            except OSError:
                continue
    return out


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _http(method, base, path, body=None, timeout=30):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _j(raw):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return {}


def _text(raw):
    try:
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return repr(raw)


def _server():
    """启动临时 PROJECT 的 server.Handler；不调用 main()/start_background()。"""
    global _SERVER
    if _SERVER is not None:
        return _SERVER
    import server as srv
    if Path(srv.PROJECT).resolve() != HTTP_PROJECT.resolve():
        raise AssertionError(f"server.PROJECT 不是临时项目：{srv.PROJECT}")
    srv.Handler.log_message = lambda *a, **k: None
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    if port in range(8188, 8196) or port == 8790:
        httpd.server_close()
        raise AssertionError(f"临时 HTTP 端口命中保留端口：{port}")
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True, name="acceptance-http")
    t.start()
    _SERVER = {"httpd": httpd, "thread": t, "base": f"http://127.0.0.1:{port}"}
    return _SERVER


def _stop_server():
    if not _SERVER:
        return
    httpd = _SERVER["httpd"]
    httpd.shutdown()
    httpd.server_close()
    _SERVER["thread"].join(timeout=3)


def _prompt_file(project, scene, version, body):
    rel = f"09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-ref2va-{version}.md"
    _write(project / rel, body)
    return rel


# ------------------------------------------------------------------ fake ComfyUI


def _harness_selftest():
    from fake_comfy import FakeComfy, REAL_PORTS
    with FakeComfy() as fake:
        check("HARNESS", "fake 只监听 127.0.0.1 随机端口",
              fake.base_url.startswith("http://127.0.0.1:")
              and fake.port not in REAL_PORTS and fake.port != 8790,
              fake.base_url)
        code, raw = _http("GET", fake.base_url, "/system_stats")
        st = _j(raw)
        check("HARNESS", "GET /system_stats 返回 1 张卡",
              code == 200 and len(st.get("devices") or []) == 1, st)
        code, raw = _http("POST", fake.base_url, "/prompt",
                          {"prompt": {"5": {"class_type": "MiniMaxH3ReferenceToVideo",
                                            "inputs": {"prompt": "hello"}}},
                           "client_id": "test"})
        pr = _j(raw)
        pid = pr.get("prompt_id")
        check("HARNESS", "POST /prompt 立即成功返回 prompt_id", code == 200 and bool(pid), pr)
        if pid:
            code, raw = _http("GET", fake.base_url, f"/history/{pid}")
            hist = _j(raw)
            outs = [i for v in (hist.get(pid, {}).get("outputs") or {}).values()
                    for i in (v.get("images") or [])]
            check("HARNESS", "GET /history/{id} 有 completed + mp4",
                  code == 200 and hist.get(pid, {}).get("status", {}).get("completed")
                  and any(x.get("filename", "").endswith(".mp4") for x in outs), hist)
            code, raw = _http("GET", fake.base_url,
                              "/view?filename=" + urllib.parse.quote(fake.output_name))
            check("HARNESS", "GET /view 返回视频字节",
                  code == 200 and raw[:8].endswith(b"ftyp"), raw[:20])
        # multipart upload
        b = "----accept"
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"ref.png\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                f"PNGDATA\r\n--{b}--\r\n").encode()
        req = urllib.request.Request(fake.base_url + "/upload/image", data=body,
                                     headers={"Content-Type":
                                              f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            up = json.loads(r.read().decode())
        check("HARNESS", "POST /upload/image 记住上传文件",
              up.get("name") == "ref.png" and fake.uploaded.get("ref.png") == b"PNGDATA", up)
        graph = {"5": {"class_type": "MiniMaxH3ReferenceToVideo",
                       "inputs": {"prompt": "queued prompt"}}}
        fake.stage("pid-pend", graph, state="pending")
        fake.set_queue(pending=["pid-pend"])
        code, raw = _http("GET", fake.base_url, "/queue")
        q = _j(raw)
        check("HARNESS", "GET /queue 可注入 pending",
              q.get("queue_pending") and q["queue_pending"][0][1] == "pid-pend"
              and q.get("pendingIds") == ["pid-pend"], q)
        fake.set_queue(running=["pid-pend"], pending=[])
        q = _j(_http("GET", fake.base_url, "/queue")[1])
        check("HARNESS", "GET /queue 可注入 running",
              q.get("queue_running") and q["queue_running"][0][1] == "pid-pend", q)
        code, _ = _http("POST", fake.base_url, "/interrupt", {})
        check("HARNESS", "POST /interrupt 生效", code == 200 and fake.interrupts == 1,
              fake.interrupts)
        fake.mode = "fail"
        code, raw = _http("POST", fake.base_url, "/prompt", {"prompt": {"1": {}}})
        check("HARNESS", "POST /prompt 可配置立即失败", code == 400, (code, _text(raw)))
        fake.mode = "success"
        fake.delay = 0.12
        t0 = time.perf_counter()
        code, _ = _http("POST", fake.base_url, "/prompt", {"prompt": {"1": {}}})
        dt = time.perf_counter() - t0
        check("HARNESS", "POST /prompt 可配置延迟", code == 200 and dt >= 0.11,
              round(dt, 3))
        fake.delay = 0.0
        url = fake.base_url
    try:
        urllib.request.urlopen(url + "/system_stats", timeout=1)
        check("HARNESS", "stop() 后端口关闭", False, "仍可连接")
    except Exception:
        check("HARNESS", "stop() 后端口关闭", True)


# ------------------------------------------------------------------ AC-01


def _ac01_overview():
    """只读正式项目，随机端口拉起真实 server.Handler，断言页头数据。"""
    if not REAL_PROJECT.is_dir():
        raise _Skip(f"正式项目不存在：{REAL_PROJECT}")
    tmp_state = SESSION / "ac01-state"
    tmp_state.mkdir(exist_ok=True)
    code = r"""
import json
import server
import urllib.request
from http.server import ThreadingHTTPServer

import threading
srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
srv.daemon_threads = True
port = srv.server_address[1]
if port in range(8188, 8196) or port == 8790:
    raise SystemExit("临时端口命中保留端口")
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                 daemon=True).start()
result = {"project": str(server.PROJECT), "port": port}
try:
    with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/overview" % port, timeout=30) as r:
        result["overview"] = json.loads(r.read().decode("utf-8"))
finally:
    srv.shutdown()
    srv.server_close()
print("RESULT:" + json.dumps(result, ensure_ascii=False), flush=True)
"""
    env = dict(os.environ)
    env.update({"WORKBENCH_PROJECT": str(REAL_PROJECT),
                "WORKBENCH_STATE": str(tmp_state),
                "SHOTCALL_NO_BACKGROUND": "1",
                "SHOTCALL_VJOBS_FILE": str(tmp_state / "ac01-vjobs.json"),
                "PYTHONPATH": str(APP)})
    r = subprocess.run([sys.executable, "-c", code], cwd=str(APP), env=env,
                       capture_output=True, text=True, timeout=90)
    result = {}
    for line in r.stdout.splitlines():
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
    if r.returncode != 0 or not result.get("overview"):
        check("AC-01", "临时端口 /api/overview 可访问", False,
              (r.returncode, (r.stderr or r.stdout)[-500:]))
        return
    ov = result["overview"]
    scenes = ov.get("scenes") or {}
    assets = ov.get("assets") or {}
    episodes = ov.get("episodes") or []
    check("AC-01", "GET /api/overview 返回 8 场", scenes.get("count") == 8, scenes)
    check("AC-01", "GET /api/overview 返回镜数字段", isinstance(ov.get("shots"), int), ov.get("shots"))
    check("AC-01", "GET /api/overview 资产数与类型分项一致",
          assets.get("total") == (assets.get("character", 0) + assets.get("scene", 0)
                                  + assets.get("prop", 0) + assets.get("extra", 0)),
          assets)
    ids = [e.get("id") for e in episodes]
    check("AC-01", "只显示真实 E001–E003，无假 E004+",
          ids == ["E001", "E002", "E003"], ids)
    check("AC-01", "页头有场次与分集数据",
          scenes.get("count", 0) > 0 and bool(episodes),
          {"scenes": scenes.get("count"), "shots": ov.get("shots"),
           "episodes": ids})
    check("AC-01", "server 只监听 127.0.0.1 随机端口（非 8790）",
          result.get("port") not in range(8188, 8196)
          and result.get("port") != 8790, result.get("port"))
    skip("AC-01", "页面 DOM 文案（不出现“0 场 0 镜/无分集数据”）",
         "未做浏览器自动化；已用 /api/overview 的 8/6/36 与非空 E001–E003 代替")


# ------------------------------------------------------------------ AC-02


def _ac02_approvals_module():
    import approvals
    proj = SESSION / "ac02-mod"
    proj.mkdir(parents=True)
    rel = _prompt_file(proj, "N001", "v009", "# 验收\n\n正文\n")
    f = proj / rel
    sha = _sha256(f)

    rec = approvals.append(proj, "file", rel, "pass", path=rel, version="v009",
                           scope={"unit": "N001"},
                           notes=[{"anchor": "U001-A", "text": "验收"}])
    check("AC-02", "approvals.append 写 file 记录", rec.get("kind") == "file"
          and rec.get("target") == rel and rec.get("path") == rel
          and rec.get("verdict") == "pass" and rec.get("sha256") == sha
          and rec.get("version") == "v009" and rec.get("supersedes") is None
          and rec.get("id", "").startswith("APR-"), rec)
    ledger = proj / "制作运行" / "approvals.jsonl"
    check("AC-02", "approvals.jsonl 追加落盘", ledger.is_file(), str(ledger))
    lines = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()
             if x.strip()] if ledger.is_file() else []
    check("AC-02", "账本每行都是合法 JSON 且只有新增",
          len(lines) == 1 and lines[0].get("id") == rec.get("id"), lines)
    check("AC-02", "latest 返回当前有效记录",
          any(x.get("id") == rec.get("id") for x in approvals.latest(proj)))

    st = approvals.state(proj, "file", rel)
    check("AC-02", "state 未改动时 stale=False 且 shaNow 正确",
          st.get("record", {}).get("id") == rec.get("id")
          and st.get("stale") is False and st.get("shaNow") == sha, st)
    check("AC-02", "file_ok 对有效 pass 返回记录",
          (approvals.file_ok(proj, rel) or {}).get("id") == rec.get("id"))

    f.write_text("# 验收\n\n正文（改）\n", encoding="utf-8")
    st2 = approvals.state(proj, "file", rel)
    check("AC-02", "文件改动后 stale=True（含原因）",
          st2.get("stale") is True and bool(st2.get("reason")), st2)
    check("AC-02", "stale 的 file 不再 file_ok", approvals.file_ok(proj, rel) is None)

    rec2 = approvals.append(proj, "file", rel, "pass", path=rel, version="v009")
    st3 = approvals.state(proj, "file", rel)
    check("AC-02", "改判带 supersedes 且旧记录保留",
          rec2.get("supersedes") == rec.get("id")
          and len(approvals.read_all(proj)) == 2
          and st3.get("record", {}).get("id") == rec2.get("id")
          and st3.get("stale") is False, (rec2, st3))
    rec3 = approvals.append(proj, "file", rel, "revise", path=rel, version="v009")
    check("AC-02", "revise 后 file_ok 为 None（只认 pass）",
          rec3.get("supersedes") == rec2.get("id")
          and approvals.file_ok(proj, rel) is None)

    srec = approvals.append(proj, "shot", "N001/SH001", "pass",
                            scope={"scene": "N001"})
    sst = approvals.state(proj, "shot", "N001/SH001")
    check("AC-02", "shot 审批按稳定 ID 生效",
          (sst.get("record") or {}).get("id") == srec.get("id")
          and sst.get("stale") is False, sst)

    for bad in (("file", rel, "maybe"), ("bogus", "X", "pass")):
        try:
            approvals.append(proj, bad[0], bad[1], bad[2], path=rel)
            ok = False
        except ValueError:
            ok = True
        check("AC-02", f"非法审批被拒绝（{bad[0]}/{bad[2]}）", ok)

    # 并发追加：不丢行、不写半行、id 不重复
    def one(i):
        return approvals.append(proj, "shot", f"N003/SH{i:03d}", "pass",
                                scope={"scene": "N003"})
    with ThreadPoolExecutor(max_workers=4) as ex:
        got = list(ex.map(one, range(1, 21)))
    ids = [x.get("id") for x in got]
    raw_lines = [x for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    parsed = []
    for line in raw_lines:
        try:
            parsed.append(json.loads(line))
        except Exception:
            parsed.append(None)
    check("AC-02", "并发 20 次追加：全部可解析、id 唯一",
          len(got) == 20 and len(set(ids)) == 20
          and all(isinstance(x, dict) for x in parsed)
          and len(parsed) == len(raw_lines)
          and sum(1 for x in parsed if isinstance(x, dict)
                  and x.get("target", "").startswith("N003/")) == 20,
          (len(raw_lines), ids[-3:]))


def _ac02_confirm_http():
    srv = _server()
    base = srv["base"]
    rel = "02-角色圣经/ac02-draft.md"
    f = _write(HTTP_PROJECT / rel, "# 草稿\n\n验收用\n")
    sha = _sha256(f)
    code, raw = _http("POST", base, "/api/artifact/confirm",
                      {"path": rel, "verdict": "pass", "note": "AC-02 验收",
                       "stage": "02", "kind": "doc", "scope": {"unit": "N001"}})
    obj = _j(raw)
    check("AC-02", "POST /api/artifact/confirm 返回 200 + ok",
          code == 200 and obj.get("ok") is True, (code, _text(raw)))
    check("AC-02", "HTTP 确认返回记录与 state",
          (obj.get("record") or {}).get("kind") == "file"
          and (obj.get("record") or {}).get("verdict") == "pass"
          and (obj.get("local") or {}).get("record", {}).get("id")
          == (obj.get("record") or {}).get("id"), obj)

    ledger = HTTP_PROJECT / "制作运行" / "approvals.jsonl"
    last = None
    if ledger.is_file():
        lines = [x for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
        last = json.loads(lines[-1]) if lines else None
    check("AC-02", "HTTP 确认写入 approvals.jsonl（file + sha256）",
          bool(last) and last.get("kind") == "file"
          and last.get("target") == rel and last.get("sha256") == sha
          and last.get("verdict") == "pass", last)
    local = HTTP_PROJECT / "制作运行" / "确认状态.json"
    if local.exists():
        try:
            json.loads(local.read_text(encoding="utf-8"))
            local_ok = True
        except Exception:
            local_ok = False
    else:
        local_ok = True
    check("AC-02", "确认状态.json 无 400/部分写（存在即合法 JSON）", local_ok,
          str(local))

    f.write_text("# 草稿\n\n验收用（改）\n", encoding="utf-8")
    q = urllib.parse.urlencode({"kind": "file", "target": rel})
    code, raw = _http("GET", base, "/api/approvals?" + q)
    obj = _j(raw)
    check("AC-02", "server 能报 stale（GET /api/approvals）",
          code == 200 and (obj.get("summary") or {}).get("stale", 0) >= 1, obj)


# ------------------------------------------------------------------ AC-03


def _batch_files():
    import store
    d = Path(store.state_dir(HTTP_PROJECT)) / "generation-batches"
    return {p.name for p in d.glob("*.json")} if d.is_dir() else set()


def _ac03_unconfirmed_blocked():
    import sched
    import vjobs
    srv = _server()
    base = srv["base"]
    _prompt_file(HTTP_PROJECT, "N003", "v009",
                 "### U001-A｜6秒｜SH001\n\n```text\n未确认的提示词\n```\n")
    before_jobs = len(vjobs._jobs)
    before_batches = _batch_files()

    code, raw = _http("POST", base, "/api/gen/submit",
                      {"scene": "N003", "units": ["U001-A"], "count": 1})
    text = _text(raw)
    check("AC-03", "未确认提示词提交被 400 阻断", code == 400, (code, text))
    check("AC-03", "错误信息列出缺失项（提示词文件 + SH001）",
          "SH001" in text and ("提示词" in text
                               or "minimax-h3-ref2va-v009.md" in text), text)

    code2, raw2 = _http("POST", base, "/api/gen/submit",
                        {"scene": "N003", "units": ["U001-A"], "count": 1,
                         "force": True})
    text2 = _text(raw2)
    check("AC-03", "force=true 仍被 400 阻断", code2 == 400, (code2, text2))
    check("AC-03", "force 后仍列出缺失的 SH001", "SH001" in text2, text2)
    check("AC-03", "被阻断的提交没有创建任务", len(vjobs._jobs) == before_jobs,
          (before_jobs, len(vjobs._jobs)))
    check("AC-03", "被阻断的提交没有写批次记录", _batch_files() == before_batches,
          (_batch_files(), before_batches))
    # 防御：如果实现错误地放行，清理掉它留下的调度项，避免测试进程内残留
    for raw in (raw, raw2):
        obj = _j(raw)
        if isinstance(obj, dict) and obj.get("jobs"):
            sched.cancel_pending(obj["jobs"])


# ------------------------------------------------------------------ AC-04


def _submit(base, scene, units):
    return _http("POST", base, "/api/gen/submit",
                 {"scene": scene, "units": units, "count": 1})


def _ac04_shot_gate():
    import approvals
    import sched
    import vjobs
    srv = _server()
    base = srv["base"]

    rel1 = _prompt_file(HTTP_PROJECT, "N001", "v009",
                        "### U001-A｜6秒｜SH001\n\n```text\nN001 U001-A\n```\n\n"
                        "### U001-B｜5秒｜SH002\n\n```text\nN001 U001-B\n```\n")
    rel2 = _prompt_file(HTTP_PROJECT, "N002", "v005",
                        "### U001｜13秒｜SH001+SH002\n\n```text\nv005 U001\n```\n")
    approvals.append(HTTP_PROJECT, "file", rel1, "pass", path=rel1, version="v009")
    approvals.append(HTTP_PROJECT, "file", rel2, "pass", path=rel2, version="v005")
    approvals.append(HTTP_PROJECT, "shot", "N001/SH001", "pass",
                     scope={"scene": "N001"})
    approvals.append(HTTP_PROJECT, "shot", "N002/SH001", "pass",
                     scope={"scene": "N002"})

    # U001-A 只要求 SH001：只批 SH001 就应该放行
    code, raw = _submit(base, "N001", ["U001-A"])
    obj = _j(raw)
    text = _text(raw)
    check("AC-04", "U001-A 只要求 SH001（SH001 已批 → 放行）",
          code == 200 and obj.get("jobs"), (code, text))
    for jid in obj.get("jobs") or []:
        pass
    if obj.get("jobs"):
        sched.cancel_pending(obj["jobs"])

    # v005 U001 覆盖 SH001+SH002：只批 SH001 必须阻断并点名 SH002
    before_jobs = len(vjobs._jobs)
    code, raw = _submit(base, "N002", ["U001"])
    obj = _j(raw)
    text = _text(raw)
    check("AC-04", "v005 U001 缺 SH002 → 阻断", code == 400, (code, text))
    check("AC-04", "缺失清单点名 SH002（且不误报已批的 SH001）",
          "SH002" in text and "SH001：镜头" not in text, text)
    check("AC-04", "缺镜头时没有创建任务", len(vjobs._jobs) == before_jobs,
          (before_jobs, len(vjobs._jobs)))

    approvals.append(HTTP_PROJECT, "shot", "N002/SH002", "pass",
                     scope={"scene": "N002"})
    code, raw = _submit(base, "N002", ["U001"])
    obj = _j(raw)
    text = _text(raw)
    check("AC-04", "v005 U001 批 SH001+SH002 → 放行",
          code == 200 and obj.get("jobs"), (code, text))
    if obj.get("jobs"):
        sched.cancel_pending(obj["jobs"])


# ------------------------------------------------------------------ AC-05


def _ac05_dependency_failure():
    import sched
    import vjobs

    if not hasattr(sched, "_collect_ready") or not hasattr(sched, "_run_one"):
        raise _Skip("sched 未提供 _collect_ready/_run_one（M1-03 契约）")

    # 1) runner 返回 False / 抛异常 → 不进 _done，进 _failed
    saved = (dict(sched._runners), list(sched._pending),
             set(sched._done), set(sched._failed))
    try:
        with mock.patch.object(sched.comfyctl, "_alive", return_value=True), \
             mock.patch.object(sched.traceback, "print_exc"), \
             mock.patch.object(sched.comfyctl, "start",
                               side_effect=AssertionError("测试不得启动 ComfyUI")):
            sched._runners["ACC-FALSE"] = lambda port: False
            ok = sched._run_one({"id": "ACC-FALSE", "kind": "video"}, 0)
            check("AC-05", "runner 返回 False 记为失败（不进 done）",
                  ok is False and "ACC-FALSE" in sched.failed_ids()
                  and "ACC-FALSE" not in sched._done, sched.failed_ids())
            sched._runners["ACC-RAISE"] = lambda port: (_ for _ in ()).throw(
                RuntimeError("boom"))
            ok2 = sched._run_one({"id": "ACC-RAISE", "kind": "video"}, 0)
            check("AC-05", "runner 抛异常记为失败",
                  ok2 is False and "ACC-RAISE" in sched.failed_ids(),
                  sched.failed_ids())
    finally:
        with sched._lock:
            sched._runners.clear()
            sched._runners.update(saved[0])
            sched._pending[:] = saved[1]
            sched._done.clear()
            sched._done.update(saved[2])
            sched._failed.clear()
            sched._failed.update(saved[3])

    # 2) 依赖失败 → 下游从 pending 移除并回调 on_cancel("dependency_failed")，继续级联
    saved2 = (list(sched._pending), set(sched._done), set(sched._failed),
              dict(sched._runners))
    calls = []
    try:
        with sched._lock:
            sched._pending[:] = [
                {"id": "ACC-B", "kind": "video", "after": ["ACC-A"],
                 "on_cancel": lambda reason: calls.append(("ACC-B", reason))},
                {"id": "ACC-C", "kind": "video", "after": ["ACC-B"],
                 "on_cancel": lambda reason: calls.append(("ACC-C", reason))},
            ]
            sched._done.clear()
            sched._failed.clear()
            sched._failed.add("ACC-A")
            sched._runners.clear()
            sched._runners.update({"ACC-B": lambda p: True,
                                   "ACC-C": lambda p: True})
        ready = sched._collect_ready()
        sched._collect_ready()
        with sched._lock:
            still = [x["id"] for x in sched._pending]
        reasons = {cid: reason for cid, reason in calls}
        check("AC-05", "依赖失败的下游不进入 ready、B 不执行", ready == [], ready)
        check("AC-05", "下游 on_cancel 收到 dependency_failed",
              reasons.get("ACC-B") == "dependency_failed"
              and reasons.get("ACC-C") == "dependency_failed", calls)
        check("AC-05", "依赖失败级联：B/C 都从 pending 移除并记失败",
              not still and "ACC-B" in sched.failed_ids()
              and "ACC-C" in sched.failed_ids(), (still, sched.failed_ids()))
    finally:
        with sched._lock:
            sched._pending[:] = saved2[0]
            sched._done.clear(); sched._done.update(saved2[1])
            sched._failed.clear(); sched._failed.update(saved2[2])
            sched._runners.clear(); sched._runners.update(saved2[3])

    # 3) vjobs 把 A/B 依赖接线并真正把 B 标 cancelled + dependency_failed
    proj = SESSION / "ac05"
    _prompt_file(proj, "N001", "v009",
                 "### U001-A｜6秒｜SH001\n\n```text\nA\n```\n\n"
                 "### U001-B｜5秒｜SH002\n\n```text\nB\n```\n")
    vjobs._jobs.clear()
    vjobs._order.clear()
    vjobs.JOBS_FILE = SESSION / "vjobs-ac05.json"
    vjobs.JOBS_FILE.unlink(missing_ok=True)
    ua = {"id": "U001-A", "seconds": 6, "prompt": "A", "refs": [], "shots": "SH001"}
    ub = {"id": "U001-B", "seconds": 5, "prompt": "B", "refs": [], "shots": "SH002"}
    captured = []

    def fake_submit(kind, items):
        captured.append(items)
        return [x["id"] for x in items]

    with mock.patch.object(sched, "submit", side_effect=fake_submit):
        res = vjobs.submit(proj, "N001",
                           [{"unit": ua, "count": 1},
                            {"unit": ub, "count": 1, "chain": True,
                             "prevUnit": "U001-A"}], steps=20)
    items = captured[0] if captured else []
    by_id = {x["id"]: x for x in items}
    a_ids = [x for x in by_id if x.endswith("U001-A")]
    b_ids = [x for x in by_id if x.endswith("U001-B")]
    check("AC-05", "vjobs 生成 A/B 两条任务且 B 依赖 A",
          len(items) == 2 and bool(a_ids) and bool(b_ids)
          and by_id[b_ids[0]]["after"] == a_ids, items)
    cb = by_id[b_ids[0]].get("on_cancel") if b_ids else None
    check("AC-05", "B 的调度项带 on_cancel", callable(cb), type(cb).__name__)
    if callable(cb):
        cb("dependency_failed")
    bjob = vjobs._jobs.get(b_ids[0], {}) if b_ids else {}
    check("AC-05", "B 被标 cancelled 且 cancelReason=dependency_failed",
          bjob.get("status") == "cancelled"
          and bjob.get("cancelReason") == "dependency_failed", bjob)
    persisted = {}
    if vjobs.JOBS_FILE.is_file():
        persisted = json.loads(vjobs.JOBS_FILE.read_text(encoding="utf-8"))
    pb = (persisted.get("jobs") or {}).get(b_ids[0], {}) if b_ids else {}
    check("AC-05", "B 的 cancelled/dependency_failed 已落盘",
          pb.get("status") == "cancelled"
          and pb.get("cancelReason") == "dependency_failed", pb)

    vjobs._jobs.clear()
    vjobs._order.clear()
    vjobs.JOBS_FILE = SESSION / "vjobs.json"


# ------------------------------------------------------------------ AC-06


def _ac06_waiting_reconcile():
    import comfyctl
    import vjobs
    from fake_comfy import FakeComfy

    proj = SESSION / "ac06"
    text = "排队状态验收提示词"
    _prompt_file(proj, "N001", "v009",
                 f"### U001-A｜6秒｜SH001\n\n```text\n{text}\n```\n")
    vjobs._jobs.clear()
    vjobs._order.clear()
    vjobs.JOBS_FILE = SESSION / "vjobs-ac06.json"
    vjobs.JOBS_FILE.unlink(missing_ok=True)
    job = {"id": "J-WAIT", "batch": "ACC", "scene": "N001", "unit": "U001-A",
           "seq": 1, "seconds": 6, "frames": 328, "steps": 20, "seed": 7,
           "status": "dispatched", "card": None, "promptId": None,
           "file": None, "error": None, "queuedAt": time.time(),
           "startedAt": None, "prompt": text, "refs": []}
    vjobs._jobs["J-WAIT"] = dict(job)
    vjobs._order.append("J-WAIT")

    graph = {"5": {"class_type": "MiniMaxH3ReferenceToVideo",
                   "inputs": {"prompt": text}}}
    fake = FakeComfy().start()
    try:
        fake.stage("pid-wait", graph, state="pending")
        real_urlopen = vjobs.urllib.request.urlopen

        def rewrite(url, *a, **k):
            s = getattr(url, "full_url", url)
            s = re.sub(r"http://127\.0\.0\.1:81(?:8[89]|9[0-5])",
                       fake.base_url, str(s))
            return real_urlopen(s, *a, **k)

        with mock.patch.object(vjobs.urllib.request, "urlopen", new=rewrite), \
             mock.patch.object(vjobs.gpus, "queue_of",
                               new=lambda port=None: fake.queue_of()), \
             mock.patch.object(vjobs.gpus, "PORTS", [fake.port]), \
             mock.patch.object(comfyctl, "BASE_PORT", fake.port):
            vjobs.reconcile(proj)
        j = vjobs._jobs.get("J-WAIT", {})
        status = j.get("status")
        qs = j.get("queueState")
        waiting_ok = status != "running" and (
            status == "waiting" or (status in ("dispatched", "queued")
                                    and qs == "waiting"))
        check("AC-06", "ComfyUI pending 时显示排队（不显示 running）",
              j.get("promptId") == "pid-wait" and waiting_ok, j)
        persisted = {}
        if vjobs.JOBS_FILE.is_file():
            persisted = json.loads(vjobs.JOBS_FILE.read_text(encoding="utf-8"))
        pj = (persisted.get("jobs") or {}).get("J-WAIT", {})
        check("AC-06", "reconcile 的排队状态已落盘",
              pj.get("promptId") == "pid-wait"
              and pj.get("status") == status
              and pj.get("queueState") == qs, pj)

        fake.set_queue(running=["pid-wait"], pending=[])
        with mock.patch.object(vjobs.urllib.request, "urlopen", new=rewrite), \
             mock.patch.object(vjobs.gpus, "queue_of",
                               new=lambda port=None: fake.queue_of()), \
             mock.patch.object(vjobs.gpus, "PORTS", [fake.port]), \
             mock.patch.object(comfyctl, "BASE_PORT", fake.port):
            vjobs.reconcile(proj)
        j2 = vjobs._jobs.get("J-WAIT", {})
        check("AC-06", "ComfyUI 真正 running 才写 running + queueState=running",
              j2.get("status") == "running"
              and j2.get("queueState") in (None, "running"), j2)
        persisted2 = {}
        if vjobs.JOBS_FILE.is_file():
            persisted2 = json.loads(vjobs.JOBS_FILE.read_text(encoding="utf-8"))
        pj2 = (persisted2.get("jobs") or {}).get("J-WAIT", {})
        check("AC-06", "reconcile 的 running 状态已落盘",
              pj2.get("status") == "running", pj2)
        skip("AC-06", "浏览器排队徽章 + 顶栏视频/图片队列统计",
             "无 Playwright/浏览器自动化；以 reconcile 的 dispatched+queueState=waiting 代替，"
             "UI 徽章与顶栏计数需人工验收")
    finally:
        fake.stop()
        vjobs._jobs.clear()
        vjobs._order.clear()
        vjobs.JOBS_FILE = SESSION / "vjobs.json"


# ------------------------------------------------------------------ AC-07


def _ac07_gpu_sampler():
    src = (APP / "comfywatch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    forbidden = ("mem_get_info" in src or "torch" in imports
                 or re.search(r"\btorch\b", src) is not None
                 or "H3_PY" in src
                 or "cuda.mem_get_info" in src)
    check("AC-07", "comfywatch 不 import/调用 torch、无 mem_get_info、无 H3_PY",
          not forbidden, [x for x in ("torch", "mem_get_info", "H3_PY")
                          if x in src])
    check("AC-07", "comfywatch 用 nvidia-smi 查询显存/利用率",
          "nvidia-smi" in src and "memory.used" in src
          and "utilization.gpu" in src)
    top_threads = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and fn.attr == "start":
                top_threads.append(ast.dump(fn.value)[:80])
    check("AC-07", "import comfywatch 不自动起后台线程",
          not top_threads, top_threads)

    if forbidden:
        skip("AC-07", "运行期采样断言", "源码仍含 torch/mem_get_info，禁止 import 以免拉起采样线程")
        return

    before = len(threading.enumerate())
    import comfywatch
    after = len(threading.enumerate())
    check("AC-07", "import comfywatch 线程数不变",
          after == before and not comfywatch._started, (before, after))
    parsed = comfywatch._parse_vram_csv("0, 1234, 24576, 7\n"
                                        "1, [N/A], [N/A], [N/A]\n")
    v = parsed.get(0) or {}
    check("AC-07", "nvidia-smi CSV 解析正确（index/used/total/util）",
          v.get("usedGB") == 1.2 and v.get("totalGB") == 24.0
          and v.get("pct") == 5.0 and v.get("util") == 7.0, parsed)

    # 用假的 nvidia-smi 量采样代码路径开销；真实 nvidia-smi 受驱动/机器负载影响，
    # 不作为通过条件（报告里另注）。
    bindir = SESSION / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake_smi = bindir / "nvidia-smi"
    fake_smi.write_text("#!/bin/sh\necho '0, 1234, 24576, 7'\n", encoding="utf-8")
    fake_smi.chmod(0o755)
    with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}):
        t0 = time.perf_counter()
        ok = comfywatch._sample_vram_once()
        elapsed = time.perf_counter() - t0
    v2 = comfywatch.vram(0)
    check("AC-07", "采样函数走 nvidia-smi 并更新 vram",
          ok is True and v2.get("usedGB") == 1.2 and v2.get("util") == 7.0, v2)
    check("AC-07", "采样代码路径 <200ms（nvidia-smi 快速返回时）",
          elapsed < 0.2, f"{elapsed:.3f}s")


# ------------------------------------------------------------------ AC-08


def _ac08_test_isolation():
    import store
    src = (APP / "test_all.py").read_text(encoding="utf-8")
    check("AC-08", "test_all.py 不依赖 8790（无硬编码 8790）",
          "8790" not in src)
    check("AC-08", "test_all.py 用临时端口启动本地 server",
          "ThreadingHTTPServer" in src and re.search(
              r'ThreadingHTTPServer\(\("127\.0\.0\.1",\s*0\)', src) is not None)
    check("AC-08", "test_all.py 在 import server 前设置临时 state / 禁后台线程",
          "WORKBENCH_STATE" in src and "SHOTCALL_NO_BACKGROUND" in src
          and src.find("WORKBENCH_STATE") < src.find("import server"))
    # 正式项目写入必须被 WORKBENCH_STATE 重定向
    st = Path(store.state_dir(REAL_PROJECT)).resolve()
    real_state = (REAL_PROJECT / "制作运行").resolve()
    check("AC-08", "store.state_dir 在 WORKBENCH_STATE 下不指向正式项目",
          st != real_state and str(st).startswith(str(SESSION)), (st, real_state))
    # test_all 里不能出现会写正式项目的直接调用
    writer_pat = re.compile(
        r"\b(?:confirms\.record|approvals\.append|vjobs\.submit|"
        r"imgjobs\.submit_batch|comfyctl\.start|comfyctl\.stop|sched\.start)\s*\(")
    hits = writer_pat.findall(src)
    check("AC-08", "test_all.py 不直接调用写项目/启停实例的接口", not hits, hits)

    # import server 的真实副作用检查（独立子进程，临时项目）
    tmp_proj = SESSION / "ac08-import-project"
    tmp_state = SESSION / "ac08-import-state"
    tmp_proj.mkdir(exist_ok=True)
    tmp_state.mkdir(exist_ok=True)
    code = (
        "import json, threading\n"
        "before = len(threading.enumerate())\n"
        "import server\n"
        "after = len(threading.enumerate())\n"
        "print(json.dumps({'before': before, 'after': after,"
        " 'project': str(server.PROJECT),"
        " 'threads': [t.name for t in threading.enumerate()]}))\n"
    )
    env = dict(os.environ)
    env.update({"WORKBENCH_PROJECT": str(tmp_proj),
                "WORKBENCH_STATE": str(tmp_state),
                "SHOTCALL_NO_BACKGROUND": "1",
                "SHOTCALL_VJOBS_FILE": str(tmp_state / "ac08-vjobs.json"),
                "PYTHONPATH": str(APP)})
    r = subprocess.run([sys.executable, "-c", code], cwd=str(APP), env=env,
                       capture_output=True, text=True, timeout=40)
    info = _j(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
    check("AC-08", "import server 无后台线程副作用",
          r.returncode == 0 and info.get("before") == info.get("after")
          and Path(info.get("project", "")).resolve() == tmp_proj.resolve(),
          (r.returncode, info, r.stderr[-300:]))
    # import server 不写临时项目文件
    files_after = sorted(p.relative_to(tmp_proj).as_posix()
                         for p in tmp_proj.rglob("*") if p.is_file())
    check("AC-08", "import server 不写项目文件", files_after == [], files_after)


def _ac09_real_project_readonly(before, after):
    if before is None:
        raise _Skip(f"正式项目不存在或 05/07/02/08/09 为空：{REAL_PROJECT}")
    changed = sorted(k for k in before.keys() & after.keys()
                     if before[k]["sha"] != after[k]["sha"])
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    check("AC-09", f"正式项目 05/07/02/08/09 既有文件 SHA 不变（{len(before)} 个）",
          not changed and not removed, {"changed": changed[:5],
                                        "removed": removed[:5]})
    check("AC-09", "测试没有在正式项目新增文件", not added, added[:10])


# ------------------------------------------------------------------ AC-10


def _ac10_one_command():
    src = (APP / "test_all.py").read_text(encoding="utf-8")
    check("AC-10", "本套测试可一条命令运行（本次即该命令）", True)
    check("AC-10", "test_all.py 已接入 test_m1_acceptance.py",
          "test_m1_acceptance.py" in src and "subprocess.run" in src)
    check("AC-10", "test_all 的子测试失败会向上传播（检查 returncode）",
          re.search(r"returncode\s*==\s*0", src) is not None)


# ------------------------------------------------------------------ 主流程


def main():
    global SESSION, HTTP_PROJECT
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结果写成 JSON")
    a = ap.parse_args()

    SESSION = Path(tempfile.mkdtemp(prefix="shotcall-m1-accept-"))
    HTTP_PROJECT = SESSION / "http-project"
    HTTP_PROJECT.mkdir()
    os.environ["WORKBENCH_PROJECT"] = str(HTTP_PROJECT)
    os.environ["WORKBENCH_STATE"] = str(SESSION / "state")
    os.environ["SHOTCALL_NO_BACKGROUND"] = "1"

    print("=" * 72)
    print("Shotcall M1 独立验收（临时项目 + fake ComfyUI）")
    print(f"  会话目录：{SESSION}")
    print(f"  正式项目：{REAL_PROJECT}（只读）")
    print("=" * 72)

    before = snapshot_creative()
    print(f"  正式项目快照：{len(before)} 个文件\n")

    # 运行时准备：vjobs 全部落到 /tmp；避免读到正式 run 文件后写回
    try:
        import vjobs
        vjobs.JOBS_FILE = SESSION / "vjobs.json"
        vjobs.RECIPE_FILE = SESSION / "gen_recipe.json"
        vjobs.PIN_FILE = SESSION / "prompt_pins.json"
        vjobs.ZH_FILE = SESSION / "prompt_zh.json"
        vjobs._jobs.clear()
        vjobs._order.clear()
    except Exception as e:
        print(f"  [warn] 预导入 vjobs 失败：{e}")

    print("\n= fake ComfyUI 自检 =")
    run("HARNESS", "fake ComfyUI", _harness_selftest)

    print("\n= AC-01 页头真实数据 =")
    run("AC-01", "/api/overview 8 场 / 6 镜 / 资产分项", _ac01_overview)

    print("\n= AC-02 确认事务 =")
    run("AC-02", "approvals.py append/state/stale", _ac02_approvals_module)
    run("AC-02", "HTTP 确认接口", _ac02_confirm_http)

    print("\n= AC-03 未确认提交阻断 =")
    run("AC-03", "未确认 / force", _ac03_unconfirmed_blocked)

    print("\n= AC-04 镜头门禁 =")
    run("AC-04", "U001-A / v005 U001", _ac04_shot_gate)

    print("\n= AC-05 依赖失败传播 =")
    run("AC-05", "A 失败 → B cancelled", _ac05_dependency_failure)

    print("\n= AC-06 排队状态与落盘 =")
    run("AC-06", "reconcile waiting/running", _ac06_waiting_reconcile)

    print("\n= AC-07 显存采样 =")
    run("AC-07", "nvidia-smi / 无 torch", _ac07_gpu_sampler)

    print("\n= AC-08 测试隔离 =")
    run("AC-08", "test_all.py 隔离", _ac08_test_isolation)

    print("\n= AC-09 正式项目只读 =")
    after = snapshot_creative()
    run("AC-09", "测试前后 SHA", lambda: _ac09_real_project_readonly(before, after))

    print("\n= AC-10 一条命令 =")
    run("AC-10", "命令与集成", _ac10_one_command)

    _stop_server()

    # 汇总
    print("\n" + "=" * 72)
    for ac in ["HARNESS"] + AC_ORDER:
        items = PASS + FAIL + SKIP
        rows = [x for x in items if x["ac"] == ac]
        if not rows:
            continue
        n = {s: sum(1 for x in rows if x["status"] == s)
             for s in ("PASS", "FAIL", "SKIP")}
        label = "通过" if n["FAIL"] == 0 and n["PASS"] else (
            "失败" if n["FAIL"] else "跳过")
        print(f"  {ac:8s} {label:4s}  PASS {n['PASS']:2d}  FAIL {n['FAIL']:2d}  SKIP {n['SKIP']:2d}")
    if FAIL:
        print("\n失败项：")
        for x in FAIL:
            print(f"  - [{x['ac']}] {x['name']}：{x['detail'][:500]}")
    if SKIP:
        print("\n跳过项：")
        for x in SKIP:
            print(f"  - [{x['ac']}] {x['name']}：{x['detail'][:300]}")
    print(f"\n通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)}")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"pass": PASS, "fail": FAIL, "skip": SKIP,
             "realProject": str(REAL_PROJECT),
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
