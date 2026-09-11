from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.domain import JobStatus


def test_get_batch_exposes_exact_downloading_and_postprocessing_progress(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/progress-api-contract"]},
        ).json()
        claim_time = datetime.fromisoformat(created["created_at"]) + timedelta(
            seconds=1
        )
        lease = app.state.worker_repository.claim_next(
            worker_id="progress-api-test-worker",
            adapter="fake",
            adapter_version="test",
            now=claim_time,
        )
        assert lease is not None

        app.state.worker_repository.transition(
            lease,
            status=JobStatus.DOWNLOADING,
            progress=0.24,
            now=claim_time + timedelta(seconds=1),
        )
        downloading_response = client.get(f"/api/v1/batches/{created['id']}")

        app.state.worker_repository.transition(
            lease,
            status=JobStatus.POSTPROCESSING,
            progress=0.79,
            now=claim_time + timedelta(seconds=2),
        )
        postprocessing_response = client.get(f"/api/v1/batches/{created['id']}")

    assert downloading_response.status_code == 200
    downloading_job = downloading_response.json()["jobs"][0]
    assert downloading_job["status"] == "downloading"
    assert downloading_job["progress"] == 0.24

    assert postprocessing_response.status_code == 200
    postprocessing_job = postprocessing_response.json()["jobs"][0]
    assert postprocessing_job["status"] == "postprocessing"
    assert postprocessing_job["progress"] == 0.79


def test_frontend_executes_real_progress_renderer_for_active_phases(
    settings: Settings,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the executable frontend contract test")

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        page = client.get("/")

    assert page.status_code == 200
    helper_start = page.text.index("function reconcileKeyed(parent")
    helper_end = page.text.index("function setListMessage(parent", helper_start)
    helper = page.text[helper_start:helper_end]
    function_start = page.text.index("function renderJobProgress(payload)")
    function_end = page.text.index(
        "async function loadReadyAssets(payload, generation)", function_start
    )
    renderer = page.text[function_start:function_end]

    harness = r"""
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.className = '';
    this.hidden = false;
    this.max = undefined;
    this.value = undefined;
    this._textContent = '';
  }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }
  get textContent() {
    return this._textContent + this.children.map(child => child.textContent).join('');
  }
      append(...children) {
    for (const child of children) {
      if (typeof child === 'string') {
        const textNode = new Element('#text');
        textNode.textContent = child;
        this.children.push(textNode);
          } else {
            child.parentElement = this;
            this.children.push(child);
          }
        }
      }
      insertBefore(child, reference) {
        const oldIndex = this.children.indexOf(child);
        if (oldIndex >= 0) this.children.splice(oldIndex, 1);
        const index = reference === null ? this.children.length : this.children.indexOf(reference);
        child.parentElement = this;
        this.children.splice(index < 0 ? this.children.length : index, 0, child);
      }
      remove() {
        if (!this.parentElement) return;
        const index = this.parentElement.children.indexOf(this);
        if (index >= 0) this.parentElement.children.splice(index, 1);
        this.parentElement = null;
      }
  replaceChildren(...children) {
    this._textContent = '';
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
}

const document = { createElement: tagName => new Element(tagName) };
const jobProgress = new Element('section');
const jobProgressList = new Element('div');

function snapshot() {
  const item = jobProgressList.children[0];
  const label = item.children[0];
  const progress = item.children[1];
  return {
    hidden: jobProgress.hidden,
    statusText: label.children[0].textContent,
    valueText: label.children[1].textContent,
    combinedText: item.textContent,
    progressMax: progress.max,
    progressValue: progress.value,
    ariaLabel: progress.attributes['aria-label']
  };
}
"""
    exercise = r"""
renderJobProgress({jobs: [{platform: 'youtube', status: 'downloading', progress: 0.24}]});
const downloading = snapshot();
renderJobProgress({jobs: [{platform: 'youtube', status: 'postprocessing', progress: 0.79}]});
const postprocessing = snapshot();
process.stdout.write(JSON.stringify({downloading, postprocessing}));
"""
    completed = subprocess.run(
        [node],
        input=harness + helper + renderer + exercise,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(completed.stdout)
    assert rendered["downloading"] == {
        "hidden": False,
        "statusText": "youtube · 正在下载",
        "valueText": "约 24%",
        "combinedText": "youtube · 正在下载约 24%",
        "progressMax": 100,
        "progressValue": 24,
        "ariaLabel": "youtube 正在下载",
    }
    assert rendered["postprocessing"] == {
        "hidden": False,
        "statusText": "youtube · 正在合并/后处理",
        "valueText": "约 79%",
        "combinedText": "youtube · 正在合并/后处理约 79%",
        "progressMax": 100,
        "progressValue": 79,
        "ariaLabel": "youtube 正在合并/后处理",
    }
