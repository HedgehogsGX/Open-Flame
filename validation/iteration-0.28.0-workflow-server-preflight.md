# Iteration 0.28.0 自动流程服务端执行预检验证记录

日期：2026-09-09

范围：发布后开发工作树；为 `/workflows` 增加服务端执行预检，使页面展示的四项就绪度在创建流程和建立下载任务前都由真实服务合同重新核对。

结论：自动流程现在会在任何下载、编辑或上传副作用之前核对本次托管下载 Worker、FFmpeg 编辑能力、上传 runtime/调度器、所选账号及会话修订，以及启用 AI 时的三项精确 authorization 与标准音色。创建阶段的预检失败不会保存 workflow 或 event，也不会创建下载 batch；创建成功后，在 `created → downloading` 的窄竞态点会用冻结账号绑定和完全无缓存的上传 runtime 校验再检查一次。即使有人原位同尺寸改写 runtime 并恢复 mtime，最多只能通过创建阶段的 identity-bound 摘要缓存，仍会在下载前被第二层拦住。该结论只证明本地服务端门禁与 synthetic/offline 整链，不证明真实网址可下载、OpenAI 可响应、账号 Cookie 仍被远端接受，或三个平台已经接收投稿。

## 实现范围

- `WorkflowDomainAdapter` 使用统一 `preflight()` seam。`WorkflowService.create()` 先保留已有幂等回放语义，再执行预检；只有成功时才写 workflow、初始 event 和冻结后的 `account_bindings`。响应丢失后的既有幂等请求直接返回原记录，不因环境稍后变化重复创建。
- `created` 状态在调用 `create_download()` 前再次执行同一预检，并要求当前 `account_id + platform + session_revision` 与已保存绑定完全一致。账号重新登录或被替换会以 `account_session_changed` 停下，不能把新会话拼接到旧流程。
- 下载门禁只接受本次 supervisor 管理的 direct Worker，且必须 `online`、网络下载已启用、队列未暂停、心跳数值有效且仍在期限内。应用刚启动、短暂暂停或心跳尚未到达时保留 `created` 并有界重试，不制造永久 attention 记录。
- 编辑门禁要求当前 `EditingManager` 已装配媒体处理器。启用 AI 时依次复核 `transcribe`、`translate`、`synthesize` 的 operation、provider、model、完整 authorization 及 SHA-256；配音还要求选定 voice 仍在当前 provider 的 `standard_voice_ids` 中。检查只读取当前声明与凭据状态，不调用 provider。
- 上传门禁先在一个 `BEGIN IMMEDIATE` 快照内验证逐平台参数、账号 `active + ready` 状态和登录 revision，再检查 runtime 与当前 scheduler。创建 admission 在 shared runtime lock 内跳过 10 秒整体验证结果缓存，重新枚举完整树；只有文件 identity、大小、mtime 与 ctime 均未变化时才复用逐文件摘要及 archive/wheel 解析结果。同一进程的 admission 扫描另由独立锁串行，避免并发冷请求同时重复散列；它不占用 WorkflowService 全局锁。`created → downloading` 使用完全无缓存校验；真正启动平台 child 前再次保留既有完全无缓存检查。
- 检查顺序为下载、编辑处理器、账号/上传 runtime、AI。账号未就绪或上传 runtime 不可用时不会反复扫描 AI runtime。成功流程只在创建和开始下载前各执行一次完整检查；runtime lock 被占用时快速返回 `runtime_busy`。
- 下载 Worker 的短暂状态、`runtime_busy`、上传数据维护和账号正在检查保持 active `created`，由后台有界退避重试；需要安装 runtime、修复凭据、重连账号或重建 authorization 的状态进入 `attention_required`，用户执行“立即对账”后会重新运行预检。
- Workflow API 将运行环境/调度器不可用映射为 `503`，账号会话或 AI authorization 漂移映射为 `409`；页面加入固定中文错误文案，不反射底层路径、异常或凭据。

## 本地验证

专用脚本保存在 ignored 的 `validation/local/`，不会进入 Git 提交。`validate_workflow_preflight.py` 使用外部 socket/DNS audit guard，并覆盖：

- 五类首次失败均保持 workflow/event 为零且 download 调用为零；同一幂等键在失败修复后可重新创建。两个同键请求可在全局流程锁外并行预检，但插入事务只保存一条 workflow 和一个初始 event。
- 创建成功后第二次检查拦截 authorization 与账号 revision 漂移；下载 Worker 暂未就绪保持 `created`，恢复后只创建一个 download。
- Local adapter 的非 AI 快路径、三项 AI 调用顺序、只在 `synthesize` 检查 voice、非法标准音色、FFmpeg 未配置、下载暂停/心跳过期，以及 expected account bindings 的逐层转发。
- UploadService 的 stopped、runtime upgrade、scheduler standby 和账号 revision 漂移；另用 workflow admission=`ready`、execution probe=`runtime_invalid` 的分歧后端证明第二层会在下载前拦住。真实 Local adapter 路径允许保存一条可核对 workflow，但断言 download 调用为零且 upload source/job 无新增。
- API 对 runtime 缺失返回 `503`、authorization 漂移返回 `409`，失败请求不会唤醒后台 manager。

结果：

```text
workflow-preflight-validation: passed
workflow-v028-validation: ok
workflow-ai-authorization-validation: passed
workflow AI ledger recovery validation PASS
automation-correctness-validation: ok
multi-segment service validation: PASS
workflow schema 1 to 2 migration: PASS
partial preparation recovery and leaf replacement: PASS
multi-segment adapter validation: PASS
upload target identity validation: PASS
workflow-preset-api: PASS
workflow-readiness-browser: PASS
workflow-multisegment-browser: PASS
workflow-preset-browser: PASS
AI authorization validator: 45 checks passed; provider runner calls 0
upload runtime integrity + backend + service + API: 188 passed
compileall: PASS
node --check current workflow inline script: PASS
uv pip check: 24 packages compatible
git diff --check: PASS
```

在现存 3,498 文件、984,153,133 bytes 的 Schema 2 本地 runtime 上，冷 workflow admission 为 34.413 秒，紧接的 identity-bound admission 为 2.841 秒；OS cache 已热但进程摘要缓存为空时另测 8.970 秒，完全无缓存检查为 8.798～10.049 秒。生产路径因此不会连续冷散列两次，同时仍在下载前做一次完整内容校验。首次冷 admission 已移出 WorkflowService 的全局 mutation lock；同进程并发 admission 会串行复用逐文件缓存，同一幂等键仍在插入事务内二次核对。

完整真实域离线整链再次通过：一个 URL 依次进入真实 `LocalWorkflowAdapter`、下载 Worker、隔离 AI worker、FFmpeg 编辑和三个本地平台任务；重启时上传域先撤回旧 queued 确认，随后 WorkflowManager 重新校验并消费原流程保存的上传预授权，无需人工调用批量确认。听写 1 次、翻译 1 次、provider health 1 次、逐 cue 配音 2 次，最终为 synthetic `submission_acknowledged`。网络 guard 保持启用，平台后端为替身；重启语义的专项证据见[预授权重启续跑记录](iteration-0.28.0-workflow-restart-continuation.md)。

相关回归集合在保留历史测试文件不变的情况下得到 `568 passed, 1 failed, 3 deselected`。失败和三个明确排除项均为当前 HEAD 已存在的旧身份断言：`tests/test_api.py` 仍期望版本 `0.27.0`，`tests/test_editing_api.py` 与 `tests/test_editing_schema.py` 仍期望 Editing Schema 1，而生产代码已为 `0.28.0` / Schema 4。本轮没有修改或提交 `tests/`。上传轮询资源预算在组合运行中两次出现 Windows handle `818 → 819` 的单句柄抖动，隔离重跑通过；该用例不经过本次 workflow preflight 路径。

## 保留边界

- 下载、编辑、上传分别使用独立持久化域，没有跨域全局事务。预检之后状态仍可能变化，因此 AI task 创建/确认、渲染确认、上传草稿创建、批量确认和平台 child 执行前的既有 CAS/完整性检查全部保留。
- `unverified` AI capability 只证明本地 runtime、模型声明、凭据格式和精确 authorization 可进入首次真实调用；不证明 provider health、额度、模型权限、价格或结果质量。
- 本地账号 `ready` 与 session revision 只证明最近一次本地检查和会话代次；不证明远端 Cookie 此刻仍有效。预检不会自动登录、扫码或请求平台。
- 发布时间仍会在创建上传草稿和批量确认时按当时的最小提前量重新验证，避免长时间下载/编辑后沿用过期时间判断。
- 真实 Bilibili、抖音、视频号投稿/草稿保存、定时触发、平台审核和公开可见状态仍为 **NOT RUN**；当前源码也没有新的 clean release receipt。
