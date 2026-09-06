# Iteration 0.25.0 T16 生产前端与本地 G6 证据

日期：2026-09-07
状态：**T16.1～T16.7 本地开发范围与 G6 已完成；最终制品结论须与包外 release receipt 联合使用**

## 1. 身份与范围

| 项目 | 记录 |
| --- | --- |
| 开发分支 | `codex/uploader-first-platforms` |
| T16 起始基线 | `1178869bbf35212e734d3fda70876033e309a99d` |
| 当前产品版本 | `0.25.0` |
| 下载数据库 | Schema 11，未因前端迁移改变 |
| 上传数据库 | 独立 Schema 2，未与下载数据库或 Cookie 合并 |
| 首批上传平台 | Bilibili、抖音、视频号；没有扩展其他上传平台 |

起始基线只用于比较 T16 前后的页面与性能，不包含本记录所述修改，不能作为最终 `source_commit`。包内记录有意不写最终提交、完整 build identity、冻结全量数字或制品 hash；这些值在源码冻结后才由与 `release/` 同级的 `release-receipt.json` 绑定 clean detached commit、五件发行文件和独立安装结果，避免修改被绑定字节造成自引用。

本轮只执行本机代码、测试和禁止远端调用的 synthetic 浏览器验收。没有真实登录、扫码、平台下载、媒体上传、草稿保存、投稿或平台审核；没有读取或修改用户既有账号、Cookie、媒体根或生产数据库。T11 三平台真实上传、T12 当前下载矩阵、T15 Linux/Docker/NAS、GitHub hosted CI 和 required-check 配置均为 **NOT RUN**。

## 2. 生产实现

- 下载页与上传页共用 `static/open-flame.css`：系统字体、语义颜色、间距、圆角、阴影、状态、44 px 操作目标、焦点、响应式断点以及浅色、深色、减少动态、减少透明度、高对比和 forced-colors 降级。
- 两页共用 `static/open-flame-shell.js`：`system` / `light` / `dark` 主题选择持久化，系统主题变化跟随，以及只由 pointer/mouse/touch 路径设置的按压反馈。键盘激活不会缩放按钮。
- `ui_assets.py` 只从固定 allowlist 提供两个本地 UI asset，并设置精确 MIME、缓存和 `nosniff`。页面不依赖外部字体、CDN 或远端图片。
- 根页和 `/uploads` 使用同一安全响应基线。CSP 以当前页面唯一内联业务脚本的 SHA-256 放行脚本，其他默认拒绝；只允许同源连接、同源或 `blob:` 图片，禁止 base URI、外部表单目标和 frame ancestor。
- 下载页重新组织创建、运行摘要、批次、进度、成品、能力与日志，同时保留重复操作保护、来源清空、详情展开、重新打开、重试、取消和 ready asset 下载语义。
- 上传页重新组织账号、登录、媒体来源、内容、草稿与任务。轮询按稳定 ID 更新，保留表单输入、选区、IME、账号/任务焦点、来源清空、详情展开、扫码清理和逐项确认语义。
- `docs/design-preview.html` 直接读取生产 CSS；设计规范、预览、生产页面和发行清单使用同一资源边界。

## 3. 审查修复与行为合同

T16 代码审查发现的九项问题已经修复并加入行为回归：

1. 下载打开、轮询、取消和重试失败同时写入页面上持续可见的 `role=alert`，不再只藏在默认折叠的原始 JSON 中。
2. 下载输入在 IME 组合期间以及组合结束后的 `keyCode === 229` 不会误提交。
3. 两张 HTML 页均带严格 CSP；二维码继续只允许本页创建的 `blob:` URL。
4. 上传登录状态只在可读文字实际变化时更新 live description，避免每秒重复播报相同内容。
5. 账号断开成功并移除确认控件后，焦点返回对应账号卡片。
6. 通用 `details[open]` 动画已移除，轮询替换展开内容不会重复播放装饰动画。
7. ready assets 整个列表不再是 live region，未变化的成品不会随轮询反复播报。
8. 按压缩放只由 pointer/mouse/touch 标记触发，键盘激活保持即时且稳定。
9. 上传新任务的账号、来源、标题、Bilibili 分区、版权和来源说明校验会设置 `aria-invalid` / `aria-describedby`、显示表单内错误并聚焦首个无效字段。

Node DOM harness 另覆盖轮询输入和选区、IME、重复动作、详情、任务选择、断开焦点、页面隐藏和 `pageshow.persisted` 的生命周期。实际 Chromium history navigation 因页面 `no-store` 未进入 BFCache：navigation type 为 `back_forward`，`activationStart=0`；因此本记录不把一次普通 history restore 称为 BFCache 实测通过。`pageshow.persisted` 的单轮询恢复合同由可执行 Node 行为测试证明。

## 4. 自动化回归

生产迁移和九项审查修复后的定向集合：

```text
uv run pytest -q tests/test_ui_design_system.py tests/test_batch_assets_ui.py \
  tests/test_upload_ui.py tests/test_api.py tests/test_upload_api.py
122 passed in 23.81s
```

同一工作树还通过 `python -m compileall -q src tests scripts`、`uv lock --check --offline`、`uv pip check`、CI 合同检查和 `git diff --check`。最终 clean commit 的全量 pytest、发行构建、源码 ZIP 与 wheel 独立安装结果只写入同批包外 receipt；这里的定向数字不能替代该冻结结果。

## 5. 真实 Chromium 本地 G6

浏览器使用 loopback synthetic 服务和明确禁止远端 backend。backend 的 upload 方法若被调用会直接断言失败；登录检查只返回固定 `synthetic_no_remote`。原始截图和会话材料位于 gitignored `validation/local/t16-browser-qa/`，不进入源码发行包。

### 视口、重排与控件

| 页面/条件 | 结果 |
| --- | --- |
| 下载页 1440 / 1024 / 768 / 390 / 320 px | 无 document 横向溢出；可见子元素没有越出视口 |
| 上传页 1440 / 1024 / 768 / 390 / 320 px | 无 document 横向溢出；可见子元素没有越出视口 |
| 可见输入、选择器、按钮和导航 | 相关操作目标高度均至少 44 px |
| 768 px、根字号 200% | 计算字号 32 px；页面无横向溢出，抽查文字未裁切 |
| 上传页 320 px、205 个额外 synthetic drafts | 每页 200 条并显示分页；无横向溢出 |

### 主题与可访问性设置

- `system` 主题会随模拟 OS light/dark 改变；显式 light/dark 在两页一致。
- light 正文对背景对比度为 `15.4568:1`，卡片次要文字为 `6.1511:1`；dark 分别为 `16.7487:1` 与 `7.9774:1`。
- `prefers-reduced-motion` 下 transition/animation 计算时长为 `0.000001s`，scroll behavior 为 `auto`；`prefers-reduced-transparency` 下 backdrop filter 为 `none`。
- forced colors 可启用，键盘焦点轮廓为实线、约 `2.857px`。
- 实际浏览器提交缺少账号的新任务时，页面显示“请选择至少一个账号。”，账号 fieldset 为 `aria-invalid=true` 并取得焦点。
- 实际鼠标按下会短暂设置 `data-pointer-active=true`，松开后清除；键盘路径不设置该属性。

### 性能与长列表

以下为同机、同类 synthetic 数据的五轮本地观测中位数，不是跨设备性能承诺：

| 页面 | T16 前基线 DCL / load / FCP | 0.25.0 当前 DCL / load / FCP |
| --- | --- | --- |
| 下载 | 55.2 / 56.6 / 104 ms；1 轮 FCP 未取到 | 71.0 / 73.2 / 118 ms；4 轮 FCP 非空 |
| 上传 | 54.2 / 57.6 / 96 ms | 62.1 / 64.1 / 104 ms |

各中位增量均低于 50 ms。205 条额外草稿的 320 px 长列表刷新 wall time 约 `382.1 ms`，没有观测到超过 50 ms 的 long task；初始导航 DCL 约 `72.2 ms`，同样没有 long task。

### 自动化仪器边界

严格 CSP 会记录一条由 Codex CUA 为可访问性定位注入内联样式所触发的 CSP violation；调用栈指向 Electron sandbox 的自动化辅助脚本，不是页面源码、共享 shell 或业务 API。该记录不通过放宽生产 CSP 消除。测试期间曾在生产文件变化后观察到 `product_build_drift`，原因是旧 synthetic 服务仍持有启动时身份；最终截图前通过重启服务消除，不把该开发期漂移当作产品成功或平台失败。

## 6. 可接受结论与剩余边界

可接受的结论是：0.25.0 的两张生产页面已经使用同一 Apple 风格设计基础，关键轮询、输入、焦点、IME、扫码与逐项确认合同得到代码和真实浏览器的本地 synthetic 验证；窄屏、200% 文字、主题、对比度、减少动态/透明度、forced colors 和长列表达到本轮 G6 门槛。

本记录不能证明真实账号可登录、二维码可被平台接受、媒体可下载或上传、草稿/投稿能被平台保存、审核或公开，也不能证明 Linux/Docker/NAS、托管 CI、required checks、外部字体以外的第三方闭包或免 Python EXE。只有包外 receipt 完成后，才能把精确 0.25.0 clean commit 和五件制品称为本轮冻结包；真实能力仍须在相同构建身份上按平台与路径分别记录 PASS / FAIL / BLOCKED / NOT RUN。
