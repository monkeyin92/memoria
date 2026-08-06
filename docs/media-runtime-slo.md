# Media Runtime SLO 与回滚门禁

`services/agent/src/voice_core/slo.py` 是 rollout controller 共用的最小判定
面。它只消费聚合指标，不消费音频、字幕或长期记忆：

- 首音 P95 默认不超过 1200 ms；
- 硬停止 P95 默认不超过 250 ms；
- 建连失败率默认不超过 2%；
- stale generation 与跨轮 ASR final 必须为 0。

其中 250 ms 是 StreamCore 早期灰度的 fail-closed 回滚门槛，不放宽
`full_duplex_voice_agent_architecture_zh.md` 的产品 DoD：明确打断开始 duck P95
仍需 `<=80 ms`，完全停止 P95 仍需 `<=180 ms`。

任一门禁失败都会产生 `rollback_required=true`。Control API 只需返回
`media_runtime=livekit` 即可回滚，不需要重新发布 H5；`STREAMCORE_KILL_SWITCH`
仍是人工紧急开关。

当 `STREAMCORE_SLO_GATE_ENABLED=true` 时，Control API 只接受带
`X-Media-SLO-Token` 的内部 `POST /v1/internal/media-runtime/slo` 报告；报告存储带
TTL，过期或 Redis 不可用会 fail-closed 到 LiveKit。生产还要求独立的
`MEDIA_SLO_REPORT_TOKEN`，不能复用用户或 Provider 凭证。仓库提供独立的
`media-slo-reporter` Compose sidecar 和 `scripts/run_media_slo_reporter.py`；sidecar 从
Agent 的 Prometheus endpoint 读取聚合值，只发送 allowlist 指标，不发送音频、字幕或
session 内容。指标缺失时不会填充为 0，Control
API 会按 fail-closed 处理；sidecar 停止或报告过期同样回退 LiveKit。当前仍没有真实
Collector/Grafana/自动 rollout controller 的生产证据。

仓库中的 synthetic replay 只能验证门禁和回滚逻辑，不能证明真实音频 SLO。
生产切换前必须把真实 H5、Linux AEC、TURN relay、重连和儿童语音回放结果
写入发布记录，并完成一次实际回滚演练。
