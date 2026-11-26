# -*- coding: utf-8 -*-
"""导演台流程页：01-09 阶段卡、提示词、登记/确认（调用项目 artifact_guard.py）。

只读写项目登记与文件；不生成图片/视频。技能名对应项目 .agents/skills/ 下的
ai-film-router + film-*；提示词里的 {{...}} 由 render_prompt 替换。
"""
from __future__ import annotations

import hashlib
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

本阶段要产出：正式剧本、成稿时长复核、小说改编对账、生产拆解，四者同一版本。文件名、命名与验收按 film-screenplay 执行。
边界：只做本阶段，不改其他阶段与登记表，不自行确认，不调用生成接口。
完成后只汇报：两份文件的版本与路径、时长偏差、需要导演拍板的点（≤3 条）。
"""


def _prompt_09():
    return """你是本项目执行 Agent。读项目内 .agents/skills 下的 ai-film-router，只执行 09 镜头与视频提示词；加载 film-cinedance。

再按需加载（都在项目内）：
- .agents/skills-upstream/minimax-h3-prompt-writing  → H3 官方写法（六段式/字段/段序）
- cinematography（构图、镜头语言、运镜）
- lighting-direction（光线、光比、色温）
- 本项目实测约束：00-项目管理/LOCAL-FACTS.md（帧数 17n+5、shotcall-units 块、音色挂载）

项目根：{{PROJECT}}
场次：{{SCOPE}}
输入：
{{INPUTS}}

按 film-cinedance 的四道门依次执行，顺序不得调换：
1. 档位与增强门；
2. 拍法与镜头门：场景登记、时长表、镜头表、空间图、连续性台账、导演方案（启用导演顾问时）、生成单元规划；
3. 素材职责与质检门：参考图映射、视觉连贯审查；
4. 模型提示词门：基于已确认的镜头表与生成单元，写 MiniMax H3 视频提示词。
每道门的产出、文件命名与验收都以 film-cinedance 为准；第 3 道没走完不得进入第 4 道。

本项目追加两条：
- 提示词里的单元数、时长、镜号必须与最新 generation-units 规划一致，要偏离先改规划并写明理由。
- 提示词文件末尾必须有 ```shotcall-units JSON 块（单元 id、时长、镜号、参考图、音色、接续）；生成台靠它读单元，缺了就读不到。
- chainFrom 默认填 null（各单元独立生成）；只有已确认的生成单元规划明确要求依赖前置单元结果时才填。
  不得因编号连续、剧情连续或共用参考图就自动串联。
- 无对白单元（规划写「无新对白/净对白 0 字」）必须在 overall_soundscape 写 "No spoken words"，
  且不得再出现 voices；否则 H3 会自行发明人声（常是听不懂的语言）。

边界：只做本阶段，不自行确认，不调用生成接口。
完成后只汇报：产出的文件清单（含版本）、单元清单（时长/帧数）、需要导演拍板的点（≤3 条）。
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
         kind="05 剧本深度融合", scope="{episode}",
         pattern=r"^05-正式剧本/(E\d{3})/screenplay-v(\d{3})(?:-\d+)?\.md$",
         output="05-正式剧本/{episode}/screenplay-{v}.md", deps=["04", "02", "03"],
         extra=[dict(label="时长复核", kind="05 成稿时长复核",
                     pattern=r"^05-正式剧本/(E\d{3})/screenplay-timing-audit-v(\d{3})(?:-\d+)?\.md$",
                     output="05-正式剧本/{episode}/screenplay-timing-audit-{v}.md"),
                dict(label="小说对账", kind="05 小说改编对账", deps=["primary"],
                     pattern=r"^05-正式剧本/(E\d{3})/novel-adaptation-audit-v(\d{3})(?:-\d+)?\.md$",
                     output="05-正式剧本/{episode}/novel-adaptation-audit-{v}.md"),
                dict(label="生产拆解", kind="05 生产拆解（07入口派生）", deps=["primary"],
                     pattern=r"^05-正式剧本/(E\d{3})/production-breakdown-v(\d{3})(?:-\d+)?\.md$",
                     output="05-正式剧本/{episode}/production-breakdown-{v}.md")],
         prompt=_prompt_05()),
    dict(key="06", no="06", stage="06", name="剧本压力测试（可选）", skill="film-stress-test",
         kind="06 剧本压力测试", scope="{episode}", register="conclusion",
         pattern=r"^06-剧本压力测试/(E\d{3})/stress-test-report-v(\d{3})(?:-\d+)?\.md$",
         output="06-剧本压力测试/{episode}/stress-test-report-{v}.md", deps=["05"],
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
         prompt=_prompt("08", "ACTING 表演", "film-acting", ["当前场 05；07 资产"], "08-表演设计/{scene}-acting-vNNN.md", "{scene}")
         + "\n另：表演方法论读 .agents/skills-upstream/performance-direction；本卡只管项目侧取舍与交付格式。\n"),
    dict(key="09", no="09", stage="09", name="镜头与视频提示词", skill="film-cinedance",
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
             dict(label="生成单元规划", kind="09 generation-units-minimax-h3", deps=["primary"],
                  pattern=r"^09-镜头与视频提示词/镜头规划/(N\d{3})/generation-units-minimax-h3-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/镜头规划/{scene}/generation-units-minimax-h3-{v}.md"),
             dict(label="视频提示词", kind="09 视频提示词", deps=["units", "primary"],
                  pattern=r"^09-镜头与视频提示词/视频提示词/(N\d{3})/minimax-h3(?:-ref2va)?-v(\d{3})(?:-\d+)?\.md$",
                  output="09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-{v}.md"),
         ],
         prompt=_prompt_09()),
]

BY_KEY = {s["key"]: s for s in STEPS}
STATUS = {"已确认-当前": "confirmed", "外部提供-已确认": "confirmed",
          "草稿待确认": "draft", "历史已替代": "superseded",
          "已过期-禁止使用": "superseded",
          "已完成-结论": "conclusion", "已作废（不合格）": "invalid",
          "缺文件-阻断": "missing"}


def episodes_of(project: Path) -> list:
    """从 04 分场大纲的「分集目录」表解析集号列表。"""
    latest = None
    for a in _all(project):
        m = re.search(r"^04-分场大纲/treatment-v(\d{3})", a.get("path", ""))
        if m and (latest is None or int(m.group(1)) > _ver(latest)):
            latest = a["path"]
    if not latest:
        return []
    f = project / latest
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(E\d{3})\s*\|", line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def scene_episode(project: Path, scene: str) -> str | None:
    """场次 N001 属于哪一集。优先读 04 大纲的逐场标题，其次读剧本。"""
    for a in _all(project):
        path = a.get("path", "")
        if not re.search(r"^(04|05)-", path):
            continue
        f = project / path
        if not f.is_file():
            continue
        m = re.search(rf"{re.escape(scene)}\s*[／/]\s*(E\d{{3}})", f.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return None


def _ver(path: str) -> int:
    m = VER.search(path or "")
    return int(m.group(1)) if m else 0


def _match(step, path: str, scene: str = None, which: list = None, episode: str = None):
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
    if step["scope"] == "{episode}":
        ep = episode
        if not ep or m.group(1) != ep:
            return None
        return int(m.group(2)) if has_ver else 0
    return int(m.group(1)) if (m.lastindex or 0) >= 1 else 0   # 全项目阶段：版本号就在第 1 组


_ALL_MEMO = {"key": None, "rows": None}


def _all_key(project: Path):
    """以登记表内容为准：mtime/size 变了，或内容哈希变了，都要重新读。

    注意路径是 00-项目管理/artifact-state.json（store.state_dir 是运行状态目录，
    不是成果登记表），早先这里取错路径导致缓存永远不失效。
    """
    f = project / artifacts.STATE_FILE
    try:
        raw = f.read_bytes()
        return (str(project), len(raw), hashlib.sha1(raw).hexdigest())
    except FileNotFoundError:
        return (str(project), 0, "")


def _all(project: Path):
    key = _all_key(project)
    if _ALL_MEMO["key"] == key and _ALL_MEMO["rows"] is not None:
        return _ALL_MEMO["rows"]
    rows = artifacts.build(project).get("artifacts") or []
    _ALL_MEMO["key"] = key
    _ALL_MEMO["rows"] = rows
    return rows


def _all_drop():
    _ALL_MEMO["key"] = None
    _ALL_MEMO["rows"] = None


def _latest(project: Path, step, scene=None, main_only: bool = True, episode=None):
    rows = []
    for a in _all(project):
        if main_only and not re.search(step["pattern"], a.get("path", "")):
            continue
        ver = _match(step, a.get("path", ""), scene, None, episode)
        if ver is not None:
            rows.append((ver, a.get("mtime") or 0, a))
    if not rows:
        return None
    rows.sort(key=lambda x: (x[0], x[1]))
    return rows[-1][2]


def _new_file(project: Path, step, scene=None, episode=None):
    """目录里存在、但登记表里还没有的 vNNN 文件。"""
    known = {a.get("path") for a in _all(project)}
    best = None
    for a in _all(project):  # 只用于已知集合
        pass
    for p in project.glob("**/*"):
        if not p.is_file() or p.suffix != ".md":
            continue
        rel = str(p.relative_to(project))
        ver = _match(step, rel, scene, None, episode)
        if ver is None or rel in known:
            continue
        if best is None or (ver, p.stat().st_mtime) > best[0]:
            best = ((ver, p.stat().st_mtime), rel)
    return best[1] if best else None


def _new_files(project: Path, step, scene=None, episode=None):
    rows_all = _all(project)
    known = {a.get("path") for a in rows_all}
    pats = [step["pattern"]] + [x["pattern"] for x in (step.get("extra") or [])]
    tops = {}
    def _mver(m, scene_scope, ep=None):
        if not m:
            return None
        if scene_scope:
            if m.group(1) != scene:
                return None
            return int(m.group(2)) if (m.lastindex or 0) >= 2 else 0
        if step["scope"] == "{episode}":
            if not ep or m.group(1) != ep:
                return None
            return int(m.group(2)) if (m.lastindex or 0) >= 2 else 0
        return int(m.group(1)) if (m.lastindex or 0) >= 1 else 0

    for pat in pats:                      # 每个产出各自已登记到第几版
        vs = []
        for a in rows_all:
            mm = re.search(pat, a.get("path", ""))
            v = _mver(mm, step["scope"] == "{scene}", episode)
            if v is not None:
                vs.append(v)
        tops[pat] = max(vs, default=0)
    # 只扫这个阶段自己的目录，避免每次遍历整个项目
    roots = []
    for tpl in [step["output"]] + [x["output"] for x in (step.get("extra") or [])]:
        parts = []
        for seg in tpl.split("/"):
            if "{" in seg:
                break
            parts.append(seg)
        head = "/".join(parts[:-1] if len(parts) > 1 and parts[-1].endswith(".md") else parts)
        if head and (project / head).exists() and head not in roots:
            roots.append(head)
    seen = set()
    files = []
    for head in roots:
        for p in (project / head).rglob("*.md"):
            if p.is_file() and p not in seen:
                seen.add(p); files.append(p)
    rows = []
    for p in files:
        if p.suffix != ".md":
            continue
        rel = str(p.relative_to(project))
        for pat in pats:
            m = re.search(pat, rel)
            ver = _mver(m, step["scope"] == "{scene}", episode)
            if ver is None:
                continue
            if rel in known or ver <= tops[pat]:
                break
            rows.append((ver, p.stat().st_mtime, rel))
            break
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r[2] for r in rows]


def _versions(project: Path, step, scene=None, episode=None):
    rows = []
    for a in _all(project):
        which = []
        ver = _match(step, a.get("path", ""), scene, which, episode)
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


def _max_disk_ver(project: Path, step, scene=None, episode=None) -> int:
    """磁盘上匹配该卡产出模板的最大版本（含未登记文件）。"""
    best = 0
    roots = []
    for tpl in [step["output"]] + [x["output"] for x in (step.get("extra") or [])]:
        parts = []
        for seg in tpl.split("/"):
            if "{" in seg:
                break
            parts.append(seg)
        head = "/".join(parts[:-1] if len(parts) > 1 and parts[-1].endswith(".md") else parts)
        if head and (project / head).exists() and head not in roots:
            roots.append(head)
    for head in roots:
        for f in (project / head).rglob("*.md"):
            rel = str(f.relative_to(project))
            v = _match(step, rel, scene, None, episode)
            if v and v > best:
                best = v
    return best


def _next_ver(project: Path, step, scene=None, episode=None, pattern=None):
    """下一版：只按指定产出的 pattern 计算，避免被同卡的其它文件抬高（如 09 的 v017 提示词）。"""
    pat = pattern or step["pattern"]
    best = 0
    for a in _all(project):
        pth = a.get("path", "")
        if re.search(pat, pth):
            v = _match(step, pth, scene, None, episode)
            best = max(best, v or 0)
    roots = []
    for tpl in [step["output"]] + [x["output"] for x in (step.get("extra") or [])]:
        parts = []
        for seg in tpl.split("/"):
            if "{" in seg:
                break
            parts.append(seg)
        head = "/".join(parts[:-1] if len(parts) > 1 and parts[-1].endswith(".md") else parts)
        if head and (project / head).exists() and head not in roots:
            roots.append(head)
    for head in roots:
        for f in (project / head).rglob("*.md"):
            rel = str(f.relative_to(project))
            if not re.search(pat, rel):
                continue
            v = _match(step, rel, scene, None, episode)
            best = max(best, v or 0)
    return best + 1


def _inputs(project: Path, step, scene=None, episode=None):
    out = []
    for key in step.get("deps") or []:
        dep = BY_KEY.get(key)
        if dep:
            dep_ep = None
            if dep["scope"] == "{episode}":
                dep_ep = episode or (scene_episode(project, scene) if scene else None)
            a = _latest(project, dep, scene if dep["scope"] == "{scene}" else None, episode=dep_ep)
            if a:
                out.append(a["path"])
    # 额外输入：同场某类文件的最新一版（如 09b 要读生成单元规划）
    for spec in step.get("extra_inputs") or []:
        pat = spec["pattern"]
        best = None
        for a in _all(project):
            m = re.search(pat, a.get("path", ""))
            if m and (spec.get("scope") != "{scene}" or m.group(1) == scene):
                if best is None or _ver(a["path"]) >= _ver(best):
                    best = a["path"]
        if best is None:
            for f in project.glob(spec.get("glob") or "**/*"):
                rel = str(f.relative_to(project))
                m = re.search(pat, rel)
                if m and (spec.get("scope") != "{scene}" or m.group(1) == scene):
                    if best is None or _ver(rel) >= _ver(best):
                        best = rel
        if best:
            out.append(best)
    return out


def _render(step, project, scene, episode=None):
    if step["scope"] == "{scene}":
        scope = scene
    elif step["scope"] == "{episode}":
        scope = episode or (scene_episode(project, scene) if scene else None) or "E001"
    else:
        scope = "全项目" if step["scope"] == "全项目" else step["scope"]
    latest = _latest(project, step, scene, episode=scope if step["scope"] == "{episode}" else None)
    nv = _next_ver(project, step, scene, scope if step["scope"] == "{episode}" else None)
    out = step["output"].format(scene=scope, episode=scope, v=f"v{nv:03d}")
    inputs = _inputs(project, step, scene, episode=scope if step["scope"] == "{episode}" else None)
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


def _outputs(project: Path, step, scene, episode=None):
    """这一阶段的全部产出：主产出 + 附带产出，各自当前版本与下一版。"""
    rows = []
    items = [("main", step["output"])] + [(x["label"], x["output"]) for x in (step.get("extra") or [])]
    for label, tpl in items:
        latest = None
        for a in _all(project):
            which = []
            v = _match(step, a.get("path", ""), scene, which, episode)
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
                     "nextPath": tpl.format(scene=(scene if step["scope"] == "{scene}" else "全项目"),
                                            episode=(episode or (scene_episode(project, scene) if scene else None) or "E001"),
                                            v=f"v{nv:03d}")})
    return rows


def _stale_index(project: Path) -> dict:
    """登记表里每份成果的过期依赖（含传递），按路径索引。"""
    try:
        import artifacts as _art
        d = _art.build(project)
    except Exception:
        return {}
    return {a.get("path"): (a.get("staleDeps") or []) for a in d.get("artifacts") or []}


def _dep_gate(project: Path, step, scene: str, episode: str = None):
    """返回 (blocked, warnings)：声明的上游一张都没有 → 阻塞；有但还没确认 → 警告。"""
    blocked, warns = [], []
    for k in step.get("deps") or []:
        dep = BY_KEY.get(k)
        if not dep:
            continue
        sc = scene if dep["scope"] == "{scene}" else None
        ep = episode if dep["scope"] == "{episode}" else None
        a = _latest(project, dep, sc, episode=ep)
        if not a:
            blocked.append(f"{k} {dep['name']} 还没有任何成果")
        elif STATUS.get(a.get("statusText")) != "confirmed":
            warns.append(f"{k} {dep['name']} 当前是「{a.get('statusText')}」，尚未确认")
    return blocked, warns


def steps_payload(project: Path, scene: str = "N001", episode: str = None):
    episode = episode or (scene_episode(project, scene) if scene else None)
    out = []
    for step_obj in STEPS:
        s = step_obj
        r = _render(s, project, scene, episode)
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
            "newFiles": _new_files(project, s,
                                    scene if s["scope"] == "{scene}" else None,
                                    episode if s["scope"] == "{episode}" else None),
            "versions": _versions(project, s,
                                   scene if s["scope"] == "{scene}" else None,
                                   episode if s["scope"] == "{episode}" else None),
            "returned": _last_for(project, "flow-returns.jsonl", s["key"]),
            "skipped": _last_for(project, "flow-skips.jsonl", s["key"]),
            "canConfirm": bool(a and STATUS.get(a.get("statusText")) == "draft"),
            "canRegister": True,
            "blocked": _dep_gate(project, s, scene, episode)[0],
            "depWarnings": _dep_gate(project, s, scene, episode)[1],
            "staleDeps": (_stale_index(project).get((a or {}).get("path")) if a else None) or [],
            "outputs": _outputs(project, step_obj, scene, episode),
        })
    # 自身成果的上游过期，也视为没 ready
    for x in out:
        if x["staleDeps"]:
            x["blocked"] = list(x["blocked"]) + [
                "上游已更新：" + "；".join(
                    f"{r['path'].split('/')[-1]}（{r['reason']}）" for r in x["staleDeps"][:3])
            ]
    eps = episodes_of(project)
    cur_ep = episode or (scene_episode(project, scene) if scene else None)
    return {"scene": scene, "episode": cur_ep, "episodes": eps, "steps": out}


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


PROMPT_KIND = "09 视频提示词"
STATUS_ENUM = {"planning", "compiled"}
FORMAT_ENUM = {"MiniMax H3", "Seedance 2.5", "Seedance 2.0"}


def _headings(txt: str):
    """正文里的单元标题：id -> (时长, 镜号)。

    允许小数秒与附加列，例如 `### U001｜8.00秒｜SH001｜192帧`；
    统一按"秒"取整，与数据块里的整数 duration 比较。
    """
    out = {}
    for m in re.finditer(r"^### (U\d{3}(?:-[A-Z])?)｜(\d+(?:\.\d+)?)秒｜([^｜\n]+)(?:｜[^\n]*)?",
                         txt, re.M):
        out[m.group(1)] = (float(m.group(2)), m.group(3).strip())
    return out


def validate_prompt_file(project: Path, rel: str, scene: str) -> list:
    """登记视频提示词前的校验。返回问题清单，空表示通过。"""
    import vjobs
    f = project / rel
    if not f.is_file():
        return ["文件不存在"]
    txt = f.read_text(encoding="utf-8")
    units, err = vjobs.shots_block(txt)
    if err:
        return [err]
    heads = _headings(txt)
    problems = []
    ids = [u.get("id") for u in units]
    if sorted(ids) != sorted(heads.keys()):
        problems.append(f"单元 id 与正文标题不一致：块 {ids} / 标题 {sorted(heads)}")
    if len(set(ids)) != len(ids):
        problems.append("单元 id 有重复")
    for u in units:
        uid = u.get("id")
        h = heads.get(uid)
        if h:
            try:
                ok_dur = abs(float(u.get("duration")) - float(h[0])) <= 0.15   # 一帧≈0.042s，允许取整误差
            except Exception:
                ok_dur = False
            if not ok_dur:
                problems.append(f"{uid}: duration={u.get('duration')} 与标题 {h[0]} 秒不一致")
            if [str(x) for x in (u.get("shotIds") or [])] != [h[1]]:
                problems.append(f"{uid}: shotIds={u.get('shotIds')} 与标题镜号 {h[1]} 不一致")
        for key in ("referenceImages", "referenceAudios"):
            for pth in u.get(key) or []:
                if not (project / pth).exists():
                    problems.append(f"{uid}: {key} 文件不存在 {pth}")
        # 无对白单元必须显式静音：H3 是音视频联合生成，不写就会自由发声（常出现听不懂的人声）
        body = (u.get("prompt") or "")
        has_dialogue = bool(re.search(r"[\u4e00-\u9fa5]{2,}[。！？]", body))
        has_silence = bool(re.search(r"no spoken words|no dialogue|silent throughout|without spoken words",
                                     body, re.I))
        if not has_dialogue and not has_silence:
            problems.append(f"{uid}: 无对白单元必须写明确静音约束（no spoken words），否则模型会自行发明人声")
        if re.search(r"\bvoices?\b", body, re.I) and has_silence:
            problems.append(f"{uid}: 已声明静音却又出现 voices，语义冲突")
        if u.get("promptStatus") not in STATUS_ENUM:
            problems.append(f"{uid}: promptStatus 非法 {u.get('promptStatus')}")
        if u.get("promptFormat") not in FORMAT_ENUM:
            problems.append(f"{uid}: promptFormat 非法 {u.get('promptFormat')}")
        if u.get("control") and u.get("promptStatus") == "compiled":
            problems.append(f"{uid}: 实验单元不能是 compiled")
        cf = u.get("chainFrom")
        if cf and (cf not in heads or cf == uid):
            problems.append(f"{uid}: chainFrom 指向不存在的单元 {cf}")
    # 模型与来源一致
    for key in ("modelId", "promptFormat"):
        vals = {u.get(key) for u in units}
        if len(vals) > 1:
            problems.append(f"各单元 {key} 不一致：{vals}")
    src = units[0].get("sourceShotPlan")
    su = units[0].get("sourceGenerationUnits")
    for label, path in (("sourceShotPlan", src), ("sourceGenerationUnits", su)):
        if not path:
            problems.append(f"缺少 {label}")
            continue
        full = project / path
        if not full.exists():
            problems.append(f"{label} 指向的文件不存在：{path}")
            continue
        from_ver = _ver(path)
        if from_ver:
            fam = re.sub(r"-v\d{3}(?:-\d+)?\.md$", "", path)
            newer = [a["path"] for a in _all(project)
                     if re.sub(r"-v\d{3}(?:-\d+)?\.md$", "", a.get("path", "")) == fam
                     and _ver(a.get("path", "")) > from_ver]
            if newer:
                problems.append(f"{label} 指向旧版本 {path.split('/')[-1]}（存在更新版 {newer[0].split('/')[-1]}）")
    return problems


def _kind_for(step, rel: str) -> str:
    """同一张卡的多个产出共用 kind 会互相顶掉当前版，这里按路径取各自 kind。"""
    for x in (step.get("extra") or []):
        if re.search(x["pattern"], rel or ""):
            return x.get("kind") or step["kind"]
    return step["kind"]


def _extra_deps(step, rel: str, project: Path, scene: str) -> list:
    """附带产出自己的依赖：如生成单元必须依赖同场镜头表。"""
    for x in (step.get("extra") or []):
        if not re.search(x["pattern"], rel or ""):
            continue
        need = x.get("deps") or []
        out = []
        for spec in need:
            if spec == "primary":
                a = _latest(project, step, scene if step["scope"] == "{scene}" else None)
                if a:
                    out.append(a["path"])
            elif spec == "units":
                # 提示词必须直接依赖同场、同模型的生成单元
                units_def = next((x for x in (step.get("extra") or [])
                                  if (x.get("label") or "").startswith("生成单元")), None)
                best = None
                if units_def:
                    for a in _all(project):
                        mm = re.search(units_def["pattern"], a.get("path", "") or "")
                        if not mm:
                            continue
                        v = int(mm.group(2))
                        if best is None or v > best[0]:
                            best = (v, a["path"])
                if best:
                    out.append(best[1])
            elif spec == "acting":
                a = _latest(project, BY_KEY["08"], scene)
                if a:
                    out.append(a["path"])
        return out
    return []


def register(project: Path, step_key: str, scene: str = "N001", path: str = None, outcome: str = "passed", episode: str = None):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    ep = episode or (scene_episode(project, scene) if scene else None)
    rel = path or _new_file(project, step, scene if step["scope"] == "{scene}" else None, ep)
    if not rel:
        raise ValueError("没有发现新的 vNNN 文件；请先让 Agent 写盘")
    p = (project / rel).resolve()
    if not str(p).startswith(str(project.resolve()) + "/") or not p.is_file():
        raise ValueError("文件不在项目内或不存在")
    if step["scope"] == "{scene}":
        scope = scene
    elif step["scope"] == "{episode}":
        scope = ep or scene_episode(project, scene) or "E001"
    else:
        scope = step["scope"]
    deps = _inputs(project, step, scene if step["scope"] == "{scene}" else None,
                   episode=scope if step["scope"] == "{episode}" else None)
    deps += _extra_deps(step, rel, project, scene)
    deps = list(dict.fromkeys(deps))
    kind = _kind_for(step, rel)
    if kind == PROMPT_KIND:
        problems = validate_prompt_file(project, rel, scene)
        if problems:
            raise ValueError("提示词文件校验未通过：" + "；".join(problems[:6]))
    if step.get("register") == "conclusion":
        args = ["register-conclusion", "--project", str(project), "--kind", kind,
                "--scope", scope, "--path", rel, "--outcome", outcome]
    else:
        args = ["register-draft", "--project", str(project), "--kind", kind,
                "--scope", scope, "--path", rel]
    for d in deps:
        args += ["--depends-on", d]
    out = _run_guard(project, args)
    _all_drop()
    return {"ok": True, "path": rel, "output": out}


def _family_latest(project: Path, dep_path: str) -> str:
    """同一家族里版本号最大的已登记文件（没有则返回原路径）。"""
    import artifacts as _art
    fam, ver = _art.family_of(dep_path)
    best, best_ver = dep_path, ver
    for a in _all(project):
        f2, v2 = _art.family_of(a.get("path", ""))
        if f2 == fam and v2 > best_ver:
            best, best_ver = a["path"], v2
    return best


def ack_upstream(project: Path, step_key: str, scene: str = "N001", path: str = None,
                 episode: str = None):
    """认可上游更新：把这份成果的 depends_on 改成上游当前最新版，保持原有确认状态。

    只改登记表里的依赖指向，不动文件内容、不动 sha、不动审批记录。
    """
    import json as _json
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    rel = path
    if not rel:
        a = _latest(project, step, scene if step["scope"] == "{scene}" else None,
                    episode=episode if step["scope"] == "{episode}" else None)
        if not a:
            raise ValueError("没有已登记的成果可认可")
        rel = a["path"]

    f = project / artifacts.STATE_FILE
    if not f.exists():
        raise ValueError("登记表不存在")
    d = _json.loads(f.read_text(encoding="utf-8"))
    rec = next((a for a in d.get("artifacts") or [] if a.get("path") == rel), None)
    if not rec:
        raise ValueError("这份成果还没登记，不能认可上游")

    before = list(rec.get("depends_on") or [])
    after, changes = [], []
    for dep in before:
        new = _family_latest(project, dep)
        after.append(new)
        if new != dep:
            changes.append({"from": dep, "to": new})
    if not changes:
        return {"ok": True, "path": rel, "changes": [], "note": "依赖已经是最新，无需改动"}

    rec["depends_on"] = list(dict.fromkeys(after))
    rec["ackedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    f.write_text(_json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    log = store.state_dir(project) / "upstream-acks.jsonl"
    with open(log, "a", encoding="utf-8") as out:
        out.write(_json.dumps({"at": rec["ackedAt"], "path": rel, "changes": changes},
                              ensure_ascii=False) + "\n")
    _all_drop()
    return {"ok": True, "path": rel, "changes": changes}


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


def confirm(project: Path, step_key: str, scene: str = "N001", path: str = None, episode: str = None):
    step = BY_KEY.get(step_key)
    if not step:
        raise ValueError("未知流程")
    if step.get("register") == "conclusion":
        return {"ok": True, "skipped": True,
                "note": "结论类成果登记即生效（已完成-结论），不需要确认"}
    rel = path
    ep = episode or (scene_episode(project, scene) if scene else None)
    if not rel:
        a = _latest(project, step, scene if step["scope"] == "{scene}" else None,
                    episode=ep if step["scope"] == "{episode}" else None)
        if not a:
            raise ValueError("没有可确认的成果")
        rel = a["path"]
    rec = next((a for a in _all(project) if a.get("path") == rel), None)
    if rec:
        return confirm_path(project, rel)
    if step["scope"] == "{scene}":
        scope = scene
    elif step["scope"] == "{episode}":
        scope = ep or scene_episode(project, scene) or "E001"
    else:
        scope = step["scope"]
    out = _run_guard(project, ["confirm", "--project", str(project), "--kind", _kind_for(step, rel),
                               "--scope", scope, "--path", rel])
    _all_drop()
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
