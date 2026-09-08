# Iteration 0.28.0 AI 与自动流程工程证据

日期：2026-09-08
状态：冻结候选记录；源码、隔离 runtime、本地状态机、媒体链路与浏览器界面已复验。最终 clean commit 与五件发行制品由包外 `release-receipt.json` 绑定，避免本文件形成摘要自引用

## 1. 范围与结论边界

本轮源码接入独立 Editing Schema 3、隔离 AI runtime builder、标准库 OpenAI provider、持久化听写/翻译任务与可审核时间轴、segment-local 字幕/标准音色配音，以及独立 Workflow Schema 1。`/workflows` 可以把一个 URL 串接到下载、编辑、AI 和所选 Bilibili、抖音、视频号上传草稿，并在保存的预授权范围内推进。

本轮没有提供 `OPEN_FLAME_AI_OPENAI_API_KEY`。没有执行真实 OpenAI 听写、Responses 翻译或 Speech 配音请求，也没有执行 Bilibili、抖音、视频号的真实登录、媒体传输、定时发布、审核或公开发布。所有 workflow/provider 替身、synthetic 视频、本地 FFmpeg 和 runtime 完整性检查都只证明对应源码与本机工程合同，不代表云模型质量、账号权限、费用、平台接受或最终发布。

## 2. 已实现的安全与一致性边界

- AI runtime 与核心 `.venv`、下载数据、编辑数据库和上传 runtime 分离。builder 只接受尚不存在的绝对输出目录，在私有 staging 内安全解压并冻结 CPython、worker、协议、provider、模型声明和全文件摘要；运行前重新核对完整集合与身份。
- 远程数据外发合同集中为 `health=none`、`transcribe=audio`、`translate=text`、`synthesize=text`。建立/确认任务与每次真实执行前都会先验证 provider 声明；HTTP worker 复用同一映射，校验发生在凭据注入与网络调用之前。
- 听写前使用固定 FFmpeg 派生 mono、24 kHz、32 kbit/s AAC M4A。自动流程只选择 recipe 的第一个分段，发送该片段并把 provider 返回时间换算回源时间轴；编辑页直接建听写任务当前仍默认完整编辑源。
- 时间轴最多 10000 个 cue，单 cue 统一上限 4096 字符，持久化时间轴 JSON 上限 3 MiB，为隔离 runtime 的 4 MiB request envelope 留出元数据与 glossary 空间。
- 翻译保持 cue ID、数量、顺序、整数毫秒时间和来源关系；听写/翻译结果先停在 `review`，整份批准后才能进入下一步。render plan 冻结批准译文、父字幕和两份摘要。
- AI 渲染按每个已选分段过滤 cue 并把时间归零，分别生成 segment-local VTT 和配音视频。分段边界切入 cue 时以 `ai_segment_boundary_splits_cue` 拒绝；没有 cue 的 B-roll 段生成空 VTT 与本地静音，不调用 Speech API。
- 每个 cue 的 Speech WAV 单独校验并对齐时间槽；溢出以 `ai_speech_timing_overflow` 失败，不截断文字或推迟后续 cue。保留原声时原声固定为 22%，`amix` 禁止自动归一化并在输出前限幅。
- AI task、编辑计划和上传 retry/restart 均须重新确认。当前不保存可对账的远程 request/job ID，也不复用已完成的远程批次或 cue；因此页面明确提示重试可能重复计费。
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
| 生产 Python 内存 AST 解析 | PASS | Python 3.12.13；`src/**/*.py` 共 111 个，全部 `ast.parse` 通过；未用 `compileall` 写入缓存 |
| 四页 inline JavaScript `node --check` | PASS | 共提取 8 段；Node v24.16.0，全部通过 |
| `git diff --check` | PASS | exit 0；仅报告 5 个既有工作树 CRLF→LF 提示 |
| `uv lock --check --offline` / `uv pip check` | PASS | 锁文件离线解析 25 个包；已安装 24 个包均兼容 |
| 全量既有回归 `pytest -q` | QUALIFIED | 2438 passed、8 skipped、12 failed，413.44 秒；12 项均是下述历史固定断言，不是当前实现异常 |
| Workflow synthetic 状态机 | PASS | URL→下载→编辑→上传、幂等、编辑/上传重启重新确认、AI 外发同意、账号 session revision 失效均通过 |
| 本地真实 FFmpeg clip/dub smoke | PASS | 选择片段、源时间回映、segment-local VTT/音轨、22% 混音与源文件不变均通过 |
| AI operation egress 合同 | PASS | 错误声明在任务校验和 worker 网络调用前失败；HTTP TTS 只声明 `text` 时可执行请求构造 |
| AI runtime rebuild + `--check` | PASS | manifest `598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378`；`ready=true`；应用显示 `integrity=verified / provider_health=unverified`，三能力均 `blocked / ai_provider_auth_missing` |
| Chromium 浏览器 QA | PASS | `/workflows` 与 `/edits` 连接成功；四页导航、0.28.0、Schema 1、缺凭据提示、22% 混音说明及 `audio / text / text` 外发范围可见 |
| HTTP route logging | PASS | 当前应用 102 个实际路由全部属于固定 allowlist；TestClient 17 个页面/API 请求与浏览器进程 263 个请求均被接受，`runtime_log.event_rejected=0` |
| release-files 清单 | PASS | 242 个唯一且存在的条目；无 `tests/`；AI、Workflow 与本记录均已列入 |
| source ZIP / sdist / wheel 与独立安装 | 包外记录 | 提交后对 clean commit 构建并用 verifier 复验；文件身份、报告和结果写入 `release-receipt.json` |
| 真实 OpenAI / 真人试听 / 费用 | NOT RUN | 需另行授权的真实账号与样本证据 |
| 三平台真实投稿与发布 | NOT RUN | 每个平台后台结果与同一冻结构建身份 |

全量回归中的 12 项历史固定断言为：5 项仍要求产品版本 `0.27.0`；3 项仍要求 Editing Schema 1；3 项只允许旧的下载/编辑/上传三项导航；1 项仍要求 sdist exclude 列表中没有 `/tests/**`。当前合同分别是 0.28.0、Editing Schema 3、增加 `/workflows` 和所有发行格式排除 `tests/`。本轮遵循用户要求，没有修改测试文件，也没有为旧断言回退产品。

## 5. 最终冻结与包外绑定

- source commit 与 clean working tree：由包外 `release-receipt.json` 记录
- 当前源码匹配 AI runtime manifest SHA-256：`598ad64ecf005daa7ed2f9007280dc560212d98daa633f5afa1fac2703260378`
- 全量回归结果：2438 passed、8 skipped、12 个明确列出的旧合同断言失败
- synthetic Workflow、本地媒体与 operation egress 脚本：全部 PASS
- source ZIP / sdist / wheel 文件名、大小与 SHA-256：由包外 `release-receipt.json` 记录
- 源码 ZIP 与 wheel 独立安装结果：由包外 `release-receipt.json` 记录
- `scripts/verify_commit_scope.py --staged` 与 hook 结果：由提交记录及包外 `release-receipt.json` 记录

最终 receipt 必须位于 release 目录之外，直接绑定 clean commit、完整 product identity、五件制品和实际验收结果；不要把最终制品摘要写回本文件形成自引用。
