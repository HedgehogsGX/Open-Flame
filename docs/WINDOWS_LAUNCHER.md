# Windows 源码版：双击启动与故障日志

本入口从 v0.22.0 提供。它不依赖 Codex 运行，但仍需要项目本地 Python 环境和已安装的锁定工具包；**不是免安装、免 Python 的 EXE**。

## 启动与停止

首次使用先按 [安装与修复指南](WINDOWS_SETUP.md)运行根 `Setup-Open-Flame.cmd`，完成后在项目目录双击 [`Start-Open-Flame.cmd`](../Start-Open-Flame.cmd)。启动器使用该目录的 `.venv\Scripts\python.exe`，不自动安装依赖、不修改系统 PATH，也不需要先打开 Codex。

启动成功后浏览器会打开本机页面，默认地址为 `http://127.0.0.1:8000/`。保持启动窗口运行；按 **Ctrl+C** 正常停止。请勿在任务进行中直接关闭窗口，以免来不及完成正常清理。CMD 启动即表示使用本机直连网络；它不是 Linux 隔离部署。

启动器不会改变工作目录或把业务数据放进源码仓库。默认业务目录仍为 `%LOCALAPPDATA%\Open-Flame\video-download-control`，可选 AI runtime 位于同一应用根的 `data-ai-runtime`。工具选择顺序为显式 `--tool-root`、存在的项目 `runtime-tools\windows-x64`、原有应用默认工具位置；已有但损坏的项目工具包会明确失败，不偷偷更换工具来源。

v0.23 的源码启动器会持有环境共享读锁，安装器则持有独占写锁；普通 Start 还会在整个生命周期持有应用根的 `.local-app.lock`，可选 AI runtime Setup 使用同一把锁，避免在运行中替换或出现 runtime。出现 `setup_busy` 时先正常结束占用该源码或同一应用根的应用/安装。高级手动 CLI 不经过全部普通启动锁，修复前也须自行停止这些实例。

需要其他端口或本地配置时，可在 PowerShell 中执行：

```powershell
.\Start-Open-Flame.cmd --port 8001
.\Start-Open-Flame.cmd --check --no-open-browser
.\Start-Open-Flame.cmd --cookie-config "C:\private\cookie-sources.json"
.\Start-Open-Flame.cmd --app-root "D:\Open-Flame\video-download-control"
```

最后两行仅为路径示例。配置必须是用户准备好的只读 JSON；不要将 Cookie 内容作为参数，也不要提交到 Git。自定义 `--app-root` 时，如需 AI，先向 Setup 传入同一个目录和 `--ai-python-embed-zip`，使 runtime 精确落到 `<app-root>\data-ai-runtime`。`--check` 只预检、不会领取下载任务或自动打开浏览器。

## 启动失败去哪里查

失败窗口会保留提示。若 Python 与项目诊断模块可运行，参数错误、端口占用、缺工具、坏 Cookie 配置、依赖缺失等会在 stderr 给出固定 JSON，并尝试写入：

```text
%LOCALAPPDATA%\Open-Flame\diagnostics\runtime-launch-diagnostics.jsonl
```

可把上述目录粘贴到文件资源管理器地址栏。诊断文件为 UTF-8 JSON Lines，含时间、错误码、位置、应用版本和进程/事件关联信息，不保存原始命令行、路径、Cookie 或异常原文。单文件上限 256 KiB，保留 3 份轮转备份。

- `diagnostic_status=saved`：本条诊断已写入；正常退出后可重新打开查看。
- `diagnostic_status=unavailable`：写入不可用，例如目录无权限或诊断锁持续被占用；保存终端提示，不要误以为已有日志。
- `log_status=not_started`：业务运行日志尚未启动；不代表独立诊断没有保存。
- `log_status=unknown`：故障阶段未知，也可能在运行中；同时查看业务运行日志。

| 错误码 | 操作 |
|---|---|
| `invalid_arguments` | 用 `--help` 核对参数；不要传入原始 Cookie |
| `local_port_unavailable` | 关闭自己确认可停止的占用程序，或用 `--port` 换端口 |
| `local_toolchain_unavailable` | 按 README 安装锁定工具包，或指定有效的 `--tool-root` |
| `local_cookie_config_invalid` | 检查只读 JSON；匿名启动可移除 `--cookie-config` |
| `local_dependencies_unavailable` | 按 README 修复项目本地 Python 依赖 |
| `local_app_failed` / `internal_error` | 保存诊断与同一时间附近的运行日志后排查 |

如果 `.venv` 的 Python 不存在，或项目自己的启动模块已损坏，诊断模块也无法运行。启动器会明确提示**未保存诊断**；先按 README 修复安装。上述机制不保证断电后所有已写日志都已进入物理介质。

## 运行中出错

打开前端的“运行日志”，或查看实际应用数据目录下的 `data\logs\runtime-*.jsonl`。默认单个业务日志文件 10 MiB，保留 5 份轮转备份；详情见 [Runbook](RUNBOOK.md)。提供故障时间、平台、错误码，以及相邻的 request/job/attempt/run ID，便于从同一次运行追查。

分享日志前仍应检查内容；不要连同业务数据库、下载媒体、Cookie 配置或 `runtime-tools` 一起提交仓库。
