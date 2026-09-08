# Iteration 0.28.0 AI 与自动流程工程证据

日期：2026-09-08；追加复验：2026-09-09
状态：冻结候选与发布后更正记录；源码、隔离 runtime、本地状态机、媒体链路与浏览器界面已复验。最终 clean commit 与五件发行制品由包外 `release-receipt.json` 绑定，避免本文件形成摘要自引用

## 1. 范围与结论边界

T19 冻结源码接入独立 Editing Schema 3、隔离 AI runtime builder、标准库 OpenAI provider、持久化听写/翻译任务与可审核时间轴、segment-local 字幕/标准音色配音，以及独立 Workflow Schema 1；发布后源码已向前迁移至 Editing Schema 4，保存脱敏远程调用账本与 unknown 人工 reconciliation。`/workflows` 可以把一个 URL 串接到下载、编辑、AI 和所选 Bilibili、抖音、视频号上传草稿，并在保存的预授权范围内推进。

本轮没有提供 `OPEN_FLAME_AI_OPENAI_API_KEY`。没有执行真实 OpenAI 听写、Responses 翻译或 Speech 配音请求，也没有执行 Bilibili、抖音、视频号的真实登录、媒体传输、定时发布、审核或公开发布。所有 workflow/provider 替身、synthetic 视频、本地 FFmpeg 和 runtime 完整性检查都只证明对应源码与本机工程合同，不代表云模型质量、账号权限、费用、平台接受或最终发布。

## 2. 已实现的安全与一致性边界

- AI runtime 与核心 `.venv`、下载数据、编辑数据库和上传 runtime 分离。builder 只接受尚不存在的绝对输出目录，在私有 staging 内安全解压并冻结 CPython、worker、协议、provider、模型声明和全文件摘要；运行前重新核对完整集合与身份。
- 普通源码 Setup 可选接受本地 CPython 3.13.15 embeddable ZIP，目标缺失时内部固定核验 SHA-256，并只向默认或显式 `<app-root>/data-ai-runtime` 原子构建。完整且匹配当前源码的 runtime 只读复用，复用时不读取 ZIP，也不把 runtime 自身当作原始归档来源证明；坏/旧目标不删除、不覆盖、不改 manifest。安装与普通 Start 共用 `.local-app.lock`，运行中用既有 `setup_busy` 停止，其他失败收敛为 `setup_ai_runtime_failed`；child 输出、路径和异常原文不转发。
- 同一源码 Setup 可选接受 `--upload-runtime`，从同一默认或显式 `<app-root>` 精确派生 `data-uploads`，并在 `.venv` 不是 CPython 3.12 x64 时接受 build-only `--upload-python ABSOLUTE_EXE`。它复用既有 `uploads.runtime_setup`，不新增安装服务；锁顺序为 source→app-root→upload runtime。ready runtime 先完整复核且不校验无关的 build Python；旧 Schema 1、损坏和含非允许内容的部分 runtime 原样失败关闭，空目录或只含允许且散列匹配的固定归档缓存可续建。该上传 builder 直接构建而非原子 staging；固定失败码和 child 输出脱敏不把工程检查解释成登录或发布。
- 远程数据外发合同集中为 `health=none`、`transcribe=audio`、`translate=text`、`synthesize=text`。建立/确认任务与每次真实执行前都会先验证 provider 声明；HTTP worker 复用同一映射，校验发生在凭据注入与网络调用之前。
- 听写前使用固定 FFmpeg 派生 mono、24 kHz、32 kbit/s AAC M4A。自动流程只选择 recipe 的第一个分段，发送该片段并把 provider 返回时间换算回源时间轴；编辑页直接建听写任务当前仍默认完整编辑源。
- 时间轴最多 10000 个 cue，单 cue 统一上限 4096 字符，持久化时间轴 JSON 上限 3 MiB，为隔离 runtime 的 4 MiB request envelope 留出元数据与 glossary 空间。
- 翻译保持 cue ID、数量、顺序、整数毫秒时间和来源关系；听写/翻译结果先停在 `review`，整份批准后才能进入下一步。render plan 冻结批准译文、父字幕和两份摘要。
- AI 渲染按每个已选分段过滤 cue 并把时间归零，分别生成 segment-local VTT 和配音视频。分段边界切入 cue 时以 `ai_segment_boundary_splits_cue` 拒绝；没有 cue 的 B-roll 段生成空 VTT 与本地静音，不调用 Speech API。
- 每个 cue 的 Speech WAV 单独校验并对齐时间槽；溢出以 `ai_speech_timing_overflow` 失败，不截断文字或推迟后续 cue。保留原声时原声固定为 22%，`amix` 禁止自动归一化并在输出前限幅。
- AI task、编辑计划和上传 retry/restart 均须重新确认。当前不保存可对账的远程 request/job ID，也不复用已完成的远程批次或 cue；因此页面明确提示重试可能重复计费。
- 普通 Windows Start 只让 control child 继承精确的 `OPEN_FLAME_AI_OPENAI_API_KEY`，并在进程边界先执行与 `AiTaskExecutor` 一致的非空、字符和长度校验。下载 Worker 仍剔除该密钥，未声明 AI 变量和通用 token/secret 也不进入任一 child。
- Workflow 建立时冻结每个上传账号的 `{account_id, platform, session_revision}`。待确认草稿若经历重新登录会以 `account_session_changed` 停止；已进入 queued/running/terminal 的任务不会因后来登录被改写。
- 上传对账沿每个任务的唯一 retry leaf 继续，只接受账号、来源和平台不变的最多 32 代后继链；分叉、循环、重复 leaf 或身份漂移失败关闭。leaf 为 draft 或因重启退回 draft 时仍须再次确认。批量确认在同一事务内先验证全部待确认任务，再一次性排队。
- 仓库现有 `tests/` 只供本地与 CI；源码 ZIP、sdist 和 wheel 均排除测试。0.28.0 功能提交不得新增或修改测试文件，临时脚本/日志/结果只放在 ignored `validation/local/`。

## 3. AI runtime 候选身份

| 项目 | 候选值 | 解释 |
| --- | --- | --- |
| CPython 归档 | `python-3.13.15-embed-amd64.zip` | Python.org Windows x64 embeddable ZIP |
| 归档大小 | `11009825` bytes | 本机上一候选读取值 |
| 归档 SHA-256 | `d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf` | 与 Python.org 3.13.15 发布材料核对的固定值 |
| 当前源码匹配 runtime manifest SHA-256 | `598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378` | 由 CPython 3.13.15 ZIP 和当前 worker/protocol/provider 源码重建；三份复制文件已逐字节 SHA-256 复核 |
| Provider 凭据 | 未配置 | `OPEN_FLAME_AI_OPENAI_API_KEY` 不存在；三项云能力应以 `ai_provider_auth_missing` blocked |

当前候选 runtime 的 builder 与 `--check` 返回 `ready=true`。应用状态将其解释为 `integrity=verified / provider_health=unverified`；三项 OpenAI 能力均为 `blocked / ai_provider_auth_missing`，并分别显示实际外发范围 `audio / text / text`。由于 runtime 会复制当前 worker/protocol/provider 字节，之后任何这些源码变化都会改变 manifest；最终 receipt 必须继续绑定本摘要与冻结提交，不能只凭本表断言最终发行身份。

## 4. 当前验证矩阵

下表均针对本轮当前源码。提交和制品自身不能安全写入被打包文件，因此对应身份在构建后写入包外 receipt。

| 检查 | 当前记录 | 最终需记录的最小证据 |
| --- | --- | --- |
| 生产 Python 内存 AST 解析 | PASS | `src/**/*.py` 共 115 个，全部 `ast.parse` 通过；未用 `compileall` 写入缓存 |
| 四页 inline JavaScript `node --check` | PASS | 共提取 8 段；Node v24.16.0，全部通过 |
| `git diff --check` | PASS | exit 0；工作树相关文档已按仓库规则统一为 LF |
| `uv lock --check --offline` / `uv pip check` | PASS | 锁文件离线解析 25 个包；已安装 24 个包均兼容 |
| 全量既有回归 `pytest -q` | QUALIFIED | 2438 passed、8 skipped、12 failed，413.44 秒；12 项均是下述历史固定断言，不是当前实现异常 |
| Workflow synthetic 状态机 | PASS | URL→下载→编辑→上传、幂等、编辑/上传重启重新确认、AI 外发同意、账号 session revision 失效均通过 |
| 本地真实 FFmpeg clip/dub smoke | PASS | 选择片段、源时间回映、segment-local VTT/音轨、22% 混音与源文件不变均通过 |
| AI operation egress 合同 | PASS | 错误声明在任务校验和 worker 网络调用前失败；HTTP TTS 只声明 `text` 时可执行请求构造 |
| AI runtime rebuild + `--check` | PASS | manifest `598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378`；`ready=true`；应用显示 `integrity=verified / provider_health=unverified`，三能力均 `blocked / ai_provider_auth_missing` |
| 普通源码 Setup 安装 AI runtime | PASS | ignored validator 在显式临时 app-root 禁网验证首次构建、完整当前 runtime 原样复用、坏/旧/SHA 不符拒绝、运行锁 busy、runner exception 固定诊断及路径不回显；默认 `data-ai-runtime` 的 `lexists` 实测前后均为 false |
| 普通源码 Setup 安装上传 runtime | PASS | ignored validator 在显式临时 app-root 禁网验证精确 `data-uploads` 派生、默认/覆盖构建解释器、ready 复用、source/app/runtime 三层锁、固定失败码、旧/坏/不安全部分目录不变及路径不回显；默认实际 Schema 1 runtime 前后不变 |
| 普通 Start 的 AI 密钥边界 | PASS | ignored validator 从已核对的 CPython ZIP 本地重建 runtime；sentinel 只进入 control，Worker/未声明变量仍剔除；三能力由 `blocked / ai_provider_auth_missing` 变为 `unverified / provider_health_required`；网络入口强制拒绝，输出不含 sentinel |
| Chromium 浏览器 QA | PASS | `/workflows` 与 `/edits` 连接成功；四页导航、0.28.0、Schema 1、缺凭据提示、22% 混音说明及 `audio / text / text` 外发范围可见 |
| HTTP route logging | PASS | 当前应用 102 个实际路由全部属于固定 allowlist；TestClient 17 个页面/API 请求与浏览器进程 263 个请求均被接受，`runtime_log.event_rejected=0` |
| release-files 清单 | PASS | 251 个唯一且存在的条目；无 `tests/`；AI、Workflow 与本记录均已列入 |
| source ZIP / sdist / wheel 与独立安装 | 包外记录 | 提交后对 clean commit 构建并用 verifier 复验；文件身份、报告和结果写入 `release-receipt.json` |
| 真实 OpenAI / 真人试听 / 费用 | NOT RUN | 需另行授权的真实账号与样本证据 |
| 三平台真实投稿与发布 | NOT RUN | 每个平台后台结果与同一冻结构建身份 |

2026-09-09 追加定向复验：`validation/local/validate_local_app_ai_secret_boundary.py` PASS；`tests/test_local_app.py`、`tests/test_desktop_launcher.py` 与 `tests/test_editing_ai_contracts.py` 合计 **113 passed**；`py_compile` PASS；`uv lock --check --offline` 与 `uv pip check` PASS。没有修改测试文件，没有远程请求。

2026-09-09 Setup 追加定向复验：`validation/local/validate_source_setup_ai_runtime.py` PASS，覆盖首次构建、ready 字节与 mtime 不变复用、坏/旧目标拒绝、固定 SHA-256 负向、同 app-root 运行锁、runner exception、孤立 `--app-root`、非目标不变和输出脱敏；build/check 子解释器的常见 socket/HTTP 入口强制拒绝，没有调用 provider。14 个既有 Setup、source lock、release、local app、launcher、诊断与 AI 合同测试文件合计 **354 passed**；相关生产模块 `py_compile`、`uv lock --check --offline` 与 `uv pip check` 均通过。验证只使用 ignored 临时 app-root；默认应用 runtime 实测仍不存在。

2026-09-09 上传 Setup 追加定向复验：`validation/local/validate_source_setup_upload_runtime.py` 的 16 项检查 PASS；child probe 强制拒绝 socket/HTTP，未下载第三方组件，未登录、扫码、上传或发布。AI Setup validator 同批复跑 PASS；15 个 Setup、进程树、诊断、source/app 锁、上传 runtime/后端、本地启动和发行清单相关既有测试文件合计 **462 passed**。validator 只写 ignored 临时 app-root，并对默认实际 `data-uploads` 的顶层集合、上传库和 runtime manifest 做前后摘要核对；现有 runtime 仍为 `runtime_upgrade_required`，没有移动、重建或改写。115 个生产 Python 文件 AST、251 条唯一存在且排除 `tests/` 的 release-files、`uv lock --check --offline`、`uv pip check` 与 diffcheck 均通过；源码实现复用已有上传 installer，未新增核心依赖或常驻服务。

全量回归中的 12 项历史固定断言为：5 项仍要求产品版本 `0.27.0`；3 项仍要求 Editing Schema 1；3 项只允许旧的下载/编辑/上传三项导航；1 项仍要求 sdist exclude 列表中没有 `/tests/**`。当前合同分别是 0.28.0、Editing Schema 4、增加 `/workflows` 和所有发行格式排除 `tests/`。本轮遵循用户要求，没有修改测试文件，也没有为旧断言回退产品。

## 5. 最终冻结与包外绑定

- source commit 与 clean working tree：由包外 `release-receipt.json` 记录
- 当前源码匹配 AI runtime manifest SHA-256：`598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378`
- 全量回归结果：2438 passed、8 skipped、12 个明确列出的旧合同断言失败
- synthetic Workflow、本地媒体与 operation egress 脚本：全部 PASS
- source ZIP / sdist / wheel 文件名、大小与 SHA-256：由包外 `release-receipt.json` 记录
- 源码 ZIP 与 wheel 独立安装结果：由包外 `release-receipt.json` 记录
- `scripts/verify_commit_scope.py --staged` 与 hook 结果：由提交记录及包外 `release-receipt.json` 记录

最终 receipt 必须位于 release 目录之外，直接绑定 clean commit、完整 product identity、五件制品和实际验收结果；不要把最终制品摘要写回本文件形成自引用。
