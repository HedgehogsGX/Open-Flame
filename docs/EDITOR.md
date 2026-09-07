# Open-Flame 0.27.0 编辑工作台指南

日期：2026-09-08
适用范围：Open-Flame 0.27.0 当前源码中的本地编辑切片

编辑工作台位于 `/edits`，负责在已登记的下载视频与上传器之间生成可核对的派生文件。当前生产能力是**视频分段**与**封面制作**。自动听写、自动翻译和 AI 配音只有时间轴、recipe 与 provider 合同；隔离 AI runtime、模型、云端凭据及真实试听尚未安装或验收，因此当前页面把这些能力显示为“尚不可用”。

本指南不证明任意真实视频均能正确处理，也不证明 Bilibili、抖音或视频号已经接收、审核或公开任何成品。当前本地证据见[Iteration 0.27.0 编辑工作台记录](../validation/iteration-0.27.0-editing-workspace-evidence.md)。

## 1. 完整工作流

```text
ready 下载视频
    │ 进入编辑
    ▼
显式复制到独立编辑根并复核 SHA-256
    ▼
编辑项目 + 版本化草稿
    ▼
冻结当前草稿为 review 计划
    │ 人工核对并明确确认
    ▼
单个本地 worker 渲染
    ▼
ready 编辑成品（分段 MP4 / 封面 PNG）
    │ 对视频点击“用于上传”并再次显式导入
    ▼
上传域中的独立媒体副本
    │ 填写平台参数、创建本地投稿草稿、逐任务确认
    ▼
上传适配器才可能被调用
```

输入网址只创建下载任务。它不会自动建立编辑项目、自动开始渲染、自动导入上传，也不会自动投稿或发布。

### 1.1 从下载成品建立编辑项目

1. 等待下载任务产生 `ready` 且 `media_kind=video` 的原始成品。
2. 在下载页对应成品上点击“进入编辑”。链接只携带下载资产 ID；打开页面本身不会复制视频或启动处理。
3. 在编辑页填写项目名称，点击“复制此下载成品并建立项目”。这是实际导入边界。
4. 服务重新解析下载资产，确认它仍为 ready 视频，复制文件到编辑域，并在复制过程中重新计算大小与 SHA-256。
5. 下载域登记摘要、源文件摘要或文件身份不一致时，导入失败关闭。渲染前处理器还会按计划来源的登记大小和 SHA-256 重新读取完整编辑源，再以同一文件身份约束后续 probe/FFmpeg；下载原件不会被移动、覆盖或重编码。输入 demuxer 由受管后缀固定：MP4/MOV/M4V 使用 `mov`，MKV/WebM 使用 `matroska`；ffprobe 与 FFmpeg 只允许 `file` protocol，并复核对应的精确 format name。伪装成视频的 ffconcat/playlist 不能借已登记源摘要读取同目录其他媒体。

编辑导入目前接受 `.mp4`、`.mkv`、`.mov`、`.webm` 和 `.m4v`，单个源文件上限为 16 GiB。目标磁盘必须至少保留“待复制文件大小 + 64 MiB”的可用空间。符号链接、reparse point、非普通文件及多 hard-link 文件会被拒绝。

### 1.2 保存版本化草稿

新项目自动建立空的草稿 `v1`。编辑页目前要求至少添加一个分段；每次点击“保存编辑草稿”都会追加一个新版本，不会覆盖旧版本。

保存请求携带 `expected_version`。如果另一页面已经先保存了新版本，旧页面会收到 `draft_version_conflict`，必须刷新并根据新版本重新编辑。每个草稿保存完整 canonical recipe 及其 SHA-256。

旧草稿和已经建立的处理计划保持不变。用户可以继续保存 `v3`、`v4`，而基于 `v2` 的旧计划仍精确引用 `v2`。

### 1.3 生成待核对计划

“生成待核对处理计划”会先保存当前表单，再把该草稿版本、完整 recipe 和 recipe SHA-256 冻结为新的 render plan。新计划状态为 `review`，代码为 `explicit_confirmation_required`。

建立计划不会运行 FFmpeg。用户应核对计划引用的草稿版本、分段和封面参数，然后单独点击“确认并开始本地处理”。只有这次明确确认才把计划转为 `queued`。

### 1.4 本地渲染与编辑成品

应用内只有一个本地编辑 worker。第一次打开编辑服务时，它先在编辑根旁取得跨进程独占 activity lease，并在 worker 与已进入服务层的 API 操作全部结束前持续持有；同一编辑根的第二实例会以 `editing_worker_busy` 失败，不会先恢复或改写第一个实例的计划。它按确认顺序领取一项 `queued` 计划，并为该次执行生成随机 claim token。输出先写入该计划和 claim 私有的 staging 目录，验证完成后才复制到正式 `assets/` 并登记为 `ready`。

页面中的“下载成品”读取正式受管输出。每次读取都会在同一个打开的文件句柄上复核大小和 SHA-256，同时建立 1 MiB 分块摘要；响应每个分块前会再次核对其摘要，等长原地改写也不能把新字节混入完整响应。实现不会校验后重新按路径打开，也不会为大文件建立完整 TEMP 副本；文件缺失或被修改时不会继续提供。源视频预览使用相同边界。

### 1.5 显式导入上传

只有 `ready` 的 `segment` 或未来的 `dubbed_video` 可成为上传视频来源。封面、字幕和独立音频不能通过视频入口导入。

1. 在视频编辑成品上点击“用于上传”。
2. 上传页显示待导入提示，但不会因 URL 参数自动复制文件。
3. 用户点击导入按钮后，上传服务从编辑域解析 ready 成品，复制到独立上传媒体根并重新计算 SHA-256。
4. 导入完成只新增一个上传来源，**不会创建投稿任务**。
5. 用户仍须选择账号、填写各平台标题、标签、封面、发布时间等参数，创建本地草稿，再逐任务明确确认。

因此存在三次相互独立的动作：确认编辑渲染、导入上传来源、确认投稿任务。前一次动作不授权后一次动作。

## 2. 当前分段能力

一份 recipe 最多包含 100 个分段。时间在 UI 中用秒输入，在 API 和数据库中保存为整数毫秒。

- 每段必须满足 `0 <= start_ms < end_ms <= 604800000`。
- 每段至少 100 ms。
- 分段必须按时间排序且不能相互重叠；相邻分段可以首尾相接。
- 起止时间必须落在真实源视频时长内。
- UI 可读取播放器当前时间作为起点、终点或封面抽帧位置。
- 片段名称只作为 recipe 中的显示信息，不进入命令参数或文件路径。

每段都重新编码为固定安全文件名 `segment-001.mp4`、`segment-002.mp4` 等。当前处理参数为 H.264（`libopenh264`、4 Mbit/s、`yuv420p`）和可选 AAC（192 kbit/s）；源视频没有音轨时，输出也不伪造音轨。字幕流、data stream 和源 metadata 不写入分段，输出使用 `+faststart`。

单次计划的全部正式输出上限为 8 GiB。开始 FFmpeg 前会按固定视频/音频码率、逐段余量和可选封面估算计划空间，并为 staging 到正式 assets 的复制按两份峰值预算，再额外保留 64 MiB。每个 FFmpeg 输出还带固定 `-fs` 上限；运行期间约每 250 ms 检查受管目标大小、累计输出和剩余空间，越界时终止进程树并清理部分文件。空间不足返回 `editing_storage_full`，输出预算越界返回 `media_output_too_large`。

完成前会用 ffprobe 复核 MP4 container、H.264、音频 codec、尺寸和实际时长。实际时长与计划时长误差超过 500 ms 时整项失败，已有部分输出不会发布。

## 3. 当前封面能力

封面从源视频指定时间抽取一帧，再在内存中完整解码、居中裁切、缩放并输出静态 PNG。页面当前提供以下比例：

| 比例 | 输出尺寸 |
| --- | --- |
| 16:9 | 1280 × 720 |
| 4:3 | 1200 × 900 |
| 3:4 | 900 × 1200 |
| 9:16 | 720 × 1280 |
| 1:1 | 1080 × 1080 |

底层 recipe 还支持 `source`：保持源比例，最长边不超过 1920 × 1080，且不放大；0.27.0 页面没有暴露该选项。底层也定义了副标题字段，但当前页面只提供标题，副标题保存为空。

标题会自动换行并放在底部黑色圆角底板上。页面限制标题 80 个字符，API 上限为 120 个字符。纯可打印 ASCII 使用 Pillow 随 12.3.0 wheel 提供的内嵌默认字体；标题或副标题含非 ASCII 字符时，只读取固定 OS allowlist 中的系统字体。Windows 依次检查 `C:\Windows\Fonts` 下的 Microsoft YaHei、Noto Sans SC、DengXian 与 SimHei 固定文件名；Linux 只检查 `/usr/share/fonts` 下固定的 Noto Sans CJK 路径。不会调用 fontconfig、接受用户字体路径、联网或从 CDN 下载字体，也不会把系统字体复制进编辑数据或发行包。

候选字体单文件最多 32 MiB。处理器从经过普通文件与稳定 identity 检查的同一打开句柄读取一次 bytes，之后各字号都从该内存快照建立字体，不按路径二次打开。每个非空白字符都会与该字体的 `.notdef` glyph 比较；无允许字体返回 `cover_font_unavailable`，字体存在但缺少任一字符返回 `cover_glyph_unsupported`。字体预检发生在 ffprobe、分段或封面 FFmpeg 命令之前，因此失败不会留下或发布同一计划的部分成品。无文字封面不需要字体，仍可正常抽帧。

本轮 Windows 本机已验证固定候选中存在可区分“中”“文”的中文 glyph；这只是该主机证据。当前 digest-pinned `python:3.12.13-slim-bookworm` candidate Dockerfile 没有安装、固定或验收任何字体，未知 tool image 中偶然存在的字体也不属于合同，因此当前 Linux/Docker 候选对非 ASCII 封面文字必须按 `cover_font_unavailable` 失败关闭。若后续要在容器内支持中文，应另行固定字体制品、来源、摘要、许可证与镜像验收，不能把 Windows 系统字体打包进去。不同 OS 字体版本也可能改变排版与 PNG 摘要，须按目标环境分别验收。

抽帧 PNG 与最终 PNG 均受 64 MiB 文件、4000 万像素和 64 MiB 解码预算限制；带方向旋转标记、动画或无法完整解码的图片会失败关闭。

## 4. 计划状态、失败、取消与重启

```mermaid
stateDiagram-v2
    [*] --> review: 建立计划
    review --> queued: 用户明确确认
    review --> canceled: 取消
    queued --> running: worker + claim token
    queued --> canceled: 取消
    running --> canceling: 请求取消
    running --> ready: 全部输出验证并登记
    running --> failed: 处理或验证失败
    canceling --> canceled: 子进程停止并清理
    failed --> review: 创建新的重试计划
    canceled --> review: 创建新的重试计划
```

取消和失败遵循以下边界：

- `review` 或 `queued` 取消后直接成为 `canceled`，不会生成 ready 输出。
- `running` 取消后先成为 `canceling`。取消事件会传给受控子进程；子进程停止后清理这次执行的 staging，并成为 `canceled`。
- 取消是协作式进程终止。收到取消响应不等于在同一瞬间已经删除所有临时字节，应等待计划进入终态。
- 已经 `ready` 的成品不会因再次调用取消而被撤销或删除。
- 一项 recipe 中任一分段、封面、ffprobe 或发布校验失败时，不发布该次执行的部分成品。
- `failed` 或 `canceled` 只能创建一个新的 `review` 重试计划；重试复制原计划的不可变 recipe，仍须再次明确确认。
- 应用在取得编辑根独占 lease 后执行重启恢复：`running` 和 `canceling` 会变为 `failed`，错误码为 `render_interrupted`；`queued` 会退回 `review`，清除旧确认并标记 `restart_confirmation_required`。恢复会删除 `sources/` 与 `assets/` 中“严格小写受管 ID + 精确小写允许后缀”但数据库未登记的崩溃残留，也会清理严格 `<plan_id>/<claim_token>` 且不属于活动 claim 的 staging，包括计划已进入终态但上次尚未完成删除的目录。case-only 文件名、未知名称、非普通文件、link/reparse 与其他不安全条目保持原样。不会在重启后静默重放。
- 停止应用后不再接受新服务操作。如果 worker 或已经进入服务层的 API 操作未能在等待时间内结束，独占 lease 会继续保留；最后一个活动方退出后才通过同一幂等路径交还，避免另一实例在文件已复制但尚未登记时执行恢复。
- claim token 不匹配的旧 worker 不能登记结果；这类结果以 `stale_render_claim` 拒绝。

常见失败含义：

| 错误码 | 含义 |
| --- | --- |
| `processor_not_configured` | 固定 FFmpeg/ffprobe 未达到 ready 且通过 offline smoke 的条件，计划保持待核对 |
| `source_hash_mismatch` / `source_changed` | 下载摘要不一致，或编辑副本在校验期间/之后发生变化 |
| `invalid_edit_recipe` | 时间、区间、封面或其他 recipe 参数无效，或超出源时长 |
| `capability_unavailable` / `ai_operation_blocked` | 请求了尚未接线或明确阻塞的 AI 能力 |
| `editing_storage_full` | 复制前检测到可用空间不足 |
| `editing_storage_unavailable` | 编辑目录或空间信息当前无法安全读取 |
| `media_output_too_large` | 预计或实际计划输出超过 8 GiB，或单项超过其固定 FFmpeg 上限 |
| `cover_font_unavailable` | 非 ASCII 封面文字没有可用的固定 OS 系统字体；不回退为方框或联网取字体 |
| `cover_glyph_unsupported` | 固定候选字体缺少标题或副标题中的至少一个字符；不删除或替换缺字 |
| `media_processing_failed` | FFmpeg、ffprobe、codec、时长、图片解码或输出校验失败 |
| `asset_changed` | 已登记编辑成品的大小或 SHA-256 已变化 |
| `render_interrupted` | 本地 running/canceling 计划在上次进程退出时未完成 |

错误响应只返回稳定代码，不返回本机路径或子进程 stderr。真实素材失败时先保留计划 ID、草稿版本和错误码；不要把一次 synthetic 通过解释为任意 container、codec 或损坏媒体都兼容。

## 5. Data root 与 Schema 1

若下载数据根为 `data`，编辑数据根默认为它的同级目录 `data-edits`。一般规则是给下载数据根的目录名追加 `-edits`；例如 `D:\OpenFlame\private-data` 对应 `D:\OpenFlame\private-data-edits`。

```text
data-edits/
├── editing.sqlite3
├── sources/                 # 导入后的不可变编辑源副本
├── assets/                  # 完成并验证的正式编辑成品
└── staging/
    └── <plan_id>/<claim_token>/
```

编辑根与下载数据库/媒体、下载 Cookie、上传 Schema 3、上传媒体及上传账号凭据相互隔离。上传导入会再次复制文件，不通过共享路径绕过两个域的校验。

Editing Schema 1 使用 SQLite `application_id=0x4F464544` 和 `user_version=1`。启动时要求表、索引和 trigger 与精确 DDL 一致，并执行 `quick_check` 与 foreign-key 检查；未知表、缺失索引、损坏、更高版本、替换竞态或不安全数据库文件都会拒绝打开，不会猜测迁移。

| 表 | 用途 |
| --- | --- |
| `metadata` | 唯一 Schema 版本行 |
| `sources` | 编辑源 ID、下载资产 ID、名称、后缀、大小、SHA-256 与创建时间 |
| `projects` | 项目与源的关系、当前草稿版本 |
| `drafts` | 按 `(project_id, version)` 保存不可变 recipe 与摘要 |
| `render_plans` | 冻结的草稿版本、recipe、状态、claim、确认/开始/完成时间及重试来源 |
| `assets` | ready 输出的种类、名称、大小、SHA-256、时长、尺寸、container 与 codec |
| `requests` | 幂等请求键、操作、请求摘要与结果 ID |

`drafts` 禁止 UPDATE/DELETE，render plan 的项目、草稿版本、recipe、摘要、创建时间和重试来源禁止修改。当前 0.27.0 没有编辑数据库备份 CLI、编辑成品删除/配额 UI 或旧 Schema 迁移；不要把下载/上传备份能力推定到编辑根。

## 6. 编辑 API

所有编辑 API 以 `/api/v1/edits` 开头。写请求必须来自同源页面并携带 `/session` 返回的 `X-Editing-CSRF`；middleware 同时核对 Host、Origin 和 `Sec-Fetch-Site`。响应设置 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` 与 `X-Frame-Options: DENY`。

| 方法与路径 | 当前行为 |
| --- | --- |
| `GET /session` | 取得本次进程内 CSRF token |
| `GET /capabilities` | 返回分段、封面、听写、翻译和配音的诚实能力状态；不安装或探测 AI 模型 |
| `GET /status` | 返回 Schema 版本、processor 是否配置、项目数与计划状态计数 |
| `GET /projects` | 列出编辑项目及当前草稿摘要 |
| `GET /projects/{project_id}` | 读取单个项目 |
| `POST /projects/assets/{download_asset_id}` | 显式复制 ready 下载视频并建立项目；body 含 `name`、`idempotency_key` |
| `GET /projects/{project_id}/draft` | 读取当前草稿 |
| `PUT /projects/{project_id}/draft` | 以 `expected_version` 追加草稿版本 |
| `POST /projects/{project_id}/plans` | 从指定当前版本建立 `review` 计划 |
| `GET /projects/{project_id}/source` | 同源预览经过复核的编辑源副本 |
| `GET /plans?project_id=...` | 列出全部或某项目的计划 |
| `GET /plans/{plan_id}` | 读取计划、冻结 recipe 和已登记输出 |
| `POST /plans/{plan_id}/confirm` | 明确确认并排队；processor 未配置时返回冲突且不排队 |
| `POST /plans/{plan_id}/cancel` | 取消 review/queued，或请求取消 running |
| `POST /plans/{plan_id}/retry` | 从 failed/canceled 建立新 review 计划；body 含 `idempotency_key` |
| `GET /assets?project_id=...` | 列出全部或某项目的正式成品 |
| `GET /assets/{asset_id}` | 读取成品 metadata |
| `GET /assets/{asset_id}/content` | 复核并读取成品；视频作为 attachment，封面 inline |

编辑域 ID 为 32 位小写十六进制；下载资产 ID 使用 canonical UUID。创建项目、保存草稿、创建计划和重试使用幂等键：只有相同键、相同操作和相同请求摘要才能安全重放；相同键对应不同内容时返回 `idempotency_conflict`。

编辑 JSON 不返回本机路径。内部 `resolve_output()` 只解析经过复核的 ready `segment`/`dubbed_video`，随后上传 API 的 `POST /api/v1/uploads/sources/edits/{output_id}` 才执行第二次复制。调用这个上传导入 API仍不会创建 upload job。

## 7. AI 时间轴与 provider 合同：已完成和未完成

### 已完成的合同

- `TimelineCue` 保存稳定 cue ID、顺序、整数毫秒起止时间、来源文字、来源语言与可选 speaker ID；provider 的听写、翻译和配音选项另以简化的 BCP-47 格式校验语言值。
- SRT/WebVTT parser 只接受 UTF-8，单文件上限 2 MiB，最多 10000 个 cue，单 cue 文本最多 8000 字符；序列化可保留 cue 顺序和时间。
- `TranslationRevision` 必须按原 cue ID 和顺序返回每段译文，不能丢段、换序或偷偷合并。
- 已定义 `TranscriptionProvider`、`TranslationProvider` 和 `SpeechProvider`，并统一 capability、进度与取消回调。
- `SpeechOptions` 当前允许 0.88～1.12 倍语速；`SpeechClip` 只接受 24 kHz、44.1 kHz 或 48 kHz及 1/2 声道的受管输出描述。
- recipe 能保存翻译的源/目标语言、provider/model，以及配音的语言、provider/model/voice、是否替换原声和审阅状态。

### 当前仍未完成

- 没有安装隔离 AI Python runtime、ASR/翻译/TTS 模型或模型 manifest。
- 没有生产 provider 实现，也没有将 provider 输出写入 Schema 1 或渲染为 `caption`、`audio`、`dubbed_video`。
- 页面中的 AI 参数仍禁用；`/capabilities` 将听写、翻译和配音标为 `blocked`，理由为 `ai_runtime_not_installed`。
- 当前 `MediaProcessor` 会拒绝任何启用的 translation/dubbing recipe，不会静默忽略或伪装完成。
- 没有云端凭据保存、费用预估、数据外发确认、远程 job reconciliation 或远端删除记录。
- 没有声音克隆；首批合同明确只考虑标准音色。

因此不能通过直接调用 API 把 AI state 改成 `ready` 来启用功能。状态字段是需要校验的工作流数据，不是绕过 runtime 和验收的开关。

## 8. 本地优先的下一步

建议先实现独立于核心 `.venv` 和上传 runtime 的 `ai-runtime`，并沿用项目现有的精确 manifest、全文件哈希、隔离进程、私有 staging、进度/取消和 fail-closed 边界。

1. **字幕优先。** 若下载成品已有受管 SRT/VTT，先导入并建立 timeline revision；不存在字幕或用户明确要求重识别时才运行 ASR。
2. **本地 ASR。** 第一候选为 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 与 [CTranslate2](https://github.com/OpenNMT/CTranslate2)。两者项目均采用 MIT License；faster-whisper 支持词级时间戳与 Silero VAD。先验收 Windows CPython 3.12 的 CPU int8；GPU 作为独立能力验收。
3. **本地翻译。** 第一候选为 [Meta M2M100 418M](https://huggingface.co/facebook/m2m100_418M)，模型卡标注 MIT，并由 [CTranslate2 Transformers 转换指南](https://github.com/OpenNMT/CTranslate2/blob/master/docs/guides/transformers.md)列为支持架构。首批只开放实际验收过的中文与 English 方向，并固定模型 revision、文件 SHA-256 和转换参数。
4. **本地 TTS。** 第一候选为 [Kokoro](https://github.com/hexgrad/kokoro)；仓库标注 Apache-2.0。中文另评估 [Kokoro-82M-v1.1-zh](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)。首批仅标准音色，每个 cue 单独输出 WAV，使用 ffprobe 测量时长；短于时间槽时补静音，超过时间槽时标记待修改，不截断或顺延后续 cue。
5. **审阅后渲染。** 翻译 revision、音色、语速、模型身份和每段时长都必须先可见；人工确认后才混音生成新的 `dubbed_video`。原下载视频与此前分段保持不可变。
6. **冻结能力。** 模型安装成功只能把状态推进到 `unverified`。许可证清单、哈希、离线 smoke、中文/英文样本、长短句、取消、重启和真人试听通过后，才能把精确 provider/model/environment 身份标为 `ready`。

## 9. 可选 OpenAI provider 的当前官方边界

以下是可选云 provider 的实现参考，不代表 0.27.0 已经接入或授权使用。启用前必须显示媒体、文字或音频会离开本机，并在服务端取得单独确认。

### 9.1 自动听写

- 文件听写使用 `POST /v1/audio/transcriptions`。OpenAI 当前[文件听写指南](https://developers.openai.com/api/docs/guides/speech-to-text)建议普通录音先用 `gpt-transcribe`；单文件最大 25 MB，接受 `mp3`、`mp4`、`mpeg`、`mpga`、`m4a`、`wav` 和 `webm`。
- 编辑时间轴需要词级时间戳时，官方当前只为 `whisper-1` 支持 `response_format=verbose_json` 与 `timestamp_granularities[]=word`。因此不能把 `gpt-transcribe` 的纯文本结果冒充精确 cue 时间轴。
- 需要说话人标签时可评估 `gpt-4o-transcribe-diarize` 和 `diarized_json`；返回 segment 的 `speaker`、`start` 与 `end`。超过 30 秒的音频需要 `chunking_strategy=auto` 或 VAD 配置。它提供的是 speaker segment，并不等同于 `whisper-1` 的词级时间戳。
- `POST /v1/audio/translations` 当前由 `whisper-1` 把语音翻译为 **English only**，不能作为中文及任意目标语言的通用翻译 API。

### 9.2 分段文字翻译

任意目标语言翻译应通过 Responses API 的文本模型完成，而不是使用 English-only 的 audio translations endpoint。请求应采用[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)，要求模型严格返回 `{segment_id, target_text}` 数组，并在本地再次验证 cue ID、数量和顺序。

模型应由 provider 配置保存并经过固定测试；不要把会自动更新的别名当作可重复构建证据。候选模型及当前可用性以[官方模型目录](https://developers.openai.com/api/docs/models)为准。翻译还需覆盖拒绝、不完整输出、token 上限、速率限制和部分请求失败，所有输出先保存为待审阅 revision。

### 9.3 AI 配音

- `POST /v1/audio/speech` 的当前首选模型是 [`gpt-4o-mini-tts`](https://developers.openai.com/api/docs/guides/text-to-speech)。它支持 `instructions` 控制口音、语速、语调和情绪等表达。
- [Create speech API reference](https://developers.openai.com/api/reference/cli/resources/audio/subresources/speech/methods/create)给出的单次输入上限为 4096 字符，输出可选 MP3、Opus、AAC、FLAC、WAV 或 PCM。工作台应逐 cue 请求 WAV/PCM，再自行测量并对齐时间槽。
- 官方列出 13 个内置音色，并说明音色目前主要针对 English 优化；中文必须经过单独真人试听，不能仅凭语言列表标为 ready。
- Audio Speech 返回音频内容或音频流，当前官方指南没有给出可依赖的词级时间戳输出。时间轴对齐仍由 Open-Flame 的分段、ffprobe 时长测量和 overflow 复核负责。
- OpenAI 使用政策要求向最终用户清楚披露听到的是 AI 生成声音。
- Custom Voice 仅向符合条件的客户开放，并要求 consent recording 与匹配的 sample recording。Open-Flame 首批继续禁用声音克隆。

### 9.4 密钥与隐私

- API key 只能进入后端隔离 runtime，通过环境变量或操作系统 secret manager 提供；不得写入浏览器、URL、编辑 recipe、SQLite、普通日志、备份或发行包。OpenAI 的[生产最佳实践](https://developers.openai.com/api/docs/guides/production-best-practices#api-keys)同样要求不要把 key 硬编码到代码或公开仓库。
- provider capability 应列出精确 data egress：听写上传音频；文本翻译上传 source cue 与上下文；TTS 上传译文、voice 和 instructions。可以用“本地 ASR + 云翻译/TTS”减少完整视频外发，但它仍会外发文字。
- 根据 OpenAI 当前[API 数据控制说明](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)，API 数据默认不用于训练，除非客户明确选择共享；不同 endpoint 的 abuse monitoring 与 application-state 保留不同，符合条件的客户可申请 Modified Abuse Monitoring 或 Zero Data Retention。实现时必须显示当时组织/项目的实际配置，不能仅凭 endpoint 名称宣称零保留。
- 文档当前列出 `/v1/audio/transcriptions` 与 `/v1/audio/translations` 无默认 abuse-monitoring/application-state 保留，`/v1/audio/speech` 有最长 30 天 abuse-monitoring 日志且无 application state；Responses API 的默认 application-state 行为另有 30 天边界。供应商政策可变化，启用和每次发行冻结时均需重新核对。
- 云请求尚未被服务端接受时可安全取消；一旦接受，应保存 provider request/job ID 并先 reconciliation，再决定重试，避免重复计费或生成重复音频。

本地 provider 应保持默认选项。任何云 provider 都必须在凭据存在、数据范围可见、费用边界可核对、用户明确确认且当前隐私说明复核后才进入 `queued`。

## 10. 当前可以准确声称的结果

0.27.0 当前源码建立了下载成品到独立编辑副本、版本化草稿、待核对计划、明确确认、本地分段与封面渲染、编辑成品以及显式导入上传的完整本地边界。仓库已有 synthetic 自动化和 Chromium 记录；真实媒体兼容性、AI runtime/模型、云 provider、真人试听、三平台真实投稿和最终发行制品仍须分别验收。
