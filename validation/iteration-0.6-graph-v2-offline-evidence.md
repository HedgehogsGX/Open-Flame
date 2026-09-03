# Iteration 0.6 graph-v2 offline engineering evidence

> Recorded: 2026-09-03  
> Application: `video-download-control 0.6.0`  
> Database: Schema `8`  
> Evidence class: offline implementation, migration, recovery and static checks only

## 1. Authority and boundary

The implementation follows the private local architecture decision document
`视频下载项目选型与整体架构规划.md`, which is not included in this repository.

The source document SHA-256 was verified locally and is omitted here to avoid
publishing a fingerprint of the private document.

This record is **not** Stage 0 platform evidence. No real X URL, Cookie,
`yt-dlp`, FFmpeg/ffprobe, Docker image, Linux namespace or external media was
used. `VDC_ENABLE_X_GRAPH_V2` remains disabled by default. Only the offline
`ScriptedGraphFakeAdapter` declares exact-selector support; the real candidate
`YtDlpAdapter` deliberately declares `supports_exact_selector=False`.

## 2. Closed implementation scope

- Schema 8 adds `x_attachment`, immutable ordered discovery snapshots,
  discovery-scoped relations, relation-to-Job occurrences, exact child targets,
  active discovery/run generation, generation-local attempt counters, reuse
  provenance and Batch `partial_success_count`.
- Schema 7 relations migrate into deterministic legacy discoveries. Existing
  flat-v1 Job, Asset, Artifact and manifest rows remain readable and are not
  rewritten into attachment children.
- A parent `discover` Job probes and commits fan-out in one `BEGIN IMMEDIATE`
  transaction. It does not download an asset and does not count as a platform
  media outcome.
- Each child `download` Job receives the parent fetch URL plus one internal
  selector and one expected media key. Worker output inventory must contain
  exactly the reported original/sidecars; an unreported sibling file fails the
  Attempt and is removed with the Attempt directory.
- Input aggregation uses only child Jobs linked to the active discovery and run
  generation. Mixed ready plus terminal failure/cancel produces
  `partial_success`; Batch counts remain Input-scoped.
- Input-level cancel, terminal-only rediscover, snapshot replay, retained/added/
  removed membership, changed target generations, generation-local retry and
  compatible ready-asset reuse are implemented.
- Public responses, DTO reprs, diagnostics, backup control manifests and metrics
  do not expose stable keys, selectors, expected media keys, internal fragments
  or credential references.
- Backup restore now checks graph membership hashes and cross-table meaning,
  including parent/child identities, relations, active generations, targets,
  Attempt numbering, ready target-to-asset mapping and reuse donor identity.
  Re-signed semantic tampering is rejected with a fixed diagnostic that does
  not echo target material.

## 3. Executed verification

Executed on the Windows development host:

| Command/check | Result |
|---|---|
| `uv run pytest -q` | `615 passed in 35.20s` |
| Graph repository fault/aggregation/replay suite | Included 11 write-stage rollback injections and all graph repository cases |
| `uv run pytest -q tests/test_backup_restore.py tests/test_observability.py` | `19 passed in 8.42s` |
| `uv lock --check` | Passed; 24 packages resolved |
| `uv run python -m compileall -q src tests` | Passed |
| version/schema import assertion | `0.6.0 8` |
| Ruff critical/unused gate `E9,F63,F7,F82,F401,F841` | Passed |
| seven non-server CLI `--help` entry points | Passed |
| FastAPI app creation from the built wheel in an isolated environment | Passed; application version `0.6.0` |
| sdist/wheel build and package-content check | Passed; graph/database/Worker modules present |
| Compose YAML safe load | Passed; four services |
| official Compose JSON Schema validation | Passed |
| Dockerfile parser | Passed; 26 instructions, three stages |

The suite contains 43 `test_*.py` modules, 372 explicit test functions and 615
collected cases after parametrization.

The current host still reports `docker`, `ffmpeg`, `ffprobe` and `yt-dlp` as
missing from `PATH`. Therefore no build/pull/up, real tool preflight, media
probe/download or Stage 0 run occurred.

## 4. Evidence hashes

```text
6D0F53806FF033F00E5E3F2903226BE5EAC84E50A6AA85AD5EB4EF32CA20B38D  pyproject.toml
0FAC419CCB2F3A80A1FA2DAD929B3DE937299DFAD0993B33084F7B277A666C2B  uv.lock
F274E2B5356038500C824419C5CD61EDC12907DC946884F93B1B7C096DC73971  src/video_download_control/database.py
4BFAE3ADE8B85BF78C02826B18E443D235A35CE0AC12C50A31182223325D2A6D  src/video_download_control/graph.py
1A23BF8EB69950175E1E16E719E322E45CE8E6125149B9779CA7029F3D8AC738  src/video_download_control/worker_repository.py
B9B1AEF3FA266A00058164AB6DF257512F933F662401081DC3205D2EFB4DCE48  src/video_download_control/worker.py
80F5E76FEC5532468198040B88AE698971256A4462D6C06FFACA64E5437A1D67  src/video_download_control/backup.py
B76C2DABF6F0EE54B0AE18AA32A0F3A327E69F9D4A4953114B0838DEDAF8473B  src/video_download_control/api.py
```

Release artifact hashes are written after the final rebuild to
`dist/SHA256SUMS.txt`; they are intentionally not embedded here, so the final
sdist can include this evidence without a self-referential hash cycle.

## 5. Remaining gates

1. Obtain explicit authorization and a reviewed private Stage 0 sample manifest.
2. On the exact pinned real toolchain, prove repeated X probes yield stable,
   unique attachment identity keys.
3. Prove one bounded selector downloads exactly one attachment without fetching
   or retaining siblings, then update the real adapter capability deliberately.
4. Build and cold-start the digest-pinned image on Linux, verify namespace/UDS,
   SSRF policy, resource limits, SIGTERM/lease recovery and Schema 8 restore on
   the target storage.
5. Keep real X graph `candidate/disabled` until those gates pass; offline fake
   results must never be relabeled as real platform support.
