# Iteration 0.28.0 managed-file identity boundary

Date: 2026-09-10
Scope: architecture review S6a; local/offline source validation only

## Result

Editing and Upload now use one domain-neutral managed-file identity module for
the shared four-field signature and plain-entry `lstat` rules. The shared
signature remains exactly `(st_dev, st_ino, st_size, st_mtime_ns)`. A plain file
must be regular, non-symlink, non-reparse and single-link; a plain directory must
be a directory, non-symlink and non-reparse, without applying the file link-count
rule.

The shared module leaves `OSError` untouched and reports an unsafe entry through
`UnsafeManagedPath`. Editing's thin wrapper still maps an unavailable `lstat` to
`editing_media_unavailable` and an unsafe entry to `unsafe_editing_file`. Upload's
wrapper still maps only unsafe entries to `unsafe_upload_file`, leaving missing
and other I/O failures for each existing call site to classify.

No hashing, opening, copy, response, transaction, cache, activity lease,
database, Schema, queue, runtime, dependency, route, user interface or platform
adapter behavior changed. No tracked test file was added or modified.

## Preserved safety contracts

- Every existing opened/final handle and final-path comparison still uses the
  same four fields; no `ctime`, mode or link count was added to the signature.
- Editing's retained verified handle and one-mebibyte chunk manifest remain in
  the Editing domain and continue to back full, HEAD and Range responses.
- Editing keeps its 16 GiB source and 8 GiB output limits, exclusive destination
  creation, `fsync` and unregistered-file cleanup.
- Upload keeps its 2 GiB source and 20 MiB cover limits, bounded cover payload,
  staged copy, hardlink publication, activity lease, cache and SQLite rollback.
- Final `lstat` still distinguishes unsafe links/reparse points from a changed
  four-field identity; missing Upload media can still become missing/deleted
  rather than being collapsed into an unsafe or changed result.

## Validation

| Check | Result |
| --- | --- |
| Ignored managed-file identity matrix | **20/20 PASS**: regular file/directory, wrong-kind use, raw and domain missing behavior, symlink, simulated Windows reparse, hardlink, same-size replacement, directory link count, dependency and duplicate-implementation checks |
| Editing service, Upload service, upload activity-lock and platform-parameter modules | **103 passed** |
| Range, same-handle manifest, send-failure close, registered-source replacement, Editing cancellation, Upload source mutation/import, interrupted copy, activity lease and staged cover cases | **14 passed** |
| Source release inventory | PASS; 281 listed files, 128 package files and the new module present |
| `compileall`, `uv lock --check --offline`, `uv pip check` and `git diff --check` | PASS |

These results bind the current source checkout only. They do not prove a frozen
release, real URL download, OpenAI response, human listening quality, platform
login, upload acceptance, scheduled publication or public visibility.

## Next boundary

S6b may extract the still-duplicated open/fstat/final-lstat and bounded SHA-256
read mechanics. Domain wrappers must retain distinct size limits and error codes;
Upload publication/rollback and Editing's retained-handle ownership remain in
their current domains.
