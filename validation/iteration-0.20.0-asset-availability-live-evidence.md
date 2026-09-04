# Iteration 0.20.0 — 成品可用性与实际运行

日期：2026-09-04。本轮工程验证完成；未重新打包、提交或 push，不构成发布完成证明。数据库 Schema 11、固定下载工具与依赖版本不变。

## 已复现与修复

- 旧查询仅按新 batch_id 取资产，导致重复批次无法访问已下载文件。现沿真实 duplicate input 引用只读追溯原 owner，递归 UNION 防环去重，且校验 source 身份；仍要求 Job/Asset ready，不从同 source 的后来任务兜底。
- 页面以整个批次 ready/partial_success 为显示门槛，隐藏了混合批次已完成的文件。现直接查询资产，并在刷新期间保留现有链接。
- 重复批次没有新 Job，因此不增加无限定时轮询；“刷新成品”用于原 owner 后来完成的情况。复用不创建任务、不改原凭证/状态/资产。迟到响应继续受 generation/requestId 保护。

## 验证记录

后端新增 13 项边界测试，相关回归 117 项通过。前端使用整段真实页面 JavaScript、DOM fixture 和 Node 执行用户提交/切批次/刷新操作。初次完整回归 `1433 passed, 8 skipped in 142.01s`；8 项为当前 Windows 无法执行的 POSIX/Unix socket/ACL/文件替换检查。编译、离线依赖锁、diff whitespace 及 36 份 Markdown 链接检查通过。

独立审查另复现：同一批次的资产列表响应持续慢于 2 秒轮询时，后发请求不断作废先前成功响应。单飞修复仅合并同 batch/generation 的在途请求，完成后不缓存；保留切批次保护。最终前端为 13 项完整 JS 回归，相关 focused suite 为 52 passed。

最终完整回归：**1436 passed, 8 skipped in 146.28s**。跳过原因同上，没有将跳过项视为通过。最终编译、offline lock、diff whitespace 和 Markdown 链接检查通过。

## 最终构建重启与成品复用

在上述慢响应修复之后冻结源码，重新启动既有测试应用目录。完整 product identity 保存在本地最终报告中，且与运行中的 capability API 一致；运行期间保持不变。没有重新访问媒体平台或新增下载工作。

- 提交相同两条规范 URL，返回一个新 duplicate 批次、两个 duplicate Input、jobs=[]；其两份资产列表与原 owner 批次完全相同。
- 再次检查两份原件的 DB/manifest/API size 与 SHA-256、ffprobe 和完整解码；通过。
- 原 owner 批次、credential_profiles、download_jobs、job_attempts 和 media_assets 的数量及内容保持不变。new_jobs/new_attempts/new_media_assets 均为 0，仅新增一个重复批次及其输入。
- 当前 control API 日志 status=ok，写入失败/拒绝事件均 0。三个进程共享一个新的 run，正常停止，claim gate stopped，进程残留 0，端口释放。
- 最终复用验证 `passed`，30.766 秒。这证明最终构建能启动并使用既有成品，不将复用记为新的平台下载成功。

## 发布与隐私检查

Git 可发布清单扫描 236 个文件：运行媒体/数据库/日志/密钥类文件 0；已知真实用户名/微信标识/历史样本 ID 和 7 类高置信秘密模式均无命中。15 处 Windows Users 路径模式均已分类为 14 处 synthetic 测试路径及 1 处历史扫描说明，无未分类项；此扫描不构成对未知秘密的绝对保证。精确样本、真实运行数据与辅助测试脚本均位于忽略目录。

项目许可仍为 Apache-2.0，NOTICE 再次核对一致；没有新增依赖或第三方二进制。未生成本轮发布包、提交或 push，第三方二进制再分发门禁不变。

## 真实一体化应用：两个样本

使用用户给定 YouTube 视频与公开 Bilibili 动画短片；Windows 本机固定工具、匿名、默认 JS runtime（不附加 `--js-runtime`），从实际正常 `local-app` 的 supervisor/control/Worker 启动与 API 提交路径运行。只停止测试拥有的进程。

| 平台 | 结果 | 尝试 |
|---|---|---|
| YouTube | ready；一份原件 | 第 1 次成功 |
| Bilibili | ready；一份原件 | 第 1 次 rate_limited，产品自身冷却/重试后第 2 次成功 |

主测试 `passed`，316.047 秒；2 个 ready Job、3 次 Attempt、2 份原件通过 DB/manifest/API size 与 SHA-256 一致性、ffprobe 与 FFmpeg 全解码。三个应用进程共享同一 run，exit 0，graceful shutdown，无强杀、无残留、端口释放、Cookie profile/绑定/副本均无。媒体请求期间源码 identity 未改变；该早期报告只保存一致性布尔值，没有持久化完整 build hash，因此不可用于 Stage 0 精确构建审批，亦不把它改标成随后 UI 修复的最终构建成绩。

最终磁盘汇总：同一 Worker 的两个执行线程，Attempt 活跃区间重叠 12.444 秒，downloading 阶段重叠 4.615 秒，单平台峰值均 1。Bilibili 成品为 634.533 秒、1080p H.264/AAC；YouTube 为 180.061 秒、1080p AV1/Opus。两份均有音视频流。control/app/worker 分别 592/10/57 条 JSONL，均无序列缺号、重复或已持久化拒绝事件。

真实浏览器在 Bilibili 仍 downloading 时已显示 YouTube 原件/缩略图；再次提交 YouTube 得到 duplicate/jobs=[]，显示同一成品，手动刷新后仍可用。临时标签页已关闭。control 运行日志 API 实测 status=ok、write_failures=0、rejected_events=0、last_failure=none；此计数不扩大解释成每个进程所有 logger 的内存状态。

一个额外 partial 验证脚本在主测试自动停止后再次访问 API，收到 connection refused；它不计入通过结果，不推翻已经完成两份成品验证的主报告。精确 URL、ID、文件指纹与本地运行路径仅保存在忽略目录，不进入本公开汇总。

首次实际运行测试在下载提交前启动超时；不是平台失败，也不是下载成功。独立离线复现定位到测试辅助脚本的阻塞 stdin 读取妨碍 Windows multiprocessing 启动；不改生产逻辑，仅修正测试停止桥接后重新测试。真实 CLI 本身没有 stdin-EOF 停止接口。

## 边界

本轮不读取真实 Cookie、不绕过平台登录或访问控制，不更新第三方工具，不升级平台 capability，不执行 Linux/Docker/安装器验收或第三方二进制再分发。项目许可保持 Apache-2.0，NOTICE 保留 Copyright 2026 HedgehogsGX & Cyaegha_Xu。下载媒体、精确样本配置和运行数据仅留在 gitignored validation/local，不进入源码包。
