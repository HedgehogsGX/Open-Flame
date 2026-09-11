# Iteration 0.28.0 Upload Schema 4 attempt receipt 记录

日期：2026-09-11（Australia/Adelaide）

记录对象：0.28.0 发布后 Upload Schema 4 源码提交
`496fb63f9d0010c22fa1abc660e6450cd403b6be`。本文随后的文档提交只记录结果；它不是
release receipt，不能覆盖 `0592b6f` 的冻结构建或借用其结果。

范围：Bilibili、抖音和视频号（内部 ID `tencent`）上传 job 的 durable attempt
receipt、dispatch 边界、固定 adapter/evidence、`unknown` 人工 reconciliation、
Schema 1/2/3→4 迁移以及上传备份格式 3。没有执行真实登录、扫码、上传、定时发布、
审核或公开可见性检查；本轮真实平台调用数为 **0**。

## 结果

- Upload Schema 4 为每个已领取 job 保存唯一 `upload_attempts` 行。领取使用
  `BEGIN IMMEDIATE`，在同一事务写入 `reserved` receipt 并把 job 从 `queued`
  转为 `running`；适配器调用前另行持久化 `dispatch_may_have_started`，返回后把
  receipt 结果与 job 终态同事务提交。
- receipt 绑定 attempt/job/root/request 身份、request 与完整 job 摘要及版本、账号、
  平台、账号 session revision、source ID/SHA-256、cover ID/SHA-256、product build、
  adapter name/revision、state/result/evidence/reconciliation、时间戳和 revision。
  读取路径在一个显式只读事务中取得 job、receipt、request、lineage 和 state 的同一
  WAL snapshot；ID grammar、摘要和历史终态 receipt 也必须继续通过完整身份检查。
- 当前自有 backend 必须实现 `receipt_identity()` 并返回精确、已列入追加式历史允许表
  的 adapter identity。缺失、未知版本、平台错配或持久数据漂移以
  `upload_attempt_identity_changed` 失败关闭；并发 revision/state 改变以
  `upload_attempt_conflict` 失败关闭。升级 adapter 时只能保留旧身份并追加新身份，
  不能用宽泛版本或 `unversioned` 身份接受历史 receipt。当前 Bilibili 身份为
  `biliup/v1.2.4`；抖音与视频号为
  `social-auto-upload/0012d2c355f88f683cc38dde2a2db209e14091bc`。
- 自动成功只接受平台、mode、result 与本地 evidence 的固定组合：Bilibili publish /
  `process_exit_zero`，抖音 publish / `uploader_returned_after_final_action`，视频号
  publish / `https_errcode_zero`，视频号 draft / `post_list_navigation`。适配器
  `result.json` 只接受 `status`、`code`、`evidence_kind` 三个字段并保持有界。

## `unknown` 与恢复合同

| receipt / job 情况 | 恢复或人工结论 | 是否允许后续 retry |
| --- | --- | --- |
| `reserved`，未越过 dispatch 边界 | `responded` + `failed / upload_dispatch_not_started` | 可按普通显式流程建立新草稿并再次确认 |
| `dispatch_may_have_started`，无可信返回 | `unknown / interrupted_result_unknown` | 否；必须先人工核对 |
| 当前格式 3 / Schema 4 的 `running` job 没有 receipt | `unknown / attempt_receipt_missing` | 否；没有可验证的 dispatch 证明 |
| 旧格式 1/2 的 `running` job 没有 receipt | 保留其已声明的 `interrupted_result_unknown` 或 legacy metadata 恢复原因 | 否；不能读取 receipt 或人工 reconciliation，须从 source 手工重建 |
| 任意旧版或其他无 receipt 的 `failed` job | 保留历史记录；重试报 `attempt_receipt_missing` | 否；从重新核验的 source 手工建立新草稿 |
| 无 receipt 的 `canceled / canceled` | 保留历史记录；可变 job 状态不能证明从未 dispatch | 否；从重新核验的 source 手工建立新草稿 |
| 人工 `not_accepted` | `reconciled` + `failed / manual_remote_not_accepted` | 是；只允许另建新草稿并再次确认 |
| publish 人工 `submission_acknowledged` | `reconciled` + `submitted / manual_submission_acknowledged` | 否 |
| 视频号 draft 人工 `draft_saved` | `reconciled` + `draft_saved / manual_platform_draft_saved` | 否 |

人工核对必须先读取 receipt，再到对应平台后台按账号、标题、时间和测试编号检查，勾选
“已在对应平台后台核对”，并提交固定结论和 expected revision。相同结论在响应丢失后
幂等重放；冲突结论、旧 revision、错误平台/mode 或非当前 lineage 失败关闭。旧
`acknowledge_unknown` 请求体不再接受。仍无法判断平台是否接收时必须保持 `unknown`；
操作者的勾选和结论是声明，不是独立的远端证明。

## Schema 与备份

- 精确 Upload Schema 1/2/3 只在结构和业务语义通过时事务化迁移到 Schema 4。迁移不
  为旧 job 补造 receipt；旧 running job 保持需要人工核对的保守语义，旧 failed job 也不能
  在缺少 dispatch 证明时进入 retry；无 receipt 的 `canceled / canceled` 也不能作为例外，因为
  单独的可变 job 状态无法提供取消来源证明。
- 上传备份格式 3 保存 Schema 4 数据库、attempt receipts、非秘密账号历史、sources、
  cover assets、jobs、operations、requests/retry 关系及登记且在位的受管媒体。审计会
  重算 request/job/source/cover 身份，并核对 adapter identity 历史允许表、固定 evidence、
  state/result/reconciliation 和 revision/timestamp 组合。
- 严格通过的格式 1 / Schema 2 与格式 2 / Schema 3 只作为只读输入在 staging 中迁移到
  Schema 4；旧备份不改写，也不补造旧 receipt。restore 不读取账号秘密、不构造 backend、
  不登录、不扫码、不上传。格式 1/2 保留其不可变 metadata 已声明的历史 running 恢复码；
  格式 3 与普通 Schema 4 启动则把 running 且无 receipt 精确标为 `attempt_receipt_missing`。
- 2026-09-10 的当前实际 app-root 记录只完成 Upload Schema 1→3 数据保留迁移。当前
  Schema 4 尚未在该实际 app root 执行或审计；临时 Schema 4 数据根和浏览器 smoke
  不能替代这项验证。

## 当前冻结验证

以下结果来自源码提交 `496fb63f9d0010c22fa1abc660e6450cd403b6be`。ignored validator
及生产页面 smoke 使用独立临时数据根，没有读取实际 app root 或联系真实平台。

| 检查 | 当前结果 |
| --- | --- |
| 最终源码与签名提交身份 | PASS：`496fb63f9d0010c22fa1abc660e6450cd403b6be` 已直接 push 到 `https://github.com/HedgehogsGX/Open-Flame.git` 的 `main`；本地与 GitHub API 均显示 author/committer 为 `Cyaegha_Xu <85352261+novahanser@users.noreply.github.com>`，GitHub login 均为 `novahanser`，签名 `verified=true / valid` |
| `validation/local/validate_upload_attempt_schema4.py` | PASS：22/22；包含格式 3 无 receipt running 的精确诊断与格式 2 历史策略保持 |
| `validation/local/upload_attempt_service_validator.py` | PASS：14/14 |
| `validation/local/validate_upload_backend_receipt_evidence_20260911.py` | PASS：18/18 |
| `validation/local/validate_upload_attempt_api_ui_20260911.py` | PASS：2/2 |
| `validation/local/validate_workflow_upload_retry.py` | PASS：11/11 组；fixture 使用真实 Schema 4 receipt transition |
| 本机生产 `/uploads` 浏览器 smoke | PASS：`validation/local/browser-upload-attempt-schema4-496fb63/data`、`127.0.0.1:18837`。读取前两个 reconciliation 按钮禁用且无 retry；勾选后台核对后按钮启用；`not_accepted` 后出现唯一可用 retry；回执显示 `biliup · v1.2.4`。本地取消的无 receipt 草稿只显示重新从受管视频建草稿的提示，不显示第二个 retry。最终 CUA 交互无页面运行异常；本次未挂接独立 console/network recorder，所有主动导航和 API 目标均为该 localhost，未确认任何上传，真实平台调用数 0 |
| 受影响的既有上传 pytest | **483 passed、56 failed，201.91 秒**。失败仍来自被冻结在旧合同的测试：31 项 service/backend/API 等路径首先被旧 fake backend identity、旧两字段 result、无 receipt 直重试、旧 unknown API 或旧表集合阻断；12 项 Schema/backup 测试固定断言 Schema 3 / backup format 2；13 项 UI 测试固定断言旧 retry/unknown 文案与 fixture。只读分组复核及当前 validator 未发现生产 `SauBackend`、service、API 的 P0/P1 行为回归；FakeBackend 未迁移属于 P1 CI/验证门禁债务，其余属于 P2 冻结断言漂移 |
| `python -m compileall -q src/video_download_control/uploads` | PASS |
| `uv pip check` | PASS：检查 24 个已安装包，全部兼容 |
| `git diff --check` 与 `scripts/verify_commit_scope.py --staged` | PASS：源码提交前两项均通过 |
| `tests/` 提交范围 | PASS：源码提交与后续文档范围均无新增或修改的 tracked test 文件 |

既有回归使用的精确命令为：

```powershell
uv run --no-sync pytest -q --tb=no tests/test_upload_service.py tests/test_upload_schema3.py tests/test_upload_schema2.py tests/test_upload_runtime_integrity.py tests/test_upload_resilience.py tests/test_upload_platform_parameters.py tests/test_upload_login.py tests/test_upload_lifecycle.py tests/test_upload_backup_restore.py tests/test_upload_backup_cli.py tests/test_upload_backend.py tests/test_upload_api.py tests/test_upload_activity_lock.py tests/test_upload_ui.py
```

ignored validator、临时数据库和浏览器数据只放在 `validation/local/`，不进入提交或发行
包。冻结历史测试若仍断言旧 Schema、旧备份格式、旧两字段结果文件或旧 unknown 直重试
行为，应逐项如实记录为 stale failure；不得通过修改 tracked tests 或放宽当前生产合同把
结果写成绿色。

## 证据边界与剩余风险

- receipt 是 Open-Flame 对本地数据库、受管媒体、当前 runtime 与适配器返回的观察。
  它不是平台签名回执、平台作品 ID、远端请求查询凭证、审核结果、定时任务执行证明、
  公开可见性证明或加密执行证明；本地管理员仍可改写代码和数据。
- 本轮真实平台调用数为 **0**。因此 Bilibili、抖音、视频号的账号 session、字段接受、
  媒体接收、平台草稿、投稿、审核、定时发布和公开可见性均未验证。
- 主视频在服务层调用适配器前会重新核对受管文件大小与 SHA-256，但适配器或其子进程
  随后按路径重新打开文件。两者之间仍存在 P2 TOCTOU 窗口，外部同账号进程可在该窗口
  改写最大 2 GiB 的 source；cover 已使用本次尝试的私有 staging 副本，不具有同一窗口。
  当前 receipt 的 source SHA-256 绑定服务复核时读到的字节，不能单独证明子进程最终读取
  了相同字节。关闭此风险需要稳定 Windows share-lock 句柄方案或为主视频建立受控 staging，
  应作为后续独立切片验收。
- `0592b6f` 的 release receipt 不覆盖本轮源码。只有最终 clean commit、五件制品、源码/
  wheel 独立安装和新的包外 receipt 绑定同一身份后，才能形成新的发行结论。
