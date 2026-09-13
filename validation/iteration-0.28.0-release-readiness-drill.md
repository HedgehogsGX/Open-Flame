# 0.28.0 隔离启停、旧上传数据副本与恢复说明

日期：2026-09-14（Australia/Adelaide）。安装与数据演练绑定
`1540a7ed55e809c9f86a2eeff87855cf7ff2963b` 的 clean detached worktree。
后续页面修订仅改变 `workflows/web.py` 的一段说明；验证对象分别记录，不能合并为发行 receipt。

## 工程基线

2026-09-13 16:36:02 UTC 读回 GitHub：上述提交的
[push run 34767925055](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34767925055) 和
[PR run 34767928010](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34767928010)
均 completed / success，Windows/Ubuntu × CPython 3.12.10/3.13.14 的四格分别通过。
GitHub author/committer 均为 `novahanser`，签名 verified / valid。
PR #2 open、未合并；main 仍为 `d48132637ce94a8b0b41bc2a965d24e27ac62aff`，
未观察到分支保护或应用规则。门禁规则仍只是准备方案，未配置、未验证阻断行为。

`7575773` 的五件制品与安装 receipt 仍只属于[上一基线](iteration-0.28.0-release-readiness-baseline.md)。
本轮没有构建新的五件发行制品，也没有合并或正式发布。

## D2：隔离安装、普通启动和停止

- 以系统 CPython 3.13.14 执行固定源码的 `Setup-Open-Flame.cmd --yes`，退出码 0。
  在新的目录安装主环境与固定 Windows 工具；没有安装可选 AI/Upload runtime。
- 普通 `Start-Open-Flame.cmd` 使用新的 canonical app root、端口 18874 和
  `--no-open-browser`。运行记录从 16:15:58 到 16:26:20 UTC；ready 为 16:16:03。
- `/`、`/edits`、`/uploads`、`/workflows` 和 capability snapshot 均 HTTP 200。
  产品身份为
  `0.28.0+build.sha256.6042f081df25eda59c821e6ae8359702ee7dd48a996ccc6e4a112f31c0ca9d5f`。
  此提交只改文档，产品字节与上一基线一致，但提交和安装证据分别记录。
- 四域数据库 `quick_check=ok`、外键错误 0，所检查的业务表全部为空。
  AI/Upload 显示 `runtime_missing`，没有账号或可执行业务。
- Ctrl+C 后日志先记录 `shutdown_requested`、`claim_gate_stopped`，最终
  `local_app.stopped / reason=normal`。三个自有进程均退出、端口监听为 0。
  Windows batch 出现 `Terminate batch job (Y/N)?`，确认 Y 后外层退出码为 1；
  本记录以应用正常停止事件、进程和端口证据判定，不将 batch 退出码改写成 0。

### 实际浏览器检查

浏览器使用上述空测试根，沿用现有 Editorial Glass。没有提交业务表单。

| 检查 | 实际观察 | 范围 |
| --- | --- | --- |
| 四页 × 跟随系统/浅色/深色 | 12/12 无横向页面或可见表单控件溢出 | 320 px viewport，根文字 32 px（200%），client/scroll 均 305 px |
| 减少动态效果 | 浏览器媒体偏好覆盖生效 | 实际 DOM 与截图检查；不代表所有动画时序均量测 |
| 键盘 | 32 个采样焦点均有可见 outline | 每页导航和首批控件的 8 次 Tab，未声称遍历所有控件 |
| 下载轮询 | 45.664 秒后名称、输入、焦点、选区、详情展开均保持 | 未提交下载 |
| 编辑轮询 | 36.894 秒后两个语言选择保持 | 无编辑项目 |
| 上传轮询 | 51.179 秒后标题、简介、账号名称、焦点、选区、详情展开均保持 | 未创建账号、草稿或登录 |
| 流程轮询 | 46.127 秒后名称、封面文字与比例保持 | 未创建流程 |

实际截图已在浏览器工具输出检查；采样 console warn/error 为空。
二维码、已有任务选择和真实账号状态保持没有测试素材，保持 NOT RUN。
视口、文字缩放、媒体偏好与主题已恢复；这组检查不代替真实业务矩阵。

## D3：实际旧数据的独立副本

先核对 canonical 实际根和现存写入方，以既有 activity/worker 锁维护停写窗口。
原上传库为 Schema 3；通过只读 SQLite backup API 生成一致副本，并复制受管 media/assets。
没有启动原根、构造原根 service/backend，也没有复制账号秘密或浏览器 profile。
所有副本和私有摘要均留在 ignored 本地目录。

| 子步骤 | 结果 | 证据 |
| --- | --- | --- |
| D3.1 上传库与受管文件保护副本 | PASS（限定上传数据库和媒体） | 1 账号、2 canceled jobs、2 operations、1 request、2 sources；2 个媒体文件共 112222 bytes |
| D3.2 既存副本 Schema 3→4 | PASS | 业务记录与媒体一致，未补造 receipt；最新结构、quick_check、外键和重复 ensure 幂等均通过 |
| D3.3 格式 3 backup→全新根 restore | PASS | 4 个 payload 文件共 220134 bytes；完整 manifest 和媒体校验；账号登录态失效，其他预期记录保持 |
| D3.4 实际根升级 | NOT RUN | 原库摘要前后相同；尚需完整保护、维护窗口与恢复执行范围 |

备份与恢复 manifest SHA-256 均为
`c50f0ee2e73e4db29b4f69eb042dcdfcc94e2194357dd23d93a5bba879baa41b`。
验证器阻止 socket 连接，网络尝试 0。Schema 4 备份前只在从未启动的副本中初始化
维护所需的 worker lock；没有改写实际根锁或通过启动服务制造维护条件。

原应用根的各域保护范围仍分别登记：

| 对象 | 本次观察 | 保存/恢复状态 |
| --- | --- | --- |
| Download `data/control.sqlite3` | 存在；本次目录下还观察到 logs | 未制作或恢复 Download 备份 |
| Editing `data-edits` | 本次根目录下不存在 | 未验证其他根或 Editing 恢复 |
| Upload `data-uploads/uploads.sqlite3`、media/assets | 已保存数据库和受管文件 | 副本迁移及独立恢复通过；private、incoming、runtime 未复制 |
| Workflow / preset `data-workflows` | 本次根目录下不存在 | 未验证其他根、Workflow 或预设恢复 |

因此本轮不是全应用备份，也不证明原实例可以直接升级启动。
实际维护前还须停止所有领域写入方、建立完整数据库/受管文件/私有状态映射，
在受控本地位置保存私有材料和匹配旧源码/runtime，并先核对恢复后可能续跑的任务。
回退必须使用匹配旧版本的数据副本；已有新业务数据时不得直接覆盖。

冻结源码下运行以下现有回归，结果为 **231 passed in 66.09s**：
`test_upload_schema2.py`、`test_upload_schema3.py`、`test_upload_backup_restore.py`、
`test_upload_backup_cli.py`、`test_upload_service.py`。没有修改这些测试。

## D6 与 D7：恢复规则复验和页面修正

实际页面原说明声称“应用重启或重试后仍会再次停下”，与已有预授权原始 queued
任务重新校验后续跑的合同不符；同时将 `submitted` 写成平台返回的提交完成结果。
本次只修正文案，区分可续跑的原始预授权任务与手动、重试、unknown，并明确本地
投稿记录与平台接收/审核/定时/公开核对的边界。没有改变按钮、处理器、样式或授权规则。
修订后 `workflows/web.py` 的 Git blob 为 `286777fd582198831fbc7f13c9cf1e36ad0caec6`。

五组离线恢复场景复验通过：工作流策略、启动管理器、profile 绑定、当前平台参数复核、
adapter retry lineage；网络尝试 0。第一次运行有一个旧 FakeEditingManager fixture
缺少现已公开的 `resolve_ai_task_retry` 接口；保留失败结果，在 ignored wrapper 中补齐
fixture 合同后 5/5 通过，原断言不变。没有修改生产行为或 tracked tests 来适配探针。

页面改动在另一新空 app root、端口 18875 的普通 Start 中实际验证，包含浅/深色
320 px / 200% 与桌面复核；采样 console warn/error 为空。应用于 16:37:02 UTC
记录 normal 停止，三个自有进程和端口监听均为 0；batch 仍按上述 Ctrl+C/Y 路径退出。
本次另运行 `pytest -q tests/test_ui_design_system.py tests/test_upload_resilience.py`：
**20 passed in 21.87s**。

D2 资源采样中，control handles 190→208、threads 7→11、private bytes
59396096→62119936；Worker handles 165→165、threads 1→3；supervisor handles 176→175。
期间逐页启动了惰性领域组件，负载为空，不能据此判断泄漏或宣布持续压力测试通过。
固定任务量、约定持续时长及资源上限的 soak、较广故障注入和真实远端恢复仍未完成。

## 下一入口

1. 当前文案修正与本记录完成源码清单、链接、隐私、staged scope 和 whitespace 检查后签名交付。
   新提交的 CI 与最终发行证据继续单独取得。
2. 无新增阻断反馈时，按执行计划盘点配置流并实现可配置的上传剩余空间阈值；
   总配额、去重与自动清理分阶段决定，保留四域所有权。
3. 真实样本、账号动作和 AI 预算确定后，在固定候选执行 D4/D5；实际根升级、门禁配置、
   合并、最终制品与正式发行仍分别留有待办。本轮没有真实下载、AI、登录、上传或发布。
