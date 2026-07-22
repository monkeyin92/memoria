# 全双工语音 Agent、情绪与 Omni A/B 实施计划

> **历史计划（已被替代）**：本文记录 2026-07-17 以前包含 Web/iOS/Omni A/B 的历史实施过程。当前唯一交付客户端为 `apps/h5`，主链固定为 FunASR Realtime + Qwen + 豆包 Seed-TTS 2.0；当前范围、状态与门禁以 [`memory-persona-implementation-plan.md`](./memory-persona-implementation-plan.md) 和根目录 [`HANDOFF.md`](../HANDOFF.md) 为准。原生 iOS 与 legacy `apps/web` 不再进入实现、CI、部署或验收。

## 成功标准

1. `GenerationFence` 对 LLM、TTS、工具结果做完整字段校验，旧音频与旧工具结果为 0。
2. “停止回答”从 Web/iOS 经控制 API 到 Agent 执行同一原子取消流程。
3. 助手历史只使用 `HeardTextTracker` 的实际已听文本；播放进度来自真实媒体事件。
4. FunASR、DeepSeek、CosyVoice 的真实 smoke、超时、取消、重连和资源回收可执行。
5. Web 与 iOS 均支持语音、字幕、状态、打断、停止、重连、麦克风控制。
6. Python、Web、iOS 构建与测试通过；覆盖率满足规范离线门槛。
7. 首声链路可从 TTS 首包追踪到浏览器实际播放，单次 trace 能定位首个失败环节。
8. 候选打断先 duck 再判定；独立 listener cue 不进入主回答、上下文或用户历史。
9. 情绪识别保留 FunASR 主链，只以非阻塞、短 TTL、不持久化的 Qwen3-ASR 旁路提供观察量。
10. CosyVoice 只使用受控情绪子集；策略失败回退 `neutral`，负向情绪不机械镜像。
11. H5 可在现有级联与 Qwen3.5-Omni 间切换；长期密钥只在服务端，两个后端生命周期彼此隔离。

## 当前结论（2026-07-17）

- 标准 1–5 的代码路径和离线回归已完成。
- Python、Web、iOS App 与 iOS 测试 bundle 均编译通过；iOS App 已在 iPhone 17 Pro 模拟器安装、启动并完成 UI/麦克风权限/控制 API 交互。
- XcodeBuildMCP scheme 测试执行被 macOS 27 + Xcode 26.6 的 destination 解析问题阻塞。
- DeepSeek、LiveKit、FunASR、Qwen、CosyVoice 实网 smoke 均通过；200 条录音、AEC 矩阵和 SLO 尚未完成，故仍不满足规模化上线 Definition of Done。
- P0–P5 的本地代码与自动化回归已完成：首声 trace、duck-first、独立 listener cue、Qwen3-ASR 情绪旁路、受控 CosyVoice 情绪输出和 Qwen3.5-Omni A/B。
- H5 默认仍为现有级联；Qwen3.5-Omni 通过浏览器 WebRTC 直连百炼媒体面，Control API 只代理 SDP 并保存服务端凭据。
- 上一轮验证为 Python `239 passed`、H5 `51 passed`，并部署为 runtime/H5 `20260716-225754`；该轮已完成真实 WebRTC SDP、远端音轨与 `session.updated` 验证。
- H5 `20260717-001826` 与最终 runtime 候选 `20260717-003211` 已完成本地门禁：级联 endpoint 从 0.90s 调为 1.50s，假打断恢复同步为 1.70s；Omni 首次 ready 主动欢迎且只触发一次，用户抢话覆盖 pending/active/迟到 created 三种竞态，远端音轨和 H5 音频元素双层去重。
- 两条链路已增加 5 秒一次的 inbound RTP 数值采样；Omni 事件时间线通过所有权校验的脱敏 API 上报，Schema 拒绝文本、音频、SDP 和非白名单字段。长期低延迟方向仍是 pre-EOU speech epoch，等待真实 P95/P99 timing 数据后实施。
- 当前本地验证为 Python `243 passed`、Agent `204 passed`、H5 `57 passed`、Web `28 passed`，Ruff、mypy strict、Web/H5 production build 全部通过。
- 2026-07-17 08:15 CST 真人复测确认 Omni 4 个真人话轮均一一对应，speech stop 到 response created 为 68–139ms；级联仅 2 次真实 speaking 却提交 6 个 turn，4 个孤儿 turn 的 speaking/timing anchors 全空。`20260717-084049` 已用 pre-EOU 锁、三态 speech anchor 门禁与 FunASR sentence ID 去重完成确定性修复；端点参数保持不变。
- `20260717-084049` 已于 2026-07-17 08:55 CST 发布为 runtime/H5；两容器 healthy，LiveKit、FunASR、Qwen、CosyVoice 真实 readiness 全部 PASS，公网 ready 绑定 `20260717-084049 / qwen`。LiveKit 日志已收紧为 `warn`，浏览器模型切换、刷新保持与 0 console warning/error 已验证；同 iPhone 真人 A/B 仍是下一项门禁。
- `20260717-111241` 已完成条件式思考开场、笑声安全语境、Qwen3-ASR 多轮证据与 CosyVoice 合法情绪/语速收紧。
- `20260717-113441` 已发布当前话轮 DeliveryPlan：级联按 `direct / deliberative / light_laughter / supportive` 选择一次性 LLM 指令、合法 CosyVoice emotion 与整轮 rate；Omni 增加正反例和自然笑声失败回退。当前门禁为 Python `257 passed`、Agent `218 passed`、H5 `57 passed`、Web `28 passed`，Ruff、mypy strict、双 production build 和全部实网 provider smoke 通过。
- `20260717-123551` 已发布本轮音质与生命周期修复：级联 Opus 上限 64 kbps；Omni 增加 400 ms 欢迎观察窗、pending/active/cancelled 分离、500 ms 尾音反馈保护、唯一音频元素静音门控和浏览器支持时的 120 ms jitter-buffer target。门禁为 Python `260 passed`、H5 `61 passed`、Web `28 passed`，Ruff、mypy strict、双 production build、全部实网 Provider/readiness 和公网浏览器配置验收通过；首词、滋滋声与立即接话仍待用户同设备复测。

## 实施阶段

### 1. 生产链路正确性

状态：完成。

- 修复完整 Fence、实际已听历史和原子取消。
- 将控制 API 的停止请求路由至目标 LiveKit room/Agent。
- 验证：核心回归测试、100 次旧 generation/tool epoch 隔离。

### 2. Provider 与可靠性

状态：代码与 mock 完成；DeepSeek 实网完成，DashScope 实网待执行。

- 完成真实 provider smoke、连接生命周期、超时、重连与 breaker。
- 提供真实 Prometheus exporter，并消除 pending task。
- 验证：协议集成测试、provider smoke（密钥存在时）、coverage。

### 3. Web 客户端

状态：完成。

- 接通远端音轨与 LiveKit Data 事件，修复停止/字幕/重连。
- 修复 frozen-lock Docker 构建。
- 验证：Vitest、lint、production build。

### 4. iOS 客户端

状态：App 编译与模拟器运行完成；测试 bundle 编译完成，scheme 测试执行受本机 Xcode 阻塞。

- 使用 SwiftUI + LiveKit Swift SDK，实现会话、麦克风、音频、字幕、状态、停止和重连。
- 密钥只留服务端；iOS 只获取短期 participant token。
- 验证：XcodeBuildMCP 模拟器 build/test/run 与 UI 快照。

### 5. 集成验收

状态：离线门禁完成；真实设备与 SLO 验收待执行。

- 运行规范离线门禁，核对真实 provider smoke 和未具备的设备/SLO 条件。
- 更新追踪矩阵与 `HANDOFF.md`，明确可发布与剩余外部条件。

### 6. P0：首声可观测

状态：本地代码与自动化回归完成；50 次真实会话门禁待执行。

- 串联 TTS task、首个非零 PCM、Agent 发布、远端音轨订阅、浏览器 `play()` 与 `playing` 事件。
- H5 将客户端首声事件回传 Agent，与同一 session/turn/generation 关联。
- CosyVoice 首包超时会取消当前连接并用已缓冲文本重试，避免无声长挂。

### 7. P1：duck-first 打断

状态：本地代码与自动化回归完成；真实 AEC 设备矩阵待执行。

- 候选打断先将助手音量降至 25%，确认真打断后原子停止，附和或回声则平滑恢复。
- 保留 GenerationFence、实际已听文本和旧输出隔离，不用硬暂停代替判断。

### 8. P2：独立 listener cue

状态：策略、独立取消域与回归测试完成；授权 cue 素材和真实主观测试待执行。

- `CueScheduler` 负责延迟、冷却、每轮上限、禁用场景和熔断。
- cue 不进入 LLM 回答、对话上下文或持久化历史，不作为“用户已听到的正式回答”。

### 9. P3：情绪旁路观察

状态：本地代码与自动化回归完成；300 条真实中文话轮校准待执行。

- 同一份 PCM 继续由 FunASR 提供主转写和时间戳，同时非阻塞旁路到 `qwen3-asr-flash-realtime`。
- 情绪观察量带来源、TTL 和保守平滑，不写入长期对话历史，也不向用户展示确定性标签。

### 10. P4：受控情绪化输出

状态：本地代码与自动化回归完成；真实听感与额外首包延迟待执行。

- 每个 generation 生成 `SpeechPlan`，仅将安全子集映射到 CosyVoice 合法 Instruct 与语速。
- `angry/disgusted/fearful` 默认不直接镜像为输出情绪，任何非法或失败策略回退 `neutral`。
- 未新增 MiniMax；笑声、叹息、咳嗽等副语言未做自由生成，待有授权素材和场景白名单后再评估。

### 11. P5：Qwen3.5-Omni 隔离 A/B

状态：代码、自动化回归、真实百炼 WebRTC 信令、生产发布和公网浏览器配置验收已完成；本轮修复后的真人音频交互与同设备 A/B 待执行。

- 页面提供“现有级联 / Qwen3.5-Omni-Flash”选择，默认级联、刷新持久化，连接和通话期间锁定。
- Omni 使用 `qwen3.5-omni-flash-realtime`、`semantic_vad`；收到 `session.updated` 前保持麦克风轨道关闭。
- H5 直接收发 RTP 音频和字幕；支持静音、停止、结束与迟到事件隔离。助手事件按 `response_id + item_id` 隔离，用户 `speech_started/stopped` 与转写 delta/completed 按同一 `item_id` 绑定话轮；未知项丢弃，A 的迟到 completed 不会覆盖或结束 B。Omni 助手字幕标记 `heard=false`，不冒充已有实际播放证据写入历史。
- 主动欢迎先等待 400 ms 安静观察窗；未知 pending response 不提前 cancel，按 response ID 去重取消并隔离迟到 done。正常回答完成后用 500 ms 实验性反馈保护抑制已观测的尾音反馈轮；浏览器支持时设置 `jitterBufferTarget=120 ms`，不支持时回退。
- Control API 不签发 LiveKit token、不派发级联 Agent，仅校验身份/会话所有权后代理 SDP；固定百炼地域、模型和 Workspace 主机，禁止重定向及环境代理，并限制媒体类型、请求体、超时与每会话交换次数。
- 未新增 MiniCPM 或 MiniMax。

### 12. P6：当前话轮自然表达 DeliveryPlan

状态：代码、自动化回归、生产发布与浏览器配置验收完成；真人主观听感待同设备 A/B。

- `direct` 不添加思考填充词；`deliberative` 在同一 generation 内使用一次 8–14 字短衔接并把级联 rate 小幅降至 0.95。
- `light_laughter` 需要文本笑声、当前声学 `happy` 或同轮声学 laughter 证据，并且没有严肃信号；`supportive` 始终优先覆盖事故、诈骗、医疗、求助和负向情绪。
- DeliveryPlan 不持久化，不记录转写正文；preamble 与正文不拆成两次 LLM/TTS 回答。
- Omni 只使用对比示例与音频行为 Prompt，不宣称轻笑、拖音和思考感为确定性 API。
