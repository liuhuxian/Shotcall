# -*- coding: utf-8 -*-
"""导演台流程页：01-09 阶段卡、提示词、登记/确认（调用项目 artifact_guard.py）。

只读写项目登记与文件；不生成图片/视频。技能名对应项目 .agents/skills/ 下的
ai-film-router + film-*；提示词里的 {{...}} 由 render_prompt 替换。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import artifacts
import store

VER = re.compile(r"-v(\d{3})(?:-\d+)?\.md$")


def _prompt(no, name, skill, inputs, output, scope):
    return f"""你是本项目执行 Agent。先读取项目内 .agents/skills/ 下的 ai-film-router，按它的规则只执行 {no} {name}；加载 {skill}。只做当前阶段，不自动跨阶段；不要调用生图/视频接口。

项目根：{{{{PROJECT}}}}
输入：
{{{{INPUTS}}}}
输出：{{{{OUTPUT}}}}（只新增 vNNN，不覆盖旧版；版本由导演台流程卡给出）
范围：{{{{SCOPE}}}}

要求：
- 只写本阶段文件；不改其他阶段；不手改 artifact-state.json；不自行确认。
- 完成后只汇报：版本、路径、需要导演拍板的点。

验收：文件存在；含本阶段必需字段；没有下游阶段内容；没有覆盖旧版。
"""


STEPS = [
    dict(key="01", no="01", stage="01", name="概念锚定", skill="film-synopsis",
         kind="01 概念锚定", scope="全项目",
         pattern=r"^01-概念锚定/synopsis-v(\d{3})(?:-\d+)?\.md$",
         output="01-概念锚定/synopsis-{v}.md", deps=[],
         prompt=_prompt("01", "概念锚定", "film-synopsis", ["01-概念锚定/synopsis-vNNN.md"], "01-概念锚定/synopsis-vNNN.md", "全项目")),
    dict(key="02", no="02", stage="02", name="角色圣经", skill="film-characters",
         kind="02 角色圣经", scope="全项目",
         pattern=r"^02-角色圣经/characters-v(\d{3})(?:-\d+)?\.md$",
         output="02-角色圣经/characters-{v}.md", deps=["01"],
         prompt=_prompt("02", "角色圣经", "film-characters", ["01 已确认版"], "02-角色圣经/characters-vNNN.md", "全项目")),
    dict(key="03", no="03", stage="03", name="世界观（可选）", skill="film-worldbuilding",
         kind="03 世界观", scope="全项目",
         pattern=r"^03-世界观/worldbuilding-v(\d{3})(?:-\d+)?\.md$",
         output="03-世界观/worldbuilding-{v}.md", deps=["01", "02"],
         prompt=_prompt("03", "世界观", "film-worldbuilding", ["01/02 已确认版"], "03-世界观/worldbuilding-vNNN.md", "全项目")),
    dict(key="04", no="04", stage="04", name="分场大纲", skill="film-treatment",
         kind="04 分场大纲", scope="全项目",
         pattern=r"^04-分场大纲/treatment-v(\d{3})(?:-\d+)?\.md$",
         output="04-分场大纲/treatment-{v}.md", deps=["01", "02", "03"],
         prompt=_prompt("04", "分场大纲", "film-treatment", ["01/02/03 已确认版；原著章节范围"], "04-分场大纲/treatment-vNNN.md", "全项目")),
    dict(key="05", no="05", stage="05", name="正式剧本", skill="film-screenplay",
         kind="05 剧本深度融合", scope="全项目",
         pattern=r"^05-正式剧本/screenplay-v(\d{3})(?:-\d+)?\.md$",
         output="05-正式剧本/screenplay-{v}.md", deps=["04", "02", "03"],
         prompt=_prompt("05", "正式剧本", "film-screenplay", ["04/02/03 已确认版；原著"], "05-正式剧本/screenplay-vNNN.md（时长复核由 05b 卡单独登记）", "当前批次")),
    dict(key="05b", no="05b", stage="05", name="时长复核", skill="film-screenplay",
         kind="05 成稿时长复核", scope="全项目",
         pattern=r"^05-正式剧本/screenplay-timing-audit-v(\d{3})(?:-\d+)?\.md$",
         output="05-正式剧本/screenplay-timing-audit-{v}.md", deps=["05"],
         prompt=_prompt("05b", "时长复核", "film-screenplay", ["05 已确认正式剧本"], "05-正式剧本/screenplay-timing-audit-vNNN.md", "当前批次")),
    dict(key="06", no="06", stage="06", name="剧本压力测试（可选）", skill="film-stress-test",
         kind="06 剧本压力测试", scope="全项目", register="conclusion",
         pattern=r"^06-剧本压力测试/stress-test-report-v(\d{3})(?:-\d+)?\.md$",
         output="06-剧本压力测试/stress-test-report-{v}.md", deps=["05"],
         prompt=_prompt("06", "剧本压力测试", "film-stress-test", ["05 已确认版"], "06-剧本压力测试/stress-test-report-vNNN.md", "全项目")),
    dict(key="07", no="07", stage="07", name="Lira 图像资产", skill="film-lira-assets",
         kind="07 Lira 图像资产", scope="全项目",
         pattern=r"^07-图像资产/assets-v(\d{3})(?:-\d+)?\.md$",
         output="07-图像资产/assets-{v}.md", deps=["05"],
         prompt=_prompt("07", "Lira 图像资产", "film-lira-assets", ["05 已确认版；production-breakdown；02"], "07-图像资产/assets-vNNN.md", "全项目")),
    dict(key="08", no="08", stage="08", name="ACTING 表演", skill="film-acting",
         kind="08 ACTING 表演设计", scope="{scene}",
         pattern=r"^08-表演设计/(N\d{3})-acting-v(\d{3})(?:-\d+)?\.md$",
         output="08-表演设计/{scene}-acting-{v}.md", deps=["05", "07"],
         prompt=_prompt("08", "ACTING 表演", "film-acting", ["当前场 05；07 资产"], "08-表演设计/{scene}-acting-vNNN.md", "{scene}")),
    dict(key="09a", no="09a", stage="09", name="镜头规划", skill="film-cinedance",
         kind="09 镜头规划", scope="{scene}",
         pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/shot-plan-v(\d{3})(?:-\d+)?\.md$",
         output="09-镜头与视频提示词/镜头规划/{scene}/shot-plan-{v}.md", deps=["05", "08", "07"],
         prompt=_prompt("09a", "镜头规划", "film-cinedance", ["05/08/07 已确认版"], "09-镜头与视频提示词/镜头规划/{scene}/shot-plan-vNNN.md", "{scene}")),
    dict(key="09b", no="09b", stage="09", name="视频提示词", skill="film-cinedance",
         kind="09 视频提示词", scope="{scene}",
         pattern=r"^09-镜头与视频提示词/视频提示词/(N\d{3})/minimax-h3-ref2va-v(\d{3})(?:-\d+)?\.md$",
         output="09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-ref2va-{v}.md", deps=["09a", "07"],
         prompt=_prompt("09b", "视频提示词", "film-cinedance", ["09a 已确认镜头表；07 已确认资产"], "09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-ref2va-vNNN.md", "{scene}")),
]

BY_KEY = {s["key"]: s for s in STEPS}
STATUS = {"已确认-当前": "confirmed", "外部提供-已确认": "confirmed",
          "草稿待确认": "draft", "历史已替代": "superseded",
          "已过期-禁止使用": "superseded",
          "已完成-结论": "conclusion", "已作废（不合格）": "invalid",
          "缺文件-阻断": "missing"}


def _ver(path: str) -> int:
    m = VER.search(path or "")
    return int(m.group(1)) if m else 0


def _match(step, path: str, scene: str = None):
    m = re.search(step["pattern"], path)
    if not m:
        return None
    if step["scope"] == "{scene}":
        if not scene or m.group(1) != scene:
            return None
        return int(m.group(2))
    return int(m.group(1))


def _all(project: Path):
    return artifacts.build(project).get("artifacts") or []


def _latest(project: Path, step, scene=None):
    rows = []
    for a in _all(project):
        ver = _match(step, a.get("path", ""), scene)
        if ver is not None:
            rows.append((ver, a.get("mtime") or 0, a))
    if not rows:
        return None
    rows.sort(key=lambda x: (x[0], x[1]))
    return rows[-1][2]


def _new_file(project: Path, step, scene=None):
    """目录里存在、但登记表里还没有的 vNNN 文件。"""
    known = {a.get("path") for a in _all(project)}
    best = None
    for a in _all(project):  # 只用于已知集合
        pass
    for p in project.glob("**/*"):
        if not p.is_file() or p.suffix != ".md":
            continue
        rel = str(p.relative_to(project))
        ver = _match(step, rel, scene)
        if ver is None or rel in known:
            continue
        if best is None or (ver, p.stat().st_mtime) > best[0]:
            best = ((ver, p.stat().st_mtime), rel)
    return best[1] if best else None


def _new_files(project: Path, step, scene=None):
    rows_all = _all(project)
    known = {a.get("path") for a in rows_all}
    known_versions = []
    for a in rows_all:
        v = _match(step, a.get("path", ""), scene)
        if v is not None:
            known_versions.append(v)
    top = max(known_versions, default=0)
    rows = []
    for p in project.glob("**/*"):
        if not p.is_file() or p.suffix != ".md":
            continue
        rel = str(p.relative_to(project))
        ver = _match(step, rel, scene)
        if ver is None or rel in known or ver <= top:
            continue
        rows.append((ver, p.stat().st_mtime, rel))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r[2] for r in rows]


def _versions(project: Path, step, scene=None):
    rows = []
    for a in _all(project):
        ver = _match(step, a.get("path", ""), scene)
        if ver is None:
            continue
        rows.append({"path": a["path"], "version": f"v{ver:03d}", "statusText": a.get("statusText"),
                     "sha256": a.get("sha256"), "dependsOn": a.get("dependsOn") or [],
                     "confirmedAt": a.get("confirmedAt") or a.get("createdAt")})
    rows.sort(key=lambda x: x["version"], reverse=True)
    return rows


def _ledger_rows(project: Path, name: str):
    f = store.state_dir(project) / name
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _last_for(project: Path, name: str, key: str):
    rows = [r for r in _ledger_rows(project, name) if r.get("step") == key]
    return rows[-1] if rows else None


def _next_ver(project: Path, step, scene=None):
    a = _latest(project, step, scene)
    return (_ver(a["path"]) + 1) if a else 1


def _inputs(project: Path, step, scene=None):
    out = []
    for key in step.get("deps") or []:
        dep = BY_KEY.get(key)
        if dep:
            a = _latest(project, dep, scene if dep["scope"] == "{scene}" else None)
            if a:
                out.append(a["path"])
    return out


def _render(step, project, scene):
    scope = scene if step["scope"] == "{scene}" else ("全项目" if step["scope"] == "全项目" else step["scope"])
    latest = _latest(project, step, scene)
    nv = (_ver(latest["path"]) + 1) if latest else 1
    out = step["output"].format(scene=scope, v=f"v{nv:03d}")
    inputs = _inputs(project, step, scene)
    prompt = step["prompt"]
    prompt = prompt.replace("{{PROJECT}}", str(project))
    prompt = prompt.replace("{{SCOPE}}", scope)
    prompt = prompt.replace("{{INPUTS}}", "\n".join(f"- {x}" for x in inputs) or "- （无）")
    prompt = prompt.replace("{{OUTPUT}}", out)
    return {"prompt": prompt, "latest": latest, "nextVersion": f"v{nv:03d}",
            "nextPath": out, "inputs": inputs}


def artifact_step(project: Path, path: str):
    """把一个登记成果路径映射到流程卡；返回 (step, scene) 或 (None, None)。"""
    scenes = re.findall(r"(?<![A-Z])(N\d{3})(?![0-9])", path or "")
    scene = scenes[0] if scenes else "N001"
    for s in STEPS:
        if _match(s, path, scene if s["scope"] == "{scene}" else None) is not None:
            return s, (scene if s["scope"] == "{scene}" else None)
    return None, None


def steps_payload(project: Path, scene: str = "N001"):
    out = []
    for s in STEPS:
        r = _render(s, project, scene)
        a = r["latest"]
        out.append({
            "key": s["key"], "no": s["no"], "name": s["name"], "skill": s["skill"],
            "kind": s["kind"], "scope": s["scope"].replace("{scene}", scene),
            "status": (STATUS.get(a.get("statusText"), "unknown") if a else "empty"),
            "statusText": (a.get("statusText") if a else "未开始"),
            "path": (a.get("path") if a else None),
            "version": (f"v{_ver(a['path']):03d}" if a else None),
            "nextVersion": r["nextVersion"], "nextPath": r["nextPath"],
            "inputs": r["inputs"], "prompt": r["prompt"],
            "newFiles": _new_files(project, s, scene if s["scope"] == "{scene}" else None),
            "versions": _versions(project, s, scene if s["scope"] == "{scene}" else None),
            "returned": _last_for(project, "flow-returns.jsonl", s["key"]),
            "skipped": _last_for(project, "flow-skips.jsonl", s["key"]),
            "canConfirm": bool(a and STATUS.get(a.get("statusText")) == "draft"),
            "canRegister": True,
        })
    return {"scene": scene, "steps": out}


def guard_script(project: Path):
    """兼容两种项目技能布局：
    - 嵌套：.agents/skills/<包名>/ai-film-router/scripts/artifact_guard.py
    - 扁平：.agents/skills/ai-film-router/scripts/artifact_guard.py
    """
    root = project / ".agents/skills"
    cands = list(root.glob("*/ai-film-router/scripts/artifact_guard.py"))
    cands += list(root.glob("ai-film-router/scripts/artifact_guard.py"))
    cands = sorted(set(cands), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def _run_guard(project: Path, args: list):
    script = guard_script(project)
    if not script:
        raise RuntimeError("项目内找不到 .agents/skills/*/ai-film-router/scripts/artifact_guard.py")
    r = subprocess.run([sys.executable, str(script)] + args, cwd=str(script.parent),
                       capture_output=True, text=True, timeout=60)
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(text.strip()[-800:] or f"artifact_guard 退出 {r.returncode}")
    return text.strip()


def register(project: Path, step_key: str, scene: str = "N001", path: str = None, outcome: str = "passed"):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    rel = path or _new_file(project, step, scene if step["scope"] == "{scene}" else None)
    if not rel:
        raise ValueError("没有发现新的 vNNN 文件；请先让 Agent 写盘")
    p = (project / rel).resolve()
    if not str(p).startswith(str(project.resolve()) + "/") or not p.is_file():
        raise ValueError("文件不在项目内或不存在")
    scope = scene if step["scope"] == "{scene}" else step["scope"]
    deps = _inputs(project, step, scene if step["scope"] == "{scene}" else None)
    if step.get("register") == "conclusion":
        args = ["register-conclusion", "--project", str(project), "--kind", step["kind"],
                "--scope", scope, "--path", rel, "--outcome", outcome]
    else:
        args = ["register-draft", "--project", str(project), "--kind", step["kind"],
                "--scope", scope, "--path", rel]
    for d in deps:
        args += ["--depends-on", d]
    out = _run_guard(project, args)
    return {"ok": True, "path": rel, "output": out}


def confirm(project: Path, step_key: str, scene: str = "N001", path: str = None):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    rel = path
    if not rel:
        a = _latest(project, step, scene if step["scope"] == "{scene}" else None)
        if not a:
            raise ValueError("没有可确认的成果")
        rel = a["path"]
    # 以登记记录自己的 kind/scope/model 为准；阶段默认 scope 可能与历史记录不同
    rec = next((a for a in _all(project) if a.get("path") == rel), None)
    if rec:
        kind = rec.get("kind") or step["kind"]
        scope = rec.get("scope") or (scene if step["scope"] == "{scene}" else step["scope"])
        model = rec.get("model") or ""
    else:
        kind = step["kind"]
        scope = scene if step["scope"] == "{scene}" else step["scope"]
        model = ""
    args = ["confirm", "--project", str(project), "--kind", kind,
            "--scope", scope, "--path", rel]
    if model:
        args += ["--model", model]
    out = _run_guard(project, args)
    return {"ok": True, "path": rel, "output": out, "kind": kind, "scope": scope}


def return_step(project: Path, step_key: str, note: str = "", scene: str = "N001"):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    f = store.state_dir(project) / "flow-returns.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    rec = {"step": step_key, "scene": scene, "note": (note or "").strip(),
           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "record": rec}


def skip_step(project: Path, step_key: str, note: str = ""):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    a = _latest(project, step, None)
    if a:
        raise ValueError("该阶段已有成果，不能跳过")
    f = store.state_dir(project) / "flow-skips.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    rec = {"step": step_key, "note": (note or "可选阶段跳过").strip(),
           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "record": rec}
