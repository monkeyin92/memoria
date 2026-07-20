# Memoria 实时 ASR、TTS、声音复刻与声纹能力对比

> 研究日期：2026-07-18  
> 证据范围：阿里云百炼官方文档、ModelScope 3D-Speaker 官方仓库、Memoria 当前代码与 `HANDOFF.md`。  
> 本文是选型与架构建议，不代表已经完成模型 A/B 或真实设备听感验收。厂商模型、价格和限流会变化，上线前需重新核对。

## 1. 结论摘要

针对 Memoria 的目标，当前最稳妥的路线不是把级联整条替换成某一个“Realtime”模型，而是保留可审计的级联主链，并把三个能力面拆开建设：

1. **主转写继续使用 Fun-ASR Realtime。** 它提供热词、上下文、字级时间戳和适合长会话的连接模式；这些能力直接服务于 Memoria 的打断、字幕、结构化记忆和“实际已听文本”。
2. **Qwen3-ASR Realtime 继续做非阻塞情感旁路。** 它的 `text + stash` 与 7 类情感很有价值，但公开协议没有热词、上下文 Prompt、字词时间戳，也不提供声纹能力，不适合直接替换当前主 ASR。
3. **TTS 第一优先 A/B CosyVoice v3.5 的复刻音色。** 它能同时保留流式文本输入、流式音频输出、自然语言指令和字级时间戳；这比直接切到 Qwen3-TTS-VC 更契合 Memoria 的可打断主链。
4. **Qwen-Audio-TTS 可作为声音相似度和自然度候选。** 它支持声音复刻与指令，但其复刻音色不支持字级时间戳，直接替换会削弱“用户实际听到哪一个字”的确定性。
5. **Qwen3-TTS Realtime 不是单一全能模型。** 系统音色、指令控制、声音复刻、声音设计被拆成不同模型；VC 模型不能同时获得 instruct 能力，公开 Realtime 协议也没有字词时间戳事件。
6. **声纹识别必须独立建设。** Fun-ASR Realtime、Qwen-ASR Realtime、TTS 复刻音色都不能判断“是不是主人”。当前 `speaker_verify.py` 只是 numpy log-mel mean/std 启发式，应升级为正式的 target-speaker verification。
7. **级联仍应是默认生产主线，端到端语音模型保持隔离 A/B。** GPT Live 式体验的核心是低延迟、自然轮替、真/假打断、旧音频取消和双讲稳定性，不是“模型名称里有 Realtime”。端到端模型也不会自动继承 Memoria 的 GenerationFence、实际已听文本、工具隔离和记忆审计。

## 2. 先澄清产品命名

| 名称 | 实际定位 | 不应混淆为 |
|---|---|---|
| Fun-ASR Realtime | 实时语音转文字 | 声纹识别或说话人分离 |
| Qwen3-ASR Flash Realtime | 实时语音转文字，附带语言与情感标签 | 主人身份识别 |
| Qwen-Audio-TTS | 独立文本转语音系列，可做复刻与指令控制 | 端到端语音对话模型 |
| Qwen-Audio-Realtime | 端到端实时语音对话系列 | Qwen-Audio-TTS 的 WebSocket 名称 |
| CosyVoice | 独立文本转语音系列，可做系统音色、复刻/设计和部分指令控制 | ASR 或声纹模型 |
| Qwen3-TTS Realtime | 新版 Qwen3-TTS 的 WebSocket 系列，按能力拆成 flash/instruct/vc/vd | 一个同时具备复刻、指令和时间戳的全能模型 |
| Qwen-TTS Realtime | 旧版 `qwen-tts-realtime*`，官方归为旧版、按 Token 计费 | 新版 Qwen3-TTS Realtime |

官方模型总览：[语音识别](https://help.aliyun.com/zh/model-studio/asr-model)、[语音合成](https://help.aliyun.com/zh/model-studio/tts-model)（访问日期：2026-07-18）。

## 3. Fun-ASR Realtime 与 Qwen3-ASR Realtime

| 维度 | Fun-ASR Realtime | Qwen3-ASR Flash Realtime | 对 Memoria 的意义 |
|---|---|---|---|
| 协议 | WebSocket `api-ws/v1/inference`；控制事件为 JSON，音频为二进制流 | WebSocket `api-ws/v1/realtime?model=...`；音频以 Base64 放在 JSON 事件中 | 两套协议不能只换模型名，必须换适配器 |
| 音频 | PCM/WAV/MP3/Opus/Speex/AAC/AMR；除 8k 专用模型外可传任意采样率 | 官方推荐 PCM/Opus；8k/16k 单声道，8k 会升采样 | 当前 16k 单声道均可接入 |
| 实时文本 | 中间结果与最终结果 | 已确认稳定前缀 `text` + 可变草稿 `stash` + final transcript | Qwen 的前缀模型适合字幕，但不等于字时间戳 |
| 时间戳 | 句级、字级时间戳 | 只有 VAD 的 `audio_start_ms` / `audio_end_ms`；公开转写事件无字词时间戳 | 当前实际已听文本依赖字级对齐，Fun-ASR 更合适 |
| 热词 | 支持 `vocabulary_id` | 公开协议未提供 | 人名、地名、家族称谓、行业术语应保留 Fun-ASR |
| 对话上下文 | `fun-asr-realtime` 与 `2025-11-07` 支持；最多各 5 条上下文消息，每轮最多 400 字符，可用 `continue-task` 更新 | 公开协议未提供 Prompt/context | 终身记忆中的专名可用于转写纠错，但不是靠 Qwen-ASR Realtime 完成 |
| 断句 | VAD/语义断句；VAD 静音阈值 200–6000ms，默认 1300ms；支持噪声阈值和 heartbeat | server VAD 或 manual commit；静音阈值 200–6000ms，默认 800ms，官方示例推荐 400ms | 两者都要用真实设备校准，不能照搬默认值 |
| 情感 | 不支持实时情感标签 | 返回 `neutral/happy/sad/angry/fearful/disgusted/surprised` | Qwen 适合做非阻塞情感观察量 |
| 说话人分离 | 不支持 | 不支持 | 官方明确仅非实时 `fun-asr` / `fun-asr-mtl` 支持 diarization |
| 声纹/主人验证 | 不支持 | 不支持 | 必须由独立 speaker verification 模块完成 |
| 会话结束 | `run-task → continue-task → finish-task`，支持 heartbeat | `session.finish` 后收到 `session.finished`，客户端需主动断开 | 当前长会话主链继续用 Fun-ASR 更顺手 |

依据：[Fun-ASR 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)、[Fun-ASR WebSocket API](https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api)、[Qwen-ASR 交互流程](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process)、[Qwen-ASR 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-server-events)（访问日期：2026-07-18）。

### ASR 建议

推荐输入路由：

```text
AEC 后同一份 16k PCM
    ├── Fun-ASR Realtime：主转写、热词、时间戳、话轮提交
    ├── Qwen3-ASR Realtime：非阻塞情感旁路
    └── Speaker Verification：主人/其他人判定
```

Qwen 情感只应影响回复语气、长度和 TTS 表达，不应直接改变权限、工具参数、记忆事实或主人身份判定。情感事件没有官方 confidence，应做多帧平滑、有效期和 neutral 回退。

## 4. Qwen-Audio-TTS / CosyVoice 与 Qwen3-TTS Realtime

### 4.1 协议与能力差异

| 维度 | Qwen-Audio-TTS / CosyVoice | Qwen3-TTS Realtime | 旧 Qwen-TTS Realtime |
|---|---|---|---|
| 模型与接口关系 | 同一模型名同时支持 WebSocket 与 HTTP | 带 `-realtime` 的模型走 WebSocket；不带后缀走 HTTP | `qwen-tts-realtime*`，官方列为旧版 |
| WebSocket 流程 | `run-task → continue-task → finish-task` | `session.update`，支持 `server_commit` / `commit` | Realtime 事件协议 |
| 文本输入 | 可多次 `continue-task`，文本流式输入、音频流式输出 | 可流式追加文本，再由服务端或客户端提交 | 可流式输入，但能力较旧 |
| 音频返回 | 二进制帧；PCM/WAV/MP3/Opus；8/16/22.05/24/44.1/48kHz | Qwen3 支持 PCM/WAV/MP3/Opus 与 8/16/24/48kHz | 仅 PCM 24kHz |
| 参数控制 | 音量、语速、音调、Opus 码率 | 依模型/协议提供 | 不支持语速、音量、音调和码率控制 |
| 连接 | 官方建议复用一个 WebSocket 处理多个新 `task_id` | 会话式连接 | 会话式连接 |
| 字级时间戳 | 可选；CosyVoice v3.5/v3/v2 复刻音色支持，部分系统音色支持；Qwen-Audio-TTS 复刻音色不支持 | 公开服务端事件没有字/词时间戳事件 | 公开服务端事件没有字/词时间戳事件 |
| 指令控制 | Qwen-Audio-TTS、CosyVoice v3.5、CosyVoice v3-flash 支持 | 仅 `qwen3-tts-instruct-flash(-realtime)` | 不支持 |
| 声音复刻 | Qwen-Audio-TTS、CosyVoice 支持 | 仅 `qwen3-tts-vc-*` | 不支持 |
| 声音设计 | CosyVoice v3.5/v3 支持 | 仅 `qwen3-tts-vd-*` | 不支持 |
| 复刻 + 指令 | Qwen-Audio-TTS 与 CosyVoice v3.5 可同时具备 | 不可在同一模型同时具备：VC 与 Instruct 是两条模型线 | 不支持 |

依据：[语音合成模型总览](https://help.aliyun.com/zh/model-studio/tts-model)、[Qwen-Audio-TTS/CosyVoice 客户端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-client-events)、[WebSocket 交互流程](https://help.aliyun.com/zh/model-studio/cosyvoice-websocket-api)、[Qwen-TTS 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-client-events)、[Qwen-TTS 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-server-events)（访问日期：2026-07-18）。

### 4.2 新 Qwen3-TTS 的能力是分开的

| 目标 | 模型族 | 声音复刻 | 指令控制 | 公开字词时间戳 |
|---|---|---:|---:|---:|
| 系统音色实时合成 | `qwen3-tts-flash-realtime` | 否 | 否 | 未提供 |
| 系统音色 + 指令 | `qwen3-tts-instruct-flash-realtime` | 否 | 是 | 未提供 |
| 声音复刻 | `qwen3-tts-vc-realtime-*` | 是 | 否 | 未提供 |
| 声音设计 | `qwen3-tts-vd-realtime-*` | 否 | 否 | 未提供 |

因此，对“既像本人，又按当前情境调整语气，还必须精确知道用户听到哪里”的 Memoria 而言，Qwen3-TTS VC 目前不是无损替代。

### 4.3 当前项目最合适的 TTS 路线

当前配置是 `cosyvoice-v3-flash + longanyang + 24kHz + word timestamps`。官方音色表确认 `longanyang` 支持 Instruct 和时间戳。这套组合的价值不只是能发声，而是能给 GenerationFence 和 HeardTextTracker 提供对齐信号。

建议按以下顺序 A/B：

1. **基线：** 保留当前 `cosyvoice-v3-flash + longanyang`。
2. **人格音色主候选：** 使用本人授权的 10–20 秒清晰录音创建 CosyVoice v3.5 flash/plus 复刻音色，保留流式、指令与字级时间戳。
3. **自然度候选：** Qwen-Audio 3.0 TTS flash/plus 复刻音色；接受其没有字时间戳的代价，只进入隔离 A/B。
4. **低延迟候选：** Qwen3-TTS-VC Realtime；只有在取消延迟、主观相似度和可替代的播放对齐方案均通过后再考虑进入主链。

声音复刻官方要求：Qwen-Audio-TTS/CosyVoice 推荐 10–20 秒、最长 60 秒、至少 16kHz；Qwen-TTS 至少 24kHz 单声道并含至少 3 秒连续清晰讲话。参见[声音复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)（访问日期：2026-07-18）。

## 5. 面向三个产品壁垒的评估

### 5.1 终身记忆与人生知识库

语音厂商只解决“听清”和“说出”，不会自动形成可靠的终身记忆。目前仓库已有消息、每日总结和 profile 的 SQLite 持久化，但与目标仍有距离：

- 缺少原始事件、可修订事实、人生事件、关系、价值观、偏好、经验问答等不同语义层；
- 缺少来源、时间、说话人、置信度、版本、冲突和撤销状态；
- 缺少“用户说过”与“系统推断”的边界；
- 缺少跨年检索、合并去重和事实纠错机制。

推荐至少分成五层，不要把它们都塞进 chat history：

```text
不可变会话事件/转写
    → 规范化话轮与实际已听助手文本
    → 人生事实/事件/关系/经验候选
    → 用户确认、冲突处理、版本化
    → 检索视图与人格上下文
```

永久保存也不应理解为“不可删除”。产品上应提供导出、纠错、撤销、删除、保留期限和加密；生物特征模板与普通聊天内容必须分库/分权。

### 5.2 人格复刻

人格复刻不是一次声音克隆，而是四层能力：

1. **音色层：** 声音复刻解决 timbre 相似度。
2. **韵律层：** 语速、停顿、句长、重音、情绪、口头禅，需要长期统计与可控 TTS。
3. **表达策略层：** 直接/委婉、理性/细腻、回答结构和措辞习惯。
4. **心智模型层：** 价值观、决策习惯、关系边界、人生经历和不确定性。

建议把长期学习结果存成**有证据、可版本化、可撤销的 PersonaProfile**，每条特征带来源话轮、置信度和适用场景；不能把模型的一次归纳直接写成永久人格事实。声音模型只承担第 1 层和部分第 2 层，无法单独形成“私人 AI 心智体”。

### 5.3 主人与其他说话人识别

当前 `services/agent/src/orchestration/speaker_verify.py` 文件开头已明确：它是依赖 numpy 的轻量 log-mel mean/std embedding，**不是商用声纹产品**。它适合临时减少同设备附近讲话者干扰，不足以支撑身份安全承诺。

应拆成两类任务：

- **主人 vs 其他人：** target-speaker verification，输出 `owner / non_owner / uncertain`，最符合当前需求。
- **多人“谁说了什么”：** diarization + speaker attribution，是更重的离线或准实时任务，不应默认压在实时主链上。

可先评估 ModelScope 官方 [3D-Speaker](https://github.com/modelscope/3D-Speaker) 的 CAM++、ERes2NetV2 等 speaker embedding/verification 模型。生产方案还需要：

- 多时段、多设备、多距离登记，不只录一条样本；
- 按手机/耳机/扬声器模式分别校准 FAR、FRR 和 uncertain 区间；
- 短话、重叠说话、远场、回声、感冒变声时 fail-uncertain，不冒充“确定是主人”；
- 主人授权、活体/重放攻击防护、模板加密、密钥隔离、撤销与删除；
- 身份只控制记忆归属和敏感动作权限，不阻止访客进行普通对话。

## 6. 推荐目标架构

```text
客户端 AEC / NS / AGC
        │
        ▼
LiveKit + 单份 PCM 输入路由
        ├── Fun-ASR 主转写 ───────────────┐
        ├── Qwen-ASR 情感侧车 ───────────┤
        └── Speaker Verification ────────┤
                                         ▼
                           UtteranceRouter / Turn State
                           GenerationFence / 权限策略
                                         │
               ┌─────────────────────────┴─────────────────────┐
               ▼                                               ▼
       Memory/Event Pipeline                             文本 LLM + Persona
               │                                               │
       结构化、版本化、可撤销                           SpeechPlan / DeliveryPlan
                                                               │
                                                               ▼
                                              CosyVoice 复刻音色主链
                                              + Qwen 候选隔离 A/B
```

端到端 Qwen-Audio/Omni 可以继续作为体验 A/B，但其转写、用户实际听到的文本、工具结果、记忆入库都要通过独立适配层重新建立可信事件，不能把模型字幕直接视为事实。

## 7. 分阶段建议与验收指标

### P0：不换主链，建立可比较基线

- 固定当前 Fun-ASR + CosyVoice 版本和地域；
- 收集 200 条授权中文真实录音，覆盖方言、家族称谓、背景电视、重叠讲话；
- 记录 ASR 字错率/专名错率、EOT 延迟、首声延迟、真打断停止时间、误打断率、实际已听文本偏差；
- 真实 iPhone/Android/耳机/扬声器听感 A/B，而不是只看接口成功率。

### P1：声音与声纹两条 A/B

- CosyVoice v3.5 flash/plus 复刻音色 vs Qwen-Audio-TTS vs Qwen3-TTS-VC；
- 盲测音色相似度、自然度、长句稳定性、情绪指令遵循、首包时间和取消尾音；
- 正式 speaker embedding 以 `owner/non_owner/uncertain` 旁路运行，先只打点，不立即阻断话轮。

### P2：记忆与人格沉淀

- 建立 event → fact candidate → confirmed memory → projection 流程；
- 给每条人格特征和人生事实添加来源、置信度、版本、冲突和删除状态；
- 非主人发言默认进入访客会话，不写入主人长期人格；需要归档时明确确认。

### P3：是否迁移端到端模型，以数据决定

只有端到端路线同时满足以下条件，才考虑扩大流量：

- 真设备主观自然度显著优于级联；
- P95 首声、打断停止和回声指标不退化；
- 能重建实际已听文本与可审计记忆事件；
- 工具调用和旧 response 取消仍有 generation/epoch 隔离；
- 每分钟成本、并发和供应商故障降级可接受。

## 8. 价格与限流快照

以下为北京地域公开价/默认限流快照，仅用于量级判断：

| 模型 | 价格 | 默认限流 |
|---|---:|---:|
| Fun-ASR Realtime | 0.00033 元/秒 | 20 RPS |
| Qwen3-ASR Flash Realtime | 0.00033 元/秒 | 20 RPS |
| Qwen-Audio 3.0 TTS flash | 1 元/万字符 | 3 RPS |
| Qwen-Audio 3.0 TTS plus | 1.4 元/万字符 | 3 RPS |
| CosyVoice v3.5 flash | 0.8 元/万字符 | 3 RPS |
| CosyVoice v3.5 plus | 1.5 元/万字符 | 3 RPS |
| CosyVoice v3 flash | 1 元/万字符 | 3 RPS |
| Qwen3-TTS Realtime 主流版本 | 约 1 元/万字符 | 180 RPM |
| 旧 Qwen-TTS Realtime | 输入 2.4 元/百万 Token，输出 12 元/百万 Token | 10 RPM、100k TPM |

RPS/RPM 是调用限流，不等于官方承诺的同时活跃 WebSocket 会话数；公开文档未给出后者，容量规划必须压测。依据：[模型计费](https://help.aliyun.com/zh/model-studio/model-pricing)、[限流](https://help.aliyun.com/zh/model-studio/rate-limit)（访问日期：2026-07-18）。

## 9. 当前 Memoria 与目标的真实差距

### 9.1 线上与实验链路

2026-07-18 实时读取公网 `/memoria-api/health/ready` 得到
`release_tag=20260718-211231`，门禁覆盖 `LiveKit + Fun-ASR + Qwen LLM + CosyVoice`。
公网 H5 已出现 Qwen-Audio 端到端选项，但当前 readiness 没有检查 Qwen-Audio 的连接、首声、取消、字幕或记忆落库，
所以它仍应视为实验 A/B，不能与级联主链做同等级的生产承诺。

### 9.2 记忆还只是“聊天留痕”，不是人生知识库

当前数据库只有 `messages`、`daily_summaries`、基础 `profiles` 和会话记录；每日总结只有标题、概述、亮点、心情和建议。
Agent 的运行时上下文最多保留 16 条消息，当前没有把跨会话历史、每日总结或结构化人生事实检索回实时 Agent。
此外，H5 只持久化 final 用户文本和 `heard=true` 的助手文本；Qwen-Audio/Omni 当前把助手字幕标为
`heard=false`，因此端到端链路的助手回答不会形成完整对话档案。

要达到产品目标，需要新增：

1. 不可变事件账本：原始/最终转写、说话人、打断点、实际播放区间、设备和 consent 版本；
2. 可修订权威转写：异步档案级 ASR、用户纠错、版本与音频证据；
3. Memory Compiler：抽取人物、关系、事件、观点、经验、价值判断，每条带来源、时间和置信度；
4. 记忆投影：近期记忆、情节记忆、稳定事实、经验问答和人格特征分层检索；
5. 可靠性：加密、导出、删除、迁移、3-2-1 备份和定期恢复演练。

“永久不会丢失”不是单个数据库能保证的承诺，更准确的产品表述应是：长期可靠保存，直到用户删除或改变保存策略。
当前入库前统一 PII 脱敏也会丢失部分真实人生信息；不应简单取消脱敏，而应增加用户明确授权的加密敏感记忆域，
与普通对话、声纹模板分库分权。

### 9.3 人格还没有长期学习闭环

当前有静态语音 Prompt 和 `direct / deliberative / light_laughter / supportive` 四类当轮 DeliveryPlan，
但它们不持久化；代码中的 `ProsodyController` 也没有接入生产路径。项目尚未持续提取并学习口头禅、句长、停顿、
价值观或决策习惯。现有 `*_VOICE_CLONE_ID` 只是配置入口，不等于已经具备音色登记、同意、版本、撤销和效果验收流程。

建议把“人格复刻”拆成四个独立、可版本化资产：

- `VoiceProfile`：音色、口音、语速、停顿；
- `DiscourseStyle`：口头禅、句长、措辞和表达结构；
- `AutobiographicalMemory`：可追溯的人生故事和事实；
- `ValuesDecisionModel`：价值排序、决策习惯、例外条件及随时间的变化。

一次情绪化表达不能直接晋升为长期人格事实；冲突内容不能静默覆盖。每次更新都应保留证据、置信度、版本，并允许用户查看、纠正和回滚。

### 9.4 当前声纹门不满足“识别其他人”

当前 Python 与浏览器实现都是会话内登记的轻量频谱 embedding：登记超时会 fail-open，浏览器在判定 mismatch 后会直接静音。
它只能尝试“放行主人、挡掉附近声音”，既不能输出具体身份，也无法让访客继续普通对话；rolling window 还可能混入上一位说话人或重叠语音。

建议把输入控制面升级为三态，而不是二元静音门：

```text
当前话轮音频
  → overlap / diarization
  → owner verification（1:1）
  → known-speaker identification（可选 1:N）
  → replay / synthetic-voice 检测 + 设备身份
  → owner | guest | uncertain
```

- `owner`：允许检索、写入私人记忆；
- `guest`：进入隔离访客会话，不读取主人隐私，默认不永久写入主人档案；
- `uncertain`：可普通对话，私密检索或敏感动作要求 Passkey、设备解锁或主人确认。

声纹是概率信号，不应成为导出全部记忆、删除数据、支付等敏感操作的唯一认证因素。

## 10. 接近 GPT Live 的方式：双车道，而不是单模型替换

OpenAI Realtime 的参考能力包括原生 speech-to-speech、连续 WebRTC、server/semantic VAD、barge-in 后的响应取消与
未播放内容截断、工具调用和会话事件。严格说，它是“可随时打断的感知全双工”，不是双方重叠说话时仍能无损并行理解、
并行生成的语义全双工。官方也明确：原生音频模型的输入转写由独立 ASR 异步完成，可能与模型实际理解不同，只能作为 rough guide。

因此 Memoria 更适合保留两条共享 ID 与控制面的车道：

```text
对话快车道：级联或端到端 S2S → 低延迟、自然轮替、情绪与声音
                         │
                         └── turn_id + generation_id + speaker_state + actual_heard_ms
                         │
身份/档案慢车道：档案级 ASR → 说话人归属 → 事实/人格编译 → 版本化记忆
```

级联继续承担可控字幕、工具、GenerationFence 和可审计主线；Qwen-Audio/Omni 负责验证更自然的端到端体验。
端到端链路只有在补齐实际已听账本、说话人归属、完整消息持久化、工具 epoch 隔离和 provider readiness 后，
才适合扩大流量。

OpenAI 一手资料：[Voice agents](https://developers.openai.com/api/docs/guides/voice-agents)、
[Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad)、
[Interruption and truncation](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation)、
[Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)、
[Speaker diarization](https://developers.openai.com/api/docs/guides/speech-to-text#speaker-diarization)（访问日期：2026-07-18）。

## 11. 最终推荐

短期生产组合：

```text
Fun-ASR Realtime 主链
+ Qwen3-ASR Realtime 情感旁路
+ 正式 Speaker Verification 旁路
+ CosyVoice v3.5 复刻音色候选
+ 现有 GenerationFence / HeardTextTracker / UtteranceRouter
+ 版本化记忆与 PersonaProfile
```

这条路线最符合 Memoria 的差异化目标：**语音体验可以逐步接近 GPT Live，但记忆、人格和身份仍掌握在自己的控制面与数据层中**。供应商模型应是可替换的感知/表达组件，终身记忆、人格演化和身份治理才是项目真正不可替代的壁垒。
