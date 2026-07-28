# 语音架构优化完成度

> 更新日期：2026-07-28
> 当前生产基线：runtime `20260728-170236`、H5 `20260723-192611`、小程序开发测试版
> `0.8.61`；三阶段代码已进入仓库 checkpoint，runtime/H5 尚未部署，生产 WSS 下发已回切
> 8443 并由用户确认连接恢复
> 说明：本文件记录工程完成度；真实手机声学验收、外部账号权限和供应商控制台资源不以代码测试代替。

## 1. 总体结论

- H5 继续使用 LiveKit/WebRTC，媒体路线不变。
- 小程序当前正式链路仍是
  `RecorderManager → PCM/WSS → 服务端 APM → LiveKit Agent → PCM/WSS → WebAudio`。
- 小程序保持受控话轮：AI 思考/播放期间暂停录音上行，本地尾音结束后再恢复；不提供
  语音打断，当前开发测试版保留独立“停止播放”按钮。
- H5 保持原有可打断能力；“等等、等一下、停一下、先别说”等按控制语意处理。
- TRTC/Agora 尚未接入；它们仍是替换小程序媒体面的候选，不是当前生产事实。
- 当前产品不要求小程序外放全双工，因此 TRTC/Agora 不是本阶段功能前置。

## 2. 已完成

### 2.1 Agent 与控制面

- `UtteranceRouter` 统一 enroll、纯打断、打断后继续提问和普通聊天。
- generation fence、播放 reset barrier 和旧 generation 音频隔离。
- TargetSpeakerFocus、PlaybackInputGuard、语义歧义复核和迟到结果 fence。
- Vosk 独立短命令 KWS 已实现并保留给直接回滚版本；当前小程序关闭 barge-in，
  因此不会构造或运行 KWS。
- 同一 speech epoch 的打断确认语最多播放一次；迟到 ASR 终稿不再越过 4 秒冷却重复播放。

### 2.2 小程序媒体链路

- 上行 `16 kHz / mono / PCM16LE`，下行
  `24 kHz / mono / PCM16LE / 20 ms / 480 samples / 960 bytes`。
- Python/JavaScript 共用 `packages/contracts/miniprogram-media.json`。
- generation/reset barrier、固定帧校验、小缺帧 conceal、gain ramp 和短淡入淡出。
- 系统录音中断结束发送 `uplink_discontinuity`，网关重置半帧、sequence 和 APM 时序。
- 播放 underflow 只重建后续排程，不再硬停仍登记中的旧 source。
- 默认 150 ms、可配置的播放尾音保护；服务端 `input_policy/policy_epoch` 与本地播放器、
  用户静音共同决定是否录音。
- 播放 lead 在 100–180 ms 间自适应：underflow 后提高，连续稳定后逐步回落。
- Gateway AEC 支持 `off/on/alternating`，按 session 稳定分配 control/aec 组并在 ready
  事件中返回实际启用状态；正常半双工默认关闭。
- 客户端将首播、underflow、hard reset 和 conceal 的有界数值指标经网关转发到既有
  `voice-agent.telemetry`，不上传音频、转写、token 或任意文本。
- 小程序 CI、JavaScript syntax check、协议 golden tests 和播放器行为测试。
- AI 响应期暂停录音；权威完成后仍等待本地播放 drain，用户手动静音优先。
- 删除小程序口头打断和播放期录音入口；“停止播放”只做本地淡出、服务端 generation 取消
  与尾音保护，不恢复全双工。
- Gateway 始终标记小程序平台；Agent 对该平台关闭 LiveKit interruption、KWS、歧义
  语意复核和播放期转写接纳。
- 新客户端在 TLS Upgrade header 提交短期 signed gateway ticket；Gateway 优先完成该认证并
  发送 `ready`，旧客户端 JSON `hello` 保持回退。ticket 不进入 URL 或 access log。

### 2.3 H5 打断

- H5 的 `barge_in_enabled` 默认保持开启。
- Cascade 继续由 `UtteranceRouter` 按控制语意区分纯打断、打断后继续提问和普通聊天；
  覆盖“等等、等一下、停一下、先别说”等同类表达。
- Qwen Omni 备选 transport 的 intent classifier 保持同类回归，但当前生产 H5 仍是
  Cascade。
- 仓库 checkpoint 已抽出 `VoiceTransport`、`LiveKitCascadeTransport` 和显式
  `voiceSessionReducer`；Omni 移入 `voice/experimental/`。
- `App.jsx` 已提取回顾页、资料页和偏好行；`api.js` 已拆为领域模块并保留兼容导出。
- Cascade 与 Omni 共用正反例打断语料，控制词只在句首构成控制意图，避免
  “我等一下再说”“这个站不是终点”等误打断。

### 2.4 后端与发布

- 独立 MiniProgramMediaGateway、短期 gateway ticket、最小权限 LiveKit participant。
- AEC 运行期 fail-closed 撤权与 Agent ACK。
- 指定单 session 的 AEC 前后 WAV 有界采样，默认关闭。
- 仓库 checkpoint 新增 provider Handler seam、包装现有 fence 的 `CancellationContext`、
  `turn_revision` 和不承载音频的 Realtime-style facade；不引入第二 runtime。
- 四角色 runtime 镜像、备份、回滚、readiness、Provider smoke 和发布清理流程。
- runtime `20260728-170236` 已完成生产部署；小程序受控话轮、H5 语意打断分流和 WSS header
  握手已上线。
- 小程序开发测试版 `0.8.61` 已上传；生产 WSS 已回切 8443 并获用户连接确认，未提交审核或正式发布。

## 3. 部分完成

| 项目 | 当前状态 | 剩余门槛 |
| --- | --- | --- |
| AEC | `off/on/alternating` A/B、固定 delay、单 session 采样 | 客户端真实播放时间、动态 delay、路由变化校准、设备矩阵 |
| 播放器 | 固定帧、80 ms 聚合、100–180 ms 自适应 lead、underflow 安全 rebase | 长期时钟漂移校准、真机 underrun SLO |
| 上行可靠性 | 同步 send 失败不提交 sequence；中断恢复有 discontinuity | 单写入者有界队列、异步成功提交、队列溢出后的保会话恢复 |
| FunASR | 支持可选 `vocabulary_id` 与 `speech_noise_threshold` | 创建生产热词表、使用真机录音集校准后才能启用 |
| 协议 | v1 上行、generation v2 下行、共享 JSON 合同 | stream epoch、统一全局 sequence、route-change 等 v3 字段 |
| 真机 WSS 可达性 | `0.8.61` 已上传；443 在当前无 VPN 网络 TLS ClientHello 后 RST；8443 回切后用户确认恢复 | 为每次验收补齐 session/timestamp，并完成 iOS/Android 网络矩阵 |
| H5 | LiveKit/WebRTC、transport/reducer/API 分域与页面拆分均有本地测试 | 现有 Chrome 与真实设备验收；后续可继续缩小 God Hook |
| 后端目录 | 领域模块和部署服务均可运行 | deployable apps 与 reusable packages 尚未物理重组 |
| 契约 | 小程序媒体合同已共享 | REST/UI/错误事件仍未全部从 Schema 生成 |

## 4. 未完成

- TRTC 或 Agora 小程序媒体 PoC、服务端媒体桥和生产迁移；仅在产品重新要求小程序随时
  打断时启动。
- iOS/Android 外放、听筒、蓝牙、弱网、前后台和系统录音中断完整矩阵。
- 客户端实际硬件播放时间与 AEC 前后音频的四路时间戳闭环。
- FunASR 生产热词表和噪声阈值校准。
- H5 `useVoiceSession.js` 仍较大；本轮先收敛 transport、状态与页面/API 边界，后续按真实
  变更热点继续拆分，不为行数单独制造抽象。
- 全端生成式契约和后端 `apps/packages` 目录重组。

## 5. TRTC 边界

TRTC 不是一个只替换小程序播放器的前端依赖。保留当前 Agent、FunASR、Qwen、Doubao、
LiveKit、权限、记忆和归档时，还需要一个可双向取送音频的 TRTC ↔ 现有媒体面桥。

当前仓库和运维配置未具备：

- TRTC `SDKAppID` 和服务端 UserSig 密钥引用；
- 已确认的企业小程序类目及 `live-pusher/live-player` 组件权限；
- 可把 TRTC 远端音频交给现有 Agent、并把 Agent PCM 发布回 TRTC 的服务端桥；
- 真机 AEC/ANS/AGC、弱网和音频路由对照数据。

因此当前受控话轮不依赖 TRTC。若未来恢复小程序外放全双工，只能在取得上述外部条件后建立
独立 PoC，不得直接把生产会话切到一个只能进房、不能接入现有 Agent 的半成品。相关平台
约束和候选路线见
`docs/research/wechat_miniprogram_duplex_audio_20260726.md`。

## 6. 下一阶段顺序

1. 真机验证 AI 思考/播放期间上行 PCM 为零、本地尾音后恢复，以及手动静音优先。
2. H5 真机/浏览器验证控制语意和非打断反例，防止“等等”漏识别或普通语句误触发。
3. 继续采集 underflow/hard reset/lead 指标，定位剩余卡顿和滋滋声。
4. 使用非播放期真机录音集校准 FunASR 热词表和噪声阈值，不直接在生产猜参数。
5. 完成上行单写入者队列和协议 v3。
6. 产品重新要求小程序随时打断时，再取得 RTC 外部条件并做独立 PoC。
7. 在真机/浏览器数据证明新边界稳定后，再决定是否继续拆 `useVoiceSession` 或物理重组
   后端目录。
