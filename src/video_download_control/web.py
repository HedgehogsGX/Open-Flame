from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>多平台视频下载控制面</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; background: #f5f6f8; color: #18202a; }
    body { margin: 0; }
    main { max-width: 880px; margin: 0 auto; padding: 48px 20px 80px; }
    h1 { margin-bottom: 8px; }
    .muted { color: #5d6875; }
    .card { background: white; border: 1px solid #dfe3e8; border-radius: 14px; padding: 22px; margin-top: 24px; box-shadow: 0 8px 30px rgba(24,32,42,.06); }
    label { display: block; font-weight: 650; margin: 14px 0 7px; }
    input, textarea, button { box-sizing: border-box; font: inherit; }
    input, textarea { width: 100%; border: 1px solid #bbc3cc; border-radius: 8px; padding: 10px 12px; }
    textarea { min-height: 190px; resize: vertical; }
    button { margin-top: 16px; border: 0; border-radius: 8px; background: #155eef; color: white; padding: 11px 18px; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .6; cursor: wait; }
    pre { white-space: pre-wrap; word-break: break-word; background: #111827; color: #d1fae5; padding: 16px; border-radius: 9px; overflow: auto; }
    .notice { border-left: 4px solid #d97706; padding-left: 12px; }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .row button { margin-top: 0; }
    .row-spread { justify-content: space-between; align-items: flex-start; }
    .row-spread h2 { margin: 0 0 8px; }
    .log-output { max-height: 360px; margin-bottom: 0; font-size: .84rem; }
    .asset-list { margin: 12px 0 0; padding-left: 22px; }
    .asset-list li { margin: 8px 0; }
    .asset-list a { color: #155eef; font-weight: 700; }
    .batch-list { list-style: none; margin: 12px 0 0; padding: 0; }
    .batch-list li { display: flex; gap: 12px; align-items: center; justify-content: space-between; padding: 10px 0; border-top: 1px solid #edf0f3; }
    .batch-list button { flex: 0 0 auto; margin-top: 0; padding: 8px 12px; }
    .danger { color: #a61b1b; font-weight: 700; }
  </style>
</head>
<body>
  <main>
    <h1>多平台视频下载控制面</h1>
    <p class="muted">迭代 0.10.0：六平台显式能力路由、Windows 本机下载 Worker、成品下载和脱敏运行日志。</p>
    <p class="notice">本机直连 Worker 已接入，但由单独进程显式运行；页面不会自行启动或推断它是否在线。Worker 在线时会处理任务并显示成品链接，离线时任务会保持排队。</p>
    <section class="card">
      <h2>运行状态</h2>
      <div class="row">
        <span id="queue-status">正在读取队列状态…</span>
        <button id="resume-queue" type="button" hidden>确认磁盘恢复并继续队列</button>
      </div>
      <div id="circuit-status" class="muted">正在读取平台状态…</div>
      <h3>下载能力</h3>
      <div id="capability-status" class="muted">正在读取平台能力矩阵…</div>
    </section>
    <section class="card" aria-labelledby="toolchain-heading">
      <div class="row row-spread">
        <div>
          <h2 id="toolchain-heading">本机工具链</h2>
          <p class="muted">控制端启动时检查固定的 yt-dlp、FFmpeg 与 ffprobe，并缓存本次离线验证结果；刷新显示不会重新校验整个工具包。</p>
        </div>
        <button id="refresh-tools" type="button">刷新显示</button>
      </div>
      <p id="tool-status" class="muted">正在读取启动检查结果…</p>
      <pre id="tool-output" class="log-output" aria-live="polite">尚无工具链检查结果。</pre>
      <p id="tool-security-note" class="notice">工具链就绪不代表外部 Worker 当前在线；Windows 本机直连入口需要在独立进程中显式启动，完整平台能力仍未验证。</p>
    </section>
    <section class="card" aria-labelledby="runtime-logs-heading">
      <div class="row row-spread">
        <div>
          <h2 id="runtime-logs-heading">运行日志</h2>
          <p class="muted">显示脱敏后的本机排障事件；日志是辅助线索，不替代数据库、资产 manifest 或验收证据。</p>
        </div>
        <button id="refresh-logs" type="button">手动刷新</button>
      </div>
      <p id="log-status" class="muted">正在读取运行日志状态…</p>
      <pre id="log-output" class="log-output" aria-live="polite">正在读取最近事件…</pre>
    </section>
    <section class="card" aria-labelledby="recent-batches-heading">
      <div class="row row-spread">
        <div>
          <h2 id="recent-batches-heading">最近批次</h2>
          <p class="muted">刷新页面后也可以重新打开任务、继续轮询并下载已完成成品。</p>
        </div>
        <button id="refresh-batches" type="button">刷新批次</button>
      </div>
      <ul id="recent-batches" class="batch-list" aria-live="polite"></ul>
    </section>
    <section class="card">
      <form id="batch-form">
        <label for="name">批次名称（可选）</label>
        <input id="name" maxlength="200" placeholder="例如：9 月素材">
        <label for="inputs">URL 或包含 URL 的分享文本（每行一条）</label>
        <textarea id="inputs" placeholder="https://www.bilibili.com/video/BV...&#10;https://www.douyin.com/video/...&#10;https://www.tiktok.com/@user/video/...&#10;https://www.instagram.com/reel/..."></textarea>
        <label for="import-file">或导入 UTF-8 TXT/CSV（最多 256 KiB）</label>
        <input id="import-file" type="file" accept=".txt,.csv,text/plain,text/csv">
        <button id="submit" type="submit">创建批次</button>
      </form>
      <div id="result" hidden>
        <h2>结果</h2>
        <div id="job-actions"></div>
        <section id="asset-links" hidden aria-live="polite">
          <h3>可下载成品</h3>
          <ul id="asset-list" class="asset-list"></ul>
        </section>
        <pre id="output"></pre>
      </div>
    </section>
  </main>
  <script>
    const form = document.querySelector('#batch-form');
    const button = document.querySelector('#submit');
    const result = document.querySelector('#result');
    const output = document.querySelector('#output');
    const jobActions = document.querySelector('#job-actions');
    const assetLinks = document.querySelector('#asset-links');
    const assetList = document.querySelector('#asset-list');
    const queueStatus = document.querySelector('#queue-status');
    const resumeQueue = document.querySelector('#resume-queue');
    const circuitStatus = document.querySelector('#circuit-status');
    const capabilityStatus = document.querySelector('#capability-status');
    const refreshTools = document.querySelector('#refresh-tools');
    const toolStatus = document.querySelector('#tool-status');
    const toolOutput = document.querySelector('#tool-output');
    const toolSecurityNote = document.querySelector('#tool-security-note');
    const refreshLogs = document.querySelector('#refresh-logs');
    const logStatus = document.querySelector('#log-status');
    const logOutput = document.querySelector('#log-output');
    const refreshBatches = document.querySelector('#refresh-batches');
    const recentBatches = document.querySelector('#recent-batches');
    let pollGeneration = 0;
    let pollRequestId = 0;
    let assetRequestId = 0;
    let toolRequestId = 0;
    let logRequestId = 0;
    let batchListRequestId = 0;
    let pollTimer = null;
    let operationsTimer = null;
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

    async function loadQueueState() {
      try {
        const queue = await fetchJson('/api/v1/operations/queue', {}, '读取队列状态');
        queueStatus.textContent = queue.paused
          ? `队列已暂停：${queue.reason || 'unknown'}`
          : '队列未暂停；可接收新任务（不代表 Worker 已启动）';
        queueStatus.className = queue.paused ? 'danger' : '';
        resumeQueue.hidden = !queue.paused;
      } catch (error) {
        queueStatus.textContent = `队列状态读取失败：${error}`;
        queueStatus.className = 'danger';
        resumeQueue.hidden = true;
      }
    }

    async function loadCircuitState() {
      try {
        const circuits = await fetchJson('/api/v1/platform-circuits', {}, '读取平台状态');
        circuitStatus.replaceChildren();
        circuitStatus.className = 'muted';
        if (!circuits.length) {
          circuitStatus.textContent = '尚无平台熔断记录。';
          return;
        }
        for (const circuit of circuits) {
          const line = document.createElement('p');
          line.textContent = `${circuit.platform}: ${circuit.state}`
            + (circuit.last_error_code ? ` (${circuit.last_error_code})` : '');
          if (circuit.state === 'open') {
            line.className = 'danger';
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
          }
          circuitStatus.append(line);
        }
      } catch (error) {
        circuitStatus.textContent = `平台状态读取失败：${error}`;
        circuitStatus.className = 'danger';
      }
    }

    async function loadCapabilityState() {
      try {
        const capabilities = await fetchJson(
          '/api/v1/download-capabilities',
          {},
          '读取平台能力矩阵'
        );
        capabilityStatus.replaceChildren();
        capabilityStatus.className = 'muted';
        const labels = {
          disabled: '未启用',
          candidate: '候选（尚未完成平台级验收）',
          verified: '已验证'
        };
        const shortLinkLabels = {
          not_applicable: '短链不适用',
          supported: '短链可直接使用',
          gated: '短链需单独启用解析服务',
          deferred: '短链尚未接入'
        };
        for (const capability of capabilities) {
          const line = document.createElement('p');
          line.textContent = `${capability.platform} / ${capability.source_type}: `
            + `${labels[capability.status] || capability.status}`
            + `；${capability.authentication}`
            + `；${shortLinkLabels[capability.short_link_status] || capability.short_link_status}`
            + `；证据环境 ${capability.environment || '未绑定'}`;
          capabilityStatus.append(line);
        }
      } catch (error) {
        capabilityStatus.textContent = `平台能力读取失败：${error}`;
        capabilityStatus.className = 'danger';
      }
    }

    async function loadOperations() {
      await Promise.all([
        loadQueueState(),
        loadCircuitState(),
        loadCapabilityState()
      ]);
    }

    async function refreshOperations() {
      await loadOperations();
      if (operationsTimer !== null) clearTimeout(operationsTimer);
      operationsTimer = setTimeout(refreshOperations, 10000);
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
        `联网下载开关: ${payload.network_download_enabled ? '已启用' : '未启用'}`,
        `本机直连 Worker: ${payload.local_direct_worker_available ? '可显式启动' : '当前不可用'}`,
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
      output.textContent = '正在打开批次…';
      jobActions.replaceChildren();
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
        output.textContent = `打开批次失败：${error}`;
      }
    }

    async function loadRecentBatches() {
      const requestId = ++batchListRequestId;
      refreshBatches.disabled = true;
      recentBatches.textContent = '正在读取最近批次…';
      try {
        const batches = await fetchJson('/api/v1/batches?limit=20', {}, '读取最近批次');
        if (requestId !== batchListRequestId) return;
        recentBatches.className = 'batch-list';
        recentBatches.replaceChildren();
        if (!Array.isArray(batches) || batches.length === 0) {
          const empty = document.createElement('li');
          empty.textContent = '暂无批次。';
          recentBatches.append(empty);
          return;
        }
        for (const batch of batches) {
          const item = document.createElement('li');
          const summary = document.createElement('span');
          const displayName = batch.name || '未命名批次';
          summary.textContent = `${displayName} · ${batch.status} · ${batch.ready_count}/${batch.total_count} ready`;
          const open = document.createElement('button');
          open.type = 'button';
          open.textContent = '打开';
          open.addEventListener('click', () => void openBatch(batch.id));
          item.append(summary, open);
          recentBatches.append(item);
        }
      } catch (error) {
        if (requestId !== batchListRequestId) return;
        recentBatches.textContent = `最近批次读取失败：${error}`;
        recentBatches.className = 'batch-list danger';
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

    async function loadReadyAssets(payload, generation) {
      if (!['ready', 'partial_success'].includes(payload.status)) {
        clearAssetLinks();
        return;
      }
      const requestId = ++assetRequestId;
      assetLinks.hidden = false;
      assetList.textContent = '正在读取可下载成品…';
      try {
        const assets = await fetchJson(
          `/api/v1/batches/${encodeURIComponent(payload.id)}/assets`,
          {},
          '读取批次成品'
        );
        if (generation !== pollGeneration || requestId !== assetRequestId) return;
        assetList.replaceChildren();
        if (!Array.isArray(assets) || assets.length === 0) {
          const empty = document.createElement('li');
          empty.textContent = '批次已结束，但没有可下载的 ready 成品。';
          assetList.append(empty);
          return;
        }
        for (const [index, asset] of assets.entries()) {
          const item = document.createElement('li');
          const link = document.createElement('a');
          link.href = asset.download_url;
          link.setAttribute('download', '');
          const kind = asset.original?.media_kind || 'media';
          const bytes = Number(asset.original?.size_bytes);
          const size = Number.isFinite(bytes) ? ` · ${bytes.toLocaleString()} bytes` : '';
          link.textContent = `下载成品 ${index + 1}（${kind}${size}）`;
          item.append(link);
          assetList.append(item);
        }
      } catch (error) {
        if (generation !== pollGeneration || requestId !== assetRequestId) return;
        assetList.replaceChildren();
        const failure = document.createElement('li');
        failure.textContent = `成品列表读取失败：${error}`;
        failure.className = 'danger';
        assetList.append(failure);
      }
    }

    function renderPayload(payload) {
      output.textContent = JSON.stringify(payload, null, 2);
      jobActions.replaceChildren();
      for (const job of payload.jobs || []) {
        if (!activeStatuses.has(job.status)) continue;
        if (job.cancel_requested_at) {
          const pending = document.createElement('span');
          pending.textContent = `${job.platform} 任务已请求取消`;
          jobActions.append(pending);
          continue;
        }
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.textContent = `取消 ${job.platform} 任务`;
        cancel.addEventListener('click', async () => {
          cancel.disabled = true;
          try {
            await fetchJson(
              `/api/v1/jobs/${encodeURIComponent(job.id)}/cancel`,
              { method: 'POST' },
              `取消 ${job.platform} 任务`
            );
            await pollBatch(payload.id, pollGeneration);
          } catch (error) {
            output.textContent = `取消失败：${error}`;
          } finally {
            if (cancel.isConnected) cancel.disabled = false;
          }
        });
        jobActions.append(cancel);
      }
      if (['ready', 'partial_success'].includes(payload.status)) {
        void loadReadyAssets(payload, pollGeneration);
      } else {
        clearAssetLinks();
      }
    }

    async function pollBatch(batchId, generation) {
      if (generation !== pollGeneration) return;
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
        output.textContent = `状态轮询失败：${error}；5 秒后重试。`;
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
      if (generation !== pollGeneration) return;
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = setTimeout(() => {
        if (generation !== pollGeneration) return;
        pollTimer = null;
        pollBatch(batchId, generation);
      }, delayMs);
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      pollGeneration += 1;
      pollRequestId += 1;
      if (pollTimer !== null) clearTimeout(pollTimer);
      const generation = pollGeneration;
      button.disabled = true;
      result.hidden = false;
      output.textContent = '正在提交…';
      jobActions.replaceChildren();
      clearAssetLinks();
      const name = document.querySelector('#name').value || null;
      const file = document.querySelector('#import-file').files[0];
      const inputs = document.querySelector('#inputs').value.split(/\\r?\\n/).filter(line => line.trim());
      try {
        if (!file && inputs.length === 0) throw new Error('请输入 URL，或选择 TXT/CSV 文件');
        if (file && inputs.length > 0) throw new Error('文本与文件只能选择一种输入方式');
        let payload;
        if (file) {
          const params = new URLSearchParams({ filename: file.name });
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
              body: JSON.stringify({ name, inputs })
            }, '创建批次');
        }
        renderPayload(payload);
        void loadRecentBatches();
        if ((payload.jobs || []).some(job => activeStatuses.has(job.status))) {
          schedulePoll(payload.id, generation);
        }
      } catch (error) {
        output.textContent = `请求失败：${error}`;
      } finally {
        button.disabled = false;
      }
    });
    refreshOperations();
    loadToolchain();
    loadRuntimeLogs();
    loadRecentBatches();
  </script>
</body>
</html>
"""
