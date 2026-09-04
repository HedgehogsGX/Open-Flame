# Iteration 0.17.0 real progress engineering evidence

Date: 2026-09-04

Product: `video-download-control` `0.17.0`

Database: Schema `11`

Status: final Windows engineering and project-only source-package record

## 1. Scope and evidence boundary

This iteration adds a live, persisted **stage estimate** for real yt-dlp downloads.
It does not claim an exact end-to-end byte percentage. The estimate is designed
to remain monotonic across main-media transfer, merge/post-processing, output
mapping, verification, and final publication while avoiding source identity or
tool-output disclosure.

The completed evidence below uses deterministic tests and a synthetic/no-network
browser scenario. No real platform URL, Cookie, account, or media payload was
used. Nothing here upgrades YouTube, X, Bilibili, Douyin, TikTok, or Instagram
from `candidate`, qualifies Stage 0, proves target Linux/Docker behavior, or
authorizes redistribution of a third-party binary, dependency wheelhouse,
installer, OCI image, or yt-dlp/FFmpeg tool bundle. The
[Iteration 0.16.0 record](iteration-0.16.0-claim-retry-evidence.md) remains the
historical evidence for Schema 11 claim fencing and explicit retry.

## 2. Progress protocol and aggregation contract

The real yt-dlp download command enables newline-delimited progress and supplies
two fixed templates: a download control line and a post-processing phase line.
The download line contains only constant presence bits plus bounded numeric
status, byte, estimate, and fragment-index fields. It does not print a URL,
source ID, format ID, title, filename, or filesystem path. Probe commands do not
enable this protocol.

The parser requires the exact field count, valid status, both main-media
presence bits, bounded non-negative integers for downloaded/total/index/max,
and at most two media-transfer slots. `total_bytes_estimate` may instead be a
finite non-negative integer or float no greater than signed 64-bit range; it is
used only for a ratio and is never exposed as an exact total. Malformed,
oversized, untrusted, or auxiliary lines fail closed and do not advance
progress. In particular:

- subtitle and other sidecar transfers lack the required main-media identity
  presence and are ignored for progress, although valid sidecars are still
  mapped, verified, and published as artifacts;
- sequential video/audio transfers occupy two conservative ordered slots, so a
  single completed stream does not falsely report the whole download complete;
- indexed fragments use the bounded `progress_idx` / `max_progress` pair and
  aggregate the latest fraction for each slot; malformed indices or a maximum
  above two are ignored;
- an unknown total continues the Worker heartbeat and may retain downloaded-byte
  diagnostics internally, but does not invent a percentage; and
- after post-processing begins, later transfer lines cannot regress the phase.

Adapter transfer estimates are capped below completion. The Worker maps adapter
progress into the Job's download band, persists verification separately, and
sets `1.0` only after output mapping, file checks, asset verification, and ready
publication succeed. The resulting browser percentage is deliberately marked
as approximate.

## 3. Streaming and log-privacy boundary

The subprocess runner observes only complete stdout lines that have already
been admitted to its bounded output buffer. It handles LF, CRLF, CR, and a final
unterminated fragment without giving a partial overflow line to the observer.
The real adapter does not install a stderr observer. Observer failure shuts down
the child through the existing bounded process path and records only an
allowlisted exception type and failure site.

Ordinary runtime logs never include raw stdout/stderr, the progress control line,
argv, URL, source identity, title, filename, Cookie ref/content, or local path.
Tests include a sentinel in a rejected control line and verify that it is absent
from persisted progress and structured log output. Operators must not enable
ad-hoc raw tool-output or verbose argv logging when diagnosing progress.

## 4. API and browser contract

Batch detail returns each Job's persisted phase and progress. The frontend maps
the stored state to a Chinese stage label, an explicitly approximate percentage,
a native `<progress>` element, and an ARIA label containing only the platform,
phase, and percentage. Values are clamped to the interval from zero to one.
Polling may miss a very short phase, but stale data cannot manufacture a higher
precision than the database state.

The implemented labels distinguish queued, analysing, downloading,
merge/post-processing, verification, ready, failed, and cancelled states. A
failure or cancellation remains a terminal state rather than displaying a
completed bar.

## 5. Completed checks

| Validation slice | Confirmed result |
|---|---|
| Broad progress-focused regression | `186 passed in 24.95s` |
| Review-focused regression | `149 passed in 13.66s` |
| Deterministic handshake regressions | `2 passed in 1.34s` |
| Isolated browser QA | A synthetic YouTube Batch on `127.0.0.1:18779` displayed queued `0%`; injected persisted downloading `0.24` displayed `正在下载 / 约 24%`; postprocessing `0.79` displayed `正在合并/后处理 / 约 79%`; native progress value and ARIA state matched, with zero console errors |
| Isolation cleanup | The temporary server was stopped and port `18779` was released; the pre-existing loopback service on port `8000` was left untouched |

The final repository regression and static gates below were run after the code,
tests, and documentation fixes above. Package results were recorded separately
after the final source-equivalent archives were rebuilt:

```text
FINAL_FULL_PYTEST: 1270 passed, 8 skipped in 106.62s
FINAL_STATIC_CHECKS: compileall -q src tests; uv lock --check --offline; git diff --check; 26 Markdown files checked with 0 missing relative links
FINAL_PACKAGE_FILE_COUNTS_AND_SOURCE_EQUIVALENCE: 215 checkout publishable files; 216 regular sdist files including PKG-INFO; 215 source files match checkout; 96 wheel entries and RECORD rows; all 60 package files match checkout and sdist byte-for-byte
FINAL_PACKAGE_IDENTITY_AND_PRIVACY_AUDIT: metadata/import 0.17.0 and Apache-2.0; 13 console scripts; 32 legal files with exact NOTICE; 14 runtime/project distributions and dependency check passed; checkout/wheel build identities match; known-real identifiers and high-confidence secrets 0 hit
```

Exact final sdist/wheel SHA-256 values must remain in the gitignored release
verifier output and the external acceptance report, not in this source file:
including an archive hash in a file that is itself packaged would create a
self-referential, non-rebuildable claim. The final package verifier repeated the
scan after documentation completion and confirmed all 215 publishable paths are
ordinary files with no Git links, reparse points, archives, executable payloads,
or unexpected binary headers. Thirteen explicit synthetic Windows-path canaries
remain confined to tests.

## 6. Remaining acceptance work

- Execute the documented Linux/Docker acceptance on an authorized target host;
  Windows tests and loopback browser QA do not satisfy it.
- Run separately authorized real-platform and Stage 0 validation. A synthetic
  progress transition proves display and persistence semantics, not downloader
  compatibility with a platform.
- Keep frozen executable/installer and all third-party redistribution blocked
  until target-specific source, SBOM, notices, relinking obligations,
  provenance, signature validation, and human license review are complete.

Operational meaning and troubleshooting steps are in the
[Runbook](../docs/RUNBOOK.md); the current scope and remaining work are tracked
in the [project handoff](../HANDOFF.md).
