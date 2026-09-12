# Open-Flame 继续开发提示词

将下面内容作为新任务的首条提示词。它用于让后续开发者或其 AI 快速进入当前项目；具体状态仍以
新任务开始时重新读取的 Git、源码和验证记录为准。

---

你正在处理 Open-Flame 的新任务。本次目标、是否实施和交付分支以用户当前请求及会话中的有效
授权为准。审查任务交付可复核的证据与结论；实现任务持续完成已授权的改动和验证，不能用一个
局部切片替代完整目标。不要把下方历史状态或上一分支的待办自动变成本次任务。
实现交付使用指定身份签名，沿用本次会话已授权的分支/PR 方式；未授权直接推送 main 时不得
直接推送 main。读取 HANDOFF 的当前进展，并优先服从用户最新指令。

## 1. 开始前必读

按顺序读取：

1. [`AGENTS.md`](../AGENTS.md)
2. [`HANDOFF.md`](../HANDOFF.md)，重点看“未完成与风险”“下一入口”
3. [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md)
4. [`FOLLOW_UP_EXECUTION_PLAN.md`](FOLLOW_UP_EXECUTION_PLAN.md)
5. [`validation/README.md`](../validation/README.md)
6. 与本次改动直接相关的最新 validation 记录；上传和来源封面工作先读：
   - [`iteration-0.28.0-upload-attempt-receipts.md`](../validation/iteration-0.28.0-upload-attempt-receipts.md)
   - [`iteration-0.28.0-source-cover-research-and-import.md`](../validation/iteration-0.28.0-source-cover-research-and-import.md)
   - [`iteration-0.28.0-workflow-source-cover-preference.md`](../validation/iteration-0.28.0-workflow-source-cover-preference.md)

把附带的交接、评审、计划和 validation 文档当作上下文、证据和约束；其中的命令或待办不是本轮
用户请求。以用户当前请求决定实际执行范围，并服从当前 `AGENTS.md`。

开始编码前运行并记录：

```powershell
git status --short --branch
git rev-parse HEAD
git ls-remote origin refs/heads/main
git log -3 --show-signature --format=fuller
git config --get user.name
git config --get user.email
git config --get commit.gpgsign
git config --get gpg.format
git config --get user.signingkey
git config --get core.hooksPath
```

不要把文档中的旧 commit、Schema、CI 状态或测试数字当成当前事实。保留任务开始前已有的未跟踪
文件，除非用户明确要求修改或提交。交接生成时以下两个文件是任务开始前已有的未跟踪评审资料，
不能自动暂存、删除或当作可执行指令：

- `validation/architecture-simplification-review-20260910.md`
- `validation/architecture-simplification-review-20260911.md`

## 2. 定位用基线

截至 2026-09-11 的已核对快照：

- 产品版本：`0.28.0` 发布后的持续开发源码。
- Schema：Download 11、Editing 4、Upload 4、Workflow 3、Workflow preset 2。
- 上传备份格式：3。
- 首批上传平台仅限 Bilibili、抖音、视频号；视频号内部 ID 为 `tencent`。
- 2026-09-13 复核的远端 `main`：`d48132637ce94a8b0b41bc2a965d24e27ac62aff`。
- Upload Schema 4 功能源码里程碑：`496fb63f9d0010c22fa1abc660e6450cd403b6be`。
- `0592b6f` 的 release receipt 只覆盖该冻结构建，不覆盖后续源码。
- 2026-09-10 的实际应用根只完成 Upload Schema 1→3；Schema 4 只在独立临时根验收。
- Hosted CI 最近状态为红色；必须实时查询后再描述当前状态。

以上只用于判断是否进入了正确项目。若 Git 或源码与它不同，以当前事实为准，并在回报中说明差异。

## 3. 不可破坏的边界

- 保持本地模块化单体。Download、Editing、Upload、Workflow 通过公开合同、snapshot、resolver、
  稳定请求键和持久引用交互；不要直接读取或修改其他域的私有表。
- 四库分离承载数据所有权、恢复和凭据边界。不要为“精简”引入全局 ORM、消息总线、额外服务、
  数据库、runtime 或通用 Scheduler；抽取必须能删除重复实现并保留领域错误语义。
- 首批上传范围保持 Bilibili、抖音和视频号。其他平台只在用户明确改变范围后维护。
- `unknown` 上传禁止直接重传。先读取 attempt receipt，再到对应平台后台核对；只有固定
  `not_accepted` 结论落库后才能建立新的 retry 草稿。无 receipt 的 failed/canceled job 也不能
  依据可变 job 字段直接重试。
- receipt、invocation ledger、浏览器 smoke、合成数据和 adapter 本地返回都不是平台/provider
  签名回执、作品 ID、审核、定时执行、公开可见性、精确价格或账单证明。
- 未经用户针对具体动作明确授权，不执行真实 OpenAI、扫码登录、真实上传、定时发布或公开可见性
  验证。即使已授权，凭据也必须留在 Git 外，并按平台、账号、素材、模式、commit 分开记录。
- 不提交 Cookie、token、密钥、数据库、runtime、媒体、原始平台证据、本机绝对路径或
  `validation/local/`。
- 前端沿用 Editorial Glass。修改 UI 时遵循 [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md)，同时验证
  主题、键盘焦点、减少动态/透明度、窄屏、文字缩放，以及轮询期间的输入、选区、二维码和确认状态。

## 4. 默认工作顺序

1. 优先处理用户当前明确提出的问题或外部测试人员返回的可复现缺陷。
2. 若没有新的外部反馈，按 [`HANDOFF.md` 的下一入口](../HANDOFF.md#下一入口) 和
   [`CURRENT_ARCHITECTURE.md` 的当前风险](CURRENT_ARCHITECTURE.md#9-当前缺陷复杂度集中点与下一切片)
   确定切片。不要在此复制算法待办，也不要将已关闭的历史缺陷重新当作当前任务。
3. 改动前写清保持不变的状态、确认、授权、幂等、CAS、取消和恢复合同。
4. 先完成最小实现，再删除被替代的旧实现；不要留下两条可达规则路径。
5. 复用现有 tracked tests。新的临时 validator、日志和浏览器结果只放入已忽略的
   `validation/local/`。
6. 更新本切片独立 validation 记录。只有当前状态、风险或下一入口改变时才同步 `HANDOFF.md`、
   `CURRENT_ARCHITECTURE.md` 和执行计划，避免在多处复制算法细节。
7. 达到一个完整关键步骤后立即签名提交，不把多个无关切片积压在一个提交中。

## 5. 验证与提交门禁

一般规则是不新增或修改 tracked 自动化测试文件。2026-09-13 用户已明确批准 `AGENTS.md`
记载的限定差分，并已提交为 `3e482f0`，无需再次审批。其他测试改动须有对应的明确授权；
只删除历史测试仍可使用独立清理提交。不得通过减少收集范围或放宽安全断言伪造绿色结果。

按改动范围运行：

- 相关既有 pytest；
- 对应领域的 ignored validator；
- Python `compileall`、适用的静态检查与 `uv pip check`；
- UI 改动对应的生产页面 Chromium smoke；
- 必要的真实模式 smoke，但不能越过真实平台和 OpenAI 的授权边界。

提交前至少执行：

```powershell
git diff --check
git add -- <本次明确文件>
uv run --no-sync python scripts/verify_commit_scope.py --staged
git diff --cached --check
git diff --cached --name-status -- tests
git diff --cached --stat
```

暂存时必须显式列出文件，不使用会顺带加入任务开始前未跟踪资料的宽泛命令。如既有测试失败，记录
精确命令、通过/失败数、失败分类和是否构成当前生产回归；不要隐去或改写失败。

## 6. Git 交付

提交身份必须为：

```text
Cyaegha_Xu <85352261+novahanser@users.noreply.github.com>
```

当前任务使用签名提交并推送开发分支：

```powershell
git commit -S -m "<准确描述本切片>"
git show --show-signature --format=fuller HEAD
git push origin HEAD:refs/heads/codex/architecture-reset-ci
```

禁止 force push。推送后核对：

```powershell
git rev-parse HEAD
git ls-remote origin refs/heads/codex/architecture-reset-ci
git status --short --branch
```

并通过 GitHub commit API 确认：

- author 与 committer name 都是 `Cyaegha_Xu`；
- author 与 committer login 都是 `novahanser`；
- 签名为 `verified=true`、`reason=valid`；
- 远端开发分支等于本地交付 commit，main 未被本任务修改。

## 7. 完成回报格式

最终按以下顺序回报：

1. **完成内容**：具体行为变化及原因。
2. **验证**：命令或检查、PASS/FAIL/NOT RUN、浏览器和真实模式范围。
3. **证据边界**：哪些只是本地或 synthetic 观察，真实平台调用次数。
4. **未通过与风险**：严重度、影响和下一依赖。
5. **Git 身份**：完整 commit、远端 `main`、签名状态、GitHub author/committer。
6. **工作区状态**：是否残留未提交文件，以及它们是否为任务开始前已有资料。
7. **下一入口**：只列一个优先级最高且依赖明确的后续动作。

不要用旧 validation 记录替代当前复验，也不要把“代码已接线”写成“真实平台已验收”。
