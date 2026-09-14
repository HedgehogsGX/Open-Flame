# 0.28.0 异步完成等待与诊断日志测试维护

日期：2026-09-14（Australia/Adelaide）。维护基线为
`0dfcf82b7896e2a61b1201f3f974a5f7adf3dcdd`；本记录随本次限定维护提交。

## 问题与改动

三个封面用例在 FakeBackend 登记请求后立即检查临时封面已经删除，但登记发生在整个
上传操作结束前。暂停 backend 的隔离探针使三个原函数确定性失败；候选等待本 job
进入 submitted 后，保留原有文件内容、参数和清理断言。

维护前 `0dfcf82` 的 [push CI](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34777870858)
四格 success；[PR CI](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34777873312)
在 Windows / CPython 3.13.14 的诊断日志 rotate=True 用例失败，结果为
2441 passed、1 failed、16 skipped。原 hosted 日志没有各子进程退出码或 stderr，
因此不能确定这次 hosted 失败的确切根因。

该用例验证六个并发进程写入的完整性和轮转；生产 1 秒锁预算可能在慢写入下耗尽。
在真实锁内受控增加 0.12 秒构造延迟后，精确原函数有 4/6 子进程失败，精确候选 6/6 通过。
本次只在该用例子进程中设置 10 秒锁等待预算，并在原成功断言后追加各进程退出码/stderr。
生产 1 秒上限、独立 busy-writer 超时用例及原成功断言条件保持。

| 现有文件 | 精确范围 |
| --- | --- |
| tests/test_upload_platform_parameters.py | 三个现有函数各增加一条本 job submitted 等待 |
| tests/test_startup_diagnostic_persistence.py | 一个现有函数的子进程预算与断言失败详情 |
| scripts/verify_commit_scope.py | 只接受下述完整增量/累计摘要 |
| AGENTS.md | 记录本次提交指令对应的限定例外 |

未新增测试、skip、fixture 或收集规则，未放宽原断言条件。用户收到完整 v3 方案后要求
“commit本次改动并pr”，本次据此应用已展示的精确方案；不构成其他测试维护的通用授权。

## 实际验证

应用到真实工作树后，在 CPython 3.13.14 / Windows 运行两个完整模块：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q tests/test_upload_platform_parameters.py tests/test_startup_diagnostic_persistence.py
```

结果为 **73 passed in 22.17s**，失败、错误和 skip 均为 0。
JUnit 记录的 SHA-256 为
`7e77262b53637ffe3d98be0d05150e514796cb431ad90afae907100f5f7d4593`。
这次是实际文件回归；此前提案的内存替换回归单独保留，不作为本次执行次数。
另运行既有 test_ci_contract.py，17 passed in 0.25s；CI 合同静态验证及 353 项源码清单检查通过。
73 个唯一用例与提案基线一致，各模块内部顺序保持；两次命令的模块先后顺序不同。

本次完整测试差分固定为：

- 增量 SHA-256：`848b90eda9f0e84d1f1071602c6f45d8a695a36ea73a11eb53c1de60c4944f15`。
- 累计 PR SHA-256：`3ab252285d0d0cde29e33a78d4bde2329823e989d3489688fd9141e167bfd143`。

提案阶段已验证原 gate 拒绝、拟议 gate 接受两个精确差分；额外修改任一测试则拒绝，
没有通用路径豁免。应用后仍须通过真实 staged gate 和提交对应的 hosted 检查。
原始提案、失败日志、脚本、JUnit 与正反验证保留在 ignored 验收目录，不进入制品。

上述旧 hosted 结果仅绑定 0dfcf82。新提交的 push/PR 结果以
[PR #2](https://github.com/HedgehogsGX/Open-Flame/pull/2) 的同提交检查及交付记录为准，
不得用本地 73 项或旧绿色 run 代替。生产源码、CI workflow、真实应用根和平台动作未改动。
本地恢复的独立证据见[进程与运行时记录](iteration-0.28.0-local-recovery-validation.md)。
