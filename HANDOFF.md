# 多平台视频下载项目开发交接

> 每轮结束更新本文件的状态、证据、风险、下一入口和历史。  
> 最后更新：2026-09-03  
> 当前迭代：Iteration 0.9.1 — Apache-2.0 开源发布与供应链边界拆分  
> 当前版本：`0.9.1`；数据库：Schema `8`

Iteration 0.9.1 保留 0.9.0 的 Windows 本机直连 Worker、最近批次、ready 资产列表/下载和 `local-worker` 结构化日志能力，并由权利人将项目自有源码、文档与脚本正式授权为 Apache-2.0，版权声明为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。用户指定的 YouTube/X 双样本此前已完整经过“API Batch → Worker claim → 真实 yt-dlp/FFmpeg/ffprobe → AssetStore/manifest → API 下载”，并由前端重新打开和取回成品；公开记录省略输入 URL、运行时标识和媒体指纹。该结论只覆盖两个样本与 Windows 本机直连路径；Linux 隔离 Worker、Docker、真实 Cookie、完整 Stage 0 和平台级认证仍未完成。源码许可已闭合，但 dependency wheelhouse、冻结可执行文件、OCI/container image 与 yt-dlp/FFmpeg 工具包的第三方再分发门禁不因此放宽。

## 1. 权威规格与边界

- 规划文档：私有本机文件 `视频下载项目选型与整体架构规划.md`（不纳入仓库）
- 文档日期：2026-09-02
- 读取时 SHA-256：已在本机核对；为避免公开私有文档指纹，此处省略

原文是选型/架构决策稿，不是已验证安装手册。代码、fake adapter、静态 Compose、Windows 测试或未执行 runner 都不等于真实平台支持。不得自行抓取随机/未授权媒体；Stage 0 只能使用用户明确提供或确认有权使用的样本。

## 2. 当前决策基线

| 决策 | 当前值 | 证据状态 |
|---|---|---|
| 产品 | 单机/NAS、单管理员、私有自托管 | FastAPI 已实现；Windows 本机可独立使用；无认证且强制 loopback |
| 批量/并发 | 每批 1–50；总活动 2；单平台 1 | 输入验证与 SQLite claim 事务强制；双样本已实际经过 Batch/claim/terminal 链 |
| 数据 | Schema 8；旧 flat-v1 保留 | graph-v2 离线编排/恢复已验证；真实 X exact selector 未验证 |
| 内核 | yt-dlp + FFmpeg/ffprobe 候选 | Windows x64 固定工具已安装、逐文件校验并通过离线 smoke；两个精确 URL 已经本机直连 Worker 实跑，平台整体未验证 |
| 短链 | 可信 egress 逐跳展开 | UDS/HMAC/replay/peer attestation 已离线实现；gate=0 |
| 凭证 | 按平台 opaque ref；网页不编辑秘密 | DB 只存 ref；Cookie override 已静态验证；真实 Cookie 未挂载 |
| 资产 | 不可变原件 + 脱敏 manifest/sidecar | fake/crash recovery 与双样本真实 Worker/AssetStore/manifest/API 下载链均已验证；API 不暴露本机路径 |
| 前端/API | Batch 提交、最近批次、状态、ready 资产与运行日志 | 双样本可从最近批次重开并通过资产下载链接取回；浏览器验收无 error/warn |
| 运行日志 | 控制面/Worker allowlist JSONL + 近期事件 API/UI | `local-worker` 已纳入既有脱敏、轮转、读取和前端展示契约；日志仍为 best-effort |
| 部署 | Windows 控制面 + 本机直连 Worker；Docker Compose 单机候选 | 本机 direct Worker 已真实验收，但明确不提供网络隔离；Linux 隔离 Worker/Compose 未 build/cold-start/full acceptance |
| 备份 | DB 快照 + 已发布资产；秘密单独恢复 | Schema 8 Windows 小数据恢复已验证；无 Linux/NAS 证据 |
| 许可证 | 项目自有材料 `Apache-2.0`；`NOTICE` 为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu` | 源码和重新验证的 project-only sdist/wheel 可按许可分发；第三方 binary/container/tool bundle 仍 blocked |

## 3. 累计交付

### 3.1 前六轮保留能力

- 四平台 MVP URL 白名单/规范化/去重、Batch/API、持久化队列、Attempt/lease/retry/cancel、queue/circuit 和脱敏 metrics。
- 不可变资产、sidecar、SHA-256 manifest、commit intent、三阶段 crash recovery 和 Schema 8 备份/独立根恢复审计。
- X graph-v2 parent discovery、snapshot、exact child target、active generation、partial success、Input cancel/rediscover 和 ready-asset reuse。只有 `ScriptedGraphFakeAdapter` 离线 exact-selector=true；真实 `YtDlpAdapter.supports_exact_selector=False`。
- admin-only CredentialProfile CLI 只存 bounded opaque ref，claim 时检查平台/禁用/过期；公开 Batch API 拒绝 credential 字段。

历史证据：[Iteration 0.5 recovery](validation/backup-restore-drill-iteration-0.5.md) 与 [Iteration 0.6 graph-v2](validation/iteration-0.6-graph-v2-offline-evidence.md)。

### 3.2 Iteration 0.7：short-link 路径

- 新增默认关闭的 `video-download-short-link-egress`。控制面只在 POSIX、显式 gate、私有 UDS/key 同时有效时注入 resolver。
- 协议使用 bounded canonical JSON、domain-separated HMAC-SHA256、256-bit nonce、freshness/clock-skew、request-hash binding 和 crash-durable replay marker。
- 控制侧每跳只解析一次 DNS，拒绝非公网/混合答案；egress 只连 numeric IP，保留原 host 做 TLS SNI/证书/HTTP `Host`，并对实际 peer 做 attestation。
- egress 不自动 redirect，只返回 bounded `Location`。跨平台、回环、hop/header/body/frame/deadline 超限均 fail closed。
- Batch 对唯一短链共享 15 秒 budget；内建 resolver 收到剩余 budget。持久化前二次验证 result type、canonical URL、platform、source type/ID；异常/恶意结果固定脱敏失败。
- signed final Location、numeric IP、socket/key path、DNS/异常原文不进 API、SQLite、backup manifest 或普通 audit。Replay 磁盘操作从 event loop 卸载到专用有界 executor。

### 3.3 Iteration 0.7：Cookie 边界

- credential-free base Compose 无 Cookie。显式 [`compose.candidate.cookies.yaml`](deployment/compose.candidate.cookies.yaml) 才把 source root、固定四 key mapping 和 reviewed wrapper 只读绑定给 Worker。产品 contract 允许 0–4 source；full acceptance 故意要求四个 synthetic source。
- host validator 把 mapping 当数据解析，不 source dotenv，不打开/哈希/打印 Cookie；检查 canonical path、owner/group/mode、nlink、base ACL、安全祖先链、唯一 identity/ref、overlap 和 snapshot。
- standalone validator 只经绝对 `/usr/bin/env -i` 传五个值，再以绝对 `/bin/sh` 执行，避免把 `BASH_FUNC_*`/`ENV`/PATH 带入子 shell。
- Worker 通过 no-follow descriptor 重验 source，每次复制到新的 Attempt-private `0600` 文件，核对 identity/大小/时间/fsync。失败清理使用已 attested directory FD，不被 path swap 诱导。
- 轮换契约：stop → clean-env staging validation → 受信绝对 GNU `/usr/bin/mv -fT` → platform-parent fsync → normal validation → `--force-recreate worker`；任一失败保持 Worker stopped。

### 3.4 Iteration 0.7：Linux/Docker acceptance

- [`run-linux-acceptance.sh`](deployment/run-linux-acceptance.sh) 默认 `--preflight`，不执行变更；full mode 额外要求精确授权 token、private env/override、digest-pinned tool image、deny-only policy、四 synthetic Cookie source、Schema 8 backup 和专用空根。
- 必须由 clean trusted root launcher 以绝对路径启动；execute 要求 effective root。runner 要求 `bash -p`、固定 `/usr/bin/python3` 且验证 Python 3.12+，所有 host Python 均用 `-I`。
- Docker endpoint 正确尊重非空 `DOCKER_CONTEXT` 高于 `DOCKER_HOST` 的官方优先级，任何 current/default context 也只能解析到 local Unix Linux daemon。execute 拒绝 inherited `DOCKER_CONTEXT`、`DOCKER_HOST`、`DOCKER_CONFIG`、`DOCKER_CERT_PATH`、`DOCKER_TLS_VERIFY`、`BUILDKIT_HOST`、`BUILDX_BUILDER`、`COMPOSE_FILE`、`COMPOSE_PROJECT_NAME`、`COMPOSE_PROFILES`。
- private env/override 精确 `root:root 0600`；policy 精确 `root:10001 0440`，并检查 nlink/size/ACL/祖先/snapshot。baseline/effective Compose 逐 service 精确比较；除 reviewed Cookie wrapper 和两个 Worker-only read-only bind 外，任何 command、healthcheck、namespace、mount、network/IPAM/name、secret、hook 或非 Worker 变化均拒绝。
- Dockerfile 无外部 syntax image；`wheel_builder`/`runtime` 两个 Python `FROM` 硬编码同一 3.12.13 digest，无 ARG override。`pyproject.toml` 与 `requirements.build.in` 精确固定 `hatchling==1.27.0`，build/runtime lock 均 exact+hashed。唯一 package 联网步骤是 hash-checked、wheel-only `pip download`；后续 install/build 均 `RUN --network=none` + `--no-index`，项目 wheel 关闭 build isolation/deps 并按精确路径安装，runtime 最后 `pip check`。Lock 同时存在 wheel/sdist hashes 不会放行 sdist，`--only-binary=:all:` 会拒绝它。Runner 的 network-none runtime contract 另精确核对 13 个 runtime-lock distributions + project `0.9.1`，并拒绝 hatchling/packaging/pathspec/pluggy/trove-classifiers 五个 build-only distributions 泄漏；target Linux 尚未执行。
- Fresh local build tag 只作初始定位；runner 立即捕获/验证不可变 `sha256:...` image ID，把该 ID 写进 effective Compose，递归拒绝任意 string key/value 中的 `$`，在 env 同目录创建 `root:root 0600` frozen JSON。二次 `compose config` 必须与原 JSON 深等值、重过完整 validator 且 identity/snapshot 不变；之后 Compose mutation、direct runs、checkpoint image inspect 与 runtime container `Image` 校验全部绑定同一 ID，避免 tag rebind。正常退出按 identity 删除 frozen file，crash/identity drift 则保留供人工定点审计。
- full mode 同时要求 non-pipe host `/proc/sys/kernel/core_pattern` 与 `RLIMIT_CORE=(0,0)`，并用 Docker inspect 确认 Cookie bind exact Source/Type/`RW=false`。
- Full-mode 代码路径及静态测试设计覆盖 cold start、health、namespace/UDS、SSRF、peer attestation、SIGTERM/lease recovery、stale socket 和独立 Schema 8 restore。首个 Compose `up` 前已武装 scoped cleanup，因此部分启动后失败也会 stop；脚本无 recursive delete。目标 Linux/Compose execute 尚未发生。

### 3.5 Iteration 0.7.1：前端、回归与许可证

- 修复 Python 三引号字符串把 JavaScript `/\\r?\\n/` 破坏为非法正则的问题；页面不再卡在“正在读取”。新增统一 `fetchJson`，HTTP/非 JSON/网络错误可见，queue/circuit 分区失败不会误报健康。
- 轮询使用 generation + request id 双重 fencing，旧请求不再覆盖新批次或取消新 timer；瞬时失败 5 秒重试。文本与文件必须二选一；resume/reset/cancel 均 `try/catch/finally` 恢复按钮。
- 页面明确区分“队列未暂停”和“Worker 已启动”，并每 10 秒非重叠刷新运行状态。主前端无外部资产；FastAPI `/docs`、`/redoc` 已禁用，保留 `/openapi.json`。
- `BatchRepository` 支持注入 timezone-aware clock；测试夹具不再依赖当天墙钟。相同 `created_at` 的 claim 使用 SQLite `rowid` 作为插入顺序 tie-breaker，修复 UUID 随机排序 flake。
- 当时的根 `LICENSE` 与包元数据为 `LicenseRef-Proprietary`。该 0.7.1 历史构建事实由 0.9.1 的 Apache-2.0 权利人决议取代；`THIRD_PARTY_NOTICES.md` 对 26 个精确 runtime/dev/build 包和 29 份 exact-wheel 法律文件的 SHA-256 约束继续有效。
- candidate tool bundle 必须提供并 hash-bind executable、source artifact、SBOM、人工许可证复核记录和法律文本；artifact/license allowlist、FFmpeg configuration、`--enable-nonfree` 拒绝与实际 version/config execution 均 fail closed。它只验证证据完整性，不替代法律判断。

### 3.6 Iteration 0.7.2：脱敏运行日志

- 新增 stdlib-only JSONL logger；控制面固定写入 `${VDC_DATA_ROOT}/logs/runtime-control.jsonl`，Worker 文件名只使用实例 ID 的短哈希。默认单文件 10 MiB、保留 5 个轮转，可通过 `VDC_RUNTIME_LOG_LEVEL`、`VDC_RUNTIME_LOG_MAX_BYTES`、`VDC_RUNTIME_LOG_BACKUP_COUNT` 调整。
- 每个事件含 UTC 毫秒时间、schema、component、run/event ID、进程/线程 ID 和 run 内单调 `sequence`。事件名、level、字段和关键枚举全部 allowlist；未知异常只收敛为固定类别，日志写入失败不阻断业务并通过状态计数暴露。
- 控制面覆盖启动/停止、模板化 HTTP 路由、批次与控制操作；Worker 覆盖 claim、phase、retry、heartbeat、cleanup、pause 与 terminal result；受控子进程只记录 executable basename、参数个数、时长、返回码和输出字节数。
- `GET /api/v1/operations/logs` 只读取通过完整 schema/语义验证的近期事件；前端“运行日志”区域以 `textContent` 呈现最近 100 条并提供手动刷新。客户端 `X-Request-ID` 不被信任，响应返回服务端新生成的 ID。
- 请求 URL/query/body、批次名、输入文本、argv/env/cwd、绝对路径、stdout/stderr 与异常消息都不进入日志；活动日志/轮转日志也不进入业务备份。日志是 best-effort 排障线索，不替代 SQLite、资产 manifest、备份审计或监控告警。

### 3.7 Iteration 0.8.0：Windows x64 本机工具链

- 新增 `video-download-tools install/verify/smoke/status`，只支持机器可读锁批准的 Windows x64 bundle。目标必须是尚不存在的绝对规范化路径，下载/缓存逐项校验固定大小与 SHA-256，经同级 staging 完成后原子发布；不修改系统 `PATH`。
- yt-dlp 固定为 `2026.08.19` Python zipimport artifact；FFmpeg/ffprobe 固定为 BtbN LGPL shared build `n9.0.1-6-g9d4ca21220-20260820`。真实二进制版本、FFmpeg configuration、完整 managed file set、许可文本和 retained source/checksum/signature evidence 均 fail closed 校验。
- 离线 smoke 使用本机 FFmpeg 生成 synthetic 音视频并由 ffprobe 验证流结构；不访问媒体平台。API/UI 显示 `ready`，同时保留 `isolated_worker_ready=false`、`network_download_enabled=false`、`platform_download_verified=false`。
- candidate Worker 可使用受校验的 yt-dlp zipimport entrypoint，但 Linux network namespace/UDS relay/受控 egress gate 未放宽；字幕语言集合也保持有界。Windows 控制面不会因为工具 ready 自动启动 Worker。
- 最终安全审查补锁了 Windows 盘符/ADS/设备名/尾点空格与 UNC/device namespace 路径逃逸；`redistribution_status` 只能保持当前 blocked 值；HTTP 重定向在下一跳请求发出前逐跳校验 HTTPS/host/userinfo/port，并受单制品 600 秒总 deadline 约束。CLI smoke/status 始终输出全部安全门禁。
- 完整记录见 [Iteration 0.8.0 local toolchain evidence](validation/iteration-0.8.0-local-toolchain-evidence.md)。

### 3.8 Iteration 0.8.1：双样本真实下载、显式 JS runtime 与 UTF-8

- 用户明确提供本轮 YouTube/X 各一个公开测试样本；精确 URL、账号/状态标识与媒体指纹只保留在 gitignored 本机证据中。两者均使用固定 yt-dlp `2026.08.19` 和 FFmpeg/ffprobe `n9.0.1-6-g9d4ca21220-20260820`，未使用 Cookie；完成真实传输、ffprobe、SHA-256 与全流解码。
- YouTube 产物通过 VP9 + Opus WebM 的 ffprobe、SHA-256 和完整解码验证。未显式 JS runtime 时下载可成功但有 yt-dlp warning；显式 Node.js `v24.16.0` 后 A/B probe 仍发现相同格式集合且 stderr 为 0 bytes。
- X 产物通过 H.264 MP4 的 ffprobe、SHA-256 和完整解码验证。该源的候选格式均无音频，现有 verifier 允许合法 silent video；应用规范化后的 canonical status URL 同样探测成功，且状态 ID 与提取媒体 ID 不同，不能合并建模。
- 新增强类型 `YtDlpJsRuntimeName` / `YtDlpJsRuntime`，只接受 `deno`、`node`、`bun`、`quickjs` 和绝对、规范化、普通且非 symlink/reparse 的 executable。candidate CLI 新增可选 `--js-runtime NAME:ABSOLUTE_EXECUTABLE`；未配置时继续 `--no-js-runtimes`，配置时先 clear 再只启用该 runtime，并继续 `--no-remote-components`。
- yt-dlp probe/download 固定增加 `--encoding utf-8`，修复含中文工作区路径的子进程文本乱码；版本命令保持最小参数。此次真实下载的原始日志是接线前的本机证据，UTF-8 修复由命令契约回归覆盖，尚未由完整 candidate Worker 实网重跑。
- 单 URL 测试确认额外使用 `--max-downloads 1` 会在媒体完整下载后返回 `101`；当前 candidate 固定命令没有该参数，继续依赖 `--no-playlist`。不能把这个实验性 `101` 当成候选 Worker 已出现的故障。
- 权威汇总见 [Iteration 0.8.1 live platform evidence](validation/iteration-0.8.1-live-platform-evidence.md)。gitignored 原始证据分别位于 `validation/local/live-platform-20260903/youtube/`（`RUN.md`、probe/download/ffprobe/decode 日志与 WebM）和 `validation/local/live-platform-20260903/x/`（`RESULT.md`、probe/download/ffprobe/decode 日志与 MP4）。
- 准确边界：这只证明上述两个精确 URL 在本机一次性工具路径可下载；未经过前端/API 队列、Worker claim/heartbeat/retry、受控 egress、AssetStore staging/commit intent、manifest/sidecar 或运行日志 E2E，不升级 `platform_download_verified`，也不代表 YouTube/X 的其他内容可用。

### 3.9 Iteration 0.9.0：Windows 本机 Worker、资产访问与双样本全链路

- 新增 `video-download-local-worker`，将既有真实 `YtDlpAdapter`、`FfprobeVerifier`、Worker 编排与 `AssetStore` 接到 Windows 本机控制数据库。入口默认关闭，只在 Windows、`VDC_ENABLE_LOCAL_REAL_WORKER=1`、命令行显式 `--allow-direct-network`、现有 Schema 8 数据库 ready、固定工具链与离线 smoke 全部通过时启动；同一数据根以 Windows byte-range lock 限制为一个本机 Worker。该模式使用宿主机直连网络，不声称具备 Linux namespace/UDS/egress 隔离。
- API 新增/闭合 `GET /api/v1/batches`、`GET /api/v1/batches/{batch_id}/assets` 与 `GET /api/v1/assets/{asset_id}/download`。只列出数据库登记的 ready 原件元数据和下载 URL，不返回本机路径；下载只接受规范 asset UUID 与仍满足数据根/登记 size 边界的原件。前端可刷新/打开最近批次，并在 `ready`/`partial_success` 时显示真实成品下载链接。
- 本机 Worker 复用既有 allowlist JSONL，但组件固定为 `local-worker`；启动、工具检查、claim、phase、受控子进程、terminal result 与停止事件均进入 `${VDC_DATA_ROOT}/logs/`，并可由既有运行日志 API/UI 汇总读取。URL、argv、绝对路径与 stdout/stderr 继续不进入普通日志。
- 最终 v3 双样本 E2E 使用用户指定的 YouTube/X 公开样本；公开记录省略精确 URL 和 batch/job/asset 标识。两个 Job 均由 `local-worker` claim，经过 `probing → downloading → verifying → committing` 并以 `ready` 结束，Batch 最终为 `ready`，得到两个不可变资产及各自 manifest/sidecar/thumbnail。
- 两个资产的原件、缩略图与 API 下载副本均完成 size/SHA-256 对照，且与 DB/manifest 一致；YouTube AV1 + Opus、X H.264 无声视频的完整流解码均 clean。具体媒体指纹与运行时标识仅保留在 gitignored 本机证据中。
- 浏览器从最近批次打开该 Batch，看到 `ready`、`2/2 ready` 与两个下载链接；页面操作、资产列表和实际下载均正常，验收期间浏览器控制台无 error/warn。gitignored 原始状态、资产、API 下载与运行日志位于 `validation/local/local-worker-e2e-20260903-v3/`；可提交汇总见 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md)。
- 准确边界：该 E2E 取代的是 0.8.1 “仅一次性工具调用”的缺口，不升级 `network_download_enabled`、`isolated_worker_ready` 或 `platform_download_verified`。它仍只证明两个精确样本在当前 Windows 本机直连模式可用；X graph-v2 保持关闭，Linux/Docker/Cookie/Stage 0 均未完成。0.9.1 后项目源码已获 Apache-2.0 授权，但第三方 binary/container/tool-bundle 门禁仍未完成。

## 4. 已执行验证

2026-09-03，Windows / Python 3.12.13 / uv 0.11.25。0.8.0–0.9.0 的历史基线保留；0.9.1 用独立版本承载 Apache-2.0 迁移，避免复用 proprietary 0.9.0 的制品版本号：

| 检查 | 结果 |
|---|---|
| 0.8.0 全套 pytest 历史基线 | `846 passed, 8 skipped` |
| 0.8.1 全套 pytest 历史基线 | `853 passed, 8 skipped in 47.79s`；该版本构建、隔离 wheel 安装和本机服务复验均已完成 |
| 0.9.0 全套 pytest 最终结果 | `881 passed, 8 skipped in 49.45s`；`compileall` 与 `uv lock --check --offline` 同步通过 |
| 0.9.1 Apache 迁移后全套 pytest | `882 passed, 8 skipped in 50.46s`；`compileall` 与 `uv lock --check --offline` 通过 |
| 0.9.0 本机 Worker/API/UI/日志 | Windows-only direct Worker 的双重显式启用、Schema/toolchain/logger/singleton lock、claim/phase/terminal；最近批次、ready asset list/download、路径/identity/size 拒绝；`local-worker` 日志读取均已纳入当前测试集 |
| 最终 v3 双样本 E2E | 两个 Job 均被真实 Worker claim 并 `ready`；输入 URL、运行时 UUID 与媒体指纹不进入公开记录，完整值保留在 gitignored 本机证据中 |
| 资产 API 与完整解码 | 两个 `/api/v1/assets/{asset_id}/download` 均返回完整原件；下载文件 size/SHA-256 与 DB/manifest 一致，两个文件完整流解码 clean |
| 浏览器使用验收 | 最近批次可打开，页面显示 `ready`、`2/2 ready` 与两个下载链接；实际下载可用，控制台无 error/warn |
| 0.9.0 package 历史 metadata | 当时的 sdist/wheel 为 `0.9.0`、`LicenseRef-Proprietary`、11 个 console scripts、31 个 legal files；不得作为当前 Apache 发布包 |
| 0.9.1 Apache package | sdist/wheel 独立构建并隔离安装通过；metadata `0.9.1`、`Apache-2.0`、11 个 console scripts、32 个 legal files；不含日志、数据库、媒体、`runtime-tools` 或 `validation/local` |

8 个 skip 均为明示环境边界：1 个 POSIX Cookie directory-FD cleanup、4 个 root POSIX/getfacl 的 0/1/3/4 source metadata contract、1 个真实 AF_UNIX roundtrip、2 个 POSIX path-swap/permission test。必须在目标 Linux 重跑，不得当作通过。

0.9.0 最终 v3 的 gitignored 原始数据库、资产/manifest、API 下载和 JSONL 位于 `validation/local/local-worker-e2e-20260903-v3/`，可提交汇总见 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md)。0.8.1 的一次性工具样本见 [Iteration 0.8.1 live platform evidence](validation/iteration-0.8.1-live-platform-evidence.md)；[Iteration 0.8.0 local toolchain evidence](validation/iteration-0.8.0-local-toolchain-evidence.md)、[Iteration 0.7.2 runtime logging evidence](validation/iteration-0.7.2-runtime-logging-evidence.md) 与 0.7.1 的 [debug/use/license evidence](validation/iteration-0.7.1-debug-use-license-evidence.md) 保留为历史基线。`dist/` 中的 0.9.0 及更早制品都是 proprietary 历史包，不得发布或使用通配符上传；当前 Apache-2.0 构建必须使用 0.9.1，并在 gitignored 独立目录验证。

明确未执行：Docker build/pull/up、Linux namespace/UDS/ACL/resource/core-pattern/runtime bind、真实 Cookie 流程、完整 Stage 0、X exact selector、Linux/NAS 恢复验收，以及任何第三方 binary/container/tool-bundle 发布。项目自有源码已获 Apache-2.0 授权；除上述两个 Windows 本机端到端样本外，没有执行其他真实平台请求，不得把双样本成功外推为平台级兼容性。

## 5. 继续工作入口

本地开发：

```powershell
uv sync --extra dev
uv run pytest -q
uv run video-download-control
```

依赖变更只在受审阅分支刷新，随后检查 lock diff、target wheel availability、hash/publisher/provenance 与 license：

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

默认地址 `http://127.0.0.1:8000`；不自动读 `.env`。普通 offline fake 只验证 flat-v1，必须保持 `VDC_ENABLE_X_GRAPH_V2=0` 和 `VDC_ENABLE_SHORT_LINK_RESOLUTION=0`。

Windows 本机工具验收入口：

```powershell
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
uv run video-download-tools verify --tool-root $ToolRoot
uv run video-download-tools smoke --tool-root $ToolRoot
uv run video-download-tools status --tool-root $ToolRoot
$env:VDC_TOOL_ROOT = $ToolRoot
uv run video-download-control
```

页面中的工具 `ready` 只证明固定工具完整且离线 smoke 通过；`local_direct_worker_available=true` 只表示当前 Windows 主机具备显式启动入口。控制面不会猜测外部 Worker 进程是否存活，三项 `network_download_enabled` / `isolated_worker_ready` / `platform_download_verified` gate 仍应保持 `false`。

Windows 本机实际下载必须同时运行控制面和独立 Worker。终端 A：

```powershell
$DataRoot = (Join-Path (Resolve-Path '.').Path 'data')
$ToolRoot = (Resolve-Path '.\runtime-tools\windows-x64').Path
$env:VDC_DATA_ROOT = $DataRoot
$env:VDC_TOOL_ROOT = $ToolRoot
$env:VDC_ENABLE_X_GRAPH_V2 = '0'
uv run video-download-control
```

终端 B：

```powershell
$DataRoot = (Join-Path (Resolve-Path '.').Path 'data')
$ToolRoot = (Resolve-Path '.\runtime-tools\windows-x64').Path
$Node = (Get-Command node.exe).Source
$env:VDC_ENABLE_LOCAL_REAL_WORKER = '1'
uv run video-download-local-worker `
  --data-root $DataRoot `
  --tool-root $ToolRoot `
  --js-runtime "node:$Node" `
  --allow-direct-network `
  --poll-interval-seconds 2
```

随后打开 `http://127.0.0.1:8000`。页面可提交批次、打开“最近批次”、观察 terminal 状态，并在 `ready`/`partial_success` 批次的“可下载成品”区域直接取回文件；“运行日志”可检查 control 与 `local-worker` 近期事件。两个进程均用 `Ctrl+C` 正常停止。本机 Worker 是 direct-network 模式，不等同于隔离 deployment Worker。

部署前必读：[Deployment candidate](deployment/README.md)、[Runbook](docs/RUNBOOK.md)、[Linux/Docker checklist](validation/linux-docker-acceptance.md)。full acceptance 必须由 clean trusted root launcher 以绝对路径、清空环境和 `bash -p` 启动，只使用 synthetic Cookie source 和 deny-only `replace.invalid` policy。授权 token、owner/mode/ACL、保留证据和清理边界以 checklist 为准。

## 6. 未完成项与残余风险

1. Stage 0 未执行；YouTube/X 各一个明确样本的 Windows 本机全链路成功仍不足以完成平台认证，四平台继续为 `candidate`，X graph-v2 另外保持 `disabled`。
2. Cookie override 只是 service-level isolation：单个被攻陷的 Worker/downloader 可读全部平台 source。真凭据前必须改为 credential sidecar、per-platform Worker 或 per-Attempt mount namespace。
3. DNS/replay OS blocking I/O 不可强制取消；slot 有界并 fail-fast，但 replay root 必须是可靠本地文件系统，更强保证需 process isolation。injected resolver 仍须遵守 timeout contract。
4. short-link egress 还没进 Compose/supervisor；真实 POSIX owner/mode、TLS/SNI、DNS rebinding、restart/replay 和 redirect chain 仍是 target gate。
5. acceptance 依赖 clean root launcher、reviewed/exclusive checkout/build context、local Docker/BuildKit 和无 physical alias/pre-existing bind mount。Unix Docker socket 不能单独证明 daemon/build/mount namespace 同机；Cookie root 的 host `nodev,nosuid,noexec` mount flags 也是 operator prerequisite，runner/YAML 当前不证明。
6. 外部 syntax tag 已移除，但目标 daemon 自带 frontend/BuildKit 的版本、配置以及 Dockerfile 1.3+ `RUN --network=none` 实际执行仍未在 Linux 验证。Base/tool registry availability、target manifests/wheels、image/package hashes 与 provenance 仍须批准；digest/hash 字面量本身不完成供应链审计。
7. Frozen Compose 文件正常退出只在 identity/snapshot 未变时删除；crash 可能在 private env 目录遗留敏感 config，必须按 exact path/device/inode 审计后定点清理，禁止 glob/递归删除。
8. Host pipe `core_pattern` 会使 `RLIMIT_CORE=0` 不足以排除 crash capture；runner 已 fail closed，但目标 host 尚未验证。
9. yt-dlp 的 remote components 继续禁用；显式单一 JS runtime 接线已实现，本轮 YouTube 既完成 Node.js A/B 探测，也在最终本机 Worker E2E 中显式使用 Node。Node.js 尚未进入 tool bundle lock，未配置时仍默认 `--no-js-runtimes`，因此不能把单样本结果外推为 YouTube 全站兼容。
10. 控制面与 ready 资产下载 API 无认证，只允许 loopback；不得直接监听 LAN/公网。本机 direct Worker 使用宿主机直连网络，双重显式开关与单实例锁只减少误启动/并发冲突，不提供 Linux 隔离 Worker 的网络边界。
11. control 与 `local-worker` 日志已统一到本机 best-effort JSONL，但仍无集中采集、告警、反代、存储配额或灾难切换；日志不能替代 SQLite、manifest、备份和外部监控。
12. 首次公开 Git 提交使用 GitHub `noreply` 作者身份，避免把本机真实邮箱写入永久历史；远端已有历史必须线性保留，禁止 force push。
13. 项目权利人已明确授予 Apache-2.0，版权声明为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。这只闭合项目自有源码、文档和脚本的公开许可，不重新许可任何第三方材料。
14. `pydantic-core` 的原生 Rust 闭包必须按实际 target/architecture 生成 Cargo SBOM、依赖映射和法律文件包；Linux amd64 调查不能外推到 Windows、macOS、ARM 或其他目标。
15. Python base image 必须按目标平台的 OCI child manifest 审计 OS packages 与许可证；multi-arch index digest 和上游 `NOASSERTION` SBOM 不能代替这一工作。
16. Windows x64 yt-dlp/FFmpeg/ffprobe 本机 bundle 已安装、hash/version/configuration 校验并完成离线 smoke，两个精确样本也已经过 Worker/AssetStore/manifest/API/UI 全链路；但对应源码/build closure、完整 SBOM、目标架构法律文本和人工批准仍未闭合，故该第三方工具包及包含它的容器/二进制再分发继续 blocked。detached signature 目前只保留，未完成密码学验签。
17. 0.9.0 最终回归为 `881 passed, 8 skipped`；sdist/wheel、隔离安装、31 个法律文件、11 个命令入口与包外 SHA-256 已复验。`pip-audit` 只是 2026-09-03 的时间点扫描；Bandit 仍保留已逐项审阅的 low 告警，全库默认 Ruff 仍有 91 条非门禁历史建议。
18. Candidate tool validator 现在会实际读取 `sources/` 下的 source artifact，拒绝缺失、路径越界、目录、symlink/reparse、hardlink、空文件和 SHA-256 不符；但其 SBOM 检查仍只验证受 hash 约束的非空 JSON 形状，不能替代组件语义审计或法律批准。
19. 0.9.1 Apache 迁移后全量回归为 `882 passed, 8 skipped`；独立 sdist/wheel 构建、隔离安装、`Apache-2.0` metadata、32 个法律文件与 11 个命令入口已复验。构建只写入 gitignored 验收目录，不覆盖 proprietary 0.9.0 制品。

## 7. Iteration 0.9.1 收尾与后续精确入口

1. 保留 `validation/local/local-worker-e2e-20260903-v3/` 的 exact batch、SQLite、manifest、API 下载与 JSONL，并保持 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md) 与最终制品状态一致；不要把 gitignored 原始媒体纳入可分发制品。
2. 在用户明确授权准备系统运行时后，于 clean target Linux/WSL 或 Docker 环境用 synthetic-only 输入先跑 preflight；修正环境后再由用户/运维明确授权 full mode，保留 JSONL、restore root 和 stale socket。
3. 在发布环境记录/验证 daemon-bundled Dockerfile frontend 与 BuildKit，证明所有 post-download `RUN --network=none` 生效；批准 base/tool registry manifests、target wheels 和包/镜像 provenance；固定 local Docker/BuildKit default context、exclusive checkout 和无 path alias/bind-mount 条件，并演练 image-ID 绑定、frozen Compose 正常删除与 crash-remnant exact-identity 清理。
4. 将 short-link egress 纳入 supervisor/隔离 topology，在 target POSIX 测 AF_UNIX、mode/owner、restart/replay、TLS/SNI、DNS rebinding、SIGTERM/stale socket 和最坏延迟。
5. 用 credential sidecar/per-platform Worker/per-Attempt namespace 将 Cookie source exposure 缩到一个平台/Attempt；先用 synthetic file 验证 rotation/durability。
6. 只在用户确认授权样本后进 Stage 0；每个 evidence identity 完成 10+ 正向、独立负向和连续三轮。X 只在真实 stable key/exact-one-selector 通过后才考虑开 graph gate。
7. 项目自有源码已按 Apache-2.0 公开；如需发布 dependency wheelhouse、冻结可执行文件、OCI/container image 或 tool bundle，仍须按实际目标架构完成 `pydantic-core` Cargo closure、Python base OS/OCI child image 与精确 tool bundle 的 SBOM/法律文件/源码义务审计，并对 source artifact 与 SBOM 做语义核验，通过第三方发布门禁后才允许交付。

## 8. 迭代历史

- **0.1 — 2026-09-02**：FastAPI/SQLite、MVP URL、Batch/Input/Source/Job；`18 passed`。
- **0.2 — 2026-09-03**：migration、lease/Attempt/retry/cancel、AssetStore 和 fake E2E；`75 passed`。
- **0.3 — 2026-09-03**：v0.3.0 / Schema 6；intent/crash recovery、queue/circuit、proxy/relay/guard、ffprobe/yt-dlp 候选；`383 passed`。
- **0.4 — 2026-09-03**：v0.4.0；sidecar 资产、candidate Worker、Cookie copier、short-link resolver、Compose 骨架；`459 passed`。
- **0.5 — 2026-09-03**：v0.5.0 / Schema 7；CredentialProfile、backup/restore 和 ADR-0001；`501 passed`。
- **0.6 — 2026-09-03**：v0.6.0 / Schema 8；X attachment graph、fan-out、exact child fake、generation/partial success/cancel/rediscover、语义恢复；`615 passed`。
- **0.7 — 2026-09-03**：v0.7.0 / Schema 8；authenticated/replay-safe short-link UDS transport、remaining Batch budget、resolver persistence boundary；deployment-owned Cookie wrapper/validator + descriptor copier + durable rotation contract；exact/clean/fail-closed Linux runner；`735 passed, 8 skipped`。Docker/Linux/真实 Cookie/平台/Stage 0 均未执行。
- **0.7.1 — 2026-09-03**：v0.7.1 / Schema 8；修复前端脚本转义、错误处理、轮询竞态、双输入拒绝、按钮恢复、repository clock 与同毫秒 FIFO；完成 synthetic 浏览器/fake Worker/资产/备份恢复验收及许可证 fail-closed 整改；`750 passed, 8 skipped`。项目仍 proprietary，公开/容器发布保持 blocked。
- **0.7.2 — 2026-09-03**：v0.7.2 / Schema 8；新增控制端、Worker、candidate Worker、子进程的 allowlist 脱敏 JSONL、轮转、读取 API、前端面板与备份排除；完成 synthetic 浏览器、日志泄漏标记、顺序/关联、备份恢复和发布包回归；`788 passed, 8 skipped`。项目仍 proprietary，使用需权利人身份或书面授权，公开/容器发布保持 blocked。
- **0.8.0 — 2026-09-03**：v0.8.0 / Schema 8；接入固定 Windows x64 yt-dlp/FFmpeg/ffprobe、逐文件 hash 与 version/configuration 校验、离线 synthetic smoke、tools API/UI/脱敏日志和 zipimport candidate Worker 路径；`846 passed, 8 skipped`，隔离 wheel smoke r3 通过。工具 `ready` 不等于隔离 Worker/联网/平台验证，真实 standalone downloader 仍未完成；公开再分发保持 blocked。
- **0.8.1 — 2026-09-03**：v0.8.1 / Schema 8；对用户指定的 YouTube/X 各一个 URL 完成无 Cookie 一次性真实下载、ffprobe、SHA-256 与全流解码；新增受校验的可选显式单一 JS runtime 接线，未配置时维持 `--no-js-runtimes`，并为 yt-dlp probe/download 固定 UTF-8。`853 passed, 8 skipped`；0.8.1 sdist/wheel、隔离 wheel 安装、法律文件/命令入口、源码一致性和本机 0.8.1 服务复验均通过。该证据仍不是前端/API/candidate Worker/AssetStore E2E，Stage 0 与平台级验证未完成，公开再分发保持 blocked。
- **0.9.0 — 2026-09-03**：v0.9.0 / Schema 8；新增 Windows 本机 direct Worker、显式启用/直连确认/单实例锁，最近批次与 ready 资产列表/下载 API/UI，并把 `local-worker` 纳入结构化日志。最终 v3 完成用户指定 YouTube/X 双样本从 API 队列到 AssetStore/manifest 和 API/UI 下载的真实 E2E；输入 URL、运行时 UUID 与媒体指纹不进入公开记录，API 下载 hash 与本机 manifest 一致、完整解码 clean、浏览器无 error/warn。最终回归 `881 passed, 8 skipped`；0.9.0 sdist/wheel、隔离安装、11 个命令入口、31 个法律文件与包外哈希均通过。项目仍 proprietary，公开/容器/二进制再分发保持 blocked。
- **0.9.1 — 2026-09-03**：项目自有源码、文档与脚本迁移到 Apache-2.0，加入 `NOTICE` 与双版权人声明；公开隐私清理移除本机路径、位置时区、真实验收 URL/账号/运行 UUID/媒体指纹。版本独立于 proprietary 0.9.0 制品；全量回归 `882 passed, 8 skipped`，Apache sdist/wheel、隔离安装、32 个法律文件和 11 个入口通过；第三方 binary/container/tool-bundle gate 保持 blocked。
