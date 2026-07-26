---
status: accepted
date: 2026-07-21
amended_by: 0021-wechat-miniprogram-media-gateway
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
- `DuplexRuntime` 按 VAD speech epoch 累积 `ACCEPT` 的 final，并记录是否出现过被隔离的
  final；endpoint 完成时把 canonical snapshot 冻结进 FIFO。`on_user_turn_completed` 必须在
  任何 `await` 之前消费最老 snapshot，只用该 epoch 的已接受片段重建 canonical text；
  没有已接受片段则丢弃整个空话轮。LiveKit 没有提供 turn id 时，依赖其 completed hook
  串行顺序，不允许退化为一个全局字符串累加器。
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
- 快速连续话轮即使 completed hook 排队，也按冻结的 FIFO snapshot 隔离；旧控制回调不会
  误清下一 speech epoch。
- 极端情况下，若真实讲话在播放期完全没有 VAD/PCM 锚点，也会被安全丢弃。实体停止按钮
  仍可立即停止；语音控制必须先有声学锚点。小程序的 AEC 后 voiced PCM 是上述窄例外的
  锚点，但 AEC 运行期失效、健康撤销 ACK 超时或播放 epoch 已变化时仍按无锚点隔离。
- 运维需同时观察 AEC、VAD/final 时序、`unanchored_playback_transcript` 指标和
  `canonical_user_turn_rebuilt` 日志，不能用关键词补丁掩盖设备回声。

## References

- [阿里云百炼 Fun-ASR 客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)
- [阿里云百炼提升语音识别准确率](https://help.aliyun.com/zh/model-studio/improve-asr-accuracy)
