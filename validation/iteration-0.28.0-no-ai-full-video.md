# Iteration 0.28.0 无 AI 完整视频与封面流程验证

日期：2026-09-10（Australia/Adelaide）

## 修复的问题

此前 `/workflows` 把 `segments: []` 显示为“完整视频”，但关闭翻译和配音时，前端与服务端仍要求至少一个显式分段，流程以 `workflow_output_count_invalid` 停止。仅制作封面也没有可供上传的视频输出。该行为与页面含义及日常“输入网址后保留完整视频”的路径不一致。

当前将空分段列表定义为一个完整视频输出：

- 无 AI、无显式分段时生成 `segment-001.mp4`。
- 无 AI、无显式分段但启用封面时生成 `segment-001.mp4` 与 `cover.png`。
- 1～10 个 Workflow 显式分段仍按原顺序逐段输出；编辑域自身仍保留最多 100 个显式分段的合同。
- AI 无分段流程继续由既有 `caption.vtt` 与 `dubbed-video-001.mp4` 路径负责，不额外生成普通 segment。

## 实现边界

普通计划由 Editing service 模块的 `render_ordinary_plan()` 统一解释完整视频语义，`EditingManager` 后台 worker 与直接 `EditingService.process_next()` 共用该入口。同一个持久计划不会因执行入口不同而分别得到“完整视频加封面”和“仅封面”。

底层 `MediaProcessor` 只有在明确传入 `full_video_output=True` 且 recipe 没有显式分段时，才在完成真实 ffprobe 后构造内存中的 `0 → source.duration_ms` 区间，并复用现有分段渲染器。实现没有增加数据库、Schema、服务、线程、队列、runtime 或依赖，也没有让 Workflow 或 Upload 直接读取下载资产。

完整视频仍经过原有安全与一致性边界：

1. 处理前核对冻结的源大小与 SHA-256，并在处理步骤后复核源文件 identity。
2. 在 plan/claim 独占 staging 目录中以 FFmpeg `-n` 生成 H.264、可选 AAC、MP4 `+faststart` 派生文件；源视频不被修改。
3. 对输出重新执行 ffprobe，复核容器、codec、时长、尺寸与大小上限；取消或失败时清理本次 owned paths。
4. `complete_plan()` 把结果复制到 Editing asset store，重新计算大小和 SHA-256、与 `RenderAsset` 对照，并在同一事务中登记 ready asset。
5. Upload 仍只通过既有 `edit_output_id` 导入正常的 `segment` 或 `dubbed_video` 资产，没有新增跨域旁路。

## 当前验证

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| `validation/local/validate_no_ai_full_video.py` | **5/5 PASS** | bundled FFmpeg 的纯完整视频、完整视频加封面、双显式分段；direct service 与 manager 执行、资产 hash/size、服务重开；LocalAdapter 重启读取；AI full-video 不进入普通 segment；Workflow profile 接受空分段 |
| 编辑页 Chrome | **PASS** | 当前生产 `EDITING_HTML`；390×844 下打开空 recipe，0 个分段可保存并建立待核对计划，cover-only 保留空分段和封面；无横向溢出、页面错误或非 loopback 请求 |
| Workflow Chrome | **PASS** | 当前生产 `WORKFLOW_HTML`；完整视频请求提交 `segments: []`，无 AI cover-only 同样成功，显式多分段、焦点与窄屏合同保持 |
| 既有完整视频整链 | **PASS** | 合成 URL 下载、隔离本地 AI、FFmpeg、Bilibili/抖音/视频号 fake backend；最终 `submission_acknowledged`，重启不重放 AI/render |
| `compileall -q src scripts` 与两页内联 JS `node --check` | **PASS** | Python 语法与当前生产页面脚本 |
| Editing media/service/API/schema 聚焦回归 | **65 passed, 3 failed** | 三项失败只断言 Editing Schema `1`，当前源码为 Schema `4`；未修改测试文件 |
| Upload service 与 Editing AI contracts | **40 + 8 passed** | 编辑成品导入/上传消费与 AI 合同保持 |
| `git diff --check`、`verify_commit_scope.py --staged` | 提交前执行 | 不允许新增或修改已跟踪自动化测试 |

所有新增探针、浏览器脚本、合成媒体和输出均位于被忽略的 `validation/local/`，不会进入源码提交或发行文件。浏览器路由只允许 `127.0.0.1` 合成 origin；Python 媒体验证只调用仓库已固定的本地 FFmpeg/ffprobe。

## 仍未证明

该记录没有执行真实网址下载、真实 OpenAI、真人配音试听、平台登录/扫码、Bilibili/抖音/视频号上传、封面接受、定时发布、审核或公开可见性。完整视频统一转码会消耗与源时长相关的时间和临时磁盘；当前验证使用短合成媒体，长片和接近 8 GiB 上限的资源行为仍需外部测试。该记录不是新的 release receipt，也不能把任一平台标记为 `verified`。
