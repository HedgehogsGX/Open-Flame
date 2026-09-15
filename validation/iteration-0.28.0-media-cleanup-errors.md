# 媒体失败清理保留主异常

日期：2026-09-12；架构重置分支上的独立基础模块修复。

原先 Editing 的 hash/identity/size/I/O 失败分支和 verified response 构造失败分支直接 close，
清理抛错会覆盖 `asset_changed`、原始 I/O cause 或 manifest 错误。现在基础受管文件模块提供
`close_binary_on_error`，只在异常退出时清理，且保留主异常；正常退出仍向调用方转移打开的
handle。匹配打开、Editing 验证和 response 构造共用这一规则，删除各自不一致的清理分支。

验证使用当前分支源码与独立临时对象：

- 7 个原始错误场景通过：hash、identity、size、read I/O、主中断，以及 response manifest
  配合普通 close 异常和 close 中断；原本通用主中断分支已正确，本轮保持该行为。
- 正常退出后 handle 仍打开，所有权转移检查通过。
- `python -m pytest tests/test_editing_service.py tests/test_editing_media.py -q`：
  **44 passed、2 skipped，6.57 秒**。两项跳过是本独立 worktree 未安装固定 FFmpeg 工具，
  不声称实际应用根缺少 runtime，也未进行媒体 render smoke。
- 修改文件 compileall、`git diff --check` 通过；没有新增或修改 tracked tests。

临时脚本和结果在 ignored `validation/local/architecture-reset-20260912/`。同脚本中的
Workflow owner 检查属于单独的线程修复记录。保留 same-handle、final fstat/lstat、Range、
有界读取和每个领域的错误映射；本切片不表示完整 CI 已修复，没有真实平台或模型动作。
