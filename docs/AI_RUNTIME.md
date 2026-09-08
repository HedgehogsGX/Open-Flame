# Open-Flame 0.28.0 可选 AI Runtime

Open-Flame 0.28.0 使用独立的轻量 AI runtime 执行自动听写、分段文字翻译和标准音色配音。它与主应用 `.venv`、下载数据、编辑数据库和上传 runtime 分离；runtime 只包含官方 CPython embeddable 文件、Open-Flame worker、协议和标准库 OpenAI provider，不安装 OpenAI SDK，也不捆绑模型权重。发布后源码进一步加入逐 operation 的精确 authorization、调用前输入硬上限和 Editing Schema 4 脱敏远程调用账本，仍未增加常驻 AI 服务、SDK 或本地模型权重。

当前 builder 只在 Windows x64 上构建，技术门禁只接受结构与运行身份符合要求的 64-bit CPython 3.12 或 3.13 embeddable ZIP；运维上只应使用 Python.org 官方归档。推荐基线为 CPython **3.13.15**：

```text
https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip
```

builder 本身不下载文件，也不会调用 OpenAI API。请先从 Python.org 获取 ZIP，并从独立的官方发布材料核对它的 SHA-256。只对刚下载的同一文件执行 `Get-FileHash`，再把结果当作“预期值”，只能发现后续文件变化，不能证明来源真实。

Python.org 为上述 3.13.15 Windows embeddable x64 ZIP 公布的 SHA-256 是 `d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf`。仍应从官方发布页独立核对后再使用。

## 1. 离线构建

默认 Windows 应用数据根为 `%LOCALAPPDATA%\Open-Flame\video-download-control\data`，因此 AI runtime 默认必须构建到同级的 `%LOCALAPPDATA%\Open-Flame\video-download-control\data-ai-runtime`。自定义 `VDC_DATA_ROOT` 时，输出目录同样是在数据根名称后追加 `-ai-runtime`。

先准备以下三个绝对路径。`$pythonExpectedSha256` 中的占位符必须替换为从官方来源独立核对的 64 位十六进制 SHA-256；不要照抄占位符执行。

```powershell
$pythonEmbedZip = "C:\Installers\python-3.13.15-embed-amd64.zip"
$pythonExpectedSha256 = "<从 Python.org 官方材料独立核对的 64 位 SHA-256>"
$aiRuntimeRoot = "$env:LOCALAPPDATA\Open-Flame\video-download-control\data-ai-runtime"

$pythonActualSha256 = (
  Get-FileHash -LiteralPath $pythonEmbedZip -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($pythonActualSha256 -ne $pythonExpectedSha256.ToLowerInvariant()) {
  throw "CPython embeddable ZIP SHA-256 不匹配，停止构建。"
}
```

已通过 wheel 或 editable 安装 Open-Flame 时，执行：

```powershell
python -m video_download_control.editing.ai_runtime_builder `
  --python-embed-zip $pythonEmbedZip `
  --python-sha256 $pythonExpectedSha256 `
  --output $aiRuntimeRoot
```

普通源码 Setup 不会把项目安装进 `.venv`。从仓库根目录运行时，临时提供 `src`：

```powershell
$aiPreviousPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = (Resolve-Path .\src).Path
  & .\.venv\Scripts\python.exe `
    -m video_download_control.editing.ai_runtime_builder `
    --python-embed-zip $pythonEmbedZip `
    --python-sha256 $pythonExpectedSha256 `
    --output $aiRuntimeRoot
} finally {
  $env:PYTHONPATH = $aiPreviousPythonPath
}
```

`--output` 必须是尚不存在的绝对目录。builder 在同级私有 staging 目录中逐项安全解压，验证 CPython 身份为 64-bit 3.12/3.13，复制固定 worker、协议和 provider，生成 manifest，完成全量校验后才以目录重命名发布。失败不会用半成品覆盖既有 runtime。

成功时标准输出是一行 JSON，至少包含：

```json
{"code":"ready","manifest_sha256":"<64 位十六进制摘要>","output":"C:\\...\\data-ai-runtime","ready":true,"runtime_id":"open-flame-ai-runtime","runtime_version":"1"}
```

实际字段以命令输出为准。请把整行输出和 CPython ZIP 的来源、版本、官方预期 SHA-256 一起保存到 runtime 目录之外的发行或运维记录中；不要修改 `manifest.json` 补写这些记录。

本轮使用上面的 CPython 3.13.15 ZIP 按当前 worker/protocol/provider 源码重建，runtime manifest SHA-256 为 `598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378`。`--check` 返回 `ready=true`，应用将这个结果标记为 `integrity=verified / provider_health=unverified`；因为本机没有 `OPEN_FLAME_AI_OPENAI_API_KEY`，三项能力都保持 `blocked / ai_provider_auth_missing`。该摘要只绑定本轮对应源码字节；若相关源码再变化，必须重建并记录新摘要。

## 2. Manifest 与完整性复核

`manifest.json` 精确声明 Python、worker、协议、provider、模型及每个 runtime 文件的大小和 SHA-256。加载 runtime 时会枚举完整文件集合并重新计算全部散列；文件新增、缺失、内容变化、manifest 结构变化、symlink/junction/reparse 重定向或平台/Python 身份不符都会失败关闭。每次执行模型操作前，还会重新核对 manifest 以及该 provider、模型、Python、worker 和协议所需的文件。

执行路径会在准备完受限输入后、把可执行路径交给 subprocess runner 前再次复核，并在返回后复核；听写派生音频先复制为私有 scratch 中的固定副本并重新核对大小/SHA-256。这个机制用于发现意外漂移与失败关闭，不是抵抗可用同一系统账号同时改写应用/runtime 的攻击者的密码学 attestation。当前 runtime 只应在受信任的单用户主机使用，维护或替换 runtime 时必须先停止应用；后续仍需为安装器与执行器加入跨进程 runtime lease 或不可变 snapshot。

构建后或启动应用前可执行只读检查：

```powershell
python -m video_download_control.editing.ai_runtime_cli `
  --root $aiRuntimeRoot `
  --check
```

源码环境继续使用上一节的临时 `PYTHONPATH` 和 `.venv` 解释器。`--check` 只检查本机目录和散列，不连接 OpenAI，不验证账号权限、模型可用性、网络、配额或计费，也不登录或上传任何国内平台。

需要更换 CPython 版本或重建 runtime 时，先正常停止所有使用该数据根的 Open-Flame 实例。将旧 runtime 整体改名留档，再用一个尚不存在的目标目录重新构建；不要混合两棵 runtime，也不要手改 manifest 或只替换其中一个文件。

## 3. OpenAI 凭据

唯一读取的凭据变量是：

```text
OPEN_FLAME_AI_OPENAI_API_KEY
```

把密钥注入启动 Open-Flame 的进程环境或受控的服务/密钥管理器。普通 `Start-Open-Flame.cmd` 只把这一个精确变量传给执行编辑、AI 和上传管理的 control child；直连下载 Worker 不继承它，未声明的 `OPEN_FLAME_AI_*` 及通用 token/secret 变量仍会被剔除。密钥值必须非空、不含码位低于 33 的字符，且最多 16384 个字符。不要把真实密钥写入命令历史、仓库、`.env`、`manifest.json`、编辑 recipe、SQLite、日志、备份或发行包。下面只展示变量名，不能把占位符当成密钥：

```powershell
$env:OPEN_FLAME_AI_OPENAI_API_KEY = "<仅向当前 Open-Flame 进程树提供的 API key>"
& .\Start-Open-Flame.cmd
Remove-Item Env:OPEN_FLAME_AI_OPENAI_API_KEY
```

没有该环境变量时，runtime 仍可通过完整性检查，但云能力会以 `ai_provider_auth_missing` 保持不可执行。密钥存在也只说明配置条件满足；首次真实请求仍可能因账号模型权限、地区、网络、配额、速率限制或供应商变更失败。

## 4. 固定 provider、模型与标准音色

当前 manifest 固定 provider ID 为 `openai`，并声明以下模型：

| 操作 | 模型 | 当前实现边界 |
| --- | --- | --- |
| 自动听写 | `whisper-1` | 请求 `verbose_json` 与 `segment` 时间戳，并逐 segment 建立时间轴；最多 10000 个 cue，每个 cue 最多 4096 字符。需要可审阅分段时间时优先选择它。 |
| 分段翻译 | `gpt-5.6-luna` | 通过 Responses API 的严格 JSON Schema 返回 `segment_id` 与 `target_text`；本地再次核对 cue 数量、ID、顺序和时间，最多每批 50 个 cue，并受 4 MiB 请求边界约束。 |
| 自动配音 | `gpt-4o-mini-tts` | 每个 cue 单独请求 WAV；单次文字最多 4096 字符，语速范围为 0.88～1.12。生成后由本地渲染器测量、对齐并混音。 |

配置这些模型 ID 不代表当前 API 账号一定能调用它们，也不冻结供应商侧价格或行为。authorization 会绑定 manifest 中的 model ID 和本地声明 revision，但远端 alias 仍可能在相同名称下由供应商改变实现、行为、价格或可用性，本地摘要无法自动证明这类服务端变化。实际可用性必须用该账号执行有授权的真实样本另行记录。

首批只允许以下 13 个内置标准音色：

```text
alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar
```

它们全部按非克隆音色处理；Open-Flame 0.28.0 不接受 custom voice、用户声音样本或声音克隆。UI 只列出 provider manifest 公布且 `is_clone` 不为真的音色。中文和其他语言的自然度、发音、风格及最终听感仍需真人试听，不能由 manifest 或一次生成自动判定。

### 4.1 逐操作 authorization

能力接口为每个 `provider × model × operation` 生成一份 canonical authorization 及 SHA-256。authorization 只包含可公开核对的定义，不含 API key、endpoint、媒体正文或 provider 响应，并精确绑定：

- authorization schema、runtime ID/version、AI protocol schema 和当前 manifest SHA-256；
- provider ID/kind、model ID 与 manifest 中的本地声明 revision；
- `transcribe`、`translate` 或 `synthesize` operation、`local`/`remote` 执行类型与该 operation 的精确 data egress；
- 本次 operation 的 effective limits。

新 AI task 把完整 authorization 写入不可变 request；配音把它写入不可变 recipe；单 URL 自动流程把三项完整 authorization 及其摘要写入冻结 profile。页面创建或确认时提交自己看到的绑定，服务层对 request/recipe/profile 做定义 CAS，并与刚重新核验的 runtime 能力比较。worker 领取后、构造 provider 前及真正调用前还会再比较；任何缺失、格式错误、摘要不一致、manifest/model 声明变化或 operation 不匹配都会失败关闭。

旧 AI task、recipe 和 Workflow profile 保持可读，便于解释历史状态；它们没有精确 authorization，因此不能直接确认、重试或执行。操作者须刷新能力后按当前定义重建任务、计划或流程。authorization 也不是跨 runtime 升级的永久许可。

核心进程规定的精确有效上限如下。当前版本没有自定义降额入口；持久化 authorization 必须与这些值完全一致，也不能通过修改 runtime manifest 替换或抬高：

| 操作 | 一次 authorization 的硬上限 | 计数口径 |
| --- | --- | --- |
| 听写 | 30 分钟、25 MiB 派生音频、1 次 provider 请求 | 完整选择范围和最终 M4A 在首次请求前一并核对 |
| 翻译 | 1000 cues、60000 输入字符、20 个调用单位 | 每 50 cues 估算一个调用单位；重复发送的 glossary 字符按每个单位计入 |
| 配音 | 600 cues、60000 输入字符、600 次调用 | 每个有文字的 cue 最多一次 Speech 调用；空 B-roll 不调用 |

这些硬上限只约束本机在一次已确认操作中允许送入 provider 的工作量。它们不是货币价格、token/秒数账单、账户额度或实际 usage ledger，也不能证明 provider 未对 unknown 请求计费。

### 4.2 Schema 4 远程调用账本

远程 `transcribe`、`translate` 和 `synthesize` 在进入隔离 provider 之前必须取得 Editing Schema 4 账本记录。本地 operation 不建立记录；`health` 是能力/运行时检查，也不写入账本或消耗 `request_units`。当前 OpenAI provider 的 `health` 只返回本地声明的 operation 和标准音色，不发出 OpenAI API 请求；它仍不能证明账号权限、模型可用性、网络、配额或价格。

`ai_invocations` 只保存 32 位 owner/invocation ID、operation、ordinal、attempt、`request_units`、authorization/owner definition/request fingerprint SHA-256、固定状态/reason code/reconciliation 结论和时间戳。它不保存媒体或文字正文、API key、环境变量值、本机路径、endpoint、HTTP header、provider request ID 或响应。定义字段不可修改，状态只允许以下单向转换：

```text
reserved → dispatched → responded
reserved → released
reserved → dispatched → unknown → reconciled
```

- `reserved` 在远程执行边界前持久化；若调用尚未 dispatch 就失败或取消，记录成为 `released`。
- `dispatched` 在 runner 取得请求前持久化；只有经过协议和本地输出验证的响应才成为 `responded`。dispatch 后出现 timeout、取消、进程错误或结果无法验证时，记录保守成为 `unknown`。
- 重启恢复同样把遗留 `reserved` 释放，把遗留 `dispatched` 标为 `unknown`，不会静默重放。
- `unknown` 只能带当前 `revision`、`acknowledge=true` 和固定结论 `not_accepted`、`accepted_without_result`、`abandoned` 之一转为 `reconciled`；没有自由文本，也不能撤销供应商侧请求或证明未计费。

Owner 完成前必须没有 `reserved`、`dispatched` 或 `unknown`。这些状态也阻止重试；已人工核对为 `accepted_without_result` 或 `abandoned` 后仍阻止重试，避免在已接受或已放弃的调用上建立重复后继。`responded`、`released`、`reconciled/not_accepted` 只表示账本允许继续评估，仍须满足原 AI task/render plan 的终态、唯一后继、authorization CAS 和再次确认规则。

听写 operation 记录 1 个单位；配音按每个有文字的 cue 建立 ordinal 记录，每项 1 个单位。翻译把整项 task 记录为一个 invocation envelope，`request_units=ceil(cues/50)`，上限 20。这个数用于在真正执行前冻结本地 authorization 工作量；OpenAI provider 还会根据 4 MiB 请求 envelope 缩小每批 cue，因此实际 HTTP 请求可能多于这个估算。`request_units` 不是精确 HTTP 请求计数、token usage、价格估算、供应商 request ID 或账单收据。

## 5. 媒体处理与时间轴审核

听写前，Open-Flame 在本机使用固定 FFmpeg 从不可变编辑源派生临时音频：移除视频、字幕、数据和 metadata，编码为 **mono、24 kHz、32 kbit/s AAC M4A**，并在上传前再次核对文件身份、大小和 SHA-256。发送给听写 API 的是该派生音频，不是原始视频文件。`/workflows` 会把 recipe 的第一个分段作为听写 clip，只编码并发送该范围，再把返回时间加回源时间轴；编辑页直接建立听写任务时当前没有 clip 控件，默认编码完整编辑源。本地派生器仍有 90 分钟的媒体处理边界，但当前 authorization 在首次 provider 请求前施加更严格的 **30 分钟且 25 MiB** 有效上限；不会偷偷截断、分片或降级上传，超过任一上限就失败并要求操作者调整输入。

推荐的人工审核顺序是：

1. 在编辑页建立听写任务，核对 provider、模型本地声明 revision、runtime manifest 摘要、硬上限和云端外发范围，再明确确认执行。
2. 听写结果保存为 `review` 时间轴。页面展开全部 cue，显示整数毫秒起止时间、原文、语言和可选 speaker；批准或拒绝前不会把它用作翻译来源。
3. 从已批准字幕建立翻译任务，明确确认执行。译文时间轴按相同 cue ID 和时间显示原文与译文；必须整份批准后才能写入 ready 编辑草稿。
4. 选择标准音色、语速和“替换原声/压低原声后叠加”，生成待确认编辑计划。配音逐 cue 生成 WAV；音频超过字幕时间槽时返回 `ai_speech_timing_overflow`，不会截断文字或顺延并覆盖后续 cue。
5. 确认本地渲染计划后，时间轴按每个已选分段过滤并从 0 ms 重新计时，分别生成 segment-local VTT 与 `dubbed_video`。分段边界切入 cue 会以 `ai_segment_boundary_splits_cue` 拒绝；没有 cue 的 B-roll 分段使用空 VTT 与本地静音，不调用 Speech API。保留原声时固定把原声压到 22% 后再混入配音；下载原件与此前编辑输出保持不变。上传仍有单独的账号和发布确认域。

单 URL 自动流程未授权“自动确认编辑”时，会在听写与翻译结果处停到 `awaiting_ai_review`，并引导到编辑页查看完整时间轴。若操作者主动启用自动确认编辑，该授权也允许流程确认 AI 调用和批准返回时间轴；profile 会冻结各 operation 的 authorization，确认时仍须与当前 runtime 做 CAS。这会跳过逐项人工停顿，应只用于已经接受该精确处理范围和结果风险的流程。runtime、model 声明、外发范围或 limits 变化后，旧流程不会继承新的能力，必须重建。

## 6. 云端外发、费用与取消边界

OpenAI provider 是远程 provider。当前执行会发送：

- 听写：由视频派生的压缩 M4A 音频及可选语言提示；
- 翻译：字幕文字、cue ID、源/目标语言和可选 glossary；
- 配音：逐 cue 文字、标准音色、语速和可选风格说明，并接收 WAV 音频。

这些内容会离开本机并由 OpenAI API 处理。manifest 对可能外发的数据类型作保守声明，页面在任务确认处显示该范围。请在发起前确认媒体和文字有权交给云服务处理，并根据当前供应商条款、保留政策和所在地区要求作出决定。

每次听写、翻译批次和逐 cue 配音都可能产生 API 费用；长时间轴会产生多次翻译和配音请求。当前固定输入/调用硬上限与 Schema 4 账本用于阻止越权工作量和不确定结果的静默重放，但仍不提供准确价格预估、供应商账单核对、token/音频 usage ledger 或远程 request ID reconciliation。操作者仍应在 OpenAI 账户侧设置可接受的配额和告警。取消会终止本机等待和后续处理，但服务端已接受的请求仍可能完成并计费；不要把本地 `canceled`、`unknown` 或人工 reconciliation 当作供应商已撤销、已返回结果或未计费的证明。允许的重试仍会建立待确认后继并重新执行完整步骤；已完成的批次/cue 可能再次计费。

## 7. 当前验证边界

构建成功、`--check` 返回 `ready`、页面列出 provider/model 或本地渲染 smoke 通过，只能证明对应本机 runtime 结构、散列、协议和本地媒体路径满足当前代码合同。

当前 0.28.0 发布后源码证据边界为：**未提供真实 OpenAI 凭据；未执行真实 OpenAI API 听写、翻译或配音；未执行 Bilibili、抖音或视频号的真实媒体上传与发布验收；未生成绑定当前开发提交的新 release receipt。** authorization/budget/ledger 的三个 ignored 本地 validator、Python compileall、编辑/自动流程页内联 JavaScript 语法检查、依赖一致性检查和本机浏览器检查已经通过；focused 既有测试当前仍为 41 passed、3 failed，三项失败都仍在断言旧 Editing Schema 1，测试文件按仓库策略未修改。详见 [Schema 4 远程调用账本记录](../validation/iteration-0.28.0-ai-invocation-ledger.md)。这些本地结果不能据此声称云模型在当前账号可用、远端 alias 未漂移、生成质量已由真人接受、费用已核对，或任一国内平台已经接收并公开发布视频。真实 API 与平台验收必须绑定同一冻结构建、明确授权的样本和平台后台结果另行记录。

2026-09-09 追加的普通 Start 密钥边界验证仅使用合成 sentinel 和本地重建 runtime：它确认 sentinel 只进入 control child，不进入下载 Worker；三项能力从 `blocked / ai_provider_auth_missing` 转为 `unverified / provider_health_required`；验证期间网络入口被强制拒绝，输出中没有 sentinel。该结果不是 provider health 或真实 API 验收。
