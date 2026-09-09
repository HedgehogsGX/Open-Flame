# Iteration 0.28.0 upload snapshot observation validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

All four Workflow upload-observation paths now use the public, state-free
`classify_upload_snapshot()` contract and one service operation that preserves
the order `inspect → current retry-leaf checkpoint/CAS → classify`. The finite
facts are ready, attention, waiting for draft confirmation, already-confirmed
active work, and invalid domain data.

The classifier does not authorize upload confirmation. Manual confirmation,
the immutable automatic-confirm flag, the positive restart reason allowlist,
the one-round restart barrier, account/session revalidation, outcome recording,
and cancellation remain in their existing callers and domains.

## Compatibility and corrections

- A changed retry leaf is checkpointed into ordered Workflow outputs with a new
  revision before any confirmation. Manual confirmation returns immediately so
  the successor must be reviewed against that revision.
- A waiting set with at least one draft remains behind upload confirmation. A
  set containing only queued/running work resumes `uploading` without invoking
  `confirm_uploads`, including the domain-commit/Workflow-checkpoint gap.
- Automatic confirmation still requires both the persisted reason and the
  observed reason to be in the existing positive allowlist. Retry, unknown,
  legacy, and conflicting persisted reasons remain blocked.
- `uploading` still returns to `awaiting_upload_confirmation` for one complete
  advance round before an eligible restart confirmation can run.
- Submitted, draft-saved, and mixed outcomes still pass through the existing
  acknowledgement mapper; a missing outcome remains `upload_outcome_missing`.
- Legitimate upload attention, including `upload_result_unknown`, retains its
  exact reason. An unknown snapshot status now fails closed as
  `workflow_domain_data_invalid`.
- Snapshot status and reason must be strings, and `needs_confirmation` must be
  a real boolean. Falsey non-string reasons cannot be normalized into the empty
  automatic-confirm allowlist entry.
- Manual `confirm_upload()` previously called the mutation-capable upload
  confirmation method for both an unknown status and a waiting snapshot marked
  `needs_confirmation=false`. Both cases now make zero confirmation calls.
- No Schema, service, thread, queue, runtime, dependency, or platform was added.

## Verification

| Check | Result |
| --- | --- |
| Workflow upload attention recovery | **7/7 PASS** |
| Upload service and resilience regression | **43 passed in 19.39s** |
| Restart continuation, automatic reason policy, and v0.28 | **PASS** |
| Multi-segment service, retry-leaf replacement, and Schema migration | **PASS** |
| Workflow cancellation, concurrency, adapter, and upload batch cancellation | **PASS** |
| Uncheckpointed edit/upload cancellation | **PASS** |
| Synthetic three-platform full-chain and full-video workflows | **PASS** |
| No-AI whole-video and cover-only validator | **5/5 PASS** |
| Seven-case confirmation/leaf/restart/reason probe | **7/7 PASS** |
| Malformed status/reason/confirmation-type probe | **3/3 PASS; 0 confirmations** |
| Release inventory regression | **100 passed in 13.28s** |
| Full source/script `compileall`, dependency check, and `git diff --check` | **PASS** |

No tracked test file was added or modified. The new fault and side-effect probe
ran from standard input and wrote only into temporary directories.

## Evidence boundary

These checks used local stores, synthetic media, deterministic fake adapters,
and network guards. They did not download a real URL, call OpenAI, log in to a
platform, upload media, schedule a real publication, or verify moderation or
public visibility. This refactor does not create a release receipt or upgrade
any platform capability evidence.
