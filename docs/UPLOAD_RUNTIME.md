# 可选上传运行时

首批平台：Bilibili、抖音、视频号。当前安装与真实浏览器运行支持 Windows x64 / CPython 3.12；项目主环境仍可使用其自身支持的 Python 版本。下载器不需要安装本节组件。

## 安装

已通过 wheel 或 editable 安装 Open-Flame 时，在该 Python 环境中执行以下命令。`--python` 指向已有 **CPython 3.12 x64**，只用它创建新的 venv，不向它安装或升级依赖。`--root` 必须与应用的上传数据目录一致。

```powershell
python -m video_download_control.uploads.runtime_setup `
  --root "$env:LOCALAPPDATA\Open-Flame\video-download-control\data-uploads" `
  --python "C:\absolute\path\to\python312.exe"
```

普通源码 Setup 只安装依赖，不安装项目包。因此，源码用户需在仓库根目录的 PowerShell 临时加入 `src`，使用下面的完整命令。如果该 `.venv` 不是 CPython 3.12，把 `--python` 后的路径替换为另一份已有 CPython 3.12 x64 的绝对路径；主应用环境不需要降级。

```powershell
$uploadPreviousPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = (Resolve-Path .\src).Path
  & .\.venv\Scripts\python.exe -m video_download_control.uploads.runtime_setup `
    --root "$env:LOCALAPPDATA\Open-Flame\video-download-control\data-uploads" `
    --python (Resolve-Path .\.venv\Scripts\python.exe).Path
} finally {
  $env:PYTHONPATH = $uploadPreviousPythonPath
}
```

普通 Start 默认使用上述 LocalAppData 根目录；自定义应用数据路径时需使用对应的上传目录。

安装器显式下载固定归档和 hash 锁定的 wheel，建立独立 venv，下载由 Patchright 版本固定的 Chromium，运行三平台 `login/check/upload-video --help`、Bilibili `upload --help` 和纯本地页面的浏览器检查。**这些检查不会登录、上传或发布。**网页的状态检查不会隐式安装组件。

```powershell
python -m video_download_control.uploads.runtime_setup `
  --root "$env:LOCALAPPDATA\Open-Flame\video-download-control\data-uploads" --check
```

源码用户可在上面的 `try` 块内把 `--python ...` 改为 `--check`，保留临时 `PYTHONPATH` 和 `.venv` 解释器。`--check` 只检查已有运行时。重复安装会复核已有环境；不跟随 latest 更新。发现完整 manifest 已被改坏时会报告 `runtime_invalid_requires_new_root`，不会为未知修改重新签发通过标记。

## 固定来源

| 内容 | 固定标识 | SHA-256 |
| --- | --- | --- |
| [SAU 源码 ZIP](https://codeload.github.com/dreammis/social-auto-upload/zip/0012d2c355f88f683cc38dde2a2db209e14091bc) | commit `0012d2c355f88f683cc38dde2a2db209e14091bc` | `c647bfd86be8e9c35bd50dafd92b1a1e150a148891b5f856f3505ebe7fe6615c` |
| [biliup Windows x64](https://github.com/biliup/biliup/releases/download/v1.2.4/biliupR-v1.2.4-x86_64-windows.zip) | release `v1.2.4` | `cb5af47aeaffd63719c94fa354a4d1404dd8437b6cc215513ec4e6054177c93e` |

源码 ZIP 散列由本次下载实测；biliup 散列与 [GitHub release API](https://api.github.com/repos/biliup/biliup/releases/tags/v1.2.4) 的 asset digest 一致。Python 的版本、原始 PyPI metadata URL 和允许 wheel SHA-256 均保存在 [runtime-lock.json](../src/video_download_control/uploads/runtime-lock.json)，安装使用 `pip --require-hashes --only-binary=:all:`。

当前 lock 共 17 个包；浏览器驱动为 Patchright 1.58.2。相对 SAU 的 uv.lock 有明确差异：requests 固定为 2.32.5、idna 为 3.15、urllib3 为 2.7.0，避免沿用其旧版本；所有散列均来自所列 PyPI 官方版本 API。实际 CLI 导入发现上游 pyproject 漏列 Playwright：其余平台仍在 eager import 中使用该模块，因此补上 Playwright 1.58.0 作为兼容导入依赖。SAU 源码通过桥接使用，没有安装其过时的 package dependency metadata；没有引入上游旧 Flask Web 后端。

## 存储与并发

| 路径 | 内容 |
| --- | --- |
| `root/runtime/` | 固定源码、17 包独立 venv、biliup、Chromium、散列 manifest |
| `root/private/accounts/<platform>/<id>.json` | 独立上传账户状态；UI 不提供 Cookie 下载或原文 |
| `root/private/operations/` | 单次操作参数、临时浏览器 profile、有限结果文件；结束后清理 |

安装器持有 root 的 OS 独占文件锁；实际上传/登录持有共享运行锁。后台工作与安装不会并发修改同一运行时。Windows Job 同时用于安装命令和平台进程，取消、超时或应用退出会清理其所属进程树。

本机首次安装检测到 Chromium 145.0.7632.6 在应用存储路径下的 Windows SideBySide 错误 14001；同一组二进制在独立临时硬链接目录可启动。运行时因此建立临时的、只含固定浏览器字节的视图并显式指定 executable_path；不切换到用户日常 Chrome，不修改系统安装。浏览器 profile 仍写私有操作临时目录。硬链接不额外复制整套浏览器；不支持硬链接的文件系统回退为临时复制，结束后清理。

Bilibili 的 Known Folder checkpoint 不完全服从环境变量；桥接使用唯一的单次媒体路径并只清理该次操作对应的新 checkpoint，防止跨账号恢复旧上传。应用不会将平台日志、请求正文或凭据返回到 HTTP 客户端。

## 页面内扫码登录

三平台的登录按钮都使用独立进程向本页传回 PNG 和固定状态，不打开 Bilibili 终端或独立登录窗口。二维码由临时操作文件传递到服务内存；不写入账号数据库、账户文件或应用日志。读取二维码的 GET 同样要求当前页面 nonce，取消/过期/完成后不可再读取。跨实例不能读取另一执行实例的内存二维码。

Bilibili 使用已有 Segno 生成官方 TV 登录二维码，保存的真实 app tokens 与 biliup `BiliTV` 格式兼容；手机授权页可能显示 TV 客户端。抖音和视频号使用固定 Chromium 的全新 headless context，仅截取可信平台页面/登录 iframe 的二维码元素。没有新增运行包或安装依赖。真实扫码、额外验证和登录后权限仍需用户验收。

## 状态解释与当前验证范围

- `ready/account_ready`：账户检查或登录流程通过，不代表所有投稿权限均已验证。
- `submitted/upstream_submitted`：上游工具报告提交完成，不等于审核通过或已经公开。
- `draft_saved/upstream_draft_saved`：视频号保存草稿后进入明确同源列表路由。
- `unknown`：执行中断、平台返回缺少明确确认、取消或超时；先检查平台后台，再决定是否创建新任务。不会自动重发。
- `runtime_missing`、`runtime_invalid`、`runtime_busy`：未安装、完整性检查失败或安装正在占用该环境。

2026-09-04 实测：独立 CPython 3.12 环境安装成功，九个 SAU CLI help、biliup upload help、同一固定浏览器的本地 DOM smoke 通过。边界测试包括 Cookie/路径不回显、缺账号不隐式登录、转载来源参数、发布单次点击、严格视频号回执、取消与超时终止真实 Windows 子孙进程。2026-09-05 补充：三平台真实二维码获取通过；用户已完成 Bilibili 扫码，随后账号检查返回 ready，本地导入、草稿创建、取消与重建通过。抖音和视频号真实扫码认证，以及三平台真实媒体传输、投稿和平台结果均未验收，交由外部测试员按 [测试计划](UPLOADER_TEST_PLAN.md) 执行。组件安装、账号检查与真实上传验收分开记录。
