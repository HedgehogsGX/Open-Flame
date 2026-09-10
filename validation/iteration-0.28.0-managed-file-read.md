# Iteration 0.28.0 managed-file read boundary

Baseline: 2026-09-10 (architecture review S6b)
Follow-up: 2026-09-11 (bounded-byte snapshot delta only)
Scope: local/offline source validation; the follow-up did not rerun every S6b
check

## Result

Editing and Upload now share four domain-neutral read primitives: binary open
with an immediate four-field `fstat` match, repeated handle identity checks, and
bounded SHA-256 reading with an optional raw per-chunk digest manifest, plus an
immutable bounded byte snapshot whose SHA-256 is calculated from the exact
captured chunks. Hash-only and byte-retaining callers use one private single-pass
consumer. The shared results report actual size and digest without seeking or
closing the handle. A successful open explicitly transfers handle ownership to
its caller; every pre-transfer failure attempts closure while preserving the
first error.

The bounded reader requests at most the caller's remaining allowance plus one
sentinel byte. A growing file is therefore detected after reading no more than
`maximum + 1` bytes, rather than after another whole chunk. It also leaves the
handle open so ordinary callers can close it with a context manager and the
Editing response path can retain the exact verified handle.

Editing's ordinary stable hash, retained verified-file path and AI render WAV
snapshot now compose these primitives. Upload's stable digest, managed source
verification and both imported/managed cover snapshots use the same mechanics.
Copying, WAV/cover decoding, publication, registration, rollback, integrity
caches, activity leases and SQLite transactions remain in their existing
domains. No database, Schema, queue, runtime, dependency, route, user interface
or platform adapter behavior changed. No tracked test file was added or
modified.

The AI render path currently turns the immutable payload into a `BytesIO`
reader. At the 64 MiB cue limit this can briefly retain two in-memory copies;
the bound is explicit, and this follow-up does not replace domain decoding with
another streaming abstraction without measured need.

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
- AI render keeps its absolute canonical-path requirement and its five-field
  signature, including Windows file attributes. Initial/final reparse and
  single-link checks, before/after `fstat`, final `lstat`, caller-selected error
  code, full PCM validation and consumption from the captured bytes are
  unchanged.
- Cover import still reports real identity drift during an over-limit read as
  `cover_changed`: while the source handle remains open it repeats `fstat`
  before allowing an identity-stable limit breach to become
  `cover_size_invalid`. Managed cover verification maps either condition to
  `cover_changed`; missing media remains `cover_reimport_required` and other
  I/O remains `cover_unavailable`.
- Upload's 2 GiB source limit, Editing's 16 GiB source and 8 GiB output limits,
  Range response verification, cancellation cleanup and all transaction scopes
  remain unchanged.

## 2026-09-11 bounded-byte follow-up validation

| Check | Result |
| --- | --- |
| Ignored managed-file read matrix | **37/37 PASS**: prior ownership/digest/manifest/identity coverage plus immutable same-byte snapshot, exact snapshot growth sentinel, AI wrapper mappings, Upload cover mapping and in-read growth precedence |
| Independent ignored managed-file identity rerun | **20/20 PASS** |
| Speech checkpoint validator | **PASS**: immutable WAV/PCM consumption, digest binding, hard-link crash recovery, remote-ledger gate and ready cleanup |
| Focused Editing AI/media and Upload service/resilience modules | **83 passed** |
| All checked-in Upload modules | **539 passed** |
| Downloaded source-cover validator | **PASS**: ready PNG/JPE import, dimensions, SHA-256, idempotency, unsafe/missing/mutated media and database failure mapping |
| `compileall`, validator `py_compile`, `uv pip check`, diff checks and staged commit-scope gate | **PASS** |

## Retained 2026-09-10 S6b baseline evidence

The following rows remain bound to the earlier S6b checkout and were not rerun
for the bounded-byte follow-up.

| Check | Result at the 2026-09-10 checkout |
| --- | --- |
| Editing service, Upload service, upload activity-lock and platform-parameter modules | **103 passed** |
| Range, same-handle manifest, send-failure close, registered-source replacement, Editing cancellation, Upload source mutation/import, interrupted copy, activity lease and staged cover cases | **14 passed** |
| Independent verified media response matrix | **13/13 PASS** |
| All five checked-in Editing modules | **73 passed, 3 failed**; all three failures still assert historical Editing Schema 1 while current Schema is 4 |
| Editing cancellation safety, no-AI whole-video and synthetic three-platform full-chain validators | PASS; Bilibili, Douyin and Tencent adapters, with zero real platform requests |
| Source release inventory | PASS; 281 listed files, 128 package files and no unlisted package file |
| Fresh shared-module import and offline lock | PASS |

Independent read-only review found no remaining P0/P1 after restoring cover
growth error precedence. The 2026-09-11 follow-up rows bind the working tree
exercised for this bounded-byte change. Rows labelled 2026-09-10 remain bound
only to the earlier S6b checkout and were not rerun by this follow-up. Neither
set is a clean release receipt, and neither proves a real URL download, OpenAI
response, human listening quality, platform login, upload acceptance, scheduled
publication or public visibility.

## Next boundary

Architecture review S6 is complete at the identity, hash and bounded-byte
snapshot boundary. Domain-specific copy/fsync/link, decoding, publication and
transaction code remains separate by design; the multi-GiB source copy paths
must not be converted to in-memory snapshots merely to reduce line count. No
further managed-file extraction is planned unless another repeated stable
algorithm appears.
