#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把解析结果整理成工作台 UI 需要的形状（纯只读，不写项目文件）。"""
import base64
import subprocess
import tempfile
from pathlib import Path

from parse_project import build

VIDEO_DIR = Path("/checkpoints/huxian/h3/output/video/MiniMax_H3")
POSTERS = ["r2v_00001_.mp4", "bench_turboB_6step_00001_.mp4", "bench_turboA_00001_.mp4"]


def snap_frames(sec: float) -> int:
    n = max(5, round(sec * 24))
    return n + (5 - n % 17) % 17


def jpg_b64(src: Path, width=560, frame=None) -> str:
    if not src.exists():
        return ""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = f.name
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if frame is not None:
        cmd += ["-vf", f"select=eq(n\\,{frame}),scale={width}:-2", "-frames:v", "1"]
    else:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd += ["-q:v", "5", tmp]
    try:
        subprocess.run(cmd, check=True)
        return "data:image/jpeg;base64," + base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception:
        return ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def build_images(project: Path) -> dict:
    imgs = {
        "chen": jpg_b64(project / "资产/定妆照/陈铭6视图.png", 620),
        "qin": jpg_b64(project / "资产/定妆照/秦淮6视图.png", 620),
        "office": jpg_b64(project / "资产/场景/陈铭办公室.png", 720),
    }
    for i, name in enumerate(POSTERS):
        imgs[f"v{i}"] = jpg_b64(VIDEO_DIR / name, 520, frame=30)
    return imgs


def _empty(project: Path, episode: str, with_images: bool = True) -> dict:
    eps = []
    try:
        import parse_project as P
        eps = P.parse_episodes(project / "项目开发/episode-map.jsonl")
        for e in eps:
            e["hasScript"] = (project / "剧集" / e["id"] / "剧本.md").exists()
    except Exception:
        pass
    zero = {"shots": 0, "scenes": 0, "totalDur": 0, "genTotal": 0, "trimShots": 0,
            "brokenOcc": 0, "brokenShots": 0, "refTotal": 0, "refPaths": {},
            "promptShots": 0, "shortShots": [], "chainGroups": 0, "dialogue": 0,
            "dialogueChars": 0, "origLines": 0, "adapted": [], "srcScenes": 0,
            "declaredAdapt": 0, "places": 0, "placesNoRef": 0, "props": 0, "people": 0,
            "idScheme": None, "promptFormat": None, "keywordMatrix": None, "wardrobe": None,
            "assetFiles": len(list((project / "资产").rglob("*.png")))}
    return {"data": {"project": f"{project.name} · {episode}", "episode": episode,
                     "episodes": eps, "scenes": [], "shots": [],
                     "assets": {"people": [], "places": [], "props": []},
                     "chain": {}, "stats": zero,
                     "checks": {}, "gaps": {}, "files": {}, "empty": True},
            "images": build_images(project) if with_images else {}}


def load(project: Path, episode: str = "EP002", with_images: bool = True) -> dict:
    ep_dir = project / "剧集" / episode
    if not (ep_dir / "剧本.md").exists():
        return _empty(project, episode, with_images)
    d = build(project, episode)
    shots, scenes, motion = d["shots"], d["scenes"], d["motion"]

    for s in shots:
        s["motion"] = motion.get(s["id"])
        s["frames"] = snap_frames(s["dur"])
        s["short"] = s["frames"] < 124
        s["genSec"] = s["frames"] / 24
        s["trim"] = s["genSec"] - s["dur"]
        for r in s["refs"]:
            r["exists"] = (project / r["path"]).exists()
        s["broken"] = [r for r in s["refs"] if not r["exists"]]

    by_scene = {}
    for s in shots:
        by_scene.setdefault(s["scene"], []).append(s)
    for sc in scenes:
        lst = by_scene.get(sc["id"], [])
        sc["shotCount"] = len(lst)
        sc["dur"] = sum(x["dur"] for x in lst)
        sc["hasPrompt"] = sum(1 for x in lst if x["motion"])
        sc["broken"] = sum(len(x["broken"]) for x in lst)
        sc["src"] = sc.get("srcRange")

    for pl in d["assets"].get("places", []):
        pl["imageExists"] = bool(pl.get("image")) and (project / pl["image"]).exists()

    ref_paths = {}
    for key in ("REF-CHEN", "REF-QIN", "REF-OFFICE"):
        ref_paths[key] = sorted({r["path"] for s in shots for r in s["refs"] if r["key"] == key})

    dialogue = sum(len(sc["dialogue"]) for sc in scenes)
    short = [s["id"] for s in shots if s["short"]]
    adapted = [{"scene": sc["id"], "spk": dd["spk"], "text": dd["text"]}
               for sc in scenes for dd in sc["dialogue"] if not dd["orig"]]

    episodes = []
    for e in d["episodes"]:
        e = dict(e)
        e["hasScript"] = (project / "剧集" / e["id"] / "剧本.md").exists()
        episodes.append(e)

    stats = {
        "shots": len(shots), "scenes": len(scenes),
        "totalDur": sum(s["dur"] for s in shots),
        "genTotal": sum(s["genSec"] for s in shots),
        "trimShots": sum(1 for s in shots if s["trim"] > 0.02),
        "brokenOcc": sum(len(s["broken"]) for s in shots),
        "brokenShots": sum(1 for s in shots if s["broken"]),
        "refTotal": sum(len(s["refs"]) for s in shots),
        "refPaths": ref_paths,
        "promptShots": sum(1 for s in shots if s["motion"]),
        "shortShots": short,
        "chainGroups": len({v["group"] for v in d["chain"].values()}),
        "dialogue": dialogue,
        "dialogueChars": sum(len(dd["text"]) for sc in scenes for dd in sc["dialogue"]),
        "origLines": sum(1 for sc in scenes for dd in sc["dialogue"] if dd["orig"]),
        "adapted": adapted,
        "srcScenes": sum(1 for sc in scenes if sc.get("src")),
        "declaredAdapt": d["checks"]["sourceFidelity"]["declared"],
        "places": len(d["assets"]["places"]),
        "placesNoRef": sum(1 for pl in d["assets"].get("places", []) if not pl.get("imageExists")),
        "props": len(d["assets"]["props"]),
        "people": len(d["assets"]["people"]),
        "idScheme": d["checks"].get("idScheme"),
        "promptFormat": d["checks"].get("promptFormat"),
        "keywordMatrix": d["checks"].get("keywordMatrix"),
        "wardrobe": d["checks"].get("wardrobe"),
    }

    data = {
        "project": f"{d['project']} · {episode}",
        "episode": episode,
        "episodes": episodes,
        "scenes": scenes, "shots": shots, "assets": d["assets"],
        "chain": d["chain"], "stats": stats, "checks": d["checks"],
        "gaps": d["gaps"], "files": d["files"],
    }
    return {"data": data, "images": build_images(project) if with_images else {}}


if __name__ == "__main__":
    import json
    out = load(Path.home() / "short_v/有谁共鸣2")
    print(json.dumps(out["data"]["stats"], ensure_ascii=False, indent=1)[:800])
    print("图片数:", len(out["images"]), "非空:", sum(1 for v in out["images"].values() if v))
