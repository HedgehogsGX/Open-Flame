# Iteration 0.28.0 workflow profile contract validation

Date: 2026-09-10 (Australia/Adelaide)

## Result

Workflow profile normalization, canonical JSON/SHA encoding, source-title
upload snapshot validation, and ordered edit-to-upload output validation now
live in the public, state-free `workflows/profile.py` module. The state machine,
preset store, API, manager, and local adapter use `WorkflowError` from the
public Workflow contracts rather than importing it through service internals.

`workflows/service.py` now owns orchestration, persistence, and state changes;
it no longer defines the profile codec. `workflows/presets.py` no longer imports
any private Workflow service helper. No compatibility wrapper or second profile
implementation remains.

## Compatibility held

- A script extracted the six original pure functions from Git `HEAD`, applied
  only their public-name substitutions, and compared that block with the new
  module: **byte-equivalent PASS**.
- Canonical JSON still uses UTF-8, `ensure_ascii=False`, sorted object keys,
  compact separators, `allow_nan=False`, the 128 KiB limit, and SHA-256.
- Explicit-title profiles still omit the default `title_mode`; source mode is
  stored explicitly.
- Account IDs, overrides, bindings, outputs, and per-output targets preserve
  their original list order and uniqueness rules.
- Legacy AI key shapes, conservative data-egress handling, exact authorization
  binding, multi-segment limits, and stored preset omissions are unchanged.
- Existing imports of `WorkflowError` from `workflows.service` still resolve to
  the same class imported by the service; current production modules use the
  public contract directly.

## Verification

| Check | Result |
| --- | --- |
| Source-title profiles, Schema 1/2→3, freeze/restart/preset | **7/7 PASS** |
| No-AI whole-video and profile contract | **5/5 PASS** |
| Preset, preset boundary, and preset API validators | **PASS** |
| Speech-rate identity, preset, API, provider, and overflow | **10/10 PASS** |
| Multi-segment service and Schema 1→2 migration | **PASS** |
| Pre-authorized restart continuation | **PASS** |
| Upload attention recovery | **7/7 PASS** |
| Whole-workflow cancellation | **PASS** |
| v0.28 workflow validator | **PASS** |
| Synthetic URL→AI→FFmpeg→three-platform full chain | **PASS** |
| Tracked subprocess lifecycle regression | **12 passed** |
| Full source `compileall` and `git diff --check` | **PASS** |

The architecture review's malformed `title_mode: []/{}` finding belongs to its
older source snapshot. Current code checks the value type before set membership.
Both values return `WorkflowError("invalid_workflow_profile")` directly and HTTP
422 through the production API; preflight, download, edit, and upload calls all
remain zero. No extra product change was required.

No tracked test file was added or modified. Current ignored validators were
updated locally to import the new public codec; those local files are not part
of the commit.

## Evidence boundary

The full-chain checks used synthetic media, local FFmpeg, deterministic AI
workers, fake upload acknowledgements, and a network guard. No real URL, OpenAI
request, platform login, upload, moderation, scheduled publication, or public
visibility was exercised. This change does not create a release receipt or
upgrade platform capability evidence.
