# shotcall-units 字段契约 · v1

> 历史文件里的 ```yoko-shots 标记仍可解析，新文件统一写 ```shotcall-units。

> 状态：**已生效**（2026-11-25）。历史文件（v016 及更早）按旧格式保留，不再使用。
> 技能 `film-cinedance` 规定意图，本文规定字段。两者冲突时以本文为准。

## 一、定位

| 问题 | 答案 |
|---|---|
| 谁写 | 写视频提示词的那次任务（09 卡片的“模型提示词”部分） |
| 谁读 | 工作台服务端：生成台读单元、参考图、音色、接续 |
| 在哪 | 提示词文件末尾的 ` ```shotcall-units ` 块 |
| 何时变 | 每次新增提示词版本时重写 |

正文给人看、给模型读；`shotcall-units` 给程序读。两者描述同一批单元。

## 二、块格式

```json
{"schemaVersion": 1, "units": [ … ]}
```

`units` 非空；每个元素是一个生成单元。

## 三、单元字段

### 3.1 标识与时长

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | `U001`、`U001-B`；与正文 `### 标题` 编号一致 |
| `shotIds` | string[] | 是 | 覆盖镜号；与标题第三个字段一致 |
| `duration` | number | 是 | 秒；与标题“N秒”一致 |

### 3.2 模型

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `modelId` | string | 是 | 如 `minimax-h3`；同文件内一致 |
| `promptFormat` | string | 是 | `MiniMax H3` / `Seedance 2.5` / `Seedance 2.0` |
| `promptStatus` | `planning` \| `compiled` | 是 | 只有可直接投产的才是 `compiled` |

### 3.3 画幅

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `aspectRatio` | string | 是 | `16:9` 等；分辨率在投产时确定 |

### 3.4 素材

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `referenceImages` | string[] | 是 | 项目内相对路径，顺序即 `<Picture n>`；可为空数组 |
| `referenceImageRoles` | object[] | 有图时必填 | `{index, label, assetId, role}`，`index` 从 1 起 |
| `referenceAudios` | string[] | 否 | 顺序即 `<Audio n>` |
| `referenceAudioRoles` | object[] | 否 | `{index, label, assetId, role}` |

### 3.5 接续

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chainFrom` | string \| null | 是 | **生成任务依赖**：用哪个单元的末帧作首帧；默认 `null`，非空时须是同文件内其他 `id` |
| `continuation` | `same-shot` \| `next-shot` \| null | 否 | 同镜分段 / 真实切镜 / 无接续 |

### 3.6 来源

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sourceShotPlan` | string | 是 | 所依据镜头表路径；须是当前确认版 |
| `sourceGenerationUnits` | string | 是 | 所依据生成单元规划路径；须是当前确认版 |

### 3.7 实验单元

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `control` | boolean | 否 | `true` 表示 A/B 对照等实验单元，状态只能是 `planning` |

## 三·五、`chainFrom` 的语义与默认值

`chainFrom` 表示**生成任务之间的依赖**（本单元的输入来自前置单元的生成结果），不是剧情顺序、人物状态连续或动作接续的表达。

- **默认 `null`**：绝大多数单元各自独立生成，跨镜连续靠参考素材与每条提示词自带的完整首尾状态。
- **只有同时满足**下列条件才填前置单元 `id`：
  1. 已确认的生成单元规划明确写了"本单元依赖某个前置单元的生成结果"；
  2. 已核对生成台确实支持该接续方式（末帧接入首帧）。
- **禁止**因为下列理由自动填写：单元编号连续、剧情连续、动作接续、共用参考素材、美术风格一致。

普通连戏关系写进连续性台账与各条提示词的起止状态，不写进 `chainFrom`。

编译提示词时逐项核对 `chainFrom` 与已确认规划是否一致；规划没要求的生成依赖，不得自行添加。

## 四、由工作台推导（文件里不写）

| 项 | 来源 |
|---|---|
| 实际分辨率、步数、工作流 | 提交时生成台的选择 |
| 确认状态与时间 | 审批账本 `制作运行/approvals.jsonl` |
| 是否过期 | 与 `source*` 对应文件的 sha 比较 |
| 帧数 | `duration` 按 `17n+5` 换算 |
| 资产细节 | 按 `referenceImageRoles.assetId` 查 `资产绑定/` |

## 五、登记校验

登记 09 视频提示词时逐条检查，任一不过即拒绝：

1. 有且只有一个 `shotcall-units` 块，且是 `{schemaVersion, units}` 格式
2. `id` 集合与正文 `### U…` 标题集合完全相等
3. 每个单元的 `duration`、`shotIds` 与标题一致
4. `id` 唯一；`chainFrom` 指向同文件内其他存在的 `id`
5. `referenceImages`、`referenceAudios` 列出的文件在项目内存在
6. 全文件 `modelId`、`promptFormat` 一致；`promptStatus` 在枚举内
7. `sourceShotPlan`、`sourceGenerationUnits` 指向的文件存在且为当前确认版
8. `control: true` 的单元状态为 `planning`

## 六、与旧格式的关系

v016 及更早的块是裸数组，字段更多（含 `prompt` 副本、`resolution`、`assetBindings` 等）。

- **解析**：工作台两种格式都读，旧版本在生成台照常可用。
- **校验**：登记新版本时按本契约检查；旧块缺新字段不视为错误，但 `source*` 指向旧版本会被拦下。
- **新写**：一律使用 `{schemaVersion, units}` 格式，字段按第三节。