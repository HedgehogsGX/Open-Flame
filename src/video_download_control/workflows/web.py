"""Single-URL workflow page built on the shared Open-Flame design system."""

WORKFLOW_HTML = r'''<!doctype html>
<html lang="zh-CN" class="no-js" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Open-Flame · 自动流程</title>
  <link rel="stylesheet" href="/assets/open-flame.css">
  <script src="/assets/open-flame-shell.js" defer></script>
</head>
<body class="workflow-page">
  <header class="topbar-shell"><div class="topbar">
    <a class="brand" href="/" aria-label="Open-Flame 下载首页"><span class="brand-mark" aria-hidden="true">OF</span><span>Open-Flame</span></a>
    <nav class="primary-nav" aria-label="主要功能"><a class="nav-link" href="/">下载</a><a class="nav-link" href="/edits">编辑</a><a class="nav-link" href="/uploads">上传</a><a class="nav-link" href="/workflows" aria-current="page">自动流程</a></nav>
    <label class="theme-control"><span>外观</span><select data-of-theme aria-label="界面外观"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
  </div></header>
  <main class="of-shell">
    <header class="page-header"><p class="eyebrow">URL 到三平台的可恢复编排</p><h1>一次设置，自动推进每个阶段</h1><p class="lede">输入一个网址，按已保存的编辑与投稿参数完成下载、翻译、配音、封面和上传。每个跨域副本都会校验，重启后从持久状态继续。</p><p class="notice page-note">云端 AI 会在单独勾选数据外发授权后启动；自动确认只适用于本次流程，应用重启或重试后仍会再次停下。平台返回 submitted 只代表提交完成；公开、审核与定时发布结果仍以平台后台为准。</p></header>
    <section class="status-strip" aria-label="自动流程摘要"><div class="status-tile"><p class="status-label">核心</p><p class="status-value">轻量状态机</p></div><div class="status-tile"><p class="status-label">AI</p><p class="status-value">独立运行时</p></div><div class="status-tile"><p class="status-label">发布</p><p class="status-value">Bilibili · 抖音 · 视频号</p></div></section>
    <p id="message" role="status" aria-live="polite">正在连接自动流程…</p>
    <section class="card primary-card" aria-labelledby="create-heading"><div class="card-header"><div><p class="section-index">新建流程</p><h2 id="create-heading">来源、处理与投稿参数</h2><p class="muted">所有参数会形成不可变快照；同一请求键重放不会创建第二条流程。</p></div><button id="refresh" class="secondary" type="button">刷新</button></div>
      <form id="workflow-form">
        <label for="source-url">视频网址</label><input id="source-url" type="url" maxlength="4096" required placeholder="https://…">
        <label for="workflow-name">流程名称</label><input id="workflow-name" maxlength="160" required value="自动翻译配音发布">
        <label for="preset-select">参数预设</label><div class="row"><select id="preset-select" aria-describedby="preset-help" disabled><option value="">不使用预设</option></select><button id="save-preset" class="secondary" type="button">保存当前参数</button></div><p id="preset-help" class="muted small">选择预设后可修改本次参数；保存不会启动流程。网址、密钥和登录会话不会写入预设。</p><details id="preset-details" hidden><summary>预设中的独立投稿参数</summary><div id="preset-targets"></div></details>
        <label for="credential-mode">下载登录方式</label><select id="credential-mode"><option value="use_default">使用已配置的默认登录</option><option value="anonymous">匿名下载</option></select>
        <fieldset><legend>编辑与 AI</legend>
          <div class="diagnostic-grid"><div class="diagnostic-panel"><h3>分段与封面</h3><label class="choice"><input id="segment-enabled" type="checkbox" checked><span>生成一个视频分段</span></label><label for="segment-start">开始（秒）</label><input id="segment-start" type="number" min="0" step="0.001" value="0"><label for="segment-end">结束（秒）</label><input id="segment-end" type="number" min="0.1" step="0.001" value="60"><label class="choice"><input id="cover-enabled" type="checkbox" checked><span>生成封面</span></label><label for="cover-ratio">封面比例</label><select id="cover-ratio"><option value="4:3">4:3 横版</option><option value="3:4">3:4 竖版</option><option value="16:9">16:9 横版</option><option value="9:16">9:16 竖版</option><option value="1:1">1:1 方形</option><option value="source">原始比例</option></select><label for="cover-time">封面取帧（秒）</label><input id="cover-time" type="number" min="0" step="0.001" value="0"><label class="choice"><input id="cover-use-title" type="checkbox" checked><span>封面使用投稿标题</span></label><label for="cover-title">独立封面标题（可留空）</label><input id="cover-title" maxlength="120"><label for="cover-subtitle">封面副标题</label><input id="cover-subtitle" maxlength="240"><label for="segment-label">分段名称</label><input id="segment-label" maxlength="120" value="主视频"></div>
          <div class="diagnostic-panel"><h3>翻译与配音</h3><p id="ai-help" class="muted small">正在读取隔离 AI runtime…</p><label class="choice"><input id="translation-enabled" type="checkbox"><span>自动听写并翻译</span></label><label for="transcription-engine">听写模型</label><select id="transcription-engine" disabled><option value="">尚无可用模型</option></select><label for="translation-engine">翻译模型</label><select id="translation-engine" disabled><option value="">尚无可用模型</option></select><label for="source-language">原文语言</label><select id="source-language"><option value="auto">自动识别</option><option value="zh-CN">中文</option><option value="en">English</option></select><label for="target-language">目标语言</label><select id="target-language"><option value="zh-CN">中文</option><option value="en">English</option></select><label class="choice"><input id="dubbing-enabled" type="checkbox"><span>使用标准音色自动配音</span></label><label for="speech-engine">配音模型</label><select id="speech-engine" disabled><option value="">尚无可用模型</option></select><label for="dubbing-language">配音语言</label><select id="dubbing-language"><option value="zh-CN">中文</option><option value="en">English</option></select><label for="voice-id">标准音色</label><select id="voice-id" disabled><option value="">尚无可用标准音色</option></select><label class="choice"><input id="replace-audio" type="checkbox"><span>替换原声；未勾选时把原声压低到 22% 后叠加配音</span></label><p id="ai-egress-summary" class="notice small">启用 AI 后会在这里显示精确 runtime、模型修订、硬预算与外发范围。</p><label class="choice"><input id="ai-egress" type="checkbox" disabled><span>若上述授权包含云端能力，我已核对并接受数据外发范围与潜在费用</span></label></div></div>
        </fieldset>
        <fieldset><legend>上传账号与内容</legend><p id="account-help" class="muted small">正在读取已连接账号…</p><div id="accounts"></div><label for="title">标题</label><input id="title" maxlength="100" required><label for="description">简介</label><textarea id="description" maxlength="2000"></textarea><label for="tags">标签（逗号分隔）</label><input id="tags" maxlength="220"><label for="category-id">Bilibili 分区 ID</label><input id="category-id" type="number" min="1" max="10000"><label for="copyright">Bilibili 来源</label><select id="copyright"><option value="1">原创</option><option value="2">转载</option></select><label for="source-credit">转载来源（原创时留空）</label><input id="source-credit" maxlength="200"><label for="mode">执行方式</label><select id="mode"><option value="publish">投稿发布</option><option value="draft">保存平台草稿</option></select><label for="publish-at">定时发布时间（可选）</label><input id="publish-at" type="datetime-local"><label for="short-title">视频号短标题（7–15 字，可选）</label><input id="short-title" minlength="7" maxlength="15"><label class="choice"><input id="ai-label" type="checkbox" checked><span>标记含 AI 生成内容</span></label></fieldset>
        <fieldset><legend>自动确认</legend><label class="choice"><input id="auto-edit" type="checkbox" checked><span>创建流程时预先授权自动开始编辑与 AI 处理</span></label><label class="choice"><input id="auto-upload" type="checkbox"><span>创建流程时预先授权所选账号自动投稿</span></label><p class="muted small">不勾选时，流程会停在核对节点，可在下方确认。未知结果永远停下，不自动重试。</p></fieldset>
        <button type="submit">创建并开始自动流程</button>
      </form>
    </section>
    <section class="card" aria-labelledby="records-heading"><div class="card-header"><div><p class="section-index">持续状态</p><h2 id="records-heading">流程记录</h2><p class="muted">页面轮询只刷新记录，不改动上方输入。</p></div></div><div id="workflows"><p class="muted">尚无流程。</p></div></section>
    <p class="page-footer">Open-Flame 0.28.0 · 自动流程使用独立 Workflow Schema 1</p>
  </main>
  <script>
'use strict';
const $=id=>document.getElementById(id);let csrf='',busy=false,pollTimer=null,accounts=[],aiCapabilities=[],workflowPresets=[],appliedPreset=null,presetParameters=null,aiEnginesLoaded=false;const changedUploadFields=new Set();
const stateNames={created:'已创建',downloading:'下载中',preparing_edit:'准备编辑',awaiting_ai_review:'等待 AI 结果核对',awaiting_edit_confirmation:'等待编辑确认',rendering:'编辑处理中',preparing_upload:'准备上传草稿',awaiting_upload_confirmation:'等待上传确认',uploading:'上传中',completed:'流程已结束',attention_required:'需要处理',canceled:'已取消'};
const codeNames={submission_acknowledged:'平台已确认接收投稿；审核、定时执行与公开状态仍须后台核对',platform_draft_saved:'视频号平台草稿已保存，尚未发布',upload_account_invalid:'上传账号登录态已失效；请重新登录并重建自动流程',upload_outcome_invalid:'上传结果与本次发布方式不一致，请到平台后台核对',upload_outcome_mixed:'所选账号返回了不同类型的上传结果，请逐个平台核对',upload_outcome_missing:'上传结果缺少可确认的类型，请到平台后台核对',download_multiple_assets:'下载产生多个视频，请手动选择',edit_multiple_outputs:'编辑产生多个视频，请手动选择',upload_unknown:'平台结果未知，请到后台核对',ai_runtime_missing:'AI 运行时未安装',ai_runtime_invalid:'AI 运行时校验失败',ai_provider_auth_missing:'AI provider 凭据未配置',ai_transcription_confirmation_required:'请确认开始自动听写',ai_transcription_review_required:'请核对并批准听写时间轴',ai_translation_confirmation_required:'请确认开始自动翻译',ai_restart_confirmation_required:'应用已重启，请重新确认当前 AI 步骤',ai_retry_confirmation_required:'已创建 AI 重试任务，请明确确认后再执行',restart_confirmation_required:'应用已重启，请重新确认编辑',edit_restart_confirmation_required:'应用已重启，请重新确认编辑',render_retry_confirmation_required:'已创建新的编辑计划，请确认重试',upload_restart_confirmation_required:'应用已重启，请重新确认投稿',upload_retry_confirmation_required:'已找到上传重试任务，请明确确认后再投稿',account_session_changed:'所选上传账号的登录会话已变化，请重建流程',ai_speech_timing_overflow:'部分配音超过字幕时间槽，请到编辑页调整文本或音色',ai_timeline_segment_empty:'所选分段内没有已批准的字幕内容',processor_not_configured:'FFmpeg 工具尚未就绪',workflow_domain_failed:'流程步骤执行失败'};
Object.assign(codeNames,{submission_and_draft_acknowledged:'投稿平台已确认接收，草稿平台已确认保存；审核、定时执行与公开状态仍须后台核对'});
Object.assign(codeNames,{ai_authorization_required:'此旧流程未绑定精确 AI runtime、模型修订与硬预算，请按当前能力重建流程',ai_authorization_binding_required:'此旧流程未绑定精确 AI runtime、模型修订与硬预算，请按当前能力重建流程',ai_authorization_changed:'AI runtime、模型修订或硬预算已变化，请刷新能力并重建流程',ai_authorization_invalid:'AI 授权数据无效，请重建流程',ai_budget_exceeded:'AI 操作超过本次授权的硬预算，请缩短分段或减少文字后重建流程',ai_budget_invalid:'AI 硬预算数据无效，请重建流程',workflow_profile_confirmation_required:'此操作需要确认当前流程参数快照',workflow_profile_changed:'流程参数快照已变化，请刷新后重新核对',invalid_workflow_profile_confirmation:'流程参数确认摘要无效，请刷新后重试',workflow_domain_data_invalid:'流程中的 AI 授权数据无效，请重建流程'});
Object.assign(codeNames,{ai_remote_result_unknown:'远程 AI 结果无法确认；请先到编辑页核对调用账本',ai_remote_retry_blocked:'远程调用账本已阻止重试；请到编辑页查看固定核对结论',ai_remote_reconciliation_required:'必须先到编辑页核对远程调用结果',ai_remote_not_accepted:'已确认远程服务没有接受本次调用，可以建立待确认重试',ai_remote_accepted_without_result:'已确认远程服务接受调用但没有可用结果；当前流程不允许重试',ai_remote_abandoned:'本次远程调用已放弃；当前流程不允许重试'});
Object.assign(codeNames,{workflow_ai_segments_must_be_contiguous:'自动 AI 多分段必须首尾连续，避免外发未选择的音频',workflow_output_count_invalid:'自动流程支持 1–10 个分段',workflow_requires_single_video_output:'旧流程只支持一个视频输出，请重新创建',workflow_progress_limit:'流程同步步骤超过安全上限，请刷新后重试'});
const ledgerReviewCodes=new Set(['ai_remote_result_unknown','ai_remote_retry_blocked','ai_remote_reconciliation_required','ai_remote_accepted_without_result','ai_remote_abandoned']);
Object.assign(codeNames,{workflow_preset_invalid:'预设参数无效，请核对后重新保存',workflow_preset_not_found:'预设不存在，请刷新列表',workflow_preset_conflict:'预设数量已达上限',workflow_preset_authorization_changed:'预设的 AI 能力已变化，请核对后保存新预设',workflow_preset_storage_unavailable:'预设文件不可读或校验失败，请检查本地存储'});
function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('danger',error);}
const pendingKeys=new Map();
function key(prefix){return prefix+'-'+crypto.randomUUID();}
function canonical(value){if(Array.isArray(value))return value.map(canonical);if(value&&typeof value==='object'){const output={};for(const name of Object.keys(value).sort())output[name]=canonical(value[name]);return output;}return value;}
async function intentDigest(value){const bytes=new TextEncoder().encode(JSON.stringify(canonical(value)));if(crypto.subtle){const digest=await crypto.subtle.digest('SHA-256',bytes);return [...new Uint8Array(digest)].map(item=>item.toString(16).padStart(2,'0')).join('');}let hash=2166136261;for(const byte of bytes){hash^=byte;hash=Math.imul(hash,16777619);}return (hash>>>0).toString(16).padStart(8,'0')+'-'+bytes.length;}
async function pendingWorkflowKey(intent){const storageKey='open-flame-workflow-pending-v2-'+await intentDigest(intent);let requestKey='';try{requestKey=sessionStorage.getItem(storageKey)||pendingKeys.get(storageKey)||'';}catch{requestKey=pendingKeys.get(storageKey)||'';}if(!/^workflow-[0-9a-f-]{36}$/.test(requestKey)){requestKey=key('workflow');try{sessionStorage.setItem(storageKey,requestKey);pendingKeys.delete(storageKey);}catch{pendingKeys.set(storageKey,requestKey);}}return {requestKey,storageKey};}
function clearPendingWorkflowKey(storageKey){pendingKeys.delete(storageKey);try{sessionStorage.removeItem(storageKey);}catch{}}
async function api(path,options={}){const headers=new Headers(options.headers||{});if(options.method&&options.method!=='GET')headers.set('X-Workflow-CSRF',csrf);if(options.body)headers.set('Content-Type','application/json');const response=await fetch('/api/v1/workflows'+path,{...options,headers});let data=null;try{data=await response.json();}catch{}if(!response.ok){const code=data?.detail||'request_failed';throw new Error(codeNames[code]||code);}return data;}
function el(tag,text,className=''){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function selectedAccounts(){return [...document.querySelectorAll('[data-account]:checked')].map(node=>node.value);}
function renderAccounts(){const host=$('accounts');host.replaceChildren();const ready=accounts.filter(item=>item.lifecycle_state!=='disconnected'&&item.auth_state==='ready');for(const account of ready){const label=el('label',undefined,'choice'),input=document.createElement('input');input.type='checkbox';input.value=account.id;input.dataset.account='';label.append(input,el('span',(account.name||'未命名')+' · '+({bilibili:'Bilibili',douyin:'抖音',tencent:'视频号'}[account.platform]||account.platform)));host.append(label);}if(!ready.length)host.append(el('p','没有 ready 上传账号；请先到上传页扫码登录。','notice'));$('account-help').textContent=ready.length?'选择本次流程使用的账号。':'需要先连接账号。';}
const sha256Pattern=/^[0-9a-f]{64}$/;
function authorizationReady(row){const auth=row?.authorization,expectedOperation=row?.operation==='dub'?'synthesize':row?.operation;return !!row&&sha256Pattern.test(row.authorization_sha256||'')&&auth&&typeof auth==='object'&&auth.operation===expectedOperation&&auth.provider_id===row.provider_id&&auth.model_id===row.model_id&&sha256Pattern.test(auth.manifest_sha256||'')&&typeof auth.model_revision==='string'&&auth.model_revision&&auth.limits&&typeof auth.limits==='object';}
function capabilityKey(row){return JSON.stringify([row?.provider_id||'',row?.model_id||'',row?.authorization_sha256||'']);}
function selectedCapability(id,operation){const value=$(id).value;return aiCapabilities.find(item=>item.operation===operation&&capabilityKey(item)===value&&authorizationReady(item))||null;}
function selectedEngine(id,operation){const row=selectedCapability(id,operation);if(!row)throw new Error('所选 AI 能力没有可确认的精确授权，请刷新后重选');return {provider:row.provider_id,model:row.model_id,authorization:row.authorization,authorization_sha256:row.authorization_sha256};}
function egressNames(values){const names={audio:'音频',text:'文字'};return values.map(value=>names[value]||value).join('、');}
function authorizationSummary(label,row){if(!authorizationReady(row))return label+'：缺少精确授权';const auth=row.authorization,limits=auth.limits||{},parts=[];if(limits.max_audio_ms)parts.push('音频≤'+Math.round(limits.max_audio_ms/60000)+'分钟');if(limits.max_audio_bytes)parts.push('音频≤'+(limits.max_audio_bytes/1048576).toFixed(0)+' MiB');if(limits.max_cues)parts.push('字幕≤'+limits.max_cues+'段');if(limits.max_input_characters)parts.push('文字≤'+limits.max_input_characters+'字符');if(limits.max_requests)parts.push('调用预算单位≤'+limits.max_requests);const scope=auth.execution==='remote'?'外发 '+(egressNames(Array.isArray(auth.data_egress)?auth.data_egress:[])||'范围未声明'):'本地执行，不外发';return label+'：'+row.provider_id+' / '+row.model_id+' @ '+auth.model_revision+'；manifest '+auth.manifest_sha256.slice(0,12)+'…；'+parts.join('、')+'；'+scope;}
function renderAiEgress(){const enabled=$('translation-enabled').checked&&$('dubbing-enabled').checked,consent=$('ai-egress');if(!enabled){consent.disabled=true;consent.checked=false;$('ai-egress-summary').textContent='AI 默认关闭；启用听写、翻译和配音后再核对精确授权。';return;}const transcribe=selectedCapability('transcription-engine','transcribe'),translate=selectedCapability('translation-engine','translate'),dub=selectedCapability('speech-engine','dub'),rows=[transcribe,translate,dub];if(rows.some(item=>!item)){consent.disabled=true;consent.checked=false;$('ai-egress-summary').textContent='所选能力缺少精确 runtime 授权，不能创建流程；请刷新并重新选择。';return;}const requiresEgress=rows.some(item=>item.authorization.execution==='remote');consent.disabled=!requiresEgress;if(!requiresEgress)consent.checked=false;const range=$('segment-enabled').checked?$('segment-start').value+'–'+$('segment-end').value+' 秒':'完整视频';$('ai-egress-summary').textContent='本次听写范围：'+range+'。'+[authorizationSummary('听写',transcribe),authorizationSummary('翻译',translate),authorizationSummary('配音',dub)].join(' ')+(requiresEgress?' 请核对并确认云端外发。':' 三项均在本地执行，无需数据外发确认。');}
function renderVoiceOptions(){const select=$('voice-id'),prior=select.value||select.dataset.presetVoice||'';select.replaceChildren();const capability=selectedCapability('speech-engine','dub');const voices=Array.isArray(capability?.voices)?capability.voices.filter(item=>item&&typeof item.id==='string'&&item.id&&item.is_clone!==true):[];for(const voice of voices){const option=document.createElement('option');option.value=voice.id;option.textContent=voice.label||voice.id;select.append(option);}select.disabled=!voices.length;if(prior&&[...select.options].some(option=>option.value===prior))select.value=prior;else if(prior){const unavailable=el('option','原音色不可用，请重新选择');unavailable.value='';select.append(unavailable);select.value='';}else if([...select.options].some(option=>option.value==='marin'))select.value='marin';if(!voices.length){const option=document.createElement('option');option.value='';option.textContent='尚无可用标准音色';select.append(option);}renderAiEgress();}
function renderAiEngines(){const specs=[['transcription-engine','transcribe','听写'],['translation-engine','translate','翻译'],['speech-engine','dub','配音']];let available=0;for(const [id,operation,label] of specs){const select=$(id),prior=select.value,rows=aiCapabilities.filter(item=>item.operation===operation&&['ready','unverified'].includes(item.status)&&typeof item.provider_id==='string'&&typeof item.model_id==='string'&&authorizationReady(item));if(operation==='transcribe')rows.sort((left,right)=>Number(right.model_id==='whisper-1')-Number(left.model_id==='whisper-1'));select.replaceChildren();const placeholder=el('option','请选择当前可用模型');placeholder.value='';select.append(placeholder);for(const row of rows){const option=document.createElement('option');option.value=capabilityKey(row);option.textContent=row.provider_id+' · '+row.model_id+' @ '+row.authorization.model_revision+' · '+row.authorization.manifest_sha256.slice(0,8)+(row.execution==='remote'?' · 云端':' · 本地');select.append(option);}select.disabled=!rows.length;if(aiEnginesLoaded)select.value=[...select.options].some(option=>option.value===prior)?prior:'';else if(rows.length)select.value=capabilityKey(rows[0]);if(rows.length)available++;else{const option=document.createElement('option');option.value='';option.textContent='尚无可用'+label+'模型';select.append(option);}}aiEnginesLoaded=true;renderVoiceOptions();const authBlocked=specs.every(([,operation])=>aiCapabilities.some(item=>item.operation===operation&&item.status==='blocked'&&item.reason_code==='ai_provider_auth_missing'));$('ai-help').textContent=available===3?'AI runtime、模型修订与硬预算已校验；真实调用仍可能受费用、网络或 provider 健康影响。':authBlocked?'AI runtime 完整性已校验；请配置 provider 凭据并重启应用后再启用云端 AI。':'请先安装并配置带精确授权、且同时支持听写、翻译和标准音色的隔离 AI runtime。';}
function recipe(){const segments=[];if($('segment-enabled').checked){const start=Math.round(Number($('segment-start').value)*1000),end=Math.round(Number($('segment-end').value)*1000);if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||end<=start)throw new Error('分段时间无效');segments.push({start_ms:start,end_ms:end,label:$('segment-label').value.trim()});}const cover=$('cover-enabled').checked?{timestamp_ms:Math.round(Number($('cover-time').value)*1000),aspect_ratio:$('cover-ratio').value,title:$('cover-use-title').checked?$('title').value.trim():$('cover-title').value.trim(),subtitle:$('cover-subtitle').value.trim()}:null;const translate=$('translation-enabled').checked,target=$('target-language').value,dub=$('dubbing-enabled').checked;if(dub&&!translate)throw new Error('自动配音需要同时启用听写与翻译');if(translate&&!dub)throw new Error('自动流程中的译文必须配音后才能进入上传；只导出字幕请使用编辑页');if(!dub&&segments.length!==1)throw new Error('未启用配音时必须选择一个上传分段');if(!segments.length&&!cover&&!translate)throw new Error('请至少启用一种处理');const translationEngine=translate?selectedEngine('translation-engine','translate'):null,speechEngine=dub?selectedEngine('speech-engine','dub'):null,voice=dub?$('voice-id').value:'';if(dub&&!voice)throw new Error('没有可用的标准音色');return {segments,cover,translation:translate?{enabled:true,source_language:$('source-language').value,target_language:target,provider:translationEngine.provider,model:translationEngine.model,state:'needs_review'}:null,dubbing:dub?{enabled:true,language:$('dubbing-language').value,provider:speechEngine.provider,model:speechEngine.model,voice,state:'needs_review',replace_original_audio:$('replace-audio').checked,authorization:speechEngine.authorization}:null};}
function textLength(value){return [...value].length;}
function localDateTimeKey(value){const pad=number=>String(number).padStart(2,'0');return value.getFullYear()+'-'+pad(value.getMonth()+1)+'-'+pad(value.getDate())+'T'+pad(value.getHours())+':'+pad(value.getMinutes());}
function scheduleFor(platform,raw){if(!raw)return {publish_at_unix:null,publish_timezone_offset_minutes:null};const parsed=new Date(raw),key=raw.slice(0,16);if(Number.isNaN(parsed.getTime())||localDateTimeKey(parsed)!==key)throw new Error('定时发布时间无效，可能落在本地夏令时跳时区间');for(let minutes=-180;minutes<=180;minutes+=30){if(!minutes)continue;const alternate=new Date(parsed.getTime()+minutes*60000);if(localDateTimeKey(alternate)===key&&alternate.getTimezoneOffset()!==parsed.getTimezoneOffset())throw new Error('该本地时间在夏令时切换时出现两次，请选择其他时间');}const lead=platform==='bilibili'?21900000:14700000;if(parsed.getTime()<=Date.now()+lead)throw new Error((platform==='bilibili'?'Bilibili':platform==='douyin'?'抖音':'视频号')+' 定时发布至少需提前 '+(platform==='bilibili'?'6 小时 5 分钟':'4 小时 5 分钟'));if(platform==='tencent'&&parsed.getMinutes()!==0)throw new Error('视频号定时发布只支持整点');if(platform==='tencent'&&parsed.getTime()>Date.now()+28*24*3600000)throw new Error('视频号定时发布最多可提前 28 天');return {publish_at_unix:Math.floor(parsed.getTime()/60000)*60,publish_timezone_offset_minutes:-parsed.getTimezoneOffset()};}
function upload(){
  const ids=selectedAccounts();
  if(!ids.length)throw new Error('请选择至少一个 ready 上传账号');
  const selected=ids.map(id=>accounts.find(item=>item.id===id));
  if(selected.some(item=>!item||item.lifecycle_state==='disconnected'||item.auth_state!=='ready'))throw new Error('所选账号状态已变化，请刷新后重新选择');
  if(presetParameters&&!changedUploadFields.has('accounts')&&presetParameters.profile.upload.account_ids.some(id=>!ids.includes(id)))throw new Error('预设账号不可用，请重新登录或明确修改账号选择');
  const title=$('title').value.trim(),description=$('description').value.trim(),tags=$('tags').value.split(/[,，]/).map(item=>item.trim()).filter(Boolean),mode=$('mode').value,shortTitle=$('short-title').value.trim(),sourceCredit=$('source-credit').value.trim(),category=$('category-id').value?Number($('category-id').value):null,copyright=Number($('copyright').value),rawSchedule=$('publish-at').value;
  if(!title)throw new Error('请填写标题');
  const fields={'title':'title','description':'description','tags':'tags','mode':'mode','category-id':'category_id','copyright':'copyright','source-credit':'source_credit'},target_overrides=[];
  for(const account of selected){
    const saved=presetParameters?.profile.upload.target_overrides?.find(item=>item.account_id===account.id),override=saved?JSON.parse(JSON.stringify(saved)):{account_id:account.id,mode};
    for(const [input,field] of Object.entries(fields))if(changedUploadFields.has(input))delete override[field];
    const effective={title,description,tags,mode,category_id:category,copyright,source_credit:sourceCredit,...override},platform=account.platform,platformName={bilibili:'Bilibili',douyin:'抖音',tencent:'视频号'}[platform];
    if(!effective.title||textLength(effective.title)>({bilibili:80,douyin:30,tencent:100}[platform]))throw new Error(platformName+' 标题为空或超过平台长度限制');
    if(effective.tags.length>10||new Set(effective.tags).size!==effective.tags.length||effective.tags.some(tag=>!tag||textLength(tag)>20||/[#＃\n\r\t]/.test(tag)))throw new Error('每个平台最多 10 个不重复标签，每个最多 20 字，标签中不要填写 #');
    if(effective.mode==='draft'&&platform!=='tencent')throw new Error('只有视频号支持保存平台草稿；Bilibili 与抖音需选择投稿发布');
    if(platform==='bilibili'){
      if(!effective.category_id||!Number.isSafeInteger(effective.category_id))throw new Error('Bilibili 投稿必须填写分区 ID');
      if(!effective.tags.length)throw new Error('Bilibili 投稿至少需要一个标签');
      if(effective.copyright===2&&!effective.source_credit)throw new Error('Bilibili 转载投稿必须填写来源');
      if(effective.copyright===1&&effective.source_credit)throw new Error('Bilibili 原创投稿请清空转载来源');
    }
    if(!saved||changedUploadFields.has('publish-at'))Object.assign(override,scheduleFor(platform,rawSchedule));
    const scheduled=override.publish_at_unix;
    if(effective.mode==='draft'&&scheduled)throw new Error('保存平台草稿时不能设置定时发布');
    if(scheduled){
      const lead=platform==='bilibili'?21900000:14700000;
      if(scheduled*1000<=Date.now()+lead)throw new Error(platformName+' 预设的发布时间已过期或提前量不足，请更新发布时间');
      if(platform==='tencent'&&scheduled*1000>Date.now()+28*24*3600000)throw new Error('视频号定时发布最多可提前 28 天');
    }
    const options={...override.platform_options};
    if(!saved||changedUploadFields.has('short-title')){
      delete options.short_title;
      if(platform==='tencent'&&shortTitle){if(textLength(shortTitle)<7||textLength(shortTitle)>15)throw new Error('视频号短标题需为 7–15 字');options.short_title=shortTitle;}
    }
    if(!saved||changedUploadFields.has('ai-label')){
      delete options.declaration;delete options.content_label;
      if($('ai-label').checked){if(platform==='douyin')options.declaration='内容由AI生成';if(platform==='tencent')options.content_label='含AI生成内容';}
    }
    if(Object.keys(options).length)override.platform_options=options;else delete override.platform_options;
    target_overrides.push(override);
  }
  return {account_ids:ids,title,description,tags,category_id:selected.some(item=>item.platform==='bilibili')?category:null,mode,copyright:selected.some(item=>item.platform==='bilibili')?copyright:null,source_credit:selected.some(item=>item.platform==='bilibili')?sourceCredit:'',target_overrides};
}
function workflowCapability(operation,provider,model,authorizationSha256,authorization){const row=aiCapabilities.find(item=>item.operation===operation&&item.provider_id===provider&&item.model_id===model&&item.authorization_sha256===authorizationSha256&&authorizationReady(item)&&['ready','unverified'].includes(item.status))||null;return row&&authorization&&typeof authorization==='object'&&JSON.stringify(canonical(row.authorization))===JSON.stringify(canonical(authorization))?row:null;}
function workflowAiAuthorization(item){const profile=item?.profile,ai=profile?.ai;if(!ai)return {enabled:false,bound:true,current:true,summary:'本流程未启用 AI。'};const editRecipe=profile?.edit_recipe||{},translation=editRecipe.translation||{},dubbing=editRecipe.dubbing||{},transcriptionAuthorization=ai.transcription_authorization,translationAuthorization=ai.translation_authorization,speechAuthorization=dubbing.authorization;const hasProfileDigest=sha256Pattern.test(item?.profile_sha256||''),hasDigests=sha256Pattern.test(ai.transcription_authorization_sha256||'')&&sha256Pattern.test(ai.translation_authorization_sha256||''),hasAuthorizations=transcriptionAuthorization&&typeof transcriptionAuthorization==='object'&&translationAuthorization&&typeof translationAuthorization==='object',hasSpeech=speechAuthorization&&typeof speechAuthorization==='object'&&speechAuthorization.operation==='synthesize'&&speechAuthorization.provider_id===dubbing.provider&&speechAuthorization.model_id===dubbing.model&&sha256Pattern.test(speechAuthorization.manifest_sha256||'')&&typeof speechAuthorization.model_revision==='string'&&speechAuthorization.model_revision&&speechAuthorization.limits&&typeof speechAuthorization.limits==='object';if(!hasProfileDigest||!hasDigests||!hasAuthorizations||!hasSpeech)return {enabled:true,bound:false,current:false,summary:'此旧流程未绑定三项完整 AI runtime、模型修订与硬预算，不能继续确认；请按当前能力重建流程。'};const transcribe=workflowCapability('transcribe',ai.transcription_provider,ai.transcription_model,ai.transcription_authorization_sha256,transcriptionAuthorization),translate=workflowCapability('translate',translation.provider,translation.model,ai.translation_authorization_sha256,translationAuthorization),speech=aiCapabilities.find(row=>row.operation==='dub'&&row.provider_id===dubbing.provider&&row.model_id===dubbing.model&&authorizationReady(row)&&['ready','unverified'].includes(row.status)&&JSON.stringify(canonical(row.authorization))===JSON.stringify(canonical(speechAuthorization)))||null;if(!transcribe||!translate||!speech)return {enabled:true,bound:true,current:false,summary:'此流程绑定的 AI runtime、模型修订或硬预算已不再可用，不能继续确认；请刷新能力或重建流程。'};return {enabled:true,bound:true,current:true,summary:'参数快照 '+item.profile_sha256.slice(0,12)+'…。'+[authorizationSummary('听写',transcribe),authorizationSummary('翻译',translate),authorizationSummary('配音',speech)].join(' ')};}
function render(items){
  const host=$('workflows');
  host.replaceChildren();
  if(!items.length){host.append(el('p','尚无流程。','muted'));return;}
  for(const item of items){
    const card=el('article',undefined,'item'),head=el('div',undefined,'row-spread'),copy=el('div'),binding=workflowAiAuthorization(item),confirmationReady=!binding.enabled||(binding.bound&&binding.current);
    copy.append(el('h3',item.name),el('p',(stateNames[item.state]||item.state)+' · 修订 '+item.revision,'muted'));
    head.append(copy);
    const actions=el('div',undefined,'row');
    if(item.state==='awaiting_ai_review'&&item.code&&confirmationReady){
      if(item.code.endsWith('_review_required')&&!item.code.includes('restart')){
        const link=el('a','在编辑页核对时间轴','button-link');
        link.href='/edits';
        actions.append(link);
      }else{
        const button=el('button','确认开始当前 AI 步骤');
        button.type='button';
        button.addEventListener('click',()=>mutate(()=>api('/'+item.id+'/confirm-ai',{method:'POST',body:JSON.stringify({expected_revision:item.revision,expected_profile_sha256:item.profile_sha256})})));
        actions.append(button);
      }
    }
    if(item.state==='awaiting_edit_confirmation'&&confirmationReady){
      const button=el('button','确认开始编辑');
      button.type='button';
      button.addEventListener('click',()=>mutate(()=>api('/'+item.id+'/confirm-edit',{method:'POST',body:JSON.stringify({expected_revision:item.revision,expected_profile_sha256:item.profile_sha256})})));
      actions.append(button);
    }
    if(item.state==='awaiting_upload_confirmation'){
      const button=el('button','确认所选账号上传');
      button.type='button';
      button.addEventListener('click',()=>mutate(()=>api('/'+item.id+'/confirm-upload',{method:'POST',body:JSON.stringify({expected_revision:item.revision})})));
      actions.append(button);
    }
    const needsLedgerReview=item.state==='attention_required'&&ledgerReviewCodes.has(item.code),aiRetry=confirmationReady&&!needsLedgerReview&&item.state==='attention_required'&&item.edit_project_id&&!item.edit_plan_id&&item.profile?.ai,renderRetry=confirmationReady&&!needsLedgerReview&&item.state==='attention_required'&&item.edit_plan_id&&!(item.edit_output_ids||[]).length&&!['edit_output_shape_invalid','edit_state_unknown'].includes(item.code);
    if(needsLedgerReview){const ledger=el('a','在编辑页核对远程调用账本','button-link');ledger.href='/edits';actions.append(ledger);}
    if(aiRetry||renderRetry){
      const retry=el('button',renderRetry?'创建编辑重试计划':'创建 AI 重试任务');
      retry.type='button';
      retry.addEventListener('click',()=>mutate(()=>api('/'+item.id+'/retry',{method:'POST',body:JSON.stringify({expected_revision:item.revision})})));
      actions.append(retry);
    }
    const advance=el('button',needsLedgerReview?'核对后重新检查':'立即对账','secondary');
    advance.type='button';
    advance.addEventListener('click',()=>mutate(()=>api('/'+item.id+'/advance',{method:'POST'})));
    actions.append(advance);
    head.append(actions);
    card.append(head);
    if(binding.enabled)card.append(el('p',binding.summary,binding.current?'muted small':'notice small'));
    if(aiRetry||renderRetry)card.append(el('p','上次运行中已经完成的远程 AI 批次或配音 cue 可能已计费；重试会重新发送完整步骤，确认后可能再次计费。','notice small'));
    if(item.code)card.append(el('p',codeNames[item.code]||item.code,'notice small'));
    card.append(el('p','Workflow '+item.id.slice(0,12)+' · 下载 '+String(item.batch_id||'等待').slice(0,12)+' · 编辑 '+String(item.edit_plan_id||'等待').slice(0,12),'mono small'));
    host.append(card);
  }
}
async function refresh(){const items=await api('');render(items);}
async function loadPresets(){
  const rows=await api('/presets');if(!Array.isArray(rows))throw new Error('预设列表格式错误');
  workflowPresets=rows;const select=$('preset-select'),prior=select.value;select.replaceChildren();
  const empty=el('option','不使用预设');empty.value='';select.append(empty);
  for(const preset of rows){const option=el('option',preset.name);option.value=preset.id;select.append(option);}
  if(prior&&!rows.some(item=>item.id===prior)){const missing=el('option','原预设已不可用，仍保留本次参数');missing.value=prior;missing.disabled=true;select.append(missing);}select.value=prior;
}
function selectPresetEngine(id,operation,provider,model,digest){
  const row=aiCapabilities.find(item=>item.operation===operation&&item.provider_id===provider&&item.model_id===model&&item.authorization_sha256===digest&&authorizationReady(item));
  $(id).value=row?capabilityKey(row):'';
}
function showPresetTargets(overrides){
  const host=$('preset-targets');host.replaceChildren();$('preset-details').hidden=!overrides.length;
  const names={title:'标题',description:'简介',tags:'标签',mode:'执行方式',category_id:'分区',copyright:'来源类型',source_credit:'来源',dynamic:'动态文案',no_reprint:'禁止转载',close_comments:'关闭评论',close_danmu:'关闭弹幕',declaration:'声明',short_title:'短标题',content_label:'内容标记',cover_landscape_asset_id:'横版封面',cover_portrait_asset_id:'竖版封面'};
  for(const target of overrides){
    const account=accounts.find(item=>item.id===target.account_id),item=el('div',undefined,'diagnostic-panel');
    item.append(el('h3',account?account.name+' · '+({bilibili:'Bilibili',douyin:'抖音',tencent:'视频号'}[account.platform]||account.platform):'账号已不可用'));
    for(const [key,value] of Object.entries({...target,...target.platform_options})){
      if(names[key]&&value!==null&&value!==undefined)item.append(el('p',names[key]+'：'+(Array.isArray(value)?value.join('、'):value===true?'是':value===false?'否':value==='draft'?'保存草稿':value==='publish'?'投稿发布':String(value)),'small'));
    }
    if(target.publish_at_unix)item.append(el('p','发布时间：'+new Date(target.publish_at_unix*1000).toLocaleString()+'（本机时区）','small'));
    host.append(item);
  }
}
function applyPreset(id){
  if(!id){appliedPreset=null;$('preset-help').textContent='已解除预设关联；已载入的独立投稿参数和当前表单继续保留。';return;}
  const preset=workflowPresets.find(item=>item.id===id);if(!preset)throw new Error('预设已不可用，请刷新');
  const p=preset.profile,r=p.edit_recipe,u=p.upload;if(!Array.isArray(r.segments)||r.segments.length>1)throw new Error('该预设包含多个分段；请等待多分段界面完成后再在本页载入，当前不会只显示或授权其中一段。');const segment=r.segments[0],cover=r.cover,t=r.translation,d=r.dubbing;
  appliedPreset=preset;presetParameters=preset;changedUploadFields.clear();$('workflow-name').value=preset.name;$('credential-mode').value=p.download_credential_mode;
  $('segment-enabled').checked=Boolean(segment);$('segment-start').value=(segment?.start_ms??0)/1000;$('segment-end').value=(segment?.end_ms??60000)/1000;$('segment-label').value=segment?.label??'主视频';
  $('cover-enabled').checked=Boolean(cover);$('cover-ratio').value=cover?.aspect_ratio??'4:3';$('cover-time').value=(cover?.timestamp_ms??0)/1000;$('cover-use-title').checked=false;$('cover-title').value=cover?.title??'';$('cover-subtitle').value=cover?.subtitle??'';
  $('translation-enabled').checked=Boolean(t?.enabled);$('dubbing-enabled').checked=Boolean(d?.enabled);
  for(const [id,value] of [['source-language',t?.source_language||'auto'],['target-language',t?.target_language||'zh-CN'],['dubbing-language',d?.language||'zh-CN']]){
    if(![...$(id).options].some(option=>option.value===value)){const option=el('option',value);option.value=value;$(id).append(option);}$(id).value=value;
  }
  $('replace-audio').checked=Boolean(d?.replace_original_audio);
  if(p.ai){selectPresetEngine('transcription-engine','transcribe',p.ai.transcription_provider,p.ai.transcription_model,p.ai.transcription_authorization_sha256);selectPresetEngine('translation-engine','translate',t.provider,t.model,p.ai.translation_authorization_sha256);selectPresetEngine('speech-engine','dub',d.provider,d.model,p.ai.synthesis_authorization_sha256);$('voice-id').dataset.presetVoice=d.voice;renderVoiceOptions();$('voice-id').value=[...$('voice-id').options].some(option=>option.value===d.voice)?d.voice:'';}
  for(const [id,value] of [['title',u.title],['description',u.description],['tags',u.tags.join(',')],['category-id',u.category_id??''],['copyright',u.copyright??1],['source-credit',u.source_credit],['mode',u.mode]])$(id).value=value;
  for(const node of document.querySelectorAll('[data-account]'))node.checked=u.account_ids.includes(node.value);
  const overrides=u.target_overrides||[],wechat=overrides.find(item=>accounts.find(account=>account.id===item.account_id)?.platform==='tencent');
  $('short-title').value=wechat?.platform_options?.short_title??'';
  $('ai-label').checked=overrides.some(item=>item.platform_options?.declaration==='内容由AI生成'||item.platform_options?.content_label==='含AI生成内容');
  const times=overrides.map(item=>item.publish_at_unix??null),sameTime=times.length&&times.every(time=>time===times[0]);
  $('publish-at').value=sameTime&&times[0]?localDateTimeKey(new Date(times[0]*1000)):'';
  $('auto-edit').checked=p.auto_confirm_edit;$('auto-upload').checked=p.auto_confirm_upload;invalidateAiEgress();
  showPresetTargets(overrides);
  const missing=u.account_ids.some(id=>!accounts.some(item=>item.id===id&&item.auth_state==='ready'&&item.lifecycle_state!=='disconnected'));
  $('preset-help').textContent=missing?'预设中有不可用账号，请重新登录，或明确修改本次账号选择。':'已载入全部参数。独立投稿参数会保留；修改公共字段会覆盖对应字段。云端授权需为本次运行重新勾选。';
}
function workflowProfile({saving=false}={}){
  if($('auto-upload').checked&&!$('auto-edit').checked)throw new Error('自动上传需要同时授权自动编辑');
  const editRecipe=recipe(),aiEnabled=Boolean(editRecipe.translation?.enabled),transcription=aiEnabled?selectedEngine('transcription-engine','transcribe'):null,translation=aiEnabled?selectedEngine('translation-engine','translate'):null,speech=aiEnabled?selectedCapability('speech-engine','dub'):null,remoteAi=aiEnabled&&[transcription.authorization,translation.authorization,editRecipe.dubbing.authorization].some(authorization=>authorization.execution==='remote');
  if(!saving&&appliedPreset?.profile.ai&&aiEnabled){const prior=appliedPreset.profile.ai;if(prior.transcription_authorization_sha256!==transcription.authorization_sha256||prior.translation_authorization_sha256!==translation.authorization_sha256||prior.synthesis_authorization_sha256!==speech.authorization_sha256)throw new Error('预设绑定的 AI 能力已变化，请核对新模型并保存新预设，或解除预设关联');}
  if(remoteAi&&!saving&&!$('ai-egress').checked)throw new Error('请先核对并确认精确 AI runtime、模型修订、硬预算与数据外发范围');
  return {download_credential_mode:$('credential-mode').value,edit_recipe:editRecipe,ai:aiEnabled?{transcription_provider:transcription.provider,transcription_model:transcription.model,transcription_authorization:transcription.authorization,transcription_authorization_sha256:transcription.authorization_sha256,translation_authorization:translation.authorization,translation_authorization_sha256:translation.authorization_sha256}:null,upload:upload(),auto_confirm_edit:$('auto-edit').checked,auto_confirm_upload:$('auto-upload').checked,ai_data_egress_accepted:remoteAi&&$('ai-egress').checked};
}
async function savePreset(){
  const name=$('workflow-name').value.trim();if(!name||name.length>100)throw new Error('预设名称需为 1–100 字');
  const generation=formGeneration,profile=workflowProfile({saving:true}),created=await api('/presets',{method:'POST',body:JSON.stringify({name,profile})});
  await loadPresets();
  try{if(generation===formGeneration&&JSON.stringify(canonical(profile))===JSON.stringify(canonical(workflowProfile({saving:true})))){appliedPreset=created;presetParameters=created;$('preset-select').value=created.id;changedUploadFields.clear();showPresetTargets(created.profile.upload.target_overrides);invalidateAiEgress();}}catch{}
}
async function mutate(work,{refreshAfter=true,successMessage='流程状态已更新。'}={}){if(busy)return;busy=true;document.querySelectorAll('button').forEach(node=>node.disabled=true);try{let result;try{result=await work();}catch(error){message(error.message,true);return;}if(refreshAfter)try{await refresh();}catch(error){message('操作已完成，但列表刷新失败；请点“刷新状态”：'+error.message,true);return result;}message(successMessage);return result;}finally{busy=false;document.querySelectorAll('button').forEach(node=>node.disabled=false);}}
$('save-preset').addEventListener('click',()=>mutate(()=>savePreset(),{refreshAfter:false,successMessage:'参数预设已保存。'}));
let formGeneration=0;
for(const event of ['input','change'])$('workflow-form').addEventListener(event,()=>{formGeneration++;});
$('preset-select').addEventListener('change',()=>{try{applyPreset($('preset-select').value);message($('preset-select').value?'参数预设已载入，请核对本次设置。':'已解除预设关联。');}catch(error){message(error.message,true);}});
for(const id of ['title','description','tags','category-id','copyright','source-credit','mode','publish-at','short-title','ai-label'])$(id).addEventListener('input',()=>changedUploadFields.add(id));
$('accounts').addEventListener('change',()=>changedUploadFields.add('accounts'));
$('workflow-form').addEventListener('submit',event=>{event.preventDefault();mutate(async()=>{
  if($('auto-upload').checked&&!$('auto-edit').checked)throw new Error('自动上传需要同时授权自动编辑');
  const profile=workflowProfile();
  const intent={source_url:$('source-url').value.trim(),name:$('workflow-name').value.trim(),profile};
  const pending=await pendingWorkflowKey(intent),payload={...intent,idempotency_key:pending.requestKey};const created=await api('',{method:'POST',body:JSON.stringify(payload)});
  clearPendingWorkflowKey(pending.storageKey);
  return created;
},{successMessage:'自动流程已创建；响应丢失时重试会复用，成功后可再次运行相同参数。'});});
$('refresh').addEventListener('click',()=>mutate(async()=>{await Promise.all([loadAccounts(),loadAiCapabilities(),loadPresets()]);invalidateAiEgress();},{successMessage:'AI 能力与流程状态已刷新，请重新核对授权。'}));
$('voice-id').addEventListener('change',()=>{delete $('voice-id').dataset.presetVoice;});
function invalidateAiEgress(){$('ai-egress').checked=false;renderAiEgress();}
$('target-language').addEventListener('change',()=>{$('dubbing-language').value=$('target-language').value;invalidateAiEgress();});
$('speech-engine').addEventListener('change',()=>{renderVoiceOptions();invalidateAiEgress();});
for(const id of ['source-url','credential-mode','transcription-engine','translation-engine','translation-enabled','dubbing-enabled','segment-enabled','segment-start','segment-end','source-language','target-language','dubbing-language','voice-id','replace-audio'])$(id).addEventListener(id.includes('engine')||id.includes('enabled')||['credential-mode','target-language','voice-id','replace-audio'].includes(id)?'change':'input',invalidateAiEgress);
async function loadAccounts(){const response=await fetch('/api/v1/uploads/accounts');if(response.ok){const selected=selectedAccounts();accounts=await response.json();renderAccounts();for(const node of document.querySelectorAll('[data-account]'))node.checked=selected.includes(node.value);}}
async function loadAiCapabilities(){const response=await fetch('/api/v1/edits/ai/capabilities');if(response.ok){aiCapabilities=await response.json();if(!Array.isArray(aiCapabilities))aiCapabilities=[];}renderAiEngines();}
function schedule(){clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{if(!document.hidden&&!busy)try{await refresh();}catch{}schedule();},2000);}
(async()=>{try{csrf=(await api('/session')).csrf_token;await Promise.all([loadAccounts(),loadAiCapabilities(),loadPresets()]);$('preset-select').disabled=false;await refresh();message('自动流程已连接。');}catch(error){message('连接失败：'+error.message,true);}finally{schedule();}})();
  </script>
</body></html>'''
