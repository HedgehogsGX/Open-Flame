# Iteration 0.28.0 发布后 AI 精确授权与输入硬预算记录

日期：2026-09-08

起始基线：`aa533bcc9a1bb099677588ada682d907fc666dd0`（0.28.0 发布后自动流程正确性提交）

范围：当前本地源码、synthetic/fake provider 与既有回归；未提供真实 OpenAI 凭据，未调用真实 AI 或国内平台，也未生成绑定本次源码的新 release receipt

## 完成的行为

- 每项 AI 能力生成 canonical authorization 与 SHA-256，精确绑定 authorization schema、runtime ID/version、AI protocol schema、manifest SHA-256、provider ID/kind、model ID 与本地声明 revision、`transcribe`/`translate`/`synthesize` operation、`local`/`remote` 执行类型、精确 data egress 和 effective limits。authorization 不保存 endpoint、密钥、媒体正文或 provider 响应。
- 新 AI task 把完整 authorization 保存到不可变 request；配音把它保存到不可变 recipe；Workflow profile 保存三项完整 authorization 及其摘要。浏览器创建或确认时提交自己核对过的绑定，服务在创建/确认处对 request、recipe 或 profile 做定义 CAS，worker 与 provider 调用前再次和当前 runtime 核对。
- 任何缺失、结构错误、operation/provider/model 不匹配、摘要不一致、runtime manifest 变化、model 本地声明变化或 limits 漂移都会失败关闭。旧 task、recipe 和 workflow 保持可读，但没有 authorization 的历史记录不能确认、重试或执行，须按当前能力重建。
- 远程 operation 仍要求单独的 data-egress 确认；三项均为本地 plugin 时不要求确认不存在的外发。页面显示 provider、model、本地声明 revision、manifest 摘要、外发范围和硬上限。刷新或变更 AI 选择会使旧页面确认失效。
- 核心硬上限在首次 provider 请求前按完整工作量检查。当前版本没有自定义降额入口；持久化 authorization 必须与核心固定值完全一致，不能通过修改 runtime manifest 替换或抬高。

| 操作 | 当前核心硬上限 | 口径 |
| --- | --- | --- |
| 听写 | 30 分钟、25 MiB、1 次请求 | 实际选择范围和最终派生 M4A |
| 翻译 | 1000 cues、60000 输入字符、20 个调用单位 | 每 50 cues 估算一个单位；每批重复发送的 glossary 也计入字符数 |
| TTS | 600 cues、60000 输入字符、600 次调用 | 每个有文字的 cue 最多一次调用；空 B-roll 不调用 |

这些值只限制本机在一次已确认 operation 中允许送入 provider 的输入和调用数量。它们不是价格上限、token/秒数账单、账户额度或实际 usage ledger，也不能证明 canceled/unknown 请求没有计费。

## 轻量架构边界

- authorization 与预算检查位于核心 Python 标准库路径；没有新增 SDK、常驻服务、后台 daemon、模型权重或核心依赖。
- 当前仍使用 Editing Schema 3；绑定复用现有不可变 task request、recipe 和 Workflow profile，不为本里程碑增加数据库表。
- 听写输入在最终预算复核后复制到每次调用的私有 scratch，再校验大小/SHA-256；runtime 在该准备完成后、subprocess runner 启动前及返回后重复散列。跨平台按路径启动仍不构成对同系统账号并发改写者的密码学 attestation，runtime 维护必须先停止应用；跨进程 runtime lease/不可变 snapshot 仍是发布前加固项。
- manifest 中的 model revision 是本地声明。`whisper-1`、`gpt-5.6-luna`、`gpt-4o-mini-tts` 等远端 ID/alias 仍可能在供应商侧改变行为、价格或可用性，而不改变本地 authorization 摘要。

## 当前验证结果

| 检查 | 结果 |
| --- | --- |
| `uv run --frozen python -m compileall -q src` | PASS |
| ignored `validation/local/validate_ai_authorization.py` | 45/45 checks passed；legacy/mismatch/非 canonical 降额/三类越界均在 provider runner 前拒绝，`provider_runner_calls=0` |
| isolated runtime 听写稳定输入成功路径 | PASS；scratch 中固定副本完成 provider 调用并在返回后清理 |
| ignored Workflow 验证 | `validate_workflow_v028.py` 与 `validate_workflow_ai_authorization.py` 均 PASS；覆盖三项完整 authorization、远程/纯本地 egress 语义、legacy create 拒绝和 profile SHA CAS |
| focused 编辑/上传回归 | 165 passed、3 deselected；两个 deselected 是既有 Schema 1 固定断言，另一个 Windows 资源计数用例在组合运行时两次出现单句柄波动、单独重跑 1 passed |
| editing/workflow inline JavaScript `node --check` | PASS |
| `uv pip check` | 24 packages compatible |
| `scripts.release.source_snapshot/source_contract` | PASS；245 个明确文件，`tests/` 条目为 0 |
| 第二轮独立只读代码复核 | 在已记录的受信任单用户主机边界内，无未修复 P0/P1/P2 |
| `tests/` 工作树与提交范围 | PASS；本里程碑没有新增或修改测试文件，`verify_commit_scope.py --staged` 已通过，pre-commit hook 会在提交时再次复核 |

上述结果已经在最终源码修正后重跑，只证明本次提交前工作树。它们不等同于冻结发行构建，也不能继承 `0592b6f` 的 release receipt。

## 未完成与下一步

1. 将 Editing Schema 升至 4，加入不含正文或密钥的远端调用 ledger，保存 operation、authorization/request digest、尝试序号、调用单位及 pending/accepted/unknown/reconciled outcome。无法证明未被服务端接受的请求必须保持 unknown，先 reconciliation 再决定是否重试。
2. 加入可复用的非密钥自动化预设。预设保存分段、封面、语言、标准音色和平台参数意图，不保存 API key、Cookie、扫码状态或账号 session revision；运行时重新绑定当前 authorization 与账号 revision。
3. 完成禁止真实网络的 URL→下载→听写→翻译→配音/编辑→Bilibili、抖音和视频号上传草稿 synthetic smoke，覆盖重启、绑定漂移、unknown 和重复提交边界。
4. 在新的 clean commit 与 release receipt 上另行执行真实 OpenAI 短样本、中文/English 真人试听、实际费用和三平台逐项验收。当前未执行真实 OpenAI、真实登录/扫码、真实下载、真实上传、定时触发、平台审核或公开发布。
