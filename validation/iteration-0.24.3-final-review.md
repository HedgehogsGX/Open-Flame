# Iteration 0.24.3：最终源码审查与仓库交付

日期：2026-09-05；Windows x64、CPython 3.12。用户要求在八项修复后再次审查源码，并直接提交到仓库。审查基线为 `f12749f1dc8d0a2ebbb805669004e69ce33666a7`，范围包括全部待提交修改和新增交付文件。

审查依据为 [原始八项发现](full-debug-20260905.md)、[修复合同](iteration-0.24.3-debug-fixes.md)、[开发约定](../AGENTS.md)、[设计规范](../docs/DESIGN_SYSTEM.md)与[执行计划](../docs/FOLLOW_UP_EXECUTION_PLAN.md)。Standards 和 Spec 独立检查，另对运行时执行边界交叉审查。没有真实平台登录、账号检查、下载、上传或投稿；所有新增故障场景使用本地合成数据。

## Standards

- **R-01 / P1：下载快照生成阶段未处理断连。** 旧同步路由在返回 Response 前完整复制原件；仅发送阶段的取消测试不能覆盖生成阶段。改为响应流程监听 `http.disconnect`，以事件逐块停止复制，收回源文件、spool 和监听任务。SHA-256 通过前不发送 200 或媒体正文；已断开的请求仅使用内部 499 空响应完成中间件生命周期，正常下载保留 200/206/416，核验失败保留 409。追加断连、外层任务取消、发送异常和清理异常回归。

同步文件系统正在进行的单次 read/write 不能被 Python 强制终止；协作取消在 64 KiB 块的读取和写入前后检查，并等待执行线程归还句柄。全量原件快照的磁盘及首字节代价仍适用，不声称消除所有资源耗尽风险。

## Spec

- **R-02 / P2：带 WAL 的已有上传库校验会在原目录创建 SHM。** 公共服务构造回归先复现 32 KiB 新 SHM，再将主库及 WAL 复制到受控临时目录；以文件身份、大小及散列复核稳定性，在副本中执行 SQLite 结构检查。固定 1 MiB 块、初始大小读取预算和最多两次尝试，禁止追尾持续增长的文件。源目录和主库/WAL/已有 SHM 不因核验改写；此快照不是上传备份或恢复工具。
- **R-03 / P1：无缓存执行漏掉 Windows 门禁。** 页面返回 `unsupported_platform` 时，原执行 helper 仍可能启动子进程。无缓存入口现在先拒绝非 Windows，再执行完整 runtime 检查；回归要求拒绝发生在创建 operation 和调用子进程前。
- **R-04 / P2：重定向安装根目录在被拒绝前留下锁文件。** CLI 现在在 mkdir、lock 和 check/install 分支前核对 symlink/junction；分别覆盖安装、只读检查及不存在根目录。

WAL 处理以 SQLite 可见结构为边界，遵循有效提交前缀与重用 WAL 的规则；不会据此证明历史提交完整或自动修复源库。[SQLite 官方文件格式](https://sqlite.org/fileformat2.html#walformat)

## 提交一致性

部分工作树源码原为 CRLF/mixed，而 `.gitattributes` 要求 LF，直接提交会改变精确构建 hash。已按既有 Git 规则统一换行；`runtime-lock.json`、审计许可文件和 Windows CMD 的原有字节规则保留。提交前核对 Git index 与工作树逐文件一致，检查发行清单、Python AST、文档链接、版本/锁合同和敏感内容边界。

## 最终验证

四处审查发现均已修复，下载取消补修另经独立只读交叉审查，未发现剩余可行动缺陷。定向验证如下：

- 下载取消、完整性与资源清理：资产/API 两组 30 passed，下载上传 API 集成另 33 passed；交叉审查再次运行资产组 30 passed。
- 上传库：service/API/下载导入集成 67 passed，相关 UI 定向 4 passed。覆盖 schema 仅存在 WAL 的无 SHM 副本、坏 header checksum、真实 SQLite RESTART 后的旧 salt 尾部及 hardlink；源目录逐字节保持不变。16,846,848 字节合成主库核验约 0.080 秒，仅为本机小型基准。
- 运行时：backend、完整性与环境锁合计 71 passed；平台门禁、check/install 的重定向拒绝均先复现失败再转绿。
- 最终全量：**1952 passed、8 skipped，218.87 秒**；命令为 `.venv\Scripts\python.exe -m pytest -q`。8 项跳过对应本机 Windows 无法满足的 POSIX/root/getfacl/Unix socket 条件，未以跳过表示通过。原复验 1941 项通过是追加审查修复前的结果，保留在本地独立 XML 中。
- 交付门槛：307 个明确发行文件、198 个 Python 文件 AST、271 个本地文档链接、版本/锁合同与敏感内容检查通过；Git index 的 308 个文件逐字节匹配工作树（额外 1 个是既有、不进入源码发行的历史许可审计报告）。`git diff --cached --check` 通过，`uv pip check` 确认 23 个依赖兼容。

## 构建与交付边界

- package_version：`0.24.3`；下载 Schema 11，上传独立 Schema 1。
- product_identity：`0.24.3+build.sha256.768205aee80994ed42b001b369e829773e5d01d06c1969da95fea822163240bd`。
- 交付分支：`codex/uploader-first-platforms`。当前源码身份采用包含本记录的完整 Git 提交；以 `git rev-parse HEAD` 和远端提交页取得 40 位 SHA，避免在提交内嵌自身 SHA。
- source_archive_sha256：本次交付为 Git 提交，填 `N/A (Git checkout)`；未将旧本地 ZIP 重新标成当前制品。
- 先前源码 ZIP/wheel 安装通过的身份仍为 `0.24.3+build.sha256.746986773cecba705e78d66f35d4530a9a6d6d276e35769a8825bbe4a0cb4c46`，源码 ZIP SHA-256 为 `fc92fa46a7dc822b515b04f95064df6e9a07483cab1d7f9eac68263d5b9c5bd1`；这些是最终审查补修前的历史证据，不能用来声称本提交的精确制品已安装验收。
- 本次没有修改用户已有 runtime、账号、媒体或数据库；详细本地回归证据保存在 ignored `validation/local/final-review-20260905/`。账号、数据库、媒体、运行时二进制及私有日志均不进入 Git。
- 上传备份恢复、长期压力、CI 门禁、真实平台矩阵、新精确制品安装及最后的 Apple 风格整站升级仍按 [执行计划](../docs/FOLLOW_UP_EXECUTION_PLAN.md)推进；Git 推送、合并 main 和发布 GitHub Release 分别记录。
