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

import artifacts
import assetsdb
import dataset
import gpus
import imagegen
import imgjobs
import sched
import scenes
import planner
import preflight as pf
import runner
import store

PROJECT = Path(os.environ.get("WORKBENCH_PROJECT") or (Path.home() / "short_v/有谁共鸣2")).expanduser()
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


THUMB_CACHE = {}


def resolve_asset(p: str):
    root = (PROJECT / "资产").resolve()
    f = (PROJECT / p).resolve()
    if not str(f).startswith(str(root)) or not f.is_file():
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
        elif path == "/api/data":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            body = json.dumps(response(q.get("ep", [None])[0]), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/execute":
            self._send(200, json.dumps(exec_status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/workflow":
            self._send(200, json.dumps({"workflow": workflow_all()}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/proj/file":
            q4 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            raw = q4.get("p", [""])[0]
            f = (PROJECT / raw).resolve()
            root = PROJECT.resolve()
            if (not str(f).startswith(str(root)) or not f.is_file()
                    or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")):
                self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")
                return
            self._send(200, f.read_bytes(),
                       "image/png" if f.suffix.lower() == ".png" else "image/jpeg")
        elif path == "/api/scenes":
            self._send(200, json.dumps(scenes.build(PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/scenes/shots":
            q3 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            sid = q3.get("scene", ["N001"])[0]
            self._send(200, json.dumps({"scene": sid, "shots": scenes.shot_details(PROJECT, sid)},
                                       ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/assets":
            self._send(200, json.dumps(assetsdb.build(PROJECT), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/stages":
            q2 = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            proj = Path(q2.get("project", [str(PROJECT)])[0]).expanduser()
            self._send(200, json.dumps(artifacts.build(proj), ensure_ascii=False).encode("utf-8"),
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
            jobs = imgjobs.list_jobs()
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
            body = json.dumps({"jobs": imgjobs.list_jobs(q.get("batch", [None])[0])},
                              ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/img/file":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.query_path).query)
            raw = q.get("p", [""])[0]
            cands = [(store.state_dir(PROJECT) / raw).resolve(), (PROJECT / raw).resolve()]
            f = next((c for c in cands
                      if (str(c).startswith(str(store.state_dir(PROJECT).resolve()))
                          or str(c).startswith(str((PROJECT / "剧集").resolve()))) and c.is_file()), None)
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
        if path == "/api/artifact/confirm":
            try:
                body = self._json_body()
                verdict = body.get("verdict") or "pass"
                if verdict not in ("pass", "revise", "hold"):
                    raise ValueError("verdict 只能是 pass / revise / hold")
                rec = store.append(
                    PROJECT, 1,
                    {"type": "artifact", "id": body["path"]}, verdict,
                    notes=[{"text": f"{body.get('stage','')} {body.get('kind','')} {body.get('scope','')}".strip()}],
                    scope=body.get("scope"),
                    evidence=payload()["data"].get("files") or {},
                )
                self._send(200, json.dumps({"ok": True, "record": rec}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            return
        if path == "/api/img/adopt":
            try:
                body = self._json_body()
                ep = body.get("ep") or current_episode()
                rec = {"ep": ep, "asset": body["asset"], "file": body["file"],
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                f = store.state_dir(PROJECT) / "图片采用.jsonl"
                with open(f, "a", encoding="utf-8") as out:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._send(200, json.dumps({"ok": True, "record": rec}, ensure_ascii=False).encode("utf-8"),
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
            try:
                body = self._json_body()
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
