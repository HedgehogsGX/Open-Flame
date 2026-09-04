# 开源上传器接入审查

核对日期：2026-09-04。首批接入 Bilibili、抖音、视频号。仓库代码、安装验证与真实平台发布是不同证据层；本文不声称已经完成真实账号发布验收。

## 选型与当前状态

| 上游 | 实时核对 | 机制与适用范围 | 本项目决定 |
| --- | --- | --- | --- |
| [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload) | 非归档；main `0012d2c355f88f683cc38dde2a2db209e14091bc`，提交于 2026-09-02；MIT | 抖音、视频号等使用 Patchright 浏览器自动化；按账户保存 storage state；CLI 的视频号键为 `tencent` | 固定源码归档作为独立运行时；调用其现有上传类，自写进程、账户、状态边界 |
| [biliup/biliup](https://github.com/biliup/biliup) | 非归档；master `906e0f6fdb104d65989d12b76c9a6f02205384cb`，提交于 2026-09-01；MIT；[v1.2.4](https://github.com/biliup/biliup/releases/tag/v1.2.4) 发布于 2026-08-23 | Rust CLI 登录、Cookie 刷新、分片上传及投稿；支持 `--copyright`、`--source`、`--tid` | 固定 Windows x64 二进制；直接调用，避免 SAU 自动下载 latest 及遗漏转载来源参数 |
| [biliup/biliup-rs](https://github.com/biliup/biliup-rs) | 已归档；最新提交于 2025-11-30；MIT | 旧独立 Rust 仓库 | 不接新功能；使用已合并发展的 biliup/biliup |
| [kebenxiaoming/matrix](https://github.com/kebenxiaoming/matrix) | Apache-2.0；最新提交 `56bb5976a458e652d30e8e09314a007d34973817`，2025-12-27 | Python/Playwright 多平台队列，需要 MySQL、Redis | 可参考工作流；不引入另一套服务及数据库 |
| [google-api-python-client](https://github.com/googleapis/google-api-python-client) | Apache-2.0；官方 Python API client；2026-09-02 有 main 提交 | YouTube 官方 OAuth 与 resumable upload | 留给以后 YouTube 接入；不在本批启用 |

源码许可正文：[SAU LICENSE](https://github.com/dreammis/social-auto-upload/blob/0012d2c355f88f683cc38dde2a2db209e14091bc/LICENSE)、[biliup LICENSE](https://github.com/biliup/biliup/blob/v1.2.4/LICENSE)、[Google client LICENSE](https://github.com/googleapis/google-api-python-client/blob/main/LICENSE)。外部下载保留上游原始声明；Open-Flame 自写桥接代码仍使用项目 Apache-2.0。MIT 不等于所有依赖、浏览器或离线合集统一改用 MIT；再次分发应分别携带其适用许可。此接入不复制上游实现到本项目源码包。

## 实际调用边界

[固定 CLI](https://github.com/dreammis/social-auto-upload/blob/0012d2c355f88f683cc38dde2a2db209e14091bc/sau_cli.py) 的三个平台均有 `login` / `check` / `upload-video`。抖音、视频号支持 `--headed`；视频号实现了 `--draft`。SAU 没有统一 JSON 结果，也没有账户根目录环境变量；桥接层自行提供私有账户路径和只含固定安全码的结果文件。Bilibili `check` 实际执行 `renew`，会刷新本地账户文件。

[SAU 的 biliup runtime](https://github.com/dreammis/social-auto-upload/blob/0012d2c355f88f683cc38dde2a2db209e14091bc/uploader/bilibili_uploader/runtime.py) 缺少二进制时会下载最新版本；当前代码 `force_check=False` 会复用已有文件，与 README 的每次检查更新描述不同。Open-Flame 完全绕过这条自动下载路径。

[biliup Studio 参数](https://github.com/biliup/biliup/blob/v1.2.4/crates/biliup/src/uploader/bilibili.rs) 区分 `copyright=1` 自制与 `2` 转载。桥接层真实传入 `--copyright` 与 `--source`，不会因调用 SAU 的简化 CLI 而让转载材料落到默认自制。Bilibili 分区编号也必须明确给出。

## 发布结果与重复投稿

[抖音提交代码](https://github.com/dreammis/social-auto-upload/blob/0012d2c355f88f683cc38dde2a2db209e14091bc/uploader/douyin_uploader/main.py) 会在失败后循环点击最终按钮。自写 `SingleSubmission` 包装同一最终按钮的普通点击与 JavaScript fallback；第一次结果不明时退出，不再点击。发布成功状态表示上游观察到进入作品管理页，不是独立服务端回执，也不代表审核通过。

[视频号提交代码](https://github.com/dreammis/social-auto-upload/blob/0012d2c355f88f683cc38dde2a2db209e14091bc/uploader/tencent_uploader/main.py) 原本会将离开 `/post/create` 或按钮消失视为成功；这也可能是登录失效。桥接层覆盖该方法：点击前监听确切投稿 POST，只有 HTTPS 视频号域名、相应 `post_create` 路径、HTTP 200 和显式整数 `errCode: 0` 才记录 `submitted`。错误、缺失回执、取消或超时均为 `unknown`，不自动重发。草稿要求保存操作后进入同源的明确作品列表路由。

视频号 POST 路径与 `errCode` 结构依据另一个实现者的[原始协议观察代码](https://github.com/liuxuehao/weixinshipinhao_publisher/blob/main/channels_publisher.py)（`CGI`、`MICRO_CGI`、`post_create`、`_post_json`）核对；该仓库未提供明确许可证，本项目没有复制其代码或安装它。该事实属于第三方实现证据，仍需真实账号验收确认当前平台响应是否一致。不会把未知响应硬改成成功。

## 登录、临时数据与运行时

登录只响应用户单独的扫码登录操作：三个平台都在本页显示二维码，由用户用手机扫码和确认；抖音/视频号只运行独立 headless 浏览器。Bilibili 按 [固定 credential.rs](https://github.com/biliup/biliup/blob/03b7a84f55a31f407570f7d19ef5581101434bf3/crates/biliup/src/uploader/credential.rs) 的官方 TV QR 申请/轮询协议接入，手机授权页可能显示 TV 客户端，完整 Cookie 与 app tokens 才能写入 `BiliTV` 账号结构。上传不会自动登录或导入日常浏览器秘密。抖音的共享 `verify_code.txt` 读取被关闭；如平台要求额外验证，页面显示提示，由用户按平台要求完成。

所有子进程先等待归属 gate；父进程完成 Windows Job 绑定后才允许工作。上游 stdout/stderr 不进入 API 或应用日志，SAU 文件日志接口在导入前替换为无输出接口。页面内二维码使用已有 Segno 生成，不新增依赖。当前真实浏览器运行仅支持 Windows x64，POSIX 下载能力不等于上传浏览器已经获得相同的进程清理保证。

[biliup checkpoint 源码](https://github.com/biliup/biliup/blob/v1.2.4/crates/biliup-cli/src/uploader.rs) 按视频路径生成哈希，并写入 Windows Known Folder。桥接层使用每次操作独有的媒体路径，避免不同账户复用同一来源文件的 checkpoint；作业结束后仅清理本次操作新创建的确切 checkpoint。不会扫描或删除其他用户文件。

YouTube 后续可优先使用[官方 resumable upload](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)；[videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert) 当前对特定未审核 API 项目限制 private 可见性。不要把浏览器自动化与官方 API 的账号/审核要求混为一谈。

安装、固定散列与验证范围见 [UPLOAD_RUNTIME.md](UPLOAD_RUNTIME.md)。
