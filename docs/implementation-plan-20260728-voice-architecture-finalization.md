# 语音架构最终收口实施计划

## 目标

按顺序完成三项工作：

1. 小程序保持受控半双工，补齐播放尾音保护、输入策略、稳定播放、按钮停止和短回复。
2. H5 保留 LiveKit/WebRTC 与语音打断，拆出 transport、状态机、页面组件和领域 API。
3. 后端复用现有 `GenerationFence`、`UtteranceRouter` 与 provider，仅吸收 Handler 边界、
   `CancellationContext`、话轮修订和 Realtime 兼容协议；不引入第二套语音运行时。

## 不变约束

- 小程序不恢复语音打断，不引入 TRTC、声网或新的 RTC 依赖。
- H5 的生产主链仍为 LiveKit cascade；Qwen Omni 只作为隔离实验实现保留。
- ASR、LLM、TTS 继续使用远端 API，不引入本地 Whisper、Parakeet、Qwen3-TTS 或 Kokoro。
- `UtteranceRouter` 仍是话轮意图与打断副作用的唯一入口。
- 所有迟到结果继续受 session、turn、generation 与 tool epoch 约束。
- 保留用户当前未提交的小程序视觉、上传脚本和文档改动。

## 测试 seam

### 小程序

- `MiniProgramMediaSession`：服务端输入策略、本地播放状态、尾音保护和用户静音共同决定是否录音。
- `PcmJitterPlayer`：固定帧契约、generation barrier、自适应 lead、平滑停止与遥测。
- 小程序首页：播放期只提供“停止播放”，不会开启录音或恢复语音打断。
- Gateway/Agent 契约：`input_policy`、AEC 试验分组和有界音频指标可跨 Python/JavaScript 验证。

### H5

- `VoiceTransport`：连接、断开、麦克风、停止回答和事件订阅。
- `voiceSessionReducer`：仅允许显式生命周期事件改变会话状态，并丢弃旧 attempt/generation。
- App 页面组件：拆分后保持现有可见行为和无障碍语义。
- 领域 API：旧 `api.js` 只保留兼容导出，行为由各领域模块测试。

### 后端

- `CancellationContext`：从现有 fence 构造并统一判断迟到 provider/tool 回调。
- Provider Handler：业务协调器只依赖窄协议，不依赖供应商对象细节。
- `turn_id + turn_revision`：同一 ASR 话轮可修订，旧 revision 不得覆盖新 revision。
- Realtime facade：只做事件映射与校验，调用同一个 Memoria runtime，不拥有第二套 pipeline。

## 阶段与验证

### 第一阶段：小程序半双工收口

- 默认 150 ms、可配置的播放尾音保护。
- 100–180 ms 自适应播放 lead：underflow 增长，稳定窗口后回落。
- 服务端发布带单调 epoch 的 `input_policy`，客户端不再从状态字符串推断录音权限。
- 播放期按钮执行本地平滑停止和服务端 generation 取消，尾音保护后恢复录音。
- 小程序普通陪伴回复限制为 1–3 句；明确长内容请求保留更长预算。
- AEC 支持明确 `off/on/alternating` 试验模式，记录匿名分组与原始/AEC 后对照指标。
- 产出真机验收记录模板和采集命令；只有真实 iPhone/Android 运行结果可标记通过。
- 验证：小程序测试、Gateway/Agent/Control API 窄测试、Ruff、mypy、契约 JSON。

### 第二阶段：H5 架构拆分

- 抽出 `VoiceTransport` 契约与 `LiveKitCascadeTransport`。
- Qwen Omni 移入实验目录并继续满足同一 transport 契约。
- `useVoiceSession` 使用显式 reducer 管理生命周期与迟到事件。
- 从 `App.jsx` 提取回顾页、资料页和共享偏好行。
- 将 `api.js` 按 auth、voice、profile、archive、persona、growth、self-model、
  digital-self、preview、legacy、voice-profile 等领域拆分，保留兼容入口。
- 正反例打断语料同时覆盖 cascade Router 与 Omni 实验 transport。
- 验证：H5 Vitest、production build、现有 Chrome 渲染/交互/console。

### 第三阶段：后端编排边界

- 在现有 fence 上增加不可变 `CancellationContext`，不平行维护第二个 generation counter。
- ASR 保持 LiveKit `STT` provider adapter，但由统一构造 seam 隔离供应商初始化；
  编排层直接调用的 LLM、TTS 收敛到窄 Handler/Protocol。
- 共享事件契约增加 `turn_revision`，并在同一 turn 内拒绝迟到 revision。
- 增加 OpenAI Realtime-compatible facade 的最小事件映射；内部仍调用同一个
  `DuplexRuntime`/`UtteranceRouter`。
- 记录 ADR，明确 HF `speech-to-speech` 仅作为设计参考。
- 验证：Agent/provider 单元与集成测试、契约测试、Ruff、strict mypy、离线 E2E。

## 2026-07-28 执行结果

- [x] 第一阶段代码收口完成：150 ms 尾音保护、AEC `off/on/alternating`、100–180 ms
  自适应 lead、单调 `input_policy`、按钮停止和 3 句/120 字普通回复均有回归测试。
- [ ] 第一阶段完整真实设备矩阵未完成：`0.8.59` 真机暴露了首播遥测与旧生产 Gateway 的严格
  契约不兼容；`0.8.60` 修复跨版本校验，`0.8.61` 修复 WebSocket handshake 与 SSL/TLS
  错误分类，均已由微信开发者工具 CLI 上传开发测试版。标准 443 精确媒体路由已经部署并验证，
  但同一 iPhone 与 Safari 在当前无 VPN 网络发送 TLS ClientHello 后主动 RST，未进入 Nginx
  HTTP/Gateway。生产已回切 8443，用户确认页面与语音连接恢复可用。
  iPhone/Android、外放/听筒/蓝牙、移动网络/弱网与 AEC A/B 数据仍待采集。
- [x] 第二阶段代码收口完成：transport、reducer、Omni 实验隔离、页面/API 拆分和共享
  正反例语料均已落地；现有 Chrome 已验证首页、回顾、资料、编辑与偏好交互，console
  无 warning/error。
- [ ] H5 真实麦克风与设备侧语意打断验收仍待完成；静态页面和自动语料不能替代真实语音。
- [x] 第三阶段完成：复用同一 fence/router/runtime 的 Provider seam、
  `CancellationContext`、终稿封口的 `turn_revision` 与 text-only Realtime facade；
  未增加本地模型、HF WebRTC 或第二套 pipeline。

最终本地门禁：

- Python：`1345 passed, 27 skipped`；
- Ruff：通过；
- strict mypy：`165 source files` 通过；
- H5：`241/241`，production build 通过；
- 小程序：`76/76`，全部 JavaScript syntax check 通过；
- 离线 E2E、共享 JSON 契约解析与 `git diff --check`：通过。
- 微信开发者工具：skill `0.3.5` 与工具版本一致、登录有效；首页 WXML/WXSS 编译成功，
  `pages/home/index` 整页编译打开，console 的 error/warn/fail/exception 过滤为空，模拟器
  截图无白屏、遮挡或明显布局回归。
- `0.8.59`、`0.8.60`、`0.8.61` 均使用已登录的微信开发者工具官方 CLI 上传开发测试版；
  最新 `0.8.61` 总包 `637,237` 字节，未提审或正式发布。
- `0.8.59` 真机在欢迎语首播后触发生产 Gateway `4400 protocol_error`。生产基线校验器已稳定
  复现新增自适应 lead 遥测字段被拒绝；客户端现默认发送 v1 遥测，只有新版 Gateway 明确广告
  `client_audio_trace_version=2` 才发送新事件和字段。跨版本反馈环、Gateway 全套测试、Ruff、
  strict mypy 均通过；`0.8.60` 已由微信开发者工具 CLI 上传成功，总包 `637,083` 字节。
- `0.8.61` 将普通 WebSocket handshake 与 SSL/TLS/certificate 错误分开。443 真机与 Safari
  的 TLS ClientHello 后 RST 证据确认问题不在 Gateway/ticket；生产回切
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 后，用户确认主链恢复。

## 真机验收矩阵

每个设备/路由至少记录：构建版本、设备型号、系统版本、网络、session id、时间窗口、
首字吞音、AI 尾音误录、underflow/hard reset、停止按钮延迟、前后台/系统中断恢复。

| 平台 | 音频路由 | 网络 | 必测 |
|---|---|---|---|
| iPhone | 外放、听筒、蓝牙 | Wi-Fi、移动网络、弱网 | 尾音、首字、停止、恢复 |
| Android | 外放、听筒、蓝牙 | Wi-Fi、移动网络、弱网 | 尾音、首字、停止、恢复 |

自动化、模拟器或上传成功不能替代上述真实设备结论。
