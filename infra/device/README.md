# 硬件娃娃参考实现

当前可复用的自有参考代码在
`services/agent/src/voice_core/device_runtime.py`、`device_client.py` 与
`device_security.py`：

- Linux SBC 先用实际扬声器 PCM 作为 NLMS AEC reference，再送 VAD/ASR；
- 每个采集帧带 `stream_epoch`、`sequence`、`capture_start_sample`；
- 每台设备用 Ed25519 身份和 signed challenge，不共用 API key；
- OTA manifest 必须 HTTPS、SHA-256、Ed25519 签名、A/B 回滚策略；
- 物理静音、最大音量、看门狗和本地紧急停止属于硬件验收项。

`LinuxMediaDeviceClient` 提供 media-v1 gRPC 客户端边界：有界采集队列、stream
epoch 重连、播放 PCM 回调、`PlaybackProgress` 精确 ACK、设备命令 TTL/allowlist
和本地静音。它把 ALSA/I2S 读写作为回调注入，因此不会把阻塞硬件调用放进媒体接收
线程；当前仍需要在目标 Linux 板上接入真实 I2S DMA 并完成校准。

仓库不提交儿童录音、私钥、固件或 Wi-Fi 凭据。`child-speech-corpus.json`
只包含 CI synthetic manifest；真实 150--300 条语料必须由监护人授权后在
隔离 fixture store 中挂载。

ESP32-S3 只能作为 PTT/外壳原型，不能在没有独立声学 AEC 的情况下宣称
全双工量产能力。Linux AEC 收敛并完成实机录音回放后，才评估更低成本 SoC
或独立 AEC DSP。

## Control API 绑定流程

量产设备先在受控 provisioning 流程中调用：

1. `POST /v1/devices/{device_id}/identity` 注册公钥（只接受服务端预置的
   `account_id`/设备归属）；
2. `POST /v1/devices/{device_id}/challenge` 获取短 TTL、单次使用 nonce；
3. 设备用私钥签名后，把 `device_proof` 提交给媒体会话创建接口；
4. 怀疑泄露时调用 `POST /v1/devices/{device_id}/revoke`，后续 challenge/proof 立即失效。

生产环境不能用 bearer bootstrap 或离线 mock；媒体 bridge 还必须在独立网络上使用
客户端证书 mTLS。上述 endpoint 只验证身份和会话权限，不接收模型密钥、长期账号 token
或原始录音。

## SLO 报告

Voice Core 可用 `/app/scripts/run_media_slo_reporter.py` 作为可选 sidecar，向
`/v1/internal/media-runtime/slo` 发送仅包含聚合延迟、失败率和 stale 计数的报告。
TTL 过期、token 错误或 Control API 不可达时，灰度门禁自动回到 LiveKit；不要把
reporter 的“进程运行”当作声学 SLO 通过。
