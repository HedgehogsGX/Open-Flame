# Iteration 0.13.0 — capability governance 与构建身份工程证据

> 日期：2026-09-04\
> 项目版本：`0.13.0`；数据库：Schema `10`\
> 范围：Windows 本机、synthetic/offline 数据、隔离 API/UI 与 project-only Python package\
> 非证据：真实平台 Stage 0、真实 Cookie、Linux/Docker、第三方 binary/container/tool-bundle 再分发

## 1. 本轮结论

本轮把“代码内存在下载路由”“某一精确构建/下载器/环境的 Stage 0 聚合证据”和“人工批准或撤销决定”拆成三个独立层次。Schema 10 保存不可变 evidence、append-only decision chain、revision CAS 和只读 current view；Schema 9 的历史能力行只读封存，不自动转换为批准。

Stage 0 CSV v3 的 `product_version` 必须是 `0.13.0+build.sha256.<64 hex>`。摘要覆盖 importable package 内按相对路径排序并带长度 framing 的源码、数据和 sourceless bytecode；PEP 3147 `__pycache__/*.pyc` 作为可再生成缓存排除，但该目录中的其他文件仍计入摘要。包根或内部出现 symlink/reparse/special file、扫描期间文件清单或 metadata 变化、运行期间磁盘载荷与进程启动基线不一致时均 fail closed。导入和批准在事务提交前再次核对构建身份；旧构建 evidence 可查询或撤销，但不能批准为当前构建。

这仍不是密码学执行证明。具有本机管理员权限并能同时伪造 CSV、程序、数据库和摘要的主体仍能伪造结论；部署还必须依赖只读安装、受控更新和独立人工复核。

## 2. Stage 0 与数据完整性边界

- Manifest/results 必须使用精确、不重复的 CSV v3 表头和行宽；短行、额外列、重复列、旧字段、无时区时间、不安全 identity token 或 manifest/result `job_kind` 不一致均被拒绝。
- 来源按 `job_kind + platform/source_type/source_id` 去重。X、Douyin、TikTok 等纯数字来源 ID 去除前导零；TikTok 同一 video ID 的不同 handle、host 或 query 不是独立样本。
- 同一 Bilibili 投稿可同时有 BV 与 av 标识；当前不维护本地可信 BV↔av 转换，因此 Stage 0 manifest 只接受 BV。普通下载入口仍接受 BV/av。
- 每一精确 evidence identity 只使用最新三次相关执行；较新的 partial run 不会被旧完整运行掩盖。
- 公开报告为 aggregate-only，不包含 URL、URL hash、source-identity hash、sample ID、run ID、本机路径或运行数据库。
- 导入只追加 evidence，绝不自动批准或开启 Worker、联网、短链、graph gate。批准/撤销使用独立本地 CLI、允许列表 reason code 与 expected revision。

Schema 10 的 evidence、decision 和 Schema 9 archive 都拒绝 UPDATE、DELETE 与 `INSERT OR REPLACE`。Readiness 检查 migration、表/view、列、索引、trigger、FK、evidence digest/identity/policy/static route、decision root/supersession/revision/current projection 等语义。HTTP 健康探针将完整审计最多缓存 5 秒，并通过 SQLite schema cookie 立即失效 DDL；若审计期间 schema 变化或最终 cookie 不可读则返回非 ready。管理员、备份和恢复路径继续调用不缓存的完整 readiness。

## 3. CLI 与只读 API/UI

- `video-download-validation --print-product-identity` 输出版本和完整 build identity；所有参数、CSV、I/O、构建漂移与内部失败均在进程边界转换为固定 JSON 错误，不回显原异常或路径。
- `video-download-capabilities` 只接受已经存在且 ready 的 Schema 10 数据库；不会创建或迁移数据库。数据库路径必须是绝对、规范、单 hard-link 的普通文件，并在每次连接前后复核文件身份。
- 只读 API 分为 implementation、evidence、current decision/history；`capability-snapshot` 在一次 readiness 后用一个 SQLite 读事务返回 UI 所需数据、总数/截断标记、当前 build identity，并补齐 current decision 引用的 evidence。
- 前端首次载入或点击“刷新能力”时读取 snapshot，不把能力视图加入 10 秒运维轮询；决定按 `identity_key + evidence_id` 精确关联，并区分当前构建、历史构建和分页窗口外未知状态。网页不提供治理写按钮。

## 4. 自动化与静态检查

| 检查 | 结果 |
|---|---|
| 全套 pytest | `1072 passed, 8 skipped in 81.80s` |
| 跳过边界 | 1 个 POSIX Cookie directory-FD cleanup；4 个 root POSIX/getfacl metadata contract；1 个 Windows 不可用 AF_UNIX roundtrip；1 个 POSIX open-file replacement；1 个 POSIX permission test |
| Python 编译 | `python -m compileall -q src tests` 通过 |
| 依赖锁 | `uv lock --check --offline` 通过 |
| 关键静态错误规则 | `ruff check --select E9,F63,F7,F82 src tests` 通过；仓库未把完整默认 Ruff 规则集配置为发布门禁 |
| diff 结构 | `git diff --check` 通过 |
| 参数型命令入口 | 11 个 `python -m ... --help` 均以 0 退出；控制面入口由独立 loopback 服务启动验收覆盖 |

8 个 skip 都是目标操作系统/权限边界，不计为通过；必须在目标 Linux/root acceptance 中重跑。

## 5. 隔离浏览器、API、日志与成品验收

使用独立 loopback 端口和 gitignored data root 启动最终源码，不配置真实工具根，不开启短链或 graph gate，不访问媒体平台。页面显示 v0.13.0 / Schema 10、7 个静态实现、0 evidence、0 decision；snapshot 的 build identity 与同一 checkout 的 CLI 输出完全一致。

通过网页提交 YouTube、Bilibili 与 TikTok 三条 synthetic 直链，随后运行显式 gate 的 offline fake Worker：

| 检查 | 结果 |
|---|---|
| Batch/Worker | `3/3 ready`；三个 route 各被领取一次；不是实际媒体下载 |
| 原件下载 | 3 个端点均为 HTTP 200；响应字节数、`Content-Length` 与 SHA-256 全部匹配数据库 DTO |
| 下载响应 | 固定 opaque 文件名；`Cache-Control` 同时含 `private`/`no-store`；`X-Content-Type-Options: nosniff` |
| 能力 snapshot | 7 implementation、0 evidence、0 decision；无截断；build identity 与源码 CLI 相同 |
| 健康 | `/health=ok`、`/health/ready=200`、Schema 10；`/docs=404`，机器契约由本地 `/openapi.json` 提供 |
| 运行日志 | control 与 offline-worker 事件可见；`status=ok`、`write_failures=0`、`rejected_events=0` |
| 浏览器 | 默认与 390×844 窄屏均可读；手动能力/日志刷新恢复；console warning/error 为 0 |

原始 QA 数据库、运行 ID、UUID、日志和 synthetic URL 只保留在 gitignored 本机验收目录，不进入本文件或发布包。

## 6. Apache-2.0、发布包与隐私复核

本记录写入后从精确源码树离线重建 sdist/wheel，并在全新 Python 3.12.13 环境仅从本机缓存安装：

| 检查 | 结果 |
|---|---|
| metadata/import | `0.13.0` / `Apache-2.0` |
| 项目命令入口 | 12 个，包括新增 `video-download-capabilities` |
| 法律文件 | 32 个：项目 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 与 29 份受审计第三方法律文件 |
| 包载荷 | wheel 内 56 个 `video_download_control` 文件与 checkout 逐字节一致；无 data/log/runtime-tools/validation-local 路径 |
| 构建身份 | checkout 与隔离安装 wheel 的 `product_identity` 完全相同 |
| 隔离安装 | 14 个项目/runtime distributions 安装成功，`uv pip check` 通过 |
| 隐私标记 | 201 个 source-tree publishable 文件，以及含生成 `PKG-INFO` 的 202 个 sdist 文件条目和 92 个 wheel 文件条目，其已知本机用户名/路径、历史真实样本 ID/账号及高置信秘密标记扫描均为 0 hit |

制品 SHA-256 保存在 gitignored 的包外校验文件，避免把 sdist 自身 hash 写回 sdist 形成自引用。项目自有源码、文档与脚本依据 Apache-2.0 分发，版权声明保持 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。这不重新许可任何媒体或第三方组件；dependency wheelhouse、冻结可执行文件、OCI/container image 与 yt-dlp/FFmpeg tool bundle 仍处于 `blocked_pending_third_party_source_and_notice_audit`。

## 7. 仍未完成

- 六个平台的完整 Stage 0 私有样本执行与独立人工批准；仓库不附带批准记录。
- Bilibili HTTP 412 的稳定性边界、Douyin fresh-cookie 授权复验、TikTok 首个授权真实样本，以及 Instagram 从单样本扩展到 Stage 0。
- 真实 X attachment stable key/exact-selector；`VDC_ENABLE_X_GRAPH_V2` 必须保持关闭。
- 目标 Linux/Docker 的 namespace、UDS、ACL、resource/core-pattern、Cookie、短链 egress、恢复演练和真实联网验收。
- 任何第三方 dependency/binary/container/tool-bundle 的公开再分发批准。
