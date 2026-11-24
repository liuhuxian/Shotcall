# -*- coding: utf-8 -*-
"""分镜首帧：从 09a 镜头表 + 07 资产描述符拼首帧提示词，出图/采用。

候选落 制作运行/首帧候选/<场>/<镜>/；采用记录 制作运行/首帧采用.jsonl。
不修改 09 既有文件；视频提交按该记录取首帧。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import assetsdb
import scenes
import store

ASSET_ID = re.compile(r"\b([CLP]\d{2}(?:-[A-Z])?)\b")


def candidate_root(project: Path) -> Path:
    return store.state_dir(project) / "首帧候选"


def accepted_map(project: Path) -> dict:
    out = {}
    f = store.state_dir(project) / "首帧采用.jsonl"
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        out[(r.get("scene"), r.get("shot"))] = r
    return out


def first_for_unit(project: Path, scene: str, shots: str):
    ids = re.findall(r"SH\d{3}", shots or "")
    m = accepted_map(project)
    for sid in ids:
        r = m.get((scene, sid))
        if r and (project / r["file"]).is_file():
            return r["file"]
    return None


def _refs(project: Path, asset_ids: list) -> list:
    data = assetsdb.build(project)
    amap = {a["id"]: a for a in data.get("assets") or []}
    out = []
    for aid in asset_ids:
        a = amap.get(aid)
        b = (a or {}).get("binding") or {}
        img = b.get("image")
        if img and (project / img).is_file() and img not in [x["file"] for x in out]:
            out.append({"asset": aid, "file": img, "version": b.get("version") or ""})
    return out


def with_ref_instructions(project: Path, prompt: str, refs: list) -> str:
    """把参考图用法追加到提示词；UI/API 都走这里，避免只靠前端文本。"""
    prompt = prompt or ""
    refs = [str(x) for x in (refs or []) if str(x).strip()]
    if not refs:
        return prompt
    if "参考图用法" in prompt:
        return prompt
    amap = {}
    try:
        for a in assetsdb.build(project).get("assets") or []:
            img = (a.get("binding") or {}).get("image")
            if img:
                amap[img] = a.get("id") or ""
    except Exception:
        pass
    lines = ["参考图用法（按上传顺序，<Picture 1> 为主参考）："]
    for i, f in enumerate(refs[:16], 1):
        aid = amap.get(f) or "参考图"
        c = (aid or "").upper()[:1]
        if c == "C":
            role = "身份参考：保留脸型、五官、发型、眼镜、体态；服装、表情、站位、背景以文字为准；不要复制参考图的白底/多视图/拼图版式。"
        elif c == "L":
            role = "空间参考：只取空间布局、门窗、课桌/栏杆/楼梯关系与光源方向；不要复制空镜构图，也不要因为参考图里没人物就删人物。"
        elif c == "P":
            role = "道具参考：只取材质、磨损、状态与尺度；不要新增参考图外的道具。"
        else:
            role = "参考：只取与本镜相关的信息，不复制版式。"
        lines.append(f"<Picture {i}> = {aid}：{role}")
    lines.append("参考图只锁身份/空间/材质，不锁构图；与文字首帧状态冲突时以文字为准。输出单帧剧照，不是参考图的版式复制。")
    return prompt.rstrip() + "\n\n" + "\n".join(lines)


def shots_payload(project: Path, scene: str):
    sc = scenes.build(project)
    info = next((s for s in sc.get("scenes") or [] if s["id"] == scene), {}) or {}
    data = assetsdb.build(project)
    amap = {a["id"]: a for a in data.get("assets") or []}
    acc = accepted_map(project)
    out = []
    for sh in scenes.shot_details(project, scene):
        ids = []
        for aid in ASSET_ID.findall(sh.get("assets") or ""):
            if aid not in ids:
                ids.append(aid)
        desc = []
        for aid in ids:
            a = amap.get(aid) or {}
            d = (a.get("descriptor") or "").strip()
            if d:
                desc.append(f"{aid}：{d}")
        prompt = f"""电影剧照，16:9，单帧；不是角色设定板、不是多视图拼图，不要分栏。
场景：{info.get('place','')}，{info.get('interior','')}，{info.get('time','')}。
镜头：{sh.get('size','')}；{sh.get('light','')}。
首帧状态：{sh.get('firstFrame','')}。
动作起点：{sh.get('action','')}。
人物/物件：
{chr(10).join(desc) if desc else '按剧本在场人物与资产设定。'}
构图与风格：写实电影摄影，自然皮肤与布料纹理，2006年中国校园；只出现本镜在场人物；无文字、无标识、无水印、无现代物件穿帮。"""
        root = candidate_root(project) / scene / sh["id"]
        cands = []
        if root.is_dir():
            for f in root.glob("*"):
                if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    try: rel = str(f.relative_to(project))
                    except ValueError: rel = str(f)
                    cands.append({"file": rel, "name": f.name, "mtime": int(f.stat().st_mtime),
                                  "sizeKB": round(f.stat().st_size / 1024)})
        cands.sort(key=lambda x: x["mtime"], reverse=True)
        rec = acc.get((scene, sh["id"]))
        out.append({
            "id": sh["id"], "duration": sh["duration"], "size": sh.get("size", ""),
            "firstFrame": sh.get("firstFrame", ""), "action": sh.get("action", ""),
            "light": sh.get("light", ""), "assets": ids,
            "prompt": prompt, "refs": _refs(project, ids),
            "candidates": cands,
            "accepted": rec if rec and (project / rec.get("file", "")).is_file() else None,
        })
    return {"scene": scene, "place": info.get("place", ""), "shots": out}


def adopt(project: Path, scene: str, shot: str, file: str):
    p = (project / file).resolve()
    root = candidate_root(project).resolve()
    if not str(p).startswith(str(root) + "/") or not p.is_file():
        raise ValueError("首帧候选不在 制作运行/首帧候选 内")
    rec = {"scene": scene, "shot": shot, "file": file,
           "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    f = store.state_dir(project) / "首帧采用.jsonl"
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
