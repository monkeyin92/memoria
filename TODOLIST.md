# Memoria 优先级执行清单

更新于 2026-09-26｜线上为 2026-09-25 整栈发布 `20260925-full-stack-v1`（main `064ed61`，保留豆包 TTS、fun-asr）加 control-api 单组件 `20260925-device-mascot-sync`（`9f02619`）；候选可见性（main `0059368`）、D1/D2（`48e34bc`/`c979f4e`）、readiness 刷新修复（`d1f05cf`）、P1-11 一对一绑定信任与按使用人删除（PR #29）均已随整栈上线，旧记录中的 `f7c4c2a`/`200d52e`/`3eede2f` 是合并前哈希。2026-09-26 减法整理（PR #42，main `00bb4b2`）只改仓库、未部署。本文件只保留未完成事项、执行边界和验收条件，完成收据归 `HANDOFF.md`。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 20260925-full-stack-v1  # main 064ed61；agent/bridge、device-media-gateway、miniprogram-gateway、speaker-model 均为该 tag；ASR fun-asr-realtime，TTS Doubao；回滚 *:rollback-20260925-full-stack-v1-pre
control_api_release: 20260925-device-mascot-sync  # main 9f02619，单组件发布，栈身份仍为 full-stack-v1；回滚 memoria-control-api:rollback-20260925-device-mascot-sync-pre-control
media_edge_release: 20260920-f1f2-owner-silence-and-barge  # d61d486；下次发布含 PR #42 的启动收紧，见 P1-12
control_api_release_lane: 可审计链已多次用通（`scripts/deploy_control_component.sh` 的 cutover 块）；整栈走 `scripts/release_ops.sh`（服务器副本 `/root/memoria-release/release-ops.sh`），缺口见 P1-12
memory_candidate_visibility: code=main 0059368 / enabled=true（随整栈上线）/ verified=SQLite/HTTP/主体隔离/评测适配器回归；四份 2026-09-23 评测收据为上线前 parent_baseline（固定集 recall@5/10=0.857、未见集 0.4、双泄漏 0），真实 PG candidate 行为与线上带鉴权读口未单独取证
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
subject_scope_batch: code=已提交 / wired=应用读出口按主体过滤 / enabled=未启用 / verified=本地 SQLite 与临时 PostgreSQL 回归
account_to_subject_migrations: code=四项已提交（含 operator 执行入口与按 subject 的只读出口）/ wired=人格改为按使用人学习与读取（2026-09-26 代码，未部署，见 P1-03），账号→主体的一次性投影迁移不再是产品读路径 / enabled=未启用 / verified=本地专测与真实 PG 契约
read_path_postgres_parity: code=已提交 `d2318e4`（CI `35501188784` success）/ wired=仅 operator CLI 可达，Control API 不导入迁移接缝 / enabled=未启用 / verified=真实 PG 契约（durable_subject/memory_scope 读 PG 真实行，archive 证据读走 `app.account_id` 上下文、无 account 且在 FORCE RLS 下拒绝；投影侧 PG 未建表时 fail closed，不回落账号键）
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. P2-03 剩余：读路径的 PG 侧对等已落地（operator `read --postgres-dsn [--account <id>]`，真实 PG 契约）；删除范围验证完成本地部分（PG 全 saga 行/对象/厂商桩/声纹 + 收据幂等）。仍未验/未做：真实 MinIO 版本删除（本地 Docker MinIO 对象写入不可用，需可用 MinIO 或生产环境）、真实 provider 删除（需密钥与授权）、备份「恢复后再删除」实现与期限声明，以及新发现的结构盲区（`memory_scope`/`session_runtime`/`policy_receipts_v2`/`identity_*`/`device_fleet_*`/`device_onboarding_*`/binding consent 不在删除 saga，`remaining_account_rows` 只统计含 `account_id` 列的表）。小程序读口核验结论：各读口读的是数据所在存储（控制库 `MEMORIA_DB_PATH` 生产即 SQLite，archive/persona/digital-self/personas/growth 走 PG），archive 读口按调用者本人主体过滤；成员主体读口需客户端会话上下文，属 P1-03/P1-05。生产/设备验收仍待授权。
2. P0-03 核心续问与工具查询最终回答已在 09-24、09-25 真机走通；下一次设备窗口先补 TLS/WSS 重连观察，再继续 >45s/B/D 长答、部分下发失败、待机/表情及点屏/摇晃/短拍/BOOT 矩阵，同时做 P1-11 三种绑定验收。不得把核心通过扩大为完整 P0-03 或全双工通过。
3. 生产切流、回滚演练和制品清理须另获授权；设备功能通过不等于学生安全或全双工通过。
4. P1-08 WAL 可独立只读测量；删除、重启、定时任务、自动备份和异地副本不在当前授权内。
5. P1-12：修整栈发布脚本两处缺陷；旧媒体链（Python 设备媒体网关、小程序网关、LiveKit）是否下线、何时发布 media-edge 需用户决定。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 待完成：建后年龄资料与 `app_confirm` UI、guardian consent 决策接口、会话记忆按 subject 键迁入 `services/memory_scope`，以及安全专项设备链（身份/年龄→有效同意→准入或受限能力→固定话术真实交付→outbox 绑定/幂等/家长读回）。发送 worker/外部投递暂缓。
- 软件门：两条策略入口覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、同意撤销/过期/无权限、管理账号更换与切人并发；不能决定时 fail closed，且不得读取其他主体私密记忆。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code/wired/enabled/verified`；`student_safety_loop_verified=false` 保持到安全专项设备链通过。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/{identity,session_runtime,guardian}/`。

### [ ] P0-03 TTS、续问竞态、设备停滞与真实时延

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

- P0 收口后冻结同一 source/lock，跑 ruff、module budget、strict mypy、协议生成、真实 PG init/repeat-upgrade、全量 pytest/覆盖率、Offline E2E、真实 exporter 隐私门和受影响镜像门；DSN-gated skip 不能当通过。
- 用标准全量构建复验 Agent/Bridge 同一产物、非 root 运行与真实环境变量；按授权切流，复核 12 个 readiness、外部路由、provider/LiveKit、设备与时延，并实际演练紧邻回滚。
- 完成条件：同一候选的构建、切流、回滚、设备验收分别有证据；仅保留当前和一个可运行回滚，清理生产另授权。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧容量预测不再复用。
- 需用户决定并授权裁剪、停用 `archive_mode`（需重启）或手工阈值策略；不得按 mtime 删除或清理 `pg_wal` 代替归档保留。
- 完成条件：保护集合、容量/恢复影响、执行证据和后续责任明确，保留恢复目标可验证。

### [ ] P1-09 readiness 定时刷新失败可见性；发布链缺口待补（2026-09-22 线上发现）

- **缺陷（已复现，非切流引入）**：`memoria-readiness-refresh.timer`（每 12h）自 2026-09-22 00:08 CST 起持续失败（systemd `status=1/FAILURE`，restart counter 3 后放弃），原因是 `/opt/memoria/current/scripts/refresh_readiness.sh` 只 `export MEMORIA_RELEASE_TAG`，而 09-21 的 agent 组件切流把 agent 的 base 换成仓库快照（`/opt/memoria/releases/20260921-defect-a-base/docker-compose.production.yml`），该 base 对 `speaker-model.build.args.MEMORIA_RELEASE_COMMIT` 用 `:?` → `docker compose config --format json` 直接报错、stdout 非 JSON → 脚本内联解析抛 `JSONDecodeError`。后果：`readiness_evidence` 中 `20260901-0945-wake-word-whitelist` 打点停在 2026-09-21T04:05Z，超过 `READINESS_GATE_TTL_S=86400` 后全栈 `/health/ready` 变 `not_ready`（core 12 项与 agent 均 ready，仅 `smokes: expired`）。
- **已修复并部署（2026-09-22）**：脚本按 tag 同一模式从 control 容器 env 解析并导出 `MEMORIA_RELEASE_COMMIT`（空值告警不静默）；`require_live_service_image` 保留并打印 Compose 的 stderr、解析失败改为可读报错；回归 `scripts/tests/test_refresh_readiness_contract.py`。修复版部署到 `/opt/memoria/current/scripts/refresh_readiness.sh`（sha `c0152d5b…`→`89c27afc…`，备份 `…/readiness-fix/refresh_readiness.sh.pre-fix`），并以真实 systemd 单元验证 `Result=success`/`ExecMainStatus=0`、readiness 200、证据 `marked_at` 刷新（收据 `readiness-refresh.txt`、`readiness-fix-deploy.txt`、`readiness-fix-verify.txt`）。
- 已完成：修复（main `d1f05cf`）随 2026-09-25 整栈发布带入，`/opt/memoria/current` 已指向新发布树，发布后定时单元 `Result=success`。
- 待完成：让失败可见（timer 失败目前只有 journal，readiness 到期才发现）。
- **发布链缺口（同日实测，供 P1-01）**：`deploy_control_component.sh` 的 cutover 块要求线上链为「base commit 的仓库 compose 快照 + `component-releases/` 内 image-only YAML 覆盖」，而线上 control-api 实际链是 `20260827 树 compose + /tmp/media-runtime.override.yml + control-api.override.json` → **任何变更前就 fail-closed**；且 (a) 无「已构建候选续跑」入口（release 目录已存在即拒绝，target 镜像已存在即拒绝重建），(b) image-only 覆盖无处承载身份 env（agent 侧的 `agent-component.override.yml` 是带 env 的非 image-only 覆盖），(c) 解析后服务配置与旧链完全相同时 Compose 不重建、`config_files` 标签不更新（归一化必须显式 `--force-recreate`），(d) 新链不校验挂载/端口/其它容器（demo02 旧工具校验了）。本轮以「同镜像强制重建归一化 + 逐字执行 cutover 块」通过，见 HANDOFF 历史归档 `docs/HANDOFF-archive-0916-0923.md` 同日节。

### [ ] P1-02 让 ASR 救援 sidecar 可复现并验证真实输入

- 待完成：把生产 sidecar 的启动脚本、Dockerfile、模型/词表摘要和基础镜像输入纳入仓库；用真实中文 PCM 验证短句/尾字、长段、低 RMS、静音、削波、并发、失败降级与 2.5s 预算。
- 约束：主链是云服务商 FunASR，救援后端单独核验；不把本地 FunASR PyPI、仓内 sherpa-onnx 或云模型版本混为一体，也不顺手改 NumPy/设备 VAD。
- 完成条件：仓内可重建，分报 vendor_error/vendor_silent/gating/low_rms；有收益且无回归后才另行切流。

### [ ] P1-03 修稳成员入口，再接按使用人切人格/音色

- 已完成（2026-09-26，代码，未部署）：人格跟随使用机器的人（用户决定）。给孩子用学孩子的人格，给老人用学老人的人格。人格存储按（绑定账号，使用人）记账：SQLite 与 PostgreSQL 引擎的特征、风格统计、版本表加 `subject_id`，现有数据回填为账号本人、ID 不变，控制面启动时幂等迁移（线上表归 `memoria_app`，现有 5 条特征、0 个版本）。学习：账号本人仍用账号的人格学习同意；其他使用人复用绑定时勾选的长期记忆（签名 profile 对该使用人确认且带 `memory_recall_private`），调度移入 `services/control_api/app/persona_learning.py`。读取：response-plan/context-prefetch 经 `subject_persona.py` 向引擎按使用人读取，其他使用人同样要求长期记忆授权，撤销后不再读出。按使用人删除流程新增 `persona_forgotten` 步骤清除该使用人的人格。
- 已知风险（用户决定）：未成年人与成人一样完整学习人格特征（含价值观、决定等可画像特征），而 P0-04 学生安全闭环尚未完成；对外发布前需在 P0-04 中复核这一口径。
- 待完成：孩子/老人人格的查看与审核入口（现有 `/v1/persona/traits` 等只看账号本人的人格）；设备验收：同一台设备绑定孩子后隔天体现孩子自己的表达风格，账号本人的人格不串入。
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
- 待完成：评估 claim/episode/原子 projection 去重；补齐实际 `ResponsePlannerClient` 超时、fallback 和生产 catalog 限额证据；再做线上带鉴权读口与设备追问验收。把会话记忆迁到当前 person/subject 键、覆盖切人、撤销、删除和旧缓存的工作仍不提前宣称完成。
- 完成条件：固定集与未见集分别报告 recall/nDCG/extraction/leakage，candidate 语义、projection 去重、实际超时/catalog 限额和 person/subject 隔离均有可复核证据；当前 candidate 契约已上线、两组隔离评测为上线前基线，线上/设备边界仍未达成。设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button/keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别/打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

### [ ] P1-11 一台设备只服务一个使用人：声纹下线，信任与同意来自绑定（PR #29 已合入并随 2026-09-25 整栈上线，设备验收未完成）

- 产品决定（用户 2026-09-25）：暂不用声纹（09-03 起生产 0 次 owner_match，主人得分 0.41–0.67 对门槛 0.78，profile 未评估）；设备只服务绑定时选定的使用人（孩子/老人/本人）。家长只看摘要、趋势、风险提醒，不看孩子原文；孩子的长期记忆需家长在绑定时勾选（默认不勾、非必选）；实名家长声明即监护关系；老人记忆由子女代为同意（如实记为代理）；同意长期有效直到撤销；解绑撤销同意并询问是否删除。
- 已做并上线（本地 + 临时 PG 验证，生产 `MEMORIA_SPEAKER_AUTHORITY_ENABLED` 已于 09-25 改为 false）：Agent 在无声纹的设备会话上以签名 profile 为据给出 `device_bound_subject` 主人（仅数据权限，`current_speaker_authority_verified` 仍为假，回声/打断门不变），Control 用同一 profile 核验；播放后 3s 内机器人自己说过的告别词不能结束会话；策略 `subject_presence=device_bound`（家长 App 切到孩子仍读不到孩子记忆）；绑定时写入同意权威授予（guardian/subject/新 `delegate`），认定关系 `guardian_attestation_v1`/`delegate_attestation_v1`，老人登记为经绑定人认定的成年人（`child_for_parent` 此前根本无法绑定）；家长页开关双写、解绑撤销、换版延续；无账号孩子可由绑定人导出；生产 env 模板关闭 `MEMORIA_SPEAKER_AUTHORITY_ENABLED`；小程序隐藏声纹入口、绑定勾选与解绑/导出/删除入口。顺带修复：任何已生效关系都会让 profile 落库复核指纹不一致（收据只锁选中的关系）。
- 按使用人删除（2026-09-25 同分支）：可续跑、有进度记录的删除流程（删除期间拒收该使用人的新证据），依次关闭该使用人的设备会话、删归档证据及其派生记忆（含引用了 TA 的合并条目）、文件对象、危机提醒与学习记录、MemoryScope（新维护角色 `memoria_memory_maintenance` 专用函数，只增不改约束对其他调用方不变）、语料；解绑并选删除时再把 TA 的身份隐去为占位并清掉审计中的旧名；最后逐库核对为空。保留的仅有无内容审计（同意记录、策略收据、关系/绑定、会话档案）。部署前须在 `/etc/memoria-postgres.env` 加 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`，控制面 env 加 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`。已知残留：已推送到 Redis 的记忆事件无法撤回（随流修剪老化）；`speech_style_stats` 聚合、persona/digital-self 快照、`entity_ids` 数组无行级来源可追。
- 未做：已有生产绑定的旧"待确认"声明不自动升级（目前无真实用户），用户需重新绑定设备以写入绑定时 consent grant 后再验证 `memory_recall_private`；监护小结 consent 仍无写入方；声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。
- 完成条件：设备上孩子/老人/本人三种绑定各一次：隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声"再见"不结束会话、真人"再见"能结束；家长端看不到孩子原文；撤销后不再记忆。

### [ ] P1-12 发布工具缺陷、media-edge 下次发布与旧媒体链去留（2026-09-26）

- 整栈发布脚本两处缺陷已修（2026-09-26）：脚本入库为 `scripts/release_ops.sh`（回归 `scripts/tests/test_release_ops_script.py`）。`finish`/`rollback` 的状态列表与 `wait_healthy` 对无 healthcheck 容器输出 `no-healthcheck`，不再中断，`finish` 遇 unhealthy/starting 失败关闭；`env` 在候选 agent 与 bridge 内断言 `MEMORIA_SPEAKER_AUTHORITY_ENABLED` 为关闭值。新模板已在线上对 12 个容器只读验证。待完成：下次整栈发布前把脚本内的线上链常量（`OLD`、`LIVE_CONTROL_RELEASE`、回滚 compose 链）改为届时线上链，再安装到服务器 `/root/memoria-release/release-ops.sh`；服务器现存副本未替换。
- media-edge 下次发布：新镜像含 PR #42 的启动收紧（`MEDIA_EDGE_WEBRTC_ENABLED=true`、`go_shadow`/`go_authoritative`、生产未开设备 WSS 均拒绝启动）；compose 已固定 `MEDIA_EDGE_DEVICE_WSS_ENABLED: "true"`，发布后把易失的 `/tmp/media-runtime.override.yml` 移出 compose 链。发布须另获授权。
- 旧媒体链去留（需用户决定）：Python 设备媒体网关（8793）、小程序网关与 LiveKit 在 2026-09-26 只读检查时过去 24 小时零业务流量，但仍部署且属于回滚链；下线等于关闭回滚窗口，需同步删 compose 服务、nginx 路由、镜像与发布脚本中的角色。
- persona 死链路已删（2026-09-26）：agent 侧 `MEMORIA_PERSONA_{ENABLED,CAPSULE_URL,READ_TOKEN,TIMEOUT_S,CACHE_TTL_S}` 配置与校验、控制面 `/v1/persona/session-capsule` 端点及 `persona_read` 内部 token（生产要求的能力 token 由十个降为九个）、env 模板与升级生成脚本中的对应项；`split_production_env.py` 接受但不分发这些已退役变量，现有 env 文件无需改动。人格学习测试改为经 `persona_engine.capsule` 读取（与现役 response-plan 同一路径）。
- session-context 死链路已删（2026-09-26）：控制面 `/v1/archive/session-context`（调用方是早已删除的 agent MemoryContextClient）及其专用 `memory_read` 内部 token（生产要求的能力 token 再降为八个）；`MEMORIA_MEMORY_READ_TOKEN` 从 env 模板与升级生成脚本移除，`split_production_env.py` 接受但不分发。记忆目录 `context()` 仍由 response-plan 与评测使用，保留；原经该端点覆盖的主体隔离断言改由 response-plan/context-prefetch 两条现役路径承担。
- 完成条件：脚本入库且两处缺陷有回归（已达成）；下次整栈发布使用仓库版本；media-edge 发布与 `/tmp` 移除有切流收据；旧媒体链有明确决定并按决定执行。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集/未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备/真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`agent.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-03 可证明删除与导出证据链

- 已完成（本地，收据见 `docs/HANDOFF-archive-0916-0923.md`）：四迁移读路径的 PG 侧对等（operator `read --postgres-dsn`，真实 PG 契约；投影侧 PG 未建表时 fail closed 不回落账号键）；PG 全 saga 删除验证（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）。
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

### [ ] P2-08 架构整理后续（2026-09-26 审计，减法优先）

- 已完成（PR #42）：删除无消费者代码约 3.09 万行；行数预算覆盖全部超 1,500 行模块；跨包依赖图冻结。
- 待完成，按收益排序：① 账号/会话/设备从生产 SQLite（`/data/memoria.sqlite3`）迁到 PostgreSQL，再逐域删除 SQLite 孪生存储（约 4 万行），API 测试改走真实 PG；② 单一装配根替代 `main.py` 的双重装配，存储改为共享连接池；③ 版本化迁移替代 `initialize()` 内建表；④ 先把重度依赖私有字段的测试迁到公开接口，再拆 `DuplexRuntime` 与媒体会话 registry；⑤ 逐步消除 `common`→`agent`/`archive`、`governance`/`memory_scope`→`control_api` 等反向依赖。
- 约束：①③涉及生产数据迁移，须另获授权并先演练恢复；不做大爆炸重写，每步可独立发布与回滚。
- 完成条件：每步有行数与依赖图基线收紧的证据，生产切换有收据。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：真实家庭服务/数据、正式发布或价值量级增长前重评；WAL 仅按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收；启用另定授权和渠道。
- Qwen-Audio 3.1 ASR/TTS（原 P1-10，已评估结论）：ASR 两次真机对照均失败（播放期回声被提交为话轮、漏识别轻声、“稍等”开口延迟 4.1s），生产保持 fun-asr；TTS 迁移按用户 2026-09-25 决定回退、生产保持豆包，重新启用即再 revert 回退提交 `d0d7a43` 与 `2be2f50`。重试前先定位新模型 VAD/段落与回声边界，并在新候选上重做缺陷 A 设备验收。收据 `docs/acceptance/run-20260924-d1d2-deploy/findings.md`。
- 多成员声纹与不依赖小程序的选人（原 P2-02）：随 P1-11 一对一绑定暂停；重启前须用真实标注样本校准误识/拒识后再定门槛。
- EOU 新模型、DuplexModel、expressive/抢跑；ESP-IDF/ESP-SR/upstream 整体升级；老人故事册/人物复刻、年轻人潮玩和最终外形均不进入当前队列。

- F1/F2 已发布（tag `20260920-f1f2-owner-silence-and-barge`）；F1 有 2026-09-21 设备窗口的 owner-silence 观察，F2 仅有 `button.stop` happy path 证据，禁止源 barge 未真实触发，不能宣称完整 F1/F2 契约已完成；修好模拟音频工具的两处自检判据（live 缓冲窗口、参考波形匹配）后跑自动问答扫频仍开放。

- 身份收敛（发布治理）：control-api 期望的 release tag 应与真实发布 tag 一致，取消"agent 上报历史冻结 tag"的临时对齐；与预构建镜像入口一并作为 P1-01 输入。

---

## 历史材料索引（非执行队列）

市场、融资、BP、产品战略和架构简化讨论已迁移至 [历史战略与融资分析](docs/strategy/fundraising-analysis-20260921.md)。
该文件不构成当前工程执行项、发布门禁或完成证据。
