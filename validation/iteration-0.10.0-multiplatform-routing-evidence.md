# Iteration 0.10.0 multi-platform routing evidence

Date: 2026-09-03
Environment: Windows / Python 3.12.13 / uv 0.11.25
Project version: 0.10.0
Database schema: 8

## Scope

This record covers the first engineering slice for Bilibili, Douyin, TikTok
and Instagram Reels. It verifies URL boundaries, declared adapter routes,
queue claiming, offline Worker/asset flow, the public capability matrix,
deployment Cookie mapping expansion, package metadata and the local UI.

This routing-verification run is not real-platform or Stage 0 evidence and did
not itself make live media requests. Later in the same iteration, a separate
redacted Instagram single-sample run completed; it is recorded in
[Iteration 0.10.0 Instagram live evidence](iteration-0.10.0-instagram-live-evidence.md).

## Implemented contract

- The shared domain now models TikTok video and Instagram Reel sources.
- The static capability registry declares seven narrow yt-dlp routes across
  six platforms. All remain `candidate`; static declarations reject manual
  `verified` promotion.
- Worker claiming filters atomically by exact
  `platform × source_type × job_kind`. Older unsupported work remains queued
  for a compatible Worker.
- Worker requests now pass the configured `max_height` through to the adapter. The
  yt-dlp format selector no longer contains an unrestricted `/b` fallback, so
  every fallback path retains the configured height ceiling.
- A pinned yt-dlp `requested format is not available` response now maps to the
  content-specific `content_unavailable` terminal state instead of
  `extractor_broken`, so height-policy mismatch cannot trip a platform circuit.
- Extractor responses that explicitly require fresh cookies are classified as
  `authentication_required`, not as a generic network failure.
- Bilibili accepts public BV/av submissions only at the default part. `p > 1`,
  invalid or repeated `p` selectors fail instead of silently selecting the
  wrong part.
- TikTok accepts only direct `/@handle/video/{numeric-id}` URLs. `vm` and `vt`
  hosts are recognized as TikTok but explicitly reported as deferred short
  links.
- Instagram accepts only public `/reel/{shortcode}` or `/reels/{shortcode}`
  URLs and canonicalizes them to `/reel/`.
- The capability API/UI distinguishes direct short-link support, separately
  gated resolution and deferred resolution. Evidence version/environment stay
  unbound because the later single-sample result does not satisfy Stage 0.
- Candidate Cookie mapping and Linux acceptance assets now model zero to six
  optional sources and a six-source synthetic maximum case.

## Verification performed

| Check | Result |
|---|---|
| Point-in-time routing pytest suite | `914 passed, 8 skipped in 53.84s`; this predates the later live-run fixes and is not the final 0.10.0 regression total |
| Final 0.10.0 pytest suite | `920 passed, 8 skipped in 52.23s` |
| Python bytecode compilation | `python -m compileall -q src tests` passed |
| Dependency lock | `uv lock --check --offline` passed |
| Pinned Windows tool verification | yt-dlp `2026.08.19`; FFmpeg/ffprobe `n9.0.1-6-g9d4ca21220-20260820`; offline smoke passed |
| Pinned extractor inventory | `BiliBili`, `Douyin`, `TikTok` and `Instagram` entries were present with plugins and remote components disabled |
| Browser/UI synthetic flow | Four-platform form submission queued four exact source types; offline fake Worker reached `4/4 ready`; four asset links rendered; browser console had no warning/error |
| Project package | 0.10.0 sdist/wheel built successfully offline; clean isolated wheel install reported `Apache-2.0` and all 11 project console scripts; temporary validation artifacts were removed |
| Package privacy boundary | sdist/wheel contained no `data`, `logs`, `runtime-tools`, `validation/local`, SQLite, Cookie or media payload paths |
| Repository privacy scan | No high-confidence private-key/token marker, private local path marker, prior user sample ID or account marker found in publishable files |
| Patch hygiene | `git diff --check` passed |

The eight skipped tests are explicit POSIX-only boundaries: one Cookie
directory-FD cleanup test, four root/getfacl metadata cases for 0/1/3/6
sources, one AF_UNIX roundtrip and two POSIX path/permission cases. They must be
rerun on the target Linux host and are not counted as passed.

## Open-source and license decision

No downloader dependency was added. The existing pinned yt-dlp Python/zipimport
artifact remains the primary engine. Its exact local artifact boundary is
`Unlicense AND MIT AND ISC`; the project does not substitute the upstream
GPL-3.0-or-later PyInstaller executable. Alternative projects remain research
or future isolated fallbacks and require exact-version dependency and
redistribution review before adoption.

Apache-2.0 covers this project's own source, documentation and scripts. It does
not grant rights in downloaded media, platform accounts or third-party tools.
Dependency wheelhouses, frozen executables, runtime tool bundles and container
images remain behind their existing third-party redistribution gate.

## Remaining acceptance work

1. Continue the planned acceptance order: investigate the Bilibili HTTP 412
   boundary; rerun Douyin only with user-authorized fresh cookies; perform the
   first TikTok live run; then expand Instagram beyond its one positive sample.
   Every evidence identity still needs the full Stage 0 sample and consecutive
   run thresholds.
2. Extend the Stage 0 evidence model, evaluator and `platform_capabilities`
   database identity to include `job_kind`, then bind validated
   `adapter_version × environment` evidence before any public route becomes
   `verified`.
3. Add platform-specific, redacted yt-dlp probe/output fixtures after the first
   authorized runs; current tests prove command and adapter plumbing, not live
   extractor response stability.
4. Run the six-source metadata/ACL contract and full candidate topology on a
   clean target Linux host.
