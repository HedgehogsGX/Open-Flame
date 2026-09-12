# Workflow 表单共享校验与固定日期意图

日期：2026-09-12；分支 `codex/architecture-reset-ci`；起点 `e4e84fd`。

此前选中“固定日期时间”但留空时，创建流程被原生 required 拦住，保存预设却绕过该门禁，
并由 `scheduleFor` 把空输入转成 null，重载后成为“不定时”。生产 HTML 的浏览器反馈环在
修改前实际发出一个 preset POST，修改后零 POST、显示字段错误并把焦点留在日期输入。
后端接受合法 null schedule 的合同不变；复用预设仍不要求填写来源 URL。

同一页面中的 `localPublishSchedule` 现在由即时清错与 schedule 构造共用；`publishTimeError`
同时用于绝对时间与已保存 target 的时限判断。标签、Bilibili 分区与视频号短标题也各自只保留
一份纯判定。分区提交规则补齐 1–10000 范围，与控件和后端一致；删除原来三处时限、两处
本地日期/DST、标签与短标题判断的副本。没有引入模块、框架、依赖或新表单状态。

调用者继续拥有原生 validity、required/disabled、字段相关性、DOM 提示、错误次序和焦点。
绝对日期缺失时只在该控件 required 的上下文拒绝；视频号草稿的禁用日期仍允许空值。
相对时间仍使用冻结 anchor，重放不被新的时钟再次判过期。逐账号字段、独立 override 和显式
null 保持，自动确认、AI 外发、真正创建/上传入口没有改变。

## 当前验证

- 最小生产页面反馈环修改前 red、修改后 green：创建及空日期预设保存的 POST 都为 0。
- 原生产函数与当前函数在 Adelaide、New York、UTC 和固定时钟下作 3321 项对照，无意外
  差异；另 35 项检查覆盖预期的 required 空日期拒绝和分区范围/提示修正。覆盖日期格式、
  DST 跳时/重复时刻、提前量、Tencent 整点/28 天边缘、标签、Unicode 与短标题。
- Chrome 152 独立 context 的 16 组生产页面检查通过：三平台 required/合法 none/合法
  absolute、Tencent draft、非法分区、三平台两天后冻结 relative 的完整 profile 重放、同平台
  两账号的独立内容与显式 null，以及深色/390px/200% 字号下的轮询与焦点保持。
- 页面截取的 11 份合法 payload 交给实际 WorkflowPresetStore，在临时根 create/get 均通过。
- 实际查看了浅色、深色、390px 和 200% 字号截图。受影响输入与焦点可见、无横向溢出；
  沿用现有 Editorial Glass 组件。没有声称重做四页视觉或完整无障碍认证。
- 原样运行 `tests/test_local_app.py` 与 `tests/test_release.py`：193 passed，17.00 秒。
  现有 tests 中未检索到 Workflow 的直接用例，因此上述生产页面与存储探针是必要补充。

浏览器使用原生产 HTML/CSS/JS，HTTP 数据和账号均由本地合成 routes 提供；存储验收单独
调用真实实现，没有启动真实 FastAPI 服务端栈、创建 Workflow、下载、登录、调用 AI 或上传。
没有修改 tracked tests。探针、截图、差分与 JUnit 位于 ignored
`validation/local/architecture-reset-20260912/frontend/`。

本切片未重复运行完整 pytest。最近封面工作区完整结果为 2194 passed、248 failed、16 skipped，
只覆盖其冻结源码；clean `e4e84fd` 的 hosted run
[34683683053](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34683683053) 仍失败。
这些结果不证明后续源码的完整 CI。Editing AI 图、Workflow retry/取消尾段、公共备份文件操作、
正式测试维护及最终发行验收继续推进，完整目标未完成。
