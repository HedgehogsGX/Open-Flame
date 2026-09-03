# Iteration 0.7 short-link, Cookie and Linux acceptance engineering evidence

> Recorded: 2026-09-03  
> Application: `video-download-control 0.7.0`  
> Database: Schema `8`  
> Evidence class: Windows offline implementation, adversarial tests and static deployment checks only

## 1. Authority and evidence boundary

The implementation continues from the private local architecture decision
document `视频下载项目选型与整体架构规划.md`, which is not included in this
repository.

The source document SHA-256 was verified locally and is omitted here to avoid
publishing a fingerprint of the private document.

This file is **not Stage 0 platform evidence**. The Windows host had no
`docker`, `yt-dlp`, `ffmpeg` or `ffprobe` on `PATH`. No real URL, Cookie,
platform request, media download, tool image, container, target Linux host or
mutation acceptance run was used. Every platform remains `candidate`; X
graph-v2 and short-link resolution remain disabled by default.

## 2. Closed implementation scope

### 2.1 Default-disabled short-link path

- `t.co`, `b23.tv` and `v.douyin.com` can be routed only when the explicit
  control-plane gate, private POSIX Unix socket and private HMAC key all pass
  startup validation. Default behavior remains
  `short_link_resolution_required`.
- Control and egress exchange bounded canonical JSON frames authenticated with
  domain-separated HMAC-SHA256. Requests bind a fresh 256-bit nonce, issued/
  expiry timestamps, method, target host, numeric address set and limits;
  responses bind the nonce and request hash.
- The egress side performs one numeric-IP TLS connection with the original
  hostname retained for SNI, certificate validation and HTTP `Host`, then
  attests the actual peer. It does not perform DNS or automatically follow a
  redirect. Only a bounded `Location` header crosses back.
- Replay markers use exclusive, fsynced files with bounded garbage collection.
  Filesystem work is offloaded from the event loop to a dedicated bounded
  executor, checked against request freshness before fetch, and fails closed
  on timeout or slot exhaustion.
- The built-in resolver receives the Batch's remaining 15-second aggregate
  budget. Returned objects are type-checked and their canonical URL, platform,
  source type and source ID are independently normalized before persistence.
  Resolver exceptions and malicious/inconsistent results become one fixed
  failure. A final signed Location is absent from API responses, SQLite,
  backup data and ordinary audit records.

### 2.2 Deployment-owned optional Cookie path

- The credential-free base Compose has no Cookie input. An explicit override
  mounts one deployment-owned source root, fixed four-key opaque mapping and
  reviewed wrapper into `worker` only; zero to four platform sources are a
  valid product configuration.
- The host validator treats the mapping as data and checks canonical paths,
  owner/group/mode, single links, base ACLs, complete safe ancestor chains,
  bounded size, unique source identity/ref, fixed entries, protected-root
  non-overlap and before/after metadata snapshots. It never opens, hashes or
  prints a Cookie file.
- The Worker revalidates each source through no-follow descriptors and copies
  it to a fresh Attempt-private `0600` file. Source/target/directory identities,
  size, timestamps, fsync and cleanup are checked. Cleanup uses the already
  attested directory descriptor, so a pathname replacement cannot redirect
  deletion to an attacker-supplied file. Public errors drop the full exception
  chain and contain no ref, path or Cookie value.
- Rotation is documented as stop, clean-environment staging validation,
  absolute GNU `mv -fT`, parent-directory fsync, normal validation and forced
  Worker recreation under one exclusive trusted-writer lock.

### 2.3 Target Linux/Docker acceptance assets

- [`run-linux-acceptance.sh`](../deployment/run-linux-acceptance.sh) defaults
  to non-mutating preflight. Full mode requires the exact authorization token,
  effective UID 0, a private env file, reviewed wrapper override, digest-pinned
  tool image, deny-only egress policy, four synthetic Cookie sources, a Schema
  8 backup and dedicated empty roots.
- The runner requires Bash privileged mode, clears interpreter variables,
  fixes host Python to `/usr/bin/python3`, verifies that exact interpreter is
  Python 3.12+, invokes the Cookie validator with `/usr/bin/env -i`, and compares
  the complete effective Compose model against the reviewed base. It rejects
  service, command, namespace, mount, healthcheck, network, config, secret and
  lifecycle-hook drift outside the exact Cookie wrapper delta.
- Endpoint inspection gives non-empty `DOCKER_CONTEXT` precedence over
  `DOCKER_HOST`; otherwise it resolves the current/default context. Every path
  must lead to a local Unix Linux daemon. Execute rejects inherited Docker,
  Compose and BuildKit endpoint/config/builder/project/profile controls so the
  default context cannot be silently redirected.
- The Dockerfile has no external syntax image. Its two Python stages hard-code
  the same `python:3.12.13-slim-bookworm` digest with no Python-image ARG, while
  the tool bundle must be supplied by reviewed digest. The target registries,
  selected-platform manifests and their provenance remain release evidence.
- `pyproject.toml` and `requirements.build.in` both pin
  `hatchling==1.27.0`; `requirements.build.lock` and
  `requirements.runtime.lock` hold the exact-version/hash-locked build and
  runtime closures.
  The sole ordinary Python package network step is a hash-checked,
  `--no-deps --only-binary=:all:` download. Build-dependency install, project
  wheel build and runtime install use `RUN --network=none` plus `--no-index`;
  the project wheel uses `--no-build-isolation --no-deps`, is copied/installed
  by exact versioned path, and is followed by `pip check`.
- Lock hash sets may include both upstream wheels and sdists. The sdist hashes
  are audit data, not permission to build from source: command-level
  `--only-binary=:all:` rejects sdists. Approved hashes/provenance and target
  wheel availability still require review.
- The network-none `image_runtime_contract` direct run is defined to verify the
  exact versions of all 13 runtime-lock distributions plus
  `video-download-control==0.7.0`, and to reject `hatchling`, `packaging`,
  `pathspec`, `pluggy`, or `trove-classifiers` if any build-only dependency
  leaked into runtime. This is runner behavior awaiting target Linux execution,
  not a Docker result recorded on Windows.
- Private env/override inputs require `root:root 0600`, one link, base ACLs and
  safe root-owned ancestors. The egress policy requires `root:10001 0440`, one
  link, 1..16384 bytes, base ACLs and safe ancestors. Metadata snapshots and
  the Cookie validator are rerun at mutation checkpoints.
- Full mode also requires a readable, non-pipe host
  `/proc/sys/kernel/core_pattern`, validates `RLIMIT_CORE=(0,0)`, inspects both
  Cookie binds as the expected host sources with Docker `RW=false`, and covers
  cold start, health, namespace/UDS, SSRF, peer attestation, SIGTERM/lease
  recovery, stale-socket behavior and independent Schema 8 restore.
- After the fresh build, full mode captures and validates its immutable local
  `sha256:...` image ID. Before the first Compose start, it injects that ID,
  rejects `$` recursively from all effective JSON string keys/values, and
  freezes the validated model beside the private env as a `root:root 0600`,
  single-link, base-ACL-only file. A second Compose render must be deeply equal
  to the original JSON, pass the full validator again and preserve the file
  snapshot. All later Compose mutations use that frozen model; every direct
  run and runtime-container inspect is bound to the same ID, and checkpoints
  re-inspect it, so tag rebinding cannot redirect the run. Normal cleanup
  removes the frozen file only after identity/snapshot recheck.
- The script contains no recursive delete and retains the restored acceptance
  root and stale-socket fixture for operator inspection. Crash/forced exit can
  leave the frozen config in the protected env directory; an operator must
  audit its exact identity before narrowly scoped removal.

## 3. Executed verification

Executed on the Windows development host:

| Command/check | Result |
|---|---|
| `.venv\\Scripts\\python.exe -m pytest -q` | `735 passed, 8 skipped in 39.89s` |
| Short-link/Cookie/API/Linux focused regression | `159 passed, 4 skipped in 8.24s` |
| Linux/deployment asset regression after final hardening | `28 passed, 4 skipped in 4.02s` |
| `uv lock --check` | Passed; 24 packages resolved |
| exact offline regeneration of both deployment lock files | Byte-for-byte identical |
| hash-checked wheel download, current host | 18 wheels available |
| hash-checked CPython 3.12 manylinux2014 x86_64 download | 18 wheels available |
| hash-checked CPython 3.12 manylinux2014 aarch64 download | 18 wheels available |
| Python `compileall` over `src` and `tests` | Passed |
| version/schema import assertion | `0.7.0 8` |
| Ruff critical/unused gate `E9,F63,F7,F82,F401,F841` | Passed |
| Ruff check/format gate for 11 changed Python files | Passed |
| eight argparse CLI `--help` entry points | Passed |
| sdist/wheel build | Passed |
| isolated wheel install with `--no-deps` | Package metadata `0.7.0`; nine console entry points |
| Git Bash syntax plus runner help/preflight smoke tests | Included in the passing suite |

The eight skips are explicit environment gaps, not silent passes:

- one POSIX directory-FD Cookie cleanup test;
- four root-POSIX/getfacl Cookie metadata contract cases for 0/1/3/4 sources;
- one real AF_UNIX round trip;
- two POSIX file replacement/permission tests for the short-link transport.

The final artifact hashes are written to `dist/SHA256SUMS.txt` after the final
rebuild. They are not embedded here, avoiding a self-referential sdist hash.

## 4. Evidence hashes

```text
9D619E099FA289C331FB3323FE8102627B1B091097F4F0B15C1642CC9B9889EA  pyproject.toml
00493E23275AF91F3EE6F735559856632465A2FE7F66CBECC92EB8FEAD9D1A7C  uv.lock
E23840910DAB11BFFB2B260DB8C9A2EDBE57168209FDA3512F056F7D17E37695  src/video_download_control/service.py
68B7AFC5CC1BC2120B4B39168BA12000BE5EE0423C427BACEE9E29537675F7F8  src/video_download_control/short_links.py
B76C4A7029AA0C94FAA830CFC0AE4B7963878DD3F11CFEF4A046160EEDFDC8B0  src/video_download_control/short_link_transport.py
024FE3150BB5EF1549DC8C2871148B3A673CCB56CB71B4B17C520BE04B87C383  src/video_download_control/candidate_cookies.py
A40D4A64934377784727D92E598AAB3867A6C02ADFEECE512B1C9C700FC3218D  src/video_download_control/candidate_worker_cli.py
7E5711E7DCF8E74D75C8B99C8F889DA2DE2C29258DA05BCC42A460AAD760D70C  deployment/Dockerfile.candidate
28E067B5263B9006CA205FAD490AFB62A89929D2AF723D82877A70DBFC1DBABB  deployment/requirements.build.in
C165BFDF805F99EC3460CFD2A885724FE579563391FE41DB21960FC45F4F9149  deployment/requirements.build.lock
8AF8A9A25B243A93FE4CDBC01FB2BFF68FC377EC5A06ACD67002DFAC23059A4D  deployment/requirements.runtime.lock
ECCBBECC148FBF8B86A829137811714998A6C75099E61AB17F147105A4F83DB5  deployment/run-linux-acceptance.sh
1C1769EB02981CE6FBBEDF096F7AED4722C6732BEC9264EE9CC06D694998FB7E  deployment/compose.candidate.cookies.yaml
45F86A3CBA7BDEB8AB45D27EA9424F692A5F9BB9A04C2A78360D746201A85D48  deployment/cookies/validate-cookie-sources.sh
1FF2C1C54EB76A25383576A14AF943527AD49ABBFD7021AB2B90D8015F95A1FF  deployment/cookies/run-candidate-worker.sh
```

## 5. Residual risks and next gates

1. Run the checked-in acceptance runner on a clean, same-host target Linux
   namespace with Docker Compose v2, effective root, fixed `/usr/bin/python3`
   3.12+, GNU tools, `getfacl`, a
   non-pipe core pattern and synthetic-only inputs. Static Windows evidence is
   not a substitute.
2. The current Cookie override is service-level isolation: one compromised
   Worker/downloader can read every configured platform source in the mounted
   root. Production credentials require a credential sidecar, per-platform
   Worker or per-Attempt mount namespace.
3. The runner relies on a clean trusted root launcher, a reviewed and exclusive
   checkout/build context, a local Docker/BuildKit endpoint, exclusive operator
   control and absence of physical-path aliases/pre-existing bind mounts. Its
   local Unix Docker socket alone does not prove same-host/mount-namespace
   provenance. With no external syntax directive, the target daemon's bundled
   frontend must support Dockerfile 1.3+ and BuildKit must actually enforce
   `RUN --network=none`; this target behavior was not executed on Windows. A
   failure occurs during build before any Cookie source is mounted. Registry
   availability, target base/tool manifests and wheels, and approved image/package
   hashes and provenance remain release gates. The Cookie source filesystem's
   `nodev,nosuid,noexec` flags are a separate operator prerequisite and are not
   proved by the runner or Compose YAML.
4. Blocking DNS and replay filesystem calls cannot be force-cancelled once in
   the OS. Slots are bounded and fail closed, but process isolation and a local
   reliable replay filesystem are still required. A custom injected resolver
   must honor its timeout contract.
5. The short-link egress service is not yet in the candidate Compose/supervisor
   topology. Real POSIX ownership, TLS/SNI, DNS rebinding, restart/replay and
   redirect-chain behavior remain target-environment gates.
6. No platform may move from `candidate` to `verified` until explicitly
   authorized Stage 0 samples satisfy the planning document's per-cell and
   three-run evidence thresholds. Do not fetch random or unauthorized media.
7. Exercise frozen-config cleanup on target Linux: verify ordinary exit removes
   only the recorded identity, then separately test the documented crash-remnant
   exact-path/device/inode audit without broad or recursive deletion.

## 6. Repository state

The Git worktree has no commit and all project files are still untracked.
Local `user.name` and `user.email` are unset, so no commit or rollback revision
was fabricated for this iteration.
