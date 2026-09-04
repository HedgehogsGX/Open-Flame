# Iteration 0.22.0 — 源码启动器与持久化故障诊断

日期：2026-09-04。本轮源码功能、固定入口验证及全量回归已完成；不代表下载器全部需求或独立 EXE/安装器已完成。未提交、push、生成发布包或重新下载平台媒体。

## 用户可用的变化

- 根 `Start-Open-Flame.cmd` → stdlib-first `start_open_flame.py` → 同进程 `desktop_launcher` → 现有 `local_app_cli`；没有再增加后台服务包装层。
- 无需当前工作目录恰好是仓库；使用项目 `.venv` 和明确的工具选择顺序，保持已有业务数据默认根。显式用户参数在默认项之后，保留覆盖关系。
- 正常打开本机页面，失败保留窗口；Ctrl+C 包括依赖导入阶段统一为取消语义，不误记为崩溃。启动提示兼容英文 Windows 输出管道；诊断在不能直接编码中文时回退为同一 JSON 的 ASCII 转义，不重复保存或换错误码。
- 独立诊断写入 `%LOCALAPPDATA%/Open-Flame/diagnostics/runtime-launch-diagnostics.jsonl`，不依赖失败的业务 app root。复用字段白名单 RuntimeLogger，256 KiB、3 份备份，线程锁与 Windows 文件锁串行化轮转/写入，等待最多 1 秒。
- 只接受固定 DiagnosticCode；参数错误、已知启动错误、未知运行错误与依赖导入失败均可落盘。`diagnostic_status=saved/unavailable` 与原业务 `log_status` 分开；保存失败不改变原错误码或退出码。

使用说明见 [Windows 启动器](../docs/WINDOWS_LAUNCHER.md)。缺 Python 或项目诊断模块本身时只能明确报告未保存，不能声称有应用日志。当前证明正常关闭后可读，不保证断电持久性。

## Red → Green 与独立审查

- 缺入口的真实隐藏 CMD 测试先失败；随后跨 CWD、占用端口、工具覆盖、参数失败和缺 Python 等场景通过。
- 严格 cp1252 stdout 复现中文 banner 阻止启动；改为 ASCII banner 后通过。导入阶段 KeyboardInterrupt 外逃已红复现并纳入正常取消边界。
- stderr 的同类编码失败独立红复现；全部 7 个固定诊断码、UTF-8 对照与严格 cp1252 均验证一条 JSON、一条原错误记录。
- 持久化覆盖真实多进程完整写入、并发轮转、外部锁占用、锁关闭异常后线程锁释放、缺失/非法目录、保存异常、hardlink 锁拒绝以及无 site-packages 的依赖错误。原异常、参数、环境值和 stderr 不进入记录。
- 新诊断事件有独立语义检查，没有放宽其他既有事件的 failure_site 合同。

中间一次预检发生在源码并发编辑窗口：已出现 control_ready / worker_preflight_ready，随后 startup_failed 并自然清理。日志不能唯一证明根因，不将构建漂移推测写成事实。最终冻结后重新验证 **6 passed in 10.65s，0 skipped**。源码成功 smoke 只在未安装工具包时明确 skip；存在但损坏不能跳过。

## 最终构建正常模式验证

`0.22.0+build.sha256.a2e442135f50f4777c952eeb6976b44b97d16edf29c2bed3aa194427237863da`；包外根入口另有 SHA-256 绑定并保存在本地报告：

- `Start-Open-Flame.cmd`: `c272575fd0654748a33f633f95567fc001bb08ce43aaf8613df7517a1e661e0b`
- `start_open_flame.py`: `c54080c6d20c28ac260b69f60e99ada40fa7db791c650a9957be359b750efa34`

从中文、空格、`!` 的外部 CWD，经实际根 Python bootstrap 启动空队列正常应用。5.546 秒内完成 ready、健康 API、HTML 页面读取、浏览器打开请求恰好一次、正常停止；三个进程同 run，退出码 0，残留 0，端口释放，未产生失败诊断，源码与根入口未改变。

OS 浏览器 opener 被测试钩子拦截，因此这只证明打开请求，不冒充实际浏览器渲染或 Explorer 鼠标双击。停止使用此前验证的 test-only PeekNamedPipe EOF → SIGINT 桥；不向用户控制台广播信号，也不将此描述为真实键盘 Ctrl+C 测试。CMD 入口另由上述 6 项真实隐藏进程测试覆盖。

## 全量回归

冻结源码后执行 `.venv\Scripts\python.exe -m pytest -q`：**1492 passed, 8 skipped in 142.88s**。8 项跳过均为当前 Windows 环境不具备的 POSIX / AF_UNIX / 权限条件，并非平台下载成功。源码构建 identity 在文档收尾时重新读取，与上方实际生命周期验收一致。

## 文档、隐私与许可复核

- 编译检查、`git diff --check`、离线锁检查通过；锁定 24 个包，除项目版本外外部依赖不变。
- 39 份 Markdown 的 133 个本地文件链接全部可解析。
- Git tracked 与 nonignored untracked 去重清单共 248 文件；未发现运行目录、数据库、日志、媒体、密钥或二进制文件进入该清单。真实用户标识、实际样本标识、高置信度令牌/私钥与 Cookie 记录扫描均为 0。
- Windows 用户路径形式的 15 处命中均已分类：14 个合成测试值、1 个历史扫描模式；没有未分类项。本轮重点 12 文件没有媒体 URL 或上述风险命中。
- 本地验证目录、工具目录、业务日志和 `.venv` 的 4 个忽略规则探针全部生效。Apache-2.0、精确 NOTICE 版权行及第三方许可材料相对 HEAD 未改。

以上为当前文件清单的静态复核，不保证排除未知形式的秘密，也不是第三方工具再分发的法律批准。没有执行提交、推送或发布。

## 保留的未完成项

源码启动器仍需要已安装的 `.venv` 与锁定工具包，不是免 Python EXE。平台 Stage 0、抖音登录态、安装器、Linux/Docker 和第三方二进制再分发仍未通过。上一轮 TikTok/Instagram 下载结果保留为 [0.21.0 历史证据](iteration-0.21.0-platform-startup-evidence.md)，本轮没有借用它们声称对新构建重新做过网络下载。

项目许可仍为 Apache-2.0，NOTICE 保留 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。没有新增依赖；本地测试运行目录和运行日志均不纳入发布源文件，自动化测试源码仍属于仓库内容。
