# Memoria 研究跟踪

本文件是仓库唯一的外部研究扫描落地处。研究助手（memoria提升大师）每次扫描只更新这一份文件，不另建文档。开发在本文件评估条目、改状态、写备注。

`RESEARCH.md` 是文档预算的唯一例外。规则、产品入口和线上状态仍分别只写在 `PROJECT_RULES.md`、`README.md`、`HANDOFF.md`。本文件不写生产密钥、环境路径、镜像 SHA、设备 ID 或运维细节。

## 怎么用

1. 研究助手扫描后：同一想法复用已有 id 并更新「最近更新」；新想法新增 `R-YYYYMMDD-NN`，状态先标「待评估」。
2. 开发评估后：只改「状态」「最近更新」「开发备注」。已完成 / 不做 / 废弃的行永不删除，只改状态并追加一行注明日期的备注。
3. 建议不得违反当前 SKU：半双工、无 barge-in、`advertised_duplex_level=none`、唤醒词「茉莉」。
4. 扫描更新必须是合并：禁止把本文件改回只剩标题。写入前若正文行数会大幅变少，停止并报错。

## 状态词（只准用这些）

| 状态 | 含义 |
| --- | --- |
| 待评估 | 新写入，开发尚未判断 |
| 适合做 | 已接受，尚未开工 |
| 进行中 | 正在做 |
| 已完成 | 已落地 |
| 不做 | 判断不适合（必须留一行原因） |
| 废弃 | 开过工后又放弃（必须留一行原因） |

## 条目字段

每条固定包含：稳定 id（`R-YYYYMMDD-NN`）、标题、类别（硬件 / 语音 / 产品技术 / 市场定位 / 合规）、状态、首次写入日期、最近更新、为何现在相关、建议下一步、来源 URL、开发备注。

## 编辑规则

- 同一想法再次出现时合并到原 id，禁止复制一条。
- 永不删除「已完成 / 不做 / 废弃」行；只改状态，并在开发备注追加注明日期的一行说明。
- 不写生产密钥、环境路径、镜像 SHA、设备 ID、HANDOFF 运维细节。
- 不建议违反当前 SKU：半双工、无 barge-in、`advertised_duplex_level=none`、唤醒词「茉莉」。不要建议播放期 KWS、现板谎称 AEC、或把 TurnPhase 从 shadow 改成有副作用的生产策略。

---

## 本周约束

- 扫描日期：2026-09-08。本轮新增 R-20260908-01（ES8388 供应链 EOL → 加速 VoCat，勿深改现板 ALC/AEC）。LiveKit ESP32 定制硬件指南并入 R-20260907-02，不另开 id。加强：R-20260907-01（1.8.0 含 #7064/#6865/#7104 OTel/PII）、R-20260907-02（VoCat 顺序与档案终端叙事）、R-20260903-03（征求意见截止已过，仍无定稿）、FunASR 钉档、U1、JUOS、Plaud One、Seed-TTS schema。NEW_LAW_IDS 空。云端 FunASR 仍钉 ≥1.4.14（无 1.4.15/1.5）；sidecar 仍 ≥1.3.29。
- 当前出货 / 投资人 demo SKU 只允许受控半双工（ATK ES8388 1-mic，无 AEC）；设备会话 `barge_in_enabled=false`，`interruptions_enabled=false`。不得在现板开 barge-in / 全双工 / AEC。
- 对外口径 `advertised_duplex_level=none`。未完成真实 AEC、双讲和连续轮次验收前，不得宣称全双工或持续聆听。`direct_real_device_verified` 仍为 false。
- 唤醒词默认「茉莉」，已支持白名单切换 / MultiNet 自定义词。安静环境阶段 4 已有 10/10、5 分钟误唤醒 0；电视/家庭噪声仍要记数。不要开播放期 KWS。
- 现板无 AEC reference；hello 必须 `aec_mode=none`。换 AEC 板属于阶段 8，不要混进半双工 demo 工单。xiaozhi #2036 截至 2026-09-04 仍 open（最后活动 2026-07-07）。不得用 ESP-SR `AEC_MODE_SR_*` 宣称双工或 barge-in。下一硬件 SKU 路径是 VoCat（喵伴/EchoEar，ES7210+ES8311 双麦），与 ATK 半双工 demo 分轨；见 R-20260907-02。固件树已有 VoCat overlay，仍按买板 → 最小唤醒/半双工上云 → AEC/reference → 再谈 barge-in；官方全双工定位与圆屏萌宠叙事不得抄进现板或对外档案终端故事。ES8388 停产风险见 R-20260908-01。
- ES8388 PGA 已到 21 dB（2026-09-02）。因 EOL 风险，不要在 ES8388 上深改 ALC/AEC；ATK 只做 demo。DTLN makeup 已冻结 `8.0×`，不要再抬。
- 陪伴感靠 generation fence 丢掉 thinking 中的旧 generation，不靠抢话。BOOT 是唯一硬停。LiveKit Agents PyPI 仍 1.8.0（2026-09-05）；现 SKU 必须显式 `interruption.enabled=False` 与 `preemptive_generation.enabled=False`；不要开 `user_turn_limit` 或 `expressive=True`。升级评估见 R-20260907-01（含 #7104 OTel/PII）。#7064 已进 1.8.0，不再当「等下一版」观察项。
- 云端 FunASR 钉 ≥1.4.14（见 R-20260904-01）；sidecar 仍 ≥1.3.29。H5 已移除；控制面只留小程序绑定 / 回顾 / 我的。微信个人 bot API 不是小程序控制面，见 R-20260831-14。

---

## 语音与硬件

### R-20260831-01 茉莉两音节低于 ESP-SR 3–6 音节门槛

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 为何现在相关：默认唤醒词「茉莉」只有两音节，低于 ESP-SR 定制唤醒词建议的 3–6 音节门槛。
- 建议下一步：安静环境阶段 4 已有 10/10、5 分钟误唤醒 0。下一步只补电视人声 / 家庭噪声两种环境的漏唤醒与误唤醒计数。误唤醒高则加 WakeNet 阈值，或切到 ≥3 音节词。不要开播放期 KWS。目录切词仍不算完成（见 R-20260901-01）。
- 来源：https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/wake_word_engine/ESP_Wake_Words_Customization.html ；https://github.com/espressif/esp-sr/issues/194
- 开发备注：2026-08-31 已合入白名单唤醒词切换与 MultiNet 自定义唤醒词（catalog 经设备设置下发固件，运行时选词，无需为每个词重刷）。2026-09-01 生产切流 `20260901-0945-wake-word-whitelist`、小程序 0.8.74、研发板已刷 patch `0021`；阶段 4 真机计数仍待做。2026-09-01 MultiNet 是唤醒后命令词，WakeNet 才是门卫；目录切词不算 R-01 完成。误唤醒高时用 `set_wakenet_threshold`（0.4–0.9999），或改 ≥3 音节 WakeNet。2026-09-03 HANDOFF：安静环境阶段 4 茉莉 10/10、5 分钟静音误唤醒 0；电视/家庭噪声仍未做，状态仍「进行中」。

### R-20260831-02 FunASR 空转写要分账 empty+vendor / empty+gating / low_rms

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：空转写仍在真机路径上出现。FunASR v1.3.29（2026-07-24）修的是无标点模型时 `sentence_info` 空时间轴；llama.cpp v0.2.4（2026-08-29）修的是 GGUF SenseVoice 空白，是另一条路径，不能当成云端 FunASR 已关闭。sidecar 仍钉 FunASR ≥1.3.29；云端 FunASR 钉路径见 R-20260904-01（≥1.4.14）；R-20260903-01 的 1.4.13 是前一档。
- 建议下一步：按 empty+vendor_error / empty+silent / empty+gating / low_rms 分账。同一切片不要再改 VAD。sidecar 空时间轴先核 FunASR 版本。云端钉 ≥1.4.14。不要为对齐 ASR 终点去拧设备 VAD（#3591 已接受最多一个 decode chunk 的 VAD overrun）。
- 来源：https://github.com/modelscope/FunASR/releases/tag/runtime-llamacpp-v0.2.4 ；https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.4 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.9 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.12 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.13 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.14 ；https://github.com/modelscope/FunASR/pull/3591
- 开发备注：2026-08-31 Agent 侧已加 `funasr_empty_accounting` 分账与 `funasr_empty_transcript_total` 指标；真机 receipt 仍待补。2026-09-01 sidecar 应钉 FunASR ≥1.3.29；llama.cpp v0.2.4 与云端 FunASR 空转写分账。2026-09-02 云端 FunASR 钉 ≥1.4.12（R-20260902-01），sidecar 仍 ≥1.3.29。2026-09-01 rescue 不得覆盖真实 final；empty 分账拆 vendor_error / silent；SenseVoice `language=zh`；`pause_asr_for_playback` 防 23s timeout。2026-09-03 FunASR v1.4.13（2026-09-02 15:12 CST）#3591 修「完整 partial 被锁成哎」；云端钉见 R-20260903-01。sidecar 仍 ≥1.3.29。2026-09-04 云端钉路径见 R-20260904-01（≥1.4.14）；sidecar 仍 ≥1.3.29。2026-09-07 PyPI 仍 1.4.14，无 1.4.15/1.5；云端钉 ≥1.4.14 不变。2026-09-08 PyPI 仍 `funasr==1.4.14`，`numpy<2`；无 1.4.15+。NO_CHANGE。

### R-20260831-03 远场先动 ES8388 模拟，DTLN makeup 已冻结

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：2026-09-02 已把 ES8388 PGA 从 18 dB 刷到 21 dB；30–60 cm 正常音量 DTLN 后 RMS 约 939/764，两轮可 commit+播报。数字侧 DTLN makeup 仍冻结 8.0×，再抬会削波或假装远场已解决。ES8388 另有 2026 停产风险，见 R-20260908-01。
- 建议下一步：PGA 21 dB 已落地。不要在 ES8388 上深改 ALC/AEC；远场若仍低 RMS，只对比 tap WAV pre/post DTLN，或把精力转到 VoCat（R-20260907-02）。不要再抬 DTLN，也不要为远场去开 barge-in。
- 来源：https://docs.espressif.com/projects/esp-adf/en/latest/api-reference/abstraction/es8388.html ；https://github.com/espressif/esp-adf/issues/1539
- 开发备注：2026-09-03 HANDOFF：PGA 21 dB 已刷写；远场若仍低 RMS，先 ALC/noise gate，不抬 DTLN。状态仍「待评估」（ALC 未做）。2026-09-08：因 #1539 经销商称 ES8388 将于 2026 停产，ALC 不再作为现板长期投入；ATK 只做 demo。

### R-20260831-04 陪伴感靠 generation fence，不靠抢话

- 类别：语音
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：半双工 SKU 不能靠 barge-in 制造“在听”。对照 LiveKit agents #6451（`allow_interruptions=False`）：丢掉 thinking 中的旧 generation，比抢话更接近陪伴感。LiveKit `TurnHandlingOptions`：`interruption.enabled=False` + `preemptive_generation.enabled=False`；#6858 已由 #6865 于 2026-09-01 合入 livekit/agents main；#7016 于 2026-09-02 关闭（重复）。PyPI 现为 livekit-agents@1.8.0（2026-09-05）；changelog 含 #6865 与 #7064。文档仍默认 interruption/preemptive 为开。Adaptive interruption 是 Cloud 向 barge-in 模型，现 SKU 不得启用。
- 建议下一步：屏上 idle / listening / speaking 可做。BOOT 仍是唯一硬停。半双工显式关打断与抢跑，见 R-20260902-02。1.7.1→1.8.0 升级评估见 R-20260907-01。不要因为 #6858/#6865 已进发行版就打开 preemptive_generation 或 user_turn_limit。
- 来源：https://github.com/livekit/agents/pull/6451 ；https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://github.com/livekit/agents/issues/7016 ；https://github.com/livekit/agents/issues/6858 ；https://github.com/livekit/agents/pull/6865 ；https://pypi.org/project/livekit-agents/ ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0
- 开发备注：2026-09-02 半双工要对齐 LiveKit `turn_handling`：关打断、关抢跑；#7016 仍 open，不要把默认全双工选项抄进现 SKU。2026-09-03 #6858 已关（#6865 merged）；#7016 关闭为重复。未进 1.7.1。现 SKU 仍关打断与抢跑。不要开 LiveKit `expressive=True`（1.7.0 emotion tags，踩拟人化办法）。2026-09-04 仍无发行 >1.7.1；文档仍默认 interruption/preemptive 开；半双工围栏不变。Watch #7064 等下一版再评，且勿因此开 barge-in。2026-09-07 PyPI 1.8.0（2026-09-05）已含 #6865 与 #7064（有 NC 时默认关 AGC）。现 SKU 围栏不变。#7064 只对后续 VoCat/NC 路径有参考，勿因此开 barge-in。升级评估见 R-20260907-01。2026-09-08：#7064 已合入 livekit-agents 1.8.0，不再是「等下一版」观察项。#7104 OTel/PII 见 R-20260907-01。半双工围栏不变。

### R-20260831-05 下一块 AEC 板：立创实战派优先

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：现板 ATK 无 reference，hello 必须 `aec_mode=none`。xiaozhi #2036 仍开着（2026-09-04 复核：仍 open，最后活动 2026-07-07，无新评论）。硬件 MIC3 回灌不等于 AFE 吃到参考通道。买板不能假定 MIC3 loopback 已可用，还要核 `channel_mask` 与 `aec_ref_type`（EXTERNAL_ADC vs INTERNAL）。VoCat（喵伴/EchoEar）是另一条下一 SKU 工作流，见 R-20260907-02，不要和立创阶段 8 或现 ATK demo 混单。
- 建议下一步：立创实战派（ES7210 MIC3 loopback）优先，BOX-3 其次，XMOS 更后。买板要自验 MIC3 是否进入 AEC reference。阶段 8 才买/刷 AEC 板；半双工 demo 工单不要混进新板。VoCat 全双工定位不得抄进 ATK。
- 来源：https://wiki.lckfb.com/zh-hans/szpi-esp32s3/beginner/introduction.html ；https://github.com/78/xiaozhi-esp32/issues/2036 ；https://www.cnblogs.com/wangya216/p/19455146
- 开发备注：2026-09-03 xiaozhi #2036 无新评论。阶段 7 剧本已锁定，换板仍是阶段 8。2026-09-04 xiaozhi #2036 仍 open，最后活动 2026-07-07，无新评论。2026-09-07 xiaozhi #1179（2025 ES8388 AEC 板）是历史 PR，不要据此在现 ATK demo 开 AEC/barge-in。VoCat 分轨见 R-20260907-02。2026-09-08：ES8388 停产风险（R-20260908-01）是采购理由，不是在 ATK 开 AEC/barge-in 的理由。下一主路径仍是 VoCat，立创阶段 8 备选不变。

### R-20260831-06 SenseVoice EOU 只当 sidecar 分数

- 类别：语音
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：Kazu418/sensevoice-eou 可作实验分数，补 END_CANDIDATE / clock-fact，但不能变成第二条话轮控制面。
- 建议下一步：只把 sidecar 分数喂给现有 END_CANDIDATE / clock-fact。不开 TurnPhase 生产副作用，不拉 X2-Turn。保持实验性质。
- 来源：https://github.com/Kazu418/sensevoice-eou
- 开发备注：

### R-20260831-07 记忆抄 Dense-Mem 的 confirm / trace，不新开服务

- 类别：产品技术
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：现有 PG 已有证据 / claim 生命周期。需要的是主人确认和可追溯，不是再开一套记忆服务。
- 建议下一步：把 Dense-Mem 的 confirm / trace 抄进现有 PG。主人确认走小程序；guest / uncertain 只记证据。
- 来源：https://github.com/markhuangai/dense-mem
- 开发备注：2026-08-31 `/v1/archive/memories/{claim_id}/review` 响应增加 `trace` 字段；小程序回顾页确认后展示追溯号与时间。

---

## 产品与市场

### R-20260831-08 SKU 站位：家庭桌面记忆终端 / 声纹档案音箱

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：公开价位夹在小智克隆 ¥87–199 与萤石 RK3 标准 ¥1299 / 适老 ¥2499 之间。钉钉 A1 约 ¥499/799、A1 Pro 约 ¥1299、安克×飞书约 ¥899；对照 Bubbo 主动陪伴。萤石 RK3 ¥1299/2499 避开（7 寸数字人 / 适老看护）。
- 建议下一步：公开故事写成「家庭桌面记忆终端 / 声纹档案音箱」。不是 7 寸数字人、跌倒看护或智家中枢。按需档案带见 R-20260902-05；Bubbo 反定位见 R-20260902-06。
- 来源：https://www.ys7.com/item/1004165.html ；https://www.ys7.com/item/927621.html ；https://www.donews.com/article/detail/8612/95906.html ；http://finance.people.com.cn/n1/2026/0822/c1004-40784302.html
- 开发备注：2026-09-02 价格带对照钉钉 A1 / 安克×飞书录音+转写订阅，不对照 RK3 适老看护或 Bubbo 常在情感。2026-09-04 万元级人形交付日历：优必选 U1 红星资本局（2026-09-03）称 MedTech 奖、首批约 9/16 交付、京东 Pro 约 9/15 后有货/客服预售约 60 天发货、可退定金质疑。不是档案终端样板。https://www.163.com/dy/article/L5U2PKT60511U82T.html 2026-09-07：U1 首批交付窗口约 2026-09-16 临近。网易 2026-08 中旬分析：H1 工业人形收入强，消费 U1 预售转化/退定金风险仍开；Lite 11.98 万起。memoria 反定位是 ¥2000–4000 桌面档案+陪伴语音，不是 11.98 万+ 人形。https://www.163.com/dy/article/L5G0HRMD0519MB19.html 2026-09-08：红星资本局 2026-09-03 仍有效——MedTechWorldAwards2026 获奖、9/16 首批交付临近、京东约 60 天发货、可退 3000 定金质疑；6/30 以来股价回撤 >20%。反定位不变，不新开 U1 id。

### R-20260831-09 投资人 3 分钟剧本卖证据档案

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-02
- 为何现在相关：对照 Plaud NotePin S 与 Friend 2.0（$10/月记30 天）。不演「越来越懂我」。投资人 3 分钟剧本卖：声纹门禁 + 按需档案 + 带标识导出 + 可证明删除。
- 建议下一步：剧本顺序：主人事实 → claim + 说话人 → 访客取不到 → 设备 + 小程序证据链 → 可证明删除。pitch = 声纹门禁 + 按需档案 + 带标识导出 + 可证明删除。
- 来源：https://www.plaud.ai/blogs/news/plaud-unveils-notepins-and-desktop ；https://techcrunch.com/2026/07/30/friend-the-lonely-ai-wearable-returns-with-a-new-voice-and-a-much-bigger-price-tag/
- 开发备注：2026-08-31 H5 已移除；证据链改由设备会话 + 小程序回顾 / 导出 / 删除承接。2026-09-02 pitch 钉死四件事：voiceprint gate、on-demand archive、labeled export、provable delete。

### R-20260831-10 五个吉祥物当 IP / 轻订阅 / 壳

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：华泰 2026-08：五个吉祥物当 IP / 轻订阅 / 壳。开机一个角色。
- 建议下一步：当 IP / 轻订阅 / 壳，不是五个人格大模型。
- 来源：https://www.stcn.com/article/detail/4070791.html
- 开发备注：

### R-20260831-11 主用户是家里的桌子；小孩是被门禁的说话人

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：主用户是家里的桌子，账号主人是家长；小孩是被门禁的说话人，不主打儿童陪伴机器人。
- 建议下一步：500–800 ms 童声停顿写成「不截断」，不写成「更懂情绪」。
- 来源：https://www.huxiu.com/article/4825146.html
- 开发备注：

### R-20260831-12 仪表只报已交货能力

- 类别：产品技术
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：仪表必须只报已交货能力，不报 TAM / 情感 / 没交货的订单。
- 建议下一步：报微信绑定、主人声纹登记、每周档案写入、访客拦截、导出删除、茉莉误唤醒、半双工 turn 延迟。
- 来源：
- 开发备注：2026-08-31 新增 `GET /v1/account/delivered-capabilities`；2026-08-31 扩展设备绑定 / 档案计数 / 合规字段，并在小程序「我的」页展示已交货能力仪表。

---

## 合规

### R-20260831-13 《人工智能拟人化互动服务管理暂行办法》已生效

- 类别：合规
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-09-07
- 为何现在相关：《人工智能拟人化互动服务管理暂行办法》2026-07-15 已生效。声纹是敏感生物识别。筑梦岛 CNR 2026-08-31：年龄/付费核验必须前置，不能先用后验、先充后验。
- 建议下一步：落地页二选一：家庭档案终端 vs 拟人化陪伴。学生账号禁止虚拟亲属 / 伴侣。小程序导出 / 删除；训练默认关；会话标明 AI；2 小时提醒。投研/包装话术审查；ES8388 机考虑物理/硬开关静音并在小程序显示麦状态。年龄/付费核验前置到登录与付费前。
- 来源：https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm ；https://www.news.cn/politics/20260731/26fdd0534922429bae213b5f6f3122ec/c.html ；https://www.cnr.cn/mspd/sywzl/20260831/t20260831_527800024.shtml
- 开发备注：2026-08-31 小程序登录页与「我的」页增加 AI 标识 / 家庭档案终端定位 / 训练默认关闭说明；App 前台连续 2 小时提醒；未成年人限制文案在 profile 展示。2026-09-01 新华/法治日报施行后解读：广告不得承诺「替代亲情」「治愈孤独」；家庭场景默认隐私、能本地则本地、麦/摄像头要有开启提示和便捷关闭。2026-09-02 筑梦岛 CNR 2026-08-31：age/pay verify 必须前置。2026-09-04 人民日报 2026-09-03 转载清朗二阶段同一稿（非新规）；案例仍含换声假冒与智能体查处。二级出处 https://paper.people.com.cn/rmrb/pc/content/202609/03/content_30178849.html 2026-09-07：清朗·整治AI应用乱象第二阶段进展（中证网转 2026-09-02）：累计清理违法违规信息 561 万余条、查处账号 4.9 万余个、处置违规网站/应用 2400 余个；豆包/元宝/千问/文心一言被点名强化生成合成内容标识。二级出处，非正式规章，无新法规号。https://www.cs.com.cn/xwzx/01/2026/09/02/detail_2026090210036393.html

### R-20260831-14 小程序 GTM：控制面，不承诺微信实时语音

- 类别：合规
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-09-08
- 为何现在相关：小程序 GTM 必须落在控制面，不承诺微信实时语音。微信 iLink 设备页可跳到厂商小程序控制面板（产品注册填 appid + page_path），不是微信实时语音通道。
- 建议下一步：配网 / 选角 / 声纹在设备录、微信确认、学生账号、静音、回顾与导出。家庭共享。设备页 → 小程序，不承诺微信实时语音。
- 来源：https://cloud.tencent.com/solution/smart-living ；https://iot.weixin.qq.com/doc
- 开发备注：2026-08-31 已裁至绑定 + 回顾 + 我的三 Tab；移除 home 实时语音与 H5 跳转。2026-09-02 微信 iLink 设备页落到小程序控制面，不据此开实时语音。2026-09-08：个人微信 bot API（含 iLink/ClawBot 类个人号通道）不是小程序控制面，不另开 memoria 产品路径。官方小程序定价/SKU 无变化。

---

## 2026-09-01 追加

### R-20260901-01 目录切词不是 WakeNet；茉莉仍要阈值或更长词

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-03
- 为何现在相关：昨夜已上白名单 / MultiNet 切词。乐鑫分层是 WakeNet 门卫 + MultiNet 唤醒后命令。两音节「茉莉」误唤醒不会因为目录多几个词而消失。
- 建议下一步：安静环境阶段 4 已有 10/10、误唤醒 0。下一步只补电视人声 / 家庭噪声。误唤醒高则只调 WakeNet 阈值或换成四字词，不要把 MultiNet 命令当成唤醒权威。
- 来源：https://github.com/espressif/esp-sr/issues/194 ；https://espressif-docs.readthedocs-hosted.com/projects/espressif-esp-moonlight/en/latest/speech_recognition.html
- 开发备注：2026-09-03 安静环境计数已有；目录切词仍不是 WakeNet。

### R-20260901-02 SenseVoice sidecar 核对 FunASR ≥1.3.29 时间轴

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：FunASR v1.3.29（2026-07-24）修复 SenseVoice 在无 token timestamps / 无标点时 `sentence_info` 空时间轴，并用 VAD 回填（例 610–5530 ms）。sidecar 走 SenseVoice-small；空转写/空边界先看版本，再改设备 VAD。`sentence_info` 只给小程序回顾时间轴，不当 EOU / 话轮结束。
- 建议下一步：云端与 sidecar 钉 `funasr==1.3.29`。在 sidecar 镜像记录 FunASR 版本。若低于 1.3.29 且出现空时间轴，先升级 sidecar，不要和云端 FunASR 空转写混账。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://github.com/modelscope/FunASR/pull/3414 ；https://pypi.org/project/funasr/1.3.29/
- 开发备注：

### R-20260901-03 7·15 伴侣下线后，适老/适幼是鼓励项，不是虚拟亲属

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：办法第六条鼓励适幼照护、适老陪伴。2026-07-15 施行当日行业集中下线自定义虚拟恋人。吉祥物和投资人口径仍可能滑向赛博亲人。
- 建议下一步：五个吉祥物禁止亲属/伴侣角色。落地页保持家庭档案终端。
- 来源：https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm ；https://www.kangdalawyers.com/library/5464.html
- 开发备注：

### R-20260901-04 立创买板验收：MIC3 必须进 AEC 参考通道

- 类别：硬件
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-04
- 为何现在相关：xiaozhi #2036 仍开着。硬件 MIC3 回灌不等于 AFE 吃到参考通道。
- 建议下一步：阶段 7 之后买立创时验收 MIC3 是否进入 AEC reference。未过清单不得改 hello，不得开 barge-in。
- 来源：https://github.com/78/xiaozhi-esp32/issues/2036 ；https://www.cnblogs.com/wangya216/p/19455146
- 开发备注：2026-09-03 #2036 仍 open（最后 2026-07-07）。2026-09-04 xiaozhi #2036 仍 open，最后活动 2026-07-07，无新评论。

### R-20260901-05 家庭成员邀请不能升级成主人声纹

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：腾讯连连家庭管理有邀请成员 API。微信家庭成员不等于麦克风前的 owner。
- 建议下一步：若做家庭共享，绑定只给控制面权限；写档案仍要设备端 owner 声纹。访客/成员默认 guest。
- 来源：https://cloud.tencent.com/document/product/1081/40776 ；https://cloud.tencent.com/document/product/1081/40817
- 开发备注：

### R-20260901-06 告别语义分类不能绕过主人能力门

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：昨夜已切 END_SESSION 小模型分类。阶段 5「再见」曾因主人能力不足降级。语义分类若对 guest 直接关会话，会把权限门拆掉。
- 建议下一步：精确词和语义分类都先过主人 subject capability；guest 只停公开播放或走超时。
- 来源：
- 开发备注：

### R-20260901-07 现 SKU 用 ESP-SR AEC_MODE_SR_* 只护唤醒/漏音，不宣称双工

- 类别：硬件
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：R-05 是下一块 AEC 板。现板 ATK ES8388 1-mic 无硬件 AEC。乐鑫 AEC 模式拆成 SR（线性滤波，面向唤醒）与 FD（线性+NLP，面向全双工）。官方示例 `aec_create(..., mic_num=1, AEC_MODE_SR_LOW_COST)`；AFE v2 `input_format` 用 `M+R`。这能压 TTS 漏进麦导致的误唤醒，不等于 barge-in 或 advertised duplex。
- 建议下一步：1) 现板确认能否拿到与喇叭对齐的 R 通道（数字 I2S 拷贝 vs 模拟回采）；量不到就记缺口，不硬开 FD，hello 仍是 `aec_mode=none`。2) 只评测 `AEC_MODE_SR_LOW_COST` vs `SR_HIGH_PERF`：TTS 播放中说「茉莉」的误唤醒/漏检，禁止用 FD 对外话术。3) 与 R-04 generation fence 对齐：AEC 只护听，打断仍走会话围栏。
- 来源：https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/acoustic_echo_cancellation/README.html ；https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32/audio_front_end/migration_guide.html ；https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/audio_front_end/Espressif_Microphone_Design_Guidelines.html
- 开发备注：

### R-20260901-08 长期档案：原始语音/声纹 vs 转写文本分级

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-08
- 为何现在相关：CAC 2026-01-10《互联网应用程序个人信息收集使用规定（征求意见稿）》第十五条：人脸/指纹/声纹须特定目的、最小必要；除法定或单独同意外应存于生物识别设备内、不得经互联网外传。第三十八条把小程序算进互联网应用程序。这与已落地的 AI 标识 + 2h 提醒不是同一层。仍是征求意见稿。CAC 令第25号（《小型个人信息处理者个人信息保护简化措施规定》）2026-09-01 已生效；处理声纹等敏感个人信息仍要单独同意，简化措施不免除声纹单独同意。2026-09-03《大型个人信息处理者个人信息保护规定（征求意见稿）》反馈窗口已于 2026-09-07 截止，截至 2026-09-08 仍无定稿，见 R-20260903-03。认定含处理 1000 万以上自然人个人信息。当前 SKU 按令第25号小型处理者路径。
- 建议下一步：默认云端只存转写+记忆条目；原始 wav/特征不长期留、不用于声纹登录。小程序不要为回顾开实时麦。隐私政策拆：对话文本 / 原始音频 / 是否训练。声纹单独同意，不因令第25号简化而并入一般同意。
- 来源：https://www.cac.gov.cn/2026-01/10/c_1769603446094128.htm ；https://www.cac.gov.cn/2026-01/09/c_1769688003183197.htm ；https://www.news.cn/politics/20260110/f08e925cb322436ca77d39962bd704aa/c.html ；https://www.cac.gov.cn/2026-07/24/c_1786638889704872.htm ；https://www.cac.gov.cn/2026-07/24/c_1786638889443160.htm ；https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm
- 开发备注：2026-09-02 CAC 令第25号 2026-09-01 生效；声纹仍要单独同意。合规无新法规号，本条只加强已有档案分级。2026-09-03 大型处理者征求意见未转正；声纹仍要单独同意。2026-09-08：大型处理者征求意见截止已过，仍无定稿；无新声纹专规。盯 R-20260903-03。

### R-20260901-09 2026-04 标识执法落到导出文件的显式+隐式元数据

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-08
- 为何现在相关：CAC 2026-04-28 对剪映/猫箱/即梦约谈处罚，依据含《人工智能生成合成内容标识办法》（2025-09-01 施行）。导出未加用户可感知显式标识、文件元数据未含隐式标识。小程序回顾/导出是用户语音+模型回复混合物；R-09 demo 若可下载，执法点在文件。豆包 Seed-TTS 已提供 `aigc_watermark` + `aigc_metadata` 可抄。
- 建议下一步：导出 JSON/音频包里模型侧显式「AI 生成」；元数据写服务提供者+内容编号。用户原话与合成 TTS 分轨或分字段。TTS 合成对齐 `aigc_watermark` + `aigc_metadata`。豆包 Seed-Audio 1.0（2026-06-23 FORCE）是端到端 prompt→对白+BGM+SFX，方舟 API 邀测，不是实时 Seed-TTS 对话主路径；只可作后期档案/故事/配音实验。
- 来源：https://www.cac.gov.cn/2026-04/28/c_1779119736411711.htm ；https://www.gov.cn/zhengce/zhengceku/202503/content_7014286.htm ；https://docs.volcengine.com/docs/6561/1598757 ；https://www.ithome.com/0/967/748.htm ；https://developer.volcengine.com/articles/7667459245423788075
- 开发备注：2026-09-02 豆包 Seed-TTS 用 `aigc_watermark` + `aigc_metadata` 做显式节奏标识与文件头隐式元数据。2026-09-04 清朗二阶段继续压 AI 标识落地；Seed-TTS 导出路径仍对齐 aigc_watermark + aigc_metadata（API 无新字段）。对照 R-20260831-13。2026-09-07 Seed-Audio 1.0（IT之家 2026-06-24 / 火山 2026-06-23 FORCE）端到端生成对白+BGM+SFX，方舟邀测；明确「非实时对话主路径」，不替换 Seed-TTS。清朗二阶段进展点名豆包/元宝/千问/文心一言标识落地，非正式规章。https://www.cs.com.cn/xwzx/01/2026/09/02/detail_2026090210036393.html 2026-09-08：火山文档 schema 未变（`additions.aigc_watermark` bool 默认 false；`aigc_metadata.enable`）。无新字段。无新清朗法规号 / AIGC 国标。

### R-20260901-10 华泰 2026-08：平价硬件 + 轻订阅 + 配件/家居，不是 LOVOT 强制月费

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-04
- 为何现在相关：华泰 2026-08-12：家庭陪伴机价格带约 2000–4000 元（研报数字）；不复制 LOVOT 高月费。长期档案+记忆回顾适合做轻订阅标的。订阅卖档案容量 / 家庭席位 / 导出，不是情感月费。
- 建议下一步：定价叙事对齐该价格带与半双工 SKU；订阅写成档案容量 / 家庭席位 / 导出，不是情感费。指标用 delivered-capabilities，不编留存数字。
- 来源：https://stock.10jqka.com.cn/20260812/c678877468.shtml ；https://finance.sina.com.cn/stock/stockzmt/2026-08-12/doc-inimzcpt7023259.shtml
- 开发备注：2026-09-02 订阅 = archive capacity / family seats / export，不是 emotion fee。2026-09-04 9 月无新券商笔记；价格带仍以 2026-08-12 华泰为准。

### R-20260901-11 Fun-ASR-Nano / SenseVoice GGUF 做离线档案回放，不做 ESP32 端 ASR

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：FunASR 2026-06 起 Fun-ASR-Nano 有 llama.cpp/GGUF 单二进制。适合家庭档案离线回放、方言补识别，不替代 Go Media Edge，不上 ESP32，不做实时双工。
- 建议下一步：在归档 worker（非 MCU）试 llama-funasr-cli + FSMN-VAD GGUF，与线上 FunASR 做差异抽样；说话人用 CAM++，不当成 SenseVoice 原生输出。
- 来源：https://github.com/QwenAudio/Fun-ASR/ ；https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://www.funasr.com/en/llama-cpp.html ；https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512
- 开发备注：

---

## 2026-09-02 追加

### R-20260902-01 云端 FunASR 钉 ≥1.4.12：长段 partial 保留 + Nano fp16 稳定

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-03
- 为何现在相关：FunASR v1.4.12 修长段 aligned partial 保留（#3589）并稳定 Fun-ASR-Nano fp16 vLLM 解码（#3597）。云端钉 ≥1.4.12；sidecar 仍钉 ≥1.3.29，见 R-20260831-02。
- 建议下一步：云端 FunASR 钉 `funasr>=1.4.12`。sidecar 不要跟云端一起升到 1.4.12，除非先核 SenseVoice 时间轴。2026-09-03 起云端改钉 ≥1.4.13，见 R-20260903-01。1.4.12 的长段 partial / Nano fp16 修复仍有效，被 1.4.13 包含。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.12 ；https://github.com/modelscope/FunASR/pull/3589 ；https://github.com/modelscope/FunASR/pull/3597 ；https://pypi.org/project/funasr/1.4.12/
- 开发备注：2026-09-03 后继版本 v1.4.13。本条保留为 1.4.12 档的来源记录。

### R-20260902-02 LiveKit turn_handling：半双工显式关打断与抢跑；#7016 仍 open

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-08
- 为何现在相关：半双工 SKU 不能抄 LiveKit 默认全双工。`TurnHandlingOptions` 要把 `interruption.enabled=False` 与 `preemptive_generation.enabled=False` 写死。#6858 已由 #6865 于 2026-09-01 合入 main；#7016 于 2026-09-02 关闭（重复）。PyPI 现为 livekit-agents@1.8.0（2026-09-05），changelog 含 #6865 与 #7064。文档仍默认打断/抢跑为开，并有 `user_turn_limit`（超时抢话）——现 SKU 不要设。
- 建议下一步：对照现会话 `barge_in_enabled=false` / `interruptions_enabled=false`，在 Voice Core 配置里显式关打断与抢跑。1.7.1→1.8.0 升级评估见 R-20260907-01。不要启用 `user_turn_limit` 或 `expressive=True`。不要因为发行版带上 #6865/#7064 就抄默认全双工。
- 来源：https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://github.com/livekit/agents/issues/7016 ；https://github.com/livekit/agents/issues/6858 ；https://github.com/livekit/agents/pull/6865 ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.7.1 ；https://pypi.org/project/livekit-agents/ ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0 ；https://github.com/livekit/agents/pull/7064
- 开发备注：2026-09-03 文档渲染仍默认全双工选项；#6858 修在 main 不在 1.7.1。半双工围栏不变。2026-09-04 仍无发行 >1.7.1；文档仍默认 interruption/preemptive 开；半双工围栏不变。Watch #7064 等下一版再评，且勿因此开 barge-in。2026-09-07 1.8.0 已含 #6865 与 #7064（NC 时默认关 AGC）。现 SKU 仍关打断与抢跑。#7064 只对 VoCat/NC 路径有参考。2026-09-08：#7064 已合入 1.8.0，关闭「等下一版」观察。半双工仍 `interruption.enabled=False` + `preemptive_generation.enabled=False`。#7104 见 R-20260907-01。

### R-20260902-03 半双工三态 UX：空闲 / 聆听 / 播报（对讲机模式对标）

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-02
- 为何现在相关：半双工要对标对讲机，不演持续聆听。ZEGO 互动模式拆全双工 / 聆听 / 对讲机；现 SKU 屏上应只报空闲 / 聆听 / 播报三态。
- 建议下一步：设备与小程序会话态写成 idle / listening / speaking。BOOT 仍是唯一硬停。不要加「持续聆听」或 barge-in 灯。
- 来源：https://doc-zh.zego.im/aiagent-server/advanced/interaction-mode
- 开发备注：

### R-20260902-04 可证明删除：在 Dense-Mem confirm/trace 上叠谱系擦除回执

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-02
- 为何现在相关：R-07 已有 confirm / trace。投资人剧本和第 09 条删除承诺还缺可离线核验的擦除回执。vismaran 按谱系扇出擦除并签回执；provem 在任意记忆层上做治理与擦除证书。
- 建议下一步：在现有 PG claim/trace 上叠 subject 谱系与删除回执，不新开记忆服务。小程序删除后给出可核验回执号。
- 来源：https://github.com/sambhal-labs/vismaran ；https://github.com/BernhardJackiewicz/provem
- 开发备注：

### R-20260902-05 中国按需档案带：钉钉/安克验证「录音+转写订阅」付费，不是常开陪伴

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-08
- 为何现在相关：钉钉 A1 约 ¥499/799、A1 Pro 约 ¥1299、安克×飞书约 ¥899，卖的是录音入口 + 转写时长订阅。萤石 RK3 ¥1299/2499 是 7 寸数字人/适老看护，避开。
- 建议下一步：公开价与订阅对齐按需档案带：硬件一次性 + 档案容量/席位/导出。不要卖常开陪伴月费。对照 R-20260831-08 / R-20260901-10。
- 来源：https://www.donews.com/article/detail/8612/95906.html ；https://www.ys7.com/item/927621.html ；https://techcrunch.com/2026/08/27/plauds-new-earphones-come-with-an-esim-enabled-case-for-talking-to-ai-agents/ ；https://www.plaud.ai/blogs/news/plaud-one-ai-earbuds-sell-out-us-pre-sale-in-one-day
- 开发备注：2026-09-03 奥维 2026Q2 份额仍未公开。2026-06 618 钉钉 A1 天猫/抖音/京东 AI 录音设备销量第一（量子位），不替代 Q1 额 1.4 亿 / 量 39.4 万 / PLAUD 份额 7.3% 这组数。来源 https://www.qbitai.com/2026/06/437308.html 2026-09-04：奥维 Q2 份额仍未公开；价格锚无新调价。Plaud One：IT之家 2026-09-01 机智连接 $249.99≈¥1684、限量 2000；官网发货区 US/FR/DE/UK/IT/ES/CA/NL，无中国大陆零售；Q4 发货。守桌面不跟耳机 Agent。2026-09-07：Plaud One Explorer Edition $249.99 / 2000 台美国预售已售罄（TechCrunch 2026-08-27；Plaud 官方 blog 称一日售罄）。9/4–9/7 扫描仍无中国大陆零售路径。钉钉 A1 / 安克×飞书 / 讯飞 / Bubbo / 二白Mini 官方定价无新 SKU。2026-09-08：Plaud One 仍 $249.99 / 约 ¥1684、限量 2000；官网发货区仍 US/FR/DE/UK/IT/ES/CA/NL，无中国大陆零售。NO_CHANGE。

### R-20260902-06 Bubbo / 主动陪伴竞品：写成常在情感的反定位

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-02
- 为何现在相关：心言 Bubbo 在 WRC 2026 主打主动智能陪伴、「越相处越懂你」。这与家庭桌面记忆终端 / 声纹档案音箱相反，也踩「替代亲情 / 情感灵魂」禁区。
- 建议下一步：对外话术写成 Bubbo 的反定位：不主动陪伴、不演越来越懂我、不卖常在情感。对照 R-20260831-08 / R-20260831-20。
- 来源：http://finance.people.com.cn/n1/2026/0822/c1004-40784302.html
- 开发备注：

---

## 2026-09-03 追加

### R-20260903-01 云端 FunASR 钉 ≥1.4.13：VAD overrun 不再把完整 partial 锁成「哎」；numpy<2

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-03
- 最近更新：2026-09-08
- 为何现在相关：FunASR v1.4.13 于 2026-09-02 15:12 CST 发布，叠在 1.4.12 长段 partial 保留之上。#3591 接受 decode 终点最多晚于 VAD 终点一个 realtime chunk（reporter 236 ms：partial 114430–126976 ms vs VAD 126740 ms；此前完整 partial 被锁成「哎」）。PyPI 核心依赖钉 `numpy<2`，防止 NumPy 2 ABI 导入失败。云端钉 ≥1.4.13；sidecar 仍钉 ≥1.3.29，见 R-20260831-02。不要把 Fun-ASR-Nano vLLM / Qwen3-ASR 示例当成现 SKU 实时路径（无 GPU、不上 ESP32）。
- 建议下一步：云端 `funasr>=1.4.13` 且 lock `numpy<2`。不要为对齐 ASR 终点去拧设备 VAD。sidecar 不要跟升，除非先核 SenseVoice 时间轴。2026-09-04 后继 v1.4.14，云端改钉见 R-20260904-01；1.4.13 的 #3591/numpy 仍有效且被包含。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.13 ；https://github.com/modelscope/FunASR/pull/3591 ；https://pypi.org/project/funasr/1.4.13/
- 开发备注：2026-09-04 后继 v1.4.14，云端改钉见 R-20260904-01；1.4.13 的 #3591/numpy 仍有效且被包含。2026-09-07 PyPI 仍 1.4.14，无 1.4.15/1.5。2026-09-08 PyPI 仍 `funasr==1.4.14`，`numpy<2`；无 1.4.15+。NO_CHANGE。

### R-20260903-02 二白Mini：声纹认主+生命模块是拟人化反例，不是档案门禁样板

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-03
- 最近更新：2026-09-03
- 为何现在相关：杭州二白智能（2025-12 / 2026-01 千万天使轮，CES 2026 与声网同台）产品二白Mini 走「领养-训练-认主」，声纹+人脸绑定数字生命，核心数据放可插拔「生命模块」。这与家庭桌面记忆终端 / 声纹档案音箱相反，也踩拟人化办法的情感依赖 / 虚拟生命话术。memoria 声纹只做主人门禁与档案归属，记忆是 claim/trace/可证明删除，不是可迁移灵魂。对照 Bubbo（R-20260902-06）。
- 建议下一步：对外话术把「声纹」钉死为门禁+档案归属。禁止「认主养成」「生命模块」「有灵魂的生命体」「越相处越懂你」。五个吉祥物仍是 IP/轻订阅/壳（R-10），不是领养对象。
- 来源：https://www.36kr.com/p/3608882030593282 ；https://finance.sina.com.cn/tech/roll/2026-01-12/doc-inhfzysp6769203.shtml
- 开发备注：

### R-20260903-03 《大型个人信息处理者个人信息保护规定（征求意见稿）》征求意见截止已过，仍无定稿；当前按小型处理者

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-03
- 最近更新：2026-09-08
- 为何现在相关：网信办 2026-08-07 征求意见，意见反馈截止 2026-09-07。截至 2026-09-08 扫描：征求意见截止已过，仍无定稿、延期公告或认定口径。认定条件含处理 1000 万以上自然人个人信息。现行令第25号（《小型个人信息处理者个人信息保护简化措施规定》）2026-09-01 已生效；声纹等敏感个人信息仍要单独同意。当前 SKU 不要按千万级「守门人 / 外部监督委员会」扩编合规组织。不另开法规 id。
- 建议下一步：继续盯正式文本或认定口径，不要另开法规 id。用户规模远低于 1000 万时继续令第25号简化路径，声纹单独同意不并入一般同意。清单化告知与敏感个人信息单独同意仍有架构卫生价值。对照 R-20260901-08。
- 来源：https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm ；http://legalinfo.moj.gov.cn/pub/sfbzhfx/zhfxfzzx/fzzxyw/202608/t20260808_538357.html ；https://www.news.cn/politics/20260807/e990e3956d174041842ef9d62e98d2e5/c.html ；https://hongkong.dentons.com/en/insights/articles/2026/september/2/large-and-small-personal-information-handlers-each-have-their-own-path-forward
- 开发备注：2026-09-04：征求意见仍开至 2026-09-07；无正式规章/延期公告。2026-09-07：官方通知反馈截止仍为今日；扫描未见正式规章、延期公告或新法规号。memoria 远低于 1000 万 PII 门槛。令第25号声纹单独同意已生效，无新声纹专规 / 未成年人陪伴专法 / 静音摄像指示灯强制。2026-09-08：征求意见截止已过，仍无定稿。新华社稿与 Dentons 2026-09-02 解读仍指向大小处理者分轨，非正式规章。NEW_LAW_IDS 空。无新清朗法规号 / 声纹专规 / AIGC 国标 / 未成年人陪伴法。

---

## 2026-09-04 追加

### R-20260904-01 云端 FunASR 钉 ≥1.4.14：默认 partial 窗 8s + --enforce-eager；仍 numpy<2

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-04
- 最近更新：2026-09-08
- 为何现在相关：FunASR v1.4.14 于 2026-09-03 16:24 UTC 发布（叠在 1.4.13 #3591 之上）。#3632 默认 interim decode window 切到 8s（`--partial-window-sec` 仍可调），final 仍全段；#3631 增加 funasr-realtime-server `--enforce-eager`（CUDA eager、无 CUDA graph）降 VRAM/排障。仍钉 numpy<2。配套 runtime-llamacpp-v0.2.6 未变。MOSS-Transcribe-Diarize 可发现性增强只影响离线档案路径，不是现 SKU 实时半双工。sidecar 仍 ≥1.3.29。
- 建议下一步：云端 `funasr>=1.4.14` 且 lock `numpy<2`；若用 funasr-realtime-server，先 A/B partial 窗，仅在 OOM/排障时试 `--enforce-eager`。不要为对齐 ASR 终点去拧设备 VAD。sidecar 不要跟升除非先核 SenseVoice 时间轴。对照 R-20260903-01 / R-20260831-02。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.14 ；https://pypi.org/project/funasr/1.4.14/ ；https://github.com/modelscope/FunASR/pull/3632 ；https://github.com/modelscope/FunASR/pull/3631 ；https://github.com/modelscope/FunASR/compare/v1.4.13...v1.4.14
- 开发备注：2026-09-06 从草稿 PR #9 / `cursor/research-scan-20260904-3084` 补回主分支；条目原文未改，仅记录合入日期。2026-09-07 PyPI 仍 1.4.14，无 1.4.15/1.5；云端钉 ≥1.4.14 不变。2026-09-08 PyPI 仍 `funasr==1.4.14`，`numpy<2`；无 1.4.15+。NO_CHANGE。

### R-20260904-02 海信 JUOS：家庭智能伴侣级 AIOS / 全屋中枢是反定位，不是档案终端样板

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-04
- 最近更新：2026-09-08
- 为何现在相关：海信 2026-08-31 发布「行业首个家庭智能伴侣级 AIOS」JUOS；报道称从被动响应迈向主动服务，超级小聚全时段陪伴，AI 个性桌面千人千面（人脸/声纹识别成员），联动电视/投影/全屋家电，首批机型 9 月起推送。这是客厅大屏/全屋 OS 入口，与家庭桌面记忆终端 / 声纹档案音箱相反，也踩「更懂家 / 全时段陪伴」叙事。对照 Bubbo（R-20260902-06）、二白Mini（R-20260903-02）、R-20260831-08/20。
- 建议下一步：对外话术钉死「按需证据档案 + 声纹门禁」，禁止「家庭智能伴侣 OS」「全时段陪伴」「千人千面开机桌面」「全屋入口」。不把电视 OS 当竞品抄产品，只当反定位日历。
- 来源：https://finance.sina.com.cn/jjxw/2026-09-03/doc-iniqpqxy2136783.shtml ；https://finance.sina.com.cn/jjxw/2026-09-01/doc-iniqhwmx8252279.shtml ；https://www.163.com/dy/article/L5M36U1E051191D6.html ；https://www.3elife.net/Art/ie/202609/04/109709.html
- 开发备注：2026-09-06 从草稿 PR #9 / `cursor/research-scan-20260904-3084` 补回主分支。2026-09-07：9 月推送已开始覆盖海信 U/E/A 与 Vidda 中高端；「小聚识人」为人脸+声纹 opt-in 登记，官方称不默认采集。反定位不变：电视/家庭 AIOS ≠ memoria 桌面长期档案终端。三易生活 https://www.3elife.net/Art/ie/202609/04/109709.html ；新浪 2026-09-03 仍有效。2026-09-08：三易生活 2026-09-04 深挖仍有效——「小聚识人」opt-in 非默认；声纹或人脸二选一，用户可跳过生物识别；9 月起推送。反定位加强：客厅大屏生物识别开机 ≠ 桌面档案终端 + 设备端声纹门禁。

### R-20260904-03 Microduck $399 桌面具身玩具热度 ≠ 中国家庭记忆终端

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-04
- 最近更新：2026-09-07
- 为何现在相关：Pollen Robotics Microduck 预售 $399，4 天约 10500 单、销售额 >$400 万（每经 2026-09-02）；高 25cm、<800g；算力瑞芯微 RK3566；开源运动/sim-to-real，不主打人类语言对话；交付排期拉长至 4–6 个月。海外桌面具身玩具/开发板热度不能写成 memoria 定价或形态依据。
- 建议下一步：对外不跟 $399 运动机器人叙事；继续卖声纹门禁 + 按需档案 + 半双工桌面终端。对照 R-20260831-08。
- 来源：https://www.nbd.com.cn/articles/2026-09-02/4570682.html ；https://www.cnbc.com/2026/09/01/hugging-faces-new-duck-robot-is-selling-fast-a-chinese-chip-powers-it.html ；https://www.ithome.com/0/996/199.htm
- 开发备注：2026-09-06 从草稿 PR #9 / `cursor/research-scan-20260904-3084` 补回主分支。2026-09-07 CNBC 2026-09-01：订单 >10k、销售额超 $5M（周二晚）；新订单无法保证 2026 圣诞，交期滑到 4–6 个月。IT之家 2026-08-30 已报结账页横幅「新订单预计 4 至 6 个月」。反定位仍是开源玩具双足 ≠ 中国家庭记忆终端。

---

## 2026-09-06 追加

### R-20260906-01 小程序体验研究与移动端交互提案

- 类别：产品技术
- 状态：进行中（研究与 HTML 已交付，跨端修复已部署/上传，开发工具通过，手机及电脑微信待验）
- 首次写入：2026-09-06
- 最近更新：2026-09-06
- 为何现在相关：用户要求审视现有小程序、对照同类手机产品、从首用者角度提出修改并交付可交互 HTML；同时解决手机有设备、电脑微信和开发工具无设备的问题。
- 结论：优先让设备与账号事实一致、启用流程能继续、设置有保存反馈、家庭邀请能闭环，再改视觉。保留「设备端使用 + 手机管理与回顾」定位和四主 Tab，不把控制面改成聊天 App。
- 证据边界：本轮审查了 10 页源码；开发工具实际访问的页面以可访问性树和运行反馈为证。竞品来自官方介绍、帮助中心与商店说明，不是安装 App 后的逐页实测。截图已保存，但当前图片输入不可用，未完成逐像素视觉审查；不据此声称完整视觉或无障碍验收。
- 建议下一步：先确认 Control API 与小程序的协同发布，再验证 PostgreSQL 权限隔离和同微信手机/电脑/开发工具三端；其余产品建议按下列 P1/P2 落地。

#### 竞品对照（官方可核内容与本产品建议分开）

| 产品与官方来源 | 官方页面可核内容 | 对 Memoria 的设计判断；不是竞品完整页面地图 |
| --- | --- | --- |
| [Google Home](https://home.google.com/about-google-home/) | Favorites 常用设备/操作、Activity、Home Brief、Automations、Ask Home | 借鉴「常用操作 + 当前状态 + 最近活动」的分层。首页只保留当前设备、一个待办和最近回顾，不照搬家居中枢和自动化平台。不同地区、订阅和设备的可用性不可外推。 |
| [米家官方商店说明](https://apps.apple.com/cn/app/id957323480) | 设备添加、联动、场景、家庭分享及商城 | 借鉴可发现的设备入口和家庭分享概念；本产品提出分步连接与单独授权，不将这些提案说成米家逐屏操作实测。商城与海量设备分类不是当前重点。 |
| [ElliQ Caregivers](https://elliq.com/pages/caregivers) | 家属 App 演示、心情/疼痛变化提醒、饮水/用药与 wellness goals、最近活跃、照片、trusted contacts；官方注明非医疗设备 | 家人需要看懂「最近是否使用、是否有值得关注的变化」。建议 Memoria 只在单独授权后展示基础趋势；「不共享对话原文」是本产品的权限设计建议，不归因为 ElliQ 的已证策略，不作医疗判断。 |
| [LOVOT App](https://help.lovot.life/app/) / [Diary](https://help.lovot.life/app/diary/) | 多机切换、网络/电池等机器人状态、睡眠时间、邀请接受；日记含相册、地图、时间线与月/日选择 | 借鉴亲和的设备状态与按日期回顾。机器人 health 不是用户医疗健康；不照搬地图、摄像头、定位轨迹或订阅模式。Memoria 支持账号多设备发现，不是单设备账号模型。 |

上述是功能与内容组织研究；对精确布局、动效、色彩与点击步数没有足够一手截图证据，不做逐屏排名或伪精确打分。

#### 当前 UI、交互、功能与文案判断

- 可保留：四主 Tab 已有稳定入口，五个角色提供品牌识别，设备/说话人/权限分离的方向正确。重设计不应推倒这些基础。
- UI：`app.wxss` 的近黑底、半透明模糊卡片、霓虹描边、发光文字与渐变按钮共同争夺注意力。判断是更适合强调角色形象而非长时间阅读回顾；本提案将深蓝集中在设备主卡，其他区域使用明亮底色、列表和留白。不是基于未看见截图声称对比度已不合格。
- 交互：需要区分未登录、同步中、同步失败、确认为空、待选设备、已绑定但离线。不能让用户用重新绑定来处理同步失败，也不能让开关外观替代服务端保存结果。
- 功能：「我的」运行页面同时堆叠能力矩阵、五种人格、自定义人格编辑/录音、声纹、偏好、账号危险操作，层级过深。建议一级页只给入口与当前结果，把角色/声音、隐私、家庭与诊断分开；不要增加新的主 Tab。
- 文案：`Runtime Profile`、`service_mode`、`readiness`、`none` 等可以留在折叠诊断，不让新用户据此判断能不能用。「不是拟人化陪伴服务」与「你的陪伴空间」、角色描述同时出现，定位表达需统一；正向说明用途，正式定位与服务协议仍需产品负责人确认。

#### 10 页逐项审查（静态发现不冒充运行复现）

下列路径均位于 `apps/miniprogram/pages/`；除账号同步及其状态反馈外，本轮没有将这些建议直接批量改进正式页面。

| 页面 / 主要功能 | 证据与不足 | 修改建议 / 优先级 |
| --- | --- | --- |
| `home` 首页与添加入口 | `index.js:107` 添加入口固定 `fresh=1`，绕过已有启用会话恢复；这不是首页数据不刷新。旧设备发现依赖本地缓存的问题已修复并发布开发版，开发工具已找回设备。 | 账号设备自动同步优先于新设备流程（P1，已发布开发版，三端验收未齐）；有未完成会话时显示「继续上次启用」，重新开始另行确认（P2）。 |
| `device` 设备与使用者状态 | `index.wxml:97` 在模板中调用 `Math.round` 计算置信度，存在模板能力适配风险；尚未触发该数据分支运行验证。 | JS 预计算展示值；默认展示可用状态，把置信度等移入诊断。模板风险先定向验证，不标成已观察到的错值（P2）。 |
| `memory` 每日回顾与记忆 | `onPullDownRefresh` 已定义，但页面 JSON 未启用 `enablePullDownRefresh`；是下拉刷新接线缺失，不是下拉选择框复杂。 | 明确刷新入口与反馈；AI 草稿、已确认记忆分区，支持来源/修改/不保存/撤销（P2）。 |
| `profile` 偏好、声音、账号 | `index.js:697–700` 开关只 `setData`；保存在选角色或保存自定义人格时触发。单改开关可能重进后被服务端旧值覆盖。运行页还显示大量技术状态。 | 开关即时保存并反馈失败/恢复旧值，或明确保存按钮，尤其过滤/自动回顾设置不能假成功（P1）；拆分入口而非长页堆表（P2）。 |
| `guardian` 家庭/监护邀请 | `index.wxml:35` 要求从孩子「我的」页找账号 ID，但该页没有对应展示。当前邀请说明缺可执行的查找路径。 | 设计微信内邀请与接受闭环，并对接真实监护验证；不可用邀请按钮模拟授权成功（P1）。 |
| `auth` 微信登录与说明 | 否定式定位说明与其他页面的陪伴/角色语言不一致，用户难判断登录后能做什么。 | 首屏正向说明「连接设备、查看回顾、管理资料」，授权说明按用途出现（P2）。 |
| `digital-self` 档案与成长 | Persona/readiness/status 等工程概念暴露；存在刷新处理函数但 JSON 未启用下拉刷新。 | 面向用户改为「我的记忆档案」，解释来源、本人确认与修改历史；能力状态留在诊断（P2）。 |
| `privacy` 授权与数据 | `index.wxml:13` 在 text 中使用 `<br />`，存在小程序组件适配风险；该页控制范围主要围绕原始语音。 | 分组管理回顾、声纹、声音样本、共享、导出与删除；换行改原生可支持写法并真机核验（P2）。 |
| `bind` 使用模式与资料 | `index.js:369–461` 校验必填 `familyName`/`familyDrafts`，但 `_buildRequest` 未提交这些内容；`adminIsBuyer` 也未进入请求。用户填写不等于形成正式家庭关系。 | 删除无效必填或补齐权威接口与保存证据；家庭成员可在连接后完善，不能收集后丢弃（P1）。 |
| `device-onboarding` 连接/配网/启用 | `index.js:69–96` 在 fresh 模式跳过 stored session 自动恢复；`.wifi-list` 的 `max-height:360rpx; overflow:hidden` 可能令列表后部网络不可达，尚未用多网络运行验证。 | 保留恢复入口；网络列表可滚动并支持手输，失败留在当前步骤；不是已确认的长 SSID 撑破（P2）。 |

另有现存规则冲突：`app.json` 含 `scope.record`，`profile/index.js` 使用 RecorderManager 和录音授权，与 `PROJECT_RULES.md` 的控制面约束不一致。本轮没有触发录音、删除这段已有实现或擅自重定产品边界；需单独确定迁移方案。HTML 不接入录音。

#### 把自己当作首次使用者（旅程、健康度与下一步）

1. **打开：方向不够清楚。** 我首先想知道这是设备遥控器、聊天工具还是档案工具，不想读合规能力表。首页先讲「在设备上使用，在手机上查看和管理」。
2. **登录找设备：原路径有阻断，开发工具已真实找回，手机/电脑待验。** 同微信换端应恢复设备；只有服务端成功返回空列表才说没有绑定，失败提供重试而非重新配网。
3. **新设备启用：已有流程，但恢复入口不足。** 连接设备 → Wi-Fi → 使用者 → 按需身份确认 → 完成；进度可继续，密码离页清空，成功以设备回执为准。
4. **给家人使用：邀请闭环与填写结果需要补齐。** 购买者、管理员、使用者不等同；父母本人确认，儿童监护独立验证，关系确认不自动共享内容。
5. **日常回顾：基本框架可用，需要降低理解成本。** 优先展示待确认内容与最近回顾，按日期找记录；不以人格工程指标代替用户可理解的记忆条目。
6. **改设置/退出：保存反馈与权限解释不足。** 每项开关都有明确保存结果；声纹身份识别与声音复刻分开，危险操作二次确认。

实施顺序：P1 先补齐账号发现三端验收、家庭邀请/无效必填、偏好保存；P2 再处理恢复入口、刷新/模板/列表适配、信息层级和视觉。未复现的静态风险先验证，不按已发生线上故障定级。

#### 建议文案（产品提案，不是现有能力承诺）

| 场景 | 建议显示 | 主操作 |
| --- | --- | --- |
| 设备同步失败 | 设备信息暂未同步。这不代表未绑定，无需重新配网。 | 重新同步 |
| 设备离线 | 设备暂时离线。已同步的回顾仍可查看，绑定关系不受影响。 | 检查连接 |
| 中断启用 | 上次已完成连接网络，继续设置使用者。 | 继续上次启用 |
| 记忆待确认 | 1 条内容，等你确认。确认后才会加入记忆档案。 | 查看并确认 |
| 家庭共享 | 关系已确认，尚未共享内容。你可以单独选择共享范围。 | 管理共享范围 |
| 声纹与声音 | 声纹用于识别本人；自定义声音用于设备表达，两者分开授权。 | 分别管理 |

#### 可交互 HTML 与验收边界

- 文件：`apps/miniprogram/design-preview/memoria-mobile-redesign.html`。10 个现有业务视图映射，加 1 个「角色与声音」视图，共 11 个；四主 Tab 为首页/设备/回顾/我的。单文件约 477 KiB，内嵌五个现有角色 WebP、图标及许可证；不依赖图片 CDN。
- 视觉方向：瓷白 `#f4f6f8`、深蓝 `#172f43`、墨色 `#192f41`、海玻璃绿 `#326e70`、浅绿 `#b7dad3`。角色保留亲和感，正文左对齐；主要按钮、行入口、弱提示分级，深蓝只强调当前设备。
- 六个模拟场景：已绑定在线、离线、同步失败、确认未绑定、多设备待选、未登录。顶部「交互原型」切换场景和重置，桌面两侧展示导航与设计说明。
- 已实际操作：登录同意门禁；找回/切换设备；同步失败重试；Wi-Fi 校验/显隐/离页清空；使用者选择；关系确认、另行共享授权及撤销；成人四段逐项同意和声纹撤回；儿童不登记成人主人声纹；记忆修改/确认/不保存/撤销；日期/搜索空态；偏好刷新保留；五角色切换；原始语音单独确认；二次清空与重置。
- 导出已验证浏览器真实下载：JSON 含 1 条待确认、1 条已确认的虚构记录，不含账号令牌、设备 ID 或密码。保存至忽略目录 `outputs/miniprogram-ux-20260906/memoria-demo-export.json`。
- 几何检查：四主页面 × 六视口（320×740、375×812、390×844、430×932、844×390、1440×1000）共 24 项，加 7 个次级页面在 320×740 共 31 项通过；无页面/内容水平溢出，每页一个 h1，角色图片加载正常，非 checkbox 控件未发现小于 24px。不能据此声称所有触点 ≥44px 或完整无障碍合规。成人/儿童分支已有定向复验；收尾设备名称改为「我的星澜 / 家人的绵绵」，另验 320px 首页/设备页无水平溢出、角色图片加载正常。
- 浏览器 warn/error 日志为空；源码脚本语法与文档门禁通过，原型已回首页交付。旧截图保留更名前的房间标签，命名复验以新增 JSON 为准；截图及几何 JSON 在 `outputs/miniprogram-ux-20260906/`；图片输入不可用，因此视觉仍待人工复核。
- HTML 是交互设计，不是接好生产的微信小程序。CSP 禁止连接请求，不登录真实微信、不录音、不控制设备、不改变生产数据。声纹流程只模拟进度；真实邀请、共享、权限、声音试听与设备回执仍须后端接线，不能以演示状态代替验收。示例使用者/偏好是原型级状态，不是正式多设备用户模型。

#### 跨端设备修复与工程状态

- 已确认代码根因：旧入口依赖各端隔离的 `memoria:miniprogram:device:binding-manifest`，没有账号级绑定发现。手机本地有缓存不能让电脑自动取得绑定。不同账号/版本仍需三端验收时核对，尚未现场比对手机账号。
- `code`：新增 `GET /v1/device-bindings` 及 repository/service 实现，只列当前账号 owner 或 active role 可见的 canonical BindingManifest；缺 actor 拒绝，不将设备归属提升为声纹或敏感权限。三个同步重试入口先恢复登录，失败进入 guest，并废弃迟到请求。
- `wired`：首页、设备、我的与无缓存敏感入口调用账号同步；有 `ready / choose / empty / cached / error`，另有同步中/未登录展示。auth epoch、context revision 与流程序号防止旧请求覆盖新账号/设备；错误用中文解释，不诱导重新配网。
- `enabled`：后端已部署，开发版 **0.8.83** 已上传（在线状态接实时连接、瓷白控制面、无角色立绘，源 `0f5632a`）。正式审核/发布与体验版设置未做。源码与发布收据只记在 `HANDOFF.md`。
- `verified`：小程序单测 211/211、DevTools CLI 上传成功。0.8.83 尚未在手机/电脑微信或开发工具做真实验收；不得把 0.8.77 的 095c 开发工具结果说成本版三端通过。
- 已知边界：手机/电脑微信最终版未实测；真实过期登录恢复只有单测，未篡改 token 强行演示。「我的」仍显示 Runtime Profile 获取失败，日志有 HTTP 404，路由或业务资源来源未定，敏感入口继续关闭。公网 ready 因既有 smoke 证据过期返回 503，候选 smoke 成功不等于栈级记录刷新；未盲目回滚、修改 TTL 或伪造通过。
- 待验收：同微信手机、电脑微信和开发工具的设备一致；多设备选择、撤销绑定、换账号及迟到响应的客户端回归；独立定位运行配置错误，并在明确范围后真实刷新生产 smoke。未进行绑定重置、硬件或声纹操作。
- 开发备注：正式 UI 的其他问题保留为建议，未批量重写；HTML 是前端交互设计而非后端能力承诺，完整视觉人工复核与三端验收未完成。当前 SKU 仍遵循受控半双工，不宣传全双工、持续聆听或抢话。


---

## 2026-09-07 追加

### R-20260907-01 Voice Agent 评估 LiveKit Agents 1.7.1→1.8.0；半双工围栏不变

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-08
- 为何现在相关：PyPI livekit-agents 仍 1.8.0（2026-09-05；现仓仍钉 1.6.10）。changelog 确认 #6865（取消 parked preemptive generation）与 #7064（有 noise cancellation 时默认关 AGC）已落在 1.8.0，不再是「等下一版」。另有破坏性 #7104：全面 OTel GenAI semantic conventions + 进程内 PII 剥离——对话内容不再作为 span events；项目级 redaction 开启时，内容在导出前剥离（含 LiveKit Cloud）。Agent 已有可选 OTLP 导出，升级要评这条。Adaptive interruption 是 Cloud 向 barge-in 模型，现 ATK 半双工 SKU 不得启用。
- 建议下一步：评估 1.6.10/1.7.1→1.8.0 时先读 changelog 与现有半双工配置，并核 OTel/PII：若导出 traces，确认对话原文不再进 span events、redaction 与现有低基数 telemetry 不冲突。现 SKU 保持 `interruption.enabled=False` 与 `preemptive_generation.enabled=False`，不要开 `user_turn_limit` 或 `expressive=True`。#7064 只对后续 VoCat/NC 路径有参考，不要因此在 ATK 开 barge-in。对照 R-20260902-02 / R-20260831-04 / R-20260907-02。
- 来源：https://pypi.org/project/livekit-agents/ ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0 ；https://github.com/livekit/agents/pull/7064 ；https://github.com/livekit/agents/pull/6865 ；https://github.com/livekit/agents/pull/7104 ；https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://docs.livekit.io/deploy/observability/pii-redaction/
- 开发备注：2026-09-08：#7064/#6865 已在 1.8.0。#7104 是升级评估的合规/观测面，不是开打断的理由。半双工围栏不变。

### R-20260907-02 VoCat（喵伴/EchoEar）是下一硬件 SKU，勿混入 ATK 半双工 demo

- 类别：硬件
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-08
- 为何现在相关：乐鑫文档仍列 ESP-VoCat（EchoEar）：ESP32-S3、1.85 寸圆屏、双麦阵列、ES7210 ADC + ES8311 codec，官方定位全双工语音交互。这是下一 SKU 工作流，不是当前 ATK ES8388 1-mic 半双工 demo 板。barge-in、降噪、声纹、全双工只挂这条路径，不得提前写进现板 hello 或投资人 demo。ES8388 停产风险（R-20260908-01）只加速 VoCat 采购，不改变现板半双工围栏。LiveKit 官方 ESP32 定制硬件指南（Waveshare ESP32-S3-Touch-LCD-1.83，同为 ES8311 DAC + ES7210 ADC TDM，`esp_capture_new_audio_aec_src`）可作 VoCat AEC/初始化参考；8-bit I2C 地址坑：ES7210 `0x80` / ES8311 `0x30`。
- 建议下一步：VoCat 另开工单，顺序固定为买板 → 最小唤醒/半双工上云 → AEC/reference → 再谈 barge-in。不要改 ATK hello / barge_in / AEC 声明。现板继续 `aec_mode=none`、半双工。对外叙事仍是桌面档案终端，不是圆屏萌宠。立创实战派仍是阶段 8 AEC 备选（R-20260831-05），与 VoCat 分轨。xiaozhi #1179 是历史 PR，不要据此在现 ATK demo 开 AEC/barge-in。
- 来源：https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp-vocat/index.html ；https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp-vocat/user_guide_v1.2.html ；https://livekit.com/blog/esp32-custom-hardware-quickstart
- 开发备注：2026-09-08：固件树已有 VoCat overlay，仍与 ATK 投资人 demo 分轨。LiveKit 指南只作 ES7210/ES8311 + AEC 参考，不把 Waveshare 圆屏产品叙事抄进 memoria。

### R-20260907-03 SiphonAI 可借鉴媒体/AI 分层与协议工程，不换栈

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-07
- 为何现在相关：[SiphonAI](https://github.com/thevoiceguy/siphon-ai) v0.51.0 是 SIP↔WebSocket 媒体桥（MIT/Apache），daemon 内禁止 STT/LLM/TTS。与 Memoria「Go Media Edge 不调模型、Python 才是交互权威」同构，但入线是电话 RTP 不是 ESP32 media-v2。值得学的是协议当公开 API、对端 conformance harness、20 ms 热路径纪律、WS/AI 掉线保会话、路径质量进话轮、升级前 config check、用数字写容量。generation fence、Actual Heard、主人声纹已比它的 seq/mark 更严，不要退化。
- 建议下一步：现 SKU 只评估不改产品行为的四件事：(1) 假 ESP32 客户端打真实 Edge 的 media-v2 harness；(2) Bridge/Agent 宕机时 Edge 本地短提示 + 有界重连，超时再 typed close；(3) WSS 丢帧/乱序投影进 ConversationProjection UNCERTAIN，不改 commit；(4) 切流 dry-run 验未知配置键与半双工围栏。不要引入 siphon-rs/forge-media。打断 pause 模式只对照 R-20260907-02 / 阶段 8。
- 来源：https://github.com/thevoiceguy/siphon-ai ；https://github.com/thevoiceguy/siphon-ai/blob/5e8f02ead7dfbb6ca14b471ab7b50841729a8bf0/docs/PROTOCOL.md ；https://github.com/thevoiceguy/siphon-ai/blob/5e8f02ead7dfbb6ca14b471ab7b50841729a8bf0/CLAUDE.md
- 开发备注：

### R-20260907-04 电话入线可把 SiphonAI 当 SIP sidecar，Memoria 做 WS server

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-07
- 为何现在相关：SiphonAI 明确不做 AI，只把 SIP/RTP 变成 20 ms PCM16 + JSON 控制。若以后要「打电话进陪伴」，这是现成组件，不必自研 SIP 栈。身份模型不同：电话是主叫号码 / STIR，Memoria 是主人声纹；不得把 PSTN 腿标成 owner。现板半双工 demo 不需要电话入线。
- 建议下一步：不混入 half_duplex_investor_demo。若产品确认要 PSTN，另开工单：部署 siphon-ai，Voice Core 实现其 WS 协议（可用官方 Python SDK 做适配层），映射到既有 generation fence；guest/uncertain 权限默认拒绝私人记忆与工具。先不要改 ESP32 协议。
- 来源：https://github.com/thevoiceguy/siphon-ai ；https://github.com/thevoiceguy/siphon-ai/blob/5e8f02ead7dfbb6ca14b471ab7b50841729a8bf0/docs/PROTOCOL.md ；https://github.com/thevoiceguy/siphon-ai/tree/5e8f02ead7dfbb6ca14b471ab7b50841729a8bf0/sdks
- 开发备注：

---

## 2026-09-08 追加

### R-20260908-01 ES8388 供应链 EOL：加速 VoCat，勿深改现板 ALC/AEC

- 类别：硬件
- 状态：待评估
- 首次写入：2026-09-08
- 最近更新：2026-09-08
- 为何现在相关：espressif/esp-adf#1539（2025-09-24 开，2026-03-10 评论）：经销商称 ES8388 将于 2026 停产；社区问 ES8390；并记 ES8388 调音量/启停爆破音。当前投资人 demo SKU 是 ATK ES8388 1-mic 半双工、无 AEC。这是采购/生命周期风险，论证不要长期押 ES8388，并保持 VoCat（ES7210+ES8311）为下一主 SKU——**不是**在现板开 barge-in/AEC 的理由。对照 R-20260907-02 / R-20260831-03。
- 建议下一步：把 ES8388 EOL 写成采购风险；ATK 板只做 demo，不长期投入。继续 VoCat：买板 → 最小唤醒/半双工上云 → AEC/reference → 再谈 barge-in。不要在 ES8388 上深改 ALC/AEC，也不要把停产或爆破音写成现板全双工借口。
- 来源：https://github.com/espressif/esp-adf/issues/1539
- 开发备注：

---

## 明确不做

### R-20260907-05 用 siphon-ai / forge-media 替换 Go Media Edge，或现板开 auto_clear barge-in

- 类别：产品技术
- 状态：不做
- 首次写入：2026-09-07
- 最近更新：2026-09-07
- 为何现在相关：SiphonAI 是 SIP/RTP daemon，设备链是 media-v2 WSS。替换 Edge 等于拆 `ESP32 → Go Media Edge → Python Voice Core`。其默认 `auto_clear` 在无 AEC 板上会把回声当抢话。
- 建议下一步：无。电话入线见 R-20260907-04（另开产品）。协议借鉴见 R-20260907-03。
- 来源：https://github.com/thevoiceguy/siphon-ai
- 原因：不拆现权威链；现板 `aec_mode=none`、`barge_in_enabled=false`；BOOT 仍是唯一硬停。与 R-20260831-16 同类。
- 开发备注：

### R-20260831-15 现板开 barge-in / TurnPhase 副作用 / 播放期 KWS / 谎称 AEC

- 类别：语音
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：现板开这些能力会违反当前 SKU。
- 建议下一步：无。
- 来源：
- 原因：现板半双工、无 barge-in、无播放期 KWS、hello 必须 `aec_mode=none`，不得谎称 AEC。
- 开发备注：

### R-20260831-16 用 LiveKit client-sdk-esp32 替换 Go Media Edge

- 类别：产品技术
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：会替换当前 Go Media Edge 权威链。
- 建议下一步：无。
- 来源：
- 原因：不拆 `ESP32 → Go Media Edge → Python Voice Core`。
- 开发备注：

### R-20260831-17 现板打开 USE_REALTIME_CHAT / SeekAudio AEC

- 类别：硬件
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：现板无已验证 AEC。
- 建议下一步：无。
- 来源：
- 原因：现板不得开 USE_REALTIME_CHAT / SeekAudio AEC。
- 开发备注：

### R-20260831-18 再抬 DTLN makeup

- 类别：语音
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：DTLN makeup 已冻结 8.0×。
- 建议下一步：无。低 RMS 先比 tap WAV pre/post DTLN。
- 来源：
- 原因：再抬 DTLN makeup 会掩盖前端增益问题。
- 开发备注：

### R-20260831-19 生产拉入 X2-Turn 4B 权重

- 类别：语音
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：X2-Turn 只作理念参考，不进生产。
- 建议下一步：无。
- 来源：
- 原因：不把 X2-Turn 4B 权重拉进生产。
- 开发备注：

### R-20260831-20 公开宣称全双工、持续聆听、情感灵魂、替代亲情、家庭入口、唤醒 99%

- 类别：市场定位
- 状态：不做
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 为何现在相关：与当前 SKU 和对外口径冲突。
- 建议下一步：无。
- 来源：
- 原因：不得公开宣称全双工、持续聆听、情感灵魂、替代亲情、家庭入口、唤醒 99%。
- 开发备注：2026-09-03 不要开 LiveKit Agents 1.7.0 `expressive=True`（会话情感标签驱动 TTS 韵律），也不要把二白Mini「领养/生命模块」或 Bubbo「有灵魂的生命体」写进对外口径。
