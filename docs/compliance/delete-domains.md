# 删除域与 seal 契约

本文是从 `HANDOFF.md` 迁出的合规参考，保留 2026-09-20 删除范围结论和 17 表 seal 契约分析。这里记录已验证边界与已知缺口，不把 `verified_empty` 写成“已擦除”，也不把未授权的窄片实现误报为完成。

## 2026-09-20 删除范围：已闭合部分与已知缺口（结论）

- 已闭合（有收据）：可删域按 saga 9 步推进，`verified_empty` 覆盖 `_delete_order` 内且含 `account_id` 列的表；本轮补齐 guardian 的 `tutor_practice_evidence`/`tutor_commit_outbox`（删除/计数/导出 + RLS 前置 policy + 最小授权，提交 `6e853ef`；测试含真 PG 的“另一主体行不受影响”与幂等断言）。
- **已知合规缺口（明确记录；不做封存实现）**：① 明文残留——`memory_records.payload`、`memory_shared_proposals.content` 明文且不可就地改写，追加 tombstone **不构成擦除**；② 不可归属面四处——`memory_outbox`（无主体列）、`session_runtime_events`（`actor_id` 可空且无 subject 列）、`session_runtime_outbox`（无 subject 列）、`policy_receipts_v2.subject_id IS NULL`；③ append-only/零 DELETE 域（`memory_records`/`memory_status_events`/`memory_shared_votes`、`session_runtime_profiles`/`events`/`profile_receipts`、`policy_receipts_v2`）**无法物理删除**，且在不加迁移的前提下**无法形成可信封存标记**（`memory_status_events.status='revoked'` 非单调、后续合法事件可恢复可见性，故不能据此判 `verified_sealed`）。
- **表述纪律**：不得对上述域使用“封存/已擦除”表述；`verified_empty` 与删除收据只证明“可删域无残留”，**不证明“已擦除”**。若未来定义合规要求（留存期限、合规接受者、主体哈希/去标识方案），再走“各域新增独立封存登记 + 读口 join + 计数口”的最小迁移路径（届时需同时定义消费者与审计留存）。
- 未开工专项：identity（`identity_delete_guard` 依赖的 GUC 全仓从未设置、SQLite 无 delete/remaining 实现）、device_fleet/onboarding（无 person 列，须经 `identity_device_bindings.account_owner_person_id` 解析归属）。

## 2026-09-20 seal 契约持久化（17 表；供下一轮审查 D1–D10 的依据）

**逐域关键列与约束**（M=`services/memory_scope/postgres_schema.sql`、S=`services/session_runtime/postgres_schema.sql`、P=`services/policy/postgres_receipt_schema.sql`）

| 表 | 归属键 | 约束（不可删/不可改） | 现有可用的封存机制（只 INSERT 下） | 残留 |
|---|---|---|---|---|
| `memory_records` | `subject_id` M:96 / `resource_owner_id` M:97 / `created_by_actor_id` M:122；共属 `family_space_id`+`co_subject_ids` M:99-100,125-126 | 无条件 BEFORE UPDATE OR DELETE 触发器 M:288-293/309-312；仅 SELECT+INSERT M:348-349 | 只可追加 `memory_status_events`；`payload` 明文不可改写 | **明文 payload 永久保留** |
| `memory_status_events` | 经 `record_id` M:142 继承 | 无条件触发器 M:295-299/314-317；仅 SELECT+INSERT M:350-351 | 追加封存事件；**但读派生取最后一条**（PS:808-818/1719-1727）→ **非单调，可被后续合法事件覆盖** | status 判定不可靠 |
| `memory_shared_votes` | `subject_id` M:225（PK 成员 M:256） | 无条件触发器 M:302-306/319-322；仅 SELECT+INSERT M:354-355 | 不可重写；只能读层去标识 | 票面保留（且是他人晋升 fence 的授权证据 M:1414-1463） |
| `memory_shared_proposals` | `proposer_subject_id` M:156 / `family_space_id` M:155 | **可变**（有 UPDATE M:352-353，无触发器） | 全域唯一可就地去标识：`title` M:180 / `content` M:181；**不得触碰** `proposal_revision`/`capture_evidence_hash`/consent 三件套 M:192-203 | canonical evidence 必须保留以复验 |
| `memory_outbox` | **无主体列** M:259-266 | 无触发器；worker 可 UPDATE | **无法按主体定位** | 投递证据保留 |
| `memory_audit_events` | `actor_subject_id` M:274 / `subject_id` M:275 | 无触发器；仅 INSERT M:356-357 | 只能追加 | 审计原名保留 |
| `memory_shared_membership_snapshots` | `family_owner_subject_id` M:1007 + `subject_ids` JSONB M:1009 | RLS 仅 owner M:1034-1041；零运行时授权 | 追加新 revision + `status='revoked'/'expired'` M:1006；**不能按单一成员封存整行** | 历史 revision 保留（fence 复验） |
| `memory_capture_evidence` | `subject_id` M:1022 | 零运行时授权，经端口 M:1053-1208 / M:1310-1374 | **追加式封存语义现成**：追加 `status='revoked'` 的新 revision，锁端口按 `status='active'`+`revision DESC` 取行 M:1346-1355 → 旧 revision 自然失效 | 证据链保留（`canonical_hash` M:1020） |
| `session_runtime_contexts` | `actor_id` S:103 / `active_subject_id` S:107-110 | 可变（CAS 更新 S:797-800）；REVOKE ALL + 仅 SELECT S:376-401 | 用既有 `session_runtime_close_session` S:1152-1258 置终态；不可删（是 profiles/events 的 FK 目标） | 会话生命周期保留 |
| `session_runtime_profiles` | `actor_id` S:135 / `active_subject_id` S:138-141 | 无条件触发器 S:334-341/344-347；DEFERRABLE FK S:153-166 | 只能追加后继 profile 换主体；`signature` S:144 覆盖主体 id → **真去标识化不可能** | 历史 profile 仍被收据引用（S:171-172） |
| `session_runtime_profile_receipts` | `actor_id` S:175 | 无条件触发器 S:353-357 | 只能停止新增 | 与 `policy_receipts_v2` 跨域留存链 |
| `session_runtime_events` | `actor_id` S:188（**可空**）；**无 subject 列** | 无条件触发器 S:348-352；仅 SELECT | 只能追加；**历史主体归属不可证明** | 审计流保留 |
| `session_runtime_outbox` | `actor_id` S:204；**无 subject 列** | 无触发器；无 UPDATE/DELETE 授权 | 只能追加；投递端口未实装 | 投递证据保留 |
| `session_runtime_idempotency` | `actor_id` S:227 | 可变；仅 owner/maintenance | 置空结果列会破坏重放去重语义 | 去重证据保留 |
| `session_runtime_tool_effect_intents` | `actor_id` S:273 + `subject_id` S:274 | 无触发器；`status` 可变 S:298-300 | 用 reconcile 端口转终态 S:1856-1895；就地清 payload 会破坏 fence S:295-296 | 执行证据保留 |
| `session_runtime_tool_effect_outbox` | `actor_id` S:313 + **`subject_id` S:314** | 无触发器；claim/complete 端口 S:1897-1940 / 1942-2038 | **唯一可按 subject 在投递前干净过滤的面**（须把封存检查内建进 claim） | 投递证据保留 |
| `policy_receipts_v2` | `actor_id` P:127；`subject_id` 可空 P:128-130；`resource_owner_id` P:131-134；共享身份在 `action_resource_fence` JSONB P:168-201 | **零 UPDATE/DELETE/TRUNCATE**（REVOKE ALL 后仅 SELECT,INSERT P:357-369；文件头 P:15-16 声明 immutable audit record） | 只能停止签发 + 读层过滤；无 sealed 列 | 收据永久保留（不变量级审计记录） |

**封存后必须过滤的读口（谁必须改）**：memory → `services/memory_scope/postgres_store.py:696-806`（注意 `include_revoked=True` 是审计开关，须先于它生效）、RLS `M:369-398`（不含封存维度，owner-grant 分支 M:388-398 会照常放行）、`services/memory_scope/service.py:764-941`、operator 读口 `services/governance/subject_postgres_reads.py:249-300`（**必须改**）、SQLite 侧 `services/memory_scope/migrations/legacy_archive.py:2950-2980`；session → `services/session_runtime/postgres_store.py:875-1030,1252-1306,1399-1435`、RLS `S:443-466`（**按 binding 判定、无主体维度**）、`S:1897-1940`（投递前门）、`S:542-605/821-848`；policy → `services/policy/postgres_receipt_repository.py:222-278,401-435`、`P:257-294`（失败关闭，封存后本人也读不回）、全局读者 `P:436-463` 与 `S:2500-2514`。

**残余泄漏面（需显式接受，不得当作全链路不可见）**：① 产品召回走 archive 目录而非 `memory_records`（`services/control_api/app/routes/interaction.py:1719-1743`）⇒ 封存 memory_scope 不影响轮次内容；operator/导出面（`subject_postgres_reads.py`、governance `build_subject_export`）与 Redis 派发（`redis_outbox.py:83-112`，at-least-once）、搜索/向量物化各自需消费封存事件；③ D4=A 明确放弃 `session_runtime_events`/`outbox` 封存；④ D6=A 保留原始审计（只收窄可读者）。

**不可归属面（必须显式报 `unattributable`，不得计 0）**：`memory_outbox`、`session_runtime_events`、`session_runtime_outbox`、`policy_receipts_v2.subject_id IS NULL`。

**现有约束（不改授权/不迁移/append-only）下的可交付边界**：三域**统一去标识不可实现**；仅两处窄片可行——`memory_capture_evidence`（追加 revoked revision，锁端口语义现成）与 `session_runtime_tool_effect_outbox`（按 `subject_id` 在 claim 前过滤）；其余必须保持**未闭合**。用户已决策：**不做封存实现，记为已知缺口**（不授权上述窄片）。
