# Shotcall · 导演工作台

本地单人导演工作台：围绕项目的 **00–09 阶段目录 + 成果登记 + 确认门禁 + 视频生成**，让“看、确认、生成、回片、挑片”可追溯、可回滚。

- 只监听 `127.0.0.1`，默认端口 **8790**
- 不写 `05-正式剧本` / `02-角色圣经` / `08-表演设计`；`07-图像资产` 只按授权新增采用/绑定快照；`09-镜头与视频提示词` 只新增，不覆盖既有 vNNN
- 不包含任何密钥；凭据只从 `~/.dsh/.credentials.yaml` 读

> 当前已完成 **M1 地基、M2 场次/资产审核、流程页、出图/改图、首帧、多项目切换与 C 端到端测试**。范围是 E001–E003；挑片、成片、135 集总览仍在后续。详见 [需求与验收标准 v1.1](docs/需求与验收标准-v1.1.md)、[工作流与提示词 v1](docs/工作流与提示词-v1.md)、[M6 多项目与 C 测试](docs/M6-多项目与C测试.md)。

## 启动

```bash
cd Shotcall

# 1) 生成界面（源码在 app/ui_src/template.html）
python3 app/build_ui.py

# 2) 启动
python3 app/server.py --port 8790
```

打开 <http://127.0.0.1:8790/>。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKBENCH_PROJECT` | `~/short_v/有谁共鸣2` | 项目根目录 |
| `WORKBENCH_STATE` | `<项目>/制作运行`（不可写时退到 `app/run/`） | 审批、批次、任务、确认 |
| `WORKBENCH_MEDIA` | `<项目>/剧集/EP002/制作成果` | 旧候选扫描根（M1 不用） |
| `SHOTCALL_VJOBS_FILE` | `app/run/vjobs.json` | 仅测试隔离用，生产不要设 |
| `SHOTCALL_NO_BACKGROUND` | 未设 | 测试用：禁止启动调度/采样线程 |

依赖：Python 3.9+ 标准库；`ffmpeg`/`ffprobe`；`nvidia-smi`（算力页）。不需要 pip。

## 页面

| 页面 | 作用 |
|---|---|
| 工作流 | 00–09 阶段轨道、成果、SHA、依赖失效、草稿确认 |
| 场次 | N001–N008 的场次/表演/镜头/出片进度 |
| 资产 | 07 资产条目、绑定图、已忽略、云端出图渠道 |
| 流程 | 左侧 01–09b 导航 + 右侧单阶段详情：复制/派发 Agent（DSH/Codex + 模型）、登记/确认、退回/跳过、版本与依赖；点文件在页内下方直接审核 |
| 项目 | 页头切换 `有谁共鸣2` / `_1` / `_2`；切项目按项目/制作运行隔离账本；可初始化新项目 |
| 镜头 | N001 6 镜：候选墙按 SH 聚合，采用/备选/hold/重做，CSV/EDL 导出；逐镜生成门禁 |
| 生成台 | 提示词版本、参考图、已有结果、提交、生成历史 |
| 资产 | 忽略/取消忽略、默认隐藏已忽略；点资产名看采用历史/候选/任务；引用当前或引用其他图生成 |
| 首帧 | 从 09a 镜头表拼首帧提示词，参考图缩略图选择，H3 Image 出图/采用；09b 视频按首帧驱动 |
| 算力 | 8 卡占用、ComfyUI 实例、队列、启停/中断/清队、工作流导入 |
| 待确认 | 登记草稿 + 未登记文档；确认写审批账本 |
| 校验 | 文件缺失/改动/失效依赖；不冒充艺术判断 |

## 数据流

```text
00–09 文档 + 00-项目管理/artifact-state.json（只读）
        ↓ 读取/核对 SHA
   工作流/场次/资产/镜头 页面
        ↓ 导演确认（文件级 / 场级 / 资产级 / 镜级）
   制作运行/approvals.jsonl（追加式，改判 supersedes）
        ↓ 生成门禁：文件版本锁 + 单元覆盖的所有镜头都通过
   制作运行/generation-batches/<BATCH>.json（绑定 approvals/SHA/参数）
        ↓
   app/run/vjobs.json（M1；M2 迁 制作运行/jobs.db）
        ↓ 调度器 → 按卡 ComfyUI → 09-…/生成结果/<场>/<单元>/<seed>.mp4
```

**门禁（M1 已生效）**：

1. 提示词版本必须有一条有效的 `file pass`，且文件 SHA 未变；
2. 生成单元覆盖的每个镜头都必须有有效 `shot pass`；
3. 缺一项就 400，并列出缺哪一项；没有 `force` 旁路；
4. 提交成功写批次记录，记录 approvals/提示词 SHA/参数/单元。

## 目录

```text
app/
  server.py          HTTP 服务/API/静态 UI/文件服务
  approvals.py       追加式审批账本
  artifacts.py       只读 artifact-state + SHA/依赖
  stages.py          00–09 阶段定义
  confirms.py        旧本地确认缓存（迁移用，不再双写）
  scenes.py          场次/镜头索引
  assetsdb.py        07 资产与绑定
  vjobs.py           视频任务、H3 图、接续、回传
  sched.py           显卡调度、依赖、失败传播
  gpus.py            8 卡探测
  comfyctl.py        按卡启停 ComfyUI（--cuda-device）
  comfywatch.py      WS 进度 + nvidia-smi 显存采样
  imagegen.py        H3 Image 图构造（M3 接 UI）
  imgjobs.py         图片任务（M3）
  translate.py       DeepSeek 提示词翻译
  test_all.py        一条命令跑全部测试
  test_*.py          各层测试
  ui_src/template.html  界面源码
  ui/index.html         构建产物（gitignore）
```

## 测试

```bash
python3 app/test_all.py
```

- 不需要启动 8790、不需要 GPU、不写正式项目；临时项目 + fake ComfyUI。
- M1 当前：`test_all` 50 通过 / 0 失败 / 2 跳过；独立验收 `test_m1_acceptance.py` 80 通过 / 0 失败 / 2 跳过（2 项为浏览器 UI 断言）。
- `test_all` 会在进程内起一个临时 HTTP 服务，不访问 8790。

## 当前边界（M1）

- **只做 E001–E003**；135 集总览推迟。
- **N001 只有 6 镜有镜头表**；N002–N008 显示“尚未拆镜”，不造假。
- **M1 用 v009**（v005 的 U001 拆分版）；M1 验收时再定是否回 v005。
- **07 出图/采用、挑片、成片拼接** 在 M3–M5；M1 不做。
- 任务库仍在 `app/run/vjobs.json`（M2 迁项目 `制作运行/jobs.db`）；图片任务 M3 才持久化。
- 真实生成验证最多 2 条/张，用完即撤或标 TEST；不跑大规模批次。
- 旧 `剧集/EP002` 四文档与 gate 1/2/3 只读归档，不再作为页面事实源。

## License

MIT
