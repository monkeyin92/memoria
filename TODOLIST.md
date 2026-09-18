# Memoria 优先级执行清单

更新于 2026-09-18（advisory 整改：Doubao 真重入回归、CosyVoice 取消优先、入窗曝光起点、回顾可追溯）。本轮在 `f2a95d6` 之上改代码+测试，已提交（`27a16cf`/`0d200c6`/`39e7fa2` docs、`768993b` 唤醒严格自包含；`8d184e3` 起审查基线升到 `39e7fa2`）、未部署、未构建发布候选、未连接生产或设备；远端 CI `35298356748`（`768993b`）success：python 全量 5109 passed/2 skipped、覆盖率 88.11%、wake 12 passed、Offline E2E PASS（provider smoke 仍 `OFFLINE_MOCK=true`）。本文件只保留未完成事项、必要基线、依赖与验收条件；完成后把仍有效的运行结论归入 `HANDOFF.md` 并移出队列，不积累修复流水账。已有编号不复用。

## 下一步与执行边界

1. **成员写入边界已修（`350d62d`）**：三个缺陷都有修复前失败的回归，并发项另有真实 PG 契约（见 `HANDOFF.md`）；剩余的是小程序成员 UI 接线与设备链。
2. **读一致性已修（`f2a95d6`）**：`read_transaction` 固定 `REPEATABLE READ` 只读事务，profile/context/binding 三次读取共享同一快照；真实 PG 交错回归证明读间旋转/撤销不污染当次 `current()`、下一次新鲜事务可见。close-only 仍无 fence、可关闭；最终 action fence 保留；未复现跨主体泄漏，不标已泄漏。
3. **TTS 软件边界整改中（P2 已修，P3 重列待办转真回归）**：`COSYVOICE_WORD_TIMESTAMPS=false` 降级已补取消优先收尾；Doubao `slow_once` 旧测试收据作废，改用 `slow` 持续首包失败真回归（personal 仅 1 次、callback/trace 各 1 次、总预算 1+4）。G 续问矩阵、EOU/告别、部分音频设备终态、B/D 定位、时延门仍待设备链。
4. **P2-05 入窗起点已修、P1-05 收据已撤回**：曝光窗口从门通过时起算（settle 等待是入场成本）；回顾出口补 `owner_event_id`/`assistant_event_id`/`assistant_approximate`，撤回“无跨主体读”，同账号切主体/subject 围栏/临时读回待验。
5. 下一步（按序）：远端 CI 已在 `768993b` 通过完整 python 门；冻结同一 source/lock，按 P1-01 跑受影响门禁、构建候选；生产切流与回滚另获授权。旧 CI 通过不替新改动背书。
6. 下一设备窗口仍按 `fd0290a` 的**功能口径**：天气→续问→播后告别至少三轮、签名允许的 button/keyword 打断、>45s 与 B/D 同类长答、双方话轮与汇总可查。学生危机设备演练留到安全专项窗口；功能通过不等于学生安全或全双工验收。
7. 再接 P1-03/04 的成员、人格与声音完整 UI/设备链，推进 P1-06 的证据记忆和 P2-06 的陪伴效果评测。P1-08 WAL 可独立只读测量；删除、重启、定时任务不在本轮授权内。

当前证据：远端 CI `35298356748`（`768993b`，2026-09-18 10:11–10:22 CST）success——Ruff/module budget/strict mypy（435 files）/可复现协议与契约/authoritative PG gate/pytest 5109 passed/2 skipped/覆盖率 88.11%/wake 12 passed/Offline E2E PASS/agent-image success；provider smoke `OFFLINE_MOCK=true` 不算真实厂商验收；media-edge/media-edge-image/agent/firmware/miniprogram 按 filter skip。`35296113073`（`264e2d3`）python 唯一失败已解释：docs-budget 撞非常驻 `RESEARCH.md`，随 `3d7db99` 移除。

本轮整改（advisory 驱动，已提交 `27a16cf`/`0d200c6`/`39e7fa2` docs）：Doubao 真重入回归改用 `slow` 持续首包失败（修前 personal 被试 4 次、修复后 personal 1 次/callback 1 次/trace 1 次/总 5 sessions）；CosyVoice 降级补取消优先收尾（trace 回调置 cancel 可确定性复现旧错）；入窗曝光起点改门通过起算，分子/收据 wall 同口径（修前多算 ~1.5 s 且 settle wake 计入，修复后排除，report wall 回归锁定分母≈1.0）；回顾出口补事件 id 与 approximate 字段并撤回跨主体收据。`interaction-delegation-start` pending 提示仍待定位（P2-04）；生产 Agent/Bridge `d96d4c2`、板卡 `d1ad38f` 仍只是 `HANDOFF.md` 带日期的最后记录，本轮未刷新在线状态。
上一轮（`f2a95d6`）回归：2 个读快照交错、1 个无时间戳降级、2 个曝光时间线、1 个会话回顾在修前失败、修复后通过；wake 去重用例内联 3 行 fixture（删 ignored 文件 9/9）；CI 新增 wake 显式步骤。`f2a95d6` 的流式单次回落用例收据作废（`slow_once` 未触发重入，改前已通过），已由本轮真回归替代。

## P0：发布前必须闭环的安全与语音问题

### [ ] P0-04 修正当前使用人的监护授权与学生安全闭环

- 既有基线：person-consent 授予/读/撤销/解绑后撤销/幂等/导出，真实 PG 的类别、权威失效、profile-session 错绑、合法管理人变化围栏，以及真实 catalog 的账号/主体键隔离已有软件收据；Agent plan stamp、旧 epoch 驱逐和轮换清理已实现。保留这些回归，不重复开发，也不据此声称“软件全闭、只剩设备”。声明监护关系仍是 binding-scoped owner 单侧 pending，不等于 verified guardian、consent 或外部通知送达。
- **成员写入缺陷已修（`350d62d`）。** 空权限被恢复为角色默认值、并发新增丢成员两项在 `routes/identity_lifecycle.py` 与 `services/identity/service.py` 修复：追加逐字携带既有角色的权限集合（含空集合），`supersede_binding` 新增 `expected_binding_id` 比较并设置，路由对陈旧视图有界重试且每次都从新版本重算列表。真实 PG 契约证明同一版本的并发追加只有一个胜者、被拒尝试不留审计/版本行，带新版本重试后两名成员都在；仍未证明的是下游同意门是否曾被绕过（未复现）。
- **读一致性已修（`f2a95d6`，不标已泄漏）。** `read_transaction` 现固定 `REPEATABLE READ` 只读事务（只读故无谓词锁、无序列化失败；写侧仍靠各自 CAS），profile/context/binding 三次读取共享同一快照：读间切主体不产生新旧混对、读间撤绑定不翻转当次 fence，下一次新鲜事务可见。close-only 在失效后仍可关闭，不误加使用权门；最终 action fence 保留；未复现跨主体泄漏。
- 软件完成条件：两条策略入口覆盖 under_14/14_17/adult/unknown_safe、权威 unavailable/过期、profile-session 错绑、无同意/撤销/过期/无权限、合法管理账号更换；切人/改年龄与旧 epoch/generation/缓存并发不得串人。`session-policy` 不能决定时 503，`response-plan` 保留有界保守 200 和固定安全回复、零私密记忆；真实 subject/account catalog、Agent 缓存与新成员写入负例均须过门。
- 控制入口及产品链：成人管理账号可建独立 under_14/14_17 使用人，不要求孩子登录账号；成员写入已随 `350d62d` 修复，接下来接最小年龄资料/app_confirm UI。成人学生不自动视为未成年人，零同意可创建 HTTP session 不等于设备准入已验；会话记忆迁入现有 subject 键 `services/memory_scope` 仍需 P1-06/P2-01 配合。
- 当前设备窗口只验顶部四类功能，不手工注入 profile、不绕声纹/准入门；HTTP 文本正确不是设备已说出。安全专项随后再核 app_confirm 身份/年龄→有效同意→准入/受限能力→固定话术真实交付→outbox 绑定/幂等/家长读回；发送 worker/外部投递暂缓。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code/wired/enabled/verified`。`student_safety_loop_verified=false` 保持到安全专项也通过，不能由功能窗口代填。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/identity/service.py`、`services/session_runtime/{service,postgres_store}.py`、`services/guardian/`、`test_student_safety_loop.py`、`test_persistent_runtime_policy_chain.py`。

### [ ] P0-03 收口 TTS、续问竞态、设备停滞与真实时延

- 当前设备证据：2026-09-16 H 同会话天气→续问→播后告别成功，F 的 48.28s 九天天气完整听完；但 B/D 停滞、G 续问丢失、H 时延越线，整体稳定性未通过。不跨 release 累计轮数，不把原笔记的约 55s 阈值或告别 3/5 当作结论。
- 已有软件基线（`7c0ef48`/`3e0b738`）：Doubao/CosyVoice × stream/batch 共用 `GenerationBudget` 的首包、进展续期、停滞与硬上限；batch retry 已明确为音频前可重试/回落、已有音频仅缺词时间戳时最多同音色重试一次，其余音频后失败终态、丢弃失败缓冲。上轮 generation budget + 两 provider mock 50 项通过；当时核到 LiveKit 在 pushed_duration>0 后不重试流式输出，未发现整句重放的该项回归，本轮未重跑。不能把 batch 的最多两次扩写成所有流式尝试也最多两次。
- 缺口已修（P2，本轮补取消优先）：`COSYVOICE_WORD_TIMESTAMPS=false` 即“只要纯音频”——batch 返回完整 PCM、空词、`alignment_status="degraded"`，单次连接不重试；降级路径与正常路径共用取消优先收尾（cancel 先判，`discarded=True`、连接丢弃不复用）。真实厂商/现网是否使用该配置仍未验；仍不能用重复整句合成补不存在的时间戳。
- P3 重列为待办（上一轮收据作废）：`f2a95d6` 的 `test_livekit_stream_personal_before_audio_fallback_fires_exactly_once` 用 `slow_once`——第二次 baseline 已成功，根本没触发框架对 `_run` 的重入，且改代码前也已通过，不算“修前失败”。`_fallback_used` 闸门代码保留，但收据撤回。本轮改用持续首包失败（`slow` 阻塞全部 session）真回归：`max_retry=3` 下 personal 仅 1 次、callback/trace 各 1 次、总 session 预算 1 personal + 4 baseline；修前 personal 被试 4 次，修复后通过。
- G 已有本地修复：`owner_silence_remaining_s` 为 `None` 未测量 / `0.0` 已耗尽 / `>0` 暂停剩余；结束已测预算不再白送满窗口。受理 ASR final/VAD 可否决等锁的迟到关闭，但仅在另有绝对说话看门狗或未到期 grace 时成立，不刷新静默预算。相关区分度与保护用例见 `HANDOFF.md`，不再把修复前失败写成当前 HEAD 失败。
- G 待闭环：明确无验证说话人且无其它界时的 owner_silence_timeout 语义（现保守关闭）；补 endpoint/commit 乱序、watchdog 交接在真实 10s/60s 配置下的矩阵，再在冻结候选捕获 G 终态。没有真机捕获不宣称续问不再丢失。
- EOU/告别边界：sidecar/END_SESSION 只能生成 END_CANDIDATE/clock fact，关闭仍经当前使用人的 subject capability；guest 只能停止公开播放或等待超时，不能由分类授予关会话能力。TurnPhase 保持 shadow，不造第二话轮控制面。
- 部分音频失败：本地 `test_media_output_partial_failure.py` 已验首帧后崩溃/停滞/硬期限有界落到后继代 CANCEL_GENERATION、交付 ERROR、provider_complete=false、权威 listening，旧代 ACK/ENDED 不复活。仍须同候选捕获 Bridge/Edge 终态与设备实际退出 speaking 的时序；历史 B 是旧总挂钟 20.23s 出错、约 38.6s 后才收尾，进程内回归不能代替设备闭环。
- B/D 独立定位：57.46s/58.88s 是桥侧音频长度，不是设备运行到该时长才卡住；两次 1.5–1.6s 先有 366/412ms supply wait，数秒内 speaking/控制台停滞，D 无同类 TTS 错误。补同 fence 的 Bridge→Edge WS writer→设备接收/解码/播放及任务/锁证据；drop=0 不证明收齐，不预设根因或先实现 pre-roll。
- 时延门：H ACK→正文 1.969s 未过 <1.5s；设备 VAD end→首帧 4.557s（A 第二问 3.748s）。拆 ASR finalize、查询、TTS 与播放，不用网络首帧冒充精确可闻延迟；保留明确 1–16 天天气/完整地名、预算、单次 ACK、防重复和代际隔离，不加第二提示、延长静默或放宽门禁遮掩。
- 完成条件：同一冻结且已启用候选上，三轮天气→续问→播后告别、>45s 长答、B/D 同类长答、临近静默与部分下发后故障均有完整捕获、fence/终态及操作员听感；有效输入才计告别可靠性。ACK→正文达标、失败有界退出，缺测如实 pending。
- 同场补验待机+五表情照片及点屏/摇晃/短拍/BOOT 不回归，按 `HANDOFF.md` 的 device window 执行。Exact DAC/AEC/播放期 voice 权限另在 P1-07，本项通过不等于全双工。入口：`providers/{generation_budget,doubao_tts,cosyvoice_tts}.py`、`voice_core/media_session_{standby,output_stream}.py`、provider mock/standby race 测试、固件 PlaybackSupplyMeter、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与学生产品闭环

### [ ] P1-01 冻结修复候选，完成发布与回滚验收

- 当前候选仍为 LiveKit agents/openai/silero 1.8.1，配套 RTC 1.1.18、API 1.2.1；保留 Python 3.12 与真实锁，不用 --no-deps、不重复同组升级。1.8.2 须另开兼容候选，不以版本说明推断解决 VoCat AEC/卡顿，不混 expressive/DuplexModel、抢跑、user_turn_limit 或 TurnPhase 副作用。
- Mypy blocker 已修，上轮（`350d62d`）远端 `python`、`agent-image` 已通过（CI `35231388322`），本轮 `f2a95d6` 远端未跑；不再安排重复修复。全量/delta/source-overlay 已接候选自带 verifier 与真实 OTLP canary，cwd/PYTHONPATH/coverage 传播、运行用户读权限及 COPY/chmod 回归保留。
- P0 修复完成后冻结同一 source/lock；受影响候选须过 ruff、module budget、strict mypy、协议生成、真实 PG init + repeat-upgrade、pytest、85% 总覆盖/90% orchestration/90% provider protocols、Offline E2E 及 agent/agent-image 门。带真实测试 DSN，不把 DSN-gated 跳过称为全量 CI；不删探针、不降阈值。文档/低风险机械改动只做相称快速检查。
- 用标准完整构建验收受影响镜像，Agent/Bridge 共用同一产物；依赖变化不能 source overlay。构建期 gate 和非 root 运行用户复验分别留据，CI 镜像成功不等于生产启用。
- PII 门保留真实 OTLP collector 正例及绕 bootstrap 泄漏反例，同时查属性和原始导出字节；fresh process 覆盖 env 未设置/空值/显式 0。`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`、`LIVEKIT_TELEMETRY_ALLOW_PII` 落实到候选运行态，`PII_REDACTION_ENABLED` 或 InMemory exporter 不能替代。
- 门禁通过后重新读取真实容器/env/override、当前与紧邻回滚及远端收据，按明确授权切流；复核 provider/LiveKit、12 个具名 readiness、外部路由、设备/时延，并实际演练回滚。只保留当前+一个可运行回滚及必要依赖底座，清理生产另授权。
- 完成条件：同一候选的构建、真实 exporter、切流、回滚、设备验收分项有证据，`HANDOFF.md` 当前摘要和 `code/wired/enabled/verified` 一致；历史收据保留，不继承旧 success 或健康容器作为设备通过。

### [ ] P1-08 为仍增长的 WAL 确定独立保留策略

- 与自动/异地备份暂缓分开处理。先只读刷新磁盘、归档增速/失败、现存 base backup/逻辑 dump 与所需 WAL 连续区间；旧“约 1GB/天、6 周写满”不能作当前预测。
- 需用户决定并授权：可证明安全的裁剪策略、停用 archive_mode（需 PG 重启），或明确频率/阈值的手工处理。不能仅按 mtime 删除并破坏仍保留 base backup 的恢复链，也不清理 pg_wal 代替归档保留。
- 完成条件：选定策略有保护集合、容量/恢复影响和可复核执行证据；保留的恢复目标仍可验证，后续增长有处理责任与阈值。启用自动备份/异地副本另行确认，不搭便车实施。

### [ ] P1-02 让 ASR 救援 sidecar 可复现并验证真实输入

- ASR 主链使用云服务商 FunASR，不跟进本地 FunASR PyPI 升级。救援 sidecar 另核真实启动脚本、Dockerfile、模型/词表摘要、基础镜像与消费者；将缺失的构建输入纳入仓库，保留独立镜像，不把云模型版本等同 Python 包版本。
- 仓内 sherpa-onnx 实现不支持“升级 FunASR 即修 sidecar”的推断；只针对实际后端与已证实问题做旧/新 A/B，不向主 Agent 添无用依赖或顺手改 NumPy/设备 VAD。
- 完成条件：仓内输入可重建；真实中文 PCM 短句、长段/尾字、静音、低 RMS、削波、并发/失败降级通过。分报 vendor_error/vendor_silent/gating/low_rms、救援耗时与 2.5s 超时行为；有收益且无回归才另行切流。
- 已有回归（2026-09-16，本地合成波形，非真实录音）：`services/agent/tests/integration/test_funasr_rescue_audio_shapes.py` 覆盖静音与室内底噪被本地门禁拦下（`rescue_total{outcome="skipped"}` + `empty_transcript{class="low_rms"}` 且厂商零请求）、削波满幅信号按上行原始字节送判、超长段只把最新尾帧（内容精确比对）送去救援、三路并发 + 慢/失败厂商降级为有界 `no_text` 且不抛不卡、以及在飞行中的救援发布 `now + 2.5s` 预算（默认 `SENSEVOICE_TIMEOUT_S=2.5`）。仓内既有的 `test_funasr_empty_accounting.py` 覆盖四个分桶判定。
- 仍未完成：真实中文录音语料（短句/尾字听感、低 RMS 真实底噪）与生产启动脚本/Dockerfile/模型/词表摘要纳入仓库——后者按原记录只存在于服务器 `/opt/memoria/sidecars/sensevoice-asr/`，本轮未连接生产故无法取得；待核的是有效云模型标识、接口与救援后端，不新开本地 FunASR 包版本跟进。
- 入口：`funasr_stt.py`、`funasr_empty_accounting.py`、`providers/sensevoice.py`、`scripts/run_sensevoice_asr.py` 及 ASR 契约/救援测试。

### [ ] P1-03 修稳成员入口，再接按使用人切人格/音色

- 最小成员/年龄/app_confirm UI 先供 P0-04，完整人格/音色设备验收随后；不把依赖写成循环。成员追加、年龄申报已有 HTTP 路由，选人接口对外声明与写入口均只接受 app_confirm 的一致性已有回归；控制端三项写入缺陷（空权限被恢复、并发丢成员、`family_shared` 丢家庭空间）已随 `350d62d` 修复并留回归，剩余是 UI 接线。
- 小程序接设备/成员、年龄资料、分配/取消分配/邀请与 app_confirm；先核实际线上 Control 路由/schema，9 月 11 日的两文件 overlay 不代表当前全量后端。客户端 flag、自报姓名或 voice_question 不作身份证据。
- 完成条件：同 binding 两个 subject 得到不同人格/音色；切换推进版本、签名失效、next_safe_point 重协商，旧上下文/音频不串人；取消回落默认。内置只读、自定义创建即冻结、克隆未 ready 回落设计音色；PG 与设备实听分别验证。
- 入口：`routes/{identity_lifecycle,persona_assignment,custom_personas,multi_subject}.py`、`services/session_runtime/profile_service.py`、`services/voice_profile/`、`apps/miniprogram/pages/device/`；不另造人格引擎或身份权威。

### [ ] P1-04 自定义声音：样本上传到设备出声

- 依赖 P0-04/P1-03；先核已有真实音频校验、进度轮询、over_budget 逻辑是否在线，避免重复开发。
- 覆盖 profile 有界录音→上传→真实解码→训练→ready/failed/超时→分配→设备实听，连同拒绝、取消、重复提交、撤销/样本处置。
- 完成条件：无效音频不训练，无假 ready/百分比；实测 60s 产品预算与 120s provider 超时，超预算如实显示，未 ready 回落设计音色。小程序不扩张手机声纹、实时对话/WSS/TTS。
- 合规与耗时边界：声纹/声音样本使用单独同意，不并入一般同意；本地样本体检不是“一分钟内可用”的瓶颈，剩余证据是 provider 克隆到设备可听的真实端到端耗时。保留 60s 对客预算和 120s provider 上限，不因超时放宽样本校验或把 failed 否决改成 ready。
- 入口：`apps/miniprogram/pages/profile/`、`services/voice_profile/`、`routes/voice.py`。

### [ ] P1-05 补权威会话状态，再做小程序三端验收

- 最小会话回顾出口已修（`f2a95d6`，`code`，本轮补可追溯字段）：`GET /v1/archive/conversation-history?session_id=&turn_limit=` 返回一会话的配对话轮（history-eligible owner 文本 + actual-heard assistant 文本），每轮带 `owner_event_id`/`assistant_event_id`/`assistant_approximate`，与 `/conversation-review` 同一 owner 鉴权域、无 retention 同意长期保存；无 eligible 话轮返回空列表、不编造汇总。**撤回“无跨主体读”收据**：现有回归只建两个成人账号、只证 account 隔离；同账号切主体、subject 围栏、无 retention 临时读回仍待验（见下）。完整三端集成与 Edge→Control 只读出口仍待 P1-03/04 同一候选。
- 待验（P1-05）：同账号切主体后旧主体话轮是否可见、subject 围栏、minor 无 retention 时的临时读回语义；输出是“已听到”近似交付记录（`approximate` 默认 true），不是精确交付状态。
- 只读状态开发可独立进行；完整三端集成使用 P1-03/04 同一候选。以 Python→Edge 的 assistant_state.phase 为源，补缺失的 Edge→Control 受鉴权只读出口，不把连接在线猜成 listening/idle。
- 投影带 session/generation fence 和新鲜度，重连/断线/过期显示 offline/unknown；不从字幕、零散 diagnostics 或客户端计时猜态，不增加话轮控制面。
- 完成条件：微信手机/电脑/开发工具同版本覆盖登录绑定、三态/断线、主体人格切换、样本进度、回顾、权限拒绝与刷新；录音范围门禁通过。0.8.84 仅开发版；体验版、提审、正式发布分别授权和记录。
- 入口：`services/media_edge/{bridge_runtime_events,session_shadow}.go`、`routes/device_control.py`、`apps/miniprogram/`。

### [ ] P1-06 定义跨会话记忆语义，补未见召回评测

- 可本地并行。既有固定集修复后 recall@5=0.9375（15/16）、nDCG@10≈0.859，candidate/cross_account leakage 为 0、extraction_recall=1.0、p50≈0.72ms；仍缺 `repeated-episode-campus-startup`。忌口转述、别名与可召回人物投影的既有修复归 `HANDOFF.md`，不重复开发。
- 先定产品语义：review 确认证据/claim 是否同时提升 person/episode/knowledge，跨 session episode 如何合并及撤销；现有逐事件 document 不满足同 episode 含两条 source_event_ids 的判据，不能靠放宽判据抬分。汇总与未来采访式回忆录复用 EvidenceEvent/claim/source 引用，区分用户原话、已确认事实和 AI 推断；未确认内容不进入 confirmed-only 召回。
- 已有真实 PG 人物投影/状态门契约，仍需真实 Qwen 抽取器（非规则）对别名句式的证据，以及固定集外未见改写集。会话记忆按当前 person/subject 键迁入既有 `services/memory_scope`，覆盖切人、撤销/删除和旧缓存，不建平行存储。
- 完成条件：遗漏项命中正确证据、原长程召回不退步、隔离/候选泄漏为 0；固定与未见集分别报 recall/nDCG，验证实际 ResponsePlannerClient 超时/catalog 限额。设备追问另取 Actual Heard；未消费 MemoryContextClient 的 0.3s 不是现网保证。
- 入口：`scripts/evaluate_memory.py`、`services/archive/{recall_planner,memory_extractor,postgres_memory_catalog}.py`、`evaluation/memory_eval_zh_v1.json`；既有诊断 `outputs/acceptance/run-20260915-p0-04-student-safety-loop/memory-eval/P1-06-diagnosis.md`。

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
- 本轮测试退出出现 `interaction-delegation-start` pending task：先区分 fixture 未关闭与运行态 shutdown 未等待，用可取消/可 await 的最小回归定位，不能先判为生产泄漏。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行，不伪造 Actual Heard/commit；预定义长稳窗口并保留内存/连接/延迟趋势。先补测量/注入，不顺手改断线产品行为；假设备不能代替 P1-07。
- SiphonAI 只借鉴协议公开化、conformance harness、20 ms 热路径和掉线保会话的工程做法；不引入 `siphon-rs`/`forge-media`，不替换 Go Media Edge，不把 PSTN 入线带入当前范围。

### [ ] P2-05 补齐唤醒计数与测试可复现性，再采家庭噪声矩阵

- 092dcf4 已加入待机/detector-on 门、去重、半开窗口与错误 invalid 分类，但本轮发现下列缺口；不能继续写“工具全修、只剩采数”。原始 `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/{receipt.json,analysis.json,console.log}` 不改写，本轮未重采设备。
- **已修（本轮追补，已提交），P2：分子/收据口径一次关干净。** `_wake_lines` 下界换 `window_start`（settle 期 wake 不计入，report-wall 单测锁定分母≈1.0），收据 `Window` 新增 `exposure_started_monotonic/iso`；`started_monotonic` 保持门前调用时刻（收据 wall 语义不 breaking），report wall 改用 exposure 起点算。旧收据无新字段时回落到原 wall 口径。
- **已修（`f2a95d6`，本轮收尾为严格自包含），P2：测试自包含 + CI 显式采集。** 去重用例内联 3 行最小 fixture（detector/wake event/lagging duplicate），不再读 ignored receipt（`RECEIPT_CONSOLE` 与对账分支已删）；CI `python` job 新增 wake 显式步骤（默认 pytest 仍不收集 `scripts/`）。本轮未重采设备。
- 原设备证据边界：gain 1.0 为 4/8、gain 0.3 为 8/8，共 12/16；gain 不是测得距离。四次失败均在已进入 idle 后，单次冷启动及先高后低顺序不足证明固定 45–50s 预热；不能只取后 12/12。60s 默认等待仅实验参数，多次冷启动/随机或交错增益，对冷启动与预定义稳态分报。成功刺激起点→唤醒行中位 1.445s 含前导静音与采集时序，不当精确声学延迟。
- 原误唤醒修正保留：带 `(state: N)` 首次行去重后新事件 0，TV=1 是上一试次滞后重复；但 TV 133.038s 内 idle=0、约 92.382s speaking/KWS off，small_talk 124.471s 内 idle≈100.909s，quiet 300.001s 均为日志可见 idle。故不能宣称电视/多人各 2 分钟有效零误唤醒；配置日志不自动证明整个窗口 detector 持续启用。
- 工具验收：音量/静音、录音和串口错误都在 finally 恢复，恢复结果实读；真人 Markdown/JSON 口径一致，不从人工提示时刻算真实开口延迟。记录实际输入电平、warmup 配置/实际等待、状态与 detector 证据，错误不算 miss 或零误唤醒。
- 设备矩阵：固定候选/固件/settings 后采真实电视/家庭音源、物理距离/角度和真人对照；保留真值、原始/有效曝光、召回/误唤醒/时延。旧日志只读到 app 2.4.2、ELF 前缀 da6ebdd16，flash receipt 不替本轮回读；日志 detector=MultiNet，先核模型与阈值权威，不笼统调 WakeNet。
- 完成条件：自包含回归与 CI 收集先过，再用预定义协议取得可比较的设备 receipt；有数据才调检测阈值/词形。不改 `advertised_duplex_level=none` 或 aec_reference_verified。

### [ ] P2-06 用可回放任务评测陪伴连续性与回顾质量

- 来自 R-20260917-01 的本项目推导，不是竞品已证实效果；依赖 P0-03/P0-04 基线，复用 P1-03 的人设连续性、P1-05 的可读回顾及 P1-06 的证据记忆。当前只增强学生线，不新增康养/移动控制产品。
- 先给既有 offline harness 增加小型场景集：学习挫败后的情绪承接与自主解题引导、隔次继续上次计划、本人/获授权管理人查看范围内回顾；交叉 under_14/14_17 与 retention 允许/拒绝，至少有切人、撤销、模型超时负例。多轮任务走真实主体/同意门，不绕门靠固定 profile 抬分。
- 逐场景记录任务是否接续、引用是否支持记忆断言、无依据回忆/越权读取次数、回顾可读性和失败降级；延迟与 Actual Heard 复用现有量表，离线生成不计真机成功。保留匿名输入、期待行为、失败样例，固定集与未见集分报，人工盲评关怀感与帮助性，不先承诺改善百分比。
- 完成条件：形成可重复 baseline 与一次同条件对照，主体/撤销隔离和编造事实零回归，汇总能追溯到话轮/证据；观察到收益再扩大场景。访谈式人生故事只复用证据模型留给后期，不新开老人 UI、医疗判断或主动通知外发。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：用户 2026-09-14 暂缓；真实家庭服务/数据、正式发布或价值量级增长前重评。现存本地还原点保留，WAL 增长仍按 P1-08 处理。
- 家长通知发送 worker/外部渠道：仅保留 outbox/readback 验收，不宣称通知已送达；启用需另定授权和渠道。
- EOU 新模型、DuplexModel、expressive/抢跑：现有语音基线与收益假设明确后再评估；TurnPhase 仍 shadow，不新增控制权威。
- ESP-IDF/ESP-SR/upstream 整体升级：有现板修复或安全依据才立项，仍须固定 upstream、clean overlay build 和身份区保护。
- 老年人故事册/人物复刻、年轻人潮玩、最终外形仍是后续阶段，不改变 ESP-VoCat SKU/价带。外部研究的已评估结论只维护在本清单，不恢复独立研究收件箱或历史旧板。

## 研究转化与重评条件：R-20260917-01

- 结论（2026-09-17）：诺恩智伴保留为老年后期市场雷达；其对当前项目的启发是验证可完成的陪伴任务、可查回顾和证据记忆，而非堆叠康养功能。上述建议已落到 P1-05/P1-06/P2-06；不把竞品的营销报道直接当产品需求或性能证据。
- 来源：[新浪财经转载中关村在线，2026-09-02](https://finance.sina.com.cn/roll/2026-09-02/doc-iniqmmyy2899674.shtml)、[天极网，2026-09-02](https://news.yesky.com/hotnews/140/370140.shtml)，本轮已读取。前者报道语音陪护、健康监测/预警与移动取物，后者还提及采访式聊天生成人生回忆录及教育产品三端互通；这些均是报道中的产品宣称。尚无本轮独立验证的实际交付、效果评测、公开零售价或央视原始报道；下订/签约不等于家庭长期可用，不用于改价带。
- 取舍：当前不做移动/抓取、疾病风险筛查、健康监测、用药建议、学校 SaaS 或老人照护工作流；家长读回仍须按现有角色/同意控制，不借“三端互通”放开孩子全部内容。ASR 继续云服务商 FunASR，救援后端按 P1-02 单独验证，不跟本地 FunASR PyPI。
- 重评触发：学生功能与安全链闭环、有明确老人试点需求和用户授权，且获得一手规格/演示、真实交付/维护成本与隐私证据后再评估老年阶段。价格未知时明确未知，不拿签约金额倒推；Doova/HUA-H/安安等历史雷达不恢复成当前开发工单。

## 研究转化与重评条件：R-20260918-01/02（2026-09-18 已评估，不恢复收件箱）

- R-20260918-01（启元 Q1/T1 9/20 发布会，market 雷达）：上纬新材旗下启元 9/20 上海主会场+多城分会场，主推可走动具身 Q1/T1；8/23 预订/9 月发货口径与 9/17 多家确认发布节点。结论：反定位“可走动具身个人机器人/家庭陪伴+科教潮玩”vs 桌面语音终端+长期档案+学生优先；不抬 `advertised_duplex_level`，不改 VoCat SKU/半双工天花板与 P0 安全/TTS 优先级。来源：[每日经济新闻 2026-09-17](https://www.nbd.com.cn/articles/2026-09-17/4583885.html)、[观点网 2026-09-17](https://www.guandian.cn/m/show/603342)、[新浪财经转载上观 2026-08-23](https://finance.sina.com.cn/jjxw/2026-08-23/doc-iniphwys2463738.shtml)。
- R-20260918-02（风峦桌面伴学，market 雷达）：风峦数千万天使+轮，研发约 ¥6000 轮足桌面伴学（语音为主、无屏、摄像头监测坐姿/注意力/情绪，对标学习机；11 月前样机，幻课 APP 宣称近 30 万用户、首季 1 万台目标；创始人称分项可撤回授权、行为感知本地算、原影像不出户）。结论：学生线直接市场参照，强化 P0 同意门/本地边界对照；不改轮足/强制摄像头，不写入路线图。来源：[36氪 2026-09-09](https://eu.36kr.com/zh/p/3975552848589056)、[RFID 世界网 2026-09-10](https://www.rfidworld.com.cn/news/2609_3888B8A427918127.html)。
- 取舍：两条均为发布会营销/融资+样机规划口径，无独立验证的交付/效果/零售价/长期可用证据；不改价带，不新增移动抓取/健康监测/用药/学校 SaaS/老人照护流；家长读回仍按角色/同意门控。
- 收件箱 `RESEARCH.md` 按三文档规则移除，结论只留本清单；后续扫描重建仍须评估后移除，不常驻。
