---
status: superseded
superseded_by: 0035-esp32-first-class-realtime-terminal
retained_for: historical release record
date: 2026-07-27
---

# 小程序采用受控话轮，H5 保持语意打断

> **状态说明（2026-08-16）**：本 ADR 记录的是 2026-07 的小程序受控媒体策略，已被
> [ADR-0035：ESP32 一等实时语音终端与小程序控制面](0035-esp32-first-class-realtime-terminal.md)
> 取代。小程序不再录音、播放实时媒体或连接 Gateway；下文仅保留历史发布语境，不能作为
> 当前产品、部署或新功能的依据。

## Context

微信小程序当前使用
`RecorderManager → PCM/WSS → 服务端 APM → LiveKit Agent → PCM/WSS → WebAudio`。
真实设备测试仍出现播放卡顿、滋滋声、回声导致的识别错误，以及“等等、停一下”等短命令
不稳定。服务端无法准确获得手机扬声器的硬件播放时钟；继续让录音和外放并行，会把 AEC、
ASR、播放抖动和打断策略耦合在同一条链路。

TRTC/Agora 可以把采集、播放、AEC、ANS、AGC 和弱网处理放到设备侧统一媒体时钟，但需要
付费账号、平台权限、凭据、RTC 到现有 Agent 的双向媒体桥和真机验证。当前产品不接受这项
成本，也不要求小程序必须随时打断。

H5 已使用 LiveKit/WebRTC，浏览器媒体层具备设备侧回声处理和稳定时钟，现有
`UtteranceRouter`、控制语意规则和 generation fence 已支持“等等、等一下、停一下、
先别说”等表达及控制后继续提问。

## Decision

- 小程序改为受控话轮。收到 `thinking / thinking_silent / tool_waiting / speaking /
  interruption_pending / recovering` 时暂停录音上行。
- Agent 状态恢复到 listening 不是单独的开麦条件；客户端还必须等待本地
  `PcmJitterPlayer` 的 pending 音频和全部已排程 source 结束。
- 用户手动静音是更高优先级意图，自动恢复不得重新打开已被用户关闭的麦克风。
- 删除小程序播放期录音、口头打断和旧的“轻触即进入下一语音话轮”路径。保留独立的
  “停止播放”按钮：本地平滑清空播放器，再调用现有 Control API 推进 generation；
  尾音保护结束且服务端重新允许输入后才恢复录音。
- MiniProgramMediaGateway 无论 AEC 是否可用都给 Agent 标记小程序平台。Agent 对该平台
  关闭 LiveKit interruption、KWS、歧义打断模型和播放期转写接纳；迟到终稿按原被禁
  speech epoch 丢弃。
- H5 保持可打断。Cascade 默认继续使用服务端 `UtteranceRouter` 的控制语意；Omni 备选
  transport 保留同类 intent classifier。小程序策略不得改变 H5 的默认
  `barge_in_enabled=true`。
- TRTC/Agora 保留为将来“重新要求小程序外放全双工”时的独立 PoC，不是当前功能前置。

## Consequences

- 小程序不能用口头命令边说边打断，但可以用“停止播放”按钮提前结束当前回答；按钮不会
  同时打开麦克风或恢复全双工。
- 播放期不再持续向服务端发送麦克风 PCM，显著降低助手回声进入 ASR、误触发下一轮和
  AEC 错配的概率。
- 本决策不能消除弱网、播放器 underrun 或非播放期录音噪声；这些仍需真机指标和录音样本
  单独治理。
- H5 的交互能力不降级，仍可按控制语意打断；两端共享业务与智能层，但媒体交互策略明确
  分离。
- 如果产品未来恢复小程序随时打断，必须以 RTC 媒体桥和真机对照重新立项，不能只恢复旧
  按钮或重新开启服务端 KWS。

## Alternatives considered

1. 继续修补小程序 raw PCM 全双工：拒绝。缺少设备硬件播放时钟，无法稳定闭合 AEC 与
   播放排程。
2. 立即接入 TRTC/Agora：暂缓。当前缺少付费账号、平台权限、服务端桥和验收资源。
3. 改为按住说话：保留为更强约束的降级方案；当前自动受控话轮已能减少一次操作。
4. 同时关闭 H5 打断：拒绝。H5 的 WebRTC 媒体基础不同，现有能力和用户体验无需降级。
5. 小程序完全不提供停止按钮：拒绝。长回答无法退出会显著放大半双工等待感，而按钮停止
   不依赖 RTC、AEC 或播放期录音。

## References

- [`0021-wechat-miniprogram-media-gateway.md`](0021-wechat-miniprogram-media-gateway.md)
- [`0022-ambiguous-interrupt-semantic-evidence.md`](0022-ambiguous-interrupt-semantic-evidence.md)
- [`0023-keyword-control-evidence-and-bounded-aec-capture.md`](0023-keyword-control-evidence-and-bounded-aec-capture.md)
- [`0035-esp32-first-class-realtime-terminal.md`](0035-esp32-first-class-realtime-terminal.md)
