# Memoria 优先级执行清单

更新于 2026-09-16，审查基线 `a8a0e43`。本文件只保留未完成事项；完成后把仍有效的运行结论归入 `HANDOFF.md`，并删除任务/子项，不积累完成记录或另建归档。已有编号不复用；生产、设备、回滚与原始证据以 `HANDOFF.md` 为准。

## 下一步与执行边界

1. 先处理 P0-04 的监护证据与数据主体授权错配，避免将未经真实验证的授权带入新 Control 候选。
2. P0-03 可并行做本地复现/测试：修正 TTS 无效回归测试，解决 G 临近静默续问丢失，再查部分音频失败终态与 B/D 设备停滞；不能只补发最新提交就宣称修复。
3. 发布前完成 P1-01 的标准制品门禁接线和候选绑定；按授权最小组件切流，再用同一候选完成语音/学生安全真机验收。P1-03 的最小手动选人入口先供 P0-04 使用，不等待完整人格功能。
4. WAL 保留策略另列 P1-08，不能随“自动备份暂缓”一起搁置；先测现状，生产删除/重启/启用定时任务须另获授权。

本轮只审查和修改文档，未部署、刷机或改数据。最新 CI `35072578098` 的 agent/python 通过，Edge/小程序/固件/镜像任务跳过；本地定向测试通过不等于下面各项完成。最新已记录 Agent/Bridge 运行 `d96d4c2`、板卡 `d1ad38f`，不能把 HEAD 的 `code` 记成线上 `enabled/verified`。

## P0：发布前必须闭环的安全与语音问题

### [ ] P0-04 修正当前使用人的监护授权与学生安全闭环

- 前提：成人管理账号可创建独立 `under_14/14_17` 使用人，不要求孩子另建登录账号。以当前使用人及合法监护关系决定策略，成人学生不自动视为未成年人；最小 app_confirm/年龄资料入口优先，不重建已有多主体权威。
- 审查发现——关系确认不实：`routes/multi_subject.py::_primary_subject` 替新建孩子调用 `confirm_relationship(person_id=child)`，写出孩子确认时间和 actor=child 的审计；随后 `establish_active_link` 在未校验微信身份的路径标 `verified_via=wechat_identity`，以合成 code hash/365d 到期建立 active link，缺少相应建立/确认事件。SQLite 探针可复现；这不是实际完成双边或微信验证。
- 待修：沿现有 identity/guardian authority 明确“家长声明、已验证监护、有效同意”的证据和权限差异。无孩子账号也必须如实记录 actor/provenance，不代孩子确认、不把声明冒充微信验证或人工审核；建关系、通知、存记忆和会话准入分别按能力门处理，失败不得留下半激活授权。
- 审查发现——数据主体错配：`interaction.py::response_plan` 用 active subject 类别，却以 `account_id` 查 `memory_retention` 并读账号记忆；`session_policy` 又按账号类别判断。已有孩子 consent 不生效，且同一会话可出现 response-plan 不读记忆、policy 却允许 private_memory/history 的矛盾。先明确记忆的真实数据归属，统一类别、consent、读写和 policy；不能只替换一个 ID 而将家长私有记忆交给孩子。
- 审查发现——不可见降级：当前使用人权威读取的 `except Exception: pass` 会无日志回落到账号资料。权威不可用/过期必须可观测且保持保守能力门；公开固定安全回复保留。正常 unknown/guest 不得被猜成孩子或强行通知某位家长，与“读取失败被静默吞掉”分别测试。
- 测试缺口：新增独立孩子用例虽调用多主体 API，仍用 `_attach_signed_runtime_profile` 手工注入签名 profile；补真实 API→session/profile 获取→response-plan/policy 路径。新 guardian 写路径须新增并实际执行 PostgreSQL/RLS 契约；不能用既有 CI PG 绿色替代未覆盖的方法。
- 完成条件：adult 管理账号下 under_14/14_17/adult/unknown 矩阵、换合法管理账号、切人/改年龄与旧缓存/旧 generation 并发、无同意/撤销/无权限均通过；类别、同意、通知与数据作用域一致。零同意可建 HTTP session 不直接等同真实设备越权，须验证实际设备 admission/受限能力门。
- 设备验收：先走正式 app_confirm 确认测试使用人及年龄，核对设备实际取得的签名 profile 与有效监护授权；不手工注入 profile、不把成人代讲当作孩子身份证据、不绕过声纹/准入门。受控成人模拟危机，固定话术逐字交付+终端回执+人工听感；通知 outbox 绑定正确孩子及有效监护授权、重放幂等、家长作用域可读、原文不外泄。通知失败不得吞公开回复。发送 worker/外部投递按用户 2026-09-14 决定暂缓，不把 outbox 称为已送达。
- 入口：`services/control_api/app/routes/{multi_subject,interaction,guardian}.py`、`services/identity/service.py`、`services/guardian/{sqlite_store,postgres_store}.py`、`services/control_api/tests/test_student_safety_loop.py`。

### [ ] P0-03 收口 TTS、续问竞态、设备停滞与真实时延

- 当前证据：2026-09-16 H 同会话天气→续问→播后告别成功，F 的 48.28s 九天天气完整听完；但 B/D 设备停滞、G 续问丢失、H 时延越线，整体稳定性未通过。不沿用跨 release 累计轮数，不以原始笔记中的“约 55s 阈值”或“告别 3/5”作为结论。
- 优先补有效 TTS 回归：新增测试走 `synthesize_stream_text()`，未覆盖被修的 `DoubaoSynthesizeStream._run_attempt`，且 happy mock 只有一个音频分片，父提交也通过。改用实际 `tts.stream()`、多分片 `split_pcm`；可用分片间隔 0.05s、超时 0.08s，使每次间隔未超时但累计挂钟超过初始预算，证明修前失败/修后通过；另测真正停顿与硬期限，不靠注释声称覆盖。
- 举一反三：批式 `_synthesize_once` 仍保留总挂钟且首包后异常分类不一致；当前消费者主要是短控制应答，不能归因为已知线上长播故障。明确批式/流式的首包、无进展、硬期限与重试语义，保持部分音频不得整句重放。
- G 仍未修：按“预算耗尽→迟到 VAD 受理→endpoint”真实顺序，HEAD 与父提交均可能 owner_silence_timeout 关闭。新增 race 测试父提交同样通过；新分支若走到，会将 remaining 置 None，下一次装表给满 10s，而正常 grace 受理保留 0。先统一耗尽/暂停/已验证主人活动的语义，再补真实入口、锁/revision、final/endpoint 乱序、watchdog 交接的有区分度回归；不得靠裸 VAD 刷满预算。
- 部分音频失败终态：B 在旧总挂钟 20.23s 出错，约 38.6s 后才错误收尾。最新流式修复有效但未闭环已发音频后的真实停顿/断连/硬期限；注入这些故障，验证同代 ERROR→cancel/flush→设备退出 speaking、有界清理及迟到结果 fencing。错误不能记 playback_completed，不能等整段排空才体现失败。
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
- 构建验收：从冻结 source/lock 标准完整构建实际受影响的镜像，Agent/Bridge 共用同一产物；依赖变化不能 source overlay。核 fresh process 的 env 未设置/空值/显式 0、入口加载和真实 exporter，无内容/PII 泄漏；不能用只改 Compose 声明或外部测试环境代替候选运行态。
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
- 入口：`funasr_stt.py`、`funasr_empty_accounting.py`、`providers/sensevoice.py`、`scripts/run_sensevoice_asr.py` 及 ASR 契约/救援测试。

### [ ] P1-03 按使用人切人格/音色：补控制入口与真实切换

- 最小“手动确认使用人及年龄”入口先供 P0-04；完整人格/音色验收依赖 P0-04，避免循环依赖。先核线上 Control 路由/schema，不能把 9 月 11 日失效通知 overlay 当成最新全量后端。
- 小程序接已有设备/成员、分配/取消分配/邀请 API，先以 app_confirm 闭环；消除 allowed_confirmation_methods 广告 voice_question、写接口只收 app_confirm 的不一致，不把客户端 flag 当身份证据。
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

- 可本地并行。固定集 16 例基线 recall@5=0.8125（13/16）、nDCG@5≈0.734；遗漏为 `paraphrase-food-preference/person-alias-mother/repeated-episode-campus-startup`，不重复做已完成的根因扫描。
- 待定语义：review 确认证据/claim 是否同时提升其 person/episode/knowledge；跨会话 episode 是否合并。现有逐事件 document 不能满足“同 episode 含两条 source_event_ids”的判据，先定产品写入语义，不能绕过判据抬分。
- 待修：忌口转述与已确认记忆零词面重叠，沿 RecallPlanner 封闭词表扩展；“家里人叫她阿梅”未抽出别名，且 person 仍为 candidate。补人物/属性区分与别名反例，沿既有 candidate→confirmed；不放宽 confirmed-only 隐私门。
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
