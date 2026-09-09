# Iteration 0.28.0 — 编辑式玻璃四页生产前端证据

日期：2026-09-09。基线提交：`0edef99f974823677cd481691f6c279b5882a43f`。本记录覆盖基线后的前端设计切片；最终提交身份以本轮提交和远端核验为准。

## 1. 范围与结果

用户选定方向 C“编辑式玻璃（Editorial Glass）”作为 Open-Flame 整体前端风格。本轮把该方向落入下载 `/`、编辑 `/edits`、上传 `/uploads` 与自动流程 `/workflows` 四个生产页面，而不是只修改静态候选：

- 浅色使用暖纸画布、深色使用暖炭画布；深珊瑚承担小字号品牌层级，亮珊瑚与紫色只用于双瓣火焰和环境装饰。
- 页面标题改为编辑式大字并对关键短语使用本地系统衬线回退；主按钮改为墨色语义 token，蓝色单独保留给键盘焦点。
- 顶栏、页面说明、主命令卡、登录/恢复与持续反馈使用选择性玻璃；普通内容卡、诊断、正文、日志与二维码保持实底。
- 桌面 hero 使用非对称双列；1023px 以下回到单列。767px 以下顶栏把品牌/主题与四个功能入口分行，导航可随文字缩放继续换行，避免隐藏入口或遮挡持续状态。
- `prefers-reduced-motion`、`prefers-reduced-transparency`、`prefers-contrast: more`、无 `backdrop-filter` 与 forced-colors 均有明确降级；当前浏览器矩阵实际切换前两项，其余降级由静态规则审查覆盖。

设计基准已同步到 `docs/DESIGN_SYSTEM.md` 1.4、`docs/design-preview.html` 与根目录 `AGENTS.md`。预览仍为合成界面；生产四页才是本轮浏览器验收对象。

## 2. 行为合同

四个生产模板只在原页面标题中加入同文 `aria-label` 与一个无事件的 `<em>` 排版标记。与基线比较，四页所有 `id`、`data-*`、事件监听数量、动态宿主、控件顺序、表单和内联 JavaScript 均未改变。轮询期间的输入、选区、焦点、扫码、来源清空、详情展开、任务选择和逐项确认仍沿用现有实现。

共享 CSS 的最终静态审查没有发现 P0～P2 问题。320px Chrome 实测中，顶栏高 112px，窄屏 `#message` 为非粘性且与顶栏重叠 0px；四项导航 `clientWidth = scrollWidth = 278px`。次级按钮边界对背景的浅/深对比为 4.406:1 / 4.141:1，当前导航边界为 4.361:1 / 4.174:1。正文、辅助文字、品牌文字、输入边界和状态文字的静态对比均达到各自 4.5:1 或 3:1 目标。

提交前审查曾在原矩阵没有组合覆盖的 320px + 200% 根字体场景发现品牌/主题重叠与导航裁切。最终 CSS 将 767px 以下顶栏改为独立导航行并允许导航换行，359px 以下隐藏品牌文字但保留品牌标记；扩展后的 12 个响应式文字缩放交叉场景全部通过。

## 3. 当前浏览器矩阵

ignored validator：`validation/local/validate_editorial_glass_four_pages_browser.cjs`。ignored 输出：`validation/local/editorial-glass-four-page-qa-20260909/`。二者不会进入源码或发行包。

| 范围 | 当前结果 |
| --- | --- |
| 四个生产路由 × 1440×1000 / 768×1024 / 320×900 × 显式浅/深主题 | **24/24 PASS** |
| 四页 768px reduced-motion + reduced-transparency | **4/4 PASS**；CDP 媒体查询真实匹配，动画/过渡移动项为零，3～4 个可见玻璃面均为实色且 `backdrop-filter: none` |
| 四页 1440px、根字体 32px（200%） | **4/4 PASS**；无整页横向溢出 |
| 四页 × 320 / 768 / 1024px、根字体 32px（200%）交叉 | **12/12 PASS**；品牌、主题与导航不重叠，四项导航及当前项完整可见，无整页横向溢出 |
| 浅/深主题的画布/正文 token 差异 | **12/12 PASS**；四路由 × 三个核心视口分别比较 `--of-canvas` 与 `--of-text` |
| 每例共同断言 | 当前导航正确且完整可见、四项导航目标高度至少 44px、键盘 `:focus-visible` 为 3px、无可见元素越界 |
| 默认材质断言 | 24 个 core、4 个桌面 200% 字体与 12 个响应式 200% 字体案例的可见玻璃面有 computed blur |
| 降低透明度断言 | 4 个 reduced 案例的可见玻璃面均为实色且 `backdrop-filter: none` |
| 浏览器侧网络与运行错误 | 零非 loopback HTTP(S)、WebSocket 或 Worker 观测；零 console error、page error、请求失败和 HTTP 4xx/5xx |

最终结果为 **44/44 PASS**。浏览器服务使用独立临时数据根与 `NoRemoteBackend`，端口在完成后关闭。最终浏览器报告 SHA-256：`73fa0045a17a8b2014c263cada436da8b4e36a6a88112e00e5f78b2acbacc2d3`；validator SHA-256：`7218f09911efbcc319e596b3fb33ef8771e54607ea8ee93502ca81889d24e42d`。

报告绑定以下生产字节；四项 HTML 值是对应 HTML 常量的 UTF-8 内容摘要，不是 Python 文件摘要：

- `open-flame.css`：`de81b305839107433b01369c9e078f33bf47c75e51bb3510b924cde5380c9639`
- 下载 HTML：`993a3d7d327264c158e9bbce5f08eb7b7f3cca4c8d550bd428dad4da93dc6bc6`
- 编辑 HTML：`9fd284c2b16a2d907047e70b6ab180518b2e9944d5722c36af81e1b8d47ea55d`
- 上传 HTML：`3b9fd49a0eb28613c548dddaae4c6c6534d8b6f5c4e2512943faa7a3a798a6d3`
- 自动流程 HTML：`57dd045bf2da902fe97460c73a02a34d7e47c6060b3f2e939a44e80660172c85`
- `open-flame-shell.js`：`2acab08f0f9fd39dff5e92465949b0632d542e5831d5d5b74aaf4443effe6747`

## 4. 回归与静态检查

- 四页当前内联 JavaScript：`node --check` 全部通过。
- 既有浏览器回归：`validate_editing_speech_rate_browser.cjs`、`validate_workflow_preset_browser.cjs`、`validate_workflow_multisegment_browser.cjs` 与 `validate_workflow_readiness_browser.cjs` 四个 ignored Chromium validator 全部通过。
- 相关既有 pytest：`.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_ui_design_system.py tests/test_batch_assets_ui.py tests/test_upload_ui.py tests/test_worker_runtime_ui.py tests/test_progress_api_ui.py tests/test_default_credentials_ui.py` 得到 `115 passed, 3 failed`。三项失败都是 `test_pages_share_local_shell_navigation_and_theme_controls[download|editing|upload]` 的历史三导航集合未接受早已进入生产的 `/workflows`；本轮没有新增或修改任何 `tests/` 文件。共享样式的辅助功能/响应式合同本身通过。
- `.\.venv\Scripts\python.exe -m compileall -q src\video_download_control`、`uv lock --check --offline` 与 `uv pip check`：全部通过；当前 25 个锁定包可离线解析，24 个已安装包兼容。
- 当前生产 CSS、shell 与四项 HTML 常量摘要均与上方 ignored 浏览器报告绑定值一致；修改文档中的相对链接检查通过。
- `git diff --check`：通过。

## 5. 证据边界

本轮没有调用 OpenAI、登录账号、下载互联网媒体或向 Bilibili、抖音、视频号发送数据。44 张截图与合成 API 状态证明当前生产页面的本地渲染、主题、视口和降级行为，不证明真实模型质量、费用、平台参数接受、定时执行、审核或公开发布。当前源码也尚未形成新的 0.28.0 clean release receipt。
