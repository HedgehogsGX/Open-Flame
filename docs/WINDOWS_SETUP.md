# Windows 源码版：首次安装与修复

本流程在源码目录准备本地 Python 环境和锁定媒体工具，也可通过同一个入口安装可选上传 runtime，或从用户已下载的固定 CPython 归档构建可选 AI runtime，之后可独立于 Codex 使用；不要求安装 uv。它仍需要系统 Python，**不是免 Python 的 EXE，也不是已发布的第三方离线工具合集**。

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

只准备核心 `.venv` 与媒体工具并需要两部分都离线时，同时传入两个**已存在的绝对目录**。缓存缺项、哈希不匹配或 wheel 不兼容会失败，不会悄悄回退联网：

```powershell
.\Setup-Open-Flame.cmd --yes --wheelhouse 'C:\OpenFlameCache\wheels' --artifact-cache 'C:\OpenFlameCache\tool-archives'
```

上述为示例路径，不会自动为你准备离线缓存。依赖安装使用 `--no-index --find-links`（指定 wheelhouse 时）、`--only-binary` 和 `--require-hashes`；这些参数含义见 [pip 官方安装文档](https://pip.pypa.io/en/stable/cli/pip_install/)。`--wheelhouse` 与 `--artifact-cache` 不控制可选上传 runtime 自身的固定下载；当前源码 Setup 尚未为这部分提供完整离线 cache 参数。本流程不验证平台登录或下载真实媒体。

### 可选上传 runtime

需要 Bilibili、抖音或视频号登录与投稿时，普通源码用户优先让同一个 Setup 调用现有上传 runtime 安装器：

```powershell
.\Setup-Open-Flame.cmd --yes --upload-runtime
```

Setup 默认用刚刚核验的项目 `.venv` 建立上传 runtime；该解释器必须是 **CPython 3.12 x64**。如果核心 `.venv` 来自 3.13，可另行指定已有的 3.12 x64 解释器。这个解释器只用于创建隔离上传 venv，不会被安装或升级依赖：

```powershell
.\Setup-Open-Flame.cmd --yes --upload-runtime `
  --upload-python 'C:\Python312\python.exe'
```

默认目标精确为 `%LOCALAPPDATA%\Open-Flame\video-download-control\data-uploads\runtime`。自定义 Start 根时，只传一次相同的 `--app-root`；Setup 会从它派生 `data-uploads`，不接受另一个上传根：

```powershell
.\Setup-Open-Flame.cmd --yes --upload-runtime `
  --app-root 'D:\Open-Flame\video-download-control'
```

`--upload-python` 只允许与 `--upload-runtime` 同时使用。缺失 runtime 会由既有 `uploads.runtime_setup` 下载并核验固定 SAU、biliup、hash-locked wheels 和 Chromium，再执行 CLI help 与纯本地浏览器 smoke；不会登录、扫码、上传或发布。已有当前 runtime 会完整复核并只读复用。旧 Schema 1、损坏或含非允许内容的部分 runtime 会以 `setup_upload_runtime_failed` 停止，绝不覆盖或修改原目录；空 runtime，或只含允许且散列匹配的一个或两个固定归档的预置目录，可以继续构建。需重建时按[上传运行环境的升级步骤](UPLOAD_RUNTIME.md#从旧运行时升级)只保留并改名 `data-uploads\runtime` 后再重试。

当前上传安装器直接在缺失的 `runtime` 目录内构建，不提供原子 staging 发布；下载、创建 venv 或浏览器安装中断可能留下可识别的部分 runtime。若只留下空目录或散列匹配的固定归档缓存，后续运行可安全续建；其他部分内容失败关闭并要求保留或改名后重建。不要把 Setup 成功理解为账号登录或真实投稿验收。

### 可选 AI runtime

普通源码用户可让同一个 Setup 从本地 CPython **3.13.15 Windows x64 embeddable ZIP** 构建 AI runtime。Setup 不下载这份 ZIP，也不调用 OpenAI；它固定只接受 Python.org 已核验 SHA-256 `d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf`：

```powershell
.\Setup-Open-Flame.cmd --yes `
  --ai-python-embed-zip 'C:\Installers\python-3.13.15-embed-amd64.zip'
```

默认输出精确为 `%LOCALAPPDATA%\Open-Flame\video-download-control\data-ai-runtime`。若 Start 使用自定义应用根，Setup 必须传入同一个规范化绝对目录：

```powershell
.\Setup-Open-Flame.cmd --yes `
  --ai-python-embed-zip 'C:\Installers\python-3.13.15-embed-amd64.zip' `
  --app-root 'D:\Open-Flame\video-download-control'

.\Start-Open-Flame.cmd --app-root 'D:\Open-Flame\video-download-control'
```

`--app-root` 只允许在 `--ai-python-embed-zip` 或 `--upload-runtime` 至少一项被请求时用于 Setup。Setup 先准备原有 `.venv` 和媒体工具，再在 `<app-root>\data-ai-runtime` 缺失时核验 ZIP 并调用现有原子 AI builder；已有 AI runtime 只有在完整 manifest、全部文件、CPython 3.13.15 及当前 worker/protocol/provider 字节都匹配时才只读复用，复用时不读取 ZIP，也不把 runtime 自身当作原始归档来源证明。已有目录损坏或来自旧源码时会以 `setup_ai_runtime_failed` 停止，绝不删除、覆盖或改写 manifest；先正常停止应用，保留并将旧目录改名到尚不存在的备份名，再重试。更多来源、完整性和 provider 边界见 [AI runtime 指南](AI_RUNTIME.md)。

两项 runtime 可以在一次显式命令中准备，并共享同一个应用根：

```powershell
.\Setup-Open-Flame.cmd --yes --upload-runtime `
  --ai-python-embed-zip 'C:\Installers\python-3.13.15-embed-amd64.zip'
```

## 修复与已有数据

先在各自窗口正常停止应用和 Worker，再执行：

```powershell
.\Setup-Open-Flame.cmd --repair --yes
```

可同时带上述缓存参数。`--repair` 只重新安装 **Setup 自己创建并标记拥有的 `.venv`** 中的锁定依赖，不是清空或重建整个目录；损坏的 Python 可执行文件不保证能通过此参数修好。

- 已有但不属于 Setup 的环境：健康时可验证并复用；需要修改或显式 `--repair` 时拒绝覆盖。先保留原内容并检查，不要伪造 ownership 标记来强制修复。
- 已有但损坏的工具包：校验失败并停止；`--repair` 也不会覆盖。保留诊断与原目录，核对来源后再制定恢复方案，不要靠删除文件绕过校验。
- 已有但损坏或过期的 AI runtime：即使带 `--repair` 也只读拒绝，不自动修补或覆盖；保留或改名后再从固定 CPython 归档重新构建。
- 已有旧版、损坏或含非允许内容的部分上传 runtime：`--repair` 不会改写它；只保留并改名 `data-uploads\runtime`，账号、数据库、媒体和封面继续留在原上传根，再用 `--upload-runtime` 重建。空目录或只含允许且散列匹配的固定归档缓存可由安装器续建。
- 安装或修复不迁移、清空业务数据库、下载媒体或 Cookie 配置。默认业务目录仍为 `%LOCALAPPDATA%\Open-Flame\video-download-control`，不在仓库内。中断可能留下 Setup 拥有的部分环境，可在正常停止相关程序后重试。

源码 Start 在整个应用生命周期持有源码共享锁，Setup 在安装期间需要源码独占写锁，使用仓库根目录的 `.open-flame-setup.lock`。AI 与上传 runtime 安装都与普通 Start 复用目标应用根的 `.local-app.lock`；上传安装子进程还持有 `data-uploads` 的既有 runtime 独占锁。另一个源码、wheel、同根应用或上传维护进程正在占用时会立即停止；应用根或源码锁冲突返回 `setup_busy`，上传子锁冲突也映射为同一码。各锁都不会等待或强行关闭应用。**直接运行不采用普通 app-root 合同的高级 CLI 不受这些锁完整保护**，维护前必须自行正常停止。锁文件存在不等于有人持锁，不要通过删除锁文件解除占用。

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
| `setup_ai_runtime_failed` | `setup_ai_runtime` | 本地 CPython ZIP、目标路径或已有 runtime 校验失败；不会显示私有路径或覆盖目标，停止应用并按 AI runtime 指南保留/改名后重试 |
| `setup_upload_runtime_failed` | `setup_upload_runtime` | 上传解释器、固定下载、安装或完整性复核失败；不会回显 child 输出。先检查 CPython 3.12；若现有 runtime 为旧版、损坏或含非允许内容的部分目录，再按上传运行环境指南保留/改名 runtime 后重试 |
| `setup_busy` | `setup_lock` | 源码应用 / 安装器占用，或锁文件不可用；正常停止相关程序后重试；无占用时检查目录权限与锁文件状态 |
| `setup_interrupted` | `setup_install` | 已开始安装后取消；保留部分环境与业务数据，确认相关进程正常停止后可重试 |

参数不合法会给出 `invalid_arguments`，请用 `--help` 核对。确认前拒绝、EOF 或 Ctrl+C 返回 130，不写安装诊断；动作开始后取消会尝试记录 `setup_interrupted`。缺少可运行 Python 时无法记录；bootstrap 导入自身模块失败则给出 `setup_launcher_unavailable` 和 `diagnostic_status=unavailable`。

应用开始运行后的故障，应查看前端“运行日志”或业务目录下的 `data\logs\runtime-*.jsonl`，详见 [Runbook](RUNBOOK.md)。诊断不保存原始参数、外部异常、Cookie 或私有路径；分享前仍需检查，不要附带数据库、媒体、Cookie 或整个工具目录。

## 验收与发布边界

Setup 的 `ready` 表示本次环境、工具以及明确请求的上传/AI runtime 完整性检查完成；它不表示 API key、OpenAI 账号/模型、费用、听感、页面、平台认证或真实下载/上传已经通过。软件源码采用 [Apache-2.0](../LICENSE)，保留 [NOTICE](../NOTICE)；第三方工具与依赖仍遵循各自许可，参见 [第三方声明](../THIRD_PARTY_NOTICES.md)。安装入口不等于第三方离线 bundle 已获发布验收，也不改变这些许可义务。
