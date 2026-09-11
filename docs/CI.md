# 无凭据持续集成

`.github/workflows/ci.yml` 在 push、pull request 和手动触发时运行 Windows 与 Linux、CPython 3.12.10 与 3.13.14 的四格矩阵。3.12.10 是当前 `actions/setup-python` 清单中可在 Windows x64 取得的固定 3.12 patch；原先的 3.12.13 没有对应 Windows x64 artifact。每格安装精确 `uv 0.11.25`，以矩阵版本设置 `UV_PYTHON`，并用 `uv sync --extra dev --locked --python ...` 建立开发环境。`UV_NO_SYNC=1` 阻止后续 `uv run` 静默改写该环境；CI 在测试前断言实际 Python patch 与 pytest 身份。Node 目前只在 `Verify runtime identities` 输出版本，不代表另有一条独立 Node UI harness。

工作流权限只有 `contents: read`，checkout 不保留 Git 凭据，也不读取 GitHub secrets、平台 Cookie 或上传账号。依赖安装本身会访问受 GitHub Actions 和包索引控制的外部服务；测试阶段依赖现有 synthetic/fake backend 与本地 HTTP fixture，不授权真实下载、扫码、登录或上传。普通 hosted Linux job 也不具备 T15 所需的可信 effective-root、getfacl、Unix socket、network namespace、容器 daemon 与冻结 image identity 证据，因此相应 skip 仍是未验收。

三个外部 action 与 uv 版本都固定在工作流中；`scripts/verify_ci_contract.py` 检查精确 action commit、矩阵、只读权限、无 credential trigger、locked install、完整测试和 whitespace gate，并把换行规范化后的完整 workflow 字节绑定到已审 SHA-256。完整字节绑定会拒绝 flow mapping、YAML 续行、anchor/alias 等未逐项建模的改写，避免文本规则与 YAML 解释结果分歧。工作流以 `fetch-depth: 0` 获取完整历史。曾经在安装依赖前运行的 `scripts/verify_commit_scope.py --github-event` 提交范围门禁已移除：它禁止一切 `tests/` 改动，使测试无法跟随产品，是 CI 长期变红的直接原因。测试现在按普通源码维护，约束改为「只能断言实际跑过的行为」，由评审把关，见 [AGENTS.md](../AGENTS.md)。最后两步分别检查工作树 whitespace 和当前提交相对父提交的 whitespace。

`tests/` 中已有回归保留给本地与 CI 运行，但不进入发行包。后续功能提交不得新增或修改自动化测试文件；本地 hook 与 hosted commit-scope gate 都会拒绝这类变更，专门清理提交仍可删除历史测试。临时 smoke 与诊断脚本写入已忽略的 `validation/local/`。

## 当前托管运行状态

[GitHub Actions run 34393235622](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622) 精确绑定提交 [`bdd88ce184b2f86f957f7df9129baa21863227dd`](https://github.com/HedgehogsGX/Open-Flame/commit/bdd88ce184b2f86f957f7df9129baa21863227dd)。2026-09-10 的最终公开状态显示，四格均已通过 checkout、CPython 选择、commit scope、固定 uv、CI definition、runtime identities、locked development environment、`uv pip check` 及矩阵 Python/pytest 身份检查，随后四格都在 `Run the offline test suite` 失败；run 总结论为 `failure`。逐格链接与最终状态记录在[托管 CI 恢复证据](../validation/iteration-0.28.0-hosted-ci-recovery.md)。

同一源码的本机全量结果是 **173 failed, 2277 passed, 8 skipped in 327.39s**。失败主要暴露既有测试与当前产品合同的漂移：大量下载 API 测试仍使用 `Host: testserver` 且未先获取下载域 session/提交 `X-Download-CSRF`，另有 Editing Schema 1、旧 release metadata/version、旧 UI 导航和 validation identity 断言。仓库策略禁止修改这些测试，本轮也没有在产品中加入测试绕过。因此当前结论是“托管执行链与前置门禁已恢复，完整测试仍红”，不能称为 CI 绿色。失败后的 whitespace steps 也没有在这些 job 中运行。

required checks 与 branch protection 仍为 **NOT CONFIGURED**。在测试合同与当前产品行为达成明确处置、四格完整通过并保存实际 runner 身份前，不应把这些 checks 设为 required。

## 本地复验

以下命令须在完整 Git checkout 中执行；源码 ZIP 与 sdist 不包含历史测试目录。

```powershell
.\.venv\Scripts\python.exe scripts\verify_ci_contract.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_ci_contract.py
```

托管运行证明的是对应提交在指定 hosted runner 上实际到达的步骤。它不替代维护者审阅 action 上游源码、runner image 漂移、T15 目标环境验收，也不证明真实模型或平台能力。
