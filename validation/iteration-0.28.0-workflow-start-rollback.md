# Workflow 首次线程启动回滚

日期：2026-09-12。基线为 `d48132637ce94a8b0b41bc2a965d24e27ac62aff`。
本切片在 `codex/architecture-reset-ci` 开发，不直接推送 main。

`WorkflowManager.get()` 原先在 `Thread.start()` 前发布 service、thread 和 active 状态，
启动失败后仍会把没有 worker 的 service 返回给下一调用者，stop 又会 join 未启动线程。
现在首次构建或启动失败会在同一 owner 锁内回滚这些状态；下一次显式 get 可以重新初始化。
若中断发生在真实线程已经启动后，则保留已有 owner，避免建立第二个 worker。

四个有界、无应用数据库的故障注入均通过：get 启动失败后重新启动；invoke 失败释放 operation
计数；Thread 构造失败后 stop 安全；真实线程启动后注入中断仍保持单 owner。lazy 初始化、
stop 后永久拒绝、已运行 worker 的扫描恢复和未知远端结果不重放的边界保持。

临时探针位于 ignored `validation/local/architecture-reset-20260912/failure_paths.py`。
最初两个启动场景复现未回滚且 stop 抛错；修复后的对应四个检查通过。该探针同一脚本中的
媒体清理检查属于独立切片，不能借给本次线程修复。语法编译和 `git diff --check` 通过。

本轮起点完整现有回归为 **229 failed、2213 passed、16 skipped，447.85 秒**，并不是本切片
完成后的 CI 结果。当前 main 的 Actions run `34616910953` 四格在提交范围门禁失败；后续
CI 恢复仍在进行。没有修改 tracked tests，没有真实网络下载、登录、模型或上传动作。
