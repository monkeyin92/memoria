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

- 扫描日期：2026-09-01（09:09 扫描误把正文写成空文件，已从 `6004907c` 恢复）。
- 当前出货 SKU 只允许受控半双工；设备会话 `barge_in_enabled=false`，`interruptions_enabled=false`。
- 对外口径 `advertised_duplex_level=none`。未完成真实 AEC、双讲和连续轮次验收前，不得宣称全双工或持续聆听。
- 唤醒词默认「茉莉」，已支持白名单切换 / MultiNet 自定义词（无需为每个词重刷）。误唤醒与漏唤醒仍要记数；不要开播放期 KWS。
- 现板无 AEC reference；hello 必须 `aec_mode=none`。阶段 7 前不刷下一块 AEC 板。
- 陪伴感靠 generation fence 丢掉 thinking 中的旧 generation，不靠抢话。BOOT 是唯一硬停。
- DTLN makeup 已冻结 `8.0×`，不要再抬。
- H5 已移除；控制面只留小程序绑定 / 回顾 / 我的。

---

## 语音与硬件

### R-20260831-01 茉莉两音节低于 ESP-SR 3–6 音节门槛

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-01
- 为何现在相关：默认唤醒词「茉莉」只有两音节，低于 ESP-SR 定制唤醒词建议的 3–6 音节门槛。
- 建议下一步：阶段 4 安静环境 ×10，分别记漏唤醒 / 误唤醒。误唤醒高则加 WakeNet 阈值，或切到 ≥3 音节词。不要开播放期 KWS。
- 来源：https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/wake_word_engine/ESP_Wake_Words_Customization.html
- 开发备注：2026-08-31 已合入白名单唤醒词切换与 MultiNet 自定义唤醒词（catalog 经设备设置下发固件，运行时选词，无需为每个词重刷）。阶段 4 计数仍待做。

### R-20260831-02 FunASR 空转写要分账 empty+vendor / empty+gating / low_rms

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：空转写仍在真机路径上出现。llama.cpp v0.2.4（2026-08-29）修的是 GGUF SenseVoice 空白，不是本仓云端 FunASR，不能当成供应商已关闭。
- 建议下一步：按 empty+vendor / empty+gating / low_rms 分账。同一切片不要再改 VAD。
- 来源：https://github.com/modelscope/FunASR/releases/tag/runtime-llamacpp-v0.2.4
- 开发备注：2026-08-31 Agent 侧已加 `funasr_empty_accounting` 分账与 `funasr_empty_transcript_total` 指标；真机 receipt 仍待补。

### R-20260831-03 远场先动 ES8388 模拟，DTLN makeup 已冻结

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：正常距离低 RMS 仍在。数字侧 DTLN makeup 已冻结 8.0×，再抬会削波或假装远场已解决。
- 建议下一步：先动 ES8388 模拟（PGA 18→21/24，或 ALC + noise gate）。下次低 RMS 先对比 tap WAV 的 pre/post DTLN。
- 来源：https://docs.espressif.com/projects/esp-adf/en/latest/api-reference/abstraction/es8388.html
- 开发备注：

### R-20260831-04 陪伴感靠 generation fence，不靠抢话

- 类别：语音
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：半双工 SKU 不能靠 barge-in 制造“在听”。对照 LiveKit agents #6451（`allow_interruptions=False`）：丢掉 thinking 中的旧 generation，比抢话更接近陪伴感。
- 建议下一步：屏上 listening / speaking 可做。BOOT 仍是唯一硬停。
- 来源：https://github.com/livekit/agents/pull/6451
- 开发备注：

### R-20260831-05 下一块 AEC 板：立创实战派优先

- 类别：硬件
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：现板 ATK 无 reference，hello 必须 `aec_mode=none`。xiaozhi #2036 仍开着，买板不能假定 MIC3 loopback 已可用。
- 建议下一步：立创实战派（ES7210 MIC3 loopback）优先，BOX-3 其次，XMOS 更后。买板要自验 MIC3。阶段 7 前不刷。
- 来源：https://wiki.lckfb.com/zh-hans/szpi-esp32s3/beginner/introduction.html ；https://github.com/78/xiaozhi-esp32/issues/2036
- 开发备注：

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
- 最近更新：2026-08-31
- 为何现在相关：公开价位夹在小智克隆 ¥87–199 与萤石 RK3 标准 ¥1299 / 适老 ¥2499 之间。
- 建议下一步：公开故事写成「家庭桌面记忆终端 / 声纹档案音箱」。不是 7 寸数字人、跌倒看护或智家中枢。
- 来源：https://www.ys7.com/item/1004165.html
- 开发备注：

### R-20260831-09 投资人 3 分钟剧本卖证据档案

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：对照 Plaud NotePin S 与 Friend 2.0（$10/月记30 天）。不演「越来越懂我」。
- 建议下一步：剧本顺序：主人事实 → claim + 说话人 → 访客取不到 → 设备 + 小程序证据链 → 删除。
- 来源：https://www.plaud.ai/blogs/news/plaud-unveils-notepins-and-desktop ；https://techcrunch.com/2026/07/30/friend-the-lonely-ai-wearable-returns-with-a-new-voice-and-a-much-bigger-price-tag/
- 开发备注：2026-08-31 H5 已移除；证据链改由设备会话 + 小程序回顾 / 导出 / 删除承接。

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
- 最近更新：2026-08-31
- 为何现在相关：《人工智能拟人化互动服务管理暂行办法》2026-07-15 已生效。声纹是敏感生物识别。
- 建议下一步：落地页二选一：家庭档案终端 vs 拟人化陪伴。学生账号禁止虚拟亲属 / 伴侣。小程序导出 / 删除；训练默认关；会话标明 AI；2 小时提醒。
- 来源：https://www.cac.gov.cn/2026-04-10/c_1777558395078289.htm ；https://www.news.cn/politics/20260731/26fdd0534922429bae213b5f6f3122ec/c.html
- 开发备注：2026-08-31 小程序登录页与「我的」页增加 AI 标识 / 家庭档案终端定位 / 训练默认关闭说明；App 前台连续 2 小时提醒；未成年人限制文案在 profile 展示。

### R-20260831-14 小程序 GTM：控制面，不承诺微信实时语音

- 类别：合规
- 状态：已完成
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：小程序 GTM 必须落在控制面，不承诺微信实时语音。
- 建议下一步：配网 / 选角 / 声纹在设备录、微信确认、学生账号、静音、回顾与导出。家庭共享。
- 来源：https://cloud.tencent.com/solution/smart-living
- 开发备注：2026-08-31 已裁至绑定 + 回顾 + 我的三 Tab；移除 home 实时语音与 H5 跳转。

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
- 最近更新：2026-08-31
- 为何现在相关：与当前 SKU 和对外口径冲突。
- 建议下一步：无。
- 来源：
- 原因：不得公开宣称全双工、持续聆听、情感灵魂、替代亲情、家庭入口、唤醒 99%。
- 开发备注：
