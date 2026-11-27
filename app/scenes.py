# -*- coding: utf-8 -*-
"""分场索引：以「场」为主键，串起 05 剧本、08 表演、09 规划与生成结果。

数据来源（只读）：
  05-正式剧本/screenplay-v001.md       逐场锁定表（N001–N008 → E00x-Sxx）
  08-表演设计/N00x-acting-v001.md      表演设计是否就绪
  09-镜头与视频提示词/镜头规划/N00x/    镜头表、生成单元
  09-镜头与视频提示词/生文提示词/N00x/  视频提示词
  09-镜头与视频提示词/生成结果/N00x/    生成产物
"""
import re
from pathlib import Path

SCREENPLAY = "05-正式剧本/screenplay-v001.md"
PLAN_DIR = "08-镜头规划"
VP_DIR = "09-镜头与视频提示词/视频提示词"
RESULT_DIR = "09-镜头与视频提示词/生成结果"

ROW = re.compile(r"^\|\s*(N\d{3})／(E\d{3}-S\d{2})\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|", re.M)


def _episode_of(nid: str) -> str:
    m = re.match(r"N(\d{3})", nid)
    if not m:
        return ""
    n = int(m.group(1))
    # 05 锁定表：N001-N003→E001，N004-N006→E002，N007-N008→E003
    return "E001" if n <= 3 else "E002" if n <= 6 else "E003"


def parse_lock_table(project: Path) -> list:
    f = project / SCREENPLAY
    if not f.exists():
        return []
    txt = f.read_text(encoding="utf-8")
    out = []
    for m in ROW.finditer(txt):
        out.append({
            "id": m.group(1), "epScene": m.group(2),
            "interior": m.group(3).strip(), "place": m.group(4).strip(),
            "time": m.group(5).strip(), "cast": m.group(6).strip(),
            "episode": _episode_of(m.group(1)),
        })
    return out


def parse_shots(project: Path, scene: str) -> list:
    d = project / PLAN_DIR / scene
    if not d.is_dir():
        return []
    f = next(iter(sorted(d.glob("shot-plan*.md"))), None)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"^### (SH\d{3})｜([^\n]+)$", txt, re.M):
        out.append({"id": m.group(1), "title": m.group(2).strip(), "source": f.name})
    return out


def material_map(project: Path, scene: str) -> dict:
    """reference-map 里的 R## 编号 -> {asset, file, role}。"""
    d = project / PLAN_DIR / scene
    f = next(iter(sorted(d.glob("reference-map*.md"))), None) if d.is_dir() else None
    out = {}
    if not f:
        return out
    txt = f.read_text(encoding="utf-8")
    for m in re.finditer(r"^\|\s*(R\d{2})／([^|]+?)\s*\|([^|]*)\|([^|]*)\|", txt, re.M):
        label, asset = m.group(1), m.group(2).split("／")[0].strip()
        cells = [c.strip() for c in txt[m.start():txt.find("\n", m.start())].split("|")]
        path = ""
        for c in cells:
            pm = re.search(r"(07-图像资产/[^\s；，]+?\.(?:png|jpg|jpeg|webp))", c)
            if pm:
                path = pm.group(1)
                break
        out[label] = {"asset": asset, "file": path,
                      "role": "identity" if asset.startswith("C") else "scene"}
    return out


def shot_details(project: Path, scene: str) -> list:
    """08 镜头规划里的逐镜明细。"""
    d = project / PLAN_DIR / scene
    if not d.is_dir():
        return []
    f = next(iter(sorted(d.glob("shot-plan*.md"))), None)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")

    # 总表：镜号 -> 时长/景别/机位/单元
    table = {}
    for m in re.finditer(r"^\|\s*(SH\d{3})\s*\|([^\n]*)$", txt, re.M):
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) >= 5:
            table[m.group(1)] = {"purpose": cells[0], "size": cells[1],
                                 "camera": cells[2], "duration": cells[3], "unit": cells[4]}

    parts = re.split(r"^### (SH\d{3})｜([^\n]+)$", txt, flags=re.M)
    out = []
    for i in range(1, len(parts) - 2, 3):
        sid, title, body = parts[i], parts[i + 1].strip(), parts[i + 2]

        def grab(*labels):
            for lab in labels:
                m = re.search(rf"^- {lab}[：:](.+)$", body, re.M)
                if m:
                    return m.group(1).strip()
            return ""

        row = table.get(sid, {})
        raw_dur = row.get("duration", "")
        try:
            dur_num = float(str(raw_dur).replace("s", "").strip())
        except Exception:
            dur_num = 0
        out.append({
            "id": sid, "title": title, "source": str(f.relative_to(project)),
            "duration": dur_num, "durationText": str(raw_dur), "size": row.get("size", ""),
            "camera": row.get("camera", ""), "unit": row.get("unit", ""),
            "action": grab("动作与节拍", "动作与表演"),
            "performance": grab("表演"),
            "composition": grab("构图"),
            "light": grab("光线"),
            "firstFrame": grab("首态", "首帧硬状态"),
            "lastFrame": grab("尾态", "尾帧硬状态"),
            "assets": grab("依据素材", "依据资产"),
            "body": body.strip(),
        })
    return out


def scene_status(project: Path, scene: str) -> dict:
    plan_dir = project / PLAN_DIR / scene
    vp = list((project / VP_DIR / scene).glob("*.md")) if (project / VP_DIR / scene).is_dir() else []
    results = list((project / RESULT_DIR / scene).rglob("*.mp4")) if (project / RESULT_DIR / scene).is_dir() else []
    return {
        "planned": plan_dir.is_dir(),
        "planFiles": len(list(plan_dir.glob("*.md"))) if plan_dir.is_dir() else 0,
        "videoPrompts": len(vp),
        "videos": len(results),
    }


def build(project: Path) -> dict:
    scenes = parse_lock_table(project)
    for s in scenes:
        shots = shot_details(project, s["id"])
        st = scene_status(project, s["id"])
        s.update({
            "shots": len(shots),
            "duration": sum(x["duration"] for x in shots) if shots else 0,
            "shotList": [{"id": x["id"], "duration": x["duration"]} for x in shots],
            **st,
        })
        # 阶段推进度：1 未开始 → 3 有镜头规划 → 4 已出片（08 现在就是镜头规划，没有单独表演页）
        if st["videos"]: s["stage"] = 4
        elif st["planned"]: s["stage"] = 3
        else: s["stage"] = 1
    episodes = {}
    for s in scenes:
        episodes.setdefault(s["episode"], []).append(s["id"])
    return {
        "scenes": scenes,
        "episodes": [{"id": k, "scenes": v} for k, v in sorted(episodes.items())],
        "summary": {
            "scenes": len(scenes),
            "planned": sum(1 for s in scenes if s["planned"]),
            "withShots": sum(1 for s in scenes if s["shots"]),
            "withVideo": sum(1 for s in scenes if s["videos"]),
            "totalSeconds": sum(s["duration"] for s in scenes),
        },
    }
