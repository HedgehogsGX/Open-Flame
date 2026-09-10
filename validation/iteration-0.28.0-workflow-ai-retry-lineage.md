# Workflow AI 重试完整谱系校验

验证日期：2026-09-11（Australia/Adelaide）

## 结论

Workflow 在继续、确认或显式重试 AI 工作前，不再只沿传入任务可见的 successor 链寻找
leaf。`LocalWorkflowAdapter._latest_ai_task()` 会读取当前 Editing project 的完整 AI task
集合，先校验整个 retry forest，再返回目标起始任务所在链的唯一 leaf。合法的 transcribe 与
translate 独立根可以共存；任一隐藏断链、分叉、循环、跨项目记录或请求身份漂移都会以
`ai_task_set_invalid` 失败关闭，不能继续确认、重试或远端调用。

这个切片只加强既有 Workflow/Editing seam，并复用 `_validate_retry_successors()` 和现有错误
码。没有新增数据库 Schema、表、状态、服务、runtime、依赖或通用抽象。

## 校验合同

1. 传入起始任务的 ID 与 project ID 必须是 canonical 32 位十六进制值；生产调用还会把
   Workflow 已冻结的 `edit_project_id` 作为独立 expected project 传入，不能由返回任务换绑。
2. `ai_tasks(project_id=...)` 必须返回 list；每项必须为 Mapping、属于该 project，且 ID 合法
   唯一。起始任务必须存在，并与调用方持有记录的 project、operation、source revision 和
   request SHA 完全一致。
3. 每个非根任务的 `retry_of` 必须合法且指向集合内父任务；禁止自引用及同一父任务出现多个
   successor。parent/child 必须保持 operation、source revision 与 request SHA 不变。
   `request_sha256` 已绑定 provider、model、options、authorization 与 project 等请求定义。
4. 循环检查覆盖集合中的全部合法独立根和 disconnected 子图，不能只检查目标链。完成后只沿
   指定起始任务的 successor 返回 leaf，不会把另一个 translation/transcription 根当成当前任务。
5. 集合或谱系结构异常统一为 `ai_task_set_invalid`，生产 Workflow 页面提示到 Editing 页核对。
   `editing_manager.invoke("ai_tasks")` 自身的运行错误不被改写，仍沿既有 Editing/Workflow 错误
   映射返回。

## 本地验证

以下结果只适用于本次工作树和本机 ignored/offline/synthetic 输入：

| 检查 | 结果 |
| --- | --- |
| `validate_workflow_ai_retry_lineage_20260911.py` | PASS：合法多根与两级 retry 返回目标 leaf；拒绝起始任务/父任务缺失、无效或重复 ID、跨项目、self/fork、reachable/disconnected cycle，以及 operation/source/request 漂移；EditingError 原样传播 |
| `validate_workflow_restart_continuation.py` | PASS：原始 queued 可按冻结授权续跑；retry、手动及 unknown 仍停下 |
| cancellation adapter、AI ledger recovery、AI authorization、Workflow preflight validators | PASS |
| source-caption adapter validator | PASS：来源字幕、translation-only retry 与 AI fallback 未受影响 |
| 完整 synthetic URL → AI → render → 三平台 upload service 链 | PASS |
| `test_worker_auxiliary_artifacts.py test_editing_ai_contracts.py test_editing_service.py test_local_app.py test_upload_platform_parameters.py` | 167 passed |
| Workflow preset production browser validator、`compileall`、`git diff --check` | PASS |

没有新增或修改 tracked `tests/` 文件。新的谱系矩阵以及为完整父子集合更新的 restart fixture
只保存在已忽略的 `validation/local/`，不会进入提交或发行包。

## 尚未证明

本记录没有真实 OpenAI key、真实平台账号或远端中断样本，因此不证明供应商调用、费用、模型
质量、三平台上传或发布。它证明的是本地控制面面对可读但不可信的重试集合会保守停止；也不把
SQLite 行变成密码学真实性证明。遇到 `ai_task_set_invalid` 时应保留现场并审计隔离数据库副本，
不能直接修改重试指针或摘要来恢复。
