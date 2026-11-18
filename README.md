# Shotcall · 导演工作台

**本地 AI 短剧导演审片台**——把"两次导演审核 + 批次授权"从口头意见，变成有范围、有版本、可追溯的决策记录。

导演只做两件事：**看**和**拍板**。写稿、拆镜、拼提示词交给 Agent；本工具负责让"拍板"这件事可执行、不越界、改得回来。

---

## 解决什么问题

AI 短剧制作里，最贵的不是生成，是**返工**。而返工的根源通常是三件事没说清：

1. **范围**——"这场可以"被理解成"整集通过"
2. **版本**——改了三轮之后，没人知道当初批的是哪一版
3. **授权**——"这个拍法我认可"被当成"你可以去跑 15 条视频"

Shotcall 把这三件事变成数据结构：每条判定绑定**单元 × 文件哈希 × 覆盖范围**，文件一变旧判定自动标"需复核"；创作通过和批次授权分开记录。

---

## 核心设计

| 原则 | 落地方式 |
|---|---|
| 创作文件只读 | 只解析 `剧本.md` / `分镜.md` / `视频提示词.md` / `视觉设定.md`，从不改写 |
| 决策只追加 | `decisions.jsonl` 一行一条，改判产生新记录 + `supersedes` 指向旧记录，历史不删 |
| 版本绑到单元 | 每条判定记录四个文件的 sha256；读取时逐条比对，不一致即 `stale` |
| 审核与授权分离 | `gate=1` 审核一、`gate=2` 审核二、`gate=3` 批次授权，三类记录互不推导 |

---

## 功能

| 页面 | 作用 |
|---|---|
| 全剧总览 | 分集进度表、全剧统计、待处理告警 |
| 工作流 | 16 步制作流程，每步标注**判定依据**（哪个文件存在 / 几条记录） |
| 审核一 · 剧本 | 逐场读剧本、原著出处行号、引用资产；通过 / 需修改 / 待定 + 意见 |
| 审核一 · 资产 | 人物、地点、道具逐项判定 |
| 审核二 · 分镜 | 90 格分镜墙，按 未审 / 已判 / 需修改 / 已选 / 短镜 筛选 |
| 单镜详情 | 分镜字段、首帧提示词、六段式视频提示词、参考图、台词链、上下镜翻页 |
| 检查 | 机械检查：断链、帧数越界、提示词规范、服装状态是否落进提示词 |
| 出图 | 资产图缺口、图片批次签字 |
| 视频生成 | 批次签字 → 预检 → 构造任务 → 提交 → 自动回片 |
| 挑片 | 浏览器内播放候选，采用 / 备选 / 重做 + 原因标签 |
| 修改单 | 一键导出 markdown 意见单（含版本清单） |

---

## 快速开始

```bash
cd Shotcall

# 1. 生成界面（源码在 app/ui_src/template.html）
python3 app/build_ui.py

# 2. 启动，只监听本机
python3 app/server.py --port 8790
```

打开 `http://127.0.0.1:8790/`。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKBENCH_PROJECT` | `~/short_v/有谁共鸣2` | 指向你的短剧项目根目录 |
| `WORKBENCH_STATE` | `<项目>/制作运行/`（不可写时退到 `app/run/`） | 决策、任务、选择、运行记录 |
| `WORKBENCH_MEDIA` | `<项目>/剧集/EP002/制作成果/` | 候选素材扫描根 |

```bash
WORKBENCH_PROJECT=~/my-drama python3 app/server.py --port 8790
```

依赖：Python 3.9+ 标准库；`ffmpeg`/`ffprobe` 用于缩略图和时长检测。不依赖任何 pip 包。

---

## 模块

```
workbench/
  server.py          HTTP 服务与全部 API
  parse_project.py   只读解析四份创作文档
  dataset.py         整理成界面数据 + 缩略图
  store.py           决策与选择的追加式存储
  preflight.py       提交前预检
  planner.py         提示词拼装 + ComfyUI API 图
  runner.py          提交 / 轮询 / 下载 / 重试
  submit.py          命令行构造任务
  build_ui.py        由 ui_src/template.html 生成 ui/index.html
  test_all.py        回归测试（不需要 GPU）
  ui_src/template.html   界面源码
  ui/index.html          界面生成物
```

**改界面改 `ui_src/template.html`，然后跑 `build_ui.py`。**

### API

```
GET  /api/data          全量数据（含决策、批次、候选、工作流状态）
GET  /api/health        健康检查与路径信息
GET  /api/export        导出导演意见单 markdown
GET  /api/batch?id=     单个批次详情
GET  /api/workflow      各集工作流状态
POST /api/decision      落账一条判定
POST /api/selection     记录挑片结果
POST /api/preflight     提交前预检
POST /api/plan          构造任务（不提交）
POST /api/execute       授权并提交生成
GET  /media?r=&p=       候选视频（支持 Range，可拖动进度）
GET  /thumb?r=&p=       候选缩略图
```

---

## 生成对接

通过 ComfyUI HTTP API（`POST /prompt`、`GET /history/{id}`、`GET /view`）提交与回片。生成前必须：

1. 在界面上给批次签字（`gate=3` 记录）
2. 通过预检（缺提示词、缺参考图、帧数越界会阻断）

同一条任务用幂等键去重，重启后按任务记录对账，不重复提交。

---

## 测试

```bash
python3 app/test_all.py
```

不需要 GPU，覆盖：大纲校验、工作流状态、预检阻断、任务图结构、落账 supersede、接口可用性。内容类断言在有剧本时自动启用，空集时自动跳过。

---

## 现状与边界

- 目前只在一个真实项目（《有谁共鸣2》，50 集 × 3 分钟）上跑过
- 生成链路真实跑通过一次（1 镜草稿，175 秒出片），未做大批量验证
- 出图耗时尚需按机器标定，界面里不给假数字
- 只支持单用户本地运行，无鉴权、无多租户

---

## License

MIT
