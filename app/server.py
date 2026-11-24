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
import hashlib
import json
import tempfile
import subprocess
import threading
import translate
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os
import re

import agentrunner
import approvals
import artifacts
import assetsdb
import cloudimg
import comfyctl
import confirms
import comfywatch
import dataset
import flow
import firstframe
import gpus
import imagegen
import imgjobs
import sched
import scenes
import vjobs
import planner
import preflight as pf
import projects
import runner
import store

PROJECT = projects.active_path()
BOOT_PROJECT = PROJECT
EPISODE = "EP002"

_lock = threading.Lock()
_cache = {"by": {}, "sig": None}

WATCHED = ["剧本.md", "分镜.md", "视频提示词.md", "视觉设定.md"]


def file_sig(episode: str | None = None) -> str:
    """四个创作文件的 mtime+size 指纹，用于缓存失效。"""
    ep = PROJECT / "剧集" / (episode or EPISODE)
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

def _switch_project(target: str) -> Path:
    """切换当前项目；只在生成队列空闲时调用。"""
    global PROJECT, MEDIA
    p = projects.set_active(target)
    PROJECT = p
    store.ACTIVE_PROJECT = p
    MEDIA = Path(os.environ.get("WORKBENCH_MEDIA") or (PROJECT / "剧集" / EPISODE / "制作成果"))
    return p


THUMB_CACHE = {}


def _inside(root: Path, f: Path) -> bool:
    """严格判断 f 是否在 root 内（杜绝 /a/b 与 /a/bc 的前缀绕过）。"""
    try:
        return f.resolve().is_relative_to(Path(root).resolve())
    except AttributeError:            # Python < 3.9 兜底
        r = str(Path(root).resolve()).rstrip("/") + "/"
        return str(f.resolve()).startswith(r)


def resolve_asset(p: str):
    root = (PROJECT / "资产").resolve()
    f = (PROJECT / p).resolve()
    if not _inside(root, f) or not f.is_file():
        return None
    return f


def media_roots() -> list:
    return [MEDIA, store.state_dir(PROJECT) / "media"]


def resolve_media(r: int, p: str):
    """把 (root 索引, 相对路径) 解析成真实文件；拒绝越界。"""
    roots = media_roots()
    if r < 0 or r >= len(roots):
        return None
    root = roots[r].resolve()
    f = (root / p).resolve()
    if not _inside(root, f) or not f.is_file():
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


def _version_of(path: str):
    m = re.search(r"-v(\d{3})(?:-\d+)?\.md$", path or "")
    return f"v{m.group(1)}" if m else ""


def _scope_obj(scope):
    """artifact-state 的 scope 是字符串；审批账本只收对象，这里做唯一归一。"""
    if isinstance(scope, dict):
        return scope
    if isinstance(scope, str) and scope.strip():
        return {"label": scope.strip()}
    return {}


def _unit_shots(unit: dict) -> list:
    """从「SH001、SH002」里取镜号；解析不到就返回空，由门禁阻断。"""
    return re.findall(r"SH\d{3}", unit.get("shots") or "")


def _prompt_confirm(rel: str) -> dict:
    """提示词版本的文件级确认状态；以 approvals 为准。"""
    rec = approvals.file_ok(PROJECT, rel)
    if rec:
        return {"local": "confirmed", "at": rec.get("at"), "approval": rec.get("id"),
                "sha256": rec.get("sha256"), "version": rec.get("version")}
    st = approvals.state(PROJECT, "file", rel)
    if st.get("record"):
        return {"local": "stale" if st.get("stale") else "rejected",
                "at": st["record"].get("at"), "reason": st.get("reason", ""),
                "version": st["record"].get("version")}
    return {"local": "none"}


STALE_ART = ("已过期-禁止使用", "历史已替代", "已作废（不合格）", "缺文件-阻断")


def _artifact_status(project: Path) -> dict:
    reg = artifacts.load_registry(project)
    return {a.get("path"): a.get("status") for a in (reg.get("artifacts") or [])}


def _stale_targets(scene: str, units: list, rel: str) -> list:
    """本次生成直接引用的成果文件里，已被登记表标为过期的路径。"""
    targets = {rel}
    for item in units:
        u = item.get("unit") or {}
        src = str(u.get("source") or "").strip()
        if src:
            targets.add(src if "/" in src else f"{vjobs.VP_DIR}/{scene}/{src}")
    return sorted(targets)


def _generation_gate(scene: str, units: list, rel: str) -> dict:
    """文件级版本锁 + 镜头级确认 + 成果状态锁；返回缺失清单和有效 approval ids。"""
    missing, ids = [], []
    status = _artifact_status(PROJECT)
    for target in _stale_targets(scene, units, rel):
        st = status.get(target)
        if st in STALE_ART:
            missing.append(f"成果已过期/禁止生成：{target}（{st}）")
    frec = approvals.file_ok(PROJECT, rel)
    if not frec:
        st = approvals.state(PROJECT, "file", rel)
        why = "已失效" if st.get("stale") else "未确认"
        missing.append(f"提示词文件{why}：{rel}")
    else:
        ids.append(frec["id"])
    for item in units:
        u = item.get("unit") or {}
        shots = _unit_shots(u)
        if not shots:
            missing.append(f"{u.get('id','?')}：无法解析覆盖镜头")
            continue
        for r in (u.get("refs") or []):
            if not r.get("exists"):
                missing.append(f"{u.get('id','?')}：参考图缺失 {r.get('file','?')}")
        for sid in shots:
            st = approvals.state(PROJECT, "shot", f"{scene}/{sid}")
            rec = st.get("record")
            if not rec or rec.get("verdict") != "pass" or st.get("stale"):
                why = "已失效" if st.get("stale") else ("未确认" if not rec else "不是通过")
                missing.append(f"{sid}：镜头{why}")
            else:
                ids.append(rec["id"])
    return {"ok": not missing, "missing": missing, "approvalIds": ids}


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
     "note": "章节索引 / 剧情单元 / 人物 / 分集候选",
     "prompt": "加载 short-drama-novel-analyze，把 <原著文件> 拆成可查证的拆解：章节索引、剧情单元、高光、人物与世界、分集候选。\n输出到 <项目根目录>/项目开发/source-analysis/。原文只读，不虚构，每条结论标出处。"},
    {"key": "bible", "name": "小说精读 · 人物圣经", "skill": "novel-analyzer",
     "projectDoc": "项目开发/source-analysis/character-bible/人物圣经.md",
     "note": "逐人物弧光与证据链；产出供大纲与剧本引用",
     "prompt": "加载 novel-analyzer，精读 <原著文件> 全文（正文；序言不入）。\n产出人物圣经：每人的动机、行为逻辑、阶段弧光、关系演变，每条带原文行号引用。资料不足处写「原文未明确」，不凭常识补写。\n输出到 <项目根目录>/项目开发/source-analysis/character-bible/。"},
    {"key": "develop", "name": "改编方案与分集大纲", "skill": "short-drama-develop",
     "projectDoc": "项目开发/episode-map.jsonl",
     "prompt": "加载 short-drama-develop，基于 <拆解目录> 与 <人物圣经>。\n重写 <项目根目录>/项目开发/episode-map.jsonl：覆盖原著正文全部章节，不跳章；集数与每集时长由内容决定。\n每集写清钩子、目标、对抗、转折、本集结果、与下一集交接。最后告诉我一共多少集。"},
    {"key": "script", "name": "剧本", "skill": "short-drama-write + performance-direction",
     "doc": "剧本.md",
     "prompt": "加载 short-drama-write 与 performance-direction，写 <项目根目录>/剧集/<集号>/剧本.md。\n原著章节范围以 <项目根目录>/项目开发/episode-map.jsonl 中 <集号> 的 source_chapter_range 为准；原著对白优先。\n表演按 开始状态 / 触发 / 过程 / 结束状态 / 声音 写清；删改、合并、叙述转对白、新增对白逐条声明在集末。"},
    {"key": "review1", "name": "审核一 · 剧本", "skill": "导演 · 逐场判定", "gate": 1,
     "prompt": "（导演本人执行）在工作台「审核 · 剧本」逐场判定。\n通过 / 需修改 / 待定，意见绑定到具体台词或段落。通过即定版，记录文件哈希与覆盖范围。"},
    {"key": "visual", "name": "视觉设定", "skill": "short-drama-assets + production-design-direction",
     "doc": "视觉设定.md",
     "prompt": "加载 short-drama-assets 与 production-design-direction，基于 <项目根目录>/剧集/<集号>/剧本.md 写 <项目根目录>/剧集/<集号>/视觉设定.md。\n人物：身份 → 造型 → 状态；地点：地点本体 → 视角；道具：状态变化与持有人。\n场景写清空间布局、材质、时代细节与光线来源。"},
    {"key": "look", "name": "光线圣经 + 摄影基调",
     "skill": "lighting-direction + cinematography",
     "note": "写入 视觉设定.md 的「光线与摄影基调」一节",
     "prompt": "加载 lighting-direction 与 cinematography，为 <集号> 写两段约束：\n1) 光线圣经：每场时间、动机光源、方向、软硬、色温、对比、受光连续性、禁止项\n2) 摄影基调：景别策略、焦段倾向、运镜规则、构图禁忌\n追加到 <项目根目录>/剧集/<集号>/视觉设定.md 末尾，不改写已有内容。"},
    {"key": "imgprompt", "name": "图片提示词",
     "skill": "short-drama-image-prompts + minimax-image-h3-prompts",
     "doc": "图片提示词.md",
     "prompt": "加载 short-drama-image-prompts 与 minimax-image-h3-prompts，基于 <项目根目录>/剧集/<集号>/视觉设定.md（含「光线与摄影基调」）写 <项目根目录>/剧集/<集号>/图片提示词.md。\n内容按短剧规范写，成稿按 H3 Image 的 caption 规范改写（十二维度、单帧输出、16:9、宽高 32 倍数）。"},
    {"key": "gen1", "name": "出图① 资产图", "skill": "工作台 · 图片批次", "gate": "img",
     "prompt": "在工作台「出图 · 资产」页：勾选 <资产范围> → 每项 <候选数> 张 → 签字批准 → 生成 → 挑候选 → 采用。\n出图前确认参考图与提示词就绪；耗时未标定，先出一张测时间。"},
    {"key": "review2", "name": "审核二 · 资产", "skill": "导演 · 逐项判定", "gate": 12,
     "prompt": "（导演本人执行）在工作台「审核二 · 资产」逐项判定人物、地点、道具。\n结合已采用的资产图判断造型与状态是否可用；资产项全剧共享，结论跨集生效。"},
    {"key": "manifest", "name": "素材清单", "skill": "asset-continuity-management",
     "note": "checksum / seed / 版本 / 派生关系 → 制作运行/manifest.jsonl",
     "prompt": "加载 asset-continuity-management，为已采用的素材建清单：asset_id、类型、文件路径、sha256、尺寸或时长、来源与派生关系。\n写入 <项目根目录>/制作运行/manifest.jsonl。审批状态以工作台记录为准，清单里不重复记。"},
    {"key": "staging", "name": "调度方案", "skill": "directing-cinematic-storyboards",
     "note": "分镜的依据：站位 / 视线 / 轴线 / 切点理由",
     "prompt": "加载 directing-cinematic-storyboards，为 <集号> 逐场写导演调度方案：\n空间关系、人物站位与走位、视线与轴线、为什么在这里切、必须保留的动作与物件。\n输出供分镜原样继承。"},
    {"key": "board", "name": "分镜", "skill": "short-drama-storyboard + performance-direction",
     "doc": "分镜.md",
     "prompt": "加载 short-drama-storyboard 与 performance-direction，基于 <项目根目录>/剧集/<集号>/剧本.md、<项目根目录>/剧集/<集号>/视觉设定.md（含光线与摄影基调）与上一步的导演调度方案写 <项目根目录>/剧集/<集号>/分镜.md。\n逐镜：来源/时长/目的/景别机位/运镜/起点/动作/终点/参考图/出场设定/声音/首帧提示词。\n轴线、站位、视线服从调度方案。"},
    {"key": "videoprompt", "name": "视频提示词",
     "skill": "short-drama-video-prompts + minimax-h3-video-prompts + performance-direction",
     "doc": "视频提示词.md",
     "prompt": "加载 short-drama-video-prompts 与 minimax-h3-video-prompts，基于 <项目根目录>/剧集/<集号>/分镜.md 逐镜写 <项目根目录>/剧集/<集号>/视频提示词.md。\n按 H3 六段式（subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music）；中文对白原生发声，non_diegetic_music: N/A。\n逐镜继承光源方向与色温；起止状态不得与分镜冲突。"},
    {"key": "gen2", "name": "出图② 分镜图", "skill": "工作台 · 图片批次", "gate": "img",
     "prompt": "在工作台「出图 · 分镜」页：选 <场次范围> 的首帧 → 每镜 <候选数> 张 → 签字批准 → 生成 → 采用。\n必须先定人物资产图，再出分镜图。"},
    {"key": "test", "name": "试做", "skill": "工作台 · 视频生成（草稿档）", "gate": "vid",
     "prompt": "在工作台「视频生成」页：选 <代表镜头>（3–5 个）→ 草稿档 → 签字提交 → 回片 → 挑片。\n试做只判断拍法、动作、视线、对白完整性、衔接与节奏。"},
    {"key": "review3", "name": "审核三 · 分镜", "skill": "导演 · 逐镜判定", "gate": 2,
     "prompt": "（导演本人执行）在工作台「审核三 · 分镜」逐镜判定：看分镜图、试做片、视频提示词。\n通过 / 需修改 / 待定；通过后可单独批准生成批次。"},
    {"key": "batch", "name": "批次生成", "skill": "工作台 · 视频生成", "gate": 3,
     "prompt": "在工作台「视频生成」页：确认预检无阻断 → 构造任务 → 批准并提交（会记一条批次授权）→ 回片后进入审核四。"},
    {"key": "review4", "name": "审核四 · 挑片", "skill": "导演 · 采用 / 备选 / 重做", "gate": "pick",
     "prompt": "（导演本人执行）在工作台「审核四 · 挑片」逐镜看候选，采用 / 备选 / 重做，并标注原因。\n采用不覆盖原文件；生成成功不等于采用。"},
]


def _script_title(ep_dir: Path):
    """从 剧本.md 的第一行标题里取出集名，用于与大纲比对。"""
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


def _video_recovery_loop():
    """每 30 秒认领一次孤儿产物：进程重启后仍在 ComfyUI 里跑完的任务。

    只恢复启动项目，避免切换项目后把另一个项目的任务重新提交。
    """
    while True:
        try:
            vjobs.reconcile(BOOT_PROJECT)
            vjobs.sweep()
            got = vjobs.recover(BOOT_PROJECT)
            if got:
                print("[recover] 认领视频：", got, flush=True)
        except Exception as e:
            print("[recover] 出错：", e, flush=True)
        time.sleep(30)


_PROBE_CACHE = {}


def _probe_video(f: Path) -> dict:
    """读分辨率/帧数，按 路径+大小+修改时间 缓存。"""
    st = f.stat()
    key = f"{f}|{st.st_size}|{int(st.st_mtime)}"
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    out = {}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,nb_frames",
                            "-of", "json", str(f)], capture_output=True, timeout=20)
        s = (json.loads(r.stdout.decode() or "{}").get("streams") or [{}])[0]
        out = {"w": s.get("width"), "h": s.get("height"),
               "frames": int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None}
    except Exception:
        out = {}
    _PROBE_CACHE[key] = out
    return out


WF_DIR = Path("/checkpoints/huxian/h3/ComfyUI/user/default/workflows")
UNSUPPORTED = {
    "SplitSigmas": "分段采样",
}


def scan_workflows() -> list:
    """扫描 ComfyUI 已保存工作流，判断工作台能否复刻。"""
    out = []
    if not WF_DIR.is_dir():
        return out
    for f in sorted(WF_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        nodes = d.get("nodes") or []
        types = [n.get("type") for n in nodes]
        def first_widget(tp):
            for n in nodes:
                if n.get("type") == tp:
                    w = n.get("widgets_values") or []
                    return w[0] if w else None
            return None

        model = first_widget("UNETLoader")
        h3 = next((n for n in nodes if n.get("type") == "MiniMaxH3ReferenceToVideo"), None)
        sched = next((n for n in nodes if n.get("type") == "BasicScheduler"), None)
        lora = first_widget("LoraLoaderModelOnly")
        sampler = first_widget("KSamplerSelect")
        hw = h3.get("widgets_values") if h3 else None
        width = hw[1] if hw and len(hw) > 3 else None
        height = hw[2] if hw and len(hw) > 3 else None
        ref_size = hw[4] if hw and len(hw) > 4 else None
        steps = None
        if sched and len(sched.get("widgets_values") or []) >= 2:
            steps = sched["widgets_values"][1]
        blockers = [why for node, why in UNSUPPORTED.items() if node in types]
        progressive = "ManualSigmas" in types and types.count("SamplerCustomAdvanced") == 1
        if types.count("SamplerCustomAdvanced") > 1:
            blockers.append("多段采样（%d 个采样器）" % types.count("SamplerCustomAdvanced"))

        # 开关节点只用来切「LoRA / 裸模型」两支：按连线解析当前实际分支
        variants = None
        by_id = {n.get("id"): n for n in nodes}
        links = {l[0]: l for l in (d.get("links") or []) if isinstance(l, list) and len(l) >= 5}

        def src_of(node, input_name):
            for inp in node.get("inputs") or []:
                if inp.get("name") == input_name:
                    link = links.get(inp.get("link"))
                    return by_id.get(link[1]) if link else None
            return None

        if "ComfySwitchNode" in types:
            sw = next(n for n in nodes if n.get("type") == "ComfySwitchNode")
            flag_node = src_of(sw, "switch")
            flag = bool((flag_node or {}).get("widgets_values", [False])[0])
            chosen = src_of(sw, "on_true" if flag else "on_false")
            chosen_type = (chosen or {}).get("type")
            if chosen_type == "LoraLoaderModelOnly":
                lora = (chosen.get("widgets_values") or [None])[0]
            elif chosen_type == "UNETLoader":
                lora = None
            # 步数：找到 BasicScheduler 的 steps 输入来源
            steps_src = src_of(sched, "steps") if sched else None
            if steps_src and steps_src.get("type") == "ComfySwitchNode":
                steps = None            # 步数也走开关，下面按分支取
                for nm in ("on_false", "on_true"):
                    branch = src_of(steps_src, nm)
                    val = (branch or {}).get("widgets_values", [None])[0]
                    variants = variants or {}
                    variants["on_true" if nm == "on_true" else "on_false"] = val
                want = "on_true" if flag else "on_false"
                steps = (variants or {}).get(want)
            variants = variants or {}
            variants["switchOn"] = flag
        active = ("LoRA 支 · %d 步" % steps) if lora else ("裸模型支 · %d 步" % steps if steps else "—")
        if not (model and "ref2va" in str(model)):
            blockers.append("不是 Ref2VA 视频工作流")
        if not h3:
            blockers.append("没有 H3 参考视频节点")
        if not sched and "ManualSigmas" not in types:
            blockers.append("没有可识别的步数调度")
        out.append({
            "file": f.name, "sizeKB": round(f.stat().st_size / 1024),
            "mtime": int(f.stat().st_mtime),
            "nodes": len(nodes), "model": model, "steps": steps, "lora": lora,
            "variants": variants, "active": active,
            "progressive": progressive,
            "sigmas": (next(((n.get("widgets_values") or [None])[0] for n in nodes
                             if n.get("type") == "ManualSigmas"), None) if progressive else None),
            "sampler": sampler, "width": width, "height": height, "refSize": ref_size,
            "ok": not blockers, "blockers": blockers,
        })
    return out


def _recipe_label(j: dict) -> str:
    """历史任务的工作流标签：优先用记录，没有就按 LoRA/步数反推。"""
    name = j.get("recipe")
    if name and name in vjobs.RECIPES:
        r = vjobs.RECIPES[name]
        return f"{name} · {j.get('steps') or r['steps']} 步"
    lora = j.get("lora")
    steps = j.get("steps")
    if lora:
        short = "turbo4" if "4step" in lora else "lora"
        return f"{short} · {steps or '?'} 步"
    if steps:
        return f"base20 · {steps} 步"      # 裸模型系列，步数如实记
    return "—"


def _guess_render(j: dict):
    """老记录把渲染耗时存在 seconds 里；片长只有十几秒，用 60 秒做分界。"""
    s = j.get("seconds")
    try:
        return float(s) if s and float(s) > 60 else None
    except Exception:
        return None


def gen_history(include_deleted: bool = False) -> dict:
    """导演台生成过的视频历史：以磁盘文件为准，任务记录补充耗时/规格。"""
    root = PROJECT / vjobs.RESULT_DIR
    by_file = {}
    for j in vjobs.list_jobs(project=PROJECT):
        if j.get("file"):
            by_file[j["file"]] = j
    items = []
    if root.is_dir():
        for f in root.rglob("*.mp4"):
            rel = str(f.relative_to(PROJECT))
            st = f.stat()
            j = by_file.get(rel) or {}
            pr = _probe_video(f)
            items.append({
                "file": rel, "name": f.name,
                "unit": j.get("unit") or f.parent.name,
                "seed": j.get("seed") or (f.stem if f.stem.isdigit() else None),
                "scene": j.get("scene") or "N001",
                "sizeMB": round(st.st_size / 2 ** 20, 1),
                "mtime": int(st.st_mtime),
                "time": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                "w": pr.get("w"), "h": pr.get("h"),
                "frames": pr.get("frames") or j.get("frames"),
                "steps": j.get("steps"),
                "renderSec": j.get("renderSec") or _guess_render(j),
                "source": j.get("source") or ("workbench" if j else "disk"),
                "recipe": _recipe_label(j) if j else "—",
                "refs": len(j.get("refs") or []),
            })
    if include_deleted:
        present = {i["file"] for i in items}
        for j in vjobs.list_jobs(project=PROJECT):
            if j.get("status") == "deleted" and j.get("file") and j["file"] not in present:
                f = j.get("file")
                items.append({
                    "file": f, "name": f.split("/")[-1],
                    "unit": j.get("unit") or f.split("/")[-2],
                    "seed": j.get("seed"), "scene": j.get("scene") or "N001",
                    "sizeMB": None, "mtime": int(j.get("deletedAt") or j.get("finishedAt") or 0),
                    "time": time.strftime("%m-%d %H:%M", time.localtime(j.get("deletedAt") or 0))
                            if j.get("deletedAt") else "—",
                    "w": None, "h": None,
                    "frames": j.get("frames"), "steps": j.get("steps"),
                    "renderSec": j.get("renderSec") or _guess_render(j),
                    "source": "deleted", "recipe": _recipe_label(j),
                    "refs": len(j.get("refs") or []),
                    "deleted": True,
                })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    known = [i for i in items if i.get("renderSec") and i.get("frames")]
    rate = round(sum(i["renderSec"] / i["frames"] for i in known) / len(known), 2) if known else None
    return {"items": items, "count": len(items),
            "totalMB": round(sum(i["sizeMB"] for i in items
                                 if isinstance(i.get("sizeMB"), (int, float))), 1),
            "ratePerFrame": rate,
            "measured": len(known)}


def backend_state(project=None) -> dict:
    """算力页：只列**已启动**的实例。"""
    cards = gpus.probe()
    live = [c for c in cards if c["state"] != "offline"]
    img_jobs = imgjobs.list_jobs(project=project)
    vid_jobs = vjobs.list_jobs(project=project)
    inst = []
    for c in live:
        port = c["port"]
        st = comfywatch.instance_state(port)
        pids = comfyctl._pids_on(port)
        pid = pids[0] if pids else None
        started = None
        if pid:
            try:
                up_s = float(open("/proc/uptime").read().split()[0])
                ticks = float(open(f"/proc/{pid}/stat").read().split()[21])
                started = max(0.0, up_s - ticks / 100.0)
            except Exception:
                started = None
        job = None
        for j in (img_jobs + vid_jobs):
            if j.get("card") == c["index"] and j.get("status") in ("running", "starting"):
                job = {"kind": ("video" if j in vid_jobs else "image"),
                       "label": j.get("unit") or j.get("dir") or j.get("asset") or j["id"],
                       "id": j["id"],
                       "elapsed": round(time.time() - (j.get("startedAt") or time.time())),
                       "promptId": j.get("promptId")}
                break
        vm = comfywatch.vram(c["index"]) or {}
        inst.append({
            "card": c["index"], "port": port, "pid": pid,
            "uptimeSec": round(started) if started else None,
            "state": c["state"], "enabled": c["index"] in set(sched.snapshot().get("enabled") or []),
            "vramUsedGB": vm.get("usedGB"), "vramTotalGB": vm.get("totalGB", c.get("vramTotalGB")),
            "executing": st.get("executing"), "progress": st.get("progress"),
            "queue": st.get("queue"), "error": st.get("error"), "job": job,
        })
    running_cards = {i["card"] for i in inst}
    startable = []
    for c in cards:
        if c["index"] in running_cards:
            continue
        vm = comfywatch.vram(c["index"]) or {}
        used = vm.get("usedGB")
        if used is not None and used < 8:
            startable.append({"card": c["index"], "vramUsedGB": used})
    JOBS = []
    for j in vid_jobs:
        if j["status"] in ("queued", "running", "starting", "waiting", "dispatched"):
            JOBS.append({"id": j["id"], "kind": "video", "unit": j.get("unit"),
                         "seq": j.get("seq"), "scene": j.get("scene"),
                         "status": j["status"], "card": j.get("card"),
                         "frames": j.get("frames"), "seconds": j.get("seconds"),
                         "queuedAt": j.get("queuedAt"), "startedAt": j.get("startedAt"),
                         "batch": j.get("batch"), "promptId": j.get("promptId"),
                         "queueState": j.get("queueState"),
                         "refs": len(j.get("usedRefs") or j.get("refs") or []),
                         "steps": j.get("steps"),
                         "execStartedAt": j.get("execStartedAt")})
    for j in img_jobs:
        if j["status"] in ("queued", "running", "starting", "waiting", "dispatched"):
            JOBS.append({"id": j["id"], "kind": "image", "unit": j.get("dir") or j.get("asset"),
                         "seq": None, "scene": None, "status": j["status"],
                         "card": j.get("card"), "frames": None, "seconds": None,
                         "queuedAt": j.get("queuedAt"), "startedAt": j.get("startedAt"),
                         "batch": j.get("batch")})
    # 状态：以 ComfyUI 实际队列为准
    run_ids, pend_ids, card_of = set(), set(), {}
    for c in cards:
        if c["state"] == "offline":
            continue
        q = gpus.queue_of(c["port"])
        run_ids |= set(q.get("runningIds") or [])
        pend_ids |= set(q.get("pendingIds") or [])
        for pid in (q.get("runningIds") or []) + (q.get("pendingIds") or []):
            card_of[pid] = c["index"]
    for j in JOBS:
        pid = j.get("promptId")
        if pid and pid in run_ids:
            j["status"] = "running"
            j["card"] = card_of.get(pid, j.get("card"))
        elif pid and pid in pend_ids:
            j["status"] = "waiting"
            j["card"] = card_of.get(pid, j.get("card"))
        elif j["status"] == "running":
            j["status"] = "dispatched"      # 已派出但还没在 ComfyUI 队列里看到

    # 单元内的第几次：已有产物数 + 在途顺序
    # 渲染速率：优先用本机实测（完成任务的 渲染秒/帧），没有就用 6.8 秒/帧（20 步实测均值）
    measured = [(j.get("renderSec") or 0) / j["frames"]
                for j in vid_jobs
                if j.get("renderSec") and j.get("frames") and (j.get("steps") or 20) == 20]
    measured = [x for x in measured if x > 0.5]
    RATE_20 = sum(measured) / len(measured) if measured else 6.8

    def rate_of(steps, frames):
        return RATE_20 * ((steps or 20) / 20.0)

    import re as _r
    def count_results(scene, unit):
        root = PROJECT / vjobs.RESULT_DIR / (scene or "N001")
        if not root.is_dir():
            return 0
        n = 0
        for f in root.rglob("*.mp4"):
            m = _r.match(r"(U\d{3})", f.stem)
            if (m.group(1) if m else f.parent.name) == unit:
                n += 1
        return n
    seen = {}
    JOBS.sort(key=lambda x: (x.get("queuedAt") or 0))
    for j in JOBS:
        key = (j.get("scene"), j.get("unit"))
        base = count_results(*key)
        seen[key] = seen.get(key, 0) + 1
        j["seq"] = base + seen[key]
        # 预计耗时：按帧数与步数折算；只在"生成中"给倒计时
        f = j.get("frames") or 0
        steps = j.get("steps") or 20
        total = f * rate_of(steps, f) if f else None
        # 已用 = 真正开始渲染到现在；还没开始就报等待时间
        if j["status"] == "running":
            el = time.time() - (j.get("execStartedAt") or j.get("startedAt") or time.time())
        elif j["status"] in ("waiting", "queued"):
            el = 0
        else:
            el = 0
        j["totalSec"] = round(total) if total else None
        j["elapsedSec"] = round(el) if el else None
        j["etaSec"] = round(max(0, total - el)) if (total and el and j["status"] == "running") else None
        j["execStartedAt"] = j.get("execStartedAt")
        j["waitSec"] = round(time.time() - (j.get("queuedAt") or time.time())) if j["status"] in ("waiting", "queued") else None
    JOBS.sort(key=lambda x: (x["status"] not in ("running",), x.get("queuedAt") or 0))
    # 8 张卡全景（用户手动管理算力时用）
    inst_by_card = {i["card"]: i for i in inst}
    all_cards = []
    for c in cards:
        vm = comfywatch.vram(c["index"]) or {}
        used, total = vm.get("usedGB"), vm.get("totalGB")
        pct = round(used / total * 100) if (used is not None and total) else None
        if pct is None:
            state = "unknown"
        elif pct >= 80:
            state = "full"
        elif pct >= 15:
            state = "busy"
        else:
            state = "free"
        all_cards.append({
            "card": c["index"], "port": c["port"],
            "usedGB": used, "totalGB": total, "pct": pct, "state": state,
            "hasInstance": c["index"] in inst_by_card,
            "instance": inst_by_card.get(c["index"]),
        })
    return {"ratePerFrame": round(RATE_20, 2),
            "instances": inst, "cards": all_cards, "jobs": JOBS, "startable": startable,
            "summary": {"instances": len(inst),
                        "pendingLocal": sum(1 for j in JOBS if j["status"] == "queued"),
                        "queueTotal": sum((i["queue"] or {}).get("pend", 0) for i in inst),
                        "running": sum(1 for i in inst if i.get("executing"))}}


def _novel_path() -> str:
    d = PROJECT / "输入"
    if d.exists():
        for f in sorted(d.glob("*.txt")):
            return f"输入/{f.name}"
    return "输入/<原著文件>"


def render_prompt(tpl: str, ep_id: str) -> str:
    """把能确定的占位符换成真实值，只留下需要用户当场决定的。"""
    mapping = {
        "<项目根目录>": str(PROJECT),
        "<集号>": ep_id,
        "<原著文件>": str(PROJECT / _novel_path()),
        "<拆解目录>": str(PROJECT / "项目开发/source-analysis"),
        "<人物圣经>": str(PROJECT / "项目开发/source-analysis/character-bible/人物圣经.md"),
    }
    out = tpl
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out


def workflow_of(ep_id: str, decisions: list, cands: list, sels=None, cur=None) -> dict:
    ep_dir = PROJECT / "剧集" / ep_id
    sels = sels or []
    titles = next((e for e in payload()["data"]["episodes"] if e["id"] == ep_id), {})
    map_title = titles.get("title")
    script_title = _script_title(ep_dir)
    mismatch = bool(script_title and map_title and script_title != map_title)
    # 判定与候选都按集过滤：单元 id 里带集号才算本集；资产项是全剧共享，不参与单集进度
    decision = [d for d in decisions if ep_id in d["unit"]["id"]]
    # 当前已解析集的场/镜总数，用于显示"已审 x/N"
    same = (cur is not None and cur.get("episode") == ep_id)
    cur_scenes = len(cur["scenes"]) if same else None
    cur_shots = len(cur["shots"]) if same else None
    cur_assets = None
    if same:
        a = cur.get("assets") or {}
        cur_assets = len(a.get("people", [])) + len(a.get("places", [])) + len(a.get("props", []))
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
            n = sum(1 for d in decision if d["gate"] == 1 and d["unit"]["type"] == "scene")
            full = bool(cur_scenes) and n >= cur_scenes
            st = "done" if full else ("progress" if n or (ep_dir / "剧本.md").exists() else "pending")
            detail = (f"已审 {n}/{cur_scenes} 场" if cur_scenes else f"已审 {n} 场") if n else "未审"
            basis = f"本集场景判定记录 {n} 条（共 {cur_scenes if cur_scenes else '?'} 场）"
        elif s.get("gate") == 12:
            n = sum(1 for d in decisions
                    if d["gate"] == 1 and d["unit"]["type"] == "asset")
            total = cur_assets
            full = bool(total) and n >= total
            st = "done" if full else ("progress" if n or (ep_dir / "视觉设定.md").exists() else "pending")
            detail = (f"已审 {n}/{total} 项" if total else f"已审 {n} 项") if n else "未审"
            basis = f"全剧资产判定 {n} 条、本集资产项 {total if total else '?'} 项（跨集共享）"
        elif s.get("gate") == "pick":
            n = sum(1 for x in sels if ep_id in (x.get("shot") or ""))
            full = bool(cur_shots) and n >= cur_shots
            st = "done" if full else ("progress" if n else "pending")
            detail = f"已采用 {n} 条" if n else "未挑"
            basis = f"本集挑片记录 {n} 条"
        elif s.get("gate") == 2:
            n = sum(1 for d in decision if d["gate"] == 2)
            full = bool(cur_shots) and n >= cur_shots
            st = "done" if full else ("progress" if n else "pending")
            detail = (f"已审 {n}/{cur_shots} 镜" if cur_shots else f"已审 {n} 镜") if n else "未审"
            basis = f"本集镜头判定 {n} 条（共 {cur_shots if cur_shots else '?'} 镜）"
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
        item = {k: s[k] for k in ("key", "name", "skill", "note") if k in s}
        if s.get("prompt"):
            item["prompt"] = render_prompt(s["prompt"], ep_id)
        if s["key"] == "review1":
            item["ready"] = (ep_dir / "剧本.md").exists()
        elif s["key"] == "review2":
            item["ready"] = (ep_dir / "视觉设定.md").exists()
        elif s["key"] == "review3":
            item["ready"] = (ep_dir / "分镜.md").exists()
        elif s["key"] == "review4":
            item["ready"] = any(ep_id in (x.get("shot") or "") for x in sels)
        steps.append({**item,
                      "no": i, "status": st, "detail": detail, "basis": basis})
    sels = sels or []
    return {"episode": ep_id, "exists": ep_dir.exists(), "steps": steps,
            "mapTitle": map_title, "scriptTitle": script_title, "titleMismatch": mismatch,
            "picks": sum(1 for s in sels if ep_id in (s.get("shot") or "")),
            "candidates": len([c for c in cands]),
            "chapters": titles.get("chapters"), "target": titles.get("target")}


def workflow_all(cur=None) -> list:
    dec, cands = store.latest(PROJECT), list_candidates()
    sels = list(store.latest_selections(PROJECT).values())
    return [workflow_of(e["id"], dec, cands, sels, cur) for e in payload()["data"]["episodes"]]


def dev_summary() -> dict:
    """原著拆解 / 人物圣经 / 分集大纲 的结构化摘要（只读）。"""
    sa = PROJECT / "项目开发/source-analysis"
    bible = sa / "character-bible"

    files = []
    if sa.exists():
        for f in sorted(sa.glob("*.md")):
            files.append({"name": f.name, "kb": max(1, f.stat().st_size // 1024)})

    characters, total_ev = [], 0
    meta = {}
    mf = bible / "metadata.json"
    if mf.exists():
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    for item in meta.get("main_characters", []):
        name = item.split("（")[0].strip()
        ev = 0
        ef = bible / "profiles" / "_evidence" / f"{name}.json"
        if ef.exists():
            try:
                ev = int(json.loads(ef.read_text(encoding="utf-8")).get("evidence_count") or 0)
            except Exception:
                ev = 0
        total_ev += ev
        characters.append({
            "name": name,
            "desc": item[len(name):].strip("（）"),
            "evidence": ev,
            "profile": f"profiles/profile_{name}.md",
        })

    return {
        "sourceAnalysis": {
            "exists": sa.exists(),
            "files": files,
            "chapters": meta.get("metadata", {}).get("total_lines"),
            "chars": meta.get("metadata", {}).get("total_chars_no_newline"),
        },
        "bible": {
            "exists": (bible / "人物圣经.md").exists(),
            "characters": characters,
            "totalEvidence": total_ev,
            "minor": meta.get("minor_characters", []),
            "world": meta.get("world_setting", ""),
            "sha256": meta.get("source_sha256", ""),
        },
    }


def exec_status() -> dict:
    return dict(EXEC, log=EXEC["log"][-12:])


def plan_tasks(body: dict) -> dict:
    """构造任务并落 tasks.jsonl；不提交、不烧卡。"""
    shots = body.get("shots") or []
    res = pf.run({"kind": "video", "items": [{"shot": s} for s in shots]}, payload()["data"])
    if res["blockers"]:
        return {"ok": False, "blockers": res["blockers"], "warnings": res["warnings"]}
    batch_id = "PLAN-" + time.strftime("%Y%m%d-%H%M%S")
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


def review_export() -> str:
    """把 approvals 账本导出成导演审核单（四段 + 批次）。"""
    KIND = {"file": "文件", "scene": "场次", "asset": "资产", "shot": "镜头",
            "pick": "挑片", "batch": "批次"}
    VERD = {"pass": "通过", "revise": "需修改", "hold": "待定", "na": "不适用"}
    lines = ["# 导演审核单", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"项目：{PROJECT}", ""]
    recs = approvals.latest(PROJECT)
    if not recs:
        lines.append("- （尚未有任何审批）")
    for kind in ("scene", "asset", "shot", "file", "pick", "batch"):
        group = [r for r in recs if r.get("kind") == kind]
        if not group:
            continue
        lines += [f"## {KIND.get(kind, kind)}", ""]
        for r in group:
            st = approvals.state(PROJECT, kind, r.get("target"))
            stale = " · **已失效**" if st.get("stale") else ""
            lines.append(f"- `{r.get('target')}` — **{VERD.get(r.get('verdict'), r.get('verdict'))}**{stale}")
            if r.get("path"):
                lines.append(f"  - 依据：`{r['path']}` `{(r.get('sha256') or '')[:12]}`")
            if r.get("version"):
                lines.append(f"  - 版本：{r['version']}")
            for n in r.get("notes") or []:
                if n.get("text"):
                    lines.append(f"  - 意见：{n['text']}")
            lines.append(f"  - 记录：{r.get('id')} · {r.get('at')}")
        lines.append("")
    return "\n".join(lines)


def payload(ep: str = EPISODE, refresh: bool = False) -> dict:
    with _lock:
        sig = file_sig(ep)
        if _cache.get("sig") != sig:
            _cache["by"].clear()
            _cache["sig"] = sig
        if ep not in _cache["by"]:
            t0 = time.time()
            out = dataset.load(PROJECT, ep)
            out["meta"] = {"builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "buildSeconds": round(time.time() - t0, 2),
                           "project": str(PROJECT)}
            _cache["by"][ep] = out
        return _cache["by"][ep]


def overview() -> dict:
    """新模型的页头/总览数据；不再读已废弃的 剧集/EP002 四文档。"""
    sc = scenes.build(PROJECT)
    ast = assetsdb.build(PROJECT)
    st = artifacts.build(PROJECT)
    shots = sum(int(s.get("shots") or 0) for s in sc.get("scenes") or [])
    return {
        "scenes": {**sc.get("summary", {}), "count": sc.get("summary", {}).get("scenes", 0)},
        "shots": shots,
        "assets": ast.get("summary", {}),
        "stages": st.get("summary", {}),
        "episodes": [{"id": e["id"], "scenes": e["scenes"]} for e in sc.get("episodes", [])],
    }


def current_episode() -> str:
    """当前集：最后一个已写出剧本的集；一集都没有时取第一集。"""
    eps = payload()["data"]["episodes"]
    if not eps:
        return EPISODE
    with_script = [e["id"] for e in eps if (PROJECT / "剧集" / e["id"] / "剧本.md").exists()]
    return with_script[-1] if with_script else eps[0]["id"]


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


def response(ep: str | None = None) -> dict:
    """解析数据可缓存；决策每次现读，保证刚落账立刻可见。"""
    ep = ep or current_episode()
    base = payload(ep)
    data = dict(base["data"])
    data["currentEpisode"] = current_episode()
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
    data["workflow"] = workflow_all(data)
    data["dev"] = dev_summary()
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
        self.query_path = self.path
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
        elif path == "/api/projects":
            self._send(200, json.dumps({"active": str(PROJECT), "projects": projects.discover()},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/agent/config":
            self._send(200, json.dumps(agentrunner.public_config(PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/agent/tasks":
            self._send(200, json.dumps({"items": agentrunner.list_tasks(PROJECT)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/data":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            body = json.dumps(response(q.get("ep", [None])[0]), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/overview":
            self._send(200, json.dumps(overview(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/workflow/steps":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            scene = q.get("scene", ["N001"])[0]
            if not re.fullmatch(r"N\d{3}", scene):
                self._send(400, b'{"error":"scene must be Nxxx"}', "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(flow.steps_payload(PROJECT, scene), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/approvals":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            kind = q.get("kind", [""])[0]
            target = q.get("target", [""])[0]
            scene = q.get("scene", [""])[0]
            items = approvals.latest(PROJECT)
            if kind:
                items = [x for x in items if x.get("kind") == kind]
            if target:
                items = [x for x in items if x.get("target") == target]
            if scene:
                items = [x for x in items
                         if x.get("target", "").startswith(scene + "/")
                         or (x.get("scope") or {}).get("scene") == scene]
            self._send(200, json.dumps({"items": items, "summary": approvals.summary(PROJECT)},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/execute":
            self._send(200, json.dumps(exec_status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/workflow":
            self._send(200, json.dumps({"workflow": workflow_all()}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/proj/audio":
            qa = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            rel = qa.get("p", [""])[0]
            f = (PROJECT / rel).resolve()
            root = PROJECT.resolve()
            if (not str(f).startswith(str(root)) or not f.is_file()
                    or f.suffix.lower() not in (".wav", ".mp3", ".flac", ".m4a")):
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            ctype = {"wav": "audio/wav", "mp3": "audio/mpeg",
                     "flac": "audio/flac", "m4a": "audio/mp4"}[f.suffix.lower()[1:]]
            data = f.read_bytes()
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    s = int(m.group(1) or 0)
                    e = int(m.group(2)) if m.group(2) else len(data) - 1
                    e = min(e, len(data) - 1)
                    chunk = data[s:e + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {s}-{e}/{len(data)}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/proj/text":
            qt = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            rel = qt.get("p", [""])[0]
            f = (PROJECT / rel).resolve()
            root = PROJECT.resolve()
            TEXT = (".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml")
            if (not _inside(root, f) or not f.is_file()
                    or f.suffix.lower() not in TEXT):
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            st = f.stat()
            raw = f.read_text(encoding="utf-8", errors="replace")
            LIMIT = 400_000
            self._send(200, json.dumps({
                "path": rel, "sizeKB": round(st.st_size / 1024, 1),
                "mtime": int(st.st_mtime),
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "text": raw[:LIMIT], "truncated": len(raw) > LIMIT,
            }, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")
        elif path == "/api/proj/file":
            q4 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            raw = q4.get("p", [""])[0]
            f = (PROJECT / raw).resolve()
            root = PROJECT.resolve()
            IMG = (".png", ".jpg", ".jpeg", ".webp")
            VID = (".mp4", ".mov", ".webm", ".m4v")
            if not _inside(root, f) or not f.is_file() or f.suffix.lower() not in IMG + VID:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            ext = f.suffix.lower()
            ctype = ("video/mp4" if ext in (".mp4", ".m4v") else
                     "video/quicktime" if ext == ".mov" else
                     "video/webm" if ext == ".webm" else
                     "image/png" if ext == ".png" else "image/jpeg")
            data = f.read_bytes()
            rng = self.headers.get("Range")
            if rng and ext in VID:                      # 支持拖动进度
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    start = int(m.group(1) or 0)
                    end = int(m.group(2)) if m.group(2) else len(data) - 1
                    end = min(end, len(data) - 1)
                    chunk = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/backend":
            self._send(200, json.dumps(backend_state(PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/backend/log":
            q7 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            card = int(q7.get("card", ["0"])[0])
            f = Path(comfyctl.COMFY_LOG) / f"comfy-card{card}.log"
            tail = ""
            if f.exists():
                try:
                    tail = "\n".join(f.read_text(encoding="utf-8", errors="ignore").splitlines()[-60:])
                except Exception as e:
                    tail = f"读取失败：{e}"
            self._send(200, json.dumps({"card": card, "tail": tail}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/cloud/status":
            self._send(200, json.dumps(cloudimg.status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/units":
            q5 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sc = q5.get("scene", ["N001"])[0]
            units = vjobs.parse_units(PROJECT, sc)
            for u in units:
                cf = u.get("chainFrom")
                u["chainReady"] = bool(cf and vjobs.latest_result(PROJECT, sc, cf))
            src = units[0]["source"] if units else None
            rel = f"{vjobs.VP_DIR}/{sc}/{src}" if src else None
            st = _prompt_confirm(rel) if rel else {"local": "none"}
            self._send(200, json.dumps({"scene": sc, "units": units, "promptFile": rel,
                                        "promptConfirm": st},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/storyboards":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sc = qs.get("scene", ["N001"])[0]
            if not re.fullmatch(r"N\d{3}", sc):
                self._send(400, b'{"error":"scene must be Nxxx"}', "application/json; charset=utf-8")
                return
            d = PROJECT / "09-镜头与视频提示词/镜头规划" / sc / "分镜图"
            items = []
            if d.is_dir():
                for f in sorted(d.glob("*.png")):
                    st = f.stat()
                    items.append({"file": str(f.relative_to(PROJECT)), "name": f.name,
                                  "sizeMB": round(st.st_size / 2 ** 20, 2),
                                  "time": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))})
            self._send(200, json.dumps({"scene": sc, "items": items}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/workflows":
            self._send(200, json.dumps({"dir": str(WF_DIR), "items": scan_workflows()},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/recipe":
            cur = vjobs.current_recipe()
            allr = [{"name": k, **v} for k, v in vjobs.RECIPES.items()]
            if cur["name"] not in vjobs.RECIPES:      # 导入的自定义工作流也要出现在卡片里
                allr.insert(0, {"name": cur["name"], "label": cur.get("label") or cur["name"],
                                "steps": cur.get("steps"), "lora": cur.get("lora"),
                                "source": cur.get("source") or "comfyui"})
            self._send(200, json.dumps({"current": cur, "all": allr},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/versions":
            qv = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sc = qv.get("scene", ["N001"])[0]
            d = PROJECT / vjobs.VP_DIR / sc
            pins = vjobs.load_pins()
            self._send(200, json.dumps({"scene": sc, "pinned": pins.get(sc),
                                        "versions": vjobs.list_versions(d) if d.is_dir() else []},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/history":
            qh = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            inc = qh.get("deleted", ["0"])[0] in ("1", "true", "yes")
            self._send(200, json.dumps(gen_history(inc), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/jobs":
            self._send(200, json.dumps({"jobs": vjobs.list_jobs(project=PROJECT)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/active":
            active = [j for j in vjobs.list_jobs(project=PROJECT)
                      if j.get("status") in ("queued", "dispatched", "running", "starting")]
            self._send(200, json.dumps({"jobs": active}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/gen/results":
            q6 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sc = q6.get("scene", ["N001"])[0]
            root = PROJECT / vjobs.RESULT_DIR / sc
            jobs_by_file = {j.get("file"): j for j in vjobs.list_jobs(project=PROJECT) if j.get("file")}
            def newest_of(unit):
                d = root / unit
                if not d.is_dir():
                    return 0
                fs = sorted(d.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
                return int(fs[-1].stat().st_mtime) if fs else 0
            out = []
            if root.is_dir():
                import re as _re
                for f in sorted(root.rglob("*.mp4"), key=lambda x: x.stat().st_mtime):
                    m = _re.match(r"(U\d{3})", f.stem)
                    mt = f.stat().st_mtime
                    job = jobs_by_file.get(str(f.relative_to(PROJECT))) or {}
                    chain_from = job.get("prevUnit")
                    chain_at = 0
                    if job.get("firstFrame"):
                        pf = PROJECT / job["firstFrame"]
                        chain_at = int(pf.stat().st_mtime) if pf.exists() else 0
                    prev_latest = newest_of(chain_from) if chain_from else 0
                    out.append({"file": str(f.relative_to(PROJECT)),
                                "unit": m.group(1) if m else f.parent.name,
                                "group": f.parent.name, "name": f.name,
                                "chainFrom": chain_from,
                                "chainStale": bool(chain_from and prev_latest > chain_at),
                                "time": time.strftime("%m-%d %H:%M", time.localtime(mt)),
                                "kb": max(1, f.stat().st_size // 1024),
                                "mtime": mt})
            self._send(200, json.dumps({"scene": sc, "results": out}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/firstframe/shots":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            scene = q.get("scene", ["N001"])[0]
            if not re.fullmatch(r"N\d{3}", scene):
                self._send(400, b'{"error":"scene must be Nxxx"}', "application/json; charset=utf-8")
                return
            self._send(200, json.dumps(firstframe.shots_payload(PROJECT, scene), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/scenes":
            d = scenes.build(PROJECT)
            for sc in d.get("scenes", []):
                sc["reviewPath"] = scenes.SCREENPLAY
                sc["approval"] = approvals.state(PROJECT, "scene", f"scene:{sc['id']}")
            self._send(200, json.dumps(d, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/scenes/shots":
            q3 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sid = q3.get("scene", ["N001"])[0]
            if not re.fullmatch(r"N\d{3}", sid):
                self._send(400, b'{"error":"scene must be Nxxx"}', "application/json; charset=utf-8")
                return
            shots = scenes.shot_details(PROJECT, sid)
            for x in shots:
                x["approval"] = approvals.state(PROJECT, "shot", f"{sid}/{x['id']}")
            self._send(200, json.dumps({"scene": sid, "shots": shots},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/assets":
            d = assetsdb.build(PROJECT)
            for a in d.get("assets", []):
                a["reviewPath"] = d.get("doc")
                a["approval"] = approvals.state(PROJECT, "asset", f"asset:{a['id']}")
            self._send(200, json.dumps(d, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/stages":
            q2 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            proj = Path(q2.get("project", [str(PROJECT)])[0]).expanduser()
            if q2.get("sig"):
                self._send(200, json.dumps({"sig": artifacts.signature(proj)},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            payload = artifacts.build(proj)
            def _local(path):
                st = approvals.state(proj, "file", path)
                rec = st.get("record")
                if not rec:
                    return {"local": "none"}
                if st.get("stale"):
                    return {"local": "stale", "at": rec.get("at"), "record": rec,
                            "reason": st.get("reason")}
                if rec.get("verdict") == "pass":
                    return {"local": "confirmed", "at": rec.get("at"), "record": rec}
                return {"local": "rejected", "at": rec.get("at"), "record": rec}
            for a in payload["artifacts"]:
                a["local"] = _local(a["path"])
            for u in payload.get("unregistered", []):
                u["local"] = _local(u["path"])
            payload["summary"]["localConfirmed"] = sum(
                1 for a in payload["artifacts"] if a["local"].get("local") == "confirmed")
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path in ("/api/gpus", "/api/sched/state"):
            snap = sched.snapshot()
            snap["default"] = gpus.default_selection(gpus.probe())
            self._send(200, json.dumps(snap, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/prompts":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            ep = q.get("ep", [current_episode()])[0]
            f = PROJECT / "剧集" / ep / "图片提示词.md"
            items = imagegen.parse_prompts(f)
            for it in items:
                it["hasRef"] = bool(it.get("ref")) and (PROJECT / it["ref"]).exists()
            body = json.dumps({"episode": ep, "exists": f.exists(), "items": items},
                              ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/comfy/progress":
            snap = sched.snapshot()
            jobs = imgjobs.list_jobs(project=PROJECT)
            batch = jobs[-1]["batch"] if jobs else None
            same = [j for j in jobs if j["batch"] == batch]
            snap.update({"batch": batch, "total": len(same),
                         "done": sum(1 for j in same if j["status"] == "done"),
                         "failed": sum(1 for j in same if j["status"] == "failed"),
                         "cancelled": sum(1 for j in same if j["status"] == "cancelled"),
                         "running": sum(1 for j in same if j["status"] == "running"),
                         "queued": sum(1 for j in same if j["status"] == "queued")})
            self._send(200, json.dumps(snap, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/files":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            ep = q.get("ep", [current_episode()])[0]
            root = PROJECT / "剧集" / ep / "制作成果" / "图片候选"
            out = []
            if root.is_dir():
                for sub in sorted(root.iterdir()):
                    if not sub.is_dir():
                        continue
                    for f in sorted(sub.glob("*.png")) + sorted(sub.glob("*.jpg")):
                        out.append({"asset": sub.name,
                                    "file": str(f.relative_to(PROJECT)),
                                    "kb": max(1, f.stat().st_size // 1024)})
            body = json.dumps({"files": out}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/img/recipe":
            cur = imagegen.current_recipe()
            allr = [{"name": k, **v} for k, v in imagegen.RECIPES.items()]
            self._send(200, json.dumps({"current": cur, "all": allr}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/history":
            # 只列导演台自己提交的图片任务结果；07 里历史平台候选不混进来
            items = []
            for j in imgjobs.list_jobs(project=PROJECT):
                if j.get("status") != "done" or not j.get("file"):
                    continue
                f = str(j["file"])
                if not (PROJECT / f).is_file():
                    continue
                items.append({"file": f, "name": f.split("/")[-1],
                              "label": j.get("asset") or j.get("dir") or "图片",
                              "at": j.get("startedAt") or j.get("queuedAt") or 0,
                              "seconds": j.get("seconds"), "source": "workbench",
                              "refs": len(j.get("refs") or []), "exists": True})
            items.sort(key=lambda x: x.get("at") or 0, reverse=True)
            self._send(200, json.dumps({"items": items[:120]}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/refs":
            roots = [PROJECT / "07-图像资产", PROJECT / "制作运行/引用图",
                     store.state_dir(PROJECT) / "首帧候选",
                     store.state_dir(PROJECT) / "引用图"]
            seen, items = set(), []
            for root in roots:
                if not root.is_dir():
                    continue
                for f in root.rglob("*"):
                    if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                        continue
                    try:
                        rel = str(f.relative_to(PROJECT))
                    except ValueError:
                        rel = str(f)
                    if rel in seen:
                        continue
                    seen.add(rel)
                    items.append({"file": rel, "name": f.name,
                                  "kind": "首帧" if "首帧候选" in str(f) else
                                          ("引用图" if "引用图" in str(f) else "资产"),
                                  "sizeKB": round(f.stat().st_size / 1024),
                                  "mtime": int(f.stat().st_mtime)})
            items.sort(key=lambda x: x["mtime"], reverse=True)
            self._send(200, json.dumps({"items": items[:300]}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/candidates":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            asset = q.get("asset", [""])[0]
            root = PROJECT / "07-图像资产/候选图片"
            items = []
            if root.is_dir():
                for f in root.rglob("*"):
                    if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                        continue
                    if asset and asset not in f.parts:
                        continue
                    try:
                        rel = str(f.relative_to(PROJECT))
                    except ValueError:
                        continue
                    items.append({"file": rel, "name": f.name, "asset": f.parent.name,
                                  "sizeKB": round(f.stat().st_size / 1024),
                                  "mtime": int(f.stat().st_mtime)})
            items.sort(key=lambda x: x["mtime"], reverse=True)
            self._send(200, json.dumps({"items": items}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/img/adopted":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            ep = q.get("ep", [current_episode()])[0]
            f = store.state_dir(PROJECT) / "图片采用.jsonl"
            rows = {}
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("ep") == ep:
                        if r.get("file"):
                            rows[r["asset"]] = r
                        else:
                            rows.pop(r["asset"], None)
            body = json.dumps({"adopted": rows}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/img/jobs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            body = json.dumps({"jobs": imgjobs.list_jobs(q.get("batch", [None])[0], project=PROJECT)},
                              ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/img/file":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            raw = q.get("p", [""])[0]
            cands = [(store.state_dir(PROJECT) / raw).resolve(), (PROJECT / raw).resolve()]
            f = next((c for c in cands
                      if (_inside(store.state_dir(PROJECT), c)
                          or _inside(PROJECT / "剧集", c)) and c.is_file()), None)
            if not f:
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            self._send(200, f.read_bytes(), "image/png")
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
        elif path == "/api/reviews/export":
            md = review_export().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="review-notes.md"')
            self.send_header("Content-Length", str(len(md)))
            self.end_headers()
            self.wfile.write(md)
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
        elif path == "/assetimg":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            f = resolve_asset(q.get("p", [""])[0])
            if not f:
                self._send(404, b'{"error":"no asset"}', "application/json; charset=utf-8")
                return
            self._send(200, f.read_bytes(), "image/png" if f.suffix.lower() == ".png" else "image/jpeg")
        elif path == "/assetthumb":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            f = resolve_asset(q.get("p", [""])[0])
            th = thumb_of(f) if f else None
            if not th:
                self._send(404, b'{"error":"no asset"}', "application/json; charset=utf-8")
                return
            self._send(200, th.read_bytes(), "image/jpeg")
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

    def _json_body(self):
        if getattr(self, "_cached_body", None) is None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                self._cached_body = json.loads(raw or b"{}")
            except Exception:
                self._cached_body = {}
        return self._cached_body

    def do_POST(self):
        self.query_path = self.path
        path = self.path.split("?")[0]
        if path == "/api/preflight":
            try:
                body = self._json_body()
                out = preflight(body)
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/asset/ignore":
            try:
                body = self._json_body()
                out = assetsdb.set_ignored(PROJECT, str(body.get("asset") or ""),
                                           bool(body.get("ignored")))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/recipe":
            try:
                body = self._json_body()
                cur = imagegen.set_recipe(str(body.get("name") or ""))
                self._send(200, json.dumps({"ok": True, "current": cur}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/upload":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 25 * 1024 * 1024:
                    raise ValueError("文件为空或超过 25MB")
                data = self.rfile.read(n)
                sig = data[:12]
                ext = ""
                if sig.startswith(b"\x89PNG"):
                    ext = ".png"
                elif sig.startswith(b"\xff\xd8"):
                    ext = ".jpg"
                elif sig.startswith(b"RIFF") and sig[8:12] == b"WEBP":
                    ext = ".webp"
                elif sig.startswith(b"GIF8"):
                    ext = ".gif"
                if not ext:
                    raise ValueError("不是可识别的图片")
                name = str(self.headers.get("X-Filename") or "ref")
                name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:60]
                d = PROJECT / "制作运行/引用图"
                try:
                    d.mkdir(parents=True, exist_ok=True)
                except OSError:
                    d = store.state_dir(PROJECT) / "引用图"
                    d.mkdir(parents=True, exist_ok=True)
                out = d / (uuid.uuid4().hex[:10] + "-" + name.rsplit(".", 1)[0] + ext)
                out.write_bytes(data)
                try:
                    rel = str(out.relative_to(PROJECT))
                except ValueError:
                    rel = str(out)
                self._send(200, json.dumps({"ok": True, "file": rel, "name": out.name,
                                            "sizeKB": round(len(data) / 1024)},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/generate":
            try:
                body = self._json_body()
                asset_id = str(body.get("asset") or "")
                assets = assetsdb.build(PROJECT).get("assets") or []
                asset = next((a for a in assets if a.get("id") == asset_id), None)
                if not asset:
                    raise ValueError("未知资产")
                adoc = assetsdb.latest_asset_doc(PROJECT)
                if adoc:
                    st = _artifact_status(PROJECT).get(str(adoc.relative_to(PROJECT)))
                    if st in STALE_ART:
                        raise ValueError(f"资产文档已过期，禁止出图：{adoc.name}（{st}）")
                mode = str(body.get("mode") or "t2i")
                prompt = str(body.get("prompt") or asset.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("缺提示词")
                ref = body.get("ref") or None
                if mode == "edit":
                    if not ref:
                        raise ValueError("改图必须选择一张底图")
                    f = (PROJECT / ref).resolve()
                    if not _inside(PROJECT, f) or not f.is_file():
                        raise ValueError("底图不在项目内或不存在")
                rec = imagegen.current_recipe()
                width = int(body.get("width") or rec.get("width") or imagegen.DEFAULT_W)
                height = int(body.get("height") or rec.get("height") or imagegen.DEFAULT_H)
                steps = int(body.get("steps") or rec.get("steps") or imagegen.DEFAULT_STEPS)
                n = max(1, min(4, int(body.get("n") or 1)))
                it = {"id": asset_id, "name": asset.get("name") or asset_id,
                      "prompt": prompt, "ref": ref, "n": n,
                      "width": width, "height": height, "steps": steps,
                      "seed": body.get("seed") if str(body.get("seed") or "").strip().isdigit() else None}
                pf = imagegen.preflight([it], PROJECT)
                if pf.get("blockers"):
                    raise ValueError("预检阻断：" + "；".join(pf["blockers"]))
                out = imgjobs.submit_batch(PROJECT, current_episode(), [it],
                                           out_root="07-图像资产/候选图片")
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/cancel":
            try:
                body = self._json_body()
                if body.get("batch"):
                    out = imgjobs.cancel_batch(body["batch"])
                else:
                    out = imgjobs.cancel(list(body.get("ids") or []))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/cards/confirm":
            try:
                out = sched.set_cards(self._json_body().get("enabled") or [])
                out["ok"] = True
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/firstframe/generate":
            try:
                body = self._json_body()
                scene = str(body.get("scene") or "N001")
                shot = str(body.get("shot") or "")
                prompt = str(body.get("prompt") or "").strip()
                if not re.fullmatch(r"N\d{3}", scene) or not re.fullmatch(r"SH\d{3}", shot):
                    raise ValueError("scene/shot 非法")
                if not prompt:
                    raise ValueError("缺首帧提示词")
                refs = body.get("refs")
                if refs is None:
                    refs = [body.get("ref")] if body.get("ref") else []
                if not isinstance(refs, list):
                    raise ValueError("refs 必须是数组")
                refs = [str(x) for x in refs if str(x).strip()][:16]
                for ref in refs:
                    f = (PROJECT / ref).resolve()
                    if not _inside(PROJECT, f) or not f.is_file():
                        raise ValueError(f"参考图不在项目内：{ref}")
                prompt = firstframe.with_ref_instructions(PROJECT, prompt, refs)
                plan = PROJECT / f"09-镜头与视频提示词/镜头规划/{scene}/shot-plan-v001.md"
                if not plan.exists():
                    plans = sorted((PROJECT / f"09-镜头与视频提示词/镜头规划/{scene}").glob("shot-plan-v*.md"))
                    plan = plans[-1] if plans else None
                if plan:
                    st = _artifact_status(PROJECT).get(str(plan.relative_to(PROJECT)))
                    if st in STALE_ART:
                        raise ValueError(f"镜头表已过期，禁止出首帧：{plan.name}（{st}）")
                width = int(body.get("width") or 1536)
                height = int(body.get("height") or 864)
                steps = int(body.get("steps") or 6)
                n = max(1, min(2, int(body.get("n") or 1)))
                it = {"id": shot, "name": shot, "prompt": prompt, "refs": refs,
                      "ref": (refs[0] if refs else None), "n": n,
                      "width": width, "height": height, "steps": steps,
                      "seed": body.get("seed") if str(body.get("seed") or "").strip().isdigit() else None}
                pf = imagegen.preflight([it], PROJECT)
                if pf.get("blockers"):
                    raise ValueError("预检阻断：" + "；".join(pf["blockers"]))
                out = imgjobs.submit_batch(PROJECT, current_episode(), [it],
                                           out_root=f"制作运行/首帧候选/{scene}")
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/firstframe/adopt":
            try:
                body = self._json_body()
                rec = firstframe.adopt(PROJECT, str(body.get("scene") or ""),
                                       str(body.get("shot") or ""), str(body.get("file") or ""))
                self._send(200, json.dumps({"ok": True, "record": rec}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/project/init":
            try:
                body = self._json_body()
                active = [j for j in vjobs.list_jobs() if j.get("status") in ("queued", "dispatched", "running", "waiting")]
                active += [j for j in imgjobs.list_jobs() if j.get("status") in ("queued", "running")]
                if active:
                    raise ValueError("当前还有生成任务在跑，暂停初始化/切换；等队列清空后再试")
                made = projects.create(str(body.get("name") or ""))
                _switch_project(made["path"])
                self._send(200, json.dumps({"ok": True, "project": made, "projects": projects.discover()},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(409 if "任务" in str(e) else 400,
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/project/switch":
            try:
                body = self._json_body()
                target = str(body.get("path") or body.get("id") or "")
                active = [j for j in vjobs.list_jobs() if j.get("status") in ("queued", "dispatched", "running", "waiting")]
                active += [j for j in imgjobs.list_jobs() if j.get("status") in ("queued", "running")]
                if active and str(PROJECT) != str(Path(target).expanduser().resolve()):
                    raise ValueError("当前还有生成任务在跑，暂停切换；等队列清空后再切")
                p = _switch_project(target)
                _cache["by"] = {}; _cache["sig"] = None
                self._send(200, json.dumps({"ok": True, "project": str(PROJECT),
                                            "projects": projects.discover()},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(409 if "任务" in str(e) else 400,
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/agent/config":
            try:
                body = self._json_body()
                out = agentrunner.save_config(PROJECT, str(body.get("agent") or ""),
                                              str(body.get("model") or ""),
                                              str(body.get("provider") or ""))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/agent/dispatch":
            try:
                body = self._json_body()
                out = agentrunner.dispatch(PROJECT, str(body.get("step") or ""),
                                           str(body.get("scene") or "N001"))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/workflow/batch":
            try:
                body = self._json_body()
                action = str(body.get("action") or "")
                scene = str(body.get("scene") or "N001")
                if action not in ("register", "confirm"):
                    raise ValueError("action 必须是 register/confirm")
                data = flow.steps_payload(PROJECT, scene)
                results = []
                for st in data.get("steps") or []:
                    if st.get("skipped"):
                        continue
                    if action == "register":
                        files = st.get("newFiles") or []
                        if not files:
                            continue
                        try:
                            r = flow.register(PROJECT, st["key"], scene, path=files[0],
                                              outcome="passed")
                            results.append({"step": st["key"], "ok": True, "path": r.get("path")})
                        except Exception as e:
                            results.append({"step": st["key"], "ok": False, "error": str(e)})
                    else:
                        drafts = [v for v in (st.get("versions") or [])
                                  if v.get("statusText") == "草稿待确认"]
                        if not drafts:
                            continue
                        try:
                            r = flow.confirm(PROJECT, st["key"], scene, path=drafts[0]["path"])
                            results.append({"step": st["key"], "ok": True, "path": r.get("path")})
                        except Exception as e:
                            results.append({"step": st["key"], "ok": False, "error": str(e)})
                self._send(200, json.dumps({"ok": True, "action": action, "results": results},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/workflow/return":
            try:
                body = self._json_body()
                out = flow.return_step(PROJECT, str(body.get("step") or ""),
                                       str(body.get("note") or ""), str(body.get("scene") or "N001"))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/workflow/skip":
            try:
                body = self._json_body()
                out = flow.skip_step(PROJECT, str(body.get("step") or ""), str(body.get("note") or ""))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/workflow/register":
            try:
                body = self._json_body()
                out = flow.register(PROJECT, str(body.get("step") or ""),
                                    str(body.get("scene") or "N001"),
                                    body.get("path"), str(body.get("outcome") or "passed"))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/workflow/confirm":
            try:
                body = self._json_body()
                out = flow.confirm(PROJECT, str(body.get("step") or ""),
                                   str(body.get("scene") or "N001"), body.get("path"))
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/approval":
            try:
                body = self._json_body()
                kind = str(body.get("kind") or "")
                target = str(body.get("target") or "")
                verdict = str(body.get("verdict") or "pass")
                rel = body.get("path") or None
                if not kind or not target:
                    raise ValueError("kind/target 必填")
                if rel:
                    f = (PROJECT / rel).resolve()
                    if not _inside(PROJECT, f) or not f.is_file():
                        raise ValueError("path 不在项目内或文件不存在")
                notes = body.get("notes")
                if isinstance(notes, str):
                    notes = [{"text": notes}]
                rec = approvals.append(
                    PROJECT, kind, target, verdict,
                    path=rel, version=body.get("version") or (_version_of(rel) if rel else None),
                    scope=_scope_obj(body.get("scope")), notes=notes, by=body.get("by") or "director",
                )
                st = approvals.state(PROJECT, kind, target)
                self._send(200, json.dumps({"ok": True, "record": rec, "state": st},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/artifact/confirm":
            try:
                body = self._json_body()
                rel = str(body.get("path") or "")
                verdict = str(body.get("verdict") or "pass")
                if not rel:
                    raise ValueError("path 必填")
                if verdict not in ("pass", "revise", "hold"):
                    raise ValueError("verdict 只能是 pass / revise / hold")
                f = (PROJECT / rel).resolve()
                if not _inside(PROJECT, f) or not f.is_file():
                    raise ValueError("path 不在项目内或文件不存在")
                notes = [{"text": body.get("note")}] if body.get("note") else []
                # 阶段成果的“确认/退回”必须同时更新 artifact-state，不能只记本地审批
                flow_info = {}
                flow_step, flow_scene = flow.artifact_step(PROJECT, rel)
                if verdict == "pass":
                    try:
                        fr = flow.confirm_path(PROJECT, rel)
                        flow_info = fr
                    except Exception as e:
                        msg = str(e)
                        if "没有对应待确认草稿" in msg or "未登记" in msg:
                            flow_info = {"warning": "登记表没有对应待确认草稿；只记本地审批"}
                        else:
                            raise
                elif verdict == "revise" and flow_step:
                    fr = flow.return_step(PROJECT, flow_step["key"],
                                          body.get("note") or "", flow_scene or "N001")
                    flow_info = {"step": flow_step["key"], **fr}
                rec = approvals.append(
                    PROJECT, "file", rel, verdict,
                    path=rel, version=body.get("version") or _version_of(rel),
                    scope=_scope_obj(body.get("scope")), notes=notes,
                )
                # 兼容旧缓存：只做 best-effort，失败不影响审批账本
                try:
                    confirms.record(PROJECT, rel,
                                    "confirm" if verdict == "pass" else "reject",
                                    (body.get("note") or ""))
                except Exception:
                    pass
                local = approvals.state(PROJECT, "file", rel)
                self._send(200, json.dumps({"ok": True, "record": rec, "local": local,
                                            "flow": flow_info},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/backend/action":
            try:
                body = self._json_body()
                act = body.get("action")
                card = int(body.get("card", -1))
                out = {"action": act, "card": card}
                if act == "start":
                    out["result"] = comfyctl.start(card, wait=120)
                    sched.set_cards(list(set(sched.snapshot().get("enabled") or []) | {card}))
                elif act == "stop":
                    out["result"] = comfyctl.stop(card)
                elif act == "restart":
                    comfyctl.stop(card)
                    time.sleep(3)
                    out["result"] = comfyctl.start(card, wait=120)
                elif act == "interrupt":
                    port = comfyctl.BASE_PORT + card
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/interrupt", data=b"{}",
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10).read()
                    out["result"] = {"ok": True}
                elif act == "clear":
                    port = comfyctl.BASE_PORT + card
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/queue",
                                                 data=json.dumps({"clear": True}).encode(),
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10).read()
                    out["result"] = {"ok": True}
                else:
                    raise ValueError("未知操作")
                self._send(200, json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/translate":
            try:
                body = self._json_body()
                scene = body.get("scene") or "N001"
                want = body.get("units") or []
                units = vjobs.parse_units(PROJECT, scene)
                if not units:
                    raise ValueError("该场没有提示词")
                picked = [u for u in units if not want or u["id"] in want]
                st = translate.available()
                if not st["ok"]:
                    raise ValueError(st["reason"])
                done, failed = [], []
                for u in picked:
                    try:
                        zh = translate.translate(u["prompt"])
                        vjobs.write_zh(scene, u["id"], u["prompt"], zh)
                        done.append({"unit": u["id"], "chars": len(zh)})
                    except Exception as e:
                        failed.append({"unit": u["id"], "error": str(e)[:120]})
                self._send(200, json.dumps({"ok": True, "model": translate.MODEL,
                                            "translated": done, "failed": failed},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/recipe":
            try:
                body = self._json_body()
                if body.get("file"):
                    wf = next((w for w in scan_workflows() if w["file"] == body["file"]), None)
                    if not wf:
                        raise ValueError("找不到该工作流")
                    if not wf["ok"]:
                        raise ValueError("工作台无法复刻：" + "；".join(wf["blockers"]))
                    spec = {"name": wf["file"].replace(".json", ""),
                            "label": wf["file"].replace(".json", ""),
                            "steps": wf["steps"] or 15, "lora": wf["lora"],
                            "res": [wf["width"], wf["height"]] if wf["width"] else None,
                            "refSize": wf["refSize"] or "match", "source": "comfyui"}
                    if wf.get("progressive"):
                        base = vjobs.RECIPES["progressive"]
                        spec.update({"sigmas": wf.get("sigmas") or base["sigmas"],
                                     "sampler": wf.get("sampler") or base["sampler"],
                                     "attention": base["attention"]})
                        spec["steps"] = max(1, len(str(spec["sigmas"]).split(",")) - 1)
                    cur = vjobs.save_recipe(spec)
                    self._send(200, json.dumps({"ok": True, "current": cur}, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                cur = vjobs.set_recipe(body.get("name") or "")
                self._send(200, json.dumps({"ok": True, "current": cur}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/version":
            try:
                body = self._json_body()
                sc = body.get("scene") or "N001"
                name = body.get("file") or None
                if name:
                    f = PROJECT / vjobs.VP_DIR / sc / name
                    if not f.exists():
                        raise ValueError("版本文件不存在")
                pins = vjobs.save_pin(sc, name)
                self._send(200, json.dumps({"ok": True, "pinned": pins.get(sc)},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/cancel":
            try:
                body = self._json_body()
                ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
                if not ids:
                    raise ValueError("没有指定任务")
                res = vjobs.cancel_jobs(ids)
                self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/delete":
            try:
                body = self._json_body()
                rel = str(body.get("file") or "")
                f = (PROJECT / rel).resolve()
                root = (PROJECT / vjobs.RESULT_DIR).resolve()
                if not _inside(root, f) or f.suffix.lower() != ".mp4":
                    raise ValueError("只能删除 09-…/生成结果 下的视频")
                if not f.exists():
                    raise ValueError("文件不存在")
                f.unlink()
                removed = vjobs.mark_deleted(rel)
                self._send(200, json.dumps({"ok": True, "file": rel, "records": removed},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/gen/submit":
            try:
                body = self._json_body()
                scene = body.get("scene") or "N001"
                want = body.get("units") or []
                ops = vjobs.parse_units(PROJECT, scene)
                by = {u["id"]: u for u in ops}
                units = []
                order = [u["id"] for u in ops]
                batch_prev = {}          # 同批里每个单元最后一条任务的 id
                seen_units = set()
                for x in want:
                    if isinstance(x, dict):
                        if x.get("id") in seen_units:
                            continue
                        seen_units.add(x.get("id"))
                        u = by.get(x.get("id"))
                        if u:
                            idx = order.index(u["id"])
                            declared = u.get("chainFrom")
                            prev = (declared if declared in order else None) or \
                                   (order[idx - 1] if idx > 0 else None)
                            want_chain = x.get("chain")
                            if want_chain is None:          # 未显式传值时按提示词声明
                                want_chain = bool(declared)
                            n = int(x.get("count") or 1)
                            deps = [batch_prev[prev]] if (want_chain and prev in batch_prev) else []
                            units.append({"unit": u, "count": n,
                                          "chain": bool(want_chain) and bool(prev),
                                          "prevUnit": prev, "afterIds": deps})
                    elif x in by:
                        units.append({"unit": by[x], "count": 1})
                if not units:
                    raise ValueError("没有可提交的单元")
                asked = {x["unit"]["id"] for x in units}
                missing_src = []
                for x in units:
                    if not x.get("chain"):
                        continue
                    prev = x.get("prevUnit")
                    if prev and prev not in asked and not vjobs.latest_result(PROJECT, scene, prev):
                        missing_src.append(f"{x['unit']['id']} 需要 {prev} 的成片")
                if missing_src:
                    raise ValueError("接续源尚未生成：" + "；".join(missing_src) +
                                     "。请把上一单元一起提交，或先生成它")
                src = units[0]["unit"]["source"]
                rel = f"{vjobs.VP_DIR}/{scene}/{src}"
                gate = _generation_gate(scene, units, rel)
                if not gate["ok"]:
                    raise ValueError("生成门禁未通过：" + "；".join(gate["missing"]))
                seed = body.get("seed")
                seed = int(seed) if str(seed).strip().isdigit() else None
                steps = body.get("steps")
                steps = int(steps) if str(steps).isdigit() else vjobs.current_recipe()["steps"]
                size = str(body.get("size") or "1344x768")
                try:
                    w, h = [int(x) for x in size.lower().split("x")]
                except Exception:
                    w, h = 1344, 768
                if steps <= 0:
                    raise ValueError("steps 必须 > 0")
                if w % 32 or h % 32:
                    raise ValueError(f"宽高必须是 32 的倍数：{w}x{h}")
                batch_id = "VID-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
                frec = approvals.file_ok(PROJECT, rel) or {}
                unit_ids = [x["unit"]["id"] for x in units]
                refs, seen_refs = [], set()
                for x in units:
                    for r in x["unit"].get("refs") or []:
                        rp = r.get("file")
                        if not rp or rp in seen_refs:
                            continue
                        seen_refs.add(rp)
                        fp = PROJECT / rp
                        sha = None
                        if fp.is_file():
                            hasher = hashlib.sha256()      # 勿用 h：h 已是画幅高度
                            with open(fp, "rb") as fh:
                                for chunk in iter(lambda: fh.read(1 << 20), b""):
                                    hasher.update(chunk)
                            sha = hasher.hexdigest()
                        refs.append({"path": rp, "sha256": sha, "exists": fp.is_file()})
                recipe = vjobs.current_recipe()
                batch_rec = {
                    "id": batch_id, "status": "pending", "scene": scene,
                    "units": unit_ids, "prompt": rel, "promptSha": frec.get("sha256"),
                    "approvalIds": gate["approvalIds"],
                    "params": {"steps": steps, "width": w, "height": h, "seed": seed},
                    "recipe": {k: recipe.get(k) for k in
                               ("name", "lora", "steps", "sigmas", "sampler", "attention", "refSize")},
                    "refs": refs, "jobs": [],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                bdir = store.state_dir(PROJECT) / "generation-batches"
                bdir.mkdir(parents=True, exist_ok=True)
                bpath = bdir / f"{batch_id}.json"

                def _write_batch(rec):
                    tmp = bpath.with_name(bpath.name + ".tmp")
                    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
                    tmp.replace(bpath)

                # 先落批次与授权，再入队；写失败时不会产生任何 GPU 任务
                _write_batch(batch_rec)
                try:
                    approvals.append(
                        PROJECT, "batch", batch_id, "pass",
                        scope={"scene": scene, "units": unit_ids},
                        notes=[{"text": f"{scene} 视频批次：{len(units)} 个单元 · {steps} 步 · {w}x{h}" +
                                       (f"，种子 {seed}" if seed is not None else "，种子随机")}],
                    )
                except Exception as e:
                    batch_rec.update({"status": "failed", "error": f"批次授权失败：{e}"})
                    _write_batch(batch_rec)
                    raise
                try:
                    res = vjobs.submit(PROJECT, scene, units, seed=seed, steps=steps,
                                       width=w, height=h, batch=batch_id)
                except Exception as e:
                    batch_rec.update({"status": "failed", "error": f"入队失败：{e}"})
                    _write_batch(batch_rec)
                    raise
                batch_rec.update({"status": "submitted", "jobs": res.get("jobs") or []})
                try:
                    _write_batch(batch_rec)
                except Exception as e:
                    # 任务已经开始跑，不能再把接口报成失败；记录警告，批次文件保持 pending
                    res["batchWarning"] = f"批次状态回写失败：{e}"
                self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/adopt":
            try:
                body = self._json_body()
                ep = body.get("ep") or current_episode()
                asset_id = str(body.get("asset") or "")
                rel = str(body.get("file") or "")
                if not asset_id or not rel:
                    raise ValueError("asset/file 必填")
                f = (PROJECT / rel).resolve()
                if not _inside(PROJECT, f) or not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                    raise ValueError("候选图不在项目内或格式不对")
                ad = assetsdb.adopt_image(PROJECT, asset_id, rel)
                reg = {}
                try:
                    reg = flow.register(PROJECT, "07", "N001", path=ad["assetDoc"])
                except Exception as e:
                    reg = {"ok": False, "error": str(e)}
                rec = {"ep": ep, "asset": asset_id, "file": rel,
                       "binding": ad["bindingFile"], "assetDoc": ad["assetDoc"],
                       "registration": reg,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                f = store.state_dir(PROJECT) / "图片采用.jsonl"
                with open(f, "a", encoding="utf-8") as out:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._send(200, json.dumps({"ok": True, "record": rec,
                                            "bindingFile": ad["bindingFile"],
                                            "assetDoc": ad["assetDoc"],
                                            "registration": reg},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/preflight":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            ep = q.get("ep", [current_episode()])[0]
            items = imagegen.parse_prompts(PROJECT / "剧集" / ep / "图片提示词.md")
            self._send(200, json.dumps(imagegen.preflight(items, PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/img/execute":
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
                ep = q.get("ep", [current_episode()])[0]
                body = self._json_body()
                n = int(body.get("n") or 4)
                allp = imagegen.parse_prompts(PROJECT / "剧集" / ep / "图片提示词.md")
                by = {x["id"]: x for x in allp}
                items = []
                for w in (body.get("items") or []):
                    base = by.get(w.get("id") if isinstance(w, dict) else w)
                    if not base:
                        continue
                    x = dict(base)
                    x["n"] = int((w or {}).get("n") or n) if isinstance(w, dict) else n
                    items.append(x)
                pf = imagegen.preflight(items, PROJECT)
                if pf["blockers"]:
                    self._send(400, json.dumps({"error": "预检未通过", "detail": pf["blockers"]},
                                               ensure_ascii=False).encode("utf-8"))
                    return
                res = imgjobs.submit_batch(PROJECT, ep, items)
                store.append(PROJECT, 3, {"type": "batch", "id": res["batch"]}, "pass",
                             notes=[{"text": f"{ep} 图片批次：{len(items)} 项 × {n} 张"}])
                self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/preflight":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            ep = q.get("ep", [current_episode()])[0]
            items = imagegen.parse_prompts(PROJECT / "剧集" / ep / "图片提示词.md")
            self._send(200, json.dumps(imagegen.preflight(items, PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/img/execute":
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
                ep = q.get("ep", [current_episode()])[0]
                body = self._json_body()
                n = int(body.get("n") or 4)
                want = body.get("items") or []
                cards = [int(c) for c in (body.get("cards") or [])]
                allp = imagegen.parse_prompts(PROJECT / "剧集" / ep / "图片提示词.md")
                by = {p["id"]: p for p in allp}
                items = []
                for w in want:
                    base = by.get(w.get("id") if isinstance(w, dict) else w)
                    if not base:
                        continue
                    x = dict(base)
                    x["n"] = int((w or {}).get("n") or n) if isinstance(w, dict) else n
                    items.append(x)
                pf = imagegen.preflight(items, PROJECT)
                probes = gpus.probe()
                ports = {c["index"]: c["port"] for c in probes}
                if pf["blockers"]:
                    self._send(400, json.dumps({"error": "预检未通过", "detail": pf["blockers"]},
                                               ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                use = [c for c in cards if c in ports]
                if not use:
                    self._send(400, json.dumps({"error": "没有可用显卡"}, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                _cards_enabled().update(use)          # 本批用到的卡纳入启用集合
                missing = [c for c in use if not comfyctl._alive(ports[c])]
                if missing:
                    threading.Thread(target=comfyctl.ensure, args=(missing,), daemon=True).start()
                res = imgjobs.submit_batch(PROJECT, ep, items, use, ports)
                store.append(PROJECT, 3, {"type": "batch", "id": res["batch"]}, "pass",
                             notes=[{"text": f"{ep} 图片批次：{len(items)} 项 × {n} 张，卡 {use}"}])
                self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/execute":
            self._send(410, json.dumps({"ok": False, "error": "旧 /api/execute 已停用；请用 /api/gen/submit（带确认门禁）"},
                                       ensure_ascii=False).encode("utf-8"),
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
                local = confirms.state_of(PROJECT, body["path"]) if body.get("path") else {}
                self._send(200, json.dumps({"ok": True, "record": rec, "local": local,
                                            "flow": flow_info},
                                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path != "/api/decision":
            self._send(404, b'{"ok":false,"error":"not found"}', "application/json; charset=utf-8")
            return
        try:
            body = self._json_body()
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


_bg_started = False


def _migrate_confirms():
    """把旧的 制作运行/确认状态.json 里仍有效的确认导入 approvals（只迁一次）。"""
    f = PROJECT / "制作运行" / "确认状态.json"
    if not f.exists():
        return 0
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    for rel, old in (data or {}).items():
        if not isinstance(old, dict) or old.get("status") != "confirmed":
            continue
        if approvals.state(PROJECT, "file", rel).get("record"):
            continue
        st = confirms.state_of(PROJECT, rel)
        if st.get("local") != "confirmed":
            continue
        try:
            approvals.append(PROJECT, "file", rel, "pass", path=rel,
                             version=_version_of(rel), notes=[{"text": "从 确认状态.json 迁移"}],
                             by="migrated")
            n += 1
        except Exception:
            pass
    if n:
        print(f"[migrate] 确认状态 → approvals：{n} 条", flush=True)
    return n


def start_background():
    """只在真正启动服务时恢复队列/起后台线程；import server 不再有副作用。"""
    global _bg_started
    if _bg_started or os.environ.get("SHOTCALL_NO_BACKGROUND"):
        return
    _bg_started = True
    # 调度器与采样器由各自模块的 start() 启动（幂等），import 时不启动
    if hasattr(sched, "start"):
        sched.start()
    if hasattr(comfywatch, "start"):
        comfywatch.start()
    try:
        _migrate_confirms()
    except Exception as e:
        print("[migrate] 出错：", e, flush=True)
    try:
        resumed = vjobs.resume(PROJECT)
        if resumed:
            print("[resume] 重新排队：", resumed, flush=True)
    except Exception as e:
        print("[resume] 出错：", e, flush=True)
    try:
        ri = imgjobs.resume(PROJECT)
        if ri:
            print("[resume] 图片重新排队：", ri, flush=True)
    except Exception as e:
        print("[resume] 图片出错：", e, flush=True)
    threading.Thread(target=_video_recovery_loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    payload()  # 预热
    start_background()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"导演工作台 → http://{a.host}:{a.port}")
    print(f"  项目 {PROJECT} · {EPISODE} · 仅监听本机")
    srv.serve_forever()


if __name__ == "__main__":
    main()
