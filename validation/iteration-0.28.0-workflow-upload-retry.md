# Iteration 0.28.0 Workflow 投稿重试验证

> 日期：2026-09-11（Australia/Adelaide）
> 初始投稿重试基线：`706da5f`；本轮跨库投稿意图加固基线：`a4cbe83`；本记录绑定包含本文件的后续签名提交。
> 范围：本地 Upload/Workflow 数据库、服务、适配器与生产 `/workflows` 页面。没有调用真实平台或 OpenAI。

## 结果

Workflow 现在可以在完整投稿 fan-out 中出现 `upload_job_failed` 时，直接为失败或已取消的当前 leaf 建立新草稿。已经提交、已保存平台草稿、仍在排队或运行中的 slot 保持原任务；远端结果为 `unknown` 时整批拒绝重试。

新草稿不会继承原 workflow 的自动确认。无论该 profile 是否曾预授权自动上传，操作者都必须再次明确确认后，Upload scheduler 才能排队草稿。

## 事务与身份边界

- `UploadService.retry_many()` 在一个 `BEGIN IMMEDIATE` 事务中读取并校验全部 slot，全部通过后才插入后继；任一 source、cover、schedule、metadata、账号或 lineage 错误都会整批回滚。
- Workflow 重试会从冻结的 `profile.upload` / `resolved_upload`、已绑定账号与已选封面重建每段完整 expected request；Upload 在同一事务中同时比对 ledger digest 和 root 的 source、title、description、tags、category、mode、copyright、source credit、cover、schedule 及 platform options。即使 Upload DB 内 root 与重算的 v2 digest 彼此自洽，只要与 Workflow 冻结意图不符，仍在创建 successor 前以 `upload_request_mismatch` 失败关闭。
- 每个 slot 重新绑定其稳定的 `wf-{workflow_id}-upload-jobs[-NNN]` request key。request ledger 只能引用 `retry_of IS NULL` 的原始 root；Upload Schema 3 既有 `requests.digest` 会从这些 root jobs 的标题、简介、标签、封面、模式、发布时间及平台参数重新计算。root 漂移、把 request 关系改指 retry descendant、错误 key、缺失 request 或不完整 fan-out 都失败关闭。
- retry forest 必须是无循环、无分叉、逐边 payload 相同的单链，且只有 failed/canceled/unknown parent 可以拥有后继。调用方记录落后时会返回当前唯一 leaf，避免响应丢失后重复建草稿。
- `submitted` 只允许对应 `publish`，`draft_saved` 只允许对应 `draft`。`unknown` 始终返回 `verify_remote_result_first`，Workflow 不提供 acknowledge 绕过。
- candidate 账号在建草稿时核对冻结的 `session_revision`；最终确认仍会再次核对账号 readiness、session、媒体、封面、schedule 与当前平台 metadata 合同。

## Workflow 与页面行为

- 服务端只接受当前 code 为 `upload_job_failed`、所有输出均已准备、每段 target 数等于账号数且总 job 数完整的批次。多分段 prefix checkpoint 不能借 `/retry` 提前进入确认。
- 重试前先观察 Upload 域并把当前 leaf IDs 写回 Workflow。Upload 已提交但 Workflow 尚未 checkpoint 的窗口会恢复同一后继，不会再建一代。
- 普通重试草稿使用 `upload_retry_confirmation_required`。如果同批还有非 retry 的本地草稿，则使用 `upload_retry_mixed_confirmation_required`，明确说明最终确认也会排队这些原草稿。
- `/workflows` 只在完整失败批次显示“建立失败投稿重试”，上传相关 attention 始终提供 `/uploads` 核对入口；`unknown` 和部分准备批次不显示重试按钮。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --no-sync python -m compileall -q src` | PASS |
| `uv run --no-sync python -m pytest -q tests/test_upload_service.py tests/test_upload_resilience.py tests/test_upload_api.py` | `76 passed` |
| 全部既有 `tests/test_upload_*.py` | `539 passed` |
| `uv run --no-sync python validation/local/validate_workflow_upload_retry.py` | 11 组 PASS：原子/幂等、unknown/session、request root/digest/key、Workflow 对 Upload 自洽漂移的跨库阻断、三平台共享封面正向重试、回滚、Adapter、显式确认、mixed draft、checkpoint、partial fail-closed |
| `uv run --no-sync python validation/local/validate_workflow_upload_attention_recovery.py` | 7 组 PASS |
| `uv run --no-sync python validation/local/validate_workflow_restart_continuation.py` | PASS |
| `uv run --no-sync python validation/local/validate_workflow_multisegment_service.py` | PASS |
| `node --check validation/local/validate_workflow_upload_retry_browser.cjs` | PASS |
| `node validation/local/validate_workflow_upload_retry_browser.cjs` | PASS：完整/部分/unknown/mixed 状态、显式 POST、`/uploads` 链接、320 px、零外网与零 console error |
| `git diff --check` | PASS；仅 Git 的 CRLF→LF 提示 |

本轮没有新增或修改 `tests/`。专用 Python/Chromium 验收脚本、截图和 JSON 报告位于被忽略的 `validation/local/`，不会进入提交或发行制品。

## 架构结论与未证明范围

实现只加深现有 Upload request ledger、retry lineage、`WorkflowDomainAdapter` 和 Workflow 状态机；没有新增服务、数据库、Schema、线程、队列、runtime、依赖或框架。

这些结果证明本地事务、恢复、确认与页面行为，不证明 Bilibili、抖音或视频号接收、审核、定时执行及公开可见，也不证明真实 URL 下载、OpenAI 翻译/配音质量或费用。真实结果仍须绑定同一 clean 候选并逐平台验收。
