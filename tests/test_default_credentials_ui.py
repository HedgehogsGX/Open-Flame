from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from video_download_control.web import INDEX_HTML


class PageFixtureParser(HTMLParser):
    """Keep real page IDs, select defaults and the complete unmodified script."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

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
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._textContent = String(value);
  }
  get textContent() { return this._textContent + this.children.map(child => child.textContent).join(''); }
  get isConnected() { return this.tagName === 'document' || !!this.parentElement?.isConnected; }
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
    const index = this.parentElement.children.indexOf(this);
    if (index >= 0) this.parentElement.children.splice(index, 1);
    this.parentElement = null;
  }
  replaceChildren(...children) { this.textContent = ''; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) {
    const handlers = this.listeners.get(name) || [];
    handlers.push(callback);
    this.listeners.set(name, handlers);
  }
  async dispatch(name) {
    const event = { target: this, currentTarget: this, preventDefault() {} };
    for (const callback of this.listeners.get(name) || []) await callback(event);
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
  querySelector(selector) {
    if (!selector.startsWith('#')) throw new Error('unsupported test selector: ' + selector);
    return ids.get(selector.slice(1)) || null;
  },
  createElement: tag => new Element(tag)
};
const state = {
  requests: [],
  credentialReply: { ok: true, status: 200, payload: { available: true, platforms: [] } },
  createdPayload: { id: 'synthetic-batch', status: 'failed', jobs: [] },
  batchPayload: { id: 'synthetic-batch', status: 'failed', jobs: [] }
};
async function fetch(url, options = {}) {
  state.requests.push({url: String(url), options});
  if (url === '/api/v1/session') return {ok: true, status: 200, json: async () => ({csrf_token: 'synthetic-download-nonce'})};
  if (url === '/api/v1/credential-defaults') {
    if (state.credentialReply.error) throw new Error(state.credentialReply.error);
    const reply = state.credentialReply;
    return {ok: reply.ok, status: reply.status, json: async () => reply.payload};
  }
  let payload;
  if (url === '/api/v1/operations/queue') payload = {paused: false};
  else if (url === '/api/v1/platform-circuits') payload = [];
  else if (url === '/api/v1/capability-snapshot') payload = {implementations: [], evidence: [], decisions: []};
  else if (url === '/api/v1/operations/tools') payload = {state: 'unconfigured', security_note: 'synthetic tools'};
  else if (url.startsWith('/api/v1/operations/logs')) payload = {status: 'ok', events: [], write_failures: 0, rejected_events: 0, last_failure_code: null, run_id: 'synthetic-run'};
  else if (url.startsWith('/api/v1/batches?')) payload = [];
  else if (options.method === 'POST' && (url === '/api/v1/batches' || url.startsWith('/api/v1/batches/import?'))) payload = state.createdPayload;
  else if (options.method === 'POST' && url.endsWith('/retry')) payload = {};
  else if (url.startsWith('/api/v1/batches/')) payload = state.batchPayload;
  else throw new Error('unexpected fetch: ' + url);
  return {ok: true, status: 200, json: async () => payload};
}
let timerId = 0;
const context = vm.createContext({
  document, fetch, Headers, URLSearchParams, console,
  setTimeout: () => ++timerId,
  clearTimeout: () => {},
  __test: state
});
(async () => {
  vm.runInContext(fixture.script, context, {timeout: 2000});
  // One actual event-loop turn drains startup fetch microtasks; no sleeps or
  // production function replacements are used to suppress initialization.
  await new Promise(resolve => setImmediate(resolve));
  const result = await vm.runInContext('(async () => {' + fixture.exercise + '})()', context, {timeout: 2000});
  await new Promise(resolve => setImmediate(resolve));
  process.stdout.write(JSON.stringify(result));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def run_frontend(exercise: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for executable default-Cookie UI tests")
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


def test_complete_frontend_renders_cookie_available_unavailable_and_unknown_states():
    rendered = run_frontend(r"""
      const status = document.querySelector('#credential-status');
      const mode = document.querySelector('#credential-mode');
      const refresh = document.querySelector('#refresh-credentials');
      const snapshot = () => ({text: status.textContent, className: status.className, mode: mode.value});
      const anonymousDefault = snapshot();
      __test.credentialReply = {ok: true, status: 200, payload: {available: true, platforms: ['douyin', 'tiktok']}};
      await refresh.dispatch('click');
      const available = snapshot();
      __test.credentialReply = {ok: true, status: 200, payload: {available: false, platforms: []}};
      await refresh.dispatch('click');
      const unavailable = snapshot();
      __test.credentialReply = {error: 'synthetic connection unavailable'};
      await refresh.dispatch('click');
      const unknown = snapshot();
      return {anonymousDefault, available, unavailable, unknown};
    """)

    assert rendered["anonymousDefault"] == {
        "text": "本次启动未配置默认 Cookie；新任务将匿名下载。",
        "className": "muted",
        "mode": "use_default",
    }
    assert rendered["available"] == {
        "text": "本次启动已配置默认 Cookie：douyin、tiktok。文件可用不代表平台登录仍有效。",
        "className": "muted",
        "mode": "use_default",
    }
    assert rendered["unavailable"]["className"] == "danger"
    assert "配置已失效" in rendered["unavailable"]["text"]
    assert "明确选择匿名下载" in rendered["unavailable"]["text"]
    assert rendered["unknown"]["className"] == "danger"
    assert rendered["unknown"]["text"].startswith("Cookie 状态未知：")
    assert "synthetic connection unavailable" in rendered["unknown"]["text"]
    assert rendered["unavailable"]["mode"] == rendered["unknown"]["mode"] == "use_default"


@pytest.mark.parametrize("mode", ["use_default", "anonymous"])
@pytest.mark.parametrize("entrypoint", ["json", "txt", "csv"])
def test_complete_frontend_submits_explicit_cookie_mode_for_json_and_file_imports(mode, entrypoint):
    submitted = run_frontend(
        "const mode = " + json.dumps(mode) + ";\n"
        "const entrypoint = " + json.dumps(entrypoint) + ";\n" + r"""
      document.querySelector('#credential-mode').value = mode;
      document.querySelector('#name').value = 'synthetic named batch';
      const file = {name: 'synthetic.' + entrypoint, syntheticBytes: 'url fixture'};
      if (entrypoint === 'json') {
        document.querySelector('#inputs').value = 'https://www.douyin.com/video/1234567890';
      } else {
        document.querySelector('#inputs').value = '';
        document.querySelector('#import-file').files = [file];
      }
      await document.querySelector('#batch-form').dispatch('submit');
      const request = __test.requests.find(item => item.options.method === 'POST');
      const query = new URLSearchParams(request.url.split('?')[1] || '');
      return {
        url: request.url.split('?')[0], method: request.options.method,
        contentType: request.options.headers.get('Content-Type'),
        jsonBody: entrypoint === 'json' ? JSON.parse(request.options.body) : null,
        query: Object.fromEntries(query),
        passesOriginalFile: entrypoint !== 'json' && request.options.body === file,
        buttonDisabled: document.querySelector('#submit').disabled,
        output: document.querySelector('#output').textContent
      };
    """
    )

    assert submitted["method"] == "POST"
    assert submitted["buttonDisabled"] is False
    assert json.loads(submitted["output"])["id"] == "synthetic-batch"
    if entrypoint == "json":
        assert submitted["url"] == "/api/v1/batches"
        assert submitted["contentType"] == "application/json"
        assert submitted["jsonBody"] == {
            "name": "synthetic named batch",
            "inputs": ["https://www.douyin.com/video/1234567890"],
            "credential_mode": mode,
        }
    else:
        assert submitted["url"] == "/api/v1/batches/import"
        assert submitted["contentType"] == ("text/csv" if entrypoint == "csv" else "text/plain")
        assert submitted["query"] == {
            "filename": f"synthetic.{entrypoint}",
            "name": "synthetic named batch",
            "credential_mode": mode,
        }
        assert submitted["passesOriginalFile"] is True


@pytest.mark.parametrize("mode", ["use_default", "anonymous"])
def test_complete_frontend_retry_reads_explicit_cookie_mode_at_click_time(mode):
    retried = run_frontend("const chosenMode = " + json.dumps(mode) + ";\n" + r"""
      const mode = document.querySelector('#credential-mode');
      mode.value = chosenMode === 'anonymous' ? 'use_default' : 'anonymous';
      const payload = {
        id: 'synthetic-batch', status: 'failed',
        jobs: [{id: 'synthetic-job', platform: 'douyin', status: 'failed',
                job_kind: 'download', source_type: 'douyin_video', progress: 0}]
      };
      renderPayload(payload);
      const retry = document.querySelector('#job-actions').children[0];
      const label = retry.textContent;
      mode.value = chosenMode;
      await retry.dispatch('click');
      const request = __test.requests.find(item => item.url.endsWith('/retry'));
      return {label, url: request.url, method: request.options.method,
              contentType: request.options.headers.get('Content-Type'),
              body: JSON.parse(request.options.body)};
    """)

    assert retried == {
        "label": "重试 douyin 任务（新一代）",
        "url": "/api/v1/jobs/synthetic-job/retry",
        "method": "POST",
        "contentType": "application/json",
        "body": {"credential_mode": mode},
    }
