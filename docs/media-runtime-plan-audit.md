# 全双工整改计划落地审计

本文把 `memoria_streamcore_full_duplex_remediation_plan.md` 的代码落地、可执行
验证和仍需外部证据的部分分开记录。它是发布前审计，不是把参考实现包装成生产
WebRTC 的完成声明。

| 计划项 | 当前落地 | 仓内证据 | 仍需外部验收 |
|---|---|---|---|
| Sample Clock / Speech Timeline | 已落地 interval、revision、epoch、committed watermark；LiveKit 旧回调仍保留兼容路径 | `test_speech_timeline.py`、`test_voice_core_contracts.py` | 将真实 Media Edge/Voice Core 输入全面切到 timeline，并完成多 VAD/多 ASR 回放 |
| FunASR reconnect | 已落地 400 ms 有界 ring、provider ack、task epoch、final watermark、跨 task 去重 | `test_funasr_protocol.py`、`test_funasr_session_edges.py`、mock reconnect 集成测试 | 真实 Provider 断线、迟到 final 和儿童语音 CER |
| Generation / Playback | 已落地 Python generation controller、Go generation gate、Playback Ledger、停止幂等；media registry 只有在 sample watermark 覆盖完整文本 span 后才提交 actual-heard 历史；固定 20 ms PCM、received-sequence/sample watermark、断线 grace 和旧回复取消均已接入 | `test_voice_core_contracts.py`、`test_media_session.py`、Go race tests、H5 transport tests | 真实下行 RTP 编码前 gate、浏览器/硬件播放 ACK 和真实播放延迟 |
| H5 双 Transport | 已落地服务端选择、WHIP transport、DataChannel epoch/sequence、local duck、playback flush、HTTP fallback、authoritative reconnect、TTL heartbeat；连接必须等到 `connected`，断线通知与 reconnect 使用 single-flight/CAS；WHIP/fetch 有超时，事件必须带完整 fence，播放期上报受已接收音频边界约束 | H5 Vitest（269 tests，其中 transport 16）、H5 build | 真正 WHIP/WebRTC 终结、ICE restart、浏览器/网络矩阵和回滚演练 |
| VAD / KWS | 已落地自适应能量 VAD 和精确中文控制词 lexical fallback | `test_voice_analysis.py` | 音频级 KWS/模型召回率、电视/远场/儿童负例；当前不能宣称音频 KWS 已完成 |
| Media Edge | 已落地自有协议、队列边界、sample gap/discontinuity、JWT、drain/health/metrics 参考服务，以及由自有 proto 生成的带 mTLS 门禁的 Go 双向 gRPC Voice Core adapter；客户端再次执行 identity、sequence、sample range 和 generation gate；生产入口阻塞等待 Core、ready probe 检查 gRPC connectivity，并独立分流 edge env | `services/media_edge/bridge.go`、`cmd/mediaruntime`、`bridge_test.go`、生成脚本、`go test -race ./...`、production env tests | RTP/Opus/DTLS/SRTP/WHIP 终结、真实多实例吞吐和 edge↔core 部署演练；这些没有复制第三方源码 |
| Voice Core gRPC Bridge | 已落地 `media-v1` 生成代码、真实 asyncio 双向 gRPC endpoint、Python/Go TLS/mTLS material loader、bounded downlink、audio/VAD/KWS callback seam；`MediaVoiceCoreRegistry` 将 session identity、`DuplexRuntime`、ASR watermark、Generation Gate 和 Playback Ledger 接到同一会话；Go `VoiceCoreBridge` 与本地 fake server E2E 验证重放丢弃；`scripts/run_media_bridge.py` 可独立启动且生产默认要求 mTLS | `test_grpc_bridge.py`、`test_media_session.py`（真实本地 gRPC + fake provider E2E）、`services/media_edge/bridge_test.go`、`test_config.py`、两套 proto 生成脚本、CI generated diff gate | provider factory 仍需在部署进程中注入现有 FunASR/Qwen/Doubao 适配器；尚无真实 Media Edge ↔ Voice Core ↔ provider/浏览器音频验收 |
| Control / Session Directory | 已落地 server-owned runtime、短期 media JWT（含 stream epoch、account/device/client_type 全绑定）、短期 coturn credential、TTL、drain、epoch/generation CAS；StreamCore HTTP stop 与 DataChannel 共用 authoritative generation，并实际派发至 Media Edge；重连 body 携带期望 epoch，避免并发重连推进两次；失败切 LiveKit 前通过 `media-fallback` CAS transition 更新路由，确保后续 Stop 仍投递 LiveKit room | Control API media/session-directory tests、H5 reconnect tests、Go JWT tests | Redis 多实例故障切换、真实 coturn allocation/relay 比例 |
| Telemetry / SLO | 已落地 allowlist telemetry、Prometheus bootstrap、可选 OTel exporter、fail-closed SLO helper；Control API 已接入带 token/TTL 的内部 SLO report 和 StreamCore rollout gate；Agent media bridge 独立暴露指标，sidecar 指向 bridge 而非错误的 agent 进程，缺项不会被默认为 0 | `test_media_runtime_hardening.py`、`test_media_slo.py`、`test_slo_reporter.py`、Control media/runtime tests、Compose config | Collector/Grafana/Sentry 接线、真实聚合指标、自动 rollout controller 和生产演练 |
| Linux / Device | 已落地 `LinuxMediaDeviceClient`、sample-clock capture/reconnect、实际 playback reference 的 NLMS AEC、设备命令 allowlist/TTL/ACK、Ed25519 challenge/OTA manifest，以及自有 A/B slot 状态机（签名、反回滚、bounded boot attempts、confirm/fallback）；Control API 已持久化设备公钥、单次 challenge/revoke，公开 challenge 只保留有限未消费 nonce 并返回 429 | `test_device_client.py`、`test_ota.py`、`test_media_runtime_hardening.py`、`test_media_api.py`、`device_registry.py` | ALSA/I2S/DMA 校准、物理静音、Secure Boot/Flash Encryption、真实 bootloader 分区/断电演练；Media Edge mTLS 设备身份和真实硬件仍需接入 |
| 儿童语料 / Chaos / Load | 已落地 synthetic corpus schema、`consented_recorded` 的 150 条门禁、确定性 replay/chaos/load harness | `scripts/media_runtime_replay.py` | 150--300 条监护人授权真实录音、真实 chaos/load 数据和 SLO |

## 发布结论

- `MEDIA_RUNTIME_DEFAULT` 和 `STREAMCORE_EXPERIMENT_PERCENT` 默认保持 `livekit/0`。
- `services/media_edge` 是自有状态机/契约/鉴权参考面，不是自写的安全敏感
  WebRTC 协议栈；生产默认仍由现有 LiveKit 链路承担。
- 在“真实 WebRTC、真实 Voice Core provider wiring、硬件 AEC、儿童语料、Redis/coturn
  多实例和回滚演练”证据齐全前，不得把 StreamCore 路线设为默认，也不得宣称
  已达到计划中的全部 SLO。

这一区分同时保留了对公开工程思想的独立实现，又避免把第三方开源产品源码、
生成物、商标或固定凭证带入 Memoria。

真实部署、验收证据、灰度与回滚步骤见 `docs/media-runtime-acceptance-runbook.md`。

## 2026-08-03 评审整改补充

- 媒体桥接的 uplink/downlink 队列改为消费后 ACK，按 generation 重置 downlink 序号并拒绝 sample gap；ASR final 进入真实 `commit_user_turn → generate_reply`，控制词经过 Router，停止会取消 provider 与旧 reply task。
- Playback Ledger 只接受已登记的 sequence/sample watermark；H5 只发布单调播放进度，非法高序号事件不会吞掉后续合法事件。Python、Control API、Go、H5 的回归测试和 smoke 已执行。
- 全仓 `1615 passed, 29 skipped`，但覆盖率仍为 `80.95%`，低于现有 `85%` 门槛；本轮没有降低门槛。真实 WHIP/RTP/DTLS/SRTP、provider factory、硬件播放与外部 SLO 仍必须按上表独立验收。
