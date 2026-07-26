# 微信原生小程序实时双向语音：能力边界、故障推断与推荐架构

> 调研日期：2026-07-26
>
> 适用范围：原生微信小程序；`RecorderManager` PCM 上行、`WebAudioContext` PCM 下行、WSS 媒体网关、现有 LiveKit/FunASR/Qwen/TTS/记忆主链
>
> 约束：H5 保持不变；只采用微信、LiveKit、阿里云/FunASR 与 W3C 的一手文档或源码作为外部证据
> 相关决策：[ADR-0021：微信原生小程序通过受限媒体网关接入既有 LiveKit 房间](../adr/0021-wechat-miniprogram-media-gateway.md)

## 1. 结论

### 1.1 已确认

1. **微信当前公开 API 没有为 `RecorderManager + WebAudioContext` 提供可配置、可验证的 AEC（Acoustic Echo Cancellation，声学回声消除）。** `RecorderManager.audioSource=voice_communication` 只在 Android 可用，官方仅描述为“适用于实时沟通”，没有承诺启用 AEC，也没有公开回声参考流、尾长、收敛状态或残余回声指标。[微信 `RecorderManager.start`](https://developers.weixin.qq.com/miniprogram/dev/api/media/recorder/RecorderManager.start.html)；[微信官方 API typings](https://github.com/wechat-miniprogram/api-typings)
2. `WebAudioContext` 能创建内存 `AudioBuffer`、创建 `BufferSourceNode`，并以与 `currentTime` 相同的时间坐标调用 `start(when)`；它适合实现自有 PCM 播放队列，但微信文档没有给出网络流播放、抖动缓冲、时钟漂移或 AEC 保证。[微信 `WebAudioContext`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/WebAudioContext.html)；[微信 `BufferSourceNode.start`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/BufferSourceNode.start.html)
3. `InnerAudioContext` 的输入是一个 `src` 音频资源地址，不是持续写入的裸 PCM 环形缓冲。`wx.setInnerAudioOption({speakerOn:false})` 可以请求听筒播放，但官方明确说明它当前不兼容 `WebAudioContext`，也不兼容开启 `useWebAudioImplement` 的 `InnerAudioContext`。[微信 `InnerAudioContext`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/InnerAudioContext.html)；[`wx.createInnerAudioContext`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/wx.createInnerAudioContext.html)；[`wx.setInnerAudioOption`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/wx.setInnerAudioOption.html)
4. `live-pusher` 只接受 RTMP 推流地址，公开的音频处理开关是 AGC（自动增益）和 ANS（噪声抑制），没有公开 AEC 开关；`live-player` 只支持 FLV/RTMP，`RTC` 模式官方仍推荐 `min-cache=0.2s`、`max-cache=0.8s`。两个组件还需要服务类目审核和组件权限。[微信 `live-pusher`](https://developers.weixin.qq.com/miniprogram/dev/component/live-pusher.html)；[微信 `live-player`](https://developers.weixin.qq.com/miniprogram/dev/component/live-player.html)
5. LiveKit 的 AEC 必须同时获得麦克风输入和扬声器反向参考流；Python SDK 的 `AudioProcessingModule` 要求 10ms frame，并分别调用 `process_reverse_stream` 与 `process_stream`。小程序网关虽然不控制手机扬声器，却持有即将下发的 24k PCM 和手机回传的 16k PCM，因此可以在网关做一层**延迟可调、失败旁路的服务端回声缓解**。它缺少手机真实渲染时刻、音量、非线性失真和路由状态，不能被描述为与终端系统 AEC 等价。[LiveKit Python `media_devices`](https://docs.livekit.io/reference/python/livekit/rtc/media_devices.html)；[LiveKit Python SDK 源码与示例](https://github.com/livekit/python-sdks)
6. 因此，**在外放扬声器场景把“麦克风常开 + 助手持续播放”作为所有微信真机的稳定免提全双工能力，目前没有官方 API 依据。** 可以做受控双向流和可打断交互，但“免提真全双工”必须是按设备、路由和微信版本实测后开放的能力，而不是默认承诺。

### 1.2 推荐

保留现有 WSS PCM 媒体网关和全部 H5 主链，新增一个只对 `client.platform=miniprogram` 生效的 **`MiniProgramAudioPolicy`（小程序媒体/话轮控制面）**：

- 默认提供稳定的“助手播放 + 一键立即打断 + 按住说话/说完恢复”模式；耳机或通过真机矩阵验证的设备可升级为语音打断。
- Android 继续把 `voice_communication` 作为候选输入源，iOS 使用 `auto`，但两者都只视为 capability hint，不视为 AEC 已通过。
- 继续用 `WebAudioContext` 播放 PCM，但重写为**序号感知、代际感知、真正有界且不会重叠重排程**的播放器。
- 在独立网关复用现有 LiveKit WebRTC APM：下行只作为 reverse reference，上行清理后再发布到 LiveKit；初始化或处理失败时逐字节旁路，不影响 H5。
- 在网关增加“媒体帧”和“即时控制”两条逻辑通道：按钮打断不等待 VAD/STT；语音打断仍消费现有 FunASR partial 和 `UtteranceRouter`，不复制 LLM、TTS、记忆、权限或 Agent 状态机。
- H5 仍直接使用 LiveKit 客户端及其浏览器媒体链路；不修改 H5 播放、采集和打断策略。

### 1.3 本轮落地

- 小程序把连续 20 ms 下行聚合成 80 ms WebAudio source；1–3 帧缺口用短淡出/淡入的静音补偿保持时间轴，generation/barrier 或更大缺口才停止全部旧 source。
- generation-aware PCM v2 只对声明能力的新客户端启用；旧客户端继续收 v1。权威 `assistant_state=speaking` 结束旧代际 quarantine，首个新代际 frame 直接进入带淡入的客户端 barrier，不再固定裁掉 20 ms。
- 独立 `MiniProgramAudioProcessor` 复用 LiveKit WebRTC APM；只把 WebSocket 已确认发送的下行送入 reverse stream，处理失败旁路原始上行。generation reset 清 reference 时序但保留健康 APM 已学习的回声路径。
- 打断后的 `memoria-ack` 临时轨以当前 generation 单独放行，旧主音轨仍受 barrier 拦截。
- AI 讲话时提供“轻触打断”：先在本机 5 ms 淡出并拒绝当前 generation 的迟到帧，再调用既有 Control API。客户端仅回传 `playout_interrupt / playout_reset` 非权威事实，网关不开放业务控制命令。
- H5、Agent、FunASR、Qwen、TTS、SpeakerAuthority、`UtteranceRouter`、记忆与权限主链均未复制。

## 2. 各候选方案比较

| 方案 | 原始 PCM | 官方 AEC 保证 | 预期打断时延 | 与现有 LiveKit/Agent 复用 | 结论 |
|---|---:|---:|---:|---:|---|
| `RecorderManager + WSS + WebAudioContext` | 上下行均可 | 无 | 可低，但取决于回声门禁、ASR 与播放队列 | 最好 | 保留；增加小程序专属媒体政策与调度修复 |
| `RecorderManager + InnerAudioContext` | 上行可；下行不是连续裸 PCM sink | 无 | 分段资源准备/切换可能增加时延 | 可 | 只作为听筒/整段音频降级实验，不作流式主链 |
| `live-pusher + live-player + RTMP` | 组件不向业务层暴露所需 PCM | 未公开；仅 AGC/ANS | 播放器缓冲本身建议 0.2–0.8s，另有转码/桥接 | 需 Ingress/Egress | 不适合低延迟可打断主链 |
| 微信 `joinVoIPChat / voip-room` | 不向业务层暴露房间 PCM | 未公开 AEC 参数；平台托管实时通话 | 平台托管 | 不能加入任意 LiveKit room | 是真正的微信房间双向通话，但不是本项目媒体接口 |
| 半双工 / push-to-talk | 可 | 不需要 AEC | 按钮打断可立即；语音开始由用户显式控制 | 完全复用 | 最可靠默认/降级模式 |
| 独立小程序 Agent | 可 | 仍无终端 AEC | 可自定义 | 容易复制状态 | 不推荐复制 Agent；只分离平台媒体/控制适配层 |

### 2.1 继续使用 WebAudio PCM bridge

**已确认：**

- `RecorderManager` 支持 `format: "PCM"`、`frameSize`，`onFrameRecorded` 返回帧数据；16 kHz、16 bit、单声道与当前 FunASR 上行契约一致。[微信 `RecorderManager.start`](https://developers.weixin.qq.com/miniprogram/dev/api/media/recorder/RecorderManager.start.html)；[`RecorderManager.onFrameRecorded`](https://developers.weixin.qq.com/miniprogram/dev/api/media/recorder/RecorderManager.onFrameRecorded.html)
- 当前客户端设置 `frameSize: 1KB`。对 16 kHz、16 bit、单声道 PCM，约为 32ms 音频；网关再重分帧成 20ms LiveKit `AudioFrame`。[当前 `media-gateway.js`](../../apps/miniprogram/utils/media-gateway.js)；[当前 `bridge.py`](../../services/miniprogram_gateway/bridge.py)
- 当前下行是 24 kHz、16 bit、单声道、20ms，一秒约创建并调度 50 个 `BufferSourceNode`。[当前 `bridge.py`](../../services/miniprogram_gateway/bridge.py)；[当前 `pcm-player.js`](../../apps/miniprogram/utils/pcm-player.js)

**推断：**

- 该路径是所有候选中最容易保留低延迟、现有 LiveKit 房间、data topic、字幕和 generation fence 的方案；主要问题不是 WSS 能否传 PCM，而是终端没有可证明的 AEC，以及当前播放器的拥塞/重排程策略。
- Android `voice_communication` 可能选择更适合通话的系统采集路径，但不能从“接口调用成功”推导出回声已消除。iOS 的 `auto` 更没有公开的通话模式/AEC 控制。

**待真机验证：**

- 录音和 WebAudio 外放同时运行时，iOS/Android 不同机型是否发生录音静音、系统抢占、采样率变化、蓝牙切路或硬件 AEC 差异。
- `wx.getAvailableAudioSources()` 实际返回值以及请求 `voice_communication` 后的真实采集源；不能只记录 start 成功。
- 外放、听筒、有线耳机、蓝牙四种路由分别测残余回声、首帧和打断时延。

### 2.2 改用 InnerAudioContext

**已确认：**

- `InnerAudioContext.src` 是资源地址；它有 `play/pause/stop/seek`、`onCanplay/onWaiting` 等媒体资源式接口，没有 `enqueue PCM` 或按音频时钟写入样本的接口。[微信 `InnerAudioContext`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/InnerAudioContext.html)
- `useWebAudioImplement=true` 只被官方推荐给短音频/频繁播放以改善性能，同时会增加内存；它仍未把 `InnerAudioContext` 变成裸 PCM sink。[`wx.createInnerAudioContext`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/wx.createInnerAudioContext.html)
- `speakerOn=false` 可请求听筒播放，但与本项目现用 `WebAudioContext` 不兼容。[`wx.setInnerAudioOption`](https://developers.weixin.qq.com/miniprogram/dev/api/media/audio/wx.setInnerAudioOption.html)

**推断：**

- 把每几十毫秒 PCM 写成 WAV 临时文件或 data/resource URL，再连续切换 `src`，会把稳定性问题从 WebAudio 排程换成资源创建、解码、`onCanplay` 和切换缝隙，通常更难做到低延迟无缝播放。
- 它的现实价值是一个**听筒降级实验**：服务端把一句或较大块 TTS 作为可播放资源，下行不追求逐 20ms 流式；听筒物理隔离可能显著降低回灌。但这牺牲首音、连续流式和快速 generation flush，不能替代当前主链。

### 2.3 live-pusher / live-player / RTMP

**已确认：**

- 微信 `live-pusher.url` 仅支持 RTMP；`live-player.src` 仅支持 FLV/RTMP。组件的 `RTC` 是低延迟模式名，不是浏览器 `RTCPeerConnection`，也不是 LiveKit participant。[微信 `live-pusher`](https://developers.weixin.qq.com/miniprogram/dev/component/live-pusher.html)；[微信 `live-player`](https://developers.weixin.qq.com/miniprogram/dev/component/live-player.html)
- `live-pusher` 提供 `enable-agc` 与 `enable-ans`，没有公开 `enable-aec`；“噪声抑制”不能等同于“播放回声消除”。
- `live-player` 在 RTC 模式仍推荐 0.2s 最小缓存和 0.8s 最大缓存，并明确说明缓冲越大，抗网络波动越好但时延越大。
- LiveKit Ingress 可把 RTMP 转码并发布到房间；Egress 可把房间/participant 转码输出 RTMP，这都是独立服务与转码路径。[LiveKit Ingress](https://docs.livekit.io/home/ingress/overview/)；[LiveKit Egress 输出](https://docs.livekit.io/transport/media/ingress-egress/egress/outputs/)

**推断：**

- 双向至少变成“小程序推流 → RTMP Ingress → LiveKit → Agent”和“Agent → Egress → RTMP/FLV player”；额外编码、转码、播放器缓存和 job 生命周期会直接侵蚀“等一下”的停止速度。
- LiveKit data topic、transcription、generation 和实际播放位置仍需另一条控制链，形成两套同步问题。
- 即使底层实现内部存在某些设备 3A 行为，公开契约也不足以把 AEC 作为生产保证；不值得仅为一个未承诺的能力引入整条 RTMP 双向桥。

### 2.4 微信自有 joinVoIPChat / voip-room

**已确认：**

- `voip-room` 的官方功能是“多人音视频对话”，通过 `wx.joinVoIPChat` 加入微信管理的实时语音房间；它需要特定服务类目审核和组件权限。[微信 `voip-room`](https://developers.weixin.qq.com/miniprogram/dev/component/voip-room.html)；[`wx.joinVoIPChat`](https://developers.weixin.qq.com/miniprogram/dev/api/media/voip/wx.joinVoIPChat.html)
- 加房契约使用 `groupId / nonceStr / timeStamp / signature`，组件以成员 `openid` 显示画面；没有任意 LiveKit URL、participant token、发布自定义 PCM track 或订阅服务端 PCM 的接口。
- 这是所核接口里真正由微信托管的双向实时通话能力，但官方文档仍没有向应用暴露 AEC 参数或可验收指标。

**推断：**

- 如果产品目标是人与人微信房间，它比拼接 `RecorderManager + WebAudioContext` 更接近平台 RTC；但 Memoria 需要服务端 Agent 作为媒体 participant、消费 PCM 并维持 LiveKit data/generation 语义，公开接口无法把该微信房间桥给现有 Agent。
- 除非微信未来提供服务端 bot/媒体接入或可验证的 LiveKit 互通接口，否则它不能替代 ADR-0021 的媒体网关。

### 2.5 半双工 / push-to-talk

**已确认：**

- 阿里云实时交互协议把 `push2talk` 定义为客户端控制开始/结束音频，`duplex` 定义为持续上传并允许语音打断；官方同时建议持续模式按约 100ms 间隔发送音频，过长或过短都会影响延迟和处理效率。[阿里云实时多模态交互协议](https://help.aliyun.com/zh/model-studio/multimodal-interaction-protocol)；[阿里云服务端 SDK 模式比较](https://help.aliyun.com/zh/model-studio/server-go-sdk)
- 当前 Memoria FunASR 适配器按 80ms 向服务商发送 PCM，与上述建议量级一致；`max_sentence_silence_ms` 默认 550ms，但流式 partial 可早于 final 返回。[当前 `funasr_stt.py`](../../services/agent/src/providers/funasr_stt.py)

**推断：**

- 半双工不是最自然的最终体验，但它从物理上消除了助手外放期间的自回声输入，是当前公开 API 下最稳定、可解释的生产基线。
- 最好的交互不是“等助手说完再录音”，而是：用户按下打断键时客户端立即停止已排程音频，同时通知服务端取消 generation；按住期间上传 PCM，松开后提交话轮。这样按钮到本地静音不需要等待 WSS、VAD、STT 或 speaker classification。

### 2.6 单独的小程序 Agent / 控制路径

**推断：**

- 完整复制一个 Mini Program Agent 会复制 Generation Fence、UtteranceRouter、speaker policy、history eligibility、工具权限和归档语义，长期最容易与 H5 分叉；它也不能解决终端没有 AEC。
- 应分离的是**平台适配控制面**，不是业务 Agent：
  - 媒体面仍由 `MiniProgramMediaGateway` 桥接同一个 LiveKit room；
  - `interrupt.request`、`playout.started/stopped`、`buffer.underrun/reset` 走受信任的版本化控制事件；
  - 显式按钮打断直达现有 generation/stop 逻辑；
  - 语音候选仍由现有 FunASR partial、speaker policy 和 `UtteranceRouter` 决策。
- 只有量测证明“现有 LiveKit → FunASR partial”本身是主要瓶颈时，才考虑在网关增加一个仅识别控制短语的 FunASR control lane。它必须复用同一 STT adapter/model，只产出候选事件，不拥有聊天上下文、LLM、TTS 或记忆。

## 3. 回声、卡顿/静电声与晚打断的根因假设

以下是基于公开契约和当前代码的工程推断，不是真机根因定案。

### 3.1 自播放回声导致假 barge-in

**最可能链路：**手机扬声器播放 24k PCM → 物理回灌到麦克风 → `RecorderManager` 采到混合信号 → WSS/LiveKit → VAD/ASR 把助手声音视为用户活动。

网关同时拥有下行原始 PCM 与混合后的上行 PCM，可以把前者作为 WebRTC APM 的 reverse stream，降低纯播放回声；但它仍不知道手机真实渲染时刻、音量、扬声器非线性、房间脉冲响应和系统路由，因此只能称为服务端回声缓解，不能宣称做出了终端级 AEC。[LiveKit `media_devices`](https://docs.livekit.io/reference/python/livekit/rtc/media_devices.html)

可以做的防护是：

1. 播放期不再用单一能量/VAD 直接停止助手；VAD 只进入 `interruption_pending`。
2. 网关把对应 generation 的下行参考送入独立 WebRTC APM；处理异常必须 fail-open，延迟与活跃窗口必须由真机时间线校准。
3. ASR partial 与助手已播文本高度重合时优先判为 echo；明确的 `等一下/停一下` 且不属于下行文本时走现有 Router 快路径。
4. 未验证设备默认使用按钮打断/PTT；耳机、听筒或白名单设备才开放 hands-free voice barge-in。

### 3.2 卡顿与静电声

**已确认的修复前实现：**

- 网关每 20ms 下发一个 PCM frame；客户端每帧创建一个 `AudioBuffer` 和一次性 `BufferSourceNode`。[当前 `bridge.py`](../../services/miniprogram_gateway/bridge.py)；[当前 `pcm-player.js`](../../apps/miniprogram/utils/pcm-player.js)
- 当 `nextStartAt < now` 时，播放器把下一块重置为 `now + 80ms`，会产生可听空隙；当排程领先超过 450ms 时，它也重置时间轴，但**没有同时停止已在未来排程的旧 source**，新旧 source 可能重叠播放。
- 协议携带 sequence/timestamp，但客户端解码后只把 payload 交给播放器；下行丢帧、迟到、乱序、队列跳帧都没有进入播放决策或遥测。[当前 `media-protocol.js`](../../apps/miniprogram/utils/media-protocol.js)；[当前 `media-gateway.js`](../../apps/miniprogram/utils/media-gateway.js)
- 网关音频队列默认 100 个 20ms frame，即最多约 2 秒；满时静默丢最旧帧。对直播可以接受的缓存，对可打断对话过大。[修复前 `config.py`](../../services/miniprogram_gateway/config.py)；[`bridge.py`](../../services/miniprogram_gateway/bridge.py)

**推断：**

- 欠载造成 gap/click，超前重置造成重叠/爆音，是当前“卡顿/静电声”的高优先级软件假设。
- W3C 规定 `currentTime` 按渲染 quantum 前进，`AudioBufferSourceNode.start(when)` 可按该时钟排程；默认 render quantum 为 128 frame。规范还指出，需要对网络/磁盘资源做 sample-accurate 播放时应使用 `AudioWorkletNode`。微信公开 `WebAudioContext`/typings 未暴露标准 AudioWorklet，因此只能在 JS 控制线程上实现更稳健的有界调度。[W3C Web Audio API 1.1](https://www.w3.org/TR/webaudio-1.1/)

**建议播放器策略：**

1. 网关仍以 20ms 获取/传输，客户端聚合为 60–100ms `AudioBuffer` 后再排程，减少节点与控制消息频率。
2. 维护期望 sequence；发现 gap、generation 变化或超过最大 lead 时，`stop all → clear → 从新 barrier 重启`，禁止只改 `nextStartAt` 后让旧 source 留在未来。
3. 初始目标 lead 从 100–160ms 开始，按真机 underrun/lead 分布自适应；对话硬上限建议先验证 250–400ms，不能保留 2 秒陈旧音频。
4. 丢弃必须显式产生 `buffer.reset/drop` 遥测，并携带 generation 和 barrier sequence；旧 generation 永不进入新时间轴。
5. reset 边界做极短淡入/淡出或零交叉处理，降低 PCM 不连续带来的 click；不得用交叠旧音频掩盖断点。

### 3.3 2026-07-26 生产真机时间线

同一台真机的两次助手播放分别在服务端 `playback_started` 后约 0.836 秒和
0.896 秒出现 `barge_in_detected`；第二次从候选插话到真正进入 `interrupted` 又耗时约
1.710 秒。相同会话中豆包首 PCM 约 0.23 秒到达，说明“两字后停播”和“等一下像没反应”
首先应排查自回声与打断保护等待，而不是把主要责任归给 LLM/TTS 首包。

网关 WSS 抽样的 20ms 下行帧未发现 sequence 丢失或削波；因此本轮反馈环分别锁定：

1. 客户端连续 20ms `BufferSourceNode` 排程与旧 source 残留；
2. 网关缺少下行 reverse reference 驱动的 AEC；
3. AEC 清理后的近端控制语是否能让既有 `UtteranceRouter` 快路径提前确认。

### 3.4 真正的“等一下”为什么仍可能晚

当前 FunASR adapter 已经发送 80ms chunk，并能把 provider interim 映射为 partial/preflight；代码也已对 Router 确认的显式打断词尝试在 final 之前触发。因此不能笼统把延迟归因于“必须等最终 ASR”。阿里云官方持续双工建议的 100ms 发送粒度也说明 80ms 本身不是异常的大块。[阿里云实时多模态交互协议](https://help.aliyun.com/zh/model-studio/multimodal-interaction-protocol)；[当前 `funasr_stt.py`](../../services/agent/src/providers/funasr_stt.py)

开源 FunASR 官方 README 把 `chunk_size` 明确定义为流式时延配置，并用 600ms 推理块举例；其 WebSocket runtime 也暴露 `chunk_size/chunk_interval`。但 Memoria 当前接的是 DashScope `fun-asr-realtime` 协议并自行按 80ms 发送，**不能把开源示例的 600ms 直接当成当前线上等待时间**。[FunASR 官方 README](https://github.com/modelscope/FunASR/blob/main/README.md)；[FunASR WebSocket runtime](https://github.com/modelscope/FunASR/blob/main/runtime/python/websocket/README.md)

**仍可能累积的阶段：**

```text
真机声学起点
→ RecorderManager 首个含人声 frame
→ WSS send / gateway receive
→ LiveKit AudioSource 排队与 Agent VAD start
→ FunASR 首个包含“等一下”的 partial
→ PlaybackInputGuard / speaker authority / UtteranceRouter
→ LiveKit stop + gateway audio_reset
→ 小程序 stop 已排程 source
→ 真机实际静音
```

LiveKit `AudioSource` 的 `queue_size_ms` 是真实内部队列上限；队列满时 `capture_frame` 会等待。当前网关上行设置为 200ms，因此也必须记录 `queued_duration` 或等价等待时间，不能只看 WSS RTT。[LiveKit Python `AudioSource`](https://docs.livekit.io/reference/python/livekit/rtc/audio_source.html)

**推断：**

- 如果 FunASR partial 很早而实际停止晚，问题在 Router 后的 stop/control/客户端排程。
- 如果 VAD start 已晚，优先查真机录音与播放并发、Recorder 回调抖动、WSS 上行或 LiveKit AudioSource 排队。
- 如果 VAD start 早但 partial 晚，才查 FunASR provider 首包、短词 partial 稳定规则与 speaker authority 等待。
- 自回声可能让 guard 长时间处于候选状态，使真正的人声与旧 echo 混在同一候选 epoch；这是需要平台控制面而不是继续加阈值的典型碰撞。

## 4. 推荐架构：受控双工，而非复制一套后端

```text
H5（保持不变） ───────── LiveKit Browser SDK ─────────┐
                                                     │
微信小程序                                            ▼
  RecorderManager ─┐                         现有 LiveKit Room
  WebAudioContext ◀┼─ WSS ─ MiniProgramMediaGateway ─ 现有 Agent
  本地立即打断按钮 ─┘        ├─ WebRTC APM             ├─ FunASR
                             └─ MiniProgramAudioPolicy ├─ Qwen / TTS
                                媒体队列、能力档案、    ├─ UtteranceRouter
                                playout ack、control    └─ 权限 / 记忆 / 归档
```

### 4.1 `MiniProgramAudioPolicy` 职责

- 输入：OS、微信版本、设备型号、音频路由、是否耳机/蓝牙、真机 capability probe、播放 generation。
- 输出：`ptt`、`tap_interrupt_then_talk`、`validated_voice_barge_in` 三种模式之一。
- 只决定媒体采集、播放、打断入口和降级；不决定 speaker 身份、历史资格、工具权限或回复内容。
- 默认 fail-safe：未知/外放设备使用显式打断；白名单必须按版本与路由过期，避免一次验收永久放行。

### 4.2 两条逻辑通道

1. **媒体通道**：PCM + sequence + capture/playout timestamp + generation；允许有界丢帧，但每次丢帧必须形成 barrier/reset。
2. **控制通道**：`interrupt.request`、`audio.reset`、`playout.started/stopped`、`buffer.underrun/overrun`、`route.changed`；优先级高于音频队列。

按钮打断时，小程序先本地 stop source，再发送带当前 generation 的 `interrupt.request`。服务端以 generation fence 保证迟到 TTS 不复活；网关清空下行队列并回 `audio.reset`。这条路径是可测量的硬实时用户控制，不应该等待语音识别。

### 4.3 语音打断策略

- 播放期 VAD 只 duck，不直接 cancel。
- FunASR partial 出现 Router 明确控制词时走快速确认；普通 overlap 继续等待现有自适应打断/最终话轮。
- 下行参考相关性、助手文本重合、speaker authority 都是证据；任何单个证据都不被提升为业务身份。
- 真机能力未通过时，UI 明确显示“轻触打断后说话”，不要让用户以为外放免提已经可靠。

## 5. 验收计划与仍未验证的事实

### 5.1 必须新增的时间点

每个事件携带同一 `session_id / turn_id / generation_id / uplink_seq / downlink_seq`：

- `device.capture_voice_start`（可用离线波形人工标注校准）
- `recorder.frame_callback`
- `gateway.uplink_received`
- `livekit.capture_begin/end` 与排队等待
- `agent.vad_start`
- `funasr.first_partial`、`router.interrupt_confirmed`
- `server.playback_stop_requested`
- `gateway.audio_reset_sent`
- `client.audio_reset_received`
- `client.sources_stopped`
- `device.silence_observed`（外部录音或回采验收）

### 5.2 真机矩阵

- iOS：至少两个主版本、两代设备；Android：至少微信主流低/中/高三档与两家厂商。
- 路由：外放、听筒、有线耳机、蓝牙；音量 30%/70%/100%。
- 网络：稳定 Wi‑Fi、蜂窝、50/100/200ms RTT、抖动与短时丢包。
- 生命周期：锁屏、前后台、电话/语音消息抢占、蓝牙接入/断开、音频权限撤销。
- 内容：纯助手回声、用户“等一下”、用户长句 overlap、背景电视、人声音乐、近场/远场、访客说话。

### 5.3 通过门槛（建议初始值，待产品确认）

- 显式按钮：按下到本机静音 P95 ≤ 100ms；到服务端 generation 取消 P95 ≤ 250ms。
- 白名单语音打断：“等一下”声学起点到本机静音 P50 ≤ 500ms、P95 ≤ 900ms。
- 助手单独外放 5 分钟：零错误 cancel；回声 transcript 不进入 chat/history。
- 连续播放 10 分钟：无可闻重叠/爆音；underrun/reset 率有明确阈值并可从日志定位。
- 任何后台切换或 audio interruption 后：要么自动恢复且 generation 正确，要么明确降级到轻触恢复；不得静默失聪。

这些数值不是官方平台保证，只是建议的产品验收线。必须由同一台真机的外部录音、客户端事件、网关日志和 Agent 事件共同证明。

## 6. 决策建议

1. **不改 H5，不换核心语音栈，不上 RTMP 双向桥。** 保留 ADR-0021 的 PCM WSS gateway 边界。
2. **先修播放时钟与队列语义。** 当前旧 source 重叠、sequence 未消费和 2 秒队列，是比调 VAD 阈值更直接的卡顿/晚停风险。
3. **把小程序定义为“受控双工”。** 可靠默认是一键本地打断 + PTT；真机白名单再开放外放语音 barge-in。
4. **只新增平台媒体/控制适配层，不复制 Agent。** 所有语义、身份、权限、记忆和 generation 继续由现有权威链决定。
5. **先做端到端时间线，再决定是否需要 FunASR control lane。** 当前已有 80ms 上送和 partial 快路径，没有证据前不应复制 ASR 会话。

最终边界是：微信原生小程序可以实现低延迟双向 PCM 和自然的可打断体验，但在平台没有公开 AEC 控制的前提下，不能把外放免提“真全双工”当成跨设备不变能力。稳健方案必须同时拥有平台能力档案、即时显式控制、下行参考门禁、严格的播放队列和真机证据。
