# Open-Flame 当前开发交接

> 最后更新：2026-09-12（Australia/Adelaide）
> 本文件只保留当前源码身份、能力边界、风险与下一入口。逐轮结果见
> [`validation/`](validation/README.md) 中的独立证据；旧交接内容仍可从 Git 历史读取。

新开发任务可直接复制 [`docs/HANDOFF_PROMPT.md`](docs/HANDOFF_PROMPT.md)；模块、进程、
数据所有权和跨域一致性见 [`docs/CURRENT_ARCHITECTURE.md`](docs/CURRENT_ARCHITECTURE.md)。
提示词中的快照只用于定位，新任务仍须重新核对 HEAD、远端 main、Schema、CI 和签名。

当前任务在 `codex/architecture-reset-ci` 独立 worktree 进行，目标是按六类核心职责加 CLI
重整架构并解决完整 CI；**尚未完成，未合并 main**。起点为远端 main `d481326`。已分别
签名提交 Workflow 启动回滚、媒体主异常保留、adapter 控制记录解码、Worker/CLI 分责、
Upload receipt 纯合同和 Download 素材读取分责。字幕清理遗漏与发行文档分责的补充进展见
下方记录。Workflow 请求键与冻结投稿字段、Upload 封面规则也已统一；AI 重试图已归 Editing
统一校验。Workflow 重试结果与取消公共尾段也已收敛；后续仍须完成剩余规则收敛、
完整测试维护及四格 hosted CI 验证。
Workflow 前端纯校验已收敛，并修复空固定日期被预设保存成不定时的分歧。
备份复制的目标所有权、三个读取入口的 descriptor 交接及关闭失败保留主异常已修复，
公共备份文件操作现由两域共同使用独立 `backup_files` Module。
Editing 复制也已补齐目标创建权与失败清理，并与备份共用受管文件清理规则，见
[复制所有权记录](validation/iteration-0.28.0-editing-copy-ownership.md)。Upload 生命周期已移到
公开 UploadManager，由 HTTP、Workflow 与退出清理共用；启动回滚、恢复/关闭竞争及系统锁
交接均已补齐，见[生命周期记录](validation/iteration-0.28.0-upload-manager-ownership.md)。
Editing 成品元数据校验已前移，复制后立即登记清理责任，见
[成品登记清理](validation/iteration-0.28.0-editing-registration-cleanup.md)。备份锁 descriptor
与 SQLite 连接交接也已补齐，见[资源回收记录](validation/iteration-0.28.0-backup-resource-handoff.md)。
公共备份迁移、39 项故障反馈及当前测试入口阻断见
[公共备份文件记录](validation/iteration-0.28.0-public-backup-files.md)。render 重试树现也由
Editing 公开解析，Workflow 删除两个重复定义；一致读、取消写事务、后继与 unknown
保护见[render 图所有权记录](validation/iteration-0.28.0-editing-render-retry-ownership.md)。
Windows 上传源 reader 现持有至 backend 消费结束，Biliup 硬链接/复制暂存均核对冻结摘要；
执行后清理保留可信结果或 unknown，见[源文件交接](validation/iteration-0.28.0-upload-source-handoff.md)。
下一步完成跨页纯规则、CLI、当前文档和完整 CI 维护。
当前 AGENTS 禁改测试规则的例外已询问用户，未答复前只保留 ignored 测试迁移草案，不能
把其局部通过当成完整 CI。源码与验证入口见下方独立记录。

## 当前源码身份

| 项目 | 当前值 |
| --- | --- |
| 仓库 | [HedgehogsGX/Open-Flame](https://github.com/HedgehogsGX/Open-Flame) |
| 开发版本 | `0.28.0` |
| 数据库 | Download Schema 11；Editing Schema 4；Upload Schema 4；Workflow Schema 3；Workflow preset Schema 2 |
| 上传备份格式 | 3 |
| 首批上传平台 | Bilibili、抖音、视频号（内部 ID `tencent`） |
| 前端方向 | 方向 C“编辑式玻璃”，遵循 [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md) |
| Git 交付 | 当前任务签名提交到 `codex/architecture-reset-ci`，通过分支/PR 交付；不直接推送 main，禁止 force push |
| Git 身份 | `Cyaegha_Xu <85352261+novahanser@users.noreply.github.com>` |
| Upload Schema 4 源码提交 | `496fb63f9d0010c22fa1abc660e6450cd403b6be`；已签名直推 `origin/main`，GitHub author/committer 均归属 `novahanser` |

当前源码位于 0.28.0 发布后的持续开发链。`0592b6f` 的五件制品和包外 receipt
只证明其冻结构建；之后的提交尚未形成新的 clean release receipt。判断当前源码时以
`git rev-parse HEAD`、源码中的版本/Schema 常量和对应提交后的验证记录为准。

## 当前已接线能力

- 下载域接受既有受支持 URL/导入入口，经独立 Worker、受管资产与 manifest 进入本地
  control plane。各平台仍按 capability/evidence 判定，不因单样本或历史结果自动升级为
  `verified`。
- 下载域原有 yt-dlp 路径会把选中的平台 thumbnail 保存为 ready 资产的登记辅助文件；下载页
  以“来源封面（平台返回）”预览/下载，JPEG、PNG、WebP 可通过深链进入上传页。上传页只有在
  用户点击后才复核并复制到独立受管封面库，且不会自动选择封面、建立草稿或触发上传。
  当前命令仍只写一张 thumbnail；第二个 attempt-private `after_move` 控制记录会把所选图片的
  最终路径绑定到同一 original，并要求它等于目录中唯一归属该 original 的 thumbnail。固定四字段
  模板未显式序列化 thumbnail URL、headers、query 字段或完整候选列表；上游原始 identifier 的
  内容仅做有界校验后即丢弃，不进入 DTO、manifest、数据库或日志，也不形成 `origin_cover` 的
  持久证据。Candidate/Local Worker CLI 自行创建的默认 `SecureSubprocessRunner` 若未得到第二
  记录会失败关闭；注入式旧 runner 的空文件只能进入不带证明的兼容路径。
- Workflow 可显式启用 `upload.prefer_download_cover`，但仍必须保留一份生成封面作为 fallback。
  它只按 workflow 已冻结的 `download_asset_id` 查询同一 ready original 所属、登记关系完整且唯一的
  ready thumbnail；没有候选时使用生成封面，多候选或登记/路径身份异常时失败关闭。安全 resolver
  在候选分流前复核受管路径、MIME、大小并稳定读取实际字节核对登记 SHA-256；随后 Upload 的
  无副作用图像检查和 `import_cover(expected_sha256=...)` 会分别再读、解码并散列。来源封面只有
  同时兼容本次所选 Bilibili、抖音和视频号账号的
  全部封面槽才会采用，否则回退生成封面。Workflow 会先无副作用选定确定性受管 ID 并以 CAS
  写入 `upload_cover_id`，随后才允许 Upload 导入或建 jobs；已有请求则从不可变 jobs 的共同封面槽
  恢复，封面媒体已在终态后合法删除时仍可只凭元数据收回 job IDs。尚无请求的取消会先原子写入
  专用空 tombstone，重启不再依赖 Editing 成品或 Download 当前封面登记，并继续取消前序分段
  jobs；sentinel digest 或请求身份异常时失败关闭。
- 编辑域支持完整视频、1–10 个非破坏分段、封面取帧/文字、审核后渲染，以及可选的
  自动听写、中文与 English 翻译和标准音色配音。`segments: []` 表示完整视频；只做封面
  也会生成可上传的完整视频。自动流程可选择优先复用下载所得 SRT/WebVTT；候选会绑定原
  asset、重新校验文件身份与 SHA-256 并作为待审核 transcription timeline 导入。首次导入
  即使启用自动确认编辑也会先停下；生产 Workflow 页面会引导操作者在 Editing 页批准，
  服务/API 也只允许在首次停下后的单独显式确认中批准。不适用或被拒绝时才回退已授权的
  AI 听写。
- 配音 render 现在把逐 cue 完整 PCM、轨道格式、总大小与时间槽验证通过的 WAV 复制到 retry root 的临时 checkpoint，并用既有
  `requests` 表保存绑定请求定义与实际音频 SHA-256 的 manifest。只有同 project、draft、recipe、
  批准时间轴、authorization 与 request fingerprint 的无分叉显式重试后继可以复用；远程 cue 还
  必须有唯一 `responded` invocation。缺失、损坏或轨道条件不符的文件只重做对应 cue，`unknown` 和禁止重试的核对
  结论不能绕过。`ready` 提交后清理整条谱系缓存，启动恢复补清理提交后崩溃残留。没有新增表、
  Schema、服务、线程、队列、runtime、依赖或框架。
- Workflow 支持 URL → 下载 → 编辑 → 所选账号上传的持久化编排、恢复、取消和精确确认。
  保存一次完整预设后可只更换 URL；来源标题、绑定 asset 与逐账号最终标题在下载 ready
  时冻结，预设仅在账号、AI capability 和预设初始读取成功且操作者尚未修改表单时恢复。
  账号可保存“创建流程后 N 个整小时发布”；每次运行以固定锚点物化为绝对时间，响应丢失
  重试会复用相同锚点、幂等键和完整投稿参数。AI 重试恢复会先校验当前 editing project 的
  完整 retry forest、起始任务身份和父子请求摘要，再选择目标链 leaf；缺父节点、分叉、循环、
  跨项目或 immutable 字段漂移都会以 `ai_task_set_invalid` 停下。
- 上传域支持首批三平台逐账号标题、简介、标签、封面、原创/转载、分区、声明、动态文案、
  视频号短标题/内容标签、草稿模式和平台允许范围内的定时发布。最多选择 3 个账号；
  1–10 个输出最多形成 30 个上传 slot；重试后继必须保持账号、来源、平台及完整投稿参数
  不变，标题、标签、封面、模式、发布时间或平台选项漂移都会失败关闭。
- Upload Schema 4 在既有上传库中为每个已领取 job 保存唯一的本地 attempt receipt。receipt
  绑定 request/job 摘要、账号 session revision、来源与封面 SHA-256、当前 product build 和适配器
  身份；`reserved` 与 `queued → running` 原子提交，调用适配器前再持久标记
  `dispatch_may_have_started`，最终 receipt 与 job 同事务落库。自有适配器只有在平台、模式、
  结果与固定 evidence 完全匹配时才接受成功：Bilibili publish 为 `process_exit_zero`，抖音
  publish 为 `uploader_returned_after_final_action`，视频号 publish 为 `https_errcode_zero`，
  视频号 draft 为 `post_list_navigation`。这些是本地工具观察，不是平台签名回执、作品 ID、
  审核或公开可见证明。
- `unknown` 不再接受直接重试。操作者须先读取 receipt、到对应平台后台核对并勾选确认，再在
  固定结论中选择：`not_accepted` 转明确失败后才允许建立新草稿；publish 可记
  `submission_acknowledged`；视频号 draft 可记 `draft_saved`。revision CAS、当前 lineage 与
  完整身份会在写入前复核，同结论的响应丢失重放保持幂等。重启时未越过 dispatch 边界的
  `reserved` 任务安全失败为 `upload_dispatch_not_started`；可能已经调用平台的任务保持
  `unknown / interrupted_result_unknown`；当前格式 3 / Schema 4 的 running job 没有 receipt
  时保持 `unknown / attempt_receipt_missing`，不会伪造历史回执。旧格式 1/2 恢复保留其
  metadata 已声明的历史 interruption code，但同样没有 receipt。其他没有 receipt 的 job，包括
  `canceled / canceled`，同样不能 retry，须从重新核验的 source 手工建新草稿；可变 job
  状态不能证明任务从未越过 dispatch 边界。
- 上传备份格式 3 保存并语义审计 Schema 4 receipt；严格通过的旧格式 1 / Schema 2 与格式 2 /
  Schema 3 只在 staging 中迁移，旧备份本身不改写，也不为旧 job 补造 receipt。恢复仍不读取
  账号秘密、不构造 backend、不登录或上传。
- 完整 Workflow fan-out 出现 `upload_job_failed` 时可在流程页建立批量重试。Upload 域在单一
  `BEGIN IMMEDIATE` 事务中先按稳定 request key/digest、request-owned root、完整 retry lineage、账号 session、媒体、
  schedule 与当前平台合同核对全部 slot。Workflow 还会从冻结的投稿配置、账号绑定与已选封面
  重建每段完整 request；即使 Upload root 和重算 digest 彼此自洽，只要标题、简介、标签、封面、
  模式、发布时间或平台参数与 Workflow 冻结意图不符，也会在建后继前失败关闭。随后只为 attempt receipt 完整校验后具备资格的 failed/canceled leaf 创建新草稿；成功、平台草稿
  与 active slot 原位保留，任一 `unknown` 整批阻断。新草稿始终要求再次明确确认；同批另有原
  draft 时使用独立 mixed review code，避免把其确认原因伪装成重试。Workflow 先观察并 checkpoint
  当前 leaf，因此 Upload 已提交而 Workflow 尚未写回的窗口不会重复建后继。该切片没有新增表、
  Schema、服务、线程、队列、runtime、依赖或框架；这是此前 Workflow retry 切片的范围，当前
  Upload Schema 4 receipt 作为后续上传执行门禁单独记录。
- 本地 HTTP 写操作使用域独立 CSRF 与严格 loopback Host/Origin/Fetch-Site 防护；受管媒体
  使用同一文件身份、匹配打开、有界散列和不可变字节 snapshot 原语，同时保留
  AI render 的增强文件属性/规范路径校验及 Editing/Upload 各自错误与事务边界。
- 架构精简 S1–S8 已按小提交完成：HTTP guard、公开 profile/metadata/identity 契约、AI/
  Upload/Edit snapshot 解释、verified media response、EditingManager、受管文件原语，以及
  Workflow 上传表单与 recipe 的职责拆分。来源封面偏好继续维持本地模块化单体，只在现有
  Download repository/service、Workflow adapter 与 Upload 导入边界间增加窄 resolver；没有引入
  新服务、数据库、Schema、runtime、依赖、框架或通用调度基类。

## 当前证据入口

| 范围 | 当前记录 |
| --- | --- |
| 当前状态与 CI 策略 | [文档状态收敛](validation/iteration-0.28.0-document-status-consolidation.md) |
| 封面规则与当前回归 | [Upload 封面所有权及备份审计](validation/iteration-0.28.0-upload-cover-ownership.md) |
| AI 重试图所有权 | [Editing 统一校验与取消事务](validation/iteration-0.28.0-editing-ai-retry-ownership.md) |
| Workflow 重试与取消 | [共用结果应用和取消尾段](validation/iteration-0.28.0-workflow-retry-cancel-tails.md) |
| 备份文件所有权 | [复制目标与 descriptor 失败清理](validation/iteration-0.28.0-backup-file-ownership.md) |
| 公共备份文件操作 | [公开 Module、领域策略与测试迁移缺口](validation/iteration-0.28.0-public-backup-files.md) |
| Editing render 重试图 | [公开解析、一致读与取消保护](validation/iteration-0.28.0-editing-render-retry-ownership.md) |
| 表单校验与日期意图 | [Workflow 共享校验](validation/iteration-0.28.0-workflow-shared-validation.md) |
| Workflow 前端职责 | [recipe 分责](validation/iteration-0.28.0-workflow-recipe-functions.md)、[上传表单分责](validation/iteration-0.28.0-workflow-upload-form-functions.md) |
| 文件与 HTTP 边界 | [受管文件读取（2026-09-10 S6b 基线；2026-09-11 bounded-byte 增量）](validation/iteration-0.28.0-managed-file-read.md)、[下载 HTTP 防护](validation/iteration-0.28.0-download-http-boundary.md) |
| Workflow 编排 | [Edit snapshot](validation/iteration-0.28.0-edit-snapshot-observation.md)、[Upload snapshot](validation/iteration-0.28.0-upload-snapshot-observation.md)、[AI snapshot](validation/iteration-0.28.0-ai-snapshot-application.md)、[AI retry lineage](validation/iteration-0.28.0-workflow-ai-retry-lineage.md) |
| 纯数据契约 | [上传身份](validation/iteration-0.28.0-upload-identity-contract.md)、[上传重试完整投稿身份](validation/iteration-0.28.0-upload-retry-payload-identity.md)、[Workflow profile](validation/iteration-0.28.0-workflow-profile-contract.md)、[上传 metadata](validation/iteration-0.28.0-upload-metadata-contract.md) |
| 上传尝试与人工核对 | [Upload Schema 4 attempt receipt](validation/iteration-0.28.0-upload-attempt-receipts.md) |
| 当前系统结构 | [当前架构](docs/CURRENT_ARCHITECTURE.md)、[继续开发提示词](docs/HANDOFF_PROMPT.md) |
| 本轮补充修复 | [字幕清理](validation/iteration-0.28.0-caption-cleanup-errors.md)、[发行文档检查边界](validation/iteration-0.28.0-release-documentation-boundary.md) |
| Workflow 身份构造 | [请求键与冻结投稿字段](validation/iteration-0.28.0-workflow-request-construction.md) |
| 本轮架构重置 | [Worker/CLI 分责](validation/iteration-0.28.0-worker-entry-boundary.md)、[启动回滚](validation/iteration-0.28.0-workflow-start-rollback.md)、[媒体清理](validation/iteration-0.28.0-media-cleanup-errors.md)、[辅助素材清理](validation/iteration-0.28.0-auxiliary-cleanup-errors.md)、[adapter 解码](validation/iteration-0.28.0-adapter-control-decoding.md)、[回执状态合同](validation/iteration-0.28.0-upload-receipt-contract.md)、[Download 素材读取](validation/iteration-0.28.0-download-asset-reader.md) |
| 用户功能 | [来源标题与网址即运行](validation/iteration-0.28.0-workflow-source-title.md)、[来源字幕优先复用](validation/iteration-0.28.0-workflow-source-caption-reuse.md)、[Workflow 来源封面偏好](validation/iteration-0.28.0-workflow-source-cover-preference.md)、[Workflow 投稿重试](validation/iteration-0.28.0-workflow-upload-retry.md)、[配音断点重试](validation/iteration-0.28.0-speech-checkpoint-retry.md)、[相对发布时间预设](validation/iteration-0.28.0-workflow-relative-schedules.md)、[无 AI 完整视频](validation/iteration-0.28.0-no-ai-full-video.md)、[编辑式玻璃前端](validation/iteration-0.28.0-editorial-glass-frontend.md)、[来源封面调研与导入](validation/iteration-0.28.0-source-cover-research-and-import.md) |
| 当前本机 runtime | [应用根与 runtime 刷新](validation/iteration-0.28.0-local-runtime-refresh.md) |
| CI | [托管 CI 执行链恢复](validation/iteration-0.28.0-hosted-ci-recovery.md) |

这些记录分别绑定各自源码和环境。它们不能相互拼接成当前 release receipt，也不能把
synthetic/offline/browser 结果解释成真实模型质量或平台接收。

## 未完成与风险

1. **真实 OpenAI 未验收。** 当前应用根的 AI runtime 完整性已验证，但没有 API key，
   provider health 保持 `provider_health_required`。真实响应、费用、译文质量、音画同步和
   真人试听仍未知。
2. **真实三平台发布未验收。** 本地账号显示 ready 只说明本地 session 记录状态；不能证明
   远端 session 仍有效，也不能证明上传、审核、定时发布或公开可见成功。
3. **当前源码尚未形成新的 release candidate。** Upload Schema 4 源码里程碑已冻结并签名
   推送为 `496fb63f9d0010c22fa1abc660e6450cd403b6be`，但 `0592b6f` 之后的功能与架构提交仍需
   新的 clean candidate、源码/wheel 独立安装和包外 receipt，才能形成新的发布结论。
   2026-09-10 的当前应用根记录只验证上传库由 Schema 1 迁移到 Schema 3；本轮没有在该实际
   应用根执行 Schema 4 迁移或审计，临时 Schema 4 浏览器 smoke 不能替代它。
4. **完整 CI 尚未恢复。** 标准 pytest 仍被备份旧私有 helper import 阻断：1 collection error，
   22 个 Download backup 用例未收集。源交接修复前的完整诊断为 2144 passed、276 failed、
   16 skipped、1 error；新增 26 个 failure IDs 来自 SHA 请求 fixture/payload 未同步。
   当前修复后的聚焦结果与 ignored fixture 副本见[源交接验证](validation/iteration-0.28.0-upload-source-handoff.md)，
   不能替代完整标准测试。其余失败仍须逐类判定，不能全部视为旧测试。
   abcbc40 hosted run 34692830186 四格均在 offline pytest 失败，不含本轮上传源交接切片。
   用户尚未批准测试维护例外；不能删除断言、排除测试或放宽生产合同来声称绿色。
   备份迁移验证入口见[公共备份记录](validation/iteration-0.28.0-public-backup-files.md)。
5. **目标 Linux/Docker 未验收。** Windows 本地与 synthetic 结果不关闭 T15 的 namespace、
   ACL、mount、AF_UNIX、恢复和第三方 runtime 分发边界。
6. **真实下载能力仍按样本证据限定。** 历史少量 YouTube/X/Instagram 成功、Bilibili 412、
   Douyin `authentication_required` 和未执行平台都不是平台级支持或否定结论。固定 yt-dlp
   的 Bilibili 提取器可读取 `videoData.pic`，Douyin 提取器可列出 `cover`、`origin_cover`
   等变体；当前私有控制记录可核对 yt-dlp 实际选中的单图路径，但不保证得到 `origin_cover`，
   不保留变体身份证明，且两平台真实封面提取及 Workflow 自动采用均未验收。视频号没有专用
   yt-dlp extractor，本轮没有增加视频号网址封面能力。
   “来源封面（平台返回）”也不代表发布者原始母版、最高分辨率或无损文件。
7. **来源字幕内容仍需核对。** ready caption 与 `origin=platform` 只证明登记关系和文件完整性；
   不能区分人工字幕与平台自动字幕，也不能证明语言、文字或时间轴准确。没有合适 SRT/VTT
   时会回退 AI 听写，因此 transcribe capability、授权、外发范围与费用仍须在流程创建前冻结。
8. **架构重置仍在推进。** 本分支已关闭线程首次启动回滚与媒体失败清理两条本地 P2 路径，
   12 个有界检查通过；这不能证明其他候选已实施，也不能覆盖原有上传 source TOCTOU 窗口。
   Download 素材读取已分责；领域规则 Locality 与完整 CI 的后续工作见
   [`docs/CURRENT_ARCHITECTURE.md`](docs/CURRENT_ARCHITECTURE.md#9-当前缺陷复杂度集中点与下一切片)。

## 下一入口

1. 继续当前架构重置目标，处理上传源文件交接、跨页纯规则、CLI 支持与最终发行
   文档。同时取得测试维护规则的明确决定并逐类关闭完整 CI；主目标
   不能缩成局部通过或文档审查。
   临时故障注入仍放在已忽略的 `validation/local/`，未得到例外前不改 tracked tests。
2. 外部测试人员从 [`TESTING.md`](TESTING.md) 开始，按
   [`docs/DEBUG_GUIDE.md`](docs/DEBUG_GUIDE.md) 定位问题，并用
   [`docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md`](docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)
   返回环境、精确 commit、步骤、脱敏日志与结果。不要提交 Cookie、token、URL 样本、媒体、
   本机路径或 `validation/local/` 内容。
3. 收到外部反馈后，先复现并修复本地可证明的问题；每个关键修复独立签名提交到当前开发
   分支，通过分支/PR 交付。首批上传范围保持 Bilibili、抖音、视频号。
   对 `unknown` 上传先保存数据库与日志、读取本次 receipt 并在对应平台后台核对；只有固定
   `not_accepted` 结论落库后才能建立 retry 草稿，不得使用旧请求体或手工改库直接重传。
4. 真实 OpenAI、真人试听、扫码/登录、上传、定时发布和公开可见性验证必须在该具体动作已
   明确授权、凭据留在 Git 外且精确候选固定后进行。每个结果按平台、来源类型、适配器版本、
   环境和 commit 单独记录。来源封面还需分别用 Bilibili 与 Douyin 的已授权样本对照平台可见
   封面、下载 artifact、Workflow 最终选用的受管封面和三平台后台实际封面；同时覆盖无候选、
   比例不兼容与多分段/重启。视频号保持无专用下载 extractor，不能用通用 extractor 结果替代
   专用证据。
   若样本返回字幕，还要分别记录语言、格式、人工/自动来源是否可知、导入 timeline、审核修订
   与是否触发 AI transcription fallback；一个平台的字幕结果不能代表其他平台。
5. 功能反馈收敛后再创建 clean release candidate，执行源码与 wheel 独立安装、完整检查、
   隐私/许可证扫描和包外 receipt；历史 receipt 不覆盖新候选。

## 开发约束

- 继续工作前阅读 [`AGENTS.md`](AGENTS.md)、
  [`docs/FOLLOW_UP_EXECUTION_PLAN.md`](docs/FOLLOW_UP_EXECUTION_PLAN.md) 和改动范围对应的
  最新 evidence。
- 后续提交不得新增或修改自动化测试文件。现有 `tests/` 只用于本地/CI 回归；临时探针、
  日志、截图和结果放在已忽略的 `validation/local/`。提交前运行
  `scripts/verify_commit_scope.py --staged`；仓库 hook 与 hosted CI 共用该分类器。
- 生产页面沿用编辑式玻璃 token、组件、焦点、减少动态、窄屏和文字缩放规则。轮询不得覆盖
  输入、账号选择、焦点、文本选区、二维码、详情展开或明确确认状态。
- 该 Codex packaged 开发宿主存在 Windows filesystem virtualization。Setup 与 Start 必须使用
  同一个人工核对后的 canonical `--app-root`；不得放宽 alias/reparse/plain-file 检查。
- 不提交本机数据库、凭据、Cookie、runtime、媒体或原始平台证据。不得 force push，不得把
  ignored 本地验证结果或合成数据描述成真实平台验收。

完整历史证据索引与 Stage 0 规则见 [`validation/README.md`](validation/README.md)。

## 建议使用的本地 skills

按任务需要从当前宿主提供的 Skills catalog 解析同名 `SKILL.md`，不要把某台机器的绝对路径写入
仓库。skill 的建议不能覆盖用户当前请求或本仓库 `AGENTS.md`。

| Skill | 适用范围 |
| --- | --- |
| `diagnose` | 两个已复现 P2、外部缺陷与性能回归的最小复现和修复 |
| `improve-codebase-architecture` | 按领域语言、当前重复和 deletion test 继续收敛架构 |
| `review` | 固定提交后的 Standards/Spec 双轴审查 |
| `handoff` | 继续更新精简交接，并引用已有证据而非复制算法 |
| `emil-design-eng` | Editorial Glass 生产页面的组件与动效细节 |
