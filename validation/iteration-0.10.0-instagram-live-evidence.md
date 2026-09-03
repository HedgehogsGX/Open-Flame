# Iteration 0.10.0 Instagram single-sample local Worker evidence

Date: 2026-09-03
Environment: Windows / local direct, non-isolated Worker / fresh data root
Project version: 0.10.0
Database schema: 8
Toolchain: yt-dlp `2026.08.19`; FFmpeg/ffprobe `n9.0.1`; explicit Node runtime
Credential mode: no Cookie

## Scope and redaction

This record covers one public Reel published by NASA's official Instagram
presence. It records only the minimum facts needed to audit the product path.
The exact URL and Reel identifier, platform account-internal identifiers,
runtime identifiers, local absolute paths, media hashes or fingerprints,
signed URLs, Cookies and raw log content are intentionally omitted.

This is one positive sample in one Windows environment. It is not Stage 0,
does not establish platform-wide compatibility, and does not upgrade any
capability from `candidate` to `verified`.

## Fixes exercised by the live path

1. Worker probe and download requests now carry the configured `max_height`
   into the adapter.
2. The yt-dlp format selector no longer falls back to an unrestricted `/b`
   branch; every branch retains the configured height ceiling.
3. Extractor responses that explicitly require fresh cookies map to
   `authentication_required` rather than `extractor_broken`.
4. A pinned `requested format is not available` response maps to
   `content_unavailable`, so a per-item height-policy mismatch cannot be counted
   as extractor breakage or open the platform circuit.

## Result

| Check | Redacted result |
|---|---|
| Input and terminal state | One direct Instagram Reel was accepted; the Batch and Job reached `1/1 ready` |
| Worker lifecycle | The local direct Worker completed `probing → downloading → verifying → committing → ready` |
| Database | `PRAGMA quick_check` passed; foreign-key checks reported no violations; no pending commit intent remained |
| Asset integrity | The original file, database record, manifest and API download copy agreed on size and SHA-256; values are withheld |
| Media verification | One complete decode of the published original finished cleanly |
| Cleanup | Attempt temporary storage and asset staging had no residual payload after commit; the complete live-test data roots were removed after evidence capture |
| Runtime logging | Field-allowlisted control and `local-worker` JSONL lifecycle records were present and readable; raw entries are not reproduced |
| Browser UI | The page showed version 0.10.0, `ready`, `1/1 ready` and an asset download link; the download worked and the console had no warning/error |

The run used the repository-pinned yt-dlp and FFmpeg/ffprobe tools, an explicit
Node runtime, no Cookie, and a newly created data root. It proves only that
this one public sample completed the documented local direct path at the
recorded time.

## Other platform observations in the same iteration

- A live attempt against a public Bilibili movie sample received HTTP 412 and
  did not produce a ready asset. This single response is not a platform-wide
  incompatibility finding.
- A live attempt against an official Douyin promotional sample was rejected by
  the extractor as requiring fresh cookies. No Cookie was provided; the Job
  correctly terminated as `authentication_required`.
- TikTok was not run.

None of these observations satisfies Stage 0. Bilibili, Douyin, TikTok and
Instagram, together with the existing YouTube and X routes, remain
`candidate`; X graph-v2 remains separately disabled.

## Remaining acceptance work

Each `platform × source_type × adapter_version × environment` evidence identity
still requires at least 10 authorized positive samples, separate negative
samples and three consecutive qualifying runs under the Stage 0 rules. The
Windows direct path also does not prove Linux/Docker network isolation, Cookie
deployment, production-scale durability, or third-party binary/container/tool
bundle redistribution readiness.
