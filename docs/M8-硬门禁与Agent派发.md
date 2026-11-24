# 过期成果硬门禁 + Agent 任务派发

- 日期：2026-11-24
- 状态：已上线 8790；本切片不再烧 GPU

## 1 过期成果硬门禁

生成前在原有「文件级审批 + 镜头确认」之上，再查 `artifact-state.json` 的成果状态：

- 视频 `/api/gen/submit`：检查 09b 提示词本身 + 每个单元 `source`；状态为
  `已过期-禁止使用 / 历史已替代 / 已作废（不合格） / 缺文件-阻断` 时直接 400。
- 首帧 `/api/firstframe/generate`：检查同场最新 `shot-plan-v*.md`；过期 400。
- 出图 `/api/img/generate`：检查最新 `assets-v*.md`；过期 400。
- 错误文案示例：`镜头表已过期，禁止出首帧：shot-plan-v001.md（已过期-禁止使用）`。
- 在 `_2` 修订场景实测：01 v002 确认后 07/09a/09b 全部过期，首帧接口 400；不会再加任务。

## 2 Agent 派发（第一版）

- 流程卡新增「派给 Agent」：
  - 未配置命令：只生成任务单 `制作运行/agent-tasks/AG-*.md`（含提示词 + 下一版路径 + “写回来登记”），返回任务单路径；不猜 CLI。
  - 配置命令后：后台启动，日志写 `AG-*.log`，任务状态 queued → running → done/failed。
- 配置方式（二选一）：
  - 环境变量 `SHOTCALL_AGENT_CMD`（JSON 数组或字符串），如
    `["claude","-p"]` / `["codex","exec"]` / `["dsh", ...]`。
  - 项目内 `00-项目管理/工作台/agent.json`：
    `{"command":["claude","-p"], "cwd":"<project>", "timeout":1800}`
  - 参数含 `{prompt_file}` 时用任务单路径替换；否则提示词从 stdin 传入。
- 任务单只负责“把提示词交给 Agent”；写盘后仍需导演在流程页登记/确认（保持确认门禁）。
- API：`POST /api/agent/dispatch`、`GET /api/agent/tasks`。

## 3 09b 依赖（B 已完成）

- 已备份并按兼容规则修改两个项目的 `artifact_guard.py`：
  - `minimax-h3-ref2va-vNNN.md` 现在会被识别为 H3 提示词变体；
  - 依赖可用 `generation-units-minimax-h3` **或** 同场 `shot-plan`（ref2va 允许直接依赖镜头表）；
  - 普通 `minimax-h3-vNNN.md` 仍必须依赖生成单元。
  - 备份：`/tmp/guard-backup-20261124-164739/`
- 正式项目：**未覆盖 v011**，新增 `minimax-h3-ref2va-v012.md`（正文同 v011，仅加依赖清洗说明），登记并确认；
  - v012 状态 `已确认-当前`，依赖：`09-…/镜头规划/N001/shot-plan-v001.md`、`07-图像资产/assets-v004.md`；
  - v010/v011 保留为旧草稿，不再作为当前。
- 测试项目 guard 同步兼容；`test_guard_compat.py` 3/0。

## 3.1 新项目初始化（B 已完成）

- 项目下拉末尾「＋ 初始化新项目…」：在 `~/short_v/<名字>` 建 00–09、制作运行、07 候选/绑定、09 镜头/提示词/生成结果等目录；
- 自动复制 `.agents` 技能模板（从已有项目），不伪造 `artifact-state.json`（由 guard 首次登记时生成）；
- 有生成任务在跑时拒绝初始化/切换；新项目创建后自动切过去；
- 项目列表增加登记数 / 有无技能 / 最后修改；`test_projects.py` 10/0（含临时根初始化）。

## 4 测试

- `test_gate.py` 2/0：过期 09b 触发视频门禁。
- `test_agent.py` 6/0：无命令生成任务单、不自动执行、下一版路径、任务可列、配置命令会启动并完成。
- `test_all` **49 通过 / 0 失败 / 2 跳过**。
- 8799 修订场景实测：首帧接口 400（镜头表过期）。
