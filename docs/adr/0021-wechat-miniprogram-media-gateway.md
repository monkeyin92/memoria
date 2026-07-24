---
status: accepted
date: 2026-07-24
---

# 微信原生小程序通过受限媒体网关接入既有 LiveKit 房间

## Context

Memoria 的当前正式语音主链是 Cascade：`FunASR Realtime → Qwen → Doubao Seed-TTS 2.0 → LiveKit/H5`。H5 使用 Browser LiveKit SDK 直接发布麦克风并订阅 Agent 音频；微信原生小程序没有可用的 `RTCPeerConnection`/MediaStream 兼容层，也没有公开、维护中的 LiveKit 小程序客户端可直接替代该 SDK。

把 `livekit-client` 通过 DOM polyfill 打进小程序不能补足 ICE、DTLS、SRTP、RTP/RTCP、Opus 和 AEC。把原生 H5 放入 web-view 虽然可避免媒体改造，但不满足原生小程序复刻和微信传播入口的目标。

## Decision

- 新增独立 `MiniProgramMediaGateway`：小程序通过 TLS WebSocket 发送/接收受限 PCM 帧；网关作为 LiveKit room 内的用户 participant 发布和订阅音频。
- Control API 对 `client.platform == "miniprogram"` 保持原有 session 冻结、账户与 ModePolicy 流程，但不返回 LiveKit URL/participant token；改为返回短期 gateway ticket 和 WSS 地址。
- gateway ticket 是短期、带 `issuer`、`audience`、`type`、会话、房间、用户 identity、agent name 和 `cascade` backend 的独立 HMAC JWT。ticket 不携带 LiveKit participant token，不写入 URL、access log 或应用日志。
- 网关以自身最小权限环境变量保留 LiveKit API key/secret，用 ticket 中经过验证的 room/identity 重新铸造短期 LiveKit participant token。小程序不能看到 LiveKit secret 或 participant token。
- 网关只桥接 `PCM16LE / 16 kHz / mono` 上行和 `PCM16LE / 24 kHz / mono` 下行；上行按 20 ms 重分帧，下行在小程序经 WebAudio 的有界 jitter buffer 播放。
- 网关只把 Agent `voice-agent.ui` data topic 和 LiveKit transcription 转发给小程序。它不接受小程序定义的 speaker/history/mode/generation/权限事件，也不创建第二套 Agent、ASR 或对话状态机。
- gateway ticket 可由已认证且仍拥有会话的用户刷新，用于 WebSocket 断线重连；刷新不会重建业务 session，也不会改变其已冻结的 mode、版本、关系或授权。
- 小程序初版仅支持 `cascade`；Qwen Omni 仍维持既有 H5 隔离 A/B 边界。
- H5 文件和 H5 会话响应保持不变；所有新增 response shape 仅在小程序 platform 分支返回。

## Consequences

- 语音业务后端和 LiveKit room 继续共用，避免复制 FunASR/Qwen/Doubao、UtteranceRouter、speaker policy 或 archive 历史逻辑。
- 增加一个媒体 hop，首音与端到端延迟会高于 H5 的直连 WebRTC 路径；必须在真机验收中量测。
- 小程序录音/播放期没有和浏览器等价的 AEC 控制，播放回灌是最大风险。Android 可优先请求 `voice_communication` 音频源；iOS 采用 `auto`，两者都不得把“接口调用成功”当作 AEC 通过。
- 发布工件增加网关镜像和最小权限 gateway env；release manifest、Nginx、Compose 和生产运行手册必须同步更新。
- 即使所有代码和 mock 测试通过，未经 iOS/Android、扬声器/听筒/蓝牙、弱网与后台切换的实机证据，不得对外宣传“全双工原生小程序已验收”。

## Alternatives considered

1. 直接使用 LiveKit 小程序 SDK：当前不存在满足“微信原生小程序、发布麦克风、订阅远端音频、加入任意 LiveKit room”的可验证实现。
2. 用 Taro/uni-app 将 Browser 或 React Native SDK 编译到微信小程序：它们不能提供微信运行时缺少的 WebRTC/native module。
3. `live-pusher/live-player` 加 LiveKit Ingress/Egress：RTMP/FLV 增加双向延迟，且缺 data topic/字幕/控制一致性，服务类目和组件权限也不适合作为交互式主链。
4. H5 web-view：可作为无原生语音能力时的降级方案，但不是本次原生客户端方案。

## References

- [`../research/livekit_wechat_miniprogram_client_research_zh.md`](../research/livekit_wechat_miniprogram_client_research_zh.md)
- [`0020-legacy-access-and-frozen-core.md`](0020-legacy-access-and-frozen-core.md)
- [`../production-deployment.md`](../production-deployment.md)
