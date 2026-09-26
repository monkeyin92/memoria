# Memoria 优先级执行清单

更新于 2026-09-26｜线上为整栈发布 `20260926-edge-flush-v1`（main `fa8a97d`，含 PR #47–#50；media-edge 同版本单独切换），上一栈 `20260926-persona-subject-v1` 为回滚目标。本次上线按使用人删除补缺与恢复后重放、提示词身份规则统一、人格查看接口、单一装配根与 archive 库共享连接池、media-edge 关闭前送达 `session.error` 与 Opus 编解码器加锁、readiness 刷新逾期提示；设备验收尚未执行。本文件只保留未完成事项、执行边界和验收条件，完成收据归 `HANDOFF.md`。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 20260926-edge-flush-v1  # main fa8a97d；agent/bridge、control-api、两个网关、speaker-model 与 media-edge 均为该 tag；ASR fun-asr-realtime，TTS Doubao；回滚 *:rollback-20260926-edge-flush-v1-pre，media-edge 回 20260926-persona-subject-v1
control_api_release_lane: 可审计链已多次用通（`scripts/deploy_control_component.sh` 的 cutover 块）；整栈走仓库版 `scripts/release_ops.sh`（已安装到服务器 `/root/memoria-release/release-ops.sh`，2026-09-26 `20260926-edge-flush-v1` 全链 PASS；仓库版常量已指向本次链）
memory_candidate_visibility: code=main 0059368 / enabled=true（随整栈上线）/ verified=SQLite/HTTP/主体隔离/评测适配器回归；四份 2026-09-23 评测收据为上线前 parent_baseline（固定集 recall@5/10=0.857、未见集 0.4、双泄漏 0），真实 PG candidate 行为与线上带鉴权读口未单独取证
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
subject_scope_batch: code=已提交 / wired=应用读出口按主体过滤 / enabled=未启用 / verified=本地 SQLite 与临时 PostgreSQL 回归
account_to_subject_migrations: code=四项已提交（含 operator 执行入口与按 subject 的只读出口）/ wired=人格改为按使用人学习与读取（2026-09-26 已上线，见 P1-03），账号→主体的一次性投影迁移不再是产品读路径 / enabled=未启用 / verified=本地专测与真实 PG 契约
read_path_postgres_parity: code=已提交 `d2318e4`（CI `35501188784` success）/ wired=仅 operator CLI 可达，Control API 不导入迁移接缝 / enabled=未启用 / verified=真实 PG 契约（durable_subject/memory_scope 读 PG 真实行，archive 证据读走 `app.account_id` 上下文、无 account 且在 FORCE RLS 下拒绝；投影侧 PG 未建表时 fail closed，不回落账号键）
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. 真机窗口（用户推动）：⓪ 先重新绑定设备并勾选长期记忆（现有绑定早于绑定授权，对话不入档），再按 HANDOFF 验收清单验 P1-11 三种绑定、P1-03 孩子人格隔天生效、P2-04 终止性拒绝不再续连，以及 P0-03 的 TLS/WSS 重连与剩余设备矩阵。不得把核心通过扩大为完整 P0-03 或全双工通过。
2. 可直接推进的代码项：P0-04 代码部分（先列需用户拍板的产品问题）、P1-02 救援 sidecar 可复现、P1-04 自定义声音闭环、P2-08 ③ 版本化迁移、P2-06 回放评测、P2-04 Python 侧进程退出注入。
3. 需用户决定：P0-04 八项产品决定（`docs/compliance/p0-04-minor-safety-decisions.md`）、P1-02 两项线上调整、旧媒体链去留（P1-12）、WAL 保留策略（P1-08）、readiness 逾期的告警渠道（P1-09）、P2-07 第 2/3 项、P2-03 已知缺口是否接受、P0-04 未成年人人格学习口径。
4. 边界：生产切流、回滚演练和制品清理须另获授权；删除、重启、定时任务、自动备份和异地副本不在当前授权内；设备功能通过不等于学生安全或全双工通过。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 待决（2026-09-26 整理）：实现前需用户拍板 8 项产品决定（年龄分档、未成年人人格学习范围、时长与夜间时段是否强制、年龄确认与变更、年龄不明时的辅导、「每周小结」勾选项、危机提醒投递、危机话术审核），见 `docs/compliance/p0-04-minor-safety-decisions.md`；文档同时列出现状核查与无需决定的工程项。
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

### [ ] P1-09 readiness 刷新失败的主动告警（刷新修复与逾期提示已上线）

- 已上线：刷新脚本修复（2026-09-22，收据见 `docs/HANDOFF-archive-0916-0923.md`）；逾期提示（`20260926-edge-flush-v1`）：证据超过刷新间隔 12h + 1h 宽限未更新时，`/health/ready` 报 `smokes: "overdue"` 与 `warnings: ["smoke_refresh_overdue"]`，状态仍为 ready；间隔常量与 `infra/memoria-readiness-refresh.timer` 有一致性回归。
- 待完成：主动告警渠道（需用户定渠道与授权）；目前逾期只在有人或脚本读取 readiness 时可见。
- 发布链缺口（供 P1-01）：组件发布脚本 `deploy_control_component.sh` 的 cutover 块缺已构建候选续跑入口，image-only 覆盖无法承载身份 env，配置不变时需显式 `--force-recreate`，且不校验挂载/端口（细节见 `docs/HANDOFF-archive-0916-0923.md` 2026-09-22 节）；整栈发布已改走 `scripts/release_ops.sh`。

### [ ] P1-02 ASR 救援 sidecar：两项线上调整待授权（可重建与验证已完成）

- 已完成（2026-09-26，代码与文档，见 `docs/runbooks/sensevoice-asr.md`）：`infra/sensevoice-asr/` 入库 Dockerfile（基础镜像按 digest 锁定，与线上逐层一致）、哈希锁定的 17 个依赖（即线上 `pip freeze`）与模型校验值；重建镜像的依赖、Python 版本、脚本与线上一致，4 段合成中文语音在本地 amd64/arm64 与线上实例上转写逐字相同。`scripts/evaluate_sensevoice_rescue.py` 用 12 段真机录音切出的 69 句评测：无外文输出，降 20 dB/8 倍削波后相似度 0.971/0.984，4 路并发与串行一致。空结果分类（`vendor_error`/`vendor_silent`/`gating`/`low_rms`）已由 `funasr_empty_accounting.py` 提供。
- 已修（代码，未部署）：模型对静音和任意噪声都返回「我。」，原先 `min_text_chars=2` 按原始长度计数让它成为一轮用户输入；现只计文字字符（`SenseVoiceRescueConfig.accepts_text`），回归覆盖。
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
- 待完成：评估 claim/episode/原子 projection 去重；补齐实际 `ResponsePlannerClient` 超时、fallback 和生产 catalog 限额证据；再做线上带鉴权读口与设备追问验收。把会话记忆迁到当前 person/subject 键、覆盖切人、撤销、删除和旧缓存的工作仍不提前宣称完成。
- 完成条件：固定集与未见集分别报告 recall/nDCG/extraction/leakage，candidate 语义、projection 去重、实际超时/catalog 限额和 person/subject 隔离均有可复核证据；当前 candidate 契约已上线、两组隔离评测为上线前基线，线上/设备边界仍未达成。设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button/keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别/打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

### [ ] P1-11 一台设备只服务一个使用人：声纹下线，信任与同意来自绑定（已上线，设备验收未完成）

- 产品决定（用户 2026-09-25）：暂不用声纹（09-03 起生产 0 次 owner_match，主人得分 0.41–0.67 对门槛 0.78，profile 未评估）；设备只服务绑定时选定的使用人（孩子/老人/本人）。家长只看摘要、趋势、风险提醒，不看孩子原文；孩子的长期记忆需家长在绑定时勾选（默认不勾、非必选）；实名家长声明即监护关系；老人记忆由子女代为同意（如实记为代理）；同意长期有效直到撤销；解绑撤销同意并询问是否删除。
- 已做并上线（本地 + 临时 PG 验证，生产 `MEMORIA_SPEAKER_AUTHORITY_ENABLED` 已于 09-25 改为 false）：Agent 在无声纹的设备会话上以签名 profile 为据给出 `device_bound_subject` 主人（仅数据权限，`current_speaker_authority_verified` 仍为假，回声/打断门不变），Control 用同一 profile 核验；播放后 3s 内机器人自己说过的告别词不能结束会话；策略 `subject_presence=device_bound`（家长 App 切到孩子仍读不到孩子记忆）；绑定时写入同意权威授予（guardian/subject/新 `delegate`），认定关系 `guardian_attestation_v1`/`delegate_attestation_v1`，老人登记为经绑定人认定的成年人（`child_for_parent` 此前根本无法绑定）；家长页开关双写、解绑撤销、换版延续；无账号孩子可由绑定人导出；生产 env 模板关闭 `MEMORIA_SPEAKER_AUTHORITY_ENABLED`；小程序隐藏声纹入口、绑定勾选与解绑/导出/删除入口。顺带修复：任何已生效关系都会让 profile 落库复核指纹不一致（收据只锁选中的关系）。
- 按使用人删除（2026-09-25 同分支）：可续跑、有进度记录的删除流程（删除期间拒收该使用人的新证据），依次关闭该使用人的设备会话、删归档证据及其派生记忆（含引用了 TA 的合并条目）、文件对象、危机提醒与学习记录、MemoryScope（新维护角色 `memoria_memory_maintenance` 专用函数，只增不改约束对其他调用方不变）、语料；解绑并选删除时再把 TA 的身份隐去为占位并清掉审计中的旧名；最后逐库核对为空。保留的仅有无内容审计（同意记录、策略收据、关系/绑定、会话档案）。部署前须在 `/etc/memoria-postgres.env` 加 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`，控制面 env 加 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`。已知残留：已推送到 Redis 的记忆事件无法撤回（随流修剪老化）；`speech_style_stats` 聚合、persona/digital-self 快照、`entity_ids` 数组无行级来源可追。
- 未做：已有生产绑定的旧"待确认"声明不自动升级（目前无真实用户），用户需重新绑定设备以写入绑定时 consent grant 后再验证 `memory_recall_private`；声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。
- 监护小结（2026-09-26 已上线；用户决定随长期记忆一起授予）：家长给孩子绑定并勾选长期记忆时，同时授予 `guardian_summary_view`（`GUARDIAN_MEMORY_CAPABILITIES`），家长页长期记忆开关同步授予/撤销；真实 PG 下家长 app 的签名 profile 因此出现该能力，孩子私人记忆仍不给。周小结接口新增无账号孩子分支：以孩子的长期记忆同意放行，只聚合家长账号下标注为该孩子的记录（不含原文），小程序在没有监护链接时自动加载绑定孩子的小结。老人（子女代同意）不授予监护小结。存量绑定需重新绑定或在家长页重开长期记忆开关后才会获得授予。
- 完成条件：设备上孩子/老人/本人三种绑定各一次：隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声"再见"不结束会话、真人"再见"能结束；家长端看不到孩子原文；撤销后不再记忆。

### [ ] P1-12 旧媒体链去留（发布工具部分已完成）

- 已完成：整栈发布脚本入库为 `scripts/release_ops.sh`（回归 `scripts/tests/test_release_ops_script.py`），已用于两次全链发布，`20260926-edge-flush-v1` 首次使用仓库版并通过新链冻结校验；常量已指向当前线上链，下次整栈前只读复核后安装。media-edge 已脱离易失的 `/tmp/media-runtime.override.yml`，两次单独切换收据见 HANDOFF。persona 与 session-context 死链路已删，生产要求的能力 token 由十个降为八个，`split_production_env.py` 接受但不分发已退役变量。
- 待决定（需用户）：Python 设备媒体网关（8793）、小程序网关与 LiveKit 在 2026-09-26 只读检查时过去 24 小时零业务流量，但仍部署且属于回滚链；下线等于关闭回滚窗口，需同步删 compose 服务、nginx 路由、镜像与发布脚本中的角色。
- 完成条件：旧媒体链有明确决定并按决定执行。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集/未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备/真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`agent.py`、`duplex_runtime.py`、`routes/interaction.py`。

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
- 已核实保留：分类器“只输出一个枚举词”（精确解析、`max_tokens` 12）、半双工 120 字规则（`agent.py:93` 代码截断）、禁 Markdown（TTS）、禁笑/咳嗽标签（CosyVoice 支持且 `prosody.py:29` 过滤）、导师不给答案、危机/暴力固定话术、`context_assembler` 指令/数据分离、小结字数上限（pydantic 校验）。
- 完成条件：1–4 逐条单独落地（1、4 已完成）；2 需在 DeepSeek V4 Flash 上用真实语音样本前后对照，3/4 跑 `scripts/evaluate_memory.py` 或小样本对照；语音/导师/prompt composition/persona/小结相关测试通过（临时副本上已通过，1 skip）。

### [ ] P2-08 架构整理后续（2026-09-26 审计，减法优先）

- 已完成：PR #42 删除无消费者代码约 3.09 万行，行数预算覆盖全部超 1,500 行模块，跨包依赖图冻结；② 第一、二步已上线（`20260926-edge-flush-v1`）：`create_app()` 与 lifespan 共用装配函数 `_wire_services`，启动资源按创建倒序关闭；同用 archive DSN 的 10 个存储共享一个池（上限 20、语句超时 15s，线上 `memoria_app` 连接 5 → 1），`main.py` 1643 → 1518 行。
- 待完成，按收益排序：① 账号/会话/设备从生产 SQLite（`/data/memoria.sqlite3`）迁到 PostgreSQL，再逐域删除 SQLite 孪生存储（约 4 万行），API 测试改走真实 PG；② 剩余：对象仍在 eager 与 live 各构建一次（需先把 API 测试迁到走 lifespan 的客户端），consent/identity/guardian 等其他 DSN 的池未合并（consent 其一带连接 `init`）；③ 版本化迁移替代 `initialize()` 内建表；④ 先把重度依赖私有字段的测试迁到公开接口，再拆 `DuplexRuntime` 与媒体会话 registry；⑤ 逐步消除 `common`→`agent`/`archive`、`governance`/`memory_scope`→`control_api` 等反向依赖。
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
