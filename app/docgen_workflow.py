#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「工作流程说明」文档（给人读，不参与流程与校验）。

用法：
    python3 app/docgen_workflow.py                     # 输出到标准输出
    python3 app/docgen_workflow.py -o ../devlog/workflow.md
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

PROJECT = Path("/users/huxian/short_v/有谁共鸣2")
STATE = PROJECT / "00-项目管理" / "artifact-state.json"

STAGE_NAMES = {
    "00": "原始资料", "01": "概念锚定", "02": "角色圣经", "03": "世界观",
    "04": "分场大纲", "05": "正式剧本", "06": "剧本压力测试",
    "07": "图像资产", "08": "表演设计", "09": "镜头与视频提示词",
}


def load_registry():
    return json.loads(STATE.read_text(encoding="utf-8"))


def steps():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import flow
    return flow.STEPS


def stage_of(path: str) -> str:
    return path.split("/")[0][:2]


def short(p: str, n: int = 42) -> str:
    name = p.split("/")[-1]
    return name if len(name) <= n else name[: n - 1] + "…"


def build() -> str:
    reg = load_registry()
    arts = reg["artifacts"]
    by_path = {a["path"]: a for a in arts}

    # 反向依赖：谁引用了它
    users = defaultdict(list)
    for a in arts:
        for dep in a.get("depends_on") or []:
            users[dep].append(a["path"])

    out = []
    now = time.strftime("%Y-%m-%d %H:%M")
    out.append("# 导演工作台 · 工作流程说明\n")
    out.append(f"> 生成时间：{now}　·　数据来源：`00-项目管理/artifact-state.json` + 工作台 `app/flow.py`\n")
    out.append("> **这份文件只供人阅读，不参与任何依赖判定与校验。**")
    out.append("> 它不登记为成果、不被任何文件依赖；改了它不会触发任何下游失效。")
    out.append("> 内容随时可从登记表重新生成，命令见文末。\n")

    out.append("## 一、全流程与技能\n")
    out.append("| 阶段 | 卡片 | skill | 产出文件 | 上游依赖 |")
    out.append("|---|---|---|---|---|")
    for s in steps():
        out.append(f"| {s['no']} | {s['name']} | `{s['skill']}` | `{s['output']}` | {', '.join(s['deps']) or '—'} |")
    out.append("")

    out.append("## 二、实际登记的成果\n")
    grouped = defaultdict(list)
    for a in arts:
        grouped[stage_of(a["path"])].append(a)
    for st in sorted(grouped):
        out.append(f"### {st} {STAGE_NAMES.get(st, '')}（{len(grouped[st])} 项）\n")
        out.append("| 状态 | 文件 | 依赖数 | 下游数 |")
        out.append("|---|---|---|---|")
        for a in sorted(grouped[st], key=lambda x: x["path"]):
            deps = len(a.get("depends_on") or [])
            down = len(users.get(a["path"], []))
            mark = "" if down else " ⚠️"
            out.append(f"| {a['status']} | `{short(a['path'])}` | {deps} | {down}{mark} |")
        out.append("")

    out.append("## 三、依赖链（按实际登记）\n")
    out.append("```text")
    for a in sorted(arts, key=lambda x: x["path"]):
        deps = a.get("depends_on") or []
        if deps:
            out.append(f"{short(a['path'])}")
            for d in deps:
                out.append(f"    ← {short(d)}")
    out.append("```\n")

    out.append("## 四、需要注意的结构问题\n")
    def is_terminal(a):
        # 视频提示词是生成台的输入，不被其他成果引用；它是链路终点
        return "视频提示词" in (a.get("kind") or "") or "ref2va" in a["path"]
    terminal = [a for a in arts if not users.get(a["path"]) and a["status"] == "已确认-当前" and is_terminal(a)]
    dead = [a for a in arts if not users.get(a["path"]) and a["status"] == "已确认-当前" and not is_terminal(a)]
    out.append(f"**终点产物（正常，本来就没有下游）：{len(terminal)} 项**\n")
    for a in terminal:
        out.append(f"- `{short(a['path'], 60)}`")
    out.append("")
    out.append(f"**断头路（已确认但没有下游，可能是缺陷）：{len(dead)} 项**\n")
    for a in dead:
        out.append(f"- `{short(a['path'], 60)}`")
    out.append("")
    fam = defaultdict(list)
    for a in arts:
        key = Path(a["path"]).name.split("-v")[0]
        fam[(stage_of(a["path"]), key)].append(a)
    multi = {k: v for k, v in fam.items() if len(v) > 1}
    if multi:
        out.append(f"**存在多版本的文件族（{len(multi)} 组）**\n")
        for (st, key), items in sorted(multi.items()):
            cur = [a for a in items if a["status"] == "已确认-当前"]
            note = ""
            if len(cur) > 1:
                note = f"　⚠️ {len(cur)} 个同时标为当前"
            cur_name = short(cur[0]["path"], 30) if cur else "无"
            out.append(f"- {st} `{key}`：{len(items)} 个版本，当前 {cur_name}{note}")
        out.append("")

    out.append("## 四bis、流程定义与实际登记的差异\n")
    out.append("流程卡要求的上游阶段，与登记表里实际写的依赖不一致时列在这里。\n")
    out.append("| 文件 | 流程卡要求 | 实际登记缺 |")
    out.append("|---|---|---|")
    mism = 0
    for s in steps():
        want = {d[:2] for d in s["deps"]}          # 步骤 key（09a/05b）归到阶段号
        pat = s["pattern"]
        import re as _re
        for a in arts:
            if not _re.search(pat, a["path"]):
                continue
            have = {stage_of(d) for d in (a.get("depends_on") or [])}
            missing = {d for d in want if d not in have}
            if missing:
                mism += 1
                out.append(f"| `{short(a['path'], 44)}` | {', '.join(sorted(want))} | {', '.join(sorted(missing))} |")
    if not mism:
        out.append("| 无 | — | — |")
    out.append("")

    out.append("## 五、改一个文件会连带失效多少\n")
    out.append("| 文件 | 直接下游 | 说明 |")
    out.append("|---|---|---|")
    for a in sorted(arts, key=lambda x: -len(users.get(x["path"], []))):
        n = len(users.get(a["path"], []))
        if n >= 2:
            out.append(f"| `{short(a['path'], 46)}` | {n} | {a['status']} |")
    out.append("")

    out.append("## 六、运行期产物（不属于创作成果）\n")
    out.append("```text")
    out.append("制作运行/")
    out.append("  ├─ approvals.jsonl          审批账本（工作台「确认」写这里）")
    out.append("  ├─ decisions.jsonl          旧版审核决策（EP001-SCxxx 编号）")
    out.append("  ├─ 确认状态.json             本地确认（生成门禁用）")
    out.append("  ├─ generation-batches/      每次提交的批次记录")
    out.append("  ├─ 接续帧/                   接上镜时抽取的上一镜末帧")
    out.append("  ├─ 首帧候选/                 首帧生成结果")
    out.append("  └─ agent-tasks/             派给 Agent 的任务单")
    out.append("")
    out.append("09-镜头与视频提示词/生成结果/<场次>/<单元>/*.mp4   成片")
    out.append("```\n")

    out.append("## 七、如何保持这份文件是新的\n")
    out.append("```bash")
    out.append("cd ~/gen_v/Shotcall")
    out.append("python3 app/docgen_workflow.py -o devlog/workflow.md")
    out.append("```\n")
    out.append("它只读登记表和工作台的流程定义，不写项目文件。改动流程或登记成果后重跑一次即可。")
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
