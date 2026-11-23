#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把原型模板转成连真 API 的界面。

替换三处：
1. 内嵌数据 → fetch 引导
2. 演示用的假候选挑片 → 真实候选扫描
3. 底部同步 render() → 异步 BOOT()

    python3 build_ui.py   → app/ui/index.html
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TPL = ROOT / "ui_src" / "template.html"
OUT = ROOT / "ui" / "index.html"

BOOT = """let D=null, IMG={}, S=null;
const loadEp=async(ep)=>{
  const r=await fetch('/api/data'+(ep?('?ep='+encodeURIComponent(ep)):''));
  const j=await r.json();
  D=j.data; IMG=j.images||{}; S=D.stats;
  state.ep = D.episode;
  state.scene = (D.scenes[0]||{}).id || null;
  state.shot = ((D.shots.find(x=>x.motion)||D.shots[0])||{}).id || null;
  state.verdicts={};
  (D.decisions||[]).forEach(d=>{
    if(d.unit) state.verdicts[d.unit.type+"::"+d.unit.id]=d.verdict;
  });
  render();
};
const BOOT=async()=>{
  await loadEp();
  if(!TICK) TICK = setInterval(pollTick, 3000);   // 全局 3 秒轮询：算力页与其它页共用
  try{
    const h = await fetch('/api/health').then(r=>r.json());
    if (h.stateDir && h.project && !String(h.stateDir).startsWith(String(h.project))){
      window.__STATE_WARN = '记录在 ' + h.stateDir + '，不在项目内；项目恢复可写后会自动迁移。';
    }
  }catch(e){}
};"""

PICK = r'''/* ---------- 挑片（真实候选） ---------- */
const SELBADGE = st => st==="adopt"?'<span class="badge b-ok">采用</span>'
  : st==="backup"?'<span class="badge b-acc">备选</span>'
  : st==="redo"?'<span class="badge b-warn">重做</span>':'<span class="badge b-mut">未审</span>';

function sPick(){
  const cands = D.candidates||[];
  const selBy = {}; (D.selections||[]).forEach(s=>{ selBy[s.shot]=s; });

  if(!cands.length){
    return `<div class="panel">
      <div class="ph"><span>挑片</span><span class="badge b-mut">0 条候选</span></div>
      <div class="pb">
        <div class="alert" style="cursor:default;margin:0">
          <div class="ic" style="background:#2a1f0d;color:var(--warn)">!</div>
          <div><b>还没有候选片</b>
            <div class="dim" style="font-size:12.5px;margin-top:4px">
              挑片只能对<b>已生成</b>的素材操作。工作台会扫描
              <span class="mono">剧集/EP002/制作成果/</span>，目前该目录为空。<br>
              下一步是出图/出片：先走「出图」或「视频生成」页，生成回来的素材会自动出现在这里。
            </div></div>
        </div>
        <div class="dim" style="font-size:12px;margin-top:14px">
          注：服务器 <span class="mono">output/video/MiniMax_H3/</span> 有 29 条测试片，
          它们不属于本剧镜头，不会被自动归集。
        </div>
      </div></div>`;
  }

  const groups = {};
  cands.forEach(c=>{ const k=c.shot||"未归类"; (groups[k]=groups[k]||[]).push(c); });
  const head = Object.keys(groups).map(k=>{
    const sel = selBy[k];
    return `<div class="sect">${esc(k)} <span class="dim">${groups[k].length} 条</span> ${sel?SELBADGE(sel.status):""}</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-bottom:16px">
      ${groups[k].map(c=>`<div class="cand">
        <div class="poster" style="aspect-ratio:16/9;background:#000">
          <video controls preload="metadata" style="width:100%;height:100%;display:block"
            poster="/thumb?r=${c.rootIdx}&p=${encodeURIComponent(c.path)}"
            src="/media?r=${c.rootIdx}&p=${encodeURIComponent(c.path)}"></video>
        </div>
        <div class="meta" style="padding:10px 12px">
          <div class="mono" style="font-size:11.5px;color:#c9d2e0;word-break:break-all">${esc(c.file)}</div>
          <div class="dim" style="font-size:11px;margin-top:3px">${(c.bytes/1048576).toFixed(1)} MB · ${esc(c.mtime)}</div>
          <div style="margin-top:6px">${SELBADGE(sel&&sel.candidate===c.path?sel.status:null)}</div>
        </div>
        <div class="acts">
          <button class="btn ok" style="flex:1" onclick="pick('${esc(c.shot)}','${esc(c.path)}','adopt')">采用</button>
          <button class="btn" style="flex:1" onclick="pick('${esc(c.shot)}','${esc(c.path)}','backup')">备选</button>
          <button class="btn" style="flex:1" onclick="pick('${esc(c.shot)}','${esc(c.path)}','redo')">重做</button>
        </div></div>`).join("")}
    </div>`;
  }).join("");
  const REASONS = ["眼神/视线","缺字/念错","动作幅度","停顿节奏","人脸漂移","口型不同步","闪烁伪影","构图/景别"];
  const cur = (state.reasonChips||[]);
  return `<div class="panel"><div class="ph"><span>挑片</span><span class="dim">一镜一个采用 · 采用不覆盖原文件</span></div>
    <div class="pb">
      <div style="margin-bottom:14px">
        <div class="dim" style="font-size:12px;margin-bottom:7px">重做原因（选中后会随「重做」一起记录）</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${REASONS.map(r=>`<div class="chip ${cur.includes(r)?'on':''}" onclick="toggleReason('${r}')">${r}</div>`).join("")}
        </div>
      </div>
      ${head}
    </div></div>`;
}

function toggleReason(r){
  const a = state.reasonChips||[];
  state.reasonChips = a.includes(r) ? a.filter(x=>x!==r) : [...a, r];
  render();
}
function pick(shot,candidate,status){
  return fetch("/api/selection",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({shot,candidate,status,reason:state.reasonChips})})
    .then(r=>r.json()).then(j=>{
      if(!j.ok){ toast("记录失败："+(j.error||"")); return; }
      toast(`已记录 ${({adopt:"采用",backup:"备选",redo:"重做"})[status]}`);
      return fetch("/api/data").then(r=>r.json()).then(x=>{ D=x.data; render(); });
    }).catch(e=>toast("记录失败："+e.message));
}

/* ---------- 批次动作：预检 / 构造（不提交） / 签字 ---------- */
function runPreflight(ids){
  const items = ids.map(id=>({shot:id}));
  return fetch("/api/preflight",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({kind:"video",items})})
    .then(r=>r.json()).then(j=>{ state.preflight=j; render(); })
    .catch(e=>toast("预检失败："+e.message));
}

function planTasks(ids){
  return fetch("/api/plan",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({shots:ids,candidates:3,tier:"draft"})})
    .then(r=>r.json())
    .then(j=>{ state.plan=j; render();
      toast(j.ok?`已构造 ${j.count} 条任务（未提交）`:`构造被拒：${(j.blockers||[]).length} 项阻断`); })
    .catch(e=>toast("构造失败："+e.message));
}


function executeBatch(ids){
  if(!confirm('确认批准并提交？\n\n会先记录一条 gate=3 授权，然后在后台跑 '+ids.length+' 个镜头。')) return;
  return fetch("/api/execute",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({shots:ids,candidates:3,tier:"draft"})})
    .then(r=>r.json()).then(j=>{
      if(!j.ok){ toast("未提交："+(j.error||"")+(j.blockers?" "+j.blockers.length+" 项阻断":"")); return; }
      state.exec={running:true,batch:j.batch,total:j.count,done:0,failed:0,log:["已提交 "+j.count+" 条"],startedAt:new Date().toLocaleTimeString()};
      render(); pollExec();
      toast("已提交 "+j.count+" 条，后台生成中");
    }).catch(e=>toast("提交失败："+e.message));
}
function pollExec(){
  clearTimeout(window.__execT);
  fetch("/api/execute").then(r=>r.json()).then(e=>{
    state.exec={...state.exec,...e}; render();
    if(e.running) window.__execT=setTimeout(pollExec,5000);
    else { toast("批次结束：完成 "+(e.done||0)+"，失败 "+(e.failed||0)); fetch("/api/data").then(r=>r.json()).then(x=>{ D=x.data; render(); }); }
  }).catch(()=>{ window.__execT=setTimeout(pollExec,8000); });
}

function openBatch(id){
  return fetch("/api/batch?id="+encodeURIComponent(id)).then(r=>r.json())
    .then(j=>{ state.batchModal=j; render(); })
    .catch(e=>toast("读取失败："+e.message));
}
function closeBatch(){ state.batchModal=null; render(); }
function batchModalHtml(){
  const b=state.batchModal; if(!b) return "";
  const d=b.decision||{};
  const dec=d.notes&&d.notes.length?d.notes.map(n=>esc(n.text)).join("；"):"—";
  const rows=(b.tasks||[]).map(t=>`<tr>
    <td class="mono" style="font-size:11.5px">${esc(t.id)}</td>
    <td class="mono" style="font-size:11.5px">${esc(t.shot.replace("SHOT-EP002-",""))}</td>
    <td class="dim">${esc(t.tier||"")} · seed ${t.params?t.params.seed:"—"} · ${t.params?t.params.frames:"—"} 帧</td>
    <td>${esc(t.status||"")}</td></tr>`).join("");
  const runs=(b.runs||[]).map(r=>`<div style="display:flex;gap:10px;font-size:12px;padding:2px 0">
    <span class="dim mono" style="min-width:70px">${esc((r.at||"").slice(11,19))}</span>
    <span style="min-width:80px;color:${r.event==="succeeded"?"var(--ok)":r.event==="failed"?"var(--bad)":"var(--mut)"}">${esc(r.event||"")}</span>
    <span class="dim">${esc(r.task||"")}${r.seconds?` · ${Math.round(r.seconds)}s`:""}${r.error?" · "+esc(String(r.error).slice(0,50)):""}</span>
  </div>`).join("");
  const files=(b.candidates||[]).map(c=>`<div style="display:flex;gap:10px;align-items:center;padding:5px 0;border-bottom:1px solid #1b2028">
    <span class="mono" style="font-size:11.5px;flex:1;word-break:break-all">${esc(c.file)}</span>
    <span class="dim" style="font-size:11px">${(c.bytes/1048576).toFixed(1)} MB</span>
  </div>`).join("");
  return `<div class="modal" onclick="if(event.target===this)closeBatch()">
    <div class="box" style="width:min(760px,94vw);max-height:86vh;overflow:auto">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
        <div><div style="font-size:15px;font-weight:600">${esc(b.id)}</div>
          <div class="dim" style="font-size:12px;margin-top:3px">${d.id?`签字记录 ${esc(d.id)} · ${esc((d.created_at||"").slice(0,16))}`:"批次单文件"}</div></div>
        <button class="btn ghost" onclick="closeBatch()">关闭</button>
      </div>
      ${b.markdown?`<div class="pb" style="margin-top:12px"><pre class="mono" style="white-space:pre-wrap;font-size:11.5px;max-height:200px;overflow:auto;color:#c9d2e0;margin:0">${esc(b.markdown)}</pre></div>`:""}
      <table style="margin-top:14px">
        <tr><th style="width:90px">覆盖</th><td>${(b.covers||[]).map(x=>esc(String(x).replace("SHOT-EP002-",""))).join("、")||"—"}</td></tr>
        <tr><th>不含</th><td class="dim">${((d.scope||{}).excludes||[]).map(esc).join("；")||"—"}</td></tr>
        <tr><th>备注</th><td>${dec}</td></tr>
        <tr><th>依据</th><td class="dim mono" style="font-size:11px">${Object.entries(d.evidence||{}).map(([k,v])=>k+" "+String(v).replace("sha256:","").slice(0,8)).join(" · ")||"—"}</td></tr>
      </table>
      ${rows?`<div class="dim" style="font-size:12px;margin:14px 0 6px">任务 ${b.tasks.length} 条</div>
        <table><thead><tr><th>任务</th><th>镜头</th><th>参数</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table>`:""}
      ${runs?`<div class="dim" style="font-size:12px;margin:14px 0 6px">运行事件</div>${runs}`:""}
      ${files?`<div class="dim" style="font-size:12px;margin:14px 0 6px">产出文件</div>${files}`:""}
      ${(!b.tasks.length&&!b.markdown)?'<div class="dim" style="margin-top:14px;font-size:12px">这条记录只有授权，没有关联任务（可能是手工签字或早期记录）。</div>':""}
    </div></div>`;
}

function signBatch(id){
  if(!confirm('确认批准批次 '+id+' ？\n\n签字只代表同意该批次的范围与上限，不代表创作内容定稿。')) return;
  return fetch("/api/decision",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({gate:3,unit:{type:"batch",id:id},verdict:"pass",
      notes:[{anchor:id,text:"导演签字批准该批次"}],
      scope:{covers:[id],excludes:["创作内容定稿","超出本批的数量"]}})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){ toast(`已签字 ${j.record.id}`);
        return fetch("/api/data").then(r=>r.json()).then(x=>{ D=x.data; render(); }); }
      toast("落账失败："+(j.error||""));
    }).catch(e=>toast("落账失败："+e.message));
}
'''


def main():
    t = TPL.read_text(encoding="utf-8")

    old = "const D = __DATA__;\nconst IMG = __IMG_JSON__;\nconst S = D.stats;"
    assert old in t, "模板占位未找到"
    t = t.replace(old, BOOT)

    t = t.replace('scene:D.scenes[0].id, shot:"SHOT-EP002-SC001-03A",',
                  'scene:null, shot:null,')

    t = t.replace(
        'const MOCK_V = {"EP002-SC001":"revise","EP002-SC002":"pass","EP002-SC003":"hold","EP002-SC009":"pass"};\nObject.assign(state.verdicts, MOCK_V);',
        "")

    # 顶部横幅：真应用不能说自己"不保存"
    t = t.replace(
        '<span><b>静态原型</b> · 只读演示</span>',
        '<span><b>导演工作台</b> · 本地运行</span>')
    t = t.replace(
        '<span>数据<b>真实</b>（从项目文件解析）</span>',
        '<span>数据<b>实时</b>（解析项目文件）</span>')
    t = t.replace(
        '<span>交互为<b>模拟</b>，不会保存任何修改</span>',
        '<span>判定与批次<b>会写入</b>运行目录；创作文件只读</span>')

    # 挑片整段换成真实实现（连同批次动作一起放回，避免被切掉）
    start = t.index("/* ---------- 挑片 ---------- */")
    end = t.index("/* ---------- 批次 ---------- */")
    t = t[:start] + PICK + "\n" + t[end:]

    # 去掉模板里残留的"原型："前缀（真应用不该自称原型）
    t = t.replace("原型：", "")

    assert "render();\n</script>" in t
    t = t.replace("render();\n</script>", "BOOT();\n</script>")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(t, encoding="utf-8")
    print("wrote", OUT, f"{OUT.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main()
