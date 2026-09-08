# Open-Flame — 多平台视频下载、编辑与上传

这是一个面向单机、单管理员、私有环境的媒体控制面，包含下载器、非破坏性编辑工作台和需要逐项确认的上传器。下载、编辑与上传分别保存数据和任务状态。

> 当前开发版本为 **0.28.0**，下载数据库仍为 **Schema 11**，编辑库独立使用 **Schema 4**，上传库独立使用 **Schema 3**，自动流程使用独立 **Workflow Schema 1**。除分段和封面外，编辑域现在支持隔离 runtime 驱动的 OpenAI `whisper-1` 听写、`gpt-5.6-luna` 翻译和 `gpt-4o-mini-tts` 标准音色配音；时间轴须审核，处理计划冻结已批准修订。`/workflows` 可把一个 URL 串接到下载、编辑、AI 和所选 Bilibili、抖音、视频号上传草稿，并按用户预先授权或逐步确认继续。AI runtime 和密钥不随包提供，三平台真实投稿、定时发布和审核结果仍需外部测试员分别验收。Windows 源码版仍需已安装的 64 位 CPython，不是免 Python EXE。

自动流程入口为 `/workflows`；编辑入口为 `/edits`，也可从 ready 下载成品点击“进入编辑”。具体流程见[编辑工作台指南](docs/EDITOR.md)与[可选 AI Runtime](docs/AI_RUNTIME.md)。外部上传测试入口仍为[三平台上传快速开始、测试计划与回报模板](docs/UPLOADER_TEST_PLAN.md)。

2026-09-05 的八项 Debug 发现已进入 0.24.3 修复：上传异常恢复、完整运行时校验、原件完整性与类型、Worker 状态、历史列表、上传库结构和运维文档。当前提交前复验及追加边界修复见[最终源码审查](validation/iteration-0.24.3-final-review.md)；[此前修复记录](validation/iteration-0.24.3-debug-fixes.md)和[原始核验报告](validation/full-debug-20260905.md)保留各自历史构建，[后续执行计划](docs/FOLLOW_UP_EXECUTION_PLAN.md)跟踪剩余工作。旧上传运行时须按[升级步骤](docs/UPLOAD_RUNTIME.md#从旧运行时升级)重建，账号和上传数据保留。

0.28.0 在 0.27.0 的 T18 本地编辑切片上完成 T19 AI runtime 与 URL 自动流程；当前证据见[0.28.0 AI 与流程记录](validation/iteration-0.28.0-ai-workflow-evidence.md)，此前[编辑工作台记录](validation/iteration-0.27.0-editing-workspace-evidence.md)、[上传参数与 Schema 3 记录](validation/iteration-0.26.0-upload-parameters-evidence.md)及更早记录保留各自历史范围。Editing Schema 1/2/3 会按精确结构逐步迁移到 Schema 4；Upload Schema 1/2 会迁移到 Upload Schema 3。未知、损坏或更高版本保持原样并拒绝启动。固定 Git commit、制品 identity、五个发行文件和独立安装的实际结果由包外 release receipt 绑定，不能写回被打包源码自证。

下载、编辑和上传三页共用[设计规范](docs/DESIGN_SYSTEM.md)、语义 token、主题脚本和固定本地资源路由；[交互式视觉基准](docs/design-preview.html)直接读取同一生产 CSS。

上传入口在下载首页，或访问 `/uploads`。使用步骤见 [上传指南](docs/UPLOADER.md)，独立工具安装见 [上传运行环境](docs/UPLOAD_RUNTIME.md)，技术选择见 [开源上传器调研](docs/OPEN_SOURCE_UPLOADER_REVIEW.md)。上传环境与浏览器不会加入原下载 `.venv`，上传账号不会复用下载 Cookie。下载备份不包含上传目录；上传数据使用单独的 `video-upload-backup` 命令。

首次使用双击 [Setup-Open-Flame.cmd](Setup-Open-Flame.cmd)，阅读联网与改动提示后输入 `y`；需要三平台上传时可向同一 Setup 传入 `--upload-runtime`，需要 AI 时传入 `--ai-python-embed-zip ABSOLUTE_ZIP`。两者都复用现有安装入口和应用根，不会建立第二套安装服务；完成后双击 [Start-Open-Flame.cmd](Start-Open-Flame.cmd)。详见 [首次安装与修复](docs/WINDOWS_SETUP.md)、[上传运行环境](docs/UPLOAD_RUNTIME.md)和[启动与日志](docs/WINDOWS_LAUNCHER.md)。当前本地证据见 [0.28.0 AI 与流程记录](validation/iteration-0.28.0-ai-workflow-evidence.md)；[0.27.0 编辑工作台记录](validation/iteration-0.27.0-editing-workspace-evidence.md)及更早记录保持为历史。

上一版 0.23.0 的独立源码发行与安装记录为 **1670 passed、8 skipped**，属于历史证据，不代表当前上传或真实平台验收。维护者见 [构建与验收说明](docs/RELEASE.md)，接续开发见 [项目交接](HANDOFF.md#本次交接入口)。源码 ZIP、sdist、wheel 不包含第三方运行二进制；本机开发和打包不自动 push 或创建 GitHub Release。

## License status

项目自有源码、文档和脚本依据 [Apache License 2.0](LICENSE) 开源，版权声明为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`，归属信息见 [NOTICE](NOTICE)。第三方组件继续受各自许可证约束，项目的 Apache-2.0 授权不会重新许可 `licenses/python/` 中的第三方材料或外部工具；精确清单与边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。仓库源码和重新验证的 project-only Python sdist/wheel 可以按上述条款分发，但 dependency wheelhouse、冻结可执行文件、离线安装器、OCI/container image 以及 yt-dlp/FFmpeg `runtime-tools` 工具包仍须完成目标架构 SBOM、对应源码、notices、relinking 义务与人工复核后才能再分发。

Apache-2.0 只授权本程序本身，不授予任何被下载媒体的版权、平台账号权限或绕过访问控制的权利。使用者必须只处理自有、已获明确授权或当地法律允许的内容，并自行遵守相关平台条款和适用法律。

## 当前状态

| 项目 | 当前状态 |
|---|---|
| 编辑工作台 | `/edits`；下载成品只读复制到独立 `data-edits`；版本化草稿、多个分段、封面、AI 听写/翻译任务、可审核时间轴、标准音色配音、绑定时间轴的不可变处理计划、取消与显式重试、成品哈希和导入上传；AI runtime 缺失或无凭据时明确阻塞 |
| 自动流程 | `/workflows`；持久化串接 URL 下载、单视频编辑、AI 审核/确认、上传草稿和所选账号的原子批量确认；重启与重试会停下再确认。账号登录 revision 改变时拒绝旧绑定；上传重试只沿账号/来源/平台不变的唯一 leaf 对账，未知远端结果停下人工核对 |
| 上传器 | `/uploads`；Bilibili、抖音、视频号的独立账号与任务；先本地草稿、后明确确认；支持逐平台文案/标签/封面/发布时间及平台参数、本地账号墓碑、媒体与封面占用/显式删除、视频精确恢复；开源工具运行环境独立安装；真实平台投稿与定时发布未验收 |
| Windows 一体化应用 | `video-download-local-app` 可独立启动控制面、Worker 与浏览器；固定 app/data/database 布局、抢占前预检、严格握手、单实例、异常子进程回收及结构化日志继续保留；Schema 11 的 two-phase run claim gate 将“允许领取”和停机关闭在 SQLite 写事务中线性化，旧 run 不能因 Pipe 检查竞态领取新 Job |
| Web UI / FastAPI 控制面 | 可由一体化入口启动，也保留开发用单独入口；下载/编辑/上传页共用本地语义样式、系统/浅/深主题、响应式布局与 CSP；显示每个 Job 的中文阶段与估算进度，列出 ready 原件及其缩略图/字幕；无认证，代码强制绑定 loopback |
| SQLite | Schema 11；WAL、`busy_timeout=5000`、`synchronous=FULL`，启动时 forward-only 迁移、结构、claim-gate singleton 与 capability ledger 语义检查；Schema 9 能力行只读封存，旧 flat-v1/graph 记录继续可读 |
| 下载 Worker | 一体化入口内部设置并持有 Windows 本机直连 Worker 的完整配置；高级手动入口仍要求环境 gate + CLI 明示确认、固定工具链、共享现有数据库和单实例锁；离线 fake 与 Linux 隔离 candidate 保持独立 |
| 本机工具链 | Windows x64 固定 yt-dlp `2026.08.19`、FFmpeg/ffprobe `n9.0.1-11-ge47273f4d9-20260831`；改用官方月末保留构建，逐文件校验并通过离线真实二进制 smoke；不加入系统 `PATH` |
| `yt-dlp` | 固定命令、zipimport 入口、本机/候选适配器已组装；可显式接入一个受校验的 Node/Deno/Bun/QuickJS，probe/download 强制 UTF-8；格式选择优先非 HLS 并保留有界回退；下载 stdout 控制协议只输出常量 transfer 标识和有界数值，实时阶段估算不会记录原始工具行 |
| 凭证 | 业务库不保存 Cookie 路径/内容；Windows 一体化 v2 配置可显式选择平台默认，启动时登记或复用有效同 ref profile，新建/重试时原子绑定；v1 仍仅映射 source。网页只选使用默认或匿名，不编辑或披露凭证；本地可用不等于平台登录有效 |
| 备份/恢复 | 下载 `video-download-backup` 要求精确 Schema 11；上传备份格式 2 的 `video-upload-backup` 要求全部实例停机和精确 Upload Schema 3，只保存数据库、已登记且校验一致的受管视频/封面及非秘密账号元数据，恢复到全新独立根并撤回排队确认；两者范围互不包含 |
| 运行日志 | supervisor、控制面、本机 Worker、离线 Worker 与 candidate Worker 写入有界、轮转、字段白名单的 JSONL；一体化三进程共享 `run_id`，前端可统一查看最近事件；启动后写入仍为 best-effort |
| 平台能力 | X、YouTube、Bilibili、Douyin、TikTok、Instagram 的窄范围静态路由均为 `candidate`；兼容 API 保留三层读接口，UI 使用单次一致性 snapshot 展示 implementation、精确 product build/downloader/environment evidence 和 current decision。导入永不自动批准，只有达到固定阈值且完整 build identity 匹配当前包的证据可经本地 CLI 显式批准；历史单样本与离线 E2E 均不满足 Stage 0，当前仓库不附带任何批准记录 |
| 短链 | Windows `local-app --allow-direct-network` 接通受控本机直连展开；通用控制面仍默认关闭，POSIX 可显式使用独立 UDS/HMAC egress。支持 `t.co`、`b23.tv`、`v.douyin.com`、TikTok `vm`/`vt`，逐跳限制 hostname/IP/redirect/时限；本轮仅离线与模拟网络，不是隔离或真实平台验收 |
| 部署 | 有默认不可运行的 Dockerfile/Compose、可选 Cookie override 和只读优先 Linux acceptance runner；未 build、pull、cold start 或执行 mutation mode |
| 本机外部工具证据 | gitignored 本机 `runtime-tools/windows-x64` 已安装并复验；工具仍不在系统 `PATH`；Docker/可用 WSL Linux runtime 仍不存在 |

当前实现包括：

- 独立 Editing Schema 4、下载原件 SHA-256 复核复制、版本化草稿、乐观并发、幂等请求、不可变 AI task/timeline/render plan 及 plan-to-timeline 绑定、脱敏远程调用账本与 unknown 人工 reconciliation、明确确认、单本地 Worker、取消/重试 lineage 和中断恢复；编辑域不会覆盖下载原件。
- 固定 FFmpeg/ffprobe 的多分段 MP4 和 Pillow 封面制作，输出记录大小、SHA-256、时长、尺寸、容器与 codec；固定文件名不包含用户标题，媒体命令不经过 shell。
- 严格 SRT/VTT 时间轴读写、隔离 CPython runtime 与标准库 OpenAI provider；`whisper-1` 听写、`gpt-5.6-luna` 结构化翻译和 `gpt-4o-mini-tts` 标准音色均经 manifest 显式声明。每个 cue 最多 4096 字符；自动流程只把第一个已选分段的派生音频交给听写，结果再换算回源时间。runtime、凭据或能力缺失时保持 blocked。
- AI 渲染按每个已选分段过滤并把字幕/配音时间归零；边界切入 cue 时以 `ai_segment_boundary_splits_cue` 拒绝，纯 B-roll 分段生成空 VTT 和本地静音而不调用 TTS。保留原声时将原声压到 22% 后与配音混合；AI 或配音重试须再次确认，已经完成的远程批次/cue 仍可能重复计费。
- 编辑视频通过 `edit_output_id` 显式复制到上传域并再次校验；该动作不创建上传任务，更不会调用平台适配器。
- Web 提交页面、最近批次恢复入口、ready 原件及其缩略图/字幕链接和显式响应 DTO；JSON、TXT、CSV 一批最多 50 条输入。
- X 单帖、YouTube 单视频/Shorts、Bilibili 普通 BV/av 默认分 P、Douyin 单作品、TikTok `/@handle/video/{id}` 与 Instagram `/reel/{shortcode}` 的 URL 提取、白名单校验、规范化和去重；Bilibili `p > 1` 与 Instagram 帖子/轮播/Story/Live 会明确拒绝。TikTok `vm`/`vt` 被建模为需受控展开的短链，不会静默当作直链。
- `t.co`、`b23.tv`、`v.douyin.com` 以及 TikTok `vm.tiktok.com` / `vt.tiktok.com` 有逐跳 HTTPS/DNS/peer/redirect 上限的 resolver；Windows 一体化入口显式确认直连后可使用，通用控制面仍须单独启用 POSIX UDS transport，关闭时以 `short_link_resolution_required` 终止。TikTok 策略只允许精确的 `vm.tiktok.com`、`vt.tiktok.com`、`tiktok.com`、`www.tiktok.com` 与 `m.tiktok.com`，拒绝相似子域和后缀欺骗；`youtu.be/{id}` 不需要展开，可直接规范化。
- SQLite Schema 11 中的 Batch、InputRecord、SourceItem、SourceDiscovery、SourceRelation、DownloadJob、JobAttempt、MediaAsset、Artifact、run-scoped `worker_claim_gate`，以及不可变 `capability_evidence`、append-only `capability_decisions`、只读 current `platform_capabilities` view 和 Schema 9 历史档案等模型。
- 默认关闭的 X graph-v2 编排：父 `discover` Job 只探测并原子 fan-out；有序成员写入不可变 discovery snapshot；每个附件使用独立 child `download` Job；Input/Batch 只按 active snapshot 聚合，混合 `ready` 与终止失败/取消得到 `partial_success`。input-level cancel 与 terminal-only rediscover API 已实现，rediscover 递增 `run_generation` 并重置该 generation 的 retry budget，同时保留 Job 的累计 `attempt_count` 和历史快照。
- 旧 flat-v1 多资产 Job、Asset 与 manifest 不会被 Schema 8 原地改写，迁移后继续可查询和读取。
- 终态 `failed` 的 flat `download` Job 可由 Web 或 `POST /api/v1/jobs/{job_id}/retry` 显式进入新的 `run_generation`；历史 Attempt 和累计 `attempt_count` 保留，新代 retry budget 重新计数。graph discover/attachment、非 failed Job、同源已有 live/ready 工作及并发重复请求均 fail closed；显式重试不会清除或绕过平台 cooldown。
- 总活动任务上限 2、单平台上限 1；持久化队列暂停、平台熔断、自动 cooldown 后的单次 half-open probe 和 manual-reset lock。Web 与只读 API 展示 `cooldown_until`、`requires_manual_reset` 等状态；reset API 只接受人工锁定状态，不能提前跳过自动冷却。
- 管理面凭证 profile：只接受 1–64 字符 opaque ref，可按平台向 queued Job 分配、清除或禁用；公开 Batch API 不接受凭证 ID。
- Worker 在 claim 事务内检查 profile 存在、平台匹配、未过期、未禁用且 ref 合法；失效时以 `authentication_required` 终止、不创建 Attempt、不调用 Adapter，并继续领取下一个有效 Job。
- 离线多资产的临时目录、staging、校验、SHA-256、`source.json`、`manifest.json`、提交意图和三阶段崩溃恢复；原件、缩略图和平台既有字幕以同一资产目录一次发布并在同一 SQL 事务登记。
- 带 SHA-256 manifest 的备份/恢复组件：使用 SQLite 一致快照，排除顶层 `logs`、`temporary` 与 `assets/.staging`，恢复到不存在的独立新根后复核 Schema、数据库和资产 manifest。
- JSON 指标、健康检查、字段白名单 JSONL 运行日志、只读成品与辅助产物列表/下载 API、Stage 0 CSV v3 聚合报告与本地 capability governance CLI，以及受控出站 proxy / Unix-domain-socket relay 的安全基础组件。辅助下载继续执行严格 UUID、ready linkage、目录/MIME/language、size/SHA-256 与安全响应头检查。证据按 `platform × source_type × job_kind × adapter × downloader_version × environment × product_version` 分格，其中 `product_version` 是版本号与完整包载荷 SHA-256 组成的 build identity；报告不写数据库，导入只追加证据，批准/撤销另走显式 CAS 决定。`jobs` / queue depth 包含 parent `discover`，但 `platform_outcomes` 只统计 `job_kind=download`。
- download capability registry、公开只读能力 API/UI，以及 Worker 对 `platform × source_type × job_kind` 的原子领取过滤；不支持的旧任务会留在队列中等待匹配 Worker。真实 Worker 会把 Job 的 `max_height` 传入 Adapter，yt-dlp 格式选择不再使用不受高度约束的 `/b` 回退；平台要求 fresh cookies 时统一归类为 `authentication_required`。
- deployment-owned Cookie source override、六平台 0–6 映射、完整祖先/ACL/路径重叠检查、Worker core-dump 禁用，以及默认只读且需要二次明确授权才执行变更的 Linux/Docker acceptance runner。
- [ADR-0001](docs/adr/0001-x-attachment-discovery.md) 的 Schema 8 graph-v2 编排已实现；真实 stable key 与 exact-selector 仍未经过 Stage 0，故真实 X graph 路由保持 gate 关闭。

准确的当前运维步骤与边界见 [Runbook](docs/RUNBOOK.md)。本轮记录在 [Iteration 0.28.0 AI 与自动流程证据](validation/iteration-0.28.0-ai-workflow-evidence.md)；[Iteration 0.27.0 编辑工作台证据](validation/iteration-0.27.0-editing-workspace-evidence.md)及更早记录均为 point-in-time 历史证据，不替代当前构建、AI 模型或真实平台验收。

## 本地启动

普通 Windows 使用者优先按 [首次安装与修复](docs/WINDOWS_SETUP.md) 操作，无需安装开发测试依赖。`Setup-Open-Flame.cmd --help` 查看参数；`--wheelhouse` 和 `--artifact-cache` 分别指定核心环境的精确 Python wheels 与工具缓存，`--upload-runtime` 选择性安装或核验三平台上传 runtime，`--ai-python-embed-zip` 选择性构建 AI runtime。上传 runtime 缺失时会获取自身固定且校验散列的 GitHub、PyPI 与 Chromium 输入；Setup 不自动下载 Python 或 AI 归档，不修改系统 PATH，也不覆盖未知环境、工具或 runtime。`--repair` 只用于安装器自己创建的 `.venv`；先正常停止使用该源码或同一 app-root 的应用。

以下为开发者或高级手动入口，仍可使用 uv：

需要 Python 3.12 或更高版本，推荐使用 `uv`。全新 clone 首次启动时，先把项目锁定的工具链安装到一个尚不存在的绝对路径，再启动一体化应用：

```powershell
uv sync --extra dev
uv run pytest -q
$ToolRoot = Join-Path $env:LOCALAPPDATA 'Open-Flame\video-download-control\runtime-tools\windows-x64'
uv run video-download-tools install --tool-root $ToolRoot
uv run video-download-tools verify --tool-root $ToolRoot
uv run video-download-tools smoke --tool-root $ToolRoot
uv run video-download-local-app --allow-direct-network
```

其中 `pytest` 命令只适用于包含历史回归的完整 Git checkout；源码 ZIP 与 sdist 不携带 `tests/`。普通源码包安装和启动不需要开发测试依赖。

上述位置就是一体化应用的默认工具目录，以后启动只需最后一条命令。如果仓库外或被 Git 忽略的其他工具目录已经通过 `verify` 与 `smoke`，也可以把 `$ToolRoot` 指向该绝对路径，并在启动时显式传入 `--tool-root $ToolRoot`，无需重复安装。

一体化入口在三次身份/健康检查通过后自动打开 `http://127.0.0.1:8000/`；页面可提交 URL、重新打开最近批次、查看每个 Job 的中文阶段与估算进度条、对符合条件的 failed flat Job 发起新代重试、查看 cooldown/half-open/manual-reset 状态、下载 ready 原件及其缩略图/字幕，并查看能力与运行日志。首次启动会创建或 forward-only 迁移数据库到 Schema 11。默认应用根为 `%LOCALAPPDATA%\Open-Flame\video-download-control`，数据库位于其 `data\control.sqlite3`，结构化日志位于 `data\logs`，不依赖启动时的当前目录。用 `Ctrl+C` 时 supervisor 先在 SQLite 中关闭本次 run 的 claim gate，再按 Worker-first 顺序停止子进程；已在线性化点之前领取的 Attempt 仍按既有 lease/恢复契约完成或恢复，但关闭点之后不会再领取新 Job。端口 8000 已占用时应用会在创建数据库或启动子进程前拒绝，不会连接或复用旧服务。

`--allow-direct-network` 同时确认下载和短链展开使用本机网络；它不建立 Linux 隔离。页面提交/导入与重试可选择 `use_default`（使用显式配置的平台默认，未配置的平台仍匿名）或 `anonymous`（新任务不绑定凭证）；不上传 Cookie 内容。重复 URL 复用已有工作时，不会因切换选项而改写它的凭证或状态。

仅检查配置而不常驻、不打开浏览器、不领取任务，也不自动登记默认凭证 profile：

```powershell
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --allow-direct-network `
  --check
```

成功 stdout 为 `{"status":"checked"}`。需要并行保留其他本机实例时，应为测试显式提供一个不同端口和独立的绝对 `--app-root`；同一 app root 只允许一个 supervisor。单独的 `video-download-control` 与 `video-download-local-worker` 仍保留用于开发和高级排障，但不再是普通 Windows 使用的首选入口。

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

### Windows 一体化 Cookie 配置

一体化入口不把 credential ref 或 Cookie 文件路径直接放进 CLI 参数，而是只接受一个仓库外、只读、普通且不含链接的绝对 JSON 配置文件。要让前端新任务使用默认凭证，必须用 v2 明确列出 `default_cookie_platforms`：

```json
{
  "schema_version": 2,
  "cookie_sources": [
    {
      "platform": "douyin",
      "credential_ref": "douyin-primary",
      "path": "C:\\private\\vdc\\douyin.cookies.txt"
    }
  ],
  "default_cookie_platforms": ["douyin"]
}
```

把 JSON 与 Cookie source 均设为只读，再执行正常启动。只列入已有 source 映射的平台，且每个平台最多一个 source；空默认列表不自动绑定：

```powershell
$CookieConfig = (Resolve-Path -LiteralPath 'C:\private\vdc\cookie-sources.json').Path
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --cookie-config $CookieConfig `
  --allow-direct-network
```

正常 `local-app` 启动会在共享数据库中原子登记缺失 profile，或复用同平台、同 ref 且仍有效的唯一 profile；禁用、过期或有歧义的既有记录会拒绝启动，不会复活或绕过。默认映射只属于本次运行，不新增持久化全局默认；配置变化后应正常停机再启动。`--check` 只校验配置/source，不登记缺失 profile，也不证明平台登录成功。

v1 仍只允许 `schema_version` 与 `cookie_sources`，保持 source-only，不会自动创建默认映射；已有手工 profile 登记/Job 分配流程仍可使用。新建与导入默认 `credential_mode=use_default`；配置重验失败或新绑定所需 profile 失效时整批失败，未配置的平台仍匿名。失败重试无请求体时保留旧绑定，显式选择时与新代次原子更新。配置与 source 在启动、绑定及领取路径重验；路径、ref 和内容不会进入公开 API 或普通运行日志。Windows 私有 DACL 尚未由程序证明，仍只适合受信任单用户本机。

### Windows 本机下载 Worker（高级手动入口）

先在终端 A 启动控制面并让它初始化数据库：

```powershell
$DataRoot = (Join-Path (Resolve-Path '.').Path 'data')
$ToolRoot = (Resolve-Path '.\runtime-tools\windows-x64').Path
$env:VDC_DATA_ROOT = $DataRoot
$env:VDC_TOOL_ROOT = $ToolRoot
$env:VDC_ENABLE_X_GRAPH_V2 = '0'
uv run video-download-control
```

再在终端 B 启动常驻本机 Worker；Node 参数展示历史 YouTube 样本使用的显式 runtime，本轮没有真实平台下载验收：

```powershell
$DataRoot = (Join-Path (Resolve-Path '.').Path 'data')
$Database = (Join-Path $DataRoot 'control.sqlite3')
$ToolRoot = (Resolve-Path '.\runtime-tools\windows-x64').Path
$Node = (Get-Command node.exe).Source
$env:VDC_ENABLE_LOCAL_REAL_WORKER = '1'
uv run video-download-local-worker `
  --data-root $DataRoot `
  --database-path $Database `
  --tool-root $ToolRoot `
  --js-runtime "node:$Node" `
  --allow-direct-network `
  --poll-interval-seconds 2
```

若某个平台需要登录态，先用下面“管理员凭证流程”登记 opaque ref 并把它分配给仍在 queued 状态的 Job；再把该 ref 映射到仓库外、只读的 Netscape Cookie 文件。先停止同一 data root 的常驻 Worker，再执行一次不会领取任务的预检：

```powershell
$CookieFile = (Resolve-Path -LiteralPath 'C:\private\vdc\douyin.cookies.txt').Path
attrib +R "$CookieFile"
$CookieSource = "douyin:douyin-primary=$CookieFile"
uv run video-download-local-worker `
  --data-root $DataRoot `
  --database-path $Database `
  --tool-root $ToolRoot `
  --js-runtime "node:$Node" `
  --cookie-source $CookieSource `
  --allow-direct-network `
  --check
```

成功时 stdout 为 `{"cookie_platforms":["douyin"],"status":"ready"}`，并写入独立的 `worker.preflight_*` 与 `toolchain.inspected` 日志；它不会创建 Attempt、领取 Job、发布 Asset 或复制 Cookie。随后在常驻命令中加入同一个 `--cookie-source $CookieSource`。每个平台最多一个 source，不同平台不能指向同一物理文件；文件必须非空、不超过 `--max-cookie-bytes`（默认 8 MiB），且必须位于 data/tool root 之外。ref 必须和该平台已分配 profile 的 `secret_ref` 精确一致，否则实际任务会 fail closed。

两个进程都用 `Ctrl+C` 正常停止。此手动入口默认不开启；环境变量必须精确为 `1`，同时还要传 `--allow-direct-network`。它只接受已由控制面初始化且通过 Schema 11 readiness 的数据库，同一数据根只允许一个本机 Worker。高级手动入口没有一体化 supervisor 的 run-scoped claim gate 生命周期，普通 Windows 使用若需要停机后不再领取新 Job，应使用上方 `video-download-local-app`。`--check` 证明配置输入可用，但不会证明每个 queued Job 都已获得匹配凭据，也不会验证平台登录态仍有效。合法 `--cookie-source` 的 ref 与绝对路径会出现在本机进程命令行/PowerShell history，因此普通使用优先选择上方只在 argv 暴露配置文件路径的一体化入口。Windows ACL、per-platform process isolation 和 secret sidecar 仍是生产凭据前的阻断项。graph-v2 discover/attachment 任务会在同一 claim 事务内被跳过而不会被错误终止；本机使用仍应保持 `VDC_ENABLE_X_GRAPH_V2=0`。

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

只读能力端点分为三层：`/api/v1/capability-implementations` 是代码内静态路由白名单，`/api/v1/capability-evidence` 是精确构建/环境的聚合评估，`/api/v1/capability-decisions` 是每个精确 identity 的当前人工决定；`/api/v1/capability-snapshot` 在一次数据库读事务中返回 UI 所需三层、总数/截断标记和当前 build identity，并保证返回决定引用的 evidence 不被分页遗漏。旧 `/api/v1/download-capabilities` 保留为兼容用静态 candidate 视图。数据库 readiness 失败时 evidence/decision/snapshot 端点返回 503，静态实现端点仍可用于诊断。HTTP 健康探针最多复用 5 秒的完整数据库审计，并用 SQLite schema cookie 立即识别 DDL；审计期间 schema 变化或 cookie 不可读会 fail closed。管理员、备份和恢复路径不使用该缓存。

### Stage 0 v3 证据与决定

先把模板复制到仓库外的私有目录，并从实际执行验证的同一当前版本包读取 build identity；把 JSON 中的完整 `product_identity` 原样填写进 results 的 `product_version` 列。不要只填 `0.28.0`，不要沿用旧构建 identity，也不要手工替换 SHA-256。另填写一个不含主机名、用户名或路径的安全 `environment` token。报告只输出 aggregate，不含 URL、来源 identity hash、sample ID 或 run ID：

```powershell
uv run video-download-validation --print-product-identity
```

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

导入和决定使用现有数据库的绝对规范路径。`--environment-key` 必须与 results 精确一致；导入会重新解析两份 CSV 并按固定 `stage0-v3` 阈值评估，不信任 Markdown report，也不会自动批准：

```powershell
$Database = (Resolve-Path -LiteralPath ".\data\control.sqlite3").Path
uv run video-download-capabilities --database-path $Database import `
  --manifest C:\private\samples.csv `
  --results C:\private\results.csv `
  --environment-key windows-x64-direct-no-cookie

uv run video-download-capabilities --database-path $Database list --kind evidence
uv run video-download-capabilities --database-path $Database approve `
  --evidence-id REPLACE_WITH_EVIDENCE_ID `
  --expected-revision 0 `
  --reason-code stage0-reviewed
uv run video-download-capabilities --database-path $Database list --kind decisions
uv run video-download-capabilities --database-path $Database history `
  --identity-key REPLACE_WITH_IDENTITY_KEY
```

撤销只允许作用于当前已批准 evidence，并需要最新 revision：

```powershell
uv run video-download-capabilities --database-path $Database revoke `
  --evidence-id REPLACE_WITH_CURRENT_EVIDENCE_ID `
  --expected-revision 1 `
  --reason-code regression
```

相同来源身份的 URL 别名（包括 TikTok 同一 video ID 的不同 handle/host）不能重复充样本，跨 bundle 也产生相同 canonical evidence；相同规范内容重复导入幂等。并发决定中只有一个旧 revision 能成功，另一方得到稳定 conflict。证据/决定不可更新、删除或用 `INSERT OR REPLACE` 覆盖，Schema 9 的历史行也不可写。`list` / `history` 只读命令不会迁移旧数据库。管理员 CLI 的数据库路径必须是绝对、规范、单 hard-link 的普通文件，并在连接边界重验 identity；构建摘要拒绝 package 内 link/reparse/special file，双重清单检查会识别哈希期间的新增、删除或 metadata 变化。CSV 和本地管理员并非受信硬件证明：能伪造执行记录或同时重写数据库和 hash 的主体仍可伪造结论；本轮不声称 tamper-proof attestation。

允许的 approve reason 为 `stage0-reviewed`、`replacement-reviewed`；revoke reason 为 `regression`、`superseded`、`operator-withdrawn`。`list` / `history --limit` 范围为 1–500。CLI 的 stdout/stderr 均为单行脱敏 JSON；退出码依次表示：`0` 成功、`2` 输入错误、`3` 策略拒绝、`4` 未找到、`5` revision 冲突、`6` readiness/完整性/I/O/`product_build_drift` 或 `product_build_unavailable`、`70` 未预期内部错误。导入和 approve 在提交事务前再次读取构建身份，漂移时回滚全部写入。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | 数据库、Schema 与队列运行状态；降级时仍返回状态体 |
| `GET` | `/health/live` | 进程存活检查 |
| `GET` | `/health/ready` | 就绪检查；数据库异常或队列暂停时返回 503 |
| `POST` | `/api/v1/batches` | 提交 1–50 条 URL 或分享文本 |
| `POST` | `/api/v1/batches/import?filename=...&name=...` | 导入 UTF-8 `.txt` / `.csv` 请求体，最大 256 KiB |
| `GET` | `/api/v1/batches?limit=50` | 列出批次，`limit` 范围 1–100 |
| `GET` | `/api/v1/batches/{batch_id}` | 查询批次、输入和任务 |
| `GET` | `/api/v1/batches/{batch_id}/assets` | 列出该批次已 ready 的原件元数据、原件下载 URL 与登记的缩略图/字幕 DTO，不暴露本机路径 |
| `GET` | `/api/v1/assets/{asset_id}/download` | 下载数据库登记且仍满足本机文件边界的 ready 原件 |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | 下载 ready asset 下登记且重新通过路径、类型、parent original、大小与 SHA-256 检查的缩略图或字幕 |
| `POST` | `/api/v1/jobs/{job_id}/retry` | 仅将终态 failed 的 flat download Job 显式排入新 `run_generation`；成功返回 `queued` 与新代编号，不清除平台 cooldown；不满足范围或并发冲突返回 409 |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | 取消 queued 任务或请求活动 Worker 协作取消 |
| `POST` | `/api/v1/inputs/{input_id}/cancel` | 原子取消该 Input 的非终态 parent/child 工作；已 ready child 保留 |
| `POST` | `/api/v1/inputs/{input_id}/rediscover` | 仅 graph-v2 且当前所有工作终态时进入下一 `run_generation`；否则返回 409 |
| `GET` | `/api/v1/metrics` | 队列、Attempt 时延、磁盘、平台结果、错误与熔断 JSON；平台结果排除 parent `discover` |
| `GET` | `/api/v1/operations/queue` | 查询持久化队列暂停状态 |
| `GET` | `/api/v1/operations/logs?limit=100` | 查询日志管线状态与最近脱敏事件，`limit` 范围 1–500 |
| `GET` | `/api/v1/operations/tools` | 校验本机工具链并显示固定版本、离线 smoke、隔离 Worker/平台验收和本机第三方工具包再分发状态 |
| `GET` | `/api/v1/credential-defaults` | 只返回本次运行显式配置的 `platforms` 列表及本地配置/profile 的 `available`；无 profile ID、ref 或路径，不证明平台登录成功 |
| `GET` | `/api/v1/download-capabilities` | 兼容用静态声明视图，只列窄范围路由、候选/禁用状态、Cookie 与短链模式；不投影 Schema 11 数据库中的 evidence/decision |
| `GET` | `/api/v1/capability-implementations` | 列出代码中实际注册的静态 route implementation，不读取治理决定 |
| `GET` | `/api/v1/capability-evidence?limit=200` | 列出 Schema 11 数据库中去标识、不可变的 Stage 0 聚合证据；数据库不就绪时返回 503 |
| `GET` | `/api/v1/capability-decisions?limit=200` | 列出每个精确 identity 的 current approve/revoke 决定；不提供写操作 |
| `GET` | `/api/v1/capability-decisions/{identity_key}/history?limit=200` | 按 64 位 identity key 返回 append-only 决定链；不存在为 404，无效 key 为 422 |
| `GET` | `/api/v1/capability-snapshot?limit=200` | 一次 readiness 与单一 SQLite snapshot 返回 UI 所需三层、当前 build identity、总数/截断标记，并补齐返回决定引用的旧 evidence |
| `POST` | `/api/v1/operations/queue/resume` | 磁盘恢复到配置水位后恢复队列 |
| `GET` | `/api/v1/platform-circuits` | 查询各平台 `closed` / `open` / `half_open`、连续失败、最后错误、`cooldown_until` 与 `requires_manual_reset` |
| `POST` | `/api/v1/platform-circuits/{platform}/reset` | 仅人工复位已进入 manual-reset lock 的六个平台；自动 cooldown 或其他冲突返回 409，不允许提前绕过冷却 |

JSON 新建请求的 `credential_mode` 和 TXT/CSV 导入 query 参数同名，省略均为 `use_default`，也可显式指定 `anonymous`。配置重验失败或新绑定所需默认 profile 失效时返回固定 409，批次创建不留下部分 Job。未配置平台保持匿名。重试请求体可为 `{"credential_mode":"use_default"}` 或 `{"credential_mode":"anonymous"}`；不发请求体时保留旧绑定以兼容旧客户端。网页重试发送当前表单选择。去重返回已有 live/ready 工作时不重新绑定或改写其凭证，不代表重新以当前模式下载；既有任务的 profile 仍由 Worker 在领取时检查。

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
- `video-download-local-worker`：Windows 本机直连 Worker；要求显式 gate、CLI acknowledgement、固定工具链、现有控制数据库、强制日志首写与单实例锁；可重复传入不同平台的 `--cookie-source`，并用 `--check` 在不领取任务时预检。
- `video-download-worker`：仅显式离线 fake QA Worker。
- `video-download-candidate-worker`：默认禁用的真实 Worker 候选；要求 feature gate、Linux loopback-only namespace、UDS relay、绝对工具路径与精确 yt-dlp/FFmpeg/ffprobe 版本；可用 `--js-runtime NAME:ABSOLUTE_EXECUTABLE` 显式提供唯一 JavaScript runtime。
- `video-download-credentials`：管理员专用的 opaque profile CLI，支持 `register`、`list`、`assign`、`clear`、`disable`；要求现有普通文件数据库的显式绝对路径，从不接受 Cookie 路径或内容。
- `video-download-backup`：使用显式绝对路径执行 `create` 或 `restore`；备份和恢复目标必须是尚不存在、不重叠的独立根。
- `video-upload-backup`：在全部 Open-Flame 实例停机后，为精确 Upload Schema 3 创建备份格式 2 的无账号秘密上传备份，或恢复到全新独立上传根；不会登录或上传。
- `video-download-validation`：评估脱敏的 Stage 0 样本与结果 CSV。
- `video-download-egress-proxy`：在绝对路径 UDS 上启动显式 `--allowed-host` 或有界只读 `--allowed-host-file` 的受控 forward proxy，并把只含 reason 与 policy host 的稀疏 audit 以 JSON 写到 stderr。
- `video-download-unix-relay`：把数值 loopback TCP 端口转发到受保护的 UDS。
- `video-download-short-link-egress`：在 egress side 运行 authenticated single-hop HTTPS fetch 服务；只接收签名的 numeric-IP target，不自行 DNS 或自动跟随 redirect，SIGINT/SIGTERM 时按 inode identity 清理自己的 UDS。

候选 Worker/relay/proxy 已由 [`deployment/compose.candidate.yaml`](deployment/compose.candidate.yaml) 表达为静态拓扑；独立 POSIX 短链 egress CLI 尚未加入该 Compose topology，必须由经过审阅的 POSIX supervisor 放在实际 egress side。Windows 一体化短链使用另外的进程内直连路径，不属于这个拓扑。Compose 从未在当前机器运行；单独执行任一 CLI 也不会自动产生隔离。

### 短链展开：Windows 本机与 POSIX 候选

Windows `video-download-local-app --allow-direct-network` 显式启用进程内受控直连，不监听 socket、不创建常驻事件循环线程；每个已接纳请求使用独立短生命周期 transport，最多同时 4 个请求，容量已满时快速拒绝。关闭后不接纳新请求，已接纳请求仍受原有时限约束。它复用逐跳地址、TLS/peer 与消息校验，不构成独立 egress 进程、网络 namespace 或权限隔离。普通 `video-download-control` 不会因为运行在 Windows 而自动启用此路径。

通用控制面的 POSIX 路径仍默认关闭。显式启用后，控制面逐跳解析 DNS、拒绝所有非公网地址并把完整 answer set 签名发送到私有 UDS；egress 服务只连接已批准的 numeric IP，同时保留原 hostname 做 TLS SNI/证书与 HTTP `Host`，再对实际 peer 做 attestation。响应不自动 redirect，只有有界 `Location` 回到控制面；body、任意响应头、异常原文和最终临时签名 URL 不会进入 API、业务数据库、backup manifest 或普通 audit。

两条路径均不向短链注入 Cookie。默认单链最多 5 跳、16 个 DNS answer、每跳 5 秒、DNS 3 秒、整链 15 秒；Batch 对全部唯一短链共享 15 秒 aggregate budget，内建 resolver 每次都收到当前剩余时间并压缩自身总时限，到期后不再开始新展开。短链请求的 4 个槽与媒体 Worker 的总并发 2 / 单平台 1 是不同限制。自定义 injected resolver 是 trusted-contract boundary；同步 Python 调用无法强制抢占一个忽略 `timeout_seconds` 的实现。DNS 使用最多四个不可取消的 bounded worker slot；若底层 `getaddrinfo` 挂死，后续解析会 fail-fast，不能把这视为高可用 DNS 隔离。POSIX replay 文件操作也从 event loop 卸载到有界 slot，但已进入 kernel/文件系统的 blocking I/O 不能强制取消。本轮只验证离线与模拟网络，真实 DNS/TLS/redirect/平台和 Linux UDS 仍未验收。

启用需要两个服务以同一有效 UID 运行，并使用没有 symlink/reparse、owner/mode 合格的绝对路径。Socket parent 必须私有，socket 为 `0600`；key 是恰好读取的 32–4096 bytes 原始随机数据、单 hard link、同 UID 且无 group/other 权限；replay directory 为同 UID `0700` 且必须在可靠本地文件系统，不得放在可能无限阻塞的网络/FUSE share。先由 supervisor 启动 `video-download-short-link-egress --unix-socket ... --shared-key-file ... --replay-directory ...`，确认健康和退出清理，再向控制面同时注入三个环境变量。详细故障边界见 [Runbook](docs/RUNBOOK.md)。当前 Windows 构建对显式 gate fail closed，真实 AF_UNIX、POSIX mode/owner、TLS、DNS 和平台 redirect 必须在目标 Linux 另行验收。

### 管理员凭证流程

`register` 只把平台、名称、过期时间和 opaque `secret_ref` 写入数据库；Cookie source 路径和内容始终由部署环境保管。`assign --batch-id` 只选择与 profile 平台匹配的 queued Job；显式 `--job-id` 范围中只要存在跨平台或非 queued Job，就整体拒绝而不做部分分配。`clear` 只允许 queued Job；`disable` 禁用 profile 并为活动 Job 请求协作取消。网页和公开 HTTP API 不接收 Cookie、路径、profile ID 或 secret ref。

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

下载备份/恢复 CLI 要求精确 Schema 11；graph snapshot、relation、generation、active-snapshot 指针、claim-gate singleton、Schema 9 capability archive、不可变 evidence 和完整 decision chain 都进入同一 SQLite 一致快照，已发布资产按 manifest 复制，顶层 `logs`、`temporary` 与 `assets/.staging` 排除。仍应先正常停止一体化应用再创建运维备份，使 snapshot 中的 gate 已关闭且活动 lease/临时输出边界清晰。Schema 10 备份必须先由匹配的 v0.15.0 历史应用恢复到独立根，Schema 8/9 备份同样先由各自匹配版本恢复；随后再由当前版本在副本上 forward-migrate，并另行制作、实际恢复 Schema 11 基线。当前版本不直接 restore 旧 schema 备份，旧程序也不得打开 Schema 11。[Iteration 0.6 offline graph evidence](validation/iteration-0.6-graph-v2-offline-evidence.md) 与 [Iteration 0.5 recovery evidence](validation/backup-restore-drill-iteration-0.5.md) 保留为当时的历史记录，不替代本轮或目标 Linux/NAS 的恢复验收。运行日志与部署所有 Cookie source 都不在业务备份内。

上传目录使用另一套格式和命令。先正常停止所有使用该上传根的 Open-Flame 实例，再执行：

```powershell
uv run video-upload-backup create `
  --source-upload-root C:\vdc\data-uploads `
  --backup-target D:\vdc-backups\upload-backup-20260907

uv run video-upload-backup restore `
  --backup-root D:\vdc-backups\upload-backup-20260907 `
  --restore-upload-root C:\vdc-upload-restore-drill
```

上传命令只接受精确 Upload Schema 3，并拒绝活动上传 Worker。备份格式 2 包含静态上传数据库、`media_state=present` 且大小/SHA-256 一致的登记视频与封面，以及数据库内的非秘密账号元数据；排除 `private`、`runtime`、`incoming`、未登记媒体、锁和临时文件。恢复目标必须不存在，恢复后 `running` 任务为 `unknown`、`queued` 任务回到 `draft`，活动账号须重新登录或检查；命令不构造上传 backend，也不访问平台。0.28.0 的应用生命周期以及 started/standby 上传服务会持有 shared activity lease，备份或恢复所需的 exclusive lease 会拒绝这些存活实例。执行前仍应正常停止所有实例；该协作锁无法约束未实现此协议的旧版本或外部写入者，不能把一次 CLI 成功解释为在线备份承诺。

## 安全边界

- 入口仅接受 HTTP/HTTPS、已列出的平台域名、默认 scheme 端口，拒绝 URL 凭证和未支持链接类型；TikTok 与 Douyin 独立建模，TikTok 当前只接收单视频直链，Instagram 当前只接收 Reel 直链。
- 控制面无认证且强制 loopback；尚无可直接部署的 HTTPS、用户认证、限流或配额层。
- 受控出站 proxy 要求显式非空 host allowlist，做 DNS A/AAAA 解析、非公网/映射/过渡地址阻断、数值 IP 连接与 peer 校验，并对请求、响应、连接数、字节数和时限设上限。
- POSIX 短链 control↔egress 协议使用 canonical JSON、domain-separated HMAC-SHA256、fresh nonce、短有效期、clock-skew 检查与 crash-durable replay marker；Windows 进程内路径使用逐请求临时 key 与内存 replay store。逐跳只允许目标平台的 HTTPS hostname，响应只披露 `Location`；这些机制不等同于已在真实网络验证或形成 Windows 隔离。
- Linux 网络隔离 guard 与 TCP→UDS relay 能验证“Worker namespace 仅 loopback + 私有 Unix socket”；candidate Worker 在数据库初始化前及每次 claim 前复核，Compose 候选让 Worker/relay 共享 `network_mode:none` namespace。
- `yt-dlp` 候选命令固定禁用用户配置、插件目录、远程组件和 JS runtime，并限制输出目录、格式、大小、重试、Cookie 副本与 proxy；这仍不能替代容器/防火墙对直连网络的阻断。
- 业务数据库只保存 profile ID 与 opaque ref；claim 后 JobLease 仅向 `ProbeRequest` / `DownloadRequest` 传递 opaque ref，不持久化 Cookie 路径或内容。Cookie 源到 Attempt 私有 `0600` 副本的组件已测试路径、权限、identity、swap 与 fsync。当前 override 只是 service-level isolation：单个 Worker 仍能读整棵多平台 source root，真实凭据上线前需要 credential sidecar、per-platform Worker 或 per-attempt mount namespace。
- 代码中的凭证流已接通；credential-free base Compose 不含 Cookie，显式 override 才把 root-owned source/mapping 只读挂到 Worker。Host preflight 拒绝路径重叠、不安全祖先、named/default ACL、links、错误 owner/mode 与 race；full acceptance 还要求 host `/proc/sys/kernel/core_pattern` 可读且不是 pipe collector，因为 Worker `RLIMIT_CORE=0` 单独不能排除主机侧 crash capture。这些仍只有静态/Windows 离线证据。
- Linux runner 的 execute mode 还要求 effective root，并固定使用通过 Python 3.12+ 检查的 `/usr/bin/python3`。Docker endpoint 判定遵循 `DOCKER_CONTEXT` 高于 `DOCKER_HOST` 的官方 precedence；execute 拒绝 inherited Docker/Compose/BuildKit endpoint、config 与 project/profile 控制变量，default context 仍须解析为 local Unix Linux daemon。Fresh build tag 只作初始名称；runner 随即捕获并验证不可变 local `sha256:...` image ID，把该 ID 写进 effective Compose，再递归拒绝 `$` 并在 private env 同目录冻结为 `root:root 0600` 文件。二次渲染必须与原 JSON 深等值并再次通过完整校验；之后 Compose mutation 只用 frozen file，direct `docker run` 和 runtime `Image` inspect 均绑定同一 ID，checkpoint 也会重验它，避免 tag rebind 改变验收对象。正常退出只按 identity/snapshot 删除冻结文件，crash 残留必须在受保护目录人工定点审计。
- Dockerfile 不再引用外部 syntax image；两个 Python `FROM` 都硬编码同一 `python:3.12.13-slim-bookworm` digest，不能由 ARG 覆盖。`pyproject.toml`/`requirements.build.in` 精确固定 `hatchling==1.27.0`，`requirements.build.lock` 与 `requirements.runtime.lock` 固定 exact version + SHA-256。唯一允许 Python package 网络访问的是 `pip download --no-deps --only-binary=:all: --require-hashes`；后续 build-dependency install、project wheel build、runtime install 均 `RUN --network=none` + `--no-index`，项目 wheel 用 `--no-build-isolation --no-deps` 构建并按精确路径安装，最后执行 `pip check`。Runner 的 network-none runtime contract 还精确核对 14 个 runtime-lock distributions + `video-download-control==0.28.0`，并拒绝五个 build-only distributions 泄漏。Lock 可能同时列 wheel/sdist hashes，但 `--only-binary=:all:` 在命令层拒绝 sdist；这些 target Linux 检查尚未执行。
- `VDC_ENABLE_X_GRAPH_V2` 只改变新 X Input 的任务形状，不会让 adapter 获得 exact selector。当前真实 `YtDlpAdapter.supports_exact_selector=False`；即使误入队，Worker claim 也会 fail closed，因此不要把该保护当作启用方案。
- 下载输出、元数据和媒体文件始终视为不可信输入；不绕过 DRM、付费墙、验证码、地区或其他访问控制。

因此，proxy、relay、命令工厂或候选 adapter 的离线测试通过，不等于生产网络隔离已经成立，也不等于平台已经可用。Proxy 只能约束实际经过它的流量。

## Stage 0 验证

[`validation/`](validation/README.md) 包含 Stage 0 CSV v3 模板、明确标为 offline 的工程证据，以及 YouTube、X、Instagram 共三个精确输入的历史本机实测摘要。Bilibili 的重复诊断也只证明记录时的间歇结果。这些结果不构成 Stage 0 或整个平台支持证据。生成 aggregate-only Stage 0 报告：

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

CSV v3 的样本和结果都强制包含 `job_kind`，结果还必须包含由 `--print-product-identity` 输出的当前完整 build identity；旧 media-count 表头、重复表头、行宽不符、缺少产品 identity、仅填裸版本或 build hash 不匹配都会 fail closed。`download` 的 output count 是已发布且完成完整验证的媒体资产数；`discover` 则是不可变 discovery snapshot 中唯一 child/source item 数。只有同一 `platform × source_type × job_kind × adapter × downloader_version × environment × product_version` 的至少 10 条公开正向样本、独立负向样本和最新连续三轮完整回归均满足门槛时，证据才具备显式审批资格；较新的 partial run 不会被跳过。来源去重使用规范的 `platform/source_type/source_id`，外层保留 `job_kind`，不会把 TikTok handle 当视频身份。由于同一 Bilibili 投稿同时存在 BV/av 公共别名而当前不维护本地可信转换，Stage 0 manifest 保守地只接受 BV；普通下载入口仍支持 BV 与 av。报告本身不运行 `yt-dlp`、不写数据库，也不改变 API/UI。证据导入与 approve/revoke 的本地 CLI、幂等/CAS 规则及非密码学信任边界见上方“Stage 0 v3 证据与决定”。

## 已知未完成项

- Stage 0、六平台完整真实链接回归和任何 `verified` 能力均未完成；Instagram 只有一个全链路正向样本，Bilibili 在相同无 Cookie 条件下有成功 probe 也有 HTTP 412，当前仅将其窄化归类为 `rate_limited` 以退避/冷却，并未修复或证明稳定下载能力；Douyin 无 Cookie 尝试正确终止为 `authentication_required`，TikTok 未实跑。这些结果都不能外推为平台级兼容性。
- candidate Worker、Compose、Cookie override 与 Linux acceptance runner 只有离线/静态证据；镜像未构建，Linux namespace/UDS/ACL/core limit、non-pipe `core_pattern`、runtime read-only bind、immutable image-ID chain 与 frozen-config lifecycle 均未实跑，真实 Cookie source 未创建或挂载。Cookie source 所在 host filesystem 的 `nodev,nosuid,noexec` 是 operator prerequisite，当前 runner/YAML 不证明；单 Worker 对整棵 Cookie source root 的可读性仍是显式残余风险。发布还须验证目标 registry 的 base/tool manifests 与 platform artifacts 可用，审阅镜像/包/批准 hashes 的 provenance 与 target wheel availability，并在目标 daemon 自带 Dockerfile frontend/BuildKit 上证明 Dockerfile 1.3+ `RUN --network=none` 被支持和落实。移除外部 syntax tag 不等于这些 target-specific build inputs 已获验证。
- 备份/恢复 CLI 已更新为精确 Schema 11；Schema 8/9/10→11 的生产迁移须用各自兼容版本建立并恢复迁移前基线，再由 v0.19.0 在副本上迁移并实际恢复 Schema 11 基线。旧记录不替代目标 Linux/NAS、真实容量、独立介质或灾难主机验收。已有本地结构化排障日志，但仍无集中式生产日志管线、反向代理、认证或磁盘告警。
- 当前百分比明确是单个 Job 的“阶段估算”，不是跨字幕、视频、音频和 fragment 的精确总字节百分比。未知总大小只维持 heartbeat；保守单流下载在后处理/完成前最多到下载阶段的一半。页面每 2 秒轮询，极短的 `postprocessing` 状态可能不被每次肉眼观察到，但 API/数据库会保留被轮询到的中间状态。
- Windows 一体化入口已接短链受控直连，但不是隔离；通用控制面 gate 仍默认关闭，POSIX 短链 egress 尚未集成到 Compose/supervisor。Linux AF_UNIX/owner/mode、真实 TLS/DNS/redirect、平台登录态和整批最坏延迟未验收。Replay blocking I/O 只是有界卸载而非可强制取消；injected resolver 仍须遵守 timeout contract。
- Schema 8 X graph-v2 编排已经实现并由 `ScriptedGraphFakeAdapter` 离线验证，但真实 `YtDlpAdapter` 仍不支持 exact selector；`VDC_ENABLE_X_GRAPH_V2` 默认 `0`，真实 X graph 继续 `candidate/disabled`，不能标记为 `verified`。旧 flat-v1 数据仍可读。
- Caption 当前只记录 `origin=platform`，尚未可靠区分平台人工字幕与自动字幕；缩略图/字幕内容也尚无真实样本验证。
