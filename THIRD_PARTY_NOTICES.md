# Third-Party Notices

## Optional upload runtime (0.24.0)

The upload integration uses separately installed upstream tools. Neither the
Open-Flame source distribution nor its wheel includes their source archives,
dependency wheels, browser binaries, account state, or the biliup executable.
The application itself remains Apache-2.0 with its existing NOTICE unchanged.

- social-auto-upload: MIT; fixed source revision and archive identity are listed
  in [the uploader review](docs/OPEN_SOURCE_UPLOADER_REVIEW.md). The installer
  retains the upstream source archive and its LICENSE in the runtime directory.
- biliup: MIT; a separately downloaded, hash-checked Windows release is used for
  Bilibili login and submission. Release and source links are documented in the
  same review.
- Upload-only Python distributions, Patchright/Playwright and their browser
  distributions have their own license materials. The exact dependency lock is
  [runtime-lock.json](src/video_download_control/uploads/runtime-lock.json).

Installing these tools locally is separate from distributing an offline bundle.
This change does not claim that an aggregate third-party binary bundle has been
audited or approved for redistribution. See [runtime setup](docs/UPLOAD_RUNTIME.md).

Project-authored source code, documentation, and scripts are licensed under
the Apache License, Version 2.0. Copyright 2026 HedgehogsGX & Cyaegha_Xu.
Third-party components remain governed by their own licenses; the project's
Apache-2.0 grant does not relicense them. See `LICENSE` and `NOTICE`.

The table below was audited against the exact versions in `uv.lock` and the
deployment hash locks. The corresponding unmodified license/notice files are
copied from the exact wheel artifacts into `licenses/python/`. Runtime wheels
also retain their upstream `.dist-info/licenses/` files when installed.

## Runtime Python components

| Component | Relationship | License expression | Upstream | Included license material |
|---|---|---|---|---|
| annotated-doc 0.0.5 | Transitive | MIT | https://github.com/fastapi/annotated-doc | `licenses/python/annotated_doc-0.0.5/LICENSE` |
| annotated-types 0.8.0 | Transitive | MIT | https://github.com/annotated-types/annotated-types | `licenses/python/annotated_types-0.8.0/LICENSE` |
| anyio 4.14.2 | Transitive | MIT | https://github.com/agronholm/anyio | `licenses/python/anyio-4.14.2/LICENSE` |
| click 8.5.0 | Transitive | BSD-3-Clause | https://github.com/pallets/click | `licenses/python/click-8.5.0/LICENSE.txt` |
| fastapi 0.141.1 | Direct | MIT | https://github.com/fastapi/fastapi | `licenses/python/fastapi-0.141.1/LICENSE` |
| h11 0.16.0 | Transitive | MIT | https://github.com/python-hyper/h11 | `licenses/python/h11-0.16.0/LICENSE.txt` |
| idna 3.19 | Transitive | BSD-3-Clause | https://github.com/kjd/idna | `licenses/python/idna-3.19/LICENSE.md` |
| Pillow 12.3.0 | Direct/native image decoder | MIT-CMU with bundled codec notices | https://github.com/python-pillow/Pillow | `licenses/python/pillow-12.3.0/LICENSE` |
| pydantic 2.13.5 | Transitive | MIT | https://github.com/pydantic/pydantic | `licenses/python/pydantic-2.13.5/LICENSE` |
| pydantic-core 2.46.5 | Transitive/native | MIT for the top-level project; native closure is a release gate below | https://github.com/pydantic/pydantic/tree/main/pydantic-core | `licenses/python/pydantic_core-2.46.5/LICENSE` |
| starlette 1.6.0 | Transitive | BSD-3-Clause | https://github.com/Kludex/starlette | `licenses/python/starlette-1.6.0/LICENSE.md` |
| typing-extensions 4.16.0 | Transitive | PSF-2.0 | https://github.com/python/typing_extensions | `licenses/python/typing_extensions-4.16.0/LICENSE` |
| typing-inspection 0.4.4 | Transitive | MIT | https://github.com/pydantic/typing-inspection | `licenses/python/typing_inspection-0.4.4/LICENSE` |
| uvicorn 0.52.4 | Direct | BSD-3-Clause | https://github.com/Kludex/uvicorn | `licenses/python/uvicorn-0.52.4/LICENSE.md` |

## Development and build components

These tools are not intended to be installed in the final runtime image. Some
reuse runtime components already listed above.

| Component | Relationship | License expression | Upstream | Included license material |
|---|---|---|---|---|
| colorama 0.4.6 | Development transitive; Windows only | BSD-3-Clause | https://github.com/tartley/colorama | `licenses/python/colorama-0.4.6/LICENSE.txt` |
| hatchling 1.27.0 | Build direct | MIT | https://github.com/pypa/hatch/tree/master/backend | `licenses/python/hatchling-1.27.0/LICENSE.txt` |
| httpcore2 2.12.0 | Development transitive | BSD-3-Clause | https://github.com/pydantic/httpx2/tree/main/src/httpcore2 | `licenses/python/httpcore2-2.12.0/LICENSE.md` |
| httpx2 2.12.0 | Development direct | BSD-3-Clause | https://github.com/pydantic/httpx2 | `licenses/python/httpx2-2.12.0/LICENSE.md` |
| httpx2-jsfetch 1.0 | Development transitive; Emscripten only | BSD-3-Clause | https://github.com/pydantic/httpx2 | `licenses/python/httpx2_jsfetch-1.0/LICENSE.md` |
| iniconfig 2.3.0 | Development transitive | MIT | https://github.com/pytest-dev/iniconfig | `licenses/python/iniconfig-2.3.0/LICENSE` |
| packaging 26.3 | Development/build transitive | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging | `licenses/python/packaging-26.3/` |
| pathspec 1.1.1 | Build transitive | MPL-2.0 | https://github.com/cpburnz/python-pathspec | `licenses/python/pathspec-1.1.1/LICENSE` |
| pluggy 1.6.0 | Development/build transitive | MIT | https://github.com/pytest-dev/pluggy | `licenses/python/pluggy-1.6.0/LICENSE` |
| Pygments 2.21.0 | Development transitive | BSD-2-Clause | https://github.com/pygments/pygments | `licenses/python/pygments-2.21.0/LICENSE`, `licenses/python/pygments-2.21.0/AUTHORS` |
| pytest 9.1.1 | Development direct | MIT | https://github.com/pytest-dev/pytest | `licenses/python/pytest-9.1.1/LICENSE` |
| trove-classifiers 2026.6.1.19 | Build transitive | Apache-2.0 | https://github.com/pypa/trove-classifiers | `licenses/python/trove_classifiers-2026.6.1.19/LICENSE` |
| truststore 0.10.4 | Development transitive | MIT | https://github.com/sethmlarson/truststore | `licenses/python/truststore-0.10.4/LICENSE` |

## Browser documentation assets

The main control page is self-contained. FastAPI's CDN-backed `/docs` and
`/redoc` pages are disabled, so normal use does not implicitly fetch mutable
Swagger UI, ReDoc, Google Fonts, or FastAPI favicon resources. The local,
machine-readable API contract remains available at `/openapi.json`.

## Optional local Windows tool bootstrap

Version 0.8.0 and later can download a fixed Windows x64 tool bundle into an
operator-selected, gitignored local directory. These binaries are not embedded
in the project wheel/sdist and are not added to the system `PATH`.

| Component | Exact local artifact | Declared license boundary | Evidence installed beside the tools |
|---|---|---|---|
| yt-dlp 2026.08.19 | Official platform-independent `yt-dlp` zipimport, SHA-256 `1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6` | `Unlicense AND MIT AND ISC`; this deliberately avoids the GPL-3.0-or-later PyInstaller Windows EXE | Exact source tarball, upstream `SHA2-256SUMS`, detached signature, and extracted yt-dlp `LICENSE` |
| FFmpeg/ffprobe n9.0.1-11-ge47273f4d9-20260831 | BtbN Windows x64 LGPL shared month-end build, archive SHA-256 `83a824f0729a69d143c9865125bb86988a11dd388325f0033711045522068aa0` | Declared `LGPL-3.0-or-later`; exact configuration must contain `--enable-version3 --enable-shared --disable-static` and must not contain `--enable-gpl` or `--enable-nonfree` | Extracted LGPL v3 text (unchanged from the previous pin) plus a lock for every installed EXE/DLL size and SHA-256 |

yt-dlp is obtained from `https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19`.
FFmpeg upstream publishes source rather than Windows binaries; its official
download page links the third-party BtbN builds used here. The fixed build is
obtained from
[BtbN month-end release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-08-31-13-27).

The previous 2026-08-20 daily artifact returned HTTP 404 during source-install validation. The current fixed month-end tag is subject to BtbN's two-year retention policy; the tag itself is not marked immutable, so the exact archive/member hashes remain mandatory. This is not a guarantee of permanent availability, and it does not authorize redistribution of the tool bundle. See the [upstream retention policy](https://github.com/BtbN/FFmpeg-Builds#release-retention-policy).

The bootstrap verifies the pinned checksum files but currently retains rather
than cryptographically verifies the yt-dlp detached OpenPGP signature. More
importantly, this local integrity record is not a complete redistribution
closure for all libraries linked into the FFmpeg build. The checked-in policy
therefore reports
`blocked_pending_third_party_source_and_notice_audit`; packaging or giving this
tool directory to a third party remains blocked until corresponding source,
build scripts, all third-party notices, target SBOM, relinking obligations, and
legal review are complete. Local installation does not grant additional rights
in those tool artifacts beyond their applicable upstream licenses.

## Distribution boundary and unresolved third-party release gates

The checked-in project-authored source and a freshly verified project-only
pure-Python sdist/wheel may be used, modified, and distributed under
Apache-2.0 when `LICENSE`, `NOTICE`, this notice, and the applicable third-party
license files are retained. This permission does not by itself approve a
dependency wheelhouse, frozen executable, offline installer, OCI/container
image, or yt-dlp/FFmpeg tool bundle for redistribution.

Those third-party binary and container outputs remain blocked until all of the
following target-specific checks are complete:

- A target-specific native SBOM and complete license bundle is generated for
  the exact `pydantic-core` and Pillow wheels. `pydantic-core`'s Rust dependency
  closure is not fully represented by the wheel's top-level MIT file; Pillow's
  native image codecs and their linked-library closure also require review for
  the exact target artifact.
- The exact target-platform OCI child image is pulled and audited, including
  CPython, pip, Debian packages, notices, corresponding-source duties, and
  image provenance. A multi-architecture index digest alone is insufficient.
- The fixed local yt-dlp/FFmpeg artifact choices above receive a target-specific
  SBOM, complete linked-library notice/source closure, and legal approval before
  any redistribution. Artifact hashes and FFmpeg configure flags are now
  locked, but they do not prove the full linked dependency closure. A build
  containing `--enable-gpl` or `--enable-nonfree` is rejected by this local lock.

The candidate Dockerfile therefore requires a reviewed, immutable tool image
whose manifest, binaries, SBOM, source artifact, approval record, and license
texts pass `deployment/validate_tool_bundle.py`. Integrity validation cannot
replace a rights-holder or legal review of whether a declared license is
correct.
