# Upload 生命周期所有者与启动回收

日期：2026-09-12；基线 `4c0c119`；分支 `codex/architecture-reset-ci`。

## 问题与最终结构

Upload 生命周期原来由 HTTP 文件中的 `_LazyUploads` 创建并持有。应用退出和 Workflow
也需要同一个实例，但依赖路由安装的副作用才能取得它。现在公开 `UploadManager` 只接收
root 和 service factory，由根应用统一组装，再交给 HTTP、Workflow 和 lifespan。
删除了原私有类和默认 factory；没有保留私有转发壳，也没有新增通用 Manager 基类。
应用 state 的 factory 注入点继续有效；独立导入 manager 不加载 FastAPI、API 或
UploadService，构造 manager 与读取页面/session 也不创建上传根或 worker。

原启动失败路径保留未启动的 thread，stop 会 join 它并遮盖最初的异常。现在在未启动时
清空 thread、设置停止信号并回收执行锁和 activity lease；已经实际启动的 thread 继续
拥有锁，直到退出，不能在它运行期间释放。manager 清理失败时保留原异常与 candidate，
后续 stop/recover 仍操作同一实例。

recover 的检查、启动与状态读取现在都在 owner 锁内，避免并发 stop 返回后又启动线程。
原 get 的 lazy single-owner、永久关闭、standby、人工 recover 与错误映射继续保留。

底层 `_SchedulerLock.acquire` 也补齐文件对象到 owner 的交接保护：在实际系统锁取得后
注入非 OSError 异常，原来会把尚未登记的 handle 留在 traceback 中，stop 无法释放。
现在复用现有 `close_binary_on_error`；即使调用者保留 traceback，第二个 owner 也能立即
重新取得锁。OSError 继续返回未取得锁并进入 standby；不把清理异常替换为新的启动错误。

## 当前验证

- 16 项生命周期矩阵全部通过：启动前 OSError/RuntimeError/KeyboardInterrupt、失败后
  再次访问、实际启动后中断、清理异常、并发首次访问、恢复、重复关闭和关闭后拒绝访问。
- recover/stop 竞争探针通过：恢复暂停期间 stop 不能提前返回；两者结束后无运行线程且
  锁均释放。
- 实际 create_app/TestClient 验证 lazy 路由、factory 注入、HTTP/Workflow 共用实例、
  status 重用及 lifespan 清理通过；新的进程独立导入 manager 未加载 HTTP/UploadService。
- 三个真实 Windows 锁反馈：修改前 KeyboardInterrupt 与 RuntimeError 两例占锁到
  traceback 回收；修改后全部通过立即释放检查，OSError 对照不变。异常发生点由注入
  提供，未证明普通 UI 的 Ctrl+C 会在后台线程触发该窗口，也不声称覆盖任意指令间中断。
- 最终修改后原样运行 Upload service、activity lock、lifecycle 三个既有测试文件：
  **44 passed、13 failed，56.90 秒**。13 个失败 ID 均与当前完整基线相同，没有新增或
  消失的选定失败；没有修改 tracked tests。

探针、JUnit 和对照位于 ignored
`validation/local/architecture-reset-20260912/upload_owner/`。
此前 owner 工作区的完整离线测试为 2194 passed、248 failed、16 skipped，380.02 秒；
它在最后这次底层锁修复之前执行，不能替代最终源码的全量测试。

没有改变 Schema、备份格式、账号/session、receipt、确认、unknown reconciliation、
runtime 或真实上传触发语义。没有调用真实登录、平台或 OpenAI。完整 CI、公共备份
文件职责、Editing 登记前资源回收与最终发行验收仍待完成。
