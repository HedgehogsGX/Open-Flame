# Iteration 0.28.0 自动流程运行就绪度界面验证记录

日期：2026-09-09

范围：发布后开发工作树；`/workflows` 在创建单 URL 流程前显示下载 Worker、AI runtime 与凭据、上传 runtime 与调度器、所选上传账号四项即时状态。

结论：自动流程页现在可以在开始下载前说明当前本机为什么能或不能继续自动执行。实现复用现有同源状态与查询接口，没有增加聚合 API、数据库、线程或后台服务。上传账号和状态接口会沿用既有 UploadService 惰性初始化；自动流程页原本就会读取账号，因此本切片没有新增这一生命周期。状态仍是创建前提示；服务端创建、账号会话绑定、worker 领取和真正执行时继续按原合同失败关闭。

## 实现范围

- 下载状态读取 `GET /api/v1/operations/runtime`。只有本次 supervisor 管理的 direct Worker 处于 `online`、网络下载已启用、队列未暂停且心跳仍在期限内时显示“可执行”。页面把请求耗时和本地经过时间计入心跳年龄，每两秒更新；`starting`、`paused`、`stale`、`stopped`、`check_only`、外部状态未知和接口失败均不会显示绿色结论。
- AI 状态同时读取 `GET /api/v1/edits/ai/runtime` 与 `GET /api/v1/edits/ai/capabilities`。没有使用按合同始终为 `false` 的 `runtime.ready`；要求 runtime `integrity=verified`，当前表单选择的 `transcribe`、`translate`、`dub` 模型各自具有可确认的 authorization，并且标准音色仍属于当前配音能力。启用 AI 后，翻译与配音必须成对启用；远程能力还会等待本次数据外发确认。`unverified` 只表示本地 runtime/授权准备完成，页面继续明确真实 provider 健康要由首次实际调用验证。
- 上传状态读取 `GET /api/v1/uploads/status`，要求 `backend.ready=true`、当前 scheduler 为 `running` 且 worker thread 存活。旧 runtime、缺失 runtime、standby、faulted 或接口失败分别显示未就绪/未知，不会把缓存完整性检查当成真实平台接收。
- 账号状态继续复用 `GET /api/v1/uploads/accounts`。页面只列出 `lifecycle_state=active` 且 `auth_state=ready` 的账号；选择 1–3 个后显示所选数量，并分别标示 Bilibili、抖音和视频号是否已有 ready 账号。
- 总结只计算当前表单需要的条件：下载、上传和所选账号始终需要；只有启用自动翻译或配音时才把 AI 加入必需项。状态不会禁用或替代原来的创建动作，避免把有时间差的前端快照伪装成原子检查。
- 各探针独立失败；一个接口不可用只把对应卡片标为“状态未知”，不会擦除其他已取得的判断。每次请求最多等待四秒，并用逐探针序号丢弃晚到的旧响应，避免挂起请求停止轮询或旧状态覆盖新状态。两秒下载状态刷新不重建表单或账号 DOM，输入、账号选区和焦点保持不变；真正的就绪度变化通过独立 live region 播报。

## 本地验证

以下专用脚本和截图保存在 ignored 的 `validation/local/`，不会进入 Git 提交：

- `validate_workflow_readiness_browser.cjs`
- `preset_browser_fixture.py`
- `preset-browser-page.html`
- `preset-browser-fixture.json`
- `workflow-readiness-browser.png`

结果：

```text
workflow-readiness-browser: PASS
workflow-multisegment-browser: PASS
workflow-preset-browser: PASS
263 passed in 38.30s
102 passed in 13.29s
tests/test_api.py: 19 passed, 1 failed（既有 OpenAPI 版本断言仍期望 0.27.0；当前项目版本已为 0.28.0，本切片未修改测试）
uv pip check: 24 packages compatible
compileall: PASS
Node --check（从当前 `preset-browser-page.html` 提取的内联脚本）: PASS
git diff --check: PASS
```

浏览器切片在合成同源响应下验证：四项配置就绪、未选账号、选择账号、启用 AI、云端外发确认、当前模型选择失效、下载队列暂停、单个上传状态接口返回 503、挂起请求超时、晚到旧响应不覆盖新状态、状态 live region、轮询期间 URL 输入与焦点保持，以及 320 px 无页面横向溢出。没有访问 OpenAI 或真实平台。

## 未完成边界

- 当前默认应用根实测仍缺少 AI runtime 和 OpenAI provider 凭据，上传 runtime 仍需要从旧格式重建；因此新的诊断页会如实显示未就绪，不能把本切片解释为当前机器已经能执行完整自动链。
- runtime 完整、心跳和账号 `ready` 都不能证明目标 URL 可下载、provider 可调用、配音质量可接受，或平台已经审核、公开、按时发布。真实 OpenAI、真人试听以及 Bilibili、抖音、视频号的投稿与后台核对仍未执行。
- 液态玻璃生产视觉仍等待用户从三套真实候选中选择。本切片只使用现有共享设计系统组件；它不代表四页液态玻璃升级已经完成。
- 当前发布后提交没有新的 clean release receipt；历史 receipt 只证明其绑定的冻结提交。
