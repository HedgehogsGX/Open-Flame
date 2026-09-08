"""Upload workspace using the existing control page's visual language."""

UPLOAD_HTML = r'''<!doctype html>
<html lang="zh-CN" class="no-js" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Open-Flame · 上传</title>
  <link rel="stylesheet" href="/assets/open-flame.css">
  <script src="/assets/open-flame-shell.js" defer></script>
</head>
<body class="upload-page">
  <header class="topbar-shell">
    <div class="topbar">
      <a class="brand" href="/" aria-label="Open-Flame 下载首页">
        <span class="brand-mark" aria-hidden="true">OF</span><span>Open-Flame</span>
      </a>
      <nav class="primary-nav" aria-label="主要功能">
        <a class="nav-link" href="/">下载</a>
        <a class="nav-link" href="/edits">编辑</a>
        <a class="nav-link" href="/uploads" aria-current="page">上传</a>
        <a class="nav-link" href="/workflows">自动流程</a>
      </nav>
      <label class="theme-control"><span>外观</span><select data-of-theme aria-label="界面外观">
        <option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option>
      </select></label>
    </div>
  </header>
  <main class="of-shell">
    <header class="page-header">
      <p class="eyebrow">本地媒体工作台</p>
      <h1>核对后再上传</h1>
      <p class="lede">将本地视频或下载成品发送到 Bilibili、抖音和视频号。视频号还可选择保存平台草稿。</p>
      <p class="notice page-note">先创建本地草稿并核对内容，再逐个确认执行。上传账号单独登录，不使用下载 Cookie。结果来自上游工具；审核和公开状态需在平台核对。</p>
    </header>

    <section class="status-strip" aria-label="上传工作流摘要">
      <div class="status-tile"><p class="status-label">支持平台</p><p class="status-value">Bilibili · 抖音 · 视频号</p></div>
      <div class="status-tile"><p class="status-label">账号边界</p><p class="status-value">上传登录独立保存</p></div>
      <div class="status-tile"><p class="status-label">执行方式</p><p class="status-value">本地草稿 · 逐项确认</p></div>
    </section>

    <p id="message" role="status" aria-live="polite">正在连接上传器…</p>

    <section class="card" aria-labelledby="engine-heading">
      <div class="card-header">
        <div><p class="section-index">本次运行</p><h2 id="engine-heading">上传引擎</h2><p class="muted">引擎或记录状态阻塞时，原因和恢复入口会保持可见。</p></div>
        <div class="row"><button id="refresh" class="secondary" type="button">刷新状态</button><button id="recover-scheduler" class="secondary" type="button" hidden>恢复上传调度</button></div>
      </div>
      <p id="engine-status" class="status-value">正在读取状态…</p>
      <p id="engine-integrity" class="muted small" hidden></p>
      <p id="records-status" class="danger small" role="status" aria-live="polite" hidden></p>
      <details>
        <summary>登录与运行说明</summary>
        <p id="engine-help" class="muted small">引擎就绪后，选择平台并点击“添加并扫码登录”，二维码会直接显示在本页。用对应手机 App 扫码并确认，登录状态会自动更新。</p>
      </details>
    </section>

    <section class="card primary-card" aria-labelledby="accounts-heading">
      <div class="card-header"><div><p class="section-index">步骤 1</p><h2 id="accounts-heading">连接上传账号</h2><p class="muted">每个账号都通过对应手机 App 独立扫码登录。</p></div></div>
      <form id="account-form" aria-labelledby="accounts-heading">
        <label for="account-platform">平台</label>
        <select id="account-platform"><option value="bilibili">Bilibili</option><option value="douyin">抖音</option><option value="tencent">视频号</option></select>
        <label for="account-name">账号备注（可选）</label>
        <input id="account-name" maxlength="60" placeholder="例如：主账号；留空会自动命名">
        <button type="submit">添加并扫码登录</button>
      </form>
      <div id="login-panel" class="login-panel" hidden tabindex="-1" role="region" aria-labelledby="login-title">
        <div class="login-content">
          <div class="qr-frame"><img id="login-qr" alt="平台登录二维码，请用对应手机 App 扫码" hidden><p id="qr-placeholder" class="qr-placeholder">正在准备登录…</p></div>
          <div class="login-copy"><span id="login-account" class="login-badge"></span><h3 id="login-title">扫码登录</h3><p id="login-description" class="muted" role="status" aria-live="polite"></p><p id="login-expiry" class="small muted"></p><p class="small muted">扫码仅用于登录；上传和发布需要另外确认。</p></div>
        </div>
        <div class="row login-actions"><button id="login-retry" type="button">重新获取二维码</button><button id="login-cancel" class="secondary" type="button">取消登录</button><button id="login-done" class="secondary" type="button" hidden>完成</button></div>
      </div>
      <div id="accounts"></div>
      <div id="operations"></div>
    </section>

    <section class="card" aria-labelledby="source-heading">
      <div class="card-header"><div><p class="section-index">步骤 2</p><h2 id="source-heading">选择视频</h2><p class="muted">导入时会核验文件，并在上传存储中创建独立的受管副本。</p></div></div>
      <div id="asset-import" class="notice" hidden><p>已从下载器选择一份成品。导入后会核验文件并创建独立上传副本。</p><button id="import-asset" type="button">导入此下载成品</button></div>
      <div id="edit-output-import" class="notice" hidden><p>已从编辑工作台选择一份派生成品。导入后会再次核验文件并创建独立上传副本。</p><button id="import-edit-output" type="button">导入此编辑成品</button></div>
      <form id="source-form" aria-labelledby="source-heading">
        <label for="source-file">选择本地视频（最多 2 GiB）</label>
        <input id="source-file" type="file" accept="video/*,.mp4,.mov,.mkv,.webm,.avi,.m4v" required>
        <button type="submit">导入视频</button>
      </form>
      <div class="storage-summary" aria-labelledby="storage-heading"><h3 id="storage-heading">上传媒体占用</h3><p id="storage-summary" class="numeric muted">正在读取媒体占用…</p><p id="storage-warning" class="notice small" role="status" aria-live="polite" hidden></p></div>
      <label for="source-id">可用于新草稿的视频</label>
      <select id="source-id" aria-describedby="source-info job-form-error"><option value="">尚无可用视频</option></select>
      <p id="source-info" class="muted small"></p>
      <button id="more-sources" class="secondary" type="button" hidden>加载更多已导入视频</button>
      <section aria-labelledby="source-library-heading">
        <h3 id="source-library-heading">受管媒体记录</h3>
        <p class="muted small">Open-Flame 导入时创建独立受管副本。列表轮询快速检查路径、类型和大小；创建、确认、重试、删除及实际上传前会重新核对 SHA-256。删除受管副本不会删除你最初选择的本地文件或下载成品。</p>
        <div id="source-library"></div>
      </section>
      <div id="source-restore-panel" class="restore-panel" hidden tabindex="-1" role="region" aria-labelledby="source-restore-heading" aria-describedby="source-restore-description">
        <h3 id="source-restore-heading">恢复受管副本</h3>
        <p id="source-restore-description" class="muted"></p>
        <form id="source-restore-form"><label for="source-restore-file">选择与历史记录完全相同的视频</label><input id="source-restore-file" type="file" accept="video/*,.mp4,.mov,.mkv,.webm,.avi,.m4v" required><p class="small muted">服务端只会在文件大小和 SHA-256 与历史记录完全一致时恢复同一媒体记录。</p><div class="row"><button type="submit">校验并恢复受管副本</button><button id="source-restore-cancel" class="secondary" type="button">取消</button></div></form>
      </div>
      <section aria-labelledby="cover-library-heading">
        <h3 id="cover-library-heading">受管封面</h3>
        <p class="muted small">封面会复制到上传存储并核验；草稿只保存封面记录 ID，不保存浏览器本地路径。支持 JPEG、PNG 和 WebP，单张最多 20 MiB。</p>
        <form id="cover-form" aria-labelledby="cover-library-heading">
          <label for="cover-file">导入本地封面</label>
          <input id="cover-file" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" required>
          <button type="submit">导入封面</button>
        </form>
        <p id="cover-status" class="muted small">正在读取封面记录…</p>
        <div id="cover-library"></div>
        <button id="more-covers" class="secondary" type="button" hidden>加载更多受管封面</button>
      </section>
    </section>

    <section class="card" aria-labelledby="compose-heading">
      <div class="card-header"><div><p class="section-index">步骤 3</p><h2 id="compose-heading">创建本地草稿</h2><p class="muted">填写公开前的投稿信息。创建草稿本身不会上传视频。</p></div></div>
      <form id="job-form" aria-labelledby="compose-heading">
        <p id="job-form-error" class="danger" role="alert" hidden></p>
        <fieldset id="account-selection" tabindex="-1" aria-describedby="job-form-error"><legend>选择接收账号</legend><div id="account-choices"><p class="muted">请先添加账号并登录或检查登录态。</p></div></fieldset>
        <fieldset id="common-content"><legend>公共投稿内容</legend>
          <p class="muted small">公共内容会作为每个平台的默认值。需要不同文案时，在对应平台区域启用独立内容。</p>
          <label for="title">标题</label>
          <input id="title" required maxlength="100" aria-describedby="title-help job-form-error">
          <p id="title-help" class="muted small">请遵守所选平台标题长度限制，提交前会再次检查。</p>
          <label for="description">简介</label>
          <textarea id="description" maxlength="2000"></textarea>
          <label for="tags">标签（逗号分隔，不加 #）</label>
          <input id="tags" maxlength="500">
        </fieldset>
        <fieldset id="bilibili-options" hidden><legend>Bilibili 投稿设置</legend>
          <label class="choice"><input id="bilibili-use-overrides" type="checkbox"><span>使用 Bilibili 独立标题、简介和标签</span></label>
          <button id="copy-bilibili-common" class="secondary" type="button">复制公共内容到 Bilibili</button>
          <label for="bilibili-title">Bilibili 标题（最多 80 字）</label><input id="bilibili-title" maxlength="80" placeholder="未启用独立内容时使用公共标题" aria-describedby="job-form-error">
          <label for="bilibili-description">Bilibili 简介</label><textarea id="bilibili-description" maxlength="2000" placeholder="未启用独立内容时使用公共简介"></textarea>
          <label for="bilibili-tags">Bilibili 标签（逗号分隔，不加 #）</label><input id="bilibili-tags" maxlength="500" placeholder="未启用独立内容时使用公共标签">
          <label for="category-id">分区 ID（tid）</label><input id="category-id" type="number" min="1" max="10000" step="1" placeholder="填写目标分区 ID" aria-describedby="job-form-error">
          <label for="copyright">投稿类型</label><select id="copyright" aria-describedby="job-form-error"><option value="">请选择原创或转载</option><option value="1">原创</option><option value="2">转载</option></select>
          <label for="source-credit">转载来源</label><input id="source-credit" maxlength="200" placeholder="转载时必须填写来源" aria-describedby="job-form-error">
          <label for="bilibili-cover-id">封面</label><select id="bilibili-cover-id"><option value="">使用平台默认封面</option></select>
          <label for="bilibili-publish-at">定时发布（本机时区，至少提前 6 小时 5 分钟）</label><input id="bilibili-publish-at" type="datetime-local" aria-describedby="schedule-timezone job-form-error">
          <label for="bilibili-dynamic">动态文案（可选）</label><textarea id="bilibili-dynamic" maxlength="250"></textarea>
          <label class="choice"><input id="bilibili-no-reprint" type="checkbox"><span>声明禁止转载</span></label>
          <label class="choice"><input id="bilibili-close-comments" type="checkbox"><span>关闭评论</span></label>
          <label class="choice"><input id="bilibili-close-danmu" type="checkbox"><span>关闭弹幕</span></label>
          <p class="muted small">关闭评论与弹幕已接入固定上传器参数；当前默认提交接口的真实平台效果仍需逐项验收。</p>
        </fieldset>
        <fieldset id="douyin-options" hidden><legend>抖音投稿设置</legend>
          <label class="choice"><input id="douyin-use-overrides" type="checkbox"><span>使用抖音独立标题、简介和标签</span></label>
          <button id="copy-douyin-common" class="secondary" type="button">复制公共内容到抖音</button>
          <label for="douyin-title">抖音标题（最多 30 字）</label><input id="douyin-title" maxlength="30" placeholder="未启用独立内容时使用公共标题" aria-describedby="job-form-error">
          <label for="douyin-description">抖音简介</label><textarea id="douyin-description" maxlength="2000" placeholder="未启用独立内容时使用公共简介"></textarea>
          <label for="douyin-tags">抖音标签（逗号分隔，不加 #）</label><input id="douyin-tags" maxlength="500" placeholder="未启用独立内容时使用公共标签">
          <label for="douyin-cover-id">封面（按图片横竖方向写入对应槽位）</label><select id="douyin-cover-id"><option value="">使用平台默认封面</option></select>
          <label for="douyin-publish-at">定时发布（本机时区，至少提前 4 小时 5 分钟）</label><input id="douyin-publish-at" type="datetime-local" aria-describedby="schedule-timezone job-form-error">
          <label for="douyin-declaration">内容声明</label><select id="douyin-declaration"><option value="">不设置内容声明</option><option value="内容由AI生成">内容由 AI 生成</option><option value="内容为转载信息">内容为转载信息</option><option value="内容为个人观点或见解">内容为个人观点或见解</option></select>
        </fieldset>
        <fieldset id="tencent-options" hidden><legend>视频号投稿设置</legend>
          <label class="choice"><input id="tencent-use-overrides" type="checkbox"><span>使用视频号独立标题、简介和标签</span></label>
          <button id="copy-tencent-common" class="secondary" type="button">复制公共内容到视频号</button>
          <label for="tencent-title">视频号主文案第一行（最多 100 字）</label><input id="tencent-title" maxlength="100" placeholder="未启用独立内容时使用公共标题" aria-describedby="job-form-error">
          <label for="tencent-description">视频号简介</label><textarea id="tencent-description" maxlength="2000" placeholder="未启用独立内容时使用公共简介"></textarea>
          <label for="tencent-tags">视频号标签（逗号分隔，不加 #）</label><input id="tencent-tags" maxlength="500" placeholder="未启用独立内容时使用公共标签">
          <label for="tencent-mode">确认后的动作</label><select id="tencent-mode"><option value="draft">上传并保存平台草稿</option><option value="publish">立即上传发布</option></select>
          <label for="tencent-short-title">平台短标题（7–15 字）</label><input id="tencent-short-title" minlength="7" maxlength="15" aria-describedby="job-form-error" placeholder="留空则从主文案生成，草稿会显示最终值">
          <label for="tencent-landscape-cover-id">横版封面（4:3）</label><select id="tencent-landscape-cover-id"><option value="">不设置横版封面</option></select>
          <label for="tencent-portrait-cover-id">竖版封面（3:4）</label><select id="tencent-portrait-cover-id"><option value="">不设置竖版封面</option></select>
          <label for="tencent-publish-at">定时发布（本机时区，提前 4 小时 5 分钟至 28 天，只支持整点）</label><input id="tencent-publish-at" type="datetime-local" step="3600" aria-describedby="schedule-timezone job-form-error">
          <label for="tencent-content-label">内容标记</label><select id="tencent-content-label"><option value="">不设置内容标记</option><option value="含AI生成内容">含 AI 生成内容</option></select>
        </fieldset>
        <p id="schedule-timezone" class="muted small">定时值按本机当前时区解释；草稿会保存 UTC 时间与当前 UTC 偏移，确认执行前仍会重新检查平台提前量。</p>
        <p class="muted">创建后请在任务区核对每个平台的最终参数。此按钮只创建本地草稿，不上传视频。</p>
        <button type="submit">创建本地草稿并预览</button>
      </form>
    </section>

    <section class="card" aria-labelledby="jobs-heading">
      <div class="card-header"><div><p class="section-index">步骤 4</p><h2 id="jobs-heading">核对草稿与任务</h2><p class="muted">在这里逐项检查，再显式确认平台动作。</p></div></div>
      <p class="notice small">若结果不确定，请先在平台查看是否已经收到视频，再决定是否创建新的草稿。取消不能撤回平台已经接收的内容。</p>
      <div id="jobs"></div>
      <button id="more-jobs" class="secondary" type="button" hidden>加载更早任务</button>
    </section>
    <p class="page-footer">Open-Flame 0.28.0 · 本地优先 · 下载、编辑与上传数据相互隔离</p>
  </main>
  <script>
'use strict';
const $=id=>document.getElementById(id);
const platformNames={bilibili:'Bilibili',douyin:'抖音',tencent:'视频号'};
const browserTimeZone=Intl.DateTimeFormat().resolvedOptions().timeZone||'本机时区';
$('schedule-timezone').textContent='定时值按 '+browserTimeZone+' 解释；草稿会保存 UTC 时间与该日期的 UTC 偏移，确认执行前仍会重新检查平台提前量。';
const mediaStateNames={present:'副本可用',missing:'副本缺失',deleted:'副本已删除',changed:'内容或记录状态已变化',unsafe:'路径不安全'};
const stateNames={draft:'本地草稿',queued:'等待执行',running:'执行中',submitted:'上游报告投稿完成',draft_saved:'上游报告草稿已保存',failed:'失败',canceled:'已取消',unknown:'结果不确定',succeeded:'完成',ready:'可用',invalid:'登录失效',unchecked:'尚未检查',checking:'检查中',disconnected:'已断开本地账号'};
const codeNames={backend_unavailable:'上传引擎未安装或不可用',runtime_unavailable:'上传引擎未安装或不可用',runtime_missing:'上传引擎未安装',runtime_upgrade_required:'旧运行环境需要重建（保留账号与上传数据），见运行环境说明',account_not_ready:'请先登录或检查账号',authentication_required:'需要重新登录',login_required:'需要重新登录',source_changed:'视频校验不一致，请重新导入',source_too_large:'视频超过 2 GiB',source_empty:'视频为空',invalid_metadata:'投稿信息不符合平台要求',unknown_acknowledgement_required:'请先核对平台结果',upload_request_forbidden:'页面会话失效或请求来源不匹配，请刷新页面',uploader_stopped:'上传器已停止，请重启应用',internal_error:'上传器内部错误',invalid_cover_image:'封面不是受支持的有效静态图片',cover_changed:'封面内容或记录已变化',cover_reimport_required:'封面副本缺失，请重新导入并创建草稿',cover_in_use:'封面仍被未完成任务引用',publish_time_too_soon:'定时发布时间已进入平台最小提前量，请重建草稿',publish_time_precision_unsupported:'定时发布时间只支持整分钟',tencent_schedule_requires_whole_hour:'视频号定时发布只支持整点',tencent_schedule_too_far:'视频号定时发布最多提前 28 天'};
Object.assign(codeNames,{ready:'就绪',account_ready:'登录态可用',account_name_exists:'该平台已有同名账号，请修改备注',account_not_found:'账号不存在',account_operation_active:'该账号已有登录或检查任务，请等待或取消',account_missing:'尚无登录态，请先登录',account_invalid:'登录态已失效，请重新登录',login_failed:'登录未完成，请重试',login_terminal_unavailable:'当前环境无法打开 Bilibili 登录终端',backend_failed:'上传引擎执行失败，请检查运行环境',runtime_invalid:'运行环境校验失败，请按文档重新准备',invalid_accounts:'请重新选择接收账号',invalid_identifier:'所选记录无效，请刷新页面',bilibili_category_required:'请填写 Bilibili 分区 ID',bilibili_copyright_required:'请选择 Bilibili 原创或转载',account_session_changed:'账号已重新登录，请核对后再次确认',runtime_busy:'运行环境正在安装，请稍后刷新',bilibili_tags_required:'Bilibili 投稿至少需要一个标签',source_credit_required:'转载必须填写来源',source_credit_not_allowed:'原创投稿不能填写转载来源',source_not_found:'视频不存在，请重新导入',source_hash_mismatch:'下载成品校验不一致，请检查原件',source_size_invalid:'视频为空或超过 2 GiB',source_unavailable:'视频不可用，请重新导入',unsupported_video_type:'不支持此视频格式',source_changed:'视频已发生变化，请重新导入',title_too_long:'标题超过所选平台的长度限制',invalid_tags:'最多 10 个不重复标签，每个最多 20 字',invalid_source_name:'文件名称无效',upload_storage_full:'上传目录可用空间不足',unsafe_upload_file:'文件或目录不符合本地存储要求',verify_remote_result_first:'请先在平台核对结果，再勾选确认重试',interrupted_result_unknown:'上次上传中断，请先在平台核对是否已收到',upstream_result_unknown:'上游结果不确定，请在平台核对',upstream_submitted:'上游工具报告投稿完成，请在平台核对审核状态',upstream_draft_saved:'上游工具报告草稿已保存',schedule_window_elapsed:'定时发布时间已进入平台最小提前量，未点击最终投稿，请创建更晚的新草稿',platform_parameter_mismatch:'平台页面参数在最终提交前发生变化，未点击投稿，请重新创建草稿',restart_confirmation_required:'应用重启后需要再次确认',operation_interrupted:'上次账号操作中断，请重新检查',job_requires_new_draft:'此任务不能再次提交，请创建新草稿',job_not_found:'任务不存在',retry_not_allowed:'该任务当前不能重试',draft_mode_unsupported:'所选平台不支持草稿模式',idempotency_conflict:'提交内容已变化，请刷新页面后重新创建草稿',unsupported_platform_field:'当前平台不支持该参数',upload_worker_stopping:'上传器正在停止，请稍后重启',canceled:'已取消',cancelled:'已取消'});
Object.assign(codeNames,{platform_parameter_mismatch:'平台页面中的投稿参数已变化，未执行最终投稿，请重新创建并核对草稿'});
let csrf='', snapshot={accounts:[],sources:[],covers:[],jobs:[],operations:[],status:{},storage:null}, busy=false, pollPromise=null, timer=null, pageActive=true, pollingSuspended=!!document.hidden;
let draftSubmission=null, sourcesInitialized=false, jobToFocus=null;const unknownAcknowledgements=new Set(), jobDetailsOpen=new Map();
let sourceNextCursor=null,jobNextCursor=null,coverNextCursor=null,sourceHistoryExpanded=false,jobHistoryExpanded=false,coverHistoryExpanded=false;
const pinnedJobs=new Map(),pinnedSources=new Map();
let loginOperationId=null, loginAccountId=null, qrObjectUrl=null, qrKey=null, qrGeneration=0, qrRequest=null, loginTimer=null, restoreSourceId=null;
const accountDisconnectOpen=new Set(),sourceDeleteOpen=new Set(),coverDeleteOpen=new Set();let requestedFocusKey=null,inputComposing=false;
const loginPhases={queued:['等待登录通道','前面的账号操作完成后，会自动显示二维码。'],preparing:['正在获取二维码','正在连接平台，请稍候。'],waiting_scan:['请扫码登录','用对应手机 App 扫描左侧二维码，并在手机上确认。'],scanned:['已扫码，等待手机确认','请在手机上确认本次登录，页面会自动更新。'],verification_required:['平台要求额外验证','请完成平台要求的验证。如仍无法继续，可取消后重新扫码。'],expired:['二维码已过期','点击“重新获取二维码”开始新的登录。'],ready:['登录成功','账号已连接，可以继续创建上传草稿。'],failed:['登录未完成','可以重新获取二维码再试一次。'],canceled:['登录已取消','需要时可以重新扫码登录。']};
Object.assign(codeNames,{login_owned_by_other_instance:'请在正在运行上传任务的应用中扫码登录',login_expired:'二维码已过期',login_qr_expired:'二维码已过期',login_timeout:'登录等待超时，请重新扫码',login_qr_unavailable:'暂时无法获取二维码，请重试',login_verification_required:'平台要求额外验证，请完成验证后重试'});
Object.assign(codeNames,{upload_database_unavailable:'上传数据库暂时不可用',scheduler_database_unavailable:'上传数据库暂时不可用',scheduler_failed:'上传调度发生内部故障',scheduler_recovery_failed:'上传调度仍无法恢复，请检查本地存储',scheduler_recovered:'上传调度已恢复；中断任务需要重新核对或确认',scheduler_owned_by_other_instance:'由另一个应用实例执行'});
Object.assign(codeNames,{account_busy:'账号仍有正在执行的操作或任务，请等待结束或先取消',account_upload_active:'该账号仍有上传正在执行，请等待结束',account_disconnected:'本地账号已断开',account_disconnect_cleanup_failed:'账号断开后的本地清理未完成，请重试',account_disconnected_confirmation_revoked:'账号断开后，旧确认已撤回，请重新登录并核对'});
Object.assign(codeNames,{account_invalid_confirmation_revoked:'账号登录态失效后，尚未执行的投稿确认已撤回；请重新登录并重新核对'});
Object.assign(codeNames,{source_media_in_use:'仍有未完成任务引用此视频',source_in_use:'仍有未完成任务引用此视频',source_media_missing:'受管副本缺失，请重新导入完全相同的视频',source_reimport_required:'受管副本不可用，请重新导入完全相同的视频',source_media_hash_mismatch:'所选视频与历史记录不一致',source_restore_mismatch:'所选视频与历史记录不一致，受管副本没有恢复',source_restore_conflict:'媒体状态已经变化，请刷新后再恢复',source_restore_failed:'恢复受管副本失败，原记录保持不变',source_already_present:'受管副本已经可用',source_delete_failed:'删除受管副本失败，原文件保持不变',source_delete_cleanup_failed:'删除记录已保存，但隔离副本清理失败；请检查容量与未登记文件清单',source_delete_rollback_failed:'删除更新失败且原路径未能恢复；请立即检查上传媒体目录',source_storage_invalid:'上传媒体目录状态不安全，请检查本地存储',binary_source_required:'恢复受管副本需要选择本地视频文件'});
Object.assign(codeNames,{legacy_tags_normalized_review_required:'旧版标签中的井号或全角逗号已安全规范化，请核对后再确认',legacy_platform_options_review_required:'旧版任务的隐式平台参数已显式保存，其他旧元数据也可能已规范化，请核对后再确认',legacy_bilibili_source_credit_removed:'旧版原创任务中矛盾的转载来源已移除，请重新核对后再确认',legacy_metadata_restart_confirmation_required:'旧版待执行任务及平台参数已迁移，请重新核对后再确认',legacy_metadata_interrupted_result_unknown:'旧版任务曾执行且平台参数已迁移，请先到平台核对结果'});
Object.assign(codeNames,{cover_not_found:'封面不存在，请重新选择',cover_unavailable:'封面不可用，请重新导入',unsupported_cover_type:'只支持 JPEG、PNG 和 WebP 封面',cover_too_large:'封面超过 20 MiB',cover_empty:'封面文件为空',publish_time_too_soon:'定时发布时间未达到平台提前量',publish_time_invalid:'定时发布时间无效',invalid_publish_time:'定时发布时间无效',scheduled_draft_unsupported:'视频号保存平台草稿时不能设置定时发布',draft_schedule_unsupported:'视频号保存平台草稿时不能设置定时发布',portrait_cover_unsupported:'该平台不支持竖版封面槽位',multiple_covers_unsupported:'抖音一次只能设置一张横版或竖版封面',douyin_cover_orientation_invalid:'抖音封面方向与所选横版或竖版槽位不一致',tencent_cover_ratio_invalid:'视频号封面比例不符合对应的 4:3 或 3:4 槽位',unsupported_declaration:'不支持此抖音内容声明',unsupported_content_label:'不支持此视频号内容标记',tencent_short_title_length:'视频号短标题需为 7–15 字',invalid_platform_options:'平台投稿参数无效',invalid_platform_option:'平台投稿参数无效',unsupported_platform_option:'平台投稿参数不受支持',unsupported_platform_field:'该投稿字段不适用于所选平台'});
function clearQR(){qrGeneration++;if(qrRequest)qrRequest.abort();qrRequest=null;if(qrObjectUrl)URL.revokeObjectURL(qrObjectUrl);qrObjectUrl=null;qrKey=null;$('login-qr').hidden=true;$('login-qr').removeAttribute('src');}
function selectedLogin(){return snapshot.operations.find(item=>item.id===loginOperationId&&item.action==='login');}
function pageVisible(){return pageActive&&!document.hidden;}
function qrStillCurrent(id,revision){const op=selectedLogin();return pageVisible()&&op&&op.id===id&&op.qr_revision===revision&&op.state==='running'&&op.qr_available&&op.login_phase==='waiting_scan'&&(!op.expires_at||op.expires_at*1000>Date.now());}
async function loadLoginQR(op){
  const key=op.id+':'+op.qr_revision;if(qrKey===key)return;clearQR();qrKey=key;
  const generation=qrGeneration;const controller=new AbortController();qrRequest=controller;
  const timeout=setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch('/api/v1/uploads/operations/'+encodeURIComponent(op.id)+'/qr',{headers:new Headers({'X-Upload-CSRF':csrf}),cache:'no-store',signal:controller.signal});
    if(!response.ok||response.headers.get('Content-Type')?.split(';')[0]!=='image/png')throw new Error('qr_unavailable');
    const blob=await response.blob();if(blob.size>524288)throw new Error('qr_invalid');
    if(generation!==qrGeneration||!qrStillCurrent(op.id,op.qr_revision))return;
    qrObjectUrl=URL.createObjectURL(blob);$('login-qr').src=qrObjectUrl;$('login-qr').hidden=false;$('qr-placeholder').hidden=true;
  }catch(error){if(generation===qrGeneration){qrKey=null;$('qr-placeholder').hidden=false;$('qr-placeholder').textContent='二维码暂未就绪，正在重试…';}}
  finally{clearTimeout(timeout);if(qrRequest===controller)qrRequest=null;}
}
function renderLogin(){
  if(!pageVisible()){clearQR();return;}
  if(!loginOperationId){const active=snapshot.operations.find(item=>item.action==='login'&&['queued','running'].includes(item.state));if(active){loginOperationId=active.id;loginAccountId=active.account_id;}}
  const op=selectedLogin();if(!op){$('login-panel').hidden=true;clearQR();return;}
  const account=snapshot.accounts.find(item=>item.id===op.account_id);loginAccountId=op.account_id;
  let phase=op.state==='running'?(op.login_phase||'preparing'):op.state;
  if(op.code==='cancellation_requested')phase='canceling';
  if(op.state==='failed'&&['login_qr_expired','login_expired','login_timeout'].includes(op.code))phase='expired';
  if(op.state==='failed'&&op.code==='login_verification_required')phase='verification_required';
  if(op.expires_at&&op.expires_at*1000<=Date.now()&&phase==='waiting_scan')phase='expired';
  const copy=phase==='canceling'?['正在取消登录','正在关闭本次登录会话。']:loginPhases[phase]||loginPhases.preparing;
  $('login-panel').hidden=false;$('login-panel').dataset.state=phase;
  $('login-account').textContent=account?platformNames[account.platform]+' · '+account.name:'账号登录';
  $('login-title').textContent=copy[0];let description=copy[1]+(op.state==='failed'&&op.code?' '+codeText(op.code):'');
  if(phase==='waiting_scan'&&account){const app={bilibili:'Bilibili App',douyin:'抖音 App',tencent:'微信'}[account.platform];description='使用'+app+'扫码，并在手机上确认本次登录。'+(account.platform==='bilibili'?'平台授权页可能显示 TV 客户端。':'');}
  if($('login-description').textContent!==description)$('login-description').textContent=description;
  $('login-expiry').textContent=phase==='waiting_scan'&&op.expires_at?'约 '+Math.max(0,Math.ceil(op.expires_at-Date.now()/1000))+' 秒后过期':'';
  const active=['queued','running'].includes(op.state);
  $('login-cancel').hidden=!active;$('login-retry').hidden=phase==='ready';$('login-done').hidden=active;
  $('login-retry').disabled=busy||phase==='canceling';$('login-cancel').disabled=busy||phase==='canceling';
  if(phase==='waiting_scan'&&op.qr_available){$('qr-placeholder').textContent='正在加载二维码…';loadLoginQR(op);}
  else{clearQR();$('qr-placeholder').hidden=false;$('qr-placeholder').textContent=phase==='ready'?'✓ 登录成功':copy[0];}
}
async function startLogin(accountId){clearQR();loginAccountId=accountId;message('正在创建登录会话…');const op=await api('/accounts/'+encodeURIComponent(accountId)+'/login',{method:'POST'});loginOperationId=op.id;message('本页会自动更新登录状态。');await refresh();$('login-panel').focus();}
async function restartLogin(target){const op=target.operationId?snapshot.operations.find(item=>item.id===target.operationId):null;const accountId=target.accountId;if(!accountId)return;clearQR();
  if(op&&['queued','running'].includes(op.state)){await api('/operations/'+encodeURIComponent(op.id)+'/cancel',{method:'POST'});
    for(let attempt=0;attempt<50;attempt++){const operations=await api('/operations');const previous=operations.find(item=>item.id===op.id);if(!previous||!['queued','running'].includes(previous.state))break;if(attempt===49)throw new Error('正在结束上一次登录，请稍后重新获取二维码。');await new Promise(resolve=>setTimeout(resolve,150));}}
  await startLogin(accountId);
}
$('login-retry').addEventListener('click',()=>{const op=selectedLogin(),target={operationId:op?.id||null,accountId:loginAccountId};mutate(()=>restartLogin(target));});
$('login-cancel').addEventListener('click',()=>{const operationId=selectedLogin()?.id||null;mutate(async()=>{clearQR();if(operationId)await api('/operations/'+encodeURIComponent(operationId)+'/cancel',{method:'POST'});message('已取消本次登录。');});});
$('login-done').addEventListener('click',()=>{clearQR();loginOperationId=null;loginAccountId=null;$('login-panel').hidden=true;});
function scheduleLoginTick(){if(loginTimer!==null)clearTimeout(loginTimer);loginTimer=pageVisible()?setTimeout(tickLogin,1000):null;}
function tickLogin(){if(loginTimer!==null)clearTimeout(loginTimer);loginTimer=null;if(!pageVisible())return;if(selectedLogin())renderLogin();scheduleLoginTick();}
scheduleLoginTick();
function element(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
function codeText(code){return code?codeNames[code]||code:'';}
function message(text,error=false){$('message').textContent=text;$('message').className=error?'danger':'muted';}
async function api(path,options={}){
  const headers=new Headers(options.headers||{});
  if(options.method && options.method!=='GET')headers.set('X-Upload-CSRF',csrf);
  if(options.json!==undefined){headers.set('Content-Type','application/json');options.body=JSON.stringify(options.json);delete options.json;}
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),options.body instanceof Blob?600000:30000);
  try{const response=await fetch('/api/v1/uploads'+path,{...options,headers,cache:'no-store',signal:controller.signal});
    let payload;try{payload=await response.json();}catch{throw new Error('服务器返回了无法读取的响应');}
    if(!response.ok){const detail=typeof payload.detail==='string'?codeText(payload.detail):'请检查必填信息和平台限制',error=new Error(detail+'（HTTP '+response.status+'）');error.status=response.status;error.code=typeof payload.detail==='string'?payload.detail:'';throw error;}return payload;
  }finally{clearTimeout(timeout);}
}
function button(text,action,capture=()=>undefined){const item=element('button',text);item.type='button';item.disabled=busy;item.addEventListener('click',()=>{if(busy)return;let captured;try{captured=capture();}catch(error){message(error.message||String(error),true);return;}mutate(()=>action(captured));});return item;}
function localButton(text,action){const item=element('button',text);item.type='button';item.disabled=busy;item.addEventListener('click',()=>{if(!busy)action();});return item;}
function focusContext(){return {key:document.activeElement?.dataset?.focusKey||null,targets:new Map()};}
function focusControl(context,item,key){item.dataset.focusKey=key;context.targets.set(key,item);return item;}
function collectFocusTargets(context,item){if(item.dataset?.focusKey)context.targets.set(item.dataset.focusKey,item);for(const child of item.children||[])collectFocusTargets(context,child);}
function restoreRenderedFocus(context){const key=requestedFocusKey||context.key,target=context.targets.get(key);if(target&&!target.disabled){target.focus({preventScroll:true});if(requestedFocusKey===key)requestedFocusKey=null;}}
function renderSignature(value){return JSON.stringify(value);}
function refreshKeyedNode(target,fresh){target.className=fresh.className;target.tabIndex=fresh.tabIndex;target.hidden=fresh.hidden;target.disabled=fresh.disabled;target.value=fresh.value;target.checked=fresh.checked;for(const key of Object.keys(target.dataset))delete target.dataset[key];Object.assign(target.dataset,fresh.dataset);if(fresh.children.length)target.replaceChildren(...fresh.children);else target.textContent=fresh.textContent;}
function reconcileKeyed(parent,specs,context){const existing=new Map([...parent.children].filter(item=>item.dataset?.renderKey).map(item=>[item.dataset.renderKey,item])),next=[];for(const spec of specs){let item=existing.get(spec.key),signature=renderSignature(spec.signature);if(!item){item=spec.build();}else if(item.dataset.renderSignature!==signature){const fresh=spec.build();refreshKeyedNode(item,fresh);}item.dataset.renderKey=spec.key;item.dataset.renderSignature=signature;collectFocusTargets(context,item);next.push(item);}for(let index=0;index<next.length;index++){const current=parent.children[index];if(current!==next[index])parent.insertBefore(next[index],current||null);}const keep=new Set(next);for(const item of [...parent.children])if(!keep.has(item))item.remove();}
function disconnectedTime(value){if(!value)return '';const parsed=new Date(value);return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString('zh-CN');}
function formatBytes(value){const bytes=Math.max(0,Number(value)||0);if(bytes<1024)return Math.round(bytes)+' B';if(bytes<1048576)return (bytes/1024).toFixed(1)+' KiB';if(bytes<1073741824)return (bytes/1048576).toFixed(1)+' MiB';return (bytes/1073741824).toFixed(1)+' GiB';}
async function disconnectLocalAccount(account,retry=false){try{const result=await api('/accounts/'+encodeURIComponent(account.id)+'/disconnect',{method:'POST'});accountDisconnectOpen.delete(account.id);snapshot.accounts=snapshot.accounts.map(entry=>entry.id===account.id?result.account:entry);requestedFocusKey='account:'+account.id+':card';if(loginAccountId===account.id){clearQR();loginOperationId=null;loginAccountId=null;$('login-panel').hidden=true;}message(retry?'本地登录清理已完成。':'已断开本地账号；已撤回 '+result.revoked_confirmation_count+' 项旧确认，取消 '+result.canceled_operation_count+' 项待执行账号操作。');}catch(error){await refresh();throw error;}}
function renderStorage(){const storage=snapshot.storage;if(!storage){$('storage-summary').textContent='媒体占用暂不可用。';$('storage-warning').textContent='';$('storage-warning').hidden=true;return;}
  const count=value=>Math.max(0,Number(value)||0),parts=['受管副本 '+formatBytes(storage.managed_bytes),count(storage.registered_source_count)+' 份记录',count(storage.present_source_count)+' 份可用',count(storage.missing_source_count)+' 份缺失',count(storage.deleted_source_count)+' 份已删除'];if(count(storage.changed_source_count))parts.push(count(storage.changed_source_count)+' 份内容变化');if(count(storage.unsafe_source_count))parts.push(count(storage.unsafe_source_count)+' 份路径不安全');if(count(storage.orphan_file_count))parts.push(count(storage.orphan_file_count)+' 份未登记文件（'+formatBytes(storage.orphan_bytes)+'）');$('storage-summary').textContent=parts.join(' · ');
  const warnings=[];if(storage.low_space)warnings.push('本地可用空间仅 '+formatBytes(storage.free_bytes)+'；导入视频后还需保留 '+formatBytes(storage.reserve_bytes)+'。请先释放空间。');if(count(storage.changed_source_count)||count(storage.unsafe_source_count)||count(storage.orphan_file_count)||count(storage.unsafe_entry_count))warnings.push('存储审计发现异常或未登记条目；这里只读显示，不会自动清理，请检查上传媒体目录。');const warning=warnings.join(' ');if($('storage-warning').textContent!==warning)$('storage-warning').textContent=warning;$('storage-warning').hidden=!warning;}
async function mutate(action){if(busy)return;busy=true;document.querySelectorAll('button').forEach(item=>item.disabled=true);
  try{if(pollPromise)await pollPromise;await action();await refresh();}catch(error){message(error.message||String(error),true);}
  finally{busy=false;document.querySelectorAll('button').forEach(item=>item.disabled=false);renderAccounts();renderSources();renderJobs();renderLogin();if(jobToFocus){let target=[...$('jobs').children].find(item=>item.dataset.jobId===jobToFocus);if(!target){try{const record=await api('/jobs/'+encodeURIComponent(jobToFocus));pinJob(record);snapshot.jobs=applyUpdates(snapshot.jobs,[record]);if(!snapshot.sources.some(item=>item.id===record.source_id)){const source=await api('/sources/'+encodeURIComponent(record.source_id));pinSource(source);snapshot.sources=applyUpdates(snapshot.sources,[source]);renderSources();}renderJobs();target=[...$('jobs').children].find(item=>item.dataset.jobId===jobToFocus);}catch{}}jobToFocus=null;if(target){target.scrollIntoView({block:'nearest'});target.focus();}}}}
function selectedAccounts(){return [...document.querySelectorAll('#account-choices input:checked')].map(item=>item.value);}
function updateMetadata(){const selected=new Set(selectedAccounts());const platforms=snapshot.accounts.filter(item=>selected.has(item.id)).map(item=>item.platform);const bili=platforms.includes('bilibili'),repost=bili&&$('copyright').value==='2';$('bilibili-options').hidden=!bili;$('douyin-options').hidden=!platforms.includes('douyin');$('tencent-options').hidden=!platforms.includes('tencent');$('category-id').required=bili;$('copyright').required=bili;$('source-credit').required=repost;$('source-credit').disabled=!repost;
  const limits=(snapshot.status.platforms||[]).filter(item=>platforms.includes(item.id)).map(item=>platformNames[item.id]+' '+item.title_limit+' 字');$('title-help').textContent=limits.length?'标题限制：'+limits.join('；'):'请先选择接收账号。';}
function renderAccounts(){
  const selected=new Set(selectedAccounts()),focus=focusContext(),accountSpecs=[],choiceSpecs=[];
  for(const account of snapshot.accounts){
    const connected=(account.lifecycle_state||'active')==='active',active=snapshot.operations.find(op=>op.account_id===account.id&&['queued','running'].includes(op.state));
    if(!connected)accountDisconnectOpen.delete(account.id);
    accountSpecs.push({key:'account:'+account.id,signature:[account.platform,account.name,account.auth_state,account.lifecycle_state,account.code,account.disconnected_at,active?.id||null,active?.action||null,active?.state||null,accountDisconnectOpen.has(account.id)],build:()=>{
      const item=focusControl(focus,element('div',undefined,'item'),'account:'+account.id+':card');item.tabIndex=-1;item.dataset.accountId=account.id;item.append(element('strong',platformNames[account.platform]+' · '+account.name));
      const visibleState=connected?(stateNames[account.auth_state]||account.auth_state):stateNames.disconnected;item.append(element('p',visibleState+(account.code?' · '+codeText(account.code):''),'muted'));
      if(!connected){const when=disconnectedTime(account.disconnected_at),cleanupFailed=account.code==='account_disconnect_cleanup_failed';item.append(element('p',(cleanupFailed?'此历史账号已停用，但本地登录文件清理尚未完成。账号不能再用于登录或上传，请重试清理。':'此历史账号仅保留任务归属与备注；本地登录已移除，不会撤销平台侧授权，平台会话需要在平台内另行处理。')+(when?' 断开时间：'+when:''),'small muted'));if(cleanupFailed){const retryCleanup=focusControl(focus,button('重试清理本地登录',()=>disconnectLocalAccount(account,true)),'account:'+account.id+':cleanup');retryCleanup.className='secondary';item.append(retryCleanup);}return item;}
      const actions=element('div',undefined,'row');
      if(active&&active.action==='login')actions.append(focusControl(focus,button('查看二维码',async()=>{loginOperationId=active.id;loginAccountId=account.id;renderLogin();$('login-panel').focus();}),'account:'+account.id+':qr'));
      else{const login=focusControl(focus,button(account.auth_state==='ready'?'重新扫码登录':'扫码登录',()=>startLogin(account.id)),'account:'+account.id+':login');login.disabled=busy||!!active;actions.append(login);}
      const checkLogin=focusControl(focus,button('检查登录态',async()=>{await api('/accounts/'+encodeURIComponent(account.id)+'/check',{method:'POST'});message('登录态检查已排队。');}),'account:'+account.id+':check');checkLogin.className='secondary';checkLogin.disabled=busy||!!active;actions.append(checkLogin);
      const disconnect=focusControl(focus,localButton('断开本地账号',()=>{accountDisconnectOpen.add(account.id);requestedFocusKey='account:'+account.id+':disconnect-confirm';renderAccounts();}),'account:'+account.id+':disconnect');disconnect.className='danger-action';disconnect.disabled=busy||!!active;actions.append(disconnect);item.append(actions);
      if(active)item.append(element('p','账号操作进行中；结束或取消后才能断开本地账号。','small muted'));
      if(accountDisconnectOpen.has(account.id)){const confirm=element('div',undefined,'inline-confirm');confirm.setAttribute('role','group');confirm.setAttribute('aria-label','确认断开本地账号');confirm.append(element('p','只会移除 Open-Flame 的本地登录状态，不会撤销平台侧授权。尚未执行的旧确认会退回本地草稿；历史任务和媒体记录继续保留。'));
        const confirmActions=element('div',undefined,'row'),apply=focusControl(focus,button('确认断开本地账号',()=>disconnectLocalAccount(account)),'account:'+account.id+':disconnect-confirm');apply.className='danger-action';apply.disabled=busy||!!active;
        const dismiss=focusControl(focus,localButton('取消',()=>{accountDisconnectOpen.delete(account.id);requestedFocusKey='account:'+account.id+':disconnect';renderAccounts();}),'account:'+account.id+':disconnect-cancel');dismiss.className='secondary';confirmActions.append(apply,dismiss);confirm.append(confirmActions);item.append(confirm);}
      return item;
    }});
    if(connected)choiceSpecs.push({key:'choice:'+account.id,signature:[account.id,account.platform,account.name,account.auth_state],build:()=>{const choice=element('label',undefined,'choice'),check=focusControl(focus,element('input'),'account:'+account.id+':choice');check.type='checkbox';check.value=account.id;check.checked=selected.has(account.id);check.addEventListener('change',updateMetadata);choice.append(check,element('span',platformNames[account.platform]+' · '+account.name+'（'+(stateNames[account.auth_state]||account.auth_state)+'）'));return choice;}});
  }
  reconcileKeyed($('accounts'),accountSpecs,focus);
  if(!choiceSpecs.length){const copy=snapshot.accounts.length?'请添加或连接可用账号。':'请先添加账号。';choiceSpecs.push({key:'choice:empty',signature:copy,build:()=>element('p',copy,'muted')});}
  reconcileKeyed($('account-choices'),choiceSpecs,focus);updateMetadata();
  const operationSpecs=snapshot.operations.filter(item=>item.action!=='login'&&['queued','running','failed','unknown'].includes(item.state)).slice(0,5).map(operation=>{const account=snapshot.accounts.find(item=>item.id===operation.account_id);return {key:'operation:'+operation.id,signature:[operation.account_id,operation.action,operation.state,operation.code,account?.name||null],build:()=>{const item=element('div',undefined,'item');item.append(element('p',(account?account.name:'账号操作')+' · '+(operation.action==='login'?'登录':'检查')+' · '+(stateNames[operation.state]||operation.state)+' '+codeText(operation.code)));if(['queued','running'].includes(operation.state))item.append(focusControl(focus,button('取消账号操作',async()=>{await api('/operations/'+encodeURIComponent(operation.id)+'/cancel',{method:'POST'});message('已请求取消账号操作。');}),'operation:'+operation.id+':cancel'));return item;}};});
  reconcileKeyed($('operations'),operationSpecs,focus);restoreRenderedFocus(focus);
}
function mergeFirstPage(first,existing,keepExisting){if(!keepExisting)return [...first];const seen=new Set(first.map(item=>item.id));return [...first,...existing.filter(item=>!seen.has(item.id))];}
function appendPage(existing,items){const seen=new Set(existing.map(item=>item.id));return [...existing,...items.filter(item=>!seen.has(item.id))];}
function remember(map,item){if(!item||!item.id)return;map.delete(item.id);map.set(item.id,item);while(map.size>64)map.delete(map.keys().next().value);}
function pinJob(item){remember(pinnedJobs,item);}function pinSource(item){remember(pinnedSources,item);}
function applyUpdates(existing,updates){const fresh=new Map(updates.map(item=>[item.id,item])),seen=new Set();const result=existing.map(item=>{seen.add(item.id);return fresh.get(item.id)||item;});for(const item of updates)if(!seen.has(item.id))result.push(item);return result;}
function sourceMediaState(source){return source.media_state||(source.media_present===false?'missing':'present');}
function sourceMediaPresent(source){return sourceMediaState(source)==='present'&&source.media_present!==false;}
function renderSources(){
  const previous=$('source-id').value,focus=focusContext(),usable=snapshot.sources.filter(sourceMediaPresent);
  const optionSpecs=[{key:'source-option:empty',signature:'请选择视频',build:()=>{const option=element('option','请选择视频');option.value='';return option;}}];
  for(const source of usable)optionSpecs.push({key:'source-option:'+source.id,signature:[source.name,source.size],build:()=>{const option=element('option',source.name+' · '+(source.size/1048576).toFixed(1)+' MiB');option.value=source.id;return option;}});
  reconcileKeyed($('source-id'),optionSpecs,focus);if(usable.some(item=>item.id===previous))$('source-id').value=previous;else $('source-id').value=!sourcesInitialized&&usable.length?usable[0].id:'';sourcesInitialized=true;$('more-sources').hidden=!sourceNextCursor;showSource();
  const sourceSpecs=[];
  if(!snapshot.sources.length)sourceSpecs.push({key:'source:empty',signature:'尚无受管媒体记录。',build:()=>element('p','尚无受管媒体记录。','muted')});
  for(const source of snapshot.sources){
    const state=sourceMediaState(source),references=Math.max(0,Number(source.active_reference_count)||0);if(!sourceMediaPresent(source))sourceDeleteOpen.delete(source.id);
    sourceSpecs.push({key:'source:'+source.id,signature:[source.name,source.size,source.sha256,state,source.media_present,source.media_deleted_at,references,source.can_delete,sourceDeleteOpen.has(source.id)],build:()=>{
      const item=focusControl(focus,element('article',undefined,'item'),'source:'+source.id+':card');item.dataset.sourceId=source.id;item.tabIndex=-1;item.append(element('h4',source.name));item.append(element('p',mediaStateNames[state]||state+' · '+formatBytes(source.size),'source-meta muted'));item.append(element('p','大小：'+formatBytes(source.size)+' · SHA-256：'+source.sha256,'source-meta small muted'));
      if(source.media_deleted_at)item.append(element('p','删除时间：'+disconnectedTime(source.media_deleted_at),'source-meta small muted'));
      if(sourceMediaPresent(source)){const actions=element('div',undefined,'row source-actions'),remove=focusControl(focus,localButton('删除受管副本',()=>{pinSource(source);sourceDeleteOpen.add(source.id);requestedFocusKey='source:'+source.id+':delete-confirm';renderSources();}),'source:'+source.id+':delete');remove.className='danger-action';remove.disabled=busy||source.can_delete===false||references>0;actions.append(remove);item.append(actions);if(references>0)item.append(element('p',references+' 个未完成任务正在引用此副本；任务结束或取消后才能删除。','small muted'));else if(source.can_delete===false)item.append(element('p','当前不能删除此受管副本，请刷新后再试。','small muted'));
        if(sourceDeleteOpen.has(source.id)&&!remove.disabled){const confirm=element('div',undefined,'inline-confirm');confirm.setAttribute('role','group');confirm.setAttribute('aria-label','确认删除受管副本');confirm.append(element('p','只删除 Open-Flame 的受管副本；你最初选择的原始文件和下载成品保持不变。媒体记录和历史任务继续保留。'));
          const buttons=element('div',undefined,'row'),apply=focusControl(focus,button('确认删除受管副本',async()=>{const updated=await api('/sources/'+encodeURIComponent(source.id)+'/media',{method:'DELETE'});pinSource(updated);snapshot.sources=applyUpdates(snapshot.sources,[updated]);sourceDeleteOpen.delete(source.id);if($('source-id').value===source.id)$('source-id').value='';requestedFocusKey='source:'+source.id+':card';message('受管副本已删除；原始文件和下载成品保持不变。');}),'source:'+source.id+':delete-confirm'),dismiss=focusControl(focus,localButton('取消',()=>{sourceDeleteOpen.delete(source.id);requestedFocusKey='source:'+source.id+':delete';renderSources();}),'source:'+source.id+':delete-cancel');apply.className='danger-action';dismiss.className='secondary';buttons.append(apply,dismiss);confirm.append(buttons);item.append(confirm);}}
      else if(['missing','deleted'].includes(state)){item.append(element('p','此记录不能用于新草稿。选择与历史大小和 SHA-256 完全一致的视频，可恢复受管副本并继续处理旧任务。','small notice'));const restore=focusControl(focus,localButton('重新导入相同视频',()=>openSourceRestore(source.id)),'source:'+source.id+':restore');restore.className='secondary';item.append(restore);}else if(state==='changed')item.append(element('p','受管路径中的内容已变化，已停止使用。请先移走变化文件并刷新，待记录显示缺失后再恢复受管副本。','small notice'));else item.append(element('p','受管媒体路径状态不安全，已停止使用。请检查上传媒体目录后刷新。','small notice'));
      return item;
    }});
  }
  reconcileKeyed($('source-library'),sourceSpecs,focus);
  if(restoreSourceId){const restoring=snapshot.sources.find(item=>item.id===restoreSourceId);if(!restoring||sourceMediaPresent(restoring)){restoreSourceId=null;$('source-restore-panel').hidden=true;$('source-restore-file').value='';}else{$('source-restore-heading').textContent='恢复受管副本 · '+restoring.name;$('source-restore-description').textContent='将文件校验后恢复到现有媒体记录；需要完全匹配 '+formatBytes(restoring.size)+' 与 SHA-256 '+restoring.sha256+'。';}}
  restoreRenderedFocus(focus);
}
function showSource(){const source=snapshot.sources.find(item=>item.id===$('source-id').value);$('source-info').textContent=source?'SHA-256：'+source.sha256:'';}
function normalizedCoverList(payload){if(Array.isArray(payload))return payload;if(payload&&Array.isArray(payload.items))return payload.items;return [];}
function currentCoverSelectionIds(){const result=[];for(const id of ['bilibili-cover-id','douyin-cover-id','tencent-landscape-cover-id','tencent-portrait-cover-id']){const value=$(id).value;if(value&&!result.includes(value))result.push(value);}return result;}
async function resolveJobCovers(covers,jobs,selectedIds=[],fallbackCovers=[],freshIds=[]){const known=new Set(covers.map(item=>item.id)),fresh=new Set(freshIds),referenced=[];for(const job of jobs){const active=['draft','queued','running'].includes(job.state);for(const id of [job.cover_landscape_asset_id,job.cover_portrait_asset_id])if(id&&(!known.has(id)||active&&!fresh.has(id))&&!referenced.includes(id))referenced.push(id);}for(const id of selectedIds)if(id&&!fresh.has(id)&&!referenced.includes(id))referenced.push(id);if(!referenced.length)return {covers,failed:false};const chunks=[];for(let index=0;index<referenced.length;index+=64)chunks.push(referenced.slice(index,index+64));const results=await Promise.allSettled(chunks.map(ids=>api('/covers/resolve?ids='+encodeURIComponent(ids.join(','))))),resolved=[],failedIds=[];for(let index=0;index<results.length;index++){const result=results[index];if(result.status==='fulfilled')resolved.push(...normalizedCoverList(result.value));else failedIds.push(...chunks[index]);}covers=applyUpdates(covers,resolved);if(failedIds.length)covers=applyUpdates(covers,fallbackCovers.filter(item=>failedIds.includes(item.id)));return {covers,failed:failedIds.length>0};}
function coverPresent(cover){return (cover.media_state||'present')==='present'&&cover.media_present!==false;}
function coverDigestLabel(cover){return typeof cover.sha256==='string'&&cover.sha256?cover.sha256.slice(0,12):'未知';}
function coverLabel(cover){const dimensions=cover.width&&cover.height?' · '+cover.width+'×'+cover.height:'';return cover.name+dimensions+' · '+formatBytes(cover.size)+' · SHA-256 '+coverDigestLabel(cover);}
function coverFitsSlot(id,cover){if(!cover.width||!cover.height)return !id.startsWith('tencent-');const ratio=Number(cover.width)/Number(cover.height);if(id==='tencent-landscape-cover-id')return Math.abs(ratio-4/3)<=0.04;if(id==='tencent-portrait-cover-id')return Math.abs(ratio-3/4)<=0.04;return true;}
function renderCovers(unavailable=false){
  const usable=snapshot.covers.filter(coverPresent),focus=focusContext();
  for(const [id,emptyLabel] of [['bilibili-cover-id','使用平台默认封面'],['douyin-cover-id','使用平台默认封面'],['tencent-landscape-cover-id','不设置横版封面'],['tencent-portrait-cover-id','不设置竖版封面']]){
    const select=$(id),previous=select.value,eligible=usable.filter(cover=>coverFitsSlot(id,cover)),specs=[{key:id+':empty',signature:emptyLabel,build:()=>{const option=element('option',emptyLabel);option.value='';return option;}}];
    for(const cover of eligible)specs.push({key:id+':'+cover.id,signature:[cover.name,cover.size,cover.sha256,cover.width,cover.height],build:()=>{const option=element('option',coverLabel(cover));option.value=cover.id;return option;}});
    reconcileKeyed(select,specs,focus);select.value=eligible.some(item=>item.id===previous)?previous:'';
  }
  const specs=[];if(!snapshot.covers.length)specs.push({key:'cover:empty',signature:unavailable?'封面记录暂不可用。':'尚未导入封面。',build:()=>element('p',unavailable?'封面记录暂不可用；可稍后刷新。':'尚未导入封面。','muted')});
  for(const cover of snapshot.covers){const references=Math.max(0,Number(cover.active_reference_count)||0);if(!coverPresent(cover))coverDeleteOpen.delete(cover.id);specs.push({key:'cover:'+cover.id,signature:[cover.name,cover.size,cover.sha256,cover.mime_type,cover.width,cover.height,cover.media_state,cover.media_present,references,cover.can_delete,coverDeleteOpen.has(cover.id)],build:()=>{const item=focusControl(focus,element('article',undefined,'item cover-item'),'cover:'+cover.id+':card');item.dataset.coverId=cover.id;item.tabIndex=-1;if(coverPresent(cover)){const preview=element('img');preview.src='/api/v1/uploads/covers/'+encodeURIComponent(cover.id)+'/content';preview.alt=cover.name+' 封面预览';preview.loading='lazy';preview.decoding='async';preview.className='cover-thumbnail';item.append(preview);}item.append(element('strong',cover.name));item.append(element('p',coverLabel(cover)+' · '+(mediaStateNames[cover.media_state||'present']||cover.media_state),'small muted'));item.append(element('p','SHA-256：'+cover.sha256,'small muted'));if(coverPresent(cover)){const actions=element('div',undefined,'row source-actions'),remove=focusControl(focus,localButton('删除受管封面',()=>{coverDeleteOpen.add(cover.id);requestedFocusKey='cover:'+cover.id+':delete-confirm';renderCovers();}),'cover:'+cover.id+':delete');remove.className='danger-action';remove.disabled=busy||cover.can_delete===false||references>0;actions.append(remove);item.append(actions);if(references>0)item.append(element('p',references+' 个未完成任务正在引用此封面；任务结束或取消后才能删除。','small muted'));else if(cover.can_delete===false)item.append(element('p','当前不能删除此受管封面，请刷新后再试。','small muted'));if(coverDeleteOpen.has(cover.id)&&!remove.disabled){const confirm=element('div',undefined,'inline-confirm');confirm.setAttribute('role','group');confirm.setAttribute('aria-label','确认删除受管封面');confirm.append(element('p','只删除 Open-Flame 的受管封面副本；历史任务和封面记录继续保留。'));const buttons=element('div',undefined,'row'),apply=focusControl(focus,button('确认删除受管封面',async()=>{const updated=await api('/covers/'+encodeURIComponent(cover.id)+'/media',{method:'DELETE'});snapshot.covers=applyUpdates(snapshot.covers,[updated]);coverDeleteOpen.delete(cover.id);requestedFocusKey='cover:'+cover.id+':card';renderCovers();message('受管封面已删除；历史任务和封面记录保持不变。');}),'cover:'+cover.id+':delete-confirm'),dismiss=focusControl(focus,localButton('取消',()=>{coverDeleteOpen.delete(cover.id);requestedFocusKey='cover:'+cover.id+':delete';renderCovers();}),'cover:'+cover.id+':delete-cancel');apply.className='danger-action';dismiss.className='secondary';buttons.append(apply,dismiss);confirm.append(buttons);item.append(confirm);}}return item;}});}
  reconcileKeyed($('cover-library'),specs,focus);$('more-covers').hidden=unavailable||!coverNextCursor;$('cover-status').textContent=unavailable?'封面记录读取失败；当前保留上次成功读取的选择。':usable.length?'当前已加载可用封面：'+usable.length+' 张。':'尚无可用于新草稿的封面。';restoreRenderedFocus(focus);
}
function openSourceRestore(sourceId){const source=snapshot.sources.find(item=>item.id===sourceId);if(!source||!['missing','deleted'].includes(sourceMediaState(source)))return;pinSource(source);restoreSourceId=sourceId;$('source-restore-heading').textContent='恢复受管副本 · '+source.name;$('source-restore-description').textContent='将文件校验后恢复到现有媒体记录；需要完全匹配 '+formatBytes(source.size)+' 与 SHA-256 '+source.sha256+'。';$('source-restore-panel').hidden=false;$('source-restore-file').focus({preventScroll:true});}
function closeSourceRestore(){const sourceId=restoreSourceId;restoreSourceId=null;$('source-restore-panel').hidden=true;$('source-restore-file').value='';if(sourceId){requestedFocusKey='source:'+sourceId+':restore';renderSources();}}
function jobMediaState(job,source){if(job.source_media_state)return job.source_media_state;if(job.source_media_present===false)return 'missing';return source?sourceMediaState(source):'present';}
function jobAccountActive(job){if(job.account_lifecycle_state)return job.account_lifecycle_state==='active';const account=job.account_id?snapshot.accounts.find(item=>item.id===job.account_id):null;return !account||(account.lifecycle_state||'active')==='active';}
function jobCover(id){return id?snapshot.covers.find(item=>item.id===id):null;}
function jobCoverName(id){if(!id)return '未设置';const cover=jobCover(id);return cover?coverLabel(cover):'封面记录 '+id;}
function jobCoverReady(id){return !id||!!jobCover(id)&&coverPresent(jobCover(id));}
function jobScheduleReady(job){if(!job.publish_at_unix)return true;const configured=snapshot.status?.platforms?.find(item=>item.id===job.platform)?.schedule_min_lead_seconds,lead=Number.isFinite(Number(configured))?Number(configured):(job.platform==='bilibili'?21900:14700);return Number(job.publish_at_unix)*1000>Date.now()+lead*1000;}
function jobScheduleText(job){if(!job.publish_at_unix)return '立即执行';const offset=Number(job.publish_timezone_offset_minutes),wall=new Date((Number(job.publish_at_unix)+offset*60)*1000);return Number.isNaN(wall.getTime())||!Number.isFinite(offset)?'时间记录无效':wall.toISOString().slice(0,16).replace('T',' ')+'（UTC 偏移 '+(offset>=0?'+':'')+offset+' 分钟）';}
function jobPlatformDetails(job){const options=job.platform_options||{},lines=['发布时间：'+jobScheduleText(job)];if(job.platform==='bilibili'){lines.push('封面：'+jobCoverName(job.cover_landscape_asset_id));if(options.dynamic)lines.push('动态文案：'+options.dynamic);lines.push('禁止转载：'+(options.no_reprint?'是':'否'),'评论：'+(options.close_comments?'关闭':'开启'),'弹幕：'+(options.close_danmu?'关闭':'开启'));}else if(job.platform==='douyin'){lines.push((job.cover_portrait_asset_id?'竖版封面：':'横版封面：')+jobCoverName(job.cover_portrait_asset_id||job.cover_landscape_asset_id),'内容声明：'+(options.declaration||'未设置'));}else{lines.push('横版封面：'+jobCoverName(job.cover_landscape_asset_id),'竖版封面：'+jobCoverName(job.cover_portrait_asset_id),'短标题：'+(options.short_title||'未设置'),'内容标记：'+(options.content_label||'未设置'));}return lines.join('\n');}
function renderJobs(){
  for(const preview of $('jobs').querySelectorAll('details[data-job-id]'))jobDetailsOpen.set(preview.dataset.jobId,preview.open);
  const focus=focusContext(),jobSpecs=[];$('more-jobs').hidden=!jobNextCursor;
  if(!snapshot.jobs.length)jobSpecs.push({key:'job:empty',signature:'尚无上传任务。',build:()=>element('p','尚无上传任务。','muted')});
  for(const job of snapshot.jobs){
    const source=snapshot.sources.find(entry=>entry.id===job.source_id),mediaState=jobMediaState(job,source),mediaReady=mediaState==='present'&&job.source_media_present!==false,accountReady=jobAccountActive(job),coversReady=jobCoverReady(job.cover_landscape_asset_id)&&jobCoverReady(job.cover_portrait_asset_id),scheduleReady=jobScheduleReady(job),actionReady=mediaReady&&accountReady&&coversReady&&scheduleReady;
    const signature=[job.platform,job.account_id,job.account_name,job.account_lifecycle_state,job.source_id,job.source_name,job.source_media_state,job.source_media_present,job.title,job.description,job.tags||[],job.mode,job.category_id,job.copyright,job.source_credit,job.cover_landscape_asset_id,job.cover_portrait_asset_id,jobCover(job.cover_landscape_asset_id)?.sha256||null,jobCover(job.cover_portrait_asset_id)?.sha256||null,job.publish_at_unix,job.publish_timezone_offset_minutes,job.platform_options||{},job.state,job.code,source?.name||null,source?sourceMediaState(source):null,source?.media_present,mediaState,mediaReady,accountReady,coversReady,scheduleReady];
    jobSpecs.push({key:'job:'+job.id,signature,build:()=>{
      const item=focusControl(focus,element('article',undefined,'item'),'job:'+job.id+':card');item.dataset.jobId=job.id;item.tabIndex=-1;item.append(element('strong',platformNames[job.platform]+' · '+job.account_name+' · '+(stateNames[job.state]||job.state)));item.append(element('p',job.title));
      const publishAction=job.mode==='draft'?'保存平台草稿':job.publish_at_unix?'按指定时间发布':'立即投稿',confirmLabel=job.mode==='draft'?'确认上传并保存平台草稿':job.publish_at_unix?'确认上传并按指定时间发布':'确认立即上传投稿';
      const preview=element('details');preview.dataset.jobId=job.id;preview.open=jobDetailsOpen.has(job.id)?jobDetailsOpen.get(job.id):job.state==='draft';preview.append(focusControl(focus,element('summary','查看投稿信息'),'job:'+job.id+':summary'));preview.append(element('p','视频：'+(source?source.name:(job.source_name||'已登记视频'))+'\n媒体状态：'+(mediaStateNames[mediaState]||mediaState)+'\n简介：'+(job.description||'（空）')+'\n标签：'+(job.tags||[]).join('，')+'\n动作：'+publishAction+(job.platform==='bilibili'?'\n分区 ID：'+job.category_id+'\n类型：'+(job.copyright===1?'原创':'转载')+(job.copyright===2?'\n来源：'+job.source_credit:''):'')+'\n'+jobPlatformDetails(job),'details'));item.append(preview);if(job.code)item.append(element('p',codeText(job.code),job.state==='failed'||job.state==='unknown'?'danger':'muted'));
      if(!mediaReady)item.append(element('p','此任务的受管副本'+(mediaStateNames[mediaState]||'不可用')+'；请先恢复受管副本：在上方媒体记录中重新导入相同视频，之后再确认或重新创建本地草稿。','notice small'));if(!accountReady)item.append(element('p','此任务的账号已断开，不能再确认或重试。请连接新账号并创建新的本地草稿。','notice small'));if(!coversReady)item.append(element('p','此任务选择的封面已缺失、变化或不在当前记录中，不能确认或重试；请重新导入封面并创建新的本地草稿。','notice small'));if(!scheduleReady)item.append(element('p','此任务的定时发布时间已进入平台最小提前量，不能按原参数确认或重试；请创建新的本地草稿并选择更晚时间。','notice small'));
      const actions=element('div',undefined,'row');if(job.state==='draft'&&actionReady)actions.append(focusControl(focus,button(confirmLabel,async()=>{const updated=await api('/jobs/'+encodeURIComponent(job.id)+'/confirm',{method:'POST'});pinJob(updated);message('已确认此任务，等待上传器执行。');}),'job:'+job.id+':confirm'));
      if(['draft','queued','running'].includes(job.state))actions.append(focusControl(focus,button('取消',async()=>{const updated=await api('/jobs/'+encodeURIComponent(job.id)+'/cancel',{method:'POST'});pinJob(updated);message('已请求取消。平台已接收的内容不会被撤回。');}),'job:'+job.id+':cancel'));
      if(['failed','canceled','unknown'].includes(job.state)&&actionReady){let acknowledge=null;if(job.state==='unknown'){const label=element('label',undefined,'choice');acknowledge=focusControl(focus,element('input'),'job:'+job.id+':acknowledge');acknowledge.type='checkbox';acknowledge.checked=unknownAcknowledgements.has(job.id);acknowledge.addEventListener('change',()=>{if(acknowledge.checked)unknownAcknowledgements.add(job.id);else unknownAcknowledgements.delete(job.id);});label.append(acknowledge,element('span','我已在平台核对结果，确定需要重新上传'));item.append(label);}actions.append(focusControl(focus,button('重新创建本地草稿',async acknowledged=>{if(job.state==='unknown'&&!acknowledged)throw new Error('请先到平台核对结果，并勾选确认。');const successor=await api('/jobs/'+encodeURIComponent(job.id)+'/retry',{method:'POST',json:{acknowledge_unknown:job.state==='unknown'&&acknowledged}});jobToFocus=successor.id;message(successor.state==='draft'?'本地草稿已就绪，已定位到该任务。请核对后再确认。':'未创建新草稿：已有后继任务为“'+(stateNames[successor.state]||successor.state)+'”。已定位到该任务，请查看其当前状态。');},()=>!!acknowledge?.checked),'job:'+job.id+':retry'));}if(actions.children.length)item.append(actions);return item;
    }});
  }
  reconcileKeyed($('jobs'),jobSpecs,focus);restoreRenderedFocus(focus);
}
function renderEngineStatus(status){const backend=status.backend||{};const state=backend.ready?'ready':backend.state||backend.status||'不可用';let scheduler=status.worker_running?'运行中':'未运行';if(status.scheduler_state==='faulted')scheduler='上传调度故障';if(status.scheduler_code)scheduler+=' · '+codeText(status.scheduler_code);$('engine-status').textContent='引擎：'+(stateNames[state]||state)+(backend.code?' · '+codeText(backend.code):'')+'；上传调度：'+scheduler;$('recover-scheduler').hidden=status.scheduler_state!=='faulted';const age=Number(backend.integrity_age_seconds),ttl=Number(backend.integrity_ttl_seconds),observed=Number.isFinite(age)&&Number.isFinite(ttl)&&ttl>0;$('engine-integrity').hidden=!observed;$('engine-integrity').textContent=observed?'运行环境最近检查：'+Math.max(0,age).toFixed(1)+' 秒前'+(backend.integrity_cached?'（缓存最多 '+ttl+' 秒）':'')+'；开始账号操作或上传前会重新完整校验。':'';}
async function refresh(){if(pollPromise)return pollPromise;const selectedCoverIds=currentCoverSelectionIds(),previousCovers=snapshot.covers,initialSelectedSource=snapshot.sources.find(item=>item.id===$('source-id').value);if(initialSelectedSource)pinSource(initialSelectedSource);pollPromise=(async()=>{const volatile=[];for(const item of [...pinnedJobs.values(),...snapshot.jobs])if(['queued','running'].includes(item.state)&&!volatile.includes(item.id)&&volatile.length<64)volatile.push(item.id);const status=await api('/status');snapshot.status=status;renderEngineStatus(status);const results=await Promise.allSettled([api('/accounts'),api('/sources/page?limit=200'),api('/jobs/page?limit=200'),api('/operations'),api('/storage'),api('/covers/page?limit=200')]);let {accounts,sources,covers,jobs,operations,storage}=snapshot,failed=results.slice(0,5).some(item=>item.status==='rejected'),coversUnavailable=results[5].status==='rejected';if(results[0].status==='fulfilled')accounts=results[0].value;if(results[1].status==='fulfilled'){const sourcePage=results[1].value;for(const item of sourcePage.items)if(pinnedSources.has(item.id))pinSource(item);sources=mergeFirstPage(sourcePage.items,snapshot.sources,sourceHistoryExpanded);if(!sourceHistoryExpanded)sourceNextCursor=sourcePage.next_cursor;const selectedSourceId=$('source-id').value;if(selectedSourceId&&!sourcePage.items.some(item=>item.id===selectedSourceId)){try{const selectedSource=await api('/sources/'+encodeURIComponent(selectedSourceId));pinSource(selectedSource);}catch(error){if(error.status===404){pinnedSources.delete(selectedSourceId);sources=sources.filter(item=>item.id!==selectedSourceId);}else failed=true;}}}sources=applyUpdates(sources,[...pinnedSources.values()]);if(results[2].status==='fulfilled'){const jobPage=results[2].value,firstIds=new Set(jobPage.items.map(item=>item.id)),missing=volatile.filter(id=>!firstIds.has(id));let resolved=[];if(missing.length){try{resolved=await api('/jobs/resolve?ids='+encodeURIComponent(missing.join(',')));}catch{failed=true;}}for(const item of jobPage.items)if(pinnedJobs.has(item.id))pinJob(item);for(const item of resolved)pinJob(item);jobs=mergeFirstPage(jobPage.items,snapshot.jobs,jobHistoryExpanded||failed);if(!jobHistoryExpanded)jobNextCursor=jobPage.next_cursor;}jobs=applyUpdates(jobs,[...pinnedJobs.values()]);if(results[3].status==='fulfilled')operations=results[3].value;if(results[4].status==='fulfilled')storage=results[4].value;if(results[5].status==='fulfilled'){const coverPage=results[5].value,pageCovers=normalizedCoverList(coverPage);covers=mergeFirstPage(pageCovers,snapshot.covers,coverHistoryExpanded);if(!coverHistoryExpanded)coverNextCursor=coverPage.next_cursor;const coverResolution=await resolveJobCovers(covers,jobs,selectedCoverIds,previousCovers,pageCovers.map(item=>item.id));covers=coverResolution.covers;if(coverResolution.failed){failed=true;coversUnavailable=true;}const liveSelectedCoverIds=currentCoverSelectionIds(),knownCoverIds=new Set(covers.map(item=>item.id)),liveCoverFallbacks=applyUpdates(previousCovers,snapshot.covers).filter(item=>liveSelectedCoverIds.includes(item.id)&&!knownCoverIds.has(item.id));covers=applyUpdates(covers,liveCoverFallbacks);}sources=applyUpdates(sources,[...pinnedSources.values()]);snapshot={status,accounts,sources,covers,jobs,operations,storage};$('records-status').hidden=!failed;$('records-status').textContent=failed?'账号、视频、任务或媒体占用更新失败；当前保留上次成功读取的内容。恢复本地存储后请刷新。':'';renderAccounts();renderSources();renderCovers(coversUnavailable);renderJobs();renderStorage();renderLogin();})();try{await pollPromise;}finally{pollPromise=null;}}
function schedulePoll(){if(timer!==null)clearTimeout(timer);timer=pageVisible()?setTimeout(poll,selectedLogin()&&['queued','running'].includes(selectedLogin().state)?1500:3000):null;}
async function poll(){if(timer!==null)clearTimeout(timer);timer=null;if(!pageVisible()){pollingSuspended=true;return;}try{if(!busy)await refresh();}catch(error){message('状态刷新失败：'+error.message,true);}finally{schedulePoll();}}
function suspendPageWork(){pollingSuspended=true;inputComposing=false;schedulePoll();scheduleLoginTick();clearQR();}
function resumePageWork(){if(!pageVisible()){pollingSuspended=true;schedulePoll();scheduleLoginTick();return;}scheduleLoginTick();if(!pollingSuspended){schedulePoll();return;}pollingSuspended=false;return poll();}
$('refresh').addEventListener('click',()=>mutate(async()=>{message('状态已刷新。');}));
$('recover-scheduler').addEventListener('click',()=>mutate(async()=>{await api('/recover',{method:'POST'});message('上传调度已恢复。请核对结果不确定的任务，并重新确认等待中的草稿。');}));
$('more-sources').addEventListener('click',()=>{const cursor=sourceNextCursor;mutate(async()=>{if(!cursor)return;const page=await api('/sources/page?limit=200&cursor='+encodeURIComponent(cursor));snapshot.sources=appendPage(snapshot.sources,page.items);sourceNextCursor=page.next_cursor;sourceHistoryExpanded=true;message('已加载更多已导入视频。');});});
$('more-jobs').addEventListener('click',()=>{const cursor=jobNextCursor;mutate(async()=>{if(!cursor)return;const page=await api('/jobs/page?limit=200&cursor='+encodeURIComponent(cursor));snapshot.jobs=appendPage(snapshot.jobs,page.items);jobNextCursor=page.next_cursor;jobHistoryExpanded=true;message('已加载更早任务。');});});
$('more-covers').addEventListener('click',()=>{const cursor=coverNextCursor;mutate(async()=>{if(!cursor)return;const page=await api('/covers/page?limit=200&cursor='+encodeURIComponent(cursor));snapshot.covers=appendPage(snapshot.covers,page.items);coverNextCursor=page.next_cursor;coverHistoryExpanded=true;message('已加载更多受管封面。');});});
$('copyright').addEventListener('change',updateMetadata);$('source-id').addEventListener('change',()=>{const source=snapshot.sources.find(item=>item.id===$('source-id').value);if(source)pinSource(source);showSource();});
$('source-restore-cancel').addEventListener('click',()=>closeSourceRestore());
const platformContentIds={bilibili:['bilibili-title','bilibili-description','bilibili-tags'],douyin:['douyin-title','douyin-description','douyin-tags'],tencent:['tencent-title','tencent-description','tencent-tags']};
function copyCommonContent(platform){const ids=platformContentIds[platform];$(ids[0]).value=$('title').value;$(ids[1]).value=$('description').value;$(ids[2]).value=$('tags').value;$(platform+'-use-overrides').checked=true;clearJobValidation();$(ids[0]).focus({preventScroll:false});}
for(const platform of Object.keys(platformContentIds))$('copy-'+platform+'-common').addEventListener('click',()=>copyCommonContent(platform));
for(const id of ['account-name','title','description','tags','source-credit','bilibili-title','bilibili-description','bilibili-tags','bilibili-dynamic','douyin-title','douyin-description','douyin-tags','tencent-title','tencent-description','tencent-tags','tencent-short-title']){const input=$(id);input.addEventListener('compositionstart',()=>{inputComposing=true;});input.addEventListener('compositionend',()=>{inputComposing=false;});input.addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.isComposing||event.keyCode===229||inputComposing))event.preventDefault();});}
function beginSubmit(event){event.preventDefault();return !(event.isComposing||inputComposing);}
const jobValidationControls=['account-selection','source-id','title','tags','category-id','copyright','source-credit','bilibili-title','bilibili-tags','bilibili-publish-at','douyin-title','douyin-tags','douyin-publish-at','tencent-title','tencent-tags','tencent-short-title','tencent-publish-at'];
function clearJobValidation(){const error=$('job-form-error');if(error.textContent)error.textContent='';error.hidden=true;for(const id of jobValidationControls)$(id).removeAttribute('aria-invalid');}
function rejectJobDraft(controlId,text){clearJobValidation();const control=$(controlId);control.setAttribute('aria-invalid','true');$('job-form-error').textContent=text;$('job-form-error').hidden=false;message(text,true);control.focus({preventScroll:false});return false;}
$('source-restore-form').addEventListener('submit',event=>{if(!beginSubmit(event))return;const sourceId=restoreSourceId,file=$('source-restore-file').files[0];mutate(async()=>{const source=snapshot.sources.find(item=>item.id===sourceId);if(!source)throw new Error('媒体记录已变化，请刷新后重试。');if(!file)throw new Error('请选择与历史记录完全相同的视频。');if(file.size!==Number(source.size))throw new Error('所选视频大小与历史记录不一致，受管副本没有恢复。');message('正在校验文件大小与 SHA-256 并恢复受管副本…');const updated=await api('/sources/'+encodeURIComponent(source.id)+'/media',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});pinSource(updated);snapshot.sources=applyUpdates(snapshot.sources,[updated]);restoreSourceId=null;$('source-restore-panel').hidden=true;if($('source-restore-file').files[0]===file)$('source-restore-file').value='';renderSources();$('source-id').value=updated.id;showSource();message('受管副本已恢复；旧任务不会自动执行，仍需手动确认或重试／重建。');});});
$('account-form').addEventListener('submit',event=>{if(!beginSubmit(event))return;const platform=$('account-platform').value,enteredName=$('account-name').value;let name=enteredName.trim();if(!name){let count=1;do{name=platformNames[platform]+' 账号 '+count++;}while(snapshot.accounts.some(item=>item.platform===platform&&item.name===name));}mutate(async()=>{const account=await api('/accounts',{method:'POST',json:{platform,name}});if($('account-name').value===enteredName)$('account-name').value='';await startLogin(account.id);});});
$('source-form').addEventListener('submit',event=>{if(!beginSubmit(event))return;const file=$('source-file').files[0];mutate(async()=>{if(!file)throw new Error('请选择视频。');if(file.size>2147483648)throw new Error('视频超过 2 GiB。');message('正在导入视频，请等待文件校验完成…');const source=await api('/sources?name='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});await refresh();pinSource(source);snapshot.sources=applyUpdates(snapshot.sources,[source]);renderSources();$('source-id').value=source.id;showSource();message('视频已导入。');});});
$('cover-form').addEventListener('submit',event=>{if(!beginSubmit(event))return;const file=$('cover-file').files[0];mutate(async()=>{if(!file)throw new Error('请选择封面。');if(file.size>20971520)throw new Error('封面超过 20 MiB。');message('正在导入并校验封面…');const cover=await api('/covers?name='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});snapshot.covers=applyUpdates(snapshot.covers,[cover]);renderCovers();if($('cover-file').files[0]===file)$('cover-file').value='';message('封面已导入，可在各平台投稿设置中选择。');});});
$('job-form').addEventListener('input',clearJobValidation);$('job-form').addEventListener('change',clearJobValidation);
function splitTags(value){return value.split(/[,，]/).map(item=>item.trim()).filter(Boolean);}
function textLength(value){return [...value].length;}
function localDateTimeKey(value){const pad=number=>String(number).padStart(2,'0');return value.getFullYear()+'-'+pad(value.getMonth()+1)+'-'+pad(value.getDate())+'T'+pad(value.getHours())+':'+pad(value.getMinutes());}
function invalidTags(tags){return tags.length>10||new Set(tags).size!==tags.length||tags.some(tag=>!tag||textLength(tag)>20||/[#＃\n\r\t]/.test(tag));}
function finalPlatformContent(platform,common){const use=$(platform+'-use-overrides').checked,ids=platformContentIds[platform];return use?{title:$(ids[0]).value.trim(),description:$(ids[1]).value,tags:splitTags($(ids[2]).value),titleControl:ids[0]}:{...common,titleControl:'title'};}
function scheduleValue(platform){const controlId=platform+'-publish-at',raw=$(controlId).value;if(!raw)return {publish_at_unix:null,publish_timezone_offset_minutes:null};const parsed=new Date(raw),key=raw.slice(0,16);if(Number.isNaN(parsed.getTime())||localDateTimeKey(parsed)!==key)return {error:'定时发布时间无效，可能落在本地夏令时跳时区间。',controlId};for(let minutes=-180;minutes<=180;minutes+=30){if(!minutes)continue;const alternate=new Date(parsed.getTime()+minutes*60000);if(localDateTimeKey(alternate)===key&&alternate.getTimezoneOffset()!==parsed.getTimezoneOffset())return {error:'该本地时间在夏令时切换时出现两次，请选择其他时间。',controlId};}const leadMilliseconds=platform==='bilibili'?21900000:14700000,leadText=platform==='bilibili'?'6 小时 5 分钟':'4 小时 5 分钟';if(parsed.getTime()<=Date.now()+leadMilliseconds)return {error:platformNames[platform]+' 定时发布至少需要提前 '+leadText+'。',controlId};if(platform==='tencent'&&parsed.getMinutes()!==0)return {error:'视频号定时发布只支持整点。',controlId};if(platform==='tencent'&&parsed.getTime()>Date.now()+28*24*3600000)return {error:'视频号定时发布最多可提前 28 天。',controlId};return {publish_at_unix:Math.floor(parsed.getTime()/60000)*60,publish_timezone_offset_minutes:-parsed.getTimezoneOffset()};}
function platformCoverValues(platform){if(platform==='bilibili')return {cover_landscape_asset_id:$('bilibili-cover-id').value||null,cover_portrait_asset_id:null};if(platform==='douyin'){const id=$('douyin-cover-id').value||null,cover=id?snapshot.covers.find(item=>item.id===id):null,portrait=!!cover&&Number(cover.height)>Number(cover.width);return {cover_landscape_asset_id:id&&!portrait?id:null,cover_portrait_asset_id:id&&portrait?id:null};}return {cover_landscape_asset_id:$('tencent-landscape-cover-id').value||null,cover_portrait_asset_id:$('tencent-portrait-cover-id').value||null};}
$('job-form').addEventListener('submit',event=>{
  if(!beginSubmit(event))return;clearJobValidation();
  const accountIds=selectedAccounts(),selected=snapshot.accounts.filter(item=>accountIds.includes(item.id)).map(item=>({id:item.id,platform:item.platform})),sourceId=$('source-id').value,title=$('title').value.trim(),description=$('description').value,tags=splitTags($('tags').value),categoryId=$('category-id').value,copyright=$('copyright').value,sourceCredit=$('source-credit').value.trim(),tencentMode=$('tencent-mode').value,bili=selected.some(item=>item.platform==='bilibili');
  if(!accountIds.length)return rejectJobDraft('account-selection','请选择至少一个账号。');if(!sourceId)return rejectJobDraft('source-id','请先导入并选择视频。');if(!title)return rejectJobDraft('title','请填写标题。');if(bili&&!categoryId)return rejectJobDraft('category-id','请填写 Bilibili 分区 ID。');if(bili&&!copyright)return rejectJobDraft('copyright','请选择 Bilibili 原创或转载。');if(bili&&copyright==='2'&&!sourceCredit)return rejectJobDraft('source-credit','转载必须填写来源。');
  const common={title,description,tags},contentByPlatform={},scheduleByPlatform={};
  for(const platform of new Set(selected.map(item=>item.platform))){const content=finalPlatformContent(platform,common),limit={bilibili:80,douyin:30,tencent:100}[platform],tagControl=$(platform+'-use-overrides').checked?platformContentIds[platform][2]:'tags';if(!content.title)return rejectJobDraft(content.titleControl,platformNames[platform]+' 标题不能为空。');if(textLength(content.title)>limit)return rejectJobDraft(content.titleControl,platformNames[platform]+' 标题不能超过 '+limit+' 字。');if(platform==='bilibili'&&!content.tags.length)return rejectJobDraft(tagControl,'Bilibili 投稿至少需要一个标签。');if(invalidTags(content.tags))return rejectJobDraft(tagControl,'每个平台最多 10 个不重复标签，每个最多 20 字，标签中不要填写 #。');contentByPlatform[platform]=content;const schedule=scheduleValue(platform);if(schedule.error)return rejectJobDraft(schedule.controlId,schedule.error);scheduleByPlatform[platform]=schedule;}
  const shortTitle=$('tencent-short-title').value.trim();if(selected.some(item=>item.platform==='tencent')&&shortTitle&&textLength(shortTitle)<7)return rejectJobDraft('tencent-short-title','视频号短标题需为 7–15 字。');if(selected.some(item=>item.platform==='tencent')&&tencentMode==='draft'&&scheduleByPlatform.tencent?.publish_at_unix)return rejectJobDraft('tencent-publish-at','视频号保存平台草稿时不能设置定时发布。');
  const biliSourceCredit=copyright==='2'?sourceCredit:'';
  const targetOverrides=selected.map(account=>{const platform=account.platform,content=contentByPlatform[platform],schedule=scheduleByPlatform[platform],mode=platform==='tencent'?tencentMode:'publish';let platformOptions;if(platform==='bilibili')platformOptions={dynamic:$('bilibili-dynamic').value.trim(),no_reprint:$('bilibili-no-reprint').checked,close_comments:$('bilibili-close-comments').checked,close_danmu:$('bilibili-close-danmu').checked};else if(platform==='douyin')platformOptions={declaration:$('douyin-declaration').value||null};else platformOptions={short_title:shortTitle||null,content_label:$('tencent-content-label').value||null};const target={account_id:account.id,title:content.title,description:content.description,tags:content.tags,mode,...platformCoverValues(platform),...schedule,platform_options:platformOptions};if(platform==='bilibili')Object.assign(target,{category_id:Number(categoryId),copyright:Number(copyright),source_credit:biliSourceCredit});return target;});
  const base={source_id:sourceId,title,description,tags,category_id:bili?Number(categoryId):null,copyright:bili?Number(copyright):null,source_credit:bili?biliSourceCredit:''},signature=JSON.stringify({...base,target_overrides:targetOverrides});if(!draftSubmission||draftSubmission.signature!==signature)draftSubmission={signature,key:crypto.randomUUID()};
  mutate(async()=>{const created=await api('/jobs',{method:'POST',json:{...base,account_ids:accountIds,mode:'publish',target_overrides:targetOverrides,idempotency_key:draftSubmission.key}});if(Array.isArray(created)&&created.length){for(const item of created)pinJob(item);snapshot.jobs=applyUpdates(snapshot.jobs,created);}draftSubmission=null;message('本地草稿已创建。请在下方核对每份平台内容和参数，再确认执行。');});
});
const assetId=new URL(location.href).searchParams.get('asset_id');if(assetId&&/^[0-9a-f-]{36}$/.test(assetId)){$('asset-import').hidden=false;$('import-asset').addEventListener('click',()=>mutate(async()=>{const source=await api('/sources/assets/'+encodeURIComponent(assetId),{method:'POST'});await refresh();pinSource(source);snapshot.sources=applyUpdates(snapshot.sources,[source]);renderSources();$('source-id').value=source.id;showSource();$('asset-import').hidden=true;message('下载成品已导入，原下载文件保持不变。');}));}
const editOutputId=new URL(location.href).searchParams.get('edit_output_id');if(editOutputId&&/^[0-9a-f]{32}$/.test(editOutputId)){$('edit-output-import').hidden=false;$('import-edit-output').addEventListener('click',()=>mutate(async()=>{const source=await api('/sources/edits/'+encodeURIComponent(editOutputId),{method:'POST'});await refresh();pinSource(source);snapshot.sources=applyUpdates(snapshot.sources,[source]);renderSources();$('source-id').value=source.id;showSource();$('edit-output-import').hidden=true;message('编辑成品已导入；编辑文件与下载原件均保持不变。');}));}
window.addEventListener('pagehide',()=>{pageActive=false;suspendPageWork();});
window.addEventListener('pageshow',event=>{if(!event.persisted)return;pageActive=true;return resumePageWork();});
document.addEventListener('visibilitychange',()=>{if(document.hidden){suspendPageWork();return;}return resumePageWork();});
(async()=>{try{csrf=(await api('/session')).csrf_token;await refresh();message('选择账号和视频，创建本地草稿后确认执行。');}catch(error){message('上传器连接失败：'+error.message,true);}finally{schedulePoll();}})();
</script></body></html>'''
