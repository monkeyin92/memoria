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

- 扫描日期：2026-09-03。本轮追加 R-20260903-01..03；加强 R-20260831-01/02/03/04/05/20 与 R-20260901-01/04/08 与 R-20260902-01/02/05。合规无新法规号（大型个人信息处理者仍为征求意见稿，评论截止 2026-09-07）。
- 当前出货 SKU 只允许受控半双工；设备会话 `barge_in_enabled=false`，`interruptions_enabled=false`。
- 对外口径 `advertised_duplex_level=none`。未完成真实 AEC、双讲和连续轮次验收前，不得宣称全双工或持续聆听。`direct_real_device_verified` 仍为 false。
- 唤醒词默认「茉莉」，已支持白名单切换 / MultiNet 自定义词。安静环境阶段 4 已有 10/10、5 分钟误唤醒 0；电视/家庭噪声仍要记数。不要开播放期 KWS。
- 现板无 AEC reference；hello 必须 `aec_mode=none`。换 AEC 板属于阶段 8，不要混进半双工 demo 工单。xiaozhi #2036 截至 2026-09-03 仍 open（最后活动 2026-07-07）。不得用 ESP-SR `AEC_MODE_SR_*` 宣称双工或 barge-in。
- ES8388 PGA 已到 21 dB（2026-09-02）；下一步才是 ALC / noise gate。DTLN makeup 已冻结 `8.0×`，不要再抬。
- 陪伴感靠 generation fence 丢掉 thinking 中的旧 generation，不靠抢话。BOOT 是唯一硬停。LiveKit 默认仍是全双工选项，现 SKU 必须显式 `interruption.enabled=False` 与 `preemptive_generation.enabled=False`；不要开 `user_turn_limit` 或 `expressive=True`。
- 云端 FunASR 钉 ≥1.4.13（见 R-20260903-01）；sidecar 仍 ≥1.3.29。H5 已移除；控制面只留小程序绑定 / 回顾 / 我的。

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
- 最近更新：2026-09-03
- 为何现在相关：空转写仍在真机路径上出现。FunASR v1.3.29（2026-07-24）修的是无标点模型时 `sentence_info` 空时间轴；llama.cpp v0.2.4（2026-08-29）修的是 GGUF SenseVoice 空白，是另一条路径，不能当成云端 FunASR 已关闭。sidecar 仍钉 FunASR ≥1.3.29；云端 FunASR 钉路径见 R-20260903-01（≥1.4.13）；R-20260902-01 的 1.4.12 是前一档。
- 建议下一步：按 empty+vendor_error / empty+silent / empty+gating / low_rms 分账。同一切片不要再改 VAD。sidecar 空时间轴先核 FunASR 版本。云端钉 ≥1.4.13。不要为对齐 ASR 终点去拧设备 VAD（#3591 已接受最多一个 decode chunk 的 VAD overrun）。
- 来源：https://github.com/modelscope/FunASR/releases/tag/runtime-llamacpp-v0.2.4 ；https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.4 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.9 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.12 ；https://github.com/modelscope/FunASR/releases/tag/v1.4.13 ；https://github.com/modelscope/FunASR/pull/3591
- 开发备注：2026-08-31 Agent 侧已加 `funasr_empty_accounting` 分账与 `funasr_empty_transcript_total` 指标；真机 receipt 仍待补。2026-09-01 sidecar 应钉 FunASR ≥1.3.29；llama.cpp v0.2.4 与云端 FunASR 空转写分账。2026-09-02 云端 FunASR 钉 ≥1.4.12（R-20260902-01），sidecar 仍 ≥1.3.29。2026-09-01 rescue 不得覆盖真实 final；empty 分账拆 vendor_error / silent；SenseVoice `language=zh`；`pause_asr_for_playback` 防 23s timeout。2026-09-03 FunASR v1.4.13（2026-09-02 15:12 CST）#3591 修「完整 partial 被锁成哎」；云端钉见 R-20260903-01。sidecar 仍 ≥1.3.29。

### R-20260831-03 远场先动 ES8388 模拟，DTLN makeup 已冻结

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 为何现在相关：2026-09-02 已把 ES8388 PGA 从 18 dB 刷到 21 dB；30–60 cm 正常音量 DTLN 后 RMS 约 939/764，两轮可 commit+播报。数字侧 DTLN makeup 仍冻结 8.0×，再抬会削波或假装远场已解决。
- 建议下一步：PGA 21 dB 已落地。下一步才是 ES8388 ALC + noise gate，或对比 tap WAV 的 pre/post DTLN。不要再抬 DTLN，也不要为远场去开 barge-in。
- 来源：https://docs.espressif.com/projects/esp-adf/en/latest/api-reference/abstraction/es8388.html
- 开发备注：2026-09-03 HANDOFF：PGA 21 dB 已刷写；远场若仍低 RMS，先 ALC/noise gate，不抬 DTLN。状态仍「待评估」（ALC 未做）。

### R-20260831-04 陪伴感靠 generation fence，不靠抢话

- 类别：语音
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 为何现在相关：半双工 SKU 不能靠 barge-in 制造“在听”。对照 LiveKit agents #6451（`allow_interruptions=False`）：丢掉 thinking 中的旧 generation，比抢话更接近陪伴感。LiveKit `TurnHandlingOptions`：`interruption.enabled=False` + `preemptive_generation.enabled=False`；#6858 已由 #6865 于 2026-09-01 合入 livekit/agents main；#7016 于 2026-09-02 关闭（重复）。最新发行 livekit-agents@1.7.1（2026-08-27）不含此修。文档 2026-09-03 仍默认 interruption/preemptive 为开。
- 建议下一步：屏上 idle / listening / speaking 可做。BOOT 仍是唯一硬停。半双工显式关打断与抢跑，见 R-20260902-02。不要因为 #6858 已修就打开 preemptive_generation 或 user_turn_limit。
- 来源：https://github.com/livekit/agents/pull/6451 ；https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://github.com/livekit/agents/issues/7016 ；https://github.com/livekit/agents/issues/6858 ；https://github.com/livekit/agents/pull/6865
- 开发备注：2026-09-02 半双工要对齐 LiveKit `turn_handling`：关打断、关抢跑；#7016 仍 open，不要把默认全双工选项抄进现 SKU。2026-09-03 #6858 已关（#6865 merged）；#7016 关闭为重复。未进 1.7.1。现 SKU 仍关打断与抢跑。不要开 LiveKit `expressive=True`（1.7.0 emotion tags，踩拟人化办法）。

### R-20260831-05 下一块 AEC 板：立创实战派优先

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 为何现在相关：现板 ATK 无 reference，hello 必须 `aec_mode=none`。xiaozhi #2036 仍开着（2026-09-03 复核：仍 open，最后活动 2026-07-07）。硬件 MIC3 回灌不等于 AFE 吃到参考通道。买板不能假定 MIC3 loopback 已可用，还要核 `channel_mask` 与 `aec_ref_type`（EXTERNAL_ADC vs INTERNAL）。
- 建议下一步：立创实战派（ES7210 MIC3 loopback）优先，BOX-3 其次，XMOS 更后。买板要自验 MIC3 是否进入 AEC reference。阶段 8 才买/刷 AEC 板；半双工 demo 工单不要混进新板。
- 来源：https://wiki.lckfb.com/zh-hans/szpi-esp32s3/beginner/introduction.html ；https://github.com/78/xiaozhi-esp32/issues/2036 ；https://www.cnblogs.com/wangya216/p/19455146
- 开发备注：2026-09-03 xiaozhi #2036 无新评论。阶段 7 剧本已锁定，换板仍是阶段 8。

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
- 最近更新：2026-09-02
- 为何现在相关：公开价位夹在小智克隆 ¥87–199 与萤石 RK3 标准 ¥1299 / 适老 ¥2499 之间。钉钉 A1 约 ¥499/799、A1 Pro 约 ¥1299、安克×飞书约 ¥899；对照 Bubbo 主动陪伴。萤石 RK3 ¥1299/2499 避开（7 寸数字人 / 适老看护）。
- 建议下一步：公开故事写成「家庭桌面记忆终端 / 声纹档案音箱」。不是 7 寸数字人、跌倒看护或智家中枢。按需档案带见 R-20260902-05；Bubbo 反定位见 R-20260902-06。
- 来源：https://www.ys7.com/item/1004165.html ；https://www.ys7.com/item/927621.html ；https://www.donews.com/article/detail/8612/95906.html ；http://finance.people.com.cn/n1/2026/0822/c1004-40784302.html
- 开发备注：2026-09-02 价格带对照钉钉 A1 / 安克×飞书录音+转写订阅，不对照 RK3 适老看护或 Bubbo 常在情感。

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
- 最近更新：2026-09-02
- 为何现在相关：《人工智能拟人化互动服务管理暂行办法》2026-07-15 已生效。声纹是敏感生物识别。筑梦岛 CNR 2026-08-31：年龄/付费核验必须前置，不能先用后验、先充后验。
- 建议下一步：落地页二选一：家庭档案终端 vs 拟人化陪伴。学生账号禁止虚拟亲属 / 伴侣。小程序导出 / 删除；训练默认关；会话标明 AI；2 小时提醒。投研/包装话术审查；ES8388 机考虑物理/硬开关静音并在小程序显示麦状态。年龄/付费核验前置到登录与付费前。
- 来源：https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm ；https://www.news.cn/politics/20260731/26fdd0534922429bae213b5f6f3122ec/c.html ；https://www.cnr.cn/mspd/sywzl/20260831/t20260831_527800024.shtml
- 开发备注：2026-08-31 小程序登录页与「我的」页增加 AI 标识 / 家庭档案终端定位 / 训练默认关闭说明；App 前台连续 2 小时提醒；未成年人限制文案在 profile 展示。2026-09-01 新华/法治日报施行后解读：广告不得承诺「替代亲情」「治愈孤独」；家庭场景默认隐私、能本地则本地、麦/摄像头要有开启提示和便捷关闭。2026-09-02 筑梦岛 CNR 2026-08-31：age/pay verify 必须前置。

### R-20260831-14 小程序 GTM：控制面，不承诺微信实时语音

- 类别：合规
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-09-02
- 为何现在相关：小程序 GTM 必须落在控制面，不承诺微信实时语音。微信 iLink 设备页可跳到厂商小程序控制面板（产品注册填 appid + page_path），不是微信实时语音通道。
- 建议下一步：配网 / 选角 / 声纹在设备录、微信确认、学生账号、静音、回顾与导出。家庭共享。设备页 → 小程序，不承诺微信实时语音。
- 来源：https://cloud.tencent.com/solution/smart-living ；https://iot.weixin.qq.com/doc
- 开发备注：2026-08-31 已裁至绑定 + 回顾 + 我的三 Tab；移除 home 实时语音与 H5 跳转。2026-09-02 微信 iLink 设备页落到小程序控制面，不据此开实时语音。

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
- 最近更新：2026-09-03
- 为何现在相关：xiaozhi #2036 仍开着。硬件 MIC3 回灌不等于 AFE 吃到参考通道。
- 建议下一步：阶段 7 之后买立创时验收 MIC3 是否进入 AEC reference。未过清单不得改 hello，不得开 barge-in。
- 来源：https://github.com/78/xiaozhi-esp32/issues/2036 ；https://www.cnblogs.com/wangya216/p/19455146
- 开发备注：2026-09-03 #2036 仍 open（最后 2026-07-07）。

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
- 最近更新：2026-09-03
- 为何现在相关：CAC 2026-01-10《互联网应用程序个人信息收集使用规定（征求意见稿）》第十五条：人脸/指纹/声纹须特定目的、最小必要；除法定或单独同意外应存于生物识别设备内、不得经互联网外传。第三十八条把小程序算进互联网应用程序。这与已落地的 AI 标识 + 2h 提醒不是同一层。仍是征求意见稿。CAC 令第25号（《小型个人信息处理者个人信息保护简化措施规定》）2026-09-01 已生效；处理声纹等敏感个人信息仍要单独同意，简化措施不免除声纹单独同意。2026-09-03《大型个人信息处理者个人信息保护规定（征求意见稿）》评论仍开至 2026-09-07，认定含处理 1000 万以上自然人个人信息；正式规章未出。当前 SKU 按令第25号小型处理者路径，见 R-20260903-03。
- 建议下一步：默认云端只存转写+记忆条目；原始 wav/特征不长期留、不用于声纹登录。小程序不要为回顾开实时麦。隐私政策拆：对话文本 / 原始音频 / 是否训练。声纹单独同意，不因令第25号简化而并入一般同意。
- 来源：https://www.cac.gov.cn/2026-01/10/c_1769603446094128.htm ；https://www.cac.gov.cn/2026-01/09/c_1769688003183197.htm ；https://www.news.cn/politics/20260110/f08e925cb322436ca77d39962bd704aa/c.html ；https://www.cac.gov.cn/2026-07/24/c_1786638889704872.htm ；https://www.cac.gov.cn/2026-07/24/c_1786638889443160.htm ；https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm
- 开发备注：2026-09-02 CAC 令第25号 2026-09-01 生效；声纹仍要单独同意。合规无新法规号，本条只加强已有档案分级。2026-09-03 大型处理者征求意见未转正；声纹仍要单独同意。

### R-20260901-09 2026-04 标识执法落到导出文件的显式+隐式元数据

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-02
- 为何现在相关：CAC 2026-04-28 对剪映/猫箱/即梦约谈处罚，依据含《人工智能生成合成内容标识办法》（2025-09-01 施行）。导出未加用户可感知显式标识、文件元数据未含隐式标识。小程序回顾/导出是用户语音+模型回复混合物；R-09 demo 若可下载，执法点在文件。豆包 Seed-TTS 已提供 `aigc_watermark` + `aigc_metadata` 可抄。
- 建议下一步：导出 JSON/音频包里模型侧显式「AI 生成」；元数据写服务提供者+内容编号。用户原话与合成 TTS 分轨或分字段。TTS 合成对齐 `aigc_watermark` + `aigc_metadata`。
- 来源：https://www.cac.gov.cn/2026-04/28/c_1779119736411711.htm ；https://www.gov.cn/zhengce/zhengceku/202503/content_7014286.htm ；https://docs.volcengine.com/docs/6561/1598757
- 开发备注：2026-09-02 豆包 Seed-TTS 用 `aigc_watermark` + `aigc_metadata` 做显式节奏标识与文件头隐式元数据。

### R-20260901-10 华泰 2026-08：平价硬件 + 轻订阅 + 配件/家居，不是 LOVOT 强制月费

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-02
- 为何现在相关：华泰 2026-08-12：家庭陪伴机价格带约 2000–4000 元（研报数字）；不复制 LOVOT 高月费。长期档案+记忆回顾适合做轻订阅标的。订阅卖档案容量 / 家庭席位 / 导出，不是情感月费。
- 建议下一步：定价叙事对齐该价格带与半双工 SKU；订阅写成档案容量 / 家庭席位 / 导出，不是情感费。指标用 delivered-capabilities，不编留存数字。
- 来源：https://stock.10jqka.com.cn/20260812/c678877468.shtml ；https://finance.sina.com.cn/stock/stockzmt/2026-08-12/doc-inimzcpt7023259.shtml
- 开发备注：2026-09-02 订阅 = archive capacity / family seats / export，不是 emotion fee。

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
- 最近更新：2026-09-03
- 为何现在相关：半双工 SKU 不能抄 LiveKit 默认全双工。`TurnHandlingOptions` 要把 `interruption.enabled=False` 与 `preemptive_generation.enabled=False` 写死。#6858 已由 #6865 于 2026-09-01 合入 main；#7016 于 2026-09-02 关闭（重复）。最新发行 livekit-agents@1.7.1（2026-08-27）不含此修。文档 2026-09-03 仍默认打断/抢跑为开，并新增 `user_turn_limit`（超时抢话）——现 SKU 不要设。
- 建议下一步：对照现会话 `barge_in_enabled=false` / `interruptions_enabled=false`，在 Voice Core 配置里显式关打断与抢跑。不要等发行版带上 #6865 再抄默认值。不要启用 `user_turn_limit` 或 `expressive=True`。
- 来源：https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://github.com/livekit/agents/issues/7016 ；https://github.com/livekit/agents/issues/6858 ；https://github.com/livekit/agents/pull/6865 ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.7.1
- 开发备注：2026-09-03 文档渲染仍默认全双工选项；#6858 修在 main 不在 1.7.1。半双工围栏不变。

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
- 最近更新：2026-09-03
- 为何现在相关：钉钉 A1 约 ¥499/799、A1 Pro 约 ¥1299、安克×飞书约 ¥899，卖的是录音入口 + 转写时长订阅。萤石 RK3 ¥1299/2499 是 7 寸数字人/适老看护，避开。
- 建议下一步：公开价与订阅对齐按需档案带：硬件一次性 + 档案容量/席位/导出。不要卖常开陪伴月费。对照 R-20260831-08 / R-20260901-10。
- 来源：https://www.donews.com/article/detail/8612/95906.html ；https://www.ys7.com/item/927621.html
- 开发备注：2026-09-03 奥维 2026Q2 份额仍未公开。2026-06 618 钉钉 A1 天猫/抖音/京东 AI 录音设备销量第一（量子位），不替代 Q1 额 1.4 亿 / 量 39.4 万 / PLAUD 份额 7.3% 这组数。来源 https://www.qbitai.com/2026/06/437308.html

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
- 最近更新：2026-09-03
- 为何现在相关：FunASR v1.4.13 于 2026-09-02 15:12 CST 发布，叠在 1.4.12 长段 partial 保留之上。#3591 接受 decode 终点最多晚于 VAD 终点一个 realtime chunk（reporter 236 ms：partial 114430–126976 ms vs VAD 126740 ms；此前完整 partial 被锁成「哎」）。PyPI 核心依赖钉 `numpy<2`，防止 NumPy 2 ABI 导入失败。云端钉 ≥1.4.13；sidecar 仍钉 ≥1.3.29，见 R-20260831-02。不要把 Fun-ASR-Nano vLLM / Qwen3-ASR 示例当成现 SKU 实时路径（无 GPU、不上 ESP32）。
- 建议下一步：云端 `funasr>=1.4.13` 且 lock `numpy<2`。不要为对齐 ASR 终点去拧设备 VAD。sidecar 不要跟升，除非先核 SenseVoice 时间轴。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.13 ；https://github.com/modelscope/FunASR/pull/3591 ；https://pypi.org/project/funasr/1.4.13/
- 开发备注：

### R-20260903-02 二白Mini：声纹认主+生命模块是拟人化反例，不是档案门禁样板

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-03
- 最近更新：2026-09-03
- 为何现在相关：杭州二白智能（2025-12 / 2026-01 千万天使轮，CES 2026 与声网同台）产品二白Mini 走「领养-训练-认主」，声纹+人脸绑定数字生命，核心数据放可插拔「生命模块」。这与家庭桌面记忆终端 / 声纹档案音箱相反，也踩拟人化办法的情感依赖 / 虚拟生命话术。memoria 声纹只做主人门禁与档案归属，记忆是 claim/trace/可证明删除，不是可迁移灵魂。对照 Bubbo（R-20260902-06）。
- 建议下一步：对外话术把「声纹」钉死为门禁+档案归属。禁止「认主养成」「生命模块」「有灵魂的生命体」「越相处越懂你」。五个吉祥物仍是 IP/轻订阅/壳（R-10），不是领养对象。
- 来源：https://www.36kr.com/p/3608882030593282 ；https://finance.sina.com.cn/tech/roll/2026-01-12/doc-inhfzysp6769203.shtml
- 开发备注：

### R-20260903-03 《大型个人信息处理者个人信息保护规定（征求意见稿）》评论至 2026-09-07；当前按小型处理者

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-03
- 最近更新：2026-09-03
- 为何现在相关：网信办 2026-08-07 征求意见，意见反馈截止 2026-09-07。认定条件含处理 1000 万以上自然人个人信息。正式规章未出。现行令第25号（《小型个人信息处理者个人信息保护简化措施规定》）2026-09-01 已生效；声纹等敏感个人信息仍要单独同意。当前 SKU 不要按千万级「守门人 / 外部监督委员会」扩编合规组织。
- 建议下一步：本周只盯是否出台正式规章或认定口径。用户规模远低于 1000 万时继续令第25号简化路径，声纹单独同意不并入一般同意。对照 R-20260901-08。
- 来源：https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm ；http://legalinfo.moj.gov.cn/pub/sfbzhfx/zhfxfzzx/fzzxyw/202608/t20260808_538357.html
- 开发备注：

---

## 明确不做

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
