# Iteration 0.28.0 Schema 4 远程 AI 调用账本记录

日期：2026-09-09

记录对象：0.28.0 发布后、逐操作 authorization 里程碑之后的当前未提交工作树。本文不绑定 clean commit、product identity、发行制品或 release receipt。

范围：Editing Schema 4、远程 AI invocation 生命周期、unknown 人工 reconciliation、重试/完成阻断、脱敏 API 与编辑页状态。没有提供真实 OpenAI 凭据，没有执行真实 OpenAI 请求、真实下载或 Bilibili/抖音/视频号登录、上传、审核及发布。

## 完成的行为

- Editing 数据库升级到 Schema 4，新增 `ai_invocations`。定义字段保存 invocation/owner ID、operation、ordinal、attempt、`request_units`、authorization SHA-256、owner definition SHA-256、request fingerprint、状态、固定 reason code、revision 和时间戳；不保存正文、媒体路径、API key、endpoint、HTTP header、provider request ID 或响应。
- 远程 `transcribe`、`translate`、`synthesize` 必须先持久化 invocation，再进入隔离 provider。定义字段由 trigger 禁止修改，删除也被禁止；状态 revision 每次只增加 1，且只允许受约束的单向转换。
- AI task 与 render plan 完成前检查所属 invocation；存在 `reserved`、`dispatched` 或 `unknown` 时返回 `ai_remote_reconciliation_required`，不登记完成结果。owner 失败时，尚未 dispatch 的 `reserved` 变为 `released`，已 dispatch 的记录变为 `unknown`，owner 固定为 `ai_remote_result_unknown`。
- `unknown` 只允许带 `expected_revision`、`acknowledge=true` 以及固定 `not_accepted`、`accepted_without_result`、`abandoned` 三项之一进行人工 reconciliation。没有自由文本；该决定不能撤销供应商请求、恢复远程响应或证明未计费。
- `reserved`、`dispatched`、`unknown` 阻止 AI task/render plan 重试；`reconciled/accepted_without_result` 和 `reconciled/abandoned` 也继续阻止重试。`responded`、`released`、`reconciled/not_accepted` 仅允许服务重新评估原有重试规则，仍须 owner 为允许的终态、没有分叉后继、authorization 未漂移并再次明确确认。
- 应用恢复把遗留 `reserved` 释放，把遗留 `dispatched` 标为 `unknown`，不会因进程重启直接重放。Schema 1/2/3 只有精确结构才可按顺序迁移；Schema 3→4 只为可能已经远程执行的旧 owner 建立 `legacy=1`、`attempt=0`、`unknown` 哨兵。能证明为本地 authorization 的旧记录不建远程哨兵；缺少或无法解析 authorization 时保守视为可能远程，允许 `authorization_sha256=null`，页面显示“旧记录未绑定”并要求人工核对。正常新记录仍要求 64 位十六进制摘要。

## 状态与核对合同

| 状态或结论 | 含义 | 完成 | 重试 |
| --- | --- | --- | --- |
| `reserved` | 调用 envelope 已持久化，尚未越过 dispatch 边界 | 阻止 | 阻止 |
| `dispatched` | 已把请求交给隔离 provider，尚无已验证响应 | 阻止 | 阻止 |
| `responded` | provider 结果已经协议与本地输出验证 | 允许继续完成 | 按原有显式确认规则评估 |
| `released` | 在 dispatch 前失败或取消 | 不构成远程完成 | 按原有显式确认规则评估 |
| `unknown` | dispatch 后无法证明远程是否接受或完成 | 阻止 | 阻止，须先人工核对 |
| `reconciled/not_accepted` | 操作者确认远程未接受 | owner 已是终态 | 按原有显式确认规则评估 |
| `reconciled/accepted_without_result` | 操作者确认远程接受但本机无可用结果 | owner 已是终态 | 阻止 |
| `reconciled/abandoned` | 操作者决定放弃本次调用 | owner 已是终态 | 阻止 |

## `request_units` 与 health 边界

- 听写 invocation 使用 1 个单位；配音按每个有文字 cue 建立独立 ordinal，每项 1 个单位。
- 翻译按完整 task 建立一个 invocation envelope，`request_units=ceil(cues/50)`，最多 20。它与 authorization 的 `max_requests` 一起作为本地输入/调用硬预算。
- OpenAI 翻译 provider 还会为满足 4 MiB request envelope 动态缩小 batch，因此实际 HTTP 请求数可能多于 `request_units`。这个字段不是精确 HTTP 请求计数、token usage、价格估算、provider request ID、付款记录或供应商账单收据；汇总值也不能用于费用对账。
- `health` 不属于 task operation，不建立 invocation，也不消耗 `request_units`。当前 OpenAI provider health 只返回本地声明的 operation 与标准音色，不发出 OpenAI API 请求。health 成功仍不能证明远程账号权限、模型可用性、网络、配额、价格或真实请求结果。

## API 与页面

- `GET /api/v1/edits/ai-invocations?project_id=<32hex>&offset=<n>&limit=<n>` 返回 `{items, summary}`；summary 包含各状态 `counts`、`total`、`request_units`、`unresolved`、分页 `offset/limit/has_more`，以及递归覆盖 retry lineage 的 `retry_blocked_ai_task_ids` 与 `retry_blocked_render_plan_ids`。列表分页不会缩小用于失败关闭的项目级阻断集合。
- `POST /api/v1/edits/ai-invocations/{id}/reconcile` 只接受 `{expected_revision, resolution, acknowledge:true}`；resolution 为上述三项固定值，而且 owner 必须已经进入 `failed` 或 `canceled` 终态。一个 owner 有多项 invocation 时，汇总按 `accepted_without_result`、`abandoned`、`not_accepted` 的保守优先级确定结果。
- `/edits` 在打开或刷新项目时读取账本，只显示短 ID/摘要、状态与单位。存在 unknown 时，AI task/render plan 明确显示“远程结果待核对”并不渲染重试按钮；对 `reserved`、`dispatched`、`reconciled/accepted_without_result`、`reconciled/abandoned` 及其全部 retry 后继同样隐藏远程确认和重试入口，并提供安全取消。迁移哨兵显示“迁移登记”和“实际远程发送时间未知”，不会把迁移时间冒充发送时间。账本读取或结构校验失败时，远程重试失败关闭。
- 账本轮询使用稳定渲染签名；未变化时不重建 AI 确认输入。阻断类别变化时才刷新关联 task/plan，并沿用已有 focus key 恢复焦点。
- `/workflows` 把账本阻断链接回编辑页。人工核对后，显式推进会重新检查 AI task/render plan：`not_accepted` 恢复原有显式重试流程；`accepted_without_result` 与 `abandoned` 保持阻断。旧 workflow 已创建的 retry 后继若被祖先账本阻断，也会返回 `attention_required/ai_remote_retry_blocked`，不会进入无效确认流程。

## 当前验证结果

| 检查 | 当前结果 |
| --- | --- |
| `uv run --frozen python -B validation/local/validate_ai_ledger.py` | PASS；输出 `ai-ledger-validation: passed`。脚本位于 ignored `validation/local/`，不会进入提交或发行包 |
| `uv run --frozen python -B validation/local/validate_ai_invocation_ledger.py` | PASS；输出 `AI invocation ledger validation PASS`。脚本位于 ignored `validation/local/` |
| `uv run --frozen python -B validation/local/validate_workflow_ai_ledger_recovery.py` | PASS；输出 `workflow AI ledger recovery validation PASS`。脚本位于 ignored `validation/local/` |
| `uv run --frozen pytest -q tests/test_editing_ai_contracts.py tests/test_editing_service.py tests/test_editing_api.py tests/test_editing_schema.py` | **41 passed、3 failed**；因此 focused 既有回归当前不是绿色 |
| `test_editing_manager_takes_root_exclusive_lease_before_service_recovery` | FAIL；仍精确断言旧 Editing Schema 1 |
| `test_worker_releases_root_lease_after_a_timed_out_stop` | FAIL；仍精确断言旧 Editing Schema 1 |
| `test_fresh_schema1_is_exact_and_idempotent` | FAIL；名称与断言仍以旧 Editing Schema 1 为当前值 |
| 同一 focused slice 排除上述三个过期精确 Schema 1 用例 | PASS；`41 passed, 3 deselected`，用于证明其余既有 focused 行为保持绿色，不掩盖完整命令的三项失败 |
| `uv run --frozen python -m compileall -q src/video_download_control/editing src/video_download_control/workflows` | PASS |
| 编辑页与自动流程页内联 JavaScript `node --check` | PASS；抽取文件位于 ignored `validation/local/` |
| `uv pip check` | PASS；24 个已安装包兼容 |
| `uv lock --check --offline` | PASS |
| `git diff --check` | PASS；仅报告既有工作树行尾转换 warning，没有 whitespace error |
| 本机实际浏览器检查 | PASS；从当前工作树启动 loopback 控制面，实际打开 `/edits` 与 `/workflows`，核对连接状态、响应式深色界面、AI blocked 文案与账本入口；没有执行真实网络、AI 或平台操作 |
| `tests/` 工作树 | 未新增或修改测试文件；上述三个旧断言按仓库测试文件策略保持未改 |

三个 ignored validator 和本机浏览器检查是本地定向工程证据，不能替代当前失败的完整 focused slice，也不能替代冻结全量、独立安装或真实远程验收。当前工作树仍不是可发布候选。

## 未完成与下一步

1. 加入不含 API key、Cookie、扫码状态或账号 session revision 的复用预设；运行时重新绑定当前 authorization、runtime 和账号。
2. 在禁止真实网络的环境中完成 URL→下载→听写→翻译→配音/编辑→Bilibili、抖音、视频号上传草稿 synthetic smoke，覆盖重启、unknown、reconciliation、绑定漂移和重复提交。
3. 提交前执行提交范围检查；focused slice 的 3 项旧 Schema 1 断言失败继续作为已知边界列示，本里程碑不修改或提交测试文件。
4. 若形成发行候选，从最终 clean commit 重新跑冻结验收并生成新的包外 release receipt。`0592b6f` 的既有 receipt 不覆盖当前发布后源码。
5. 真实 OpenAI 短样本、供应商 request/账单证据、中文/English 真人试听和三平台逐项发布验收必须由同一冻结构建另行执行；当前全部 **NOT RUN**。
