# 0.28.0 当前应用数据的保护副本与独立恢复

日期：2026-09-14（Australia/Adelaide）。备份与首次恢复基于 clean detached
`d970bce18f23d205476b3ca86be4cb2465947dcb`。原应用根未启动或升级；源文件和私有状态保留。

## 观察到的数据与保护范围

本轮重新规范化实际 app root，并查询数据库结构、记录数量与状态。
进程盘点未发现其他匹配的 Open-Flame Python 进程；盘点脚本自己的两个解释器进程单独排除。
首次宽泛进程匹配因包含脚本自身而停止，未开始复制；随后修正识别条件。

| 领域 | 实际根内容 | 本次结果 |
| --- | --- | --- |
| Download | Schema 11；0 个 job、attempt、asset、credential profile | 数据库一致性保护副本、独立 backup/restore、全部表记录比对通过 |
| Upload | Schema 3；1 个账号、2 个来源、2 个 canceled job、2 个 ready operation、1 个 request | 原 Schema 3 副本保留；另一副本迁移至 4，格式 3 backup/restore 通过 |
| Editing | 该实际根中不存在对应目录 | 记录缺席；没有历史内容可用于本轮恢复验收 |
| Workflow / presets | 该实际根中不存在对应目录 | 记录缺席；没有历史内容可用于本轮恢复验收 |

保护期间同时持 Upload activity exclusive lease、既存 worker 锁及 Download 数据库
`BEGIN IMMEDIATE` 写保留。使用 SQLite backup API 取得两份静态数据库，再稳定复制上传
media/assets；不是复制运行中的单个 SQLite 主文件。前后数据库记录和原主文件 SHA-256 一致，
上传 worker 锁字节保持不变。没有执行原根的 Schema ensure、service.start 或 manager.get。

受管上传文件为 **2 份、112,222 bytes**；没有受管封面。账号秘密、两个 runtime、incoming
和原日志均不复制。该副本覆盖本次观察到的业务数据，不是凭据/安装环境/整台机器的备份。

## 两个独立备份合同

在保护副本上分别调用现有 Download 与 Upload 备份入口，没有新增组合备份格式或 CLI。

| 备份 | 文件数（含 metadata） | 总字节 | manifest SHA-256 |
| --- | ---: | ---: | --- |
| Download Schema 11 | 2 | 348,607 | `835557079088e07dfbab11b332936be3e622266510c05935ae48704cbe373f46` |
| Upload Schema 4 / format 3 | 4 | 220,134 | `f0502fe9c78b2c42e2e9541bd9c1dce413d08253b0860f030a3007c49b975a49` |

Upload 只在独立副本执行 Schema 3→4；原记录完整保留，`upload_attempts` 为空。
restore 再发布到全新的恢复 app root，账号按合同变为 `unchecked / account_missing`，
没有恢复秘密、复用确认或补造 receipt。两份媒体的登记和实际摘要一致。
所有离线备份/迁移/恢复步骤的网络尝试为 0。

## 恢复副本普通启动与修复反馈

使用已有项目解释器、已验证工具目录和普通 Python bootstrap，在独立端口启动恢复副本。
这不是重新 Setup 或发行包独立安装。四个页面 HTTP 200；这里只做 HTTP 检查，未补做浏览器渲染 QA。

实际 GET 核对 2 个来源及摘要、2 个 canceled job、1 个 unchecked 账号和 2 个历史 ready operation。
没有登录、账号 check、确认或投稿请求；Upload runtime 与 AI runtime 分别保持
`runtime_missing`、`ai_runtime_missing`。Editing/Workflow 在恢复副本按需创建为空，不能把这种
新库创建记为旧内容恢复。Ctrl+C 后 3 个自有进程退出、端口释放，`local_app.stopped` 为 normal；
PTY 实际退出码为 1，未使用强制终止。

首次最终检查发现一次 `runtime_log.event_rejected / invalid_record`，原最终报告保留为 FAIL。
数据与正常停机检查分别 PASS。原因是 Workflow 预设路由未登记到日志白名单，见
[日志修复](iteration-0.28.0-runtime-route-logging.md)。修复后的第二次普通启动按独立 run_id
验证，相关真实 HTTP 日志记录正常、拒写事件为 0；再次正常停机后四库 quick_check=ok，
外键错误为 0，Schema 为 11/4/4/3，upload attempt 和活动 operation 为 0，incoming 为空。
原应用根的两个数据库摘要仍未改变，原本缺席的两个领域目录仍未创建。

原始报告、数据库、媒体和映射保留在 ignored 本地验收区。该数据集没有 Download 媒体或
Editing/Workflow 历史内容，因此不能证明这些内容的真实容量恢复。
凭据恢复、旧源码/runtime 配套回退、异机/offsite、实际根维护与真实业务仍各自待办。
