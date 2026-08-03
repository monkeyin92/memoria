# Memoria media v1

这是 Memoria 自有的媒体面/Voice Core 契约，不是第三方项目的代码副本。它只
承载媒体时钟、事件和 generation fence；LLM、记忆、人格、工具和供应商密钥不
进入该协议。

`.proto` 是唯一稳定契约；Python Voice Core 的生成物位于
`services/agent/src/voice_core/generated/`，使用仓库 dev extra 中的
`grpcio-tools` 生成：

```bash
uv run --extra dev python scripts/generate_media_proto.py
```

`MediaBridgeGrpcServer` 使用这些生成物提供真实双向 `VoiceMediaBridge.Connect`
流；JSON 实现仍保留给 LiveKit 迁移和纯单测。新增字段必须保持向后兼容，并通过
`stream_epoch`、`sequence` 和 `generation_id` 拒绝迟到事件。
