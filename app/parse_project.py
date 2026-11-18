#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导演工作台 · 只读解析层（P0 第一步）

只读项目文件，产出结构化 JSON 与检查清单；不写项目、不调生成、不改创作文件。

用法：
    python3 parse_project.py --project ~/short_v/有谁共鸣2 --episode EP002 --out build/data.json
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

SPEAKER = r"([\u4e00-\u9fa5]{2,4})"
SPK = r"(陈铭|秦淮|妻子|技术员|同事)"
COMMENT = re.compile(r"<!--.*?-->", re.S)
NO_REF_PLACE_MARK = "具体美术参考尚未生成"


def read(p: Path) -> str:
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def read_opt(p: Path) -> str:
    return read(p) if p.exists() else ""


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_script(text: str):
    scenes, order = {}, []
    text = COMMENT.sub("", text)
    heads = list(re.finditer(r"(?m)^## (EP\d{3}-SC\d+) (\S+) · ([^·]+?) · ([^\n]+)$", text))
    for i, m in enumerate(heads):
        sid = m.group(1)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        blocks, dialogue = [], []
        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            dm = re.match(rf"^{SPEAKER}(?:（([^）]*)）)?：(.*)$", line)
            if dm:
                note = dm.group(2) or ""
                dialogue.append({"spk": dm.group(1), "text": dm.group(3), "note": note})
                blocks.append({"t": "line", "spk": dm.group(1), "text": dm.group(3), "note": note})
            else:
                blocks.append({"t": "act", "text": line})
        scenes[sid] = {
            "id": sid, "kind": m.group(2),
            "place": m.group(3).strip(), "time": m.group(4).strip(),
            "blocks": blocks, "dialogue": dialogue,
        }
        order.append(sid)
    # 原著出处声明
    for mm in re.finditer(r"^- (SC\d+)：原文第(\d+)[^\d]{1,3}(\d+)行", text, re.M):
        key = "EP002-" + mm.group(1)
        if key in scenes:
            scenes[key]["srcRange"] = [int(mm.group(2)), int(mm.group(3))]
    dec = re.search(r"新增可发声对白仅(\d+)句", text)
    return [scenes[k] for k in order], (int(dec.group(1)) if dec else None)


def parse_storyboard(text: str):
    shots, cur = [], None
    for line in text.splitlines():
        hm = re.match(r"^## (SHOT-EP\d{3}-[^\s]+) · (.*)$", line)
        if hm:
            cur = {"id": hm.group(1), "title": hm.group(2), "fields": {},
                   "refs": [], "firstFrame": "", "_ff": False}
            shots.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("### "):
            # 只认「首帧提示词」这一节，其余小节（如整集汇总）结束采集
            cur["_ff"] = line.strip().startswith("### 首帧提示词")
            continue
        fm = re.match(r"^- ([^：]+)：(.*)$", line)
        if fm:
            k, v = fm.group(1).strip(), fm.group(2).strip()
            cur["fields"][k] = v
            cur["_ff"] = False
            continue
        if cur["_ff"] and line.startswith(">"):
            cur["firstFrame"] += line.lstrip("> ").strip()
        # 空行不结束块：首帧提示词与标题之间本来就有空行
    for s in shots:
        s["scene"] = s["fields"].get("来源", "")
        m = re.match(r"(\d+)", s["fields"].get("时长", ""))
        s["dur"] = int(m.group(1)) if m else 0
        s["sound"] = s["fields"].get("声音", "")
        s["characters"] = sorted(set(re.findall(r"人物「([^」]+)」", s["fields"].get("出场设定", ""))))
        for r in re.finditer(r"(REF-[A-Z]+)（顺序：(\d+)）· ([^《]+)《([^》]+)》",
                             s["fields"].get("输入参考图", "")):
            s["refs"].append({"key": r.group(1), "order": int(r.group(2)),
                              "path": r.group(3).strip(), "name": r.group(4)})
        s.pop("_ff", None)
    return shots


def parse_motion(text: str):
    out = {}
    for m in re.finditer(r"(?m)^## (MOTION-EP002-[^\s]+) · .*$", text):
        start = m.end()
        nxt = re.search(r"(?m)^## MOTION-", text[start:])
        body = text[start:start + (nxt.start() if nxt else len(text))]
        ref = re.search(r"^- 分镜：(.*)$", body, re.M)
        six, fields = {}, {}
        for fm in re.finditer(r"(?m)^- ([^：]+)：(.*)$", body):
            fields[fm.group(1).strip()] = fm.group(2).strip()
        for line in body.splitlines():
            lm = re.match(r"^> (\w+): (.*)$", line)
            if lm:
                six[lm.group(1)] = lm.group(2)
        if ref:
            out[ref.group(1).strip()] = {"six": six, "fields": fields}
    return out


def parse_visual(text: str):
    out = {"people": [], "places": [], "props": []}
    kind, item = None, None
    for line in text.splitlines():
        hm = re.match(r"^## (人物|地点|道具) · (.*)$", line)
        if hm:
            item = {"name": hm.group(2), "bullets": []}
            out[{"人物": "people", "地点": "places", "道具": "props"}[hm.group(1)]].append(item)
            continue
        bm = re.match(r"^\s*- ([^：]+)：(.*)$", line)
        if bm and item:
            item["bullets"].append({"k": bm.group(1), "v": bm.group(2)})
    # 全文出现的资产图片路径（视觉设定常在开头列出定妆照，不挂在人物小节下）
    all_paths = [m.lstrip("/") for m in re.findall(
        r"[\u4e00-\u9fa5A-Za-z0-9_/\-]*资产/[^\s、，。；（）()`]+?\.(?:png|jpg|jpeg|webp)", text)]

    def _guess(name):
        key = (name or "").split("（")[0].strip()[:2]
        if not key:
            return None
        for path in all_paths:
            if key in path.rsplit("/", 1)[-1]:
                return path
        return None

    # 人物 / 地点：只有明确写了指向现存图片的路径，才算有参考图
    for it in out["people"] + out["places"]:
        it["image"] = None
        for b in it["bullets"]:
            m = re.search(r"[\u4e00-\u9fa5A-Za-z0-9_/\-]*资产/[^\s、，。；（）()]+?\.(?:png|jpg|jpeg|webp)", b["v"])
            if m:
                it["image"] = m.group(0).lstrip("/")
                break
        if not it["image"]:
            it["image"] = _guess(it.get("name", ""))
    return out


def parse_episodes(path: Path):
    eps = []
    for line in read(path).splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        hook = d.get("hook") or {}
        obj = d.get("objective") or {}
        eps.append({"id": d["episode_id"], "title": d["title"],
                    "target": d.get("target_duration_seconds"),
                    "chapters": d.get("source_chapter_range"),
                    "hook": hook.get("realization") if isinstance(hook, dict) else str(hook or ""),
                    "objective": obj.get("desired_change") if isinstance(obj, dict) else str(obj or "")})
    return eps


def snap_frames(seconds: float) -> int:
    n = max(5, round(seconds * 24))
    return n + (5 - n % 17) % 17


def build(project: Path, episode: str):
    ep_dir = project / "剧集" / episode
    script_md = read_opt(ep_dir / "剧本.md")
    sb_md = read_opt(ep_dir / "分镜.md")
    vp_md = read_opt(ep_dir / "视频提示词.md")
    vs_md = read_opt(ep_dir / "视觉设定.md")
    present = {name: (ep_dir / name).exists() for name in
               ("剧本.md", "分镜.md", "视频提示词.md", "视觉设定.md")}

    scenes, declared = parse_script(script_md)
    shots = parse_storyboard(sb_md)
    motion = parse_motion(vp_md)
    assets = parse_visual(vs_md)
    episodes = parse_episodes(project / "项目开发/episode-map.jsonl")

    novel_path = next(iter(sorted((project / "输入").glob("*.txt"))), None)
    novel = read(novel_path) if novel_path else ""
    novel_norm = re.sub(r"\s", "", novel)

    # 台词出处
    for sc in scenes:
        for d in sc["dialogue"]:
            d["orig"] = re.sub(r"\s", "", d["text"]) in novel_norm

    # 台词链
    seq = [(s["id"], s["scene"],
            [(m.group(1), m.group(2).strip()) for m in
             re.finditer(rf"{SPEAKER}(?:（[^）]*）)?：([^；;]+)", s["sound"])])
           for s in shots]
    chain, gid = {}, 0
    for sc in scenes:
        rows = [(sid, l) for sid, s, ls in seq if s == sc["id"] for l in ls]
        j = 0
        for d in sc["dialogue"]:
            txt, buf, grp = d["text"], "", []
            while j < len(rows) and buf != txt:
                buf += rows[j][1][1]
                grp.append(rows[j][0])
                j += 1
            if len(grp) > 1:
                gid += 1
                uniq = list(dict.fromkeys(grp))
                for g in uniq:
                    chain[g] = {"group": gid, "shots": uniq, "text": txt}

    # 资产缺口
    people_img = {"陈铭": "资产/定妆照/陈铭6视图.png", "秦淮": "资产/定妆照/秦淮6视图.png"}
    place_has_ref = {}
    for p in assets["places"]:
        place_has_ref[p["name"]] = not any(NO_REF_PLACE_MARK in b["v"] for b in p["bullets"])

    shots_by_scene = {}
    for s in shots:
        shots_by_scene.setdefault(s["scene"], []).append(s)

    gaps = {"people": [], "places": [], "props": [], "shotsNoCharRef": [], "shotsNoMotion": []}
    for p in assets["people"]:
        hit = [s["id"] for s in shots if p["name"] in s["characters"]]
        if p["name"] not in people_img:
            gaps["people"].append({"name": p["name"], "shots": len(hit), "ids": hit})
    for p in assets["places"]:
        if not place_has_ref[p["name"]]:
            sc_ids = [sc["id"] for sc in scenes if sc["place"] == p["name"]]
            gaps["places"].append({"name": p["name"], "scenes": sc_ids,
                                   "shots": sum(len(shots_by_scene.get(i, [])) for i in sc_ids)})
    for p in assets["props"]:
        gaps["props"].append({"name": p["name"]})
    for s in shots:
        for c in s["characters"]:
            if c not in people_img:
                gaps["shotsNoCharRef"].append({"shot": s["id"], "character": c})
                break
        if s["id"] not in motion:
            gaps["shotsNoMotion"].append(s["id"])

    # 检查
    broken = [(s["id"], r["path"]) for s in shots for r in s["refs"] if not (project / r["path"]).exists()]
    short = [s["id"] for s in shots if snap_frames(s["dur"]) < 124]
    total = sum(s["dur"] for s in shots)
    gen_total = sum(snap_frames(s["dur"]) / 24 for s in shots)
    orig = sum(1 for sc in scenes for d in sc["dialogue"] if d["orig"])
    adapted = [{"scene": sc["id"], "spk": d["spk"], "text": d["text"]}
               for sc in scenes for d in sc["dialogue"] if not d["orig"]]

    # 提示词规范
    SIX = ["subject_definitions", "summary", "retention_analysis",
           "detailed_description", "overall_soundscape", "non_diegetic_music"]
    prompt_issues = []
    for s in shots:
        m = motion.get(s["id"])
        if not m:
            continue
        six = m.get("six") or {}
        missing = [k for k in SIX if not (six.get(k) or "").strip()]
        if missing:
            prompt_issues.append(f"{s['id']}：缺 {','.join(missing)}")
        refs_n = len(s["refs"])
        nums = [int(x) for x in re.findall(r"<Picture (\d+)>", six.get("detailed_description", "")
                                           + six.get("subject_definitions", ""))]
        if nums and max(nums) > refs_n:
            prompt_issues.append(f"{s['id']}：引用 <Picture {max(nums)}> 但只有 {refs_n} 张参考图")

    # 服装状态声明 vs 提示词
    WARDROBE = re.compile(r"(SC\d{3})((?:(?!SC\d{3})[^；。])*)")
    wardrobe = {"declared": [], "issues": []}
    for person in assets["people"]:
        for b in person["bullets"]:
            if not re.search(r"外套|上衣|衣服|衬衫", b["v"]):
                continue
            for m in WARDROBE.finditer(b["v"]):
                scene_id = "EP002-" + m.group(1)
                phrase = m.group(2).strip("，, ")
                if not re.search(r"外套|上衣|衣服|衬衫", phrase):
                    continue
                wardrobe["declared"].append({"person": person["name"], "scene": scene_id, "state": phrase[:28]})
                ss = [x for x in shots if x["scene"] == scene_id]
                if not ss:
                    continue
                hit = any(re.search(r"外套|上衣|衣服|衬衫|内衬", x["firstFrame"]) for x in ss)
                if not hit:
                    wardrobe["issues"].append(
                        f"{scene_id}：视觉设定声明「{person['name']} {phrase[:20]}」，"
                        f"但该场 {len(ss)} 个首帧提示词均未写服装状态")

    # 镜号规范
    sc_fmt = [s["id"] for s in shots if re.match(r"SHOT-EP002-SC\d{3}-\d{2}[A-C]?$", s["id"])]
    r_fmt = [s["id"] for s in shots if re.match(r"SHOT-EP002-R\d{3}$", s["id"])]
    other = [s["id"] for s in shots if s["id"] not in sc_fmt and s["id"] not in r_fmt]

    # 服装/道具关键词矩阵（信息项，不自动判定）
    KEYWORDS = ["外套", "毯子", "烟", "纸杯", "茶杯", "酒盅", "白酒", "瓶子", "名片", "教材", "茶叶"]
    matrix = {}
    for kw in KEYWORDS:
        hits = []
        for sc in scenes:
            ss = [x for x in shots if x["scene"] == sc["id"]]
            if any(kw in (x["firstFrame"] + x["sound"] + x.get("fields", {}).get("动作", "")) for x in ss):
                hits.append(sc["id"][-3:])
        matrix[kw] = hits

    checks = {
        "refsBroken": {"count": len(broken), "detail": broken[:5]},
        "promptFormat": {"issues": prompt_issues, "checked": len(motion)},
        "idScheme": {"scFormat": len(sc_fmt), "rFormat": len(r_fmt), "other": other},
        "keywordMatrix": matrix,
        "wardrobe": wardrobe,
        "shortShots": {"count": len(short), "ids": short},
        "motionCoverage": {"have": len(shots) - len(gaps["shotsNoMotion"]), "total": len(shots)},
        "dialogueConsistency": {"lines": sum(len(sc["dialogue"]) for sc in scenes),
                                "chars": sum(len(d["text"]) for sc in scenes for d in sc["dialogue"])},
        "sourceFidelity": {"orig": orig, "adapted": len(adapted), "declared": declared},
        "frameGrid": {"editSeconds": total, "genSeconds": round(gen_total, 1),
                      "trimSeconds": round(gen_total - total, 1)},
        "sceneCoverage": {"scenes": len(scenes), "covered": len([s for s in shots_by_scene])},
    }

    return {
        "project": project.name, "episode": episode, "episodes": episodes,
        "files": {name: sha(src) for name, src, ok in (
            ("剧本.md", script_md, present["剧本.md"]),
            ("分镜.md", sb_md, present["分镜.md"]),
            ("视频提示词.md", vp_md, present["视频提示词.md"]),
            ("视觉设定.md", vs_md, present["视觉设定.md"])) if ok},
        "scenes": scenes, "shots": shots, "motion": motion,
        "assets": assets, "chain": chain, "gaps": gaps, "checks": checks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=str(Path.home() / "short_v/有谁共鸣2"))
    ap.add_argument("--episode", default="EP002")
    ap.add_argument("--out", default="build/data.json")
    a = ap.parse_args()

    data = build(Path(a.project).expanduser(), a.episode)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    c, g = data["checks"], data["gaps"]
    print(f"项目 {data['project']} · {data['episode']}")
    print(f"  场次 {len(data['scenes'])} · 镜头 {len(data['shots'])} · 台词 {c['dialogueConsistency']['lines']} 句")
    print(f"  检查：断链 {c['refsBroken']['count']} · 短镜 {c['shortShots']['count']} · "
          f"提示词 {c['motionCoverage']['have']}/{c['motionCoverage']['total']} · "
          f"原句 {c['sourceFidelity']['orig']}（改编 {c['sourceFidelity']['adapted']}，声明 {c['sourceFidelity']['declared']}）")
    print(f"  帧网格：标注 {c['frameGrid']['editSeconds']}s → 生成 {c['frameGrid']['genSeconds']}s "
          f"（多 {c['frameGrid']['trimSeconds']}s）")
    print(f"\n资产缺口：")
    print(f"  人物 {len(g['people'])} 个未定图：" +
          "、".join(f"{x['name']}({x['shots']}镜)" for x in g["people"]))
    print(f"  地点 {len(g['places'])} 处无参考：" +
          "、".join(f"{x['name']}({x['shots']}镜)" for x in g["places"]))
    print(f"  道具 {len(g['props'])} 项无图：" + "、".join(x["name"] for x in g["props"]))
    print(f"  角色缺图影响的镜头：{len(g['shotsNoCharRef'])} 个")
    print(f"\n写出 {out}  ({out.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
