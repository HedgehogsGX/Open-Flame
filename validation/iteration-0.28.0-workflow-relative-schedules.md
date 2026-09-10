# Iteration 0.28.0 Workflow 相对发布时间预设

Validated: 2026-09-10. Source baseline: `7ef94f45288a4b5d1315696f744d3d0842ad91f2`; current working-tree evidence, not a frozen release receipt.

## Result

Workflow 预设现在可以按账号保存“创建流程后 N 个整小时发布”，不再把保存预设时的绝对日期重复用于以后输入的新网址。生产页面仍把最终请求转换成上传域原有的绝对 `publish_at_unix` 与本机时区偏移；Workflow、Upload 数据库和平台适配器的执行合同没有变化。

- 预设文件升级为 Schema 2。每条记录分别保存 secret-free profile、账号级 `schedule_policies` 及两个 SHA-256；精确 Schema 1 文件仍可读，下一次实际写入会原子改写为 Schema 2。
- 相对策略只接受 `account_id`、`platform`、`delay_seconds` 三个字段，延迟必须为整小时。Bilibili 为 7–8760 小时，抖音为 5–8760 小时，视频号为 5–671 小时；草稿账号不能带相对发布时间。
- Bilibili 与抖音向未来整分钟取整。视频号从真实 Unix instant 开始按该时刻的本机 UTC offset 向前搜索本地整点，并继续受既有 28 天限制约束；Adelaide 夏令时跳时和重复时段不会转换为不存在或含糊的 wall-clock 时间。
- 每次创建流程时，页面把 `scheduleBaseUnix` 与幂等 request key 一同冻结在 session storage。网络响应丢失后的重试复用相同锚点和相同具体 profile；页面不会因当前时间已经前进而在到达服务端幂等查询前阻止该重放。
- 相同平台的多个账号可保留不同的相对延迟、绝对时间或不定时。预设没有包含的平台恢复为“不定时”；账号参数不同会显示只读的“各账号不同”，须显式统一后才能覆盖。

实现只扩展现有 `WorkflowPresetStore`、严格 API model 和 `/workflows` 页面，没有新增数据库、表、服务、进程、线程、队列、runtime 或依赖。

## Validation

| Check | Result |
| --- | --- |
| `validation/local/validate_workflow_relative_schedule_presets_20260910.py` | **PASS**. 覆盖 Schema 1→2、摘要与 exact-key 拒绝、逐账号差异、缺少固定 anchor、布尔/浮点/越界延迟、草稿冲突、分钟/整点取整、28 天边界及 Adelaide 2026–2028 六次 DST 切换。 |
| `validation/local/validate_workflow_relative_schedule_browser_manual_20260910.cjs` | **PASS**. 当前生产 HTML 保存 Bilibili 7 小时策略；首次 POST 模拟响应丢失，`Date.now()` 前进 8 小时后重放仍得到相同 request key 和字节等价 profile。 |
| `validation/local/validate_workflow_relative_schedule_replay_browser.cjs` | **PASS**. 独立 Chromium 探针将时钟前进 3 小时，复核相同幂等键与具体 profile。 |
| Existing preset validators | **PASS**: preset store、边界和 API；来源标题 preset round-trip 保持。 |
| Targeted local-app/platform tests | **133 passed**. |
| Upload backend/platform/service review set | **174 passed** in the independent backend review. |
| Existing workflow smoke | **PASS**: full-chain、full-video、source-title 7/7、speech-rate 10/10、restart continuation、cancellation、checkpoint gap 和 upload attention。 |
| Static and browser syntax | **PASS**: `compileall`, extracted production inline JavaScript `node --check`, `git diff --check`; current production screenshot manually inspected. |
| `tests/test_ui_design_system.py` | **14 passed, 3 failed**. 三项仍是既有的三页导航断言遗漏 `/workflows`；按测试文件策略未修改，不把本组写成绿色。 |

两个 Chromium 探针、截图和临时抽取脚本均位于已忽略的 `validation/local/`，不进入提交。`tests/` 没有新增或修改文件。

## Evidence boundary

以上检查使用临时预设文件、合成账号/能力、loopback 页面和模拟的响应丢失。没有执行真实 URL 下载、OpenAI 请求、账号登录、扫码、平台上传、定时触发或公开发布；平台是否接受具体时间仍需以最终冻结构建和获授权测试账号分别验收。

这是 0.28.0 发布后的源码里程碑，不替代 `0592b6f` 的 release receipt。新的发行结论仍需 clean frozen commit、匹配制品和独立包外 receipt。
