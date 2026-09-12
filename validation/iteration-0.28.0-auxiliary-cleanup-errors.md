# 辅助素材清理保留原始失败

日期：2026-09-12；分支 `codex/architecture-reset-ci`；切片起点 `26631fe`。

辅助素材的摘要校验、响应构造、流读取或 ASGI 发送先失败时，原先在裸 except/finally
中关闭 spool/handle。如果 close 同时失败，清理错误会替换原始错误。四条真实调用路径的
有界故障注入均能在改动前复现该问题，正常句柄转移对照通过。

现在四个边界复用既有 `managed_files.close_binary_on_error`。失败清理保留正在传播的
主异常；成功构造继续转移打开的句柄，正常流结束仍关闭。正常关闭自身失败没有被静默吞掉。
未新增清理框架、响应协议或新的业务状态。

本切片验证：

- 同一原始探针由四条失败转为全部五项通过。
- 三项补充边界通过：正常流的 close 错误继续传播；generator shutdown 完成清理；
  ASGI cancellation 保持原始 CancelledError 并关闭句柄。
- 既有 artifact resilience 的六项响应、manifest、发送失败与取消检查全部通过，
  1.95 秒；三项依赖完整 HTTP fixture 的用例未选入该精确集合。
- API 语法编译和 whitespace 检查通过。

临时脚本、JSON 和 JUnit 位于 ignored `validation/local/architecture-reset-20260912/`。
本切片没有修改 tracked tests，没有真实下载、模型、登录或平台操作。这是清理故障修复，
不是整体架构或完整 CI 通过结论；Download 素材读取的职责迁移仍待实施。
