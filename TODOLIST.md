# Memoria 优先级执行清单

更新于 2026-09-24｜DEMO-03 控制面已用可审计发布链切流上线（tag `20260922-demo03-control-review`/`ee57ad4`，healthy + env/binds/ports 不变 + 13 容器未触碰 + 线上 readiness 200 + 固定集无召回退步），回滚点与收据见 HANDOFF 历史归档 `docs/HANDOFF-archive-0916-0923.md` 同日节；2026-09-23 已用生产配置中的百炼 key 完成 parent-baseline source 的固定 7-case 与未见 4-case 隔离 Qwen 评测，收据已保存，未部署候选可见性代码。candidate 可见性契约及回归已在代码提交 `f7c4c2a0f2ec2ec7a9d72fef8c03f85fad8ddf6b` 中完成并验证，仍未部署；2026-09-24 缺陷 A 核心续问边界真实设备复测通过，3/5/8s 与 30 分钟基础长稳有证据，但工具查询最终回答未完成（离线诊断：底噪占话语权 D1 + 底噪救援误判告别 D2；D2 已提交、D1 已本地实现，均未部署）且 TLS/WSS 自动重连保留观察项，不能关闭 `P0-03`。四份评测收据统一为 `receipt_scope=parent_baseline`、`source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948`，不证明候选代码已部署或完整验证。其余发布、主体隔离和删除边界保持不变；本文件只保留未完成事项、执行边界和验收条件，完成收据归 `HANDOFF.md`，过时探针、旧 CI 数字和重复修复流水账从本文件删除。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 3eede2f  # tag 20260924-d1d2-evidence-floor（本地 tag），2026-09-24 agent/bridge 手动 cutover 块切流：2a33a50 + D2 救援告别能量门 + D1 无证据占用上限；ASR 仍为 fun-asr-realtime，TTS 仍为 Doubao；回滚 memoria-agent:rollback-20260924-d1d2-evidence-floor-pre
control_api_release: ee57ad4  # tag 20260922-demo03-control-review，2026-09-22 控制面可审计发布链切流 PASS（healthy / env-binds-ports 不变 / 13 容器未触碰），回滚点 memoria-control-api:rollback-20260922-demo03-control-review-pre-control
control_api_release_lane: 可审计链已用通一次（`scripts/deploy_control_component.sh` 的 cutover 块）；缺口见 P1-09
frozen_candidate: memoria-agent:b668960  # 仅本地构建验收，未启用
memory_evaluation_20260923: code=候选可见性契约已在代码提交 f7c4c2a0f2ec2ec7a9d72fef8c03f85fad8ddf6b 完成 / wired=只核验生产 Qwen key 非空、qwen-flash、OFFLINE_MOCK=false / enabled=false，线上仍为旧 control-api 镜像 / verified=SQLite/HTTP/主体隔离/评测适配器/archive/control-api 回归；四份收据为 parent_baseline source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948，固定集 recall@5/10=0.857、未见集 recall@5/10=0.4、双泄漏均为 0；真实 PG candidate 行为、线上和设备验收未验证
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
2. 缺陷 A 核心设备窗口已完成；D1/D2 已随 `20260924-d1d2-evidence-floor` 上线，工具查询最终回答在真机走通（`docs/acceptance/run-20260924-d1d2-deploy/findings.md`）；下一次设备窗口先补齐工具查询最终交付和 TLS/WSS 重连观察，再按同一候选继续 P0-03 的 >45s/B/D 长答、部分下发失败、待机/表情及点屏/摇晃/短拍/BOOT 矩阵。不得把本轮核心通过扩大为完整 P0-03 或全双工通过。
3. 生产切流、回滚演练和制品清理须另获授权；设备功能通过不等于学生安全或全双工通过。
4. P1-08 WAL 可独立只读测量；删除、重启、定时任务、自动备份和异地副本不在当前授权内。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 待完成：建后年龄资料与 `app_confirm` UI、guardian consent 决策接口、会话记忆按 subject 键迁入 `services/memory_scope`，以及安全专项设备链（身份/年龄→有效同意→准入或受限能力→固定话术真实交付→outbox 绑定/幂等/家长读回）。发送 worker/外部投递暂缓。
- 软件门：两条策略入口覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、同意撤销/过期/无权限、管理账号更换与切人并发；不能决定时 fail closed，且不得读取其他主体私密记忆。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code/wired/enabled/verified`；`student_safety_loop_verified=false` 保持到安全专项设备链通过。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/{identity,session_runtime,guardian}/`。

### [ ] P0-03 TTS、续问竞态、设备停滞与真实时延

- 待完成：G 的 owner-silence/endpoint/commit/watchdog 真实配置矩阵；B/D 的 Bridge→Edge→设备接收/解码/播放同 fence 证据；部分音频失败后的设备终态；ACK→正文 <1.5s 的链路拆分与达标。
- 设备窗口发现（2026-09-20，已启用候选 `d96d4c2`，收据见 `HANDOFF.md`）：**F1 owner-silence 待命过早已修复并发布**——旧语义播后只沿用剩余预算（本例 ~4.4s）即待命，多轮续问接不上；现改为**任何被接受的主人话轮都重新给足整段窗口**、默认 `MEDIA_OWNER_SILENCE_TIMEOUT_S` 10s→30s、助手自发提示不得延长、明确告别立即待命；2026-09-21 设备窗口有 30s owner-silence 观察，但不替代当前 P0-03 的完整设备验收。**F2 待命后再唤醒被拒已修复并发布**——`barge_source_forbidden retryable=0` 曾终止整段会话并弹错；现：禁止源 barge 只拒绝交接话语权（不转发/不关连接/不发 session.error，`device_barge_ignored_total` 可观测），且 `playbackActive` 与 **fence** 绑定、**只由设备自己的 `button.stop`（本地 flush）撤销**；`generation.cancelled` 不清窗口（它携带后继 generation，既不匹配回执 fence 也不证明设备已停播，清它会 fail-open），残余情形保持 fail-closed。已有设备窗口只覆盖 `button.stop` happy path；禁止源 barge 尚未在设备旁真实触发，完整 F2 契约保持未验证。固件侧「播放期不发 `vad.start`」为可选加固（需刷机），voice barge 长期走 P1-07 签名授权。
- 设备验收：同一已启用候选完成天气→续问→播后告别至少三轮、>45s 与 B/D 同类长答、临近静默和部分下发后故障；补待机、五表情及点屏/摇晃/短拍/BOOT 不回归。2026-09-24 已完成缺陷 A 核心续问的真实设备证据：3/5/8s 精确格通过，30 分钟基础长稳通过但带 TLS/WSS 自动重连观察项；工具查询最终回答、长答/故障/待机/表情及交互矩阵仍待补齐。
- 缺陷 A（2026-09-21 window-a 新发现）：ASR 段落跨界拒绝吞续问 + 回声驻留 VAD 压制拆分 + endpoint 空等 ~20s。**方向一已实现入库（`b41ff7a`，pytest/mypy/ruff/offline e2e 全绿）**：播放终止快照上行捕获域边界（证据水位 + 0.8s 回声尾余量）→ 边界拆分不再被回声 VAD/间隔门压制 → followup 提前 endpoint（1.2s grace，不等 vad.end/离线段，续说并入同话轮）；supervisor 拒绝门未放宽。**已发布并于 2026-09-24 完成核心真机复测**：tag `20260921-defect-a-followup-endpoint`（commit `2a33a50`）生产 agent/bridge 切流 PASS；3/5/8s 精确追问和无 echo+追问合并核心边界通过，30 分钟基础长稳通过但带 TLS/WSS 重连观察项。工具查询最终回答未完成，故不能关闭 P0-03；完整收据见 `docs/acceptance/run-20260924-defect-a-retest-live/findings.md`。方向二 2a（rescue 换 paraformer 词级时间戳）留独立工单，仅当后续发现 realtime 错字时启用。
- 工具查询回答被取消（2026-09-24 复测 `turn_id=6`，离线诊断收据见同目录 `findings.md`「离线诊断」节）：搜索 10.18s 已返回，但回答被 `floor_blocked` 压约 8s，随后被一段 3 字救援文本判为告别，generation 8 首帧前 `preempted`。该段上行 rms 389 / peak 2616，与底噪同量级（真话 rms ≥2812、peak 削波），FunASR 静音；无原始音频，判为“极可能底噪幻听”。**D2 底噪救援告别可结束会话**：已提交 `200d52e`、未部署——救援结果携带段能量 `rescue_rms`/`rescue_peak_abs`，`CROSS_SENTENCE_OVERLAP` 与 `STRADDLES_COMMITTED_WITHOUT_TIMING` 两种被拒形状下 rms<1000 且 peak<8000 的救援告别不结束会话（低音量真实告别退回 owner-silence 30s 待命）；回归 `test_device_straddling_rescue_farewell_needs_speech_energy`。**D1 底噪 VAD 占住话语权**：两路 ASR 已判空仍不释放，每段新 VAD 重置 2.5s 尾超时；已本地实现、未部署——「无证据占用上限」（基础 3s，每次 vad.start 至多推后 1.5s，总上限 6s；有任何文本证据即不干预），回归 `test_qa_evidence_less_vad_cannot_hold_weather_result_past_cap`；下次设备窗口须复测 3/5/8s 续问格不回退。附带：真话上行普遍削波（DTLN makeup gain 18 dB），单独评估；下次采集须确认 agent 日志流非空。
- 2026-09-20 设备侧异常（非刺激引起，空闲期发生）：BMI2 IMU I2C 读持续超时刷屏；端口复位后两次 `abort() PC 0x4038acd6` → `RTC_SW_CPU_RST`（约 12s 后再起，随后自愈）。需硬件/固件侧单独排查。
- 删除域状态（2026-09-20 决策收敛，过程记录见 HANDOFF）：物理不可删域不做封存实现、记为已知缺口（表述禁用「封存/已擦除」）；Slice A（guardian tutor 两表删除/计数/导出）已完成 `6e853ef`；开放项（identity/device_fleet 归属、封存计数面）归 P2-03。
- 2026-09-20 产品侧提醒（读口真相）：产品召回**不读** `memory_records`，而走 archive 目录（`services/control_api/app/routes/interaction.py:1719-1743`，account_id+subject_id）⇒ 仅封存 memory_scope 不会让轮次内容消失；operator 读口 `services/governance/subject_postgres_reads.py:249-300` 必须同步改。
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

### [ ] P1-09 readiness 定时刷新：已修并部署（遗留带入下次发布）；发布链三个缺口待补（2026-09-22 线上发现）

- **缺陷（已复现，非切流引入）**：`memoria-readiness-refresh.timer`（每 12h）自 2026-09-22 00:08 CST 起持续失败（systemd `status=1/FAILURE`，restart counter 3 后放弃），原因是 `/opt/memoria/current/scripts/refresh_readiness.sh` 只 `export MEMORIA_RELEASE_TAG`，而 09-21 的 agent 组件切流把 agent 的 base 换成仓库快照（`/opt/memoria/releases/20260921-defect-a-base/docker-compose.production.yml`），该 base 对 `speaker-model.build.args.MEMORIA_RELEASE_COMMIT` 用 `:?` → `docker compose config --format json` 直接报错、stdout 非 JSON → 脚本内联解析抛 `JSONDecodeError`。后果：`readiness_evidence` 中 `20260901-0945-wake-word-whitelist` 打点停在 2026-09-21T04:05Z，超过 `READINESS_GATE_TTL_S=86400` 后全栈 `/health/ready` 变 `not_ready`（core 12 项与 agent 均 ready，仅 `smokes: expired`）。
- **已修复并部署（2026-09-22）**：脚本按 tag 同一模式从 control 容器 env 解析并导出 `MEMORIA_RELEASE_COMMIT`（空值告警不静默）；`require_live_service_image` 保留并打印 Compose 的 stderr、解析失败改为可读报错；回归 `scripts/tests/test_refresh_readiness_contract.py`。修复版部署到 `/opt/memoria/current/scripts/refresh_readiness.sh`（sha `c0152d5b…`→`89c27afc…`，备份 `…/readiness-fix/refresh_readiness.sh.pre-fix`），并以真实 systemd 单元验证 `Result=success`/`ExecMainStatus=0`、readiness 200、证据 `marked_at` 刷新（收据 `readiness-refresh.txt`、`readiness-fix-deploy.txt`、`readiness-fix-verify.txt`）。
- 待完成（遗留）：① 该补丁位于冻结发布树内，需随下次全量发布带入并退役 `/opt/memoria/current` 的手工补丁；② 让失败可见（timer 失败目前只有 journal，readiness 到期才发现）；③ 复核下一次 timer 触发（12h 内）自动 PASS。
- **发布链缺口（同日实测，供 P1-01）**：`deploy_control_component.sh` 的 cutover 块要求线上链为「base commit 的仓库 compose 快照 + `component-releases/` 内 image-only YAML 覆盖」，而线上 control-api 实际链是 `20260827 树 compose + /tmp/media-runtime.override.yml + control-api.override.json` → **任何变更前就 fail-closed**；且 (a) 无「已构建候选续跑」入口（release 目录已存在即拒绝，target 镜像已存在即拒绝重建），(b) image-only 覆盖无处承载身份 env（agent 侧的 `agent-component.override.yml` 是带 env 的非 image-only 覆盖），(c) 解析后服务配置与旧链完全相同时 Compose 不重建、`config_files` 标签不更新（归一化必须显式 `--force-recreate`），(d) 新链不校验挂载/端口/其它容器（demo02 旧工具校验了）。本轮以「同镜像强制重建归一化 + 逐字执行 cutover 块」通过，见 HANDOFF 历史归档 `docs/HANDOFF-archive-0916-0923.md` 同日节。

### [ ] P1-10 Qwen-Audio 3.1 ASR/TTS 切换（2026-09-24，本地 code，未验真实 API、未部署；TTS 部分 2026-09-25 已回退）

- **TTS 迁移已回退（2026-09-25，用户决定生产暂留豆包）**：分支 `revert/keep-doubao-tts` 回退 `457d669`/`d6bd536`，main 恢复 Doubao TTS + CosyVoice v3.5 复刻链路，可直接用现有生产 env（`TTS_PROVIDER=doubao`、`DOUBAO_TTS_*`）启动；Qwen-Audio TTS 待重新评估，重新启用 = revert 这两个回退提交。下文 TTS 相关条目仅作历史记录。
- 已完成（本地）：ASR 默认模型改为 `qwen-audio-3.1-asr-flash-streaming`（`17904a1`，与 `fun-asr-realtime` 同一 run-task 协议，现有 DashScope 域名可用；新增可选 `FUNASR_VAD_MODEL`）。TTS 在分支 `feat/qwen-audio-tts-migration` 彻底替换为 `qwen-audio-3.1-tts-flash`：删除 Doubao TTS/克隆、CosyVoice v3.5 设计音色与 `infra/voices` 注册表；统一身份见 `services/common/voice_identity.py`（provider `alibaba_model_studio`，人格与复刻共用同一 model/resource id，靠 voice_kind 区分）；人格音色映射 星澜→`longanyang_v3.1`、桃喜→`longhua_v3.1`、绵绵→`longwan_v3.1`、阿序→`longanzhi_v3.1`、玄墨→`longsanshu_v3.1`（按官方音色描述选取，未试听）；复刻 `target_model=qwen-audio-3.1-tts-flash`、无 provider 过期；旧 Doubao/v3.5 复刻一律回落人格音色、激活 409 提示重录、档案带 `reenrollment_required`，小程序据此提示重新录制；历史数字分身清单里的 Doubao 音色引用仍可解码。连接池上限 3（官方 3 RPS）。就绪证据 provider 改为 `qwen_audio`，冒烟行改为 `QwenAudioTTS`。
- 真实 API 冒烟（2026-09-24，生产 key、`memoria-prod` 一次性只读容器、不挂生产数据库，跑完已清理）：`provider_smoke_test PASS: FunASR, QwenRealtimeSearch, Qwen, QwenAudioTTS, InterruptSemantic`；5 个人格音色均可合成且对齐 `ok`，ASR 新模型对 6 段合成音频给出中间/最终结果与单调字级时间戳。首跑发现 3.1 每句音频在最后一个字后带 290–450ms 尾部静音，旧对齐逻辑会把字时间戳拉伸进静音并判 `degraded`；已改为尾部静音 ≤800ms 时保留原时间戳（`TRAILING_SILENCE_MAX_MS`）。
- 未验证：真实设备上 ASR 的 VAD/空音频错误码与缺陷 A 边界是否一致；首包时延、音量 45 是否削波；复刻真实 enrollment（返回 ID 前缀、`DEPLOYING→OK` 状态）；3 RPS 在多设备并发下是否够用；设备试听与小程序三端。
- 部署前必须：真实 key 跑通 provider 冒烟；生产 env 用 `prepare_production_upgrade_env.py` 重新生成（`split_production_env.py` 遇到任何 DOUBAO 键会拒绝）；readiness 证据需在同一发布里刷新为 `qwen_audio`；在途会话冻结的旧 fallback 三元组会 fail closed，需低峰切流并演练回滚。本地未跟踪的 `.env` 仍是 `TTS_PROVIDER=doubao`、`MEMORIA_VOICE_TARGET_MODEL=cosyvoice-v3.5-flash`，需要改掉才能本地启动。
- 对 P0-03 的影响：ASR/TTS 换代后，缺陷 A 的 3/5/8s、30 分钟长稳等设备证据不再适用于新候选，必须在新候选上重新验收。
- **ASR 真机对照失败（2026-09-24，收据 `docs/acceptance/run-20260924-d1d2-deploy/findings.md`）**：生产把 `FUNASR_MODEL` 切到 `qwen-audio-3.1-asr-flash-streaming` 后，两轮会话出现 3 个“无人说话却提交的话轮”（机器人回答自己播放期的回声，含一次误判告别结束会话），同一流程的 fun-asr 对照为 0；新模型对同一问句多识别约 10 字、“稍等”开口延迟 4.1s（fun-asr 1.2–1.7s）。已切回 fun-asr；用户亲测再切一次同样失败（漏识别轻声语句；最终结果晚于结束点 2.5s 触发 `turn_prepare_timeout` 待命），已再次切回，选型结论为继续用 fun-asr（收据同上）。若将来重试，需先定位：新模型的段落/VAD（默认 `far_field_meeting_16k`）是否把播放期回声并进播放后的 final、结束点与回声边界逻辑是否需要按新模型调整；可先试 `FUNASR_VAD_MODEL=near_meeting_16k` 与离线回放对照，再上真机。在此之前 TTS 迁移也不应随新 ASR 一起发布。

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

- 已完成（2026-09-23，生产配置隔离评测，未部署）：parent-baseline source 已用真实 Qwen `qwen-flash` 跑完固定 7-case 与未见 4-case；收据与完整指标见 `docs/HANDOFF-archive-0916-0923.md`、`docs/memory-evaluation-configured-qwen-flash-20260923.json` 和 `docs/memory-evaluation-configured-qwen-flash-unseen-20260923.json`。固定集 6 个正例全部形成并匹配目标 projection、敏感负例为 0；两组双账户泄漏/候选泄漏均为 0。固定集 `extraction_precision=6/18`、未见集 `extraction_precision=0.2777777778` 均为 projection-level 指标；两份 Qwen 收据均标注 `receipt_scope=parent_baseline`、`source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948`，不能作为候选可见性提交的完整验证。
- 已落地（2026-09-23，代码提交 `f7c4c2a0f2ec2ec7a9d72fef8c03f85fad8ddf6b`，未部署）：普通 search/context 默认仅返回 confirmed 且 `conflict_state != active`；`include_candidates=true` 作为显式审核/评测/诊断入口；companion、response-plan、context-prefetch 显式不带 candidate，评测适配器显式带 candidate。相关 SQLite/HTTP/主体隔离回归与 archive/control-api 测试目录回归通过；PostgreSQL catalog 文件已把审核/候选读取改为显式 `include_candidates=True`，并补了默认隐藏 candidate 断言，当前环境收集为 1 passed/8 skipped（无 `MEMORIA_TEST_POSTGRES_DSN`）；Ruff 与 `git diff --check` 通过，真实 PG 行为仍未验。
- 待完成：评估 claim/episode/原子 projection 去重；补齐实际 `ResponsePlannerClient` 超时、fallback 和生产 catalog 限额证据；之后才考虑部署当前工作区，并做线上带鉴权与设备验收。把会话记忆迁到当前 person/subject 键、覆盖切人、撤销、删除和旧缓存的工作仍不提前宣称完成。
- 完成条件：固定集与未见集分别报告 recall/nDCG/extraction/leakage，candidate 语义、projection 去重、实际超时/catalog 限额和 person/subject 隔离均有可复核证据；当前仅 candidate 契约和两组隔离评测达成，线上/设备边界仍未达成。设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button/keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别/打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

### [ ] P1-11 一台设备只服务一个使用人：声纹下线，信任与同意来自绑定（2026-09-25，分支 `feat/bound-subject-trust`，未推送、未部署）

- 产品决定（用户 2026-09-25）：暂不用声纹（09-03 起生产 0 次 owner_match，主人得分 0.41–0.67 对门槛 0.78，profile 未评估）；设备只服务绑定时选定的使用人（孩子/老人/本人）。家长只看摘要、趋势、风险提醒，不看孩子原文；孩子的长期记忆需家长在绑定时勾选（默认不勾、非必选）；实名家长声明即监护关系；老人记忆由子女代为同意（如实记为代理）；同意长期有效直到撤销；解绑撤销同意并询问是否删除。
- 已做（本地 + 临时 PG）：Agent 在无声纹的设备会话上以签名 profile 为据给出 `device_bound_subject` 主人（仅数据权限，`current_speaker_authority_verified` 仍为假，回声/打断门不变），Control 用同一 profile 核验；播放后 3s 内机器人自己说过的告别词不能结束会话；策略 `subject_presence=device_bound`（家长 App 切到孩子仍读不到孩子记忆）；绑定时写入同意权威授予（guardian/subject/新 `delegate`），认定关系 `guardian_attestation_v1`/`delegate_attestation_v1`，老人登记为经绑定人认定的成年人（`child_for_parent` 此前根本无法绑定）；家长页开关双写、解绑撤销、换版延续；无账号孩子可由绑定人导出；生产 env 模板关闭 `MEMORIA_SPEAKER_AUTHORITY_ENABLED`；小程序隐藏声纹入口、绑定勾选与解绑/导出/删除入口。顺带修复：任何已生效关系都会让 profile 落库复核指纹不一致（收据只锁选中的关系）。
- 按使用人删除（2026-09-25 同分支）：可续跑、有进度记录的删除流程（删除期间拒收该使用人的新证据），依次关闭该使用人的设备会话、删归档证据及其派生记忆（含引用了 TA 的合并条目）、文件对象、危机提醒与学习记录、MemoryScope（新维护角色 `memoria_memory_maintenance` 专用函数，只增不改约束对其他调用方不变）、语料；解绑并选删除时再把 TA 的身份隐去为占位并清掉审计中的旧名；最后逐库核对为空。保留的仅有无内容审计（同意记录、策略收据、关系/绑定、会话档案）。部署前须在 `/etc/memoria-postgres.env` 加 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`，控制面 env 加 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`。已知残留：已推送到 Redis 的记忆事件无法撤回（随流修剪老化）；`speech_style_stats` 聚合、persona/digital-self 快照、`entity_ids` 数组无行级来源可追。
- 未做：已有生产绑定的旧"待确认"声明不自动升级（目前无真实用户）；声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。
- 完成条件：设备上孩子/老人/本人三种绑定各一次：隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声"再见"不结束会话、真人"再见"能结束；家长端看不到孩子原文；撤销后不再记忆。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集/未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备/真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`agent.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-02 多成员声纹与不依赖小程序的选人

- 2026-09-25 暂停：产品改为一台设备一个使用人、声纹下线（P1-11）。重启前须先用真实标注样本校准误识/拒识，再定门槛。
- 先确定识别人范围、单独同意/撤销和设备确认交互；owner-only 登记不能区分家人。
- 完成条件：逐人登记、冲突/不确定/访客隔离、撤销与审计有 PG 和设备证据；识别只提出候选，不升级为 owner 或敏感授权。

### [ ] P2-03 可证明删除与导出证据链

- 已完成（本地，收据见 `HANDOFF.md`）：四迁移读路径的 PG 侧对等（operator `read --postgres-dsn`，真实 PG 契约；投影侧 PG 未建表时 fail closed 不回落账号键）；PG 全 saga 删除验证（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）。
- 待完成：真实 MinIO 版本删除（本地 Docker MinIO 对象写入不可用，需可用 MinIO 或生产环境）、真实 provider 删除（需密钥与授权）、备份「恢复后再删除」实现与期限声明、物理不可删域已决策不做封存（2026-09-20，明文残留与不可归属面为已知缺口）、结构盲区补齐（`memory_scope`/`session_runtime`/`policy_receipts_v2`/`identity_*`/`device_fleet_*`/`device_onboarding_*`/binding consent 不在删除 saga；`remaining_account_rows` 需覆盖非 `account_id` 键表。其中 `memory_scope` 的可行路径已核实：`memory_owner_records`/`memory_owner_status_events` 对 `memoria_memory_owner` 是 `FOR ALL`（`pg_has_role(...,'member')`，NOLOGIN，维护登录 `SET ROLE` 即可），RLS 层允许 owner 角色删，缺的是应用侧删除路径与清点，不是权限；开工前需先定：哪些行属于删除范围（`resource_owner_id` vs `subject_id`、`family_shared` 行的族空间归属、`co_subject_ids` 共同主体）、`memory_status_events` 先于 `memory_records` 的顺序、删除 fence 与收据/幂等语义。小程序成员主体读口（需客户端会话上下文，属 P1-03/P1-05）、生产/设备与真实机器人对话验收。
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

### [ ] P2-07 Prompt 审计遗留（2026-09-24 审计，暂不修改）

- 审计口径：全仓发给模型的文本；实际模型为 DeepSeek V4 Flash（主对话，百炼）、`qwen-flash`（四个语义分类器/人格结构化）、`qwen-plus`/`qwen3.7-flash`（抽取/联网）。判据源自 Claude 文档，对这些模型只算经验判断，置信度最高为“中”；均未调用真实模型验证。
- 中-1 身份规则双版本矛盾：`services/agent/src/prompts.py:12-15` 的旧“不得自称或讨论 AI”规则仍在 `SAFETY_CORE`/`VOICE_SYSTEM_PROMPT` 中，并经 `tutor_session.voice_system_prompt` 与 `Orchestrator` 默认 `ContextManager`（`scripts/run_e2e.py`）可达；生产透明版靠 `prompts.py:74` 的 `.replace()` 生成，`SAFETY_CORE` 改一字即静默回退为隐藏版。拟改：`AI_IDENTITY_RULE_TRANSPARENT` 作为唯一规则拼入 `SAFETY_CORE`，`SAFETY_CORE_TRANSPARENT` 保留为别名（生产文本逐字节不变，已在临时副本验证）。
- 中-2 口癖禁用词表：`prompts.py:34` 列举“首先、其次、最后/综上所述/希望以上内容对你有帮助”，无来由（首次提交即有），列出原词可能反向锚定。拟改为正面表述“像当面聊天一样自然衔接，不用书面报告式的分点连接词、总结句或客服式结束语。”；会改变生产陪伴提示词。
- 中-3 小结指令语义歧义：`services/control_api/app/routes/memory.py:293`“也不要输出敏感信息之外的推断”字面可读成允许推断敏感信息。拟改为“只写聊天中实际出现的内容：不编造事实，也不推测对方没有明说的健康、财务、关系等敏感信息。”（本意需确认）。
- 中-4 人格抽取规则重复：`services/persona/qwen_extractor.py:66` 与 `:68` 同一规则两种措辞（“永久性格”/“稳定风格”）；拟删 66 行分句，保留更精确的 68 行。
- 低-5 仅标记：`services/agent/src/providers/qwen_realtime_search.py:90` 发往 DashScope 只带 `thinking: {type: disabled}`，而其余 DashScope 调用都带 `enable_thinking: False`（`handlers.py:82-83`、四个分类器）；需对照百炼文档或抓响应确认后再决定是否补齐。
- 低-6 仅标记：三个 Qwen JSON 抽取器用 `response_format: json_object` + pydantic 兜底；若当前 Qwen 支持 `json_schema` 结构化输出可再评估，现状可用。
- 已核实保留：分类器“只输出一个枚举词”（精确解析、`max_tokens` 12）、半双工 120 字规则（`agent.py:93` 代码截断）、禁 Markdown（TTS）、禁笑/咳嗽标签（CosyVoice 支持且 `prosody.py:29` 过滤）、导师不给答案、危机/暴力固定话术、`context_assembler` 指令/数据分离、小结字数上限（pydantic 校验）。
- 完成条件：1–4 逐条单独落地；2 需在 DeepSeek V4 Flash 上用真实语音样本前后对照，3/4 跑 `scripts/evaluate_memory.py` 或小样本对照；语音/导师/prompt composition/persona/小结相关测试通过（临时副本上已通过，1 skip）。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：真实家庭服务/数据、正式发布或价值量级增长前重评；WAL 仅按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收；启用另定授权和渠道。
- EOU 新模型、DuplexModel、expressive/抢跑；ESP-IDF/ESP-SR/upstream 整体升级；老人故事册/人物复刻、年轻人潮玩和最终外形均不进入当前队列。

- F1/F2 已发布（tag `20260920-f1f2-owner-silence-and-barge`）；F1 有 2026-09-21 设备窗口的 owner-silence 观察，F2 仅有 `button.stop` happy path 证据，禁止源 barge 未真实触发，不能宣称完整 F1/F2 契约已完成；修好模拟音频工具的两处自检判据（live 缓冲窗口、参考波形匹配）后跑自动问答扫频仍开放。

- 身份收敛（发布治理）：control-api 期望的 release tag 应与真实发布 tag 一致，取消"agent 上报历史冻结 tag"的临时对齐；与预构建镜像入口一并作为 P1-01 输入。

---

## 历史材料索引（非执行队列）

市场、融资、BP、产品战略和架构简化讨论已迁移至 [历史战略与融资分析](docs/strategy/fundraising-analysis-20260921.md)。
该文件不构成当前工程执行项、发布门禁或完成证据。
