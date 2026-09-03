# Stage 0 validation workspace

This directory contains Stage 0 schemas/templates plus explicitly labeled
engineering evidence. Raw current-platform samples, logs and media remain under
gitignored `validation/local/`; the summaries here are not by themselves evidence
that an entire platform is supported. Iteration 0.6's offline
Schema 8/graph record is [separate from Stage 0](iteration-0.6-graph-v2-offline-evidence.md),
as is Iteration 0.7's [short-link, Cookie and Linux acceptance engineering
record](iteration-0.7-short-link-cookie-linux-offline-evidence.md). Iteration 0.7.1 的
[前端、完整回归、使用与许可证证据](iteration-0.7.1-debug-use-license-evidence.md)
同样只包含本机 synthetic/offline 工程验收；它不是 Stage 0、真实平台、Linux/Docker
运行或公开发布许可的证明。Iteration 0.7.2 的
[脱敏运行日志、调试与使用验收证据](iteration-0.7.2-runtime-logging-evidence.md)
进一步覆盖控制端/Worker/子进程日志、前端查看、泄漏标记扫描和备份排除，但仍受同样边界约束。
Iteration 0.8.0 的
[Windows x64 本机工具链接入验收证据](iteration-0.8.0-local-toolchain-evidence.md)
记录固定 yt-dlp/FFmpeg/ffprobe、离线 synthetic smoke、API/UI/日志与发布包回归；其中
`ready` 仅表示本机工具完整，**不表示**隔离 Worker、联网下载、真实平台或 standalone downloader 已完成。
Iteration 0.8.1 的
[真实平台双样本验收证据](iteration-0.8.1-live-platform-evidence.md)
记录用户明确授权的一个 YouTube URL 和一个 X URL 的无 Cookie 探测、真实下载、
Node.js A/B、ffprobe、SHA-256 与完整解码；它只证明这两个样本在记录时的本机结果，
不代表整个平台受支持，也不满足 Stage 0 的样本量与连续运行门槛。
Iteration 0.9.0 的
[Windows 本机 Worker 全链路验收证据](iteration-0.9.0-local-worker-e2e-evidence.md)
继续用同两个精确输入覆盖 API 队列、独立 Worker、AssetStore/manifest、成品下载 API、
浏览器 UI 与结构化日志。该结果仍只证明精确样本和记录时的 Windows 环境，不会把
平台能力升级为 `verified`，也不构成 Linux/Docker 隔离证明。

Version 0.9.1 的
[Apache-2.0 许可证迁移与公开发布证据](apache-2.0-license-migration-evidence.md)
记录权利人决议、版本隔离、包元数据、法律文件、隐私扫描与回归结果。0.7.1–0.9.0
文档中的 `LicenseRef-Proprietary` 和 31-files 描述是当时构建的 point-in-time 历史事实；
0.9.1 只在项目自有源码许可范围取代它们，不解除第三方 binary/container/tool-bundle
发布门禁。

Iteration 0.10.0 的
[六平台下载能力与 Worker 路由工程证据](iteration-0.10.0-multiplatform-routing-evidence.md)
记录 Bilibili、Douyin、TikTok、Instagram 的窄范围 URL、声明式能力矩阵、精确
Worker claim 过滤、离线 fake 资产链、前端、打包、隐私和许可证复核。该工程记录本身
没有执行真实媒体请求；同轮稍后的
[Instagram 单样本本机全链路证据](iteration-0.10.0-instagram-live-evidence.md)
记录一条 NASA 官方公开 Reel 在全新 data root、Windows direct/no-cookie/Node 下达到
`1/1 ready`，并覆盖 DB、manifest/API copy、完整解码、清理、JSONL 日志和浏览器 UI。
Bilibili 真实尝试遇 HTTP 412；Douyin 官方宣传样本要求 fresh cookies，未提供 Cookie 时
正确归类为 `authentication_required`；TikTok 未实跑。单样本不是 Stage 0，全部能力仍为
`candidate`。

1. Copy `sample_manifest.template.csv` to a private location outside Git.
2. Add at least 10 current, public, browser-accessible positive samples for every `platform × source_type` cell, plus separate negative samples.
3. Record each execution in a results CSV using `results.template.csv`.
4. Generate a URL-redacted report:

```powershell
uv run video-download-validation C:\private\samples.csv `
  --results C:\private\results.csv `
  --output C:\private\capability-report.md
```

A cell becomes `verified` only when the manifest has at least 10 public positives, negative evidence exists, and the latest three complete runs for the exact same `adapter × downloader_version × environment` each meet the positive success threshold while all negative results match their expected terminal outcome. Runs that mix those identity fields are rejected. The report contains URL SHA-256 values, not raw URLs.

The tool evaluates recorded evidence; it does not launch yt-dlp and cannot create real validation evidence by itself.
