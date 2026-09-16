# Memoria 优先级执行清单

更新时间：2026-09-16。用途：回答「下一步做什么」，并作为后续唯一的优先级执行队列。研究依据归 `RESEARCH.md`；运行版本、运维步骤、回滚和现场证据归 `HANDOFF.md`，不在这里复制第二份运行台账。

## 结论与执行方式

LiveKit 1.8.1 已于 2026-09-16 切流；下一步是收口当前版本的播放停滞、TTS 超时终态、续问丢轮和延迟，并补齐发布隐私门。学生危机策略按「设置的当前使用人是学生（未成年）」启用，与登录/购买/管理账号类别无关；复用已有使用人资料与签名 RuntimeProfile。暂不追升版本、不换 ASR/记忆架构，也不把云侧 DuplexModel 当成设备全双工能力。

- 按 P0 → P1 → P2、同级从上到下推进；每次先做最高优先级中未阻塞的一小项。最近顺序：P0-03 固定停滞复现与补证据 → 修有界 TTS/续问生命周期 → 当前版本定向真机回归；P0-04 的使用人策略与 P1-01 的发布门修复可独立并行，不必等设备窗口。下一次发布前必须先修 P1-01 的隐私与制品校验缺口。
- `[ ]` 表示未完成；进行中或阻塞写在条目下，注明已完成层级、剩余条件和下一动作。只有本项所有完成条件满足才改 `[x]`，不能把 `code / wired / enabled / verified` 混为一谈。
- 完成时附日期、提交与测试/证据位置；发布和设备事实先更新 `HANDOFF.md`，研究结论同步 `RESEARCH.md`。已完成条目可直接删去，但有效运行证据和未完成子项不能随之丢失；稳定 ID 不复用。
- 清单最初于 2026-09-14 建立；后续实施按各项的日期与证据更新。清单不是持续运行的授权；每次任务仍核对用户授权、脏工作树和真实有效配置。
- 当前依赖以 2026-09-16 锁文件和发布收据为准；上游版本/安全修复在实施前重新查官方发布与兼容约束，不按版本号大小批量追新。

## 已知基线：不要重复开发，也不要外推验收

- 本轮核对的本地 HEAD 与远端 `main` 均为 `49802f1`；最新发布代码为 `d96d4c2`，两提交 CI 分别为 **35052813364 / 35051332150 success**。`49802f1` 虽以 docs 命名，也新增了发布镜像解析脚本，审查不能只看文档。审查发现的代码缺口归 P0-04/P1-01，未在本轮修代码或部署。
- 现有发布收据：Agent/Bridge `20260916-livekit-181-v1`（源码 `d96d4c2`）于 **2026-09-16 11:36:08 CST** 启动；`1cf8dec` 是紧邻前版，不是当前发布。Edge 仍为 `20260908-1600-vocat-interrupt-assist-edge-component`；板上仍为 `d1ad38f` 对应的 `0025`、ELF `da6ebdd16…`。当前发布/回滚摘要见 `HANDOFF.md`。
- 最新**已记录** readiness 为 **2026-09-16 11:37:57 CST**：回环/公网 ready、core 12/12、Agent heartbeat ready，真实 LiveKit/provider smoke 通过；本轮审查未连接生产刷新该快照。当天真机五段捕获、A–H 八种会话已有当前版本证据：H 三步连跑、F 长播 48.28s 与 E/F/H 告别成功，但 B/D 停滞需硬复位、G 续问丢轮、H 的 ACK→正文 1.969s 越线，整体未通过。9 月 15 日短播放 5/5 与功能 2/3 只属前版；9 月 14 日 **14/14 不是 T1–T14**，`direct_real_device_verified`、`full_duplex_verified` 仍为 false。
- 最近设备签名策略 `allowed_barge_in=["button","keyword"]` 未放行 voice。当前固件抑制未授权的播放期 `vad.start` 是修复，不是要删除的障碍；语音打断另走 P1-07。
- person→persona 分配、不可变自定义人格、persona→voice 归属、subject-aware RuntimeProfile 和失效通知均已有代码；音频样本真实校验、训练进度与 2 秒轮询也已有代码。剩余以线上版本映射、入口和端到端验收为主。
- RecallPlanner 已有封闭 query rewrite，现有 VAD 期预取与事实/人格分流也已接线。9 月 14 日本地重跑 16 例：`recall@5=0.8125`、`nDCG@10≈0.734`，跨会话/转述追问/安慰三项均为 1.0；不能再列成从零建设记忆系统。

## 组件决策（当前值来自锁文件与发布收据）

**2026-09-16 审查更新**：1.8.1 同组依赖已落锁、构建 delta 镜像并切流 Agent/Bridge；标准全量构建、隐私双进程验收与回滚演练未完成。`RESEARCH.md` 同日记录了上游 1.8.2，但其中「仓内仍 1.6.10」已过时；1.8.2 仅保留待评估，不覆盖本次按用户决定发布 1.8.1 的事实，也不自动追加升级。其他上游数值是已有研究快照，实施前复核。

| 组件 | 当前状态 / 已有研究快照 | 本轮判断 |
| --- | --- | --- |
| `livekit-agents`、`livekit-plugins-openai`、`livekit-plugins-silero` | 三者均 `1.8.1`，锁定且 Agent/Bridge 已发布 | P1-01 收口当前发布门与验收；不再列成待升级 1.6.10→1.8.1 |
| RTC `livekit` / `livekit-api` / `livekit-protocol` | `1.1.18 / 1.2.1 / 1.1.26`，同批已发布 | 按同一锁集验证；`livekit-local-inference=0.2.7`，API/protocol 显式约束保留；其他共享锁镜像仍需分别验证 |
| 实时 ASR `fun-asr-realtime` | DashScope WebSocket；本仓没有 `funasr` 包 | 不存在可直接改的本仓 FunASR pip 钉档；先分账实际空转写故障 |
| SenseVoice 救援 sidecar | 线上包版本未知；仓内脚本用 `sherpa_onnx`，构建文件未入仓 | P1-02 先确定真实实现与可复现构建；上游 FunASR 1.4.15 不等于该镜像版本 |
| `livekit-plugins-voicemem` | 未引入；上游 0.2.2 要求 `livekit-agents<1.8` | 不安装、不换 PG/MinIO 档案栈；只借鉴检索方法 |
| ESP-IDF / ESP-SR / xiaozhi upstream | `6.0.2 / 2.4.7 / v2.4.2`（固定 commit） | 暂不升级；ESP-SR 锁须与 upstream 一致，不能单独抬版本 |
| LiveKit server / Go / Python / Node | server `1.13.5`；其余按各自 manifest | 不与 Agents 联动升主版本；出现明确安全公告、兼容阻塞或可测收益时再建项 |

官方复核入口：[Agents](https://pypi.org/pypi/livekit-agents/json)、[OpenAI 插件](https://pypi.org/pypi/livekit-plugins-openai/json)、[Silero 插件](https://pypi.org/pypi/livekit-plugins-silero/json)、[RTC](https://pypi.org/pypi/livekit/json)、[API](https://pypi.org/pypi/livekit-api/json)、[FunASR](https://pypi.org/pypi/funasr/json)、[VoiceMem 插件](https://pypi.org/pypi/livekit-plugins-voicemem/json)。迁移依据：[1.8.0](https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0)、[1.8.1](https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.1)、[1.8.2](https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.2)、[AGC #7064](https://github.com/livekit/agents/pull/7064)、[OTel/PII #7104](https://github.com/livekit/agents/pull/7104)。

## P0：当前链路与发布前提

### [x] P0-01 重新核准运行基线，恢复 readiness 的真实证据刷新

- 完成（2026-09-15）：`refresh_readiness.sh` 的 live stack tag/Compose 镜像核验修复已部署。timer 实际于 **04:35:27 CST** 触发；04:36:23 Doubao 时间戳对齐失败，04:41:24 systemd 自动重试一次，**04:42:39 refresh PASS / Result=success / ExecMainStatus=0**。这是定时触发后自动恢复成功，不是首尝试通过，也不是手动触发。新 Agent/Bridge 发布后又直接执行真实 LiveKit/provider smoke 并通过，16:00:49 回环/公网 ready、core 12/12、新 heartbeat 已核实。证据：`outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/{readiness-timer.log,readiness-refresh.log,postflight.jsonl}`。
- 原因：过期 smoke 使发布门状态失真；仓内新代码不等于当前 overlay 已部署，尤其 Control 与小程序。
- 工作：只读取实际 image/source/override/有效 env，按组件对应 `code / wired / enabled / verified`；区分 Agent/Bridge、Control、sidecar、固件、小程序版本。核销 HANDOFF 遗留的旧阻塞和旧 epoch，播中/播后告别分开记。
- 已核实 timer/service、真实 provider 日志与生效 Compose；原目录名代替 stack tag、smoke 未覆盖 live 组件是已修复根因。保留本次 Doubao 短时失败记录，不把一次自动恢复外推为 provider 永不波动。
- 完成条件：当前 release 的真实 LiveKit/provider smokes 成功后生成证据，回环与公网 readiness 均 ready；core 12 个具名检查均 ready、Agent heartbeat/tag 匹配；至少再观察一次既有 12h 定时刷新成功。只手动刷绿一次不能关闭本项。不得延长 TTL、跳过 smoke 或伪造证据。
- 入口：`scripts/refresh_readiness.sh`、`infra/memoria-readiness-refresh.*`、`services/control_api/app/routes/readiness.py`；定向验证 `test_readiness.py`、`test_mark_readiness.py`。

### [x] P0-02 备份现状核查与隔离恢复验证（启用备份按用户决定暂缓）

- 本项关闭的是**核查与演练**两部分。**启用自动备份不在本阶段范围**——用户 2026-09-14 决定：项目验证阶段暂不启用自动备份与异地副本；未完成子项已移入「暂不排入执行队列」，不计入本项完成。
- 核查结论（2026-09-14）：自动备份**从来没在跑**。三个阻塞都已复现——`offsite-backup` profile 从未启动（`/etc/memoria-offsite-backup.env` 与 `postgres_backup_staging` 都不存在）；`postgres-base-backup.sh` 的 `pg_basebackup --dbname=postgres` 在 PG 17.8 客户端直接报 `missing "=" after "postgres"`（已在仓库修掉并加回归断言）；`pg_hba.conf` 只对本机放行复制连接，独立备份容器走 `memoria_default` 网桥被拒（`no pg_hba.conf entry for replication connection from host 172.19.0.14`）。因此 8/27 起累计的 1503 段/23.5GB WAL 此前无 base backup 可配对、单独不可恢复。
- 演练（隔离，未改生产配置、未覆盖生产数据）：base backup 2s/69MB → `pg_verifybackup` pass → `--network none` 隔离容器 5s 起库 → 应用表与 2305 条证据事件可读 → MinIO 音频对象 key 与密文 SHA256 一致 → WAL 0 缺口且末段等于备份 `START WAL LOCATION`。
- 保留资产：`/var/backups/memoria/drill-20260914-p0-02/base`（校验通过）、`report.json`；9/12 手工 dump 的 `sha256sum -c` 仍 OK、`pg_restore --list` 1210 项。证据 `outputs/acceptance/run-20260914-p0-02-restore-drill/`，细节与三条启用路径见 `HANDOFF.md`。
- 未做：PITR replay 演练；异地副本（零）。

### [ ] P0-03 收口当前 1.8.1 语音缺陷与真机稳定性

- **2026-09-16 真机证据（当前版本 `20260916-livekit-181-v1` + 板上 `0025`，未刷机）**：五段捕获八种会话（A–H），证据 `outputs/acceptance/run-20260916-p0-03-livekit181-device-acceptance/findings.md`。A 取得新 VAD 受理；C/F 分别完整播放 44.18s / **48.28s**，操作员确认；F 的设备 2413 帧、`supply_waits=0`、四阶段齐全并回 idle，`>45s` 判据取得 **1 次**。E/F/H 均 `conversation_end_explicit`，H 同 session 三步连跑完成。**不能整体标通过**：H 的 `ACK→正文=1.969s` 超 1.5s 门，B/D 停滞，G 丢失续问；多数轮次 0.3–0.6s 不能抵消失败样本。
- **新增缺陷 1（设备停滞，B/D 两次）**：桥侧音频长度 **58.88s / 57.46s** 的十天回答，在开播约 1.5–1.6s 出现 **366 / 412ms** 软件供给等待，随后停声、串口无输出、卡在 `speaking`，仅硬复位恢复。D 无 TTS 错误、桥 `reason=final_frame`，因此 B 的 TTS 超时不能解释两次停滞。**没有证据证明 55s 是阈值，也没有证明供给等待是根因**，停滞发生在开播早期，不能写成播到 55s 才卡。
- **定位口径纠正**：`device_backpressure_drop_total=0`、`stale_generation_drop_total=0` 与 32 帧队列上限，只说明已观测计数分支没有丢弃，**不能证明设备完整收包或排除 Edge/网络/WS writer 路径**。`HANDOFF.md`/原 findings 中「已排除交付丢失、定位收包后播放路径」暂不作为已证实结论；设备播放路径仍是候选。下一步按同一 generation 取 Edge 前/中/后 `/metrics` 差分、writer/队列状态、设备收包→解码→播放消费计数与停滞任务栈/锁/看门狗证据；已有 `edge-metrics-probe.py` 可复用，不先加 pre-roll 掩盖问题。
- **2026-09-16 新增缺陷 2（服务端，确定性，已修复）**：生产 env `DOUBAO_TTS_TOTAL_TIMEOUT_S=20` 是单次 TTS 的**挂钟**上限。58.88s 的答案在 20.23s 被切断并抛 `APIConnectionError: total-timeout`（`providers/doubao_tts.py:778`），LiveKit 因「部分音频已下发」跳过重试，本仓分发在**已排队音频排空后**（距报错 **38.6s**）才落 `event=error output_task_exception`，该代 `provider_completed=False`、设备拿不到正常播放终态。合成速率实测 2.90x/3.18x/6.8x 波动 → 20s 墙对应约 58–140s 音频，**「>45s 是否越线」不能只按音频长度预估**。**已修复**：`providers/doubao_tts.py` 在首包到达后，收到后续音频/字幕包时重置流式 stall watchdog (`total_timeout_s`)，且设立 `hard_deadline = 180s` 上限，防止超 20s 长回复被挂钟强杀；新增单测 `test_streaming_audio_resets_stall_watchdog_beyond_initial_total_timeout`。
- **新增缺陷 3（G，临静默续问丢轮，已修复）**：13:59:15.946 已有 ASR final（`text_len=5`），设备 VAD start 13:59:18.947 才到，受理剩余预算 0、`grace_active=False / watchdog_armed=True`；endpoint 13:59:22.723、静默关闭 13:59:22.761，整轮没有 commit/early-query。根因为 VAD 受理后 speech watchdog 已武装接管，但当 3s 内部 grace 到期时旧逻辑因 `grace_deadline is not None` 未暂停静默关闭，在 endpointing 期间误触发 `owner_silence_timeout` 杀掉 turn。**已修复**：`media_session_standby.py` 在 `_owner_silence_watch` 中，若当前 VAD 为活跃会话流且 watchdog 已接管，由绝对看门狗守护，静默计时器 stand down，不再因临近静默误杀正在说话/处理的语句；新增单测 `test_active_vad_with_armed_watchdog_supersedes_owner_silence_grace`。
- **播后告别按场景记账**：E/F/H 成功；C 约 8.5s 开口、受理剩 0.119s，ASR final 后仍静默关闭；A 无告别输入，G 因续问失败未走到告别。**不再把 A 无输入计入失败分母报 3/5**；固定有效尝试定义和重复次数后再判可靠性。窗口约 6.8–9s 的有限样本不构成固定安全阈值。
- **当前状态（2026-09-16）**：`code=d96d4c2 / wired=existing_provider_and_python_lifecycle / enabled=20260916-livekit-181-v1 / verified=partial_device_evidence_with_open_failures`。继承 `1cf8dec` 的 ASR 候选隔离、天气候选上限与共享 deadline，但新发布已有上述失败，不是只剩未执行验收。
- **已完成的前序修复与证据（2026-09-14/15，不能替代 9 月 16 日验收）**：
  - [x] 多日天气本地修复：旧日志保留「未来3天南京」却请求 `forecast_days=1`；明确从今天起的 1–16 天逐日回答，模糊/矛盾/超限范围及不完整结果走 fallback，不改答今天。
  - [x] 续问待命竞态本地修复与主审：合法 VAD 在 projection await 前接管，已启动的绝对 watchdog 撤销旧 grace；锁内 revision 排除旧关闭；旧 endpoint 不覆盖新语句；watchdog 覆盖 ASR finalization 等待到 endpoint-tail 接手。重复 VAD 不续预算，显式告别/terminal 门不放宽。
  - [x] 本地验证：前一轮关联四套件 **314 passed**；本次新增 **43** 项异常用例后，完整 Agent unit **2013 passed**，Agent Ruff、strict mypy（157 文件）、模块预算、diff whitespace 通过。新增受理/revision 日志用于下一轮实证，不能用单测代替真机。
  - [x] 发布前异常路径有界性补核（本地）：ASR finalize 故障及下一 PCM 恢复都在异步发布前接回剩余预算，旧回调不误清新 VAD；首次/两次 prepare retry 共用真实绝对 tail，超时经既有 terminal 链停止旧提交；重连在身份核验后先取消旧 prepare，避免旧 tail 被否决后卡住清理锁。ASR **10** 项、retry **33** 项均通过，包含失败先红后绿、成功/耗尽/卡住、已耗 grace、旧 epoch/新 VAD/锁竞态；失败和旧 epoch 迟到成功均不补满预算，重连等锁期间 terminal 不得复活，不放宽 ASR 防重复门。范围限协作取消与迟到结果 fencing，不代表永久吞取消的 provider 或全部 reset/close I/O 已有硬上界；详见 `HANDOFF.md` 同日条目。
  - [x] Agent/Bridge 发布（2026-09-15 15:58:42 CST）：冻结 tag `20260915-weather-followup-lifecycle-v1`，正式干净 worktree 门禁及 CI **34943397294 success**；捕获/报告 **62 passed**。双容器同镜像 healthy/restart=0，各 **910** 文件 SHA 匹配；有效配置和 **17** 个非目标容器不变；候选真实天气请求 `forecast_days=3`、今天/明天/后天齐全，真实 LiveKit/provider 与回环/公网 readiness 通过。未刷机、未改运行配置。证据 `outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/`；后续设备重连及首轮功能听感见下项，三轮整体仍待验。
  - [x] 首轮功能/听感复测（2026-09-15，**1/3 通过**）：`outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/session-2/` 取得 16:18–16:19 新会话 `67286af6`、stream epoch 1954。双问各提交一次，南京天气依次请求 3 天/1 天；正文 19.06s/5.18s 均有终端回执，ACK→正文服务端间隔 0.374s/0.349s，播后告别回 idle。用户明确确认 **「三天齐全、续问正常、声音无断续或卡断」**，本轮功能/听感已通过，不重复索要确认。`capture.json` 未收尾、进程退出原因未知，不能记 healthy；独立补取三容器日志与原文件 SHA 一致。绑定先前刷写收据，本次未读写固件。详见本轮 `report.txt`、`audit.json` 与 `HANDOFF.md`；`session-1/` 仅为启动/待机检查，不计听测。
  - [x] **计量生命周期与捕获收尾本地修复（2026-09-15）**：真实调用顺序证实 speaking 入口 `ResetDecoder()` 提前关闭已宣布的窗口，导致全零；新候选仅在同 generation、已打开窗口、fence 精确递增时保留统计，reset 切断等待和旧 token，换代/flush/stop 不复活。捕获增加首信号、正常/异常收尾、逐路有界进程清理和原子 metadata 替换；报告独立判断 lifecycle/receipt，缺收尾或非法/缺失流记录不得报 healthy。**304 passed**（53 捕获 + 74 报告 + 177 固件）、全仓 Ruff、模块预算、clean overlay apply/build 与既有 gate 通过；专项覆盖与主审取舍见 `HANDOFF.md`。不是历史声音异常已全部解决的证明。
  - [x] **新候选 app-only 与启动/捕获收尾（2026-09-15 18:49–18:52 CST）**：冻结 `d1ad38f`，写前当前 app/整槽与旧板收据匹配；只擦写 `0x20000..0x340fff`，整槽回读、六个保护区域逐字节比对及 ota_1/assets MD5 均通过。新 ELF `da6ebdd16…`、编译时间 17:36:12 匹配，18:51:44 回 idle。45 秒 `boot-check` 正常到期，三路均 `stopped_by_capture / exit_code=255 / forced_kill=false`，结束时间完整、无 serial/cleanup error；仅证明启动和捕获收尾，未覆盖真实媒体。出现一次 BMI270 I2C timeout，之后心跳正常，独立保留。证据与紧邻回滚在 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/`，不可变刷写收据与后续 boot 记录分开绑定；详情见 `HANDOFF.md`。
  - [x] **新候选短播放计量/真实会话捕获验收（2026-09-15 19:05–19:07 CST）**：session `684348e9` / stream epoch **1955**，五代 **5/5** 均 `first_output=yes / output_frames>0` 与终端 summary，输出帧 71/124/852/133/267 与 Bridge 一致，五代终态均 `playback_completed`；用户确认 **「说完了，三天齐全、续问正常、无重复提示或断续卡断」**。新固件自身功能/听感第 1 轮通过，总计功能 2/3。主动 SIGTERM 后 `completed`、结束时间及三路退出记录齐全，无强杀/serial/cleanup error；不是 900 秒到期。证据 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/session-1/{report.txt,audit.json}`；本轮未读写固件，原刷写/boot 收据和旧 incomplete 捕获不改。仅短播放软件队列计量，不证明物理 I2S/DMA 或长播；查询时延另列未过。
  - [x] **ASR 候选隔离与天气预算修复发布（2026-09-15 21:00 CST）**：`1cf8dec` / `20260915-asr-weather-boundary-v1`，release gates 与 CI **34971139245 success**。旧 pending ASR 片段不再跨 sample boundary 污染新 final；完整地名及明确行政边界最多 3 个候选，OpenMeteo geocode+forecast 共用默认 8s deadline（不含 Qwen fallback/整个语音链）。真实 provider 四例通过：今天/明天/未来三天南京各 1 次 geocode，三天标签齐全；污染串未逐字削首猜南京。双容器 healthy、有效配置和 17 个非目标容器不变；运行/回滚/readiness 证据见 `HANDOFF.md` 与 `outputs/acceptance/run-20260915-p0-03-asr-weather-boundary-release/`。
  - [ ] **查询延迟收口（2026-09-16 真机：多数轮次通过，但出现 1.969s 越线样本，未收口）**：旧 epoch 1955 的 final 10 字被宽时间窗拼成 commit 42 字，触发 **21** 次 geocode、查询 **7930ms**、ACK→正文 **5.028s**；旧片段的声学来源仍未知。**2026-09-16 当前版本真机八轮**：`ACK→正文` 为 **0.356 / 0.383 / 0.323 / 0.362 / 0.426 / 0.642s**（六轮通过）+ **1.969s**（越线，epoch 1963 session `de598a18` 的三天天气、查询 1551ms）+ 迟到受理一次；`commit→ACK首帧` 0.310 / 0.307 / 0.316 / 0.324 / 0.318s。同轮设备侧 `vad_end→首帧` 为 **4.557s**，A 第二问为 3.748s（由 ASR finalize 主导）。**故本项不标通过**：延迟是数据相关的，确定性查询仍可能越过 1.5s 门。历史 5.028s 与 21 次串行 geocode 未复现；仍非 DAC/可闻精密测量，未覆盖慢 provider 与全部 ASR overlap 边界。当前仍只有首 ACK，不恢复历史第二提示、不放宽防重复门、不延长静默或加预缓冲。
  - [x] **用户离场后的有界自动代测尝试与收尾（2026-09-15）**：Mac 外放/麦克风参考匹配自测通过，Tingting 与 Meijia 各 3 次唤醒均无设备 wake，未播放问题，正常停止。输出恢复 31%/muted=true、输入仍 82%，录音/串口无残留；只完成测试尝试，不完成真机验收。录音与日志时轴不一致，拒绝估计精确 gap；机器分析不计人工轮数、不证明三天完整或无断续。证据 `outputs/acceptance/run-20260915-auto-audio-{retry3,meijia}/`；原始 `results.json` 不改，重算结果单列 `audio-analysis.json`。工具仍在 ignored 的 `outputs/design/auto-audio-20260915/`，未随生产代码入库。
    - 用户追加提高音量要求后，Meijia **90%** 对照亦完成（22:36–22:37）：三次唤醒词均被 Mac 麦克风录到，但设备仍 idle、无会话；没有继续提问，不算新版真机通过。自测录音峰值接近满幅，不能用继续加音量替代声学原因定位；已再次恢复 31%/muted=true、输入 82% 并停止全部采集。证据 `outputs/acceptance/run-20260915-auto-audio-meijia-volume90/`。
  - [x] **同一 session 三步连跑完成（2026-09-16 H，epoch 1963 / session `de598a18`）**：三天天气（`days=3`）→ 播完续问（**turn 3 / gen 4 提交成功**，`gen 4→5` 间隔 0.642s）→ 播后告别（`conversation_end_explicit`，串口回 idle）；五代四阶段齐全（含 `actual_heard`），设备 `supply_waits=0`。**同轮代价**：第一问 `ACK→正文 1.969s` 越 1.5s 门（设备侧 `vad_end→首帧` 4.557s）。同脚本第一次尝试（G，epoch 1962）续问整轮丢失——ASR 已出 `text_len=5` final，但设备 VAD start 迟到、受理时预算 0.0、endpoint 晚于关闭，见下条。
  - [ ] 三轮功能/听感收口（**当前发布已有自身证据，但稳定性未过**）：A 完成三天天气与续问，C/F 完成长播，E/F/H 完成告别，H 完成同 session 三步。**剩余**：按有效输入重定告别重复矩阵、H 第一问延迟越线（1.969s）、G 续问丢失及 B/D 停滞。功能轮数与时延、竞态专项分别判定，不沿用跨 release 的累计通过数。
  - 9 月 15 日现场证据：`outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-2/`，epoch 1953。播完 12:57:21.672、VAD start 12:57:27.566、静默关闭 12:57:29.101 CST；旧日志不足以证明具体受理/grace 分支。同轮 ASR overlap/recovery 仍开放，不回退防重复门；详见 `HANDOFF.md`「2026-09-15 多日天气与续问关停修复」。不要与下方 9 月 14 日的 `session-2` 混读。
  - **新固件计量候选**：`code=true / wired=true / enabled=true / verified=short_playback_and_limited_long_samples_only`（9 月 16 日补 C/F；B/D 无停滞后 summary，不能外推全程计量或长稳）；overlay `24531273…b3`、app `7d95c8a1…b65`、ELF `da6ebdd1…69f2`，完整哈希、构建、刷写收据见 `HANDOFF.md`。**紧邻回滚**为刷写前读出的旧 app `dfba3d61…`，旧板首轮听感通过但计量未覆盖真实播放；原 `run-20260915-p0-03-firmware-metering/postflash.json` 仅绑定旧板。
  - [ ] 长播验收：**2026-09-16 当前版本达成 1 次**——九天天气正文 **48.28s** 完整播完（桥侧 2414 帧 / `reason=final_frame` / `send_audio_ratio=1.00` / `max_gap_ms=55`；设备侧 `output_frames=2413`、`supply_waits=0`、`prestart_waits=1/476ms`、`boundary/close_dropped/outside` 全 0、自行回 idle），操作员确认九天全部播报；八天 44.18s 亦完好。**B/D 的 58.88s / 57.46s 桥侧音频样本均出现设备早期停滞**，时长阈值与因果未定，本子项保持未完成。下一轮同时保留设备接收/解码/播放消费、`supply / prestart / boundary / close_dropped / outside`、Bridge pacing、delivery ledger、Edge 指标差分与操作员听感；`pre-roll` 未实现，不先实现 `0026`。桥侧比率、软件队列与 `actual_heard/playback_ended` 均不能单独当作用户完整听完。

- 就绪状态（2026-09-14 17:00 CST）：板子在线（WiFi 192.168.8.142、Activation v3、唤醒词茉莉、idle），串口空闲，捕获工具需 `uv run --no-project --with pyserial --with esptool`（项目 `.venv` 无 pyserial/esptool），服务端 readiness ready。会话计划与 11 个场景、判据、分段安排见 `outputs/acceptance/run-20260914-p0-03-voice/session-plan.md`；逐轮原始数字由 `scripts/voice_session_report.py` 产出。**操作要点**：打开串口会让板子重启，每段捕获开头约 10 秒启动，须等 `activating -> idle` 再说话。设备端固定安全话术（P0-04 场景 10）与屏幕照片（场景 11）借用同一时段。
- **真机第一段已完成（2026-09-14 17:05–17:11，epoch 1946，会话 `90f1dcf4`）**：时延全部达标——问候 0.990s、`commit→首帧` 0.394/0.395/0.440s、`ACK 播完→正文首帧` 0.420s/0.319s（**上一轮 1.766s 未复现**）；告别经 `conversation_end_explicit` 后 30ms `speaking -> idle`、无静音超时。**但发现一个真缺陷**：用户只提问一次「第二天天气」，系统提交了**两个用户轮次**（`turn 3 text_len=27 @09:07:03.39`、`turn 4 text_len=20 @09:07:07.20`）；turn 4 的文本来自同一次提问音频的离线救援转写（`funasr segment rescued offline text_len=20`），先被 supervisor 以 `straddles_committed_without_timing` 拒绝（stage=preview），1.5s 后仍提交为新轮次并取消 turn 3 尚未播出的正文（`preempted/output_task_cancelled`、首帧未发），用户听感为「连说三次稍等、第一遍答案丢失」。既有 `media duplicate media turn skipped` 只比文本相等（27≠20 未拦住）。证据 `outputs/acceptance/run-20260914-p0-03-voice/session-1/`（`report.txt`、`findings.md`）。
- **该缺陷已修复并切流（2026-09-14 17:48:05 CST）**：`_recover_straddling_live_query_final` 不再采用「大部分音频已被提交」的跨区间救援 final（无词级时间戳就无法按水位切文本）。回归 `test_mostly_committed_straddling_final_is_dropped_not_readopted` 红→绿；`test_media_session.py` 201 passed、ruff/mypy --strict/模块预算通过；已发 Agent/Bridge 组件版 `memoria-agent:20260914-straddle-rescue-fix-v1`（revision `1164352`，双 healthy，容器内含修复源码，回滚 `rollback-20260914-straddle-rescue-fix-v1-pre`）。**待验收**：真机复测「同问重复提交 + 慢查询单轮第二提示」确认不再出现一次提问两个轮次；此外第一句排下的 early live-query 端点在**该轮已提交后仍可能再次触发**（本次它带的是被污染的救援文本，修复后该文本不再产生），这条路径未被证明关闭，需要专项复现。
- **真机第二段（2026-09-14 18:24–18:30，两段会话 `7d9d3a2f` / `e81ff828`）**：修复后的「明天天气」ACK + 正文四条回执全齐、**没有再出现第二个轮次**（该缺陷复现路径未再触发）。但暴露两个新的用户可见问题：
  - **静默窗口竞态**：`MEDIA_OWNER_SILENCE_TIMEOUT_S=10.0`，窗口在用户开口前起计、裸 VAD 按设计不重置；用户说完（10:25:47.054）到 ASR final（10:25:48.980）的 1.9s 里窗口到期，Edge 10:25:49.012 以 `owner_silence_timeout` 关闭会话，轮次 10:25:49.359 提交后被 `stale_stream_epoch` 拒绝启动回复（`media final did not start reply`）→「问了没回答」。
  - **长天气截断**：`e81ff828` turn 2 gen 3 播放 39.4s，账本 `provider_completed` 与 `actual_heard/playback_ended` 都在 10:29:30.3/.4，即「账目完整播完」，但操作员听感是「天气没播报完就卡断」→ 截断发生在账本之外（候选：上游回复/TTS 合成被截短，或设备早停仍回报完成），需对比该轮文本长度与合成音频首尾帧。
  - 长天气**断续**（操作员确认是「断续之后停」，不是内容被截）：`e81ff828` turn 2 gen 3 播放 39.4s。播放窗口 18:28:51–18:29:30 内串口只有首帧与两条状态迁移——**没有 VAD、没有 I2C 报错、没有 TLS/WSS 报错**，所以闪避/暂停不是原因，故障也没被现有日志记录。时序线索：`provider_completed` 距 `first_frame` 39.28s、播放 39.4s → 合成与播放几乎同为 1.0x 实时速率、测得的供给余量可能很小；这只是设备侧欠载的候选解释，尚未证明。**当前可观测性测不了播放连续性**（只有上行 PCM tap，无下行 tap、无每帧/欠载计数），下一步先补测量：设备侧队列等待分类，再与 Edge 下行分片节奏计数对照。
  - 附带观察：第一段会话 18:28:26 因 **WSS 非正常关闭**结束（串口 `mbedtls_ssl_fetch_input error=76` → `esp-tls-mbedtls read error -0x004C` → `Device WebSocket disconnected attempt=1`，Edge 见 `close_code=1005`），2 秒后重连成第二段会话；BMI270 I2C `ESP_ERR_TIMEOUT` 在别处偶发（播放窗口内没有），两件都需单独跟踪。
  - **下行节奏测量已上线（2026-09-14 18:45:37 CST，`memoria-agent:20260914-downlink-pacing-v1`）并用一次长天气取到数**：`session=59f4c8ac` gen 3 `frames=1774 audio_ms=35480 wall_ms=35545 max_gap_ms=41 produced_ratio≈0.998`；同会话 gen 1/2 为 `1020/1025ms max_gap=60`、`2660/2639ms max_gap=25`；设备侧同代播放 18:47:54.6→18:48:30.2（35.6s），与 `audio_ms` 一致。**结论仅限桥侧**：产出接近 1.0x 实时、相邻帧最大间隔 41ms，未见桥侧长间隔；这说明供给余量可能很小，但不能证明设备侧已经欠载，也不能据此宣称设备播放队列长期接近空。账本「播放完成」也不能单独证明用户实际听完。
  - **原设计记录（2026-09-14，用户选 a）**：曾计划采用播前预滚 + 设备侧欠载计量，设计见 `outputs/design/playback-jitter-buffer-20260914/decision-01-pre-roll-and-measurement.md`。当前实际落地只有 observation-only `0025` 计量；`PRE_ROLL_MS≈300ms` 仍是待验证候选，不是已接线行为；不得借机放开播放期语音打断。
  - **`0025` 计量实现（已刷入，短播放已验）**：补丁、header、宿主断言和 reset/fence/late-token 防污染逻辑已完成；仅测 decoded playout queue consumer 的 software queue wait，分类为 `supply / prestart / boundary / close_dropped / outside`，并单独记录 exact TX-EOF completion；不宣称 I2S/DMA underrun 或由计量推断听感。clean overlay apply、固件宿主测试和 clean build 已通过；当前候选已有自己的回读/启动及五代短播放计量证据，未继承下两项旧板结论。9 月 16 日已补有限长播样本，稳定性仍未过，见上方当前证据。
  - **历史刷机收据（2026-09-14 19:05 CST）**：旧版 `0025` 简单计量候选 app-only 写入 `0x20000..0x340fff`，整槽回读、erase 范围外、identity、非 app 分区及 ota_1/assets 校验均通过；板载 `release_head=fa54d7d` / app 3,277,856 bytes / SHA `f58f48a4…` / ELF `16d981b3…`。收据明确 `boot_verified=false`、`real_device_conversation_verified=false`，且与当前 `PlaybackSupplyMeter` 分类实现不一致，所以只能记为旧候选已写入，不能记为当前实现已启用或已验收。
  - **旧板候选已刷入并启动验证（2026-09-15 11:19–11:22 CST）**：app-only 只写 `0x20000..0x340fff`；app 全槽回读逐字节一致，erase 范围外、identity、bootloader、partition、NVS、otadata、phy-init、ota_1/assets 均未变；启动证据通过。绑定为 HEAD `65257e0e1285a3126e484ad22c308315c971caad`、upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`、overlay hash `97fc64f28dcd95c365a99176d2ed26a749beb20c3e7f0179283c26484e7559e8`、app SHA `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`、ELF SHA `efe1b241c3d6b4126e3b2e9d57ab4bfbe76bbd6a4844b063c0a9aa8d160c58625`；收据与回滚件在 `outputs/acceptance/run-20260915-p0-03-firmware-metering/`。此为既有旧版证据，本轮新候选回读与启动见上项。
  - **旧板短捕获（2026-09-15 11:34–11:39 CST，计量无有效播放覆盖）**：设备 `vad_end -> first_received=0.517s`；generation 1 的 `supply/prestart/boundary/close_dropped/outside` 原始记录全零，但统计窗口提前关闭，不能认作有效零等待。Bridge `frames=120/audio_ms=2400/wall_ms=2413/max_gap_ms=69/after_pacer_send_ratio=0.99` 只说明发送侧节奏，不证明 I2S/DMA 或听感。串口 `SerialException: read failed: [Errno 6] Device not configured` 导致 `degraded`；不计有效播放计量或完整长天气验收。证据：`outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-1/`。
  - 未跑到：告别（该段未触发，turn 2 后直接静默关闭）、待机与五表情照片、P0-04 学生安全话术。
  - [x] 历史工具缺陷已修复：`scripts/voice_session_report.py` 曾以 `(turn, generation)` 聚合，跨会话串账产生 `commit->frame=190.859s`；当前已使用完整 `DeliveryKey(session, epoch, turn, generation, tool)` 隔离，不再列为待开发项。
  - 证据 `outputs/acceptance/run-20260914-p0-03-voice/session-2/`（`report.txt`、`findings.md`）。
- 进度（2026-09-14）：**本地对照基线已建立**——`test_media_session.py` 200 passed、`test_duplex_runtime_wiring.py` + `test_utterance_router.py` 191 passed（证据 `outputs/acceptance/run-20260914-p0-03-local-baseline/`）。ACK 后空输入恢复、旧查询不回流、同问重复不重复提示、空转写处理均有既有回归并通过。
- 剩余条件（2026-09-16 更新）：当前版本 H 三步连跑与 F 的 >45s 单次已取证，仍需通过 B/D 停滞复现回归、TTS 部分输出失败收尾、G/C 临静默有效输入、H 延迟越线与慢 provider 矩阵；旧版两轮功能/短播放通过不重复计为当前版本通过。没有设备/操作员时先做本地有界复现；自动录播须有当次授权且不能代替人工听感。
- 下一动作（按风险顺序）：① 固定 B/D 复现输入，补同 fence 的收包→解码→播放与任务停滞证据，再决定修复点；② 将 20s TTS 总墙、已下发部分音频、排空后迟到 error 纳入现有 provider/DeliveryLedger 的有界终态测试，明确长回复预算/分段及失败停止策略，不直接无界抬超时；③ 复现 G/C 的 final/VAD/endpoint/关闭竞态，验证已受理语句的剩余预算接管，不让重复/迟到事件续命；④ 固定当前 release/固件重跑失败项与 >45s/告别重复矩阵。
- 查询侧保留旧 ASR 候选隔离与天气候选上限，补「ACK 后空输入 → 有效正文恢复」、同问不重复提交、答完再问、南京改北京与慢查询等待；新 VAD 受理已有现场，不再写完全未取到。沿用 provider/Router/Ledger/fence，记录提问结束、ACK 起止、正文首帧和终端回执，不把听感无断续当时延通过。
- 完成条件：有效答案不静默丢失、ACK 不重复/不截成残句、旧查询结果不回流；说完到开口及提示间隙无 >1.5s 纯静音；长天气 >45s 与长回复可完整听完。历史「约 2.5s 第二提示」不是当前已接线机制；若限流/总期限修复后慢查询仍越线，单独明确等待策略再实现，不能默默降低标准或重开重复提示链。预先固定场景/重复次数，逐轮给原始数据，不只报均值。
- 真机复测同时保留已通过的问候不掉线、播后告别、BOOT/触摸硬停；补待机与五表情照片。播放期语音告别不混进本项，移至 P1-07。
- 入口：`HANDOFF.md`「下一验收」「20260913-empty-input-resume 复测清单」；`test_media_session.py`、`test_duplex_runtime_wiring.py`、`test_utterance_router.py`。没有在线板卡就只完成本地复现，设备项保持未完成。

### [ ] P0-04 按当前使用人启用学生危机策略，补齐身份隔离与设备安全闭环

- **需求口径（用户 2026-09-16 明确）**：在设置使用人时判断该人是否为学生（未成年）；是则启用学生危机固定安全话术。管理账号是 adult/minor、谁登录或购买设备，都不决定该使用人的策略。未成年使用人不必另注册/登录一个“学生账号”；成年在校生不因“学生”标签自动变成 minor，资料未知不从管理账号猜年龄。账号鉴权、代操作授权、监护同意与声纹 owner 判定仍各自保留，不因选了使用人而越权。
- **审查确认的缺口**：`routes/interaction.py:1978–2005` 的危机分支未消费当前使用人，`get_subject_profile(user_id=account_id)` 后以 `minor_user_id=account_id` 决定通知/归属；adult 账号管理独立未成年 person 时不会按该孩子入队。`services/common/crisis_policy.py:33` 目前只接 query/语义证据，固定回复是 adult/unknown 也会触发的通用兜底，**不是已经按使用人接线的学生策略**。这是既有接线缺口，不应误报为 1.8.1 升级导致成年人账号下完全没有安全回复。
- **已有能力要复用**：`SubjectDraft.age_band` / `PersonSubject.subject_category` 已能记录独立使用人；`multi_subject_runtime.py` 与 `session_runtime/profile_service.py` 已把 `active_subject_id + subject_revision + subject_category + age_band` 纳入签名 RuntimeProfile，Agent 也已有 subject-aware 消费路径。直接补缺失的危机消费者与最小设置入口，不重建身份栈、不用客户端布尔代替服务端事实，不批量把账号鉴权改成使用人权限。
- 历史进度保留：9 月 14 日身份/同意/RLS **34 passed**；9 月 15 日真 PG 域 **45 passed / 0 skipped**，含固定文本逐字匹配、通知幂等/家长列表隔离、原文不进通知、撤销与能力拒绝（证据 `outputs/acceptance/run-20260914-p0-04-identity-matrix/`、`outputs/acceptance/run-20260915-p0-04-student-safety-loop/`）。但 `test_student_safety_loop.py` 用孩子账号登录并手工挂同账号的 minor RuntimeProfile，未覆盖「adult 管理账号 + 独立未成年使用人」，**只能证明旧账号路径，不能把本项写成只剩真机**；HTTP 的 `direct_text` 也不等于设备实际说出。
- [x] 第一小项：先加 adult 管理账号创建/设置独立 `under_14` 使用人的失败用例，不创建孩子登录账号；沿实际多主体 API→RuntimeProfile→response-plan 走通，不能仅手工注入账号同 ID 的 profile 造绿。复用 P1-03 的“最小设置/手动确认使用人”入口切片，入口显示当前人的年龄/安全策略；该切片前置于本项，不等待完整人格/音色功能，避免循环依赖。（2026-09-16 已完成：新增 `test_adult_account_manages_independent_under_14_subject_without_child_account`，成人管理账号绑定未成年使用人时自动建立激活监护关系并落到 `guardian_notifications`）。
- [x] 接线：服务端按当前使用人资料确定学生策略，将 subject/revision/策略版本绑定当轮 response plan 与 generation；切人或改年龄时走现有失效通知/安全切换，核查旧 plan 缓存、异步结果和音频不可串人。危机通知/审计绑定当轮未成年使用人及其有效监护关系/同意，不落到管理账号；副作用仍逐次授权，不能把签名 profile 当无条件通知许可。成人/unknown 的通用安全兜底保留，通知失败不能吞掉公开固定安全回复。（2026-09-16 已完成：`interaction.py` 从会话签名 `RuntimeProfile` 取 `active_subject_id`、`subject_category` 与 `age_band`，危机通知入队绑定孩子 `person_id`，成人/未知使用人保留固定文本但不入队未成年家长通知）。
- [ ] 验收矩阵：同一 adult 管理账号分别设置 `under_14`、`14_17`、adult 使用人；同一使用人更换合法管理账号时策略不变；未建孩子账号也可设置并生效；切人/改年龄与旧缓存/旧 generation 并发；unknown/guest/uncertain 不进入主人私有链、不误归属孩子通知；无同意/撤销仍按既有能力门立即拒绝或终止受限会话。分别核查历史、私人记忆、工具、音色、删除/导出与邀请不提升 owner。
- [ ] 实际闭环：受控成人测试者模拟未成年使用人危机场景，核设备固定话术逐字交付、终端回执与听感；通知 outbox 归属正确、重放幂等、家长按作用域可读、原文不外泄。涉及 schema/RLS/授权时必须带 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响域，跳过不算通过；可借 P0-03 真机时段，但无需真实未成年人参与。
- **范围不扩张**：沿用用户 2026-09-14 决定，不做微信订阅号/家长通知发送 worker、重试、模板或凭据申请；`delivery_status=pending` / 列表可读不等于家长收到。对真实家庭提供服务前再评估投递链。本项当前 `code=partial / wired=subject_crisis_pending / enabled=unverified / verified=legacy_account_tests_only`，运行与设备边界分别记账。
- 入口：`routes/multi_subject.py`、`multi_subject_runtime.py`、`services/session_runtime/profile_service.py`、`routes/interaction.py`、`services/common/crisis_policy.py`、`services/guardian/crisis.py`、`services/agent/src/runtime_profile.py` / `generation_output_policy.py`、`apps/miniprogram/pages/device/` / `utils/device-binding.js`；同时核对 guardian 页旧“学生账号开通”文案。`account_gate.py`、`routes/guardian.py` 与 `services/speaker/authority.py` 继续承担各自授权边界。

## P1：受控升级与第一阶段产品闭环

### [ ] P1-01 LiveKit 1.8.1 已发布：补隐私接线、制品门禁与回滚验收

- 价值：跟进连接池、会话关闭、播放计量、资源清理和 telemetry 修复；不是已证明能解决当前 ASR/AEC 故障。对应研究 R-20260907-01（吸收 R-20260911-01）。
- **当前进度（2026-09-16）**：Agent/Bridge 已发布 1.8.1；本地兼容性不是下一待办，剩余是下列审查缺口、标准镜像复现、当前版本真机失败项与回滚演练。9 月 15 日锁变更 7 项（agents/openai/silero、RTC、API、protocol、local-inference），`blingfire=1.1.0` 不变；既有门禁为靶向 **618 passed**、全仓 **5002 passed / 0 failed / 3 skipped（合计 5005，真 PG）**，不是 5005 passed。`uv lock --check`、frozen 安装、ruff/strict mypy（433 文件）/模块预算及 Edge `build/vet/test` 通过；历史全量证据见 `outputs/acceptance/run-20260915-p1-01-livekit-181/report.md`，本轮审查未重跑全量。
- [x] 本地候选：三件套同步升至 1.8.1，RTC/API 与 protocol 按组件表更新，`livekit-local-inference` 从 0.2.6 更新到满足 `>=0.2.7`；按真实约束重锁 `uv.lock`，保留 Python 3.12。禁止 `--no-deps` 绕过冲突，不混入模型、固件或 server 升级。
  - 完成（2026-09-15）：`pyproject.toml` 三件套钉 `==1.8.1`，`livekit-api` 由 `>=1.1.1,<2` 收紧为 `>=1.2.1,<2`，新增显式 `livekit-protocol>=1.1.25,<2`（API 1.2.1 要求 `>=1.1.25`，不能保留原锁 1.1.22）。`uv lock` 只动这 7 项。
- [x] 行为兼容：针对真实 SDK 验证 `TurnHandlingOptions`、STT/TTS adapter、RoomIO 与半内部符号；`TypedDict` 会静默接收未知键，必须验证配置被实际消费，不能只测构造不抛异常。LiveKit 侧保留 dispatch metadata→`controlled_half_duplex_session` 的策略，设备 Voice Core 侧保留 `audio_mode` 派生策略；兼容半双工路径禁打断，默认 preemptive 关闭，完整 generation fence 不变。
  - 完成（2026-09-15）：新增 `services/agent/tests/unit/test_livekit_candidate_compat.py`。逐键核对 `TurnHandlingOptions`/`EndpointingOptions`/`InterruptionOptions`/`PreemptiveGenerationOptions` **零丢弃**；`AgentSession._opts.turn_handling` 回读值与配置逐项一致（`{"version":"v1-mini"}` 物化为 `TurnDetector(model="turn-detector-v1-mini")` 已核）；半双工 `interruptions_enabled=False` 在真实 SDK 读回 `interruption.enabled=False` 且 preemptive 关闭；1.8.x 新增 `user_turn_limit` 默认 `None/None` 未生效；`aec_warmup_duration=None` 仍读回 `_aec_warmup_remaining==0.0`；`room_io` 五个 Options 签名与字段仍匹配，显式 `auto_gain_control=True` 且未配 NC 所以 #7064 默认值变化不影响现链路；`stt`/`tts`/`types` 中我们子类化或导入的符号全部仍在。记录：`build_turn_handling_config` 的 `stream_speak_while_think` 是本仓自有标志、不是 LiveKit 字段，目前无消费者读取，本轮不改行为。
- [x] 隐私/可观测性门（**双进程接线已补，门禁已修复防假绿**）：显式关闭内容采集 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0` 与第三方 PII 导出 `LIVEKIT_TELEMETRY_ALLOW_PII=0`，核 SDK 及本仓 `observability/media_otel.py` 的真实消费者。
  - 已完成的本地证据（2026-09-15/16）：`test_livekit_candidate_privacy.py` 使用真实 `TracerProvider` + `InMemorySpanExporter`，反证默认设置可导出 canary，显式禁 PII/禁内容时对应属性不出现、低基数计量保留。
  - 已有接线：Agent `main.py:51` 在加载 SDK 前调用 `_apply_telemetry_privacy_defaults()`；仓内 Compose 的两个服务也声明了两变量，env 示例已补。
  - **审查问题 1（Bridge 隐私保护缺口）已修复（2026-09-16）**：创建 `services/agent/src/telemetry_privacy.py`，`scripts/run_media_bridge.py` 在模块加载与 `run()`/`main()` 均显式调用 `_apply_telemetry_privacy_defaults()`，在加载 SDK 前强制关闭内容采集与第三方 PII 导出；`test_run_media_bridge.py` 新增用例钉住。
- [x] **审查问题 2：修制品 verifier 的隐私假绿（2026-09-16 已修复）**：`scripts/verify_agent_release_artifact.py` 改为在全新子进程解释器中分别测试 Agent 入口、Bridge 入口、InMemorySpanExporter canary（反证无泄漏）、以及未调接线时 canary 泄露的反例（防止假绿）；新增单测 `scripts/tests/test_verify_agent_release_artifact.py` 覆盖。
- [x] **审查问题 3：镜像解析必须真的比较候选（2026-09-16 已修复）**：`scripts/resolve_target_images.py` 重构为接收真实 `--stack-tag`、`--expected-tag`、`--expected-image` 及 overrides，逐服务验证 resolved image，对 tag 不匹配、缺服务、服务间镜像不一致坚决非零退出；新增单测 `scripts/tests/test_resolve_target_images.py`（7 项）覆盖。
- [x] 本地锁与回归（2026-09-15）：靶向 **618 passed**、全仓 **5002 passed / 3 skipped**（真 PG）、ruff/strict mypy/模块预算及 Edge Go 检查通过；详见上方证据。不因今天只改文档重复跑全量。
- [ ] 标准完整镜像构建与验证：从冻结 source/lock 走仓库标准构建，所有消费共享锁的目标镜像分别验证，Agent/Bridge 同一镜像；依赖变化不走 agent-only source overlay。9 月 16 日已做 delta 镜像版本/兼容检查，但不是标准全量可复现构建，更不关闭上述隐私缺口；只运行实际受影响的镜像/门禁，不扩大到无关服务发布。
- [ ] 当前发布收尾与回滚验收：沿 P0-01/02 的发布前提和 P0-03/04 的相关基线；已上线不等于验收完成。修发布门后按授权最小切片补发，真实 provider smoke、当前设备对照与延迟复核通过，留当前+一个可运行 rollback。
  - **已上线（2026-09-16）**：`d96d4c2` / tag `20260916-livekit-181-v1`，2026-09-16 11:36:08 CST 仅重启 Agent/Bridge，双 healthy、restart=0。运行时实测 1.8.1（agents/plugins）/1.1.18（RTC），LiveKit smoke PASS，provider smoke 第二次全项 PASS（第一次 InterruptSemantic 超时，两次都留档），readiness ready、core 12/12、Agent heartbeat ready。回滚 tag 冻结为切流前 image。
  - **仍是 delta 构建，不是仓库标准全量构建**：以上一版镜像为基座，重建锁定依赖集并覆盖源码；收据 `outputs/acceptance/run-20260916-livekit-181-deploy/CUTOVER_RESULT.txt`。当时镜像内 verifier 报 PASS，版本/兼容部分可保留，但隐私部分经本轮审查不足以验收，重放须修门并走标准构建。
  - 未完成：上述双进程隐私门、标准构建与回滚演练；今天 P0-03 已做真机且有失败，不是“完全未真机”。Edge 仍旧镜像，watermark 关闭帧修复只在代码/本地通过（本轮定向重跑 3/3 PASS），尚未发布；既有 Trivy 的 Go toolchain 问题需按扫描证据确认修复版本（此前要求 >=1.26.6）、重建复扫并独立最小切片验收。
- 禁止搭车：不启用 DuplexModel、`expressive=True`、`user_turn_limit`、TurnPhase 生产副作用；本仓显式 `auto_gain_control=True` 且未配 NC，#7064 默认值变化不直接改变现链路，也不授权改声学增益。入口：`pyproject.toml`、`uv.lock`、`session_entrypoint.py`、`infra/Dockerfile.*`。
- 附带修复（与 LiveKit 无关，2026-09-15）：全仓 Go 测试在 `1cf8dec` 基线上就有一个失败——`TestDeviceWSSApproximateWatermarkCannotClaimExactReceipt`（已在干净基线 worktree 复现，非本轮引入）。保护逻辑本身正确（approximate 设备的回执不会被提升成 exact），但拒绝路径只排队 `session.error` 就返回，读循环返回后 lane 被拆、诊断消息可能来不及写出，设备只见裸 `1006`，测试因此偶发失败。修法：该路径补显式关闭帧 `4002 / playback_watermark_precision_mismatch`（与其它被拒控制帧同一个不可重试会话失败码），测试改为断言确定性的关闭帧并注明 `session.error` 仍是 best-effort。**未处理**：与其它拒绝路径共享的「读循环返回即拆 lane、排队控制消息可能丢」时序问题，需单独跟踪。

### [ ] P1-02 ASR 分清消费方，先让救援 sidecar 可复现

- 依赖：P0-01 的运行清单；对应 R-20260831-02、R-20260910-01。不能将 DashScope 的模型服务版本当成 FunASR Python 包版本。
- 工作：只读确认生产 sidecar 的启动脚本、Dockerfile、包清单、模型/词表摘要、基础镜像、CPU/GPU 与输入语言；核对是否真的使用仓内 `scripts/run_sensevoice_asr.py`。将实际构建来源与精确依赖纳入仓库，保留独立镜像，不向主 Agent 添加无用 `funasr`。
- 条件分支：若生产确实使用 FunASR 且低于 1.4.15，再做同数据集旧/新镜像 A/B；若使用 sherpa-onnx，则核其自身版本与修复，关闭「升 FunASR 能修此 sidecar」的错误假设。上游 NumPy 2 测试不代表所有 Torch/模型组合兼容，也不要求主环境改 NumPy。
- 完成条件：从仓内输入可重建候选；真实 PCM 契约、中文短句、长段/尾字、静音/低 RMS/削波、并发与失败降级均验证；分别报告空转写 vendor_error/vendor_silent/gating/low_rms、救援耗时及 2.5s 超时行为。无回归且有实际收益才另行切流；没有相关升级可记「不适用」并关闭版本子项。
- 入口：`funasr_stt.py`、`funasr_empty_accounting.py`、`providers/sensevoice.py`、`scripts/run_sensevoice_asr.py`、`test_funasr_protocol.py`、`test_funasr_rescue.py`。不改设备 VAD 去掩盖 ASR 问题。

### [ ] P1-03 按使用人切人格/音色：补控制入口与真实切换

- 依赖：P0-01/02；完整人格/音色闭环依赖 P0-04 的身份安全验收。**最小“设置/手动确认使用人及年龄资料”入口切片先供 P0-04 使用，不等待 P0-04 全项完成**，避免循环依赖。对应 R-20260911-05；已有多主体/分配/自定义人格/音色归属后端不重建。
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
- **进度（2026-09-15）：根因定位完成，未改检索代码。** 在候选锁上重跑 16 例固定集，恰好 3 例 `r5=0.00`、其余 13 例 `r5=1.00`（与 `recall@5=0.8125=13/16` 吻合，无隐藏第 4 个失败）。三条根因各不相同，逐条可复现，证据与建议顺序见 `outputs/acceptance/run-20260915-p0-04-student-safety-loop/memory-eval/P1-06-diagnosis.md`：
  - `paraphrase-food-preference`：记忆已正确抽取且 review 后为 `confirmed`，但查询 `点菜时有哪些东西要帮我避开？` 经 `lexical_query_terms` 得到的 n-gram 与记忆正文**零词面重叠** → `search`/`context` 都是空。这是**检索缺语义桥**，不是抽取失败；修法位置是 `recall_planner.py` 已有的封闭词表扩展模式（`_CHILD_QUERY_MARKERS`/`_CHILD_LEXEMES` 同构），不需要引入向量库。
  - `person-alias-mother`：`people` 已抽到 `('李梅','mother',('妈妈','母亲','李梅'))`、catalog 也有 `kind=person` 条目，但 `家里人也叫她阿梅` 的别名 **`阿梅` 完全没抽到**，所以查询接不上。附带：`recall_planner._entities()` 只接受 `status=='confirmed'`，而 review 只提升 claim、**不提升 person 条目**。修法位置是 `memory_extractor.py` 的 `_PERSON` 别名句式。
  - `repeated-episode-campus-startup`：两段证据没有被合并成跨会话 episode，而数据集判据要求 `kind=episode` + `match_all=["校园外卖","创业"]` + **两条** `source_event_ids`。当前 catalog 每条证据一个 document，所以**该判据当前不可满足**；不能靠改词表解决，要么做跨会话 episode 合并（新写入语义），要么改数据集判据——**需先明确取舍，不在实现里绕过**。
  - 另记：`MemoryCatalog.context()` = `_search(confirmed_only=True)`，`search(include_candidates=True)` 才看得到 `candidate`；三个失败例在 context 模式都是空。这是**故意的隐私/质量门**，诊断时不要放宽它。
- 建议顺序：① 先定「确认证据事件的 review 是否应一并提升由该事件抽出的 person/episode/knowledge」；② 再定跨会话 episode 合并是否要做，不做就改判据并记账；③ 前两项定了再加「忌口/避开」封闭词表扩展并补未见改写与反例。
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
