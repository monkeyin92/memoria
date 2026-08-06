# StreamCore 全双工架构升级复核

## 2026-08-06 阶段推进补充

本轮按文章建议关闭了可在仓内证明的性能与控制面缺口：Voice Core 有界 Audio Pump/尾超时/有序 LRU/分 lane 出站队列，Go Edge 使用 Ring Buffer、Opus FEC/PLC 和固定工作缓冲，H5 使用统一采集约束、transceiver、ICE 有界重试、三条 DataChannel 与 client/server 单调时钟分离。H5 StreamCore 远端音频已接入 AudioWorklet 软件播放图，按渲染帧产生 generation-fenced sample watermark；Python `media_session.py` 与 Go 的 WebRTC/Actor 职责已按 ingress、turn、output、audio、DataChannel 和 shadow 规则拆分。Media Edge 具备 Ed25519/EdDSA `kid`/JWKS 验证和可选公私监听器。实时天气查询已接入 Open-Meteo/Qwen resolver，实时 delegation 先播确认语，再按 generation 回告；静态“我不知道”不会抢先结束实时请求。

这些仓内结果不改变生产边界：Worklet 路径的 `approximate: false` 只表示软件播放图处理过 watermark，不是浏览器 DAC 或扬声器的实际出声证明；`currentTime` 回退继续标记为 approximate。Redis 多实例 ownership、真实 Provider/TURN/浏览器/硬件/儿童语料/容量/Chaos/灰度和回滚证据仍开放；`go_authoritative` 继续 fail closed。

## 结论

**架构判断和实施路线已经完成；架构升级本身尚未完成。**

生产默认仍是 `python_authoritative + LiveKit`。在 A6B 前，`go_authoritative`
必须继续 fail closed，不能因为本文件中任一“仓内完成”项而切换流量。

当前尚未闭环的工作只有以下五类：

- F0：提交当前修复并取得新的远端 CI 全绿证据；
- A6A：真实 Qwen/Doubao provider smoke，以及 StreamCore 实际需要的输出来源；
- A2/A3/A7：真实浏览器、TURN、provider、设备和端到端 SLO 证据；
- A8：授权生产 shadow、fixed-set parity、容量和数据生命周期证据；
- A9：真实语料、硬件、多实例、chaos/load、灰度和回滚验收。

## 当前完成度

状态定义：

- **已完成（仓内）**：当前约定范围的代码和本地回归已经闭环；真实环境验收另列；
- **部分完成**：仍有仓内实现或外部证据缺口；
- **未开始**：尚未进入允许启用的迁移阶段。

| 阶段 | 状态 | 剩余门槛 |
| --- | --- | --- |
| F0 | **部分完成** | 提交当前修复后取得新的远端 CI 全绿 |
| A0 | **已完成（仓内）** | 与真实 A2/A7 会话共同验收 |
| A1 | **已完成（仓内）** | 与真实 A2/A7 会话共同验收 |
| A2 | **已完成（仓内）** | 真实 TURN、provider、跨主机浏览器 playout/stop |
| A3 | **已完成（仓内）** | 真实 provider + 浏览器媒体 smoke |
| A4 | **已完成（仓内）** | 长会话性能数据由 A8 统一追踪 |
| A5 | **已完成（仓内）** | 授权生产 shadow、fixed-set parity、长会话容量 |
| A6A | **部分完成** | 真实 provider smoke 与产品需要的输出来源 |
| A6B | **未开始** | A7/A8/A9 门槛后逐状态迁移、回退验证 |
| A7 | **部分完成** | 真机 renderer/AEC、真实 playout、trace、停止词和普通插话 SLO |
| A8 | **部分完成** | 授权生产 shadow、fixed-set parity、CPU/heap/队列上界、保留/删除审计 |
| A9 | **部分完成** | 真实儿童授权语料、硬件 AEC、多实例、真实 chaos/load、灰度、回滚和发布证据 |

## 已关闭（仓内）

- [x] **C1**：正式 `FloorEffect` 已由 Python Core 经 Go/WHIP DataChannel 传到 H5
  `floor.state`；identity、generation fence、单调 `floor_epoch`、TTL、candidate 和 A6B
  gate 均有回归。
- [x] **C2**：StreamCore 的 realtime-search 已收口为
  `media_deep_response -> DEEP_RESULT -> OutputWork`；该路径不会再产生普通
  `CONVERSATION_REPLY` 的重复 resolver 调用。
- [x] **C3**：realtime delegation 的 `FAST_ACKNOWLEDGEMENT` 已进入正式 `OutputWork`；确认语
  先经 output owner 门禁，MediaSession 在 playback ACK 后才让 `DEEP_RESULT` 接替，Agent
  流式路径也沿用同一 generation fence，迟到或过期结果丢弃。
- [x] **C7**：实时意图会覆盖响应规划器的静态 `direct_text`，并在 Agent 与 MediaSession
  两条链路统一使用 `REALTIME_UNAVAILABLE_REPLY` fail closed；危害/危机混合语句仍由固定
  安全回复优先。成功、慢查询、无结果、generation 变化和安全边界均有回归。
- [x] **C8**：H5 StreamCore 播放 ACK 已接入 AudioWorklet 渲染 watermark，并对 generation、静音、seek、flush、重连和关闭保持 fence/连续前缀门禁；不支持时回退为 `currentTime` 近似 ACK。该项只关闭软件播放图实现，不关闭真实浏览器/DAC 门禁。
- [x] **C9**：Python Voice Core 已保留单一 `MediaVoiceCoreRegistry` facade，并把有界音频 ingress、话轮端点和输出仲裁拆出；Go 已把 WebRTC 音频、DataChannel 转发和 shadow speech/output/comparison 拆出。拆分不改变 Python 权威或 Go shadow 边界。
- [x] **C4**：当前工作区已留下 CI 同镜像 PostgreSQL/pgvector 的全量 Python 与 coverage
  通过记录（`1856 passed, 2 skipped`，总 coverage `87.70%`）。
- [x] **C5**：StreamCore 的物理 PCM 执行面只允许 `CONVERSATION_REPLY`、
  `FAST_ACKNOWLEDGEMENT`、`DEEP_RESULT`；预留 kind 在边界 fail closed。
- [x] **C6**：本机 proto、Go lint/test/race、镜像构建和 Trivy 门禁已通过；镜像依赖
  `golang.org/x/crypto` 已升至 `v0.52.0`。

这些条目不再作为开放问题追踪，也不构成 A6B 或 `go_authoritative` 的启用依据。

## 当前开放问题

### R1 [P1] F0 远端 CI 尚未关闭

远端最新 run 是 2026-08-04 的
[`#30889467167`](https://github.com/monkeyin92/memoria/actions/runs/30889467167)。除 Python job
外其他 job 均通过；Python job 因 `funasr_stt.py` 导入未显式导出的 `ASRResult` 在 mypy 失败，
后续步骤被跳过。当前工作区的 `uv run mypy services --strict` 已通过，但代码尚未提交并触发新的
远端 run。

关闭条件：提交当前工作区，并以新的远端 CI 全绿作为唯一 F0 关闭证据。

### R2 [P1] A6A 缺真实 provider smoke 和完整 StreamCore 输出矩阵

当前可执行的 StreamCore 输出只有 `CONVERSATION_REPLY`、`FAST_ACKNOWLEDGEMENT`、
`DEEP_RESULT`。其中 realtime delegation 只在已配置 Qwen resolver 时启用；本机缺少
`DASHSCOPE_API_KEY` 和 Doubao TTS 认证，尚未发起真实请求，因此不能把 fake/offline 回归视为
provider 通过。

`TOOL_RESULT` 的表述需要特别区分：LiveKit Agent 路径有工具结果生产者，但 StreamCore 的物理
执行边界会明确拒绝它。也就是说，缺的是“被支持的 StreamCore source + OutputWork + PCM 路径”，
而不是系统中完全没有工具结果来源。`BACKCHANNEL`、`REMINDER`、`NOTIFICATION` 目前同样仅保留
wire/shadow 兼容，不能播放。

关闭条件：

1. 用真实 Qwen/Doubao provider smoke 验证 `DEEP_RESULT`，包括 stale result 不播放；
2. 新增任一输出 kind 前，补齐产品生产者、权限设计和独立 `OutputWork` 回归；
3. A6B 前继续拒绝 `go_authoritative`。

### R3 [P1] A2/A3/A7 缺真实端到端证据

仓内 loopback、fake provider 和 H5 壳回归不能替代真实语音链路。仍需：

- 真实浏览器 + TURN + production provider 的双向媒体；
- 跨主机 stop/reconnect 后旧 generation PCM 为 0；
- 实际扬声器 playout ACK、Linux/设备 renderer 和 AEC reference；
- 客户端 -> Edge -> Core -> sender -> playout 的 trace；
- 停止词开始 duck P95 `<=80ms`、完全停止 P95 `<=180ms`；
- 普通插话 P50/P95/P99、明确打断成功率 `>95%`、附和误停止 `<3%`。

### R4 [P1] A8 缺生产 shadow 与容量模型

指标接线和本地 comparison ledger 已存在，但尚不能回答并发长会话容量、CPU/heap、20ms frame
deadline、队列年龄、provider WebSocket 上界或 fixed-set parity。

关闭条件：从内部授权账号开始无副作用 shadow；candidate 不播放、不调用有副作用工具、不写历史/记忆；
输出 Timeline/Floor/Output mismatch 报告；记录长会话资源与 provider reconnect；数据授权、保留、
加密、删除可审计。

### R5 [P1] A9 外部验收、回滚和发布未完成

仓内 synthetic harness 不能替代真实环境。仍需至少 200 条授权、脱敏、分层儿童语料，真实硬件 AEC，
多实例 Redis ownership/session reclaim，TURN/provider/Redis/实例故障注入，灰度和
Go-authoritative -> Python/LiveKit 回滚演练。

发布前必须同时满足真实媒体 E2E、provider smoke、PCM/字级时间戳和全部 SLO；旧 ASR final 跨话轮
为 0、generation cancel 后旧 PCM 为 0；播放期间麦克风有效率 100%、前端密钥扫描为 0；CI、race、
lint、镜像安全和 production build 全绿；证据写入 `docs/releases/`。

## 架构升级判断

该判断已完成，后续不需要重新讨论方向，只需按门槛实施和验收：

1. 连续媒体和交互状态是实时系统主事实，离散聊天话轮是应用层投影。
2. Turn Detector 只控制权威提交、高风险副作用和正式输出许可，不阻塞 ASR、duck、KWS、prefetch
   或 warmup。
3. Go 只负责不能停顿、不能乱序、不能被业务阻塞的实时状态；Python 负责 Projection、
   Delegation、Context、Memory、speaker/owner policy 和工具权限。
4. 同一种状态只有一个权威写者；shadow 可以双算，但 candidate 不执行真实 effect。
5. provisional 允许修订；长期历史、记忆、分析和高风险工具只消费 committed +
   `history_eligible=true`。
6. 新链路必须先 shadow、再逐状态迁移，不能一次性切换或双写。

目标模块边界、迁移/回滚契约以
[`ADR-0029`](docs/adr/0029-continuous-interaction-authority-and-shadow-migration.md)、
[`media-runtime-foundation.md`](docs/media-runtime-foundation.md) 和
[`media-runtime-acceptance-runbook.md`](docs/media-runtime-acceptance-runbook.md) 为准。

## 推荐执行顺序

```text
F0 当前工作区与远端 CI
  -> A6A 真实 provider 与输出来源
  -> A2/A3/A7 真实浏览器、TURN、设备与 SLO
  -> A8 授权 shadow、fixed-set parity、容量
  -> A9 回滚就绪
  -> A6B 逐状态 authority 迁移
  -> A9 灰度与发布验收
```

每次只迁移一种状态写权；任一门槛失败时回到 `python_authoritative + LiveKit`。

## 本次复核的本地证据

2026-08-06：`uv run mypy services --strict` 通过（228 source files），Python 全量 `pytest --no-cov` 通过；架构相关的
MediaSession/Provider/Delegation/Authority/Factory 定向 Python 回归通过；H5 为
`26 files / 300 tests` 通过且 production build 成功，Worklet 静态资源在本地预览返回 `200 text/javascript`；Go 的
`go test ./...`、`go test -race ./...` 和 `go vet ./...` 通过。应用内浏览器 H5 壳无 console error，但其沙箱未暴露
Web Audio API，因此不能替代真实浏览器 Worklet/DAC 证据。没有新的远端 CI、真实 TURN/provider、硬件、生产 shadow 或回滚证据，因此 R1-R5 保持开放。
