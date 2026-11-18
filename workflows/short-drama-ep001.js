// 短剧 EP001 创作流水线
// 用法：args = { project, episode, stage:"A"|"B", provider, model }
// stage A → 剧本 / 视觉设定 / 图片提示词   （产出后由导演做审核一）
// stage B → 分镜 / 视频提示词 / 独立审稿   （产出后由导演做审核二）
const P = args.project || "/users/huxian/short_v/有谁共鸣2";
const EP = args.episode || "EP001";
const model = args.provider && args.model ? { provider: args.provider, model: args.model } : {};
const base = `项目根目录：${P}，本集：${EP}。先加载技能 {SKILL}，按其完整指令执行；只读写本集相关文件，不要动 ${args.other || "其他集"}。`;
const ctx = `已有输入：项目开发/episode-map.jsonl 中 ${EP} 的分集大纲、项目开发/source-analysis/ 的全本拆解、输入/ 原著正文。`;

if ((args.stage || "A") === "A") {
  phase("继承已有成果");
  const inherit = await agent(
    `${base.replace("{SKILL}","short-drama")}\n任务：只读盘点，不写任何文件。读 项目开发/开发总览.md、episode-map.md 中 ${EP} 的条目、source-analysis/全本拆解总览.md 与 characters.md，再以 剧集/EP002/ 的剧本/视觉设定/分镜/视频提示词 作为本剧的格式与口径基准。\n输出一份「${EP} 创作约束摘要」：\n1) 已完成的阶段清单（原著拆解、分集大纲分别覆盖到哪）\n2) ${EP} 必须继承的设定：人物身份与体态、时代与地域、台词口径（原著对白优先）\n3) 从 EP002 学到的格式约定：文件结构、字段、时长估算口径、六段式提示词写法\n4) 仍然空缺、需要本集新建的内容\n只写事实，不臆造未读到的内容。`,
    { ...model, label: "继承已有成果" });

  phase("剧本");
  const script = await agent(
    `${base.replace("{SKILL}","short-drama-write")}\n${ctx}\n任务：先读下面这份「创作约束摘要」，再写出 剧集/${EP}/剧本.md（原著对白优先；删改、合并、叙述转对白、新增对白逐条在文件末尾声明）。完成后自检台词与原著出处。\n\n【创作约束摘要】\n${inherit}`,
    { ...model, label: "剧本" });

  phase("视觉设定");
  const assets = await agent(
    `${base.replace("{SKILL}","short-drama-assets")}\n任务：基于 剧集/${EP}/剧本.md 写 剧集/${EP}/视觉设定.md（人物身份/造型/状态、地点/视角、道具/状态、声线）。剧本摘要：${script}`,
    { ...model, label: "视觉设定" });

  phase("光线与摄影基调（约束层）");
  const [light, camera] = await parallel([
    () => agent(`${base.replace("{SKILL}","lighting-direction")}\n任务：为 ${EP} 写「光线圣经」——每场的时间、动机光源、方向、软硬、色温、对比、受光连续性约束、禁止项。输出为纯文本段落，供下游原样继承。`,
      { ...model, label: "光线" }),
    () => agent(`${base.replace("{SKILL}","cinematography")}\n任务：为 ${EP} 写「摄影基调」——景别策略、焦段倾向、运镜规则、构图禁忌。输出为纯文本段落，供下游原样继承。`,
      { ...model, label: "摄影" }),
  ]);

  phase("写入基调");
  await agent(`${base.replace("{SKILL}","short-drama-assets")}\n任务：把下面两段约束追加到 剧集/${EP}/视觉设定.md 末尾，作为「光线与摄影基调」一节，不要改写已有内容。\n\n【光线圣经】\n${light}\n\n【摄影基调】\n${camera}`,
    { ...model, label: "写入视觉设定" });

  phase("图片提示词");
  const prompts = await agent(
    `${base.replace("{SKILL}","short-drama-image-prompts")}\n任务：基于 剧集/${EP}/视觉设定.md（含「光线与摄影基调」一节）写 剧集/${EP}/图片提示词.md。光源方向、色温、对比必须与光线圣经一致。`,
    { ...model, label: "图片提示词" });

  return { stage: "A", episode: EP, inherited: inherit.slice(0, 400), files: [`剧集/${EP}/剧本.md`, `剧集/${EP}/视觉设定.md`, `剧集/${EP}/图片提示词.md`] };
}

phase("调度与镜头逻辑");
const staging = await agent(
  `${base.replace("{SKILL}","directing-cinematic-storyboards")}\n任务：为 ${EP} 逐场写导演调度方案——场次空间关系、人物站位与走位、视线与轴线、镜头分配的理由（为什么这里切、为什么用这个角度）、必须保留的动作与物件。输出纯文本，供分镜原样继承。`,
  { ...model, label: "调度" });

phase("分镜");
const board = await agent(
  `${base.replace("{SKILL}","short-drama-storyboard")}\n任务：基于 剧本.md、视觉设定.md（含「光线与摄影基调」）与下面的导演调度方案写 剧集/${EP}/分镜.md（逐镜：来源/时长/目的/景别机位/运镜/起点/动作/终点/参考图/出场设定/声音/首帧提示词）。轴线、站位、视线必须服从调度方案。\n\n【导演调度方案】\n${staging}`,
  { ...model, label: "分镜" });

phase("视频提示词");
const video = await agent(
  `${base.replace("{SKILL}","short-drama-video-prompts")}\n任务：基于 分镜.md 逐镜写 剧集/${EP}/视频提示词.md（H3 六段式，中文对白原生发声，non_diegetic_music: N/A；逐镜继承光线圣经的光源方向与色温）。分镜摘要：${board}`,
  { ...model, label: "视频提示词" });

phase("独立审稿");
const review = await agent(
  `${base.replace("{SKILL}","short-drama-review")}\n任务：独立审查 ${EP} 的剧本、视觉设定、分镜、视频提示词，重点核对跨镜受光方向、色温、肤色一致性；只出问题单与修改要求，不改原稿。`,
  { ...model, label: "审稿" });

return { stage: "B", episode: EP, review, files: [`剧集/${EP}/分镜.md`, `剧集/${EP}/视频提示词.md`] };
