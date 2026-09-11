# Memoria 研究跟踪

本文件是仓库唯一的外部研究扫描落地处。研究助手（memoria提升大师）每次扫描只更新这一份文件，不另建文档。开发在本文件评估条目、改状态、写备注。

`RESEARCH.md` 是文档预算的唯一例外。规则、产品入口和线上状态仍分别只写在 `PROJECT_RULES.md`、`README.md`、`HANDOFF.md`。本文件不写生产密钥、环境路径、镜像 SHA、设备 ID 或运维细节。

## 怎么用

1. 研究助手扫描后：同一想法复用已有 id 并更新「最近更新」；新想法新增 `R-YYYYMMDD-NN`，状态先标「待评估」。已关闭 id 见文末，禁止用新 id 复活同一想法。
2. 开发评估后：改「状态」「最近更新」「开发备注」。已完成、过时或被后继条目替代的内容删除，仍有效的结论并入活条目或「明确不做」。
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

- 扫描：2026-09-11。设备工单只写 `HANDOFF.md`（`vocat_interrupt_assist`：表情照片、barge-in、长天气、主人匹配、小程序 0.8.84）。记忆召回（R-20260909-03）另轨：`RecallPlanner` 已加封闭 query rewrite，长程三项 `recall@5=1`；整体 `recall@5=0.8125`，仍不开预取。按使用人切换人格与音色是 2026-09-11 明确的新产品方向，见 R-20260911-05。
- SKU：ESP-VoCat，默认 `interrupt_assist`。hello 报 simultaneous capture + `aec_mode=fd_low_cost`，`aec_reference_verified=false`。对外 `advertised_duplex_level=none`。`direct_real_device_verified=false`。ATK ES8388 半双工 demo 已退役。
- 唤醒词「茉莉」。播放期 KWS 关；说话中 BOOT / 触摸硬停。DTLN makeup 冻结 `8.0×`。安静环境阶段 4 茉莉 10/10、5 分钟误唤醒 0；电视/家庭噪声仍要记数。
- 钉档：云端 FunASR 仍 1.4.15（无 1.4.16/1.5.0；钉档评估仍 R-20260910-01；已测 NumPy 2，旧 `numpy<2` 不再是硬约束）。sidecar ≥1.3.29。livekit-agents PyPI latest=1.8.1（2026-09-10；含 DuplexModel）；仓内仍 1.6.10。升级评估见 R-20260907-01 + R-20260911-01。#7064 已随 1.8.0 合并（NC 时默认关 AGC）；stale「open」索引作废。LiveKit 设备路径保持 `interruption.enabled=False` + `preemptive_generation.enabled=False`。
- 合规：拟人化办法已生效；令第25号已生效，声纹仍要单独同意、本月无新细则。最高法涉人工智能纠纷意见（法发〔2026〕10号，司法意见非 CAC 新法）加强见 R-20260910-02。大型处理者征求意见截止已过、截至 2026-09-11 仍无定稿；清朗无新实质。NEW_LAW_IDS（法规/CAC）空。

## 当前站位（已采纳，不是工单）

公开故事：家庭桌面记忆终端 / 声纹档案音箱。Pitch：声纹门禁 + 按需档案 + 带标识导出 + 可证明删除。五个吉祥物是 IP / 轻订阅 / 壳，不是五个人格大模型。主用户是家里的桌子，小孩是被门禁的说话人。订阅卖档案容量 / 家庭席位 / 导出，不是情感月费。价格带对照钉钉 A1 / 安克×飞书的录音+转写（约 ¥499–1299），不对照萤石 RK3 适老看护或万元级人形。

不是：7 寸数字人、跌倒看护、智家中枢、全屋 OS、常在情感、领养/生命模块、运动玩具、耳机 Agent、微信实时语音。Bubbo / 二白Mini / JUOS / Microduck / Plaud One / 优必选 U1 / 涂鸦 Doova / 青心意创 Amoo / Lingverse iKairos / 镭萌 bibo / 华拟 HUA-H 只作反定位日历。iKairos 见 R-20260911-02；bibo / HUA-H 见 R-20260911-03 / R-20260911-04；Doova / Amoo 仍昨日条目（R-20260910-03 / R-20260910-04）；其余不新开 id。童声 500–800 ms 停顿写成「不截断」，不写成「更懂情绪」。

2026-09-11 反定位日历：优必选 U1 声称 2026-09-16 起交付（T-5），仍无「已开始交付」实锤；全年可交付仍 1,500–2,000 台（对比预售约 1.34 万 / 13,361），柳州工厂延期，京东仍约 60 天 / 9/15 后有货（https://leaderobot.com/news/9425 ；https://www.163.com/dy/article/L5U2PKT60511U82T.html）。海信 JUOS 约 8/31–9/3 起对首批电视/投影推 Sep OTA；小聚识人非默认，声纹/人脸需登记（https://www.3elife.net/Art/ie/202609/04/109709.html）；仍是 TV/AIOS 全家中枢 ≠ 桌面档案终端。Plaud One 仍 explorer $249.99，未见大陆零售 SKU。Microduck 灰色市场国内约至 ¥5567（https://www.yicai.com/news/103350821.html）。华泰 ¥2000–4000 价带本月无修订。iKairos 见 R-20260911-02；bibo / HUA-H 见 R-20260911-03 / R-20260911-04。

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

### R-20260831-02 FunASR 空转写分账与钉档

- 类别：语音
- 状态：进行中
- 首次写入：2026-08-31
- 最近更新：2026-09-10
- 吸收：R-20260901-02、R-20260902-01、R-20260903-01、R-20260904-01、R-20260901-11
- 为何现在相关：空转写仍在真机路径上出现。云端钉写 `funasr>=1.4.14`（含 1.4.12 长段 partial、1.4.13 #3591 VAD overrun、1.4.14 默认 8s partial 窗），升钉评估见 R-20260910-01。sidecar SenseVoice 钉 ≥1.3.29。llama.cpp GGUF 空白是另一条路径。**注意钉不在本仓**：`funasr_stt.py` 经 `websockets` 直连 DashScope `fun-asr-realtime`（`services/agent/src/providers/funasr_stt.py:78,116`），`pyproject.toml`/`uv.lock` 都没有 `funasr` 包；sidecar 是服务器上的独立镜像（`SENSEVOICE_URL` 指向，见 `infra/memoria.env.production.example:304`）。
- 建议下一步：真机按 empty+vendor_error / empty+silent / empty+gating / low_rms 补 receipt。sidecar 空时间轴先核版本。不要为对齐 ASR 终点去拧设备 VAD。Fun-ASR-Nano / GGUF 只可作离线档案回放，不上 ESP32、不替代实时路径。升钉 1.4.15 先看空转写分账与流式 VAD 边界。
- 来源：https://github.com/modelscope/FunASR/releases/tag/v1.4.14 ；https://github.com/modelscope/FunASR/pull/3591 ；https://github.com/modelscope/FunASR/releases/tag/v1.3.29 ；https://pypi.org/project/funasr/1.4.15/ ；https://github.com/modelscope/FunASR/releases/tag/v1.4.15
- 开发备注：Agent 已有 `funasr_empty_accounting`。2026-09-10 PyPI latest=1.4.15，无 1.5.0；1.4.15 已测 NumPy 2，旧 `numpy<2` 不再是硬约束。新 id 不并入关闭表，待开发评估。

### R-20260910-01 FunASR 钉档评估 ≥1.4.15

- 类别：语音
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-10
- 为何现在相关：PyPI funasr 1.4.15 于 2026-09-09T04:03:45Z 上传（1.4.14 为 2026-09-03）。What's new：已测 NumPy 2 兼容；修流式 KWS/VAD 边界与 checkpoint ranking。Voice Core/Agent 用 FunASR；旧钉 ≥1.4.13/1.4.14 已吸收进 R-20260831-02。1.4.14 时代的 `numpy<2` 护栏不再是硬约束。
- 建议下一步：评估把云端钉从 `funasr>=1.4.14` 升到 `>=1.4.15`（`python -m pip install -U "funasr==1.4.15"`）。先看空转写分账与流式 VAD 边界，再改约束。不因此改设备 VAD，不上 Fun-ASR-Nano 替实时路径。
- 来源：https://pypi.org/project/funasr/1.4.15/ ；https://github.com/modelscope/FunASR/releases/tag/v1.4.15
- 开发备注：2026-09-10 现场核 PyPI latest=1.4.15，无 1.5.0。活工单仍是 R-20260831-02。2026-09-11 更正：本仓不是消费方——实时路径是 DashScope 云 `fun-asr-realtime` websocket（`funasr_stt.py:78,116`），`funasr` 包只可能存在于服务器侧 sidecar 镜像；升钉要改的是服务器 sidecar，不是 `pyproject.toml`，因此**不走** agent-only 快速通道门禁。落地前需先确认 sidecar 镜像的构建来源（HANDOFF 记「Dockerfile 只在服务器该目录，重建不可复现」）。

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

### R-20260907-01 LiveKit Agents 1.8.0 升级评估

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-07
- 最近更新：2026-09-11
- 吸收：R-20260831-04、R-20260902-02
- 为何现在相关：PyPI livekit-agents 已有 1.8.1（2026-09-10；含 DuplexModel），细节与 DuplexModel 评估见 R-20260911-01。仓内仍钉 1.6.10。#7064 已随 1.8.0 合并：changelog 写明 NC→默认关 AGC；`AudioInputOptions.auto_gain_control` 为 `NotGivenOr[bool]=NOT_GIVEN`，省略时若已配 noise cancellation 则 AGC 关。GitHub 索引页曾误显示 open，以 tag / 源码为准。#7104 是 OTel/PII 破坏性变更。文档仍默认打断/抢跑为开。陪伴感靠 generation fence 丢掉 thinking 中的旧 generation，不靠抢话。
- 建议下一步：仍先评估 1.6.10→1.8.0（或经 1.8.0 再到 1.8.1），核 traces 是否还带对话原文，以及 redaction 与现有低基数 telemetry 是否冲突。LiveKit 设备路径保持 `interruption.enabled=False` + `preemptive_generation.enabled=False`。不要开 `user_turn_limit` 或 `expressive=True`。#7064 不是开 barge-in 的理由。
- 来源：https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.0 ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.1 ；https://pypi.org/project/livekit-agents/1.8.1/ ；https://github.com/livekit/agents/pull/7064 ；https://github.com/livekit/agents/pull/7104 ；https://docs.livekit.io/reference/agents/turn-handling-options/ ；https://pypi.org/project/livekit-agents/
- 开发备注：2026-09-10 复核 #7064 = merged in 1.8.0（merged_at 2026-08-31）。不要再写「仍 open」。1.8.1 + DuplexModel 另钉 R-20260911-01，不并入本条。

### R-20260911-01 LiveKit Agents 1.8.1 / DuplexModel 评估（不上线宣称全双工）

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-11
- 为何现在相关：PyPI livekit-agents 1.8.1 于 2026-09-10T18:59:28Z 上传；GitHub release livekit-agents@1.8.1 published 2026-09-10T21:26:43Z。相对 1.8.0：新增 DuplexModel（full-duplex speech models），首个实现 OpenAI GPT-Live；另有若干 voice/telemetry/TTS pool/false-interruption resume 测试修复。仓内仍钉 1.6.10；R-20260907-01 仍是 1.8.0 升级评估。memoria 当前协商上限 `interrupt_assist`、对外 `advertised_duplex_level=none`、`full_duplex_verified=false`——云侧出现 DuplexModel 不等于设备侧可宣传全双工。
- 建议下一步：评估 1.6.10→1.8.1（或先 1.8.0 再 1.8.1）时把 DuplexModel 标为实验观察，不接生产会话路径；LiveKit 设备路径继续 `interruption.enabled=False` + `preemptive_generation.enabled=False`；不要因 DuplexModel 打开 `user_turn_limit` / `expressive=True` / 宣称全双工。VoiceMem 插件仍要求 `livekit-agents<1.8`，不能用 1.8.1 当 VoiceMem 升级借口。
- 来源：https://pypi.org/project/livekit-agents/1.8.1/ ；https://github.com/livekit/agents/releases/tag/livekit-agents%401.8.1 ；https://docs.livekit.io/agents/models/realtime/#full-duplex
- 开发备注：与 R-20260907-01 并存；本条专钉 1.8.1 + DuplexModel。不把云侧 duplex API 写成 VoCat T1–T14 已通过。

### R-20260907-02 VoCat AEC / barge-in 真机未验

- 类别：硬件
- 状态：进行中
- 首次写入：2026-09-07
- 最近更新：2026-09-11
- 为何现在相关：出货板已是 VoCat（ES7210 + ES8311）。协商 `interrupt_assist` 已切流，AEC residual、真机 barge-in 和 T1–T14 仍未过。现场步骤只写 `HANDOFF.md`。xiaozhi-esp32 #2036（ES7210 MIC3 作 AEC reference，MMR vs MR）仍与现板路径相关，不是已验证 barge-in。
- 建议下一步：量 AEC residual，真机测打断。未过证不得改 `aec_reference_verified` 或宣传全双工。立创附件 `vocat_xiaozhi_1_1_0.bin` 只作适配参考，不要抄「萌宠全双工」叙事。LiveKit ESP32 指南可作 ES7210 `0x80` / ES8311 `0x30` 初始化参考。
- 来源：https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp-vocat/index.html ；https://livekit.com/blog/esp32-custom-hardware-quickstart ；https://github.com/78/xiaozhi-esp32/issues/2036
- 开发备注：hello 已报 simultaneous capture / fd_low_cost；Agent barge-in 跟 `audio_mode`。2026-09-11：#2036 仍 open，`updated_at` 仍 2026-07-07，无新活动；不外推为全双工证据。

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
- 最近更新：2026-09-11
- 为何现在相关：VoiceMem（v0.0.2）是实时语音智能体的记忆检索层：左脑事实、右脑人格/情绪笔记，ingest 抽事实、search 把 Top-K 注入回复。宣传 LoCoMo ~91%、PersonaMem ~69%、检索 ~134ms、每轮约 300–430 memory token。它不能替代 Memoria 的对话档案。与已落地的 Dense-Mem confirm/trace（原 R-20260831-07）同族：增强现有 catalog / persona / recall，不换栈。当前工单仍是 VoCat interrupt_assist，本条另轨。第三方 `livekit-plugins-voicemem` 0.2.2（PyPI 2026-09-01）是 pgvector 记忆插件，要求 `preemptive_generation` 关，且声明 `livekit-agents<1.8`，与仓内要评估的 1.8.0 / 现已发布的 1.8.1 仍不兼容。
- 建议下一步：不 pip install voicemem / livekit-plugins-voicemem、不克隆、不接音频。只喂已 commit 且 `history_eligible=true` 的主人文本；输出只能当候选 Memory Claim。硬约束见 R-20260909-04。长程三项已过，但整体 `recall@5` 仍 0.8125（其余无共享词转述，如「避开」≠「香菜」）；不要开工预取、双通道注入或平行记忆库。新零重叠句式先加评测再扩封闭词表。
- 来源：https://github.com/xzf-thu/VoiceMem ；https://xzf-thu.github.io/VoiceMem/ ；https://arxiv.org/pdf/2608.26005 ；https://pypi.org/project/livekit-plugins-voicemem/0.2.2/
- 开发备注：2026-09-09 只读评估。LICENSE Apache 2.0；捆绑模型另有许可。左脑底层 Mem0 + 本地 Qdrant，默认不是多进程安全的生产 catalog。2026-09-10：插件版不能当升级路径，也不用来换 PG/MinIO 档案栈。同日扩 `memory_eval_zh_v1` 并让评测查询走生产 RecallPlanner。同日在 `RecallPlanner` 加封闭 rewrite（不改 archive、不设 entity 过滤）：「小朋友/小孩子/孩子」扩 `儿子/女儿` 及已确认子女别名；「难受/不开心/伤心/委屈」扩 `难过`；「小朋友」不误匹配「朋友」。`memory_eval_zh_v1`：跨会话 / 转述追问 / 安慰 `recall@5` 均为 1.0；隔离/候选泄漏仍 0；整体 `recall_at_5=0.8125`、`ndcg_at_10≈0.734`。抽取器仍抽不出「我儿子小周对猫毛过敏」的人物，所以转述靠词表而非实体。不引入 Mem0。2026-09-11：`livekit-plugins-voicemem` 0.2.2 仍声明 `livekit-agents<1.8`，与 1.8.1 仍不兼容；不因此换栈。

产品拆两层，默认只做第一条：

1. 记住用户（了解你）：从主人权威对话抽出稳定事实、关系、近期事件。对应 memory catalog（life_story / daily_life / 人物关系 / episode），candidate → confirmed；实时只把 confirmed 打进 MemoryContextClient。
2. 模仿用户（变成你）：口癖/句长/语速走 persona 胶囊；数字分身走 Companion / Self Preview，须披露「授权模拟，不能替本人作决定」，模拟输出不得反哺主人证据。VoiceMem 右脑是给模型看的内部笔记，不是分身引擎。

「像正常人对话」不是第三块记忆库：日期是运行时上下文，查询是工具，关联旧事才要证据档案 + recall。安慰分三层——当轮 `emotion_observation`、persona 表达习惯、「上次你很难过」才是记忆 claim。合成一块右脑图会把当天心情写成性格。

现有权威链不得拆：EvidenceEvent / ArchiveSink / PG + MinIO；主人历史只消费 `history_eligible=true`；事实走 extractor + write policy + catalog；召回走 RecallPlanner + `catalog.context(confirmed_only=true)`（超时 0.3s，最多 8 条）；说话方式走 persona；当轮情绪走 `emotion_observation`；guest / uncertain 不进主人历史、私人记忆和工具。`memory_eval_zh_v1` 已覆盖跨会话 / 转述追问 / 安慰召回；日历窗与封闭 rewrite 已过这三项，其余无共享词转述仍可能漏。

后续四步（均在现有栈上）：

1. 说话过程中 query-conditioned 预取。在主人 partial / 即将 final 的转写上调用 RecallPlanner + `catalog.context()`，写入 MemoryContextClient 缓存。失败沿用旧缓存或空；不准改 archive、不准挡热路径、不准用 guest/uncertain 文本查询。
2. 双通道注入 + 硬 token 预算。事实走记忆块；人格/情绪走独立块，只塑造语气，禁止念给用户听。把「最多 8 条、snippet 上限 4000 字」收成可观测的 memory-token 上限。
3. 长程评测，不换引擎。跨会话 / 转述追问 / 安慰召回已进 `memory_eval_zh_v1`，三项 `recall@5=1`。整体 `recall@5` 仍 0.8125，因此第 1、2 步暂不开工。
4. 可选：若第 3 步显示「问人问时间仍搜成语义大杂烩」，再加强 slot/entity 路由，仍落在 `catalog.search` 的 filter，不引入 Mem0。

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
- 最近更新：2026-09-11
- 吸收：R-20260901-03、R-20260901-05、R-20260901-09、R-20260903-03
- 为何现在相关：令第25号已生效，声纹仍要单独同意，2026-09-11 无新声纹细则。大型处理者征求意见截止已过、截至 2026-09-11 仍无正式文本/延期公告/解读（https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm）；当前按小型处理者，不扩编守门人组织。清朗转载无新实质。最高法涉人工智能纠纷意见是司法意见不是 CAC 新法，拟声/训练语料见 R-20260910-02。导出是用户语音+模型回复混合物，执法点在文件标识。拟人化办法的落地页/2h 提醒已在小程序完成（原 R-20260831-13）。
- 建议下一步：默认云端只存转写+记忆条目；原始 wav/特征不长期留、不用于声纹登录。声纹单独同意，不并入一般同意。导出 JSON/音频包里模型侧显式「AI 生成」，元数据写服务提供者+内容编号；TTS 对齐 `aigc_watermark` + `aigc_metadata`。五个吉祥物禁止亲属/伴侣角色。家庭邀请只给控制面权限，写档案仍要设备端 owner 声纹。继续盯大型处理者正式文本，不新开法规 id。
- 来源：https://www.cac.gov.cn/2026-07/24/c_1786638889704872.htm ；https://www.cac.gov.cn/2026-08/07/c_1787851071612596.htm ；https://www.gov.cn/zhengce/zhengceku/202503/content_7014286.htm ；https://docs.volcengine.com/docs/6561/1598757
- 开发备注：2026-09-11 NEW_LAW_IDS（法规/CAC）空。Seed-TTS `aigc_watermark` / `aigc_metadata` schema 仍 NO_CHANGE。不把大型处理者征求意见稿写成已生效。

### R-20260910-02 最高法涉人工智能纠纷意见（法发〔2026〕10号）

- 类别：合规
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-11
- 为何现在相关：2026-09-07 公布《最高人民法院关于依法审理涉人工智能纠纷案件的意见》，全国法院首份涉 AI 纠纷规则，5 部分 / 24 条。这是司法意见，不是 CAC 新法规。直接相关：规范 AI 换脸/拟声；「未经同意使用自然人声音作为训练语料，模仿其音色、语调和发音风格生成可识别的合成人声的，构成对声音权益的侵害」；另有生成式 AI 服务提供者责任、AI 幻觉等。陪伴 TTS / 可选声音克隆 / 声纹档案要把第三方声音训练与拟声当民事风险。
- 建议下一步：产品同意书与导出标识对齐拟声/训练语料单独同意；不要把大型处理者征求意见稿写成已生效。合规落地仍走 R-20260901-08。
- 来源：https://www.chinanews.com.cn/gn/2026/09-07/10691909.shtml ；https://legal.gmw.cn/2026-09/07/content_38989161.htm ；http://paper.people.com.cn/rmrb/pc/content/202609/08/content_30179776.html ；https://www.ncsti.gov.cn/kjdt/xwjj/202609/t20260910_255808.html ；https://paper.people.com.cn/rmrbhwb/pc/content/202609/08/content_30179682.html ；https://finance.sina.com.cn/jjxw/2026-09-10/doc-inirihnw5380235.shtml
- 开发备注：2026-09-11 追加次级来源（国家科技管理信息系统 2026-09-10 转载；人民日报海外版 2026-09-08）。米哈游 AI 变声民事案（媒体 9/9–10，判决约 6/30）仅作拟声风险旁证，不新开法规 id。法规/CAC 的 NEW_LAW_IDS 仍空。本条因声音/拟声对 TTS/声纹路径实质相关而单开。清朗转载不新开 id。

### R-20260910-03 涂鸦 Doova（IFA 2026）反定位

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-11
- 为何现在相关：界面新闻 2026-09-04 报道涂鸦 IFA 推出 Doova：适老独居陪伴机器人，LDS 雷达、4 麦声源定位、跌倒/姿态检测、移动巡航、IoT 中枢、聊天陪伴。这是移动看护 + 全屋 IoT 陪伴，不是桌面档案语音终端。
- 建议下一步：公开叙事继续写家庭桌面记忆终端 / 声纹档案音箱。不要把跌倒响应、室内巡逻或智家中枢写进当前 SKU。不新开适老看护工单。
- 来源：https://www.jiemian.com/article/15059778.html ；https://www.itheat.com/view/63447.html
- 开发备注：2026-09-11 追加次级对比稿（ITHeat 2026-09-09）。与 Bubbo / 萤石 RK3 / JUOS 同属反定位日历；本条只钉 Doova。不复制新 id。

### R-20260910-04 青心意创 Amoo（IFA 2026）反定位

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-10
- 最近更新：2026-09-11
- 为何现在相关：ITBear 2026-09-09 报道青心意创 Amoo 在 IFA 2026 以软毛角色/萌宠情感陪伴出场。这是情感领养/萌宠路线，不是家庭桌面记忆终端或声纹档案音箱。
- 建议下一步：五个吉祥物继续当 IP / 轻订阅 / 壳，不要写成常在情感或生命模块。不对照 Amoo 的萌宠叙事改 SKU。
- 来源：https://www.itbear.com.cn/html/2026-09/1548976.html ；https://www.itheat.com/view/63447.html ；https://finance.sina.com.cn/jjxw/2026-09-08/doc-inirauhy6075402.shtml
- 开发备注：2026-09-11 追加次级对比稿（ITHeat 2026-09-09；新浪财经 2026-09-08）。与 Bubbo / 二白Mini 同属反定位日历；本条只钉 Amoo。不复制新 id。

### R-20260911-02 Lingverse iKairos（IFA 2026）反定位

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-11
- 为何现在相关：机器人大讲堂 2026-09-08 报道新加坡 Lingverse 的 iKairos：圆形「神经核心」可当项链或嵌入桌面机器人头部；定位「AI记忆伙伴」/ LifeOS 持续观察构建用户画像；主动 Agent 行为；物理摄像头滑盖+静音键；尚在开发、定价/上市未公布；Pre-A $29M（2026-07）。这是随身常在记忆感知 + 模块化萌宠桌面，不是家庭桌面声纹档案终端 / 按需证据档案。
- 建议下一步：公开叙事继续写家庭桌面记忆终端 / 声纹门禁 + 按需档案 + 显式导出 + 可证明删除。不要跟项链随身、常在环境采集或主动替用户发消息的 Agent 叙事。不新开可穿戴工单。
- 来源：https://leaderobot.com/news/9497
- 开发备注：与 Plaud One / Doova / Amoo / Bubbo 同属反定位日历；本条只钉 iKairos。

### R-20260911-03 镭萌 bibo ¥1499 潮玩非语言情感（反定位）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-11
- 为何现在相关：36氪 2026-09-09：杭州镭萌科技千万级天使轮；潮玩非语言情感机器人 bibo 定价 ¥1499（报道称 6 月已上市）。东方财富/每经 2026-09-10 转载：大厂创业者涌入「非语言交互」消费级具身（bibo / Ropet / BubblePal）。这是情绪潮玩 / 反语音对话红海，不是声纹门禁 + 按需证据档案终端。
- 建议下一步：公开叙事继续桌面记忆终端 / 可导出档案，不要跟非语言萌宠或「不靠说话」情感玩具比拼。不新开潮玩工单。
- 来源：https://eu.36kr.com/zh/p/3975777320284422 ；https://finance.eastmoney.com/a/202609103870922661.html
- 开发备注：与 Amoo / Bubbo 同属情感反定位日历；本条只钉 bibo 价带与融资信号。

### R-20260911-04 华拟智能 HUA-H 仿生人头养老陪护（反定位）

- 类别：市场定位
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-11
- 为何现在相关：动脉网/VBData 2026-09-10：东莞华拟智能完成数千万元天使轮，仿生人头养老陪护产品 HUA-H。资本继续涌入仿生人形头 / 养老情感，不是固定桌面声纹档案音箱。
- 建议下一步：不要把人形头/养老陪护写进当前 VoCat SKU 或公开 pitch。不新开养老看护工单。
- 来源：https://www.vbdata.cn/1519092689.html
- 开发备注：与 U1 / Doova 同属人形/看护反定位；本条只钉华拟融资。

### R-20260906-01 小程序控制面待验 P1/P2

- 类别：产品技术
- 状态：进行中
- 首次写入：2026-09-06
- 最近更新：2026-09-11
- 吸收：R-20260902-03
- 为何现在相关：小程序是控制面，不承诺微信实时语音（原 R-20260831-14 已完成）。账号级设备发现已上传开发版；偏好保存、下拉刷新与 Wi-Fi 列表滚动已完成。设备侧已有 idle / listening / speaking，小程序会话态未对齐。
- 建议下一步：只剩一项——同微信手机/电脑/开发工具三端验收（现场状态见 `HANDOFF.md`）。`app.json` 的 `scope.record` / RecorderManager 与服务端允许的自定义音色样本范围一致（`apps/miniprogram/tests/no-realtime-media-gate.test.js` 已钉住「仅 profile 页可用」），不再是冲突项，无需删除。
- 来源：
- 开发备注：2026-09-11 逐项对代码核销——`bind` 的 `familyName`/`familyDrafts` 确实不进请求体（`apps/miniprogram/tests/bind-flow.test.js:355` 断言，理由是「家庭名称与其他成员可以稍后添加」）；`_savePreference` 已有成功/失败双向 toast（`apps/miniprogram/pages/profile/index.js:731`）；下拉刷新 5 个页面各自 `enablePullDownRefresh: true` 且都有 `onPullDownRefresh` handler；Wi-Fi 列表已可滚动（`apps/miniprogram/pages/device-onboarding/index.wxss:58-59` 的 `max-height: 360rpx; overflow-y: scroll`）。所谓「家庭邀请」是另一个概念，已移到 R-20260911-05。HTML 原型 `apps/miniprogram/design-preview/memoria-mobile-redesign.html` 仍不是生产小程序。

### R-20260911-05 按使用人切换人格与音色（同一设备给本人/父母/子女用）

- 类别：产品技术
- 状态：待评估
- 首次写入：2026-09-11
- 最近更新：2026-09-11
- 为何现在相关：2026-09-11 用户明确产品方向——主人初始化后把设备给指定的人用（自己 / 父母 / 子女），机器人随使用人切换人格与音色（孩子用：温柔亲切、关心陪伴；老人用：沉稳、多问候身体、聊过去；自己用：知心朋友），使用者不需要再看小程序。这不是「邀请家人共享设备」，是**同一设备的按人切换**。骨架已在，但三条成熟管线互相没有连线。
- 建议下一步：先在 Control 侧建最小闭环，不碰固件（音色是服务端下发 Opus、人格是 prompt 段，两者都不需要设备改动）：(1) 新增 `(binding, subject_id) → persona` 分配实体，替换 `ProfileAuthority.persona()` 里 `del subject_id` 的行为；(2) 音色归属从 account 级改为 subject 级（需放开 `voice_profiles` 的一账号一 active 唯一索引）；(3) 让 `switch_active_subject` 触发 runtime profile ledger 推进与 Edge `runtime_profile.invalidated`，使在线设备在安全点重协商；(4) 小程序补「设备与成员」页（`bind/index.wxml` 已承诺但页面不存在），邀请关系走后端**已有**的 `/v1/relationships/invites`。**不要**现在做「自动认人切换」——那需要多家庭成员声纹，当前架构不支持。
- 来源：仓库内证据为主，见开发备注
- 开发备注：2026-09-11 只读探查（未改代码）。**已有的地基**：多主体绑定模型（`services/identity/domain.py:732` 的复数 `primary_subject_ids`、roles、`family_space_id`）；事后改绑端点 `POST /v1/devices/{device_id}/binding/supersede`（`services/control_api/app/routes/identity_lifecycle.py:449`，请求体支持 `primary_subject_ids`）；会话内切主体端点 `POST /v1/sessions/{session_id}/active-subject` + 权限矩阵（`multi_subject.py:635`、`multi_subject_runtime.py:510`）；RuntimeProfile 已携带 `active_subject_id`/`subject_category` 且有签名与 fence 校验（`services/agent/src/runtime_profile.py:99`）；人格→system prompt 的单一接缝已能渲染 `【人格】`（`services/agent/src/prompt_composition.py:369,424`）；音色解析/下发全链路（`services/agent/src/agent_voice_profile.py:63`）；配置变更→版本推进→Edge 通知设备→安全点重协商的完整握手（`services/control_api/app/device_control.py:133`、`services/media_edge/device_ws_server.go:261`、`firmware/.../memoria_protocol.cc:1526`）；固件激活清单已定义 `persona_assignment_id`/`primary_subject_display_name`/`robot_name`（只校验不消费）。**完全没有的四块**：① person→persona 映射为零（`services/session_runtime/profile_service.py:293` 的 `persona()` 直接 `del subject_id`；persona 钉在 binding，`supersede` 还强制继承）；② person→voice 映射为零且架构不允许（`services/voice_profile/postgres_schema.sql:87` 的 `idx_voice_one_active` 限制一账号一 active，全表无 person/subject 列；RuntimeProfile 无 voice 字段）；③ 多家庭成员声纹为零（`services/speaker/authority.py:350` 的 identity 由 account 派生 uuid5，注册语句 `:372` 写死 `owner`，无 guest enrollment 路径，因此无法辨别「是谁」）；④ 设备端选人交互与协议为零（`memoria_protocol.cc:1526` 明写设备不持有 persona 数据；四个物理输入 BOOT 短按/长按、点屏、拍打没有一个与选人相关；无语音指令；协议无 persona/subject 消息）。**两个半成品**：小程序无「改用途/换主体/加成员」入口（`supersede` 端点存在但无人调用，`getDeviceBinding` 已定义但无页面调用，`familyDrafts` 收集了不进请求体）；`voice_question` 确认方式在 `allowed_confirmation_methods` 里返回给客户端却**被服务端拒绝**（`multi_subject.py:158` 只允许 `app_confirm`），即「使用者在设备上语音确认自己是谁」这条路是空的。**约束**：人格与设计音色在 companion 目录里强耦合（`services/common/companions.py:31,154`），换 persona 天然换音色，这是目前唯一「人格+音色成对切换」的机制，但它绑 binding 而非 subject。合规上仍走 R-20260901-08：家庭邀请只给控制面权限，写档案仍要设备端 owner 声纹；`reject_non_owner_voice` 不得放宽。

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
