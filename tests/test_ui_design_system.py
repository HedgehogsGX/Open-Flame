from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from video_download_control.api import create_app
from video_download_control.editing.web import EDITING_HTML
from video_download_control.ui_assets import page_content_security_policy, ui_asset
from video_download_control.uploads.web import UPLOAD_HTML
from video_download_control.workflows.web import WORKFLOW_HTML
from video_download_control.web import INDEX_HTML


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def matching(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for element, attrs in self.elements if element == tag]


def _classes(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("open-flame.css", "text/css; charset=utf-8"),
        ("open-flame-shell.js", "text/javascript; charset=utf-8"),
    ],
)
def test_ui_asset_loads_only_nonempty_allowlisted_package_assets(
    name: str, media_type: str
) -> None:
    payload, actual_media_type = ui_asset(name)

    assert actual_media_type == media_type
    assert isinstance(payload, bytes)
    assert payload.strip()
    assert payload.decode("utf-8").strip()


@pytest.mark.parametrize(
    "name",
    [
        "../open-flame.css",
        "OPEN-FLAME.CSS",
        "missing.css",
        "",
    ],
)
def test_ui_asset_rejects_names_outside_the_fixed_allowlist(name: str) -> None:
    with pytest.raises(ValueError, match="^unknown_ui_asset$"):
        ui_asset(name)


@pytest.mark.parametrize(
    ("page", "active_path", "body_class"),
    [
        (INDEX_HTML, "/", "download-page"),
        (EDITING_HTML, "/edits", "editing-page"),
        (UPLOAD_HTML, "/uploads", "upload-page"),
        (WORKFLOW_HTML, "/workflows", "workflow-page"),
    ],
    ids=("download", "editing", "upload", "workflow"),
)
def test_pages_share_local_shell_navigation_and_theme_controls(
    page: str, active_path: str, body_class: str
) -> None:
    parser = _PageParser()
    parser.feed(page)

    html = parser.matching("html")
    assert len(html) == 1
    assert html[0].get("data-theme") == "system"
    assert "no-js" in _classes(html[0])

    body = parser.matching("body")
    assert len(body) == 1
    assert body_class in _classes(body[0])

    stylesheets = [
        attrs
        for attrs in parser.matching("link")
        if "stylesheet" in (attrs.get("rel") or "").split()
    ]
    assert [attrs.get("href") for attrs in stylesheets] == [
        "/assets/open-flame.css"
    ]

    external_scripts = [
        attrs for attrs in parser.matching("script") if attrs.get("src")
    ]
    assert [attrs.get("src") for attrs in external_scripts] == [
        "/assets/open-flame-shell.js"
    ]
    assert "defer" in external_scripts[0]
    assert parser.matching("style") == []

    navigation = [
        attrs
        for attrs in parser.matching("nav")
        if "primary-nav" in _classes(attrs)
    ]
    assert len(navigation) == 1
    assert navigation[0].get("aria-label")

    nav_links = [
        attrs
        for attrs in parser.matching("a")
        if "nav-link" in _classes(attrs)
    ]
    assert {attrs.get("href") for attrs in nav_links} == {"/", "/edits", "/uploads", "/workflows"}
    current = [attrs for attrs in nav_links if attrs.get("aria-current") == "page"]
    assert len(current) == 1
    assert current[0].get("href") == active_path

    theme_controls = [
        attrs
        for attrs in parser.matching("select")
        if "data-of-theme" in attrs
    ]
    assert len(theme_controls) == 1
    assert theme_controls[0].get("aria-label")
    assert {attrs.get("value") for attrs in parser.matching("option")} >= {
        "system",
        "light",
        "dark",
    }

    resource_locations = [
        value
        for tag in ("link", "script", "img", "source")
        for attrs in parser.matching(tag)
        for attribute in ("href", "src", "srcset")
        if (value := attrs.get(attribute))
    ]
    assert not any(
        re.match(r"^(?:https?:)?//", location, flags=re.IGNORECASE)
        for location in resource_locations
    )


def test_http_assets_preserve_content_headers_and_upload_laziness(settings) -> None:
    app = create_app(settings)
    factory_calls = []

    def fail_if_uploads_initialize(root):
        factory_calls.append(root)
        pytest.fail("shared UI asset initialized the upload manager")

    app.state.upload_service_factory = fail_if_uploads_initialize
    expected = {
        "/assets/open-flame.css": ui_asset("open-flame.css"),
        "/assets/open-flame-shell.js": ui_asset("open-flame-shell.js"),
    }

    with TestClient(app, base_url="http://127.0.0.1") as client:
        for path, (payload, media_type) in expected.items():
            response = client.get(path)
            assert response.status_code == 200
            assert response.content == payload
            assert response.headers["content-type"] == media_type
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-content-type-options"] == "nosniff"
        assert app.state.upload_manager.service is None

        openapi_paths = client.get("/openapi.json").json()["paths"]
        assert not set(expected).intersection(openapi_paths)

    assert factory_calls == []


@pytest.mark.parametrize(
    ("path", "page"),
    [("/", INDEX_HTML), ("/edits", EDITING_HTML), ("/uploads", UPLOAD_HTML),
     ("/workflows", WORKFLOW_HTML)],
    ids=("download", "editing", "upload", "workflow"),
)
def test_html_pages_bind_inline_business_script_with_self_only_csp(
    settings, path: str, page: str
) -> None:
    app = create_app(settings)
    app.state.upload_service_factory = lambda root: pytest.fail(
        "HTML rendering initialized the upload manager"
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(path)

    policy = response.headers["content-security-policy"]
    assert response.status_code == 200
    assert policy == page_content_security_policy(page)
    assert "default-src 'none'" in policy
    assert "script-src 'self' 'sha256-" in policy
    assert "style-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert "img-src 'self' blob:" in policy
    assert "'unsafe-inline'" not in policy
    assert "http:" not in policy and "https:" not in policy


def test_shared_stylesheet_encodes_the_accessible_responsive_contract() -> None:
    payload, _ = ui_asset("open-flame.css")
    css = payload.decode("utf-8")

    for token in (
        "--of-canvas",
        "--of-surface",
        "--of-text",
        "--of-text-secondary",
        "--of-border",
        "--of-accent",
        "--of-focus",
        "--of-fast: 120ms",
        "--of-medium: 180ms",
        "--of-slow: 240ms",
    ):
        assert token in css

    assert re.search(r":root\s*\{[^{}]*color-scheme:\s*light", css, re.DOTALL)
    assert re.search(
        r':root\[data-theme=["\']dark["\']\]\s*\{'
        r"[^{}]*color-scheme:\s*dark",
        css,
        re.DOTALL,
    )
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root[data-theme="system"]' in css

    assert re.search(r":focus-visible\s*\{[^{}]*outline:", css, re.DOTALL)
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-reduced-transparency: reduce)" in css
    assert "@media (prefers-contrast: more)" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (hover: hover) and (pointer: fine)" in css

    for maximum_width in (1023, 767, 520, 359):
        assert re.search(
            rf"@media\s*\(max-width:\s*{maximum_width}px\)", css
        )

    assert re.search(
        r"button,\s*\.button-link\s*\{[^{}]*min-height:\s*44px",
        css,
        re.DOTALL,
    )
    assert re.search(
        r'input:not\(\[type="checkbox"\]\):not\(\[type="radio"\]\),'
        r"\s*select\s*\{[^{}]*min-height:\s*44px",
        css,
        re.DOTALL,
    )
    assert re.search(r"\.choice\s*\{[^{}]*min-height:\s*44px", css, re.DOTALL)
    assert re.search(r"summary\s*\{[^{}]*min-height:\s*44px", css, re.DOTALL)
    assert re.search(
        r"\.theme-control select\s*\{[^{}]*min-height:\s*44px",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"\.nav-link\s*\{[^{}]*min-height:\s*44px",
        css,
        re.DOTALL,
    )

    assert not re.search(r"transition\s*:\s*all(?:\s|;)", css, re.IGNORECASE)
    assert "button:active" not in css
    assert "details[open] > :not(summary)" not in css
    assert not re.search(r"@import\b", css, re.IGNORECASE)
    assert not re.search(
        r"url\(\s*[\"\']?(?:https?:)?//", css, re.IGNORECASE
    )


def test_shared_shell_script_only_manages_local_theme_state() -> None:
    payload, _ = ui_asset("open-flame-shell.js")
    script = payload.decode("utf-8")

    assert "open-flame-theme" in script
    assert "data-of-theme" in script
    assert {f'"{theme}"' for theme in ("system", "light", "dark")} <= set(
        re.findall(r'"[a-z]+"', script)
    )
    assert re.search(r"addEventListener\([\"']storage[\"']", script)
    assert "localStorage.getItem" in script
    assert "localStorage.setItem" in script
    assert 'addEventListener("pointerdown"' in script
    assert "dataset.pointerActive" in script
    assert script.count("try {") >= 2
    assert not re.search(r"\b(?:fetch|XMLHttpRequest|WebSocket)\b", script)
    assert not re.search(r'''["'](?://|https?://)''', script, re.IGNORECASE)


def test_shared_shell_applies_syncs_and_safely_falls_back_when_storage_is_blocked() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for executable theme-shell tests")
    script = ui_asset("open-flame-shell.js")[0].decode("utf-8")
    harness = r"""
const vm = require('node:vm');
const production = JSON.parse(process.argv[1]);

function run(blocked) {
  const controls = [0, 1].map(() => ({
    value: '', listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }));
  let stored = 'dark';
  let writes = 0;
  let storageListener = null;
  const root = {
    dataset: {},
    classList: {removed: [], remove(value) { this.removed.push(value); }}
  };
  const context = vm.createContext({
    Set,
    document: {
      documentElement: root,
      readyState: 'complete',
      querySelectorAll(selector) {
        if (selector !== '[data-of-theme]') throw new Error('unexpected selector');
        return controls;
      }
    },
    localStorage: {
      getItem() { if (blocked) throw new Error('blocked'); return stored; },
      setItem(_key, value) { if (blocked) throw new Error('blocked'); stored = value; writes += 1; }
    },
    window: {
      addEventListener(name, callback) {
        if (name === 'storage') storageListener = callback;
      }
    }
  });
  vm.runInContext(production, context, {timeout: 1000});
  const initial = {theme: root.dataset.theme, values: controls.map(item => item.value)};
  controls[0].value = 'light';
  controls[0].listeners.change();
  const changed = {theme: root.dataset.theme, values: controls.map(item => item.value), stored, writes};
  storageListener({key: 'open-flame-theme', newValue: 'dark'});
  return {
    initial, changed,
    synchronized: {theme: root.dataset.theme, values: controls.map(item => item.value), writes},
    removed: root.classList.removed
  };
}
process.stdout.write(JSON.stringify({normal: run(false), blocked: run(true)}));
"""
    completed = subprocess.run(
        [node, "-e", harness, json.dumps(script)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["normal"]["initial"] == {
        "theme": "dark",
        "values": ["dark", "dark"],
    }
    assert result["normal"]["changed"] == {
        "theme": "light",
        "values": ["light", "light"],
        "stored": "light",
        "writes": 1,
    }
    assert result["normal"]["synchronized"] == {
        "theme": "dark",
        "values": ["dark", "dark"],
        "writes": 1,
    }
    assert result["blocked"]["initial"] == {
        "theme": "system",
        "values": ["system", "system"],
    }
    assert result["blocked"]["changed"]["theme"] == "light"
    assert result["normal"]["removed"] == ["no-js"]
    assert result["blocked"]["removed"] == ["no-js"]


def test_editing_polling_preserves_record_dom_focus_and_upload_allowlist() -> None:
    assert "setInterval(" not in EDITING_HTML
    assert "if(recordsPromise)return recordsPromise" in EDITING_HTML
    assert "if(pollPromise)return pollPromise" in EDITING_HTML
    assert "setTimeout(poll,2000)" in EDITING_HTML

    assert (
        "capturedGeneration=projectSelectionGeneration,capturedProjectId="
        "currentProjectId()" in EDITING_HTML
    )
    assert (
        "capturedGeneration!==projectSelectionGeneration||capturedProjectId!=="
        "currentProjectId()" in EDITING_HTML
    )
    for record_kind in ("projects", "plans", "outputs"):
        assert f"renderIfChanged('{record_kind}'" in EDITING_HTML

    assert "document.activeElement?.dataset?.focusKey" in EDITING_HTML
    assert "querySelectorAll('[data-focus-key]')" in EDITING_HTML
    for stable_key in (
        "'project:'+id+':open'",
        "key+'confirm'",
        "key+'cancel'",
        "key+'retry'",
        "key+'download'",
        "key+'upload'",
    ):
        assert stable_key in EDITING_HTML

    assert "if(['segment','dubbed_video'].includes(kind))" in EDITING_HTML
    assert "if(kind!=='cover')" not in EDITING_HTML
