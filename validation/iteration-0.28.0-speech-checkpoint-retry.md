# Iteration 0.28.0 配音逐 cue 断点重试验证

> 日期：2026-09-11（Australia/Adelaide）
> 基线：`a300c95`；本记录绑定包含本文件的后续签名提交。
> 范围：本地 Editing/Workflow 数据库、render worker、合成 WAV 与生产页面文案。没有提供真实 OpenAI 凭据，也没有调用真实下载或平台上传。

## 结果

配音 render 在后续 cue 失败时会保留此前已完整验证的逐 cue WAV。操作者从 `failed`、`canceled` 或 `render_interrupted` 计划显式建立后继并再次确认后，worker 只重新调用缺失、损坏或不再满足轨道条件的 cue；有效 cue 直接从同一 retry lineage 的本地 checkpoint 读取。

该复用不会跳过远程结果核对。远程 checkpoint 的生产计划必须存在唯一、精确匹配 ordinal、request fingerprint 与 authorization SHA-256 的 `responded` invocation；`reserved`、`dispatched`、`unknown`、`accepted_without_result` 和 `abandoned` 仍按 Schema 4 规则阻止完成或重试。

## 身份、文件与恢复边界

- 谱系必须是无循环、无分叉的唯一链；当前计划不能已有后继，每个祖先必须为 `failed` 或 `canceled`，并且 project、draft version、完整 recipe/recipe SHA-256 与 `plan_timeline_bindings` 全部相同。
- cue request key 绑定批准时间轴中的全局 ordinal、ID、窗口化起止时间、文字、语言、speaker，以及 provider、model、音色、语言、语速和 authorization。远程 fingerprint 通过与实际执行共用的 synthesis payload builder 及 `validate_request()` 规范化，避免首尾空白导致预计算与账本漂移。
- 现有 `requests` 表保存每个生产计划/ordinal 的不可变 manifest，绑定 request key、fingerprint、authorization、execution 与实际 WAV SHA-256；不新增 Schema 或数据库表。
- checkpoint 加载通过一次有界读取建立不可变内存 snapshot，同时计算 SHA-256，并从同一 snapshot 校验 WAV header、声明 frame 数与完整 PCM 数据；实际写轨前再次 snapshot 并比对已验证 SHA。跨 cue 格式、总合成大小和当前时间槽也必须通过；不符合条件的旧缓存只重做对应 cue，不会形成永久失败循环。
- 写入先按已验证源文件的精确大小有界复制，额外读 1 byte 确认 EOF，再以 hard-link create-if-absent 发布。进程若停在 link 与临时名清理之间，下一次读取只会在临时名和目标都是同一普通 inode、各自 link count 为 2 时删除临时名；其他 link、symlink、reparse point、目录和未知名称会保留，但不会被跟随、读取、删除或当作音频使用。
- `ready` 数据库提交后删除整条谱系的 checkpoint。若进程停在提交与删除之间，下一次取得编辑根 exclusive lease 后的 orphan recovery 会补清理。失败、取消和中断保留已验证 cue，等待显式重试。

## 验证

| 检查 | 当前结果 |
| --- | --- |
| `uv run --no-sync python validation/local/validate_speech_checkpoints_20260911.py` | PASS：后段失败只重做未完成 cue、损坏回退、严格输入绑定、规范化远程 fingerprint、完整 PCM/轨道条件、实际消费字节 SHA 绑定、源文件打开失败的稳定错误映射、hard-link 中断恢复、远程 `responded` gate、成功与重启清理 |
| `uv run --no-sync python validation/local/validate_ai_authorization.py` | `45` 项 PASS，provider runner 调用数为 `0` |
| `uv run --no-sync python validation/local/validate_ai_ledger.py` | PASS |
| `uv run --no-sync python validation/local/validate_ai_invocation_ledger.py` | PASS |
| `uv run --no-sync python validation/local/validate_workflow_full_chain.py` | PASS：三平台离线整链、2 个合成 cue、重启续跑、幂等重放与 Python 网络阻断 |
| `uv run --no-sync python validation/local/validate_workflow_speech_rate.py` | `10/10` PASS |
| `uv run --no-sync python validation/local/validate_workflow_restart_continuation.py` | PASS |
| `uv run --no-sync pytest -q tests/test_editing_service.py tests/test_editing_ai_contracts.py tests/test_editing_media.py` | `54 passed` |
| 上述三组加 `tests/test_editing_api.py` | `69 passed, 2 failed`；两项失败仍精确断言旧 Editing Schema 1，而当前基线为 Schema 4，与本切片无关 |
| `uv run --no-sync python -m compileall -q src scripts` | PASS |
| 编辑页与 Workflow 页内联 JavaScript `node --check` | PASS |
| `uv pip check` | PASS；24 个已安装包兼容 |
| `uv lock --check --offline` | PASS |
| `git diff --check` | PASS；仅 Git 的 CRLF→LF 提示 |

专用 validator 位于被忽略的 `validation/local/`，不会进入提交或发行制品。本轮没有新增或修改 `tests/`。独立只读复核重放了 SHA/WAV 路径替换、文本规范化、截断 PCM、跨 cue 格式 poison、复制入口源文件消失与持续增长文件；本地 validator 另覆盖同 inode、同大小且恢复时间戳后的实际消费字节变更。修正后这些路径均返回稳定领域错误、保留不安全条目但不使用，或只重做对应 cue。

## 架构结论与未证明范围

实现加深现有 `AiRenderProcessor`、Editing request ledger、Schema 4 invocation ledger 与 render retry lineage；没有新增服务、数据库、Schema、常驻线程、队列、runtime、依赖或框架。checkpoint 只保存临时 PCM WAV，不进入下载资产、编辑成品或发行包。

这些结果证明离线代码路径中的复用、身份校验、恢复与清理合同。它们不证明真实 OpenAI 请求成功、供应商没有计费、中文或 English 配音质量、真实 URL 下载、来源封面质量，也不证明 Bilibili、抖音或视频号已经接收、审核、定时执行或公开发布。真实验收仍须绑定同一 clean 构建、明确授权的短样本、供应商记录和平台后台结果。
