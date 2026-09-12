# Upload 人工核对控件的轮询保留

日期：2026-09-13（Australia/Adelaide）。起点为 `773dc5d`，开发分支为
`codex/architecture-reset-ci`。本切片只删除 Upload 页面渲染缓存键中的一个冗余字段。

## 触发与改动

任务为 `unknown` 时，读取本次 attempt receipt、勾选“我已在对应平台后台核对这次上传尝试”，
随后第一次轮询即使收到相同业务字段，也会重新构造详情、摘要、复选框和结论按钮。
外层任务卡片保持；勾选值和焦点原本能恢复。本轮没有观察到确认值丢失，也没有测量性能收益。

原因是 change handler 已同步本地确认集合和按钮禁用状态，而 `renderJobs` 又把同一个
`acknowledged` 值写入用于识别服务器内容变化的 signature。移除 signature 中该字段后，
事件处理器继续同步控件；job、attempt revision、媒体状态等真正改变时仍会重建对应内容。
人工核对、receipt CAS、重新创建草稿与上传确认的 API、请求字段和服务端限制保持。

没有新增服务、数据库、Schema、runtime、依赖或自动化测试提交。四域 Schema 仍为
Download / Editing / Upload / Workflow `11 / 4 / 4 / 3`，preset 为 2，上传备份格式为 3，
CLI 为 14 个。

## 验证

所有临时脚本与原始结果在 ignored
`validation/local/architecture-reset-20260912/ci-recovery-773dc5d/`：

- `ui-ack-probe/result.json`：原实现失败；不改变确认值时节点保留；去掉账号排序和时间字段
  变化后原实现仍重建；只改变缓存键的候选通过原有节点保留断言。
- `ui-ack-browser-v2/browser-result.json`：实际 Chrome 152、1280×720，使用生产 API、隔离
  SQLite 和合成 backend。原页面四类子节点 ID 变化，候选在勾选、取消勾选和自动轮询后均保持。
  勾选值与焦点保持；取消勾选后两个结论按钮禁用。没有 browser POST、真实平台或下载动作。
  页面字节 SHA 已核对，候选 console 无 error / warning；临时标签页和服务均已关闭。
- `maintenance-full-v2/result.json`：**2,442 passed / 16 skipped，exit 0**。这是包含本源码
  改动和一份未应用测试维护草案的完整隔离副本；16 个 skip IDs 与原基线相同。该结果不是
  当前 tracked tests 或 GitHub hosted CI 的绿色证明。
- `final-source-checks/result.json`：源码修复应用后，当前工作区的 7 项现有 CSP、HTTP 静态资源、
  共享样式与主题脚本回归通过；标准 pytest 仍为 1 collection error、exit 2。源码 AST、diff
  格式检查及未应用维护补丁的 `git apply --check` 通过。tracked tests 和实际范围门禁保持未改。

候选生产文件 SHA-256：
`658ea3eaee22dfc285dfccf0368937a042ebfb26888af7019efd5a41d049bbef`。
候选 Upload HTML SHA-256：
`6e7015c0fdd182243f963666a41d501ef4a64e8c4e4253359a74ea09567eb7c2`。

## 发行清单补充复验（2026-09-13）

`f51416d` 的源码发行预检失败为 `unlisted_documentation_link`：HANDOFF 和验证索引已引用
本记录，但 `release-files.txt` 漏记它。已补齐这一项；预检通过，明确源码清单共 341 个文件，
没有修改生产代码、测试文件或发行校验规则。现有 `test_release.py`、`test_release_wheel_smoke.py`
与 `test_release_windows_smoke.py` 合计 **153 passed**。

该增量的前后预检和原有回归记录位于 ignored `validation/local/release-f51416d/`。它证明当前
发行清单的闭合与对应回归，不替代后续实际制品构建、独立安装或完整 hosted CI。

## CI 维护仍待批准

原 `773dc5d` 完整诊断是 2,091 passed / 329 failed / 16 skipped / 1 collection error。
隔离草案迁移了 44 个历史测试文件并新增 2 个显式 fixture helper；没有删除用例、增加 skip，
或改变 pytest 收集范围。迁移覆盖合法 loopback / CSRF 会话、公共 Module 注入点、完整 backend
identity / evidence、receipt reconciliation、当前 Schema / 版本和现行页面交互。

`approval-proposal/complete-maintenance-proposal.patch` 同时包含拟议 AGENTS 例外和范围门禁。
门禁只接受完整测试差分 SHA-256
`5ddff9cedcdfe0f5dd4a67beb9cab8566e1a743cebd3a37695ce4caeb3297a14`；
部分补丁、内容变化或额外测试均拒绝。7 个隔离门禁场景和 staged / range-tree 摘要一致性检查通过。
这些测试与规则修改均未应用、未提交、未获批准。

下一步须取得这一份具体测试维护补丁的明确批准，再应用并签名提交，执行当前分支的标准回归与
Windows / Linux × Python 3.12 / 3.13 四格 hosted CI。源码和 wheel 独立验收、最终 receipt
及分支/PR 交付也仍未完成；不合并 main。
