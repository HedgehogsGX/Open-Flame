# Iteration 0.28.0 upload identity contract validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

Upload account bindings, ordered job targets, cancellation batches, and current
retry-leaf identity matching now share the public, state-free
`uploads/identity.py` contract. `UploadService`, the Workflow profile codec,
and `LocalWorkflowAdapter` use the same exact field definitions and platform
set. The former private `UploadService._cancel_batch` implementation has been
removed.

The helper performs no database, filesystem, runtime, login, retry traversal,
request-digest, or state transition work. Callers retain their own cardinality,
authorization, error mapping, and transactional checks.

## Compatibility held

- Account bindings retain their supplied order, require exact keys and unique
  account IDs, and continue to distinguish malformed identifiers from invalid
  binding structure in the upload domain. The entire batch shape is checked
  before values, preserving the previous error precedence.
- Workflow upload targets remain ordered by output and then account. A bound
  Workflow accepts at most 10 outputs by 3 accounts, or 30 target slots.
- The legacy `inspect_upload(..., expected_targets=None)` path still delegates
  its unique 1–64 root limit to `latest_jobs_by_ids`; malformed root IDs and
  duplicate/oversized batches retain their previous distinct error paths.
- Retry traversal and leaf selection remain in `UploadService`. The shared
  matcher only verifies the current leaf ID plus immutable source, account, and
  platform identity.
- Cancellation still rechecks every leaf inside the upload transaction. A new
  retry successor remains `job_retry_lineage_changed`; re-login still cannot
  block emergency cancellation because that path deliberately uses
  `verify_current_session=False` while retaining the platform check.
- Uncheckpointed discovery still validates the complete request digest and
  preserves `upload_request_invalid` versus `upload_request_mismatch` before it
  delegates the exact batch to cancellation.
- No Schema, service, thread, queue, runtime, dependency, or platform was added.

## Verification

| Check | Result |
| --- | --- |
| Upload service, resilience, platform parameters, backup, and Schema 3 | **243 passed in 70.37s** |
| Upload `cancel_many`, adapter cancellation, and uncheckpointed discovery | **PASS** |
| Whole-workflow cancellation and cancellation concurrency | **PASS** |
| Multi-segment adapter/service, retry-leaf replacement, and Schema migration | **PASS** |
| Source title, preflight, restart continuation, and upload attention recovery | **PASS** |
| v0.28, no-AI whole-video, automation, and AI-ledger validators | **PASS** |
| Synthetic three-platform full-chain and full-video workflows | **PASS** |
| Speech-rate and preset boundary/API validators | **PASS** |
| Direct identity, binding error precedence, and legacy 64-root probes | **PASS** |
| Full source `compileall` and `git diff --check` | **PASS** |

No tracked test file was added or modified. New probes were executed from
standard input or existing ignored `validation/local/` validators.

## Evidence boundary

These checks used local stores, synthetic media, deterministic fake AI workers,
fake upload acknowledgements, and network guards. They did not download a real
URL, call OpenAI, log in to a platform, upload media, schedule a real
publication, or verify moderation/public visibility. This refactor does not
create a release receipt or upgrade any platform capability evidence.
