---
status: accepted
date: 2026-07-21
amended_by: 0021-wechat-miniprogram-media-gateway
last_amended: 2026-07-30
---

# 在播放污染后重建权威用户话轮

## Context

真实扬声器设备可能把正在播放的助手语音回灌到麦克风。生产日志进一步证明，
LiveKit 会先发出 `user_input_transcribed` final，再把该 final 追加到内部
`_audio_transcript`；若回灌转写没有对应的新 VAD，旧实现会把它当成正常输入，随后
与用户真正说出的下一句合并。H5 只是展示 Agent 发布的 final，无法判断哪一段来自回声。

FunASR 的多轮 `input.context` 会提高历史内容的识别偏置。它不是本次声学回灌的根因，
但会放大“上一轮内容出现在当前识别”这一失效模式。

## Decision

以 Runtime 接受后的 final 作为唯一权威用户话轮：

- 播放期间没有新 VAD/PCM 锚点的转写一律不可信，interim 只等待，final 直接隔离；
  不能仅凭“不是”“我问的是”“停一下”等文本关键词放行。
- 原生小程序只允许一个窄例外：媒体网关启动时已证明 AEC 可用、运行期健康尚未被
  单向撤销、当前播放实例内曾有一个 900 ms 的 AEC 后 PCM 窗口至少包含 160 ms voiced，
  并且冻结的声学 witness 距转写到达不超过 2500 ms 时，才允许 `UtteranceRouter`
  判定为纯控制命令的转写继续进入 TargetSpeakerFocus。witness 保存对应的有声窗口，
  不能用转写到达时已经被静音覆盖的 PCM 尾窗替代；VAD start、播放替换/结束或 AEC 撤销
  都会清除它。命令还必须未命中助手回声/语言/backchannel 门禁，只能停止当前播放，
  不能进入 chat、历史、记忆或权限平面；普通 H5、普通聊天与 interrupt+chat 不走此例外。
- `SpeechEpochAssembler` 按 provider final segment 保存已接受文本，而不是把整个 VAD
  speech epoch 压成不可拆分的 FIFO snapshot。LiveKit 的一个逻辑话轮可以覆盖多个 VAD
  epoch；反过来，迟到 final 也可能在下一 VAD epoch 开始后才到达。因此
  `on_user_turn_completed` 以 LiveKit 已提交文本为关联证据，选择最小的连续 segment 集，
  允许跨 epoch 合并、跳过不相关旧 segment，并将未匹配旧 segment 隔离，禁止留给后续话轮。
  连续相同文本按 completed hook 顺序各消费一次；没有可信匹配时 fail closed。
- FunASR 通过独立 side-channel 记录有界数值时序
  `task_epoch / sentence_id / begin_ms / end_ms / duration_ms`，供后续按音频区间关联与重连
  诊断使用；该通道不得携带 transcript、provider task id、设备标识或麦克风标签。当前
  canonical 正确性不依赖该诊断通道可用。
- `UtteranceRouter`、LLM 上下文、H5 final、证据账本、记忆和 Persona 只能消费同一份
  canonical text，客户端不再承担清洗或猜测职责。
- `AgentSession.clear_user_turn()` 继续用于纯控制话轮和播放结束后的 endpoint 清理，但只是一项
  卫生措施，不是防止回声污染的正确性前提；迟到控制回调只能清理自己消费的 epoch，不能
  清除已经开始的下一轮真实语音。
- FunASR 历史对话 context 默认关闭；只有显式受控实验通过历史泄漏回归后才可开启。
- 浏览器/系统 AEC、NS 与硬件回声控制仍是第一道防线；服务端 canonical 边界负责在
  声学防线失效时阻止错误内容进入产品状态。

## Considered Options

- 只在 H5 隐藏可疑前缀：拒绝。LLM、档案和 Persona 已经会先收到污染文本。
- 收到可疑 final 后异步调用 `clear_user_turn()`：拒绝。真实语音可能已经进入同一
  epoch，延迟清理会误删合法输入。
- 只做助手文本相似度过滤：拒绝。ASR 回声可能出现同音改写，且短控制词与普通语义
  会相撞。
- 关闭 FunASR context 即视为修复：拒绝。它只能减少历史偏置，不能消除声学回灌。

## Consequences

- 用户看见、模型理解和长期归档的用户 final 保持一致；被污染的 LiveKit 内部拼接文本
  不再成为权威内容。
- 快速连续话轮即使 completed hook 排队，也按独立 final segment 与 callback 顺序隔离；
  多段停顿可以合成一个逻辑话轮，旧 segment 不会在两三轮后重新出现。
- 极端情况下，若真实讲话在播放期完全没有 VAD/PCM 锚点，也会被安全丢弃。实体停止按钮
  仍可立即停止；语音控制必须先有声学锚点。小程序的 AEC 后 voiced PCM 是上述窄例外的
  锚点，但 AEC 运行期失效、健康撤销 ACK 超时或播放 epoch 已变化时仍按无锚点隔离。
- 运维需同时观察 AEC、VAD/final 时序、FunASR 数值时序、
  `unanchored_playback_transcript` 指标和 `canonical_user_turn_rebuilt` 日志，不能用
  关键词补丁掩盖设备回声。

## References

- [阿里云百炼 Fun-ASR 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [阿里云百炼提升语音识别准确率](https://help.aliyun.com/zh/model-studio/improve-asr-accuracy)
