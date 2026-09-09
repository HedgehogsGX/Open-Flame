# Iteration 0.28.0 verified media response boundary

Date: 2026-09-10
Scope: architecture review S5a; local/offline source validation only

## Result

The response that streams an already-open, verified media handle now lives in
`video_download_control.verified_media_response`. Editing routes import the
public `VerifiedOpenFileResponse` directly, so importing the editing API no
longer imports the top-level application API. The download API keeps a private
import alias for frozen tests and internal call sites, but it no longer owns a
second response implementation.

The boundary remains deliberately small. Path registration, filesystem
identity checks, spool creation, HTTP disconnect coordination and the download
snapshot response remain in the download API. Editing continues to open and
verify its managed source or asset, produce the one-megabyte chunk manifest and
map domain errors. The shared response only owns the supplied binary handle,
rechecks the supplied manifest while streaming and closes the handle on every
exit path.

No database, Schema, worker, queue, runtime, dependency, user interface or
platform adapter changed. No tracked test file was added or modified.

## Preserved contracts

- Full and `HEAD` responses retain exact length, SHA-256 ETag, last-modified,
  private no-store, nosniff and byte-range headers.
- Bounded, open-ended, suffix and multipart ranges still stream from the
  supplied handle. The response never uses ASGI `pathsend` and never reopens a
  placeholder path.
- `If-Range` match keeps the range response; mismatch falls back to the full
  verified body. Malformed and unsatisfiable ranges retain 400 and 416.
- Each involved verified chunk is hashed before its slice is sent. An in-place
  change is rejected before the changed chunk can be emitted.
- Constructor rejection, normal completion, `HEAD`, range errors, send failure
  and verification failure all close the owned handle.
- `api._VerifiedOriginalFileResponse` and
  `api._VERIFIED_STREAM_CHUNK_BYTES` remain import aliases for existing callers;
  production editing code no longer imports a private symbol from the top-level
  API.

## Validation

| Check | Result |
| --- | --- |
| `python -m compileall -q src` | PASS |
| AST source comparison after public-name substitutions | PASS; the 221-line response class is a mechanical extraction |
| Fresh-process editing API import without `video_download_control.api` in `sys.modules`; compatibility alias identity | PASS |
| Independent response matrix: full/HEAD, single/overlap/multipart ranges, malformed/unsatisfiable/>100 ranges, `If-Range` ETag/date/mismatch, no `pathsend`, replacement, concurrency, mutation/truncation and every close path | **26/26 PASS** |
| Checked-in focused resilience and guarded editing route cases | **5 passed** |
| `tests/test_editing_media.py` | **32 passed** |
| `tests/test_editing_api.py` | **15 passed, 2 failed**; both failures assert historical Editing Schema 1 while current Schema is 4 |
| Combined artifact/asset/editing-media run | **38 passed, 25 failed**; all 25 failures enter the already documented historical download `testserver` or missing-CSRF setup before this response boundary is exercised |
| No-AI full-video validator | **5/5 PASS** |
| Synthetic three-platform full-chain validator | PASS; Bilibili, Douyin and Tencent adapters, with zero real network calls |
| Source release inventory | PASS; 279 listed files and no unlisted package file |
| `git diff --check` | PASS |

The ignored probe is
`validation/local/s5a_verified_media_response_probe.py`; it is intentionally
excluded from Git. These results bind the current source checkout only. They do
not prove a frozen release, a real URL download, an OpenAI response, platform
login, upload acceptance, scheduled publication or public visibility.

## Next boundary

S5b moves `EditingManager` and its worker lifecycle out of `editing/api.py`.
That extraction must preserve the existing service factory injection seam,
exclusive lease ownership, stop timeout behavior, cancellation order and
restart recovery while keeping the old `editing.api.EditingManager` import
available to frozen callers.
