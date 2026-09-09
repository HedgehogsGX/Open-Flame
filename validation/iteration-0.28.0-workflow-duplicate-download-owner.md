# Iteration 0.28.0 duplicate-download owner recovery evidence

Validated: 2026-09-09. Source baseline: `566518a41acfa15c399ad1cef7736ca086bfe573`; current working-tree evidence, not a frozen release receipt.

## Result

A second single-URL workflow no longer fails immediately when the download domain deduplicates it against an existing live job. The duplicate batch remains a durable reference with no job of its own. Its workflow now waits while the original owner is active, reuses the owner's verified ready asset, propagates an allowlisted persisted download `ErrorCode` when the owner fails, and uses the fixed `download_canceled` terminal code when the owner is canceled. No second download job is created.

`BatchRepository.inspect_single_input_download()` opens one deferred SQLite read transaction and observes the workflow batch, reachable ready assets, and ultimate duplicate owner in one snapshot. It validates the single-input duplicate batch counters, every link's full source identity, absent jobs and errors on intermediate duplicate rows, an empty root pointer, a 32-link safety bound, and one current flat download job. The root job must belong to the owner input and batch, match the input's platform/source type/source ID/canonical URL and active generation, and have no graph discovery parent or attachment target; a listed ready asset must belong to that job's source item. Broken links, cycles, source or generation substitution, hidden terminal pointers, invalid state pairs, unknown error codes, and multiple assets fail closed as `download_state_invalid` or the existing precise attention code.

The workflow adapter consumes that snapshot without copying owner state into the duplicate batch. The production page maps the resulting safe download codes to Chinese operator guidance. This slice adds no schema, table, database, service, runtime, background thread, queue, cache, dependency, or test file.

## Validation

| Check | Result |
| --- | --- |
| Pre-fix focused reproduction | **RED as expected**: an active owner produced `failed/download_no_ready_video` for the duplicate workflow instead of remaining in `downloading`. |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_duplicate_download_owner_20260909.py --case all` | **14/14 PASS in 4.70 seconds**. Covers active, ready, persisted failure, cancel, missing failure code, broken link, cycle, input identity mismatch, hidden terminal pointer, excessive chain depth, owner-job source substitution, generation mismatch, forged attachment target and ready-asset source substitution. Every case reopens the stores and asserts exactly one download job. Ignored validator SHA-256: `CAE88DC6C260A780B500BBB89B10AC3899368A11F2A8EB44D8A92D7F61F3AFE6`. |
| `.venv\\Scripts\\python.exe validation/local/validate_workflow_duplicate_owner_snapshot_20260909.py` | **PASS**. A real `WorkerRepository.finish_failure(... NETWORK_ERROR ...)` writer commits after the reader's first SELECT; the in-flight observation consistently returns the earlier `waiting` result and the next observation returns `failed/network_error`. The deferred read does not block the WAL writer, and the job ID/count remain unchanged. Ignored validator SHA-256: `e9ad8481bb2a1afc79ca0ff3019467efb0c066b5a6cab8111d87b35803684fdc`. |
| `pytest -q tests/test_batches.py tests/test_batch_asset_availability.py` | **35 passed in 7.95 seconds**. Existing batch creation, deduplication and ready-asset behavior remains valid. |
| Additional related regression | **49 passed in 16.35 seconds** across `test_batch_asset_availability.py`, `test_batches.py` and `test_download_upload_integration.py`. |
| `validate_workflow_full_chain.py`; `validate_workflow_full_video.py` | **PASS**. Real local stores, Download Worker, Editing/FFmpeg, isolated synthetic AI provider and Bilibili/Douyin/WeChat Channels substitute backends still reach the expected terminal result with networking blocked. These runs took 7.66 and 7.28 seconds. |
| Workflow Chromium regression | **PASS** for multi-segment, preset and readiness validators against a regenerated production fixture. No page error or unexpected network request was observed. |
| Full repository regression | **2438 passed, 12 failed, 8 skipped in 360.39 seconds**. The 12 failures assert historical `0.27.0`, Editing Schema 1, the old three-link navigation, an older sdist exclusion list or older Linux acceptance text. This slice does not modify those test files, so the full suite is not reported as green. |
| `compileall`; `uv pip check`; `git diff --check` | **PASS**. All 24 installed packages are compatible and the changed source compiles without whitespace errors. |
| Staged commit scope | **PASS**. `scripts/verify_commit_scope.py --staged` and `git diff --cached --check` exit 0 for ten explicitly staged files; no path under `tests/` and no ignored validator is staged. |

The validators use temporary roots, the offline fake downloader, a synthetic isolated AI provider, synthetic upload backends and browser route interception. They do not contact a media site, OpenAI, Bilibili, Douyin or WeChat Channels.

## Remaining boundaries

- Store reopen coverage reconstructs every repository/service over the same SQLite files in one Python process. It does not simulate abrupt process termination.
- This result proves duplicate-owner recovery for the current local contracts. It does not prove live extraction, real OpenAI responses or quality, platform acceptance, review, scheduled publication or public visibility.
- The earlier `0592b6f` release receipt does not cover this post-release working tree.
- The next minimal lightweight slice is to carry the already supported speech `rate` through the editing/workflow recipe, UI, presets, authorization fingerprint and provider call. It must preserve old recipe identity at the default `1.0` and must not add a new runtime, queue or automatic paid retry.
