# Memoria Media Runtime 基础改造

2026-08-02 起，整改方案先在现有仓库落地一条可回归的媒体契约层。它不复制
StreamCore/Pion 或其他第三方仓库的代码，也不改变 LiveKit 生产默认值。

## 已落地

- `SpeechTimeline` 以 `stream_epoch + capture_start_sample + capture_end_sample`
  作为唯一音频事实源；同一 `segment_id` 的 revision 替换旧 partial，committed
  watermark 之后的迟到结果直接丢弃。
- FunASR 结果增加 `task_epoch`、sample range、revision；会话维护
  `last_sent_sample`、provider ack、committed/final watermark，重连只回放水位之后的
  有界尾部。
- `voice_core` 提供自有标准库实现：`ASRStreamSupervisor`、`GenerationController`、
  `ResponseLease`、`PlaybackLedger`、`MediaBridgeServer`、`MediaVoiceCoreRegistry`、`AdaptiveEnergyVAD`、中文
  精确控制词 KWS、`media-v1` JSON 契约、Linux NLMS AEC、设备签名/OTA、telemetry/SLO
  与 synthetic replay/chaos/load harness。
- `packages/proto` 通过 `grpcio-tools` 生成并在 CI 检查无漂移的 Python media-v1
  bindings；`MediaBridgeGrpcServer` 提供真实双向 asyncio gRPC endpoint，生产启动脚本
  `scripts/run_media_bridge.py` 在启用时强制证书、客户端 CA 和 mTLS。音频、VAD、KWS
  通过明确的 callback seam 交给 Voice Core，不在 bridge 内复制第二套 Agent。
- `services/media_edge/bridge.go` 使用同一份自有 `media-v1` proto 生成 Go bindings，提供
  `VoiceCoreBridge`/`VoiceCoreSession`：连接前拒绝未授权明文，连接后重复校验 identity、
  event sequence、sample range 和完整 generation；`scripts/generate_media_go_proto.sh`
  可重复生成 bindings，且本地 fake gRPC server 已覆盖 replay/stale frame。
- `scripts/media_runtime_smoke.py` 在不访问外网 Provider 的情况下启动真实 gRPC
  endpoint，验证 hello、authoritative generation、downlink PCM 和 stale frame 丢弃；
  该 smoke 已纳入 CI，但不能替代真实 WebRTC/Provider/硬件验收。
- `MediaVoiceCoreRegistry` 为每条 bridge session 建立独立的 `DuplexRuntime`、ASR
  watermark、Generation Fence 和 Playback Ledger；它会把真实 gRPC 音频送入注入的
  provider adapter，并只在客户端 `PlaybackProgress` 覆盖音频 sample range 后发布
  `actual-heard` 文本。当前启动脚本仍保持 provider-neutral，部署必须显式注入现有
  FunASR/Qwen/Doubao adapter，避免复制第二套智能编排。
- `LinuxMediaDeviceClient` 提供 bounded capture/reconnect、本地 mute、NLMS AEC
  reference、PCM 播放回调和精确 playback ACK；`media-slo-reporter` Compose sidecar
  只上报 allowlist 聚合指标，报告过期或字段缺失时 StreamCore gate fail-closed。
- `voice_core/ota.py` 提供不依赖第三方 OTA 框架的 A/B 控制状态机：只接受已签名且
  digest/bootloader/版本检查通过的 inactive-slot artifact，启动确认前限制尝试次数，
  失败自动回到上一份 confirmed slot。它只描述 boot metadata 合同，真实分区、Secure
  Boot 和断电持久化仍需设备侧验收。
- Control API 的 `DeviceRegistry` 保存每台设备公钥、一次性签名 challenge 和 revoke
  状态；生产设备媒体会话必须提交 signed proof，私钥不进入服务端。`MediaSLOGate`
  通过带 token 的内部 report endpoint 为 StreamCore 灰度提供 TTL/fail-closed 门禁。
- `services/control_api/app/session_directory.py`、`turn_credentials.py` 和
  `services/media_edge/` 提供 Redis/coturn/Go media-only 参考运行面；Control API 的
  media façade 会先写入带 TTL 的路由记录，并把短期 TURN 凭证交给浏览器，不发送共享密钥。
- `packages/proto/memoria/media/v1` 保存与 JSON 实现对齐的媒体边界；协议只承载
  媒体时钟、事件和 generation，不承载 LLM、记忆、人格、工具或供应商密钥。
- H5 新增浏览器原生 `StreamCoreTransport`、DataChannel sequence/epoch fence、
  `connected` readiness、断线 single-flight、`playback.flush`/ping/session 事件、
  本地停止 duck 和 HTTP fallback；`voiceTransportFactory` 默认仍返回 LiveKit，
  StreamCore 只有服务端灰度字段显式选择时才会构造。
- Control API 增加 server-owned `media_runtime`、`fallback_runtime`、
  `stream_epoch` 和短期、session-scoped media JWT。生产启用 StreamCore 时要求独立
  `STREAMCORE_TOKEN_SECRET` 与 HTTPS WHIP URL；小程序始终回 LiveKit/半双工。
- 生产 env 分流新增独立 `/etc/memoria-media-edge.env`：其中仅放 edge JWT、Voice Core
  地址和 mTLS 文件路径；`MEDIA_EDGE_JWT_SECRET` 与 Control API 的
  `STREAMCORE_TOKEN_SECRET` 保持同值但不重复写入 Control/Agent key。Go edge 启动时
  阻塞等待 Core，`/readyz` 在 gRPC connectivity 非 Ready 时 fail-closed。

## 不变量

1. 旧 `stream_epoch`、旧 `generation_id`、sequence 倒退事件不得进入播放或历史。
2. 取消和旧 response 的 defer 只能清理自己拥有的 fence。
3. Playback Ledger 只把客户端 ACK 已完整覆盖的文本标记为 actual-heard。
4. LiveKit 是默认和 fallback；灰度配置缺失或签名配置错误时 fail closed 回 LiveKit。
5. 原始音频、长期记忆和 provider secret 不进入媒体契约。

## 参考实现与真实验收的边界

代码层面的 Go 状态机、coturn 凭证算法、Redis 适配、Linux AEC、OTA 签名和自动回滚
门禁已经有单测/竞态测试；它们不是对生产外部系统的成功证明。以下仍需要独立部署、
真实硬件/音频夹具和生产演练：

- 真正的 WebRTC WHIP/RTP/DTLS/SRTP 终结、Media Edge 到 bridge 的 mTLS 部署和
  provider adapter 到真实 FunASR/LLM/TTS 的生产音频会话；仓内 registry 只完成
  fake-provider 的可回归闭环；
- coturn relay 比例、Redis 多实例故障切换和 Media Edge drain；
- Linux 麦克风/扬声器实际 AEC 校准、儿童授权语料（150--300 条）和全部 SLO；
- 签名 OTA 的 A/B 分区断电回滚与设备物理静音验收。

在这些外部证据齐全前，不得把 `MEDIA_RUNTIME_DEFAULT` 改为 `streamcore`；LiveKit
仍是生产默认和所有 fallback。
