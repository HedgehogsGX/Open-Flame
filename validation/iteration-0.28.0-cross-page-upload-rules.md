# Upload / Workflow 跨页纯规则收敛

日期：2026-09-12；基线 405107e；开发分支 codex/architecture-reset-ci。

两页的 Unicode 字数、标签拆分/校验、提前量、日期/DST 与发布时间规则现在共用
uploads.web_rules 中的 8 个纯函数。页面继续负责 DOM、能力值、错误说明、预设、
冻结时间锚点与显式确认；没有新增运行服务、依赖、Schema 或前端框架。

Upload 创建页改为读取有效的 schedule_min_lead_seconds，与 Workflow 一致；缺失
或无效能力仍回退原有平台默认值。后端实际能力值没有改动。必填空固定日期仍拒绝，
视频号草稿保持不可定时，整点/28 天与夏令时歧义检查保留。

共享片段保留在现有一个 inline business script 内。两页先输出 'use strict' 再拼
共享函数，修复审查中发现的 directive 失效；现有 CSP 对组合后的完整脚本重新计算
精确 SHA。发行清单已加入新模块，并按该清单重建独立源码目录，确认两页及 CSP
能够从该目录导入。没有保留另一份可达的旧日期/标签算法。

三个完全相同的 helper 及反向标签判定有实际收敛；新增模块 30 行，两页净减 17 行，
三份生产文件合计净增 13 行。主要收益是维护同一份规则，不能称整体代码行数下降。

修复后当前验证：

- 3,501 项三时区纯规则对照通过，含预期的合成 60 秒能力差异及冻结锚点。
- 两页 strict 反馈从隐式全局写入恢复为 ReferenceError；完整脚本语法和 CSP hash 通过。
- 实际 Chrome 152 的 Workflow 16 / Upload 12 个场景通过。覆盖日期/标签/预设、
  Tencent 模式、默认与合成提前量、暗色、320/390/1365px、200% 文字及轮询期间
  输入、选区、焦点、账号与来源清空。全部 HTTP 数据来自隔离同源路由，没有真实平台动作。
- 现有 Upload UI + design-system 回归 16 passed / 69 failed，没有相对审查快照新增的
  失败 ID。仅在 ignored 副本补标准 previousElementSibling 的模拟 DOM getter 后，
  Upload UI 为 55 passed / 13 failed；323 个断言 AST 不变，tracked tests 未改。
- 发行源码预检及按清单重建的 138 个 src 文件的独立页面导入通过。

审查时完整诊断的 2091 passed / 329 failed / 16 skipped / 1 collection error 属于
修复前工作区。该次新增的 53 个 UI 失败已由 harness-only 副本精确定位；它们不是
53 个生产故障。原始测试入口、其余失败家族、四格 hosted CI 与最终独立发行验证
仍未完成，局部对照通过不能替代它们。

ignored 当前证据：validation/local/architecture-reset-20260912/cross_page_rules/final-405107e/。
旧审查证据保持冻结。当前测试维护例外仍待用户答复；本切片未修改测试策略或门禁。
