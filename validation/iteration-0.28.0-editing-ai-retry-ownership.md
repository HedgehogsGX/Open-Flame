# Editing AI 重试图所有权

日期：2026-09-12；分支 `codex/architecture-reset-ci`；重构前基线 `59168c1`。

## 改动与保持条件

此前 Workflow 为确定一个 AI task 的最终重试任务，会读取 Editing project 的全部任务并自行
重建父子关系、检查分叉、循环和请求身份；Editing 的项目级取消又维护一份同类校验。
同一个 Editing 领域规则因此需要同时修改两个业务域。

现在 EditingService 的 `_ai_retry_forest_in` 统一解码和校验整个 project；公开的
`resolve_ai_task_retry` 从同一个快照返回起点及最终任务。Workflow 通过既有 EditingManager
调用它，只复核返回合同以及自己持有的冻结 project、operation、source revision、request SHA。
起点和最终任务都必须匹配该独立身份，不能用一个新任务的自洽摘要替换 Workflow 的意图。

- 校验仍覆盖整个 project，包括与选中链无关的坏子图；缺父、自引用、分叉、循环、请求漂移
  均拒绝。合法多根与长历史不受任意深度限制。
- 环检查使用已完成集合，避免从每个任务重复扫描同一条后继链。
- 项目取消继续在原 `BEGIN IMMEDIATE` 中完成全部任务预检，再写入全部 leaf；保留原有
  leaf 顺序、invocation 祖先检查、运行中取消信号与失败回滚。公共 resolver 没有取消副作用。
- 坏任务记录在 Workflow 路径仍透传 `editing_data_invalid`；项目取消仍映射为
  `ai_task_set_invalid`。数据库读取错误与 manager 错误也保留原语义。
- 不改 `ai_tasks` 列表接口及其排序，不合并 render 与 AI 状态机，不新增模块、表、Schema、
  DTO、线程或第三方依赖。原 Workflow 的 render 重试检查仍服务其现有调用者。

## 当前验证

| 检查 | 结果与范围 |
| --- | --- |
| 重构前后真实 SQLite 对照 | 36 组，0 差异；比较返回值、错误类型/错误码、取消后全部任务状态 |
| 返回合同与错误映射 | 12 项通过，包括冻结起点漂移、最终任务漂移与读取错误透传 |
| `unknown` leaf / 祖先 | 两个独立 SQLite 场景均在第二个 leaf 预检被阻断；所有任务与 invocation 原样保留 |
| 预检写锁 | 第二条真实 SQLite 连接无法取得写事务；每次 leaf 预检前任务均尚未被修改 |
| 写入后的故障回滚 | 在最后一个返回记录读取处注入错误，已经执行的取消写入全部回滚 |
| 公开调用链 | 实际 `create_ai_task` → resolver，以及 Workflow → 实际 EditingManager → EditingService 通过；manager 停止后释放线程/活动 lease |
| 300 个任务的链 | 在实际函数中计数：取消时环扫描边访问由 44,850 次降至 299 次；未测量真实 UI 延迟 |
| 现有 Editing 回归 | `test_editing_service.py`、`test_editing_schema.py`、`test_editing_ai_contracts.py`、`test_editing_api.py`：41 passed、3 failed，7.23 秒 |

上述 3 个失败与分支起点完整基线的失败 ID 一致，失败断言均为 `4 == 1`：Schema 及两个
EditingManager 用例仍期待 Schema 1。没有更改 tracked tests，也没有恢复旧接口来满足内部
patch 路径。临时脚本、JSON、JUnit 与日志保存在 ignored
`validation/local/architecture-reset-20260912/ai_retry/`。

本切片没有重复运行完整 pytest。最近完整结果 2194 passed、248 failed、16 skipped 只覆盖
封面切片当时的源码；`59168c1` 的 hosted run
[34684423453](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34684423453) 在本次查询时
已有三格失败于完整离线测试，一格仍在运行，不能称为 CI 通过。以上均为离线合成数据验证，
没有调用模型、下载真实 URL、登录或上传。

后续继续收敛 Workflow 重试结果应用、取消分流后的公共尾段、公共备份文件操作，并处理
完整测试维护与当前发行验收；整体重置和全部 CI 恢复目标尚未完成。
