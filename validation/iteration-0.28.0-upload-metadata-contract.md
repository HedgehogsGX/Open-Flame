# Iteration 0.28.0 upload metadata contract validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

The upload domain now exposes one public, state-free metadata contract in
`uploads/metadata.py`. Upload execution, workflow presets, and upload backup
validation reuse its title limits, tag normalization, platform options, target
override fields, and publication schedule rules. `workflows/presets.py` no
longer imports private constants, text helpers, or static methods from
`UploadService`.

Database, account/session, cover-file, runtime, and current scheduling checks
remain in `UploadService`. The change adds no schema, service, thread, queue,
runtime, or dependency.

## Compatibility held

- Existing `UploadError` codes and list/dict/bool type boundaries are unchanged.
- Tag order, duplicate rejection, character rules, and the ten-tag limit are
  unchanged.
- Bilibili, Douyin, and WeChat Channels option defaults and enum checks are
  unchanged.
- Publication time precision, lead time, timezone, and WeChat Channels
  whole-hour/28-day rules are unchanged.
- Preset normalization still maps upload-domain failures to
  `workflow_preset_invalid`, retains only explicitly supplied platform option
  keys, and performs no account, media, or network access.
- Constants imported historically from `uploads.service` remain available as
  imported module names while current callers use the public metadata module.

## Verification

| Check | Result |
| --- | --- |
| Upload service, platform parameters, backup/restore, and Schema 3 regression | **240 passed in 55.70s** |
| Workflow preset validator | **PASS** |
| Workflow preset boundary validator | **PASS** |
| Workflow preset API validator | **PASS** |
| `compileall` for upload and workflow packages | **PASS** |
| `git diff --check` | **PASS** |

No tracked test file was added or modified. The architecture review and local
probe outputs remain untracked or under ignored `validation/local/` and are not
part of this evidence commit.

## Evidence boundary

This is a source-level refactor validated with local state and synthetic inputs.
It did not log in to a platform, upload media, schedule a real publication, call
OpenAI, or create a new release receipt. It therefore does not upgrade any
platform capability or current source build to verified status.
