---
status: accepted
date: 2026-07-28
---

# Hugging Face speech-to-speech 只作为边界设计参考

> **架构更新（2026-08-16）**：本 ADR 中小程序受控媒体适配器的描述只保留为历史语境。
> 当前实时终端边界由 [ADR-0035](0035-esp32-first-class-realtime-terminal.md) 定义：H5 继续使用
> LiveKit，ESP32 使用 Direct Device WSS；小程序只承担控制面。本 ADR 关于不引入第二套
> pipeline、统一取消与 Handler 边界的决策仍有效。

## Context

Hugging Face `speech-to-speech` 提供模块化实时语音流水线，将 VAD、STT、LLM、TTS、
传输和取消拆成独立 Handler/队列，并提供 WebSocket/Realtime 与 WebRTC 接入方式。

Memoria 已有生产级业务与语音控制面：

- H5 使用 LiveKit/WebRTC；
- 小程序使用受控半双工 PCM/WSS 媒体适配器；
- ASR、LLM、TTS 使用远端 API；
- `DuplexRuntime`、`UtteranceRouter`、`GenerationFence`、实际已听文本、权限、记忆与工具
  已贯穿现有链路。

整体引入另一个 pipeline runtime 会形成第二套取消、队列、会话和媒体生命周期；替换
LiveKit 也会重新承担信令、TURN、重连和浏览器设备兼容，而不会自动提升现有 API 模型的
识别率、回答质量或音质。

## Decision

只吸收四类设计：

1. 用统一构造 seam 隔离 ASR/LLM/TTS 供应商初始化；编排层直接调用的 LLM、TTS 使用窄
   Handler/Protocol，现有 callable 通过 adapter 兼容。ASR 仍实现 LiveKit 的 `STT`
   adapter，不再复制一层无消费者的本地 Protocol。
2. 新增不可变 `CancellationContext`，但它只包装现有完整 `GenerationFence`，不维护第二个
   generation counter。
3. 权威字幕使用 `turn_id + turn_revision`；同一 fenced turn 内 revision 单调递增，首个
   final 关闭该修订流，避免 UI 最终稿与长期历史分叉。客户端拒绝迟到旧 revision，并兼容
   滚动发布期间的旧版无 revision 事件。
4. 提供最小 OpenAI Realtime-style facade，只映射 `session.update`、文本
   `conversation.item.create`、`response.cancel` 和既有 UI 事件；音频继续由 LiveKit 或
   MiniProgramMediaGateway 承载，facade 不拥有第二套 transport 或 pipeline。

明确不引入：

- Hugging Face `speech-to-speech` runtime 或其线程/队列生命周期；
- 本地 Whisper、Parakeet、Qwen3-TTS、Kokoro 等模型；
- 其 WebRTC transport 替换 LiveKit；
- 与 Memoria `GenerationFence` 平行的 CancelScope；
- 第二套会话上下文、工具执行、记忆或权限系统。

## Consequences

- Provider 边界和取消语义更清楚，但生产主链、供应商 API 和媒体拓扑不变。
- Realtime facade 是兼容层，不是新的产品入口；在有明确客户端需求前不扩展协议内音频。
- `turn_revision` 成为共享事件契约的一部分；新 runtime 发出正整数 revision，H5 和小程序
  在滚动发布时仍可显示旧 runtime 的无 revision 连续字幕。
- 未来若某个 HF Handler 或 transport 有可量化优势，应以独立 PoC 比较延迟、取消、实际已听
  文本、成本和运维复杂度，不得直接替换生产 runtime。

## Alternatives considered

1. 整体接入 HF runtime：拒绝。与现有编排、取消和媒体生命周期重复。
2. 用 HF WebRTC 替换 LiveKit：拒绝。重新承担已由 LiveKit 解决的生产媒体问题。
3. 将本地 STT/TTS 模型带入生产：拒绝。当前约束是继续使用远端 API，也没有新的 GPU
   运维与模型验收预算。
4. 完全不借鉴：拒绝。Handler 边界、统一取消上下文、话轮修订和兼容协议能在不换 runtime
   的情况下直接降低现有耦合。

## References

- https://github.com/huggingface/speech-to-speech
- [`0025-controlled-miniprogram-turns-and-h5-semantic-barge-in.md`](0025-controlled-miniprogram-turns-and-h5-semantic-barge-in.md)
- [`0035-esp32-first-class-realtime-terminal.md`](0035-esp32-first-class-realtime-terminal.md)
