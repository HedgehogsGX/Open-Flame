# 0.28.0 旧版环境回退与私有状态保护演练

日期：2026-09-14（Australia/Adelaide）。检查源码为
`ceb77ece167e4ab4e6f18a720e8ebe8cc0804d2e`；隔离运行的旧源码为
`97929f67d200fa765d3f43d084f9774121645828`。这是同一 Windows 用户和宿主上的限定恢复证据，
实际应用根仍未启动或升级。本记录接续[业务数据恢复](iteration-0.28.0-application-data-recovery.md)。

## 环境保护与独立副本

先核对实际路径、plain-file 身份和大小/摘要，再保护及还原六组文件。两轮复制均逐文件重读
核对；包含合法 bytecode cache，因此数量不同于 runtime manifest 的受管载荷数量。

| 对象 | 文件数 | 字节数 | 保护及独立还原 |
| --- | ---: | ---: | --- |
| 主 CPython 3.13.14 x64 | 9,952 | 202,811,982 | PASS |
| Upload 外部 CPython 3.12.13 x64 | 3,542 | 66,538,662 | PASS |
| Upload runtime | 3,498 | 984,174,539 | PASS |
| AI runtime | 38 | 21,420,767 | PASS |
| 固定工具载荷 | 17 | 143,178,771 | PASS |
| 已取得一致性的业务数据库和媒体 | 4 | 542,302 | PASS |
| 合计 | 17,051 | 1,418,667,023 | PASS |

复制共 695.25 秒；该离线步骤的网络尝试、原目录写入尝试均为 0，原数据库摘要保持。
两个独立 Python 都通过版本、64 位、SSL、SQLite 和 base_prefix 检查。

旧源码的 requirements/toolchain 锁与此前已验收候选一致。从 PyPI 按哈希下载 14 个精确
wheel 后，使用恢复出的主 Python 执行旧 Setup：依赖来自本地 wheelhouse，工具载荷已还原，
artifact cache 为空，Setup 返回 ready / exit 0。新建旧版 venv 的 base_prefix 指向恢复出的
主 Python。准备 wheel 的步骤访问了 PyPI，不能把整个演练称为无网络执行。

后续又从精确 `ceb77ec` 建立独立 clean detached 候选，核对相同锁和 17 个工具文件后，
使用上述恢复 Python 与本地 wheel 完成 Setup；独立新根的 Start --check 返回 checked。
这只证明该候选的隔离安装和启动检查。首次 Setup 多传仅适用于可选 runtime 的 app-root
参数，在安装前被拒绝；只修正 ignored 调用脚本，保留首次日志，没有修改产品入口。

**Upload 的绝对路径绑定仍保留。** 其 venv/runtime manifest 仍绑定原外部 Python；独立
Python 副本虽已逐文件核对且能单独运行，本轮没有让 Upload venv 改用该副本，也未改写 manifest。
因此证明的是原路径依赖仍在时的同机回退组合；不证明可直接搬移的 Upload 环境。若原解释器
丢失，须先按受控映射恢复相同路径或用正式 builder 重建，再做执行验证。

## 旧数据与普通启动

旧源码的公开 validate/ensure 均接受 Schema 3，均拒绝 Schema 4；调用前后对应数据库摘要
不变。Schema 4 负向样本只由当前 ensure 在另一副本生成。旧源码不能单独降级新数据库。

在没有账号秘密的恢复 app root，用旧版普通 Start 启动并仅发出 GET：

- 四页 HTTP 均为 200；2 个来源、2 个 canceled job、2 个历史 ready operation 的身份和状态
  与保护副本一致，2 份媒体摘要一致。
- Upload runtime 完整性通过，调度线程启动；存储账号仍为 ready，但实验根没有凭据文件，
  没有执行账号 check、登录或上传，不能据此判断平台会话有效。
- AI 完整性 verified，provider 状态仍为 provider_health_required；没有 health 或模型调用。
- Ctrl+C 后 3 个自有进程退出、监听端口释放，应用停止原因为 normal；PTY 返回 1，未强制终止。
- 最终四库 quick_check=ok、外键错误 0，Schema 为 **11/4/3/2**。原根没有 Editing/Workflow
  内容，这两个库由旧程序新建为空，不能记为历史内容恢复。活动 operation、下载 job/attempt
  和 incoming 均为空，原数据库摘要与原目录缺席状态保持。

旧版预设读取产生 1 条 runtime_log.event_rejected / invalid_record，日志检查单列为
KNOWN_OLD_VERSION_FAILURE；数据恢复、完整性和正常启停分别 PASS。
[ceb77ec 的日志修复](iteration-0.28.0-runtime-route-logging.md)没有回填到旧源码。
首次最终 validator 错把旧 Workflow 版本写为 3，在比较处停止；核对旧常量为 2 后只修正
ignored validator 并重验，原失败记录保留。未执行浏览器渲染 QA。

## 私有状态加密保护

私有状态另存于 Git 和实验 app root 之外的受控本地目录。先关闭 ACL 继承、限定当前用户
与 SYSTEM 并读回，再在 Upload activity exclusive lease 下读取。内容和相对名称仅存在于
内存及加密 envelope，不写入明文备份、公开日志或实验应用。

实际保护 1 个文件、5 个目录、2,182 bytes，采用 Windows DPAPI 当前用户范围。
磁盘密文读回解密及逐项核对通过，损坏密文被拒绝；另一独立进程再次解密和校验通过。
原私有文件内容保持，没有平台或其他网络调用。前两次 ACL 子进程初始化失败发生在读取
私有内容前，空目录和错误记录保留；限定子进程使用 Windows inbox PowerShell 模块后完成。

本次只证明同一 Windows 身份/宿主上的加密字节可恢复，没有把真实秘密恢复到文件系统、
验证平台认证或验证异机/offsite 恢复。DPAPI 默认与登录身份相关，通常还依赖加密时的机器；
范围依据见 [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。

## 后续边界

原始 manifest、路径映射、脚本、wheel、环境、数据库和报告保留在 ignored 本地验收区；
加密私有备份在 Git 外。真实应用根升级、完整凭据落盘恢复、脱离原解释器的 Upload 执行、
异机/offsite、真实模型/平台、最终发行仍分别为 NOT RUN。
实际根维护前须重新停写、取得最新保护副本、核对任务状态，并绑定明确候选；不能直接复用
较早快照覆盖期间可能新增的业务数据。
