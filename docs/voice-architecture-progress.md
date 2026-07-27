# 语音架构优化完成度

> 更新日期：2026-07-27
> 当前生产基线：runtime `20260727-221555`、H5 `20260723-192611`、小程序体验版 `0.8.52`
> 说明：本文件记录工程完成度；真实手机声学验收、外部账号权限和供应商控制台资源不以代码测试代替。

## 1. 总体结论

- H5 继续使用 LiveKit/WebRTC，媒体路线不变。
- 小程序当前正式链路仍是
  `RecorderManager → PCM/WSS → 服务端 APM → LiveKit Agent → PCM/WSS → WebAudio`。
- TRTC/Agora 尚未接入；它们仍是替换小程序媒体面的候选，不是当前生产事实。
- 本阶段先修复真实会话中已复现的重复确认语，并补齐客户端播放证据；没有在缺少腾讯
  RTC 权限、凭据和服务端媒体桥时伪造 TRTC 已完成。

## 2. 已完成

### 2.1 Agent 与控制面

- `UtteranceRouter` 统一 enroll、纯打断、打断后继续提问和普通聊天。
- generation fence、播放 reset barrier 和旧 generation 音频隔离。
- TargetSpeakerFocus、PlaybackInputGuard、语义歧义复核和迟到结果 fence。
- Vosk 独立短命令 KWS；KWS 只提供控制证据，不直接执行停止或写入历史。
- 同一 speech epoch 的打断确认语最多播放一次；迟到 ASR 终稿不再越过 4 秒冷却重复播放。

### 2.2 小程序媒体链路

- 上行 `16 kHz / mono / PCM16LE`，下行
  `24 kHz / mono / PCM16LE / 20 ms / 480 samples / 960 bytes`。
- Python/JavaScript 共用 `packages/contracts/miniprogram-media.json`。
- generation/reset barrier、固定帧校验、小缺帧 conceal、gain ramp 和短淡入淡出。
- 系统录音中断结束发送 `uplink_discontinuity`，网关重置半帧、sequence 和 APM 时序。
- 播放 underflow 只重建后续排程，不再硬停仍登记中的旧 source。
- 客户端将首播、underflow、hard reset 和 conceal 的有界数值指标经网关转发到既有
  `voice-agent.telemetry`，不上传音频、转写、token 或任意文本。
- 小程序 CI、JavaScript syntax check、协议 golden tests 和播放器行为测试。

### 2.3 后端与发布

- 独立 MiniProgramMediaGateway、短期 gateway ticket、最小权限 LiveKit participant。
- AEC 运行期 fail-closed 撤权与 Agent ACK。
- 指定单 session 的 AEC 前后 WAV 有界采样，默认关闭。
- 四角色 runtime 镜像、备份、回滚、readiness、Provider smoke 和发布清理流程。
- runtime `20260727-221555` 已完成生产部署；重复确认幂等和客户端播放诊断已上线。

## 3. 部分完成

| 项目 | 当前状态 | 剩余门槛 |
| --- | --- | --- |
| AEC | 服务端 APM、固定 delay、单 session 采样 | 客户端真实播放时间、动态 delay、路由变化校准、设备矩阵 |
| 播放器 | 固定帧、80 ms 聚合、underflow 安全 rebase、异常遥测 | 自适应 jitter buffer、长期时钟漂移校准、真机 underrun SLO |
| 上行可靠性 | 同步 send 失败不提交 sequence；中断恢复有 discontinuity | 单写入者有界队列、异步成功提交、队列溢出后的保会话恢复 |
| FunASR | 支持可选 `vocabulary_id` 与 `speech_noise_threshold` | 创建生产热词表、使用真机录音集校准后才能启用 |
| 协议 | v1 上行、generation v2 下行、共享 JSON 合同 | stream epoch、统一全局 sequence、route-change 等 v3 字段 |
| H5 | LiveKit/WebRTC 稳定并有测试 | 状态机、统一 transport、TypeScript strict、API 分域拆分 |
| 后端目录 | 领域模块和部署服务均可运行 | deployable apps 与 reusable packages 尚未物理重组 |
| 契约 | 小程序媒体合同已共享 | REST/UI/错误事件仍未全部从 Schema 生成 |

## 4. 未完成

- TRTC 或 Agora 小程序媒体 PoC、服务端媒体桥和生产迁移。
- iOS/Android 外放、听筒、蓝牙、弱网、前后台和系统录音中断完整矩阵。
- 客户端实际硬件播放时间与 AEC 前后音频的四路时间戳闭环。
- FunASR 生产热词表和噪声阈值校准。
- H5 God File 拆分：
  `App.jsx`、`useVoiceSession.js`、`api.js`、`QwenOmniWebRTCTransport.js`。
- 全端生成式契约和后端 `apps/packages` 目录重组。

## 5. TRTC 边界

TRTC 不是一个只替换小程序播放器的前端依赖。保留当前 Agent、FunASR、Qwen、Doubao、
LiveKit、权限、记忆和归档时，还需要一个可双向取送音频的 TRTC ↔ 现有媒体面桥。

当前仓库和运维配置未具备：

- TRTC `SDKAppID` 和服务端 UserSig 密钥引用；
- 已确认的企业小程序类目及 `live-pusher/live-player` 组件权限；
- 可把 TRTC 远端音频交给现有 Agent、并把 Agent PCM 发布回 TRTC 的服务端桥；
- 真机 AEC/ANS/AGC、弱网和音频路由对照数据。

因此下一步只能在取得上述外部条件后建立独立 PoC，不得直接把生产会话切到一个只能进房、
不能接入现有 Agent 的半成品。相关平台约束和候选路线见
`docs/research/wechat_miniprogram_duplex_audio_20260726.md`。

## 6. 下一阶段顺序

1. 用新遥测复测真机会话，确认重复确认语为 0，并取得 underflow/hard reset/lead 证据。
2. 为失败 session 定向开启 AEC 前后采样，区分采集、AEC 和 ASR 错误。
3. 基于录音集创建 FunASR 热词表并校准噪声阈值，不直接在生产猜参数。
4. 完成上行单写入者队列和协议 v3。
5. 取得腾讯 RTC 外部条件后做独立 TRTC PoC；对照通过后再决定媒体迁移。
6. 小程序媒体闭环后，再推进 H5 状态机/API 拆分和后端目录重组。
