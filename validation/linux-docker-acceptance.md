# Iteration 0.26.0 target Linux/Docker acceptance

Status: procedure and runner only. This acceptance has **not** been executed on
the current Windows development host, where Docker is unavailable. Nothing in
this document is evidence that Docker, the candidate image, the bundled real
tools, any Cookie, Stage 0, X graph-v2, or any real platform download passed.
The repository contains no accepted target-Linux/Compose full-run JSONL record.

The executable companion is
[`deployment/run-linux-acceptance.sh`](../deployment/run-linux-acceptance.sh).
It is intentionally narrower than a production rollout: it operates against a
new, empty acceptance data root, a deny-only egress policy, synthetic Cookie
fixtures, and an independently supplied Schema 11 backup. It never submits a
real media URL.

The Iteration 0.13.0 application contract separately retained ready-asset
thumbnail/caption listing and strict auxiliary downloads, gated TikTok
`vm`/`vt` short links with an exact hostname allowlist, and offline fake E2E
coverage for YouTube video/Shorts, Bilibili BV/av, gated Douyin short links,
and TXT/CSV import. This runner does not exercise those API/media paths or the
short-link egress service; their engineering evidence is not target Linux or
real-platform evidence.

Iteration 0.14.0 additionally connects the shared Cookie resolver to the
Windows-only local Worker and adds its no-claim preflight. That direct host
path is outside this Linux topology; the shared resolver's new empty/oversize
and cross-platform physical-source rejection remains covered by offline tests,
not by a completed target-Linux run.

Iteration 0.16.0 introduced Schema 11 and the Windows local-app claim gate; that
is retained historical engineering evidence. Iteration 0.26.0 advances this
runner's current application identity to `video-download-control==0.26.0` while
keeping Schema 11. The current adapter can emit redacted, bounded progress
control lines and persist conservative stage estimates, but this runner still
does not submit real media, exercise those events in a target container, or
turn the implementation into Linux or platform evidence. The empty-database and
independent-restore checks continue to require Schema 11 readiness; Schema 10
appears below only as a historical forward-migration waypoint.

The v0.19 Windows local-app short-link direct transport and explicitly configured
Cookie defaults are outside this Linux topology. They retain direct/non-isolated
host networking, do not auto-enable the generic control entry, and do not change
this runner's credential assignment or short-link egress coverage. Their
[offline/simulated-network engineering record](iteration-0.19.0-short-links-cookie-defaults-evidence.md)
is not real-platform, real-Cookie or target-Linux acceptance.

## Evidence and safety boundary

The runner has two modes:

- The default `--preflight` mode is read-only. It checks the target kernel,
  host core-dump policy, required host tools, Docker/Compose availability,
  canonical input files, and the effective Compose safety shape. It does not
  build an image, start or stop a container, create a socket, write a database,
  or restore a backup.
- `--execute` is mutation-capable and refuses to proceed without the exact
  authorization token `I_ACCEPT_TARGET_LINUX_MUTATIONS`, effective UID 0, an
  explicit private env file, a digest-pinned tool image, a Schema 11 backup root,
  and a new empty restore parent. It builds a freshly tagged local acceptance
  image, immediately captures its immutable `sha256:...` image ID, creates a
  scoped Compose project, confines application fixture writes to
  operator-prepared acceptance roots, and exercises the checks below. Docker's
  local image and build cache are separate retained engine artifacts.

Full execution also requires the effective Cookie injection contract to be
present. The product contract remains zero to six optional platform sources;
normal operation does not require Cookies. For acceptance only, the operator
enables six non-secret synthetic sources with distinct `acceptance-*` opaque
refs so the maximum mapping and permission boundary is exercised. Full
acceptance accepts only the reviewed wrapper-based mapping, because that shape
has the complete host ancestor, ACL, non-overlap, and file-identity preflight.
Direct per-file worker mounts remain part of the zero-to-six product contract,
but are rejected by full acceptance until they receive equivalent host checks.

Standard output is JSON Lines with only these fixed fields: `schema`, `run_id`,
`timestamp`, `check`, `status`, and `detail`. Command output is suppressed.
Details are fixed status tokens; they contain no environment value, host path,
container path, opaque ref, Cookie content, URL, signed URL, image reference,
or backup name. Do not add raw Docker output to this report.

The runner deliberately does not remove retained data, a restored tree, the
local image/build cache, or the stale-socket fixture. This avoids path-derived
deletion. Its temporary frozen Compose file is different: ordinary exit removes
it only after an identity/metadata snapshot match. A crash may leave it beside
the private env for exact-identity manual review. Other cleanup is a separate
operator action after the exact targets and evidence have been reviewed.

## Target prerequisites

Use a dedicated target Linux host or isolated Linux VM, not the current Windows
development machine. Before running:

1. Install a supported local Linux Docker Engine, reached through a local Unix
   socket, with the Compose v2 plugin, Bash, GNU-compatible `realpath`, `stat`,
   `find`, and `getfacl`. The runner uses fixed `/usr/bin/python3`, not a
   `PATH` lookup, and requires that exact interpreter to be Python 3.12+.
   Before any separate Cookie rotation, also verify and record that exact
   interpreter and that `/usr/bin/mv` is GNU coreutils with `-fT` support.
   Remote Docker contexts are outside this host-bind acceptance and are
   rejected. Endpoint detection gives non-empty `DOCKER_CONTEXT` official
   precedence over `DOCKER_HOST`, otherwise it inspects the current/default
   context; every result must be a local `unix:///...` Linux daemon. The Unix
   endpoint alone does not prove same-host/same-mount-namespace placement;
   record that operator attestation separately.
2. Review the candidate Dockerfile, dependency locks, and effective Compose
   config. The Dockerfile has no external syntax directive: the target daemon's
   bundled frontend must support Dockerfile 1.3 or newer and BuildKit must honor
   `RUN --network=none`. Record and approve the target daemon/frontend/BuildKit
   versions and configuration. If that syntax or behavior is unavailable, the
   image build must fail before any Cookie source is mounted; this has not been
   exercised on the Windows development host.

   The `wheel_builder` and `runtime` stages each hard-code the same literal
   `python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`;
   no Python-image ARG can override either `FROM`. Select the tool-bundle image
   by immutable `name@sha256:<64 lowercase hex>` digest. Both registries must
   make the reviewed manifests and target-platform images available; approve
   and record base/tool provenance rather than treating digest syntax alone as
   provenance.

   `pyproject.toml` and `requirements.build.in` fix `hatchling==1.27.0`.
   `requirements.build.lock` and `requirements.runtime.lock` contain exact
   versions and SHA-256 hashes. The only ordinary Python package network step is
   `pip download --no-deps --only-binary=:all: --require-hashes`; every later
   build-dependency install, project-wheel build, and runtime install uses both
   `RUN --network=none` and `--no-index`. Project build uses
   `--no-build-isolation --no-deps`, runtime installation names the exact
   project-wheel path, and `pip check` closes the install. Locks may include
   both wheel and sdist hashes, but command-level `--only-binary=:all:` rejects
   sdists; verify approved hashes, publisher/provenance, and compatible wheel
   availability for the target platform. Base/tool registry resolution is a
   separate Docker-network input.

   Use a reviewed source checkout with no unrelated sensitive files in the
   Docker build context; the runner does not certify arbitrary untracked
   content. Full mode can contact the approved registries and package index
   during image resolution and the one download step, outside the deny-only
   runtime proxy policy.
3. Prepare the private env and, when used, private Compose override outside the
   repository. Each must be a canonical, non-symlink, single-link regular file
   owned by `root:root`, exact mode `0600`, with base ACL entries only. Every
   ancestor through `/` must be canonical, root-owned, non-symlink,
   group/world-non-writable, and base-ACL-only. Keep both inputs non-overlapping
   with the dedicated data/socket roots. Set the candidate Worker gate to `1`,
   exact non-placeholder tool/policy versions, a non-zero candidate image
   digest for initial config validation, and dedicated host paths. Full
   execution snapshots these inputs and revalidates identity and metadata at
   mutation checkpoints.
4. Create empty data and socket roots owned by `10001:10001`, modes `0750` and
   `0770` respectively. Do not point them at an existing deployment. Prepare a
   deny-only egress policy whose sole active entry is `replace.invalid`. The
   policy must be a canonical, non-symlink, single-link regular file owned by
   `root:10001`, exact mode `0440`, 1–16384 bytes, with base ACL entries only.
   Every ancestor through `/` must be canonical, root-owned, non-symlink,
   group/world-non-writable, and base-ACL-only. Keep it bidirectionally
   non-overlapping with the repository, data/socket/recovery roots, private env,
   and private override. Full execution snapshots it and repeats both metadata
   and identity checks at mutation checkpoints.
5. Keep the host control plane stopped for this isolated run and keep
   `VDC_ENABLE_X_GRAPH_V2` unset or `0`. The real `YtDlpAdapter` does not
   support exact attachment selection, and this acceptance must not create
   graph Jobs.
6. Configure six non-secret, non-empty synthetic Cookie fixtures for X,
   YouTube, Bilibili, Douyin, TikTok, and Instagram. Use distinct
   `acceptance-*` opaque refs and
   use the canonical ownership/modes in the
   [deployment Cookie contract](../deployment/README.md#cookie-source-and-opaque-ref-contract).
   Do not set named access ACLs or any default ACL on the roots, ancestors,
   platform directories, mapping, or fixtures. With the current wrapper shape,
   full execution invokes the metadata-only host validator before any build or
   container start. The effective top-level `cookie-runner` config must point to
   the exact canonical checked-in `deployment/cookies/run-candidate-worker.sh`;
   a private override copied outside the repository should use that absolute
   path rather than a relative `./cookies/...` path. Do not copy a browser
   profile, session, token, or real Cookie into the fixture. For any later real
   deployment, a `nodev,nosuid,noexec` Cookie-source filesystem where supported
   is an operator prerequisite; this runner does not inspect/prove those host
   mount flags.
7. Supply a previously audited, non-sensitive Schema 11 acceptance backup
   outside the repository through a canonical read-only directory. It must
   contain no real Cookie, token, signed URL, or private media. Prepare a
   separate empty restore parent owned by `10001:10001` and mode `0750`. The
   restore parent must not overlap the backup, repository, data, or socket
   roots. A Schema 8, 9, or 10 backup must first be restored with its matching
   historical application and is not a direct input to this runner. For a
   Schema 8 or 9 artifact, migrate a working copy with v0.15 to the historical
   Schema 10 waypoint. Then open a writable working copy with v0.17, migrate it
   to Schema 11, validate readiness, and ensure it is re-backed up as Schema 11
   before acceptance.
8. Reserve a protected destination for the sanitized JSONL report. Record the
   change ticket, Docker versions, reviewed digests, host identity, and
   operator separately in protected evidence; the runner omits them to avoid
   leaking deployment metadata.

Before either mode, `/proc/sys/kernel/core_pattern` must be readable and must
not begin with `|`. If the target host uses a pipe collector, disable it or
change to an approved non-pipe pattern before any container start. Worker
`RLIMIT_CORE=(0, 0)` is still required, but it does not by itself exclude a
host pipe collector. The runner checks this setting during preflight and again
at mutation checkpoints; unreadable or pipe-configured state fails closed.

Do not use a production data directory, real queue, broad egress allow-list,
real Cookie source, or authorized Stage-0 sample for this runner. It validates
deployment boundaries, not platform behavior.

Pass the private env only with `--env-file`; do not source or export it first.
Full execution rejects inherited Compose interpolation variables so a shell
left over from another deployment cannot override the reviewed file.
It also rejects inherited `DOCKER_CONTEXT`, `DOCKER_HOST`, `DOCKER_CONFIG`,
`DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`, `BUILDKIT_HOST`, `BUILDX_BUILDER`,
`COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, and `COMPOSE_PROFILES`; therefore the
default context used in execute mode must itself resolve to the reviewed local
Unix Linux daemon.
Invoke the runner itself through a clean trusted root launcher with an
empty/scrubbed environment and an absolute trusted checkout path. Its shebang,
internal unsets, and fixed `PATH` run too late to neutralize loader/interpreter
effects such as inherited `LD_PRELOAD` before process startup. The reviewed
checkout/build context must remain immutable and under one trusted privileged
writer for the full run; physical aliases and pre-existing bind mounts remain
operator-attested exclusions.

Apply the same launcher boundary to the entire Cookie-rotation mutation script,
not only to its two `/usr/bin/env -i` validator calls. The rename must call the
verified absolute `/usr/bin/mv -fT`; an exported `mv` function or `PATH` shim is
not trusted. After rename and before normal validation or Worker recreate, a
trusted absolute Python 3.12 helper must receive only the already-whitelisted
platform parent path and `fsync` that directory descriptor. On open/fsync
failure, keep the Worker stopped. Same-directory rename is atomic but is not
crash-durable without that directory sync. This checklist does not claim that
rotation has been run on the target host.

## Read-only preflight

From the repository root on the target Linux host:

```bash
deployment/run-linux-acceptance.sh --preflight
```

To render a reviewed private configuration without starting anything:

```bash
deployment/run-linux-acceptance.sh \
  --preflight \
  --env-file /absolute/private/candidate.env \
  --compose-override /absolute/private/acceptance.override.yaml
```

The override is optional and must be a canonical, non-symlink regular file.
For full execution it must satisfy the exact private-input contract above:
outside the repository, single-link, `root:root`, mode `0600`, base-ACL-only,
with a safe root-owned ancestor chain. The env file has the same exact
contract; “owner-readable” alone is insufficient.
Preflight may finish `blocked` with exit code `2` when the target is not Linux,
Docker is unavailable, or another prerequisite is absent. A `pass` proves only
that read-only preflight checks succeeded; it does not clear the deliberate
candidate gates or prove a build/start.

## Authorized full execution

Only after reviewing every prerequisite, run the exact repository runner and
redirect standard output to the protected evidence destination:

```bash
deployment/run-linux-acceptance.sh \
  --execute \
  --authorize I_ACCEPT_TARGET_LINUX_MUTATIONS \
  --env-file /absolute/private/candidate.env \
  --compose-override /absolute/private/acceptance.override.yaml \
  --tool-image registry.example/vdc-tools@sha256:REPLACE_WITH_REVIEWED_DIGEST \
  --backup-root /absolute/read-only/schema11-backup \
  --restore-parent /absolute/empty/acceptance-restore-parent \
  > /absolute/protected/linux-acceptance.jsonl
```

With the current checked-in assets, do not omit `--compose-override`: full
execution requires the exact reviewed wrapper-based Cookie shape, while the
credential-free base intentionally has none. The runner never prints the
effective configuration. After validation and local image selection, it recursively
rejects `$` in every string key/value and writes the resulting JSON beside the
private env as `.vdc-effective-<run-id>.json`. Effective-root creation plus the
private parent contract must yield a `root:root`, mode `0600`, single-link,
base-ACL-only frozen file. A second `docker compose config` over that file must
be deeply equal to the original effective JSON, pass the complete validator
again, and leave its identity/metadata snapshot unchanged. All subsequent
mutation commands use that frozen file.

Full execution can pull build inputs, builds a local image tag, starts and
stops containers, initializes the empty acceptance database, creates a
synthetic queued Job and lease, writes an independent restore tree, and leaves
one stale Unix socket for explicit cleanup. The acceptance Compose project is
stopped and removed at the end; the local image, build cache, and host files are
retained.

The generated tag is only the fresh-build locator. Immediately after build,
the runner resolves and validates a local `sha256:...` ID, injects that ID into
the frozen effective Compose model, uses it for every direct `docker run`, and
requires runtime `docker inspect` to report that exact ID for every container.
Mutation checkpoints also re-inspect the ID. Rebinding the tag therefore cannot
redirect the accepted execution.

## Checklist and expected evidence

Every required check must have status `pass`, followed by exactly one passing
`summary`. A `fail`, `blocked`, missing check, duplicate summary, truncated
line, or non-JSON output means the run is not accepted.

A successful full run with the required reviewed override emits these check IDs
in order. The initial `meta` record has status `info`; every other record has
status `pass`.

```text
meta
fixed_assets
host_linux
host_coredump_policy
host_tools
config_inputs
docker_daemon
compose_config
authorization
root_execution
graph_gate
private_env
compose_override
tool_image
cookie_compose
cookie_host_metadata
dedicated_roots
deny_only_policy
scoped_project
image_build
image_identity
image_contract
image_runtime_contract
cold_config
cold_start
base_health
runtime_policy
empty_database
worker_start
service_health
runtime_hardening
worker_core_dump
runtime_namespace
namespace_uds
ssrf_policy
peer_attestation
cookie_mount_permissions
worker_sigterm
sigterm_lease_recovery
worker_restart
schema11_restore
stack_shutdown
stale_socket
audit_redaction
summary
```

| Check area | Runner evidence | What it establishes | Important limit |
|---|---|---|---|
| Host input integrity | `private_env`, optional `compose_override`, `deny_only_policy` | Private files satisfy exact `root:root`/`0600`/single-link/base-ACL/safe-ancestor requirements; policy satisfies exact `root:10001`/`0440`/1–16384-byte/single-link/base-ACL/safe-ancestor requirements. Protected paths do not overlap, and recorded identity/metadata snapshots are revalidated at mutation checkpoints. | Canonical string checks do not discover physical aliases or pre-existing bind mounts; trusted exclusive host control remains required. |
| Compose config | `compose_config`, `root_execution`, `cold_config` | Execute confirms effective root. Four services, no published ports, none/shared-none namespace shape, non-root/read-only/cap/resource declarations, candidate gate, immutable image references, and optional Cookie injection structure survive effective rendering. The validated local image ID is embedded in `$`-free effective JSON, frozen beside the env, re-rendered, deeply compared, fully revalidated, snapshotted, and used for all Compose mutations. | The file contains deployment configuration. Normal exit removes only the unchanged recorded identity; a crash can leave a protected remnant requiring manual exact-identity cleanup. |
| Image build | `scoped_project`, `image_build`, `image_identity`, `image_contract`, `image_runtime_contract` | The generated project/tag were unused; two Python stages use the same hard-coded digest (still subject to release review), the tool bundle is digest-pinned, the Python closures are exact/hash locked, post-download package operations are networkless/no-index, and the resulting tag is resolved to an immutable local image ID. Image contract inspection, every direct helper run, frozen Compose, checkpoint re-inspection, and each runtime container are bound to that ID. A network-none direct run checks exact versions for the 13 runtime-lock distributions plus `video-download-control==0.26.0`, rejects the five build-only distributions, confirms Schema 11, and keeps the real adapter's exact-selector capability false. | This does not validate a platform request, registry availability, package/image provenance, target wheel availability, or the target daemon's bundled frontend/BuildKit behavior by itself. Full Linux execution must show Dockerfile 1.3+ `RUN --network=none` is supported and enforced; no such run occurred on Windows. |
| Cold start and health | `cold_start`, `base_health`, `worker_start`, `service_health` | A new scoped project starts sandbox, proxy, relay, then Worker; all declared health checks become healthy. | Health is necessary, not proof of media correctness. |
| Runtime hardening | `host_coredump_policy`, `runtime_hardening`, `worker_core_dump` | Preflight reads host `/proc/sys/kernel/core_pattern` and accepts only a non-pipe value; mutation checkpoints repeat that test. Docker inspect reports non-root, read-only root, `cap_drop=ALL`, no-new-privileges, PID/memory/CPU limits, and no published port for every service; Worker inspect and in-container `RLIMIT_CORE` both report soft/hard zero. | `RLIMIT_CORE=0` alone does not exclude a host pipe collector. Any unreadable or `|`-prefixed host policy blocks acceptance; kernel/cgroup enforcement outside the inspected values remains host-operations evidence. |
| Namespace and UDS | `runtime_namespace`, `namespace_uds` | Docker reports sandbox `none`, Worker/relay sharing that exact namespace, and only proxy on a bridge; the Worker's Linux guard observes loopback-only state and reaches the relay through the expected Unix-socket path. | It does not make the unauthenticated control plane safe for network exposure. |
| SSRF and peer checks | `ssrf_policy`, `peer_attestation` | Live proxy CONNECT attempts to loopback, RFC1918, and metadata addresses are rejected; private DNS resolution and a mismatched connected peer are rejected by the shipped security helpers in-container. | The peer-mismatch case is deterministic and does not claim a real DNS-rebinding or Internet endpoint test. |
| Cookie ceiling and permissions | `cookie_compose`, `cookie_host_metadata`, `cookie_mount_permissions` | Full acceptance requires the exact reviewed wrapper config, rejects every Compose secret and non-worker config, and verifies worker-only/read-only injection. The host validator rejects runtime/repository overlap, unsafe ancestors, and extended/default ACLs; in the full six-source synthetic case, all refs are unique, files are regular/single-link/non-writable, read access works, and write-open fails. | Synthetic content proves no authentication, freshness, platform acceptance, or rotation success. Rotation separately requires a clean trusted root launcher, verified absolute GNU `/usr/bin/mv -fT`, and post-rename platform-directory `fsync`. Normal deployments may configure zero to six sources; direct per-file mounts are outside this acceptance until equivalent host validation exists. |
| SIGTERM and lease recovery | `worker_sigterm`, `sigterm_lease_recovery`, `worker_restart` | The real idle Worker exits on the stop sequence without OOM or forced-kill status; a separate synthetic holder is terminated by SIGTERM, its 10-second lease expires, Attempt 1 becomes abandoned, Attempt 2 reclaims the same Job, and the Worker becomes healthy again. | Exit `0` or signal-derived `143` is accepted; this does not claim an in-flight real downloader subprocess was terminated safely or that the Python Worker installs its own SIGTERM handler. |
| Schema 11 restore | `schema11_restore` | The backup CLI restores into a previously nonexistent independent child root and a separate read-only run confirms Schema version 11 readiness. The result is retained. | The supplied backup's prior audit, storage capacity, offsite copy, RTO, and RPO remain separate evidence. A Schema 8, 9, or 10 backup must first be restored with its matching historical application; older copies pass through the v0.15/Schema 10 waypoint where needed, then a writable copy is migrated by v0.17 and re-backed up as Schema 11. It is not a direct input to this check. |
| Stale socket | `stale_socket`, `stack_shutdown` | Graceful proxy stop removes its socket; a deliberately stale socket then prevents proxy health without being replaced, and the runner retains its inode for manual cleanup. | An authorized trusted supervisor must remove it later after confirming no process owns it. |
| Report boundary | `audit_redaction`, `summary` | The successful path emitted fixed-field sanitized JSONL and suppressed command output. | Operators must still protect the report and separately redact any troubleshooting material. |

## Manual review and cleanup

Validate the report with a local JSON parser and compare its check IDs with the
table. Do not paste the private env, rendered Compose, Docker inspect output,
Cookie metadata, backup manifest, or daemon logs into the report. If
troubleshooting is required, collect it into a separate protected workspace,
review it for paths, refs, URLs, headers, and secrets, and do not relabel a
failed run after editing its output.

On an early failure, the exit trap tries to stop the exact scoped fixture
container and Compose project but deliberately does not remove either. A
`cleanup` failure record means an operator must immediately inspect the target
and stop those exact objects; never assume the failed runner left them stopped.

Ordinary exit also attempts to remove `.vdc-effective-<run-id>.json` from the
private env directory, but only after the file still satisfies the private
metadata contract and matches its recorded identity snapshot. Crash/forced
termination can leave this sensitive configuration artifact; an
identity-changed file is deliberately retained with a cleanup failure. Inspect
the exact path, owner/mode/ACL/link count and device/inode under protected
change control, then remove only the confirmed unchanged runner artifact. Do
not glob or recursively clean `.vdc-effective-*` files.

After evidence review, an authorized operator may remove the exact scoped
acceptance objects. Before removing the retained stale socket, verify the
canonical socket-root path, re-read and record its current inode in the
separately protected cleanup record, confirm the acceptance Compose project is
down, and confirm no process has the socket open. The sanitized runner report
records only that the inode stayed unchanged during the refusal test. Use the
host's trusted service supervisor or a narrowly targeted manual operation. The
runner contains no recursive delete, `eval`, Docker prune, volume removal, or
cleanup based on unresolved variables.

The retained Schema 11 restore must be inspected before deletion. Confirm the
restored database remains ready when mounted read-only and that no Cookie file
or deployment-private mapping entered the backup payload. Delete the local
acceptance image, data fixture, restore tree, report, and stale socket only
under the target host's approved retention and change-control procedure.

## Acceptance decision

A clean runner summary is required but not sufficient. Target Linux/Docker
acceptance is complete only when an operator reviews the JSONL, the separately
protected host/version/digest record, retained restore, and exact cleanup
record. Keep the conclusion scoped to Linux/Docker deployment mechanics.

Do not infer or write any of these conclusions from this acceptance:

- Docker or this checklist passed on the current Windows host;
- real yt-dlp, FFmpeg, ffprobe, Cookie authentication, or platform media passed;
- X stable attachment keys or exact-one-selector behavior passed;
- Schema 11 graph-v2 is safe to route to the real candidate Worker;
- any platform, Stage 0, disaster recovery program, or production deployment is
  `verified`.
