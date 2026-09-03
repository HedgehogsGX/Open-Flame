# ADR-0001：X 多附件使用不可变发现快照与逐附件任务

- 状态：Implemented in v0.6.0 / Schema 8；真实 X 路由仍受 exact-selector 与 Stage 0 capability gate 阻断
- 决策日期：2026-09-03
- 首个实现迭代：0.6（schema 8 起）

## 背景

需求规定：一条 X 帖子包含多个媒体附件时，父 `discover` Job 记录探测快照，建立带顺序的 `SourceRelation`，并为每个附件创建独立的子 `download` Job。所有附件成功时 Input 为 `ready`；一部分成功、一部分终止失败时为 `partial_success`。

Schema 6/7 的历史运行路径虽然已有 `source_relations` 和 `job_kind` 字段，但仍是一个 `download` Job 在一次 Attempt 中 probe N 项、下载 N 项并整组提交。这是有效的 flat-v1 原子提交路径，却不是上述 graph-v2 语义。直接复用父 URL 创建多个 child 还会违反 `source_items.canonical_url UNIQUE`，并且当前 yt-dlp Adapter 没有经过验证的“只下载指定 X 附件”契约。

## Implementation status

v0.6.0 / Schema 8 已实现本 ADR 的 orchestration 与持久化范围：parent `discover` Job、不可变有序 discovery snapshot、逐附件 child `download` Job 与 exact target、`active_discovery_id` / `active_run_generation` 聚合、`partial_success`、generation-local retry、input-level cancel，以及 terminal-only rediscover。HTTP 操作入口为 `POST /api/v1/inputs/{input_id}/cancel` 与 `POST /api/v1/inputs/{input_id}/rediscover`；后者在存在任何非终态工作或 Input 不是 graph-v2 时返回 409。

实现完成不表示真实路由可用。`VDC_ENABLE_X_GRAPH_V2` 默认 `0`；只有测试内显式组装的离线 `ScriptedGraphFakeAdapter` 声明 `supports_exact_selector=True` 并验证过 graph-v2。真实 candidate Worker 使用的 `YtDlpAdapter.supports_exact_selector=False`，普通 offline fake adapter 也不满足 exact-selector contract。Worker claim 会对不支持的 graph job fail closed，但这只是 containment，不是启用方案。真实 X graph 因此继续保持 `candidate/disabled`，不能标为 `verified`。

Schema 8 migration 保留旧 flat-v1 Job、Asset 与 manifest 的可读性，并把已有 relation 迁入 deterministic legacy discovery，不原地转换旧资产。当前备份/恢复 CLI 要求精确 Schema 8，graph 表与 active snapshot 指针随 SQLite 一致快照保存，但 `temporary` / `assets/.staging` 仍排除；Iteration 0.6 已用离线 graph 数据完成临时独立根恢复与重签名跨表语义篡改拒绝，[证据](../../validation/iteration-0.6-graph-v2-offline-evidence.md) 不替代目标 Linux/NAS 验收。Iteration 0.5 / Schema 7 记录继续作为历史证据。metrics 的队列/Job 计数包含 parent `discover`，而 `platform_outcomes` 只统计 `job_kind=download`，不会把 orchestration ready 算成真实平台下载成功。

## 决策

### 1. 支持边界

graph-v2 第一阶段只实现 `SourceType.X_ATTACHMENT`。公开 X 帖子仍是父 `SourceItem`；附件是子 `SourceItem`。播放列表、频道、Bilibili 多 P 和抖音合集后续复用同一模型，但不在首次 migration 中假定其 selector 行为。

在 Stage 0 尚未用真实授权样本证明以下两点前，真实 X graph capability 保持 `candidate/disabled`：

1. probe 返回的附件 key 在重复探测中稳定且唯一；
2. 固定版本 yt-dlp 能使用受控 selector 只下载一个指定附件，且不会顺带下载 sibling。

离线 fake Adapter 可以先验证状态机和事务，但不能把平台能力升级为 `verified`。

### 2. 子来源身份与抓取目标分离

附件身份不使用 ordinal。ordinal 只表达一次发现中的出现顺序。

- `source_items.platform = x`
- `source_items.source_type = x_attachment`
- `source_items.source_id = xatt:v1:<sha256(parent source identity + NUL + stable attachment key)>`
- `source_items.canonical_url = <parent canonical URL>#vdc-media=<percent-encoded stable attachment key>`

hash 输入采用 UTF-8 且字段有显式长度/分隔规则，避免字符串拼接歧义。stable attachment key 必须是 bounded、非空、无控制字符的普通标量；缺失、重复或不稳定时 fail closed，绝不退化为 ordinal 派生身份。fragment 是内部、无秘密的 canonical locator，不直接发送给下载器。

实际抓取信息单独保存在 `download_job_targets`：

- `job_id`
- `fetch_source_item_id`：父帖子
- `selector_key`
- `expected_media_key`

Worker 读取父帖子 URL和 bounded selector。该 target、Credential、Cookie 路径、签名 URL、headers 与 raw extractor JSON 都不得进入公共 API、日志或资产 metadata。

### 3. 不可变 discovery snapshot

新增 `source_discoveries`，身份为：

`(parent_source_item_id, relation_type, snapshot_hash)`

snapshot hash 是 canonical JSON 数组的 SHA-256。数组保持 probe 顺序，每个成员只包含稳定 child identity、selector key 和 media kind；标题等易变 metadata 不参与 membership hash。

每个 `InputRecord` 通过 `active_discovery_id` 指向自己的当前快照。`SourceRelation` 属于一个 immutable discovery；旧 discovery、relation、Job、Attempt 和 Asset 不覆盖、不删除。相同快照重放必须复用 discovery、relations 和既有 child Jobs，结果幂等。

### 4. Job 与关系所有权

- parent `discover` Job 只负责 probe 和原子 fan-out，不下载媒体。
- child `download` Job 只负责一个 exact selector 和一份 original；其缩略图和平台字幕仍与该 original all-or-nothing 提交。
- `input_relation_jobs` 将当前 Input 的 relation occurrence 映射到 child Job；同一快照内重复出现的同一 child 可由多条 relation 映射到一个 Job。
- SourceItem 和已经 ready 的 MediaAsset 可以复用；活跃 DownloadJob 不跨 Input 共享，因为 credential、route policy、取消和错误归属属于 Input。
- 新 Input 命中兼容的 ready Asset 时，可创建本 Input 的逻辑 Job 并通过 `reused_from_job_id` 记录复用，不重新抓取。

原有 `idx_one_live_job_per_source` 不适合 graph-v2；Schema 8 migration 已以 Input/run 为作用域替换该约束，并补充幂等唯一键，而非仅删除旧索引。

### 5. 原子 fan-out

新增一个 repository 原子方法，例如 `commit_discovery()`。它在同一个 `BEGIN IMMEDIATE` 事务内完成：

1. CAS 校验 parent Job 仍为 `probing`、lease 有效且 Attempt 为 `running`；
2. 检查 parent/Input cancel request；
3. 验证 count `1..50`、identity、selector、顺序和调用方 snapshot hash；
4. get-or-create immutable discovery，并逐项验证相同 hash 的成员完全一致；
5. upsert child SourceItems；
6. 写入 ordered SourceRelations；
7. 为当前 Input get-or-create child Jobs、targets 和 relation-job links；
8. 继承 parent route policy 与显式 credential profile；
9. 更新 `active_discovery_id` 与 expected count；
10. 完成 parent Attempt/Job；
11. 只按当前快照 child Jobs 重新聚合 Input 与 Batch；
12. commit。

probe 后、事务前崩溃不会留下半套 graph；事务提交后崩溃会留下完整 parent + child queue；stale Worker 由 lease CAS 拒绝。

### 6. 聚合规则

`discover` Job 的 `ready` 只表示编排成功，不计作媒体成功，也不进入平台下载成功率。

Input 只聚合 `active_discovery_id` 的 child `download` Jobs：

- discovery 未完成，或任一当前 child 非终态：`queued`
- 所有当前 child `ready`：`ready`
- 至少一个 `ready`，且另有 `failed/canceled`：`partial_success`
- 无 `ready`，但至少一个 `failed`：`failed`
- 无 `ready`，且全部 `canceled`：`canceled`
- discover 在 fan-out 前失败/取消：直接 `failed/canceled`

因此 parent ready + 全部 child failed 的结果必须是 `failed`，不能是 `partial_success`。Batch 始终按 InputRecord 计数，不按附件数膨胀；schema 增加 `partial_success_count`，使状态计数可以闭合 `total_count`。

### 7. 取消、重试与重探测

- 保留 job-level cancel。
- 增加 input-level cancel：fan-out 前取消 parent 且不生成 child；fan-out 后 queued child 原子取消、active child 写 cancel request、ready child 保留。
- parent retry 只重做 probe/fan-out；child retry 只重试该附件，不重复已 ready sibling。
- `attempt_no` 仍在 Job 生命周期内单调递增；显式 rediscover 使用 `run_generation`，不得通过重置 attempt count 制造唯一键冲突。
- graph-v2 第一版只允许在当前 child 全部终态时 rediscover；存在 active child 返回 409。
- 新快照的 added/retained/removed 只改变新的 active snapshot：removed 的历史证据不删除；unchanged 重放零新增关系和 Job。

旧 flat-v1 多资产 Job/Asset/manifest 不原地改写。它们继续可读；迁移到 graph-v2 必须通过重新提交或后续显式转换，避免改变旧 `MediaAsset.source_item_id` 造成 manifest 与数据库失配。

## Schema 8 实现范围

已实现的 forward-only migration 包括：

1. `SourceType.X_ATTACHMENT`；
2. `source_discoveries`；
3. 重建 `source_relations`，增加 `discovery_id` 与真实 Attempt FK，唯一键改为 discovery 作用域；
4. `input_relation_jobs`；
5. `download_job_targets`；
6. `input_records.active_discovery_id`、`input_records.cancel_requested_at`；
7. `download_jobs.run_generation`、generation-local attempt count、可空 `reused_from_job_id`；
8. `job_attempts.run_generation`；
9. `batches.partial_success_count`；
10. 以 Input/run 为作用域的 Job 幂等索引与 readiness 检查。

若旧库已有 relation，migration 为其创建 deterministic legacy discovery 并保留 relation ID、parent、child、ordinal、Attempt 和时间；迁移中任一异常必须整体回滚。

## 验证门槛

离线实现按以下四个增量完成；它们不替代真实 Stage 0：

1. schema/migration/readiness；
2. repository fan-out、幂等与故障注入；
3. Worker discover/child 双路径及 exact-selector fake contract；
4. 聚合、cancel/rediscover、API/metrics 与端到端恢复。

最少覆盖：1/50 成员边界、0/51 拒绝、重复/重排/新增/移除、相同 snapshot 重放、每个写入点回滚、stale lease、parent/child 崩溃点、所有聚合组合、取消竞态、终态 rediscover、旧 flat-v1 保持不变，以及内部 selector/credential 不外泄。

## 影响

该决策增加了 schema 与状态机复杂度，但避免把 ordinal、父 URL 或一次 extractor 输出误当成稳定身份；也保留了逐附件失败、取消和审计能力。X 多附件 graph-v2 已完成离线编排实现，但不能仅凭 `ScriptedGraphFakeAdapter` 宣称真实可用；在 Stage 0 exact-selector 证据与 capability 更新前，默认 gate 必须保持关闭。
