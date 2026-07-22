---
status: accepted
date: 2026-07-22
---

# 认知、决策与关系画像必须由可追溯证据和本人审核派生

## Context

现有 Persona 的 `decision_habit`、`value_priority` 以及 Archive 的 relationship 事实，
可以帮助发现候选内容，但它们分别是轻量表达标签和人物事实，不能直接回答“主人会怎样
判断”或“主人面对某个人会怎样说话”。如果把这些旧投影直接注入 Digital Self，会把
模型推断、引导性问题和人物关系误升级为本人的稳定价值、决策规则或访问授权。

## Decision

新增三个相互独立但共享证据门禁的一等领域模型：

- `CognitiveClaim` 表示 belief、preference、value、decision rule、red line、
  uncertainty、conflict 和 support 等可审核主张；
- `DecisionCase` 保存具体情境、候选方案、约束、选择、舍弃、结果和事后反思，并区分
  真实经历与假设情境；
- `RelationshipProfile` 只描述主人面对已存在人物/关系时的称呼、语气、建议方式、
  分享偏好上限和禁区；它不创建人物事实，也不授予访问权限。

共同规则：

- candidate 和 effective 分离；effective 是根据当前状态、来源、负面证据和审核记录
  查询时派生的结果，不保存可漂移的第二份布尔真相；
- 每项必须引用 owner 权威来源；guest、assistant、模拟模式和陪伴者输出不能升级；
- support 与 counterexample 分别保存；value、decision rule、red line、conflict 和
  support 等高敏内容必须经密码 step-up 的本人审核，并保留至少一个本人反例来源；
- 假设情境只能帮助发现候选，不能单独成为生效 DecisionCase；
- 已批准 RelationshipProfile 是不可变版本；修改会生成下一版，旧版只能
  supersede/revoke，不能原地改写；
- `sharing_scope` 只是偏好上限，最终可见范围始终取
  `LegacyGrant ∩ RelationshipProfile ∩ SafetyPolicy`；
- 旧 Persona 决策/价值标签和原始 relationship 只作为 legacy candidate，不进入
  adopted/effective 状态或 DigitalSelf manifest；
- 继续复用同一条已记录 Evidence、账户写入门禁和幂等命令；结构化成长任务在请求重试时
  收敛为候选，不新增平行 worker 或第二套事实账本。

## Consequences

- DigitalSelf manifest 需要向后兼容旧 v1 字节，并在新版本中增加 typed cognitive、
  decision 和 relationship entries。
- Growth Map 只有在新模型真正 effective 后，才把相应维度标记为已采用。
- H5 必须让本人查看来源、反例和适用情境；高敏确认与关系批准需要 step-up。
- Self Preview 与 Legacy 只能消费指定 DigitalSelfVersion 中已冻结的条目，不能查询
  “当前最新候选”代替版本内容。
- 账户导出、删除传播、PostgreSQL FORCE RLS 和生命周期审计必须覆盖所有新表。
