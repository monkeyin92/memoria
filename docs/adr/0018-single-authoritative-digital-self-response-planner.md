---
status: accepted
date: 2026-07-22
---

# 每个话轮只允许一个权威 Digital Self 响应规划器

## Context

实时 Agent 曾分别读取 Persona Capsule、Memory Context、Companion Style、说话人规则、
恢复提示和语气计划，再在 LLM 调用前拼成多条 system prompt。每条链路单独看都合理，
但组合后会产生三个不可接受的问题：

- 同一代回答的事实、风格、关系和权限可能来自不同时间点；
- guest、uncertain、旧 generation 或已撤销版本可能通过某条旁路重新进入回答；
- 归档只能证明“说了什么”，无法证明“依据哪个冻结版本和哪些来源作答”。

Self Preview 与 Legacy 需要比普通陪伴更强的可追溯性，因此不能继续在 Agent 中维护平行
规则链或让模型自行决定哪些资料可信。

## Decision

Control API 提供唯一的 `DigitalSelfResponsePlanner`。每个已提交用户话轮只规划一次，
输入固定为：

- 服务端冻结的 interaction mode、DigitalSelfVersion、relationship 与授权上限；
- 当前话轮的 canonical query；
- 完整 `GenerationFence`；
- 不含声纹分数、embedding 或原始音频的 `SpeakerDecision` 快照。

输出为有界的 `ResponsePlan`：

- canonical instructions；
- 仅作为数据使用的 grounded items；
- `fact / inference / unknown`；
- disclosure 决策；
- voice target；
- 只含稳定 ID、版本和模型元数据的 provenance。

运行规则：

- Agent 只消费与当前完整 fence 精确匹配的计划；等待期间 fence 变化即丢弃；
- Companion 可读取 Control 当次显式提供的已确认 Memory 与 Persona；
  guest 不读取主人资料，shadow owner candidate 最多读取已确认低敏表达风格；
- Self Preview/Legacy 只读取会话绑定的不可变 manifest，不读取“当前最新投影”；
- ContextAssembler 只处理 actual-heard history、current-user isolation 与 resume 适配，
  并生成一个 canonical system block；不再叠加 Persona、Memory、Companion、non-owner、
  prosody 或 deep-reasoning prompt；
- `direct_text` 用于 privacy、unknown 和不可用等确定性回答，不再调用 LLM；
- Planner 或网络不可用时，Companion 只能基于当前话轮进行无私有上下文降级，
  Self Preview/Legacy 必须固定拒绝；
- false-interrupt 恢复只播放固定控制确认，不重新触发一条无规划 LLM generation；
- assistant actual-heard evidence 绑定同一 fence 的 response provenance；
  归档重新核验会话、版本、关系、主人来源和 manifest，不信任 Agent 提交的权限字段。

## Consequences

- Realtime Agent 不再创建或读取旧 PersonaClient、MemoryContextClient；对应 API 和模块
  可暂时保留给非实时用途或兼容迁移，但不能成为第二条回答路径。
- 每次回答可以追溯到 mode/version/relationship/source IDs、Planner policy、
  speaker snapshot 和实际 LLM/TTS 模型，同时不归档 query、prompt、来源正文、
  声纹分数、embedding 或原始音频。
- 新增回答能力必须扩展 Planner 合同与测试，不能直接在 `agent.py` 添加另一条 system
  prompt 或绕过 Planner 调用模型。
- `fact` 只能来自获准来源；DecisionCase 只能作为 inference precedent；冲突、越权和
  无来源稳定进入 unknown/privacy。
- Self Preview 与 Legacy 的身份披露、Fidelity 评测和声音选择在后续阶段复用同一计划，
  不再新增平行模拟运行时。
