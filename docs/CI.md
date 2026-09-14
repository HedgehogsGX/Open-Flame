# 无凭据持续集成

`.github/workflows/ci.yml` 在 push、pull request 和手动触发时运行 Windows 与 Linux、CPython 3.12.10 与 3.13.14 的四格矩阵。3.12.10 是当前 `actions/setup-python` 清单中可在 Windows x64 取得的固定 3.12 patch；原先的 3.12.13 没有对应 Windows x64 artifact。每格安装精确 `uv 0.11.25`，以矩阵版本设置 `UV_PYTHON`，并用 `uv sync --extra dev --locked --python ...` 建立开发环境。`UV_NO_SYNC=1` 阻止后续 `uv run` 静默改写该环境；CI 在测试前断言实际 Python patch 与 pytest 身份。Node 目前只在 `Verify runtime identities` 输出版本，不代表另有一条独立 Node UI harness。

工作流权限只有 `contents: read`，checkout 不保留 Git 凭据，也不读取 GitHub secrets、平台 Cookie 或上传账号。依赖安装本身会访问受 GitHub Actions 和包索引控制的外部服务；测试阶段依赖现有 synthetic/fake backend 与本地 HTTP fixture，不授权真实下载、扫码、登录或上传。普通 hosted Linux job 也不具备 T15 所需的可信 effective-root、getfacl、Unix socket、network namespace、容器 daemon 与冻结 image identity 证据，因此相应 skip 仍是未验收。

三个外部 action 与 uv 版本都固定在工作流中；`scripts/verify_ci_contract.py` 检查精确 action commit、矩阵、只读权限、无 credential trigger、locked install、完整测试和 whitespace gate，并把换行规范化后的完整 workflow 字节绑定到已审 SHA-256。完整字节绑定会拒绝 flow mapping、YAML 续行、anchor/alias 等未逐项建模的改写，避免文本规则与 YAML 解释结果分歧。工作流以 `fetch-depth: 0` 获取完整历史，并在安装依赖前运行 `scripts/verify_commit_scope.py --github-event`。提交范围检查从 push、pull request 或手动触发事件取得 base/head，在完整历史上求 merge-base；新分支 push 使用默认分支，单提交仓库回退到空树。它读取 NUL 分隔的 name-status，重命名同时检查旧、新路径，删除历史测试不被误拦截，新增或修改自动化测试则失败关闭。最后两步分别检查工作树 whitespace 和当前提交相对父提交的 whitespace。

`tests/` 中已有回归保留给本地与 CI 运行，但不进入发行包。后续功能提交不得新增或修改自动化测试文件；本地 hook 与 hosted commit-scope gate 都会拒绝这类变更，专门清理提交仍可删除历史测试。临时 smoke 与诊断脚本写入已忽略的 `validation/local/`。

## 当前托管运行状态

2026-09-15 重新读取 GitHub：提交 `7b48a9fe4dae09279a3e986642af68263386e796` 的
[push run 34807785099](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34807785099)
和 [PR run 34807788883](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34807788883)
均 completed/success，Windows/Linux × CPython 3.12/3.13 共八格全部 success。
PR 评论中 `0dfcf82` 的红格属于下方历史记录；该旧失败确切原因仍不能从缺少 stderr 的
日志推定。后续提交仍需核对自己的 CI，不继承这里的绿色结论。

本轮删除可重复使用的测试补丁摘要豁免。`--staged` 一律拒绝新增/修改自动化测试；
范围检查仅在 merge-base 精确等于 `d481326`、head 包含已审 `7b48a9f`，且净测试差分
与这段固定历史逐字节一致时接受本 PR 的既有维护。新测试内容、缺少已审提交的复制历史、
base 前移及合并后再次应用旧补丁均不在该范围。固定历史校验允许本 PR 首次 merge 的
正常 push 检查；后续 base 包含已合并历史时自然失效，再删除这两个历史端点及比较函数。
这些端点不随迭代追加，也不是仓库所有者授权的远端证明。

2026-09-14 维护前核对：提交
[`0dfcf82b7896e2a61b1201f3f974a5f7adf3dcdd`](https://github.com/HedgehogsGX/Open-Flame/commit/0dfcf82b7896e2a61b1201f3f974a5f7adf3dcdd)
的 [push run 34777870858](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34777870858)
四格 success；[PR run 34777873312](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34777873312)
的 Windows / CPython 3.13.14 在诊断日志 rotate=True 并发用例失败，其他三格 success。
两套 run 均已 completed。原日志缺少子进程 stderr，确切 hosted 根因未被证明。

此前 `d970bce` push 的 Bilibili 封面失败、`9822454` PR 的视频号封面失败保留在
[原记录](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-storage-soak-and-ci-follow-up.md)。在暂停 backend 的
隔离探针中，三个现有封面用例均可确定性复现过早检查删除；backend 列表已有记录不能证明
任务已经提交或后续清理完成。本次按用户提交指令应用 v3：三个函数各增加一行 submitted
等待，另一个诊断日志用例只设置子进程锁预算并补充断言详情。原断言条件、独立超时测试、
收集范围和生产默认预算保持；真实两模块回归 73 项通过，精确范围与摘要见
[维护记录](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-async-test-maintenance.md)。本次维护提交的 CI
以 PR 同提交检查及交付记录为准，上述维护前结果不自动覆盖它。
PR #2 仍未合并。此前签名和隔离验收见
[隔离验收记录](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-release-readiness-drill.md)；此前 `7575773` 的
安装 receipt 与合并门禁准备见[发布准备基线](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-release-readiness-baseline.md)。

`3e482f0` 和 `ac3532b` 的限定测试维护已获批准并应用，Windows SQLite sidecar 竞争由
`8564b30` 修复，详见[CI 合同维护记录](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-ci-contract-maintenance.md)。
这两份冻结测试差分不构成后续修改测试文件的通用权限。

本次查询 main 返回 `protected=false`，应用规则为空；required checks / branch protection
仍为 **NOT CONFIGURED**。包含上级规则的 ruleset 清单为空，当前 API 身份未显示 admin
权限；四个实际 context、GitHub Actions 来源、完整配置选择与验证步骤见
[main 门禁提案](MAIN_MERGE_GATE.md)。配置及实际阻断验证仍是独立待办。

## 历史失败：2026-09-10，bdd88ce

[GitHub Actions run 34393235622](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622) 精确绑定提交 [`bdd88ce184b2f86f957f7df9129baa21863227dd`](https://github.com/HedgehogsGX/Open-Flame/commit/bdd88ce184b2f86f957f7df9129baa21863227dd)。2026-09-10 的最终公开状态显示，四格均已通过 checkout、CPython 选择、commit scope、固定 uv、CI definition、runtime identities、locked development environment、`uv pip check` 及矩阵 Python/pytest 身份检查，随后四格都在 `Run the offline test suite` 失败；run 总结论为 `failure`。逐格链接与最终状态记录在[托管 CI 恢复证据](https://github.com/HedgehogsGX/Open-Flame/blob/7b48a9fe4dae09279a3e986642af68263386e796/validation/iteration-0.28.0-hosted-ci-recovery.md)。

同一源码的本机全量结果是 **173 failed, 2277 passed, 8 skipped in 327.39s**。失败主要暴露既有测试与当前产品合同的漂移：大量下载 API 测试仍使用 `Host: testserver` 且未先获取下载域 session/提交 `X-Download-CSRF`，另有 Editing Schema 1、旧 release metadata/version、旧 UI 导航和 validation identity 断言。仓库策略禁止修改这些测试，本轮也没有在产品中加入测试绕过。因此当前结论是“托管执行链与前置门禁已恢复，完整测试仍红”，不能称为 CI 绿色。失败后的 whitespace steps 也没有在这些 job 中运行。

以上是该旧提交的失败记录，后续限定维护与生产修复已完成；不能将这段历史结论解释为
`7575773` 或未来 HEAD 的当前状态。

## 本地复验

以下命令须在完整 Git checkout 中执行；源码 ZIP 与 sdist 不包含历史测试目录。

```powershell
.\.venv\Scripts\python.exe scripts\verify_ci_contract.py
.\.venv\Scripts\python.exe scripts\verify_commit_scope.py --staged
.\.venv\Scripts\python.exe -m pytest -q tests\test_ci_contract.py
```

托管运行证明的是对应提交在指定 hosted runner 上实际到达的步骤。它不替代维护者审阅 action 上游源码、runner image 漂移、T15 目标环境验收，也不证明真实模型或平台能力。
