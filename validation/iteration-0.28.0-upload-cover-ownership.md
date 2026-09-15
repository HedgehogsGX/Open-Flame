# Upload 封面规则所有权与备份朝向审计

日期：2026-09-12；分支 `codex/architecture-reset-ci`；起点 `36cfb56`。

此前 Upload service、backup 与 Workflow 分别解释封面格式和平台槽位。Bilibili 的一项实际
分歧已经复现：创建/确认拒绝竖图，保持 asset ID 并同步真实图片、尺寸和摘要后，备份及恢复
却接受同一配置。现在 `uploads/covers.py` 拥有图像结构与解码，轻量 `uploads/metadata.py`
拥有平台槽位、组合和尺寸规则；三个消费者使用相同公开合同，backup 不再导入 service。
Bilibili 非法朝向以既有备份错误在目标发布前拒绝。

解码器移入新文件不等于删除其行数。实际删除的是平台判断、MIME 映射及 Workflow 私有槽位
规则的副本；格式结构校验与 Pillow 完整解码均保留。没有新增依赖、数据库、Schema、runtime
或通用媒体框架。Workflow 使用纯 metadata 不加载 Pillow。

## 保留的行为

- service 先拒绝非法槽组合，再依次读取 landscape/portrait，最后判断尺寸；backup 保留自身
  引用、schedule、尺寸检查的原次序及领域错误。无媒体验证仍只核对组合。
- 槽位返回完整 `cover_landscape_asset_id` / `cover_portrait_asset_id` 字段，保留正方形、
  Tencent 比例容差和所有目标共同兼容时才采用来源封面的规则。
- Workflow 保留冻结身份、CAS 先于导入、fallback、取消 tombstone 和终态媒体删除后的重放。
- 图像结构、静态帧、EXIF、像素/内存预算、受管文件身份与最终导入/dispatch 复验保留。
- 备份格式 1/2 staging 迁移、格式 3 receipt、恢复政策和原备份不可变性保留。

## 当前验证

- 修复前建立的异常备份在修复后恢复被拒绝，未发布目标且原备份字节不变；异常源的新备份
  同样拒绝，合法横图仍能恢复。三项公开入口检查通过。
- 四个解码函数 AST 与冻结旧实现一致；41 组解码、56 组槽位、900 组规则与读取顺序比较
  无差异；两项非法组合在 I/O 前拒绝。
- 69 组真实 Workflow override 字段/顺序/冲突与 12 组无媒体 normalizer 对照无差异。
- 真实临时 UploadService 完成“取消 draft → 删除封面 → 同 key 重放 → backup → restore”，
  恢复仍为 canceled/deleted。三个 fresh import 核对责任方向通过，metadata 不加载 Pillow。
- 未修改的上传三文件回归：186 passed、30 failed，107.81 秒。相比 clean `36cfb56`，27 个
  旧失败 ID 保留，新增三项仍 patch `service.zlib/Image`。只在 ignored 副本迁移定位后，原
  三项断言通过；这不是 tracked tests 或 CI 通过。
- 本切片完整现有 pytest：2194 passed、248 failed、16 skipped，397.54 秒；245 个原有失败
  ID 保留，新增同上三项。没有将全部历史失败统一判成无害或放宽生产规则。
- 130 个生产 Python 文件可解析；24 个依赖兼容，CI 定义检查、发行源码预检通过。

前后 fixture/差分位于本分支 ignored `validation/local/architecture-reset-20260912/`；消费者、
完整回归及源码冻结清单位于本机审查目录 `architecture-review-20260912-cover-working`。
没有访问真实应用根、下载源、OpenAI、登录或上传平台；dispatch 为 0。

同期 clean `36cfb56` 的 hosted run
[34680878428](https://github.com/HedgehogsGX/Open-Flame/actions/runs/34680878428) 四格均失败于
offline pytest，不覆盖本切片。测试维护例外尚未明确，完整 CI、其他职责收敛和 clean release
仍未完成。本记录不宣称真实平台验收或整个架构重置完成。
