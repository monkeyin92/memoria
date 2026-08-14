# ADR-0034：设备 Bootstrap、认领与 ESP32-S3 客户端边界

- 状态：已接受（仓库软件基线）
- 日期：2026-08-11
- 适用范围：微信小程序首次启用、Device Fleet 未认领设备、正点原子 ATK-DNESP32S3 V1 固件

> 2026-08-13 更新：本文的 Bootstrap、Claim、Binding、Activation 与设备身份决策继续有效；
> “设备媒体路径（第 2 条）”中经 `MiniProgramLiveKitBridge` 的链路已由 ADR-0035 降为显式
> `livekit_compat` 回滚路径。新硬件默认目标是 Device WSS → Go Media Edge → `media-v1` →
> Python Voice Core，代码存在、生产接线、功能启用和真机验证必须分别记录。

## 决策

设备首次启用拆成五个互相独立、由服务端状态推进的阶段：

```text
签名二维码识别
→ BLE 近场与 Wi-Fi 配网
→ 设备签名在线证明
→ 当前账号一次性 Claim Reservation
→ 多主体 Binding 与设备 Activation ACK
```

任何较早阶段都不能替代较晚阶段。扫码、BLE 连接、Wi-Fi 成功或设备在线均不单独产生所有权。

### 二维码与设备身份

- 二维码格式固定为
  `memoria-bootstrap:v1:<base64url(canonical-json)>.<base64url(ed25519-signature)>`。
- 设备私钥签名；Device Fleet 中预登记的制造公钥验签。
- 二维码会话 TTL 为 15 分钟；设备进入配网模式或主动刷新时生成新的
  `bootstrap_nonce` 与高熵 PoP，旧会话失效。
- 二维码不携带 Wi-Fi 凭证、用户令牌、Provider Key 或长期设备凭证。
- 单板研发身份与量产身份隔离。研发脚本可以生成并烧录唯一密钥，但量产密钥签发、HSM、Secure Boot 与 eFuse 永久写入不在本轮自动执行。

### BLE 与 Wi-Fi

- 目标协议为 ESP-IDF `network_provisioning` + NimBLE + Protocomm Security 1，PoP 每个 Bootstrap Session 唯一；Security 0 禁止进入外部测试。
- 微信小程序不能调用原生 Espressif Provisioning SDK。小程序 BLE Adapter 负责发现、连接、分片、Notify、attempt epoch 和资源清理；密码只存在页面实例内存。
- 在经过测试向量验证的 Protocomm Security 1 JavaScript 实现落地前，客户端必须 fail closed，不能回退到明文 GATT 或把“连接成功”显示成“安全会话成功”。
- Wi-Fi 密码不得进入 HTTP API、Storage、日志、埋点、错误对象或二维码。重新配网只清网络配置，不解除账号绑定。

### 在线证明、Claim 与 Activation

- 设备在线证明使用一次性 challenge、Ed25519 签名、二维码 nonce 哈希、手机 nonce 哈希和单调计数器；challenge 只能消费一次。
- Claim Reservation 绑定当前登录 actor、设备和 onboarding session，TTL 为 10 分钟；同一设备同时只能有一个有效 Claim，相同幂等键返回同一结果。
- `/v1/device-bindings` 的正式路径消费 `claim_id + onboarding_session_id`。旧
  `device_claim_token` 只允许 `OFFLINE_MOCK` 测试兼容，生产不得开启。
- Device Fleet 与 Identity 使用可恢复 Saga，不伪装成跨库强事务。
- Activation Manifest 使用 Ed25519 签名并绑定 device、binding/version、配置哈希和版本；只有设备签名 ACK 通过后，小程序才可显示“可直接对话”。

## 客户端定位

小程序长期定位为设备设置、管理、状态和权威对话信息查看端，不进入机器人日常媒体路径。本轮保留现有首页 AI 对话和回顾入口，新增设备主入口；后续硬件体验稳定后再单独评审是否移除手机对话。

机器人完成激活后，应使用设备身份直接连接 Memoria Control/Media Edge。它不得保存用户 Access Token，现有 Agent、ASR、LLM、TTS、记忆、Policy 与多主体 Runtime Profile 继续保持权威。

## 设备媒体路径（第 2 条，历史兼容路径）

本节保留 2026-08-11 已上线链路的历史合同与回滚依据，不再是新硬件目标架构。除安全、
生产故障和回滚可用性修复外，不得继续向该路径增加业务能力；目标路径见 ADR-0035。

ESP32 不复用小程序 bearer，也不继续使用旧 `DeviceRegistry` 作为第二套身份权威。
完成 Activation ACK 后，设备按以下固定控制链建立媒体会话：

```text
Device Fleet 制造公钥
→ 一次性 media challenge + Ed25519 proof
→ Control API 冻结绑定主体与 cascade session
→ 独立 audience/type 的 5 分钟 device media ticket
→ GET /v1/device/media
→ Device WSS/Opus Adapter
→ 现有 Mini Program LiveKit Bridge / Agent
```

- 新设备接口使用 `POST /v1/devices/{device_id}/media-challenge` 与
  `POST /v1/devices/{device_id}/media-sessions`；只有 `BOUND` 且最新 Activation 为
  `ready_for_conversation` 的设备可调用。
- `media-challenge` 同时要求证书头和稳定 `X-Client-ID`；`client_id` 进入设备签名的
  canonical payload、短期票据和 WSS `Client-Id` 头，任一处不一致均拒绝连接。
- Challenge 只消费一次、带过期时间并签名精确 canonical JSON。媒体票据与小程序票据使用
  不同 `aud`/`typ`，绑定 `device_id`、`binding_id/version`、`session_id` 和
  `stream_epoch`，两类客户端不能互换票据。
- WSS 首版采用严格 `MemoriaAudioFrameV1` 网络字节序头，必须验证
  `stream_epoch`、连续 `sequence`、`sample_start`、`frame_samples` 和方向；不接受小智
  raw Opus v1。
- 上行固定 Opus mono 16 kHz/20 ms，下行固定 Opus mono 24 kHz/20 ms。Adapter 只负责
  协议、转码和 LiveKit 桥接，不新建 ASR/LLM/TTS/记忆控制链。
- 设备上报的 `playback.started/progress/ended/error` 才是实际播放证据；服务端生成或发送
  TTS 不等于用户已经听到。所有回执必须受 `stream_epoch + generation_id` fence 约束。
- 旧 `/v1/devices/{id}/identity`、旧 challenge 和单数 `media-session` 仅作为显式开发兼容
  路径保留，Memoria 固件不得调用；生产配置不得从 Device Fleet 失败回退到旧 registry。

### 研发板身份与回滚

- Flash `0x10000..0x1ffff` 的既有空洞划为独立 `memoria_identity` NVS 分区，不覆盖上游
  Wi-Fi NVS；研发脚本只烧录该分区。
- 分区保存 `device_id`、`certificate_id`、Ed25519 seed、Activation 验签公钥和 Control API
  base URL。私钥只生成到权限 `0600` 的被忽略文件，不写源码、日志或构建产物。
- 当前开发板不自动写 Secure Boot、Flash Encryption 或 eFuse。量产前以托管制造签发、
  NVS/Flash 加密和防回滚门禁替换研发注入流程。
- 每次切换第 2 条路径前，保留当前已验收 merged image，并读取启动区、分区表、Wi-Fi NVS
  与 OTA 元数据，形成可执行的一步回滚。

## 固件基线

- 上游固定为 `78/xiaozhi-esp32` `v2.4.2`，commit
  `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`。
- 工具链固定为 ESP-IDF `v6.0.2`。
- 自定义板型为 `memoria-atk-dnesp32s3-v1`，保留 ES8388、XL9555、ST7789、BOOT 与 LED，删除无摄像头硬件上的所有 OV2640/EspVideo 初始化和构建开关。
- 主仓库保存上游锁、可审查 overlay 和构建/烧录脚本，不提交上游整仓、managed components、工具链或含设备私钥的生成物。

## 持久化与发布边界

SQLite Device Onboarding Store 只用于本地开发和单进程测试。生产固定使用独立
`memoria_device_onboarding_api` / `memoria_device_onboarding_maintenance` 登录角色、FORCE RLS 和
独立 Activation Ed25519 seed；schema 由 root-only forward-only 数据升级安装，Control 运行时不持有
维护 DSN。生产缺少该 authority 或签名材料时必须启动失败，不得回退 SQLite。

仓库测试和 clean firmware build 只能证明软件可构建。以下证据必须在实物接入后补齐：

- 串口识别与真实 flash；
- LCD 方向/颜色、Flash/PSRAM、BOOT/LED；
- ES8388 麦克风 RMS 与低音量扬声器；
- iOS/Android 微信 BLE、2.4 GHz Wi-Fi、切后台和断电恢复；
- 小程序关闭后的设备独立语音与真实声学半双工。
