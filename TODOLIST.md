# Memoria 优先级执行清单

更新于 2026-09-20｜主体隔离批次与四个 account→subject 迁移（`00dc059`，CI `35495932582`）及其按 subject 只读出口、persona 会话胶囊主体读路径、`account_deletions` operator 回执出口（`7f589f0`，CI `35496738878`）均已提交、推送并远端全绿；本轮读路径 PG 侧对等与 PG 全 saga 删除验证（`d2318e4`，CI `35501188784` success）同样已提交、推送、远端全绿。MinIO、真实 provider 与备份项仍开放。本文件只保留未完成事项、执行边界和验收条件；完成收据归 `HANDOFF.md`，过时探针、旧 CI 数字和重复修复流水账从本文件删除。已有编号不复用。

## 当前边界（不得越界宣称）

```yaml
enabled_release: d96d4c2
frozen_candidate: memoria-agent:b668960  # 仅本地构建验收，未启用
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
subject_scope_batch: code=已提交 / wired=应用读出口按主体过滤 / enabled=未启用 / verified=本地 SQLite 与临时 PostgreSQL 回归
account_to_subject_migrations: code=四项已提交（含 operator 执行入口与按 subject 的只读出口）/ wired=persona 会话胶囊按当前主体读投影、账号本人回落现表 / enabled=未启用 / verified=本地专测与真实 PG 契约
read_path_postgres_parity: code=已提交 `d2318e4`（CI `35501188784` success）/ wired=仅 operator CLI 可达，Control API 不导入迁移接缝 / enabled=未启用 / verified=真实 PG 契约（durable_subject/memory_scope 读 PG 真实行，archive 证据读走 `app.account_id` 上下文、无 account 且在 FORCE RLS 下拒绝；投影侧 PG 未建表时 fail closed，不回落账号键）
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. P2-03 剩余：读路径的 PG 侧对等已落地（operator `read --postgres-dsn [--account <id>]`，真实 PG 契约）；删除范围验证完成本地部分（PG 全 saga 行/对象/厂商桩/声纹 + 收据幂等）。仍未验/未做：真实 MinIO 版本删除（本地 Docker MinIO 对象写入不可用，需可用 MinIO 或生产环境）、真实 provider 删除（需密钥与授权）、备份「恢复后再删除」实现与期限声明，以及新发现的结构盲区（`memory_scope`/`session_runtime`/`policy_receipts_v2`/`identity_*`/`device_fleet_*`/`device_onboarding_*`/binding consent 不在删除 saga，`remaining_account_rows` 只统计含 `account_id` 列的表）。小程序读口核验结论：各读口读的是数据所在存储（控制库 `MEMORIA_DB_PATH` 生产即 SQLite，archive/persona/digital-self/personas/growth 走 PG），archive 读口按调用者本人主体过滤；成员主体读口需客户端会话上下文，属 P1-03/P1-05。生产/设备验收仍待授权。
2. 随后冻结同一候选并进入设备 live 窗口。当前只允许 preflight-only；开始需要机器人、串口或人工听感前先通知用户。
3. 生产切流、回滚演练和制品清理须另获授权；设备功能通过不等于学生安全或全双工通过。
4. P1-08 WAL 可独立只读测量；删除、重启、定时任务、自动备份和异地副本不在当前授权内。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 待完成：建后年龄资料与 `app_confirm` UI、guardian consent 决策接口、会话记忆按 subject 键迁入 `services/memory_scope`，以及安全专项设备链（身份/年龄→有效同意→准入或受限能力→固定话术真实交付→outbox 绑定/幂等/家长读回）。发送 worker/外部投递暂缓。
- 软件门：两条策略入口覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、同意撤销/过期/无权限、管理账号更换与切人并发；不能决定时 fail closed，且不得读取其他主体私密记忆。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code/wired/enabled/verified`；`student_safety_loop_verified=false` 保持到安全专项设备链通过。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/{identity,session_runtime,guardian}/`。

### [ ] P0-03 TTS、续问竞态、设备停滞与真实时延

- 待完成：G 的 owner-silence/endpoint/commit/watchdog 真实配置矩阵；B/D 的 Bridge→Edge→设备接收/解码/播放同 fence 证据；部分音频失败后的设备终态；ACK→正文 <1.5s 的链路拆分与达标。
- 设备窗口发现（2026-09-20，已启用候选 `d96d4c2`，收据见 `HANDOFF.md`）：**F1 owner-silence 待命过早（已按产品要求修复，待发布+设备复验）**——旧语义播后只沿用剩余预算（本例 ~4.4s）即待命，多轮续问接不上；现改为**任何被接受的主人话轮都重新给足整段窗口**、默认 `MEDIA_OWNER_SILENCE_TIMEOUT_S` 10s→30s、助手自发提示不得延长、明确告别立即待命；到设备仍需随全量发布（组件快车道因依赖输入漂移拒绝）。**F2 待命后再唤醒被拒（症状+病因已修，待发布复验）**——`barge_source_forbidden retryable=0` 曾终止整段会话并弹错；现：禁止源 barge 只拒绝交接话语权（不转发/不关连接/不发 session.error，`device_barge_ignored_total` 可观测），且 `playbackActive` 与 **fence** 绑定、**只由设备自己的 `button.stop`（本地 flush）撤销**；`generation.cancelled` 不清窗口（它携带后继 generation，既不匹配回执 fence 也不证明设备已停播，清它会 fail-open），残余情形保持 fail-closed。覆盖（可审计）：真实 `button.stop → CancelGeneration/SendStop 成功 → 窗口关闭`（barge 用例）、successor-cancel 保持打开、陈旧 fence 不清、终态关闭清窗口、忽略后恢复转发。第三轮 idle 已用日志证明是**点屏 #3** 触发、非 owner-silence。固件侧「播放期不发 `vad.start`」为可选加固（需刷机），voice barge 长期走 P1-07 签名授权。
- 设备验收：同一已启用候选完成天气→续问→播后告别至少三轮、>45s 与 B/D 同类长答、临近静默和部分下发后故障；补待机、五表情及点屏/摇晃/短拍/BOOT 不回归。F1/F2 修复后需重跑本窗口（含 3s/5s/8s 延迟续问边界格）。
  - 2026-09-20 进展：待命后**立即再唤醒**回归通过（同秒待命 → 2s 内重新唤醒并开新会话，无拒绝）；收据 `outputs/acceptance/run-20260920-rewake-after-standby/`。
- 2026-09-20 设备侧异常（非刺激引起，空闲期发生）：BMI2 IMU I2C 读持续超时刷屏；端口复位后两次 `abort() PC 0x4038acd6` → `RTC_SW_CPU_RST`（约 12s 后再起，随后自愈）。需硬件/固件侧单独排查。
- 2026-09-20 删除 saga 盲区·逐域设计表已完成（55 表 + 三清单，全量 `'/Users/monkeyin/.omp/agent/sessions/-projects-memoria/2026-09-20T07-44-59-919Z_01a0bdc6-880f-703a-98fa-1c60dcd48dc3/deletionDomainDesign.md'`）。**结构性事实**：① `memory_records`/`memory_status_events`/`memory_shared_votes` 与 `session_runtime_profiles`/`events`/`profile_receipts` 有**无条件 append-only 触发器**（`services/memory_scope/postgres_schema.sql:309-322`、`services/session_runtime/postgres_schema.sql:344-357`），任何角色（含表 owner）都**无法 DELETE**；② `memory_*`/`session_runtime_*`/`policy_receipts_v2`/identity 核心表/`device_onboarding_*` 对运行时角色**零 DELETE 授权**（`services/policy/postgres_receipt_schema.sql:357-369`、`services/device_fleet/bootstrap_postgres_schema.sql:422-456`）；③ `identity_delete_guard` 需 GUC `app.identity_account_deletion='1'`，但**全仓无代码设置**（`services/identity/postgres_schema.sql:568-603`），且 `services/identity/sqlite_store.py` 无 `delete_for_account`/`remaining_account_rows` ⇒ identity 删除**未实现**；④ **guardian 漏 `tutor_practice_evidence`/`tutor_commit_outbox`**（`services/guardian/postgres_store.py:1975-2054` 删除与 `:2056-2143` 计数均未覆盖，SQLite 同构），`verified_empty`（`services/governance/account_data.py:1124-1140`）对它们全盲；⑤ `device_fleet_*` 无 person 列，账户归属须经 `identity_device_bindings.account_owner_person_id`（`services/identity/postgres_schema.sql:137`）解析；`family_space_id` 是 memory 与 device_fleet 共用的共享空间键（共属行判定核心）。**结论：物理删除对多域不可行，需先定"封存/去标识化 vs 改授权+迁移"的语义**；唯一无需新决策的干净缺口是 ④。
- 2026-09-20 逐域 seal 契约草稿完成（17 表：memory_scope 8 / session_runtime 8 / policy 1）。**结论**：三域均无 `sealed` 字段；封存只能在"仅可 INSERT"下用 (a) 追加语义行（revoked/新 revision）+ (b) 既有可变列状态转移 + (c) 读层过滤 组合实现；**tombstone 不会移除明文**（`memory_records.payload`、`memory_shared_proposals.content` 明文且不可就地改写；policy 收据被声明为不可变审计记录）⇒ 契约必须把「访问撤销/读口过滤」「payload 去标识化」「保留原始审计」分成三种结果，**禁止**把 `verified_empty` 或"删除完成"表述成"已擦除"。另有四处**不可归属**面须显式报 `unattributable` 而非计 0：`memory_outbox`（无主体列）、`session_runtime_events`（`actor_id` 可空且无 subject 列）、`session_runtime_outbox`（无 subject 列）、`policy_receipts_v2.subject_id IS NULL`。
- 2026-09-20 **契约不可闭合的结论（采纳审计意见，覆盖上一行"全按推荐默认"对 D1/D2 的选择）**：在既有约束（封存/去标识化、不改授权、**不加迁移**）下，seal 契约**无法闭合**——① 可靠封存需要**单调、独立、且所有读口强制检查**的标记；可复用标记 `memory_status_events(status='revoked')` **不可靠**（后续合法状态事件会恢复可见性；草稿已记"封存判定必须独立于 status 的单调性"），故不能据此判 `verified_sealed`；② 四处无主体键面（`memory_outbox`、`session_runtime_events`、`session_runtime_outbox`、`policy_receipts_v2.subject_id IS NULL`）在不新增归属列的前提下**无法归属**；③ 明文残留（`memory_records.payload`、`memory_shared_proposals.content`）在不改写的约束下只能"读层不可见"，不等于清除。**因此只有两条路**：(甲) 允许**最小迁移**（各域新增独立封存登记表/列 + 读口 join + 计数口）才可能形成可信 `verified_sealed`；(乙) 不做封存，把"删除收据只覆盖可删域、残留明文与不可归属面为已知合规缺口"作为**明确结论**记录，不再以封存口径对外表述。**在选定前不实现半套封存、不改任何计数面/读口。**
- 2026-09-20 seal 决策待定（D1–D10，推荐默认在括号内；可整体回"全按推荐"）：**D1** 语义边界（**A 仅可见性封存**；B 含读层去标识需映射表；C 引入物理删除豁免违背零 DELETE 契约）；**D2** 标记位置（**A 复用现有列/事件**；B 新增 `*_subject_seals` 需三域迁移；C 只读层外部登记无持久审计）；**D3** family_shared 共属行（A 任一成员即整行不可读；**B 按请求主体视角部分封存**，需重写 owner-grant 分支；C 家庭决议）；**D4** session 主体归属缺口（**A 接受 events/outbox 不封存**；B 新增 subject_id 列需迁移且历史行无法回填；C 用 contexts 近似会污染计数）；**D5** 计数面落点（A 改 saga 加 `verified_sealed` 步；**B 保留 `verified_empty` + 独立封存复检清单**；C 塞进 `remaining_account_rows` 需触碰 `_delete_order`，本任务非目标）；**D6** 审计出口（**A 原样保留、只收窄可读者**；B 读层假名化；C 分档）；**D7** 发起权（A 仅本人；**B 本人+监护人+运营 DSR**；C 任何共属成员）；**D8** 在飞/重放（A 一律拒绝；**B 已签发且在有效期内允许完成**，需定义时延上界；C 允许重放返回去标识结果）；**D9** 下游传播（A 各域自负；B 跨域事件广播；**C 先在 `control_api` 读口加前置门止血**，注意 operator/导出仍会泄漏）；**D10** RLS 是否内建封存维度（**A 纯应用层过滤**，代价：绕过服务的 psql/owner 读仍见明文；B RLS 内建封存维度，与 D2(B) 强绑定；C 两者都做）。
- 2026-09-20 产品侧提醒（读口真相）：产品召回**不读** `memory_records`，而走 archive 目录（`services/control_api/app/routes/interaction.py:1719-1743`，account_id+subject_id）⇒ 仅封存 memory_scope 不会让轮次内容消失；operator 读口 `services/governance/subject_postgres_reads.py:249-300` 必须同步改。
- 2026-09-20 顺序纠正（审计意见，采纳）：**先出逐域 seal 契约，再改计数面**。理由：三类盲区没有统一的 `sealed`/去标识化状态或既有 saga 接口，memory/session 还有共享行与无条件 append-only 触发器、policy 仅有 `subject_id`/`actor_id` 读权限；用户只定了语义，尚未定每域的封存记录/字段、owner key、共享行处理、`verified_empty` 判定与审计留存。**在契约与决策点明确前，不改 `remaining_account_rows`、不把无 `account_id` 的域硬塞进 `_delete_order`。** 契约草稿见 `agent://sealContractDraft`（含必须由用户决策的问题清单）。
- 2026-09-20 **Slice A 完成**：`tutor_practice_evidence`/`tutor_commit_outbox` 已纳入 `guardian_tutor_account_scope_{delete,remaining,export}` 及 PG/SQLite 两个 store 的 export/delete/remaining；RLS 前置（`guardian_controller_tutor_evidence` 的 TO 增加 `memoria_guardian_maintenance`，保留 `allow_account_scope` 条件）+ 最小授权 `GRANT SELECT, DELETE`；测试含真 PG 的"另一主体行不受影响"与重复删除幂等断言。验证：`services/guardian` 31 passed（含 DSN-gated PG）、`services/governance` 47 passed、ruff check、mypy --strict、模块预算 PASS。提交 `6e853ef`。**仍待办**：identity（`identity_delete_guard` 依赖的 GUC 全仓从未设置、SQLite 无 delete/remaining 实现）、device_fleet/onboarding（无 person 列，须经 identity binding 解析归属）、以及 append-only/零 DELETE 域的"封存"计数面。
- 2026-09-20 用户决策：① 切片顺序——先补 **guardian 的 `tutor_practice_evidence`/`tutor_commit_outbox`**（删除+计数+导出同口径；PG SQL 函数与 SQLite store 同构）；② 物理不可删域（`memory_records`/`memory_status_events`/`memory_shared_votes`、`session_runtime_profiles`/`events`/`profile_receipts`、`policy_receipts_v2`）统一按**封存/去标识化**语义处理，并把计数面改为反映“已封存”而非“已删除”，**不改授权、不加迁移**。身份/设备域（identity/device_fleet/onboarding）留待后续专项。
- 2026-09-20 设备复验的前置（下次设备窗口用）：临时驱动必须修证据链——记录 `afplay` **起始**时刻与每次 prompt 唯一 ID，并用**同一 session/stream/turn/generation** 的服务端 `turn_committed`/`playback_ended` 做绑定；同时读取设备签名 `allowed_barge_in` 以分别闭合 q3/q5/q8 与被禁源 barge。在此之前 F1 维持“部分观察”、F2 维持“未验证”。
- 2026-09-20 删除 saga 结构盲区（勘探完成，待设计决策后实施）：saga 在 `services/governance/account_data.py`（`AccountDataGovernance._delete_account`，9 步 checkpoint + `account_deletions` 收据；入口 `POST /v1/archive/deletion-requests`、guardian 路由、后台 `AccountDeletionWorker`）。`remaining_account_rows` 判据＝"表在 `_delete_order` 中且有 `account_id` 列"（`account_data.py:490-491` SQLite / `:689-690` PG），因此 `memory_scope`/`session_runtime`/`policy_receipts_v2`/`identity_*`/`device_fleet_*`/`device_onboarding_*`/binding consent 这些**无 `account_id` 列**的域表既不删也不计数，`verified_empty` 看不见。**两个必须先定的设计问题**：① `policy_receipts_v2` 的角色被 REVOKE ALL 后只授 SELECT/INSERT（`services/policy/postgres_receipt_schema.sql:357-373`，无 DELETE 权限），删除要么改授权/迁移、要么改语义（如按主体封存而非物理删）；② 这些域表的键列各不相同（`subject_id`/`actor_id`/`person_id`/`account_owner_person_id` 等），需要各自的归属口径而非 `account_id` 单列。**建议切片**：先做"让 `verified_empty` 看得见"（把上述域表纳入计数面并按各自键列统计），再逐域实现删除；切片一一旦落地，未实现的域会**诚实**报为未清空（而非静默通过）。
- 2026-09-20：F2 **禁止源 barge 仍未验证**（撤回此前“通过”结论）。判据是 Edge 日志 `ignored barge from a forbidden source`（`device_ws_uplink.go:111` 触发时必然打印；三次采集均为 0 行），`device_barge_ignored_total` 在 mTLS 私有 `:8081` 上抓不到（明文 400）。复现需先读设备签名 `allowed_barge_in` 再构造会话内被禁源 barge；`button` 源只能在设备上产生（触摸面板/按键，需人到场或非生产 edge）。
- 2026-09-20：F1 在本窗口只取得**观察**，**未达“判据通过”**——会话跨约 8.2s/11s 仍有后续输出（旧语义 ~4.4s 预算下难以维持）；但 +3/+5/+8s 精确边界与“每个 prompt→generation”归属**未证实**（串口 21:39:38 的 `listening->speaking` 早于 q5 播放；脚本时间戳为 afplay 播完后打印），已撤回“F1 判据通过”的表述。可直接归因的只有“待命后立即再唤醒成功并开新会话”。**F2 禁止源 barge 仍未验证**（判据为 Edge `ignored barge` 日志，全生命周期 0 行）。
- 约束：保留现有 GenerationBudget、代际隔离和失败有界退出；不靠延长静默、重复整句合成、第二提示或放宽门禁遮掩问题。入口：`providers/{generation_budget,doubao_tts,cosyvoice_tts}.py`、`voice_core/media_session_{standby,output_stream}.py`、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与产品闭环

### [ ] P1-01 冻结修复候选，完成发布与回滚验收

- P0 收口后冻结同一 source/lock，跑 ruff、module budget、strict mypy、协议生成、真实 PG init/repeat-upgrade、全量 pytest/覆盖率、Offline E2E、真实 exporter 隐私门和受影响镜像门；DSN-gated skip 不能当通过。
- 用标准全量构建复验 Agent/Bridge 同一产物、非 root 运行与真实环境变量；按授权切流，复核 12 个 readiness、外部路由、provider/LiveKit、设备与时延，并实际演练紧邻回滚。
- 完成条件：同一候选的构建、切流、回滚、设备验收分别有证据；仅保留当前和一个可运行回滚，清理生产另授权。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧容量预测不再复用。
- 需用户决定并授权裁剪、停用 `archive_mode`（需重启）或手工阈值策略；不得按 mtime 删除或清理 `pg_wal` 代替归档保留。
- 完成条件：保护集合、容量/恢复影响、执行证据和后续责任明确，保留恢复目标可验证。

### [ ] P1-02 让 ASR 救援 sidecar 可复现并验证真实输入

- 待完成：把生产 sidecar 的启动脚本、Dockerfile、模型/词表摘要和基础镜像输入纳入仓库；用真实中文 PCM 验证短句/尾字、长段、低 RMS、静音、削波、并发、失败降级与 2.5s 预算。
- 约束：主链是云服务商 FunASR，救援后端单独核验；不把本地 FunASR PyPI、仓内 sherpa-onnx 或云模型版本混为一体，也不顺手改 NumPy/设备 VAD。
- 完成条件：仓内可重建，分报 vendor_error/vendor_silent/gating/low_rms；有收益且无回归后才另行切流。

### [ ] P1-03 修稳成员入口，再接按使用人切人格/音色

- 待完成：建后年龄申报 UI；guardian 邀请/授权与声音撤销的 consent 决策 seam；运行中会话的 `next_safe_point` 主动重协商与 device→session 映射；同 binding 两个 subject 的人格/音色设备实听。
- 完成条件：切换推进版本并使旧签名、上下文和音频失效；取消分配回落默认，克隆未 ready 回落设计音色；PG 与设备证据分开记录。入口：`routes/{identity_lifecycle,persona_assignment,custom_personas,multi_subject}.py`、`services/session_runtime/profile_service.py`、`apps/miniprogram/pages/{device,persona-custom,guardian,privacy}/`。

### [ ] P1-04 自定义声音：样本上传到设备出声

- 待完成：超过 60s 后区分“仍在处理/需人工处理”；接通 consent 配置门和 privacy 页撤回删除；取得真实 provider 克隆→分配→设备可听的端到端耗时与失败证据。
- 保持 `reconciliation_required` 为可恢复语义，不伪造 ready 或百分比；声纹/声音样本使用单独同意，未 ready 回落设计音色。
- 完成条件：录音→上传→真实解码→训练→ready/failed/超时→分配→设备实听，连同拒绝、取消、重放、撤销和样本处置全链通过。

### [ ] P1-05 补权威会话状态，再做小程序三端验收

- 待完成：P2-03 迁移及读路径切换、小程序手机/电脑/开发工具同版本验收、Edge→Control 受鉴权只读状态出口，以及生产/设备验收。
- 状态以 Python→Edge 的 `assistant_state.phase` 为源，带 session/generation fence 与新鲜度；重连、断线、过期显示 offline/unknown，不从字幕或客户端计时猜态。
- 完成条件：三端覆盖登录绑定、三态/断线、主体人格切换、样本进度、回顾、权限拒绝与刷新；体验版、提审和正式发布分别授权。

### [ ] P1-06 定义跨会话记忆语义，补未见召回评测

- 待完成：用真实 Qwen 抽取器验证别名与跨会话 episode（需要密钥和预算）；把会话记忆迁到当前 person/subject 键，覆盖切人、撤销、删除和旧缓存，不建平行存储。
- 完成条件：固定集与未见集分别报告 recall/nDCG/extraction/leakage，遗漏项命中正确证据且隔离泄漏为 0；验证实际 ResponsePlannerClient 超时和 catalog 限额。设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button/keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别/打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集/未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备/真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`agent.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-02 多成员声纹与不依赖小程序的选人

- 先确定识别人范围、单独同意/撤销和设备确认交互；owner-only 登记不能区分家人。
- 完成条件：逐人登记、冲突/不确定/访客隔离、撤销与审计有 PG 和设备证据；识别只提出候选，不升级为 owner 或敏感授权。

### [ ] P2-03 可证明删除与导出证据链

- 已完成（本地，收据见 `HANDOFF.md`）：四迁移读路径的 PG 侧对等（operator `read --postgres-dsn`，真实 PG 契约；投影侧 PG 未建表时 fail closed 不回落账号键）；PG 全 saga 删除验证（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）。
- 待完成：真实 MinIO 版本删除（本地 Docker MinIO 对象写入不可用，需可用 MinIO 或生产环境）、真实 provider 删除（需密钥与授权）、备份「恢复后再删除」实现与期限声明、结构盲区补齐（`memory_scope`/`session_runtime`/`policy_receipts_v2`/`identity_*`/`device_fleet_*`/`device_onboarding_*`/binding consent 不在删除 saga；`remaining_account_rows` 需覆盖非 `account_id` 键表。其中 `memory_scope` 的可行路径已核实：`memory_owner_records`/`memory_owner_status_events` 对 `memoria_memory_owner` 是 `FOR ALL`（`pg_has_role(...,'member')`，NOLOGIN，维护登录 `SET ROLE` 即可），RLS 层允许 owner 角色删，缺的是应用侧删除路径与清点，不是权限；开工前需先定：哪些行属于删除范围（`resource_owner_id` vs `subject_id`、`family_shared` 行的族空间归属、`co_subject_ids` 共同主体）、`memory_status_events` 先于 `memory_records` 的顺序、删除 fence 与收据/幂等语义。小程序成员主体读口（需客户端会话上下文，属 P1-03/P1-05）、生产/设备与真实机器人对话验收。
- 安全约束：不得默认 `account_id == subject_id`；必须有 Control 注册、active Identity person、owner evidence 和一致事件 subject；child/member 只接受唯一 lineage，foreign/inactive/ambiguous/NULL/mixed subject fail closed；源表与 `snapshot_json` 保持字节不变；rollback 只移除本 migration 行。
- 完成条件：PG、MinIO、投影/缓存和 provider 范围一致，重试幂等、回执可查询、导出标记 AI/授权/服务提供者；保留备份写明期限与恢复后再删除，不承诺即时物理抹除全部副本；真实 MinIO 与 provider 各需一次可复现收据。

### [ ] P2-04 协议故障注入与长稳观测

- 待完成：WSS 丢帧/乱序/重连、Bridge/Agent 退出、未知配置、迟到终端、profile 失效、并发/资源泄漏；查清 Go 侧控制/error 帧在 read-loop 关闭 lane 后丢失的路径；重跑当前候选 Go/Trivy。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行；预定义长稳窗口保留内存、连接和延迟趋势。先补测量/注入，不顺手改断线产品行为。

### [ ] P2-05 补齐唤醒计数，再采家庭噪声矩阵

- 待完成：固定候选/固件/settings，按物理距离、角度、输入电平、多冷启动和预定义稳态重采电视/家庭音源与真人对照；先核实际模型、阈值、状态和 detector 权威。
- 旧日志不足以证明电视/多人各 2 分钟有效零误唤醒，也不证明固定 45–50s 预热；错误不能算 miss 或零误唤醒。
- 完成条件：取得可比较设备 receipt 后才调检测阈值/词形；不修改 `advertised_duplex_level=none` 或 `aec_reference_verified=false`。

### [ ] P2-06 用可回放任务评测陪伴连续性与回顾质量

- 待完成：补学习挫败承接、隔次继续计划、本人/获授权管理人查看回顾等场景，交叉 under_14/14_17 与 retention 允许/拒绝，包含切人、撤销、模型超时和未见集。
- 逐场景记录任务接续、记忆证据、越权/编造、回顾可读性和失败降级；模型措辞需 recorded-bundle 人工盲评或设备窗口，离线生成不计真机成功。
- 完成条件：形成可重复 baseline 与同条件对照，主体/撤销隔离和编造事实零回归，汇总可追溯到话轮/证据。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：真实家庭服务/数据、正式发布或价值量级增长前重评；WAL 仅按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收；启用另定授权和渠道。
- EOU 新模型、DuplexModel、expressive/抢跑；ESP-IDF/ESP-SR/upstream 整体升级；老人故事册/人物复刻、年轻人潮玩和最终外形均不进入当前队列。

- 2026-09-20：F1/F2 已按组件发布上线（tag `20260920-f1f2-owner-silence-and-barge`，收据与回滚镜像见 HANDOFF）。下一步是设备上的 F1/F2 行为复验（问答 + 打断序列），以及在修好模拟音频工具的两处自检判据（live 缓冲窗口、参考波形匹配）后跑自动问答扫频。

- 身份收敛（发布治理）：control-api 期望的 release tag 应与真实发布 tag 一致，取消"agent 上报历史冻结 tag"的临时对齐；与预构建镜像入口一并作为 P1-01 输入。

## 研究重评条件（非开发队列）

- R-20260917-01 已转化为 P1-05/P1-06/P2-06；只有学生功能与安全链闭环、出现明确老人试点和一手交付/隐私证据时，才重评老年阶段。
- R-20260918-01/02 不改变当前桌面语音终端、学生优先、半双工和 P0 安全/TTS 优先级；只有独立验证的交付、效果、价格和长期可用证据出现时再重评硬件与市场定位。
