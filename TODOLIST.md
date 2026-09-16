# Memoria 优先级执行清单

更新时间：2026-09-15。用途：回答「下一步做什么」，并作为后续唯一的优先级执行队列。研究依据归 `RESEARCH.md`；运行版本、运维步骤、回滚和现场证据归 `HANDOFF.md`，不在这里复制第二份运行台账。

## 结论与执行方式

先让当前语音链路可稳定验收、运行状态可信、数据可恢复，再升级 LiveKit；产品增量优先完成「指定使用人 → 对应人格/音色 → 机器人实际生效」和学生安全闭环。暂不换 ASR/记忆架构，也不把云侧 DuplexModel 当成设备全双工能力。

- 按 P0 → P1 → P2、同级从上到下推进；每次先做最高优先级中未阻塞的一小项。设备/生产窗口受阻时，可并行做 P1-01 的本地兼容性验证、P1-06 的离线召回，不必等全部硬件矩阵通过才写代码。
- `[ ]` 表示未完成；进行中或阻塞写在条目下，注明已完成层级、剩余条件和下一动作。只有本项所有完成条件满足才改 `[x]`，不能把 `code / wired / enabled / verified` 混为一谈。
- 完成时附日期、提交与测试/证据位置；发布和设备事实先更新 `HANDOFF.md`，研究结论同步 `RESEARCH.md`。已完成条目可直接删去，但有效运行证据和未完成子项不能随之丢失；稳定 ID 不复用。
- 清单最初于 2026-09-14 建立；后续实施按各项的日期与证据更新。清单不是持续运行的授权；每次任务仍核对用户授权、脏工作树和真实有效配置。
- 依赖版本是 2026-09-14 的核验结果，实施前重新查官方发布与兼容约束；不按版本号大小批量追新。

## 已知基线：不要重复开发，也不要外推验收

- Agent 最新修复已冻结并推送为 `1cf8dec`，2026-09-15 21:00:12 CST 仅 Agent/Bridge 启动新镜像，21:00:38 切流检查通过。板上仍是 `d1ad38f` 对应的 `0025` 计量候选（18:51 app-only 刷入/回读/启动），本轮未刷固件。**19:05–19:06 新固件短播放计量 5/5 与听感通过、19:07 捕获正常收尾**属于前版 Agent，不能继承为新发布的设备验收。9 月 14 日的 **14/14 不是 T1–T14**；`direct_real_device_verified`、`full_duplex_verified` 仍为 false。
- 最新 readiness 观测 **2026-09-15 21:02:33 CST**：回环与公网 8443 均 200 ready、core 12/12、新 Agent heartbeat ready；真实 LiveKit/provider smoke 通过。本次切流 `connected=false` 快照保留；21:57–21:58 Tingting 与 22:27–22:28 Meijia 自动外放各尝试 3 次唤醒，Mac 麦克风录到参考声音，但设备无新唤醒/WSS 会话，未播放天气问题，已停止并恢复音量。历史功能/听感 **2/3（旧固件 1 + 新固件 1，均前版 Agent）**不变，不是新版通过或整体性能 2/3；查询时延、新 VAD/临近静默专项与 >45s 长播仍待验。
- 最近设备签名策略 `allowed_barge_in=["button","keyword"]` 未放行 voice。当前固件抑制未授权的播放期 `vad.start` 是修复，不是要删除的障碍；语音打断另走 P1-07。
- person→persona 分配、不可变自定义人格、persona→voice 归属、subject-aware RuntimeProfile 和失效通知均已有代码；音频样本真实校验、训练进度与 2 秒轮询也已有代码。剩余以线上版本映射、入口和端到端验收为主。
- RecallPlanner 已有封闭 query rewrite，现有 VAD 期预取与事实/人格分流也已接线。9 月 14 日本地重跑 16 例：`recall@5=0.8125`、`nDCG@10≈0.734`，跨会话/转述追问/安慰三项均为 1.0；不能再列成从零建设记忆系统。

## 组件决策（当前值来自锁文件，上游值来自官方包元数据）

**2026-09-15 更新**：下表前三行的候选**已写入本地 `uv.lock`/`pyproject.toml`**（P1-01 本地子项），但**尚未构建镜像、未切流**。生产当前值仍是 `1.6.10` 系列；下表的「当前」列指本仓锁文件的**上一个**发布状态。

| 组件 | 当前 → 候选 | 本轮判断 |
| --- | --- | --- |
| `livekit-agents`、`livekit-plugins-openai`、`livekit-plugins-silero` | 三者 `1.6.10` → 三者 `1.8.2`（9 月 15 日发布） | P1-01，同一依赖变更评估；读完中间版本迁移说明，不需要先部署 1.8.0。**本地候选已完成并全绿**，见 P1-01 |
| RTC `livekit` / `livekit-api` | `1.1.14 / 1.2.0` → `1.1.18 / 1.2.1` | 随上一项更新锁；Agents 1.8.x 精确要求 RTC 1.1.18，API 1.2.1 还要求 `livekit-protocol>=1.1.25`。**已落锁**：protocol `1.1.22 → 1.1.26`，并在 `pyproject.toml` 新增显式 `livekit-protocol>=1.1.25,<2` |
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

### [ ] P0-03 收口当前语音缺陷，建立升级前对照基线（真机复测持续收口）

- **最新状态（2026-09-15，以下历史过程以此为准）**：
  - **Agent 最新已发布**：`code=committed_1cf8dec`；`wired=existing_provider_and_python_lifecycle`；`enabled=true_20260915T210012CST`；`verified=release_ci_provider_smoke_pass_device_pending_auto_audio_wake_blocked`。新增旧 ASR 候选隔离、天气地名候选上限与共享查询 deadline；此前会话/待机、多日天气/续问、prepare retry 与重连清理修复继续保留。历史功能两轮为前版 Agent 上的旧固件 1 + 新固件 1，不是最新发布已过；当前/回滚及验收边界见 `HANDOFF.md`。
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
  - [ ] **查询延迟收口（修复已发布，真机性能待验）**：旧 epoch 1955 的 final 10 字被宽时间窗拼成 commit 42 字，触发 **21** 次 geocode、查询 **7930ms**、ACK→正文 **5.028s**（设备同钟 5.049s）；旧片段的声学来源仍未知。新 provider smoke 耗时 **4589/2857/2406ms**不是 ACK→正文，也不代表 1.5s 门已过。当前只有首 ACK，不恢复历史第二提示、不放宽防重复门、不延长静默或加预缓冲。自动代测两音色各 3 次均未唤醒，未建新会话，保留待验。
  - [x] **用户离场后的有界自动代测尝试与收尾（2026-09-15）**：Mac 外放/麦克风参考匹配自测通过，Tingting 与 Meijia 各 3 次唤醒均无设备 wake，未播放问题，正常停止。输出恢复 31%/muted=true、输入仍 82%，录音/串口无残留；只完成测试尝试，不完成真机验收。录音与日志时轴不一致，拒绝估计精确 gap；机器分析不计人工轮数、不证明三天完整或无断续。证据 `outputs/acceptance/run-20260915-auto-audio-{retry3,meijia}/`；原始 `results.json` 不改，重算结果单列 `audio-analysis.json`。工具仍在 ignored 的 `outputs/design/auto-audio-20260915/`，未随生产代码入库。
    - 用户追加提高音量要求后，Meijia **90%** 对照亦完成（22:36–22:37）：三次唤醒词均被 Mac 麦克风录到，但设备仍 idle、无会话；没有继续提问，不算新版真机通过。自测录音峰值接近满幅，不能用继续加音量替代声学原因定位；已再次恢复 31%/muted=true、输入 82% 并停止全部采集。证据 `outputs/acceptance/run-20260915-auto-audio-meijia-volume90/`。
  - [ ] 三轮功能/听感收口（**历史剩余 1 轮，新发布必须取得自身证据**）：沿用「未来三天 → 播完续问 → 播后告别」，按 session 记录问题、请求天数、VAD/ASR/受理/超时/播放回执与听感。前两轮第二问均没有新 VAD/admission，只验证 ASR-final 续问；下一轮应覆盖新 VAD 接管和接近静默期限的续问。当前用户不在场、合成音未唤醒，不重复索要现场配合或盲试；有可用声学条件后再继续。功能轮数与查询时延、竞态专项分别判定，不以历史 2/3 外推新版或后两项通过。
  - 9 月 15 日现场证据：`outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-2/`，epoch 1953。播完 12:57:21.672、VAD start 12:57:27.566、静默关闭 12:57:29.101 CST；旧日志不足以证明具体受理/grace 分支。同轮 ASR overlap/recovery 仍开放，不回退防重复门；详见 `HANDOFF.md`「2026-09-15 多日天气与续问关停修复」。不要与下方 9 月 14 日的 `session-2` 混读。
  - **新固件计量候选**：`code=true / wired=true / enabled=true / verified=true_short_playback_only`；overlay `24531273…b3`、app `7d95c8a1…b65`、ELF `da6ebdd1…69f2`，完整哈希、构建、刷写收据与本轮独立会话审计见 `HANDOFF.md`。**紧邻回滚**为刷写前读出的旧 app `dfba3d61…`，旧板首轮听感通过但计量未覆盖真实播放；原 `run-20260915-p0-03-firmware-metering/postflash.json` 仅绑定旧板。
  - [ ] 长播验收：短播放计量/真实捕获前置已通过，但本轮最长正文仅 **17.04s**；后续 USB/串口稳定的窗口做完整 >45 秒天气/长回复，保留设备 `supply / prestart / boundary / close_dropped / outside`、Bridge pacing、delivery ledger 和操作员完整听感确认。记录计量日志对实际收包/播放时序的影响；`pre-roll` 未实现，不实现 `0026`。桥侧比率、软件队列与 `actual_heard/playback_ended` 均不能单独当作用户完整听完。

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
  - **`0025` 计量实现（已刷入，短播放已验）**：补丁、header、宿主断言和 reset/fence/late-token 防污染逻辑已完成；仅测 decoded playout queue consumer 的 software queue wait，分类为 `supply / prestart / boundary / close_dropped / outside`，并单独记录 exact TX-EOF completion；不宣称 I2S/DMA underrun 或由计量推断听感。clean overlay apply、固件宿主测试和 clean build 已通过；当前候选已有自己的回读/启动及五代短播放计量证据，未继承下两项旧板结论，长播仍待验。
  - **历史刷机收据（2026-09-14 19:05 CST）**：旧版 `0025` 简单计量候选 app-only 写入 `0x20000..0x340fff`，整槽回读、erase 范围外、identity、非 app 分区及 ota_1/assets 校验均通过；板载 `release_head=fa54d7d` / app 3,277,856 bytes / SHA `f58f48a4…` / ELF `16d981b3…`。收据明确 `boot_verified=false`、`real_device_conversation_verified=false`，且与当前 `PlaybackSupplyMeter` 分类实现不一致，所以只能记为旧候选已写入，不能记为当前实现已启用或已验收。
  - **旧板候选已刷入并启动验证（2026-09-15 11:19–11:22 CST）**：app-only 只写 `0x20000..0x340fff`；app 全槽回读逐字节一致，erase 范围外、identity、bootloader、partition、NVS、otadata、phy-init、ota_1/assets 均未变；启动证据通过。绑定为 HEAD `65257e0e1285a3126e484ad22c308315c971caad`、upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`、overlay hash `97fc64f28dcd95c365a99176d2ed26a749beb20c3e7f0179283c26484e7559e8`、app SHA `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`、ELF SHA `efe1b241c3d6b4126e3b2e9d57ab4bfbe76bbd6a4844b063c0a9aa8d160c58625`；收据与回滚件在 `outputs/acceptance/run-20260915-p0-03-firmware-metering/`。此为既有旧版证据，本轮新候选回读与启动见上项。
  - **旧板短捕获（2026-09-15 11:34–11:39 CST，计量无有效播放覆盖）**：设备 `vad_end -> first_received=0.517s`；generation 1 的 `supply/prestart/boundary/close_dropped/outside` 原始记录全零，但统计窗口提前关闭，不能认作有效零等待。Bridge `frames=120/audio_ms=2400/wall_ms=2413/max_gap_ms=69/after_pacer_send_ratio=0.99` 只说明发送侧节奏，不证明 I2S/DMA 或听感。串口 `SerialException: read failed: [Errno 6] Device not configured` 导致 `degraded`；不计有效播放计量或完整长天气验收。证据：`outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-1/`。
  - 未跑到：告别（该段未触发，turn 2 后直接静默关闭）、待机与五表情照片、P0-04 学生安全话术。
  - [x] 历史工具缺陷已修复：`scripts/voice_session_report.py` 曾以 `(turn, generation)` 聚合，跨会话串账产生 `commit->frame=190.859s`；当前已使用完整 `DeliveryKey(session, epoch, turn, generation, tool)` 隔离，不再列为待开发项。
  - 证据 `outputs/acceptance/run-20260914-p0-03-voice/session-2/`（`report.txt`、`findings.md`）。
- 进度（2026-09-14）：**本地对照基线已建立**——`test_media_session.py` 200 passed、`test_duplex_runtime_wiring.py` + `test_utterance_router.py` 191 passed（证据 `outputs/acceptance/run-20260914-p0-03-local-baseline/`）。ACK 后空输入恢复、旧查询不回流、同问重复不重复提示、空转写处理均有既有回归并通过。
- 剩余条件：两轮功能听感、短播放计量及真实会话收尾已过；**查询延迟未过**（9 月 15 日 epoch 1955 第二问 ACK→正文 5.028s）。最后一轮人工功能复测、新 VAD/临近静默、>45s 长播及其它既定专项仍开放；用户不在场时先做有界自动录播，不能把机器录音计为第 3 轮人工听感通过。
- 原因：旧 pending ASR 范围拼接与逐字地名候选串行放大已可复现，旧片段来自回声/背景/幻觉中的哪一种仍未知；当前修复还缺空输入恢复、新 VAD 接管与临近静默的专项真机时序。不把用户无断续反馈当时延通过。
- 工作：先按上方「查询延迟收口」修复与验证，再补「ACK 后空输入 → 有效正文恢复」、同问重复提交、答案完成后再问同题、查询南京时改问北京、慢查询等待行为；沿用既有 provider/Router/Ledger/fence。记录提问结束、ACK 起止、正文首帧、终端回执，不能只统计首 token。
- 完成条件：有效答案不静默丢失、ACK 不重复/不截成残句、旧查询结果不回流；说完到开口及提示间隙无 >1.5s 纯静音；长天气 >45s 与长回复可完整听完。历史「约 2.5s 第二提示」不是当前已接线机制；若限流/总期限修复后慢查询仍越线，单独明确等待策略再实现，不能默默降低标准或重开重复提示链。预先固定场景/重复次数，逐轮给原始数据，不只报均值。
- 真机复测同时保留已通过的问候不掉线、播后告别、BOOT/触摸硬停；补待机与五表情照片。播放期语音告别不混进本项，移至 P1-07。
- 入口：`HANDOFF.md`「下一验收」「20260913-empty-input-resume 复测清单」；`test_media_session.py`、`test_duplex_runtime_wiring.py`、`test_utterance_router.py`。没有在线板卡就只完成本地复现，设备项保持未完成。

### [ ] P0-04 核实身份隔离，补齐学生安全闭环（不含家长通知发送链路）

- 进度（2026-09-14）：身份/同意门的**本地合同与 RLS 证据已取得**——真 PG（`memoria-pgv`）跑 guardian schema/语料同意栅栏、guardian 表按 guardian/minor 作用域隔离、tutor 行 subject 隔离且 RLS 生效、账号能力门、生产同意接线、多主体权限矩阵、声纹权威合同，共 **34 passed**（证据 `outputs/acceptance/run-20260914-p0-04-identity-matrix/`）。这只到合同/API 级，不等于真机或真实账号端到端。
- **进度（2026-09-15）：学生安全闭环的 HTTP 级端到端已补齐**——受控学生账号（`minor`/`under_14` + 已确认 guardian link + `minor_voice_session` 同意）触发危机：response plan 的 `direct_text` **逐字等于** `CRISIS_SUPPORT_REPLY`（即设备端会听到的文本）；`guardian_notification_outbox` 入队 1 条，家长端列表可读且 `delivery_status=pending`、`contains_transcript=false`、`contains_severity=false`；危机原文不进家长端响应、不进 evidence payload。另覆盖：同 generation 重放不重复入队（缓存路径 + 绕缓存的 store 层幂等）、新 generation 是独立事件、**成人危机不碰 outbox**、**unknown/guest 拿固定话术但不进主人私有链**、入队不可用时固定话术不被压掉、家长端列表按 guardian 作用域隔离且学生本人 403、**撤销同意立即生效**（新会话 403 且旧语音会话被终止）、**邀请家人不把学生提升为 owner**（`digital_self`/`speaker_enrollment` 继续 `minor_forbidden`）。真 PG 域合计 **45 passed / 0 skipped**（证据 `outputs/acceptance/run-20260915-p0-04-student-safety-loop/`，命令与逐条说明见 `report.md`）。仍**不是**真机或真实账号端到端。
- 范围决定（2026-09-14，用户）：**本阶段不做微信订阅号/家长通知发送链路**（验证阶段）。因此本项不含发送 worker、重试、模板与凭据申请；通知侧只验「入队 + 家长端列表可读 + 明确记账当前无推送通道」，不把入队当作家长已收到。重新评估触发条件：开始对真实家庭或学生提供服务前。
- 剩余待验：学生危机的**设备端固定话术**（借用 P0-03 真机时段的场景 10）与受控学生账号下的能力门端到端；guest/uncertain 不进主人私有链、撤销立即生效等已由合同/RLS/HTTP 级覆盖，但真实账号与真机路径仍需一次演练。
- 原因：账号能力门、声纹与固定危机话术已有实现；家长通知目前只有 outbox 入队与列表读取，尚无发送 worker/投递状态更新，不能把入队当作家长已收到。学生阶段仍缺实际端到端闭环。
- 工作：用受控测试账号/脚本覆盖 adult、minor、unknown/未声明类别，owner、guest、uncertain，以及 guardian 同意/撤销；核查历史、私人记忆、工具、自定义音色、删除/导出、切主体和告别的权限。模拟学生危机场景，不要求真实未成年人参与高风险试验。
- 本阶段实现边界：不新增发送消费者、幂等重试与模板凭据；只在文档与状态里显式记录「outbox 有入队、无投递通道」，避免被误读为家长已收到。
- 完成条件：无同意拒绝、撤销立即生效；guest/uncertain 不进入主人私有链；切使用人或邀请家人不提升 owner 身份；**真实设备固定安全话术通过**，且 outbox 入队与家长端列表可读、无投递通道这一边界被显式记账（不要求微信回执）。涉及 schema/RLS/授权修改时带 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响域，跳过不算通过。
- 入口：`services/control_api/app/account_gate.py`、`routes/guardian.py`、`services/guardian/crisis.py`、`services/agent/src/generation_output_policy.py`、`services/speaker/authority.py` 及对应测试。技术验收不代替法律合规结论。

## P1：受控升级与第一阶段产品闭环

### [ ] P1-01 LiveKit 1.8.x 同组升级：先兼容性，再独立发布

- 价值：跟进连接池、会话关闭、播放计量、资源清理和 telemetry 修复；不是已证明能解决当前 ASR/AEC 故障。对应研究 R-20260907-01（吸收 R-20260911-01）。
- **进度（2026-09-15）：本地子项全部完成，候选就绪但未发布。** 锁候选精确变更 7 项（`livekit-agents`/`plugins-openai`/`plugins-silero` 1.6.10→1.8.2，RTC 1.1.14→1.1.18，API 1.2.0→1.2.1，protocol 1.1.22→1.1.26，local-inference 0.2.6→0.2.7），`blingfire` 保持 1.1.0；无其他漂移，未用 `--no-deps`。门禁：`uv lock --check`、frozen 安装一致、靶向 **618 passed**、全仓 **5002 passed / 0 failed / 3 skipped（合计 5005，真 PG）**、ruff/mypy --strict(433 文件)/模块预算 PASS、Media Edge `go build/vet/test` 通过。逐条证据与边界见 `outputs/acceptance/run-20260915-p1-01-livekit-181/report.md`。**候选上线与回滚验收仍未做**（需生产/设备窗口），所以本项保持未完成。
- [x] 本地候选：三件套同步升至 1.8.2，RTC/API 与 protocol 按组件表更新，`livekit-local-inference` 从 0.2.6 更新到满足 `>=0.2.7`；按真实约束重锁 `uv.lock`，保留 Python 3.12。禁止 `--no-deps` 绕过冲突，不混入模型、固件或 server 升级。
  - 完成（2026-09-15）：`pyproject.toml` 三件套钉 `==1.8.2`，`livekit-api` 由 `>=1.1.1,<2` 收紧为 `>=1.2.1,<2`，新增显式 `livekit-protocol>=1.1.25,<2`（API 1.2.1 要求 `>=1.1.25`，不能保留原锁 1.1.22）。`uv lock` 只动这 7 项。
- [x] 行为兼容：针对真实 SDK 验证 `TurnHandlingOptions`、STT/TTS adapter、RoomIO 与半内部符号；`TypedDict` 会静默接收未知键，必须验证配置被实际消费，不能只测构造不抛异常。LiveKit 侧保留 dispatch metadata→`controlled_half_duplex_session` 的策略，设备 Voice Core 侧保留 `audio_mode` 派生策略；兼容半双工路径禁打断，默认 preemptive 关闭，完整 generation fence 不变。
  - 完成（2026-09-15）：新增 `services/agent/tests/unit/test_livekit_candidate_compat.py`。逐键核对 `TurnHandlingOptions`/`EndpointingOptions`/`InterruptionOptions`/`PreemptiveGenerationOptions` **零丢弃**；`AgentSession._opts.turn_handling` 回读值与配置逐项一致（`{"version":"v1-mini"}` 物化为 `TurnDetector(model="turn-detector-v1-mini")` 已核）；半双工 `interruptions_enabled=False` 在真实 SDK 读回 `interruption.enabled=False` 且 preemptive 关闭；1.8.x 新增 `user_turn_limit` 默认 `None/None` 未生效；`aec_warmup_duration=None` 仍读回 `_aec_warmup_remaining==0.0`；`room_io` 五个 Options 签名与字段仍匹配，显式 `auto_gain_control=True` 且未配 NC 所以 #7064 默认值变化不影响现链路；`stt`/`tts`/`types` 中我们子类化或导入的符号全部仍在。记录：`build_turn_handling_config` 的 `stream_speak_while_think` 是本仓自有标志、不是 LiveKit 字段，目前无消费者读取，本轮不改行为。
- [x] 隐私/可观测性门：迁移 #7104 的 message event→attributes、span 与 token 字段；显式关闭内容采集 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0`，禁 PII 外发 `LIVEKIT_TELEMETRY_ALLOW_PII=0`（或等价 API）。这是候选 SDK 支持的配置，不是本仓已经接线；须核导入/初始化时序与部署 env，兼顾 SDK 和本仓 `observability/media_otel.py` 的导出路径。用假姓名/私有文本/tool 参数作 canary，检查实际 exporter、日志和报表，证明不泄漏且计量不重复，不能仅凭 env 存在判绿。上游默认采集内容、默认允许 PII，不能假设「新增 redaction」会自动保护本项目。
  - 完成（2026-09-15）：新增 `services/agent/tests/unit/test_livekit_candidate_privacy.py`，用真实 `TracerProvider` + `InMemorySpanExporter` 与假姓名/私有正文/tool 参数 canary。**反证用例**先证明不显式关闭时 canary 确实到达 exporter；显式 `allow_pii=False` 时五处内容属性都不出现，而非内容计量（`gen_ai.tool.name`、`gen_ai.operation.name`）仍到达；`LIVEKIT_TELEMETRY_ALLOW_PII=0` 同样生效；`CAPTURE_MESSAGE_CONTENT=0` 时内容属性根本不写入。SDK 的 env 解析规则被钉住（**未设置/空串=采集**，只有 `0/false/no/off` 关闭，且在 SDK 导入期求值）。`pii.is_pii_attribute` 的分类名被钉住。本仓 `MediaOtelBridge` span 只挂低基数 fence 且明确丢弃 `session_id`/`device_id`。
  - 本轮接线（不只是 SDK 支持的配置）：`services/agent/src/main.py` 新增 `_apply_telemetry_privacy_defaults()`，在 `main()` 第一步执行（早于 `load_settings` 与 worker 启动），空值/未设置补 `0`、显式非空值保留；`docker-compose.production.yml` 的 `agent` 与 `voice-core-media-bridge` 都显式写上这两项；`.env.example` 与 `infra/memoria.env.production.example` 补带注释的 `=0`。**边界**：未做真实 OTLP 端到端导出验证，未核 LiveKit Cloud dashboard 设置；仓库里 `PII_REDACTION_ENABLED=true` 只被 `split_production_env.py` 归类、无任何 Python 代码消费，也不是 SDK redaction 开关，本轮不为它新增行为。
- [x] 完整构建与回归：`uv lock --check`、frozen 安装、ruff/module budget/strict mypy、Agent 单测/集成、Control/API 与 Bridge 使用面、offline E2E；所有消费共享锁的镜像分别验证，不只测开发 venv。Agent/Bridge 必须同一镜像；依赖锁变化不走 agent-only source overlay 快速通道。
  - 完成（本地，2026-09-15）：靶向 618 passed、全仓 5005 passed / 0 failed / 3 skipped（真 PG）、ruff 全绿、`mypy services --strict` 433 文件无问题、模块预算 PASS 未放宽、Media Edge Go `build/vet/test` 通过。**镜像级验证仍未做**：本轮只验证开发 venv 与 Go 使用面，没有构建候选镜像、也没有分别验证消费共享锁的每个镜像。
- [ ] 候选上线与回滚验收：依赖 P0-01/02 的发布前提和 P0-03/04 的相关基线；获准后最小切片发布，真实 provider smoke、当前设备对照与延迟复核通过，留当前+一个可运行 rollback。未获生产/设备窗口时只标本地子项完成。
  - 未开始：需要生产与设备窗口。依赖锁变化的发布**不能**走 agent-only source overlay 快速通道（`.dockerignore`/`pyproject.toml`/`uv.lock`/`infra/Dockerfile.agent` 任一被改动即 REJECT）。
- 禁止搭车：不启用 DuplexModel、`expressive=True`、`user_turn_limit`、TurnPhase 生产副作用；本仓显式 `auto_gain_control=True` 且未配 NC，#7064 默认值变化不直接改变现链路，也不授权改声学增益。入口：`pyproject.toml`、`uv.lock`、`session_entrypoint.py`、`infra/Dockerfile.*`。
- 附带修复（与 LiveKit 无关，2026-09-15）：全仓 Go 测试在 `1cf8dec` 基线上就有一个失败——`TestDeviceWSSApproximateWatermarkCannotClaimExactReceipt`（已在干净基线 worktree 复现，非本轮引入）。保护逻辑本身正确（approximate 设备的回执不会被提升成 exact），但拒绝路径只排队 `session.error` 就返回，读循环返回后 lane 被拆、诊断消息可能来不及写出，设备只见裸 `1006`，测试因此偶发失败。修法：该路径补显式关闭帧 `4002 / playback_watermark_precision_mismatch`（与其它被拒控制帧同一个不可重试会话失败码），测试改为断言确定性的关闭帧并注明 `session.error` 仍是 best-effort。**未处理**：与其它拒绝路径共享的「读循环返回即拆 lane、排队控制消息可能丢」时序问题，需单独跟踪。

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
