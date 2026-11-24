# 多项目切换 + C 端到端工作流测试

- 日期：2026-11-24
- 测试项目：`/users/huxian/short_v/有谁共鸣2_2`（隔离；未写正式项目）
- 正式项目：`/users/huxian/short_v/有谁共鸣2`（只读；测试前后 artifact-state mtime 未变）
- 测试工作台：`127.0.0.1:8799`，启动 env 固定 `WORKBENCH_PROJECT=_2`
- 测试子代理：`workflow-tester`，独立上下文；lead 只收结果

## 1 多项目切换

- 入口：页头项目下拉（`/api/projects`）。
- API：
  - `GET /api/projects` → 项目列表 + active
  - `POST /api/project/switch {"path"|"id":...}` → 切换；有生成任务在跑时返回 **409**，等队列清空再切
- 状态隔离：
  - `store.state_dir` 在切换后（`store.ACTIVE_PROJECT` 非空）改用**目标项目/制作运行**，不再用启动项目的 `WORKBENCH_STATE`，避免 decisions/approvals/confirm 串账。
  - 测试覆盖：`test_projects.py` 7/0（发现项目、非法路径、启动 state、切换后 state、旧项目不串）。
- 说明：生产 8790 启动时无 `WORKBENCH_STATE`，直接按项目/制作运行；`WORKBENCH_STATE` 只用于测试/隔离启动。

## 2 C 测试结果（11 张卡）

子代理在 `_2` 写简化文件，经 8799 工作台登记/确认：

| 阶段 | register | confirm | 最终状态 |
|---|---|---|---|
| 01 概念 | 200 | 200 | 已确认-当前 |
| 02 角色 | 200 | 200 | 已确认-当前 |
| 03 世界观 | 200 | 200 | 已确认-当前 |
| 04 分场 | 200 | 200 | 已确认-当前 |
| 05 剧本 | 200 | 200 | 已确认-当前 |
| 05b 时长复核 | 200 | 200 | 已确认-当前（测试后补登记） |
| 06 压力测试 | 200（`outcome=passed`） | 不确认（结论型） | 已完成-结论 |
| 07 资产 | 200 | 200 | 已确认-当前 |
| 08 ACTING | 200 | 200 | 已确认-当前 |
| 09a 镜头规划 | 200 | 200 | 已确认-当前 |
| 09b 视频提示词 | 200 | 200 | 已确认-当前 |

- `artifact-state.json`：**11 条**（10 已确认 + 1 结论），schema 3.4；sha256 与磁盘一致。
- 负向用例：01 草稿时登记 02 → 400「依赖不是当前确认版本」；重复登记 → 400；项目外路径 → 400；非法 scene/阶段 → 400。
- 发现并修复：
  1. **扁平 skill 布局**：`_2/.agents/skills/ai-film-router/...` 不被旧 glob 识别 → `flow.guard_script` 同时兼容嵌套/扁平。
  2. **05 双产出**：`screenplay-timing-audit` 原先无法登记 → 新增 **05b 时长复核** 卡并补登记测试文件。
  3. **已过期状态**：`已过期-禁止使用` 映射为 `superseded`，不再 `unknown`。
  4. **跨项目串账**：切换项目后 state 目录隔离（见上）。
- 取证：[workflow-test-evidence-20261124.md](/users/huxian/short_v/有谁共鸣2_2/制作运行/workflow-test-evidence-20261124.md)

## 3 仍未做（测试建议）

1. 流程页「登记成果」无文件选择器；同阶段多新文件时靠自动挑最高版本，易登错。
2. 可选阶段（03/06）无「跳过」按钮；无批量登记/确认、无版本差异/sha/依赖链视图、确认无二次确认/撤销。
3. Agent 未接入：仍需人工复制提示词；没有自动执行入口。
4. 空项目没有「初始化 00-项目管理」入口；guard 缺失/扁平问题会先报模糊错。
5. 09b 依赖校验正则只认 `minimax-h3-vNNN.md`，不认 `minimax-h3-ref2va-vNNN.md`；目前靠 flow 传 depends_on 兜底。
6. 多项目选择器只显示“空/有登记”，没有健康度（.agents、输入、集数、最后修改）；不能手工添加路径。

## 3.1 追加的退回/修订实测

- `_2` 沙箱追加：01 退回 → 01 v002 确认 → 下游 10 项自动过期 → 02 v002 确认；artifact-state 13 条。
- 完整说明见 [M7 流程交互与修订](M7-流程交互与修订.md)；自动化 `test_revision.py` 11/0。

## 4 测试

- `test_all` **48 通过 / 0 失败 / 2 跳过**；`test_projects` 7/0；`test_revision` 11/0；`test_flow` 10/0（11 卡）；`test_m1_acceptance` 80/0/2。
