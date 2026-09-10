# Iteration 0.28.0 document status consolidation

Date: 2026-09-10
Scope: architecture review S8; documentation and existing CI-policy validation only

## Result

`HANDOFF.md` now has one current development authority: source identity,
connected capabilities, evidence links, unresolved risks, a single next path
and repository constraints. Repeated per-iteration test counts and obsolete
"next" instructions were removed. The previous handoff remains available in
Git history, while its durable engineering results remain in the existing
`validation/iteration-*.md` records.

`validation/README.md` is now a latest-first evidence index plus the Stage 0
procedure. It no longer combines test counts from different commits into a
single current result. Historical records were not edited or deleted.

The execution plan now marks architecture review S1–S8 complete and points to
the same next path as the handoff: receive external test evidence, fix locally
reproducible issues, and then freeze an exact clean candidate. Real OpenAI,
human listening and platform actions remain separately authorized operations.

No source, CI definition, hook or tracked automated test file changed in this
slice.

## Removed stale current-state claims

- Old S5/S6/S7 paragraphs no longer compete with the completed S8 next path.
- Historical `tests/test_api.py` counts are no longer described as current.
  Current HTTP guards make those frozen TestClient expectations inapplicable;
  exact failures remain scoped to their recorded run.
- Old instructions to bind future gates to a final 0.25.0 build were removed
  from the current handoff. Future acceptance must bind its exact candidate.
- The first-public-commit instruction was replaced by the current signed Git
  identity and no-force-push rule.
- The hosted CI result remains accurately limited to execution-chain recovery:
  its four full pytest cells are red and required checks/branch protection are
  not inferred.

## Existing CI and hook contract

No new checker was needed. Commit `bdd88ce` already made both entry points use
`scripts/verify_commit_scope.py`:

- `.githooks/pre-commit` calls `--staged`.
- Hosted CI uses a full checkout and calls `--github-event`.
- Push, pull-request and dispatch events resolve base/head commits; range mode
  compares the merge-base-to-head net diff.
- NUL-delimited name-status parsing checks both paths for copies/renames.
- The diff filter excludes deletion-only changes, so dedicated historical-test
  cleanup remains allowed while additions/modifications are rejected.

## Validation

| Check | Result |
| --- | --- |
| Source identity | **PASS**: version `0.28.0`; Download/Editing/Upload/Workflow Schemas `11/4/3/3`; upload backup format `2` |
| Handoff consolidation | **PASS**: Git baseline 187,303 → working tree 8,345 bytes; 700 → 107 lines |
| Validation index consolidation | **PASS**: Git baseline 20,368 → working tree 9,590 bytes; cross-commit current-count aggregation removed |
| Evidence preservation | **PASS**: no existing `validation/iteration-*.md` file deleted or modified |
| Relative links in the handoff, plan, validation index and this record | **PASS** |
| `verify_commit_scope.py --range HEAD^ HEAD` and `--staged` | **PASS** |
| `verify_ci_contract.py` | **PASS** |
| `git diff --check` | **PASS** |

The documentation still states the material limits: the `0592b6f` receipt is
point-in-time, current source has no new clean receipt, hosted pytest remains
red, and real OpenAI/listening/platform publication plus target Linux/Docker
acceptance remain unverified.

## Next boundary

Architecture review S1–S8 is complete. Further architecture work should start
from a newly observed duplication or failure rather than adding another generic
layer. The active path is external testing and reproducible issue repair,
followed by an exact clean candidate and a new package receipt.
