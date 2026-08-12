# 项目交接

## 当前状态（2026-08-12）

### 小程序硬件管理与 ESP32-S3 Path 2（代码已提交并进入生产真机验收）

- 小程序保留首页与 AI 对话，主导航调整为“陪伴 / 设备 / 回顾 / 我的”；新增独立扫码启用页，
  串起二维码校验、真实 `wx` BLE 生命周期、Wi-Fi 表单、Claim、首次多主体初始化、Binding、
  Activation 查询和失败恢复。设备页成为硬件管理入口，绑定页只接受服务端确认的
  `claim_id + onboarding_session_id`，生产默认拒绝旧 `device_claim_token`。
- Wi-Fi 密码只存在页面内存和可清零缓冲区，隐藏、卸载或提交后清除，不进入 Storage、日志、
  二维码或后端。因小程序侧尚无经审计的 Protocomm Security 1/Protobuf codec，默认配网
  transport 在写凭据前 fail-closed；当前不能宣称微信真机 BLE 配网已完成。
- Device Fleet 新增独立 Bootstrap/Claim/Activation 子域和版本化合同：二维码及设备在线证明使用
  Ed25519，一次性 Claim 与 Identity 权威 Binding 通过可恢复 Saga 衔接，Activation Manifest
  按版本和设备单调计数 ACK。新增一次性 device media challenge/session，设备专用票据使用独立
  `aud/typ/secret` 并冻结 device、client、binding、cascade session 与 stream epoch；生产在
  PostgreSQL/RLS、托管签名密钥接入前显式 503，不回退 SQLite 或旧 DeviceRegistry 权威。
- `firmware/esp32` 固定 `xiaozhi-esp32 v2.4.2` commit
  `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5` 与 ESP-IDF `v6.0.2`，新增独立
  `memoria-atk-dnesp32s3-v1` 板型，保留 ES8388/ST7789/XL9555/按键并关闭 OV2640。新增独立
  `memoria_identity` NVS 分区、Ed25519 研发身份注入、Activation Manifest 验签/ACK、设备
  challenge/ticket/WSS 客户端和严格 `MemoriaAudioFrameV1`；上行 Opus 16 kHz/20 ms，下行
  24 kHz/20 ms。身份脚本只写 `0x10000..0x1ffff`，不会写 Secure Boot、Flash Encryption 或 eFuse。
- 新增独立 `services/device_media_gateway`，只负责设备票据、帧协议、Opus 与 LiveKit 桥接，复用
  现有 Mini Program LiveKit Bridge 和 Agent/ASR/LLM/TTS/Policy 控制链，不创建第二套媒体或身份
  权威。设备只有在真实播放队列排空后才发送 `playback.ended`；服务器生成/发送 TTS 不算已听到。
- `2026-08-11 18:29 CST` 已在真实正点原子板卡完成首次烧录：esptool 识别为 ESP32-S3
  rev `v0.2`、8 MB PSRAM、USB-Serial/JTAG，Bootloader、分区表、OTA 数据、资源和主应用全部
  写入并逐段通过 Hash 校验。自动复位后真实启动到 `wifi_configuring`，串口确认板型 SKU、8 MB
  PSRAM、LVGL/ST7789、ES8388、24 kHz I2S 和 SoftAP 均初始化成功；连续观察到 50 秒无 panic、
  看门狗或重启循环，也无摄像头初始化错误。串口监视已正常退出，未执行不可逆安全配置。
- 用户随后通过上游热点完成 Wi-Fi 配置，并用测试账号走通 `xiaozhi.me` 激活和真实硬件对话；
  这是 Path 1 的上游验收，不作为 Memoria 闭环证据。
- `2026-08-11 22:32 CST` 已把 Path 2 最终固件写入同一实板并逐段通过 Hash 校验。真机重启后从
  独立 NVS 读取研发身份，Activation Manifest 验签成功并进入 `idle`；短按后真实完成 Control API
  media challenge、media session 和 WSS 握手。网关随后因本机没有 LiveKit（`127.0.0.1:7880`
  refused）以服务器错误关闭，所以只验收到真实设备进入 Memoria 媒体边界，尚未完成 Memoria 对话。
- WSS 关闭回调曾暴露高/低优先级任务销毁竞态并造成 `StoreProhibited`；已按连接生命周期修复，
  最终固件连续短按两次均约 1 秒提示“设备媒体服务不可用”并回到待机，持续观察无 panic、重启或
  堆继续下降。已刷合并包 `9,873,069` bytes，SHA-256
  `29ab9cf4825099aa586a007aa03c97a786c2d16daad54911dfc96b665cca096f`。
- `2026-08-12` 已将五个 Path 2 运行时服务部署到生产。实板 Ed25519 身份、media challenge、
  media session、TURN 身份、设备 WSS `session.ready` 和 Agent `unknown_safe` 策略验签均已通过；
  未确认说话人只允许普通对话，主人称呼、私人记忆、历史、学习、工具和个性化声线保持关闭。
- 真实欢迎语暴露出内部 Agent/小程序 `generation_id=0` 与设备协议“0 表示尚无播放代次”的边界
  冲突。修复统一放在设备媒体网关：所有内部代次映射为设备代次 `N+1`，设备播放回执再映射回
  `N`；没有改 Agent 的全局 generation fence，也没有为欢迎语增加旁路特判。
- `20260812-123053` 已修复已登记但无需下发硬件的 Agent UI 遥测导致 WSS 4400；生产容器与
  readiness 已验收。后续仿真确认连接不再被 UI 事件断开，但欢迎语 PCM 因固定 `session.say`
  未先发布 generation-bound `speaking` 而被共享桥接安全丢弃。该状态合同修复随下一 superseding
  release 发布；在设备签名公网仿真和实板麦克风/扬声器闭环通过前，不把 Path 2 记为语音验收完成。
- 当前这块研发板的生产 authority 身份与绑定是人工受控投影；后续新设备的“小程序扫码 → Claim
  → Binding → Activation”自动 Saga 尚未完成微信真机和生产批量验收，不能据此宣称新设备已能
  零人工自动接入。
- 回滚保留在被忽略目录：Path 1 整包
  `firmware/esp32/artifacts/backups/pre-path2-upstream-working-merged.bin`，SHA-256
  `ea3b37904e42c42a8334b9808871e8bebff2f72f9ed02dbc4000ec35fdf1e250`；切换前启动/NVS/OTA 区备份
  SHA-256 `956c727accb33f1718be569a01275fc4f67627a77d8d258bfcb8816d2b13f77e`。
- 最终本地证据：相关 Python 纵切 `234` 项、小程序 `193/193`、Ruff、mypy、干净 upstream
  overlay 重放、ESP-IDF clean build/merge-bin 通过。仍缺微信 iOS/Android Protocomm、生产
  PostgreSQL/RLS/托管签名密钥、真实 LiveKit/Agent/provider 部署后的 Memoria 对话，以及完整
  LCD/麦克风/扬声器声学、AEC/全双工和量产安全验收；本轮临时 Control API/设备网关已在验收后
  正常关闭，本地 SQLite 数据不是生产发布。

### 多主体整改本地工作区（未提交、未推送、未部署）

- 《Memoria 多用户场景产品策略与架构开发调整方案》PR-01~PR-17 的主体、
  绑定、关系、Runtime Profile、Session epoch、Policy V2、Memory Scope、Tutor、
  Notification、Device Fleet 与 PostgreSQL RLS 软件主体已落入当前 dirty worktree。
- 本轮完成 Identity FORCE RLS、Guardian 核心表 actor/subject RLS、Policy
  nullable-subject receipt scope、Session action subject fence、Notification 写入 actor
  防伪与主体本人读取语义、Device Fleet actor fence，以及 Agent action-policy
  装配；Agent 策略装配与固定播报控制已从 `agent.py` 抽离，模块预算收紧到 `3413`。
- PR-10/12/14 的仓库软件闭环已补齐：Agent 通过生产 HTTP
  `TransactionalToolEffectCommitPort` 提交/对账 Session Runtime 持久化 intent/outbox；
  Memory capture 由 Control 生产 authority 装配和 Archive canonical evidence projector
  驱动同事务写入；家庭共享由生产 executor 完成不同主体 propose/confirm/promotion、
  object/withdraw 与回滚。缺配置或 authority 时仍 fail-closed，不回退 legacy 写入。
- 本地证据：全部服务测试目录与所有真实 PostgreSQL 合约文件通过（pgvector 专用
  文件使用 pgvector 0.8.1/PostgreSQL 17，其余使用 PostgreSQL 16）；strict mypy
  `380` 个源码文件通过；H5 `372` 项测试和 production build、小程序 `180` 项测试和
  JS 语法检查通过；scripts 测试、canonical 合同、模块预算、Ruff、compileall 与
  `git diff --check` 通过。以上均不是生产迁移、外部副作用送达、真实通知或真机证据。
- 详细状态以 `docs/architecture/multi-subject-pr-plan.md` 的
  “2026-08-11 本地实现状态”为准；不再新建平行会话文档。

### 学生线本地工作区（未提交、未部署、未发布）

- `implementation-plan-20260808-student-first-remediation.md` 的 P0、P1 仓库能力、P2 导师域和 P3
  本地软件链路已实现并逐项回填；外部/生产门禁仍保持未完成，不能据此创建对外学生体验版。
- 未成年账号能力由 `account_gate.py` 单点 fail-closed；guardian 绑定/孩子确认/分项同意/撤销、
  tutor focus/Router/进度投影、周报、危机固定回复 + Evidence/outbox、授权儿童语料限额/到期删除均已接线。
- guardian/tutor PostgreSQL FORCE RLS schema 与 forward-only 升级脚本、WAL/base backup、异地对象镜像
  和独立恢复演练脚本已进入仓库；这些只证明可执行能力，不证明生产已安装、远端持续上传或完成恢复。
- CI 等价本地 PostgreSQL 环境为 `2108 passed, 2 skipped`（两项为需显式 CAM++ 模型/官方 WAV 的
  真实 ONNX 测试），总覆盖率 `87.0985%`，编排层 `90%`、
  provider 协议层 `97%`；ruff、strict mypy、module budget、tutor E2E、H5 304 项、小程序 90 项与
  Node 24 的 87 文件 upload dry-run 均通过。
- 仍阻塞发布：PIA/法务/算法备案确认、危机话术专业评审、真实微信订阅消息、生产 guardian 升级、
  真实异地备份与独立恢复报告、iOS/Android 完整语音链、ESP32/AEC、200 条真实授权儿童语料。

### 当前生产基线

- 生产 runtime 为 `20260812-123053`，源码 commit
  `310f2bfe2dc29cde056c2cca3e18f9cd568f4ca9`；H5 有意保持 `20260808-171749`，本轮硬件修复不
  切 H5。直接 runtime 回滚目标为 `20260812-114447`。完整证据见
  `docs/releases/20260812-123053.md`。
- Agent、Control API、Speaker Model、小程序 Gateway、Device Media Gateway 五个应用容器以及
  PostgreSQL/Redis/MinIO 均 healthy；readiness 已绑定 runtime tag，LiveKit/Agent 权威语音链保持
  复用。Runtime Profile 验签键已在生产 Agent 以 root-only 最小权限临时接通，正式生成器修复随
  `20260812-114447` 已由正式环境生成器逻辑和最小权限拆分测试固化。
- PostgreSQL 已 forward-only 安装独立 `memoria_evolution` 角色、8 张表、8/8 FORCE RLS 与 8/8
  controller policy。不要为代码回滚删除这些对象；旧 runtime 可与 additive schema 共存。
- H5 已最后切流；240 个 immutable URL 全部 HTTPS 200，历史 4776 条资源引用均可用。公网正向路由
  为 200，internal/PocketSparks/Goods Invoice 为 404；390x844 与 667x375 浏览器无横向溢出、
  console warning/error 为 0。
- 小程序 `0.8.66` 已于 `2026-08-08 19:21 CST` 经产品负责人授权使用微信开发者工具官方 CLI
  上传成功；AppID `wx20a3a044b52fcbb7`，包体 `656904 bytes（641.5 KB）`，说明为
  “绑定助手表情完整话轮 fence”。只创建开发/体验版，未提审、未正式发布。
- 项目内 `upload:test` 仍是长期可复现链路：Node 25/Web Storage 问题已 fail-fast，Node 24.16
  compile-only 通过；真实 CI 上传仍需把 `112.20.18.77` 加入微信代码上传 IP 白名单。本次 DevTools
  上传来自干净远端一致 HEAD `05a933f`，小程序业务源码相对 runtime tag `9812fac` 无变化。

## 回滚与备份

- 可执行回滚 receipt：`/var/backups/memoria/rollback-20260808-171749.env`，`root:root 0600`；它由
  上线前冻结的 legacy receipt 安全转换，未从切流后的 `current` 反推旧版本。原
  `release-state-pre-20260808-171749` 继续保留。
- H5 回滚 snapshot：`/var/www/memoria-releases/rollback-20260808-171749`，使用旧入口/provenance
  与当前 240 个 immutable assets；manifest SHA-256：
  `4f37ea9952463ef556aaf28e241c9d5244acf6a1236a46f49a5b968702890169`，逐文件验签与
  `www-data` 可读门禁通过。
- SQLite 双副本 SHA-256：`6443bde9695697296413ed9d7486b744bc52bc52162670d158529bbf4d1577af`。
- PostgreSQL dump SHA-256：`209b09028fdc31be2f2d5d8763b3fe456d34a625c8c43777827c815875cb3fdf`；
  MinIO 对象清单 SHA-256：`c7e273671460428a5fa9183841134a2eae6ded9814c0aeffab3fb6d5ee6155ff`。
- 旧 PostgreSQL/Control/Agent/Speaker/Gateway env 均已 root-only 备份。Runtime 故障先恢复旧 data
  Compose 与全部 env，再切旧软链和四应用；H5-only 故障只切 H5。

## 仍需完成

- 多主体能力上线前必须先做生产备份和 forward-only schema dry-run，配置并验证
  Policy/Session/Memory 内部 token、Redis/outbox、角色权限与 readiness，再执行回滚演练；
  当前 dirty worktree 未提交、未推送、未部署，线上仍是旧基线。
- `TransactionalToolEffectCommitPort` 已保证本地持久化、幂等和 worker 领取合同，但仓库
  当前没有具体第三方业务工具/投递 worker；只有在明确业务动作和供应商后才能实现并验收
  外部 delivery，不能把 outbox completed 或单测通过表述为第三方副作用已发生。
- 先完成学生线全部外部门禁并把证据写入 `docs/acceptance/` / `docs/releases/`；当前危机通知只有本地
  outbox 与家长页提醒，不能宣称微信订阅消息已送达，也不能用自动化代替真实 iOS/Android 声学验收。
- 在任何学生数据进入生产前，先执行 guardian forward-only 升级，配置真实远端备份 endpoint，验证
  WAL/base backup 与关键 bucket 持续同步，并在独立主机完成恢复演练；仓库脚本通过不是生产证据。
- 后续为恢复项目脚本真实上传，可把 `112.20.18.77` 加入微信代码上传 IP 白名单；这不再阻塞
  `0.8.66` 体验版交付。
- `0.8.66` 仍需在真实 iOS/Android 上覆盖麦克风、扬声器/AEC、弱网、前后台与蓝牙；H5 仍需真实
  登录后语音验收。现有单测、浏览器、WSS、Provider smoke 或上传成功都不能替代。
- 成功上传并确认回滚工件后，精确清理旧 incoming 大归档；保留当前 `20260808-171749` basis、
  `20260807-163916` runtime/H5/镜像以及全部备份。
