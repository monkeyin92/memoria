# Media Runtime 代码来源边界

本轮新增的 `voice_core`、`services/media_edge`、H5 transport、`.proto` 和
Control API media façade 均为 Memoria 自有实现：没有复制 StreamCore、Pion、
LiveKit 或其他仓库的源代码、生成物、商标或专有配置。

实现只借鉴公开协议/工程思想并重新组织为本项目的 sample-clock、epoch、generation
和权限契约。生产 LiveKit 依赖仍保留在原有链路中；它是运行时依赖，不是本轮新增的
Media Edge 源码。`grpcio`、`cryptography`、`redis`、`prometheus-client`、OpenTelemetry SDK
等现有依赖只通过公开 API 使用，未内嵌第三方源码。

`services/media_edge` 和 `LinuxMediaDeviceClient` 特意不重新实现 DTLS/SRTP/Opus/RTP
或硬件 I2S 驱动：这类安全敏感协议若
“自写一套”反而会造成更高的安全、互操作和维护风险。它提供的是自有状态机、契约、
鉴权、队列与控制面；真实 WebRTC 终结必须经过独立安全审查和部署验收。

`services/media_edge/gen` 中的 Go 文件和 `services/agent/src/voice_core/generated` 中的
Python 文件均是从 `packages/proto` 内 Memoria 自有 schema 生成的 bindings，不是从
StreamCore/Pion/LiveKit 仓库复制的 generated output；生成脚本和依赖版本会在构建时
重新验证漂移。
