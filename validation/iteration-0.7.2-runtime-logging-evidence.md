# Iteration 0.7.2 运行日志、调试与使用验收证据

> 许可历史说明：本文中的 proprietary/31-files 表述记录 0.7.2 当时的构建事实；项目自有材料已自 0.9.1 起由权利人改授 Apache-2.0，第三方 binary/container/tool-bundle 门禁不变。

> 日期：2026-09-03  
> 版本：`video-download-control 0.7.2`、Schema 8  
> 范围：Windows 本机、synthetic 输入、离线 fake Worker；不是 Stage 0、真实平台或 Linux/Docker 验收

## 1. 结论

本轮新增的本机运行日志已贯通控制面、offline/candidate Worker 和受控子进程。活动文件位于 `${VDC_DATA_ROOT}/logs/`，使用一行一个对象的 JSONL、固定 schema、run/event/request/job/attempt 关联 ID、run 内单调 `sequence` 和有界轮转。前端新增“运行日志”区域，后端新增只返回通过 schema/语义验证事件的读取 API。

日志严格排除 URL/query/body、批次名、输入文本、命令参数、环境变量、工作目录、绝对路径、stdout/stderr 和异常消息。本轮 synthetic 敏感标记扫描为 0 命中，全部 JSON 行可解析，业务备份和恢复均未包含顶层 `logs/`。日志仍是 best-effort 诊断线索，不是审计账本，也不替代数据库、资产 manifest、备份证据或生产监控。

## 2. 实现覆盖

| 边界 | 已记录 | 明确不记录 |
|---|---|---|
| 控制面 | 初始化/启动/停止、模板化 HTTP route、status、duration、服务端 request ID、批次与队列/熔断操作 | 原始 path/query、header、body、URL、批次名、输入文本、异常消息 |
| Worker | lifecycle、claim、phase、retry、heartbeat、cleanup、pause、terminal result、job/attempt/run ID | canonical URL、目标路径、凭证、adapter 原始错误、资产内容 |
| 受控子进程 | executable basename、参数个数、duration、return code、stdout/stderr byte count、固定失败类别 | argv、env、cwd、完整 executable path、stdout/stderr 内容 |
| 日志读取 | 允许的文件名、regular-file 身份、完整 schema/字段/枚举/时间/ID 验证，近期事件排序 | symlink/reparse/hard-link、未知文件、未知字段、畸形或超界事件 |

默认配置：

- `VDC_RUNTIME_LOG_LEVEL=INFO`
- `VDC_RUNTIME_LOG_MAX_BYTES=10485760`（10 MiB）
- `VDC_RUNTIME_LOG_BACKUP_COUNT=5`

写入失败不会让下载控制业务失败；logger 将状态降为 `degraded/error` 并累计 `write_failures`。前端只用安全文本节点显示日志，支持手动刷新最近 100 条。

## 3. 自动化回归

| 检查 | 结果 |
|---|---|
| 全套 pytest | `788 passed, 8 skipped in 47.51s` |
| 运行日志/API/CLI/Worker/子进程聚焦 | `68 passed` |
| Worker 日志接入聚焦 | `34 passed` |
| 子进程日志安全边界聚焦 | `20 passed` |
| UI、备份排除与文档聚焦 | `38 passed` |
| 许可证、tool bundle 与 deployment 最终聚焦 | `41 passed, 4 skipped in 5.10s` |
| `uv lock --check` | 通过；版本锁与 0.7.2 一致 |
| `compileall` + version/schema | 通过；`0.7.2 8` |
| Ruff 致命语法/未定义 + 本轮变更默认规则 | 通过 |
| `pip-audit` 当前环境、runtime lock、build lock | 未发现已知漏洞；本地 editable project 无 PyPI advisory 映射 |
| Bandit | 0 high；10 medium 为已审阅的 SQL 参数占位符/固定 allowlist；22 low 为受控 subprocess/crypto/invariant/best-effort 路径 |

8 个 skip 与 0.7.1 相同，均为 Windows 无法证明的 POSIX/root/getfacl/AF_UNIX/path-swap/permission 条件；必须在目标 Linux 重跑，不计作通过。

## 4. 真实浏览器与 API 链路

本轮使用 Codex 内置浏览器对 `http://127.0.0.1:8000/` 做真实 DOM/交互测试，服务使用独立数据根：

```text
validation/local/runtime-log-demo/
```

结果：

1. 页面显示 `Iteration 0.7.2`，运行日志状态为“正常”，写入失败 0、拒绝事件 0。
2. 从前端提交 3 条 synthetic 输入：两个唯一 URL 加一个规范化重复；创建结果为 `2 queued / 1 duplicate`。
3. 在同一独立数据根启动显式 opt-in 的 `video-download-worker --offline-fake --drain`，只生成本地假资产，不访问平台。
4. 最终批次为 `ready`：`2 ready / 1 duplicate / 0 failed`；两个 Worker job 均有 claim、preparing、probing、downloading、verifying、committing、finished 事件。
5. `/health` 返回 HTTP 200、`status=ok`、`database=ok`、Schema 8；OpenAPI version 为 0.7.2。一轮 Worker drain 结束后 `worker=not_started` 是预期状态。
6. 前端手动刷新后可见 control 与 offline-worker 的交错事件，并可按 `job_id`、`attempt_id`、`run_id` 和 `sequence` 关联。

## 5. 运行日志结构与泄漏扫描

点位快照：

```text
runtime-control.jsonl
runtime-offline-worker-ce03edcc1ea9.jsonl
JSONL lines: 133
runs: 2
components: control, offline-worker
parse failures: 0
run sequence failures: 0
write failures: 0
rejected events: 0
```

使用批次名、URL query、重复参数、Bilibili source ID、客户端 `X-Request-ID` 和额外 GET query 中的不同 `RUNTIME_LOG_SECRET_*` 标记做黑盒扫描：

```text
RUNTIME_LOG_SECRET marker hits: 0
http:// or https:// hits: 0
client request-id marker hits: 0
query marker hits: 0
Uvicorn console access-log hits: 0
```

服务忽略客户端给出的 `X-Request-ID`，本轮响应返回新的 32 位十六进制 ID。Uvicorn access log 已关闭，避免原始请求目标落入 stderr；应用日志只保存 route template，例如 `/api/v1/batches/{batch_id}` 或 `/unmatched`。

## 6. 备份与独立恢复

对同一 live 数据根执行 `video-download-backup create`：

```text
status=ok
schema_version=8
file_count=8
manifest_sha256=b4b1b08fce57b0eabf97487e7225d04781e997655b800cb9de350b36cf651dd9
payload/data/logs exists=false
manifest logs-path hits=0
source logs still exist=true
```

随后恢复到不存在的独立根：

```text
status=ok
schema_version=8
file_count=7
restored database exists=true
restored logs exists=false
```

因此排障日志既不会混入可恢复业务状态，也不会因备份而删除或移动源日志。

## 7. 构建、依赖与许可证

- 版本已统一为 0.7.2；`uv build` 生成 wheel 与 sdist，当前文件和 SHA-256 只以 `dist/SHA256SUMS.txt` 为准。
- wheel/sdist 各携带 31 个法律文件；wheel metadata 为 `LicenseRef-Proprietary`、31 个 `License-File` headers，隔离离线安装确认 9 个 console entry points。
- 当前环境、runtime lock 和 build lock 的 `pip-audit` 均未发现已知漏洞；这只是 2026-09-03 的时间点结果。
- Python 包的许可元数据与随包法律文件完整；只有使用者是项目权利人或另有书面授权时，才能说相应私有使用未受包内材料阻断。根 `LICENSE` 本身不向第三方授予 use/copy/modify/distribute 权利。
- 权利人没有授予公开许可证，所以仓库、wheel、sdist 和容器仍不得被描述为开源发布。纯 Python wheel 不捆绑 FFmpeg/yt-dlp 或原生依赖，但项目自身的权利授予仍先行阻断公开分发。
- 如需公开/容器分发，仍须先由权利人选择公开许可证，并按实际 target/architecture 闭合 Python 原生依赖、base-image OS packages、yt-dlp 与 FFmpeg/ffprobe tool bundle 的 SBOM、来源/源码提供义务、法律文本和人工批准。

参考的权威边界包括 [PyPA licensing guide](https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/)、[PEP 639](https://peps.python.org/pep-0639/)、[FFmpeg Legal](https://www.ffmpeg.org/legal.html) 和 [yt-dlp license](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE)。这些资料与自动 validator 都不替代针对实际发布物的法律判断。

## 8. 尚未完成、不得外推

- 未执行 Docker build/pull/up、target Linux runtime、namespace/UDS/ACL/core-pattern 或 Cookie bind 验收。
- 未安装或运行真实 yt-dlp、FFmpeg、ffprobe；未发送真实平台请求，也未使用真实 Cookie。
- 未执行用户授权样本的 Stage 0；四平台能力仍是 `candidate`。
- 未建设集中日志、留存策略、告警或远程支持上传；当前日志仅在本机数据根内轮转。
- `LicenseRef-Proprietary` 不是开源许可证。公开源码、wheel/sdist、二进制或容器分发继续 fail closed。
- `dist/` 还保留旧版本历史包；它们不是当前交付物，禁止用 `dist/*` 批量分发，只能精确选择经当前哈希清单覆盖的 0.7.2 文件。
