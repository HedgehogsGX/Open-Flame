# Iteration 0.28.0 speech-rate evidence

Validated: 2026-09-09. Frozen comparison baseline: `9a773c41c315cf308facb6a071c1f21ccd8935bb`; current working-tree evidence, not a frozen release receipt.

## Result

The Editing page and the URL workflow page now accept a finite speech rate from `0.88` through `1.12`, preserve arbitrary valid precision such as `1.005`, and freeze the value with the selected standard voice. Workflow presets restore the value exactly. Plan, confirmation and workflow record summaries show the actual voice and unrounded rate. Changing the workflow rate immediately revokes the prior data-egress acknowledgement and recalculates readiness.

`DubbingSpec` and the strict HTTP request model reject booleans, strings, NaN, infinity, huge integers and out-of-range values. The recipe, `SpeechOptions`, isolated protocol and provider boundaries compare the bounded range before converting to `float`, so a JSON integer such as `10**400` produces the layer's stable invalid-input code instead of an uncaught `OverflowError`; the same conversion-order defect was removed from the existing runtime-manifest numeric helper. A missing or explicit default `1.0` is normalized to a float and omitted from canonical recipe JSON, preserving the JSON and SHA-256 of existing drafts, plans, workflows and presets. A non-default value changes the recipe/profile SHA-256. It then follows the existing `AiRenderProcessor → SpeechOptions → protocol → bridge/worker → provider` path and becomes the OpenAI Speech `speed`; the validated request fingerprint changes while the operation authorization SHA-256 remains stable.

The existing cue-slot guard remains authoritative. A deliberately selected faster rate can make a small synthetic overflow fit, while a larger overflow still returns `ai_speech_timing_overflow`. The application does not perform a second paid Speech request, truncate audio or move later cues automatically.

Editing draft hydration now waits for the saved capability and approved timeline context before restoring translation, dubbing, provider/model, voice and rate. An unavailable saved capability remains visibly unselected instead of silently falling back to the first provider. Once the operator changes or clears a restored choice, periodic record polling preserves that newer local choice; clearing translation also clears and disables dubbing until translation is selected again.

This slice adds no database, schema, runtime, service, process, background thread, queue, cache, dependency or tracked test file. Its two new focused validators remain under the ignored `validation/local/` tree.

## Validation

| Check | Result |
| --- | --- |
| Same 10-check validator against frozen `HEAD=9a773c41` | **8 RED / 2 PASS as expected**. The old source rejected recipe/API/preset `rate`, always sent `1.0` to the provider and accepted `SpeechOptions(rate=True)`. The already-existing request fingerprint and timing-overflow checks were the two passes. |
| `.venv\Scripts\python.exe validation/local/validate_workflow_speech_rate.py` | **10/10 PASS**, repeated after the final guard-order fix. Covers default identity, numeric boundaries including `10**400` across recipe, direct dataclass, `SpeechOptions`, isolated protocol and provider parsing, plus the matching runtime numeric helper; authorization/recipe/profile/request fingerprints; preset and real ASGI API round trips; stable 422 with zero side effects for invalid API input; renderer-to-provider forwarding; and fail-closed timing overflow. Ignored validator SHA-256: `4AF6C76B673C31B7F0F20892890396A16F14F7E9AE65D6C8AD576E1902D6408F`. |
| Existing AI authorization validator | **45/45 PASS**. Legacy recipe/request identity, current operation authorizations, input budgets, create/confirm CAS and zero provider calls on rejected work remain valid. |
| Existing invocation-ledger, workflow-preset and preset-ASGI validators | **PASS**. Authorization SHA behavior, invocation ownership and preset materialization remain valid. |
| Editing Chromium validator generated from current production HTML | **PASS**, repeated. Restores a non-first provider/model, Cedar, replace mode and `1.005`; preserves value and focus through changing timeline polls; preserves subsequent manual voice/translation/dubbing choices; rejects blank and `1.13`; shows exact plan/confirmation values; and has no horizontal overflow at 1440 or 320 px, page/console errors, or undeclared network request. Ignored browser validator SHA-256: `85990119078DA07C07A1D7EB88A7187FAD2D6966DDEAD9E246D7A06196F9F110`. |
| Workflow preset Chromium validator regenerated from current production HTML | **PASS**. Restores `1.005`, preserves rate and focus through polling, changes to `1.007`, revokes old egress consent, submits `1.007`, and retains the existing preset/platform/320 px assertions with no unexpected network request. Ignored browser validator SHA-256: `491014F3ADEF8C38B31A5B72890D362F041DB50E2E8193B1670E69EE0080CF98`. |
| Existing workflow multi-segment and readiness Chromium validators | **PASS**. Current production page still preserves its segment and readiness behavior. |
| `pytest -q tests/test_editing_ai_contracts.py tests/test_editing_service.py tests/test_editing_media.py tests/test_editing_api.py` | **69 passed, 2 failed in 11.12 seconds** after the final protocol/runtime/provider guard-order fix. Both failures assert historical Editing Schema 1 while the product is Schema 4; no test file was changed. |
| Full `pytest -q` | **2437 passed, 13 failed, 8 skipped in 380.58 seconds** before the final two-line huge-integer guard-order fix; the subsequent focused suite and 10-check validator cover that final change. Twelve failures are the already documented stale assertions for version `0.27.0`, Editing Schema 1, three-link navigation and the old source-distribution exclude list. One unrelated Windows eight-process upload-schema initialization case had one child exit inside `_exclusive_schema_access`; the exact failed parameter then passed three consecutive isolated reruns (`0.76 s`, `0.81 s`, `7.00 s`). The full suite therefore remains non-green and the isolated pass is recorded as transient evidence, not removal of the failure. |
| Python AST/`compileall`, extracted inline JavaScript `node --check`, `git diff --check` | **PASS**. |

The validators use temporary roots, synthetic providers, route interception and loopback-only browser fixtures. They do not contact OpenAI, a media site, Bilibili, Douyin or WeChat Channels.

## Compatibility and remaining boundaries

- New code reads old records because a missing rate means `1.0`, and the default remains omitted from canonical JSON.
- An old binary cannot read a new record containing a non-default rate because its exact-key recipe parser rejects the added field. This is a forward-only application-data extension; a rollback after saving non-default rates must retain the newer binary or restore a compatible backup.
- The duplicated `0.88`/`1.12` policy currently appears at the recipe, HTTP, SpeechOptions, protocol and provider boundaries. Any future range change must update and validate all of them together. A future provider with a narrower range needs an explicit capability/adapter constraint.
- Faster synthesis may still exceed a cue slot. Only measured output decides whether rendering can continue.
- Editing draft recipes still identify a selected translation by language, provider and model rather than by revision ID. If more than one approved revision has the same tuple, a page reload can choose the first match; a later lightweight change should persist and validate the exact revision before rendering.
- No real OpenAI request, price check, human listening test, account-permission check, media-site download, domestic-platform submission, review, scheduled publication or public-visibility check was run.
- The earlier `0592b6f` release receipt does not cover this post-release working tree. A new release claim still requires one frozen clean commit and a new external receipt for the same artifacts.
