# Iteration 0.23.0 — 源码首次安装与修复

日期：2026-09-04。源码安装/修复、安装后的正常运行及全量回归已完成；这是本轮工程验收，不是下载器全部需求或独立发行包验收。未提交、push 或再分发第三方工具包。

## 已实现

- `Setup-Open-Flame.cmd` → stdlib-first `setup_open_flame.py` → `source_setup`，使用已安装的 Windows x64 CPython 3.12+。不自动下载 Python，不改系统 PATH；关闭 Python launcher 自动安装与 CWD 命令搜索。
- 默认交互确认；`--yes` 明确确认环境改动与所需 PyPI/GitHub 下载。确认前 no/EOF/取消不安装；动作中取消记录 `setup_interrupted` 并返回 130。
- 仅安装现有 hash-lock 的运行依赖；不添加开发包，不安装项目 distribution，源入口直接导入自身源码。两个缓存参数分别关闭对应的索引或工具下载。
- 健康环境复用；未知损坏环境不改写。安装器自建环境有固定所有权标记，失败保留半成品可重试，`--repair` 强制修复锁定依赖。不会 clear、重定位或删除环境，不改业务数据库。
- 工具复用严格安装/校验/smoke；存在但损坏不覆盖。启动共享读锁、安装独占写锁；高级 CLI 不经过此源码锁，修复前需正常停止。
- 新增 6 个固定安装诊断码，沿用独立轮转日志，不记录原始 argv、stderr、Cookie 或本机路径。

## 已复现和修复

通过 diagnose 的实际进程反馈循环定位并修复两类入口问题：CRT `LK_NBRLCK` 实际仍排他，现改为 `LockFileEx` 共享模式；CMD 原先会优先搜索外部 CWD 的 `py.exe`，现禁用该搜索但保持 PATH 不变。共享读/独占写的真实多进程回归通过。语义参考 [Microsoft CRT locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking?view=msvc-170)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)。

真实 Python/venv 子孙进程取消验证使用保留句柄，在测试兜底 Job 关闭前确认进程已退出；原 KeyboardInterrupt 对象不变。真实 pip 配置探针使用临时环境与离线 ensurepip，证明宿主 PIP/proxy/PYTHON 环境与 site pip.ini 不生效。安装命令采用 `-I`、pip `--isolated` 与 `PIP_CONFIG_FILE=os.devnull`，依据 [pip 配置说明](https://pip.pypa.io/en/stable/topics/configuration/)。

## 真实首次安装发现的问题

在无 `.venv` 与工具包的独立源码复制目录，使用精确本地 wheelhouse 安装运行依赖成功；工具下载阶段失败，退出 2、`setup_toolchain_failed` 且 `diagnostic_status=saved`，未创建业务数据库。此为开发中预检，不冒充冻结后的最终构建。

原锁定 FFmpeg daily build URL 连续两次返回 HTTP 404。4 个 yt-dlp artifact 均通过精确 size/SHA-256。BtbN 官方规定只保留最近 14 个 daily build，但每月最后一份保留两年；原 8 月 20 日 URL 被清理是与规则一致的推论，不是删除日志证明。见 [官方保留规则](https://github.com/BtbN/FFmpeg-Builds#release-retention-policy)。

已切换到 [2026-08-31 月末固定构建](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-08-31-13-27)：FFmpeg/ffprobe `n9.0.1-11-ge47273f4d9-20260831`，ZIP 大小 `67201333`，SHA-256 `83a824f0729a69d143c9865125bb86988a11dd388325f0033711045522068aa0`。官方 API digest、官方 checksum 与本地下载一致；9 个 managed EXE/DLL、相同 LGPLv3 文本、实际 `-version` 与离线编解码 smoke 全部通过。实际 configuration SHA-256 为 `1789f7f5ee156a7ffd715d54dcbd6c3e8f15ac2bcac580809d712e7d0b5b0d56`，要求的 flags 全部存在，GPL/nonfree flags 不存在。该 tag 不带 immutable 保证，仍必须逐项检查哈希；不承诺永久可获取。

本机 canonical 工具通过当前锁重新安装，旧工具保留在 ignored `runtime-tools/retained-windows-x64-20260820-v023`，没有删除。仅清理了本轮未采用的临时工具副本。Apache-2.0/NOTICE 未改，第三方声明同步新工具版本及来源；这不是第三方再分发批准。

## 冻结构建：全新环境完整安装

`0.23.0+build.sha256.64a62ca9ccb395d6e559d276feea2dea1fa562da0fb25035ee53838f0eda77cf`

实际根 CMD 使用系统 Python **3.13.14**，从外部中文、空格、`!` CWD 运行；源码复制目录起初无 `.venv`、无工具包。没有用开发环境中的已安装包替代目标安装，没有使用缓存参数替代这次默认联网安装。

| 验证 | 结果 | 耗时 |
|---|---|---|
| `Setup --yes` 默认 PyPI + GitHub 安装 | 退出 0 / ready；未创建业务数据根 | 35.437s |
| 再次 `Setup --yes` | 退出 0；整个目标环境与工具文件内容 hash 均不变 | 6.172s |
| `--repair` + 空 wheelhouse | 预期退出 2；`setup_dependencies_failed`，诊断 saved | 6.422s |
| 再次 `--repair` 默认联网 | 退出 0；可从上述失败恢复 | 27.938s |
| 同一新环境实际 `Start --check` | 退出 0；仅启动时建立业务 DB，端口释放 | 5.734s |

4 个根入口独立绑定 SHA-256（这些文件在 package identity 范围之外），安装前后均未改变：

- `Start-Open-Flame.cmd`: `c2be332cf0956d8197318ad79653ed0dd1469479cc325188da3f5657358257f7`
- `start_open_flame.py`: `e586b7f0b510394e5b79bf7e35c3861b5700ef4dd2862749d78568736681a04e`
- `Setup-Open-Flame.cmd`: `3efa5099e786af0e6281f4abb52d11f054e9a81bb465df79c1e104c7665ece60`
- `setup_open_flame.py`: `3ba1cd7ef2b49682e2dc85a0802dfada633b47924057b823a94c84e65c3c1d40`

## 安装后的正常应用与全量回归

同一新环境的实际 source bootstrap 正常模式在 **6.047s** 内完成 ready、claim gate active、健康 API、HTML 读取、一次浏览器打开请求和正常停止。三个应用进程同 run，退出码 0、残留 0、端口释放、无失败诊断；源码及根入口 hash 未变。

浏览器 opener 被测试钩子拦截，停止使用已有 test-only EOF → SIGINT 桥；这不证明真实浏览器渲染、Explorer 双击或键盘 Ctrl+C。正常模式测试仍引用本地历史 helper，不能把它称为已可随新 clone 分发的独立 release verifier。

完整 `.venv\Scripts\python.exe -m pytest -q`：**1532 passed, 8 skipped in 185.44s**。8 项均为当前 Windows 缺少 POSIX / AF_UNIX / 权限条件，不计作平台通过。默认安装使用 Python 3.13.14，全量开发回归使用 Python 3.12.13；这不证明所有未来 Python 版本均可用。

## 最终静态检查

- 258 个 tracked / nonignored untracked 文件均可读；未发现真实 Cookie、令牌、私钥、已知私有标识，亦未将媒体、数据库、日志、环境或工具二进制纳入清单。命中的路径、UUID、userinfo URL 与宽泛凭据字面值均已分类为合成测试或既有说明。6 个忽略规则探针全部通过。静态扫描不保证排除所有未知秘密。
- 41 份 Markdown、148 个本地链接全部可解析；compileall、whitespace 与离线锁检查通过。
- 13 个运行时外部依赖、uv 的 24 总包（项目 1 + 外部 23）均保持原外部版本；两个 deployment lock 不变。LICENSE、精确 NOTICE 与 Python 许可材料不变；FFmpeg notice 与新锁版本/归档 hash 对齐，许可文本 hash 不变，第三方再分发门禁未解除。

## 未完成

源码安装不等于免 Python EXE；当前源码发行包及可独立运行的发布验收脚本、平台完整样本验收、抖音登录态、实际浏览器/键盘、Linux/Docker 与第三方二进制再分发仍未完成。本轮没有新的平台媒体下载，也不借用旧构建的单样本结果宣称通过当前完整平台验收。
