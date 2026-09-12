# Open-Flame 外部完整测试手册

本文件供受委托的开发者、测试员及其 AI 对一个**固定 Git 提交**进行验收。当前首批上传范围只有 **Bilibili、抖音、视频号**。测试结果必须区分本地合成验证、真实 AI 调用、平台接收、平台审核和公开发布；其中任一层成功都不能替代下一层。

测试者完成后须基于[回传模板](docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)输出一份独立 Markdown 文件，用它记录证据、失败复现和下一步开发入口。排障方法见[完整 Debug 指南](docs/DEBUG_GUIDE.md)。更细的上传矩阵见[上传器外部验收计划](docs/UPLOADER_TEST_PLAN.md)。

## 1. 测试边界与结果词汇

- 只使用测试者本人管理的账号，以及本人创作或已获授权下载、翻译、配音和投稿的媒体。
- 不把 API key、Cookie、二维码、手机号、平台 session、完整本地用户名路径或未脱敏日志提交到仓库。
- 每项只使用 `PASS`、`FAIL`、`BLOCKED`、`NOT RUN`。`PASS` 必须写明观察到什么；`BLOCKED` 必须写明阻断条件；未执行的真实远端动作使用 `NOT RUN`。
- `submitted` 只表示上游工具报告平台接收了投稿请求；`draft_saved` 只表示上游工具报告视频号草稿已保存。两者都要在平台后台核对，不能自动写成审核通过或公开成功。
- 本地草稿、runtime `ready`、二维码出现、HTTP 200、合成 provider 成功及历史 receipt 都不是当前真实平台成功证据。
- 测试期间不要直接编辑 SQLite、manifest、runtime 或账号私有目录。不要通过重复点击来“试出成功”；远端结果为 `unknown` 时先到平台后台核对。

## 2. 固定被测提交

在全新目录检出维护者指定的完整 40 位提交，不用会移动的分支名充当最终身份：

```powershell
$openFlameCommit = '<维护者提供的完整 40 位 Git SHA>'
if ($openFlameCommit -notmatch '^[0-9a-f]{40}$') { throw '请填写完整提交 SHA' }
git clone https://github.com/HedgehogsGX/Open-Flame.git Open-Flame-test
Set-Location .\Open-Flame-test
git checkout --detach $openFlameCommit
git rev-parse HEAD
git status --porcelain
git show -s --format='author=%an <%ae>%ncommitter=%cn <%ce>%nsubject=%s' HEAD
git config core.hooksPath .githooks
git config --get core.hooksPath
```

`git status --porcelain` 在测试开始前应无输出。报告必须记录：完整 SHA、获取方式、Windows 版本、Python/Node 版本、CPU 架构、系统时区、浏览器版本、是否使用全新 app root、测试开始/结束时间。若使用维护者提供的源码 ZIP，还要记录 ZIP SHA-256；不要把 GitHub 自动 ZIP 的散列当作项目 release receipt。

启动后从实际进程读取产品身份：

```powershell
$openFlameBase = 'http://127.0.0.1:8000'
$identity = (Invoke-RestMethod -Uri "$openFlameBase/api/v1/capability-snapshot?limit=1").current_product_identity
$health = Invoke-RestMethod -Uri "$openFlameBase/health"
[pscustomobject]@{
  git_commit = (git rev-parse HEAD)
  product_identity = $identity
  health = $health.status
}
```

若产品身份缺失或报告 `product_build_drift`，停止该轮并重新核对启动目录。源码 SHA 与运行中的产品身份必须同时写入报告。

## 3. 安装与运行前门槛

先阅读 [Windows Setup](docs/WINDOWS_SETUP.md)、[启动与日志](docs/WINDOWS_LAUNCHER.md)、[AI runtime](docs/AI_RUNTIME.md)和[上传 runtime](docs/UPLOAD_RUNTIME.md)。需要 AI 的完整测试使用第二条 Setup 命令；不测试 AI 时使用第一条：

```powershell
# 不运行 AI 的上传测试：
.\Setup-Open-Flame.cmd --yes --upload-runtime
# 或者，运行 AI 与上传测试：
# .\Setup-Open-Flame.cmd --yes --upload-runtime --ai-python-embed-zip '<文档指定的绝对 ZIP 路径>'

# Setup 只安装产品运行依赖。完整 Git checkout 要执行第 4 节源码回归时，
# 在应用启动前按 uv.lock 安装锁定的 dev extra：
uv sync --extra dev --frozen

.\Start-Open-Flame.cmd --no-open-browser
```

若不执行源码回归，可跳过 `uv sync`，保持普通产品运行环境。不得在应用运行期间修改 `.venv`；如已启动，先按 `Ctrl+C` 正常停止，再安装开发依赖并重新启动。报告须记录 `uv --version`；无法取得或安装锁定开发依赖时，把源码回归标为 `BLOCKED`，不要改用未锁定 pytest。

在另一个 PowerShell 中记录四项就绪度：

```powershell
$openFlameBase = 'http://127.0.0.1:8000'
Invoke-RestMethod "$openFlameBase/health/ready"
Invoke-RestMethod "$openFlameBase/api/v1/operations/runtime"
Invoke-RestMethod "$openFlameBase/api/v1/edits/ai/runtime"
Invoke-RestMethod "$openFlameBase/api/v1/uploads/status"
Invoke-RestMethod "$openFlameBase/api/v1/uploads/accounts"
```

真实 AI 测试只有在测试者自行配置本次 OpenAI key 后运行。真实平台测试只有在测试者本人决定扫码和提交后运行。若缺少 runtime、凭据或账号，把对应远端行标为 `BLOCKED` 或 `NOT RUN`，其余本地测试继续。

## 4. 无凭据源码回归

以下命令不得调用 OpenAI 或国内平台，且不应修改 `tests/`。当前开发基线的稳定回归集为：

```powershell
uv lock --check --offline
uv pip check
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_editing_service.py `
  tests/test_editing_ai_contracts.py `
  tests/test_upload_service.py `
  tests/test_upload_resilience.py `
  tests/test_upload_platform_parameters.py `
  tests/test_upload_api.py `
  tests/test_upload_backup_restore.py
git diff --check
git status --porcelain
```

报告保存每条命令、退出码、通过/失败/跳过数和运行时长。当前仓库另有少量仍断言历史 Schema/导航的旧测试；遇到失败必须逐项判断是既有过期断言还是产品回归，不能删除、改写或静默排除后宣称全绿。

## 5. 四页人工功能检查

在同一启动实例依次打开 `/`、`/edits`、`/uploads`、`/workflows`，在浅色、深色、系统主题及 320 px 窄视口检查：

| ID | 页面 | 操作 | 预期 |
| --- | --- | --- | --- |
| UI-01 | 全部 | 键盘遍历导航、表单、details、按钮 | 焦点可见、顺序合理、无键盘陷阱；轮询不抢焦点。 |
| UI-02 | 全部 | 输入文字、选择账号/任务、展开详情，等待至少 10 秒 | 轮询后输入、选择、焦点和展开状态保持。 |
| UI-03 | 全部 | 系统/浅色/深色、减少动态效果、200% 文字缩放、320 px | 内容可读，无关键控件裁切或水平溢出。 |
| DL-01 | 下载 | 提交一个合法自有 URL，观察 Batch/Input/Job/Asset | 阶段与错误码一致；ready 原件可进入编辑或显式导入上传。 |
| ED-01 | 编辑 | 建立项目、分段、封面、保存新草稿、建计划 | 原件不被覆盖；计划冻结版本；未确认不运行 FFmpeg。 |
| ED-02 | 编辑 | 取消 review/queued/running 计划，再显式重试 | 状态不倒退；重试产生明确 lineage，不自动沿用旧确认。 |
| ED-03 | 编辑 | 用 `1.005` 保存配音草稿、刷新并生成计划 | 页面原样恢复并显示 `1.005×`；非默认值进入 recipe SHA 与逐 cue request fingerprint，默认 `1.0` 仍兼容旧记录。 |
| UP-01 | 上传 | 导入媒体/封面、创建本地草稿、刷新 | 未最终确认前平台无新增；详情中的平台参数与输入一致。 |
| WF-01 | 自动流程 | 首次进入不选预设 | 显示“完整视频 · 0 段”；主动启用分段后出现 0–60 秒起始行。 |
| WF-02 | 自动流程 | 保存/载入预设 | 不保存 URL、key、Cookie 或 session revision；本次云端外发仍需重新勾选。 |
| WF-03 | 自动流程 | 启动后点击整流程取消 | 尚未发送的后续阶段停止；正在运行或取消中的最远任务显示等待；未知远端结果及部分平台已完成转为人工核对，不显示假取消。 |
| WF-04 | 自动流程 | 保存启用“使用下载来源标题”的预设，刷新后只更换 URL | 公共手动标题保持禁用；下载 ready 后卡片显示最终公共标题及每个账号的“来源标题 / 按平台限制生成 / 手动覆盖”来源，Bilibili/抖音/视频号分别遵守当前 80/30/100 字 capability。 |
| WF-05 | 自动流程 | 初次打开时临时阻断预设、账号或 AI capability 接口，再恢复并点“刷新状态” | 失败期间明确提示且不覆盖默认/已编辑表单；接口恢复后只在用户尚未编辑时恢复记住的预设，已编辑时要求手动选择。 |
| WF-06 | 自动流程 | 启用“优先使用下载到的来源字幕”，保存预设、刷新并重新载入 | 选项精确恢复；页面明确说明来源字幕仍需核对、人工/自动来源未知且无可用字幕时才回退 AI 听写；修改该选项后本次 data-egress 勾选立即清空，须重新核对。 |
| WF-07 | 自动流程 | 保存/恢复“优先使用下载到的来源封面”，再关闭“生成封面” | 显式偏好精确恢复；生成封面始终作为 fallback，关闭它会同时清除来源封面偏好。详见[来源封面验证](validation/iteration-0.28.0-workflow-source-cover-preference.md)。 |
| WF-08 | 自动流程 | 在完整 fan-out 中制造 `submitted`、`failed/canceled` 与重启后待确认原草稿并存，再点“建立失败投稿重试” | 只替换 failed/canceled slot；submitted 保持原 ID，原草稿不会复制；页面说明最终确认会同时排队重试草稿和原草稿。确认前平台无新增；任一 `unknown` 或部分准备批次不显示重试按钮。 |

页面视觉检查只证明当前浏览器中的呈现。若维护者声明某个新视觉方向，报告应附四页桌面与窄屏截图，并单独记录透明度关闭/减少动态效果的结果。

## 6. URL → AI → 三平台完整流程

每轮使用唯一测试编号，例如 `OF-20260909-B1`，并把编号写入标题、源视频首帧或口播。先用 15–60 秒自有短片验证，再决定是否测试长片。完整视频、分段和每个平台分别判定。

来源字幕用例应准备测试者有权使用、且下载结果确实登记字幕 sidecar 的 URL。下载记录中的 `ready` 和 `origin=platform` 只证明文件与父资产的登记链，不证明字幕由人工制作、内容正确或适合直接配音；报告仍须把来源类型写为“人工/自动未知”，并人工核对文字与时间轴。

来源封面测试须保留生成封面 fallback，并用 frozen `download_asset_id`、最终 `upload_cover_id` 和平台后台事实核对；完整矩阵见[来源封面验证](validation/iteration-0.28.0-workflow-source-cover-preference.md)。

| ID | 场景 | 必须观察的结果 |
| --- | --- | --- |
| E2E-01 | 完整视频；听写、翻译、标准音色配音；仅一个平台 | 请求保持 `segments=[]`；成品时长接近完整源；译文、字幕、配音内容与时间基本对应；只建立所选账号的任务。 |
| E2E-02 | 3 个连续分段 × 3 个平台账号 | 生成 3 个有序成品和 9 个精确任务；标题、标签、封面、发布时间及平台字段不串账号/平台；批量确认前均停在本地。 |
| E2E-03 | 预设重用 | 换一个 URL 后恢复编辑/AI/投稿参数，但重新绑定当前 AI authorization 和账号 session；旧外发勾选不复用。 |
| E2E-04 | 自动执行 | 明确勾选本次自动编辑/上传后，原始未派发任务可向前推进；重启后的 running、retry 或 unknown 不自动重放。 |
| E2E-05 | 整流程取消 | 分别在下载、AI、渲染、上传草稿/排队阶段取消；后续阶段不再创建。运行中任务等待其原域确认停止；未知远端结果或部分平台已完成时停下核对。 |
| E2E-06 | 输入/预算边界 | 超过当前 30 分钟听写、25 MiB 音频或文本/cue/request 硬上限 | 在 provider 请求前阻止；调用账本和平台均没有被掩盖的额外提交。 |
| E2E-07 | 配音语速 | 对同一短片分别保存 `1.0`、区间边界 `0.88`/`1.12` 和一个非百分位小数 | 预设精确恢复；计划/流程卡显示实际值；provider 收到对应 speed；少量溢出可因明确提高语速而通过，较大溢出仍以 `ai_speech_timing_overflow` 停止且不自动再次调用。 |
| E2E-08 | 来源标题冻结 | 使用标题长于抖音限制的自有来源，给其中一个账号另填标题；下载 ready 后重启，再完成多段上传准备 | 公共标题、三平台截断结果和手写覆盖在首次 ready 时冻结；重启及所有分段复用同一结果。标题暂不可读时只进入 `workflow_source_metadata_unavailable`，修复后对账不重复下载、不换绑 asset、不创建重复上传。 |
| E2E-09 | 来源字幕优先复用；完整视频：使用只有一份匹配语言 ready SRT/VTT 的来源并启用来源字幕优先 | 首次停在 `ai_source_caption_review_required`；即使勾选自动确认编辑，也不能批准首次导入的未知来源字幕。Editing timeline 为 `provider=download`、`state=review`，内容与绝对时间对应来源字幕；AI task/ledger 中没有 transcribe 远端调用。生产 Workflow 页面只引导前往 Editing 页核对并批准；另验证服务/API 只有首次停下后的单独显式确认才能批准。获批后才建立 translation，随后继续既有配音与上传流程。 |
| E2E-10 | 来源字幕优先复用；连续分段：选择 2–3 个首尾连续区间，使部分 cue 完全位于区间外、其余 cue 完全位于连续总区间内 | 区间外 cue 被排除，保留 cue 的绝对媒体时间并按结果重排顺序；审核前不翻译。让任一 cue 跨越外层或内部任一分段边界时，该字幕不被裁字或静默采用，而是走一次正常 AI transcribe fallback。 |
| E2E-11 | 来源字幕安全回退：分别使用无字幕、仅不支持格式、无匹配语言、同优先级多候选、无效 UTF-8/空字幕、HTML/SSA 标记字幕；再明确拒绝一条已导入 timeline | 前述无候选或不可用场景只创建正常 transcribe task，并继续要求冻结的 AI authorization 与本次 data-egress 确认；内容解析失败不留下半导入 timeline。被拒绝的来源 timeline 保留，下一次推进才另建 transcribe，不能在同一次审核动作中暗中调用。 |
| E2E-12 | 来源字幕重启、漂移与重试：导入后在审核前正常重启；批准后令 translation 以可重试失败结束；另在独立测试根中改变 caption 的 asset/artifact/path/hash 身份；在 AI fallback task 已建立后让候选列表出现字幕 | 重启复用同一 timeline ID，不能重复读取并导入第二份；translation retry 只建立绑定同一已批准 parent revision 的 translation successor，不能重新 transcribe。登记链或 hash 漂移必须进入精确 attention/error，且不得把完整性冲突当作可用性不足而回退远端 AI；已经开始的 AI fallback 必须继续原 task，不能中途切换字幕并遗留听写任务。 |
| E2E-13 | 来源封面偏好：覆盖可信 owner 下的 0/1/多候选、登记伪装/漂移、三平台共同兼容、3 分段、选择后/导入后/建 jobs 后崩溃重启、封面媒体删除、取消 tombstone 重放/篡改 | 唯一且全体兼容时采用来源封面；0 个或不兼容时回退生成封面；歧义/完整性漂移停下；任何 Upload 写入前先 CAS 冻结 cover ID。已有请求从不可变历史恢复且不要求终态封面字节；无请求的取消先原子 tombstone，重启不依赖 Download，并继续取消前序分段。详见[来源封面验证](validation/iteration-0.28.0-workflow-source-cover-preference.md)。 |

真实 OpenAI 结果另记录：模型标识、源/目标语言、音色、用例时长、听写主要错误、翻译主要错误、配音缺字/错音/爆音/时间溢出、是否需要人工修订。不要把硬上限当作价格估算；费用以测试者自己的 provider 账单核对。

真实平台从小到大执行：先 Bilibili、再抖音、最后视频号；每次只确认一个清楚核对过的草稿。到各平台后台记录平台是否接收、稿件/作品 ID、审核状态、公开状态、音画、标题、简介/正文、标签、封面、分区/声明、定时和平台专属字段。视频号 `draft_saved` 与 `submitted` 分开测试。详细用例和 unknown 处理遵循[上传器外部验收计划](docs/UPLOADER_TEST_PLAN.md)。

## 7. 恢复与故障注入

故障注入只在测试 app root 和测试账号上进行，不强杀或损坏维护者的真实数据目录。

1. 在 download queued、AI review、AI queued、render review、render queued、upload draft、upload queued 分别正常退出并重启；记录恢复后的固定 code 和是否需要再确认。
2. 对一个已知会失败的本地输入执行显式 retry；验证旧记录保留，新记录引用 predecessor，且同一重试请求不会产生多个 successor。
3. 在远端调用可能已经发送后中断网络或进程；期望 `unknown`/reconciliation，而不是自动重试。先到 provider 或平台后台核对，再使用 UI 的明确核对入口。
4. 更换上传账号 session、AI runtime manifest/model revision 或源媒体内容；旧 workflow/plan/job 应因绑定变化停止。
5. 测试相同 URL 的并发/重复下载、应用正常重启和整个 Workflow 取消；记录是否出现无法继续的 duplicate owner、残留子任务或重复远端请求。
6. 检查磁盘不足、runtime 缺失/损坏、调度器 standby、Worker paused/stale；修复后只用产品提供的刷新、retry、advance 或重建入口恢复，不直接改库。

## 8. 证据与回传

每个 `FAIL` 至少包含：固定提交、product identity、用例 ID、最小输入特征、准确步骤、预期、实际、UTC 与本地时间、页面固定错误码、相关 workflow/batch/project/plan/job ID、脱敏日志事件、是否发生远端动作、平台后台核对结果、重现次数。截图和媒体证据放在仓库外受控位置，报告只写经授权的引用和 SHA-256。

测试者必须从[回传模板](docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md)生成一个文件。可在源码根目录执行：

```powershell
$shortSha = (git rev-parse --short=12 HEAD).Trim()
$testerLabel = '<非隐私唯一测试轮次短标签>'
if ($testerLabel -notmatch '^[a-z0-9][a-z0-9-]{0,31}$') { throw '测试者短标签只能使用小写字母、数字和连字符' }
$reportDirectory = 'validation/external'
$reportPath = Join-Path $reportDirectory "$(Get-Date -Format yyyyMMdd)-$shortSha-$testerLabel-handoff.md"
if (Test-Path -LiteralPath $reportPath) { throw '报告路径已存在，请换一个非隐私测试轮次短标签' }
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
Copy-Item 'docs/EXTERNAL_TESTER_HANDOFF_TEMPLATE.md' $reportPath
$reportPath
```

该文件既是验收报告，也是下一位开发者的入口，默认作为 Git 仓库中的交接记录。`testerLabel` 只用于区分测试轮次，不得使用 Windows/GitHub 用户名、姓名、手机号、账号 ID 或其他私有标识。报告必须列出未验证边界、按优先级排序的缺陷、首个可复现下一步、涉及文件/模块和禁止破坏的约束。新报告不会因位于 `validation/external/` 而自动进入 project-only release；只有维护者完成隐私与内容复核并把精确路径显式加入 `release-files.txt` 后，才可随该发行包交付。测试者若提交代码修复，应把测试报告和产品修改分成清晰提交，并不得加入凭据、账号私有数据、媒体、runtime、构建目录或新的/修改过的 `tests/` 文件。提交前须执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_commit_scope.py --staged
git diff --cached --check
```

2026-09-13 维护者已按用户明确授权应用一次固定差分的测试维护；这不是外部测试者的通用例外。
具体范围及后续 CI 见 [CI 合同维护记录](validation/iteration-0.28.0-ci-contract-maintenance.md)，
其他测试修改仍须单独获得对应授权。
