# Memoria Media Edge（自有参考实现）

这里是一个不依赖第三方开源媒体框架的 Media-only 边缘：它实现
`media-v1` 的 Session Directory、sample range、generation gate、reconnect
epoch、设备/会话 HTTP 控制面、Prometheus 基础指标，以及到 Voice Core 的
带 mTLS 门禁的双向 gRPC 适配器。Go protobuf 代码由仓库内自有 proto 生成，
没有复制任何媒体框架源码。

它刻意不复制 Pion/StreamCore/LiveKit 的 RTP、DTLS、SRTP 或 Opus 实现。那些
协议栈属于独立的部署选择，错误地重新实现会把安全和版权风险一起带进主链。
因此 HTTP 参考端点仍用于契约、状态机和故障演练；真实协议终结器只需把已
审核的 PCM 帧交给 `VoiceCoreBridge`，并完成真实音频验收后才能进入灰度。

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

## 验证

```bash
go test ./...
go test -race ./...
MEDIA_EDGE_JWT_SECRET='...' ENVIRONMENT=development \
  MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT=true go run .
```

生产环境必须配置至少 32 字节的 `MEDIA_EDGE_JWT_SECRET`，且它必须与 Control
API 的 `STREAMCORE_TOKEN_SECRET` 使用同一份 secret（不要复制成第二份可漂移的
密钥）；并在网关层完成 mTLS、限流和真实 WebRTC 终结。参考 HTTP 端点不会接收
长期模型/用户密钥。无 secret 的本地测试必须显式设置
`AllowInsecureDevelopment=true`，默认 fail closed。
