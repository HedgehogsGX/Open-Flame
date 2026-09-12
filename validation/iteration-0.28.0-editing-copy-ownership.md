# Editing 复制目标所有权与失败清理

日期：2026-09-12；修改前基线 `8341c38`；分支 `codex/architecture-reset-ci`。

## 问题与改动

`EditingService._copy_and_hash` 原来在独占创建失败后仍按目标路径清理，因而可能删除
既有文件；目标被替换后再进入校验失败，也会误删替换文件。隔离真实 `import_source`
强制 UUID 冲突时，Source 行仍在，但原媒体已被删除。

现在只有独占创建成功、取得目标 handle 的身份且失败清理时仍为同一普通单链接文件，
才尝试删除。创建失败、身份未知或目标已被替换时保留文件。复制返回前也核对目标身份，
不能把相同大小的替换文件当成本次产物。

相同的清理规则由既有 `managed_files.discard_created_file` 公开拥有，Download/Upload
备份也使用它；删除原 backup 私有副本，没有从 Editing 反向导入备份域。各调用者仍拥有
创建、容量、hash、来源变化检查、错误映射和事务政策；公共函数只处理失败后的文件身份。
身份无法取得时可能保留部分产物，不能为了清理彻底而删除无法证明归本次操作所有的路径。

Editing 源/目标 handle 同时复用 `close_binary_on_error`，读写主异常和中断不再被关闭
错误替换；正常完成后的关闭失败继续传播。没有改变 Schema、request/replay、并发来源
导入收敛、render claim/CAS、来源和输出格式或远端执行授权。

## 验证

- 初始六项所有权反馈中四项失败：既有目标、源目标同一路径、替换目标和真实导入的强制
  UUID 冲突；正常复制及本次部分产物清理通过。修复后七项通过，新增一项验证同大小的
  替换目标不能被当成成功复制结果。
- 八项实际 Editing 复制 handle 关闭矩阵：冻结基线六项主异常保留失败，当前全部通过。
  覆盖读取/写入的 EditingError、OSError、KeyboardInterrupt，以及正常关闭自身失败；
  每个合成 handle 仅尝试关闭一次，失败时本次产物仍被清理。
- 实际 `complete_plan` 的第二次成品登记强制碰撞第一个 asset ID：保留首个 ready asset
  的记录与字节，第二个 plan 保持 running 且无已登记资产；取消故障注入后，同一 claim
  可正常完成。使用合成渲染结果，没有调用 FFmpeg 或真实平台。
- 共享清理迁移后的备份十五项所有权和十三项关闭矩阵均通过。
- 原样运行五个 Editing 文件和三个备份测试文件：**227 passed、11 failed、2 skipped，
  60.99 秒**。11 个失败 ID 均存在于 clean `8341c38` 全量基线：三个旧 Editing Schema 1
  断言及八个既有 Upload backup Schema/CLI/篡改断言。两个跳过是本 checkout 没有安装
  pinned Windows FFmpeg；不是完成真实 FFmpeg smoke 的证据。

探针、JUnit、基线差分和结果位于 ignored
`validation/local/architecture-reset-20260912/editing_copy/`；备份所有权复验结果另保存在
同级 `backup_io/`。未新增或修改 tracked tests。

正常业务路径使用新 UUID；强制碰撞只证明失败保护，不证明发生过自然碰撞或实际用户损失。
按路径 unlink 前的身份检查也不等于抵御同权限恶意进程的原子删除保证。
完整 CI、Upload 启动回滚、公共备份 Module 和最终发行验收仍未完成；`8341c38` 的
2194 passed、248 failed、16 skipped 仅证明修改前完整基线。
