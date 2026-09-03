# Iteration 0.8.1 真实平台双样本验收证据

> 许可历史说明：本文中的 proprietary/31-files 表述记录 0.8.1 当时的构建事实；项目自有材料已自 0.9.1 起由权利人改授 Apache-2.0，第三方 binary/container/tool-bundle 门禁不变。

> 执行日期：2026-09-03  
> 环境：Windows x64；项目私有、固定版本工具链  
> 证据类型：用户明确授权的两个公开 URL 的无 Cookie 探测、真实下载与本机媒体完整性验证  
> 证据边界：这是 YouTube 与 X 各一个样本的实测结果，不代表任一平台整体能力、长期兼容性或 Stage 0 已通过

## 1. 范围与工具

用户明确提供并授权本轮测试以下两个公开样本：

1. 一个 YouTube 视频（精确 URL 在公开记录中省略）
2. 一个 X 帖子视频（精确 URL、账号与状态标识在公开记录中省略）

两项测试均使用 `--ignore-config` 与 `--no-playlist`，没有传入浏览器 Cookie、Cookie 文件或 netrc，也没有安装新的下载工具。固定工具版本为：

| 组件 | 版本 |
|---|---|
| yt-dlp | `2026.08.19` |
| FFmpeg / ffprobe | `n9.0.1-6-g9d4ca21220-20260820` |
| Node.js（YouTube 第二次探测） | `v24.16.0` |

原始探测 JSON、stdout/stderr、媒体文件和完整命令保存在 Git 忽略的 `validation/local/live-platform-20260903/`。本公开记录不复制临时媒体地址、大段平台元数据或与验收无关的个人信息。

## 2. 结果总览

| 样本 | 真实下载 | ffprobe | 完整解码 | 最终文件 |
|---|---|---|---|---|
| YouTube | exit `0`；视频与音频合并为 WebM | exit `0`；VP9 + Opus，媒体结构有效 | FFmpeg 全流解码至 null sink，exit `0`，stdout/stderr 均为空 | size/SHA-256 与本机证据一致；公开记录省略媒体指纹 |
| X | exit `0`；MP4 | exit `0`；H.264，1 个视频流、0 个音频流 | FFmpeg 全流解码至 null sink，exit `0`，stdout/stderr 均为空 | size/SHA-256 与本机证据一致；公开记录省略媒体指纹 |

## 3. YouTube：Node.js A/B 探测与下载

无下载探测对同一 URL 做了两次 A/B：

| 条件 | 结果 |
|---|---|
| A：未显式配置受支持的 JavaScript runtime | exit `0`；发现 43 个格式；默认选择 `616+251`；stderr 出现 yt-dlp 的 JavaScript runtime 缺失警告 |
| B：显式启用 Node.js `v24.16.0` | exit `0`；仍发现 43 个格式并选择 `616+251`；stderr 为 0 bytes，不再出现 runtime warning |

Node.js 复验说明固定的本机 Node runtime 能消除这个样本的 runtime warning；格式数未变化，因此不能据此推断所有 YouTube 页面或格式均已覆盖。

真实下载明确选择直接可用的 `248+251`：VP9 视频和 Opus 音频分别下载后由固定 FFmpeg 合并。最终 WebM 经 ffprobe 和完整解码验证，两个步骤均以 `0` 退出。

Git 忽略的本机证据位置：

- `validation/local/live-platform-20260903/youtube/RUN.md`
- `validation/local/live-platform-20260903/youtube/01-probe.json`
- `validation/local/live-platform-20260903/youtube/02-download.stdout.log`
- `validation/local/live-platform-20260903/youtube/03-ffprobe.json`
- `validation/local/live-platform-20260903/youtube/04-sha256.txt`
- `validation/local/live-platform-20260903/youtube/09-node-probe.stdout.json`
- `validation/local/live-platform-20260903/youtube/09-node-probe.stderr.log`
- `validation/local/live-platform-20260903/youtube/10-decode.stdout.log`
- `validation/local/live-platform-20260903/youtube/10-decode.stderr.log`
- `validation/local/live-platform-20260903/youtube/<redacted-media-key>.webm`

## 4. X：无声媒体、ID 差异与退出码发现

原始 URL 与应用规范化后的 canonical status URL 均能无 Cookie 探测成功。平台状态 ID 与实际提取媒体 ID 不同；精确值在公开记录中省略，且两者不能被数据模型假定为相同。

探测得到的 6 个候选格式都没有可用音频流。最终 MP4 的 ffprobe 结果也是 1 个 H.264 视频流、0 个音频流，且完整解码无错误。因此对这个样本而言，“无音频”是源格式特征，不是下载或合并失败；通用完整性判断不应硬性要求每个 X 媒体都含音频。

测试还复现了一个 CLI 退出语义：单 URL 已完整下载后，额外的 `--max-downloads 1` 会令 yt-dlp 以 `101` 停止。移除该参数、保留 `--no-playlist` 后，干净实际传输以 `0` 退出。上层不能把该首轮 `101` 简化为媒体字节未完成；单 URL 路径应避免该参数，或在接受它前验证产物完整性。

Git 忽略的本机证据位置：

- `validation/local/live-platform-20260903/x/RESULT.md`
- `validation/local/live-platform-20260903/x/01-probe.stdout.json`
- `validation/local/live-platform-20260903/x/02-download.stdout.log`
- `validation/local/live-platform-20260903/x/06-download-clean.stdout.log`
- `validation/local/live-platform-20260903/x/07-ffprobe-clean.stdout.json`
- `validation/local/live-platform-20260903/x/08-sha256-clean.stdout.log`
- `validation/local/live-platform-20260903/x/09-decode.stdout.log`
- `validation/local/live-platform-20260903/x/09-decode.stderr.log`
- `validation/local/live-platform-20260903/x/10-canonical-probe.stdout.json`
- `validation/local/live-platform-20260903/x/media/x-<redacted-media-key>.mp4`

## 5. 可下结论与不可下结论

本轮可以确认：在 2026-09-03 的固定 Windows 工具环境中，这两个明确样本均完成了无 Cookie 探测、真实媒体传输、ffprobe 验证、SHA-256 记录和完整解码。YouTube 样本还完成了 Node.js runtime A/B，X 样本验证了无声媒体与状态/媒体双 ID 情况。精确 URL 与媒体指纹只保留在 gitignored 本机证据中。

本轮不能确认：YouTube 或 X 的其他 URL、登录/年龄/地域受限内容、直播、播放列表、Cookie 流程、所有编码组合或未来平台变更均可工作。每个平台仅有一个正向样本，不满足仓库 Stage 0 对每个 `platform × source_type` 单元至少 10 个公开正样本、负样本以及连续完整运行的能力声明门槛。

## 6. 代码与安装包回归

真实样本发现转化为两个受测试的代码改动：候选 Worker 可选接收一个经过绝对路径、普通文件与 reparse/symlink 校验的 `--js-runtime NAME:ABSOLUTE_EXECUTABLE`，而未配置时仍明确禁用 JavaScript runtime；yt-dlp 的 probe/download 命令固定加入一次 `--encoding utf-8`，version 命令不受影响。remote components 继续禁用。

最终仓库与制品验证结果：

| 检查 | 结果 |
|---|---|
| 全量测试 | `853 passed, 8 skipped`；跳过项均为当前 Windows 环境不具备的 POSIX/Linux 条件 |
| `compileall` | 通过 |
| `uv lock --check` | 通过；项目锁版本为 `0.8.1` |
| sdist | `video_download_control-0.8.1.tar.gz` 构建通过；最终大小与 SHA-256 以包外 `dist/SHA256SUMS.txt` 为准，避免制品内文档自引用哈希 |
| wheel | `video_download_control-0.8.1-py3-none-any.whl` 构建通过；最终大小与 SHA-256 以包外 `dist/SHA256SUMS.txt` 为准 |
| 隔离 wheel 安装 | Python `3.12.13`；metadata `0.8.1`；`LicenseRef-Proprietary`；10 个 console scripts；31 个 legal files |
| 缺失工具根 | 只读 `status` 返回 `state=invalid` / `detail_code=bundle_invalid`；强校验 `verify` 以 exit `2` fail closed |
| wheel/source 一致性 | 本轮关键模块 `yt_dlp_contract.py`、`candidate_worker_cli.py`、`toolchain.py`、`web.py`、`__init__.py` 的逐字节 SHA-256 一致 |

这部分回归证明代码修改已进入 0.8.1 制品，但仍不把两个直接工具链样本扩大解释为控制端到隔离 Worker 的端到端下载。
