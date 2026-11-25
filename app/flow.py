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
    return f"""你是本项目执行 Agent。读项目内 .agents/skills 下的 ai-film-router，只执行 {no} {name}；加载 {skill}。

项目根：{{{{PROJECT}}}}
范围：{{{{SCOPE}}}}
输入：
{{{{INPUTS}}}}

这阶段产出哪些文件、怎么命名、验收标准，全按 {skill} 技能执行；输出路径里的版本号按项目现有版本递增。
边界：只做本阶段，不改其他阶段与登记表，不自行确认，不调用生成接口。
完成后只汇报：版本、路径、需要导演拍板的点（≤3 条）。
"""


def _prompt_05():
    return """你是本项目执行 Agent。读项目内 .agents/skills 下的 ai-film-router，只执行 05 正式剧本；加载 film-screenplay。

项目根：{{PROJECT}}
范围：当前批次
输入：
{{INPUTS}}

这一阶段要产出两份文件：正式剧本，以及基于它的成稿时长复核。文件名、命名与验收按 film-screenplay 执行。
边界：只做本阶段，不改其他阶段与登记表，不自行确认，不调用生成接口。
完成后只汇报：两份文件的版本与路径、时长偏差、需要导演拍板的点（≤3 条）。
"""


def _prompt_09a():
    return """你是本项目执行 Agent。读项目内 .agents/skills 下的 ai-film-router，只执行 09a 镜头规划；加载 film-cinedance。

项目根：{{PROJECT}}
场次：{{SCOPE}}
输入：
{{INPUTS}}

产出清单（镜头表、时长表、空间图、连续性、生成单元等）、命名与验收，全按 film-cinedance 的「拍法、镜头与生成单元确认门」执行；台词逐字取自 05 剧本。
边界：只做本阶段，不写视频提示词正文，不自行确认，不调用生成接口。
完成后只汇报：版本、路径、产出的文件清单、需要导演拍板的点（≤3 条）。
"""


def _prompt_09b():
    return """你是本项目执行 Agent。读项目内 .agents/skills 下的 ai-film-router，只执行 09b 视频提示词。
加载：film-cinedance、minimax-h3-video-prompts、performance-direction、lighting-direction。

项目根：{{PROJECT}}
场次：{{SCOPE}}
输入：
{{INPUTS}}

六段式、帧数与时长规则按 minimax-h3-video-prompts；表演按 performance-direction；光线按 lighting-direction；文件命名与验收按 film-cinedance。
本项目追加一条：单元数、时长、镜号必须与最新 generation-units 规划一致，要偏离先改规划并写明理由。
边界：只做本阶段，不自行确认，不调用生成接口。
完成后只汇报：版本、路径、单元清单（时长/帧数）、需要导演拍板的点（≤3 条）。
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
         extra=[dict(label="时长复核", kind="05 成稿时长复核",
                     pattern=r"^05-正式剧本/screenplay-timing-audit-v(\d{3})(?:-\d+)?\.md$",
                     output="05-正式剧本/screenplay-timing-audit-{v}.md")],
         prompt=_prompt_05()),
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
         extra=[
             dict(label="场景登记", kind="09 场景登记",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/scene-registry\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/scene-registry.md"),
             dict(label="时长表", kind="09 时长表",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/scene-timing-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/scene-timing-{v}.md"),
             dict(label="空间图", kind="09 空间图",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/scene-space-map-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/scene-space-map-{v}.md"),
             dict(label="导演方案", kind="09 导演方案",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/director-plan-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/director-plan-{v}.md"),
             dict(label="参考图映射", kind="09 参考图映射",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/reference-map-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/reference-map-{v}.md"),
             dict(label="连续性台账", kind="09 连续性台账",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/continuity-ledger-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/continuity-ledger-{v}.md"),
             dict(label="视觉连贯审查", kind="09 视觉连贯审查",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/visual-continuity-audit-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/visual-continuity-audit-{v}.md"),
             dict(label="生成单元规划", kind="09 generation-units-minimax-h3",
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/generation-units-minimax-h3-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/generation-units-minimax-h3-{v}.md"),
         ],
         prompt=_prompt_09a()),
    dict(key="09b", no="09b", stage="09", name="视频提示词", skill="film-cinedance",
         kind="09 视频提示词", scope="{scene}",
         pattern=r"^09-镜头与视频提示词/视频提示词/(N\d{3})/minimax-h3-ref2va-v(\d{3})(?:-\d+)?\.md$",
         output="09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-ref2va-{v}.md", deps=["09a", "07"],
         prompt=_prompt_09b()),
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


def _match(step, path: str, scene: str = None, which: list = None):
    """匹配主产出或附带产出；which 传入时把命中的类型名追加上去。"""
    pats = [(step["pattern"], "main")] + [(x["pattern"], x["label"]) for x in (step.get("extra") or [])]
    m = None
    for pat, label in pats:
        m = re.search(pat, path)
        if m:
            if which is not None:
                which.append(label)
            break
    if not m:
        return None
    has_ver = m.lastindex and m.lastindex >= 2
    if step["scope"] == "{scene}":
        if not scene or m.group(1) != scene:
            return None
        return int(m.group(2)) if has_ver else 0
    return int(m.group(1)) if has_ver else 0


def _all(project: Path):
    return artifacts.build(project).get("artifacts") or []


def _latest(project: Path, step, scene=None, main_only: bool = True):
    rows = []
    for a in _all(project):
        if main_only and not re.search(step["pattern"], a.get("path", "")):
            continue
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
    pats = [step["pattern"]] + [x["pattern"] for x in (step.get("extra") or [])]
    tops = {}
    def _mver(m, scene_scope):
        if not m:
            return None
        if scene_scope:
            if m.group(1) != scene:
                return None
            return int(m.group(2)) if (m.lastindex or 0) >= 2 else 0
        return int(m.group(1)) if (m.lastindex or 0) >= 1 else 0

    for pat in pats:                      # 每个产出各自已登记到第几版
        vs = []
        for a in rows_all:
            v = _mver(re.search(pat, a.get("path", "")), step["scope"] == "{scene}")
            if v is not None:
                vs.append(v)
        tops[pat] = max(vs, default=0)
    rows = []
    for p in project.glob("**/*"):
        if not p.is_file() or p.suffix != ".md":
            continue
        rel = str(p.relative_to(project))
        for pat in pats:
            m = re.search(pat, rel)
            ver = _mver(m, step["scope"] == "{scene}")
            if ver is None:
                continue
            if rel in known or ver <= tops[pat]:
                break
            rows.append((ver, p.stat().st_mtime, rel))
            break
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r[2] for r in rows]


def _versions(project: Path, step, scene=None):
    rows = []
    for a in _all(project):
        which = []
        ver = _match(step, a.get("path", ""), scene, which)
        if ver is None:
            continue
        rows.append({"path": a["path"], "version": f"v{ver:03d}", "label": which[0] if which else "主产出",
                     "statusText": a.get("statusText"),
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


def _outputs(project: Path, step, scene):
    """这一阶段的全部产出：主产出 + 附带产出，各自当前版本与下一版。"""
    rows = []
    items = [("main", step["output"])] + [(x["label"], x["output"]) for x in (step.get("extra") or [])]
    for label, tpl in items:
        latest = None
        for a in _all(project):
            which = []
            v = _match(step, a.get("path", ""), scene, which)
            if v is None or (which and which[0] != ("main" if label == "main" else label)):
                continue
            if latest is None or v > _ver(latest["path"]):
                latest = a
        disk = None
        if latest is None:
            for f in project.rglob("*"):
                if f.is_file() and re.search((step["pattern"] if label == "main"
                                              else next(x["pattern"] for x in (step.get("extra") or [])
                                                        if x["label"] == label)),
                                             str(f.relative_to(project))):
                    disk = str(f.relative_to(project))
                    break
        nv = (_ver(latest["path"]) + 1) if latest else (_ver(disk) + 1 if disk and _ver(disk) else 1)
        rows.append({"label": ("主产出" if label == "main" else label),
                     "path": latest["path"] if latest else disk,
                     "version": (f"v{_ver(latest['path']):03d}" if latest
                                 else (f"v{_ver(disk):03d}" if disk and _ver(disk) else None)),
                     "statusText": latest.get("statusText") if latest else ("文件已在磁盘，未登记" if disk else None),
                     "nextPath": tpl.format(scene=(scene if step["scope"] == "{scene}" else step["scope"]),
                                            v=f"v{nv:03d}")})
    return rows


def steps_payload(project: Path, scene: str = "N001"):
    out = []
    for step_obj in STEPS:
        s = step_obj
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
            "outputs": _outputs(project, step_obj, scene),
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


def confirm_path(project: Path, path: str):
    """确认任意已登记且 current.draft 指向它的成果；不要求它属于 01–09b 卡。"""
    rel = str(path)
    reg = artifacts.load_registry(project)
    rec = next((a for a in (reg.get("artifacts") or []) if a.get("path") == rel), None)
    if not rec:
        raise ValueError("成果未登记，不能确认")
    key = "::".join([rec.get("kind", ""), rec.get("scope", ""), rec.get("model", "")])
    cur = (reg.get("current") or {}).get(key) or {}
    if cur.get("draft") != rel:
        if cur.get("confirmed") == rel:
            return {"ok": True, "path": rel, "already": True}
        raise ValueError("登记表没有对应待确认草稿")
    args = ["confirm", "--project", str(project), "--kind", rec.get("kind", ""),
            "--scope", rec.get("scope", ""), "--path", rel]
    if rec.get("model"):
        args += ["--model", rec["model"]]
    out = _run_guard(project, args)
    return {"ok": True, "path": rel, "output": out,
            "kind": rec.get("kind"), "scope": rec.get("scope")}


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
    rec = next((a for a in _all(project) if a.get("path") == rel), None)
    if rec:
        return confirm_path(project, rel)
    scope = scene if step["scope"] == "{scene}" else step["scope"]
    out = _run_guard(project, ["confirm", "--project", str(project), "--kind", step["kind"],
                               "--scope", scope, "--path", rel])
    return {"ok": True, "path": rel, "output": out}

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
