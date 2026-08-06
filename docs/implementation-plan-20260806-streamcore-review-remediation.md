# StreamCore 评审整改阶段计划

更新日期：2026-08-06

本文把评审文章中的建议拆成可在仓内验收的代码阶段和必须在真实环境取得的证据阶段。生产默认在全部外部门禁关闭前保持 `python_authoritative + LiveKit`。

## 阶段一：实时正确性与联网查询

状态：仓内完成。

- Voice Core 使用按 session 的 singleflight、有限创建并发、有限 Audio Pump、Adaptive Endpoint 和绝对尾超时。
- ASR final 去重使用有序 LRU；gRPC 输出分为 Critical、Reliable、Coalescing 三条逻辑 lane。
- Go Edge 使用优先级 actor mailbox、固定容量 Ring Buffer、20 帧音频上限和 `loss_concealed` 透传；Opus 缺帧优先 FEC/PLC，失败才补静音。
- 实时查询先发 `稍等，我查询一下。`，后台调用 `PublicRealtimeSearch`；天气优先使用无密钥 Open-Meteo，其他查询在有凭据时使用 Qwen forced search，结果受 generation fence 约束。实时意图会覆盖响应规划器的静态 `direct_text`（包括“我不知道。”），但危害/危机安全意图仍由固定安全回复优先，避免旧 fallback 抢先结束生成或绕过安全门禁。

验收：`uv run mypy services --strict`、Python 全量回归、`go test -race ./...`、真实 Open-Meteo 南京 canary 均通过。

## 阶段二：客户端与媒体边缘控制面

状态：仓内完成，真实浏览器/设备媒体证据待执行。

- H5 所有麦克风采集统一约束；无麦克风启动时预留 audio transceiver，后续启用只替换 track。
- 非 Trickle ICE 未 complete 时不交换 SDP，并对 ICE/连接超时做一次有界重试。
- H5 与 Edge 支持 control、conversation、ephemeral 三通道；旧 `memoria.events.v1` 客户端继续兼容，ephemeral 不得挤占控制队列。
- 客户端事件使用 `client_monotonic_ms`，服务端事件使用 `server_monotonic_ms`；Edge 拒绝冲突时钟。
- StreamCore 远端音频默认接入 `MediaElementAudioSourceNode -> AudioWorkletNode -> destination`。Worklet 按渲染帧建立 generation-scoped sample watermark；静音、seek、flush、重连、关闭和不支持 Web Audio 时 fail closed 或回退为 `currentTime` 近似 ACK。
- `approximate: false` 在此路径仅表示软件播放图已经处理相应的 sample watermark，绝不表示浏览器 DAC 或扬声器已经实际出声。普通 WebRTC `MediaStream` 不提供可验证的逐 RTP/PCM 来源映射，硬件级 ACK 仍需要自有 PCM playout ring、可控解码链或设备反馈。
- Voice Core 的 `MediaVoiceCoreRegistry` 维持原有 facade seam，内部按 lifecycle、connection、ingress、projection、commit、turn endpoint、output dispatch/owner/stream 拆分；Go Edge 同样按 WHIP signaling、WebRTC audio/DataChannel、bridge event/shadow、actor mailbox、session/server 拆分。每条会话仍只有一个可变状态 owner，priority mailbox 还保留了跨 lane 的总容量门禁。
- Edge 可将 `/readyz`、`/metrics` 绑定 `MEDIA_EDGE_INTERNAL_HTTP_ADDR` 私网监听。

验收：H5 `26 files / 300 tests` 与 production build、Go real-media loopback、协议生成无 diff 均通过；本地浏览器已确认 Worklet 资源以 `200 text/javascript` 提供，并在 `MediaElementAudioSourceNode -> AudioWorkletNode` 图中取得递增渲染帧水位。该 synthetic-media 证据不替代真实跨主机 WebRTC、DAC 或设备媒体验收。

## 阶段三：密钥与运行观测

状态：仓内完成，生产密钥轮换演练待执行。

- Control API 支持 Ed25519/EdDSA + `kid` 私钥文件或 PEM；Media Edge 支持 PEM/JWKS 多 key 验证、TTL、`nbf/iat/exp` 和时钟偏差校验。
- HS256 只作为迁移 fallback；配置非对称公钥后 Edge 拒绝 HS256，Control API 不向 Edge 分发私钥。
- Edge 导出 PLC/FEC/静音 conceal、heap、malloc 和 GC pause 指标；malloc 指标是进程级累计值，不能冒充单帧精确分配率。

验收：Python/Go EdDSA 互操作单测、密钥轮换（双 `kid`）演练、镜像和安全扫描；本轮本地 Go/Python 门禁已通过，生产双 `kid` 演练仍待执行。

## 阶段四：外部生产 Gate

状态：阻塞于外部环境，不能由本地 mock 关闭；本机已完成单节点 Redis Lua CAS canary，但不替代多实例证据。

- 真实 Qwen、FunASR、Doubao provider smoke；真实 TURN、跨主机浏览器 playout/stop 和重连。
- H5/设备真实播放 ACK、Linux/ESP32 AEC、至少 200 条监护人授权儿童语料。
- Redis 多实例 ownership/fencing、并发容量矩阵、Provider/Pod/网络 Chaos、无副作用 shadow、灰度和回滚演练。

本轮可复现证据：真实 Open-Meteo 南京 canary、Redis 8.10 单节点 Lua CAS/owner fencing、Python/Go/H5 本地门禁均通过。多实例 Redis、真实 provider/TURN/硬件和发布演练仍保持 pending。

在这些证据齐全前，不启用 `go_authoritative`，不把 `STREAMCORE_EXPERIMENT_PERCENT` 提升到生产流量，也不把 AudioWorklet 软件播放图 watermark 宣称为硬件实际听到位置。
