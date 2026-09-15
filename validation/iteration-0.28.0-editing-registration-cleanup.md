# Editing 成品登记前的文件清理责任

日期：2026-09-12；基线 `7f35919`；分支 `codex/architecture-reset-ci`。

## 问题与改动

`complete_plan` 原来先复制成品，再校验 ordinal、duration、尺寸和 codec/container，
最后才把文件写入失败清理用的 records。元数据无效时，复制已成功，当前目标却不在
records 中，因而留下未登记文件。正常修正后再次完成也不会收回旧孤儿。

现在先校验不依赖复制结果的元数据，再复制。成功取得新目标后，立即加入 records，
之后才比较实际大小/hash 并进入登记事务。size/hash 两个失败分支改为复用同一处
records 清理，删除重复的按路径清理调用。复制失败时不会提前登记目标，已有文件和
UUID 碰撞保护继续由复制所有权合同保证。

没有改变合法成品、元数据错误码、实际大小/hash 要求、claim/CAS、事务、输出根、Schema、
失败后可重用的 claim 或平台调用语义。此改动只收敛已成功复制后的普通异常清理范围，
不宣称解决任意指令间中断或同权限恶意文件替换的原子性。

## 验证

- 实际公共 `complete_plan` 的 11 项反馈：修改前 9 项遗留文件，修改后全部通过。
  包含七个无效 metadata 字段、大小/hash 不匹配、错误大小字段类型，以及第二个成品
  metadata 失败。七个单成品纯 metadata 失败现在均在复制前拒绝；后续失败会清理
  本次已完成复制的文件，数据库未登记成品、claim 仍保持 running。
- 每个失败样例随后用同一 claim 完成正常成品，均无遗留孤儿。
- 既有七项复制所有权矩阵通过；真实第二次 `complete_plan` 的强制 UUID 碰撞仍保留
  第一个 ready asset 的记录和字节，取消注入后第二个 claim 可完成。
- 五个 Editing 既有测试文件原样运行：**71 passed、3 failed、2 skipped，7.61 秒**。
  三个失败 ID 均存在于 `4c0c119` 工作区完整基线，仍为旧 Schema 1 断言；两个跳过
  来自本 checkout 未安装 pinned Windows FFmpeg。没有新增或修改 tracked tests。

反馈、JUnit、失败对照和碰撞结果保存在 ignored
`validation/local/architecture-reset-20260912/editing_registration/`。
所有输入均为临时合成文件，没有执行 FFmpeg、真实下载、登录、上传或 OpenAI 调用。
完整 CI、公共备份文件迁移与最终制品验收仍未完成。
