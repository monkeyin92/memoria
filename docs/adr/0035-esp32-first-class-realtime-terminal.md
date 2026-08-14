# ADR-0035：ESP32 一等实时语音终端与小程序控制面

- 状态：已接受（目标架构；分阶段实施）
- 日期：2026-08-13
- 适用范围：微信小程序、ESP32-S3、Control API、Go Media Edge、Python Voice Core/Agent
- 实施基线：`Memoria_ESP32一等语音终端与小程序控制面全双工整改方案_2026-08-13.md`

## 决策

产品与运行时边界固定为：

```text
微信小程序 = 初始化、绑定、Persona/Policy、设备管理、记录与总结的控制面
ESP32-S3   = 唯一面向用户的实时麦克风、扬声器与物理交互终端
Control API = 身份、主体、策略、Runtime Profile、短期媒体票据与持久化投影权威
Go Media Edge = 设备 WSS、Opus/PCM、Sample Clock、Epoch、Generation Gate 与背压权威
Python Voice Core/Agent = ASR、话轮、打断语义、Generation、LLM/TTS、Memory/Persona 权威
```

新硬件媒体路径为：

```text
ESP32 WSS + MemoriaAudioFrameV1
  → Go Media Edge
  → memoria.media.v1.VoiceMediaBridge.Connect (mTLS gRPC)
  → Python Voice Core/Agent
```

Go 只接管硬件媒体边界，不接管交互决策。`InteractionAuthority` 必须保持
`PYTHON_AUTHORITATIVE`；本 ADR 不解冻 ADR-0032 禁止的 Go 对话权威迁移。

## 产品约束

小程序生产包不得包含或调用实时语音能力：

- 不申请 `scope.record`；
- 不调用 `wx.getRecorderManager`；
- 不连接 Mini Program Media Gateway、设备媒体 WSS 或 LiveKit；
- 不播放实时 TTS，不参与 VAD、打断、Generation 或播放 Gate；
- 只通过 HTTPS 读取服务端权威状态、记录和总结。

ESP32 不保存用户 Access Token、Provider 永久密钥、完整 Persona Prompt 或长期记忆。设备完成
Activation 后用设备身份申请一次性 challenge 与短期媒体票据，小程序退出或换网不能影响现有
机器人会话。

## 权威与 Fence

| 事实 | 唯一权威 |
|---|---|
| 采集样本、DAC 播放水位、物理按钮/静音 | ESP32 |
| 网络顺序、Opus/PCM、`stream_epoch`、背压 | Go Media Edge |
| `turn_id + generation_id + tool_epoch`、打断与取消 | Python Voice Core |
| 身份、主体、Persona、Policy、Memory 权限 | Control/Policy |
| 实际听到的文本 | ESP32 watermark + Python Playback Ledger |

新会话的 `generation_id` 从 1 开始，0 只表示没有有效 Generation。新直连路径不得做
`Agent N ↔ Device N+1` 映射。所有下行音频、播放回执、取消、字幕和表情都必须携带并校验
完整 fence；重连必须增加 `stream_epoch`，清队列不能代替 Epoch/Generation 推进。

## 打断等级

- L0：物理按钮在设备本地 flush，同时向 Edge/Core 关闭旧 Generation；
- L1：离线停止词本地 flush，不等待云端 ASR；
- L2：经已校准 AEC 的语音先形成 candidate/duck，由共享 `InterruptionPolicy` 区分
  backchannel、误触发和 true interrupt；
- L3：完成多音量、距离、方位、噪声及成人/儿童/老人双讲矩阵后，才允许标记
  `full_duplex_verified`。

`VAD` 只是声学观察，不能直接等同于 interrupt。无 AEC 实板证据时运行模式必须降级为
`interrupt_assist` 或 `half_duplex_safe`，产品不得宣传全双工。

## 兼容与回滚

Python `services/device_media_gateway` 暂时保留为 `livekit_compat` 回滚路径：

- 只允许安全、故障和回滚可用性修复；
- 不增加业务字段或新的状态权威；
- 不作为新设备生产默认；
- Direct path 完成 Canary、回滚演练与真实硬件验收后再删除；
- 回滚不得撤销 additive 数据库 schema，也不得复活旧 Epoch/Generation。

## 状态与发布门禁

每项能力必须分别标记 `code`、`wired`、`enabled`、`verified`。本地单测、WSS 握手、模拟
Playback ACK、上游小智对话或配置值都不能替代真实设备、真实 Provider、生产接线和 DAC/AEC
证据。权威状态记录在根目录 `architecture-status.yaml`，当前生产事实与回滚入口记录在
`HANDOFF.md`。

出现以下任一情况必须拒绝发布：旧 Generation 误播、旧 Epoch 复活、跨主体私人上下文泄漏、
未播放文本进入已听历史、小程序关闭导致设备会话停止、回声进入永久记忆，或无声学证据却启用
`full_duplex_verified`。
