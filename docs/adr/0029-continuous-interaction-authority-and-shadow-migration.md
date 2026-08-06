---
status: accepted
date: 2026-08-04
---

# 连续交互主事实、单写者与 Shadow 迁移

## 背景

Memoria 当前由 Python Voice Core 负责话轮、打断、generation、播放门禁和业务策略，Go
Media Edge 负责媒体接入、队列、传输 fence 与下行安全门禁。离散聊天消息不足以表达 ASR
修订、播放进度、重叠说话和打断等连续状态；但直接把全部业务迁到 Go，或让 Go/Python
同时修改 generation、floor 和输出队列，会产生无法审计的双写与副作用竞争。

本 ADR 只冻结架构边界和 additive 契约，不提前拆分网络服务，也不宣称 Go 实时内核已经
具备生产权威。真实媒体终结、Conversation Projection、Interaction/Delegation、Context
Snapshot、Go actor、shadow parity 和逐状态迁移分别由后续阶段完成。

## 决策

1. 连续媒体与实时交互事件是实时系统的主事实；离散话轮是 Python Conversation
   Projection 产生的应用视图。
2. 每类可变状态在任一 session mode 下只有一个权威写者。Shadow 可以镜像计算，但镜像
   不得回写权威状态。
3. 每个 effect 只有一个执行方。生产者可以产生候选 decision，只有 mode 指定的执行方可以
   触发播放、generation、工具或持久化副作用。
4. Go 只逐步接管不能停顿、不能乱序、不能被业务阻塞的实时状态。说话人/主人权限、工具
   权限、记忆、Persona、正式历史和 context policy 继续由 Python 持有。
5. 新客户端通过 `SessionHello.interaction_authority` 请求 mode，服务端通过
   `SessionAccepted.interaction_authority` 返回实际 mode。服务端选择是最终事实。
6. 旧客户端省略字段得到 `UNSPECIFIED`；未知或未来枚举值也必须 fail closed 为
   `python_authoritative`，不能因此获得 Go 权威或更高业务权限。

## 会话权威模式

| mode | 实时状态权威 | Go candidate | 真实实时 effect |
| --- | --- | --- | --- |
| `python_authoritative` | Python | 不要求 | 仅 Python |
| `go_shadow` | Python | 可以计算并记录 diff | 仅 Python |
| `go_authoritative` | Go | 不再是 shadow | 仅 Go；Python 只消费明确交付给业务面的 effect |

`go_authoritative` 只是冻结的目标契约。A5 后可以开发 A6A 的 Python-authoritative
effect transport 和不执行 effect 的 Go candidate；只有 A7 trace/SLO、A8 fixed-set shadow
parity/容量和 A9 回滚预案均有证据后，才允许通过 A6B 按单状态启用。部署配置在 A6B 前不得选择
该 mode。

截至 2026-08-05，A6A 已实现 actor 级 dormant `SpeechTimeline`、按来源域保留待决
`OutputArbiter` candidate，以及 Python 多 kind candidate metadata 的有界 after-state admission；Registry→bridge→Session→actor 的生产
`shadow_observation` 已接通。两类状态可以做无副作用 parity；lossy shadow 队列跳号时，Speech
与 Output、Floor 各自在收到本域下一份可比较的 authoritative after-state 后恢复，并把该事件单列为
transport discontinuity，不计为算法 mismatch。phase-derived typed Floor shadow evidence 已以 additive
`ShadowFloorDecision` 接入；Output 普通 observation 由 Go 独立 apply 后比较，只有 transport
gap resync 才覆盖完整权威集合；每个 rank 最多 4 个候选，同域/跨域 expiry fallback 均有回归。
生产侧普通主回复已通过 `CONVERSATION_REPLY` winner 取得 session-scoped PCM owner；异步 speaking
transition 后再次校验 generation/owner，取消先撤本地 lease 并排空 task，再做 best-effort provider
清理；全部已发送 PCM 与文本 span 获得 playback ACK 后才发送 consumed after-state。`OutputWork`/
source binding 已覆盖 normal reply、`tts_source`、`pcm_s16le`，queued candidate 会在旧 owner ACK 后
wakeup，高优先级 work 会立即撤销旧 owner、flush generation 并从 sequence/sample `0` 重启。最小
Python-authoritative `RealtimeEffect` 也已经接通：仅允许 duck/cancel/pause/resume，完整 fence
校验后由 Go runtime 执行，candidate 不执行。正式 `FloorEffect` 也已在 Python-authoritative 路径接通：
它携带完整 identity、generation fence、单调 floor epoch 与 TTL；Go 复验后仅安装 Python 所有的
floor snapshot，并经 WHIP DataChannel 发布给 H5。candidate、过期/旧 epoch 与 A6B 前的
`go_authoritative` 都 fail closed。当前 StreamCore 的物理 PCM 执行面只允许
`CONVERSATION_REPLY`、`FAST_ACKNOWLEDGEMENT`、`DEEP_RESULT`；`TOOL_RESULT`、backchannel、
reminder、notification 等 wire enum 仅用于兼容或 shadow metadata，未有真实生产者前必须在执行
边界拒绝。新增其中任一能力必须同时定义产品生产者、权限与独立 `OutputWork` 回归。真实 provider
smoke、逐状态切权和回滚仍未实现。因此当前状态仍是 Python authority，`go_shadow` 只用于候选比较，
`go_authoritative` 继续 fail closed。

### 迁移与回滚

允许的前进路径只有：

```text
python_authoritative -> go_shadow -> go_authoritative
```

进入 `go_authoritative` 必须有同一版本契约、固定评测集 parity、无副作用 shadow 证据、
容量、SLO 与回滚门禁。迁移清单必须显式包含 generation、playback watermark、实时
`SpeechTimeline`、OutputArbiter 和 Floor；遗漏 Timeline 时不得宣称 Go 已成为实时状态权威。
任何 mode 都可以直接回滚到 `python_authoritative`；Go 权威也可先退回
`go_shadow` 观察。回滚必须：

1. 停止接收旧 authority 的新 effect；
2. 递增 generation/stream fence，取消旧 sender 和待播放输出；
3. 清空旧权威的未提交队列；
4. 从已确认的连续事件、Projection 和 Context Snapshot 恢复；
5. 记录 mode、原因、旧/新 fence 和 contract version。

不能在一个事件处理过程中原地换 mode。切换只在 session 创建、重连或显式安全点生效。

## 状态 Ownership

下表中的“镜像”只能用于比较和指标，不具有写权。

| 状态 | `python_authoritative` | `go_shadow` | `go_authoritative` | 永久业务 owner |
| --- | --- | --- | --- | --- |
| 媒体 frame/sample clock、RTP/RTCP、jitter、边缘队列 | Go | Go | Go | Go Media Edge |
| playback watermark 与 sender cancellation | Python 决策，Go 安全门禁 | Python 决策，Go 镜像/安全门禁 | Go | 无第二写者 |
| generation gate/lifecycle | Python | Python；Go 镜像 | Go | mode 指定写者 |
| floor、duck/pause/resume、OutputArbiter | Python | Python；Go 镜像 | Go | mode 指定写者 |
| 实时 SpeechTimeline | Python | Python；Go 镜像 | Go | mode 指定写者 |
| provisional/committed conversation | Conversation Projection | Conversation Projection | Conversation Projection | Python |
| speaker evidence、owner/guest 分类与权限 | Python | Python | Python | Python |
| 工具许可、工具执行和 compensation | Python | Python | Python | Python |
| 正式历史、memory、Persona、evidence ledger | Python | Python | Python | Python |
| `context_version` 的 prepare/activate/CAS | Context Snapshot Manager | Context Snapshot Manager | Context Snapshot Manager | Python |

Go 的 transport safety gate 可以拒绝非法或过期 frame/effect，但拒绝权不等于业务状态写权；
它不得自行提升 generation、owner、tool permission 或 `history_eligible`。

## Effect 唯一执行方

| effect | Python 权威 / Go Shadow | Go 权威 | 说明 |
| --- | --- | --- | --- |
| `DUCK_OUTPUT`、`CANCEL_GENERATION`、`PAUSE_OUTPUT`、`RESUME_OUTPUT` | Python | Go | 只有 mode 指定 runtime 改实时状态 |
| `DROP_STALE_EVENT` | Python | Go | Edge 安全门禁仍可拒绝非法传输，但不能推进权威状态 |
| `EMIT_PROVISIONAL_PATCH`、`COMMIT_TURN_CANDIDATE` | Python Conversation Projection | Python Conversation Projection | Go 只提交候选证据，不写历史 |
| `START_DELEGATION` | Python Delegation Coordinator | Python Delegation Coordinator | Go 不执行工具或深度任务 |
| `ENQUEUE_OUTPUT_INTENT` | Python arbiter | Go OutputArbiter | Python 业务结果在 Go 权威下只能提交 intent，不能直发 TTS/PCM |
| 历史、memory、Persona、工具副作用 | Python | Python | 不属于 Go realtime effect |

`RealtimeEffect` 携带 source event、session/stream/sequence、generation fence、task/context
version 和完整 `SessionIdentity`。它已加入 `CoreToMedia.oneof`；Python authority 在
`python_authoritative`/`go_shadow` 只能发送 duck/cancel/pause/resume，Go 复验 legacy/full identity、
sequence、effect id、payload 和 fence 后才执行。Go shadow 产生的值必须设置 `candidate_only=true`；
执行门禁无条件拒绝 candidate，即使上游错误地把它送到生产 effect 通道。A6B 前收到
`go_authoritative` effect 同样拒绝。

## Conversation Projection

Projection 接收连续事件和 commit evidence，维护两类视图：

- `ProvisionalTurn` 可修订、可丢弃，可发布 started/patch/discarded；
- `CommittedTurn` 带完整 fence、speaker authority 和 `history_eligible`，只发布一次
  `turn.committed` 权威事实。

H5/设备可以显示 provisional patch，但正式历史、Memory、分析、高风险工具只能消费
`turn.committed` 且继续要求 `history_eligible=true`。Backchannel 可以是非持久交互事件，
不自动形成聊天消息。

## Additive 契约

`packages/proto/memoria/media/v1/` 增加：

- `InteractionAuthority`、`ContinuousEventKind`、`FloorState`、`FloorEffect`；
- `ContinuousInteractionEvent` 与 `SpeakerEvidence`；
- `RealtimeEffect`、`RealtimeEffectKind`；
- `OutputIntent`、`OutputIntentKind`、`FloorRequirement`；
- `ProjectionEvent`、`ProjectionEventKind`；
- `SessionHello` / `SessionAccepted` 的 typed authority 字段；
- `EventEnvelope` 的 `tool_epoch`、`task_epoch` 和 `context_version` 字段。

现有字段编号、类型和 RPC 不改变。A0 初始契约没有另开第二条输入链；A5/A6A 随后以 additive
field 将 `ShadowObservation` 加入既有 `CoreToMedia` oneof，使生产 bridge 可以复用同一条有界
输出流。旧 oneof 成员及字段号保持不变，旧客户端仍按未知字段规则兼容。CI 必须继续执行：

1. Python checked-in binding 可重复生成；
2. Go checked-in binding 可重复生成；
3. `buf lint`；
4. PR 相对 base 的 `buf breaking`；
5. Python/Go 对同一 golden wire vector 的 round-trip；
6. 省略 authority 字段的旧客户端仍得到 Python 权威语义。

## Shadow 强制隔离

`go_shadow` 只允许：接收已授权且最小化的数据、计算 candidate、记录延迟和 decision diff。
它禁止：

- 播放或生成音频；
- 改 generation、floor、sender、权威 Timeline；
- 执行工具；
- 写正式历史、memory、Persona 或 evidence ledger；
- 提升 owner/guest 权限；
- 让 candidate failure、超时或队列拥塞影响 Python 权威链。

涉及儿童音频的 shadow 还必须满足监护人授权、加密、短期保留、自动删除和删除审计；在
这些外部验收完成前只允许使用合成或明确授权的内部数据。

## 结果与代价

收益是迁移期间任一状态和 effect 都有唯一责任方，旧客户端保持可用，Go 可以先双算再按
状态逐项接管。代价是 mode/fence、Projection 和 candidate diff 需要额外遥测与测试，且
Python/Go 在 shadow 阶段会有受控的重复计算。这个成本低于一次性重写和线上双写竞争。

## 明确不做

- 不在 A0 新建五个网络服务；
- 不把 Python 业务逻辑整体重写成 Go；
- 不在 shadow 阶段执行任何 candidate effect；
- 不在真实 terminator、parity、SLO 和回滚演练完成前启用 Go 权威；
- 不把 transport selection、LiveKit fallback 或部署灰度误当作 interaction authority。
