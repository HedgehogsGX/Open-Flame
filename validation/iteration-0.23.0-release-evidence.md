# v0.23.0 源码发行工具与解压安装验收

本记录补充 [v0.23.0 源码安装](iteration-0.23.0-source-setup-evidence.md)，不把历史下载结果改标为新平台验收。

本轮新增随源码分发的显式文件清单、构建/归档校验、Windows 解压安装检查，替代仅存在于 ignored 历史目录的 verifier 导入链。产品包载荷与四个 Setup / Start 入口未因发行工具本身变更。用法见 [发行说明](../docs/RELEASE.md)。

## 验证范围

源码 ZIP / sdist / wheel 以同一明确文件清单中的原始字节构建，检查完整源文件与法律材料、项目身份、根入口摘要、wheel RECORD / metadata / console scripts、归档路径与隐私规则。实际发布摘要与每文件清单放在归档外，避免自引用。

Windows 安装检查使用新工作目录与隔离 profile，实际运行解压包中的 Setup、重复 Setup、Start `--check`。不读取真实 Cookie、不下载平台媒体、不修改日常业务数据。它不是浏览器渲染或免 Python 发行验收。

## 结果

新增 138 个发行相关回归：归档/隐私/法律材料 98，源码安装验收 20，wheel 验收 20。最终全量 **1670 passed, 8 skipped in 174.30s**；跳过项为 Windows 不具备的 POSIX directory-fd、root/getfacl、AF_UNIX、打开文件替换、权限位。compileall、离线 24 包锁和 whitespace 检查通过。没有新运行依赖。

构建时发现源码仍被并行编辑，工具以 `source_changed_during_build` 拒绝候选输出；冻结后重新构建通过。负面回归还发现并修复了 `.ENV` 大小写、媒体/SQLite sidecar 扩展名、正斜杠用户路径漏检、元数据版本未核对、仅检查 LICENSE 片段的问题。最终保留原失败断言，不通过放宽测试换取通过；现在比对确认的完整 Apache 文本 SHA-256。

首次实际完整制品为 `validation/local/release-0.23.0-preflight2-20260904/release/`：266 份源码、267 个 sdist 成员、103 个 wheel 成员（67 包载荷、32 法律文件、4 元数据文件）。不包含日志、Cookie、数据库、媒体、EXE/DLL、第三方 wheels 或历史运行目录。显式文件清单、文本扫描及高置信度秘密规则不能证明排除所有未知秘密。

其真实解压安装报告在 ignored `validation/local/release-0.23.0-source-check-20260904/report.json`：系统 CPython **3.13.14**，依赖来自 PyPI、工具来自已核对的本地缓存；首次 Setup **30.453s**、重复 **6.094s**、Start `--check` **5.672s**。两次安装环境/工具内容相同，安装不创建业务目录，Start 才创建测试数据库；三个组件同 run、无任务/凭据/媒体，端口释放。每一步在关闭兜底 Job **之前**检查所属活动进程数为 0。

独立 wheel 报告在 ignored `validation/local/release-0.23.0-wheel-check-20260904/wheel-smoke-report.json`：CPython **3.12.13**，全部依赖来自本地缓存，`pip check`、13 份 runtime 版本/位置、安装后的精确包身份、13 个真实 console scripts `--help` 全通过。使用现有有界 subprocess runner；这份报告不声称实际使用 Job accounting 或物理键盘。

实际浏览器 smoke 使用相同包身份的已解压源码、独立业务根 `validation/local/release-0.23.0-ui-check-20260904` 和端口 18833。截图确认页面正常渲染；提交 `not-a-url` 与保留无效域名的输入，分别显示 `invalid_url` / `unsupported_platform`，不创建 DownloadJob。刷新后从最近批次重新打开，成品空态、日志手动刷新、匿名选项可用；检查时浏览器捕获的 warning/error 列表为空。运行日志中有浏览器额外未匹配请求的 404 WARNING，不把它隐藏为零告警。没有请求媒体平台，也未验证真实 Cookie、TXT/CSV 浏览器文件选择或所有下载交互。

通过 PTY 发送 Ctrl+C 后，日志显示正常 supervisor 关闭、Worker 和 control 停止；三 PID 消失、端口可重新独占绑定。PowerShell/PTY 包装会话返回 1，**不把它报告为应用进程 exit 0**，亦不当作 Explorer/物理键盘验收。数据库保留本轮 1 个失败测试批次、2 条无效输入、0 个下载任务。

以上测试绑定运行包 `0.23.0+build.sha256.64a62ca9ccb395d6e559d276feea2dea1fa562da0fb25035ee53838f0eda77cf`；根四个入口保持安装阶段的精确 hash。提交阶段发现四份旧 Markdown 的 hard-break 行尾空格，改为 CommonMark 显式反斜杠换行，不改变历史事实；因此最终文档冻结后的交付目录改为 ignored `dist/Open-Flame-0.23.0-final-r2/release/`。制品 hash 必须从该目录的外部 manifest / SHA256SUMS 读取，不能套用预构建或 first-final ZIP 的摘要。最终包要重新执行两路安装核对；其独立报告保留在本机 `validation/local/release-0.23.0-final-*-r2-20260904`。

## 许可与剩余工作

源码仍为 Apache-2.0，保留原版权 NOTICE；项目 wheel 不包含第三方运行二进制。标准包格式及现有许可材料的机械核对，不代表第三方离线包再分发获批准。抖音登录态、完整平台 Stage 0、Linux / Docker、免 Python 分发与实际浏览器/键盘生命周期仍需各自验证。
