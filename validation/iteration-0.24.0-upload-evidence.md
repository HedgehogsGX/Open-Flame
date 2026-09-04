# v0.24.0 首批上传器开发与验证

日期：2026-09-04。首批范围按用户最新选择为 Bilibili、抖音、视频号；其他平台后续维护。项目源码版本更新为 0.24.0，下载 Schema 11 保持，上传另用 Schema 1。

## 已实现边界

- 独立上传账号与私有运行状态，不读取下载 Cookie 或日常浏览器的登录资料。
- 浏览器本地文件导入、既有 ready 成品复制与 hash 复核、受管媒体执行前重验。
- 多账号本地草稿、明确确认、串行执行、取消、不可确定结果及人工核对后的新草稿。
- 登录变化撤回旧排队确认，执行前重验账号状态；重启不自动重放排队或结果未知的投稿。
- loopback Host/Origin/Sec-Fetch 校验、会话 nonce、公开字段白名单及原始输出隔离。
- 固定上游工具、独立 Python 与浏览器环境、Windows 子进程 Job 管理。

## 本地测试记录

初始服务回归 11 项通过；独立审查进一步复现跨实例取消未传达、畸形 backend 结果中断调度、账号检查失败后旧队列继续调用三项问题。均已修复，并补跨实例、队列继续执行、重新登录、未知数据库保留与显式 Bilibili 版权选择等回归。服务/API/UI 的最后定向结果为 **40 passed**。

独立审查还发现：视频号旧上游将跳到登录页误当成功，以及空浏览器配置依赖系统 Chrome。bridge 改用投稿响应确认及显式私有浏览器，不将“离开编辑页”或“按钮消失”视为成功。缺少充分结果证据时保留 `unknown`。

最终全量回归为 **1752 passed, 8 skipped in 192.69s**。8 项跳过均为 Windows 不具备的 POSIX directory-fd、root/getfacl、AF_UNIX、打开文件替换、权限位检查。首轮有一项严格 sdist 清单断言尚未加入 `data-uploads`，已同步该排除项，并新增发布隐私门禁测试，确保即使手写发行清单也拒绝上传账号目录。没有删除失败断言或放宽隐私门禁。

当前源码发行清单含 **284** 个实际文件；compileall、whitespace 与本轮 7 份主要文档的 **96** 个本地链接检查通过。源码用户的安装命令另外以 `python -S` 禁用 site-packages 后成功载入 `runtime_setup --help`，证明不依赖开发环境的 editable 安装。精确包身份为 `0.24.0+build.sha256.073565a25297d14c80ba119364839980cae0783c0d471a1eadd61e0915aedf22`。

本地源码 ZIP、sdist、wheel 的构建输出位置为 ignored `dist/Open-Flame-0.24.0-uploader/`；制品验证、外部 manifest 与安装报告记录最终构建结果，不在归档内嵌入自身的散列。没有 push 或创建 GitHub Release。

## 真实本地浏览器操作

使用独立控制面、独立测试数据和 `OfflineBackend`，端口 18839。通过 Codex 浏览器实际完成添加三平台模拟账号、检查模拟状态、文件选择器导入、选择三账号、填写分区与投稿类型、创建三份本地草稿、模拟 Bilibili 投稿、模拟视频号平台草稿、取消抖音草稿。刷新页面及重启服务后，三种终态保留，未重新执行。页面有 5 个 section、3 个任务条目，无水平溢出；检查的 browser warning/error 记录为空。

这些操作仅验证浏览器、API、存储和模拟执行器的连接，不是平台登录或真实上传。测试文件明确是 dummy bytes，不能作为有效视频或编码验收。测试 helper、数据库和源文件留在 ignored `validation/local/uploader-ui-20260904-01/`。长页面合成截图出现动态页面拼接重复，因此以实际单视口截图、DOM 与持久化记录核对，不把拼接重复当成页面实际 DOM。

模拟服务随后通过 Ctrl+C 停止（PowerShell 包装返回 1，不声称应用 exit 0）。同端口启动了接入实际已安装上传环境的验收页：上传使用普通 Windows `data-uploads`，下载控制库使用另一份独立空目录。真实浏览器已看到“引擎：可用 · 就绪”、仅三平台选项、账号与任务为空，无水平溢出。该页没有创建账号、登录、上传或发布；登录和内容确认留给用户。验收 helper 位于 ignored `validation/local/uploader-ready-preview-20260904/`。

## 真实运行环境

独立安装位于普通 Windows 应用的 `data-uploads/runtime`。固定 SAU revision、biliup release、archive SHA-256 及 Python lock 见 [运行环境](../docs/UPLOAD_RUNTIME.md)。主代理已读取实际 `SauBackend.inspect()`：`ready=true`，SAU `0012d2c355f88f683cc38dde2a2db209e14091bc`，biliup `v1.2.4`。安装流程实际执行三平台各 3 个 CLI help、biliup upload help 与本地 Chromium DOM smoke；没有登录、凭据或媒体平台请求。

真实安装发现并处理上游漏列 Playwright 依赖及 Windows Chromium 路径兼容问题；具体处理和限制见运行环境说明。原下载 `.venv` 的运行依赖没有加入上传包或浏览器依赖。

后端定向回归 **41 passed**，包括真实 Windows 子孙进程的取消和超时终止。另实际载入 async bridge，使用视频号的 `channel=chrome` 路径打开固定 Chromium；本地 role/aria-label 按钮的普通点击与 JS fallback 连续调用后，点击计数仍为 1。该检查仍仅操作本地 DOM。

## 尚未证明的事项

- 三个平台各自的真实账号登录、平台权限、验证码及登录失效行为。
- 用户选定视频的实际上传、平台草稿/投稿响应、后台作品与审核结果。
- 实际平台链路上的取消、网络中断、结果核对及安全重试。
- Linux / Docker / NAS 上传，其他平台，独立免 Python 安装器。

本轮不宣称上述项目已通过，也不将开源项目的功能清单当作 Open-Flame 的真实平台证据。项目源码更新不等于 GitHub push、GitHub Release 或第三方二进制合集再分发。

独立审查曾生成一个只含模拟视频与数据库的系统 Temp 目录；自动审批拒绝其精确路径清理，未提供更详细理由，目录已保留。不存在真实账号或用户媒体。

另有两处本轮 SideBySide 诊断临时目录的清理被自动审批拒绝并保留，内容为固定浏览器二进制硬链接及失败诊断日志，不含账号或媒体。未换手段执行删除。正常操作产生的临时浏览器视图与平台子进程已检查无残留。
