# Coordination Harness Architecture

**状态**: 草稿  
**版本**: 0.1  
**作者**: DuplexAgent-RSI 设计代理  
**日期**: 2026-10-26

## 概要

这篇文档设计一套清晰的协调契约,将 Memoria 现有分布式协调逻辑规范为六个领域模块,对齐到 DuplexAgent-RSI 的 Policy / Intake / Hold / Worker / Deliver / Memory 框架。目标是明确失败归属、简化协调追踪,并为后续实现优化提供类型安全的基础。

## 当前状况

Memoria 的协调逻辑分布在以下模块:

- **utterance_router.py**: `UtteranceIntent` 枚举 + 路由规则(enroll / interrupt / stop / chat 优先级表)
- **interruption_guard.py**: 打断词表 / 回溯判定 / 垫话白名单 / 短语 normalize
- **delegation_coordinator.py**: `DelegationRequest` + 后台工具调度 + `GenerationFence` 栅栏管理 + `OutputIntent` admission
- **task_manager.py**: 异步 `ToolTask` 管理 / 取消 / 超时 / 副作用授权
- **media_session_input.py**: VAD/ASR 包级联输入管理 + 语音段入口门禁
- **media_session_commit.py**: 输出决策 + 发送门禁 + 样本区间 commit 逻辑

这些文件已实现具体逻辑,但领域边界模糊、失败归因分散。本设计提取协调契约,划分明确的职责边界。

## 六模块设计

### 1. Policy (协调策略)

**职责**: 对齐到 `UtteranceRouter` + `InterruptionGuard` 的路由与打断判定。

- 决定一条语音的**控制意图**(聊天 / 打断 / 停止 / 回放重启 / 离场)
- 打断语义判定(「等一下我想问」→ `INTERRUPT_THEN_CHAT`)
- 回溯垫话白名单(「嗯你说」)

**已做对的部分**:
- `UtteranceRoute` 数据结构清晰分离了 `intent` / `should_interrupt` / `enter_chat` / `speaker_gate_override` / `ack_phrase`
- 路由规则按优先级编号(`speaker PENDING` → `resume` → `conversation_close` → `interrupt_command` → ...)
- `InterruptionGuard.evaluate()` 返回 `InterruptDecision` 枚举

**需补齐的协议**:
- 明确失败分类(`PolicyFailure` 枚举:路由失败 vs 打断失败)
- 语音期望生命周期(`PolicyExpectation`:等待 ASR 最终 vs 立即判定)

### 2. Intake (入口门禁)

**职责**: 对齐 `media_session_input.py` 的 VAD/ASR/KWS 包级联入口与资格门禁。

- 接收 `AudioFrame` + `SpeechSegment`
- 判定一个 VAD/ASR 包是否被当前 session 接纳(`spurious_connect_vad` / `vad_precedes_pending_turn` / 听话握持期拒绝)
- 维护 `pending_turn_onset_floor`:边界之前的内容一律拒绝,避免跨边界修订污染新轮次

**已做对的部分**:
- `_admit_vad_start()` / `_asr_precedes_pending_turn()` 是清晰的门禁函数
- `listening_capture_hold` / `empty_turn_endpoint_pin` 等精细的状态标记已设计完成
- `reopen_evidence_window` 逻辑已精确处理背景声多次 reopen

**需补齐的协议**:
- 明确入口门禁失败的家族分类(`IntakeRejectReason` 枚举:stale / spurious / overlap / floor-mismatch)
- 定义 VAD/ASR/KWS 的统一包协议(`SpeechPacket` protocol)

### 3. Hold (挂起协调)

**职责**: 对齐到 `media_session_commit.py` 中"等待决策"的逻辑部分。

- 决定一个已入口的语音应该**立即 commit** 还是**等待更多证据**
- 时间管理:endpoint 跳过 grace、reopen 证据窗口、ASR tail 超时
- 空轮次判定:有 VAD 但没 ASR 文本 → 是 commit 空轮还是继续等待?

**已做对的部分**:
- `turn_endpoint_sample` / `turn_retire_sample` 双水印设计精细
- `_reopen_evidence_window()` 逻辑精确控制 hold 时长
- `_empty_turn_endpoint_pin` 机制避免假噪声占位

**需补齐的协议**:
- `HoldDecision` 枚举(commit / wait / discard)
- `HoldTimeout` 协议(grace / tail / evidence-window)

### 4. Worker (后台工作调度)

**职责**: 对齐到 `DelegationCoordinator` + `TaskManager` 的后台工具与深度结果协调。

- 异步 `ToolTask` 启动 / 取消 / 超时
- `GenerationFence` + `task_epoch` + `context_version` 多层栅栏隔离
- `OutputIntent` 生成与 admission 门禁
- 副作用授权(`EffectAuthorization`)与幂等性(`idempotency_key`)

**已做对的部分**:
- `DelegationRequest` / `DelegationEvent` 类型清晰
- `TaskManager` 已有完整的 `side_effect_policy` / `ActionPolicyPort` / 事务提交口
- `output_intent()` / `admit_output_intent()` 已实现栅栏检查与 shadow state 管理

**需补齐的协议**:
- `WorkerFailure` 分类(fence-stale / task-timeout / policy-denied / uncommitted)
- `OutputIntent` 状态机(candidate / queued / selected / consumed / superseded)

### 5. Deliver (发送决策)

**职责**: 对齐到 `media_session_commit.py` 中"输出是否允许发送"的逻辑。

- 判定 `OutputIntent` 是否可以发送(栅栏 / floor / 过期检查)
- 决定对话回放打断策略(duck / cancel / continue)
- TTS 音频帧门禁

**已做对的部分**:
- `admit_output_intent()` 已实现完整的栅栏检查 + floor 门禁
- `_shadow_output_by_session` 实现了优先级排序与域内裁剪(`_MAX_SHADOW_OUTPUT_CANDIDATES_PER_DOMAIN`)
- `OutputIntentAdmission` 记录观测追踪

**需补齐的协议**:
- `DeliverDecision` 枚举(send / queue / supersede / drop)
- `FloorGate` 协议(assistant_may_speak / blocked)

### 6. Memory (会话记忆)

**职责**: 对齐到 `ConversationProjection` 的轮次 Commit 与历史管理。

- Commit 一条用户轮次到会话历史
- Projection 的 validate / align / commit 三段门禁
- `TurnPhase` 管理(unverified / provisional / acoustic_only / text_verified)

**已做对的部分**:
- `CommitEvidence` / `CommittedTurn` / `ProjectionPatch` 清晰的数据结构
- `align_provisional_range()` / `align_provisional_text()` 处理 timeline 对齐
- `ProjectionRejectReason` 枚举列出所有拒绝原因

**需补齐的协议**:
- `MemoryFailure` 分类(text-mismatch / range-mismatch / speaker-mismatch / uncommitted)
- `ProjectionState` 协议(provisional / aligned / committed)

## 失败家族分类法

| 模块 | 失败家族 | 示例原因 | 责任边界 |
|------|----------|---------|----------|
| Policy | `PolicyFailure` | `invalid_intent` / `route_ambiguous` / `interrupt_weak_evidence` | 路由表自身定义不清 |
| Intake | `IntakeRejectReason` | `stale_stream_epoch` / `spurious_vad` / `floor_conflict` / `listening_hold` | 包自身不符合当前窗口 |
| Hold | `HoldTimeout` | `grace_expired` / `tail_timeout` / `evidence_window_expired` | 决策超时(非包自身问题) |
| Worker | `WorkerFailure` | `fence_stale` / `task_timeout` / `policy_denied` / `effect_uncommitted` | 后台工具执行失败 |
| Deliver | `DeliverRejectReason` | `floor_blocked` / `fence_mismatch` / `expired` / `superseded` | 输出不满足发送条件 |
| Memory | `MemoryFailure` | `text_mismatch` / `speaker_mismatch` / `projection_rejected` | 历史 commit 条件不满足 |

## 模块间接口协议

```
          ┌───────────┐
          │  Policy   │ (路由意图 + 打断判定)
          └─────┬─────┘
                │ UtteranceRoute
                ↓
          ┌───────────┐
          │  Intake   │ (VAD/ASR 包门禁)
          └─────┬─────┘
                │ AdmittedSpeech
                ↓
          ┌───────────┐
          │   Hold    │ (等待决策)
          └─────┬─────┘
                │ HoldDecision
           ┌────┴────┐
           ↓         ↓
     ┌─────────┐  ┌─────────┐
     │ Worker  │  │ Deliver │
     └────┬────┘  └────┬────┘
          │            │
          └─────┬──────┘
                ↓
          ┌───────────┐
          │  Memory   │ (Commit 历史)
          └───────────┘
```

## 实施路径

1. **第一步**: 定义契约文件 `services/agent/src/orchestration/coordination_contracts.py`
   - 所有 Protocol / dataclass / StrEnum
   - 所有接口函数签名(body 为 `raise NotImplementedError`)

2. **第二步**: 将现有文件按模块重新组织(不改行为,只改边界)
   - `policy_module.py`: 从 `utterance_router.py` + `interruption_guard.py` 提取
   - `intake_module.py`: 从 `media_session_input.py` 提取门禁函数
   - `hold_module.py`: 从 `media_session_commit.py` 提取决策等待逻辑
   - `worker_module.py`: `delegation_coordinator.py` + `task_manager.py` 重构
   - `deliver_module.py`: 从 `media_session_commit.py` 提取输出门禁
   - `memory_module.py`: `conversation_projection.py` 适配器

3. **第三步**: 为每个模块补充失败归因追踪
   - 所有 `RejectReason` / `Failure` 枚举记录到 Metrics
   - 关键决策点记录到结构化日志

4. **第四步**: 建立端到端验证测试
   - 每个模块的单元测试覆盖其协议
   - 集成测试覆盖多模块协作场景

## 与 DuplexAgent-RSI 的对齐

| DuplexAgent-RSI | Memoria 模块 | 主要职责对齐 |
|-----------------|--------------|-------------|
| Policy | Policy | 路由规则 + 打断判定 |
| Intake | Intake | VAD/ASR 包门禁 |
| Hold | Hold | 等待决策 + 超时管理 |
| Worker | Worker | 后台工具 + OutputIntent |
| Deliver | Deliver | 输出门禁 + TTS 发送 |
| Memory | Memory | Projection Commit |

## 附录:术语对照表

- **Fence**: 多层版本号栅栏(`session_epoch` / `turn_id` / `generation_id` / `tool_epoch`)
- **Task Epoch**: 单工具的单调递增版本号
- **Context Version**: delegation 会话上下文版本
- **Shadow Output**: 尚未被选中但保留候选的 `OutputIntent`
- **Reopen Evidence Window**: 在已有文本的 endpoint 之后 VAD 重开,要求新 speech 在时间窗内产生文本,否则回退到旧 endpoint
- **Empty Turn Endpoint Pin**: VAD 边缘无文本但已占位,防止后续 final 抢占
