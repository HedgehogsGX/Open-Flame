# Windows 源码版：首次安装与修复（v0.23）

本流程在源码目录准备本地 Python 环境和锁定媒体工具，之后可独立于 Codex 使用；不要求安装 uv。它仍需要系统 Python，**不是免 Python 的 EXE，也不是已发布的第三方离线工具合集**。

本轮已在独立空目录通过默认联网安装、重复安装、失败后修复及正常启动验证，实测系统 Python 为 3.13.14；详情与范围见 [v0.23 验收记录](../validation/iteration-0.23.0-source-setup-evidence.md)。这不代表所有平台或所有 Python 版本都已验收。

## 第一次使用

1. 在 Windows x64（AMD64）上安装 **64 位 CPython 3.12 或更新版本**。从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/) 获取；还须有与所选 Python 版本兼容的锁定依赖 wheel。当前工具链不支持 Windows ARM64。
2. 将完整源码放在可写的普通本地目录，保留根目录的两个 `.cmd`、两个 `.py`、`src` 和 `deployment` 等文件；不要只复制启动器。
3. 双击根目录的 [`Setup-Open-Flame.cmd`](../Setup-Open-Flame.cmd)。看到 `Continue? [y/N]` 后，确认允许准备环境及所需下载，再输入 `y` 并回车。空白、`n` 或输入结束会取消，不开始安装。
4. 等待出现 `Open-Flame source setup complete...` 和 `{"status":"ready"}`。无参数双击成功后窗口会暂停，便于检查结果；失败时保留错误提示。取消返回码为 130，不当作安装错误。
5. 安装实际成功后，双击 [`Start-Open-Flame.cmd`](../Start-Open-Flame.cmd)。默认打开 `http://127.0.0.1:8000/`；保持启动窗口运行，退出时按 **Ctrl+C**。端口、Cookie 配置与运行日志见 [启动指南](WINDOWS_LAUNCHER.md)。

Setup 优先检测已安装的 `py -3`，再尝试符合条件的 `python.exe`；不自动下载 Python、不修改系统 PATH，也不优先使用待安装的项目 `.venv`。如果找不到可用 Python，窗口会明确提示本次没有保存诊断。

以下命令在**源码根目录的 PowerShell** 中执行；`.cmd` 本身按自身位置寻找源码，不依赖调用时的工作目录：

```powershell
.\Setup-Open-Flame.cmd --help
.\Setup-Open-Flame.cmd --yes
```

`--yes` 是明确确认环境变更及所需联网，不是只检查。`--help` 不安装、不暂停。没有把系统 Python 加入 PATH 时，也可用其真实绝对路径调用根 bootstrap；下面路径仅为示例，需替换：

```powershell
& 'C:\Python\python.exe' -I .\setup_open_flame.py
```

不要用本仓库 `.venv\Scripts\python.exe` 执行 Setup，包括修复；它不能安装或修复自己。若当前终端激活了该环境，请先退出该环境，或明确指定外部系统 Python。

## 安装内容与联网范围

Setup 在仓库内准备 `.venv` 与 `runtime-tools\windows-x64`，并校验固定版本、哈希、依赖导入和工具离线 smoke。已有健康环境默认只验证，不重新安装依赖；已有工具包始终验证，不自动覆盖。

运行依赖来自 [`requirements.runtime.lock`](../deployment/requirements.runtime.lock)，不安装开发依赖，也不把项目本身安装为 Python 包。因此本流程不会创建项目的 `video-download-*` console scripts；日常使用根目录的 Setup / Start 入口，不能把高级 CLI 文档中的命令存在当作首次安装结果。

默认按需要从 PyPI 获取 Python 依赖、从 GitHub 获取锁定媒体工具及其来源材料。两个参数**分别**控制下载来源：

| 参数 | 作用 | 必须准备的内容 |
|---|---|---|
| `--wheelhouse ABSOLUTE_DIR` | 依赖仅从本地目录安装，不访问 PyPI；不限制媒体工具的 GitHub 获取 | 锁文件要求的全部 wheel，版本、哈希与当前 Python / Windows x64 兼容 |
| `--artifact-cache ABSOLUTE_DIR` | 缺失工具仅从本地缓存获取，不访问 GitHub；不限制依赖的 PyPI 获取 | [工具锁](../src/video_download_control/toolchains/windows-x64.json) 中各 `cache_name` 对应的文件，包括工具、来源包、校验及签名材料；不是解压后的工具目录 |

需要两部分都离线时，同时传入两个**已存在的绝对目录**。缓存缺项、哈希不匹配或 wheel 不兼容会失败，不会悄悄回退联网：

```powershell
.\Setup-Open-Flame.cmd --yes --wheelhouse 'C:\OpenFlameCache\wheels' --artifact-cache 'C:\OpenFlameCache\tool-archives'
```

上述为示例路径，不会自动为你准备离线缓存。依赖安装使用 `--no-index --find-links`（指定 wheelhouse 时）、`--only-binary` 和 `--require-hashes`；这些参数含义见 [pip 官方安装文档](https://pip.pypa.io/en/stable/cli/pip_install/)。本流程不验证平台登录或下载真实媒体。

## 修复与已有数据

先在各自窗口正常停止应用和 Worker，再执行：

```powershell
.\Setup-Open-Flame.cmd --repair --yes
```

可同时带上述缓存参数。`--repair` 只重新安装 **Setup 自己创建并标记拥有的 `.venv`** 中的锁定依赖，不是清空或重建整个目录；损坏的 Python 可执行文件不保证能通过此参数修好。

- 已有但不属于 Setup 的环境：健康时可验证并复用；需要修改或显式 `--repair` 时拒绝覆盖。先保留原内容并检查，不要伪造 ownership 标记来强制修复。
- 已有但损坏的工具包：校验失败并停止；`--repair` 也不会覆盖。保留诊断与原目录，核对来源后再制定恢复方案，不要靠删除文件绕过校验。
- 安装或修复不迁移、清空业务数据库、下载媒体或 Cookie 配置。默认业务目录仍为 `%LOCALAPPDATA%\Open-Flame\video-download-control`，不在仓库内。中断可能留下 Setup 拥有的部分环境，可在正常停止相关程序后重试。

源码 Start 在整个应用生命周期持有共享锁，Setup 在安装期间需要独占写锁，使用仓库根目录的 `.open-flame-setup.lock`。竞争时立即给出 `setup_busy`，不会等待或强行关闭应用。**直接运行高级 CLI 的旧实例不受这把源码入口锁保护**，修复前必须自行正常停止。锁文件存在不等于有人持锁，不要通过删除锁文件解除占用。

从旧版本升级时，如果旧工具完整但与新源码锁不匹配，先正常停止应用，将 `runtime-tools\windows-x64` 改名为同目录下尚不存在的备份名，再运行 Setup 安装当前固定工具；保留旧目录供旧版本回退。v0.23 已将失效的 FFmpeg daily URL 改为月末构建，仍核对精确哈希，不自动采用浮动 `latest`。

## 失败时检查什么

先保留窗口中的固定 JSON，重点看 `error_code`、`failure_site`、`diagnostic_status`。Python 和诊断模块可运行时，错误会尝试写到：

```text
%LOCALAPPDATA%\Open-Flame\diagnostics\runtime-launch-diagnostics.jsonl
```

在文件资源管理器地址栏粘贴其目录即可定位。该路径独立于业务数据目录；日志为 UTF-8 JSON Lines，单文件上限 256 KiB，保留 3 份轮转备份。

- `diagnostic_status=saved`：本次诊断已写入；可按故障时间和错误码查找。
- `diagnostic_status=unavailable`：本次未保存，保留终端提示。旧日志文件存在不证明本次已经记录。
- `log_status=not_started`：业务运行日志尚未启动，不代表这份独立诊断未保存。

| Setup 错误码 | 位置 `failure_site` | 检查与下一步 |
|---|---|---|
| `setup_prerequisite_unavailable` | `setup_preflight` | 检查 Windows x64、外部 64 位 CPython 3.12+、完整普通源码目录；不要从目标 `.venv` 运行 Setup |
| `setup_environment_unavailable` | `setup_environment` | 本地环境不可用或不允许修改；保留已有内容，确认是否为 Setup 拥有的环境再考虑修复 |
| `setup_dependencies_failed` | `setup_dependencies` | 检查 PyPI 连接，或 wheelhouse 是否具备锁定哈希及兼容 wheel；不要改锁文件或关闭哈希检查来绕过 |
| `setup_toolchain_failed` | `setup_toolchain` | 工具获取、哈希、版本或离线 smoke 失败；核对 GitHub / 本地缓存，已有坏工具不会被自动替换 |
| `setup_busy` | `setup_lock` | 源码应用 / 安装器占用，或锁文件不可用；正常停止相关程序后重试；无占用时检查目录权限与锁文件状态 |
| `setup_interrupted` | `setup_install` | 已开始安装后取消；保留部分环境与业务数据，确认相关进程正常停止后可重试 |

参数不合法会给出 `invalid_arguments`，请用 `--help` 核对。确认前拒绝、EOF 或 Ctrl+C 返回 130，不写安装诊断；动作开始后取消会尝试记录 `setup_interrupted`。缺少可运行 Python 时无法记录；bootstrap 导入自身模块失败则给出 `setup_launcher_unavailable` 和 `diagnostic_status=unavailable`。

应用开始运行后的故障，应查看前端“运行日志”或业务目录下的 `data\logs\runtime-*.jsonl`，详见 [Runbook](RUNBOOK.md)。诊断不保存原始参数、外部异常、Cookie 或私有路径；分享前仍需检查，不要附带数据库、媒体、Cookie 或整个工具目录。

## 验收与发布边界

Setup 的 `ready` 只表示本次环境准备与工具离线检查完成；Start 的页面就绪、平台认证和真实下载需要分别确认。软件源码采用 [Apache-2.0](../LICENSE)，保留 [NOTICE](../NOTICE)；第三方工具与依赖仍遵循各自许可，参见 [第三方声明](../THIRD_PARTY_NOTICES.md)。安装入口不等于第三方离线 bundle 已获发布验收，也不改变这些许可义务。
