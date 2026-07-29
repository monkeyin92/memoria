# 项目交接

## 当前状态

- 记忆架构 P0–P4 发布候选 `20260729-093337` 已完成，正在提交、推送和生产部署：
  类型化投影、13 场景评测、
  EpisodeConsolidator、Skill Domain、Mem0 影子、pgvector HNSW/基准和 TurboVec 硬门禁均已落地。
  权威证据账本不变，工作记忆仍只按话轮动态组装。
- 当前仓库代码基线：本文件所在 `main` 提交，包含三阶段语音架构收口；已同步
  `origin/main`。生产 runtime/H5 尚未切到该仓库 checkpoint。
- 生产 runtime source / annotated tag：
  `e7230236ddff0d64f602e38487387e614d2fb515 / 20260728-170236`，直接回滚点为
  `20260728-123528`；该版本已完成原子切换、生产门禁和公网验收。
- 生产 H5：`20260723-192611`，本轮 runtime 发布没有切换 H5。
- 微信小程序开发测试版：`0.8.59` 已上传成功（`636,385` 字节），但真机已确认欢迎语首播后
  因遥测契约不兼容断开；修复后的 `0.8.60` 已通过 CLI 上传开发测试版（`637,083` 字节），
  开 VPN 可完整聊天。诊断版 `0.8.61` 已上传（`637,237` 字节）；真机和 Safari 均确认标准 443
  在当前无 VPN 网络不可达，生产已按用户决定回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`。443 精确路由保留但不下发。
  用户已确认回切后页面与语音连接恢复可用。小程序未提审或正式发布；完整 iPhone/Android、
  音频路由、弱网和 AEC A/B 真机矩阵仍待完成。
- 本轮开发测试版均通过已登录的微信开发者工具 `upload` 完成，只上传开发版本，不提审、不正式发布。
  本机未跟踪的上传私钥、辅助脚本和 lockfile 不属于仓库交付，路径和值不得写入本文或提交。
- 当前交付客户端为 `apps/h5` 与 `apps/miniprogram`；legacy Web 与原生 iOS 源码已移除。
- 历史路线、架构决策和发布证据分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与
  `docs/releases/20260728-170236.md`。

## 最新实现

- 长期记忆新增独立的 `memory_kind / domain_category / item_kind`，搜索投影携带实体、
  有效期、观察时间、稳定度、重要度、敏感度、冲突状态、检索分数和全部来源。
- EpisodeConsolidator 可跨会话合并同一现实事件，同时保留独立 timeline/evidence；
  canonical key 按主题领域隔离，明确不相交实体与不同事件保守分开。
- 程序技能具备候选、版本、主人审批、单次运行确认、严格 JSON Schema 子集、工具白名单、
  顺序步骤、完整运行审计和逆序补偿。Skill definition/version/run 是持久业务状态；
  只有统一搜索文档可重建。尚未自动接入实时 Agent 工具链。
- Mem0 只运行隔离影子评测，TurboVec 只作为可选实验索引；二者均不写权威记忆。
  pgvector 按 embedding model/dimensions 隔离并建立 partial HNSW，维度漂移 fail closed。
- 账户导出、删除、PostgreSQL dump/restore 与投影重建已覆盖 Skill 状态和搜索文档。
  设计与边界见 `docs/adr/0028-typed-memory-projections-and-derived-experiments.md` 和
  `docs/implementation-plan-20260728-memory-architecture.md`。
- `UtteranceRouter` 仍是 enroll、纯打断、打断后继续提问和普通聊天的唯一副作用入口。
- 小程序改为受控话轮：AI 处于 thinking、tool waiting、speaking、recovering 等响应状态时
  暂停 `RecorderManager` 上行；Agent 回到 listening 后仍等待本地 pending PCM 和全部
  WebAudio source 结束，才恢复录音。
- 用户主动静音优先，回答结束不会擅自重新开麦；小程序播放期录音、口头打断和旧的
  `interruptPlayback` 话轮入口已删除。当前本地候选保留独立“停止播放”按钮，但它不会
  同时开麦或恢复全双工。
- Gateway 无论 AEC 是否可用都标记小程序平台；Agent 对该平台关闭 LiveKit interruption、
  KWS、歧义语意复核和播放期转写接纳，迟到终稿按原 speech epoch 丢弃。
- H5 的 `barge_in_enabled` 默认保持开启；Cascade `UtteranceRouter` 与 Omni 备用
  transport 均覆盖“等等、等一下、停一下、先别说”等控制语意。
- Vosk KWS 实现和宿主机模型仅为直接回滚版本及未来独立 RTC PoC 保留；
  当前小程序会话不会构造或运行 KWS。
- Gateway 新增精确 session、默认 5 秒、最大 15 秒的 AEC 前后 WAV 采样；默认关闭，
  文件/目录权限为 `0600/0700`，只记录 session hash 和音频参数。
- 小程序媒体契约由 `packages/contracts/miniprogram-media.json` 同时约束 Python 与
  JavaScript，固定下行 `24 kHz / mono / s16le / 20 ms / 960 bytes`。
- Socket 断开立即停止录音；mic 关闭压过迟到的 `RecorderManager.onStart`；同步发送失败
  不提交 sequence。
- 播放器使用固定帧校验、首批 lead、短 gain ramp 和 generation/reset barrier。
- 系统录音中断结束后发送 `uplink_discontinuity(next_sequence)`，同一 WSS 会话重置
  半帧、sequence 与 AEC 时序后恢复。
- Agent、H5 与小程序只消费带有效 `session_id`、generation 与权威来源的会话事件。
- 同一 `speech_epoch` 的打断确认语最多播放一次；迟到 ASR 终稿和下一次 VAD
  不再重复发布确认音频。
- 小程序回传首播、underflow、hard reset 和 conceal 的有界数值遥测；
  Gateway 白名单校验并限速，不接受文本、音频、token 或 cookie。
- 播放 underflow 只重建后续排程，不再硬停仍登记中的旧 source。
- FunASR 已支持可选热词表 ID 和噪声阈值，但生产没有录音校准证据，当前保持未配置。

## 2026-07-28：语音架构最终收口（仓库 checkpoint，runtime/H5 未部署）

- 小程序新增默认 150 ms、可配置的播放尾音保护；播放 lead 在 100–180 ms 间自适应；
  `input_policy/policy_epoch` 成为服务端录音权威，用户静音和本地播放器仍是客户端安全门。
- Gateway AEC 支持 `off/on/alternating`，正常半双工默认关闭；A/B 分组按 session 稳定，
  `ready.aec` 返回 variant 与实际 APM 状态。真机结果记录模板见
  `docs/acceptance/miniprogram-half-duplex-device-matrix.md`，当前全部仍为待验收。
- 小程序保留独立“停止播放”按钮：本地平滑清空后调用既有 stop-response 推进 generation；
  不恢复播放期录音或语音打断。普通半双工回答限制为 3 句/120 字，明确长内容请求保留
  长回复预算。
- H5 已抽出 `VoiceTransport`、`LiveKitCascadeTransport` 与 `voiceSessionReducer`；
  Omni 移入 `voice/experimental/`；回顾页、资料页、偏好行和 14 个领域 API 模块已拆出，
  旧 `api.js` 只保留兼容导出。
- H5 与 Cascade 共用 `packages/contracts/h5-interruption-corpus.json`，句首控制意图不会再
  误伤“我等一下再说”“这个站不是终点”“不是所有人……”。
- 后端新增包装现有 `GenerationFence` 的不可变 `CancellationContext`、provider Handler
  seam、单调 `turn_revision` 和最小 Realtime-style facade；facade 只支持文本映射与取消，
  音频仍由 LiveKit/MiniProgramMediaGateway 承载，没有第二套 runtime。
- ASR 保持 LiveKit `STT` adapter，并由统一构造 seam 隔离供应商初始化；编排层直接调用的
  LLM/TTS 使用窄 Handler/Protocol。首个用户权威 final 或助手 actual-heard final 会关闭
  对应 fenced turn 的修订流，避免 UI 终稿和长期历史分叉。
- HF `speech-to-speech` 只作为 Handler、取消、revision 和协议设计参考；不引入其 runtime、
  本地 STT/TTS 模型或 WebRTC。决策见
  `docs/adr/0027-hf-speech-to-speech-as-design-reference.md`。
- 本地门禁：Python `1345 passed, 27 skipped`；Ruff；165 个 strict mypy source；
  H5 `241/241` 与 production build；小程序 `76/76`、JavaScript syntax、共享 JSON；
  离线 E2E 与 `git diff --check` 全部通过。
- 现有 Chrome 已验证首页、回顾页、个人页、编辑弹层和偏好保存/恢复，console 无
  warning/error；未触发真实麦克风权限或设备语音链路。
- 微信开发者工具 skill `0.3.5` 与当前工具版本一致、登录有效；小程序首页 WXML/WXSS
  编译成功，`pages/home/index` 整页编译打开，console 的 error/warn/fail/exception
  过滤为空，模拟器截图无白屏、遮挡或明显布局回归。
- `0.8.59`、`0.8.60` 与 `0.8.61` 均由已登录的微信开发者工具官方 CLI 上传开发测试版；
  最新 `0.8.61` task 为 `confirmation_upload_3e652991-38c2-4c75-ae65-d16433d1f3a5`，
  返回 `status=success / execution_success`，总包 `637,237` 字节。未提审或正式发布。
- 三阶段代码已提交并推送仓库；生产 runtime/H5 仍是本文件“当前状态”所列版本。

## 2026-07-28：`0.8.59` 首播后断开与向后兼容修复

- 真机点击开始语音后，生产 Gateway 均先完成 `ack_sent` 与 `ready_sent`，随后分别在约
  `1503 ms`、`1558 ms` 进入 `protocol_error`；Agent 已加入房间并开始欢迎语，因此不是公网、
  ticket、LiveKit 或 Provider 启动失败。
- 根因是 `0.8.59` 的 `first_playback` 遥测新增 `target_lead_ms`、`underflow_count`，而生产
  `20260728-170236` 仍使用旧的严格字段白名单。将真实首播形状送入该生产版本校验器可稳定复现
  `invalid client audio trace`；去掉两个新字段后立即通过。
- 客户端遥测出口现默认使用 v1 名称与字段；只有 Gateway 在 `ready` 中明确广告
  `client_audio_trace_version >= 2`，才发送 `miniprogram_playback_lead_adjusted` 及两个新字段。
  新 Gateway 广告 v2，未来版本仍可保留完整自适应播放指标；旧 Gateway 无需先部署即可兼容新客户端。
- 修复后跨版本反馈环已验证：当前客户端生成的 `first_playback` payload 可被生产
  `20260728-170236` 校验器接受。小程序 `75/75`、Gateway 全套测试、共享契约测试、Ruff、
  strict mypy 165 个 source、JavaScript syntax、JSON 与 `git diff --check` 通过。
- `0.8.60` 上传 `--dry-run` 已通过；随后经用户确认，由微信开发者工具 CLI `upload` 成功上传，
  task `confirmation_upload_64566f4f-070a-4d1f-a788-5da39349889b` 返回
  `status=success / execution_success`，总包 `637,083` 字节。未提审或正式发布；立即恢复真机连接
  不要求先切换生产 Gateway。

## 2026-07-28：无 VPN 网络诊断、标准 443 尝试与回切 8443

- 同一 iPhone 的 `0.8.60` 开 VPN 后，session `9477a320-7a46-495f-b263-629859105ea0`
  完成 Gateway `ack_sent/ready_sent`、欢迎语、用户 VAD/ASR、LLM、TTS、首播遥测和按钮停止，
  证明客户端协议修复与完整语音链路有效。
- 关 VPN 后，多次点击仍能通过 HTTPS 创建 session 并重取 gateway ticket，例如
  `2568674a-4544-4bab-bf2d-5e05eb2ce602`、`23636276-2b7d-4f12-b758-94f3a91eb2f4`、
  `47d7e7f9-f949-459c-b570-3da96c706fd4`，但 Gateway 完全没有 WebSocket
  `connection open/ack_sent`。
- 针对手机公网来源 `222.94.122.53` 的双向抓包确认：到达服务器的普通 8443 HTTPS
  请求均完成 TCP 三次握手、TLS 双向传输和正常 FIN，服务器没有 RST、限流或防火墙拒绝；
  这些连接与 Nginx 中的 profile/session/ticket API 一一对应，没有独立
  `wx.connectSocket` Upgrade。阿里、腾讯、Cloudflare、Google DNS 均只返回同一 IPv4
  `122.51.108.140`，没有 AAAA 分流。
- 结论：connection refused 发生在手机当前 Wi-Fi/无 VPN 的非标准端口 Socket 直连路径，
  不是 Gateway、Nginx、TLS、DNS、ticket 或应用协议错误。切换前生产 443 的同一 Upgrade
  探针稳定返回 `404`，形成可执行的红色反馈环。
- 本地候选新增单一共享 `infra/nginx-memoria-miniprogram-media.conf`，只包含精确媒体 WSS
  location；8443 Memoria server 与未来 WMS 443 server 复用该 snippet。Control API 示例 URL
  改为 `wss://aigcnice.com/memoria-mini-media/v1/mini-program/media`。没有迁移 H5、Control API、
  LiveKit 或 WMS 根路径。
- 生产配置/升级环境测试 `39/39`、Ruff、Bash syntax 与 `git diff --check` 通过。生产已安装
  443 snippet、更新 Control API env、reload Nginx，并只重建 Control API；首次 readiness
  `503` 明确为等待 Agent 新心跳，14 秒后自然恢复 `ready / 9/9 core`。
- 用户授权暂时停止 WMS 并将资源留给 Memoria：`wms.service` 当前为 `inactive / enabled`，
  8090 已关闭，WMS 数据和配置未删除。root-only 回滚目录为
  `/var/backups/memoria/miniprogram-443-20260728-214525`；WMS 配置按真实软链解引用备份。
- 443 与 8443 的无效票据 Upgrade 均返回 `101`，443 根仍为 `302`、未知路径仍为 `404`；
  独立公网主机已直连 443 获得 `101`。同一 iPhone 关闭 VPN 的
  `ack_sent → ready_sent → first_playback → listening` 仍是最终验收。
- 真机首次切到 443 后页面显示“证书校验失败”，但 443/8443 实际下发同一张
  `aigcnice.com` 证书和三证书链；独立公网 OpenSSL 与 macOS Apple 信任库均验证成功，
  SAN、CT 和 Certum 交叉签发链正常。根因是客户端把任何包含 `handshake` 的原始错误都误分类为
  certificate，当前文案不能证明 TLS 证书失败。
- `0.8.61` 已将 SSL/TLS/certificate 与普通 WebSocket handshake 分开，后者保留
  截断后的原始 `errMsg`；回归测试先红后绿，小程序 `76/76`、JavaScript syntax、
  `git diff --check` 和上传 `--dry-run` 均通过。用户确认后由微信开发者工具上传成功，
  task `confirmation_upload_3e652991-38c2-4c75-ae65-d16433d1f3a5` 返回
  `status=success / execution_success`，总包 `637,237` 字节；未提审或正式发布。
- `0.8.61` 真机在 443 仍返回 SSL/TLS 错误；同期抓包显示手机完成 TCP 三次握手并发送约
  517 字节 TLS ClientHello，随后手机或当前网络在确认服务器 TLS 数据前发送 RST，Gateway
  和 Nginx HTTP 层均未收到请求。同一 iPhone 关闭 VPN 后用 Safari 打开 443 也失败，证明问题
  不属于 `wx.connectSocket`、票据或 Gateway。
- 用户决定继续使用 8443。生产 `MINIPROGRAM_MEDIA_GATEWAY_URL` 已回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`，仅重建 Control API；
  readiness 首次因等待 Agent 心跳返回 503，14 秒后恢复 `ready / 9/9 core`。回切前 env 备份为
  `/var/backups/memoria/miniprogram-media-url-8443-20260728-222317`。WMS 继续
  `inactive / enabled`，8090 关闭；443 Nginx 精确路由保留但当前不下发。
- 回切后用户已确认页面与语音连接恢复可用；该确认关闭 8443 主链连通性故障，但不替代
  外放/听筒/蓝牙、弱网、前后台、系统录音中断和 AEC A/B 的完整声学矩阵。

## 2026-07-28：游客浏览与微信身份（已发布，真机验收待完成）

- 三个主 Tab 未登录可浏览，不再启动即跳登录。游客态不创建服务端匿名账号；回顾页不读取
  或展示历史，“我的”页不读取资料和统计。
- 对话、生成回顾、修改资料、数字分身与语音授权统一经过 `utils/auth-gate.js`，并保留登录
  返回路径。登录页使用 `wx.login`、手机号授权、微信昵称、微信头像和平台隐私保护指引。
- 退出或清除身份会广播到隐藏的陪伴 Tab，立即作废仍在等待中的语音启动、关闭媒体并清空
  当前字幕；首次手机号登录前的 openid 探测命中 `phone_authorization_required` 后不在登录页
  重复请求。
- 认证状态使用单调 `authEpoch` 约束所有私有异步回调；退出、401 或重新登录后，迟到的回顾、
  资料、统计、隐私授权和数字分身响应不得重新写回游客页。
- 短期 access token、到期时间和最小身份快照可本地恢复；token 失效后，已存在的 openid
  身份通过 `wx.login` 静默换取新会话，首次身份才要求手机号；微信平台
  `40014/42001` 会清除服务端 access token 缓存并只重取一次。
- Control API 新增微信登录、头像上传/读取和微信注销复核。`openid` 沿用 EchoLife 的
  `wx_<sha256(openid)[:24]>`，手机号只保存独立 HMAC 与脱敏值；H5 用户名密码入口保留。
- `scripts/migrate_echolife_users.py` 已支持 EchoLife JSON、备份和 SQLite，支持 dry-run、
  目标 SQLite 在线备份、幂等身份导入和可选本地头像复制；旧故事/时间线不写成 Memoria
  对话。
- 本机真实目录 dry-run：扫描 4 个源文件，发现 1 个旧身份、0 冲突；该身份来自开发备份，
  头像为不可迁的 `wxfile` 路径，4 份旧内容源保持 deferred，因此未正式写入本机或生产。
- EchoLife 旧生产主机是 `110.42.235.198`，Memoria 当前生产主机是
  `122.51.108.140`，两者不得混用。2026-07-25 17:00 UTC 清理旧主机前曾把完整
  `/opt/echolife` 打成 39 MiB 的 root-only 回滚归档；约 4 分钟后该唯一归档也被永久删除。
  当前旧主机、现存服务器备份及 Memoria 冻结快照均不含 EchoLife 数据。若需恢复正式旧用户，
  只能从腾讯云实例 `ins-d5nngvzh` 在删除前的云硬盘快照提取；禁止在仍运行其他生产服务的
  ext4 根盘上直接执行 undelete。
- 当前生产 Memoria SQLite 有 96 个 profile、4 个注册账号、0 个 external identity、0 个 avatar，
  尚未执行旧用户迁移。不能把本机仅含 1 个开发身份的备份冒充正式迁移。
- AppID 已确认是 `wx20a3a044b52fcbb7`；产品负责人已重新提供现有
  `WECHAT_MINIPROGRAM_APPSECRET`，只允许写入 Memoria 生产服务器的 root-only env，不得进入命令
  输出、仓库或文档。该值曾出现在历史自动化命令记录中，体验版闭环后应在微信公众平台重置。
  `MEMORIA_WECHAT_IDENTITY_SECRET` 必须独立生成，不能复用 AppSecret 或其他认证密钥。
- 微信开发者工具已验证陪伴、回顾、“我的”三个游客页可直接渲染，页面数据均为
  `authenticated=false`，且 console 无异常；点击“开始语音陪伴”会进入带返回路径的登录页。
  生产微信登录与头像路由已上线，微信官方 access token 接口验证通过；手机号、昵称、头像、
  静默恢复和登录后语音仍需在 `0.8.58` 真机验收。
- `e723023` / tag `20260728-170236` 已提交、推送并部署；小程序体验版 `0.8.58`
  已经微信开发者工具确认上传，包体 `631,692` 字节，未提审、未正式发布。

## 生产健康

- `agent / control-api / speaker-model / miniprogram-gateway` 四容器均为
  `healthy`、restart 0。
- readiness 为 `ready / 20260728-170236`；9/9 core checks、Agent heartbeat、
  LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过。
- 四个 runtime 容器最近十五分钟未出现 traceback、关键 provider、provenance、
  archive durable/spool 或连接拒绝错误。
- 公网根 H5、兼容 H5、SPA、API live/ready 与 WMS 静态页为 200；
  `/memoria-api/internal/` 为 404。
- 小程序公网 WSS 在标准 443 和回滚 8443 均成功升级；无效 TLS Upgrade header ticket
  按协议关闭为 `4401`，证明请求头已透传至 Gateway。
- Nginx 配置、readiness timer 与 certbot timer 正常；WMS 暂时为 `inactive / enabled`，
  8090 已释放。IP 证书有效至
  `2026-08-03 01:40:00 UTC`，域名证书有效至 `2026-10-18 03:59:59 UTC`。
- PostgreSQL、MinIO、LiveKit、WMS、数据库快照与 Docker 数据卷未在清理中修改。

## 2026-07-28：Gateway 握手验收日志上线（真机无 VPN 复测待完成）

- `33a741b` / tag `20260728-123528` 已推送并原子切换 runtime；本轮只让 Gateway 的脱敏
  `ack_sent` / `ready_sent` 等 `INFO` 日志走 Uvicorn 的 `uvicorn.error` handler，不改协议、
  小程序包、H5、Nginx、数据库或 provider 配置。新增回归测试断言 header 握手日志恰有两条且不含 ticket。
- source、四个 linux/amd64 镜像、H5 artifact 的 manifest/verifier、服务器导入校验与隔离
  H5/API/SQLite restart smoke 均通过；SQLite 双副本 SHA-256 为
  `a285511d8e26413263e792b9312ad4a0304e7f098b3d7d9acf3f2477a309176f`，四份 root-only env
  回滚副本已创建。
- 四容器 healthy，ready 绑定 `20260728-123528`；LiveKit、FunASR、Qwen、Doubao 和 5 条
  InterruptSemantic smoke 最终通过。首次 refresh 的语意分类请求在 0.6 秒上限内瞬时超时，立即重试
  已通过；需继续观察，不把一次超时写成代码回归。
- 发布后用无效 ticket WSS smoke 验证 `close_code=4401`，容器日志可见
  `mini_program_gateway_handshake ... phase=ticket_rejected`，证明新增 `INFO` 输出已经生效。
- 用户关闭 VPN 的实时取证显示 TCP SYN 到达服务器，且其中两次已进入 Gateway；独立宽带的
  `8443` TLS/WSS 也获得 `101`。因此不能把当前现象归因为“8443 全局被封”。443 外部 TLS 尚有
  ClientHello 后断开证据，未迁移入口，避免影响 WMS；下一步只以同一 iPhone 无 VPN 的
  `ack_sent` / `ready_sent` 日志定性。

## 2026-07-28：小程序 handshake acknowledgement 竞态修复（体验版已上传，真机待验收）

- `569e225` / tag `20260728-131612` 已推送。客户端现将有效 `handshake_ack` 视为已认证传输确认：
  取消此前泛化 `SocketTask.onError`，等待严格校验后的 `ready`；ack 后的泛化 error 不再覆盖具体
  close code。
- 新增 10 秒 bridge-ready deadline；超时返回 `gateway_ready_timeout` 并关闭 socket。失败、关闭或显式
  停止后，迟到 `ready` 不得重新启动录音。
- 小程序完整测试 `58/58`、JavaScript syntax check 和 `git diff --check` 已通过；独立复核未发现
  P0/P1。体验版 `0.8.57` 已在微信开发者工具确认后上传成功（约 600 KB），无需重新部署服务端。
- 尚未完成：同一 iPhone 关闭 VPN、完全退出后重新进入 `0.8.57`，单次点击“开始语音陪伴”验证
  Gateway `ack_sent → ready_sent` 和页面 listening；这项真实验收不能由上传成功替代。

## 2026-07-28：小程序 WSS 首包缺失与 header 握手修复

- 体验版 `0.8.54` 已上传成功，但同一 iPhone 仍报“connection refused”。真实抓包已证明：手机
  已完成 `8443` TCP/TLS 和 WebSocket Upgrade，Nginx 已转发至 loopback `8792`；随后约 2.9 秒
  没有任何客户端 WebSocket 数据（未发送首条 JSON `hello`），客户端才发送 close/FIN。网关首包
  超时为 10 秒且没有主动关闭。根因不在网络、证书、Nginx、Agent 或 ticket，而是原生
  `SocketTask.onOpen → hello` 握手路径在真机上未可靠执行。
- 修复：新客户端把同一短期、签名 gateway ticket 与 generation 能力放入 TLS Upgrade header；
  网关在 Upgrade 后直接验证并发送 `ready`。旧客户端的首条 JSON `hello` 仍保持回退，ticket 不进
  URL、access log 或应用日志。纯 `connection refused` 才触发一次 ticket 刷新；混合 timeout/refused
  不再被误归类重试。
- 已完成：网关单测 `6/6`、小程序 `51/51`、Ruff、mypy、JS syntax 与 diff check；runtime
  `20260728-103318` 已先行上线，commit-bound source/images/H5 manifest、隔离 smoke、SQLite
  双副本、四份 env 回滚副本、readiness、Nginx/WMS 和无效 header `4401` WSS smoke 均通过。
  体验版 `0.8.55` 随后上传成功；服务器临时抓包已删除。
- 尚未完成：同一 iPhone 使用 `0.8.55` 实际点击“开始语音陪伴”并确认收到 `ready`、不再显示网络
  拒绝；这项真实设备验收不能由上传成功或无效 ticket smoke 替代。

## 2026-07-28：`0.8.55` 仍显示连接拒绝的二次修复（已上线，真机待验收）

- 同一 iPhone 的后续点击仍显示泛化的“connection refused”。Nginx 时间线同时显示同一设备在短时间内
  重复创建会话，并在第四次 `POST /memoria-api/v1/sessions` 命中 `429`。由于 `8443 → 9443` 的
  stream 转发让内层 HTTP 看见 loopback 地址，旧 `burst=3` 会放大重复启动；这不是唯一根因，不能只靠
  放宽限流掩盖问题。
- 候选修复把 `startVoice()` 改为脱离 `setData` 异步刷新的单飞 Promise，避免一次点击或紧邻点击创建多
  个 session；精确会话路由仅将安全的 `burst` 从 `3` 提到 `6`，保留相同 `10r/m` 限速。没有把未验证的
  `Authorization` header 用作 Nginx 限流身份；那会允许伪造不同 header 绕过桶。真正的按账号限流应在
  Control API 完成 JWT 校验之后再单独实现。
- 新客户端继续发送 TLS Upgrade header，同时在 `SocketTask.onOpen` 发送既有 JSON `hello` 回退；Gateway
  无论 header 或 hello 验证成功，都会先回无敏感数据的 `handshake_ack`，再等待 LiveKit bridge 并发送
  `ready`。旧客户端和旧 Gateway 均保持互操作，ticket 不进入 URL、access log 或应用日志。
- 处理两个真机时序：`ready` 后不再补发 hello；header 认证的服务端只忽略一次精确的迟到 v1 hello，即使
  首个 PCM 已到达。`SocketTask.onError` 先到时会给 `onClose` 100 ms 传递 `4400/4401/1011` 的机会，避免
  有意义的 gateway 拒绝码被泛化成“connection refused”。
- Gateway 只新增脱敏的 `transport=header|hello`、`ack_sent|ready_sent`、耗时日志，用于下一次真机验收；
  不记录 ticket、PCM、用户文本或 cookie。
- 候选本地门禁：小程序 `56/56`；Gateway 与生产 Nginx 契约 Python `81/81`；Ruff、严格 mypy、JS syntax、
  JSON 解析和 `git diff --check` 均通过。Python 开发环境已由锁定的 `uv` 重新创建为 CPython `3.12.13`，
  原 `.venv` 指向已删除的 Homebrew Python 3.12，未使用系统 Python 3.13 代替。
- 已完成：`57eefdc` 与 tag `20260728-114049` 已推送；commit-bound source、四镜像和 H5 artifact
  均在服务器 manifest 校验、隔离 smoke、SQLite 双备份、四份 env 备份后原子切 runtime。新 runtime
  的四容器 healthy，9/9 readiness、LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过；
  Nginx 精确 session `burst=6` 已 reload，公网 H5/API/WMS 为 200，WSS 无效 ticket 为 `101 → 4401`。
- 小程序 `0.8.56` 已于微信开发者工具确认后上传成功（约 599 KB）；真机验收仍必须观察 Gateway
  `ack_sent` 与 `ready_sent`，并确认页面进入 listening。若仍失败，按同一时间窗口取 Gateway 脱敏日志和设备
  原始 `errMsg`，不再猜测网络问题。

## 保留版本与回滚

- 当前 runtime：`20260728-170236`。
- 直接回滚 runtime：`20260728-123528`。
- 固定 H5：`20260723-192611`。
- 本机和生产均只保留当前与直接回滚两套 runtime 的四角色 Docker tag。
- 生产 source release 保留 `20260728-170236` 与 `20260728-123528`；
  H5 release 只保留 `20260723-192611`。
- 本 release 的 SQLite 双备份 SHA-256：
  `6b9fc7f7ead2aca3e47837a97d827de0b407fbd81a1ba784e14f48bf53d6cbd0`；
  四份 env 与 Nginx 回滚副本均为 root-only。
- 上一 runtime release 的 SQLite 双备份 SHA-256：
  `ca973329eb36519494d22076739cf4c32c102ccf2e04d5cca9c5faa423775382`
  （`/var/lib/memoria` 与 root-only `/var/backups/memoria` 一致）；四份 env 与两份 Nginx
  回滚副本均为 root-only。
- 更早 SQLite 快照 SHA-256：
  `9676292067df01121a7568b37b5f9ef5486296b7bd69703f7cbcdf76203ec006`
  （`/var/lib/memoria` 与 root-only `/var/backups/memoria` 两份一致）。
- runtime 回滚不自动恢复数据库；只有数据迁移或数据异常时才使用快照。

## 2026-07-27 清理结果

- 删除非交付 `apps/web`、`infra/Dockerfile.web` 与对应 pnpm workspace 文件，
  约减少 `5,183` 行 tracked 内容。
- 前序阶段已删除三个干净临时 worktree、已合并 hotfix 分支、旧发布工件、构建缓存、
  五套旧 source/runtime/image 和非交付客户端；累计回收约 `32 GiB`。
- 本次发布后已删除服务器 `2.2 GiB` incoming、未激活 H5 候选、过时临时候选 env、旧
  runtime/source/image `20260727-221555`；当前、直接回滚、数据库备份和两份切换证据均保留。
- 当日清理后的历史时点曾仅保留 `20260728-103318` 与 `20260727-235959` 两套 runtime/source/image；
  当前保留集以本文件“保留版本与回滚”和下节为准。
- 本机已删除本轮临时 worktree 与约 `2.1 GiB` 发布工件；未运行 Docker broad prune，也没有删除
  数据卷、数据库、其他项目镜像或用户未跟踪内容。
- 没有运行 `docker system prune -a`；没有删除卷、数据库、其他项目镜像或跨项目构建缓存。

## 2026-07-28：`20260728-123528` 发布后精确清理

- 已删除本机两套已完成发布的临时 artifact/worktree（`20260728-114049`、`20260728-123528`）和
  无容器引用的本机 image tags `20260727-221555`、`20260727-235959`、`20260728-103318`；保留当前
  `20260728-123528` 与直接回滚 `20260728-114049` 的四角色 tag。
- 已删除服务器已导入的两套 incoming/tmp 发布包、未激活 H5 候选、过时 source release
  `20260727-235959`/`20260728-103318` 及其八个 Docker image tags。服务器根分区由约 `37 GiB`
  已用降至 `28 GiB`（约 `86 GiB` 可用）。
- 没有删除当前 runtime、直接回滚、现网 H5、SQLite/环境备份、PostgreSQL/MinIO/Docker 卷或任何用户
  未跟踪文件；没有执行 `docker system prune`。本机其余 `7.0 GiB` Docker volumes 和约 `21.4 GiB`
  build cache 未逐项归属，明确保留，避免影响其他项目。

## 2026-07-28：`20260728-170236` 发布与精确清理

- runtime 已切换至 `20260728-170236`；H5 保持 `20260723-192611`，直接回滚 runtime 为
  `20260728-123528`。四容器、readiness、Provider、公网 API/H5/WMS、微信登录/头像路由及
  WSS 无效 ticket `4401` 验收通过。
- 发布前已创建 SQLite 双备份、四份 env 与 Nginx 配置备份；微信 AppSecret 只写入 root-only
  生产 env，独立身份 HMAC secret 已生成，secret 值未进入仓库或本文。
- 本机与服务器已删除本次临时 artifact/incoming、第三旧版本 source/image tag 和明确无用发布包；
  未运行 broad Docker prune，未删除 volumes、数据库、其他项目镜像或用户未跟踪文件。
- 小程序 `0.8.58` 已上传开发版本；手机号、昵称、头像、静默恢复、退出清理和登录后语音仍待真机验收。

## 验证

- Ruff：通过。
- `mypy services --strict`：175 个 source files 无问题。
- Python（含真实 PostgreSQL/pgvector、RLS、Skill、账户治理和恢复演练）：
  `1421 passed, 2 skipped`。
- 固定中文记忆评测集覆盖 13 类场景且无失败。当前规则基线：
  Extraction Precision `0.3947`、Recall `1.0000`、Recall@5/10 `0.1538`、
  nDCG@10 `0.1538`、Temporal Accuracy `1.0000`、Source Attribution `0.9333`、
  Cross-account/Candidate/Contradiction Leakage 均为 `0`。
- H5：`241/241`，production build 通过；现有 Chrome 的首页、回顾、个人、编辑和偏好交互
  已通过，未见 console warning/error。
- 微信小程序：`76/76`，全部 JavaScript syntax check 和共享 JSON 契约解析通过；此前
  开发者工具中的三个游客 Tab 与登录页已打开且无 console 异常；当前候选首页 WXML/WXSS
  及整页编译也已通过。`0.8.59` 已确认存在首播遥测兼容故障；诊断后的 `0.8.61` 已上传，
  生产回切 8443 后用户确认连接恢复。
- `scripts/run_e2e.py --profile offline`：通过。
- Agent Linux/amd64 镜像以 `--require-hashes` 成功构建；`vosk==0.3.45` 和控制词文件
  均进入镜像，官方模型通过宿主机只读挂载。
- `vosk-model-small-cn-0.22` 官方模型页标记 Apache-2.0；归档 SHA-256 为
  `3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba`。
  成品 Linux/amd64 Agent 镜像实测“停一下”命中、“等一下我想问……”拒绝。
  模型未进入仓库、镜像或发布包；生产安装路径为
  `/var/lib/memoria-agent/models/vosk-model-small-cn-0.22`，目录/文件权限为
  `0750/0640`，owner/group 为 `65532:65532`。
- commit-bound manifest/verifier、镜像导入、候选 H5/API/SQLite restart server smoke、SQLite
  双副本、四份 env 回滚、生产 Provider/readiness、公网 H5/API/WMS/TLS/WSS header smoke 均通过。
- 本次工件 SHA-256、镜像 ID、备份和生产验收见
  `docs/releases/20260728-170236.md`。
- 关键覆盖率子门槛：Agent orchestration `92%`、provider protocols `92%`。
- 既有全 `services` 覆盖率门槛仍未闭环：实测 `81.54%`，低于 CI 配置的 `85%`；
  本轮没有降低门槛或伪报通过。

## 未闭环与下一步

1. 记忆候选 `20260729-093337` 正在发布。完成前必须按现有发布流程备份 PostgreSQL/SQLite，
   验证 schema 升级、HNSW 创建耗时和回滚；不得把本地全绿写成生产已生效。
2. 当前规则检索的中文同义、人物别名和语义召回仍低，先以固定评测集改进 BM25/实体/向量
   融合，不得因单次演示切换 Mem0 或 TurboVec。Skill 自动执行需先建立安全的跨服务工具注册表，
   统一权限、generation fence、幂等和账户删除门禁。
3. 8443 回切后的主链已获用户确认；继续按
   `docs/acceptance/miniprogram-half-duplex-device-matrix.md` 完成 iPhone/Android、
   外放/听筒/蓝牙、Wi-Fi/移动网络/弱网、前后台和系统录音中断矩阵；上传成功不得冒充
   完整真机通过。后续每次记录 `ack_sent → ready_sent → first_playback → listening`
   的 session/timestamp，并复核游客三页、手机号、昵称/头像、静默恢复、退出清理与登录后语音。
4. 在明确测试窗口将 Gateway 临时设为 `MINIPROGRAM_GATEWAY_AEC_MODE=alternating`，记录
   `ready.aec` 分组、首字丢失、尾音误转写、失真、underflow 与 hard reset；结束后恢复
   `off`，没有真实 A/B 数据前不启用生产 AEC。
5. H5 仍需真实浏览器和设备验证“等等、等一下、停一下、先别说”等语意打断，
   同时覆盖“我等一下再说”等非打断语句，避免误触发。
6. 继续观察小程序 underflow、hard reset、lead 指标；只有需要定位非播放期噪声时才为
   单一 session 开启有界 AEC pre/post 采样，测试后立即关闭。
7. 单独处理全仓覆盖率门槛：优先补齐 PostgreSQL/外部边界测试，不通过降低标准换绿。

## 用户工作区边界

以下未跟踪内容属于用户，必须保留：

- `.workbuddy/`
- `apps/miniprogram/assets/bg/aurora-light.webp`
- `apps/miniprogram/assets/mascot-alpha.webp`
- `apps/miniprogram/design-preview/`
- `apps/miniprogram/package-lock.json`
- `apps/miniprogram/package.json`（仅本机上传辅助命令与依赖）
- `scripts/upload_miniprogram_test.js`
