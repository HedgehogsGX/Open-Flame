# 本地源码发行包与独立验收

面向维护者；普通 Windows 用户解压 `Open-Flame-VERSION-source.zip` 后，按 [首次安装与修复](WINDOWS_SETUP.md) 运行根目录 Setup / Start 即可。源码包仍需要 **Windows x64 CPython 3.12+**，并不包含 Python、FFmpeg、yt-dlp 或依赖 wheel；不是免 Python EXE。

## 构建

在源码根目录、使用已安装的 Python 3.12+。不需要 Git、uv、已有项目虚拟环境或 `validation/local` 中的旧 verifier。以 PowerShell 为例，输出路径必须是**尚不存在**的绝对目录，其父目录应已存在：

```powershell
py -3 -I .\scripts\release.py build --output 'C:\OpenFlameBuild\candidate-01' --allow-network
```

`--allow-network` 明确允许从 PyPI 安装 [锁定的构建依赖](../deployment/requirements.build.lock)。只在新输出目录创建构建环境，不修改系统 Python 或当前 `.venv`。已有精确匹配缓存时无需联网授权：

```powershell
py -3 -I .\scripts\release.py build --output 'C:\OpenFlameBuild\candidate-02' --wheelhouse 'C:\OpenFlameCache\wheels'
```

`--wheelhouse` 使用 `--no-index`，缺项失败，不回退联网；构建只需要 build lock 对应的五个包。命令失败保留独立输出目录供检查，不覆盖旧包、不自动清理数据。更换新的输出名重试。

只交付输出下的 **`release` 子目录**中的五个文件，不要交付整个输出目录：

- `Open-Flame-VERSION-source.zip`：Windows 用户便于解压的源码包，含四个 Setup / Start 入口。
- `video_download_control-VERSION.tar.gz`：标准源码分发包。
- `video_download_control-VERSION-py3-none-any.whl`：只含项目包的开发者 wheel；不含根启动器，也不自带第三方运行依赖。
- `release-manifest.json`：文件清单、每文件大小/摘要、精确包身份和四个根入口摘要。
- `SHA256SUMS`：上述三个制品与 manifest 的 SHA-256。

生成器先按 [release-files.txt](../release-files.txt) 的明确清单冻结工作树字节，再构建；不使用 Git index/archive 字节代替当前文件。新增源文件、文档、测试或脚本需要人工复核后更新清单。包树中存在未列出的可导入文件会拒绝构建；清单之外的日志、下载内容、缓存、凭据和历史制品不打包。历史许可迁移证据含自引用摘要，因此不打包，其余选择以清单为准。

## 核对已有制品（只读）

```powershell
py -3 -I .\scripts\release.py verify --release-dir 'C:\OpenFlameBuild\candidate-01\release'
```

核对 ZIP / sdist 的源码逐字节一致性、wheel 包载荷、完整 `RECORD`、13 个命令入口映射、Apache-2.0 元数据与 32 份法律文件。归档成员须为普通相对路径，无重复、Windows 大小写碰撞或链接。manifest 在归档外，避免自引用；校验不执行包内代码。

源码包的原始文件字节决定 identity，四个根入口另行记录；Git 换行转换可能改变身份。SHA-256 是完整性校验，**不是签名或发布者身份认证**，须从可信渠道比较摘要。显式清单、纯文本限制和高置信度规则只能发现部分隐私问题；对外分享前仍需人工查看本轮变更。

## Windows 解压后安装 / 启动验收

该命令实际创建独立环境、安装工具并运行 Start `--check`，不是只读。`--work-dir` 必须尚不存在；所有测试业务数据、日志和环境保留在该目录，不碰日常默认业务目录，也不占用 8000 端口。

```powershell
py -3 -I .\scripts\verify_windows_release.py --release-dir 'C:\OpenFlameBuild\candidate-01\release' --work-dir 'C:\OpenFlameBuild\install-check-01' --allow-network
```

完全走本地缓存时，同时传 `--wheelhouse 'C:\OpenFlameCache\wheels'` 与 `--artifact-cache 'C:\OpenFlameCache\tool-archives'`，不需要 `--allow-network`。运行缓存须兼容测试所用 Python ABI；例如 cp312 wheel 不能用于 Python 3.13。缓存内容与联网范围详见 [安装指南](WINDOWS_SETUP.md)。

验收包含：原始制品核对、真实解压源码、中文/空格/`!` 路径、外部 CWD 下的真实隐藏 CMD、首次安装、重复安装无内容变化、Start `--check`、端口释放及身份不漂移。失败保留固定阶段报告和本地原始日志；只分享检查过的报告，不打包原始日志或测试环境。

`--check` 不领取下载任务；它不是实际浏览器渲染、键盘 Ctrl+C、真实平台下载或免 Python 分发验收。工具链本地安装和第三方二进制再分发是不同事项，参见 [第三方声明](../THIRD_PARTY_NOTICES.md)。这些脚本不 commit、push、上传或创建 GitHub Release。

## wheel 独立安装验收

这条命令另建虚拟环境，按 runtime lock 安装运行依赖和已核对的项目 wheel，执行 `pip check`、检查导入位置与精确包身份，并实际调用 13 个已安装的 console scripts 的 `--help`。不安装开发依赖，不领取媒体任务；它不替代上面的源码 CMD 测试。

```powershell
py -3 -I .\scripts\verify_wheel_release.py --release-dir 'C:\OpenFlameBuild\candidate-01\release' --work-dir 'C:\OpenFlameBuild\wheel-check-01' --allow-network
```

或者用 `--wheelhouse 'C:\OpenFlameCache\wheels'` 替代联网授权。默认无缓存、无明确联网授权时拒绝创建环境。只在新的 `--work-dir` 中保留运行依赖与报告；不要把该目录当作可再分发的项目 wheel。

## 规范依据

构建使用锁定的 Hatchling；文件选择与固定构建时间见 [Hatch 构建配置](https://hatch.pypa.io/1.13/config/build/)。wheel 的 `RECORD`、许可证目录与 metadata 检查依据 [PyPA wheel 规范](https://packaging.python.org/en/latest/specifications/binary-distribution-format/) 和 [Core metadata](https://packaging.python.org/en/latest/specifications/core-metadata/)。符合这些机械检查不构成法律批准。
