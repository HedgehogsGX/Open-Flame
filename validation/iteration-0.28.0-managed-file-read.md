# Iteration 0.28.0 managed-file read boundary

Date: 2026-09-10
Scope: architecture review S6b; local/offline source validation only

## Result

Editing and Upload now share three domain-neutral read primitives: binary open
with an immediate four-field `fstat` match, repeated handle identity checks, and
bounded SHA-256 reading with an optional raw per-chunk digest manifest. The
shared read result reports actual size and digest without seeking or closing the
handle. A successful open explicitly transfers handle ownership to its caller;
every pre-transfer failure attempts closure while preserving the first error.

The bounded reader requests at most the caller's remaining allowance plus one
sentinel byte. A growing file is therefore detected after reading no more than
`maximum + 1` bytes, rather than after another whole chunk. It also leaves the
handle open so ordinary callers can close it with a context manager and the
Editing response path can retain the exact verified handle.

Editing's ordinary stable hash and retained verified-file path now compose these
primitives. Upload's stable digest, managed source verification and source/cover
matching opens use the same mechanics. Copying, cover payload decoding,
publication, registration, rollback, integrity caches, activity leases and
SQLite transactions remain in their existing domains. No database, Schema,
queue, runtime, dependency, route, user interface or platform adapter behavior
changed. No tracked test file was added or modified.

## Preserved domain contracts

- Editing still rejects an initially empty or oversized file as
  `editing_media_size_invalid`; growth or identity drift after acceptance is
  `editing_media_changed`; ordinary open/read/fstat I/O failure remains
  `editing_media_unavailable`.
- Editing's retained path maps open/read/identity/size/hash failures to its
  caller's `source_changed` or `asset_changed` code. Initial and final plain-path
  checks keep `unsafe_editing_file` and `editing_media_unavailable` distinct.
- The retained path still performs final `lstat` while the verified handle is
  open, seeks that same handle to zero, and returns the first `fstat` plus one
  raw SHA-256 digest per 1 MiB manifest chunk. Every failure after ownership
  transfer attempts closure, including an unexpected `BaseException`; a close
  failure cannot replace the original error.
- Upload maps an in-read limit breach to the supplied `size_code`, handle or
  final-path drift to `changed_code`, and I/O failure to `unavailable_code`.
  A final symlink, reparse point or hardlink still remains
  `unsafe_upload_file`.
- Upload's 2 GiB source limit, Editing's 16 GiB source and 8 GiB output limits,
  Range response verification, cancellation cleanup and all transaction scopes
  remain unchanged.

## Validation

| Check | Result |
| --- | --- |
| Ignored managed-file read matrix | **27/27 PASS**: ownership transfer, digest, raw chunk manifest, mismatch close, close-failure first-error preservation, exact one-byte growth sentinel, append/truncate drift, both domain mappings, retained handle/manifest and unexpected-exception close |
| Ignored managed-file identity matrix | **20/20 PASS** |
| Editing service, Upload service, upload activity-lock and platform-parameter modules | **103 passed** |
| Range, same-handle manifest, send-failure close, registered-source replacement, Editing cancellation, Upload source mutation/import, interrupted copy, activity lease and staged cover cases | **14 passed** |
| Independent verified media response matrix | **13/13 PASS** |
| All five checked-in Editing modules | **73 passed, 3 failed**; all three failures still assert historical Editing Schema 1 while current Schema is 4 |
| Editing cancellation safety, no-AI whole-video and synthetic three-platform full-chain validators | PASS; Bilibili, Douyin and Tencent adapters, with zero real platform requests |
| Source release inventory | PASS; 281 listed files, 128 package files and no unlisted package file |
| Fresh shared-module import, `compileall`, offline lock, `uv pip check` and `git diff --check` | PASS |

Three independent read-only reviews found no P1/P2 after the sentinel and
unexpected-exception close fixes. These results bind the current source checkout
only. They do not prove a frozen release, real URL download, OpenAI response,
human listening quality, platform login, upload acceptance, scheduled
publication or public visibility.

## Next boundary

Architecture review S6 is complete at the primitive boundary. Domain-specific
copy, payload, publication and transaction code remains separate by design. The
next review slice is S7: split repeated frontend parameter construction, preset
merge and validation into ordinary functions while preserving capability-driven
limits, immediate feedback and operator state.
