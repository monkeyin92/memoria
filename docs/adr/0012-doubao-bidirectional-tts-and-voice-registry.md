---
status: accepted
date: 2026-07-21
---

# 级联播放主链采用豆包双向流式 TTS 与批准音色目录

## Context

Memoria 的级联链路会从流式 LLM 持续得到短语，并且必须在用户打断时同时
停止播放、隔离旧 generation、取消 TTS 会话和截断实际已听文本。豆包的
单向流式接口要求客户端一次提交完整文本，服务端再流式返回音频，适合固定文案，
但会让首段等待完整回答，也无法在同一会话中自然接收 LLM 的增量文本。

豆包 TTS 2.0 双向流式 WebSocket 提供连接、会话和增量任务三个生命周期，
可以在 LLM 产生可独立朗读的短语时立即发送，并返回音频与字幕事件。真实供应商
探针同时发现：2026-07-21 把情绪指令放入 `context_texts` 曾使字幕时间戳相对
PCM 偏移超过 1.4 秒，破坏打断后的实际已听文本计算。2026-07-29 更新的官方
双向接口文档已明确：`seed-tts-2.0` 预置音色可通过 `context_texts` 使用自然语言
语音指令或引用上文；复刻音色不支持。该能力因此可以重新接线，但不能绕过真实
时间戳门禁直接全量开启。

## Decision

级联播放主链固定使用豆包 TTS 2.0 双向流式 WebSocket：

- 地址为 `wss://openspeech.bytedance.com/api/v3/tts/bidirection`，资源模型为
  `seed-tts-2.0`。
- 每条可复用连接先完成 `StartConnection`；每次回答建立独立 session，LLM
  增量短语分别作为 `TaskRequest` 发送，文本结束后发送 `FinishSession`。
- 正常完成的连接回到池中。打断时先提升 generation fence、停止播放器并取消
  sender/receiver，再向活跃 session 发送 `CancelSession`，随后关闭并从池中
  剔除该连接；任何迟到音频仍必须经过 fence 丢弃。
- 输出契约固定为 PCM s16le、24 kHz、mono，并要求字级时间戳。时间戳必须单调，
  且继续作为 `HeardTextTracker` 截断助手历史的事实来源；缺失时间戳不能通过
  provider smoke/readiness。
- 鉴权允许两种形式：非空 `DOUBAO_TTS_API_KEY`，或同时提供非空
  `DOUBAO_TTS_APP_ID` 与 `DOUBAO_TTS_ACCESS_TOKEN`。火山引擎 Secret Key
  不参与该 WebSocket 鉴权，不进入 Agent 配置，也不得部署。
- `DOUBAO_TTS_STYLE_CONTROL_ENABLED` 默认 `false`。仅在开关开启且当前资源为
  `seed-tts-2.0` 预置音色时发送 `context_texts`；`seed-icl-2.0` 个人复刻音色
  无条件清空该字段。
- `SpeechPlan` 是唯一的 provider-neutral 控制面。情绪、方言和语气只能从固定枚举
  映射为短白名单指令；不得把 LLM 输出或用户原话当成自由指令。明确的用户命令可选择
  悲伤、生气、四川话、北京话、撒娇、暧昧、争辩或夹子音；叙述“她在撒娇/他们在吵架”
  不能误改助手音色。争辩仍禁止辱骂和威胁，暧昧仍保持关系边界。
- 语速使用 `audio_params.speech_rate`，音调使用 `post_process.pitch`，当前显式请求分别
  限在 `0.90/1.10` 与 `-2/+2`；自然语音指令不再重复要求语速或音调，避免和数值字段冲突。
- 引用上文只来自当前 speaker scope 尾部连续的短期工作记忆：助手消息必须是
  `actual-heard`，当前用户 final 必须先脱敏；遇到 `owner/public` 边界立即停止。发送前
  再压成一条有界 `context_texts`，明确标注“只理解语境和承接情绪，不要朗读”。真正
  合成的回答仍只出现在 `TaskRequest.req_params.text`。
- 不使用 provider `section_id` 保存跨请求历史。服务端历史无法遵守 Memoria 的
  generation fence、取消、实际已听和 owner/public 隔离边界。
- 不发送 SSML、`[laughter]`、`[laugh]` 或其他未被当前双向 WS 契约定义的动作标签；
  “带一点自然笑意”只作为软能力，不能宣称一定产生真实笑声。

五个机器人只从 `infra/voices/doubao_voice_ids.json` 的批准目录解析默认音色：

| 机器人 | profile | 豆包音色 | speaker ID |
|---|---|---|---|
| 星澜 | `warm_companion` | 阳光青年 2.0 | `zh_male_yangguangqingnian_uranus_bigtts` |
| 桃喜 | `bright_peer` | 甜美桃子 2.0 | `zh_female_tianmeitaozi_uranus_bigtts` |
| 绵绵 | `soft_confidante` | 温柔小雅 2.0 | `zh_female_wenrouxiaoya_uranus_bigtts` |
| 阿序 | `calm_guide` | 高冷沉稳 2.0 | `zh_male_gaolengchenwen_uranus_bigtts` |
| 玄墨 | `low_magnetic` | 深夜播客 2.0 | `zh_male_shenyeboke_uranus_bigtts` |

历史 CosyVoice clone profile 和授权记录继续保留，以支持查看、评估和撤销，
但不再进入当前播放链路。即使历史 profile 仍为 active，Agent 也必须回退到
当前机器人的上述豆包默认音色，不能把 CosyVoice voice ID 发送给豆包。

## Considered Options

- 豆包单向流式：协议更简单，适合一次提交完整固定文案；拒绝用于实时级联主链，
  因为它不能直接消费 LLM 增量短语，首音频和取消边界都更差。
- 继续使用 CosyVoice 作为播放主链：现有适配器和历史 clone 资产可复用，但与
  已选定的豆包音色库和双向会话协议不一致；历史资产生命周期与播放 provider
  因此分离。
- 无门禁地开启 `context_texts`：主观表达可能更丰富，但历史实测超过 1.4 秒的
  时间戳偏移会破坏精确打断和已听文本，因此拒绝。保留默认关闭的能力开关和真实
  Provider canary，在不牺牲实际已听事实的前提下逐步验证。

## Consequences

- 生产候选配置在写盘前必须验证豆包鉴权完整，且按最小权限只把鉴权路由给
  Agent。
- provider smoke 必须用增量 `TaskRequest` 验证真实 PCM 与字级时间戳；只检查
  WebSocket 建连或 HTTP 状态不算通过。
- 当风格开关开启时，provider smoke 额外发送白名单指令与引用上文，并要求原始
  时间戳为 `alignment_status=ok` 或现有播放链已支持的 `scaled`；`degraded` 仍直接失败。
  2026-07-29 生产候选对照中，无 `context_texts` 的 `calm_guide/low_magnetic` 同样出现
  `scaled`，因此 raw-only 不是有效的风格回归判据。门禁必须遍历当前批准目录中的五个
  预置音色，并把每段风格音频交给 FunASR 回识别目标正文、拒绝引用上文关键词；开关
  缺失或关闭时 required smoke 必须失败，不能通过 readiness。
- `SpeechPlan`、引用上文和 provider session 配置必须按 generation fence 冻结。控制确认音、
  迟到回调或后续话轮不得覆写当前回答；self-preview/legacy 最终修正 voice target 后必须对
  同一 fence 重应用冻结计划。
- 音色变更必须先更新批准目录并重新完成试听与时间戳门禁，不能在生产环境任意
  注入 speaker ID。
- CosyVoice 专属协议、配置和验收仅作为历史实现资料保留；provider-neutral 的
  generation fence、原子打断、连接淘汰和实际已听文本不变量继续有效。

## References

- [豆包双向流式语音合成](https://www.volcengine.com/docs/6561/2532486?lang=zh)
- [豆包语音指令与引用上文](https://www.volcengine.com/docs/6561/1871062?lang=zh)
- [豆包语音合成音色列表](https://www.volcengine.com/docs/6561/1257544?lang=zh)
