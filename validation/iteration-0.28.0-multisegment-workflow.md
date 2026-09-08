# Iteration 0.28.0 多分段自动流程验证记录

日期：2026-09-09

范围：发布后开发工作树；Workflow Schema 2、有序多输出、上传 fan-out 与恢复语义
结论：本地 synthetic/offline 工程切片通过；真实 OpenAI 与三平台投稿仍未执行

## 实现范围

- Workflow Schema 2 新增 canonical `outputs_json`。每项按 `segment_ordinal` 排列，并冻结 `edit_output_id`、对应 `upload_source_id`，以及按 workflow profile 账号顺序排列的 `account_id`、`platform`、当前 leaf `job_id`。
- 自动流程接受最多 10 个视频输出和最多 3 个账号，因此单流程最多形成 30 个上传任务；该上限低于上传域一次 `confirm_many` 的 32 项边界。
- 编辑适配器按 recipe ordinal 核对 segment、dubbed video、caption 与 cover。启用配音时选择每段 `dubbed_video`；单输出继续提供兼容别名，多输出不会把首项伪装成唯一输出。
- 非 AI 多分段可分别渲染。自动 AI 多分段必须首尾连续，听写 clip 使用首段起点到末段终点；含间隙的选择在创建远端 task 前以 `workflow_ai_segments_must_be_contiguous` 拒绝，避免外发未选择音频。
- 上传准备仍按一个 edit output 建立一个 upload source 和一组账号任务。每段使用稳定幂等键，Workflow 在每段完成后保存 durable prefix checkpoint；进程或后段失败后，显式推进会从第一个未完成分段恢复，已经建立的任务继续保持 draft。
- 所有 segment × account 草稿只调用一次 `confirm_many`。确认前逐 slot 核对 source、account 与 platform；retry 只允许在同一 slot 原位更新 current leaf ID，并要求使用新 revision 再确认。
- `d30a39d` 对应切片中的自动流程页曾暂时拒绝多分段预设，避免旧单段表单静默丢失后续分段。后续生产多分段控件及浏览器验证见[自动流程多分段界面记录](iteration-0.28.0-workflow-multisegment-ui.md)；上传确认语义保持不变。

## Schema 1 → 2

迁移只接受精确 Schema 1，并在一个 `BEGIN IMMEDIATE` 事务中完成：

1. 核对表、索引、trigger、application/user/metadata version 与 SQLite 完整性；
2. 核对 profile 为 canonical JSON，且 SHA-256 与保存值一致；
3. 核对旧 profile 只可能产生一个视频输出：一个显式 segment，或启用 dubbing 的完整视频；
4. 核对账号 binding、旧 job 数量和 state/reference 组合一致；
5. 把合法标量引用转换为一个有序 output，并清空旧标量 authoritative fields；
6. 再按精确 Schema 2 验证后提交。

矛盾 job 数、伪造多分段 profile、`completed` 却没有上传引用、未知结构或迁移中验证失败都会回滚；数据库保持 `user_version=1`，不会残留 `outputs_json` 列。失败连接关闭后文件可立即移动。

运行时还按状态核对 output shape：下载/准备编辑阶段必须为空；渲染阶段只允许空或完整的未准备输出；`preparing_upload` 允许完整输出的有序准备 prefix；等待上传确认、上传中和完成态必须全部准备；attention/canceled 保留其当时可解释的空状态或完整 prefix。这样不能把空 fan-out 的记录解释为已完成。

## 本地验证

以下 ignored validator 位于 `validation/local/`，不进入 Git 提交、sdist 或源码 ZIP；两者都阻断 socket 创建：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe validation\local\validate_workflow_multisegment_service.py
.\.venv\Scripts\python.exe validation\local\validate_workflow_multisegment_adapter.py
```

结果：

```text
multi-segment service validation: PASS
workflow schema 1 to 2 migration: PASS
partial preparation recovery and leaf replacement: PASS
multi-segment adapter validation: PASS
upload target identity validation: PASS
```

覆盖的关键场景：

- 2 segments × 3 accounts 形成 6 个精确 slot；
- 第二段准备首次失败后进入 attention，第一段 3 个草稿已 checkpoint；显式推进后幂等恢复并补齐 6 项；
- 一个 retry leaf 改变时先保存新 ID 和 revision，不在同一次请求中沿用旧确认；第二次确认把 6 项一次性交给 adapter；
- 11 个 segments 在任何 domain side effect 前拒绝；
- AI 分段有间隙时在 task 创建前拒绝；
- 合法 Schema 1 单输出迁移，矛盾 job 数和状态/引用迁移回滚；
- output/source/job 标识、ordinal、数量、唯一性、账号顺序和平台错配均失败关闭。

源码检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

两项均通过。

既有相关回归执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_editing_ai_contracts.py tests\test_editing_service.py `
  tests\test_upload_service.py tests\test_upload_platform_parameters.py `
  tests\test_upload_ui.py tests\test_local_app.py tests\test_ui_design_system.py
```

结果为 **277 passed、3 failed**。三项失败都来自 `tests/test_ui_design_system.py` 的历史固定集合仍要求导航只有 `/`、`/edits`、`/uploads`，而当前页面早已包含 `/workflows`；本轮没有修改下载、编辑或上传导航，也没有修改或提交任何 `tests/` 文件。新行为由上述 ignored validator 覆盖。

移除这一个已知过时的测试文件后，同一 editing/upload/local-app 集合为 **263 passed**。文档与发行合同 `tests/test_current_operations_docs.py tests/test_release.py` 为 **102 passed**。当前 `WORKFLOW_HTML` 的唯一内联脚本另经 Node `--check` 通过；抽取文件保存在 ignored `validation/local/`。

## 证据边界

本记录证明有序多输出持久化、Schema 迁移、synthetic 编辑适配、draft fan-out、部分失败恢复、slot 对账和全批确认的本地代码合同。逐 source 的草稿建立是 checkpointed/idempotent，不是跨 source 的单事务创建；只有最后的完整 job 确认使用上传域一次原子批量操作。

本轮没有使用真实 URL 下载媒体，没有调用 OpenAI，没有登录或访问 Bilibili、抖音、视频号，也没有证明平台接收、保存草稿、定时发布、审核或公开成品。当前开发提交还没有新的 release receipt；此前 0.28.0 receipt 不能覆盖本轮源码。
