# Workflow 来源字幕优先复用

验证日期：2026-09-11（Australia/Adelaide）

## 结论

当前源码为自动 Workflow 增加了可选的 `ai.transcription_mode =
"prefer_source_caption"`。开启后，流程会从下载视频所绑定的 ready caption 中确定性选择一份
SRT 或 WebVTT，经过受管路径、文件身份、2 MiB 上限和 SHA-256 复核后，导入为待审核的
transcription timeline。字幕批准后直接进入既有 translation → dubbing → render → upload
链路，不创建 transcribe AI task；没有合适字幕或字幕内容无法安全采用时，才使用本次已冻结
授权的 AI 听写能力。

首次导入始终停在来源字幕审核，即使 Workflow 已勾选自动确认编辑也不会代替操作者批准未知
来源字幕。后台推进只观察审核结果；生产 Workflow 页面引导操作者在 Editing 页批准或拒绝，
服务/API 也只允许在首次停下后的单独显式确认中批准。批准后才继续翻译，拒绝后下一次推进
才建立 AI 听写回退。

这个切片复用了 Download Schema 11 的 `artifacts/captions`、Editing Schema 4 的
`timeline_revisions/requests` 和 Workflow profile/preset 合同。没有新增数据库 Schema、后台
服务、runtime、provider、第三方包或通用编排层。

## 数据与信任边界

- `BatchRepository.list_ready_captions_for_asset()` 只返回精确 source asset 下、具有唯一匹配
  original、ready download job、`caption.status = ready` 的登记字幕，同时保留 artifact path、
  MIME、language、SHA-256、origin 与 downloader 名称/版本。
- Workflow 先按 editing project 中持久化的 `source_asset_id` 取得候选，再按 artifact ID 读取
  唯一入选文件。候选选择后，path、MIME、language、hash、origin、tool 或 asset/artifact
  identity 发生变化都会失败关闭。
- 读取沿用 `_registered_auxiliary_payload()` 的无链接路径与 opened-file identity 校验；Workflow
  字幕在完整读取前把通用辅助文件上限收紧为 `MAX_SUBTITLE_BYTES`（2 MiB）。
- Editing 再次计算 payload SHA-256、核对 project/source asset 绑定，严格解析 UTF-8 SRT/VTT，
  并以稳定 request key 幂等写入不可变 transcription revision。`provider = download`，`model`
  保存完整 artifact UUID 与 SHA-256。
- `origin = platform` 只说明字幕来自平台下载链。当前下载元数据不能区分人工字幕和平台自动
  字幕，也不能证明语言、文字或时间轴准确，所以 revision 初始始终为 `review`；生产页面明确
  提示“平台返回字幕的人工或自动来源未知”。

## 选择、分段与回退

- 首版只采用 `application/x-subrip` 和 `text/vtt`。ASS、JSON3、TTML 等仍可作为下载辅助
  文件保留，但不进入自动编辑。
- 显式 `en` 优先精确 `en`；显式 `zh-CN` 按 `zh-CN → zh-Hans → zh`；不会把
  `zh-Hant` 当作简体中文。
- source language 为 `auto` 时，目标中文优先 English，目标 English 按
  `zh-CN → zh-Hans → zh`；同等级存在多个候选时视为不唯一并回退 AI。
- 连续分段只保留完全落在选定分段内的 cues，保留原绝对媒体时间并重新连续编号。任何 cue
  横跨外层或内部任一分段边界、过滤后为空、字幕无效、非 UTF-8、超过限制或含明显
  HTML/WebVTT/SSA 控制标记
  时回退 AI；不会裁剪句子或把控制标记交给翻译/配音。
- 下载登记、文件或幂等身份冲突不会回退，流程进入 attention。来源字幕被明确拒绝后，下一次
  advancement 才建立原有 AI transcription fallback。
- 一旦已建立 transcribe task，后续轮询会固定沿该 AI 回退链继续；即使候选列表随后变化，也
  不会中途改用来源字幕并遗留已创建的听写任务。
- 已批准来源字幕之后的 translation retry 继续绑定同一 parent revision，只建立 translation
  successor，不创建 transcribe task。重放先发现已导入 timeline，不重新读取已经成功导入的
  sidecar。

## 本地验证

以下结果只适用于本次工作树及本机 synthetic/offline 输入：

| 检查 | 结果 |
| --- | --- |
| `validate_source_caption_import.py` | PASS：严格解析、全部分段边界、hash、project 绑定、幂等与并发同键收敛 |
| `validate_workflow_source_caption_adapter_20260910.py` | PASS：首次显式推进仍要求审核、停下后的显式确认可批准、零 transcribe 复用、固定已开始的 AI 回退、translation-only retry、拒绝后回退、完整视频/连续分段、重放、真实 Download registry metadata |
| `validate_workflow_source_caption_service_20260911.py` | PASS：手动/自动后台 advance 都只观察来源字幕审核，并能发现 Editing 页的批准或拒绝；不覆盖通用显式 `confirm_ai` |
| `validate_workflow_source_caption_profile_20260910.py` | PASS：省略时旧 canonical JSON 不变、严格 mode、preset 保存/物化、旧 Workflow/preset schema 拒绝未来字段 |
| `validate_downloaded_source_cover.py` 扩展本地探针 | PASS：真实 Worker caption 登记、asset catalog、API resolver 字节/hash、2 MiB 上限 |
| preset / boundary / API validators | PASS |
| production Workflow Playwright preset validator | PASS：控件保存、恢复、确认失效与请求 payload |
| Workflow AI authorization、ledger recovery、multi-segment、restart、no-AI validators | PASS |
| `test_worker_auxiliary_artifacts.py` | 12 passed |
| `test_editing_ai_contracts.py test_editing_service.py` | 22 passed |
| `test_local_app.py test_upload_platform_parameters.py` | 133 passed |
| `compileall`、JavaScript/browser 检查、`git diff --check` | PASS |

合计 **167 项与改动直接相关的现有 pytest 回归通过**。没有新增或修改 tracked `tests/`
文件；新增验证器和截图只位于已忽略的 `validation/local/`。

另行尝试的冻结 `test_asset_access_api.py` / `test_artifact_resilience.py` 组合仍因其未采用当前
下载 CSRF 与 claim-gate 合同而失败（25 failed、18 passed）；这与 HANDOFF 中记录的 hosted/full
pytest 历史测试漂移一致。本轮没有修改测试或放宽当前安全合同来制造绿色结果。

## 尚未证明

本记录没有真实 OpenAI key、真实平台账号或获授权媒体，因此不证明来源字幕在 Bilibili、抖音
或视频号样本上一定存在或准确，也不证明 AI 翻译、配音质量、平台字段接受、上传、审核、定时
执行或公开可见。视频号仍没有 pinned yt-dlp 专用 extractor。真实结果必须绑定精确 commit、
平台、来源类型、adapter/downloader 版本与环境分别记录。
