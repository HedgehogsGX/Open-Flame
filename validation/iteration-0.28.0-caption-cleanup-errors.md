# 字幕消费清理保留原始失败

日期：2026-09-12；分支 `codex/architecture-reset-ci`；起点 `f4e68e2`。

`DownloadAssetReader.read_caption` 的 finally 原先直接关闭已取得的 spool。
当读取与关闭同时失败时，关闭错误会替换预期的字幕不可用错误；KeyboardInterrupt
和非预期 RuntimeError 也会被替换。问题位于消费端，独立于下层素材读取与 Workflow 映射。

现在取得 handle 后使用既有 `close_binary_on_error` 保护读取。失败时关闭并保留正在传播
的主异常；正常读取后的 close 位于错误映射之外，关闭自身失败仍然可见。没有引入新的
资源抽象，也没有改变成功 payload、读取上限或领域错误映射。

验证：

- 实际生产消费方法的六项最小对照从 3 passed / 3 failed 变为全部通过，覆盖双故障、
  单独读错、单独关错、正常成功、KeyboardInterrupt 和 RuntimeError；所有句柄均已关闭。
- 原四个 resolver 与当前 Download reader/API 映射的 45 组差分仍无差异。
- 仅在 ignored 副本迁移旧工厂位置、保持原响应断言的六项回归通过，2.38 秒。
  这不表示 tracked 响应测试或全套 CI 已通过；两处旧 `api.tempfile` 引用仍需正式迁移。
- whitespace 与 staged scope gate 通过；没有修改 tracked tests。

临时脚本、前后 JSON 和响应 JUnit 位于 ignored
`validation/local/architecture-reset-20260912/`。本轮未使用真实数据库、模型、下载或平台。
完整架构与全部 CI 修复目标仍未完成。
