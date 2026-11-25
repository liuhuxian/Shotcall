# 导演工作台 · 工作流程说明

> 生成时间：2026-11-25 10:59　·　数据来源：`00-项目管理/artifact-state.json` + 工作台 `app/flow.py`

> **这份文件只供人阅读，不参与任何依赖判定与校验。**
> 它不登记为成果、不被任何文件依赖；改了它不会触发任何下游失效。
> 内容随时可从登记表重新生成，命令见文末。

## 一、全流程与技能

| 阶段 | 卡片 | skill | 产出文件 | 上游依赖 |
|---|---|---|---|---|
| 01 | 概念锚定 | `film-synopsis` | `01-概念锚定/synopsis-{v}.md` | — |
| 02 | 角色圣经 | `film-characters` | `02-角色圣经/characters-{v}.md` | 01 |
| 03 | 世界观（可选） | `film-worldbuilding` | `03-世界观/worldbuilding-{v}.md` | 01, 02 |
| 04 | 分场大纲 | `film-treatment` | `04-分场大纲/treatment-{v}.md` | 01, 02, 03 |
| 05 | 正式剧本 | `film-screenplay` | `05-正式剧本/screenplay-{v}.md` | 04, 02, 03 |
| 05b | 时长复核 | `film-screenplay` | `05-正式剧本/screenplay-timing-audit-{v}.md` | 05 |
| 06 | 剧本压力测试（可选） | `film-stress-test` | `06-剧本压力测试/stress-test-report-{v}.md` | 05 |
| 07 | Lira 图像资产 | `film-lira-assets` | `07-图像资产/assets-{v}.md` | 05 |
| 08 | ACTING 表演 | `film-acting` | `08-表演设计/{scene}-acting-{v}.md` | 05, 07 |
| 09a | 镜头规划 | `film-cinedance` | `09-镜头与视频提示词/镜头规划/{scene}/shot-plan-{v}.md` | 05, 08, 07 |
| 09b | 视频提示词 | `film-cinedance` | `09-镜头与视频提示词/视频提示词/{scene}/minimax-h3-ref2va-{v}.md` | 09a, 07 |

## 二、实际登记的成果

### 00 原始资料（2 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `35d83283-fea3-4b14-9692-e278f5f2e28b-有谁共鸣…` | 0 | 1 |
| 已确认-当前 | `8b90e60d-9ac7-4af0-9376-e9c4b01d7c89-有谁共鸣…` | 0 | 1 |

### 01 概念锚定（1 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `synopsis-v001.md` | 0 | 14 |

### 02 角色圣经（1 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `characters-v001.md` | 1 | 21 |

### 03 世界观（1 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `worldbuilding-v001.md` | 2 | 12 |

### 04 分场大纲（3 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 历史已替代 | `treatment-v001.md` | 3 | 0 ⚠️ |
| 历史已替代 | `treatment-v005.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `treatment-v006.md` | 3 | 9 |

### 05 正式剧本（4 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `novel-adaptation-audit-v001.md` | 7 | 2 |
| 已确认-当前 | `production-breakdown-v001.md` | 5 | 0 ⚠️ |
| 已确认-当前 | `screenplay-timing-audit-v001.md` | 5 | 0 ⚠️ |
| 已确认-当前 | `screenplay-v001.md` | 4 | 31 |

### 06 剧本压力测试（1 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已完成-结论 | `stress-test-report-v001.md` | 5 | 0 ⚠️ |

### 07 图像资产（5 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 历史已替代 | `assets-v001.md` | 5 | 0 ⚠️ |
| 历史已替代 | `assets-v002.md` | 5 | 0 ⚠️ |
| 历史已替代 | `assets-v003.md` | 6 | 0 ⚠️ |
| 已确认-当前 | `assets-v004.md` | 6 | 17 |
| 已确认-当前 | `音色参考-v001.md` | 1 | 6 |

### 08 表演设计（8 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 已确认-当前 | `N001-acting-v001.md` | 3 | 8 |
| 已确认-当前 | `N002-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N003-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N004-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N005-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N006-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N007-acting-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `N008-acting-v001.md` | 3 | 0 ⚠️ |

### 09 镜头与视频提示词（17 项）

| 状态 | 文件 | 依赖数 | 下游数 |
|---|---|---|---|
| 草稿待确认 | `minimax-h3-ref2va-v010.md` | 2 | 0 ⚠️ |
| 草稿待确认 | `minimax-h3-ref2va-v011.md` | 2 | 0 ⚠️ |
| 已确认-当前 | `minimax-h3-ref2va-v012.md` | 2 | 0 ⚠️ |
| 已确认-当前 | `minimax-h3-ref2va-v013.md` | 2 | 0 ⚠️ |
| 已确认-当前 | `minimax-h3-ref2va-v014.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `minimax-h3-ref2va-v015.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `minimax-h3-ref2va-v016.md` | 4 | 0 ⚠️ |
| 已确认-当前 | `continuity-ledger-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `director-plan-v001.md` | 3 | 0 ⚠️ |
| 历史已替代 | `generation-units-minimax-h3-v001.md` | 2 | 0 ⚠️ |
| 已确认-当前 | `generation-units-minimax-h3-v002.md` | 3 | 1 |
| 已确认-当前 | `reference-map-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `scene-space-map-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `scene-timing-v001.md` | 3 | 0 ⚠️ |
| 已确认-当前 | `shot-plan-v001.md` | 3 | 8 |
| 已确认-当前 | `shot-table-from-yoko-shots-v001.md` | 2 | 0 ⚠️ |
| 已确认-当前 | `visual-continuity-audit-v001.md` | 3 | 0 ⚠️ |

## 三、依赖链（按实际登记）

```text
characters-v001.md
    ← synopsis-v001.md
worldbuilding-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
treatment-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
treatment-v005.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
treatment-v006.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
novel-adaptation-audit-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
    ← 8b90e60d-9ac7-4af0-9376-e9c4b01d7c89-有谁共鸣…
    ← 35d83283-fea3-4b14-9692-e278f5f2e28b-有谁共鸣…
production-breakdown-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
screenplay-timing-audit-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
screenplay-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
stress-test-report-v001.md
    ← screenplay-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
assets-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
assets-v002.md
    ← screenplay-v001.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
assets-v003.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
    ← novel-adaptation-audit-v001.md
assets-v004.md
    ← synopsis-v001.md
    ← characters-v001.md
    ← worldbuilding-v001.md
    ← treatment-v006.md
    ← screenplay-v001.md
    ← novel-adaptation-audit-v001.md
音色参考-v001.md
    ← assets-v004.md
N001-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N002-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N003-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N004-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N005-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N006-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N007-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
N008-acting-v001.md
    ← screenplay-v001.md
    ← characters-v001.md
    ← assets-v004.md
minimax-h3-ref2va-v010.md
    ← screenplay-v001.md
    ← 音色参考-v001.md
minimax-h3-ref2va-v011.md
    ← screenplay-v001.md
    ← 音色参考-v001.md
minimax-h3-ref2va-v012.md
    ← shot-plan-v001.md
    ← assets-v004.md
minimax-h3-ref2va-v013.md
    ← shot-plan-v001.md
    ← 音色参考-v001.md
minimax-h3-ref2va-v014.md
    ← screenplay-v001.md
    ← shot-plan-v001.md
    ← 音色参考-v001.md
minimax-h3-ref2va-v015.md
    ← screenplay-v001.md
    ← shot-plan-v001.md
    ← 音色参考-v001.md
minimax-h3-ref2va-v016.md
    ← screenplay-v001.md
    ← shot-plan-v001.md
    ← 音色参考-v001.md
    ← generation-units-minimax-h3-v002.md
continuity-ledger-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
director-plan-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
generation-units-minimax-h3-v001.md
    ← shot-plan-v001.md
    ← screenplay-v001.md
generation-units-minimax-h3-v002.md
    ← shot-plan-v001.md
    ← screenplay-v001.md
    ← N001-acting-v001.md
reference-map-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
scene-space-map-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
scene-timing-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
shot-plan-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
shot-table-from-yoko-shots-v001.md
    ← shot-plan-v001.md
    ← screenplay-v001.md
visual-continuity-audit-v001.md
    ← screenplay-v001.md
    ← assets-v004.md
    ← N001-acting-v001.md
```

## 四、需要注意的结构问题

**终点产物（正常，本来就没有下游）：5 项**

- `minimax-h3-ref2va-v012.md`
- `minimax-h3-ref2va-v013.md`
- `minimax-h3-ref2va-v014.md`
- `minimax-h3-ref2va-v015.md`
- `minimax-h3-ref2va-v016.md`

**断头路（已确认但没有下游，可能是缺陷）：16 项**

- `screenplay-timing-audit-v001.md`
- `production-breakdown-v001.md`
- `N002-acting-v001.md`
- `N003-acting-v001.md`
- `N004-acting-v001.md`
- `N005-acting-v001.md`
- `N006-acting-v001.md`
- `N007-acting-v001.md`
- `N008-acting-v001.md`
- `continuity-ledger-v001.md`
- `director-plan-v001.md`
- `reference-map-v001.md`
- `scene-space-map-v001.md`
- `scene-timing-v001.md`
- `visual-continuity-audit-v001.md`
- `shot-table-from-yoko-shots-v001.md`

**存在多版本的文件族（4 组）**

- 04 `treatment`：3 个版本，当前 treatment-v006.md
- 07 `assets`：4 个版本，当前 assets-v004.md
- 09 `generation-units-minimax-h3`：2 个版本，当前 generation-units-minimax-h3-v…
- 09 `minimax-h3-ref2va`：7 个版本，当前 minimax-h3-ref2va-v012.md　⚠️ 5 个同时标为当前

## 四bis、流程定义与实际登记的差异

流程卡要求的上游阶段，与登记表里实际写的依赖不一致时列在这里。

| 文件 | 流程卡要求 | 实际登记缺 |
|---|---|---|
| `minimax-h3-ref2va-v010.md` | 07, 09 | 09 |
| `minimax-h3-ref2va-v011.md` | 07, 09 | 09 |

## 五、改一个文件会连带失效多少

| 文件 | 直接下游 | 说明 |
|---|---|---|
| `screenplay-v001.md` | 31 | 已确认-当前 |
| `characters-v001.md` | 21 | 已确认-当前 |
| `assets-v004.md` | 17 | 已确认-当前 |
| `synopsis-v001.md` | 14 | 已确认-当前 |
| `worldbuilding-v001.md` | 12 | 已确认-当前 |
| `treatment-v006.md` | 9 | 已确认-当前 |
| `N001-acting-v001.md` | 8 | 已确认-当前 |
| `shot-plan-v001.md` | 8 | 已确认-当前 |
| `音色参考-v001.md` | 6 | 已确认-当前 |
| `novel-adaptation-audit-v001.md` | 2 | 已确认-当前 |

## 六、运行期产物（不属于创作成果）

```text
制作运行/
  ├─ approvals.jsonl          审批账本（工作台「确认」写这里）
  ├─ decisions.jsonl          旧版审核决策（EP001-SCxxx 编号）
  ├─ 确认状态.json             本地确认（生成门禁用）
  ├─ generation-batches/      每次提交的批次记录
  ├─ 接续帧/                   接上镜时抽取的上一镜末帧
  ├─ 首帧候选/                 首帧生成结果
  └─ agent-tasks/             派给 Agent 的任务单

09-镜头与视频提示词/生成结果/<场次>/<单元>/*.mp4   成片
```

## 七、如何保持这份文件是新的

```bash
cd ~/gen_v/Shotcall
python3 app/docgen_workflow.py -o docs/workflow.md
```

它只读登记表和工作台的流程定义，不写项目文件。改动流程或登记成果后重跑一次即可。
