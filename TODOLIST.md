# Memoria 优先级执行清单

更新于 2026-09-30｜线上为整栈 `20260930-local-stop-v2`（源 `88a3c80`；bridge 镜像仍带 `livekit-agents`，#148 已合并未发布），media-edge 单独在 `20260930-late-receipt-v1`，control-api 单独在 `20260930-vector-keyword-v1`（回滚镜像 `rollback-20260930-vector-keyword-v1-pre-control`）；整栈回滚目标 `20260929-session-limits-v1`，media-edge 回滚目标 `20260930-edge-reject-log-v1`。固件 build 16 已合并，未发布 OTA。2026-09-28 的收尾待办见下方同名一节；发布与验收收据见 `HANDOFF.md`。本文件只保留未关闭事项。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 20260930-local-stop-v2  # 源 88a3c80；三角色为该 tag，其中 control-api 为 20260930-vector-keyword-v1、media-edge 为 20260930-late-receipt-v1；LLM qwen3.7-flash（联网查询 qwen-plus），ASR fun-asr-realtime，TTS Doubao；整栈回滚 *:rollback-20260930-local-stop-v2-pre（= 20260929-session-limits-v1），media-edge 回 20260930-edge-reject-log-v1
control_api_release_lane: 整栈走仓库版 `scripts/release_ops.sh`（安装在服务器 `/root/memoria-release/release-ops.sh`）；PREV 常量随每次发布 PR 更新；依赖文件（pyproject/uv.lock）变化时增量与 overlay 发布会被脚本拒绝，须本机全量 linux/amd64 构建；control-api 组件链支持已移除，单组件发布前须先补回
memory_candidate_visibility: code=main 0059368 / enabled=true（随整栈上线）/ verified=SQLite/HTTP/主体隔离/评测适配器回归；四份 2026-09-23 评测收据为上线前 parent_baseline（固定集 recall@5/10=0.857、未见集 0.4、双泄漏 0），真实 PG candidate 行为与线上带鉴权读口未单独取证
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
subject_scope_batch: code=已提交 / wired=应用读出口按主体过滤 / enabled=未启用 / verified=本地 SQLite 与临时 PostgreSQL 回归
account_to_subject_migrations: 已删除（2026-09-28 第 2 批）——四项账号→主体一次性迁移、operator CLI `run_subject_migrations.py` 与治理层迁移接缝从未在生产启用，人格已改为按使用人学习与读取（P1-03），不再有读路径依赖；表结构保留在各 schema，git 历史可恢复
read_path_postgres_parity: 随上一项删除（仅 operator CLI 可达，Control API 从未导入）
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga
conversation_archive: code=#151（成人）与 `feat/minor-memory-capture-receipt`（孩子，含 Policy 对齐）/ wired=Agent 归档闸门认已签名 profile 的 `memory_recall_private` / enabled=未上线 / verified=本地 PG 链路测试与单元测试；生产自 2026-08-08 起无设备对话入库，且设备信任为 `untrusted`（`device_fleet_attestations` 0 行）时 profile 不含该授权，两个分支单独上线都不会让归档恢复（根因与证据见 `HANDOFF.md` 2026-09-30「设备对话为什么没有入库」）
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. 真机窗口（用户推动）：设备已于 2026-09-28 重新绑定为「给孩子使用」并勾选长期记忆（`e8a27e45` v3，`growth_summary`），⓪ 已完成；当日真机项见「2026-09-28 收尾待办」。之后按 HANDOFF 验收清单验 P1-11 三种绑定、P1-03 孩子人格隔天生效、P2-04 终止性拒绝不再续连，以及 P0-03 的 TLS/WSS 重连与剩余设备矩阵。不得把核心通过扩大为完整 P0-03 或全双工通过。
2. 可直接推进的代码项：P0-04 按 2026-09-26 产品决定实现（进行中）、P1-02 救援 sidecar 可复现、P1-04 自定义声音闭环、P2-06 回放评测、P2-04 Python 侧进程退出注入。
3. 需用户决定：**设备信任**（最优先，见 P1-11：生产设备没有 attestation，Policy 对所有主体拒绝 `memory_recall_private`/`memory_capture`，设备对话因此不入库、也不会有隔天记忆）、P1-02 两项线上调整、旧媒体链去留（P1-12）、WAL 保留策略（P1-08）、readiness 逾期的告警渠道（P1-09）、P2-07 第 2/3 项、P2-03 已知缺口是否接受、P0-04 未成年人人格学习口径。
4. 边界：生产切流、回滚演练和制品清理须另获授权；删除、重启、定时任务、自动备份和异地副本不在当前授权内；设备功能通过不等于学生安全或全双工通过。

## 2026-09-28 收尾待办

当日工作的遗留事项汇总在这里（收据见 `HANDOFF.md` 当日各节）。处理完删除对应行；长期事项已并入下方原有条目。

**等用户在手机/机器人上做**

- [ ] 伙伴页选一次「绵绵」：账号伙伴仍是 09-25 的桃喜，绑定所选只写进了设备人格；#76 修复只对之后的绑定生效，存量数据未代改。
- [ ] 有背景声时（电视、音乐、旁人说话）问一个非天气问题（天气走实时查询快速通道，不经过这段逻辑），验证 #83：日志应出现 `media reopened turn committing without new text`，说完到开口约 3 s 内。09-28 15:18 的测试环境安静，窗口未触发。
- [ ] 唤醒一次机器人并完整对话一轮，确认 media-edge `20260928-writer-teardown-v1` 下设备能连上、能回答（17:26 切换前后都没有设备会话，重连尚无真机证据）。
- [ ] 确认公众平台「扫普通链接二维码打开小程序」规则（前缀 `https://aigcnice.com/memoria-bind/`）已点校验并保存（09-27 起的待办，文档里没有确认记录）。
- [ ] 需要时重新配网一次，真机验证固件 build 9 的「重试激活成功→重启进已绑定路径」（开发板当前 build 10，这条路径尚未真机触发）。

**需要用户决定**

- [ ] 固件 build 9/10 只 USB 刷了开发板，未签名发布 OTA；是否发布（`publish_firmware_release.py sign/upload --build 10`）。
- [ ] stash 存档 `outputs/stash-archive-20260928/stash2-realtime-scope-wording.patch`：联网查询隐私约束「只用本轮地点」→「本会话说过的也行」，当时未上线；是否作为产品改动重新评估。

**待查（有现场证据，原因未定）**

- [ ] `response_plan_cached reason=no_verified_runtime_profile mode=companion fallback=True`：09-28 下午 6 h 内 2 次。设备信任为 `untrusted`（无 Device Fleet 证明）是否使回复规划走兜底、影响记忆与个性化（`untrusted` 使 profile 不含 `memory_recall_private`，见 P1-11；此处兜底判定本身仍待查），需查 `services/agent/src/reply_pipeline.py`（`prepare_turn`）与 `agent.py`（`plan_matches_mode_policy`）的判定。
- [ ] SenseVoice 救援 `httpx.ReadTimeout`：09-28 04:59:40、06:23:46 各一次，都在空闲后的首个救援请求上，符合 P1-02 ① 的 swap 问题。
- [ ] 回复首音时延：09-28 追问一轮从提交到开口约 2.2 s（`qwen3.7-flash` 首 token + Doubao 首段合成），尚未拆分；用户暂缓埋点（归 P0-03 的时延项）。

**定期运维**

- [ ] **2026-12-17 前**更换 aigcnice.com 证书：腾讯云会自动续签，但「未托管、未关联资源」，新证书不会自动到服务器；需下载 Nginx 格式，原位替换 `/etc/nginx/ssl/aigcnice.com_bundle.crt` 与 `.key`，`nginx -t` 后 reload，核对 443/8443（09-28 流程见 HANDOFF）。
- [ ] 发布制品随发布累积：每次整栈在 `/opt/memoria/incoming/` 留约 3 GB，加载镜像另占数 GB；09-28 清理后根分区约 69%（118 GB）。约定一个保留规则（例如只留当前 + 紧邻回滚两批），按授权定期清理；仓库 `docker_image_retention.sh` 因保护全部 `rollback-*`/runtime-base 与 pre-state 引用而 0 候选，需要改或另写。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 产品决定（用户 2026-09-26）：未成年人不分档；只学表达风格；时长与夜间时段运行时强制（危机除外）；年龄以家长申报为准并可在设备页修改；年龄不明也允许学习辅导（不留存）；删「每周小结」勾选项；孩子与老人的危机提醒都推送（家长/代为同意的子女）；话术加 12356 并待专业审核。详见 `docs/compliance/p0-04-minor-safety-decisions.md`。
- 已实现（2026-09-26，已随 `20260926-minor-safety-v1` 上线，设备未验；各项边界见决策文档「实现状态」）：D2 未成年人只学表达风格；D3 家长设定的单次时长与夜间时段签入授权并由设备会话强制（危机除外）；D4 设备页显示并修改年龄段、改后重签授权；D5 年龄不明允许不留存的学习辅导；D6 删除「每周小结」勾选项；D7 孩子与老人的危机提醒都入队、设备页为老人绑定人显示提醒；D8 话术加 12356。同时修复一个既有缺陷：`guardian_enqueue_declared_notification` 只接受待确认声明，而 2026-09-25 起绑定写入的是已认定关系，孩子的危机提醒入队在 PostgreSQL 必然失败且被静默吞掉（线上尚无孩子绑定，未影响真实用户）。
- 长期记忆留存口径已对齐（2026-09-30，`feat/minor-memory-capture-receipt`，未上线，设备未验）：Policy 对未成年人 `memory_capture` 原为 `PERSIST_AGGREGATE_ONLY`（`minor_memory_minimized`），与 09-25「家长勾选长期记忆后孩子被记住」的决定不符；现在有效监护同意、有效 `guardian_of`、绑定 fence、受信设备、使用人本人说话同时满足时，只带 `RETENTION_TTL`（30 天）与 `NO_MODEL_TRAINING`（`minor_memory_guardian_authorized`），缺任一项仍拒绝。Agent 归档闸门对孩子也认 profile 的 `memory_recall_private`；服务端仍独立按家长 `memory_retention` 同意决定保留原文，长期记忆仍经 catalog 的窄投影。归档证据行没有过期任务，`RETENTION_TTL` 目前只有 MemoryScope 记录执行。详见 `docs/compliance/p0-04-minor-safety-decisions.md`。
- 待完成：微信订阅消息模板开通与 `MEMORIA_GUARDIAN_PUSH_ENABLED`（需授权）；会话记忆按使用人迁入 `services/memory_scope`；两条策略入口软件矩阵；安全专项设备链验收；话术专业审核。
- 待完成：建后年龄资料与 `app_confirm` UI、guardian consent 决策接口、会话记忆按 subject 键迁入 `services/memory_scope`，以及安全专项设备链（身份/年龄→有效同意→准入或受限能力→固定话术真实交付→outbox 绑定/幂等/家长读回）。发送 worker/外部投递暂缓。
- 软件门：两条策略入口覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、同意撤销/过期/无权限、管理账号更换与切人并发；不能决定时 fail closed，且不得读取其他主体私密记忆。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code/wired/enabled/verified`；`student_safety_loop_verified=false` 保持到安全专项设备链通过。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/{identity,session_runtime,guardian}/`。

### [ ] P0-03 TTS、续问竞态、设备停滞与真实时延

- 2026-09-28 现场修复（收据见 HANDOFF 当日各节）：播放后追问的端点以空文本 final 钉住、且推进时不重置 2.5 s 绝对截止，导致问天气说到一半即待命（#80，真机已验证能完整回答）；已有文字的轮次被背景声反复 `vad start` 重开，回答被拖 17 s（#83 加 2 s 证据窗口，已上线，嘈杂环境尚未实测）。
- 待完成：G 的 owner-silence/endpoint/commit/watchdog 真实配置矩阵；B/D 的 Bridge→Edge→设备接收/解码/播放同 fence 证据；部分音频失败后的设备终态；ACK→正文 <1.5s 的链路拆分与达标。
- 设备窗口发现（2026-09-20，已启用候选 `d96d4c2`，收据见 `HANDOFF.md`）：**F1 owner-silence 待命过早已修复并发布**——旧语义播后只沿用剩余预算（本例 ~4.4s）即待命，多轮续问接不上；现改为**任何被接受的主人话轮都重新给足整段窗口**、默认 `MEDIA_OWNER_SILENCE_TIMEOUT_S` 10s→30s、助手自发提示不得延长、明确告别立即待命；2026-09-21 设备窗口有 30s owner-silence 观察，但不替代当前 P0-03 的完整设备验收。**F2 待命后再唤醒被拒已修复并发布**——`barge_source_forbidden retryable=0` 曾终止整段会话并弹错；现：禁止源 barge 只拒绝交接话语权（不转发/不关连接/不发 session.error，`device_barge_ignored_total` 可观测），且 `playbackActive` 与 **fence** 绑定、**只由设备自己的 `button.stop`（本地 flush）撤销**；`generation.cancelled` 不清窗口（它携带后继 generation，既不匹配回执 fence 也不证明设备已停播，清它会 fail-open），残余情形保持 fail-closed。已有设备窗口只覆盖 `button.stop` happy path；禁止源 barge 尚未在设备旁真实触发，完整 F2 契约保持未验证。固件侧「播放期不发 `vad.start`」为可选加固（需刷机），voice barge 长期走 P1-07 签名授权。
- 设备验收：同一已启用候选完成天气→续问→播后告别至少三轮、>45s 与 B/D 同类长答、临近静默和部分下发后故障；补待机、五表情及点屏/摇晃/短拍/BOOT 不回归。2026-09-24 已完成缺陷 A 核心续问的真实设备证据：3/5/8s 精确格通过，30 分钟基础长稳通过但带 TLS/WSS 自动重连观察项；工具查询最终回答已于 2026-09-24（`docs/acceptance/run-20260924-d1d2-deploy/findings.md`）与 2026-09-25 整栈真机验收走通；TLS/WSS 重连、长答/故障/待机/表情及交互矩阵仍待补齐。
- 缺陷 A（2026-09-21 window-a 新发现）：ASR 段落跨界拒绝吞续问 + 回声驻留 VAD 压制拆分 + endpoint 空等 ~20s。**方向一已实现入库（`b41ff7a`，pytest/mypy/ruff/offline e2e 全绿）**：播放终止快照上行捕获域边界（证据水位 + 0.8s 回声尾余量）→ 边界拆分不再被回声 VAD/间隔门压制 → followup 提前 endpoint（1.2s grace，不等 vad.end/离线段，续说并入同话轮）；supervisor 拒绝门未放宽。**已发布并于 2026-09-24 完成核心真机复测**：tag `20260921-defect-a-followup-endpoint`（commit `2a33a50`）生产 agent/bridge 切流 PASS；3/5/8s 精确追问和无 echo+追问合并核心边界通过，30 分钟基础长稳通过但带 TLS/WSS 重连观察项。工具查询最终回答随后在 D1/D2 上线后走通，但其余设备矩阵未完成，故不能关闭 P0-03；完整收据见 `docs/acceptance/run-20260924-defect-a-retest-live/findings.md`。方向二 2a（rescue 换 paraformer 词级时间戳）留独立工单，仅当后续发现 realtime 错字时启用。
- 工具查询回答被取消（2026-09-24 复测 `turn_id=6`，离线诊断收据见同目录 `findings.md`「离线诊断」节）：搜索 10.18s 已返回，但回答被 `floor_blocked` 压约 8s，随后被一段 3 字救援文本判为告别，generation 8 首帧前 `preempted`。该段上行 rms 389 / peak 2616，与底噪同量级（真话 rms ≥2812、peak 削波），FunASR 静音；无原始音频，判为“极可能底噪幻听”。**D2 底噪救援告别可结束会话**：已上线（main `c979f4e`，旧记录 `200d52e` 为合并前哈希）——救援结果携带段能量 `rescue_rms`/`rescue_peak_abs`，`CROSS_SENTENCE_OVERLAP` 与 `STRADDLES_COMMITTED_WITHOUT_TIMING` 两种被拒形状下 rms<1000 且 peak<8000 的救援告别不结束会话（低音量真实告别退回 owner-silence 30s 待命）；回归 `test_device_straddling_rescue_farewell_needs_speech_energy`。**D1 底噪 VAD 占住话语权**：两路 ASR 已判空仍不释放，每段新 VAD 重置 2.5s 尾超时；已上线（main `48e34bc`）——「无证据占用上限」（基础 3s，每次 vad.start 至多推后 1.5s，总上限 6s；有任何文本证据即不干预），回归 `test_qa_evidence_less_vad_cannot_hold_weather_result_past_cap`；09-25 整栈真机验收续问正常，完整 3/5/8s 格仍待在当前版本复测。附带：真话上行普遍削波（DTLN makeup gain 18 dB），单独评估；下次采集须确认 agent 日志流非空。
- 2026-09-20 设备侧异常（非刺激引起，空闲期发生）：BMI2 IMU I2C 读持续超时刷屏；端口复位后两次 `abort() PC 0x4038acd6` → `RTC_SW_CPU_RST`（约 12s 后再起，随后自愈）。需硬件/固件侧单独排查。
- 删除域状态（2026-09-20 决策收敛，过程记录见 HANDOFF）：物理不可删域不做封存实现、记为已知缺口（表述禁用「封存/已擦除」）；Slice A（guardian tutor 两表删除/计数/导出）已完成 `6e853ef`；开放项（identity/device_fleet 归属、封存计数面）归 P2-03。
- 2026-09-20 产品侧提醒（读口真相）：产品召回**不读** `memory_records`，而走 archive 目录（`services/control_api/app/routes/interaction.py:1719-1743`，account_id+subject_id）⇒ 仅封存 memory_scope 不会让轮次内容消失；operator 读口 `services/governance/subject_postgres_reads.py:249-300` 必须同步改。
- 约束：保留现有 GenerationBudget、代际隔离和失败有界退出；不靠延长静默、重复整句合成、第二提示或放宽门禁遮掩问题。入口：`providers/{generation_budget,doubao_tts,cosyvoice_tts}.py`、`voice_core/media_session_{standby,output_stream}.py`、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与产品闭环

### [ ] P1-01 冻结修复候选，完成发布与回滚验收

- P0 收口后冻结同一 source/lock，跑 ruff、module budget、strict mypy、协议生成、真实 PG init/repeat-upgrade、全量 pytest/覆盖率、Offline E2E 和受影响镜像门（Agent 镜像发布门：bridge 不导入 livekit、自有媒体遥测隐私 canary、DTLN 去噪器可加载）；DSN-gated skip 不能当通过。
- 用标准全量构建复验 Agent/Bridge 同一产物、非 root 运行与真实环境变量；按授权切流，复核 12 个 readiness、外部路由、provider/LiveKit、设备与时延，并实际演练紧邻回滚。
- 完成条件：同一候选的构建、切流、回滚、设备验收分别有证据；仅保留当前和一个可运行回滚，清理生产另授权。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧容量预测不再复用。
- 需用户决定并授权裁剪、停用 `archive_mode`（需重启）或手工阈值策略；不得按 mtime 删除或清理 `pg_wal` 代替归档保留。
- 完成条件：保护集合、容量/恢复影响、执行证据和后续责任明确，保留恢复目标可验证。

### [ ] P1-09 readiness 刷新失败的主动告警（刷新修复与逾期提示已上线）

- 已上线：刷新脚本修复（2026-09-22，收据见 `docs/HANDOFF-archive-0916-0923.md`）；逾期提示（`20260926-edge-flush-v1`）：证据超过刷新间隔 12h + 1h 宽限未更新时，`/health/ready` 报 `smokes: "overdue"` 与 `warnings: ["smoke_refresh_overdue"]`，状态仍为 ready；间隔常量与 `infra/memoria-readiness-refresh.timer` 有一致性回归。
- 待完成：主动告警渠道（需用户定渠道与授权）；目前逾期只在有人或脚本读取 readiness 时可见。
- 发布链缺口（供 P1-01）：组件发布脚本 `deploy_control_component.sh` 的 cutover 块缺已构建候选续跑入口，image-only 覆盖无法承载身份 env，配置不变时需显式 `--force-recreate`，且不校验挂载/端口（细节见 `docs/HANDOFF-archive-0916-0923.md` 2026-09-22 节）；整栈发布已改走 `scripts/release_ops.sh`。

### [ ] P1-02 ASR 救援 sidecar：两项线上调整待授权（可重建与验证已完成）

- 已完成（2026-09-26，代码与文档，见 `docs/runbooks/sensevoice-asr.md`）：`infra/sensevoice-asr/` 入库 Dockerfile（基础镜像按 digest 锁定，与线上逐层一致）、哈希锁定的 17 个依赖（即线上 `pip freeze`）与模型校验值；重建镜像的依赖、Python 版本、脚本与线上一致，4 段合成中文语音在本地 amd64/arm64 与线上实例上转写逐字相同。`scripts/evaluate_sensevoice_rescue.py` 用 12 段真机录音切出的 69 句评测：无外文输出，降 20 dB/8 倍削波后相似度 0.971/0.984，4 路并发与串行一致。空结果分类（`vendor_error`/`vendor_silent`/`gating`/`low_rms`）已由 `funasr_empty_accounting.py` 提供。
- 已修（随 `20260926-minor-safety-v1` 上线）：模型对静音和任意噪声都返回「我。」，原先 `min_text_chars=2` 按原始长度计数让它成为一轮用户输入；现只计文字字符（`SenseVoiceRescueConfig.accepts_text`），回归覆盖。
- 现场证据（2026-09-28）：两次对话的首个救援请求 `httpx.ReadTimeout`（04:59:40、06:23:46），与 ① 一致。
- 待授权（线上问题，建议见手册）：① 空闲约 7h 后首请求解码 12.7s（模型匿名内存被换到主机 swap，VmSwap 约 450MB），空闲后的首次救援必超 2.5s 预算，建议 `--memory 1536m --memory-swap 1536m` 重建容器禁用 swap；② 27–30s 语段解码 2.5–2.8s 必超时，建议 agent `SENSEVOICE_MAX_AUDIO_S=12`。
- 约束：主链是云服务商 FunASR，救援后端单独核验；不把本地 FunASR PyPI、仓内 sherpa-onnx 或云模型版本混为一体，也不顺手改 NumPy/设备 VAD。
- 完成条件：两项线上调整执行后，用合成语音在线上复测首请求与长语段时延均在 2.5s 内；「我。」修复随 agent 发布上线。

### [ ] P1-03 修稳成员入口，再接按使用人切人格/音色

- 已上线（2026-09-26，`20260926-persona-subject-v1`，线上迁移已核对：三张表 `subject_id` 非空、5 条特征回填为账号本人）：人格跟随使用机器的人（用户决定）。给孩子用学孩子的人格，给老人用学老人的人格。人格存储按（绑定账号，使用人）记账：SQLite 与 PostgreSQL 引擎的特征、风格统计、版本表加 `subject_id`，现有数据回填为账号本人、ID 不变，控制面启动时幂等迁移（线上表归 `memoria_app`，现有 5 条特征、0 个版本）。学习：账号本人仍用账号的人格学习同意；其他使用人复用绑定时勾选的长期记忆（签名 profile 对该使用人确认且带 `memory_recall_private`），调度移入 `services/control_api/app/persona_learning.py`。读取：response-plan/context-prefetch 经 `subject_persona.py` 向引擎按使用人读取，其他使用人同样要求长期记忆授权，撤销后不再读出。按使用人删除流程新增 `persona_forgotten` 步骤清除该使用人的人格。
- 已知风险（用户决定）：未成年人与成人一样完整学习人格特征（含价值观、决定等可画像特征），而 P0-04 学生安全闭环尚未完成；对外发布前需在 P0-04 中复核这一口径。
- 查看入口（2026-09-26，接口已随 `20260926-edge-flush-v1` 上线；小程序页面未发布体验版）：绑定人（家长给孩子、子女给老人）在设备页看到「TA 的表达风格」，接口 `GET /v1/persona/subjects/{id}/style` 只返回已确认的固定风格标签（与 uncertain 说话人同一档，不含特征描述、价值观、决定或原话），`POST /v1/persona/subjects/{id}/reset` 经确认后清除该使用人的人格；仅限有效一对一绑定的所有者。
- 待完成：设备验收：同一台设备绑定孩子后隔天体现孩子自己的表达风格，账号本人的人格不串入。
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

- 已完成（2026-09-23，生产配置隔离评测，未部署）：parent-baseline source 已用真实 Qwen `qwen-flash` 跑完固定 7-case 与未见 4-case；收据与完整指标见 `docs/HANDOFF-archive-0916-0923.md`、`docs/memory-evaluation-configured-qwen-flash-20260923.json` 和 `docs/memory-evaluation-configured-qwen-flash-unseen-20260923.json`。固定集 6 个正例全部形成并匹配目标 projection、敏感负例为 0；两组双账户泄漏/候选泄漏均为 0。固定集 `extraction_precision=6/18`、未见集 `extraction_precision=0.2777777778` 均为 projection-level 指标；两份 Qwen 收据均标注 `receipt_scope=parent_baseline`、`source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948`，不能作为候选可见性提交的完整验证。
- 已上线（契约 main `0059368`，随 2026-09-25 整栈发布；旧记录 `f7c4c2a` 为合并前哈希）：普通 search/context 默认仅返回 confirmed 且 `conflict_state != active`；`include_candidates=true` 作为显式审核/评测/诊断入口；companion、response-plan、context-prefetch 显式不带 candidate，评测适配器显式带 candidate。相关 SQLite/HTTP/主体隔离回归与 archive/control-api 测试目录回归通过；PostgreSQL catalog 文件已把审核/候选读取改为显式 `include_candidates=True`，并补了默认隐藏 candidate 断言，当前环境收集为 1 passed/8 skipped（无 `MEMORIA_TEST_POSTGRES_DSN`）；Ruff 与 `git diff --check` 通过，真实 PG 行为仍未验。
- 留出集（2026-09-29）：`services/archive/evaluation/memory_eval_zh_v2_unseen.json`，8 个用例、9 个查询，未针对它写任何规划器扩展；测试只锁定泄漏为 0 与来源归属，召回只记录不设门槛。规则基线（收据 `docs/memory-evaluation-rules-unseen-v2-20260930.json`）：抽取召回 1.0、泄漏 0，但 recall@5 与 nDCG@10 仅 0.22——换一种问法就检索不到，短板在检索而非抽取。逐条诊断（2026-09-30）：7 个未召回查询与目标记忆没有一个共同的内容字（如「早餐饮品」对「牛奶」、「家里老人」对「奶奶」、「我是哪里人」对「老家」），无向量路径直接返回空；分词、n-gram、打分都不是原因，缺的是近义、上位类别、问句类型到属性、常识情境这类语义知识。能桥接这些用例的任何词表都必须写进留出集自己的词，会让它不再是留出集，所以无向量路径不为它加扩展。生产走配置的向量混合检索，不受词面限制，它在留出集上的结果见下方「向量路径评测」。
- 评测后端（2026-09-29）：档案数据只存 PostgreSQL 后，`memory_evaluation` 与 `scripts/evaluate_memory.py` 改在 PostgreSQL 上跑（`--dsn` 必填，每个用例一个临时 schema）。迁移时发现 PG 检索在没有向量的路径上（评测、开发、生产 embedding 不可用时的回退）所有中文命中都并列 0.5 分，缺少 SQLite 版的查询词命中计数排序，固定集 nDCG@10 从 0.859 掉到 0.753、演示集 recall@5 从 1.0 掉到 0.857；已给 PG 无向量路径补上「命中词数 + 0.1×元数据分」且在 LIMIT 前排序，各固定基线与留出集在 PG 上与原 SQLite 数字一致。2026-09-30 起无向量路径只用内容词匹配和计分：完全由代词、疑问词、助词组成的 n-gram（「什么」「是什么」「我们」）不再参与，查询只剩这类词时才保留原词；此前规则抽取的 knowledge 模板问句「这段经历或原则是什么？」会靠这些词压过真正回答的 claim。演示集 nDCG@10 从 0.895 升到 0.947，固定集、未见集、留出集不变，泄漏均为 0（收据 `docs/memory-evaluation-rules-{,unseen-,demo-,unseen-v2-}20260930.json`）。固定集剩余的 nDCG 损失来自同一句话的 claim/episode/knowledge 投影排序，属于投影去重问题，与改写无关。向量路径的检索行为已改动并上线（见「大语料干扰评测」）；评测只有显式传入 `--embedding-model` 才启用向量，且为严格模式：任何 embedding 失败都会中止评测，不会静默退回词面检索。
- 向量路径评测（2026-09-30，用户授权）：本地 pgvector 上的生产 `PostgresMemoryCatalog` 混合检索（0.35 词面 + 0.45 语义 + 0.2 元数据），规则抽取（不调用文本抽取模型），1024 维；两个模型各跑四套：百炼 `qwen3.7-text-embedding-flash`（收据 `docs/memory-evaluation-rules-vector-flash-{,unseen-,demo-,unseen-v2-}20260930.json`）与当前生产的 `text-embedding-v4`（收据 `docs/memory-evaluation-rules-vector-v4-{,unseen-,demo-,unseen-v2-}20260930.json`），各约 190 次 embedding 调用、约 1.1 万字，费用可忽略。recall@5 / nDCG@10（规则｜flash｜v4）：固定集 0.938/0.859｜1.0/0.793｜1.0/0.838，未见集 v1 1.0/1.0｜1.0/0.852｜1.0/0.926，演示集 1.0/0.947｜1.0/0.655｜1.0/0.795，留出集 v2 0.222/0.222｜1.0/0.959｜1.0/1.0；两个模型在四套上的跨账户泄漏、候选泄漏、矛盾率均为 0，抽取指标不变。这套评测分不出 flash 与 v4 的高低，也不能证明向量路径能在大量记忆里挑对。①语料太小：每个账户只有 2–4 条投影文档，向量路径没有最低相似度阈值，基本把整个账户都返回，recall@5 几乎必然为 1.0；34 个有目标的查询里只有 7 个的账户里还有另一条竞争记忆，其余只返回目标自己的投影；留出集 v2 从 0.222 到 1.0，只说明向量路径不再要求词面命中。②nDCG 的差别来自同一句话的 claim 与 episode 投影谁先谁后：两者送去 embedding 的文本不同（claim 是「原句 subject:self predicate:daily_life value:原句 context:原句」，episode 是「原句 原句\n[retrieval-context] 原句」），混合分只差 0.01–0.03，顺序随模型和句子翻转（同一句式，v4 在「敦煌」里 episode 领先、在「武夷山」里 claim 领先），而评测只把 claim 标为相关；这也是「投影去重」问题在向量路径下被放大的原因，同一句话会占满 top-k 的 2–3 个位置。③把同一来源的投影合并成一条记忆后，两个模型完全一致：目标记忆排第一的查询数相同（固定集 13/14、未见集 v1 5/5、演示集 5/6、留出集 v2 9/9），有竞争记忆的 7 个查询里都是 5/7 排第一、错的是同样两条（`repeat-query`、`demo-math-followup`），目标与最强竞争记忆的平均分差 flash +0.068、v4 +0.076（n=7，无统计力）。延迟含 embedding 往返，均为本机经代理的单次运行：flash p50 约 270 ms / p95 290–484 ms，v4 p50 约 300–330 ms / p95 366–711 ms，差异不足以下结论，生产服务器到百炼的链路未测。单请求超时：flash 的固定集与未见集用生产默认 5 秒；flash 演示集首次运行遇一次 5 秒读超时，严格模式据实中止，flash 演示集与留出集重跑以及 v4 全部四套用 15 秒，超时不改变成功调用的结果。这套小数据集分不出模型，大语料版本见下一条。
- 大语料干扰评测（2026-09-30，用户授权）：`services/archive/evaluation/memory_eval_zh_v3_distractors.json`（版本 `-2`），一个账户 422 条记忆（20 个主题簇 × 6 条核心 + 3 条同类干扰、97 条他人的同类事实、145 条日常琐事）和 154 条查询：换问法 82 条（内容 n-gram 与目标零重叠）、词面对照 20 条、精确词 32 条（年份 / 人名 / 专名，查询里的稀有词只出现在目标那一句里）、话题缺失与近似缺失的无答案查询各 10 条；全部经 `RecallPlanner` 后文本不变，以上性质均由测试机检。指标在记忆层（同一句话的 claim / episode / knowledge 投影算一条记忆），另报生产口径「目标是否落在 `context(limit=8)` 的 8 个文档里」；同一批查询在 120 / 220 / 422 条记忆三种规模下重复。代码 `services/archive/memory_distractor_evaluation.py`、`scripts/evaluate_memory_distractors.py`（严格模式，embedding 瞬时错误重试 2 次，`--embedding-cache` 复用向量）、`scripts/compare_memory_distractor_receipts.py`（配对自助法）。收据 `docs/memory-evaluation-distractors-*-20260930.json`（同一数据集哈希，含逐查询结果；`rules` 为词面基线，`{flash,v4}-before` 为改动前，`-after` 为改动后，`-semantic` 为纯语义上界，`v4-candidate-*` 为候选修法，产生方式写在各收据的 `config.note`），规则抽取。改动前（生产路径原样，120→220→422 条记忆，换问法 82 条）：top-1 两个模型同为 0.427→0.378→0.305，MRR flash 0.576→0.531→0.470、v4 0.586→0.538→0.483，目标落进 8 文档窗口 flash 0.951→0.951→0.878、v4 0.939→0.915→0.854；词面对照 1.000，精确词 1.000→0.969→0.969；两个模型配对 MRR 差 +0.007～+0.013，区间跨零，打平。错误主要是答非所问：120 条记忆时 47 个 top-1 错误里 39（flash）/36（v4）个冠军是别的主题簇的记忆。原因是混合分 `0.35×词面 + 0.45×语义 + 0.2×元数据` 的词面项对任何 n-gram 命中都固定给 0.5（+0.175），而换问法的目标词面恒为 0：①带 knowledge 投影的记忆（含「小时候 / 我们家 / 毕业 / 工作 / 觉得」）的模板标题「这段经历或原则是什么？」命中查询里的「什么」；②纯功能字二元组（「我是」「的是」）；③跨词边界的偶然二元组（「里的」「味的」「时的」）；④真实但无关的重叠（「教育」「交通」「喜欢」）。候选修法（v4，422 条记忆，换问法 top-1 / 窗口命中 / 精确词 top-1）：原样 0.305 / 0.854 / 0.969；C1 向量路径也过滤纯功能字 n-gram 0.378 / 0.878 / 1.000；C2 只保留首尾字都是内容字的 n-gram 0.598 / 0.915 / 1.000；C3 = C2 + 词面分按命中查询词占比（线性）0.817 / 0.976 / 1.000；C4 = C2 + 占比平方 0.854 / 0.976 / 1.000；纯语义（关掉词面 n-gram，上界）0.854 / 0.976 / 0.938——纯语义在大规模下精确词反而掉分，所以词面项要修而不是删。选 C4：两个占比变体里平方在每项指标上都更好，这是在本数据集上选的，因此另用从未参与选择的 4 套旧数据集做留出验证（见下）。实施：`memory_domain.edge_content_query_terms`；`PostgresMemoryCatalog._search` 的向量路径改用它取词面词项，词面分改为 `0.5 × (命中词数 / 词项数)²`（整句或 tsquery 命中仍给 0.5；无向量的回退路径不变）。改动后（120→220→422 条记忆）：换问法 top-1 v4 0.939→0.902→0.854、flash 0.939→0.902→0.817，MRR v4 0.959→0.939→0.910、flash 0.959→0.939→0.891，窗口命中两者 0.988→0.988→0.976；词面对照与精确词在全部规模下均为 1.000。改动前后配对：换问法 MRR +0.37～+0.43（95% 区间均远离 0），每个规模 42～46 条变好、至多 1 条变差，词面对照与精确词无一变差；改动后 v4 与 flash 仍无显著差（MRR 差 ≤ +0.020，区间跨零）。留出验证：固定集、未见集 v1、演示集、留出集 v2 共 35 个用例 × 两个模型，recall@5 均为 1.0，跨账户与候选泄漏、矛盾率均为 0，nDCG 持平（flash 固定集 +0.023），仅 2 条查询排序有变化，收据 `docs/memory-evaluation-rules-vector-{flash,v4}-after-{,unseen-,demo-,unseen-v2-}20260930.json`。延迟（422 条记忆、向量走缓存、只比数据库检索、本机）：均值 32.2→20.8 ms，p95 44.6→33.1 ms。新增测试：词项函数单测，以及用「全部向量相同」的桩 embedder 把语义项拉平、只看词面分的 PG 集成测试（在旧代码上失败）。无答案查询：向量路径对全部 20 条仍返回满 8 个文档，没有阈值。改动后垃圾文档的最高分均值（话题缺失）0.48→0.33～0.34，目标对话题缺失的 AUC 0.40～0.42→0.80～0.82（正例含词面对照与精确词），保住 75% 目标的阈值约 0.347 时仍有 20% 的话题缺失查询带垃圾；近似缺失（同主题但没有被问的属性）AUC 0.49～0.56，靠分数分不开。是否加阈值仍待决。局限：语料由人工撰写，规则抽取器把整句话原样作为 claim（生产 Qwen 抽取的 claim 更短更干净，未测）；选型在本数据集上做，旧数据集只有 2–4 条文档 / 账户、区分力弱；精确词只有 32 条、且都是中文；82 条换问法对小差异统计力有限；延迟为本机单次测量。已合并（#150）并于 2026-09-30 随 control-api 组件发布 `20260930-vector-keyword-v1`（基于线上提交 `88a3c80` 的热修分支 `hotfix/control-vector-keyword-bonus`，仅 `services/archive` 5 个文件，模型仍为 text-embedding-v4）上线；线上容器 healthy、无错误日志，尚未做设备追问验收。
- 真实 Qwen 评测（2026-09-29，用户授权，qwen-flash，PG，四集合计约 5 万 token、不到 ¥0.05）：生产抽取提示词 v2 在固定集 recall@5 0.69、nDCG 0.60、抽取召回 0.85，未见集 recall@5 0.80，均低于规则基线（0.94 / 0.86 / 1.0、1.0）。原因：把一句话拆成只剩名字/短语的 claim（「阿梅」「项目验收被否」），关系只给英文码导致 person 文档缺「妈妈」，做事方法类只写 claim 不写 knowledge。提示词 v3（完整陈述、方法类同时写 claim+knowledge）+ person 别名补中文关系词后：固定集 0.875 / 0.83 / 1.0，未见集 1.0 / 1.0 / 1.0，演示集 nDCG 0.86→0.95，抽取精度各集均高于规则；两次运行一致；泄漏均为 0（收据 `docs/memory-evaluation-configured-qwen-flash-*-20260929.json` 为 v2、`-v3-*` 为 v3）。v2 留出集两种抽取器都是 0.22、抽取召回 1.0，瓶颈在检索（改写问法），属方向 B。固定集剩余两处未召回：跨会话同一事件合并、病情边界查询。评测框架注意：若 Qwen 对需审核的来源没出 claim，整集报错退出。
- 待完成：用线上带鉴权读口与设备追问验收向量路径词面项的改动（已上线）；是否加分数阈值（近似缺失分不开；我在 control_api / agent 里没找到按分数过滤记忆的调用方，未逐一核实）；用真实 Qwen 抽取的 claim 形态与更多真实专名 / 英文词复测；评估 claim/episode/原子 projection 去重（向量路径下已使同一句话占满 top-k，见上；claim 送去 embedding 的文本带 `subject:/predicate:/value:` 脚手架是否拉低语义分，未验证）；生产更换 embedding 模型前须先回填向量——向量行只在编译落文档时写入、检索按（模型，维度）过滤，换模型后已有记忆只剩词面路径，仓库暂无回填工具；补齐实际 `ResponsePlannerClient` 超时、fallback 和生产 catalog 限额证据，并做线上带鉴权读口与设备追问验收。把会话记忆迁到当前 person/subject 键、覆盖切人、撤销、删除和旧缓存的工作仍不提前宣称完成。
- 完成条件：固定集与未见集分别报告 recall/nDCG/extraction/leakage，candidate 语义、projection 去重、实际超时/catalog 限额和 person/subject 隔离均有可复核证据；当前 candidate 契约已上线、两组隔离评测为上线前基线，线上/设备边界仍未达成。设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button/keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别/打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

### [ ] P1-11 一台设备只服务一个使用人：声纹下线，信任与同意来自绑定（已上线，设备验收未完成）

- 产品决定（用户 2026-09-25）：暂不用声纹（09-03 起生产 0 次 owner_match，主人得分 0.41–0.67 对门槛 0.78，profile 未评估）；设备只服务绑定时选定的使用人（孩子/老人/本人）。家长只看摘要、趋势、风险提醒，不看孩子原文；孩子的长期记忆需家长在绑定时勾选（默认不勾、非必选）；实名家长声明即监护关系；老人记忆由子女代为同意（如实记为代理）；同意长期有效直到撤销；解绑撤销同意并询问是否删除。
- 已做并上线（本地 + 临时 PG 验证，生产 `MEMORIA_SPEAKER_AUTHORITY_ENABLED` 已于 09-25 改为 false）：Agent 在无声纹的设备会话上以签名 profile 为据给出 `device_bound_subject` 主人（仅数据权限，`current_speaker_authority_verified` 仍为假，回声/打断门不变），Control 用同一 profile 核验；播放后 3s 内机器人自己说过的告别词不能结束会话；策略 `subject_presence=device_bound`（家长 App 切到孩子仍读不到孩子记忆）；绑定时写入同意权威授予（guardian/subject/新 `delegate`），认定关系 `guardian_attestation_v1`/`delegate_attestation_v1`，老人登记为经绑定人认定的成年人（`child_for_parent` 此前根本无法绑定）；家长页开关双写、解绑撤销、换版延续；无账号孩子可由绑定人导出；生产 env 模板关闭 `MEMORIA_SPEAKER_AUTHORITY_ENABLED`；小程序隐藏声纹入口、绑定勾选与解绑/导出/删除入口。顺带修复：任何已生效关系都会让 profile 落库复核指纹不一致（收据只锁选中的关系）。
- 按使用人删除（2026-09-25 同分支）：可续跑、有进度记录的删除流程（删除期间拒收该使用人的新证据），依次关闭该使用人的设备会话、删归档证据及其派生记忆（含引用了 TA 的合并条目）、文件对象、危机提醒与学习记录、MemoryScope（新维护角色 `memoria_memory_maintenance` 专用函数，只增不改约束对其他调用方不变）、语料；解绑并选删除时再把 TA 的身份隐去为占位并清掉审计中的旧名；最后逐库核对为空。保留的仅有无内容审计（同意记录、策略收据、关系/绑定、会话档案）。部署前须在 `/etc/memoria-postgres.env` 加 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`，控制面 env 加 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`。已知残留：已推送到 Redis 的记忆事件无法撤回（随流修剪老化）；`speech_style_stats` 聚合、persona/digital-self 快照、`entity_ids` 数组无行级来源可追。
- 已重新绑定（2026-09-28）：开发板以「给孩子使用」绑定为 `e8a27e45` v3 并勾选长期记忆，绑定时写入 consent grant；重新绑定曾因会话信任表未跟随而拒绝设备会话（#77 修复并上线，09-28 设备页 runtime-profile 与机器人 media-sessions 均已 200）。`memory_recall_private` 与隔天记忆在设备信任问题解决前无法验证（见下一条）。
- **阻塞：设备信任（2026-09-30 只读核对，最优先）**：`action_device_lock_trust` 只有「证书有效且 attestation 当前有效」才返回 `verified`；生产 `device_fleet_attestations` 为 0 行，返回 `untrusted / device_attestation_unavailable`，Policy 因此对所有主体（成人和孩子）拒绝 `memory_recall_private` 与 `memory_capture`（`device_untrusted`）。库里最近签发的设备与 app 会话 profile 能力只有 `["chat"]` 或 `["chat","english_practice"]`。这就是设备对话自 2026-08-08 起不入库的最深一层，#151 与 A1 单独上线都不会改变它。`DeviceFleetService` 已有 nonce 与签名 attestation 的接收逻辑，但控制面没有对外路由、固件也没有实现。选项：①做真 attestation（固件对 nonce 签名 + 控制面路由 + 证书链），工作量大、无排期；②为「经 onboarding 认证绑定、生命周期为 bound」的设备新增只对记忆能力生效的信任级别（改 `action_device_lock_trust` SQL、引擎把 `TRUSTED_DEVICE_TRUSTS` 拆开并同步生成契约，走 `release-ops schema`），相当于把信任放在绑定与设备凭据上而不是硬件密钥，ESP32 没有安全元件时两者强度接近，但这是安全口径的改变；③维持现状，设备没有长期记忆。
- 未做：声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。
- 监护小结（2026-09-26 已上线；用户决定随长期记忆一起授予）：家长给孩子绑定并勾选长期记忆时，同时授予 `guardian_summary_view`（`GUARDIAN_MEMORY_CAPABILITIES`），家长页长期记忆开关同步授予/撤销；真实 PG 下家长 app 的签名 profile 因此出现该能力，孩子私人记忆仍不给。周小结接口新增无账号孩子分支：以孩子的长期记忆同意放行，只聚合家长账号下标注为该孩子的记录（不含原文），小程序在没有监护链接时自动加载绑定孩子的小结。老人（子女代同意）不授予监护小结。存量绑定需重新绑定或在家长页重开长期记忆开关后才会获得授予。
- 完成条件：设备上孩子/老人/本人三种绑定各一次：隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声"再见"不结束会话、真人"再见"能结束；家长端看不到孩子原文；撤销后不再记忆。

### [x] P1-12 旧媒体链去留（2026-09-29 下线并清理完成）

- 已完成：整栈发布脚本入库为 `scripts/release_ops.sh`（回归 `scripts/tests/test_release_ops_script.py`），已用于两次全链发布，`20260926-edge-flush-v1` 首次使用仓库版并通过新链冻结校验；常量已指向当前线上链，下次整栈前只读复核后安装。media-edge 已脱离易失的 `/tmp/media-runtime.override.yml`，两次单独切换收据见 HANDOFF。persona 与 session-context 死链路已删，生产要求的能力 token 由十个降为八个（2026-09-28 删除 `/v1/evolution` 后再降为七个，`MEMORIA_EVOLUTION_VALIDATOR_TOKEN` 退役），`split_production_env.py` 接受但不分发已退役变量。
- 已决定（2026-09-29，用户）：下线。当天只读复核：LiveKit、LiveKit worker（`agent` 容器）、Python 设备媒体网关（8793）、小程序网关过去 72 小时零业务流量，唯一设备的 350 个媒体会话全是 `direct_voice_core`；今后没有手机/浏览器实时语音计划。代码已删（worker 入口与仅其可达的运行时代码、两个网关包、control-api 的 LiveKit 会话/token/房间关闭/回退与设备放量白名单、LiveKit 部署件与 nginx 片段、发布脚本角色、约 56 个环境键）；readiness 改由 voice-core bridge 上报心跳（不再有 `livekit_ready`），设备配网与绑定授权改用 `DEVICE_DIRECT_MEDIA_WSS_URL`。`release_ops.sh` cutover 只 `docker stop` 旧三个容器不删除，回滚仍按上一版本 compose 重建。已上线（2026-09-29 整栈 `20260929-livekit-retire-v1`，真机对话验收通过，收据见 HANDOFF）：旧三个容器已停止未删除。主机旧件已于验收后清理（LiveKit server、旧 nginx 片段与 include、三个旧容器、网关 env 与镜像，收据见 HANDOFF）；此后回滚按组件进行。`ReplyPipeline` 已抽取（批次 5 D，`services/agent/src/reply_pipeline.py`，不再继承 `livekit.agents.Agent`，`DuplexVoiceAgent` 已删除）；bridge 仍以类库方式依赖 `livekit.agents`（TTS 流、`llm.ChatContext`、`StopResponse`）与 `livekit.plugins.openai.LLM`。
- 完成条件：旧媒体链有明确决定并按决定执行。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集/未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备/真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`reply_pipeline.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-03 可证明删除与导出证据链

- 已完成（本地，收据见 `docs/HANDOFF-archive-0916-0923.md`）：四迁移读路径的 PG 侧对等（operator `read --postgres-dsn`，真实 PG 契约；投影侧 PG 未建表时 fail closed 不回落账号键）；PG 全 saga 删除验证（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）。
- 已完成（2026-09-26，已随 `20260926-edge-flush-v1` 上线）：按使用人删除覆盖面逐存储复核并记入 `docs/compliance/delete-domains.md`；旧版“结构盲区”中 `memory_scope` 已由 P1-11 覆盖，同意/策略收据/会话 profile/身份绑定为有意保留的内容无关审计。修复：数字分身与进化学习只接收账号本人（或早于主体归属）的证据；删除末尾核对人格剩余行；删除前按使用人失效 response-plan 缓存；备份「恢复后再删除」由 `scripts/replay_subject_deletions.py` 重放全部已完成删除，已写入运维手册恢复流程。
- 待完成：真实 MinIO 版本删除与真实 provider 删除各需一次可复现收据（需可用环境与授权）；已知缺口（Redis 已派发载荷、未脱敏的显示名与激活清单、工具效果载荷、SQLite 迁移备份、悬空实体 id、空主体历史证据）按合规文档明确接受，若要闭合需先定权限与迁移方案；小程序成员主体读口（P1-03/P1-05）；生产/设备与真实机器人对话验收。
- 安全约束：不得默认 `account_id == subject_id`；必须有 Control 注册、active Identity person、owner evidence 和一致事件 subject；child/member 只接受唯一 lineage，foreign/inactive/ambiguous/NULL/mixed subject fail closed；源表与 `snapshot_json` 保持字节不变；rollback 只移除本 migration 行。
- 完成条件：PG、MinIO、投影/缓存和 provider 范围一致，重试幂等、回执可查询、导出标记 AI/授权/服务提供者；保留备份写明期限与恢复后再删除，不承诺即时物理抹除全部副本；真实 MinIO 与 provider 各需一次可复现收据。

### [ ] P2-04 协议故障注入与长稳观测

- 已修并上线（`20260926-edge-flush-v1`，设备行为未验）：关闭前送达排队的 `session.error`（固件只据此判断终止/可重试，从不读关闭码）；Opus 编解码器加锁，消除连接关闭时 `opus_encode` 的 SIGBUS。复现与验证数据见 HANDOFF 同名发布一节。
- 已补故障注入（2026-09-26，测试，`services/media_edge/device_ws_fault_injection_test.go`）：七种关闭路径（设备 `session.close`、突然断网、拒绝控制帧、上行序号重放、Voice Core 断流、控制面关闭、新 epoch 接管）加拒绝 hello，三轮 24 条连接后协程、连接表、租约、活跃连接数与 Voice Core runtime 全部回收；回答下发中途 Voice Core 断流时设备先收到可重试 `session.error`、其后无音频、以 1011 关闭；关闭与迟到 Core 事件并发时无竞态、关闭后不再转发；上行重复/跳号帧回可重试 `uplink_sequence_gap` 后关闭。变异验证：去掉关闭时的 runtime 关闭、去掉 P0 冲刷、让写循环不退出，均被测试抓到（写循环不退出时报 27 个协程泄漏）；`-race -count=20` 稳定。本轮未发现新缺陷。
- 待完成：Python 侧 Bridge/Agent 进程退出与重启的注入；预定义长稳窗口（内存、连接、延迟趋势）；govulncheck/Trivy 扫描当前候选（本机未安装工具）；设备上验证终止性拒绝不再续连。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行；预定义长稳窗口保留内存、连接和延迟趋势。先补测量/注入，不顺手改断线产品行为。

### [ ] P2-05 补齐唤醒计数，再采家庭噪声矩阵

- 待完成：固定候选/固件/settings，按物理距离、角度、输入电平、多冷启动和预定义稳态重采电视/家庭音源与真人对照；先核实际模型、阈值、状态和 detector 权威。
- 旧日志不足以证明电视/多人各 2 分钟有效零误唤醒，也不证明固定 45–50s 预热；错误不能算 miss 或零误唤醒。
- 完成条件：取得可比较设备 receipt 后才调检测阈值/词形；不修改 `advertised_duplex_level=none` 或 `aec_reference_verified=false`。

### [ ] P2-06 用可回放任务评测陪伴连续性与回顾质量

- 待完成：补学习挫败承接、隔次继续计划、本人/获授权管理人查看回顾等场景，交叉 under_14/14_17 与 retention 允许/拒绝，包含切人、撤销、模型超时和未见集。
- 逐场景记录任务接续、记忆证据、越权/编造、回顾可读性和失败降级；模型措辞需 recorded-bundle 人工盲评或设备窗口，离线生成不计真机成功。
- 完成条件：形成可重复 baseline 与同条件对照，主体/撤销隔离和编造事实零回归，汇总可追溯到话轮/证据。

### [ ] P2-07 Prompt 审计遗留（2026-09-24 审计；第 1、4 项已上线，第 2、3 项待定）

- 审计口径：全仓发给模型的文本；实际模型为 DeepSeek V4 Flash（主对话，百炼）、`qwen-flash`（四个语义分类器/人格结构化）、`qwen-plus`/`qwen3.7-flash`（抽取/联网）。判据源自 Claude 文档，对这些模型只算经验判断，置信度最高为“中”；均未调用真实模型验证。
- 中-1、中-4 已修复并上线（`20260926-edge-flush-v1`）：`SAFETY_CORE` 只保留透明版 AI 身份规则（生产文本 SHA-256 不变）；人格抽取删去重复的一次性情绪规则。
- 中-2 口癖禁用词表：`prompts.py:34` 列举“首先、其次、最后/综上所述/希望以上内容对你有帮助”，无来由（首次提交即有），列出原词可能反向锚定。拟改为正面表述“像当面聊天一样自然衔接，不用书面报告式的分点连接词、总结句或客服式结束语。”；会改变生产陪伴提示词。
- 中-3 小结指令语义歧义：`services/control_api/app/routes/memory.py:293`“也不要输出敏感信息之外的推断”字面可读成允许推断敏感信息。拟改为“只写聊天中实际出现的内容：不编造事实，也不推测对方没有明说的健康、财务、关系等敏感信息。”（本意需确认）。
- 低-5 仅标记：`services/agent/src/providers/qwen_realtime_search.py:90` 发往 DashScope 只带 `thinking: {type: disabled}`，而其余 DashScope 调用都带 `enable_thinking: False`（`handlers.py:82-83`、四个分类器）；需对照百炼文档或抓响应确认后再决定是否补齐。
- 低-6 仅标记：三个 Qwen JSON 抽取器用 `response_format: json_object` + pydantic 兜底；若当前 Qwen 支持 `json_schema` 结构化输出可再评估，现状可用。
- 已核实保留：分类器“只输出一个枚举词”（精确解析、`max_tokens` 12）、半双工 120 字规则（`reply_pipeline.py` `MAX_CONTROLLED_VOICE_REPLY_CHARS` 代码截断）、禁 Markdown（TTS）、禁笑/咳嗽标签（CosyVoice 支持且 `prosody.py:29` 过滤）、导师不给答案、危机/暴力固定话术、`context_assembler` 指令/数据分离、小结字数上限（pydantic 校验）。
- 完成条件：1–4 逐条单独落地（1、4 已完成）；2 需在 DeepSeek V4 Flash 上用真实语音样本前后对照，3/4 跑 `scripts/evaluate_memory.py` 或小样本对照；语音/导师/prompt composition/persona/小结相关测试通过（临时副本上已通过，1 skip）。

### [ ] P2-08 架构整理后续（2026-09-26 审计，减法优先）

状态（2026-09-30）：2026-09-28 双视角评审的第 0–6 批代码已全部合并（每批一个 PR；顺序：先修缺陷，再做减法，再做需授权的存储迁移，最后拆大对象；产品轨并行），没有打开的 PR。约束：涉及生产数据迁移须另获授权并先演练恢复；不做大爆炸重写，每步可独立发布与回滚。

**已完成（均已上线，注明者除外）**

- PR #42 删除无消费者代码约 3.09 万行，行数预算覆盖全部超 1,500 行模块，跨包依赖图冻结；`create_app()` 与 lifespan 共用装配函数 `_wire_services`，同 archive DSN 的 10 个存储共享一个池（`20260926-edge-flush-v1`）。
- 第 0 批（#86、#87）：小程序 tab 导航；Edge 写循环出错即关连接、`CloseSend` 串行、accept 之后才登记连接。
- 第 1 批（#89）：Edge 开槽等待跟随请求 context、超载 503、关键词发送不持锁做网络写；guardian `grant_consent` 补 actor 校验；`BootstrapStorePort` 改 Protocol、删 `sqlalchemy`；小程序隐藏不可用入口、失败回滚，回顾页文案改为"点确认后才会留下"。
- 第 2 批（#91）：删零消费者路由与装配、一次性迁移、Python 设备网关与 evolution 运行时残留；域包保留，Go keyword 通路留给 P1-07。
- 第 3 批：① 控制库同一套 SQL 跑 SQLite 与 PostgreSQL（`database/backend.py`），2026-09-28 23:35 生产切换完成（2266 行逐表校验一致），迁移脚本与 `release-ops.sh control-store`（#92）；② PG 测试底座 `testing/postgres_harness.py` 与 CI 作业 `control-api-postgres`（#108），底座找出的生产缺陷已修（#96–#104）；删除 identity、consent、memory_scope（#118）、guardian（#120）、archive 一族（#121）的 SQLite 孪生，测试只在 PG 下跑，顺带修 PG 声音登记 RLS 与无向量路径排序；③ 版本化 schema 迁移（#98，`control_schema_migrations`，运行时角色只读台账，库版本落后即拒绝启动）。
- 第 4 批：跨语言 fence 向量（#93）；`TTS_PROVIDER` 工厂（#94，生产仍用豆包）；P1-12 旧媒体链下线（#122，`20260929-livekit-retire-v1`，主机旧件经用户确认已清理）；`ReplyPipeline` 抽取（#126）。
- 第 5 批（`20260929-voice-core-refactor-v1`，#123–#126，真机验收通过）：拆 `test_media_session.py`（#95）、prompt 测试按段落 ID 断言（#107）、公开测试接缝（私有读取 1290→852）与录制基准、`GenerationRecords`、`VoiceFloorState`、`PendingTurn`/`OutputState`、删可信中断与 listener-cue 死代码。可控时钟不做：实测最慢的测试是 5 秒的 mock 超时，非零 sleep 合计约 2 秒。
- 第 6 批：media-edge 改 `slog`、CI 固件编译（#90）；环境模板改为漂移校验不做生成（#105，`test_env_template_drift.py`）；反向依赖清理（#106）；`ControlSettings` 按域拆 8 个 mixin（#111）。
- 09-29 停止词与轮次：`20260929-stop-word-v1`（#127）、`stop-playback-v1`（#129，#128 抽取提示词 v3）、`stop-reconnect-v1`（#130）、`turn-taking-v1`（#133、#134）；`session-limits-v1`（#136，设备页「使用时段」可修改，小程序体验版 `0.2.20260929.1`）。
- 09-29 CI 分片（#137、#138）：python 测试分 4 片，一轮约 10 分钟；main 上不再自动跑 CI，只在 PR 与手动触发时跑。
- 09-30 `20260930-local-stop-v2`（#139–#142）与 media-edge 三次组件发布（#145、#146）；固件仅 USB 刷入：#144（唤醒词 0.10，build 14）、#147（播放期关闭本地停止词、唤醒词 0.12，build 15/16）；#143 组件快车道接受整栈 bridge（未在生产实跑）。
- 记忆评测：抽取提示词 v3 + 中文关系标签，固定集 recall 0.875、未见集 1.0、泄漏 0；留出 v2 集仍 0.22；留出集（#119）。流式 ASR 四家离线 A/B（#131，`docs/acceptance/run-20260929-asr-ab/findings.md`）：正常朗读打平，qwen-audio-3.1 与豆包最干净，空结果来自上行电平，暂不换。测试稳定性（#109、#110）。
- 09-30 去掉 `livekit.agents` 依赖（#148，已合并，**待发布**）：本地 `llm_types.py`、`providers/openai_chat.py`、`providers/tts_stream.py`、`provider_errors.py` 取代 livekit 的 ChatContext、openai 插件、TTS 流与错误类型；旧库并排对照 543 例通过后连同依赖删除（提交 `15075621` 可复现）；锁文件 122→95 包，镜像 101→69 包；`onnxruntime` 显式声明，`pillow`/`sounddevice` 入 dev extra；发布门改为验证 bridge 不导入 livekit（保留自有遥测隐私 canary、DTLN 加载）。

**未完成**

1. 第 3 批 ② 其余域：剩控制库 `MemoryStore`、speaker、evolution 的 SQLite（未要求删除）。
2. 发布 #148：依赖文件变了，须本机全量 linux/amd64 构建的整栈发布（上次依赖变化时用清华源，speaker-model 首次曾因 PyPI 索引响应截断失败，重试通过），发布后用 `scripts/voice_session_capture.py` 做真机对话验收（重点听慢速 TTS 的节奏）。需用户授权。
3. 验证欠账：固件 build 15/16 播放期关掉本地停止词后，长回复是否还触发任务看门狗与断线，云端语音停止延迟（开口到声音停下）未测；`20260929-turn-taking-v1` 的电脑模拟复测被家长设的使用时段挡住（唤醒只播晚安、不开 ASR），需在使用时段内复测。
4. 在途分支：#151（`fix/device-archive-persistence`，成人主体凭 `memory_recall_private` 授权归档；根因已核实：生产自 2026-08-08 起没有设备对话入库，另有设备信任一层，见 P1-11）；`feat/minor-memory-capture-receipt`（叠在 #151 上：Policy 对孩子的 `memory_capture` 对齐 09-25 决定 + 闸门放开孩子，未开 PR）；`wip/tts-stream-hardening`（本地，#148 时摘出的 TTS 清理钩子与 `APIConnectOptions` 有限值校验，钩子版 `aclose()` 会吞调用方取消，需返工）。`feat/memory-retrieval-paraphrase` 已合并为 #150 并随 control-api 组件发布上线。

**需要用户决定**

- 发布 #148（见上）；固件 build 16 是否签名发布 OTA；固件 AGC（缺陷 B，空结果来自上行电平）。
- 播放期本地停止词（09-30 决定先关，由云端按语义停播）何时以更省算力的方式加回，等云端停播延迟数据。
- self_model 关系画像版本保护：PG trigger 只保护 approved 版本，是否收紧到所有状态（改生产 schema）。
- 记忆检索方向 B：09-30 生产向量路径实测 `qwen3.7-text-embedding-flash` 与 `text-embedding-v4` 在四个评测集上打平，这四个集分不出嵌入模型（每账号 2–4 条文档、无相似度阈值）；需带大量干扰项的合并评测集，换生产嵌入模型前还要先回填向量。
- 在途分支怎么处理；控制词不重置静默计时（UX）；生产常开 PCM tap（`MEDIA_PCM_TAP_DIR`，容器 tmpfs 每会话 4 MB，重启即清）是否长期保留（隐私）。

**产品轨（大多需要用户）**：P0-03 时延拆段（用户暂缓埋点；09-30 另查到播放约 10–13 s 时设备任务看门狗触发，MultiNet 与 Opus 同核，已用 build 15 缓解，待验）；外部试用（10 个家庭、4 周）；路演材料自洽、DEMO-09/10 竞品与定价、定位二选一。

**验证约定**：每个 PR 本地跑 ruff、module budget、mypy strict、相关 pytest（默认模式 + `MEMORIA_TEST_APP_POSTGRES=1`），涉及 PG 加跑 `run_authoritative_postgres_gate.sh`；语音链改动用 `scripts/voice_session_capture.py` 真机验收并写入 `HANDOFF.md`；改依赖时从旧锁重新求解、本地建 Agent 与 control-api 镜像并在 `--network none` 下重跑发布门、用 AST 扫描全仓 import 比对新旧 venv（查丢失的间接依赖）；小程序改动用微信开发者工具 CLI 上传体验版。
- 完成条件：每步有行数与依赖图基线收紧的证据，生产切换有收据。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：真实家庭服务/数据、正式发布或价值量级增长前重评；WAL 仅按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收；启用另定授权和渠道。
- Qwen-Audio 3.1 ASR/TTS（原 P1-10，已评估结论）：ASR 两次真机对照均失败（播放期回声被提交为话轮、漏识别轻声、“稍等”开口延迟 4.1s），生产保持 fun-asr；TTS 迁移按用户 2026-09-25 决定回退、生产保持豆包，重新启用即再 revert 回退提交 `d0d7a43` 与 `2be2f50`。重试前先定位新模型 VAD/段落与回声边界，并在新候选上重做缺陷 A 设备验收。收据 `docs/acceptance/run-20260924-d1d2-deploy/findings.md`。
- 流式 ASR 四家离线 A/B（2026-09-29，收据 `docs/acceptance/run-20260929-asr-ab/findings.md`，脚本 `scripts/evaluate_streaming_asr.py`）：43 段真机录音中有 11 句对上剧本。正常朗读四家基本打平；fun-asr 控制词出错最多，qwen3-realtime 在非人声段普遍吐「嗯。」，qwen-audio-3.1 与豆包最干净。第二轮补录的轻声段与重连后的低电平段四家全空，空结果是上行电平（无有效 AGC）问题，不换厂商。
- 多成员声纹与不依赖小程序的选人（原 P2-02）：随 P1-11 一对一绑定暂停；重启前须用真实标注样本校准误识/拒识后再定门槛。
- EOU 新模型、DuplexModel、expressive/抢跑；ESP-IDF/ESP-SR/upstream 整体升级；老人故事册/人物复刻、年轻人潮玩和最终外形均不进入当前队列。

- F1/F2 已发布（tag `20260920-f1f2-owner-silence-and-barge`）；F1 有 2026-09-21 设备窗口的 owner-silence 观察，F2 仅有 `button.stop` happy path 证据，禁止源 barge 未真实触发，不能宣称完整 F1/F2 契约已完成；修好模拟音频工具的两处自检判据（live 缓冲窗口、参考波形匹配）后跑自动问答扫频仍开放。

- 身份收敛（发布治理）：control-api 期望的 release tag 应与真实发布 tag 一致，取消"agent 上报历史冻结 tag"的临时对齐；与预构建镜像入口一并作为 P1-01 输入。

---

## 历史材料索引（非执行队列）

市场、融资、BP、产品战略和架构简化讨论已迁移至 [历史战略与融资分析](docs/strategy/fundraising-analysis-20260921.md)。
该文件不构成当前工程执行项、发布门禁或完成证据。
