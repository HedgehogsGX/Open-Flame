# Iteration 0.28.0 Workflow 扫描恢复验证

日期：2026-09-10

修复起始提交：`0e7fce676f4a55a5cce015837cfd6f609ac6d12a`

范围：`WorkflowManager` 后台 reconciliation 的顶层扫描故障恢复。

## 修复内容

原实现只在逐条 `advance()` 周围处理异常。若读取活动页的 `active_page()` 抛出 `WorkflowError` 或 `sqlite3.Error`，后台线程会直接结束；之后的 `get()` 和 `wake()` 仍返回原 service，却不能恢复扫描。

当前实现把这两类已知扫描错误作为一次没有派发域操作的失败轮次处理：保留原扫描游标、跳过本轮任务派发、复用现有有界退避并由同一 worker 重试。没有加入线程自动重建，也没有改变逐条任务的 attention、数据库错误或未知远端结果处理，因此不会借恢复扫描盲目重放结果不确定的操作。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 冻结旧源码故障注入 | **RED**。`active_page()` 注入 `OperationalError` 或 `WorkflowError` 后均只扫描 1 次，worker 结束，`get()`/`wake()` 不能恢复。 |
| `validation/local/probe_workflow_manager_recovery.py` | **4/4 PASS**。两类顶层扫描错误均由同一 service 重试到第 2 次，worker 保持存活且没有未捕获线程异常；逐条 `OperationalError` 继续重试，逐条 `WorkflowError` 继续进入一次 `require_attention()`。 |
| `validation/local/validate_workflow_restart_continuation.py` | **PASS**。只有原始、已预授权且尚未 dispatch 的 queued 工作续跑；manual、retry 和 unknown 保持阻断。 |
| `uv run --offline pytest -q tests/test_local_app.py` | **93 passed**。只运行仓库已有回归；没有新增或修改测试文件。 |
| `python -m compileall -q src`、`uv lock --check --offline`、`git diff --check` | **PASS**。 |

验证探针和原始输出保留在被忽略的 `validation/local/`，不进入提交或发行载荷。本轮没有创建真实应用根，没有发出网络请求，也没有执行真实下载、OpenAI、账号登录、上传、审核或发布。该结果只证明本文件所在源码提交的本地恢复语义。

## 剩余边界

- 持续数据库不可用会按最多 6 秒的既有上限继续退避；需要修复存储或数据库后恢复。
- 未知的编程错误仍会结束 worker，避免在未知执行边界下自动重建并重放工作；应先固定日志、数据库和远端结果再修复。
- 架构审查发现的下载 API Host、Origin、Fetch-Site 与 CSRF 边界缺口不属于本切片，继续作为下一项独立加固工作。
