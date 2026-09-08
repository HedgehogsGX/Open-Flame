# Iteration 0.28.0 自动流程多分段界面验证记录

日期：2026-09-09

范围：发布后开发工作树；`/workflows` 的 1–10 分段输入、预设回填、AI 外发摘要、三账号上限与 fan-out 状态呈现

结论：生产页面已完整暴露 Workflow Schema 2 的多分段输入；本地 Chromium synthetic 浏览器切片与既有回归通过。液态玻璃视觉仍等待用户从三套候选中选定，本记录不把现有共享样式描述成新视觉升级。

## 实现范围

- 自动流程页用现有 `.segment-row`、`.item`、`.segment-fields` 和 `.row-spread` 组件渲染 1–10 个有序分段。添加分段默认从上一段终点继续 60 秒；关闭分段会隐藏分段动作并表示让 AI 处理完整视频，未启用 AI 配音时则在页面内要求至少一个视频分段。
- 每个动态输入有稳定且唯一的 `id`、对应 `label`、原生数值边界和错误关联。删除后重新编号，并把键盘焦点移动到相邻分段或添加按钮。
- 提交前按 DOM 顺序核对每段至少 100 ms、0–7 天范围、非空名称、无重叠且保持输入顺序。启用自动翻译/配音时额外要求相邻分段精确首尾连续；不会自动排序、吞掉间隙或扩大 AI 外发范围。
- 添加、删除、修改、启停分段都会撤销已经勾选的 AI 外发同意。摘要显示完整视频、单段区间、连续多段的首尾区间，或明确标出含间隙的区间。
- 多分段预设会先完整校验，再一次性按原序回填。空分段预设恢复为“完整视频”；1–10 段不再被单段页面拒绝或只显示第一段。
- 上传账号在页面内限制为最多 3 个；达到上限后只禁用未选账号，仍可取消当前选择。帮助文字说明每个分段都会向每个所选账号建立任务。
- 流程记录显示计划成品数、已准备上传源数和 `segment × account` 上传任务数；上传确认按钮显示本次批量确认的任务总数。
- 相同轮询数据不重建记录 DOM。状态变化需要重绘时，同一操作仍存在则按稳定键恢复焦点；操作消失时聚焦同一流程卡，并通过独立 live region 播报真正的状态变化，避免两秒轮询打断键盘操作。
- 页脚已从 Workflow Schema 1 更正为 Workflow Schema 2。

## 本地验证

以下浏览器 validator 与截图保存在 ignored 的 `validation/local/`，不进入 Git 提交、sdist 或源码 ZIP：

```powershell
.\.venv\Scripts\python.exe validation\local\preset_browser_fixture.py
node validation\local\validate_workflow_preset_browser.cjs
node validation\local\validate_workflow_multisegment_browser.cjs
```

结果：

```text
workflow-preset-browser: PASS
workflow-multisegment-browser: PASS
```

多分段 Chromium 验证覆盖默认 0–60 秒分段、连续添加、唯一 label/input 关联、三账号选择上限、三段请求 body、重叠错误与 `aria-invalid`、三段 AI 预设、10 段上限、两段删到一段后的焦点、非 AI 零段本地拒绝、2×3 fan-out 数量、无变化轮询的 DOM/焦点保持、状态变化播报与同流程卡焦点回退，以及 320 px 无横向溢出。

源码、依赖与既有回归：

```text
python -m compileall -q src                              PASS
node --check <WORKFLOW_HTML inline script>              PASS
git diff --check                                        PASS
uv pip check                                            24 packages compatible
editing/upload/local-app existing regression            263 passed in 34.73s
current operations docs/release existing regression     102 passed in 13.45s
```

本次没有新增、修改或提交 `tests/` 文件。行为增量只由 ignored validator 覆盖。

## 证据边界

浏览器验证拦截并返回 synthetic 账号、AI capability、预设和流程记录，没有下载真实 URL、调用 OpenAI、读取真实登录态或访问 Bilibili、抖音、视频号。它证明表单和状态呈现能忠实构造并显示既有多输出合同，不证明媒体处理质量、平台接收、审核、定时发布或公开结果。

当前所有分段仍共用一次流程内的标题、简介、标签、封面、发布时间和平台参数；逐分段独立元数据需要另行设计 profile/schema，未在本切片中暗中加入。所有草稿仍由一次 `confirm_many` 共同确认。

液态玻璃候选只存在于 ignored 本地预览；生产 CSS 和四个页面仍保持现有已验收设计系统。最终视觉方向、真实 AI/三平台验收、新 clean release receipt 和 GitHub 推送均未由本记录完成。
