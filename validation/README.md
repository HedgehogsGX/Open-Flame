# Open-Flame validation index

This directory contains immutable, scope-limited engineering records plus the
schemas and templates for Stage 0 evidence. Each record applies only to the
source/build, environment and inputs named in that file. Counts from different
records must not be combined into a current release claim.

Raw URLs, samples, media, cookies, credentials, logs, screenshots and local
results belong outside Git or under ignored `validation/local/`.

## Current status

- Current architecture-reset supplements: [caption cleanup](iteration-0.28.0-caption-cleanup-errors.md)
  and [release documentation boundary](iteration-0.28.0-release-documentation-boundary.md).
- Workflow request identity: [shared key and frozen request construction](iteration-0.28.0-workflow-request-construction.md).
- Workflow form rules: [shared validation and incomplete absolute schedules](iteration-0.28.0-workflow-shared-validation.md).
- Editing AI retry graph: [one owner for resolution and project cancellation](iteration-0.28.0-editing-ai-retry-ownership.md).
- Editing render retry graph: [shared resolution, snapshot and cancellation fences](iteration-0.28.0-editing-render-retry-ownership.md).
- Workflow retry/cancellation: [shared result application and cancellation tail](iteration-0.28.0-workflow-retry-cancel-tails.md).
- Backup file ownership: [protect foreign targets and close owned descriptors](iteration-0.28.0-backup-file-ownership.md).
- Editing copy ownership: [shared owned-file cleanup and collision protection](iteration-0.28.0-editing-copy-ownership.md).
- Upload lifecycle ownership: [shared manager, startup recovery and native lock handoff](iteration-0.28.0-upload-manager-ownership.md).
- Editing output registration: [validate metadata before copying and retain cleanup ownership](iteration-0.28.0-editing-registration-cleanup.md).
- Backup resource handoff: [owned descriptor wrapping and SQLite connection cleanup](iteration-0.28.0-backup-resource-handoff.md).
- Public backup files: [shared file operations and independent domain policies](iteration-0.28.0-public-backup-files.md).
- Upload cover ownership: [shared format/platform rules and backup audit](iteration-0.28.0-upload-cover-ownership.md).
  The cover working-tree full suite was 2194 passed, 248 failed and 16 skipped;
  the three added failures still patch the former service decoder location.
- Development version: `0.28.0`; the current post-release source has no new
  clean release receipt.
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
  fallback; see the scoped [validation record](iteration-0.28.0-workflow-source-cover-preference.md).
- Upload Schema 4 now records one durable local attempt receipt for every
  claimed job, and upload backup format 3 preserves and audits those receipts.
  `unknown` can only move through the fixed operator conclusions after the
  receipt is read and the corresponding platform backend is checked; only
  `not_accepted` permits a later explicit retry. A receipt is a local tool
  observation, not a platform-signed acknowledgement, work ID, moderation or
  public-visibility proof. Real platform calls in this milestone are **0**,
  and the 2026-09-10 actual app-root record stops at Upload Schema 1→3; Schema
  4 has not been migrated or audited in that actual root. See the scoped
  [attempt receipt record](iteration-0.28.0-upload-attempt-receipts.md).
- Explicit dubbing render retries can reuse individually verified cue WAV
  checkpoints from the same immutable retry lineage; see the scoped
  [validation record](iteration-0.28.0-speech-checkpoint-retry.md).
- Hosted branch run `34680878428` at clean `36cfb56` reached offline pytest on
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

### Architecture reset branch in progress (2026-09-12)

`codex/architecture-reset-ci` is not merged into main and the full CI recovery is
unfinished. These scoped records cover the implemented slices, not the entire goal:

- [Workflow startup rollback](iteration-0.28.0-workflow-start-rollback.md)
- [Primary media errors and cleanup](iteration-0.28.0-media-cleanup-errors.md)
- [Adapter control-record decoding](iteration-0.28.0-adapter-control-decoding.md)
- [Worker assembly and CLI ownership](iteration-0.28.0-worker-entry-boundary.md)

### Architecture and boundaries

- [Current document status and CI policy](iteration-0.28.0-document-status-consolidation.md)
- [Workflow recipe functions](iteration-0.28.0-workflow-recipe-functions.md)
- [Workflow upload form functions](iteration-0.28.0-workflow-upload-form-functions.md)
- [Managed-file reads](iteration-0.28.0-managed-file-read.md) and
  [managed-file identity](iteration-0.28.0-managed-file-identity.md)
- [EditingManager boundary](iteration-0.28.0-editing-manager-boundary.md) and
  [verified media response](iteration-0.28.0-verified-media-response.md)
- [Edit snapshot](iteration-0.28.0-edit-snapshot-observation.md),
  [Upload snapshot](iteration-0.28.0-upload-snapshot-observation.md) and
  [AI snapshot](iteration-0.28.0-ai-snapshot-application.md)
- [Upload identity contract](iteration-0.28.0-upload-identity-contract.md),
  [upload retry payload identity](iteration-0.28.0-upload-retry-payload-identity.md),
  [Upload Schema 4 attempt receipts](iteration-0.28.0-upload-attempt-receipts.md),
  [Workflow profile contract](iteration-0.28.0-workflow-profile-contract.md) and
  [upload metadata contract](iteration-0.28.0-upload-metadata-contract.md)
- [Download HTTP boundary](iteration-0.28.0-download-http-boundary.md) and
  [Workflow manager recovery](iteration-0.28.0-workflow-manager-recovery.md)

### Workflow, editing and frontend

- [AI Workflow](iteration-0.28.0-ai-workflow-evidence.md),
  [AI authorization](iteration-0.28.0-post-release-ai-authorization.md) and
  [AI invocation ledger](iteration-0.28.0-ai-invocation-ledger.md), plus
  [per-cue dubbing retry checkpoints](iteration-0.28.0-speech-checkpoint-retry.md)
- [Workflow presets](iteration-0.28.0-workflow-presets.md),
  [source-caption reuse](iteration-0.28.0-workflow-source-caption-reuse.md),
  [source-cover preference](iteration-0.28.0-workflow-source-cover-preference.md),
  [AI retry lineage](iteration-0.28.0-workflow-ai-retry-lineage.md),
  [relative publish schedules](iteration-0.28.0-workflow-relative-schedules.md),
  [source-title freezing](iteration-0.28.0-workflow-source-title.md),
  [multi-segment workflow](iteration-0.28.0-multisegment-workflow.md) and
  [no-AI whole video](iteration-0.28.0-no-ai-full-video.md)
- [Post-release automation correctness](iteration-0.28.0-post-release-automation-correctness.md),
  [full-video default](iteration-0.28.0-workflow-full-video-default.md) and
  [multi-segment UI](iteration-0.28.0-workflow-multisegment-ui.md)
- [Restart continuation](iteration-0.28.0-workflow-restart-continuation.md),
  [whole-workflow cancellation](iteration-0.28.0-workflow-cancellation.md),
  [duplicate download owner](iteration-0.28.0-workflow-duplicate-download-owner.md)
  and [upload attention recovery](iteration-0.28.0-workflow-upload-attention-recovery.md)
- [Server preflight](iteration-0.28.0-workflow-server-preflight.md),
  [readiness UI](iteration-0.28.0-workflow-readiness-ui.md),
  [platform parameters](iteration-0.28.0-workflow-platform-parameters.md) and
  [speech rate](iteration-0.28.0-speech-rate.md)
- [Editorial Glass frontend](iteration-0.28.0-editorial-glass-frontend.md) and
  [source-cover research and explicit import](iteration-0.28.0-source-cover-research-and-import.md),
  and [translation revision binding](iteration-0.28.0-translation-revision-binding.md)
- [Current local runtime refresh](iteration-0.28.0-local-runtime-refresh.md),
  [synthetic full-chain smoke](iteration-0.28.0-full-chain-smoke.md) and
  [hosted CI execution-chain recovery](iteration-0.28.0-hosted-ci-recovery.md)

## Historical milestone index

Historical records remain point-in-time evidence. In particular, a historical
platform sample, package hash, test count, license inventory or runtime status
does not describe current source unless a current record explicitly revalidates
it.

- [0.27.0 editing workspace](iteration-0.27.0-editing-workspace-evidence.md)
- [0.26.0 upload parameters](iteration-0.26.0-upload-parameters-evidence.md)
- [0.25.0 production frontend](iteration-0.25.0-t16-frontend-evidence.md)
- [0.24.4 upload lifecycle](iteration-0.24.4-upload-data-lifecycle-evidence.md),
  [0.24.3 final review](iteration-0.24.3-final-review.md),
  [0.24.2 integration](iteration-0.24.2-integration-evidence.md),
  [0.24.1 QR login](iteration-0.24.1-qr-login-evidence.md) and
  [0.24.0 first-platform uploader](iteration-0.24.0-upload-evidence.md)
- [0.19.0 short links and Cookie defaults](iteration-0.19.0-short-links-cookie-defaults-evidence.md),
  [0.18.0 concurrent worker](iteration-0.18.0-concurrent-worker-evidence.md),
  [0.17.0 progress](iteration-0.17.0-real-progress-evidence.md),
  [0.16.0 claim fencing/retry](iteration-0.16.0-claim-retry-evidence.md),
  [0.15.0 local app](iteration-0.15.0-local-app-evidence.md) and
  [0.14.0 local Cookie](iteration-0.14.0-local-cookie-evidence.md)
- [0.13.0 capability governance](iteration-0.13.0-capability-governance-evidence.md),
  [0.12.0 auxiliary assets/TikTok](iteration-0.12.0-artifact-tiktok-evidence.md),
  [0.11.0 Bilibili 412](iteration-0.11.0-schema9-bilibili-evidence.md),
  [0.10.0 routing](iteration-0.10.0-multiplatform-routing-evidence.md) and
  [0.10.0 Instagram sample](iteration-0.10.0-instagram-live-evidence.md)
- [0.9.0 local Worker](iteration-0.9.0-local-worker-e2e-evidence.md),
  [0.8.1 real dual sample](iteration-0.8.1-live-platform-evidence.md),
  [0.8.0 local toolchain](iteration-0.8.0-local-toolchain-evidence.md),
  [0.7.2 runtime logging](iteration-0.7.2-runtime-logging-evidence.md),
  [0.7.1 debug/license](iteration-0.7.1-debug-use-license-evidence.md),
  [0.7 short-link/Cookie/Linux](iteration-0.7-short-link-cookie-linux-offline-evidence.md)
  and [0.6 graph v2](iteration-0.6-graph-v2-offline-evidence.md)
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
