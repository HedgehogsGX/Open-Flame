# Iteration 0.11.0 — Schema 9 与 Bilibili HTTP 412 工程证据

> 日期：2026-09-03\
> 范围：Stage 0 证据身份、Schema 8→9 前向迁移、Bilibili HTTP 412 诊断与错误语义\
> 结论：工程边界已收紧；Bilibili 下载能力仍为 `candidate`，不是 `verified`

## 1. Stage 0 CSV v2 与 Schema 9

Stage 0 的证据身份现在固定为：

`platform × source_type × job_kind × adapter × downloader_version × environment`

`job_kind` 只能是 `discover` 或 `download`。样本 CSV 与结果 CSV 都必须显式填写该字段，结果中的值还必须与对应样本一致。CSV v2 同时把原来的 media count 改为 route-neutral 的 output count：

- `download`：`output_count` 表示已发布且完成完整验证的媒体资产数；
- `discover`：`output_count` 表示写入不可变 discovery snapshot 的唯一 child/source item 数。

旧 CSV 表头缺少 `job_kind`，并继续使用 `expected_media_count` / `observed_media_count`，会被当前 evaluator fail closed 拒绝；不会静默推断为 `download`。相同 URL 可以分别成为 `discover` 与 `download` 样本，但同一 `job_kind` 内的重复 URL 仍会拒绝。

Schema 9 给 `platform_capabilities` 增加受约束的 `job_kind`，并把唯一证据索引升级为上述 route-specific identity。Schema 8 的既有能力行在迁移时保守映射为 `job_kind=download`。迁移保持 forward-only，readiness 会检查列约束和唯一索引的完整形状。

报告工具只读取私有样本/结果 CSV 并生成脱敏 Markdown；它不会运行下载器，不会写入 `platform_capabilities`，也不会自动把公开能力 API/UI 的静态 `candidate` 状态升级为 `verified`。

## 2. Schema 8→9 备份与回滚边界

当前 0.11.0 备份/恢复工具只接受精确 Schema 9。升级必须保留两个独立且实际恢复过的恢复点：

1. 迁移前使用 0.10.0 对精确 Schema 8 数据制作备份，并用同一 0.10.0 工具恢复到独立新根完成校验；
2. 在数据副本上由 0.11.0 执行 Schema 8→9 迁移，检查 readiness、SQLite quick/foreign-key check 和既有能力行的 `download` 映射；
3. 迁移后使用 0.11.0 制作精确 Schema 9 备份，并再次恢复到另一个独立新根。

0.11.0 不能替代迁移前的 Schema 8 恢复工具；0.10.0 也不得打开或恢复 Schema 9 数据库。自动降级、删除 migration marker 或原地重写新库都不属于支持路径。

## 3. Bilibili HTTP 412 诊断

诊断固定使用仓库批准的 yt-dlp `2026.08.19`、同一公开样本和无 Cookie 条件。精确输入 URL、运行标识、临时路径、媒体与内容指纹不进入本文件。

- raw yt-dlp probe：`2/2` 成功；
- 产品同款 fresh attempt：`6` 次中 `4` 次成功、`2` 次在 probe 阶段收到 HTTP 412。

同一工具、样本与无 Cookie 条件下出现交替结果，最符合“平台侧瞬时门禁”的工作假设；这只是依据当前观测的推断，不能证明具体风控机制、账号状态或 IP 原因，也不能排除平台后续行为变化。

产品修复只收紧错误语义：仅当平台已解析为 Bilibili 且 yt-dlp 输出包含受限的 HTTP/API 412 标记时，映射为 `rate_limited`，让现有 Worker 使用 60/120 秒退避和 platform cooldown。其他平台的 HTTP 412 不会被此规则吞并。实现没有添加 Cookie、伪造 header、代理或其他绕过，也没有因为偶发成功把 Bilibili 标记为 `verified`。

## 4. 能力、许可与隐私结论

- Bilibili 仍为 `candidate`；本轮重复诊断不是 Stage 0，也不证明稳定下载能力已修复。
- 完整 Stage 0 仍要求同一 route-specific evidence identity 下至少 10 个公开正向样本、独立负向样本和连续三轮完整运行达到门槛。
- 项目自有源码、文档和脚本继续使用 Apache-2.0，版权声明保持 `Copyright 2026 HedgehogsGX & Cyaegha_Xu`。
- dependency wheelhouse、冻结可执行文件、OCI/container image 和 yt-dlp/FFmpeg tool bundle 的第三方再分发门禁保持不变。
- 本文件不包含精确输入 URL、账号/Cookie、运行 UUID、绝对临时路径、媒体、媒体 hash 或签名请求数据。

## 5. 本轮验证

- Schema/migration/backup 与 Stage 0 CSV v2 定向回归：`78 passed, 4 skipped`；skip 为目标 Linux/root 环境边界。
- Bilibili 412 分类、retry/cooldown 与 repository 定向回归：`67 passed`；包括 probe/download、API `code -412`、其他平台隔离及版本探测不误分类。
- 最终全套 pytest：`935 passed, 8 skipped in 56.55s`；8 个 skip 均为必须在目标 Linux 重跑的 POSIX/root/AF_UNIX 环境边界。
- `compileall`、`uv lock --check --offline` 与 `git diff --check` 通过。
- 0.11.0 sdist/wheel 离线构建通过；wheel metadata 为 `0.11.0` 与 `Apache-2.0`，要求的项目法律材料存在。
- 对 tracked tree 与解包 sdist 的敏感标记检查未发现本轮真实输入、私有消息标识、本机用户路径、访问令牌或私钥；仓库未跟踪媒体、数据库、Cookie、日志或 JSONL 运行产物。
- 隔离本机实例的 health 返回版本 `0.11.0`、Schema `9`；能力 API/UI 显示 7 条 `candidate` download route；synthetic batch 创建/取消和日志刷新通过，浏览器控制台无 error/warn。该实例有意不配置工具根，未执行真实媒体下载。
