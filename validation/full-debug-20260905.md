# Open-Flame 完整 Debug 核验报告

> 后续处置：这份报告保留 f12749f / 0.24.2 的原始发现与验证结果；八项问题的 0.24.3 修复及重新验收见 [修复记录](iteration-0.24.3-debug-fixes.md)。
日期：2026-09-05。范围：当前仓库的下载、上传、数据库与资产、启动停机、依赖、发行安装、页面、备份恢复和运维文档。

## 1. 结论与构建基线

**常规回归、当前精确包安装和本地浏览器主流程通过；仍有需要修复的异常恢复与完整性校验缺口。** 当前结果支持继续本地开发与受控测试，不能据此宣布三平台真实上传或生产稳定性已验收。

本次任务是核验并输出后续计划，保留受测源码作为复现基线。下列新发现尚未修复；对应修复、验证和交付顺序见 [完整后续执行计划](../docs/FOLLOW_UP_EXECUTION_PLAN.md)。

| 项目 | 当前核验值 |
| --- | --- |
| Git 分支 | `codex/uploader-first-platforms` |
| 本地与远端开发分支提交 | `f12749f1dc8d0a2ebbb805669004e69ce33666a7` |
| 远端 main | `50169ae38e3ce199326becb990806e6483cc589f`，仍为 v0.23.0 历史基线 |
| 应用版本 | `0.24.2` |
| 精确包身份 | `0.24.2+build.sha256.a15efcc694d4123724e5c0e9983cc16235a704dc9d9be576a315ff926c8dc3f6` |
| 数据库 | 下载 Schema 11；上传独立 Schema 1 |
| 开发测试环境 | Windows 11 x64，10.0.26200；CPython 3.12.13 |
| 源码首次安装解释器 | 普通 Setup 创建的独立环境，CPython 3.13.14 |
| 首批上传范围 | Bilibili、抖音、视频号；其他平台后续维护 |

没有执行真实平台视频下载、上传、投稿或平台草稿保存。联网只用于独立源码安装所需的锁定依赖；运行环境检查和媒体用例均不执行真实平台操作。

## 2. 本轮执行与结果

| 核验面 | 实际执行 | 结果与边界 |
| --- | --- | --- |
| 全量回归 | `python -m pytest -q --durations=15 --junitxml=...` | **1867 passed、8 skipped，229.54 秒**；107 个测试文件。未采集代码覆盖率，测试数不等于覆盖百分比 |
| Python 语法与发行清单 | 对清单中 Python 解析 AST，检查源码发行契约、路径与高置信度敏感信息规则 | **192 份 Python 文件、294 个受测发行文件通过**；静态规则不构成完整漏洞审计 |
| 开发依赖一致性 | `uv pip check --python .venv/Scripts/python.exe` | 23 个已安装包兼容。该开发 venv 无 pip 模块，使用 uv 完成检查，没有改装环境 |
| 下载工具 | 当前固定工具 verify | yt-dlp `2026.08.19`、FFmpeg/ffprobe `n9.0.1-11-ge47273f4d9-20260831` 校验通过，既有离线 smoke 标记有效 |
| 上传运行环境 | 对已安装可选环境执行 `runtime_setup --check` | 返回 `ready=true`。其覆盖范围存在 F-02 缺口，不能解读为全部可加载文件均已核验 |
| 当前源码包构建 | `scripts/release.py build`，使用锁定本地构建 wheel 缓存 | 同一精确包身份生成 source ZIP、sdist、wheel，并通过内置逐字节及 RECORD/许可清单检查 |
| ZIP 独立安装 | 当前 source ZIP → 中文/空格/`!` 路径 → Setup → 重复 Setup → Start `--check` | **通过，50.563 秒**；各步骤拥有的进程均退出，工具与环境重复安装不变，端口释放 |
| wheel 独立安装 | 当前 wheel → 新虚拟环境 → 依赖及 console scripts | **13 个运行依赖、13 个命令入口通过** |
| 正常应用生命周期 | 实际源码 `Start-Open-Flame.cmd`，独立 app root，18845，正常运行模式 | 控制面/Worker/supervisor 三进程同 run；浏览器实际打开下载和上传页面；无下载任务领取；停机事件为 normal，无强制停机，进程与端口均释放 |
| 全新安装上传页 | 打开可选环境尚未安装的新 app root 的 `/uploads` | 明确提示 `runtime_missing`，不自动安装、不自动登录、不产生媒体任务 |
| 浏览器下载主流程 | 18844 独立离线 fixture：无效输入、重复输入、原 ready 成品、用于上传入口 | 无效输入 `failed/invalid_url`、无 job；重复输入复用同一原件、未新增下载 job |
| 浏览器上传主流程 | 导入原件 → 选三平台 → 本地草稿 → 清空来源/刷新/详情折叠 → 取消 | 创建 3 份本地草稿，最终均 canceled；来源与详情选择保持，空来源阻止新建；无横向溢出，warning/error 为空 |
| 下载备份恢复 | 对上述真实登记的离线数据运行 backup CLI，恢复到新的独立目录 | Schema 就绪；3 batches、3 inputs、1 job、1 attempt、1 media asset 与原库数量一致；3 个资产文件字节一致 |
| 代码与异常路径 | 下载、上传、发布运维三路独立审查，针对具体发现建立离线复现 | 发现 F-01～F-04；常规测试未覆盖这些异常或防御性边界 |
| 文档与交付状态 | 受测清单 Markdown 相对文件链接、Git 状态、版本引用、部署配置 | 相对文件路径无缺失；另发现现行 Runbook 与真实 Worker 状态语义问题 |

正常启动通过 PTY 发送 Ctrl+C，随后回答 CMD 的 `Terminate batch job` 提示。外层 CMD 返回 1；应用日志明确记录正常停机、没有 `forced_shutdown`，三个原进程均已退出。该证据属于实际 CMD 和应用生命周期验证，不冒充物理键盘或 Explorer 双击验收。

## 3. 已确认的新发现

优先级：P1 为扩大真实测试前优先解决；P2 为可靠性或使用体验改进。以下均保留了触发条件，避免把有限场景扩大为所有用户都会遇到的故障。

### F-01 / P1：上传结果持久化失败会终止调度线程

- 位置：[上传调度与结果写入](../src/video_download_control/uploads/service.py)，`_run` 约 575 行、`_execute_operation` 结果事务；[缓存服务生命周期](../src/video_download_control/uploads/api.py)，`_LazyUploads.get` 约 91 行。
- 触发：后端已报告提交完成，最终 SQLite 写入抛异常，例如磁盘或 I/O 错误。
- 离线复现：合成后端执行一次并返回 `submitted`；只对最终写入注入 `sqlite3.OperationalError`。
- 观察：`backend_upload_calls=1`、`worker_running=false`、持久任务仍为 `running`、错误码为空。`_run` 没有单任务异常隔离，异常越过循环；Lazy service 继续返回已缓存对象，不因页面刷新自动恢复。
- 影响：页面与数据库可能长期保留“运行中”，后续排队任务停止处理；若真实平台已收到作品，不能靠再次执行补偿。
- 恢复证据：显式再次启动服务后，首任务成为 `unknown/interrupted_result_unknown`，第二任务退回 `draft/restart_confirmation_required`；后端调用仍为 1。现有安全恢复可复用，但 Lazy manager 不会自动触发它。
- 修复方向：立即停止领取并暴露明确故障；数据库恢复后先将不确定任务保守恢复为 `unknown`，保留原任务与远端核对要求。恢复策略须确保原任务永不自动重传。

### F-02 / P1：上传环境 ready 校验不覆盖完整可执行输入

- 位置：[运行环境校验](../src/video_download_control/uploads/runtime_setup.py) 74 行起、约 328 行起的 manifest 生成；[桥接加载路径](../src/video_download_control/uploads/bridge.py) 33 行。
- 触发：安装后，运行环境中出现未登记的 Python 源文件，或未被 manifest 覆盖的解释器依赖/浏览器文件发生变化。
- 离线复现：在独立测试运行时加入未列入 manifest 的 `source/patchright` 后，`inspect_runtime()` 仍报告 ready，桥接代码实际加载了这个未登记模块。当前仅逐一检查 manifest 已列出的条目，不比较实际文件集合；venv 和完整浏览器载荷也不在同等覆盖范围内。
- 影响：`ready` 的完整性保证弱于运行时真正会加载的内容；`runtime/source` 位于 Python 导入搜索路径前部，额外模块可能影响执行。
- 边界：这是本地运行目录可被写入后的完整性/漂移检测问题，**未证明远程入侵或越权**。可写 manifest 本身也不能充当可信签名。
- 修复方向：明确受信任的固定归档与依赖清单，核对允许的实际加载文件集合、额外代码、重定向与异常文件；区分合法缓存。校验缓存须同时考虑准确性和每次页面轮询的成本。

### F-03 / P2：原件下载端点接受同尺寸内容变更

- 位置：[原件路径核验与响应](../src/video_download_control/api.py)，`_registered_original_file` 约 254～305 行、`download_asset` 约 939 行；[登记查询](../src/video_download_control/repository.py) 451 行起。
- 触发：ready 文件被本地其他操作改写，文件尺寸不变。
- 离线复现：正常 video 分类成品经 Worker/AssetStore 登记后，把原件换成同尺寸不同字节；GET download URL 返回 **200 和变更后的内容**，资产列表仍显示原登记 SHA-256。
- 影响：提供给用户的下载内容可能与登记 hash 不一致。当前路径/链接/大小检查没有覆盖这个情况。
- 边界：没有证明跨目录读取或远程写入；上传导入另有 hash 重算，会拒绝该变更。
- 修复方向：选定可处理大文件的完整性策略，避免“先 hash，再按路径重新打开”的竞态；必须覆盖校验后替换、并发 Range 请求和中途取消。

### F-04 / P2：下载成品直导上传 API 缺少 media_kind 约束

- 位置：[登记查询](../src/video_download_control/repository.py) 451～490 行、[上传原件 resolver](../src/video_download_control/api.py) 650 行起、[导入端点](../src/video_download_control/uploads/api.py) 240 行起。
- 触发：数据库已登记为 audio 的 ready 原件具有 `.mp4` 后缀。
- 离线复现：使用真实 BatchService、WorkerRepository、AssetStore 与合成 adapter 登记该组合；资产列表返回 audio，但直接调用导入 API 返回 **201**。
- 边界：当前固定 yt-dlp/FFprobe 正常路径按扩展名分类，Worker 拒绝 probe/download 类型不一致；正常主流程不会自然生成该组合。主要影响自定义/未来 adapter 或异常历史登记，属于 API 防御性缺口。
- 修复方向：服务器按可信登记明确要求 video，拒绝非视频后不产生上传来源或残留文件；不能只依靠页面隐藏入口。

### F-05 / P2：真实 Worker 状态与页面文案不一致

正常 Start 的日志已证明 `worker.started/direct_network_enabled=true` 和 `claim_gate_activated`，但页面仍写“联网下载开关：未启用”“本机直连 Worker：可显式启动”。[api.py](../src/video_download_control/api.py) 579～587 行返回的是静态工具/隔离候选政策，[web.py](../src/video_download_control/web.py) 203、387～388 行将其显示在运行页面。

这不是 Worker 未启动的证据，是静态能力与本次运行状态的展示缺口。应建立有时效的 supervisor 状态通道，并区分 managed/direct/isolated/unknown；不能把 claim gate 的一个开关直接当作存活心跳。

### D-01 / P2：现行 Runbook 混有旧版本操作口径

[README](../README.md) 把 [Runbook](../docs/RUNBOOK.md) 指为当前运维入口，但后者标题仍为 v0.23.0，部分固定工具描述仍为旧 FFmpeg `n9.0.1-6`，Linux runtime contract 段仍写 `video-download-control==0.23.0`；实际锁与 runner 已更新至本轮版本。

应保留明确标记的历史证据，统一现行命令、版本与支持边界，并补充上传侧数据与可选运行时入口。测试交付还需同时记录固定 commit 和受控源码包 hash，不能仅靠可移动分支名或仅覆盖 Python 包的 product identity。

### F-06 / P2：最近 200 项限制会隐藏旧活动任务

- 位置：[上传来源与任务查询](../src/video_download_control/uploads/service.py) 350～352、439～441 行；[页面任务渲染](../src/video_download_control/uploads/web.py) 137 行起。
- 离线复现：数据库构造 201 项，最旧任务仍为 running；任务接口只返回最新 200 项，旧 running 不在列表内，关联旧来源也不在最近来源中。
- 影响：用户不能从当前页面找到该活动任务并取消；重建返回的已有后继离开最近列表时，也无法真正定位。没有分页或单项查询补足访问。
- 修复方向：活动/待处理任务始终可访问，历史分页，按 ID 解析任务及引用来源；不以无限增大 LIMIT 代替容量策略。

### F-07 / P2：上传库缺少既有结构的完整只读校验

- 位置：[上传库初始化](../src/video_download_control/uploads/service.py) 约 143～163 行。
- 离线复现：专用空白测试目录中构造 `metadata.version=1`、一个无关 valuable 表且缺少业务表的 SQLite；初始化 UploadService 后，代码就地补建 accounts/jobs/operations/requests/sources，而非拒绝这个不完整的既有库。
- 影响：版本号相同不足以证明既有库结构有效，残缺或混入其他结构的库可能被静默修改。此复现没有证明真实账号库已经损坏、数据丢失或存在远程攻击。
- 修复方向：区分新建空库与既有库；既有 Schema 1 先只读核验表/列/索引/外键及数据库健康，不将 CREATE IF NOT EXISTS 当作恢复工具。媒体清单核验与 T07 的恢复契约共同设计。

## 4. 已知未闭环能力与本次未验证范围

这些项目不是被本次常规回归判为成功的能力，也不都属于新增 bug。

| 项目 | 当前状态 | 后续需要的证据 |
| --- | --- | --- |
| 三平台真实上传 | 本轮均未执行；Bilibili 用户扫码 ready、三平台取码属于此前有边界的证据 | 各平台实际授权登录、媒体传输、平台接收及审核/公开状态分别回报 |
| 视频号平台草稿 | 仅本地草稿模式与执行契约的离线测试 | 真实上传后的 `draft_saved` 必须在视频号后台找到对应草稿 |
| 下载真实平台回归 | 六平台实现与不同旧构建样本证据存在 | 在新的冻结构建按平台验证，不能移用旧成功记录 |
| 上传备份恢复 | 下载备份故意不包含同级 data-uploads；文档仅提供停机后安全拷贝 | 上传库、媒体、任务链和可选账号秘密的恢复契约、独立目录演练 |
| 账号退出与媒体保留 | 无受支持的本地账号断开/凭据移除入口；每次导入独立复制，取消/终态不清理媒体 | 显式账号断开与媒体保留/删除、引用保护、磁盘用量/配额及孤儿审计；与上传备份格式共同设计 |
| Linux/Docker/NAS | 本机 Docker 不可用，WSL 未安装可用 Linux；8 项 POSIX 用例跳过 | 真正 Linux/root/getfacl/Unix socket 环境执行候选部署验收；当前上传运行时仍为 Windows 范围 |
| 持续集成 | 仓库未发现 `.github` 工作流 | 无账号凭据的 Windows/Linux 回归和发行检查；分支保护需另行配置 |
| 普通用户分发 | 源码 Setup/Start 已验证，依赖已有 Python | 全新普通用户机器、物理双击、升级/卸载、签名和独立 EXE 另行验收 |
| 容量与长期运行 | 有界列表/双槽/取消等有单测；本轮小型 fixture | 大量历史任务、长时间运行、磁盘/锁故障、低空间和大文件性能基准 |
| 可访问性与多尺寸 | 默认桌面浏览器操作及画面检查 | 键盘全流程、读屏、移动尺寸单独用例；本轮不作认证结论 |

## 5. 制品与证据索引

本轮验收制品由受测的 `f12749f` 源码生成，输出在本机 ignored `dist/Open-Flame-0.24.2-full-debug-20260905/release/`。

| 制品 | SHA-256 |
| --- | --- |
| `Open-Flame-0.24.2-source.zip` | `158328c76bfafa62a7660a8aaff8dcc113f1a793e2e928840a232cbd76f3afce` |
| `video_download_control-0.24.2-py3-none-any.whl` | `d722c75654783b1e39b74e0aac4019634b850e55e5743f41f1b960814a243a28` |
| `video_download_control-0.24.2.tar.gz` | `6fd84c72ec2e17b7e06116594cbc7dddcbb02ba4245d17c1e8b95775647a4ca8` |

原始本地证据：

- `validation/local/full-debug-20260905/audit-summary.json`：测试、语法、浏览器、正常生命周期、备份恢复汇总。
- `validation/local/full-debug-20260905/pytest-baseline.xml`：本轮完整回归与跳过原因。
- `validation/local/full-debug-source-20260905/report.json`：当前 ZIP 独立 Setup/repeat/Start 检查。
- `validation/local/full-debug-wheel-20260905/wheel-smoke-report.json`：当前 wheel 独立安装。
- `validation/local/full-debug-20260905/sqlite-final-write-repro/`、`runtime-extra-source-repro/`：上传故障注入证据。
- `validation/local/full-debug-20260905/repro_upload_final_write_failure.py`、`repro_runtime_extra_source.py`：可运行的两条上传故障复现。
- `validation/local/full-debug-20260905/repro_upload_list_capacity.py`、`upload-list-capacity-repro/result.json`：201 项列表边界复现与结果。
- `validation/local/full-debug-20260905/repro_upload_schema_readiness.py`、`upload-schema-readiness-repro/result.json`：既有上传库结构缺失时就地补建的离线复现与结果。
- `validation/local/full-debug-20260905/repro_ready_asset_same_size_tamper.py`、`repro_nonvideo_ready_import.py`：两条离线资产复现。

证据目录包含合成数据库、测试媒体、安装环境和日志，按现有忽略规则留在本机。本报告和后续计划是在受测制品冻结后输出，不包含在上述 ZIP 中；没有把旧包改标成新包，也没有在本轮提交、推送或发布制品。

清理例外：一份仅含合成数据的 `open-flame-upload-schema-audit-*` 临时目录被自动审批以 `blocked by policy` 拒绝删除；已保留且不重试，没有因清理受阻改变核验结果。
