# Iteration 0.24.4 上传数据生命周期与离线交付工程证据

日期：2026-09-07
状态：**0.24.4 T10 外测冻结源；制品级结论须与包外 release receipt 联合使用**

## 1. 身份与结论边界

| 字段 | 当前值 |
| --- | --- |
| 开发分支 | `codex/uploader-first-platforms` |
| 本轮起点 | `9927cfa2d6c0be62d5dea741ae5f426b653457dd` |
| 工作树版本 | `0.24.4` |
| 下载数据库 | Schema `11` |
| 上传数据库 | 独立 Schema `2` |
| `source_commit` | 不嵌入被打包文件；由 clean detached checkout 的包外 release receipt 记录 |
| `product_identity` | 由 `release-manifest.json` 生成，并由包外 release receipt 绑定 |
| source ZIP / sdist / wheel / manifest / SHA256SUMS | 实际文件摘要与验收结果仅记录在包外 release receipt，避免自引用 |

本记录证明 0.24.4 冻结源中 T14 必要生命周期切片、T07 停机上传备份/新根恢复、T08 有界 synthetic 韧性切片，以及 T09 工作流与本地合同检查的工程范围。它本身不证明某一组构建制品已通过 T10；该结论还必须由包外 release receipt 绑定 clean commit、全量结果、制品和独立验收。它也不证明真实平台、真实账号、生产数据、长期运行、异机灾备、Linux/NAS 上传或最终 Apple 风格整站升级。

0.24.4 的开发和验证没有执行真实登录、扫码、下载、平台草稿保存或投稿；自动化 backend 均为 synthetic/fake，本轮也没有修改用户已有上传账号、媒体、数据库或 runtime。Bilibili、抖音、视频号仍是首批平台，未扩展其他上传平台。

## 2. T14：账号与受管媒体生命周期

Upload Schema 2 在 Schema 1 业务表基础上增加账号生命周期与断开时间、来源媒体状态与删除时间，并保留历史账号、来源、任务、operation、request 和 retry 关系。精确 Schema 1 由单次事务迁移；新库直接原子建立 Schema 2；Schema 2 打开时执行精确表/列/索引/外键与数据库检查。未知、损坏或更高版本失败关闭，不通过补表或改 marker 猜测修复。

当前本地实现包括：

- “断开本地账号”采用两步确认。正在运行上传时拒绝断开；操作先保留平台、账号备注和墓碑时间并撤回该账号未执行的 `queued` 确认，再删除本地登录秘密。清理失败时账号仍保持 disconnected，记录 `account_disconnect_cleanup_failed`，页面显示风险并允许重试；只有清理成功才报告 `local_login_removed=true`。
- 已取消登录的迟到成功回调不能把墓碑账号恢复为可用；重启会再次 scrub 已断开账号的凭据，失败仍保持显式错误而不会称为已移除。断开只处理本地会话，平台侧撤销仍由用户在平台账号安全设置完成。
- 上传页显示受管媒体字节数、登记记录、present/missing/deleted/changed/unsafe、未登记文件、磁盘剩余空间和 64 MiB 上传预留线。高频轮询只检查路径、普通文件和大小；创建、确认、重试、删除和实际上传前重验 SHA-256，完整校验发现等长字节变化后会在当前服务进程显示 `changed`。审计不自动删除文件。
- 媒体删除采用两步确认，只删除上传域受管副本。`draft`、`queued` 或 `running` 引用会阻止删除；历史名称、大小、SHA-256、任务关系和删除时间保留，用户原件与下载成品不动。
- 缺失或已删除来源不能创建、确认或重试上传。用户显式选择文件后，只有大小和 SHA-256 完全匹配才恢复原 source ID；恢复不会确认或执行旧任务。
- 内容变化、link/reparse point、hard link 与不安全路径保持失败关闭；删除事务失败会把暂存副本放回，不留下 `.delete` 文件。

本切片没有实现 hash 去重、总配额、可配置预留阈值或自动孤儿清理。这些仍是 T14 后续优化，不影响 T07 当前备份语义冻结。

## 3. T07：secret-free 停机备份与新根恢复

新增独立入口：

```powershell
uv run video-upload-backup create `
  --source-upload-root C:\vdc\data-uploads `
  --backup-target D:\vdc-backups\upload-backup-20260907

uv run video-upload-backup restore `
  --backup-root D:\vdc-backups\upload-backup-20260907 `
  --restore-upload-root C:\vdc-upload-restore-drill
```

上传和下载使用不同格式：`video-upload-backup` 只包含 Upload Schema 2、非秘密账号历史、sources、jobs、operations、requests/retry 和已登记的 present 媒体；`video-download-backup` 仍只处理下载 Schema 11 与下载资产。两者任一个成功都不能代表另一域已备份。

### 一致性与 activity lock

每个上传根在父目录使用 sibling `.<root-name>.activity.lock`。当前代码的锁语义为：

- FastAPI lifespan 从控制面启动到关闭持 shared lease，即使上传服务尚未按需初始化、上传根尚未创建也一样；
- started active 与 standby `UploadService` 持 shared lease，停止服务的方法在操作期间取得短 shared lease；
- create 在源上传根持 exclusive lease，并同时持旧 `.worker.lock`，覆盖静态数据库快照、媒体复制、内部审计和一次 rename 发布；
- restore 在不存在的目标上传根持 exclusive lease，覆盖 staging、恢复状态策略、二次审计、同步和发布；
- 路径按实际物理位置规范化，Windows 长路径/8.3 短名不能取得两把独立锁或隐藏 source/target overlap；不安全、非空、hard-linked 的锁文件拒绝使用。

因此 create 前必须正常停止使用源上传根的**所有当前应用与 active/standby 服务**；restore 目标也不能被当前应用占用。activity lock 是当前版本之间的协调合同，不会自动停止旧版本应用、手工 SQLite 连接或自写文件 writer；这些 writer 必须由操作者另行停止。锁冲突不是通过删除或替换 lock 文件来解决。

### 格式、排除与恢复策略

create 从稳定的 main/WAL 字节快照生成静态 `payload/uploads.sqlite3`，不在源目录打开 SQLite 或新建 SHM。只复制 `media_state=present` 且 size/SHA-256 与登记一致的媒体。manifest 以规范相对路径、大小和 SHA-256 覆盖 metadata 与每个 payload；sidecar 再覆盖 manifest。它能检出意外损坏，但不是数字签名。

当前格式固定 `secret_material_included=false`，排除 `private/`、`runtime/`、`incoming/`、锁、账号登录秘密、操作临时文件、未登记文件和非 present 媒体，没有含秘密选项。路径逃逸、大小写冲突、symlink/reparse point、hard link、special file、Windows alternate data stream、文件不稳定、Schema/外键/业务关系异常、目标存在或重叠均失败关闭。

restore 只写入不存在的新独立目录；发布前检查 manifest/hash/inventory、精确 Schema 2、`quick_check`、外键，以及账号、来源、任务、operation、request/retry 的业务语义。状态策略为：

| 备份状态 | 恢复状态 |
| --- | --- |
| job `running` | `unknown / interrupted_result_unknown` |
| job `queued` | `draft / restart_confirmation_required` |
| operation `queued` 或 `running` | `failed / operation_interrupted` |
| active account `ready` 或 `checking` | `unchecked / account_missing` |

恢复不构造上传 backend，不登录、不扫码、不上传，也不会把 runtime 或账号秘密带入新根。失败会清理私有 staging，现有 backup/source 不变，最终 restore root 保持不存在。

## 4. T08：有界 synthetic 韧性切片

新增 `tests/test_upload_resilience.py`，覆盖三类有界场景：

1. Bilibili、抖音、视频号三个账号的已确认任务严格串行，最多一个 backend 调用活跃；每个 job/account/platform 恰好调用一次，完成后服务仍能创建并取消新本地草稿。
2. 300 轮 status/accounts/jobs/sources/storage 本地读取，共 1500 次视图访问；每轮有 45 秒硬上限，核对线程、Windows handle、tracemalloc retained/peak 和临时文件。
3. 在第二次 1 MiB 读取处确定性中断媒体复制；异常后 DB/source/media/incoming 均无残留，原始 2 MiB+1 文件保持原大小。

该文件共执行 4 轮并全部通过。记录到的单轮范围为 5.672～6.109 秒、线程 `1→1`、Windows handles `211→211`、retained `0～7928` bytes、peak delta `261400～269188` bytes。这是同一台开发机的小型合成预算，不是长期 soak、生产容量或跨主机性能承诺；本计划列出的全部浏览器超时、数据库故障与进程树组合也不能由这三个测试代替。

## 5. T09：无凭据 CI 合同

新增 `.github/workflows/ci.yml`，配置 push、pull request 和手动触发的 Windows/Linux × CPython 3.12.13/3.13.14 四格矩阵。工作流只有 `contents: read`，checkout 不保留凭据；三个外部 action 按 commit SHA 固定，uv 固定为 0.11.25。每格执行 locked development sync、`uv pip check`、完整 pytest、Node identity 和 `git diff --check`。

`scripts/verify_ci_contract.py` 核对精确 action pins、矩阵、权限、无 credential-bearing trigger、locked install、完整测试和失败传播；`tests/test_ci_contract.py` 在内存中破坏 action pin、权限、trigger 和失败传播，确认 validator 会拒绝弱化。工作流与测试都不保存真实平台 Cookie/账号，不扫码、不上传；包安装所需网络与测试阶段的 synthetic/no-remote 边界在 [CI 指南](../docs/CI.md)分开说明。

当前托管状态必须保持如下：

| 项目 | 状态 |
| --- | --- |
| GitHub hosted 四格 workflow | **NOT RUN** |
| hosted runner/Python/Node/uv 身份记录 | **NOT RUN** |
| required checks | **NOT CONFIGURED** |
| branch protection | **NOT CONFIGURED** |
| T15 root/getfacl/network namespace/容器证据 | **NOT RUN** |

本地 validator 通过不代表 GitHub 已启用门禁；普通 hosted Linux 的 skip 也不能替代 T15 目标环境验收。

## 6. 当前本地验证

以下结果是 2026-09-07 冻结准备阶段的定向快照；T10 clean commit 的全量、静态与制品检查另由包外 release receipt 记录。

1. 上传 Schema/lifecycle/activity/backup/resilience/service/API/backend/UI/login/runtime/download-integration 精确集合：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests\test_upload_schema2.py `
  tests\test_upload_lifecycle.py `
  tests\test_upload_activity_lock.py `
  tests\test_upload_backup_restore.py `
  tests\test_upload_backup_cli.py `
  tests\test_upload_resilience.py `
  tests\test_upload_service.py `
  tests\test_upload_api.py `
  tests\test_upload_backend.py `
  tests\test_upload_ui.py `
  tests\test_upload_login.py `
  tests\test_upload_runtime_integrity.py `
  tests\test_download_upload_integration.py
```

结果：**383 passed in 100.29s**。

2. activity lock、上传备份与 CLI 定向复验：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests\test_upload_activity_lock.py `
  tests\test_upload_backup_restore.py `
  tests\test_upload_backup_cli.py
```

结果：**133 passed in 49.68s**。

3. 下载备份、发行、wheel、CI、validation、deployment、Linux assets、API/capability 组合：

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests\test_backup_restore.py `
  tests\test_release.py `
  tests\test_release_wheel_smoke.py `
  tests\test_release_windows_smoke.py `
  tests\test_ci_contract.py `
  tests\test_validation.py `
  tests\test_deployment_candidate.py `
  tests\test_linux_acceptance_assets.py `
  tests\test_api.py `
  tests\test_capability_api.py
```

结果：**287 passed、4 skipped in 44.13s**。4 个 skip 是当前 Windows 主机上需要 root/POSIX/getfacl 的环境合同，不是通过。

本地 synthetic 浏览器检查覆盖上传页 1440、390、320 px 宽度，无页面横向溢出；轮询保留输入、选区与两步确认，restore cancel 焦点、长状态换行、44 px 输入/选择器/返回链接、reduced motion、forced colors 和可见键盘焦点已核对。该观察没有访问真实平台，也不等于 T16 的 1024/768、200% 文字缩放、完整读屏及最终整站验收。

## 7. T10 外测冻结及后续入口

源码与文档采用以下无自引用流程冻结：

1. 运行 T10 冻结前全量 pytest、compileall、`uv pip check`、Markdown/本地链接、source snapshot、隐私/发行清单和 `git diff --check`。
2. 固定 Git commit，在 clean detached checkout 中核对工作树与该 commit 精确一致；不要再改被打包文件。
3. 从该 checkout 构建源码 ZIP、sdist、wheel、`release-manifest.json` 和 `SHA256SUMS`，核对包内没有账号、数据库、媒体、日志、runtime 或私有路径。
4. 在独立目录执行 Setup、重复 Setup、Start check、普通 Start/stop、wheel 14 个入口及上传 no-remote 保护；在 `release` 目录外的 receipt 中记录 source commit、完整 product identity、实际制品 hash、环境与结果。
5. 只有上述 T10 完成后，才把固定包交给 T11/T12。真实三平台上传、当前下载矩阵、T13 回报修复、T15 环境支线与 T16 整站升级分别保留自己的 PASS/FAIL/BLOCKED/NOT RUN 证据。

没有对应包外 receipt 时，不得把 0.24.4 源码、定向测试或本机 CLI 演练称为已通过 T10 的制品；即使 receipt 完整，也不能把它称为完整灾备、真实平台能力或 T16 后的最终交付包。
