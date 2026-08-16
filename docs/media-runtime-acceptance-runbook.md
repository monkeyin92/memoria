# Media Runtime 生产验收与回滚 Runbook

这份 runbook 把仓内可执行检查和必须在独立环境取得的证据分开。它不把 synthetic
replay、fake provider 或本地 gRPC 测试当成真实 WebRTC、硬件或 Provider 的替代品。
所有命令都不应把 token、私钥、原始儿童录音或完整音频写入日志。

## 1. 发布前仓内门禁

```bash
uv run pytest -q
uv run ruff check services scripts packages
uv run mypy services --strict
uv run --extra dev python scripts/generate_media_proto.py
uv run python scripts/media_runtime_smoke.py
uv run python scripts/media_runtime_replay.py

cd services/media_edge
go vet ./...
go test ./...
go test -race ./...

# 如果启用 media-runtime profile，先生成并安装独立 edge env；默认 LiveKit 不需要。
python scripts/split_production_env.py --source /root-only/memoria.env \
  --media-edge /etc/memoria-media-edge.env
```

H5 还必须通过 `npm test -- --run` 和 `npm run build`。Go bindings 由
`scripts/generate_media_go_proto.sh` 从仓库自己的 proto 生成；生成工具版本和
`services/media_edge/go.mod` 需与 CI 的 Go 版本保持兼容。

### 1.1 真实 Provider smoke（仅受控环境）

CI 使用 `OFFLINE_MOCK=true`，该模式只能证明脚本会明确跳过，不能作为 Provider 验收。把
`DASHSCOPE_API_KEY` 与 Doubao TTS 认证通过受控 secret 注入到独立验收环境后，运行：

```bash
OFFLINE_MOCK=false MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
  uv run python scripts/provider_smoke_test.py
```

命令必须返回 `PASS`；它会检查 FunASR、隔离的 Qwen realtime-search、主 LLM、Doubao 与
播放期语义分类。缺少认证或 `SKIP` 结果均为未通过。日志只保留 provider 名称、fence、
时间戳和通过/失败原因，不记录 token、原始音频或完整转写。

## 2. 真实环境验收顺序

1. 先保持 `MEDIA_RUNTIME_DEFAULT=livekit`、`STREAMCORE_EXPERIMENT_PERCENT=0`，记录
   LiveKit 的首音、停止、失败率、stale generation 和跨轮 final 基线。
2. 用短期 Control API session JWT 创建一条 H5 session，确认 `stream_epoch`、WHIP
   URL、TURN credential 的 TTL 和 audience；不能把永久 Provider key 下发给浏览器。
3. 由经过安全审查的 WebRTC/WHIP 终结器接入 Media Edge；验证 RTP/Opus 解码后的 PCM
   帧能以连续 `capture_start_sample` 送入 `VoiceCoreBridge`，并以 mTLS 连接 Python
   `MediaBridgeGrpcServer`。不得用 HTTP 参考端点替代这个验收。
   启动前确认 `/etc/memoria-media-runtime/` 中的 Voice Core server/client cert、key
   和 CA 与两端 env 路径一致；edge `/readyz` 必须在 Core gRPC stream 可达后才返回 200。
4. 注入现有 FunASR/Qwen/Doubao provider adapter，执行：正常话轮、两段 VAD 合并、
   ASR 断线重连、迟到 final、TTS 取消、重复 Stop、播放 ACK 覆盖部分/全部文本。对
   `DEEP_RESULT` 还必须在 resolver 已开始后触发新话轮或 Stop，证明旧 fence 的结果没有
   产生下行 PCM 或新的 playout ACK。证据必须带 `session_id + stream_epoch + turn_id +
   generation_id + tool_epoch`，不要保存原始音频。
5. 在 H5 和 Linux 设备分别测：用户开口 duck、硬停止延迟、旧 generation 不播、断网
   新 epoch 重连、StreamCore 失败后的 CAS LiveKit fallback、TURN relay 比例、DataChannel
   关闭后的 HTTP stop fallback；fallback 后再次点击停止必须仍能到达 LiveKit room。
6. Linux 设备再做真实 ALSA/I2S/DMA 播放进度、外放 AEC、物理静音、设备证书吊销和
   A/B OTA 断电测试。`voice_core/ota.py` 只是 boot metadata 状态机，不能替代真实
   bootloader 的双分区验收。
7. 使用监护人授权且最终不少于 200 条的儿童语料（建议 200--300 条），单独记录 CER、Stop/KWS 召回率、附和误
   打断率、电视/远场负例和隐私删除证明。仓库的 synthetic manifest 不满足此项。

### 2.1 从 golden trace 生成 T4–T7 候选回执（collect）

一次真实板端会话（固件串口/屏幕回执 + Edge + Voice Core 日志按 `session_id +
stream_epoch` 归并）可整理为 golden trace JSON，再由验收编排器生成 T4–T7 候选回执：

```bash
uv run python scripts/hardware_realtime_acceptance.py collect \
  --trace golden-trace-20260815-101500.json \
  --tag direct-t4t7-20260815 --output ./acceptance-collected
```

Golden trace 契约（`schema_version=1.0`、`trace_type=memoria_golden_trace`）：

- 顶层固定字段：`origin`（仅 `real_device` 可产生 pass）、`session_id`、
  `stream_epoch`、`collected_at`（必须带时区）、`device`（与回执 device 块同构）、
  `events`。
- 事件白名单：`session.accepted / vad.start / vad.end / uplink.audio / asr.final /
  turn.committed / user.text.injected / llm.reply / tts.started / playback.started /
  playback.ended / error`。未知事件或未知字段直接拒绝；事件时间必须单调不倒退；
  `session.accepted` 必须是首个事件且 `session_id/stream_epoch` 与顶层一致。
- `asr.final` 需要 `engine=funasr`、`displayed` 布尔值；`playback.ended` 需要
  `ack`、`dac_verified` 布尔值与 `watermark_precision`。
- `user.text.injected` 与 `llm.reply` 必须携带正整数 `generation_id`；T5–T7
  要求 TTS、Playback Start/End、Turn/LLM 事件绑定同一 Generation，并按真实因果
  顺序出现。仅有另一代次的播放 ACK 不得为当前话轮放行。

Fail-closed 规则（collect 永不把软件证据升级为真机证据）：

- `origin=repository|mock` 的 trace 被直接拒绝；仓内软件回放走 `run` 的 probe。
- 缺 `reference_text/recognized_text`（无法计算 CER）→ T4 输出 `blocked`。
- T5/T6/T7 的 `pass` 必须同时满足 `playback.ended` 的 `ack=true`、
  `dac_verified=true`、`watermark_precision=exact`；缺 DAC/actual-heard 证据一律
  输出 `blocked` 并写明缺失项。当前板声明 `playback_watermark=approximate`（无 DAC
  样本计数），因此在真机提供精确水位证据前，T5–T7 只能得到 blocked 候选。
- trace 中出现任何 `error` 事件时，T4–T7 全部输出 `blocked`。
- 输入 trace 由验收操作者提供，`collect` 只生成候选回执，不认证采集来源，也不能
  单独替代并发串口、服务器日志和用户 Actual Heard 确认。

输出到 `--output` 目录：`T4-<tag>.json ... T7-<tag>.json` 与
`evidence/golden-trace-<tag>.json`（回执引用的证据文件，带 sha256）。复制到证据目录
的 trace 会把 ASR reference/recognized、注入文本、LLM 回复和错误消息替换为
`[REDACTED]`，不复制真实对话原文；原始 trace 由验收操作者按最小权限和保留期
单独管理。每个回执在
写出时已通过 `verify` 的 schema/TTL/证据哈希自检；pass 回执的 `collected_at` 取自
trace，TTL 按 `real_hardware` 类别 72 小时计。命令退出码：0=全部 pass；2=已写出但
存在 blocked 候选；1=trace 非法或拒绝。候选回执仍需正式验收：

```bash
uv run python scripts/hardware_realtime_acceptance.py verify \
  --dir ./acceptance-collected \
  --evidence-root ./acceptance-collected/evidence
```

collect 只整理与降级，不创造真机事实：任何回执仍必须通过 `verify` 的 schema、
TTL、路径/hash 与 per-item scenario 门禁，并保留原始 trace 作为证据 artifact。

## 3. 灰度与回滚

- 灰度只由 Control API server-owned runtime、`STREAMCORE_EXPERIMENT_PERCENT` 和
  `STREAMCORE_KILL_SWITCH` 控制；客户端不得自行选择 runtime。
- `MediaSLOGate` 只接受新鲜、完整、allowlist 聚合指标。报告缺失、Redis 不可用、
  provider/WHIP/硬件验收失败，均保持或回退到 LiveKit。
- 回滚步骤：先把 gate 置为 `livekit`/kill switch，停止新 StreamCore session，设置
  Media Edge readiness=false，等待或通知旧 session 以新 `stream_epoch` 重连；确认
  LiveKit 基线恢复后再处理旧 edge。不要清空队列来“取消”旧 generation。
- 发布记录至少包含：代码 SHA、配置版本、基线/灰度 SLO、失败原因、Redis/coturn
  状态、回滚时间、外部验收 artifact 路径。不得把 secret、完整 JWT 或音频写入记录。

## 4. 当前明确未证明的项目

仓内测试已经证明协议、状态机、fence、mTLS material loader、fake provider 和
synthetic replay 的行为；但在真实 WebRTC/Provider/浏览器网络矩阵、Redis 多实例、
coturn relay、Linux 声学、儿童授权语料和生产回滚演练完成前，不能把 StreamCore
路线设为默认，也不能宣称达到计划中的全部 SLO。
