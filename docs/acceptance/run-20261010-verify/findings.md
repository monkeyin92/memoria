# 2026-10-10 发布后两组语音记录复盘

本报告复盘已经发生的两组记录，并在 2026-10-10 16:33–16:51（北京时间）只读核验生产、发布收据和限定测试会话的档案。没有重新播放素材、操作串口、刷机或发布。原始记录保留在本机 ignored `outputs/acceptance/run-20261010-verify-{standard,probe}/`，未改写；全部文件的大小和 SHA-256 见 [source-receipts.json](source-receipts.json)。时间线是原驱动的观察结果，不等于问题内容或整体验收通过。

## 结论与验收状态

- `20261010-stale-interim-v1`（PR #198，源 `bef8e1174679b40239b35c96729911ea0331882c`）已经发布；三个整栈角色的运行身份一致、healthy、重启 0，公网 readiness 为 `ready`、提供方冒烟 `passed`。回滚镜像指向 `20261009-textless-spare-v1`，本次发布没有再次演练 rollback。
- 标准轮 46 步、44 次进入说话、2 次 `no_reply`，实际 13 分 19 秒；不能关闭 N-5 的 ≥30 分钟长稳。44 次出声没有逐句核验内容，不能写成 44 个问题答对。
- 10 个打断尝试全部对应 `playback_completed`，确认停止 0；原驱动 `stopped=true` 的 7 次和全部停止延迟作废。包含口头停止、新问题和附和素材，不能把每种素材都当成应当停下的命令。
- 噪声探针 6 步只有 1 次出声，但该次的转写与回答都不对应算术题；另外 5 步没有已提交回复被取消的证据。不能把 5 次 `no_reply` 记作 #197 / #198 修复失败，也不能把唯一出声记作内容通过。
- #197 的保护日志 `media reply spared…` 在两轮都为 0；标准轮两次丢弃均是 `fresh_text_evidence=True`，没有证明 #198 专门修复的旧临时文字路径被撞中。保持 `direct_real_device_verified=false`、`full_duplex_verified=false`、`student_safety_loop_verified=false`。

| 项目 | code / wired | enabled | verified（2026-10-10） |
|---|---|---|---|
| #197 首帧之后保护未被听到的回复 | 已合并并接入 | 已随 10-09 发布，10-10 继承 | 单测有复现；本轮没有目标事件，设备专项未通过 |
| #198 旧临时文字不当作新边缘的话 | 已合并并接入 | 10-10 07:59 起 | 单测有复现；发布身份/健康已核验，本轮不能证明目标路径修复 |
| `voice_soak` 停止判据 v2 | 本批本地实现，驱动和分析器共用 | 后续用本批脚本运行才生效，不涉及生产服务 | 离线回归和既有记录复算；没有新真机验收 |
| ASR 重叠 / 空 endpoint / 关闭诊断 | 候选分支实现，接到既有日志出口 | 未发布；原生产日志没有新字段 | 12 条离线诊断用例、Agent 全套 2958 条回归通过；没有新设备证据 |

## 条件与汇总

两轮都通过 USB 唤醒，使用音色库素材（时间线 `voice=bank`），Mac 音量 30、运行前 19；固件使用既有 build 24 产品镜像。标准轮会话 `d0208cd4-ce37-40e7-bc55-2d69b7a39104`、stream epoch 2570；探针会话 `751e33c8-15d5-447a-a672-66fdceb42c67` / 2571 和 `70e5dd2e-a3d3-45b4-8efb-1df4bb9fb4e2` / 2572。一台设备、一个房间、电脑素材；没有孩子真实声音的证据。

| 指标 | 标准轮 | 噪声探针 |
|---|---:|---:|
| 时间（北京时间） | 08:01:53–08:15:11 | 08:16:53–08:20:20 |
| 实际时长（不使用参数里的上限） | 13.31 分钟 | 3.44 分钟 |
| 问句素材 / 进入说话 / `no_reply` | 46 / 44 / 2 | 6 / 1 / 5 |
| 中性素材出声延迟 p50 / p90 | 2.585 / 6.29 秒（34 次，含一次错配可能） | 唯一出声 1.98 秒，答非所问 |
| 扣住 / 放出 / 丢弃准备回复 | 5 / 3 / 2 | 0 / 0 / 0 |
| ASR tail timeout | 8 | 10 |
| `cross_sentence_overlap` 拒绝 | 6 | 0 |
| `empty-timestamps` / `reply_task_exception` | 0 / 0 | 0 / 0 |
| ASR 上行边界 / 全零 / 削波 | 70 / 0 / 6 | 18 / 0 / 0 |
| 编码队列警告行 / 末尾累计丢帧 | 4 / 6 | 0 / 0 |
| `media spoken stop interrupted reply` | 0 | 0（未安排停止场景） |

标准轮的 3 次扣住放出分别等待 3.77、3.02、3.01 秒。上行编码丢帧累计是 6 帧，不能把 4 行警告写成 4 帧；日志按累计采样输出。上行全零不是本轮的证据，部分窗口削波或低电平对识别的作用仍未分开。

## 标准轮：分别看出口，不统称丢回复

| 步骤 | 已核对的事实 | 判断与剩余未知 |
|---|---|---|
| 唤醒 / t001 | 08:02:19.874，turn / generation 1 的问候被取消，`fresh_text_evidence=True`、首帧未发；t001 08:02:23.829 播的是 generation 2 | 不能把整轮两次 `superseded` 都算作问句丢失；这一次是新输入让问候让位 |
| t038 下雨 | 素材 08:12:22.843–25.006；驱动到 08:12:55.013 判无回复。服务端直到 08:12:55.038 才提交 turn 39，档案转写不对应下雨问题。08:12:55.858 回复备好扣住；08:12:56.386 以 `textless_turn_over` 取消、`fresh_text_evidence=True`、`first_frame_sent=False` | 提交时驱动已超时，且与下一步素材开始几乎同时；取消发生在其后。这不是已证明的旧临时文字触发。新问句、噪声和误识别的贡献没有分开，不能直接改 #198 的 40 ms 阈值 |
| t039 月亮 | 素材 08:12:55.039–58.747；一条 5 字终稿被 `cross_sentence_overlap` 拒绝。08:13:01.257 是 `empty_media_turn`；直到 08:13:18.464 才提交 turn 40，档案转写不对应月亮问题；08:13:19.500 首帧、23.470 播放结束 | 驱动记开口 20.95 秒，但回答是泛化闲聊，不是已验证的月亮答案。阻挡重叠的区间未记录，延迟归因和拒绝是否伤到用户终稿仍未定 |
| t046 唱歌 | 素材 08:14:39.820–41.810；08:14:46.789 出现 3 字终稿，随即 `early conversation-close endpoint source=final`；08:14:46.809 `conversation_end_explicit`，Edge 46.810 投影关闭 | 没有准备回复被取消，是关闭路由结束会话。`source=final` 只说明从终稿入口触发，同步判断也会读取已有缓存，不能据此断言命中告别关键词或排除语义判定。终稿原文未归档，不能断言具体听到了什么 |

原始桥日志的主要行：问候 33–35；t038 / t039 的提交 3214、扣住 3233、取消 3236–3238、重叠拒绝 3245、空回合 3259、下一提交 3314、首帧 3329；t046 3846–3854。它们只记长度和围栏；归因辅以对上述三个 session 的限定窗口 `BEGIN READ ONLY` 档案查询，没有读取其他主体或时间段。

## 噪声探针：目标取消路径没有发生

`noise.jsonl` 记录每次在素材结束后 1.602–1.610 秒开始播放噪声，文件按 `noise_m20.wav`、`noise_m14.wav`、`noise_m10.wav` 循环（计划标称 −20 / −14 / −10 dBFS）；这是文件电平和调度时间，不是设备旁测到的声压。声音进入 VAD / ASR 的位置与文件调度存在差距，没有用 PCM 重放或双端采集证明因果。

| 步骤 | 驱动 | 桥与 Edge / 档案证据 |
|---|---|---|
| p01 天空 | 无回复，仍聆听 | 08:17:21.867 收到 2 字终稿；21.881 的待定回合仍为旧区间 `320–8960`，已有文字区间 `100320–102240` 不在其中，提交结果仍为 `empty_media_turn`；22.528 尾部超时。后续多次 `partial_present=False`、`empty+vendor_silent`，没有用户回复提交或准备回复取消；文字与 endpoint 的归属需继续查 |
| p02 下雨 | 无回复，空闲 | 没有用户回复提交，ASR tail timeout；第一会话 08:18:17.531 以 `owner_silence_timeout` 关闭 |
| p03 恐龙 | 无回复，仍聆听 | 第二会话的 ASR 多次无文字/低电平；08:18:48.504 和 08:19:03.675 是 `ASR tail timeout`，没有准备回复被取消 |
| p04 算术 | 有出声 | 档案把素材对应输入转写成另一句，答的是“我一直在”的闲聊；turn 2 / generation 2 完整播出。只证明有音频，不证明算术答案通过 |
| p05 英语 | 无回复，仍聆听 | 08:19:27.415、38.409 无文字尾部超时，没有新回复提交 |
| p06 笑话 | 无回复，空闲 | 08:20:03.849 无文字尾部超时；第二会话 08:20:07.096 以 `owner_silence_timeout` 关闭，没有准备回复被取消 |

探针只有一条用户 turn commit（桥日志 356）；其余两条完整播放是 USB 唤醒问候。`media prepared reply dropped`、`superseded`、`media reply spared…` 全部为 0。下一步应先确认素材实际进入识别器的质量和 endpoint / 尾部超时，再设计能撞到“回复已备好后新噪声到达”的实验；本轮不能估计取消路径的丢失率。

## 后续本地追查：输入、endpoint 与关闭路径

用户随后授权“进行下一步”。本轮只读取留存素材和原日志、补服务端诊断与离线测试；没有播放声音、访问生产、操作串口或部署。[input-audit.json](input-audit.json) 固定 12 个本机文件的格式、SHA-256、数字电平以及 9 个素材窗口里的 VAD 观察。素材文件没有在原播放时记录哈希，因此这些是当前本机留存文件的读数，不能倒推当时的实际声学输入。

- 6 个探针问句都是 24 kHz / mono / 16-bit PCM，长 1.395–2.373 s，RMS −23.73 至 −19.90 dBFS，无全零或数字削波；标准轮下雨、月亮、唱歌素材也有非零音频。探针噪声文件实测 RMS：`noise_m20` −20.19、`noise_m14` −14.19、`noise_m10` **−12.32 dBFS**（不能把最后一项的文件名当作实测 −10）。这些读数不证明内容正确、机器人旁 SPL、麦克风、AEC 后 PCM 或提供方实际输入。
- 只按日志的主机时间比较素材进程窗口，p01 和 p04 没有新设备 VAD start，桥也没有同期新起点；p02 / p03 / p05 / p06 各有 1 个。标准 t038 也无同期新起点，t039 / t046 各有 1 个。这是已有日志的观察，不等于没有采到 PCM，也不是校准后的声学时序。
- **p01 的失败不需要归因于后来的噪声**：素材窗口 08:17:19.154–22.351，短终稿在 21.867 到达，旧 endpoint 在 22.528 超时；首个计划噪声到 23.954 才开始。离线用真实映射函数重放 `sample_offset=23360 + begin/end=4810/4930 ms`，得到已记录的 `100320–102240`；旧回合 `320–8960` 仍只投影空文本。该换算可复现，没有样本算术错误的证据；这不排除上游起点、时钟或提供方标注问题。为何设备未给出该文字所属的新 VAD / endpoint 仍未知。
- **t038 的大部分等待发生在输入阶段**：08:12:24.705 有短终稿、24.726 钉过 follow-up endpoint，后续多次 VAD 重开/延长；52.165 才有下一短终稿，54.836 走 `reopened turn committing without new text`，55.038 才提交。不能把这 30 s 等待归到 LLM / TTS；是否存在该样例的 endpoint 状态机缺陷还需完整事件重放，当前不改变宽限或 #198 的 40 ms 阈值。
- **t039 的拒绝区间仍不能从旧日志复原**：拒绝的是 mid-utterance rescue，区间 `10209600–10273600`，task epoch 59；旧日志没有阻挡它的已接受区间。即使某次重叠在读码上符合保护规则，也不能借合成阻挡区间宣称这次拒绝正确或错误。
- **t046 的关闭来源仍需新日志**：原问句“你会唱歌吗？”在当前词法规则里不是关闭命令；测试同时证明词法告别与缓存为真的普通短文本均可从 `source=final` 钉 endpoint。这是用于区分机制的合成对照，不是还原那条未归档的 3 字终稿。

本地补的诊断只记位置、计数和布尔值，不记录终稿、阻挡文字或 sentence id：

| 日志出口 | 新证据 | 行为边界 |
|---|---|---|
| `media ASR result rejected` | 拒绝稿的 `rescue_synthesized`；`overlap` 包含规范化后的拒绝区间、阻挡区间总数及最多 4 条 task epoch / sample range / text length / rescue 标志 | 快照在 supervisor 作出拒绝时生成；保留已提交水位与 provider 优先于 rescue 的规则 |
| `media turn has no text` | `text_after_endpoint`、pending end、active VAD start、last playback end、committed sample | p01 对应“文字已在旧 endpoint 之外”，不强行提交或合并未知归属的文字 |
| `media early conversation-close endpoint` | `rule_match`、ASR sample range / task epoch / rescue 标志 | `source=final` 且 `rule_match=False` 可识别缓存路径；规则命中为真仍不能排除同时存在缓存，不改判定结果 |

阶段性软件验证：新诊断用例先在旧代码的缺字段处失败，实现后 **12 passed**；当时 Agent 套件为 **2956 passed / 0 skipped**。录制基准 `test_media_session_golden.py` 字节未改，SHA-256 `f7825893b147685e44e55ebbaa500acc6f0c69999f382157fc27ef010f40b938`。后续 t038 回放用例加入后的最终完整验证见下方候选验证记录。

### t038 离线回放结论

新增 [t038-replay.json](t038-replay.json)，从本轮原 bridge 日志 SHA-256 `e4954e3a6dbb9d5a440c2ab62e63a75480d057fd1e7784f5b7c62154285ed06e` 提取 18 条带源行号的 VAD、ASR final 与 endpoint 事实。它是已记录事件的确定性回放，不是完整重建：原日志没有保留当时所有 ASR interim、文本内容和 reopen-window guard 结果；ASR 文本与设备运行时初态因此使用了标明的合成控制，不能把回放当作该设备的实际识别还原。

- 按记录首个短终稿与 VAD 范围回放，已有无新文字保护会在第一次合格重开后的 2 秒提交 `9728640`。这是代码当前设计行为，不是强制复原线上原先提交时刻。
- 加入一个仅用于差分的合成 interim，其文字范围越过 `9728640` 后，定时器按规则暂缓；后续 VAD endpoint 超出该短 final 覆盖范围，窗口因 `endpoint_uncovered` 不启动。待 08:12:52 的新 final 形成覆盖后，窗口于约 08:12:54.836 再次提交，与原日志记录相差不足 1 ms。
- 这个差分说明一个未归档的 interim 足以解释长等待，但不能证明它当时真实存在。因此没有改 endpoint、重叠或文字提交行为，t038 仍是待设备诊断验证，不记为已修复缺陷。

为了让下一次同类事件可以定因，`media_session_input.py` 新增只记录布尔原因、ASR sample 范围、文字长度和 endpoint 水位的诊断：重开窗口是 `armed`、因 `no_text` / `endpoint_uncovered` / `reply_in_flight` 未启动，还是到期时因 `turn_end_advanced` / `partial_after_endpoint` / `turn_changed` / 输出仍在途而暂缓。字段不包含终稿内容，不改变既有判定。新增 `test_media_reopen_replay.py` 两条确定性差分用例验证原范围和合成 interim 分支。

候选本地验证：完整 Agent 套件 **2958 passed、0 skipped**；脚本套件 **501 passed、1 skipped**；新增 ASR/reopen 诊断与发布脚本聚焦检查通过；`ruff check .`、严格 `mypy services --strict`（435 文件）、26 项模块预算、Bash 语法及文档预算均通过。远端 PR CI 尚待运行；未进行设备测试、生产访问或部署。

下一阶段可将这批判据、输入/关闭诊断与 reopen-window 诊断作为一个服务端候选提交并走 CI；生产发布和设备语音仍分开授权。目标事件要以完整 fence 和新的诊断字段验收，不靠放宽阈值、强制合并空 endpoint 或将 synthetic replay 宣称为真机通过。

## 停止判据的修正与历史纠正

`scripts/voice_soak.py` 新增回复开口、打断素材起止、截止点、设备退出说话的绝对时间与串口时区偏移。`scripts/voice_soak_evidence.py` 被驱动和 `voice_soak_analyze.py` 共用：

1. 串口 `First playable downlink frame` 的 generation 对应开口，结合桥的 `first_frame_sent` 找到唯一完整 `delivery_id`（session / session epoch / turn / generation / tool）；有歧义就不认定。
2. 同一回复须在尝试窗口内有 `preempted` 终态，并且有实际存在的 `media spoken stop interrupted reply`（同 session / turn / generation，`flush=True`）。`superseded`、任务取消或非完成的任意终态单独都不能证明口头停止；新问题抢话缺少该正证据时记未验证。
3. 同一 generation 的串口播放收据须有实际输出，并且设备在窗口内退出说话。自然完成、异常退出、超时和缺日志都不能算停止成功。
4. `stopped` 为 `true / false / null`，另记判定原因与回复身份；缺证据为 `null`。延迟从素材起点到设备退出说话计算，明确不是声学停止延迟。旧字段原封不动保留在原 JSONL，报告重新算；老记录没有打断绝对时间，不能借旧 `stopped=true` 或负延迟宣称成功。

离线复算命令（无 SSH、无设备、无音频）：

```bash
uv run python scripts/voice_soak_evidence.py outputs/acceptance/run-20261010-verify-standard
```

[stop-audit-standard.json](stop-audit-standard.json) 固定本轮 10 个尝试的完整回复身份与结局：generation 14–23，全为 `playback_completed`，确认停止 0。

另外只读复算了 10-09 的两组既有记录：修前基线 10/10 自然完成；晚间复测 9/10 自然完成，t016 对应的 generation 16 是 `reply_task_exception`，也不能算成功（后续复用同 fence 发声不改写这个失败终态）。此前“晚间十个都自然完成”的表述过宽，已回写两份 findings 和 HANDOFF。**`playback-stop taken` 在代码里不存在**；以后核验实际停止动作使用 `media spoken stop interrupted reply`，并核对同一回复的终态，不能只凭一个不存在的日志子串计数。

代码与主机回归不等于新真机验收；本批没有运行新的语音测试。离线测试覆盖正常完成、真实停止、错误/传输退出、缺证据、跨会话/epoch/tool、错代、超时、跨午夜、延迟日志补到后的复算，以及模拟驱动的时间记录与音量恢复。软件门的结果记在 HANDOFF 本批收据。

## 发布收据（只读核验，非本批重新发布）

- 源与 tag：`bef8e1174679b40239b35c96729911ea0331882c` / `20261010-stale-interim-v1`。切流 07:58:53–07:59:39，finish 08:01:01。六步 `verify-load / freeze / env / schema / cutover / finish` 全 PASS、exit 0；schema 为 `UPDATE 0`。
- 三个服务端角色的标签、OCI revision 与 current 一致；image id：Control API `5da9e8d7a992…`，Bridge `89d0ce364884…`，Speaker Model `a4b9422a5440…`。Media Edge 仍为 `20261002-late-progress-v1` / `f3fe8742177e4002db652203b1fadffa549d1098` / `8c3815357fd3…`。
- 回滚镜像 `*:rollback-20261010-stale-interim-v1-pre` 与 `20261009-textless-spare-v1` 各角色 image id 相同：Control `e45acac7227f…`、Bridge `46da9b837b26…`、Speaker `bcb4acae191f…`。本次没有新回滚演练，不能把镜像存在写作演练通过。
- manifest `/opt/memoria/incoming/20261010-stale-interim-v1/release-manifest.json`：SHA-256 `9dbb9c7a57065e73babda18179da061aa8323e8d6936eaf0fc69bce81c14f5e4`；images 1,463,958,528 B、SHA-256 `5e7f6e0811f537564dfd7c6533ffe868c1241eea62a091d203df61409ff89dfd`；source 66,703,360 B、SHA-256 `c8635eda650eb6047a550a13f432f01ed18aa3b3bec6c61ca94fab93d729cbd8`（后两项从已经通过 verify-load 的 manifest 回读，本轮未重新读整个制品）。
- 切前 dump `/opt/memoria/releases/20261010-stale-interim-v1/.cutover/memoria-pre-20261010-stale-interim-v1.dump`：4,427,691 B、SHA-256 `cc7217aed648e1cac460d62b02ba6728a398e9e71f5aa00c80aa2525224fa10d`；同目录有 pre / post-state 与 env 快照，未读取或复制秘密值。
- 10-10 16:51 只读核验时，主机 `/root/memoria-release/release-ops.sh` 与当时仓库版本 SHA-256 同为 `a8dfb1babb2f3cb3cf355402c57e0822926fc07b0edaf546487bc59cbf739f5b`，PREV 为 20261009（当时正确的回滚目标）。当前候选分支已将仓库 `PREV_TAG/PREV_COMMIT` 与脚本断言前移到 `20261010-stale-interim-v1` / `bef8e1174679b40239b35c96729911ea0331882c`；主机文件未安装或修改，线上没有切流。
- 既有清理收据 `/root/memoria-release/cleanup-20261010.log`：09:40–09:41 清掉 20261007 的旧镜像 tag、incoming 与源码树，保留 `.cutover`；当前根分区 21 / 40 GB（55%，剩 18 GB），incoming 保留 20261009 / 20261010。本批只读核对，不执行清理。
