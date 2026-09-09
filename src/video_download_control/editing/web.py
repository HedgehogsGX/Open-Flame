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
        <a class="nav-link" href="/workflows">自动流程</a>
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
      <div class="card-header"><div><p class="section-index">AI 能力</p><h2 id="ai-heading">听写、翻译与标准音色配音</h2><p class="muted">每次 AI 执行都先建立待确认任务。生成的字幕或译文会连同时间码完整展示；批准时间轴后，再明确选择是否写入编辑草稿。</p></div></div>
      <div id="capabilities" class="capability-grid"><p class="muted">正在读取能力状态…</p></div>
      <div class="diagnostic-grid">
        <div class="diagnostic-panel">
          <h3>1. 建立听写任务</h3>
          <label for="transcription-capability">Provider 与模型</label><select id="transcription-capability" disabled><option value="">当前没有可执行的听写能力</option></select>
          <label for="transcription-language">已知源语言（可选）</label><select id="transcription-language"><option value="">自动检测</option><option value="zh">中文</option><option value="en">English</option></select>
          <button id="create-transcription" type="button" disabled>建立待确认听写任务</button>
          <p class="muted small">创建任务不会开始听写。任务出现在下方后，还需核对 provider、模型与数据范围并显式确认。</p>
        </div>
        <div class="diagnostic-panel">
          <h3>2. 从已批准字幕翻译</h3>
          <label for="translation-capability">Provider 与模型</label><select id="translation-capability" disabled><option value="">当前没有可执行的翻译能力</option></select>
          <label for="source-language">字幕语言</label><select id="source-language" disabled><option value="">批准字幕后自动填写</option></select>
          <label for="target-language">目标语言</label><select id="target-language"><option value="zh">中文</option><option value="en">English</option></select>
          <p class="muted small">请在已批准的听写时间轴下建立翻译任务。翻译保持每个 cue 的顺序和时间不变，并再次等待确认与逐段批准。</p>
        </div>
        <div class="diagnostic-panel">
          <h3>3. 写入草稿与配音</h3>
          <label class="choice"><input id="use-translation" type="checkbox" disabled><span>在处理计划中使用已批准译文</span></label>
          <label class="choice"><input id="dubbing-enabled" type="checkbox" disabled><span>同时生成标准音色配音</span></label>
          <label for="voice-provider">配音 provider 与模型</label><select id="voice-provider" disabled><option value="">当前没有可执行的配音能力</option></select>
          <label for="voice-id">标准音色</label><select id="voice-id" disabled><option value="">选择 provider 后读取</option></select>
          <label for="voice-rate">配音语速</label><input id="voice-rate" type="number" min="0.88" max="1.12" step="any" value="1" inputmode="decimal" aria-describedby="voice-rate-help" disabled>
          <label for="mix-mode">原声处理</label><select id="mix-mode" disabled><option value="mix">压低原声后叠加</option><option value="replace">替换原声</option></select>
          <p id="voice-rate-help" class="muted small">声音克隆不会出现在这里。语速限制在 0.88×～1.12×，较快语速可缓解少量时长溢出，但不会自动重试或截断音频。批准译文会把 translation 以 ready 状态写入当前草稿；选择配音后请保存草稿。</p>
        </div>
      </div>
      <div class="card-header"><div><h3>AI 任务</h3><p class="muted small">只有点击“确认并执行”后，待确认任务才会进入队列。</p></div></div>
      <div id="ai-tasks"><p class="muted">请先打开一个编辑项目。</p></div>
      <div class="card-header"><div><h3 id="ai-invocations-heading">远程调用账本</h3><p class="muted small">这里只显示脱敏的调用身份、状态和单位。远程结果不确定时必须先人工核对，不能直接重试。</p></div></div>
      <div id="ai-invocations" role="region" aria-labelledby="ai-invocations-heading" aria-live="polite"><p class="muted">请先打开一个编辑项目。</p></div>
      <div class="card-header"><div><h3>字幕与译文核对</h3><p class="muted small">批准和拒绝按钮位于完整 cue 列表之后，方便先核对文字与时间。</p></div></div>
      <div id="timelines"><p class="muted">尚无待核对的字幕或译文。</p></div>
    </section>

    <section class="card" aria-labelledby="plans-heading">
      <div class="card-header"><div><p class="section-index">步骤 3</p><h2 id="plans-heading">核对计划与处理任务</h2><p class="muted">确认会启动本地媒体处理；配音能力由隔离 runtime 执行，若 provider 是远程服务，确认后会逐段发送已批准译文并可能产生费用。</p></div></div>
      <div id="plans"><p class="muted">尚无处理计划。</p></div>
    </section>

    <section class="card" aria-labelledby="outputs-heading">
      <div class="card-header"><div><p class="section-index">步骤 4</p><h2 id="outputs-heading">编辑成品</h2><p class="muted">每个成品都记录来源计划、大小与 SHA-256。视频可显式带入上传器，封面可下载后在上传器中导入。</p></div></div>
      <div id="outputs" class="output-grid"><p class="muted">尚无编辑成品。</p></div>
    </section>
    <p class="page-footer">Open-Flame 0.28.0 · 本地优先 · 下载、编辑与上传数据相互隔离</p>
  </main>
  <script>
'use strict';
const $=id=>document.getElementById(id);
let csrf='',selectedProject=null,draft=null,busy=false,recordsPromise=null,refreshPromise=null,pollPromise=null,pollTimer=null,pageActive=true,projectSelectionGeneration=0;
let aiCapabilities=[],aiTasks=[],timelines=[],aiInvocations=[],aiInvocationSummary={},aiInvocationOffset=0,aiLedgerReady=false,aiLedgerError='',loadedAiRecipe={translation:null,dubbing:null},activeTranslationId=null;
const aiInvocationPageSize=200;
const renderSignatures=new Map();
const stateNames={review:'待核对',queued:'等待处理',running:'处理中',canceling:'正在取消',succeeded:'等待核对结果',ready:'已完成',approved:'已批准',rejected:'已拒绝',failed:'失败',canceled:'已取消'};
const operationNames={transcribe:'自动听写',translate:'自动翻译',dub:'标准音色配音'};
const codeNames={idempotency_conflict:'相同请求标识对应了不同内容',draft_version_conflict:'草稿已在其他页面更新，请刷新后重试',stale_draft_version:'草稿已在其他页面更新，请刷新后重试',editing_worker_busy:'另一个本地实例正在使用编辑目录',processor_not_configured:'固定媒体工具尚未就绪',source_asset_changed:'编辑源视频已变化，不能继续处理',source_changed:'编辑源视频已变化，不能继续读取',source_hash_mismatch:'源文件校验不一致',source_not_found:'源视频不存在',asset_changed:'编辑成品已变化，不能继续读取',edit_output_not_found:'编辑成品不存在',invalid_edit_recipe:'编辑参数不合法',invalid_ai_request:'AI 任务参数不合法',ai_operation_blocked:'所选 AI 能力当前不可执行',ai_task_not_reviewable:'此 AI 任务已不能确认',ai_task_state_conflict:'AI 任务状态已变化，请刷新后重试',ai_task_retry_not_allowed:'此 AI 任务不能重试',ai_task_retry_lineage_changed:'AI 任务重试链已变化，请刷新后操作当前任务',ai_task_definition_changed:'AI 任务定义已变化，请刷新后重新核对',source_timeline_invalid:'来源字幕已变化或尚未批准',timeline_review_conflict:'时间轴已在其他页面完成核对',timeline_parent_not_approved:'必须先批准来源字幕',ai_runtime_missing:'隔离 AI runtime 尚未安装',ai_runtime_invalid:'隔离 AI runtime 校验失败',ai_runtime_changed:'AI runtime 在执行前后发生变化',ai_runtime_unsupported:'当前 AI runtime/运行环境不受支持',ai_provider_not_found:'所选 provider 不存在',ai_provider_operation_unsupported:'所选 provider 不支持此操作',ai_model_not_found:'所选模型不存在',ai_model_operation_unsupported:'所选模型不支持此操作',ai_provider_auth_missing:'所选 provider 的凭据尚未配置',ai_provider_auth_environment_invalid:'provider 凭据配置不安全',ai_authorization_binding_required:'此旧 AI 项目没有精确运行时授权，请按当前能力重新创建',ai_authorization_changed:'AI runtime、模型声明或预算已变化，请刷新并重新创建任务',ai_authorization_invalid:'AI 授权数据无效',ai_budget_exceeded:'此 AI 操作超过本次授权的硬预算',ai_budget_invalid:'AI 预算数据无效',ai_invocation_invalid:'远程调用账本参数无效',ai_invocation_not_remote:'本地 AI 操作不能建立远程调用记录',ai_invocation_owner_not_found:'远程调用所属任务或计划不存在',ai_invocation_owner_inactive:'远程调用所属任务或计划已不能执行',ai_invocation_owner_changed:'远程调用所属任务或计划定义已变化',ai_invocation_conflict:'相同远程调用位置对应了不同定义',ai_invocation_budget_exceeded:'远程调用单位超过冻结硬上限',ai_invocation_not_found:'远程调用记录不存在',ai_invocation_state_conflict:'远程调用状态已变化，请刷新后重新核对',ai_invocation_database_unavailable:'远程调用账本暂不可用',ai_remote_ledger_required:'远程 AI 执行缺少调用账本，已停止',ai_remote_ledger_invalid:'远程调用账本接口无效，已停止',ai_remote_ledger_unavailable:'远程调用账本暂不可用，已停止',ai_remote_ledger_conflict:'远程调用账本状态冲突，已停止',ai_remote_result_unknown:'远程调用结果不确定，必须先核对',ai_remote_not_accepted:'已确认远程服务没有接受本次调用',ai_remote_accepted_without_result:'已确认远程服务接受调用但没有可用结果',ai_remote_abandoned:'本次远程调用已放弃',ai_remote_retry_blocked:'远程调用账本阻止当前重试',ai_remote_reconciliation_required:'必须先核对远程调用结果',ai_remote_reconciliation_acknowledgement_required:'请明确确认远程调用核对结论',ai_voice_not_allowed:'所选音色不是当前允许的标准音色',ai_translation_not_approved:'译文尚未批准',ai_dubbing_not_approved:'配音设置尚未批准',ai_timeline_required:'处理计划找不到匹配的已批准译文',capability_unavailable:'所选能力尚不可用',editing_storage_full:'编辑目录可用空间不足',editing_storage_unavailable:'编辑目录暂不可用',media_output_too_large:'预计或实际编辑输出超过 8 GiB 上限',cover_font_unavailable:'本机没有可用的受支持封面字体；请清空封面文字后重试',cover_glyph_unsupported:'本机字体不支持封面文字中的部分字符；请修改或清空后重试',restart_confirmation_required:'应用重启后需要重新核对并确认',render_interrupted:'上次本地处理被中断',media_processing_failed:'媒体处理失败',ai_data_egress_confirmation_required:'请先核对并确认此计划的数据范围与费用',plan_definition_changed:'处理计划定义已变化，请刷新后重新核对',render_retry_lineage_changed:'处理计划重试链已变化，请刷新后操作当前计划',ai_timeline_segment_empty:'所选分段内没有已批准的字幕内容',ai_segment_boundary_splits_cue:'分段切在字幕句子中间，请把边界对齐到字幕开始或结束时间'};
const invocationReasonNames={remote_dispatch_started:'已在远程发送边界前落账',runtime_response_validated:'运行时响应已完成结构校验',remote_dispatch_not_started:'远程发送前已释放',remote_outcome_unknown:'远程结果无法确认',remote_outcome_reconciled:'人工核对结论已记录',process_restarted_before_dispatch:'应用重启前尚未发送',process_restarted_after_dispatch:'应用在发送后重启',owner_failed_before_dispatch:'所属任务在发送前失败',owner_failed_after_dispatch:'所属任务在发送后失败',legacy_remote_outcome_unknown:'旧版远程结果无法确认',legacy_remote_execution_unknown:'旧版执行位置无法确认'};
function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('danger',error);}
async function api(path,options={}){const headers=new Headers(options.headers||{});if(options.method&&options.method!=='GET')headers.set('X-Editing-CSRF',csrf);if(options.body&&typeof options.body==='string')headers.set('Content-Type','application/json');const response=await fetch('/api/v1/edits'+path,{...options,headers});let data=null;try{data=await response.json();}catch{}if(!response.ok){const code=data?.detail||'request_failed';throw new Error(codeNames[code]||code);}return data;}
async function mutate(work){if(busy)return;busy=true;document.querySelectorAll('button').forEach(button=>button.disabled=true);try{if(pollPromise)await pollPromise;else if(recordsPromise)await recordsPromise;await work();}catch(error){message(error.message,true);}finally{busy=false;document.querySelectorAll('button').forEach(button=>button.disabled=false);updateActionState();}}
function randomKey(prefix){return prefix+'-'+crypto.randomUUID();}
function element(tag,text,className=''){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function focusControl(node,key){node.dataset.focusKey=key;return node;}
function renderIfChanged(key,payload,render){const signature=JSON.stringify(payload);if(renderSignatures.get(key)===signature)return false;const focusKey=document.activeElement?.dataset?.focusKey||null;render(payload);renderSignatures.set(key,signature);if(focusKey){const target=[...document.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===focusKey);if(target&&!target.disabled)target.focus({preventScroll:true});}return true;}
function fmtBytes(value){const bytes=Number(value);if(!Number.isFinite(bytes))return '未知大小';if(bytes<1024)return bytes+' B';if(bytes<1024**2)return (bytes/1024).toFixed(1)+' KiB';return (bytes/1024**2).toFixed(1)+' MiB';}
function fmtTime(ms){const seconds=Math.max(0,Number(ms)||0)/1000;return seconds.toFixed(3)+' 秒';}
function fmtClock(ms){const value=Math.max(0,Math.round(Number(ms)||0)),hours=Math.floor(value/3600000),minutes=Math.floor(value%3600000/60000),seconds=Math.floor(value%60000/1000),millis=value%1000;return String(hours).padStart(2,'0')+':'+String(minutes).padStart(2,'0')+':'+String(seconds).padStart(2,'0')+'.'+String(millis).padStart(3,'0');}
function capabilityKey(row){return [row.operation,row.provider_id||'',row.model_id||'',row.authorization_sha256||''].join('|');}
function usableCapabilities(operation){return aiCapabilities.filter(row=>row.operation===operation&&row.status!=='blocked'&&row.provider_id&&row.model_id&&row.authorization&&row.authorization_sha256);}
function selectedCapability(operation){const id=operation==='transcribe'?'transcription-capability':operation==='translate'?'translation-capability':'voice-provider',key=$(id).value;return aiCapabilities.find(row=>capabilityKey(row)===key)||null;}
function standardVoices(row){return Array.isArray(row?.voices)?row.voices.filter(voice=>voice&&voice.id&&voice.is_clone!==true):[];}
function dataScope(row){if(!row)return '当前能力清单中已找不到此 provider/model；请刷新并重新核对后再执行。';const values=Array.isArray(row.data_egress)?row.data_egress:[];return row.execution==='remote'?(values.length?'将发送：'+values.join('、'):'远程数据范围未声明'):'在隔离的本地 runtime 中执行';}
function authorizationMatches(row,authorizationSha256){return !!row&&!!authorizationSha256&&row.authorization_sha256===authorizationSha256;}
function authorizationSummary(value){const auth=value?.authorization||value;if(!auth||!auth.manifest_sha256)return '未绑定精确 AI runtime；必须按当前能力重新创建。';const limits=auth.limits||{},parts=[];if(limits.max_audio_ms)parts.push('音频≤'+Math.round(limits.max_audio_ms/60000)+'分钟');if(limits.max_cues)parts.push('cues≤'+limits.max_cues);if(limits.max_input_characters)parts.push('字符≤'+limits.max_input_characters);if(limits.max_requests)parts.push('调用单位≤'+limits.max_requests);return '模型声明 '+(auth.model_revision||'无')+' · Runtime '+String(auth.manifest_sha256).slice(0,12)+'… · '+(parts.join('、')||'固定预算');}
const invocationStateNames={reserved:'已预留',dispatched:'已发送，等待响应',responded:'已收到响应',released:'发送前已释放',unknown:'远程结果待核对',reconciled:'已人工核对'};
const invocationResolutionNames={not_accepted:'确认未被远程服务接受',accepted_without_result:'确认已接受但没有可用结果',abandoned:'已放弃本次调用'};
const invocationOperationNames={transcribe:'自动听写',translate:'自动翻译',synthesize:'标准音色配音'};
function shortDigest(value,legacy=false){const text=typeof value==='string'?value:'';return /^[0-9a-f]{64}$/.test(text)?text.slice(0,12)+'…':legacy&&value===null?'旧记录未绑定':'未记录';}
function safeCount(value,fallback=0){const number=Number(value);return Number.isSafeInteger(number)&&number>=0?number:fallback;}
function validInvocationRow(row){const id=value=>typeof value==='string'&&/^[0-9a-f]{32}$/.test(value),digest=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value),integer=(value,minimum=0)=>Number.isSafeInteger(value)&&value>=minimum,task=id(row?.ai_task_id),plan=id(row?.render_plan_id),legacy=typeof row?.legacy==='boolean'&&row.legacy;return !!row&&typeof row==='object'&&!Array.isArray(row)&&id(row.id)&&task!==plan&&['transcribe','translate','synthesize'].includes(row.operation)&&integer(row.ordinal)&&integer(row.attempt)&&integer(row.request_units,1)&&(digest(row.authorization_sha256)||(legacy&&row.authorization_sha256===null))&&digest(row.owner_definition_sha256)&&digest(row.request_fingerprint)&&Object.hasOwn(invocationStateNames,row.state)&&integer(row.revision)&&typeof row.legacy==='boolean'&&(row.state==='reconciled'?Object.hasOwn(invocationResolutionNames,row.resolution):row.resolution==='');}
function normalizeInvocationSnapshot(value){const integer=(item,minimum=0)=>Number.isSafeInteger(item)&&item>=minimum,id=item=>typeof item==='string'&&/^[0-9a-f]{32}$/.test(item);if(!value||!Array.isArray(value.items)||value.items.some(row=>!validInvocationRow(row))||!value.summary||typeof value.summary!=='object'||Array.isArray(value.summary)||!value.summary.counts||typeof value.summary.counts!=='object'||Array.isArray(value.summary.counts))throw new Error(codeNames.ai_invocation_invalid);const summary=value.summary,states=Object.keys(invocationStateNames),taskIds=summary.retry_blocked_ai_task_ids,planIds=summary.retry_blocked_render_plan_ids;if(states.some(state=>!integer(summary.counts[state]))||!integer(summary.total)||!integer(summary.request_units)||!integer(summary.unresolved)||!integer(summary.offset)||!integer(summary.limit,1)||typeof summary.has_more!=='boolean'||!Array.isArray(taskIds)||!Array.isArray(planIds)||taskIds.some(item=>!id(item))||planIds.some(item=>!id(item))||new Set(taskIds).size!==taskIds.length||new Set(planIds).size!==planIds.length||states.reduce((total,state)=>total+summary.counts[state],0)!==summary.total||summary.unresolved!==summary.counts.reserved+summary.counts.dispatched+summary.counts.unknown||value.items.length>summary.limit||summary.offset+value.items.length>summary.total||summary.has_more!==(summary.offset+value.items.length<summary.total))throw new Error(codeNames.ai_invocation_invalid);return {available:true,items:value.items,summary,error:''};}
async function loadInvocationSnapshot(id,offset){try{return normalizeInvocationSnapshot(await api('/ai-invocations?project_id='+encodeURIComponent(id)+'&offset='+encodeURIComponent(offset)+'&limit='+aiInvocationPageSize));}catch(error){return {available:false,items:[],summary:{},error:error.message||codeNames.ai_invocation_database_unavailable};}}
function ownerInvocations(ownerKey,ownerId){return aiInvocations.filter(row=>row[ownerKey]===ownerId);}
function unknownInvocations(ownerKey,ownerId){return ownerInvocations(ownerKey,ownerId).filter(row=>row.state==='unknown');}
function invocationOwnerBlocked(ownerKey,ownerId){const field=ownerKey==='ai_task_id'?'retry_blocked_ai_task_ids':'retry_blocked_render_plan_ids',values=aiInvocationSummary?.[field];return Array.isArray(values)&&values.includes(ownerId);}
function invocationOwner(row){if(row.ai_task_id)return 'AI 任务 '+String(row.ai_task_id).slice(0,8);if(row.render_plan_id)return '处理计划 '+String(row.render_plan_id).slice(0,8);return '历史调用';}
function fmtLedgerTime(value){if(typeof value!=='string'||!value)return '未记录';const parsed=new Date(value);return Number.isNaN(parsed.getTime())?'时间无效':parsed.toLocaleString('zh-CN',{hour12:false});}
async function reconcileInvocation(row,resolution){await api('/ai-invocations/'+encodeURIComponent(row.id)+'/reconcile',{method:'POST',body:JSON.stringify({expected_revision:row.revision,resolution,acknowledge:true})});message('远程调用核对结论已保存；相关重试入口会按最新账本状态重新评估。');await refreshRecords();}
function renderAiInvocations(snapshot){
  const host=$('ai-invocations');host.replaceChildren();
  if(!selectedProject){host.append(element('p','请先打开一个编辑项目。','muted'));return;}
  if(!snapshot.available){host.append(element('p','远程调用账本暂不可用；为避免重复调用，远程 AI 重试入口已暂停。','notice'));if(snapshot.error)host.append(element('p',snapshot.error,'muted small'));return;}
  const rows=snapshot.items||[],summary=snapshot.summary||{},stateCounts=summary.counts&&typeof summary.counts==='object'?summary.counts:{};
  const total=safeCount(summary.total),unknown=safeCount(stateCounts.unknown),unresolved=safeCount(summary.unresolved),units=safeCount(summary.request_units),offset=safeCount(summary.offset),limit=safeCount(summary.limit,aiInvocationPageSize),pageStart=rows.length?offset+1:0,pageEnd=offset+rows.length;
  host.append(element('p','记录 '+total+' 项 · 未解决 '+unresolved+' 项（unknown '+unknown+'）· 调用单位 '+units+' · 当前 '+pageStart+'–'+pageEnd,'status-value'));
  if(!rows.length){host.append(element('p','当前项目尚无远程 AI 调用记录。','muted'));return;}
  for(const row of rows){
    const item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div'),key='ai-invocation:'+row.id+':',state=invocationStateNames[row.state]||'状态未知';
    copy.append(element('h3',(invocationOperationNames[row.operation]||'远程 AI 调用')+' '+String(row.id||'').slice(0,8)),element('p',state+' · '+invocationOwner(row)+' · 单元 '+safeCount(row.ordinal)+' · 尝试 '+safeCount(row.attempt)+' · '+safeCount(row.request_units)+' 调用单位','muted small'));
    const timing=row.legacy?'迁移登记：'+fmtLedgerTime(row.created_at)+' · 实际远程发送时间未知':'发送：'+fmtLedgerTime(row.dispatched_at)+' · 待核对：'+fmtLedgerTime(row.unknown_at);
    head.append(copy);item.append(head,element('p','授权 '+shortDigest(row.authorization_sha256,row.legacy)+' · 请求 '+shortDigest(row.request_fingerprint),'mono small'),element('p',timing,'muted small'));
    if(row.reason_code)item.append(element('p','记录原因：'+(invocationReasonNames[row.reason_code]||codeNames[row.reason_code]||row.reason_code),'muted small'));
    if(row.legacy)item.append(element('p','这是从旧记录迁移的脱敏调用；迁移登记时间不代表实际发送时间，结论必须由操作者重新核对。','muted small'));
    if(row.state==='unknown'){
      item.append(element('p','远程服务可能已经接受并计费，但本机没有可用响应。核对前不得重试，否则可能重复调用。','notice small'));
      const actions=element('div',undefined,'row');
      for(const choice of [{value:'not_accepted',label:'确认：未被接受'},{value:'accepted_without_result',label:'确认：已接受但无结果'},{value:'abandoned',label:'放弃本次调用'}]){const button=focusControl(element('button',choice.label,'secondary'),key+choice.value);button.type='button';button.addEventListener('click',()=>mutate(()=>reconcileInvocation(row,choice.value)));actions.append(button);}
      item.append(actions,element('p','选择会作为固定核对结论写入账本，不会撤销供应商侧请求、恢复响应或证明未计费。','muted small'));
    }else if(row.state==='reconciled'&&row.resolution){item.append(element('p','核对结论：'+(invocationResolutionNames[row.resolution]||'已记录'),'muted small'));}
    host.append(item);
  }
  if(offset>0||summary.has_more){const pages=element('div',undefined,'row');if(offset>0){const previous=focusControl(element('button','上一页','secondary'),'ai-invocation-page:previous');previous.type='button';previous.addEventListener('click',()=>mutate(async()=>{aiInvocationOffset=Math.max(0,offset-limit);renderSignatures.delete('ai-invocations');await refreshRecords();}));pages.append(previous);}if(summary.has_more){const next=focusControl(element('button','下一页','secondary'),'ai-invocation-page:next');next.type='button';next.addEventListener('click',()=>mutate(async()=>{aiInvocationOffset=offset+limit;renderSignatures.delete('ai-invocations');await refreshRecords();}));pages.append(next);}host.append(pages);}
}
function addSegment(segment={start_ms:0,end_ms:10000,label:''}){const index=$('segments').children.length,row=element('div',undefined,'segment-row item');row.dataset.segment='';const title=element('div',undefined,'row-spread'),heading=element('h3','分段 '+(index+1));const remove=element('button','移除','secondary');remove.type='button';remove.addEventListener('click',()=>{row.remove();renumberSegments();markDirty();});title.append(heading,remove);row.append(title);const grid=element('div',undefined,'segment-fields');for(const spec of [{key:'start',label:'开始（秒）',value:Number(segment.start_ms||0)/1000},{key:'end',label:'结束（秒）',value:Number(segment.end_ms||10000)/1000}]){const wrap=element('div'),label=element('label',spec.label);const input=document.createElement('input');input.type='number';input.min='0';input.step='0.001';input.value=String(spec.value);input.dataset[spec.key]='';label.append(input);const use=element('button','取当前时间','secondary');use.type='button';use.addEventListener('click',()=>{input.value=$('preview').currentTime.toFixed(3);markDirty();});wrap.append(label,use);grid.append(wrap);}const label=element('label','片段名称（可选）');const input=document.createElement('input');input.maxLength=80;input.value=segment.label||'';input.dataset.label='';label.append(input);row.append(grid,label);row.querySelectorAll('input').forEach(node=>node.addEventListener('input',markDirty));$('segments').append(row);renumberSegments();}
function renumberSegments(){[...$('segments').children].forEach((row,index)=>row.querySelector('h3').textContent='分段 '+(index+1));}
function markDirty(){if(selectedProject)$('draft-state').textContent='有未保存更改';}
function selectedTranslation(){return timelines.find(row=>row.id===activeTranslationId&&row.kind==='translation'&&row.state==='approved')||null;}
function parentTimeline(row){return row?.parent_id?timelines.find(parent=>parent.id===row.parent_id)||null:null;}
function readRecipe(){const segments=[...document.querySelectorAll('[data-segment]')].map((row,index)=>({start_ms:Math.round(Number(row.querySelector('[data-start]').value)*1000),end_ms:Math.round(Number(row.querySelector('[data-end]').value)*1000),label:row.querySelector('[data-label]').value.trim()||('片段 '+(index+1))}));for(const [index,segment] of segments.entries())if(!Number.isSafeInteger(segment.start_ms)||!Number.isSafeInteger(segment.end_ms)||segment.start_ms<0||segment.end_ms<=segment.start_ms)throw new Error('分段 '+(index+1)+' 的起止时间无效。');let cover=null;if($('cover-enabled').checked){cover={timestamp_ms:Math.round(Number($('cover-at').value)*1000),aspect_ratio:$('cover-aspect').value,title:$('cover-title').value.trim(),subtitle:''};if(!Number.isSafeInteger(cover.timestamp_ms)||cover.timestamp_ms<0)throw new Error('封面抽帧时间无效。');}let translation=null,dubbing=null;if($('use-translation').checked){const revision=selectedTranslation(),parent=parentTimeline(revision);if(!revision||!parent)throw new Error('请选择一份已批准且来源完整的译文。');translation={enabled:true,source_language:parent.language,target_language:revision.language,provider:revision.provider,model:revision.model,state:'ready'};if($('dubbing-enabled').checked){const capability=selectedCapability('dub'),voice=$('voice-id').value,rawRate=$('voice-rate').value,rate=Number(rawRate);if(!capability||!capability.authorization||!standardVoices(capability).some(item=>item.id===voice))throw new Error('请选择当前 provider 提供的标准音色。');if(!rawRate||!Number.isFinite(rate)||rate<0.88||rate>1.12){$('voice-rate').focus();throw new Error('请选择 0.88×～1.12× 的配音语速。');}dubbing={enabled:true,language:revision.language,provider:capability.provider_id,model:capability.model_id,voice,state:'ready',replace_original_audio:$('mix-mode').value==='replace',authorization:capability.authorization,rate};}}else if($('dubbing-enabled').checked){throw new Error('启用配音前请先选择已批准译文。');}if(!segments.length&&!translation&&!cover)throw new Error('请至少添加分段、封面或已批准译文。');return {segments,cover,translation,dubbing};}
function loadRecipe(recipe){$('segments').replaceChildren();for(const segment of recipe?.segments||[])addSegment(segment);if(!$('segments').children.length&&!recipe?.translation?.enabled&&!recipe?.dubbing?.enabled)addSegment();$('cover-enabled').checked=!!recipe?.cover;$('cover-controls').hidden=!recipe?.cover;if(recipe?.cover){$('cover-at').value=String(Number(recipe.cover.timestamp_ms)/1000);if([...$('cover-aspect').options].some(option=>option.value===recipe.cover.aspect_ratio))$('cover-aspect').value=recipe.cover.aspect_ratio;$('cover-title').value=recipe.cover.title||'';}loadedAiRecipe={translation:recipe?.translation||null,dubbing:recipe?.dubbing||null};activeTranslationId=null;$('use-translation').checked=!!recipe?.translation?.enabled;$('dubbing-enabled').checked=!!recipe?.dubbing?.enabled;$('mix-mode').value=recipe?.dubbing?.replace_original_audio?'replace':'mix';$('voice-rate').value=String(recipe?.dubbing?.rate??1);chooseCapabilityForRecipe('translation-capability','translate',loadedAiRecipe.translation);chooseCapabilityForRecipe('voice-provider','dub',loadedAiRecipe.dubbing);syncAiControls();}
function projectId(project){return project.id||project.project_id;}
function renderProjects(projects){const host=$('projects');host.replaceChildren();if(!projects.length){host.append(element('p','尚无编辑项目。请从下载成品进入编辑。','muted'));return;}for(const project of projects){const id=projectId(project),item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div');copy.append(element('h3',project.name||'未命名项目'),element('p',(project.source_name||'已登记视频')+' · '+fmtBytes(project.source_size||project.size),'muted small'));const select=focusControl(element('button',selectedProject&&projectId(selectedProject)===id?'正在编辑':'打开项目',selectedProject&&projectId(selectedProject)===id?'secondary':''),'project:'+id+':open');select.type='button';select.addEventListener('click',()=>mutate(()=>selectProject(project)));head.append(copy,select);item.append(head);host.append(item);}}
async function selectProject(project){const id=projectId(project),generation=++projectSelectionGeneration;selectedProject=project;aiTasks=[];timelines=[];aiInvocations=[];aiInvocationSummary={};aiInvocationOffset=0;aiLedgerReady=false;aiLedgerError='';activeTranslationId=null;renderSignatures.delete('ai-invocations');$('ai-invocations').replaceChildren(element('p','正在读取当前项目的远程调用账本…','muted'));const nextDraft=await api('/projects/'+encodeURIComponent(id)+'/draft');if(generation!==projectSelectionGeneration||id!==(selectedProject&&projectId(selectedProject)))return;draft=nextDraft;$('workspace').hidden=false;$('project-summary').textContent=(project.name||'未命名项目')+' · '+(project.source_name||'视频')+' · 源 SHA-256 '+String(project.source_sha256||'').slice(0,16)+'…';$('preview').src='/api/v1/edits/projects/'+encodeURIComponent(id)+'/source';loadRecipe(draft.recipe);$('draft-state').textContent='草稿 v'+draft.version+' 已保存';if(recordsPromise)try{await recordsPromise;}catch{}await refreshRecords();}
async function saveDraft(){if(!selectedProject)throw new Error('请先打开项目。');const payload={expected_version:draft.version,recipe:readRecipe(),idempotency_key:randomKey('draft')};draft=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/draft',{method:'PUT',body:JSON.stringify(payload)});loadedAiRecipe={translation:draft.recipe?.translation||null,dubbing:draft.recipe?.dubbing||null};$('draft-state').textContent='草稿 v'+draft.version+' 已保存';message('编辑草稿已保存；尚未开始处理。');return draft;}
function fillCapabilitySelect(id,operation){const select=$(id),previous=select.value,rows=usableCapabilities(operation).slice();if(operation==='transcribe')rows.sort((left,right)=>(left.model_id==='whisper-1'?-1:0)-(right.model_id==='whisper-1'?-1:0));select.replaceChildren();for(const row of rows){const option=element('option',row.provider_id+' · '+row.model_id);option.value=capabilityKey(row);select.append(option);}if([...select.options].some(option=>option.value===previous))select.value=previous;if(!rows.length){const option=element('option','当前没有可执行的'+(operationNames[operation]||operation)+'能力');option.value='';select.append(option);}select.disabled=!rows.length;}
function chooseCapabilityForRecipe(id,operation,recipe){if(!recipe?.enabled)return true;const select=$(id),row=usableCapabilities(operation).find(item=>item.provider_id===recipe.provider&&item.model_id===recipe.model&&(!recipe.authorization||JSON.stringify(item.authorization)===JSON.stringify(recipe.authorization)));if(row){select.value=capabilityKey(row);return true;}const unavailable=element('option','已保存的'+(operationNames[operation]||operation)+'能力不可用，请重新选择');unavailable.value='';select.append(unavailable);select.value='';return false;}
function updateVoiceOptions(){const select=$('voice-id'),previous=select.value,preferred=loadedAiRecipe.dubbing?.voice||'',voices=standardVoices(selectedCapability('dub'));select.replaceChildren();for(const voice of voices){const option=element('option',voice.label||voice.id);option.value=voice.id;select.append(option);}const wanted=[preferred,previous].find(value=>voices.some(voice=>voice.id===value));if(wanted)select.value=wanted;if(!voices.length){const option=element('option','此 provider 未公布标准音色');option.value='';select.append(option);}updateActionState();}
function renderCapabilities(rows){aiCapabilities=rows;const host=$('capabilities');host.replaceChildren();for(const row of rows){const card=element('article',undefined,'capability-card');card.append(element('h3',row.label||operationNames[row.operation]||row.operation));const status=element('p',row.status==='ready'?'可用':row.status==='blocked'?'尚不可用':'完整性已验证，执行健康待确认','status-value');card.append(status,element('p',row.description||row.reason||row.reason_code||'','muted small'));if(row.provider_id)card.append(element('p','Provider：'+row.provider_id+(row.model_id?' · '+row.model_id:''),'mono small'));card.append(element('p',dataScope(row),'muted small'),element('p',authorizationSummary(row),'mono small'));if(row.operation==='dub'&&standardVoices(row).length)card.append(element('p','标准音色：'+standardVoices(row).map(voice=>voice.label||voice.id).join('、'),'muted small'));host.append(card);}if(!rows.length)host.append(element('p','能力清单暂不可用。','muted'));fillCapabilitySelect('transcription-capability','transcribe');fillCapabilitySelect('translation-capability','translate');fillCapabilitySelect('voice-provider','dub');chooseCapabilityForRecipe('translation-capability','translate',loadedAiRecipe.translation);chooseCapabilityForRecipe('voice-provider','dub',loadedAiRecipe.dubbing);updateVoiceOptions();syncAiControls();renderSignatures.delete('ai-tasks');renderSignatures.delete('plans');}
function renderAiTasks(rows){
  aiTasks=rows;const host=$('ai-tasks');host.replaceChildren();
  if(!selectedProject){host.append(element('p','请先打开一个编辑项目。','muted'));return;}
  if(!rows.length){host.append(element('p','当前项目尚无 AI 任务。','muted'));return;}
  for(const task of rows){
    const operation=task.operation==='translate'?'translate':'transcribe',item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div'),key='ai-task:'+task.id+':',cap=aiCapabilities.find(row=>row.operation===operation&&authorizationMatches(row,task.authorization_sha256)),unknownCalls=unknownInvocations('ai_task_id',task.id),ownerBlocked=invocationOwnerBlocked('ai_task_id',task.id),remoteLedgerRequired=task.authorization?.execution!=='local';
    copy.append(element('h3',(operationNames[task.operation]||task.operation)+'任务 '+String(task.id).slice(0,8)),element('p',(stateNames[task.state]||task.state)+' · '+task.provider+' · '+task.model,'muted small'));
    if(task.operation==='translate')copy.append(element('p','来源时间轴 '+String(task.source_revision_id||'').slice(0,8)+' · 目标语言 '+(task.target_language||'未知'),'muted small'));
    head.append(copy);item.append(head,element('p',task.authorization?dataScope(task.authorization):dataScope(null),'notice small'),element('p',authorizationSummary(task.authorization),'mono small'));
    if(unknownCalls.length)item.append(element('p','远程结果待核对','status-value'),element('p','账本中有 '+unknownCalls.length+' 次调用无法确认是否已被远程服务接受；请先在下方记录结论，当前任务不提供重试。','notice small'));
    else if(ownerBlocked)item.append(element('p','远程重试已阻止','status-value'),element('p','账本仍有发送中记录，或已经确认远程服务接受/放弃本次调用。为避免重复计费，当前任务不提供重试。','notice small'));
    const actions=element('div',undefined,'row');let consent=null;
    if(task.state==='review'&&!ownerBlocked&&task.authorization?.execution==='remote'){
      const label=element('label',undefined,'choice'),input=document.createElement('input');input.type='checkbox';input.dataset.aiEgress='';label.append(input,element('span','我确认按以上冻结的 runtime、模型声明、数据范围和硬预算执行；远程调用可能产生费用。'));item.append(label);consent=input;
    }
    if(task.state==='review'&&!ownerBlocked){
      const confirm=focusControl(element('button','确认并执行'),key+'confirm');confirm.type='button';confirm.dataset.aiConfirm=capabilityKey({operation,provider_id:task.provider,model_id:task.model,authorization_sha256:task.authorization_sha256});
      confirm.addEventListener('click',()=>mutate(async()=>{if(!task.authorization_sha256||!cap)throw new Error('此任务绑定的 AI runtime 已不可用，请重新创建任务。');if(task.authorization.execution==='remote'&&!consent?.checked)throw new Error('请先核对并确认冻结的数据范围与硬预算。');await api('/ai-tasks/'+encodeURIComponent(task.id)+'/confirm',{method:'POST',body:JSON.stringify({expected_request_sha256:task.request_sha256,expected_authorization_sha256:task.authorization_sha256,ai_data_egress_accepted:Boolean(consent?.checked)})});message('AI 任务已按冻结授权进入队列。');await refreshRecords();}));
      const cancel=focusControl(element('button','取消任务','secondary'),key+'cancel');cancel.type='button';cancel.addEventListener('click',()=>mutate(async()=>{await api('/ai-tasks/'+encodeURIComponent(task.id)+'/cancel',{method:'POST'});message('待确认 AI 任务已取消。');await refreshRecords();}));actions.append(confirm,cancel);
    }
    if(['queued','running','canceling'].includes(task.state)&&!(task.state==='queued'&&ownerBlocked)){const cancel=focusControl(element('button','请求取消','secondary'),key+'cancel');cancel.type='button';cancel.addEventListener('click',()=>mutate(async()=>{await api('/ai-tasks/'+encodeURIComponent(task.id)+'/cancel',{method:'POST'});message('已请求取消 AI 任务。');await refreshRecords();}));actions.append(cancel);}
    if(['failed','canceled'].includes(task.state)&&!ownerBlocked){if(remoteLedgerRequired&&!aiLedgerReady)item.append(element('p','远程调用账本暂不可用；在确认没有未解决记录前，重试入口保持暂停。','notice small'));else{item.append(element('p','中断前已完成的远程批次可能已经计费；重试沿用原冻结授权，runtime 变化时必须重新创建任务。','notice small'));const retry=focusControl(element('button','建立待确认重试任务','secondary'),key+'retry');retry.type='button';retry.addEventListener('click',()=>mutate(async()=>{await api('/ai-tasks/'+encodeURIComponent(task.id)+'/retry',{method:'POST',body:JSON.stringify({idempotency_key:randomKey('ai-retry')})});message('已建立待确认重试任务；尚未执行。');await refreshRecords();}));actions.append(retry);}}
    if(actions.children.length)item.append(actions);if(task.code)item.append(element('p',codeNames[task.code]||task.code,'muted small'));host.append(item);
  }
  updateActionState();
}
function cueLine(cue,parent){const item=element('li',undefined,'item'),timing=element('p',fmtClock(cue.start_ms)+' → '+fmtClock(cue.end_ms),'mono small');item.append(timing);if(parent)item.append(element('p','原文：'+parent.source_text,'muted small'),element('p','译文：'+cue.source_text));else item.append(element('p',cue.source_text));if(cue.speaker_id)item.append(element('p','说话人 '+cue.speaker_id,'muted small'));return item;}
async function reviewTimeline(revision,decision){const reviewed=await api('/timelines/'+encodeURIComponent(revision.id)+'/review',{method:'POST',body:JSON.stringify({decision,expected_review_version:revision.review_version})});timelines=timelines.map(item=>item.id===reviewed.id?reviewed:item);if(decision==='approved'&&reviewed.kind==='translation'){activeTranslationId=reviewed.id;$('use-translation').checked=true;syncAiControls();message('译文时间轴已批准；自动流程可继续，手动项目可再选择“用于当前编辑草稿”。');}else message(decision==='approved'?'字幕已批准，可从该时间轴建立翻译任务。':'时间轴已拒绝，不会写入编辑草稿。');await refreshRecords();}
function renderTimelines(rows){timelines=rows;const host=$('timelines');host.replaceChildren();if(!selectedProject){host.append(element('p','请先打开一个编辑项目。','muted'));return;}if(!rows.length){host.append(element('p','尚无待核对的字幕或译文。','muted'));syncAiControls();return;}const byId=new Map(rows.map(row=>[row.id,row]));for(const revision of rows){const item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div'),kind=revision.kind==='translation'?'译文':'字幕',parent=revision.parent_id?byId.get(revision.parent_id):null,key='timeline:'+revision.id+':';copy.append(element('h3',kind+' '+String(revision.id).slice(0,8)),element('p',(stateNames[revision.state]||revision.state)+' · '+revision.language+' · '+revision.provider+' · '+revision.model+' · '+revision.cue_count+' 段','muted small'));head.append(copy);item.append(head);const list=element('ol',undefined,'timeline-list');for(const cue of revision.cues||[]){const source=parent?.cues?.find(parentCue=>parentCue.id===cue.id&&parentCue.start_ms===cue.start_ms&&parentCue.end_ms===cue.end_ms)||null;list.append(cueLine(cue,revision.kind==='translation'?source:null));}item.append(list);const actions=element('div',undefined,'row');if(revision.state==='review'){const approve=focusControl(element('button','批准以上'+kind),key+'approve'),reject=focusControl(element('button','拒绝以上'+kind,'secondary'),key+'reject');approve.type=reject.type='button';approve.addEventListener('click',()=>mutate(()=>reviewTimeline(revision,'approved')));reject.addEventListener('click',()=>mutate(()=>reviewTimeline(revision,'rejected')));actions.append(approve,reject);}if(revision.kind==='transcription'&&revision.state==='approved'){const translate=focusControl(element('button','用此字幕建立待确认翻译任务','secondary'),key+'translate');translate.type='button';translate.dataset.translationSource=revision.id;translate.addEventListener('click',()=>mutate(()=>createTranslationTask(revision)));actions.append(translate);}if(revision.kind==='translation'&&revision.state==='approved'){const use=focusControl(element('button',activeTranslationId===revision.id?'已选择当前译文':'用于当前编辑草稿','secondary'),key+'use');use.type='button';use.addEventListener('click',()=>mutate(async()=>{activeTranslationId=revision.id;$('use-translation').checked=true;syncAiControls();await saveDraft();renderSignatures.delete('timelines');message('已批准译文已写入当前草稿；生成计划前仍可选择标准音色配音。');await refreshRecords();}));actions.append(use);}if(actions.children.length)item.append(actions);host.append(item);}syncAiControls();}
function syncAiControls(){const approved=timelines.filter(row=>row.kind==='translation'&&row.state==='approved');if(activeTranslationId&&!approved.some(row=>row.id===activeTranslationId))activeTranslationId=null;if(!activeTranslationId&&loadedAiRecipe.translation?.enabled){const match=approved.find(row=>row.language===loadedAiRecipe.translation.target_language&&row.provider===loadedAiRecipe.translation.provider&&row.model===loadedAiRecipe.translation.model);if(match)activeTranslationId=match.id;}const revision=selectedTranslation(),parent=parentTimeline(revision);$('source-language').replaceChildren(element('option',parent?.language||'批准字幕后自动填写'));$('source-language').value=$('source-language').options[0].value;if(revision)$('target-language').value=revision.language;$('use-translation').checked=!!revision&&($('use-translation').checked||!!loadedAiRecipe.translation?.enabled);if(revision&&loadedAiRecipe.dubbing?.enabled)$('dubbing-enabled').checked=true;if(!revision){$('use-translation').checked=false;$('dubbing-enabled').checked=false;}updateVoiceOptionsWithoutRecursion();updateActionState();}
function updateVoiceOptionsWithoutRecursion(){const select=$('voice-id'),previous=select.value,preferred=loadedAiRecipe.dubbing?.voice||'',voices=standardVoices(selectedCapability('dub'));select.replaceChildren();for(const voice of voices){const option=element('option',voice.label||voice.id);option.value=voice.id;select.append(option);}const wanted=[preferred,previous].find(value=>voices.some(voice=>voice.id===value));if(wanted)select.value=wanted;if(!voices.length){const option=element('option','此 provider 未公布标准音色');option.value='';select.append(option);}}
async function createTranscriptionTask(){if(!selectedProject)throw new Error('请先打开项目。');const capability=selectedCapability('transcribe');if(!capability?.authorization_sha256)throw new Error('当前没有带精确授权的听写能力。');const task=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/ai-tasks',{method:'POST',body:JSON.stringify({operation:'transcribe',provider:capability.provider_id,model:capability.model_id,options:{language:$('transcription-language').value||null,word_timestamps:true,vad:true},expected_authorization_sha256:capability.authorization_sha256,idempotency_key:randomKey('transcribe')})});message('已建立待确认听写任务 '+String(task.id).slice(0,8)+'；尚未执行。');await refreshRecords();}
async function createTranslationTask(source){const capability=selectedCapability('translate'),target=$('target-language').value;if(!capability?.authorization_sha256)throw new Error('当前没有带精确授权的翻译能力。');if(!target||target===source.language)throw new Error('目标语言必须与字幕语言不同。');const task=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/ai-tasks',{method:'POST',body:JSON.stringify({operation:'translate',provider:capability.provider_id,model:capability.model_id,source_revision_id:source.id,options:{source_language:source.language,target_language:target,glossary:[]},expected_authorization_sha256:capability.authorization_sha256,idempotency_key:randomKey('translate')})});message('已建立待确认翻译任务 '+String(task.id).slice(0,8)+'；尚未执行。');await refreshRecords();}
function renderPlans(plans){
  const host=$('plans');host.replaceChildren();
  if(!plans.length){host.append(element('p','尚无处理计划。','muted'));return;}
  for(const plan of plans){
    const item=element('article',undefined,'item'),head=element('div',undefined,'row-spread'),copy=element('div'),state=plan.state||'review',key='plan:'+plan.id+':',translation=plan.recipe?.translation,dubbing=plan.recipe?.dubbing,unknownCalls=unknownInvocations('render_plan_id',plan.id),ownerBlocked=invocationOwnerBlocked('render_plan_id',plan.id),remoteLedgerRequired=!!dubbing?.enabled&&dubbing.authorization?.execution!=='local';
    const speechCapability=dubbing?.enabled?aiCapabilities.find(row=>row.operation==='dub'&&authorizationMatches(row,plan.dubbing_authorization_sha256)):null;
    copy.append(element('h3','计划 '+String(plan.id).slice(0,8)),element('p',(stateNames[state]||state)+' · 草稿 v'+(plan.draft_version||plan.revision||'?')+(plan.progress!==undefined?' · '+Math.round(Number(plan.progress)*100)+'%':''),'muted'));
    if(translation?.enabled)copy.append(element('p','已批准译文 '+translation.source_language+' → '+translation.target_language+' · '+translation.provider+' · '+translation.model,'muted small'));
    if(dubbing?.enabled)copy.append(element('p','标准音色 '+dubbing.voice+' · '+String(Number(dubbing.rate??1))+'× · '+(dubbing.replace_original_audio?'替换原声':'压低原声后叠加')+' · '+dubbing.provider+' · '+dubbing.model,'muted small'),element('p',authorizationSummary(dubbing.authorization),'mono small'));
    head.append(copy);
    if(unknownCalls.length)item.append(element('p','远程结果待核对','status-value'),element('p','账本中有 '+unknownCalls.length+' 次配音调用无法确认是否已被远程服务接受；请先在远程调用账本记录结论，当前计划不提供重试。','notice small'));
    else if(ownerBlocked)item.append(element('p','远程重试已阻止','status-value'),element('p','账本仍有发送中记录，或已经确认远程服务接受/放弃本次配音调用。为避免重复计费，当前计划不提供重试。','notice small'));
    const actions=element('div',undefined,'row');
    let egressConsent=null;
    if(state==='review'&&!ownerBlocked&&dubbing?.enabled){
      const consent=element('label',undefined,'choice'),input=document.createElement('input'),summary=dubbing.authorization?.execution==='remote'
        ?'我确认此计划会把每个已批准译文 cue、标准音色和 '+String(Number(dubbing.rate??1))+'× 语速发送给 '+dubbing.provider+'；真实调用可能产生费用。冻结范围：'+((dubbing.authorization.data_egress||[]).join('、')||'未声明')+'。'
        :'我确认此计划使用 '+dubbing.provider+' · '+dubbing.model+'、'+String(Number(dubbing.rate??1))+'× 语速生成配音；当前能力清单声明为本地执行。';
      input.type='checkbox';input.dataset.planEgress='';
      consent.append(input,element('span',summary));
      item.append(consent);
      egressConsent=input;
    }
    if(state==='review'&&!ownerBlocked){
      const confirm=focusControl(element('button',dubbing?.enabled?'确认范围并开始处理':'确认并开始本地处理'),key+'confirm');
      confirm.type='button';
      confirm.addEventListener('click',()=>mutate(async()=>{
        if(dubbing?.enabled&&!speechCapability)throw new Error('此计划绑定的配音能力已不可用，请从新草稿生成计划。');
        if(dubbing?.enabled&&dubbing.authorization?.execution==='remote'&&!egressConsent?.checked)throw new Error('请先核对并确认此计划的配音数据范围与费用。');
        await api('/plans/'+encodeURIComponent(plan.id)+'/confirm',{method:'POST',body:JSON.stringify({expected_recipe_sha256:plan.recipe_sha256,expected_authorization_sha256:dubbing?.enabled?plan.dubbing_authorization_sha256:null,ai_data_egress_accepted:Boolean(dubbing?.enabled&&egressConsent?.checked)})});
        message('处理计划已确认并排队。');await refreshRecords();
      }));
      actions.append(confirm);
    }
    if(['queued','running'].includes(state)&&!(state==='queued'&&ownerBlocked)){
      const cancel=focusControl(element('button','取消','secondary'),key+'cancel');cancel.type='button';
      cancel.addEventListener('click',()=>mutate(async()=>{await api('/plans/'+encodeURIComponent(plan.id)+'/cancel',{method:'POST'});message('已请求取消处理。');await refreshRecords();}));
      actions.append(cancel);
    }
    if(['failed','canceled'].includes(state)&&!ownerBlocked&&(!remoteLedgerRequired||aiLedgerReady)){
      if(dubbing?.enabled)item.append(element('p','中断前已生成的远程配音 cue 可能已经计费；新计划会重新生成完整配音，可能再次计费。','notice small'));
      const retry=focusControl(element('button','创建重试计划','secondary'),key+'retry');retry.type='button';
      retry.addEventListener('click',()=>mutate(async()=>{await api('/plans/'+encodeURIComponent(plan.id)+'/retry',{method:'POST',body:JSON.stringify({idempotency_key:randomKey('plan-retry')})});message('已创建新的待核对重试计划；此前完成的远程配音 cue 可能已计费。');await refreshRecords();}));
      actions.append(retry);
    }
    if(['failed','canceled'].includes(state)&&!ownerBlocked&&remoteLedgerRequired&&!aiLedgerReady)item.append(element('p','远程调用账本暂不可用；在确认没有未解决记录前，重试入口保持暂停。','notice small'));
    if(actions.children.length)head.append(actions);
    item.prepend(head);
    if(plan.code)item.append(element('p',codeNames[plan.code]||plan.code,'notice small'));
    item.append(element('p','Recipe SHA-256：'+String(plan.recipe_sha256||plan.recipe_digest||'').slice(0,24)+'…','mono small'));
    host.append(item);
  }
}
function renderOutputs(outputs){const host=$('outputs'),kindNames={segment:'视频片段',cover:'封面',caption:'字幕',audio:'配音音频',dubbed_video:'配音视频'};host.replaceChildren();if(!outputs.length){host.append(element('p','尚无编辑成品。','muted'));return;}for(const output of outputs){const card=element('article',undefined,'output-card'),kind=output.kind||output.media_kind,key='output:'+output.id+':';card.append(element('h3',output.name||('编辑成品 '+String(output.id).slice(0,8))),element('p',(kindNames[kind]||'编辑成品')+' · '+fmtBytes(output.size_bytes||output.size)+(output.duration_ms?' · '+fmtTime(output.duration_ms):''),'muted small'));if(kind==='cover'){const image=document.createElement('img');image.src='/api/v1/edits/assets/'+encodeURIComponent(output.id)+'/content';image.alt=output.name||'编辑封面预览';image.loading='lazy';card.prepend(image);}const actions=element('div',undefined,'row');const download=focusControl(document.createElement('a'),key+'download');download.className='button-link secondary';download.href='/api/v1/edits/assets/'+encodeURIComponent(output.id)+'/content';download.download='';download.textContent='下载成品';actions.append(download);if(['segment','dubbed_video'].includes(kind)){const upload=focusControl(document.createElement('a'),key+'upload');upload.className='button-link';upload.href='/uploads?edit_output_id='+encodeURIComponent(output.id);upload.textContent='用于上传';actions.append(upload);}card.append(actions,element('p','SHA-256 '+String(output.sha256||'').slice(0,20)+'…','mono small'));host.append(card);}}
function currentProjectId(){return selectedProject?projectId(selectedProject):null;}
async function refreshRecords(){
  if(recordsPromise)return recordsPromise;
  const capturedGeneration=projectSelectionGeneration,capturedProjectId=currentProjectId(),capturedInvocationOffset=aiInvocationOffset,projectParam=capturedProjectId?'?project_id='+encodeURIComponent(capturedProjectId):'';
  const work=(async()=>{
    const [projects,plans,outputs,tasks,revisions,invocationSnapshot]=await Promise.all([api('/projects'),api('/plans'+projectParam),api('/assets'+projectParam),capturedProjectId?api('/ai-tasks'+projectParam):Promise.resolve([]),capturedProjectId?api('/timelines'+projectParam):Promise.resolve([]),capturedProjectId?loadInvocationSnapshot(capturedProjectId,capturedInvocationOffset):Promise.resolve({available:false,items:[],summary:{},error:''})]);
    if(capturedGeneration!==projectSelectionGeneration||capturedProjectId!==currentProjectId()||capturedInvocationOffset!==aiInvocationOffset)return false;
    aiInvocations=invocationSnapshot.items;aiInvocationSummary=invocationSnapshot.summary;aiLedgerReady=invocationSnapshot.available;aiLedgerError=invocationSnapshot.error;
    const ledgerGate={ready:aiLedgerReady,task_blocks:aiInvocationSummary.retry_blocked_ai_task_ids||[],plan_blocks:aiInvocationSummary.retry_blocked_render_plan_ids||[]};
    renderIfChanged('projects',{selected_project_id:capturedProjectId,items:projects},value=>renderProjects(value.items));
    renderIfChanged('ai-invocations',{project_id:capturedProjectId,available:aiLedgerReady,items:aiInvocations,summary:aiInvocationSummary,error:aiLedgerError},renderAiInvocations);
    renderIfChanged('plans',{items:plans,ledger:{ready:ledgerGate.ready,blocks:ledgerGate.plan_blocks}},value=>renderPlans(value.items));
    renderIfChanged('outputs',outputs,renderOutputs);
    renderIfChanged('ai-tasks',{project_id:capturedProjectId,items:tasks,ledger:{ready:ledgerGate.ready,blocks:ledgerGate.task_blocks}},value=>renderAiTasks(value.items));
    renderIfChanged('timelines',{project_id:capturedProjectId,items:revisions},value=>renderTimelines(value.items));
    return true;
  })();
  recordsPromise=work;
  try{return await work;}finally{if(recordsPromise===work)recordsPromise=null;}
}
async function refresh(){if(refreshPromise)return refreshPromise;const work=(async()=>{const capabilities=await api('/ai/capabilities');renderIfChanged('capabilities',capabilities,renderCapabilities);await refreshRecords();message('编辑器已连接。');})();refreshPromise=work;try{return await work;}finally{if(refreshPromise===work)refreshPromise=null;}}
function pageVisible(){return pageActive&&!document.hidden;}
function schedulePoll(){if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=pageVisible()?setTimeout(poll,2000):null;}
async function poll(){if(pollPromise)return pollPromise;if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;if(!pageVisible()){schedulePoll();return;}const work=(async()=>{try{if(!busy)await refreshRecords();}catch(error){message('状态刷新失败：'+error.message,true);}})();pollPromise=work;try{return await work;}finally{if(pollPromise===work)pollPromise=null;schedulePoll();}}
function updateActionState(){const transcribe=selectedCapability('transcribe'),translate=selectedCapability('translate'),revision=selectedTranslation(),dub=selectedCapability('dub'),voices=standardVoices(dub);$('create-plan').disabled=busy||!selectedProject;$('draft-form').querySelector('button[type="submit"]').disabled=busy||!selectedProject;$('create-transcription').disabled=busy||!selectedProject||!transcribe;$('transcription-capability').disabled=busy||!usableCapabilities('transcribe').length;$('translation-capability').disabled=busy||!usableCapabilities('translate').length;$('use-translation').disabled=busy||!revision;$('dubbing-enabled').disabled=busy||!revision||!$('use-translation').checked||!voices.length;$('voice-provider').disabled=busy||!revision||!usableCapabilities('dub').length;$('voice-id').disabled=busy||!revision||!$('dubbing-enabled').checked||!voices.length;$('voice-rate').disabled=busy||!revision||!$('dubbing-enabled').checked||!voices.length;$('mix-mode').disabled=busy||!revision||!$('dubbing-enabled').checked;document.querySelectorAll('[data-translation-source]').forEach(button=>{const source=timelines.find(row=>row.id===button.dataset.translationSource);button.disabled=busy||!selectedProject||!translate||!source||source.language===$('target-language').value;});document.querySelectorAll('[data-ai-confirm]').forEach(button=>{button.disabled=busy||!aiCapabilities.some(row=>capabilityKey(row)===button.dataset.aiConfirm&&row.status!=='blocked');});}
$('add-segment').addEventListener('click',()=>{addSegment();markDirty();});
$('cover-enabled').addEventListener('change',()=>{$('cover-controls').hidden=!$('cover-enabled').checked;markDirty();});
$('cover-current').addEventListener('click',()=>{$('cover-at').value=$('preview').currentTime.toFixed(3);markDirty();});
$('cover-controls').addEventListener('input',markDirty);
$('create-transcription').addEventListener('click',()=>mutate(createTranscriptionTask));
$('transcription-capability').addEventListener('change',updateActionState);
$('translation-capability').addEventListener('change',updateActionState);
$('target-language').addEventListener('change',updateActionState);
$('voice-provider').addEventListener('change',()=>{loadedAiRecipe.dubbing=null;updateVoiceOptions();markDirty();});
$('voice-id').addEventListener('change',()=>{loadedAiRecipe.dubbing=null;markDirty();});
$('voice-rate').addEventListener('input',()=>{loadedAiRecipe.dubbing=null;markDirty();});
$('mix-mode').addEventListener('change',()=>{loadedAiRecipe.dubbing=null;markDirty();});
$('use-translation').addEventListener('change',()=>{loadedAiRecipe.translation=null;if(!$('use-translation').checked){loadedAiRecipe.dubbing=null;$('dubbing-enabled').checked=false;}markDirty();updateActionState();});
$('dubbing-enabled').addEventListener('change',()=>{loadedAiRecipe.dubbing=null;markDirty();updateActionState();});
$('draft-form').addEventListener('submit',event=>{event.preventDefault();if(event.isComposing)return;mutate(saveDraft);});
$('create-plan').addEventListener('click',()=>mutate(async()=>{await saveDraft();const needsTimeline=!!draft.recipe?.translation?.enabled;if(needsTimeline&&!selectedTranslation())throw new Error('请先选择一份已批准译文，再生成处理计划。');const plan=await api('/projects/'+encodeURIComponent(projectId(selectedProject))+'/plans',{method:'POST',body:JSON.stringify({expected_version:draft.version,timeline_revision_id:needsTimeline?activeTranslationId:null,idempotency_key:randomKey('plan')})});message('已生成待核对计划 '+String(plan.id).slice(0,8)+'；确认前不会开始处理。');await refreshRecords();}));
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
