# 备份 descriptor 与 SQLite 连接交接

日期：2026-09-12；基线 `dc79bdd`；分支 `codex/architecture-reset-ci`。

Upload 备份取得 worker-lock raw descriptor 后，文件对象包装失败时尚未进入锁的 finally，
因而遗留 descriptor。现在复用公开 `managed_files.fdopen_owned_binary`：包装失败回收
raw descriptor，包装成功由文件对象承担关闭。Download 的三个备份读取入口也改用这个
已有两个真实消费者的交接函数，删除原私有 reader 内的重复包装/失败关闭代码。
`rb` 与 `r+b`、祖先路径检查、锁字节和各域错误映射均不变。

SQLite online backup 原来在打开两个连接后才进入 finally。目标连接失败时源连接没有
关闭；目标关闭失败还会跳过源关闭并覆盖更早错误。现在每个连接成功打开后立即进入
自己的关闭范围，两者嵌套使用。正常关闭错误仍向上传播；已有工作错误或中断时，两边
均尝试关闭一次，关闭错误不能替换主异常。没有把 SQLite 事务 context manager 误当作
connection.close 的替代。

## 验证

- 新的 11 项资源交接反馈：修改前 9 项失败，修改后全部通过。覆盖 Upload fdopen 的
  OSError/RuntimeError/KeyboardInterrupt，真实不存在目标父目录导致的 SQLite 连接失败，
  body 主异常及关闭失败、正常关闭失败和成功快照。
- 原有 15 项复制/descriptor 所有权与 13 项关闭反馈通过，保持已存在目标、替换目标、
  同路径目标、主异常与正常关闭错误的行为。
- 三个既有备份测试文件原样运行：**156 passed、8 failed，53.23 秒**。8 个失败 ID 均在
  当前完整基线中，没有新增或消失的选定失败；其 Schema/CLI/篡改断言尚待完整 CI 维护。
- Python 编译、source preflight、diff 和提交范围检查通过。未新增或修改 tracked tests。

反馈和 JUnit 保存在 ignored
`validation/local/architecture-reset-20260912/backup_resources/`。
这些验证使用临时根、真实本地 descriptor/SQLite 连接与注入异常。fdopen 失败发生于
取得 worker 原生锁之前，不能解释为原生锁永久被占用；没有真实数据损失或平台调用。

本切片尚未移动公共备份文件 Module。Download 的 `BEGIN IMMEDIATE`、pending intents、
graph 审计，Upload 的双锁、稳定 DB/WAL 字节快照、源 SHM 只读、格式迁移和 receipt
恢复语义均保持不变，后续迁移继续以这些公共行为验收。完整 CI 和最终发行仍未完成。
