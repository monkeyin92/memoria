---
status: accepted
date: 2026-07-28
---

# 小程序游客浏览与微信身份

## Context

微信小程序审核不应在启动后立即强制登录。产品同时需要把旧 EchoLife 用户平滑带到
Memoria，并继续使用微信昵称、头像和手机号完成首次身份建立。

Memoria 已有用户名密码账号和服务端匿名身份；直接把审核游客建成匿名账号会产生无意义的
服务端资料、会话和回顾边界，也容易让游客内容误入长期数据。EchoLife 已使用
`openid → SHA-256 前 24 位 → wx_<hash>` 作为稳定用户 ID，旧数据需要保持这一映射。

## Decision

- 小程序三个 Tab 在未登录时均可浏览，不在启动或 `onShow` 时跳转登录。
- 游客浏览是纯客户端展示态，不创建服务端匿名账号，不请求个人资料、统计、回顾或语音会话。
- 对话、生成回顾、修改资料、数字分身和授权管理等受保护操作统一经过
  `apps/miniprogram/utils/auth-gate.js`；登录页保存返回路径。
- 身份被清除时，隐藏页面也必须立即停止仍在连接或录音的媒体任务，并清空当前字幕，不能
  依赖下一次切换 Tab 才清理私密状态。
- 首次微信登录使用 `wx.login`、`open-type="getPhoneNumber"`、
  `open-type="chooseAvatar"` 和 `input type="nickname"`。已建立过身份的用户可通过
  `wx.login` 静默恢复，不重复索要手机号。
- `openid` 是主身份；用户 ID 沿用 EchoLife 的 `wx_<sha256(openid)[:24]>`。
- 手机号只保存独立密钥 HMAC 和脱敏值，不保存明文；生产
  `MEMORIA_WECHAT_IDENTITY_SECRET` 必须与 JWT、LiveKit、Gateway 和内部能力密钥独立。
- 头像上传后保存为 Control API SQLite BLOB，并通过随机或迁移期稳定的 bearer-style
  public ID 读取；响应禁止缓存和 MIME sniff。
- H5 的用户名密码注册、登录和匿名接口继续保留，不因小程序微信登录而改变。
- EchoLife 迁移工具同时支持 JSON 会话/备份和 `echolife.sqlite`，可选复制本地头像。
  旧故事、时间线和访谈内容首轮不伪造成 Memoria 对话或回顾，只报告为 deferred。

## Consequences

- 审核人员可以直接查看三个主页面，只有主动操作时才进入微信登录。
- 游客不会产生服务端垃圾账号，也不会看到其他身份的资料或回顾。
- 首次登录依赖微信小程序 AppID/AppSecret；部署前必须安全配置服务端密钥，密钥不得进入
  小程序包、仓库或文档。
- 旧用户只要仍使用同一小程序 AppID 和 openid，就会回到迁移后的同一 `wx_` 账号。
- 旧数据迁移必须先 dry-run、备份目标 SQLite，并核对源/目标身份数；缺少旧生产数据源时
  不能把本机开发备份当作正式迁移完成。

## Alternatives considered

1. 启动即登录：拒绝。会阻塞审核和首次浏览。
2. 启动时自动创建匿名服务端账号：拒绝。游客没有产生个人数据的必要。
3. 以昵称或手机号明文作为主键：拒绝。昵称不唯一，手机号不应明文持久化。
4. 把 EchoLife 故事直接写成 Memoria 对话：拒绝。两套数据语义和证据边界不同。
