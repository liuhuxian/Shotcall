# skills-deps

`有谁共鸣2` 用到的 craft 技能，在这里**只存指针**。

## 结构

```
skills-deps/
├─ meta/skills.map.json    技能 → 上游仓库 + commit + 文件 sha1
├─ vendor/                 三个上游仓库的 submodule（固定 commit）
└─ scripts/verify.sh       下载上游文件，与项目内副本逐字节比对
```

`vendor/` 是 submodule，仓库里只记录「哪个仓库、哪个 commit」。

## 当前固定版本

| 技能 | 上游仓库 | commit |
|---|---|---|
| cinematography | fal-ai-community/skills | `653a2b79` |
| lighting-direction | calesthio/generative-media-skills | `d5daa8a2` |
| performance-direction | calesthio/generative-media-skills | `d5daa8a2` |
| minimax-h3-prompt-writing | MiniMax-AI/MiniMax-H3 | `b731d884` |

## 校验

```bash
./skills-deps/scripts/verify.sh
# 或指定项目路径
PROJECT_ROOT=/path/to/project ./skills-deps/scripts/verify.sh
```

逐文件输出 ✅/❌ 与 sha1。任一项不一致即返回非 0。

## 升级流程

1. 改 `meta/skills.map.json` 的 commit 与 sha1
2. `git -C skills-deps/vendor/<repo> checkout <new-commit>`，提交 submodule 指针
3. 跑 `scripts/verify.sh`
4. 同步更新项目内 `.agents/skills-upstream/<skill>/`
5. 跑一条测试片确认没退化，再提交项目侧变更

**不要**让项目直接跟随上游 main：技能一改，出片行为就变。
