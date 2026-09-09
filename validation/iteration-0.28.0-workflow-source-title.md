# Iteration 0.28.0 workflow source-title snapshot

Validated: 2026-09-10. Source baseline: `bb8031ab03c2d31474ea4090dff9a1d23600f1d6`; current working-tree evidence, not a frozen release receipt.

## Result

Workflow Schema 3 makes “use download source title” an explicit, durable workflow choice. The page selects it by default, while a manual title remains available as an explicit override.

- After a download becomes ready, the existing workflow adapter reads the title associated with that exact ready asset and resolves a concrete upload snapshot against the frozen account bindings and current upload capability limits. The workflow service stores the asset reference and snapshot atomically. An X attachment without its own title may fall back only to the parent source title owned by that download job.
- The snapshot stores the common title and each account’s final title in `resolved_upload_json`, bound to `download_asset_id`. Bilibili, Douyin and WeChat Channels limits are applied as 80, 30 and 100 characters. An explicit per-account title remains unchanged.
- Polling, restart, 1–10-output fan-out, upload preparation and cancellation reconciliation reuse the same snapshot. A database trigger rejects changing or clearing the snapshot and rejects retargeting its asset.
- Missing source metadata or an unusable capability enters `workflow_source_metadata_unavailable`. Explicit reconciliation retries local resolution against the already-bound asset without creating another download; an attempted asset change fails closed as `workflow_data_invalid`.
- Exact Schema 1 and 2 databases migrate to Schema 3 with a nullable snapshot column and an immutability trigger. Existing explicit-title rows retain their old meaning and a null snapshot. A forged legacy profile containing the new source-title mode rolls the whole migration back.

The implementation stays within the existing download metadata, workflow service and upload capability boundary. It adds one nullable column and one trigger, with no new table, service, process, thread, queue, runtime or dependency.

The browser remembers only the last successfully applied or saved 32-character preset ID. It restores that preset only after presets, accounts and AI capabilities have all completed their initial read, and only if the user has not edited the form. An initial preset or dependency failure remains visible and leaves the form untouched; a later refresh can recover. The first run still requires account selection and the Bilibili category/tags/copyright choice, Douyin declaration, WeChat Channels mode, cover and scheduling choices that cannot be inferred from a URL. After saving a complete preset, the same configuration can be run again by changing only the URL.

## Validation

| Check | Result |
| --- | --- |
| `validation/local/validate_workflow_source_title.py` | **7/7 PASS**. Covers new Schema 3 and exact Schema 1/2 migration, forged v1/v2 rollback, source/explicit profile and malformed-type rules, malformed resolver-title refusal, source-title preset create/get/materialize round-trip, one-time freeze, the exact post-snapshot/pre-transition crash window, 80/30/100 platform limits, explicit account-title preservation, restart and trigger immutability, metadata retry without another download, asset-retarget refusal and legacy explicit-title behavior. |
| `validation/local/validate_workflow_source_title_browser.cjs` | **PASS**. Covers the default source-title choice, manual override, 320 px layout, successful preset restoration, visible first-read preset/dependency failures and refresh recovery without overwriting user edits. |
| Existing production-browser validators | **PASS**: editorial-glass matrix **44/44**, plus multisegment, readiness and preset browser validators. |
| Existing workflow validators | **PASS**: full-video and full-chain, upload-attention recovery **7/7**, cancellation, and multisegment service. |
| Targeted graph/repository/route tests | **100 passed**. |
| Earlier metadata/asset regression set | **179 passed**. This is prior evidence retained for context, not a new full-suite result. |
| UI/upload focused group final rerun | **187 passed, 3 failed**. All three failures are stale `test_ui_design_system` navigation assertions that omit `/workflows`; the completed group is therefore recorded exactly as non-green. |
| Restart-continuation validator | The first run inside the parallel group failed; an isolated rerun **PASS**. Both observations are retained, and the isolated pass does not erase the first-run repeatability signal. |

Final ignored-validator SHA-256 values: Python `D11D316ED98076573012B295BCBE59CA5E956717931DF1009B2D0199E70224D9`; browser `A38760D618ED3CD0F5D64027C27BA5FDA26753342AB712E7326474EE2FD9C7C6`.

## Evidence boundary

The source-title validators use temporary databases, synthetic adapters and a loopback browser harness. The full-chain checks use generated local media, isolated synthetic AI responses and synthetic upload backends under network guards. No real URL download, OpenAI request, account login, QR scan, platform upload, scheduled publication or public submission was performed.

This is a post-release source milestone. It does not replace the `0592b6f` release receipt and is not a new release receipt. A release claim still requires a clean frozen commit, matching artifacts and independent receipt-bound validation.
