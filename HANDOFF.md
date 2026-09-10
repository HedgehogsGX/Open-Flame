# Open-Flame 当前开发交接

> 最后更新：2026-09-10（Australia/Adelaide）
> 本文件只保留当前源码身份、能力边界、风险与下一入口。逐轮结果见
> [`validation/`](validation/README.md) 中的独立证据；旧交接内容仍可从 Git 历史读取。

## 当前源码身份

| 项目 | 当前值 |
| --- | --- |
| 仓库 | [HedgehogsGX/Open-Flame](https://github.com/HedgehogsGX/Open-Flame) |
| 开发版本 | `0.28.0` |
| 数据库 | Download Schema 11；Editing Schema 4；Upload Schema 3；Workflow Schema 3；Workflow preset Schema 2 |
| 上传备份格式 | 2 |
| 首批上传平台 | Bilibili、抖音、视频号（内部 ID `tencent`） |
| 前端方向 | 方向 C“编辑式玻璃”，遵循 [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md) |
| Git 交付 | 关键步骤后签名提交并直接推送 `origin/main`；禁止 force push |
| Git 身份 | `Cyaegha_Xu <85352261+novahanser@users.noreply.github.com>` |

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
- 编辑域支持完整视频、1–10 个非破坏分段、封面取帧/文字、审核后渲染，以及可选的
  自动听写、中文与 English 翻译和标准音色配音。`segments: []` 表示完整视频；只做封面
  也会生成可上传的完整视频。
- Workflow 支持 URL → 下载 → 编辑 → 所选账号上传的持久化编排、恢复、取消和精确确认。
  保存一次完整预设后可只更换 URL；来源标题、绑定 asset 与逐账号最终标题在下载 ready
  时冻结，预设仅在账号、AI capability 和预设初始读取成功且操作者尚未修改表单时恢复。
  账号可保存“创建流程后 N 个整小时发布”；每次运行以固定锚点物化为绝对时间，响应丢失
  重试会复用相同锚点、幂等键和完整投稿参数。
- 上传域支持首批三平台逐账号标题、简介、标签、封面、原创/转载、分区、声明、动态文案、
  视频号短标题/内容标签、草稿模式和平台允许范围内的定时发布。最多选择 3 个账号；
  1–10 个输出最多形成 30 个上传 slot；重试后继必须保持账号、来源、平台及完整投稿参数
  不变，标题、标签、封面、模式、发布时间或平台选项漂移都会失败关闭。
- 本地 HTTP 写操作使用域独立 CSRF 与严格 loopback Host/Origin/Fetch-Site 防护；受管媒体
  使用同一文件身份、匹配打开和有界散列原语，同时保留 Editing/Upload 各自错误与事务边界。
- 架构精简 S1–S8 已按小提交完成：HTTP guard、公开 profile/metadata/identity 契约、AI/
  Upload/Edit snapshot 解释、verified media response、EditingManager、受管文件原语，以及
  Workflow 上传表单与 recipe 的职责拆分。没有引入新服务、框架或通用调度基类。

## 当前证据入口

| 范围 | 当前记录 |
| --- | --- |
| 当前状态与 CI 策略 | [文档状态收敛](validation/iteration-0.28.0-document-status-consolidation.md) |
| Workflow 前端职责 | [recipe 分责](validation/iteration-0.28.0-workflow-recipe-functions.md)、[上传表单分责](validation/iteration-0.28.0-workflow-upload-form-functions.md) |
| 文件与 HTTP 边界 | [受管文件读取](validation/iteration-0.28.0-managed-file-read.md)、[下载 HTTP 防护](validation/iteration-0.28.0-download-http-boundary.md) |
| Workflow 编排 | [Edit snapshot](validation/iteration-0.28.0-edit-snapshot-observation.md)、[Upload snapshot](validation/iteration-0.28.0-upload-snapshot-observation.md)、[AI snapshot](validation/iteration-0.28.0-ai-snapshot-application.md) |
| 纯数据契约 | [上传身份](validation/iteration-0.28.0-upload-identity-contract.md)、[上传重试完整投稿身份](validation/iteration-0.28.0-upload-retry-payload-identity.md)、[Workflow profile](validation/iteration-0.28.0-workflow-profile-contract.md)、[上传 metadata](validation/iteration-0.28.0-upload-metadata-contract.md) |
| 用户功能 | [来源标题与网址即运行](validation/iteration-0.28.0-workflow-source-title.md)、[相对发布时间预设](validation/iteration-0.28.0-workflow-relative-schedules.md)、[无 AI 完整视频](validation/iteration-0.28.0-no-ai-full-video.md)、[编辑式玻璃前端](validation/iteration-0.28.0-editorial-glass-frontend.md)、[来源封面调研与导入](validation/iteration-0.28.0-source-cover-research-and-import.md) |
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
3. **当前源码未冻结。** `0592b6f` 之后的功能与架构提交需要新的 clean candidate、源码/
   wheel 独立安装和包外 receipt，才能形成新的发布结论。
4. **托管 CI 仍为红色。** CI 已在 Windows/Linux 与 CPython 3.12/3.13 真实执行；环境和
   提交范围门禁通过后，完整 pytest 因冻结历史测试与当前安全/Schema/版本合同不一致而失败。
   按仓库策略不得通过修改这些测试或弱化生产合同来伪造绿色结果。
5. **目标 Linux/Docker 未验收。** Windows 本地与 synthetic 结果不关闭 T15 的 namespace、
   ACL、mount、AF_UNIX、恢复和第三方 runtime 分发边界。
6. **真实下载能力仍按样本证据限定。** 历史少量 YouTube/X/Instagram 成功、Bilibili 412、
   Douyin `authentication_required` 和未执行平台都不是平台级支持或否定结论。固定 yt-dlp
   的 Bilibili 提取器可读取 `videoData.pic`，Douyin 提取器可列出 `cover`、`origin_cover`
   等变体，但当前单 thumbnail 流程不保证得到 `origin_cover`，且两平台真实封面提取尚未验收。
   视频号没有专用 yt-dlp extractor，本轮没有增加视频号网址封面能力。“来源封面（平台返回）”
   也不代表发布者原始母版、最高分辨率或无损文件。

## 下一入口

1. 外部测试人员从 [`TESTING.md`](TESTING.md) 开始，按
   [`docs/DEBUG_GUIDE.md`](docs/DEBUG_GUIDE.md) 定位问题，并用
   [`docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md`](docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)
   返回环境、精确 commit、步骤、脱敏日志与结果。不要提交 Cookie、token、URL 样本、媒体、
   本机路径或 `validation/local/` 内容。
2. 收到外部反馈后，先复现并修复本地可证明的问题；每个关键修复独立签名提交并直推
   `origin/main`。首批上传范围保持 Bilibili、抖音、视频号。
3. 真实 OpenAI、真人试听、扫码/登录、上传、定时发布和公开可见性验证必须在该具体动作已
   明确授权、凭据留在 Git 外且精确候选固定后进行。每个结果按平台、来源类型、适配器版本、
   环境和 commit 单独记录。来源封面还需分别用 Bilibili 与 Douyin 的已授权样本对照平台可见
   封面、下载 artifact 和导入副本；视频号保持未支持，不能用通用 extractor 结果替代专用证据。
4. 功能反馈收敛后再创建 clean release candidate，执行源码与 wheel 独立安装、完整检查、
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
