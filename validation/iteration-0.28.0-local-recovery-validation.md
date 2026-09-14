# 0.28.0 跨进程恢复、Upload 重建与 Chromium 回收

日期：2026-09-14（Australia/Adelaide）。以下场景均固定生产源码
`0dfcf82b7896e2a61b1201f3f974a5f7adf3dcdd`，在独立本地根执行。
本记录汇总已完成的分项证据；它们没有随本次测试维护再次运行。

## SQLite 故障与执行权交接

使用真实独立 SQLite 写锁和 Windows worker 锁，上传后端为 FakeBackend，媒体为合成字节。

- 独立进程持有 BEGIN IMMEDIATE 0.505 秒后释放，两个已确认 job 各执行一次，均有 responded receipt。
- 持锁 12.378 秒耗尽真实写入/恢复预算后，scheduler 为 faulted/scheduler_database_unavailable，worker 停止领取；数据库不能写入时原 job 仍为 running/queued。
- 释放锁并正常停止，再由新 Python 进程恢复，分别得到 unknown/interrupted_result_unknown 和 draft/restart_confirmation_required；再确认前调用数为 0。
- unknown 的直接重试被拒绝；仅在模拟 not_accepted 核对后产生唯一 retry 后继，并要求新确认。
- 跨进程 standby 实例可取消 queued/running 项；前者 canceled 且无 attempt，后者 unknown 且 receipt 保持未知。
- owner 正常停止后，standby 显式 start 取得执行权；旧终态不重放，新确认任务只执行一次。

三个隔离数据库 quick_check 均为 ok、外键错误 0，输入摘要保持；相关进程和 Upload 线程结束。
父/子探针网络尝试均为 0。这不是完整应用 HTTP/UI、真实视频或平台验收。

## 恢复解释器上的正式 Upload 重建

使用恢复出的 CPython 3.12.13 x64，在全新根调用未修改的正式 runtime_setup：
安装 ready/exit 0（157.527 秒），独立 --check ready/exit 0（18.620 秒）。
两次均执行正式三平台 CLI/biliup help 与本地浏览器检查。

新 venv/manifest 和独立进程实际加载的 python312.dll、_ssl.pyd、_sqlite3.pyd
均指向恢复副本；2,771 个运行时非缓存文件、2,766 个外部 Python 非缓存文件通过核对。
旧 runtime manifest 保持，新根没有业务数据库或凭据，构建/check/helper 结束。

安装器按锁访问 PyPI 和浏览器分发源；这不是纯离线安装。原系统 Python 未删除或禁用，
旧 venv 的绝对路径绑定未改写；不证明可直接搬移环境、异机/offsite、真实认证或原根升级。

## 真实 Chromium 的故障回收

在重建的 runtime 上使用 Chromium 145.0.7632.6 和未修改的 SauBackend 执行器、
Windows Kill-on-close Job Object；仅把桥接命令换为本地页面探针，没有调用平台代码或 UploadService。

| 场景 | 结果 | 观察进程 / 持有查询句柄 |
| --- | --- | ---: |
| 正常预热 | submitted / upstream_submitted（模拟） | 13 / 13 |
| 15 秒超时 | unknown / upload_timeout_unknown | 12 / 12 |
| 浏览器就绪后取消 | unknown / upload_cancelled_unknown | 12 / 12 |
| 故障后正常执行 | submitted / upstream_submitted（模拟） | 13 / 13 |

4/4 通过；每个场景先观察 8–9 个活跃 Chromium，保留全部已枚举进程的查询句柄，
任意 OpenProcess 失败即使验证失败。50 次句柄观察均确认对应进程退出，临时浏览器视图已删除。
相对预热后父进程，结束时采样最大增长为句柄 0、线程 0、私有内存 90,112 bytes；
预先固定上限为 8 个句柄、2 个线程和 64 MiB，不代表浏览器总峰值或长期无泄漏。

仅拒绝的本机代理记录 36 次背景请求拒绝，未转发；页面请求为 0。运行时完整性前后通过，
manifest 不变，private 为空，最后进程盘点匹配数为 0。
首次探针允许跳过无法取得句柄的 PID；保留原记录后改为严格计数，在新目录复验，未覆盖旧结果。

## 证据身份与未完成范围

原始文件只保留在 ignored 本地验收目录，核心 manifest 的 SHA-256 为：

| 范围 | SHA-256 |
| --- | --- |
| 跨进程存储/执行权 | d48707938af86d5f34fe90c83a6579e60a05c24d1565f301b8dfcfbaed5edaf2 |
| 恢复 Python 正式重建 | 2305be26e874f7db7be6cdd3bef0271ba991944f2de26b932a9c8a73de9b207f |
| Chromium 严格句柄复验 | dc6d796c5a18fd13f66729d5f221dc2bd913503a8201aeb6ea6e6a1c2624f147 |

以上不覆盖真实 URL、AI/provider、UploadService/receipt/平台整链、整卷满盘、长期运行、
真实秘密恢复或实际根维护。此前[固定存储负载](iteration-0.28.0-storage-soak-and-ci-follow-up.md)
仍只按其原场景引用；最终候选、main 门禁及正式发行仍需各自验收。
