# Worker 执行模块与 CLI 边界

日期：2026-09-12；分支 `codex/architecture-reset-ci`，尚未合并 main。

此前 LocalApp supervisor 从 `local_worker_cli.py` 导入配置、builder、私有锁与 logger，
导致可复用的应用执行路径反向依赖命令行入口。现在 `local_worker.py` 和 `candidate_worker.py`
分别拥有两种 Worker 的配置、preflight、组装和执行所有权；CLI 负责 argparse、错误输出、
check/drain/poll 命令行为。`worker_cli_support.py` 共用 JS runtime/poll 参数解析与结果序列化。

LocalApp 直接依赖执行模块的公开接口；该模块不导入 argparse 或 CLI。CLI 继续 re-export
配置类和 builder，已有命令名和参数保持。参数命名空间转换现在由 CLI 的 `config_from_args`
负责，`LocalWorkerConfig.from_args` 不再属于运行配置类的接口。原件路径和 Cookie 参数在两种
模式中有不同严格程度，未为去重而混同。

下载 claim gate、Windows direct opt-in、Linux namespace/relay 检查、独占 Worker 锁、固定
toolchain、Cookie 隔离、有界日志及默认 runner 的 thumbnail proof 要求保持。preflight 仍先于
数据库初始化/领取，Worker 仍在每次领取前再次检查网络门禁。

## 验证

- 执行模块 fresh import 和 AST 检查：不加载 CLI，无 argparse 依赖；源码中 supervisor 对 CLI
  的反向 import 已消除。
- 两个真实 Worker 命令的 `--help` 均成功。
- 既有 candidate CLI、LocalApp、并发 Worker 三文件：**122 passed，3.64 秒**。
- 旧 local CLI 文件保持未修改，因引用旧模块 monkeypatch 位置和 `Config.from_args`，当前为
  **13 failed、11 passed**。不能把这一结果隐去或宣称整个 CLI/CI 已绿色。
- 在 ignored 目录制作原测试的迁移草案，仅将 monkeypatch 指向当前实现所有者、参数转换改用
  `config_from_args`，不改断言和业务场景；复用原 `tests.conftest` 后 **24 passed，1.55 秒**。
  命令为 `python -m pytest -p tests.conftest validation/local/architecture-reset-20260912/test_local_worker_entry_migration.py -q`。
- compileall 与 source whitespace 通过。没有真实网址下载、登录、模型或上传调用。

tracked test 维护及提交门禁调整仍待用户对当前 AGENTS 测试禁改规则作明确授权。迁移草案
不能替代当前完整 CI；其位置和失败明细保存在 ignored `validation/local/architecture-reset-20260912/`。
新执行模块已加入显式源码发行清单，仍需最终打包与全量矩阵验证。
