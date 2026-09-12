# Upload 执行与备份共用回执状态合同

日期：2026-09-12；独立开发分支 `codex/architecture-reset-ci`。
本切片起点为 `5e4a976de5a56eed39b521b6007a8bd3025948a4`。

此前 UploadService 与备份审计各自实现 reserved、dispatch、responded、unknown 和
reconciled 的完整状态分派。现在 `uploads/receipts.py` 拥有纯状态校验，执行和备份
实际复用它；公共 code grammar 位于 Upload contracts。service 原内部调用名保留为
同一函数的 staticmethod 引用，没有第二套状态实现。

备份仍先验证数据库关系、request/root/完整谱系、账号 session、媒体摘要、build 与
历史 adapter 身份，再做自己的 metadata/timestamp 错误分类，最后使用共享状态合同。
备份错误继续为固定的 `upload attempt state is invalid`；service 保持原领域错误码。
事务、恢复政策、旧备份迁移、逐项确认和 unknown 不直接重试均保持，没有 Schema 变化。

## 本次验证

- 共享函数与父提交的 service/backup 状态片段作 1,764 组差分：0 个接受/拒绝差异，
  其中 364 组被接受。固定有效 build/adapter identity；不把它当作所有 DB 关系的证明。
- 五个完整离线生产路径通过：三平台 publish、视频号 draft、Bilibili unknown 经人工
  not_accepted 模拟核对。每个路径通过真实 UploadService 建草稿/确认/领取/落 receipt，
  再经真实 backup/restore，回执逐字段不变；伪造 revision 均被备份拒绝。backend 是
  确定性假实现，每例只执行一次 fake dispatch，恢复后零 dispatch，真实平台调用为 0。
- 既有 `test_upload_service.py` 与 `test_upload_backup_restore.py`：改动前
  157 passed、19 failed（88.64 秒）；改动后 157 passed、19 failed（89.91 秒）。
  176 个用例的结果分类逐一相同。现有失败仍需处理，不能声称完整 CI 已恢复。
- 纯 receipt 模块可在不导入 service/backend/HTTP 的情况下加载；语法编译和 whitespace
  检查通过。显式发行清单已包含新模块。

脚本、差分及 JUnit 仅保存在 ignored `validation/local/architecture-reset-20260912/`。
没有新增或修改 tracked tests，没有接触真实应用数据、账号、模型或平台。
