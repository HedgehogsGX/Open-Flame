# 0.28.0 上传剩余空间阈值

日期：2026-09-14（Australia/Adelaide）。本切片基于 `a4e24b18c181e9a19e2601f80e3dd5e69ff137cc`。
所列本地结果针对该基线后的配置/容量增量；最终提交、CI 和发行 receipt 另行绑定。

## 问题与规则

上传媒体原先固定预留 64 MiB，无法按本机容量调整；HTTP 临时接收也缺少相同空间门槛。
当前默认值保持 64 MiB，增加以下部署配置并贯通普通启动、控制子进程、UploadService、
容量 API 与上传页显示：

- 普通 Windows Start / `video-download-local-app`：`--upload-storage-min-free-bytes`。
- 独立控制面：`VDC_UPLOAD_STORAGE_MIN_FREE_BYTES`。
- Python 配置：`Settings.upload_storage_min_free_bytes` 与
  `UploadService(..., storage_min_free_bytes=...)`。

接受 0 到 `2**63-1` 的整数，拒绝 bool、负值、浮点、非整数和越界值；验证先于目录创建。
API 的 `reserve_bytes` 和 `low_space` 使用服务实际配置，页面持续显示当前可用与预留空间。
配置属于部署环境；原启动命令继续提供时重启保持，恢复到新主机时采用新主机配置。
它不进入媒体库、预设或业务备份，也不因恢复旧数据覆盖当前阈值。
详细用法见[启动指南](../docs/WINDOWS_LAUNCHER.md#上传媒体预留空间)。

## 写入与失败边界

- 服务在 source/cover 导入、视频精确恢复前检查完整目标大小；视频逐块复制和三种路径登记前继续检查。
- HTTP 接收已知长度时先检查临时文件与受管副本的两份空间；流式接收按实际字节继续检查。
  三个入口共用接收写入/flush 逻辑，仍保留各自大小、类型、身份和错误合同。
- OS `ENOSPC`、`EDQUOT`、Windows disk-full 及 SQLite `SQLITE_FULL` 归为
  `upload_storage_full`；HTTP 返回 409。其他数据库错误仍为 503，不吞掉非容量异常。
- 失败仍关闭文件、删除本次未完成的临时/受管副本，保留来源和既有记录；没有自动清理用户内容。
- Download 阈值不变；同卷剩余空间来自即时观察，没有全卷原子预订、总配额或跨域删除责任。
  其他写入方可以在检查之后占用磁盘，实际耗尽仍须按失败路径处理。
  Upload Schema 4、备份格式 3 和四域数据库边界不变；整份备份的维护空间策略未改。

## 验证

现有聚焦回归：**313 passed in 44.34s**，范围为
`test_upload_lifecycle.py`、`test_upload_api.py`、`test_upload_service.py`、
`test_local_app.py`、`test_local_app_cli.py`、`test_config.py`、
`test_upload_ui.py`、`test_ui_design_system.py`。未新增或修改 tracked tests。

ignored 容量探针的五组检查全部通过：

| 组 | 观察 |
| --- | --- |
| 配置边界 | CLI/Settings/服务一致；跨进程 pickle 保留上传阈值；下载仍为原默认值 |
| 服务入口 | source/cover/restore 在边界不足时拒绝，足够时成功；原件摘要不变 |
| 复制中断 | 模拟另一写入方消耗空间、flush 满盘、发布 hard link 满盘和 SQLite 满盘；无错误登记和残留 stage |
| HTTP 入口 | known-length 双份空间门槛、无 Content-Length 流式中断、flush 满盘返回 409；incoming 清空，已存在媒体保持 |
| 重启与备份恢复 | 重新构造和独立格式 3 restore 保留媒体；相同启动配置继续生效，新主机可显式采用另一阈值 |

探针使用新隔离根、合成字节和受控故障；外网尝试 0。第一版探针误拦 Windows asyncio
用于自身唤醒的 loopback socketpair，HTTP 组未进入产品逻辑；保留该失败，修正探针允许
loopback IPC 后五组通过，共观察到 1 次该本地连接。没有放宽生产网络合同。

普通 Start 的空测试实例实际启动两次，均以同一 canonical app root 和端口 18876：

1. 512 MiB：页面显示 `写入后预留 512.0 MiB`；三主题 × 320 px / 200% 文字为 3/3
   无横向溢出，减少动态媒体偏好生效，标题焦点 outline 可见。
   56.566 秒轮询后标题、选区 `[7,8]` 和焦点保持；此阶段采样 console warn/error 为空。
2. 正常 Ctrl+C 停止后，以 1 TiB 重启：页面读取新阈值并显示低空间提示；观察时实际可用
   438758715392 bytes。对自有测试内容的 HTTP 导入返回 409 / `upload_storage_full`；
   source、managed bytes、orphan 和 incoming 均为 0，没有产生媒体或平台任务。

重启中另发现上传页的旧“状态刷新失败”提示在恢复成功后仍残留，尽管新容量数据已到达。
这是可复现的既有刷新状态缺陷，须在后续独立修复中清除已恢复的连接错误，同时保留业务操作反馈。
本记录只确认容量行为，不将该观察隐藏为整页无缺陷。

原基线 `a4e24b1` 的 [push](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34769446438)
和 [PR](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34769448248) 四格均已通过；
这些结果不覆盖本容量增量。固定负载持续运行、跨进程并发资源预订、总配额、真实媒体容量、
AI 与平台验收仍分别待办。本切片没有真实下载、登录、AI、上传或发布。
