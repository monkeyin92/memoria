# Memoria 优先级执行清单

更新于 2026-10-06｜**本文件只留未关闭事项**：做完的条目直接删除；收据在 `HANDOFF.md` 与 `docs/HANDOFF-archive-*.md`，真机轮次的数字在 `docs/acceptance/*/findings.md`，被删掉的全文在 git 历史里。条目编号被代码注释与文档引用，已完成而删掉的编号见文末「编号索引」。线上现状（当前发布、固件、小程序体验版、主机资源）以 `HANDOFF.md` 开头「当前生产快照」为准，这里只放不得越界宣称的边界。

## 当前边界（不得越界宣称）

```yaml
enabled_release: 20261006-speaking-flush-v1  # 整栈，源 e6fb10f3（PR #189 的分支头，合并提交 c1357a1f），2026-10-06 09:53 上线；media-edge 是组件发布 20261002-late-progress-v1（源 f3fe8742）；LLM deepseek（联网查询 qwen-plus），ASR fun-asr-realtime，TTS Doubao；回滚：整栈 → 20261005-subject-candidates-v1（新机上有 *:rollback-20261006-speaking-flush-v1-pre 镜像与保留的发布树，`rollback` 这一步在新机上没演练过）；media-edge 的回滚目标 20260930-late-receipt-v1 没迁到新机
production_host: 110.42.235.198（2026-10-05 01:37 起，原 pocketSparks 生产机，与 hr-tracker / MySQL / aginice.cn 共用；ssh 别名 memoria-prod；Control API 在 127.0.0.1:18791；4 vCPU、内存 3.7 GiB、盘 40 GB；无持续数据库备份，整栈回滚深度 1）；旧机 122.51.108.140（别名 memoria-prod-old）只剩 8443 中继，2026-10-06 到期
public_domain: aginice.cn（2026-10-05 起，aigcnice.com 弃用、不做桥；证书 2027-01-02 到期，最迟 2026-12-19 换，见「定期运维」；小程序体验版、固件与机器人身份都已指向它）
control_api_release_lane: 整栈走仓库版 `scripts/release_ops.sh`（步骤与门禁见 `docs/runbooks/release-rollback.md`）；新机上装在 `/root/memoria-release/release-ops.sh`（装法：scp 新版、旧的备份为 `.pre-<tag>`、核对 sha256），现装的是 PREV = `20261005-subject-candidates-v1` 的版本（10-06 发布前装上，sha256 与仓库一致）；这次发布成功后，仓库里的 PREV 还要前移到 `20261006-speaking-flush-v1` / `e6fb10f3a6ada02e99dbd3362691be074e298015`，下一次整栈发布前再装到主机；Control API 在新机绑 18791（hr-tracker 占着 8791），compose、`refresh_readiness.sh`、`release_ops.sh` 用 `MEMORIA_CONTROL_API_PORT`，`verify-load` 的预检端口是 28791 / 28891；当前发布树 `releases/20261006-speaking-flush-v1` 是仓库原样加 `.env`，`releases/20261004-first-warm-v1` 里是手工改的 18791，不要删它（media-edge 的 compose 链还指着它）；新机上的整栈发布可拿 `incoming/<上一个 tag>` 作种子做增量，依赖文件（pyproject / uv.lock）变化时增量与 overlay 发布会被脚本拒绝，须本机全量 linux/amd64 构建；control-api 的组件链支持已从 `release_ops.sh` 移除，此后再做 control-api 单组件发布须先把链支持补回；agent 组件快车道（`deploy_agent_component.sh`）在整栈发布后的第一次会被「runtime base 必须独立于线上镜像」拒绝（2026-10-01 实测，切流前被拒，服务器无变化），需整栈发布或与线上镜像不同的同 lock 基座
memory_candidate_visibility: code=main 0059368 / enabled=true（随整栈上线）/ verified=SQLite/HTTP/主体隔离/评测适配器回归；四份 2026-09-23 评测收据为上线前 parent_baseline（固定集 recall@5/10=0.857、未见集 0.4、双泄漏 0），真实 PG candidate 行为与线上带鉴权读口未单独取证
direct_real_device_verified: false
full_duplex_verified: false
student_safety_loop_verified: false
deletion_scope: code=已提交 `d2318e4`（CI `35501188784` success：PG 全 saga 用例在远端实跑）/ enabled=未启用 / verified=PG 全 saga 本地已验（归档/声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）；真实 MinIO 仍未验（本地 Docker MinIO 对象写入不可用）、真实 provider 未验（需密钥与授权）、备份「恢复后再删除」无实现、subject 键存储不在 saga；已删除的 subject 重放由 `scripts/replay_subject_deletions.py` 负责
conversation_archive: code=#151（成人）、#152（孩子的 Policy 口径与闸门）、#153（设备信任分档）均已合并 / wired=Agent 归档闸门认已签名 profile 的 `memory_recall_private`；Policy 按能力分档设备信任 / enabled=已随整栈 `20261001-device-archive-v1` 上线，`MEMORIA_BOUND_DEVICE_TRUST_ENABLED=true` 于 2026-10-01 00:30 打开（env 备份 `/etc/memoria-control-api.env.pre-bound-trust-20261001`，关闭 = 删该行并重建 control-api） / verified=本地 PG 链路与变异测试，线上只读核对（`action_device_lock_trust` 返回 `device_bound=true`；开关打开后 00:33–01:32 有 8 条 `owner` 的 `speech.utterance_finalized` 入库，`history_eligible` / `owner_projection_eligible` 均为真、`retained`，`subject_id` 是孩子的 person id）；生产自 2026-08-08 起无设备对话入库，原因是设备无 attestation 而 `untrusted`（`device_fleet_attestations` 0 行，根因与证据见 `docs/HANDOFF-archive-0924-1003.md` 2026-09-30 两节）。尚未验证：编译 / 向量写入 / 隔天召回 / 家长小结的下游结果，以及家长账号看不到孩子原文的回顾页表现（见 N-4）
```

这里的“通过”仅代表本地、SQLite 或临时 PostgreSQL 证据，不等于远端 CI、生产、设备或真实机器人对话验收。

## 下一步与执行边界

1. **刚发布的一批（PR #189，2026-10-06 09:53 上线，收据在 `HANDOFF.md`「2026-10-06 整栈发布」）**：N-8 卡「说话」修复已在线上；WAL 归档已关（P1-08）；M-1 / M-2 / M-6 ①②③⑥ 的固件改动已在 main，固件版本号不动（build 24），固件不在整栈发布里，刷 bench 镜像与真机验收要你点头。还剩：仓库 `scripts/release_ops.sh` 的 PREV 前移到 `20261006-speaking-flush-v1`（连同 `scripts/tests/test_release_ops_script.py` 里的断言），随下一个代码 PR 走；旧域名与旧机残留清理见「域名切换与新机收尾」。
2. **真机窗口（你推动；每一轮真机语音、每一次 USB 刷机都要你点头）**：①孩子真实声音的取证一轮（你 10-06 说现在不做）；②N-8 修复的复核（第十八轮的做法，音量 50 须先问）；③M 系列：先刷 bench 镜像，用 `status` 量帧率与渲染耗时、用 `snap` 取屏，再刷回产品镜像；④N-5 ≥ 30 分钟长稳；⑤之后排一个 P0 设备验收窗口：P1-11 孩子绑定、P1-03 孩子人格隔天生效、P2-04 终止性拒绝不再续连、P0-03 的 TLS / WSS 重连与剩余设备矩阵、P0-04 安全闭环。不得把核心通过扩大为完整 P0-03 或全双工通过。
3. **不动线上就能直接做的代码项**：P0-04 剩余部分、P1-04 自定义声音闭环、P2-06 回放评测、P2-04 Python 侧进程退出注入、M-3、M-6 ④⑤。
4. **要你定的事（汇总；细节与选项在各自条目里，不挡以上步骤）**：
   - 线上与运维：固件 OTA 指针留 21 还是指回 8（「2026-09-28 收尾待办」）；新机每夜 `pg_dump` 做不做（P1-08，默认不做）；是否轮换数据库角色口令；P1-09 readiness 逾期的告警渠道；P1-02 两项线上调整；`MEMORIA_GUARDIAN_PUSH_ENABLED` 与话术专业审核、未成年人人格学习口径（P0-04）。
   - 产品口径：N-1 关键字唤醒的代码与开关随不随一起删；N-4 监护人看不看孩子原文、周小结的数据从哪来；N-6 听感、危机话术、天气城市；N-9 孩子闭集词表、置信度余量、规划器；N-10 `FUNASR_VOCABULARY_ID`；N-8 的两种候选修法与安静房间假回合阈值；N-13 / N-14 的 2.2 s 阈值、0.8–0.9 s 宽限中间值、影子模式的数据去向、「前半句先提交、后半句被丢」要不要单独立项；P2-03 已知缺口接不接受；P2-07 第 2 / 3 项；P2-08 里的 `self_model` 版本保护、PCM tap 留存、控制词与静默计时。
   - 设计取舍：M-5 预读（pre-roll）做不做；M-8 角色包存放选 A 还是 B；stash 存档的「本会话说过的地点也行」要不要作为产品改动重新评估。
5. 边界：生产切流、回滚演练和制品清理须另获授权（2026-10-01 已获授权打开设备信任开关并清理旧制品，那次授权不外延）；`MEMORIA_BOUND_DEVICE_TRUST_ENABLED` 的开 / 关仍属改变安全口径的动作，变更须用户授权；删除、重启、定时任务、自动备份和异地副本不在当前授权内；设备功能通过不等于学生安全或全双工通过。

## 域名切换与新机收尾（2026-10-05 迁移与换域名之后还开着的）

收据与细节见 [HANDOFF「2026-10-05 域名切换到 aginice.cn」「2026-10-05 服务迁移到 110.42.235.198」](HANDOFF.md)。

- [ ] **本批主机维护窗口**（你 10-06 说「现在一起做了」；无活跃设备会话时做，步骤逐个确认）：新机上整栈 `release-ops.sh rollback` 演练（`verify-load`、`freeze`、`cutover`、`finish` 在 2026-10-05 实跑过，`rollback` 没演练过；会把三个角色切回上一栈再切回来，两次各约半分钟断流）；WAL 归档关掉（见下一条）；换上仓库版 `release_ops.sh`（PREV 已前移）；清理旧域名站点 / 证书 / 备份与 `ALLOWED_ORIGINS` 里的旧机 IP（下一条）。
- [ ] **新机没有持续数据库备份（P1-08）**：WAL 归档已在 2026-10-06 09:58–10:00 关闭（`archive_mode=off`，postgres 容器重建，归档卷 39 个文件 / 654 MB 已清空，盘剩 14 GB），不再增长；现在靠发布前 `freeze` 的 dump。要不要另做每夜 `pg_dump`，你定（默认不做）；以后若要重开归档，归档卷必须是 `999:999 0700` 并先做 base backup。
- [ ] 清理旧域名与旧机残留：新机 `sites-enabled/memoria-prod`（aigcnice.com 站点）与 `/etc/nginx/ssl/aigcnice.com/`；服务器 `/root/domain-switch-20261005/`（含旧证书私钥、env 与头像原值备份，确认不再回滚后删）与旧私钥备份；`/etc/memoria-control-api.env` 的 `ALLOWED_ORIGINS` / `LIVEKIT_URL` 里的旧机 IP（只给浏览器跨域用，小程序请求不走它；只打印键名，不打印值）；核对 `outputs/` 里没有再指旧域名的脚本。
- [ ] 旧机 122.51.108.140 2026-10-06 到期（时刻不明）：到期后删掉 ssh 别名 `memoria-prod-old` 与 `~/.ssh/known_hosts` 里旧机的条目；旧机上没迁的东西（WMS、13 GB WAL、7.3 GB `incoming`、旧发布树与回滚镜像）随机器消失，按你的决定不再保留。`aigcnice.com` 不做桥（已定）：仍用旧域名的别的设备 / 旧版小程序在旧机到期后连不上，据我所知没有。
- [ ] 新机资源偏紧：内存 3.7 GiB（Memoria 栈约 1.3–1.4 GiB，可用约 1.7 GiB，swap 已用约 0.6 GiB，与 pocketSparks 的 MySQL / node 共用），4 vCPU 约比旧机慢 1.3 倍。发布后 1–2 天看 `docker stats`、`free -m` 与时延（对照第十六轮 24 轮时延场景说完 → 首帧 p50 2.92 s）；不够就扩内存（你在控制台做）。
- [ ] build 24 的时钟修复仍无证据：2026-10-05 22:56 的重绑一次过，但时钟多半是 21:32 拔插 USB 后清空的，老版本也能过；要证明得让机器人时钟比清单 `issued` 落后 5 分钟以上再绑。不紧迫，碰上再看串口（那次的记录在本机 git-ignored 的 `outputs/serial/robot-20261005-trial3-rebind.log`）。
- [ ] 你定：是否轮换数据库角色口令（一次 grep 把各角色连接串打印进了本机会话记录，库不对宿主发布端口）。
- [ ] 邻近发现（不挡路）：8443 经 stream 透传没有 PROXY protocol，9443 上的 TLS 服务器看到的客户端地址一直是 127.0.0.1（新旧机一样），按 `$binary_remote_addr` 的限流区（`memoria_media` 30 r/m 等）对所有设备共用一个键；一台机器人没有影响，设备多了要改。

## 2026-10-01 新需求里还开着的（N 系列）

用户 2026-10-01 的六点要求（唤醒方式设置、空闲熄屏、发布与刷机、≥ 30 分钟电脑对话、「回顾」没有记录、产品体验官评审说话方式）里，熄屏、发布与刷机、唤醒方式的设置已经做完并上线；下面只留还开着的部分。

### [ ] N-1 唤醒方式（功能已上线，剩真机验证与一项取舍）

后端 `wake_mode`（`button` / `keyword` / `button_or_keyword`，随显示档案下发）、固件 build 18 起读它、小程序开关都已上线。你 10-02 决定去掉关键字唤醒（改点屏），之后电脑测试一律经 USB `wake`（见「真机测试约定」）。

- [ ] 点屏唤醒没有单独的串口记录（第十轮起的测试都经 USB `wake`；点屏只有你 10-02 口头确认「空闲时黑屏、点屏唤醒在用」）：要你在机器旁点一下屏，串口出现 `screen tap wakes the device` 即可。
- [ ] 关键字唤醒的代码与小程序里「唤醒词唤醒」开关是否随「去掉关键字唤醒」一起删，没有单独立项（现在 `wake_mode` 默认仍是 `button_or_keyword`）；合成音召回 18/20、近音词（茉莉花、摩里、莫里亚等 7 个）会叫醒机器，真人孩子的召回从没测过。你定。

### [ ] N-4 小程序「回顾」（代码与手机验收已完成，剩一项数据与一项决定）

家长在回顾页看到的是孩子哪些天聊过（只有次数）与按请求现写的 AI 概括（`GET /v1/guardian/minors/{id}/days`、`POST …/days/{day}/recap`，概括不落库），不看孩子的原话。

- [ ] 监护人是否可看孩子对话原文：当前按 2026-09-24/25 的产品决定「不可」；要调整（例如仅测试期开放、或绑定时多一个勾选）需要你明确。
- [ ] 周小结（`/summary`）依赖的 `emotion_observation` / `topic.observation` 事件线上无人写入（档案里只有 `speech.utterance_finalized`、`assistant.playout_stopped` 等），所以它仍是空的；要由这两类事件补，还是改用同一份概括，待定。

### [ ] N-5 电脑对话长稳测试（≥ 30 分钟，修复上线后复测）

2026-10-01 第二、三轮（36 分钟那次在修复之前）暴露的问题都已修复上线；之后的真机轮次以针对性场景为主，修复齐了以后没有再跑一次连续 ≥ 30 分钟的混合对话。

- [ ] 要你点头的一轮：天气 / 知识 / 故事 / 闲聊、播放中打断（「停」「别说了」）、续问、长静默、重连、负面情绪（孤独、难过、被欺负、焦虑、厌学；不含危机类话术，会给家长队列写入假的危机提醒）、USB 唤醒。孩子的 profile 带 `QUIET_HOURS 04:00–07:00`（时段内唤醒只播晚安）与 `MAX_SESSION_SECONDS 1800`（单次最长 30 分钟），这两项是家长设定、我不绕过，所以不能在 07:00 前测，≥ 30 分钟要分多次会话。工具 `scripts/voice_soak.py`（场景在 `scripts/voice_soak_scenarios/`），产出写进 `docs/acceptance/run-20261001-longsoak/findings.md`。

### [ ] N-6 产品体验官评审：说话内容、语气（孩子；2026-10-04 起老人一侧不再评）

并排实验、孩子口吻提示词（`SERVICE_MODE_BLOCKS`）与 TTS 语气提示已上线，第三轮真机复核过孩子侧。未关：

- [ ] 你实听：`outputs/listening-20261001/README.md`（git-ignored，59 段音频，同一句话新旧声音对比最值得听）——孩子的音色提示是否听得出、人格「阿序」（沉稳干练）给孩子是否偏严肃；孩子的伙伴可在小程序换成桃喜或绵绵。我无法听音频，听感这一半只有你能评。
- [ ] 唤醒应答短语对孩子的语气：现在是一张表，夜里「这么晚还醒着，我在」对孩子有点像责备。
- [ ] 要你拍板或专业审核的话术（我只读了、没改）：①危机固定话术 `CRISIS_SUPPORT_REPLY`（约 95 字、偏临床）对孩子建议更短、先说「我在这儿」、把「联系一位可信的人」落到「告诉爸爸妈妈或老师」，它属于 P0-04 的「话术待专业审核」，未经审核不改；②有害请求的固定回复只有「我不知道。」，对孩子像机器坏了，可考虑「这个我不能教你哦，我们聊点别的吧」，但这会改变「不解释、不扩写」的安全口径。
- [ ] 提示词微调 ④：长颈鹿等常识题不必说「不确定」（孩子块里「没有把握就说不确定」的触发太宽，可收紧到「有争议或不知道的」）。
- [ ] 天气：设备没有城市设置，只能反问「你想查哪个城市」；绑定时填城市，还是按设备 IP 粗定位，待你定。

### [ ] N-8 噪声下丢回复与设备卡在「说话」

已修并验证：同围栏输出序号重置（整栈 `20261001-output-seq-v1`）、熄屏计时回绕（build 19）、回答被丢的预算与回退（`20261001-turn-budget-v1`）、假 VAD 抢掉已提交的回复（PR #171 的扣住，第十轮 12/12）。未关：

- [ ] **扣住的缺口（第十八轮，音量 50，线上没改）**：配方同第十轮（0.6 s、−20 dBFS 粉红噪声），噪声起点在话音结束后 1.2–2.4 s 扫描，两次运行共 27 次试验；设备 VAD 到达服务端 19 次，14 次回答，**5 次整句丢失**：4 次第一帧前被 `superseded`（`n8-pilot` p06、p12、p13，`n8-ab` p11；p06、p13 改答了噪声被识别出的字，p12、p11 什么也没答），1 次（p03）第一帧发出 0.10 s 后被切断。其中 3 次（p06、p12、p11）没有 `held` 行：推断是 `_pending_turn_has_text_evidence` 把识别器的中间结果也算文字证据，噪声被识别出一个字的瞬间扣住不成立（中间结果不进日志，未证实）。候选修法（改 `media_session_output_stream.py` / `media_session_turns.py`，要 PR 与发布，你来定）：扣住期间中间结果不单独取代回复、等终稿；或回复被取代而取代它的回合又是空的时，把准备好的回复重放。只有一台设备、一个房间、合成音。
- [ ] **p03：取消后设备在「说话」里停了 30.76 s**，直到 edge 以 `owner_silence_timeout` 关会话、WSS 重连，这段时间孩子说什么都进不来。本批 PR 的修法：设备会话里 `playback.flush` 成功之后立即结束替换代（见 HANDOFF 本批收据），并给「准备好的回复为何没被扣住」加了诊断日志（floor_open、turn_started、partial / provisional 字数）；真机复核要你点头的一轮（用第十八轮的配方）。
- [ ] 安静房间里的假回合：唤醒应答播完后，设备 VAD 会因 −46…−53 dBFS 的底噪多次起止，ASR 给出 `empty+vendor_silent` 或一句幻觉文字，常见结局是边缘以 `turn_prepare_timeout` 关会话（08:12–09:00 之间至少 8 次），少数触发查询。调高设备端 VAD 阈值，或让服务端对低能量幻觉文字更保守，待决定（会影响远场 / 小声说话）。
- 慢性、无影响：BMI270 的 I2C 读超时（`BMI2_ESP32: I2C read reg 0x03 len 24 failed: ESP_ERR_TIMEOUT`）自 09-24 起成串出现在每次 HTTPS 握手期间。

### [ ] N-9 孩子的记忆（写入、召回与家长确认已上线，剩三项要你定）

设备会话带系统提示词与会话内上文（PR #161）、跨会话记忆取回（#172）、「帮我记住」写入口径与家长确认入口（#178）、唤醒后第一句与逗号连着的尾问（#181、#182；第十四轮真机通过）都已上线。

- [ ] 孩子闭集词表偏窄（「画画」「恐龙」不在内，说「我喜欢恐龙」连候选都不会有，家长入口里只会看到学习进度类候选与闭集内的）：是否给孩子加一批无害的词。
- [ ] 抽取器置信度恒为 0.9，正好等于自动确认的门槛，没有余量（换模型若给 0.85，显式请求会退回候选，而提示仍让机器人说「记住啦」）：要不要对显式闭集请求放宽到 0.85，或不看模型置信度。
- [ ] 真正的响应规划器在设备会话里仍用不了：围栏纪元（规划器回显没有 `session_epoch`，设备围栏纪元 ≥ 1 恒 `fence_mismatch`）与策略版本（规划器写 `s2-v1`，运行时档案会话是 `multi-subject-v1`，`plan_matches_mode_policy` 拒绝真计划）两处不兼容。现在设备会话走本地安全计划加设备记忆取回，启用规划器的收益主要是人设指令，风险是这些指令首次进入生产；要启用须：客户端采用请求围栏的纪元、对齐策略版本、复核音色 / 快照检查；control-api 是另一条发布通道。**不要因为这个计划总被丢弃就跳过计划请求**：`/v1/interaction/response-plan` 在服务端还做危机路由并给监护人入队通知（`_route_crisis_with_bounded_evidence` → `record_minor_crisis`），副作用不会因为客户端丢弃计划而消失；现在它由 sidecar 在后台跑（`response_plan_sidecar.py`，回复不等它）。
- 已知边界：提到闭集词的尾问（「还是红色？」）整句不写，机器人会说记不下来；孩子说「我记住我妈妈的电话了」也会触发「不许诺」提示，后果只是机器人不说「我记住了」。

### [ ] N-10 机器人说话时喊「别说了」不一定停

空 VAD 占住终点时停止词接管（PR #169，第九轮真机通过）与停止后迟到收据不再关连接（PR #174）已上线。未关：

- [ ] 口头停止的成功率随音量剧变：音量 30 为 1/24（放大 2 倍 1/12），50 为 8/10（第十一轮）；第十八轮音量 50 下 46 步停下 25 次（54 %；停止延迟 p50 0.74 s / p90 1.58 s；没停下的 21 步服务端没有任何 `stop_word=True` 的终稿，故事讲满）。未被接受的几乎都是 ASR 终稿里只有机器人自己的回声。含义：声音小或离得远的孩子靠嗓子停不住它（P1-07 的范围，没有改动）；测试时音量要写进结果。
- [ ] 「停」单字识别偏弱（第十轮 1/5，「别说了」11/15；第八轮基线 3/7）：识别配置里有可选的 `FUNASR_VOCABULARY_ID`（词表要先在云端创建、再用真机录音校准，会动生产 env）；要不要做，你定。
- [ ] 机器人自己的声音透过回声消除漏进来（−43…−51 dBFS，真人说话 −21…−27），仍被识别成用户的话（每个故事 3–6 条）。N-15 处理了它并进下一句的最坏后果，漏音本身没有处理。

### [ ] N-11 同类未处理：设备按钮停止带着已被替换代的围栏

edge 对已被替换代的迟到 `playback.progress` / `started` 已改为丢弃、不再关连接（PR #174，第十、十八轮真机 0 次 `playback_receipt_rejected`）。形状相同但没动的：设备按钮停止带着已被替换代的围栏时，`CancelGeneration` 失败 → `stop_rejected` 并关连接（2026-09-30 04:30 一次，`button.stop … expected_fence.generation=2`）；按钮停止没有测过，看 edge 日志里的频率再定。

### [ ] N-13 唤醒后的问候与第一句（观察项）

`stale_echo_window` 分割（PR #177）与固件 build 21 起开麦后 300 ms、本地提示音后 800 ms 丢弃输入（PR #178，补丁 0033）已上线；第十二、十四、十五、十六轮合计 36 次 USB 唤醒里问候 0 次被取代、1 次被扣到 5 s（第十四轮 t001：开麦后 1.24 s 的无文字 VAD 把问候扣到 3 s 上限，与驱动的第一句撞车）。

- [ ] 方案 A（固件：照常喂引擎，但前约 600 ms 内屏蔽它的 VAD 事件与输出）与 B（服务端：首个 VAD 段起点在流前 0.5 s 内、能量极低时，不让它的 ASR 终稿顶掉已准备好的问候）暂不做，早期 VAD 再次让问候被盖掉时再启动。
- [ ] 判据「开麦后 1 s 内不再出现 rms > 1e-4 的 `Device VAD start`，且问候首帧在 listening 后 ≤ 2.2 s 的不少于 9/10」第十六轮按字面不满足（≤ 2.2 s 的只有 2/10，问候比前几轮晚 0.2–0.4 s，原因没查，服务端侧与第十五轮同量级），判据要防的失败一次也没出现；2.2 s 的阈值是否放宽，你定。
- [ ] 回声窗口（0.8 s 余量、1 s 跨边界）没动；盖在问候上的那句整句落在窗口内、超出边界约 0.6 s 不足 1 s 的情形没覆盖，放宽有把机器人自己的漏音当成用户话的风险。

### [ ] N-14 说完到开口的时延（目标 ≤ 2.5 s；现状 p50 约 2.83–2.92 s，第十四、十六轮）

已上线并在第十至十六轮真机验证：①分类器去重并在终稿时起跑、②回复不再等回复计划、④提交各段打点（`python scripts/voice_commit_timing.py <bridge.log>`）、⑦结束对话判定在端点钉住时起跑、⑧设备记忆提前取回、⑨VAD 流程的提交也给提前量、⑩唤醒后第一句与 VAD 起头句子的设备记忆提前取回；旋钮 `MEDIA_PLAYBACK_FOLLOWUP_GRACE_S`（bridge 环境变量，0.3–1.2 s，只能缩短，默认 1.2 s）。剩下的大头是 1.2 s 的播放后续问宽限（终稿 → 提交开始 p50 约 1.3 s，约占一半）。

- [ ] ③宽限改短：第十七轮实验（步骤 `docs/runbooks/followup-grace-experiment.md`，数字见 findings 第十七轮，50 步 × 2 种宽限）——0.6 s 省约 0.6 s（终稿 → 提交开始 p50 1201 → 602 ms），runbook 判据满足（停顿 0.3 + 0.6 s 切成两次提交 0/20 对 0/20），但停顿 ≥ 0.9 s 的句子切成两次提交 1/19 → 8/20（p = 0.020），「前半句先提交、后半句被丢」7/50 → 14/50（基线 1.2 s 下就有），整句没被一次答对 8/50 → 22/50。**默认值维持 1.2 s（你已定）。** 还没定：要不要试 0.8–0.9 s 的中间值（约 15 分钟，要你当场授权再改一次线上环境变量，机器人 USB 接着）、真实孩子的停顿分布怎么量、「前半句先提交、后半句被丢」要不要单独立项。
- [ ] ⑤影子模式、⑥级联接入（百炼决策模型 `decision-model-preview`，评测与脚本在 `docs/acceptance/run-20261003-decision-model/`）：**暂缓，不替换 DeepSeek**——快约 4 倍（一对调用 p50 约 170 ms，DeepSeek 约 650 ms），但结束对话准确率 95–99 % 对 DeepSeek 100 %，是预览版、限时免费，孩子的原话要交给数据处理条款没核对的模型。启动条件：宽限缩到 0.6 s 以内之后，提交路径上又在等结束对话判定（`close_ms` p90 ≥ 100 ms，或 ≥ 10 % 的提交 `close_ms` ≥ 200 ms；0.6 s 宽限实验里 p90 77 ms、基线 0，未到）；⑤ 还要你同意数据去向（每句话多发一次请求）；⑥ 只在 ⑤ 做了且数据支持时单独一个 PR（阈值事先定、不在评测集上调；危机与打断分类器不动）。
- [ ] 其余候选（未做）：回声终稿不必起分类器（N-15 修复后回声候选能在提交前被认出，认出时可跳过，先看打点数据）；去掉 `on_turn_committed` 里第二次 `refresh_runtime_profile`（约 0.06 s，先确认是否有意）；LLM 首 token / 豆包首包 / 首句缩短（各 0.1 s 量级，可能影响语气）；每轮两次 context-prefetch（设备记忆一次、`duplex_runtime.py` 的上下文预取一次）有一次是重复的；要到 2.5 s 以内还得预跑 LLM（更大的设计改动，未评估）。
- [ ] 第十六轮遗留：「问句 + 再见 × 6」那一遍只有第 1 个问句有回答，后 5 个服务端没有 ASR 终稿或只有 2 字终稿，进到服务端的声音很弱（救援片段 `rms` 9–51，健康值 554–2547）；第十四轮有过同类的一段，都没复现，原因没查清。
- 数字的边界：Mac 合成音，单设备、安静房间；首帧不等于听到（再约 0.1–0.2 s）。

### [ ] N-15 机器人回声并进下一句（只剩未覆盖的形状）

`stale_echo_window` 的 `echo_candidate_live_vad` 分割（PR #180）已上线，第十二轮真机 24/24 有回答、`assistant_echo` 丢弃 0 次。未覆盖：问话终稿在 VAD 结束已钉住端点之后才到的情形（保存的日志里没有，也没造出来）。

## 伙伴动画升级与调试手段（M 系列，2026-10-06 起）

来源：2026-10-05 对 Meta `facebookincubator/muse-gadget-sdk`（Apache‑2.0）的只读评估，加上你 10-06 对屏幕动画的反馈（「我们这个看起来就很死板，就像一张图片在上下动一下」）和按需下载角色的思路。它是 ESP32 薄客户端加 Meta 云端大脑，SDK 里没有 VAD、AEC、唤醒词和双工，所以音频对话主线没有可借的，可借的是形象动画与调试方式。借做法、不拷代码；若拷其 Apache‑2.0 代码，保留版权声明并标注改动；Jollybot 角色与仓库里提交的开发签名密钥不拿；SDK 令牌条款只约束使用 Meta 云端，我们用自己的后端，不受影响。

**本批（10-06）已写完代码与主机测试、整机上一项都没量过的**：M-1、M-2、M-6 ①②③⑥；M-4 已做完（见编号索引）。没做的：M-3、M-5、M-6 ④⑤、M-7、M-8。固件 build 号没动（24），没有刷机：产品镜像比 build 24 大 6,112 B，占 OTA 槽（0x3f0000）的 80 %；bench 镜像另带 `snap` / `status`。动画任务在真机上的 CPU、PSRAM、QSPI 开销与对音频的抢占没有量过，上机要你点头，顺序是先刷 bench 镜像用 `status` 量，再决定产品镜像。

### [ ] M-1 嘴型、声环和身体律动由真实音量驱动（代码与主机测试完成，真机未验）

- 做了什么：patch `0034` 在 `AudioService` 里留了两个读数点——播放任务把 PCM 交给 codec 时量一次（约比喇叭出声早 60 ms），上行 Opus 任务量经回声消除与降噪之后的上行；`memoria_audio_level.h` 把 20 ms 块的 RMS 折成 0–255（−54…−12 dBFS 线性）经 atomic 交给显示任务，显示任务每帧取一次，不管亮不亮屏（熄屏期间的音频不会冲进下一帧）。`MascotScene::SetOutputLevel` / `SetInputLevel`：嘴在一个块序列里「从谷底升起就张、从峰顶落下就合」（迟滞，最短张 80 ms、最短合 60 ms，不是固定阈值），声环亮度与身体上抬跟着慢包络，聆听时声环跟麦克风电平；播放队列停转时读数点什么也不发，嘴合上、声环变暗，不再演一段没人听见的回答。
- 主机验收：预览脚本的电平曲线剧本；`test_a_stalled_reply_keeps_the_mouth_shut`、`test_the_mouth_flaps_with_the_syllables_of_the_reply`、`test_the_mouth_shuts_within_a_quarter_second_of_the_audio_stopping`、`test_the_ring_glow_follows_the_voice_it_hears_and_the_voice_it_speaks`；「增量重绘 = 全量重绘」逐帧校验仍 0 不一致。
- 未验：真机上播放卡住时屏幕确实不再「说话」（第十八轮 p03 在 speaking 里停了 30.76 s，按代码推断那期间嘴一直在随机动，没回放视频核对）；动画任务优先级仍低于全部音频任务，真机对照 patch `0026` 的播放欠载计数不得比基线差。真机轮次要你点头。
- [x] 代码与单测　[x] 预览出片　[ ] 真机验收

### [ ] M-2 串口截屏与只读状态命令（只进 bench 构建；代码与主机测试完成，真机未验）

- 做了什么：Kconfig `CONFIG_MEMORIA_BENCH_SERIAL`（patch `0035`，默认 n）加 `config.bench.json`；只有 bench 构建多两条只读 USB 命令。`snap`：LVGL snapshot（带文字层，场景帧缓冲不含它），经串口分块 base64 吐出 RGB565，行格式 `SNAP <id> <w>x<h> <seq>/<total> <crc> <base64>`，PC 端 `scripts/snap_to_png.py` 校验 CRC 后还原 PNG。`status`：一行 `MemoriaBench: status up_ms=… phase=… mood=… frame=… frames=… drawn=… render_us=… busy_us=… px=… heap_free=…`（相位、心情、帧名、是否熄屏，加自开机的重绘次数与合成耗时的累计），`scripts/bench_status.py` 读它，两行之间算出速率；字段表与 C++ 格式串由测试互相核对。`scripts/voice_soak_serial_command.py snap|status --log …` 经常驻串口记录那一路发请求并等答案（snap 约 6–8 s）。产品固件没有这两条：二维码卡片上有绑定载荷，产品固件不能有把屏幕吐出来的入口；`memoria_usb_command.h` 的头注释与 `test_memoria_usb_command.py` 同步成「产品只收 `wake`，bench 多 `snap`、`status`」。
- 防串味：bench 镜像带标记 `MEMORIA_BENCH_BUILD=1;`，`publish_firmware_release.py` 见到就拒绝发布；入口是 `build.sh --bench` 与 `flash.sh --bench`；用完要把产品镜像刷回（USB）。两种镜像各编译过一遍，产品镜像里没有 bench 的代码，`check-overlay.sh` 通过。用法见运行手册 `docs/runbooks/release-rollback.md`「固件 bench 构建」。
- 发现（读源码，没上机）：上游的 MCP 工具 `self.screen.snapshot` 链进了产品固件（`LV_USE_SNAPSHOT` 上游默认开），但 Memoria 的协议上没有通道能调到它；Memoria 自己加的吐屏代码只在 bench 构建里。
- 未验：真机上 `snap` 还原图与肉眼所见一致，`status` 的数字合理；常驻串口记录里同一次会话自动留下「这一刻屏幕是什么样」，不再靠拍照。Muse 的 `>face=<mode>` 强制切状态会改显示状态，不在「只读」范围，要不要加另议。
- [x] 代码与单测　[x] 还原脚本　[ ] 真机验收

### [ ] M-3 在 sprite 上叠几个程序画的动态小特效（未做）

- 现状：聆听的声波、思考的泡泡、惊讶的感叹号已经画在姿势帧的美术里（如星澜的 `listening/thinking/surprised.png`），是静止的；拍一拍只有开心帧加一跳，没有爱心。
- 做法：用代码画叠加层让它们动起来——声波随音量向外扩散（M-1 的电平已就位）、思考的点一个个升起、拍一拍或 `loving` 时飘出爱心、偶尔闪星光。叠加层不依赖具体美术，换成定稿美术后只需微调位置和配色，不必等定稿。走现有脏矩形合成器，注意 `kMaxDirty = 6` 的上限。参照 Muse 的 `muse_pixel.c` 的粒子与叠加层。
- 完成条件：预览出片目测；`test_scripted_day_redraws_exactly` 仍 0 不一致；纯合成时间的预算先量后定（`status` 的 `render_us` / `busy_us` 就是量法）。
- [ ] 代码与单测　[ ] 预览出片　[ ] 真机验收

### [ ] M-5 预读（pre-roll）：先做取舍，再决定做不做（需要你决定）

- 现状：`InputSettle` 在输入刚上电时丢前 300 ms，并在本地提示音期间再静音 800 ms（`memoria_input_settle.h`，N-13 的依据：ADC 上电、提示音余响、AEC 未收敛）；头注释自己写着「丢得更长只会吃掉孩子第一个字的开头」。HANDOFF「板卡与固件」一节写着 `pre-roll` 未实现。唤醒后第一句丢第一个字我们见过（N-9，第十三轮「我记住…」），这是不是 `InputSettle` 造成的，没有证据。
- Muse 的做法：麦克风空闲时也在跑，环形缓冲保留按键前的一小段，所以边按边开口的第一个字不会丢；设备自己出声后立刻 `pre_reset()`，让自己的声音不进预读。
- 取舍：常开麦克风的预读环对儿童产品牵涉隐私（监护人同意、对外说明）和功耗；点屏唤醒模式下输入本来不常开（有的唤醒要重新给输入上电，见 `memoria_input_settle.h` 注释）。在你定之前不做。
- 做法（若做）：先量化——用 USB 唤醒加「唤醒的同时开口」的录音，量出第一个字的丢失率，确认丢字是否来自 `InputSettle`；`pre_reset` 规则对应 `InputSettle::NoteLocalSound`。
- 完成条件：你给出决定；若做，上述测试里第一个字的丢失率下降，且给监护人的说明就位。

### [ ] M-6 动画第 0 档：不要新美术，先让现有伙伴动得顺（①②③⑥ 代码与主机测试完成，④⑤ 未做，真机未验）

- 起点（已改，留作对照）：待机和聆听时屏幕每秒只重画几次且是整像素阶梯，合成器只有平移和以脚为轴的缩放，动势是固定时长的正弦，换姿势是「压扁—硬换—弹起」，没有过冲和跟随。差距不在硬件：Muse 在 S3 上同样只有 25 fps。
- 做了什么：① 子像素渲染（`memoria_mascot_raster.{h,cc}`）：位置和缩放保留小数、双线性采样，脏矩形多留 1 px；② 亮屏且伙伴醒着时统一 40 ms 一帧（25 fps），Wi-Fi / 绑定画面与打瞌睡的伙伴减半；`memoria_frame_pacer.h` 的节拍器量一帧占住动画任务多久（含等显示锁），超过间隔的 65 % 就每次加 10 ms（最多 +120 ms），连续宽松约 1 s 才还回来，所以屏幕出不起帧时它自己慢下来；③ 呼吸改成以脚为轴的体积守恒缩放（`sy` 增则 `sx` 按 0.7 减），不再整体上下平移；⑥ 接 M-1 的电平（嘴、声环、说话时身体上抬）。预览脚本 `preview_memoria_mascot.py` 出 `scene.mp4`，并逐秒数「有可见变化的帧」：`test_every_state_moves_visibly_20_frames_a_second` 要求每个状态逐秒的中位数 ≥ 20、最差一秒 ≥ 18（起点是待机和聆听的秒里 25 帧只有 3–7 帧有变化；「可见」指与前一帧在状态光环内至少差 5 个像素）；「增量重绘 = 全量重绘」仍 0 不一致，`kMaxDirty` 没放宽。
- 还没做：④ 弹簧阻尼代替固定正弦（换姿势、拍一拍、摇晃、说话起伏带过冲和回弹，目标改变时速度连续）；⑤ 换姿势用短交叉淡化代替压扁里的硬切（`MascotPack::Frame()` 对补丁帧只有一个暂存 sprite，见 `memoria_mascot_pack.h` 的注释，同时画两帧要处理）。
- 未验：你看前后对比视频认可（跑预览脚本即得 `scene.mp4`，旧版在 git 历史里）；真机一轮（要你点头）：对话中重绘从每秒 3–7 次涨到 25 次、待机从约 4 次涨到 25 次（约 6 倍），CPU、PSRAM、QSPI（40 MHz、4 线）开销与音频抢占没有量过，对照 patch `0026` 的播放欠载计数不得变差，动画任务优先级仍低于全部音频任务。量法是 bench 镜像的 `status`（M-2）。
- [x] ①②③⑥ 代码与单测　[ ] ④⑤　[ ] 前后对比视频　[ ] 真机验收

### [ ] M-7 动画第 2 档：分层 rig 试点（星澜），定稿美术要求分层交付

- 现状：美术是整张姿势图——源图在 `apps/miniprogram/assets/companions/<id>/`（512 px），眨眼和张嘴是图像编辑出来的差异补丁（`apps/miniprogram/design-preview/memoria-v2/tools/device_frames.py`），打成 19 帧的 `MMP1` 包（`memoria_mascot_pack.h`）。眼睛、嘴、耳朵、手臂不能单独动，所以视线不会游移、嘴形不连续、耳朵不会甩。Muse 的灵动来自逐部件的实时控制：视线每 1.2–3.6 s 漂向新目标并缓动，每 2.2–5.2 s 随机眨眼含双眨，呼吸约 ±3 %，嘴随电平加微颤，听、想、开心各有肢体语言。阵容是占位资产（README，将来整体替换），五个都拆层不值得，先拿星澜（默认角色）试点。
- 做法：① 先出一个星澜小样，验证分层图怎么来——美术重画、3D 毛绒建模分部件渲染，或用图像模型把现有姿势图拆层，哪种质量可接受没验证；② 交付规格（也写进将来的定稿美术要求，仓库里目前没有这份需求文档）：统一画布与脚点；部件清单（身体、头、耳 / 触角 / 叶芽、双臂、瞳孔、眼睑、每个心情若干嘴形、听 / 想 / 惊讶的小道具）；每个部件带枢轴点和层序；眨眼与嘴形成系列；格式用分层 PNG；③ 包格式：`MMP1` 升版本，加部件表（锚点、枢轴、父子、层序）与嘴形 / 眼睑表，沿用调色板加 zlib，旧包仍走整帧路径；④ 合成器：部件各自旋转平移缩放，弹簧跟随（头滞后身体，耳朵、触角摆动），瞳孔漂移与缓动，随机眨眼含双眨，嘴形由 M-1 的电平映射成连续开度，各心情的肢体语言做成姿势偏置而不是整图替换。运动库（弹簧、注视、眨眼）是共用代码，新角色只交付分层图和很小的 rig 描述。
- 不做的（已想过）：不照搬 Muse 的程序化像素角色，会丢掉毛绒质感；不做预渲染整幅动画片段——固定的帧不会看你、嘴不会跟声音走，几百帧全解码进不了 PSRAM（一张解码后约 147 KB），要边读边解（新的播放路径），每个角色都得出几百帧；按 14 KB 一张全帧、25 fps 算，assets 剩余空间五等分只够约 1.4 s，每槽 6 MiB 约 17.6 s，而一套完整动画（9 个姿势各循环 2 s 加过渡，假设）约 20 s。第 1 档（在现有位图上套粗网格形变，让触角、耳朵、叶芽带滞后摆动）只在分层美术迟迟不到时当过渡，不与第 2 档并行。
- 完成条件：星澜小样在主机预览里出片——视线游移、双眨、嘴随电平连续张合、头与耳的滞后、听 / 想 / 开心的肢体语言，你看了认可；包体积进预算（一个角色估 100–300 KB，没有分层美术可量，以实测为准），`test_pack_budget_fits_the_assets_partition` 与「增量重绘 = 全量重绘」继续通过，旧包仍可读；真机一轮对比（要你点头）。先看 M-6 的真机结果，再做它。
- [ ] 小样　[ ] 包格式与合成器　[ ] 预览出片　[ ] 真机验收

### [ ] M-8 角色包按需下载（A/B 槽，一次只存一个角色）

- 来源：你问「机器里一开始不存多个角色的整幅动画，当初始化选择一个或者后期更换时，才把那个角色的整幅动画下载到机器上（更换时先下载然后删除旧的）」。结论：可行，但它解决的是空间与交付，不是「僵硬」本身；独立一项，不挡 M-6、M-7。价值先体现在换美术不用刷机，分层美术试点时可以直接推到真机迭代。
- 现状：五个 `.mmp` 共 753,617 B（0.72 MiB，每个 115–184 KB）；最近一次构建的 assets 镜像 5.56 / 8 MiB，剩约 2.44 MiB（去掉五个角色是 3.16 MiB），镜像里还有唤醒词模型和字库。不能往现有 assets 分区里单独写一个角色：上游 `main/assets.cc` 的 `InitializePartition` 对整个镜像做校验，按 patch `0014`，本地资源加载失败时激活失败关闭。今天换美术只能 USB 刷 assets：Memoria 的 OTA 只写应用槽（10-04 回滚演练前后 assets 的 MD5 不变），上游的整镜像下载器要 NVS 键 `assets/download_url` 才触发，没有东西设置它，也不适合。分区表在 0x1000000（assets 0x800000 + 8M）之后没有分配（`overlay/files/partitions/v2/32m.csv`），配置与型号写 32 MB flash，即空着 16 MiB；改分区表要 USB 刷一次，OTA 不碰它。
- 可复用：手机选伙伴 → 设备空闲轮询显示档案 → `companion_id`（`memoria_activation_client.cc`），档案加字段对老固件无害（`wake_mode` 先例），但 `display_version` 只在换角色时变，美术更新要另有包版本；固件 OTA 整套（`memoria_firmware_update.cc`：设备签名的 GET、Ed25519 签名清单、流式 SHA-256、用户一开口就中止、校验通过才切指针；服务端 `services/control_api/app/device_firmware.py`；发布脚本 `publish_firmware_release.py`）；`LoadCompanion` 本来就一次只在 PSRAM 里放一个角色。一个速度数据点：build 22 的 OTA 3,331,344 B 用了 70 s，约 47 KB/s。
- 做法：① 容器与内部格式无关：头部加签名清单 `{companion_id, pack_version, format, size, sha256, min_firmware_build}`，Ed25519，签名域与固件清单隔开；服务端加 `GET /v1/devices/{id}/companion-pack`，发布脚本照固件那个；② 存放（要你定）：A. assets 尾部做 A/B，不改分区表，每槽 1.2–1.6 MiB，靠「镜像之外不参与校验」这个实现细节；B. 用末尾空着的 16 MiB 开两个槽，例如各 6 MiB，USB 刷一次分区表（只写 0x8000 的表加新分区，按 ESP-IDF 的设计不碰身份区与 nvs，需演练）。倾向 B，趁只有一台机器时做；③ 设备端 `memoria_companion_pack.{h,cc}` 仿固件更新：槽头带提交标记，写完且校验通过才翻指针，旧槽留到下次下载时再擦（「先下载再删旧的」不需要单独删）；只在空闲下载，用户一开口就中止，不续传、重来；包版本或最低固件版本不兼容就不装也不动旧的；下载期间旧角色照常显示，装好后走现有的换装「挥手」；④ 出厂默认：星澜（142 KB）留在镜像里——开机配网时还没有网络，屏幕就要有角色；实际是「一个下载位加一个小默认」；⑤ 小程序在选伙伴后提示「机器正在换装」（可选）。
- 范围：服务端角色固定五个且带人格、音色、欢迎语（`services/common/companions.py`），小程序目录也写死五个（`apps/miniprogram/utils/companions.js`），所以现阶段的价值是替换这五个的美术；「目录无限扩」另需服务端与小程序各发一版，不在本项内。
- 完成条件：主机测试编译真实的槽状态机，覆盖断电矩阵（下载一半、校验后翻指针前、翻指针后）、清单验签与签名域隔离、版本与固件兼容、用户开口中止；真机（每一步要你点头）：写前后用 `flash_backup.py md5` 比较保护区，B 方案 USB 刷一次分区表并用 `flash-id` 读出真实 flash 大小，Wi-Fi 下载一个包并换装，下载一半断电后旧角色仍在、重新上电再下，改一个字节的包校验失败且不换装不删旧的，再换回星澜。完成后在 `docs/runbooks/release-rollback.md` 加「角色包发布与回滚」一节（照「固件 OTA 发布与回滚演练」），并更新 README「手机同步」一段。
- 未知：flash 实际是不是 32 MB（没读过芯片）；只刷分区表是否不碰身份区；下载速度只有一次测量（6 MiB 约 2 分钟）；设备端工作量按固件 OTA 的规模估（342 行加发布脚本加回滚演练），没做过。
- [ ] 取舍（放法）　[ ] 主机测试　[ ] 服务端与发布脚本　[ ] 设备端　[ ] 真机验收

## 2026-09-28 收尾待办里还开着的

当日其余遗留事项已做完或被后来的轮次覆盖（见文末「编号索引」）。

**等你在手机 / 机器人上做**

- [ ] 伙伴页选一次「绵绵」（验证 #76：账号伙伴同步到设备人格）：账号伙伴曾停在 09-25 的桃喜，绑定页所选只写进了设备人格；#76 之后绑定页选的伙伴也写成账号伙伴，10-05 重绑之后没有再核对。
- [ ] 有背景声时（电视、音乐、旁人说话）问一个非天气问题（天气走实时查询快速通道，不经过这段逻辑），验证 #83：日志应出现 `media reopened turn committing without new text`，说完到开口约 3 s 内。09-28 的测试环境安静，窗口未触发。
- [ ] 公众平台「扫普通链接二维码打开小程序」规则：旧规则（`https://aigcnice.com/memoria-bind/`）随旧域名作废；新规则的前缀是 `https://aginice.cn/memoria-bind/`（公众平台上它当前是什么状态我看不到）。2026-10-05 12:35 用微信「扫一扫」扫机器人屏幕上的新码，打开的是 `aginice.cn/memoria-bind/` 的说明页、没有进小程序。按微信开放文档（2026-10-05 读取）：规则未发布时，只有「测试链接」（要填含 `?b=…` 的完整链接）且扫码人是管理员 / 开发者 / 体验者才会跳转，机器人二维码里的 `b` 每次都不同，测试链接命不中；对所有二维码生效要先「发布」规则，而「小程序必须先发布代码才可以发布二维码跳转规则」。所以体验版阶段要在小程序内「添加设备」→「开始扫码」绑定；想让微信「扫一扫」直达，得等小程序提交审核并发布线上版本、再发布这条规则。
- [ ] 需要时重新配网一次，真机验证固件 build 9 的「重试激活成功 → 重启进已绑定路径」（这条路径从没在真机上触发过；开发板现在是 build 24）。

**需要你决定**

- [ ] 固件 OTA 指针：线上 OTA 指针现在是 build 21（2026-10-04 回滚演练结束时从 22 指过去，演练前是 build 8、sha256 `046492ee…`；只有这一块板在轮询、已是 21，没有设备被升级；22 号作废）。保留 21 还是指回 8？build 9/10、15–20、23、24 都只 USB 刷了开发板。
- [ ] stash 存档 `outputs/stash-archive-20260928/stash2-realtime-scope-wording.patch`：联网查询隐私约束「只用本轮地点」→「本会话说过的也行」，当时未上线；是否作为产品改动重新评估。

## 定期运维

- [ ] **证书定期更换**：aginice.cn 现用的证书 2026-10-05 装入，**2027-01-02 23:59:59（GMT）到期**（腾讯云免费证书约 90 天一期、不会自动到服务器）；最迟 2026-12-19 前从腾讯云下载新一期 Nginx 格式（RSA）给我，我在新机原位替换 `/etc/nginx/ssl/aginice.cn/aginice.cn_bundle.crt` 与 `aginice.cn.key`（先备份、核对密钥与证书匹配 / SAN / 有效期 / 链，`nginx -t` 后 reload，核对 443 与 8443；做法见运行手册「生产主机」一节的换证书条）。这份证书与 pocketSparks 的其它站点共用，换的时候别碰它们的站点文件。
- [ ] **发布制品随发布累积**：每次整栈约占 4 GB（`/opt/memoria/incoming/` 约 1.5 GB + 镜像 tag 约 2.2 GB）；清理按「当前 + 紧邻回滚」、清单带 sha256、逐项 `docker rmi` / 精确目录删除、不用 `docker system prune`，每次要你授权（规则、脚本与收据见 `docs/runbooks/operations-space-governance.md`），约每 5–6 次整栈清一次，或根分区超过 70 % 时。待做：`scripts/docker_image_retention.sh` 因保护全部 `rollback-*` / runtime-base 与 pre-state 引用仍是 0 候选，要改成「只保护当前发布的 rollback 与运行中引用」，否则下一轮仍要手工清理。新机的磁盘现状（40 GB 盘，2026-10-05 16:37 整栈发布后剩 17 GB）见「域名切换与新机收尾」的 WAL 一条。

## P0：发布前必须闭环

### [ ] P0-04 当前使用人的监护授权与学生安全闭环

- 产品决定（你 2026-09-26，详见 `docs/compliance/p0-04-minor-safety-decisions.md`「实现状态」）：未成年人不分档、只学表达风格；时长与夜间时段运行时强制（危机除外）；年龄以家长申报为准并可在设备页修改；年龄不明也允许不留存的学习辅导；删「每周小结」勾选项；危机提醒推送给家长（老人一侧 2026-10-04 起暂不适用，代码暂留）；话术加 12356 并待专业审核。D2–D8 已随 `20260926-minor-safety-v1` 上线；未成年人长期记忆的留存口径（有效监护同意、有效 `guardian_of`、绑定 fence、受信设备、使用人本人说话同时满足时只带 30 天 TTL 与禁止训练）随 #152 上线，Agent 归档闸门对孩子也认 profile 的 `memory_recall_private`。设备上都没验。
- 待完成：微信订阅消息模板开通与 `MEMORIA_GUARDIAN_PUSH_ENABLED`（要你授权）；建后年龄资料与 `app_confirm` UI、guardian consent 决策接口；会话记忆按 subject 键迁入 `services/memory_scope`；两条策略入口的软件矩阵；安全专项设备链验收（身份 / 年龄 → 有效同意 → 准入或受限能力 → 固定话术真实交付 → outbox 绑定 / 幂等 / 家长读回）；话术专业审核。发送 worker / 外部投递暂缓。
- 软件门：两条策略入口覆盖 under_14 / 14_17 / adult / unknown_safe、权威 unavailable / 过期、profile-session 错绑、同意撤销 / 过期 / 无权限、管理账号更换与切人并发；不能决定时 fail closed，且不得读取其他主体私密记忆。
- 完成条件：软件矩阵与真实设备链一致，分别记录 `code / wired / enabled / verified`；`student_safety_loop_verified=false` 保持到安全专项设备链通过。入口：`routes/{identity_lifecycle,multi_subject,interaction,guardian}.py`、`services/{identity,session_runtime,guardian}/`。

### [ ] P0-03 TTS、续问竞态、设备停滞与真实时延

- 待完成：G 的 owner-silence / endpoint / commit / watchdog 真实配置矩阵；B/D 的 Bridge → Edge → 设备接收 / 解码 / 播放同 fence 证据；部分音频失败后的设备终态；ACK → 正文 < 1.5 s 的链路拆分与达标（你暂缓了埋点）。
- 设备验收：同一已启用候选完成天气 → 续问 → 播后告别至少三轮、> 45 s 与 B/D 同类长答、临近静默和部分下发后故障；补待机、五表情及点屏 / 摇晃 / 短拍 / BOOT 不回归。已有：缺陷 A 核心续问的真机证据（3 / 5 / 8 s 精确格通过，30 分钟基础长稳通过但带 TLS/WSS 自动重连观察项，`docs/acceptance/run-20260924-defect-a-retest-live/findings.md`），工具查询最终回答走通。仍缺：TLS/WSS 重连、长答 / 故障 / 待机 / 表情及交互矩阵，以及完整 3 / 5 / 8 s 格在当前版本上的复测。
- F1 / F2（`20260920-f1f2-owner-silence-and-barge` 已发布）：F1 有 2026-09-21 设备窗口的 owner-silence 观察；F2 只有 `button.stop` happy path 的设备证据，禁止源 barge 没有在设备旁真实触发，不能宣称完整 F1/F2 契约已完成。固件侧「播放期不发 `vad.start`」是可选加固（要刷机），voice barge 长期走 P1-07。修好模拟音频工具的两处自检判据（live 缓冲窗口、参考波形匹配）后跑自动问答扫频仍开放。
- 嘈杂环境：#83 的 2 s 证据窗口已上线，没在嘈杂环境实测（见「2026-09-28 收尾待办里还开着的」）。
- 上行削波：真话上行普遍削波（DTLN makeup gain 18 dB），单独评估；下次采集须确认 agent 日志流非空。
- 设备侧异常（2026-09-20 空闲期，非刺激引起）：BMI270 的 I2C 读超时刷屏（至今每次 HTTPS 握手期间仍成串出现，无影响）；端口复位后两次 `abort() PC 0x4038acd6` → `RTC_SW_CPU_RST`（约 12 s 后再起，随后自愈）。需硬件 / 固件侧单独排查。
- 约束：保留现有 GenerationBudget、代际隔离和失败有界退出；不靠延长静默、重复整句合成、第二提示或放宽门禁遮掩问题。入口：`providers/{generation_budget,doubao_tts,cosyvoice_tts}.py`、`voice_core/media_session_{standby,output_stream}.py`、`scripts/voice_session_{capture,report}.py`。

## P1：发布门禁、运行保障与产品闭环

### [ ] P1-01 冻结修复候选，完成发布与回滚验收

- 门禁、构建、上传、切流、回滚与验收底线在 `docs/runbooks/release-rollback.md`；整栈发布已用 `scripts/release_ops.sh` 跑过多次（`verify-load`、`freeze`、`cutover`、`finish`）。整栈 `rollback` 在新机没演练过（并入「域名切换与新机收尾」第一条）。
- 完成条件：同一候选的构建、切流、回滚、设备验收分别有证据；仅保留当前和一个可运行回滚，清理生产另授权。
- 身份收敛（发布治理）：control-api 期望的 release tag 应与真实发布 tag 一致，取消「agent 上报历史冻结 tag」的临时对齐；与预构建镜像入口一并作为本项输入。

### [ ] P1-08 新机的数据库备份策略

- 现状：WAL 归档已于 2026-10-06 关闭（仓库 `infra/memoria-data.production.yml` 与主机一致，归档卷已清空），新机没有 base backup（09-14 演练的 base 在 `/root/old-host-20261005/var-backups-memoria.tar.gz`，它的 WAL 链在旧机、到期即失）。以后若要重新开归档：归档卷必须是 `999:999 0700`，否则 `archive_command` 一直失败，并先做 base backup。
- 需你定并授权：每夜 `pg_dump`（默认不做）；不得按 mtime 删除或清理 `pg_wal`。
- 完成条件：保护集合、容量 / 恢复影响、执行证据和后续责任明确，保留恢复目标可验证。

### [ ] P1-09 readiness 刷新失败的主动告警（刷新修复与逾期提示已上线）

- 已上线：刷新脚本修复（2026-09-22）；逾期提示（`20260926-edge-flush-v1`）：证据超过刷新间隔 12 h + 1 h 宽限未更新时，`/health/ready` 报 `smokes: "overdue"` 与 `warnings: ["smoke_refresh_overdue"]`，状态仍为 ready。
- 待完成：主动告警渠道（要你定渠道与授权）；目前逾期只在有人或脚本读取 readiness 时可见。
- 发布链缺口（供 P1-01）：组件发布脚本 `deploy_control_component.sh` 的 cutover 块缺已构建候选续跑入口，image-only 覆盖无法承载身份 env，配置不变时需显式 `--force-recreate`，且不校验挂载 / 端口（细节见 `docs/HANDOFF-archive-0916-0923.md` 2026-09-22 节）；整栈发布走 `scripts/release_ops.sh`。

### [ ] P1-02 ASR 救援 sidecar：两项线上调整待授权

- 已完成：`infra/sensevoice-asr/` 入库 Dockerfile（基础镜像按 digest 锁定）、哈希锁定的 17 个依赖与模型校验值，重建镜像的转写与线上逐字相同；评测脚本 `scripts/evaluate_sensevoice_rescue.py`；模型对静音和任意噪声都返回「我。」的缺陷已修并上线（`SenseVoiceRescueConfig.accepts_text`）。手册 `docs/runbooks/sensevoice-asr.md`。
- 待授权（线上问题，建议见手册）：① 空闲约 7 h 后首请求解码 12.7 s（模型匿名内存被换到主机 swap，VmSwap 约 450 MB），空闲后的首次救援必超 2.5 s 预算，建议 `--memory 1536m --memory-swap 1536m` 重建容器禁用 swap（新机内存只有 3.7 GiB、swap 已用约 0.6 GiB，应先看它在新机上的表现）；② 27–30 s 语段解码 2.5–2.8 s 必超时，建议 agent `SENSEVOICE_MAX_AUDIO_S=12`。现场证据：2026-09-28 两次对话的首个救援请求 `httpx.ReadTimeout`（04:59:40、06:23:46），与 ① 一致。
- 约束：主链是云服务商 FunASR，救援后端单独核验；不把本地 FunASR PyPI、仓内 sherpa-onnx 或云模型版本混为一体，也不顺手改 NumPy / 设备 VAD。
- 完成条件：两项线上调整执行后，用合成语音在线上复测首请求与长语段时延均在 2.5 s 内。

### [ ] P1-03 修稳成员入口，再接按使用人切人格 / 音色

- 现状（`20260926-persona-subject-v1` 起上线）：人格跟随使用机器的人；人格存储按（绑定账号，使用人）记账，学习与读取都要求该使用人的长期记忆授权，按使用人删除流程含 `persona_forgotten` 步骤；绑定人在设备页看「TA 的表达风格」（`GET /v1/persona/subjects/{id}/style` 只返回已确认的固定风格标签），可经确认后重置（`POST …/reset`）。
- 已知风险（你的决定）：未成年人与成人一样完整学习人格特征（含价值观、决定等可画像特征），而 P0-04 学生安全闭环尚未完成；对外发布前需在 P0-04 中复核这一口径。
- 待完成：设备验收：同一台设备绑定孩子后隔天体现孩子自己的表达风格，账号本人的人格不串入；建后年龄申报 UI；guardian 邀请 / 授权与声音撤销的 consent 决策 seam；运行中会话的 `next_safe_point` 主动重协商与 device → session 映射；同 binding 两个 subject 的人格 / 音色设备实听。
- 完成条件：切换推进版本并使旧签名、上下文和音频失效；取消分配回落默认，克隆未 ready 回落设计音色；PG 与设备证据分开记录。入口：`routes/{identity_lifecycle,persona_assignment,custom_personas,multi_subject}.py`、`services/session_runtime/profile_service.py`、`apps/miniprogram/pages/{device,persona-custom,guardian,privacy}/`。

### [ ] P1-04 自定义声音：样本上传到设备出声

- 待完成：超过 60 s 后区分「仍在处理 / 需人工处理」；接通 consent 配置门和 privacy 页撤回删除；取得真实 provider 克隆 → 分配 → 设备可听的端到端耗时与失败证据。
- 保持 `reconciliation_required` 为可恢复语义，不伪造 ready 或百分比；声纹 / 声音样本使用单独同意，未 ready 回落设计音色。
- 完成条件：录音 → 上传 → 真实解码 → 训练 → ready / failed / 超时 → 分配 → 设备实听，连同拒绝、取消、重放、撤销和样本处置全链通过。

### [ ] P1-05 补权威会话状态，再做小程序三端验收

- 待完成：小程序手机 / 电脑 / 开发工具同版本验收、Edge → Control 受鉴权只读状态出口，以及生产 / 设备验收。
- 状态以 Python → Edge 的 `assistant_state.phase` 为源，带 session / generation fence 与新鲜度；重连、断线、过期显示 offline / unknown，不从字幕或客户端计时猜态。
- 完成条件：三端覆盖登录绑定、三态 / 断线、主体人格切换、样本进度、回顾、权限拒绝与刷新；体验版、提审和正式发布分别授权。

### [ ] P1-06 定义跨会话记忆语义，补未见召回评测

- 现状：候选可见性契约（main `0059368`）与向量路径词面项改动（#150，`20260930-vector-keyword-v1`，模型仍为 `text-embedding-v4`）已上线；抽取提示词 v3 + 中文关系标签后固定集 recall 0.875、未见集 1.0、泄漏 0。评测的完整叙事与数字（2026-09-23 至 09-30 的 Qwen、规则、向量路径与 422 条记忆的大语料干扰评测）在 [`docs/HANDOFF-archive-0924-1003.md` 末节「跨会话记忆语义与召回评测」](docs/HANDOFF-archive-0924-1003.md)，收据在 `docs/memory-evaluation-*.json`，复跑用 `scripts/evaluate_memory.py` 与 `scripts/evaluate_memory_distractors.py`（严格模式，需要 PG DSN）。
- 待完成：用线上带鉴权读口与设备追问验收向量路径词面项的改动；是否加分数阈值（近似缺失分不开，话题缺失 AUC 0.80–0.82，保住 75 % 目标的阈值约 0.347 时仍有 20 % 的话题缺失查询带垃圾；control_api / agent 里没找到按分数过滤记忆的调用方，未逐一核实）；用真实 Qwen 抽取的 claim 形态与更多真实专名 / 英文词复测；评估 claim / episode / 原子 projection 去重（向量路径下同一句话会占满 top-k 的 2–3 个位置；claim 送去 embedding 的文本带 `subject:/predicate:/value:` 脚手架是否拉低语义分，未验证）；生产更换 embedding 模型前须先回填向量——向量行只在编译落文档时写入、检索按（模型，维度）过滤，换模型后已有记忆只剩词面路径，仓库暂无回填工具；补齐实际 `ResponsePlannerClient` 超时、fallback 和生产 catalog 限额证据；把会话记忆迁到当前 person / subject 键、覆盖切人、撤销、删除和旧缓存的工作仍不提前宣称完成。
- 完成条件：固定集与未见集分别报告 recall / nDCG / extraction / leakage，candidate 语义、projection 去重、实际超时 / catalog 限额和 person / subject 隔离均有可复核证据；线上 / 设备边界仍未达成，设备追问另取 Actual Heard。

### [ ] P1-07 AEC 与播放期语音打断：独立受控实验

- 依赖 P0-03 / P0-04、真实 VoCat、双端采集和操作员；签名放行 voice 须另获授权，当前仅 button / keyword。
- 先验证 Exact DAC reference、通道映射、pre/post AEC residual、近端保留和双讲，再测播中告别 / 打断；不得绕签名、开播放期 KWS 或丢采集假装 AEC。
- 完成条件：同候选 / 身份 / 策略 / fence 的 T1–T14 逐格有证据，非主人 / 回声不越权；完整硬件门通过前 `full_duplex_verified=false`。

### [ ] P1-11 一台设备只服务一个使用人：声纹下线，信任与同意来自绑定（已上线，设备验收未完成）

- 现状：暂不用声纹（`MEMORIA_SPEAKER_AUTHORITY_ENABLED` 自 2026-09-25 为 false）；设备只服务绑定时选定的使用人（孩子；老人 / 本人绑定 2026-10-04 起不在范围内）；家长只看摘要、趋势、风险提醒，不看孩子原文；孩子的长期记忆需家长在绑定时勾选（默认不勾）；同意长期有效直到撤销；解绑撤销同意并询问是否删除；监护小结（`guardian_summary_view`）随长期记忆勾选一起授予，存量绑定要重新绑定或在家长页重开长期记忆开关才获得。按使用人删除流程（可续跑、有进度记录）已上线。
- 设备信任分档（#153，`MEMORIA_BOUND_DEVICE_TRUST_ENABLED=true` 自 2026-10-01 00:30）：生产设备没有 attestation（`device_fleet_attestations` 0 行），绑定链路上的设备按 `trusted` 只放行 `memory_capture`、`memory_recall_private`、`guardian_summary_view`，其余仍要 `verified`（硬件 attestation）。运维步骤见 `docs/runbooks/release-rollback.md`「设备信任开关」。残余风险：设备私钥种子在普通 NVS 分区，取出私钥的人可在电脑上冒充设备、通过对话召回孩子的记忆，家长解绑即 `revoked`，根治要固件开 flash 与 NVS 加密；`trusted` 不防被改过的固件；真硬件 attestation 留作后续。
- 待完成设备验收：新设备会话 profile 含 `memory_recall_private` 且没有其他敏感能力 → `archive_processing_outbox` 为 completed 而非 dead → 隔天追问能记起 → 家长小程序出现小结入口（回顾页见 N-4）；孩子绑定一次（10-05 重绑已有一次）后隔天仍记得前一天说过的事；播放期回声不自答、刚播完的回声「再见」不结束会话、真人「再见」能结束；家长端看不到孩子原文；撤销后不再记忆。编译与向量写入自 08-08 后没在现网跑过，可能暴露新问题。
- 未做：声纹代码与 `speaker-model` 容器待设备验证后清理；危机推送订阅号未开通（功能另分支，默认关闭）。已知残留（按使用人删除覆盖不到的）：已推送到 Redis 的记忆事件无法撤回（随流修剪老化）；`speech_style_stats` 聚合、persona / digital-self 快照、`entity_ids` 数组无行级来源可追。

## P2：质量增强与后续能力

### [ ] P2-01 统一记忆预取后的端到端验收

- 待完成：真实热路径时延前后对照，以及 P1-06 固定集 / 未见集的召回不退步证明；离线快照尺寸和构建耗时不能代替设备 / 真实链路。
- 完成条件：统一路径在预算、隔离、迟到拒绝和召回质量上均不退步。入口：`reply_pipeline.py`、`duplex_runtime.py`、`routes/interaction.py`。

### [ ] P2-03 可证明删除与导出证据链

- 现状：PG 全 saga 删除（归档 / 声纹行 + 加密对象 + 厂商音色桩 + 收据幂等）本地与 CI 已验；按使用人删除覆盖面逐存储记在 `docs/compliance/delete-domains.md`；备份「恢复后再删除」由 `scripts/replay_subject_deletions.py` 重放全部已完成删除，已写入运维手册恢复流程。
- 待完成：真实 MinIO 版本删除与真实 provider 删除各需一次可复现收据（需可用环境与授权）；已知缺口（Redis 已派发载荷、未脱敏的显示名与激活清单、工具效果载荷、SQLite 迁移备份、悬空实体 id、空主体历史证据）按合规文档明确接受，若要闭合需先定权限与迁移方案；小程序成员主体读口（P1-03 / P1-05）；生产 / 设备与真实机器人对话验收。
- 读口真相：产品召回不读 `memory_records`，走 archive 目录（`services/control_api/app/routes/interaction.py` 的 account_id + subject_id），所以仅封存 memory_scope 不会让轮次内容消失。
- 安全约束：不得默认 `account_id == subject_id`；必须有 Control 注册、active Identity person、owner evidence 和一致事件 subject；child / member 只接受唯一 lineage，foreign / inactive / ambiguous / NULL / mixed subject fail closed；源表与 `snapshot_json` 保持字节不变。
- 完成条件：PG、MinIO、投影 / 缓存和 provider 范围一致，重试幂等、回执可查询、导出标记 AI / 授权 / 服务提供者；保留备份写明期限与恢复后再删除，不承诺即时物理抹除全部副本；真实 MinIO 与 provider 各需一次可复现收据。

### [ ] P2-04 协议故障注入与长稳观测

- 现状（`20260926-edge-flush-v1` 起上线，设备行为未验）：关闭前送达排队的 `session.error`；Opus 编解码器加锁；`services/media_edge/device_ws_fault_injection_test.go` 覆盖七种关闭路径加拒绝 hello，三轮 24 条连接后协程、连接表、租约与 Voice Core runtime 全部回收，变异验证与 `-race -count=20` 稳定。
- 待完成：Python 侧 Bridge / Agent 进程退出与重启的注入；预定义长稳窗口（内存、连接、延迟趋势）；govulncheck / Trivy 扫描当前候选（本机未安装工具）；设备上验证终止性拒绝不再续连。
- 完成条件：旧代无副作用，有效输出有交付或可解释终态，回滚可运行；预定义长稳窗口保留内存、连接和延迟趋势。先补测量 / 注入，不顺手改断线产品行为。

### [ ] P2-05 补齐唤醒计数，再采家庭噪声矩阵

- 待完成：固定候选 / 固件 / settings，按物理距离、角度、输入电平、多冷启动和预定义稳态重采电视 / 家庭音源与真人对照；先核实际模型、阈值、状态和 detector 权威。
- 旧日志不足以证明电视 / 多人各 2 分钟有效零误唤醒，也不证明固定 45–50 s 预热；错误不能算 miss 或零误唤醒。这一项只在关键字唤醒还保留时有意义（N-1）。
- 完成条件：取得可比较设备 receipt 后才调检测阈值 / 词形；不修改 `advertised_duplex_level=none` 或 `aec_reference_verified=false`。

### [ ] P2-06 用可回放任务评测陪伴连续性与回顾质量

- 待完成：补学习挫败承接、隔次继续计划、本人 / 获授权管理人查看回顾等场景，交叉 under_14 / 14_17 与 retention 允许 / 拒绝，包含切人、撤销、模型超时和未见集。
- 逐场景记录任务接续、记忆证据、越权 / 编造、回顾可读性和失败降级；模型措辞需 recorded-bundle 人工盲评或设备窗口，离线生成不计真机成功。
- 完成条件：形成可重复 baseline 与同条件对照，主体 / 撤销隔离和编造事实零回归，汇总可追溯到话轮 / 证据。

### [ ] P2-07 Prompt 审计遗留（2026-09-24 审计；第 2、3 项待定）

- 审计口径：全仓发给模型的文本；实际模型为 DeepSeek V4 Flash（主对话，百炼）、`qwen-flash`（四个语义分类器 / 人格结构化）、`qwen-plus` / `qwen3.7-flash`（抽取 / 联网）。判据源自 Claude 文档，对这些模型只算经验判断，置信度最高为「中」；均未调用真实模型验证。
- 中-2 口癖禁用词表：`prompts.py` 里列举「首先、其次、最后 / 综上所述 / 希望以上内容对你有帮助」，无来由（首次提交即有），列出原词可能反向锚定。拟改为正面表述「像当面聊天一样自然衔接，不用书面报告式的分点连接词、总结句或客服式结束语。」；会改变生产陪伴提示词。
- 中-3 小结指令语义歧义：`services/control_api/app/routes/memory.py` 里「也不要输出敏感信息之外的推断」字面可读成允许推断敏感信息。拟改为「只写聊天中实际出现的内容：不编造事实，也不推测对方没有明说的健康、财务、关系等敏感信息。」（本意需你确认）。
- 低-5 仅标记：`services/agent/src/providers/qwen_realtime_search.py` 发往 DashScope 只带 `thinking: {type: disabled}`，而其余 DashScope 调用都带 `enable_thinking: False`（`handlers.py`、四个分类器）；需对照百炼文档或抓响应确认后再决定是否补齐。
- 低-6 仅标记：三个 Qwen JSON 抽取器用 `response_format: json_object` + pydantic 兜底；若当前 Qwen 支持 `json_schema` 结构化输出可再评估，现状可用。
- 完成条件：2 需在 DeepSeek V4 Flash 上用真实语音样本前后对照，3 跑 `scripts/evaluate_memory.py` 或小样本对照；语音 / 导师 / prompt composition / persona / 小结相关测试通过。

### [ ] P2-08 架构整理后续（2026-09-26 审计，减法优先）

第 0–6 批代码与 09-29、09-30 的小批次都已合并上线（过程见 `docs/HANDOFF-archive-0924-1003.md` 与更早的归档）。约束：涉及生产数据迁移须另获授权并先演练恢复；不做大爆炸重写，每步可独立发布与回滚。

- 第 3 批 ② 其余域：剩控制库 `MemoryStore`、speaker、evolution 的 SQLite（没要求删除）。
- #148 去 livekit 依赖已上线（bridge 镜像 892 → 683 MB）；发布后仍待真机对话验收：慢速 TTS 的节奏，用 `scripts/voice_session_capture.py`，须在使用时段内。
- 验证欠账：固件 build 15/16 起播放期关掉本地停止词后，长回复是否还触发任务看门狗与断线没有专项核对（本机留着的 748 份文本日志里没有 `Task watchdog` 字样，但没有专项的长回复串口窗口，仍算未验）。
- 在途分支 `wip/tts-stream-hardening`（#148 时摘出的 TTS 清理钩子与 `APIConnectOptions` 有限值校验，钩子版 `aclose()` 会吞调用方取消，需返工）：本批按你的「清理其他分支」删除，提交 `417627ce`、`edc6e40a` 在 git 回收前仍可取（`git branch <名字> edc6e40a`）；要不要重做，你定。
- 需要你定：播放期本地停止词（09-30 决定先关，由云端按语义停播）何时以更省算力的方式加回，等云端停播延迟数据（第十八轮已有：音量 50 下 p50 0.74 s / p90 1.58 s，见 N-10）；`self_model` 关系画像版本保护：PG trigger 只保护 approved 版本，是否收紧到所有状态（改生产 schema）；控制词不重置静默计时（UX）；生产常开 PCM tap（`MEDIA_PCM_TAP_DIR`，容器 tmpfs 每会话 4 MB，重启即清）是否长期保留（隐私）。
- 产品轨（大多需要你）：P0-03 时延拆段（你暂缓埋点；09-30 另查到播放约 10–13 s 时设备任务看门狗触发，MultiNet 与 Opus 同核，已用 build 15 缓解，待验）；外部试用（10 个家庭、4 周）；路演材料自洽、DEMO-09/10 竞品与定价（「定位二选一」已被 10-04 聚焦儿童青少年解决）。
- 完成条件：每步有行数与依赖图基线收紧的证据，生产切换有收据。

## 暂缓，不自动扩张范围

- 自动备份与异地副本：真实家庭服务 / 数据、正式发布或价值量级增长前重评；WAL 仅按 P1-08 处理。
- 家长通知发送 worker / 外部渠道：仅保留 outbox / readback 验收；启用另定授权和渠道。
- Qwen-Audio 3.1 ASR / TTS（已评估，结论）：ASR 两次真机对照均失败（播放期回声被提交为话轮、漏识别轻声、「稍等」开口延迟 4.1 s），生产保持 fun-asr；TTS 迁移按你 2026-09-25 的决定回退、生产保持豆包，重新启用即再 revert 回退提交 `d0d7a43` 与 `2be2f50`。重试前先定位新模型 VAD / 段落与回声边界，并在新候选上重做缺陷 A 的设备验收。收据 `docs/acceptance/run-20260924-d1d2-deploy/findings.md`。
- 流式 ASR 四家离线 A/B（2026-09-29，收据 `docs/acceptance/run-20260929-asr-ab/findings.md`，脚本 `scripts/evaluate_streaming_asr.py`）：43 段真机录音中有 11 句对上剧本。正常朗读四家基本打平；fun-asr 控制词出错最多，qwen3-realtime 在非人声段普遍吐「嗯。」，qwen-audio-3.1 与豆包最干净。第二轮补录的轻声段与重连后的低电平段四家全空，空结果是上行电平（无有效 AGC）问题，不换厂商。
- 多成员声纹与不依赖小程序的选人（原 P2-02）：随 P1-11 一对一绑定暂停；重启前须用真实标注样本校准误识 / 拒识后再定门槛。
- EOU 新模型、DuplexModel、expressive / 抢跑；ESP-IDF / ESP-SR / upstream 整体升级；老人故事册 / 人物复刻、年轻人潮玩（2026-10-04 起产品聚焦儿童青少年，老年人与年轻人方向先删去，见 README「产品定位」）和最终外形均不进入当前队列。

## 真机测试约定

- 不再用唤醒词唤醒（2026-10-02 起关键字唤醒在去掉的路上）：电脑模拟测试经 USB 唤醒，固件 build 20 起的串口 `wake` 命令（`scripts/voice_soak_serial_command.py wake`），需要常驻的 `scripts/voice_soak_serial_logger.py` 持有串口（2 h 上限，停用 SIGINT 而不是 SIGTERM；复位后的第一次唤醒不算数）。
- 每一轮真机语音测试、每一次 USB 刷机都要你当场点头；音量以你最新说的为准，默认 30、不主动调高（口头停止要约 50，须先问、测完还原）。
- 话术里不放危机类措辞（会给家长队列写入假的危机提醒）；家长设定的 `QUIET_HOURS 04:00–07:00` 与 `MAX_SESSION_SECONDS 1800` 不绕过（见 N-5）。
- 数字的口径：Mac 合成音（`say -v Tingting`）、单台设备、单个房间；第一帧不等于听到（要加约 0.1–0.2 s）；结果里写明音量。

## 验证约定

每个 PR 本地跑 ruff、module budget、mypy strict、相关 pytest（默认模式 + `MEMORIA_TEST_APP_POSTGRES=1`），涉及 PG 加跑 `run_authoritative_postgres_gate.sh`；语音链改动用 `scripts/voice_session_capture.py` 真机验收并写入 `HANDOFF.md`；改依赖时从旧锁重新求解、本地建 Agent 与 control-api 镜像并在 `--network none` 下重跑发布门、用 AST 扫描全仓 import 比对新旧 venv（查丢失的间接依赖）；小程序改动用微信开发者工具 CLI 上传体验版。

## 编号索引（已完成或已并入别处而从本文件删掉的）

代码注释与文档里还会出现这些编号；全文在 git 历史里，收据在 `HANDOFF.md` 与 `docs/HANDOFF-archive-*.md`。代码注释里引用 N-1、N-13、N-14 ④⑦⑧⑩、M-1、M-2、M-6、P1-05、P2-08 等的，指向本文件里还开着的条目。

- N-2 空闲熄屏：固件 build 18，你 2026-10-02 确认「空闲时黑屏」，第十轮串口有熄屏 / 亮屏记录。
- N-3 发布与刷机：`20261001-wake-mode-v1`、整栈 `20261001-audience-recap-v1`（PR #156）、小程序 `0.2.20261001.1` 与 `.2`、固件 build 18；OTA 指针留哪一版见「2026-09-28 收尾待办」。
- N-7 bridge 容器 `/tmp` 被 PCM tap 写满：已修，随 `20261001-audience-recap-v1` 上线；留下的隐私问题（PCM tap 长期留不留）在 P2-08。
- N-12 不用唤醒词唤醒机器人：固件 build 20 的 USB `wake` 命令（PR #176），用法见上「真机测试约定」，收据 `docs/HANDOFF-archive-0924-1003.md`。
- P1-12 旧媒体链去留：2026-09-29 随 `20260929-livekit-retire-v1` 下线并清理。
- P2-02 多成员声纹与不依赖小程序的选人：并入上面的「暂缓」。
- M-4 预览编译加 ASan / UBSan：`firmware/esp32/scripts/preview_memoria_mascot.py --sanitize` 与 `test_compositor_is_clean_under_sanitizers`（5 个伙伴 × 3 条时间线）已入库，没有发现。
- 2026-09-28 收尾待办里删掉的四项：writer-teardown 版本切换后的设备重连（此后多轮真机已覆盖）；`response_plan_cached reason=no_verified_runtime_profile`（09-28 的两次发生在设备信任开关 2026-10-01 00:30 打开之前，2026-10-06 只读查新机 voice-core 容器近 24 h 日志该签名 0 次，设备会话现在走本地安全计划，见 N-9 的规划器一条）；SenseVoice 救援 `httpx.ReadTimeout`（归 P1-02 ①）；回复首音时延（归 N-14）。
- 边界 yaml 里原有的 `subject_scope_batch`、`account_to_subject_migrations`、`read_path_postgres_parity` 三项：账号 → 主体迁移于 2026-09-28 第 2 批删除，人格改为按使用人学习与读取（P1-03）；表结构保留在各 schema，git 历史可恢复。

---

## 历史材料索引（非执行队列）

市场、融资、BP、产品战略和架构简化讨论已迁移至 [历史战略与融资分析](docs/strategy/fundraising-analysis-20260921.md)。
该文件不构成当前工程执行项、发布门禁或完成证据。
