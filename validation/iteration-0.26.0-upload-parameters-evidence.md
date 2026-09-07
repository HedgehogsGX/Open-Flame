# Iteration 0.26.0 三平台投稿参数与本地 G7 证据

日期：2026-09-07
状态：**T17 本地实现与 G7 已完成；真实平台能力及最终制品仍须分别验收**

## 1. 身份与范围

| 项目 | 记录 |
| --- | --- |
| 开发分支 | `codex/uploader-first-platforms` |
| T17 起始基线 | `c5b57ff7472d3eac7550bfe3f2b3129704f277b7` |
| 当前产品版本 | `0.26.0` |
| 下载数据库 | Schema 11；本轮未修改下载库结构或 Cookie 边界 |
| 上传数据库 | 独立 Schema 3 |
| 上传备份 | 格式 2；继续只读接受格式 1 并在 staging 中迁移 |
| 上传运行时 manifest | Schema 2 |
| 首批上传平台 | Bilibili、抖音、视频号；没有扩展其他上传平台 |

本轮只执行本机代码、合成 backend、损坏输入和 loopback 浏览器验收。没有真实登录、扫码、远端下载、媒体上传、平台草稿保存、定时触发、投稿、审核或公开。没有读取或修改用户既有账号、Cookie、媒体根或生产数据库。GitHub hosted CI、required checks、Linux/Docker/NAS 与五件发行制品均不属于本记录的完成结论。

包内记录有意不写最终提交、完整 product identity 或制品 hash。源码冻结后，只有同批包外 `release-receipt.json` 可以把 clean detached commit、五件发行文件、源码/wheel 独立安装与冻结测试绑定成一个最终构建身份。

## 2. 用户流程与参数合同

网址入口仍沿用下载域：输入网址会创建下载任务；下载完成后，用户可把已登记视频导入独立上传根。上传页创建的是可核对的本地草稿，逐账号生成任务；只有用户对某个任务单独点击确认，才会排队调用该账号的平台适配器。网址输入本身不会直接投稿，也不会批量绕过逐任务确认。

| 平台 | 本地草稿可核对字段 | 适配及限制 |
| --- | --- | --- |
| Bilibili | 独立标题、简介、标签、分区、原创/转载与来源、单封面、发布时间、动态文案、禁止转载、关闭评论、关闭弹幕 | 标题最多 80 字；本地定时至少提前 6 小时 5 分钟，为平台 4 小时下限预留最多 2 小时传输和 5 分钟安全余量；固定传入 biliup 的 `--cover`、`--dtime`、`--dynamic`、`--no-reprint`、`--up-close-reply`、`--up-close-danmu`，关闭评论/弹幕时使用 `--submit app` |
| 抖音 | 独立标题、简介、标签、单张横版或竖版封面、发布时间、可选自主声明 | 标题最多 30 字；本地定时至少提前 4 小时 5 分钟，为平台 2 小时下限预留最多 2 小时传输和 5 分钟安全余量；声明只接受三个固定值。用户未选声明时不会沿用固定上游的 AI 默认 |
| 视频号 | 独立主文案、标签、4:3 横封面、3:4 竖封面、发布时间、7～15 字短标题、可选“含AI生成内容”、发布/平台草稿 | 主文案第一行最多 100 字；本地定时至少提前 4 小时 5 分钟，为平台 2 小时下限预留最多 2 小时传输和 5 分钟安全余量；最多 28 天并按本地时区整点；平台草稿不能同时定时；短标题留空时先规范化并把最终值写入草稿快照 |

标签以不含 `#` 的文本保存，最多 10 个、不重复、每个最多 20 字；适配器调用时再添加平台语法。页面每个平台只有一套覆盖字段；同一平台同时选择多个账号时会用相同表单值生成彼此独立的任务快照，再分别预览、确认和执行。服务 API 可按 `account_id` 接受不同 `target_overrides`，但生产页面尚不能对同平台的多个账号分别编辑。任务预览、重试和实际 `UploadRequest` 使用各自已持久化快照。视频号自动补位得到的全角逗号短标题在取消、重试和确认过程中保持不变，不会在正常生命周期中再次规范化。

`datetime-local` 按浏览器本地时区解释，草稿同时保存 UTC Unix 时间和确认时的 UTC 偏移。夏令时跳过或重复的本地时间被拒绝；创建、确认及调用适配器前均重查最小提前量。抖音和视频号在上游填表后还会回读精确“定时”单选项及日期/时间；字段正确但页面仍为立即发布时以 `schedule_mode_mismatch` 失败关闭。

## 3. 受管封面与数据生命周期

- 封面只接受有效静态 JPEG、PNG 或 WebP；编码文件上限 20 MiB、像素上限 40,000,000、完整解码预算 64 MiB。Pillow 解码前后均按实际图像 mode 检查预算，压缩体积很小的超大 JPEG/WebP 也会被拒绝；EXIF Orientation 不为默认方向的图片失败关闭，避免显示方向与视频号封面槽比例不一致。
- 导入时复制到独立上传根并保存尺寸、大小和 SHA-256。创建、确认、重试与执行前重新核对；缺失、变化、删除或仍被未完成任务引用时明确拒绝。用户原件与下载原件不被改写。
- Bilibili 只允许一个横版封面；抖音按宽高选择且复核单张横版或竖版槽；视频号分别验证 4:3 与 3:4。执行时封面已验证字节会先进入任务私有临时副本。浏览器适配器还要求精确文件输入调用、弹窗关闭和目标预览状态符合预期，不能把只点击控件当作已应用。
- Upload Schema 3 保存两个封面外键、发布时间、时区偏移和平台参数 JSON。Schema 1→2→3 与精确 Schema 2→3 均在单一迁移事务中完成；损坏 JSON、不可哈希 `job_ids`、额外表/索引/触发器、外键或内容不一致统一拒绝并回滚。
- Schema 2 的旧标签和固定上游隐式声明在迁移时变成可见、可复核的 Schema 3 值。原 queued 任务撤回为 draft，原 running 任务转为 unknown，并保留组合的旧元数据/重启或结果不确定诊断；不会自动重传。
- 上传备份格式 2 包含受管封面及其 hash、任务参数和 Schema 3 数据。恢复在 staging 校验后原子发布；格式 1 / Schema 2 是只读兼容输入，不会就地改写备份。

## 4. 适配器失败关闭边界

- Bilibili 参数使用固定 biliup CLI 白名单；布尔值、分区、转载来源、定时值与封面路径不符合合同即不启动子进程。
- 抖音显式声明要求固定选项、弹窗关闭和所选 radio 回读；显式封面要求预览变化。提交按钮由同一次防重复 guard 覆盖普通 click 与 JavaScript fallback，结果不明时停止，不循环再次点击。
- 视频号两个封面槽要求各自控件、裁剪弹窗和主弹窗关闭、各自预览变化；短标题和内容标记要求精确局部回读。投稿仅在固定 HTTPS 视频号域、`post_create` 路径、HTTP 200 且响应含显式整数 `errCode: 0` 时记为 `submitted`；其他结果保持 `unknown`。
- `submitted` 只表示适配器观察到上游提交确认，不代表审核通过、已公开或真实平台验收。

## 5. 自动化回归

投稿参数、Schema、备份、服务、API、适配器和页面的联合定向集合：

```text
.venv\Scripts\python.exe -m pytest -q \
  tests/test_upload_api.py tests/test_upload_backend.py \
  tests/test_upload_backup_cli.py tests/test_upload_backup_restore.py \
  tests/test_upload_platform_parameters.py tests/test_upload_schema2.py \
  tests/test_upload_schema3.py tests/test_upload_service.py \
  tests/test_upload_ui.py
465 passed in 92.83s (0:01:32)
```

它覆盖逐平台覆盖值、标签边界、Schema 2 旧标签的 HTTP 幂等重放、封面解析与压缩炸弹预算、定时/DST/过期竞态、radio 回读、重试快照、严格提交确认、Schema 迁移/损坏回滚、格式 1/2 备份恢复、API 防重放及轮询表单保留。来源选择在跌出最新 200 条后会精确查询；只有精确 404 才清除，瞬时失败及刷新后段并发切换均保留上次有效选择。

当前完整工作树回归：

```text
.venv\Scripts\python.exe -m pytest -q
2367 passed, 8 skipped in 362.91s (0:06:02)
```

8 个 skip 分别受 POSIX directory-fd、root/getfacl、Unix domain socket、POSIX open-file replacement 与 permission-bit 环境约束；它们保持目标 Linux/Docker/NAS **NOT RUN**，不以 Windows 结果代替。

## 6. 真实 Chromium 的本地合成验收

使用 Codex CUA 驱动真实 Chromium，服务仅监听 `127.0.0.1`，并注入三个合成 ready 账号、一个合成视频来源、两张合成封面和三个 draft。backend 的登录、检查和上传入口若被调用会直接断言失败，因此此验收不能产生远端动作。

| 检查 | 结果 |
| --- | --- |
| 319×611 可见窄视口 | `documentElement.scrollWidth=304`、`body.scrollWidth=305`，无页面横向溢出 |
| 1280×720 宽视口 | `documentElement.scrollWidth=1265`、`body.scrollWidth=1265`，无页面横向溢出 |
| 平台参数控件 | 三个平台区块均可访问；抽查三个 select 高度均为 44 px |
| 轮询保留 | 选择 Bilibili 封面、抖音封面、视频号发布模式/双封面/短标题/内容标记后刷新，全部值保持 |
| 任务预览 | 三个平台的封面、发布时间和专属参数均显示在各自本地草稿卡片 |
| 控制台 | 宽、窄两个页面的 warning/error 日志均为空 |

原始本地合成服务位于 gitignored `validation/local/iteration-0.26.0-browser/`，不进入源码或发行包。浏览器验收没有点击登录、检查、确认投稿或任何远端操作。

## 7. 可接受结论与剩余边界

G7 可接受的结论是：0.26.0 已在本地实现 Bilibili、抖音、视频号逐平台表单值、逐账号任务快照、受管封面、定时值、Schema 3 和上传备份格式 2；草稿预览、逐项确认、重试保值、失败关闭和窄/宽视口均有合成证据。

本记录不能证明三平台当前真实账号可登录、二维码可接受、字段/封面能被平台保存、定时能在指定时刻触发、投稿能通过审核或公开。T11 三平台真实验收、T12 当前下载矩阵、T15 Linux/Docker/NAS、GitHub hosted CI、最终 clean commit 与五件发行制品均保持 **NOT RUN** 或由包外 receipt 判定。
