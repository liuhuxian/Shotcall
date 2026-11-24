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
ACTING_DIR = "08-表演设计"
PLAN_DIR = "09-镜头与视频提示词/镜头规划"
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
    shots = []
    for f in sorted(d.glob("shot-plan*.md")):
        txt = f.read_text(encoding="utf-8")
        for m in re.finditer(r"^## (SH\d{3})｜(\d+)至(\d+)秒\s*$", txt, re.M):
            blocks = re.split(r"^## SH\d{3}", txt, flags=re.M)
            shots.append({
                "id": m.group(1),
                "start": int(m.group(2)), "end": int(m.group(3)),
                "duration": int(m.group(3)) - int(m.group(2)),
                "source": f.name,
            })
        break
    return shots


def shot_details(project: Path, scene: str) -> list:
    """带景别、时长、对白、首尾状态的镜头明细。"""
    d = project / PLAN_DIR / scene
    if not d.is_dir():
        return []
    f = next(iter(sorted(d.glob("shot-plan*.md"))), None)
    if not f:
        return []
    txt = f.read_text(encoding="utf-8")
    parts = re.split(r"^## (SH\d{3})｜(\d+)至(\d+)秒\s*$", txt, flags=re.M)
    out = []
    for i in range(1, len(parts) - 2, 4):
        sid, s0, s1, body = parts[i], int(parts[i + 1]), int(parts[i + 2]), parts[i + 3]

        def grab(label):
            m = re.search(rf"^- {label}[：:](.+)$", body, re.M)
            return m.group(1).strip() if m else ""

        out.append({
            "id": sid, "start": s0, "end": s1, "duration": s1 - s0,
            "source": str(f.relative_to(project)),
            "purpose": grab("目的与调度"),
            "size": grab("时长"),
            "light": grab("光线"),
            "action": grab("动作与表演"),
            "firstFrame": grab("首帧硬状态"),
            "lastFrame": grab("尾帧硬状态"),
            "cut": grab("剪切理由"),
            "assets": grab("依据资产"),
            "status": grab("状态"),
        })
    return out


def scene_status(project: Path, scene: str) -> dict:
    acting = list((project / ACTING_DIR).glob(f"{scene}-acting*.md")) if (project / ACTING_DIR).is_dir() else []
    plan_dir = project / PLAN_DIR / scene
    vp = list((project / VP_DIR / scene).glob("*.md")) if (project / VP_DIR / scene).is_dir() else []
    results = list((project / RESULT_DIR / scene).rglob("*.mp4")) if (project / RESULT_DIR / scene).is_dir() else []
    return {
        "acting": bool(acting),
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
        # 阶段推进度：0 未开始 → 4 已出片
        if st["videos"]: s["stage"] = 4
        elif st["planned"]: s["stage"] = 3
        elif st["acting"]: s["stage"] = 2
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
