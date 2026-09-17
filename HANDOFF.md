# Memoria 当前交接

更新于 2026-09-17。这里只保留当前运行基线、一个紧邻回滚、必要运维步骤和下一验收；完成过程与旧版本流水账已删除。唯一执行队列见 `TODOLIST.md`，后续完成项直接移出队列，不新增归档文档。

2026-09-17 复核文档、源码与提交，并修 P1-01 与 P0-04 的代码、测试与接线：修正真实 exporter 探针的仓库根路径（镜像门禁与覆盖率合并恢复），给 `guardian_of` 声明加上绑定范围约束，并完成无账号孩子的 person 级 consent 授予/读门/撤销闭环（见下）。全部改动在本机既有 Docker PostgreSQL 容器与普通 venv 上实跑（未构建发布候选、未连接生产、未打开串口、未连接设备）。下列生产/板卡状态均为标注日期的既有收据，不是本轮实时健康证明；操作前须重新核验。 [2026-09-17 fix round: repaired the account-less person-consent lifecycle on both stores (trusted subject context for grantor read/revoke under NOBYPASSRLS FORCE RLS, unbind-proof grantor revoke, idempotent replay, SQLite export) and cleared the P1-01 Mypy blocker; see the dated receipt below. Still no production, device or firmware access this round.]

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-17
reviewed_source_commit: a8ce4e1a43ff09b208fc0d75d24bae0439204839
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
current_work_order: vocat_interrupt_assist
code: committed_through_a8ce4e1
wired: existing_python_voice_core_and_signed_runtime_profile_authorities
enabled: last_recorded_agent_bridge_d96d4c2_and_board_d1ad38f_not_head
verified: scoped_receipts_only_voice_stability_and_student_device_loop_pending
guardian_declaration_scope: binding_scoped_owner_only_third_party_excluded
accountless_person_consent: person_scoped_grant_read_revoke_replay_unbind_revoke_and_export_verified_device_pending
production_readiness: ready_at_last_observation_not_refreshed_this_review
production_readiness_observed_at: 2026-09-16T11:37:57+08:00
student_safety_loop_verified: false
student_safety_local_scope: real_persistent_session_runtime_on_ephemeral_pg_with_sqlite_account_and_declared_guardian_outbox_and_real_pg_person_consent_gate
guardian_authority_evidence: parent_declaration_only_never_verified_link
guardian_declaration_notification_basis: deliberate_identity_declaration_not_consent
postgres_contracts_for_new_paths: executed_and_passing_on_real_pg17_ephemeral_container
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

`a8a0e43`（2026-09-16 16:12 CST）是上一轮已核对的本地与远端 `main`。CI `35072578098` 的 agent/python 通过；Edge、小程序、固件及镜像任务跳过，不能据此宣称这些制品通过。没有该候选上线的证据；最后记录的线上 Agent/Bridge 仍为 `d96d4c2`。 [2026-09-17 fix round reviewed 52241ed and a8ce4e1; CI 35184070037 failed at the Mypy step and the following jobs did not run; no candidate was built, pushed or deployed.]

The 2026-09-16/17 local work has since been committed (`e5f9d50`, `7c0ef48`, `ec56d9a`, `6ab16b9`, `77fc86a`, `e1878ce`, `e8571d3`, `52241ed`, `a8ce4e1`). The earlier "uncommitted local worktree" wording is historical only; delivery levels stay `code` unless a dated receipt says otherwise.

- 语音 TTS（P0-03）：空洞的 TTS 回归测试已删除，`services/agent/src/providers/generation_budget.py::GenerationBudget` 现在是两条 provider、四条路径（Doubao/CosyVoice × stream/batch）唯一的"首包预算 / 收到消息即续期的停滞预算 / `max(total*5, hard_deadline_s)` 硬上限"决策点，超时分类统一为 `first-audio-timeout` 与 `total-timeout`（批式首包后不再抛裸 `TimeoutError`）。新增 `DOUBAO_TTS_HARD_DEADLINE_S`（默认 180，生效上限取 `max(total*5, 该值)`，默认行为与旧内联 180 一致）。用例：流式续期（父提交 `a8a0e43^` 上以 `total-timeout` 失败）、真实停顿有界失败、40 分片持续进展仍被 0.5s 硬上限按 `total-timeout` 终止、批式续期与批式首包后分类、CosyVoice 批式续期；后四条在 `HEAD` provider 上均失败。`MockDoubaoServer` 新增 `pcm_chunks`（N 路分片输入）与 `chunk_count`（已发分片计数），`MockCosyVoiceServer` 新增 `chunk_delay_s`。
- TTS 重试语义（P0-03，2026-09-16 第四轮，本地代码+测试）：`synthesize_stream_text` 的重试规则已显式定义并与预算同址（`generation_budget.py` 的 `BeforeAudioError`/`AlignmentRetryError` 标记与 `retry_allowed`/`retry_may_change_voice`，未知错误 fail closed）。音频前失败（首包超时、连接/握手、供应商 pre-audio 错误）可重试一次并允许回落设计音色；音频已存在只缺时间戳时可重试一次但必须保持同音色；音频后的停滞（`APIConnectionError: total-timeout`）、供应商错误、PCM 连续性失败与时间戳失败一律终态，最多两次尝试，且失败尝试的缓冲被丢弃——部分音频不会作为整句返回或重放。本轮修掉一处真缺陷：CosyVoice 克隆音色在"音频后缺时间戳"重试时会被换成设计音色（`test_clone_missing_timestamps_retries_without_changing_voice` 在旧行为下失败、现通过）。`MockCosyVoiceServer` 新增 `stall_after_pcm` 场景；Doubao 侧保护用例证明音频后停滞只 1 个 session 且音色不变、音频前首包失败仍回落一次。
- 语音输出终态（P0-03）：首帧已下发后的 provider 崩溃、停滞（`MEDIA_OUTPUT_GENERATION_TIMEOUT_S`）与硬期限（`APIConnectionError: total-timeout`）三种故障已本地注入验证：`services/agent/tests/unit/test_media_output_partial_failure.py` 断言有界时间内（0.2s 停滞预算内）落地同一终态——一个后继代 `REALTIME_EFFECT_KIND_CANCEL_GENERATION`（设备据此退出 speaking 并 flush，source `output_provider_failed`/`output_timeout`）、交付账终态为 `ReplyDeliveryEvent.ERROR` 而非 `PLAYBACK_ENDED`、`provider_complete=false`、`interaction_phase=listening`、`assistant_speaking=false`，且旧代的迟到 ACK/ENDED 不改变权威 fence、终态与发射计数。这验证的是进程内故障注入；B 的真机表现（桥侧旧总挂钟 20.23s 出错、约 38.6s 后才错误收尾）仍需设备捕获核对。
- 语音续问（P0-03）：G 的静默预算语义已统一：`owner_silence_remaining_s` 的 `None/0.0/>0` 三态明确，结束已测预算记 `0.0` 而非 `None`；计数器改为 `owner_silence_activity_revision` 并收敛到 `_note_owner_silence_activity`；被受理的 ASR final 与已受理 VAD 一样可作为"已受理主人活动"失效仍在等锁的关闭，但只在仍有其它界时生效、且不刷新预算。`test_accepted_final_can_veto_a_parked_grace_close` 与 `test_ending_a_measured_budget_records_it_as_spent_not_unmeasured` 在 `HEAD` 上失败、当前通过；两条真实入口保护用例两版都通过。仍未做：真机复跑 G 取证、无验证说话人且无其它界时的关闭语义、endpoint/commit 乱序与 watchdog 交接的 10s/60s 完整矩阵、部分音频失败终态、B/D 设备停滞、时延门。
- 学生安全（P0-04）：`_primary_subject` 不再替孩子确认，`guardian_of` 保持 `pending` 且只记录家长一侧确认（evidence `guardian_declaration_v1:device_binding`）；伪造的 `establish_active_link`（含 `verified_via=wechat_identity`、合成 code hash、365d 到期，并会激活既有 pending link）已从 port 与两个 store 删除。危机通知改由调用方从 Identity 解析声明监护人以 `declared_guardian_ids` 显式传入，PostgreSQL 侧新增 `guardian_enqueue_declared_notification` 在库内复核声明；SQLite 直接插入。这是有意决定：单方声明是通知依据，但不是已验证监护，也不解锁 consent。
- 声明来源约束（P0-04，2026-09-17，本地代码+测试+真实 PG + authoritative gate）：`e1878ce`。此前任何已登录账号都能对任意已知 person id 发 `guardian_of` 邀请并自己 accept，`declared_guardians` 与 `parent_for_child` 的 guardian 角色都接受该单方行，于是第三方可进入孩子的危机通知收件人集合。现在声明必须同时满足：带设备绑定证据 `guardian_declaration_v1:device_binding`、在有效期内、且声明人是某个 ACTIVE binding 的 `account_owner` 而该 binding 把主体列为 `primary_subject`（即 `POST /v1/device-bindings` 的 `parent_for_child` + `subject_draft` 唯一生产写入形状）。`parent_for_child` 的 guardian 角色另要求声明人就是 binding owner，或持双边 ACTIVE `guardian_of`。存储层 `has_source_confirmed_relationship` 保持无范围语义（建绑定时尚无 binding 行，加范围会形成循环依赖）；绑定范围判断在 `IdentityService.declared_guardians`（服务层，含 validity window）与新增的 PG 授权函数 `identity_relationship_declared_for_binding`——后者被 `guardian_relationship_declared`（危机事件读取策略与通知读取策略共用）与 `guardian_enqueue_declared_notification` 同时调用，授权缺失即 fail closed。回归：身份层第三方自声明排除/撤销/过期三例（内存+SQLite 双参数）、HTTP 层真实 invite+accept 后仍被排除且拿不到 guardian 角色、PG guardian 契约新增绑定范围正例与「无 binding 的真实单方声明」反例。实跑：`services/{identity,guardian,control_api,session_runtime,policy,consent}/tests` 带 `MEMORIA_TEST_POSTGRES_DSN` 全绿，`scripts/tests/run_authoritative_postgres_gate.sh`（init + repeat-upgrade + verify）通过。交付等级为 `code`；通知投递通道仍缺席，未验设备端。
- 无账号孩子 consent 闭环（P0-04，2026-09-17，本地代码+测试+真实 PG + authoritative gate）：`77fc86a`。旧链路 consent 强依赖 `guardian_links`，无账号孩子无法创建/确认 link 导致 consent 永久 fail closed。新增 `PersonConsentRecord` 领域实体，在 SQLite 与 PostgreSQL 建立 `guardian_person_consents`（带 FORCE RLS、`guardian_controller_person_consents` 策略与 maintenance 数据治理统计）；提供 `POST/GET/DELETE /v1/guardian/minors/{person_id}/consents`，由活跃 `parent_for_child` 设备绑定拥有者控制；读门 `active_consent` 做 link/person 并集统一解析。端到端回归验证：HTTP 授予 `memory_retention` 后 `/session-policy` 解除 `ephemeral_only` 记忆天花板，撤销后恢复保守态；非拥有者 403；`/response-plan` 补齐 `unknown_safe` 模式下直接 200 且 0 条记忆项泄漏回归。交付等级为 `code`。
- [fixed 2026-09-17 a8ce4e1, local code+tests+real PG] Person-consent lifecycle and P1-01 type blocker: `GuardianStorePort.active_consent` is now the link/person union (mypy --strict 0 errors); person-consent single read and revoke run under a trusted subject context on the non-superuser NOBYPASSRLS PG role (cross-child/cross-parent denial, revoke replay, `active_consent` tightening, export/delete/remaining counts); the recorded grantor can revoke after an unbind while strangers get 404; grant replay is idempotent (same key/body returns the original record and evidence, a different payload is 409, concurrent replay converges, an evidence write failure leaves no usable consent).
- [fixed scope 2026-09-17, moved from TODOLIST] 立即修（P1，PG 撤销失效）：`postgres_store.py:get_person_consent/revoke_person_consent` 首次查询把 `guardian_subject_id` 设成家长 id，但 `guardian_controller_person_consents` 要求它等于记录的孩子 id。本轮隔离 PG、`rolsuper=false/rolbypassrls=false`、FORCE RLS 下实测授予与列表成功，家长 get/revoke 均 `GuardianNotFoundError`，失败后 `active_consent` 仍有效；HTTP DELETE 的前置 get 会映射为 404。让读/撤销使用可信 subject 上下文，保留 FORCE RLS，不用 superuser/放宽策略绕过；补跨孩子/跨家长拒绝、撤销重放及撤销后策略收紧的真实 PG 行为测试。
- [fixed scope 2026-09-17, moved from TODOLIST] 立即修（P1，解绑后不可撤销）：本地 SQLite HTTP 实测 grant=201 → unbind=200 → 原授予人 DELETE=403，存储读门仍返回有效 consent。原因是 DELETE 与授予共用 ACTIVE binding owner 前置门，而 person consent 没有联动失效路径。先明确解绑、过期、移除主体/换管理人时授权保留或终止的语义，并保证原授予人可安全撤销自己签发的授权；不得靠重绑设备或伪造 link 才能撤销。手工 profile 探针中解绑后 `/session-policy` 仍未恢复 `ephemeral_only`，仅证明该测试形态；真实 Runtime 的 profile 失效传播及在途会话还须补验。
- [fixed scope 2026-09-17, moved from TODOLIST] 同批补（P2，授权重试）：相同 body 与 `Idempotency-Key` 的 HTTP POST 首次 201、重放 409。路由每次重算 `granted_at`，store 对固定 consent id 做完整 record 比较而冲突；补同请求返回原授权与原证据、异 payload 冲突、并发重放及证据写入失败后的恢复，SQLite/PG 两端一致。
- [fixed scope 2026-09-17, moved from TODOLIST] 同批补（P2，导出遗漏）：SQLite `remaining_account_rows` 实测 `person_consents=1`，但 `export_for_account` 没有 `person_consents` 字段；PG 已有该导出。补 SQLite 导出与两端 grantor/subject 范围、真实非空行的导出/删除/剩余计数契约，不以空表键集合或只测删除替代。完整 subject 谱系仍归 P2-03。
- 记忆召回（P1-06）：固定集 16 例 recall@5 由 0.8125 升到 0.9375、nDCG@10 由 0.734 升到 0.859，`candidate_leakage`/`cross_account_leakage` 仍为 0、`extraction_recall`=1.0、p50≈0.72ms。三处改动：`RecallPlanner` 新增“避开/忌口/不能吃/别吃/注意别/过敏”标记 → “不喜欢/讨厌/不吃/忌口/不要”词表扩展；评测 adapter 改为与生产读路径一致（生产总是先 plan）；规则抽取器新增“家里人叫(她|他)X”别名句式（仅在唯一人物且非角色词时生效，并补了反例）；编译期为人物建立 search document（此前 person 只在 `person_aliases`，没有任何读路径会搜它），确认时随同事件投影提升，未确认人物仍被 confirmed-only 挡在外面。仅剩 `repeated-episode-campus-startup`（跨会话 episode 合并的产品语义未定，不抬分）。人物投影已同步到生产 `postgres_memory_catalog.py`，并补了 DSN 门控契约 `test_postgres_person_alias_is_recallable_and_status_gated`；本轮已用本机 Docker（`/usr/local/bin/docker`，Docker Desktop 29.7.2）起临时 `pgvector/pgvector:0.8.1-pg17-bookworm` 容器（宿主 55432），把全部 DSN 门控契约实跑通过；`scripts/tests/run_authoritative_postgres_gate.sh` 的 init + repeat-upgrade 门禁与带 DSN 的全量 `pytest services/ tests/` 都通过。实跑发现并修掉两处真缺陷（跨 schema 授权的安装顺序依赖、声明监护人被 RLS 拒绝写/读）。
- ASR 救援边界（P1-02）：`services/agent/tests/integration/test_funasr_rescue_audio_shapes.py` 用**合成** PCM 波形（非真实录音）钉住救援路径的判定边界——静音/室内底噪在本地被门禁拦下且厂商零请求、削波满幅按上行原始字节送判、超长段只送最新尾帧（内容精确比对）、三路并发 + 慢厂商降级为有界 `no_text`、在飞行中的救援发布 `now + 2.5s` 预算。仍未做：真实中文录音语料与 sidecar 的生产启动脚本/Dockerfile/模型摘要入仓（后者只在服务器上，本轮未连生产），故 P1-02 的「仓内输入可重建」仍不成立。
- 真实 API 路径（P0-04）：`test_accountless_child_profile_reaches_the_device_through_the_real_api` 用真实 HTTP 链（device-binding → session → resolve-subject → app_confirm 切人 → device runtime-profile）取到 account-less 孩子的签名 profile，并用决策路径同一 `verify_runtime_profile_payload` 校验 `active_subject_id/subject_category/age_band/service_mode` 与「仍无 active guardian link」；此前该链只有手工注入。
- 真实 persistent Runtime → 两条策略入口（P0-04，2026-09-16 第四轮，本地代码+测试+真实 PG 实跑）：`services/control_api/tests/test_persistent_runtime_policy_chain.py`（DSN 门控，3 例）把 `PostgresSessionRuntimeService` 接到真实 PostgreSQL（本机 Docker `pgvector/pgvector:0.8.1-pg17-bookworm`，宿主 55439），经真实绑定 → `/v1/sessions` → 权威 `switch_subject` 取到孩子的签名 profile，再驱动 `/v1/interaction/session-policy` 与 `/v1/interaction/response-plan`（不再注入 profile）：两条入口读到同一 `runtime_profile_id`、`session_epoch=2`、`active_subject_id=child`、`minor/under_14/student_minor`，签名复核通过；`owner_display_name` 不跨主体；无同意时 `memory_retention=ephemeral_only` 且能力门全 False；旧 epoch profile 在权威里判 stale；crisis 话术逐字交付并把通知只绑孩子与声明监护人（`declared_guardian_count=1`，`active_link` 仍为 None）。**区分度已证**：把 `_account_keyed_memory_is_subject_scoped` 临时改成恒真后，已获 retention 同意的孩子那一轮真的取到账号主人记忆（`我在杭州读过书。`）而失败，还原后 3/3 通过。回归 46 项（`services/session_runtime/tests/test_postgres_store.py` + 本文件）通过；`test_student_safety_loop`/`test_interaction_api`/`test_multi_subject_api` 带 DSN 全通过。仍未验：设备 admission、主体切换真机与受控危机听感；本地账号/监护声明平面仍是 SQLite，绑定与主体权威在 PG。 [2026-09-17: 'only device left' is too strong - the P0-04 software matrix (authority unavailable/expired, profile-session mismatch, legal-manager change, category matrix) is still open; the person-consent PG lifecycle is now covered, device admission remains pending.]
- 选人入口一致性（P1-03）：`resolve_subject` 不再广告写接口不接受的 `voice_question`，只返回 `app_confirm`；`test_advertised_subject_confirmation_methods_match_the_write_api` 断言广告集合、`app_confirm` 被接受、`voice_question` 仍 422。
- 数据主体（P0-04）：`interaction.py` 的记忆上限改为同一 person id 同时提供类别与 `memory_retention` consent；`session-policy` 改按签名 RuntimeProfile 的 active subject 判定并把 `owner_display_name` 限定账号本人当轮；`response_plan` 在 subject≠account 时不再读账号键旧档案记忆（该表把第一人称 claim 全存成 `subject_key="self"`），并补了正/反向回归。会话记忆迁到 subject 键的 `services/memory_scope` 仍未做。
- 不可见降级（P0-04）：当前使用人权威"配置存在但读取失败/过期"现在记日志并保持保守能力门（不回落账号资料、不猜 minor、不通知家长），公开固定安全回复保留；离线无该权威的部署仍按账号即使用人。正常 unknown/guest 与读取失败仍分别有测试。
- 验证边界：新增 3 条 PostgreSQL/RLS 契约（identity 声明、parent_for_child 仍要求关系、guardian 声明通知）已在真机外的真实 PostgreSQL 17.8 容器实跑通过（见 TODOLIST P0-04 的"同期已验"），仍需 CI 的 `python` job 持续覆盖。独立孩子用例历史上用手工注入的签名 RuntimeProfile；现在真实 API→设备取 profile 与真实 persistent Runtime→两条策略入口都已补测（见上一节与 `test_persistent_runtime_policy_chain.py`），未验的只剩设备 admission 与真机听感。
- 真实 exporter 隐私门禁（P1-01，2026-09-16 第四轮，本地代码+测试）：新增 `services/agent/tests/integration/telemetry_pii_probe.py`（无 pytest 依赖，镜像内可直接 `python -m services.agent.tests.integration.telemetry_pii_probe`）与 `test_telemetry_pii_real_exporter.py`。每个用例起全新解释器，装真实 OTLP/HTTP exporter 打本机回环采集器，再用 SDK 的 gated 写入点写 canary，同时断言解析后的属性与落线原始字节。实测双开关语义：`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 在 import 时读取、未设置/空值即为开；`LIVEKIT_TELEMETRY_ALLOW_PII` 决定内容能否到 exporter、未设置默认开而 bootstrap 强制为 0；只打开 capture 时 bootstrap 仍关掉第二个开关（纵深防御）。5 例含正例（两个开关都显式打开→canary 出现）与反回归（跳过 bootstrap→canary 出现）。探针已接入 `scripts/verify_agent_release_artifact.py` 的新步骤 `_check_real_exporter_privacy`，三条构建路径都 `COPY services`，构建期/CI `agent-image` 与 `python -m scripts.verify_agent_release_artifact` 都会跑真实导出，`scripts/tests/test_verify_agent_release_artifact.py` 通过。
- 发布实跑（P1-01，2026-09-16 本机 Docker Desktop 29.7.2）：`infra/Dockerfile.agent` 全量构建成功，构建期 gate 通过；对构建出的镜像按运行用户离线复跑同一 gate 通过（exit 0）。构建暴露一处真缺陷：`COPY` 保留宿主文件模式，umask 077 的检出会让源文件与 gate 脚本变成 0600，非 root 运行用户读不到——三条构建路径现都显式 `--chmod=0644` 并在 COPY 后统一放宽，镜像不再依赖构建者 umask。`resolve_target_images.py` 在真实 compose + override 链上演通过（候选命中 exit 0；缺候选 override / 旧镜像后置覆盖 → exit 1；未给候选身份 → exit 2）。
- 发布（P1-01）：三条 Agent 镜像路径（全量 / delta / source overlay）现在 COPY 并 `RUN /app/.venv/bin/python -m scripts.verify_agent_release_artifact`；`deploy_agent_component.sh` 随候选上传并校验 `resolve_target_images.py` 的 sha256，在写回滚点之后、切流之前强制解析"有效 stack tag + release commit + 预期 candidate tag/image + 真实 override 链"，失败即停止且不触碰在线栈。`resolve_target_images.py` 必须显式给出候选身份（缺省/无效 tag/缺服务/不一致/仍指向 stack 镜像分别拒绝）。CI 新增 `agent-image` job 与显式采集 `scripts/tests` 门禁测试的步骤（此前 `testpaths` 不含 `scripts`，9 项定向测试默认不会被任何 pytest 运行收集）。真实 exporter 已补（见下一节）：verifier 现在同时跑 InMemory canary 与真实 OTLP 导出探针；仍未验的是在本次候选镜像内的构建/复跑收据、以及生产切流与回滚演练（需要生产授权与目标主机）。

以上是本轮改动与待验摘要；实现入口、顺序与完成条件只维护在 `TODOLIST.md`，不在此展开修复历史。

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
- B 在 `20.23s` 挂钟触发旧 TTS 总超时，错误终态又延后约 `38.6s`；D 未见同类 TTS 错误。最新流式修复不能解释两次设备停滞，也未解决部分音频失败后及时 cancel/flush/回 listening。
- G（epoch 1962）ASR final 先于迟到 VAD，受理时静默预算 0，无新 turn，最终 owner_silence_timeout；本轮最新代码对照仍复现。E/F/H 播后告别成功，C 的迟到告别失败；A 无有效告别输入，G 未到告别步骤，不计算“3/5 成功率”。

下一次设备窗口先确认已冻结并实际启用目标候选；执行顺序及修复前置见 P0-03/P0-04/P1-01：

1. 开串口可能复位，先等 `activating→idle` 和心跳再讲话；唤醒词“茉莉”。无人配合或设备未连接时只做离线检查，不自行刷机或播放自动代测。
2. 同一候选重跑三天天气→续问→播后告别，至少三轮；另测 >45s 长答、B/D 同类长答、临近静默续问，以及已下发部分音频后 provider 失败。每项分别判定功能、时延、终态、听感，不跨 release 累加通过数。
3. 同时取 Bridge/Edge WS writer/设备接收、解码、播放消费及任务/锁状态，绑定 session/stream/turn/generation/tool fence。保留 supply/prestart/boundary/close_dropped/outside、delivery ledger、指标差分、PCM RMS/削波；缺观测先补观测，不先加预缓冲或改阈值。
4. 学生危机场景先按 P0-04 的正式 app_confirm 路径确认测试使用人及年龄，核对设备实际取得的签名 profile 与有效监护授权；不手工注入 profile 或绕过声纹/准入门。只由受控成人模拟话术，固定话术逐字交付、终端回执和人工听感、正确主体的通知 outbox/幂等/家长读取同时验；HTTP 文本正确不是设备已说出。通知发送 worker 按用户决定暂缓。

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

- 2026-09-16 设备窗口（本机外放 + 麦克风，开启串口会复位板子）：合成 TTS（Tingting + 0.25 s/0.4 s 静音垫）**可以**唤醒本板，此前“TTS 唤不醒”结论来自未加静音垫的渲染；系统输出默认 `muted` 与 `Flo/Eddy/Grandma` 静默空渲染是两个会伪造“0 召回”的本机坑，工具已加校验。矩阵 receipt `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/`（`receipt.json` + `analysis.md/json` + `console.log`）：近档 4/8、远档 8/8，但前 4 次漏唤醒全在开机待机后约 42–50 s 内、此后 12/12，故属预热效应；唤醒时延（刺激起点→唤醒行）中位 1.445 s（1.35–1.52 s）；按唤醒行重算的误唤醒：合成电视人声 0、多人说话 0、静默 5 分钟 0。刺激非真人、距离/角度未变化，仍不据此宣传家庭场景召回率，也不改写 `advertised_duplex_level` / `aec_reference_verified`；工具 `scripts/wake_word_matrix.py`。

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
2. 在候选镜像核依赖版本、双进程隐私默认值与真实 exporter；镜像解析显式给 expected candidate 并对齐有效 overrides。当前自动门禁接线缺口见 P1-01。
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
