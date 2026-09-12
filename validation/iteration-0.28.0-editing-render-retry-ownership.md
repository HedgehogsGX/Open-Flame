# Editing 统一拥有 render 重试图

日期：2026-09-12；基线 `35b3924`；分支 `codex/architecture-reset-ci`。

Workflow 已知计划取消通过 Editing 的公开 `resolve_plan_retry` 取得经过验证的
leaf，不再自行读取并解释整组 plans。Editing 的该入口与发现式取消共用既有
`_latest_plan_for_project_in`，完整核对项目内父节点、分叉、循环和不可变字段。
Workflow 删除 `_latest_edit_plan` 与 `_validate_retry_successors`，共 73 定义行；
计入公开入口和错误处理后，两份生产文件增加 39 行、删除 84 行，净减 45 行。

## 保留的合同

- 公开解析使用 `BEGIN` 一致读；图、timeline binding 和 assets 属于同一读快照。
- 已知计划取消仍核对 Workflow 冻结的项目、名称和 source asset；取消事务再次检查
  后继。解析后出现新 retry 会返回 `waiting / render_retry_lineage_changed`。
- 发现式取消仍在同一 `BEGIN IMMEDIATE` 中发现请求、解析 leaf、写取消和 tombstone；
  没有拆成两个公开调用或跨域事务。
- 已知取消保留原具体 Editing 解码错误；发现式取消继续返回集合级错误。
- 持久化 invocation 的 unknown/禁止重试状态仍能阻止取消；Manager 在持久化后发送 Event。
- speech checkpoint 的 recipe、binding、claim、authorization、缓存与 invocation 证明
  保留独立实现。相关 12 个方法与基线 AST 相同，没有通用 retry framework。

环检测记录已经验证的链，避免重复遍历。300 节点链的实际函数计数从 44,850 次走边
降为 299 次；这只测环检测循环，不是完整数据库查询或取消耗时的性能承诺。

## 当前验证

- 33 组实际临时 SQLite 的修改前/后对照：取消 DTO、错误和数据库状态全部相同。
  包含不连通循环、缺父节点、分叉、字段漂移、损坏 JSON/hash、冻结身份和各计划状态。
- 10 个公开消费合同通过；错误/不完整 DTO 不调用取消。
- 发现式取消的真实写锁、写后异常回滚、错误映射及 tombstone 重放通过。
- WAL 中并发提交第三个后继时，解析读快照仍看到原两行；随后真实取消被后继保护拒绝。
- 祖先持久化 AI unknown ledger 阻断取消，数据库不变。
- 实际 EditingManager 的公开 invoke、持久化后 Event、退出与 lease 回收通过；无媒体执行。
- 现有 Editing 集合为 71 passed、3 failed、2 skipped；三个失败仍为旧 Schema 1 假设，
  两个跳过为缺少指定 Windows FFmpeg。随后完整诊断包含同一组失败。
- 独立复审在新的 ignored 目录重跑 33 组对照和全部专项合同，结果相同。

标准 `pytest -q` 仍因备份测试 import 已移除私有 helper 而收集失败：1 error，
退出 2，1.95 秒。诊断 `--continue-on-collection-errors` 为 2170 passed、250 failed、
16 skipped、1 error，退出 1，369.51 秒；相较上轮没有新增或消失的失败 ID。
22 个 Download backup 用例未收集，不能算通过；相同 ID 也不证明所有失败根因相同。
本轮没有修改 tracked tests。测试维护例外仍待答复，完整 CI 目标未完成。

源码编译、CI 定义、依赖和源码发行预检通过。`35b3924` 的 hosted run `34691113623`
在四格 offline pytest 失败，它不包含本切片；没有完整 hosted 日志或新的发行安装证据。

原始开发探针在 ignored `validation/local/architecture-reset-20260912/render_retry/`；
独立复审副本和当前测试/AST摘要保存在该目录的 `review-evidence/`。只使用生成数据、
本地 SQLite、锁、线程和假依赖；没有真实下载、FFmpeg、OpenAI、登录、上传或发布。

后续优先处理上传源文件校验到消费之间的所有权，再收敛跨页规则、CLI 和当前文档，
并在测试维护约束明确后逐类关闭完整测试、四格 CI 与最终制品验证。
