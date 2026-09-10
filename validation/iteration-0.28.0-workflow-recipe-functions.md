# Iteration 0.28.0 Workflow recipe functions

Date: 2026-09-10
Scope: architecture review S7b; local/offline production-template validation only

## Result

The Workflow page recipe builder now separates cover input, cover validation
and AI recipe input into ordinary functions. The remaining `recipe()` function
keeps the original orchestration order: read operator state, reject unmatched
translation/dubbing choices, read full-video or segment input, validate the
cover, build the AI fields, and return exactly `segments`, `cover`,
`translation` and `dubbing`.

The extraction keeps capability selection in the existing helpers. It does not
introduce a UI framework, schema, shared rule service or new capability source.
It does not change markup, Editorial Glass styling, backend validation, model
authorization, upload behavior or confirmation semantics. No tracked test file
was added or modified.

## Preserved ordering and behavior

- Cover controls retain their short-circuit read order. Reusing the public
  upload title still depends on `cover-use-title` and explicit-title mode;
  source-title mode retains the independent cover title.
- Dubbing without translation and translation without dubbing fail before
  segment parsing. Segment errors retain their existing field association,
  `aria-invalid` state and focus.
- Full video, AI full video and cover-only workflows keep `segments: []`.
  Explicit workflows retain the 1–10 segment bound, overlap checks and the
  stricter contiguous-boundary rule when AI is enabled.
- Cover checks remain fixed-ratio allowlist, selected-platform intersection and
  nonnegative safe-integer timestamp, in that order. The platform ratio error
  still focuses `cover-ratio`; the other two errors leave focus unchanged.
- AI input order remains translation capability, speech capability, voice and
  speech rate. A missing capability precedes voice/rate errors; a missing voice
  precedes an invalid rate; only the rate error focuses `voice-rate`.
- The successful translation and dubbing objects retain their provider, model,
  state, language, voice, replace-audio, exact speech authorization and numeric
  rate fields. The final four-field recipe object is explicit, so future helper
  fields cannot accidentally enter the API payload.

## Validation

The fixture-backed browser page was regenerated from the current
`WORKFLOW_HTML` before every final run.

| Check | Result |
| --- | --- |
| Current production-template preset browser | **PASS**: exact null baseline, complete cover/translation/dubbing payload, both unmatched AI choices, fixed ratio, negative timestamp, missing voice, invalid-rate focus, incompatible-ratio focus and missing exact capability |
| Current production-template multi-segment browser | **PASS**: AI full video, three segments, cover-only full video, overlap and AI-gap error/focus, preset restoration, polling and cancellation |
| Current production-template source-title, readiness and no-AI full-video browsers | **3/3 PASS** |
| No-AI whole-video validator | **5/5 PASS** |
| Speech-rate validator | **10/10 PASS** |
| AI authorization, multi-segment service and Workflow preflight validators | **3/3 PASS** |
| Synthetic three-platform full-video and full-chain validators | **2/2 PASS**; Bilibili, Douyin and WeChat Channels, with guarded Python networking and no real platform request |
| Extracted inline JavaScript `node --check`, `compileall`, dependency compatibility and `git diff --check` | **PASS** |
| Independent behavior-equivalence diff review | No P1/P2; DOM reads, error priority, focus, full-video, cover and AI capability semantics remain equivalent |

These results bind this source checkout and are not a frozen release receipt.
No real URL, OpenAI response, human listening check, platform login, upload,
review, scheduled publication or public visibility was tested.

## Next boundary

Architecture review S7 is complete at the ordinary-function boundary.
Capability and frontend/backend validation remain separate where they serve
different execution boundaries. Further sharing should occur only when it
removes a proven duplicate without changing immediate feedback.

The next review slice is S8: reduce repeated current-state prose in the handoff
and validation index, and confirm that hosted CI and the local hook use the same
commit-scope classifier. Historical evidence remains immutable and keeps its
point-in-time results.
