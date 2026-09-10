# Open-Flame 0.28.0 Workflow 来源封面偏好验证

日期：2026-09-11（Australia/Adelaide）

基线提交：`10c28a0`

状态：基线后的候选源码；本记录随同候选提交，尚无 release receipt

## 1. 目标与行为边界

本切片把下载域已经登记的来源 thumbnail 接入 URL Workflow 的上传准备阶段。它不改变下载
extractor，也不自动扩大平台或远端动作范围：

- `upload.prefer_download_cover` 是显式 opt-in；只有值为 `true` 时才进入 canonical profile 和
  preset。缺字段沿用生成封面。
- 选择来源封面时，`edit_recipe.cover` 仍为必填 fallback。生产页关闭“生成封面”会同时清除并
  禁用来源封面偏好。
- Workflow 只使用其已冻结的 `download_asset_id`。Download repository 在一个 SQLite 快照中
  返回 owner envelope 与所有声明为该资产 thumbnail 的潜在登记行；resolver 先验证 ready video
  asset、唯一 original、original SHA、ready job link、artifact owner/parent/kind/path，再做候选分流。
  这避免结构损坏的登记行被 SQL 过滤后误作“没有封面”。
- 可信 owner 下的 0 个 thumbnail 属于正常不可用，回退生成封面；多于 1 个完整候选失败关闭为
  `workflow_source_cover_ambiguous`。登记字段、受管路径或支持格式不可信时失败关闭为
  `workflow_source_cover_unavailable`，不会把完整性冲突当成正常 fallback。
- resolver 复核 canonical artifact/asset ID、thumbnail kind、受管路径、MIME、大小、扩展名和
  SHA-256 格式，只返回 `artifact_id / asset_id / path / sha256 / name`。`.jpe` 名称规范为
  `.jpeg`；接受 JPEG、PNG、WebP。
- resolver 会在 0/1/多候选分流前稳定读取并核对每个潜在 thumbnail 的实际 SHA-256；Upload
  再以 `inspect_managed_cover_import` 做无副作用图像解码和摘要复核，最终
  `import_cover(expected_sha256=...)` 仍重新读取实际字节。读取时已与登记摘要不符会在 resolver
  阶段失败关闭为 `workflow_source_cover_unavailable`；resolver 返回后至 Upload 复读前又发生的
  TOCTOU 漂移才保留 `cover_hash_mismatch` 等 Upload 精确错误，且不会转成正常 fallback。
- 来源封面只有同时兼容本次所有已选 Bilibili、抖音和视频号目标的 capability/封面槽时才会
  使用。任一目标不兼容时，整个 fan-out 回退同一生成封面，避免同一输出的账号使用不同封面。

## 2. 重放、分段与取消

来源封面的 Upload 受管 ID 由 `workflow_id + download_cover + artifact_id + sha256` 确定性派生；
生成封面继续由 `workflow_id + edit_cover + cover_id + sha256` 派生。上传准备先走纯读取的
`select_upload_cover`，Workflow 以 CAS 把结果写入既有 `upload_cover_id`，之后
`prepare_upload` 才允许导入封面、视频或创建 jobs。即使在任一调用后进程退出，重启也不会因
Download 当前状态变化而改选另一张封面。后续分段复用同一 checkpoint。

若 Upload jobs 已建立而 Workflow 尚未写回本段 source/job IDs，重放与取消都先按稳定请求键读取
不可变请求历史，从全部历史 jobs 的共同封面槽恢复最终 cover ID，再验证其属于本 workflow 的
确定性 Upload 副本。两条恢复路径只依赖该副本的元数据身份，不因本地封面字节已在终态后合法
删除而无法收回 job IDs 或停止远端任务。若当前分段尚无请求，取消会先原子占用稳定请求键并写入
专用 v2 空 tombstone；重启会直接识别并严格复核 sentinel digest，不再依赖可变 Editing 成品或
Download 登记，也不会遗漏前序分段 jobs。请求、tombstone 或封面元数据身份无法证明时停在
attention。

## 3. 架构处置

本切片遵循 2026-09-10 架构精简评审的核心判断：继续保持本地模块化单体。实现复用现有
Download repository/service、顶层组装处的窄只读 resolver、`LocalWorkflowAdapter` 与 Upload
受管导入，不复制 Download 状态机或 Upload 图像安全逻辑，也不增加通用 Workflow 引擎。

没有新增服务、数据库、表、列、trigger、Schema 版本、runtime、线程、队列、依赖或框架。
Workflow 已有 `upload_cover_id`、outputs、稳定请求键及 cancellation discovery 足以保存恢复语义。
本轮没有新增或修改 `tests/`；新增验证逻辑只在一次性临时数据库探针中运行。

涉及的生产文件：

- `src/video_download_control/repository.py`
- `src/video_download_control/service.py`
- `src/video_download_control/api.py`
- `src/video_download_control/uploads/backup.py`（同步 tombstone 恢复语义注释）
- `src/video_download_control/uploads/service.py`
- `src/video_download_control/workflows/contracts.py`
- `src/video_download_control/workflows/local_adapter.py`
- `src/video_download_control/workflows/presets.py`
- `src/video_download_control/workflows/profile.py`
- `src/video_download_control/workflows/service.py`
- `src/video_download_control/workflows/web.py`

## 4. 已执行验证

| 检查 | 当前结果 | 证明范围 |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe -m compileall -q src` | PASS | 当前 Python 源可编译；不证明业务或远端行为 |
| `.\.venv\Scripts\python.exe -m pytest -q tests/test_worker_auxiliary_artifacts.py tests/test_local_app.py tests/test_upload_service.py tests/test_upload_platform_parameters.py` | **185 passed** | 既有下载辅助资产、本地应用、Upload 服务和三平台参数聚焦回归；测试文件未改 |
| `.\.venv\Scripts\python.exe -m pytest -q tests/test_upload_service.py tests/test_upload_resilience.py` | **43 passed** | Upload 请求、恢复和媒体生命周期聚焦回归；测试文件未改 |
| 上述 185 项加 `tests/test_upload_resilience.py` 的并行审计复跑 | **187 passed, 1 failed**；失败项隔离复跑 **1 passed** | 唯一首次失败为 Windows handle 预算 `284 <= 283`，隔离复跑通过；保留偶发证据，不把组合写成全绿 |
| `validation/local/validate_workflow_source_cover_20260911.py` | PASS | 禁网、真实临时 Upload SQLite；覆盖两阶段零副作用选择、来源/回退、分段、三处 response-loss、封面媒体删除后的请求恢复、取消 tombstone 重启/篡改、hash/图像失败和 override 冲突 |
| 同一 validator 的临时 Download Schema 11 / `create_app` 矩阵 | PASS | 覆盖可信 0 / 1 / 多候选、manifest/caption 共存、`.jpe → .jpeg`、kind/path 伪装、artifact owner 漂移、坏 MIME/路径/hash 与缺失 ready owner |
| 四个 Workflow Chromium validator | PASS | preset、多分段、来源标题和生产下载/上传来源封面页面；含 light/dark、窄屏、200% 文字与 reduced motion |
| `.\.venv\Scripts\python.exe -m pytest -q tests/test_download_upload_integration.py` | **14 failed** | 旧集成用例在首个 batch POST 未提供当前 Download CSRF，统一得到 403；保留失败，未修改测试或弱化 guard |
| `git diff --check` | PASS | 当前完整 diff 无空白错误；clean commit 与 GitHub 签名在提交后另行核对 |

以上结果绑定当前可变工作区，不等于 clean candidate、完整仓库绿色或发行验收。root 汇总其他并行
validator 后，应只记录实际执行的最终命令与结果，不能把历史或其他提交的数量拼接为本轮通过数。

## 5. 未验证边界

- 没有用真实 Bilibili、Douyin 或其他 URL 执行下载提取，也没有把来源图片与平台可见封面进行
  对照；当前固定 yt-dlp 的单 thumbnail 选择也不保证取得发布者原始母版或 `origin_cover`。
- 没有执行真实 Bilibili、抖音或视频号登录、封面采用、草稿、投稿、审核、定时发布或公开可见
  验收。三平台 capability 的本地兼容判定不能替代平台后台事实。
- 视频号没有专用下载 extractor；本切片没有增加其 URL 来源封面能力。
- 没有执行真实 OpenAI 调用、真人试听或最终 clean release receipt。

后续外部验证应使用固定 clean commit、自有或已授权短片，分别记录下载 artifact、最终
`upload_cover_id`、各分段/账号 job、平台后台实际封面及远端状态；样本 URL、媒体、Cookie、
session、日志与本机路径留在 Git 外。
