# Iteration 0.28.0 pre-authorized workflow restart continuation

Validated: 2026-09-09. Source baseline: `15d0137469feecbf618763b526c5d2230b639548`; current working tree evidence, not a frozen release receipt.

## Result

An automatic workflow can now continue after an application restart when the durable domain proves that each current non-terminal item being re-queued is an original AI task, render plan, or upload leaf that was still `queued` and therefore had not been claimed or dispatched. Already successful upload leaves can remain terminal while the remaining original leaves resume. The workflow must already contain the corresponding immutable `auto_confirm_edit` or `auto_confirm_upload` authorization. Manual workflows, every retry successor, running/canceling work, and unknown remote outcomes still stop.

The lower-level recovery rules remain conservative:

- Editing recovery converts queued AI tasks and render plans to `review / restart_confirmation_required`; running or canceling work becomes failed, and a dispatched remote invocation becomes unknown.
- Upload recovery converts queued jobs to `draft / restart_confirmation_required`; running jobs become `unknown / interrupted_result_unknown`.
- Claims change a row from queued to running inside the domain transaction before the provider, media processor, or upload backend is called. The restart code therefore identifies work that the previous process did not dispatch.

The workflow adapter now gives retry lineage precedence over the generic restart code. A queued retry recovered after another restart is still reported as `ai_retry_confirmation_required`, `render_retry_confirmation_required`, or `upload_retry_confirmation_required` and cannot consume the original workflow automation setting. Upload inspection continues to resolve the unique current leaf before applying this rule; unknown leaves still win over every waiting state.

Automatic confirmation uses positive reason allowlists. Normal first-run confirmation/review reasons and the exact original restart reason are eligible; legacy migration reasons, mixed review reasons, and any unknown future reason remain visible and require an explicit decision. This prevents a new domain review code from silently inheriting an older workflow's automation setting.

Before re-queueing, the existing confirmation methods perform their normal current-state checks: AI authorization and runtime/model identity, render recipe/source/timeline and synthesis authorization, upload runtime readiness, full frozen account/session binding, schedule, source, cover, and platform parameters. Upload confirmation re-normalizes the complete persisted target under the current per-platform contract and requires exact equality before queueing. Changed authorization, runtime, source identity, account session, or job identity fails closed; invalid, non-canonical, or no-longer-supported title, tag, mode, category, copyright, and platform-option values also fail before the backend. The upload database does not bind otherwise-valid metadata to a separate immutable digest, so this evidence does not claim detection of a direct canonical-to-canonical database rewrite. The workflow reader additionally verifies that the duplicate database `auto_confirm_*` columns exactly match the canonical, SHA-256-bound profile before either flag can authorize restart continuation.

No schema, table, service, background thread, secret store, provider, or platform adapter was added.

## Validation

| Check | Result |
| --- | --- |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_restart_continuation.py` | **PASS**. The ignored validator covers original queued AI/render/upload, manual mode, retry lineage, two-pass reason-conflict laundering, empty/invalid edit review reasons, upload unknown, unknown adapter statuses, unknown/legacy/mixed review reasons, current platform-parameter revalidation, the maximum 10 segments × 3 accounts fan-out, real `WorkflowManager.resume_existing()` background reconciliation, and a tampered detached auto-confirm column. Final validator SHA-256: `4e1da26bddc88ba82f937b12db7ddd54ffd685143a18dfd7a129fe1f71819ecc`. |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_full_chain.py` | **PASS**, 8.69 seconds. The real local domains reach `downloading → awaiting_ai_review → rendering → uploading → awaiting_upload_confirmation → uploading → completed`; the reopened original upload jobs resume without an explicit API confirmation. AI counts remain one transcription, one translation, and two cue-level syntheses; edit asset hashes and all three job IDs remain unchanged. |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_ai_ledger_recovery.py` | **PASS**. Unresolved remote results continue to block workflow retry; only the existing explicit reconciliation path can restore eligibility. |
| `.venv\\Scripts\\python.exe -m pytest -q tests/test_editing_service.py tests/test_upload_service.py tests/test_upload_resilience.py -k "restart or recover or interrupted"` | **8 passed, 49 deselected in 2.38 seconds**. Existing domain recovery assertions remain unchanged; no test file was modified. |
| `.venv\\Scripts\\python.exe -m pytest -q tests/test_editing_service.py tests/test_editing_ai_contracts.py tests/test_upload_service.py tests/test_upload_resilience.py tests/test_upload_platform_parameters.py tests/test_upload_api.py` | **138 passed in 47.47 seconds**. The broader editing, AI contract, upload service, resilience, platform-parameter, and API regression set remains green; no test file was modified. |
| Current workflow inline JavaScript and three installed-Chrome validators | **PASS**. Both current inline scripts pass `node --check`; readiness, multi-segment, and preset browser validators pass against the regenerated production page with synthetic APIs. |
| `compileall`; `uv lock --check --offline`; `uv pip check`; `git diff --check`; changed-Markdown link and release inventory checks | **PASS**. The release inventory contains 257 unique existing files and excludes `tests/`; all 11 changed local Markdown targets resolve. |

The full-chain validator uses a local generated MP4, an isolated synthetic AI plugin, real FFmpeg processing, and a synthetic three-platform backend under Python socket/DNS guards. It performs no real download, OpenAI call, login, upload, scheduled publication, or public release.

## Remaining boundaries

- This evidence does not prove abrupt power-loss durability, multi-process workflow ownership, a real OpenAI response, speech quality, exact provider billing, or platform acceptance.
- The current default application root still lacks a prepared AI runtime, and its existing upload runtime still requires the documented Schema 3 rebuild/upgrade path.
- The current working tree has no new release receipt. The earlier `0592b6f` receipt does not cover this post-release change.
- Production liquid-glass styling remains blocked on the user's selection among the three local candidates; this functional change does not alter page layout or visual tokens.
