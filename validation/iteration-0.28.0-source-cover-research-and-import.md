# Iteration 0.28.0 来源封面调研与导入

Date: 2026-09-10
Scope: official-repository research, existing thumbnail boundary audit, and explicit downloaded-cover import; current checkout only

## Result

The selected implementation reuses the already pinned yt-dlp `2026.08.19`
runtime and Open-Flame's existing registered thumbnail artifacts. It does not
add another downloader, scrape a thumbnail URL in the browser, or make an
unregistered remote image part of the Upload domain.

The user-facing term is **来源封面（平台返回）**: the image bytes returned by the
platform thumbnail URL through the pinned extractor. This is not described as
the creator's lossless master image. Bilibili exposes its normal video cover as
`videoData.pic`; Douyin exposes several cover variants including
`origin_cover`, but yt-dlp does not publish a stable promise that its selected
`thumbnail` is always that exact variant.

The current Download pipeline already asks yt-dlp to write one thumbnail using
the dedicated `thumbnail:` output template, associates the result with the
corresponding original media key, verifies and publishes it as a registered
auxiliary artifact, and exposes it through the bounded artifact API. This slice
uses the dedicated
`POST /api/v1/uploads/covers/download-artifacts/{artifact_id}` route to resolve
only a ready registered thumbnail, recheck its managed identity and SHA-256,
then create an independent managed cover copy. The implementation is therefore
limited to the smallest product bridge needed to let an operator invoke that
import and then select the resulting cover; it does not duplicate extraction or
storage.

No tracked automated test file is added or modified.

## Official-source candidates

All repository observations below are fixed to the reviewed revision rather
than inferred from a project name, search result or old release description.

| Candidate | Reviewed revision | Observed cover boundary | License and decision |
| --- | --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp/tree/2026.08.19) | tag `2026.08.19` | Dedicated Bilibili and Douyin extractors; `--write-thumbnail`, `--write-all-thumbnails`, separate `thumbnail:` output templates and Python `extract_info(..., download=False)` are documented | Upstream source is Unlicense. The selected platform-independent zipimport also contains ISC `meriyah` and MIT `astring`; this exact runtime is already pinned and recorded by Open-Flame. **Selected.** |
| [you-get](https://github.com/soimort/you-get/tree/049548f3f3f35e67ba8d3181c71fdc71d11cf260) | `049548f3` | Bilibili and Douyin downloaders exist, but the reviewed platform extractors do not return a reusable video-cover artifact; the Douyin path uses an older API shape | MIT is permissive, but this adds a second Python downloader without filling the product gap. Rejected. |
| [lux](https://github.com/iawia002/lux/tree/dd00f6d258d80b6684a0b9402d7124e5c18ef42f) | `dd00f6d2` | Its Douyin response structs contain `cover`, `cover_original_scale`, `dynamic_cover` and `origin_cover`, but the public `Data` returned by the extractor discards them; Bilibili has no equivalent cover result | MIT is permissive, but adopting a Go runtime still requires a custom cover contract. Rejected. |
| [BBDown](https://github.com/nilaoda/BBDown/tree/1b2fbd4372d0b9840d28072f7914ae9887508e5d) | `1b2fbd43` | Bilibili-only project; the current repository states that it is archived and no longer maintained | MIT, but no maintained cross-platform cover path. Rejected. |
| [weixin-articles-mcp](https://github.com/jj-cheng25/weixin-articles-mcp/tree/060fb3dd7e41d1c0950a19bc1367d66a6881f915) | `060fb3dd` | For a Channels item embedded in a public `mp.weixin.qq.com` article, calls the undocumented `batch_get_video_snap` endpoint and prefers `feed_full_cover_url`, falling back to `feed_cover_url` | MIT. Useful as a qualified reference for an article-embed adapter, but it does not handle an ordinary direct Channels share URL and is outside this slice. |
| [wx-video-channel-download](https://github.com/oliver-zch/wx-video-channel-download/tree/baf68f25726b2f4a7f1885721ac993feeb18df37) | `baf68f25` | Resolves a direct Channels share URL through Tencent Yuanbao, then returns `coverUrl` from a Channels feed response | MIT, but it requires a Yuanbao session Cookie and undocumented endpoints. Low-maturity reference only; not imported or executed. |
| [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download/tree/d3a59ed5909a332fdd61b3c49bd49442db9c687f) | `d3a59ed5` | Active Channels capture/download project with cover data, but its normal workflow installs a root certificate and intercepts traffic from a WeChat client | Current license is Commons Clause layered on MIT and removes the right to sell the software. Its code cannot be presented as Apache-2.0 Open-Flame code, and its MITM/client architecture is not this product boundary. Rejected. |

The software license of a downloader does not grant copyright or publication
rights in a downloaded cover. Open-Flame keeps the source URL sanitized and the
cover as a user-controlled local artifact; upload or publication remains a
separate explicit action.

## Why the existing yt-dlp boundary is retained

- Open-Flame already pins and verifies the platform-independent yt-dlp
  `2026.08.19` zipimport. Reusing it preserves the current product identity,
  worker isolation, Cookie boundary, command allowlist and third-party notice.
- yt-dlp's Bilibili extractor maps the platform `videoData.pic` field to
  `thumbnail`. Its Douyin parser returns `cover`, `origin_cover` and dynamic
  alternatives in the thumbnail collection. Both extractors are included in
  the fixed tag's supported-site list.
- `--write-thumbnail` writes the chosen platform thumbnail response to disk;
  the implementation copies the response bytes unless an explicit thumbnail
  conversion applies. Open-Flame's existing `image>png` rule is restricted to
  the ambiguous `.image` suffix seen on TikTok and does not generally transcode
  ordinary Bilibili or Douyin JPEG/PNG/WebP covers.
- `--embed-thumbnail` is unnecessary. Upstream documents that embedding may use
  GPL-licensed mutagen or AtomicParsley, which would add a distribution boundary
  without helping this separate-cover feature.
- Adding another downloader would duplicate platform requests, authentication,
  subprocess controls and evidence identity while still requiring the same
  managed-image validation and Upload import step.

## Platform boundary

### Bilibili

For an accepted ordinary BV/av input, the pinned extractor's platform cover is
the `videoData.pic` thumbnail. Multipart and playlist identity remains governed
by the existing Download probe; this feature imports only a thumbnail already
registered against the selected ready original. It does not fetch a second
cover from a Bilibili API and does not infer that all pages in a multipart item
have independent covers.

### Douyin

The pinned extractor can return static `cover` and `origin_cover` entries plus
dynamic variants. The current one-thumbnail command accepts yt-dlp's selected
static result. Open-Flame does not depend on the internal tie-break order and
does not label it `origin_cover` unless a later adapter contract captures and
verifies that variant explicitly. Fresh Cookie requirements, short-link
resolution and platform request failures remain the existing Download
capability boundary.

### WeChat Channels

yt-dlp `2026.08.19` has no dedicated WeChat/Weixin/Channels extractor. The
absence of a named extractor cannot be converted into a verified capability by
the generic extractor. Direct-share approaches reviewed here depend on either
an undocumented Yuanbao/Channels API and session Cookie or a WeChat client with
MITM root-certificate installation. The less invasive article-embed approach
only covers a Channels item embedded in a public Official Account article and
also uses an undocumented endpoint.

Consequently this slice does not add Channels as a Download source, install a
certificate, request a Yuanbao Cookie, or claim Channels cover support. A future
adapter would need its own source type, credential contract, exact endpoint
allowlist, request budget, failure taxonomy and separately approved real-world
evidence.

## Implementation scope

- Reuse the existing ready `thumbnail` auxiliary artifact and
  `POST /api/v1/uploads/covers/download-artifacts/{artifact_id}` import boundary.
- Expose an explicit operator action only where a ready downloaded thumbnail is
  present. Captions and original media must never be accepted as cover IDs.
- Preserve the artifact's registered parent, managed path, MIME, size and
  SHA-256 checks before copying it into Upload's managed cover store.
- Keep import idempotent for the same artifact and digest. A retry must return
  the same cover record rather than create repeated copies.
- Let Upload perform its existing complete-image decode, static JPEG/PNG/WebP,
  pixel, orientation and size validation. A valid Download sidecar is not
  automatically a platform-compatible upload cover.
- Make the imported result available in the ordinary Upload cover selection;
  importing it must not create an upload job, select an account, confirm a job
  or send any platform request.
- Preserve Download polling state, focus, selection and Editorial Glass
  behavior. The UI name is “来源封面（平台返回）” and must not promise an original
  master image.
- Do not change schema versions, the pinned downloader, capability decisions,
  user Cookie storage, upload confirmation semantics or tracked `tests/`.

## Validation

These results apply only to the working tree that was subsequently committed;
the commit and remote identity are recorded after the scope check below.

| Check | Result |
| --- | --- |
| Final implementation diff and exact affected files | **PASS** — 12 tracked files: six source files, four operator/design documents, `HANDOFF.md`, and this validation record; no schema, downloader pin, package metadata or tracked test changed |
| Fixture-backed yt-dlp output inventory: original plus owned thumbnail, no unowned file accepted | **PASS** — pinned command/thumbnail contract 26 passed; auxiliary ownership, ordinal, escape, unowned and recovery matrix 12 passed; ignored fixture published registered PNG/JPE covers and a caption with the ready original |
| Ready registered thumbnail import, independent managed copy and returned dimensions/MIME/SHA-256 | **PASS** — ignored full-app validator compared downloaded and upload-domain bytes/SHA-256 and checked PNG 400×300 plus JPE normalization to a validated JPEG cover |
| Same artifact/digest retry returns the same cover identity | **PASS** — repeated POST returned the same managed cover ID and no duplicate record |
| Wrong artifact kind, unknown/non-ready record, missing file, path/identity/size/hash mutation and invalid image rejection | **PASS** — ignored validator covered caption, unknown ID, non-ready job/asset, unsafe path, wrong MIME/hash, invalid bytes, hard link and missing file; injected Download SQLite failure returned bounded `503 upload_database_unavailable` |
| Browser action visibility, explicit invocation, success/error text, focus/input/selection preservation and no implicit upload job | **PASS** — production HTML against a no-remote local app showed preview/download/deep-link; navigation caused zero import POSTs, the explicit click caused exactly one, retained typed title and focused the imported card, left all platform cover selections empty and left jobs empty |
| Editorial Glass light/dark, reduced motion, narrow width and 200% text checks for the changed production page | **PASS** — focused Chromium run passed light/dark, reduced-motion, 390 px and 200% text checks; screenshots were inspected; the existing four-page production matrix also passed 44/44 with zero external requests, page/console errors or horizontal overflow |
| Relevant existing regressions and ignored local validators, with no tracked test change | **PASS with frozen-baseline note** — 151 selected tests passed (upload UI/API, runtime logging, auxiliary artifacts and yt-dlp contracts) plus both ignored validators. `test_download_upload_integration.py` remained 14 failed and `test_batch_assets_ui.py` remained 18 failed/1 passed; an isolated clean-HEAD worktree produced the exact same results, before this diff, at their obsolete CSRF/custom-DOM harness boundaries. No tracked test changed |
| Inline JavaScript `node --check`, `compileall`, dependency compatibility and `git diff --check` | **PASS** — both changed inline scripts parsed, source compiled, `uv pip check` reported 24 compatible packages and the diff was whitespace-clean |
| `scripts/verify_commit_scope.py --staged` and independent diff review | **PASS** — scope guard and cached whitespace check passed for the exact 12-file staged set; an independent final review found no P1/P2 after the Database-error mapping, JPE normalization and preview-failure fixes |

No real Bilibili, Douyin or WeChat Channels URL, Cookie, thumbnail bytes,
platform login, upload, review, scheduled publication or public visibility is
validated by this research record. Real platform evidence must name the exact
platform, source type, adapter/downloader version, environment and product
build; synthetic success cannot upgrade a capability to `verified`.

## Next boundary

After local import and browser validation are recorded above, the next useful
evidence is a separately authorized, bounded real download where the returned
cover can be compared with the visible platform cover and then imported without
creating an upload. Bilibili and Douyin require separate observations. WeChat
Channels remains a separate adapter decision rather than a fallback inside the
yt-dlp path.
