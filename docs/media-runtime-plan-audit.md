# 全双工整改计划落地审计

本文把 `memoria_streamcore_full_duplex_remediation_plan.md` 的代码落地、可执行
验证和仍需外部证据的部分分开记录。它是发布前审计，不是把参考实现包装成生产
WebRTC 的完成声明。

| 计划项 | 当前落地 | 仓内证据 | 仍需外部验收 |
|---|---|---|---|
| Sample Clock / Speech Timeline | 已落地 interval、revision、epoch、committed watermark；Media Edge/Voice Core Registry 已使用 range-stamped Timeline，LiveKit 旧回调仍保留兼容路径；Go actor 已有有界、candidate-only Timeline 算法，生产 bridge observation/parity 代码路径已接通 | `test_speech_timeline.py`、`test_voice_core_contracts.py`、`test_media_session.py`、`live_session_actor_test.go`、`bridge_runtime_test.go` | 真实 provider 多 VAD/多 ASR 回放、fixed-set parity、生产 shadow 证据与逐状态写权迁移 |
| FunASR reconnect | 已落地 400 ms 有界 ring、provider ack、task epoch、final watermark；跨 task 扩展重放只接收连续音频区间和文本前缀可证明的新后缀，歧义重叠 fail-closed；VAD end 显式携带排除 hangover 的 `voiced_end_sample`，ASR 覆盖该声学边界后才提交，canonical 与含 hangover 的 transport retire watermark 分离，尾静音区间迟到结果在提交后拒绝，缺字段 fail-closed | `test_funasr_protocol.py`、`test_funasr_session_edges.py`、`test_provider_adapter.py`、`test_media_session.py`、`test_grpc_bridge.py` | 真实 Provider 断线、迟到 final 和儿童语音 CER |
| Generation / Playback | 已落地 Python generation controller、Go generation gate、Playback Ledger、停止幂等；按钮/KWS hard stop 先关闭 Edge 本地 gate，旧 PCM 立即拒绝，session/uplink 和下一 generation 保持可用；media registry 只有在 sample watermark 覆盖完整文本 span 后才提交 actual-heard 历史；固定 20 ms PCM、received-sequence/sample watermark、断线 grace 和旧回复取消均已接入 | `test_voice_core_contracts.py`、`test_media_session.py`、Go race tests、H5 transport tests | 真实浏览器/硬件播放 ACK、实际扬声器停止和播放延迟 |
| H5 双 Transport | 已落地服务端选择、WHIP transport、DataChannel epoch/sequence、local duck、playback flush、HTTP fallback、authoritative reconnect、TTL heartbeat；连接必须等到 `connected`，断线通知与 reconnect 使用 single-flight/CAS；短请求有显式超时，长耗时 Control API 不受全局 10 秒误杀；状态/表达式/transcript 消费 turn/generation/tool fence，client-event envelope additive 透传 task/context version；ACK 以每代 `currentTime` 基线计算，flush 后更高 fence 恢复同一 remote track；Go 使用进程单调时钟填写 `server_monotonic_ms` | H5 Vitest（286 tests）、H5 build | 真实 TURN/ICE restart、跨主机浏览器/网络矩阵、设备 playout/AEC、端到端 SLO 和回滚演练 |
| VAD / KWS | 已落地自适应能量 VAD、精确中文控制词 lexical fallback，以及显式 `hard_stop` wire flag、0.8 confidence threshold 和 Edge 本地原子 gate | `test_voice_analysis.py`、`test_media_session.py`、Go hard-stop/race tests | 音频级 KWS/模型召回率、电视/远场/儿童负例；当前不能宣称音频 KWS 已完成 |
| Media Edge | 已落地自有协议、队列边界、sample gap/discontinuity、JWT、drain/health/metrics、Pion WHIP/ICE/DTLS/SRTP/RTP/Opus terminator、真实 generation-scoped downlink sender，以及由自有 proto 生成的带 mTLS 门禁的 Go 双向 gRPC Voice Core adapter；ready probe 检查 bridge 与 terminator 能力 | `services/media_edge/bridge.go`、`webrtc_terminator.go`、`cmd/mediaruntime`、Go `vet/test/test-race`、production env tests | 真实 TURN/跨主机浏览器媒体、多实例吞吐和 edge↔core 部署演练 |
| Voice Core gRPC Bridge | 已落地 `media-v1` 生成代码、真实 asyncio 双向 gRPC endpoint、Python/Go TLS/mTLS material loader、bounded downlink、audio/VAD/KWS callback seam；`MediaVoiceCoreRegistry` 通过仓内共享 session factory 原子获得同一 `DuplexRuntime` 与 provider，复用 ModePolicy、ResponsePlanner、speaker/voice authority、Context 与生命周期；生产 TTS 在一个 generation 复用单条双向流 | `test_grpc_bridge.py`、`test_media_session.py`、`test_media_agent_factory.py`、`test_provider_adapter.py`、Go bridge tests、两套 proto 生成脚本 | 真实 Media Edge ↔ Voice Core ↔ provider/浏览器跨主机音频验收仍缺；默认灰度保持关闭 |
| Control / Session Directory | 已落地 server-owned runtime、短期 media JWT（含 stream epoch、account/device/client_type 全绑定）、短期 coturn credential、TTL、drain、epoch/generation CAS；HTTP stop 先取 Edge/Core 完整权威 fence，再由 Directory 单调观察，相同 idempotency key 不重复推进；停止不关闭 session，下一话轮下行有 Go 回归；失败切 LiveKit 前通过 `media-fallback` CAS transition 更新路由 | Control API media/session-directory tests、H5 reconnect tests、Go JWT/stop-next-generation tests | Redis 多实例故障切换、真实 coturn allocation/relay 比例 |
| Telemetry / SLO | 已落地 allowlist telemetry、Prometheus bootstrap、可选 OTel exporter、fail-closed SLO helper；Control API 已接入带 token/TTL 的内部 SLO report 和 StreamCore rollout gate；Agent media bridge 独立暴露指标，sidecar 指向 bridge 而非错误的 agent 进程，缺项不会被默认为 0 | `test_media_runtime_hardening.py`、`test_media_slo.py`、`test_slo_reporter.py`、Control media/runtime tests、Compose config | Collector/Grafana/Sentry 接线、真实聚合指标、自动 rollout controller 和生产演练 |
| Linux / Device | 已落地 `LinuxMediaDeviceClient`、sample-clock capture/reconnect、实际 playback reference 的 NLMS AEC、设备命令 allowlist/TTL/ACK、Ed25519 challenge/OTA manifest，以及自有 A/B slot 状态机（签名、反回滚、bounded boot attempts、confirm/fallback）；Control API 已持久化设备公钥、单次 challenge/revoke，公开 challenge 只保留有限未消费 nonce 并返回 429 | `test_device_client.py`、`test_ota.py`、`test_media_runtime_hardening.py`、`test_media_api.py`、`device_registry.py` | ALSA/I2S/DMA 校准、物理静音、Secure Boot/Flash Encryption、真实 bootloader 分区/断电演练；Media Edge mTLS 设备身份和真实硬件仍需接入 |
| 儿童语料 / Chaos / Load | 已落地 synthetic corpus schema、`consented_recorded` 的 200 条门禁、确定性 replay/chaos/load harness | `scripts/media_runtime_replay.py`、`tests/test_media_runtime_replay.py` | 200--300 条监护人授权真实录音、真实 chaos/load 数据和 SLO |

## 发布结论

- `MEDIA_RUNTIME_DEFAULT` 和 `STREAMCORE_EXPERIMENT_PERCENT` 默认保持 `livekit/0`。
- `services/media_edge` 是自有状态机/契约/鉴权参考面，不是自写的安全敏感
  WebRTC 协议栈；生产默认仍由现有 LiveKit 链路承担。
- 在“真实跨主机 WebRTC/Voice Core/provider 部署、硬件 AEC、儿童语料、Redis/coturn
  多实例和回滚演练”证据齐全前，不得把 StreamCore 路线设为默认，也不得宣称
  已达到计划中的全部 SLO。

这一区分同时保留了对公开工程思想的独立实现，又避免把第三方开源产品源码、
生成物、商标或固定凭证带入 Memoria。

真实部署、验收证据、灰度与回滚步骤见 `docs/media-runtime-acceptance-runbook.md`。

## 2026-08-06 当前门禁

- 当前工作区已使用 CI 同镜像 `pgvector/pgvector:0.8.1-pg17-bookworm` 完成全量 Python：
  `1856 passed, 2 skipped`；总 coverage `87.70%`、Agent orchestration `90%`、provider
  protocol `97%`，均达到 CI 门槛。Ruff、strict mypy（223 source files）、Python/Go proto
  生成摘要一致、Go `gofmt/vet/test -race`、media smoke/replay、offline E2E 均通过。`buf lint`、
  相对 `HEAD` 的 `buf breaking`、GolangCI `v2.12.2`（`0 issues`）、当前媒体 Edge 镜像构建与
  Trivy `v0.70.0`（HIGH/CRITICAL 为 0）也已通过；扫描发现的 `golang.org/x/crypto v0.51.0`
  已升级到 `v0.52.0`。
- H5 当前为 `288 passed` 且 production build 通过；in-app browser 在 `390x844` 验证无横向溢出、
  语音/文字入口可用且 console 无 error。这只是本地 UI/transport 壳验证，不替代真实 TURN/provider
  媒体会话。
- 本轮 media smoke、replay 与 offline E2E 均通过（旧 generation PCM 为 0）；`provider_smoke_test.py`
  在 offline 模式按 CI 约定跳过，去除该开关后明确报告缺少 `DASHSCOPE_API_KEY` 与 Doubao TTS 认证，
  因而未发起真实 provider 请求。required-mode 已补入隔离的 `QwenRealtimeSearch` forced-search
  source，凭据可用时会和 FunASR、主 LLM、Doubao、语义分类一同验收；本机只验证了其 fake 回归和
  缺凭据 fail-closed 行为。
- StreamCore 物理 PCM 执行面当前只支持 `CONVERSATION_REPLY`、`FAST_ACKNOWLEDGEMENT`、
  `DEEP_RESULT`；`TOOL_RESULT`、backchannel、reminder、notification 仅保留 wire/shadow 兼容，
  没有真实产品生产者时在执行边界 fail closed。新增该类能力必须连同权限设计、生产 source 与独立
  `OutputWork` 回归一起进入。
- 没有新的远端 CI run；真实 TURN/跨主机 provider/浏览器、硬件 AEC、A6B 与 A7-A9 外部证据
  仍保持未关闭。

## 2026-08-05 当前门禁（历史）

- 历史 CI 同版本 PostgreSQL/pgvector 基线为 Python `1816 passed, 2 skipped`、coverage
  `87.59%`；当前新增代码尚未在该条件重跑。strict mypy `223 source files, 0 errors`。
- StreamCore range-stamped VAD 已在 commit 前完成正式 Speaker Classification；archive replay
  不再阻塞 session 创建，bridge 异常关闭始终释放共享 factory。
- H5 `286 passed` 且 production build 通过；Go `test`、`test -race`、`vet` 通过。
- 远端 CI、真实 TURN/跨主机 provider/浏览器、硬件 AEC、A6B 与 A7–A9 外部证据仍未关闭；
  A6A actor 算法和生产 bridge observation 代码路径已接通 Timeline/Python candidate metadata 的有界 active-set parity/phase-derived typed Floor shadow，
  同域/跨域 expiry fallback、consumed lifecycle 与 gap resync 已完成；普通主回复已通过 winner
  admission 取得 owner，异步 transition 后重验 owner，取消先撤 lease，且等待全部 PCM/text span
  playback ACK。normal reply、`tts_source`、`pcm_s16le` 已统一经 `OutputWork` 取得唯一 owner；queued
  candidate 会在旧 owner ACK 后唤醒，高优先级 work 会取消旧 owner、flush generation，并从
  sequence/sample `0` 重启；无 span ACK 与 provider COMPLETE 时序回归也已关闭。慢 realtime delegation
  的 `FAST_ACKNOWLEDGEMENT` 已走同一 OutputWork，快速 resolver 不产生 ACK。H5 StreamCore
  client-event envelope/fence 已对齐，task/context version 已进入客户端。实际 `TOOL_RESULT`、
  backchannel/reminder/notification 的生产来源仍缺；
  Python-authoritative `RealtimeEffect` 已经由 CoreToMedia/Go fence gate/WebRTC-H5 执行链接通，
  candidate、stale、非法 payload 与 A6B 前的 `go_authoritative` 均 fail closed；真实生产
  provider 矩阵、容量与授权 shadow 证据仍缺。正式 `FloorEffect` 已通过 Python→Go
  bridge/runtime→WHIP WebRTC→H5 接通；candidate、旧 epoch、TTL 与 A6B 前的
  `go_authoritative` 均 fail closed。

## 2026-08-03 评审整改补充

- 媒体桥接的 uplink/downlink 队列改为消费后 ACK，按 generation 重置 downlink 序号并拒绝 sample gap；ASR final 只作为证据，由 VAD+sample coverage coordinator 统一提交；控制词仍经过 Router，停止会取消 provider 与旧 reply task。
- Playback Ledger 只接受已登记的 sequence/sample watermark；H5 以每 generation 播放基线发布单调进度，非法高序号/旧 tool epoch 事件不会吞掉后续合法事件。Python、Control API、Go、H5 的回归测试和 smoke 已执行。
- 全仓功能测试 `1641 passed, 29 skipped`，但本机 coverage 仍为 `81.00%`，低于现有 `85%` 门槛；本机没有 CI PostgreSQL，条件测试被跳过，本轮没有降低门槛。真实 WHIP/RTP/DTLS/SRTP、完整 Agent orchestrated provider、硬件播放与外部 SLO 仍必须按上表独立验收。

## 2026-08-03 第二轮评审整改（已提交 c3b6c19、f7845e2 后再次评审）

- **CI 门禁修复**：`buf lint` 对 media-v1 的既有命名（`MediaToCore`/`CoreToMedia`、
  `VAD_EVENT_*`、服务名）配置显式例外并说明理由，避免把既有契约改成破坏性变更；
  Trivy action 版本修正为存在的 `v0.36.0`；golangci 修复未使用字段与弃用
  `grpc.DialContext`/`WithBlock`（迁移到 `grpc.NewClient` + 显式 ready 等待）。
- **ASR 真区间去重**：supervisor 与 provider adapter 不再用“最大 final end
  watermark”压掉乱序 final；改为已接受区间集合去重——不重叠乱序 final 接受、
  同区间更高 revision/更新文本可修正、跨 task 同区间重放与跨句模糊重叠
  fail-closed、扩展区间仍走连续前缀对账。回归覆盖 320..640 先于 0..320、
  修正、重放、歧义重叠、扩展与历史有界。
- **流式 TTS 部分已听文本**：生产双向流只以 Doubao `TTS_SUBTITLE` 返回的
  字级时间戳登记 playback span，时间戳可以在 PCM 后到达；ledger 会按已有 ACK
  立即结算。没有 provider 对齐时间戳时不登记 text span，避免把 LLM 入队时机
  误当音频边界。
- **INTERRUPTION_PENDING 不再丢已听前缀**：按钮/KWS stop 在
  SPEAKING 或 INTERRUPTION_PENDING 都执行同一 interrupted-playback finalize。
- **interrupt SLO 口径**：不再跨主机相减墙钟。Core 只记录本进程
  `interrupt_core_stop`；端到端 `interrupt.detect → interrupt.cancel` 在
  分布式 trace/时钟同步落地前保持缺失并由 SLO gate fail-closed。
- **下行 gate 与 sender 原子**：`Session.DeliverDownlink` 先记录受 fence 保护的
  帧，再在锁外调用带 generation context 的 sender；cancel 先取消 context，因而
  slow sender 不会阻塞 hard-stop，且合规终结器不能在取消后写 PCM。gRPC 输出
  队列溢出会以 terminal `GENERATION_ACTION_CANCEL`（`downlink_queue_full`）关闭
  generation，并同步取消 Voice Core runtime/provider；重连收到 CANCEL 而非陈旧 RESUME。
- **HTTP stop 携带完整 fence**：H5 DataChannel 失败后的 HTTP fallback 携带
  按钮时刻的 stream_epoch/turn_id/generation_id/tool_epoch，Control API 校验
  并转发，Edge 用 `StopGenerationRequest.ExpectedFence` 只取消用户按下按钮时
  看到的 generation，迟到的 fallback 不会误停新回答。
- **生产 ready 接缝**：生产 `Server` 必须收到真实终结器提供的
  `DownlinkSenderFactory`，factory 产生的 sender 直接传入 bridge runtime；参考
  binary 没有 factory 时 `/readyz` 与 session 创建均 fail-closed，不能再用环境
  变量伪造 ready。
- 验证：全量 `pytest` 通过（本机 coverage `81.08%`，仍低于 `85%` 门槛且未降级；
  缺口集中于历史遗留的 postgres/ONNX 真实依赖模块），Ruff 与 strict mypy
  （216 源文件）通过，H5 `275 passed` + production build，Go
  `test/vet/test-race/golangci` 通过，buf lint 通过，proto 生成无 diff，
  media bridge smoke 与 replay/chaos/load 通过，`git diff --check` 通过。
- 仍未关闭且必须外部验收：真实 WHIP/RTP/DTLS/SRTP/Opus 终结器与音频级 KWS
  producer、完整 Agent orchestrated provider factory、真实浏览器/硬件播放
  ACK、监护人授权儿童语料、Linux 硬件 AEC、Redis/coturn 多实例、真实
  chaos/load/SLO 与回滚演练；`media-runtime` profile 仍不可上线，默认链路不变。
