# 中文字幕上屏样式、换行与显示时长

验证日期：2026-09-11（Asia/Shanghai）

## 结论

当前源码把原本独立的本机中文字幕流程（ComfyUI `ComfyUI-VideoTranslator-Biliup`）中的
**字幕上屏能力**合并进编辑域。翻译目标语言为中文时，`AiRenderProcessor` 除既有
`caption.vtt` 外，再按同一批已批准 cue 生成：

- `caption.srt`：同样文本的 SubRip 侧车；
- `caption.ass`：带样式的 Advanced SubStation Alpha（`Microsoft YaHei` 48，1920×1080
  script 分辨率），可直接交给后续烧录步骤；
- `caption-checks.json`：这批字幕的可读性与样式观察记录。

新增 `src/video_download_control/editing/captions.py` 是纯函数模块：不访问文件系统、不调用
provider、不读时钟，一份字幕文件只由「已批准 cue + 一套样式」唯一决定。

## 只改上屏，不改已批准结果

- **不修改 translation revision**。已批准译文保留自然标点，因为那是审核者读到的文本，也是
  配音需要的断句。上屏样式只作用在写出的字幕文件上。
- **不修改 cue 时间**。`hold_captions()` 只在写字幕文件时延长*显示*结束时间；传给语音合成
  与 PCM 对轨的仍是原始 cue，配音对齐不受影响。
- **不改 AI 请求**。翻译 prompt 未改动，因此 request fingerprint、authorization 摘要与调用
  账本都不变。样式由代码保证，不依赖模型是否遵守指令。
- **目标语言非中文时行为逐字节不变**：仍只写 `caption.vtt`，内容与本切片前相同。

## 上屏标点规则

字幕一眼读完，它自己的边界已经结束了这句话，所以句读标点多数只是噪音。

- 句号、问号、叹号、分号、冒号、顿号、省略号、破折号、引号与各类括号一律删除。
- 全角逗号 `，` 是唯一保留的标记，且只在两侧文本都足够长（默认各 6 字）时由被删标记转成，
  每行至多一个。
- token 内部的标记保留，因此 `3.5`、`12:30`、`R-301`、`K/D` 不受影响。
- 折行处的逗号删除：换行本身已经标出那个停顿。
- 归一化后若整条为空，则原样保留原文，宁可留下标点也不留空屏。

## 换行与显示时长

- 按字符宽度折行（默认每行 22 字），优先在最后一个可断处断开。
- `hold_captions()` 解决“字幕消失太早”：强制对齐把词的结束标在最后一个可检出音素上，
  在那一刻切断字幕会在说话人尚未说完时清屏；译文中文往往也比原文更费时间读完。规则为
  在原结束时间上取三者较大值——`+250 ms` 收尾、最短 `1.0 s`、按 9 字/秒读完所需时间——
  并以「下一条开始前 100 ms」为上限。

  不变量：起始时间从不移动；结束时间从不缩短；不制造新的重叠；不超出该输出窗口的媒体
  长度。原本就重叠或紧邻的两条保持原样。

## 与本机流程的差分验证

`normalize_caption_text`、`stray_caption_punctuation`、`wrap_caption_text` 与本机流程
`vtb/subtitles.py` 中对应实现逐字节比对：**40,080 条输入**（真实 31 条已审核字幕、其英文原文、
18 条手写边界用例，以及在中文/拉丁/数字/全部相关标点字符集上随机生成的 40,000 条），
`min_comma_run` 取 1/6/12、折行宽度取 1/8/22/40，全部输出完全一致，无差异。

## 真实字幕数据上的显示时长结果

用本机两份真实运行结果（未经翻译 provider，仅重放已有已审核字幕）：

| 数据 | 条数 | 最短时长 | 不足 1 秒 | 被延长 | 条间空屏合计 |
|---|---|---|---|---|---|
| `examples/Caption-test`（2 分钟样片） | 31 | 0.64 s → **1.00 s** | 2 → **0** | 25/31，中位 +250 ms | 43 s → 37 s |
| 1004 秒完整视频的已审核字幕 | 275 | 0.52 s → **1.00 s** | 12 → **0** | 222/275，中位 +250 ms，最大 +479 ms | 232 s → 178 s |

275 条那份在延长前有 3 条触发 `caption_reading_speed_high`，延长后为 0。四项不变量在两份
数据上都成立。

同一视频的强制对齐缓存（466 个已对齐 segment、5,821 个词、切分为 567 条字幕）给出问题
规模：**566 条中有 373 条（66%）在字幕结束后 300 ms 内就有下一个词出声**，其中 215 条在
100 ms 内；最短字幕 0.12 秒。这正是“说话人还没说完字幕就没了”的来源。

## 渲染路径实测

用一个只报告媒体事实、并替代分段渲染的 stub `MediaProcessor` 驱动真实
`AiRenderProcessor.render()`（关闭 translation 之外的一切；没有 provider、没有 FFmpeg）：

- 目标语言 `zh-CN`、单输出：产出 `caption.vtt`、`caption.srt`、`caption.ass`、
  `caption-checks.json` 四个 `caption` 成品，登记的名称集合与目录内文件逐项一致，
  `size_bytes` 与 `sha256` 与实际字节吻合。
- 目标语言 `en`、同一批 cue：只产出 `caption.vtt`，首条时间轴为 `00:00:00.300 --> 00:00:05.223`，
  即已批准 cue 的原始时间，未被本切片改动。
- 目标语言 `zh-CN` 时同一条为 `00:00:00.300 --> 00:00:05.473`（+250 ms 收尾），
  `caption.srt` 为 `00:00:00,300 --> 00:00:05,473`，`caption.ass` 为
  `Dialogue: 0,0:00:00.30,0:00:05.47,Default,…`，三份文件的 31 条 cue 数量一致。
- 两个分段（边界对齐到真实 cue 边界）：按输出各得一套
  `caption-001.*` / `caption-checks-001.json` 与 `caption-002.*` / `caption-checks-002.json`。

这次实测在 macOS 上必须临时把 `ai_render._REPARSE_POINT` 置 0：`stat.FILE_ATTRIBUTE_REPARSE_POINT`
在所有平台都有定义（本机为 1024），而 `os.stat_result.st_file_attributes` 只在 Windows 存在，
因此 `_plain_directory()` / `_plain_file()` / `_assert_signature()` 在 POSIX 上一律抛
`AttributeError`，`AiRenderProcessor.render()` 在 macOS/Linux 根本无法进入。这是**本切片之前
就存在、与字幕无关**的可移植性缺陷，产品面向 Windows 所以平时不会触发；本切片没有改动它，
应另行修复。

## 检查项

`caption-checks.json` 只记录观察，不阻止渲染，也不是对翻译正确性的判断：

`caption_line_too_long`、`caption_reading_speed_high`（超过 12 字/秒）、
`caption_punctuation_not_allowed`、`caption_multiple_commas`、`caption_overlaps_previous`、
`caption_empty`。

## 回归

本机 macOS 15（Darwin 25.5.0）、`uv 0.12.5`、锁定 `uv sync --extra dev --frozen`
（CPython 3.12.13）：

- `python -m compileall -q src` 通过。
- `TESTING.md` 第 4 节稳定回归集：本切片 **40 failed、229 passed、5 skipped**；同一命令在
  未改动的 `d2c4b639` 上为 **完全相同的 40 failed、229 passed、5 skipped**。本切片未引入
  新失败，那 40 项是 `TESTING.md` 已声明的历史上传用例。
- 另跑 asset/editing/workflow 相关 10 个文件：改动前后失败集合逐项相同（各 59 项），
  既没有新增失败，也没有因本切片而变绿。

按 `AGENTS.md`，本提交未新增或修改 `tests/` 下任何文件。

## 未验证边界

- **没有烧录**。本切片只产出侧车字幕文件，没有把字幕压进视频，也没有新增 recipe 字段、
  Editing Schema、API 或页面开关。`caption.ass` 中的 `Microsoft YaHei` 只是样式声明，本轮
  没有在 Windows 上用 libass 实际渲染过，也没有核对该机是否装有该字体。
- **没有在 Windows 上运行**。本机为 macOS，编辑域 `MediaProcessor` 固定使用工具目录中的
  `ffmpeg.exe`，因此本轮没有执行任何真实 FFmpeg 渲染，也没有走完整 render plan。字幕层
  是在真实字幕数据上单独验证的，不是一次端到端产品运行。
- **没有真实 provider 调用**，没有平台投稿，也没有浏览器页面核对。`/edits` 成品列表会把
  新的三个文件按既有 `caption` 类型显示，本轮未在浏览器中确认。
- 检查项只针对结构与可读性，不保证识别或翻译内容正确，重叠人声、专有名词仍需人工核对。
