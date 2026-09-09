# Iteration 0.28.0 edit snapshot observation validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

Workflow editing observations now pass through the public, state-free
`classify_edit_snapshot()` contract and one service observation path. Ledger
reconciliation, manual confirmation, explicit retry, ordinary advancement,
and restart advancement no longer maintain separate status mappings.

The finite facts are waiting for confirmation, already-active work, ready,
retryable failure, attention, and invalid domain data. The classifier does not
grant permission to confirm or retry. Revision/profile CAS, automatic reason
allowlists, explicit action authority, stable retry keys, successor
checkpointing, and cancellation remain in their existing callers and domains.

## Compatibility and corrections

- `EditSnapshot.needs_confirmation` distinguishes a review plan from
  queued/running/canceling work. It is appended with the conservative default
  `True`, preserving positional construction by older adapters. The production
  local adapter reports active work explicitly as `False`.
- `failed` remains distinct from `attention`. Only a currently observed
  editing-domain failure can create a retry successor; an uncertain status or
  output shape cannot inherit retry permission.
- Manual confirmation first observes the current plan. Ready, active, failed,
  attention, ledger-blocked, and invalid observations make zero confirmation
  calls. A changed review reason is checkpointed with a new revision before a
  later confirmation can proceed.
- Explicit retry first observes the current plan. Ready output is checkpointed,
  active work resumes, review work returns to its confirmation barrier, and
  attention or invalid data remains blocked without creating a successor.
- A changed failure reason is checkpointed before retry. The operator must
  submit the new revision after seeing the current reason; the old revision
  cannot authorize a retry for different facts.
- AI invocation-ledger codes returned as snapshots or raised while inspecting
  are converted to the same persistent attention state. They are never retried
  or confirmed, and the path no longer attempts a second stale CAS write.
- Snapshot status and reason must be strings, and `needs_confirmation` must be
  a real boolean. Malformed values fail closed as
  `workflow_domain_data_invalid` before any mutation-capable adapter call.
- The historical empty failure fallback remains `edit_attention_required`.
  An older adapter returning `waiting` with an empty reason while rendering
  continues to mean active work, avoiding a confirmation loop.
- No Schema, service, thread, queue, runtime, dependency, or platform was
  added.

## Verification

| Check | Result |
| --- | --- |
| All 23 ignored `validate_workflow*.py` validators | **PASS** |
| Automation correctness validator | **PASS** |
| No-AI whole-video and cover-only validator | **5/5 PASS** |
| Manual-confirm/retry/status/type side-effect matrix | **21/21 PASS** |
| Retry reason-drift and legacy empty-code follow-up probes | **PASS** |
| Editing, AI-contract, upload service/resilience/parameter/API regression | **138 passed in 41.32s** |
| Release inventory regression | **100 passed in 16.55s** |
| Full source `compileall`, dependency/lock checks, and `git diff --check` | **PASS** |

No tracked test file was added or modified. The focused fault probes ran from
standard input and wrote only into temporary directories.

## Evidence boundary

These checks used local stores, synthetic media, deterministic fake adapters,
and network guards. They did not download a real URL, call OpenAI, log in to a
platform, upload media, schedule a real publication, or verify moderation or
public visibility. This refactor does not create a release receipt or upgrade
any platform capability evidence.
