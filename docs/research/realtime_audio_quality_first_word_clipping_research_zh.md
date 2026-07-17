# Memoria 实时语音音质与首词截断专项研究

> 研究日期：2026-07-17  
> 本文只记录可回溯到厂商官方文档、官方示例/SDK、标准规范或当前仓库源码的结论。厂商接口仍在更新，正式实施前应重新核对文档更新时间与模型版本。

## 证据等级与写法

- **[官方事实]**：来自阿里云百炼官方帮助中心或 W3C / WHATWG 标准。可作为当前公开接口契约，但仍受模型版本、地域和文档更新影响。
- **[源码事实]**：来自阿里官方 GitHub 仓库、官方 SDK，或 Memoria 当前源码快照。源码事实只说明“代码现在这样做”，不自动等于服务端契约；本地工作区快照与正式发布版本必须分开标注。
- **[工程推断]**：由事件时序、浏览器媒体行为和现有实现共同推导的高概率解释。必须通过带 `response_id` 的事件时间线、首轮远端录音和 WebRTC Stats 做真机验证。
- **[未证实]**：当前没有找到阿里官方文档、官方示例或阿里一方公开 issue 可以确认的行为。不得写成“厂商已知问题”或“官方保证”。

引用优先使用固定版本源码链接。公开 issue 中用户描述只算现象线索；只有仓库维护者回复可提升为阿里一方证据。

## 1. Qwen3.5-Omni-Flash-Realtime：WebRTC 首词、取消与欢迎语生命周期

### 1.1 结论先行

1. **[官方事实] `semantic_vad` 处理的是输入侧语音结束与语义有效性，不修复输出侧首帧。** 官方说明它可过滤回应语、背景音等无意义声音；`threshold` 和 `silence_duration_ms` 影响的是何时判定用户语音及何时触发响应。把首词被吞直接归因于 VAD 参数，没有官方依据。
2. **[官方事实] WebRTC 只能使用 VAD 模式，VAD 会在用户语音结束后自动创建响应。** `response.create` 不是普通用户话轮的必需步骤。主动欢迎语是额外的客户端行为，不在阿里官方浏览器 WebRTC 快速示例的主流程中。
3. **[官方事实] `response.cancel` 只承诺取消“正在进行”的响应；当前没有可取消响应时，服务端会返回 `error`。** 因此“已经发送 `response.create`，但尚未收到 `response.created`”不能被当成“已有活动响应”。
4. **[源码事实] 阿里官方 SDK 的 `cancel_response()` 只发送控制事件，不清理本地播放缓存。** 官方 Server VAD 示例会在 `speech_started` 时另行清空本地两级播放队列；官方 WebRTC Python 示例也单独清理低延迟播放器缓存。这说明服务端取消和客户端停止已接收音频是两个动作。
5. **[线上版本事实] 用户本轮测试所用的 `20260717-113441` 是修复前基线：欢迎语请求、开麦与 `pending` 取消窗口可能相撞。** 下文的“双 cancel / 未知 response cancel”描述针对该线上版本和修复前源码，不再代表 12:26 后的本地工作区。
6. **[源码与发布事实] `20260717-123551` 已发布第一轮状态机修复。** `session.updated` 后先开 `400 ms` 安静观察窗；用户先开口则抑制欢迎语；欢迎已经 requested 但尚无 ID 时只记 `responseSuperseded`，等 `response.created(response_id)` 后最多 cancel 一次；被取消 ID 的迟到 `response.done` 不再覆盖新状态。该实现已通过完整门禁、Provider/readiness 和公网浏览器配置验收，但仍不能冒充真人听感，需同设备复测。
7. **[未证实] 截至本次复核，没有在阿里官方文档、官方 SDK 仓库或官方语音示例仓库公开 issue 中找到“Qwen3.5 Omni WebRTC 固定吞首词/首帧”的已确认服务端缺陷。** 现阶段不能把该现象直接定性为模型 bug。

因此，当前优先级是：**先验收本地状态机修复与逐 response 观测，再判断是否存在 RTP 首帧或浏览器播放问题；暂不优先调 `semantic_vad`。**

### 1.2 官方 WebRTC 与响应生命周期

#### 1.2.1 建连和媒体事实

- **[官方事实]** WebRTC 通过 HTTP POST 完成 Offer/Answer SDP 交换，随后底层自动建立媒体通道。音频通过 RTP 传输，控制事件通过 DataChannel 传输；服务端固定使用名为 `txt` 的通道推送事件。WebRTC 场景不会通过 `response.audio.delta` 返回音频数据。
- **[官方事实]** WebRTC 只支持 VAD，不支持 Manual 模式。典型输入顺序是 `speech_started` → `speech_stopped` → `committed`，随后服务端自动发出 `response.created` 并通过 RTP 输出音频。
- **[官方事实]** `track` 事件只说明接收端新增了远端媒体轨道；它不等于 `<audio>` 已成功开始播音。WHATWG 对 `play()` 的定义是返回 Promise，并在播放真正启动时 resolve，同时触发 `playing`。因此必须分别记录 `track`、`srcObject`、`play()` 和 `playing`，不能用其中一个代替整条首声证据链。

官方主流程可简化为：

```text
setRemoteDescription
        │
        ├── remote track / txt DataChannel 到达
        │
session.created
        │ client: session.update
session.updated
        │
        ├── 用户音频通过 RTP 持续上行
        │
speech_started → speech_stopped → committed
        │
response.created(response_id)
        │
        ├── response.audio_transcript.* 通过 DataChannel
        └── assistant audio 通过 RTP
        │
response.done(response_id, status)
```

来源：

- [Qwen-Omni-Realtime 官方文档：WebRTC 建连、交互流程与快速示例](https://help.aliyun.com/zh/model-studio/realtime#webrtc01connh2)
- [服务端事件：speech_started](https://help.aliyun.com/zh/model-studio/server-events#d06b426b1fnun)
- [服务端事件：speech_stopped](https://help.aliyun.com/zh/model-studio/server-events#fd08ffdf0a2mt)
- [服务端事件：response.created](https://help.aliyun.com/zh/model-studio/server-events#e43ca5aa9eqp6)
- [服务端事件：response.done](https://help.aliyun.com/zh/model-studio/server-events#f2333c777d9s4)
- [W3C WebRTC：`track` 事件](https://www.w3.org/TR/webrtc/#event-track)
- [WHATWG HTML：`play()` 与 pending play promises](https://html.spec.whatwg.org/multipage/media.html#dom-media-play)

#### 1.2.2 `response.create` 的边界

- **[官方事实]** 客户端事件文档明确写明：VAD 模式下服务端自动生成响应，无需发送 `response.create`；工具调用回传结果后，客户端才需要再发 `response.create` 触发最终回答。
- **[源码事实]** 阿里官方 Python SDK 仍提供 `create_response(instructions, output_modalities)`，并生成带 `response.instructions` 和 `response.modalities` 的事件。也就是说，主动创建一轮响应有官方 SDK 源码支撑，但它不是官方浏览器 WebRTC 示例验证过的“会话一建立就欢迎”流程。
- **[工程推断]** 主动欢迎语可做，但应被视为独立的“无用户输入 response”，拥有自己的 `welcome_id`、超时、抑制和取消策略，不能复用普通用户话轮的隐式 VAD 状态。

来源：

- [客户端事件：response.create](https://help.aliyun.com/zh/model-studio/client-events#b4b869840ce6m)
- [DashScope Python SDK 固定版本：`create_response()`](https://github.com/dashscope/dashscope-sdk-python/blob/cd7c7fd5a8981225d3cbcbeb731b538c18f10f9c/dashscope/audio/qwen_omni/omni_realtime.py#L472-L500)

### 1.3 `semantic_vad` 能做什么，不能做什么

#### 1.3.1 已有公开契约

- **[官方事实]** `semantic_vad` 基于语义有效性检测语音结束，可过滤回应语和背景音等无意义声音；只支持 Qwen3.5 Omni Realtime 系列。官方模型说明进一步称其“语义打断”可避免附和声和无意义背景音触发打断。
- **[官方事实]** `threshold` 范围为 `[-1.0, 1.0]`，默认 `0.5`。值越低越灵敏，越容易把微弱声音和背景噪声识别为语音；值越高越需要清晰、音量更大的语音。
- **[官方事实]** `silence_duration_ms` 范围为 `[200, 6000]`，默认 `800`。降低它会更快触发响应，但更容易把短暂停顿误判成一轮结束。
- **[源码事实]** Memoria 当前为 Qwen3.5 Omni 使用 `semantic_vad + threshold=0.5 + silence_duration_ms=800`，即厂商默认阈值和默认静音窗口。
- **[官方事实与文档缺口]** 阿里官方 WebRTC 浏览器快速示例中出现了 `prefix_padding_ms: 500`，但当前客户端事件参数表没有正式说明该字段。它可作为示例线索，不能被写成 Qwen3.5 已承诺的稳定参数，更不能用来解释输出侧首词被吞。

来源：

- [客户端事件：session.update / turn_detection](https://help.aliyun.com/zh/model-studio/client-events#26a8302028sjm)
- [Qwen-Omni-Realtime 模型说明](https://help.aliyun.com/zh/model-studio/realtime)
- Memoria 当前实现：`apps/h5/src/voice/QwenOmniWebRTCTransport.js:38-61`

#### 1.3.2 对客户端打断策略的含义

官方文档没有提供独立的“语义意图已经确认”事件。`speech_started` 的公开定义只是“VAD 检测到语音开始”。因此：

- **[工程推断]** `speech_started` 更适合被当成“候选打断开始”，不足以单独证明用户真的要夺取话权。
- **[工程推断]** 如果客户端在每个 `speech_started` 上立即执行硬 cancel，那么在事件可能先于语义过滤结果到达的实现中，会削弱 `semantic_vad` 过滤附和声和背景音的价值。
- **[工程建议]** 使用两阶段策略：候选期立即对本地输出做短时 duck；只有出现明确打断文本、持续有效语音或服务端已经提交新用户项时才硬静音/取消。若候选被过滤，应平滑恢复，不创建新回答。
- **[需要实测]** 阿里服务端在 `semantic_vad` 下是否完全抑制无意义声音对应的 `speech_started`，当前官方文档没有给出事件级保证。必须用“AI 播放中用户说嗯/对、咳嗽、桌面碰撞、完整插话”四组音频记录事件序列后再定最终延迟窗口。

### 1.4 `response.cancel` 不等于清空已接收音频

#### 1.4.1 官方与源码证据

- **[官方事实]** `response.cancel` 用于取消正在生成的响应；当前没有响应可取消时，服务端返回 `error`。
- **[官方事实]** 即使响应被中断、不完整或取消，仍可能收到 `response.audio.done` 等收尾事件。客户端必须按 `response_id` 隔离迟到事件，不能在发送 cancel 后假定旧事件从此消失。
- **[源码事实]** 官方 Python SDK 的 `cancel_response()` 只发送 `{type: "response.cancel"}`，没有播放器参数，也没有清理播放队列。
- **[源码事实]** 阿里官方 Server VAD 示例在收到 `speech_started` 时调用 `b64_player.cancel_playing()`；播放器实现会分别清空 Base64 输入队列与已解码 PCM 队列。官方 README 还说明播放块过大会增加打断延迟，示例建议约 `100 ms`。
- **[源码事实]** 当前官方 WebRTC Python 快速示例比旧 WebSocket 播放器更激进：先启动输出流，以 `5 ms` block 播放、最多保留约 `0.2 s` 缓存，并在 `speech_started` 时清缓存。

来源：

- [客户端事件：response.cancel](https://help.aliyun.com/zh/model-studio/client-events#38f40cb6447up)
- [服务端事件：response.audio.done](https://help.aliyun.com/zh/model-studio/server-events#9e8eb59c67qnt)
- [DashScope Python SDK 固定版本：`cancel_response()`](https://github.com/dashscope/dashscope-sdk-python/blob/cd7c7fd5a8981225d3cbcbeb731b538c18f10f9c/dashscope/audio/qwen_omni/omni_realtime.py#L502-L513)
- [阿里官方语音示例固定版本：`speech_started` 时取消本地播放](https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/a59f3ea9ab04148724ff75425ce558ca5815601e/samples/conversation/omni/python/run_server_vad.py#L68-L74)
- [阿里官方语音示例固定版本：播放器清空两级队列](https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/a59f3ea9ab04148724ff75425ce558ca5815601e/samples/conversation/omni/python/B64PCMPlayer.py#L65-L70)
- [阿里官方 WebRTC Python 快速示例](https://help.aliyun.com/zh/model-studio/realtime#rtc08quickwebrtch2)

#### 1.4.2 对浏览器 RTP 播放的推论

浏览器 WebRTC 音频不暴露类似 Python 示例的 PCM 队列，应用不能直接清空内部 jitter buffer。因此不能生搬硬套 `queue.clear()`，但仍需建立本地输出门控：

1. **候选打断**：先将 `<audio>` 音量快速降到约 `0.15–0.25`，不要立刻销毁元素或替换远端轨道。
2. **确认打断**：将本地元素静音，让实时媒体继续消费旧 RTP；同时仅在确认存在活动 `response_id` 时发送一次 `response.cancel`。
3. **新回答开始**：新 `response.created(response_id)` 且用户已停止有效讲话后再解除静音，并对旧 response 的所有 late events 继续按 ID 丢弃。
4. **假打断**：若没有形成有效用户项或新响应，约定窗口内平滑恢复原音量。

**[工程推断]** “静音但继续消费”比 `pause()` 更适合实时 MediaStream：暂停可能保留不可控的浏览器缓冲，而持续消费更有利于在解除静音时回到实时点。该行为需要 Chrome、Safari 和目标手机分别实测，不能仅靠桌面 Chrome 下结论。

### 1.5 Memoria 修复前线上竞态与当前本地状态机

#### 1.5.1 `20260717-113441` 线上基线 / 修复前时序

**[线上版本事实 + 修复前源码事实]** 用户本轮测试时的正式发布仍可能出现以下顺序；该图用于解释 `20260717-113441`，不表示当前本地工作区仍这样实现：

```text
session.updated
    ├── sessionUpdated = true
    ├── 打开麦克风 track.enabled
    ├── welcomeRequested = true
    ├── responsePending = true
    └── 立即发送 welcome response.create

若很快收到 speech_started
    ├── userSpeaking = true
    ├── 因 responsePending=true 发送 response.cancel
    └── 立刻清空 pending/active 本地状态

若随后才收到 welcome 的 response.created
    ├── 发现 userSpeaking=true
    └── 再发送一次 response.cancel
```

`response.cancel` 的公开约束解释了风险：`responsePending` 只表示客户端已经请求生成，并不能证明服务端已有可取消响应。若 cancel 先于 `response.created` 被处理，服务端有权返回错误。线上版本证据与发布边界见 [`20260717-113441` 发布清单](../releases/20260717-113441.md)。

#### 1.5.2 当前本地已实施的欢迎语状态机

**[源码事实]** 当前本地实现虽然仍使用若干布尔量和 ID 集合，但行为上已经拆开以下状态：

| 状态 | 含义 | 允许动作 |
|---|---|---|
| `WELCOME_ARMED` | session 已更新，等待 `400 ms` 静默窗口 | 用户先开口则取消 timer、抑制欢迎语，不发送 cancel |
| `WELCOME_REQUESTED` | 已发 `response.create`，尚未收到 ID | 可标记 `superseded`，但不向“未知响应”发送 cancel |
| `ACTIVE(response_id)` | 已收到 `response.created` | 只允许发送一次 cancel，并保留 ID 直到对应 `response.done` |
| `CANCELLING(response_id)` | 已发送 cancel，等待旧响应收尾 | 丢弃该 ID 的文本和状态，不得覆盖新一轮 UI |
| `ACTIVE(next_response_id)` | VAD 为用户输入自动创建的新回答 | 解除本地静音，旧 ID 的迟到事件继续忽略 |

当前实现时序：

1. `session.updated` 后打开 **400 ms 安静观察窗**；这是首版实验值，并不表示浏览器远端音轨或 `play()` 已经完成。
2. 窗口内若出现用户语音，取消欢迎定时器，完全不发送欢迎 `response.create`；等待 VAD 自动回答用户。
3. 窗口结束且用户未讲话，才发送一次欢迎 `response.create`。
4. 欢迎请求发出后、`response.created` 前用户开口，只记 `superseded=true`；等 ID 到达后最多 cancel 一次。
5. `cancelledResponseIds` 对 cancel 去重；对应 `response.done` 只清理旧 ID，不得把新一轮状态改回 `ready`。
6. 正常 `response.done` 后启动 `500 ms` 反馈保护实验：暂时禁用上行麦克风轨道；保护窗内若仍收到 `speech_started`，抑制该 item，并在服务端为其创建 response 时取消。该策略用于验证本轮观测到的尾音反馈，不是最终 AEC 方案。

对应当前本地源码：

- `apps/h5/src/voice/QwenOmniWebRTCTransport.js:281-318`：armed、用户先开口抑制、pending 只标记 superseded；
- `apps/h5/src/voice/QwenOmniWebRTCTransport.js:360-428`：按 response ID 激活、取消与忽略迟到 done；
- `apps/h5/src/voice/QwenOmniWebRTCTransport.js:482-586`：cancel 去重、400 ms 欢迎观察窗和 500 ms 尾音反馈保护。

**[工程推断]** 欢迎状态机应降低“欢迎语没声音、开头只响半句、首轮取消后又接着说”三类现象；但 `500 ms` 直接关麦可能吞掉用户在 AI 结束后立即接话的开头，必须用“AI 说完后 0–700 ms 立即回答”的真机矩阵验证。观察窗和反馈窗都只是实验参数，不应在没有分布数据时固化为长期产品常量。

### 1.6 首词/首帧被吞：如何先定位发生在哪一层

“听起来少了第一个字”至少有四种完全不同的原因。应在同一轮保存以下对照，而不是只听主观感受：

| 证据组合 | 更可能的层级 | 下一步 |
|---|---|---|
| `audio_transcript` 完整；远端 MediaRecorder 录音也完整；扬声器听感缺首词 | 本地 `<audio>` 启动、输出路由或音量门控 | 查 `play()`、`playing`、静音切换与设备路由 |
| transcript 完整；远端录音缺首帧；首包附近 `concealedSamples/concealmentEvents` 增长 | RTP 丢包、迟到或 jitter buffer concealment | 查网络、ICE 路径、首包 stats delta |
| transcript 完整；远端录音缺首帧；网络 stats 平稳；只发生在主动欢迎语 | 欢迎语与远端播放器准备竞态，或服务端首个 RTP 起播 | 关闭欢迎语做 A/B，再加安静观察窗 |
| transcript 本身也从第二个词开始 | 模型生成/转录内容，不是本地播放吞帧 | 保存 `response.done.output[].content[].transcript` 复核 |
| 仅在 cancel 后的新回答发生 | cancel、静音恢复或 response ID 生命周期 | 按 response ID 还原 cancel/created/done 顺序 |

阿里官方浏览器 WebRTC 示例已经包含对远端 `MediaStream` 的 `MediaRecorder` 录制与下载，可借鉴为测试工具。生产环境不得默认持久化通话音频；仅在明确测试、取得授权并设置短时删除策略时使用。

#### 建议的逐 response 时间线

每条记录至少带 `session_id`、本地 `turn_id`、`response_id`、欢迎语标志、单调时钟：

1. `setRemoteDescription` 完成；
2. `track_subscribed`；
3. `<audio>.srcObject` 设置；
4. `play_called / play_resolved / play_rejected`；
5. `session.created / session.updated`；
6. 欢迎观察窗开始、取消或触发；
7. `response.create` 实际发送；
8. `speech_started / speech_stopped / committed`；
9. `response.cancel` 发送原因，以及发送时是否已有活动 `response_id`；
10. `response.created(response_id)`；
11. 首个 `response.audio_transcript.delta`；
12. `<audio>` 首次 `playing`，以及每轮首个非静音能量点；
13. `response.done(response_id, status)`；
14. 在第 7、10、12、13 步各取一次 inbound RTP stats 快照并计算差值。

**[源码事实]** Memoria 已采集 `packetsLost`、`packetsReceived`、`concealedSamples`、`silentConcealedSamples`、`concealmentEvents`、`jitterBufferDelay`、`jitterBufferTargetDelay`、`jitterBufferMinimumDelay`、`jitterBufferEmittedCount`、加减速采样数和编码音频码率。当前本地实现保留每 `5 s` 巡检，并在 `response.created`、首个 assistant transcript delta、`response.done` 和欢迎语 request 时额外快照；但它仍缺少浏览器端“首个非静音能量点”，所以还不能单独完成首词归因。

W3C 定义中，`concealedSamples` / `concealmentEvents` 可反映丢失或迟到包导致的音频隐藏处理；平均 jitter buffer 延迟可由 `jitterBufferDelay / jitterBufferEmittedCount` 计算。

来源：

- [W3C WebRTC Stats：RTCInboundRtpStreamStats](https://www.w3.org/TR/webrtc-stats/#dom-rtcinboundrtpstreamstats-concealedsamples)
- Memoria 当前统计映射：`apps/h5/src/voice/webrtcStats.js:1-74`
- Memoria 当前巡检与事件快照：`apps/h5/src/voice/QwenOmniWebRTCTransport.js:360-428,599-624`

### 1.7 建议的最小验证矩阵

在继续改 prompt、语速或 VAD 前，先固定同一网络、同一浏览器、同一音色做以下对照：

| 组别 | 欢迎语 | cancel 策略 | 目的 |
|---|---|---|---|
| A | 关闭 | 仅服务端 semantic interruption | 建立无主动响应基线 |
| B | `session.updated` 后立即发送 | `20260717-113441` 修复前策略 | 复现线上竞态基线 |
| C | 400 ms 安静观察窗后发送 | 只有 active response 才 cancel | 验证当前本地状态机 |
| D | 同 C | 候选期 duck，确认后 cancel | 验证附和声与真打断兼容性 |

每组至少覆盖：完全静默进入、连接后立刻开口、欢迎语中途插话、只说“嗯/对”、咳嗽/桌面碰撞、弱网。重点指标不是“是否更自然”一个总分，而是：

- 无活动 response 的 cancel error 次数；
- 每个用户话轮生成的 `response.created` 数；
- 欢迎语被抑制、被取消和正常完成的比例；
- 首词缺失比例，并区分远端录音与扬声器听感；
- 从 `speech_started` 到本地 duck、确认 cancel、恢复播放的时延；
- 首包窗口内 RTP 丢包、concealment 和平均 jitter buffer delay 的增量。

在这些证据完成前，不建议通过固定加“嗯”、在文本开头塞无意义字或强制欢迎语先说一个牺牲音节来掩盖首词问题；这会让 GPT Live 式自然感变差，也会隐藏真正的媒体与状态机缺陷。

### 1.8 本节资料索引

1. [阿里云百炼：Qwen-Omni-Realtime](https://help.aliyun.com/zh/model-studio/realtime)
2. [阿里云百炼：Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)
3. [阿里云百炼：Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/server-events)
4. [DashScope Python SDK：Qwen Omni Realtime 固定版本源码](https://github.com/dashscope/dashscope-sdk-python/blob/cd7c7fd5a8981225d3cbcbeb731b538c18f10f9c/dashscope/audio/qwen_omni/omni_realtime.py)
5. [阿里云百炼语音示例：Omni Server VAD 固定版本](https://github.com/aliyun/alibabacloud-bailian-speech-demo/tree/a59f3ea9ab04148724ff75425ce558ca5815601e/samples/conversation/omni)
6. [阿里官方公开 issue：本次未发现已确认的 Omni WebRTC 吞首词问题](https://github.com/aliyun/alibabacloud-bailian-speech-demo/issues?q=is%3Aissue+omni)
7. [W3C WebRTC](https://www.w3.org/TR/webrtc/)
8. [W3C WebRTC Stats](https://www.w3.org/TR/webrtc-stats/)
9. [WHATWG HTML Media](https://html.spec.whatwg.org/multipage/media.html)

## 2. LiveKit / WebRTC / Opus：24 kHz PCM、电流感与首词截断

### 2.1 24 kHz PCM 到 Opus 48 kHz 时钟并不矛盾

- **[源码事实]** Memoria 当前使用 `livekit-agents==1.6.5`、Python RTC SDK `1.1.13` 与 H5 `livekit-client==2.20.1`。LiveKit Agents 的 `AudioOutputOptions` 默认是 `24 kHz / mono`；RoomIO 仅在 TTS frame 的采样率与输出采样率不同时创建 `AudioResampler`。
- **[源码事实]** 当前 CosyVoice 与 RoomIO 都配置为 `24 kHz / mono`，因此正常路径不会先在 Python Agent 内做一次 `24 kHz → 48 kHz` 重采样。RoomIO 将 PCM 切为约 `50 ms` frame，`AudioSource` 的队列预算为约 `200 ms`；取消时会清理输出 buffer。
- **[标准事实]** RFC 7587 规定 Opus RTP timestamp clock 固定为 `48 kHz`。这不代表源 PCM 必须先由业务代码转换成 48 kHz，也不表示浏览器统计中显示 48 kHz 就发生了错误。RFC 6716 允许 Opus 使用 8/12/16/24/48 kHz 的输入/输出采样率；实际 RTP 编码和重采样由 WebRTC/Opus 栈处理。
- **[工程结论]** “CosyVoice 输出 24 kHz，而浏览器收到 Opus 48 kHz”本身是正常链路，不能作为电流声根因。真正需要排查的是：PCM 元数据是否错标、字节序/声道数是否错误、同一轨道是否重复播放、首帧是否被丢弃、Opus 码率是否过低，以及网络 concealment 是否在异常窗口增长。

来源：

- [LiveKit Agents 1.6.5：voice generation](https://github.com/livekit/agents/blob/livekit-agents%401.6.5/livekit-agents/livekit/agents/voice/generation.py)
- [LiveKit Agents 1.6.5：RoomIO output](https://github.com/livekit/agents/blob/livekit-agents%401.6.5/livekit-agents/livekit/agents/voice/room_io/_output.py)
- [LiveKit Agents 1.6.5：RoomIO types](https://github.com/livekit/agents/blob/livekit-agents%401.6.5/livekit-agents/livekit/agents/voice/room_io/types.py)
- [LiveKit Python SDK 1.1.13：AudioResampler](https://github.com/livekit/python-sdks/blob/rtc-v1.1.13/livekit-rtc/livekit/rtc/audio_resampler.py)
- [RFC 7587：RTP Payload Format for Opus](https://www.rfc-editor.org/rfc/rfc7587)
- [RFC 6716：Definition of the Opus Audio Codec](https://www.rfc-editor.org/rfc/rfc6716)

### 2.2 浏览器起播与自动播放是独立证据层

- **[源码事实]** LiveKit JS 的 `Track.attach()` 创建或复用媒体元素、设置 `srcObject` 并调用 `play()`；`Room.startAudio()` 用于显式解除浏览器自动播放限制，iOS 路径还会创建静音 dummy audio。
- **[工程结论]** `track_subscribed`、元素已挂载、`play()` resolve、`playing`、`currentTime` 前进必须分别记录。远端轨道已出现，不等于用户已经听见首字。
- **[工程风险]** 同一音轨被两个 `<audio>` 元素同时播放会形成 comb filtering，听起来可能像金属声、电流声或轻微回声。Memoria 已对 Omni 使用单一复用元素，对 LiveKit track/stream 做去重；是否仍重复必须看运行时事件，而不能只看静态代码。

来源：

- [LiveKit Client SDK 2.20.1：Room](https://github.com/livekit/client-sdk-js/blob/v2.20.1/src/room/Room.ts)
- [LiveKit Client SDK 2.20.1：Track](https://github.com/livekit/client-sdk-js/blob/v2.20.1/src/room/track/Track.ts)
- [LiveKit `Room.startAudio()` 参考](https://docs.livekit.io/reference/client-sdk-js/classes/Room.html#startaudio)

### 2.3 LiveKit 一方 issue：与首部 PCM 截断高度相似，但不能直接定因

LiveKit 官方仓库仍开放的 [Issue #5158](https://github.com/livekit/agents/issues/5158) 报告了以下现象：

- **[一方 issue 报告事实]** 自定义 TTS 的 `24 kHz / mono PCM` 经 `AudioByteStream → AudioSource.capture_frame()` 输出时，约每 3–5 次发生一次首部 `50–300 ms` 截断，表现为开头 pop、首音节或首词缺失。
- **[一方 issue 报告事实]** 报告者保存的原始 PCM 完整，frame 也都到达 `capture_frame()`；MP3 经 `AudioStreamDecoder` 的路径没有观察到同样现象。
- **[一方 issue 报告事实]** 加 `30–100 ms` 延迟、前置 `200 ms` 静音、修改 chunk 和 queue size 都未解决。
- **[证据限制]** 报告版本是 Agents `1.4.2` / RTC `1.1.2`，不是 Memoria 当前版本；issue 仍 open，也没有维护者确认 Memoria 的根因相同。

因此它只能提高“PCM → AudioSource → 编码/首帧门控”这一假设的优先级。只有在“原始 CosyVoice PCM 完整、网络 delta 平稳、浏览器远端录音仍缺首部”同时成立时，才可称为同类路径问题。

### 2.4 本轮生产样本与实验室检查

以下均是 **[Memoria 本轮实测]**，不是厂商保证：

- 最新级联真人会话中，浏览器 inbound RTP 全程 `packets_lost=0`、`concealed_samples=0`，jitter 约 `1–3 ms`。因此该轮若仍有轻微金属感，网络丢包不是第一嫌疑。
- 三段 CosyVoice 原始 `24 kHz PCM` 均为偶数字节、没有 RIFF/WAV header，`clip_ratio=0`、DC offset 约为 2，峰值约 `15k–23k`。这排除了最基础的容器头误传、奇数字节、明显削波和大幅直流偏移，但不能证明所有句子的频谱与首帧都正常。
- 同一 PCM 的 PyAV Opus 往返误差代理随码率稳定改善：`16 kbps≈12.5 dB`、`24 kbps≈14.9 dB`、`32 kbps≈16.6 dB`、`48 kbps≈19.1 dB`、`64 kbps≈22.2 dB`。该数值不是 MOS，也不能证明 64 kbps 必然解决主观音质；它支持把级联发布码率提高到 `64 kbps` 做 A/B，而不是继续在低码率下猜测。
- **[源码与发布事实]** `20260717-123551` 已把 LiveKit `TrackPublishOptions.audio_encoding.max_bitrate` 设为 `64,000 bit/s`，同时保持 RoomIO `24 kHz / mono`；运行容器已读回 `24 kHz / mono / 64,000 bit/s`。`64 kbps` 是编码上限/目标配置，不是实际发送码率保证，验收仍应读取浏览器 `bytesReceived / totalSamplesDuration` 的窗口增量并做同设备盲听。
- 最新 Omni 会话首个统计样本中，非静音 concealment 约为 `32,463 / 204,480 ≈ 15.9%`；另有三次 `response.done → speech_started` 仅约 `8–10 ms`。这种紧贴输出结束的 VAD 事件更像播放尾音反馈或同一竞态窗口，而不是自然的人类反应时间，但仍需真机录音/AEC 对照确认。

### 2.5 WebRTC Stats 必须按窗口增量解释

W3C 定义中：

- `concealedSamples`：因丢失或迟到而由接收端合成/隐藏的样本；
- `silentConcealedSamples`：其中以静音隐藏的样本；
- `concealmentEvents`：隐藏处理事件数；
- `jitterBufferDelay / jitterBufferEmittedCount`：在同一窗口取增量后，二者比值可估计平均 jitter buffer 延迟；
- `insertedSamplesForDeceleration / removedSamplesForAcceleration`：jitter buffer 为减速/加速而插入或删除的样本。

建议每 `5 s` 巡检，同时在 `response.created`、首个 assistant transcript delta、`response.done` 和 cancel 时额外快照；后续再补首个非静音能量点。判定时使用：

```text
窗口丢包率 = Δpackets_lost / (Δpackets_received + Δpackets_lost)
窗口隐藏率 = Δconcealed_samples / Δtotal_samples_received
窗口非静音隐藏率 = (Δconcealed_samples - Δsilent_concealed_samples)
                  / Δtotal_samples_received
平均 jitter buffer delay(ms) = 1000 × Δjitter_buffer_delay
                               / Δjitter_buffer_emitted_count
```

累计值只能说明“从连接开始到现在”，不能解释某一个首词。若 glitch 同时出现 loss/concealment/delay delta 跳升，优先查网络与 jitter；若 delta 平稳、原始 PCM 完整但首部仍缺失，优先查 PCM/AudioSource/起播门控；若保存的 PCM 已经有噪声，则回到供应商输出或格式层。

#### `RTCRtpReceiver.jitterBufferTarget` 的标准与浏览器边界

- **[标准事实]** W3C WebRTC 规范定义 `RTCRtpReceiver.jitterBufferTarget`，单位为毫秒。它允许应用表达希望 jitter buffer 保留的媒体时长，在播放延迟与网络抖动导致的断续风险之间取舍；规范明确说它只是 **target**，只会影响浏览器策略，实际缓冲仍受浏览器允许范围、网络条件与内存约束影响，不能承诺精确等于设定值。
- **[兼容性事实]** MDN 当前将该属性标为 `Limited availability`。MDN Browser Compat Data 记录桌面 Chrome 从 `124`、Firefox 从 `115`、Safari 从 `27` 起支持；这不等于所有内嵌 WebView、旧 Android、旧 iOS 或应用内浏览器都可用，也不能从桌面 Chrome 成功外推到目标手机。
- **[源码与发布事实]** `20260717-123551` 的 Omni 在远端 receiver 暴露该属性时尝试设置 `120 ms`，并仅在读回同值时上报 `omni_playout_buffer_configured`；属性不存在、赋值抛错或浏览器忽略时静默回退，不阻断通话。
- **[工程结论]** `120 ms` 只应作为缓解首包 underrun / concealment 的 A/B 参数。它可能以增加约一段 playout delay 为代价改善连续性，也可能被浏览器动态夹取或忽略；必须对照 `jitterBufferTargetDelay / jitterBufferEmittedCount` 的窗口增量验证实际影响，不能把“赋值成功”当成音质修复完成。

来源：

- [W3C WebRTC：`RTCRtpReceiver.jitterBufferTarget`](https://w3c.github.io/webrtc-pc/#dom-rtcrtpreceiver-jitterbuffertarget)
- [MDN：`RTCRtpReceiver.jitterBufferTarget`](https://developer.mozilla.org/en-US/docs/Web/API/RTCRtpReceiver/jitterBufferTarget)
- [MDN Browser Compat Data 固定版本：Chrome 124 / Firefox 115 / Safari 27](https://github.com/mdn/browser-compat-data/blob/1a19fb81cde2520283e0e3de7880daecd2f3e046/api/RTCRtpReceiver.json)
- [W3C WebRTC Stats](https://www.w3.org/TR/webrtc-stats/)
- Memoria 当前本地实现：`apps/h5/src/voice/QwenOmniWebRTCTransport.js:5,137-145,544-553`

### 2.6 推荐的同 trace 首声链

级联与 Omni 都应尽量对齐到同一单调时钟：

```text
用户最后有效音频
  → VAD / ASR final / turn commit
  → LLM 首 token / 首短语
  → TTS 首个非静音 PCM（级联）或 response.created（Omni）
  → AudioSource.capture / RTP 首包
  → track / srcObject / play_resolved
  → playing / currentTime 前进 / 首个非静音能量点
  → response 或 playout done
```

只有这条链能区分“模型没生成”“首个 PCM 没进入 WebRTC”“RTP/jitter 丢了”“浏览器元素没起播”和“扬声器路由听不到”。生产默认不保存通话音频；远端 `MediaRecorder` 只用于明确授权、短时保留的诊断会话。

## 3. 国内厂商替代与补充方案

### 3.1 横向结论

| 厂商/方案 | 官方确认的实时能力 | 情绪与副语言 | 时间戳/取消 | 对 Memoria 的判断 |
|---|---|---|---|---|
| 阿里 Qwen3.5 Omni + CosyVoice | Omni WebRTC/WSS、AEC/降噪、`semantic_vad`；CosyVoice 双向流式 TTS | Omni 可用自然语言软控语速/情绪；`longanyang` 只有 7 类固定 Instruct；没有稳定实时笑/咳标签 | Omni 按 response cancel；CosyVoice 有词时间戳 | 保持主线，先修生命周期、反馈环与笑声融合 |
| 火山豆包端到端 + RTC AI | 全双工、VAD、自动/手动打断；官方 RTC 方案描述整体链路约 1 秒 | O2.0/SC2.0 强调语音理解与角色表现；无稳定笑/咳标签 | 端到端只有快速字幕，不能复用词级 heard-text | 值得独立端到端 Spike，迁移成本高 |
| 腾讯 TRTC + FlowTTS | FlowTTS 首包官方称低至 300 ms；双向文本/音频流；`InterruptSession`；TRTC 音频/语义打断 | 官方仓库称可自然呈现语气词、情绪和副语言细节，但协议无逐句 emotion 或笑/咳标签 | FlowTTS 只有句子和句时长；无词级 TTS 对齐 | 最值得作为 CosyVoice 的第一个 TTS A/B，代价是 heard-text 精度 |
| 百度端到端语音 Lite/Pro | OpenAI-Realtime 风格 WSS、server VAD；官方称整轮等待约 1 秒 | 官方称感知原始语音情绪/语气并可表现快慢、悄声和情绪切换；无结构化 emotion | `interrupt_response` 当前只支持 true；无词级时间戳 | 适合端到端 Spike；浏览器 PCM/AEC/凭证代理需重建 |
| 讯飞超拟人交互 / SuperTTS | `continuous` 全双工；SuperTTS 文本流入、音频流出 | `remain=0` 可增加填充、重复、语气词和话语符号；FAQ 提到呼吸/叹气/语速变化 | Web API 缺少清晰中途 cancel 契约；口语化参数当前仅 x4 音色 | 最适合做“边想边说”离线试听，不宜直接替生产主链 |

这些首包/整轮数字来自不同产品和测量口径，不能直接排成延迟排行榜。五家公开契约中，没有一家保证“检测到用户笑声后必然先笑一下”，也没有可跨主链稳定使用的实时 `[laugh]` / `[cough]` 控制。

### 3.2 阿里现有链路的上限

- **[官方事实]** 阿里实时 TTS 支持 WebSocket 双向流式、PCM/WAV/MP3/Opus，最高 48 kHz；FAQ 将正常首包描述为约 500 ms、RTF 应小于 1，并建议从文本发送节奏、回调阻塞和网络波动排查卡顿。
- **[官方事实]** `longanyang` 的 Instruct 必须使用规定中文句式，合法情感为 `neutral / fearful / angry / sad / surprised / happy / disgusted`，不能自由传入“局部拖长、边想边说”。
- **[官方事实]** Qwen-Audio-TTS 的 `[giggles] [laughing] [cough] ...` 属于另一条单向流式模型能力，不能直接套到当前 CosyVoice 双向 adapter。它理论上可用于离线生成版本化 cue，但这会新增模型路径；当前实施不建议因此改主链。
- **[工程结论]** 级联的“嗯、好、让我先理一下”应由 LLM 生成一个短口头片段、标点与小幅全局 rate 实现；真正确定性的非语言笑声仍应使用与主音色一致、授权清晰、可取消的本地 cue。咳嗽不应作为常规自然化效果。

来源：

- [阿里云实时语音合成](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide)
- [CosyVoice 音色与 Instruct](https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list)

### 3.3 火山豆包

- **[官方事实]** RTC AI 方案支持全双工、VAD 和自动/手动打断；接入端到端模型后，原有 `ASRConfig/TTSConfig` 不再作为独立级联配置生效。
- **[官方事实]** 端到端字幕必须使用快速模式，不能直接取得当前 CosyVoice 级别的词时间戳与播放对齐。
- **[证据边界]** “约 1 秒”来自 RTC 解决方案描述，不是端到端模型 TTFT SLA；“情绪识别与生成”文档中的业务标签链也不能自动等价为端到端 S2S 的结构化 emotion 输出。

来源：

- [RTC AI 音视频互动方案](https://www.volcengine.com/docs/6348/1310537)
- [接入端到端实时语音大模型](https://www.volcengine.com/docs/6348/1902994)
- [端到端 Realtime API](https://www.volcengine.com/docs/6561/1594356)
- [情绪识别与生成](https://www.volcengine.com/docs/6348/2139328)

### 3.4 腾讯 FlowTTS

- **[官方事实]** 产品文档称 TTS 首包低至 300 ms；WebSocket 协议有 `InterruptSession`，适合频繁打断。
- **[官方事实]** 官方仓库称可自然呈现语气词、情绪与副语言细节；但公开协议主要暴露音色、速度、音量和音高，没有笑声/咳嗽标签或逐句 emotion 参数。
- **[官方事实]** `SentenceAudio` 有句子和句时长，没有词级 TTS 时间戳。腾讯 ASR 自身可返回句级/词级起止时间，但不能补回 TTS 的实际已播词。
- **[工程结论]** 它是最小的供应商补充 PoC：只替换级联 TTS，不动 FunASR/Qwen/LiveKit；主要回归风险是中断时 heard-text 从词级退化为句级。

来源：

- [FlowTTS 官方仓库](https://github.com/Tencent-RTC/FlowTTS)
- [FlowTTS 双向流协议](https://github.com/Tencent-RTC/FlowTTS/blob/main/docs/ws_bidirection_protocol.md)
- [对话式 TTS 与首包](https://cloud.tencent.com/document/product/647/131300)
- [TRTC 智能打断](https://cloud.tencent.com/document/product/647/112472)
- [腾讯实时 ASR](https://cloud.tencent.com/document/product/647/131297)

### 3.5 百度端到端语音

- **[官方事实]** API 使用 `session.update`、speech started/stopped、response/audio/transcript delta 等 OpenAI-Realtime 风格事件；server VAD 的 `interrupt_response` 当前只支持开启。
- **[官方事实]** 官方材料称模型可直接感知原始语音情绪/语气，用户整轮等待约 1 秒；公开实时事件没有结构化 emotion confidence，也没有音频或词级时间戳。
- **[工程结论]** 协议直观，但 Web 浏览器仍需新建 PCM16 AudioWorklet、播放队列、AEC 与服务端 token 代理，不能当作低成本替换。

来源：

- [百度端到端语音语言大模型 API](https://ai.baidu.com/ai-doc/SPEECH/nmcytnwei)
- [百度端到端语音产品页](https://ai.baidu.com/tech/speech/chatbot)

### 3.6 讯飞超拟人交互 / SuperTTS

- **[官方事实]** 超拟人交互默认 `continuous` 全双工，持续上传音频并返回 VAD、ASR、LLM 文本和 TTS 音频；文档公开 BOS/EOS/Silence，但没有清晰说明 BOS 必然停止当前 TTS。
- **[官方事实]** SuperTTS 的 `spark_assist=1`、`remain=0` 可增加填充语、重复语、语气词与话语符号；FAQ 提到呼吸、叹气和语速变化。当前口语化参数只适用于 x4 音色，公开 x4 主要是天津话/东北话。
- **[证据边界]** 文档没有稳定笑/咳标签，也没有足够清晰的 Web 中途 cancel 契约；“超拟人”不能替代打断与已听文本验收。

来源：

- [讯飞超拟人交互 API](https://www.xfyun.cn/doc/spark/sparkos_interactive.html)
- [讯飞超拟人语音合成](https://www.xfyun.cn/doc/spark/super%20smart-tts.html)
- [讯飞实时转写](https://www.xfyun.cn/doc/asr/rtasr/API.html)

## 4. 面向 GPT Live 的实施顺序

### 4.1 当前主线，先不换供应商

1. **Omni 欢迎语拆状态（`20260717-123551` 已发布）。** `session.updated` 后留 `400 ms` 安静观察窗；用户先说话就抑制欢迎，不向未知 response 发 cancel；按 ID 去重 cancel 并忽略迟到 done。自动化和公网工件已通过，但不能把这些等同于用户已经听到修复。
2. **Omni 反馈环先证实再收紧（`20260717-123551` 已发布实验）。** 针对已观察到的 `response.done` 后 `8–10 ms` speech start，当前加入 `500 ms` 关麦保护与反馈 response 抑制；它可能损伤用户立即接话，必须跑“0–700 ms 接话”与“嗯、对、咳嗽、完整插话”矩阵。短保护窗不能长期替代全双工 AEC。
3. **级联修复笑声融合。** Qwen3-ASR 已能把本次用户笑声转成 laughter 文本，但整体 emotion 仍可能是 neutral。笑声只驱动当前轮 `light_laughter`，不应把用户长期情绪写成 happy；严肃关键词始终优先。
4. **级联保持同一 generation。** 规划/口语训练的 preamble 与正文继续走同一个 LLM generation、同一个 CosyVoice stream；用一个 8–14 字口头片段、逗号和小幅 rate 形成局部思考感，不拆成两次回答。
5. **音质按数据调（`20260717-123551` 已发布实验）。** 级联已配置 `64 kbps Opus` 上限；Omni receiver 在浏览器支持时尝试 `jitterBufferTarget=120 ms`，并增加 response 期 stats 快照。两项都必须做同设备 A/B；不要把 24 kHz PCM、48 kHz RTP clock、属性赋值成功或累计 concealment 单独当作根因。

### 4.2 后续低风险实验

1. 保持 Qwen3.5 Omni 为端到端主 A/B；不要同时引入多个端到端厂商。
2. 若现有 CosyVoice 在真机盲听中仍显著僵硬，先做腾讯 FlowTTS 的单音色实验 adapter，只验证首包、取消、自然度与句级 heard-text 退化。
3. 用固定四句脚本离线试听讯飞 SuperTTS 的 oral 参数，先听效果再决定是否编码接入。
4. 端到端候选只选百度 Pro 或豆包 O2.0/SC2.0 之一做独立 Spike；不直接替换生产。
5. listener backchannel 继续使用独立 cue 通道，但只有 AEC 真机矩阵通过后才能开启；“自动打断”不等于“AI 能在用户讲话中安全地说嗯、对”。

### 4.3 固定验收脚本

| 场景 | 期望行为 | 禁止行为 |
|---|---|---|
| “帮我安排一个十五分钟的英语口语训练。” | 一次短、变化的思考式衔接；正文恢复正常速度 | 整段慢读、重复固定 opener |
| 回答提议后用户说“可以。” | 直接进入下一步或给话题 | 重复上一轮安排、再问同一个问题 |
| “今天星期几？” | 直接回答 | “嗯、好的、让我想想” |
| 笑着说“我把单词读错得太离谱了。” | 安全时允许一次短暖笑/开心语气 | 把“哈哈”机械朗读多次 |
| “哈哈，其实我刚刚出车祸了。” | 稳重关切，绝对不笑 | 情绪镜像、轻佻 filler |
| AI 播放中说“嗯/对” | 不创建新主回答，必要时短 duck 后恢复 | 硬 cancel、抢答 |
| AI 播放中说“等等/不是/我有新问题” | 快速停止本地输出并只回答最新内容 | 旧 response 迟到覆盖新状态 |

验收不能只看 Prompt 或字幕。至少同时保存：response/turn 生命周期、实际扬声器听感、`playing/currentTime`、逐窗口 RTP stats，以及取消后是否仍有旧音频继续播放。
