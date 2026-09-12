# CLI 共用路径解析与 Worker 输出

日期：2026-09-12；基线 798b24d；开发分支 codex/architecture-reset-ci。

LocalApp 与 LocalWorker 的两个规范化绝对路径 parser 已移到既有
worker_cli_support.absolute_path。原入口保留参数、错误映射及配置组装；两份旧函数体
与共享函数 AST 等价。Candidate 的路径接受范围不同，保留其自己的 parser。

离线 Worker CLI 复用既有 print_worker_result，删除 idle 和 result 两块共 12 毛行
JSON 输出。调用仍位于 run_once 之后、idle/drain/单轮退出判断之前；stop reason、
异常、计时、日志和清理顺序保持。没有新 CLI、运行服务、依赖或解析框架。
四份生产文件增加 12 行、删除 30 行，净减 18 行；不是删除 30 行净代码。

现有 worker、LocalApp、LocalWorker、startup diagnostics 与 Candidate CLI 回归：
56 passed、13 failed（pytest 8.28 秒）。13 个失败 ID 都是此前 LocalWorker 内部组装
迁移后的既有失败，没有新增 ID；不能称为完整验证通过。tracked tests 未改。
临时 AST 对照、JUnit 与日志在 ignored
validation/local/architecture-reset-20260912/cli_shared/。

源码编译、CI 定义、发行源码预检、diff 与提交范围门禁仍须随提交核对。
没有真实网络、登录或平台执行；全量 CI 与最终发行验证继续保留为完整目标。
