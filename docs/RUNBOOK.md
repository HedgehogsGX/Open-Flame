# Open-Flame / Video Download Control v0.28.0 Runbook

> 当前版本：Iteration 0.28.0 / v0.28.0，下载数据库 Schema 11、独立编辑数据库 Schema 4、独立上传数据库 Schema 3、独立 Workflow Schema 3。编辑页支持版本化草稿、分段、封面、可选隔离 AI runtime、审核时间轴、字幕和标准音色配音；自动流程页把一个 URL 串接到下载、编辑、AI 与所选三平台上传草稿，并保留逐节点确认或预授权。上传首批为 Bilibili、抖音、视频号。Windows 仍为 direct/non-isolated；本地 runtime 完整性和 synthetic 回归不等于真实 OpenAI 可用、真实平台投稿、定时发布、免 Python EXE 或第三方再分发许可。`VDC_ENABLE_X_GRAPH_V2` 和通用控制面 `VDC_ENABLE_SHORT_LINK_RESOLUTION` 默认 `0`，真实 `YtDlpAdapter.supports_exact_selector=False`。

0.28.0 AI runtime、自动流程与本地验证边界见[本轮证据](../validation/iteration-0.28.0-ai-workflow-evidence.md)、[AI Runtime](AI_RUNTIME.md)和[编辑指南](EDITOR.md)；此前 [0.27.0 编辑工作台](../validation/iteration-0.27.0-editing-workspace-evidence.md)及更早记录保留各自冻结构建的历史范围。首次使用见 [安装与修复](WINDOWS_SETUP.md)，日常使用见 [Windows 启动器](WINDOWS_LAUNCHER.md)。上传另见 [上传指南](UPLOADER.md)、[运行环境](UPLOAD_RUNTIME.md)、[测试计划与构建身份](UPLOADER_TEST_PLAN.md)。重复批次不创建新下载，也不改变原任务的凭证。

0.24.3 的历史数据口径是“下载数据库 Schema 11、独立上传数据库 Schema 1”；0.25.0 使用上传 Schema 2，0.27.0 使用 Editing Schema 1，0.28.0 冻结版本先使用 Editing Schema 3 和 Workflow Schema 1。这些短语只用于识别旧记录。当前发布后源码将精确 Editing Schema 1/2/3 向前迁移到 Schema 4，并把精确 Workflow Schema 1 或 2 迁移到 Workflow Schema 3；Schema 1 会在同一事务内先重建合法单输出 `outputs_json`。上传库仍为 Schema 3，上传备份仍使用格式 2。不能用旧程序打开当前数据。

### 启动失败时

CLI 在 stderr 输出单行 JSON，并独立写入 `%LOCALAPPDATA%/Open-Flame/diagnostics/runtime-launch-diagnostics.jsonl`（256 KiB、3 份备份）。`invalid_arguments`、端口/工具/配置错误和依赖错误均提供固定码与处理建议。`diagnostic_status=saved/unavailable` 表示本次诊断是否写入；`log_status=not_started` 只表示业务运行日志尚未启动，两者不要混淆。未知故障仍为 `unknown`，可能发生在启动后，应同时查看业务运行 JSONL。

缺少 Python、缺少项目启动模块、诊断目录不可用或持续锁占用时不能保证保存，入口会明确提示未保存。失败窗口会保留；成功后按 Ctrl+C 正常停止。源码入口不依赖 Codex，但仍需已安装 `.venv` 和工具包，不是独立 EXE。

本文只覆盖当前仓库真实存在的能力。项目概览见 [README](../README.md)，迭代与剩余缺口见 [HANDOFF](../HANDOFF.md)，Stage 0 证据规则见 [validation/README](../validation/README.md)。

## 1. 当前运行边界

- 控制面无认证，启动安全检查只允许 `127.0.0.1`、`::1` 或 `localhost`。不得直接监听 LAN / WAN 地址。
- 普通 `video-download-worker` 仍只有 `--offline-fake` QA 模式。Windows 普通使用首选 `video-download-local-app`；它在内部受控设置本机 Worker gate，并要求 CLI 明示 `--allow-direct-network`。高级手动入口 `video-download-local-worker` 仍要求 `VDC_ENABLE_LOCAL_REAL_WORKER=1`、同一数据库和独立终端。`video-download-candidate-worker` 是另一条 Linux 隔离候选路径，若缺 feature gate、loopback-only namespace、UDS relay、绝对工具路径或精确版本会在 claim 前失败。
- [`deployment/`](../deployment/README.md) 含 Dockerfile/Compose、可选 Cookie override 与 acceptance runner，但默认镜像、feature gate、工具版本和出站策略都是不可运行占位值。
- 当前固定工具为 yt-dlp `2026.08.19`、FFmpeg/ffprobe `n9.0.1-11-ge47273f4d9-20260831`，安装到项目私有或 app root 的 `runtime-tools/windows-x64`，不进入系统 `PATH`。2026-09-03 使用的 `n9.0.1-6-g9d4ca21220-20260820` 仅是历史工具记录，不能作为当前安装输入。Docker/可用 WSL Linux runtime 与容器部署证据仍不存在。
- Iteration 0.8.1 的直接工具样本证据继续保留在 [Iteration 0.8.1 live-platform evidence](../validation/iteration-0.8.1-live-platform-evidence.md)。v0.9.0 又让同两个精确授权样本经过共享控制数据库的真实 Worker、AssetStore、`source.json`/`manifest.json` 与成品访问 API 完成端到端闭环；脱敏汇总见 [Iteration 0.9.0 local Worker evidence](../validation/iteration-0.9.0-local-worker-e2e-evidence.md)，其原始文件/日志仍在 gitignored `validation/local/`。v0.10.0 在全新 data root 对一条 NASA 官方公开 Instagram Reel 完成同级 Windows direct/no-cookie/Node `1/1 ready` 闭环，公开汇总见 [Iteration 0.10.0 Instagram live evidence](../validation/iteration-0.10.0-instagram-live-evidence.md)；本轮临时 data root 已在取证后删除。Compose 与 Linux runner 仍只有 YAML、静态契约、shell syntax 和 Windows 只读 preflight 证据。
- Iteration 0.11.0 的 Stage 0 CSV v2、Schema 8→9 边界和 Bilibili 脱敏诊断见 [Schema 9 / Bilibili 412 engineering evidence](../validation/iteration-0.11.0-schema9-bilibili-evidence.md)。raw probe `2/2` 成功，产品同款 fresh attempt `4/6` 成功、`2/6` 在 probe 阶段收到 HTTP 412；这是间歇现象的记录，不是平台认证。
- Iteration 0.12.0 的辅助产物下载、TikTok 短链安全门禁与 MVP 路由离线 E2E 见 [artifact / TikTok engineering evidence](../validation/iteration-0.12.0-artifact-tiktok-evidence.md)。该记录没有真实媒体请求、Cookie、运行 UUID、绝对路径或媒体指纹。
- Iteration 0.13.0 的 Schema 10 capability governance 见 [capability governance evidence](../validation/iteration-0.13.0-capability-governance-evidence.md)。静态实现、聚合 evidence 与人工 decision 被明确分离；仓库不附带真实样本或批准记录。
- Iteration 0.14.0 的 Windows 本机 Worker Cookie 接入、非领取式预检与 synthetic 端到端结果见 [local Cookie evidence](../validation/iteration-0.14.0-local-cookie-evidence.md)。该记录不证明真实登录态或平台兼容性。
- Iteration 0.15.0 的 Windows 一体化入口、握手、单实例、Ctrl+C、异常子进程回收、端口释放和日志关联见 [local application evidence](../validation/iteration-0.15.0-local-app-evidence.md)。该记录验证本机软件生命周期，不新增任何平台级下载结论。
- Iteration 0.17.0 的实时阶段估算、异常来源边界、API/DOM/隔离浏览器与发布门禁记录见 [real progress engineering evidence](../validation/iteration-0.17.0-real-progress-evidence.md)。该记录不是平台级、目标 Linux/Docker 或第三方工具包再分发证据。
- Iteration 0.18.0 的当时入口并发、连续补位、清理排除与中断回归见 [concurrent Worker engineering evidence](../validation/iteration-0.18.0-concurrent-worker-evidence.md)。默认两个执行槽；SQLite 继续限制总活动任务 2、单平台 1。槽位在最终清理返回后才释放，运行期间的补位不执行回收。独立 Worker 的 Ctrl+C 中断会关闭所拥有的子进程；`worker_lost` 按现有策略为终止失败，可在 Web 显式重试，不声称自动重试。local-app 的停止仍由 supervisor 先关闭 DB gate，再等待并按原有期限终止其拥有的进程树。
- Iteration 0.19.0 的 Windows 受控直连短链、run-scoped 默认凭证及其 API/导入/重试回归见 [short-link / Cookie defaults engineering evidence](../validation/iteration-0.19.0-short-links-cookie-defaults-evidence.md)。本轮没有真实平台请求或登录态验收；本地 `available` 不是平台授权证明。
- Iteration 0.16.0 的 Schema 11 claim fencing、停止/claim 事务顺序、lease recovery 与显式 flat retry/cooldown/manual reset 记录见 [claim/retry engineering evidence](../validation/iteration-0.16.0-claim-retry-evidence.md)，现为 point-in-time 历史证据。
- Schema 8 graph-v2 已实现 parent `discover`、不可变 discovery snapshot、逐附件 child `download`、active-snapshot 聚合、`partial_success`、input cancel、terminal-only rediscover 与 generation-local retry。旧 flat-v1 Job/Asset/manifest 不原地改写，仍可读。
- graph-v2 的 exact-selector 证据只来自测试内的离线 `ScriptedGraphFakeAdapter`。Windows 本机 Worker 在领取任务的同一 SQL 事务中跳过 `discover` 与 `x_attachment`，让这些任务保持 queued 并继续寻找可处理的 flat-v1 `download`；这不是 graph 支持。candidate real Worker 同样不能据此运行真实 X graph。
- 任意现存批次可通过 `GET /api/v1/batches/{batch_id}/assets` 获得已完成原件的元数据与相对下载 URL，包括沿实际重复输入引用找到的原 owner 成品；空列表不表示其他任务已经结束。`GET /api/v1/assets/{asset_id}/download` 仍只提供数据库登记且重验通过的 original。前端不再等待整个批次 ready，并提供“刷新成品”；不会按同链接的后来任务替换原 owner，也不使用批次名或平台标题拼接下载文件名。
- 控制面与 Worker 具有本地、有界、轮转、字段白名单的 JSONL 排障日志；`/health` 在队列未暂停时报告 `worker=external_status_unknown`，明确表示控制面不知道外部 Worker 进程状态，而不是心跳、在线或已启动证明。
- `/workflows` 的 AI 步骤把首个已选分段起点到末个已选分段终点的连续派生音频发送给听写；启用 AI 时各分段必须首尾连续，不能把未选择的间隙一并外发。单 cue 上限 4096 字符。字幕与配音按每个分段过滤并把时间归零，边界切入 cue 时拒绝，空 B-roll 段使用本地静音，保留原声时固定压到 22% 后混音。应用重启时，只有 canonical profile 已预授权且域恢复证明为原始、尚未 dispatch 的 queued AI/render/upload 会重新校验并续跑；手动流程、所有 retry、running/canceling、账本 dispatched/unknown 与上传 unknown 都停下。远程调用账本不保存 provider request ID，人工 reconciliation 也不能把未知结果改写成可静默重试。
- Workflow 冻结每个上传账号的 `session_revision`。待确认的账号若重新登录则以 `account_session_changed` 停下；上传重试只沿账号、来源和平台不变的唯一 retry leaf 对账，分叉、循环或身份变化失败关闭。远端 `unknown` 永远要求先查平台后台，不能自动重发。
- 完整 fan-out 进入 `upload_job_failed` 后，`/workflows` 可建立失败投稿重试。系统会在同一个 Upload 事务中重新核对稳定 request key/digest、完整投稿参数、当前 leaf、账号 session、source/cover、schedule 与平台合同，只为 failed/canceled slot 建立 draft；submitted、draft_saved、queued、running 与既有 draft 保持原位。所有 retry draft 都必须再次明确确认，即使原 profile 已预授权自动上传。若同批还含重启后或其他原因留下的原 draft，页面显示 `upload_retry_mixed_confirmation_required`，最终确认会同时排队这些原 draft，应先通过 `/uploads` 逐项核对。Workflow 在创建重试前先观察 Upload 域并写回当前 leaf，因此 Upload 提交成功但 Workflow 响应丢失时不会重复建后继。完整行为与边界见[Workflow 投稿重试验证](../validation/iteration-0.28.0-workflow-upload-retry.md)。
- `/workflows` 顶部的“自动执行就绪度”分别读取本次托管下载 Worker 心跳、AI runtime/三项 authorization、上传 runtime/当前 scheduler，以及 `active + ready` 的所选账号。四项探针相互独立；“可执行”只表示创建前本地条件通过，不证明 URL、provider 或平台会接受。页面状态有时间差，创建、worker 领取和实际执行仍由服务端重新校验；详见[就绪度界面记录](../validation/iteration-0.28.0-workflow-readiness-ui.md)。
- `/workflows` 默认使用下载来源标题。首次先选择账号并填写 Bilibili 分区、标签、原创/转载、各平台声明、封面和发布时间等不可推断参数，再保存预设；后续恢复该预设时可只更换 URL。页面只有在预设、账号和 AI capability 都读取成功且用户尚未编辑时才恢复上次预设；读取失败会保留表单并允许刷新后重试。下载 ready 后，Workflow Schema 3 把公共标题和每个账号按当前 capability 截断后的最终标题一次冻结到 `resolved_upload`；显式账号标题优先。普通 ready asset 使用自身来源标题，X 附件标题缺失时只回退到同一 Job 输入的父来源标题。重启、分段 fan-out 与取消对账复用该快照，冻结后 asset ID 也不可改。`workflow_source_metadata_unavailable` 表示来源标题或本地 capability 暂不可读，修复后点“立即对账”只重试本地解析，不重复下载或换绑素材。预设只保存参数和账号 ID，不保存 URL、密钥或登录会话。

代码已把 Linux Worker network namespace、只读 digest 镜像、受控 proxy 私有 Unix socket、loopback relay、精确版本、资源限制、Cookie boundary 和短链可信 transport 组装成 fail-closed candidate；v0.9.0 可显式提供唯一受校验的 Node/Deno/Bun/QuickJS 路径且保持 remote components 禁用，但目标镜像尚未随附或验收 JS runtime。Windows 本机直连闭环不满足这条隔离 contract；没有 Docker/Linux 实跑、Stage 0 host allowlist、外部认证和生产规模恢复证据时，不得把 Linux candidate 改称可部署版本。

## 2. 安装与配置

普通源码用户先运行 `Setup-Open-Flame.cmd`，再运行 `Start-Open-Flame.cmd`，步骤见 [Windows 首次安装](WINDOWS_SETUP.md)。以下为开发者流程，前置条件是 Python 3.12+ 与 `uv`，在仓库根目录执行：

```powershell
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src
```

默认配置可直接启动。需要显式路径时：

```powershell
$env:VDC_HOST = "127.0.0.1"
$env:VDC_PORT = "8000"
$env:VDC_DATA_ROOT = "C:\vdc-data"
$env:VDC_DATABASE_PATH = "C:\vdc-data\control.sqlite3"
$env:VDC_MAX_BATCH_URLS = "50"
$env:VDC_ROUTE_POLICY_VERSION = "mvp-v1"
$env:VDC_ENABLE_X_GRAPH_V2 = "0"
$env:VDC_STORAGE_MIN_FREE_BYTES = "1073741824"
$env:VDC_RUNTIME_LOG_LEVEL = "INFO"
$env:VDC_RUNTIME_LOG_MAX_BYTES = "10485760"
$env:VDC_RUNTIME_LOG_BACKUP_COUNT = "5"
$env:VDC_TOOL_ROOT = "C:\vdc-runtime-tools\windows-x64"
uv run video-download-control
```

程序不会自动读取 `.env`；[`.env.example`](../.env.example) 只用于说明变量。应由 shell 或将来的服务管理器注入配置。

首次启动会：

1. 创建 `VDC_DATA_ROOT` 与数据库父目录；
2. 将 SQLite journal mode 设为 WAL；
3. 依次应用 forward-only migration；Schema 7→8 重建 graph 关联结构，Schema 8→9 为旧能力表加入 `job_kind`，Schema 9→10 将该表只读封存并建立 capability ledger，Schema 10→11 新增唯一的 `worker_claim_gate` 行；每步都在独立、fail-closed 的 `BEGIN IMMEDIATE` 写事务内完成；
4. 验证精确 1–11 migration history、Schema 11 表/view 类型、列/索引/trigger/FK、graph 语义、Schema 9 archive、evidence identity/digest/policy/static-route、decision revision chain/current view，以及 `queue_control` / `worker_claim_gate` singleton。

若数据库 schema 高于当前程序支持版本，启动会拒绝继续，不能强行降级。

### 2.1 Windows 本机工具链

使用尚不存在的绝对目录安装。工具只进入该目录，不修改系统 `PATH`：

```powershell
$ToolRoot = "C:\vdc-runtime-tools\windows-x64"
uv run video-download-tools install --tool-root $ToolRoot
uv run video-download-tools verify --tool-root $ToolRoot
uv run video-download-tools smoke --tool-root $ToolRoot
uv run video-download-tools status --tool-root $ToolRoot
```

`install` 会拒绝覆盖已有目标，严格校验固定 release URL、大小和 SHA-256，只复制锁定的 FFmpeg EXE/DLL 与许可文件，执行 yt-dlp zipimport、FFmpeg/ffprobe 精确版本与 configuration 检查，再生成 1 秒 synthetic 音视频并由 ffprobe 确认同时存在 audio/video stream。该流程不传入媒体 URL，不访问平台。若使用已下载缓存，额外传入尚受本机管理员保护的绝对 `--artifact-cache`；缓存中的每个文件仍会按 lock 重验。

安装后的 `bundle-lock.json` 必须与应用随包 lock 逐字节相同，任何多余文件、link/reparse/hardlink、大小/hash/version/configuration 漂移均 fail closed。`smoke-result.json` 只是本机可修改的运维标记，不是数字签名或平台证据。yt-dlp release signature 文件会作为来源证据保留，但当前 bootstrap 没有执行 OpenPGP 验签，信任锚仍是随应用审阅的固定 SHA-256。

确认控制面状态：

```powershell
$env:VDC_TOOL_ROOT = $ToolRoot
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/tools
```

只有 `state=ready`、`detail_code=ok`、`offline_smoke_passed=true` 才表示本机工具闭环完成。此工具接口的 `isolated_worker_ready`、`platform_download_verified`、`network_download_enabled` 继续为 `false`，其中最后一项是通用隔离 Worker 的静态联网策略；Windows 的 `local_direct_worker_available=true` 只说明本机入口具备启动条件。它们不表示本次进程状态。

本次状态读取 `GET /api/v1/operations/runtime`：普通 Start 的 supervisor 以同一 run/build 的进程间共享状态报告 `mode=managed_direct`，`state=online/paused` 且心跳未满 3 秒才表示本次托管 Worker 存活并启用本机直连下载。暂停只停止新领取；独立控制面为 `external_unknown/unknown`，在线心跳过期为 `stale`。`starting/check_only/stopping/stopped` 保留本次已记录的生命周期阶段，不是在线或响应能力断言，不因慢预检/清理而变成过期在线。页面每 1.5 秒读取，响应迟到或请求挂起时，先前在线显示也会在本地时限到达后失效。`/health` 原有 `external_status_unknown` 是数据库健康契约，不替代该心跳接口。心跳只证明 supervisor 最近观测到进程存活，不证明任务进度、平台可用或隔离执行。

### 2.2 Windows 一体化本机应用（推荐）

正常退出时 supervisor 记录 `stopping`，先等待 Worker 再关闭控制面；浏览器能收到时显示正在停止。控制面已经关闭后，页面无法再次取得状态，应显示未知，不能继续显示在线。内部最终 `stopped` 记录不等于服务退出后仍有可访问的状态 API。

开发树已存在并通过校验的工具包可显式传入；普通安装也可把工具放在默认 app root 下的 `runtime-tools\windows-x64`：

```powershell
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --allow-direct-network
```

默认 app root 为 `%LOCALAPPDATA%\Open-Flame\video-download-control`，固定数据布局为 `data\control.sqlite3`、`data\assets`、`data\temporary` 与 `data\logs`；不会读取 `VDC_DATA_ROOT`、`VDC_DATABASE_PATH`、`VDC_TOOL_ROOT` 或 `VDC_ENABLE_LOCAL_REAL_WORKER` 来拼出第二套隐式配置。成功时 stdout 只输出 `{"status":"ready","url":"http://127.0.0.1:8000/"}`，随后 Windows 才会打开浏览器。使用 `--no-open-browser` 可只保持服务；使用 `Ctrl+C` 会先停 Worker、再停控制面并释放端口。

部署或修改配置后先做非领取式检查：

```powershell
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --allow-direct-network `
  --check
```

成功 stdout 为 `{"status":"checked"}`，然后两个子进程和端口全部释放。它会验证 Windows host、绝对普通路径、工具链、日志、Schema 11 数据库、build identity 和可选 Cookie config/source，并 prepare 一个保持关闭的本次运行 claim gate，但不会 activate、打开浏览器、领取 Job、创建 Attempt/Asset、自动登记默认凭证 profile 或发布文件。此检查不证明真实平台登录或下载成功。端口已占用时在创建 app 布局或子进程前失败；同一 app root 的 `.local-app.lock` 阻止第二个 supervisor。不要把 HTTP 200 单独当作本次子进程身份：supervisor 还会核对严格私有握手、Schema、product identity 与 capability snapshot。

三类日志位于 app root 的 `data\logs`：`runtime-local-app.jsonl`、`runtime-control.jsonl`、`runtime-local-worker-*.jsonl`。同一次启动共享一个随机 `run_id`；排障先按该值关联 `local_app.*`、`control.*`、`worker.preflight_*`、`worker.*` 和有界 subprocess 事件。CLI 顶层错误固定为无路径/参数/异常原文的 JSON。浏览器打开失败只写 warning，不会关闭已就绪服务。

`--allow-direct-network` 同时允许媒体下载与本机受控短链展开；第 7.6 节说明与 POSIX UDS 路径的区别和时限。页面可选择使用配置默认或匿名，该选择用于新建、导入和显式重试；短链展开本身不附带 Cookie。

如需前端新任务使用 Cookie 默认，建立仓库外、只读、单链接普通 v2 JSON，并只把配置文件路径交给 CLI：

```json
{
  "schema_version": 2,
  "cookie_sources": [
    {
      "platform": "youtube",
      "credential_ref": "youtube-primary-v1",
      "path": "C:\\private\\vdc\\youtube.cookies.txt"
    }
  ],
  "default_cookie_platforms": ["youtube"]
}
```

```powershell
$CookieConfig = (Resolve-Path -LiteralPath "C:\private\vdc\cookie-sources.json").Path
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --cookie-config $CookieConfig `
  --allow-direct-network `
  --check
```

v2 顶层精确字段为 `schema_version`、`cookie_sources`、`default_cookie_platforms`；source 每项只允许 `platform`、`credential_ref`、`path`，每个平台最多一项。默认平台列表只能引用已有 source 映射，不得重复，空列表表示不启用默认。旧 v1 顶层仍只有 `schema_version` 与 `cookie_sources`，保持 source-only，不会因为已配置 source 而自动绑定新任务。

上述 `--check` 不登记 profile。检查后去掉 `--check` 正常启动，控制面才在同一数据库事务中登记缺失 profile，或复用同平台、同 ref 且未禁用/过期的唯一 profile；禁用、过期或有歧义的既有记录会拒绝启动，不能靠默认配置复活或绕过。映射是本次 `run_id` 的不可变配置，不增加持久化全局默认；更改配置/source 后应正常停机并重新启动。v1 及高级手工 Worker 仍可使用第 7.2 节的管理员登记/分配流程。

`POST /api/v1/batches` JSON 的 `credential_mode`、`POST /api/v1/batches/import` query 的同名参数省略均为 `use_default`；`anonymous` 明确不为新任务绑定凭证。未配置的平台仍匿名。需要新绑定时，失效配置/profile 以固定 409 拒绝整个批次，凭证和 Job 在同一写事务提交，常驻 Worker 不会抢先领取未完成绑定的 Job。`POST /api/v1/jobs/{job_id}/retry` 无请求体保留旧绑定；显式请求体 `{"credential_mode":"use_default"}` 或 `{"credential_mode":"anonymous"}` 与新 `run_generation` 原子提交，不绕过 cooldown/重试范围。网页重试发送当前表单选择。跨批次去重复用已有 live/ready 工作时不改写其凭证或状态，也不表示重新按当前模式下载。

`GET /api/v1/credential-defaults` 只返回已配置的 `platforms` 列表与本地配置/profile `available`，无配置时为 `{"platforms":[],"available":true}`；它不会返回 profile UUID、ref、source 路径或 Cookie，不执行平台登录校验。config/source 在启动、绑定及领取路径重验，并须位于 app/tool root 之外。配置路径本身仍会出现在本机 argv；内容、ref 与 source 路径不会进入公开 API 或普通运行日志。Windows DACL 私有性尚未由该检查证明。

### 2.3 JavaScript runtime 与历史直接工具样本

`video-download-candidate-worker` 与 `video-download-local-worker` 都可选接受 `--js-runtime NAME:ABSOLUTE_EXECUTABLE`，其中 NAME 只能是 `node`、`deno`、`bun` 或 `quickjs`。未设置时 yt-dlp 显式清空所有 JS runtime；设置时先清空默认项再只加入这个绝对普通文件，并继续使用 `--no-remote-components`。probe/download 另固定 `--encoding utf-8`，避免 Windows 本机代码页影响错误分类或排障输出。

本机 A/B 使用 Node.js `v24.16.0`：未配置 runtime 时 YouTube 样本虽成功但产生缺失 runtime 警告；显式 Node 后同一 probe exit `0`、stderr 为 0 bytes。真实下载结果与 X 无声音轨样本的验证详情见 [Iteration 0.8.1 live-platform evidence](../validation/iteration-0.8.1-live-platform-evidence.md)。Node 当前不是 `runtime-tools` 锁的一部分，不得把本机路径直接复制为目标 Linux 配置。

### 2.4 Windows 本机直连 Worker（高级手动入口）

先在终端 A 用目标数据根启动控制面，让它创建并迁移控制数据库：

```powershell
$ProjectRoot = (Resolve-Path -LiteralPath ".").Path
$DataRoot = Join-Path $ProjectRoot "data"
$Database = Join-Path $DataRoot "control.sqlite3"
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
$env:VDC_DATA_ROOT = $DataRoot
$env:VDC_DATABASE_PATH = $Database
$env:VDC_TOOL_ROOT = $ToolRoot
$env:VDC_ENABLE_X_GRAPH_V2 = "0"
uv run video-download-control
```

终端 B 必须指向同一个 data root 与同一个已存在数据库。下面的 Node 路径只是 Windows 本机示例；CLI 会要求 runtime 为绝对、普通、非链接的可执行文件：

```powershell
$ProjectRoot = (Resolve-Path -LiteralPath ".").Path
$DataRoot = Join-Path $ProjectRoot "data"
$Database = Join-Path $DataRoot "control.sqlite3"
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
$Node = (Get-Command node.exe).Source
$env:VDC_ENABLE_LOCAL_REAL_WORKER = "1"
uv run video-download-local-worker `
  --data-root $DataRoot `
  --database-path $Database `
  --tool-root $ToolRoot `
  --js-runtime "node:$Node" `
  --allow-direct-network `
  --poll-interval-seconds 2
```

两个条件缺一不可：环境变量必须精确为 `1`，且当前进程必须传 `--allow-direct-network`；启动时和每次 claim 前都会重新检查。该 Worker 只支持 Windows，直接使用宿主网络，不是隔离模式；它会验证共享数据库 readiness、固定工具链/ffprobe、日志首写与单实例锁后才领取任务。正常停止两个进程都使用 `Ctrl+C`。

需要 Cookie 的平台先按 7.2 节登记/分配 profile，再准备仓库外、非空、只读的 Netscape Cookie 文件。ref 必须和该平台 profile 的 `secret_ref` 一致。先停止同一 data root 的常驻 Worker，再运行预检：

```powershell
$CookieFile = (Resolve-Path -LiteralPath "C:\private\vdc\douyin.cookies.txt").Path
attrib +R "$CookieFile"
$CookieSource = "douyin:douyin-primary-v1=$CookieFile"

uv run video-download-local-worker `
  --data-root $DataRoot `
  --database-path $Database `
  --tool-root $ToolRoot `
  --js-runtime "node:$Node" `
  --cookie-source $CookieSource `
  --allow-direct-network `
  --check
```

只有 stdout 返回 `{"cookie_platforms":["douyin"],"status":"ready"}` 且 `logs/` 中出现 `worker.preflight_started`、`toolchain.inspected`、`worker.preflight_succeeded`，才表示数据库、工具链和已配置 source 的本地预检通过。`--check` 不领取 Job，不创建 Attempt/Asset/commit intent，也不复制 Cookie。它不会检查 queued Job 是否都已分配匹配 ref，也不能证明真实 Cookie 尚未过期。正式常驻命令必须加入同一个 `--cookie-source $CookieSource`。

本机 Worker 每个平台最多接受一个 source；不同平台不得指向同一物理文件。source 必须位于 data/tool root 之外、不是 link/reparse/hard-link alias、不可写，且大小为 1 到 `--max-cookie-bytes`（默认 8 MiB）。每次 probe/download 会从已打开且身份稳定的 source 复制到当前 Attempt 下的新文件，yt-dlp 不接触原 source 路径，操作结束后副本被清理。合法命令行仍会把 opaque ref 与绝对路径暴露给本机进程列表和 PowerShell history；Windows 路径尚未证明私有 NTFS DACL。只允许在受信任的单用户主机使用，生产凭据需先完成 ACL、secret sidecar 或 per-platform/per-Attempt process isolation。

本机 Worker 固定 `skip_unsupported_graph_jobs=True`：claim SQL 在同一事务中排除 `job_kind=discover` 与 `source_type=x_attachment`，不领取、不失败、不改写这些任务，并可以继续领取队列中后面的 flat-v1 `download`。这只是防止本机 Worker 污染 graph 状态；日常本机运行仍应保持 `VDC_ENABLE_X_GRAPH_V2=0`。

## 3. 启动、停止与健康检查

启动：

```powershell
uv run video-download-local-app --tool-root $ToolRoot --allow-direct-network
```

正常停止使用当前终端的 `Ctrl+C`。单独调试控制面时才运行 `uv run video-download-control`；它不会同时启动 Worker。当前没有 Windows Service / systemd unit；若由外部服务管理器托管，应先发出正常终止信号，再设置有限的强制停止超时。

`video-download-local-app` 的停止顺序是数据库 gate → Worker → control：supervisor 先以 `BEGIN IMMEDIATE` 为本次 `run_id` 提交 `accepting_claims=0` 和 `stop_requested_at`，然后才关闭 Worker command Pipe、等待 Worker，最后关闭控制面 Pipe。Worker 的 `claim_next()` 在自己的 `BEGIN IMMEDIATE` 内、创建 lease/Attempt 之前核对同一 `run_id`、`worker_id` 和 open gate，因此 stop 与 claim 只有一个 SQLite 写事务顺序：先提交的 claim 可作为在途工作完成，先提交的 stop 则保证该 Worker 不会新增 claim。Pipe/EOF 只是停止通知，不是线性化边界。

如果 gate stop 无法提交，supervisor 会把停止标为 forced，并在关闭 advisory Pipe 前终止仅包含本应用子进程的 Windows Job；若 Job API 本身失败，再定点终止仍存活的本应用子进程。此时不要把退出描述为 graceful，也不要立即手工改写 Job/Attempt：先确认子进程已消失，再按下述 lease 恢复规则观察数据库。

已在 stop 之前提交的活动 lease 不会被 gate 撤销。正常 Worker 会继续 heartbeat 并完成该 Attempt；若 Worker 被强制终止，默认 lease 到期后，后续 Worker 的 claim 事务会把旧 running Attempt 标成 `abandoned/worker_lost`，清空旧 lease，并在本 generation 尚未达到 4 次时重新排队，达到上限时终态失败。带 `asset_commit_intent` 的中断发布会先走既有 commit-intent recovery，不能通过删除 Attempt、改 `lease_expires_at` 或清空 intent 来“加速恢复”。

检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/tools
Invoke-RestMethod http://127.0.0.1:8000/api/v1/download-capabilities
Invoke-RestMethod http://127.0.0.1:8000/api/v1/capability-implementations
Invoke-RestMethod http://127.0.0.1:8000/api/v1/capability-evidence
Invoke-RestMethod http://127.0.0.1:8000/api/v1/capability-decisions
Invoke-RestMethod http://127.0.0.1:8000/api/v1/capability-snapshot
```

预期正常状态包括 `status=ok`、`database=ok`、`schema_version=11`。注意：

- `/health/live` 只证明 HTTP 进程存活。
- `/health/ready` 在数据库结构异常或持久化队列暂停时返回 503。HTTP probe 的完整 readiness 最多缓存 5 秒，SQLite schema cookie 在 DDL 后立即失效缓存；完整审计期间 schema 发生变化或最终 cookie 不可读时固定 fail closed。管理员 CLI、备份与恢复继续执行不缓存的完整检查，不能用 probe 缓存代替发布门禁。
- 队列未暂停时 `worker=external_status_unknown`；队列暂停时为 `worker=paused`。`external_status_unknown` 的含义是控制面没有外部 Worker 心跳/存活协议，绝不能解释为在线、离线、已启动或可下载。
- `/api/v1/download-capabilities` 保留为兼容的静态 `candidate` 视图；`capability-implementations`、`capability-evidence`、`capability-decisions` 分别呈现代码实现、精确 product build/downloader/environment 聚合证据与 current 人工决定。`capability-snapshot` 只执行一次 readiness，并在一个 SQLite 读事务中返回 UI 所需三层、当前 build identity、总数/截断标记和决定引用的 evidence；页面仅在首次打开或点击“刷新能力”时读取，不纳入 10 秒运维轮询。history 需按 identity key 查询。Stage 0 报告不写数据库，导入只追加 evidence，approve/revoke 另需本地 CLI 与 revision CAS；任何一层都不证明平台当前在线可用。

机器可读 OpenAPI 位于 `http://127.0.0.1:8000/openapi.json`。`/docs` 与 `/redoc` 默认不存在，防止本地控制面隐式加载第三方 CDN 资源；如需交互式浏览，应在受控开发环境用已审计并固定哈希的本地静态资源另行托管。

## 4. 运行观测与操作控制

直接调用第 4～6 节中的下载域写接口前，先从同一 loopback origin 取得当前进程令牌并构造 header。应用重启后必须重新执行；令牌不得放入 URL/query、日志或支持材料。

```powershell
$OpenFlameBase = "http://127.0.0.1:8000"
$DownloadSession = Invoke-RestMethod "$OpenFlameBase/api/v1/session"
if (
  [string]::IsNullOrWhiteSpace($DownloadSession.csrf_token) -or
  $DownloadSession.csrf_token -match '[^\x00-\x7F]'
) {
  throw "Open-Flame returned an invalid download session token"
}
$DownloadHeaders = @{ "X-Download-CSRF" = $DownloadSession.csrf_token }
```

读取 JSON 指标：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/metrics
```

当前指标包含 queued / active 数量、平均 Attempt 次数、P50/P95 Attempt 时长、磁盘总量/剩余量、队列暂停原因、平台结果、平台熔断和最终错误计数。`jobs` / queue depth 会包含 parent `discover` 编排任务；`platform_outcomes` 明确只统计 `job_kind=download`，parent ready 不会抬高平台下载成功率。它不是 Prometheus endpoint，也没有外部告警管线。

### 4.1 持久化队列暂停

受管资产存储发生容量、落盘、fsync 或 rename 等 `storage_error` 时，Worker 会把暂停状态写入数据库；新 Worker 进程也会读取这一状态，重启不会绕过暂停。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/queue
```

处理步骤：

1. 停止 Worker，确认没有继续写入资产目录。
2. 检查容量、权限、目标卷可用性和文件系统错误。
3. 先备份数据库与资产，再修复根因。
4. 确认 `disk_free_bytes >= VDC_STORAGE_MIN_FREE_BYTES`。
5. 恢复队列：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/operations/queue/resume `
  -Headers $DownloadHeaders
```

若剩余空间仍低于水位，API 返回 409 并保持暂停。当前没有管理员手动 pause API；计划维护窗口时应停止 Worker 和控制面，而不是修改数据库。

### 4.2 平台熔断

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/platform-circuits
```

当前策略：

- `rate_limited` 第一次即打开至少 60 秒冷却；冷却结束只允许一个 half-open probe。连续第 3 次同类失败要求人工 reset。
- Bilibili 的受限 HTTP/API 412 标记按 `rate_limited` 处理，使用既有 60/120 秒 retry backoff 和 platform cooldown；该窄化规则不会把其他平台的 412 自动改类。不要通过反复 reset、添加未审阅 Cookie/header 或代理来绕过。
- `extractor_broken` 连续 2 次后打开并要求人工 reset。
- `authentication_required` 与普通 `network_error` 不会全局熔断整个平台。
- half-open probe 成功会关闭熔断；Worker 丢失后租约恢复不会让 probe 永久卡住。

仅在已确认版本、凭证、平台状态与限流根因后复位：

```powershell
$Platform = "youtube" # x | youtube | bilibili | douyin | tiktok | instagram
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/platform-circuits/$Platform/reset" `
  -Headers $DownloadHeaders
```

不要为了清空红色状态反复 reset；这会绕过抑制重试风暴的运维意图。

### 4.3 显式 flat retry、cooldown 与 manual reset

只有终态 `failed`、没有 graph parent/target/active discovery 的 flat `download` Job 可显式重试。前端会对这种 Job 显示“重试 … 任务（新一代）”；API 等价操作：

```powershell
$JobId = "replace-with-terminal-failed-flat-job-id"
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/jobs/$JobId/retry" `
  -Headers $DownloadHeaders
```

成功返回 `status=queued` 和递增后的 `run_generation`。该操作在一个 `BEGIN IMMEDIATE` 事务内推进 Input/Job 的 active generation、清空本代终态字段并把 `generation_attempt_count` 归零；累计 `attempt_count` 和所有历史 Attempt 保留。缺失 Job 返回 404；非终态、graph Job、并发重试失败，或相同 source 已有 queued/active/ready 工作时返回 409。不要通过重复提交或直接改库绕过 CAS。

显式 retry **不会**关闭或复位 `platform_circuits`。若平台仍在自动 cooldown，Job 会保持 queued，冷却到期后只允许一个 half-open probe；若 `requires_manual_reset=true`，Job 会继续等待，必须先检查版本、凭证、平台状态、错误码与日志，确认根因已处理后才调用 4.2 节的 reset。reset 只接受确实要求人工复位的 circuit；普通 cooldown 不需要也不允许提前 reset。

### 4.4 结构化运行日志

控制面、离线 Worker、Windows 本机 Worker 与 candidate Worker 默认把各自的 UTF-8 JSONL 写到 `${VDC_DATA_ROOT}/logs/`。active 文件按组件区分；达到 `VDC_RUNTIME_LOG_MAX_BYTES` 后轮转，最多保留 `VDC_RUNTIME_LOG_BACKUP_COUNT` 份历史文件。默认分别为 10 MiB 和 5 份；`VDC_RUNTIME_LOG_LEVEL` 接受 `DEBUG`、`INFO`、`WARNING` 或 `ERROR`。程序不会自动读取 `.env`，变量必须由 shell 或 supervisor 注入。

前端“运行日志”区域只在打开页面及点击“手动刷新”时读取最近事件。也可直接查询：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/operations/logs?limit=100"
```

返回体中的 `status` 表示日志管线状态：`ok` 为当前可写且没有被拒绝的事件，`degraded` 表示有调用方事件违反字段边界，`error` 表示日志尚不可写或写入失败；同时检查 `write_failures`、`rejected_events` 和 `last_failure_code`。最近事件按 `run_id`、`request_id`、`job_id`、`attempt_id` 关联。HTTP 只记录 method、route template、status 与耗时；原始 Uvicorn access log 被关闭，防止 query 形成第二套未脱敏日志。

日志字段白名单明确禁止 raw/submitted/canonical URL、query、Batch name、导入 filename、请求/响应 body 与 headers、Cookie、credential ref、短链签名、subprocess argv/env/cwd/stdout/stderr、绝对路径和任意异常消息。事件只保留固定状态、错误码、数量、耗时和关联 ID。不要修改代码重新输出这些原始值；yt-dlp/FFmpeg 的 stdout/stderr 可能包含 Cookie、signed URL 或平台响应。

建议排障流程：

1. 记录问题发生时间，在前端手动刷新或调用上述 API，找到相同 run/job/attempt 的第一个异常状态。
2. 用稳定 `error_code` 与事件顺序定位阶段，再检查 `/health`、`/api/v1/metrics`、Batch/Job 状态和磁盘水位。
3. 若已有资产发布，复核数据库 Artifact 与资产 manifest 的 size/SHA-256；日志不能作为文件完整性证明。
4. `degraded` / `error` 时检查 `${VDC_DATA_ROOT}/logs` 容量、目录类型和当前管理员权限。日志恢复不代表队列或平台故障已修复；仍须按 4.1/4.2 的条件恢复或复位。

运行日志是 best-effort、会轮转、可能缺失且可由本机管理员修改，只是排障线索。业务事实仍以 SQLite、`job_attempts`、资产 manifest、备份 manifest 和正式 Stage 0 证据为准。顶层 `logs/` 被业务备份精确排除；如需保留支持材料，应在复核脱敏和访问范围后另行受控复制。日志虽经过字段限制，文件仍应按本机运维数据限制访问。

### 4.5 实时进度的含义与排障

`downloading` 百分比是 Job 级“阶段估算”，不是 yt-dlp 所有 sidecar、视频、音频与 fragment 的精确总字节百分比。固定模板只输出主媒体字段存在位、状态、字节数及受限并行索引；不输出 URL、ID、format ID、文件名或原始工具行。字幕等 auxiliary transfer 被忽略；普通串行主媒体最多按两个槽位保守累计，并行 fragment 按每个索引的最新单调 fraction 求和。下载阶段最多为 adapter `0.98`，首个后处理控制行进入 `postprocessing/0.99`，只有映射、分类和文件 stat 成功后 adapter 才报告 `1.0`。Worker 将该范围映射为持久化的 `0.05–0.80`，随后验证为 `0.85`、最终 ready 为 `1.0`。

未知总大小只产生 heartbeat，不伪造百分比；fragment 的有限浮点 estimate 可以参与 fraction，但不会作为“精确 total bytes”写入 DTO。单个合并流在后处理前可能保守停在下载阶段的一半；多个 stream 的每路 100% 不代表整个 Job 已完成。页面每 2 秒轮询，短暂后处理阶段可能未被每次肉眼捕获；可用 Batch API、SQLite 状态和同一 `job_id` 的阶段日志核对。若百分比不动，依次确认 Job 的 heartbeat/lease、磁盘、平台 circuit、是否为未知总大小，再查第一个稳定 `error_code`；不要通过记录 yt-dlp 原始 stdout/stderr 来排障。

## 5. 离线 fake Worker QA

该流程使用同一个隔离 QA 数据库，但让 Worker 进程的“普通配置根目录”保持在其他位置，以通过防污染检查。

终端 A：

```powershell
$QaRoot = Join-Path $env:TEMP "vdc-offline-qa"
$env:VDC_DATA_ROOT = $QaRoot
$env:VDC_DATABASE_PATH = Join-Path $QaRoot "control.sqlite3"
uv run video-download-control
```

通过 `http://127.0.0.1:8000` 创建任务。然后在终端 B：

```powershell
$QaRoot = Join-Path $env:TEMP "vdc-offline-qa"
Remove-Item Env:VDC_DATA_ROOT -ErrorAction SilentlyContinue
Remove-Item Env:VDC_DATABASE_PATH -ErrorAction SilentlyContinue
$env:VDC_ENABLE_OFFLINE_FAKE_WORKER = "1"
uv run video-download-worker `
  --offline-fake `
  --data-root $QaRoot `
  --worker-id "offline-qa-1" `
  --drain
Remove-Item Env:VDC_ENABLE_OFFLINE_FAKE_WORKER
```

输出是一行一个 JSON 结果，直到 `{"status":"idle"}`。产物是 `.fake` 文件，只能证明 flat-v1 lease、retry、staging、hash、manifest 和数据库提交链路，不证明 URL 可下载，也不经过 FFmpeg/ffprobe。该命令使用的普通 fake adapter 不支持 exact selector；运行这个流程时必须保持 `VDC_ENABLE_X_GRAPH_V2=0`。graph-v2 的 `ScriptedGraphFakeAdapter` 目前只在自动化测试/测试 harness 内组装，没有可供运维调用的真实启用命令。

### 5.1 MVP 路由离线 E2E

Iteration 0.12.0 的自动化 E2E 以隔离临时数据库、`ScriptedFakeAdapter` 和 synthetic bytes 覆盖 YouTube video/Shorts、Bilibili BV/av 默认分 P、经显式 gate 和受信测试 resolver 展开的 Douyin 短链，以及有/无表头 CSV 和 TXT 导入。每条路径检查规范化、队列路由、fake Worker、ready 聚合、资产列表与原件下载。测试不启动 yt-dlp/FFmpeg、不访问外网、不证明链接在平台可下载；Douyin 的 injected resolver 只是测试信任边界。当前实际展开入口及其 Windows/POSIX 差异见第 7.6 节，这段历史证据不替代当前入口验证。

## 6. API 操作速查

创建 JSON 批次：

```powershell
$Body = @{
  name = "local-check"
  inputs = @(
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  )
} | ConvertTo-Json

$Batch = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/batches `
  -Headers $DownloadHeaders `
  -ContentType "application/json" `
  -Body $Body
$Batch.id
```

批次进入 `ready` 后，前端 `http://127.0.0.1:8000/` 会读取 ready 成品并渲染可点击链接。API 等价操作：

```powershell
$Assets = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/batches/$($Batch.id)/assets"
$Assets | Format-List

$FirstAsset = $Assets | Select-Object -First 1
if ($null -ne $FirstAsset) {
  $DownloadUri = "http://127.0.0.1:8000$($FirstAsset.download_url)"
  Invoke-WebRequest `
    -Uri $DownloadUri `
    -OutFile (Join-Path $env:TEMP "vdc-downloaded-asset")
}
```

列表只返回该批次已经 `ready` 的 original 元数据、`asset_id`、`job_id`、顺序、原件 `download_url`，以及登记的 thumbnail/caption DTO；不会暴露本机路径或用户标题。每个辅助 DTO 含 opaque `artifact_id`、`kind`、MIME、可选 caption language、SHA-256 与 `/api/v1/artifacts/{artifact_id}/download`。

原件下载端点只接受规范 UUID，并重新核对数据库登记的 ready original、`assets/{asset_id}/original` 路径约束、data-root containment、普通文件、非 link/单 hard-link 与登记 size。辅助端点进一步要求 ready asset/job、唯一且 hash 匹配的 parent original、`thumbnails/` 或 `captions/` 精确目录、受限文件名/扩展/MIME、caption language、1 byte 到配置上限的单链接普通文件，并在打开后重验 identity、size 与 SHA-256。不存在或不满足 ready/关系条件返回 404，已登记但文件边界失效返回 409；成功响应使用固定 `asset-{uuid}` / `artifact-{uuid}` 文件名、`Cache-Control: private, no-store` 与 `X-Content-Type-Options: nosniff`，不使用用户标题。

导入 UTF-8 TXT：

```powershell
$Bytes = [System.IO.File]::ReadAllBytes("C:\private\urls.txt")
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/batches/import?filename=urls.txt&name=import-check" `
  -Headers $DownloadHeaders `
  -ContentType "text/plain" `
  -Body $Bytes
```

导入接口只接受 `.txt` / `.csv` 文件名和最多 256 KiB 的 UTF-8 请求体。取消任务：

```powershell
$JobId = "replace-with-job-id"
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/jobs/$JobId/cancel" `
  -Headers $DownloadHeaders
```

按 Input 取消其所有非终态工作：queued parent/child 会原子变为 canceled，active Job 写协作取消请求，已 ready child 与不可变历史保留；聚合结果仍只看 active snapshot。

```powershell
$InputId = "replace-with-input-record-id"
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/inputs/$InputId/cancel" `
  -Headers $DownloadHeaders
```

仅对 graph-v2 Input 做显式 rediscover。当前 Input 的所有 parent/child 都必须已终态；只要还有 queued 或 active 工作，或 Input 不是 graph-v2，API 返回 409。成功后 parent 进入下一 `run_generation` 并返回新的 generation：

```powershell
$InputId = "replace-with-terminal-graph-input-id"
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/inputs/$InputId/rediscover" `
  -Headers $DownloadHeaders
```

普通失败重试留在同一 generation，受 `generation_attempt_count` 上限约束；rediscover 会把 parent 的 generation-local attempt count 重置为 0，但累计 `attempt_count` 保持单调递增。新探测只切换 `active_discovery_id` / `active_run_generation`，不删除旧 discovery、relation、Job、Attempt 或 Asset；相同 snapshot 可复用，变化只影响新的 active snapshot。

### 6.1 graph-v2 安全启用边界

`VDC_ENABLE_X_GRAPH_V2=1` 只让新提交的 X 单帖创建 parent `discover` Job，不会给 adapter 增加 exact-selector 能力。当前唯一满足该 contract 的实现是离线 `ScriptedGraphFakeAdapter`；真实 `YtDlpAdapter.supports_exact_selector=False`，普通 offline fake adapter 也不满足。candidate/offline Worker 的 capability check 会对误领的 graph Job fail closed；Windows 本机直连 Worker 采用更窄的 flat policy，在 claim 查询的同一事务内跳过 `discover` 与 `x_attachment`，使它们保持 queued 而不是被误标失败。这两种 containment 都不是运维启用 graph 的方案。

因此当前所有普通开发、Windows 本机直连、candidate deployment 与真实试运行配置都必须保持该变量未设置或等于 `0`。只有隔离、一次性 Schema 11 测试数据库中显式组装 `ScriptedGraphFakeAdapter` 的自动化测试可以覆盖 graph-v2。不得把本机原子跳过、离线 parent/child 成功、fake 生成的资产或 API 状态称为 X 多附件、真实 selector 或 graph 下载能力证据。

## 7. Proxy、UDS relay 与真实 adapter 边界

### 7.1 已实现的安全组件

- `ControlledForwardProxy`：显式非空 host allowlist；DNS A/AAAA 解析；阻断非公网、IPv4-mapped IPv6、NAT64/Teredo/6to4 等过渡地址；连接数、header、上传/下载字节与 timeout 上限；连接到批准的数值 IP 并复核 peer；不会自动跟随 redirect。
- `video-download-egress-proxy`：在绝对 UDS 路径启动 proxy，要求至少一个可重复的 `--allowed-host`，或一个绝对路径、plain regular、单链接、最多 16 KiB 的 UTF-8 `--allowed-host-file`；稀疏 JSON audit 只输出 `reason` 与 operator-configured policy host。
- `UnixSocketRelay`：只监听数值 loopback 地址，将 TCP 流量转给绝对路径 UDS；复核 UDS 类型、权限与 device/inode；限制并发、双向字节、连接/idle/总时限。
- `UnixRelayNetworkGuard`：Linux-only；要求 Worker namespace 只有 `lo` interface 与 loopback routes，并验证 UDS 与本地 relay 可达。
- `YtDlpCommandFactory`：固定 executable / FFmpeg 路径与预期版本，禁用用户配置、插件、远程组件、下载器内部重试及危险自由参数，并强制 loopback proxy 与 attempt-private Cookie 副本。Worker 会把配置的 `max_height` 传入 Adapter；格式选择的所有分支均保持高度约束，不再使用不受限 `/b` 回退。
- `YtDlpAdapter` 候选：每次操作先校验精确版本，使用有输出/时间/环境边界的 subprocess runner，解析有界 JSON 并只保留白名单元数据；平台明确要求 fresh cookies 时归类为 `authentication_required`；高度策略找不到格式时归类为 `content_unavailable`，不计为 extractor breakage 或触发平台熔断。`supports_exact_selector=False`，不能领取 graph-v2 parent 或附件 child 作为受支持的真实路径。
- `video-download-candidate-worker`：feature gate 与 builder 双重门禁；数据库初始化前和每次 claim 前运行网络 guard；启动时精确验证 yt-dlp、FFmpeg 与 ffprobe。
- `video-download-local-worker`：Windows-only `DirectEgress` 路径；`VDC_ENABLE_LOCAL_REAL_WORKER=1` 与 `--allow-direct-network` 双重明示，在日志、共享数据库、工具链与单实例检查完成前不 claim；可按平台接入 Cookie source，并用 `--check` 在不 claim 的情况下验证本地配置；每次 claim 前重验 gate/acknowledgement，并原子跳过不支持的 graph Job。它是本机功能路径，不是隔离安全组件。
- `AttemptCookieResolver`：验证 deployment-owned 只读、非空、有界 Cookie source，拒绝跨平台复用同一打开文件身份，并在每次 Adapter 操作前复制为 Attempt 私有 `0600` 文件；Job 的 `credential_profile_id` 已在 claim 时校验并解析为不透明 `secret_ref`，再以 `credential_ref` 传到 Adapter。数据库 ID、Cookie 源路径与 Cookie 内容不会穿过 Adapter request 边界。POSIX mode 不能替代尚未实现的 Windows DACL 证明。
- `ControlledShortLinkResolver` + `UnixAttestedShortLinkTransport`：控制面逐跳解析 DNS、阻断非公网地址并签名完整 numeric target；egress 端保留 hostname 做 TLS SNI/证书与 `Host`、连接 numeric IP、复核 peer，且绝不自动跟随 redirect。只有 bounded `Location` 返回，最终 signed URL 不进入业务持久化。

### 7.2 凭据管理流程（仅限本机管理员 CLI）

`video-download-credentials` 是直接操作现有控制数据库的 admin-only CLI，不是 HTTP API，也不提供给普通用户。`--database-path` 必须是已存在、绝对且规范化的 plain regular SQLite 文件；路径中不得有 symlink / reparse point。CLI 只登记短的不透明引用，不接受 Cookie 内容或 Cookie 文件路径。

登记并查看 profile metadata：

```powershell
$Database = (Resolve-Path -LiteralPath "C:\vdc-data\control.sqlite3").Path

uv run video-download-credentials `
  --database-path $Database `
  register `
  --platform youtube `
  --name youtube-primary `
  --secret-ref youtube-primary-v1 `
  --expires-at 2026-12-31T23:59:59Z

uv run video-download-credentials `
  --database-path $Database `
  list `
  --platform youtube
```

把返回的 profile ID 只分配给尚未 claim 的 queued Job；profile platform 必须与所选 Job 相同。可逐个 Job 或按 batch 分配：

```powershell
$ProfileId = "replace-with-profile-id"
$JobId = "replace-with-job-id"
$BatchId = "replace-with-batch-id"

uv run video-download-credentials `
  --database-path $Database `
  assign `
  --profile-id $ProfileId `
  --job-id $JobId

# 或：把同平台 profile 分配给该 batch 中匹配平台的 queued Jobs
uv run video-download-credentials `
  --database-path $Database `
  assign `
  --profile-id $ProfileId `
  --batch-id $BatchId
```

queued Job 可在 claim 前清除分配。禁用 profile 会阻止后续 claim 使用它，并为当前正在使用它的活动 Job 请求取消；它不会删除或轮换部署侧秘密：

```powershell
uv run video-download-credentials `
  --database-path $Database `
  clear `
  --job-id $JobId

uv run video-download-credentials `
  --database-path $Database `
  disable `
  --profile-id $ProfileId
```

Candidate Worker 和 Windows 本机 Worker 都必须由部署管理员为每个平台显式提供至多一个 `--cookie-source PLATFORM:OPAQUE_REF=ABSOLUTE_PATH`；当前允许的平台 literal 为 `x`、`youtube`、`bilibili`、`douyin`、`tiktok`、`instagram`。其中 `OPAQUE_REF` 必须与 claim 后解析出的 `secret_ref` 一致；文件必须是部署侧只读、非空、有界的 Cookie source，且不同平台不能共用同一物理文件。引用缺失、平台不符、文件不安全或过期/禁用 profile 都会 fail closed。Credential-free base Compose 不含任何 Cookie 路径；只有显式叠加 `compose.candidate.cookies.yaml` 才提供 Worker-only read-only root/mapping/runner。Linux host preflight 要求 canonical data/socket roots，拒绝 Cookie path 与 data/socket/repository 双向重叠、任意不安全 root-owned ancestor、named/default ACL、link、错误 owner/mode 和 metadata race。Windows `--check` 验证共享 Schema 11 数据库、工具链与 source 文件身份/大小/只读状态，但不证明 NTFS DACL 私有性或队列 credential coverage。Worker 的启动与运行顶层错误固定脱敏；Linux candidate 的 soft/hard core limit 均为零。**仓库没有任何真实 Cookie source**；完成上述数据库操作不会自动把 Cookie 注入 Worker。

当前 override 只做到 service-level mount isolation：同一个 Worker 能读取挂载 root 内所有已配置平台的 source。因此一旦 downloader/Worker 被攻陷，它可能读取本次任务以外、其他平台的 Cookie source。真实凭据上线前必须补做 credential sidecar、按平台拆 Worker，或按 Attempt 创建仅暴露单一 source 的 mount namespace；attempt-private `0600` 副本本身不能消除整个 source root 的可读性。

### 7.3 尚未形成的生产证据

上述 Linux 隔离组件已形成静态候选；Windows 本机直连 Worker 虽已完成 YouTube、X、Instagram 共三个精确输入的 Worker/AssetStore/manifest E2E，但不改变下列 Linux/生产证据缺口：

- Docker build/pull、Linux `network_mode:none`、共享 UDS/AF_UNIX、ACL、host `core_pattern` + Worker core limit 或 cold-start 的运行证据；
- 经过 Stage 0 验证的完整平台站点/CDN host allowlist；
- 已为目标 Linux 镜像构建并审计的 yt-dlp/FFmpeg tool bundle；JS runtime 与 `yt-dlp-ejs` 仍明确未加入该镜像；
- 生产 secret provider、Compose 只读 secret mount、Cookie source 权限、到期验证、轮换和外部秘密吊销演练；当前只有合成/静态边界；
- 短链真实 DNS、TLS、平台 redirect、POSIX supervisor topology、批次总耗时与 DNS resolver 可用性验收；Windows 进程内受控直连已接通，但不是网络隔离证据；
- 三个精确本机成功样本之外的平台覆盖；Bilibili 的间歇 HTTP 412 尚未形成稳定下载或 Stage 0 闭环，Douyin `authentication_required` 仍待用户授权 Cookie 后复验；TikTok 首次真实运行；以及任何真实网络隔离验收。

Proxy 只约束实际经过它的流量；若 Worker 仍有直连 interface、route、宿主机/LAN 路径或其他 proxy，它不能构成隔离证明。因此 Linux candidate 不得手工 import adapter 绕过其 CLI 门禁；Windows 本机下载只使用受支持的 `video-download-local-worker` 明示入口，并必须把结果标为 direct/non-isolated。

此外，现有 runner 只接受解析为 `unix:///...` endpoint 的 Linux daemon；Unix Docker socket 不能证明 daemon 与 runner 位于同一物理主机或 mount namespace。same-host/same-mount-namespace、无物理 alias/预置 bind mount、受审阅 checkout/build context 在整个验收期间由一个可信 privileged writer 独占，仍是必须由 operator 独立证明并记录的 trust assumptions。Dockerfile 已移除外部 `# syntax` 引用，改由目标 daemon 自带 frontend 解释；它必须至少支持 Dockerfile 1.3 的 `RUN --network=none`，且目标 BuildKit 必须实际落实断网语义。发布方须记录并审阅 daemon/frontend/BuildKit 版本、配置和实际行为；当前 Windows 离线环境未执行这一验证。

### 7.4 Proxy / relay 独立接口

下面只展示接口，不是当前可部署配方。`--allowed-host` 必须来自 Stage 0 中审阅过的站点与媒体/CDN 集合，示例的 `.invalid` 值必须替换。Proxy 会拒绝已存在的 UDS 路径；stale socket 只能由可信 supervisor 在确认类型、所有者和无活动进程后处理。

Egress namespace / container：

```bash
uv run video-download-egress-proxy \
  --unix-socket /run/vdc-egress/proxy.sock \
  --allowed-host platform.replace.invalid \
  --allowed-host media.replace.invalid \
  --max-connections 64 \
  --max-upload-bytes 67108864 \
  --max-download-bytes 536870912 \
  --connect-timeout 10 \
  --idle-timeout 30 \
  --total-timeout 300
```

Worker namespace / container（只有 loopback，挂载同一受保护 UDS）：

```bash
uv run video-download-unix-relay \
  --upstream-socket /run/vdc-egress/proxy.sock \
  --listen-host 127.0.0.1 \
  --listen-port 18080 \
  --max-connections 16 \
  --max-upload-bytes 67108864 \
  --max-download-bytes 536870912 \
  --connect-timeout 5 \
  --idle-timeout 30 \
  --total-timeout 300
```

单独运行任一 CLI 都不会隔离 Worker；必须由容器/network namespace/firewall 取消所有旁路，并在 Worker 启动时通过 `UnixRelayNetworkGuard`。

### 7.5 Docker Compose candidate

静态候选及其 fail-closed 占位值、UID/GID、host path、tool-bundle 与检查流程见 [`deployment/README.md`](../deployment/README.md)。关键边界：

- `sandbox` 使用 `network_mode:none`；`relay` 与 `worker` 都共享 `service:sandbox`，不加入普通 Docker network。
- 只有 `egress-proxy` 加入 outbound bridge；Worker 通过 loopback relay → 只读挂载的 UDS 到达 proxy。
- 四个服务均 UID/GID 10001、read-only rootfs、drop ALL capabilities、`no-new-privileges`，并有 PID/CPU/memory/tmpfs/nofile 上限。
- Worker 另声明 `RLIMIT_CORE` soft/hard 为零；目标 host 的 `/proc/sys/kernel/core_pattern` 还必须可读且不得以 `|` 开头，因为 pipe collector 不由该 Worker limit 单独排除。可选 Cookie override 只修改 Worker，并由 metadata-only preflight 先验证 protected-root non-overlap、完整 ancestor 与 ACL。
- checked-in env、image digest 与 host policy 故意不可用；不经人工替换不会启动真实 Worker。
- Compose 没有控制面 service，也没有发布端口；FastAPI 继续只在宿主 loopback 运行，避免在无认证时放宽 bind guard。
- `wheel_builder` 与 `runtime` 的两个 Python `FROM` 都直接写死同一个 `python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`，没有可覆盖它们的 Python image ARG；tool bundle 仍必须以受审阅的 `name@sha256:...` 提供。Registry 可用性、目标 platform manifest、镜像 provenance 与批准记录仍是发布边界。
- `pyproject.toml` 与 `requirements.build.in` 精确固定 `hatchling==1.27.0`；`requirements.build.lock` 固定 build closure，`requirements.runtime.lock` 固定从 `uv.lock` 导出的 runtime closure。两份 lock 都是 exact-version + SHA-256，可能同时列出 upstream wheel 与 sdist hash；实际 `pip download/install --only-binary=:all:` 会拒绝 sdist，不能把 lock 中的 sdist hash 写成执行许可。
- 唯一允许普通 Python package 网络访问的是 hash-checked `pip download --no-deps --only-binary=:all: --require-hashes`。build dependency install、`pip wheel` 和 runtime install 都使用 `RUN --network=none` 与 `--no-index`；项目 wheel 以 `--no-build-isolation --no-deps` 构建、按精确 `video_download_control-0.28.0-py3-none-any.whl` 路径复制/安装，最后执行 `pip check`。Base/tool image registry resolution 属于 Docker 自身的独立联网输入。

目标 Linux 必须提供绝对 `/usr/bin/python3` 且该解释器实际为 Python 3.12+；runner 不通过 `PATH` 解析它，Cookie rotation 另须验证绝对 `/usr/bin/mv` 是支持 `-fT` 的 GNU coreutils。可执行清单和默认只读 runner 见 [Linux/Docker acceptance](../validation/linux-docker-acceptance.md)。默认 `--preflight` 不 build、不启动/停止容器、不写数据库；mutation mode 还必须以 effective UID 0 运行，并显式给出 `--execute --authorize I_ACCEPT_TARGET_LINUX_MUTATIONS`。完整执行比产品 contract 更窄：只接受 checked-in、reviewed `run-candidate-worker.sh` wrapper 与其精确的 Worker-only read-only root/mapping binds；直接逐文件 `--cookie-source`、通用 command override、任意非 Worker service 变化都被拒绝。完整合成验收还要求六个不同的 `acceptance-*` ref/source，这不是使用真实 Cookie 的授权。

传给 runner 的 private env 和可选 private Compose override 必须各自为 canonical、single-link、`root:root`、mode `0600`、base-ACL-only regular file；直到 `/` 的 ancestor 必须 root-owned、无 symlink、无 group/world write 且只有 base ACL。Egress policy 必须为 canonical、single-link、`root:10001`、mode `0440`、1–16384-byte、base-ACL-only regular file，并具有同样安全的 root-owned ancestor。Policy 必须与 repository、data/socket/recovery roots、private env/override 双向不重叠。runner 对这些输入记录 device/inode、size、high-resolution mtime/ctime snapshot，并在相关 service 每次 start/restart 前重新验证；Cookie validator 也在执行边界重跑。Runner 同时在 preflight 和 mutation checkpoints 重新读取 `/proc/sys/kernel/core_pattern`；不可读或首字符为 `|` 时 fail closed。必须在启动前通过目标 host 的受控配置停用 pipe collector 或改用审阅过的 non-pipe pattern；`RLIMIT_CORE=(0, 0)` 不能单独证明 host collector 不会接收进程内存。

Full mode 只把随机 local build tag 用作新构建的初始名称；构建后立即解析并验证不可变 `sha256:...` image ID。Runner 将该 ID 写入重新取得的 effective Compose JSON，递归拒绝任意 string key/value 中的 `$`，再在 private env 同目录创建 `.vdc-effective-<run-id>.json` 冻结输入。该文件必须保持 `root:root 0600`、single-link、base-ACL-only 与原 identity/metadata snapshot。Runner 用冻结文件二次执行 `docker compose config`，要求结果与原 JSON 深等值、重新通过完整安全 validator，并再次核对冻结文件 snapshot；之后 Compose mutation 只使用冻结文件，direct `docker run` 也只使用同一 ID，runtime inspect 要求每个 container 的 `Image` 精确等于该 ID，并在 mutation checkpoints 重验它。另一个 network-none direct run 会按 exact version 核对 `requirements.runtime.lock` 的 14 个 distributions 与 `video-download-control==0.28.0`，并拒绝 runtime 中出现 build-only 的 `hatchling`、`packaging`、`pathspec`、`pluggy`、`trove-classifiers`。即使 tag 随后 rebind，也不能改变本次验收对象。上述逻辑尚未在 target Linux execute。正常退出只在 identity/snapshot 未变时删除冻结文件；crash/强制终止可能留下含部署路径/config 的文件，须在受保护目录按 exact path 与 identity 人工审计，确认未变后再定点清理，不能泛化删除。

最外层必须由 clean trusted root launcher 以 empty/scrubbed environment 和 absolute trusted path 启动 runner。`#!/bin/bash -p`、脚本内 `unset` 与固定 `PATH` 都只能在 process/interpreter 已启动后生效，不能把 inherited `BASH_FUNC_*`、`LD_PRELOAD` 等 pre-body loader/interpreter 行为变成可信输入，也不能保护从不可信外层环境启动的子 shell。Endpoint 的只读判定遵循官方 precedence：非空 `DOCKER_CONTEXT` 高于 `DOCKER_HOST`，否则读取当前/default context；无论来源都必须解析为 local Unix Linux daemon。Execute 为避免重定向 daemon/build/config，明确拒绝 inherited `DOCKER_CONTEXT`、`DOCKER_HOST`、`DOCKER_CONFIG`、`DOCKER_CERT_PATH`、`DOCKER_TLS_VERIFY`、`BUILDKIT_HOST`、`BUILDX_BUILDER`、`COMPOSE_FILE`、`COMPOSE_PROJECT_NAME`、`COMPOSE_PROFILES`，此时 default context 仍必须通过同一 local-Unix/Linux 检查。

Standalone Cookie validator 不得 `source` dotenv；按 [`deployment/README.md`](../deployment/README.md) 使用绝对 `/usr/bin/env -i`，仅传五个 validator 变量，再用绝对 `/bin/sh` 调用受审阅脚本。Cookie rotation 的整个 mutation script 也必须由同类 clean trusted root launcher 执行，不能只隔离两个 validator；实际 rename 必须调用已验证的绝对 `/usr/bin/mv -fT`，不得解析 exported `mv` function 或 `PATH` shim。Rename 后、normal validator/Worker recreate 前，可信 helper 只接收 whitelist 后的 platform parent path，打开该目录并 `fsync` directory fd；失败时 Worker 保持 stopped。Atomic rename 不等于 crash-durable，缺少 directory `fsync` 时断电可能回滚目录项。当前 Windows 没有 Docker，不能把脚本存在或 syntax/test 通过写成运行验收。

依赖变更时，在受审阅分支刷新并审计 lock；不要在部署主机临时解锁或让 pip 自行求解：

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

人工审阅 `pyproject.toml`、`requirements.build.in`、两份生成 lock 与 `uv.lock` 的一致性，以及每个版本、wheel/sdist hash 来源、target wheel availability、publisher/provenance 和 license；命令通过不等于这些批准已完成。

### 7.6 Windows 短链直连与 POSIX egress gate

短链支持四类需要展开的入口：`t.co`、`b23.tv`、`v.douyin.com`，以及 TikTok `vm.tiktok.com` / `vt.tiktok.com`；`youtu.be/{id}` 可直接规范化。TikTok 每一跳只允许精确 hostname `vm.tiktok.com`、`vt.tiktok.com`、`tiktok.com`、`www.tiktok.com`、`m.tiktok.com`，不会接受任意子域、相似后缀或其他 TikTok host。两条路径都逐跳检查 HTTPS、公开 IP、numeric peer、TLS SNI/证书和 redirect，不注入 Cookie。

Windows 一体化 `video-download-local-app --allow-direct-network` 显式启用进程内受控直连。每个请求创建短生命周期 transport，使用临时 shared key、内存 replay store 与既有 numeric TLS connector；不监听 socket，也没有常驻 event-loop 线程。transport 默认最多 4 个在途请求，容量满时快速拒绝；关闭后拒绝新请求，已经接纳的请求继续受其时限约束。这不是独立 egress 进程、network namespace 或权限隔离；本轮只有离线/模拟网络回归，没有真实 DNS/TLS/平台证明。通用 `video-download-control` 不自动开启此本机路径，`--check` 也不发短链请求。

以下配置与命令仅适用于独立 POSIX UDS 路径。通用控制面默认 `VDC_ENABLE_SHORT_LINK_RESOLUTION=0`，即使配置路径也不会启用；关闭时 Batch 以 `short_link_resolution_required` 终止。显式启用必须同时设置绝对、规范化的 `VDC_SHORT_LINK_TRANSPORT_SOCKET` 与 `VDC_SHORT_LINK_ATTESTATION_KEY_FILE`；这组 UDS 配置在 Windows 上仍 fail closed，不与一体化进程内直连混用。

控制面与 egress 进程必须以同一 effective UID 运行。准备一个 owner 为该 UID、mode `0700` 的 socket parent；socket leaf 由服务创建为 `0600`。Shared key 必须是该 UID 所有、单 hard link、无 group/other permission、32–4096 bytes 的原始随机 bytes；不要添加换行或将 key 放在 argv/env。Replay directory 必须是同 UID `0700` plain directory，其 ancestors 只能由 root/该 UID 所有且不得有非 sticky group/world write。两个进程读同一个 key；不要复制到 data root、日志、backup 或容器 build context。

由受信 supervisor 先启动 egress side：

```bash
umask 077
install -d -o 10001 -g 10001 -m 0700 /run/vdc-short-link
install -d -o 10001 -g 10001 -m 0700 /var/lib/vdc-short-link/replay
dd if=/dev/urandom of=/etc/vdc-short-link.key bs=32 count=1 status=none
chown 10001:10001 /etc/vdc-short-link.key
chmod 0600 /etc/vdc-short-link.key

video-download-short-link-egress \
  --unix-socket /run/vdc-short-link/egress.sock \
  --shared-key-file /etc/vdc-short-link.key \
  --replay-directory /var/lib/vdc-short-link/replay
```

这些路径的实际 owner 必须与两个进程的 effective UID 一致；上述命令只是形状示例，不能在未审阅目标路径/身份时照抄。服务拒绝 stale socket，不会盲删现有路径；SIGINT/SIGTERM 会关闭连接，并且只在 parent 与 socket inode identity 均未改变时移除自己的 leaf。可信 supervisor 只能在确认类型、owner、无 listener 和 incident evidence 后清理 stale socket。

服务启动并由受保护的本地探针确认后，才向 loopback control plane 注入：

```bash
export VDC_ENABLE_SHORT_LINK_RESOLUTION=1
export VDC_SHORT_LINK_TRANSPORT_SOCKET=/run/vdc-short-link/egress.sock
export VDC_SHORT_LINK_ATTESTATION_KEY_FILE=/etc/vdc-short-link.key
video-download-control
```

POSIX 协议使用 canonical JSON、domain-separated HMAC-SHA256、fresh 256-bit nonce、短有效期、clock-skew window、request hash/expiry binding 与 crash-durable one-time replay marker。请求方法/header/address 数、request/response frame、header/body、连接数和时限均有 hard cap；普通 audit 和跨边界 error 只有方向化 allowlisted reason，未知值降级为 `transport_failure`。

两条路径的 Resolver 默认每链最多 5 跳、16 个 DNS answer、每跳 5 秒、DNS 3 秒、单链总计 15 秒，并对每个 URL 只做一次每跳 DNS。Batch 对所有唯一短链共享 15 秒 aggregate budget；内建 `ControlledShortLinkResolver` 每次都会收到当前剩余 budget，并把自身总时限压到该值，budget 到期后也不再启动新展开。短链 transport 的 4 个在途槽与媒体 Worker 的总并发 2 / 单平台 1 是不同限制。注入自定义 resolver 是 trusted-contract boundary：它必须实际遵守传入的 `timeout_seconds`；同步 Python 调用无法在它无视 contract 时强制抢占，因此不能把恶意/失效的 injected resolver 算入 hard-deadline 保证。

DNS 通过最多四个 daemon worker slot 包围 blocking resolver；底层 `getaddrinfo` 不能被 Python 取消，四个调用都卡住后该 resolver 实例会 fail-fast，直到线程返回或进程重启。Replay marker 的文件系统操作也从 event loop 卸载到有界线程/slot，并受响应时限约束，但已经进入 kernel/文件系统的 blocking I/O 不能被 Python 强制取消。Replay directory 必须位于本机可靠文件系统，不得放在可能无限卡住的网络/FUSE share；否则一个卡住的操作会持续占据对应 slot，直到 I/O 返回或进程重启。这些有界降级都不等于 resolver/replay process isolation 或高可用保证。

短链 egress CLI 尚未加入 Docker candidate；不要把它与下载 proxy 的 relay/socket 混用，也不要在控制面 host 仍有未审阅网络旁路时称为隔离完成。真实 AF_UNIX owner/mode、HMAC clock、process restart/replay、TLS certificate/SNI、DNS rebinding、redirect chain、SIGTERM/stale socket 和批次最坏耗时都属于目标 Linux acceptance。

## 8. 数据布局与持久性

默认布局：

```text
data/
  control.sqlite3
  control.sqlite3-wal       # 运行中可能存在
  control.sqlite3-shm       # 运行中可能存在
  logs/runtime-*.jsonl*     # 脱敏、轮转、非业务权威的排障日志
  temporary/{job_id}/{attempt_id}/
  assets/.staging/
  assets/{asset_id}/
    original/source.ext
    metadata/source.json
    metadata/manifest.json
    thumbnails/thumbnail-0000.ext
    captions/caption-0000-language.ext
```

上传域位于独立的 sibling root；普通 Windows Start 默认为同一父目录下的 `data-uploads`：

```text
data-uploads/
  uploads.sqlite3             # Upload Schema 3
  uploads.sqlite3-wal         # 运行中可能存在
  media/                      # 登记的上传视频受管副本
  assets/                     # 登记的上传封面受管副本
  incoming/                   # 可丢弃的导入临时数据
  private/                    # 登录秘密与操作临时状态，不进入上传备份
  runtime/                    # 可重建上传运行时，不进入上传备份
.<root-name>.activity.lock    # 位于上传根旁，不在根内
```

两套根、数据库、媒体和备份格式互不包含；上传不复用下载 CredentialProfile/Cookie，下载备份也不会遍历 sibling `data-uploads`。

原件、缩略图和平台既有字幕先进入同一 staging 资产目录，分别完成受控路径、regular-file、identity、size 与 SHA-256 检查，再以一次目录 rename 发布；Artifact/Caption 与 MediaAsset 在同一 SQL 事务登记。数据库中的 `asset_commit_intents` 覆盖发布与登记之间的崩溃窗口。Caption 当前以 `origin=platform` 表示非本地生成，尚未细分人工/自动。

## 9. 可执行备份

下载和上传使用两套互不包含的备份格式。`video-download-backup` 处理下载 Schema 11 与下载资产；`video-upload-backup` 使用备份格式 2 处理旁路 `data-uploads` 的 Upload Schema 3、登记受管视频/封面及非秘密账号元数据。任何一个命令成功都不能证明另一套数据已备份。Linux candidate 仍仅是下载 Worker 路径，不提供 Windows 上传运行时的跨平台验收。

对 9.1～9.3 的下载备份而言：**本节命令不包含这些上传数据**；对 9.4 的上传备份而言，它也不包含下载数据库或下载资产。

`video-download-backup create` 在 Iteration 0.5 引入；当前 v0.28.0 版本只接受通过 readiness 的精确 Schema 11 数据库。它不会删除或改写源数据，并在与目标同一父目录先构建隐藏 staging directory；所有文件写入、SHA-256、内部审计与目录同步成功后才以一次 rename 发布最终备份目录。目标已存在时会拒绝覆盖。

### 9.1 一致性边界与停机要求

本节 9.1～9.3 描述下载备份。备份在源数据库上持有 SQLite `BEGIN IMMEDIATE` 写保留，同时执行 SQLite online backup 并复制已发布的受管文件。该边界会阻止并发数据库 writer；每个复制源还必须保持 regular-file identity、size 与 metadata 稳定。备份会 fail closed 于：

- 源数据库 readiness 失败、不是精确 Schema 11、claim gate singleton 或 capability ledger 结构/identity/digest/route/decision chain 不一致，或存在尚未协调的 `asset_commit_intents`；
- source / target 路径重叠，目标已存在，或任一路径经过 symlink / reparse point；
- 受管数据含 link、hard link、special file、不稳定文件，或资产数据库、Artifact/Caption、asset manifest 与原件/辅助文件的 size / SHA-256 不一致；
- 存储、fsync、校验或最终 rename 失败。

SQLite 主文件及 `-wal`、`-shm`、`-journal` 不会作为普通数据复制；`payload/database/control.sqlite3` 是 online backup 产生并通过 `quick_check` 的静态快照。以下 volatile 工作树被有意排除：

- 顶层 `logs/`：有界轮转的 best-effort 排障事件；它不是业务恢复数据，需留存时应另行受控复制；
- `temporary/`：进行中 Attempt 的下载、临时 Cookie 副本和工具输出；
- `assets/.staging/`：尚未完成 durable commit-intent 协调的资产。

Schema 11 的 discovery/graph/generation、Schema 9 capability archive、Schema 10 capability ledger 与 `worker_claim_gate` 都随 SQLite 一致快照保存；已发布不可变资产另按 manifest 复制。备份前应优先正常停止 `video-download-local-app`，让 gate stop 先于 Worker/control 退出；独立 Worker 模式仍须先停 Worker、再停控制面，以避免长期阻塞 writer，并让活动 lease 与已排除临时输出的边界更清晰。恢复出的历史 gate 状态不是启动授权：新 local-app 必须先用自己的新 `run_id` prepare 为关闭状态，完成预检后才能 activate。

### 9.2 秘密排除边界

业务数据库只保存 credential profile metadata 与不透明 `secret_ref`；部署侧真实 Cookie 文件和外部 secret store 必须位于 data root 之外，`video-download-backup` 不会发现或备份它们。备份 CLI 会复制 data root 下除精确顶层 `logs`、`temporary` 和 `assets/.staging` 之外的普通受管文件，因此**不得为了“顺便备份”而把 Cookie、token 或密钥放进 data root 或日志目录**。秘密应使用独立、加密、访问受控的备份流程，并单独演练恢复权限、轮换和吊销。

### 9.3 创建命令

三个参数都必须是显式、绝对、规范化路径。source data root 与 source database 必须已存在；backup parent 必须已存在，而最终 target 必须不存在，且不得为卷根、source 的父/子路径或 link/reparse 路径。

```powershell
$DataRoot = (Resolve-Path -LiteralPath "C:\vdc-data").Path
$Database = (Resolve-Path -LiteralPath "C:\vdc-data\control.sqlite3").Path
$BackupParent = (Resolve-Path -LiteralPath "D:\vdc-backups").Path
$BackupTarget = Join-Path $BackupParent (Get-Date -Format "yyyyMMdd-HHmmss")

if (Test-Path -LiteralPath $BackupTarget) {
  throw "Backup target must not exist"
}

uv run video-download-backup create `
  --source-data-root $DataRoot `
  --source-database $Database `
  --backup-target $BackupTarget
```

成功时 stdout 是单行 JSON，包含 `status=ok`、`operation=create`、`schema_version=11`、`file_count`、`total_bytes`、`manifest_sha256` 与 `backup_root`；失败时退出码为 2，并向 stderr 输出不含源内容的有界错误。

备份目录包含：

```text
backup-target/
  backup-metadata.json
  backup-manifest.json
  backup-manifest.sha256
  payload/database/control.sqlite3
  payload/data/...                # 已发布的受管数据；不含 volatile 工作树
```

`backup-manifest.json` 以 SHA-256 覆盖 metadata 与每个 payload 文件的规范相对路径、字节数和内容；`backup-manifest.sha256` 覆盖 manifest 自身。这能检出意外损坏，但同目录 hash sidecar 不是数字签名，不能独自证明攻击者未同时替换 manifest 与 sidecar。生产备份应复制到独立介质，并在独立受保护位置保存或签名 manifest hash。

### 9.4 Upload Schema 3 / 备份格式 2 停机备份

上传备份只处理上传根。运行中的当前控制面从 FastAPI lifespan 开始即在 sibling `.<root-name>.activity.lock` 持 shared lease，即使上传服务尚未按需初始化也一样；started active 和 standby `UploadService` 也持续持 shared lease，停止状态下的本地读写只持短 shared lease。`video-upload-backup create` 在源上传根取得 exclusive lease，并保留旧 `.worker.lock` 兼容检查，直到稳定 main/WAL 字节快照、登记媒体复制、业务审计和最终发布完成。

因此，先正常停止使用该上传根的**所有当前应用以及 active/standby 服务**。锁冲突会以固定错误拒绝创建；它不是替代停机流程的在线快照承诺。旧版应用、手工 SQLite 连接和自写文件 writer 若不采用当前 activity-lock 合同，不会被完整协调，必须单独停止并确认静止。不要删除、硬链接或改写 sibling lock 文件来绕过拒绝。

```powershell
$UploadRoot = (Resolve-Path -LiteralPath "C:\vdc\data-uploads").Path
$UploadBackupParent = (Resolve-Path -LiteralPath "D:\vdc-backups").Path
$UploadBackupTarget = Join-Path $UploadBackupParent "upload-20260907-020000"

if (Test-Path -LiteralPath $UploadBackupTarget) {
  throw "Upload backup target must not exist"
}

uv run video-upload-backup create `
  --source-upload-root $UploadRoot `
  --backup-target $UploadBackupTarget
```

命令只接受精确 Upload Schema 3。备份格式 2 的 payload 包含静态 `uploads.sqlite3`、非秘密账号历史/墓碑、sources、upload_assets、jobs、operations、requests/retry 关系，以及所有 `media_state=present` 且大小/SHA-256 与登记一致的受管视频和封面。它明确排除 `private/`、`runtime/`、`incoming/`、锁、账号登录秘密、操作临时文件、未登记文件以及不是 present 的媒体；当前格式没有“包含秘密”选项。WAL 核对只在独立临时目录打开 SQLite，不在源根创建 SHM。源、备份目标或 staging 路径重叠、文件不稳定、路径别名/link/reparse/hard link/alternate data stream、Schema/外键/业务关系异常或最终同步失败都会失败关闭并清理未发布 staging。格式 1 / Schema 2 备份只作为只读兼容输入，经 staging 精确迁移后恢复为 Schema 3；不会改写旧备份。

成功 stdout JSON 包含 `status=ok`、`operation=create`、`schema_version=3`、文件数、总字节、manifest hash 与备份根。该成功只覆盖上传域；仍须另跑 `video-download-backup` 才能保存下载 Schema 11 与下载资产。

## 10. 可执行独立根恢复演练

`video-download-backup restore` 只恢复到一个不存在的新根目录，不会覆盖现有数据。backup root 与 restore parent 必须已存在；restore data root 必须不存在，且不能与 backup root 重叠。restore database 必须是 restore root 内部的显式绝对路径。

```powershell
$BackupRoot = (Resolve-Path -LiteralPath "D:\vdc-backups\20260903-020000").Path
$RestoreParent = (Resolve-Path -LiteralPath "C:\vdc-restore-drills").Path
$RestoreRoot = Join-Path $RestoreParent "drill-20260903-020000"
$RestoreDatabase = Join-Path $RestoreRoot "control.sqlite3"

if (Test-Path -LiteralPath $RestoreRoot) {
  throw "Restore target must not exist"
}

uv run video-download-backup restore `
  --backup-root $BackupRoot `
  --restore-data-root $RestoreRoot `
  --restore-database $RestoreDatabase
```

恢复会在最终目录发布前完成以下 fail-closed 检查：

1. 验证 manifest hash sidecar、manifest header、metadata、entry 数量与总字节；
2. 拒绝 traversal、绝对路径、反斜线、重复/case-fold 冲突、未跟踪或缺失文件，以及 symlink / reparse / hard link / special file；
3. 对每个输入及恢复副本验证 regular-file identity、size 与 SHA-256；
4. 对恢复数据库执行 `PRAGMA quick_check`、`PRAGMA foreign_key_check`、精确 Schema 11 与应用 readiness 检查（包括 claim gate singleton、graph、Schema 9 archive、evidence/decision 不可变 trigger、identity/digest/route、CAS chain、current view 与 FK），并拒绝 pending commit intent；
5. 对 graph 数据执行跨表语义审计：canonical discovery hash、ordered relation、parent/child identity、active generation、target、Attempt 代际、ready target-to-asset 映射与 reuse donor 必须一致；失败只返回固定错误，不回显 selector/target；
6. 核对 `media_assets` 与实际发布目录、Artifact/Caption 语义、每个 asset manifest、original 与 thumbnail/caption 的路径、size 和 SHA-256；
7. 同步隐藏 staging tree 后一次 rename 发布。任何一步失败都会清理 staging，最终 restore root 保持不存在；backup 与 source 均不被修改。

成功 stdout JSON 包含 `status=ok`、`operation=restore`、Schema、文件数、总字节、manifest hash、恢复根与数据库路径。随后可在独立端口只对恢复副本做控制面检查：

```powershell
$env:VDC_HOST = "127.0.0.1"
$env:VDC_PORT = "8001"
$env:VDC_DATA_ROOT = $RestoreRoot
$env:VDC_DATABASE_PATH = $RestoreDatabase
uv run video-download-control
```

在另一个终端检查 `http://127.0.0.1:8001/health` 与批次/任务查询。若 `/health/ready` 因源快照中队列本就 paused 而返回 503，应先确认暂停原因与恢复盘空间，再按第 4.1 节恢复；不要直接改表。

### 10.1 历史本机演练证据与限制

2026-09-03 在当时的 Windows 开发机、Iteration 0.5 / Schema 7 代码上执行 `uv run pytest -q tests/test_backup_restore.py`，记录结果为 **14 passed**。其中主演练以 offline fake Worker 创建 ready asset，把数据库、原件和额外受管文件备份后恢复到不同的临时根目录，再验证 Schema 7 readiness、batch 状态以及恢复原件 SHA-256 与数据库记录一致；同一测试文件也实际调用当时的 `create` / `restore` CLI，并覆盖 payload/manifest 篡改、路径逃逸、link、目标碰撞、Schema 篡改、pending commit intent 与未跟踪文件的拒绝路径。命令、文件哈希和边界记录在 [Iteration 0.5 recovery evidence](../validation/backup-restore-drill-iteration-0.5.md)。不得把这条历史记录改写为后续 v0.10.0 / Schema 8、v0.11.0–0.12.0 / Schema 9、v0.13.0–0.15.0 / Schema 10、后续 Schema 11 版本 或 graph-v2 恢复演练。

这只是本机小型临时数据的自动化独立根功能证据，不包含生产媒体量、NAS/网络文件系统、跨卷性能、容器 UID/GID、加密/offsite 介质、灾难主机、RTO/RPO 或人工值班流程。生产前仍须用真实容量和实际部署身份完成、记录并定期重复恢复演练；记录至少包括应用 revision、实际应用版本与实际数据库 Schema、备份 ID、文件数/字节、manifest hash、存储介质、开始/完成时间、恢复检查结果、失败与处置。

### 10.2 Iteration 0.6 / Schema 8 离线 graph 演练

2026-09-03 在同一 Windows 开发机执行当时的 `tests/test_backup_restore.py` 与 `tests/test_observability.py`，结果为 **19 passed**；当时全套为 **615 passed**。主演练使用 `ScriptedGraphFakeAdapter` 创建包含 generation 2 与跨 Input ready-asset reuse 的 graph 数据，备份后恢复到不存在的临时独立根，并逐表核对 discovery、relation、target、active generation、Attempt 与 reuse 字段。篡改用例会同时更新 payload 数据库并重算外层 manifest/hash，验证 active pointer、selector target、expected-media/asset、child identity 与 reuse donor 的跨表不一致仍被固定、不泄露内部 key 的错误拒绝，最终恢复根保持不存在。命令、代码哈希和明确的非 Stage 0 边界见 [Iteration 0.6 offline graph evidence](../validation/iteration-0.6-graph-v2-offline-evidence.md)。

该 0.6 记录只证明当时代码能处理小型离线 Schema 8 graph 快照，不证明 Docker/Linux 文件权限、真实媒体容量、NAS/跨卷 durability、offsite 介质、RTO/RPO、真实 Cookie 或灾难主机切换。生产验收必须另行记录对应部署 revision、Schema 8 baseline、制品/tool digest、备份 ID、文件数/字节、manifest hash、存储介质、开始/完成时间、恢复检查与失败处置；不得沿用本机临时目录结果。

### 10.3 Upload Schema 3 新根恢复

上传恢复使用上传专属格式，只接受不存在且与备份根不重叠的新目标。restore 在目标 sibling activity lock 上持 exclusive lease，覆盖 staging 复制、恢复策略、二次审计、目录同步和一次 rename 发布；目标若已由当前应用/API/active 或 standby 服务使用，立即拒绝并保持目标不存在。旧版本或手工 writer 不采用当前锁时仍须由操作者另行排除。

```powershell
$UploadBackupRoot = (Resolve-Path -LiteralPath "D:\vdc-backups\upload-20260907-020000").Path
$UploadRestoreParent = (Resolve-Path -LiteralPath "C:\vdc-restore-drills").Path
$UploadRestoreRoot = Join-Path $UploadRestoreParent "data-uploads-20260907-020000"

if (Test-Path -LiteralPath $UploadRestoreRoot) {
  throw "Upload restore target must not exist"
}

uv run video-upload-backup restore `
  --backup-root $UploadBackupRoot `
  --restore-upload-root $UploadRestoreRoot
```

恢复在发布前核对 manifest sidecar、规范相对路径、文件 inventory/大小/SHA-256、link/reparse/hard link/alternate data stream、精确 Upload Schema 3、`quick_check`、外键，以及账号、来源、封面、任务、operation、request 与 retry 的业务关系。当前格式中原 `running` 任务恢复为 `unknown / interrupted_result_unknown`，原 `queued` 任务恢复为 `draft / restart_confirmation_required`；旧格式任务同时发生平台参数或标签迁移时改用 `legacy_metadata_interrupted_result_unknown` / `legacy_metadata_restart_confirmation_required`，让页面同时说明中断状态和迁移复核。未完成账号操作变为 `failed / operation_interrupted`；活动账号原 `ready`/`checking` 变为 `unchecked / account_missing`。断开账号墓碑、历史任务、平台参数和定时值保留，媒体恢复不会自动执行旧任务。

恢复过程不构造上传 backend，不读取账号秘密，不登录、扫码或发起上传。成功后先以独立端口/独立 app root 打开恢复副本，核对账号墓碑、来源/封面状态、任务与 storage summary；重新登录仍由测试员显式执行，任何 `unknown` 任务仍须先到平台后台核对。当前 0.28.0 只完成本机 synthetic/offline 工程演练，真实容量、异机/offsite、NAS、RTO/RPO 和人工值班流程仍为 NOT RUN。

## 11. 升级与回滚（Schema 11 forward-only）

Schema 11 在保留 Schema 10 capability ledger 的基础上增加唯一 `worker_claim_gate`。Schema 10→11 迁移只创建该表和关闭的初始 singleton；它不会把恢复出的旧 supervisor 状态视为启动授权。Schema 10 本身是在 Iteration 0.13.0 将 Schema 9 的可写 `platform_capabilities` 封存为只读 `capability_legacy_schema9`，并建立 evidence/decision ledger 与只读 current-head view。生产升级必须分别演练迁移前与迁移后的恢复点：

1. 记录待迁移应用 revision、Schema、依赖锁与 Worker/tool digest；停止 Worker 和控制面。
2. 用与原 Schema 8、9 或 10 精确兼容的历史工具创建备份，并用同一版本恢复到独立新根。v0.28.0 的下载 restore 只接受 Schema 11，不能直接恢复旧下载备份。
3. 只在恢复副本上用 v0.28.0 启动迁移；确认 marker 精确为 1–11、`quick_check` / `foreign_key_check` 和 readiness 通过，`worker_claim_gate` 只有 `id=1` 的关闭初始行，并抽查 Schema 9 archive、evidence/decision/current view 及资产状态未漂移。
4. 检查 `/health`、Batch/Input/Job/asset API，以及三个 capability 分层端点。用 `video-download-local-app --check` 验证 gate 只 prepare、不 activate且无 claim；使用 synthetic 私有 CSV 演练 import、approve、revoke、stale revision 与 history，但不要把 synthetic 决定带入生产库。
5. 使用 v0.28.0 创建 Schema 11 baseline，并恢复到另一个不存在的独立根；核对 claim gate singleton、archive、evidence、完整 decision chain、current view、资产与 manifest。恢复后再启动 local-app 时，确认新 `run_id` 先 prepare 为关闭状态。
6. 最后才在维护窗口切换，同时保留迁移前后各自匹配的应用、锁文件和恢复工具。回滚只能整体恢复已经用历史版本实际演练过的旧数据库及匹配资产树。

旧程序不得打开 Schema 11。不得原地删除 migration marker、`worker_claim_gate`、trigger、ledger 表或 view，也不得把旧数据库覆盖到新的资产树。没有经过实际恢复验证的迁移前备份时，只能修复并向前升级，不能声称可安全回滚。

### 11.1 历史 Schema 7→8 记录

以下保留 0.6–0.10 时期的 point-in-time Schema 8 操作边界，不是当前 Schema 11 的升级步骤：

Schema 8 增加不可变 discovery、重建 relation/job 关联结构，并加入 active snapshot、run generation 与 `partial_success_count`。迁移是 forward-only，没有自动 downgrade；Schema 8 migration 只接受 Schema 7 起点。升级前后必须分别保留兼容恢复点：

1. 迁移前记录 v0.5.x revision、Schema 7、依赖锁文件和 Worker image/tool digest；使用**与 Schema 7 兼容的旧版本工具**制作并恢复验证完整数据库 + 资产备份。当时的 v0.10.0 `video-download-backup` 只接受 Schema 8，不能用来制作 Schema 7 的迁移前回滚点。
2. 在数据副本上用 v0.10.0 启动迁移。确认 migration marker 为 8、`PRAGMA quick_check` / `foreign_key_check` 通过、readiness 正常；抽查 legacy relation 已进入 deterministic legacy discovery，旧 flat-v1 Batch/Job/Asset/manifest 仍可读取。不要设置 `VDC_ENABLE_X_GRAPH_V2=1` 来测试数据库升级。
3. 对迁移副本读取 `/health`、Batch/Input/Job API、metrics，并验证 input cancel / terminal-only rediscover 的权限和 409 边界；graph 行为只使用测试内 `ScriptedGraphFakeAdapter`，不运行 candidate real Worker，也不把结果记为 Stage 0。
4. 生产数据迁移成功后，按第 9 节创建新的 Schema 8 baseline，并按第 10 节恢复到独立新根；目标部署的 Schema 8 恢复检查必须单独记录，不能沿用第 10.1 节的 Schema 7 历史证据或第 10.2 节的本机临时目录结果。
5. 最后才在维护窗口切换，并同时保留与 Schema 7 和 Schema 8 备份分别匹配的应用代码、依赖锁与恢复工具。

只支持 Schema 7 或更低版本的旧代码不得打开 Schema 8 数据库。回滚 v0.5.x 只能同时恢复迁移前、与 Schema 7 兼容且已演练的数据库和匹配资产树；不得原地删除 migration marker、trigger、column 或 graph table，不得把旧数据库覆盖到新的资产树，也不得把 Schema 8 备份交给旧程序。若不存在经过验证的迁移前兼容备份，只能修复并向前升级，不能声称可安全回滚。

### 11.2 Stage 0 CSV v3 与 capability 决定

从 [`validation/sample_manifest.template.csv`](../validation/sample_manifest.template.csv) 和 [`validation/results.template.csv`](../validation/results.template.csv) 复制模板到 Git 仓库外的私有目录。两个 CSV 都必须保留 v3 精确且不重复的表头、正确行宽并填写 `job_kind`；results 的 `product_version` 必须填写实际执行包输出的完整 product identity。旧表头、重复列、行宽不符、缺字段、不安全 identity token、裸版本（例如 `0.19.0`）、build hash 不匹配或与 manifest 不符的结果都会 fail closed。

先在实际执行验证的同一安装/checkout 中读取 identity；输出不含本机路径：

```powershell
uv run video-download-validation --print-product-identity
```

`expected_output_count` / `observed_output_count` 按 route 解释：`download` 是已发布且完成完整验证的媒体资产数；`discover` 是不可变 discovery snapshot 中唯一 child/source item 数。不要把 parent `discover` 成功记成媒体下载成功，也不要把未验证临时文件计入 `download`。

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

一个证据单元固定为 `platform × source_type × job_kind × adapter × downloader_version × environment × product_version`；这里的 `product_version` 是 `<当前版本>+build.sha256.<64 hex>` 形式的完整 build identity，不是裸版本。至少 10 个公开正向样本、独立负向样本和同一身份下最新连续三轮完整运行都达到门槛后，评估才为 qualified；更新但不完整的最新轮次会使其 fail closed。来源样本按规范的 `platform/source_type/source_id` 去重，外层保留 `job_kind`；TikTok 同一 video ID 的不同 handle/host 不算独立样本。Bilibili 同一投稿可同时表示为 BV/av，Stage 0 暂时只接受 BV 以防双算；普通下载仍接受 BV 和 av。报告只含 aggregate，不含 URL、来源 identity hash、sample/run ID。

导入必须使用现有且已经就绪的 Schema 11 数据库、当前完整 build identity 和显式 environment key；CLI 不创建或迁移数据库。数据库路径必须是绝对、规范、单 hard-link 的普通文件，并在每次连接前后重验文件 identity，避免 hard-link 别名形成不同 WAL/SHM 锁域。它从 CSV 重新计算，不导入 Markdown status。导入后仍是未审批，必须用另一条 CAS 命令批准：

```powershell
$Database = (Resolve-Path -LiteralPath ".\data\control.sqlite3").Path
uv run video-download-capabilities --database-path $Database import `
  --manifest C:\private\samples.csv --results C:\private\results.csv `
  --environment-key windows-x64-direct-no-cookie
uv run video-download-capabilities --database-path $Database list --kind evidence
uv run video-download-capabilities --database-path $Database approve `
  --evidence-id REPLACE_WITH_EVIDENCE_ID --expected-revision 0 `
  --reason-code stage0-reviewed
uv run video-download-capabilities --database-path $Database list --kind decisions
uv run video-download-capabilities --database-path $Database history `
  --identity-key REPLACE_WITH_IDENTITY_KEY
uv run video-download-capabilities --database-path $Database revoke `
  --evidence-id REPLACE_WITH_CURRENT_EVIDENCE_ID --expected-revision 1 `
  --reason-code regression
```

`revoke` 只允许当前已批准 evidence，并要求最新 revision；旧 build 的历史批准仍可撤销，但旧 evidence 不可批准为当前 build。相同证据撤销后不可重放，需产生并审阅新 evidence。approve reason 只允许 `stage0-reviewed` / `replacement-reviewed`，revoke reason 只允许 `regression` / `superseded` / `operator-withdrawn`；`list` / `history --limit` 范围为 1–500。CLI stdout/stderr 使用稳定的单行脱敏 JSON，不回显输入路径、样本值或底层异常；退出码为 0 成功、2 输入错误、3 策略拒绝、4 未找到、5 revision 冲突、6 readiness/完整性/I/O/构建漂移或不可读、70 未预期内部错误。构建摘要覆盖 package 内所有非 PEP 3147 cache-pyc 的普通文件，拒绝 link/reparse/special file，并在读取前后比较完整 inventory；导入和 approve 在事务提交前再次核对身份，漂移时回滚。evidence/decision 的 update/delete/replace 均被拒绝，readiness 复核 guard 与 ledger 语义。CSV 可被管理员手写，普通 SHA-256 也不能抵抗攻击者同时改库并重算摘要；本机制保证应用规则、内部一致性、幂等和审计链，不构成密码学 attestation。

## 12. 生产启用闸门

以下条件全部完成前，不得把 Windows 本机直连 Worker 或 Linux candidate 称为生产就绪，也不得把 Linux candidate 从受控验收 gate 提升为常规真实下载服务：

- 可复现 Docker/Compose 或等价 service 定义；所有外部镜像按批准 digest 固定，验收构建解析成不可变 local image ID 后由 frozen Compose、direct runs 与 runtime inspect 全程绑定该 ID；
- 目标 Docker daemon 自带 frontend 至少支持 Dockerfile 1.3，并实证 BuildKit 对所有 post-download `RUN --network=none` 步骤落实断网；记录 daemon/frontend/BuildKit 版本与配置；
- Python build/runtime exact-hash locks、target wheel availability、base/tool registry availability、镜像与包 hash provenance 均经人工批准；不得因 lock 同时含 sdist hash 而允许 source build；
- Linux Worker 只有 loopback 网络路径，所有出站只能经受控 proxy / UDS；
- pinned yt-dlp、FFmpeg/ffprobe、必要 JS runtime 与 `yt-dlp-ejs`，并通过版本漂移检查；
- CPU、内存、进程、任务时长、下载/输出字节、磁盘水位和连接数限制；
- Worker `RLIMIT_CORE=(0, 0)` 与 target host 非 pipe、可持续重验的 `/proc/sys/kernel/core_pattern`；不得只凭容器 limit 排除 host crash collector；
- effective-root acceptance 使用固定 Python 3.12+ `/usr/bin/python3`、无 inherited Docker/Compose/BuildKit 控制变量的 local Unix Linux default context，并完成 frozen Compose 深等值/复验、正常清理及 crash-remnant exact-identity 演练；
- 生产 secret provider、Compose 只读 Cookie source mount、最小权限、到期验证、轮换和外部吊销演练；并以 credential sidecar、per-platform Worker 或 per-attempt mount namespace 消除单一 Worker 对整棵多平台 source root 的可读性；数据库/Adapter 接线与 attempt-private copy 已实现，但不能代替这些部署证据；
- HTTPS、认证、限流、存储配额与审计；
- 将现有本地、best-effort、结构化脱敏日志接入受控集中式生产保留，并补齐磁盘/队列/熔断告警；
- 生产容量、实际部署身份、加密/offsite 介质和既定 RTO/RPO 下的 Schema 迁移、备份、恢复与 forward-only 回滚演练；本机 14-test 独立根功能演练不满足此闸门；
- 每个 `platform × source_type × job_kind × adapter × downloader_version × environment × product_version` 的 Stage 0 CSV v3 evidence 达到验收线并经独立人工决定；导入本身不批准，批准也不启用执行 gate；
- 对真实 X graph 另须证明 stable attachment key 可重复且唯一，并证明 pinned `YtDlpAdapter` 能 exact-select 一个附件且不下载 sibling；完成、审阅并更新 adapter capability 前，`VDC_ENABLE_X_GRAPH_V2` 必须为 `0`；
- 授权、版权、平台条款与实际分发依赖的许可证审查。
