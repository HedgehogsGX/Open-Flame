# 多平台视频下载项目开发交接

> 每轮结束更新本文件的状态、证据、风险、下一入口和历史。
> 最后更新：2026-09-10
> 当前迭代：Iteration 0.28.0 发布后开发 — Workflow Schema 3 来源标题冻结、网址即运行预设恢复、有序多输出、完整视频默认/生产 1–10 段界面、四项运行就绪度、三平台投稿参数、服务端零副作用执行/封面预检、三账号上传 fan-out、预授权 queued 重启续跑、整流程安全取消、重复下载 owner 恢复、配音语速端到端贯通、方向 C“编辑式玻璃”四页生产实现、译文精确 `revision_id` 绑定、上传 attention 精确确认恢复及当前应用根 AI/上传 runtime 刷新均已通过当前本地 synthetic/offline、浏览器、故障恢复或实际本地启动验证；真实模型、真人试听、三平台发布与当前制品仍未验收
> 当前版本：`0.28.0`；下载数据库：Schema `11`；编辑数据库：独立 Schema `4`；上传数据库：独立 Schema `3`；自动流程数据库：独立 Schema `3`；上传备份格式：`2`

## 本次交接入口

2026-09-10 Workflow recipe 分责：生产 `/workflows` 页已把封面输入、封面校验与 AI recipe 输入从总编排拆为普通函数；`recipe()` 现在只按原顺序处理操作者状态、AI 成对约束、完整视频/分段、封面、能力/音色/语速并显式返回四个既有字段。cover 子控件短路读取、AI 配对早于 segment、固定比例→平台交集→时间戳、translation capability→speech capability→voice→rate 的错误优先级保持；segment、平台不兼容比例与语速错误仍聚焦原控件，source-title 独立封面标题、`segments: []` 完整视频、精确 speech authorization 与 0.88–1.12 数值语速保持。五组当前生产模板浏览器回归、无 AI **5/5**、语速 **10/10**、AI 授权/多分段/预检及两条三平台禁网整链均 PASS，独立逐行审查无 P1/P2。没有改 DOM、编辑式玻璃视觉、后端权威校验、Schema、依赖或已跟踪测试，见[Workflow recipe 分责验证](validation/iteration-0.28.0-workflow-recipe-functions.md)。S7 至此完成，下一入口为 S8 当前状态文档收敛与 CI/hook 提交范围分类器复核；本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 Workflow 上传表单分责：生产 `/workflows` 页已把上传账号/公共字段读取、保存预设与当前修改的逐账号合并、既有即时字段校验及最终 payload 编排拆为四个普通函数。source-title 空公共标题、独立平台标题、三账号顺序与上限、深拷贝 saved override、`changedUploadFields` 选择性覆盖、DST/定时时区解析、三平台 AI 标签默认值、错误顺序及 focus/ARIA 目标保持；动态标题长度与发布时间提前量仍来自当前 capability，没有引入框架或规则服务，也未改 DOM、视觉、后端权威校验或确认语义。四组在当前 `WORKFLOW_HTML` 上运行的 Chrome 回归均 PASS，其中 readiness 新核对轮询期间的 URL 值、焦点及精确文本选区；六组 Python 契约/服务验证与 inline JavaScript 语法均 PASS，独立 diff 审查无 P1/P2。没有新增或修改已跟踪测试，见[Workflow 上传表单分责验证](validation/iteration-0.28.0-workflow-upload-form-functions.md)。S7 下一入口为独立拆分 `recipe()` 的封面构建/校验与 AI 参数构建；本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 受管文件读取边界：公开 `managed_files.py` 现同时提供 ownership-transfer 的匹配打开、重复 `fstat` 身份核对和带可选 raw chunk manifest 的有界 SHA-256；增长文件最多读取 `maximum + 1` 字节即停止。Editing 普通散列和 same-handle retained response、Upload stable digest、受管来源复核及 source/cover 匹配打开均复用该底层机制，但继续保留各域大小限制、错误码、final `lstat` 时序、复制、封面 payload/解码、发布、回滚、lease、缓存和 SQLite 事务。读取矩阵 **27/27 PASS**，身份矩阵 **20/20 PASS**，既有消费者 **103 passed**，Range/替换/取消/复制/封面聚焦边界 **14 passed**，独立 response 矩阵 **13/13 PASS**；五个 Editing 模块仍为 **73 passed / 3 个已知 Schema 1 旧断言**。取消安全、无 AI 完整视频和 Bilibili/抖音/视频号禁网整链均 PASS，三个只读审查在修正异常句柄关闭和哨兵读取后无 P1/P2。没有新增或修改已跟踪测试，见[受管文件读取边界验证](validation/iteration-0.28.0-managed-file-read.md)。S6 至此完成，下一入口为 S7 前端参数构建/预设合并/即时校验分责；本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 受管文件身份边界：Editing 与 Upload 已统一使用公开 `managed_files.py` 中完全相同的四字段身份签名及 plain-entry `lstat` 判定；regular/directory、symlink、Windows reparse 与普通文件单链接规则只维护一份。两域薄 wrapper 仍分别保留 `editing_media_unavailable` / `unsafe_editing_file` 与 Upload 的原始 `OSError` / `unsafe_upload_file` 边界；打开、散列、复制、retained handle、Range、大小限制、缓存、lease、发布、回滚与 SQLite 事务均未迁移。忽略目录安全矩阵 **20/20 PASS**，既有 Editing/Upload 回归 **103 passed**，Range/替换/取消/复制/封面聚焦边界 **14 passed**，source inventory 为 281 项且无未列出的 package 文件。没有新增或修改已跟踪测试，见[受管文件身份边界验证](validation/iteration-0.28.0-managed-file-identity.md)。S6 下一入口为共享受限打开与有界散列循环，同时继续保留各域错误映射和 same-handle 语义；本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 EditingManager 职责边界：后台 editing service/worker、root activity lease、AI 优先 claim、render、取消、active-operation 与 stop deadline 已整体迁入公开 `editing.manager`；该模块不导入 FastAPI、Pydantic、HTTP route、verified response 或顶层应用 API。`editing.api.EditingManager` 继续指向同一类，路由安装通过私有 service factory 注入保留既有测试/组装 seam；LocalWorkflowAdapter 的类型引用也改到 canonical manager 且运行时仍不加载它。27 个方法的 AST 对比、5 项边界探针、3 项 stop/claim/cancel 竞态、两项取消 validator、45 项 AI authorization、无 AI 5/5、AI full-video 与三平台禁网整链均通过；五个 Editing 测试模块为 73 passed / 3 个已知 Schema 1 旧断言。没有新增或修改已跟踪测试，见[EditingManager 边界验证](validation/iteration-0.28.0-editing-manager-boundary.md)。S5 至此完成，下一入口为 S6 受管文件底层原语；本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 verified media response 边界：已打开并完成身份/摘要核验的媒体句柄现在统一由公开 `verified_media_response.VerifiedOpenFileResponse` 负责 full/HEAD/Range、逐块复核与关闭；Editing 路由直接导入该边界，不再延迟反向导入顶层 `api.py` 私有类。下载域的注册路径检查、snapshot/spool 与断连协作仍留在顶层 API，编辑域的受管文件打开、1 MiB manifest 和领域错误映射仍留在 EditingService。旧 `api._VerifiedOriginalFileResponse` 只保留同一类的 import alias，未保留第二份实现。独立 26 项响应矩阵、既有聚焦 5 项和编辑媒体 32 项均通过；Editing API 为 15 passed / 2 个已知 Schema 1 旧断言，下载资产组合的 25 个失败仍在旧 testserver/缺失 CSRF 建立阶段。没有新增或修改已跟踪测试，见[verified media response 验证](validation/iteration-0.28.0-verified-media-response.md)。本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 EditSnapshot 集中观察：Workflow 的 ledger 对账、人工编辑确认、显式重试、普通推进与重启推进现统一经无状态分类器和单一观察入口解释“待确认、已 active、ready、可重试 failed、attention、非法域数据”。`EditSnapshot.needs_confirmation` 明确区分 review 与 queued/running/canceling；人工确认和重试都会先检查当前状态及 reason，reason 漂移先写新 revision，ready/active/attention/非法数据与 ledger 阻断均不会错误调用 mutation-capable confirm/retry。只有编辑域当前明确报告 failed 且 reason 与操作者所见一致时才建立重试后继；旧 adapter 的位置参数、空 failure fallback 与 rendering/waiting 空 reason 行为保持。全部 23 个 Workflow validator、automation、无 AI 整段、21 项副作用矩阵及 follow-up 漂移探针均 **PASS**，既有聚焦回归 **138 passed**，没有新增或修改已跟踪测试。见[EditSnapshot 集中观察验证](validation/iteration-0.28.0-edit-snapshot-observation.md)。本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 UploadSnapshot 集中观察：Workflow 的 attention 恢复、人工上传确认、自动确认门槛与 uploading 轮询现统一通过 `inspect → current retry leaf checkpoint/CAS → classify` 解释 ready、attention、仍需草稿确认、已确认 active 与非法域数据。确认权限、冻结 auto flag、persisted + observed reason 双 allowlist、单轮 restart barrier、账号/session 事务复核、outcome 与取消仍留在原边界。人工确认遇未知 status 或 `waiting + needs_confirmation=false` 时不再调用 mutation-capable `confirm_uploads`；successor leaf 变化仍先写新 revision 并要求重新确认。attention **7/7**、上传服务 **43 passed**、重启/fan-out/v0.28/automation、取消/并发/checkpoint gap、无 AI 整段与三平台禁网完整链均 **PASS**，7 项副作用/门槛探针 **PASS**，没有新增或修改已跟踪测试。见[UploadSnapshot 集中观察验证](validation/iteration-0.28.0-upload-snapshot-observation.md)。本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 AI snapshot 集中处理：人工确认、普通/重启推进与远程账本 attention 对账现在统一通过无状态分类器及一个显式上下文应用函数解释 AI 域观察；授权参数、自动 reason allowlist、ledger 异常、CAS 与是否继续推进仍由各调用路径持有。`waiting/review`、`failed/attention`、ready 引用与既有上下文错误码保持；人工确认收到未知 status 时现以 `workflow_domain_data_invalid` 失败关闭，不再经 `advance()` 第二次调用适配器。授权/ledger/重启/v0.28/automation、多分段、无 AI 整段、语速、三平台禁网完整链与 checkpoint-gap validator 均 **PASS**；未知状态探针确认只调用 `[(True, True)]`，没有新增或修改已跟踪测试。见[AI snapshot 集中处理验证](validation/iteration-0.28.0-ai-snapshot-application.md)。本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 上传身份纯契约：账号 binding、按输出/账号有序的 upload target、取消批次与 current retry leaf 身份现统一使用公开且无状态的 `uploads/identity.py`；`UploadService` 的私有 `_cancel_batch` 已删除，Workflow profile、prepare/resolve/inspect/cancel/discovery 不再各自维护字段集合和平台枚举。无 targets 的 legacy inspect 仍由上传域保持 1–64 根，绑定 Workflow 仍是最多 10 输出 × 3 账号 = 30 slots；retry 遍历、事务内 leaf/successor 复核、当前 session 检查和完整 request digest 均留在原权限边界，重新登录仍不阻止精确身份的紧急取消。上传聚焦回归 **243 passed**，取消/并发/discovery、多分段/重启/预检/attention、无 AI 整段、v0.28、三平台禁网完整链及 64 根兼容探针均 **PASS**，没有新增或修改已跟踪测试。见[上传身份纯契约验证](validation/iteration-0.28.0-upload-identity-contract.md)。本轮没有真实网址、OpenAI、登录、上传、定时发布或公开结果验证。

2026-09-10 Workflow profile 纯契约：profile normalize/canonical JSON/SHA、来源标题 concrete upload 快照与有序 edit→upload outputs 已从 2,400 余行状态机服务迁入公开且无数据库/文件/runtime 副作用的 `workflows/profile.py`；API、manager、adapter、service 与 preset 统一使用 `contracts.WorkflowError`，preset 不再导入任何 Workflow service 私有 helper，也没有保留第二份实现或兼容 wrapper。原函数块只做公开命名替换后的逐字节对比 **PASS**；来源标题 **7/7**、无 AI 整段 **5/5**、预设、多分段、重启续跑、上传 attention **7/7**、取消、v0.28 与三平台禁网完整链均 **PASS**。架构报告中的 `title_mode: []/{}` 500 属于旧快照，当前直调为 `invalid_workflow_profile`、生产 API 为 422，四类下游调用均为 0。没有新增 Schema、服务、线程、队列、runtime、依赖或已跟踪测试，见[Workflow profile 纯契约验证](validation/iteration-0.28.0-workflow-profile-contract.md)。真实 URL、OpenAI、真人试听和三平台投稿仍未执行。

2026-09-10 上传参数纯契约：上传标题限制、标签、三平台专属选项、目标 override 字段与发布时间规则已从 `UploadService` 的私有实现收敛到公开且不接触数据库/文件/账号/runtime 的 `uploads/metadata.py`。上传执行、备份与 Workflow 预设复用同一实现，`workflows/presets.py` 不再跨域导入 Upload service 私有常量或静态方法；数据库、账号 session、封面文件、runtime 与当前时间窗口复核仍留在上传服务。上传/平台参数/备份/Schema 3 回归 **240 passed**，三项预设 validator 与 compileall 均 **PASS**，没有新增 Schema、服务、线程、队列、依赖或已跟踪测试。见[上传参数纯契约验证](validation/iteration-0.28.0-upload-metadata-contract.md)。该切片没有真实登录、上传、定时发布或 OpenAI 调用。

2026-09-10 无 AI 完整视频：编辑页和自动流程现在把 `segments: []` 解释为一个完整视频输出；只启用封面时会得到 `segment-001.mp4` 与 `cover.png`，显式 1–10 个 Workflow 分段及 AI 全片的既有语义不变。API worker 与直接 `EditingService.process_next()` 共用同一个 ordinary-plan 渲染入口，完整视频仍经现有 H.264/AAC/MP4 转码、源文件身份复核、claim 私有 staging、ffprobe 校验、二次大小/SHA-256 核对和事务登记后成为普通 editing asset，上传域没有新增旁路。真实本地 FFmpeg/重启/adapter/AI 隔离 validator **5/5 PASS**，两张生产页 Chrome 验证 **PASS**，既有三平台 AI 完整视频禁网整链 **PASS**，相关 pytest **48 passed**；编辑聚焦组 **65 passed / 3 个既知 Schema 1 旧断言失败**。没有新增服务、数据库、Schema、队列、runtime、依赖或已跟踪测试，见[无 AI 完整视频验证](validation/iteration-0.28.0-no-ai-full-video.md)。真实网址、OpenAI、真人试听与三平台投稿仍未执行。

2026-09-10 托管 CI 执行链恢复：提交 `bdd88ce184b2f86f957f7df9129baa21863227dd` 已修正 Windows 可取得的 CPython 3.12.10 矩阵、完整 Git 历史、事件推导的提交范围检查、locked dev environment 与矩阵解释器身份绑定。[GitHub Actions run 34393235622](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622) 的 Windows/Linux × 3.12.10/3.13.14 四格均已通过 checkout、解释器选择、commit scope、固定 uv、CI definition、runtime identities、locked install、`uv pip check` 和 Python/pytest 身份检查，随后四格都在完整 pytest 失败，run 总结论为 `failure`。本机完整结果为 **173 failed, 2277 passed, 8 skipped in 327.39s**，主要是冻结历史测试仍用 `testserver`/无下载 session-CSRF，以及 Editing Schema 1、旧 release metadata/version、旧 UI/validation identity 断言。测试文件未改，产品没有测试绕过；结论是托管执行基础已恢复、CI 仍红，required checks / branch protection 仍为 **NOT CONFIGURED**。逐格链接和边界见[托管 CI 恢复证据](validation/iteration-0.28.0-hosted-ci-recovery.md)。

2026-09-10 下载 HTTP 边界：顶层下载首页和非 Editing / Upload / Workflow 的 `/api/v1/*` 现校验唯一 loopback Host、同源 Origin/Fetch-Site，并要求 8 个写路由携带当前 `GET /api/v1/session` 取得的进程内 `X-Download-CSRF`。页面在令牌成功且通过非空 ASCII 校验前保持提交禁用；上传、编辑、自动流程仍使用独立令牌，Upload QR GET 和 Workflow 精确根路径特例保持。ignored 合同探针和当前生产页 Chrome 验证均 **PASS**，8 个写路由的拒绝路径下游调用数为 0，外网尝试为 0，见[下载 HTTP 边界验证](validation/iteration-0.28.0-download-http-boundary.md)。历史 TestClient 回归仍使用 `testserver` 且无新 CSRF 会话，本切片未写入产品绕过也未修改已跟踪测试。架构报告使用的旧快照及其 ignored 本地链接不作为当前发布证据。

2026-09-10 Workflow 扫描恢复：架构复核确认旧实现会在 `active_page()` 抛出 `WorkflowError` 或 `sqlite3.Error` 时结束唯一的后台 reconciliation 线程，后续 `get()`/`wake()` 不能恢复。当前实现把这两类已知读取错误收敛为未派发工作的失败轮次，保留游标、复用原有最多 6 秒退避并由同一 worker 重试；没有增加线程重建或未知远端操作重放。故障探针 **4/4 PASS**、重启续跑验证 **PASS**、既有 local-app 回归 **93 passed**，且没有新增或修改测试文件，见[Workflow 扫描恢复验证](validation/iteration-0.28.0-workflow-manager-recovery.md)。该记录只绑定其自身源码与本地故障注入结果。

2026-09-10 来源标题冻结与网址即运行：`/workflows` 已默认选择“使用下载来源标题”，并在成功应用或保存预设后只记住该预设 ID；账号、AI capability 和预设列表全部完成初始读取后才恢复，任一初次读取失败都会明确保持表单原值，后续刷新也只会在用户尚未编辑时恢复。首次仍须明确选择账号并保存 Bilibili 分区/标签/原创转载、抖音声明、视频号模式以及封面/发布时间等不可从 URL 推断的参数；后续相同配置可只更换 URL。下载 ready 时从 ready asset 关联的 `source_items` 原子读取标题；X 附件自身没有标题时仅回退到该下载 Job 所属输入的父来源标题。服务按上传 capability 为 Bilibili、抖音、视频号生成各账号最终标题，保留显式账号标题，并把 concrete `resolved_upload` 与 asset ID 一次冻结到 Workflow Schema 3。重启、1–10 段 fan-out、上传准备和取消 checkpoint 对账均复用同一快照；crash 留在 `downloading` 时不会用变化后的 metadata/capability 重算，metadata 重试也不能换绑 asset。精确旧 Schema 1/2 只按旧 profile grammar 迁移，不能预埋新 `title_mode` 获得新语义。来源标题或 capability 暂不可用时进入 `workflow_source_metadata_unavailable`，显式“立即对账”只重试本地解析，不重复下载。该切片只新增一个 nullable column 和不可变 trigger，没有新表、服务、线程、队列、runtime 或依赖。验证见[来源标题冻结记录](validation/iteration-0.28.0-workflow-source-title.md)；真实 URL 下载、OpenAI、账号登录和三平台上传/发布没有在本轮执行。

2026-09-10 当前应用根运行时刷新：在应用停止后先保留旧 Upload runtime，并用 SQLite backup API 在 Git 外保存迁移前数据库，再用同一个 canonical `--app-root` 完成源码 Setup 与 Start。AI runtime 现以固定 CPython 3.13.15 Windows x64 归档构建并通过完整性检查；Upload runtime 现验证固定 Social Auto Upload revision `0012d2c355f88f683cc38dde2a2db209e14091bc`、Biliup `v1.2.4` 和首批三个适配器。首次上传 API 初始化把实际数据库从 Upload Schema 1 精确迁移到 Schema 3，原账号行和两条 canceled job 均保留，迁移前后 `quick_check=ok` 且无外键错误。实际本地 Start 后下载 runtime 为 `managed_direct / online`，上传 backend、scheduler 与 worker ready，四张生产页均为 HTTP 200；AI 完整性 verified，但因没有 API key 保持 `provider_health_required`，没有发起 OpenAI 请求。该 packaged 开发宿主存在 Windows filesystem virtualization，普通 `%LOCALAPPDATA%` 词法路径与 Python 最终解析路径不同；builder 的安全拒绝正确，未放宽路径规则。此环境须让 Setup 与 Start 使用同一经人工核对的 canonical `--app-root`，普通非虚拟化终端继续使用默认根。没有扫码、登录、下载任务、上传、定时发布或公开提交；已存账号的本地 ready 状态不证明远端 session 有效。详见[当前应用根运行时刷新记录](validation/iteration-0.28.0-local-runtime-refresh.md)。

2026-09-10 上传 attention 精确确认恢复：Workflow 现在从上传域快照读取明确的 `needs_confirmation`，不再用空 reason code 猜测草稿是否已经确认。当前 leaf 含任一 `draft` 时，手动流程回到 `awaiting_upload_confirmation`，自动流程也只有在冻结 profile 已预授权且既有正向 code allowlist 允许时才确认；仅含 `queued`/`running` 时直接恢复 `uploading`，同时闭合“上传域确认已提交、Workflow 状态写入失败”的 checkpoint gap，不重复调用域确认。混合 draft + active/success 只确认剩余草稿，终态 outcome 与 unknown/failed 边界不变。聚焦 ignored 验证 **7/7 PASS**，automation、v0.28、restart、multisegment、full-chain/full-video 以及上传服务/韧性 **43 passed**；没有新增表、Schema、服务、线程、队列、runtime、依赖或已跟踪测试，见[上传 attention 恢复记录](validation/iteration-0.28.0-workflow-upload-attention-recovery.md)。

2026-09-10 译文精确修订绑定：ready 翻译 recipe 现可保存操作者所选批准译文的精确 `revision_id`。新手动草稿与新 AI-ready workflow 草稿写入 ID；服务端复核项目、目标语言/provider/model、已批准父听写、source language（`auto` 除外）和 cue 结构，建立计划时要求请求 ID 与 recipe ID 相等，并在既有 `plan_timeline_bindings` 冻结译文、父修订及两份 cue 摘要。已有 ID 只恢复精确修订；legacy recipe 仅在完整匹配唯一时自动恢复，零个或多个匹配保持未选并要求明确重选，下一次保存完成升级。旧 in-flight AI-ready workflow 草稿保持可读且不被静默改写，profile/preset 禁止保存运行期 ID。translation ready 而 dubbing 仍 review/blocked 时可建立可检查且已精确绑定的 review 计划，`confirm_plan` 仍拒绝排队。服务验证 **17/17 PASS**、严格浏览器验证及 **4 个既有浏览器回归 PASS**，full-chain/full-video/speech-rate/multisegment 也均 **PASS**；聚焦 pytest 为 **69 passed、2 failed**，两项仍是既有 Editing Schema 1 旧断言，测试文件未改。没有新增表、Schema、服务、队列或依赖；没有真实 OpenAI、真实下载、真实上传/发布或真人听审，见[译文精确修订绑定记录](validation/iteration-0.28.0-translation-revision-binding.md)。

2026-09-09 编辑式玻璃四页生产前端：用户选定的方向 C 已同步到共享 `open-flame.css`、下载 `/`、编辑 `/edits`、上传 `/uploads`、自动流程 `/workflows`、`DESIGN_SYSTEM.md` 1.4 与设计预览。四页使用暖纸/暖炭画布、编辑式大标题、双瓣品牌标记、浮动玻璃导航、选择性玻璃主命令区和实底正文卡；767px 以下顶栏把品牌/主题与导航分行，导航可随文字缩放继续换行，窄屏持续消息不再遮挡。四个生产模板除标题中的同文 `aria-label` 和无事件 `<em>` 外未改业务 DOM/JavaScript。当前 Chromium 覆盖四页 × 三视口 × 两主题、reduced-motion/transparency、桌面 200% 根字体及四页 × 320/768/1024px 的 200% 交叉场景，共 **44/44 PASS**，无整页横向溢出；浏览器侧未观测到非 loopback 请求、控制台/页面/请求或 HTTP 错误，见[编辑式玻璃四页生产前端证据](validation/iteration-0.28.0-editorial-glass-frontend.md)。ignored 候选、validator、报告与截图仍只在 `validation/local/`；本地视觉验证不证明真实 OpenAI、平台参数接受、审核或发布。

2026-09-09 配音语速贯通：编辑页与自动流程页现在接受有限的 `0.88`～`1.12` 语速，旧 recipe/preset 缺失字段时继续使用 `1.0`，且默认值不写入 canonical JSON，历史 recipe/profile SHA 保持兼容。非默认值冻结到配方和 workflow profile，预设原样恢复，确认卡同时显示音色与实际语速；输入变化立即撤销 workflow 的旧外发同意。渲染器把冻结值传入既有 `SpeechOptions`，协议、bridge、worker 与 OpenAI provider 沿既有路径把它映射为 `speed`，脱敏 request fingerprint 随值变化，而 authorization SHA 保持不变。非法类型、布尔、NaN/Inf、超大整数和越界值在 Python/API 边界稳定失败；轻微溢出可由操作者明确提高语速处理，较大溢出仍以 `ai_speech_timing_overflow` 停止，不自动发起第二次付费调用。冻结基线复现为 8 RED/2 PASS，当前 ignored validator 为 10/10 PASS；页面脚本、预设/API 往返、provider 转发和现有溢出边界均已覆盖，见[配音语速验证](validation/iteration-0.28.0-speech-rate.md)。没有新增 Schema、数据库、runtime、服务、线程、队列或依赖，也没有新增或修改已跟踪测试文件。真实 OpenAI 质量、费用、真人试听和平台发布仍待外部验收。

2026-09-09 重复下载 owner 恢复：同一规范网址已有 live/ready 下载 job 时，新 workflow 保留自己的 `duplicate` 批次且不建立第二个 job。`BatchRepository.inspect_single_input_download()` 在一个 deferred SQLite 读事务中同时读取 workflow 批次、可用资产和最终 owner；逐层核对完整来源身份、无 job/error 的 duplicate 节点、根指针、最多 32 层、flat download 来源/job kind/current generation/无 graph target，并拒绝断链、循环、身份或状态矛盾。`LocalWorkflowAdapter` 在 owner active 时保持 `downloading`，ready 后复用原 asset，failed 时只传播下载域 `ErrorCode`，canceled 固定为 `download_canceled`；不一致数据进入 `download_state_invalid`，不会重新下载。14 项 ignored 状态/篡改验证、独立 WAL snapshot 并发探针、35 项下载回归、完整视频与多段三平台离线整链以及三个 Workflow Chromium 回归通过，见[重复下载 owner 验证](validation/iteration-0.28.0-workflow-duplicate-download-owner.md)。没有新增 Schema、数据库、服务、线程、队列、依赖或测试文件。该记录当时列出的语速缺口已由上方后续切片修复；真实下载、OpenAI、真人试听和三平台发布仍未运行。

2026-09-09 整流程安全取消：`POST /api/v1/workflows/{id}/cancel` 以当前 revision 提交持久取消意图，`WorkflowManager` 重启后仍会从最远的已创建下游继续处理。下载、编辑 project/plan 或逐段上传已创建但 workflow 引用尚未写回时，会按稳定请求键发现；Editing 与 Upload 在对应稳定请求缺失时，会在各自的单一 `BEGIN IMMEDIATE` 中写入取消占位，阻止并发 stable-key 创建。Editing 同时核对来源、原始配方、请求摘要、完整 retry 图与当前 leaf，再取消 render 或全部 AI leaf。上传发现只接受完整标题、标签、封面、发布时间及平台参数一致的 v2 request digest，随后在原域事务内核对最多 30 个 job/source/account/platform slot。draft/queued 可安全停止，running/canceling 保持等待；只有完整 fan-out 已 checkpoint 且全部已提交或已保存平台草稿时才按原 outcome 完成，pending slot 即使已出现成功证据也进入 `upload_partially_completed` 人工核对。部分成功、未知远端结果、缺失/错配引用及身份漂移同样进入精确 attention code，不会伪装成取消。取消成功终态固定为 `canceled/workflow_canceled`，重复取消幂等，旧 revision 与已完成流程拒绝；旧 adapter 缺少 discovery seam 时失败关闭，不抛 500。相同数据库路径的 WorkflowService 共用进程内 mutation lock，引用/状态写入再以 revision/state/code CAS 防止旧推进覆盖取消。页面提供整流程取消按钮并在轮询重绘后保留输入、展开状态和焦点。本地 ignored service、checkpoint gap、并发、adapter、API、上传批量取消与页面脚本验证以及相关现有回归见[整流程取消证据](validation/iteration-0.28.0-workflow-cancellation.md)；没有新增 Schema、队列、服务、依赖或测试文件，真实 Worker/provider/平台 backend 运行中取消仍待外部验收。完整外部复验从[测试手册](TESTING.md)开始，失败按[Debug 指南](docs/DEBUG_GUIDE.md)定位并用[回传模板](docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)交接。该记录当时列出的重复下载 owner 缺陷已由上方后续切片修复。

2026-09-09 自动流程完整视频默认：`/workflows` 不再默认启用隐藏风险较高的 `0–60 秒` 分段；首次进入显示“完整视频 · 0 段”，AI 流程提交 `segments: []` 并复用既有完整源输出语义。操作者主动开启分段时仍取得一个可编辑的 60 秒起始模板，带分段预设继续精确恢复。真实 Chrome 已验证完整视频请求 body 与既有多分段/窄屏行为；真实下载 Worker、隔离合成 AI runtime、FFmpeg、编辑/上传服务的禁网整链对 2 秒完整源完成听写、翻译、两段配音并向三个合成平台提交同一完整成品。`LocalWorkflowAdapter` 同时把仅用于类型标注的 `EditingManager` 改为 `TYPE_CHECKING` 导入，避免工作流适配器运行时连带载入 FastAPI/Pydantic。没有新增服务、线程、队列、数据库、Schema 或依赖；证据见[完整视频默认记录](validation/iteration-0.28.0-workflow-full-video-default.md)。真实网络、模型质量与平台发布仍未运行。

2026-09-09 已预授权流程安全重启续跑：编辑与上传域仍先把旧进程的 queued 行降回 `review/draft + restart_confirmation_required`，而 WorkflowManager 下一轮只会为 canonical profile 中保存了对应 auto-confirm、当前 leaf 没有 `retry_of`、且当前授权/runtime/source/账号 session/完整平台参数重新校验通过的原始任务重新排队。自动确认只接受正常首次原因和精确 restart 原因；legacy migration、混合或未知 review reason 均保持人工确认。域 claim 都会在 provider、FFmpeg 或上传 backend 前事务性地把 queued 改为 running，因此该 restart code 是“上次未 dispatch”的持久化证据。AI/render/upload retry 的谱系优先于通用 restart code；running、canceling、账本 dispatched/unknown 和上传 unknown 均继续停下。10 段 × 3 账号策略矩阵、混合 review reason、无效或非 canonical 平台参数拒绝、auto flag/profile digest 篡改拒绝、真实 WorkflowManager 启动扫描及真实上传域离线整链均通过，整链没有重放 AI 或渲染；AI/render 的底层恢复另由既有定向回归与 ledger validator 证明。本次不是 abrupt power-loss、浏览器重启链或三域真实 E2E。上传库没有为 otherwise-valid metadata 另存不可变摘要，因此本次不声称能检测 canonical-to-canonical 的直接数据库改写。证据见[预授权重启续跑记录](validation/iteration-0.28.0-workflow-restart-continuation.md)。

2026-09-09 三平台参数与封面预检：`/workflows` 现按所选账号显示 Bilibili、抖音和视频号参数面板，可分别覆盖标题、简介、标签、发布时间、Bilibili 动态/转载/评论弹幕开关、抖音三种自主声明，以及视频号发布/草稿、短标题和内容标记。标题上限、发布时间提前量、视频号模式与声明枚举优先读取既有上传 capability；启用 AI 时会逐账号为缺少显式 preset 值的目标补建议标记，显式空值/差异值或用户明确选空均原样保留；共享面板不会把一个账号的声明复制给另一个账号。同平台多账号 preset 差异未编辑时逐账号保留，可按字段或用明确按钮统一整个面板；视频号草稿会清空定时，Bilibili 原创/转载会联动来源。平台校验会关联并聚焦首个错误控件，独立标题与当前有效的 Bilibili 标签输入带动态 required。生成封面只显示固定比例，并按所选平台共同支持范围禁用；冲突的当前或 preset 比例会保留并持续显示错误，直至操作者明确重选。首次 upload preflight 已校验既有受管封面文件/尺寸，生成封面与 preset 封面冲突、无效比例及三平台封面方向/比例错误均在 workflow/event/download 为零时失败，工作流页对可达封面错误提供中文说明。没有新增数据库、服务或线程，且删除了外部 `execution_check` 参数，第二层 runtime probe 由冻结账号绑定推导。证据见[三平台参数与封面预检记录](validation/iteration-0.28.0-workflow-platform-parameters.md)。真实平台参数接受、裁切、定时触发与发布仍未运行。

2026-09-09 自动流程服务端执行预检：`WorkflowService.create()` 现在先核对本次托管下载 Worker、FFmpeg、逐平台投稿参数、所选账号 ready/session revision、上传 runtime/调度器，以及启用 AI 时听写/翻译/配音三项当前 authorization 与标准音色；创建预检失败时不保存 workflow/event，也不建立下载 batch。创建成功后在 `created → downloading` 前用冻结账号绑定和完全无缓存的上传 runtime 校验再次检查，覆盖请求后的窄竞态、重启恢复和 Windows `ctime` 不能代表 ChangeTime 的摘要缓存限制。创建 admission 跳过页面的 10 秒整体验证结果缓存并重新枚举树，只对 identity/大小/mtime/ctime 未变文件复用摘要；同进程扫描用独立锁避免并发冷散列，平台 child 前还会第三次执行完全无缓存检查。首次冷校验不持有 WorkflowService 全局 mutation lock。启动/暂停等短暂下载状态留在 `created` 有界重试，需要修复 runtime、凭据、账号或 authorization 的状态进入 attention，并可由显式“立即对账”重新检查。ignored validator 以外部网络 audit guard 覆盖缓存分歧、三项 AI/音色、账号绑定转发、API 状态码和真实 Local adapter 零下游写入；完整离线整链再次通过。证据见[自动流程服务端预检记录](validation/iteration-0.28.0-workflow-server-preflight.md)。真实下载、OpenAI 与三平台网络仍未运行。

2026-09-09 多分段自动流程切片：Workflow Schema 2 以 `outputs_json` 保存最多 10 个按 recipe ordinal 排列的视频输出；每个输出分别绑定上传 source，并按冻结账号顺序保存 account/platform/current job slot。最多 3 个账号形成不超过 30 个草稿，逐段准备通过稳定幂等键和 durable prefix checkpoint 恢复，但所有草稿仍要一起交给一次 `confirm_many`，任一校验失败时整批不会进入队列。retry 只原位替换当前 leaf job ID，并重新要求确认；Schema 1 只在精确结构、canonical profile/digest、单输出基数和状态/引用一致时事务迁移，矛盾数据库保持 Schema 1。自动 AI 多段只允许首尾连续，避免用 bounding clip 外发未选择的音频。证据见[多分段自动流程记录](validation/iteration-0.28.0-multisegment-workflow.md)。

2026-09-09 多分段生产界面：`/workflows` 已从单段表单改为 1–10 个有序分段，可添加、删除和完整恢复多段预设；提交前核对 100 ms 下限、七天范围、输入顺序、无重叠及 AI 首尾连续。每次分段变化都会撤销旧的 AI 外发同意。页面内限制最多 3 个账号，并显示计划成品、已准备 source、`segment × account` 投稿任务数与批量确认总数；无变化轮询不重建记录，重绘时恢复相同操作的键盘焦点。实现仅复用当前共享组件，未提前采用待选液态玻璃视觉。ignored Chromium 验证覆盖三段 body、10 段上限、预设、焦点和 320 px，无真实网络；证据见[多分段界面记录](validation/iteration-0.28.0-workflow-multisegment-ui.md)。

2026-09-09 自动执行就绪度：`/workflows` 在创建前分别显示本次托管下载 Worker 心跳、AI runtime/三项精确 authorization、上传 runtime/当前 scheduler，以及 `active + ready` 的所选账号。页面按当前是否启用 AI 汇总必需条件；单个探针失败只标记对应项未知，下载状态的两秒轮询不重建表单，并保留输入、账号选区和焦点。实现只复用现有同源 GET 和共享组件，没有新增 API、数据库、线程或服务；前端即时状态不替代服务端创建/领取/执行校验，也不证明远端接受。ignored Chromium 验证覆盖 ready、未选账号、暂停、503、live region 和 320 px；证据见[运行就绪度界面记录](validation/iteration-0.28.0-workflow-readiness-ui.md)。

2026-09-09 前端视觉候选历史：本地 `huashu-design`、`apple-design` 与 `emil-design-eng` 产生的三套液态玻璃候选分别侧重雾面工具台、极光工作区和编辑式玻璃排版，用户随后选定方向 C。候选与比较页保存在 ignored 的 `validation/local/liquid-glass-directions-20260909/`，只记录选型过程；生产实现和验收以本交接入口顶部的当前记录为准。

2026-09-09 复核更正：`b240392` 的旧整链脚本跳过了 AI，仅走一个合成 Bilibili 任务，不能证明 AI/三平台/重启。当前已用真实下载 Worker、LocalWorkflowAdapter、编辑/上传服务、隔离 AI worker 和 FFmpeg 替换验证，实际听写/翻译/配音各一次，输出音频与三平台任务均有断言；真实模型响应和平台网络仍是替身。此前“只剩运行环境”的完成结论撤回。预设还修复了未恢复 AI/音色/定时字段、配音授权摘要缺失和嵌套参数未校验问题。当前证据见[预设记录](validation/iteration-0.28.0-workflow-presets.md)与[整链更正记录](validation/iteration-0.28.0-full-chain-smoke.md)。验证覆盖不足、当前发行冻结和真实平台验收仍是后续工作，目标尚未完成。

2026-09-09 普通启动路径更正：以前 `Start-Open-Flame.cmd` 的 supervisor 在 spawn control child 前会剔除 `OPEN_FLAME_AI_OPENAI_API_KEY`，因此即使 runtime 完整且操作者按文档设置密钥，三项 AI 能力仍会错误地停在 `ai_provider_auth_missing`。当前环境边界仅允许这一个精确变量进入 control child，并执行与 AI executor 一致的值校验；直连下载 Worker、未声明 AI 变量和通用 token/secret 仍保持隔离。ignored 验证用本地重建 runtime 和合成 sentinel 确认三项能力转为 `unverified / provider_health_required`，并强制禁止网络、检查输出不含 sentinel。截至该次验证，普通应用目录仍没有 `data-ai-runtime`，已有上传 runtime 的只读 `--check` 返回 `runtime_upgrade_required`；该环境状态已由顶部 2026-09-10 当前应用根刷新记录更新。

2026-09-09 源码 Setup 追加可选 AI runtime 安装：同一 `Setup-Open-Flame.cmd` 接受 `--ai-python-embed-zip ABSOLUTE_ZIP`，固定只接受已核验 CPython 3.13.15 SHA-256，并把输出精确放到默认或显式 `--app-root` 的 `data-ai-runtime`。缺失目标用现有 builder 原子构建；完整且匹配当前 worker/protocol/provider 的目标只读复用；坏/旧目标拒绝且不删除、覆盖或改 manifest。安装与普通 Start 复用 `.local-app.lock`，运行中返回既有 `setup_busy`；其他 AI 失败收敛为 `setup_ai_runtime_failed`，不回显路径或 child 异常。ignored validator 在临时 app-root 禁网验证首次构建、复用、坏/旧/SHA/锁/runner 失败边界；截至该次验证，默认应用 runtime 的 `lexists` 前后均为 false，没有改动实际业务目录、凭据或调用 OpenAI。当前应用根后续状态见顶部 2026-09-10 刷新记录。

2026-09-09 源码 Setup 已进一步复用既有 `uploads.runtime_setup`：显式 `--upload-runtime` 从同一 `--app-root` 派生 `data-uploads`，默认使用已核验的项目 `.venv`，必要时只用 `--upload-python ABSOLUTE_EXE` 覆盖构建解释器。已有 ready runtime 先由 child 完整复核，且不因 build-only Python 不是 3.12 而阻塞；只有缺失目标才先校验 CPython 3.12 x64 后执行原安装器。源码写锁、应用根锁和上传 runtime 锁按固定顺序使用；锁冲突统一 `setup_busy`，其他 child/安装失败固定为 `setup_upload_runtime_failed`，不转发路径或 child 输出。旧 runtime manifest Schema 1、损坏或含非允许内容的部分 runtime 保持原样并失败关闭；空目录和只含允许且散列匹配的固定归档缓存可以续建。当前上传 builder 仍为直接构建而非原子 staging，不能把本入口描述成自动升级或原子替换。ignored validator 已在禁网临时 app-root 覆盖精确路径、默认/覆盖解释器、ready 复用、三层锁、固定失败码、旧/坏/不安全部分目录不变及输出脱敏；截至该次验证，默认实际 runtime 前后均保持 manifest Schema 1，本次没有移动它或触发第三方下载、登录、上传。当前 active runtime 已由顶部 2026-09-10 记录接续并验证为 ready。

用户要求继续中间编辑部分的开发，首批需求为分段、封面制作、自动 AI 翻译和自动 AI 配音，并以轻量架构继续到“输入一个网址后自动完成处理并上传发布”。0.28.0 在 T18 本地编辑基础上加入可离线构建的 CPython AI runtime、标准库 OpenAI provider、持久化听写/翻译任务、审核时间轴、标准音色配音和独立 Workflow Schema；发布后当前代码已迁移到 Workflow Schema 3。`/workflows` 已把下载、编辑、AI 与所选 Bilibili/抖音/视频号上传草稿串接起来，支持预先授权或逐节点确认、最多 10 个有序输出、原始未派发 queued 工作的预授权重启续跑、AI 后继重试和最多 30 个任务的一次批量确认。保存一次完整参数预设后，后续同配置运行可只更换 URL；来源标题与逐账号最终标题会在下载 ready 时冻结，不会在重启后漂移。包内文件不能嵌入自身最终提交和制品摘要；0.28.0 是否完成最终冻结，必须查看同批包外 release receipt 是否绑定新的 clean commit、product identity、五件制品和独立验收结果：

`0592b6f96c8eef60381b31b1e78e5ebf6d7c6a1d` 的 0.28.0 五件制品已由 ignored 的 `validation/local/release-v028-0592b6f-final/release-receipt.json` 绑定并通过独立源码/wheel 验收；这份 receipt 只证明该冻结提交。当前发布后开发进一步让 workflow 区分“平台接收投稿”“平台保存草稿”和合法的混合结果，远端结果不确定时仍优先停止核对；上传账号失效会标记账号并撤回同账号尚未执行的确认；浏览器幂等键只在响应丢失重试期间复用，成功后同参数可重新运行；后台 reconciliation 在无进展时有界退避。证据见 [自动流程正确性记录](validation/iteration-0.28.0-post-release-automation-correctness.md)。

随后源码把每项 AI 同意冻结为可摘要的 authorization，绑定 runtime ID/version、协议版本、manifest SHA-256、provider kind、model ID 与本地声明 revision、operation、精确 data egress 和有效硬上限。AI task request、配音 recipe 与 Workflow profile 都持久化该绑定，并在创建、确认、worker 领取及 provider 调用前执行摘要/定义 CAS；旧记录仍可读取，但缺少绑定或当前 runtime 已变化时必须按当前能力重建，不能沿用旧确认。当前硬上限为听写 30 分钟且 25 MiB、翻译 1000 cues/60000 输入字符/20 个按 50 cues 估算的调用单位、TTS 600 cues/60000 输入字符/600 次调用。它们只限制一次授权可发送的输入和调用数量，不是价格估算或 usage ledger。证据见 [AI 精确授权与输入硬预算记录](validation/iteration-0.28.0-post-release-ai-authorization.md)。

当前源码已把编辑库迁移到 Schema 4，并为远程听写、翻译和逐 cue 配音加入不保存正文、密钥、本机路径、endpoint 或 provider 响应的 `ai_invocations` 账本。每项调用在远程边界前先 `reserved`，真正交给隔离 provider 前变为 `dispatched`；只有已验证响应才成为 `responded`，发送前失败为 `released`，发送后无法确认结果则成为 `unknown`。`unknown` 只能带 revision CAS 和明确确认，在 `not_accepted`、`accepted_without_result`、`abandoned` 三个固定结论中人工 reconciliation。`reserved`、`dispatched`、`unknown` 阻止 owner 完成和整条 retry lineage 重试；已核对为 `accepted_without_result` 或 `abandoned` 仍阻止重试，只有 `responded`、`released` 或 `reconciled/not_accepted` 可在原有 owner 状态与再次确认规则下进入重试。编辑页和自动流程页会把祖先阻断传播到现有后继；核对为 `not_accepted` 后显式推进才恢复原有重试入口。翻译 `request_units=ceil(cues/50)` 只是本地 runtime envelope 与硬预算估算，不能当作精确 HTTP 请求数、token/价格或计费收据；`health` 能力检查不写入该账本，也不能证明真实模型可用。证据与当前验证边界见 [Schema 4 远程调用账本记录](validation/iteration-0.28.0-ai-invocation-ledger.md)。

| 工作包 | 截至 2026-09-10 状态 | 仍未完成的边界 |
| --- | --- | --- |
| T14 必要切片 | 已实现本地账号断开与墓碑、排队确认撤回、迟到登录/重启凭据 fencing、媒体占用与状态、活动引用删除保护、两步删除及同大小/同 SHA-256 恢复 | hash 去重、总配额、自动孤儿清理仍是后续；没有修改用户现有账号、媒体或 runtime |
| T07 停机备份/恢复 | 上传备份格式 2 / Upload Schema 3 保存受管封面、定时值和平台参数；格式 1 / Schema 2 只读输入经 staging 迁移，并撤回需要复核的旧确认 | 仅是本机 synthetic/offline 工程范围；真实容量、异机/offsite、NAS 与人工灾备演练未做 |
| T08 有界韧性切片 | 已覆盖三平台多账号严格串行、300 轮/1500 次本地读取的资源预算、媒体复制中断清理，并重复运行 | 执行计划中的更广数据库/浏览器/进程树故障矩阵仍按后续风险决定补充；没有远端调用 |
| T09 无凭据 CI | Windows/Linux × CPython 3.12.10/3.13.14 托管四格已运行；四格的 checkout、解释器、commit scope、固定 uv、CI definition、locked dev env、依赖与 Python/pytest 身份门禁均通过 | 四格完整 pytest 均失败，run 总结论为 `failure`；本机对照为 173 failed、2277 passed、8 skipped；required checks / branch protection **NOT CONFIGURED** |
| T10、T16 与当前方向 C | 0.24.4 的 T10 与 0.25.0 的 T16/G6 保留各自历史；当前“编辑式玻璃”共享主题、响应式和无障碍规范已用于下载/编辑/上传/自动流程页面，并完成当前四页 Chromium 44/44 复验 | 0.28.0 最终冻结只由包外 receipt 判定；没有有效 receipt 时须完成 clean commit、冻结全量、五个制品和源码/wheel 独立安装 |
| T17 投稿参数 | 0.26.0 本地 G7 保留为历史；当前 `/uploads` 与 `/workflows` 均可设置 Bilibili/抖音/视频号的标题、简介、标签、受管/生成封面、发布时间和平台字段；Workflow 同平台多账号可保留 preset 差异或明确统一面板 | Workflow 同一平台仍以一个可显式统一的面板编辑，不提供每个账号并排表单；三平台真实登录、扫码、上传、定时触发及平台后台接受结果均 **NOT RUN** |
| T18 编辑工作台 | 独立 `data-edits` 已 forward-migrate 到 Schema 4；下载来源复核复制；版本化草稿与绑定时间轴的不可变计划；0 段完整 H.264/AAC MP4、显式分段、PNG 封面、确认、取消和重试 | 未做编辑备份/恢复、真实用户长片/大文件矩阵或最终发行制品；下载原件不会被编辑域覆盖 |
| T19 / T20 AI 与自动流程 | `data-ai-runtime` 离线 builder、源码 Setup 可选 AI/上传 runtime、时间轴审核、segment-local 字幕/配音与可恢复 workflow 已接线；Workflow Schema 3 与生产页面保存/配置最多 10 个有序输出，在下载 ready 时冻结来源标题派生的公共及逐账号最终投稿参数，并向最多 3 个账号建立不超过 30 个上传草稿；逐段失败可幂等恢复，完整批次只确认一次；原始预授权 queued AI/render/upload 可在重启后重新校验并续跑，手动、retry、running 与 unknown 仍停下；服务端在保存 workflow 与建立下载前复核下载 Worker、FFmpeg、三项 AI authorization/音色、上传 runtime/调度器及账号绑定；发布后还收敛真实 upload outcome、固定硬上限及 Schema 4 脱敏调用账本；远程 unknown 必须人工 reconciliation，完成与重试按账本失败关闭；普通 Start 已实现仅在操作者显式配置时由 control child 继承精确的 OpenAI 密钥变量，本轮未配置密钥；当前实际应用根的 AI 与上传 runtime 已构建/验证，上传数据库已在私有停机备份后从 Schema 1 迁移到 Schema 3 并保留原记录 | 多分段 fan-out、来源标题冻结与执行预检完成 synthetic/offline 适配、迁移、恢复和浏览器验证；重启续跑完成策略/Manager/域恢复及真实上传域 graceful reopen，尚无 abrupt power-loss、浏览器重启链或真实三域网络 E2E。当前 AI provider 因未配置密钥保持 `provider_health_required`；真实 OpenAI、真人试听、三平台真实登录/发布及当前发行制品仍未执行 |
| 仓库测试策略 | 127 个既有回归冻结供本地与 CI 使用；源码发行清单不再携带 `tests/`，Git ignore、提交检查脚本及本地 pre-commit hook 阻止今后新增或修改测试文件进入提交 | 新 clone 须执行 `git config core.hooksPath .githooks`；历史回归结果仍只证明对应源码，临时验证材料必须留在 ignored `validation/local/` |
| T11 / T12 / T15 | **NOT RUN** | 三平台真实上传、当前六平台下载与 Linux/Docker/NAS 必须绑定 0.28.0 最终 receipt 后的同一构建分别执行 |

0.28.0 变更后的最终全量、定向回归、真实本地媒体 smoke、浏览器检查和发行构建结果须在当前源码冻结后重新执行，并写入本轮证据或包外 receipt；不得沿用 0.27.0 的数字。即使这些本地检查通过，也只构成 synthetic/offline 工程证据，不改变真实 OpenAI、真实平台与最终发行状态。

上传数据一致性使用上传根旁的 `.<root-name>.activity.lock`：当前应用 lifespan、运行中的 active/standby `UploadService` 及短事务持共享锁；上传备份在源根、恢复在目标根持排他锁至完成。创建上传备份前仍须正常停止使用该上传根的**所有**应用和 standby 实例。这个新锁只能协调采用该合同的当前代码；旧版本应用、自写脚本或手工 SQLite/file writer 不受其完整协调，必须由操作者另行停止。下载与上传各有独立备份格式，任一命令成功都不代表另一域已经备份。

当前 0.28.0 本地范围见[当前应用根运行时刷新记录](validation/iteration-0.28.0-local-runtime-refresh.md)、[上传 attention 恢复记录](validation/iteration-0.28.0-workflow-upload-attention-recovery.md)、[译文精确修订绑定记录](validation/iteration-0.28.0-translation-revision-binding.md)、[编辑式玻璃四页生产前端证据](validation/iteration-0.28.0-editorial-glass-frontend.md)、[自动流程服务端预检记录](validation/iteration-0.28.0-workflow-server-preflight.md)、[多分段自动流程记录](validation/iteration-0.28.0-multisegment-workflow.md)、[生产多分段界面记录](validation/iteration-0.28.0-workflow-multisegment-ui.md)、[运行就绪度界面记录](validation/iteration-0.28.0-workflow-readiness-ui.md)、[AI 与自动流程记录](validation/iteration-0.28.0-ai-workflow-evidence.md)、[自动流程正确性记录](validation/iteration-0.28.0-post-release-automation-correctness.md)、[AI 精确授权与输入硬预算记录](validation/iteration-0.28.0-post-release-ai-authorization.md)、[Schema 4 远程调用账本记录](validation/iteration-0.28.0-ai-invocation-ledger.md)、[AI runtime 指南](docs/AI_RUNTIME.md)与[编辑指南](docs/EDITOR.md)；冻结全量、最终 commit、制品 identity/hash 与独立安装结果只记录在同批包外 receipt。此前 [0.27.0 编辑工作台记录](validation/iteration-0.27.0-editing-workspace-evidence.md)及更早记录只保留各自历史，不能证明 0.28.0。本轮未执行真实登录、扫码、OpenAI 调用、真实下载或上传。

[0.24.3 最终源码审查](validation/iteration-0.24.3-final-review.md)与[此前八项修复记录](validation/iteration-0.24.3-debug-fixes.md)保留各自冻结/候选范围，不能借给当前工作树。旧上传 Schema 1 先按精确结构迁移为 Schema 2，再迁移到 Schema 3；旧标签会规范化，抖音/视频号旧版上游隐式 AI 参数会显式保存，受影响的活动任务必须重新核对，原 running 结果仍保持 unknown。未知、损坏或更高版本失败关闭。旧 runtime Schema 1 是另一套运行时 manifest 概念，保留的 legacy runtime 仍按[升级说明](docs/UPLOAD_RUNTIME.md#从旧运行时升级)处理，不能修改 manifest 伪造通过；当前 active upload runtime 已验证为 ready。

### 此前设计准备与 0.24.2 历史

用户在完整 Debug 核验后补充：最后对整个前端进行 Apple 风格升级，提升排版/字体并加入适量动效，后续开发沿用同一设计。已建立[设计规范](docs/DESIGN_SYSTEM.md)、[合成视觉预览](docs/design-preview.html)与根目录 [AGENTS.md](AGENTS.md)；新增 T16 最终阶段，当前计划共 **16 个工作包**。规范先用于新增控件，生产页面整体迁移在功能修复与验收收尾后实施；当时未改生产前端，也未执行真实上传或新的推送。

设计准备验收：静态预览的下载/上传两片段在 1440/1024/768/390/320 宽度均无页面横向溢出，主要控件高度至少 44px；实际浏览器验证主题、键盘切换、标题与摘要同步、局部反馈、details、减少动态/透明度、高对比设置及无 JavaScript 双片段可读，控制台 warning/error 为 0。12 组规范文字配色的计算对比度均 ≥4.5:1，JS 语法与文档清单/本地链接检查通过。证据保留在 ignored `validation/local/design-system-20260905/`；这些是当时只针对规范和合成预览的历史结果。0.25.0 已完成生产两页的 T16 本地 G6，当前边界与最终冻结状态见本次交接入口和对应证据。

此前完整 debug 核验受测基线 `f12749f1dc8d0a2ebbb805669004e69ce33666a7`。详见[本轮核验报告](validation/full-debug-20260905.md)和[后续执行计划](docs/FOLLOW_UP_EXECUTION_PLAN.md)。全量 **1867 passed、8 skipped**，当前精确源码 ZIP/wheel 独立安装、普通 Start/stop、离线浏览器和下载备份恢复通过；另复现上传最终写入失败导致调度退出、未登记运行时代码仍被 ready 接受、原件同尺寸改写、非视频登记导入及 200 项容量边界问题。**当时这些新发现尚未修复；本轮进展见上方 0.24.3 记录。** 当时计划从 T01/T02 开始；下方上轮已修复项目与制品记录保持历史范围。

本轮完成目标是上传器开发、下载成品到上传草稿集成、再次调试，并推送代码和测试说明；真实平台上传由用户安排外部测试。首批验收平台固定为 **Bilibili、抖音、视频号**，其他平台后续维护。Git 交付分支为 `codex/uploader-first-platforms`。测试员直接阅读 [快速开始、测试计划与回报模板](docs/UPLOADER_TEST_PLAN.md)，最新工程证据见 [0.24.2 集成验收末节](validation/iteration-0.24.2-integration-evidence.md#推送前再次调试2026-09-05)。

推送前再次复现并修复两项交互问题：用户清空视频选择后，轮询和手动刷新保持无选择，不再自动换成第一项；重复重建返回已有后继时，页面按后继实际状态提示并定位该任务，不再误报新草稿创建成功。新增 8 项回归，上传前端组 20 项通过。当时精确包身份为 `0.24.2+build.sha256.a15efcc694d4123724e5c0e9983cc16235a704dc9d9be576a315ff926c8dc3f6`；此前 `test-candidate-r2` ZIP 保留旧构建身份，该记录仅对应当时分支源码；当前测试按新版固定身份说明。

用户已完成 Bilibili 扫码，账号检查返回 ready；实际本地控件测试完成导入、必填校验、草稿、取消和重建，两个测试任务均取消，未执行真实上传。所发现的草稿详情折叠状态被轮询重置问题在本轮修复。

最新改动将三个平台的二维码统一显示在 `/uploads`：账号备注可省略，点击“添加并扫码登录”即可获取二维码；扫码、确认、成功、取消和过期均在本页显示。Bilibili 使用固定 biliup 同款官方 TV QR 协议；抖音/视频号在独立无头浏览器中打开登录页，仅传回二维码元素截图。二维码不进入 SQLite 或日志，操作临时目录结束清理。真实扫码及投稿仍须用户完成；证据见 [0.24.1 扫码登录](validation/iteration-0.24.1-qr-login-evidence.md)。

用户最新决定：优先开发 **Bilibili、抖音、视频号** 上传；小红书等其他平台后续维护。这一明确请求接续并调整了历史“先完善下载再做上传”的顺序。

新增 `/uploads` 页面及 `/api/v1/uploads/*`。普通 Start 的上传目录为 `%LOCALAPPDATA%\Open-Flame\video-download-control\data-uploads`，与下载 `data` 同级；现有下载备份不包含上传记录或账号。服务在首次访问上传 API 时初始化，下载启动与 `--check` 不自动启用上传、登录账号或提交内容。使用见 [上传指南](docs/UPLOADER.md)；安装与开源依据见 [运行环境](docs/UPLOAD_RUNTIME.md)、[方案调研](docs/OPEN_SOURCE_UPLOADER_REVIEW.md)。

实现包括：独立账号、受管视频与 SHA-256、ready 下载成品导入、本地草稿、多账号分发、逐项确认、视频号草稿/发布、取消和持久化结果；未知结果必须先到平台核对，再显式创建新草稿。重启将 running 标记 unknown、queued 退回 draft；重新登录会撤回同账号尚未执行的旧队列确认。上传不使用下载 Cookie，不修改下载 Schema 11 或原始媒体。

执行层采用固定 social-auto-upload 与 biliup，以及独立 CPython 3.12/浏览器环境。安装检查、模拟测试和实际本地网页操作都不能证明真实平台投稿已成功；登录和真实视频/投稿许可仍须由用户提供。代码与测试说明使用上述开发分支交付，具体远端提交以 Git 为准；本轮未新建 GitHub Release，0.23.0 的制品与旧平台记录保持历史身份。

上一轮上传核心、浏览器操作与已安装运行环境的记录见 [0.24.0 上传开发证据](validation/iteration-0.24.0-upload-evidence.md)。该轮当时指向外部三平台测试；当前已由顶部 0.25.0 T16 与最终冻结入口取代，仍不代为确认真实上传。

### v0.23.0 交付历史入口

普通 Windows x64 使用：完整源码 ZIP 解压后，先运行 `Setup-Open-Flame.cmd`，成功后运行 `Start-Open-Flame.cmd`；需要上传时向同一 Setup 提供 `--upload-runtime`，需要 AI 时提供 `--ai-python-embed-zip ABSOLUTE_ZIP`。需要已安装 64 位 CPython 3.12+，无需 Codex 或 uv，仍不是免 Python EXE。安装、修复、日志位置与故障处理见 [Windows 安装](docs/WINDOWS_SETUP.md) 和 [启动指南](docs/WINDOWS_LAUNCHER.md)。

新增 [明确发行清单](release-files.txt) 与仓库内的 [构建/归档核对](scripts/release.py)、[解压安装验收](scripts/verify_windows_release.py)、[wheel 安装验收](scripts/verify_wheel_release.py)，不再依赖 ignored 历史 verifier。实际源码 ZIP 安装/repeat/Start 检查、wheel 新环境的 13 个依赖和 13 个命令入口均通过。前端已在独立正常应用中实际检查页面渲染、无效输入、刷新恢复批次、空成品列表、日志刷新、匿名模式；通过 PTY Ctrl+C 停止后，应用日志为 normal，三个进程退出、端口释放。未做 Explorer 双击或物理键盘验收。

精确运行包身份仍为 `0.23.0+build.sha256.64a62ca9ccb395d6e559d276feea2dea1fa562da0fb25035ee53838f0eda77cf`。本轮发行工具不修改业务包与四个根启动入口；标准源码包/wheel/便捷 ZIP 从同一清单冻结字节生成。最终摘要只放归档外 `release-manifest.json` / `SHA256SUMS`；不同文档快照的 ZIP 不能混用摘要。详见 [发行用法](docs/RELEASE.md) 与 [本轮验收](validation/iteration-0.23.0-release-evidence.md)。

本机交付候选目录为 ignored `dist/Open-Flame-0.23.0-final-r2/release/`；只交付其中五个文件，不交付构建环境、测试 profile、原始日志或工具二进制。Git 目标是 `HedgehogsGX/Open-Flame` 的 `main`，使用正常 fast-forward push，不覆盖远端历史；具体提交以 `git log` / GitHub 提交页为准。Git push 不等于创建 GitHub Release 或上传这些本机制品。

### v0.23.0 当时的待办（上传开发由上方 0.24.0 接续）

| 优先级 | 工作 | 当前限制与完成条件 |
|---|---|---|
| P1 | 抖音真实登录态下载 | 匿名实测要求登录；由用户提供本地只读 Cookie **配置路径**后，验证成功、过期、失效与重试，不索取聊天中的 Cookie 内容 |
| P1 | 六平台稳定性与下载回归 | 已有不同旧构建的单样本成功记录，不是本次全面证明；用有权使用的样本覆盖短链、重复、并发、取消、冷却和失败恢复，保留版本/构建级证据 |
| P1 | 普通用户界面 | 当前偏工程控制面；任务入口前移、中文状态收敛、Worker 实际健康状态与隔离开关区别、任务列表与下载成品展示，避免将“工具就绪”误解为“平台全部可用” |
| P2 | 免 Python 独立发行 | 嵌入式运行时/打包方案、清洁 Windows 机器安装升级卸载、双击/键盘生命周期、签名与第三方源码/NOTICE义务；现有源码安装不是这些项目的验收 |
| P2 | 多媒体内容类型 | Bilibili 多 P/合集、Douyin 图集/合集、Instagram 帖子/轮播/Story/Live 尚未实现；X graph-v2 真实 exact-selector 仍关闭，先验证上游选择契约再启用 |
| P2 | Linux / Docker / NAS | 需要真实 Linux/POSIX/root/getfacl 环境；目前 8 个相关测试跳过，不能以 Windows 结果代替网络隔离与容器验收 |
| 后续 | 媒体编辑 | 用户要求先完善下载；剪切/转码/字幕处理应派生新文件，保留下载原件与来源信息 |
| 后续 | 上传 / 发布 | 尚未实现；先选目标平台并核对官方 API/OAuth、配额、幂等与失败重试，再开发，不复用下载 Cookie 假定上传授权 |

继续开发前先读本节、对应运行指南和相关测试；不要重复建设安装器、日志系统或历史 verifier。不要将本机媒体、数据库、凭据、诊断和工具缓存加入 Git。源码 Apache-2.0 / Copyright NOTICE 保持不变，第三方各自许可与离线 bundle 再分发检查仍独立。

## Iteration 0.23.0 源码安装阶段历史摘要

新增 [Setup-Open-Flame.cmd](Setup-Open-Flame.cmd) 与 stdlib bootstrap。已安装 Windows x64 CPython 3.12+ 时，无需 uv/Codex 即可准备 `.venv` 和锁定媒体工具；已有健康环境复用，未知损坏环境不改写，自建环境可重试/`--repair`，安装写锁与源码启动共享读锁互斥，6 个固定安装诊断码落盘。使用说明见 [首次安装与修复](docs/WINDOWS_SETUP.md)。它仍不是免 Python EXE。

真实安装揭露旧 FFmpeg daily URL 连续 HTTP 404；已切换到 BtbN 官方月末保留构建 `n9.0.1-11-ge47273f4d9-20260831`。API digest、官方 checksum 与本地 ZIP hash 一致，9 个 managed 文件、实际版本/configuration、LGPL 条件与离线音视频 smoke 已验证。旧本机工具保留在 ignored `runtime-tools/retained-windows-x64-20260820-v023`；新 canonical 工具由当前精确锁重新安装，未删除旧版本。

冻结构建 `0.23.0+build.sha256.64a62ca9ccb395d6e559d276feea2dea1fa562da0fb25035ee53838f0eda77cf`：真实隐藏 CMD 从无环境/工具的独立源码目录、外部中文/空格/`!` CWD，通过系统 Python **3.13.14** 完成默认联网安装 **35.437s**；重复安装 **6.172s**，环境与工具内容不变；空 wheelhouse 修复失败正确落盘，再联网修复 **27.938s** 成功；实际 Start `--check` **5.734s**、端口释放。相同新环境的正常应用 **6.047s** 完成 ready/health/HTML/一次浏览器打开请求与正常停机，三个进程同 run、无残留。浏览器 opener 被拦截、停止为 test-only SIGINT 桥，不冒充实际 GUI 渲染或键盘验收。全量 **1532 passed, 8 skipped in 185.44s**。详细边界及 4 个包外入口 hash 见 [Iteration 0.23.0](validation/iteration-0.23.0-source-setup-evidence.md)。

最终收尾：258 文件静态隐私检查未发现真实敏感数据，6 个忽略规则探针通过；41 份 Markdown / 148 本地链接、compileall、离线 24 总包锁及 whitespace 检查通过。13 个 runtime 外部依赖、23 个全部外部 Python 包版本未变；LICENSE / NOTICE / Python 许可材料保持原样，FFmpeg 第三方声明已同步。静态检查不是未知秘密完全排除或法律批准。

该安装阶段当时的下一入口是独立发行工具与解压验收，现已由上方交付阶段接续。其当时尚未打包或 push；历史单样本和旧包 identity 不能作为当前整体验收。

## Iteration 0.22.0 历史摘要

新增根目录 [Start-Open-Flame.cmd](Start-Open-Flame.cmd)，使用项目本地 Python 环境启动一体化应用；支持从其他工作目录运行、明确工具参数覆盖和启动后自动请求打开浏览器。参数错误、端口占用、缺工具/依赖等现在可保存到独立诊断目录；启动阶段取消及英文终端编码问题已修复。它仍是源码启动器，不是免 Python EXE。使用说明见 [Windows 启动器](docs/WINDOWS_LAUNCHER.md)。

最终验证：`1492 passed, 8 skipped in 142.88s`。真实 CMD 入口测试 **6/6 通过**；精确构建 `0.22.0+build.sha256.a2e442135f50f4777c952eeb6976b44b97d16edf29c2bed3aa194427237863da` 的正常模式验证在 5.546 秒内完成 ready、健康 API、HTML、一次浏览器打开请求和正常停止，三个进程退出、无残留、端口释放。浏览器请求被测试钩子拦截，停止用 test-only SIGINT 桥，不能声称已做 Explorer 双击、实际浏览器渲染或键盘 Ctrl+C 验收。本轮没有重新下载媒体、添加依赖或生成发布包。详细证据与两个包外入口 SHA-256 见 [Iteration 0.22.0](validation/iteration-0.22.0-launcher-diagnostics-evidence.md)。

文档与发布面收尾：248 文件静态隐私复核未发现本轮阻断项；39 份 Markdown、133 个本地链接通过，编译、离线 24 包锁与 whitespace 检查通过。Apache-2.0、精确 NOTICE 与外部依赖未改；扫描不保证排除所有未知形式的秘密。

该轮的源码首次安装/修复入口已由上方 0.23.0 接续。原平台和分发缺口继续保留，不能把旧工具或旧发布包记录改标为当前构建。

## Iteration 0.21.0 历史摘要

本轮继续实际一体化应用测试：Douyin 匿名要求登录；Instagram Reel 下载、hash/API/ffprobe/完整解码通过。TikTok 最初有非稳定解析失败，随后正常下载阶段稳定暴露 `.image` 后缀 JPEG 封面被拒绝的问题；以固定 yt-dlp 原生 `image>png` 封面转换修复，不放宽输出白名单，不转码视频原件。CLI 早期错误改为显式端口/工具目录/Cookie 配置分类，输出中文处理建议与日志状态；未知运行错误不误标启动阶段。精确构建、基线失败与修复后验证分别记录于 [Iteration 0.21.0](validation/iteration-0.21.0-platform-startup-evidence.md)。

最终验证：`1447 passed, 8 skipped in 153.95s`。固定工具合成封面回归通过；最终构建 `0.21.0+build.sha256.4d089ef9b8fc44bc162dcc7cc3540f77340d3f414a1a9801e93087e3a0cd4a5d` 的 TikTok + Instagram 新下载 **2/2 ready、各第 1 次成功**，两份 hash/API/ffprobe/完整解码与停机后 strict `-xerror` 原件/封面检查通过。三个应用进程正常退出，无残留、端口释放。239 文件发布面复查、37 份 Markdown 链接与编译/离线锁/whitespace 检查通过；没有新依赖、真实 Cookie、发布包或 push。

该轮下一入口是源码级启动器与独立持久化启动诊断；现已由上方 0.22.0 接续。该轮平台下载结果仅属于 0.21.0 精确构建。

## Iteration 0.20.0 历史摘要

Iteration 0.20.0 修复重复输入无法读取原 owner 成品、混合活跃批次隐藏 ready 文件的问题，并增加手动“刷新成品”。新任务、凭证、资产和 Schema 不因复用而改写。第一轮全量 `1433 passed, 8 skipped in 142.01s`；真实 Windows 一体化应用、固定工具、匿名且无额外 JS runtime 的两个样本均 ready：YouTube 第一次、Bilibili 第一次 rate_limited 后由产品自身冷却/重试成功。主验收检查两份 DB/manifest/API 的 size/SHA-256、ffprobe 与完整解码，三进程同 run、正常停止、无残留与端口释放均通过。该下载记录绑定当时精确构建；随后独立审查新增了慢响应同批次 assets 请求竞争的回归与修复，最终构建将用已有媒体重启/复用验收，不冒称再次平台下载。详见 [Iteration 0.20.0](validation/iteration-0.20.0-asset-availability-live-evidence.md)。

最终补充：慢响应单飞修复后完整回归为 `1436 passed, 8 skipped in 146.28s`；最终构建重启既有目录、提交两个 duplicate 输入、两份成品 hash/API/ffprobe/完整解码验收通过，`new_jobs/new_attempts/new_media_assets=0`，原记录不变，三进程正常退出、claim gate stopped、端口释放。最终复用报告完整记录当前 product identity；早期真实下载报告只存源码未漂移布尔值，不能用于 Stage 0 精确 build 审批，亦不能改标成最终 UI 构建的新下载成绩。

下一入口：按用户原定顺序继续 Douyin、TikTok、Instagram 的实际样本验收；需要真实登录态时由用户通过本地只读配置提供，不读取浏览器秘密或绕过访问控制。另一个已发现但未实现的可用性缺口是启动早期的端口占用/工具缺失/配置错误仍统一显示 local_app_failed，应补可操作的安全错误分类。发布包与 push 要单独完成当前源码/隐私/许可检查，不能沿用 0.19 包的 hash。

以下 0.19.0 及更早内容保留为历史；不代表当前源码已经重新打包。

Iteration 0.19.0 接通普通 Windows 一体化入口：明确 `--allow-direct-network` 时，控制面使用复用既有 numeric-IP/TLS/peer/逐跳校验的短链 transport；JSON、TXT、CSV 均在线程池展开，避免阻塞健康检查和取消。Cookie source JSON v2 的 `default_cookie_platforms` 明确选择本次启动默认平台；正常启动注册或复用可用 profile，`--check` 不注册或修改 profile。新任务与显式重试在同一 SQLite 事务中完成绑定，网页可选匿名；v1 和没有配置的平台保持匿名，重试无请求体保持旧绑定。配置失效拒绝默认模式，不静默降级；重复输入不改写现有任务。完整说明与证据见 [0.19 接入证据](validation/iteration-0.19.0-short-links-cookie-defaults-evidence.md) 和 7.9。本轮仅 synthetic/offline 与独立本地页面测试，没有真实平台、真实 Cookie 或新发布结论。

上一轮 0.18.0 历史总结（不作为 0.19.0 发布证明）：

Iteration 0.18.0 修复了“SQLite 允许总活动 2，但实际 Windows 入口串行执行”的产品缺口：一体化应用和独立 `local-worker --drain` / `--poll-interval-seconds` 现在使用同一双槽调度器，跨平台任务可重叠，空槽可在另一个任务未结束时立即补位，单次模式仍只执行一项。任务完成最后的文件清理前保留本地槽位和 job 排除；有本地在途工作时不运行目录、asset-intent 或过期 lease 回收。暂停写库失败锁存在当前 Worker，Ctrl+C/异常通过共享 subprocess stop 信号停止所属工具。实际入口先 red 后 green；heartbeat 与 reader 线程启动失败清理已修复，最终全量 `1307 passed, 8 skipped in 118.43s`、静态门禁与独立 spawned app `--check` 通过。0.18 source-equivalent package 预检已通过，文档冻结后的精确制品须匹配同一验收契约并由包外最终报告绑定。见 [0.18 并发证据](validation/iteration-0.18.0-concurrent-worker-evidence.md) 与本文件 7.8。没有新增真实平台请求、真实 Cookie 或浏览器验收；六平台仍为 `candidate`。

上一轮 0.17.0 历史总结（保留，不作为 0.18.0 发布证明）：

Iteration 0.17.0 在保留 0.16 的 Schema 11 stop/claim 线性化、显式新代重试与 cooldown/manual reset 契约之上，为真实 yt-dlp 下载加入固定脱敏 stdout 控制协议和有界完整行观察器。字幕等 sidecar 不计入主媒体进度；顺序视频/音频与 indexed fragment 采用最多两个 transfer slot 的保守聚合，未知总量只续租不虚构百分比，进入 `postprocessing` 后阶段保持单调，输出映射、文件检查、验证与发布成功后才到 `ready=100%`。API/Web 显示持久化中文阶段、估算百分比、原生进度条与 ARIA。定向回归、独立 loopback synthetic 浏览器 QA、最终全量、静态门禁及 0.17 project-only source-equivalent package 已完成；没有新增真实平台、真实 Cookie、Stage 0、Linux/Docker、安装器或第三方再分发结论。

## 1. 权威规格与边界

- 规划文档：私有本机文件 `视频下载项目选型与整体架构规划.md`（不纳入仓库）
- 文档日期：2026-09-02
- 读取时 SHA-256：已在本机核对；为避免公开私有文档指纹，此处省略

原文是选型/架构决策稿，不是已验证安装手册。代码、fake adapter、静态 Compose、Windows 测试或未执行 runner 都不等于真实平台支持。不得自行抓取随机/未授权媒体；Stage 0 只能使用用户明确提供或确认有权使用的样本。

## 2. 当前决策基线

| 决策 | 当前值 | 证据状态 |
|---|---|---|
| 产品 | 单机/NAS、单管理员、私有自托管 | FastAPI 已实现；Windows 本机可独立使用；无认证且强制 loopback |
| 批量/并发 | 每批 1–50；单 Worker 进程总执行槽 2；单平台活动 Job 1 | 0.18 Windows app 与 standalone drain/poll 已接入真实调度循环，并由无网络屏障回归证明跨平台重叠与连续补位；SQLite claim 事务仍强制上限；历史真实样本不能证明本轮并发 |
| 数据 | 下载 Schema 11；独立编辑 Schema 4；独立上传 Schema 3；独立 Workflow Schema 3 | 下载继续保留旧 flat-v1/graph、Schema 10 治理和 run-scoped claim gate；编辑域保存导入源、版本化草稿、AI task/timeline、render plan、脱敏远程调用账本与成品；上传旧 Schema 1/2 精确迁移到 Schema 3，Workflow Schema 1/2 精确迁移到 Schema 3，旧行的来源标题快照保持空。当前实际上传库已在停机 SQLite 备份后完成 Schema 1→3 迁移并保留既有账号和 canceled job，迁移前后 `quick_check=ok` 且外键错误为 0。四个数据库、Cookie 与各媒体根不混用，未知结构失败关闭 |
| 内核 | yt-dlp + FFmpeg/ffprobe 候选 | Windows x64 固定工具已安装、逐文件校验并通过离线 smoke；0.17 固定脱敏 progress/phase 控制协议只在 download 启用，真实平台历史样本不等于本轮进度验收或平台整体验证 |
| 短链 | 逐跳 DNS/numeric TLS/peer 校验 | 0.19 Windows local-app 的直连明确确认同时接通控制面短链；普通 control 仍默认关闭，只支持既有 POSIX UDS 配置；不监听新端口，不声称隔离 |
| 凭证 | 本次运行显式平台默认与匿名模式；网页不编辑秘密 | 0.19 config v2、事务内新任务/重试绑定、API平台可用性及UI已接通；v1不自动默认；仅 synthetic source 已验证，真实 Cookie 未挂载 |
| 资产 | 不可变下载原件 + 独立编辑源/成品 + 独立上传媒体 | ready 下载列表含 thumbnail/caption DTO；原件、辅助产物和编辑成品分别由严格端点重验；编辑输出须显式复制进上传域，API 不暴露本机路径或用户标题 |
| 前端/API | 下载、编辑、自动流程及 Bilibili/抖音/视频号上传四页 | 0.28.0 四页共用“编辑式玻璃”语义 token、选择性玻璃组件、系统/浅/深主题、本地静态资源、响应式与无障碍降级；当前 Chromium 44/44 通过；编辑/自动流程页显示 provider、模型、标准音色、数据外发和费用边界，云调用与配音计划分别要求明确确认；当前 AI runtime 完整性已 verified，provider health 因无密钥仍为 unverified，任何缺失的 runtime、凭据或授权继续保持 blocked |
| 运行日志 | supervisor/控制面/Worker allowlist JSONL + 近期事件 API/UI；0.22 独立启动故障日志 | 三进程共享同一 `run_id`；业务日志不写原始 stdout/stderr、URL、source ID、标题、文件名、argv、Cookie 或本机路径；独立诊断仅记录固定代码和上下文，256 KiB × 3 备份，明确 saved/unavailable，正常关闭持久化不等于断电保证 |
| 部署 | Windows 一体化本机应用；高级手动 Worker；Docker Compose 单机候选 | 本机 supervisor/直连 Worker 已真实验收，但明确不提供网络隔离；当前 packaged 开发宿主因 Windows filesystem virtualization 需让 Setup 与 Start 使用同一经核对的 canonical `--app-root`，这不是普通 Windows 启动的一般要求；Linux 隔离 Worker/Compose 未 build/cold-start/full acceptance |
| 备份 | 下载与上传使用两套独立格式；编辑与 Workflow 域尚无备份/恢复；秘密不混入 | `video-download-backup` 保存下载 Schema 11/已发布资产；`video-upload-backup` 以格式 2 保存 Upload Schema 3、登记 present 媒体及非秘密关系。上传 create/restore 使用 sibling shared/exclusive activity lock并要求所有 active/standby 实例停机；旧版本/手工 writer 仍需人工停止。Editing Schema 4、Workflow Schema 3、导入源和编辑成品当前不在这两套备份中 |
| Stage 0 | CSV v3；七字段精确 identity | `product_version` 为版本 + 完整包载荷 hash；规范 source identity 防别名充样本；最新 partial run fail closed；报告 aggregate-only，导入只追加 evidence，approve/revoke 另走 revision CAS |
| 许可证 | 项目自有材料 `Apache-2.0`；`NOTICE` 为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu` | 0.28.0 当前源码继续执行 source-equivalence、metadata、法律文件、依赖、build identity 与隐私门禁；最终重建须匹配冻结契约，精确 archive hash 只在包外报告；旧包记录保留为历史，第三方 binary/container/tool bundle 与 AI 模型再分发仍 blocked |

## 3. 累计交付

### 3.1 前六轮保留能力

- 六平台窄范围 URL 白名单/规范化/去重、Batch/API、持久化队列、Attempt/lease/retry/cancel、queue/circuit 和脱敏 metrics。当前新增范围为 TikTok 单视频、Instagram Reel，Bilibili 只接受默认分 P。
- 不可变资产、sidecar、SHA-256 manifest、commit intent、三阶段 crash recovery 和 Schema 8 备份/独立根恢复审计。
- X graph-v2 parent discovery、snapshot、exact child target、active generation、partial success、Input cancel/rediscover 和 ready-asset reuse。只有 `ScriptedGraphFakeAdapter` 离线 exact-selector=true；真实 `YtDlpAdapter.supports_exact_selector=False`。
- admin-only CredentialProfile CLI 只存 bounded opaque ref，claim 时检查平台/禁用/过期；公开 Batch API 拒绝 credential 字段。

历史证据：[Iteration 0.5 recovery](validation/backup-restore-drill-iteration-0.5.md) 与 [Iteration 0.6 graph-v2](validation/iteration-0.6-graph-v2-offline-evidence.md)。

### 3.2 Iteration 0.7：short-link 路径

- 新增默认关闭的 `video-download-short-link-egress`。控制面只在 POSIX、显式 gate、私有 UDS/key 同时有效时注入 resolver。
- 协议使用 bounded canonical JSON、domain-separated HMAC-SHA256、256-bit nonce、freshness/clock-skew、request-hash binding 和 crash-durable replay marker。
- 控制侧每跳只解析一次 DNS，拒绝非公网/混合答案；egress 只连 numeric IP，保留原 host 做 TLS SNI/证书/HTTP `Host`，并对实际 peer 做 attestation。
- egress 不自动 redirect，只返回 bounded `Location`。跨平台、回环、hop/header/body/frame/deadline 超限均 fail closed。
- Batch 对唯一短链共享 15 秒 budget；内建 resolver 收到剩余 budget。持久化前二次验证 result type、canonical URL、platform、source type/ID；异常/恶意结果固定脱敏失败。
- signed final Location、numeric IP、socket/key path、DNS/异常原文不进 API、SQLite、backup manifest 或普通 audit。Replay 磁盘操作从 event loop 卸载到专用有界 executor。

### 3.3 Iteration 0.7：Cookie 边界

- Iteration 0.7 当时的 credential-free base Compose 无 Cookie；显式 [`compose.candidate.cookies.yaml`](deployment/compose.candidate.cookies.yaml) 才把 source root、固定 mapping 和 reviewed wrapper 只读绑定给 Worker。0.10.0 已把当前资产扩到六 key、产品 contract 0–6 source，full acceptance 要求六个 synthetic source。
- host validator 把 mapping 当数据解析，不 source dotenv，不打开/哈希/打印 Cookie；检查 canonical path、owner/group/mode、nlink、base ACL、安全祖先链、唯一 identity/ref、overlap 和 snapshot。
- standalone validator 只经绝对 `/usr/bin/env -i` 传五个值，再以绝对 `/bin/sh` 执行，避免把 `BASH_FUNC_*`/`ENV`/PATH 带入子 shell。
- Worker 通过 no-follow descriptor 重验 source，每次复制到新的 Attempt-private `0600` 文件，核对 identity/大小/时间/fsync。失败清理使用已 attested directory FD，不被 path swap 诱导。
- 轮换契约：stop → clean-env staging validation → 受信绝对 GNU `/usr/bin/mv -fT` → platform-parent fsync → normal validation → `--force-recreate worker`；任一失败保持 Worker stopped。

### 3.4 Iteration 0.7：Linux/Docker acceptance

- [`run-linux-acceptance.sh`](deployment/run-linux-acceptance.sh) 默认 `--preflight`，不执行变更；当前 full mode 额外要求精确授权 token、private env/override、digest-pinned tool image、deny-only policy、六 synthetic Cookie source、Schema 11 backup 和专用空根。
- 必须由 clean trusted root launcher 以绝对路径启动；execute 要求 effective root。runner 要求 `bash -p`、固定 `/usr/bin/python3` 且验证 Python 3.12+，所有 host Python 均用 `-I`。
- Docker endpoint 正确尊重非空 `DOCKER_CONTEXT` 高于 `DOCKER_HOST` 的官方优先级，任何 current/default context 也只能解析到 local Unix Linux daemon。execute 拒绝 inherited `DOCKER_CONTEXT`、`DOCKER_HOST`、`DOCKER_CONFIG`、`DOCKER_CERT_PATH`、`DOCKER_TLS_VERIFY`、`BUILDKIT_HOST`、`BUILDX_BUILDER`、`COMPOSE_FILE`、`COMPOSE_PROJECT_NAME`、`COMPOSE_PROFILES`。
- private env/override 精确 `root:root 0600`；policy 精确 `root:10001 0440`，并检查 nlink/size/ACL/祖先/snapshot。baseline/effective Compose 逐 service 精确比较；除 reviewed Cookie wrapper 和两个 Worker-only read-only bind 外，任何 command、healthcheck、namespace、mount、network/IPAM/name、secret、hook 或非 Worker 变化均拒绝。
- Dockerfile 无外部 syntax image；`wheel_builder`/`runtime` 两个 Python `FROM` 硬编码同一 3.12.13 digest，无 ARG override。`pyproject.toml` 与 `requirements.build.in` 精确固定 `hatchling==1.27.0`，build/runtime lock 均 exact+hashed。唯一 package 联网步骤是 hash-checked、wheel-only `pip download`；后续 install/build 均 `RUN --network=none` + `--no-index`，项目 wheel 关闭 build isolation/deps 并按精确路径安装，runtime 最后 `pip check`。Lock 同时存在 wheel/sdist hashes 不会放行 sdist，`--only-binary=:all:` 会拒绝它。当前 Runner 的 network-none runtime contract 另精确核对 13 个 runtime-lock distributions + project `0.17.0`，并拒绝 hatchling/packaging/pathspec/pluggy/trove-classifiers 五个 build-only distributions 泄漏；target Linux 尚未执行。
- Fresh local build tag 只作初始定位；runner 立即捕获/验证不可变 `sha256:...` image ID，把该 ID 写进 effective Compose，递归拒绝任意 string key/value 中的 `$`，在 env 同目录创建 `root:root 0600` frozen JSON。二次 `compose config` 必须与原 JSON 深等值、重过完整 validator 且 identity/snapshot 不变；之后 Compose mutation、direct runs、checkpoint image inspect 与 runtime container `Image` 校验全部绑定同一 ID，避免 tag rebind。正常退出按 identity 删除 frozen file，crash/identity drift 则保留供人工定点审计。
- full mode 同时要求 non-pipe host `/proc/sys/kernel/core_pattern` 与 `RLIMIT_CORE=(0,0)`，并用 Docker inspect 确认 Cookie bind exact Source/Type/`RW=false`。
- Full-mode 代码路径及静态测试设计覆盖 cold start、health、namespace/UDS、SSRF、peer attestation、SIGTERM/lease recovery、stale socket 和独立 Schema 11 restore。首个 Compose `up` 前已武装 scoped cleanup，因此部分启动后失败也会 stop；脚本无 recursive delete。目标 Linux/Compose execute 尚未发生。

### 3.5 Iteration 0.7.1：前端、回归与许可证

- 修复 Python 三引号字符串把 JavaScript `/\\r?\\n/` 破坏为非法正则的问题；页面不再卡在“正在读取”。新增统一 `fetchJson`，HTTP/非 JSON/网络错误可见，queue/circuit 分区失败不会误报健康。
- 轮询使用 generation + request id 双重 fencing，旧请求不再覆盖新批次或取消新 timer；瞬时失败 5 秒重试。文本与文件必须二选一；resume/reset/cancel 均 `try/catch/finally` 恢复按钮。
- 页面明确区分“队列未暂停”和“Worker 已启动”，并每 10 秒非重叠刷新运行状态。主前端无外部资产；FastAPI `/docs`、`/redoc` 已禁用，保留 `/openapi.json`。
- `BatchRepository` 支持注入 timezone-aware clock；测试夹具不再依赖当天墙钟。相同 `created_at` 的 claim 使用 SQLite `rowid` 作为插入顺序 tie-breaker，修复 UUID 随机排序 flake。
- 当时的根 `LICENSE` 与包元数据为 `LicenseRef-Proprietary`。该 0.7.1 历史构建事实由 0.9.1 的 Apache-2.0 权利人决议取代；`THIRD_PARTY_NOTICES.md` 对 26 个精确 runtime/dev/build 包和 29 份 exact-wheel 法律文件的 SHA-256 约束继续有效。
- candidate tool bundle 必须提供并 hash-bind executable、source artifact、SBOM、人工许可证复核记录和法律文本；artifact/license allowlist、FFmpeg configuration、`--enable-nonfree` 拒绝与实际 version/config execution 均 fail closed。它只验证证据完整性，不替代法律判断。

### 3.6 Iteration 0.7.2：脱敏运行日志

- 新增 stdlib-only JSONL logger；控制面固定写入 `${VDC_DATA_ROOT}/logs/runtime-control.jsonl`，Worker 文件名只使用实例 ID 的短哈希。默认单文件 10 MiB、保留 5 个轮转，可通过 `VDC_RUNTIME_LOG_LEVEL`、`VDC_RUNTIME_LOG_MAX_BYTES`、`VDC_RUNTIME_LOG_BACKUP_COUNT` 调整。
- 每个事件含 UTC 毫秒时间、schema、component、run/event ID、进程/线程 ID 和 run 内单调 `sequence`。事件名、level、字段和关键枚举全部 allowlist；未知异常只收敛为固定类别，日志写入失败不阻断业务并通过状态计数暴露。
- 控制面覆盖启动/停止、模板化 HTTP 路由、批次与控制操作；Worker 覆盖 claim、phase、retry、heartbeat、cleanup、pause 与 terminal result；受控子进程只记录 executable basename、参数个数、时长、返回码和输出字节数。
- `GET /api/v1/operations/logs` 只读取通过完整 schema/语义验证的近期事件；前端“运行日志”区域以 `textContent` 呈现最近 100 条并提供手动刷新。客户端 `X-Request-ID` 不被信任，响应返回服务端新生成的 ID。
- 请求 URL/query/body、批次名、输入文本、argv/env/cwd、绝对路径、stdout/stderr 与异常消息都不进入日志；活动日志/轮转日志也不进入业务备份。日志是 best-effort 排障线索，不替代 SQLite、资产 manifest、备份审计或监控告警。

### 3.7 Iteration 0.8.0：Windows x64 本机工具链

- 新增 `video-download-tools install/verify/smoke/status`，只支持机器可读锁批准的 Windows x64 bundle。目标必须是尚不存在的绝对规范化路径，下载/缓存逐项校验固定大小与 SHA-256，经同级 staging 完成后原子发布；不修改系统 `PATH`。
- yt-dlp 固定为 `2026.08.19` Python zipimport artifact；FFmpeg/ffprobe 固定为 BtbN LGPL shared build `n9.0.1-6-g9d4ca21220-20260820`。真实二进制版本、FFmpeg configuration、完整 managed file set、许可文本和 retained source/checksum/signature evidence 均 fail closed 校验。
- 离线 smoke 使用本机 FFmpeg 生成 synthetic 音视频并由 ffprobe 验证流结构；不访问媒体平台。API/UI 显示 `ready`，同时保留 `isolated_worker_ready=false`、`network_download_enabled=false`、`platform_download_verified=false`。
- candidate Worker 可使用受校验的 yt-dlp zipimport entrypoint，但 Linux network namespace/UDS relay/受控 egress gate 未放宽；字幕语言集合也保持有界。Windows 控制面不会因为工具 ready 自动启动 Worker。
- 最终安全审查补锁了 Windows 盘符/ADS/设备名/尾点空格与 UNC/device namespace 路径逃逸；`redistribution_status` 只能保持当前 blocked 值；HTTP 重定向在下一跳请求发出前逐跳校验 HTTPS/host/userinfo/port，并受单制品 600 秒总 deadline 约束。CLI smoke/status 始终输出全部安全门禁。
- 完整记录见 [Iteration 0.8.0 local toolchain evidence](validation/iteration-0.8.0-local-toolchain-evidence.md)。

### 3.8 Iteration 0.8.1：双样本真实下载、显式 JS runtime 与 UTF-8

- 用户明确提供本轮 YouTube/X 各一个公开测试样本；精确 URL、账号/状态标识与媒体指纹只保留在 gitignored 本机证据中。两者均使用固定 yt-dlp `2026.08.19` 和 FFmpeg/ffprobe `n9.0.1-6-g9d4ca21220-20260820`，未使用 Cookie；完成真实传输、ffprobe、SHA-256 与全流解码。
- YouTube 产物通过 VP9 + Opus WebM 的 ffprobe、SHA-256 和完整解码验证。未显式 JS runtime 时下载可成功但有 yt-dlp warning；显式 Node.js `v24.16.0` 后 A/B probe 仍发现相同格式集合且 stderr 为 0 bytes。
- X 产物通过 H.264 MP4 的 ffprobe、SHA-256 和完整解码验证。该源的候选格式均无音频，现有 verifier 允许合法 silent video；应用规范化后的 canonical status URL 同样探测成功，且状态 ID 与提取媒体 ID 不同，不能合并建模。
- 新增强类型 `YtDlpJsRuntimeName` / `YtDlpJsRuntime`，只接受 `deno`、`node`、`bun`、`quickjs` 和绝对、规范化、普通且非 symlink/reparse 的 executable。candidate CLI 新增可选 `--js-runtime NAME:ABSOLUTE_EXECUTABLE`；未配置时继续 `--no-js-runtimes`，配置时先 clear 再只启用该 runtime，并继续 `--no-remote-components`。
- yt-dlp probe/download 固定增加 `--encoding utf-8`，修复含中文工作区路径的子进程文本乱码；版本命令保持最小参数。此次真实下载的原始日志是接线前的本机证据，UTF-8 修复由命令契约回归覆盖，尚未由完整 candidate Worker 实网重跑。
- 单 URL 测试确认额外使用 `--max-downloads 1` 会在媒体完整下载后返回 `101`；当前 candidate 固定命令没有该参数，继续依赖 `--no-playlist`。不能把这个实验性 `101` 当成候选 Worker 已出现的故障。
- 权威汇总见 [Iteration 0.8.1 live platform evidence](validation/iteration-0.8.1-live-platform-evidence.md)。gitignored 原始证据分别位于 `validation/local/live-platform-20260903/youtube/`（`RUN.md`、probe/download/ffprobe/decode 日志与 WebM）和 `validation/local/live-platform-20260903/x/`（`RESULT.md`、probe/download/ffprobe/decode 日志与 MP4）。
- 准确边界：这只证明上述两个精确 URL 在本机一次性工具路径可下载；未经过前端/API 队列、Worker claim/heartbeat/retry、受控 egress、AssetStore staging/commit intent、manifest/sidecar 或运行日志 E2E，不升级 `platform_download_verified`，也不代表 YouTube/X 的其他内容可用。

### 3.9 Iteration 0.9.0：Windows 本机 Worker、资产访问与双样本全链路

- 新增 `video-download-local-worker`，将既有真实 `YtDlpAdapter`、`FfprobeVerifier`、Worker 编排与 `AssetStore` 接到 Windows 本机控制数据库。入口默认关闭，只在 Windows、`VDC_ENABLE_LOCAL_REAL_WORKER=1`、命令行显式 `--allow-direct-network`、现有 Schema 8 数据库 ready、固定工具链与离线 smoke 全部通过时启动；同一数据根以 Windows byte-range lock 限制为一个本机 Worker。该模式使用宿主机直连网络，不声称具备 Linux namespace/UDS/egress 隔离。
- API 新增/闭合 `GET /api/v1/batches`、`GET /api/v1/batches/{batch_id}/assets` 与 `GET /api/v1/assets/{asset_id}/download`。只列出数据库登记的 ready 原件元数据和下载 URL，不返回本机路径；下载只接受规范 asset UUID 与仍满足数据根/登记 size 边界的原件。前端可刷新/打开最近批次，并在 `ready`/`partial_success` 时显示真实成品下载链接。
- 本机 Worker 复用既有 allowlist JSONL，但组件固定为 `local-worker`；启动、工具检查、claim、phase、受控子进程、terminal result 与停止事件均进入 `${VDC_DATA_ROOT}/logs/`，并可由既有运行日志 API/UI 汇总读取。URL、argv、绝对路径与 stdout/stderr 继续不进入普通日志。
- 最终 v3 双样本 E2E 使用用户指定的 YouTube/X 公开样本；公开记录省略精确 URL 和 batch/job/asset 标识。两个 Job 均由 `local-worker` claim，经过 `probing → downloading → verifying → committing` 并以 `ready` 结束，Batch 最终为 `ready`，得到两个不可变资产及各自 manifest/sidecar/thumbnail。
- 两个资产的原件、缩略图与 API 下载副本均完成 size/SHA-256 对照，且与 DB/manifest 一致；YouTube AV1 + Opus、X H.264 无声视频的完整流解码均 clean。具体媒体指纹与运行时标识仅保留在 gitignored 本机证据中。
- 浏览器从最近批次打开该 Batch，看到 `ready`、`2/2 ready` 与两个下载链接；页面操作、资产列表和实际下载均正常，验收期间浏览器控制台无 error/warn。gitignored 原始状态、资产、API 下载与运行日志位于 `validation/local/local-worker-e2e-20260903-v3/`；可提交汇总见 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md)。
- 准确边界：该 E2E 取代的是 0.8.1 “仅一次性工具调用”的缺口，不升级 `network_download_enabled`、`isolated_worker_ready` 或 `platform_download_verified`。它仍只证明两个精确样本在当前 Windows 本机直连模式可用；X graph-v2 保持关闭，Linux/Docker/Cookie/Stage 0 均未完成。0.9.1 后项目源码已获 Apache-2.0 授权，但第三方 binary/container/tool-bundle 门禁仍未完成。

### 3.10 Iteration 0.10.0：六平台路由修复与 Instagram 单样本真实闭环

- 在六平台静态路由基础上修复四个真实下载路径问题：Worker 现将配置的 `max_height` 传给 Adapter；yt-dlp 格式选择已移除不受高度限制的 `/b` 回退；平台明确要求 fresh cookies 时由 `extractor_broken` 改为 `authentication_required`；当高度策略找不到可用格式时以 `content_unavailable` 终止，不再误计为 extractor breakage 或触发平台熔断。
- 2026-09-03 使用全新 data root，在 Windows `local-worker` direct/non-isolated 模式下，以固定 yt-dlp `2026.08.19`、FFmpeg/ffprobe `n9.0.1`、显式 Node 且不提供 Cookie，对一条 NASA 官方公开 Instagram Reel 完成 `1/1 ready`。
- 数据库 `quick_check`、外键检查和 pending commit intent 均正常；原件、DB、manifest 与 API copy 的 size/hash 相等且具体值不进入公开记录；原件一遍完整流解码 clean，`temporary` 与 staging 无残留。
- 控制端与 `local-worker` 的字段白名单 JSONL 生命周期事件可读取；前端显示 0.10.0、`ready`、`1/1 ready` 和成品下载链接，实际下载可用，验收期间浏览器控制台无 error/warn。
- 同轮探索中，Bilibili 公开电影样本遇 HTTP 412；Douyin 官方宣传样本要求 fresh cookies，因未提供 Cookie 正确终止为 `authentication_required`；TikTok 未实跑。精确 URL/Reel ID、账号内部 ID、运行 UUID、本机绝对路径、媒体指纹、签名 URL、Cookie 和日志原文均不进入公开证据。权威脱敏汇总见 [Iteration 0.10.0 Instagram live evidence](validation/iteration-0.10.0-instagram-live-evidence.md)。这一条正向样本不是 Stage 0，所有能力继续为 `candidate`。

### 3.11 Iteration 0.11.0：Schema 9、Stage 0 CSV v2 与 Bilibili 412

- Schema 9 为 `platform_capabilities` 增加受约束的 `job_kind`，唯一索引纳入完整 route identity；Schema 8 既有行保守迁移为 `download`，readiness 检查列、CHECK 与索引形状。
- Stage 0 样本/结果 CSV v2 都强制 `job_kind`，结果必须匹配样本；`expected_output_count` / `observed_output_count` 按 `download` 已发布完整验证资产与 `discover` 不可变 snapshot 唯一 child/source item 分别解释。旧 media-count 表头不自动兼容。
- 报告按 `platform × source_type × job_kind × adapter × downloader_version × environment` 分格，只生成脱敏 Markdown，不运行下载器、不写能力表、不自动改变公开 API/UI 的 `candidate` 状态。
- Bilibili 固定工具、同一公开样本、无 Cookie 的重复诊断得到 raw `2/2` 成功与产品 fresh attempt `4/6` 成功、`2/6` probe HTTP 412。只将该平台受限 412 标记映射为 `rate_limited`，让现有 Worker 退避/冷却；不添加 Cookie/header/代理绕过，不将间歇成功外推为稳定能力。
- 升级前必须用 0.10.0 对精确 Schema 8 制作并恢复备份；副本迁移成功后，再用 0.11.0 对精确 Schema 9 制作并恢复另一基线。两次演练、应用版本与恢复工具必须分别保留。
- 脱敏边界、Apache-2.0 与第三方 dependency/binary/container/tool-bundle 再分发门禁保持不变。公开证据见 [Iteration 0.11.0 Schema 9 / Bilibili 412](validation/iteration-0.11.0-schema9-bilibili-evidence.md)。

### 3.12 Iteration 0.12.0：辅助产物、TikTok 短链与 MVP 路由离线 E2E

- ready asset DTO 新增 `artifacts`，只列数据库登记的 thumbnail/caption metadata 和 opaque 下载 URL，不返回本机路径或用户标题；UI 只用 DOM API 构造链接。
- `/api/v1/artifacts/{artifact_id}/download` 只服务 ready asset/job 下的 thumbnail/caption，并重验规范 UUID、唯一 parent original、目录/文件名/MIME/language、regular/single-link、identity、size 与 SHA-256；损坏旧 sidecar 逐行跳过，不影响有效原件。已校验内容进入 1 MiB memory-capped spool 后按 64 KiB 分块输出，正常、disconnect/cancel 与 `send` 异常均显式关句柄；响应固定文件名并带精确 `Content-Length`、`private, no-store`、`nosniff`。
- TikTok `vm.tiktok.com` / `vt.tiktok.com` 进入默认关闭的短链 gate。受控 resolver 每跳继续执行 HTTPS、DNS public-address、numeric target 与 peer attestation，只接受五个精确 TikTok hostname，拒绝相似子域和后缀欺骗。
- 隔离临时数据库、fake Worker 与 synthetic bytes 覆盖 YouTube video/Shorts、Bilibili BV/av、受 gate 保护的 Douyin 短链和 TXT/CSV 导入的规范化→队列→ready asset→下载闭环。没有真实媒体请求，也不构成 Stage 0。
- 最终全量回归为 `969 passed, 8 skipped in 63.58s`；隔离 API/UI 显示 v0.12.0 / Schema 9 和 synthetic `1/1 ready`，原件、缩略图、字幕下载均成功，console 无 warning/error。公开证据见 [Iteration 0.12.0 artifact / TikTok evidence](validation/iteration-0.12.0-artifact-tiktok-evidence.md)。

### 3.13 Iteration 0.13.0：Schema 10 capability governance

- Stage 0 CSV v3 在 route identity 中加入 `product_version`，其值必须是 `0.13.0+build.sha256.<完整 64 hex>`；摘要覆盖按相对路径排序、带长度 framing 的 importable package 源码、数据与 sourceless bytecode，只排除可再生成的 `__pycache__/*.pyc`。缓存目录中的其他文件仍计入摘要，包根或内部 link/reparse/special file 会被拒绝，前后两次完整 inventory 还会识别哈希期间的新增、删除或 metadata 漂移。导入和 approve 精确匹配当前构建，并在事务提交前再次复核；history/readiness/revoke 保留旧构建历史。严格校验不重复表头、行宽、安全 token 与 manifest/result 的 `job_kind` 一致性。
- 样本按 `job_kind + platform/source_type/source_id` 去重；TikTok 同一 video ID 的不同 handle、host 或 query 不能充成独立样本，跨 bundle 也得到同一 canonical evidence。Bilibili 同一投稿的 BV/av 双标识暂不做本地转换，因此 Stage 0 只接受 BV，普通下载入口仍接受两者。每一 identity 只看最新三次相关执行，较新的 partial run 不会被旧完整运行掩盖。
- 脱敏报告改为 aggregate-only，不再输出 URL、URL hash、sample ID 或 run ID。导入端重新解析原始 CSV、使用固定 `stage0-v3` 门槛，并计算与行序、换行和规范 URL 别名无关的 canonical evidence digest；相同内容重复导入幂等，但永不自动批准。
- Schema 10 将 Schema 9 可写 `platform_capabilities` 改名为不可写的 `capability_legacy_schema9` 档案；新增不可变 `capability_evidence`、append-only `capability_decisions` 与只读 current `platform_capabilities` view。UPDATE、DELETE 和 `INSERT OR REPLACE` 均被 guard 拒绝，连接启用 recursive triggers；readiness 复核 guard 及 root revoke 等语义。approve/revoke 要求精确 identity、允许的 reason code 和 expected revision；撤销后不能重放同一 evidence。被禁用 route 必须保留 registry tombstone，因此历史批准仍可撤销。
- 启动 readiness 复核精确 1–10 migration history、表/view 类型、列/索引/trigger/FK、evidence identity/hash/policy/static-route，以及 decision revision chain/current view。HTTP probe 的完整审计最多缓存 5 秒，并用 SQLite schema cookie 立即识别 DDL；审计期间 schema 变化或最终 cookie 不可读会 fail closed。管理员、备份与恢复路径不使用该缓存。旧 Schema 9 的 `verified` 值不会复制到新 ledger，也不会变成当前批准。
- 新增本地 `video-download-capabilities` CLI，以及 implementation/evidence/current decision/history 的兼容只读 API；CLI 要求既有 ready Schema 10、绝对规范且单 hard-link 的普通数据库文件，并在每次连接前后重验文件 identity；`list` / `history` 不会偷偷迁移旧库。`capability-snapshot` 在一次 readiness 后用单一 SQLite 读事务返回三层、当前 build identity、总数/截断状态，并补齐 current decision 引用的旧 evidence。前端只在首次打开或手动刷新时读取，按 `identity_key + evidence_id` 精确关联，区分当前/历史构建和被截断的未知状态，仍不提供变更按钮。CSV 或本地管理员不是受信硬件证明，能够伪造输入或同时改写数据库与 digest 的主体仍可伪造结论。
- 本轮只使用 synthetic/offline 数据验证治理路径，不发起真实平台请求、不启用真实 Worker/短链/graph gate，也不在仓库中附带 evidence 数据库或批准记录。最终工程证据见 [Iteration 0.13.0 capability governance evidence](validation/iteration-0.13.0-capability-governance-evidence.md)。

### 3.14 Iteration 0.14.0：Windows 本机 Worker Cookie 接入

- `video-download-local-worker` 新增可重复的 `--cookie-source PLATFORM:OPAQUE_REF=ABSOLUTE_PATH`、有界 `--max-cookie-bytes` 与互斥 `--check`；同一 resolver 现在服务 Windows direct Worker 与 Linux candidate。CLI 配置拒绝非规范相对路径、重复平台以及 source 与 data/tool root 重叠。
- Worker 在任何 claim 前验证全部 source：必须是只读、非空、有界、稳定、普通且非 link/reparse 的单链接文件；不同平台不能通过路径别名复用同一 `(device, inode)`。probe/download 各自获得新的 Attempt `secrets/*.cookies.txt` 副本，完成后删除，原 source 路径/ref/内容不写入业务库、普通日志或 Adapter request。
- `--check` 使用真实 builder 验证现有 Schema 10 数据库、固定工具链、ffprobe 与 Cookie source，然后输出只含平台列表的 JSON；它不调用 `run_once()`。真实构建链回归会对 `download_jobs`、`job_attempts`、`media_assets`、`asset_commit_intents` 做前后快照，并确认没有 secrets 副本。日志使用 `worker.preflight_started/succeeded/failed`，不再伪装为常驻 Worker 已启动。
- synthetic Douyin E2E 覆盖 profile 注册/分配、claim 校验、probe/download 两次独立 Cookie 副本、ready Asset 与 cleanup；Cookie/ref/path/content 泄漏标记均为 0 hit。Bilibili Stage 0 的 BV-only fail-closed 回归另用 pinned yt-dlp 已确认的 `BV13x41117TL ↔ av8903802` 等价对加固，普通下载仍接受 BV/av。
- 本轮没有真实 Cookie、真实媒体请求或平台级结论。合法 Windows CLI 参数仍会出现在本机进程列表/PowerShell history，且当前未证明私有 NTFS DACL；`--check` 也不判断 queued Job credential coverage 或 Cookie 登录态有效性。只允许受信任单用户本机使用，生产凭据仍需 ACL + secret sidecar/per-platform/per-Attempt process isolation。工程证据见 [Iteration 0.14.0 local Cookie evidence](validation/iteration-0.14.0-local-cookie-evidence.md)。

### 3.15 Iteration 0.15.0：Windows 一体化本机应用

- `video-download-local-app` 默认使用 `%LOCALAPPDATA%\Open-Flame\video-download-control`，固定 `data\control.sqlite3` 与默认同根工具目录；不从 CWD 或环境中的旧 VDC 路径拼接隐式数据库。可显式覆盖绝对 app/tool root 与端口，但拒绝 root/UNC/device/reserved/trailing-dot-space、link/reparse 和 data/tool/config/source 重叠。
- supervisor 在任何数据库/子进程副作用前以 `SO_EXCLUSIVEADDRUSE` 预占 `127.0.0.1`；以私有 Pipe 验证 control、Worker preflight、Worker ready 的严格 protocol/role/phase/run/build/schema 身份，并在每阶段调用 `/health` 与 capability snapshot。浏览器只在第三次验证后打开；失败只写脱敏 warning，不影响已就绪服务。
- 一次启动由 `.local-app.lock`、Windows kill-on-close Job Object 和最小子进程环境管理。正常停止及异常清理均先关闭 Worker command channel、有限等待，再关闭 control；仍存活的本应用 Job 才会被定点终止。子进程忽略 console 信号，由 supervisor 处理 Ctrl+C/SIGBREAK；强杀任一子进程会得到固定 JSON 错误、回收另一进程并释放端口。
- 旧共享 `multiprocessing.Event` 控制曾在 Worker 被强杀、恰好阻塞于 `Event.wait()` 时令父进程永久卡在 `Event.set()`；已以单向 Pipe 命令和 EOF stop 取代，并加入独立 spawn 子进程回归。真实 Windows `--check`、常驻 health/UI、Ctrl+C、10 次重复启动、同根 singleton、worker/control 强杀均使用独立随机 root/port 验证，未触碰用户既有 8000 服务。
- `video-download-control --help` 现在会在读取运行配置前正常返回，不再意外尝试启动 8000 服务；13 个命令模块的 `--help` 均返回 0。
- `--cookie-config` 只把一个配置文件路径放入 argv；严格 JSON snapshot 内的 ref/source path 不进入 CLI 输出和普通日志。真实凭据、Windows DACL 私有性、安装器/冻结 EXE、第三方工具包再分发和平台 Stage 0 仍未完成。工程证据见 [Iteration 0.15.0 local application evidence](validation/iteration-0.15.0-local-app-evidence.md)。

### 3.16 Iteration 0.16.0：Schema 11 claim fencing 与显式 retry

- Schema 11 新增唯一 `worker_claim_gate` 行，保存当前 `run_id`、`worker_id`、是否接受领取、激活时间与 stop 请求时间。readiness 要求规范化后的 canonical constrained DDL 精确匹配，拒绝 gate 上任何 trigger，并只接受严格 singleton 与 coherent pristine/prepared/active/stopped 状态。新 supervisor 在 control 身份就绪后先以关闭状态 `prepare` 并 fence 旧 run；Worker preflight 与第二次 HTTP 身份复核通过后才 `activate`，`--check` 永不打开 gate。
- 一体化 Worker 将当前 `run_id` 作为 claim fencing token。`claim_next()` 在既有 `BEGIN IMMEDIATE` 写事务中先核对 run、worker 与 `accepting_claims`，再执行过期 lease 恢复、`queued → probing`、lease 写入和新 Attempt 插入；`stop_claim_gate()` 使用同一 SQLite writer serialization。若 stop 先提交，后续 claim 不创建 Job lease/Attempt；若 claim 先提交，该笔 active Attempt 保持已提交事实，stop 只阻止下一次领取。
- supervisor 清理现在先提交 gate stop，再关闭 Worker command Pipe 并等待 Worker，随后关闭 control。`stop_claim_gate()` 在同一事务中写后重读；若状态未持久化或 SQLite stop 事务抛错，无法证明 stop-before-claim 顺序，则先 fail-safe 终止该应用拥有的 Windows Job，再触碰 advisory Pipe，并记录 forced shutdown；新 run 的 `prepare` 也会立即 fence 旧 generation。
- active Attempt 的恢复沿用 lease 语义而不是“恢复同一 Attempt”：被强停的 lease 到期后，下一 run 在 claim 事务中把旧 Attempt 终止为 `abandoned` / `worker_lost`，清除旧 lease，再创建递增 attempt number 的新 Attempt。确定性 barrier 回归覆盖 claim-first、stop-first、旧 run fencing、idle EOF 不二次 `run_once()`，以及 stop 后等待 lease expiry 再重领。
- 新增 `POST /api/v1/jobs/{job_id}/retry`，仅接受终态 `failed` 的 flat `download` Job；graph parent/child、非失败状态、同 source 已有 live/ready 工作及并发重复请求均 fail closed。成功请求在同一写事务中递增 `run_generation`、把 generation retry budget 归零、保留累计 Attempt 历史，并恢复 Input/Batch 为 queued。该动作不会关闭或绕过 platform circuit。
- 前端只为可重试的 flat failure 显示“新一代”按钮；平台状态显示自动 cooldown 截止时间、一次 half-open probe，且只在 `requires_manual_reset=true` 时显示人工复位。reset API 对不存在平台返回 404，对无需人工复位或并发冲突返回 409；`job.retry_requested` / `circuit.reset` 只记录有界控制字段。异步 submit、queue 和 circuit refresh 均有 generation guard，过时响应不会回滚当前 UI。
- claim gate 记录 prepared/activated/stopped 及 prepare/activate/stop failure/fenced 事件；forced shutdown 只接受 `gate_stop_failed + claim_gate_stop` 或 `child_timeout + child_shutdown` 配对，读取器拒绝不完整或篡改状态，日志不含路径、异常文本或重复 run ID。
- synthetic `rate_limited → terminal failed → explicit retry → cooldown 等待 → ready` 已覆盖，旧/新 generation 的 Attempts 均保留，成功后 circuit 回到 closed。最终回归、真实 Windows 隔离 UI/active-lease 关停、日志和 project-only package 均已完成；本轮仍未运行真实下载、真实 Cookie、Stage 0、目标 Linux/Docker、安装器或第三方发布流程。

### 3.17 Iteration 0.17.0：脱敏实时阶段估算

- yt-dlp download 命令加入固定 `download:` / `postprocess:` progress template、`--progress --newline --progress-delta 0.5`；probe 不启用。控制行只含常量 presence bit 与有界状态/数值字段，不输出 URL、source/format ID、标题、文件名或路径。
- subprocess runner 以 bounded buffer 流式观察 stdout 的完整 LF/CRLF/CR 行与末尾 fragment；超限 partial line 不交给 observer，observer 异常走既有有界停机并只记录异常类型/failure site。真实 Adapter 不观察 stderr；普通运行日志禁止原始 stdout/stderr、argv 与控制行内容。
- parser 严格要求两个主媒体 presence bit，字幕/sidecar 因缺失而不参与 progress，但仍按原资产契约映射、验证与发布。顺序视频/音频占两个保守 slot；indexed fragment 聚合各 slot 最新 fraction，未知总量只 heartbeat 不造百分比。`total_bytes_estimate` 可为有限非负 int/float，只用于 ratio，不冒充 exact total；畸形/超限/多于两路均忽略。
- Adapter 下载估算封顶低于完成，进入 `postprocessing` 后不退回 downloading；Worker 将其映射到 Job 的下载阶段区间，验证单列，只有 mapping、stat、完整验证和 ready 发布成功才写 `1.0`。这是一种阶段估算，不是精确全任务字节进度，单主流可能在进入后处理前保守停留于约半个下载区间。
- Batch API 原样返回持久化 progress；Web 显示中文阶段、`约 N%`、原生 `<progress>` 与仅含 platform/phase/percent 的 ARIA。独立 loopback synthetic QA 已验证 queued `0%`、downloading `24%`、postprocessing `79%` 与 console 0 error，并释放验收端口且未触碰既有 8000 服务。
- 已完成 broad/review/deterministic 聚焦回归、最终全量与静态检查，以及 project-only package 的 source-equivalence、identity、许可和隐私复核。权威记录见 [Iteration 0.17.0 real progress evidence](validation/iteration-0.17.0-real-progress-evidence.md)。本轮不新增真实平台、真实 Cookie、Stage 0、Linux/Docker 或第三方再分发结论。

### 3.18 Iteration 0.18.0：单进程双槽执行

- Windows app Worker child、standalone drain/poll 复用双槽协调器；实际执行可跨平台重叠并在空槽连续补位，不是只允许数据库领取两个任务。one-shot 与 check 模式保持原含义。
- claim/execute 拆分，live Future 包括最后清理阶段；补位排除仍由本进程持有的 job，并关闭目录、intent 与过期 lease 回收。暂停写库失败锁存，共享 stop Event 处理中断，受控 `WORKER_LOST` 仍为 terminal/manual retry。
- 入口先 red 后 green，`127 passed` 保留为阶段性记录；最终全量为 `1307 passed, 8 skipped in 118.43s`，reader 启动失败 4 项、Worker stop 5 项、实际入口 4 项均通过。静态门禁、独立 spawned `--check` 与 package 预检通过；冻结制品契约见 [0.18 并发证据](validation/iteration-0.18.0-concurrent-worker-evidence.md)。没有新真实平台、Cookie 或 UI 验收。

## 4. 已执行验证

0.19.0 最终全量为 `1410 passed, 8 skipped in 135.51s`；compileall、离线 lock（24 packages）、diff 与 35 Markdown 相对链接（0 missing）通过。默认模式/匿名/旧重试、短链导入、实际 Worker/Cookie 私有副本、前端可执行 JS、独立浏览器创建/取消均有回归。审查发现并修复 Cookie 状态接口遗漏日志路由白名单导致误降级；最终浏览器复验为正常/零拒绝/零写失败。真实 spawned local-app v2 synthetic `--check` 退出 0，三进程共享 run_id，profile/batch/job/attempt/asset 均为 0，未 claim/打开浏览器/复制 Cookie，三进程退出且 18821 可重新 bind。0.19 项目包预检通过 231 source / 232 sdist / 63 package / 99 wheel-RECORD / 32 legal / 13 CLI / 14 distributions，以及隐私 8 markers + 11 patterns 零命中；最终文档冻结后必须重新构建并验证同一契约，精确 hash 和最终结果仅保留在包外报告，不能复用预检 hash。

当前 0.19.0 结果以本轮证据和包外最终报告为准。下方 0.18.0 的最终全量、静态门禁、独立 spawned app 与 source-equivalent package 记录，以及 0.17.0 和更早各行，均是 point-in-time 历史基线，不能自动继承为 0.19 通过。0.9.1 用独立版本承载 Apache-2.0 迁移，避免复用 proprietary 0.9.0 的制品版本号：

0.19 文档完整后的冻结 inventory 为 **232 checkout source / 233 sdist 普通文件**；比上方早期预检多出本轮 evidence.md。63 package / 99 wheel-RECORD 与其余许可、CLI、依赖、隐私契约不变。最终报告必须对应包含本段最新文档的独立重建，不能把较早的预检或文档未完整版本作为最终包。

| 检查 | 结果 |
|---|---|
| 0.28.0 当前应用根运行时刷新 | canonical `--app-root` 下 AI runtime integrity PASS；无密钥时 provider health 为 UNVERIFIED；Upload runtime/backend、scheduler 与 worker READY；实际 Upload DB Schema 1→3 且既有数据保留；未发起真实供应商或平台调用 |
| 0.18.0 实际入口 red reproduction | 阻塞第一个平台后，旧 local-worker drain/poll 与 app Worker child 不能启动第二个平台；actual entrypoint 回归先失败后修复。工具构建边界注入 synthetic Worker，不访问平台 |
| 0.18.0 阶段性集中回归 | `127 passed`；仅覆盖当时实现切片，不是最终结果；后续线程启动和 stop 修复已纳入下方最终回归 |
| 0.18.0 最终全量与静态门禁 | `1307 passed, 8 skipped in 118.43s`；`compileall -q src tests`、`uv lock --check --offline`（24 packages）、`git diff --check` 通过；34 个 Markdown 文件的相对链接检查 0 missing |
| 0.18.0 故障/入口回归 | reader startup 4 个真实 child 回归、Worker stop 5 项、实际入口并发/中断 4 项通过；heartbeat 启动异常不再遗留无心跳的 running Attempt |
| 0.18.0 独立 spawned app 预检 | 最终源码 `local_app_cli --check`、独立 app root/18819、固定本地 tool root；stdout 为 checked，preflight/stop JSONL 存在，未 claim、未使用 Cookie/媒体/浏览器，端口已释放 |
| 0.18.0 package/许可/隐私契约 | 预检通过并冻结为最终重建门槛：221 checkout source、222 sdist 普通文件、61 package 文件逐字节一致、97 wheel/RECORD 条目、32 legal、13 CLI、14 runtime/project distributions；metadata/import 0.18.0/Apache-2.0/精确 NOTICE、依赖和 checkout/wheel build identity 一致；8 个已知真实标记与 11 类高置信秘密模式均 0 hit。精确最终 archive hash/重建结果只留在包外报告 |
| 0.8.0 全套 pytest 历史基线 | `846 passed, 8 skipped` |
| 0.8.1 全套 pytest 历史基线 | `853 passed, 8 skipped in 47.79s`；该版本构建、隔离 wheel 安装和本机服务复验均已完成 |
| 0.9.0 全套 pytest 最终结果 | `881 passed, 8 skipped in 49.45s`；`compileall` 与 `uv lock --check --offline` 同步通过 |
| 0.9.1 Apache 迁移后全套 pytest | `882 passed, 8 skipped in 50.46s`；`compileall` 与 `uv lock --check --offline` 通过 |
| 0.10.0 六平台路由阶段性 pytest | `914 passed, 8 skipped in 53.84s`；该数字早于本轮真实下载修复，只作为路由切片的时间点记录 |
| 0.10.0 本轮最终全套 pytest | `920 passed, 8 skipped in 52.23s`；`compileall`、`uv lock --check --offline` 与 `git diff --check` 通过 |
| 0.10.0 Instagram 单样本本机 E2E | 全新 data root、Windows direct/no-cookie/Node、固定 yt-dlp/FFmpeg；`1/1 ready`，DB/manifest/API copy/完整解码/清理/JSONL/UI 均通过；不是 Stage 0 |
| 0.11.0 Bilibili 重复诊断 | 固定 yt-dlp `2026.08.19`、同一公开样本、无 Cookie；raw probe `2/2` 成功，产品同款 fresh attempt `4/6` 成功、`2/6` 在 probe 阶段 HTTP 412；只支持“瞬时平台门禁最符合观测”的推断，不是 Stage 0 或稳定下载证明 |
| 0.11.0 Schema/migration/backup 定向回归 | `78 passed, 4 skipped`；Schema 8→9、并发迁移、readiness、Schema 9 backup/restore 与 Stage 0 CSV v2 均覆盖，skip 为目标 Linux/root 环境边界 |
| 0.11.0 Bilibili/retry 定向回归 | `67 passed`；覆盖 probe/download 的 HTTP 412 与 API `code -412`、仅 Bilibili 生效、平台级 cooldown 及版本探测不误分类 |
| 0.11.0 最终全套 pytest | `935 passed, 8 skipped in 56.55s`；`compileall`、`uv lock --check --offline` 与 `git diff --check` 通过 |
| 0.11.0 隔离 API/UI 验收 | health 返回版本 `0.11.0`、Schema `9`；7 条能力均为 `candidate` 且显示 `job_kind=download`；完成 synthetic batch 创建/取消、日志刷新，浏览器控制台无 error/warn；隔离实例有意不配置工具根，未执行真实下载 |
| 0.11.0 package 与隐私 | sdist/wheel 离线构建成功；wheel metadata 为 `0.11.0` / `Apache-2.0`，包含要求的项目法律材料；tracked tree 与解包 sdist 的敏感标记扫描通过，未跟踪媒体、数据库、Cookie、日志或 JSONL 运行产物 |
| 0.12.0 辅助产物/TikTok/离线路由 | 离线定向回归 `160 passed in 14.08s`；辅助稳健性回归 `18 passed in 5.74s`；最终全套 `969 passed, 8 skipped in 63.58s`；覆盖损坏 sidecar、>1 MiB 流式响应/断连清理、TikTok gate/exact-host policy，以及 YouTube/Bilibili/Douyin/TXT/CSV fake E2E |
| 0.12.0 隔离 API/UI | v0.12.0 / Schema 9；synthetic batch `1/1 ready`；页面列出并实际触发原件、缩略图与 en-US 字幕下载；API 内容/长度/安全头通过，浏览器 console 0 warning/error；未配置工具根、未访问真实平台 |
| 0.12.0 package/许可/隐私 | 回填最终证据后的 source-equivalent sdist/wheel offline rebuild 与隔离 wheel 安装通过；metadata/import `0.12.0` / `Apache-2.0`、11 个 console scripts、32 个 legal files；53 个包源码文件逐字节匹配；193 个 publishable 源文件与两个 archive 的已知私有标记扫描 0 hit |
| 0.13.0 capability governance 全量回归 | `1072 passed, 8 skipped in 81.80s`；`compileall`、离线 lock check、Ruff `E9,F63,F7,F82` 与 `git diff --check` 通过；默认完整 Ruff 规则集不是当前发布门禁 |
| 0.13.0 隔离 API/UI/日志 | v0.13.0 / Schema 10；7 implementation、0 evidence、0 decision；YouTube/Bilibili/TikTok synthetic batch `3/3 ready`，3 个下载端点的内容/长度/hash/安全头匹配；control/offline-worker 日志无写入或拒绝事件，浏览器 console 0 warning/error；未访问真实平台 |
| 0.13.0 package/许可/隐私 | 最终文档回填后离线重建并隔离安装；metadata/import `0.13.0` / `Apache-2.0`、12 个项目 console scripts、32 个 legal files；wheel 中 56 个 package 文件与 checkout 逐字节匹配，checkout/wheel build identity 相同；201 个 source-tree publishable 文件、202 个 sdist 文件条目、92 个 wheel 文件条目及两个 archive 的已知私有标记扫描 0 hit |
| 0.14.0 Windows 本机 Worker/Cookie 回归 | `1084 passed, 8 skipped in 90.42s`；`compileall`、离线 lock check 与 `git diff --check` 通过；本机未安装 Ruff，因此不声称本版本 Ruff 通过 |
| 0.14.0 独立 CLI/API/UI/日志 | 真实固定工具链 + synthetic Douyin Cookie source 的 `local-worker --check` 成功且数据库四张工作表前后不变；v0.14.0 / Schema 10；Bilibili/Douyin/TikTok synthetic batch `3/3 ready`，原件 API 内容/hash/安全头一致，浏览器 console 0 warning/error；Cookie 标记 0 hit，未访问真实平台 |
| 0.14.0 package/许可/隐私 | 文档回填后离线重建并隔离安装；metadata/import `0.14.0` / `Apache-2.0`、12 个项目 console scripts、32 个 legal files；202 个 publishable/source 文件、203 个 sdist 条目、92 个 wheel 条目、56/56 个 package 文件逐字节一致；wheel 隐私/秘密扫描 0 hit，sdist 仅有 tests synthetic canary |
| 0.15.0 一体化生命周期/回归 | local-app/control CLI 定向 `96 passed`，supervisor/API/Cookie 切片 `249 passed, 1 skipped`；最终全量 `1196 passed, 8 skipped in 93.06s`；`compileall`、离线 lock check 与 `git diff --check` 通过；check Worker 只有收到第二次 HTTP 身份校验后的专用完成命令才记录 `check_complete`，EOF 始终表示 supervisor shutdown |
| 0.15.0 真实进程/UI/日志 | `--check`、10 次重复、singleton、Ctrl+C/SIGBREAK、worker/control 强杀与死 Pipe receiver 均有界收敛；随机根/port 的 v0.15.0 UI 完成四类刷新、safe `.invalid` 失败批次与重开，console 0 warning/error；三组件共享 `run_id`且泄漏 marker 0 hit |
| 0.15.0 package/许可/隐私 | 最终文档回填后离线重建，从精确 sdist 构建 wheel 并隔离安装；metadata/import `0.15.0` / `Apache-2.0`、13 个 console scripts、32 个 legal files；211 个 publishable/source 文件、212 个 sdist 普通文件、96 个 wheel/RECORD 条目、60/60 个 package 文件逐字节一致；14 个隔离 runtime/project distributions 通过 dependency check，checkout/wheel build identity 一致，已知隐私与高置信凭据 marker 0 hit |
| 0.16.0 claim/retry 核心阶段性回归 | 首轮定向核心回归 `243 passed in 28.11s`；发生在最终版本号与两阶段 prepare 调整前，只作为实现切片的时间点记录，不是当前发布验收 |
| 0.16.0 最终聚焦回归 | claim/migration/backup 四模块 `101 passed in 27.92s`；local-app/log/API 四模块 `135 passed in 9.01s`；两个不重叠切片合计 236 tests，覆盖 exact gate DDL/trigger/state、stop 写后验证、生命周期日志、UI generation guards、retry/circuit 与备份 |
| 0.16.0 首次全量阶段性回归 | `1219 passed, 8 skipped in 99.56s`；该次发生在后续 hardening 前，仅作为时间点记录 |
| 0.16.0 最终全量与静态门禁 | `1238 passed, 8 skipped in 104.14s`；`compileall -q src tests`、`uv lock --check --offline` 与 `git diff --check` 通过 |
| 0.16.0 真实 Windows UI/日志/active lease | 独立随机根/端口的 `--check` 与完整 TTY 生命周期均正确 prepare/activate/stop 并释放端口；延迟旧 submit/queue/circuit 响应不覆盖新状态，console 0 warning/error；外部 claim 提交 Attempt 1 后正常 stop 关闭 gate 且不倒写 active lease，Worker 尚未启动媒体、无平台请求；日志 write/reject/leak marker 均为 0，用户既有 8000 未动 |
| 0.16.0 package/许可/隐私 | 最终文档回填后离线重建，从精确 sdist 构建 wheel 并按 runtime lock 隔离安装；metadata/import `0.16.0` / `Apache-2.0`、13 个 console scripts、32 个 legal files及精确 NOTICE；212 个 checkout publishable 文件、213 个 sdist 普通文件、96 个 wheel/RECORD 条目、60/60 个 package 文件逐字节一致；14 个 runtime/project distributions 通过 dependency check，checkout/wheel build identity 一致，已知真实隐私标记与高置信秘密 0 hit |
| 0.17.0 broad progress 聚焦回归 | `186 passed in 24.95s`；覆盖 Adapter/parser/runner/Worker/API/UI 的较宽实现切片，属于阶段性工程证据 |
| 0.17.0 review 聚焦回归 | `149 passed in 13.66s`；另有两个确定性握手回归 `2 passed in 1.34s` |
| 0.17.0 隔离浏览器 QA | gitignored 独立根与 `127.0.0.1:18779` synthetic YouTube Batch：queued `0%`；DB 状态 downloading `0.24` 显示“正在下载 / 约 24%”；postprocessing `0.79` 显示“正在合并/后处理 / 约 79%”；原生 progress value/ARIA 正确，console 0 error；服务已关闭并释放 18779，既有 loopback 8000 未动 |
| 0.17.0 最终全量与静态门禁 | `1270 passed, 8 skipped in 106.62s`；`compileall -q src tests`、`uv lock --check --offline`、`git diff --check` 通过；26 个 Markdown 文件的相对链接检查为 0 missing |
| 0.17.0 package/许可/隐私 | 最终文档回填后离线重建，从精确 sdist 构建 wheel 并按 runtime lock 隔离安装；metadata/import `0.17.0` / `Apache-2.0`、13 个 console scripts、32 个 legal files及精确 NOTICE；215 个 checkout publishable 文件、216 个 sdist 普通文件（215 source）、96 个 wheel/RECORD 条目、60/60 个 package 文件逐字节一致；14 个 runtime/project distributions 通过 dependency check，checkout/wheel build identity 一致，已知真实隐私标记与高置信秘密 0 hit。精确 archive hash 只留在 gitignored verifier/外部验收报告，避免源内自引用 |
| 0.9.0 本机 Worker/API/UI/日志 | Windows-only direct Worker 的双重显式启用、Schema/toolchain/logger/singleton lock、claim/phase/terminal；最近批次、ready asset list/download、路径/identity/size 拒绝；`local-worker` 日志读取均已纳入当前测试集 |
| 最终 v3 双样本 E2E | 两个 Job 均被真实 Worker claim 并 `ready`；输入 URL、运行时 UUID 与媒体指纹不进入公开记录，完整值保留在 gitignored 本机证据中 |
| 资产 API 与完整解码 | 两个 `/api/v1/assets/{asset_id}/download` 均返回完整原件；下载文件 size/SHA-256 与 DB/manifest 一致，两个文件完整流解码 clean |
| 浏览器使用验收 | 最近批次可打开，页面显示 `ready`、`2/2 ready` 与两个下载链接；实际下载可用，控制台无 error/warn |
| 0.9.0 package 历史 metadata | 当时的 sdist/wheel 为 `0.9.0`、`LicenseRef-Proprietary`、11 个 console scripts、31 个 legal files；不得作为当前 Apache 发布包 |
| 0.9.1 Apache package | sdist/wheel 独立构建并隔离安装通过；metadata `0.9.1`、`Apache-2.0`、11 个 console scripts、32 个 legal files；不含日志、数据库、媒体、`runtime-tools` 或 `validation/local` |
| 0.10.0 package | sdist/wheel 离线构建成功；metadata `0.10.0`、`Apache-2.0`、11 个项目 console scripts 与包内容隐私边界通过；临时验证产物已删除 |

0.16.0 最终全量中的 8 个 skip 均为明示环境边界：1 个 POSIX Cookie directory-FD cleanup、4 个 root POSIX/getfacl 的 0/1/3/6 source metadata contract、1 个当前 Windows 环境不可用的 AF_UNIX roundtrip、1 个 POSIX open-file replacement、1 个 POSIX permission test。必须在目标 Linux 重跑，且不得当作通过。

0.9.0 最终 v3 的 gitignored 原始数据库、资产/manifest、API 下载和 JSONL 位于 `validation/local/local-worker-e2e-20260903-v3/`，可提交汇总见 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md)。0.10.0 的 Instagram 单样本临时 data root 已在取证后定点删除，只提交 [Iteration 0.10.0 Instagram live evidence](validation/iteration-0.10.0-instagram-live-evidence.md) 的去标识汇总。0.11.0 的 Schema/CSV/412、0.12.0 的辅助产物/TikTok/离线 E2E、0.13.0 的 capability governance、0.14.0 的本机 Cookie 接入与 0.15.0 的一体化应用汇总只在各自公开证据文件保留去标识事实。`dist/` 中的 0.9.0 及更早制品都是 proprietary 历史包，不得发布或使用通配符上传；0.16.0 project-only Apache-2.0 sdist/wheel 已在新的 gitignored 独立目录从最终源码重建并验证，没有覆盖或改名复用旧制品。该结果不包含第三方可再分发工具、安装器、wheelhouse 或 OCI image。

0.13.0 本轮明确没有发起任何真实媒体请求；仅执行 synthetic/offline Worker 和隔离 UI/API 验收。仍未执行：TikTok 真实下载请求、Docker build/pull/up、Linux namespace/UDS/ACL/resource/core-pattern/runtime bind、真实 Cookie 流程、完整 Stage 0、X exact selector、Schema 8→9/10 的目标生产数据迁移前后恢复演练、Linux/NAS 恢复验收，以及任何第三方 binary/container/tool-bundle 发布。历史上的 Bilibili 重复 probe 诊断未形成 ready 资产，Douyin 尝试也无 ready 资产；Instagram 仅有一条成功样本。项目自有源码已获 Apache-2.0 授权；不得把离线链路、间歇 probe、失败探索或 YouTube/X/Instagram 三个精确输入的成功外推为平台级兼容性。

0.14.0 同样没有发起真实媒体请求，也没有接触真实 Cookie；只使用 synthetic Cookie/fake media 验证 Windows 本机 Worker 的凭据接线。不得把这条工程回归写成 Douyin、TikTok、Bilibili 或 Instagram 的真实登录态下载成功。

0.15.0 也没有发起真实平台请求或使用真实 Cookie；浏览器验收只提交保留的 `.invalid` 输入并在本地规范化阶段终止。该轮只证明一体化生命周期，不新增任何平台结论。

0.16.0 同样没有发起真实平台请求或使用真实 Cookie；`rate_limited → retry → ready`、浏览器 manual reset 及真实 spawned active lease 都使用 synthetic/no-network 状态。active lease 只证明已提交 Attempt 的关停语义，不是 active platform download。该轮只能证明 claim fencing、显式 retry、UI 竞态与 circuit 展示的受测契约，不能升级任何平台能力、隔离部署或第三方再分发状态。

0.17.0 同样没有发起真实平台请求或使用真实 Cookie；进度浏览器验收通过直接注入 synthetic 持久化阶段，只证明 API/DOM/ARIA 展示与阶段单调契约。顺序/fragment/sidecar/未知总量由确定性测试和固定 bundle 的离线模板求值覆盖，不是平台网络实跑。最终全量、静态检查与 project-only package 已通过，但这些结果不能升级任何平台、Linux 隔离或第三方再分发状态。

0.18.0 本轮只新增无网络的并发/入口/中断工程回归；没有新浏览器验收，也没有使用真实 Cookie 或访问真实媒体平台。`WORKER_LOST` 经现有 retry policy 处理时为终态失败，不自动重试；合法 flat failed Job 可由用户显式发起新一代重试。该受控中断与进程强杀后由 lease-expiry recovery 创建新 Attempt 是不同路径，不能混写。

0.18.0 最终全量的 8 个 skip 为 POSIX directory-FD cleanup 1 项、root/getfacl Cookie source metadata 4 项、AF_UNIX roundtrip 1 项、POSIX open-file replacement 1 项、POSIX permission 1 项；不是通过，仍需目标 Linux 验证。当时 checkout/wheel package-payload identity 为 `0.18.0+build.sha256.b36396390d9782343f0b3579d220e6a32314e4c78ffc82d4fd55a98f518afdf2`，它不是当前 0.19 identity 或包含本交接文件的 archive hash。

## 5. 继续工作入口

继续前先读[当前应用根运行时刷新记录](validation/iteration-0.28.0-local-runtime-refresh.md)、[上传 attention 恢复记录](validation/iteration-0.28.0-workflow-upload-attention-recovery.md)、[译文精确修订绑定记录](validation/iteration-0.28.0-translation-revision-binding.md)、[编辑式玻璃四页生产前端证据](validation/iteration-0.28.0-editorial-glass-frontend.md)、[配音语速验证](validation/iteration-0.28.0-speech-rate.md)、[重复下载 owner 验证](validation/iteration-0.28.0-workflow-duplicate-download-owner.md)、[预授权重启续跑记录](validation/iteration-0.28.0-workflow-restart-continuation.md)、[三平台参数与封面预检记录](validation/iteration-0.28.0-workflow-platform-parameters.md)、[自动流程服务端预检记录](validation/iteration-0.28.0-workflow-server-preflight.md)、[0.28.0 AI 与自动流程证据](validation/iteration-0.28.0-ai-workflow-evidence.md)、[发布后自动流程正确性记录](validation/iteration-0.28.0-post-release-automation-correctness.md)、[AI 精确授权与输入硬预算记录](validation/iteration-0.28.0-post-release-ai-authorization.md)、[Schema 4 远程调用账本记录](validation/iteration-0.28.0-ai-invocation-ledger.md)、[整流程取消证据](validation/iteration-0.28.0-workflow-cancellation.md)、[AI runtime 指南](docs/AI_RUNTIME.md)、[编辑指南](docs/EDITOR.md)及对应包外 `release-receipt.json`。若 receipt 不存在或未同时绑定 clean commit、冻结全量、完整 identity、五件制品及源码/wheel 独立验收，就把当前状态视为源码里程碑，不把它称为最终发行制品。预授权 queued 工作安全重启续跑、三平台参数卡、封面预检、Editing Schema 4 账本、unknown 人工 reconciliation、可复用预设、真实域离线整链、整流程安全取消、重复下载 owner 恢复、配音语速、编辑式玻璃四页、译文精确 revision 绑定及上传 attention 精确确认恢复均已重新验证。legacy 同语言/provider/model 多个已批准修订现在保持未选择并失败关闭；当前实际 app-root 已完成两个 runtime、Upload Schema 1→3 数据保留迁移与实际 Start 验证，AI provider 仍因无密钥保持 `provider_health_required`。下一步是准备新 clean commit/receipt，并在单独明确授权后对同一冻结提交执行真实 OpenAI、真人试听和 Bilibili、抖音、视频号逐平台验收。真实登录、扫码、下载、上传或发布仍须用户另行明确授权。旧版本结果或制品不能绑定给 0.28.0。

本地开发：以下命令适用于普通非虚拟化终端；当前 packaged 开发宿主应按[运行时刷新记录](validation/iteration-0.28.0-local-runtime-refresh.md)让 Setup 与 Start 使用同一 canonical `--app-root`。

```powershell
uv sync --extra dev
uv run pytest -q
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
uv run video-download-local-app --tool-root $ToolRoot --allow-direct-network
```

依赖变更只在受审阅分支刷新，随后检查 lock diff、target wheel availability、hash/publisher/provenance 与 license：

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

默认地址 `http://127.0.0.1:8000`；不自动读 `.env`。普通 offline fake 只验证 flat-v1，必须保持 `VDC_ENABLE_X_GRAPH_V2=0` 和 `VDC_ENABLE_SHORT_LINK_RESOLUTION=0`。

Windows 本机工具与一体化入口：

```powershell
$ToolRoot = (Resolve-Path -LiteralPath ".\runtime-tools\windows-x64").Path
uv run video-download-tools verify --tool-root $ToolRoot
uv run video-download-tools smoke --tool-root $ToolRoot
uv run video-download-tools status --tool-root $ToolRoot
uv run video-download-local-app `
  --tool-root $ToolRoot `
  --allow-direct-network `
  --check
uv run video-download-local-app --tool-root $ToolRoot --allow-direct-network
```

页面中的工具 `ready` 只证明固定工具完整且离线 smoke 通过；`local_direct_worker_available=true` 只表示当前 Windows 主机具备显式启动入口。一体化 supervisor 会确认它自己的控制面和 Worker 身份，但对外 API 仍不把三项 `network_download_enabled` / `isolated_worker_ready` / `platform_download_verified` gate 猜成 `true`。

普通 Windows 实际下载只启动一个 `video-download-local-app`；它在三次私有握手/HTTP 身份校验后打开浏览器，`Ctrl+C` 先停 Worker 再停控制面。需要 Cookie 时，先按 Runbook 7.2 登记/分配 profile，然后按 Runbook 2.2 只向一体化入口传入受保护的 `--cookie-config` 路径。单独的 `video-download-control` 和 `video-download-local-worker` 只保留给高级排障。本机 Worker 是 direct-network 模式，不等同于隔离 deployment Worker。

部署前必读：[Deployment candidate](deployment/README.md)、[Runbook](docs/RUNBOOK.md)、[Linux/Docker checklist](validation/linux-docker-acceptance.md)。full acceptance 必须由 clean trusted root launcher 以绝对路径、清空环境和 `bash -p` 启动，只使用 synthetic Cookie source 和 deny-only `replace.invalid` policy。授权 token、owner/mode/ACL、保留证据和清理边界以 checklist 为准。

## 6. 未完成项与残余风险

1. Stage 0 未执行；CSV v3 按 `platform × source_type × job_kind × adapter × downloader_version × environment × product_version` 分格，`product_version` 必须是版本号加当前完整 package-payload hash。报告不写数据库，导入只追加 evidence，批准/撤销另走本地 revision CAS。仓库没有当前批准记录。YouTube/X 各一个明确样本和 Instagram 一个 NASA 官方公开样本的 Windows 本机全链路成功仍不足以完成平台认证，六平台兼容视图继续为 `candidate`，X graph-v2 另外保持 `disabled`。Bilibili 重复 probe 中的间歇 HTTP 412 与 Douyin 的 `authentication_required` 都不是平台级否定或支持结论。
2. Linux Cookie override 只是 service-level isolation：单个被攻陷的 Worker/downloader 可读全部平台 source。Windows 一体化入口只在 argv 中暴露受保护的 config 路径，但高级手动 `--cookie-source` 仍会在进程命令行/PowerShell history 暴露合法 ref 与绝对 source 路径，且 readonly 不等于私有 NTFS DACL。真凭据前必须补 owner/DACL 实证，并改为 credential sidecar、per-platform Worker 或 per-Attempt process/mount namespace。
3. DNS/replay OS blocking I/O 不可强制取消；slot 有界并 fail-fast，但 replay root 必须是可靠本地文件系统，更强保证需 process isolation。injected resolver 仍须遵守 timeout contract。
4. short-link egress 还没进 Compose/supervisor；真实 POSIX owner/mode、TLS/SNI、DNS rebinding、restart/replay 和 redirect chain 仍是 target gate。
5. acceptance 依赖 clean root launcher、reviewed/exclusive checkout/build context、local Docker/BuildKit 和无 physical alias/pre-existing bind mount。Unix Docker socket 不能单独证明 daemon/build/mount namespace 同机；Cookie root 的 host `nodev,nosuid,noexec` mount flags 也是 operator prerequisite，runner/YAML 当前不证明。
6. 外部 syntax tag 已移除，但目标 daemon 自带 frontend/BuildKit 的版本、配置以及 Dockerfile 1.3+ `RUN --network=none` 实际执行仍未在 Linux 验证。Base/tool registry availability、target manifests/wheels、image/package hashes 与 provenance 仍须批准；digest/hash 字面量本身不完成供应链审计。
7. Frozen Compose 文件正常退出只在 identity/snapshot 未变时删除；crash 可能在 private env 目录遗留敏感 config，必须按 exact path/device/inode 审计后定点清理，禁止 glob/递归删除。
8. Host pipe `core_pattern` 会使 `RLIMIT_CORE=0` 不足以排除 crash capture；runner 已 fail closed，但目标 host 尚未验证。
9. yt-dlp 的 remote components 继续禁用；显式单一 JS runtime 接线已实现，本轮 YouTube 既完成 Node.js A/B 探测，也在最终本机 Worker E2E 中显式使用 Node。Node.js 尚未进入 tool bundle lock，未配置时仍默认 `--no-js-runtimes`，因此不能把单样本结果外推为 YouTube 全站兼容。
10. 控制面与 ready 资产下载 API 无认证，只允许 loopback；不得直接监听 LAN/公网。本机 direct Worker 使用宿主机直连网络，双重显式开关与单实例锁只减少误启动/并发冲突，不提供 Linux 隔离 Worker 的网络边界。
11. supervisor、control 与 `local-worker` 日志已统一到共享 `run_id` 的本机 best-effort JSONL，但仍无集中采集、告警、反代、存储配额或灾难切换；日志不能替代 SQLite、manifest、备份和外部监控。
12. 首次公开 Git 提交使用 GitHub `noreply` 作者身份，避免把本机真实邮箱写入永久历史；远端已有历史必须线性保留，禁止 force push。
13. 项目权利人已明确授予 Apache-2.0，版权声明为 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。这只闭合项目自有源码、文档和脚本的公开许可，不重新许可任何第三方材料。
14. `pydantic-core` 的原生 Rust 闭包必须按实际 target/architecture 生成 Cargo SBOM、依赖映射和法律文件包；Linux amd64 调查不能外推到 Windows、macOS、ARM 或其他目标。
15. Python base image 必须按目标平台的 OCI child manifest 审计 OS packages 与许可证；multi-arch index digest 和上游 `NOASSERTION` SBOM 不能代替这一工作。
16. Windows x64 yt-dlp/FFmpeg/ffprobe 本机 bundle 已安装、hash/version/configuration 校验并完成离线 smoke，YouTube、X、Instagram 共三个精确输入也已经过 Worker/AssetStore/manifest/API/UI 全链路；但对应源码/build closure、完整 SBOM、目标架构法律文本和人工批准仍未闭合，故该第三方工具包及包含它的容器/二进制再分发继续 blocked。detached signature 目前只保留，未完成密码学验签。
17. 0.9.0 的 `881 passed, 8 skipped`、31 个法律文件和 11 个入口仅是 proprietary 历史记录。0.13.0 当时的关键 Ruff 错误规则 `E9,F63,F7,F82` 已通过；本机当前未安装 Ruff，因此不声称 0.17.0 Ruff 通过，也不能误称全库 Ruff clean。`pip-audit` 也只是 2026-09-03 的时间点扫描，不代表当前或未来无漏洞。
18. Candidate tool validator 现在会实际读取 `sources/` 下的 source artifact，拒绝缺失、路径越界、目录、symlink/reparse、hardlink、空文件和 SHA-256 不符；但其 SBOM 检查仍只验证受 hash 约束的非空 JSON 形状，不能替代组件语义审计或法律批准。
19. 0.9.1 Apache 迁移后全量回归为 `882 passed, 8 skipped`；独立 sdist/wheel 构建、隔离安装、`Apache-2.0` metadata、32 个法律文件与 11 个命令入口已复验。构建只写入 gitignored 验收目录，不覆盖 proprietary 0.9.0 制品。
20. v0.16.0 已把一体化 run 的 stop 与新 lease/Attempt 领取线性化：以 SQLite writer transaction 的提交顺序为准，不能把 console 信号到达时刻当作线性化点。stop 不撤销已先提交的 active Attempt；强停后要等待 lease expiry，旧 Attempt 才会变为 `abandoned/worker_lost` 并由新 Attempt 重领。`run_once()` 在最终 claim 事务前仍可能执行有界的终态目录 reconciliation 或 asset-intent recovery，因此这里只承诺 stop commit 后不新增该 run 的 Job lease/Attempt，不宣称“停止后零文件系统副作用”或强制取消阻塞 I/O。当前 deterministic repository/cleanup 回归已覆盖此契约，真实平台 active-download 强停不属于本轮证据。
21. v0.17.0 progress 是保守阶段估算，不是全任务精确 byte accounting。字幕/sidecar 不推动进度，未知总量不显示伪百分比，顺序双流最多按两个 slot 聚合，单流可能在后处理前停留于较低估算；浏览器约 2 秒轮询也可能错过很短阶段。为保持隐私，普通排障不得打开原始 stdout/stderr、argv、URL、source ID、标题、文件名或路径日志。
22. 0.24.4 T10 只保留为中间冻结源历史，不能用其定向测试、commit 或制品关闭 0.25.0 最终冻结。0.25.0 包内文档同样有意不嵌入自身最终 commit、build identity 或五件发行文件 hash；只有同批包外 release receipt 同时绑定新的 clean commit、冻结全量、完整 identity、五件发行文件和源码/wheel 独立验收时，才可把最终交付包记为通过。
23. 上传 activity lock 只协调采用当前 shared/exclusive 合同的应用、active/standby 服务与备份工具。旧版本、手工 SQLite 连接、自写文件 writer 或绕过锁的进程不在完整保证内；停机备份仍要求操作者确认它们全部停止。下载和上传备份必须分别创建与演练。
24. T09 已在 GitHub hosted 执行 Windows/Linux × CPython 3.12.10/3.13.14 四格；`bdd88ce` 对应 run `34393235622` 的四格均通过测试前环境与门禁，完整 pytest 仍红，因此只能确认执行链恢复，不能确认 CI 通过。required checks 与 branch protection 为 **NOT CONFIGURED**；Windows 上跳过的 root/POSIX/getfacl 合同仍归 T15。
25. 0.25.0 T16 本地 G6 没有执行真实登录、扫码、下载或上传，也没有运行 GitHub hosted CI 或目标 Linux/Docker T15。T11/T12 仍须绑定最终 0.25.0 构建分别记录 PASS/FAIL/BLOCKED/NOT RUN；T13 与 T15 不因本地前端完成而关闭。
26. AI authorization 固定的是本地 runtime manifest、provider/model 声明、operation、外发范围和输入/调用上限。远端模型 ID 仍可能是供应商可更新的 alias；供应商在相同 ID 下改变模型行为、价格或可用性时，本地摘要不会自动发现。Editing Schema 4 已有脱敏调用状态账本，但本次没有真实 provider 调用，因此没有供应商 request ID、实际 usage、价格或账单证据；本地硬上限不能证明账单金额，unknown outcome 后仍须停止并核对。runtime 在 provider 启动前后重复散列，听写输入使用私有稳定副本，但跨平台按路径启动不构成抵抗同系统账号并发改写者的密码学 attestation；维护 runtime 前须停止应用，跨进程 lease/不可变 snapshot 仍是发布前加固项。

## 7. 历史收尾与后续精确入口

1. 保留 `validation/local/local-worker-e2e-20260903-v3/` 的历史 YouTube/X exact batch；Instagram 单样本的临时 data root 已删除，只保留 [Iteration 0.10.0 Instagram live evidence](validation/iteration-0.10.0-instagram-live-evidence.md) 的公开脱敏汇总。继续保持 [Iteration 0.9.0 local Worker E2E evidence](validation/iteration-0.9.0-local-worker-e2e-evidence.md) 的既有边界，不把原始媒体、精确 URL 或运行标识纳入可分发制品。
2. 在用户明确授权准备系统运行时后，于 clean target Linux/WSL 或 Docker 环境用 synthetic-only 输入先跑 preflight；修正环境后再由用户/运维明确授权 full mode，保留 JSONL、restore root 和 stale socket。
3. 在发布环境记录/验证 daemon-bundled Dockerfile frontend 与 BuildKit，证明所有 post-download `RUN --network=none` 生效；批准 base/tool registry manifests、target wheels 和包/镜像 provenance；固定 local Docker/BuildKit default context、exclusive checkout 和无 path alias/bind-mount 条件，并演练 image-ID 绑定、frozen Compose 正常删除与 crash-remnant exact-identity 清理。
4. 将 short-link egress 纳入 supervisor/隔离 topology，在 target POSIX 测 AF_UNIX、mode/owner、restart/replay、TLS/SNI、DNS rebinding、SIGTERM/stale socket 和最坏延迟。
5. 用 credential sidecar/per-platform Worker/per-Attempt namespace 将 Cookie source exposure 缩到一个平台/Attempt；先用 synthetic file 验证 rotation/durability。
6. 延续 Bilibili → Douyin → TikTok → Instagram 的验收优先级：先调查 Bilibili HTTP 412 的可复现边界；Douyin 仅在用户提供 fresh cookies 并确认授权后重跑；再执行 TikTok 首个真实样本；最后把 Instagram 从当前一个正向样本扩展到正式 Stage 0。每个 evidence identity 仍需 10+ 正向、独立负向和连续三轮；X 只在真实 stable key/exact-one-selector 通过后才考虑开 graph gate。
7. 项目自有源码已按 Apache-2.0 公开；如需发布 dependency wheelhouse、冻结可执行文件、OCI/container image 或 tool bundle，仍须按实际目标架构完成 `pydantic-core` Cargo closure、Python base OS/OCI child image 与精确 tool bundle 的 SBOM/法律文件/源码义务审计，并对 source artifact 与 SBOM 做语义核验，通过第三方发布门禁后才允许交付。

### 7.1 Iteration 0.11.0 后续精确入口

1. 0.11.0 全量回归、migration/backup targeted tests、`compileall`、离线 lock check、包构建与隐私清单已完成并记录；后续不得沿用 0.10.0 的测试数或制品。
2. 在真实迁移前，用 v0.10.0 对 Schema 8 制作备份并恢复到独立新根；在副本上迁移到 Schema 9 后，再用 v0.11.0 制作并恢复第二个基线。两次演练都要保留匹配的应用与恢复工具。
3. Stage 0 只用 CSV v2：`download` 统计已发布完整验证资产，`discover` 统计不可变 snapshot 唯一 child/source item；旧 CSV 不转换，直接 fail closed。报告生成后仍需人工审阅，不能直接写能力表或晋级 API。
4. Bilibili 保持 `candidate`。后续在已授权样本集上观测 412 频率、阶段与 cooldown 行为；不要通过 Cookie/header/代理绕过，也不要把偶发成功 probe 表述为下载能力已修复。
5. 延续 Douyin（仅在用户提供 fresh cookies 并确认授权后）→ TikTok → Instagram Stage 0 的优先级；每个 route-specific identity 分别满足样本量和连续三轮门槛。

### 7.2 Iteration 0.12.0 后续精确入口

1. 0.12.0 全量回归、package/隔离安装与隐私扫描已经完成；该节只保留历史边界，当前不得沿用 0.11.0 或 0.12.0 的数字作为 0.13.0 证明。
2. 0.12.0 已在 loopback 隔离实例验证含 thumbnail/caption 的 ready asset 列表、辅助下载和 UI；后续继续禁止用用户标题或本机路径作为下载文件名。
3. TikTok 短链真实验收只能在用户授权样本、target POSIX、受控 egress service 与 gate 明确开启时进行；保持精确 host allowlist，禁止扩大为任意子域。
4. 当时的 YouTube/Bilibili/Douyin/TXT/CSV E2E 只使用 fake adapter；0.13.0 已升级为 CSV v3/product-build identity，平台 Stage 0 必须按 v3 重新独立完成，旧 v2 不转换。

### 7.3 Iteration 0.13.0 后续精确入口

1. 这是 0.13.0 当时的历史入口，已由下方 7.4 取代：用户 `127.0.0.1:8000` 进程需在原终端正常 `Ctrl+C` 后重启；当轮验收使用独立端口，没有终止或替换该进程。不要再按本条选择版本。
2. 不得把 `validation/local/` 中的 synthetic QA 数据库、evidence 或 decision 复制到生产；仓库也不得附带任何批准数据库。
3. 真实 Stage 0 必须在 Git 仓库外的私有目录使用 CSV v3、当前精确 build identity 与 ready Schema 10 单 hard-link 数据库。Bilibili Stage 0 只接受 BV；导入后由独立复核者使用 expected revision 明确 approve/revoke。
4. 发布或维护时先正常停止所有控制面和 Worker，再替换只读 package，随后核对 checkout/wheel build identity、Schema readiness 与恢复基线；不要在运行中的包目录热替换文件。
5. 目标 Linux/root 的 8 个跳过项、Docker/UDS/ACL/Cookie/恢复门禁、真实六平台 Stage 0 和第三方再分发审计仍是下一阶段，不因 Windows synthetic 验收而解除。

### 7.4 Iteration 0.14.0 历史入口（已由 7.5 取代）

1. 当时的 8000 服务未被本轮开发/验收杀死或替换；若要切换，仍须由用户在原终端正常 `Ctrl+C`，再启动当前 v0.15 一体化入口。
2. Windows `video-download-local-app` supervisor 已在 0.15.0 完成，该条不再是未完成项。
3. 随后为 terminal failed Job 提供显式 retry/new-generation 流程与 cooldown 可见性，先用 synthetic `rate_limited → retry → ready` 锁定行为，再处理 Bilibili 的间歇 412；不得用隐式无限重试或绕过平台门禁。
4. `--cookie-source` 后续应改为只暴露一个受 ACL 保护的配置文件路径，预检队列中 credential coverage，并在 Windows 用 owner/DACL 实证拒绝宽泛继承 ACE。完成前不要把 synthetic Douyin Cookie E2E 当作真实凭据上线批准。
5. 真实 Bilibili、Douyin、TikTok、Instagram 验收仍按用户授权样本进行；Stage 0 每一精确 identity 要求 10+ 正向、独立负向与连续三轮。仓库不附带 URL、Cookie、运行数据库或人工批准记录。

### 7.5 Iteration 0.15.0 历史入口（已由 7.6 取代）

1. 用户现有 loopback 8000 服务仍不得由开发流程终止或替换。需要切换时，由用户在原终端正常停止，然后使用 v0.15.0 `video-download-local-app`；并行 QA 继续使用独立根与随机端口。
2. 当时待办的 terminal failed Job 显式 retry/new-generation 与 cooldown 可见性已由 0.16.0 完成；synthetic `rate_limited → retry → ready` 已锁定 API/UI/日志行为，仍不允许隐式无限重试。
3. 当时待办的 Worker cycle/claim barrier、SQLite DB-linearized stop gate、idle EOF 与 active Attempt lease recovery 已由 0.16.0 实现并进入聚焦回归；精确保证以 stop/claim 写事务提交顺序为准。
4. 真凭据前实证 Windows app/config/source owner 与私有 DACL；冻结 EXE/安装器只能在目标 Windows 的第三方源码、SBOM、notices、relinking 与人工批准门禁闭合后进行。
5. 真实 Bilibili、Douyin、TikTok、Instagram 验收仍按用户授权样本和 Stage 0 v3 独立完成；任何单样本、离线 E2E 或间歇 412 观测都不得转成平台批准。
6. 在 clean authorized target Linux 执行 8 个 Windows skip 的合约和完整 Docker acceptance，然后才能更新隔离部署结论。

### 7.6 Iteration 0.16.0 历史入口（已由 7.7 取代）

1. 用户现有 loopback 8000 服务仍不得由开发流程终止或替换。需要切换时，由用户在原终端正常停止，再使用最终验收过的 v0.16.0 `video-download-local-app`；并行 QA 继续使用独立根与随机端口。
2. 本轮最终 pytest、`compileall`、离线 lock、diff、source-equivalent sdist→wheel、隔离安装、metadata/entry points/legal files、build identity、依赖、隐私、高置信秘密与隔离 UI/API/日志门禁已经完成；后续任何源代码或发布证据改动都必须重复这些门禁并生成新的制品，不得复用本轮 hash。
3. 用匹配历史版本在独立根建立并实际恢复 Schema 8/9/10 基线，再由 v0.16.0 在副本上 forward-migrate，并另建、实际恢复 Schema 11 基线；目标 Linux/NAS、真实容量、独立介质与灾难主机恢复仍分别验收。
4. 真凭据前实证 Windows app/config/source owner 与私有 DACL，并继续收窄高级手动 `--cookie-source` 的 argv/history 暴露与单 Worker 全平台 source 可读范围；只用 synthetic source 先验收 credential coverage、rotation 和 durability。
5. 按用户授权样本独立完成真实 Bilibili、Douyin、TikTok、Instagram Stage 0 v3；每一精确 identity 仍需至少 10 条正向、独立负向与最新连续三轮，任何单样本、离线 E2E、间歇 412 或 synthetic retry 都不得转成平台批准。X graph 继续等待真实 stable key/exact selector。
6. 将 short-link egress 纳入 supervisor/Compose，并在 clean authorized target Linux 执行 8 个 Windows skip、AF_UNIX/owner/mode、真实 TLS/DNS/redirect/restart/replay、完整 Docker acceptance 和恢复演练，之后才能更新隔离部署结论。
7. 冻结 EXE/安装器、dependency wheelhouse、OCI image 或 tool bundle 之前，按精确目标/架构闭合第三方源码、Cargo/OS SBOM、notices、relinking、签名验证、provenance 与人工批准；项目 Apache-2.0 不解除这些门禁。
8. 继续补足真实 thumbnail/caption 样本，并在可可靠判定时区分平台人工字幕与自动字幕；不能用 synthetic auxiliary artifact 关闭该项。

### 7.7 Iteration 0.17.0 历史入口（已由 7.8 取代）

1. 本轮最终 pytest、`compileall`、离线 lock、diff、source-equivalent sdist→wheel、隔离安装、metadata/entry points/legal files、build identity、依赖与隐私门禁均已完成；后续任何源码或发布证据改动都必须重跑并生成新制品，不得复用本轮 hash。精确 archive hash 只保存在 gitignored verifier/外部验收报告，不能写回被打包源。
2. 用用户明确授权且合法的样本分别完成 Bilibili、Douyin、TikTok、Instagram 真实下载与 Stage 0 v3；进度测试、旧单样本、离线 fake、间歇 412 或 synthetic Cookie 均不能替代每个精确 identity 的 10+ 正向、独立负向和最新连续三轮。X graph 继续等待真实 stable key/exact selector。
3. 在真实平台试运行中单独观察 main video/audio 顺序流、indexed fragment、未知 total、字幕/thumbnail sidecar、后处理和失败/取消；只验证阶段估算语义，不把它包装成精确总字节百分比，也不记录原始工具输出或来源标识。
4. 在 clean authorized target Linux 执行 8 个 Windows skip、完整 Docker acceptance、AF_UNIX/ACL/core-pattern/runtime bind/恢复演练，并验证容器中的进度控制行；当前 Windows 回归不能更新隔离部署结论。
5. 真凭据前完成 Windows app/config/source owner 与私有 DACL 实证，并继续将 credential 暴露收窄到 secret sidecar、per-platform Worker 或 per-Attempt process/mount namespace；short-link egress 仍须进入受控 supervisor/Compose。
6. 冻结 EXE/安装器、dependency wheelhouse、OCI image 或 tool bundle 之前，按精确目标/架构闭合第三方源码、Cargo/OS SBOM、notices、relinking、签名验证、provenance 与人工批准；项目 Apache-2.0 不解除这些门禁。

### 7.8 Iteration 0.18.0 历史入口（已由 7.9 取代）

1. 下一轮优先接通 Windows 一体化短链展开，让普通前端可使用 Bilibili/Douyin/TikTok 等已列入范围的分享短链；现有 POSIX/UDS primitive 不等于 Windows 产品通路完成。
2. 随后接通平台默认 Cookie/profile 选择和 queued credential coverage，使普通前端任务能使用已配置的合法凭据；秘密仍不进入网页、公开 API 或普通日志。先用 synthetic source 锁定选择、禁用/过期、缺失与取消契约，再进行授权凭据验收。
3. 0.18 的并发、线程启动清理、stop、最终全量和静态结果已记录；无需把本轮已完成审核重新列为下一功能。未来任何源码变更仍需新回归和新制品，不得复用 0.17/0.18 数字或 archive hash。现有开发环境使用 `.venv\Scripts\python.exe`，未经依赖变更任务不要先 `uv sync`。
4. 保持一个 Worker 进程、双槽、单平台上限、连续补位和清理完成前保留槽位的契约；one-shot 只执行一项，`--check` 永不领取。停止时区分正常 EOF 收尾与 Ctrl+C/异常中断，受控 `WORKER_LOST` 为 terminal/manual retry。
5. 继续 Bilibili → Douyin → TikTok → Instagram 的授权样本及 Stage 0 验收，不因并发、spawned preflight 或旧单样本提升 `candidate`；目标 Linux/Docker、真凭据权限与第三方再分发仍单独闭合。
6. 用户既有 loopback 8000 服务不得被开发流程终止或替换；本轮未更新它，也没有新增真实网络/Cookie/UI 证据。没有新的 commit/push 或第三方再分发授权。精确最终包身份和哈希以包外冻结后重建报告为准。

### 7.9 Iteration 0.19.0 历史入口（已由 7.10 取代）

1. Windows 短链和默认 Cookie 前端通路已经接入，不再重复列为待实现功能；先读本轮证据与包外 final report，不将旧 0.18 package hash 用于当前源码。
2. 后续优先验证完整 spawned local-app 的实际短链/下载行为，按 Bilibili → Douyin → TikTok → Instagram 使用授权样本；真实 Cookie 需要用户配置，不能从历史 profile 推断默认或从浏览器自行提取。工程 fake/synthetic 不提升 `candidate`。
3. `--check` 允许原有 SQLite 初始化、日志和 claim gate 写入，但不领取、不打开浏览器、不注册 profile、不改变 Job 凭据。正常启动只对 v2 明确选择的平台准备默认；缺失/重复/禁用/到期或配置漂移应失败，不能悄悄匿名。
4. JSON/import 默认 `use_default`；未配置的平台匿名。显式 `anonymous` 可用于新任务/failed flat retry；retry 无 body 保持旧绑定。source-level live/ready 去重仍不新建任务、更不改既有 Cookie，页面已说明。
5. 一个 Worker、双槽、单平台上限、stop/claim fencing、私有 Attempt 副本清理与脱敏日志继续保留。保持 `.venv\Scripts\python.exe` 开发环境；无依赖变更时不要 `uv sync`。该历史轮次没有 commit/push 授权。
6. 当前临时浏览器 smoke 为独立 synthetic control-only 18820，已完成两种模式创建/取消并停服；未启动或替换用户 8000。后续目标 Linux、真实私有 DACL、安装器和第三方 binary/container 再分发仍独立验收。

### 7.10 Iteration 0.28.0 当前精确入口

1. T19 已实现独立 AI runtime、持久化 AI task/timeline、原 Editing Schema 3、标准音色配音和 URL→下载→编辑→所选三平台上传草稿的可恢复编排；T20 已追加真实 upload outcome、逐操作 AI 精确 authorization 与多输出 fan-out，当前编辑库为 Schema 4，自动流程为 Workflow Schema 3。保存一次完整预设后可只更换 URL；来源标题和逐账号最终标题只在下载 ready 时冻结一次，冻结后 asset 与标题快照都不可换绑。上次预设只在预设、账号和 AI capability 成功读取且用户未编辑时恢复。先读[来源标题冻结记录](validation/iteration-0.28.0-workflow-source-title.md)、[多分段自动流程记录](validation/iteration-0.28.0-multisegment-workflow.md)、本轮其他证据、`docs/AI_RUNTIME.md` 与 `docs/EDITOR.md`，不要把 runtime 完整性或 synthetic/offline 验收写成真实云模型或平台通过。
2. AI authorization 的摘要绑定 runtime ID/version、protocol、manifest、provider kind、model ID/本地声明 revision、operation、data egress 与有效 limits；task request、dubbing recipe 和 Workflow profile 保存该值。创建、确认、worker 与 provider 前都要与当前能力重新比对；旧记录可读但不能继续执行，须按当前能力重建。
3. 自动流程允许完整视频、单个分段或首尾连续的多个分段；有分段时只把首段起点至末段终点的连续派生音频发送给听写，含间隙的多段在远端 task 创建前拒绝。在 provider 前执行 30 分钟/25 MiB 硬上限。翻译最多 1000 cues、60000 输入字符和 20 个按 50 cues 估算的调用单位；TTS 最多 600 cues、60000 输入字符和 600 次调用。这些是本地输入/调用上限，不是供应商价格或已计费 usage ledger。
4. 渲染按每个分段裁出并归零字幕/配音时间轴；边界切入 cue 时拒绝，纯 B-roll 分段使用空字幕与本地静音。保留原声时固定压到 22% 后再混入配音。
5. 只有 immutable profile 已预授权、且域恢复明确证明每个待重新排队的当前非终态项仍为原始 queued 并从未 dispatch 时，AI task、配音/渲染计划或上传 leaf 才会在重启后重新校验并续跑；同批已成功的上传 leaf 可保持终态。手动任务、所有 retry leaf、running/canceling、账本 dispatched/unknown 与上传 unknown 必须停下。Workflow 冻结账号 `session_revision`；每个 segment/source/account/platform slot 独立持久化，逐段创建失败从 durable prefix 幂等恢复，最多 30 个草稿只经一次完整批量确认。待确认任务若经历重新登录会以 `account_session_changed` 停止；上传 retry lineage 只接受账号、来源和平台不变的唯一后继链。
6. 原始下载、编辑源、每次 render staging、ready 编辑成品与上传媒体保持不同受管副本；不得覆盖下载原件。媒体输入按受管后缀固定 `mov`/`matroska` demuxer、只允许 `file` protocol，并严格复核 format name，不能恢复为内容自动探测。
7. Editing Schema 4 的远端调用 ledger 已冻结可脱敏 request identity，并用 `reserved`、`dispatched`、`responded`、`released`、`unknown`、`reconciled` 表达本地状态；unknown 只允许三项固定人工结论。阻断会递归覆盖已经存在的 retry 后继，自动流程在人工核对后重新检查 owner，只有 `not_accepted` 恢复原有显式重试。ledger 与多分段 ignored validator、compileall、当前内联 JS、依赖一致性和本机浏览器检查已通过；当前相关既有回归 263 passed，文档/发行回归 102 passed，测试文件保持未改。`tests/test_api.py` 当前另为 19 passed、1 failed：既有版本断言仍期待 `0.27.0`，而项目已是 `0.28.0`；按仓库策略不修改测试，因此完整 CI 尚不能标绿。预设已追加配音授权摘要、严格嵌套参数/文件摘要校验；生产自动流程页现可完整恢复 0–10 段，并显示最多 30 个投稿任务的 fan-out。真实域离线整链实际调用听写/翻译/配音各一次，并确认配音进入三平台上传的视频；网络与模型响应仍由合成替身提供。远端 alias 漂移、实际价格与平台结果仍须外部验收。
8. 新保存的 ready translation recipe 冻结精确 `revision_id`，计划继续使用既有 `plan_timeline_bindings` 冻结译文和父听写摘要；legacy recipe 只在完整匹配唯一时恢复，歧义时要求明确重选。mixed-state 计划可供审阅，但 dubbing 未 ready 时不能确认排队。验证详情见[译文精确修订绑定记录](validation/iteration-0.28.0-translation-revision-binding.md)。
9. 上传 `waiting` 快照以 `needs_confirmation` 明确区分仍含 draft 与仅含 queued/running；attention、awaiting 和 uploading 三处都按该字段恢复。默认值为 `true` 以让未更新适配器失败关闭；本地适配器仅在确认已完成时明确给出 `false`。详见[上传 attention 恢复记录](validation/iteration-0.28.0-workflow-upload-attention-recovery.md)。
10. 当前实际应用根已完成本地 runtime 准备：AI runtime 完整性 verified，Upload runtime/backend、scheduler 与 worker ready，上传数据库经私有停机备份后从 Schema 1 迁移到 Schema 3 并保留既有记录。该 packaged 开发宿主必须让 Setup 与 Start 使用同一 canonical `--app-root`；不要放宽 alias/reparse 检查，也不要把私有解析路径写入仓库。AI provider 仍因无密钥保持 `provider_health_required`，真实调用与平台 session/发布尚未验证。详见[运行时刷新记录](validation/iteration-0.28.0-local-runtime-refresh.md)与[Debug 指南](docs/DEBUG_GUIDE.md)。
11. 开发仍使用 `.venv\Scripts\python.exe`，依赖检查用 `uv pip check`。最终验证材料放在 ignored `validation/local/`，提交前运行 `scripts/verify_commit_scope.py --staged`；不得新增或修改 `tests/`。
12. 用户已授权关键开发步骤完成后直接创建本地 Git commit，并要求本轮验证后正常合并远端历史、直接 push 到 `origin/main`；禁止 force push。真实平台操作、云端 AI 调用、GitHub Release 与第三方二进制再分发仍未授权。方向 C“编辑式玻璃”已用于生产四页并完成当前 44/44 Chromium 复验；后续新增控件继续沿用 `DESIGN_SYSTEM.md` 1.4。

建议技能：实现/故障回归用 `tdd` 与 `diagnose`；需要刷新交接时用 `handoff`，并保留本文件的历史证据边界。对应文件已有完整实现与测试说明，不必复制源码进入交接。

## 8. 迭代历史

- **0.28.0 — 2026-09-08 至 2026-09-10**：完成 T19 隔离 OpenAI runtime、持久化听写/翻译与时间轴审核、segment-local 字幕/标准音色配音、URL 自动流程、上传批次原子确认、账号登录 revision 绑定和 retry leaf 对账；发布后开发又收敛 upload outcome，加入逐操作 AI authorization、输入/调用硬上限、Editing Schema 4 脱敏远程调用账本、Workflow Schema 2 多输出及生产 1–10 段界面，并将用户选定的方向 C“编辑式玻璃”落入四张生产页后完成当前 Chromium 44/44 复验；随后把 ready translation recipe、计划与新 workflow AI-ready 草稿绑定到精确批准 `revision_id`，legacy 歧义恢复失败关闭，并以显式上传快照字段修复 attention 恢复时跳过草稿确认或重复确认已排队任务的问题。当前实际应用根又在保留旧 runtime 和私有 SQLite 备份后完成 AI/上传 runtime 构建验证及 Upload Schema 1→3 迁移；实际 Start 验证本地下载、上传 scheduler/worker 和四页入口，AI provider 因无密钥按预期停在 `provider_health_required`。unknown 结果只能人工 reconciliation，完成与重试按账本失败关闭。本地 runtime/synthetic/浏览器工程证据不代表真实 OpenAI 质量、实际费用、三平台发布或新的发行冻结；以各轮证据和包外 receipt 为准。

- **0.27.0 — 2026-09-07**：以 `d358f2a338129a9eec81e4055249b968b0068fbd` 为起始基线，完成 T18 非破坏性编辑工作台、分段与封面、下载→编辑→上传显式复制链路，以及 AI provider/capability/timeline 合同。AI runtime、模型、推理、试听、真实素材范围、真实平台与最终 release receipt 仍未完成。

- **0.26.0 — 2026-09-07**：完成 T17/G7 三平台独立标题、简介、标签、受管封面、定时与平台字段，以及 Upload Schema 3/备份格式 2；真实平台逐字段接受、定时触发、封面裁切、发布和最终发行制品保持未验收。

- **0.25.0 — 2026-09-07**：以 `1178869bbf35212e734d3fda70876033e309a99d` 为起始基线，完成 T16 下载/上传生产前端的共享 Apple 风格、主题、本地静态资源、响应式、状态稳定与无障碍降级，本地 G6 已完成。该基线不是最终提交；冻结全量、新 clean commit、五件制品、源码/wheel 独立安装与最终身份只由包外 receipt 记录。没有新增真实登录、扫码、下载、上传、平台审核、GitHub hosted CI 或目标 Linux/Docker T15 结论。

- **0.24.4 — 2026-09-07**：完成 T14 必要数据生命周期切片、T07 上传停机备份/新根恢复、T08 有界 synthetic 韧性切片和 T09 本地 CI 合同，并准备 T10 版本与发行门禁。该版本保留为 T16 前的中间冻结源历史；没有同批包外 receipt 时，不得把其源码或定向结果称为已通过制品冻结，也不得借给 0.25.0。

- **0.19.0 — 2026-09-04**：Windows 一体化短链、JSON/TXT/CSV threadpool、v2 显式平台默认 Cookie、事务内新任务与重试绑定、匿名选择与可用性 UI/API。新增来源/复制/去重/竞态/日志/前端回归，修复新接口导致日志误降级；最终 `1410 passed, 8 skipped in 135.51s`，静态与35文档链接检查、浏览器复验、真实 spawned synthetic `--check` 和 source-equivalent package 预检通过。最终包依冻结契约另行重建，hash只在包外报告；无新真实平台、真 Cookie、Linux/installer 或 push 结论。

- **0.1 — 2026-09-02**：FastAPI/SQLite、MVP URL、Batch/Input/Source/Job；`18 passed`。
- **0.2 — 2026-09-03**：migration、lease/Attempt/retry/cancel、AssetStore 和 fake E2E；`75 passed`。
- **0.3 — 2026-09-03**：v0.3.0 / Schema 6；intent/crash recovery、queue/circuit、proxy/relay/guard、ffprobe/yt-dlp 候选；`383 passed`。
- **0.4 — 2026-09-03**：v0.4.0；sidecar 资产、candidate Worker、Cookie copier、short-link resolver、Compose 骨架；`459 passed`。
- **0.5 — 2026-09-03**：v0.5.0 / Schema 7；CredentialProfile、backup/restore 和 ADR-0001；`501 passed`。
- **0.6 — 2026-09-03**：v0.6.0 / Schema 8；X attachment graph、fan-out、exact child fake、generation/partial success/cancel/rediscover、语义恢复；`615 passed`。
- **0.7 — 2026-09-03**：v0.7.0 / Schema 8；authenticated/replay-safe short-link UDS transport、remaining Batch budget、resolver persistence boundary；deployment-owned Cookie wrapper/validator + descriptor copier + durable rotation contract；exact/clean/fail-closed Linux runner；`735 passed, 8 skipped`。Docker/Linux/真实 Cookie/平台/Stage 0 均未执行。
- **0.7.1 — 2026-09-03**：v0.7.1 / Schema 8；修复前端脚本转义、错误处理、轮询竞态、双输入拒绝、按钮恢复、repository clock 与同毫秒 FIFO；完成 synthetic 浏览器/fake Worker/资产/备份恢复验收及许可证 fail-closed 整改；`750 passed, 8 skipped`。项目仍 proprietary，公开/容器发布保持 blocked。
- **0.7.2 — 2026-09-03**：v0.7.2 / Schema 8；新增控制端、Worker、candidate Worker、子进程的 allowlist 脱敏 JSONL、轮转、读取 API、前端面板与备份排除；完成 synthetic 浏览器、日志泄漏标记、顺序/关联、备份恢复和发布包回归；`788 passed, 8 skipped`。项目仍 proprietary，使用需权利人身份或书面授权，公开/容器发布保持 blocked。
- **0.8.0 — 2026-09-03**：v0.8.0 / Schema 8；接入固定 Windows x64 yt-dlp/FFmpeg/ffprobe、逐文件 hash 与 version/configuration 校验、离线 synthetic smoke、tools API/UI/脱敏日志和 zipimport candidate Worker 路径；`846 passed, 8 skipped`，隔离 wheel smoke r3 通过。工具 `ready` 不等于隔离 Worker/联网/平台验证，真实 standalone downloader 仍未完成；公开再分发保持 blocked。
- **0.8.1 — 2026-09-03**：v0.8.1 / Schema 8；对用户指定的 YouTube/X 各一个 URL 完成无 Cookie 一次性真实下载、ffprobe、SHA-256 与全流解码；新增受校验的可选显式单一 JS runtime 接线，未配置时维持 `--no-js-runtimes`，并为 yt-dlp probe/download 固定 UTF-8。`853 passed, 8 skipped`；0.8.1 sdist/wheel、隔离 wheel 安装、法律文件/命令入口、源码一致性和本机 0.8.1 服务复验均通过。该证据仍不是前端/API/candidate Worker/AssetStore E2E，Stage 0 与平台级验证未完成，公开再分发保持 blocked。
- **0.9.0 — 2026-09-03**：v0.9.0 / Schema 8；新增 Windows 本机 direct Worker、显式启用/直连确认/单实例锁，最近批次与 ready 资产列表/下载 API/UI，并把 `local-worker` 纳入结构化日志。最终 v3 完成用户指定 YouTube/X 双样本从 API 队列到 AssetStore/manifest 和 API/UI 下载的真实 E2E；输入 URL、运行时 UUID 与媒体指纹不进入公开记录，API 下载 hash 与本机 manifest 一致、完整解码 clean、浏览器无 error/warn。最终回归 `881 passed, 8 skipped`；0.9.0 sdist/wheel、隔离安装、11 个命令入口、31 个法律文件与包外哈希均通过。项目仍 proprietary，公开/容器/二进制再分发保持 blocked。
- **0.9.1 — 2026-09-03**：项目自有源码、文档与脚本迁移到 Apache-2.0，加入 `NOTICE` 与双版权人声明；公开隐私清理移除本机路径、位置时区、真实验收 URL/账号/运行 UUID/媒体指纹。版本独立于 proprietary 0.9.0 制品；全量回归 `882 passed, 8 skipped`，Apache sdist/wheel、隔离安装、32 个法律文件和 11 个入口通过；第三方 binary/container/tool-bundle gate 保持 blocked。
- **0.10.0 — 2026-09-03**：新增六平台静态能力矩阵和 `platform × source_type × job_kind` Worker claim 过滤；接入 TikTok 单视频、Instagram Reel，收紧 Bilibili 默认分 P 并明确 TikTok 短链 deferred；扩展六平台 Cookie/acceptance 资产与能力 API/UI。修复 Worker `max_height` 透传、不受限 `/b` 格式回退、fresh-cookie 错误分类，以及高度策略无匹配格式时误触平台熔断；Instagram 一个 NASA 官方公开 Reel 已完成 Windows direct/no-cookie/Node `1/1 ready` 全链路，Bilibili 尝试遇 HTTP 412，Douyin 无 Cookie 尝试为 `authentication_required`，TikTok 未实跑。真实 Stage 0 未执行。最终回归数字见本文件验证表；`compileall`、`uv lock --check --offline`、`git diff --check` 通过；0.10.0 sdist/wheel 离线构建成功，临时验证产物已删除。
- **0.11.0 — 2026-09-03**：v0.11.0 / Schema 9；Stage 0 CSV v2 将 `job_kind` 纳入样本、结果、报告与能力证据身份，route-specific output count 分别覆盖完整验证下载资产与不可变 discovery snapshot child/source item。Schema 8 能力行保守迁移为 `download`；旧 CSV fail closed，报告不自动写能力表/API。Bilibili 固定工具、同一公开样本、无 Cookie 重复诊断中 raw `2/2` 成功、产品 fresh attempt `4/6` 成功且 `2/6` probe HTTP 412；仅 Bilibili 受限 412 映射 `rate_limited` 以退避/冷却，不声称稳定下载或 `verified`。最终回归 `935 passed, 8 skipped`，0.11.0 package、隔离 API/UI 与隐私检查通过；Apache-2.0 与第三方再分发门禁不变。
- **0.12.0 — 2026-09-03**：v0.12.0 / Schema 9；ready asset 列出 thumbnail/caption，并新增严格辅助下载端点/UI；损坏 sidecar 被隔离，较大辅助文件经验证后分块发送且断连显式释放；TikTok `vm`/`vt` 短链进入默认关闭、精确 hostname allowlist 的受控 resolver；YouTube video/Shorts、Bilibili BV/av、受 gate 保护的 Douyin 短链与 TXT/CSV 完成离线 fake E2E。最终回归 `969 passed, 8 skipped`，隔离 synthetic API/UI 三类下载、source-equivalent Apache package 和隐私复核通过；真实平台/Stage 0 结论不变，第三方再分发门禁不变。
- **0.13.0 — 2026-09-04**：v0.13.0 / Schema 10；Stage 0 CSV v3 将完整 package build identity 纳入七字段 evidence identity，Schema 9 能力行只读封存，新增不可变 evidence、append-only decision chain、revision CAS、current view、治理 CLI 与单事务 UI snapshot。构建摘要、数据库文件 identity、提交前复核、readiness cache/schema cookie 与错误脱敏均 fail closed；Bilibili Stage 0 暂只接受 BV。最终全量回归 `1072 passed, 8 skipped in 81.80s`，隔离 synthetic `3/3 ready` UI/API/日志、source-equivalent Apache package 与隐私复核通过；本轮无真实平台请求，真实 Stage 0、Linux/Docker 与第三方再分发门禁不变。
- **0.14.0 — 2026-09-04**：v0.14.0 / Schema 10；Windows 本机 Worker 接入按平台 Cookie source 和 Attempt-private 副本，新增不领取 Job 的 `--check`，拒绝空/超限/可写/link/物理复用 source，并用独立 preflight 日志事件；Bilibili Stage 0 BV-only 等价身份回归进一步加固。最终全量回归 `1084 passed, 8 skipped in 90.42s`，独立 synthetic `3/3 ready` CLI/API/UI/日志、source-equivalent Apache package 与隐私复核通过；无真实 Cookie/平台请求，Windows DACL、目标 Linux/Docker、真实 Stage 0 与第三方再分发门禁仍未完成。
- **0.15.0 — 2026-09-04**：v0.15.0 / Schema 10；新增 Windows `video-download-local-app` 一体化 supervisor、固定 app/data/database/tool 配置、私有三阶段握手与 HTTP 身份检查、端口预占、单实例、Windows Job Object、Worker-first 停机、严格 launch/claim/check-complete 单字节命令、EOF 停机和三组件共享 `run_id` 日志；补上无副作用的 control `--help`。真实 Windows `--check`、10 次重复、singleton、Ctrl+C/SIGBREAK、worker/control 强杀、隔离 UI 与安全失败输入均收敛；最终全量 `1196 passed, 8 skipped in 93.06s`，source-equivalent Apache package 与隐私复核通过。无真实 Cookie/新平台请求；DB-linearized stop/claim、Windows DACL/安装器、目标 Linux/Docker、Stage 0 与第三方再分发门禁仍未完成。
- **0.16.0 — 2026-09-04**：v0.16.0 / Schema 11；新增 exact-DDL/no-trigger、run-scoped `worker_claim_gate`，supervisor 以 prepare→activate→stop 管理本次 `run_id`，stop 与 `claim_next()` 通过 SQLite `BEGIN IMMEDIATE` 建立唯一提交顺序，stop 写后重读失败时先 fail-safe 终止 owned Windows Job。已先领取的 active Attempt 不被倒写撤销，lease 到期后旧 Attempt 以 `abandoned/worker_lost` 收尾并由新 Attempt 重领。终态失败 flat Job 可经 API/UI 显式进入新 `run_generation`，不绕过 cooldown/circuit；UI generation guard、claim gate 生命周期日志及 state/cause 配对已加固。最终全量 `1238 passed, 8 skipped in 104.14s`；真实 Windows 隔离 UI/active-lease/日志与 project-only source-equivalent Apache package/隐私复核通过。本轮无真实平台/Cookie/Stage 0/Linux/Docker/安装器/第三方发布结论。
- **0.17.0 — 2026-09-04**：v0.17.0 / Schema 11；新增固定脱敏 yt-dlp stdout progress/phase 协议、有界完整行观察器、sidecar 忽略、顺序双流与 indexed fragment 保守聚合、未知总量 heartbeat、持久化 postprocessing，以及 API/UI 中文阶段、约百分比、原生 progress/ARIA。阶段性回归 `186 passed in 24.95s`、`149 passed in 13.66s`、确定性握手 `2 passed in 1.34s`，隔离 synthetic 浏览器 QA 通过并未触碰既有 8000；最终全量 `1270 passed, 8 skipped in 106.62s`、静态门禁和 project-only source-equivalent Apache package/隐私复核通过。本轮无真实平台/Cookie/Stage 0/Linux/Docker/安装器/第三方发布结论。
- **0.18.0 — 2026-09-04**：v0.18.0 / Schema 11；修复 Windows 实际入口串行执行，接入单 Worker 进程双槽调度、跨平台重叠、单平台上限与连续补位；拆分 claim/execute，保留 live cleanup 槽位与排除项，仅本地静默时回收；暂停持久化失败锁存，共享 subprocess stop 信号用于 Ctrl+C/异常。实际入口先 red 后 green，heartbeat/reader 启动失败清理修复；最终全量 `1307 passed, 8 skipped in 118.43s`、静态门禁、独立 spawned app `--check` 和 source-equivalent Apache package 预检通过，冻结契约与最终重建精确身份由包外报告绑定。没有新增真实网络、Cookie、浏览器或平台/第三方发布结论。
