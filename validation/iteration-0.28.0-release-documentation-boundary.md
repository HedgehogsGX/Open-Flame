# 新发行文档完整性与通用制品合同分责

日期：2026-09-12；分支 `codex/architecture-reset-ci`；起点 `1baef72`。

已有未提交的文档 gate 接入 `source_contract`，使最小制品 fixture 必须递归带入历史
文档，11 项既有发行回归因而失败。实际发行清单同时漏掉 24 份已被引用的现有文档。

现在 `validate_documentation_inventory` 只在新发行准备边界执行：`build` 在创建输出目录
和启动构建工具前检查冻结清单；新增只读 `scripts/release.py check-source` 使用同一检查。
通用 archive/identity 验证继续按制品自身的清单、摘要、安全与格式合同工作，不依赖当前
checkout 的文档历史。没有放宽既有隐私、归档路径、包内容、签名/身份或依赖合同。

24 份缺失文档已经逐项加入明确发行清单，包括当前架构、通用交接提示词与相应验收记录。
通用提示词同时改为按当前用户目标选择审查/实施范围，专题整体重整目标仍留在 HANDOFF。

验证：

- 同一只读 `check-source` 在清单修复前以 `unlisted_documentation_link` 拒绝，修复后通过。
  本记录加入后共 321 个发行文件，没有缺失的受检文档目标；tests 和 ignored evidence 未列入。
- `tests/test_release.py` 原样运行：100 passed，12.99 秒。先前新 gate 引起的 11 项失败已消除。
- 最小历史 fixture 的通用合同通过；使用同一 fixture 准备新包则在输出创建、构建工具调用前
  拒绝缺文档输入。检查只使用临时目录和 mock 构建调用，没有联网或启动构建工具。
- inline 本地链接、片段、编码空格、相对路径、外部 URL、fenced 示例和 ignored evidence
  的有界匹配检查通过；缺失目标与 CLI 稳定错误码检查通过。
- whitespace、staged scope 和 Python 语法检查通过，没有修改 tracked tests。

匹配范围仅为 docs/validation 下有扩展名的字面 inline Markdown 本地文件链接；不是完整
Markdown、锚点或全站链接验证。现有专门排除的历史报告仍排除。新增的是维护脚本子命令，
不是新的安装后 CLI，pyproject 声明仍为 14 项。

脚本、前后预检、边界 JSON、pytest 日志/JUnit 留在 ignored
`validation/local/architecture-reset-20260912/`。没有构建新的正式发行包，完整 CI 仍未通过。
