"""Execute the production page; timers/network are controlled at their edges."""
from test_batch_assets_ui import run_frontend


def test_runtime_online_status_expires_while_next_request_is_pending():
    result = run_frontend(r"""
      let calls = 0;
      globalThis.fetch = async () => {
        calls += 1;
        if (calls > 1) return new Promise(() => {});
        return {ok: true, json: async () => ({mode: 'managed_direct', state: 'online',
          heartbeat_age_seconds: 0, heartbeat_timeout_seconds: 3, network_download_enabled: true})};
      };
      await loadWorkerRuntime();
      const online = document.querySelector('#worker-runtime-status').textContent;
      const enabled = document.querySelector('#worker-runtime-detail').textContent;
      const expiry = __test.pendingTimerDelays().find(delay => delay > 1500 && delay <= 3000);
      void __test.fireTimer(1500);
      await __test.turn();
      __test.fireTimer(expiry);
      return {online, enabled, expired: document.querySelector('#worker-runtime-status').textContent,
        detail: document.querySelector('#worker-runtime-detail').textContent, calls};
    """)
    assert '运行中（本机托管直连）' in result['online']
    assert '本机网络下载已启用' in result['enabled']
    assert '心跳已过期' in result['expired']
    assert '本机网络下载已启用' not in result['detail']
    assert result['calls'] == 2


def test_runtime_late_response_is_stale_and_failure_is_unknown():
    result = run_frontend(r"""
      globalThis.fetch = async () => ({ok: true, json: async () => ({mode: 'managed_direct',
        state: 'online', heartbeat_age_seconds: 4, heartbeat_timeout_seconds: 3,
        network_download_enabled: true})});
      await loadWorkerRuntime();
      const late = document.querySelector('#worker-runtime-status').textContent;
      globalThis.fetch = async () => {throw new Error('offline');};
      await loadWorkerRuntime();
      return {late, failed: document.querySelector('#worker-runtime-status').textContent};
    """)
    assert '心跳已过期' in result['late']
    assert '状态未知（外部进程未观测）' in result['failed']


def test_runtime_paused_and_check_only_do_not_claim_active_work():
    result = run_frontend(r"""
      let state = 'paused';
      globalThis.fetch = async () => ({ok: true, json: async () => ({mode: 'managed_direct',
        state, heartbeat_age_seconds: state === 'paused' ? 0 : 120, heartbeat_timeout_seconds: 3,
        network_download_enabled: state === 'paused'})});
      await loadWorkerRuntime();
      const paused = document.querySelector('#worker-runtime-status').textContent;
      state = 'check_only';
      await loadWorkerRuntime();
      return {paused, check: document.querySelector('#worker-runtime-status').textContent,
        detail: document.querySelector('#worker-runtime-detail').textContent};
    """)
    assert '队列已暂停' in result['paused']
    assert '仅检查模式' in result['check']
    assert '不领取下载任务' in result['detail']
