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
   ASR 断线重连、迟到 final、TTS 取消、重复 Stop、播放 ACK 覆盖部分/全部文本。
   证据必须带 `session_id + stream_epoch + turn_id + generation_id + tool_epoch`，
   不要保存原始音频。
5. 在 H5 和 Linux 设备分别测：用户开口 duck、硬停止延迟、旧 generation 不播、断网
   新 epoch 重连、StreamCore 失败后的 CAS LiveKit fallback、TURN relay 比例、DataChannel
   关闭后的 HTTP stop fallback；fallback 后再次点击停止必须仍能到达 LiveKit room。
6. Linux 设备再做真实 ALSA/I2S/DMA 播放进度、外放 AEC、物理静音、设备证书吊销和
   A/B OTA 断电测试。`voice_core/ota.py` 只是 boot metadata 状态机，不能替代真实
   bootloader 的双分区验收。
7. 使用监护人授权的 150--300 条儿童语料，单独记录 CER、Stop/KWS 召回率、附和误
   打断率、电视/远场负例和隐私删除证明。仓库的 synthetic manifest 不满足此项。

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
