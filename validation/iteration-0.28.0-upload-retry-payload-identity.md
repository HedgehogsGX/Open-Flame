# Iteration 0.28.0 upload retry payload identity validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

Runtime retry-leaf selection and upload-backup auditing now use the same
state-free matcher for the complete immutable publish intent. A retry successor
is accepted only when it preserves the account, source, platform, title,
description, tags, category, mode, copyright/source credit, both cover slots,
publication time and timezone offset, and platform-specific options.

Before this change, the runtime traversal checked only account, source and
platform. A canonical successor whose title had changed could therefore be
selected as the current retry leaf. The focused runtime probe now rejects that
case with `job_retry_lineage_invalid`; the backup path applies the same contract
instead of maintaining a separate field list.

The matcher lives in the existing `uploads/identity.py` module and contains no
database, filesystem, retry traversal or state-transition behavior. No Schema,
service, thread, queue, runtime, dependency or platform was added.

## Verification

| Check | Result |
| --- | --- |
| Focused helper, canonical title-drift and valid 32-edge retry-chain probe | **3/3 PASS** |
| Upload service, resilience and backup regression | **179 passed in 57.93s** |
| Upload platform-parameter and backend regression | **134 passed in 11.88s** |
| Workflow full chain, cancellation, checkpoint-gap, attention and restart validators | **PASS** |
| Full source `compileall` | **PASS** |

No tracked test file was added or modified. The focused probe remains ignored
under `validation/local/`.

## Evidence boundary

These checks used local databases and synthetic upload records. They did not
download a real URL, call OpenAI, log in to Bilibili, Douyin or WeChat Channels,
upload media, schedule a real publication, or verify moderation/public
visibility. This change does not create a release receipt or upgrade real-world
platform capability evidence.
