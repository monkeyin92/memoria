# Memoria 优先级执行清单

更新于 2026-09-17（第五轮，复核 2026-09-16 提交与证据），审查基线 `cb9fe6560d725b01fdacff6766db1eb08d360447`；本地与远端 `main` 已核对一致。本文件只保留未完成事项及其必要证据边界；完成后把仍有效的运行结论归入 `HANDOFF.md`，并删除任务/子项，不积累完成记录或另建归档。已有编号不复用；生产、设备、回滚与原始证据以 `HANDOFF.md` 中带日期的收据为准，不把旧文档状态当成当前验收。

## 下一步与执行边界

1. 先处理 P1-01 当前发布阻塞：`ea05ab9` 的真实 exporter 探针算错仓库根目录，最新 CI 的 `agent-image` 导入失败，`python` 又在 coverage 合并阶段失败。修路径并补无 editable 安装、带 coverage 的回归，再重建候选；不能沿用旧镜像/旧 CI 成功或跳过新门禁。
2. 同时推进 P0-04 软件闭环：约束谁可声明监护关系，明确无账号孩子的 person 级 consent 授予/撤销路径，补两条策略 API 的负例与并发矩阵，再接 P1-03 的最小 app_confirm/年龄资料入口。已有真实 PG 签名链测试不代表这些问题已解决，绝非只剩真机验收。
3. P0-03 通用 batch retry 已在 `3e0b738` 实现，不再重复开发；补下面的时间戳关闭/流式 fallback 边界和 G 的软件矩阵。完成拟发布 slice 后冻结同一候选，按授权构建、切流与回滚验证，再复跑 G/B/D、退出 speaking 时序、时延和学生设备链。P2-05 先修计数工具，下一设备窗口再采数。
4. WAL 保留策略仍单列 P1-08，可独立只读测量；生产删除/重启/启用定时任务须另获授权，不能随自动备份暂缓一起搁置。

本轮范围：审查 `c055004`（persistent Runtime 链）、`3e0b738`（TTS retry）、`ea05ab9`（真实 exporter）、`f405a92`（唤醒工具）、`cb9fe65`（文档）及关联实现；只更新本清单，未改业务代码、部署、刷机或访问生产/设备，原始验收收据不改写。本地定向复核：release 脚本 18 项、TTS 50 项、真实 PG persistent Runtime 链 3 项通过；另用现有 mock/本地 HTTP 探针复现下面的 TTS 与监护声明问题。

最新 CI `35116668250`（HEAD=`cb9fe65`，2026-09-16 23:47 CST 结束）整体失败：changes、agent 通过；agent-image 失败；python 的 5062 项断言通过、2 项跳过，但 coverage 合并报错退出 3，后续 Coverage gates/Offline E2E 未执行；firmware、miniprogram、media-edge-image、media-edge 跳过。旧 `35092363159` 对应 `28a41ee`，不代表本次 HEAD。最新已记录 Agent/Bridge 为 `d96d4c2`、板卡为 `d1ad38f`，本轮未刷新在线状态；HEAD 的 `code/wired` 不能记成生产 `enabled/verified`。`HANDOFF.md` 的 `ca9dae1`/未提交测试与镜像已通过等头部表述已滞后，须在下一次交接/发布前同步，不能回改历史收据。

## P0：发布前必须闭环的安全与语音问题

### [ ] P0-04 修正当前使用人的监护授权与学生安全闭环

- 前提：成人管理账号可创建独立 `under_14/14_17` 使用人，不要求孩子另建登录账号；以当前使用人及合法监护关系决定策略，成人学生不自动视为未成年人。最小 app_confirm/年龄资料入口优先，不重建多主体权威。
- 已有代码边界（`e5f9d50`/`c055004`）：不再替孩子确认关系或伪造微信 active link；`guardian_of` 仅家长一侧确认、仍为 pending。两条策略入口按签名 profile 的 active subject 判类别，subject≠account 时禁止读取账号键旧档案，`owner_display_name` 不串人；配置了主体权威却读取失败时保持保守门与公开固定回复。声明通知不等于验证监护或有效 consent；真正的 subject 键记忆在 `services/memory_scope`，会话记忆迁移尚未完成。
- 证据边界：真实 HTTP device-binding → session → app_confirm → device runtime-profile 已有测试；本轮重跑 `test_persistent_runtime_policy_chain.py`，真实 PostgreSQL 的 persistent Runtime → `/session-policy`、`/response-plan` 3/3 通过。账号/监护 store 仍用 SQLite，memory catalog 是替身；旧 epoch 仅在 Runtime authority 中判 stale，不能外推两条 HTTP 入口的并发隔离。此前 PostgreSQL/RLS、init + repeat-upgrade 收据仍有效，但不是本轮全栈/设备验收。
- 已闭环（`e1878ce`，声明来源约束）：`guardian_of` 声明只有带设备绑定证据（`guardian_declaration_v1:device_binding`）且声明人是某个 ACTIVE binding 的 `account_owner`、该 binding 把主体列为 `primary_subject` 时才算数；`parent_for_child` 的 guardian 角色还必须是 binding owner 或持有效双边 `guardian_of`。第三方 invite+self-accept 仍会留下 pending 行，但不再进入 `declared_guardians`、不能取得 guardian 角色、也不会成为危机通知收件人；撤销/过期后自动退出。存储层 `has_source_confirmed_relationship` 保持无范围语义（建绑定时尚无 binding 行），绑定范围判断在 `IdentityService.declared_guardians` 与 PG `identity_relationship_declared_for_binding`（通知入队与危机读取策略共用）。
- 已闭环（`77fc86a`，无账号孩子 person 级 consent 闭环）：`parent_for_child` 下的无账号孩子主体没有独立登录账号，旧链路 `/v1/guardian/links` 强求账号 profile 导致 consent 无法授予。新增 `PersonConsentRecord` 领域实体与 SQLite/PostgreSQL 存储层（`guardian_person_consents` 表，强制 FORCE RLS，配对 `guardian_controller_person_consents` 策略与 maintenance 数据治理统计）；`POST/GET/DELETE /v1/guardian/minors/{person_id}/consents` 路由由活跃 `parent_for_child` 绑定的拥有者家长控制；读门 `active_consent(minor_user_id, consent_kind)` 统一对 link-scoped 和 person-scoped 做并集解析。端到端 HTTP 用例验证：授予后 `/session-policy` 解除 `ephemeral_only` 记忆天花板、撤销后重新收紧恢复保守态、非绑定拥有者 403、`/response-plan` 补齐 `unknown_safe` 模式下直接 200 且 0 条记忆项泄漏回归。
- 软件验收矩阵：两条策略 API 都覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、无同意/撤销/过期/无权限、换合法管理账号；切人/改年龄与旧 epoch/旧 generation/旧缓存并发不得串人。使用真实 subject/account 键 catalog 补隔离证据，不把 catalog 替身或一次手工变异当作完整数据链验收。
- 入口前置：先接 P1-03 的最小使用人/年龄资料与 app_confirm 控制入口，完整人格/音色后做。零同意可建 HTTP session 不直接等同设备越权，须另验设备实际 admission 与受限能力门。
- 设备验收：在授权窗口走正式 app_confirm 确认使用人及年龄，核设备取得的签名 profile 与有效监护授权；不手工注入 profile、不把成人代讲当孩子身份证据、不绕声纹/准入门。受控成人模拟危机，固定话术逐字交付、终端回执与人工听感齐全；outbox 绑正确孩子和获授权收件人、重放幂等、家长作用域可读、原文不外泄，通知失败不吞公开回复。发送 worker/外部投递按 2026-09-14 决定暂缓，不把 outbox 称为送达。
- 完成条件：类别、有效同意、通知与数据作用域在上述软件矩阵和真实设备链一致，分别记录 code/wired/enabled/verified。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/identity/service.py`、`services/guardian/{sqlite_store,postgres_store}.py`、`test_student_safety_loop.py`、`test_persistent_runtime_policy_chain.py`。

### [ ] P0-03 收口 TTS、续问竞态、设备停滞与真实时延

- 当前设备证据：2026-09-16 H 同会话天气→续问→播后告别成功，F 的 48.28s 九天天气完整听完；但 B/D 停滞、G 续问丢失、H 时延越线，整体稳定性未通过。不跨 release 累计轮数，不把原笔记的约 55s 阈值或告别 3/5 当作结论。
- 已有软件基线（`7c0ef48`/`3e0b738`）：Doubao/CosyVoice × stream/batch 共用 `GenerationBudget` 的首包、进展续期、停滞与硬上限；batch retry 已明确为音频前可重试/回落、已有音频仅缺词时间戳时最多同音色重试一次，其余音频后失败终态、丢弃失败缓冲。本轮 generation budget + 两 provider mock 50 项通过；已核当前 LiveKit 在 pushed_duration>0 后不重试流式输出，未发现整句重放的该项回归。不能把 batch 的最多两次扩写成所有流式尝试也最多两次。
- 本轮发现（P2，条件性）：`COSYVOICE_WORD_TIMESTAMPS=false` 时 batch 仍强制要求 `all_words`，完整音频但无词会抛 `CosyVoiceTimestampError`，随后以同一关闭配置再合成一次。本地 `empty_ts` mock 复现 2 次 run request、两个 flag 均为 False，最终异常；真实厂商/现网是否使用该配置未验。先明确不支持时启动即拒绝还是允许无对齐音频降级，再让重试遵守该配置，补 mock 尊重 flag 的用例；不能用重复整句合成补不存在的时间戳。
- 本轮发现（P3，流式回落放大）：Doubao `_run` 每次被框架重入都从个人音色再回落设计音色，缺少 CosyVoice 的一次性闸门。本地连续音频前超时、`max_retry=2` 时实测 6 个 provider session、同 fence 回落回调/trace 各 3 次。后续收口 stream 级回落次数与总体尝试预算，补框架级重试用例；这是额外请求/时延，未证明账本重复或音频后重放。
- G 已有本地修复：`owner_silence_remaining_s` 为 `None` 未测量 / `0.0` 已耗尽 / `>0` 暂停剩余；结束已测预算不再白送满窗口。受理 ASR final/VAD 可否决等锁的迟到关闭，但仅在另有绝对说话看门狗或未到期 grace 时成立，不刷新静默预算。相关区分度与保护用例见 `HANDOFF.md`，不再把修复前失败写成当前 HEAD 失败。
- G 待闭环：明确无验证说话人且无其它界时的 owner_silence_timeout 语义（现保守关闭）；补 endpoint/commit 乱序、watchdog 交接在真实 10s/60s 配置下的矩阵，再在冻结候选捕获 G 终态。没有真机捕获不宣称续问不再丢失。
- EOU/告别边界：sidecar/END_SESSION 只能生成 END_CANDIDATE/clock fact，关闭仍经当前使用人的 subject capability；guest 只能停止公开播放或等待超时，不能由分类授予关会话能力。TurnPhase 保持 shadow，不造第二话轮控制面。
- 部分音频失败：本地 `test_media_output_partial_failure.py` 已验首帧后崩溃/停滞/硬期限有界落到后继代 CANCEL_GENERATION、交付 ERROR、provider_complete=false、权威 listening，旧代 ACK/ENDED 不复活。仍须同候选捕获 Bridge/Edge 终态与设备实际退出 speaking 的时序；历史 B 是旧总挂钟 20.23s 出错、约 38.6s 后才收尾，进程内回归不能代替设备闭环。
- B/D 独立定位：57.46s/58.88s 是桥侧音频长度，不是设备运行到该时长才卡住；两次 1.5–1.6s 先有 366/412ms supply wait，数秒内 speaking/控制台停滞，D 无同类 TTS 错误。补同 fence 的 Bridge→Edge WS writer→设备接收/解码/播放及任务/锁证据；drop=0 不证明收齐，不预设根因或先实现 pre-roll。
- 时延门：H ACK→正文 1.969s 未过 <1.5s；设备 VAD end→首帧 4.557s（A 第二问 3.748s）。拆 ASR finalize、查询、TTS 与播放，不用网络首帧冒充精确可闻延迟；保留明确 1–16 天天气/完整地名、预算、单次 ACK、防重复和代际隔离，不加第二提示、延长静默或放宽门禁遮掩。
- 完成条件：同一冻结且已启用候选上，三轮天气→续问→播后告别、>45s 长答、B/D 同类长答、临近静默与部分下发后故障均有完整捕获、fence/终态及操作员听感；有效输入才计告别可靠性。ACK→正文达标、失败有界退出，缺测如实 pending。
- 同场补验待机+五表情照片及点屏/摇晃/短拍/BOOT 不回归，按 `HANDOFF.md` 的 device window 执行。Exact DAC/AEC/播放期 voice 权限另在 P1-07，本项通过不等于全双工。入口：`providers/{generation_budget,doubao_tts,cosyvoice_tts}.py`、`voice_core/media_session_{standby,output_stream}.py`、provider mock/standby race 测试、固件 PlaybackSupplyMeter、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与学生产品闭环

### [ ] P1-01 修复最新制品门禁失败，完成 1.8.1 候选发布与回滚验收

- 当前候选仍为 LiveKit agents/openai/silero 1.8.1，配套 RTC 1.1.18、API 1.2.1；保留 Python 3.12 与真实锁，不用 --no-deps、不重复同组升级。1.8.2 若要评估须另开完整兼容候选，不能以 run-loop/tracing/transcript 改进推断解决 VoCat AEC/卡顿，也不混 expressive/DuplexModel、抢跑、user_turn_limit 或 TurnPhase 副作用。
- 已有接线（`ec56d9a`/`ea05ab9`）：全量 Dockerfile、delta 模板、source-overlay 都执行候选自带 `verify_agent_release_artifact`；部署前按显式候选身份+真实 override 链解析 Agent/Bridge 镜像，缺候选、旧 override 覆盖等会拒绝。真实 OTLP/HTTP exporter 的 5 种 fresh-process canary 正反例已实现并接进 verifier；剩余不是新增 exporter，而是修复并验收这条实际运行链。
- 当前阻塞一（P1，镜像可复现）：`services/agent/tests/integration/telemetry_pii_probe.py:49` 的 `Path(__file__).resolve().parents[3]` 指向 `/app/services`，`run_case` 又以它作 cwd/PYTHONPATH。未安装项目本身的镜像在 5 个 emitter 子进程中全部报 `ModuleNotFoundError: No module named services`；CI `35116668250` 的 agent-image job `104863778509` 在构建期 gate 失败，镜像内运行用户复验未执行。本地 editable `.pth` 能掩盖导入错误，脚本单测绿不代表镜像绿。
- 当前阻塞二（同探针的 coverage 上下文）：python job `104863778746` 在 5062 passed 后报 `Can't combine statement coverage data with branch data`，退出 3。子进程切到 services 后漏读根目录 `pyproject.toml` 的 branch=true，继承的 pytest-cov 环境产生 statement 数据。本轮只跑 exporter 5 例即复现同错误；仅在进程内改正 `_REPO_ROOT` 的对照 5/5 且退出 0，显式绝对 `--cov-config` 的另一对照也通过。对照只定位根因，未修改源码/CI，不能当作正式 85%/90% coverage 门已过。
- 下一步：修仓库根路径（此布局应为 `parents[4]`），把 cwd/PYTHONPATH 与 coverage 配置传播作为回归条件；在无 editable/未安装项目的环境验证包导入，在正式带 coverage 命令下验证数据能合并。不得以删探针、CI 加 --no-cov 或降低 coverage 阈值绕过。重跑 agent-image 与 python 的实际门禁，后者还须执行 Coverage gates/Offline E2E。
- 构建验收：先冻结本次拟发布 source/lock 和 P0 slice，标准完整构建实际受影响镜像；Agent/Bridge 共用同一产物，依赖变化不能 source overlay。验证构建期 gate 与非 root 运行用户离线复验，保持三条构建路径的 COPY/chmod 权限保障；旧成功镜像收据早于本次 exporter 提交，只能作历史基线。
- PII 验收：保留真实 OTLP collector 收到 span 的正例与故意绕 bootstrap 的泄漏反例，并同时查属性和原始导出字节。核 fresh process 的 env 未设置/空值/显式 0 与入口加载；SDK import-time 开关 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`、`LIVEKIT_TELEMETRY_ALLOW_PII` 必须落实到候选运行态，`PII_REDACTION_ENABLED` 或 InMemorySpanExporter 不可替代。
- 发布验收：以上门禁与拟发布 P0 回归通过后，重新读取真实容器、env、override、当前/紧邻回滚与远端收据，再按明确授权切流；复核 provider/LiveKit、12 个具名 readiness、外部路由、设备/时延并实际演练回滚。只保留当前+一个可运行回滚及必要依赖底座，不自动清理生产。
- 交接与完成条件：同步 `HANDOFF.md` 的 reviewed_source_commit、已提交范围与当前 gate 状态，保留带日期的历史 enabled/verified，原始收据不改。code/wired/enabled/verified 分层有证据，构建、真实 exporter、生产切流与回滚各自过门，不以 18 项脚本测试或旧 CI success 替代。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 与自动/异地备份暂缓分开处理。先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧“约 1GB/天、6 周写满”不能作当前预测。
- 需用户决定并授权：可证明安全的裁剪策略、停用 archive_mode（需 PG 重启），或明确频率/阈值的手工处理。不能仅按 mtime 删除并破坏仍保留 base backup 的恢复链，也不清理 pg_wal 代替归档保留。
- 完成条件：选定策略有保护集合、容量/恢复影响和可复核执行证据；保留的恢复目标仍可验证，后续增长有处理责任与阈值。启用自动备份/异地副本另行确认，不搭便车实施。

### [ ] P1-02 让 ASR 救援 sidecar 可复现并验证真实输入

- 先读生产启动脚本、Dockerfile、包/模型/词表摘要、基础镜像与输入语言，核对实际消费者；将真实构建输入纳入仓库，保留独立镜像。DashScope 模型服务版本不是 FunASR Python 包版本；仓内 sherpa-onnx 实现不支持“升级 FunASR 即修 sidecar”的推断。
- 仅在实际使用 FunASR 且确有对应修复时做旧/新 A/B；否则核真实后端，不向主 Agent 添无用依赖或顺手改 NumPy/设备 VAD。
- 完成条件：仓内输入可重建；真实中文 PCM 短句、长段/尾字、静音、低 RMS、削波、并发/失败降级通过。分报 vendor_error/vendor_silent/gating/low_rms、救援耗时与 2.5s 超时行为；有收益且无回归才另行切流。
- 已有回归（2026-09-16，本地合成波形，非真实录音）：`services/agent/tests/integration/test_funasr_rescue_audio_shapes.py` 覆盖静音与室内底噪被本地门禁拦下（`rescue_total{outcome="skipped"}` + `empty_transcript{class="low_rms"}` 且厂商零请求）、削波满幅信号按上行原始字节送判、超长段只把最新尾帧（内容精确比对）送去救援、三路并发 + 慢/失败厂商降级为有界 `no_text` 且不抛不卡、以及在飞行中的救援发布 `now + 2.5s` 预算（默认 `SENSEVOICE_TIMEOUT_S=2.5`）。仓内既有的 `test_funasr_empty_accounting.py` 覆盖四个分桶判定。
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
- 合规与耗时边界：声纹/声音样本使用单独同意，不并入一般同意；本地样本体检不是“一分钟内可用”的瓶颈，剩余证据是 provider 克隆到设备可听的真实端到端耗时。保留 60s 对客预算和 120s provider 上限，不因超时放宽样本校验或把 failed 否决改成 ready。
- 入口：`apps/miniprogram/pages/profile/`、`services/voice_profile/`、`routes/voice.py`。

### [ ] P1-05 补权威会话状态，再做小程序三端验收

- 只读状态开发可独立进行；集成使用 P1-03/04 同一候选。以 Python→Edge 的 assistant_state.phase 为源，补缺失的 Edge→Control 受鉴权只读出口，不把连接在线猜成 listening/idle。
- 投影带 session/generation fence 和新鲜度，重连/断线/过期显示 offline/unknown；不从字幕、零散 diagnostics 或客户端计时猜态，不增加话轮控制面。
- 完成条件：微信手机/电脑/开发工具同版本覆盖登录绑定、三态/断线、主体人格切换、样本进度、回顾、权限拒绝与刷新；录音范围门禁通过。0.8.84 仅开发版；体验版、提审、正式发布分别授权和记录。
- 入口：`services/media_edge/{bridge_runtime_events,session_shadow}.go`、`routes/device_control.py`、`apps/miniprogram/`。

### [ ] P1-06 修复三类召回遗漏与人物抽取

- 可本地并行。基线 recall@5=0.8125（13/16）、nDCG@10≈0.734；已有修复后的固定集 recall@5=0.9375（15/16）、nDCG@10≈0.859，`candidate_leakage`/`cross_account_leakage` 仍为 0、`extraction_recall`=1.0、p50≈0.72ms（见 `HANDOFF.md` 的三处改动）。仅剩 `repeated-episode-campus-startup`，它卡在下面的产品语义，不靠放宽判据抬分。
- 待定语义：review 确认证据/claim 是否同时提升其 person/episode/knowledge；跨会话 episode 是否合并。现有逐事件 document 不能满足“同 episode 含两条 source_event_ids”的判据，先定产品写入语义，不能绕过判据抬分。
- 已有修复（2026-09-16）：① 忌口转述——`RecallPlanner` 封闭词表新增“避开/忌口/不能吃/别吃/注意别/过敏”标记 → “不喜欢/讨厌/不吃/忌口/不要”扩展，并让评测 adapter 与生产读路径一致（生产**总是**先 plan，之前 13 个无 clock 的用例根本没走 planner）；② 别名——规则抽取器新增“家里人/大家/别人…叫(她|他)X”封闭句式，只在唯一人物且非角色词时挂为该人的 alias；③ 人物不可召回——person 之前只存在于 `person_aliases`，没有任何读路径会搜它，因此“阿梅是谁？”永远召不回该人物；现已在编译期为人物建立 search document（title=display_name、body=关系+全部 alias、kind=person/memory_kind=relationship、sensitivity=personal），确认时随同事件投影一起提升，未确认人物仍被 confirmed-only 的 `context` 挡住。
- 人物投影已同步到生产路径：`postgres_memory_catalog.py::_write_extraction` 现在也写同名 search document（PG schema 的 kind 无枚举约束、`memory_kind=relationship`、`sensitivity=personal` 均合法，确认时的同事件状态传播已由 `memory_search_document_sources` 覆盖），并补了 DSN 门控契约 `test_postgres_person_alias_is_recallable_and_status_gated`；此前真实 PostgreSQL 与旧 CI 的 `python` job 已通过；当前 HEAD 的 CI 状态见顶部。仍未验：真实 Qwen 抽取器（非规则）是否覆盖该别名句式；固定集之外的未见改写集尚未报告 recall/nDCG。
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
- 导出细则：用户语音与模型回复的混合导出必须显式标记“AI 生成”，元数据写入服务提供者与内容编号；TTS/合成音频核对 `aigc_watermark`/`aigc_metadata`。声纹、原始 WAV、特征不默认长期保存；家庭邀请不升级为声纹登录权限。

### [ ] P2-04 协议故障注入与长稳观测

- 复用现有 Edge/Voice Core 契约和 offline harness。覆盖 WSS 丢帧/乱序/重连、Bridge/Agent 退出、未知配置、迟到终端、profile 失效、并发与资源泄漏；查清入队控制/error 帧在 read-loop 关闭 lane 后丢失的通用路径。watermark 4002 局部修复不等于整体闭环。
- WSS 1005/TLS、BMI270 I2C 是已有孤立观察，未证明为播放卡死根因；Edge 旧 Go/Trivy 风险需重扫当前候选再判，不以旧报告宣称当前受影响。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行，不伪造 Actual Heard/commit；预定义长稳窗口并保留内存/连接/延迟趋势。先补测量/注入，不顺手改断线产品行为；假设备不能代替 P1-07。
- SiphonAI 只借鉴协议公开化、conformance harness、20 ms 热路径和掉线保会话的工程做法；不引入 `siphon-rs`/`forge-media`，不替换 Go Media Edge，不把 PSTN 入线带入当前范围。

### [ ] P2-05 修正唤醒计数边界，再补“茉莉”家庭噪声矩阵

- 历史安静真人阶段有“茉莉”10/10、5 分钟零误唤醒，不跨版本累加。本轮只离线复核 `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/{receipt.json,analysis.json,console.log}`；未连接设备、未改原始收据。
- 可确认的 2026-09-16 设备事实：Tingting 合成 TTS（前后静音垫 0.25s/0.4s）能唤醒该板；原始矩阵 gain 1.0 为 4/8、gain 0.3 为 8/8，合计 12/16。gain 是播放增益，不是测得的近/远物理距离。成功试次刺激开始→终端唤醒行中位 1.445s，包含前导静音与采集时序，不当作精确声学处理延迟。
- 修正因果结论：首次 activating→idle 为 23:18:58.330 CST，前四个漏唤醒刺激在此后约 7.35/17.54/27.71/37.98s；首个成功唤醒在约 49.59s，之后 12/12 是事后子集，不可据此剔除原始失败。单次开机、先高增益后低增益不足证明固定 45–50s 预热，也不能排除召回/音源/顺序因素。工具默认 60s 等待只是暂定实验参数；后续多次冷启动、固定刺激、交错/随机增益次序对照，分别报告冷启动和预先定义的稳态窗口。
- 修正误唤醒分母：按带 `(state: N)` 的首次唤醒行去重后，新事件确为 0；原 receipt 的 TV=1 是上次试次的滞后重复行。但 TV 窗口 23:23:11.464 起共约 133.038s，日志状态为 connecting/listening/speaking，idle 为 0s；其中约 92.382s 在 speaking，固件禁用播放期 KWS。small_talk 共约 124.471s，仅约 100.909s idle；quiet 约 300.001s 均为日志可见 idle。故不能宣称 TV/多人各 2 分钟有效零误唤醒；静默窗口的已观察零事件可保留，但不替代完整 detector-on 证据。
- 工具必修：`scripts/wake_word_matrix.py:694` 后的干扰/静默窗口没有待机门，也未累计检测器启用时长，上一会话会污染分母。每窗先确认 idle+detector-on，状态离开时暂停有效暴露计时并记录原因，去重与窗口边界统一；串口断流/播放失败/状态未知标 invalid，不算漏唤醒或零误唤醒。先用 fake console/播放器覆盖边界与断流，再开设备窗口。不得为凑曝光开启播放期 KWS。
- 工具可靠性：音量/静音修改后的录音、串口打开也须纳入 finally 恢复；恢复成功须实读核对，不能无条件写 true。真人模式的 Markdown 与 JSON 应一致，真人延迟不从人工提示时刻冒充实际开口计；记录各档实测输入电平、warmup 配置及实际等待，保留静音输出/空音频校验。
- 剩余设备矩阵：真实电视/家庭噪声音源（现为 TTS 模拟）、物理距离/角度、真人说话者对照；固定候选/固件/settings 并记录窗口状态、真值、输入范围、召回/误唤醒/时延。既有日志只读到 app 2.4.2、ELF 前缀 da6ebdd16，flash receipt 不是该轮回读；日志明确 detector=MultiNet，先核真实检测器/模型及阈值权威，不继续笼统写调整 WakeNet threshold。
- 完成条件：修工具后用预定义协议取得可比较 receipt，原始分母、有效暴露时长、候选/固件身份和未测边界可复核；数据证明必要后才调检测阈值/词形。该项不改写 advertised_duplex_level=none 或 aec_reference_verified。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：用户 2026-09-14 暂缓；真实家庭服务/数据、正式发布或价值量级增长前重评。现存本地还原点保留，WAL 增长仍按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收，不宣称通知已送达；启用需另定授权和渠道。
- EOU 新模型、DuplexModel、expressive/抢跑：现有语音基线与收益假设明确后再评估；TurnPhase 仍 shadow，不新增控制权威。
- ESP-IDF/ESP-SR/upstream 整体升级：有现板修复或安全依据才立项，仍须固定 upstream、clean overlay build 和身份区保护。
- 老年人故事册/人物复刻、年轻人潮玩、最终外形保留阶段方向；当前学生线未闭环前不扩张。外部研究继续归 `RESEARCH.md`，不自动转为工程任务。
