# Iteration 0.21.0 — 平台实测、TikTok 封面与启动诊断

日期：2026-09-04。本轮修复、真实样本复测与工程回归完成；下载器整体仍有下文未完成项。本轮不代表整个平台通过 Stage 0，不代表已生成安装器或发布包；没有提交或 push。

## 从真实下载定位的问题

在 Windows 一体化应用中使用公开样本、匿名模式和已锁定的 yt-dlp `2026.08.19` / FFmpeg 工具包，未提供额外 JS runtime、真实 Cookie 或浏览器凭据。

| 基线样本 | 结果 | 实际证据 |
|---|---|---|
| Douyin | `authentication_required` | 1 次 Attempt、0 成品；应用正常停止 |
| TikTok | 首次 `extractor_broken` | 1 次 Attempt；随后完全相同的生产 probe 命令成功，不能断定稳定解析器损坏或必须登录 |
| Instagram Reel | `ready` | 1 次 Attempt、1 成品；DB/manifest/API size 与 SHA-256、ffprobe 和完整解码通过 |

上述三个基线样本的完整 product identity 均为 `0.20.0+build.sha256.e0d65ca737f48befac17965c51010e2d85451f2ed798cf916f752aab473c1ec5`，每次运行期间源码保持不变。每个应用运行均核对三进程同 run、退出码 0、正常清理、无残留与端口释放，不因样本失败而漏查生命周期。

启动诊断修复后，另一次 TikTok 完整运行在 probe 之后两次均返回 `validation_failed`，诊断为未识别输出文件类型；该中间构建 identity 为 `0.20.0+build.sha256.55075337edd3b295f98ad24cd54cb492db391cb29f8ccda2146afafcfa3c9b2f`。独立生产 Adapter 重放取得正常 MP4 和 `.image` 后缀的 JPEG 封面，确认文件类型拒绝是这两次失败的原因。它不解释最初非稳定的网页解析失败。

## 修复契约

下载命令使用上游已支持的 `--convert-thumbnails image>png`：仅标准化不明确的 `.image` 封面，普通 JPG/PNG/WebP 不额外转码；WebP 内容沿用上游纠正后缀的逻辑。JPEG/PNG 的 `.image` 转为 PNG 会改变封面字节、大小及部分元数据，但不重新编码视频原件、不加入 JPEG 式有损压缩。不放宽原件/sidecar 类型与归属检查，不接受任意未知文件，也不引入新依赖或自写解析器。该固定版本的实现参考 [上游源码](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/postprocessor/ffmpeg.py)。

## 修复后的真实一体化应用验收

冻结 `0.21.0+build.sha256.4d089ef9b8fc44bc162dcc7cc3540f77340d3f414a1a9801e93087e3a0cd4a5d` 后，在新的独立应用目录通过真实 supervisor/control/Worker 和 Batch API 再下载同一 TikTok 与 Instagram 样本。仍为匿名、锁定工具、无额外 JS runtime；源码 identity 全程不变。

- **2/2 ready，2 次 Attempt，两个平台各第 1 次成功**；23.047 秒。
- 两份成品分别通过 DB/manifest/API 大小与 SHA-256 一致性、ffprobe 和 FFmpeg 完整解码。
- 停机后再次使用 `-xerror` 且要求空错误输出严格解码原件与封面：两份通过。TikTok 为 658,136 bytes / 5.866667 秒，封面 `image/png`；Instagram 为 1,068,500 bytes / 48.9 秒，封面 `image/jpeg`；两份原件均有音视频流，封面哈希与数据库一致。
- 三个进程共享一个 run，应用退出码 0，正常停止；保留的三个进程句柄均确认已退出，残留 0，测试端口释放，Cookie 副本 0，无强制终止。
- 用于自动验收的 stdin EOF → SIGINT 桥接仅存在于忽略目录测试脚本；真实 CLI 仍使用 Ctrl+C/原有 supervisor 停止路径，未增加生产 stdin-EOF 接口。

这是当前构建两个样本的新下载证据，不是旧文件复用，也不将先前失败抹去。确切 URL、媒体标识和运行目录保存在本地忽略目录，不纳入公开记录。

## 可操作的启动诊断

过去不同启动失败在 CLI 合并成 `local_app_failed`。现在故障边界携带闭合类型，输出固定单行 JSON，不根据异常英文猜原因，不输出 Cookie、路径、ref 或原异常。

| 错误码 | 位置 | 处理建议 |
|---|---|---|
| `local_port_unavailable` | `port_reservation` | 关闭占用端口的程序，或指定其他 `--port` |
| `local_toolchain_unavailable` | `toolchain_directory` | 安装锁定工具包，或指定有效 `--tool-root` |
| `local_cookie_config_invalid` | `cookie_config` | 检查只读 JSON；匿名启动可移除 `--cookie-config` |

以上已知边界明确 `log_status=not_started`。未知故障保持原通用错误码，位置和日志状态均为 `unknown`，不把运行中故障误称为启动失败。既有退出码保持：预期应用错误 2，内部错误 70。

此修改只改善 stderr。早期失败仍没有持久化启动记录，参数解析失败仍是安全的 usage 提示，尚无 Windows 双击启动器；这些是下一步的实际可用性工作，不能宣称完整启动日志已经完成。已有三进程运行 JSONL 与网页日志功能不变。

## 工程回归与发布面检查

- 最终完整测试：**1447 passed, 8 skipped in 153.95s**。跳过项为 1 项 POSIX directory-FD、4 项 root/getfacl 元数据、1 项 Unix socket、1 项 POSIX 文件替换和 1 项 POSIX 权限；不计作已通过。
- 启动诊断的 7 项 red 先复现，再修复为 green；真实 CLI 覆盖端口占用、缺失/非目录工具路径、只读无效 Cookie JSON，另有未知错误脱敏回归。独立审查修正了通用错误误标启动阶段的问题。
- 新增 4 项固定工具测试，在生产命令缺少转换配置时先 red。真实固定 parser → thumbnail postprocessor → after_move printer → Adapter mapping/classification → 资产 MIME 契约均执行；5 种正向封面、不可解码封面、未知后缀和无归属文件被覆盖。合成 fixture、不访问网络，不以 fake converter 代替真实工具。
- compileall、`uv lock --check --offline` 和 `git diff --check` 通过；37 份 Markdown 的 121 个本地链接均有效。最终 product identity 与真实运行报告一致。
- Git 可发布清单 239 文件读取无失败；运行媒体/数据库/JSONL/密钥误入 0，已知真实标识、实际样本标识、高置信 token/私钥及 Netscape Cookie 记录无命中。15 处 Windows Users 路径模式均已分类为 14 处 synthetic 测试值与 1 处历史扫描说明；本轮重点 8 文件无未分类私密字面量。此检查不保证排除所有未知形式的秘密。
- `validation/local` 与 `runtime-tools` 忽略规则有效；LICENSE/NOTICE 未改，无新增依赖，23 个外部锁定包版本不变。未创建发布包、commit 或 push。

## 尚未完成的验收

- 抖音登录态需要用户提供已准备好的本地只读配置路径；不索取 Cookie 内容、不自动读取浏览器秘密。
- 每个平台的完整 Stage 0 样本矩阵、重复运行和负例证据仍未满足，capability 不升级。
- Windows 源码启动器、早期诊断持久化、安装器，以及目标 Linux/Docker 和第三方二进制再分发仍需各自验证。
- Apache-2.0 与 `Copyright 2026 HedgehogsGX & Cyaegha_Xu` 不变；精确样本、运行媒体、数据库、日志和测试辅助脚本留在忽略目录，不进入源码提交。
