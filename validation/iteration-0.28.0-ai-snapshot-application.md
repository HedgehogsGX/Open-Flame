# Iteration 0.28.0 AI snapshot application validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

Workflow AI observations now pass through the public, state-free
`workflows/snapshots.py` classifier and one context-aware service application
method. Manual confirmation, ordinary/restart advancement, and remote-ledger
reconciliation no longer maintain three separate status mappings.

The classifier only interprets adapter facts. Authorization flags, explicit
confirmation, automatic reason allowlists, ledger exception handling, CAS,
and whether a caller may continue advancing remain in `WorkflowService`.

## Compatibility and correction

- `waiting` and `review` retain their reason and return to
  `awaiting_ai_review`; an absent adapter reason is normalized to the historical
  empty value instead of reaching the database as `NULL`.
- `failed` and `attention` retain their reason, with the historical
  `ai_review_required` fallback.
- A valid `ready` observation checkpoints the exact plan and draft references
  before entering `awaiting_edit_confirmation`.
- Missing ready references retain the prior contextual distinction:
  manual/automatic paths use `workflow_data_invalid`, while ledger
  reconciliation uses `workflow_domain_data_invalid`.
- Ledger reconciliation with an unchanged attention reason does not create a
  second state revision.
- Unknown adapter status during manual confirmation now fails closed as
  `workflow_domain_data_invalid`. Previously it was treated as another waiting
  state and `confirm_ai()` immediately called the adapter a second time through
  `advance()`. The direct probe now records exactly `[(True, True)]` and no
  automatic follow-up call.
- No Schema, service, thread, queue, runtime, dependency, or platform was added.

## Verification

| Check | Result |
| --- | --- |
| AI authorization and exact manual confirmation | **PASS** |
| AI invocation-ledger attention recovery | **PASS** |
| Workflow restart continuation and automatic reason allowlists | **PASS** |
| v0.28 and automation correctness validators | **PASS** |
| Multi-segment service, retry-leaf replacement, and Schema migration | **PASS** |
| Synthetic three-platform full-chain and full-video workflows | **PASS** |
| No-AI whole-video and cover-only validator | **5/5 PASS** |
| Speech-rate propagation and boundaries | **10/10 PASS** |
| Uncheckpointed edit/upload cancellation | **PASS** |
| Unknown manual AI status single-call probe | **PASS** |
| Ledger waiting observation with absent reason | **PASS** |
| Full source/script `compileall`, dependency check, and `git diff --check` | **PASS** |

No tracked test file was added or modified. The new fault probe ran from
standard input and wrote only into a temporary directory.

## Evidence boundary

These checks used local stores, synthetic media, deterministic fake adapters,
and network guards. They did not download a real URL, call OpenAI, log in to a
platform, upload media, schedule a real publication, or verify moderation or
public visibility. This refactor does not create a release receipt or upgrade
any platform capability evidence.
