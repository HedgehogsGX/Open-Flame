# Open-Flame validation index

This directory contains immutable, scope-limited engineering records plus the
schemas and templates for Stage 0 evidence. Each record applies only to the
source/build, environment and inputs named in that file. Counts from different
records must not be combined into a current release claim.

Raw URLs, samples, media, cookies, credentials, logs, screenshots and local
results belong outside Git or under ignored `validation/local/`.

Source ZIPs and sdists retain this index, the two Stage 0 CSV templates and the
Linux operator guide. The 118 historical reports stay in Git; links below bind
them to commit `7b48a9fe4dae09279a3e986642af68263386e796`. They require online
access when reading an extracted source package.

## PR #2 review follow-up (2026-09-15)

Read [the review](https://github.com/HedgehogsGX/Open-Flame/pull/2#issuecomment-5661999057)
against clean `7b48a9f`, then verified its push and PR runs: all eight jobs are
completed/success. The comment's failed `0dfcf82` job is historical; its exact
hosted failure cause remains unproven.

The original gate accepted a previously approved patch newly staged in an
unrelated repository (exit 0). The same real-Git reproducer now rejects it
(exit 1). Nine isolated history scenarios pass: existing PR, production-only
increment, first merge, deletion-only cleanup, new test edits, copied history,
advanced base and later patch replay. There are no tracked test changes.
The reusable patch hashes are removed; the remaining bridge binds the original
base and already-reviewed commit, never a new staged patch. Once the merge base
advances it expires and its two endpoints/helper can be deleted.

The source inventory falls from 353 to 235 files. Historical report entries fall
from 118 (699,877 bytes) to zero; the four user/developer entry files remain.
All rewritten historical links resolve to paths verified in the fixed commit,
and local documentation targets still have to exist in the shipped inventory.
Temporary reproductions and detailed results are under ignored
`validation/local/pr-feedback-20260915-01/`.

Validation: existing release, CI and yt-dlp modules reported **215 passed,
5 skipped** in 18.39 seconds. The five pre-existing bundle checks skipped
because the pinned local yt-dlp/media bundles are absent. They did not exercise
the user's real YouTube cases. CI definition, locked dependency resolution and
whitespace checks passed. An actual isolated build and archive verification
passed: source ZIP 235 files, sdist 236 (including PKG-INFO), wheel 175;
neither source archive contains historical reports or tests. Direct Hatch
building also excludes report/test sentinels actually present in its isolated
input. The legacy `7575773` release still verifies; missing local documentation
and newly listed historical reports are rejected. These are development build
checks, not a new final installation receipt or platform acceptance.

Remaining review boundaries: the repository owner's PR confirmation of prior
test-maintenance authorization is not supplied by code or by this record;
real Bilibili upload compatibility, platform acceptance, macOS execution and
original-root migration remain unverified. User-reported YouTube results are
6/10; the four failing URLs, errors and execution version have not been supplied
and are absent from the PR comment. No YouTube fix or improved pass rate is
claimed from the review changes.

## Current status

- Exact test maintenance: [cover completion waits, diagnostic child lock budget, unchanged assertions and 73-test regression](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-async-test-maintenance.md).
- Local recovery supplements: [cross-process locks, restored-Python runtime rebuild and actual Chromium process cleanup](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-local-recovery-validation.md).
- Rollback preparation: [old-source environment copies, normal startup/stop, schema compatibility and encrypted private-state verification](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-rollback-environment-drill.md).
- Main merge gate: [exact proposal, observed permission boundary and outstanding remote verification](../docs/MAIN_MERGE_GATE.md).
- Application data: [observed Download/Upload protection, independent restore, normal startup and original-root preservation](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-application-data-recovery.md).
- Runtime logging: [eight registered route templates, bounded log fields and real HTTP verification](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-runtime-route-logging.md).
- Frozen load and CI: [9822454 storage 300-cycle run, normal stop and pending PR cover-test wait correction](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-storage-soak-and-ci-follow-up.md).
- Upload reconnection: [refresh the current session, clear recovered poll errors and preserve action feedback](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-poll-reconnection.md).
- Upload storage: [configurable free-space floor, incoming/copy checks and bounded exhaustion cleanup](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-storage-threshold.md).
- Release drill: [1540a7e isolated Setup/Start/stop, four-page browser checks, old Upload copy migration/restore and workflow notice correction](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-release-readiness-drill.md).
- Release preparation: [live 7575773 baseline, green push/PR CI, matching installation receipt and pending merge gate](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-release-readiness-baseline.md).
- CI recovery history: [bounded Windows SQLite snapshot fixes and approved test maintenance](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-ci-contract-maintenance.md).
- Current architecture-reset supplements: [caption cleanup](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-caption-cleanup-errors.md)
  and [release documentation boundary](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-release-documentation-boundary.md).
- Workflow request identity: [shared key and frozen request construction](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-request-construction.md).
- Workflow form rules: [shared validation and incomplete absolute schedules](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-shared-validation.md).
- Editing AI retry graph: [one owner for resolution and project cancellation](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editing-ai-retry-ownership.md).
- Upload reconciliation polling: [retained controls and the historical test-maintenance proposal](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-reconciliation-node-retention.md).
- Cross-page upload rules: [shared text, tags and schedules with retained page ownership](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-cross-page-upload-rules.md).
- CLI shared rules: [normalized paths and existing worker output](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-cli-shared-parsing-output.md).
- Upload source handoff: [protected consumption, staged bytes and post-execution results](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-source-handoff.md).
- Editing render retry graph: [shared resolution, snapshot and cancellation fences](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editing-render-retry-ownership.md).
- Workflow retry/cancellation: [shared result application and cancellation tail](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-retry-cancel-tails.md).
- Backup file ownership: [protect foreign targets and close owned descriptors](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-backup-file-ownership.md).
- Editing copy ownership: [shared owned-file cleanup and collision protection](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editing-copy-ownership.md).
- Upload lifecycle ownership: [shared manager, startup recovery and native lock handoff](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-manager-ownership.md).
- Editing output registration: [validate metadata before copying and retain cleanup ownership](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editing-registration-cleanup.md).
- Backup resource handoff: [owned descriptor wrapping and SQLite connection cleanup](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-backup-resource-handoff.md).
- Public backup files: [shared file operations and independent domain policies](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-public-backup-files.md).
- Upload cover ownership: [shared format/platform rules and backup audit](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-cover-ownership.md).
  The cover working-tree full suite was 2194 passed, 248 failed and 16 skipped;
  the three added failures still patch the former service decoder location.
- Development version: `0.28.0`; post-release commit `7575773` has a matching
  clean source/wheel installation receipt. Later commits need their own receipt.
- The `0592b6f` receipt applies only to that frozen build.
- Real OpenAI responses, human listening quality, Bilibili/Douyin/WeChat
  Channels upload acceptance, scheduled publication and public visibility are
  unverified for the current source.
- The Download pipeline already registers the thumbnail selected by pinned
  yt-dlp. The current source can preview/download that **source cover returned
  by the platform** and explicitly copy JPEG/PNG/WebP into Upload's managed
  cover library. This wording does not claim a creator master or lossless
  image. Bilibili/Douyin real extraction remains unverified, and yt-dlp has no
  dedicated WeChat Channels extractor.
- Workflow now has an explicit source-cover preference with a generated-cover
  fallback; see the scoped [validation record](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-source-cover-preference.md).
- Upload Schema 4 now records one durable local attempt receipt for every
  claimed job, and upload backup format 3 preserves and audits those receipts.
  `unknown` can only move through the fixed operator conclusions after the
  receipt is read and the corresponding platform backend is checked; only
  `not_accepted` permits a later explicit retry. A receipt is a local tool
  observation, not a platform-signed acknowledgement, work ID, moderation or
  public-visibility proof. Real platform calls in this milestone are **0**,
  and the 2026-09-10 actual app-root record stops at Upload Schema 1→3; Schema
  4 has not been migrated or audited in that actual root. See the scoped
  [attempt receipt record](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-attempt-receipts.md).
- Explicit dubbing render retries can reuse individually verified cue WAV
  checkpoints from the same immutable retry lineage; see the scoped
  [validation record](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-speech-checkpoint-retry.md).
- Historical failures before CI recovery: hosted branch run `34680878428` at clean `36cfb56` reached offline pytest on
  Windows/Linux and CPython 3.12/3.13; all four jobs failed. Local failures
  include old internal interfaces, HTTP, Schema and fixture contracts, but
  their causes are not a complete classification of hosted logs. This run
  does not validate the later cover slice or imply a green build. Cover commit
  `e4e84fd` run `34683683053` also completed with failure; the subsequent
  frontend record reports scoped checks, not full CI success.
- External testing starts with [`TESTING.md`](../TESTING.md). Use the
  [`Debug guide`](../docs/DEBUG_GUIDE.md) and return findings with the
  [`external tester handoff template`](../docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md).

The concise current development state, risks and next actions live in
[`HANDOFF.md`](../HANDOFF.md). The implementation sequence lives in
[`docs/FOLLOW_UP_EXECUTION_PLAN.md`](../docs/FOLLOW_UP_EXECUTION_PLAN.md).

## Current 0.28.0 evidence

### Architecture reset implementation and CI recovery (2026-09-14 snapshot)

`codex/architecture-reset-ci` is not merged into main. Its `7575773` push and PR
runs each passed all four CI jobs. Merge enforcement and real business acceptance
remain separate work. These records cover the implemented slices:

- [Workflow startup rollback](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-start-rollback.md)
- [Primary media errors and cleanup](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-media-cleanup-errors.md)
- [Adapter control-record decoding](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-adapter-control-decoding.md)
- [Worker assembly and CLI ownership](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-worker-entry-boundary.md)

### Architecture and boundaries

- [Current document status and CI policy](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-document-status-consolidation.md)
- [Workflow recipe functions](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-recipe-functions.md)
- [Workflow upload form functions](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-upload-form-functions.md)
- [Managed-file reads](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-managed-file-read.md) and
  [managed-file identity](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-managed-file-identity.md)
- [EditingManager boundary](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editing-manager-boundary.md) and
  [verified media response](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-verified-media-response.md)
- [Edit snapshot](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-edit-snapshot-observation.md),
  [Upload snapshot](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-snapshot-observation.md) and
  [AI snapshot](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-ai-snapshot-application.md)
- [Upload identity contract](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-identity-contract.md),
  [upload retry payload identity](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-retry-payload-identity.md),
  [Upload Schema 4 attempt receipts](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-attempt-receipts.md),
  [Workflow profile contract](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-profile-contract.md) and
  [upload metadata contract](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-upload-metadata-contract.md)
- [Download HTTP boundary](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-download-http-boundary.md) and
  [Workflow manager recovery](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-manager-recovery.md)

### Workflow, editing and frontend

- [AI Workflow](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-ai-workflow-evidence.md),
  [AI authorization](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-post-release-ai-authorization.md) and
  [AI invocation ledger](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-ai-invocation-ledger.md), plus
  [per-cue dubbing retry checkpoints](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-speech-checkpoint-retry.md)
- [Workflow presets](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-presets.md),
  [source-caption reuse](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-source-caption-reuse.md),
  [source-cover preference](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-source-cover-preference.md),
  [AI retry lineage](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-ai-retry-lineage.md),
  [relative publish schedules](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-relative-schedules.md),
  [source-title freezing](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-source-title.md),
  [multi-segment workflow](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-multisegment-workflow.md) and
  [no-AI whole video](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-no-ai-full-video.md)
- [Post-release automation correctness](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-post-release-automation-correctness.md),
  [full-video default](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-full-video-default.md) and
  [multi-segment UI](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-multisegment-ui.md)
- [Restart continuation](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-restart-continuation.md),
  [whole-workflow cancellation](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-cancellation.md),
  [duplicate download owner](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-duplicate-download-owner.md)
  and [upload attention recovery](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-upload-attention-recovery.md)
- [Server preflight](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-server-preflight.md),
  [readiness UI](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-readiness-ui.md),
  [platform parameters](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-workflow-platform-parameters.md) and
  [speech rate](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-speech-rate.md)
- [Editorial Glass frontend](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-editorial-glass-frontend.md) and
  [source-cover research and explicit import](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-source-cover-research-and-import.md),
  and [translation revision binding](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-translation-revision-binding.md)
- [Current local runtime refresh](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-local-runtime-refresh.md),
  [synthetic full-chain smoke](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-full-chain-smoke.md) and
  [hosted CI execution-chain recovery](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-hosted-ci-recovery.md)

## Historical milestone index

Historical records remain point-in-time evidence. In particular, a historical
platform sample, package hash, test count, license inventory or runtime status
does not describe current source unless a current record explicitly revalidates
it.

- [0.27.0 editing workspace](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.27.0-editing-workspace-evidence.md)
- [0.26.0 upload parameters](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.26.0-upload-parameters-evidence.md)
- [0.25.0 production frontend](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.25.0-t16-frontend-evidence.md)
- [0.24.4 upload lifecycle](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.24.4-upload-data-lifecycle-evidence.md),
  [0.24.3 final review](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.24.3-final-review.md),
  [0.24.2 integration](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.24.2-integration-evidence.md),
  [0.24.1 QR login](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.24.1-qr-login-evidence.md) and
  [0.24.0 first-platform uploader](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.24.0-upload-evidence.md)
- [0.19.0 short links and Cookie defaults](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.19.0-short-links-cookie-defaults-evidence.md),
  [0.18.0 concurrent worker](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.18.0-concurrent-worker-evidence.md),
  [0.17.0 progress](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.17.0-real-progress-evidence.md),
  [0.16.0 claim fencing/retry](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.16.0-claim-retry-evidence.md),
  [0.15.0 local app](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.15.0-local-app-evidence.md) and
  [0.14.0 local Cookie](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.14.0-local-cookie-evidence.md)
- [0.13.0 capability governance](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.13.0-capability-governance-evidence.md),
  [0.12.0 auxiliary assets/TikTok](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.12.0-artifact-tiktok-evidence.md),
  [0.11.0 Bilibili 412](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.11.0-schema9-bilibili-evidence.md),
  [0.10.0 routing](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.10.0-multiplatform-routing-evidence.md) and
  [0.10.0 Instagram sample](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.10.0-instagram-live-evidence.md)
- [0.9.0 local Worker](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.9.0-local-worker-e2e-evidence.md),
  [0.8.1 real dual sample](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.8.1-live-platform-evidence.md),
  [0.8.0 local toolchain](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.8.0-local-toolchain-evidence.md),
  [0.7.2 runtime logging](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.7.2-runtime-logging-evidence.md),
  [0.7.1 debug/license](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.7.1-debug-use-license-evidence.md),
  [0.7 short-link/Cookie/Linux](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.7-short-link-cookie-linux-offline-evidence.md)
  and [0.6 graph v2](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.6-graph-v2-offline-evidence.md)
- [Apache-2.0 migration record](apache-2.0-license-migration-evidence.md)

## Stage 0 evidence procedure

1. Copy `sample_manifest.template.csv` to a private location outside Git.
2. Use the Stage 0 v3 headers exactly, with no duplicate columns and the exact
   row width. Both files require `job_kind`; the results file also requires the
   full `product_version` printed by the exact package under test. Old columns,
   bare release versions, mismatched build hashes or missing product identity
   fail closed.
3. Add at least 10 current, public, browser-accessible positive samples for each
   `platform × source_type × job_kind` cell, plus separate negative samples.
4. For `download`, count published and fully verified media assets. For
   `discover`, count unique child/source items in the immutable snapshot.
5. In the exact checkout or installation under test, run
   `uv run video-download-validation --print-product-identity` and copy the
   returned identity verbatim into every result row.
6. Record every execution in `results.template.csv` format.
7. Generate a URL-redacted report:

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

A cell becomes eligible for explicit approval only when it has at least 10
public positive samples, separate negative evidence, and the latest three
complete executions for the exact same
`platform × source_type × job_kind × adapter × downloader_version × environment × product_version`
all meet the positive threshold while every negative reaches its expected
terminal outcome.

`product_version` has the form `<release>+build.sha256.<64 hex>` and binds the
release to the importable package payload, excluding regenerable PEP 3147
`__pycache__/*.pyc`. Package links, reparse points, special files or inventory
drift fail closed. A newer partial run fails closed. Samples are deduplicated by
framed `platform/source_type/source_id` plus `job_kind`; aliases for one media
item are not independent samples. Bilibili Stage 0 accepts BV identifiers only.
Aggregate reports omit raw URLs, paths, sample IDs and source fingerprints.

`video-download-validation` evaluates recorded evidence; it does not launch
yt-dlp or write the database. `video-download-capabilities import` requires a
ready Schema 11 single-linked ordinary database, re-evaluates the private CSV
and appends qualified or insufficient evidence, but never auto-approves it.
Approve/revoke is a separate local CAS-protected decision and does not enable a
Worker, short-link or graph gate. Import and approval require the exact current
build identity and recheck it before commit. Historical evidence remains
readable and revocable but is not current-build eligibility. Application rules
provide an auditable boundary; they are not cryptographic execution attestation
against a local administrator who rewrites code and data.
