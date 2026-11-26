#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「工作流程说明」文档：只讲流程，不涉及任何具体项目的进度。

用法：
    python3 app/docgen_workflow.py -o docs/workflow.md
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

STAGE_DESC = {
    "01": ("全项目", "主题、类型、基调、篇幅、核心冲突"),
    "02": ("全项目", "人物身份、年龄、体态、关系、说话方式"),
    "03": ("全项目", "时代、地域、制度、行业口径（可跳过）"),
    "04": ("全项目", "分场表：场号、地点、时间、事件、预算"),
    "05": ("单集", "可拍摄剧本、成稿时长复核、小说改编对账、生产拆解（同一集同一版本）"),
    "06": ("单集", "结构、节奏、逻辑压力测试（可跳过）"),
    "07": ("全项目", "角色/场景/道具的资产清单与图片提示词"),
    "08": ("单场", "逐场表演设计：目标、障碍、节拍、视线"),
    "09": ("单场", "镜头规划（镜表/时长/空间/连续性/生成单元）与视频提示词"),
}

STAGE_CRAFT = {
    "08": "`performance-direction`（表演方法论）",
    "09": "`minimax-h3-prompt-writing`（官方 H3 写法）、`cinematography`（镜头语言）、`lighting-direction`（光线）",
}

EXTRA_09 = [
    ("09-…/镜头规划/<场次>/scene-registry.md", "场次登记（每场一份，不升版本）"),
    ("09-…/镜头规划/<场次>/scene-timing-vNNN.md", "逐镜时长表"),
    ("09-…/镜头规划/<场次>/scene-space-map-vNNN.md", "空间关系与走位"),
    ("09-…/镜头规划/<场次>/director-plan-vNNN.md", "导演意图与取舍"),
    ("09-…/镜头规划/<场次>/reference-map-vNNN.md", "哪张参考图对应哪一镜"),
    ("09-…/镜头规划/<场次>/continuity-ledger-vNNN.md", "连续性台账：服装、光向、持物"),
    ("09-…/镜头规划/<场次>/visual-continuity-audit-vNNN.md", "视觉连贯自查"),
    ("09-…/镜头规划/<场次>/generation-units-minimax-h3-vNNN.md", "生成单元规划：哪几镜合成一次生成"),
]



OUTPUT_DESC = {
    ("01", "主产出"): "故事核心：主题、类型、基调、篇幅、核心冲突与结局方向",
    ("02", "主产出"): "人物身份、外形、年龄、关系、说话方式与人物弧线",
    ("03", "主产出"): "时代、地域、制度、行业与生活口径（可跳过）",
    ("04", "主产出"): "分场表：场号、地点、时间、事件顺序与时长预算",
    ("05", "主产出"): "可拍摄剧本：场次、动作、对白；事件与台词的唯一权威",
    ("05", "时长复核"): "逐场时长核算，与 04 预算对照，判断本批是否超时",
    ("05", "小说对账"): "原著章节锚点与剧本改动逐段对照：保留了什么、为什么改",
    ("05", "生产拆解"): "逐场制片信息：角色服装、空间道具、声音、动作风险与时长索引",
    ("06", "主产出"): "结构、节奏、逻辑与人物动机的压力测试结论",
    ("07", "主产出"): "角色/场景/道具的资产清单、图片提示词与连续性交接",
    ("08", "主产出"): "逐场表演设计：目标、障碍、动作动词、节拍与视线",
    ("09", "主产出"): "镜头表：景别、机位、构图、动作、台词与时长",
    ("09", "场景登记"): "本场次镜头清单的索引与状态，每场一份",
    ("09", "时长表"): "逐镜时长表，作为生成单元切分的依据",
    ("09", "空间图"): "场景地标、轴线、走位与摄影机一侧的空间关系",
    ("09", "导演方案"): "导演意图与取舍：为什么这样拍",
    ("09", "参考图映射"): "哪张参考图对应哪一镜、承担什么职责",
    ("09", "连续性台账"): "服装、光向、持物、伤势等跨镜连续性记录",
    ("09", "视觉连贯审查"): "轴线、视线、动作接力的自查与结论",
    ("09", "生成单元规划"): "哪几镜合成一次生成、每条多长、起止状态与参考图职责",
    ("09", "视频提示词"): "可直接投产的六段式提示词，末尾附 shotcall-units 数据块",
}

def craft_skills():
    """读 skills-deps/meta/skills.map.json，拿到每个技能的上游仓库与固定 commit。"""
    import json
    f = Path(__file__).resolve().parent.parent / "skills-deps/meta/skills.map.json"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
        return [(k, v.get("repo", ""), str(v.get("commit", ""))[:12]) for k, v in m["skills"].items()]
    except Exception:
        return []


def steps():
    sys.path.insert(0, str(Path(__file__).parent))
    import flow
    return flow.STEPS


def build() -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    out = []
    out.append("# 导演工作台 · 工作流程说明\n")
    out.append(f"> 生成时间：{now}　·　数据来源：工作台流程定义 `app/flow.py`\n")
    out.append("> 本文只说明**流程本身**：有哪些阶段、每步用什么技能、产出什么文件、谁依赖谁。")
    out.append("> 流程卡只负责给输入；产物清单、命名与验收标准由对应 skill 决定。")
    out.append("> 它不记录任何具体项目的进度、状态或版本，也不参与依赖判定与校验。\n")

    out.append("## 〇、卡片与技能的分工\n")
    out.append("流程卡里写的是**只有这个项目知道的事**：项目根、场次、要读哪些上游文件、边界（只做本阶段、不自行确认、不调生成接口）。")
    out.append("至于“这一步产出哪些文件、怎么命名、版本怎么递增、验收看什么”，全部以对应 skill 为准，卡片不复述。")
    out.append("这样技能更新时流程自动跟上，不会出现卡片和技能各说一套。\n")

    out.append("## 〇·二、技能分层与版本\n")
    out.append("技能分两层，职责不重叠：\n")
    out.append("| 层 | 位置 | 管什么 | 会不会随上游变 |")
    out.append("|---|---|---|---|")
    out.append("| 流程层 | 项目内 `.agents/skills/`（如 `film-cinedance`） | 档位、门禁、依赖链、生成单元、时长预算、交付契约 | 由本项目维护 |")
    out.append("| craft 层 | 项目内 `.agents/skills-upstream/` | 构图、运镜、光线、表演、H3 写法 | 固定上游 commit，手动升级 |")
    out.append("")
    out.append("流程层决定「交付什么、合不合规」，craft 层决定「写得好不好」；craft 层不产出文件。\n")
    out.append("### craft 技能从哪来\n")
    out.append("| 技能 | 上游仓库 | 固定 commit |")
    out.append("|---|---|---|")
    craft = craft_skills()
    if craft:
        for name, repo, sha in craft:
            short = repo.replace("https://github.com/", "")
            out.append(f"| `{name}` | {short} | `{sha}` |")
    else:
        out.append("| （未找到 skills-deps/meta/skills.map.json） | — | — |")
    out.append("")
    out.append("指针与校验放在工作台仓库 `skills-deps/`：`meta/skills.map.json` 记录仓库+commit+文件 sha1，")
    out.append("`vendor/` 是三个上游仓库的 submodule，`scripts/verify.sh` 逐字节比对项目内副本。\n")
    out.append("### 项目实测约束放哪\n")
    out.append("上游技能不知道本机的帧数规则、数据块契约和音色挂法，这些写在项目自己的")
    out.append("`00-项目管理/LOCAL-FACTS.md`，与上游同步互不覆盖。尚未验证的现象不写进规则，只列在「待观察」。\n")
    out.append("### 升级方式\n")
    out.append("```text")
    out.append("改 skills.map.json 的 commit → 更新 submodule 指针 → verify.sh 校验")
    out.append("  → 同步项目副本 → 跑一条测试片确认没退化 → 提交")
    out.append("```\n")
    out.append("不跟随上游 main：技能一改，出片行为就变。\n")

    out.append("## 一、阶段总览\n")
    out.append("| 阶段 | 卡片 | 流程技能 | 附带 craft 技能 | 范围 | 这一阶段决定什么 |")
    out.append("|---|---|---|---|---|---|")
    for s in steps():
        scope, desc = STAGE_DESC.get(s["key"], (s["scope"], ""))
        craft = STAGE_CRAFT.get(s["key"], "—")
        out.append(f"| {s['no']} | {s['name']} | `{s['skill']}` | {craft} | {scope} | {desc} |")
    out.append("")

    out.append("## 二、每步产出什么文件\n")
    out.append("| 阶段 | 产出路径（`{v}` 为三位版本号） | 这个文件是什么 | 上游 |")
    out.append("|---|---|---|---|")
    for s in steps():
        deps = "、".join(s["deps"]) or "—"
        out.append(f"| {s['no']} | `{s['output']}` | {OUTPUT_DESC.get((s['key'], '主产出'), '')} | {deps} |")
        for x in (s.get("extra") or []):
            desc = OUTPUT_DESC.get((s['key'], x['label']), '')
            out.append(f"| {s['no']} | `{x['output']}`（{x['label']}） | {desc} | 同上 |")
    out.append("")

    out.append("> 产出路径按文件性质归档，不代表由该阶段的卡片生成。")
    out.append("> 05 与 06 按集管理：路径里的 `{episode}` 是集号（E001），一集一组文件；切集时 05/06 卡片随之切换。\n")

    out.append("### 09 镜头规划下的配套文件\n")
    out.append("视频提示词按官方 H3 六段式（`subject_definitions` → `summary` → `retention_analysis` → `detailed_description`")
    out.append(" → `overall_soundscape` → `non_diegetic_music`），末尾附 shotcall-units 数据块；")
    out.append("这些文件由 `film-cinedance` 技能定义（“拍法、镜头与生成单元确认门”三个内部门禁），")
    out.append("流程卡不逐一列举，只保证它们能进入登记与确认流程。\n")
    out.append("| 文件 | 作用 |")
    out.append("|---|---|")
    for path, why in EXTRA_09:
        out.append(f"| `{path}` | {why} |")
    out.append("")

    out.append("## 三、依赖规则\n")
    out.append("```text")
    out.append("00 原始资料（小说/原稿，外部提供）")
    out.append("  ↓")
    out.append("01 概念锚定 → 02 角色圣经 → 03 世界观（可选）→ 04 分场大纲")
    out.append("  ↓")
    out.append("05 正式剧本 ──┬→ 05b 时长复核")
    out.append("              ├→ 06 压力测试（可选）")
    out.append("              ├→ 07 图像资产（含音色参考）")
    out.append("              └→ 08 表演设计（逐场）")
    out.append("                       ↓")
    out.append("                  09 镜头规划（含生成单元规划）→ 视频提示词")
    out.append("                       ↓")
    out.append("                  生成台 → ComfyUI → 成片")
    out.append("```\n")

    out.append("三条硬规则（由项目校验脚本执行）：\n")
    out.append("1. **生成单元**必须直接依赖同场镜头清单。")
    out.append("2. **视频提示词**必须依赖同场、同模型的生成单元；ref2va 类允许直接依赖镜头表。")
    out.append("3. **镜头表、导演方案、时长表**必须有已登记的权威剧本或作者原稿作为依据。\n")

    out.append("## 四、版本与登记\n")
    out.append("- 每次改动都**新增版本**（`-v001`、`-v002`…），不覆盖旧版。")
    out.append("- 一个文件被登记后才有 sha256；文件内容一变，哈希就对不上，系统会标它“已改动”。")
    out.append("- 上游文件被替换或改动，下游会标“依赖失效”，需要重新确认。")
    out.append("- 同一族文件（同一个文件名前缀）在同一时间只应有一个「当前版」。\n")

    out.append("## 五、确认与生成门禁\n")
    out.append("| 环节 | 谁把关 | 结果 |")
    out.append("|---|---|---|")
    out.append("| 文件确认 | 导演在工作台点「确认」 | 写入审批账本，绑定文件 sha |")
    out.append("| 提交生成 | 生成台检查提示词是否已确认 | 未确认或 sha 已变 → 拒绝提交 |")
    out.append("| 出片入库 | 任务完成后落到成片目录 | 记录耗时、排队、工作流、参考图 |")
    out.append("")

    out.append("## 六、运行期产物（不是创作成果）\n")
    out.append("```text")
    out.append("制作运行/")
    out.append("  ├─ approvals.jsonl          审批账本（工作台「确认」写这里）")
    out.append("  ├─ 确认状态.json             本地确认状态（生成门禁用）")
    out.append("  ├─ generation-batches/      每次提交的批次记录（参数/参考图 sha/任务 id）")
    out.append("  ├─ 接续帧/                   接上镜时抽取的上一镜末帧")
    out.append("  ├─ 首帧候选/                 首帧生成结果")
    out.append("  └─ agent-tasks/             派给 Agent 的任务单")
    out.append("")
    out.append("09-镜头与视频提示词/生成结果/<场次>/<单元>/*.mp4   成片")
    out.append("```\n")

    out.append("## 七、如何更新本文\n")
    out.append("```bash")
    out.append("cd ~/gen_v/Shotcall")
    out.append("python3 app/docgen_workflow.py -o docs/workflow.md   # 重生成本文")
    out.append("./skills-deps/scripts/verify.sh                   # 校验技能副本与上游一致")
    out.append("```\n")
    out.append("流程卡（阶段、技能、产出路径、上游依赖）改了就重跑一次；")
    out.append("它只读 `app/flow.py`，不读任何项目数据。")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    text = build()
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写入 {p}（{len(text)} 字符）")
    else:
        print(text)


if __name__ == "__main__":
    main()
