# Iteration 0.28.0 offline workflow integration smoke

Revalidated: 2026-09-09. Source baseline: `b240392f838254aaec425e6ace96ab03cc5af8af`; current working tree validation, not a frozen release receipt.

## Correction to the earlier result

The earlier validator inherited an adapter whose `prepare_edit` returned an already prepared plan. It never entered `advance_ai`, used only one synthetic Bilibili job, and never reopened the workflow service. Its green result established only the minimal synthetic state-machine path. The earlier claims about translation, dubbing and restart safety were unsupported and are superseded by the scoped evidence below.

## Current execution and result

Command: `uv run --frozen python -B validation/local/validate_workflow_full_chain.py` — **PASS**, 6.94 seconds on this Windows machine, including preset roundtrip and output-audio checks. The ignored validator SHA-256 is `5d6034facfa60b9f349e08ea5f9b63d820d0cc37eca5fbf2eae6a73ec44e3723`; its compact output is retained in ignored `validation/local/workflow-full-chain-result.json`. Neither temporary script nor output enters Git or release artifacts.

The replacement invokes the production `WorkflowPresetStore`, `LocalWorkflowAdapter`, `BatchService`, download `Worker`/`AssetStore`/`FfprobeVerifier`, `EditingManager`, `AiTaskExecutor`, current isolated AI worker/protocol, real FFmpeg rendering, and `UploadService`. Only internet extraction/transfer, inference-model responses, and login/upload network backends are synthetic. A temporary virtual environment contains a fully hashed local plugin and model declaration; it contains no real inference model or credentials.

The input is a syntactically supported Bilibili URL used solely as fixture identity. The download adapter supplies a locally generated two-second MP4 through the normal download worker, which validates and commits it to the download domain. `auto_confirm_edit` and `auto_confirm_upload` are enabled at workflow creation; no manual `confirm_ai`, `confirm_edit`, or `confirm_upload` calls advance the smoke.

| Assertion | Current evidence |
| --- | --- |
| Saved preset enters actual chain | A full profile with existing account bindings is saved, then loaded through a new `WorkflowPresetStore`. Materialization restores the saved segment and upload title over different current placeholders, using freshly resolved full AI authorizations. This materialized profile is the one that runs through the complete three-platform chain. |
| Preset excludes execution state | A recursive JSON inspection finds no full authorization, URL, session revision, account binding, stored egress consent, Cookie or API-key field. The saved record retains all three account choices and the synthesis authorization digest. Saving/materializing causes no AI or upload call and does not rewrite the stored file. |
| Current account binding | All three fixture accounts log in again after saving; their session revisions change. Workflow creation binds the new revisions, with none of the saved profile's old sessions restored. |
| Actual AI operation entry | Isolated plugin audit records exactly one `transcribe`, one `translate`, one `synthesize`, plus one voice `health` call. The plugin checks the transcription input hash, translation source cue and language, and the actual Chinese text/voice received by synthesis. |
| Translation review and rendering | The persisted timeline is approved and contains the fixture translation `你好世界`. The real render produces segment, PNG cover, VTT caption and dubbed MP4 assets. |
| Correct upload derivative | The workflow selects the `dubbed_video` asset. FFprobe reports H.264 video, AAC audio and 1520 ms duration for the requested 1500 ms segment. Decoded output audio has over 100 times more energy at the synthesized fixture's 660 Hz tone than the original source's 440 Hz tone, proving replacement audio reached the output. Each upload request's managed video SHA-256 equals the edited output SHA-256. |
| Three-platform propagation | Exactly three jobs are created for `bilibili`, `douyin`, and `tencent`. Every synthetic upload backend call receives the requested title/tags and an existing landscape cover. All jobs reach `submitted` using synthetic acknowledgements. |
| Automatic completion | Observed states: `downloading → awaiting_ai_review → rendering → uploading → completed`; final code `submission_acknowledged`. Intermediate synchronous transitions may happen within one `advance` call. |
| Durable repository reopen | A new `WorkflowService` reads the same workflow and batch ID while download is pending, then continues the chain. This is a repository reopen, not a process crash or power-loss test. |
| Completed replay | After another repository reopen, creation with the same idempotency key returns the same workflow. Three further advances leave AI call counts and the three upload jobs unchanged. |
| Source preservation | The original generated source SHA-256 remains unchanged. |
| Offline boundary | Controller and plugin install Python audit hooks rejecting outbound socket/DNS operations; an explicit DNS attempt proves each guard is active. No unexpected controller network attempt occurs. FFmpeg subprocesses receive only generated/local files. This is not an OS firewall or packet-capture test. |

## Unverified requirements

This smoke does **not** execute the OpenAI provider, test inference quality or intelligible speech, download internet media, authenticate a real account, run the three real platform uploader adapters, establish platform acceptance/public visibility, or test scheduled publication. It does not validate remote-AI invocation accounting because the fixture provider is local. It does not cover browser preset application, manager/API process lifecycle, crash/power-loss recovery, multi-cue/long-video quality, or a frozen distribution. Those requirements remain separate work; a synthetic `submitted` result is never a real publication claim.
