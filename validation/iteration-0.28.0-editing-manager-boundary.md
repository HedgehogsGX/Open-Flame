# Iteration 0.28.0 EditingManager boundary

Date: 2026-09-10
Scope: architecture review S5b; local/offline source validation only

## Result

`EditingManager` now lives in `video_download_control.editing.manager` instead
of the Editing FastAPI module. The new module owns the editing service, worker
thread, root activity lease, AI and render claim ordering, cancellation events,
active-operation count, stop deadline and output resolution. It does not import
FastAPI, Pydantic, an HTTP route, the verified media response or the top-level
application API.

`editing.api` keeps an import alias so frozen callers can continue to construct
`editing_api.EditingManager`. Route installation injects the API module's
current `EditingService` binding into the manager. This preserves the existing
service factory seam without making the manager import the API in reverse.
`LocalWorkflowAdapter` now imports the manager from its canonical module only
under `TYPE_CHECKING`; importing the adapter at runtime still does not load the
manager or web stack.

No database, Schema, queue, runtime, dependency, route, request model, user
interface or platform adapter behavior changed. No tracked test file was added
or modified.

## Preserved lifecycle contracts

- Construction remains lazy and creates no editing database or activity lease.
- The root-exclusive activity lease is acquired before the service factory,
  interrupted-work recovery or worker publication.
- Processor creation, service construction, recovery or thread-start failure
  clears unpublished state and releases the lease.
- The worker still checks AI claims before render claims, publishes cancellation
  events around the durable claim gap and keeps the existing 0.5 second wake
  behavior.
- An operation increments the active count before resolving the service. Stop
  retains the root lease until the worker and every in-flight operation finish.
- Stop still signals current AI and render work, is idempotent, honors its
  deadline and permanently rejects later service operations.
- Output and cover resolution retain their existing asset-kind checks.

## Validation

| Check | Result |
| --- | --- |
| AST comparison of extracted manager methods | PASS; all 27 method names match, 25 methods are identical, `get()` differs only at the injected factory call, and `__init__()` only stores that factory |
| Fresh-process canonical manager import | PASS; FastAPI, Pydantic and top-level API remain unloaded |
| Fresh-process LocalWorkflowAdapter import | PASS; Editing manager and Editing API remain unloaded |
| Compatibility alias and API service-factory/lease probe | **5/5 PASS**: lazy root, alias identity, lease-before-factory, API binding injection and initialization-failure release |
| Focused checked-in stop/claim/cancel race tests | **3 passed** |
| Editing API plus upload activity-lock tests | **24 passed, 2 failed**; the same two historical Schema 1 assertions |
| All five checked-in Editing test modules | **73 passed, 3 failed**; all three failures assert historical Editing Schema 1 while current Schema is 4 |
| Atomic Editing cancellation validator | PASS |
| Editing cancellation safety matrix | PASS |
| AI authorization and hard-limit validator | **45/45 PASS**, provider runner calls 0 |
| No-AI whole-video validator | **5/5 PASS** |
| Synthetic AI full-video and three-platform full-chain validators | PASS; Bilibili, Douyin and Tencent adapters, with zero real network calls |
| Workflow cancellation adapter/service/concurrency, restart continuation, AI ledger recovery and uncheckpointed edit-upload validators | PASS |
| Workflow adapter light-import validator | PASS |
| Source release inventory | PASS; 280 listed files and no unlisted package file |
| `compileall`, `uv pip check`, offline lock check and `git diff --check` | PASS |

The older ignored `ai-cancel-progress-smoke.py` is not a valid current gate. Its
saved HTTP payload first omitted the now-required authorization digests; after
a local-only adjustment it stopped at `processor_not_configured` because the
harness does not configure the media processor required by current
transcription. It never reached the cancellation assertion. The two current
cancellation validators and the three checked-in stop/claim/cancel race cases
above cover the manager boundary; this stale ignored script was not committed
or counted as passing.

These results bind the current source checkout only. They do not prove a frozen
release, a real URL download, an OpenAI response, human listening quality,
platform login, upload acceptance, scheduled publication or public visibility.

## Next boundary

Architecture review S5 is complete. S6 may extract common managed-file identity,
safe-open and bounded-hash primitives only after demonstrating that Editing and
Upload keep their distinct limits, error mappings, transactions, retained-handle
semantics and TOCTOU protections.
