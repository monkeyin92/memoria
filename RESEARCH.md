# Memoria 研究跟踪

本文件是仓库唯一的外部研究扫描落地处。研究助手（memoria提升大师）每次扫描只更新这一份文件，不另建文档。开发在本文件评估条目、改状态、写备注。

本文件与 `TODOLIST.md` 分工：这里保存研究依据与采纳状态，后者保存唯一优先级执行队列、依赖和完成标记。规则、产品入口和线上状态仍分别只写在 `PROJECT_RULES.md`、`README.md`、`HANDOFF.md`。本文件不写生产密钥、环境路径、镜像 SHA、设备 ID 或运维细节。

## 怎么用

1. 研究助手扫描后：同一想法复用已有 id 并更新「最近更新」；新想法新增 `R-YYYYMMDD-NN`，状态先标「待评估」。已关闭 id 见文末，禁止用新 id 复活同一想法。
2. 开发评估后：改「状态」「最近更新」「开发备注」；采纳的执行项合并进 `TODOLIST.md`，不重复排队。已完成、过时或被后继条目替代的内容删除，仍有效的结论并入活条目或「明确不做」。
3. 建议不得违反当前 SKU：VoCat、`interrupt_assist`、`advertised_duplex_level=none`、唤醒词「茉莉」。未过 T1–T14 不得建议宣传全双工或把 `aec_reference_verified` 改成 true。
4. 扫描是合并活条目，不得清空本文件或删掉仍有效的活工单。开发精简已完成/过时内容不受行数下限限制。

## 状态词（只准用这些）

| 状态 | 含义 |
| --- | --- |
| 待评估 | 新写入，开发尚未判断 |
| 适合做 | 已接受，尚未开工 |
| 进行中 | 正在做 |
| 已完成 | 已落地；下次精简时从正文删除，结论并入活条目或关闭表 |
| 不做 | 判断不适合；并入「明确不做」，不保留长文 |
| 废弃 | 开过工后又放弃；并入关闭表 |

## 条目字段

每条固定包含：稳定 id（`R-YYYYMMDD-NN`）、标题、类别（硬件 / 语音 / 产品技术 / 市场定位 / 合规）、状态、首次写入日期、最近更新、为何现在相关、建议下一步、来源 URL、开发备注。

## 编辑规则

- 同一想法再次出现时合并到原 id 或关闭表里的 id，禁止复制一条。
- 已完成 / 过时 / 被替代的条目删除；硬约束进「明确不做」。
- 不写生产密钥、环境路径、镜像 SHA、设备 ID、HANDOFF 运维细节。
- 不建议违反当前 SKU：VoCat、`interrupt_assist`、`advertised_duplex_level=none`、唤醒词「茉莉」。不要建议播放期 KWS、谎称已验证 AEC、或把 TurnPhase 从 shadow 改成有副作用的生产策略。

---

## 当前约束

- 扫描：2026-09-15。现场设备工单与证据只写 `HANDOFF.md`，跨任务优先级见 `TODOLIST.md`。记忆召回（R-20260909-03）另轨：`RecallPlanner` 已有封闭 query rewrite；2026-09-14 本地重跑长程三项 `recall@5=1`、整体 `recall@5=0.8125`。既有 VAD 期预取/事实人格分流已接线，暂不增加平行预取或缓存链。按使用人切换人格与音色见 R-20260911-05，已有实现和剩余验收须分开；服务前期学生 / 后期老年 / 再后年轻人的阶段定位。
- SKU：ESP-VoCat，默认 `interrupt_assist`。hello 报 simultaneous capture + `aec_mode=fd_low_cost`，`aec_reference_verified=false`。对外 `advertised_duplex_level=none`。`direct_real_device_verified=false`。ATK ES8388 半双工 demo 已退役。
- 唤醒词「茉莉」。播放期 KWS 关；说话中 BOOT / 触摸硬停。DTLN makeup 冻结 `8.0×`。安静环境阶段 4 茉莉 10/10、5 分钟误唤醒 0；电视/家庭噪声仍要记数。
- 版本核验（2026-09-15 官方 PyPI）：FunASR 上游仍 1.4.15（upload 2026-09-09），**不是已知云端/sidecar 钉档**。实时链是 DashScope `fun-asr-realtime` WebSocket，本仓没有 `funasr` 包；仓内 SenseVoice 服务脚本用 `sherpa_onnx`，生产镜像包版本与构建来源待核，见 R-20260910-01。LiveKit Agents/OpenAI/Silero 仓内均 1.6.10、上游仍均 1.8.1（2026-09-10）；未见 1.8.2/1.9。RTC 1.1.14→1.1.18、API 1.2.0→1.2.1 随耦合约束评估，统一 R-20260907-01。#7064 已随 1.8.0 合并，但本仓显式 `auto_gain_control=True`、未配 NC，默认值变化不直接改变现链路。LiveKit 侧按 dispatch metadata 保持兼容半双工禁打断；设备 Voice Core 侧按协商 `audio_mode` 派生，不能跨运行路径混称为统一开关；默认 preemptive 关闭。VoiceMem 插件仍 0.2.2 / `livekit-agents<1.8`，不安装。
- 合规：拟人化办法已生效；令第25号已生效，声纹仍要单独同意。令第25号/拟人化办法无本周新细则。最高法涉人工智能纠纷意见（法发〔2026〕10号，司法意见非 CAC 新法）加强见 R-20260910-02。大型处理者征求意见截止已过、截至 2026-09-15 仍无定稿；清朗二阶段仍以 2026-09-02 进展稿为执行报道（无 9/10–9/15 新法规），见 R-20260901-08。NEW_LAW_IDS（法规/CAC）空。

## 当前站位（2026-09-12 用户重申，已采纳，不是工单）

定位：陪伴机器人，陪伴与关怀是内核，人群分三阶段扩展——**前期以学生为主**（陪伴、关怀、教学导师）；**后期扩展老年人**（陪伴、关怀、人物复刻、人生故事搜集并制作成人生故事册）；**再往后扩展年轻人**（陪伴、潮玩）。机器人外形未定；五个吉祥物（星澜/桃喜/绵绵/阿序/玄墨）为占位资产，商业计划书可暂用，不绑定最终外形，不是五个人格大模型。

声纹门禁、按需档案、带标识导出、可证明删除是贯穿各阶段的能力底座，不是定位本身；「家庭桌面记忆终端 / 声纹档案音箱」旧定位作废，仅作能力描述。订阅叙事随阶段走：前期学生线卖陪伴/教学价值与家庭席位，后期老年线承接人生故事册等档案服务；价格带仍对照钉钉 A1 / 安克×飞书（约 ¥499–1299），不对照萤石 RK3 适老看护或万元级人形。

任何阶段都不做：7 寸数字人屏、跌倒/医疗级看护硬件、智家中枢、全屋 OS、运动巡航形态、耳机 Agent、微信实时语音。阶段外（后续阶段方向，当前不投入但不是反定位）：老年陪伴 / 人物复刻 / 人生故事册属后期，年轻人潮玩属再后期——Doova / HUA-H / bibo / Amoo / iKairos / 安安 等条目改作「阶段雷达」，供对应阶段回看；Bubbo / 二白Mini / JUOS / Microduck / Plaud One / 优必选 U1 / 小度 Pro Max / 糯宝(Robie) 仍纯反定位。童声 500–800 ms 停顿写成「不截断」，不写成「更懂情绪」。

2026-09-15 阶段雷达日历：优必选 U1 声称 2026-09-16 起交付现为 T-1，仍无「已开始交付」实锤（沿用红星资本局 2026-09-03 + 京东约 60 天 / 9/15 后有货口径；2026-09-14 21世纪经济报道（新浪转载）：周剑 8 月底承认广西工厂因自然因素投产推迟两三个月，要到 9 月中下旬才能批量交付，深圳产能补位；全年可交付仍 1,500–2,000 台，对比预售约 1.34 万 / 13,361，柳州工厂延期；https://leaderobot.com/news/9425 ；https://www.163.com/dy/article/L5U2PKT60511U82T.html ；https://finance.sina.com.cn/roll/2026-09-14/doc-iniruark4819871.shtml）。小度智能音箱 Pro Max 9/28 开售见 R-20260914-01（反定位·智能音箱/家庭 Agent，订阅叙事不对照 ¥299 音箱）。无芯科技安安欧洲 2026-09 开售见 R-20260914-02（阶段雷达·老年后期；Duncan 仅旁证学生线，不新开 id）。糯宝 Robie / Pophie 见 R-20260915-01（反定位·桌面萌宠）。Doova / Amoo / iKairos / bibo / HUA-H 不新开 id（R-20260910-03 / R-20260910-04 / R-20260911-02 / R-20260911-03 / R-20260911-04）。海信 JUOS 约 8/31–9/3 起对首批电视/投影推 Sep OTA；小聚识人非默认，声纹/人脸需登记（https://www.3elife.net/Art/ie/202609/04/109709.html）；仍是 TV/AIOS 全家中枢，纯反定位。Plaud One 仍 explorer $249.99，未见大陆零售 SKU。Microduck 灰色市场国内约至 ¥5567（https://www.yicai.com/news/103350821.html）。华泰 ¥2000–4000 价带本月无修订。Bubbo / 二白Mini / JUOS / Microduck / Plaud One / 优必选 U1 / 糯宝(Robie) 仍纯反定位。

---

## 语音与硬件

### R-20260831-01 茉莉噪声环境计数

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-03
- 吸收：R-20260901-01
- 为何现在相关：默认唤醒词「茉莉」只有两音节，低于 ESP-SR 定制唤醒词建议的 3–6 音节门槛。目录切词 / MultiNet 不是 WakeNet，不会消灭误唤醒。
- 建议下一步：只补电视人声 / 家庭噪声的漏唤醒与误唤醒计数。误唤醒高则加 WakeNet 阈值（0.4–0.9999）或改 ≥3 音节词。不要开播放期 KWS。
- 来源：https://docs.espressif.com/projects/esp-sr/zh_CN/latest/esp32s3/wake_word_engine/ESP_Wake_Words_Customization.html ；https://github.com/espressif/esp-sr/issues/194
- 开发备注：白名单切词已切流。安静环境阶段 4 茉莉 10/10、5 分钟误唤醒 0。电视/家庭噪声仍未做。

### R-20260831-02 FunASR 空转写分账与消费方核实

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-15
- 吸收：R-20260901-02、R-20260902-01、R-20260903-01、R-20260904-01、R-20260901-11
- 为何现在相关：空转写仍需要专项真机证据。`services/agent/src/providers/funasr_stt.py` 经 `websockets` 直连 DashScope `fun-asr-realtime`，`pyproject.toml`/`uv.lock` 没有 `funasr` 包；救援客户端经 `SENSEVOICE_URL` 调独立镜像。仓内 `scripts/run_sensevoice_asr.py` 用 sherpa-onnx，不能把 FunASR 1.3.29/1.4.x 的修复推定为现网已用或必需的升级。
- 建议下一步：按 empty+vendor_error / empty+vendor_silent / empty+gating / low_rms 补 receipt；核实真实 sidecar 构建与版本后再做相关升级 A/B（R-20260910-01，执行 P1-02）。不要为对齐 ASR 终点去拧设备 VAD。Fun-ASR-Nano / GGUF 不上 ESP32、不替代实时路径。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.14 ；https://github.com/modelscope/FunASR/pull/3591 ；https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://pypi.org/project/funasr/1.4.15/ ；https://github.com/modelscope/FunASR/releases/tag/v1.4.15
- 开发备注：Agent 已有 `funasr_empty_accounting`。2026-09-14 已纠正消费方口径；上游 PyPI latest=1.4.15 不证明 DashScope 或救援镜像使用该包。现有分账与空输入恢复保留，剩余是运行来源核验、同条件测试与真实链路验收。2026-09-15：官方 PyPI latest 仍 1.4.15，无更新上传；不宣称 FunASR>1.4.15。

### R-20260910-01 ASR sidecar 可复现构建与 FunASR 1.4.15 适用性

- 类别：语音
- 状态：适合做
- 首次写入：2026-09-10
- 最近更新：2026-09-15
- 为何现在相关：PyPI funasr 1.4.15 于 2026-09-09 上传，含 NumPy 2 测试与流式 KWS/VAD 边界修复；但本仓实时链不是 pip 消费方，SenseVoice 脚本也使用 sherpa-onnx。线上镜像 Dockerfile 尚未入仓，当前首先缺可复现来源，不是缺一个盲升包命令。
- 建议下一步：先只读核实生产脚本/包/模型与构建输入，并补精确可复现锁定。只有确认线上实际使用低版本 FunASR 才评估 1.4.15；若是 sherpa-onnx，按其自身修复评估，不强行引入 FunASR。验收空转写分类、中文短句/尾字、2.5s 救援上限与降级；完整执行条件见 `TODOLIST.md` P1-02。
- 来源：https://pypi.org/project/funasr/1.4.15/ ；https://github.com/modelscope/FunASR/releases/tag/v1.4.15
- 开发备注：2026-09-14 官方 PyPI latest=1.4.15；采纳的是「核实消费方并补可复现构建」，不是批准升级/切流。上游 NumPy 2 测试仅覆盖其公布环境，不能外推到未知 Torch/模型组合。sidecar 独立发布；如改主项目依赖，仍必须遵守完整镜像构建门禁。2026-09-15：官方 PyPI latest 仍 1.4.15，无更新包；不宣称 FunASR>1.4.15。

### R-20260831-06 SenseVoice EOU 只当 sidecar 分数

- 类别：语音
- 状态：待评估
- 首次写入：2026-08-31
- 最近更新：2026-08-31
- 为何现在相关：Kazu418/sensevoice-eou 可作实验分数，补 END_CANDIDATE / clock-fact，但不能变成第二条话轮控制面。
- 建议下一步：只把 sidecar 分数喂给现有 END_CANDIDATE / clock-fact。不开 TurnPhase 生产副作用，不拉 X2-Turn。
- 来源：https://github.com/Kazu418/sensevoice-eou
- 开发备注：

### R-20260901-06 告别语义分类不能绕过主人能力门

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-01
- 为何现在相关：END_SESSION 小模型分类若对 guest 直接关会话，会把权限门拆掉。
- 建议下一步：精确词和语义分类都先过主人 subject capability；guest 只停公开播放或走超时。
- 来源：
- 开发备注：

### R-20260907-01 LiveKit Agents 1.8.1 同组升级评估

- 类别：产品技术
- 状态：适合做
- 首次写入：2026-09-07
- 最近更新：2026-09-15
- 吸收：R-20260831-04、R-20260902-02、R-20260911-01
- 为何现在相关：官方 1.8.1（2026-09-10）含连接池/会话关闭/音频计量与资源清理修复。仓内 Agents/OpenAI/Silero 均 1.6.10；候选三者同为 1.8.1，RTC 必须 1.1.18，API 1.2.1 还会推进 protocol 约束。需要读中间版本说明，但不需要先部署 1.8.0。收益要在本项目实测，不能承诺解决现有 ASR/硬件问题。
- 建议下一步：统一为 `TODOLIST.md` P1-01 的本地兼容性→完整镜像→独立上线验收。LiveKit 侧保留 dispatch metadata→`controlled_half_duplex_session`，设备 Voice Core 侧保留 `audio_mode` 派生逻辑；兼容半双工路径禁打断、默认 preemptive 关闭。验证 TypedDict 字段实际被消费，不能只测构造成功。#7104 将对话 event 改 attributes，并变更 span/计量；内容采集与 PII 默认仍开启，要显式禁内容/PII并验证实际 exporter 不泄漏。
- 来源：https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0 ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.1 ；https://pypi.org/project/livekit-agents/1.8.1/ ；https://github.com/livekit/agents/pull/7064 ；https://github.com/livekit/agents/pull/7104 ；https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://pypi.org/project/livekit-agents/
- 开发备注：2026-09-14 已核官方包元数据与本仓消费点。#7064 于 2026-08-31 合并并随 1.8.0 发布；本仓 RoomIO 显式 `auto_gain_control=True`、未配置 NC，所以「NC 时默认关 AGC」不改变现有行为。1.8.1 的 DuplexModel 只观察，不接生产；禁止搭车开启 `user_turn_limit`、`expressive=True`、TurnPhase 副作用或放行设备语音打断。`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0` 与 `LIVEKIT_TELEMETRY_ALLOW_PII=0` 需结合实际导出测试；锁变化不走 agent-only overlay。2026-09-15：官方 PyPI latest 仍 1.8.1，未见 1.8.2/1.9；DuplexModel 仍只观察。升级评估仍走 `TODOLIST.md` P1-01，不宣称 LiveKit>1.8.1。

### R-20260907-02 VoCat AEC / barge-in 真机未验

- 类别：硬件
- 状态：进行中
- 首次写入：2026-09-07
- 最近更新：2026-09-15
- 为何现在相关：出货板已是 VoCat（ES7210 + ES8311）。协商 `interrupt_assist` 已切流，AEC residual、真机 barge-in 和 T1–T14 仍未过。现场步骤只写 `HANDOFF.md`。xiaozhi-esp32 #2036（ES7210 MIC3 作 AEC reference，MMR vs MR）仍与现板路径相关，不是已验证 barge-in。
- 建议下一步：量 AEC residual，真机测打断。未过证不得改 `aec_reference_verified` 或宣传全双工。立创附件 `vocat_xiaozhi_1_1_0.bin` 只作适配参考，不要抄「萌宠全双工」叙事。LiveKit ESP32 指南可作 ES7210 `0x80` / ES8311 `0x30` 初始化参考。
- 来源：https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp-vocat/index.html ；https://livekit.com/blog/esp32-custom-hardware-quickstart ；https://github.com/78/xiaozhi-esp32/issues/2036
- 开发备注：hello 已报 simultaneous capture / fd_low_cost；Agent barge-in 跟 `audio_mode`。2026-09-11：#2036 仍 open，`updated_at` 仍 2026-07-07，无新活动；不外推为全双工证据。2026-09-14：#2036 仍 open，`updated_at` 仍 2026-07-07T01:52:32Z（https://github.com/78/xiaozhi-esp32/issues/2036）；不外推为 VoCat AEC/barge-in 已验。相关 #2140 麦阵讨论不改变 memoria 围栏。2026-09-15：#2036 仍 open，`updated_at` 仍 2026-07-07T01:52:32Z；无新 AEC 核验。`full_duplex_verified=false`、`direct_real_device_verified=false`、`aec_reference_verified` 仍 false。

### R-20260909-02 XVF3800 硬件 AEC 是更高天花板

- 类别：硬件
- 状态：待评估
- 首次写入：2026-09-09
- 最近更新：2026-09-11
- 为何现在相关：Seeed reSpeaker XVF3800 / XMOS 4-mic 提供芯片侧 AEC，高于当前 VoCat 软件 / `fd_low_cost` AEC。不是退役 ATK 的升级路径，也不是把现 SKU 改口成全双工的理由。
- 建议下一步：只作 VoCat T1–T14 之后的备选。不要把采购混入当前工单。
- 来源：https://wiki.seeedstudio.com/cn/respeaker_xvf_3800_xiaozhi/ ；https://wiki.seeedstudio.com/cn/respeaker_xvf3800_introduction/ ；https://www.seeedstudio.com/ReSpeaker-XVF3800-4-Mic-Array-With-XIAO-ESP32S3-p-6489.html ；https://www.iceasy.com/product/101991441
- 开发备注：2026-09-11：Seeed XIAO 套件约 $66.99 in stock；国内 iCEasy 裸板约 ¥422 但现货库存 0 / 订货约 7 工作日。仍是 VoCat 之后备选，不混入当前工单。

## 记忆

### R-20260909-03 VoiceMem：借鉴召回手法，不换档案栈

- 类别：产品技术
- 状态：进行中
- 首次写入：2026-09-09
- 最近更新：2026-09-15
- 为何现在相关：VoiceMem（v0.0.2）是实时语音智能体的记忆检索层：左脑事实、右脑人格/情绪笔记，ingest 抽事实、search 把 Top-K 注入回复。宣传 LoCoMo ~91%、PersonaMem ~69%、检索 ~134ms、每轮约 300–430 memory token。它不能替代 Memoria 的对话档案。与已落地的 Dense-Mem confirm/trace（原 R-20260831-07）同族：增强现有 catalog / persona / recall，不换栈。当前工单仍是 VoCat interrupt_assist，本条另轨。第三方 `livekit-plugins-voicemem` 0.2.2（PyPI 2026-09-01）是 pgvector 记忆插件，要求 `preemptive_generation` 关，且声明 `livekit-agents<1.8`，与仓内要评估的 1.8.0 / 现已发布的 1.8.1 仍不兼容。
- 建议下一步：不 pip install voicemem / livekit-plugins-voicemem、不克隆、不接音频。只喂已 commit 且 `history_eligible=true` 的主人文本；输出只能当候选 Memory Claim。硬约束见 R-20260909-04。先修整体 `recall@5=0.8125` 的遗漏与人物抽取（`TODOLIST.md` P1-06）；已有预取/分流不是零，不再新增平行记忆库或缓存链。
- 来源：https://github.com/xzf-thu/VoiceMem ；https://xzf-thu.github.io/VoiceMem/ ；https://arxiv.org/pdf/2608.26005 ；https://pypi.org/project/livekit-plugins-voicemem/0.2.2/
- 开发备注：2026-09-09 只读评估。LICENSE Apache 2.0；捆绑模型另有许可。左脑底层 Mem0 + 本地 Qdrant，默认不是多进程安全的生产 catalog。2026-09-10：插件版不能当升级路径，也不用来换 PG/MinIO 档案栈。同日扩 `memory_eval_zh_v1` 并让评测查询走生产 RecallPlanner。同日在 `RecallPlanner` 加封闭 rewrite（不改 archive、不设 entity 过滤）：「小朋友/小孩子/孩子」扩 `儿子/女儿` 及已确认子女别名；「难受/不开心/伤心/委屈」扩 `难过`；「小朋友」不误匹配「朋友」。`memory_eval_zh_v1`：跨会话 / 转述追问 / 安慰 `recall@5` 均为 1.0；隔离/候选泄漏仍 0；整体 `recall_at_5=0.8125`、`ndcg_at_10≈0.734`。抽取器仍抽不出「我儿子小周对猫毛过敏」的人物，所以转述靠词表而非实体。不引入 Mem0。2026-09-11：`livekit-plugins-voicemem` 0.2.2 仍声明 `livekit-agents<1.8`，与 1.8.1 仍不兼容；不因此换栈。2026-09-14：插件仍 0.2.2 / `livekit-agents<1.8`；仍禁止换 PG+MinIO 档案栈。2026-09-15：插件仍 0.2.2 / `livekit-agents<1.8`；不安装、不换栈。

产品拆两层，默认只做第一条：

1. 记住用户（了解你）：从主人权威对话抽出稳定事实、关系、近期事件。对应 memory catalog（life_story / daily_life / 人物关系 / episode），candidate → confirmed；实时仅允许 confirmed 进入既有上下文路径，不把未消费的 MemoryContextClient 写成运行权威。
2. 模仿用户（变成你）：口癖/句长/语速走 persona 胶囊；数字分身走 Companion / Self Preview，须披露「授权模拟，不能替本人作决定」，模拟输出不得反哺主人证据。VoiceMem 右脑是给模型看的内部笔记，不是分身引擎。

「像正常人对话」不是第三块记忆库：日期是运行时上下文，查询是工具，关联旧事才要证据档案 + recall。安慰分三层——当轮 `emotion_observation`、persona 表达习惯、「上次你很难过」才是记忆 claim。合成一块右脑图会把当天心情写成性格。

现有权威链不得拆：EvidenceEvent / ArchiveSink / PG + MinIO；主人历史只消费 `history_eligible=true`；事实走 extractor + write policy + catalog；召回走 RecallPlanner + `catalog.context`（`include_candidates=false`，最多 8 条）；说话方式走 persona；当轮情绪走 `emotion_observation`；guest / uncertain 不进主人历史、私人记忆和工具。活跃预取的 HTTP 超时来自 ResponsePlannerClient 的有效 `MEMORIA_RESPONSE_PLAN_TIMEOUT_S`（代码默认 0.8s），不是未消费 MemoryContextClient 的 0.3s；实施时核现网覆盖和服务端超时，不把默认值当 SLA。`memory_eval_zh_v1` 已覆盖跨会话 / 转述追问 / 安慰召回；日历窗与封闭 rewrite 已过这三项，其余无共享词转述仍可能漏。

2026-09-14 代码核销与剩余工作（均在现有栈上）：

1. 已有 VAD 期 query-conditioned 预取：`agent.py` → `duplex_runtime.py` → `routes/interaction.py`，内部用 RecallPlanner + confirmed catalog。事实与 `persona_trait` 也已分流。独立 `MemoryContextClient` 构造后未消费；先追踪并明确一条权威路径，不能再从零造预取。
2. 未完成的是硬 memory-token 预算、可观测裁剪与缓存隔离/失效；条数上限不等于 token 上限。换主体不能沿用旧私有缓存，guest/uncertain 不准发私人查询。执行 P2-01，依赖召回遗漏先修。
3. 同日本地重跑 16 例：长程三项 `recall@5=1`，整体仍 0.8125。具体 miss 为 `paraphrase-food-preference`、`person-alias-mother`、`repeated-episode-campus-startup`；先加未见改写/反例再扩封闭规则，不靠改评测答案提高分数。
4. 人物抽取仍漏「我儿子小周对猫毛过敏」「阿梅是我妈妈」；沿原 extractor/write policy 修 person 与属性 claim，保留 candidate→confirmed。只有评测证明必要才扩实体路由，不引入 Mem0。

验收：相关 memory_eval / catalog 单测；prompt 注入有 token 或条数上限的回归；实时路径超时失败可降级。真机「记得上周那件事」另开设备验收。

### R-20260902-04 可证明删除：在 confirm/trace 上叠擦除回执

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-02
- 最近更新：2026-09-02
- 为何现在相关：投资人剧本的删除承诺还缺可离线核验的擦除回执。
- 建议下一步：在现有 PG claim/trace 上叠 subject 谱系与删除回执，不新开记忆服务。小程序删除后给出可核验回执号。
- 来源：https://github.com/sambhal-labs/vismaran ；https://github.com/BernhardJackiewicz/provem
- 开发备注：

## 产品与合规

### R-20260901-08 声纹/原始音频分级与标识

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-01
- 最近更新：2026-09-15
- 吸收：R-20260901-03、R-20260901-05、R-20260901-09、R-20260903-03
- 为何现在相关：令第25号已生效，声纹仍要单独同意，2026-09-15 无新声纹细则。大型处理者征求意见截止已过、截至 2026-09-15 仍无正式文本/延期公告/解读（https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm）；当前按小型处理者，不扩编守门人组织。清朗二阶段 2026-09-02 CAC 稿（https://www.cac.gov.cn/2026-09/02/c_1790099041364574.htm）为执行进展（清理 561 万余条等），非正式新规章；无 9/10–9/15 新法规。最高法涉人工智能纠纷意见是司法意见不是 CAC 新法，拟声/训练语料见 R-20260910-02。导出是用户语音+模型回复混合物，执法点在文件标识。拟人化办法的落地页/2h 提醒已在小程序完成（原 R-20260831-13）。
- 建议下一步：默认云端只存转写+记忆条目；原始 wav/特征不长期留、不用于声纹登录。声纹单独同意，不并入一般同意。导出 JSON/音频包里模型侧显式「AI 生成」，元数据写服务提供者+内容编号；TTS 对齐 `aigc_watermark` + `aigc_metadata`。五个吉祥物禁止亲属/伴侣角色。家庭邀请只给控制面权限，写档案仍要设备端 owner 声纹。继续盯大型处理者正式文本，不新开法规 id。
- 来源：https://www.cac.gov.cn/2026-07/24/c_1786638889704872.htm ；https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm ；https://www.cac.gov.cn/2026-09/02/c_1790099041364574.htm ；https://www.gov.cn/zhengce/zhengceku/202503/content_7014286.htm ；https://docs.volcengine.com/docs/6561/1598757
- 开发备注：2026-09-14 NEW_LAW_IDS（法规/CAC）空。Seed-TTS `aigc_watermark` / `aigc_metadata` schema 仍 NO_CHANGE（https://docs.volcengine.com/docs/6561/1598757）。不把大型处理者征求意见稿写成已生效；不把清朗二阶段进展稿写成新规。2026-09-15：大型处理者征求意见稿仍无定稿；清朗二阶段仍以 9/02 进展稿为最新执行报道。NEW_LAW_IDS（法规/CAC）空。令第25号/拟人化办法无本周新细则。

### R-20260911-07 声音样本检验与「一分钟内可用」（消费端）

- 类别：产品技术
- 状态：进行中
- 首次写入：2026-09-11
- 最近更新：2026-09-14
- 为何现在相关：用户要求上传/录制后尽量一分钟内检验，并显示真实进度。过去的写死评估、伪样本放行与无轮询已由样本实测校验、failed 硬否决和数据库进度替代；当前缺的是线上版本对齐与真 provider/设备端到端耗时证据，不是重新实现这些功能。
- 建议下一步：按 `TODOLIST.md` P1-04 核实际部署、异常/取消/撤销，再量上传到设备可听的总耗时；60s 对客预算与 120s provider 上限分开记录。若常态超过 60s，改文案或优化实际瓶颈，不放宽样本校验/over_budget。
- 来源：仓库内证据为主
- 开发备注：2026-09-11 已实现。**本地体检验真**：新增 `services/voice_profile/sample_validation.py`，用 PyAV（既有依赖 `av>=18`，与 `services/device_media_gateway/opus.py` 同一路解码）解出单声道 float32，量真实时长、采样率、声道、RMS/峰值 dBFS、削波占比、静音占比、有效人声时长、直流偏移；实测 15s WAV 7ms、15s MP3 4ms、60s WAV 15ms——**体检从来不是那一分钟的瓶颈**，瓶颈在 provider 克隆。判定阈值：≥10s、≤60s(+2s 容器余量)、≥16kHz、RMS ≥ −45 dBFS、有声 ≥8s、削波 <5%；不合格在**写入任何记录之前**抛 `VoiceSampleRejectedError`，路由翻成 422 + 中文可行动文案（`sample_copy.py`）并带 `X-Memoria-Voice-Rejection` 头。**判据改真**：新增 `voice_profiles.sample_validation_status` 与 `voice_sample_validations` 证据表（sqlite + postgres，双写），写死的 evaluate/quality 已从消费端移除；放行由单一函数 `voice_profile_delivery_admitted()` 决定——样本实测通过，或（A/B 评估通过 ∧ 质量测量通过）；任一项显式 `failed` 一律否决。这同时堵住了一个既有缺陷：小程序每次进「我的」页都会对 `candidate` 调 ready-for-device，会把**已被判失败**的评估翻成 `passed` 并激活；现在 `failed` 是不可覆盖的否决（回归测试 `test_a_rejected_sample_cannot_be_put_on_a_device_by_a_later_read`）。**进度可续**：`POST /v1/voices/enrollments`（`ready_for_device=true`）改为立即返回 `enrolling` 并在后台任务里跑 provider；进度由**数据库状态推导**（`services/voice_profile/enrollment_progress.py`），不做请求内跟踪，因此刷新/离开/换端都能恢复；阶段 queued→cloning→activating，`over_budget` 在超过 `PROMISED_TOTAL_MS=60s` 后置真，前端据此撤掉「一分钟」措辞。`ready_for_device` 的 POST 仍在 45s 内直接等到 active（`_COMPLETION_WAIT_S`），超时返回真实在途状态而不是报错。前端（小程序 `pages/profile`）由子代理完成：2s 轮询、进度条、`stage_label` 原样渲染、终态不显示进度块、卸载清理定时器并加代际计数防迟到结果重新武装定时器。**测试**：`services/voice_profile/tests/test_sample_validation.py`(10)、`test_enrollment_progress.py`(5)、`test_manager.py`(13) 全绿；`services/control_api/tests/test_voice_profile_api.py`(21) 全绿；小程序 `node --test`(223) 全绿。**遗留**：未在真机/真 provider 上量端到端耗时；`MEMORIA_VOICE_ENROLLMENT_TIMEOUT_S=120` 仍是 provider 侧上限，与 60s 对客承诺是两个量，未改。

### R-20260910-02 最高法涉人工智能纠纷意见（法发〔2026〕10号）

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-11
- 为何现在相关：2026-09-07 公布《最高人民法院关于依法审理涉人工智能纠纷案件的意见》，全国法院首份涉 AI 纠纷规则，5 部分 / 24 条。这是司法意见，不是 CAC 新法规。直接相关：规范 AI 换脸/拟声；「未经同意使用自然人声音作为训练语料，模仿其音色、语调和发音风格生成可识别的合成人声的，构成对声音权益的侵害」；另有生成式 AI 服务提供者责任、AI 幻觉等。陪伴 TTS / 可选声音克隆 / 声纹档案要把第三方声音训练与拟声当民事风险。
- 建议下一步：产品同意书与导出标识对齐拟声/训练语料单独同意；不要把大型处理者征求意见稿写成已生效。合规落地仍走 R-20260901-08。
- 来源：https://www.chinanews.com.cn/gn/2026/09-07/10691909.shtml ；https://legal.gmw.cn/2026-09/07/content_38989161.htm ；http://paper.people.com.cn/rmrb/pc/content/202609/08/content_30179776.html ；https://www.ncsti.gov.cn/kjdt/xwjj/202609/t20260910_255808.html ；https://paper.people.com.cn/rmrbhwb/pc/content/202609/08/content_30179682.html ；https://finance.sina.com.cn/jjxw/2026-09-10/doc-inirihnw5380235.shtml
- 开发备注：2026-09-11 追加次级来源（国家科技管理信息系统 2026-09-10 转载；人民日报海外版 2026-09-08）。米哈游 AI 变声民事案（媒体 9/9–10，判决约 6/30）仅作拟声风险旁证，不新开法规 id。法规/CAC 的 NEW_LAW_IDS 仍空。本条因声音/拟声对 TTS/声纹路径实质相关而单开。清朗转载不新开 id。

### R-20260910-03 涂鸦 Doova（IFA 2026）阶段雷达·老年后期

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-12
- 为何现在相关：界面新闻 2026-09-04 报道涂鸦 IFA 推出 Doova：适老独居陪伴机器人，LDS 雷达、4 麦声源定位、跌倒/姿态检测、移动巡航、IoT 中枢、聊天陪伴。
- 建议下一步：老年陪伴是 Memoria 后期方向（见当前站位），但 Doova 的跌倒检测、移动巡航、IoT 中枢形态任何阶段都不做；其适老交互与子女端叙事留作后期参考。当前阶段不新开适老工单。
- 来源：https://www.jiemian.com/article/15059778.html ；https://www.itheat.com/view/63447.html
- 开发备注：2026-09-11 追加次级对比稿（ITHeat 2026-09-09）。2026-09-12 定位重申后由「反定位」改挂「阶段雷达·老年后期」。不复制新 id。

### R-20260910-04 青心意创 Amoo（IFA 2026）阶段雷达·年轻人潮玩后期

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-12
- 为何现在相关：ITBear 2026-09-09 报道青心意创 Amoo 在 IFA 2026 以软毛角色/萌宠情感陪伴出场。
- 建议下一步：情感陪伴是 Memoria 内核，萌宠领养叙事当前不做；年轻人潮玩属再后阶段，Amoo 的角色 IP 运营与萌宠交互留作该阶段参考。不对照 Amoo 改当前 SKU。
- 来源：https://www.itbear.com.cn/html/2026-09/1548976.html ；https://www.itheat.com/view/63447.html ；https://finance.sina.com.cn/jjxw/2026-09-08/doc-inirauhy6075402.shtml
- 开发备注：2026-09-11 追加次级对比稿（ITHeat 2026-09-09；新浪财经 2026-09-08）。2026-09-12 定位重申后由「反定位」改挂「阶段雷达·年轻人潮玩后期」。不复制新 id。

### R-20260911-02 Lingverse iKairos（IFA 2026）阶段雷达·老年后期/档案

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-12
- 为何现在相关：机器人大讲堂 2026-09-08 报道新加坡 Lingverse 的 iKairos：圆形「神经核心」可当项链或嵌入桌面机器人头部；定位「AI记忆伙伴」/ LifeOS 持续观察构建用户画像；主动 Agent 行为；物理摄像头滑盖+静音键；尚在开发、定价/上市未公布；Pre-A $29M（2026-07）。
- 建议下一步：记忆与故事搜集和后期「人生故事册」有交集，其 LifeOS / 用户画像叙事留作后期参考；随身佩戴形态、常在环境采集、主动替用户发消息的 Agent 行为不是当前方向。不新开可穿戴工单。
- 来源：https://leaderobot.com/news/9497
- 开发备注：2026-09-12 定位重申后由「反定位」改挂「阶段雷达·老年后期/档案」。不复制新 id。

### R-20260911-03 镭萌 bibo ¥1499 潮玩非语言情感（阶段雷达·年轻人潮玩后期）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-12
- 为何现在相关：36氪 2026-09-09：杭州镭萌科技千万级天使轮；潮玩非语言情感机器人 bibo 定价 ¥1499（报道称 6 月已上市）。东方财富/每经 2026-09-10 转载：大厂创业者涌入「非语言交互」消费级具身（bibo / Ropet / BubblePal）。
- 建议下一步：年轻人潮玩是 Memoria 再后阶段方向（见当前站位）；bibo 的 ¥1499 定价、潮玩渠道与非语言情感设计留作该阶段对标。当前阶段不新开潮玩工单。
- 来源：https://eu.36kr.com/zh/p/3975777320284422 ；https://finance.eastmoney.com/a/202609103870922661.html
- 开发备注：2026-09-12 定位重申后由「反定位」改挂「阶段雷达·年轻人潮玩后期」；本条钉 bibo 价带与融资信号。不复制新 id。

### R-20260911-04 华拟智能 HUA-H 仿生人头养老陪护（阶段雷达·老年后期）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-12
- 为何现在相关：动脉网/VBData 2026-09-10：东莞华拟智能完成数千万元天使轮，仿生人头养老陪护产品 HUA-H。资本继续涌入仿生人形头 / 养老情感。
- 建议下一步：老年陪伴是 Memoria 后期方向（见当前站位），仿生人头形态不是；养老陪护资本热度作信号。当前阶段不把人形头/养老看护写进 VoCat SKU，不新开养老看护工单。
- 来源：https://www.vbdata.cn/1519092689.html
- 开发备注：2026-09-12 定位重申后由「反定位」改挂「阶段雷达·老年后期」；本条只钉华拟融资。不复制新 id。

### R-20260914-01 小度智能音箱 Pro Max / 百度搭子（反定位·智能音箱/家庭 Agent）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-14
- 最近更新：2026-09-15
- 为何现在相关：百度 AI Day 小度 2026-09-08 发布小度智能音箱 Pro Max：上市价 ¥349、首销 ¥299，9/28 开售；强调多意图理解、长期记忆/家庭档案偏好、跨设备调度摄像机、小彩屏+温湿度光传感器联动家电。同期超能小度接入百度搭子。这是智能音箱/家庭 Agent 中枢路线，与 Memoria「陪伴机器人 + 声纹门禁/按需档案能力底座、非智家中枢」形成对照；不要把「长期记忆」口号抄成产品定位。
- 建议下一步：作纯反定位日历（价带/记忆叙事对照）；不新开音箱屏/IoT 中枢工单；订阅叙事仍走学生陪伴/教学价值，不对照小度 ¥299 音箱。
- 来源：https://www.ithome.com/0/999/786.htm ；https://finance.sina.com.cn/roll/2026-09-08/doc-inircmeu9356305.shtml ；https://www.eefocus.com/article/2083546.html
- 开发备注：不并入 Doova/Amoo/iKairos；不属于阶段雷达（不是机器人形态）。2026-09-15：仍 9/28 开售 / ¥299 首销；无新事实。

### R-20260914-02 无芯科技安安 / 邓肯（IFA 2026）阶段雷达·老年后期（邓肯旁证学生线）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-14
- 最近更新：2026-09-14
- 为何现在相关：深圳无芯科技（Mind With Heart Robotics）IFA 2026 推情感 AI 熊猫安安（CES 2026 AI 创新奖）：全身触觉、主动陪伴/提醒、长期记忆学习交互习惯；欧洲零售约 $1000–1500（约 ¥7k–11k），首批约 100 台，德/比渠道，2026-09 欧洲开售；另有 Duncan 系列面向神经多样性与学龄前儿童。老年情感陪护/机构康养仪表盘属后期阶段雷达；仿生熊猫+触觉+B 端康养数据不是当前学生线硬件。Duncan 仅作学生/儿童陪伴旁证，不新开医疗/神经多样性硬件工单。
- 建议下一步：安安挂「阶段雷达·老年后期」；Duncan 备注旁证学生线情感陪伴，不复制新 id。不把欧洲仿生康养价带写成 Memoria 对标。
- 来源：https://www.prnewswire.com/news-releases/mind-with-heart-robotics-brings-anan-panda-robot-to-europe-at-ifa-berlin-2026-302864722.html ；https://news.pedaily.cn/202609/568625.shtml ；https://theaiinsider.tech/2026/09/05/mind-with-heart-robotics-launches-anan-panda-robot-in-europe/
- 开发备注：与 Doova/HUA-H 同类阶段雷达；不复活任何已关闭反定位 id。

### R-20260915-01 糯宝 Robie / Pophie（InsBotics）反定位·桌面萌宠

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-15
- 最近更新：2026-09-15
- 为何现在相关：CES/桌面 AI 生命体路线；官方售价约 $399；声纹+人脸、主动互动、订阅云脑（标 $10/$20 月）。与 Microduck / 二白Mini / Bubbo 同类桌面萌宠/潮玩形态，不是 memoria 当前学生陪伴档案终端叙事。
- 建议下一步：标纯反定位；不对照改 SKU/价带；不新开萌宠工单。年轻人潮玩属再后阶段，亦不把 Robie 当该阶段样板去抄。
- 来源：https://pophie.com/zh/shop/pophie ；https://pophie.com/zh-Hant/press-kit ；https://www.52audio.com/archives/265603.html ；https://eu.36kr.com/zh/p/3630131383977225
- 开发备注：与 Bubbo / 二白Mini / Microduck 同类纯反定位，不并入 Amoo 阶段雷达。不对照改当前 ESP-VoCat SKU 或 ¥499–1299 价带。

### R-20260906-01 小程序控制面待验 P1/P2

- 类别：产品技术
- 状态：进行中
- 首次写入：2026-09-06
- 最近更新：2026-09-14
- 吸收：R-20260902-03
- 为何现在相关：小程序是控制面，不承诺微信实时语音（原 R-20260831-14 已完成）。账号级设备发现已上传开发版；偏好保存、下拉刷新与 Wi-Fi 列表滚动已完成。设备侧已有 idle / listening / speaking，小程序会话态未对齐。
- 建议下一步：剩余包括服务端权威会话态投影、小程序消费、按使用人分配入口（R-20260911-05）与同微信手机/电脑/开发工具三端验收，不是只验 UI。执行见 `TODOLIST.md` P1-03～05。`scope.record` / RecorderManager 仅限 profile 页自定义音色样本，既有门禁保留；不恢复实时媒体或手机声纹登记。
- 来源：
- 开发备注：2026-09-11 逐项对代码核销——`bind` 的 `familyName`/`familyDrafts` 确实不进请求体（`apps/miniprogram/tests/bind-flow.test.js:355` 断言，理由是「家庭名称与其他成员可以稍后添加」）；`_savePreference` 已有成功/失败双向 toast（`apps/miniprogram/pages/profile/index.js:731`）；下拉刷新 5 个页面各自 `enablePullDownRefresh: true` 且都有 `onPullDownRefresh` handler；Wi-Fi 列表已可滚动（`apps/miniprogram/pages/device-onboarding/index.wxss:58-59` 的 `max-height: 360rpx; overflow-y: scroll`）。所谓「家庭邀请」是另一个概念，已移到 R-20260911-05。HTML 原型 `apps/miniprogram/design-preview/memoria-mobile-redesign.html` 仍不是生产小程序。

### R-20260911-05 按使用人切换人格与音色（同一设备给本人/父母/子女用）

- 类别：产品技术
- 状态：进行中
- 首次写入：2026-09-11
- 最近更新：2026-09-14
- 为何现在相关：2026-09-11 用户明确同一设备按指定使用人切换人格与音色，最终使用人无需操作小程序；这不等于家庭邀请。当前服务端分配、自定义人格和音色归属已实现，剩余是控制入口、运行版本对齐、安全点重协商与真机实听，不能重开已完成后端。
- 建议下一步：执行 `TODOLIST.md` P1-03，先以 app_confirm 手动指定打通端到端：补小程序「设备与成员」与分配写入、核线上 Control/schema、验证切主体与取消分配回落。修正 advertised `voice_question` 与写接口只接受 `app_confirm` 的矛盾，暂不宣传未实现确认方式。多成员声纹/设备选人单列 P2-02；自动认人仍需独立身份与同意前提。人格文本和音色由服务端生效，设备不持有 persona 本体不是缺陷。
- 来源：仓库内证据为主，见开发备注
- 开发备注：2026-09-14 按代码核销。`services/identity/postgres_schema.sql` 已有 `identity_persona_assignments` / `identity_custom_personas`；`routes/persona_assignment.py`、`routes/custom_personas.py`、`session_companion.py` 与相关 API/readend 测试已存在，旧「只完成 T01、T02–T05/增量二未开始」作废。`profile_service.py` 解析顺序为 subject 覆盖→binding 默认→全局兜底；voice schema 已用 `custom_persona_id` 与部分唯一索引替代旧账号级单 active，所以归属是 person→persona→voice，不能再按旧索引重建。边界保持：内置人格只读；自定义创建即冻结、改动须新建；绑定本人克隆声音，未就绪回落设计音色。小程序目前只读显示，尚未调用 persona-assignments 写接口；线上 Control 是否部署全部新代码需独立核验。失效通知与安全点重协商已有代码，真实在线切人验收待补。`speaker/authority.py` 仍只写 owner，无逐人 guest 登记；设备端选人体验仍未实现。邀请/指定使用人不赋予 owner 声纹权限。

### R-20260907-03 SiphonAI 可借鉴协议工程，不换栈

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-07
- 为何现在相关：SiphonAI 是 SIP↔WebSocket 媒体桥，daemon 内禁止 STT/LLM/TTS，与「Go Media Edge 不调模型」同构。值得学协议当公开 API、conformance harness、20 ms 热路径、掉线保会话。generation fence / Actual Heard 已更严，不要退化。
- 建议下一步：只评估不改产品行为的四件事：(1) 假 ESP32 客户端打真实 Edge 的 media-v2 harness；(2) Bridge/Agent 宕机时 Edge 本地短提示 + 有界重连；(3) WSS 丢帧/乱序投影进 ConversationProjection UNCERTAIN，不改 commit；(4) 切流 dry-run 验未知配置键。不要引入 siphon-rs/forge-media。PSTN 入线当前不做（R-20260907-04）。
- 来源：https://github.com/thevoiceguy/siphon-ai
- 开发备注：

---

## 明确不做

| id | 禁止 |
| --- | --- |
| R-20260909-04 | 用 VoiceMem / Mem0 / 本地 Qdrant / `livekit-plugins-voicemem` 替换终身档案，或引入平行 ASR/声纹/情绪。不能用抽取后的记忆图替代 Evidence Event 与 `history_eligible` 历史。数字分身不能当成「第二个相同的用户」。 |
| R-20260907-05 | 用 siphon-ai / forge-media 替换 Go Media Edge，或现板开 `auto_clear` barge-in。 |
| R-20260907-04 | 当前不做 PSTN 电话入线；若以后要，另开工单，不得把主叫号码标成 owner。 |
| R-20260831-15 | 播放期 KWS、谎称已验证 AEC、把 TurnPhase 从 shadow 改成有副作用。VoCat 可走协商 `interrupt_assist`，hello 不得写 `aec_reference_verified=true`。 |
| R-20260831-16 | 用 LiveKit client-sdk-esp32 替换 Go Media Edge。 |
| R-20260831-17 | 打开 `USE_REALTIME_CHAT` / SeekAudio AEC 充当已验证 AEC。 |
| R-20260831-18 | 再抬 DTLN makeup。低 RMS 先比 tap WAV pre/post DTLN。 |
| R-20260831-19 | 生产拉入 X2-Turn 4B 权重。 |
| R-20260831-20 | 宣称全双工、持续聆听、情感灵魂、替代亲情、家庭入口、唤醒 99%。不要开 `expressive=True`。 |
| R-20260908-01 | 在退役 ES8388 / ATK 上深改 ALC/AEC，或把停产写成全双工借口。 |
| R-20260911-06 | 在当前架构上开工「自动认人切换人格/音色」：单账号只有一条 owner 声纹（`services/speaker/authority.py:350` 写死 `owner`），无 guest enrollment 与 person 映射，必须先把多家庭成员声纹作为独立工单做完。不得把 `profile_id` 当作 `person_id`。 |

---

## 已关闭

同一想法复现时合并到对应活 id 或「明确不做」，不要新建条目。

| id | 结论 |
| --- | --- |
| R-20260831-03 | 废弃：ES8388 远场调音。现板 VoCat PGA 36.0 dB，DTLN 冻结。 |
| R-20260831-04 | 并入 R-20260907-01：陪伴感靠 generation fence。 |
| R-20260831-05 | 废弃：立创实战派采购。 |
| R-20260831-07 | 完成：Dense-Mem confirm/trace 已进 PG + 小程序回顾。召回增强走 R-20260909-03。 |
| R-20260831-08、R-20260831-09、R-20260831-10、R-20260831-11 | 并入「当前站位」。 |
| R-20260831-12 | 完成：`GET /v1/account/delivered-capabilities` + 小程序「我的」仪表。 |
| R-20260831-13 | 完成：小程序 AI 标识 / 2h 提醒 / 训练默认关。余下合规走 R-20260901-08。 |
| R-20260831-14 | 完成：小程序控制面，不承诺微信实时语音。待验走 R-20260906-01。 |
| R-20260901-01 | 并入 R-20260831-01。 |
| R-20260901-02、R-20260902-01、R-20260903-01、R-20260904-01 | 并入 R-20260831-02 FunASR 钉档。 |
| R-20260901-03、R-20260901-05、R-20260901-09、R-20260903-03 | 并入 R-20260901-08。 |
| R-20260901-04 | 过时：立创 MIC3 AEC 清单。 |
| R-20260901-07 | 过时：ATK ES8388 `AEC_MODE_SR_*`。 |
| R-20260901-10、R-20260902-05、R-20260902-06、R-20260903-02、R-20260904-02、R-20260904-03 | 并入「当前站位」。 |
| R-20260901-11 | 并入 R-20260831-02：Nano/GGUF 只作离线档案回放。 |
| R-20260902-02 | 并入 R-20260907-01。 |
| R-20260902-03 | 并入 R-20260906-01：设备侧三态已有，小程序会话态未对齐。 |
| R-20260911-01 | 并入 R-20260907-01：统一评估 1.6.10→1.8.1；DuplexModel 不接生产，不作为设备全双工证据。 |
