# Iteration 0.28.0 Workflow upload form functions

Date: 2026-09-10
Scope: architecture review S7a; local/offline production-template validation only

## Result

The Workflow page upload builder now has four explicit responsibilities. The
form reader checks selected ready accounts and reads common operator input. The
target merger deep-clones any saved per-account override, applies only fields
the operator actually changed, and resolves Bilibili, Douyin and WeChat
Channels options. The target validator applies the existing capability-driven
limits and immediate field errors. The remaining `upload()` function only
orchestrates those steps and returns the existing payload.

This is an extraction of existing behavior. It does not change the DOM, visual
design, routes, schemas, backend validation, capability payload, account limit,
platform set or confirmation behavior. No UI framework or shared rule service
was introduced, and no tracked test file was added or modified.

## Preserved contracts

- Account selection order, the three-account ceiling, ready/session checks and
  unavailable saved-account guard run before any target is built.
- Source-title mode may keep the common title empty while preserving an
  independent per-platform title. Explicit-title mode still focuses the common
  title immediately when it is empty.
- A saved target is deep-cloned. `changedUploadFields` continues to control
  selective replacement, so polling or preset hydration cannot overwrite
  untouched per-account title, description, tags, schedules or platform
  options.
- `scheduleFor()` still owns local time, DST and timezone parsing. Its errors
  occur during the same account merge step and focus the same control.
- Validation order and controls remain title, tags, mode, Bilibili metadata,
  draft/schedule conflict, capability lead time, the WeChat Channels 28-day
  ceiling, Bilibili dynamic text and WeChat Channels short title.
- Douyin declaration and WeChat Channels content-label defaults continue to
  distinguish an explicit empty value from an untouched AI-derived default.
- The resulting `target_overrides` and top-level upload fields retain account
  order and the existing source-title omission rules.

## Validation

The three fixture-based browser validators were regenerated from the current
`WORKFLOW_HTML` immediately before execution; they did not run against their
older saved page.

| Check | Result |
| --- | --- |
| Current production-template preset browser | **PASS**: polling/focus, async save, preset hydration, three-platform target overrides, complete POST payload and immediate invalid-field state |
| Current production-template multi-segment browser | **PASS**: account ceiling, segment/preset state, full/multi/cover payloads, polling, cancellation and stale-response handling |
| Current production-template readiness browser | **PASS**: live readiness, out-of-order/timeout recovery, source URL value, focus and exact `selectionStart`/`selectionEnd` through polling |
| Current production-template source-title browser | **PASS**: remembered preset, dependency/dirty guards, account restoration and source-title POST payload |
| Editorial Glass four-page production browser | **44/44 PASS**: light/dark, reduced motion, 200% text and responsive widths; zero external requests, console/page errors and horizontal overflow |
| Preset API, preset boundaries and preset validation | **3/3 PASS** |
| Source-title validator | **7/7 PASS** |
| Multi-segment service and preflight validators | **PASS** |
| Extracted inline JavaScript `node --check` and `git diff --check` | **PASS** |
| Independent behavior-equivalence diff review | No P1/P2; error order, focus targets, source-title, preset merge, schedules, AI labels and final payload remain equivalent |

All browser routes were synthetic loopback responses. No real URL, OpenAI,
platform login, upload, review, scheduled publication or public visibility was
tested. These results bind this source checkout and are not a frozen release
receipt.

## Next boundary

S7b should split `recipe()` into ordinary cover construction, cover validation
and AI recipe construction functions. Preserve the current error/focus order,
full-video and segment semantics, capability-selected models, speech-rate
validation and Editorial Glass markup. Capability consolidation should remain
a later slice and only proceed where it deletes a proven duplicate rule.
