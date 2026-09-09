# Iteration 0.28.0 local runtime refresh

Validated: 2026-09-10 against source commit `97929f67d200fa765d3f43d084f9774121645828`. This is local environment evidence, not a frozen release receipt or real-provider/platform acceptance.

## Result

The active Windows application root now has current AI and upload runtimes. The previous upload runtime was retained intact under a new local sibling backup name before installation. The existing upload database was also copied with the SQLite backup API while the application was stopped before its first current-schema initialization; that private backup remains outside Git. The application then migrated the exact Upload Schema 1 database to Schema 3, preserving the existing account and two canceled-job records. Both the source backup and migrated database returned `quick_check=ok` with zero foreign-key errors.

The AI runtime was built from the already-downloaded CPython 3.13.15 Windows x64 embeddable archive whose SHA-256 matched the pinned value `D1F04D990AEE1253D8569E8E5104E30FA9F5FA830899F14843448872D936A2CF`. Its current manifest SHA-256 is `82a228e3d59800f7e0d11d360e34c928f4c25733b13e47fb62528932e6cd4503`. The upload runtime verified the pinned Social Auto Upload revision `0012d2c355f88f683cc38dde2a2db209e14091bc`, Biliup `v1.2.4`, and Bilibili, Douyin and WeChat Channels adapters.

This Codex desktop host applies Windows package filesystem virtualization to the ordinary `%LOCALAPPDATA%` spelling. The AI builder correctly rejected that lexical alias because its final resolved directory differed. The successful Setup and Start used the same canonical resolved application root through the existing `--app-root` option. This is a host-specific path condition; no source change or weaker path-integrity rule was needed. The canonical path, database backup path, account identifiers and private runtime data are deliberately omitted from this repository record.

## Runtime validation

| Check | Result |
| --- | --- |
| `Setup-Open-Flame.cmd --yes --app-root <canonical-app-root> --upload-runtime --ai-python-embed-zip <verified-local-archive>` | **PASS**. Core environment and tools were reused, the AI runtime verified, and the current upload runtime installed and verified. |
| AI runtime CLI `--check` | **PASS**: `ready=true`, CPython 3.13.15 x64, runtime version 1 and protocol schema 1. No provider call was made. |
| Upload runtime CLI `--check` | **PASS**: `ready=true`, `code=ready`. |
| Actual `Start-Open-Flame.cmd --app-root <canonical-app-root> --allow-direct-network --no-open-browser --port 18833` | **PASS** after confirming the retained upload rows were canceled: local app reached `status=ready`; download runtime reported `managed_direct / online` with network enabled. No download job was created. |
| `/api/v1/edits/ai/runtime` | **Expected blocked provider state**: runtime integrity `verified`, provider health `unverified`, `ready=false`, `code=provider_health_required`. No API key was present and no OpenAI request was made. |
| `/api/v1/uploads/status` | **PASS**: backend ready, all three target platforms present, scheduler `running`, upload worker running. No login or upload job was triggered. |
| Production pages `/`, `/edits`, `/uploads`, `/workflows` | **4/4 HTTP 200** from the actual local app. |
| Shutdown | **PASS**: the loopback port was released and the observed download Worker process exited. |

The upload installer followed its existing fixed-input installation path and obtained the pinned source, wheel and Chromium inputs. No QR scan, account login, OpenAI call, target media-site download, upload, scheduled publication or public submission occurred. The existing local account row still reports its stored `ready` state, but this run did not contact its platform and therefore does not prove the remote session remains valid.

## Remaining boundaries

- The local runtime installation removes the missing-runtime blocker on this application root. AI workflows remain blocked until an explicitly configured key passes provider health, and real calls remain subject to separate data-egress and cost confirmation.
- Runtime/backend readiness proves local integrity and command contracts. It does not prove that any platform currently accepts the configured title, tags, cover, schedule, declaration or video.
- The retained legacy runtime and private pre-migration database backup are local rollback evidence only. They are not committed or included in release artifacts.
- Commit `97929f6` is a clean source milestone on `origin/main`, but the earlier `0592b6f` release receipt does not cover it. A new release still requires frozen artifacts and a matching external receipt.
