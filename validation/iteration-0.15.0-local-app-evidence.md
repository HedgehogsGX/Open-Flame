# Iteration 0.15.0 Windows local application engineering evidence

Date: 2026-09-04\
Product: `video-download-control` `0.15.0`\
Database: Schema `10`

## 1. Scope and evidence boundary

This iteration turns the existing Windows control plane and direct local Worker
into one ordinary entry point, `video-download-local-app`. The evidence below
covers process supervision, configuration binding, no-claim preflight,
structured logging, the bundled web interface, package identity, and
project-authored licensing.

No real Cookie was used and no new real-platform download was attempted. The
browser submission used a reserved `.invalid` host and terminated locally as
`unsupported_platform` before any Job, Attempt, Asset, commit intent, or
external request was created. Therefore this record does not change any
platform from `candidate`, does not qualify Stage 0 evidence, and does not prove
the Linux/Docker candidate.

Raw application roots, randomized ports, process IDs, run IDs, local usernames,
database files, JSONL logs, and the synthetic input remain only in gitignored
local QA directories. A pre-existing loopback service was not stopped,
replaced, or reused during this validation.

## 2. Integrated startup and configuration contract

- The default Windows root is `%LOCALAPPDATA%\Open-Flame\video-download-control`.
  One resolved application root deterministically owns `data`, the Schema 10
  database, logs, temporary files, and the default tool root. Explicit
  overrides must be absolute plain local paths; root, UNC/device, reserved,
  trailing-dot/space, link/reparse, and overlapping app/data/tool/config/Cookie
  paths fail closed.
- The supervisor reserves the loopback listener before creating database or
  child-process side effects. It then performs strict private handshakes for the
  control process, Worker preflight, and Worker claim gate. Each handshake is
  bound to protocol, role, phase, run ID, build identity, and Schema; HTTP
  health and capability snapshots are checked before the browser can open.
- `--check` executes the same configuration, toolchain, logging, database,
  Cookie-config, and build-identity preflight without opening a browser or
  letting the Worker call `run_once()`. Success prints only
  `{"status":"checked"}` and releases both children and the listener.
- One `.local-app.lock` prevents two supervisors from owning the same app root.
  On Windows, a kill-on-close Job Object contains only the application's own
  children. Normal and failure cleanup closes the Worker command channel and
  waits for it before stopping control; a remaining child is terminated only
  through that owned Job.
- Cross-process launch, claim, and check-complete signalling uses strict
  one-byte commands on one-way Pipes; EOF means owner shutdown. A check Worker
  records `check_complete` only after the supervisor has completed the second
  HTTP identity check and sent the dedicated completion command. This replaced
  a shared `multiprocessing.Event` design that could leave the parent blocked in
  `Event.set()` after an abruptly killed receiver retained a synchronization
  lock.
- `--cookie-config` exposes only one protected configuration-file path in the
  command line. The strict JSON snapshot maps platform and opaque reference to
  existing source files; its contents and resolved source paths are not emitted
  by normal CLI output or allowlisted logs.

## 3. Automated and real-process validation

| Check | Result |
|---|---|
| Local-app and control-CLI slice | `96 passed` |
| Supervisor/API/Cookie focused slice | `249 passed, 1 skipped`; the skip is the documented POSIX-only Cookie directory-FD case |
| Full repository regression | `1196 passed, 8 skipped in 93.06s` |
| No-claim preflight | Real spawned control and Worker returned `checked`; both logs used the same run ID and recorded `check_complete` only after the explicit completion command; zero listener and zero related child remained; all four work tables stayed empty |
| Repeated lifecycle | Ten consecutive isolated checks succeeded with ten distinct run IDs and ten complete app/Worker stop sequences; no process, listener, Job, Attempt, Asset, or commit intent leaked |
| Singleton | A second supervisor for the same root failed while the first remained healthy; the first then stopped normally |
| Console stop | Real Ctrl+C/SIGBREAK handling produced a bounded Worker-first shutdown and released the listener |
| Abrupt Worker exit | Killing the authenticated Worker made the supervisor return the fixed `local_app_failed` JSON error, stop control, and release the listener instead of hanging |
| Abrupt control exit | Killing the authenticated control process produced the same bounded owned-child cleanup |
| Dead receiver regression | A real spawned command receiver was terminated while waiting; closing its one-way Pipe could not block parent cleanup |

All eight full-suite skips are explicit target-environment boundaries: one
POSIX Cookie directory-FD cleanup test, four root POSIX/getfacl Cookie metadata
contracts, one AF_UNIX roundtrip, one POSIX open-file replacement case, and one
POSIX permission-bit case. They require target Linux execution and are not
counted as passes.

Shutdown is cooperative and bounded, not linearized with SQLite claim. If a
stop request races the already-completed command-channel check at the start of
a Worker cycle, the Worker can enter at most one additional cycle and may claim
one Job; the next channel check observes EOF, and any interrupted Attempt
remains subject to the existing lease-recovery contract. Eliminating this
window requires a database-linearized stop/claim gate and is intentionally left
for a separate iteration rather than being hidden by an extra non-atomic poll.

## 4. Browser and logging validation

An isolated `video-download-local-app` instance was opened through the bundled
web interface. The page showed v0.15.0, Schema 10, all seven declared capability
rows, and the exact pinned tool versions. Capability, tool, log, and recent
Batch refresh controls all completed and re-enabled. The safe invalid
submission produced one failed Batch/Input with `unsupported_platform` and no
work-table rows. Reopening the Batch worked, and the browser console contained
zero warning or error entries.

The UI wording now states that ordinary Windows use is supervised by
`video-download-local-app`; it no longer tells that user to start a separate
Worker. DOM inspection confirmed one form, one tool panel, one log panel, one
recent-Batch panel, and six top-level sections after refresh.

An installed-entry audit also found and fixed a separate usability defect:
`video-download-control --help` previously ignored the argument and attempted
to start the server. It now parses help before loading runtime settings, and a
regression proves that help has no runtime side effects. All 13 command modules
return success for `--help`.

Each real start wrote separate `runtime-local-app`, `runtime-control`, and
`runtime-local-worker` JSONL streams under the same data-log root. All three
components shared one random `run_id`, lifecycle events were ordered, and scans
found no synthetic URL, Batch name, Cookie marker, opaque reference, source
path, local username, or raw exception/path leakage. Logs remain best-effort
local diagnostics; they do not replace SQLite, manifests, backups, centralized
monitoring, or alerts.

## 5. Package, privacy, and license validation

After this evidence file was added, the project-only source distribution was
rebuilt offline in a fresh gitignored directory and the wheel was built from
that exact sdist. The final archives contain 211 source-tree files plus one
generated `PKG-INFO` entry in the sdist, and 96 files in the wheel. Both archives
have unique safe paths and no link or special entries; the wheel passes CRC and
all 96 `RECORD` rows.

Wheel metadata and an isolated import both report `0.15.0` and `Apache-2.0`.
All 13 project console scripts are present, including
`video-download-local-app`; 32 license files are present. The 60 importable
package files in both archives match the checkout byte-for-byte, and the
checkout and isolated wheel produce the same package-payload build identity.
The isolated runtime contains the 13 exact hash-locked runtime dependencies
plus the project package and passes dependency checking.

Archive and checkout scans found no local username/path, messaging-app path,
historical real test URL/ID, Cookie, database, log, media, `runtime-tools`, or
`validation/local` payload. High-confidence private-key and provider-token
patterns found no secret. A broader keyword heuristic only matched ordinary
source identifiers such as `schema_cookie` / local `cookie` variables and
explicit synthetic redaction fixtures; manual review found no credential.

The project-authored source, documentation, and scripts remain licensed under
Apache-2.0 with `Copyright 2026 HedgehogsGX & Cyaegha_Xu`. The exact Python
license copies and notices remain covered by the checked-in hash manifest.
This does not relicense downloaded media or third-party components. Dependency
wheelhouses, frozen executables, offline installers, OCI/container images, and
the yt-dlp/FFmpeg tool bundle remain
`blocked_pending_third_party_source_and_notice_audit` until their target-specific
source, SBOM, notices, relinking obligations, provenance, and human review are
complete.

## 6. Remaining acceptance work

- Build and exercise an installer/frozen executable only after the Windows
  target-specific third-party redistribution gate is complete.
- Prove private owner/DACL behavior for the app root, Cookie config, and Cookie
  sources with real Windows identities before using real credentials.
- Implement a database-linearized shutdown/claim gate and add active-Attempt
  stop/recovery integration coverage.
- Add explicit terminal retry/new-generation and cooldown visibility before
  resuming Bilibili 412 investigation.
- Execute the eight skipped contracts and the full Docker acceptance runner on
  a clean authorized target Linux host.
- Run authorized Stage 0 v3 evidence independently for each exact platform and
  route identity; this engineering record supplies no such approval.
