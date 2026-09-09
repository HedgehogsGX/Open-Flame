# Iteration 0.28.0 full-video workflow default evidence

Date: 2026-09-09

The production `/workflows` page now starts in full-video mode. Previously its initial form silently enabled a `0–60000 ms` segment, so the shortest URL-first path would process and publish only the first minute unless the operator noticed and changed the range. The segment toggle is now off, the visible status is `完整视频 · 0 段`, and the request profile contains `edit_recipe.segments: []`. Enabling segmentation still reveals one editable `0–60 s` row; saved presets with one to ten segments continue to restore their exact ranges.

This is a UI-default correction, not a new media subsystem. Existing Workflow Schema 2 and the existing AI-aware editor already interpret an empty segment list as one full-source output. The adapter also no longer imports the presentation-layer `EditingManager` at runtime solely for a type annotation; it uses `TYPE_CHECKING`, so importing the workflow adapter does not pull FastAPI or Pydantic into that dependency path. No service, thread, queue, database, schema, or dependency was added.

Current local checks, all PASS:

- `validation/local/validate_workflow_multisegment_browser.cjs`: installed Chrome verified the initial unchecked toggle, hidden segment actions, `完整视频 · 0 段`, and an AI workflow POST with `segments: []`; enabling segmentation still produced three ordered ranges, restored a segmented preset, enforced the ten-segment cap, preserved focus, and stayed within 320 px.
- `validation/local/validate_workflow_full_video.py`: the real local Download Worker, `LocalWorkflowAdapter`, Editing service, isolated synthetic AI runtime, FFmpeg, Upload service, and three synthetic platform backends completed one 2-second source with no segment recipe. It made one transcription, one translation and two speech calls, produced one H.264/AAC dubbed video with a measured 2000 ms duration, and delivered that same output to Bilibili, Douyin and WeChat Channels substitutes. Python network access was blocked.
- `validation/local/validate_workflow_adapter_light_import.py`: a fresh process imported the local workflow adapter without loading `video_download_control.editing.api`, FastAPI, or Pydantic, and still accepted a real `EditingManager` instance at runtime.
- The current editing/AI contract/upload regression selection passed `138 passed in 49.22s`; all three existing Workflow Chrome validators passed against the regenerated production page. Python compileall, `uv lock --check --offline`, `uv pip check`, changed-Markdown links, the 258-file release inventory, and `git diff --check` also passed.

A supplemental run of `tests/test_editing_media.py tests/test_editing_api.py` produced `47 passed, 2 failed`. Both failures are pre-existing assertions that require Editing Schema `1` while the current source is Schema `4`; this slice did not change schema behavior. Existing tracked tests were not modified.

These checks use local synthetic providers and platform substitutes. They do not prove internet extraction, a real OpenAI response, voice quality, account login, platform acceptance, review, scheduling, or public release.
