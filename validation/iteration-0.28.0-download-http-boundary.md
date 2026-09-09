# Iteration 0.28.0 下载 HTTP 边界验证

日期：2026-09-10

修复起始提交：`936750f75918e241c5daab0df1aa1b13e9bec5d2`

范围：下载首页与非 Editing / Upload / Workflow 的顶层 `/api/v1/*` 请求边界。

## 修复内容

顶层下载控制面现在与编辑、上传和自动流程共用同一个本地 HTTP guard 实现，但保持独立的进程内令牌和拒绝码。它要求唯一且可解析的 loopback `Host`，限制 `Origin` 与 `Sec-Fetch-Site`，并对下载域所有非 `GET` / `HEAD` 请求校验唯一、ASCII 且常量时间比较的 `X-Download-CSRF`。被拒绝的请求统一返回 `403 download_request_forbidden`，不进入业务路由。

`GET /api/v1/session` 只对合法本地请求返回当前下载会话令牌。每个 `create_app()` 实例生成新令牌；上传、编辑和自动流程令牌不能替代它。下载页默认禁用“创建批次”，只在 session 读取成功、令牌非空且全部为 ASCII 后启用；六类页面写操作由集中 `fetchJson()` 附加 header。会话失败时页面显示明确错误，提交继续禁用。

下载域的 8 个写路由全部受保护：创建批次、导入批次、恢复队列、重置平台熔断、重试/取消 Job，以及取消/重新发现 Input。不属于下载域的 `/health*`、`/assets/*` 和 `/openapi.json` 不被这一 token 策略扩张。运行时日志 middleware 保持在请求边界外层，拒绝结果仍有 `X-Request-ID` 与受限路由日志。

共享 guard 默认继续将受保护响应收敛为 `Cache-Control: no-store`。顶层下载域会保留已有且包含精确 `no-store` directive 的更严格策略，因此成品返回的 `private, no-store` 不会被降级。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| `validation/local/validate_download_http_guard.py` | **PASS**。覆盖 session/security headers，非本地与重复 authority header 拒绝，8 个写路由的缺失/错误/重复/非 ASCII/query-only token，合法 Origin/Fetch-Site，App 与四域 token 隔离，Upload QR GET 特例、Workflow 精确根路径和 `private, no-store` 保留。全部拒绝路径的下游调用数为 0，外网尝试为 0。 |
| 当前生产页 Chrome / Playwright | **PASS**。真实本地服务启动后，session bootstrap、四项 security header、创建批次的 CSRF header、session 失败时按钮禁用、320 px 无横向溢出均通过；只访问 `127.0.0.1`，外网尝试为 0。截图留在 ignored `validation/local/`。 |
| 下载页 inline JavaScript `node --check` | **PASS**；Node `v24.16.0`。 |
| Editing / Upload / Workflow 定向回归 | **120 passed, 2 failed**。两项失败均仍是历史 `Editing Schema == 1` 断言与当前 Schema 4 不符，本切片没有修改这两个合同。 |
| `python -m compileall -q src`、`uv lock --check --offline`、`uv pip check`、`git diff --check` | **PASS**。 |

现有下载 API 回归在默认 `http://testserver` 且没有获取新 session 的情况下会被新边界正确拒绝；这些历史断言不能作为弱化产品 Host/CSRF 边界的理由。仓库当前策略禁止新增或修改已跟踪 `tests/`，因此本切片没有将测试环境特例写入产品码，也没有把全量 pytest 记为绿色。新的可执行探针与原始输出只保留在 ignored `validation/local/`。

## 证据边界

本轮只创建了临时本地数据库和一个 queued 合成批次，没有启动下载 Worker，也没有执行真实 URL 下载、OpenAI 请求、账号登录、二维码扫描、平台上传、定时或发布。该结果仅证明本文件所在源码提交的本地 HTTP 边界与页面启动行为，不是新的发行 receipt 或真实平台证据。
