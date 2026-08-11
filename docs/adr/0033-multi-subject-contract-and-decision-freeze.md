---
status: accepted
date: 2026-08-09
---

# 多主体合同与产品/架构决策冻结（D-01~D-08）

## 背景

`Memoria_多用户场景产品策略与架构开发调整方案_2026-08-09.md` 第 0/1/5/7/8/9 章定义了多年龄段的统一平台模型：首次绑定是产品一级分流入口，但运行时必须按当前说话人、年龄证据、关系、设备状态和风险状态动态决策。此前仓库中同名枚举（`service_mode`、`subject_category`、`speaker_state` 等）散落在 Agent、Control API、Guardian、Archive 与客户端，存在同名不同值的漂移风险，且缺少统一的 fail-closed 语义。

## 决策

### 1. 冻结产品与架构决策 D-01~D-08

以下决策进入产品需求基线与验收标准，不再由页面、Prompt 或客户端自行解释：

- **D-01**：Memoria 是多年龄段平台（儿童/成人/适老/家庭），不是儿童专属产品。
- **D-02**：首次绑定对象是产品一级分流入口（给孩子/给自己/给父母/家庭共同使用）。
- **D-03**：绑定时声明用途 ≠ 运行时永远由同一人使用；必须同时维护 `device_declared_mode`、`current_session_mode`、`active_subject_id`、`speaker_confidence`。
- **D-04**：Persona（怎么说）、Service Mode（能做什么）、Policy Engine（此刻是否允许及附带义务）必须解耦；Persona 不承载任何权限。
- **D-05**：保留人格沉浸，身份透明采用低干扰、事件触发、分龄表达，不在每轮机械自报，不冒充现实自然人身份。
- **D-06**：付款人、设备管理员、主要使用者、数据主体、监护人、代理人、紧急联系人、受益人、当前说话人必须在领域模型中分离。
- **D-07**：设备用途只是默认值；声音复刻、数字自我、传承、支付、原始音频、训练语料、私人记忆等敏感能力必须依据当前主体再次决策。
- **D-08**：平台可以多场景，商业传播必须单点清晰（分场景 SKU 与广告）。

### 2. 单一 canonical 枚举合同与确定性 codegen

- `packages/contracts/schemas/multi-subject/multi-subject.schema.json` 是
  `DeviceDeclaredMode`、`ServiceMode`、`SubjectCategory`、`AgeBand`、
  `AgeEvidenceStatus`、`BindingRole`、`RelationshipStatus`、`SpeakerState`、
  `MemoryScope`、`PolicyEffect`、`PolicyObligation`、`Capability` 十二个枚举的
  唯一权威来源。
- Python、TypeScript、Go 枚举/常量与固件紧凑 JSON 合同由
  `scripts/generate_multi_subject_contracts.py` 确定性生成（无时间戳、按
  schema 文档序输出），生成物提交仓库；CI 用 `--check` 保证重新生成无
  diff。固件只获得紧凑 JSON 合同（离线降级与能力清单用），不虚构可编译固件
  目录。
- 值集与默认值以整改文档为准并做必要收敛：`DeviceDeclaredMode` 统一为
  `parent_for_child / self_use / child_for_parent / family_shared`（文档
  §2.3 示例 `child_primary` 不采用）；`BindingRole` 严格采用 §8.3 CHECK
  集合，`beneficiary` 走 Relationship（`beneficiary_of`）/Legacy 域，
  `active_speaker` 走会话级 `SpeakerState`/`active_subject`；`MemoryScope`
  增加 `unknown` 兜底，归属不明的历史记忆进入 `quarantine`
  （`memory_item.status`）而非任何 scope。

### 3. unknown_safe 与 fail-closed 语义

- `SubjectCategory`、`AgeBand` 默认 `unknown`；`AgeEvidenceStatus` 默认
  `unverified`；`SpeakerState` 默认 `unknown`；`MemoryScope` 默认
  `unknown`；`PolicyEffect` 默认 `deny`；无法确认说话人/主体时
  `ServiceMode` 必须解析为 `unknown_safe`。
- 任何 `unknown/unconfirmed/unverified/deny` 上下文不得获得成人专属、私人
  记忆、声音复刻、数字自我、传承、支付或原始音频能力；禁止把历史 `adult`
  默认值当作已验证成年人（§8.8 迁移原则）。
- 语义义务（`PolicyObligation`，含 `AI_IDENTITY_CLARIFICATION`、
  `REALITY_REMINDER`）由 Policy Engine 产出、Persona Renderer 按年龄与
  人格表达，模型不得自行判断是否提醒。

### 4. 模块化单体与迁移顺序

- 继续采用模块化单体：先建立统一决策内核（SubjectResolver、
  ServiceModeResolver、PolicyEngine、PersonaRenderer、MemoryScopeResolver、
  SessionEpochFence、PolicyReceiptWriter），再按领域拆 `services/control_api`
  与 `services/agent`，不立即引入网络边界。
- 迁移顺序固定为阶段 A~F（PR-01~PR-18，见
  `docs/architecture/multi-subject-pr-plan.md`）：先冻结语义与合同，再绑定与
  关系，再会话主体解析与 Runtime Profile，再 Persona/策略引擎，再记忆/学
  习/家庭共享，最后通知/设备/生产治理。
- 核心账号、主体、关系、绑定、会话、同意、策略回执与通知以 PostgreSQL 为
  权威，RLS 按家庭/主体隔离；SQLite 仅作本地开发 fixture。

### 5. PR-01 对象合同（整改文档 §9.6，objects_version 1）

在既有 12 个 canonical 枚举之外，`multi-subject.schema.json` 新增 `objects`
段（独立 `objects_version` 递增，与枚举 `schema_version` 解耦），定义并
确定性生成 14 个顶层对象合同：

- `BindingManifest`（§2.3/§8.2-8.4，对齐 `services/identity` 的
  `BindingManifest.to_dict`，status/reason/roles/persona/service/policy 全部
  快照）；
- `RuntimeProfile`（§7.3/§9.3，字段与 Control
  `runtime_profile_wire_payload` 完全一致，`signature_schema` 为
  `runtime-profile-v1` const）与 `RuntimeProfileSigned`（
  `x-memoria-extends` 继承模型 + `signature` 信封；签名对除 `signature` 外的
  完整载荷重建 canonical bytes 校验，缺失/非 hex64/不匹配一律 fail closed）；
- `PolicyDecision` 与 `PolicyReceipt`（§8.6 表 + `services/policy/receipts.py`
  栅栏字段：actor/subject/device/binding/session/epoch/profile/context_hash）；
- `SubjectResolution`（§9.2，与 Control resolve-subject 响应形状一致）；
- `MemoryWriteFence` 与 `MemoryEvent`（§6.2/§8.7/§11.5，指纹绑定回执）；
- `NotificationFence` / `RelationshipSnapshot` / `RecipientSnapshot` 与
  `NotificationIntent`（§6.5/§10.6，幂等键 + policy_receipt_id 必填 +
  收件人/关系快照）；
- `SessionEpochFence` 与 `SessionEvent`（§11.5 事件信封：session_id/epoch/
  generation_id/turn_id/event_sequence/active_subject_id/runtime_profile_id）。

关键约束（生成器强制防漂移，改动必须同步 ADR 与测试固定期望）：

- `binding_version` 全合同统一为 `integer >= 1`；string/0/null 一律 fail
  closed。`RelationshipSnapshot` 因关系可独立于设备绑定存在，整体省略该字段。
- `session_epoch`/`epoch`（RuntimeProfile、PolicyReceipt、MemoryWriteFence、
  NotificationFence、MemoryEvent、SessionEpochFence）为 `integer >= 1`
  （Control 首次签发即为 1，换主体递增）；`SessionEvent.session_epoch` 保留
  0 仅表达签发前 lifecycle（断线/降级/失败），此类事件不得授权任何敏感行为。
- 所有对象 `additionalProperties`/`unevaluatedProperties` 闭合、required 显
  式、未知枚举拒绝；`RuntimeProfileSigned` 因组合扩展使用
  `unevaluatedProperties: false`，其字段必须是基座严格超集（生成器校验）。
- 生成产物带 schema sha256（`source_hash`），连续生成 byte-identical，
  CI `--check` 防漂移；Python 为 Pydantic v2（`extra="forbid"` +
  `frozen=True`，数值 strict 且 `allow_inf_nan=False`，uniqueItems 用
  field_validator 强制），TypeScript 为可擦除语法类型 + `isX`/`validateX`
  运行时约束（仓库无 zod 依赖，不引入假依赖），Go 为 struct + `Validate()`
  seam（pattern/format 由消费方解析时校验，固定长度 pattern 生成长度检查），
  固件为紧凑 JSON（含对象字段元数据）。

### 6. 待服务端迁移点（本轮合同先行，服务收敛在 PR-02~PR-10）

- `services/memory_scope` `WriteFence.binding_version` 现为
  `str | None`，须迁移为 canonical `int >= 1` 并同步指纹输入。
- `services/notification` `NotificationFence`/`NotificationReceiptRecord`/
  `RelationshipSnapshot.binding_version` 现为 `str | None`，须迁移为
  `int >= 1`（RelationshipSnapshot 按合同省略该字段，改用
  relationship 自身快照）。
- `services/policy/receipts.py` `PolicyReceipt` 缺少 `purpose`（§8.6 列），
  合同已按可选 nullable 声明，引擎后续补齐。
- `apps/miniprogram/utils/multi-subject-contracts.js` 是待迁移消费者：它按
  schema 冻结值手工镜像枚举，自有漂移测试兜底，但**不是第二权威**；后续 PR
  应改消费生成的 TypeScript 产物（含对象合同）后删除该镜像。

## 结果与代价

- 前后端、Agent、Go Gateway 与固件不再各自定义同名不同值枚举；任何取值变
  更必须先改 canonical schema、递增 `schema_version` 并同步更新本 ADR 与
  `scripts/tests/test_generate_multi_subject_contracts.py` 的固定期望。
- 现有代码中的本地枚举（如 `services/control_api` 的 `subject_category`
  常量、`services/agent` 的 service mode 字面量）在 PR-02~PR-10 中逐步收敛
  到生成物，本 ADR 不要求一次性全量替换。
- 真机（AEC、语料、物理隐私灯、多成员连续会话）、法务/PIA、专业危机评审、
  真实通知渠道、生产异地恢复等门禁不属于软件可完成范围，由
  `multi-subject-pr-plan.md` 明确标注，不得在软件验收中冒充完成。
- 本 ADR 只冻结决策与合同；`architecture-status.yaml`、`HANDOFF.md` 与整改
  主文档保持不动。
