# 备份文件与描述符所有权修复

日期：2026-09-12；分支 `codex/architecture-reset-ci`；修改前基线 `02e7228`。

## 问题与修复

`backup._copy_regular_file` 原来在独占创建目标失败后仍无条件 unlink 目标。隔离探针确认，
Download 入口及 Upload 转发入口都会删除已经存在的目标；传入同一个源/目标路径时也会
删除源文件。目标由其他文件替换后，hash 失败清理同样可能删除替换文件。

现在只有独占创建成功、取得本次目标 handle 的文件身份，并在清理时仍读到同一普通单链接
文件，才尝试删除。创建失败、无法取得身份或已经换成别的文件时保留目标。复制成功仍返回
实际 SHA-256 与大小；本次拥有的部分产物在验证失败时继续清理。

复制、hash 和 bounded-read 三个入口还存在相同的 raw descriptor → `fdopen` 交接空隙。
它们现在共用 `_owned_binary_reader`：包装失败时回收原始 descriptor；成功交接后只关闭
文件对象。源/目标 handle 的失败清理复用既有 `close_binary_on_error`，保留触发失败的原始
错误。正常完成后关闭本身失败仍向上传播，不将其伪装成成功。

没有改动备份格式、Schema、Download 事务、Upload activity/worker 锁、DB/WAL 快照、
源 SHM 行为、域内恢复政策或发布目录结构。公共文件模块尚未抽取；先在既有文件中关闭
已经复现的错误，后续迁移再单独验证。

## 当前证据

| 检查 | 修改前 | 修改后 |
| --- | --- | --- |
| 15 项文件所有权矩阵 | 13 failed；两个正常行为对照通过 | 15 passed |
| 13 项关闭失败矩阵 | 8 failed：读取/写入原始错误或中断被 close 错误覆盖 | 13 passed |
| 现有备份回归 | 使用 `02e7228` 全量结果作失败 ID 对照 | 156 passed、8 failed，49.68 秒；8 个失败 ID 均与基线相同 |

文件矩阵包含 Download/Upload 已有目标、源目标同一路径、目标替换、正常复制、错误 hash
清理，以及三个读取入口面对 OSError、RuntimeError、KeyboardInterrupt 的 descriptor 回收。
关闭矩阵覆盖源与目标 handle 的读写主错误、KeyboardInterrupt、正常关闭失败和 unlink
失败；每个合成 handle 都只尝试关闭一次。探针回收了基线故障注入产生的 descriptor。

现有回归原样运行 `tests/test_backup_restore.py`、`tests/test_upload_backup_restore.py`、
`tests/test_upload_backup_cli.py`。失败包括旧 Schema 3 期望及旧版本的 schema 篡改/CLI
断言；失败 ID 相同不等于全部根因已经解决。没有新增或修改 tracked tests。
探针、JUnit、日志与差分保存在 ignored `validation/local/architecture-reset-20260912/backup_io/`。

当前调用主要使用受控 staging。本记录证明的是隔离文件与注入错误下的本地行为，没有
真实业务数据损失证据，也不把 lstat 后的按路径删除称为抵御同权限恶意进程的原子操作。
完整 CI、公共备份所有权迁移与最终发布候选验收继续推进；最近 `02e7228` 的全量
2194 passed、248 failed、16 skipped 不覆盖此后改动，不能据此声明当前 CI 已通过。
