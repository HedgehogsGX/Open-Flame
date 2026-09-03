# Docker deployment candidate (Iteration 0.10.0 / v0.10.0 / Schema 8)

This directory is a fail-closed deployment skeleton, not a verified deployment recipe. Docker was unavailable on the development host, the image was not built, and Stage 0 has not supplied a reviewed egress host set. The separate v0.9.0 Windows direct Worker completed two exact user-supplied samples end to end, but it does not run this topology or prove its Linux isolation. Implemented Schema 8 graph-v2 orchestration does not change that boundary. Do not mark any platform, the Docker candidate, or an X graph path `verified` from these files.

The project-authored Dockerfile, Compose files, validator, runner, and documentation are licensed under Apache-2.0. That source-code license does not approve any resulting dependency wheelhouse, OCI image, frozen executable, or yt-dlp/FFmpeg tool bundle for redistribution; the target-specific third-party gates below remain mandatory.

Iteration 0.5 wired admin-only Job assignment and claim-time `credential_profile_id` validation to the opaque `credential_ref` used by Adapter probe/download requests. The public Batch API rejects this field. Iteration 0.6 added Schema 8 parent discovery, immutable snapshots and per-attachment children, but that graph path has only been exercised with the offline `ScriptedGraphFakeAdapter`. Iteration 0.7 added a static, deployment-owned Cookie injection override, a default-read-only Linux/Docker acceptance runner and a separate, default-disabled short-link UDS transport; no Cookie file, Compose container or short-link egress process was exercised. Iteration 0.8.1 directly exercised the pinned Windows tools against one authorized YouTube URL and one authorized X URL. Iteration 0.9.0 then exercised those same exact samples through the host API queue, Windows Worker, real yt-dlp/FFmpeg/ffprobe, AssetStore/manifest and asset-download path. Those two samples remain historical/narrow evidence, not platform-wide or deployment evidence. The Linux candidate still uses `YtDlpAdapter.supports_exact_selector=False` and cannot safely select one X attachment. The historical Iteration 0.5 / Schema 7 and Iteration 0.6 / Schema 8 Windows restore records also remain valid, but neither covers this deployment topology.

## v0.9.0 host-local state outside this topology

- `video-download-local-worker` is an explicitly integrated Windows-only, direct-network Worker. It refuses to start or claim unless `VDC_ENABLE_LOCAL_REAL_WORKER` is exactly `1` **and** the CLI includes `--allow-direct-network`. It is not started by Compose and is not a substitute for `video-download-candidate-worker`.
- The host control plane and local Worker must use the same canonical data root and the same existing Schema 8 database. Pass the same `--data-root` and, preferably, the same explicit `--database-path` used by `VDC_DATA_ROOT`/`VDC_DATABASE_PATH`; creating a second database would create a different queue. One local Worker lock is allowed per data root.
- A Windows Node example is `--js-runtime "node:C:\Program Files\nodejs\node.exe"`. In practice, resolve `node.exe` to an absolute plain-file path (for example `$Node = (Get-Command node.exe).Source`, then `--js-runtime "node:$Node"`). Node is not part of the pinned `runtime-tools` bundle and the Windows path must not be copied into a Linux candidate configuration.
- The local Worker uses a flat-only claim policy: `discover` and `x_attachment` Jobs are excluded in the same claim transaction, remain queued, and do not prevent a later flat-v1 `download` Job from being claimed. This is an atomic skip, not graph-v2 support; keep `VDC_ENABLE_X_GRAPH_V2=0`.
- `GET /api/v1/batches/{batch_id}/assets` lists ready original metadata and a relative `download_url`. `GET /api/v1/assets/{asset_id}/download` serves only the DB-registered ready original after data-root containment and plain/non-link/single-link checks. The frontend renders these links after the Batch becomes ready.
- `/health` reports `worker=external_status_unknown` while the queue is available (or `worker=paused` when paused). `external_status_unknown` deliberately means there is no external-Worker heartbeat; it is not an online, offline or startup assertion.
- The exact YouTube and X samples completed Worker/AssetStore/manifest E2E, but both platform statuses remain `candidate` because sample coverage, Cookie cases, graph attachment selection, Linux isolation and Stage 0 are incomplete. The direct-tool evidence remains [Iteration 0.8.1](../validation/iteration-0.8.1-live-platform-evidence.md); the Worker E2E summary is [Iteration 0.9.0](../validation/iteration-0.9.0-local-worker-e2e-evidence.md).

## What the topology enforces

- `sandbox` owns a Docker `network_mode: none` namespace.
- `relay` and `worker` share that namespace, so they have loopback but no ordinary Docker network.
- `egress-proxy` is the only service attached to the outbound bridge. It accepts traffic only over a host-prepared Unix-socket directory and revalidates DNS and the connected peer.
- No service publishes a host port. The unauthenticated FastAPI control plane remains a host-loopback process; this candidate does not weaken its bind guard.
- All four containers use UID/GID `10001`, a read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded tmpfs, PID, memory, CPU and file-descriptor limits.
- The real Worker needs an exact feature gate, exact tool versions and a digest-pinned image. It checks the Linux namespace before database initialization and again before every queue claim.
- The candidate real Worker does not advertise exact-selector support. A Schema 8 graph `discover` or `x_attachment` Job fails closed at claim; operators must prevent such Jobs by keeping the host control plane's `VDC_ENABLE_X_GRAPH_V2=0`.
- The base [`compose.candidate.yaml`](compose.candidate.yaml) is the credential-free mode and has no Cookie path, mapping, mount or runner. Only when [`compose.candidate.cookies.yaml`](compose.candidate.cookies.yaml) is explicitly layered does `worker` receive the read-only Cookie root, private opaque-ref mapping and checked-in runner config; `sandbox`, `relay`, `egress-proxy`, the host API and backup tooling receive none.
- A blank platform ref means no Cookie for that platform. The runner appends `--cookie-source` only for a non-empty validated ref whose fixed platform file exists; an unmapped file, wrong platform/ref pair or unknown mapping key fails closed.
- The Worker validates every configured source before database initialization and copies only the selected source to an attempt-private `0600` file.

## Deliberate blockers

The checked-in environment and host policy are intentionally unusable:

- image references point at `replace.invalid` and a zero digest;
- the feature gate is `0`;
- the host control plane's separate `VDC_ENABLE_X_GRAPH_V2` gate must remain unset or `0` for this candidate;
- tool and policy versions are placeholders;
- the credential-free base Compose does not reference Cookie assets; the optional override's separate mapping template has six blank refs, and no Cookie file exists;
- no Cookie source or Cookie-format example is checked into the repository;
- [`egress-hosts.candidate.txt`](egress-hosts.candidate.txt) allows only `replace.invalid`.

This means copying the examples cannot accidentally start real downloads.

## Image build contract

[`Dockerfile.candidate`](Dockerfile.candidate) has no external `# syntax` directive. Its `wheel_builder` and `runtime` stages each hard-code the same literal `python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`; there is no Python-image build argument that can override either `FROM`. The digest was read from the registry but neither stage was pulled or executed locally. The target registry must still make that manifest and its selected platform image available, and the release owner must approve and record its provenance.

Without an external syntax image, the target Docker daemon's bundled Dockerfile frontend interprets this file. The candidate requires Dockerfile syntax support for `RUN --network=none` (Dockerfile 1.3 or newer) and BuildKit behavior that actually denies network access for those steps. That removes the old mutable frontend-tag dependency, but it does not make daemon/frontend version, configuration or enforcement reproducible by itself; full target Linux execution must record and validate them, and a failure occurs during image build before any Cookie source is mounted.

Python packaging is also fail-closed:

- `pyproject.toml` names the exact build backend requirement `hatchling==1.27.0`; [`requirements.build.in`](requirements.build.in) repeats that direct pin and [`requirements.build.lock`](requirements.build.lock) locks its complete build dependency closure with SHA-256 hashes.
- [`requirements.runtime.lock`](requirements.runtime.lock) locks the complete non-development runtime dependency closure exported from `uv.lock`, again with exact versions and hashes.
- The only Python dependency command allowed normal build-network access is the builder's `pip download --no-deps --only-binary=:all: --require-hashes`, which fills `/locked-wheels` from both locks. Registry access needed to resolve the digest-pinned base/tool images is a separate Docker build input and remains an availability/provenance boundary.
- Build-dependency installation, the project-wheel build, and runtime installation each use `RUN --network=none` and `--no-index`. The project is built with `--no-build-isolation --no-deps`, copied and installed by the exact path `/tmp/video_download_control-0.10.0-py3-none-any.whl`, and the runtime stage finishes with `python -m pip check`.
- The lock files deliberately carry the approved hashes exported for both wheels and source distributions where upstream publishes both. That does **not** authorize an sdist: command-level `--only-binary=:all:` rejects source distributions during download and installation. Hash presence and wheel availability still require release review for the target platform.

Refresh the locks only in a reviewed dependency-change branch, inspect the diff and provenance, then run the static audit:

```bash
uv lock
uv export --frozen --no-dev --no-emit-project --no-annotate \
  --format requirements-txt \
  --output-file deployment/requirements.runtime.lock
uv pip compile deployment/requirements.build.in \
  --generate-hashes --only-binary :all: --universal --no-annotate \
  --output-file deployment/requirements.build.lock
uv lock --check
uv run --frozen pytest -q \
  tests/test_deployment_candidate.py tests/test_linux_acceptance_assets.py
```

Do not accept a mechanical refresh merely because it compiles: review every version, artifact hash, publisher/source and license, verify that a compatible wheel exists for every target platform, and keep `pyproject.toml`, `requirements.build.in`, both generated locks and `uv.lock` consistent.

The build also requires `VDC_TOOL_IMAGE` to be a reviewed image reference ending in `@sha256:<64 lowercase hex>`. That image must provide a self-contained directory:

```text
/opt/vdc-tools/
  yt-dlp
  ffmpeg
  ffprobe
  bundle-manifest.json
  sources/...
  licenses/...
  reviews/...
  sbom/...
```

`bundle-manifest.json` must use schema version 1 and describe exactly those three tools. Every entry carries a `source_artifact_path` below `sources/`, the exact `source_artifact_sha256`, an HTTPS source, the exact executable SHA-256, a constrained artifact kind/license expression, one or more hash-bound license texts, a hash-bound non-empty JSON SBOM, and a hash-bound human license-review record. Each source artifact must actually be present as a non-empty, ordinary single-link file and match its declared SHA-256. FFmpeg and ffprobe also carry the complete configuration string; `--enable-nonfree` is rejected, and `--enable-gpl` must agree with a GPL conclusion. The image build runs [`validate_tool_bundle.py`](validate_tool_bundle.py) without network, executes every tool's version command, and compares the reported version plus FFmpeg/ffprobe configuration with the manifest. Missing, malformed, traversing, linked, directory-backed or hash-mismatched evidence fails the build.

This validates evidence integrity and a narrow fail-closed policy; it cannot prove that a human license conclusion is legally correct. Release review must still match the SBOM and texts to the exact yt-dlp packaging form, FFmpeg linked-library closure and source-delivery obligations. The runtime candidate Worker separately requires the exact configured yt-dlp, ffmpeg and ffprobe version tokens. Missing shared libraries or a false bundle fail startup.

Example build shape, after approving the hard-coded Python digest and replacing the fail-closed tool-image reference:

```bash
docker build \
  --file deployment/Dockerfile.candidate \
  --build-arg VDC_TOOL_IMAGE=registry.example/vdc-tools@sha256:REPLACE_64_HEX \
  --tag registry.example/vdc-candidate:0.10.0 \
  .
```

Push the result to a private registry and use its resulting `name@sha256:...` reference in `VDC_CANDIDATE_IMAGE`. The Compose file uses `pull_policy: never`; preload that exact image on the single host before startup. The acceptance runner uses its generated tag only to name a fresh build, immediately captures and validates the built image's immutable local `sha256:...` ID, freezes that ID into effective Compose, uses the same ID for every direct `docker run`, and requires each runtime container's inspected `Image` field to equal it. Its network-none runtime-contract run also checks the exact versions of all 13 distributions in `requirements.runtime.lock` plus `video-download-control==0.10.0`, and rejects any installed `hatchling`, `packaging`, `pathspec`, `pluggy`, or `trove-classifiers` as build-only leakage. A later tag rebind therefore cannot redirect the accepted run. These checks are implemented but have not been executed on target Linux.

## Host preparation contract

Prepare explicit local paths on the target Linux host; the acceptance runner requires Python 3.12 or newer in addition to `realpath`, GNU-compatible `stat`/`find`, `getfacl`, Bash, Docker and Docker Compose v2. Before Cookie rotation, separately verify and record that the exact `/usr/bin/mv` is GNU coreutils with `-fT` support and that the exact `/usr/bin/python3` is Python 3.12+; those are the absolute executables used by the mutation snippet. Compose has `create_host_path: false` and will not silently create a typo as a root-owned directory.

```bash
sudo install -d -o root -g root -m 0755 /srv/vdc
sudo install -d -o 10001 -g 10001 -m 0750 /srv/vdc/data
sudo install -d -o 10001 -g 10001 -m 0770 /srv/vdc/run/egress
sudo install -d -o root -g 10001 -m 0750 /srv/vdc/config
sudo install -o root -g 10001 -m 0440 reviewed-egress-hosts.txt \
  /srv/vdc/config/egress-hosts.txt
```

The egress policy is UTF-8, non-empty and at most 16 KiB, one hostname or suffix per line, at most 128 unique entries. Blank lines and lines beginning with `#` are ignored. It must be a canonical, single-link regular file owned by `root:10001`, mode `0440`, with only base ACL entries. Its complete parent chain through `/` must be canonical, root-owned, free of symlinks and group/world write bits, and restricted to base ACL entries. Keep it bidirectionally non-overlapping with the repository, data/socket/recovery roots, private env and private Compose override. Full acceptance records its device/inode, size and high-resolution modification/change timestamps and revalidates that snapshot before each start/restart boundary. The policy must come from observed and reviewed Stage-0 redirects/media requests; do not guess a broad CDN suffix.

Before acceptance, `/proc/sys/kernel/core_pattern` must be readable and must not begin with `|`. A pipe pattern can invoke a host crash collector even when the Worker has `RLIMIT_CORE=(0, 0)`; the container limit alone therefore does not establish that Cookie-bearing process memory cannot reach a collector. Disable that collector or change the target host to an approved non-pipe pattern before startup. The runner checks this host setting during preflight and again at mutation checkpoints before relevant service starts/restarts; unreadable or pipe-configured state fails closed.

Copy [`.env.candidate.example`](.env.candidate.example) to a private path and replace every active placeholder. In credential-free mode, leave the two commented Cookie variables unset, do not create a Cookie root or mapping, and use only the base Compose file. Set `VDC_ENABLE_CANDIDATE_REAL_WORKER=1` only for an authorized isolated trial. That candidate-worker gate is unrelated to `VDC_ENABLE_X_GRAPH_V2`: enabling the former does not prove or enable exact attachment selection. The unauthenticated host-loopback control plane must keep the graph flag at `0`.

For the explicit Cookie override, create the deployment-owned root plus only the platform directories that have non-empty refs. Copy the complete six-key mapping template to the private mapping path, leave unused refs blank, and uncomment both Cookie variables in the private candidate env. The root and mapping are override scaffolding, not credentials; the files remain independently optional. A configuration with no credentials uses the base Compose and therefore requires none of this scaffolding.

```bash
sudo install -d -o root -g 10001 -m 0750 /srv/vdc/cookies
# Repeat only for each configured platform literal.
sudo install -d -o root -g 10001 -m 0750 /srv/vdc/cookies/youtube
sudo install -o root -g 10001 -m 0440 \
  deployment/cookies/cookie-sources.env.example \
  /srv/vdc/config/cookie-sources.env
```

The private candidate env and any private Compose override supplied to the acceptance runner must each be a canonical, single-link regular file owned by `root:root`, mode `0600`, with only base ACL entries. Their complete parent chains through `/` must be canonical, root-owned, free of symlinks and group/world write bits, and restricted to base ACL entries. They must remain outside the repository and must not overlap the data/socket roots. After strict validation, the runner records device/inode, size and high-resolution modification/change timestamps and repeats both metadata validation and snapshot comparison before full configuration use and every start/restart boundary. The independently worker-readable Cookie mapping remains `root:10001`, mode `0440`, single-link and base-ACL-only; do not weaken it to satisfy the private-input rule.

## Cookie source and opaque-ref contract

The source files are deployment inputs, not application configuration. Create only the files needed from an approved secret manager or browser export without putting content on a command line, in shell history, or in a build layer. The common deployment parent is `root:root` mode `0755`; `VDC_COOKIE_SOURCE_ROOT` and each present immediate platform directory are `root:10001` mode `0750`, so UID/GID `10001` can traverse but cannot replace entries. Each configured final source must be a canonical, single-link, non-empty regular file owned by `root:10001`, mode `0440`, and no larger than 16 MiB. Never use a symlink, hard link, shared file, file below `/srv/vdc/data`, or file inside this repository. The root directory is mounted only into `worker` and contains only present members of the six fixed platform directories; a directory for a blank ref may be absent.

Non-empty opaque refs must be unique 1–64 character identifiers matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. They must exactly match the platform-specific `CredentialProfile.secret_ref` registered through the admin CLI, but must not encode a username, account, host path, container path or secret-store locator. A blank ref is the explicit no-Cookie state and requires that platform's `cookies.txt` to be absent.

| Platform | Mapping key | Fixed host-relative file | Fixed worker target |
|---|---|---|---|
| X | `VDC_COOKIE_X_OPAQUE_REF` | `x/cookies.txt` | `/run/vdc-cookie-sources/x/cookies.txt` |
| YouTube | `VDC_COOKIE_YOUTUBE_OPAQUE_REF` | `youtube/cookies.txt` | `/run/vdc-cookie-sources/youtube/cookies.txt` |
| Bilibili | `VDC_COOKIE_BILIBILI_OPAQUE_REF` | `bilibili/cookies.txt` | `/run/vdc-cookie-sources/bilibili/cookies.txt` |
| Douyin | `VDC_COOKIE_DOUYIN_OPAQUE_REF` | `douyin/cookies.txt` | `/run/vdc-cookie-sources/douyin/cookies.txt` |
| TikTok | `VDC_COOKIE_TIKTOK_OPAQUE_REF` | `tiktok/cookies.txt` | `/run/vdc-cookie-sources/tiktok/cookies.txt` |
| Instagram | `VDC_COOKIE_INSTAGRAM_OPAQUE_REF` | `instagram/cookies.txt` | `/run/vdc-cookie-sources/instagram/cookies.txt` |

The explicit Cookie Compose override binds the source root and mapping file with `read_only: true` and `create_host_path: false`; only the worker receives them. A read-only Compose config invokes [`run-candidate-worker.sh`](cookies/run-candidate-worker.sh), which parses the mapping as data rather than sourcing it and appends fixed-path CLI pairs only for configured platforms. The base Compose neither interpolates nor mounts Cookie paths. The `0440` source mode removes every execute and write bit. Docker Compose does not provide a portable directory-bind `noexec` switch: placing `/srv/vdc/cookies` on a host filesystem mounted `nodev,nosuid,noexec` where supported is an operator prerequisite. The current runner does not inspect or prove those host mount flags, and the YAML cannot prove them. Attempt tmpfs is already `noexec`, attempt-private Cookie copies are mode `0600`, and the Worker declares both soft and hard core-dump limits as zero. That limit must be combined with the non-pipe host `core_pattern` requirement above.

Before rendering the Cookie override or starting its Worker, pass the five required deployment values as data through an empty environment and run the metadata-only preflight. Do not `source`/dot-execute `candidate.env`: it is dotenv input for Compose, not trusted shell code. Use an absolute `/usr/bin/env`, absolute `/bin/sh` and a reviewed absolute repository path in the real launcher; the relative script path below assumes an already verified checkout working directory.

```bash
/usr/bin/env -i \
  VDC_COOKIE_SOURCE_ROOT=/srv/vdc/cookies \
  VDC_COOKIE_MAPPING_FILE=/srv/vdc/config/cookie-sources.env \
  VDC_DATA_ROOT=/srv/vdc/data \
  VDC_SOCKET_ROOT=/srv/vdc/run/egress \
  VDC_COOKIE_STAGING_PLATFORM= \
  /bin/sh deployment/cookies/validate-cookie-sources.sh
```

The script uses `find`, `getfacl`, `realpath` and `stat`; it opens only the small mapping file and never opens, hashes or prints a Cookie file. It validates the mapping before and after parsing, verifies canonical paths, the complete root-owned/non-writable ancestor chains, the absence of named/default extended ACLs, permitted directory entries, owner/group/mode, one hard link, bounded non-zero size, unique file identity, and unique valid opaque refs. It also requires the canonical data and socket roots and rejects either Cookie deployment path when it overlaps either runtime root or the repository in either direction. Finally it repeats each configured source metadata snapshot before success. Blank refs with an absent platform directory or absent fixed file pass. The Worker's independent descriptor-based startup check then rejects writable files, links, path swaps or source mutation without echoing a path, ref or content. Neither check proves that a Cookie is current or accepted by a platform.

Only `worker` has the root, mapping and runner mounts/config. Sharing UID/GID `10001` with the other containers does not grant them the host files because those services have no corresponding bind. Do not mount the Cookie root into another service. This is service-level, not per-platform, isolation: the one Worker can read the entire mounted source root, so a compromised downloader handling one platform could read the configured sources for the other platforms. Treat credential sidecar delivery, one Worker per platform, or a per-attempt mount namespace as required follow-up before production credentials.

### Rotation procedure

Rotation is deliberately a stop-and-recreate operation. Do not overwrite a live file in place and do not rely on a stopped container retaining the new inode. Run the whole sequence under the deployment system's exclusive privileged-writer lock: no other root process may alter the source root, mapping or staging file between the first preflight and post-start verification. The complete mutation snippet—not merely its two validator subprocesses—must be stored as a reviewed, root-owned script and invoked by a clean trusted root launcher with an empty/scrubbed environment and an absolute script path, for example the policy-equivalent shape `/usr/bin/env -i /bin/bash -p /absolute/reviewed/rotate-cookie.sh`. Do not paste it into an inherited interactive shell: an exported `mv` function, `PATH` shim or other inherited shell state is outside the trust boundary. The validator rejects ordinary symlink and metadata races, but cannot defend against an uncooperative privileged writer after it exits.

1. Stop `worker` and confirm it is stopped; leave `sandbox`, `relay` and `egress-proxy running if their health remains valid.
2. Choose exactly one literal platform from `x`, `youtube`, `bilibili`, `douyin`, `tiktok`, or `instagram`. In its already-validated fixed directory, have the secret manager write the exact sibling `cookies.txt.next`; fsync it, set `root:10001` and `0440`, and do not enable shell tracing.
3. Through the clean `/usr/bin/env -i` shape shown below, set only the five validator inputs and put the reviewed literal in `VDC_COOKIE_STAGING_PLATFORM`. This mode derives `cookies.txt.next` below the canonical source root, permits no other staging name, and validates that file. If it fails, keep the Worker stopped.
4. Derive `PLATFORM_DIR="$COOKIE_ROOT/<literal-platform>"`, `NEXT="$COOKIE_ROOT/<literal-platform>/cookies.txt.next"` and `LIVE="$COOKIE_ROOT/<literal-platform>/cookies.txt"` only after the platform whitelist and staging preflight pass. As root, run the previously verified absolute GNU `/usr/bin/mv -fT -- "$NEXT" "$LIVE"` with stderr replaced by a generic failure; both names are in the same validated directory, so the successful rename is atomic.
5. Before the normal validator or Worker recreate, pass only the already-whitelisted `PLATFORM_DIR` to the trusted absolute Python 3.12 helper and `fsync` that directory descriptor. Rename atomicity is not crash durability: without the directory `fsync`, power loss can roll back the directory entry. If open or `fsync` fails, keep the Worker stopped and recover through the protected secret source.
6. Immediately run the normal metadata preflight, which rejects a remaining `.next` or any unexpected entry. Start with both `--file deployment/compose.candidate.yaml` and `--file deployment/compose.candidate.cookies.yaml`, then `up --detach --no-deps --force-recreate worker`, so Docker establishes the new Cookie binds. Wait for the Worker health check. Candidate startup success proves only that metadata, isolation and tool preflights passed.
7. Verify a specifically authorized credential case through status/error codes only. Never print Cookie data, run downloader verbose/debug modes, run non-quiet Compose rendering in an ordinary terminal/log, or include host/container paths in an application log or validation report.

If replacement or verification fails, stop the Worker, restore from the protected deployment secret source, repeat the atomic replacement and both preflights, and only then recreate it. Starting first and replacing later is outside this contract.

The reviewed promotion script must whitelist the platform before deriving any path, call the verified absolute executables, replace raw rename/fsync diagnostics with generic failures, and exit while the Worker remains stopped on either failure:

```bash
COOKIE_ROOT=/srv/vdc/cookies
COOKIE_MAPPING=/srv/vdc/config/cookie-sources.env
DATA_ROOT=/srv/vdc/data
SOCKET_ROOT=/srv/vdc/run/egress
VALIDATOR=/absolute/reviewed/checkout/deployment/cookies/validate-cookie-sources.sh
COOKIE_PLATFORM=youtube  # one reviewed literal: x, youtube, bilibili, douyin, tiktok, or instagram
case "$COOKIE_PLATFORM" in
  x|youtube|bilibili|douyin|tiktok|instagram) ;;
  *) printf '%s\n' 'Cookie source promotion rejected.' >&2; exit 1 ;;
esac
PLATFORM_DIR="$COOKIE_ROOT/$COOKIE_PLATFORM"
if ! /usr/bin/env -i \
  VDC_COOKIE_SOURCE_ROOT="$COOKIE_ROOT" \
  VDC_COOKIE_MAPPING_FILE="$COOKIE_MAPPING" \
  VDC_DATA_ROOT="$DATA_ROOT" \
  VDC_SOCKET_ROOT="$SOCKET_ROOT" \
  VDC_COOKIE_STAGING_PLATFORM="$COOKIE_PLATFORM" \
  /bin/sh "$VALIDATOR"; then
  printf '%s\n' 'Cookie staging validation failed.' >&2
  exit 1
fi
NEXT="$COOKIE_ROOT/$COOKIE_PLATFORM/cookies.txt.next"
LIVE="$COOKIE_ROOT/$COOKIE_PLATFORM/cookies.txt"
if ! /usr/bin/mv -fT -- "$NEXT" "$LIVE" 2>/dev/null; then
  printf '%s\n' 'Cookie source promotion failed.' >&2
  exit 1
fi
if ! /usr/bin/env -i /usr/bin/python3 -I -c '
import os
import sys

flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
descriptor = os.open(sys.argv[1], flags)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
' "$PLATFORM_DIR" 2>/dev/null; then
  printf '%s\n' 'Cookie source directory sync failed.' >&2
  exit 1
fi
/usr/bin/env -i \
  VDC_COOKIE_SOURCE_ROOT="$COOKIE_ROOT" \
  VDC_COOKIE_MAPPING_FILE="$COOKIE_MAPPING" \
  VDC_DATA_ROOT="$DATA_ROOT" \
  VDC_SOCKET_ROOT="$SOCKET_ROOT" \
  VDC_COOKIE_STAGING_PLATFORM= \
  /bin/sh "$VALIDATOR"
```

### Data, API, backup and logging boundary

The DB stores only the opaque ref associated with a `CredentialProfile`; it never stores either path or Cookie content. The host root/mapping paths exist only in the private Compose env and daemon mount metadata. Fixed container paths exist only in deployment assets and the Worker's private `CookieSource`; its `repr` and errors omit both path and ref. The runner emits generic platform-labelled failures and never prints mapping values. Public API DTOs, metrics, media metadata and diagnostics must not add any Cookie field.

Keep `/srv/vdc/cookies` and the mapping outside `VDC_DATA_ROOT`, `VDC_SOCKET_ROOT`, and the repository; the mandatory metadata preflight enforces these bidirectional non-overlap checks. The checked-in `.dockerignore` also excludes conventional private candidate-env, mapping, and Cookie filenames from the build context, but this is defense in depth rather than permission to stage secrets in the checkout. Business-data backup may include the DB's opaque ref, but Cookie files, mapping, host/container paths and attempt-private copies must not enter a backup control manifest or asset manifest. Recover them through a separate protected deployment-secret procedure. Do not use `set -x`, log private deployment files, persist rendered Compose output, or pass a Cookie path/content in an API or admin-CLI value. The mapping is mounted as a file rather than interpolated by Compose, so `docker compose config` cannot render an opaque ref. Successful `config --quiet` emits no rendered model; its stderr and all `up` diagnostics are privileged deployment output and must not be piped or shipped to an ordinary application log. Privileged process inspection can see the non-secret opaque ref because the existing CLI contract carries it in argv; ordinary application logs must not.

## X graph-v2 remains deployment-disabled

Schema 8 persists parent `discover` Jobs, immutable discovery snapshots, per-attachment child Jobs, active-snapshot aggregation, input cancel and terminal-only rediscover generations. Those orchestration semantics were validated only with `ScriptedGraphFakeAdapter`, an offline adapter whose exact-selector contract is manufactured for tests.

The candidate real Worker constructs `YtDlpAdapter`, which intentionally declares `supports_exact_selector=False` until authorized Stage 0 samples prove both stable attachment keys and single-sibling selection. Do not set `VDC_ENABLE_X_GRAPH_V2=1` on a control plane that shares this queue. The Worker's fail-closed claim check is a containment measure, not a supported deployment mode, and a failed graph claim is not platform validation. Real X graph remains `candidate/disabled` and must not be labeled `verified`.

## Static and future runtime checks

With Docker Compose available, credential-free mode uses only the base file. Validate it without rendering the model to stdout:

```bash
docker compose \
  --env-file /srv/vdc/config/candidate.env \
  --file deployment/compose.candidate.yaml \
  --profile candidate-real-worker \
  config --quiet
```

Cookie mode first requires the metadata preflight, then layers the explicit override. Run this only in a protected operator terminal: never capture Compose diagnostics in an ordinary log.

```bash
/usr/bin/env -i \
  VDC_COOKIE_SOURCE_ROOT=/srv/vdc/cookies \
  VDC_COOKIE_MAPPING_FILE=/srv/vdc/config/cookie-sources.env \
  VDC_DATA_ROOT=/srv/vdc/data \
  VDC_SOCKET_ROOT=/srv/vdc/run/egress \
  VDC_COOKIE_STAGING_PLATFORM= \
  /bin/sh deployment/cookies/validate-cookie-sources.sh
docker compose \
  --env-file /srv/vdc/config/candidate.env \
  --file deployment/compose.candidate.yaml \
  --file deployment/compose.candidate.cookies.yaml \
  --profile candidate-real-worker \
  config --quiet
```

Only after reviewing the private source files directly—not stdout from rendered Compose—confirm the image digest, worker-only read-only Cookie mounts, command and policy path:

```bash
docker compose \
  --env-file /srv/vdc/config/candidate.env \
  --file deployment/compose.candidate.yaml \
  --file deployment/compose.candidate.cookies.yaml \
  --profile candidate-real-worker \
  up --detach
```

### Full synthetic acceptance runner

[`run-linux-acceptance.sh`](run-linux-acceptance.sh) defaults to `--preflight`, which does not build, start/stop containers, create sockets, restore data or clean artifacts. Mutation mode additionally requires the exact `--execute --authorize I_ACCEPT_TARGET_LINUX_MUTATIONS` pair, an explicit protected env file, immutable tool image, Schema 8 backup root and empty restore parent.

`--execute` additionally requires effective UID 0. Host tooling is resolved through fixed `/usr/bin/python3`, not `PATH`, and that exact interpreter must report Python 3.12 or newer. Read-only endpoint detection follows Docker's official precedence: a non-empty `DOCKER_CONTEXT` selects that context ahead of `DOCKER_HOST`; otherwise the shown/default context endpoint is used. In every case it must resolve to a local `unix:///...` endpoint backed by a Linux daemon. Full execution rejects inherited `DOCKER_CONTEXT`, `DOCKER_HOST`, `DOCKER_CONFIG`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`, `BUILDKIT_HOST`, `BUILDX_BUILDER`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME` and `COMPOSE_PROFILES`, so its default context must independently satisfy that same local-Unix/Linux check.

Full execution is intentionally narrower than ordinary credential-free configuration. It accepts only the reviewed Cookie wrapper shape: the checked-in `run-candidate-worker.sh` entrypoint/config plus the exact worker-only, read-only source-root and mapping binds. A direct per-file `--cookie-source` command override, a generic Compose override, altered service command/healthcheck/security/resource/network fields, or a non-worker service change is rejected. The acceptance mapping must contain six distinct synthetic `acceptance-*` refs and their synthetic files; this exercises the maximum supported configuration and is not permission to use real Cookies.

The private env and optional Compose override, egress policy and Cookie inputs are validated as described above, snapshotted and revalidated immediately before each relevant service start/restart. The same checkpoints re-read the host `core_pattern` and reject an unreadable value or a value beginning with `|`. Full execution compares the effective Compose JSON with the trusted base model and permits only the reviewed Worker wrapper delta. After the fresh local build, it resolves the generated tag to an immutable `sha256:...` image ID, validates that ID, rejects a dollar sign in every string key/value anywhere in the effective model, and creates `.vdc-effective-<run-id>.json` beside the private env as a `root:root`, mode `0600`, single-link, base-ACL-only frozen input with that ID embedded. Compose renders that frozen file a second time; the result must be deeply equal to the original effective JSON, pass the complete safety validator again, and leave the frozen file's identity/metadata snapshot unchanged. All subsequent Compose mutations use only this frozen file; all direct runs and runtime image-identity checks use the same immutable ID, which is re-inspected at checkpoints.

On ordinary exit, cleanup removes the frozen file only after its exact identity and metadata still match the recorded snapshot. A crash or forced termination can leave it in the private env directory; the file may contain sensitive deployment paths/configuration even though dollar-bearing interpolation strings were rejected. Audit a remnant under the same protected permissions using its exact path and identity, and delete only after confirming it is the runner-created unchanged file; never remove an unfamiliar or identity-changed path automatically.

A `unix:///...` Docker endpoint and Linux daemon are necessary checks, but a Unix Docker socket does not prove that the daemon shares the runner's host or mount namespace. The operator must independently attest that the runner, Docker daemon and every validated bind source refer to the same target host and mount namespace.

The runner must itself be invoked by a clean, trusted root launcher with an empty/scrubbed environment and an absolute trusted script path. Its `#!/bin/bash -p`, internal unsets and fixed `PATH` begin only after the interpreter/process image has started: inherited `BASH_FUNC_*`, `LD_PRELOAD` or equivalent loader/interpreter state may act earlier or reach a child shell if the outer launcher is untrusted. The runner also assumes a reviewed checkout/build context and exclusive trusted operator control for the duration of the run. It validates canonical string paths and ordinary symlink/metadata races, but cannot discover alternate physical aliases, pre-existing bind mounts or another privileged writer that makes two distinct canonical paths name the same storage. Do not claim acceptance unless those assumptions were independently enforced; the target daemon/BuildKit versions and bundled frontend behavior were recorded; `RUN --network=none` was observed to work; both digest-pinned registries and target-platform artifacts were available; and every base image, tool bundle, Python version, wheel hash and provenance record was approved. None of those target-build facts was established on this Windows host.

The independent backup/restore path has two Windows records: the historical Iteration 0.5 / Schema 7 exercise, and an Iteration 0.6 / Schema 8 offline graph exercise with cross-table semantic tamper rejection ([evidence](../validation/iteration-0.6-graph-v2-offline-evidence.md)). Both use small temporary local roots and neither is a target Linux/NAS recovery acceptance. Runtime acceptance still requires Linux/container evidence that only `lo` exists in the Worker namespace; private/loopback/link-local/metadata addresses fail through the proxy; stale UDS recovery is controlled; SIGTERM/lease recovery is safe; configured resource limits apply; Cookie mounts are worker-only/read-only with the documented host ownership and mount flags; stop/atomic-replace/recreate rotation works; and the digest-pinned image starts with the reviewed real yt-dlp, ffmpeg and ffprobe binaries. Formal Stage 0 validation of this Linux/Docker candidate has not run.

Credential profiles reach Adapter requests through an opaque reference, the attempt-private Cookie copier is tested, and the explicit Compose override declares a deployment-owned source root plus mapping. The base Compose is the credential-free configuration and has no Cookie assets. Within the override, all six blank refs are also fail-closed and each non-empty ref independently requires only its own fixed file and platform directory. The mounts, runner and host metadata preflight are only statically tested; no real source was created or mounted and no credential-dependent attempt ran.

The Windows backup/restore exercise does not validate Linux permissions, container mounts, namespace isolation, UDS behavior, resource enforcement or real-tool execution. The short-link replay store likewise requires a local, reliable filesystem: replay-marker work is offloaded from the event loop, but blocking OS/filesystem I/O in that worker thread cannot be force-cancelled. A wedged filesystem can therefore retain a bounded worker slot until the call returns or the process is restarted. The built-in resolver receives and enforces the Batch's remaining aggregate time budget; a custom injected resolver is a trusted-contract boundary and must honor the supplied `timeout_seconds`. This candidate must not be described as production-ready.

The host-loopback control plane may point `VDC_DATA_ROOT` and `VDC_DATABASE_PATH` at the same local data directory while the Worker runs. Do not put SQLite on an unreliable network share.
