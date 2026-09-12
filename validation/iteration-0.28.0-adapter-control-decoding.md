# 适配器控制记录的共享传输解码

日期：2026-09-12；独立分支 `codex/architecture-reset-ci`。

原件与 thumbnail mapping 原先各自实现行数、空行、UTF-8、JSONL 和精确字段集合检查。
现在两种 parser 复用 `_mapping_lines` 与 `_mapping_record`；记录的标量、路径、所属 original、
唯一性和 proof 含义仍由各自 parser 验证。错误码和有界固定错误文案保持。

刻意保留两阶段顺序：先检查 framing，再验证输出目录，再逐记录解码和业务校验。因此损坏记录
与不存在目录同时出现时，错误优先级也保持。原件空 mapping 失败；thumbnail 空 mapping 在
默认生产 CLI 的 strict 策略下失败，在显式注入旧 runner 的兼容路径返回无 proof；显式 null/null
仍表示没有 thumbnail。没有更改公共 DTO、持久字段、Schema 或 yt-dlp 命令参数。

验证：

- 从父提交读取旧 parser，对比当前生产 parser 的 **122 组**组合：有效记录、LF/CRLF、空文件、
  空行、无效 UTF-8/JSON、字段缺失/额外、重复、超量、非法 ID/路径、显式无 thumbnail、半份
  identity、缺失输出目录，以及 strict/compatibility。返回值、异常类型、错误码与 cause 均一致。
- 现有 `test_adapter_contract.py`、`test_worker_auxiliary_artifacts.py`、
  `test_candidate_worker_cli.py`、`test_local_worker_cli.py`：**65 passed，10.30 秒**。
- compileall 与 `git diff --check` 通过；没有修改 tracked tests。

差分脚本与原始结果只放 ignored `validation/local/architecture-reset-20260912/`。
这是共享适配器传输规则的维护收敛，不是新的真实平台或最高质量封面验收；完整 CI 仍待恢复。
