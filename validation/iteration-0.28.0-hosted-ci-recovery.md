# Iteration 0.28.0 托管 CI 执行链恢复证据

日期：2026-09-10

证据提交：[`bdd88ce184b2f86f957f7df9129baa21863227dd`](https://github.com/HedgehogsGX/Open-Flame/commit/bdd88ce184b2f86f957f7df9129baa21863227dd)

托管运行：[`34393235622`](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622)

## 修复范围

该提交修复托管工作流无法稳定进入完整测试的执行基础：CPython 3.12 矩阵从 `3.12.13` 改为具有 Windows x64 artifact 的 `3.12.10`，同时保留 `3.13.14`；checkout 改为完整历史；commit-scope 检查从 GitHub push、pull request 或手动触发事件推导 base/head 并以 merge-base 检查完整提交范围；locked dev environment 显式绑定矩阵 Python，后续 `uv run` 禁止隐式同步，并在测试前核对实际 Python patch 和 pytest 身份。

提交范围检查读取 NUL 分隔的 Git name-status，重命名会同时检查旧、新路径，删除历史测试仍允许，新增或修改自动化测试失败关闭。它在安装第三方依赖前运行，因此不合规提交不会先消耗依赖安装步骤。没有削减完整 pytest、把失败用例排除出 CI、修改已跟踪测试或加入产品测试绕过。

## GitHub Actions 时间点结果

以下是 2026-09-10 通过公开 Actions API 观察到的最终状态。证据始终绑定上面的 `bdd88ce`，后续纯文档提交触发的运行不写回本记录形成自引用。

| 矩阵格 | 链接 | 前置环境与门禁 | `Run the offline test suite` | 时间点结论 |
| --- | --- | --- | --- | --- |
| Ubuntu / CPython 3.12.10 | [job 102606496757](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622/job/102606496757) | **PASS** | **FAIL** | `failure` |
| Ubuntu / CPython 3.13.14 | [job 102606496582](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622/job/102606496582) | **PASS** | **FAIL** | `failure` |
| Windows / CPython 3.12.10 | [job 102606496744](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622/job/102606496744) | **PASS** | **FAIL** | `failure` |
| Windows / CPython 3.13.14 | [job 102606496760](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34393235622/job/102606496760) | **PASS** | **FAIL** | `failure` |

四格均已通过：checkout、`Select CPython`、`Verify commit scope`、固定 uv 安装、`Verify CI definition`、runtime identities、locked development environment、`uv pip check` 和矩阵 Python/pytest 身份检查。四个 job 的失败步骤都精确为 `Run the offline test suite`，run 总结论为 `failure`；因为该 step 失败，位于其后的两个 whitespace 检查没有运行。这个结果证明托管执行链和前置门禁能够运行，不证明完整 CI 绿色。

## 本地完整测试对照

同一源码状态的本机完整命令结果：

```text
173 failed, 2277 passed, 8 skipped in 327.39s
```

失败输出主要集中在当前产品合同与冻结历史测试之间的漂移：

- 下载 API 已要求唯一 loopback Host、下载域 session 和写请求 `X-Download-CSRF`；大量既有 TestClient 仍使用 `Host: testserver` 且不取得 session，因此在业务路由前得到预期的 `403 download_request_forbidden`。
- 仍有 Editing Schema 1、旧 release metadata/version、旧 UI 导航及 validation identity 断言。

仓库策略禁止新增或修改已跟踪测试，因此该次修复没有通过修改 `tests/` 消除红灯，也没有把 TestClient 特例写入生产边界。当前必须保留 **CI FAIL** 结论；后续需要在不削弱产品安全合同、且不违反测试文件策略的前提下明确处理冻结测试与现状的关系。

## 状态与证据边界

required checks 与 branch protection 仍为 **NOT CONFIGURED**。在四格完整通过前，不应把当前失败 check 设为 required，也不能把“工作流已执行”表述成“发布门禁已通过”。

托管 runner 的依赖准备会访问 GitHub Actions 和包索引；本记录没有执行或授权真实网址下载、OpenAI 调用、账号登录、扫码、三平台上传、定时或发布。普通 hosted Ubuntu 也不能替代 T15 的目标 Linux/Docker/NAS、权限、socket、network namespace 或容器证据。本记录不是新的 release receipt，只证明 `bdd88ce` 的托管 CI 执行路径与已观察失败边界。
