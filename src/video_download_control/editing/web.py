"""Editing workspace HTML built on the shared Open-Flame design system."""

EDITING_HTML = r'''<!doctype html>
<html lang="zh-CN" class="no-js" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Open-Flame · 编辑</title>
  <link rel="stylesheet" href="/assets/open-flame.css">
  <script src="/assets/open-flame-shell.js" defer></script>
</head>
<body class="editing-page">
  <header class="topbar-shell">
    <div class="topbar">
      <a class="brand" href="/" aria-label="Open-Flame 下载首页"><span class="brand-mark" aria-hidden="true">OF</span><span>Open-Flame</span></a>
      <nav class="primary-nav" aria-label="主要功能">
        <a class="nav-link" href="/">下载</a>
        <a class="nav-link" href="/edits" aria-current="page">编辑</a>
        <a class="nav-link" href="/uploads">上传</a>
      </nav>
      <label class="theme-control"><span>外观</span><select data-of-theme aria-label="界面外观"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
    </div>
  </header>
  <main class="of-shell">
    <header class="page-header">
      <p class="eyebrow">非破坏性媒体工作台</p>
      <h1>从原片生成可核对的编辑版本</h1>
      <p class="lede">设置多个分段并抽取封面；处理计划保存后仍需单独确认。下载原件不会被覆盖，编辑成品进入上传器时还会再次复制并校验。</p>
      <p class="notice page-note">AI 翻译与配音使用独立能力状态。模型、凭据或数据外发条件不满足时不会静默调用远程服务；AI 成品必须先在这里核对。</p>
    </header>

    <section class="status-strip" aria-label="编辑工作流摘要">
      <div class="status-tile"><p class="status-label">原件</p><p class="status-value">只读快照 · SHA-256 校验</p></div>
      <div class="status-tile"><p class="status-label">本地编辑</p><p class="status-value">多段导出 · 封面制作</p></div>
      <div class="status-tile"><p class="status-label">后续动作</p><p class="status-value">核对成品 · 显式导入上传</p></div>
    </section>

    <p id="message" role="status" aria-live="polite">正在连接编辑器…</p>

    <section class="card primary-card" aria-labelledby="source-heading">
      <div class="card-header"><div><p class="section-index">步骤 1</p><h2 id="source-heading">建立编辑项目</h2><p class="muted">从已登记且完成的下载视频创建独立编辑副本。打开本页本身不会复制或处理文件。</p></div><button id="refresh" class="secondary" type="button">刷新状态</button></div>
      <div id="asset-import" class="notice" hidden>
        <label for="project-name">项目名称</label><input id="project-name" maxlength="100" value="新编辑项目">
        <button id="import-asset" type="button">复制此下载成品并建立项目</button>
      </div>
      <div id="projects" aria-live="polite"></div>
    </section>

    <section id="workspace" class="card" aria-labelledby="workspace-heading" hidden>
      <div class="card-header"><div><p class="section-index">步骤 2</p><h2 id="workspace-heading">编辑草稿</h2><p id="project-summary" class="muted"></p></div><span id="draft-state" class="status-value">尚未载入</span></div>
      <div class="editor-grid">
        <div class="preview-panel">
          <video id="preview" controls preload="metadata"><track kind="captions"></video>
          <p id="preview-help" class="muted small">选择项目后可在本机预览源视频；播放器不会修改源文件。</p>
        </div>
        <form id="draft-form" aria-labelledby="workspace-heading">
          <p id="draft-error" class="danger" role="alert" hidden></p>
          <fieldset>
            <legend>分段</legend>
            <p class="muted small">每个区间会导出一份独立 MP4。时间使用秒，可直接输入，也可从播放器读取。</p>
            <div id="segments"></div>
            <button id="add-segment" class="secondary" type="button">添加分段</button>
          </fieldset>
          <fieldset>
            <legend>封面制作</legend>
            <label class="choice"><input id="cover-enabled" type="checkbox"><span>从视频抽取并制作封面</span></label>
            <div id="cover-controls" hidden>
              <label for="cover-at">抽帧时间（秒）</label><div class="row keep-row"><input id="cover-at" type="number" min="0" step="0.001" value="0"><button id="cover-current" class="secondary" type="button">使用当前播放时间</button></div>
              <label for="cover-aspect">成品比例</label><select id="cover-aspect"><option value="4:3">4:3 横版（1200 × 900）</option><option value="3:4">3:4 竖版（900 × 1200）</option><option value="16:9">16:9 横版（1280 × 720）</option><option value="9:16">9:16 竖版（720 × 1280）</option><option value="1:1">1:1 方形（1080 × 1080）</option></select>
              <label for="cover-title">封面标题（可选）</label><input id="cover-title" maxlength="80" placeholder="例如：三分钟看懂重点">
              <p class="muted small">中文等非 ASCII 文字只使用本机受支持的系统字体；不会联网下载字体，缺字时会停止处理。</p>
            </div>
          </fieldset>
          <div class="row"><button type="submit">保存编辑草稿</button><button id="create-plan" class="secondary" type="button">生成待核对处理计划</button></div>
          <p class="muted small">保存只更新本地草稿；生成计划会冻结当前版本，但仍不会开始处理。</p>
        </form>
      </div>
    </section>

    <section class="card" aria-labelledby="ai-heading">
      <div class="card-header"><div><p class="section-index">AI 能力</p><h2 id="ai-heading">自动翻译与自动配音</h2><p class="muted">统一时间轴会保留每段起止时间、来源文字、译文、音色和模型版本。首批禁用声音克隆。</p></div></div>
      <div id="capabilities" class="capability-grid"><p class="muted">正在读取能力状态…</p></div>
      <details><summary>计划中的可审阅参数</summary>
        <div class="diagnostic-grid">
          <div class="diagnostic-panel"><h3>自动翻译</h3><label for="source-language">源语言</label><select id="source-language" disabled><option>自动检测</option></select><label for="target-language">目标语言</label><select id="target-language" disabled><option>中文 ↔ English</option></select><p class="muted small">优先导入 SRT/VTT；保持 cue 顺序与时间不变。模型未安装或未验收时不会创建远程请求。</p></div>
          <div class="diagnostic-panel"><h3>自动配音</h3><label for="voice-provider">配音模式</label><select id="voice-provider" disabled><option>标准音色（不克隆声音）</option></select><label for="mix-mode">原声处理</label><select id="mix-mode" disabled><option>压低原声后叠加</option><option>替换原声</option></select><p class="muted small">逐段生成并校验时长；超出时间槽会标记待修改，不会静默截断。</p></div>
        </div>
      </details>
    </section>

    <section class="card" aria-labelledby="plans-heading">
      <div class="card-header"><div><p class="section-index">步骤 3</p><h2 id="plans-heading">核对计划与处理任务</h2><p class="muted">确认只启动本地编辑处理。翻译或配音涉及远程服务时，还需显示并确认数据范围。</p></div></div>
      <div id="plans"><p class="muted">尚无处理计划。</p></div>
    </section>

    <section class="card" aria-labelledby="outputs-heading">
      <div class="card-header"><div><p class="section-index">步骤 4</p><h2 id="outputs-heading">编辑成品</h2><p class="muted">每个成品都记录来源计划、大小与 SHA-256。视频可显式带入上传器，封面可下载后在上传器中导入。</p></div></div>
      <div id="outputs" class="output-grid"><p class="muted">尚无编辑成品。</p></div>
    </section>
    <p class="page-footer">Open-Flame 0.27.0 · 本地优先 · 下载、编辑与上传数据相互隔离</p>
  </main>
  <script>
'use strict';
const $=id=>document.getElementById(id);
let csrf='',selectedProject=null,draft=null,busy=false,recordsPromise=null,refreshPromise=null,pollPromise=null,pollTimer=null,pageActive=true,projectSelectionGeneration=0;
const renderSignatures=new Map();
const stateNames={review:'待核对',queued:'等待处理',running:'处理中',canceling:'正在取消',ready:'已完成',failed:'失败',canceled:'已取消'};
const codeNames={idempotency_conflict:'相同请求标识对应了不同内容',draft_version_conflict:'草稿已在其他页面更新，请刷新后重试',stale_draft_version:'草稿已在其他页面更新，请刷新后重试',editing_worker_busy:'另一个本地实例正在使用编辑目录',processor_not_configured:'固定媒体工具尚未就绪',source_asset_changed:'编辑源视频已变化，不能继续处理',source_changed:'编辑源视频已变化，不能继续读取',source_hash_mismatch:'源文件校验不一致',source_not_found:'源视频不存在',asset_changed:'编辑成品已变化，不能继续读取',edit_output_not_found:'编辑成品不存在',invalid_edit_recipe:'编辑参数不合法',capability_unavailable:'所选能力尚不可用',editing_storage_full:'编辑目录可用空间不足',editing_storage_unavailable:'编辑目录暂不可用',media_output_too_large:'预计或实际编辑输出超过 8 GiB 上限',cover_font_unavailable:'本机没有可用的受支持封面字体；请清空封面文字后重试',cover_glyph_unsupported:'本机字体不支持封面文字中的部分字符；请修改或清空后重试',restart_confirmation_required:'应用重启后需要重新核对并确认',render_interrupted:'上次本地处理被中断',media_processing_failed:'媒体处理失败'};
function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('danger',error);}
async function api(path,options={}){const headers=new Headers(options.headers||{});if(options.method&&options.method!=='GET')headers.set('X-Editing-CSRF',csrf);if(options.body&&typeof options.body==='string')headers.set('Content-Type','application/json');const response=await fetch('/api/v1/edits'+path,{...options,headers});let data=null;try{data=await response.json();}catch{}if(!response.ok){const code=data?.detail||'request_failed';throw new Error(codeNames[code]||code);}return data;}
async function mutate(work){if(busy)return;busy=true;document.querySelectorAll('button').forEach(button=>button.disabled=true);try{if(pollPromise)await pollPromise;else if(recordsPromise)await recordsPromise;await work();}catch(error){message(error.message,true);}finally{busy=false;document.querySelectorAll('button').forEach(button=>button.disabled=false);updateActionState();}}
function randomKey(prefix){return prefix+'-'+crypto.randomUUID();}
function element(tag,text,className=''){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function focusControl(node,key){node.dataset.focusKey=key;return node;}
function renderIfChanged(key,payload,render){const signature=JSON.stringify(payload);if(renderSignatures.get(key)===signature)return false;const focusKey=document.activeElement?.dataset?.focusKey||null;render(payload);renderSignatures.set(key,signature);if(focusKey){const target=[...document.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===focusKey);if(target&&!target.disabled)target.focus({preventScroll:true});}return true;}
function fmtBytes(value){const bytes=Number(value);if(!Number.isFinite(bytes))return '未知大小';if(bytes<1024)return bytes+' B';if(bytes<1024**2)return (bytes/1024).toFixed(1)+' KiB';return (bytes/1024**2).toFixed(1)+' MiB';}
function fmtTime(ms){const seconds=Math.max(0,Number(ms)||0)/1000;return seconds.toFixed(3)+' 秒';}
function addSegment(segment={start_ms:0,end_ms:10000,label:''}){const index=$('segments').children.length,row=element('div',undefined,'segment-row item');row.dataset.segment='';const title=element('div',undefined,'row-spread'),heading=element('h3','分段 '+(index+1));const remove=element('button','移除','secondary');remove.type='button';remove.addEventListener('click',()=>{row.remove();renumberSegments();markDirty();});title.append(heading,remove);row.append(title);const grid=element('div',undefined,'segment-fields');for(const spec of [{key:'start',label:'开始（秒）',value:Number(segment.start_ms||0)/1000},{key:'end',label:'结束（秒）',value:Number(segment.end_ms||10000)/1000}]){const wrap=element('div'),label=element('label',spec.label);const input=document.createElement('input');input.type='number';input.min='0';input.step='0.001';input.value=String(spec.value);input.dataset[spec.key]='';label.append(input);const use=element('button','取当前时间','secondary');use.type='button';use.addEventListener('click',()=>{input.value=$('preview').currentTime.toFixed(3);markDirty();});wrap.append(label,use);grid.append(wrap);}const label=element('label','片段名称（可选）');const input=document.createElement('input');input.maxLength=80;input.value=segment.label||'';input.dataset.label='';label.append(input);row.append(grid,label);row.querySelectorAll('input').forEach(node=>node.addEventListener('input',markDirty));$('segments').append(row);renumberSegments();}
function renumberSegments(){[...$('segments').children].forEach((row,index)=>row.querySelector('h3').textContent='分段 '+(index+1));}
function markDirty(){if(selectedProject)$('draft-state').textContent='有未保存更改';}
function readRecipe(){const segments=[...document.querySelectorAll('[data-segment]')].map((row,index)=>({start_ms:Math.round(Number(row.querySelector('[data-start]').value)*1000),end_ms:Math.round(Number(row.querySelector('[data-end]').value)*1000),label:row.querySelector('[data-label]').value.trim()||('片段 '+(index+1))}));if(!segments.length)throw new Error('请至少添加一个分段。');for(const [index,segment] of segments.entries())if(!Number.isSafeInteger(segment.start_ms)||!Number.isSafeInteger(segment.end_ms)||segment.start_ms<0||segment.end_ms<=segment.start_ms)throw new Error('分段 '+(index+1)+' 的起止时间无效。');let cover=null;if($('cover-enabled').checked){cover={timestamp_ms:Math.round(Number($('cover-at').value)*1000),aspect_ratio:$('cover-aspect').value,title:$('cover-title').value.trim(),subtitle:''};if(!Number.isSafeInteger(cover.timestamp_ms)||cover.timestamp_ms<0)throw new Error('封面抽帧时间无效。');}return {segments,cover,translation:null,dubbing:null};}
function loadRecipe(recipe){$('segments').replaceChildren();for(const segment of recipe?.segments||[])addSegment(segment);if(!$('segments').children.length)addSegment();$('cover-enabled').checked=!!recipe?.cover;$('cover-controls').hidden=!recipe?.cover;if(recipe?.cover){$('cover-at').value=String(Number(recipe.cover.timestamp_ms)/1000);if([...$('cover-aspect').options].some(option=>option.value===recipe.cover.aspect_ratio))$('cover-aspect').value=recipe.cover.aspect_ratio;$('cover-title').value=recipe.cover.title||'';}}
function projectId(project){return project.id||project.project_id;}
function renderProjects(projects){const host=$('projects');host.replaceChildren();if(!projects.length){host.append(element('p','尚无编辑项目。请从下载成品进入编辑。','muted'));return;}for(const project of projects){const id=projectId(project),item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div');copy.append(element('h3',project.name||'未命名项目'),element('p',(project.source_name||'已登记视频')+' · '+fmtBytes(project.source_size||project.size),'muted small'));const select=focusControl(element('button',selectedProject&&projectId(selectedProject)===id?'正在编辑':'打开项目',selectedProject&&projectId(selectedProject)===id?'secondary':''),'project:'+id+':open');select.type='button';select.addEventListener('click',()=>mutate(()=>selectProject(project)));head.append(copy,select);item.append(head);host.append(item);}}
async function selectProject(project){const id=projectId(project),generation=++projectSelectionGeneration;selectedProject=project;const nextDraft=await api('/projects/'+encodeURIComponent(id)+'/draft');if(generation!==projectSelectionGeneration||id!==(selectedProject&&projectId(selectedProject)))return;draft=nextDraft;$('workspace').hidden=false;$('project-summary').textContent=(project.name||'未命名项目')+' · '+(project.source_name||'视频')+' · 源 SHA-256 '+String(project.source_sha256||'').slice(0,16)+'…';$('preview').src='/api/v1/edits/projects/'+encodeURIComponent(id)+'/source';loadRecipe(draft.recipe);$('draft-state').textContent='草稿 v'+draft.version+' 已保存';if(recordsPromise)try{await recordsPromise;}catch{}await refreshRecords();}
async function saveDraft(){if(!selectedProject)throw new Error('请先打开项目。');const payload={expected_version:draft.version,recipe:readRecipe(),idempotency_key:randomKey('draft')};draft=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/draft',{method:'PUT',body:JSON.stringify(payload)});$('draft-state').textContent='草稿 v'+draft.version+' 已保存';message('编辑草稿已保存；尚未开始处理。');return draft;}
function renderCapabilities(rows){const host=$('capabilities');host.replaceChildren();for(const row of rows){const card=element('article',undefined,'capability-card');card.append(element('h3',row.label||row.operation));const status=element('p',row.status==='ready'?'本机可用':row.status==='blocked'?'尚不可用':'待验收','status-value');card.append(status,element('p',row.description||row.reason||row.reason_code||'','muted small'));if(row.provider_id)card.append(element('p','Provider：'+row.provider_id+(row.model_id?' · '+row.model_id:''),'mono small'));host.append(card);}if(!rows.length)host.append(element('p','能力清单暂不可用。','muted'));}
function renderPlans(plans){const host=$('plans');host.replaceChildren();if(!plans.length){host.append(element('p','尚无处理计划。','muted'));return;}for(const plan of plans){const item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div'),state=plan.state||'review',key='plan:'+plan.id+':';copy.append(element('h3','计划 '+String(plan.id).slice(0,8)),element('p',(stateNames[state]||state)+' · 草稿 v'+(plan.draft_version||plan.revision||'?')+(plan.progress!==undefined?' · '+Math.round(Number(plan.progress)*100)+'%':''),'muted'));head.append(copy);const actions=element('div',undefined,'row');if(state==='review'){const confirm=focusControl(element('button','确认并开始本地处理'),key+'confirm');confirm.type='button';confirm.addEventListener('click',()=>mutate(async()=>{await api('/plans/'+encodeURIComponent(plan.id)+'/confirm',{method:'POST'});message('处理计划已确认并排队。');await refreshRecords();}));actions.append(confirm);}if(['queued','running'].includes(state)){const cancel=focusControl(element('button','取消','secondary'),key+'cancel');cancel.type='button';cancel.addEventListener('click',()=>mutate(async()=>{await api('/plans/'+encodeURIComponent(plan.id)+'/cancel',{method:'POST'});message('已请求取消处理。');await refreshRecords();}));actions.append(cancel);}if(['failed','canceled'].includes(state)){const retry=focusControl(element('button','创建重试计划','secondary'),key+'retry');retry.type='button';retry.addEventListener('click',()=>mutate(async()=>{await api('/plans/'+encodeURIComponent(plan.id)+'/retry',{method:'POST'});message('已创建新的待核对重试计划。');await refreshRecords();}));actions.append(retry);}if(actions.children.length)head.append(actions);item.append(head);if(plan.code)item.append(element('p',codeNames[plan.code]||plan.code,'notice small'));item.append(element('p','Recipe SHA-256：'+String(plan.recipe_sha256||plan.recipe_digest||'').slice(0,24)+'…','mono small'));host.append(item);}}
function renderOutputs(outputs){const host=$('outputs'),kindNames={segment:'视频片段',cover:'封面',caption:'字幕',audio:'配音音频',dubbed_video:'配音视频'};host.replaceChildren();if(!outputs.length){host.append(element('p','尚无编辑成品。','muted'));return;}for(const output of outputs){const card=element('article',undefined,'output-card'),kind=output.kind||output.media_kind,key='output:'+output.id+':';card.append(element('h3',output.name||('编辑成品 '+String(output.id).slice(0,8))),element('p',(kindNames[kind]||'编辑成品')+' · '+fmtBytes(output.size_bytes||output.size)+(output.duration_ms?' · '+fmtTime(output.duration_ms):''),'muted small'));if(kind==='cover'){const image=document.createElement('img');image.src='/api/v1/edits/assets/'+encodeURIComponent(output.id)+'/content';image.alt=output.name||'编辑封面预览';image.loading='lazy';card.prepend(image);}const actions=element('div',undefined,'row');const download=focusControl(document.createElement('a'),key+'download');download.className='button-link secondary';download.href='/api/v1/edits/assets/'+encodeURIComponent(output.id)+'/content';download.download='';download.textContent='下载成品';actions.append(download);if(['segment','dubbed_video'].includes(kind)){const upload=focusControl(document.createElement('a'),key+'upload');upload.className='button-link';upload.href='/uploads?edit_output_id='+encodeURIComponent(output.id);upload.textContent='用于上传';actions.append(upload);}card.append(actions,element('p','SHA-256 '+String(output.sha256||'').slice(0,20)+'…','mono small'));host.append(card);}}
function currentProjectId(){return selectedProject?projectId(selectedProject):null;}
async function refreshRecords(){if(recordsPromise)return recordsPromise;const capturedGeneration=projectSelectionGeneration,capturedProjectId=currentProjectId(),projectParam=capturedProjectId?'?project_id='+encodeURIComponent(capturedProjectId):'';const work=(async()=>{const [projects,plans,outputs]=await Promise.all([api('/projects'),api('/plans'+projectParam),api('/assets'+projectParam)]);if(capturedGeneration!==projectSelectionGeneration||capturedProjectId!==currentProjectId())return false;renderIfChanged('projects',{selected_project_id:capturedProjectId,items:projects},value=>renderProjects(value.items));renderIfChanged('plans',plans,renderPlans);renderIfChanged('outputs',outputs,renderOutputs);return true;})();recordsPromise=work;try{return await work;}finally{if(recordsPromise===work)recordsPromise=null;}}
async function refresh(){if(refreshPromise)return refreshPromise;const work=(async()=>{const [capabilities]=await Promise.all([api('/capabilities'),refreshRecords()]);renderIfChanged('capabilities',capabilities,renderCapabilities);message('编辑器已连接。');})();refreshPromise=work;try{return await work;}finally{if(refreshPromise===work)refreshPromise=null;}}
function pageVisible(){return pageActive&&!document.hidden;}
function schedulePoll(){if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=pageVisible()?setTimeout(poll,2000):null;}
async function poll(){if(pollPromise)return pollPromise;if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;if(!pageVisible()){schedulePoll();return;}const work=(async()=>{try{if(!busy)await refreshRecords();}catch(error){message('状态刷新失败：'+error.message,true);}})();pollPromise=work;try{return await work;}finally{if(pollPromise===work)pollPromise=null;schedulePoll();}}
function updateActionState(){$('create-plan').disabled=busy||!selectedProject;$('draft-form').querySelector('button[type="submit"]').disabled=busy||!selectedProject;}
$('add-segment').addEventListener('click',()=>{addSegment();markDirty();});
$('cover-enabled').addEventListener('change',()=>{$('cover-controls').hidden=!$('cover-enabled').checked;markDirty();});
$('cover-current').addEventListener('click',()=>{$('cover-at').value=$('preview').currentTime.toFixed(3);markDirty();});
$('cover-controls').addEventListener('input',markDirty);
$('draft-form').addEventListener('submit',event=>{event.preventDefault();if(event.isComposing)return;mutate(saveDraft);});
$('create-plan').addEventListener('click',()=>mutate(async()=>{await saveDraft();const plan=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/plans',{method:'POST',body:JSON.stringify({expected_version:draft.version,idempotency_key:randomKey('plan')})});message('已生成待核对计划 '+String(plan.id).slice(0,8)+'；确认前不会开始处理。');await refreshRecords();}));
$('refresh').addEventListener('click',()=>mutate(refresh));
const assetId=new URL(location.href).searchParams.get('asset_id');if(assetId&&/^[0-9a-f-]{36}$/.test(assetId)){$('asset-import').hidden=false;$('import-asset').addEventListener('click',()=>mutate(async()=>{const project=await api('/projects/assets/'+encodeURIComponent(assetId),{method:'POST',body:JSON.stringify({name:$('project-name').value.trim()||'新编辑项目',idempotency_key:randomKey('project')})});$('asset-import').hidden=true;message('下载成品已复制到编辑域，原件保持不变。');await selectProject(project);}));}
window.addEventListener('pagehide',()=>{pageActive=false;schedulePoll();});
window.addEventListener('pageshow',event=>{pageActive=true;if(event.persisted)return poll();schedulePoll();});
document.addEventListener('visibilitychange',()=>{if(document.hidden){schedulePoll();return;}return poll();});
addSegment();updateActionState();
(async()=>{try{csrf=(await api('/session')).csrf_token;await refresh();}catch(error){message('编辑器连接失败：'+error.message,true);}finally{schedulePoll();}})();
  </script>
</body>
</html>'''
