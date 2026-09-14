# Memoria 优先级执行清单

更新时间：2026-09-14。用途：回答「下一步做什么」，并作为后续唯一的优先级执行队列。研究依据归 `RESEARCH.md`；运行版本、运维步骤、回滚和现场证据归 `HANDOFF.md`，不在这里复制第二份运行台账。

## 结论与执行方式

先让当前语音链路可稳定验收、运行状态可信、数据可恢复，再升级 LiveKit；产品增量优先完成「指定使用人 → 对应人格/音色 → 机器人实际生效」和学生安全闭环。暂不换 ASR/记忆架构，也不把云侧 DuplexModel 当成设备全双工能力。

- 按 P0 → P1 → P2、同级从上到下推进；每次先做最高优先级中未阻塞的一小项。设备/生产窗口受阻时，可并行做 P1-01 的本地兼容性验证、P1-06 的离线召回，不必等全部硬件矩阵通过才写代码。
- `[ ]` 表示未完成；进行中或阻塞写在条目下，注明已完成层级、剩余条件和下一动作。只有本项所有完成条件满足才改 `[x]`，不能把 `code / wired / enabled / verified` 混为一谈。
- 完成时附日期、提交与测试/证据位置；发布和设备事实先更新 `HANDOFF.md`，研究结论同步 `RESEARCH.md`。已完成条目可直接删去，但有效运行证据和未完成子项不能随之丢失；稳定 ID 不复用。
- 本次仅完成分析与文档，不执行升级、安装、刷机、生产切流或放宽权限。清单不是持续运行的授权；后续每次任务仍核对用户授权、脏工作树和真实有效配置。
- 依赖版本是 2026-09-14 的核验结果，实施前重新查官方发布与兼容约束；不按版本号大小批量追新。

## 已知基线：不要重复开发，也不要外推验收

- 当前源码检查点为 `c48fee3`；本轮初始工作树干净。9 月 14 日 15:11 的既有真机证据确认唤醒问候不掉线、天气 ACK/正文可听、播后告别回 idle。该回归的 **14/14 不是 T1–T14**；`direct_real_device_verified`、`full_duplex_verified` 仍为 false。
- `HANDOFF.md` 最近一次 readiness 观测是 **2026-09-13 23:40 CST**：core 12/12、Agent ready，但 smoke 证据过期使 readiness 返回 503。本轮公网探测 TLS 失败、未取得 HTTP 响应，不能据此确认它现在仍为 503，也不能推断服务宕机。
- 最近设备签名策略 `allowed_barge_in=["button","keyword"]` 未放行 voice。当前固件抑制未授权的播放期 `vad.start` 是修复，不是要删除的障碍；语音打断另走 P1-07。
- person→persona 分配、不可变自定义人格、persona→voice 归属、subject-aware RuntimeProfile 和失效通知均已有代码；音频样本真实校验、训练进度与 2 秒轮询也已有代码。剩余以线上版本映射、入口和端到端验收为主。
- RecallPlanner 已有封闭 query rewrite，现有 VAD 期预取与事实/人格分流也已接线。9 月 14 日本地重跑 16 例：`recall@5=0.8125`、`nDCG@10≈0.734`，跨会话/转述追问/安慰三项均为 1.0；不能再列成从零建设记忆系统。

## 组件决策（当前值来自锁文件，上游值来自官方包元数据）

| 组件 | 当前 → 候选 | 本轮判断 |
| --- | --- | --- |
| `livekit-agents`、`livekit-plugins-openai`、`livekit-plugins-silero` | 三者 `1.6.10` → 三者 `1.8.1`（9 月 10 日发布） | P1-01，同一依赖变更评估；读完中间版本迁移说明，不需要先部署 1.8.0 |
| RTC `livekit` / `livekit-api` | `1.1.14 / 1.2.0` → `1.1.18 / 1.2.1` | 随上一项更新锁；Agents 1.8.1 精确要求 RTC 1.1.18，API 1.2.1 还要求 `livekit-protocol>=1.1.25`，不能保留现锁 1.1.22 |
| 实时 ASR `fun-asr-realtime` | DashScope WebSocket；本仓没有 `funasr` 包 | 不存在可直接改的本仓 FunASR pip 钉档；先分账实际空转写故障 |
| SenseVoice 救援 sidecar | 线上包版本未知；仓内脚本用 `sherpa_onnx`，构建文件未入仓 | P1-02 先确定真实实现与可复现构建；上游 FunASR 1.4.15 不等于该镜像版本 |
| `livekit-plugins-voicemem` | 未引入；上游 0.2.2 要求 `livekit-agents<1.8` | 不安装、不换 PG/MinIO 档案栈；只借鉴检索方法 |
| ESP-IDF / ESP-SR / xiaozhi upstream | `6.0.2 / 2.4.7 / v2.4.2`（固定 commit） | 暂不升级；ESP-SR 锁须与 upstream 一致，不能单独抬版本 |
| LiveKit server / Go / Python / Node | server `1.13.5`；其余按各自 manifest | 不与 Agents 联动升主版本；出现明确安全公告、兼容阻塞或可测收益时再建项 |

官方复核入口：[Agents](https://pypi.org/pypi/livekit-agents/json)、[OpenAI 插件](https://pypi.org/pypi/livekit-plugins-openai/json)、[Silero 插件](https://pypi.org/pypi/livekit-plugins-silero/json)、[RTC](https://pypi.org/pypi/livekit/json)、[API](https://pypi.org/pypi/livekit-api/json)、[FunASR](https://pypi.org/pypi/funasr/json)、[VoiceMem 插件](https://pypi.org/pypi/livekit-plugins-voicemem/json)。迁移依据：[1.8.0](https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0)、[1.8.1](https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.1)、[AGC #7064](https://github.com/livekit/agents/pull/7064)、[OTel/PII #7104](https://github.com/livekit/agents/pull/7104)。

## P0：当前链路与发布前提

### [ ] P0-01 重新核准运行基线，恢复 readiness 的真实证据刷新

- 进度（2026-09-14）：readiness 假阴性已定位并修复——`refresh_readiness.sh` 改用运行容器的 stack tag，并复用 live 容器的 Compose 文件集且断言 smoke 镜像等于容器镜像；已部署（脚本 SHA `c0152d5b…`，改前副本 `.bak-20260914-pre-tag-fix`）并实跑通过，loopback 与公网（8443）均 ready、core 12/12。**剩余子项只有一条**：定时器已重排到 2026-09-15 04:35:21 CST，需再观察一次“定时触发”成功，不能用手动 `systemctl start` 代替。
- 原因：过期 smoke 使发布门状态失真；仓内新代码不等于当前 overlay 已部署，尤其 Control 与小程序。
- 工作：只读取实际 image/source/override/有效 env，按组件对应 `code / wired / enabled / verified`；区分 Agent/Bridge、Control、sidecar、固件、小程序版本。核销 HANDOFF 遗留的旧阻塞和旧 epoch，播中/播后告别分开记。
- 在后续获准维护生产时，检查已有 `memoria-readiness-refresh.timer/service` 的启用、最近执行、凭据和失败日志。核实 `refresh_readiness.sh` 是否使用当前组件 override、当前真实 release tag，而不是只用基础 compose/目录名；这是待查风险，不是已证明根因。
- 完成条件：当前 release 的真实 LiveKit/provider smokes 成功后生成证据，回环与公网 readiness 均 ready；core 12 个具名检查均 ready、Agent heartbeat/tag 匹配；至少再观察一次既有 12h 定时刷新成功。只手动刷绿一次不能关闭本项。不得延长 TTL、跳过 smoke 或伪造证据。
- 入口：`scripts/refresh_readiness.sh`、`infra/memoria-readiness-refresh.*`、`services/control_api/app/routes/readiness.py`；定向验证 `test_readiness.py`、`test_mark_readiness.py`。

### [x] P0-02 备份现状核查与隔离恢复验证（启用备份按用户决定暂缓）

- 本项关闭的是**核查与演练**两部分。**启用自动备份不在本阶段范围**——用户 2026-09-14 决定：项目验证阶段暂不启用自动备份与异地副本；未完成子项已移入「暂不排入执行队列」，不计入本项完成。
- 核查结论（2026-09-14）：自动备份**从来没在跑**。三个阻塞都已复现——`offsite-backup` profile 从未启动（`/etc/memoria-offsite-backup.env` 与 `postgres_backup_staging` 都不存在）；`postgres-base-backup.sh` 的 `pg_basebackup --dbname=postgres` 在 PG 17.8 客户端直接报 `missing "=" after "postgres"`（已在仓库修掉并加回归断言）；`pg_hba.conf` 只对本机放行复制连接，独立备份容器走 `memoria_default` 网桥被拒（`no pg_hba.conf entry for replication connection from host 172.19.0.14`）。因此 8/27 起累计的 1503 段/23.5GB WAL 此前无 base backup 可配对、单独不可恢复。
- 演练（隔离，未改生产配置、未覆盖生产数据）：base backup 2s/69MB → `pg_verifybackup` pass → `--network none` 隔离容器 5s 起库 → 应用表与 2305 条证据事件可读 → MinIO 音频对象 key 与密文 SHA256 一致 → WAL 0 缺口且末段等于备份 `START WAL LOCATION`。
- 保留资产：`/var/backups/memoria/drill-20260914-p0-02/base`（校验通过）、`report.json`；9/12 手工 dump 的 `sha256sum -c` 仍 OK、`pg_restore --list` 1210 项。证据 `outputs/acceptance/run-20260914-p0-02-restore-drill/`，细节与三条启用路径见 `HANDOFF.md`。
- 未做：PITR replay 演练；异地副本（零）。

### [ ] P0-03 收口当前语音缺陷，建立升级前对照基线（真机时段已就绪待约）

- 就绪状态（2026-09-14 17:00 CST）：板子在线（WiFi 192.168.8.142、Activation v3、唤醒词茉莉、idle），串口空闲，捕获工具需 `uv run --no-project --with pyserial --with esptool`（项目 `.venv` 无 pyserial/esptool），服务端 readiness ready。会话计划与 11 个场景、判据、分段安排见 `outputs/acceptance/run-20260914-p0-03-voice/session-plan.md`；逐轮原始数字由 `scripts/voice_session_report.py` 产出。**操作要点**：打开串口会让板子重启，每段捕获开头约 10 秒启动，须等 `activating -> idle` 再说话。设备端固定安全话术（P0-04 场景 10）与屏幕照片（场景 11）借用同一时段。
- 进度（2026-09-14）：**本地对照基线已建立**——`test_media_session.py` 200 passed、`test_duplex_runtime_wiring.py` + `test_utterance_router.py` 191 passed（证据 `outputs/acceptance/run-20260914-p0-03-local-baseline/`）。ACK 后空输入恢复、旧查询不回流、同问重复不重复提示、空转写处理均有既有回归并通过。
- 剩余阻塞：真机时序与听感需要操作员在场（说完到开口、ACK→正文纯静音间隙、慢查询第二提示、长天气 >45s、终端播放回执），板卡在线但本轮未做声学/听感验收；上一轮实测 ACK→正文为 0.366s/1.766s，第二轮仍超 1.5s。本地全绿不能替代真机证据。
- 原因：当前已部署修复仍缺空输入恢复的专项真机时序，且 9 月 13 日一轮 ACK→正文纯静音为 1.766s，未过现有 1.5s 标准。
- 工作：先复现「ACK 后空输入 → 有效正文恢复」、同问重复提交、答案完成后再问同题、查询南京时改问北京、慢查询第二提示；只改既有 Router/Ledger/fence 接缝。记录提问结束、ACK 起止、正文首帧、终端播放回执，不能只统计服务器首 token。
- 完成条件：有效答案不静默丢失、ACK 不重复/不截成残句、旧查询结果不回流；说完到开口及提示间隙无 >1.5s 纯静音；慢查询按现有约 2.5s 机制给第二提示；长天气 >45s 与长回复可完整听完。预先固定场景/重复次数，逐轮给出原始数据，不只报均值。
- 真机复测同时保留已通过的问候不掉线、播后告别、BOOT/触摸硬停；补待机与五表情照片。播放期语音告别不混进本项，移至 P1-07。
- 入口：`HANDOFF.md`「下一验收」「20260913-empty-input-resume 复测清单」；`test_media_session.py`、`test_duplex_runtime_wiring.py`、`test_utterance_router.py`。没有在线板卡就只完成本地复现，设备项保持未完成。

### [ ] P0-04 核实身份隔离，补齐学生安全闭环（不含家长通知发送链路）

- 进度（2026-09-14）：身份/同意门的**本地合同与 RLS 证据已取得**——真 PG（`memoria-pgv`）跑 guardian schema/语料同意栅栏、guardian 表按 guardian/minor 作用域隔离、tutor 行 subject 隔离且 RLS 生效、账号能力门、生产同意接线、多主体权限矩阵、声纹权威合同，共 **34 passed**（证据 `outputs/acceptance/run-20260914-p0-04-identity-matrix/`）。这只到合同/API 级，不等于真机或真实账号端到端。
- 范围决定（2026-09-14，用户）：**本阶段不做微信订阅号/家长通知发送链路**（验证阶段）。因此本项不含发送 worker、重试、模板与凭据申请；通知侧只验「入队 + 家长端列表可读 + 明确记账当前无推送通道」，不把入队当作家长已收到。重新评估触发条件：开始对真实家庭或学生提供服务前。
- 剩余待验：学生危机的**设备端固定话术**（借用 P0-03 真机时段的场景 10）与受控学生账号下的能力门端到端；guest/uncertain 不进主人私有链、撤销立即生效等已由合同/RLS 覆盖，但真实账号与真机路径仍需一次演练。
- 原因：账号能力门、声纹与固定危机话术已有实现；家长通知目前只有 outbox 入队与列表读取，尚无发送 worker/投递状态更新，不能把入队当作家长已收到。学生阶段仍缺实际端到端闭环。
- 工作：用受控测试账号/脚本覆盖 adult、minor、unknown/未声明类别，owner、guest、uncertain，以及 guardian 同意/撤销；核查历史、私人记忆、工具、自定义音色、删除/导出、切主体和告别的权限。模拟学生危机场景，不要求真实未成年人参与高风险试验。
- 本阶段实现边界：不新增发送消费者、幂等重试与模板凭据；只在文档与状态里显式记录「outbox 有入队、无投递通道」，避免被误读为家长已收到。
- 完成条件：无同意拒绝、撤销立即生效；guest/uncertain 不进入主人私有链；切使用人或邀请家人不提升 owner 身份；**真实设备固定安全话术通过**，且 outbox 入队与家长端列表可读、无投递通道这一边界被显式记账（不要求微信回执）。涉及 schema/RLS/授权修改时带 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响域，跳过不算通过。
- 入口：`services/control_api/app/account_gate.py`、`routes/guardian.py`、`services/guardian/crisis.py`、`services/agent/src/generation_output_policy.py`、`services/speaker/authority.py` 及对应测试。技术验收不代替法律合规结论。

## P1：受控升级与第一阶段产品闭环

### [ ] P1-01 LiveKit 1.8.1 同组升级：先兼容性，再独立发布

- 价值：跟进连接池、会话关闭、播放计量、资源清理和 telemetry 修复；不是已证明能解决当前 ASR/AEC 故障。对应研究 R-20260907-01（吸收 R-20260911-01）。
- [ ] 本地候选：三件套同步升至 1.8.1，RTC/API 与 protocol 按组件表更新，`livekit-local-inference` 从 0.2.6 更新到满足 `>=0.2.7`；按真实约束重锁 `uv.lock`，保留 Python 3.12。禁止 `--no-deps` 绕过冲突，不混入模型、固件或 server 升级。
- [ ] 行为兼容：针对真实 SDK 验证 `TurnHandlingOptions`、STT/TTS adapter、RoomIO 与半内部符号；`TypedDict` 会静默接收未知键，必须验证配置被实际消费，不能只测构造不抛异常。LiveKit 侧保留 dispatch metadata→`controlled_half_duplex_session` 的策略，设备 Voice Core 侧保留 `audio_mode` 派生策略；兼容半双工路径禁打断，默认 preemptive 关闭，完整 generation fence 不变。
- [ ] 隐私/可观测性门：迁移 #7104 的 message event→attributes、span 与 token 字段；显式关闭内容采集 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0`，禁 PII 外发 `LIVEKIT_TELEMETRY_ALLOW_PII=0`（或等价 API）。这是候选 SDK 支持的配置，不是本仓已经接线；须核导入/初始化时序与部署 env，兼顾 SDK 和本仓 `observability/media_otel.py` 的导出路径。用假姓名/私有文本/tool 参数作 canary，检查实际 exporter、日志和报表，证明不泄漏且计量不重复，不能仅凭 env 存在判绿。上游默认采集内容、默认允许 PII，不能假设「新增 redaction」会自动保护本项目。
- [ ] 完整构建与回归：`uv lock --check`、frozen 安装、ruff/module budget/strict mypy、Agent 单测/集成、Control/API 与 Bridge 使用面、offline E2E；所有消费共享锁的镜像分别验证，不只测开发 venv。Agent/Bridge 必须同一镜像；依赖锁变化不走 agent-only source overlay 快速通道。
- [ ] 候选上线与回滚验收：依赖 P0-01/02 的发布前提和 P0-03/04 的相关基线；获准后最小切片发布，真实 provider smoke、当前设备对照与延迟复核通过，留当前+一个可运行 rollback。未获生产/设备窗口时只标本地子项完成。
- 禁止搭车：不启用 DuplexModel、`expressive=True`、`user_turn_limit`、TurnPhase 生产副作用；本仓显式 `auto_gain_control=True` 且未配 NC，#7064 默认值变化不直接改变现链路，也不授权改声学增益。入口：`pyproject.toml`、`uv.lock`、`session_entrypoint.py`、`infra/Dockerfile.*`。

### [ ] P1-02 ASR 分清消费方，先让救援 sidecar 可复现

- 依赖：P0-01 的运行清单；对应 R-20260831-02、R-20260910-01。不能将 DashScope 的模型服务版本当成 FunASR Python 包版本。
- 工作：只读确认生产 sidecar 的启动脚本、Dockerfile、包清单、模型/词表摘要、基础镜像、CPU/GPU 与输入语言；核对是否真的使用仓内 `scripts/run_sensevoice_asr.py`。将实际构建来源与精确依赖纳入仓库，保留独立镜像，不向主 Agent 添加无用 `funasr`。
- 条件分支：若生产确实使用 FunASR 且低于 1.4.15，再做同数据集旧/新镜像 A/B；若使用 sherpa-onnx，则核其自身版本与修复，关闭「升 FunASR 能修此 sidecar」的错误假设。上游 NumPy 2 测试不代表所有 Torch/模型组合兼容，也不要求主环境改 NumPy。
- 完成条件：从仓内输入可重建候选；真实 PCM 契约、中文短句、长段/尾字、静音/低 RMS/削波、并发与失败降级均验证；分别报告空转写 vendor_error/vendor_silent/gating/low_rms、救援耗时及 2.5s 超时行为。无回归且有实际收益才另行切流；没有相关升级可记「不适用」并关闭版本子项。
- 入口：`funasr_stt.py`、`funasr_empty_accounting.py`、`providers/sensevoice.py`、`scripts/run_sensevoice_asr.py`、`test_funasr_protocol.py`、`test_funasr_rescue.py`。不改设备 VAD 去掩盖 ASR 问题。

### [ ] P1-03 按使用人切人格/音色：补控制入口与真实切换

- 依赖：P0-01/02/04；对应 R-20260911-05。已有分配/自定义人格/音色归属后端不重建。
- 工作：核对线上 Control 是否包含当前相关路由/服务/schema，而非仅有失效通知 overlay；小程序补齐已承诺的「设备与成员」、按人分配/取消分配与现有邀请入口，接已有 API。以 app_confirm 的手动指定作为首个闭环。
- 修正 `allowed_confirmation_methods` 宣称 `voice_question`、写接口却只收 `app_confirm` 的不一致：当前先不广告未实现能力；可信设备选人/自动认人另走 P2-02，不用客户端 flag 充当身份证据。
- 完成条件：同一 binding 两个 subject 可得到不同人格/音色；在线切换触发版本推进、签名失效通知、next_safe_point 重协商，旧上下文/音频不串入新主体；取消分配回落 binding 默认。内置只读、自定义创建即冻结、克隆未就绪回落设计音色均过真 PG 与设备实听。
- 入口：`routes/persona_assignment.py`、`routes/custom_personas.py`、`routes/multi_subject.py`、`services/session_runtime/profile_service.py`、`services/voice_profile/postgres_schema.sql`、`apps/miniprogram/pages/device/`。人格/音色由服务端生效，不能因固件不持有 persona 文本就再造设备人格引擎。

### [ ] P1-04 自定义声音：从样本上传到设备出声完成验收

- 依赖：P0-04、P1-03；对应 R-20260911-07。真实音频校验、进度轮询、over_budget 文案已有实现，先查线上是否部署。
- 工作：覆盖 profile 页有界录音→上传→真实解码校验→训练→ready/failed/超时→人格分配→设备实听，连同拒绝授权、取消、重复提交、撤销与样本处置。
- 完成条件：校验失败不能启动训练；没有假 ready/假百分比；实测端到端耗时，60s 超预算展示事实、120s provider 超时正确收尾。达不到一分钟就修预期/文案或优化瓶颈，不降低校验标准；未 ready 仍回落设计音色。
- 入口：`apps/miniprogram/pages/profile/`、`services/voice_profile/`、`services/control_api/app/routes/voice.py`。小程序录音仅限已有自定义音色样本，不恢复手机声纹登记、实时对话、媒体 WSS 或实时 TTS。

### [ ] P1-05 小程序补权威会话态，再做三端验收

- 只读态开发可独立进行，集成验收使用 P1-03/04 的候选；对应 R-20260906-01。设备已有 idle/listening/speaking，小程序不能把连接在线猜成正在听或空闲。
- 工作：以 Python→Edge 的 `assistant_state.phase`（已有 fence、Edge 内存态镜像）为源，补 Edge→Control 的受鉴权只读状态出口，再由小程序消费；这条读端目前缺失，不只是接已有字段。投影携带 session/generation fence 与新鲜度，重连、断线、过期显示 offline/unknown；不从字幕、diagnostics 零散字段或客户端计时猜状态，不新增话轮控制面。
- 完成条件：微信手机/电脑/开发工具使用同一可识别候选包，覆盖登录/绑定、在线状态、三态与断线、主体/人格切换、样本进度、回顾、权限拒绝和刷新；录音范围门禁仍过。当前只上传过 0.8.84 开发版，不能称新源码已发布；体验版/提审/正式发布需分别授权和记录。
- 入口：`services/media_edge/bridge_runtime_events.go`、`services/media_edge/session_shadow.go`、`routes/device_control.py`、`apps/miniprogram/`、`apps/miniprogram/tests/no-realtime-media-gate.test.js`。

### [ ] P1-06 先修可复现的召回遗漏与人物抽取

- 可本地并行；对应 R-20260909-03。基线遗漏是 `paraphrase-food-preference`、`person-alias-mother`、`repeated-episode-campus-startup`，分别对应忌口转述、人名别名、重复旧事关联。
- 工作：固定现有 16 例并补未见改写/反例；先从 RecallPlanner 封闭词表、已确认别名和路由修起。人物抽取另设回归：「我儿子小周对猫毛过敏」「阿梅是我妈妈」，区分 person 与属性 claim，沿现有 write policy 进入 candidate→confirmed，不能直接成为主人事实。
- 完成条件：三个已知遗漏命中正确证据；原长程三项不退步，隔离/候选泄漏保持 0；补充集单独报 recall/nDCG，避免靠改答案或硬编码测例抬分。沿现有 ResponsePlannerClient 的实际超时与 catalog 条数限制验证空降级、延迟不退步；未消费 MemoryContextClient 的 0.3s 不是现网保证，未确认/guest 数据不进 prompt。真机追问旧事另补 Actual Heard。
- 入口：`scripts/evaluate_memory.py`、`services/archive/evaluation/memory_eval_zh_v1.json`、`services/archive/recall_planner.py`、`services/archive/memory_extractor.py` 及 catalog/抽取测试。不引入 VoiceMem/Mem0/Qdrant。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 阻塞条件：真实 VoCat、连续双端采集与操作员；涉及签名放行 voice 必须另获明确授权。依赖 P0-03/04；对应 R-20260907-02、R-20260831-01。
- 工作：先检查 Exact DAC reference/通道映射、pre/post AEC 与 residual、近端人声保留和双讲，再在获准的测试策略下验证打断；保留当前稳定固件与回滚。不得绕过签名策略、开启播放期 KWS、提高 DTLN 或丢掉采集来假装成功。
- 完成条件：受控测试有当前固件/身份/策略/fence 对应证据，播中告别与普通打断按权限终止旧音频，非主人/回声不越权，BOOT/触摸仍硬停；电视人声/家庭噪声唤醒按距离、样本次数、观察时长记录。真实 T1–T14 逐格填证，未过项保持 blocked/failed。
- 本项可得出「现 SKU 只支持已测范围」的结论；全双工标志只有完整硬件门禁通过才可改。硬件天花板确认后，才考虑 XVF3800 独立选型，不把采购混入实验。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取路径与可观测 token 预算

- 依赖 P1-06。现有 `_build_prefetched_context_snapshot` → `ResponsePlannerClient.prefetch_context` → `/v1/interaction/context-prefetch` 与事实/人格分流已接线；待解决的是未消费的 `MemoryContextClient`、缓存语义与硬 token 计量，不是再造一条预取链。
- 先追踪现有消费者并确定一个权威路径，再决定接入还是删去死代码；缓存必须按 subject/session/fence 隔离并拒绝过期结果，不能在换人后沿用旧私有缓存。补可观测 memory-token 上限，区别 persona 与当轮情绪。
- 完成条件：现有召回不退步、预算超限可裁剪、超时可空降级，热路径延迟有前后对照；没有收益就保留简单路径。入口：`agent.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-02 多成员声纹与不依赖小程序的选人体验

- 声纹增量依赖 P0-04；接入切人格/音色另依赖 P1-03。当前 owner-only 登记不能区分具体家人；先确认明确的识别人范围、单独同意/撤销与设备确认交互，再开 guest/person 声纹独立增量。
- 完成条件：逐人登记、冲突/不确定/访客隔离、撤销和审计通过真实 PG/设备验证；识别只提出使用人候选，敏感授权仍走独立能力门。只有这些前提完成后才评估自动切人格/音色，不让语音自报身份或一个 `profile_id` 升格为 owner/person。
- 入口：`services/speaker/authority.py`、`services/identity/`、`routes/multi_subject.py`。未决定交互前，不擅自复用 BOOT/拍打或改唤醒词。

### [ ] P2-03 可证明删除回执与导出证据链

- 依赖 P0-02/04；对应 R-20260902-04、R-20260901-08、R-20260910-02。先核已有删除/撤销/导出行为，再补缺失的 subject 谱系与可离线核验回执。
- 完成条件：PG、MinIO、派生索引/缓存与音色/声纹的处置范围一致；失败重试幂等，回执可查询验证，导出有 AI/授权标识；如存在依法或按策略保留的备份，明确期限与恢复后再删除机制，不承诺「即时物理抹除所有副本」。
- 入口：现有 Archive/EvidenceEvent、identity/consent、删除/导出路由；不新建平行记忆服务。若 P0-04 发现实际越权或撤销失效，将具体缺陷立即提到 P0，不等本项。

### [ ] P2-04 扩展协议故障注入与长稳观测

- 对应 R-20260907-03；复用现有 Edge/Voice Core 契约测试与 offline harness，先查缺口。
- 覆盖 WSS 丢帧/乱序/重连、Bridge/Agent 退出、未知配置、迟到终端回执、profile 失效、并发与资源泄漏；优先只加测量/故障注入，不顺手改断线提示或重连产品行为。
- 完成条件：旧 generation 无副作用、有效输出有交付或可解释终态、回滚路径可运行，故障期间不伪造 Actual Heard/commit；长稳窗口事前定义并保存内存/连接/延迟趋势。假设备测试不能代替 P1-07 声学验收。

## 暂不排入执行队列

- 自动备份与异地副本：用户 2026-09-14 决定项目验证阶段暂不启用。重新评估触发条件：开始对真实家庭提供服务或写入真实家庭数据、正式发布前、或数据价值/量级显著增长。届时三条启用路径见 `HANDOFF.md`（推荐把备份容器改成 `network_mode: service:postgres` 走 loopback，不改 pg_hba）。**连带问题必须一起看**：WAL 归档仍在写且从不裁剪。2026-09-14 已按授权回收 1502 段 / 23GB 历史积压（根盘 68%→47%，可用 37G→60G），但归档仍以约 1GB/天累积，约 6 周后再次写满；终态需在「只裁剪的定时任务 / 停用 `archive_mode`（要重启 PG）/ 手工定期回收」中选一个。
- EOU 新模型、DuplexModel、抢跑与 expressive：等现有语音质量基线和明确收益假设；若实验只做 shadow，不另建控制权威。
- ESP-IDF/ESP-SR/upstream 整体升级：需明确现板修复或安全依据，届时按固定 upstream 重放 overlay、clean build、host 测试和保护身份区的真机门禁另立项。
- 老年人生故事册/人物复刻、年轻人潮玩、机器人最终外形：保留阶段方向，当前学生线未闭环前不扩张。市场新闻继续留 `RESEARCH.md`，不自动变成工程任务。

## 已完成（可在后续清理）

- [x] DOC-01 建立唯一优先级清单，核对锁文件/当前代码与官方升级约束，纠正研究中重复/过时的开发状态；同步五份长期文档边界与预算检查（2026-09-14；本次文档 diff）。这不代表上面的升级或现场验收已完成。
