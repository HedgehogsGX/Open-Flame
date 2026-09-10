# Open-Flame 后续执行计划

路线制定：2026-09-05；进度更新：2026-09-11。本文保留工作包、验收门槛与依赖顺序；当前源码身份、能力边界、风险和唯一下一入口以 [HANDOFF](../HANDOFF.md) 为准，逐项结果以 [validation 索引](../validation/README.md) 链接的独立 evidence 为准。当前开发版本为 0.28.0：Download/Editing/Upload/Workflow Schema 分别为 11/4/3/3，Workflow preset Schema 为 2，上传备份格式为 2；首批上传平台仍限 Bilibili、抖音和视频号。0592b6f 的五件制品与包外 receipt 只证明该冻结构建，之后源码尚无新 clean receipt；真实 OpenAI、真人试听、三平台发布与目标 Linux/Docker 验收均未完成。

架构精简 S1–S8 已按可独立回退的小切片完成：HTTP guard，公开 profile/metadata/identity 契约，AI/Upload/Edit snapshot 解释，verified media response，EditingManager，受管文件身份/读取，以及 Workflow 上传表单与 recipe 分责均已收敛；各域权限、事务、错误、确认与 capability 边界保持。HANDOFF 现只保留当前身份、能力、风险与下一入口，validation README 只做证据索引；历史结果继续留在各自 evidence 和 Git 历史。仓库 hook 与 hosted CI 已复核为共用 scripts/verify_commit_scope.py，分别检查 staged diff 与事件 merge-base 净差，允许只删除旧测试。下一入口统一为接收外部测试反馈、修复可复现问题，再固定精确 clean candidate；真实 OpenAI、真人试听与三平台发布仍需对具体动作另行明确授权。WorkflowStore 或更短 handler 只在能够删除现有重复且故障恢复语义可逐项证明时再提取。来源封面偏好沿用该评审的核心判断：保持本地模块化单体，以 Download 的窄只读 resolver 接入既有 Workflow/Upload 边界，不复制下载状态、不增加跨域数据库或运行服务。

2026-09-11 的发布后轻量切片新增可选来源字幕优先策略：Workflow 按 editing project 中的
source asset 枚举已登记 ready caption，只对唯一匹配的 SRT/WebVTT 进行 2 MiB 有界读取和
完整 identity/hash 复核，再通过 Editing Schema 4 既有 timeline/requests 导入为待审核
transcription。批准后复用既有 translation、dubbing、render、upload 路径；不适用时才回退
已冻结授权的 AI transcription。该切片没有增加 Schema、服务、runtime、依赖或通用抽象，
详见[来源字幕优先复用证据](../validation/iteration-0.28.0-workflow-source-caption-reuse.md)。

同日的来源封面偏好也是显式 opt-in，并要求 recipe 保留生成封面 fallback。Workflow 只按冻结
`download_asset_id` 接受同一 ready original 的唯一 registered thumbnail，经受管 resolver 与
Upload `expected_sha256` 双层复核后，只有兼容全部所选平台封面槽才采用；否则使用生成封面。
Workflow 先纯读取选定确定性受管 ID 并 CAS 写入 `upload_cover_id`，之后才允许 Upload 持久写入；
已有请求从不可变 jobs 的共同封面槽恢复，重放与取消只以元数据身份收回任务。尚无请求时取消会
先原子写入专用空 tombstone，重启不再读取可变 Editing 成品或 Download 封面登记，并继续取消
前序分段 jobs；sentinel digest 异常时失败关闭。
多分段、崩溃重启和响应重放因此保持同一选择。
该切片未增加服务、数据库、Schema、runtime、依赖或 tracked tests，见
[Workflow 来源封面偏好证据](../validation/iteration-0.28.0-workflow-source-cover-preference.md)。

## 1. 目标、边界与完成定义

当前阶段目标是在不改变首批 **Bilibili、抖音、视频号** 上传边界的前提下，冻结并验收已接线的中间编辑、隔离 AI 听写/翻译/标准音色配音和 URL 自动流程。小红书等其他上传平台、多 P/合集、声音克隆及本地 AI provider 后续维护。

用户在核验后补充的前端目标最初由 T16 以 Apple 风格建立共享基线；2026-09-09 用户进一步选定方向 C“编辑式玻璃”作为整站生产风格。下载、编辑、上传与自动流程四页、[设计规范](DESIGN_SYSTEM.md) 1.4 和[交互式视觉基准](design-preview.html)已经同步，当前禁止远端的 Chromium 矩阵为 44/44 PASS，见[编辑式玻璃四页生产前端证据](../validation/iteration-0.28.0-editorial-glass-frontend.md)。后续新增界面继续沿用同一规范。

2026-09-09 实际启动路径审查发现，普通 Start 原本会在 spawn control child 前剔除 `OPEN_FLAME_AI_OPENAI_API_KEY`，使已安装 runtime 也无法进入 AI provider 健康/执行路径。当前已收敛为精确的 control-only 密钥白名单与值校验，下载 Worker 仍剔除该密钥；同一源码 Setup 可从固定核验的本地 CPython 3.13.15 ZIP 在 `<app-root>/data-ai-runtime` 原子构建或只读复用 AI runtime，也可用 `--upload-runtime` 从同一应用根派生 `data-uploads` 并复用既有上传安装器。两条入口的合成 sentinel、临时 runtime、路径、锁、复用和失败矩阵均在禁网条件下通过。2026-09-10 已在当前实际应用根保留旧上传 runtime、以 SQLite backup API 保存 Git 外迁移前副本，然后构建/验证两个 runtime；上传库从 Schema 1 迁移到 Schema 3 并保留既有记录。packaged 开发宿主的 Windows filesystem virtualization 会使普通 `%LOCALAPPDATA%` 词法路径与最终解析路径不同，因此本环境由 Setup 与 Start 共同使用经人工核对的 canonical `--app-root`，未放宽路径安全检查。AI provider 仍因无密钥保持 `provider_health_required`；真实 OpenAI 及三平台验收仍是后续门槛。见[当前应用根运行时刷新记录](../validation/iteration-0.28.0-local-runtime-refresh.md)。

“完成”需要同时满足：本轮已确认问题有明确处置、相应真实模式回归通过、用户能确认当前运行状态、测试包可独立安装并记录身份、三平台结果分别由测试员核对。全量测试绿色、环境 ready、二维码显示或本地 draft 均不能单独替代这些条件。

本文件保留完整路线及验收门槛。2026-09-05 用户要求修复八项发现后，0.24.3 已实施 T01～T06 所对应的缺陷修复，以及 T07 的“已有上传库结构校验”部分；其审查范围见 [最终源码审查](../validation/iteration-0.24.3-final-review.md)。2026-09-07 的 0.24.4 冻结源继续完成 T14、T07～T09 的本地切片，v0.25.0 完成 T16/G6，v0.26.0 完成 T17/G7，v0.27.0 完成 T18。本轮 v0.28.0 实现 T19 的隔离运行时、可审核 AI 任务和自动流程；任何历史源码身份、定向结果或包均不能作为 0.28.0 最终制品验收。

| 工作包 | 当前可确认状态 | 后续门槛 |
| --- | --- | --- |
| T14 | 必要生命周期切片在本地完成 | 去重、总配额、自动孤儿清理仍为后续优化 |
| T07 | 上传备份格式 2 / Upload Schema 3 可保存受管封面、定时和平台参数；旧格式 1 / Schema 2 只读迁移已有本地回归 | 真实容量、异机/offsite、NAS 与人工值班演练未做 |
| T08 | 三平台串行、300 轮本地轮询和复制中断清理的有界切片完成 | 更广故障矩阵按剩余风险继续补充，不把本切片扩写为长期生产压测 |
| T09 | 托管 Windows/Linux × CPython 3.12.10/3.13.14 四格已运行，测试前环境和门禁均通过 | 完整 pytest 仍红；required checks / branch protection **NOT CONFIGURED** |
| T10（0.24.4 历史阶段） | 版本号、第 14 个 wheel 命令入口及源码侧发行门禁已接线 | 只保留为 0.24.4 源码准备记录，不能证明 0.25.0 制品 |
| T16 / G6（0.25.0 历史阶段） | T16.1～T16.7 本地开发与禁止远端调用的 G6 验收完成 | 保留为前端基线，不能证明当前制品或平台能力 |
| T17 / G7（0.26.0 历史阶段） | 三平台独立标题/简介/标签、受管封面、定时和平台专属字段完成 | 真实平台逐字段接受、定时触发、封面裁切和发布结果 **NOT RUN** |
| T18 / G8（0.27.0 历史阶段） | 独立 Editing Schema 1、版本化草稿、多个分段、封面、确认/取消/重试、真实本地 FFmpeg 与显式导入上传完成 | 由 0.28.0 T19 接续；该阶段证据不证明 AI 或真实平台 |
| T19 / G9（0.28.0 当前阶段） | 当前 Editing Schema 4（T19 冻结时为 Schema 3）、隔离 AI runtime builder、OpenAI 标准库 provider、segment-local 字幕/配音、Workflow Schema 3（Schema 2 阶段已完成多输出 fan-out）、域级重启撤回、显式 retry 再确认、账号 login revision 与三平台上传 retry leaf/批次原子确认已实现；T20 仅让原始预授权且可证明未 dispatch 的 queued 工作在重启后重新校验续跑。生产页可配置/恢复最多 10 个输出，乘以最多 3 个账号形成 30 个精确 slot，逐段准备可从 prefix checkpoint 恢复，全部草稿只经一次批量确认；来源标题模式在下载 ready 时冻结公共标题、逐账号最终标题与 asset 绑定 | 真实 OpenAI 调用、真人试听、费用核对、三平台真实发布及最终发行制品仍待外部验收 |
| T20（0.28.0 发布后开发） | 已区分投稿接收、草稿保存和合法混合 outcome；账号失效撤回同账号 queued 确认；成功响应后轮换浏览器幂等键；workflow reconciliation 有界退避；逐操作 AI authorization 与输入硬上限已完成；Editing Schema 4 已保存脱敏远程调用状态，unknown 只能经三项固定结论人工 reconciliation；普通 Start 已实现仅在操作者显式配置时由 control child 继承精确的 OpenAI 密钥变量，本轮未配置密钥；普通源码 Setup 已可选构建/复用 AI runtime，并复用既有上传安装器准备 upload runtime；`/workflows` 已在创建前显示四项即时状态，服务端在写 workflow 与建下载前用真实域合同复核并冻结账号 session revision；原始预授权 queued AI/render/upload 可安全重启续跑；整流程取消会持久停止意图、按最远下游调用域取消，对 checkpoint gap 以稳定请求键发现，并核对 Editing 配方与 Upload v2 完整发布参数摘要；来源标题/字幕及显式来源封面偏好均复用冻结 asset 与既有 checkpoint；pending upload slot 的成功证据保持人工核对，running/unknown/部分远端成功同样保守等待或核对；当前实际应用根的 AI/上传 runtime 已验证，上传数据库已由 Schema 1 迁移到 Schema 3 并保留原记录 | 预设边界/API/生产页面浏览器、服务端零副作用预检、重启续跑、整流程取消、真实域离线整链、两个 Setup runtime 入口及当前应用根实际 Start 已通过；AI provider 因无密钥保持 `provider_health_required`，尚无真实 OpenAI、真人试听或平台接受证据；来源封面真实 URL 提取与平台接受同样未验证，视频号没有专用下载 extractor；`request_units` 是本地 envelope 估算，不是价格、精确 HTTP 数或 usage receipt |
| T11 / T12 | **NOT RUN** | 三平台真实上传与当前六平台下载必须绑定最终 0.28.0 receipt 后的同一构建分别执行 |
| T15 | **NOT RUN** | Linux、Docker 与 NAS 仍需目标环境独立验收；Windows 本地 UI 结果不能替代 |

以下是 0.24.4 冻结准备阶段的历史定向结果，不能单独替代 T10，也不能借给 0.25.0：上传相关精确集合为 **383 passed in 100.29s**；activity lock/上传备份/CLI 为 **133 passed in 49.68s**；包含 Windows 发布离线门禁的下载备份/发行/CI/验证/部署/API 组合为 **287 passed、4 skipped in 44.13s**，其中 4 个 skip 是 Windows 上的 root/POSIX/getfacl 环境合同。0.25.0 冻结 commit 的全量结果和源码/制品身份只记录在本轮新生成的包外 release receipt。

0.24.4、0.25.0 T16、0.26.0 T17、0.27.0 T18 与当前 0.28.0 T19 的本地验收均未进行真实 OpenAI 调用或媒体上传。T18/T19 的真实媒体处理只使用本机 synthetic 视频。T11、T12 与 T15 当前均未运行；GitHub hosted CI 已实际进入四格完整 pytest，但测试仍红，不能称为门禁通过。外部开发者和测试员先按[完整测试手册](../TESTING.md)固定身份与执行边界，再按[上传测试计划](UPLOADER_TEST_PLAN.md)逐项决定真实动作，并用[回传模板](EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)留下后续开发入口。

## 2. 执行顺序与阶段门槛

```text
A. 修复上传异常恢复与运行时校验（T01、T02）
           ↓ G1：关键故障可解释、无自动重传
B. 完成资产边界、状态展示与文档（T03～T06）
           ↓ G2：正常/异常/容量状态可操作且口径一致
C. 数据生命周期、恢复与 0.24.4 发行准备（T14、T07～T10，历史阶段）
           ↓ G3：源码侧门禁和恢复范围完成；历史结果不迁移身份
D. 前端完整升级与本地回归（0.25.0，T16.1～T16.7）
           ↓ G6：本地无远端页面、视觉、交互与发行载荷合同验收完成
E. 三平台投稿参数、受管封面与定时（0.26.0，T17）
           ↓ G7：Schema/备份、适配映射、合成回归和无远端浏览器验收完成
F. 非破坏性编辑工作台、分段与封面（0.27.0，T18）
           ↓ G8：独立编辑域、真实本地媒体渲染和显式导入上传通过
G. 隔离 AI 运行时、字幕/听写、翻译、配音与试听（T19）
           ↓ G9：冻结本地依赖/模型声明、许可、隐私和 offline/synthetic 边界；真实试听待外部验收
H. 0.28.0 冻结与包外 release receipt（`0592b6f` 已完成）
           ↓ 有效 receipt：clean commit、五件制品、独立安装和冻结结果同一身份
I. 无人值守结果语义、AI 精确 authorization 与输入硬上限（T20 已完成切片）
           ↓ 当前源码只在精确绑定和固定输入/调用上限内推进
J. Schema 4 远端调用 ledger 与 unknown 人工 reconciliation（T20 已完成切片）
           ↓ unknown 不静默重放，完成与重试均由持久化账本失败关闭
K. 非密钥复用预设与真实域离线整链（T20 已复验；网络与模型响应为合成）
           ↓ 复用参数仍重新绑定 runtime 与账号，禁止真实网络的整链可重复
K2. 普通源码 Setup 的 AI runtime 安装（T20 入口契约先在临时 app-root 复验）
           ↓ 固定本地 CPython 归档、当前源码字节、原子发布、只读复用和 app-root 锁均失败关闭
  K3. 普通源码 Setup 的上传 runtime 安装（T20 入口契约先在临时 app-root 复验）
            ↓ 复用既有安装器、精确 data-uploads 派生、三层锁、ready 复用及旧/坏目标失败关闭
  K3b. 当前实际应用根 runtime 刷新（2026-09-10 已完成；packaged 宿主使用 canonical `--app-root`）
            ↓ AI runtime integrity verified、provider health 无密钥未验证、Upload runtime ready、Upload DB Schema 1→3 数据保留迁移，零真实供应商/平台调用
  K4. Workflow Schema 2 多输出、生产多分段界面与三账号 fan-out（本地 synthetic 已通过）
            ↓ 1–10 段输入/预设、有序 slot、prefix checkpoint、retry leaf 原位替换和单次全批确认均失败关闭
  K4b. 自动流程服务端执行预检（本地 synthetic/offline 已通过）
            ↓ 写 workflow 与建下载前复核 Worker、FFmpeg、AI、上传 runtime/调度器及冻结账号绑定，失败保持零下游副作用
  K4c. 三平台参数卡与封面预检闭环（本地 synthetic/Chromium 已通过）
            ↓ capability 驱动限制、逐平台内容/定时/选项、账号级预设差异保留及受管封面在下载前失败关闭
  K4d. 原始预授权 queued 工作安全重启续跑（本地 synthetic/真实上传域整链已通过）
            ↓ 只有未 dispatch 原始 leaf 可重新校验排队；手动、retry、running、canceling 与 unknown 继续停下
  K4e. 整流程安全取消（本地 synthetic/offline、API 与页面验证已通过）
            ↓ 持久取消意图从最远下游继续；只有全部安全停止才取消，远端 outcome 与身份异常保持人工核对
  K4f. 重复下载 owner 等待恢复（本地 synthetic、WAL 并发与故障注入已通过）
            ↓ active owner 继续等待，owner ready 后复用资产，failed/canceled 传播精确终态
  K4g. 配音语速参数端到端贯通（本地合同/API/页面/预设/provider/溢出验证已通过）
            ↓ 默认 1.0 保持旧 SHA；非默认值进入 recipe/profile/request fingerprint；不增加 runtime、Schema、队列或远程重试
  K5. 编辑式玻璃四页生产视觉（本地生产实现与浏览器 QA 已完成）
            ↓ 统一 token、响应式、键盘、减少动态/透明度，当前 44/44 PASS；最终制品仍待新 receipt
  K6. Workflow Schema 3 来源标题冻结与网址即运行预设恢复（本地 synthetic/Chromium 已通过）
            ↓ ready asset、来源标题与逐账号最终参数一次冻结；旧 Schema 1/2 只按旧 grammar 迁移；恢复只记预设 ID，依赖初读失败或用户已编辑时不覆盖表单
  L. 三平台上传与下载实测（T11、T12；当前 NOT RUN）
           ↓ G4：按平台、入口、模式分别接受
M. 根据真实回报修复并收敛当前功能（T13）
           ↓ G5：实际发现的关键缺陷闭环，业务与状态契约稳定
```

T06 文档、T09 CI 设计可与阶段 A 并行；真实上传测试须先过 G1，并使用重新冻结的测试构建。完整上传备份恢复 T07 应在扩大测试账号/媒体规模前完成。不能因某个平台通过就跳过其他平台的门槛。

T14 先确定账号/媒体生命周期，再冻结 T07 的备份格式。T15 是 Linux/Docker/NAS 的独立环境验收支线，不据此阻断已经明确仅支持 Windows 的源码测试版，也不能省略后宣称跨平台通过。工作包编号用于跟踪，执行先后以本节依赖为准。

T19 延续 T16 设计规范和 T17 逐任务确认；自动流程按预授权或逐节点确认推进，未知远端结果仍停止。T11/T12/T15 保持 **NOT RUN**；GitHub hosted CI 的执行链已经恢复，但当前完整 pytest 为 **FAIL**。不能用 G6～G9 的本地页面、合成媒体或截图替代外部能力证据。必须以 0.28.0 的 clean commit 构建并独立安装五件制品，再由包外 receipt 绑定，才可交给 T11/T12 或称为最终交付包。

## 3. 工作包与验收条件

### T01 / P1：上传调度异常恢复（对应 F-01）

**目的：** 避免一次结果落库失败让全部后续上传永久停摆，并避免以重传修复不确定结果。

- 修改范围：`uploads/service.py` 调度循环、单任务执行/最终事务、服务状态；`uploads/api.py` 生命周期和 `uploads/web.py` 故障显示。
- 先建立回归：合成后端只执行一次，返回 submitted/draft_saved 后让最终写入失败；同时放置第二个任务，观察线程、队列、结果和错误状态。
- 建议恢复原则：发生持久化错误立即停止继续领取；对已开始远端动作保留不确定状态。数据库恢复后完成一次恢复对账，已开始的任务变 unknown，尚未执行的队列需要重新确认；不直接重调后端。
- 区分暂时数据库锁与持续 I/O/磁盘错误；所有等待有上限，不将错误文本、SQL 或账号信息直接反射到页面。
- 暴露明确 `worker_running`、故障码和可操作提示；页面刷新不能把缓存死线程重新标成正常。

**验收：** 最终写入失败、事务开始失败、线程/取消监视器启动失败、进程重启各有可重复用例；单任务 backend 调用次数始终最多 1；无任务永久伪装 running；后续任务不会未经重新确认自动传输；服务及所属子进程能正常结束。

**依赖：** 无。**交付：** 源码、真实调用边界上的回归、故障状态说明和恢复演练记录。

### T02 / P1：上传运行环境完整性边界（对应 F-02）

**目的：** 让 `ready` 与实际可加载的固定运行环境一致，同时不因每三秒刷新而反复扫描整个浏览器。

- 修改范围：`uploads/runtime_setup.py`、`backend.py`、`bridge.py` 和 runtime manifest 契约。
- 从固定已核验的源码归档、包锁及浏览器发行载荷确定预期集合，覆盖上游源码、venv 可加载依赖、biliup、实际使用的浏览器文件。
- 核对实际集合，拒绝额外可执行 Python/JavaScript/二进制、缺失项、重定向及非预期文件；明确合法 `__pycache__`、日志和运行时缓存的处理方式。
- 把安装时固定验证、执行前漂移检查、页面状态缓存分层；缓存失效规则必须有测试，不能仅信任用户可改的 ready 标记。
- 对已有运行时给出明确迁移/重装路径；验证失败时不自动“重新签发”通过的 manifest。

**验收：** 新增遮蔽模块、修改依赖/浏览器文件、删除条目、manifest 被改坏、链接/重定向均失败关闭；未改环境可登录流程准备但不在自动测试扫码；首次检查和重复页面刷新耗时有记录，合法缓存不误报。报告明确本地写权限和可信来源边界，不宣称可抵御同权限用户重写整个应用。

**依赖：** 与 T01 可并行，执行前共同通过 G1。**交付：** manifest/迁移规范、正负向测试、检查耗时与兼容性记录。

### T03 / P2：ready 原件完整性与视频类型边界（F-03、F-04）

- 在下载原件 GET 路径处理“同尺寸字节改变”，确保登记 hash 与输出内容一致。先确定同一文件描述符或受控快照的响应设计，避免 hash 后再次按路径打开的竞态。
- 保留大文件流式和 Range 行为，记录额外读取、临时磁盘和首字节延迟；不得一次性读入整个大视频。
- 下载成品转上传的 resolver 明确检查登记 `media_kind == video`；页面隐藏与服务器拒绝一致。
- 保持上传副本独立，错误不删除或改写下载原件；拒绝导入后无来源记录或临时残留。

**验收：** 原件正常下载、同尺寸变更、hash 期间替换、缺失文件、链接/硬链接、并发读取、取消/Range；video 可导入，audio/image/异常登记不可直导；原登记与原件不被“自动修好”。固定 yt-dlp 正常下载路径和下载→上传集成回归通过。

**依赖：** 无，可并行。**交付：** API/仓储调整、边界回归、性能取舍说明。

### T04 / P2：真实运行状态与故障提示（F-05）

- 将“工具存在/离线校验”“候选隔离能力”“本次 Worker 在线”分别表达。
- 由 supervisor 的受控状态通道提供实例/run、启动阶段、进程存活时效、direct/isolated 模式；控制面不靠静态开关猜测在线。
- 普通 Start 显示“Worker 在线、直连非隔离”；独立 control 显示 unknown；`--check` 结束后不能继续显示在线。
- 上传侧显示 T01 的调度故障，区分 backend 缺失、账号需登录、任务待确认和远端结果 unknown。

**验收：** 未启动、启动中、正常在线、暂停领取、Worker 崩溃、心跳超时、正常停止、另一个实例和 check-only 各有适用 API/UI 用例；本次实际 Start 场景不再显示未启动的误导文案。正常停止后控制面关闭，页面应转为未知且不继续在线；不要求已经退出的服务仍提供 stopped API。starting/check_only/stopping/stopped 表示已记录的阶段，只有 online/paused 是有 3 秒时效的存活断言。

**依赖：** 上传故障部分依赖 T01。**交付：** 状态契约、页面文案、生命周期自动化和实际浏览器证据。

### T05 / P2：历史任务容量与可操作性（F-06）

- 核对当前 sources/jobs 最近 200 项、operations 最近 100 项的限制，给出分页或活动任务与历史分开查询的方案。
- 正在运行、待确认或可取消的任务不能被新草稿挤出用户可访问范围；重建返回的后继必须可定位，来源信息不能因离开近期列表而模糊。
- 保持清空来源不自动换源、各任务详情独立、选择不受轮询覆盖；避免每次刷新整个表单导致输入或焦点丢失。

**验收：** 0/1/200/201/1000 条合成历史下，活动任务仍可查找和取消，后继可定位；无未授权确认请求；查询/渲染耗时、内存和轮询请求数有基准。本轮已在 201 项时复现旧 running 和引用来源被隐藏；完整容量承诺须待这些用例通过。

**依赖：** T04 状态契约；可先独立设计。**交付：** API 游标/过滤约定、UI、容量回归和基准。

### T06 / P2：当前运维与测试身份收敛（D-01）

- 更新 Runbook 当前版本、FFmpeg 固定版本、Linux runtime contract、下载与上传数据库边界；历史内容只放在明确标记的历史段。
- 明确 Linux candidate 当前属于下载 Worker 路径，不能根据同一仓库版本号推断已支持 Windows 上传运行时。
- 统一 README → 安装/运行 → 上传运行环境 → 测试计划 → HANDOFF 的入口；明确 Bilibili 既有扫码证据与其他平台待验收项。
- 测试交付增加 `source_commit`、`source_archive_sha256`、完整 `product_identity`；采用固定提交和受控源码 ZIP/manifest。分支页用于浏览，移动分支不是唯一验收身份。

**验收：** 当前命令与锁/入口一致，全部本地链接有效；版本一致性检查只检查“当前”字段而不改写历史证据；干净机器测试员能按文档取得同一构建并完整回报身份。

**依赖：** 可先并行整理，最终对齐 T01～T05 和 T10 冻结结果。**交付：** 当前 Runbook、测试模板、交接与小型一致性门禁。

### T07 / P2：上传库结构校验、备份与独立恢复（F-07）

- 先区分新建库与既有库。新库原子初始化为 Schema 2；精确 Schema 1 在单次事务中迁移到 Schema 2，Schema 2 直接核验。缺表、混入无关结构、损坏或版本不符时拒绝，禁止通过补建表“修复”未知现状。
- 定义停机的一致备份作为首个实现范围，避免一开始承诺在线一致复制。
- 备份上传 Schema 2、sources/media、jobs/retry/request 关系和必要账号元数据；账号登录秘密不进入当前格式，不提供含秘密模式。
- runtime 可重建，incoming 和临时操作不是恢复来源；不盲拷正在运行的浏览器 profile。
- 恢复目标必须是新的独立目录，校验 hash、外键和状态语义；原 running 恢复为待核对，queued 回待确认，不自动登录/上传。
- 在下载备份命令和文档明确展示备份范围，不能让用户误以为一次下载备份包含所有上传账号和任务。
- 上传根旁的 sibling activity lock 为当前应用/API/active 与 standby 服务提供 shared lease，备份在源根、恢复在目标根持 exclusive lease 覆盖整个操作；旧 worker lock 继续作为兼容防线。所有当前应用与 standby 实例仍须先正常停止；旧版本或手工 writer 不参与完整 activity-lock 合同，须由操作者另行停止。

**验收：** 合成三平台数据备份→新根恢复→页面核对；缺表/多表/列或外键不符、缺文件/篡改/版本过新拒绝且原目录不变；精确 Schema 1 可迁移且历史关系保留；备份不存在 Cookie/token 内容；恢复全过程远端请求为 0。

**依赖：** T01 恢复语义、T14 的数据生命周期设计。**交付：** 备份格式、命令/指南、校验与恢复演练；扩大真实账号测试前完成。

**2026-09-07 状态：本地范围完成。** `video-upload-backup create/restore`、manifest/hash、Schema 与业务语义审计、登记媒体核对、秘密/临时树排除、恢复状态降级、活动锁及失败清理均已进入源码和 synthetic 回归。该结论不包含真实账号目录、生产容量、异机/offsite、NAS、RTO/RPO 或目标环境人工演练。

### T08 / P2：持续运行与资源故障测试

- 围绕 T01/T02/T03 建立可重复的故障注入：数据库锁、磁盘空间不足、媒体复制中断、浏览器超时、取消竞态、应用重启和进程树清理。
- 覆盖多个账号和两个下载槽；确认上传仍按其设计串行，不把下载并发扩展到同一账号会话。
- 记录长期轮询的内存、句柄、临时文件、线程数与日志增长；仅使用合成媒体和禁止远端调用的 backend。
- 先建立本机基准，再按明确预算优化；不将不同并发负载下的 pytest 总耗时变化直接称为性能回归。

**验收：** 可复现脚本、明确超时、所有 own 进程退出、端口释放、无孤立运行任务和重复 backend 调用；压力测试结束后可继续创建/取消本地草稿。

**依赖：** T01～T05。**交付：** 有界故障集、资源基准、修复后对比。

**2026-09-07 状态：有界本地切片完成。** 新增用例证明 Bilibili/抖音/视频号三个已确认任务在多账号条件下仍严格串行且 backend 各调用一次，完成后仍可创建/取消本地草稿；300 轮共 1500 次 status/accounts/jobs/sources/storage 读取在 45 秒门槛内完成；第二个 1 MiB 读取时注入复制中断后没有 DB/source/media/incoming 临时残留，用户原件保持不变。该文件连续 4 轮通过；实测单轮 5.672～6.109 秒、线程 `1→1`、Windows handles `211→211`、retained `0～7928` bytes、peak delta `261400～269188` bytes。它不是无限期 soak，也未单独覆盖本节列出的每一种浏览器、数据库和进程树故障。

### T09 / P2：无凭据持续集成与发布门禁

- Windows CPython 3.12/3.13 覆盖普通入口、上传 UI/服务、运行时契约和发行安装的适用子集；当前 workflow 只输出 Node runtime identity，不另行声称存在独立 Node UI harness。
- Linux runner 执行 Linux/POSIX 相关测试，root/getfacl/network namespace 要求单独命名，不把普通容器里的 skip 当通过。
- 核对 Python lock、静态文件/许可/隐私规则、Markdown 当前口径及发行清单；只缓存按 hash 固定的依赖/工具。
- CI 不存真实平台 Cookie，不扫码，不发布视频；日志和报告按现有脱敏规范输出。
- 四格完整通过后，再按仓库维护流程设置 required checks；分支保护属于另一个需要实际配置并核验的动作。

**验收：** 新分支或 PR 自动触发；人为引入一项断言/清单错误会阻断；Windows 与 Linux 结果明确分开；发布候选可定位到单个固定 commit。

**依赖：** 可并行搭建，纳入 T01～T08 的回归。**交付：** 工作流、失败演练、门禁说明与托管配置记录。

**2026-09-10 状态：托管执行链恢复，完整测试仍失败。** `.github/workflows/ci.yml` 配置 push、pull request 与手动触发的 Windows/Linux × CPython 3.12.10/3.13.14 四格矩阵，权限为 `contents: read`，不保留 checkout 凭据；action commit 与 `uv 0.11.25` 精确固定。完整 checkout 允许 `verify_commit_scope.py --github-event` 从事件 base/head 求 merge-base 并在依赖安装前检查提交范围；locked dev environment 绑定矩阵 Python，测试前再核对实际 Python patch 与 pytest 身份。[run 34393235622](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622) 绑定 `bdd88ce184b2f86f957f7df9129baa21863227dd`，四格均已通过完整测试之前的全部环境与门禁，随后四格都在 `Run the offline test suite` 失败，run 总结论为 `failure`。本机同源码全量为 **173 failed, 2277 passed, 8 skipped in 327.39s**，主要暴露冻结测试仍用 `testserver`/无下载 session-CSRF 及既有 Schema、release、UI、validation identity 漂移。测试文件未改，产品没有测试绕过；required checks / branch protection 仍为 **NOT CONFIGURED**。详见[托管 CI 恢复证据](../validation/iteration-0.28.0-hosted-ci-recovery.md)。普通 hosted Linux 不能替代 T15。

### T10 / P1 交付门槛：冻结并交付 0.24.4 外部测试包（历史阶段，保留）

- 为修复集合按项目版本策略确定新 patch 版本，统一 package、入口文档和当前部署契约；不要继续让多个不同源码包仅凭“0.24.2”区分。
- 从最终待交付 commit 冻结源码，构建源码 ZIP/sdist/wheel、release-manifest 与 SHA256SUMS。
- 在独立目录执行 Setup、重复 Setup、Start check、普通 Start/stop、wheel 14 入口；补可选上传的离线发布门禁：缺环境提示、已验证环境检查、三平台本地草稿和最终确认保护。
- 复核制品包含测试说明和最新交接，排除账号、数据库、媒体、私有日志与工具缓存；比对测试字节、Git 对象和实际交付字节。

**验收：** 同一新身份对应全部报告；上传自动测试远端请求为 0；测试员能按固定包复现；旧 f12749f 包保留其历史身份，不借用其测试结果。

**依赖：** G1、T03/T04/T06、T08；T07 在扩大测试前完成。**交付：** 五个发行文件、精确提交链接、测试说明、外部验收记录模板。Git push、合并 main、GitHub Release 分别记录，不混称完成。

**2026-09-07 源码状态：冻结入口已准备。** v0.24.4 版本字段、部署候选引用、发行检查、精确 14 项入口合同和上传 no-remote 探针已经接线。T10 是否通过须查看包外 release receipt：它必须证明从 clean detached commit 运行冻结全量，构建源码 ZIP/sdist/wheel、`release-manifest.json`、`SHA256SUMS`，并完成源码与 wheel 独立验收。这个包内文件不嵌入自身 commit 或制品 hash。

### T11 / P1 验收门槛：三平台真实上传

沿用 [UPLOADER_TEST_PLAN](UPLOADER_TEST_PLAN.md) 的编号，并绑定 T10 的新构建。每个平台先跑本地/登录矩阵，再由测试员逐项确认一次最小案例。

| 平台/模式 | 必做验证 | 接受条件 |
| --- | --- | --- |
| Bilibili 投稿 | 本人扫码/检查，分区、标签、原创/转载来源，确认后传输 | 创作中心找到对应稿件，账号/媒体/文案/分区一致；接收、审核、公开分别记录 |
| 抖音投稿 | 本人完成真实扫码/额外验证，正文与标签，确认后传输 | 创作者后台找到对应作品，无截断/错账号/重复；状态分别记录 |
| 视频号平台草稿 | 本人登录，明确选平台草稿并确认上传 | 后台草稿列表存在对应媒体和文案，未因该动作被发布 |
| 视频号投稿 | 不同测试编号，明确发布模式并确认 | 后台找到对应作品，接收/审核/可见性分别核对 |

三平台各覆盖 `L` 本地导入与 `D` 下载成品到本地草稿；真实远端案例总体至少覆盖两种入口。首次每次只确认一个任务。平台额外验证、权限不足和拒绝投稿记 BLOCKED/FAIL 的实际原因，不绕过验证或反复点击追求通过。

**未知结果处理：** 记录任务 ID/时间/模式 → 正确账号核对草稿、作品与审核列表 → 记录已收到/仍处理/无法确定 → 由测试员决定等待或是否需要新草稿。取消不证明远端未收到，也不等于撤稿。

**验收：** 四行都有独立报告或明确 BLOCKED；不能用一个平台的成功代替其他平台；不把 submitted 当审核通过；错账号、未确认发送、重复或本地成功但远端无法对应时暂停该平台后续测试。

**依赖：** T10/G3 和测试员自己管理的账号、合法测试媒体。**交付：** 脱敏的平台 × 入口 × 模式结果表和必要复现信息。

### T12 / P2：当前构建的六平台下载实测

- Bilibili、抖音、YouTube、TikTok、Instagram、X 各自验证当前支持的普通单视频路径；不把多 P、合集、图集或 X graph-v2 未启用路径混入“当前支持”。
- 使用明确允许下载的样本；需要 Cookie 时仅使用用户本机只读配置，不复用上传登录态。
- 覆盖正常、短链适用情况、duplicate、并发/取消、失败重试；每个成品对照登记 hash、ffprobe 与完整解码。
- 记录平台策略失败与本地技术失败的区别，给出构建级证据，保持 candidate/approved 状态的证据规则。

**验收：** 每个平台和路径有 PASS/FAIL/BLOCKED/NOT RUN；成品可以通过原件下载和上传导入；失败不产生错误 ready 记录或无法清理的临时文件。

**依赖：** T03、T10；与 T11 可由不同测试员并行。**交付：** 当前构建下载矩阵和回归清单。

### T13 / 后续：测试反馈闭环与功能扩展

- 对真实回报先复现，再补正确边界上的失败测试，再修复；小规模验证后才扩大账号/媒体规模。
- T11/T12 未通过的平台继续保持待验收；维护者依据结果决定是否合并 main 和发布版本。
- T16 本地开发和 G6 已在 0.25.0 完成；后续真实回报先进入 T13 修复，再按受影响范围重跑前端回归并重新冻结。更多功能扩展不作为当前 0.25.0 receipt 的无限等待条件。
- Windows 独立 EXE、签名/升级卸载、Linux 上传、媒体编辑、更多平台分别立项；不得用本次 Windows 源码证据直接为这些能力背书。
- Bilibili 分区选择、可编辑草稿、进度细节等体验优化放在可靠性门槛之后，以实际测试员反馈决定顺序。

### T14 / P2：账号断开、媒体保留与容量管理

- 增加明确的“断开本地账号”入口。先处理或拒绝该账号的活动操作与任务，撤回尚未执行的旧确认，尝试移除本地登录状态并保留历史任务对应的账号备注/墓碑记录；清理失败必须保持账号不可用、明确提示残留并允许重试，不能报告已经移除。
- 防止取消后的迟到登录回调或重启重新写回已断开的登录；说明本地断开与平台侧撤销授权的区别，平台会话由用户在平台处理。
- 提供媒体用量、显式保留/删除和低空间提示。存在活动任务引用时不能删除；保留历史 hash/名称与 media-present 状态，缺媒体的重试必须先重新导入。
- 评估重复 hash 去重、总配额和可配置剩余空间阈值；上传与下载通常共用磁盘，不能让不同阈值造成用户看不懂的容量行为。当前上传预留 64 MiB、下载默认预留 1 GiB 是现有代码事实，不保证在所有设备上足够。
- 对复制完成但数据库未登记等中断窗口建立孤儿审计。先展示可核对的清理清单，清理只涉及本应用确认拥有且无活动引用的文件，不自动删除用户原件。

**验收：** 合成账号断开后不可继续用旧会话确认上传；迟到回调/重启无凭据复活；删除受引用媒体被拒绝；低空间导入在复制前有明确失败；旧历史可解释、无新增秘密输出；断开/删除/备份恢复语义一致。

**依赖：** T01 状态恢复、T05 全量访问；设计结果先供 T07 使用。**交付：** 生命周期状态图/契约、显式控件或 CLI、回归和测试收尾指南。

**2026-09-07 状态：首个必要切片本地完成。** Upload Schema 2 保存账号 active/disconnected 与断开时间、来源 present/missing/deleted/changed/unsafe 与删除时间；服务/API/页面提供两步断开、两步删除、空间/未登记文件审计和原 source ID 的精确 hash 恢复。活动上传或活动任务引用会拒绝对应破坏操作；迟到登录回调不能复活墓碑账号，断开和重启会尝试清理已断开账号秘密。清理失败会保留 disconnected 隔离状态、显式错误和可重试入口，不会报告已经移除。恢复媒体不自动执行旧任务。重复 hash 去重、总配额、可配置阈值和自动孤儿清理没有纳入这个切片，继续保留为后续优化。

### T15 / P2：Linux、Docker 与 NAS 独立验收

- 准备可实际运行的可信 Linux/root 环境，核对 getfacl、Unix socket、network namespace、只读 bind 和候选工具版本；本轮 Windows 跳过项必须在这里实际执行。
- 按 `deployment/run-linux-acceptance.sh` 和 `validation/linux-docker-acceptance.md` 执行已有候选流程，验证 worker 仅经声明 relay/egress 出站、Cookie mounts/权限隔离、停机清理和 Schema 11 恢复。
- 明确容器持久卷、UID/GID、磁盘配额、日志和备份路径；默认仅承诺现有下载 candidate，上传页及可选运行时需另作平台适配，不能直接把 Windows 路径带入容器。
- NAS 需选定具体 CPU/文件系统/容器平台后核对架构与权限；未选定硬件前不作通用 NAS 支持承诺。

**验收：** 8 个当前跳过项各有实际结果；候选 runner 的必需 gate 不被静默跳过；出站、ACL、只读文件系统和恢复证据存在；使用说明明确已验收与未验收的平台能力。

**依赖：** 实际 Linux 环境及匹配工具载荷；可与 Windows 支线并行。**交付：** 环境清单、Linux 测试/部署报告及支持矩阵。

<a id="frontend-upgrade"></a>

### T16 / 最终阶段：前端完整升级与持续设计规范

**2026-09-07 当前状态：** v0.25.0 的 T16.1～T16.7 本地开发范围已经完成，G6 本地验收已通过。最终 clean commit、五件发行文件、源码与 wheel 独立安装及冻结全量是否通过，只查看同批包外 release receipt；包内不声称自身最终制品身份。

**用户要求：** Apple 风格、精致排版与字体、适量动态效果，并作为后续开发的统一设计依据。实现覆盖实际下载/上传两页及本轮新增的界面，不是只更换首页背景或按钮颜色。

**设计依据：** [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md)定义颜色、字号、字重、行高、间距、圆角、组件、主题、动效、响应式和验收；[设计预览](design-preview.html)提供可交互的合成示例；根目录 [AGENTS.md](../AGENTS.md)让后续开发任务持续遵循同一规范。规范、预览与生产前端已在本轮同步，后续变更仍须共同维护。

| 顺序 | 已完成的本地实施内容 | 当前状态与边界 |
| --- | --- | --- |
| T16.1 盘点与共享基础 | 冻结控件/状态/事件合同；建立公共样式和 shell 资源，统一浅/深/系统主题、字体与语义 token | **本地完成**；两页共享资源纳入源码与发行清单，保持 FastAPI + 原生前端架构 |
| T16.2 信息结构与页面 | 共用导航、运行摘要；下载突出创建/进度/成品，上传按账号/视频/内容/核对任务组织 | **本地完成**；原有操作与状态边界保留 |
| T16.3 全组件迁移 | 统一表单、按钮、账号、扫码、来源、草稿、任务、标签、提示与必要确认界面；覆盖空/加载/错误/禁用/unknown | **本地完成**；主次操作与长内容处理已纳入页面实现 |
| T16.4 交互稳定 | 按稳定 ID 增量更新 DOM，保留焦点、选区、IME、勾选、详情和空来源；处理轮询、页面隐藏与 BFCache 生命周期 | **本地完成**；安全确认语义和请求时序未由视觉变化改写 |
| T16.5 动效与适配 | 实现克制、可中断的按压、局部展开和状态切换；同步窄屏、减少动态/透明度及高对比模式 | **本地完成**；动效不触发业务请求，二维码即时隐藏边界保留 |
| T16.6 真实浏览器验收 | 使用禁止远端调用的本地页面与合成 backend 核对关键状态、视口、主题、键盘、焦点、读屏提示与动效降级 | **G6 本地验收完成**；不包含真实平台、真实账号、真实下载或上传 |
| T16.7 最终冻结准备与持续维护 | 同步规范、预览、指南、交接和发行载荷合同，完成本地回归与冻结入口准备 | **本地开发完成**。最终 clean commit、五件制品、独立安装和冻结结果只由包外 receipt 判定 |

**必须保持：** 扫码/检查与上传相互独立；创建仅本地草稿；每个任务明确确认；unknown 先核对平台；取消不暗示远端撤回；`submitted`/`draft_saved` 不改写为审核或公开成功。二维码退出/过期/页面隐藏立即隐藏并清理，不能等待动画；任何动效完成事件均不得触发或重复业务请求。

**验收门槛 G6（本地已完成）：** 两个生产页面与本轮新增界面已完成迁移，本地行为回归、禁止远端的真实浏览器检查及设计规范要求已经验收；没有外部字体/CDN 依赖，也没有执行真实媒体上传。G6 不包含 T11 三平台真实上传、T12 当前六平台下载、T15 Linux/Docker/NAS 或 GitHub hosted CI，不能由视觉验收、合成数据或本地通过结果替代。独立安装的最终 0.25.0 包是否包含相同前端字节和当前说明，仍由本轮最终冻结及包外 receipt 证明。

**依赖与交付边界：** T16 本地实现依赖 T01～T05、T14 已收敛的页面与状态合同；不以 T11/T12/T15 或 hosted CI 已通过为前提，也不改变它们的 **NOT RUN** 状态。共享样式/组件、升级后的完整前端、最新设计规范与预览及 G6 本地验收已经完成；最终测试说明与制品必须复用 T06/T08/T09 的本地门禁及现有发行工具重新冻结。0.24.4 的 T10 身份和结果不可复用。

### T17 / 历史阶段：三平台投稿参数、封面与定时发布

**2026-09-07 当前状态：** v0.26.0 本地实现、定向回归、真实 Chromium 合成验收与 G7 证据已完成。首批范围仍只有 Bilibili、抖音和视频号；创建动作仍只产生 Open-Flame 本地草稿，每个任务必须由用户单独确认后才可调用适配器。最终发行冻结、真实平台逐字段验收及目标 Linux/Docker/NAS 验收仍未执行。

| 平台 | 本地草稿可核对字段 | 当前适配边界 |
| --- | --- | --- |
| Bilibili | 独立标题/简介/标签、分区、原创/转载及来源、单封面、动态文案、禁止转载、关闭评论/弹幕、立即或定时发布 | 固定映射到 biliup `--cover`、`--dtime`、`--dynamic`、`--no-reprint`、`--up-close-reply`、`--up-close-danmu`；关闭评论/弹幕需 `--submit app` |
| 抖音 | 独立标题/简介/标签、单封面、立即或定时发布、自主声明 | 显式声明只接受白名单；未选择时覆盖固定上游的旧 AI 默认且不操作声明；弹窗或选择状态无法回读时失败关闭 |
| 视频号 | 独立标题/简介/标签、4:3 横封面、3:4 竖封面、7–15 字短标题、内容标记、平台草稿/立即/定时发布 | 封面要求裁剪和主弹窗关闭且槽位预览变化；未选择内容标记时不沿用固定上游旧 AI 默认；短标题和标记必须回读一致 |

封面保存为受管资源，只接受完整解码的静态 JPEG/PNG/WebP，并执行 20 MiB 文件、4000 万像素和 64 MiB 解码预算；活动任务引用时不能删除。Bilibili 本地创建与执行至少提前 6 小时 5 分钟，抖音/视频号至少提前 4 小时 5 分钟：均在各平台提前量下限之外预留最多 2 小时传输和 5 分钟安全余量。视频号还要求本地整点且最多 28 天。时间保存 Unix 秒和当时 UTC 偏移，DST 不存在或歧义的墙钟时间会拒绝。

Upload Schema 3 和上传备份格式 2 保存封面引用、时间、平台参数及 request digest version 2。格式 1 / Schema 2 只作为严格只读输入在 staging 迁移；旧标签规范化，抖音/视频号旧版隐式 AI 默认显式保存，受影响的 draft/queued/running 均要求重新核对，原 queued/running 的恢复原因不会被迁移提示覆盖。

**验收门槛 G7：** 服务/API/适配器、Schema/备份迁移、页面状态和真实浏览器均在禁止远端的合成环境通过；最终结果写入 [0.26.0 投稿参数记录](../validation/iteration-0.26.0-upload-parameters-evidence.md)。G7 不包括真实账号登录、扫码、媒体上传、定时触发、平台后台接收/审核/公开，也不包括最终发行制品；这些仍由 T11 和包外 0.26.0 receipt 判定。

### T18 / 历史阶段：非破坏性编辑工作台、分段与封面

**2026-09-07 历史状态：** v0.27.0 已完成 T18 本地实现、定向回归和 Chromium synthetic 流程；证据见 [0.27.0 编辑工作台记录](../validation/iteration-0.27.0-editing-workspace-evidence.md)。当前 T19 状态见本文顶部与 [0.28.0 AI/Workflow 证据](../validation/iteration-0.28.0-ai-workflow-evidence.md)。

- 编辑数据位于下载根同级的独立 `data-edits`；当前发布后源码将精确 Editing Schema 1/2/3 按顺序向前迁移为 Schema 4。导入重新计算并核对下载成品 SHA-256，只在编辑域创建副本，不覆盖下载原件。
- 草稿按版本保存并用 `expected_version` 防止并发覆盖；render plan 冻结 recipe，必须从 `review` 单独确认后才进入单一后台媒体 Worker。
- 本地媒体处理使用已锁定的 FFmpeg/ffprobe，把多个时间区间生成 H.264/AAC MP4；封面经本地抽帧、居中裁切、缩放和可选标题叠加生成 PNG。输出记录大小、SHA-256、时长、尺寸、容器和 codec。
- 取消覆盖 claim 与内存取消信号之间的竞态；中断或失败不会发布半成品。失败/取消可生成新的待核对重试计划，历史 lineage 保留。
- 编辑视频的“用于上传”只打开显式导入提示；导入会在上传域再次复制并核验，不创建上传任务、不登录账号、不调用平台。
- `TranscriptionProvider`、`TranslationProvider`、`SpeechProvider`、能力清单和严格 SRT/VTT 时间轴已建立；T18 当时尚未安装 AI runtime 与模型。当前 T19/G9 的本地 runtime 完整性已经验证，但远端模型仍因无密钥未进行 provider health 或实际调用，见[运行时刷新记录](../validation/iteration-0.28.0-local-runtime-refresh.md)。

**验收门槛 G8：** 自动化覆盖 Schema、服务状态机、媒体安全、API/CSRF、页面与下载→编辑→上传；真实 Chromium 使用 8 秒本地 synthetic 视频生成 3500ms/3000ms 两段和 720×1280 封面，再显式导入第一段到上传域，最终上传任务数仍为 0。G8 不包含用户真实媒体、大文件/长片矩阵、编辑备份恢复、AI 模型、真实平台或最终发行制品。

### T19 / 当前阶段：AI 字幕、翻译、配音与自动流程

1. 建立与核心 `.venv`、上传 runtime 分离的 `data-ai-runtime`；builder 只接受官方 CPython 3.12/3.13 Windows x64 embeddable ZIP，并冻结 worker/provider 文件 SHA-256。模型为供应商云端 alias，不捆绑模型权重。
2. 当前听写使用 OpenAI `whisper-1` 的 segment timestamps；本机生成 mono AAC 派生音频，provider 调用前的有效硬上限为 30 分钟且 25 MiB。`/workflows` 允许完整视频、一个分段或首尾连续的多个分段；有分段时只派生首段起点至末段终点的连续音频，并把返回时间加回源时间轴。编辑页直接建听写任务时仍默认完整编辑源。自动 Workflow 可选优先导入下载登记的 SRT/VTT 为待审核 transcription timeline；cue 跨越外层或内部任一分段边界时失败关闭。本地 faster-whisper 尚未实现，列入后续。
3. 翻译使用 `gpt-5.6-luna` Responses API，保持 cue ID、顺序、整数毫秒时间、来源文字、译文、provider/model 和审核状态；页面第一批提供中文与 English 目标语言。单 cue 上限统一为 4096 字符，时间轴 JSON 限 3 MiB，为隔离 runtime 的 4 MiB envelope 留出固定空间。
4. 配音使用 `gpt-4o-mini-tts` 的 13 个标准音色，禁用声音克隆；逐 cue 生成 WAV、测量时长并按每个分段重新归零构造 PCM 时间线。分段边界切入 cue 时拒绝；空 cue 的 B-roll 段生成空 VTT 与本地静音，不调用 TTS；溢出时失败关闭，不静默截断或覆盖下一 cue。保留原声时固定压到 22% 后混入配音。
5. 编辑页提供完整听写/译文时间轴审核、批准/拒绝、任务取消/后继重试，以及绑定已批准修订的处理计划。当前 Schema 4 账本先冻结 owner、operation、ordinal/attempt、authorization/owner/request 摘要和调用单位；发送后无法确认结果时标为 `unknown`，页面只提供 `not_accepted`、`accepted_without_result`、`abandoned` 三项 revision-fenced 人工 reconciliation。它不保存 provider request ID 或响应正文，也不复用部分结果。逐 cue 音频试听和单 cue 重新生成尚未实现。
6. `/workflows` 使用独立持久化状态机串接 URL 下载、AI、编辑和所选三平台上传；可预授权或逐节点确认，重启继续对账。服务端在保存 workflow 与建立下载前分别复核下载 Worker、FFmpeg、AI authorization/音色、上传 runtime/调度器和账号绑定；首次失败保持 workflow/event/download 为零。域恢复先撤回旧 queued 确认；只有 canonical profile 已保存对应 auto-confirm，且每个待重新排队的当前非终态 leaf 均为无 `retry_of`、可证明尚未 dispatch 的原始项时，WorkflowManager 才重新校验并排队；同批成功的上传 leaf 可保持终态。手动、retry、running/canceling、账本 dispatched/unknown 和上传 unknown 保持停止。上传前仍按平台校验标题、标签、封面比例、AI 声明、模式和发布时间，并在一次事务内确认全部待执行账号。流程保存账号 `session_revision`；重新登录会让旧绑定以 `account_session_changed` 停下。上传重试只跟随完整不可变投稿参数一致的唯一 retry leaf；账号、来源、平台、标题、简介、标签、分区、模式、版权/来源、封面、发布时间、时区或平台选项漂移，以及分叉或循环都会失败关闭。
7. 发布后里程碑把一次 AI 同意绑定到 runtime ID/version、protocol、manifest SHA-256、provider kind、model ID/本地声明 revision、operation、data egress 和有效 limits。task request、配音 recipe 与 Workflow profile 持久化完整 authorization 及摘要；创建、确认、worker 与 provider 前都复核当前定义。只有授权中存在远程 operation 时才要求 data-egress 确认。旧记录仍可读取，但缺少绑定或摘要漂移时必须重建。翻译上限为 1000 cues、60000 输入字符、20 个按 50 cues 估算的调用单位；TTS 上限为 600 cues、60000 输入字符和 600 次调用。听写上限见第 2 项。这些是本地输入/调用上限，不是价格预算或已计费 usage ledger。

**当前门槛 G9：** 官方 CPython 3.13.15 embeddable ZIP 已按官方 SHA-256 在本机构建并重新加载 runtime；无凭据时三项云能力保持 blocked。本地 synthetic 媒体、字幕/配音渲染、取消/恢复、损坏输入及 authorization/budget/ledger 失败关闭边界必须通过。authorization 中的 model revision 只是本地 manifest 声明；远端 alias 在供应商侧仍可能漂移。账本与人工核对不能替代供应商 request ID、账单或后台证据。真实 OpenAI 调用、人工试听、费用、账号权限、三平台真实投稿和最终制品仍是独立外部验收，当前不得宣称通过。

### T20 / 当前发布后阶段：无人值守正确性、精确 AI 授权与调用对账

1. **已完成：上传结果与恢复语义。** Workflow 区分 `submitted`、`draft_saved` 和合法混合 outcome；unknown 优先停下核对。账号失效撤回同账号 queued 确认，浏览器幂等键只在响应丢失恢复窗口内复用，reconciliation 无进展时有界退避。
2. **已完成：逐操作 AI authorization。** 能力接口返回 authorization 与 SHA-256；它冻结 runtime、协议、manifest、provider/model 本地声明、operation、精确外发范围及 effective limits。task request、配音 recipe 和 Workflow profile 都保存绑定，创建/确认 CAS、worker 及 provider 前复核。缺少绑定的旧记录可读但不能继续执行，须按当前能力重建。
3. **已完成：调用前输入硬上限。** 听写在首次 provider 请求前限制 30 分钟、25 MiB 和 1 次调用；翻译限制 1000 cues、60000 输入字符和最多 20 个按 50 cues 估算的调用单位；TTS 限制 600 cues、60000 输入字符和 600 次逐 cue 调用。上限用于阻止一次确认意外扩大输入或调用数，不代表价格、额度、实际 token/音频计费或供应商账单。
4. **已完成：Schema 4 远端调用 ledger。** `ai_invocations` 仅保存 owner、operation、ordinal/attempt、调用单位、authorization/owner definition/request fingerprint 摘要、状态、固定 reason code 和时间，不保存正文、密钥、本机路径、endpoint 或 provider 响应。状态只沿 `reserved→dispatched→responded`、`reserved→released`、`dispatched→unknown→reconciled` 前进；迁移只为可能已经远程执行的旧任务/计划建立保守 `unknown` 哨兵，不能证明为远程或缺少 authorization 摘要时仍要求人工核对。`reserved/dispatched/unknown` 阻止完成与重试；`reconciled/accepted_without_result` 和 `reconciled/abandoned` 继续阻止重试，`responded`、`released`、`reconciled/not_accepted` 才可在原有 owner 状态和再次确认规则下继续。翻译 `request_units=ceil(cues/50)` 是 runtime envelope 估算；provider 仍可因 4 MiB 请求边界拆成更多实际 HTTP 请求，因此它不是精确请求数、token/价格或账单收据。`health` 检查不入账，也不证明远端账号/模型可用。
5. **非密钥复用预设与本地整链 smoke 已接线。** `WorkflowPresetStore` 与 `/api/v1/workflows/presets` 已保存 URL 之外可复用的分段/封面/语言/音色/平台内容参数、authorization 摘要及账号选择意图；Workflow preset Schema 2 另保存逐账号相对发布时间策略及摘要，每次运行用固定 `schedule_base_unix` 物化为上传域原有绝对时间，响应丢失重试复用相同锚点和幂等键。不得保存 API key、Cookie、扫码状态或账号 session revision，运行时重新绑定当前能力和账号。页面只在成功应用或保存后记住预设 ID，并等预设、账号与 AI capability 的初次读取都成功后恢复；初次读取失败或用户已编辑时保留当前表单，后续刷新只在仍未编辑时重试。旧脚本跳过 AI 的成功声明已撤回；当前真实域整链覆盖预设重载、URL→下载 Worker→隔离听写/翻译/配音→FFmpeg→三平台上传服务，且核对配音确实进入视频，最终为合成 `submission_acknowledged`；真实 OpenAI、下载提取、账号登录和三平台发布仍需单独授权验证。
6. **普通源码 Setup 的两个可选 runtime 入口已接线。** AI runtime 使用固定本地 CPython ZIP 和原子 builder；上传 runtime 使用显式 `--upload-runtime`、可选 build-only `--upload-python` 并复用 `uploads.runtime_setup`。上传目标固定从 `<app-root>/data-uploads` 派生，锁顺序为 source→app-root→upload runtime；ready 复核、旧/坏/含非允许内容的部分目标拒绝、固定失败码和输出脱敏已在 ignored 禁网 validator 通过。上传 builder 仍直接构建，空目录或仅含散列匹配固定归档的安全预置可以续建，不能描述成原子发布。
7. **自动流程服务端执行预检已接线。** 首次创建在保存 workflow/event 和建立下载前核对本次托管下载 Worker、FFmpeg、逐平台投稿参数、上传账号 ready/session revision、上传 runtime/调度器及启用 AI 时的三项精确 authorization/标准音色。创建 admission 跳过 10 秒整体验证结果缓存并重新枚举，只复用 identity/大小/mtime/ctime 未变文件的摘要；首次冷校验不持有 WorkflowService 全局 mutation lock，同进程并发扫描通过独立 runtime admission 锁避免重复冷散列。`created → downloading` 前以冻结账号绑定再次执行，并对上传 runtime 做完全无缓存内容校验，因此 Windows `ctime`/mtime 缓存限制不能放行下载。短暂下载/锁/账号检查状态留在 `created` 有界重试，其余外部修复项进入 attention 并由显式推进重新核对。后续 AI task、render、upload draft/confirm 与平台 child 的完全无缓存执行检查继续保留。
8. **三平台参数卡与封面预检已接线。** `/workflows` 按所选账号显示 Bilibili、抖音和视频号面板，可独立覆盖标题、简介、标签、定时与平台字段；标题上限、提前量、模式和声明枚举优先读取上传 capability manifest。相同平台多个账号的 preset 差异默认逐账号保留，用户可只统一被编辑字段或显式统一整个面板。视频号草稿会清空并禁用定时，Bilibili 原创/转载会同步来源字段。自动生成封面只接受固定比例并取所选平台共同支持范围；既有 cover ID 在首次 preflight 即校验文件和尺寸，生成封面与 override 封面冲突、Bilibili 竖向 landscape slot 均在下载前失败。
9. **原始预授权 queued 工作安全重启续跑已接线。** Editing/Upload 域保留原有失败关闭恢复，Workflow 只在摘要绑定的 `auto_confirm_*` 与 canonical profile 一致、当前 leaf 无 retry 谱系，并且现有确认入口重新通过 authorization/runtime/recipe/source/account session/完整平台参数校验时恢复 AI、render 或整批上传。自动确认采用正常首次原因与精确 restart 原因的正向列表；legacy migration、混合或未知 review reason 均停下。retry 谱系优先于通用 restart code，任一 unknown 仍阻断整批。当前证据组合覆盖策略 seam、真实 WorkflowManager 启动扫描、10×3 fan-out、混合 review reason、无效/非 canonical/不符合当前合同的平台参数拒绝、profile flag 篡改拒绝、既有 AI/edit 恢复回归与真实上传域离线整链。运行时 leaf 选择与备份审计现在复用完整投稿 payload 等值合同，因此 canonical-to-canonical 的 retry 后继字段漂移也会失败关闭；这不是数据库行的密码学真实性证明，也不构成 abrupt power-loss 或真实三域网络 E2E。
10. **重复下载 owner 恢复已接线。** 单 URL duplicate 批次不再因为自己没有 job 而立即失败；同一 deferred SQLite 快照读取批次、ready asset 和最终 owner。owner 链逐层核对来源身份、根指针、32 层上限和无 job/error 的 duplicate 节点；最终 owner 必须是同来源、同 batch/input、当前 generation、无 graph target/discover parent 的唯一 flat download。active 保持等待，ready 复用同一资产，failed 只传播下载域 `ErrorCode`，canceled 使用固定 `download_canceled`；损坏链路、状态矛盾或多资产进入人工核对。没有新增 Schema、状态复制、服务、线程、队列、runtime 或依赖，见[重复下载 owner 验证](../validation/iteration-0.28.0-workflow-duplicate-download-owner.md)。
11. **配音语速端到端贯通已接线。** Editing `DubbingSpec`、严格 API、两张生产页面与 workflow preset 接受并恢复有限的 `0.88`～`1.12`；默认 `1.0` 从 canonical JSON 省略以保持旧 recipe/profile SHA，非默认值改变冻结 SHA 并经现有 render→SpeechOptions→protocol/bridge/worker→provider 路径成为 `speed`，同时进入脱敏 request fingerprint。计划与 workflow 卡片显示音色和未舍入语速，输入变化撤销旧外发同意。现有 timing overflow 继续失败关闭，不自动追加远程调用。没有新增 Schema、runtime、服务、线程、队列或依赖，也没有新增或修改已跟踪测试文件，见[配音语速验证](../validation/iteration-0.28.0-speech-rate.md)。
12. **编辑式玻璃四页生产视觉已接线。** 共享 CSS、四页标题、设计规范和预览已同步为方向 C；当前 Chromium 覆盖四页 × 三视口 × 两主题、减少动态/透明度、桌面 200% 根字体及 320/768/1024px 的 200% 交叉场景，共 44/44 PASS。四页业务 DOM/JavaScript 合同保持不变，既有相关 pytest 为 115 passed、3 failed；三项失败仍是历史三导航断言，测试文件按策略未修改。见[编辑式玻璃四页生产前端证据](../validation/iteration-0.28.0-editorial-glass-frontend.md)。这不构成新 clean release receipt 或真实平台证据。
13. **译文精确 revision 绑定已接线。** 新手动草稿和新 AI-ready workflow 草稿保存精确 `revision_id`；保存与计划创建复核项目、语言/provider/model、已批准父听写、source language 和 cue 结构，并在既有 `plan_timeline_bindings` 冻结修订及父修订摘要。legacy recipe 只有一个完整匹配时才自动恢复，零个或多个匹配保持未选并要求重选；mixed-state 可建立带精确绑定的 review 计划，但 dubbing 未 ready 时确认仍失败关闭。没有新增表、Schema、服务、队列或依赖，见[译文精确修订绑定记录](../validation/iteration-0.28.0-translation-revision-binding.md)。
14. **上传 attention 精确确认恢复已接线。** `UploadSnapshot.needs_confirmation` 明确表示当前 leaf 是否仍含 draft，并默认 `true` 让未分类适配器失败关闭。本地适配器对 draft + active/success 返回 true，对仅 queued/running 或 active + success 返回 false；Workflow 在 attention、awaiting 和 uploading 三处按同一字段恢复。手动草稿保留确认门，冻结 profile 已预授权的自动草稿才走既有 allowlist 确认，已经确认的工作无需再次点击或再次调用域确认；终态与 unknown/failed 边界不变。没有新增表、Schema、服务、线程、队列、runtime、依赖或已跟踪测试，见[上传 attention 恢复记录](../validation/iteration-0.28.0-workflow-upload-attention-recovery.md)。
15. **当前实际应用根运行时刷新已完成。** 停止应用后保留旧 Upload runtime，并用 SQLite backup API 将迁移前数据库保存到 Git 外；同一个经人工核对的 canonical `--app-root` 用于 Setup 与 Start。AI runtime 通过固定 CPython 3.13.15 归档与 manifest 检查，Upload runtime 验证固定 Social Auto Upload/Biliup 和三个目标适配器；实际上传库从 Schema 1 迁移到 Schema 3，既有账号行与两条 canceled job 保留，迁移前后 SQLite 检查通过。实际 Start 的下载 runtime、上传 backend/scheduler/worker 与四页 HTTP 入口通过；AI 完整性 verified，但无密钥时按预期停在 `provider_health_required`。没有登录、扫码、平台下载、OpenAI 调用、上传或发布。packaged 开发宿主需要同一 canonical `--app-root` 是已证实的 Windows filesystem virtualization 条件，不得放宽路径规则。见[当前应用根运行时刷新记录](../validation/iteration-0.28.0-local-runtime-refresh.md)。

16. **Workflow Schema 3 来源标题冻结与网址即运行已接线。** `/workflows` 默认使用下载来源标题，手工标题仍可明确覆盖；下载 ready 后从绑定 asset 的来源记录取得标题，X attachment 无自身标题时只回退到该下载 Job 的父来源标题，再按 Bilibili/抖音/视频号 capability 生成 80/30/100 字符上限内的逐账号最终标题并保留显式账号标题。`resolved_upload` 与 `download_asset_id` 一次原子冻结，重启、fan-out、上传准备和取消对账只复用快照；metadata/capability 不可用时以 `workflow_source_metadata_unavailable` 停下，本地对账不重复下载且不允许换绑 asset。精确 Schema 1/2 只按旧 profile grammar 迁移，旧行快照为空，伪造新 `title_mode` 会整笔回滚。该轻量切片只增加一个 nullable column 和不可变 trigger，没有新表、服务、线程、队列、runtime 或依赖。页面只记住成功应用/保存的预设 ID，依赖初次读取都成功且用户未编辑时才恢复，见[来源标题冻结记录](../validation/iteration-0.28.0-workflow-source-title.md)。

17. **下载 HTTP 本地边界已接线。** 顶层下载首页及非 Editing / Upload / Workflow 的 `/api/v1/*` 已复用共享 guard，校验唯一 loopback Host、同源 Origin/Fetch-Site，并让 8 个写路由只接受当前 `/api/v1/session` 返回的 `X-Download-CSRF`。页面在会话成功前保持提交禁用，四域令牌互不通用，成品 `private, no-store` 保持。ignored 合同探针和真实本地 Chrome 验证通过，没有真实下载或平台请求，见[下载 HTTP 边界记录](../validation/iteration-0.28.0-download-http-boundary.md)。

18. **无 AI 完整视频与 cover-only 已接线。** Editing recipe 的空分段列表现在表示一个完整视频输出；普通编辑计划和 Workflow 都走共享 `render_ordinary_plan`，由既有 FFmpeg/ffprobe、源 identity、claim staging、资产 hash 与事务注册边界生成 `segment-001.mp4`。显式分段和 AI `dubbed_video` 路径不变，cover-only 生成完整视频加 `cover.png`。真实本地媒体、服务/manager 重启、LocalAdapter、AI 隔离、两页 Chrome 与三平台合成 AI 全链均通过，见[无 AI 完整视频验证](../validation/iteration-0.28.0-no-ai-full-video.md)。

19. **逐账号相对发布时间预设已接线。** 生产页面可在不定时、绝对时间和“创建流程后 N 小时”之间选择；Schema 2 预设保存账号级策略和独立摘要，精确 Schema 1 可读并在下次写入时迁移。Bilibili/抖音按分钟、视频号按目标 instant 的本机时区整点向前取整；固定锚点与幂等键一同冻结，使响应丢失后的重放保持完整 profile 相同。执行层仍只接收原有绝对时间，没有新增数据库、服务、runtime 或依赖，见[相对发布时间预设记录](../validation/iteration-0.28.0-workflow-relative-schedules.md)。

20. **AI 重试完整谱系校验已接线。** Workflow 在确认、观察或显式重试前，从 Editing 域读取当前 project 的完整 AI task forest；逐项核对 canonical ID、project、起始任务 immutable 身份、父节点、单后继，以及 parent/child 的 operation、source revision 与 request SHA，并对全部独立根执行循环检查。合法多根仍可并存，目标链只返回自己的 leaf；缺父节点、隐藏循环、分叉、跨项目或请求漂移统一失败关闭为 `ai_task_set_invalid`。Editing 域运行错误继续保留原错误语义。该切片没有新增 Schema、服务、runtime、依赖或 tracked tests，见[AI 重试谱系验证](../validation/iteration-0.28.0-workflow-ai-retry-lineage.md)。

21. **Workflow 来源封面偏好已接线。** `upload.prefer_download_cover` 只以显式 `true` 保存，且 profile 必须同时生成封面作为 fallback。Workflow 按冻结的 `download_asset_id` 取得 owner envelope 与潜在 thumbnail 登记，完整验明 ready original、artifact owner/parent/kind、受管路径、MIME、大小和实际 SHA 后才做 0/1/多候选分流；0 个候选回退生成封面，多候选进入 `workflow_source_cover_ambiguous`，登记身份无效进入 `workflow_source_cover_unavailable`。来源封面必须兼容全部所选账号的平台封面槽，否则整组回退生成封面。`select_upload_cover` 先无副作用确定 managed ID，Workflow CAS 写入 `upload_cover_id` 后 `prepare_upload` 才能持久导入；已有请求从不可变 jobs 的共同封面槽和 Upload cover 元数据恢复并做 exact claim，尚无请求的取消则先原子写入并严格复核 stable-key sentinel 空 tombstone，不再依赖 Editing/Download 当前状态。多分段、任一阶段崩溃、重放和取消因此保持同一选择。实现保持本地模块化单体，没有新增服务、数据库、Schema、runtime、依赖或 tracked tests，见[Workflow 来源封面偏好证据](../validation/iteration-0.28.0-workflow-source-cover-preference.md)。真实 URL 提取及三平台采用/投稿尚未验收，视频号没有专用下载 extractor。

22. **Workflow 投稿失败批量重试已接线。** 完整 fan-out 处于 `upload_job_failed` 时，Workflow 先观察并 checkpoint 当前 retry leaf，再由 Upload Schema 3 的单一 `BEGIN IMMEDIATE` 事务按稳定 request key/digest、完整 root payload、唯一 lineage、账号 session、媒体、封面、schedule 和当前平台合同核对全部 slot。只有 failed/canceled leaf 创建新 draft；submitted、draft_saved、queued、running 和原 draft 原位保留，任一 unknown 整批阻断。响应丢失重放返回既有 leaf，部分准备不能进入 retry。所有新 draft 均再次要求明确确认；若同批另有原 draft，独立 `upload_retry_mixed_confirmation_required` 会说明最终确认范围。生产页面提供 `/uploads` 核对入口，完整失败批次才显示重试按钮。实现没有新增表、Schema、服务、线程、队列、runtime、依赖或 tracked tests，见[Workflow 投稿重试验证](../validation/iteration-0.28.0-workflow-upload-retry.md)。

23. **配音逐 cue 失败重试断点已接线。** render worker 从有界不可变 snapshot 核对每个 Speech WAV 的 SHA、完整 PCM 与实际消费字节，再通过轨道格式、总大小、时间槽和文件身份校验后，用原子 create-if-absent 副本写入该计划 retry root，并在现有 `requests` 表保存请求定义与音频 SHA-256 的不可变 manifest。显式重试只沿同 project/draft/recipe/批准时间轴/authorization 的唯一无分叉谱系查找；远程缓存还要求生产计划中相同 ordinal、规范化 fingerprint 和 authorization 的 invocation 已为 `responded`。命中后不会再次调用 provider；缺失、损坏或轨道条件不符的普通缓存只重做该 cue，更早的有效祖先仍可使用。`unknown`、`accepted_without_result` 与 `abandoned` 的现有阻断保持，成功提交 `ready` 后清理整条谱系，启动恢复覆盖原子临时链接与提交后清理前崩溃窗口。实现没有新增表、Schema、服务、线程、队列、runtime、依赖或 tracked tests，见[配音断点重试验证](../validation/iteration-0.28.0-speech-checkpoint-retry.md)。

**T20 当前证据边界：** 见[Workflow 投稿重试验证](../validation/iteration-0.28.0-workflow-upload-retry.md)、[Workflow 来源封面偏好证据](../validation/iteration-0.28.0-workflow-source-cover-preference.md)、[AI 重试谱系验证](../validation/iteration-0.28.0-workflow-ai-retry-lineage.md)、[相对发布时间预设记录](../validation/iteration-0.28.0-workflow-relative-schedules.md)、[无 AI 完整视频验证](../validation/iteration-0.28.0-no-ai-full-video.md)、[来源标题冻结记录](../validation/iteration-0.28.0-workflow-source-title.md)、[当前应用根运行时刷新记录](../validation/iteration-0.28.0-local-runtime-refresh.md)、[上传 attention 恢复记录](../validation/iteration-0.28.0-workflow-upload-attention-recovery.md)、[译文精确修订绑定记录](../validation/iteration-0.28.0-translation-revision-binding.md)、[编辑式玻璃四页生产前端证据](../validation/iteration-0.28.0-editorial-glass-frontend.md)、[配音语速验证](../validation/iteration-0.28.0-speech-rate.md)、[重复下载 owner 验证](../validation/iteration-0.28.0-workflow-duplicate-download-owner.md)、[预授权重启续跑记录](../validation/iteration-0.28.0-workflow-restart-continuation.md)、[三平台参数与封面预检记录](../validation/iteration-0.28.0-workflow-platform-parameters.md)、[自动流程服务端预检记录](../validation/iteration-0.28.0-workflow-server-preflight.md)、[多分段自动流程记录](../validation/iteration-0.28.0-multisegment-workflow.md)、[生产多分段界面记录](../validation/iteration-0.28.0-workflow-multisegment-ui.md)、[运行就绪度界面记录](../validation/iteration-0.28.0-workflow-readiness-ui.md)、[发布后自动流程正确性记录](../validation/iteration-0.28.0-post-release-automation-correctness.md)、[AI 精确授权与输入硬预算记录](../validation/iteration-0.28.0-post-release-ai-authorization.md)及[Schema 4 远程调用账本记录](../validation/iteration-0.28.0-ai-invocation-ledger.md)。本轮投稿重试 validator 为 11 组 PASS，覆盖事务/幂等、unknown/session、request digest/key、Workflow 对 Upload 自洽漂移的跨库阻断、三平台共享封面正向重试、整批回滚、Adapter、显式确认、mixed draft、跨库 checkpoint 和 partial fail-closed；生产 Chromium 验证覆盖完整/部分/unknown/mixed 状态、显式 POST、320 px、零外网和零 console error。当前来源封面 validator 的 11 组均通过，含真实临时 Schema 11 `create_app` 的 0/1/多候选、`.jpe` 规范化、损坏登记矩阵，以及两阶段选择、响应丢失、封面媒体删除、tombstone 重启/篡改和多分段取消；`compileall` 及 185 项聚焦既有回归通过。下载上传旧集成组的 14 项在建立批次时因未提供当前 Download CSRF 而收到 403，未修改测试绕过。其余既有证据仍按各自记录限定：当前只是未发布源码里程碑；来源标题 Python validator 为 7/7 PASS（含 source-title preset create/get/materialize round-trip）、Chromium validator 为 PASS；UI/upload 聚焦组最终复跑为 187 passed、3 failed，三项均是 `test_ui_design_system` 遗漏 `/workflows` 的 stale navigation 断言；restart-continuation 在并行首跑失败后隔离复跑 PASS，两次结果均保留；最新上传恢复聚焦验证为 7/7 PASS，automation/v0.28/restart/multisegment/full-chain/full-video 以及上传服务/韧性 43 项回归均 PASS。当前实际应用根又完成两个 runtime、Schema 迁移、实际 Start、四页 HTTP 与正常停机验证；AI provider 仍因无密钥未验证。语速、duplicate-owner、WAL、完整离线整链、compileall、页面内联 JS、依赖一致性、diff、既有浏览器回归与编辑式玻璃当前 44/44 Chromium 矩阵分别按对应记录执行。译文绑定的服务验证为 17/17 PASS、严格浏览器检查及 4 个既有浏览器回归 PASS，full-chain/full-video/speech-rate/multisegment 也均 PASS；聚焦 pytest 为 69 passed、2 个既有 Editing Schema 1 旧断言失败。最近一次完整仓库回归为 `2437 passed, 13 failed, 8 skipped`；其中 12 项仍断言旧版 `0.27.0`、Editing Schema 1、旧三页导航或旧发行排除清单，另一个 Windows 八进程上传 Schema 初始化参数单次失败后连续三次单独复跑通过，按偶发证据保留，完整 CI 仍不能标绿。测试文件按仓库策略保持未改，`tests/` 未修改。没有新的 clean release receipt，也没有真实 OpenAI、真实下载、真实平台上传/发布、实际价格或真人质量证据。

## 4. 建议分工与工作量

以下是工程安排建议，按 1 人专注工作日粗估，不是已承诺日程；平台验证等待、未知兼容性问题和第三方审核不计入编码估算。

| 阶段 | 建议分工 | 粗估 | 出口 |
| --- | --- | --- | --- |
| A：T01/T02 | 上传服务与运行时各一条独立工作线 | 3～6 人日 | G1 故障不丢语义、不重复上传，运行环境检查边界明确 |
| B：T03～T06 | 资产/API、前端状态、文档可并行 | 3～6 人日 | G2 可操作的状态与稳定数据边界 |
| C：T14、T07～T10 | 数据生命周期/恢复、测试/发布维护 | 5～9 人日 | G3 固定包、独立安装/恢复与自动门禁 |
| D：T11/T12 | 测试员逐平台执行，维护者复现问题 | 首轮约 2～4 测试人日，另加平台等待 | G4 逐平台证据与明确未通过项 |
| E：T13 本轮反馈 | 根据实际失败分配 | 由回报决定 | G5 关键缺陷闭环、业务状态稳定 |
| F：T16 | 前端设计/组件、交互与浏览器验收 | 本地实施与 G6 已完成（原估 5～8 人日） | G6 Apple 风格完整前端与持续设计规范 |
| G：T17 | 上传服务/适配器、数据迁移、前端与浏览器验收 | 本地实现与 G7 已完成 | G7 三平台参数可核对、持久化并安全映射 |
| H：T18 | 编辑域/媒体处理、页面与跨域导入 | 本地实现与 G8 已完成 | G8 分段、封面与显式上传导入闭环 |
| I：T19 | AI runtime、字幕/翻译/TTS 与 URL 自动流程 | 本地工程完成 | G9 固定模型、许可/隐私、失败边界已验证；真实 API 与人工试听待外部验收 |
| J：0.28.0 最终冻结 | 发布维护与独立验收 | 由包外 receipt 记录 | clean commit、五件制品、独立安装和包外 receipt 同一身份 |
| K：T20 发布后开发 | 自动流程 outcome、逐操作授权/硬上限、远端调用账本、复用预设、多输出 fan-out、服务端执行预检、安全重启续跑、编辑式玻璃四页生产视觉、译文精确修订绑定、上传 attention 精确确认恢复、Workflow 投稿失败批量重试、无 AI 完整视频/cover-only、Workflow Schema 3 来源标题冻结、网址即运行预设恢复、逐账号相对发布时间及当前应用根运行时刷新 | outcome、authorization/预算、Schema 4 ledger、Workflow Schema 2 多输出、Workflow Schema 3 来源标题快照、预设/API/浏览器、四项运行就绪度、零下游副作用预检、原始 queued 预授权续跑、真实域离线 smoke、2×3 故障恢复、上传确认恢复、当前应用根 Setup/Start 及 K5 当前 44/44 Chromium 矩阵已通过；当前制品及真实网络/模型仍待验收 | provider key、账号 session、授权与当前 runtime 检查均通过后，日常 workflow 可从 URL 开始；unknown 不会被静默重放 |

T15 的环境准备与 Linux/NAS 适配另估，不包含在 Windows 支线的工作量中。

当前既有 0.28.0 release receipt 只绑定 `0592b6f`，不覆盖 T20 发布后源码。Schema 4 ledger/unknown reconciliation、预设、真实域离线 smoke、Workflow Schema 2 多输出 fan-out、Workflow Schema 3 来源标题冻结与安全预设恢复、逐账号相对发布时间、服务端执行预检、预授权重启续跑、重复下载 owner 恢复、配音语速、方向 C“编辑式玻璃”四页生产视觉、译文精确修订绑定、上传 attention 精确确认恢复、Workflow 投稿失败批量重试及无 AI 完整视频/cover-only 已分别按对应验证记录复验；源码 Setup 的 AI runtime 路径也已在 ignored 临时根禁网通过。K5 当前生产字节的 Chromium 矩阵为 44/44 PASS；上传恢复 7/7、投稿重试 11/11 及其生产 Chromium 交互均 PASS，相关整链/回归也通过，译文绑定的服务 17/17、严格浏览器检查、4 个既有浏览器回归及四条既有整链均 PASS。当前实际 app-root 已保留旧上传 runtime 和私有 SQLite 备份，构建并验证两个 runtime，把 Upload Schema 1 数据库迁移到 Schema 3 并保留原记录；实际 Start、下载 runtime、上传 scheduler/worker、四页 HTTP 及正常停机通过。AI provider 因无密钥按预期仍为 `provider_health_required`。GitHub hosted CI 已在 `bdd88ce` 上执行四格并通过测试前环境与门禁，但完整 pytest 仍红，不能借此关闭发行门禁。下一步先从最终源码取得 clean commit，再执行冻结全量、source snapshot、detached 构建及源码/wheel 两类独立验收，并由新的包外 receipt 绑定同一提交、身份和五件制品；随后在单独明确授权下，用同一冻结构建执行真实 OpenAI、真人试听和三平台验收，才交给 T11/T12。T11、T12 与 T15 均仍为 **NOT RUN**。

上传 Setup 的独立 ignored validator 已在显式临时 app-root 禁网通过，覆盖精确 `data-uploads` 派生、默认/覆盖构建解释器、ready 复用、三层锁、固定失败码、旧/坏/含非允许内容的部分 runtime 不变和输出脱敏；当前实际 app-root 的后续刷新又验证了真实目录上的安全留档、两个 runtime 安装、Upload Schema 1→3 数据保留迁移与本地 Start。Workflow Schema 2 阶段已完成最多 10 段 × 3 账号的有序 fan-out、prefix checkpoint、重启/unknown/retry slot 对账、原始 queued 预授权续跑、一次完整批量确认、重复下载 owner 恢复、语速冻结/恢复、attention 后精确确认恢复，以及只替换 failed/canceled slot 的事务化投稿重试。当前 Workflow Schema 3 又把来源标题、逐账号最终标题与 ready asset 一次冻结；Workflow preset Schema 2 让上次成功预设在依赖读取成功且用户未编辑时恢复，并按每次创建的固定锚点生成逐账号相对发布时间。方向 C“编辑式玻璃”也已实施于生产四页并完成当前 Chromium 44/44 QA；translation revision 精确绑定已经完成并按独立证据复验。其余工作是冻结当前候选，并在明确授权与测试账号/素材具备后执行真实 OpenAI、真人试听及三平台外部验收。

## 5. 每个工作包统一交付检查

1. 在基线环境复现其具体问题，记录触发条件与安全错误码。
2. 复用 `tests/` 中既有回归，并在 ignored `validation/local/` 增加真正覆盖调用链的临时 validator，先观察失败；不得新增或修改提交中的测试文件。
3. 实现局部修改，确认原复现已消失，运行相关回归。
4. 进入阶段门槛时运行必要全量与集成检查；不在没有新风险时无限重复全量。
5. 更新当前说明和证据边界，保留历史提交/包 hash；清理或保留在明确 ignored 目录的临时调试材料。
6. 交付时列出：修改、原因、测试、尚未通过项、精确源码/制品身份及下一依赖。
7. 涉及前端时对照 DESIGN_SYSTEM，提交相应状态/视口证据；引入新的视觉规则时同步规范与预览，避免后续开发产生第二套风格。

执行计划完成的判据是这些交付物和平台证据实际存在；不是所有待办已被写进文档。
