from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from video_download_control.web import INDEX_HTML


class PageFixtureParser(HTMLParser):
    """Preserve the page's real DOM fixture and complete, unmodified script."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.scripts = []
        self.script_parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.script_parts = None if "src" in dict(attrs) else []
            return
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag == "script":
            if self.script_parts is not None:
                self.scripts.append("".join(self.script_parts))
            self.script_parts = None
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.script_parts is not None:
            self.script_parts.append(data)
        else:
            self.stack[-1]["children"].append({"tag": "#text", "text": data})


NODE_HARNESS = r"""
const vm = require('node:vm');
const ids = new Map();
const documentListeners = new Map();
const windowListeners = new Map();

function addListener(registry, name, callback) {
  const handlers = registry.get(name) || [];
  handlers.push(callback);
  registry.set(name, handlers);
}

async function dispatchEvent(registry, target, name, init = {}) {
  const event = {
    target,
    currentTarget: target,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    ...init
  };
  for (const callback of registry.get(name) || []) await callback(event);
  return event;
}

class Element {
  constructor(tag, attrs = {}, text = '') {
    this.tagName = tag;
    this.attributes = Object.fromEntries(Object.entries(attrs).map(([k, v]) => [k, v ?? '']));
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.className = attrs.class || '';
    this.hidden = Object.hasOwn(attrs, 'hidden');
    this.disabled = Object.hasOwn(attrs, 'disabled');
    this.value = attrs.value || '';
    this.files = [];
    this._textContent = text;
    if (attrs.id) ids.set(attrs.id, this);
  }
  set textContent(value) {
    if (document.activeElement !== this
        && document.activeElement
        && this.contains(document.activeElement)) {
      document.activeElement = null;
    }
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._textContent = String(value);
  }
  get textContent() { return this._textContent + this.children.map(child => child.textContent).join(''); }
  get isConnected() { return this.tagName === 'document' || !!this.parentElement?.isConnected; }
  contains(candidate) {
    for (let current = candidate; current; current = current.parentElement) {
      if (current === this) return true;
    }
    return false;
  }
  append(...children) {
    for (let child of children) {
      if (typeof child === 'string') child = new Element('#text', {}, child);
      if (child.parentElement) {
        const previous = child.parentElement.children.indexOf(child);
        if (previous >= 0) child.parentElement.children.splice(previous, 1);
      }
      child.parentElement = this;
      this.children.push(child);
    }
  }
  insertBefore(child, reference) {
    if (child === reference) return child;
    if (reference !== null && reference.parentElement !== this) {
      throw new Error('reference is not a child');
    }
    if (child.parentElement) {
      const previous = child.parentElement.children.indexOf(child);
      if (previous >= 0) child.parentElement.children.splice(previous, 1);
    }
    const index = reference === null ? this.children.length : this.children.indexOf(reference);
    child.parentElement = this;
    this.children.splice(index, 0, child);
    return child;
  }
  remove() {
    if (!this.parentElement) return;
    if (document.activeElement && this.contains(document.activeElement)) {
      document.activeElement = null;
    }
    const index = this.parentElement.children.indexOf(this);
    if (index >= 0) this.parentElement.children.splice(index, 1);
    this.parentElement = null;
  }
  replaceChildren(...children) { this.textContent = ''; this.append(...children); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'hidden') this.hidden = true;
    if (name === 'disabled') this.disabled = true;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === 'class') this.className = '';
    if (name === 'hidden') this.hidden = false;
    if (name === 'disabled') this.disabled = false;
  }
  focus() {
    if (this.isConnected && !this.disabled) document.activeElement = this;
  }
  addEventListener(name, callback) {
    addListener(this.listeners, name, callback);
  }
  async dispatch(name, init = {}) {
    return dispatchEvent(this.listeners, this, name, init);
  }
}
function construct(node) {
  const element = new Element(node.tag, node.attrs, node.text || '');
  for (const child of node.children || []) element.append(construct(child));
  if (node.tag === 'textarea') element.value = element.textContent;
  if (node.tag === 'select') {
    const options = element.children.filter(child => child.tagName === 'option');
    const selected = options.find(child => Object.hasOwn(child.attributes, 'selected')) || options[0];
    element.value = selected?.value || '';
  }
  return element;
}
const root = construct(fixture.page);
const document = {
  activeElement: null,
  hidden: false,
  querySelector(selector) {
    if (!selector.startsWith('#')) throw new Error('unsupported test selector: ' + selector);
    return ids.get(selector.slice(1)) || null;
  },
  createElement: tag => new Element(tag),
  addEventListener(name, callback) {
    addListener(documentListeners, name, callback);
  }
};
const timers = new Map();
let nextTimer = 0;
const state = {
  requests: [],
  batches: {},
  assets: {},
  recent: [],
  created: null,
  // Advance an actual event-loop turn, never a time-based sleep. Timers are
  // held at the boundary so production polling cannot race the assertions.
  turn: () => new Promise(resolve => setImmediate(resolve)),
  pendingTimerDelays: () => [...timers.values()].map(timer => timer.delay),
  dispatchDocument(name, init = {}) {
    return dispatchEvent(documentListeners, document, name, init);
  },
  dispatchWindow(name, init = {}) {
    return dispatchEvent(windowListeners, context, name, init);
  },
  setHidden(value) { document.hidden = Boolean(value); },
  fireTimer(delay) {
    const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    if (!entry) throw new Error('no scheduled timer with delay: ' + delay);
    const [id, timer] = entry;
    timers.delete(id);
    return timer.callback();
  },
  deferAssets(batchId) {
    let release;
    const gate = {entered: false};
    gate.promise = new Promise(resolve => {release = resolve;});
    gate.release = release;
    this.assets[batchId] = gate;
    return gate;
  },
  mutations: {},
  deferMutation(url) {
    let release;
    const gate = {entered: false};
    gate.promise = new Promise(resolve => {release = resolve;});
    gate.release = release;
    this.mutations[url] = gate;
    return gate;
  }
};
function response(payload, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => payload};
}
async function fetch(url, options = {}) {
  url = String(url);
  state.requests.push({url, options});
  if (url === '/api/v1/operations/runtime') return response({mode: 'external_unknown', state: 'unknown'});
  if (url === '/api/v1/credential-defaults') return response({available: true, platforms: []});
  if (url === '/api/v1/operations/queue') return response({paused: false});
  if (url === '/api/v1/platform-circuits') return response([]);
  if (url === '/api/v1/capability-snapshot') return response({implementations: [], evidence: [], decisions: []});
  if (url === '/api/v1/operations/tools') return response({state: 'unconfigured', security_note: 'synthetic tools'});
  if (url.startsWith('/api/v1/operations/logs')) return response({status: 'ok', events: [], write_failures: 0, rejected_events: 0, last_failure_code: null, run_id: 'synthetic-run'});
  if (url.startsWith('/api/v1/batches?')) return response(state.recent);
  if (url === '/api/v1/batches' && options.method === 'POST') return response(state.created, 201);
  if (/^\/api\/v1\/jobs\/[^/]+\/(?:cancel|retry)$/.test(url) && options.method === 'POST') {
    if (!Object.hasOwn(state.mutations, url)) throw new Error('unconfigured mutation: ' + url);
    const reply = state.mutations[url];
    if (reply?.promise) {
      reply.entered = true;
      const released = await reply.promise;
      return response(released.payload, released.status);
    }
    return response(reply);
  }
  const assetMatch = url.match(/^\/api\/v1\/batches\/([^/]+)\/assets$/);
  if (assetMatch) {
    const batchId = decodeURIComponent(assetMatch[1]);
    if (!Object.hasOwn(state.assets, batchId)) throw new Error('unconfigured assets: ' + batchId);
    const reply = state.assets[batchId];
    if (reply?.promise) {
      reply.entered = true;
      const released = await reply.promise;
      return response(released.payload, released.status);
    }
    return response(reply);
  }
  const batchMatch = url.match(/^\/api\/v1\/batches\/([^/]+)$/);
  if (batchMatch) {
    const batchId = decodeURIComponent(batchMatch[1]);
    if (!Object.hasOwn(state.batches, batchId)) throw new Error('unconfigured batch: ' + batchId);
    return response(state.batches[batchId]);
  }
  throw new Error('unexpected fetch: ' + url);
}
const context = vm.createContext({
  document, fetch, URLSearchParams, console,
  addEventListener: (name, callback) => addListener(windowListeners, name, callback),
  setTimeout: (callback, delay) => {
    const id = ++nextTimer;
    timers.set(id, {callback, delay});
    return id;
  },
  clearTimeout: id => timers.delete(id),
  __test: state
});
(async () => {
  vm.runInContext(fixture.script, context, {timeout: 2000});
  await state.turn();
  const result = await vm.runInContext('(async () => {' + fixture.exercise + '})()', context, {timeout: 2000});
  await state.turn();
  process.stdout.write(JSON.stringify(result));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


def run_frontend(exercise: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for executable batch-assets UI tests")
    parser = PageFixtureParser()
    parser.feed(INDEX_HTML)
    parser.close()
    assert len(parser.scripts) == 1
    fixture = {"page": parser.root, "script": parser.scripts[0], "exercise": exercise}
    completed = subprocess.run(
        [node],
        input="const fixture = " + json.dumps(fixture) + ";\n" + NODE_HARNESS,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


ASSET_HELPERS = r"""
  const asset = id => ({
    id, job_id: id + '-job',
    download_url: '/api/v1/assets/' + id + '/download',
    original: {media_kind: 'video', size_bytes: 128}, artifacts: []
  });
  const allLinks = element => [
    ...(element.tagName === 'a' ? [{href: element.href, text: element.textContent}] : []),
    ...element.children.flatMap(allLinks)
  ];
  const snapshot = () => ({
    hidden: document.querySelector('#asset-links').hidden,
    links: allLinks(document.querySelector('#asset-list')),
    text: document.querySelector('#asset-list').textContent,
    output: JSON.parse(document.querySelector('#output').textContent)
  });
"""


def test_active_batch_displays_ready_media_while_other_job_remains_queued():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'active-batch', status: 'queued', ready_count: 1, total_count: 2,
        jobs: [
          {id: 'finished-job', platform: 'youtube', status: 'ready', progress: 1},
          {id: 'queued-job', platform: 'bilibili', status: 'queued', progress: 0}
        ]
      };
      __test.assets['active-batch'] = [asset('finished-asset')];
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      return {
        ...snapshot(),
        actions: document.querySelector('#job-actions').textContent,
        progressVisible: !document.querySelector('#job-progress').hidden,
        hasPoll: __test.pendingTimerDelays().includes(2000),
        assetRequests: __test.requests.filter(request => request.url.endsWith('/assets')).map(request => request.url)
      };
    """)

    assert rendered["output"]["status"] == "queued"
    assert [job["status"] for job in rendered["output"]["jobs"]] == ["ready", "queued"]
    assert rendered["progressVisible"] is True
    assert "取消 bilibili 任务" in rendered["actions"]
    assert rendered["hasPoll"] is True
    assert rendered["hidden"] is False, rendered
    assert [link["href"] for link in rendered["links"]] == [
        "/api/v1/assets/finished-asset/download"
    ]
    assert rendered["assetRequests"] == ["/api/v1/batches/active-batch/assets"]


def test_duplicate_batch_without_jobs_displays_existing_ready_media():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'duplicate-batch', status: 'duplicate', ready_count: 0,
        duplicate_count: 1, total_count: 1, jobs: [],
        inputs: [{id: 'duplicate-input', status: 'duplicate',
                  duplicate_of_input_record_id: 'original-input'}]
      };
      __test.assets['duplicate-batch'] = [asset('existing-asset')];
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      return {
        ...snapshot(),
        actions: document.querySelector('#job-actions').textContent,
        assetRequests: __test.requests.filter(request => request.url.endsWith('/assets')).map(request => request.url),
        postRequests: __test.requests.filter(request => request.options.method === 'POST').map(request => request.url)
      };
    """)

    assert rendered["output"]["status"] == "duplicate"
    assert rendered["output"]["jobs"] == []
    assert rendered["actions"] == ""
    assert rendered["postRequests"] == ["/api/v1/batches"]
    assert rendered["hidden"] is False, rendered
    assert [link["href"] for link in rendered["links"]] == [
        "/api/v1/assets/existing-asset/download"
    ]
    assert rendered["assetRequests"] == ["/api/v1/batches/duplicate-batch/assets"]


def test_duplicate_batch_can_manually_refresh_when_owner_finishes_later():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'waiting-duplicate', status: 'duplicate', ready_count: 0,
        duplicate_count: 1, total_count: 1, jobs: []
      };
      __test.assets['waiting-duplicate'] = [];
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      const beforeRefresh = snapshot();
      __test.assets['waiting-duplicate'] = [asset('owner-finished-asset')];
      const refresh = document.querySelector('#refresh-assets');
      const refreshLabel = refresh.textContent;
      await refresh.dispatch('click');
      const afterRefresh = snapshot();
      return {
        beforeRefresh, afterRefresh, refreshLabel,
        refreshDisabled: refresh.disabled,
        assetRequests: __test.requests.filter(request => request.url.endsWith('/assets')).map(request => request.url),
        postRequests: __test.requests.filter(request => request.options.method === 'POST').map(request => request.url),
        pendingTimers: __test.pendingTimerDelays()
      };
    """)

    assert rendered["beforeRefresh"]["links"] == []
    assert "刷新成品" in rendered["beforeRefresh"]["text"]
    assert rendered["refreshLabel"] == "刷新成品"
    assert rendered["refreshDisabled"] is False
    assert [link["href"] for link in rendered["afterRefresh"]["links"]] == [
        "/api/v1/assets/owner-finished-asset/download"
    ]
    assert rendered["afterRefresh"]["output"]["id"] == "waiting-duplicate"
    assert rendered["assetRequests"] == [
        "/api/v1/batches/waiting-duplicate/assets",
        "/api/v1/batches/waiting-duplicate/assets",
    ]
    assert rendered["postRequests"] == ["/api/v1/batches"]
    assert 2000 not in rendered["pendingTimers"]


@pytest.mark.parametrize("status", ["queued", "duplicate"])
def test_empty_asset_list_does_not_claim_the_batch_has_finished(status):
    rendered = run_frontend(
        "const status = " + json.dumps(status) + ";\n" + ASSET_HELPERS + r"""
      __test.created = {
        id: 'empty-batch', status, ready_count: 0, total_count: 1,
        jobs: status === 'queued'
          ? [{id: 'queued-job', platform: 'youtube', status: 'queued', progress: 0}]
          : []
      };
      __test.assets['empty-batch'] = [];
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      return snapshot();
    """)

    assert rendered["output"]["status"] == status
    assert rendered["hidden"] is False
    assert rendered["links"] == []
    assert "批次已结束" not in rendered["text"]
    assert "没有可下载的 ready 成品" in rendered["text"]


def test_poll_keeps_existing_links_visible_until_refreshed_assets_arrive():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'polling-batch', status: 'queued', ready_count: 1, total_count: 2,
        jobs: [
          {id: 'ready-job', platform: 'youtube', status: 'ready', progress: 1},
          {id: 'active-job', platform: 'bilibili', status: 'downloading', progress: 0.2}
        ]
      };
      __test.assets['polling-batch'] = [asset('first-asset')];
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      const beforePoll = snapshot();
      const originalLink = document.querySelector('#asset-list').children[0].children[0];
      const nextBatch = JSON.parse(JSON.stringify(__test.created));
      nextBatch.jobs[1].progress = 0.7;
      __test.batches['polling-batch'] = nextBatch;
      const refresh = __test.deferAssets('polling-batch');
      __test.fireTimer(2000);
      await __test.turn();
      const whilePending = snapshot();
      const sameLinkWhilePending = document.querySelector('#asset-list').children[0]?.children[0] === originalLink;
      refresh.release({status: 200, payload: [asset('first-asset'), asset('new-asset')]});
      await __test.turn();
      return {
        beforePoll, whilePending, sameLinkWhilePending,
        afterRefresh: snapshot(), refreshEntered: refresh.entered,
        pollScheduled: __test.pendingTimerDelays().includes(2000),
        actions: document.querySelector('#job-actions').textContent
      };
    """)

    assert rendered["refreshEntered"] is True
    assert rendered["whilePending"]["output"]["jobs"][1]["progress"] == 0.7
    assert rendered["pollScheduled"] is True
    assert "取消 bilibili 任务" in rendered["actions"]
    assert rendered["whilePending"]["hidden"] is False
    assert rendered["whilePending"]["links"] == rendered["beforePoll"]["links"]
    assert rendered["sameLinkWhilePending"] is True
    assert [link["href"] for link in rendered["afterRefresh"]["links"]] == [
        "/api/v1/assets/first-asset/download",
        "/api/v1/assets/new-asset/download",
    ]


@pytest.mark.parametrize("slow_status", [200, 503])
def test_slow_assets_are_single_flight_across_polls_and_later_polls_still_refresh(slow_status):
    rendered = run_frontend(
        "const slowStatus = " + json.dumps(slow_status) + ";\n" + ASSET_HELPERS + r"""
      __test.created = {
        id: 'slow-batch', status: 'queued', ready_count: 1, total_count: 2,
        jobs: [
          {id: 'ready-job', platform: 'youtube', status: 'ready', progress: 1},
          {id: 'active-job', platform: 'bilibili', status: 'downloading', progress: 0.2}
        ]
      };
      __test.batches['slow-batch'] = __test.created;
      const firstAssets = __test.deferAssets('slow-batch');
      document.querySelector('#inputs').value = 'https://youtu.be/synthetic-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      const unwantedRequests = [];
      // The initial asset response stays pending across three actual scheduled
      // poll callbacks. Any accidental extra fetch is held independently, so
      // resolving the first response cannot accidentally resolve its successors.
      for (let index = 0; index < 3; index++) {
        unwantedRequests.push(__test.deferAssets('slow-batch'));
        __test.batches['slow-batch'].jobs[1].progress = 0.3 + index / 10;
        __test.fireTimer(2000);
        await __test.turn();
      }
      const requestsWhileSlow = __test.requests.filter(request => request.url.endsWith('/assets')).length;
      const whilePending = snapshot();
      firstAssets.release({
        status: slowStatus,
        payload: slowStatus === 200 ? [asset('first-asset')] : {detail: 'synthetic slow failure'}
      });
      await __test.turn();
      const afterFirstResponse = snapshot();
      const refreshDisabled = document.querySelector('#refresh-assets').disabled;
      for (const gate of unwantedRequests) gate.release({status: 200, payload: []});
      await __test.turn();
      __test.assets['slow-batch'] = [asset('first-asset'), asset('later-asset')];
      __test.fireTimer(2000);
      await __test.turn();
      return {
        firstRequestEntered: firstAssets.entered,
        unwantedRequestStarted: unwantedRequests.some(gate => gate.entered),
        requestsWhileSlow, whilePending, afterFirstResponse, refreshDisabled,
        afterLaterPoll: snapshot(),
        totalAssetRequests: __test.requests.filter(request => request.url.endsWith('/assets')).length,
        actions: document.querySelector('#job-actions').textContent
      };
    """)

    assert rendered["firstRequestEntered"] is True
    assert rendered["whilePending"]["output"]["jobs"][1]["progress"] == 0.5
    assert rendered["requestsWhileSlow"] == 1
    assert rendered["unwantedRequestStarted"] is False
    assert rendered["refreshDisabled"] is False
    if slow_status == 200:
        assert [link["href"] for link in rendered["afterFirstResponse"]["links"]] == [
            "/api/v1/assets/first-asset/download"
        ]
    else:
        assert "成品列表读取失败" in rendered["afterFirstResponse"]["text"]
    assert [link["href"] for link in rendered["afterLaterPoll"]["links"]] == [
        "/api/v1/assets/first-asset/download",
        "/api/v1/assets/later-asset/download",
    ]
    assert rendered["totalAssetRequests"] == 2
    assert "取消 bilibili 任务" in rendered["actions"]


def test_reopening_same_batch_uses_new_generation_without_clearing_its_single_flight():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.batches['reopened-batch'] = {
        id: 'reopened-batch', status: 'queued', ready_count: 0, total_count: 1,
        jobs: [{id: 'active-job', platform: 'bilibili', status: 'downloading', progress: 0.2}]
      };
      __test.recent = [__test.batches['reopened-batch']];
      const oldAssets = __test.deferAssets('reopened-batch');
      await document.querySelector('#refresh-batches').dispatch('click');
      const open = document.querySelector('#recent-batches').children[0].children.find(child => child.tagName === 'button');
      await open.dispatch('click');
      await __test.turn();
      const newAssets = __test.deferAssets('reopened-batch');
      await open.dispatch('click');
      await __test.turn();
      const newRequestEntered = newAssets.entered;
      oldAssets.release({status: 200, payload: [asset('stale-asset')]});
      await __test.turn();
      const afterOldResponse = snapshot();
      __test.fireTimer(2000);
      await __test.turn();
      const requestCount = __test.requests.filter(request => request.url.endsWith('/assets')).length;
      newAssets.release({status: 200, payload: [asset('current-asset')]});
      await __test.turn();
      return {newRequestEntered, afterOldResponse, requestCount, completed: snapshot()};
    """)

    assert rendered["newRequestEntered"] is True
    assert rendered["afterOldResponse"]["links"] == []
    assert rendered["requestCount"] == 2
    assert [link["href"] for link in rendered["completed"]["links"]] == [
        "/api/v1/assets/current-asset/download"
    ]


@pytest.mark.parametrize("late_status", [200, 503])
@pytest.mark.parametrize("manual_refresh", [False, True])
def test_late_asset_response_cannot_replace_newly_opened_batch(late_status, manual_refresh):
    rendered = run_frontend(
        "const lateStatus = " + json.dumps(late_status) + ";\n"
        "const manualRefresh = " + json.dumps(manual_refresh) + ";\n" + ASSET_HELPERS + r"""
      __test.batches = {
        'old-batch': {id: 'old-batch', status: 'ready', jobs: [], ready_count: 1, total_count: 1},
        'new-batch': {id: 'new-batch', status: 'ready', jobs: [], ready_count: 1, total_count: 1}
      };
      __test.recent = Object.values(__test.batches);
      let oldAssets;
      if (manualRefresh) __test.assets['old-batch'] = [asset('old-initial-asset')];
      else oldAssets = __test.deferAssets('old-batch');
      __test.assets['new-batch'] = [asset('new-asset')];
      await document.querySelector('#refresh-batches').dispatch('click');
      const entries = document.querySelector('#recent-batches').children;
      await entries[0].children.find(child => child.tagName === 'button').dispatch('click');
      await __test.turn();
      let manualPending;
      if (manualRefresh) {
        oldAssets = __test.deferAssets('old-batch');
        manualPending = document.querySelector('#refresh-assets').dispatch('click');
        await __test.turn();
      }
      const oldRequestEntered = oldAssets.entered;
      await entries[1].children.find(child => child.tagName === 'button').dispatch('click');
      await __test.turn();
      const beforeRelease = snapshot();
      const newRefreshDisabled = document.querySelector('#refresh-assets').disabled;
      oldAssets.release({
        status: lateStatus,
        payload: lateStatus === 200 ? [asset('old-asset')] : {detail: 'synthetic late failure'}
      });
      if (manualPending) await manualPending;
      await __test.turn();
      return {oldRequestEntered, newRefreshDisabled, beforeRelease, afterRelease: snapshot()};
    """)

    assert rendered["oldRequestEntered"] is True
    assert rendered["newRefreshDisabled"] is False
    before = rendered["beforeRelease"]
    assert before["output"]["id"] == "new-batch"
    assert before["hidden"] is False
    assert [link["href"] for link in before["links"]] == [
        "/api/v1/assets/new-asset/download"
    ]
    assert rendered["afterRelease"] == before


def test_unchanged_poll_preserves_job_action_asset_nodes_and_action_focus():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'stable-batch', status: 'queued', ready_count: 1, total_count: 1,
        jobs: [
          {id: 'stable-job', platform: 'bilibili', status: 'downloading', progress: 0.4}
        ]
      };
      __test.batches['stable-batch'] = JSON.parse(JSON.stringify(__test.created));
      __test.assets['stable-batch'] = [asset('stable-asset')];
      document.querySelector('#inputs').value = 'https://youtu.be/stable-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();

      const progressItem = document.querySelector('#job-progress-list').children[0];
      const action = document.querySelector('#job-actions').children[0];
      const assetItem = document.querySelector('#asset-list').children[0];
      const downloadLink = assetItem.children[0];
      action.focus();

      __test.fireTimer(2000);
      await __test.turn();
      await __test.turn();

      return {
        progressSame: document.querySelector('#job-progress-list').children[0] === progressItem,
        actionSame: document.querySelector('#job-actions').children[0] === action,
        assetSame: document.querySelector('#asset-list').children[0] === assetItem,
        downloadLinkSame: document.querySelector('#asset-list').children[0].children[0] === downloadLink,
        focusSame: document.activeElement === action,
        assetRequests: __test.requests.filter(request => request.url.endsWith('/assets')).length,
        batchRequests: __test.requests.filter(request => request.url === '/api/v1/batches/stable-batch').length
      };
    """)

    assert rendered == {
        "progressSame": True,
        "actionSame": True,
        "assetSame": True,
        "downloadLinkSame": True,
        "focusSame": True,
        "assetRequests": 2,
        "batchRequests": 1,
    }


@pytest.mark.parametrize(
    ("status", "job_kind", "source_type", "operation"),
    [
        ("downloading", None, None, "cancel"),
        ("failed", "download", "bilibili_video", "retry"),
    ],
)
def test_rapid_duplicate_job_action_sends_one_post_while_in_flight(
    status, job_kind, source_type, operation
):
    config = json.dumps(
        {
            "status": status,
            "jobKind": job_kind,
            "sourceType": source_type,
            "operation": operation,
        }
    )
    rendered = run_frontend(
        "const config = " + config + ";\n" + ASSET_HELPERS + r"""
      const job = {
        id: 'guarded-job', platform: 'bilibili', status: config.status,
        progress: config.status === 'failed' ? 0 : 0.4,
        job_kind: config.jobKind, source_type: config.sourceType
      };
      __test.created = {
        id: 'guarded-batch', status: config.status, ready_count: 0,
        total_count: 1, jobs: [job]
      };
      __test.batches['guarded-batch'] = JSON.parse(JSON.stringify(__test.created));
      __test.assets['guarded-batch'] = [];
      document.querySelector('#inputs').value = 'https://youtu.be/guarded-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();

      const action = document.querySelector('#job-actions').children[0];
      const mutationUrl = '/api/v1/jobs/guarded-job/' + config.operation;
      const gate = __test.deferMutation(mutationUrl);
      await Promise.all([action.dispatch('click'), action.dispatch('click')]);
      await __test.turn();

      const sameWhilePending = document.querySelector('#job-actions').children[0] === action;
      renderPayload(__test.created);
      await __test.turn();
      await action.dispatch('click');
      await __test.turn();
      const postsWhilePending = __test.requests.filter(
        request => request.url === mutationUrl && request.options.method === 'POST'
      ).length;
      const disabledWhilePending = action.disabled;

      gate.release({status: 200, payload: {state: 'synthetic-complete'}});
      await __test.turn();
      await __test.turn();
      await __test.turn();

      return {
        entered: gate.entered,
        sameWhilePending,
        disabledWhilePending,
        postsWhilePending,
        totalPosts: __test.requests.filter(
          request => request.url === mutationUrl && request.options.method === 'POST'
        ).length,
        actionStillConnected: action.isConnected,
        actionSameAfter: document.querySelector('#job-actions').children[0] === action,
        disabledAfter: action.disabled
      };
    """
    )

    assert rendered["entered"] is True
    assert rendered["sameWhilePending"] is True
    assert rendered["disabledWhilePending"] is True
    assert rendered["postsWhilePending"] == 1
    assert rendered["totalPosts"] == 1
    assert rendered["actionStillConnected"] is True
    assert rendered["actionSameAfter"] is True
    assert rendered["disabledAfter"] is False


def test_ime_submission_and_inline_validation_do_not_create_a_batch():
    rendered = run_frontend(r"""
      const inputs = document.querySelector('#inputs');
      const form = document.querySelector('#batch-form');
      inputs.value = 'https://youtu.be/composing-input';
      const keyEvent = await inputs.dispatch('keydown', {isComposing: true, key: 'Enter'});
      await inputs.dispatch('compositionstart');
      const composingSubmit = await form.dispatch('submit', {isComposing: false});
      await __test.turn();
      const postsWhileComposing = __test.requests.filter(
        request => request.url === '/api/v1/batches' && request.options.method === 'POST'
      ).length;

      await inputs.dispatch('compositionend');
      const legacyImeKey = await inputs.dispatch(
        'keydown', {isComposing: false, key: 'Enter', keyCode: 229}
      );
      if (!legacyImeKey.defaultPrevented) await form.dispatch('submit');
      await __test.turn();
      const postsAfterLegacyIme = __test.requests.filter(
        request => request.url === '/api/v1/batches' && request.options.method === 'POST'
      ).length;
      inputs.value = '';
      const invalidSubmit = await form.dispatch('submit');
      const error = document.querySelector('#batch-error');
      const invalidState = {
        visible: !error.hidden,
        text: error.textContent,
        ariaInvalid: inputs.attributes['aria-invalid'],
        focused: document.activeElement === inputs,
        submitPrevented: invalidSubmit.defaultPrevented
      };
      inputs.value = 'https://youtu.be/valid-later';
      await inputs.dispatch('input');

      return {
        keyPrevented: keyEvent.defaultPrevented,
        legacyImeKeyPrevented: legacyImeKey.defaultPrevented,
        composingSubmitPrevented: composingSubmit.defaultPrevented,
        postsWhileComposing,
        postsAfterLegacyIme,
        invalidState,
        cleared: error.hidden && !Object.hasOwn(inputs.attributes, 'aria-invalid'),
        totalPosts: __test.requests.filter(
          request => request.url === '/api/v1/batches' && request.options.method === 'POST'
        ).length
      };
    """)

    assert rendered["keyPrevented"] is True
    assert rendered["legacyImeKeyPrevented"] is True
    assert rendered["composingSubmitPrevented"] is True
    assert rendered["postsWhileComposing"] == 0
    assert rendered["postsAfterLegacyIme"] == 0
    assert rendered["invalidState"] == {
        "visible": True,
        "text": "请输入 URL，或选择 TXT/CSV 文件",
        "ariaInvalid": "true",
        "focused": True,
        "submitPrevented": True,
    }
    assert rendered["cleared"] is True
    assert rendered["totalPosts"] == 0


def test_current_batch_failures_remain_visible_outside_raw_details():
    rendered = run_frontend(r"""
      await openBatch('missing-synthetic-batch');
      const visible = document.querySelector('#current-batch-error');
      return {
        resultVisible: !document.querySelector('#result').hidden,
        alertVisible: !visible.hidden,
        alertText: visible.textContent,
        rawText: document.querySelector('#output').textContent
      };
    """)

    assert rendered["resultVisible"] is True
    assert rendered["alertVisible"] is True
    assert "打开批次失败" in rendered["alertText"]
    assert rendered["rawText"] == rendered["alertText"]


def test_bfcache_page_lifecycle_suspends_and_resumes_current_batch_polling():
    rendered = run_frontend(ASSET_HELPERS + r"""
      __test.created = {
        id: 'lifecycle-batch', status: 'queued', ready_count: 0, total_count: 1,
        jobs: [
          {id: 'lifecycle-job', platform: 'bilibili', status: 'downloading', progress: 0.3}
        ]
      };
      __test.batches['lifecycle-batch'] = JSON.parse(JSON.stringify(__test.created));
      __test.assets['lifecycle-batch'] = [];
      document.querySelector('#inputs').value = 'https://youtu.be/lifecycle-input';
      await document.querySelector('#batch-form').dispatch('submit');
      await __test.turn();
      const action = document.querySelector('#job-actions').children[0];
      action.focus();

      await __test.dispatchWindow('pagehide');
      const pollScheduledWhileHidden = __test.pendingTimerDelays().includes(2000);
      const beforeResume = __test.requests.filter(
        request => request.url === '/api/v1/batches/lifecycle-batch'
      ).length;
      await __test.dispatchWindow('pageshow', {persisted: false});
      await __test.turn();
      const afterOrdinaryShow = __test.requests.filter(
        request => request.url === '/api/v1/batches/lifecycle-batch'
      ).length;

      await __test.dispatchWindow('pageshow', {persisted: true});
      await __test.turn();
      await __test.turn();
      const afterRestore = __test.requests.filter(
        request => request.url === '/api/v1/batches/lifecycle-batch'
      ).length;
      return {
        pollScheduledWhileHidden,
        beforeResume,
        afterOrdinaryShow,
        afterRestore,
        pollRescheduled: __test.pendingTimerDelays().includes(2000),
        actionSame: document.querySelector('#job-actions').children[0] === action,
        focusSame: document.activeElement === action
      };
    """)

    assert rendered == {
        "pollScheduledWhileHidden": False,
        "beforeResume": 0,
        "afterOrdinaryShow": 0,
        "afterRestore": 1,
        "pollRescheduled": True,
        "actionSame": True,
        "focusSame": True,
    }
