# Download 素材读取与 HTTP 组装分责

日期：2026-09-12；分支 `codex/architecture-reset-ci`；切片起点 `38a77d1`。

`download_assets.py` 现在拥有 Download 登记文件解析、原件稳定 snapshot、辅助文件有界
读取，以及供 Editing/Upload/Workflow 使用的原件、thumbnail、caption 和唯一封面候选。
`DownloadAssetReader` 依赖 Download 的公开登记查询；新模块不加载 HTTP 或其他三个领域。

API 中原四个跨域 resolver 只保留调用、HTTP 状态和 Workflow 错误映射。字幕消费上限由
API composition 从 Editing 合同传入，读取模块不反向依赖 Editing。HTTP 继续拥有响应、
Range、发送/断连生命周期；Download 的 cooperative cancellation 只在有界文件操作间检查。
原件/thumbnail 的 path+digest 引用仍须在消费者导入时重新验证；caption 返回已校验 bytes，
唯一封面检查完整登记集合和文件内容后返回引用，消费者仍保留最终导入检查。

原件和辅助素材读取不再在 API 中有第二份实现。没有新增数据库、Schema、runtime、外部依赖
或通用资源框架；API 从 1,932 行收窄到 1,396 行，收益是读取职责可以脱离 HTTP 使用。

## 本切片验证

- 原四个 resolver 与当前 reader + 实际 API 错误映射作 45 项差分，0 项差异。
  比较返回内容、错误和登记查询次序；覆盖有效/缺失原件、非视频拒绝、大小/路径错误、
  thumbnail 类型/语言/摘要格式、caption 内容和上限、可信零候选、损坏 owner/父关系、
  多候选与“损坏先于歧义”，以及原件 snapshot 正常、摘要漂移和提前取消。
- 四个正常入口、可信零候选和 snapshot 正常样本均实际成功，不以两个入口同样报错自证。
- 六项既有响应回归原样运行得到 4 passed、2 failed；失败都在访问已迁走的
  `api.tempfile`，尚未执行取消/发送行为。只在 ignored 副本把四处工厂定位改为
  `download_assets.tempfile`、保持全部断言后，六项通过（2.14 秒）。这不是 tracked
  tests 或完整 CI 通过，正式测试迁移仍受当前 AGENTS 政策约束。
- 辅助错误清理迁移到新位置后，原四条故障及正常所有权对照仍全部通过。
- 完整 `create_app`、loopback Host、CSRF 与 fake Download Worker 的真实 HTTP 栈：
  正常原件 200；同尺寸内容漂移 409；两个子进程均在有界时间内退出。
- 新模块 fresh import 没有加载 FastAPI/Starlette 或 Editing/Upload/Workflow。
- 语法编译、whitespace 与显式发行源清单检查通过；新增模块已列入清单。

冻结前实现、差分脚本、临时迁移副本、日志与 JSON/JUnit 保存在 ignored
`validation/local/architecture-reset-20260912/`。没有修改 tracked tests，没有访问真实应用
数据库、下载源、模型、登录或上传平台。完整架构、全部 CI 与发行验证仍未完成。
