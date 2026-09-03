# Iteration 0.7.1 debug、使用与许可证证据

> 许可历史说明：本文中的 proprietary/31-files 表述记录 0.7.1 当时的构建事实；项目自有材料已自 0.9.1 起由权利人改授 Apache-2.0，第三方 binary/container/tool-bundle 门禁不变。

> 日期：2026-09-03  
> 环境：Windows 11、Python 3.12.13、uv 0.11.25  
> 版本：`video-download-control 0.7.1`、Schema 8  
> 证据等级：本机 synthetic/offline 工程验收

本记录证明控制面前端、API、SQLite、offline fake Worker、资产提交、备份/恢复、Python 包许可证材料和发布门禁在本机按下述范围工作。它**不证明**真实平台可下载、真实 Cookie 可用、Linux/Docker candidate 可部署、Stage 0 已通过，也不授予任何公开发布权利。

## 1. 本轮修复

- 修复内嵌前端 JavaScript 的换行正则转义，消除页面加载时的语法错误。
- API/网络/非 JSON 错误不再被误报为健康状态；队列“未暂停”不再暗示 Worker 已启动。
- operations 刷新改为非重叠调度；批次轮询加入 generation/request fencing、失败后 5 秒重试和跨批次 stale-response 拒绝。
- 创建、取消、恢复队列、人工复位按钮均在异常路径恢复；新提交会清理旧任务按钮。
- 文本与文件同时提供时明确拒绝，不再静默丢弃其中一种；空名称不写入 import query。
- `/docs` 和 `/redoc` 关闭，避免默认从公共 CDN 加载可漂移的 Swagger/ReDoc 资产；`/openapi.json` 保留。
- Repository 使用可注入且必须 timezone-aware 的 clock；测试不再把真实创建时间与固定历史时间混用。
- 同一时间戳的队列 FIFO 以 SQLite `rowid` 做稳定 tie-break，不再按随机 UUID 排序。

## 2. 真实浏览器与 API 使用复验

最终服务从隔离数据根启动：

```text
validation/local/frontend-demo
```

绑定仅为 `127.0.0.1:8000`，`VDC_ENABLE_X_GRAPH_V2=0`，`VDC_ENABLE_SHORT_LINK_RESOLUTION=0`。浏览器直接打开 `http://127.0.0.1:8000/` 后确认：

- 页面标题、表单、队列状态和四个平台 circuit 状态正常呈现；浏览器 dev log 为空。
- 页面明确显示真实 Worker 未启用、页面不会启动 Worker，以及“队列未暂停”不代表 Worker 已启动。
- 空表单提交显示可见错误，按钮恢复可用。
- 创建并取消 1 个 synthetic YouTube 批次，终态为 `canceled`。
- 提交 5 条输入，覆盖 YouTube、X、Bilibili、Douyin 四种格式，其中一个 `youtu.be` 与既有 YouTube URL 重复；结果为 4 queued、1 duplicate。
- CSV 含 1 条有效和 1 条无效输入；导入结果为 1 queued、1 failed。
- 另行运行 `--offline-fake` 后 5 个 Job 成为 ready，队列 drain 至 idle。fake 产物不会访问任何平台。

最终 API smoke：

| 路径 | 结果 |
|---|---|
| `/` | HTTP 200，前端 HTML |
| `/health` | HTTP 200，`status=ok`、`database=ok`、Schema 8、`worker=not_started` |
| `/health/live` | HTTP 200，进程存活 |
| `/health/ready` | HTTP 200，数据库与持久化队列可接收任务；不代表 Worker 已启动 |
| `/openapi.json` | HTTP 200 |
| `/docs`、`/redoc` | HTTP 404（预期关闭） |
| `/api/v1/operations/queue` | `paused=false`；只表示可接收任务 |
| `/api/v1/platform-circuits` | 四个平台均 `closed` |

## 3. 数据、资产与恢复

本轮隔离 SQLite 终态：

| 实体 | 数量 |
|---|---:|
| Batch | 3 |
| Input record | 8 |
| Source item | 6 |
| Download Job | 6 |
| Attempt | 5 |
| Media Asset | 5 |
| Job–Asset link | 5 |

5 个 synthetic asset 的 `original/source.fake` 均与各自 manifest 中的 size/SHA-256 一致。

- 备份根：`validation/local/frontend-demo-backup`
- 备份 manifest：20 entries、287,224 bytes
- `backup-manifest.json` SHA-256：`d51b0e14f35edec63906520736cf13af1215496f62e3a7cb61ebb0d16d7d2126`
- 独立恢复根：`validation/local/frontend-demo-restore`
- 恢复结果：19 files、286,790 bytes
- 源库与恢复库上述业务实体数量一致；两边 15 个 asset 文件的相对路径与 SHA-256 树完全一致。

这只是同机小数据恢复演练，不是 NAS、异机、离线介质、RTO/RPO 或灾难切换认证。

## 4. 自动化、静态与依赖检查

| 检查 | 结果 |
|---|---|
| 全套 pytest | `750 passed, 8 skipped in 41.00s` |
| UI/API/license/deployment 聚焦回归 | `53 passed, 4 skipped in 5.89s` |
| clock/FIFO 受影响集合 | `39 passed`；原随机顺序用例连续 `8/8` 通过 |
| `uv lock --check` | 通过，24 packages |
| `compileall` + version/schema | `0.7.1 8` |
| Ruff `E9,F63,F7,F82` | `src`、`tests`、validator 通过 |
| 新增合规文件默认 Ruff | 通过 |
| 全库未配置默认 Ruff | 91 条历史风格建议，其中 53 条可自动修；不是当前门禁 |
| `pip-audit` | 当前环境、runtime lock、build lock 均未发现已知漏洞；这是时间点扫描，editable project 无 PyPI 漏洞记录 |
| Bandit | 0 high、10 medium、20 low；10 个 medium 均逐项确认为参数占位符/固定 allowlist 误报，未发现真实 P0/P1 |

8 个 full-suite skip 是环境边界，不视为通过：1 个 POSIX Cookie directory-FD cleanup、4 个 root POSIX/getfacl source-metadata contract、1 个真实 AF_UNIX roundtrip、2 个 POSIX path-swap/permission test。它们必须在目标 Linux 重跑。

Bandit low 项为受控 subprocess、内部 invariant 与 best-effort cleanup/audit sink。后续可以用显式异常替换安全边界里的 `assert`，并对 Windows `taskkill.exe` 路径增加系统目录 API 与 executable/reparse 校验；本轮未用全局 suppression 掩盖结果。

## 5. 许可证与发行门禁

### 已闭合：私有使用与 Python 包材料

- 项目自有代码使用 `LicenseRef-Proprietary`；根 `LICENSE` 明确不授予公开许可。因此当前仓库**不是开源项目**。
- `THIRD_PARTY_NOTICES.md` 精确覆盖 runtime/dev/build lock 的 26 个外部 Python distributions。
- `licenses/python/` 包含 29 份上游法律文件；`compliance/python-license-files.sha256` 的文件集合与 SHA-256 全部匹配。
- wheel/sdist 均携带 31 个 legal files（项目 `LICENSE`、notices、29 份第三方文件）；wheel 使用 Metadata 2.4、`License-Expression: LicenseRef-Proprietary` 和 31 条 `License-File`。
- `/docs`、`/redoc` 已关闭，当前前端没有 vendored 或运行时 CDN JavaScript/CSS/binary。
- `deployment/validate_tool_bundle.py` 对 yt-dlp/FFmpeg/ffprobe 的 executable、source artifact、SBOM、人工批准记录、法律文件和 FFmpeg configuration 做 hash-bound、allowlist、fail-closed 校验；`--enable-nonfree` 被拒绝，GPL configuration 必须得出 GPL 结论。

相关标准与上游边界：PyPA 的 [Core Metadata](https://packaging.python.org/en/latest/specifications/core-metadata/)、[licensing guidance](https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/)、[license expressions](https://packaging.python.org/en/latest/specifications/license-expression/)、[PEP 639](https://peps.python.org/pep-0639/)、[SPDX license expressions](https://spdx.github.io/spdx-spec/v3.0.1/annexes/spdx-license-expressions/)、[yt-dlp repository](https://github.com/yt-dlp/yt-dlp) 与 [FFmpeg legal page](https://www.ffmpeg.org/legal.html)。

### 仍 blocked：公开或容器发布

1. 权利人必须先明确选择并授予项目公开许可证；当前 proprietary 文本不能被解释成开源授权。
2. `pydantic-core 2.46.5` 的 Linux amd64 调查得到 root + 87 个 crates、无未知或强 copyleft，但 148 个 crate legal-file instances 尚未组成 target-specific Cargo SBOM/license bundle；其他架构必须分别重算。
3. Python base 的 multi-arch index digest 不等于目标 child image 许可证审计；实际目标架构的 Debian/CPython/pip/OS packages、SBOM 与源码义务仍须审阅。
4. 实际 yt-dlp/FFmpeg/ffprobe tool image 尚未构建；许可证随具体 artifact、FFmpeg configure options 与 linkage 改变，必须审核最终二进制和来源闭包。

validator 只证明输入证据格式、allowlist 和哈希关系正确，不替代权利归属、许可证解释或法律意见。

## 6. 最终制品

当前可验收制品仅为：

```text
dist/video_download_control-0.7.1.tar.gz
dist/video_download_control-0.7.1-py3-none-any.whl
```

两者的最终哈希只以 `dist/SHA256SUMS.txt` 为准。旧 0.7.0 及更早文件是整改前历史包；不得使用 `dist/*` 通配上传或交付。

wheel 已在独立 venv 以 `--offline --no-deps` 安装并确认：版本 0.7.1、9 个 console entry points、`LicenseRef-Proprietary`、31 个 license files。

## 7. 明确未执行

- 任何真实 YouTube、X、Bilibili、Douyin 请求或媒体下载；
- 任何真实 Cookie、登录会话或私有样本；
- Docker build/pull/up、Linux namespace/UDS/ACL/core-pattern/resource/runtime-bind 验收；
- Stage 0 的每能力单元 10+ 正向、独立负向与连续三轮；
- X exact attachment selector、真实短链 TLS/DNS/redirect；
- 公开源码、公开二进制或容器发布。

因此，本轮可验收结论是：**本地控制面、synthetic/offline 使用链路、Python 包许可证材料和 fail-closed 发布门禁已通过；真实平台能力与公开/容器发布尚未通过。**
