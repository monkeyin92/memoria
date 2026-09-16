# Memoria 优先级执行清单

更新于 2026-09-16（第二轮），审查基线 `a8a0e43`。本文件只保留未完成事项；完成后把仍有效的运行结论归入 `HANDOFF.md`，并删除任务/子项，不积累完成记录或另建归档。已有编号不复用；生产、设备、回滚与原始证据以 `HANDOFF.md` 为准。

## 下一步与执行边界

1. P0-04 的代码侧证据与数据主体错配已改并补了本地回归，但新增的 PostgreSQL/RLS 契约只在本机跳过、尚未由 PG 实跑；先让 CI `python` job（带 postgres service）跑绿这三个新契约，再做真实 API→签名 profile 路径与设备验收。
2. P0-03 的 TTS 预算语义、无效回归测试、G 静默预算与部分音频失败终态都已本地闭环（含前后对照与故障注入用例）；仍缺真机 G/B/D 复跑取证、设备侧实际退出 speaking 的时序核对与时延门，需设备窗口。
3. P1-01 的标准构建/发布门禁接线与解析器候选绑定已落代码；需在真实构建（CI `agent-image` 或构建机）验证 `-m` 调用与镜像内 `RUN` 通过，再谈候选切流。
4. WAL 保留策略另列 P1-08，不能随“自动备份暂缓”一起搁置；先测现状，生产删除/重启/启用定时任务须另获授权。

本轮只改代码、测试与文档，未部署、未刷机、未改生产数据，也未启动 docker/PG/设备。最新 CI `35072578098` 的 agent/python 通过，Edge/小程序/固件/镜像任务跳过；本地定向测试通过不等于下面各项完成。最新已记录 Agent/Bridge 运行 `d96d4c2`、板卡 `d1ad38f`，不能把 HEAD 的 `code` 记成线上 `enabled/verified`。

## P0：发布前必须闭环的安全与语音问题

### [ ] P0-04 修正当前使用人的监护授权与学生安全闭环

- 前提：成人管理账号可创建独立 `under_14/14_17` 使用人，不要求孩子另建登录账号。以当前使用人及合法监护关系决定策略，成人学生不自动视为未成年人；最小 app_confirm/年龄资料入口优先，不重建已有多主体权威。
- 审查发现——关系确认不实：`routes/multi_subject.py::_primary_subject` 替新建孩子调用 `confirm_relationship(person_id=child)`，写出孩子确认时间和 actor=child 的审计；随后 `establish_active_link` 在未校验微信身份的路径标 `verified_via=wechat_identity`，以合成 code hash/365d 到期建立 active link，缺少相应建立/确认事件。SQLite 探针可复现；这不是实际完成双边或微信验证。
- 已修（2026-09-16，本地代码+测试）：`_primary_subject` 只写家长自己一侧的确认，关系保持 `pending`/`confirmed_by_target_at=NULL`，evidence id 明确为 `guardian_declaration_v1:device_binding`；`establish_active_link` 已从 port 与 SQLite/PostgreSQL store 删除（它还会顺带激活既有 pending link、绕过孩子确认）。Identity 新增 `has_source_confirmed_relationship` + `declared_guardians`，`parent_for_child` 的 guardian 角色可基于该声明建 binding，但声明不会被升级为已验证监护。危机通知由调用方从 Identity 解析声明监护人以 `declared_guardian_ids` 显式传入，PostgreSQL 侧由 `guardian_enqueue_declared_notification` 在库内复核声明后才入队；这是有意决定（声明是通知依据，不是已验证监护或同意），已记入本项。
- 待修：把该决定与"有效同意"的边界写进面向产品的说明；通知 outbox 目前仍是声明/激活两种依据并存，等 app_confirm 入口落地后复评是否需要收窄。
- 审查发现——数据主体错配：`interaction.py::response_plan` 用 active subject 类别，却以 `account_id` 查 `memory_retention` 并读账号记忆；`session_policy` 又按账号类别判断。已有孩子 consent 不生效，且同一会话可出现 response-plan 不读记忆、policy 却允许 private_memory/history 的矛盾。先明确记忆的真实数据归属，统一类别、consent、读写和 policy；不能只替换一个 ID 而将家长私有记忆交给孩子。
- 已修（2026-09-16，本地代码+测试）：唯一 ID 是当前使用人。`_subject_memory_retention_allowed` 用同一 person id 同时提供类别与 consent 查询；`session_policy` 改按签名 RuntimeProfile 的 active subject 判定上限并把 `owner_display_name` 限定在账号本人当轮；`response_plan` 在 subject≠account 时不再走账号键的旧档案记忆读（该表把第一人称 claim 全部存成 `subject_key="self"`，换键会把账号主人自己的记忆喂给使用人），并补了 subject≠account 的回归测试。真正的 subject 键记忆在 `services/memory_scope`，把会话记忆迁到该权威仍是后续工作。
- 已修（2026-09-16）：当前使用人权威配置存在但读取失败/过期时记日志并保持保守能力门（不回落账号资料、不猜 minor、不通知家长），保留公开固定安全回复；离线无该权威的部署仍按"账号即使用人"处理。
- 测试缺口：真实 API→签名 profile 一段已补——`test_accountless_child_profile_reaches_the_device_through_the_real_api` 走真实 HTTP 链（device-binding → session → resolve-subject → app_confirm 切人 → device runtime-profile），对拿到的签名 profile 用与决策路径相同的 `verify_runtime_profile_payload` 校验，并断言 `active_subject_id/subject_category/age_band/service_mode` 都是孩子且仍无 active guardian link。仍未补：把该 profile 送进 `/response-plan` 与 `/session-policy` 需要真实的 persistent Session Runtime（`PostgresSessionRuntimeService`，仅生产安装），本地与无 PG 环境无法执行；crisis 用例仍用 `_attach_signed_runtime_profile`。新 guardian 写路径的 PostgreSQL/RLS 契约（`test_declared_guardianship_is_one_sided_and_never_verified`、`test_parent_for_child_still_requires_a_relationship_for_the_guardian_role`、`test_declared_guardian_notification_requires_the_identity_declaration`）**已在真实 PostgreSQL 17.8（pgvector/pgvector:0.8.1-pg17-bookworm 临时容器，宿主 55432）实跑通过**，`scripts/tests/run_authoritative_postgres_gate.sh` 的 init + repeat-upgrade 门禁也在同一批次通过，全量 `pytest services/ tests/` 带 `MEMORIA_TEST_POSTGRES_DSN` 通过。实跑暴露并已修掉两处真问题：① 跨 schema EXECUTE 授权只在"Identity 先装"这一种顺序下生效，guardian 侧单边 conditional grant 在反序时静默漏授权（现由 identity 侧补一条带 pg_roles 保护的 grant，两种顺序都成立）；② 声明监护人没有 active link，导致危机事件与通知 outbox 的 RLS 拒绝写入/读出（现新增 maintenance-owned 的 `guardian_relationship_declared` 包装 + 收件人判定与危机事件读策略的声明分支）。
- 完成条件：adult 管理账号下 under_14/14_17/adult/unknown 矩阵、换合法管理账号、切人/改年龄与旧缓存/旧 generation 并发、无同意/撤销/无权限均通过；类别、同意、通知与数据作用域一致。零同意可建 HTTP session 不直接等同真实设备越权，须验证实际设备 admission/受限能力门。
- 设备验收：先走正式 app_confirm 确认测试使用人及年龄，核对设备实际取得的签名 profile 与有效监护授权；不手工注入 profile、不把成人代讲当作孩子身份证据、不绕过声纹/准入门。受控成人模拟危机，固定话术逐字交付+终端回执+人工听感；通知 outbox 绑定正确孩子及有效监护授权、重放幂等、家长作用域可读、原文不外泄。通知失败不得吞公开回复。发送 worker/外部投递按用户 2026-09-14 决定暂缓，不把 outbox 称为已送达。
- 入口：`services/control_api/app/routes/{multi_subject,interaction,guardian}.py`、`services/identity/service.py`、`services/guardian/{sqlite_store,postgres_store}.py`、`services/control_api/tests/test_student_safety_loop.py`。

### [ ] P0-03 收口 TTS、续问竞态、设备停滞与真实时延

- 当前证据：2026-09-16 H 同会话天气→续问→播后告别成功，F 的 48.28s 九天天气完整听完；但 B/D 设备停滞、G 续问丢失、H 时延越线，整体稳定性未通过。不沿用跨 release 累计轮数，不以原始笔记中的“约 55s 阈值”或“告别 3/5”作为结论。
- TTS 语义与回归测试：流式/批式的首包、停滞、硬期限与超时分类语义已统一到共享的 `GenerationBudget`（`services/agent/src/providers/generation_budget.py`），Doubao 与 CosyVoice 两条 provider 的 stream/batch 四条路径都走同一决策点，`cosyvoice_tts.py` 的绝对挂钟已随之改掉（结论与用例名见 `HANDOFF.md`）。剩余待修：`synthesize_stream_text` 的重试仍只有"personal voice 首包失败回落设计音色 + 非个人音色重试一次"，未定义停滞/已下发部分音频后的重试语义；必须保持部分音频不得整句重放。
- G 部分修（2026-09-16 第二轮，本地代码+测试）：已按"先统一语义"处理。`owner_silence_remaining_s` 现在只有三种含义（`None` 未测量 / `0.0` 已耗尽 / `>0` 暂停剩余），结束已测预算一律记 `0.0`（原分支会记 `None`，使下一次装表白送一个满窗口）；计数器改名 `owner_silence_activity_revision` 并收敛到单一入口 `_note_owner_silence_activity`；被受理的 ASR final 现在与已受理 VAD 一样属于"已受理主人活动"，可失效仍在等 `standby_lock` 的 `owner_silence_timeout` 关闭，但只在仍有其它界（绝对说话看门狗或未到期 grace）时生效，不会让会话失去所有界，也不刷新静默预算（无验证说话人时不得靠文本延长会话）。
- 已证有区分度：`test_accepted_final_can_veto_a_parked_grace_close`（final 先于迟到 VAD 的 G 顺序：grace 关闭停在锁上时到达的 final 必须否决关闭）与 `test_ending_a_measured_budget_records_it_as_spent_not_unmeasured` 在 `HEAD` 上失败、在当前分支通过；`test_spent_budget_stays_spent_after_a_late_vad_retracts_the_grace` 与 `test_accepted_final_cannot_hold_an_unbounded_session_open` 是两版都通过的保护用例（真实入口 `on_speech_segment` / `accept_asr_result`）。
- G 仍待做：没有真机捕获就不宣称"续问不再丢失"。剩余是"无验证说话人、且没有其它界时的 owner_silence_timeout"产品语义（当前保守关闭，未改），以及 endpoint/commit 乱序、watchdog 交接在真实 10s/60s 配置下的完整矩阵；需在冻结候选上按 `HANDOFF.md` 的 device window 复跑 G 并记录终态。
- 部分音频失败终态：已补本地故障注入回归（`services/agent/tests/unit/test_media_output_partial_failure.py`，见 `HANDOFF.md`）：首帧已下发后 provider 崩溃/停滞/硬期限，都在有界时间内落到同一终态——一个后继代的 CANCEL_GENERATION（设备据此退出 speaking 并 flush）、交付账记 ERROR 而非 playback_completed、`provider_complete=false`、权威相位回 listening，且旧代的迟到 ACK/ENDED 不能复活该代。仍未闭环：这些结论只覆盖进程内注入，B 的真实表现（旧总挂钟 20.23s 出错、约 38.6s 后才错误收尾）来自设备捕获，需在冻结候选上按 `HANDOFF.md` 的 device window 重跑并核对桥/Edge 侧终态与设备实际退出 speaking 的时序。
- B/D 独立定位：57.46s/58.88s 是桥侧音频长度，不是设备运行到该时长才卡住；两次 1.5–1.6s 先有 366/412ms supply wait，数秒内 speaking/控制台停滞，D 没有同类 TTS 错误。补同 fence 的 Bridge→Edge WS writer→设备接收/解码/播放及任务/锁证据；drop=0 不证明收齐，不预设根因或先实现 pre-roll。
- 时延门：H ACK→正文 1.969s 未过 <1.5s；设备 VAD end→首帧 4.557s（A 第二问 3.748s）。拆分 ASR finalize、查询、TTS 与播放，不用网络首帧冒充精确可闻延迟；保留明确 1–16 天天气/完整地名与预算、单次 ACK、防重复和代际隔离，不通过第二提示、加长静默或放宽门禁遮掩。
- 完成条件：冻结并启用同一候选后，三轮天气→续问→播后告别、>45s 长答、B/D 同类长答、临近静默与部分下发后故障分别取得完整捕获、fence/终态和操作员听感；有效输入才进入告别可靠性统计。ACK→正文满足门槛，失败能有界退出；缺测项如实 pending。
- 同场补验待机+五表情照片及点屏/摇晃/短拍/BOOT 不回归，步骤见 `HANDOFF.md`。Exact DAC/AEC/播放期 voice 权限另在 P1-07，不能把本项通过写成全双工。
- 入口：`services/agent/src/providers/doubao_tts.py`、`voice_core/media_session_{standby,output_stream}.py`、`test_doubao_mock.py`、`test_media_standby_races.py`、固件 PlaybackSupplyMeter、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与学生产品闭环

### [ ] P1-01 接好制品/隐私门禁，完成当前 1.8.1 候选发布与回滚验收

- 当前锁为 LiveKit agents/openai/silero 1.8.1；运行镜像是 delta 构建。不要重复做同组升级，也不要因研究文档旧基线再推 1.8.2。保留 Python 3.12 与真实锁约束，不使用 --no-deps；不混入 expressive/DuplexModel、抢跑、user_turn_limit 或 TurnPhase 有副作用策略。
- 审查缺口：`verify_agent_release_artifact.py` 的干净进程/negative-control canary 可用，但标准 `infra/Dockerfile.agent` 未复制/运行它，发布流水线也未调用；`resolve_target_images.py` 未传 expected candidate 时只验一致性，两服务同指旧镜像仍成功。
- 工作：把锁/版本/兼容及隐私 canary 接到标准完整构建/发布门；解析器要求明确候选身份，缺省、旧镜像覆盖、缺服务、服务不一致均拒绝。显式保留有效 stack tag 与目标 candidate tag 的区别，覆盖真实 override 组合，不仅测试 helper 的带参路径。
- 已接（2026-09-16，本地代码+测试，尚未在真实构建/发布上执行）：三条 Agent 镜像路径（`infra/Dockerfile.agent` 全量、`scripts/delta_build_images.sh` delta 模板、`infra/Dockerfile.agent-source-overlay` 组件覆盖）都 COPY 并 `RUN /app/.venv/bin/python -m scripts.verify_agent_release_artifact`（源码覆盖自带候选的 verifier，不信任 base 镜像副本）；`deploy_agent_component.sh` 把 `resolve_target_images.py` 随候选上传并校验 sha256，在写回滚点之后、切流之前用"有效 stack tag + release commit + 预期 candidate tag/image + 真实 override 链"解析目标镜像，失败即停止且不触碰在线栈。`resolve_target_images.py` 现在必须显式给出候选身份，缺省/无效 tag/缺服务/服务不一致/仍解析到 stack 镜像分别返回 2 或 1，并保留 stack tag 与 candidate tag 的区别。CI 增加 `agent-image` job（构建镜像并重跑 verifier）与显式采集 `scripts/tests` 的两个门禁测试文件（此前 9 项定向测试不在 `testpaths` 内，默认任何 pytest 运行都不会收集）。
- 构建验收：从冻结 source/lock 标准完整构建实际受影响的镜像，Agent/Bridge 共用同一产物；依赖变化不能 source overlay。核 fresh process 的 env 未设置/空值/显式 0、入口加载和真实 exporter，无内容/PII 泄漏；不能用只改 Compose 声明或外部测试环境代替候选运行态。
- 已验（2026-09-16，本机 Docker Desktop 29.7.2）：`docker build -f infra/Dockerfile.agent` 全量构建成功，构建期 `RUN … -m scripts.verify_agent_release_artifact` 通过（版本锁/隐私默认值+canary+反例/SDK 兼容三项）；再以 `docker run --rm --network none --entrypoint /app/.venv/bin/python memoria-agent:… -m scripts.verify_agent_release_artifact` 对**已构建镜像**按运行用户离线复跑通过（exit 0），即 CI `agent-image` job 的同一组命令。`resolve_target_images.py` 也在真实 `docker-compose.production.yml`（env_file 仅指向本地临时 stub）+ 真实 override 链上演练：live+candidate → 两个服务都解析到候选、exit 0；只给 live（缺候选 override）→ exit 1 并明确报“仍解析到 stack 镜像”；不给候选身份 → exit 2；**旧镜像 override 后置覆盖候选** → exit 1。构建暴露并已修：`COPY` 保留宿主文件模式，umask 077 的检出会把源文件与 gate 脚本带成 0600，非 root 运行用户（65532）读不到 → 现三条构建路径都显式 `--chmod=0644` 并在 COPY 之后统一 `chmod -R u=rwX,go=rX`，镜像不再依赖构建者 umask。仍未验：真实 exporter（非 InMemorySpanExporter）的 PII 不泄露、以及切流/回滚演练需要生产授权与目标主机。
- 发布验收：先完成本次拟发布 slice 的 P0 修复与回归；以现有本地 1.8.1 部署收据为历史基线，重新读取真实容器、env、override、当前/紧邻回滚和远端收据，记录跳过/重试边界。按授权切流，provider/LiveKit、12 个具名 readiness、外部路由、设备和延迟复核，再实际验证回滚路径；只保留当前+一个可运行回滚及必要依赖底座。
- 完成条件：`code/wired/enabled/verified` 分层有日期与证据；不以 9 项定向测试或 CI 镜像 job skipped 称为正式制品已验。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 与自动/异地备份暂缓分开处理。先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧“约 1GB/天、6 周写满”不能作当前预测。
- 需用户决定并授权：可证明安全的裁剪策略、停用 archive_mode（需 PG 重启），或明确频率/阈值的手工处理。不能仅按 mtime 删除并破坏仍保留 base backup 的恢复链，也不清理 pg_wal 代替归档保留。
- 完成条件：选定策略有保护集合、容量/恢复影响和可复核执行证据；保留的恢复目标仍可验证，后续增长有处理责任与阈值。启用自动备份/异地副本另行确认，不搭便车实施。

### [ ] P1-02 让 ASR 救援 sidecar 可复现并验证真实输入

- 先读生产启动脚本、Dockerfile、包/模型/词表摘要、基础镜像与输入语言，核对实际消费者；将真实构建输入纳入仓库，保留独立镜像。DashScope 模型服务版本不是 FunASR Python 包版本；仓内 sherpa-onnx 实现不支持“升级 FunASR 即修 sidecar”的推断。
- 仅在实际使用 FunASR 且确有对应修复时做旧/新 A/B；否则核真实后端，不向主 Agent 添无用依赖或顺手改 NumPy/设备 VAD。
- 完成条件：仓内输入可重建；真实中文 PCM 短句、长段/尾字、静音、低 RMS、削波、并发/失败降级通过。分报 vendor_error/vendor_silent/gating/low_rms、救援耗时与 2.5s 超时行为；有收益且无回归才另行切流。
- 本轮已补（本地，合成波形，非真实录音）：`services/agent/tests/integration/test_funasr_rescue_audio_shapes.py` 覆盖静音与室内底噪被本地门禁拦下（`rescue_total{outcome="skipped"}` + `empty_transcript{class="low_rms"}` 且厂商零请求）、削波满幅信号按上行原始字节送判、超长段只把最新尾帧（内容精确比对）送去救援、三路并发 + 慢/失败厂商降级为有界 `no_text` 且不抛不卡、以及在飞行中的救援发布 `now + 2.5s` 预算（默认 `SENSEVOICE_TIMEOUT_S=2.5`）。仓内既有的 `test_funasr_empty_accounting.py` 覆盖四个分桶判定。
- 仍未完成：真实中文录音语料（短句/尾字听感、低 RMS 真实底噪）与生产启动脚本/Dockerfile/模型/词表摘要纳入仓库——后者按原记录只存在于服务器 `/opt/memoria/sidecars/sensevoice-asr/`，本轮未连接生产故无法取得；DashScope 模型服务版本与 FunASR Python 包版本仍未核对，不做"升级即修"的推断。
- 入口：`funasr_stt.py`、`funasr_empty_accounting.py`、`providers/sensevoice.py`、`scripts/run_sensevoice_asr.py` 及 ASR 契约/救援测试。

### [ ] P1-03 按使用人切人格/音色：补控制入口与真实切换

- 最小“手动确认使用人及年龄”入口先供 P0-04；完整人格/音色验收依赖 P0-04，避免循环依赖。先核线上 Control 路由/schema，不能把 9 月 11 日失效通知 overlay 当成最新全量后端。
- 小程序接已有设备/成员、分配/取消分配/邀请 API，先以 app_confirm 闭环。`allowed_confirmation_methods` 只广告 `app_confirm` 的不一致已修：`resolve_subject` 现在只返回写接口真正接受的方法，`test_advertised_subject_confirmation_methods_match_the_write_api` 同时断言广告集合、`app_confirm` 被接受与 `voice_question` 仍被 422 拒绝；不把客户端 flag 当身份证据。剩余：小程序端接已有设备/成员与邀请闭环仍待做。
- 完成条件：同一 binding 两个 subject 得到不同人格/音色；切换推进版本、签名失效、next_safe_point 重协商，旧上下文/音频不串人；取消回落默认。内置只读、自定义创建即冻结、克隆未就绪回落设计音色，均获真 PG 与设备实听。
- 入口：`routes/{persona_assignment,custom_personas,multi_subject}.py`、`services/session_runtime/profile_service.py`、`services/voice_profile/`、`apps/miniprogram/pages/device/`；不再造设备人格引擎。

### [ ] P1-04 自定义声音：样本上传到设备出声

- 依赖 P0-04/P1-03；先核已有真实音频校验、进度轮询、over_budget 逻辑是否在线，避免重复开发。
- 覆盖 profile 有界录音→上传→真实解码→训练→ready/failed/超时→分配→设备实听，连同拒绝、取消、重复提交、撤销/样本处置。
- 完成条件：无效音频不训练，无假 ready/百分比；实测 60s 产品预算与 120s provider 超时，超预算如实显示，未 ready 回落设计音色。小程序不扩张手机声纹、实时对话/WSS/TTS。
- 入口：`apps/miniprogram/pages/profile/`、`services/voice_profile/`、`routes/voice.py`。

### [ ] P1-05 补权威会话状态，再做小程序三端验收

- 只读状态开发可独立进行；集成使用 P1-03/04 同一候选。以 Python→Edge 的 assistant_state.phase 为源，补缺失的 Edge→Control 受鉴权只读出口，不把连接在线猜成 listening/idle。
- 投影带 session/generation fence 和新鲜度，重连/断线/过期显示 offline/unknown；不从字幕、零散 diagnostics 或客户端计时猜态，不增加话轮控制面。
- 完成条件：微信手机/电脑/开发工具同版本覆盖登录绑定、三态/断线、主体人格切换、样本进度、回顾、权限拒绝与刷新；录音范围门禁通过。0.8.84 仅开发版；体验版、提审、正式发布分别授权和记录。
- 入口：`services/media_edge/{bridge_runtime_events,session_shadow}.go`、`routes/device_control.py`、`apps/miniprogram/`。

### [ ] P1-06 修复三类召回遗漏与人物抽取

- 可本地并行。基线 recall@5=0.8125（13/16）、nDCG@10≈0.734；Fix后本轮 recall@5=0.9375（15/16）、nDCG@10≈0.859，`candidate_leakage`/`cross_account_leakage` 仍为 0、`extraction_recall`=1.0、p50≈0.72ms（见 `HANDOFF.md` 的三处改动）。仅剩 `repeated-episode-campus-startup`，它卡在下面的产品语义，不靠放宽判据抬分。
- 待定语义：review 确认证据/claim 是否同时提升其 person/episode/knowledge；跨会话 episode 是否合并。现有逐事件 document 不能满足“同 episode 含两条 source_event_ids”的判据，先定产品写入语义，不能绕过判据抬分。
- 已修（本轮）：① 忌口转述——`RecallPlanner` 封闭词表新增“避开/忌口/不能吃/别吃/注意别/过敏”标记 → “不喜欢/讨厌/不吃/忌口/不要”扩展，并让评测 adapter 与生产读路径一致（生产**总是**先 plan，之前 13 个无 clock 的用例根本没走 planner）；② 别名——规则抽取器新增“家里人/大家/别人…叫(她|他)X”封闭句式，只在唯一人物且非角色词时挂为该人的 alias；③ 人物不可召回——person 之前只存在于 `person_aliases`，没有任何读路径会搜它，因此“阿梅是谁？”永远召不回该人物；本轮在编译期为人物建立 search document（title=display_name、body=关系+全部 alias、kind=person/memory_kind=relationship、sensitivity=personal），确认时随同事件投影一起提升，未确认人物仍被 confirmed-only 的 `context` 挡住。
- 人物投影已同步到生产路径：`postgres_memory_catalog.py::_write_extraction` 现在也写同名 search document（PG schema 的 kind 无枚举约束、`memory_kind=relationship`、`sensitivity=personal` 均合法，确认时的同事件状态传播已由 `memory_search_document_sources` 覆盖），并补了 DSN 门控契约 `test_postgres_person_alias_is_recallable_and_status_gated`（本地无 DSN 跳过，须由 CI `python` job 实跑才算通过）。仍未验：真实 Qwen 抽取器（非规则）是否覆盖该别名句式；固定集之外的未见改写集尚未报告 recall/nDCG。
- 完成条件：三项命中正确证据，原长程召回不退步、隔离/候选泄漏为 0；固定集与未见改写集分别报 recall/nDCG，验证实际 ResponsePlannerClient 超时/catalog 限额，设备追问另取 Actual Heard。未消费 MemoryContextClient 的 0.3s 不是现网保证；不引入新向量库/平行记忆服务。
- 入口：`scripts/evaluate_memory.py`、`services/archive/{recall_planner,memory_extractor}.py` 与 `evaluation/memory_eval_zh_v1.json`；现有诊断见 `outputs/acceptance/run-20260915-p0-04-student-safety-loop/memory-eval/P1-06-diagnosis.md`。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03/P0-04、真实 VoCat、双端采集与操作员；签名放行 voice 须另获明确授权，当前仅 button/keyword。
- 先验 Exact DAC reference/通道映射、pre/post AEC residual、近端保留与双讲，再测播中告别/打断。不得绕签名、开播放期 KWS、提高 DTLN 或丢采集假装 AEC；保留当前固件与紧邻回滚。
- 完成条件：同候选/身份/策略/fence 的 T1–T14 逐格有证据，非主人/回声不越权、BOOT/触摸硬停不退步；电视/家庭噪声按距离、次数、时长量化。未过项保持 blocked/failed，只有完整硬件门禁后才改全双工标志。
- 可得出当前 SKU 只支持已测范围的结论；确认硬件天花板后再考虑 XVF3800，采购不混入实验。

## P2：质量增强与后续能力

### [ ] P2-01 统一现有记忆预取与 token 预算

- 依赖 P1-06。已有 snapshot→ResponsePlannerClient.prefetch_context→context-prefetch；追清未消费 MemoryContextClient 后决定接入或删除，不另建预取/缓存链。
- 完成条件：按 subject/session/fence 隔离并拒迟到缓存，memory-token 硬预算可观测/可裁剪，persona 与当轮情绪分开；超时可空降级、召回不退步、热路径延迟有前后对照。入口 `agent.py/duplex_runtime.py/routes/interaction.py`。

### [ ] P2-02 多成员声纹与不依赖小程序的选人

- 依赖 P0-04，切人格/音色另依赖 P1-03。先确定识别人范围、单独同意/撤销与设备确认交互；owner-only 登记不能区分家人。
- 完成条件：逐人登记、冲突/不确定/访客隔离、撤销/审计获 PG 与设备证据。识别只提出候选，不把语音自报、profile_id 或邀请升级为 owner/敏感授权；未定交互前不复用 BOOT/拍打或改唤醒词。入口 `services/speaker/authority.py`、identity 与 multi_subject。

### [ ] P2-03 可证明删除与导出证据链

- 依赖 P0-04；备份处置衔接 P1-08，不等待启用异地副本。先核现有删除/撤销/导出，再补缺失的 subject 谱系与离线核验回执，复用 Archive/EvidenceEvent/consent 权威。
- 完成条件：PG、MinIO、投影/缓存、音色/声纹范围一致，重试幂等、回执可查询、导出有 AI/授权标识；对保留备份写明期限与恢复后再删除，不承诺即时物理抹除全部副本。若发现实际越权/撤销失效，具体缺陷立即提 P0。

### [ ] P2-04 协议故障注入与长稳观测

- 复用现有 Edge/Voice Core 契约和 offline harness。覆盖 WSS 丢帧/乱序/重连、Bridge/Agent 退出、未知配置、迟到终端、profile 失效、并发与资源泄漏；查清入队控制/error 帧在 read-loop 关闭 lane 后丢失的通用路径。watermark 4002 局部修复不等于整体闭环。
- WSS 1005/TLS、BMI270 I2C 是已有孤立观察，未证明为播放卡死根因；Edge 旧 Go/Trivy 风险需重扫当前候选再判，不以旧报告宣称当前受影响。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行，不伪造 Actual Heard/commit；预定义长稳窗口并保留内存/连接/延迟趋势。先补测量/注入，不顺手改断线产品行为；假设备不能代替 P1-07。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：用户 2026-09-14 暂缓；真实家庭服务/数据、正式发布或价值量级增长前重评。现存本地还原点保留，WAL 增长仍按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收，不宣称通知已送达；启用需另定授权和渠道。
- EOU 新模型、DuplexModel、expressive/抢跑：现有语音基线与收益假设明确后再评估；TurnPhase 仍 shadow，不新增控制权威。
- ESP-IDF/ESP-SR/upstream 整体升级：有现板修复或安全依据才立项，仍须固定 upstream、clean overlay build 和身份区保护。
- 老年人故事册/人物复刻、年轻人潮玩、最终外形保留阶段方向；当前学生线未闭环前不扩张。外部研究继续归 `RESEARCH.md`，不自动转为工程任务。
