# 0.28.0 固定上传存储负载与 CI 后续

日期：2026-09-14（Australia/Adelaide）。源码固定为
`9822454462264297e83521b7c523cce557dd0c3f` 的 clean detached checkout。
本记录分别描述存储运行检查和同提交 CI；两者不能相互替代。

## 运行条件与负载

通过该副本的普通 `start_open_flame.py` 启动新隔离应用根，端口 18880，上传预留 512 MiB。
解释器使用已有项目环境，工具使用此前已验证的固定工具目录；本轮没有重新执行独立 Setup，
因此不是新的安装 receipt。原用户应用根没有参与。

建立 4 条来源记录，每份为 2 MiB 合成字节，受管总量固定 8 MiB。
这些字节用于存储复制，不是可解码视频或真实下载样本。完成 6 轮预热后：

- 每 2 秒删除一份受管媒体，再精确恢复同一份内容。
- 恢复同时并发读取 storage、来源分页、任务分页和 health。
- 每轮核对 4 份媒体、8 MiB、零孤儿、零 unsafe entry 和空 incoming。
- 每约 30 秒采样 3 个固定进程；预先约定单进程增长上限为私有内存 64 MiB、
  句柄 32、线程 8。上限没有在观察结果后调整。

## 有界结果

定时阶段 **600.77 秒、300 轮、0 失败**。HTTP 计数 **2,153**，包含准备、预热和结束核对，
不包含后面的草稿检查。下表为相对预热后基线的采样最大增长，不是逐时刻峰值或长期泄漏证明。

| 进程 | 私有内存增长 | 句柄增长 | 线程增长 |
| --- | ---: | ---: | ---: |
| control | 5,251,072 bytes | 0 | 2 |
| local-worker | 73,728 bytes | 0 | 2 |
| local-app | 73,728 bytes | 0 | 2 |

负载结束后，同一 HTTP 实例创建一个 `unchecked` 合成账号记录和一个本地上传草稿，再取消草稿。
没有请求登录、账号 check 或投稿确认。最终保留 1 个 canceled job、0 个 account operation、
0 个 upload attempt；4 份受管媒体与原合成输入的 SHA-256 均一致。

Ctrl+C 正常停止后，`local_app.stopped` 的 reason 为 `normal`；3 个自有进程退出、端口释放。
PTY 的实际退出码为 1，未记作 0，也未使用强制终止代替正常退出。
Download 和 Upload 两个已创建数据库均 `quick_check=ok`、外键错误为 0；下载 job/attempt 为 0。
incoming 为空；3 份运行日志共 883,555 bytes，ERROR/CRITICAL 和 HTTP 4xx/5xx 事件均为 0。
本次没有采样日志字节基线，不据此宣称日志增长已受压测上限验证。

原始证据保留在 ignored 本地验收区。核心报告 SHA-256：

| 报告 | SHA-256 |
| --- | --- |
| load report | `bc5c0ebab3a3fe2620e554bac0a03fcfe21f17f35ec08538a19970d5bcfeb9db` |
| post-load | `e0f1a300ee792c584292c4e37c5b62748b1668abe44980fd017131e650746abd` |
| shutdown | `e2fef6d4d937edae96dbfa8de1ed4b5ca4dbd40ee8c9df4be7483480093392c0` |

## 同提交 CI 与可复现失败

2026-09-14 查询确认：
[push run 34771038229](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34771038229)
四格 success；[PR run 34771040471](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34771040471)
为 3 success、1 failure。PR #2 未合并，main 仍未受保护。

失败 job 为 Windows / CPython 3.12.10：**1 failed、2441 passed、16 skipped**。
`test_tencent_dual_cover_ratio_short_title_and_content_label` 等到模拟 backend 的请求列表非空，
就断言两张临时封面已经删除；这个观察点早于 backend 返回和服务清理。

ignored 探针在原用例暂停模拟 backend，稳定得到同一封面存在断言失败：此时 job 仍 running，
封面仍供 backend 使用。放行后进入 submitted，两张临时封面正常删除。
源码也确认清理 finally 先于结果落库，故不为此改变生产顺序或提前删除正在使用的文件。

拟议修正只增加一行等待当前 job 的 submitted 状态，再保留所有原有封面、参数与定时断言。
内存中替换该函数后，原文件 **40 passed in 10.39s**；收集数 40，仅替换 1 个函数。
暂停 backend 的探针也验证修正后等待完成再通过；两次探针的网络尝试均为 0。
精确增量/累计差分及门禁方案已在 Git 外冻结，并验证额外一行改动仍被拒绝。
本记录冻结时，新的测试维护例外尚待用户确认，仓库测试和门禁未修改，PR 失败仍未关闭。

## 后续范围

本次只补齐固定小负载下的 Upload 存储运行、局部资源增长、草稿取消和正常停机证据。
真实媒体容量、两个实际下载槽与上传写入竞争、完整恢复故障矩阵、长期运行、AI 和平台执行
仍需各自验收；没有新增总配额、跨进程原子空间预订或自动清理能力。

下一步先处理上述限定测试维护并核对新提交 CI；全应用保护、实际根升级、真实业务样本与
最终同提交制品/receipt 继续保留在[执行计划](../docs/FOLLOW_UP_EXECUTION_PLAN.md)中。
