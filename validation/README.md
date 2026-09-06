# Stage 0 validation workspace

Current iteration: [0.24.4 upload data lifecycle and offline delivery](iteration-0.24.4-upload-data-lifecycle-evidence.md), following the [0.24.3 final source review](iteration-0.24.3-final-review.md), [0.24.2 download/upload integration](iteration-0.24.2-integration-evidence.md), [0.24.1 inline QR login](iteration-0.24.1-qr-login-evidence.md) and [0.24.0 first-platform uploader](iteration-0.24.0-upload-evidence.md). The [0.23.0 release](iteration-0.23.0-release-evidence.md) and [source setup](iteration-0.23.0-source-setup-evidence.md) records remain historical evidence.
The 0.24.4 record covers the source-side freeze contract: Upload Schema 2, account/media lifecycle controls, a secret-free stopped-state upload backup and new-root restore, bounded synthetic resilience tests, and the local contract for a credential-free CI matrix. The packaged record deliberately does not embed its own final Git commit, product identity or artifact hashes; an external release receipt must bind the clean detached commit, full-suite result, five artifacts and independent installation. GitHub hosted checks are NOT RUN and required checks/branch protection are NOT CONFIGURED. This iteration performs no real login, scan, download, media upload or publication. Earlier Bilibili QR evidence remains bound to its historical build and does not make 0.24.4 a real-platform acceptance result.

This directory contains Stage 0 schemas/templates plus explicitly labeled
engineering evidence. Raw current-platform samples, logs and media remain under
gitignored `validation/local/`; the summaries here are not by themselves evidence
that an entire platform is supported. Iteration 0.6's offline
Schema 8/graph record is [separate from Stage 0](iteration-0.6-graph-v2-offline-evidence.md),
as is Iteration 0.7's [short-link, Cookie and Linux acceptance engineering
record](iteration-0.7-short-link-cookie-linux-offline-evidence.md). Iteration 0.7.1 的
[前端、完整回归、使用与许可证证据](iteration-0.7.1-debug-use-license-evidence.md)
同样只包含本机 synthetic/offline 工程验收；它不是 Stage 0、真实平台、Linux/Docker
运行或公开发布许可的证明。Iteration 0.7.2 的
[脱敏运行日志、调试与使用验收证据](iteration-0.7.2-runtime-logging-evidence.md)
进一步覆盖控制端/Worker/子进程日志、前端查看、泄漏标记扫描和备份排除，但仍受同样边界约束。
Iteration 0.8.0 的
[Windows x64 本机工具链接入验收证据](iteration-0.8.0-local-toolchain-evidence.md)
记录固定 yt-dlp/FFmpeg/ffprobe、离线 synthetic smoke、API/UI/日志与发布包回归；其中
`ready` 仅表示本机工具完整，**不表示**隔离 Worker、联网下载、真实平台或 standalone downloader 已完成。
Iteration 0.8.1 的
[真实平台双样本验收证据](iteration-0.8.1-live-platform-evidence.md)
记录用户明确授权的一个 YouTube URL 和一个 X URL 的无 Cookie 探测、真实下载、
Node.js A/B、ffprobe、SHA-256 与完整解码；它只证明这两个样本在记录时的本机结果，
不代表整个平台受支持，也不满足 Stage 0 的样本量与连续运行门槛。
Iteration 0.9.0 的
[Windows 本机 Worker 全链路验收证据](iteration-0.9.0-local-worker-e2e-evidence.md)
继续用同两个精确输入覆盖 API 队列、独立 Worker、AssetStore/manifest、成品下载 API、
浏览器 UI 与结构化日志。该结果仍只证明精确样本和记录时的 Windows 环境，不会把
平台能力升级为 `verified`，也不构成 Linux/Docker 隔离证明。

Version 0.9.1 的 Apache-2.0 许可证迁移与公开发布证据记录在仓库专属的
`apache-2.0-license-migration-evidence.md`；该文件包含自身历史 sdist 摘要，为避免
自引用而被所有后续 sdist 明确排除。它记录权利人决议、版本隔离、包元数据、法律文件、
隐私扫描与回归结果。0.7.1–0.9.0
文档中的 `LicenseRef-Proprietary` 和 31-files 描述是当时构建的 point-in-time 历史事实；
0.9.1 只在项目自有源码许可范围取代它们，不解除第三方 binary/container/tool-bundle
发布门禁。

Iteration 0.10.0 的
[六平台下载能力与 Worker 路由工程证据](iteration-0.10.0-multiplatform-routing-evidence.md)
记录 Bilibili、Douyin、TikTok、Instagram 的窄范围 URL、声明式能力矩阵、精确
Worker claim 过滤、离线 fake 资产链、前端、打包、隐私和许可证复核。该工程记录本身
没有执行真实媒体请求；同轮稍后的
[Instagram 单样本本机全链路证据](iteration-0.10.0-instagram-live-evidence.md)
记录一条 NASA 官方公开 Reel 在全新 data root、Windows direct/no-cookie/Node 下达到
`1/1 ready`，并覆盖 DB、manifest/API copy、完整解码、清理、JSONL 日志和浏览器 UI。
Bilibili 真实尝试遇 HTTP 412；Douyin 官方宣传样本要求 fresh cookies，未提供 Cookie 时
正确归类为 `authentication_required`；TikTok 未实跑。单样本不是 Stage 0，全部能力仍为
`candidate`。

Iteration 0.11.0 的
[Schema 9 与 Bilibili HTTP 412 工程证据](iteration-0.11.0-schema9-bilibili-evidence.md)
记录 Stage 0 CSV v2、route-specific evidence identity、Schema 8→9 迁移/恢复边界，以及
固定 yt-dlp `2026.08.19`、同一公开样本、无 Cookie 条件下的重复 412 诊断。raw probe
为 `2/2` 成功，产品同款 fresh attempt 为 `4/6` 成功、`2/6` 在 probe 阶段收到 HTTP
412；当前只能推断平台侧瞬时门禁最符合观测，不能声称已确定具体风控原因或已修复
Bilibili 下载能力。仅 Bilibili 的受限 412 标记会映射为 `rate_limited`，不添加 Cookie、
header 或代理绕过；能力继续为 `candidate`。

Iteration 0.12.0 的
[辅助产物、TikTok 短链与 MVP 路由离线 E2E 工程证据](iteration-0.12.0-artifact-tiktok-evidence.md)
记录 ready thumbnail/caption 列表、严格辅助下载端点/UI、TikTok `vm`/`vt`
默认关闭的精确 host allowlist 短链路径，以及 YouTube video/Shorts、Bilibili
BV/av、gated Douyin 短链和 TXT/CSV 导入的 fake Worker E2E。该记录不含真实
媒体请求或私有运行数据，不是 Stage 0。相关离线定向回归为
`160 passed in 14.08s`，辅助产物稳健性回归为 `18 passed in 5.74s`，最终全量为
`969 passed, 8 skipped in 63.58s`；8 个 skip 均为待目标 Linux 执行的明示环境边界。
回填证据后的 source-equivalent 0.12.0 sdist/wheel、隔离安装、Apache-2.0 metadata、
11 个入口、32 个法律文件、源码一致性与已知私有标记扫描均已通过。

Iteration 0.13.0 的
[Schema 10 capability governance 工程证据](iteration-0.13.0-capability-governance-evidence.md)
记录 CSV v3、aggregate-only report、immutable evidence、append-only approve/revoke decision
chain、revision CAS、read-only current view、version + package-payload build identity，以及单次
snapshot API/UI、迁移、备份和发布包复验。
该记录只使用 synthetic/offline 数据，不包含真实 URL、URL hash、sample/run ID、主机路径、
运行数据库或批准记录；手写 CSV 和普通 SHA-256 也不是密码学执行证明。

Iteration 0.14.0 的
[Windows 本机 Worker Cookie 接入工程证据](iteration-0.14.0-local-cookie-evidence.md)
记录按平台 Cookie source、attempt-private 副本、非领取式 `--check`、source 大小/物理身份
门禁，以及 synthetic Douyin CredentialProfile → Worker → ready Asset 的离线闭环。该记录
没有使用真实 Cookie 或发起真实平台请求，也没有把 Windows direct Worker 提升为隔离部署。

Iteration 0.15.0 的
[Windows 一体化本机应用工程证据](iteration-0.15.0-local-app-evidence.md)
记录 supervisor 的固定配置、三阶段握手、单实例、Windows Job Object、
Worker-first 停机、异常子进程回收、共享 `run_id` 日志、真实浏览器 QA 和
source-equivalent Apache package 复验。该记录不含真实 Cookie 或新的平台请求，
也不改变 Stage 0、Linux/Docker 或第三方二进制再分发结论。

Iteration 0.16.0 的
[Schema 11 claim fencing 与显式 flat retry 工程证据](iteration-0.16.0-claim-retry-evidence.md)
记录 local-app 的 prepare/activate 两阶段 gate、SQLite stop/claim 顺序、gate 关闭失败时的
owned-Job fail-safe、既有 lease recovery，以及终态失败 flat download 的新 generation
重试与独立 cooldown/manual reset 运维。最终全量为
`1238 passed, 8 skipped in 104.14s`；真实 Windows 隔离进程、浏览器竞态、active lease
关停及结构化日志均以 synthetic/no-network 输入复验，project-only source-equivalent
Apache sdist/wheel 也已独立检查。该记录不证明真实平台、目标 Linux/Docker、Stage 0、
Windows 安装器或任何第三方 binary/container/tool-bundle 可发布。

Iteration 0.17.0 的
[实时阶段估算进度工程证据](iteration-0.17.0-real-progress-evidence.md)
记录固定脱敏的 yt-dlp stdout 控制协议、有界完整行观察器、字幕/sidecar 忽略、
顺序主媒体与 indexed fragment 的保守聚合、持久化 `postprocessing` 阶段，以及
API/原生 `<progress>`/ARIA/中文阶段显示。定向回归、独立 loopback 浏览器 QA、
最终全量回归与 project-only source-equivalent package 复验均已完成；
精确 archive hash 只保存在 gitignored verifier/外部验收报告，不写入被打包源。
该记录没有发起真实平台请求，不证明目标 Linux/Docker、Stage 0、真实 Cookie 或任何
第三方 binary/container/tool-bundle 可发布；0.16.0 及更早记录继续作为历史证据保留。

Iteration 0.18.0 的 [双槽并发工程证据](iteration-0.18.0-concurrent-worker-evidence.md)
记录 Windows app 与独立 Worker drain/poll 的真实入口回归、跨平台重叠执行、连续补位、
清理期间排除、暂停与 Ctrl+C 收尾。该工程记录不包含新的平台请求、Cookie 或 UI 验收；
所有平台仍为 `candidate`，0.17.0 的浏览器和包记录只作为历史证据保留。

1. Copy `sample_manifest.template.csv` to a private location outside Git.
2. Use the Stage 0 v3 headers exactly, with no duplicate columns and the exact row width. Both files require `job_kind`; the results file additionally requires `product_version`, whose value is the full product build identity printed by the exact package under test. Old files using `expected_media_count` / `observed_media_count`, bare release versions, mismatched build hashes, or results without `product_version` fail closed instead of being inferred or combined with current evidence.
3. Add at least 10 current, public, browser-accessible positive samples for every `platform × source_type × job_kind` cell, plus separate negative samples.
4. Interpret output count by route: for `download`, count published and fully verified media assets; for `discover`, count unique child/source items in the immutable discovery snapshot.
5. In the exact checkout or installation used for validation, run `uv run video-download-validation --print-product-identity`. Copy the returned `product_identity` verbatim into every results row; do not synthesize or shorten it.
6. Record each execution in a results CSV using `results.template.csv`.
7. Generate a URL-redacted report:

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

A cell becomes eligible for explicit approval only when the manifest has at least 10 public positives, negative evidence exists, and the latest three executions for the exact same `platform × source_type × job_kind × adapter × downloader_version × environment × product_version` are all complete and each meets the positive success threshold while all negative results match their expected terminal outcome. `product_version` has the form `0.23.0+build.sha256.<64 hex>` and binds the release number to every ordinary file in the importable package payload except regenerable PEP 3147 `__pycache__/*.pyc`; non-bytecode files inside that directory and sourceless bytecode elsewhere remain covered. Package links/reparse/special files or inventory drift fail closed. A newer partial execution fails closed instead of being skipped in favour of older complete runs, and runs that mix identity fields are rejected. Source samples are deduplicated by framed `platform/source_type/source_id` plus the outer `job_kind`, so TikTok aliases with the same video ID are not independent samples. Bilibili Stage 0 accepts BV identifiers only because the same submission can also have an av alias; normal downloads still accept both forms. The report contains only aggregate evidence; raw URLs, filesystem paths, sample IDs and source-identity fingerprints are omitted.

`video-download-validation` evaluates recorded evidence; it does not launch yt-dlp and cannot create real validation evidence by itself. It does not write the database. `video-download-capabilities import` requires an existing ready Schema 11 database whose absolute canonical path is a single-linked ordinary file, re-evaluates the private CSV and appends qualified or insufficient evidence, but never auto-approves it; list/history never migrate an older database. An explicit local CAS-protected approve/revoke decision is a separate operation and does not enable any Worker, short-link or graph gate. Import and approve require the exact current build identity and recheck it before commit; historical evidence remains readable and revocable but is not current-build eligibility. Evidence and decisions reject update, delete and `INSERT OR REPLACE`. A local administrator can still fabricate CSV or rewrite both database guards and hashes, so this remains an auditable application rule rather than cryptographic execution attestation.
