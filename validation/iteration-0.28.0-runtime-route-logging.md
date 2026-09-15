# 0.28.0 补齐业务路由的结构化日志模板

日期：2026-09-14（Australia/Adelaide）。基线 `d970bce18f23d205476b3ca86be4cb2465947dcb`；
修订 `runtime_logging.py` 的 Git blob 为 `d8906d4c419ae938b9d500d5168c8e6de48d10da`。

## 可复现问题

[恢复副本检查](iteration-0.28.0-application-data-recovery.md)中，`GET /api/v1/workflows/presets`
成功返回，但对应请求日志被替换为 `runtime_log.event_rejected / invalid_record`。
这会丢失合法请求的排障记录，并产生误导性的 ERROR 事件。

ignored 探针从当前应用的 OpenAPI 路径与根路由取得 **112 个已注册模板**，逐一通过真实
`RuntimeLogger.emit` 校验；其中 8 个失败，其他字段完全相同。由此缩小为静态路径清单缺项，
不需要改变字段校验、日志写入方式或业务 handler。第一次枚举未展开延迟 include 的路由，
在进入比较前停止；补用应用 OpenAPI 的已注册路径后保留了稳定的 8 项失败报告。

缺少的模板是 Editing 的 invocation 列表/核对，Upload 的 attempt 读取/核对，以及
Workflow 的预设列表、单项、由预设创建和取消。

## 修正与边界

只在既有封闭 `_ROUTES` 集合中增加这 8 个精确模板，例如 `{preset_id}`、`{job_id}`。
不接受任意路径，不序列化实际 ID、URL、查询参数、请求体或秘密；没有改动 handler、
Schema、任务调度、确认或远端执行路径。

## 验证

- 同一个路由探针：**112 个模板全部通过**。实际标识路径、带 token 查询的路径和完整 URL
  三个负向输入仍被拒绝，测试 canary 没有出现在日志，网络尝试为 0。
- 现有 `test_runtime_logging.py` 与 `test_observability.py`：**15 passed in 3.29s**。
  没有修改或新增 tracked tests。
- 在恢复副本上普通 Start，用 GET 实际访问预设列表、不存在的预设、AI invocation 列表和
  不存在 job 的 attempt；分别得到预期的 200/404/200/404。每个响应的 request ID 都对应
  一条 `http.request_completed`，route 为精确模板，实际标识与查询 canary 均未写入。
- 本次独立 run_id 中拒写事件为 0。正常 Ctrl+C 后 3 个自有进程退出、端口释放，四库
  quick_check/外键检查通过，受管媒体摘要保持，原应用根数据库摘要未变。

首次恢复检查中的日志错误仍保留，没有因后续修复把原失败记录改为 PASS。
真实 HTTP 只覆盖上述四个读取入口；其他模板由注册路径与日志边界探针覆盖，未执行真实
reconciliation、取消业务流程、AI 或平台动作。最终提交仍须核对自身 CI。
