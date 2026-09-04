# Iteration 0.14.0 — Windows 本机 Worker Cookie 接入工程证据

> 日期：2026-09-04
> 项目版本：`0.14.0`；数据库：Schema `10`
> 范围：Windows 本机、synthetic Cookie/fake media、独立 loopback API/UI、project-only Python package
> 非证据：真实 Cookie、真实平台下载、平台 Stage 0、Linux/Docker 隔离、第三方 binary/container/tool-bundle 再分发

## 1. 本轮结论

本轮把已经存在于 Linux candidate 的 `AttemptCookieResolver` 接入 Windows
`video-download-local-worker`。管理员可为 `x`、`youtube`、`bilibili`、
`douyin`、`tiktok`、`instagram` 分别提供至多一个
`PLATFORM:OPAQUE_REF=ABSOLUTE_PATH` source；opaque ref 必须和 Job 已分配
CredentialProfile 的 `secret_ref` 精确一致。数据库不保存 Cookie 路径或内容，公开
HTTP API/网页也不接收这些字段。

probe 与 download 分别从已打开且身份稳定的 source 建立新的 Attempt 私有副本；
yt-dlp 只接收副本，操作结束后副本被清理。启动预检会拒绝空文件、超过
`--max-cookie-bytes` 的文件、可写/link/reparse/hard-link source、路径重叠，以及不同
平台通过路径别名复用同一打开文件身份。所有公开失败均使用固定脱敏消息。

新增 `--check` 先验证双重本机直连开关、单实例、共享数据库 Schema 10 readiness、
固定工具链、ffprobe 与所有 Cookie source，然后输出只含已配置平台的 JSON。它不会
调用 `Worker.run_once()`，日志使用独立 `worker.preflight_started/succeeded/failed`
事件，不会把预检误写成常驻 Worker 已启动。

## 2. 自动化回归

| 检查 | 结果 |
|---|---|
| 本机 Worker CLI | `21 passed`；覆盖解析、重叠/重复平台、真实 builder no-claim 快照、日志事件与错误脱敏 |
| Worker/Cookie/凭据/Adapter/日志/验证定向集 | `261 passed, 1 skipped in 11.48s`；skip 为既有 POSIX-only directory-fd cleanup |
| 当前版本/API/capability/deployment 定向集 | `112 passed, 4 skipped in 15.26s`；4 个 skip 为 root POSIX/getfacl metadata contract |
| 全套 pytest | `1084 passed, 8 skipped in 90.42s` |
| Python 编译 | `.venv\Scripts\python.exe -m compileall -q src tests` 通过 |
| 依赖锁 | `uv lock --check --offline` 通过；仅 project root version 从 0.13.0 更新到 0.14.0 |
| diff 结构 | `git diff --check` 通过 |

8 个 skip 均为既有目标操作系统/权限边界：1 个 POSIX Cookie directory-fd cleanup、
4 个 root POSIX/getfacl metadata contract、1 个当前 Windows 不可用 AF_UNIX roundtrip、
1 个 POSIX open-file replacement、1 个 POSIX permission test。它们不计为通过，必须在
目标 Linux/root acceptance 中重跑。本机环境没有安装 Ruff，因此本轮没有宣称 0.14
通过 Ruff；0.13 的 Ruff 结果只保留为历史事实。

## 3. Cookie 与队列端到端验证

synthetic Douyin 回归在全新 Schema 10 数据库中执行以下完整链路：

1. 创建单作品 Batch/queued Job；
2. 注册 Douyin CredentialProfile 并按 Batch 分配 opaque ref；
3. 从仓库外只读 synthetic Cookie source 构造真实 Windows 本机 Worker；
4. claim 时复核 profile 存在、平台一致、未过期/禁用；
5. probe 和 download 各得到一个不同的 Attempt-private Cookie 副本；
6. fake runner 验证副本内容后生成 synthetic media；
7. Worker 完成 verifying/committing，Asset 为 `ready`；
8. 两个 Cookie 副本均被删除，日志中不含 ref、source 路径或内容标记。

另一条 `main --check` 集成回归使用真实 `build_local_worker`，只替换外部工具探测；
执行前后逐表比较 `download_jobs`、`job_attempts`、`media_assets`、
`asset_commit_intents`，结果完全相同，source identity/size/mtime 不变，且未出现
`temporary/**/secrets`。这锁定了“预检不领取、不创建 Attempt/Asset、不复制 Cookie”的
产品契约。

Bilibili Stage 0 的 BV-only 门禁另外用 pinned yt-dlp 已确认的等价 locator 对加固：BV
形式可进入 manifest，等价 av 形式必须 fail closed，避免一个实际投稿被算作两个正样本；
普通下载 URL 仍接受 BV/av。本轮没有据此发起 Bilibili 请求或改变其 `candidate` 状态。

## 4. 独立 API/UI/日志验收

验收使用独立 `127.0.0.1:8140` 和新的 gitignored data root；用户原有
`127.0.0.1:8000` 进程未停止、替换或复用。先用真实固定 Windows 工具链和只读
synthetic Cookie source 执行本机 Worker `--check`：stdout 返回
`{"cookie_platforms":["douyin"],"status":"ready"}`，日志依次出现 preflight、
toolchain/subprocess 与 succeeded 事件；`/health/ready` 返回 v0.14.0 / Schema 10。

随后通过真实网页表单提交 Bilibili、Douyin、TikTok 三条 synthetic 直链，并用显式
gate 的 offline fake Worker drain：

| 检查 | 结果 |
|---|---|
| Batch/Worker | `3/3 ready`；三个 route 各有一个 succeeded Attempt 与 ready Asset；commit intent 为 0 |
| 前端 | 版本 0.14.0、7 个静态 capability、固定工具链 ready、最近批次 `3/3 ready`、3 个成品链接均可见 |
| 原件下载 | 3 个端点均 HTTP 200；响应字节数、`Content-Length`、SHA-256 与资产 DTO 全部匹配 |
| 下载响应 | `Cache-Control` 同时含 `private`/`no-store`；`X-Content-Type-Options: nosniff` |
| 日志 | control、local-worker preflight、offline-worker terminal 事件可读；`write_failures=0`、`rejected_events=0` |
| 隐私标记 | 日志/文本中 Cookie 内容、opaque ref、Cookie 文件名/source-root 标记 0 hit；data root 无残留 `secrets` 目录 |
| 浏览器 | 创建、刷新批次、刷新能力、刷新工具、刷新日志和成品下载操作正常；console warning/error 为 0 |
| 清理 | 独立 8140 控制面正常 shutdown，端口不再监听；原 8000 仍由原 PID 监听 |

原始数据库、日志、UUID、synthetic URL 和 Cookie fixture 只在 gitignored
`validation/local/` 中保留，不进入本文件或发布包。

## 5. Apache-2.0、发布包与隐私边界

项目自有源码、文档与脚本继续依据 Apache-2.0 分发，版权声明保持
`Copyright 2026 HedgehogsGX & Cyaegha_Xu`。发布构建只包含 project sdist/wheel，
不捆绑本机 `runtime-tools`、Cookie、数据库、日志、媒体或 `validation/local`。

最终 package 在文档回填后离线重建并复核：source tree 有 202 个 publishable 文件；
sdist 有 203 个 regular file 条目（其中 1 个为生成的 `PKG-INFO`），wheel 有 92 个
file 条目；两者没有 duplicate、unsafe path、link 或 special entry，wheel CRC 与 92 行
`RECORD` 全部通过。metadata/import 为 `0.14.0` / `Apache-2.0`；12 个项目 console
scripts；32 个法律文件；wheel 与 sdist 中 56 个 package 文件均与 checkout 逐字节一致；
checkout/wheel product build identity 相同；隔离安装与 `pip check` 通过。wheel 的 18 类
隐私/秘密 marker 全部 0 hit；sdist 仅命中 `tests/**` 中明确用于负向验证的 synthetic
canary，已知本机路径、真实用户标识、历史真实样本及高置信秘密标记均为 0 个真实泄漏。
制品 SHA-256 只写入 gitignored 的包外 `SHA256SUMS.txt`，避免 sdist 自引用。

项目 Apache-2.0 不重新许可下载媒体或第三方组件。dependency wheelhouse、冻结可执行文件、
OCI/container image 与 yt-dlp/FFmpeg tool bundle 继续保持
`blocked_pending_third_party_source_and_notice_audit`。

## 6. 已知限制与下一入口

- Windows 合法 `--cookie-source` 参数会进入进程命令行和 PowerShell history；当前 source/
  Attempt 文件没有经过 Windows owner/DACL 私有性证明。只允许受信任单用户主机使用。
- `--check` 验证配置的文件与工具，不检查当前 queued Job 的 credential coverage，也不能
  证明 Cookie 登录态仍有效。
- 同一 Worker 进程仍可读取其所有平台 source。真实凭据前需要受 ACL 保护的配置入口、
  credential sidecar，以及 per-platform 或 per-Attempt process/mount isolation。
- 控制面与 Worker 仍需分别启动，错误 data root/database 会形成不同队列。下一产品切片
  应实现 Windows `video-download-local-app` supervisor，统一路径、健康握手、浏览器打开、
  子进程生命周期与单实例。
- Bilibili 间歇 412、Douyin 真实 fresh-cookie、TikTok 首个真实样本、Instagram Stage 0、
  X exact-selector、目标 Linux/Docker 与完整第三方再分发审计均未完成。
