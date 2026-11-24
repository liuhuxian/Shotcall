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

## 3 09b 依赖

- 工作台 `flow.register` 已给 09b 传 `--depends-on`（09a 镜头表 + 07 资产），新登记会带依赖。
- 项目 `artifact_guard.py` 的提示词正则仍只认 `minimax-h3-vNNN.md`，不认 `-ref2va-`；**未直接改项目技能**（避免影响现有 v011 草稿）。下一步要么改 guard 兼容 `ref2va`，要么清洗 v011 依赖后重登。

## 4 测试

- `test_gate.py` 2/0：过期 09b 触发视频门禁。
- `test_agent.py` 6/0：无命令生成任务单、不自动执行、下一版路径、任务可列、配置命令会启动并完成。
- `test_all` **48 通过 / 0 失败 / 2 跳过**。
- 8799 修订场景实测：首帧接口 400（镜头表过期）。
