# 0.28.0 CI 合同维护与故障修复

日期：2026-09-13（Australia/Adelaide）。本记录区分已批准测试补丁、实际托管结果与后续生产修复。

## 已批准的维护范围

用户明确回复“批准本次限定测试维护例外”。签名提交 `3e482f0a4df57fbcf719e73cea160e815f06a451`
已应用审查过的 44 个历史测试修改、2 个 fixture helper 和 2 个策略/门禁文件。
规范化测试差分 SHA-256 为
`5ddff9cedcdfe0f5dd4a67beb9cab8566e1a743cebd3a37695ce4caeb3297a14`；实际 staged 门禁通过。
此授权只覆盖这一份补丁，其他测试改动仍按 [AGENTS](../AGENTS.md) 管理。
`tests/` 仍排除于源码 ZIP、sdist 和 wheel，没有更改 pytest 收集范围。

## 实际结果与新发现

[3e482f0 的四格 CI](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34706714849)
已恢复正常收集。Windows / Python 3.12.10 实际通过 `2442 passed, 16 skipped`，用时 544.66 秒。
Ubuntu / Python 3.12.10 为 `30 failed, 2291 passed, 137 skipped`；Ubuntu / Python 3.13.14
为 `36 failed, 2285 passed, 137 skipped`。Windows / Python 3.13.14 的最终状态须从该运行复核。
这些是修复前的结果，不能当作本次生产修复后的验收。

本地第一轮全量为 `2441 passed, 1 failed, 16 skipped`：Windows 源码安装中断测试读取
自己的握手文件时出现 `PermissionError`；原用例独立重复 20 次均通过，保留偶发失败记录。
第二轮在资产磁盘满场景卡住，线程栈与 async await 链均指向响应发送前的断连监听回收。
重复不变的现有请求，第 18 次再次卡住；修复后同一重复器连续 50 次通过。
原全量进程经身份检查后终止，不能计为通过。

## 本次生产修复

| 问题 | 修复与边界 |
| --- | --- |
| 资产读取错误响应与断连监听互相等待 | 先完成 409 响应，再在既有 finally 中回收监听；保留 spool 关闭、断连取消与真实 409 语义。 |
| 同 inode、同大小的源文件变化未被识别 | 复用受管文件 signature，在 original 与辅助文件复制后复核路径、mtime、ctime；复制期间也复核时间戳，不改变已有复制入口参数。 |
| 暂存清理错误覆盖主错误 | 保留原存储/验证/中断异常，附加固定的清理失败 note；失败残留由已有受管恢复路径处理。 |
| Linux 上传备份拒绝正常 worker 锁 | 接受 POSIX flock 的空文件与既有单字节 `0`；Windows 仍要求单字节 `0`，并保留真实互斥、普通文件、单链接与身份检查。 |
| Python 3.13 子进程日志被拒绝 | 为现有固定可执行文件白名单补充 `python3.13`；任意文件名、路径和输出仍不可写入日志。 |

离线故障探针已分别观察到修复前失败与修复后通过；锁探针在 Windows 注入 flock 边界，
真实 Linux 内核锁的验证必须由后续 hosted CI 给出。既有相关回归第一组 210 项通过；
源文件变化补充修复后，资产存储与访问的 32 项现有回归通过。
四格 CI 以本记录后的实际提交运行作为最终依据。
本次未新增或修改 tracked 测试，没有真实平台下载、登录、上传或 OpenAI 调用。

## 分支目录清理

已删除 86 个可重建目录根、6512 个文件，合计 143429162 逻辑字节（约 136.8 MiB）。
删除前逐项核对忽略规则、tracked 文件、重解析点、运行进程和重复暂存副本的 SHA-256。
保留开发环境、源码、测试、发行制品、receipt、原始日志和已批准补丁；逻辑大小不等于
NTFS 实际释放空间。详细清单和本轮探针保存在忽略目录 `validation/local/cleanup-20260913/`。

## 后续验收

继续依据新提交的完整 CI 日志处理剩余故障。Windows 专用 fixture、目录同步故障注入等
候选维护必须保留安全断言，超出已批准差分时先准备具体可审查补丁。
当前不宣称四格 CI 全绿，不宣称已形成最终 clean release receipt，也未合并 main。

## 后续实际复验与短链接预算

`3e482f0` 的 Windows / Python 3.13.14 最终也通过 `2442 passed, 16 skipped`，用时
1277.48 秒。生产修复提交 `4b0cf3a8b38b236dcdb7fdee9781fec6cfffb6ae` 本地全量为
`2442 passed, 16 skipped`，337.63 秒；其资产提交/Worker 补充回归 47 项通过。
[该提交的 hosted CI](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34707909353)
两组 Linux 均为 `21 failed, 2300 passed, 137 skipped`，源文件变化、上传备份锁与日志问题
均已通过真实 Linux 执行。剩余错误集中在六个测试文件的平台假设与目录同步故障注入；
候选修改只在 ignored 目录，Windows 相关 246 项通过，尚无该候选的真实 Linux 验收。

另一个可独立复现的短链接边界：固定 monotonic 为 `144 / 97`，15 秒预算的浮点加减得到
`15.000000000000002`，严格 resolver 因此拒绝请求，API 的 queued_count 为 0。
调用前以配置预算做 min 上限后，同一探针得到 15 秒与 queued_count 1，现有 API/短链接
69 项回归通过。旧 CI 中两项短链接失败没有记录实际 timeout，因此不能单凭同症状认定
这就是它们的唯一原因。临时探针在 `validation/local/cleanup-20260913/`，未改 tracked 测试。

## 平台维护落地与 Windows 快照读取竞争

用户在收到七个测试/fixture 路径的补充方案后要求“继续修复ci,并确保其全绿”。本轮据此
应用已审查范围，签名提交为 `ac3532be91f632da8a09c543379b8ced24f6da7d`；`AGENTS.md`
记录这次继续执行的具体范围。六个测试文件与一个显式请求的共用 fixture 调整平台假设，
门禁只接受冻结差分，没有扩大 pytest 收集豁免或增加 skip。

增量测试差分 SHA-256 为
`b8b8a60ac0508bba00ef9fb2e5fb95c595da9e26f9e46b846a8aa88460e60679`；相对 main 的累计差分为
`7dedb2a7d5b5c5d6638097669c62c3e863cdbdb596b77ea9854c00e1bf2a2de1`。
实际范围、用例名称、skip 数量和安全断言检查见 ignored 的 `platform-applied.json`。
相关 246 项回归通过；该提交的本地全量为 `2442 passed, 16 skipped`，394.02 秒。

[ac3532b 的四格 CI](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34761461628) 已全部结束：

| 环境 | 实际结果 | 时间 |
| --- | --- | --- |
| Ubuntu / CPython 3.12.10 | 2321 passed, 137 skipped | 125.70 秒 |
| Ubuntu / CPython 3.13.14 | 2321 passed, 137 skipped | 127.13 秒 |
| Windows / CPython 3.13.14 | 2442 passed, 16 skipped | 546.05 秒 |
| Windows / CPython 3.12.10 | 1 failed, 2441 passed, 16 skipped | 424.98 秒 |

原有 21 个 Linux 平台 fixture 失败全部消除，跳过计数保持不变。Windows 3.12 的失败发生在
`test_draft_is_persistent_and_requires_explicit_confirm` 创建第二个 UploadService reader 时。
以相同 CPython 3.12.10 重放未修改的原用例，第 64 次复现 `upload_schema_unsupported`；
异常链实际指向源 `uploads.sqlite3-shm` 的 `PermissionError`。只保留空库、真实空闲 Worker
和第二个 reader 的精简探针在第 67 次也观察到同一错误，目标为源 WAL 文件。

修复只让 Windows 源 `-wal` / `-shm` 的 `PermissionError` 进入已有整份快照重试，仍限定
五次、每次间隔 10 ms。每次重新复制并执行文件身份、摘要、WAL 和精确 Schema 校验；
主数据库、临时目录或其他路径的权限错误保持立即拒绝。没有写回或修复原数据库文件。
加压探针还在 `read()` 调用处观察到不带文件名的 `PermissionError`，因此 sidecar 内容读取
也在最窄的读取边界转换为快照重试，目标临时文件写入不进入这项例外。
随后第 3110 次加压读取记录到普通 WAL 文件 `st_nlink=0`：SQLite 正在解除路径链接。
Windows 的零链接元数据现在按文件已消失抛出 `FileNotFoundError`，由 sidecar 的既有缺失
重试处理；主数据库缺失仍失败，硬链接、重解析点和非普通文件仍拒绝。

确定性探针分别覆盖带文件名的打开冲突、不带文件名的内容读取冲突及零链接窗口，共十五项：修复前单次
冲突即失败；修复后 WAL/SHM 单次冲突均恢复，持续冲突均在第五次失败，主数据库读取
无权限只尝试一次。首次修复后原失败用例连续 250 次通过；现有 Upload Service、Schema
2/3、备份恢复与 activity lock 回归为 `234 passed`，86.99 秒；内容读取补充修复后同集合
再次 `234 passed`，84.26 秒。零链接分类补充后，14 项现有 Schema/数据库回归通过，3.82 秒。
临时探针和完整异常链位于 ignored 的 `validation/local/ci-continue-20260913/`。

更高频的诊断轮询仍可能耗尽原有五次快照一致性重试；这个有界失败保护没有放宽。
上述结果只覆盖各自源码状态。最终交付必须核对后续提交的四格 CI 和同一提交制品 receipt，
不能把 ac3532b 的三格通过解释为全绿。没有执行真实下载、登录、OpenAI 或平台发布。
