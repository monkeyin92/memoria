# Memoria 优先级执行清单

更新于 2026-09-28｜线上为整栈发布 `20260928-reopen-window-v1`（main `173445d`，当日第 3 次整栈发布；media-edge 随后单独发布为 `20260928-writer-teardown-v1`，main `5e358ec`），上一栈 `20260928-followup-endpoint-v1` 为回滚目标。当日上线：配网后激活卡「连接中」修复（固件 build 9/10）、伙伴一致与设备页/伙伴页整改（小程序 `0.2.20260928.1`）、会话设备信任跟随 onboarding（#77）、追问不再中途待命（#80）、背景声不能再拖住已识别的问句（#83），并把 aigcnice.com 证书换成续期证书（至 2026-12-17）。当日遗留见下方「2026-09-28 收尾待办」。本文件只保留未完成事项、执行边界和验收条件，完成收据归 `HANDOFF.md`。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 20260928-reopen-window-v1  # main 173445d；agent/bridge、control-api、两个网关、speaker-model 为该 tag，media-edge 为 20260928-writer-teardown-v1（main 5e358ec）；LLM qwen3.7-flash（联网查询 qwen-plus），ASR fun-asr-realtime，TTS Doubao；回滚 *:rollback-20260928-reopen-window-v1-pre（= 20260928-followup-endpoint-v1），media-edge 回 20260926-minor-safety-v1
control_api_release_lane: 整栈走仓库版 `scripts/release_ops.sh`（安装在服务器 `/root/memoria-release/release-ops.sh`，2026-09-28 三次整栈全链一次 PASS）；PREV 常量已指向 `20260928-followup-endpoint-v1`，下次发布前须改为 `20260928-reopen-window-v1`（小改，随发布 PR 一起带上）；control-api 组件链支持已移除，单组件发布前须先补回
memory_candidate_visibility: code=main 0059368 / enabled=true（随整栈上线）/ verified=SQLite/HTTP/主体隔离/评测适配器回归；四份 2026-09-23 评测收据为上线前 parent_baseline（固定集 recall@5/10=0.857、未见集 0.4、双泄漏 0），真实 PG candidate 行为与线上带鉴权读口未单独取证
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
subject_scope_batch: code=已提交 / wired=应用读出口按主体过滤 / enabled=未启用 / verified=本地 SQLite 与临时 PostgreSQL 回归
account_to_subject_migrations: 已删除（2026-09-28 第 2 批）——四项账号→主体一次性迁移、operator CLI `run_subject_migrations.py` 与治理层迁移接缝从未在生产启用，人格已改为按使用人学习与读取（P1-03），不再有读路径依赖；表结构保留在各 schema，git 历史可恢复
read_path_postgres_parity: 随上一项删除（仅 operator CLI 可达，Control API 从未导入）
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. 真机窗口（用户推动）：设备已于 2026-09-28 重新绑定为「给孩子使用」并勾选长期记忆（`e8a27e45` v3，`growth_summary`），⓪ 已完成；当日真机项见「2026-09-28 收尾待办」。之后按 HANDOFF 验收清单验 P1-11 三种绑定、P1-03 孩子人格隔天生效、P2-04 终止性拒绝不再续连，以及 P0-03 的 TLS/WSS 重连与剩余设备矩阵。不得把核心通过扩大为完整 P0-03 或全双工通过。
2. 可直接推进的代码项：P0-04 按 2026-09-26 产品决定实现（进行中）、P1-02 救援 sidecar 可复现、P1-04 自定义声音闭环、P2-06 回放评测、P2-04 Python 侧进程退出注入。
3. 需用户决定：P1-02 两项线上调整、旧媒体链去留（P1-12）、WAL 保留策略（P1-08）、readiness 逾期的告警渠道（P1-09）、P2-07 第 2/3 项、P2-03 已知缺口是否接受、P0-04 未成年人人格学习口径。
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

- [ ] `response_plan_cached reason=no_verified_runtime_profile mode=companion fallback=True`：09-28 下午 6 h 内 2 次。设备信任为 `untrusted`（无 Device Fleet 证明）是否使回复规划走兜底、影响记忆与个性化，需查 `services/agent/src/agent.py` 的判定。
- [ ] SenseVoice 救援 `httpx.ReadTimeout`：09-28 04:59:40、06:23:46 各一次，都在空闲后的首个救援请求上，符合 P1-02 ① 的 swap 问题。
- [ ] 回复首音时延：09-28 追问一轮从提交到开口约 2.2 s（`qwen3.7-flash` 首 token + Doubao 首段合成），尚未拆分；用户暂缓埋点（归 P0-03 的时延项）。

**定期运维**

- [ ] **2026-12-17 前**更换 aigcnice.com 证书：腾讯云会自动续签，但「未托管、未关联资源」，新证书不会自动到服务器；需下载 Nginx 格式，原位替换 `/etc/nginx/ssl/aigcnice.com_bundle.crt` 与 `.key`，`nginx -t` 后 reload，核对 443/8443（09-28 流程见 HANDOFF）。
- [ ] 发布制品随发布累积：每次整栈在 `/opt/memoria/incoming/` 留约 3 GB，加载镜像另占数 GB；09-28 清理后根分区约 69%（118 GB）。约定一个保留规则（例如只留当前 + 紧邻回滚两批），按授权定期清理；仓库 `docker_image_retention.sh` 因保护全部 `rollback-*`/runtime-base 与 pre-state 引用而 0 候选，需要改或另写。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 产品决定（用户 2026-09-26）：未成年人不分档；只学表达风格；时长与夜间时段运行时强制（危机除外）；年龄以家长申报为准并可在设备页修改；年龄不明也允许学习辅导（不留存）；删「每周小结」勾选项；孩子与老人的危机提醒都推送（家长/代为同意的子女）；话术加 12356 并待专业审核。详见 `docs/compliance/p0-04-minor-safety-decisions.md`。
- 已实现（2026-09-26，已随 `20260926-minor-safety-v1` 上线，设备未验；各项边界见决策文档「实现状态」）：D2 未成年人只学表达风格；D3 家长设定的单次时长与夜间时段签入授权并由设备会话强制（危机除外）；D4 设备页显示并修改年龄段、改后重签授权；D5 年龄不明允许不留存的学习辅导；D6 删除「每周小结」勾选项；D7 孩子与老人的危机提醒都入队、设备页为老人绑定人显示提醒；D8 话术加 12356。同时修复一个既有缺陷：`guardian_enqueue_declared_notification` 只接受待确认声明，而 2026-09-25 起绑定写入的是已认定关系，孩子的危机提醒入队在 PostgreSQL 必然失败且被静默吞掉（线上尚无孩子绑定，未影响真实用户）。
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
- 已重新绑定（2026-09-28）：开发板以「给孩子使用」绑定为 `e8a27e45` v3 并勾选长期记忆，绑定时写入 consent grant；重新绑定曾因会话信任表未跟随而拒绝设备会话（#77 修复并上线，09-28 设备页 runtime-profile 与机器人 media-sessions 均已 200）。待验证 `memory_recall_private` 与隔天记忆。
- 未做：声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。
- 监护小结（2026-09-26 已上线；用户决定随长期记忆一起授予）：家长给孩子绑定并勾选长期记忆时，同时授予 `guardian_summary_view`（`GUARDIAN_MEMORY_CAPABILITIES`），家长页长期记忆开关同步授予/撤销；真实 PG 下家长 app 的签名 profile 因此出现该能力，孩子私人记忆仍不给。周小结接口新增无账号孩子分支：以孩子的长期记忆同意放行，只聚合家长账号下标注为该孩子的记录（不含原文），小程序在没有监护链接时自动加载绑定孩子的小结。老人（子女代同意）不授予监护小结。存量绑定需重新绑定或在家长页重开长期记忆开关后才会获得授予。
- 完成条件：设备上孩子/老人/本人三种绑定各一次：隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声"再见"不结束会话、真人"再见"能结束；家长端看不到孩子原文；撤销后不再记忆。

### [ ] P1-12 旧媒体链去留（发布工具部分已完成）

- 已完成：整栈发布脚本入库为 `scripts/release_ops.sh`（回归 `scripts/tests/test_release_ops_script.py`），已用于两次全链发布，`20260926-edge-flush-v1` 首次使用仓库版并通过新链冻结校验；常量已指向当前线上链，下次整栈前只读复核后安装。media-edge 已脱离易失的 `/tmp/media-runtime.override.yml`，两次单独切换收据见 HANDOFF。persona 与 session-context 死链路已删，生产要求的能力 token 由十个降为八个（2026-09-28 删除 `/v1/evolution` 后再降为七个，`MEMORIA_EVOLUTION_VALIDATOR_TOKEN` 退役），`split_production_env.py` 接受但不分发已退役变量。
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
- 待完成，按收益排序：① 账号/会话/设备从生产 SQLite（`/data/memoria.sqlite3`）迁到 PostgreSQL，再逐域删除 SQLite 孪生存储（约 4 万行），API 测试改走真实 PG；② 剩余：对象仍在 eager 与 live 各构建一次（需先把 API 测试迁到走 lifespan 的客户端），consent/identity/guardian 等其他 DSN 的池未合并（consent 其一带连接 `init`）；③ 版本化迁移替代 `initialize()` 内建表；④ 先把重度依赖私有字段的测试迁到公开接口，再拆 `DuplexRuntime` 与媒体会话 registry；⑤ 反向依赖已消除：`memory_scope`→`control_api`（第 2 批）；`common`→`agent`（危机语义判定枚举移入 `common/crisis_policy.py`，agent 分类器转引）、`common`→`archive`（`evidence_policy` 只读三个字段，改为结构化 Protocol）、`governance`→`control_api`（`AccountStore` Protocol 列出治理用到的 5 个账号库方法）。`companionship`→`control_api` 保留：它是在进程内启动 Control API 的评测工具，位于依赖图顶层。
- 2026-09-28 双视角评审后的批次顺序（先修缺陷，再做减法，再做需授权的存储迁移，最后拆大对象；每批一个 PR）：第 0 批 media-edge 写循环/CloseSend/accept 后登记与小程序 tab 导航已合并（PR #86、#87，media-edge 已单独发布）；第 1 批正确性修复见下；第 2 批删除零消费者路由与域包、一次性迁移、声纹残留、Python 设备网关残留；第 3 批即 ①③；第 4 批即 P1-12 加 `ReplyPipeline` 抽取、`TTS_PROVIDER` 工厂与跨语言 fence 向量；第 5 批即 ④ 加测试瘦身（可控时钟、按 mixin 切分）；第 6 批 `ControlSettings` 按域拆分、Go `%w` 与结构化日志、固件 CI 编译、⑤。
- 第 6 批（部分完成，待发布）：media-edge 日志改为 `slog`（默认格式不变，`voice_session_report` 解析照旧）；CI 新增 ESP-IDF 固件编译作业（PR #90）；环境模板改为校验而非生成：`scripts/tests/test_env_template_drift.py` 要求 Control/Agent/两个网关的每个 settings alias 都出现在 `infra/memoria.env.production.example`（可为注释的 `# KEY=默认值`），或登记在带理由的豁免表（SQLite 开发库路径、镜像构建注入的 release tag、生产禁用的旧全权 token、只在 root 升级流程出现的 bootstrap DSN）；补齐了 23 个缺失项。模板为多服务共用且带人工注释，生成会丢注释，故不生成。`ControlSettings` 的 210 个字段按域拆为 `app/config_fields/` 下 8 个 mixin，属性访问与环境变量名不变，逐字段比对一致（PR #111）；Go `%w` 已核实无需改动（包装底层 error 的 `fmt.Errorf` 均已用 `%w`，其余为 C 错误字符串）；⑤ 见上。
- 第 5 批（部分完成）：15k 行 `test_media_session.py` 按行为拆成 6 个文件并共用 `media_session_support.py`（PR #95）。prompt 测试改按段落 ID 断言（`PROMPT_SECTIONS`/`ComposedPrompt.section()`，渲染文本逐字节不变，PR #107）。可控时钟暂不做：实测 agent 测试最慢的是 5 s mock 超时，非零 `sleep` 合计约 2 s（162 处是 `sleep(0)` 让步），收益不抵改动。待完成：私有属性读取改公开接口（688 处）与 `DuplexRuntime` 拆分——属语音主链，须有真机验收窗口。
- 第 4 批（部分完成，待发布）：Go 与 Python 共用一组 generation fence 向量（`packages/contracts/generation-fence-vectors.json`，两端各一个测试，PR #93）；TTS 经 `providers/tts_factory.py` 按 `TTS_PROVIDER` 构建，克隆音色在工厂内决定（PR #94，生产仍为豆包）。待完成：`ReplyPipeline` 抽取与 LiveKit 路径下线（需 P1-12 决定）。
- 第 3 批 ②（第一步完成：生产形态的 PG 测试底座）：`testing/postgres_harness.py` 用真实 `infra/postgres/init-memoria.sh` 与数据 compose 挂载的 schema 建模板库，运行时自建 schema 的存储按生产角色初始化，每个测试克隆一份（约 50 ms）；每个 DSN 以其生产角色连接，FORCE RLS 真实生效。`MEMORIA_TEST_APP_POSTGRES=1` 让 `services/control_api/tests` 与 `services/governance/tests` 的 `create_app()` 在 eager 装配里也构建 PostgreSQL 存储（`MEMORIA_EAGER_POSTGRES`，仅测试，生产环境忽略），CI 新增并行作业 `control-api-postgres`；测试按后端中立改写（`testing/app_store.py` 读写夹具行；RLS 下陌生人得 404 而非 403 由 `assert_stranger_denied` 统一）。它已找出并修复 9 个 SQLite 孪生掩盖的生产缺陷：self-model JSONB 列表按字符解码（#96）、长辈绑定先校验年龄后记关系（#97）、被替代绑定版本对非主体属主不可见致版本列表 500（#99）、账号删除因 `memoria_evolution` 无 lifecycle 表 DELETE 权限而中断（#100，生产已核实缺权限）、记忆编译与留存无 actor 读取绑定主体（#101，潜伏）、PG 导出不解码 JSONB 致自助导出无证据（#102）、监护行 UUID/时间不可 JSON 序列化致含监护记录的账号导出失败（#103，生产 1 个账号受影响）、语料维护角色无法标记删除（#104，潜伏）、开发运行时刷新权限未带 actor（本 PR）。默认 SQLite 模式与 PG 模式全部通过。已删 identity 的 SQLite 孪生（2.3k 行）：生产启动本就要求 `MEMORIA_IDENTITY_DATABASE_URL`，无 DSN 的开发/默认测试改用单测已在用的内存存储。待完成：其余域（guardian、memory_scope、consent、device_fleet、archive）——guardian 等没有内存替身，删除前需先定：补内存替身，还是把这些测试改为只在 PG 下跑。
- 第 3 批 ①（代码已完成，待发布；生产切换待授权）：控制库同一套 SQL 跑两个后端（`database/backend.py`：`?`→`%s`、`BEGIN IMMEDIATE`→事务级 advisory lock 保持单写者语义、写操作包 savepoint、布尔转 0/1、行对象兼容下标与列名），PostgreSQL 表结构 `database/postgres_schema.sql` 共 28 张（原 23 张 + 删除台账 + 数字分身预览 4 张；预览表必须同库，账号删除在同一事务里清它们），NOLOGIN 属主 `memoria_control_owner` + 运行时角色 `memoria_control`，全部 FORCE RLS；`MEMORIA_CONTROL_DATABASE_URL` 为空时仍用 SQLite 文件。发布链：`env` 步骤只生成角色密码不写 DSN，`schema` 步骤经 stdin 幂等建表（运行中的 PG 容器尚无 011 挂载），`verify_authoritative_postgres.sh` 检查 28 张表。迁移 `scripts/migrate_control_sqlite_to_postgres.py`（只读源、目标非空拒绝、未映射的非空表拒绝、逐表行数+校验和、序列续接、收据不含行内容）；切换 `release-ops.sh control-store`（`CONTROL_STORE_MODE=dry-run|apply`，失败自动回 SQLite），步骤与回滚写入 `docs/runbooks/release-rollback.md`。验证：对照开关 `MEMORIA_TEST_CONTROL_STORE=postgres` 让全部测试的 `MemoryStore(path)` 跑在 PG 上，全量通过；真实角色契约测试 `test_postgres_control_store.py` 证明运行时角色在 FORCE RLS 下可读写、其他角色被拒、迁移可复核且拒绝重跑。已知取舍：同步驱动 psycopg（沿用现有同步调用约定，异步化另议）；按账号的行级策略与 SQLite 持平，未新增。生产数据现状（2026-09-28 只读核对）：18 张非空表共 2,559 行，全部有映射。
- 第 3 批 ③（代码已完成，待发布）：PostgreSQL 控制库引入版本台账 `control_schema_migrations`。`postgres_schema.sql` 是幂等基线，记为版本 1，每次发布照旧重放；基线无法幂等表达的改动放 `database/migrations/NNNN_<名>.sql`（从 0002 起连续编号，发布后不再修改），发布 `schema` 步骤逐个以单事务（`psql -1`）执行并写台账。运行时角色只读台账：库版本落后于代码即拒绝启动；库版本领先（代码回滚）仍可启动。SQLite 路径的建表与补列随孪生存储删除，不另做版本化。验证：真实角色契约测试覆盖只应用一次、失败不留痕、编号必须连续、落后拒启、领先可启，以及发布脚本与代码指向同一目录。
- 第 2 批（代码已完成，待发布）：删除零消费者代码——一次性迁移与接缝（约 1.9 万行，含测试）、Python 设备网关残留 `device_client`/`device_runtime`、`speaker/evaluation.py`、`evolution` 的 runtime/trajectory/replay/skill；删除无调用方的路由 `/v1/legacy`、`/v1/self-model`、`/v1/skills`、`/v1/evolution`、`/v1/tutor`、`/v1/production/memories` 及仅服务它们的装配（tutor 授权、evolution 运行时采集、`tutor_profile`、MemoryScope 的 capture/recall 适配器）；域包本身保留，因 session/interaction/agent 仍导入其领域模型。依赖基线收紧：去掉 governance→digital_self/identity/memory_scope/persona、`memory_scope→control_api`（已知反向依赖之一）与 tutor→archive。保留决定：Go 端 `keyword.detected` 转发路径保留，它是 README 所述签名本地硬停与 P1-07 语音打断的契约入口，只是固件尚未产生；声纹后端（`/v1/speakers`、speaker-model 容器）按 P1-11 等设备验收后整体清理，内部登记依赖公开登记意向，不能只删一半。
- 第 1 批（代码已完成，待发布）：media-edge 开槽等待跟随请求 context，超载答 503；关键词发送不再持 `stateMu` 做 gRPC 写（`Session.mu` 原子门保留，因关键词事件无 fence、Voice Core 不能迟到拒收）；guardian SQLite `grant_consent` 补上与 PG 一致的 actor/`guardian_user_id` 校验；`BootstrapStorePort` 由空类改为 Protocol（`bootstrap_port.py`），删除 41 处 `attr-defined` 忽略并修正一处被掩盖的类型收窄；删零导入依赖 `sqlalchemy`；小程序「我的」敏感入口改用 `entryAllowed` 结果（数字分身/原始语音不再永远「暂未开放」），监护与原始语音授权的未接入状态提前显示并禁用控件，首页唤醒词读设备设置，回顾页文案改为「点确认后才会留下」，换伙伴保存失败回滚，删 5 个无引用 API 导出。
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
