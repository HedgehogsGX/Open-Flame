# Iteration 0.27.0 T18 中间编辑工作区本地证据

日期：2026-09-08
状态：**T18 本地编辑切片已实现并完成 synthetic 验收；AI 模型、真实媒体流程及最终发行仍须分别验收**

## 1. 身份与范围

| 项目 | 记录 |
| --- | --- |
| 开发分支 | `codex/editing-workspace` |
| 工作包 | T18 中间编辑工作区 |
| 下载数据库 | Schema 11；本轮未修改下载库结构或 Cookie 边界 |
| 上传数据库 | 独立 Schema 3；本轮未把编辑状态写入上传库 |
| 编辑数据库 | 独立 Schema 1 |
| 编辑数据根 | 下载 `data` 的同级目录 `data-edits` |

本轮范围是下载成品与上传草稿之间的本地、非破坏性编辑工作区：导入已登记下载原件，保存版本化编辑草稿，明确确认不可变 render plan，生成视频分段和封面，再由用户把选定视频成品显式导入上传域。下载原件始终保留，网址输入不会自动编辑、自动上传或自动发布。

包内记录有意不写最终提交、完整 product identity 或发行制品 hash。源码冻结后，最终 clean commit、全量测试及制品身份只能由同批包外 release receipt 绑定。

## 2. 数据隔离与状态合同

- 编辑域使用独立 `data-edits/editing.sqlite3`、`sources/`、`assets/` 与任务私有 `staging/`，不复用下载 Schema 11、上传 Schema 3、下载 Cookie 或上传账号凭据。
- 已登记下载视频导入时复制到编辑根，重新计算大小与 SHA-256，并核对下载域登记摘要；源文件在复制前后发生变化、摘要不一致、路径重定向或媒体类型不允许时失败关闭。编辑和渲染不改写下载原件。
- Editing Schema 1 包含 sources、projects、版本化 drafts、不可变 render plans、输出 assets 与 idempotency requests。项目草稿通过 `expected_version` 防止旧页面覆盖新版本；重复请求只在操作与摘要完全一致时重放，否则返回幂等冲突。
- render plan 保存确认时所见的完整 recipe 和摘要。状态集合为 `review / queued / running / canceling / ready / failed / canceled`，本地渲染没有远端结果不确定所需的 `unknown`。创建 plan 后先停在 `review`；只有明确确认才进入 `queued`。
- worker 只领取一项任务，并用随机 claim token 防止旧 worker 回写新任务。重启后，未完成的本地 running/canceling 任务转为 `failed`，原 queued 任务退回 `review` 并要求再次确认，不会静默重放。
- 编辑根先取得跨进程独占 lease 才初始化和恢复。worker 以及已经进入服务层的 API 操作均计入活动生命周期；停止超时不会提前释放 lease，最后一个活动方结束后才释放，阻止新实例把“已复制、尚未登记”的文件误清理。
- 独占恢复只删除严格小写受管名且未登记的 source/asset，以及严格双 ID 且不属于活动 claim 的 staging。case-only 名称、未知名称、普通文件、link/reparse 和其他不安全条目保留；已进入终态但崩溃前未删除的 staging 可在下次启动回收。
- 公共项目、plan 与 asset JSON 不返回本机路径。预览、渲染和上传导入通过内部校验后的 `Path` 使用受管文件；输出再次核对大小、SHA-256、时长、尺寸、容器和 codec 后才登记为 ready。

## 3. 分段与封面

首个本地 processor 复用项目固定的 FFmpeg/ffprobe 工具链，不调用 shell，不从用户标题拼接命令或输出路径，并把所有结果写入 plan 私有 staging 后再由编辑服务发布。输入 demuxer 由受管后缀固定为 `mov` 或 `matroska`，ffprobe/FFmpeg 只允许 `file` protocol 并严格复核 format name；伪装为 `.mp4` 的 ffconcat 清单在输出前失败。

| 操作 | 当前本地合同 |
| --- | --- |
| 视频分段 | 一份 recipe 可包含多个有序、不重叠的毫秒区间；每段至少 100 ms；输出使用固定安全文件名 `segment-001.mp4` 等，并记录实际时长、尺寸、容器、视频与音频 codec |
| 封面制作 | 从指定毫秒位置抽帧；支持 source、16:9、4:3、3:4、9:16 与 1:1；可叠加标题和副标题；非 ASCII 文本只读取固定 OS 字体 allowlist 并逐字符拒绝缺字；输出为经过完整解码验证的静态 PNG |
| 原件保护 | 分段和封面均派生新文件；处理前后复核原件文件身份，不覆盖、移动或重编码下载原件 |
| 取消与失败 | 取消事件传递至本地子进程；失败或取消清理任务 staging，只有完整验证的输出进入 assets |

当前切片没有实现任意轨道剪辑、转场、滤镜、人声分离或背景声重建；这些能力不能由“分段与封面通过”推定。

## 4. AI 翻译与配音合同

本轮建立了翻译、配音、provider capability 和字幕时间轴的可扩展合同：recipe 可保存 source/target language、provider、model、voice、是否替换原声及 `disabled / needs_review / ready / blocked` 状态；SRT/WebVTT 使用稳定 cue ID 与整数毫秒往返。

当前生产 capability 明确将本地分段和封面标为 ready，并把翻译和配音标为 blocked，直到隔离 AI runtime、固定模型、凭据/隐私边界、许可证和实际试听验收完成。AI recipe 即使进入 `needs_review` 也必须经过用户明确确认；处于 blocked 的 AI 操作不能排队。此合同和 synthetic 测试不证明已经完成真实自动翻译、语音合成、声音克隆或配音对齐。

## 5. 编辑成品到上传草稿

- 编辑域只有 ready 的 `segment` 或 `dubbed_video` 可作为视频来源；封面、字幕和音频不能被当作上传视频。
- 用户在编辑页点击“用于上传”后，上传页仍只执行显式导入。上传服务复制并重新计算编辑成品，核对编辑域登记的 SHA-256 与大小后才建立受管上传来源。
- 导入编辑成品不会创建投稿任务。用户仍须填写平台参数、创建本地草稿并逐任务明确确认，才可能调用 Bilibili、抖音或视频号适配器。

## 6. 定向自动化

定向集合覆盖 Schema 精确创建与损坏拒绝、下载 UUID 与源摘要、原件复制及篡改、draft 版本并发、plan 快照与幂等、明确确认、单 worker claim fencing、停止时的活动操作、取消/重启和孤儿恢复、输出 metadata、真实 FFmpeg/ffprobe 分段与封面、中文字体与伪装 ffconcat 拒绝、SRT/WebVTT 时间轴、AI capability、loopback API/CSRF，以及编辑成品导入上传但不创建任务。

```text
.venv\Scripts\python.exe -m pytest -q \
  tests/test_editing_schema.py tests/test_editing_service.py \
  tests/test_editing_media.py tests/test_editing_ai_contracts.py \
  tests/test_editing_api.py tests/test_download_upload_integration.py

2450 passed, 8 skipped in 358.21s
```

最终全量 pytest 为 `2450 passed, 8 skipped in 358.21s`；8 个 skip 对应当前 Windows 主机无法执行的 POSIX directory-fd、root/getfacl、Unix domain socket、POSIX replacement 与 permission-bit 环境合同。随后 `compileall`、锁文件检查、当前 `.venv` 依赖检查、文档/链接及发行验证和 diff whitespace 检查均通过。

## 7. 真实 Chromium 本地 synthetic 验收

使用真实 Chromium 打开仅监听 loopback 的本地应用，输入为本轮生成的 **8 秒、1280×720、H.264/AAC synthetic 视频**。没有使用用户媒体或远端平台内容。

| 检查 | 结果 |
| --- | --- |
| 项目与草稿 | 从已登记 synthetic 下载成品进入编辑页，建立项目并保存版本化 recipe |
| 分段 | 配置 2 个分段，成品实测时长分别为 **3500 ms** 与 **3000 ms** |
| 封面 | 以 9:16 方向生成 **720×1280** PNG 封面 |
| plan 生命周期 | 创建后停在 review；明确确认后由单 worker 渲染，最终状态为 ready，code 为 `render_complete` |
| 编辑输出 | 页面展示两个视频片段和一张封面，并可通过同源内容入口读取 |
| 上传衔接 | 选择一个编辑视频成品显式导入上传域；导入前后 **SHA-256 与 size 一致**，同时 `upload_jobs=0`，未创建投稿任务 |
| 桌面页面 | **1440×900** 视口完成编辑页项目、参数、计划、输出与上传衔接检查 |
| 窄页面 | Chromium 默认窄视口完成页面重排及主要编辑控件可用性检查 |
| 轮询与键盘焦点 | 最终隔离页面夹具中，视频“用于上传”链接经过 3.5 秒状态轮询后仍保持键盘焦点；封面只显示下载入口，没有视频导入动作 |

浏览器验收没有执行登录、扫码、远端下载、远端 AI 调用、平台上传、平台草稿保存、定时发布、投稿、审核或公开。synthetic 文件、数据库和浏览器会话只属于本地验收，不进入源码或发行包。

## 8. 可接受结论与剩余边界

可接受的本地结论是：T18 已建立独立、可版本化和明确确认的编辑工作区；已登记下载原件可在不被修改的前提下生成多个本地视频分段及封面，ready 视频成品可由用户显式导入上传域，而且导入本身不会创建投稿任务。真实 Chromium synthetic 流程观察到 `render_complete`，两个分段、竖版封面及上传来源摘要保持符合预期。

本记录不能证明任意真实视频均可正确处理，不能证明 AI 自动翻译或自动配音已经可用，也不能证明 Bilibili、抖音、视频号的真实账号、字段、封面、定时或投稿结果。真实媒体兼容性、AI runtime/模型与试听、三平台真实上传、Linux/Docker/NAS、GitHub hosted CI、最终 clean commit 和发行制品均保持 **NOT RUN**、**BLOCKED** 或等待最终冻结记录。
