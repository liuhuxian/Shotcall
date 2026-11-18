#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导演工作台 · 本地只读服务（P0 骨架）

只读项目文件；监听 127.0.0.1；不写项目、不调生成。

    python3 server.py --port 8791
    GET /            → 状态页
    GET /api/data    → 解析后的全量数据（含缩略图）
    GET /api/health  → 健康检查
"""
import argparse
import json
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os

import dataset
import planner
import preflight as pf
import runner
import store

PROJECT = Path.home() / "short_v/有谁共鸣2"
EPISODE = "EP002"

_lock = threading.Lock()
_cache = {"data": None, "ts": 0.0, "sig": None}

WATCHED = ["剧本.md", "分镜.md", "视频提示词.md", "视觉设定.md"]


def file_sig() -> str:
    """四个创作文件的 mtime+size 指纹，用于缓存失效。"""
    ep = PROJECT / "剧集" / EPISODE
    parts = []
    for name in WATCHED:
        f = ep / name
        try:
            st = f.stat()
            parts.append(f"{name}:{int(st.st_mtime)}:{st.st_size}")
        except FileNotFoundError:
            parts.append(f"{name}:missing")
    return "|".join(parts)


UI = Path(__file__).resolve().parent / "ui" / "index.html"
BATCH_DIR = Path(__file__).resolve().parent / "batches"


def list_batches() -> list:
    out = []
    if not BATCH_DIR.exists():
        return out
    for f in sorted(BATCH_DIR.glob("*.md")):
        txt = f.read_text(encoding="utf-8")
        title = next((l.lstrip("# ").strip() for l in txt.splitlines() if l.startswith("# ")), f.stem)
        status = next((l.split("：", 1)[1].strip() for l in txt.splitlines() if l.startswith("状态：")), "")
        out.append({"file": f.name, "title": title, "status": status,
                    "bytes": f.stat().st_size})
    return out


LABEL = {"pass": "通过", "revise": "需修改", "hold": "待定", "na": "不适用"}
MEDIA = Path(os.environ.get("WORKBENCH_MEDIA") or (PROJECT / "剧集" / EPISODE / "制作成果"))


THUMB_CACHE = {}


def media_roots() -> list:
    return [MEDIA, store.state_dir(PROJECT) / "media"]


def resolve_media(r: int, p: str):
    """把 (root 索引, 相对路径) 解析成真实文件；拒绝越界。"""
    roots = media_roots()
    if r < 0 or r >= len(roots):
        return None
    root = roots[r].resolve()
    f = (root / p).resolve()
    if not str(f).startswith(str(root)) or not f.is_file():
        return None
    return f


def thumb_of(f: Path) -> Path:
    key = (str(f), f.stat().st_mtime)
    hit = THUMB_CACHE.get(key)
    if hit and Path(hit).exists():
        return Path(hit)
    out = Path(tempfile.gettempdir()) / ("wbthumb_" + str(abs(hash(key))) + ".jpg")
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if f.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv"):
        cmd += ["-ss", "1", "-i", str(f), "-frames:v", "1"]
    else:
        cmd += ["-i", str(f)]
    cmd += ["-vf", "scale=480:-2", "-q:v", "5", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=30)
        THUMB_CACHE[key] = str(out)
        return out
    except Exception:
        return None


def list_candidates() -> list:
    """扫描 制作成果/ 与运行目录 media/，两处合并（存在才返回）。"""
    out = []
    roots = media_roots()
    for roots_index, root in enumerate(roots):
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in (".mp4", ".mov", ".png", ".jpg", ".webp"):
                continue
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                rel = f.as_posix()
            parts = rel.split("/")
            shot = next((x for x in parts if x.startswith("SHOT-") or x.startswith("SC")), "")
            out.append({"file": f.name, "path": rel, "shot": shot, "rootIdx": roots_index,
                        "root": str(root),
                        "bytes": f.stat().st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))})
    return out


def preflight(body: dict) -> dict:
    return pf.run(body, payload()["data"])


EXEC = {"running": False, "total": 0, "done": 0, "failed": 0, "log": [], "startedAt": None}


def execute_batch(body: dict) -> dict:
    """工作台内执行：先记一条 gate=3 授权，再后台提交。"""
    if EXEC["running"]:
        return {"ok": False, "error": "已有批次在跑"}
    shots = body.get("shots") or []
    res = pf.run({"kind": "video", "items": [{"shot": s} for s in shots]}, payload()["data"])
    if res["blockers"]:
        return {"ok": False, "error": "预检未通过", "blockers": res["blockers"]}

    batch_id = "BATCH-EP002-VID-" + time.strftime("%m%d%H%M")
    store.append(PROJECT, 3, {"type": "batch", "id": batch_id}, "pass",
                 notes=[{"anchor": batch_id, "text": "导演在工作台点按批准并提交"}],
                 scope={"covers": shots, "excludes": ["超出本批的镜头", "创作内容定稿"]},
                 evidence=payload()["data"]["files"])

    tasks = planner.plan(payload()["data"], shots,
                         int(body.get("candidates", 1)), body.get("tier", "draft"),
                         int(body.get("width", 1344)), int(body.get("height", 768)),
                         int(body.get("seed", 42)), batch=batch_id)
    path = store.state_dir(PROJECT) / "tasks.jsonl"
    with open(path, "a", encoding="utf-8", newline="") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    EXEC.update({"running": True, "total": len(tasks), "done": 0, "failed": 0,
                 "log": [], "startedAt": time.strftime("%H:%M:%S")})

    def media_root():
        """优先写到项目的 制作成果/，不可写时退回运行目录 media/。"""
        try:
            MEDIA.mkdir(parents=True, exist_ok=True)
            probe = MEDIA / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return MEDIA
        except OSError:
            d = store.state_dir(PROJECT) / "media"
            d.mkdir(parents=True, exist_ok=True)
            return d

    dest = media_root()

    def job():
        try:
            r = runner.run_batch(PROJECT, tasks, dest,
                                 log_fn=lambda m: EXEC["log"].append(str(m)))
            EXEC["done"] = len(r["done"])
            EXEC["failed"] = len(r["failed"])
        except Exception as e:
            EXEC["log"].append(f"批次异常：{e}")
        finally:
            EXEC["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return {"ok": True, "batch": batch_id, "count": len(tasks)}


def _read_jsonl(name: str) -> list:
    path = store.state_dir(PROJECT) / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def batch_detail(bid: str) -> dict:
    dec = next((d for d in store.latest(PROJECT)
                if d["unit"]["type"] == "batch" and d["unit"]["id"] == bid), None)
    covers = ((dec or {}).get("scope") or {}).get("covers") or []

    tasks = [t for t in _read_jsonl("tasks.jsonl")
             if t.get("batch") == bid
             or (not t.get("batch") and t.get("shot") in covers)]
    tids = {t["id"] for t in tasks}
    runs = [r for r in _read_jsonl("runs.jsonl") if r.get("task") in tids]
    shots = {t["shot"] for t in tasks} | set(covers)
    cands = [c for c in list_candidates() if c["shot"] in shots]

    md = None
    f = BATCH_DIR / (bid + ".md")
    if f.exists():
        md = f.read_text(encoding="utf-8")

    # 用运行事件推导真实状态，而不是任务建好时的初始值
    state_of = {}
    for r in runs:
        if r.get("event") == "succeeded":
            state_of[r["task"]] = "成功"
        elif r.get("event") == "failed":
            state_of[r["task"]] = "失败"
    for t in tasks:
        t["status"] = state_of.get(t["id"], t.get("status") or "待执行")

    return {"ok": True, "id": bid, "decision": dec, "covers": covers,
            "tasks": tasks, "runs": runs, "candidates": cands,
            "markdown": md}


WORKFLOW_STEPS = [
    {"key": "analyze", "name": "原著拆解", "skill": "short-drama-novel-analyze",
     "projectDoc": "项目开发/source-analysis/全本拆解总览.md",
     "note": "章节索引 / 剧情单元 / 人物 / 分集候选"},
    {"key": "develop", "name": "改编方案与分集大纲", "skill": "short-drama-develop",
     "projectDoc": "项目开发/episode-map.jsonl"},
    {"key": "script", "name": "剧本", "skill": "short-drama-write + performance-direction", "doc": "剧本.md"},
    {"key": "visual", "name": "视觉设定", "skill": "short-drama-assets + production-design-direction", "doc": "视觉设定.md"},
    {"key": "look", "name": "光线圣经 + 摄影基调",
     "skill": "lighting-direction + cinematography",
     "note": "写入 视觉设定.md 的「光线与摄影基调」一节"},
    {"key": "imgprompt", "name": "图片提示词", "skill": "short-drama-image-prompts", "doc": "图片提示词.md"},
    {"key": "gen1", "name": "出图① 资产图", "skill": "工作台 · 图片批次", "gate": "img"},
    {"key": "manifest", "name": "素材清单", "skill": "asset-continuity-management",
     "note": "checksum / seed / 版本 / 派生关系 → 制作运行/manifest.jsonl"},
    {"key": "review1", "name": "审核一", "skill": "导演 · 场次与资产判定", "gate": 1},
    {"key": "staging", "name": "调度方案", "skill": "directing-cinematic-storyboards",
     "note": "分镜的依据：站位 / 视线 / 轴线 / 切点理由"},
    {"key": "board", "name": "分镜", "skill": "short-drama-storyboard + performance-direction", "doc": "分镜.md"},
    {"key": "videoprompt", "name": "视频提示词", "skill": "short-drama-video-prompts + performance-direction", "doc": "视频提示词.md"},
    {"key": "gen2", "name": "出图② 分镜图", "skill": "工作台 · 图片批次", "gate": "img"},
    {"key": "test", "name": "试做", "skill": "工作台 · 视频生成（草稿档）", "gate": "vid"},
    {"key": "review2", "name": "审核二", "skill": "导演 · 逐镜判定", "gate": 2},
    {"key": "batch", "name": "批次生成", "skill": "工作台 · 视频生成", "gate": 3},
]


def _script_title(ep_dir: Path):
    f = ep_dir / "剧本.md"
    if not f.exists():
        return None
    try:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines()[:5]:
            if line.startswith("# "):
                parts = line[2:].strip().split(None, 1)
                return parts[1].strip() if len(parts) > 1 else ""
    except OSError:
        return None
    return None


def workflow_of(ep_id: str, decisions: list, cands: list, sels=None) -> dict:
    ep_dir = PROJECT / "剧集" / ep_id
    titles = next((e for e in payload()["data"]["episodes"] if e["id"] == ep_id), {})
    map_title = titles.get("title")
    script_title = _script_title(ep_dir)
    mismatch = bool(script_title and map_title and script_title != map_title)
    # 判定与候选都按集过滤：单元 id 里带集号才算本集；资产项是全剧共享，不参与单集进度
    decision = [d for d in decisions if ep_id in d["unit"]["id"]]
    cands = [c for c in cands if ep_id in (c.get("shot") or "")]
    steps = []
    for i, s in enumerate(WORKFLOW_STEPS, 1):
        st = "pending"
        detail = ""
        basis = ""
        if s.get("projectDoc"):
            f = PROJECT / s["projectDoc"]
            exists = f.exists()
            basis = f"{s['projectDoc']} {'存在' if exists else '不存在'}"
            if exists:
                st = "done"
                detail = f"{f.stat().st_size // 1024} KB"
        if s.get("doc"):
            f = ep_dir / s["doc"]
            exists = f.exists()
            basis = f"剧集/{ep_id}/{s['doc']} {'存在' if exists else '不存在'}"
            if exists:
                st = "done"
                detail = f"{f.stat().st_size // 1024} KB"
        if s.get("doc") is None and s.get("projectDoc") is None and s.get("gate") is None:
            st = "manual"
            basis = "无独立文件；是否做到体现在下游文档内容里"
        if s.get("gate") == 1:
            n = sum(1 for d in decision if d["gate"] == 1)
            st = "done" if n else ("progress" if (ep_dir / "剧本.md").exists() else "pending")
            detail = f"{n} 条判定" if n else "未审"
            basis = f"本集审核一判定记录 ≥1 条（当前 {n} 条）"
        elif s.get("gate") == 2:
            n = sum(1 for d in decision if d["gate"] == 2)
            st = "done" if n else "pending"
            detail = f"{n} 条判定" if n else "未审"
            basis = f"本集审核二判定记录 ≥1 条（当前 {n} 条）"
        elif s.get("gate") == 3:
            n = sum(1 for d in decision if d["gate"] == 3)
            st = "done" if n else "pending"
            detail = f"{n} 个批次" if n else "未授权"
            basis = f"本集批次授权记录 ≥1 条（当前 {n} 个）"
        elif s.get("gate") == "img":
            n = sum(1 for c in cands if c["file"].lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
            st = "done" if n else "pending"
            detail = f"{n} 张候选" if n else "未出图"
            basis = f"本集图片候选 ≥1 张（当前 {n} 张）"
        elif s.get("gate") == "vid":
            n = sum(1 for c in cands if c["file"].lower().endswith((".mp4", ".mov", ".webm")))
            st = "done" if n else "pending"
            detail = f"{n} 条候选" if n else "未出片"
            basis = f"本集视频候选 ≥1 条（当前 {n} 条）"
        steps.append({**{k: s[k] for k in ("key", "name", "skill", "note") if k in s},
                      "no": i, "status": st, "detail": detail, "basis": basis})
    sels = sels or []
    return {"episode": ep_id, "exists": ep_dir.exists(), "steps": steps,
            "mapTitle": map_title, "scriptTitle": script_title, "titleMismatch": mismatch,
            "picks": sum(1 for s in sels if ep_id in (s.get("shot") or "")),
            "candidates": len([c for c in cands]),
            "chapters": titles.get("chapters"), "target": titles.get("target")}


def workflow_all() -> list:
    dec, cands = store.latest(PROJECT), list_candidates()
    sels = list(store.latest_selections(PROJECT).values())
    return [workflow_of(e["id"], dec, cands, sels) for e in payload()["data"]["episodes"]]


def exec_status() -> dict:
    return dict(EXEC, log=EXEC["log"][-12:])


def plan_tasks(body: dict) -> dict:
    """构造任务并落 tasks.jsonl；不提交、不烧卡。"""
    shots = body.get("shots") or []
    res = pf.run({"kind": "video", "items": [{"shot": s} for s in shots]}, payload()["data"])
    if res["blockers"]:
        return {"ok": False, "blockers": res["blockers"], "warnings": res["warnings"]}
    tasks = planner.plan(payload()["data"], shots,
                         int(body.get("candidates", 3)), body.get("tier", "draft"),
                         int(body.get("width", 1344)), int(body.get("height", 768)),
                         int(body.get("seed", 42)), batch=batch_id)
    path = store.state_dir(PROJECT) / "tasks.jsonl"
    with open(path, "a", encoding="utf-8", newline="") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    est = round(sum(x["params"]["frames"] for x in tasks) * 106 / 124 / 60, 1)
    return {"ok": True, "count": len(tasks), "estMinutes": est, "file": str(path),
            "tasks": [{"id": x["id"], "shot": x["shot"], "seed": x["params"]["seed"],
                       "frames": x["params"]["frames"], "tier": x["tier"]} for x in tasks]}


def export_markdown() -> str:
    head = next((e["id"] + " " + e["title"] for e in payload()["data"]["episodes"]
                 if e["id"] == EPISODE), EPISODE)
    lines = [f"# {head} · 导演意见单", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    dec = store.latest(PROJECT)
    if not dec:
        lines.append("- （尚未有任何判定）")
    for gate, name in ((1, "审核一"), (2, "审核二"), (3, "视频生成")):
        group = [d for d in dec if d["gate"] == gate]
        if not group:
            continue
        lines += [f"## {name}", ""]
        for d in group:
            note = "；".join(n["text"] for n in d.get("notes", []) if n.get("text"))
            lines.append(f"- [{d['unit']['type']}] {d['unit']['id']} — **{LABEL.get(d['verdict'], d['verdict'])}**")
            if note:
                lines.append(f"  - 意见：{note}")
            covers = (d.get("scope") or {}).get("covers") or []
            if covers:
                lines.append(f"  - 覆盖：{'、'.join(covers)}")
            lines.append(f"  - 记录：{d['id']} · {d.get('created_at', '')}")
        lines.append("")
    sels = list(store.latest_selections(PROJECT).values())
    if sels:
        lines += ["## 挑片", ""]
        for s in sels:
            lines.append(f"- {s['shot']} → **{ {'adopt':'采用','backup':'备选','redo':'重做'}.get(s['status'], s['status']) }**"
                         f"（{s['candidate']}）")
            if s.get("reason"):
                lines.append(f"  - 原因：{'、'.join(s['reason'])}")
            lines.append(f"  - 记录：{s['id']} · {s.get('created_at','')}")
        lines.append("")
    stale = [d for d in dec if d.get("unit") and (d.get("evidence") or {})]
    files = payload()["data"].get("files", {})
    if files:
        lines += ["## 依据文件版本", ""]
        lines += [f"- {k} `{v}`" for k, v in files.items()]
        lines.append("")
    return "\n".join(lines)


def payload(ep: str = EPISODE, refresh: bool = False) -> dict:
    with _lock:
        sig = file_sig()
        if refresh or _cache["data"] is None or _cache.get("sig") != sig:
            t0 = time.time()
            out = dataset.load(PROJECT, ep)
            out["meta"] = {"builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "buildSeconds": round(time.time() - t0, 2),
                           "project": str(PROJECT)}
            _cache["data"] = out
            _cache["ts"] = time.time()
            _cache["sig"] = sig
        return _cache["data"]


def _annotate_stale(records, files):
    out = []
    for r in records:
        r = dict(r)
        ev = r.get("evidence") or {}
        changed = [k for k, v in ev.items() if files.get(k) and files.get(k) != v]
        r["stale"] = bool(changed)
        r["staleFiles"] = changed
        out.append(r)
    return out


def response() -> dict:
    """解析数据可缓存；决策每次现读，保证刚落账立刻可见。"""
    base = payload()
    data = dict(base["data"])
    files = data.get("files", {})
    data["decisions"] = _annotate_stale(store.latest(PROJECT), files)
    data["batches"] = list_batches()
    data["candidates"] = list_candidates()
    data["mediaRoot"] = str(MEDIA)
    runs_path = store.state_dir(PROJECT) / "runs.jsonl"
    runs = []
    if runs_path.exists():
        for line in runs_path.read_text(encoding="utf-8").splitlines()[-40:]:
            if line.strip():
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    data["runs"] = runs
    data["workflow"] = workflow_all()
    data["selections"] = list(store.latest_selections(PROJECT).values())
    meta = dict(base["meta"])
    meta["decisions"] = store.summary(PROJECT)
    return {"data": data, "images": base["images"], "meta": meta}


STATUS_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>导演工作台 · P0 骨架</title>
<style>body{background:#0c0e11;color:#e7eaf0;font:14px/1.7 -apple-system,"PingFang SC",system-ui,sans-serif;padding:32px;max-width:900px;margin:0 auto}
h1{font-size:17px;font-weight:600}.k{color:#8b93a3}.v{color:#4a9eff;font-size:20px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
.card{background:#14171c;border:1px solid #242a33;border-radius:10px;padding:14px}
code{background:#191d24;padding:2px 6px;border-radius:4px;font-size:12px}</style></head><body>
<h1>导演工作台 · P0 骨架（真实数据，直连项目文件）</h1>
<p class="k">这一页由本地服务实时解析 <code>~/short_v/有谁共鸣2</code> 得到，不写任何文件。</p>
<div class="grid" id="g"></div>
<p class="k" id="meta"></p>
<script>
fetch('/api/data').then(r=>r.json()).then(({data,meta})=>{
  const s=data.stats;
  const c=(l,v)=>`<div class="card"><div class="k" style="font-size:12px">${l}</div><div class="v">${v}</div></div>`;
  document.getElementById('g').innerHTML =
    c('场次', s.scenes)+c('镜头', s.shots)+c('台词', s.dialogue)+c('断链', s.brokenOcc)+
    c('提示词覆盖', s.promptShots+'/'+s.shots)+c('短镜', s.shortShots.length)+
    c('地点缺参考', s.placesNoRef+'/'+s.places)+c('分镜图', '0/'+s.shots);
  document.getElementById('meta').textContent =
    `解析耗时 ${meta.buildSeconds}s · 构建于 ${meta.builtAt}`;
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if UI.exists():
                body = UI.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(200, STATUS_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            body = json.dumps(response(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/execute":
            self._send(200, json.dumps(exec_status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/workflow":
            self._send(200, json.dumps({"workflow": workflow_all()}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/batch":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bid = q.get("id", [""])[0]
            if not bid:
                self._send(400, b'{"error":"missing id"}', "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(batch_detail(bid), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/batches":
            body = json.dumps({"batches": list_batches(),
                               "decisions": store.latest(PROJECT)}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/export":
            md = export_markdown().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="EP002-review-notes.md"')
            self.send_header("Content-Length", str(len(md)))
            self.end_headers()
            self.wfile.write(md)
        elif path == "/media":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            f = resolve_media(int(q.get("r", ["0"])[0]), q.get("p", [""])[0])
            if not f:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            self._serve_file(f, "video/mp4")
        elif path == "/thumb":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            f = resolve_media(int(q.get("r", ["0"])[0]), q.get("p", [""])[0])
            th = thumb_of(f) if f else None
            if not th:
                self._send(404, b'{"error":"no thumb"}', "application/json; charset=utf-8")
                return
            self._send(200, th.read_bytes(), "image/jpeg")
        elif path == "/api/health":
            body = json.dumps({
                "ok": True,
                "project": str(PROJECT),
                "episode": EPISODE,
                "stateDir": str(store.state_dir(PROJECT)),
                "mediaRoots": [str(x) for x in media_roots()],
                "ui": str(UI),
                "decisions": len(store.latest(PROJECT)),
                "selections": len(store.latest_selections(PROJECT)),
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

    def _serve_file(self, f: Path, ctype: str):
        """带 Range 的文件服务，视频才能拖动进度。"""
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            end = min(end, size - 1)
            code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/preflight":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = preflight(body)
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/execute":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = execute_batch(body)
                self._send(200 if out.get("ok") else 400,
                           json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/plan":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = plan_tasks(body)
                self._send(200 if out["ok"] else 400,
                           json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/selection":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                rec = store.select(PROJECT, body["shot"], body["candidate"],
                                   body["status"], body.get("reason"))
                self._send(200, json.dumps({"ok": True, "record": rec}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path != "/api/decision":
            self._send(404, b'{"ok":false,"error":"not found"}', "application/json; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            rec = store.append(
                PROJECT,
                gate=int(body.get("gate", 1)),
                unit=body["unit"],
                verdict=body["verdict"],
                notes=body.get("notes"),
                scope=body.get("scope"),
                evidence=payload()["data"]["files"],
            )
            self._send(200, json.dumps({"ok": True, "record": rec}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    payload()  # 预热
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"导演工作台 P0 骨架 → http://{a.host}:{a.port}")
    print(f"  项目 {PROJECT} · {EPISODE} · 仅监听本机")
    srv.serve_forever()


if __name__ == "__main__":
    main()
