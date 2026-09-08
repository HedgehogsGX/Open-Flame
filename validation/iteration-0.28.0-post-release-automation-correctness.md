# Iteration 0.28.0 发布后自动流程正确性记录

日期：2026-09-08

基线：`0592b6f96c8eef60381b31b1e78e5ebf6d7c6a1d` 及其有效包外 0.28.0 release receipt

范围：本地源码、synthetic backend、既有回归和 inline JavaScript；未进行真实 OpenAI 或平台调用

## 完成的行为

- Workflow terminal state 保持 Schema 1 的 `completed`，并用结果码区分 `submission_acknowledged`、`platform_draft_saved` 与合法的 `submission_and_draft_acknowledged`。这些结果只表示上游接收投稿或保存草稿，不表示审核、定时执行或公开发布。
- `submitted` 只接受 publish 任务，`draft_saved` 只接受 draft 任务。缺少或矛盾 outcome 会停止处理；混合 publish/draft 目标按各自实际结果完成。
- 上传账号返回 `account_invalid` 或 `account_missing` 时，活动账号标记 invalid，同账号尚未执行的 queued 任务原子退回 draft 并撤销旧确认。若调用后结果是 unknown，Workflow 仍优先提示先核对平台，避免重复投稿。
- `/workflows` 的 pending idempotency key 使用 v2 sessionStorage 命名空间。响应丢失前的重试复用同一个 key；成功收到创建响应后清除 key，使用户能用相同参数再次运行。旧版本永久残留的 key 不再误复用。
- Workflow manager 在持续无进展时从 0.75 秒有界退避至 6 秒，API mutation 仍会主动唤醒；没有新增常驻进程、SDK、模型权重或核心依赖。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| Python `py_compile`：7 个生产模块及 ignored 验证脚本 | PASS |
| ignored `validation/local/validate_automation_correctness.py` | PASS；覆盖三类 outcome、缺失/mismatch、unknown 优先、账号 checking 竞争、queued 撤回、浏览器幂等与轮询退避 |
| ignored `validation/local/validate_workflow_v028.py` | PASS |
| `tests/test_upload_service.py tests/test_upload_resilience.py tests/test_upload_platform_parameters.py` | 83 passed in 30.75s |
| 上传页与自动流程页 inline JavaScript `node --check` | PASS |
| `uv pip check --python .venv/Scripts/python.exe` | PASS；24 个已安装包兼容 |
| 独立只读代码复核 | PASS；最终 diff 未发现 P0/P1/P2 阻塞项 |
| `git diff --check` | PASS；仅显示既有 CRLF→LF 提示 |
| `tests/` 工作树与提交范围 | 0 项变更；127 个历史测试文件保持冻结 |

## 仍未完成

下一切片按轻量架构继续：把 AI 同意绑定到精确 runtime manifest、provider/model 与每项 operation，执行前加入音频分钟数、翻译批次/字符数、TTS cue/字符数硬上限，并保存本地费用/请求账本；之后加入不含密钥的可复用自动化预设，使日常运行只需 URL，同时让账号 session 在运行时重新绑定。平台侧还需持久化可脱敏的 submission receipt 与 reconciliation；真实 OpenAI 质量、费用、真人试听和三平台发布仍须在绑定对应冻结构建后分别验收。
