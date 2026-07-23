---
status: accepted
date: 2026-07-23
---

# 传承模式使用授权快照、冻结核心和每位受赠人的独立关系外壳

## Context

现有 `voice_sessions.user_id` 同时被当作当前登录者、Digital Self 主人和声纹判断主体。
Self Preview 中三者都是本人，因此没有暴露问题；在 Legacy 中，当前登录者和声纹主体是
grantee，Digital Self 主人却是 owner。继续复用单一 `account_id` 会导致两类越权：把
grantee 错当 owner，或为了读 owner manifest 而直接放宽 PostgreSQL RLS。

RelationshipProfile 描述主人面对某个人的称呼、语气与边界，但不是访问凭证；
SpeakerDecision 只描述当前会话声音判断，也不能携带 owner 数据 scope。Legacy 对话产生的
新内容属于 grantee 与数字分身激活后的关系，不是主人生前知道的事实。

## Decision

- 所有 Legacy 控制路径显式区分：
  `actor_account_id`（当前登录者）、`resource_owner_account_id`（冻结 Digital Self 主人）和
  `speaker_subject_account_id`（声纹分类相对的账户）。grantee 的 `speaker=owner` 只表示
  匹配其本人声纹，不授予 Digital Self owner 权限。
- `LegacyGrant` 只绑定一个 registered grantee、一个 frozen `DigitalSelfVersion` 及其
  manifest digest、一个 approved RelationshipProfile 及版本、manifest 内 exact item
  allowlist、声音权限、固定到期时间和激活/撤销状态。未知、private 或不在 manifest 中的
  scope fail closed；新版本不会自动替换或扩大授权。
- owner 可在激活前做在世预演，但不能写关系外壳；grantee 只有在 grant 已激活、未过期、
  未撤销时才能进入。首版不实现死亡检测、遗嘱认证或法律执行。
- 每个 grant 只有一个 `LegacyRelationshipShell`。shell 只保存 activation 后 actual-heard 的
  grantee/digital-self 对话和少量 allowlisted 互动偏好；不得修改 grant、manifest、关系
  引用或 owner core，也不得进入 owner Evidence、Memory、Persona、Growth 或普通历史。
- `LegacyAccessResolver` 是唯一授权入口。它先在 actor scope 验证 exact grant，再由服务端
  解析 owner、version、relationship、scope 和 shell；客户端不能提交 owner ID、manifest、
  relationship version 或授权 scope。
- PostgreSQL 现有 Digital Self 与 Self Model RLS 不放宽。Legacy 表使用 FORCE RLS，
  owner/grantee 只能看到与自己相关的 grant；服务端 resolver 使用解析出的 owner scope读取
  exact core。跨 grantee 统一表现为 not found。
- Legacy 回答仍走唯一 `DigitalSelfResponsePlanner`。每一 generation 重验 grant；
  `speaker_decision` 不再承担 scope。所有回答和 H5 全程披露“基于冻结资料生成的数字分身，
  不是本人”；不能声称“我就是你的父亲/朋友”。
- `voice_allowed` 只控制能否使用 owner approved personal voice。关闭时使用会话冻结的、
  明显非本人的伙伴设计音色；开启时仍需满足 ADR-0019 的 exact version、expiry、撤销和
  generation fence，失败只回退设计音色。
- Legacy 审计只保留 actor/owner/grantee/grant/shell/session/fence/target 等 ID、动作、决定、
  原因和时间，不保存 query、prompt、source excerpt、response text 或自由 payload。

## Consequences

- `voice_sessions.user_id` 继续代表 authenticated actor，并新增 resource owner、actor role、
  grant/scope/shell 快照字段；旧会话回填 `resource_owner_account_id=user_id`。
- owner 删除和 grantee 删除必须走不同传播方向：两者都清理相关 grant/shell/session，
  grantee 删除绝不能删除 owner core。
- manifest v1/v2/v3 canonical bytes 不变；S9 只消费 exact frozen manifest，不新增 v4。
- v1/v2/v3 没有统一的可分享范围字段，因此首版不推断“可分享”：Memory 仅允许明确
  `family/public` 的 `sensitive_domain`，Persona 因缺少 scope 暂不授权，
  Cognitive/Decision/Relationship 仅允许明确 `family/public` 的 `sharing_scope`。
  private、unknown 和缺字段均拒绝。后续若引入 manifest v4 或本人逐项分享审核，应保持旧
  digest 兼容，并要求重新签发 grant，不能自动扩大既有授权。
- 首版只支持一个 owner、一个 grantee、一个 grant/version/relationship/shell 的闭环；
  多执行人、争议冻结、死亡认证和法律流程保留后续阶段。

## References

- [`0016-separate-companion-style-from-digital-self.md`](0016-separate-companion-style-from-digital-self.md)
- [`0019-version-bound-personal-voice-runtime.md`](0019-version-bound-personal-voice-runtime.md)
- [`../silicon-life-implementation-plan.md`](../silicon-life-implementation-plan.md)
