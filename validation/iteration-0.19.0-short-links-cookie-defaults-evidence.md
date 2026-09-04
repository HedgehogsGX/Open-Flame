# Iteration 0.19.0 Windows short links and Cookie defaults

Date: 2026-09-04

Product: `video-download-control` `0.19.0`; database Schema `11` (unchanged).

Status: final Windows engineering record and frozen project-only package acceptance contract.

## Scope

This iteration connects two existing lower-level features to the ordinary Windows application. `LocalAppConfig.allow_direct_network` now enables a control-owned short-link transport. It reuses the existing signed protocol, numeric-IP HTTPS connection, TLS hostname and peer verification, bounded HTTP parsing and redirect policy. No new listener, persistent loop thread, proxy, UDS file or shared secret file is introduced. Each request has a fresh in-memory key/service/replay store; the shared transport admits at most four requests and refuses new work after closure. In-flight requests keep their existing bounded deadline and cleanup. This is explicitly direct/non-isolated, not Linux egress isolation.

Both synchronous JSON and asynchronous TXT/CSV import routes execute normalization/resolution off the event loop. The old import path was reproduced with a resolver observing a running event loop; after threadpool offload it passed. Local-app wiring tests were red before the resolver factory was connected: JSON/TXT/CSV returned no queued short-link Jobs. They are now green using the actual resolver/transport and an injected numeric connector. The generic control environment never implicitly enables this Windows path; its existing opt-in POSIX UDS configuration remains unchanged.

Cookie configuration v2 adds the exact `default_cookie_platforms` list; source entries still contain only `platform`, `credential_ref`, and `path`. Version 1 remains source-only. A normal local-app control child prepares explicitly chosen profiles for the current supervisor run; it registers missing profiles or reuses an unambiguous enabled, unexpired matching profile. It never revives disabled/expired profiles. Config and source metadata are revalidated without recording Cookie contents. `--check`, v1, and empty defaults do not register profiles. `--check` is not completely read-only: existing database/log/gate initialization still occurs.

New Jobs bind their profile inside the creation transaction before any Worker claim can see them. Explicit retry changes the binding in the same transaction as the new generation. JSON and file imports default to `use_default`; absent mappings remain anonymous. Explicit `anonymous` removes Cookie use. Retry with no body preserves the previous binding for compatibility. Invalid configured defaults fail with a fixed error rather than silently falling back. Source-level live/ready dedup creates no new Job and never overwrites an existing Job's profile, including when modes differ.

The page offers a native selection and reports only configured platform names and current file/profile availability. `available` does not prove platform login validity. No HTTP endpoint accepts Cookie contents, private paths, opaque refs or profile IDs. Existing attempt-private Cookie copies and cleanup remain in use.

## Focused evidence

| Slice | Observed result and limit |
|---|---|
| Local transport | 23 tests cover admission, closure, timeout/reader cleanup, peer/header/body policy, event-loop misuse and failure redaction; no real network |
| Short-link application integration | 15 tests cover local-app settings → app factory → JSON/TXT/CSV → four ready synthetic assets per input format; suspended file import leaves health/cancel responsive |
| Default configuration/repository | 30 tests cover strict v1/v2, partial registration rollback, ambiguity/disabled/expired/ref drift, commit-time configuration drift, create/retry claim races and non-overwriting dedup |
| API contract | 14 tests cover creation, narrow availability response, invalid modes, config drift, explicit anonymous and default/preserve retry choices |
| Real Worker code with synthetic external output | 6 tests exercise JSON/TXT/CSV × both modes through real Worker, YtDlpAdapter, command factory and AttemptCookieResolver; probe/download get separate copies only in default mode; copies are deleted before another maintenance cycle; ready original download matches synthetic bytes |
| Local-app child | 5 tests exercise actual child setup, SQLite and app lifespan with a non-listening server double; current run binding, normal registration, check/v1/empty non-registration and existing Job/profile preservation |
| Executable frontend | 9 tests run the complete real page script in Node with a DOM/fetch harness; state/error rendering, all input modes and click-time retry selection are verified |
| Browser smoke | Independent control-only loopback 18820 instance, synthetic configuration and no Worker/network: default then anonymous creation, cancellation, visible mode/status and layout checked; DB confirms profile-bound versus NULL; owned service stopped and port released |

The first browser smoke was performed before version metadata was frozen. A second smoke after source freeze repeated both create/cancel choices and checked the runtime-log panel after refreshing Cookie status: version 0.19.0, log status normal, zero rejected events and zero write failures. Both temporary instances were stopped. The full-page script regression and final package checks separately bind the frozen source. One stale AX node caused by active polling was re-observed and resolved with the stable button label; this was not a product failure.

Independent review found a real integration bug: the new availability route was missing from the runtime-log route allowlist. A red regression reproduced `ok` becoming `degraded` immediately after GET. The route is now explicitly permitted and normal status polling uses DEBUG severity; the regression also checks that a DEBUG log actually contains the route. Another test had fetched a nonexistent log endpoint and examined a 404 body; it now requests the actual logs endpoint and asserts HTTP 200 before testing redaction. Focused API/log/UI regression after these fixes: `65 passed in 13.10s`.

API/status/logs, asset `source.json` and `manifest.json` were checked against synthetic Cookie value, opaque ref, source/config path and private-copy markers. No new third-party dependency or license was introduced. All six platforms remain `candidate`; there were no real Cookie/account uses, real media requests, Stage 0 runs, Linux/Docker checks, installer runs, new commits or pushes in this iteration. Project-owned materials remain Apache-2.0 with `Copyright 2026 HedgehogsGX & Cyaegha_Xu`. Third-party binary/container/tool-bundle redistribution is a separate unresolved gate.

## Final checks

| Final check | Result |
|---|---|
| Full repository suite | `1410 passed, 8 skipped in 135.51s` |
| Static checks | `compileall -q src tests`, `uv lock --check --offline` (24 packages), `git diff --check`; 35 Markdown files, zero missing relative links |
| Spawned Windows app | Actual `local_app_cli --check` with an independent app root, pinned existing tools and readonly v2 synthetic config: exit 0, checked, about 5.7 seconds, port 18821 released |
| Spawned state | All three processes exited; 18 JSONL events share one run across supervisor/control/local-worker; preflight and stops present; no activation/claim/browser; profiles, batches, Jobs, Attempts and media assets all zero; source/config unchanged |
| Package precheck | Offline sdist → exact-sdist wheel, isolated locked runtime install, source equivalence, RECORD, 32 legal files, exact NOTICE, 13 CLI help commands and 14 distributions passed; no privacy marker/pattern hits |

The eight skipped tests require the other host: POSIX directory-FD cleanup (one), root/getfacl Cookie metadata (four), AF_UNIX (one), POSIX open-file replacement (one), POSIX permission bits (one). They are not passes.

An earlier full run returned three failures: two stale homepage wording expectations and a build-identity drift caused by concurrent source edits. The wording contracts were updated to the preserved/new controls, source editing stopped, and the final full suite above passed. No fail-closed identity check was removed.

The precheck is not the final release archive: production changes made during subsequent review invalidate that precheck identity. Its 231 checkout files did not yet include this evidence document. The documentation-complete inventory therefore contains 232 checkout files and 233 sdist regular files including PKG-INFO; the wheel payload counts are unchanged. After this documentation is frozen, rebuild into a fresh output directory and require the independently derived source/metadata/privacy contract below. Exact archive hashes and the final verification result belong to the external final verifier report, not to a document included inside those archives.

```text
FINAL_FULL_PYTEST: 1410 passed, 8 skipped in 135.51s
FINAL_STATIC_CHECKS: compileall; offline lock (24 packages); diffcheck; 35 Markdown files / 0 missing links
FROZEN_PACKAGE_CONTRACT: 232 checkout source; 233 regular sdist; 63 byte-identical package files; 99 wheel entries and RECORD rows
FROZEN_METADATA_CONTRACT: version 0.19.0; Apache-2.0; exact Copyright 2026 HedgehogsGX & Cyaegha_Xu; 32 legal; 13 CLI; 14 runtime/project distributions
FROZEN_PRIVACY_CONTRACT: 8 known-real markers and 11 high-confidence secret patterns, zero hits; synthetic path canaries separately classified
FROZEN_PACKAGE_PAYLOAD_IDENTITY: 0.19.0+build.sha256.cd7c1cbf8b4bda678693848b050b37d033f30cbd3e94421db92be7c4efe66d96
```

## Next validation

Continue whole spawned-app and authorized real-platform checks in Bilibili → Douyin → TikTok → Instagram order. A default source being available is not evidence of fresh platform authentication. Keep current stop/claim, two-slot/per-platform limits, and credential copy cleanup regression checks. Validate host-specific skipped tests on Linux and review real Windows private file permissions before real credentials. Do not reuse a previous iteration's archive hash or promote synthetic evidence to platform support.
