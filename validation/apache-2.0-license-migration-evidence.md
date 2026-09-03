# Version 0.9.1 Apache-2.0 license migration evidence

> Date: 2026-09-03  
> Scope: project-authored source, documentation, scripts, package metadata, and public Git source release  
> Historical boundary: version 0.9.0 artifacts retain their recorded proprietary metadata and must not be published as the Apache release

## 1. Rights-holder decision

The applicable rights holders selected the Apache License, Version 2.0 for the
project-authored work and supplied this public attribution:

```text
Copyright 2026 HedgehogsGX & Cyaegha_Xu
```

The root `LICENSE` is the unmodified Apache License 2.0 standard text. `NOTICE`
contains only the project name and rights-holder attribution; separately
licensed material remains documented in `THIRD_PARTY_NOTICES.md` and the
`licenses/` directory.

| File | SHA-256 |
|---|---|
| `LICENSE` | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` |
| `NOTICE` | `49347878128695acc9d559096e2de323870a692df0dde1147b7bf033788e7091` |

## 2. License scope and retained gates

The Apache-2.0 grant covers project-authored source code, documentation, and
scripts. A newly verified project-only pure-Python sdist/wheel may be
distributed when the applicable license and notice files are retained.

This decision does not relicense third-party materials and does not approve a
dependency wheelhouse, frozen executable, offline installer, OCI/container
image, or yt-dlp/FFmpeg tool bundle for redistribution. The machine-readable
tool policy remains exactly
`blocked_pending_third_party_source_and_notice_audit`; its target SBOM,
corresponding-source, linked-library, relinking, notice, and legal-review gates
remain fail closed.

## 3. Version isolation

The Apache package version is `0.9.1`. It was not emitted as another `0.9.0`
artifact because the earlier 0.9.0 sdist/wheel have different proprietary
metadata and recorded hashes. The application feature set and database Schema
8 are otherwise unchanged by this license migration.

## 4. Package verification

The package was built into the gitignored
`validation/local/apache-2.0-release-0.9.1-r4/` directory, not `dist/`, so the
historical 0.9.0 artifacts were not overwritten.

| Check | Result |
|---|---|
| sdist | `video_download_control-0.9.1.tar.gz`; 540,160 bytes; SHA-256 `422a9394c734f9e134858cea3a9565c09e0365ca5a3748ed5f5824832686267c` |
| wheel | `video_download_control-0.9.1-py3-none-any.whl`; 290,100 bytes; SHA-256 `357a7416f9e96dca39efc858edea215603945a7f8a1c8a04419b06fa8b892280` |
| metadata | `Version: 0.9.1`; `License-Expression: Apache-2.0` |
| packaged legal files | 32 in both sdist and wheel: root `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, and 29 audited third-party files |
| entry points | 11 console scripts |
| isolated install | Python 3.12.13; import version and installed metadata both `0.9.1`; Apache expression, 32 license files, and `NOTICE` confirmed |
| content boundary | no `data`, `dist`, `runtime-tools`, `validation/local`, logs, database, or media paths in either artifact; this self-referential release-evidence file is explicitly excluded from the sdist |

The Git attributes mark `licenses/python/**` as non-text so the audited legal
files retain their exact upstream bytes after a clean clone. All 29 indexed
files match `compliance/python-license-files.sha256`; the r4 sdist and wheel
both retain the audited pydantic-core license bytes rather than a line-ending
normalized variant.

## 5. Regression evidence

- Focused license-compliance suite after the clean-clone and sdist fixes:
  `5 passed`.
- Full suite before the final source-equivalent r4 package rebuild:
  `882 passed, 8 skipped in 50.46s`.
- The eight skips are the existing explicit Windows/POSIX boundary: one
  directory-FD Cookie cleanup, four root/getfacl metadata cases, one AF_UNIX
  roundtrip, and two POSIX replacement/permission cases.
- `uv lock --offline` updated only the project package version from 0.9.0 to
  0.9.1; the final `uv lock --check --offline` and source/test `compileall`
  both passed.

## 6. Public-source privacy boundary

The public documentation omits the private architecture-document path and
fingerprint, Windows account path, location/timezone metadata, exact live-test
URLs and account/status identifiers, runtime batch/job/asset UUIDs, and media
fingerprints. Raw platform evidence, downloaded media, JSONL logs, SQLite data,
runtime tools, build output, and isolated-install environments remain under
gitignored local directories and are not part of the Git publication.
