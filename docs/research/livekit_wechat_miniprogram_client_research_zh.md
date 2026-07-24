# LiveKit 微信小程序客户端可用性调研

> 调研日期：2026-07-24
> 调研目标：确认是否存在可直接用于微信小程序、连接现有 LiveKit 房间并发布/订阅实时音频的客户端；若不存在，确定最薄的媒体接入适配方案。
> 结论边界：本文优先核验官方文档、官方源码、npm 元数据、GitHub/Gitee/DCloud 可见代码与平台兼容声明。它不能排除某家公司内部存在未公开的私有实现，但可以判断当前是否有公开、可验证、可维护的生产候选。

## 1. 执行结论

截至 2026-07-24，**没有找到可公开获取、仍在维护、能在微信原生小程序运行时直接加入 LiveKit 房间，并完成麦克风发布和远端音频订阅的生产可用客户端**。

结论不是“LiveKit 服务端不能复用”，而是：

1. Memoria 现有 Control API、LiveKit Server、Agent、FunASR、Qwen、豆包 TTS、ModePolicy、UtteranceRouter 和 Generation Fence 都可以保持不变。
2. 缺失的是微信小程序到 LiveKit 的媒体客户端。
3. LiveKit 官方 JavaScript SDK 是浏览器 SDK，依赖 `RTCPeerConnection`、`MediaStream`、`navigator.mediaDevices.getUserMedia()`、DOM 音频元素等浏览器能力。
4. 微信小程序官方 API 提供录音 PCM 帧、WebSocket、WebAudio、RTMP 推拉流和微信自有 VoIP 房间，但没有暴露可供 `livekit-client` 使用的通用 `RTCPeerConnection` 接口。
5. 因此，若坚持“原生小程序 + 现有 LiveKit/Agent 主链”，需要增加一层**媒体接入适配器**。

推荐先实现一个严格限域的 PoC：

```text
微信小程序
  RecorderManager PCM 上行
  WebAudioContext PCM 下行
          │ WSS
          ▼
MiniProgramMediaGateway
  LiveKit Python RTC participant
          │
          ▼
现有 LiveKit Room → 现有 Agent / FunASR / Qwen / 豆包
```

这个适配器只负责媒体帧、LiveKit 数据事件和连接生命周期，不承载业务规则，不复制 Agent 或 Control API 逻辑。

## 2. “可用客户端”的判定标准

一个候选只有同时满足以下条件，才算本项目所需的 LiveKit 小程序客户端：

1. 能运行在**微信原生小程序**，不是 Android/iOS App、React Native、uni-app App 或 H5 `web-view`。
2. 能使用现有 `livekit_url` 和 participant token 加入 LiveKit 房间。
3. 能把小程序麦克风作为 LiveKit audio track 发布。
4. 能订阅并低延迟播放 Agent 发布的远端 audio track。
5. 能处理 LiveKit reliable data、transcription、重连和 track lifecycle。
6. 有可核验源码或官方文档，近期仍维护，许可证和发布方式明确。
7. 不要求把现有后端替换成另一个 RTC 厂商的房间系统。

仅能建立 LiveKit signaling WebSocket、只能播放 HLS/RTMP、只能运行在 App、或只是 UI 组件封装，都不满足条件。

## 3. 调研覆盖面

本次检查了：

- LiveKit 官方文档、sitemap 和 GitHub 组织下 109 个公开仓库；
- LiveKit 官方 JavaScript、React Native、Android、C++、Python RTC SDK；
- LiveKit GitHub repository、issue/discussion 关键词；
- npm Registry 的 `livekit wechat`、`livekit miniprogram`、`livekit wx`、`livekit uniapp`，以及 177 个 `keywords:livekit` 结果；
- GitHub、Gitee、GitLab、GitCode、DCloud 插件市场、微信开放社区和中文网页搜索；
- 微信官方 `miniprogram-api-typings` 5.2.1 和当前开放文档；
- Taro、uni-app/DCloud、微信 `live-pusher` / `live-player` / VoIP / RecorderManager / WebAudio 能力；
- LiveKit Ingress、Egress 和 Python RTC SDK 作为适配器基础的可行性。

主要一手来源：

- [LiveKit Client SDK 文档](https://docs.livekit.io/home/client/connect/)
- [LiveKit GitHub organization](https://github.com/livekit)
- [LiveKit JavaScript SDK](https://github.com/livekit/client-sdk-js)
- [livekit-client npm](https://www.npmjs.com/package/livekit-client)
- [微信小程序 API typings](https://github.com/wechat-miniprogram/api-typings)
- [RecorderManager.start](https://developers.weixin.qq.com/miniprogram/dev/api/media/recorder/RecorderManager.start.html)
- [WebAudioContext](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/WebAudioContext.html)
- [live-pusher](https://developers.weixin.qq.com/miniprogram/dev/component/live-pusher.html)
- [live-player](https://developers.weixin.qq.com/miniprogram/dev/component/live-player.html)
- [wx.joinVoIPChat](https://developers.weixin.qq.com/miniprogram/dev/api/media/voip/wx.joinVoIPChat.html)
- [LiveKit Python RTC SDK](https://github.com/livekit/python-sdks)

## 4. 候选核验结果

| 候选 | 微信原生小程序 | 直接加入 LiveKit | 发布麦克风 | 订阅远端音频 | 结论 |
|---|---:|---:|---:|---:|---|
| `livekit-client` JavaScript SDK | 否 | 浏览器中可以 | 依赖浏览器 WebRTC | 依赖 MediaStream/DOM | 不可直接使用 |
| `@livekit/react-native` | 否 | React Native App 可以 | 依赖原生 WebRTC 模块 | 依赖原生模块 | 与 Taro/小程序不是同一运行时 |
| LiveKit Android/iOS/C++ SDK | 否 | 原生 App 可以 | 可以 | 可以 | 小程序不能加载任意原生 SDK |
| LiveKit Rust/WASM 路线 | 无现成实现 | 无 | 无 | 无 | 缺媒体/网络底层能力，不是可用候选 |
| DCloud `wrs-uts-livekit` | 否 | Android/iOS App 可以 | 原生插件提供 | 原生插件提供 | 当前仍维护，但兼容表明确不支持微信小程序 |
| DCloud `livekit-plugin` | 否 | Android App 可以 | 原生插件提供 | 原生插件提供 | 兼容表不支持微信小程序 |
| npm/GitHub `livekit-miniprogram` 类包 | 未找到 | 未找到 | 未找到 | 未找到 | 无公开候选 |
| 微信 `wx.joinVoIPChat` | 是 | 否 | 微信自有房间 | 微信自有房间 | 不是任意 LiveKit SFU 客户端 |
| 微信 `live-pusher/live-player` | 是 | 否 | RTMP 推流 | FLV/RTMP 拉流 | 可做流媒体桥，但不是 LiveKit 客户端 |
| H5 `web-view` + `livekit-client` | H5 容器 | 可以 | 取决于 WebView 权限 | 可以 | 后端零改动，但不是原生复刻 |

### 4.1 LiveKit 官方没有微信小程序 SDK

LiveKit 当前公开客户端包括 Browser JavaScript、Swift、Android、Flutter、React Native、Rust、Node、Python、Unity、C++、ESP32 等；官方文档、sitemap 和 GitHub 组织仓库中没有 WeChat Mini Program / 微信小程序客户端。

相关来源：

- [LiveKit SDK 列表](https://docs.livekit.io/home/client/connect/)
- [client-sdk-js README 的 SDK 列表](https://github.com/livekit/client-sdk-js#readme)
- [LiveKit GitHub repositories](https://github.com/orgs/livekit/repositories)

GitHub repository 搜索：

- `livekit miniprogram`：0 个仓库；
- `livekit wechat`：0 个仓库；
- `livekit weixin`：0 个仓库；
- LiveKit organization issue 中 `miniprogram` / `"mini program"`：0 个相关 issue。

### 4.2 `livekit-client` 不能通过普通 polyfill 变成小程序 SDK

本次核验的当前 npm 版本为 `livekit-client@2.21.0`，发布日期为 2026-07-23。

其分发代码直接使用：

- `new RTCPeerConnection(...)`
- `navigator.mediaDevices.getUserMedia(...)`
- `MediaStream` / `MediaStreamTrack`
- `HTMLAudioElement`
- `document.createElement("audio")`
- `window.RTCRtpSender` / `window.RTCRtpReceiver`

源码和 README 也把它定义为 browser client SDK，并列出 Browser Support。

微信小程序官方 API typings 5.2.1 中没有：

- `RTCPeerConnection`
- `RTCRtpSender`
- `RTCRtpReceiver`
- `RTCDataChannel`
- 浏览器式 `MediaStreamTrack`
- `navigator.mediaDevices.getUserMedia`

这不是补一个 `window`、`document` 或 `fetch` polyfill 能解决的问题。LiveKit media path 依赖 ICE、DTLS、SRTP、RTP/RTCP、Opus、网络自适应和 WebRTC media track；小程序没有暴露足够的底层接口让纯 JavaScript 重建这些能力。

来源：

- [livekit-client source](https://github.com/livekit/client-sdk-js)
- [livekit-client npm package](https://www.npmjs.com/package/livekit-client)
- [微信官方 API typings](https://github.com/wechat-miniprogram/api-typings)

### 4.3 React Native、Taro 和 uni-app 不能改变运行时事实

`@livekit/react-native` 依赖 LiveKit 的 React Native WebRTC 原生模块。Taro 使用 React 语法不代表具备 React Native 的原生模块，也不会为微信小程序补出 `RTCPeerConnection`。

DCloud 可搜索到两个 LiveKit 方向候选：

- [wrs-uts-livekit](https://ext.dcloud.net.cn/plugin?id=23304)：当前版本 1.0.11，更新于 2026-07-02，支持 Android 5.0+ 和 iOS 13+；
- [DCloud livekit-plugin](https://ext.dcloud.net.cn/plugin?id=24583)

两者本质都是把 LiveKit Android/iOS 原生 SDK 包成 uni-app App 插件。页面平台兼容表中，微信小程序、支付宝小程序、抖音小程序等全部标记为不支持。较新的 `wrs-uts-livekit` 已经提供连接、麦克风、音频 track、transcription、data publish 等能力，但这些能力来自 Android/iOS 原生基座，不能编译为微信小程序。

因此：

- “用 Taro React 写小程序”可以复用开发习惯；
- “用 uni-app 编译微信小程序”可以复用部分页面代码；
- 二者都不能让 LiveKit 原生/浏览器 SDK自动获得小程序 RTC 能力。

### 4.4 npm 没有可验证的小程序候选

截至调研日：

- `livekit wechat`
- `livekit miniprogram`
- `livekit wx`
- `livekit uniapp`
- `livekit mini program`

这些 npm 搜索的高分结果仍是官方 browser、React Native、Node、Agents 和 Server SDK。

对 npm Registry 返回的 177 个 `keywords:livekit` 包按 name、description、keywords 和 repository 搜索 `miniprogram/wechat/weixin/wxapp/uni-app/小程序`，没有发现可直接用于微信小程序的 LiveKit 客户端。

来源：

- [npm Registry search API](https://registry.npmjs.org/-/v1/search?text=keywords%3Alivekit&size=250)

### 4.5 微信自有实时音频能力不是 LiveKit 客户端

微信小程序当前有四类相关能力。

#### RecorderManager

`RecorderManager.start()` 支持：

- `format: "PCM"`
- 8 kHz 到 48 kHz 采样率；
- 单/双声道；
- `frameSize`；
- `onFrameRecorded()` 返回 `ArrayBuffer`。

它适合把 PCM 分帧通过 WSS 发送给自有网关，但它不产生 LiveKit WebRTC track。

#### WebAudioContext

微信提供：

- `createBuffer()`
- `AudioBuffer.copyToChannel()`
- `createBufferSource()`
- 按时间调度 `BufferSourceNode.start()`

因此小程序可以接收服务端 PCM，建立短 jitter buffer 后连续播放；但 gapless、clock drift、前后台切换和 iOS/Android 差异必须真机验证。

#### live-pusher / live-player

官方当前文档明确：

- `live-pusher.url` 仅支持 RTMP；
- `live-player.src` 仅支持 FLV、RTMP；
- 二者需要匹配服务类目并开通权限；
- `RTC` 是组件的低延迟模式，不代表它实现标准浏览器 WebRTC 或 LiveKit protocol。

这组组件可与 LiveKit Ingress/Egress 组成流媒体桥，但不等于直接加入 LiveKit Room。

#### wx.joinVoIPChat

`wx.joinVoIPChat` 使用微信自己的 `groupId / nonceStr / signature / openId` 房间体系，没有配置任意 LiveKit SFU URL、participant token 或 media track 的接口。

它不能直接连接现有 LiveKit 房间。

## 5. 为什么不推荐 RTMP Ingress/Egress 作为主方案

LiveKit 官方 Ingress 支持 RTMP 推流进入房间，理论上可以形成：

```text
小程序 live-pusher → RTMP → LiveKit Ingress → Agent
```

但下行还需要：

```text
Agent → LiveKit Egress → RTMP/FLV → 小程序 live-player
```

问题是：

1. 多一轮音频编码、Ingress、Egress 和播放器缓冲；
2. `live-player` RTC 模式官方仍建议约 0.2–0.8 秒缓冲；
3. Egress 是独立 job，不适合每次短对话快速建立、取消和重连；
4. LiveKit reliable data、transcription、`voice-agent.ui` 和 telemetry 不会自动通过 RTMP；
5. barge-in 时的 duck、stop、generation 前移和实际播放状态更难对齐；
6. `live-pusher/live-player` 还受小程序服务类目和组件权限约束。

因此它更适合直播/单向播放，不适合 Memoria 的低延迟可打断语音。

来源：

- [LiveKit Ingress](https://docs.livekit.io/home/ingress/overview/)
- [LiveKit Egress](https://docs.livekit.io/home/egress/overview/)
- [微信 live-pusher](https://developers.weixin.qq.com/miniprogram/dev/component/live-pusher.html)
- [微信 live-player](https://developers.weixin.qq.com/miniprogram/dev/component/live-player.html)

## 6. 推荐的最薄媒体接入适配器

### 6.1 模块职责

新增一个 `MiniProgramMediaGateway`，只承担：

1. 验证短期、会话绑定的 gateway ticket 和 session ownership；
2. 为一个小程序连接加入一个现有 LiveKit room；
3. 把小程序 PCM 上行转换为 LiveKit local audio track；
4. 订阅 Agent remote audio track，并把 PCM 下发给小程序；
5. 双向转发必要的 LiveKit data、transcription、telemetry 和连接事件；
6. 处理 backpressure、断线、超时和资源释放。

明确不承担：

- STT、LLM、TTS；
- UtteranceRouter 或打断语义；
- ModePolicy、Digital Self、Legacy；
- Memory、Persona、Archive；
- 账号和权限判断；
- 服务端 response planning。

这样现有业务与语音智能主链保持单一事实源。

### 6.2 上行链路

```text
RecorderManager
  PCM16 / mono / 16 kHz 或 24 kHz
  frameSize 先从 1–2 KB 验证
        │ ArrayBuffer + seq + capture_ts
        ▼
WSS binary frame
        ▼
Gateway jitter/backpressure
        ▼
livekit.rtc.AudioSource.capture_frame()
        ▼
LocalAudioTrack.publish()
```

生产前必须确认：

- PCM 位深、字节序和不同微信客户端的一致性；
- iOS/Android 实际 frame callback 间隔；
- 录音与 WebAudio 播放能否同时稳定工作；
- 扬声器回声、耳机、蓝牙、系统通话中断；
- 前后台切换后是否能正确停止或恢复。

### 6.3 下行链路

```text
Agent remote audio track
        ▼
livekit.rtc.AudioStream(track)
        ▼
Gateway PCM frame + seq + playback_ts
        ▼
WSS binary frame
        ▼
WebAudioContext
  AudioBuffer.copyToChannel()
  BufferSourceNode.start(when)
```

小程序侧至少需要：

- 有界 jitter buffer；
- sequence gap 检测；
- 统一播放时钟；
- 旧 generation 音频丢弃；
- duck/restore/stop；
- session end 时清空已排程 buffer；
- 播放欠载和实际播放进度遥测。

### 6.4 控制与事件转发

当前 H5 不只消费音频，还依赖：

- `RoomEvent.TrackSubscribed`
- `RoomEvent.DataReceived`
- `RoomEvent.TranscriptionReceived`
- `RoomEvent.Reconnecting/Reconnected/Disconnected`
- `voice-agent.ui`
- `voice-agent.telemetry`

Gateway 应把它们映射为一个版本化的 WSS 协议，例如：

```text
session.ready
assistant.state
assistant.audio
transcript.delta
emotion.observation
speaker.rejected
rtc.reconnecting
rtc.recovered
rtc.closed
```

协议只转发 Agent 权威事件，不允许小程序自行升级 speaker、history、mode 或 generation。

### 6.5 为什么优先复用 Python RTC SDK

Memoria 已经固定使用 Python 3.12、`livekit-agents==1.6.5` 和 `livekit.rtc`。

LiveKit 官方 Python RTC SDK 已支持：

- `Room.connect(url, token)`
- `AudioSource(sample_rate, channels)`
- `AudioSource.capture_frame(frame)`
- `LocalAudioTrack.create_audio_track(...)`
- `AudioStream(remote_track)`

仓库现有 Agent 也已经使用 `rtc.AudioSource` 和 `LocalAudioTrack` 发布临时音频。

因此 PoC 不需要引入第二套 RTC SDK 或新的编程语言。

来源：

- [LiveKit Python SDK](https://github.com/livekit/python-sdks)
- [LiveKit Python SDK basic room example](https://github.com/livekit/python-sdks/tree/main/examples)
- 本仓库 `services/agent/src/agent.py`

## 7. 最大技术风险不是 LiveKit，而是小程序双讲

PCM/WSS Gateway 可以解决“连接 LiveKit”的问题，但它不会自动获得浏览器 WebRTC 已提供的：

- AEC；
- packet loss concealment；
- jitter adaptation；
- Opus congestion control；
- device switching；
- echo reference；
- media reconnect。

`RecorderManager` 没有浏览器式 `echoCancellation: true` 参数。Android 有 `audioSource: "voice_communication"`，iOS 没有完全对称的公开选项。

因此 Go/No-Go 的第一门槛不是“能听见声音”，而是：

> 小程序扬声器播放 Agent 音频时持续录音，是否会把远端声音重新送回 Agent，形成错误打断、重复识别或啸叫。

如果原生 PCM/WSS 在 iOS 或 Android 上无法得到可接受的 AEC，应停止继续堆补丁，改为：

1. 使用具有微信小程序 SDK 的成熟 RTC 厂商作为前端媒体层，再桥接到 LiveKit；或
2. 退回 H5 `web-view` 承载完整语音；或
3. 小程序只提供宣传、文字和按住说话，不提供全双工。

采用另一个 RTC 厂商并不等于找到 LiveKit 小程序客户端，它会形成“第二 RTC 房间系统 + 桥接”，成本和运维复杂度明显更高。

## 8. 建议 PoC 与验收门槛

建议先做 5–7 个工作日的隔离 PoC，不接入完整 H5 页面。

### 8.1 PoC 范围

1. 小程序创建现有 cascade session；
2. WSS 连接 Gateway；
3. Gateway 使用现有 participant token 加入 LiveKit；
4. 小程序 PCM 上行，Agent 能正常识别；
5. Agent 豆包 PCM 下行，小程序连续播放；
6. Agent 说话时麦克风保持开启；
7. “等一下”能够停止旧回答；
8. 断网恢复后调用现有 `rtc-recovered`；
9. `voice-agent.ui`、字幕和错误事件可见；
10. 会话结束后麦克风、WebAudio、WSS 和 LiveKit participant 全部释放。

### 8.2 必须真机验证

- iPhone 和 Android 各至少两种机型；
- 扬声器、听筒、有线耳机、蓝牙耳机；
- Wi-Fi、4G/5G、弱网、前后台切换；
- 微信语音/系统来电中断；
- 连续 10 分钟对话；
- 用户在 AI 播放时持续讲话；
- 回声、重复 ASR、啸叫、下行断裂和 buffer 欠载。

### 8.3 Go/No-Go 指标

建议至少满足：

- Gateway 额外首音频延迟 P95 不超过约 250 ms；
- 正常网络下无持续可感知播放断裂；
- 10 分钟会话无 PCM 累积漂移；
- barge-in 不播放旧 generation 尾音；
- 扬声器模式不形成稳定回声自循环；
- 断线后 generation、session ownership 和 mic state 一致；
- Gateway 断开后不遗留 LiveKit participant 或麦克风占用。

若 AEC 或 gapless playback 不通过，不应开始完整小程序 UI 复刻。

## 9. 对 Memoria 现有架构的影响

### 保持不动

- LiveKit Server；
- Control API 的账号、Profile、Memory、Persona、Digital Self、Legacy；
- Agent；
- FunASR、Qwen、豆包；
- UtteranceRouter；
- Generation Fence；
- `history_eligible`、speaker policy 和 Archive；
- H5。

### 最小新增

1. `apps/miniprogram/`；
2. 一个 `MiniProgramMediaGateway` 进程；
3. 一个版本化 WSS media/control protocol；
4. Control API 签发短期、会话绑定的 gateway ticket，或由 Gateway 安全取得 session participant token；
5. 小程序平台值和定向合同测试；
6. 真机音频与服务类目验收。

### 不建议

- 把 `livekit-client` 通过 DOM polyfill 强行打进小程序；
- 自己实现 WebRTC/ICE/DTLS/SRTP；
- 在小程序重写 UtteranceRouter、speaker policy 或 history eligibility；
- 首版同时引入 TRTC/Agora、RTMP Ingress/Egress 和自定义 PCM Gateway；
- 在没有 AEC 真机证据前承诺完整双工体验。

## 10. 最终建议

当前应按“没有可用的 LiveKit 微信小程序客户端”制定计划。

下一步不是直接建设完整适配器，而是：

1. 先建立 `RecorderManager PCM ↔ WSS ↔ LiveKit Python RTC ↔ WebAudioContext` 的隔离 PoC；
2. 用真机双讲和 AEC 结果决定是否继续；
3. PoC 通过后，把 Gateway 固化为纯媒体 adapter；
4. PoC 不通过时，再比较成熟小程序 RTC 厂商桥接和 H5 `web-view`，不要在 raw PCM 路线上继续堆规则。

这条路线最大程度保留现有后端和 LiveKit 投资，同时把新风险集中在一个可删除、可替换的媒体 seam 上。
