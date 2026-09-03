# Open-source downloader review

Review date: 2026-09-03

This is a time-stamped discovery and architecture memo, not adoption-time legal
evidence for every fallback. Repository links below are intentionally useful for
continued evaluation but can move after the review date. Any fallback selected
later must be re-reviewed at an exact tag/commit with its complete dependency and
redistribution closure before code is imported, bundled or shipped.

## Decision

Open-Flame will keep its existing pinned `yt-dlp` subprocess adapter as the
primary engine for Bilibili, Douyin, TikTok and Instagram Reels. No additional
downloader dependency is added in this iteration.

An upstream extractor being present is only enough for `candidate` status.
Each exact `platform × source_type × adapter × version × environment` route
must still pass the repository's Stage 0 evidence gate before it can be called
`verified`. The static route registry deliberately rejects manual `verified`
promotion. Before a later iteration can bind validated Stage 0 evidence, it
must extend the evidence model, evaluator and `platform_capabilities` database
identity to include `job_kind`; the current table is not sufficient by itself.

## Candidate matrix

| Platform | Primary | Possible fallback | License notes | Adoption decision |
|---|---|---|---|---|
| Bilibili | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [BBDown-rust](https://github.com/Joey-Project/BBDown-rust), [lux](https://github.com/iawia002/lux) | Open-Flame's exact official zipimport artifact `2026.08.19` is reviewed as `Unlicense AND MIT AND ISC`; upstream PyInstaller executables are GPL-3.0-or-later. Fallback licenses must be rechecked at adoption. | Keep yt-dlp. Observe the alternatives only. |
| Douyin | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [F2](https://github.com/Johnserf-Seed/f2) | F2 is Apache-2.0 but brings a substantially larger browser, signing and cookie stack. | Keep yt-dlp; evaluate F2 later as an isolated, opt-in adapter only. |
| TikTok | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [TikTok-Api](https://github.com/davidteather/TikTok-Api) | TikTok-Api is MIT but requires a browser/session-oriented runtime. | Implement the route and error model first; keep browser automation out of the control process. |
| Instagram Reels | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | [Instaloader](https://github.com/instaloader/instaloader) | Instaloader is MIT. [gallery-dl](https://github.com/mikf/gallery-dl) is GPL-2.0. | Keep yt-dlp; Instaloader is the preferred future isolated fallback. Do not import or copy gallery-dl into the Apache-2.0 core. |

## Rejected as direct dependencies

- [nilaoda/BBDown](https://github.com/nilaoda/BBDown) is MIT but was archived
  and emptied on 2026-05-14. It is not a maintainable dependency.
- [bilix](https://github.com/HFrost0/bilix) is Apache-2.0, but its current
  dependency graph includes GPL-licensed components and its latest release is
  old relative to frequently changing platform endpoints. It may be used only
  for behavioural comparison until a fresh dependency and redistribution audit
  is completed.
- [TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader) and
  [gallery-dl](https://github.com/mikf/gallery-dl) are GPL projects. Their
  designs may be studied, but their code is not copied into this Apache-2.0
  project.
- Projects based on undocumented platform APIs are not copied merely because
  they are open source. API stability, account/session handling and project
  maintenance are separate acceptance gates.

## Distribution boundary

The [yt-dlp licensing section](https://github.com/yt-dlp/yt-dlp/blob/master/README.md#licensing)
distinguishes source and artifact forms. Open-Flame therefore keeps the exact
[2026.08.19 release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)
Python/zipimport artifact locked by size and SHA-256, with the artifact-specific
`Unlicense AND MIT AND ISC` expression already recorded in
`THIRD_PARTY_NOTICES.md`; it does not silently replace it with a GPL-3.0-or-later
PyInstaller executable.

No fallback project listed above is currently bundled, imported, vendored or
redistributed by Open-Flame. Adding one later requires a new dependency lock,
third-party notice, target artifact hash and release-license review.

## Initial route scope

The first route for each platform is deliberately narrow:

1. Bilibili: public BV/av submission, default part only; `p > 1` is rejected.
2. Douyin: public `/video/{numeric-id}` item; its existing short-link resolver
   remains separately gated.
3. TikTok: public `www.tiktok.com/@handle/video/{numeric-id}` item; `vm`/`vt`
   short links are deferred.
4. Instagram: public `/reel/{shortcode}` item; posts, carousels, profiles,
   Stories and Live are rejected.

Cookies remain optional capability metadata, not a stability promise. Raw
cookies, session IDs, signed media URLs and platform response bodies must not
enter the database, API responses, manifests or runtime logs.
