# Iteration 0.9.0 Windows 本机 Worker 全链路验收证据

> 许可历史说明：本文中的 proprietary/31-files 表述记录 0.9.0 当时的构建事实；项目自有材料已自 0.9.1 起由权利人改授 Apache-2.0，第三方 binary/container/tool-bundle 门禁不变。

> 执行日期：2026-09-03  
> 环境：Windows x64 / Python 3.12.13 / Node.js v24.16.0  
> 数据根：gitignored `validation/local/local-worker-e2e-20260903-v3/`  
> 证据类型：用户明确提供的两个 URL 经控制 API、持久化队列、独立本机 Worker、固定工具、AssetStore/manifest、成品 API 与浏览器 UI 的真实下载验收  
> 证据边界：每个平台只有一个精确样本；这不是完整 Stage 0、长期平台兼容性、Linux/Docker 隔离或公开发布许可证明

## 1. 验收输入与固定工具

用户明确提供本轮两个公开测试输入：

1. 一个 YouTube 视频（精确 URL 在公开记录中省略）
2. 一个 X 帖子视频（精确 URL、账号与状态标识在公开记录中省略）

本轮没有使用 Cookie、浏览器会话、netrc、DRM 绕过或访问控制规避。Worker 使用项目私有固定工具链：

| 组件 | 版本 |
|---|---|
| video-download-control | `0.9.0` / Schema `8` |
| yt-dlp | `2026.08.19` |
| FFmpeg / ffprobe | `n9.0.1-6-g9d4ca21220-20260820` |
| JavaScript runtime | Node.js `v24.16.0`，以受校验绝对路径显式传入 |

Windows 本机 Worker 必须同时满足环境变量 `VDC_ENABLE_LOCAL_REAL_WORKER=1` 与命令行 `--allow-direct-network`；它与控制面共享已初始化的数据库和数据根，并以 OS 文件锁限制同一数据根只有一个实例。`VDC_ENABLE_X_GRAPH_V2` 保持 `0`；本机 Worker 会在领取任务的同一事务内跳过当前不支持的 graph-v2 `discover` / `x_attachment` 工作。

## 2. 调试收敛过程

首次隔离运行暴露 yt-dlp `2026.08.19` 不支持 `--no-netrc`，两个 Job 均在探测阶段明确失败。移除该无效参数并增加命令契约回归后，第二次运行完成下载，但 YouTube 默认选择 HLS `616+251`，完整解码出现时间戳诊断。格式选择器随后改为优先非 HLS、再做有界 HLS 回退；最终运行选择非 HLS AV1 + Opus，完整解码无诊断。

最终命令契约还包括：每次调用恰好一个 `--proxy`；本机直连以 yt-dlp 明确支持的空 proxy 值禁用宿主代理继承；probe/download 固定 UTF-8；用户配置与远程组件保持关闭。

## 3. 最终队列与任务结果

最终隔离批次：

- Batch：本机验收批次（运行时 UUID 在公开记录中省略）
- 名称：`final-0.9.0-youtube-x-e2e`
- 状态：`ready`
- 汇总：`2/2 ready`、`0 failed`、`0 duplicate`、`0 canceled`
- X 输入已规范化为 canonical status URL；精确状态标识在公开记录中省略

| 样本 | Job | Attempt | 状态 | adapter / exit |
|---|---|---|---|---|
| YouTube | 运行时 UUID 已省略 | 运行时 UUID 已省略 | `ready` / `succeeded`，仅 1 次 Attempt | `yt_dlp 2026.08.19` / `0` |
| X | 运行时 UUID 已省略 | 运行时 UUID 已省略 | `ready` / `succeeded`，仅 1 次 Attempt | `yt_dlp 2026.08.19` / `0` |

## 4. 不可变资产与媒体验证

| 样本 | Asset / 原件 | 媒体 | 大小 | SHA-256 |
|---|---|---|---:|---|
| YouTube | runtime asset UUID / `original/source.mkv` | AV1 + Opus；媒体结构有效 | 与本机 manifest 一致 | 与本机 manifest 一致；公开记录省略 |
| X | runtime asset UUID / `original/source.mp4` | H.264；源本身无音轨 | 与本机 manifest 一致 | 与本机 manifest 一致；公开记录省略 |

两个 manifest 均记录 `worker_version=0.9.0`、`adapter=yt_dlp`、固定 adapter/verifier 版本。缩略图也作为受管 Artifact 登记，且 size/SHA-256 与本机 manifest 一致；公开记录省略内容指纹。

完整性复核结果：

- `PRAGMA quick_check=ok`，`foreign_key_check` 无记录。
- `asset_commit_intents=0`；`temporary` 与 `assets/.staging` 无残留。
- 数据库、manifest 与实际原件/缩略图的相对路径、字节数和 SHA-256 一致。
- 两个原件均用固定 FFmpeg 对全部 stream 完整解码至 null sink；`-xerror` exit `0`，诊断行数均为 `0`。

## 5. 成品 API、前端与日志

`GET /api/v1/batches/{batch_id}/assets` 返回两个 ready 原件。两个 `download_url` 均实际以 HTTP `200` 下载到隔离复核目录，下载副本 SHA-256 与 manifest 完全一致；`Content-Disposition` 只使用系统生成的 Asset UUID 和受控扩展名，不采用用户标题。

浏览器重新加载 v0.9.0 页面后，“最近批次”显示 `final-0.9.0-youtube-x-e2e · ready · 2/2 ready`。重新打开批次会显示两个“下载成品”链接；实际点击 X 链接触发浏览器下载并留在原页。该流程中浏览器 console 的 warning/error 数量为 `0`。

日志 API 返回 `status=ok`、`write_failures=0`、`rejected_events=0`。控制面与本机 Worker 使用独立 JSONL；Worker 记录 mandatory `worker.initializing`、toolchain、started、两个 ready `job_finished`、`drain_complete` 和 stopped 事件。日志采用字段白名单，不记录原始 URL、query、Cookie、argv/stdout/stderr 或绝对路径。启动首写是强制边界；启动后的持续写入仍为 best-effort，不能把日志当成不可缺失的审计账本。

## 6. 产品与发布边界

本轮证明这台 Windows 机器上的控制面和独立本机 Worker 已能作为单机软件处理这两个精确公开样本，并能从前端重新找到、下载成品。控制面仍不会自行启动 Worker，`/health` 对外部 Worker 只报告 `external_status_unknown`，这不是在线心跳。

项目自有代码仍为 `LicenseRef-Proprietary`，因此不能称为开源软件。第三方 notices 与许可文本会进入包，但公开/容器/二进制再分发仍按 policy 保持 blocked；目标 Linux、Docker、Cookie、完整 Stage 0 以及四个平台的更广泛样本均未完成。

## 7. 最终回归、安装包与许可证门禁

在所有 0.9.0 代码和文档定稿后执行：

| 检查 | 结果 |
|---|---|
| `compileall` | `src` 与 `tests` 通过 |
| 锁文件 | `uv lock --check --offline` 通过；项目版本为 `0.9.0` |
| 全量 pytest | `881 passed, 8 skipped in 49.45s`；8 项均为当前 Windows 不具备的明确 POSIX/Linux 条件 |
| 构建 | `video_download_control-0.9.0.tar.gz` 与 `video_download_control-0.9.0-py3-none-any.whl` 通过 `uv build` |
| 隔离安装 | 新建 Python 3.12.13 环境，从 0.9.0 wheel 安装通过；metadata 与 import 均为 `0.9.0` |
| 包内许可 | `License-Expression: LicenseRef-Proprietary`；31 个 `License-File`；11 个 console scripts，包含 `video-download-local-worker` |
| fail-closed smoke | 缺失工具根的 `status` 返回 `invalid/bundle_invalid` 且 exit `0`；`verify` 返回 error 且 exit `2`；本机 Worker `--help` 含 direct-network 与 JS runtime 明示参数 |
| 内容审计 | 13 个本轮关键源码模块与 wheel 逐字节 SHA-256 一致；wheel/sdist 都不含 `validation/local`、`runtime-tools`、SQLite、API 下载副本或原始媒体 |

最终制品哈希只记录在包外 `dist/SHA256SUMS.txt`，避免 sdist 文档对自身哈希形成循环。0.8.1 及更早制品仅保留为历史包，不得使用 `dist/*` 通配符发布。

许可证聚焦回归还补上一个实质完整性缺口：candidate tool manifest 现在必须给出 `sources/` 下的 `source_artifact_path`，validator 会实际读取非空普通 single-link 文件并核对 SHA-256，同时拒绝缺失、目录、越界、symlink/reparse 与 hardlink。该门禁仍只验证证据完整性；SBOM 目前只验证 hash 绑定的非空 JSON 形状，不能证明其组件语义完整，也不能替代权利人授权或法律复核。
