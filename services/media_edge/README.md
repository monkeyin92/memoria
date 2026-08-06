# Memoria Media Edge（自有参考实现）

这里是 Memoria 自有的 Media-only 边缘。它实现 `media-v1` Session Directory、
sample clock、generation gate、reconnect epoch、设备/会话 HTTP 控制面、
Prometheus 基础指标、到 Voice Core 的双向 gRPC 适配器，以及真实
WHIP/WebRTC terminator。Go protobuf 代码由仓库内自有 proto 生成；ICE、DTLS、
SRTP、RTP 和 DataChannel 复用 Pion，Opus 上行使用 Pion decoder，下行只通过一层
最小 `libopus` encoder 包装，不自行重写媒体协议。

`POST /whip` 验证 Control API 签发的短期 JWT，从 token 绑定 session/account/device/
client/stream epoch，完成非 trickle offer/answer，并返回 `Location` 和 `ETag`。
`PATCH /whip/{resource}` 是 Memoria 的完整 SDP 非 trickle restart 扩展（要求
`If-Match`）；标准 WHIP client 只依赖初始 POST/DELETE，
`DELETE /whip/{resource}` 关闭对应 epoch。上行 Opus 转为连续 16 kHz mono PCM，
丢包按 RTP timestamp 补静音、乱序包丢弃，再送入 `VoiceCoreBridge`；下行 24 kHz
PCM 转为 48 kHz Opus 并写入 SRTP track。`memoria.events.v1` DataChannel 转发
transcript/projection/state/error/audio metadata，接收 stop、playout progress 和其他
client event。

真实 sender 通过 `DownlinkSenderFactory` 与 exact `(session_id, stream_epoch)` peer
绑定。sender 收到 generation-scoped `context.Context`；hard-stop 先取消 context 再
关闭 gate，后续旧 generation PCM 会在 Session/sender 两层拒绝。已经进入一次不可撤回
WebRTC write 的 RTP 包无法“撤回”，因此 Edge 同时立即发送 `playback.flush`，A7 仍须以
真实浏览器 playout 测量最终停止。新的 epoch 只有在 ICE/DTLS connected、sender 和
Voice Core bridge 全部创建成功后才替换旧 epoch；删除旧 WHIP resource 不会关闭新会话。开发用 HTTP
PCM 端点仍保留用于契约/故障测试，但 production readiness 必须同时看到真实
terminator、sender 和 Voice Core。

## Voice Core gRPC bridge

```bash
./scripts/generate_media_go_proto.sh
cd services/media_edge
go test ./...
go test -race ./...
```

生产连接必须提供 CA、客户端证书和私钥，并通过
`DialVoiceCore(VoiceCoreBridgeConfig{TLS: ...})` 建立通道；没有 TLS 时只有
显式的 `AllowInsecureDevelopment` 才会使用明文。`VoiceCoreSession` 会在
客户端再次校验 identity、事件 sequence、sample range 和完整 generation，
遇到旧帧返回错误并要求新 `stream_epoch`，不会靠清空队列取消旧回答。
`VAD_EVENT_SPEECH_END` 必须同时给出 `sample_position` 和
`voiced_end_sample`：前者是含 hangover 的事件时间，后者是尾静音前最后声学样本。
Voice Core 只以后一位置判断 ASR 是否覆盖完整话轮；缺字段或越过事件时间会
fail-closed，不能用固定 sample 容差猜测“尾静音还是迟到文本”。

会话握手的实时写权默认是 `python_authoritative`。只有 Edge 设置
`MEDIA_EDGE_INTERACTION_AUTHORITY=go_shadow` 且 Python bridge 同时设置
`MEDIA_BRIDGE_GO_SHADOW_ENABLED=true` 时，服务端才会回传 `go_shadow`；候选 Go
decision 仍不得执行 effect。`go_authoritative` 在 A6 parity、SLO 和回滚门槛完成前
会回退或拒绝，浏览器从 `session.ready.interaction_authority` 观测服务端实际选择。

`POST /v1/media/sessions/{id}/stop` 只取消当前 generation，不关闭 session 或 Voice
Core stream。调用方可提供完整 expected fence；未提供时 Edge 在当前 active fence 上
原子派生 `generation_id + 1`，并用 `Idempotency-Key` 固化、返回完整 replacement
fence。HTTP 与未来 DataChannel 适配器必须复用这个 current→replacement 语义。

`GET /v1/media/sessions/{id}/shadow` 使用同一 session JWT，只读导出当前
`LiveSessionActor` snapshot 和最近的 Python-vs-Go comparison ledger。该入口不执行
candidate effect；`shadow_decision_mismatch_total` 同时按 `scenario` 与
`contract_version` 标签导出，供 A5 parity 分析。

Actor 内已经有 A6A dormant `SpeechTimeline`、按来源域保留 candidate 的 `OutputArbiter` 算法和
typed Floor shadow parity，所有输出强制
`candidate_only=true`，且 actor 不持有 sender、工具或持久化依赖。Session/bridge 已通过内部
`shadow_observation` 投递 Timeline、typed Floor 与 sanitized OutputIntent 的生产事件和权威 after-state；
comparison 覆盖单事件 apply、rejected reason 和 lossy gap 分域 resync。完整多来源 Arbiter、
生产侧 typed Floor effect 与逐状态 authority 仍未实现，因此不能视为 A6B authority enable。

## WebRTC 网络配置

production 必须提供下面两种可达路径之一，否则 readiness 保持 fail closed：

- `MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON`：Pion `ICEServer` JSON 数组，至少含一个
  `turn:`/`turns:` URL；
- `MEDIA_EDGE_WEBRTC_PUBLIC_IPS` 加
  `MEDIA_EDGE_WEBRTC_UDP_PORT_MIN`/`MEDIA_EDGE_WEBRTC_UDP_PORT_MAX`。编排层还必须
  把同一 UDP 范围映射到容器。

readiness 每 5 秒实际执行一次 ICE candidate gathering；TURN-only 部署只有获得 relay
candidate 才可用。第二种方式适合 1:1 NAT；一般生产部署优先 TURN。Voice Core TLS 继续使用
`MEDIA_EDGE_VOICE_CORE_CA_FILE`、`MEDIA_EDGE_VOICE_CORE_CLIENT_CERT_FILE`、
`MEDIA_EDGE_VOICE_CORE_CLIENT_KEY_FILE` 和 `MEDIA_EDGE_VOICE_CORE_SERVER_NAME`。
构建机需要 `pkg-config` 与 `libopus-dev`，运行镜像已携带 `libopus.so.0`。

## 验证

```bash
go test ./...
go test -race ./...
MEDIA_EDGE_JWT_SECRET='...' ENVIRONMENT=development \
  MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT=true go run ./cmd/mediaruntime
```

生产环境必须配置至少 32 字节的 `MEDIA_EDGE_JWT_SECRET`，且它必须与 Control
API 的 `STREAMCORE_TOKEN_SECRET` 使用同一份 secret（不要复制成第二份可漂移的
密钥）；并在网关层完成 mTLS 与限流。参考 HTTP 端点不会接收长期模型/用户密钥。
无 secret 的本地测试必须显式设置
`AllowInsecureDevelopment=true`，默认 fail closed。
