# 公共备份文件操作与领域策略分离

日期：2026-09-12；基线 `936d8ab`；分支 `codex/architecture-reset-ci`。

Download 和 Upload 的备份现在直接使用公开 `backup_files` Module。通用路径、文件身份、
复制、有限读取、散列、持久化和 SQLite snapshot 移出 Download backup；两域各自继续
拥有输入选择、格式、事务、数据库审计和恢复政策。公共模块只依赖标准库和
`managed_files` 的公开资源所有权原语，不导入领域数据库、graph、HTTP 或执行服务。

迁移删除 Upload 的 `_Entry`、`_entry_for_file`、`_copy_regular_file` 三个浅结构，合计
20 定义行；55 个 Upload→Download 私有调用、21 个私有名字归零。迁移本身为 34 个定义、
453 定义行，不是净删除 453 行。没有保留私有生产兼容壳或新增统一 backup engine。
旧公开 Download `BackupRestoreError` import 与 Upload 错误别名仍指向同一公共类。
领域结果 DTO、BackupFileEntry 字段、异常文字和格式不变。

## 保留的领域责任

- Download 保留 `BEGIN IMMEDIATE`、pending asset intents 拒绝、graph/ready asset 审计，
  以及 logs、temporary、assets/.staging 排除。
- Upload 保留 activity lease + worker lock、五次稳定 DB/WAL 字节快照尝试；SHM 仅作为
  指纹观察，原库不交给 SQLite 打开，不创建源 SHM。公共 SQLite snapshot 只处理已选定
  的临时 snapshot。
- Upload 格式 1/2 的严格输入只在 staging 中迁移；格式 3、Schema 4 receipt、unknown、
  重新确认和账号恢复规则继续属于 Upload。秘密/runtime/incoming 排除不变。
- 祖先 links/reparse、canonical、单链接、Windows named streams 和发布后父目录同步
  等检查原样保留，没有把强路径合同缩成只检查 leaf。

## 验证与当前失败

- 独立 AST 复核：34 个迁移定义、保留的领域定义和常量，在显式名称重写后全部等价。
  收尾只规范 LF 与末尾空行；另有 AST 前后等价记录。
- 独立进程导入公共模块或 Upload backup，不再加载 Download backup/database/graph，
  错误别名和 copy 实现身份一致。
- 公开入口故障反馈 **15/15 + 13/13 + 11/11 = 39 项通过**，包括已有/替换目标保护、
  descriptor 包装失败、正常 close 失败、工作主异常与双 close 失败、SQLite 第二连接失败。
- 仅在 ignored 临时测试副本更新旧 helper import 和 monkeypatch 目标，不改任何断言：
  Download backup **22 passed**；Upload backup **129 passed、7 failed**，合计 56.05 秒。
  六个失败仍期望 Schema 3，一个把版本改成当前合法值 4 后期望拒绝；均在前一基线中。
- 仓库标准 `pytest -q` 仍在旧私有 helper import 处收集失败：**1 error，2.22 秒**。
  明确的诊断模式 `--continue-on-collection-errors` 得到 **2170 passed、250 failed、
  16 skipped、1 error，370.94 秒**。原 248 个失败 ID 全保留；新增两项仍 patch 旧
  `_common` / `_copy_regular_file`。22 个 Download 用例未收集，不能计为继续通过。
- 当前基线提交的 hosted run `34689817723` 四格在 offline pytest 失败；它不包含这份
  当时未提交的迁移。源码预检和 CI 定义检查通过，不构成完整 CI 或发行通过。

本机反馈、AST 对照、测试副本差异和 JUnit 保存在 ignored
`validation/local/architecture-reset-20260912/backup_files/` 及对应的
`architecture-review-20260912-936d8ab-working` 审查目录。原始审查记录保持冻结。
本切片没有新增或修改 tracked tests。AGENTS 的测试维护例外尚未答复，临时副本不是
仓库验证结果；完整 CI 仍是主目标，不能通过恢复私有壳、弱化领域合同或跳过测试来关闭。

没有真实下载、FFmpeg、登录、上传、OpenAI 或发行安装操作。后续继续集中 Editing render
retry forest、跨页纯规则、CLI 支持与当前交接，然后完成完整测试/四格 CI 和最终制品验证。
