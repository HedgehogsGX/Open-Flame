from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="zh-CN" class="no-js" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Open-Flame · 下载</title>
  <link rel="stylesheet" href="/assets/open-flame.css">
  <script src="/assets/open-flame-shell.js" defer></script>
</head>
<body class="download-page">
  <header class="topbar-shell">
    <div class="topbar">
      <a class="brand" href="/" aria-label="Open-Flame 下载首页">
        <span class="brand-mark" aria-hidden="true">OF</span><span>Open-Flame</span>
      </a>
      <nav class="primary-nav" aria-label="主要功能">
        <a class="nav-link" href="/" aria-current="page">下载</a>
        <a class="nav-link" href="/edits">编辑</a>
        <a class="nav-link" href="/uploads">上传</a>
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
      <h1>下载并整理视频</h1>
      <p class="lede">创建批次、跟踪真实处理阶段，并从同一处取得原件、缩略图和字幕。上传工作台继续使用独立账号与逐项确认。</p>
      <p class="notice page-note">普通 Windows 应用同时管理控制面与本机 Worker，本次运行状态由应用心跳报告。页面不会启动进程；单独启动控制面时，外部 Worker 状态保持未知。</p>
    </header>

    <section class="card" aria-labelledby="automation-entry-heading">
      <div class="card-header"><div><p class="section-index">自动流程</p><h2 id="automation-entry-heading">从一个网址完成翻译、配音与投稿</h2><p class="muted">使用独立、可恢复的流程记录串联下载、AI 时间轴、封面、编辑成品和三平台投稿参数。</p></div><a class="button-link" href="/workflows">打开自动流程</a></div>
    </section>

    <section class="status-strip" aria-label="当前运行摘要">
      <div class="status-tile">
        <p class="status-label">本次 Worker</p>
        <p id="worker-runtime-status" class="status-value" role="status" aria-live="polite">正在读取本次 Worker 状态…</p>
        <p id="worker-runtime-detail" class="muted small"></p>
      </div>
      <div class="status-tile">
        <p class="status-label">下载队列</p>
        <p id="queue-status" class="status-value">正在读取队列状态…</p>
        <button id="resume-queue" class="secondary" type="button" hidden>确认磁盘恢复并继续队列</button>
      </div>
      <div class="status-tile">
        <p class="status-label">平台状态</p>
        <div id="circuit-status" class="status-value muted">正在读取平台状态…</div>
      </div>
    </section>

    <section class="card primary-card" aria-labelledby="create-batch-heading">
      <div class="card-header">
        <div><p class="section-index">新建</p><h2 id="create-batch-heading">创建下载批次</h2><p class="muted">粘贴分享文本或导入 UTF-8 文件；提交前会先规范化并校验每条输入。</p></div>
      </div>
      <form id="batch-form">
        <label for="name">批次名称（可选）</label>
        <input id="name" maxlength="200" placeholder="例如：9 月素材">
        <label for="inputs">URL 或包含 URL 的分享文本（每行一条）</label>
        <textarea id="inputs" aria-describedby="inputs-help batch-error" placeholder="https://www.bilibili.com/video/BV...&#10;https://www.douyin.com/video/...&#10;https://www.tiktok.com/@user/video/...&#10;https://www.instagram.com/reel/..."></textarea>
        <p id="inputs-help" class="muted small">每行可包含一个链接或一段分享文本。文本输入与文件导入只能选择一种。</p>
        <label for="import-file">或导入 UTF-8 TXT/CSV（最多 256 KiB）</label>
        <input id="import-file" type="file" accept=".txt,.csv,text/plain,text/csv" aria-describedby="inputs-help batch-error">
        <label for="credential-mode">本次新任务与手动重试的 Cookie 模式</label>
        <select id="credential-mode" aria-describedby="credential-status credential-help">
          <option value="use_default">使用本次启动配置的默认 Cookie（未配置的平台匿名）</option>
          <option value="anonymous">匿名下载（不使用 Cookie）</option>
        </select>
        <p id="credential-status" class="muted" aria-live="polite">正在读取默认 Cookie 状态…</p>
        <button id="refresh-credentials" class="secondary" type="button">刷新 Cookie 状态</button>
        <p id="credential-help" class="muted">仅通过本地配置启用平台默认 Cookie，不在页面上传或展示 Cookie。选择只影响实际新建任务和点击重试的任务；去重不会改写已存在任务。</p>
        <p id="batch-error" class="danger" role="alert" hidden></p>
        <button id="submit" type="submit">创建批次</button>
      </form>
    </section>

    <section id="result" class="card" aria-labelledby="current-batch-heading" hidden>
      <div class="card-header"><div><p class="section-index">当前</p><h2 id="current-batch-heading">当前批次</h2><p class="muted">阶段百分比是保守估算；只有验证并发布完成的文件会出现在成品区。</p></div></div>
      <p id="current-batch-error" class="danger" role="alert" hidden></p>
      <section id="job-progress" hidden aria-live="polite">
        <h3>任务进度（阶段估算）</h3>
        <div id="job-progress-list" class="job-progress-list"></div>
      </section>
      <div id="job-actions" class="row"></div>
      <section id="asset-links" hidden>
        <div class="row row-spread">
          <h3>可下载成品</h3>
          <button id="refresh-assets" class="secondary" type="button">刷新成品</button>
        </div>
        <ul id="asset-list" class="asset-list"></ul>
      </section>
      <details><summary>查看完整任务数据</summary><pre id="output"></pre></details>
    </section>

    <section class="card" aria-labelledby="recent-batches-heading">
      <div class="card-header">
        <div><p class="section-index">历史</p><h2 id="recent-batches-heading">最近批次</h2><p class="muted">重新打开批次后可继续轮询，并访问已完成的成品。</p></div>
        <button id="refresh-batches" class="secondary" type="button">刷新批次</button>
      </div>
      <ul id="recent-batches" class="batch-list" aria-live="polite"></ul>
    </section>

    <details class="card operations-card">
      <summary>运行详情与诊断</summary>
      <div class="operations-content diagnostic-grid">
        <section class="diagnostic-panel" aria-labelledby="capabilities-heading">
          <div class="card-header"><div><h2 id="capabilities-heading">下载能力</h2><p class="muted small">实现、构建/环境证据与人工决定分别展示；导入证据不会自动开启能力。</p></div><button id="refresh-capabilities" class="secondary" type="button">刷新能力</button></div>
          <div id="capability-status" class="muted">正在读取平台能力矩阵…</div>
        </section>
        <section class="diagnostic-panel" aria-labelledby="toolchain-heading">
          <div class="card-header"><div><h2 id="toolchain-heading">本机工具链</h2><p class="muted small">显示控制端启动时缓存的 yt-dlp、FFmpeg 与 ffprobe 检查。</p></div><button id="refresh-tools" class="secondary" type="button">刷新显示</button></div>
          <p id="tool-status" class="muted">正在读取启动检查结果…</p>
          <pre id="tool-output" class="log-output" aria-live="polite">尚无工具链检查结果。</pre>
          <p id="tool-security-note" class="notice small">工具链检查与本次运行状态分别展示；外部 Worker 状态保持未知，完整平台能力仍未验证。</p>
        </section>
        <section class="diagnostic-panel wide" aria-labelledby="runtime-logs-heading">
          <div class="card-header"><div><h2 id="runtime-logs-heading">运行日志</h2><p class="muted small">显示脱敏后的本机排障事件；日志是辅助线索，不替代数据库、资产 manifest 或验收证据。</p></div><button id="refresh-logs" class="secondary" type="button">手动刷新</button></div>
          <p id="log-status" class="muted">正在读取运行日志状态…</p>
          <pre id="log-output" class="log-output" aria-live="polite">正在读取最近事件…</pre>
        </section>
      </div>
    </details>
    <p class="page-footer">Open-Flame 0.28.0 · 本地优先 · 下载、编辑与上传数据相互隔离</p>
  </main>
  <script>
    const form = document.querySelector('#batch-form');
    const button = document.querySelector('#submit');
    const result = document.querySelector('#result');
    const output = document.querySelector('#output');
    const currentBatchError = document.querySelector('#current-batch-error');
    const jobProgress = document.querySelector('#job-progress');
    const jobProgressList = document.querySelector('#job-progress-list');
    const jobActions = document.querySelector('#job-actions');
    const assetLinks = document.querySelector('#asset-links');
    const assetList = document.querySelector('#asset-list');
    const refreshAssets = document.querySelector('#refresh-assets');
    const queueStatus = document.querySelector('#queue-status');
    const workerRuntimeStatus = document.querySelector('#worker-runtime-status');
    const workerRuntimeDetail = document.querySelector('#worker-runtime-detail');
    const resumeQueue = document.querySelector('#resume-queue');
    const circuitStatus = document.querySelector('#circuit-status');
    const capabilityStatus = document.querySelector('#capability-status');
    const refreshCapabilities = document.querySelector('#refresh-capabilities');
    const refreshTools = document.querySelector('#refresh-tools');
    const toolStatus = document.querySelector('#tool-status');
    const toolOutput = document.querySelector('#tool-output');
    const toolSecurityNote = document.querySelector('#tool-security-note');
    const refreshLogs = document.querySelector('#refresh-logs');
    const logStatus = document.querySelector('#log-status');
    const logOutput = document.querySelector('#log-output');
    const refreshBatches = document.querySelector('#refresh-batches');
    const recentBatches = document.querySelector('#recent-batches');
    const credentialMode = document.querySelector('#credential-mode');
    const credentialStatus = document.querySelector('#credential-status');
    const refreshCredentials = document.querySelector('#refresh-credentials');
    const nameInput = document.querySelector('#name');
    const inputsField = document.querySelector('#inputs');
    const importFile = document.querySelector('#import-file');
    const batchError = document.querySelector('#batch-error');
    let credentialRequestId = 0;
    let pollGeneration = 0;
    let pollRequestId = 0;
    let assetRequestId = 0;
    let assetRequestInFlight = null;
    let toolRequestId = 0;
    let logRequestId = 0;
    let batchListRequestId = 0;
    let capabilityRequestId = 0;
    let queueRequestId = 0;
    let circuitRequestId = 0;
    let currentBatchPayload = null;
    let pollTimer = null;
    let operationsTimer = null;
    let runtimeTimer = null;
    let runtimeExpiryTimer = null;
    let runtimeRequestId = 0;
    let runtimeStopped = false;
    let pageActive = true;
    let inputComposing = false;
    const jobActionInFlight = new Set();
    const activeStatuses = new Set(['queued', 'probing', 'downloading', 'postprocessing', 'verifying']);

    async function fetchJson(url, options = {}, action = '请求') {
      const response = await fetch(url, options);
      let payload;
      try {
        payload = await response.json();
      } catch (_) {
        throw new Error(`${action}失败（HTTP ${response.status}）：响应不是有效 JSON`);
      }
      if (!response.ok) {
        const detail = typeof payload?.detail === 'string'
          ? payload.detail
          : JSON.stringify(payload?.detail ?? payload);
        throw new Error(`${action}失败（HTTP ${response.status}）：${detail}`);
      }
      return payload;
    }

    function reconcileKeyed(parent, entries, keyFor, createNode, updateNode) {
      const existing = new Map(
        Array.from(parent.children).map(node => [node._openFlameKey, node])
      );
      const ordered = [];
      for (const entry of entries) {
        const key = String(keyFor(entry));
        const node = existing.get(key) || createNode(entry, key);
        node._openFlameKey = key;
        updateNode(node, entry, key);
        ordered.push(node);
        existing.delete(key);
      }
      for (const [index, node] of ordered.entries()) {
        const current = parent.children[index] || null;
        if (current !== node) parent.insertBefore(node, current);
      }
      for (const node of existing.values()) node.remove();
    }

    function setListMessage(parent, key, message, className = '') {
      reconcileKeyed(
        parent,
        [{key, message, className}],
        entry => entry.key,
        () => document.createElement('li'),
        (item, entry) => {
          item.className = entry.className;
          if (item.textContent !== entry.message) item.textContent = entry.message;
        }
      );
    }

    function clearCurrentBatchError() {
      currentBatchError.hidden = true;
      currentBatchError.textContent = '';
    }

    function showCurrentBatchError(message) {
      if (currentBatchError.textContent !== message) {
        currentBatchError.textContent = message;
      }
      currentBatchError.hidden = false;
    }

    function clearBatchError() {
      batchError.hidden = true;
      batchError.textContent = '';
      inputsField.removeAttribute?.('aria-invalid');
      importFile.removeAttribute?.('aria-invalid');
    }

    function showBatchError(message, target = null) {
      batchError.textContent = message;
      batchError.hidden = false;
      if (target) {
        target.setAttribute('aria-invalid', 'true');
        target.focus?.();
      }
    }

    function renderWorkerRuntime(payload) {
      if (runtimeExpiryTimer !== null) clearTimeout(runtimeExpiryTimer);
      if (payload.mode === 'managed_direct' && ['online', 'paused'].includes(payload.state)
          && Number(payload.heartbeat_age_seconds) >= Number(payload.heartbeat_timeout_seconds)) {
        payload = {...payload, state: 'stale', network_download_enabled: null};
      }
      const labels = {starting: '正在启动', online: '运行中', paused: '运行中 · 队列已暂停',
        stopping: '正在停止', stopped: '已停止', check_only: '仅检查模式', stale: '心跳已过期', unknown: '状态未知'};
      const managed = payload.mode === 'managed_direct';
      const state = labels[payload.state] || labels.unknown;
      const text = `本次 Worker：${state}${managed ? '（本机托管直连）' : '（外部进程未观测）'}`;
      if (workerRuntimeStatus.textContent !== text) workerRuntimeStatus.textContent = text;
      workerRuntimeStatus.className = ['stale', 'unknown', 'stopped'].includes(payload.state)
        ? 'status-value muted'
        : 'status-value';
      workerRuntimeDetail.textContent = payload.network_download_enabled === true
        ? '本机网络下载已启用；这不代表平台验证或投稿审核通过。'
        : (payload.state === 'check_only' ? '本次只验证启动，不领取下载任务。'
          : '当前无法确认可执行网络下载；工具链检查与进程运行状态分别展示。');
      if (managed && ['online', 'paused'].includes(payload.state)) {
        const validFor = Math.max(0, Number(payload.heartbeat_timeout_seconds) - Number(payload.heartbeat_age_seconds));
        if (Number.isFinite(validFor) && pageActive && document.hidden !== true) runtimeExpiryTimer = setTimeout(() => {
          renderWorkerRuntime({mode: 'managed_direct', state: 'stale', network_download_enabled: null});
        }, Math.min(validFor, 3) * 1000);
      }
    }

    async function loadWorkerRuntime() {
      if (runtimeStopped || !pageActive || document.hidden === true) return;
      const requestId = ++runtimeRequestId;
      const clock = () => globalThis.performance?.now?.() ?? Date.now();
      const started = clock();
      try {
        const payload = await fetchJson('/api/v1/operations/runtime', {}, '读取本次 Worker 状态');
        if (runtimeStopped || requestId !== runtimeRequestId) return;
        if (typeof payload.heartbeat_age_seconds === 'number') payload.heartbeat_age_seconds += Math.max(0, clock() - started) / 1000;
        renderWorkerRuntime(payload);
      } catch (_) {
        if (!runtimeStopped && requestId === runtimeRequestId) renderWorkerRuntime({mode: 'external_unknown', state: 'unknown'});
      } finally {
        if (!runtimeStopped && requestId === runtimeRequestId) {
          if (runtimeTimer !== null) clearTimeout(runtimeTimer);
          runtimeTimer = setTimeout(loadWorkerRuntime, 1500);
        }
      }
    }

    globalThis.addEventListener?.('pagehide', () => suspendPageWork(true));
    globalThis.addEventListener?.('pageshow', event => {
      if (event.persisted) resumePageWork();
    });
    document.addEventListener?.('visibilitychange', () => {
      if (document.hidden === true) suspendPageWork(false);
      else resumePageWork();
    });

    async function loadQueueState() {
      const requestId = ++queueRequestId;
      try {
        const queue = await fetchJson('/api/v1/operations/queue', {}, '读取队列状态');
        if (requestId !== queueRequestId) return;
        queueStatus.textContent = queue.paused
          ? `队列已暂停：${queue.reason || 'unknown'}`
          : '队列未暂停；可接收新任务（不代表 Worker 已启动）';
        queueStatus.className = queue.paused ? 'status-value danger' : 'status-value';
        resumeQueue.hidden = !queue.paused;
      } catch (error) {
        if (requestId !== queueRequestId) return;
        queueStatus.textContent = `队列状态读取失败：${error}`;
        queueStatus.className = 'status-value danger';
        resumeQueue.hidden = true;
      }
    }

    async function loadCircuitState() {
      const requestId = ++circuitRequestId;
      try {
        const circuits = await fetchJson('/api/v1/platform-circuits', {}, '读取平台状态');
        if (requestId !== circuitRequestId) return;
        circuitStatus.replaceChildren();
        circuitStatus.className = 'status-value muted';
        if (!circuits.length) {
          circuitStatus.textContent = '尚无平台熔断记录。';
          return;
        }
        for (const circuit of circuits) {
          const line = document.createElement('p');
          line.textContent = `${circuit.platform}: ${circuit.state}`
            + ` · 连续失败 ${circuit.consecutive_failures}`
            + (circuit.last_error_code ? ` (${circuit.last_error_code})` : '');
          if (circuit.state === 'open' && circuit.requires_manual_reset) {
            line.className = 'danger';
            line.append(' · 已锁定，需人工复位');
            const reset = document.createElement('button');
            reset.type = 'button';
            reset.textContent = `人工复位 ${circuit.platform}`;
            reset.addEventListener('click', async () => {
              reset.disabled = true;
              try {
                await fetchJson(
                  `/api/v1/platform-circuits/${encodeURIComponent(circuit.platform)}/reset`,
                  { method: 'POST' },
                  `复位 ${circuit.platform}`
                );
                await loadCircuitState();
              } catch (error) {
                line.append(` 复位失败：${error}`);
              } finally {
                if (reset.isConnected) reset.disabled = false;
              }
            });
            line.append(' ', reset);
          } else if (circuit.state === 'open') {
            line.className = 'danger';
            line.append(` · 自动冷却至 ${circuit.cooldown_until || '未知时间'}`);
          } else if (circuit.state === 'half_open') {
            line.append(' · 正在进行一次受控探测');
          }
          circuitStatus.append(line);
        }
      } catch (error) {
        if (requestId !== circuitRequestId) return;
        circuitStatus.textContent = `平台状态读取失败：${error}`;
        circuitStatus.className = 'status-value danger';
      }
    }

    async function loadCapabilityState() {
      const requestId = ++capabilityRequestId;
      refreshCapabilities.disabled = true;
      try {
        const snapshot = await fetchJson(
          '/api/v1/capability-snapshot',
          {},
          '读取能力快照'
        );
        if (requestId !== capabilityRequestId) return;
        const { implementations, evidence, decisions } = snapshot;
        capabilityStatus.replaceChildren();
        capabilityStatus.className = 'muted';
        const implementationLabels = {
          disabled: '未启用',
          candidate: '已实现（静态候选）',
          verified: '已实现'
        };
        const assessmentLabels = {
          qualified: '达到 Stage 0 阈值',
          insufficient: '证据不足'
        };
        const decisionLabels = {
          approved: '已人工批准',
          revoked: '已撤销'
        };
        const shortLinkLabels = {
          not_applicable: '短链不适用',
          supported: '短链可直接使用',
          gated: '短链需单独启用解析服务',
          deferred: '短链尚未接入'
        };
        if (snapshot.evidence_truncated || snapshot.decision_truncated) {
          const bounded = document.createElement('p');
          bounded.textContent = `当前显示为有界快照：证据 ${evidence.length}/${snapshot.evidence_total}`
            + `，当前决定 ${decisions.length}/${snapshot.decision_total}。`;
          capabilityStatus.append(bounded);
        }
        for (const capability of implementations) {
          const line = document.createElement('p');
          line.textContent = `${capability.platform} / ${capability.source_type} / ${capability.job_kind}: `
            + `${implementationLabels[capability.implementation_status] || capability.implementation_status}`
            + `；${capability.authentication}`
            + `；${shortLinkLabels[capability.short_link_status] || capability.short_link_status}`;
          capabilityStatus.append(line);
          const matchingEvidence = evidence.filter(item =>
            item.implementation_id === capability.implementation_id
          );
          if (!matchingEvidence.length) {
            const empty = document.createElement('p');
            empty.textContent = snapshot.evidence_truncated
              ? '  证据：当前有界窗口未显示；决定：未知。'
              : '  证据：尚未导入；决定：待审批。';
            capabilityStatus.append(empty);
            continue;
          }
          for (const item of matchingEvidence) {
            const identityDecision = decisions.find(current =>
              current.identity_key === item.identity_key
            );
            const decision = identityDecision?.evidence_id === item.evidence_id
              ? identityDecision
              : null;
            const decisionLabel = decision
              ? (decisionLabels[decision.state] || decision.state)
              : identityDecision
                ? '未用于当前决定'
                : snapshot.decision_truncated
                  ? '当前决定未知（窗口外）'
                  : '待审批';
            const buildLabel = item.product_version === snapshot.current_product_identity
              ? '当前构建'
              : '历史构建（不适用于当前运行）';
            const detail = document.createElement('p');
            detail.textContent = `  证据 ${item.evidence_id}: `
              + `${assessmentLabels[item.assessment] || item.assessment}`
              + `；产品 ${item.product_version}；downloader ${item.downloader_version}`
              + `；${buildLabel}`
              + `；环境 ${item.environment}；正/负样本 ${item.positive_samples}/${item.negative_samples}`
              + `；完整轮次 ${item.complete_runs}`
              + `；决定 ${decisionLabel}`
              + `${decision ? `（revision ${decision.revision}，${decision.reason_code}）` : ''}`;
            capabilityStatus.append(detail);
          }
        }
      } catch (error) {
        if (requestId !== capabilityRequestId) return;
        capabilityStatus.textContent = `平台能力读取失败：${error}`;
        capabilityStatus.className = 'danger';
      } finally {
        if (requestId === capabilityRequestId) refreshCapabilities.disabled = false;
      }
    }

    async function loadOperations() {
      await Promise.all([
        loadQueueState(),
        loadCircuitState()
      ]);
    }

    async function refreshOperations() {
      if (!pageActive || document.hidden === true) return;
      await loadOperations();
      if (operationsTimer !== null) clearTimeout(operationsTimer);
      if (pageActive && document.hidden !== true) {
        operationsTimer = setTimeout(refreshOperations, 10000);
      }
    }

    function renderToolchain(payload) {
      const labels = { unconfigured: '未配置', invalid: '无效', ready: '离线检查通过' };
      const state = labels[payload.state] || '未知';
      toolStatus.textContent = `工具链状态：${state}`
        + (payload.detail_code ? `（${payload.detail_code}）` : '');
      toolStatus.className = payload.state === 'ready' ? 'muted' : 'danger';
      toolOutput.textContent = [
        `yt-dlp: ${payload.yt_dlp_version || '不可用'}`,
        `FFmpeg: ${payload.ffmpeg_version || '不可用'}`,
        `ffprobe: ${payload.ffprobe_version || '不可用'}`,
        `离线 smoke: ${payload.offline_smoke_passed ? '通过' : '未通过'}`,
        `隔离 Worker: ${payload.isolated_worker_ready ? '已就绪' : '未就绪'}`,
        `真实平台下载: ${payload.platform_download_verified ? '已验证' : '未验证'}`,
        `通用隔离 Worker 联网策略（静态）: ${payload.network_download_enabled ? '已启用' : '默认关闭'}`,
        `本机直连入口（静态能力）: ${payload.local_direct_worker_available ? '具备启动条件' : '缺少启动条件'}`,
        `本机第三方工具包再分发状态: ${payload.redistribution_status || '未知'}`
      ].join('\\n');
      toolSecurityNote.textContent = payload.security_note;
    }

    async function loadToolchain() {
      const requestId = ++toolRequestId;
      refreshTools.disabled = true;
      toolStatus.textContent = '正在读取启动检查结果…';
      toolStatus.className = 'muted';
      try {
        const payload = await fetchJson(
          '/api/v1/operations/tools',
          {},
          '读取启动工具链状态'
        );
        if (requestId !== toolRequestId) return;
        renderToolchain(payload);
      } catch (error) {
        if (requestId !== toolRequestId) return;
        toolStatus.textContent = `工具链检查失败：${error}`;
        toolStatus.className = 'danger';
        toolOutput.textContent = '无法读取本机工具链状态，也无法据此判断独立本机 Worker 是否在线。';
      } finally {
        if (requestId === toolRequestId) refreshTools.disabled = false;
      }
    }

    function renderRuntimeLogs(payload) {
      const labels = { ok: '正常', degraded: '降级', error: '不可用' };
      const state = labels[payload.status] || '未知';
      const events = Array.isArray(payload.events) ? payload.events : [];
      logStatus.textContent = `日志状态：${state}；最近 ${events.length} 条（新→旧）；`
        + `写入失败 ${payload.write_failures}；拒绝事件 ${payload.rejected_events}；`
        + `最后错误 ${payload.last_failure_code}；run ${payload.run_id}`;
      logStatus.className = payload.status === 'ok' ? 'muted' : 'danger';
      logOutput.textContent = events.length
        ? events.slice().reverse().map(event => JSON.stringify(event)).join('\\n')
        : '暂无运行事件。';
    }

    async function loadRuntimeLogs() {
      const requestId = ++logRequestId;
      refreshLogs.disabled = true;
      logStatus.textContent = '正在读取运行日志状态…';
      logStatus.className = 'muted';
      try {
        const payload = await fetchJson(
          '/api/v1/operations/logs?limit=100',
          {},
          '读取运行日志'
        );
        if (requestId !== logRequestId) return;
        renderRuntimeLogs(payload);
      } catch (error) {
        if (requestId !== logRequestId) return;
        logStatus.textContent = `运行日志读取失败：${error}`;
        logStatus.className = 'danger';
        logOutput.textContent = '无法读取最近事件。请检查控制面日志状态和数据目录权限。';
      } finally {
        if (requestId === logRequestId) refreshLogs.disabled = false;
      }
    }

    resumeQueue.addEventListener('click', async () => {
      resumeQueue.disabled = true;
      try {
        await fetchJson('/api/v1/operations/queue/resume', { method: 'POST' }, '恢复队列');
        await loadQueueState();
      } catch (error) {
        queueStatus.textContent = `恢复失败：${error}`;
        queueStatus.className = 'danger';
      } finally {
        resumeQueue.disabled = false;
      }
    });

    refreshLogs.addEventListener('click', loadRuntimeLogs);
    refreshTools.addEventListener('click', loadToolchain);

    async function openBatch(batchId) {
      pollGeneration += 1;
      pollRequestId += 1;
      if (pollTimer !== null) clearTimeout(pollTimer);
      const generation = pollGeneration;
      result.hidden = false;
      clearCurrentBatchError();
      output.textContent = '正在打开批次…';
      currentBatchPayload = null;
      jobActions.replaceChildren();
      clearJobProgress();
      clearAssetLinks();
      try {
        const payload = await fetchJson(
          `/api/v1/batches/${encodeURIComponent(batchId)}`,
          {},
          '打开批次'
        );
        if (generation !== pollGeneration) return;
        renderPayload(payload);
        if ((payload.jobs || []).some(job => activeStatuses.has(job.status))) {
          schedulePoll(payload.id, generation);
        }
      } catch (error) {
        if (generation !== pollGeneration) return;
        const message = `打开批次失败：${error}`;
        output.textContent = message;
        showCurrentBatchError(message);
      }
    }

    async function loadRecentBatches() {
      const requestId = ++batchListRequestId;
      refreshBatches.disabled = true;
      if (recentBatches.children.length === 0) {
        setListMessage(recentBatches, 'loading', '正在读取最近批次…');
      }
      try {
        const batches = await fetchJson('/api/v1/batches?limit=20', {}, '读取最近批次');
        if (requestId !== batchListRequestId) return;
        recentBatches.className = 'batch-list';
        if (!Array.isArray(batches) || batches.length === 0) {
          setListMessage(recentBatches, 'empty', '暂无批次。');
          return;
        }
        reconcileKeyed(
          recentBatches,
          batches,
          batch => batch.id,
          () => {
            const item = document.createElement('li');
            item.className = 'item';
            item._summary = document.createElement('span');
            item._open = document.createElement('button');
            item._open.type = 'button';
            item._open.className = 'secondary';
            item._open.textContent = '打开';
            item._open.addEventListener('click', () => void openBatch(item._batchId));
            item.append(item._summary, item._open);
            return item;
          },
          (item, batch) => {
            item._batchId = batch.id;
            const displayName = batch.name || '未命名批次';
            const text = `${displayName} · ${batch.status} · ${batch.ready_count}/${batch.total_count} ready`;
            if (item._summary.textContent !== text) item._summary.textContent = text;
            item._open.setAttribute('aria-label', `打开批次：${displayName}`);
          }
        );
      } catch (error) {
        if (requestId !== batchListRequestId) return;
        recentBatches.className = 'batch-list danger';
        setListMessage(recentBatches, 'error', `最近批次读取失败：${error}`, 'danger');
      } finally {
        if (requestId === batchListRequestId) refreshBatches.disabled = false;
      }
    }

    refreshBatches.addEventListener('click', loadRecentBatches);

    function clearAssetLinks() {
      assetRequestId += 1;
      assetLinks.hidden = true;
      assetList.replaceChildren();
    }

    function clearJobProgress() {
      jobProgress.hidden = true;
      jobProgressList.replaceChildren();
    }

    function renderJobProgress(payload) {
      const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      if (jobs.length === 0) {
        jobProgressList.replaceChildren();
        jobProgress.hidden = true;
        return;
      }
      const statusLabels = {
        queued: '等待中',
        probing: '正在分析链接',
        downloading: '正在下载',
        postprocessing: '正在合并/后处理',
        verifying: '正在校验',
        ready: '已完成',
        failed: '失败',
        canceled: '已取消'
      };
      reconcileKeyed(
        jobProgressList,
        jobs,
        job => job.id,
        () => {
          const item = document.createElement('div');
          item.className = 'job-progress-item';
          const label = document.createElement('div');
          label.className = 'job-progress-label';
          item._state = document.createElement('span');
          item._value = document.createElement('span');
          item._progress = document.createElement('progress');
          item._progress.max = 100;
          label.append(item._state, item._value);
          item.append(label, item._progress);
          return item;
        },
        (item, job) => {
          const rawProgress = Number(job.progress);
          const hasProgress = Number.isFinite(rawProgress);
          const percent = hasProgress
            ? Math.round(Math.min(Math.max(rawProgress, 0), 1) * 100)
            : null;
          const platform = job.platform || 'unknown';
          const status = statusLabels[job.status] || job.status || 'unknown';
          const stateText = `${platform} · ${status}`;
          const valueText = percent === null ? '进度未知' : `约 ${percent}%`;
          if (item._state.textContent !== stateText) item._state.textContent = stateText;
          if (item._value.textContent !== valueText) item._value.textContent = valueText;
          if (percent === null) item._progress.removeAttribute?.('value');
          else item._progress.value = percent;
          item._progress.setAttribute('aria-label', `${platform} ${status}`);
        }
      );
      jobProgress.hidden = false;
    }

    async function loadReadyAssets(payload, generation) {
      if (assetRequestInFlight?.batchId === payload.id
          && assetRequestInFlight.generation === generation) {
        return assetRequestInFlight.promise;
      }
      const pending = {batchId: payload.id, generation, promise: null};
      assetRequestInFlight = pending;
      pending.promise = fetchReadyAssets(payload, generation).finally(() => {
        if (assetRequestInFlight === pending) assetRequestInFlight = null;
      });
      return pending.promise;
    }

    function updateAssetItem(item, asset, index) {
      if (!item._downloadLink) {
        item._downloadLink = document.createElement('a');
        item._downloadLink.setAttribute('download', '');
        item.append(item._downloadLink);
      }
      const kind = asset.original?.media_kind || 'media';
      const bytes = Number(asset.original?.size_bytes);
      const size = Number.isFinite(bytes) ? ` · ${bytes.toLocaleString()} bytes` : '';
      item._downloadLink.href = asset.download_url;
      item._downloadLink.textContent = `下载成品 ${index + 1}（${kind}${size}）`;

      const canUpload = kind === 'video' && typeof asset.asset_id === 'string';
      if (canUpload && !item._uploadLink) {
        item._editLink = document.createElement('a');
        item._editLink.textContent = '进入编辑';
        item.insertBefore(item._editLink, item._artifactList || null);
        item._uploadLink = document.createElement('a');
        item._uploadLink.textContent = '用于上传';
        item.insertBefore(item._uploadLink, item._artifactList || null);
      }
      if (canUpload) {
        item._editLink.href = '/edits?asset_id=' + encodeURIComponent(asset.asset_id);
        item._editLink.setAttribute('aria-label', `将成品 ${index + 1} 复制到编辑工作台`);
        item._uploadLink.href = '/uploads?asset_id=' + encodeURIComponent(asset.asset_id);
        item._uploadLink.setAttribute('aria-label', `将成品 ${index + 1} 用于上传`);
      } else if (item._uploadLink) {
        item._editLink.remove();
        item._editLink = null;
        item._uploadLink.remove();
        item._uploadLink = null;
      }

      const artifacts = Array.isArray(asset.artifacts) ? asset.artifacts : [];
      if (artifacts.length > 0 && !item._artifactList) {
        item._artifactList = document.createElement('ul');
        item._artifactList.className = 'artifact-list';
        item.append(item._artifactList);
      }
      if (artifacts.length > 0) {
        reconcileKeyed(
          item._artifactList,
          artifacts.map((artifact, artifactIndex) => ({artifact, artifactIndex})),
          entry => entry.artifact.artifact_id || entry.artifact.download_url || entry.artifactIndex,
          () => {
            const artifactItem = document.createElement('li');
            artifactItem._link = document.createElement('a');
            artifactItem._link.setAttribute('download', '');
            artifactItem.append(artifactItem._link);
            return artifactItem;
          },
          (artifactItem, entry) => {
            const {artifact, artifactIndex} = entry;
            const artifactKind = artifact.kind === 'thumbnail' ? '缩略图' : '字幕';
            const language = artifact.kind === 'caption' && artifact.language
              ? ` · ${artifact.language}`
              : '';
            artifactItem._link.href = artifact.download_url;
            artifactItem._link.textContent = `下载${artifactKind} ${artifactIndex + 1}${language}`;
          }
        );
      } else if (item._artifactList) {
        item._artifactList.remove();
        item._artifactList = null;
      }
    }

    async function fetchReadyAssets(payload, generation) {
      const requestId = ++assetRequestId;
      refreshAssets.disabled = true;
      assetLinks.hidden = false;
      if (assetList.children.length === 0) {
        setListMessage(assetList, 'loading', '正在读取可下载成品…');
      }
      try {
        const assets = await fetchJson(
          `/api/v1/batches/${encodeURIComponent(payload.id)}/assets`,
          {},
          '读取批次成品'
        );
        if (generation !== pollGeneration || requestId !== assetRequestId) return;
        if (!Array.isArray(assets) || assets.length === 0) {
          setListMessage(
            assetList,
            'empty',
            '暂时没有可下载的 ready 成品；任务完成后请点击“刷新成品”。'
          );
          return;
        }
        reconcileKeyed(
          assetList,
          assets.map((asset, index) => ({asset, index})),
          entry => entry.asset.asset_id || entry.asset.id || entry.asset.download_url,
          () => document.createElement('li'),
          (item, entry) => updateAssetItem(item, entry.asset, entry.index)
        );
      } catch (error) {
        if (generation !== pollGeneration || requestId !== assetRequestId) return;
        setListMessage(assetList, 'error', `成品列表读取失败：${error}`, 'danger');
      } finally {
        if (generation === pollGeneration && requestId === assetRequestId) {
          refreshAssets.disabled = false;
        }
      }
    }

    refreshAssets.addEventListener('click', async () => {
      if (currentBatchPayload === null) return;
      const payload = currentBatchPayload;
      const generation = pollGeneration;
      await loadReadyAssets(payload, generation);
    });

    async function loadCredentialDefaults() {
      const requestId = ++credentialRequestId;
      try {
        const payload = await fetchJson('/api/v1/credential-defaults', {}, '读取默认 Cookie 状态');
        if (requestId !== credentialRequestId) return;
        credentialStatus.className = payload.available ? 'muted' : 'danger';
        credentialStatus.textContent = !payload.available
          ? '默认 Cookie 配置已失效：请检查本地配置并重启，或明确选择匿名下载。'
          : payload.platforms.length
            ? `本次启动已配置默认 Cookie：${payload.platforms.join('、')}。文件可用不代表平台登录仍有效。`
            : '本次启动未配置默认 Cookie；新任务将匿名下载。';
      } catch (error) {
        if (requestId !== credentialRequestId) return;
        credentialStatus.className = 'danger';
        credentialStatus.textContent = `Cookie 状态未知：${error}`;
      }
    }

    function renderJobActions(payload) {
      const entries = [];
      for (const job of payload.jobs || []) {
        const retryableFlatFailure = job.status === 'failed'
          && job.job_kind === 'download'
          && job.source_type !== 'x_attachment';
        if (retryableFlatFailure) {
          entries.push({
            key: `retry:${job.id}`,
            kind: 'retry',
            jobId: job.id,
            platform: job.platform,
            batchId: payload.id,
            generation: pollGeneration
          });
        } else if (activeStatuses.has(job.status) && job.cancel_requested_at) {
          entries.push({
            key: `pending:${job.id}`,
            kind: 'pending',
            jobId: job.id,
            platform: job.platform,
            batchId: payload.id,
            generation: pollGeneration
          });
        } else if (activeStatuses.has(job.status)) {
          entries.push({
            key: `cancel:${job.id}`,
            kind: 'cancel',
            jobId: job.id,
            platform: job.platform,
            batchId: payload.id,
            generation: pollGeneration
          });
        }
      }
      reconcileKeyed(
        jobActions,
        entries,
        entry => entry.key,
        entry => {
          const control = document.createElement(entry.kind === 'pending' ? 'span' : 'button');
          if (entry.kind !== 'pending') {
            control.type = 'button';
            control.addEventListener('click', () => void runJobAction(control));
          }
          return control;
        },
        (control, entry) => {
          control._action = entry;
          if (entry.kind === 'pending') {
            control.className = 'muted';
            control.textContent = `${entry.platform} 任务已请求取消`;
            return;
          }
          control.className = entry.kind === 'cancel' ? 'danger-action' : 'secondary';
          control.textContent = entry.kind === 'retry'
            ? `重试 ${entry.platform} 任务（新一代）`
            : `取消 ${entry.platform} 任务`;
          control.disabled = jobActionInFlight.has(entry.key);
        }
      );
    }

    async function runJobAction(control) {
      const action = control._action;
      if (!action || jobActionInFlight.has(action.key)) return;
      const credential_mode = credentialMode.value;
      jobActionInFlight.add(action.key);
      control.disabled = true;
      try {
        if (action.kind === 'retry') {
          await fetchJson(
            `/api/v1/jobs/${encodeURIComponent(action.jobId)}/retry`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ credential_mode })
            },
            `重试 ${action.platform} 任务`
          );
          await loadCircuitState();
        } else {
          await fetchJson(
            `/api/v1/jobs/${encodeURIComponent(action.jobId)}/cancel`,
            { method: 'POST' },
            `取消 ${action.platform} 任务`
          );
        }
        if (action.generation !== pollGeneration) return;
        await pollBatch(action.batchId, action.generation);
      } catch (error) {
        if (action.generation !== pollGeneration) return;
        const message = `${action.kind === 'retry' ? '重试' : '取消'}失败：${error}`;
        output.textContent = message;
        showCurrentBatchError(message);
      } finally {
        jobActionInFlight.delete(action.key);
        if (control.isConnected) control.disabled = false;
        if (pageActive && currentBatchPayload) renderJobActions(currentBatchPayload);
      }
    }

    function renderPayload(payload) {
      currentBatchPayload = payload;
      clearCurrentBatchError();
      output.textContent = JSON.stringify(payload, null, 2);
      renderJobProgress(payload);
      renderJobActions(payload);
      void loadReadyAssets(payload, pollGeneration);
    }

    async function pollBatch(batchId, generation) {
      if (generation !== pollGeneration || !pageActive || document.hidden === true) return;
      const requestId = ++pollRequestId;
      let payload;
      try {
        payload = await fetchJson(
          `/api/v1/batches/${encodeURIComponent(batchId)}`,
          {},
          '轮询批次状态'
        );
      } catch (error) {
        if (generation !== pollGeneration || requestId !== pollRequestId) return;
        const message = `状态轮询失败：${error}；5 秒后重试。`;
        output.textContent = message;
        showCurrentBatchError(message);
        schedulePoll(batchId, generation, 5000);
        return;
      }
      if (generation !== pollGeneration || requestId !== pollRequestId) return;
      renderPayload(payload);
      if ((payload.jobs || []).some(job => activeStatuses.has(job.status))) {
        schedulePoll(batchId, generation);
      }
    }

    function schedulePoll(batchId, generation, delayMs = 2000) {
      if (generation !== pollGeneration || !pageActive || document.hidden === true) return;
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = setTimeout(() => {
        if (generation !== pollGeneration) return;
        pollTimer = null;
        pollBatch(batchId, generation);
      }, delayMs);
    }

    function suspendPageWork(markRuntimeUnknown) {
      pageActive = false;
      runtimeStopped = true;
      runtimeRequestId += 1;
      pollGeneration += 1;
      pollRequestId += 1;
      assetRequestId += 1;
      queueRequestId += 1;
      circuitRequestId += 1;
      capabilityRequestId += 1;
      toolRequestId += 1;
      logRequestId += 1;
      batchListRequestId += 1;
      credentialRequestId += 1;
      assetRequestInFlight = null;
      clearTimeout(runtimeTimer);
      clearTimeout(runtimeExpiryTimer);
      clearTimeout(pollTimer);
      clearTimeout(operationsTimer);
      runtimeTimer = null;
      runtimeExpiryTimer = null;
      pollTimer = null;
      operationsTimer = null;
      if (markRuntimeUnknown) {
        renderWorkerRuntime({mode: 'external_unknown', state: 'unknown'});
      }
    }

    function resumePageWork() {
      if (pageActive || document.hidden === true) return;
      pageActive = true;
      runtimeStopped = false;
      void loadWorkerRuntime();
      void refreshOperations();
      void loadCapabilityState();
      void loadToolchain();
      void loadRuntimeLogs();
      void loadCredentialDefaults();
      void loadRecentBatches();
      if (currentBatchPayload) {
        const generation = pollGeneration;
        void pollBatch(currentBatchPayload.id, generation);
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (event.isComposing || inputComposing) return;
      clearBatchError();
      pollGeneration += 1;
      pollRequestId += 1;
      if (pollTimer !== null) clearTimeout(pollTimer);
      const submitGeneration = pollGeneration;
      button.disabled = true;
      result.hidden = false;
      clearCurrentBatchError();
      output.textContent = '正在提交…';
      currentBatchPayload = null;
      jobActions.replaceChildren();
      clearJobProgress();
      clearAssetLinks();
      const name = nameInput.value || null;
      const credential_mode = credentialMode.value;
      const file = importFile.files[0];
      const inputs = inputsField.value.split(/\\r?\\n/).filter(line => line.trim());
      try {
        if (!file && inputs.length === 0) {
          showBatchError('请输入 URL，或选择 TXT/CSV 文件', inputsField);
          throw new Error('请输入 URL，或选择 TXT/CSV 文件');
        }
        if (file && inputs.length > 0) {
          showBatchError('文本与文件只能选择一种输入方式', inputsField);
          importFile.setAttribute('aria-invalid', 'true');
          throw new Error('文本与文件只能选择一种输入方式');
        }
        let payload;
        if (file) {
          const params = new URLSearchParams({ filename: file.name, credential_mode });
          if (name) params.set('name', name);
          payload = await fetchJson(`/api/v1/batches/import?${params}`, {
            method: 'POST',
            headers: { 'Content-Type': file.name.toLowerCase().endsWith('.csv') ? 'text/csv' : 'text/plain' },
            body: file
          }, '导入批次');
        } else {
          payload = await fetchJson('/api/v1/batches', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, inputs, credential_mode })
            }, '创建批次');
        }
        void loadRecentBatches();
        if (submitGeneration !== pollGeneration) return;
        renderPayload(payload);
        if ((payload.jobs || []).some(job => activeStatuses.has(job.status))) {
          schedulePoll(payload.id, submitGeneration);
        }
      } catch (error) {
        if (submitGeneration !== pollGeneration) return;
        output.textContent = `请求失败：${error}`;
        if (batchError.hidden) showBatchError(`请求失败：${error}`);
      } finally {
        button.disabled = false;
      }
    });
    for (const input of [nameInput, inputsField]) {
      input.addEventListener('compositionstart', () => { inputComposing = true; });
      input.addEventListener('compositionend', () => { inputComposing = false; });
      input.addEventListener('keydown', event => {
        if ((event.isComposing || event.keyCode === 229) && event.key === 'Enter') {
          event.preventDefault();
        }
      });
      input.addEventListener('input', clearBatchError);
    }
    importFile.addEventListener('change', clearBatchError);
    refreshCapabilities.addEventListener('click', loadCapabilityState);
    refreshCredentials.addEventListener('click', loadCredentialDefaults);
    loadCredentialDefaults();
    loadWorkerRuntime();
    refreshOperations();
    loadCapabilityState();
    loadToolchain();
    loadRuntimeLogs();
    loadRecentBatches();
  </script>
</body>
</html>
"""
