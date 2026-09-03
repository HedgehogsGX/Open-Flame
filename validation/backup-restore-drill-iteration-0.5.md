# Iteration 0.5 backup / restore drill evidence

- Executed: 2026-09-03
- Host: current Windows development host
- Application: `video-download-control 0.5.0`
- Database: Schema `7`
- Result: **PASS (offline functional evidence only)**

## Commands and observed results

```powershell
uv lock --check
# Resolved 24 packages

uv run pytest -q
# 501 passed in 24.26s

uv run pytest -q tests/test_backup_restore.py
# 14 passed in 5.20s

uv run python -m compileall -q src
# exit 0
```

The focused suite invokes both the Python API and the installed-style `create` / `restore` CLI entrypoints. Its primary drill creates a ready asset with the deterministic offline fake Worker, backs up the database plus published asset tree, restores into a different nonexistent root and database path, and then verifies:

- exact Schema 7 application readiness;
- SQLite `quick_check` and foreign keys;
- restored Batch state;
- database Artifact path and SHA-256;
- restored original file existence and SHA-256 equality;
- exclusion of `temporary/` and `assets/.staging/`;
- preservation of an additional non-secret managed file.

The remaining cases exercise fail-closed behavior for payload and manifest corruption, rehashed path traversal, inner asset-manifest inconsistency, Schema tampering, source and backup links/reparse points where supported, existing/overlapping/root targets, a database target outside the restore root, pending asset commit intent, untracked backup files, and manifest coverage of metadata.

## Evidence identity

| File | SHA-256 |
|---|---|
| `src/video_download_control/backup.py` | `19350632ABC562F3A2DFBBD86549AB185E5C2331B0FFCEEDD9BA1F72EDDF9F51` |
| `src/video_download_control/backup_cli.py` | `669BD89841E94F44AE358920F738F2F20FA796DAA1D292427806A8CE0514DE3B` |
| `tests/test_backup_restore.py` | `54A47593FA97AD909D6B4032EA0BDD83CDED07B3352900507A9001EA6400D46A` |

## Boundaries

This is a small, temporary, offline Windows functional drill. It is not evidence for Docker/Linux UID/GID behavior, network filesystems, production media volume, independent physical/offsite media, encryption, retention, RTO/RPO, disaster-host recovery or a downgrade to older code. The manifest hash sidecar lives beside the manifest; it detects accidental corruption but is not an external signature against an attacker able to replace both. Deployment-owned Cookie files and external secret stores are intentionally outside the business-data backup and require a separate protected recovery procedure.
