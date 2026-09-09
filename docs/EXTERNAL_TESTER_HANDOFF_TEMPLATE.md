# Open-Flame 外部测试回传与后续开发交接模板

复制本文件到 `validation/external/YYYYMMDD-<short-sha>-<tester-label>-handoff.md` 后填写；`tester-label` 使用非隐私、可区分本轮测试的小写字母、数字或连字符，避免多名测试者覆盖同一路径。不要在标签中使用 Windows/GitHub 用户名、姓名、手机号、账号 ID 或其他私有标识。删除占位提示，但保留未执行项并标为 `NOT RUN`。本文件不得包含 API key、Cookie、二维码、手机号、平台 session、完整私有路径、未脱敏日志或未经授权的媒体。

## 1. 被测对象

| 字段 | 值 |
| --- | --- |
| Repository | `https://github.com/HedgehogsGX/Open-Flame` |
| Git commit（40 位） | `<required>` |
| Branch/tag（仅辅助） | `<value>` |
| Commit author / committer | `<name and public email>` |
| 获取方式 | `git detached checkout / controlled ZIP / other` |
| Source ZIP SHA-256 | `<hash or N/A (Git checkout)>` |
| `product_identity` | `<exact runtime value>` |
| Working tree before test | `clean / dirty` |
| Working tree after test | `clean / dirty` |
| 测试开始/结束 | `<local time, timezone, UTC>` |

## 2. 环境

| 项目 | 值 |
| --- | --- |
| Windows edition/build/architecture | `<value>` |
| CPU / RAM / free disk | `<value>` |
| Python / uv / Node | `<value>` |
| Browser and version | `<value>` |
| Display scale / viewport | `<value>` |
| App root | `<redacted label; do not expose username>` |
| Fresh or reused data | `<fresh/reused, prior schema>` |
| Download Worker | `<state/detail_code>` |
| AI runtime/provider | `<integrity/status; no secret>` |
| Upload runtime/scheduler | `<schema/status>` |
| Accounts | `<platform + opaque test label only>` |

## 3. 执行摘要

| 层级 | PASS | FAIL | BLOCKED | NOT RUN | 证据说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| 无凭据源码回归 | 0 | 0 | 0 | 0 | `<commands and duration>` |
| 四页 UI / accessibility | 0 | 0 | 0 | 0 | `<viewports/themes>` |
| URL 下载 | 0 | 0 | 0 | 0 | `<source types>` |
| 编辑/封面 | 0 | 0 | 0 | 0 | `<cases>` |
| AI 听写/翻译/配音 | 0 | 0 | 0 | 0 | `<synthetic or real>` |
| Workflow 恢复/取消 | 0 | 0 | 0 | 0 | `<stages>` |
| Bilibili | 0 | 0 | 0 | 0 | `<local/submitted/review/public>` |
| 抖音 | 0 | 0 | 0 | 0 | `<local/submitted/review/public>` |
| 视频号 | 0 | 0 | 0 | 0 | `<local/draft_saved/submitted/public>` |

## 4. 命令结果

| 命令 | Exit | 结果/计数 | 时长 | 备注 |
| --- | ---: | --- | ---: | --- |
| `uv lock --check --offline` | `<n>` | `<value>` | `<s>` |  |
| `uv pip check` | `<n>` | `<value>` | `<s>` |  |
| `.\.venv\Scripts\python.exe -m compileall -q src` | `<n>` | `<value>` | `<s>` |  |
| 稳定 pytest 选择（粘贴完整命令） | `<n>` | `<passed/failed/skipped>` | `<s>` |  |
| `git diff --check` | `<n>` | `<value>` | `<s>` |  |
| 其他 | `<n>` | `<value>` | `<s>` |  |

## 5. 用例结果

每行对应仓库根目录 `TESTING.md` 或 `docs/UPLOADER_TEST_PLAN.md` 中的 ID。复制到 `validation/external/` 后仍按这两个仓库相对路径定位，避免报告随目录变化而指向错误文件。

| ID | 结果 | 实际观察 | 本地记录 ID | 远端动作/后台结果 | 证据引用 |
| --- | --- | --- | --- | --- | --- |
| `<ID>` | `<PASS/FAIL/BLOCKED/NOT RUN>` | `<observable fact>` | `<opaque IDs>` | `<none/submitted/draft_saved/review/public/unknown>` | `<authorized reference + sha256>` |

## 6. 缺陷记录

每个缺陷复制一节，按 `P0/P1/P2/P3` 排序。

### `<priority> <short title>`

- 用例：`<ID>`
- 首次出现：`<UTC timestamp>`
- 重现率：`<x/y>`
- 前置状态：`<runtime, account, workflow state>`
- 最小步骤：
  1. `<step>`
  2. `<step>`
- 预期：`<observable expected result>`
- 实际：`<observable actual result and exact safe code>`
- 本地 IDs：`<workflow/batch/project/task/plan/job; no secret>`
- 是否可能已发生远端动作：`<no/yes/unknown>`
- Provider/平台后台核对：`<result>`
- 脱敏日志事件：`<run_id + event names + timestamps>`
- 最小相关模块：`<paths/functions>`
- 初步判断：`<observation separated from inference>`
- 证据：`<screenshot/media/log reference + hash>`

## 7. 真实平台结果

| 测试编号 | 平台 | 本地终态/code | 平台稿件 ID | 接收 | 审核 | 公开 | 参数差异 | 核对时间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<id>` | `<platform>` | `<value>` | `<redacted if needed>` | `<yes/no/unknown>` | `<value>` | `<value>` | `<title/tags/cover/schedule/etc>` | `<time>` |

不要从本地 `submitted` 推断“审核通过”或“公开”。平台 ID 不便提交时记录受控证据引用及其 SHA-256。

## 8. 后续开发交接（必填）

- 当前可确认的最高能力：`<exact boundary>`
- 尚未验证：`<real AI / listening / platform / schedule / release / other>`
- 未解决缺陷，按优先级：
  1. `<issue and evidence>`
- 建议第一个开发切片：`<one bounded vertical slice>`
- 入口文件和符号：`<repo-relative paths + functions/classes; repository 外路径只写脱敏 label>`
- 必须保持的行为：`<recovery, identity, consent, unknown semantics, etc>`
- 不应新增的结构：`<duplicate service/queue/db/runtime/dependency>`
- 可重复的最小命令：`<commands>`
- 预期验收：`<observable pass conditions>`
- 推荐提交拆分：`<small commits>`

## 9. 测试者声明

- 我测试的是上方完整 Git SHA 与运行中的 `product_identity`。
- 我已区分 synthetic、本地、provider 接收、平台接收、审核和公开状态。
- 我没有在报告中加入凭据、Cookie、二维码、session 或未脱敏私有数据。
- 我没有为了得到绿色结果而重复未知远端动作。
- 我列出了全部已知失败、阻断和未执行边界。
- 我没有新增或修改 `tests/`，并记录了 staged scope check 的结果。

测试者/AI：`<name or system>`

报告提交：`<commit/PR>`
日期：`<date>`
