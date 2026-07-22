---
status: accepted
date: 2026-07-21
---

# 低敏人格特征采用跨会话自动学习

## Context

生产 CAM++ 仍为 shadow-only，因此已登录用户的大多数话轮只能得到 `uncertain`。
旧策略允许这些话轮生成 candidate，但要求客户逐条查看和确认，结果是自然聊天长期积累了
证据却始终没有 active Persona 版本。这既不符合“在大量聊天中逐步学习”的产品预期，
也把内部模型审核工作转嫁给了客户。

同时，`uncertain` 不能被当成正式 owner。静默学习若不限制类别、会话跨度和授权，会把
旁人、回声或单次偶发表达写成人格。

## Decision

Persona 继续先生成可追溯 candidate，但客户日常流程不再逐条确认：

- 只有注册账号、有效 Persona consent 和 active session 的话轮可进入学习；guest、匿名、
  direct archive、助手/合成音频、污染和低质量证据在入口拒绝。
- 仅 `verbal_tic`、`sentence_length`、`speech_rate`、`pause_style` 和
  `discourse_style` 属于可自动发布的低敏表达风格。
- 纯 owner 证据累计 3 次后可自动确认。shadow-only 的 uncertain 文本必须累计至少
  6 次、跨至少 3 个非空独立 session，且绑定同一 `shadow_owner_candidate` profile、模型
  与模板 provenance，不能与 owner 或其他 profile 证据混合，才可自动确认并发布新
  Persona 版本。声纹轮换后，新 profile 使用独立 lane 重新累计，不被旧 profile 永久阻塞。
- `sentence_length`、`speech_rate`、`pause_style` 等互斥 bucket 只有主导证据至少达到
  次高项的 2 倍时才可自动确认；任一类别始终最多一个 confirmed，主导性消失时自动特征
  退回 candidate，人工确认与 sticky disabled 仍优先。
- uncertain 证据不采用 `speech_duration_ms` 或 `pause_ratio`，因此不能静默学习声学语速
  或停顿；`decision_habit` 与 `value_priority` 永不自动晋升。
- 被用户停用的 trait 保持 sticky disabled，后续重复观察不得复活。撤销 consent 后，
  owner 和 uncertain 的所有 Persona capsule 都必须立即为空。consent 的授权、撤销、观察
  最终写入和 capsule 读取按账户串行化，并在同一事务中复核，避免并发撤销后的脏写/脏读。
- H5 隐藏内部 candidate 和确认控件，只展示持续学习状态、已生效特征、停用、授权撤销
  与版本回滚。review API 保留为纠错、运维和兼容 seam，不是客户日常学习步骤。
- shadow-only 实时会话只可读取已 confirmed 且命中固定描述白名单的低敏风格；必须清空
  自由文本情境、反例和证据 ID，仍禁止私人记忆、旧话轮、价值/决策和工具。
- Agent 在每个已接受话轮进入 LLM 前做一次有界刷新；撤销、空 capsule 或刷新失败会先清掉
  该 `(session_id, speaker_class)` 缓存，不能把 owner 胶囊泄漏给 uncertain/guest，也不能让
  已撤销 Persona 多影响一轮。

## Considered Options

- 所有 candidate 都自动发布：拒绝。价值判断、决定习惯和自由文本可能敏感或误判。
- 只降低人工确认成本：拒绝。仍要求客户承担内部分类工作，也无法形成无感学习闭环。
- 把 shadow uncertain 直接升级为 owner：拒绝。CAM++ 尚无 anti-spoof 与真人 FAR/FRR
  生产结论，不能借 Persona 绕过 SpeakerAuthority。
- 单一会话内重复达到次数即发布：拒绝。同一话题和回声更容易制造伪稳定模式。

## Consequences

- 客户只需一次授权并正常聊天，稳定低敏表达风格会自动形成 v1 及后续版本。
- 首个版本至少需要三个会话，短期内显示“持续学习中”是正常状态，不再显示“尚未发布”
  或“待确认”。
- 高敏特征可以保留为内部候选用于未来评估，但当前不会进入生产 Prompt；这缩小了静默
  误学习的影响面。
- 自动晋升准确率、跨会话稳定性、误自动人格率、停用/纠正/回滚率需要纳入长期验收。
