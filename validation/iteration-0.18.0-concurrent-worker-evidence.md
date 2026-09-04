# Iteration 0.18.0 concurrent Worker engineering evidence

Date: 2026-09-04

Product: `video-download-control` `0.18.0`

Database: Schema `11` (unchanged)

Status: final Windows engineering record and frozen project-only package acceptance contract

## 1. Scope and evidence boundary

This iteration closes an execution gap in the Windows downloader. The database
already limited active Jobs to two globally and one per platform, but both the
standalone local Worker loop and the app's Worker child called synchronous
`run_once()` serially. Repository claim-limit tests therefore did not prove
that the actual application could execute different platforms concurrently.

The new coordinator runs in the existing single Worker process and has two
execution slots. It is wired into `video-download-local-app`, standalone
`video-download-local-worker --drain`, and the resident
`--poll-interval-seconds` mode. A free slot can refill while another platform is
still executing. Standalone one-shot mode remains one attempt; `--check` never
claims work. This iteration does not change the candidate Linux Worker loop.

All new evidence described here uses synthetic data, offline adapters, injected
entrypoint boundaries, or local subprocesses. No new real-platform request,
real Cookie/account use, or browser/UI acceptance occurred. The previous
[0.17.0 progress and package record](iteration-0.17.0-real-progress-evidence.md)
is historical and cannot stand in for a 0.18.0 build or acceptance result.
YouTube, X, Bilibili, Douyin, TikTok, and Instagram remain `candidate`; Stage 0,
Linux/Docker, installers, and third-party redistribution are not qualified by
this work.

## 2. Execution and recovery contract

[`Worker`](../src/video_download_control/worker.py) now exposes an immutable
`WorkerClaim` carrying the lease and original cycle start time, a serial
`claim_once()` boundary, and `execute_claimed()` for the whole Attempt lifecycle.
The lease is omitted from the claim's repr. Existing `run_once()` remains a
compatibility wrapper, preserving the synchronous result and lifecycle logging.

The [`coordinator`](../src/video_download_control/worker_pool.py) alone owns
claiming, the active Future map, result callbacks, and the stop channel. There
are at most two execution Futures; the
[`repository`](../src/video_download_control/worker_repository.py) retains the
transactional global-two/per-platform-one constraints and app claim gate.

A slot is not released when its database row becomes terminal. It remains
occupied until `execute_claimed()` has returned through its final cleanup.
Live job identities stay excluded from refill claims. While any local Future
is active, refill disables all directory reconciliation, asset-intent recovery,
and expired-lease recovery; full recovery resumes only at local quiescence.
This prevents maintenance from deleting files still owned by a finishing or
cancelling in-process Attempt. It also prevents a database-terminal but still
cleaning job from being reclaimed prematurely.

Worker queue state and claiming are synchronized with pause updates. If a
storage-triggered pause cannot be persisted, this Worker instance latches the
failure and cannot clear it by reading an unpaused database later. A new Worker
instance is required after the underlying storage problem is corrected.

## 3. Stop and failure contract

Normal app shutdown keeps the established supervisor contract: stop the
SQLite claim gate, close the Worker command channel, allow already claimed work
to finish within the supervisor's existing deadline, and terminate only the
owned Windows Job if bounded shutdown requires it. The coordinator checks the
stop channel before each individual new claim, not just once per pair.

Standalone Ctrl+C or an exception stops new dispatch, sets the Worker/runner's
shared one-way stop Event, interrupts owned subprocesses, and joins execution
threads before returning. The shared runner signal also reaches probes and
ffprobe verification, which do not necessarily have per-job cancellation
callbacks. The Event does not replace the app's database-linearized claim gate.

An ambiguous executor submission failure uses an at-most-once dispatch wrapper:
stop first, then close the claimed Attempt once whether submission enqueued it
or failed before enqueue. A heartbeat thread-start failure was separately
reproduced and moved into the Attempt exception boundary so it does not leave
an unowned running Attempt outside normal failure handling.

`WORKER_LOST` returned by a controlled interruption is terminal under the
existing retry policy; it is **not automatically retried**. Eligible failed
flat Jobs can use the explicit new-generation retry action. This differs from
an abrupt process kill, where a still-active lease expires and the existing
recovery path may abandon that Attempt and create a new one.

Reader-thread startup failure after subprocess creation was reproduced during
review and fixed. Four real-child regressions cover failure on the first or
second reader start, ordinary errors and KeyboardInterrupt; the owned child is
terminated and its pipe handles are closed. No raw tool output, argv, URL, Cookie,
private path, or exception message is added to normal runtime logging.

## 4. Completed validation and frozen package contract

| Validation slice | Evidence and limits |
|---|---|
| Actual entrypoint red reproduction | Blocking the first platform prevented the old standalone drain/poll and app Worker child from starting the second; the regression failed before integration was changed |
| Actual entrypoint integration | `4 passed`; [`test_concurrent_worker_integration.py`](../tests/test_concurrent_worker_integration.py) exercises real CLI/app child loops with synthetic Worker construction, strict launch/claim/EOF handling, result output, and Ctrl+C/join behavior; it is not a real network or full active-download spawned-app acceptance |
| Real Worker/repository overlap | [`test_worker_pool.py`](../tests/test_worker_pool.py) uses the actual Worker, SQLite repository and AssetStore with an offline blocking adapter; it proves two-platform overlap, no concurrent same-platform download, and immediate refill while the other platform remains blocked |
| Cleanup and recovery | Worker/repository/asset-intent tests cover live job exclusion, expired recovery suppression, cancelled staged intents with and without a cleanup callback, and slots retained through final cleanup |
| Pause and dispatch failures | Tests cover failed pause persistence, at-most-once submission fallback, stop checks between claims, and heartbeat thread-start failure |
| Preliminary focused regression | `127 passed`; this is a point-in-time implementation result, not the final full test count or package acceptance |
| Reader-thread startup cleanup | Four real-child resource-failure regressions passed; original exceptions retain provenance, readers and child processes are cleaned |
| Worker stop regression | `5 passed`; controlled stopping, preclaim stop recheck and interrupted Attempt handling remain covered |
| Spawned Windows application preflight | Final source `local_app_cli --check`, separate synthetic app root and port 18819, pinned local tool bundle, no Cookie: stdout `{"status":"checked"}`; control/Worker preflight and shutdown logs present, no claim, port released |
| Final repository regression | `1307 passed, 8 skipped in 118.43s` |
| Final static checks | `compileall -q src tests`, `uv lock --check --offline` (24 packages), and `git diff --check` passed; 34 Markdown files checked with zero missing relative links |

The eight skips are explicit host limitations: one POSIX directory-FD cleanup,
four root/getfacl Cookie-source metadata contracts, one AF_UNIX roundtrip, one
POSIX open-file replacement and one POSIX permission case. They are not passes
and must be executed on the target Linux environment.

The source-equivalent package precheck passed all counts and identity/privacy
checks below. These values are also the frozen acceptance contract for the
final documentation-complete archive rebuild; its exact archive identity and
final verification outcome belong to the external release report. This source
record does not substitute a precheck archive for the final rebuilt archives.

```text
FINAL_FULL_PYTEST: 1307 passed, 8 skipped in 118.43s
FINAL_STATIC_CHECKS: compileall -q src tests; uv lock --check --offline (24 packages); git diff --check; 34 Markdown files with 0 missing relative links
FINAL_PACKAGE_FILE_COUNTS_AND_SOURCE_EQUIVALENCE: 221 checkout source files; 222 regular sdist files including PKG-INFO; 61 package files equal across checkout/sdist/wheel; 97 wheel entries and RECORD rows
FINAL_PACKAGE_IDENTITY_AND_PRIVACY_AUDIT: metadata/import 0.18.0 and Apache-2.0; 32 legal files with exact NOTICE; 13 console scripts; 14 runtime/project distributions and dependency check passed; 8 known-real markers and 11 high-confidence secret patterns, 0 hits
READER_THREAD_STARTUP_FIX: fixed; 4 actual-child regressions passed
CHECKOUT_AND_WHEEL_BUILD_IDENTITY: 0.18.0+build.sha256.b36396390d9782343f0b3579d220e6a32314e4c78ffc82d4fd55a98f518afdf2
```

The final package process independently rebuilds the 0.18.0 project-only sdist,
builds its wheel from that exact source archive, installs into an isolated
environment, and must match this frozen contract before release. Exact archive
SHA-256 values belong in gitignored verifier output or an external acceptance
report, never inside a source file that is itself included in those archives.
The package-payload build identity above excludes these documentation files and
is not a self-referential archive hash. Project-only Apache-2.0 acceptance does
not authorize any third-party binary, wheelhouse, installer, OCI image or tool
bundle redistribution.

## 5. Remaining work

- Retain regression coverage for reader startup cleanup and the gates above.
- Close the Windows app short-link integration gap and the default per-platform
  Cookie/profile-selection and queued credential-coverage gap; existing secure
  primitives alone do not make those normal frontend workflows complete.
- Continue authorized real-platform acceptance in the established Bilibili,
  Douyin, TikTok, Instagram order, without promoting candidate status from
  synthetic concurrency tests.
- Retain the outstanding target Linux/Docker, real credential permissions,
  Stage 0 and third-party release obligations described in the
  [project handoff](../HANDOFF.md) and [Runbook](../docs/RUNBOOK.md).

The existing loopback service on port `8000` was not replaced by this iteration.
No commit, push, installer, tool-bundle or other third-party release is implied
by this engineering record.
