# 无凭据持续集成

`.github/workflows/ci.yml` 在 push、pull request 和手动触发时分别运行 Windows 与 Linux、CPython 3.12.13 与 3.13.14 的四格矩阵。每格安装精确 `uv 0.11.25`，执行 locked development environment、`uv pip check`、完整离线 pytest、Node UI harness 和 whitespace 检查。工作流运行 `pytest -q`，并继承 `pyproject.toml` 的 `addopts = "-ra"` 保留各平台 skip 原因；Windows 与 Linux 结果不能互相替代。

工作流权限只有 `contents: read`，checkout 不保留 Git 凭据，也不读取 GitHub secrets、平台 Cookie 或上传账号。依赖安装本身会访问受 GitHub Actions 和包索引控制的外部服务；测试阶段依赖现有 synthetic/fake backend 与本地 HTTP fixture，不授权真实下载、扫码、登录或上传。普通 hosted Linux job 也不具备 T15 所需的可信 effective-root、getfacl、Unix socket、network namespace、容器 daemon 与冻结 image identity 证据，因此相应 skip 仍是未验收。

三个外部 action 与 uv 版本都固定在工作流中；`scripts/verify_ci_contract.py` 检查精确 action commit、矩阵、只读权限、无 credential trigger、locked install、完整测试和 whitespace gate，并把换行规范化后的完整 workflow 字节绑定到已审 SHA-256。完整字节绑定会拒绝 flow mapping、YAML 续行、anchor/alias 等未逐项建模的改写，避免文本规则与 YAML 解释结果分歧。工作树用 `git diff --check` 检查测试期间产生的变化；固定拉取两层历史后，再用 `git diff-tree --check --root -r -m --no-commit-id HEAD` 检查当前提交，包括 pull request merge commit 相对各父提交的差异，避免 clean checkout 上的空 diff 被误当成源码检查。`tests/test_ci_contract.py` 会在内存中分别破坏 action pin、权限、secret、trigger、提交检查和失败传播，证明 validator 会拒绝这些弱化。该检查不能代替维护者审阅 action 上游源码或 runner image 漂移。

`tests/` 中已有回归保留给本地与 CI 运行，但不进入发行包。后续功能提交不得新增或修改自动化测试文件；提交 hook 会拒绝这类 staged 变更，清理提交仍可删除历史测试。临时 smoke 与诊断脚本写入已忽略的 `validation/local/`。

本地复验：

以下命令须在完整 Git checkout 中执行；源码 ZIP 与 sdist 不包含历史测试目录。

```powershell
.\.venv\Scripts\python.exe scripts\verify_ci_contract.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_ci_contract.py
```

首次把工作流推到 GitHub 后，需保存四格运行链接和实际 runner/Python/Node/uv 身份。只有这些运行全部通过后，才能在仓库设置中把对应 checks 设为 required；配置文件存在、一次本机通过或未触发的工作流都不能声称托管门禁已经启用。
