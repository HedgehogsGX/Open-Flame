# Upload 源文件到实际消费者的交接

日期：2026-09-12；基线 `abcbc40`；分支 `codex/architecture-reset-ci`。

此前 UploadService 校验主视频后关闭 reader，只把路径交给 backend；在后端打开前的
本地并发写入可以改变消费字节，而 receipt 仍绑定原摘要。现在校验 reader 的寿命延伸到
backend 返回或抛错。Windows 使用共享读取、禁止写入/删除打开的句柄，其他读者仍可打开媒体。

`managed_files.open_windows_shared_read_binary` 负责 HANDLE → FD → reader 交接，转换、
包装、身份检查或返回对象构造失败时回收资源；领域保留文件路径政策、大小限制及错误映射。
`UploadRequest.source_sha256` 是由 Service 提供的必需冻结输入，没有改变 HTTP DTO 或 Schema。

Biliup 的 operation 媒体可能是硬链接，也可能是复制回退；复制产生的新文件不受源句柄保护。
因此 Backend 对实际暂存文件再持有、散列并比对冻结摘要，句柄在消费及 checkpoint 清理后关闭，
随后删除 operation 目录。这两次摘要分别约束登记源与实际暂存字节，不能相互替代。

## 执行结果与清理

独立审查复现：新增暂存 close 若在执行器返回 submitted/unknown 后抛 OSError，原外层 catch
会把它转换为 failed/backend_failed。Service 随后写 responded receipt，允许新建 retry draft，
绕过 unknown 人工核对。直接执行器抛 OSError 的同类外层分类问题在旧 HEAD 已存在。

现在 Backend 明确记录是否已进入执行，并在退出资源范围前完成结果类型归一化：

- 执行前准备失败仍返回 failed；尚未调用执行器。
- 清理失败保留已经取得的结果，包括 submitted、unknown 或执行器明确返回的失败。
- 已进入执行、但没有取得返回对象的异常保持 unknown，不推断为安全失败。
- 类型与投稿模式不匹配的成功状态先变成 unknown，再退出资源范围。
- 账号检查/登录继续使用原有失败语义；Upload Service 的源 reader 关闭错误也保持 unknown。

没有改变 receipt 的事务、revision、冻结身份、人工核对、显式确认与后继建立合同。

## 当前验证

- 实际 Service → SauBackend → SQLite receipt → retry：6 场景。修改前 3 项错误开放 retry；
  修改后全部符合合同。正常、准备失败、submitted/unknown 后 close、源 close、执行器异常
  均覆盖；未知结果要求 verify_remote_result_first，已提交结果拒绝 retry。
- control/write/replace/delete 四场景：独立本地子进程只能读取，消费 SHA 与 source/receipt 相同。
- Biliup hardlink/copy 两路径：暂存改写被拒绝，operation 在源 reader 关闭前已清理。
- Native 7 项：转换/包装/身份/构造失败保留主异常并释放；已有 writer 拒绝；Unicode 长路径
  与父目录 rename 反馈符合本机预期。源异常 4 项、执行异常/取消/stop 3 项通过。
- Backend 边界 17 项：copy 同长篡改/增长/错摘要在执行前拒绝；三平台的 unknown、错误类型、
  明确失败在清理时保留合同；账号检查/登录及非法请求摘要均保持预期。
- 现有上传服务、生命周期、韧性、参数、backend 聚焦回归：133 passed、
  52 failed；相较审查冻结工作区没有新增失败 ID。
- ignored 副本仅补两处生成媒体的实际 SHA：93 passed、1 failed。
  所有 assert AST 不变，tracked tests 未改；剩余一项仍是旧的缺少 evidence 结果期待。

修复前的最新完整诊断为 2144 passed、276 failed、16 skipped、1 collection error（365.55 秒）；
它对应 abcbc40 加当时四文件工作区，不能当作本修复后的完整测试结果。新增 26 个 failure IDs
来自 required source_sha256 的旧 fixture/payload 未接线；另有 250 个既有失败仍须逐类关闭。
标准 pytest 仍被备份旧私有 import 阻断，22 个用例未收集。abcbc40 hosted run 34692830186
四格都在 offline pytest 失败，尚不包含本切片。完整 CI 与发行安装仍未完成。

原始源交接、copy 回退、结果错误的 red/green 反馈在 ignored
`validation/local/architecture-reset-20260912/source_handoff/`；`review-evidence/` 保存修复前
独立审查和完整测试摘要。探针仅使用生成文件、临时 SQLite、真实本地锁/线程与假执行器。
没有真实下载、OpenAI、登录、上传、发布或应用根迁移。

Windows 普通文件共享语义参考 [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
和 [open_osfhandle](https://docs.python.org/3/library/msvcrt.html#msvcrt.open_osfhandle)。非 Windows
继续原有匹配读取，自有实际上传 backend 仍限既有 Windows 范围。当前反馈不证明任意指令中断、
所有目录竞态、全部文件系统或真实平台接受；本地 receipt 也不是平台签名回执。

下一步收敛跨页纯规则和 CLI，完成文档、完整 CI 维护与同一最终构建的发行验证。
