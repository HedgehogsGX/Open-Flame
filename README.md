# Open-Flame — 多平台视频下载控制面

这是一个面向单机、单管理员、私有环境的媒体下载控制面。架构方向是“模块化单体控制面 + 独立 Worker + 可替换适配器 + 不可变媒体资产”。

> 当前为 **Iteration 0.9.1 / version 0.9.1**，数据库仍为 **Schema 8**。本版本将项目自有材料正式迁移到 Apache-2.0，同时保留 0.9.0 已接入的 Windows 本机直连 Worker、前端最近批次、成品列表/下载 API 与 local-worker 结构化日志。用户指定的 YouTube/X 双样本已通过“API 队列 → Worker → yt-dlp/FFmpeg/ffprobe → AssetStore/manifest → 前端/API 下载”的全链路实测；公开记录已省略精确输入与媒体指纹。该结果只证明这两个样本；Linux 隔离 Worker、Docker、Cookie 与完整 Stage 0 仍未执行，不能据此把整个平台标记为 `verified`。

## License status

项目自有源码、文档和脚本依据 [Apache License 2.0](LICENSE) 开源，版权声明为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`，归属信息见 [NOTICE](NOTICE)。第三方组件继续受各自许可证约束，项目的 Apache-2.0 授权不会重新许可 `licenses/python/` 中的第三方材料或外部工具；精确清单与边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。仓库源码和重新验证的 project-only Python sdist/wheel 可以按上述条款分发，但 dependency wheelhouse、冻结可执行文件、离线安装器、OCI/container image 以及 yt-dlp/FFmpeg `runtime-tools` 工具包仍须完成目标架构 SBOM、对应源码、notices、relinking 义务与人工复核后才能再分发。

## 当前状态

| 项目 | 当前状态 |
|---|---|
| Web UI / FastAPI 控制面 | 可在本机启动；无认证，代码强制绑定 loopback |
| SQLite | Schema 8；WAL、`busy_timeout=5000`、`synchronous=FULL`，启动时 forward-only 迁移与结构检查；旧 flat-v1 记录继续可读 |
| 下载 Worker | Windows 本机直连入口已接入，要求环境 gate + CLI 明示确认、固定工具链、共享现有数据库和单实例锁；离线 fake 与 Linux 隔离 candidate 入口保持独立 |
| 本机工具链 | Windows x64 固定 yt-dlp `2026.08.19`、FFmpeg/ffprobe `n9.0.1-6-g9d4ca21220-20260820`；逐文件校验并通过离线真实二进制 smoke；不加入系统 `PATH` |
| `yt-dlp` | 固定命令、zipimport 入口、本机/候选适配器已组装；可显式接入一个受校验的 Node/Deno/Bun/QuickJS，probe/download 强制 UTF-8；格式选择优先非 HLS 并保留有界回退 |
| 凭证 | 业务库不保存 Cookie 路径/内容，只以非秘密 opaque `secret_ref` 引用部署秘密；可选 Worker-only read-only Cookie override、metadata/ACL/overlap preflight 与 attempt-private copier 已离线/静态验证 |
| 备份/恢复 | 当前 create/restore 要求精确 Schema 8；Iteration 0.6 已用离线 graph 状态完成独立新根恢复与跨表语义篡改拒绝，仍不替代目标 Linux/NAS 恢复验收；Iteration 0.5 / Schema 7 记录保留为历史证据 |
| 运行日志 | 控制面、本机 Worker、离线 Worker 与 candidate Worker 写入有界、轮转、字段白名单的 JSONL；前端可统一查看最近事件；启动后写入仍为 best-effort |
| 平台能力 | X、YouTube、Bilibili、抖音仍为 `candidate`；YouTube/X 各一个授权单视频样本已通过本机 Worker 全链路，样本量不足以升级平台状态；X 多附件 graph-v2 仍受 ADR-0001 的 exact-selector / Stage 0 gate 阻断 |
| 短链 | `t.co`、`b23.tv`、`v.douyin.com` 已接默认关闭的 POSIX UDS/HMAC egress transport；离线恶意输入通过，真实 UDS/TLS/DNS/平台均未验收 |
| 部署 | 有默认不可运行的 Dockerfile/Compose、可选 Cookie override 和只读优先 Linux acceptance runner；未 build、pull、cold start 或执行 mutation mode |
| 本机外部工具证据 | gitignored 本机 `runtime-tools/windows-x64` 已安装并复验；工具仍不在系统 `PATH`；Docker/可用 WSL Linux runtime 仍不存在 |

当前实现包括：

- Web 提交页面、最近批次恢复入口、ready 成品链接和显式响应 DTO；JSON、TXT、CSV 一批最多 50 条输入。
- X 单帖、YouTube 单视频/Shorts、Bilibili 普通 BV/av、抖音单作品的 URL 提取、白名单校验、规范化和去重。
- `t.co`、`b23.tv`、`v.douyin.com` 有逐跳 HTTPS/DNS/peer/redirect 上限的 resolver，并通过 authenticated/replay-safe UDS transport 接到显式 feature gate；默认关闭时仍以 `short_link_resolution_required` 终止。`youtu.be/{id}` 不需要展开，可直接规范化。
- SQLite Schema 8 中的 Batch、InputRecord、SourceItem、SourceDiscovery、SourceRelation、DownloadJob、JobAttempt、MediaAsset、Artifact 等模型，以及租约、心跳、取消、过期恢复和有限重试。
- 默认关闭的 X graph-v2 编排：父 `discover` Job 只探测并原子 fan-out；有序成员写入不可变 discovery snapshot；每个附件使用独立 child `download` Job；Input/Batch 只按 active snapshot 聚合，混合 `ready` 与终止失败/取消得到 `partial_success`。input-level cancel 与 terminal-only rediscover API 已实现，rediscover 递增 `run_generation` 并重置该 generation 的 retry budget，同时保留 Job 的累计 `attempt_count` 和历史快照。
- 旧 flat-v1 多资产 Job、Asset 与 manifest 不会被 Schema 8 原地改写，迁移后继续可查询和读取。
- 总活动任务上限 2、单平台上限 1；持久化队列暂停、平台熔断、冷却后的单次 half-open probe 和人工 reset。
- 管理面凭证 profile：只接受 1–64 字符 opaque ref，可按平台向 queued Job 分配、清除或禁用；公开 Batch API 不接受凭证 ID。
- Worker 在 claim 事务内检查 profile 存在、平台匹配、未过期、未禁用且 ref 合法；失效时以 `authentication_required` 终止、不创建 Attempt、不调用 Adapter，并继续领取下一个有效 Job。
- 离线多资产的临时目录、staging、校验、SHA-256、`source.json`、`manifest.json`、提交意图和三阶段崩溃恢复；原件、缩略图和平台既有字幕以同一资产目录一次发布并在同一 SQL 事务登记。
- 带 SHA-256 manifest 的备份/恢复组件：使用 SQLite 一致快照，排除顶层 `logs`、`temporary` 与 `assets/.staging`，恢复到不存在的独立新根后复核 Schema、数据库和资产 manifest。
- JSON 指标、健康检查、字段白名单 JSONL 运行日志、只读成品列表/下载 API、Stage 0 脱敏证据评估工具，以及受控出站 proxy / Unix-domain-socket relay 的安全基础组件。`jobs` / queue depth 包含 parent `discover`，但 `platform_outcomes` 只统计 `job_kind=download`，不会把编排成功算成平台下载成功。
- deployment-owned Cookie source override、四平台 0–4 映射、完整祖先/ACL/路径重叠检查、Worker core-dump 禁用，以及默认只读且需要二次明确授权才执行变更的 Linux/Docker acceptance runner。
- [ADR-0001](docs/adr/0001-x-attachment-discovery.md) 的 Schema 8 graph-v2 编排已实现；真实 stable key 与 exact-selector 仍未经过 Stage 0，故真实 X graph 路由保持 gate 关闭。

准确的运维步骤与边界见 [Runbook](docs/RUNBOOK.md)；Apache-2.0 迁移与发布验收见 [0.9.1 许可证迁移证据](validation/apache-2.0-license-migration-evidence.md)，工具链基线见 [Iteration 0.8.0 本机工具链接入验收证据](validation/iteration-0.8.0-local-toolchain-evidence.md)，直接工具样本见 [Iteration 0.8.1 真实平台验收证据](validation/iteration-0.8.1-live-platform-evidence.md)，最终 Worker/UI 全链路见 [Iteration 0.9.0 本机 Worker 验收证据](validation/iteration-0.9.0-local-worker-e2e-evidence.md)，累计状态与未完成项见 [HANDOFF.md](HANDOFF.md)。

## 本地启动

需要 Python 3.12 或更高版本，推荐使用 `uv`：

```powershell
uv sync --extra dev
uv run pytest -q
uv run video-download-control
```

浏览器打开 `http://127.0.0.1:8000`；页面可提交 URL、重新打开最近批次、查看成品下载链接，并手动刷新工具链与运行日志。机器可读 API 契约位于 `http://127.0.0.1:8000/openapi.json`。默认关闭 FastAPI 的 `/docs` 与 `/redoc`。首次启动会创建或 forward-only 迁移数据库到 Schema 8。默认数据库与运行数据写入 `./data`，结构化日志写入 `./data/logs`；该数据根不会提交到 Git。

控制面当前没有认证，且只接受 `127.0.0.1`、`::1` 或 `localhost` 绑定。不要用它直接监听局域网或公网地址。

### Windows 本机工具链接入

`video-download-tools` 只安装项目锁定的 Windows x64 工具，不写系统目录、不修改 `PATH`、不访问任何媒体平台。默认从固定 GitHub release URL 下载；若已有逐项匹配的私有缓存，可用 `--artifact-cache`。目标目录必须是尚不存在的绝对路径，安装器拒绝覆盖：

```powershell
$ToolRoot = "C:\vdc-runtime-tools\windows-x64"
uv run video-download-tools install --tool-root $ToolRoot
uv run video-download-tools verify --tool-root $ToolRoot
uv run video-download-tools smoke --tool-root $ToolRoot
$env:VDC_TOOL_ROOT = $ToolRoot
uv run video-download-control
```

`install` 会先在目标同级的随机 staging 目录中下载/复制、核对精确大小与 SHA-256，只提取锁定的 archive members，保留 yt-dlp release checksum/signature 与许可文本，执行真实二进制版本/configuration 检查，再用本机生成的 1 秒音视频做 FFmpeg→ffprobe 离线闭环；全部成功才原子发布。`status` 只检查文件和已完成 smoke marker，不执行平台请求。

状态 `ready` 的含义严格限定为“本机工具完整且离线 smoke 通过”。API 中 `network_download_enabled`、`isolated_worker_ready`、`platform_download_verified` 仍全部为 `false`，另以 `local_direct_worker_available` 表示 Windows 本机入口是否可显式启动；控制面不会把外部 Worker 的进程存活状态猜成事实。

### Windows 本机下载 Worker

先在终端 A 启动控制面并让它初始化数据库：

```powershell
$DataRoot = (Join-Path (Resolve-Path '.').Path 'data')
$ToolRoot = (Resolve-Path '.\runtime-tools\windows-x64').Path
$env:VDC_DATA_ROOT = $DataRoot
$env:VDC_TOOL_ROOT = $ToolRoot
$env:VDC_ENABLE_X_GRAPH_V2 = '0'
uv run video-download-control
```

再在终端 B 启动常驻本机 Worker；Node 参数是本轮 YouTube 验收使用的显式 runtime：

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

两个进程都用 `Ctrl+C` 正常停止。Worker 默认不开启；环境变量必须精确为 `1`，同时还要传 `--allow-direct-network`。它只接受已由控制面初始化且通过 Schema 8 readiness 的数据库，同一数据根只允许一个本机 Worker。graph-v2 discover/attachment 任务会在同一 claim 事务内被跳过而不会被错误终止；本机使用仍应保持 `VDC_ENABLE_X_GRAPH_V2=0`。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VDC_HOST` | `127.0.0.1` | 必须是 loopback host |
| `VDC_PORT` | `8000` | HTTP 端口 |
| `VDC_DATA_ROOT` | `./data` | 资产、临时文件和默认数据库根目录 |
| `VDC_DATABASE_PATH` | `${VDC_DATA_ROOT}/control.sqlite3` | SQLite 文件；可显式放在其他本机路径 |
| `VDC_MAX_BATCH_URLS` | `50` | 批次上限；当前产品边界为 1–50 |
| `VDC_ROUTE_POLICY_VERSION` | `mvp-v1` | 写入任务的路由策略版本 |
| `VDC_ENABLE_X_GRAPH_V2` | `0` | 仅控制新提交的 X 单帖是否创建 graph-v2 parent `discover` Job；真实 `YtDlpAdapter` 不支持 exact selector，当前运维环境必须保持 `0` |
| `VDC_ENABLE_SHORT_LINK_RESOLUTION` | `0` | 仅在 POSIX、可信 egress 服务与私有 UDS/key 均已准备时设为 `1`；只配置路径不会自动启用 |
| `VDC_SHORT_LINK_TRANSPORT_SOCKET` | 未设置 | 短链 egress 服务创建的绝对、规范化、同 UID 私有 Unix socket |
| `VDC_SHORT_LINK_ATTESTATION_KEY_FILE` | 未设置 | 控制面与 egress 服务共同读取的绝对、规范化、同 UID 私有 HMAC key 文件 |
| `VDC_STORAGE_MIN_FREE_BYTES` | `1073741824` | 低于该剩余空间时拒绝恢复持久化队列 |
| `VDC_RUNTIME_LOG_LEVEL` | `INFO` | 运行日志最低级别：`DEBUG`、`INFO`、`WARNING` 或 `ERROR` |
| `VDC_RUNTIME_LOG_MAX_BYTES` | `10485760` | 每个组件 active JSONL 文件触发轮转前的最大字节数，范围 1 KiB–1 GiB |
| `VDC_RUNTIME_LOG_BACKUP_COUNT` | `5` | 每个组件保留的轮转文件数，范围 1–20 |
| `VDC_TOOL_ROOT` | 未设置 | 固定本机工具链的绝对、规范化目录；只显示/记录完整性状态，不会启用联网 Worker |
| `VDC_ENABLE_LOCAL_REAL_WORKER` | `0` | Windows 本机直连 Worker gate；只有精确值 `1` 加 CLI 明示确认才允许启动 |

[`.env.example`](.env.example) 只是配置参考；程序当前不会自动读取 `.env` 文件，必须通过 shell 或服务管理器注入环境变量。

## API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | 数据库、Schema 与队列运行状态；降级时仍返回状态体 |
| `GET` | `/health/live` | 进程存活检查 |
| `GET` | `/health/ready` | 就绪检查；数据库异常或队列暂停时返回 503 |
| `POST` | `/api/v1/batches` | 提交 1–50 条 URL 或分享文本 |
| `POST` | `/api/v1/batches/import?filename=...&name=...` | 导入 UTF-8 `.txt` / `.csv` 请求体，最大 256 KiB |
| `GET` | `/api/v1/batches?limit=50` | 列出批次，`limit` 范围 1–100 |
| `GET` | `/api/v1/batches/{batch_id}` | 查询批次、输入和任务 |
| `GET` | `/api/v1/batches/{batch_id}/assets` | 列出该批次已 ready 的成品元数据和下载 URL，不暴露本机路径 |
| `GET` | `/api/v1/assets/{asset_id}/download` | 下载数据库登记且仍满足本机文件边界的 ready 原件 |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | 取消 queued 任务或请求活动 Worker 协作取消 |
| `POST` | `/api/v1/inputs/{input_id}/cancel` | 原子取消该 Input 的非终态 parent/child 工作；已 ready child 保留 |
| `POST` | `/api/v1/inputs/{input_id}/rediscover` | 仅 graph-v2 且当前所有工作终态时进入下一 `run_generation`；否则返回 409 |
| `GET` | `/api/v1/metrics` | 队列、Attempt 时延、磁盘、平台结果、错误与熔断 JSON；平台结果排除 parent `discover` |
| `GET` | `/api/v1/operations/queue` | 查询持久化队列暂停状态 |
| `GET` | `/api/v1/operations/logs?limit=100` | 查询日志管线状态与最近脱敏事件，`limit` 范围 1–500 |
| `GET` | `/api/v1/operations/tools` | 校验本机工具链并显示固定版本、离线 smoke、隔离 Worker/平台验收和本机第三方工具包再分发状态 |
| `POST` | `/api/v1/operations/queue/resume` | 磁盘恢复到配置水位后恢复队列 |
| `GET` | `/api/v1/platform-circuits` | 查询平台熔断状态 |
| `POST` | `/api/v1/platform-circuits/{platform}/reset` | 人工复位 `x`、`youtube`、`bilibili` 或 `douyin` |

提交示例：

```json
{
  "name": "示例批次",
  "inputs": [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://x.com/example/status/1234567890"
  ]
}
```

创建批次只会生成 queued 任务；控制面本身不会联网下载媒体。只有另行显式启动的本机 Worker 或部署 candidate Worker 才会消费队列。

Batch 请求使用 `extra="forbid"`，显式拒绝 `credential_profile_id`。凭证分配只能通过本机管理员 CLI 执行，避免公开提交面扩大权限或将单个 profile 误用到混合平台批次。

## 运行日志与排障

程序默认在 `${VDC_DATA_ROOT}/logs/` 写入 UTF-8 JSON Lines。控制面、本机 Worker、离线 Worker 与 candidate Worker 使用独立组件日志；每条事件包含 UTC 时间、component、run ID、event ID 和按场景允许的 request/job/attempt/error 等关联字段。默认每个 active 文件到 10 MiB 时轮转并保留 5 份历史文件。原始 Uvicorn access log 已关闭，避免导入文件名、批次名称等 query 出现在第二套未脱敏访问日志中。

日志边界采用字段白名单，不记录 raw/submitted/canonical URL、query、请求/响应 body、HTTP headers、Cookie、credential ref、短链签名、subprocess argv/env/cwd/stdout/stderr、绝对路径或任意异常消息。不要为了获得“更多细节”把这些内容手工写进日志或提交支持材料。日志文件仍是本机明文运维数据，应只授予当前管理员访问权。

排障时按以下顺序确认：

1. 在前端“运行日志”区域点击“手动刷新”，先看 `status`、`write_failures`、`rejected_events` 和 `last_failure_code`。
2. 用 `run_id`、`request_id`、`job_id`、`attempt_id` 关联最近事件，优先定位第一个稳定 `error_code` 或状态转折。
3. 同时核对 `/health`、`/api/v1/metrics`、Batch/Job 当前状态及已发布资产 manifest；日志不能覆盖或改写这些业务事实。
4. 若日志状态为 `degraded` / `error`，检查 `${VDC_DATA_ROOT}/logs` 的容量、目录类型和访问权限；修复后再次手动刷新，不能仅凭日志恢复就恢复队列或复位平台熔断。

运行日志是 best-effort、会轮转、可缺失且并非不可篡改，只用于快速定位代码路径。它不替代 SQLite `job_attempts`、资产 SHA-256/manifest、备份 manifest、Stage 0 证据或正式审计记录。顶层 `logs/` 被业务备份精确排除；需要留存排障材料时，应在确认脱敏与访问范围后另行受控复制。

## 离线 Worker QA

离线 fake Worker 会消费任务并生成 `.fake` 文件，只用于验证队列与资产提交，不是任何平台的下载证据。它强制要求显式、独立的数据根目录，避免污染普通数据。

先在终端 A 用隔离目录启动控制面并通过页面提交测试 URL：

```powershell
$QaRoot = Join-Path $env:TEMP "vdc-offline-qa"
$env:VDC_DATA_ROOT = $QaRoot
$env:VDC_DATABASE_PATH = Join-Path $QaRoot "control.sqlite3"
uv run video-download-control
```

再在终端 B 清除控制面路径变量，并对同一 QA 根目录运行 fake Worker：

```powershell
$QaRoot = Join-Path $env:TEMP "vdc-offline-qa"
Remove-Item Env:VDC_DATA_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:VDC_DATABASE_PATH -ErrorAction SilentlyContinue
$env:VDC_ENABLE_OFFLINE_FAKE_WORKER = "1"
uv run video-download-worker --offline-fake --data-root $QaRoot --drain
Remove-Item Env:VDC_ENABLE_OFFLINE_FAKE_WORKER
```

如果 `--data-root` 与普通控制面数据路径重叠，Worker 会拒绝启动。这个旧入口省略 `--offline-fake` 也会安全退出；真实 Windows 本机下载必须改用上文单独的 `video-download-local-worker` 入口。

普通 `--offline-fake` 使用的不是 `ScriptedGraphFakeAdapter`，不具备 exact-selector contract。不要为了运行该命令而设置 `VDC_ENABLE_X_GRAPH_V2=1`；当前 graph-v2 证据来自测试内显式组装的离线 `ScriptedGraphFakeAdapter`，仓库没有对应的真实运维启用命令。

## CLI

- `video-download-control`：启动 loopback FastAPI 控制面。
- `video-download-tools`：`install` / `verify` / `smoke` / `status` 固定 Windows x64 本机工具链；不启用联网 Worker。
- `video-download-local-worker`：Windows 本机直连 Worker；要求显式 gate、CLI acknowledgement、固定工具链、现有控制数据库、强制日志首写与单实例锁。
- `video-download-worker`：仅显式离线 fake QA Worker。
- `video-download-candidate-worker`：默认禁用的真实 Worker 候选；要求 feature gate、Linux loopback-only namespace、UDS relay、绝对工具路径与精确 yt-dlp/FFmpeg/ffprobe 版本；可用 `--js-runtime NAME:ABSOLUTE_EXECUTABLE` 显式提供唯一 JavaScript runtime。
- `video-download-credentials`：管理员专用的 opaque profile CLI，支持 `register`、`list`、`assign`、`clear`、`disable`；要求现有普通文件数据库的显式绝对路径，从不接受 Cookie 路径或内容。
- `video-download-backup`：使用显式绝对路径执行 `create` 或 `restore`；备份和恢复目标必须是尚不存在、不重叠的独立根。
- `video-download-validation`：评估脱敏的 Stage 0 样本与结果 CSV。
- `video-download-egress-proxy`：在绝对路径 UDS 上启动显式 `--allowed-host` 或有界只读 `--allowed-host-file` 的受控 forward proxy，并把只含 reason 与 policy host 的稀疏 audit 以 JSON 写到 stderr。
- `video-download-unix-relay`：把数值 loopback TCP 端口转发到受保护的 UDS。
- `video-download-short-link-egress`：在 egress side 运行 authenticated single-hop HTTPS fetch 服务；只接收签名的 numeric-IP target，不自行 DNS 或自动跟随 redirect，SIGINT/SIGTERM 时按 inode identity 清理自己的 UDS。

候选 Worker/relay/proxy 已由 [`deployment/compose.candidate.yaml`](deployment/compose.candidate.yaml) 表达为静态拓扑；短链 egress CLI 尚未加入该 Compose topology，必须由经过审阅的 POSIX supervisor 放在实际 egress side。Compose 从未在当前机器运行；单独执行任一 CLI 也不会自动产生隔离。

### 短链可信展开候选

这条能力默认关闭。控制面逐跳解析 DNS、拒绝所有非公网地址并把完整 answer set 签名发送到私有 UDS；egress 服务只连接已批准的 numeric IP，同时保留原 hostname 做 TLS SNI/证书与 HTTP `Host`，再对实际 peer 做 attestation。响应不自动 redirect，只有有界 `Location` 回到控制面；body、任意响应头、异常原文和最终临时签名 URL不会进入 API、业务数据库、backup manifest 或普通 audit。每跳和整链都有硬上限，Batch 对全部唯一短链共享 15 秒 aggregate budget；内建 resolver 每次都收到当前剩余时间并压缩自身总时限，到期后不再开始新展开。自定义 injected resolver 是 trusted-contract boundary；同步 Python 调用无法强制抢占一个忽略 `timeout_seconds` 的实现。DNS 使用最多四个不可取消的 bounded worker slot；若底层 `getaddrinfo` 挂死，后续解析会 fail-fast，不能把这视为高可用 DNS 隔离。Replay 文件操作也从 event loop 卸载到有界 slot，但已进入 kernel/文件系统的 blocking I/O 不能强制取消。

启用需要两个服务以同一有效 UID 运行，并使用没有 symlink/reparse、owner/mode 合格的绝对路径。Socket parent 必须私有，socket 为 `0600`；key 是恰好读取的 32–4096 bytes 原始随机数据、单 hard link、同 UID 且无 group/other 权限；replay directory 为同 UID `0700` 且必须在可靠本地文件系统，不得放在可能无限阻塞的网络/FUSE share。先由 supervisor 启动 `video-download-short-link-egress --unix-socket ... --shared-key-file ... --replay-directory ...`，确认健康和退出清理，再向控制面同时注入三个环境变量。详细故障边界见 [Runbook](docs/RUNBOOK.md)。当前 Windows 构建对显式 gate fail closed，真实 AF_UNIX、POSIX mode/owner、TLS、DNS 和平台 redirect 必须在目标 Linux 另行验收。

### 管理员凭证流程

`register` 只把平台、名称、过期时间和 opaque `secret_ref` 写入数据库；Cookie source 路径和内容始终由部署环境保管。`assign --batch-id` 只选择与 profile 平台匹配的 queued Job；显式 `--job-id` 范围中只要存在跨平台或非 queued Job，就整体拒绝而不做部分分配。`clear` 只允许 queued Job；`disable` 禁用 profile 并为活动 Job 请求协作取消。

```powershell
$Database = (Resolve-Path -LiteralPath ".\data\control.sqlite3").Path
uv run video-download-credentials --database-path $Database register `
  --platform youtube --name "Primary" --secret-ref youtube-primary
uv run video-download-credentials --database-path $Database list
uv run video-download-credentials --database-path $Database assign `
  --profile-id REPLACE_WITH_PROFILE_ID --batch-id REPLACE_WITH_BATCH_ID
uv run video-download-credentials --database-path $Database clear `
  --job-id REPLACE_WITH_JOB_ID
uv run video-download-credentials --database-path $Database disable `
  --profile-id REPLACE_WITH_PROFILE_ID
```

### 备份与独立根恢复

```powershell
uv run video-download-backup create `
  --source-data-root C:\vdc\data `
  --source-database C:\vdc\data\control.sqlite3 `
  --backup-target D:\vdc-backups\backup-20260903

uv run video-download-backup restore `
  --backup-root D:\vdc-backups\backup-20260903 `
  --restore-data-root C:\vdc-restore-drill `
  --restore-database C:\vdc-restore-drill\control.sqlite3
```

当前 v0.9.1 备份/恢复 CLI 要求精确 Schema 8；graph snapshot、relation、generation 和 active-snapshot 指针作为 SQLite 一致快照的一部分保存，已发布不可变资产按 manifest 复制，顶层 `logs`、`temporary` 与 `assets/.staging` 排除。Iteration 0.6 已在 Windows 临时独立根实际恢复包含 generation 2、跨 Input ready-asset reuse 和 exact target 的离线 graph 数据，并验证即使重算外层 manifest/hash，active pointer、target、child identity 或 reuse 语义篡改仍会以不回显内部 key 的固定错误拒绝；命令与边界见 [Iteration 0.6 offline graph evidence](validation/iteration-0.6-graph-v2-offline-evidence.md)。[Iteration 0.5 recovery evidence](validation/backup-restore-drill-iteration-0.5.md) 仍是 Schema 7 历史记录。两者都不替代目标 Linux/Docker 主机、独立物理介质和实际故障流程上的运维验收。运行日志与部署所有的 Cookie source 都不在业务备份内，必须按各自的保留和秘密存储策略处理。

## 安全边界

- 入口仅接受 HTTP/HTTPS、已列出的平台域名、默认 scheme 端口，拒绝 URL 凭证和未支持链接类型；TikTok 不会被当作抖音。
- 控制面无认证且强制 loopback；尚无可直接部署的 HTTPS、用户认证、限流或配额层。
- 受控出站 proxy 要求显式非空 host allowlist，做 DNS A/AAAA 解析、非公网/映射/过渡地址阻断、数值 IP 连接与 peer 校验，并对请求、响应、连接数、字节数和时限设上限。
- 短链 control↔egress 协议使用 canonical JSON、domain-separated HMAC-SHA256、fresh nonce、短有效期、clock-skew 检查与 crash-durable replay marker；逐跳只允许目标平台的 HTTPS hostname，响应只披露 `Location`。这不等同于已在真实 Linux 网络路径验证。
- Linux 网络隔离 guard 与 TCP→UDS relay 能验证“Worker namespace 仅 loopback + 私有 Unix socket”；candidate Worker 在数据库初始化前及每次 claim 前复核，Compose 候选让 Worker/relay 共享 `network_mode:none` namespace。
- `yt-dlp` 候选命令固定禁用用户配置、插件目录、远程组件和 JS runtime，并限制输出目录、格式、大小、重试、Cookie 副本与 proxy；这仍不能替代容器/防火墙对直连网络的阻断。
- 业务数据库只保存 profile ID 与 opaque ref；claim 后 JobLease 仅向 `ProbeRequest` / `DownloadRequest` 传递 opaque ref，不持久化 Cookie 路径或内容。Cookie 源到 Attempt 私有 `0600` 副本的组件已测试路径、权限、identity、swap 与 fsync。当前 override 只是 service-level isolation：单个 Worker 仍能读整棵多平台 source root，真实凭据上线前需要 credential sidecar、per-platform Worker 或 per-attempt mount namespace。
- 代码中的凭证流已接通；credential-free base Compose 不含 Cookie，显式 override 才把 root-owned source/mapping 只读挂到 Worker。Host preflight 拒绝路径重叠、不安全祖先、named/default ACL、links、错误 owner/mode 与 race；full acceptance 还要求 host `/proc/sys/kernel/core_pattern` 可读且不是 pipe collector，因为 Worker `RLIMIT_CORE=0` 单独不能排除主机侧 crash capture。这些仍只有静态/Windows 离线证据。
- Linux runner 的 execute mode 还要求 effective root，并固定使用通过 Python 3.12+ 检查的 `/usr/bin/python3`。Docker endpoint 判定遵循 `DOCKER_CONTEXT` 高于 `DOCKER_HOST` 的官方 precedence；execute 拒绝 inherited Docker/Compose/BuildKit endpoint、config 与 project/profile 控制变量，default context 仍须解析为 local Unix Linux daemon。Fresh build tag 只作初始名称；runner 随即捕获并验证不可变 local `sha256:...` image ID，把该 ID 写进 effective Compose，再递归拒绝 `$` 并在 private env 同目录冻结为 `root:root 0600` 文件。二次渲染必须与原 JSON 深等值并再次通过完整校验；之后 Compose mutation 只用 frozen file，direct `docker run` 和 runtime `Image` inspect 均绑定同一 ID，checkpoint 也会重验它，避免 tag rebind 改变验收对象。正常退出只按 identity/snapshot 删除冻结文件，crash 残留必须在受保护目录人工定点审计。
- Dockerfile 不再引用外部 syntax image；两个 Python `FROM` 都硬编码同一 `python:3.12.13-slim-bookworm` digest，不能由 ARG 覆盖。`pyproject.toml`/`requirements.build.in` 精确固定 `hatchling==1.27.0`，`requirements.build.lock` 与 `requirements.runtime.lock` 固定 exact version + SHA-256。唯一允许 Python package 网络访问的是 `pip download --no-deps --only-binary=:all: --require-hashes`；后续 build-dependency install、project wheel build、runtime install 均 `RUN --network=none` + `--no-index`，项目 wheel 用 `--no-build-isolation --no-deps` 构建并按精确路径安装，最后 `pip check`。Runner 的 network-none runtime contract 还精确核对 13 个 runtime-lock distributions + `video-download-control==0.9.1`，并拒绝五个 build-only distributions 泄漏。Lock 可能同时列 wheel/sdist hashes，但 `--only-binary=:all:` 在命令层拒绝 sdist；刷新/审计命令见 [Deployment candidate](deployment/README.md#image-build-contract)。这些 target Linux 检查尚未执行，候选源码采用 Apache-2.0 也不代表构建出的第三方依赖或工具镜像已获再分发批准。
- `VDC_ENABLE_X_GRAPH_V2` 只改变新 X Input 的任务形状，不会让 adapter 获得 exact selector。当前真实 `YtDlpAdapter.supports_exact_selector=False`；即使误入队，Worker claim 也会 fail closed，因此不要把该保护当作启用方案。
- 下载输出、元数据和媒体文件始终视为不可信输入；不绕过 DRM、付费墙、验证码、地区或其他访问控制。

因此，proxy、relay、命令工厂或候选 adapter 的离线测试通过，不等于生产网络隔离已经成立，也不等于平台已经可用。Proxy 只能约束实际经过它的流量。

## Stage 0 验证

[`validation/`](validation/README.md) 包含样本/结果模板、明确标为 offline 的工程证据，以及两个用户指定样本的本机实测摘要。后者只证明精确样本，不构成 Stage 0 或整个平台支持证据。生成脱敏 Stage 0 报告：

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

只有同一 `platform × source_type × adapter × downloader_version × environment` 的至少 10 条公开正向样本、独立负向样本和连续三轮完整回归均满足门槛时，能力才可从 `candidate` 标为 `verified`。该 CLI 只评估记录，不会自行运行 `yt-dlp`。

## 已知未完成项

- Stage 0、四平台真实链接回归和任何 `verified` 能力均未完成。
- candidate Worker、Compose、Cookie override 与 Linux acceptance runner 只有离线/静态证据；镜像未构建，Linux namespace/UDS/ACL/core limit、non-pipe `core_pattern`、runtime read-only bind、immutable image-ID chain 与 frozen-config lifecycle 均未实跑，真实 Cookie source 未创建或挂载。Cookie source 所在 host filesystem 的 `nodev,nosuid,noexec` 是 operator prerequisite，当前 runner/YAML 不证明；单 Worker 对整棵 Cookie source root 的可读性仍是显式残余风险。发布还须验证目标 registry 的 base/tool manifests 与 platform artifacts 可用，审阅镜像/包/批准 hashes 的 provenance 与 target wheel availability，并在目标 daemon 自带 Dockerfile frontend/BuildKit 上证明 Dockerfile 1.3+ `RUN --network=none` 被支持和落实。移除外部 syntax tag 不等于这些 target-specific build inputs 已获验证。
- 备份/恢复 CLI 已更新为精确 Schema 8，并有 Iteration 0.6 离线 graph 独立根恢复与语义篡改拒绝证据；尚未在目标 Linux/NAS、真实容量、独立介质或灾难主机上执行运维验收。已有本地结构化排障日志，但仍无集中式生产日志管线、反向代理、认证或磁盘告警。
- 短链 resolver、可信 transport 与 gated 控制面接线已实现，但 gate 默认关闭；短链 egress 尚未集成到 Compose/supervisor，Linux AF_UNIX/owner/mode、真实 TLS/DNS/redirect 和整批最坏延迟未验收。Replay blocking I/O 只是有界卸载而非可强制取消；injected resolver 仍须遵守 timeout contract。
- Schema 8 X graph-v2 编排已经实现并由 `ScriptedGraphFakeAdapter` 离线验证，但真实 `YtDlpAdapter` 仍不支持 exact selector；`VDC_ENABLE_X_GRAPH_V2` 默认 `0`，真实 X graph 继续 `candidate/disabled`，不能标记为 `verified`。旧 flat-v1 数据仍可读。
- Caption 当前只记录 `origin=platform`，尚未可靠区分平台人工字幕与自动字幕；缩略图/字幕内容也尚无真实样本验证。
