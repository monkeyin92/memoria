# Fun-ASR、Qwen ASR、Qwen-Audio、CosyVoice 与 Qwen-TTS Realtime 对比

> 核验日期：2026-07-19  
> 证据边界：阿里云百炼官方文档、Qwen/FunAudioLLM/ModelScope 官方仓库，以及 Memoria 当前代码与 `HANDOFF.md`。  
> 本文严格区分云产品、开源项目和 Memoria 当前实装。厂商未公开的延迟、并发和识别置信度不作推断；开源仓库的测试数字不等于云 API SLA。

## 1. 结论

针对 Memoria，近期最合理的生产组合仍是：

```text
Fun-ASR Realtime 主转写
+ Qwen3-ASR Flash Realtime 非阻塞情感侧车
+ 独立 SpeakerAuthority（owner / guest / uncertain）
+ Qwen LLM + Memoria 自有记忆 / Persona
+ CosyVoice v3.5 本人复刻音色
+ UtteranceRouter / GenerationFence / HeardTextTracker
```

关键判断：

1. **不要把 Fun-ASR 主链直接换成 Qwen3-ASR Realtime。** Fun-ASR 的热词、对话上下文和字级时间戳更适合人名、家族称谓、档案证据和实际已听对齐；Qwen3-ASR 的独特价值是稳定前缀 `text + stash` 与七类情感。
2. **人格声音首选 CosyVoice v3.5 复刻音色。** 它同时满足双向流式、声音复刻、自然语言指令和官方字级时间戳契约。当前 `warm_companion` 是“设计音色”，只是好听，不代表像用户本人。
3. **Qwen-Audio-TTS 是独立 TTS，不是实时对话模型。** 它适合做自然度、相似度和指令遵循 A/B；其复刻音色不支持字级时间戳，不宜无损替换 Memoria 主 TTS。
4. **Qwen3-TTS Realtime 也是独立 TTS。** Flash、Instruct、VC、VD 分别负责系统音色、指令、复刻和设计，不是一个同时具备全部能力的模型；公开事件没有字词时间戳，也没有 GPT Realtime 等价的生成中截断契约。
5. **旧 `qwen-tts-realtime*` 不应新接。** 官方已经归到“旧版、按 Token 计费”，并建议迁移到 Qwen3-TTS。
6. **若要做最接近 GPT Live 的阿里系端到端 A/B，应评估 `Qwen-Audio-Realtime`，不是 Qwen-Audio-TTS 或 Qwen-TTS-Realtime。** 它是端到端语音对话模型，具备双工 WebSocket、`smart_turn`、Function Calling、上下文事件、自动打断、复刻音色和目标说话人增强。
7. **即使使用 Qwen-Audio-Realtime，声纹权限仍必须由 Memoria 自己掌握。** 官方目标说话人增强用于锁定/过滤说话人，没有输出可审计的 `owner / guest / uncertain` 身份结论，不能直接决定私人记忆或敏感动作权限。

## 2. 先校准名称

| 名称 | 准确定位 | 容易混淆之处 |
|---|---|---|
| 云 `fun-asr-realtime` | 百炼实时 ASR | 不等于开源 FunASR 工具包，也不自带实时说话人分离 |
| `qwen3-asr-flash-realtime` | 百炼实时 ASR | 不是端到端语音对话模型，也不是声纹模型 |
| Qwen-Audio-TTS | 独立 TTS，支持系统/复刻音色和指令 | 不是 Qwen-Audio-Realtime |
| Qwen-Audio-Realtime | 端到端实时语音对话模型 | 声音复刻只用于它的回复音色 |
| CosyVoice | 独立 TTS，支持复刻/设计/指令（依版本） | 云 v3.5 不等于官方仓库公开的 Fun-CosyVoice 3.0 权重 |
| Qwen3-TTS Realtime | 新版独立实时 TTS，按 Flash/Instruct/VC/VD 分线 | `Realtime` 指低延迟流式 TTS，不代表全双工对话 |
| `qwen-tts-realtime*` | 旧版独立实时 TTS | 官方已列入旧版，不是 Qwen3-TTS |

## 3. Fun-ASR Realtime 与 Qwen3-ASR Flash Realtime

| 维度 | Fun-ASR Realtime | Qwen3-ASR Flash Realtime | 对 Memoria 的影响 |
|---|---|---|---|
| 协议 | `run-task → continue-task → finish-task`；控制 JSON，音频二进制 | Realtime 事件协议；Base64 音频放 JSON | 需要两套适配器，不能只换模型名 |
| 实时文本 | 中间/最终 `sentence` | 已确认前缀 `text` + 可变草稿 `stash` + final | Qwen 的字幕草稿体验较好 |
| 字词时间戳 | `words[].begin_time/end_time` | 转写事件无字词时间戳，只有 VAD 音频起止 | Fun-ASR 更适合档案证据和对齐 |
| 热词 | `vocabulary_id` | 公开协议未提供 | 专名、称谓、行业词继续选 Fun-ASR |
| 对话上下文 | 官方为稳定别名及 `2025-11-07` 记录 `context`；可 `continue-task` 更新 | 公开协议未提供 | 可把近期已确认专名送 Fun-ASR，不应塞整份人生档案 |
| 断句 | VAD/语义断句、静音与噪声阈值、heartbeat | server VAD 或 manual commit | 两者都要在真实设备按 P95/P99 调参 |
| 情感 | 实时接口不提供 | neutral/happy/sad/angry/fearful/disgusted/surprised | Qwen 适合做本轮语气侧车 |
| 情感置信度 | 不适用 | 官方事件未返回 confidence | 不可直接写人格或驱动权限 |
| 实时说话人分离 | 不支持 | 不支持 | “谁说了什么”需独立 diarization/归属链 |
| 声纹身份 | 不支持 | 不支持 | 必须使用独立 speaker verification |

开源边界也要分清：开源 FunASR 是一个可组合 ASR、VAD、标点、CAM++ 和 diarization 的工具包，工具包源码 MIT、模型权重许可证各自独立；这不表示云 `fun-asr-realtime` 自动返回 speaker ID。开源 Qwen3-ASR 支持 streaming/offline 统一推理，但官方 README 明确 streaming 不返回时间戳；时间戳需要另跑 Qwen3-ForcedAligner。

### 推荐 ASR 路由

```text
AEC 后同一份 16 kHz PCM
  ├─ Fun-ASR：权威实时转写、热词、上下文、字级时间戳
  ├─ Qwen3-ASR：非阻塞本轮情感观察量
  └─ SpeakerAuthority：owner / guest / uncertain
```

Qwen 情感只可调整本轮回复长度、措辞和 TTS 表达，不得改变事实、工具参数、主人身份或长期人格。

## 4. Qwen-Audio-TTS、CosyVoice、Qwen3-TTS Realtime 与旧 Qwen-TTS

| 维度 | Qwen-Audio-TTS | CosyVoice v3.5 | Qwen3-TTS Realtime | 旧 Qwen-TTS Realtime |
|---|---|---|---|---|
| 定位 | 独立高自然度 TTS | 独立可控/复刻/设计 TTS | 新版低延迟流式 TTS | 旧版流式 TTS |
| WebSocket | 同一模型名兼容 WS/HTTP；WS 双向流式 | 同左 | 带 `-realtime` 模型走 Realtime WS | `qwen-tts-realtime*` WS |
| 文本流入 / 音频流出 | 支持 / 支持 | 支持 / 支持 | 支持 / 支持 | 支持 / 支持 |
| 系统音色 | 支持 | v3.5 plus/flash 不支持 | Flash/Instruct 线 | 支持 |
| 声音复刻 | 支持 | 支持 | 仅 VC 线 | 不支持 |
| 声音设计 | 不支持 | 支持 | 仅 VD 线 | 不支持 |
| 自然语言指令 | 支持 | 支持 | 仅 Instruct 线 | 不支持 |
| 复刻 + 指令同一模型 | 支持 | 支持 | 不支持，VC 与 Instruct 分线 | 不支持 |
| 字级时间戳 | 系统音色依表；**复刻音色不支持** | 官方覆盖 v3.5/v3/v2 复刻音色及标明支持的系统音色 | 公开事件未提供 | 公开事件未提供 |
| 生成中取消 | 无 Realtime 对话式 cancel；应用需停播并丢弃迟到音频 | 同左；通常关闭/丢弃当前 task/连接 | 客户端事件有清未提交文本缓冲，未公开生成中 `response.cancel` | 未提供 GPT Realtime 等价截断契约 |
| 是否理解用户语音并回答 | 否 | 否 | 否 | 否 |

### Qwen3-TTS Realtime 不是“全能力合一”

| 目标 | 模型线 | 复刻 | 设计 | 指令 | 公开字词时间戳 |
|---|---:|---:|---:|---:|---:|
| 系统音色实时合成 | Flash | 否 | 否 | 否 | 无 |
| 系统音色 + 指令 | Instruct | 否 | 否 | 是 | 无 |
| 本人音色复刻 | VC | 是 | 否 | 否 | 无 |
| 描述生成新声音 | VD | 否 | 是 | 否 | 无 |

Qwen3-TTS 官方开源 README 报告端到端最低 97 ms，但这是开源模型及其测试口径，不是百炼云 API 的 P50/P95 SLA；同一 README 当前还说明 vLLM-Omni 仅支持离线推理，在线 serving 后续提供。因此不能据此承诺生产云端或自部署首包延迟。

### 对 Memoria 的 TTS 排序

1. **主候选：CosyVoice v3.5 本人复刻音色。** 兼顾复刻、流式、指令和官方字时间戳，最匹配 `HeardTextTracker`。
2. **基线：当前 CosyVoice v3.5 设计音色。** 适合比较“好听”，但不是本人复刻；官方时间戳说明只明确覆盖 v3.5 复刻音色，设计音色必须以 readiness 实测为准。
3. **自然度 A/B：Qwen-Audio 3.0 TTS。** 重点盲测音色相似度、自然度和指令遵循；接受复刻音色无字时间戳的代价，先不进主链。
4. **低延迟 A/B：Qwen3-TTS VC Realtime。** 只有真实设备首包/取消明显更好，并补齐实际已听对齐后再扩大。
5. **不新增：旧 `qwen-tts-realtime*`。** 官方已建议优先迁移 Qwen3-TTS。

## 5. 哪个才接近 GPT Live 式端到端体验

Fun-ASR、Qwen3-ASR、Qwen-Audio-TTS、CosyVoice 和 Qwen3-TTS 都是单向组件；把它们串起来可以做优秀的可打断级联，但不会自动变成原生 speech-to-speech。

阿里系当前更适合隔离 A/B 的端到端候选是 **Qwen-Audio-Realtime**。官方公开能力包括：

- WebSocket 双工协议，持续发送麦克风音频并同时接收语音和文本；
- `server_vad`、`smart_turn`、push-to-talk 三种轮次模式；
- `smart_turn` 把附和声视为 ambient audio，不必打断主回复；
- Function Calling 和对话项创建、查询、删除；
- 用户开口自动取消当前响应，客户端也可发 `response.cancel`；
- 系统音色与声音复刻音色；
- `smart_turn.voiceprint_audio_urls` 目标说话人增强，最多 5 个 16 kHz PCM/WAV URL。

但它仍不等于 Memoria 已经获得完整产品能力：

1. 目标说话人增强是过滤/锁定能力，不是输出身份和权限的 SpeakerAuthority；无法说明非目标者是谁，也不能作为高风险认证。
2. `response.done` 的完整 transcript 不是“用户实际听到的内容”；本地清播放缓冲、播放进度、迟到 PCM 隔离和实际已听账本仍要自己实现。
3. 云端会话上下文不是终身记忆；人物、时间线、经验、人格版本、纠错、撤销和删除仍属于 Memoria。
4. 端到端模型的工具事件也必须映射到 Memoria 的 `turn_id / generation_id / tool_epoch`，避免迟到工具结果或旧 response 污染新话轮。

因此建议保留双车道：

```text
生产快车道：Fun-ASR → Qwen LLM → CosyVoice v3.5 clone
实验快车道：Qwen-Audio-Realtime（独立 A/B，统一事件适配）

共享控制面：SpeakerAuthority + UtteranceRouter + GenerationFence
共享慢车道：Evidence Ledger → MemoryCompiler → PersonaEngine
```

## 6. 对三项产品壁垒的评估

### 6.1 终身记忆

任何 ASR/TTS/端到端会话模型都不会替 Memoria 建成终身档案。正确边界是：

```text
原始/权威证据事件
  → 说话人归属与可修订转写
  → 人物、关系、时间线、故事、经验和价值候选
  → 用户审核、冲突、版本、撤销
  → 可重建检索投影与 PersonaCapsule
```

“永久不会丢失”不应作绝对承诺；应表述为“在用户未删除或改变保存策略前，提供加密、多副本、PITR、导出、校验和恢复演练的长期可靠保存”。访客、助手、电视回声和合成音不能进入主人长期人格训练集。

### 6.2 人格与声音复刻

声音复刻只解决音色和部分韵律，不等于人格或心智。至少要分为：

1. `VoiceProfile`：本人授权音色、模型和版本；
2. `SpeechStyle`：语速、停顿、重音、节奏、口头禅；
3. `DiscourseStyle`：直接/委婉、句长、结构、措辞；
4. `ValuesDecisionModel`：价值排序、决策习惯、例外和随时间变化。

每项长期特征都要有主人证据、跨场景重复、置信度、版本和撤销；一次情绪或模型总结不能直接晋升为永久人格。

### 6.3 声纹与非主人

- Fun-ASR Realtime、Qwen3-ASR Realtime 和所有 TTS 都不输出主人身份。
- Qwen-Audio-Realtime 的目标说话人增强可减少旁人干扰，但不返回可审计的身份三态。
- 生产应继续使用独立 target-speaker verification，例如 3D-Speaker CAM++/ERes2NetV2，并增加 anti-spoof/liveness、质量门和设备辅助信号。
- 默认输出 `owner / guest / uncertain`；访客可普通对话但不能读取或污染主人私人记忆，`uncertain` 对敏感动作要求设备解锁/Passkey/主人确认。
- 若以后要识别具体家人，需要独立的 1:N 已知说话人登记和离线/准实时 diarization，不应把它塞进每轮首声关键路径。

## 7. 对当前 Memoria 的状态判断

截至本次核验：

- 生产 H5 只暴露级联；Qwen Omni/Audio 端到端入口已下线。
- 生产主链是 Fun-ASR Realtime + Qwen LLM + CosyVoice v3.5 设计音色；设计音色并非本人复刻。
- 仓库本地已出现 Evidence Ledger、MemoryCatalog、PersonaEngine、SpeakerAuthority、VoiceProfile 和实时 ContextAssembler，但这些 P1–P6 工作尚未部署。
- 没有授权 200 条真人声纹评估集、没有可宣称 active 的正式主人模板，也没有完成真人 CosyVoice enrollment/盲测。
- 当前 1.50 秒 endpoint 稳定窗明显高于接近 GPT Live 的目标体验；应先建立干净的 speech-stop→首声 P50/P95，再评估动态 EOU 或端到端 `smart_turn`，不能只把静音阈值拍脑袋调低。

所以当前产品成熟度应分开描述：

| 能力 | 生产状态 | 结论 |
|---|---|---|
| 可打断级联对话 | 已上线 | 控制面较完整，仍缺真人设备 SLO 闭环 |
| 端到端 S2S | 内部/历史 A/B | 不作为当前正式能力 |
| 终身记忆 | 本地工程完成度较高、未部署 | 还不能承诺生产“人生知识库” |
| 人格长期学习 | 本地闭环、未做真人 A/B | 还不能承诺“越用越像本人” |
| 本人声音 | 有治理与接线、无正式 enrollment | 当前声音不是本人克隆 |
| 主人/访客识别 | 三态架构和接口已做、无 active 模板 | 当前不能承诺正式身份识别 |

## 8. 建议的决策顺序

1. **保持级联主链，不做模型名驱动的整体替换。**
2. **先完成 CosyVoice v3.5 本人复刻音色与正式 SpeakerAuthority 真人 A/B。** 两者样本、授权、密钥和撤销生命周期必须分离。
3. **把记忆/Persona/声纹本地能力部署成最小闭环，先做到 owner-only 证据、可查来源、可纠正、可撤销。**
4. **用 Qwen-Audio-Realtime 做隔离端到端 A/B。** 与级联比较真实设备首声、真打断停止、误打断、回声误提交、actual-heard 偏差、工具正确性和成本。
5. **只有端到端路线在自然度显著更好，同时不破坏身份、记忆、实际已听和工具隔离时，才扩大流量。**

## 9. 官方来源

### 阿里云百炼

- [语音识别模型总览](https://help.aliyun.com/zh/model-studio/asr-model)
- [Fun-ASR Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [Fun-ASR Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/fun-asr-server-events)
- [Fun-ASR Realtime WebSocket API](https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api)
- [Qwen-ASR Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-client-events)
- [Qwen-ASR Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-server-events)
- [语音合成模型总览](https://help.aliyun.com/zh/model-studio/tts-model)
- [Qwen-Audio-TTS / CosyVoice 客户端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-client-events)
- [Qwen-Audio-TTS / CosyVoice 服务端事件](https://help.aliyun.com/zh/model-studio/cosyvoice-server-events)
- [Qwen-TTS Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-client-events)
- [Qwen-TTS Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-server-events)
- [Qwen-Audio-Realtime 使用说明](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)
- [Qwen-Audio-Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)
- [声音复刻](https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide)

### 官方开源项目

- [ModelScope FunASR](https://github.com/modelscope/FunASR)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)
- [FunAudioLLM CosyVoice](https://github.com/FunAudioLLM/CosyVoice)
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- [ModelScope 3D-Speaker](https://github.com/modelscope/3D-Speaker)

