# Iteration 0.12.0 — 辅助产物、TikTok 短链与 MVP 路由离线工程证据

> 日期：2026-09-03\
> 项目版本：`0.12.0`；数据库：Schema `9`\
> 证据等级：**synthetic/offline engineering evidence only**\
> 结论：辅助产物与短链安全契约已加固；本轮未执行任何真实平台请求，TikTok gate 默认关闭，六平台能力仍为 `candidate`

## 1. 本轮范围

Iteration 0.12.0 延续 Iteration 0.11.0 的 Schema 9 与 Stage 0 CSV v2，本轮不变更 schema，也不改写既有 0.11.0 证据。本文仅记录：

- ready asset 列表中的 thumbnail/caption 辅助产物 DTO；
- 独立的辅助产物下载端点与前端 DOM 链接；
- TikTok `vm.tiktok.com` / `vt.tiktok.com` 短链进入现有默认关闭 resolver 后的精确 hostname 策略；
- YouTube video/Shorts、Bilibili BV/av、受 gate 保护的 Douyin 短链以及 TXT/CSV 导入的 fake Worker 离线 E2E。

## 2. 辅助产物 API 契约

`GET /api/v1/batches/{batch_id}/assets` 只列出 ready Job 下的 ready original。每个资产的 `artifacts` 字段只接受数据库登记的 `thumbnail` 或 ready `caption`，并通过显式 DTO 返回：

`artifact_id`、`kind`、`mime_type`、`language`、`sha256`、`download_url`

列表不返回本机路径或用户标题；单个旧行不合契约时会对该 sidecar fail closed，不会隐藏同批次中的有效原件。前端使用 `document.createElement`、`textContent` 与 `replaceChildren` 构造原件/辅助下载链接，不使用 `innerHTML` 注入服务端字段。

`GET /api/v1/artifacts/{artifact_id}/download` 只服务 ready asset/Job 下登记的 thumbnail/caption，并在返回任何字节前重新验证：

- 规范小写 UUID，辅助类型及唯一、hash 匹配的 parent original；
- `assets/{asset_id}/thumbnails` 或 `assets/{asset_id}/captions` 的精确目录形状；
- 受限文件名、扩展名/MIME 对应、caption language 与非空有界大小；
- 非 symlink/reparse、regular file、single hard link，以及打开前后的 device/inode/size identity；
- 完整读取后的 SHA-256。

文件通过校验后写入内存上限为 1 MiB 的 `SpooledTemporaryFile`，较大内容会落到系统临时文件，再以 64 KiB 块发送；响应的 `Content-Length` 来自已校验字节数。响应对象在正常完成、ASGI disconnect/cancel 及 `send` 异常时都显式关闭 spool，避免并发大文件把完整内容长期留在进程内存或让临时句柄等待析构回收。

不存在、非 ready 或关系不满足时固定返回 404；已登记但文件边界失效时返回 409。成功响应使用固定 `artifact-{uuid}{suffix}` 文件名、`Cache-Control: private, no-store` 与 `X-Content-Type-Options: nosniff`；不使用用户标题。

## 3. TikTok 短链门禁

`vm.tiktok.com` 与 `vt.tiktok.com` 现在只会被规范化为 `tiktok / short_link`，不会被当成可直接下载的单视频 URL。`VDC_ENABLE_SHORT_LINK_RESOLUTION` 默认为 `0`；在默认配置下，提交 TikTok 短链会以 `short_link_resolution_required` fail closed，不会发起网络请求。

即使显式开启 gate，内建控制面 resolver 仍要求 POSIX 语义、绝对且私有的 UDS/key 路径和受证明的 transport。TikTok 每一跳只允许以下五个精确 hostname：

- `vm.tiktok.com`
- `vt.tiktok.com`
- `tiktok.com`
- `www.tiktok.com`
- `m.tiktok.com`

策略拒绝相似子域、后缀欺骗、HTTP 降级、URL 凭证、numeric host、非公网 DNS 结果与连接 peer 不匹配；每跳不自动跟随 redirect，并继续受跳数、header/body 和总时间上限约束。当前 short-link egress 尚未纳入 Compose/supervisor。

**本轮没有对 TikTok 或其他平台执行真实 DNS、TLS、HTTP redirect、probe 或 download 请求。** TikTok 相关测试使用 fake transport、固定公网形式的 synthetic IP 与占位 URL；它们只证明策略与控制流，不证明 TikTok 当前的 redirect 形状、可访问性或下载能力。

## 4. MVP 路由离线 E2E

自动化 E2E 使用每个测试独立的临时 data root/SQLite、`ScriptedFakeAdapter`、`NonEmptyTestVerifier` 与 synthetic bytes，覆盖：

- YouTube `watch` 视频与 `/shorts/{id}`；
- Bilibili BV 与 av，只接受默认分 P；
- 经显式短链 gate 与受信测试 resolver 转换的 Douyin 单作品；
- UTF-8 TXT、有表头 CSV 与无表头 CSV 导入。

每条路径都检查输入规范化、平台/source type 路由、持久化队列、fake Worker claim、ready 聚合、资产列表与原件下载内容。该 E2E 不启动 yt-dlp、FFmpeg/ffprobe、真实 short-link egress 或浏览器登录会话；占位 URL 不会被请求。

## 5. 能力、许可与隐私边界

- 六平台公开能力继续为 `candidate`；本轮不是 Stage 0，也不写入 `platform_capabilities` 或自动晋级 API/UI。
- 真实平台验收只能使用用户明确提供或确认有权使用的当前公开样本；还必须独立检查授权、版权、平台条款、Cookie/账号边界与当地法律。本轮没有获得或假设这些许可。
- 本文不包含用户的真实平台 URL、账号或 Cookie、token/签名 URL、运行 UUID、本机绝对路径、真实媒体、媒体 hash/指纹或原始平台响应。本轮定向测试中的 URL、IP 与 bytes 都是不发起网络请求的占位数据。
- 项目自有源码、文档与脚本继续采用 Apache-2.0，版权声明保持 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。
- dependency wheelhouse、冻结可执行文件、OCI/container image 与 yt-dlp/FFmpeg tool bundle 的第三方再分发门禁不变。本轮离线工程证据不是法律批准或发布授权。

## 6. 验证状态

| 验证项 | 当前状态 | 证据边界 |
|---|---|---|
| 辅助产物 DTO/下载/UI 与拒绝路径 | 已纳入本轮离线定向回归 | synthetic 产物，不含真实平台字节 |
| TikTok 规范化、默认 gate、精确 host 及 HTTPS/DNS/peer 拒绝 | 已纳入本轮离线定向回归 | fake transport/resolver，未触达 TikTok |
| YouTube/Bilibili/Douyin/TXT/CSV MVP 路由 E2E | 已纳入本轮离线定向回归 | 临时 DB + fake Worker + synthetic bytes |
| 离线定向 pytest | `160 passed in 14.08s` | Python 3.12.13；仅下列八个相关测试文件 |
| deployment/acceptance 静态定向 pytest | `28 passed, 4 skipped in 4.44s` | 4 个 skip 需要 root POSIX host + `getfacl`；不是 target Linux execute |
| 辅助产物稳健性复审 | `18 passed in 5.74s`；独立复审无剩余 P0/P1/P2 | 包含损坏 sidecar 隔离、>1 MiB 多帧响应、精确长度与 `send` 失败显式关句柄 |
| 最终全量 pytest | `969 passed, 8 skipped in 63.58s` | 8 个 skip 均为下述 POSIX/Linux 环境边界，不计为通过 |
| `compileall`、offline lock check、`git diff --check` | 通过 | Windows / Python 3.12.13 / uv 0.11.25 |
| 隔离 API/UI 使用验收 | 通过 | v0.12.0 / Schema 9；synthetic `1/1 ready`；原件、缩略图、字幕下载成功；console 0 warning/error |
| project-only package | sdist/wheel offline build 与隔离 wheel 安装通过；metadata/import `0.12.0` / `Apache-2.0`；11 个 console scripts；32 个 legal files | 最终 archive 由本次回填后的精确源码树重建并按下述契约复核 |
| source/package 内容与隐私 | 53 个 wheel 包源码文件逐字节匹配；193 个 publishable 源文件与 2 个 archive 的已知私有标记均为 0 hit | 检查已知真实样本 ID、私有消息标识和本机用户路径；无 DB/log/media/Cookie/key/`runtime-tools`/`validation/local` 包路径 |

成功的定向命令为：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_asset_access_api.py `
  tests/test_worker_auxiliary_artifacts.py `
  tests/test_short_links.py `
  tests/test_api.py `
  tests/test_batches.py `
  tests/test_normalization.py `
  tests/test_capabilities.py `
  tests/test_download_mvp_routes_e2e.py
```

deployment/acceptance 静态定向命令为：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_deployment_candidate.py `
  tests/test_linux_acceptance_assets.py
```

最终全量命令为：

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m pytest -q
uv lock --check --offline
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

8 个 skip 分别为：1 个 POSIX Cookie directory-FD cleanup、4 个要求 root POSIX + `getfacl` 的 source metadata contract、1 个当前 Windows 环境不可用的 AF_UNIX roundtrip、1 个 POSIX open-file replacement，以及 1 个 POSIX permission-bits 测试。它们必须在目标 Linux 执行，不能并入 969 个通过项。

最终 package 使用本节已回填的精确源码树再次离线构建；复核要求 wheel 的 53 个项目源码文件逐字节匹配、sdist 内本证据包含本轮最终回归数字、版本/许可/入口/法律文件满足上表且隐私扫描仍为 0 hit。制品 SHA-256 只保存在 gitignored 本机验收目录，避免把 sdist 自身 hash 写回 sdist 形成自引用。

## 7. 未完成项

- TikTok 首个授权真实样本与完整 Stage 0；
- 目标 Linux 上的 AF_UNIX、owner/mode、TLS/SNI、DNS rebinding、redirect chain、replay/restart 和 supervisor/Compose 集成；
- 真实 thumbnail/caption 内容的 API/UI 复验，以及 caption 为人工或自动的可靠来源区分；
- 六平台各 route-specific evidence identity 的至少 10 个授权公开正向样本、独立负向样本与连续三轮完整运行。
