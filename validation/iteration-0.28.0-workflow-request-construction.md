# Workflow 请求键与冻结投稿字段统一构造

日期：2026-09-12；分支 `codex/architecture-reset-ci`；起点 `470c05c`。

原来 Workflow service、准备上传、取消恢复各有一份 per-segment 请求键模板；创建 Upload
jobs 又手写了已经用于 retry/cancel 身份检查的完整投稿字段投影。现在由现有
`workflows.contracts` 中两个纯函数统一维护，不增加模块、数据库、依赖或运行服务。

`workflow_upload_request_key` 保持首段无 ordinal 后缀、后续三位 ordinal 的原字节；
调用者保留 workflow/segment 校验。`workflow_upload_request` 供创建、retry 和 cancellation
共享原冻结字段投影；创建边界仍单独复制 tags，保持原有可变列表隔离及转换行为。
原私有方法和三处重复格式化表达式已移除。

验证：

- 65 组与冻结实现的比较没有差异：33 组段/账号展开顺序，32 组投稿字段、空值、Unicode
  与 override 投影。字段和顺序比较包括 canonical JSON 文本；起点和脚本固定在本记录基线。
- 实际 LocalWorkflowAdapter 与 UploadService 使用临时 SQLite/受管文件，两平台×两段
  创建 4 个 draft；创建重放保持同一 jobs/键和账号次序；改变冻结标题以原
  `idempotency_conflict` 拒绝且不增加 jobs；发现当前未 checkpoint 段并携带前序段 roots
  的取消与取消重放都成功。Editing 仅使用合成 output resolver，backend 只模拟账号检查，
  没有实际编辑、确认上传或任何 dispatch。
- `tests/test_local_app.py` 原样运行 93 passed，2.96 秒。
- 全部 129 个生产 Python 文件可解析；whitespace、源码发行预检和 staged scope gate 通过。
- 没有修改 tracked tests；完整 Workflow 重试副作用矩阵仍由后续结果应用切片继续验证。

临时前后对照、集成脚本、JSON、日志/JUnit 位于 ignored
`validation/local/architecture-reset-20260912/`。封面平台规则、Editing AI retry forest、
Workflow 重试结果应用、完整 CI 和最终发行验收仍未完成。
