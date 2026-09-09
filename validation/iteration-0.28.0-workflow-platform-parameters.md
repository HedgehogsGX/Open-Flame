# Iteration 0.28.0 workflow platform parameters evidence

Date: 2026-09-09

## Milestone scope

This post-release source milestone brings the existing Bilibili, Douyin and
WeChat Channels upload parameter contract into `/workflows`. It is evidence for
the current working tree only. It is not a new frozen release and has no new
release receipt.

The workflow form now keeps common title, description and tags, then shows one
platform card for every platform represented by the selected accounts. Each
card can override common content and exposes only the platform's own fields:

- Bilibili: title, description, tags, `category_id`, `copyright`,
  `source_credit`, schedule, `dynamic`, `no_reprint`, `close_comments` and
  `close_danmu`.
- Douyin: title, description, tags, schedule and `declaration`.
- WeChat Channels: main text, description, tags, per-target `publish` or
  `draft` mode, schedule, `short_title` and `content_label`.

The base upload mode remains `publish`. Bilibili and Douyin targets are always
publication targets; only a WeChat Channels target may override the base mode
to `draft`. A draft target cannot also carry a schedule.

`/api/v1/uploads/status` supplies per-platform capability metadata, including
title limits, description/tag limits, supported modes, cover slots, schedule
lead time, platform fields and allowed declaration/content-label values. The
workflow page consumes the current title limits, schedule lead times and
allowed declaration/content-label values, with conservative built-in values
when capability data is unavailable. Server-side upload validation remains the
authoritative check for the complete metadata contract.

## Preset preservation

A preset may contain different target overrides for two accounts on the same
platform even though the live form intentionally presents one shared platform
card. Applying such a preset shows the first account's effective values and a
visible divergence notice. If the operator does not edit the corresponding
shared field, workflow creation preserves each account's saved title,
description, tags, schedule, mode and platform options. Detaching the preset
also keeps those values. Editing a field in the shared platform card explicitly
replaces that field for the selected accounts instead of silently flattening
unrelated saved differences. A separate explicit action applies the whole
visible card to every selected account on that platform. Changing WeChat
Channels to `draft` clears and disables its schedule; changing Bilibili between
original and repost updates the source field as one consistency group.

When AI translation is enabled, the shared card visibly suggests the platform's
AI content label only when none of its selected accounts carries an explicit
saved option. During profile materialization, each target that lacks that option
receives the suggestion independently while every explicit preset value,
including `null`, is preserved. A mixed preset therefore cannot copy one
account's explicit declaration into a neighboring account, and divergent saved
values are not flattened. A label the operator has explicitly changed in the
current form, including an empty choice, is also not overwritten.

Platform validation reports its message in a field-associated alert, marks the
first invalid control with `aria-invalid`, adds the alert to
`aria-describedby`, and moves focus to that control. Independent platform titles
become required while their card is active; the currently effective Bilibili
tag input is also required. A custom error remains associated while the edited
value still violates the rule, including Bilibili's tighter common-title limit,
and is removed after correction or when that field is no longer active.

The browser roundtrip covers independent metadata for all three platforms,
including Bilibili options and schedule, Douyin declaration and schedule, and
WeChat Channels draft mode, short title and content label. It also verifies
that the serialized base mode is `publish` while the WeChat Channels target
override remains `draft`.

## Cover admission checks

Workflow-generated covers must use a fixed ratio known before downloading:
`16:9`, `4:3`, `3:4`, `9:16` or `1:1`. The source video's unknown original
ratio is not accepted by the workflow form. The form intersects the selected
platforms' supported slots and disables incompatible choices. If a newly
selected platform or loaded preset conflicts with the current ratio, the form
preserves that value, marks it invalid, keeps the error visible and blocks
submission until the operator explicitly selects a compatible ratio. During
admission, `LocalWorkflowAdapter` maps the generated ratio to a valid cover slot
for every selected platform. An incompatible ratio fails with
`workflow_cover_incompatible` before a workflow, event or download batch is
created.

A workflow-generated cover and a preset target's existing
`cover_landscape_asset_id` or `cover_portrait_asset_id` cannot be combined in
one request. This fails with `upload_cover_override_conflict` before downstream
state is written. Existing cover asset references are also resolved and
verified during upload preflight rather than deferred until draft creation.

Bilibili's landscape slot now rejects a portrait asset (`width < height`) with
`bilibili_cover_orientation_invalid`. The existing Douyin slot-orientation and
WeChat Channels 4:3/3:4 ratio checks continue to run through the same upload
preflight boundary.

## Current validation

All checks below passed against this source milestone:

The upload regression collection was run under Python 3.12.13 with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_upload_platform_parameters.py tests/test_upload_service.py tests/test_upload_api.py tests/test_upload_ui.py tests/test_upload_backend.py
```

It completed as `275 passed in 50.22s`.

| Check | Result and covered boundary |
| --- | --- |
| Existing upload regression collection | `275 passed`; service, API, platform parameters, cover assets, runtime, lifecycle, resilience, schema and backup behavior remained green. |
| Workflow Python validators | `validate_workflow_v028.py`, preset store/boundary/API validators, multi-segment service/adapter validators and the local full-chain validator passed. These cover current Workflow Schema behavior, preset materialization and three-platform target identity without remote platform traffic. |
| Preset browser validator | `validate_workflow_preset_browser.cjs`: PASS in installed Chrome with synthetic APIs; covers the three platform cards, base-publish/WeChat-draft serialization, per-platform roundtrip, explicit empty/divergent/mixed/missing AI declaration materialization, same-platform divergence/detach/apply-all behavior, required metadata and associated focused validation errors, incompatible preset cover-ratio retention and blocking, localized cover-preflight errors, draft schedule clearing and the 320 px overflow boundary. |
| Multi-segment browser validator | `validate_workflow_multisegment_browser.cjs`: PASS; the current workflow form still preserves multi-segment payload, limit, preset, focus and narrow-screen behavior. |
| Readiness browser validator | `validate_workflow_readiness_browser.cjs`: PASS; platform form changes did not regress readiness probes, retained input or narrow-screen behavior. |
| Server preflight validator | `validate_workflow_preflight.py`: `workflow-preflight-validation: passed`; covers cover-asset resolution, Bilibili portrait rejection, generated/existing-cover conflict, ratio incompatibility, runtime/account/AI admission and zero workflow/event/download/upload writes on the tested admission failures. |

The browser validators use production HTML/CSS/JavaScript with synthetic local
API responses. The Python validators use isolated local data and network audit
guards. These results establish local contract and failure-boundary behavior;
they do not establish remote acceptance.

## Unverified external boundary

Real internet download, real OpenAI calls, real Bilibili/Douyin/WeChat Channels
login, upload, draft save, scheduled publication, moderation and public
visibility are **NOT RUN**. No claim in this record upgrades any platform to
real-world verified status.

No file under `tests/` was added, modified, staged or committed for this
milestone. The `275 passed` result comes from the existing tracked regression
suite. Temporary validators, fixtures, screenshots and logs remain ignored
under `validation/local/` and are not submitted as product source.
