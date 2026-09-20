# Memoria 当前交接

更新于 2026-09-20（主体隔离批次与四个 account→subject 迁移，`aac99d3` 之后本轮提交）。这里只保留当前运行基线、一个紧邻回滚、必要运维步骤和下一验收。唯一执行队列及已评估研究结论见 `TODOLIST.md`，后续完成项直接移出队列，不新增归档文档。

本轮在 `f2a95d6` 之上完成 advisory 整改：Doubao 真重入回归（`slow` 持续首包失败，旧 `slow_once` 收据作废）、CosyVoice 降级取消优先、P2-05 分子/分母/report wall 全口径门后起算、P1-05 回顾可追溯字段并撤回跨主体收据，另收尾 P2-05 严格自包含去重（`768993b`）与收据 docs（`b668960`/`87262d3`）；未部署、未连接生产或设备。远端 CI `35298356748`（`768993b`）success：python 全量 5109 passed/2 skipped、覆盖率 88.11%、wake 12 passed、Offline E2E PASS（provider smoke 仍 `OFFLINE_MOCK=true`）。冻结候选 `memoria-agent:b668960` 仅本地构建验收（source `b668960` docs-only 等同 `768993b`，image `sha256:927d9f473fe44f95e52c617912f26e1fbfccf83f8afd4baf776bec8ca7fba081`，`arm64/linux`，构建期 gate + `65532:65532` 运行用户复验均 PASSED，活体 LiveKit agents/openai/silero 1.8.1 + RTC 1.1.18/API 1.2.1）——未启用（enabled 仍是 `d96d4c2`，生产切流另需授权）。此前 exporter、person-consent、Runtime 与 Agent cache 修复保留下方带日期/提交的收据，不能概括为“软件全闭、只剩设备”。下列生产/板卡状态仍是既有观察，不是本轮实时健康证明；操作前须重新核验。

本轮提交（`1e6d730` 之后两次）：先提交主体隔离批次 + 四个 account→subject SQLite 迁移接缝（`00dc059`），再补上四迁移的按 subject 只读出口、persona 会话胶囊的主体读路径与 `account_deletions` 的 operator 回执出口。已完成 code 与本地/权威 PostgreSQL 回归，但**未 enabled、未部署、未接入设备 live 链**，也未连接生产或设备。它覆盖 P0-04/P1-05/P2-03 的 `EvidenceEvent.subject_id`、主体读出口、按主体 retention、subject-scoped partial export、四迁移接缝与回执可查询；本记录不能据此宣称发布、真机验收或真实机器人对话。
本轮主体批次验证收据：`303 passed, 1 skipped`；PostgreSQL raw-voice contract、Ruff、`mypy services --strict`、module budget、`git diff --check`、authoritative PostgreSQL init/repeat-upgrade/contract gate 均通过。临时 PG 仅用于本轮隔离回归，生产/既有 `memoria-pgv` 未触碰。
提交前门禁复核另修三处：`_schedule_persona_observation` 只接受账号本人主体（不同主体的已授权话轮不再教会账号人格）；growth 控制面回执（学习任务/决策回顾/反馈）落认证账号的 `subject_id`，否则会从主体读出口与导出里消失；9 个旧用例改为显式声明主体或已验证成人账户，导出用例改为断言这些回执仍在本人导出内。affected 域（control_api/archive/governance/identity/persona/digital_self/memory_scope + tests）`1640 passed, 1 skipped`，Ruff/module budget/strict mypy（447 files）通过。
本轮读路径与回执收据（code，本地；无生产/设备访问）：
- 四迁移各新增按 subject 的只读出口：`durable_subject.read_subject`（in-place 证据行 + 收据）、`digital_self/persona.read_subject`（投影行，persona 另有 `read_active_version`）、`memory_scope.read_subject`（`memory_records`/`memory_status_events`，scope=legacy_archive）。全部只读、不建 schema，缺库/缺表/无投影返回空而不是回落账号键。经 `services/governance/subject_migrations.read` 与 CLI `run_subject_migrations.py read --migration … --subject …` 暴露，`list/plan/status/read` 均只读。
- persona 投影读放在产品侧 `services/persona/subject_projection.py`（表名映射由迁移模块导入，两侧不会漂移），Control API 仍不导入任何迁移接缝（守卫用例继续通过）。会话胶囊按会话解析出的当前主体取人格：主体不是账号本人时只读该主体的投影，无投影或权威不可用时返回空胶囊，绝不回落账号人格；账号本人路径不变。
- 胶囊渲染抽成共享纯函数 `persona_capsule_from_snapshot`，SQLite/PostgreSQL 两个引擎与新读路径共用同一算法。
- `account_deletions` 回执新增 operator 出口 `GET /v1/archive/deletion-receipts[/{request_id}]`（archive 内部 token，注销会话终止后仍可查），返回 status/step/progress/deleted_counts/last_error 与短审计哈希 `account_ref`，不返回账号 id；本人 GET 语义不变。
- 仍未做：删除范围验证（MinIO/provider/音色声纹）、读路径的 PG 侧对等与小程序读口、其余 owner 控制面写入者（skills/self_model/self_preview）的 subject 归因。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-18
reviewed_source_commit: subject_scope_batch_00dc059_plus_read_paths_this_round
current_worktree: clean_after_migration_read_paths_and_receipt_round
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
current_work_order: vocat_interrupt_assist
code: committed_through_subject_scope_batch_and_migration_read_paths
wired: subject_scoped_read_exits_plus_migration_read_paths_and_persona_capsule_subject_read
enabled: false_for_current_head
verified: local_and_authoritative_postgres_regression_for_subject_scope_migrations_read_paths_and_receipts
guardian_declaration_scope: binding_scoped_owner_only_third_party_excluded
accountless_person_consent: person_scoped_grant_read_revoke_replay_unbind_revoke_and_export_verified_device_pending
production_readiness: ready_at_last_observation_not_refreshed_this_review
production_readiness_observed_at: 2026-09-16T11:37:57+08:00
student_safety_loop_verified: false
student_safety_local_scope: real_persistent_session_runtime_on_ephemeral_pg_with_sqlite_account_and_declared_guardian_outbox_real_pg_person_consent_gate_real_catalog_subject_key_isolation_and_read_path_binding_fence_closing_the_agent_facing_seams_after_a_legal_manager_change_including_context_prefetch_and_cached_plan_reuse_plus_repeatable_read_snapshot
guardian_authority_evidence: parent_declaration_only_never_verified_link
guardian_declaration_notification_basis: deliberate_identity_declaration_not_consent
postgres_contracts_for_new_paths: scoped_pg17_contracts_passed_including_member_write_read_snapshot_and_history_exit
agent_release_gate_wiring: image_built_and_gate_rerun_offline_as_runtime_user_passing
guardian_notification_delivery_channel: absent_outbox_only_status_pending
firmware_playback_supply_meter_verified: short_playback_software_queue_only_not_dma
pre_roll_code: not_implemented
firmware_face_hardware_verified: false
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.84
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
offsite_backup_enabled: false
wal_retention_policy: unresolved_no_automatic_pruning_at_last_observation
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: pending
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
device_id: dev_atk_a4cb8fd6095c
```

产品不得宣传全双工。小程序仅 profile 页允许经授权有界录制自定义音色样本，不承担实时对话、手机声纹登记或实时媒体回滚。登录账号、当前使用人、说话人确认和敏感授权不得互相替代。

## 最新候选与审查边界

审查基线 `768993b`（2026-09-18 10:22 CST，远端 CI `35298356748` success：`python` 的 Ruff/模块预算/strict mypy（435 files）/可复现协议与契约/authoritative PG gate/pytest 5109 passed/2 skipped/覆盖率 88.11%/wake 12 passed/Offline E2E PASS 与 `agent-image` 实跑，provider smoke `OFFLINE_MOCK=true`，agent/media-edge/media-edge-image/firmware/miniprogram 按 filter skip）；`35296113073`（`264e2d3`）python 唯一失败是 docs-budget 撞非常驻 `RESEARCH.md`（已随 `3d7db99` 折叠进 TODOLIST 并移除）。上轮修复 `350d62d`（CI `35231388322`）收据保留；`f2a95d6` 与整改的远端收据现为本轮 `35298356748`，不再写“远端未跑”。最近功能提交为 `092dcf4`；`fd0290a` 已把下一设备窗口限定为功能验收。旧 Mypy 失败和“gh 不可用、待观察 CI”不再是当前状态；没有这些候选已上线的证据。

本轮仍开放的审查项：

- P1-03 人格投影即时生效（`6f9bcc4`，远端 CI `35304857728` success：Pytest 5110 passed/2 skipped、覆盖率 88.11%、orchestration 90%、provider protocols 95%、wake 12 passed、Offline E2E PASS，Ruff/mypy/模块预算/协议与 PG gate 通过；本地另有真实 PG 用例，无生产/设备访问）：控制端 `ensure_profile` 过去会把 TTL 内、签名仍有效的缓存 profile 原样返回，人格写入要等最长 5 分钟 `profile_ttl` 或下一次自然人脸轮换才可见。现在已签发 profile 的人格快照必须等于授权当前的解析值（subject override 优先、binding default 兜底），不等则在保留 `active_subject_id`/`actor_id` 的前提下推进 `session_epoch+1` 重签，旧 profile 随即 stale。回归：内存控制 `test_persona_write_reaches_the_next_profile_read`（修前第二次读取仍是 `starlight:v1`，修后 starlight→taoxi→starlight 且主体与会话不变）与真实 PG 路由用例 `test_real_pg_control_routes_share_start_current_resolve_and_switch_authority` 扩展（临时禁用检查时 `assert 'taoxi:v1' == 'starlight:v1'` 失败）。仍未验：运行中会话的 `next_safe_point` 主动重协商（写入只在下一次 profile 读取/协商生效；主动投影需要 device→session 映射）、小程序分配 UI 与设备实听。
- P2-04 关闭路径任务泄漏已修（`c817ce9` + `861bd87`，远端 CI `35313490964` success：Pytest 5114 passed/2 skipped、覆盖率 88%、orchestration 90%、provider protocols 95%，模块预算门通过；首版 `c817ce9` 被该门拦下，已把关闭逻辑移入 `runtime_shutdown.py` 并把 `duplex_runtime.py` 收到 4263 行）：`close()` 现在是终结态（幂等），关闭后调度的后台任务被拒绝而不是泄漏，并在 orchestrator 关闭后二次排空 `_background_tasks`；回归用例触发真实 `interaction-delegation-start` 后断言关闭不残留任务。仍未验：WSS/桥接故障注入与 Go 侧 lane 关闭丢帧路径。
- P2-03 主体隔离批次与四迁移（第①步 `504e868`；②/③步与四迁移随本轮提交）：`EvidenceEvent.subject_id` 已通过 `504e868` 落地并有远端 CI `35317165398`、SQLite+真实 PG 回归；本轮进一步把 timeline/search、session context、conversation review/history、life timeline、people、review queue/review、guardian summary/export 与 catalog episode/claim/person/document/review cascade 接入 subject lineage，并提交四个 account→subject SQLite 迁移接缝（durable subject、digital self、persona、memory scope）与唯一 operator 执行入口 `scripts/run_subject_migrations.py`（plan/apply/rollback/status，双围栏；应用启动不执行）。
  - 读取时重新校验 minor retention，撤回后拒绝读回；父子事件主体不一致返回 `409 parent_subject_mismatch`，无法证明旧数据主体归属则省略不猜。
  - subject-scoped partial export 已使用 `format_version=2`、`scope.partial=true`、字段白名单、`content_id`、`service_provider`、`ai_generated`、`manifest_sha256` 与 `omitted_sections`；guardian export 仍是治理 metadata-only，不是逐字内容导出。
  - 仍未做：删除范围验证（备份/MinIO/provider/音色声纹）、读路径的 PG 侧对等与小程序读口、其余 owner 控制面写入者的 subject 归因，以及生产、设备和真实机器人对话验收。
- P2-06 陪伴场景评测首片（`18d36ff`，远端 CI `35315844422` success：Pytest 5116 passed/2 skipped、覆盖率≥85%、orchestration 90%、provider protocols 95%；本地 companionship 用例与 Ruff/strict mypy 通过）：`services/companionship/evaluation.py` 用离线 SQLite ASGI 走真实门（绑定→会话→app_confirm 切人→权威签名 profile→策略/监护同意），固定集 5 例：under_14/14_17 无同意 ⇒ 轮廓无会话能力且无私密记忆能力、成人自用 adult_companion、切人后旧 profile 决策被拒、记忆保留同意授予→撤回。基线 5/5、gate_violations 0、unauthorized_recall 0、p50≈284ms；CLI `scripts/evaluate_companionship.py --dataset … --output …`。仍未验：模型措辞关怀度、回顾可读性评分、超时负例、设备准入。
- P1-06 未见改写集与基线（`c48fcf9`，本地 archive 全套与 Ruff/strict mypy 通过）：新增 4 例未见集（跨会话计划、忌口转述、安慰式回忆、跨账号隔离），固定与未见分报；规则路径基线 recall@5=0.6/nDCG@10=0.6/extraction_recall=1.0/source_attribution=1.0/leakage=0，两条未命中按现状记录为天花板（未调参），用例钉住基线。仍未验：真实 Qwen 抽取器下的两组指标（需密钥）。
- P2-01 记忆预取与 token 预算（`948938c`，本地全量 pytest 通过、Ruff/strict mypy 434 files/模块预算通过，无生产/设备访问）：① 删除未接入的 `MemoryContextClient` 链（生产零构造点 + 专测“被忽略”；prefetch 产出会被 plan 冻结胶囊覆盖），含配置/令牌/部署样例/env 脚本与相关断言；② 记忆胶囊在快照冻结处加 `MEMORY_CAPSULE_MAX_CHARS=1200` 硬预算：整条优先、溢出条目截断、其余丢弃，persona 不计入；指标 `context_memory_chars`/`context_memory_trimmed_total`，实测算例 32×240 字 → 5 条/1200 字、裁剪 27 条、单次约 1.0µs；③ 核对隔离：请求带 session + speaker decision、响应校验同 `speaker_class`、非 owner 草稿清空胶囊、迟到由 epoch/版本守卫拒绝、persona 与当轮情绪分属两处。仍未验：真实链路端到端时延前后对照与未见集召回基线。
- P1-06 episode 身份语义已定并落地（`b79307f`，远端 CI `35310179722` success：Pytest 5116 passed/2 skipped、覆盖率 88.13%/门 85%、orchestration 90%、provider protocols 95%；本地 SQLite + 真实 PG，修前红/修后绿）：抽取器显式 `canonical_key` 即 episode 身份，独立于词表 `domain_category`——同一 key 跨 domain 合并、episode 保留首见 domain；不同 key 与无 key 路径的 domain 硬门、实体互斥、阈值 0.62 全部保留（判据/阈值/数据集未动，规则路径固定集指标仍 0.9375/0.859/0.95）。此前该用例结构性不可过：跨会话无 key 上限 0.55×1.0+0.15×0.4=0.61<0.62，且候选 SQL 按 domain 过滤取不到对方 episode；规则抽取器从不产出 canonical_key（唯一产出者是 Qwen 路径）。评测脚本新增 `--extractor configured` 用生产装配跑同一固定集，缺密钥/未关 `OFFLINE_MOCK` 时显式退出而不是静默按规则打分。回归：跨 domain 合并、不同 key 不合并、SQLite e2e 双来源 episode、`repeated-episode-campus-startup` 判据通过（用产出 key 的抽取器）、PG 同款契约，另钉住规则路径固定集指标。仍未验：真实 Qwen 抽取器下的固定集与未见改写集（需密钥）。
- P1-04 现状核查（本轮只读，scout 全量 + 抽查，无生产/设备访问）：链路的服务端与 Agent 侧**已完整**——真实解码体检（`services/voice_profile/sample_validation.py`，不过门 422 且不落行）、拒绝文案（`sample_copy.py`）、持久化派生的进度与 60s 预算（`enrollment_progress.py`）、生命周期与撤销删样本/删厂商音色（`manager.py`/`postgres_manager.py`）、厂商客户端超时（`cosyvoice_enrollment.py` 120s）、HTTP 面（`routes/voice.py`）、设备出声合同（`companion_delivery.py::_attach_personal_clone`，含 provider/model/resource/过期与设计音色回落）、Agent 侧解析与应用（`voice_profile_client.py`/`agent_voice_profile.py`/`providers/doubao_tts.py`）；小程序已有录音/预检/轮询/over_budget 闭环与回归。**四处缺口（① 已按代码证据修正）**：① 不是缺陷——provider 结果不确定/失败时 operation 进 `reconciliation_required`、profile 保持 `enrolling` 是**有意**的可恢复语义（`manager.reconcile_enrollment` + `pending_enrollments` 可补完为 `candidate`，见 `services/voice_profile/tests/test_manager.py` 的 provider 持久化触发器用例：重放 409 → 对账恢复成功），不得改成终态 `failed`；真实缺口只是用户可见信号——过 60s 预算后客户端只有"可以离开"，没有"仍在处理/需要处理"的区分（`status='failed'` 实践中只由 `revoked` 触达）；② 客户端 `enrollments` 从不带 `custom_persona_id`，克隆绑不到人格；③ 客户端撤销被 `configActionGate` 无条件 fail-closed 挡住（占位弹窗），属待决策的 consent 决策接口边界，**不得绕过门**直连 `DELETE /v1/voices/consent`；④ 缺真实厂商克隆→设备实听耗时证据（smoke 仍 `OFFLINE_MOCK`）。
- P1-03/P1-04 小程序自定义人格与声音闭环（`6c3bc20`/`9e0d2b4`/`b25268b`，本地 `npm test` 229 passed + 全量 `node --check`，CI miniprogram job 覆盖）：新增 `pages/persona-custom`（名字+描述 → 结构化或手填十一个受控字段 → 创建即冻结 v1；列出并确认后删除）；我的页在 persona 为 `cu_*` 且账号目录存在时把 `custom_persona_id` 随声音克隆提交（内置人格留空）；设备页人格选择器纳入账号自建人格；**退役 bio 标记死路径**（`utils/custom-persona.js` 与三处调用删除——服务端旧 bio 自由文本形态已移除，客户端继续写 bio 是假能力），home 改为按运行时人格 + 账号目录显示自定义人格名。仍未验：设备实听、厂商克隆耗时。
- P1-03 小程序人格分配 UI（`64a742f`，远端 CI `35306937489` miniprogram job success：225 passed / 0 fail + 全量 `node --check`；本地同口径）：设备页按使用人分配/取消分配人格。选项来自内置伙伴目录（与服务端 `COMPANION_IDS` 一致），当前值按 override 优先、binding 默认兜底解析；PUT 后立即回读服务端分配，失败保持抽屉打开并显示错误（不置成功态）。实测边界：自定义人格未进选择器（缺 `GET /v1/personas` 接线），产品内邀请通道是 `/v1/guardian/links` 而非 `/v1/relationships/invites`（后者要求小程序拿不到的 `established_evidence_id`），`pages/guardian` 已有邀请/年龄段/授权开关。仍未验：设备实听、运行中会话的 next_safe_point 轮换。
- P1-05 旧三项探针结论已被当前未提交主体批次 supersede：此前“同账号切主体可见/无 subject 字段”只描述旧数据层，不能继续作为当前状态。当前代码已把 review/history 接入 subject lineage，按主体过滤并在读取时复核 minor retention；撤回后拒绝读回，无 eligible 话轮返回空列表且不编造汇总。
  - 当前仍未验：小程序三端的登录绑定、主体切换、回顾与权限刷新；Edge→Control 受鉴权只读状态出口；生产/设备链。当前状态为 `code=完成（未提交）/wired=未接入/enabled=未启用/verified=本地与权威 PG 回归`。
- P0-04 / P1-03：成员追加的三处缺陷已修（`350d62d`，见下方收据）。同一轮仍未证明的是下游同意门是否曾被绕过——本地只证明了 binding grant 扩大，没有复现越权读取。
- P0-04 读一致性已修（`f2a95d6`）：`read_transaction` 固定 `REPEATABLE READ` 只读；真实 PG 交错回归各 1 例（读间旋转混对、读间撤销翻转），修前源码上失败、修复后通过。不标已泄漏。
- P0-03 TTS（`27a16cf` 修正 `f2a95d6` 收据）：无时间戳降级 1 例 + 取消优先 1 例通过；`f2a95d6` 的 `slow_once` 回落测试收据作废（未触发重入、改前已通过），已由 `slow` 持续首包失败真回归替代（personal 1 次、callback/trace 各 1 次、总 5 sessions；修前 personal 4 次）。G 矩阵/EOU/部分音频设备终态/B/D/时延门仍待设备链。
- P2-05 工具（`f2a95d6` + `27a16cf` + numerator 追补 + `768993b` 严格自包含）：曝光时间线 2 例、入窗起点 2 例、settle-wake 排除 1 例；去重用例内联 fixture 且不再读 ignored receipt（`RECEIPT_CONSOLE` 与对账分支已删）；CI 新增 wake 显式步骤，远端 12 passed。分子/分母/report wall 全口径为门后起算，`started_monotonic` 保持门前（收据不 breaking）。设备矩阵未采。
- P1-05（历史收据已被当前未提交主体批次 supersede）：回顾出口保留事件 id 与 `assistant_approximate`；旧收据只证明 account 隔离，当前批次已补 subject lineage、主体过滤与 minor retention 读取复核。小程序三端、Edge→Control 只读出口、生产/设备链仍待验。
- 修复轮回归（远端 `35298356748`，`768993b`）：python 全量 5109 passed/2 skipped、覆盖率 88.11%、wake 12 passed、Offline E2E PASS，Ruff/模块预算/strict mypy（435 files）/authoritative PG gate/agent-image 通过；provider smoke 仍 `OFFLINE_MOCK=true`。Agent interaction 用例退出时的 `interaction-delegation-start` pending 提示仍归 P2-04 定位，本地 agent 全套加 `-W error::RuntimeWarning` 未复现（见 P2-04），不是本轮结论。

下一步以冻结候选 `memoria-agent:b668960`（`sha256:927d…`，未启用）等人确认启用后再按 P1-01 跑设备 live 验收（本轮仅 preflight-only：receipt 合法、`usbmodem101` 可见、`serial_opened=False`）；生产切流与回滚另获授权。学生安全设备专项及全双工仍未通过。详细顺序、复现和完成条件只在 `TODOLIST.md` 维护。

### 已有软件收据（按提交范围解读）

- [fixed 2026-09-17, commit `f2a95d6`; local code+tests+real PG (Docker PG17) where the seam needs it, no production/device access] P0-04 读一致性 + P0-03 TTS 软件边界 + P2-05 唤醒工具 + P1-05 会话回顾：
  - 读一致性：`PostgresSessionRuntimeStore.read_transaction` 现为 `transaction(isolation="repeatable_read", readonly=True)`，profile/context/binding fence 共享同一快照（快照由首条 profile 读建立；只读无谓词锁，写侧 CAS 不变）。2 个真实 PG 交错回归在修前源码上失败、修复后通过；session_runtime 全套 47 例、policy chain 全套通过。
  - TTS P2：`COSYVOICE_WORD_TIMESTAMPS=false` 即纯音频语义——batch 返回完整 PCM + 空词 + `degraded`，单次连接；`test_word_timestamps_disabled_returns_complete_audio_without_word_metadata` 修前抛 `CosyVoiceTimestampError`、修复后通过。
  - TTS P3（收据已作废，见下条）：`test_livekit_stream_personal_before_audio_fallback_fires_exactly_once` 用 `slow_once` 未触发框架重入、改前已通过，不算修前失败。
  - P2-05：`_exposure_over_timeline` + 2 个时间线用例 + 去重 fixture 内联（删 ignored 文件 9/9）+ CI wake 显式步骤；`_run_window` 的端点采样循环已删，曝光/暂停全由时间线求交得出。
  - P1-05：`GET /v1/archive/conversation-history` + 1 个配对/跨主体用例；archive 全套 53 passed / 1 skipped。
  - 门禁：`ruff check .`、module budget、strict mypy（435 files / 0 errors）通过；带 DSN 全量 `pytest` 5107 passed / 3 skipped，总覆盖率 87.86%，85% 总覆盖与 orchestration 90%、provider protocols 95% 通过；Offline E2E PASS。远端 CI 未跑。仍未验：小程序成员/回顾 UI 与设备链、厂商真实 TTS/厂商、设备矩阵与时延、安全专项。
- [fixed 2026-09-18, commits `27a16cf` + numerator 追补; local code+tests, real PG only for untouched-path rerun, no production/device access] advisory 整改：
  - Doubao 真重入：`slow` 持续首包失败 + `max_retry=3`，personal 仅 1 次、callback/trace 各 1 次、总 5 sessions；修前 provider（`f2a95d6~1` 文本比对 + 临时旧文件实跑，tree 未动）personal 被试 4 次。旧 `slow_once` 测试收据作废。
  - CosyVoice 取消优先：降级分支并入取消优先收尾；trace 回调置 cancel 的回归确定性覆盖（旧分支返回 `discarded=False` + 连接释放）。
  - P2-05 全口径：`window_start = gate_ready_at`，`_wake_lines` 下界同口径，收据新增 `exposure_started_*`（`started_monotonic` 不动），report wall 取 exposure 起点（旧收据回落原口径）；fake-clock 证明修前多算 ~1.5 s/settle wake 计入，修复后排除。
  - P1-05：话轮带 `owner_event_id`/`assistant_event_id`/`assistant_approximate`；撤回跨主体收据，同账号切主体/subject 围栏/临时读回待验。
  - 门禁（本地）：Ruff、模块预算、strict mypy 435 files、文档预算通过；受影响 121 passed / 1 skipped；PG 两套 59 passed。全量 pytest/覆盖率未重跑。
- [fixed 2026-09-17, commit `350d62d`; local HTTP+SQLite and real PostgreSQL 17, no production/device access] P0-04 / P1-03 成员追加三处缺陷：
  - 门禁：`ruff check .`、module budget、`mypy services --strict`（435 files / 0 errors）通过；带 `MEMORIA_TEST_POSTGRES_DSN` 的全量 `pytest` 5101 passed / 3 skipped，总覆盖率 87.77%，85% 总覆盖与 orchestration 90%、provider protocols 95% 门禁通过；远端 CI `35231388322` 整轮 success。仍未验：小程序成员 UI 与设备链、下游同意门是否曾被绕过（未复现）。

The 2026-09-16/17 local work has since been committed (`e5f9d50`, `7c0ef48`, `ec56d9a`, `6ab16b9`, `77fc86a`, `e1878ce`, `e8571d3`, `52241ed`, `a8ce4e1`, `30dea92`, `229ee13`, `3babf17`, `a65b8f2`). That historical wording does not describe the current checkout: the current worktree intentionally contains a new uncommitted subject-scope batch on top of `aac99d3`; its delivery level is `code` only until it is committed, wired, enabled, and verified.

- 语音 TTS（P0-03）：空洞的 TTS 回归测试已删除，`services/agent/src/providers/generation_budget.py::GenerationBudget` 现在是两条 provider、四条路径（Doubao/CosyVoice × stream/batch）唯一的"首包预算 / 收到消息即续期的停滞预算 / `max(total*5, hard_deadline_s)` 硬上限"决策点，超时分类统一为 `first-audio-timeout` 与 `total-timeout`（批式首包后不再抛裸 `TimeoutError`）。新增 `DOUBAO_TTS_HARD_DEADLINE_S`（默认 180，生效上限取 `max(total*5, 该值)`，默认行为与旧内联 180 一致）。用例：流式续期（父提交 `a8a0e43^` 上以 `total-timeout` 失败）、真实停顿有界失败、40 分片持续进展仍被 0.5s 硬上限按 `total-timeout` 终止、批式续期与批式首包后分类、CosyVoice 批式续期；后四条在当时修复前的 provider 基线上均失败。`MockDoubaoServer` 新增 `pcm_chunks`（N 路分片输入）与 `chunk_count`（已发分片计数），`MockCosyVoiceServer` 新增 `chunk_delay_s`。
- TTS 重试语义（P0-03，2026-09-16 第四轮，本地代码+测试）：`synthesize_stream_text` 的重试规则已显式定义并与预算同址（`generation_budget.py` 的 `BeforeAudioError`/`AlignmentRetryError` 标记与 `retry_allowed`/`retry_may_change_voice`，未知错误 fail closed）。音频前失败（首包超时、连接/握手、供应商 pre-audio 错误）可重试一次并允许回落设计音色；音频已存在只缺时间戳时可重试一次但必须保持同音色；音频后的停滞（`APIConnectionError: total-timeout`）、供应商错误、PCM 连续性失败与时间戳失败一律终态，最多两次尝试，且失败尝试的缓冲被丢弃——部分音频不会作为整句返回或重放。本轮修掉一处真缺陷：CosyVoice 克隆音色在"音频后缺时间戳"重试时会被换成设计音色（`test_clone_missing_timestamps_retries_without_changing_voice` 在旧行为下失败、现通过）。`MockCosyVoiceServer` 新增 `stall_after_pcm` 场景；Doubao 侧保护用例证明音频后停滞只 1 个 session 且音色不变、音频前首包失败仍回落一次。
- 语音输出终态（P0-03）：首帧已下发后的 provider 崩溃、停滞（`MEDIA_OUTPUT_GENERATION_TIMEOUT_S`）与硬期限（`APIConnectionError: total-timeout`）三种故障已本地注入验证：`services/agent/tests/unit/test_media_output_partial_failure.py` 断言有界时间内（0.2s 停滞预算内）落地同一终态——一个后继代 `REALTIME_EFFECT_KIND_CANCEL_GENERATION`（设备据此退出 speaking 并 flush，source `output_provider_failed`/`output_timeout`）、交付账终态为 `ReplyDeliveryEvent.ERROR` 而非 `PLAYBACK_ENDED`、`provider_complete=false`、`interaction_phase=listening`、`assistant_speaking=false`，且旧代的迟到 ACK/ENDED 不改变权威 fence、终态与发射计数。这验证的是进程内故障注入；B 的真机表现（桥侧旧总挂钟 20.23s 出错、约 38.6s 后才错误收尾）仍需设备捕获核对。
- 语音续问（P0-03）：G 的静默预算语义已统一：`owner_silence_remaining_s` 的 `None/0.0/>0` 三态明确，结束已测预算记 `0.0` 而非 `None`；计数器改为 `owner_silence_activity_revision` 并收敛到 `_note_owner_silence_activity`；被受理的 ASR final 与已受理 VAD 一样可作为"已受理主人活动"失效仍在等锁的关闭，但只在仍有其它界时生效、且不刷新预算。`test_accepted_final_can_veto_a_parked_grace_close` 与 `test_ending_a_measured_budget_records_it_as_spent_not_unmeasured` 在当时修复前基线上失败、修复后通过；两条真实入口保护用例两版都通过。仍未做：真机复跑 G 取证、无验证说话人且无其它界时的关闭语义、endpoint/commit 乱序与 watchdog 交接的 10s/60s 完整矩阵、部分音频失败的设备终态、B/D 设备停滞、时延门。
- 学生安全（P0-04）：`_primary_subject` 不再替孩子确认，`guardian_of` 保持 `pending` 且只记录家长一侧确认（evidence `guardian_declaration_v1:device_binding`）；伪造的 `establish_active_link`（含 `verified_via=wechat_identity`、合成 code hash、365d 到期，并会激活既有 pending link）已从 port 与两个 store 删除。危机通知改由调用方从 Identity 解析声明监护人以 `declared_guardian_ids` 显式传入，PostgreSQL 侧新增 `guardian_enqueue_declared_notification` 在库内复核声明；SQLite 直接插入。这是有意决定：单方声明是通知依据，但不是已验证监护，也不解锁 consent。
- 声明来源约束（P0-04，2026-09-17，本地代码+测试+真实 PG + authoritative gate）：`e1878ce`。此前任何已登录账号都能对任意已知 person id 发 `guardian_of` 邀请并自己 accept，`declared_guardians` 与 `parent_for_child` 的 guardian 角色都接受该单方行，于是第三方可进入孩子的危机通知收件人集合。现在声明必须同时满足：带设备绑定证据 `guardian_declaration_v1:device_binding`、在有效期内、且声明人是某个 ACTIVE binding 的 `account_owner` 而该 binding 把主体列为 `primary_subject`（即 `POST /v1/device-bindings` 的 `parent_for_child` + `subject_draft` 唯一生产写入形状）。`parent_for_child` 的 guardian 角色另要求声明人就是 binding owner，或持双边 ACTIVE `guardian_of`。存储层 `has_source_confirmed_relationship` 保持无范围语义（建绑定时尚无 binding 行，加范围会形成循环依赖）；绑定范围判断在 `IdentityService.declared_guardians`（服务层，含 validity window）与新增的 PG 授权函数 `identity_relationship_declared_for_binding`——后者被 `guardian_relationship_declared`（危机事件读取策略与通知读取策略共用）与 `guardian_enqueue_declared_notification` 同时调用，授权缺失即 fail closed。回归：身份层第三方自声明排除/撤销/过期三例（内存+SQLite 双参数）、HTTP 层真实 invite+accept 后仍被排除且拿不到 guardian 角色、PG guardian 契约新增绑定范围正例与「无 binding 的真实单方声明」反例。实跑：`services/{identity,guardian,control_api,session_runtime,policy,consent}/tests` 带 `MEMORIA_TEST_POSTGRES_DSN` 全绿，`scripts/tests/run_authoritative_postgres_gate.sh`（init + repeat-upgrade + verify）通过。交付等级为 `code`；通知投递通道仍缺席，未验设备端。
- 无账号孩子 consent 闭环（P0-04，2026-09-17，本地代码+测试+真实 PG + authoritative gate）：`77fc86a`。旧链路 consent 强依赖 `guardian_links`，无账号孩子无法创建/确认 link 导致 consent 永久 fail closed。新增 `PersonConsentRecord` 领域实体，在 SQLite 与 PostgreSQL 建立 `guardian_person_consents`（带 FORCE RLS、`guardian_controller_person_consents` 策略与 maintenance 数据治理统计）；提供 `POST/GET/DELETE /v1/guardian/minors/{person_id}/consents`，由活跃 `parent_for_child` 设备绑定拥有者控制；读门 `active_consent` 做 link/person 并集统一解析。端到端回归验证：HTTP 授予 `memory_retention` 后 `/session-policy` 解除 `ephemeral_only` 记忆天花板，撤销后恢复保守态；非拥有者 403；`/response-plan` 补齐 `unknown_safe` 模式下直接 200 且 0 条记忆项泄漏回归。交付等级为 `code`。
- [fixed 2026-09-17 a8ce4e1, local code+tests+real PG] Person-consent lifecycle and P1-01 type blocker: `GuardianStorePort.active_consent` is now the link/person union (mypy --strict 0 errors); person-consent single read and revoke run under a trusted subject context on the non-superuser NOBYPASSRLS PG role (cross-child/cross-parent denial, revoke replay, `active_consent` tightening, export/delete/remaining counts); the recorded grantor can revoke after an unbind while strangers get 404; grant replay is idempotent (same key/body returns the original record and evidence, a different payload is 409, concurrent replay converges, an evidence write failure leaves no usable consent).
- [fixed 2026-09-17, local code+tests+real PG on 5526c39, this continuation round] P0-04 remaining real-PG matrix + local full-CI rerun:
  - Four new tests in `services/control_api/tests/test_persistent_runtime_policy_chain.py` (commit `30dea92`) close the previously open authority/mismatch/manager/concurrency part of the P0-04 software matrix on the real persistent Runtime (fresh PG database per test): ① authority unavailable — with the service missing, `/session-policy` fails closed 503 `session_runtime_authority_unavailable` while `/response-plan` runs the documented offline profile (account is the subject by construction); with a real store whose PostgreSQL is unreachable (dead DSN), the policy seam still 503s and `/response-plan` stays conservative — the crisis fixed reply is delivered verbatim, zero memory reads, no account fallback for the child subject. ② real profile expiry — the authority itself issues `profile_ttl=2s`; after the TTL both seams close (policy 503, response-plan conservative 200 with zero memory reads) and `close_session` still applies, so expiry denies work without stranding the authority. ③ legal-manager change — a foreign anonymous account cannot switch the subject of a session it does not own and cannot read it as its authority, the session epoch is unchanged, and the device-facing `/v1/interaction/action-policy` rejects a superseded profile id with 403 `action_profile_forged` and a stale session epoch with 409 `action_fence_stale`; the fresh-epoch positive authorization remains covered by `test_authorize_action_reuses_same_transaction_consent_discovery` on the consent-enabled PG fixture. ④ concurrent switches — contending `switch_subject` calls serialize into strictly increasing unique epochs or are denied; the authority's current profile after the storm is exactly the last accepted epoch and `/session-policy` reads that same signed profile; the pre-storm child epoch is denied as stale by `decide`.
  - Local full python-CI rerun (all steps of the CI `python` job) on HEAD 30dea92 (same code as a8ce4e1 for runtime paths; 30dea92 only adds tests): ruff, module budget, mypy --strict 0 errors (435 files), media proto reproducible, multi-subject contracts in sync, authoritative PG init + repeat-upgrade gate, media smoke + replay/chaos/load, full pytest with `MEMORIA_TEST_POSTGRES_DSN` (local Docker `pgvector/pgvector:0.8.1-pg17-bookworm`, host 55439): coverage 87.80% (85% gate), orchestration 90% and provider-protocol 90% gates pass, Offline E2E PASS, provider smoke skipped under `OFFLINE_MOCK=true`. Gotcha recorded: the same pytest command without the DSN yields 81.96% and fails the 85% gate — DSN-gated tests must not be skipped when claiming CI parity. At the time of this receipt, GitHub CI was unobserved (`gh` unavailable in that session); this status is superseded by the successful remote CI runs recorded in the review above.
  - P0-04 category matrix + real-catalog subject-key isolation (commit `a65b8f2`, same local scope: Docker PostgreSQL + plain venv, no production/device access): `test_subject_category_matrix_keeps_minor_adult_and_unknown_safe_distinct` drives both seams per category on one real persistent Runtime — under_14 and 14_17 both resolve to `student_minor` and are treated identically (ephemeral-only ceiling, no issued session capability, exactly one `guardian.crisis_event` plus a pending notification bound to the declared guardian), the two legal adult shapes produce NO minor-crisis evidence (the crisis evidence event is written before any notification target is resolved, so its absence is the assertion an "always a minor" regression breaks), and the self-owned `adult_companion` turn is the positive control that keeps its own display name and really reads its own memory (per-cell catalog query delta 1) while the non-owner adult family member gets NO `owner_display_name` and a read delta of 0; the signed `unknown_safe` profile stays conversation-only (`conversation` the only true capability) and records no crisis evidence. `test_real_catalog_withholds_account_memory_from_a_child_subject` replaces the catalog double with a real `PostgresLifeArchive` + `PostgresMemoryCatalog`: an account-keyed claim is compiled, confirmed through `review_queue`/`review`, proven recallable under the account key directly, and STILL withheld from the child after a real retention consent provably lifts the ephemeral-only ceiling, while the account's own turn in the same session reads it. Local re-verification of the whole python gate on `a65b8f2`: ruff, module budget, mypy --strict 0 errors (435 files), media proto reproducible, multi-subject contracts in sync, authoritative PG init + repeat-upgrade, media smoke + replay, `buf lint`, full pytest with the DSN (5084 passed / 3 skipped), coverage 87.81% (85% gate), orchestration 90%, provider protocols 95%, Offline E2E PASS. Open after this: old-cache replay beyond switch/epoch races, the P1-03 minimal app_confirm entry, and device admission — `student_safety_loop_verified` stays false.

  - P0-04 profile-session mismatch cell + the legal-manager change CLOSED on the read path (commits `9c04a35` for the mismatch cell, `f67a134` for the fix; local code+tests+real PG, no production/device access): `test_both_policy_seams_reject_a_diverged_runtime_projection` forces the authority's projection row to diverge from the stored signed profile (the condition the nine-field guard exists for) and pins the split contract — `/session-policy` 503 `session_runtime_profile_binding_mismatch` (it must not decide), `/response-plan` a deliberately bounded conservative 200 (the crisis fixed reply must stay deliverable), with zero memory reads, no grounded items, no retention grant and no crisis evidence on either seam. The manager-change defect recorded at `9c04a35` is now FIXED and its characterization test is flipped into `test_legal_manager_change_closes_the_agent_facing_seams`. The take-over is still applied on the authority plane — the ACTIVE binding goes `superseded` and another owner's binding version 2 becomes ACTIVE for the same device — and the read path now withdraws the authorization basis three ways: `current()` applies a new narrow `SECURITY DEFINER` predicate `action_identity_binding_is_current(device_id, binding_id, binding_version, now)`, created as the Identity owner inside the Session schema's existing cross-domain section (plain `LANGUAGE sql STABLE`, fixed `search_path`, `row_security = on`, no locking clause because the caller runs a read-only transaction, validity window part of the answer for the same reason the write-side lock applies it), REVOKEd from PUBLIC and granted to `memoria_session_api` only — deliberately NOT inside `_active_profile_context`, because `close_session` goes through that path and the close-only path must still close (`session_runtime_close_session` states the same rule: closure is terminal cleanup, not a new authorization). Two further routes could still carry the withdrawn subject's memory and are closed in the same commit: `/v1/interaction/context-prefetch` skipped the subject scope ENTIRELY — no authority read, and `_companion_items` called without `account_keyed_memory_readable`, so it defaulted to True and handed the account's keyed memory to whatever subject was using the device, take-over or not — and it now resolves the same scope as `/response-plan` through one shared resolver (`_resolve_subject_memory_scope`) so the two seams cannot drift apart again; and a cached `/response-plan` payload is now re-fenced before reuse, because the cache is keyed by the generation fence that the take-over leaves intact, so widening the key would NOT have fixed it (the denied entry is dropped rather than retained). Contract side: the new function joins `_REQUIRED_FUNCTIONS` (set equality) and a `_SESSION_API_FUNCTION_SIGNATURES` set that `_verify_runtime_roles` checks as a real EXECUTE grant on the read role, so a deployment that cannot answer the question 503s instead of answering True. Observed outcome on the real persistent Runtime: `current()` raises `PersistentSessionDenied`, `/session-policy` 503 `session_runtime_authority_unavailable`, `/response-plan` a bounded conservative 200 with the crisis fixed reply verbatim, zero account-keyed reads for a new turn, for a VERBATIM RETRY of the pre-change turn and for `/context-prefetch` (asserted as a POSITIVE CONTROL before the change, so the empty result afterwards is not vacuous), no substituted subject, no crisis evidence, and `close_session` still applied — the close-only exemption is itself an assertion. Both new assertions were confirmed non-vacuous by disabling each guard in turn and watching the expected assertion fail, which is also how the cache path was caught: an earlier pass was traced to a dead-code insert that had left the fence active, not to a weak test. Local gates on `f67a134`: authoritative PostgreSQL init + repeat-upgrade, `ruff check .`, module budget, `mypy services --strict` (435 files, 0 errors), and the real-PG suites `services/session_runtime` 90/90 and `services/control_api` 677/677 (both exit 0, zero failures). Two new contract tests pin it: `test_postgres_readiness_rejects_missing_binding_fence_execute` and `test_postgres_binding_fence_port_is_definer_fixed_path_and_read_role_only`. Still open under P0-04: old-cache replay beyond the switch/epoch races now covered, the P1-03 minimal app_confirm entry, and device admission — `student_safety_loop_verified` stays false.
  - [fixed 2026-09-17（晚四）, local code+tests (+real PG where the seam needs it), commit `092dcf4`] P0-04 read-path hardening round 2 + P1-03 minimal subject entry + P2-05 tool repair. (a) `PostgresSessionRuntimeService.current()` now evaluates profile, context, TTL and the binding fence inside ONE read-only transaction (`_load_active_profile_context` shared with the still-unfenced close-only `_active_profile_context`); one call costs one connection. Review correction: the store does not specify isolation, so a stable snapshot across these reads is not yet proven under default read committed; see the P0-04 interleaving-test gap above. (b) Agent-side stale caches closed: every cached response plan is stamped with its authorizing `runtime_profile_id` and a same-fence retry after degrade/replacement fails closed and drops the entry (older-epoch entries evicted on insert); epoch rotation now resets the snapshot manager, the rolling conversation context and the epoch-bound prefetch keys exactly like the orchestrator bump already did -- previously a refresh-degrade kept generating from the old subject's capsules. Four new unit tests pin it (`test_cached_plan_stays_reusable...`, `test_cached_plan_dropped_once_authority_degraded`, `test_plan_cache_evicts_superseded_epochs_on_insert`, `test_identity_rotation_resets_snapshots_context_and_prefetch_keys`; the rotation test was confirmed to fail with the duplex fix reverted). Incidental find while verifying: the repair pass once dropped the `legacy_expires_at` provenance field and broke two legacy voice tests; restored and the diff re-checked to a single-purpose change. (c) P1-03 control-side minimal entry: `POST /v1/devices/{id}/binding/members` (owner appends an existing or `person_id=new` draft member as a new binding version; single-subject modes 422, duplicates 409, extra roles copied (this review found empty permission sets were lost), guardian declaration for parent_for_child minors) and `PATCH /v1/persons/{id}/age-evidence` (self or binding-owner declaration only; adult claims 422; verified-adult contradiction fails closed to disputed). Three new HTTP tests prove the chain the P0-04 matrix needs: second child added, resolvable as a candidate, and switched via app_confirm. The miniprogram client already gates everything on `app_confirm` and was left untouched; member/invite/assignment UI wiring stays pending. (d) P2-05 wake tool initial repair per the tool-mandate list (standby + detector-on gates, effective-exposure accounting with pause reasons, half-open windows, unified `_is_wake_event` dedup, invalid-not-miss on serial/play/state failures, finally-safe restore with reread verification, human-confirm onset latency, per-level input metering) with 7 new offline tests in `scripts/tests/test_wake_word_matrix.py`; the 20260916 receipt was only read, never rewritten or re-collected. Local gates on `092dcf4`: `ruff check .`, module budget (agent.py 2370 / duplex_runtime.py 4264 re-pinned to exact counts), `mypy services --strict` (435 files, 0 errors), media proto reproducible, contracts in sync, authoritative PG init + repeat-upgrade, `buf lint`, media smoke + replay, full pytest with the DSN (5095 passed / 3 skipped, coverage 87.83% with the 85/90/95 gates green), Offline E2E PASS; the two Go-contract script tests fail only under the stale session `GOROOT=/usr/local/go` and pass with it unset (environmental, pre-existing). External acceptance remaining at that time (not the complete current defect list): device admission (a final P0-04 gate), miniprogram member/invite UI, the P2-05 device matrix on a predefined protocol, P1-07 hardware runs, production cutover/rollback rehearsal, and any item whose completion condition names production, hardware, or an explicit authorization -- `student_safety_loop_verified` stays false.

- [fixed scope 2026-09-17, moved from TODOLIST] 立即修（P1，PG 撤销失效）：`postgres_store.py:get_person_consent/revoke_person_consent` 首次查询把 `guardian_subject_id` 设成家长 id，但 `guardian_controller_person_consents` 要求它等于记录的孩子 id。本轮隔离 PG、`rolsuper=false/rolbypassrls=false`、FORCE RLS 下实测授予与列表成功，家长 get/revoke 均 `GuardianNotFoundError`，失败后 `active_consent` 仍有效；HTTP DELETE 的前置 get 会映射为 404。让读/撤销使用可信 subject 上下文，保留 FORCE RLS，不用 superuser/放宽策略绕过；补跨孩子/跨家长拒绝、撤销重放及撤销后策略收紧的真实 PG 行为测试。
- [fixed scope 2026-09-17, moved from TODOLIST] 立即修（P1，解绑后不可撤销）：本地 SQLite HTTP 实测 grant=201 → unbind=200 → 原授予人 DELETE=403，存储读门仍返回有效 consent。原因是 DELETE 与授予共用 ACTIVE binding owner 前置门，而 person consent 没有联动失效路径。先明确解绑、过期、移除主体/换管理人时授权保留或终止的语义，并保证原授予人可安全撤销自己签发的授权；不得靠重绑设备或伪造 link 才能撤销。手工 profile 探针中解绑后 `/session-policy` 仍未恢复 `ephemeral_only`，仅证明该测试形态；真实 Runtime 的 profile 失效传播及在途会话还须补验。
- [fixed scope 2026-09-17, moved from TODOLIST] 同批补（P2，授权重试）：相同 body 与 `Idempotency-Key` 的 HTTP POST 首次 201、重放 409。路由每次重算 `granted_at`，store 对固定 consent id 做完整 record 比较而冲突；补同请求返回原授权与原证据、异 payload 冲突、并发重放及证据写入失败后的恢复，SQLite/PG 两端一致。
- [fixed scope 2026-09-17, moved from TODOLIST] 同批补（P2，导出遗漏）：SQLite `remaining_account_rows` 实测 `person_consents=1`，但 `export_for_account` 没有 `person_consents` 字段；PG 已有该导出。补 SQLite 导出与两端 grantor/subject 范围、真实非空行的导出/删除/剩余计数契约，不以空表键集合或只测删除替代。完整 subject 谱系仍归 P2-03。
- 记忆召回（P1-06）：固定集 16 例 recall@5 由 0.8125 升到 0.9375、nDCG@10 由 0.734 升到 0.859，`candidate_leakage`/`cross_account_leakage` 仍为 0、`extraction_recall`=1.0、p50≈0.72ms。三处改动：`RecallPlanner` 新增“避开/忌口/不能吃/别吃/注意别/过敏”标记 → “不喜欢/讨厌/不吃/忌口/不要”词表扩展；评测 adapter 改为与生产读路径一致（生产总是先 plan）；规则抽取器新增“家里人叫(她|他)X”别名句式（仅在唯一人物且非角色词时生效，并补了反例）；编译期为人物建立 search document（此前 person 只在 `person_aliases`，没有任何读路径会搜它），确认时随同事件投影提升，未确认人物仍被 confirmed-only 挡在外面。仅剩 `repeated-episode-campus-startup`（跨会话 episode 合并的产品语义未定，不抬分）。人物投影已同步到生产 `postgres_memory_catalog.py`，并补了 DSN 门控契约 `test_postgres_person_alias_is_recallable_and_status_gated`；本轮已用本机 Docker（`/usr/local/bin/docker`，Docker Desktop 29.7.2）起临时 `pgvector/pgvector:0.8.1-pg17-bookworm` 容器（宿主 55432），把全部 DSN 门控契约实跑通过；`scripts/tests/run_authoritative_postgres_gate.sh` 的 init + repeat-upgrade 门禁与带 DSN 的全量 `pytest services/ tests/` 都通过。实跑发现并修掉两处真缺陷（跨 schema 授权的安装顺序依赖、声明监护人被 RLS 拒绝写/读）。
- ASR 救援边界（P1-02）：`services/agent/tests/integration/test_funasr_rescue_audio_shapes.py` 用**合成** PCM 波形（非真实录音）钉住救援路径的判定边界——静音/室内底噪在本地被门禁拦下且厂商零请求、削波满幅按上行原始字节送判、超长段只送最新尾帧（内容精确比对）、三路并发 + 慢厂商降级为有界 `no_text`、在飞行中的救援发布 `now + 2.5s` 预算。仍未做：真实中文录音语料与 sidecar 的生产启动脚本/Dockerfile/模型摘要入仓（后者只在服务器上，本轮未连生产），故 P1-02 的「仓内输入可重建」仍不成立。
- 真实 API 路径（P0-04）：`test_accountless_child_profile_reaches_the_device_through_the_real_api` 用真实 HTTP 链（device-binding → session → resolve-subject → app_confirm 切人 → device runtime-profile）取到 account-less 孩子的签名 profile，并用决策路径同一 `verify_runtime_profile_payload` 校验 `active_subject_id/subject_category/age_band/service_mode` 与「仍无 active guardian link」；此前该链只有手工注入。
- 真实 persistent Runtime → 两条策略入口（P0-04，2026-09-16 第四轮，本地代码+测试+真实 PG 实跑）：`services/control_api/tests/test_persistent_runtime_policy_chain.py` 把 `PostgresSessionRuntimeService` 接到真实 PostgreSQL（本机 Docker `pgvector/pgvector:0.8.1-pg17-bookworm`，宿主 55439），经真实绑定 → `/v1/sessions` → 权威 `switch_subject` 取到孩子的签名 profile，再驱动 `/v1/interaction/session-policy` 与 `/v1/interaction/response-plan`（不再注入 profile）：两条入口读到同一 `runtime_profile_id`、`session_epoch=2`、`active_subject_id=child`、`minor/under_14/student_minor`，签名复核通过；`owner_display_name` 不跨主体；无同意时 `memory_retention=ephemeral_only` 且能力门全 False；旧 epoch profile 在权威里判 stale；crisis 话术逐字交付并把通知只绑孩子与声明监护人（`declared_guardian_count=1`，`active_link` 仍为 None）。**区分度已证**：把 `_account_keyed_memory_is_subject_scoped` 临时改成恒真后，已获 retention 同意的孩子那一轮真的取到账号主人记忆（`我在杭州读过书。`）而失败，还原后通过。同一文件现有 10 例（DSN 门控），含 30dea92 的权威 unavailable/过期、错绑管理账号与并发切人矩阵，以及 a65b8f2 的四类别矩阵与真实 catalog 账号/主体键隔离。仍未验：设备 admission、主体切换真机与受控危机听感；本地账号/监护声明平面仍是 SQLite，绑定与主体权威在 PG。 [2026-09-17（晚二）: the category matrix is now closed on the real catalog and real PG authority; only old-cache replay beyond switch/epoch races and device admission remain under P0-04.]
- 选人入口一致性（P1-03）：`resolve_subject` 不再广告写接口不接受的 `voice_question`，只返回 `app_confirm`；`test_advertised_subject_confirmation_methods_match_the_write_api` 断言广告集合、`app_confirm` 被接受、`voice_question` 仍 422。
- 数据主体（P0-04）：`interaction.py` 的记忆上限改为同一 person id 同时提供类别与 `memory_retention` consent；`session-policy` 改按签名 RuntimeProfile 的 active subject 判定并把 `owner_display_name` 限定账号本人当轮；`response_plan` 在 subject≠account 时不再读账号键旧档案记忆（该表把第一人称 claim 全存成 `subject_key="self"`），并补了正/反向回归。会话记忆迁到 subject 键的 `services/memory_scope` 仍未做。
- 不可见降级（P0-04）：当前使用人权威"配置存在但读取失败/过期"现在记日志并保持保守能力门（不回落账号资料、不猜 minor、不通知家长），公开固定安全回复保留；离线无该权威的部署仍按账号即使用人。正常 unknown/guest 与读取失败仍分别有测试。
- 验证边界（既有收据）：新增 3 条 PostgreSQL/RLS 契约（identity 声明、parent_for_child 仍要求关系、guardian 声明通知）已在真机外的真实 PostgreSQL 17.8 容器实跑通过，仍由 CI 的 `python` job 持续覆盖。独立孩子用例历史上用手工注入的签名 RuntimeProfile；现在真实 API→设备取 profile 与真实 persistent Runtime→两条策略入口都已补测（见上一节与 `test_persistent_runtime_policy_chain.py`），该收据覆盖链路的设备 admission 与真机听感仍未验；本轮新发现的成员写入与读一致性缺口见顶部，不作“所有软件已闭环”结论。
- 真实 exporter 隐私门禁（P1-01，2026-09-16 第四轮，本地代码+测试）：新增 `services/agent/tests/integration/telemetry_pii_probe.py`（无 pytest 依赖，镜像内可直接 `python -m services.agent.tests.integration.telemetry_pii_probe`）与 `test_telemetry_pii_real_exporter.py`。每个用例起全新解释器，装真实 OTLP/HTTP exporter 打本机回环采集器，再用 SDK 的 gated 写入点写 canary，同时断言解析后的属性与落线原始字节。实测双开关语义：`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 在 import 时读取、未设置/空值即为开；`LIVEKIT_TELEMETRY_ALLOW_PII` 决定内容能否到 exporter、未设置默认开而 bootstrap 强制为 0；只打开 capture 时 bootstrap 仍关掉第二个开关（纵深防御）。5 例含正例（两个开关都显式打开→canary 出现）与反回归（跳过 bootstrap→canary 出现）。探针已接入 `scripts/verify_agent_release_artifact.py` 的新步骤 `_check_real_exporter_privacy`，三条构建路径都 `COPY services`，构建期/CI `agent-image` 与 `python -m scripts.verify_agent_release_artifact` 都会跑真实导出，`scripts/tests/test_verify_agent_release_artifact.py` 通过。
- 发布实跑（P1-01，2026-09-16 本机 Docker Desktop 29.7.2）：`infra/Dockerfile.agent` 全量构建成功，构建期 gate 通过；对构建出的镜像按运行用户离线复跑同一 gate 通过（exit 0）。构建暴露一处真缺陷：`COPY` 保留宿主文件模式，umask 077 的检出会让源文件与 gate 脚本变成 0600，非 root 运行用户读不到——三条构建路径现都显式 `--chmod=0644` 并在 COPY 后统一放宽，镜像不再依赖构建者 umask。`resolve_target_images.py` 在真实 compose + override 链上演通过（候选命中 exit 0；缺候选 override / 旧镜像后置覆盖 → exit 1；未给候选身份 → exit 2）。
- 发布（P1-01）：三条 Agent 镜像路径（全量 / delta / source overlay）现在 COPY 并 `RUN /app/.venv/bin/python -m scripts.verify_agent_release_artifact`；`deploy_agent_component.sh` 随候选上传并校验 `resolve_target_images.py` 的 sha256，在写回滚点之后、切流之前强制解析"有效 stack tag + release commit + 预期 candidate tag/image + 真实 override 链"，失败即停止且不触碰在线栈。`resolve_target_images.py` 必须显式给出候选身份（缺省/无效 tag/缺服务/不一致/仍指向 stack 镜像分别拒绝）。CI 新增 `agent-image` job 与显式采集 `scripts/tests` 门禁测试的步骤（此前 `testpaths` 不含 `scripts`，9 项定向测试默认不会被任何 pytest 运行收集）。真实 exporter 已补（见下一节）：verifier 现在同时跑 InMemory canary 与真实 OTLP 导出探针；当前基线的镜像门已由上述 CI 刷新；后续修复候选仍须重新构建/复跑，生产切流与回滚演练需要生产授权与目标主机。

以上是既有实现收据与待验边界；实现入口、顺序与完成条件只维护在 `TODOLIST.md`，不在此展开修复历史。

## 当前生产与紧邻回滚

### Agent / Voice Core Media Bridge

- 最后收据：2026-09-16 11:36:08 CST，两个容器同镜像、healthy、restart=0。当前 `memoria-agent:20260916-livekit-181-v1`；image `sha256:7033214ddc3a5f4b0f99e016d1edf156b091169d6511ee95139e81f6ec0a9ac4`；revision `d96d4c29b7f13719d052b94647b2c59223ea70a1`。
- 紧邻回滚：`memoria-agent:rollback-20260916-livekit-181-v1-pre`；image `sha256:3f746b4dede8b2f35773ca5b2c6300174a278932eee7d720f4904c00d2b865bb`；revision `1cf8decaee8b28aa73d104f7aea89086e942db66`。本次升级后的实际回滚演练未做。
- 当前为 delta 构建，不是仓库标准全量可复现构建。远端收据 `/opt/memoria/component-releases/20260916-livekit-181-v1/CUTOVER_RESULT.txt`；本地 `outputs/acceptance/run-20260916-livekit-181-deploy/` 内的 `CUTOVER_RESULT.txt`、`report.md` 与切流前后 overrides 均存在，本轮已核收据的 tag/image/revision 与上述记录一致，但未重新核验实时容器；标准全量构建与本次回滚演练仍未完成。
- 独立构建依赖底座（不是第二个业务回滚）：`memoria-agent-runtime-base:uv-c34f031b4a40c7a7-af6e83d18883`，别名 `memoria-agent:20260912-p0-rollback-single-tag-agent-component`；image `sha256:3d46ca183984c2e5e7fd5f06e62b2eac6660049c637d1e9177d5c6874741364a`。删除前必须核依赖，不按旧业务版本误删。
- 11:37:57 CST readiness 的 12 个具名 core 检查与 heartbeat 均 ready。既有 LiveKit smoke 通过；provider smoke 首轮 InterruptSemantic 超时、重试通过。双进程实际隐私环境/exporter 仍待核：Compose 声明或本地 helper 不证明当前容器生效。
- 锁文件：agents/openai/silero `1.8.1`、RTC `1.1.18`、API `1.2.1`、protocol `1.1.26`、local-inference `0.2.7`。研究中的旧 1.6.10 基线不再适用，1.8.2 仅为研究项，不自动再升级。

### Media Edge / Control API

- Edge 当前 `memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`，override `/tmp/media-runtime.override.yml`；回滚 `memoria-media-edge:20260901-0945-wake-word-whitelist`，配置备份 `/tmp/media-runtime.override.yml.pre-20260908-1600-vocat-interrupt-assist`。watermark close-frame 4002 修复仅在代码，未有发布证据；控制帧入队后 read-loop 提前销毁 lane 的通用问题仍待查。
- Control 当前 `memoria-control-api:20260911-subject-switch-device-notify-control-api`；image `sha256:2af29dde2e8c6f1f1781bef322fe3b8d7f3b5ea13d3017b191aa443aa58bcebd`；overlay 源 `b2644124e002d1aac60b1b5a32c336c327af57bc`，仅覆盖 `device_control.py` 与 `multi_subject.py`，不代表当前全量 Control 已部署。
- Control 回滚 `memoria-control-api:rollback-20260911-subject-switch-device-notify-control-api-pre-control`；image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`。收据 `/opt/memoria/component-releases/20260911-subject-switch-device-notify-control-api/`。
- 主体变更→profile 版本→`runtime_profile.invalidated/apply_at=next_safe_point` 已接代码/线上 overlay；真机换人未验。设备 settings_version=12、`audio_mode=interrupt_assist`；签名 `allowed_barge_in=["button","keyword"]`，不含 voice。播放中语音告别不能按“播后告别通过”外推。

### 其它组件

- SenseVoice：`memoria-sensevoice-asr:20260901-pin-language`，默认 zh、未知语言 415；回滚 `v1` + `/opt/memoria/sidecars/sensevoice-asr/run_sensevoice_asr.py.rollback-20260901-prelang`。Dockerfile 仅在服务器该目录，尚不可由仓库重建；仓内实现使用 sherpa-onnx，不能假定升 FunASR 就能修此服务。
- 对话/分类/深查最后配置分别为 `qwen3.7-flash/qwen-flash/qwen-plus`。声纹 profile `1b5b577b` 为 active，CAM++ anti-spoof 为 unavailable；不宣称主人认证已验证。克隆唤醒听感有旧证据，天气正文音色绑定待听测。
- 小程序仅记录开发版 `0.8.84`（2026-09-06，源 `7e5137a`）；手机/电脑验收、体验版和正式发布均未完成。

## 板卡与固件

当前硬件 ESP-VoCat N32R16，`board=memoria-esp-vocat`；ES7210 双麦 + ES8311 输出，36dB 输入增益。hello 声明 simultaneous capture、software_post_gain_pre_i2s reference，但 `aec_reference_verified=false`。旧 ATK ES8388 已退役。

- 板上源码 `d1ad38fe0d9b36eb9057d8eb97e782b8cc8066d9`；upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`；ESP-IDF 6.0.2；overlay `24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3`。
- 当前 app 3,280,832 bytes，SHA256 `7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65`；ELF `da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2`。2026-09-15 18:51 CST 匹配启动；仅 app-only `0x20000`。
- 证据目录 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/`：`postflash.json` 是不可变刷写收据（其中 `boot_verified=false`）；实际启动独立记录在 `boot-verification.json`，五代短播放与人工听感在 `session-1/audit.json`。不得回改原收据或把短播放软件队列计量称为 DMA/长稳验证。
- 唯一紧邻回滚为该目录 `rollback-app.bin`，3,280,512 bytes，SHA256 `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`；它来自 dirty 构建，以实际二进制为准，不能仅靠旧 HEAD 重建。
- 同目录 `protected/app-before-full-slot.bin` 为写前 `0x20000/0x3f0000` 全槽，SHA256 `cc175040af934575b7804ec01031ebf8543415c98c0084801682e8ce121da333`。身份区 `0x10000..0x1ffff` SHA256 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`；身份/NVS/otadata/bootloader/分区表/assets 均须保护，禁止输出身份内容。
- `pre-roll` 未实现。固件声学、AEC residual、双讲、Exact DAC 与 T1–T14 仍未通过；普通制品清理须在新候选和可运行回滚核验后另行执行，不因精简文档删除实体证据。

## 当前语音缺口与下一验收

原始证据：`outputs/acceptance/run-20260916-p0-03-livekit181-device-acceptance/findings.md` 及同目录五段捕获。八种会话 A–H 均使用上述板卡与 1.8.1 线上版本；原始文件保留，本节纠正其中超出证据的归因。

- H（epoch 1963 / session `de598a18`）同会话完成三天天气→续问→播后告别，但 ACK→正文 `1.969s` 超过 1.5s 门，设备 VAD end→首帧 `4.557s`；A 第二问为 `3.748s`。功能成功不等于时延稳定。
- F 的九天天气 `48.28s`、设备 2413 帧、supply_waits=0，操作员确认完整；C 的 `44.18s` 也完成。B/D 桥侧音频长度 `58.88s/57.46s`，但设备在开始后数秒就停滞，1.5–1.6s 处先有 `366ms/412ms` supply wait，speaking/控制台冻结直到复位。尚无“约 55s 阈值”证据；Edge drop=0 也不能排除 WS writer、网络、接收/解码/播放路径。
- B 在 `20.23s` 挂钟触发旧 TTS 总超时，错误终态又延后约 `38.6s`；D 未见同类 TTS 错误。已有本地部分音频故障注入通过有界 CANCEL_GENERATION/ERROR/权威 listening 断言（见上方软件收据），但设备实际 cancel/flush/退出 speaking 的时序未复验，仍不能解释 B/D 两次停滞。
- 历史 G（epoch 1962）ASR final 先于迟到 VAD，受理时静默预算 0，无新 turn，最终 owner_silence_timeout。静默预算三态与迟到关闭已有本地修复和区分度回归，当前候选未复跑 G 真机，完整 10s/60s 交接矩阵仍待补；不再称修复前复现为当前 HEAD 失败。E/F/H 播后告别成功，C 的迟到告别失败；A 无有效告别输入，G 未到告别步骤，不计算“3/5 成功率”。

下一次设备窗口先确认已冻结并实际启用目标候选；执行顺序及修复前置见 P0-03/P0-04/P1-01：

1. 开串口可能复位，先等 `activating→idle` 和心跳再讲话；唤醒词“茉莉”。无人配合或设备未连接时只做离线检查，不自行刷机或播放自动代测。
2. 同一候选重跑三天天气→续问→播后告别，至少三轮；另测 >45s 长答、B/D 同类长答、临近静默续问，以及已下发部分音频后 provider 失败。每项分别判定功能、时延、终态、听感，不跨 release 累加通过数。
3. 同时取 Bridge/Edge WS writer/设备接收、解码、播放消费及任务/锁状态，绑定 session/stream/turn/generation/tool fence。保留 supply/prestart/boundary/close_dropped/outside、delivery ledger、指标差分、PCM RMS/削波；缺观测先补观测，不先加预缓冲或改阈值。
4. 功能口径（2026-09-17 晚四确定，本阶段只验功能）：能对话、能打断（button/keyword 按签名放行范围，中断后有界退出并回 listening）、长时间对话稳定、每次对话内容汇总可查（双方话轮与汇总以服务端日志/会话记录为准，操作员可读；无可读出口记缺口、不算通过）。不手工注入 profile，不绕声纹/准入门；HTTP 文本正确不是设备已说出，以终端回执与人工听感为准。学生危机场景的设备交付与通知 outbox 链（app_confirm 确认使用人及年龄、固定话术逐字交付、终端回执、outbox 绑定/幂等/家长读取）defer 到安全专项窗口，软件矩阵证据保留；通知发送 worker 按用户决定暂缓。

使用新目录，绑定板上收据（不是本次重新回读的证明）：

```bash
uv run --no-project --with pyserial --with esptool scripts/voice_session_capture.py \
  --out "$CAPTURE_DIR" \
  --firmware-receipt outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/postflash.json \
  --server-logs --duration 900
uv run python scripts/voice_session_report.py "$CAPTURE_DIR"
```

`--preflight-only` 不触碰设备；`--boot-reset` 仅在明确需要时使用。捕获必须有结束时间、逐路退出原因、无未解释 serial/cleanup error；缺终态不能报 healthy。Actual Heard 需设备终端证据和用户听感共同确认。

## 唤醒词（P2-05）设备证据

- 2026-09-16 原始矩阵 `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/` 保留不改写。Tingting 合成 TTS + 0.25s/0.4s 静音垫可唤醒该板；系统输出静音与部分 voice 空渲染会伪造零召回，工具已有校验。gain 1.0 为 4/8，gain 0.3 为 8/8，合计 12/16；gain 不是测得的近/远距离。
- 首次 activating→idle 后，四次失败刺激分别在约 7.35/17.54/27.71/37.98s，首次成功约 49.59s。单次启动、先高后低的播放顺序不足证明固定 45–50s 预热，后 12/12 是事后子集；60s 默认等待仅实验参数。成功刺激起点→唤醒行中位 1.445s 含前导静音与采集时序，不是精确声学延迟。
- 去重后新唤醒事件为 0，原 TV=1 为上一试次滞后重复；但 TV 133.038s 内 idle=0，约 92.382s 为 speaking/KWS off；small_talk 124.471s 内 idle≈100.909s，quiet 300.001s 均为日志可见 idle。不能据此报电视/多人各两分钟有效零误唤醒，配置日志也不自动证明整窗 detector 持续启用。
- `092dcf4` 已有门控/去重初修，仍存在本轮复现的曝光高估与 fixture/CI 缺口（见 P2-05）。先修工具再按多次冷启动、随机/交错增益、真实音源/物理距离预定义协议采数；本轮无设备新数据，不改 `advertised_duplex_level` / `aec_reference_verified`。

## 屏幕表情：对话脸

当前是 360 圆屏黑底白描 v3；`code/wired/enabled` 有既有证据，`hardware_verified=false`，仍缺待机脸和五表情照片。权威几何在 `memoria_face.cc`；预览 `outputs/firmware-face-v3-20260909/sheet.png` 只作对照。

| 助手实际表达 | 期望表情 | 照片 |
| --- | --- | --- |
| 待机/回复结束 | neutral 月牙眼、短平嘴 | idle.jpg |
| 祝贺、开心 | happy | happy.jpg |
| 关怀 | loving | loving.jpg |
| 遗憾、抱歉 | sad | sad.jpg |
| 惊讶 | surprised 杏仁瞳孔、小 O | surprised.jpg |
| 思考 | thinking | thinking.jpg |

以同代 `assistant_expression→screen.expression` 和串口 `emotion` 为准，不从用户原话猜脸；未知表情回 neutral。照片连同 fence 存入本次 ignored 验收目录。说完/断线/中断清除表情；待机点屏、摇晃不开麦，短拍只短暂惊讶；说话中 BOOT/触摸硬停。不回归项和六张照片均亲眼确认后才签收；回滚只用上节唯一 app，不再保留旧表情专用回滚命令。

## 生产拓扑与安全边界

- `/opt/memoria/current` 最后指向 `/opt/memoria/releases/20260827-architecture-split-v1`；有效栈 `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。目录名、栈 tag、组件 tag 是三个概念；readiness 刷新必须取有效栈配置。
- Control/legacy mini/legacy device/Direct Edge 仅回环端口 `8791/8792/8793/8794`；当前 Bridge 容器 `memoria-voice-core-media-bridge-1`。PostgreSQL 17 + pgvector、MinIO、独立 mTLS Redis；LiveKit server `1.13.5`。SQLite 兼容库 `/data/memoria.sqlite3` 挂载自 `/var/lib/memoria`。
- readiness 入口 `https://aigcnice.com:8443/memoria-api/health/ready`；443 根站是 WMS，不用该端口的 404 判断 Memoria 健康。
- ESP32 Direct：`wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`。公共 8080 不承载设备 WSS。H5 `/memoria-h5` 固定返回 `410 Gone`，不再发布静态前端。
- `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 是已退役的原生小程序媒体兼容回滚入口，只能用于明确的 legacy 回滚，不接回小程序产品；443/8443 Nginx 保留以下 include，不改同机 WMS 路由/数据。

```nginx
include /etc/nginx/snippets/memoria-miniprogram-media.conf;
```

Secret 仅在 root-only `/etc/memoria-*.env`（root:root 0600）；候选从真实源复制并按 `scripts/split_production_env.py` 分流，禁止在输出/日志/manifest 留值。内部 token 不等于账号身份；设备/LiveKit token 必须短期且绑定 audience/subject/fence。Direct 缺少 mTLS Device State Redis 时 fail closed，不回退本地权威。

## 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. 在候选镜像核依赖版本、双进程隐私默认值与真实 exporter；镜像解析显式给 expected candidate 并对齐有效 overrides。已有自动门禁接线，当前候选/生产复验要求见 P1-01。
3. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
4. dry-run→上传校验→授权切流→候选 provider/LiveKit smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片；`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

运行门禁需剥离本地 `LISTENER_CUES_ENABLED/LIVEKIT_ADAPTIVE_INTERRUPTION/OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

## 完整制品上传与校验

构建机生成 portable verifier 和绑定 source/images 的 manifest：

```bash
uv run python scripts/package_release_verifier.py \
  --output "$ARTIFACT_DIR/release-verifier.pyz"
uv run python scripts/create_release_manifest.py \
  --release-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --output "$ARTIFACT_DIR/release-manifest.json"
for artifact in source.tar images.tar release-manifest.json release-verifier.pyz; do
  (cd "$ARTIFACT_DIR" && sha256sum "$artifact")
done
```

`images.tar + images.tar.sha256` 必须成对上传。先 dry-run，再 seeded upload；不要对 basis 使用 `rsync --inplace`，失败不得污染不可变基座。

```bash
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG" --dry-run
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG"
```

构建机可信摘要须经已认证运维通道提供；先验 verifier/manifest，再运行 verifier，成功后才解包：

```bash
: "${MEMORIA_RELEASE_VERIFIER_SHA256:?required}"
: "${MEMORIA_RELEASE_MANIFEST_SHA256:?required}"
printf '%s  %s\n' "$MEMORIA_RELEASE_VERIFIER_SHA256" "$UPLOAD_DIR/release-verifier.pyz" | sha256sum -c -
printf '%s  %s\n' "$MEMORIA_RELEASE_MANIFEST_SHA256" "$UPLOAD_DIR/release-manifest.json" | sha256sum -c -
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --verify-imported-images
tar --extract --file "$UPLOAD_DIR/source.tar" --directory "$CANDIDATE_DIR"
```

不允许手工 retag 未绑定 manifest 的模型镜像。切流后运行 `scripts/smoke_server_deployment.sh`、真实 provider smoke、健康/私有 readiness/外部路由和延迟复核。

## 数据层、备份与恢复

仅在需要启动/恢复且获授权时，先创建 runtime 共享网络，再从真实目录启动数据层，避免异地工作目录建错卷：

```bash
DATA_COMPOSE_DIR=/opt/memoria/current/infra
cd /opt/memoria/current
docker compose -f docker-compose.production.yml create --no-build
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" up -d
```

- 用户 2026-09-14 决定验证阶段暂缓自动备份/异地副本；最后核查 offsite profile 未启用、无真实 endpoint/告警。真实家庭数据、正式发布或价值/量级增长前必须重评，不把同机副本称为异地灾备或已验证 PITR。
- 当前保留的本地还原点：`/var/backups/memoria/drill-20260914-p0-02/base`；2026-09-14 通过 `pg_verifybackup`、隔离启动、应用表/对象核对。报告 `outputs/acceptance/run-20260914-p0-02-restore-drill/report.json`。另保留 `/var/backups/memoria/20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump`。它们不会自动更新。
- WAL 最后观察仍归档且无自动裁剪；旧日增长估计/磁盘余量不是当前值。P1-08 单独处理保留策略，不因备份暂缓而遗漏。禁止只按文件年龄删 WAL，必须保护仍保留 base backup 所需连续链；本轮未删任何数据。
- 重新启用备份时，已修的 pg_basebackup CLI 仍需真实部署验证；网桥复制受现有 pg_hba 限制，优先评估 `network_mode: service:postgres` 走 loopback。真实异地 endpoint/凭据与恢复演练须另行补齐。

恢复集合须含 PostgreSQL base/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest/回执。用 `scripts/run_offsite_restore_drill.sh` 在隔离环境校验备份、对象清单/哈希、外键和应用读取；不能拿缓存当权威。恢复不可变 evidence/claims 后，在 Control API 镜像中重建投影：

```bash
python -m scripts.rebuild_memory_projections --confirm-rebuild
```

## 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-all` 或误用旧 run 回滚件。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。
