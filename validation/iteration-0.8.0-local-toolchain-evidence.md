# Iteration 0.8.0 本机工具链接入验收证据

> 许可历史说明：本文中的 proprietary/31-files 表述记录 0.8.0 当时的构建事实；项目自有材料已自 0.9.1 起由权利人改授 Apache-2.0，第三方 binary/container/tool-bundle 门禁不变。

> 日期：2026-09-03  
> 环境：Windows x64 / Python 3.12.13 / uv 0.11.25  
> 证据类型：本机固定工具链、离线 synthetic smoke、控制面/API/UI/日志与 Python 发布包工程验收  
> 非证据：真实平台下载、Linux 隔离 Worker、Docker/WSL、真实 Cookie、Stage 0、公开再分发许可

## 1. 结论

Iteration 0.8.0 已把固定版本的 yt-dlp、FFmpeg 与 ffprobe 安装到项目私有、gitignored 的 Windows x64 工具目录，并完成逐文件完整性校验、真实二进制版本/configuration 检查和无网络 synthetic 音视频闭环。控制面能以只读方式显示该状态，前端与脱敏运行日志也已接入。

当前 `state=ready` **只表示本机工具完整且离线 smoke 通过**。下列门禁仍为 `false`：

- `isolated_worker_ready=false`
- `network_download_enabled=false`
- `platform_download_verified=false`

因此，本轮没有把项目变成可真实联网下载的 standalone downloader。candidate Worker 仍要求 Linux network namespace/container、受控 egress、明确授权样本和 Stage 0 验收；Windows 控制面与已安装二进制本身不满足这些条件。

## 2. 固定工具与供应链记录

权威机器可读锁为 `src/video_download_control/toolchains/windows-x64.json`。本轮锁定：

| 组件 | 固定值 | 本机执行方式 / 许可记录 |
|---|---|---|
| yt-dlp | `2026.08.19` | 项目 Python 以 zipimport artifact 执行；`Unlicense AND MIT AND ISC` |
| FFmpeg | `n9.0.1-6-g9d4ca21220-20260820` | BtbN Windows x64 LGPL shared build；`LGPL-3.0-or-later` |
| ffprobe | `n9.0.1-6-g9d4ca21220-20260820` | 与上述 FFmpeg bundle 同源、同 configuration closure |

关键下载制品均固定 URL、字节数和 SHA-256。yt-dlp 的 release checksum、detached signature、source tarball 与许可文本也随本机安装保留；detached signature **仅保留、尚未完成密码学验签**。FFmpeg 只提取锁定的 `ffmpeg.exe`、`ffprobe.exe` 与所需 shared DLL/许可文本，拒绝未列出的文件和 `--enable-gpl` / `--enable-nonfree` configuration。

最终安全审查进一步验证并修复了四类边界：拒绝 Windows 盘符逃逸、ADS、设备名、尾点/空格以及 UNC/device namespace 工具根；把 `redistribution_status` 锁死为当前 blocked 值；在发出下一跳请求前逐跳校验 HTTPS、批准 host、无 userinfo 与默认 443 端口；并为单制品下载增加 600 秒总 deadline。对应 toolchain/CLI 聚焦回归为 `48 passed`。

工具安装位置记录为项目相对路径 `runtime-tools/windows-x64/`；该目录被 Git 排除，不写系统目录、不修改系统 `PATH`。下载缓存位于项目外的临时目录，具体用户路径不进入本证据。

## 3. install / verify / smoke / status

以下四个入口均已针对同一工具根实际执行并通过：

```powershell
uv run video-download-tools install --tool-root <project>\runtime-tools\windows-x64
uv run video-download-tools verify --tool-root <project>\runtime-tools\windows-x64
uv run video-download-tools smoke --tool-root <project>\runtime-tools\windows-x64
uv run video-download-tools status --tool-root <project>\runtime-tools\windows-x64
```

最终状态：

```text
state=ready
detail_code=ok
yt_dlp_version=2026.08.19
ffmpeg_version=n9.0.1-6-g9d4ca21220-20260820
ffprobe_version=n9.0.1-6-g9d4ca21220-20260820
offline_smoke_passed=true
isolated_worker_ready=false
platform_download_verified=false
network_download_enabled=false
redistribution_status=blocked_pending_third_party_source_and_notice_audit
```

`smoke` 使用本机 FFmpeg 生成 1 秒 synthetic 蓝色视频与 440 Hz 音频、封装为 MKV，再由 ffprobe 验证恰好存在一个视频流和一个音频流。该流程不访问任何媒体平台；`smoke-result.json` 是本机完成标记，不是数字签名或平台能力证明。

## 4. API、前端与运行日志验收

以 `VDC_TOOL_ROOT=<project>\runtime-tools\windows-x64` 启动 v0.8.0 控制面后，完成以下检查：

| 检查 | 结果 |
|---|---|
| `GET /health` | `status=ok`，版本 `0.8.0`，Worker 为 `not_started` |
| `GET /api/v1/operations/tools` | `state=ready`；三项版本与上表一致；offline smoke 为 true；隔离 Worker、网络下载、平台验证均为 false |
| Web UI | “本机工具链”卡片显示相同状态与安全边界；工具 ready 未被显示为 Worker 已启动或平台已验证 |
| 启动日志 | 产生 `toolchain.inspected` 事件，仅含白名单状态、detail code、版本与 smoke 布尔值 |
| 日志泄漏检查 | `write_failures=0`、`rejected_events=0`；事件不包含工具绝对路径、下载 URL、workspace 路径、argv 或异常原文 |

工具状态在应用启动时校验并缓存；API/UI 的“刷新”只刷新当前控制面展示，不会重新执行二进制、联网或改变 Worker gate。

## 5. 回归与发布包

| 检查 | 结果 |
|---|---|
| 全套 pytest | `846 passed, 8 skipped` |
| `uv lock --check` | 通过；24 packages |
| `compileall` | 通过 |
| `uv build` | 生成 v0.8.0 sdist 与 wheel；wheel 含工具链锁、10 个 console entry points 和 31 个法律文件 |
| 隔离 wheel smoke | `validation/local/wheel-smoke-0.8.0-r3/` 从最终重建 wheel 安装通过：metadata `0.8.0`、`LicenseRef-Proprietary`、10 个 scripts、工具锁可加载；`video-download-tools status` 对未安装工具根 fail closed 并完整输出安全门禁 |

第一次隔离安装目录 `validation/local/wheel-smoke-0.8.0/` 暴露了 uv cache hardlink 与包内 immutable lock 检查之间的兼容性问题。修复后的 `r2` 证明该问题关闭；最终安全补丁和制品重建后又以 `wheel-smoke-0.8.0-r3/` 从最终 wheel 重新安装验证。旧目录只保留为失败历史，不是当前通过证据。

当前发布制品 SHA-256：

```text
F291BB68347950AC98FC620196F1964D9824AC5AFBE8E4EAA2C6BE761F7A2834  video_download_control-0.8.0.tar.gz
F6AD900D33BC10714BE9AF6CB87556E1DCCC8FD84DB5E436543E005AFAE17F28  video_download_control-0.8.0-py3-none-any.whl
```

最终交付哈希仍以 `dist/SHA256SUMS.txt` 为准；若后续重建制品，必须同步更新本记录或把本表标为历史。

8 个 skip 仍是明示的目标环境边界：1 个 POSIX Cookie directory-FD cleanup、4 个 root POSIX/getfacl source metadata contract、1 个真实 AF_UNIX roundtrip、2 个 POSIX path-swap/permission test。它们必须在目标 Linux 重跑，不能当作通过。

## 6. 许可与再分发门禁

- 项目自有代码仍为 `LicenseRef-Proprietary`，不是开源发布。
- 本机 bootstrap 使用的第三方组件已记录固定版本、来源、hash 与本机许可文本，但完整性校验不替代法律判断。
- `redistribution_status=blocked_pending_third_party_source_and_notice_audit` 保持不变。
- 在公开源码、分发 wheel/portable bundle、容器或安装器前，仍需权利人选择项目许可证，并完成第三方 notices、对应源码/build closure、SBOM、目标架构依赖与人工法律复核。

因此，“本机私有安装可复验”和“允许公开再分发”是两个独立结论；本轮只完成前者。

## 7. 下一验收门

要让软件成为可真实使用的 standalone downloader，下一步至少需要：

1. 在明确授权后准备可审计的 Linux/WSL 或 Docker 隔离运行时，并执行 candidate Worker 的 namespace、UDS relay、egress、资源与退出恢复验收。
2. 使用 synthetic Cookie 和 deny-only policy 先完成 Linux acceptance；真实 Cookie 继续保持未挂载。
3. 只对用户确认有权使用的样本执行 Stage 0；每个精确 evidence identity 满足正向/负向和连续三轮门槛后，平台能力才可从 `candidate` 改为 `verified`。
4. 公开发布前单独完成项目许可证选择与第三方再分发 closure。
