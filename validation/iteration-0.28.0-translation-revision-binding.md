# Iteration 0.28.0 translation revision binding evidence

Validated: 2026-09-10. Current working-tree evidence, not a frozen release receipt.

## Result

Ready translation recipes can now carry the exact approved `revision_id` selected by the operator. New manual draft saves and new AI-ready workflow drafts persist this 32-character lowercase hexadecimal ID. The service verifies that the revision belongs to the current project, is an approved translation, matches the recipe target language/provider/model, has an approved transcription parent with the expected source language unless the recipe uses `auto`, and preserves the expected cue structure. Plan creation requires a supplied `timeline_revision_id` to equal the recipe ID, then freezes the translation revision, its cue digest, its parent transcription revision and the parent cue digest in the existing `plan_timeline_bindings` row. Plan API summaries expose the bound revision and cue digest for review.

Compatibility remains explicit. A missing `revision_id` is omitted from canonical recipe JSON, so existing draft JSON and SHA-256 identities remain unchanged. When loading a legacy ready recipe without an ID, the Editing page auto-selects only if exactly one approved revision satisfies the complete project, target/provider/model, approved-parent, source-language and cue-shape contract. Zero matches or multiple matches remain unselected and fail closed until the operator explicitly chooses a revision; the next save upgrades the draft with that exact ID. A recipe that already contains an ID restores only that exact approved revision and does not fall back to another match.

Existing in-flight workflow drafts that were already AI-ready before this change remain readable and are not rewritten, avoiding an idempotency conflict. Newly materialized AI-ready workflow drafts include the exact translation ID. Run-specific revision IDs are rejected in reusable workflow profiles and presets. The workflow page links to the Editing plan review section so the frozen binding can be inspected.

A ready translation may now produce an inspectable `review` plan even while dubbing is still `review` or `blocked`; the exact translation binding is still frozen at plan creation. `confirm_plan` remains the execution gate and refuses to queue the plan until dubbing is ready, or rejects a blocked dubbing definition. This preserves review visibility without weakening execution approval.

This slice adds no database table, schema migration, runtime, service, process, thread, queue or dependency. It reuses canonical recipe JSON, the existing timeline tables and the existing immutable `plan_timeline_bindings` table. No tracked test file was added or modified.

## Validation

| Check | Result |
| --- | --- |
| Focused translation-revision service validator | **17/17 PASS**. Covers canonical compatibility, exact save/reload, missing/wrong/rejected/cross-project revision rejection, exact plan binding, legacy plan compatibility and mixed-state review-plan behavior. |
| Strict Editing browser validator generated from current production HTML | **PASS**. Covers exact selection/save/reload, ambiguous legacy failure, plan request identity, binding summary and confirmation gate. |
| Four existing browser regressions | **PASS**. Editing speech-rate plus Workflow preset, multi-segment and readiness behavior remain valid; the single-candidate legacy Editing fixture also continues to restore safely. |
| Existing full-chain, full-video-default, speech-rate and multi-segment validators | **PASS**. The exact revision binding does not break the established local synthetic workflow paths. |
| `pytest -q tests/test_editing_ai_contracts.py tests/test_editing_service.py tests/test_editing_media.py tests/test_editing_api.py` | **69 passed, 2 failed**. Both failures are existing assertions that expect historical Editing Schema 1 while the product remains Editing Schema 4; test files were not changed. |
| `python -m compileall -q src`, current Editing/Workflow inline JavaScript syntax, `uv lock --check --offline`, `uv pip check`, `git diff --check` | **PASS**. |

Ignored validator SHA-256 values: service validator `17BE550E55721762896D3425D6BC1CDAD6C8B79A095474A4B52A3031A7576189`; strict browser validator `FD22A8C504A69CE953495D872B7268C7F9A1E17F6F7CFA86699CEFC2A4F92036`. The browser validator loaded production `EDITING_HTML` SHA-256 `597b4cd790be76b9f2c83cb809634b517514ca6ed466a1555343a61e5787ee46`.

The validators use local temporary roots, synthetic providers, route interception and loopback browser fixtures. They did not make a real OpenAI request, download from a real media site, upload or submit to Bilibili, Douyin or WeChat Channels, publish content, or conduct a human listening review.

## Compatibility and remaining boundaries

- Old recipes without `revision_id` remain readable and keep their previous canonical JSON and SHA-256. Their UI recovery is deliberately unique-only; ambiguous records require an explicit new selection and save.
- Old plans that already contain an immutable timeline binding remain readable and confirmable under their existing gate. Existing AI-ready workflow drafts without the field remain usable and are not silently rewritten.
- An older binary whose exact-key parser does not know `revision_id` cannot read a newly saved recipe containing the field. Rollback after such a save requires the newer binary or a compatible backup.
- Exact local binding proves which approved revision a plan uses. It does not prove translation quality, voice quality, provider acceptance, billing, download compatibility, platform acceptance, scheduled publication or public visibility.
- The earlier `0592b6f` release receipt does not cover this post-release working tree. A release claim still requires a frozen clean commit and a new external receipt for the same artifacts.
