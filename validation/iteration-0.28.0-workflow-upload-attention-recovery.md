# Iteration 0.28.0 workflow upload attention recovery

Validated: 2026-09-10. Source baseline: `9eead4d71df976392df97a2be660930f1910ce34`; current working-tree evidence, not a frozen release receipt.

## Result

A workflow that entered `attention_required` after upload drafts had been created can now recover without bypassing the upload confirmation boundary. The workflow upload snapshot explicitly distinguishes a waiting set that still contains at least one `draft` from a waiting set containing only already-confirmed `queued` or `running` jobs.

- Any current upload leaf still in `draft` sets `needs_confirmation=true`. Manual workflows return to `awaiting_upload_confirmation`; automatic workflows cross that gate only when the immutable profile already contains `auto_confirm_upload=true` and the existing positive review-code allowlist permits it.
- A set containing only `queued` or `running` non-terminal leaves sets `needs_confirmation=false`. Recovery resumes `uploading` without a duplicate domain confirmation. This also closes the checkpoint gap where the upload-domain transaction committed but the workflow transition did not.
- Mixed draft plus active or successful leaves still require confirmation for the remaining drafts. `confirm_many()` continues to queue only current drafts in one transaction and treats queued, running, submitted and draft-saved leaves idempotently.
- Terminal submitted, draft-saved or valid mixed outcomes remain terminal. Failed, canceled, unknown, account-invalid and malformed job sets keep their previous fail-closed result.

`UploadSnapshot.needs_confirmation` defaults to `true`, so an older adapter that returns an unclassified waiting snapshot cannot silently bypass confirmation. `LocalWorkflowAdapter` supplies the exact value from the current upload leaf states. `WorkflowService` consumes it consistently while recovering from attention, while reconciling an awaiting confirmation checkpoint, and while polling an uploading workflow.

The baseline fault injection reproduced the defect before the change: both a manual draft and a pre-authorized automatic draft recovered directly as `uploading`; the automatic case had not invoked the domain confirmation at all. A first conservative fix restored the gate for every waiting snapshot, then review found that it would make already-queued or running manual work request a redundant confirmation. The final explicit snapshot field preserves the gate for drafts and resumes already-confirmed work directly.

This slice adds no database table, schema migration, service, process, thread, queue, runtime or dependency. It changes no tracked test file.

## Validation

| Check | Result |
| --- | --- |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_upload_attention_recovery.py` | **7/7 PASS**. Covers manual and automatic draft recovery, already-confirmed manual and automatic recovery, a committed-domain/workflow checkpoint gap, terminal completion during attention, and direct classification of draft-only, draft plus active/success, active-only, active plus success, submitted and mixed terminal sets. |
| Existing automation and workflow policy validators | **PASS**: `validate_automation_correctness.py`, `validate_workflow_v028.py`, `validate_workflow_restart_continuation.py` and `validate_workflow_multisegment_service.py`. |
| Existing local full-chain validators | **PASS**: `validate_workflow_full_chain.py` and `validate_workflow_full_video.py`. Both retained automatic restart confirmation, the exact AI/edit path, three-platform fan-out and `submission_acknowledged`. |
| `.venv\\Scripts\\python.exe -m pytest -q tests/test_upload_service.py tests/test_upload_resilience.py` | **43 passed in 19.40 seconds**. Existing upload confirmation and recovery behavior remains green; test files were not changed. |
| `.venv\\Scripts\\python.exe -m pytest -q tests/test_release.py` | **100 passed in 12.44 seconds**. The reviewed release inventory and packaging exclusions remain valid. |
| `python -m compileall -q src`, `uv lock --check --offline`, `uv pip check`, `git diff --check` | **PASS**. |

Final ignored validator SHA-256: `C1D64C8E03FCDCBFEDAF457771D10DBCA8B746334546E7139E6A09388A0DA9E0`.

The validator uses temporary workflow stores and synthetic adapters. The full-chain validators use generated local media, isolated synthetic AI responses and a synthetic three-platform backend under network guards. No real OpenAI request, login, QR scan, real media-site download, upload, scheduled publication or public submission was performed.

## Remaining boundaries

- This evidence proves local state and confirmation semantics only. It does not prove Bilibili, Douyin or WeChat Channels accepted metadata, media, covers, schedules or a publication request.
- The default application root still needs its AI runtime prepared and its old upload runtime rebuilt before the current automatic chain can run there. Existing local account rows do not prove that remote sessions remain valid.
- The earlier `0592b6f` release receipt does not cover this post-release source change. A release claim still requires a new clean commit, frozen artifacts and a matching external receipt.
