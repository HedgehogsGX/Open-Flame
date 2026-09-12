# Workflow 重试结果与取消公共尾段

日期：2026-09-12；分支 `codex/architecture-reset-ci`；重构前基线 `0c23fbe`。

## 改动

`WorkflowService._retry_upload` 在观察既有任务后、以及 Upload 返回重试结果后，原来分别维护
成功、等待执行、等待确认三组相同状态写入。现在同一函数 `_apply_upload_retry_observation`
处理这三种已经 checkpoint 的结果；调用者继续拥有重试权限判断。

前后两个阶段的不同规则保持显式：只有重试前观察到 `upload_job_failed` 才能调用 Upload；
调用后仍失败时直接进入 attention，不能循环建立新任务。观察后和重试返回后，均先通过 CAS
更新 job IDs 与本地 record revision，再分类及写入 Workflow 状态。
`upload_retry_confirmation_required` 与 mixed 确认码仍分别保留，其他确认原因不被冒充为重试。

`LocalWorkflowAdapter.cancel_uploads_for_workflow` 中，优先来源封面和普通封面分支原来各有
相同的 override 构造及 request claim。现在封面分流只负责确定封面身份，随后共同执行原尾段。
早期的空 tombstone 仍先于 Editing/Download 媒体访问；既有任务仍可通过
`require_media=False` 读取冻结封面元数据。没有移动前序分段取消、账号绑定与异常映射的次序。

两个改动净减少 17 行生产源码，不新增模块、状态、Schema、依赖或调度层。

## 当前验证

- 重构前后 67 组真实 Workflow SQLite 对照全部一致：比较返回值、完整 workflow 行、revision、
  job IDs、adapter 调用顺序和调用次数。覆盖成功三种 outcome、缺失 outcome、active、普通与
  mixed 重试确认、非法确认码、unknown、无效 snapshot，以及 job IDs 保持/替换两种情形。
- 对照包含重试前/后的 revision 竞争和不完整 job IDs；重试调用时还读取真实数据库，确认
  观察所得的最新 job IDs 已经写回。冻结身份、冲突错误与零自动确认均保留。
- 六组现有 ignored 集成验证在修改前后均通过：显式重试确认、跨库 checkpoint 恢复、部分
  结果失败关闭、导入前取消、tombstone 篡改/前序分段取消、准备阶段崩溃/媒体缺失后的取消。
- 普通封面分支通过实际 UploadService 的两段 × 两账号草稿建立、同键重放、冻结标题漂移
  拒绝、取消与重放；最终四个 job 为 canceled，backend dispatch 为 0。该探针还完成 65 项
  请求身份对照，0 差异。

脚本和结果在 ignored `validation/local/architecture-reset-20260912/workflow_tails/`。
集成探针只使用临时 SQLite、合成媒体与 fake adapter；保留的临时证据根也位于该目录。
没有修改 tracked tests，没有调用真实平台或模型。现有 tests 缺少直接 Workflow 回归；本轮
没有以其他领域测试的通过数替代这些生产调用路径检查，也没有重复宣称完整 pytest 已通过。

完整 CI、公共备份文件操作、最终文档与发行验收继续推进。历史 2194 passed、248 failed、
16 skipped 的全量结果只绑定其当时的封面工作树，不覆盖本提交。
