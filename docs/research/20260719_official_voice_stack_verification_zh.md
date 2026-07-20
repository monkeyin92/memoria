# Memoria 实时语音技术栈官方资料核验与选型建议

> 核验日期：2026-07-19  
> 证据边界：阿里云百炼官方文档、OpenAI 官方开发文档、Qwen/FunAudioLLM/ModelScope 官方仓库，以及 Memoria 当前代码和 `HANDOFF.md`。  
> 本文区分“云产品公开契约”“开源亲缘项目能力”“Memoria 当前实装”。厂商未公开的数据不作推断；开源仓库的延迟或许可证不自动等同于同名/近似名云产品。

## 1. 结论先行

Memoria 当前不应把级联主链整体替换成某个带 `Realtime` 名称的 ASR 或 TTS。最符合项目目标的近期组合是：

```text
Fun-ASR Realtime 主转写
+ Qwen3-ASR Flash Realtime 非阻塞情感侧车
+ 正式 Speaker Verification 三态旁路
+ Qwen LLM + 版本化记忆/PersonaCapsule
+ CosyVoice v3.5 复刻音色主候选
+ 现有 UtteranceRouter / GenerationFence / HeardTextTracker
```

核心判断：

1. **主 ASR 继续 Fun-ASR。** 热词、上下文和字级时间戳对专名、档案转写、字幕与实际已听文本更重要；Qwen3-ASR 的优势是稳定前缀和七类情感，不足以无损替换主转写。
2. **Qwen3-ASR 继续做侧车。** 它没有公开热词、上下文 Prompt、字词时间戳、说话人分离或声纹接口；情感也没有官方 confidence。
3. **人格声音优先评估 CosyVoice v3.5“复刻音色”，不是当前“设计音色”。** 当前生产的 `warm_companion` 是按描述设计出的声音，不代表像用户本人。
4. **Qwen-Audio-TTS 是自然度/相似度 A/B 候选。** 它支持复刻和指令，但复刻音色不支持字级时间戳，会削弱 Memoria 的实际已听对齐。
5. **新版 Qwen3-TTS Realtime 不是一个全能模型。** 系统音色、指令、复刻、设计分成 Flash/Instruct/VC/VD 四条模型线；VC 不能同时获得 Instruct，公开事件也没有字词时间戳。
6. **旧 `Qwen-TTS-Realtime` 不应新选。** 官方已标为旧版，能力和参数控制明显弱于新系列。
7. **声纹不是 ASR/TTS 附属能力。** 当前 Memoria 的 numpy log-mel 启发式只能做临时会话门禁，不能支撑“这是主人”的产品或安全承诺。
8. **接近 GPT Live 的主要差距在控制面，不是换模型名。** AEC、双讲、语义话轮、取消、迟到音频隔离、已听文本、工具 epoch 和说话人归属仍需 Memoria 自己负责。

## 2. 名称校准

| 用户常用名称 | 当前准确定位 | 备注 |
|---|---|---|
| Fun-ASR | 云端实时模型 `fun-asr-realtime` | 不等于开源 FunASR 工具包或 Fun-ASR-Nano |
| qwen-asr-realtime | 当前云模型 `qwen3-asr-flash-realtime` | ASR，不是声纹模型 |
| Qwen-Audio-TTS | `qwen-audio-3.0-tts-plus` / `qwen-audio-3.0-tts-flash` | 独立 TTS 系列 |
| Qwen-Audio-Realtime | 端到端实时语音对话模型 | 不是 Qwen-Audio-TTS 的 WebSocket 名称 |
| CosyVoice | 百炼 TTS 系列 | 云 v3.5 不等于公开仓库的 Fun-CosyVoice 3.0 |
| Qwen-TTS-Realtime | 旧 `qwen-tts-realtime*` | 官方列为旧版、按 Token 计费 |
| Qwen3-TTS Realtime | 新版 `qwen3-tts-*-realtime` | Flash/Instruct/VC/VD 能力分线 |

## 3. Fun-ASR Realtime 与 Qwen3-ASR Flash Realtime

| 维度 | Fun-ASR Realtime | Qwen3-ASR Flash Realtime | 对 Memoria 的判断 |
|---|---|---|---|
| WebSocket 协议 | 控制 JSON + 二进制音频；`run-task → continue-task → finish-task` | URL 指定模型；Base64 音频放 JSON 事件 | 适配器不可只换模型名 |
| 实时结果 | 中间/最终结果 | 稳定前缀 `text` + 可修正 `stash` + final | Qwen 字幕草稿体验更好，但不是时间戳 |
| 字词时间戳 | 默认句级、字级毫秒时间戳 | 转写事件无字词时间戳，仅 VAD 起止毫秒 | Fun-ASR 更适合 actual-heard 与档案证据 |
| 热词 | 支持 `vocabulary_id` | 公开协议未提供 | 人名、地名、家族称谓、行业术语选 Fun-ASR |
| 对话上下文 | 稳定版及 `2025-11-07` 支持；每类最多 5 条、每轮合计 400 字符 | 公开协议未提供 Prompt/context | 可把已确认专名送 Fun-ASR，但不能塞完整人生知识库 |
| VAD | 默认静音 1300ms，可调 200–6000ms；可调噪声阈值 | VAD 或 manual commit；默认静音 800ms，文档示例推荐 400ms | 均须真实设备按 P95/P99 调参 |
| 情感 | 实时接口不提供 | 固定七类：neutral/happy/sad/angry/fearful/disgusted/surprised | Qwen 适合非阻塞观察量 |
| 情感置信度 | 不适用 | 官方事件不返回 confidence | 不得直接驱动权限或永久人格 |
| 说话人分离 | 实时接口不支持 | 不支持 | 非实时 Fun-ASR 的 diarization 不能外推到实时模型 |
| 声纹/主人验证 | 不支持 | 不支持 | 必须独立 Speaker Verification |
| 音频 | 多格式；Memoria 当前 16k PCM 可用 | PCM/Opus，8k/16k；8k 会升采样 | 当前输入无需因此改造 |
| 地域 | 北京、新加坡；Key/Workspace/域名按地域隔离 | 北京、新加坡；同样按地域隔离 | 会话内不要跨地域混用 |

版本快照：官方模型总览在核验日显示，Fun-ASR 北京稳定版对应 `2025-11-07`、最新快照 `2026-02-28`；Qwen3-ASR 稳定版对应 `2025-10-27`、最新快照 `2026-02-10`。上线应显式固定版本或接受稳定别名漂移，并在变更后重跑中文专名和端点测试。

### ASR 最终选择

```text
同一份 AEC 后 16k PCM
  ├─ Fun-ASR：权威实时转写、时间戳、热词、话轮提交
  ├─ Qwen3-ASR：当前话轮情感侧车，掉线不阻塞主链
  └─ Speaker Verification：owner / guest / uncertain
```

Qwen 情感只可影响本轮回复长度、措辞和 TTS 交付方式；不得改变事实、权限、工具参数、主人身份或直接写成长期人格。

## 4. TTS 系列差异

### 4.1 总表

| 维度 | Qwen-Audio-TTS | CosyVoice | 新 Qwen3-TTS Realtime | 旧 Qwen-TTS Realtime |
|---|---|---|---|---|
| 定位 | 高自然度独立 TTS | 可控、复刻/设计型独立 TTS | 新一代低延迟流式 TTS | 旧版实时 TTS |
| WebSocket | 与 CosyVoice 共用 `run/continue/finish-task` 协议 | 同左 | `session.update` + server/client commit | 旧 Realtime 事件协议 |
| 文本流入/音频流出 | 支持/支持 | 支持/支持 | 支持/支持 | 支持/支持 |
| 音频输出 | PCM/WAV/MP3/Opus，8k–48k | PCM/WAV/MP3/Opus，8k–48k | PCM/WAV/MP3/Opus，8/16/24/48k | 仅 PCM 24k |
| 系统音色 | 支持 | 依版本；v3.5 plus/flash 无系统音色 | Flash/Instruct 线 | 支持 |
| 声音复刻 | 支持 | 支持 | 仅 VC 线 | 不支持 |
| 声音设计 | 不支持 | v3.5/v3 支持 | 仅 VD 线 | 不支持 |
| 自然语言指令 | 支持 | v3.5、v3-flash 支持 | 仅 Instruct 线 | 不支持 |
| 复刻 + 指令同模型 | 支持 | v3.5 支持 | 不支持，VC 与 Instruct 分线 | 不支持 |
| 字级时间戳 | 系统音色可依表支持；**复刻音色不支持** | 官方明确覆盖 v3.5/v3/v2 复刻音色及标明支持的系统音色 | 公开事件未提供 | 公开事件未提供 |
| 生成中取消 | 无公开 `response.cancel` | 无公开 `response.cancel` | 只能清未提交文本缓冲；无公开生成中 `response.cancel` | 无 GPT Realtime 等价契约 |
| 参数控制 | 音量、语速、音调、Opus 码率 | 同左 | 依模型/协议 | 不支持语速、音量、音调、码率 |

### 4.2 CosyVoice 版本边界

| 版本 | 系统音色 | 复刻 | 设计 | 指令 |
|---|---:|---:|---:|---:|
| `cosyvoice-v3.5-plus/flash` | 否 | 是 | 是 | 是 |
| `cosyvoice-v3-plus` | 否 | 是 | 是 | 否 |
| `cosyvoice-v3-flash` | 是 | 是 | 是 | 是 |

重要限制：官方字时间戳说明明确覆盖 v3.5/v3/v2 的**复刻音色**及指定系统音色，但没有把 v3.5 的**设计音色**列入公开支持范围。Memoria 当前生产是：

```text
cosyvoice-v3.5-flash
+ warm_companion 设计音色
+ word_timestamp_enabled=true
```

因此当前“设计音色能返回时间戳”只能算实测能力，不能当成稳定公开契约。由于适配器把空时间戳视为失败，这一项必须进入 provider readiness 硬门禁；若实网连续缺失，应自动回退到已验证的复刻音色或支持时间戳的系统音色，而不是伪造字级对齐。

### 4.3 Qwen3-TTS Realtime 的能力分线

| 目标 | 模型线 | 复刻 | 指令 | 公开字词时间戳 |
|---|---:|---:|---:|---:|
| 系统音色实时合成 | Flash | 否 | 否 | 无 |
| 系统音色 + 指令 | Instruct | 否 | 是 | 无 |
| 声音复刻 | VC | 是 | 否 | 无 |
| 声音设计 | VD | 否 | 否 | 无 |

开源 Qwen3-TTS README 声称端到端合成最低可到 97ms，但这是开源模型及其测试口径，不是百炼云 API 的 P50/P95 SLA。README 还说明 vLLM-Omni 当前只支持离线推理，在线 serving 尚待支持。因此不能据此承诺云端或自部署生产首包延迟。

### 4.4 面向 Memoria 的 TTS 排序

1. **首选主候选：CosyVoice v3.5 复刻音色。** 同时需要本人音色、流式、指令和官方字时间戳契约，综合最匹配。
2. **保留当前设计音色作体验基线。** 它解决“好听/合适”，不解决“像本人”；时间戳需 readiness 实测兜底。
3. **Qwen-Audio 3.0 TTS 作自然度/相似度 A/B。** 接受复刻音色无字时间戳的代价，先不进主链。
4. **Qwen3-TTS VC 作低延迟实验。** 只有补齐取消、已听文本替代对齐且实测显著获益后再考虑扩大流量。
5. **旧 Qwen-TTS Realtime 不新建集成。** 能力不足且已被官方归入旧版。

所有供应商 TTS 在真实打断时都需要 Memoria 继续执行：本地立即停播、关闭/丢弃活动连接、GenerationFence 拦截迟到 PCM、只持久化实际已听内容。仅发送 `finish` 或清文本缓冲不能替代取消。

## 5. 地域、价格、限流与部署边界

### 5.1 公开价格与默认限流快照（北京）

| 模型 | 价格 | 默认限流 |
|---|---:|---:|
| Fun-ASR Realtime | 0.00033 元/秒 | 20 RPS |
| Qwen3-ASR Realtime | 0.00033 元/秒 | 20 RPS |
| Qwen-Audio 3.0 TTS flash / plus | 1 / 1.4 元每万字符 | 3 RPS |
| CosyVoice v3.5 flash / plus | 0.8 / 1.5 元每万字符 | 3 RPS |
| Qwen3-TTS Realtime 主流版本 | 约 1 元每万字符 | 180 RPM |
| 旧 Qwen-TTS Realtime | 输入 2.4、输出 12 元每百万 Token | 10 RPM、100k TPM |

RPS/RPM 是请求速率限制，**不是**公开承诺的同时活跃 WebSocket 数。官方没有公布上述 ASR/TTS 的活跃连接并发上限，也没有公布可用于采购承诺的首包/端到端 P50、P95 SLA。容量和延迟必须以 Memoria 的同地域实压为准，并按实际并发申请提额。

地域方面，Fun-ASR、Qwen3-ASR 和 Qwen-Audio-TTS 已确认北京/新加坡；CosyVoice v3.5 当前仅北京。跨地域的 API Key、Workspace 和域名不能混用。

### 5.2 云产品与开源项目不能画等号

- FunASR 工具包源码为 MIT，但模型权重各自有许可证；它不等于云 `fun-asr-realtime`。
- CosyVoice 官方仓库为 Apache-2.0，公开的是 Fun-CosyVoice 3.0；不能假设能自托管云 v3.5 同权重。
- Qwen3-ASR、Qwen3-TTS 官方仓库为 Apache-2.0，可本地评估；不能假设与云 Flash 完全同权重、同延迟、同服务契约。
- Qwen3-ASR 开源流式模式明确不返回时间戳；时间戳需独立 Qwen3-ForcedAligner。
- 未确认到可自部署的同名 `Qwen-Audio-TTS` 或旧 `Qwen-TTS-Realtime` 开放权重。

## 6. 隐私与“终身保存”边界

阿里云官方数据安全说明称客户数据不会用于模型训练，并说明 AES-256 加密；同时服务仍会依法、按提供服务所需时间处理/存储调用数据，公开页面没有给出适用于所有语音接口的具体保留天数。声音复刻音色数量上限为 1000 个，一年未使用会自动删除；原始复刻样本的具体云端保留期限未公开。

因此：

- 云 ASR/TTS 只能是处理器，不能是 Memoria 的永久档案库。
- “永久不会丢失”不能作为绝对工程承诺。更准确的产品表述是：**在用户未删除或改变保存策略前，提供加密、多副本、版本化、可导出和经恢复演练的长期可靠保存。**
- 聊天/音频、声纹模板、声音复刻样本必须分域授权、分密钥、可撤销；供应商删除也要进入本地删除工作流和审计。

## 7. 与 GPT Live 类体验的真实差距

OpenAI 官方把两类架构明确区分为：

- 原生 speech-to-speech：模型直接处理实时音频输入输出，目标是自然、低延迟；
- chained pipeline：应用显式控制转写、文本推理和语音输出，适合可预测工作流。

Realtime API 在 VAD 打断时可取消正在生成的响应；WebRTC/SIP 服务端知道播放进度并自动截断未播放音频，WebSocket 则要求客户端停播并发送 `conversation.item.truncate`。Fun-ASR、Qwen-ASR、Qwen-Audio-TTS、CosyVoice 和 Qwen-TTS 都只是单向组件，组合后能做“可打断级联”，但不会自动获得上述原生会话语义。

Memoria 已有的优势是 `UtteranceRouter`、`GenerationFence`、`HeardTextTracker`、播放期输入守卫和工具 epoch 隔离；距离 GPT Live 体验仍主要有：

1. 客户端/设备级 AEC、NS、AGC 与真正双讲稳定性；
2. EOU 低延迟与不抢话之间的动态平衡；
3. TTS 无原生 cancel 时的本地停止和迟到音频隔离；
4. 设计音色时间戳契约不稳导致的 actual-heard 风险；
5. 访客/不确定说话人的话轮归属与权限还未进入统一控制面；
6. 端到端 S2S 若重新 A/B，仍需重建可审计文本、实际已听、工具和记忆事件。

所以推荐保留“双车道”，而不是押注单模型替换：

```text
实时快车道：级联（未来可并行 S2S A/B）
  └─ 首声、轮替、打断、工具、播放

身份/档案慢车道：档案级 ASR + 说话人归属 + MemoryCompiler
  └─ 纠错、人物、故事、经验、人格、版本与审计
```

## 8. 对三项产品壁垒的评估

### 8.1 终身记忆与人生知识库

**当前成熟度：约 2/5。** 当前已有 SQLite 消息、每日总结、基础 Profile、PII 脱敏和实际已听消息语义，但仍是聊天留痕，不是人生知识库；P0 架构已写完，P1–P6 尚未实施。

必须建立：

```text
追加式证据账本
  → 权威转写版本与说话人归属
  → 人物/关系/时间线/故事/经验候选
  → 用户确认、冲突、撤销
  → 可重建检索投影与 PersonaCapsule
```

每条事实需包含来源话轮、实际说话人、发生时间/记录时间、置信度、抽取器版本、状态与冲突。必须区分“用户明确说过”“系统推断”“助手生成”；访客默认不能写入主人长期档案。

### 8.2 人格复刻与本人声音

**当前成熟度：表达 2/5，心智 1/5，本人音色 0–1/5。** 当前有静态 Prompt、当轮 DeliveryPlan 和 v3.5 设计音色，但没有持续学习闭环；设计音色也不是本人复刻。

人格至少分四层：

1. `VoiceProfile`：音色、口音、声音模型与授权版本；
2. `SpeechStyle`：语速、停顿、节奏、口头禅、句长和重音；
3. `DiscourseStyle`：直接/委婉、叙事结构、用词和表达策略；
4. `ValuesDecisionModel`：价值排序、决策习惯、适用情境、反例和随时间变化。

声音克隆只解决第 1 层和部分第 2 层。人格特征必须有证据、跨场景重复、版本和撤销，不能把一次情绪或模型总结直接晋升为永久人格。建议使用本人明确授权的多场景样本创建 CosyVoice v3.5 复刻音色，与当前设计音色、Qwen-Audio-TTS、Qwen3-TTS VC 做盲测。

### 8.3 声纹与非主人识别

**当前成熟度：原型门禁 1/5，生产身份 0/5。** `speaker_verify.py` 文件自身说明它只是 numpy log-mel mean/std embedding；登记超时/失败可进入 `OPEN`，且当前业务主要是接受/拒绝，不能识别具体访客，也没有防重放保证。

正式方案建议使用 ModelScope 官方 3D-Speaker 的 CAM++ 或 ERes2NetV2 做 target speaker verification；该项目官方提供 verification、recognition、diarization 和 ONNX Runtime，Apache-2.0。实时主链先只做 1:1 主人验证，输出：

| 状态 | 普通对话 | 主人私人记忆 | 写主人长期人格 | 敏感动作 |
|---|---:|---:|---:|---:|
| `owner` | 允许 | 按授权 | 进入候选/确认流程 | 仍需按风险二次认证 |
| `guest` | 允许 | 禁止 | 默认禁止，访客隔离 | 禁止 |
| `uncertain` | 允许 | 默认禁止 | 暂存待确认 | Passkey/设备解锁/主人确认 |

Speaker Verification 应异步/shadow 旁路，不阻塞首字；短话、重叠、远场、回声、感冒、模型超时统一降为 `uncertain`，不能 fail-open 成 owner。3D-Speaker README 没有承诺防重放/合成音活体能力，必须另加 anti-spoof/liveness；高风险动作不能只靠声纹。

## 9. 分阶段建议

### P0：当前主链先守住正确性

1. 不因本次对比更换 Fun-ASR 主链或 CosyVoice 协议适配器。
2. 给 `v3.5 + 设计音色 + 字时间戳` 增加 readiness 硬门禁：连续样本必须同时有音频、可用字时间戳、取消后无迟到播放。
3. 真实 iPhone/Android、耳机/扬声器采集：EOU→首声 P50/P95、真打断→静音 P95、误打断率、旧 PCM 泄漏率、actual-heard 偏差、回声误提交率。
4. 固定模型版本和地域；稳定别名变更时自动跑专名、方言、噪声、短打断回归。

### P1：先把身份和“本人声音”做成可验证 A/B

1. 3D-Speaker CAM++/ERes2NetV2 以 shadow 模式输出 `owner/guest/uncertain`，用 200 条授权真人/访客/噪声/重放样本测 FAR、FRR、EER、unknown rejection。
2. 将当前 fail-open 频谱门禁退出身份权限；访客不静音，改为隔离对话与记忆权限。
3. 创建 CosyVoice v3.5 本人复刻音色，对比当前设计音色、Qwen-Audio 3.0 TTS、Qwen3-TTS VC：相似度、自然度、长句稳定性、指令遵循、首包、取消尾音和时间戳完整率。
4. 同步建设声音样本授权、版本、激活、撤销和供应商删除流程；声纹样本与合成声音样本分开授权。

### P2：记忆和人格形成真正壁垒

1. 落地追加式 Evidence Ledger 与可重建投影，先保存可信 final、说话人结论和实际已听助手文本。
2. MemoryCompiler 异步抽取人物、关系、人生情节、经验问答、价值观候选；敏感或冲突内容必须进审核队列。
3. PersonaEngine 只学习 `owner + confirmed` 证据，排除访客、助手、合成音、电视和回声污染。
4. 实时快车道只读取预计算的 `ContextBundle/PersonaCapsule`，不让记忆编译阻塞首声。

### P3：端到端 S2S 只在控制契约完整后扩大

只有同时满足以下条件，才考虑用 Qwen-Audio-Realtime/Omni 或其他 S2S 扩大流量：

- 真设备自然度显著优于级联；
- P95 首声、打断停止、回声和误提交不退化；
- 可重建实际已听、说话人归属和完整消息事件；
- 工具调用、旧 response 和迟到事件仍有 generation/epoch 隔离；
- provider readiness、成本、并发、地域和降级方案可接受。

## 10. 最终选型表

| 能力位置 | 推荐 | 角色 | 是否立即替换 |
|---|---|---|---:|
| 主实时 ASR | Fun-ASR Realtime | 权威转写、热词、上下文、时间戳 | 否，保留 |
| 情感 ASR | Qwen3-ASR Flash Realtime | 非阻塞当前话轮情感 | 否，保留侧车 |
| 主 TTS | CosyVoice v3.5 | 近期转向本人复刻音色；设计音色保留基线 | 先 A/B |
| 自然度 TTS | Qwen-Audio 3.0 TTS | 复刻/指令候选 | 隔离 A/B |
| 低延迟 TTS | Qwen3-TTS VC Realtime | 实验候选 | 暂不主用 |
| 旧 TTS | Qwen-TTS Realtime | 旧版 | 不新接入 |
| 声纹 | 3D-Speaker CAM++/ERes2NetV2 + anti-spoof | owner/guest/uncertain | 必须新增 |
| 终身记忆 | Memoria 自有 Ledger/Archive | 产品数据壁垒 | 必须新增 |
| 人格 | Memoria 自有 PersonaEngine | 证据化、版本化心智壁垒 | 必须新增 |

最终建议不是“Fun-ASR 还是 Qwen-ASR”“CosyVoice 还是 Qwen-TTS”二选一，而是让供应商承担可替换的感知/表达能力，让 Memoria 自己拥有身份、实际已听、记忆证据和人格演化控制面。这样既能继续逼近 GPT Live 的实时体验，也不会把真正的产品壁垒绑定在某个模型名称上。

## 11. 官方来源索引

### 阿里云百炼

- [语音识别模型总览](https://help.aliyun.com/zh/model-studio/asr-model)
- [实时语音识别使用说明](https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide)
- [Fun-ASR Realtime WebSocket API](https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api)
- [Fun-ASR 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [Fun-ASR 服务端事件](https://help.aliyun.com/zh/model-studio/fun-asr-server-events)
- [Qwen-ASR Realtime 交互流程](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-interaction-process)
- [Qwen-ASR Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-client-events)
- [Qwen-ASR Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-server-events)
- [语音合成模型总览](https://help.aliyun.com/zh/model-studio/tts-model)
- [实时语音合成使用说明](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide)
- [Qwen-Audio-TTS / CosyVoice 客户端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-client-events)
- [Qwen-Audio-TTS / CosyVoice 服务端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-server-events)
- [Qwen-TTS Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-client-events)
- [Qwen-TTS Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-server-events)
- [声音复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)
- [声音设计](https://help.aliyun.com/zh/model-studio/voice-design-user-guide)
- [模型计费](https://help.aliyun.com/zh/model-studio/model-pricing)
- [限流](https://help.aliyun.com/zh/model-studio/rate-limit)
- [数据安全](https://help.aliyun.com/zh/model-studio/data-security)

### 官方开源项目

- [ModelScope FunASR](https://github.com/modelscope/FunASR)
- [FunAudioLLM CosyVoice](https://github.com/FunAudioLLM/CosyVoice)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- [ModelScope 3D-Speaker](https://github.com/modelscope/3D-Speaker)

### OpenAI 官方参考

- [Voice agents：speech-to-speech 与 chained pipeline](https://developers.openai.com/api/docs/guides/voice-agents#choose-the-right-architecture)
- [Realtime：Interruption and Truncation](https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation)
