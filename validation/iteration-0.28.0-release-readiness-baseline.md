# 0.28.0 发布准备基线

核对时间：2026-09-13 16:08:07 UTC（2026-09-14，Australia/Adelaide）。
本记录绑定开始本轮工作前的 `7575773116f7aeebe6bc21bbdb02f9f469a3ea5e`；
后续文档或功能提交必须取得自己的 CI 和制品证据，不能借用此基线。

## 源码与远端

- 实现分支：`codex/architecture-reset-ci`；核对时工作树干净，本地与远端 HEAD 一致。
- main：`d48132637ce94a8b0b41bc2a965d24e27ac62aff`。
- [PR #2](https://github.com/HedgehogsGX/Open-Flame/pull/2)：open、非 draft、未合并，
  head 为上述候选，base 为上述 main；GitHub 返回 `mergeable=true`。
- GitHub author/committer 均为 `novahanser`，显示名 `Cyaegha_Xu`；
  commit 签名 `verified=true / reason=valid`，本地 SSH 验签也通过。
- `.githooks` 已启用；提交签名设置为 true，格式 ssh。
- 开发环境实际输出：Windows AMD64，CPython 3.13.14，uv 0.11.25，Node v24.16.0；
  项目导入来自实现 worktree 的 `src/video_download_control`。

## 同提交工程证据

| 范围 | 精确运行 | 本次读回 |
| --- | --- | --- |
| push CI | [34763783506](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34763783506) | completed / success，四个 job 均 success |
| pull_request CI | [34765157437](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34765157437) | completed / success，四个 job 均 success |
| source/wheel 独立安装 | 包外 `installation-receipt-7575773.json` | source_commit 与候选一致；两种安装报告 passed |

上述 receipt 的 SHA-256 为
`08846b21e25d925a5a3f65e0cdbf51f1969ba919eec5cfb860bff2b2592a4f36`，产品身份为
`0.28.0+build.sha256.6042f081df25eda59c821e6ae8359702ee7dd48a996ccc6e4a112f31c0ca9d5f`。
receipt 和原始安装环境保存在 Git 外；它们不是正式发布动作，也不覆盖后续提交。
本轮另核对五件既存制品各自大小及 SHA-256，全部与 receipt 一致。
本次阅读和摘要核对不是重新安装。安装记录包含 14 个 CLI、首次/重复 Setup、Start 检查和
离线上传门禁；普通 Start/Ctrl+C、四页浏览器及新数据演练继续单独取证。

## 合并门禁准备

main 分支 API 返回 `protected=false`；protection API 为 HTTP 404，应用规则为 `[]`。
这三个结果共同表明此次查询没有观察到生效保护，不能仅凭某个 404 推断配置状态。
required checks 与分支保护尚未设置，本轮没有合并或直接推送 main。

从同 HEAD 的 check-runs 读到以下精确 context，来源均为 GitHub Actions
（app ID `15368`，slug `github-actions`）：

- `windows-latest / CPython 3.12.10`
- `windows-latest / CPython 3.13.14`
- `ubuntu-latest / CPython 3.12.10`
- `ubuntu-latest / CPython 3.13.14`

待实施规则：通过 PR 合并、要求四项检查及签名、禁止 force push 和分支删除。
建议要求与最新 base 同步，避免只验收旧 base；管理员也受检查约束，避免绕过。
审阅人数依真实维护者配置，不凭空要求无人能够满足的审阅。
配置后必须读回并在隔离分支验证失败/未完成检查阻断；规则提案不算完成门禁验收。

## 下一阶段与证据范围

1. 在固定源码和新的 canonical app root 完成隔离启动、四页浏览器、正常停止。
2. 确认旧数据根及全部写入方，先取得一致保护副本；当前上传 backup create 仅接受 Schema 4。
   在已存在的明确副本调用 `ensure_upload_schema`，比较业务记录、媒体摘要和幂等结果；
   再在另一新根演练 create/restore，不通过 service/manager 构造进行只读验证。
3. Upload 迁移/恢复与实际根升级分别报告；Download、Editing、Workflow 和预设的
   保存/恢复另列，不能以上传备份代表全应用备份。
4. 以已有外部测试手册覆盖无 AI 基础链路、真实 AI、三平台与多输出；外部账号、素材、
   远端动作和预算按具体测试确定，`unknown` 保持 receipt 与人工核对门禁。
5. 根据可复现缺陷迭代；无阻断缺陷时再推进可配置空间阈值与容量预算。

本轮基线核对没有真实媒体下载、AI、登录、上传或发布，也未启动或迁移实际用户应用根。
Linux/Docker/NAS 与公网多人服务保持独立范围。当前文档修订不代表新增产品能力或完成发行。

## 本次文档验证

- `scripts/release.py check-source`：PASS，343 个源码清单文件，包含本记录；
  执行现有 UTF-8、隐私、许可、元数据和发行文档链接清单检查。
- `scripts/verify_ci_contract.py`：PASS；未更改 workflow、运行矩阵或测试范围。
- `pytest -q tests/test_ci_contract.py`：17 passed。
- receipt、五件既存制品与两种安装报告的本地核对：PASS。
- 提交前继续执行 staged scope 与 whitespace 门禁；后续提交 CI 尚不在本记录的证明范围。
