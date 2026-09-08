# Open-Flame 0.28.0 可选 AI Runtime

Open-Flame 0.28.0 使用独立的轻量 AI runtime 执行自动听写、分段文字翻译和标准音色配音。它与主应用 `.venv`、下载数据、编辑数据库和上传 runtime 分离；runtime 只包含官方 CPython embeddable 文件、Open-Flame worker、协议和标准库 OpenAI provider，不安装 OpenAI SDK，也不捆绑模型权重。

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

把密钥注入启动 Open-Flame 的进程环境或受控的服务/密钥管理器，并让应用子进程继承。不要把真实密钥写入命令历史、仓库、`.env`、`manifest.json`、编辑 recipe、SQLite、日志、备份或发行包。下面只展示变量名，不能把占位符当成密钥：

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

配置这些模型 ID 不代表当前 API 账号一定能调用它们，也不冻结供应商侧价格或行为。实际可用性必须用该账号执行有授权的真实样本另行记录。

首批只允许以下 13 个内置标准音色：

```text
alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar
```

它们全部按非克隆音色处理；Open-Flame 0.28.0 不接受 custom voice、用户声音样本或声音克隆。UI 只列出 provider manifest 公布且 `is_clone` 不为真的音色。中文和其他语言的自然度、发音、风格及最终听感仍需真人试听，不能由 manifest 或一次生成自动判定。

## 5. 媒体处理与时间轴审核

听写前，Open-Flame 在本机使用固定 FFmpeg 从不可变编辑源派生临时音频：移除视频、字幕、数据和 metadata，编码为 **mono、24 kHz、32 kbit/s AAC M4A**，并在上传前再次核对文件身份、大小和 SHA-256。发送给听写 API 的是该派生音频，不是原始视频文件。`/workflows` 会把 recipe 的第一个分段作为听写 clip，只编码并发送该范围，再把返回时间加回源时间轴；编辑页直接建立听写任务时当前没有 clip 控件，默认编码完整编辑源。当前单请求实现限制**实际选择范围**不超过 **90 分钟**、派生文件不超过 **25 MiB**；不会偷偷截断、分片或降级上传，超过任一上限就失败并要求操作者调整输入。

推荐的人工审核顺序是：

1. 在编辑页建立听写任务，核对 provider、模型和云端外发范围，再明确确认执行。
2. 听写结果保存为 `review` 时间轴。页面展开全部 cue，显示整数毫秒起止时间、原文、语言和可选 speaker；批准或拒绝前不会把它用作翻译来源。
3. 从已批准字幕建立翻译任务，明确确认执行。译文时间轴按相同 cue ID 和时间显示原文与译文；必须整份批准后才能写入 ready 编辑草稿。
4. 选择标准音色、语速和“替换原声/压低原声后叠加”，生成待确认编辑计划。配音逐 cue 生成 WAV；音频超过字幕时间槽时返回 `ai_speech_timing_overflow`，不会截断文字或顺延并覆盖后续 cue。
5. 确认本地渲染计划后，时间轴按每个已选分段过滤并从 0 ms 重新计时，分别生成 segment-local VTT 与 `dubbed_video`。分段边界切入 cue 会以 `ai_segment_boundary_splits_cue` 拒绝；没有 cue 的 B-roll 分段使用空 VTT 与本地静音，不调用 Speech API。保留原声时固定把原声压到 22% 后再混入配音；下载原件与此前编辑输出保持不变。上传仍有单独的账号和发布确认域。

单 URL 自动流程未授权“自动确认编辑”时，会在听写与翻译结果处停到 `awaiting_ai_review`，并引导到编辑页查看完整时间轴。若操作者主动启用自动确认编辑，该授权也允许流程确认 AI 调用和批准返回时间轴；这会跳过逐项人工停顿，应只用于已经接受该处理范围和结果风险的流程。

## 6. 云端外发、费用与取消边界

OpenAI provider 是远程 provider。当前执行会发送：

- 听写：由视频派生的压缩 M4A 音频及可选语言提示；
- 翻译：字幕文字、cue ID、源/目标语言和可选 glossary；
- 配音：逐 cue 文字、标准音色、语速和可选风格说明，并接收 WAV 音频。

这些内容会离开本机并由 OpenAI API 处理。manifest 对可能外发的数据类型作保守声明，页面在任务确认处显示该范围。请在发起前确认媒体和文字有权交给云服务处理，并根据当前供应商条款、保留政策和所在地区要求作出决定。

每次听写、翻译批次和逐 cue 配音都可能产生 API 费用；长时间轴会产生多次翻译和配音请求。Open-Flame 0.28.0 不提供准确费用预估、预算上限、供应商账单核对或远程 request ID reconciliation，操作者应在 OpenAI 账户侧设置可接受的配额和告警。取消会终止本机等待和后续处理，但服务端已接受的请求仍可能完成并计费；不要把本地 `canceled` 当作供应商已撤销或未计费的证明。AI task 或配音计划重试会建立待确认后继，确认后重新发送完整步骤；已完成的批次/cue 可能再次计费。

## 7. 当前验证边界

构建成功、`--check` 返回 `ready`、页面列出 provider/model 或本地渲染 smoke 通过，只能证明对应本机 runtime 结构、散列、协议和本地媒体路径满足当前代码合同。

当前 0.28.0 开发证据边界为：**未提供真实 OpenAI 凭据；未执行真实 OpenAI API 听写、翻译或配音；未执行 Bilibili、抖音或视频号的真实媒体上传与发布验收。** 因此不能据此声称云模型在当前账号可用、生成质量已由真人接受、费用已核对，或任一国内平台已经接收并公开发布视频。真实 API 与平台验收必须绑定同一冻结构建、明确授权的样本和平台后台结果另行记录。
