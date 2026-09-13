# main 合并门禁：配置提案与验收

状态：**PROPOSED / NOT CONFIGURED**。2026-09-14 只读核对绑定
`ceb77ece167e4ab4e6f18a720e8ebe8cc0804d2e`；PR #2 未合并，main 仍为 `d481326`，
protected=false、适用规则为空，包含上级规则的 ruleset 清单也为空。
当前 API 身份未显示仓库 admin 权限；具备仓库管理权限的身份配置后，还须独立验证生效。

## 本次具体选择

| 设置 | 提案 | 原因与影响 |
| --- | --- | --- |
| 目标 | 仅 refs/heads/main | 不约束其他开发分支或已有 tag |
| 强制等级 | active；bypass_actors=[] | 不设置管理员或 app 绕过者 |
| PR | 必须经 PR；merge 方法 | 保留已有提交及签名；仓库当前允许 merge commit |
| 审阅 | 0 个强制批准；讨论须解决 | 没有已确认的独立审阅人员；0 不是已完成代码审查的声明 |
| 提交 | required_signatures | PR 当前 39 个提交经 API 核对均 verified；新增提交须重验 |
| 分支同步 | strict_required_status_checks_policy=true | main 更新后须以最新 base 重新测试 |
| 历史保护 | deletion、non_fast_forward | 阻止匹配分支删除及 force push |
| 检查来源 | GitHub Actions integration_id=15368 | 绑定本轮实际 check-runs 返回的 app，而非仅匹配名称 |

精确 required_status_checks 为：

```json
[
  {"context": "ubuntu-latest / CPython 3.12.10", "integration_id": 15368},
  {"context": "ubuntu-latest / CPython 3.13.14", "integration_id": 15368},
  {"context": "windows-latest / CPython 3.12.10", "integration_id": 15368},
  {"context": "windows-latest / CPython 3.13.14", "integration_id": 15368}
]
```

完整请求 JSON 和摘要保留在本轮本地提案中，未调用写入 API。
字段及行为依据 [GitHub ruleset REST API](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset)
与 [GitHub 可用规则](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)。
原生 required check 可接受的结论以 GitHub 合同为准；项目发行仍逐项要求实际四格 success，
不把中性或跳过的 job 当作该平台测试通过。

## 配置与验证步骤

1. 有管理权限的执行者重新读取 main、PR head/base、规则清单和四个 context/app。
   若出现已有规则或权限不足，停止并审阅差异；不删除、替换或放宽其他规则。
2. 核对提案 JSON 摘要和批准范围后创建这个精确 main ruleset，保存返回的规则 ID、
   创建结果和时间；读回该 ID 与 main 的适用规则，逐字段比较目标、来源、同步和绕过设置。
   请求结果不明时先查询规则 ID/名称，不能盲目重复创建。
3. 使用明确隔离的临时验证分支与相同规则副本，观察真实 PR 在检查 pending、failure
   时的 GitHub 合并阻断状态。故意失败只放在隔离分支的文档/whitespace，不修改测试文件、
   CI 合同或生产行为，不把故障提交合入 main。以读取合并状态取证，不尝试合并来测试拒绝。
4. 修正隔离提交后等待四格真实结束，记录由失败到 success 的检查与门禁状态变化。
   只有用户选定该临时远端验证范围后才创建资源；记录临时资源归属与清理范围。
5. 最后再核对 PR #2 的当前提交、检查、签名和待处理讨论。规则生效不代表 PR 审阅或合并
   已获授权；合并与合并后 main 的 CI、制品仍单独取证。

只读字段比对或本地 JSON 检查都不能代替第 2～4 步的远端证据。当前这些步骤未执行，
不能将本提案标为门禁 PASS。封面测试的确定性等待竞争也仍需按
[当前 CI 记录](CI.md)中的精确维护范围处理。
