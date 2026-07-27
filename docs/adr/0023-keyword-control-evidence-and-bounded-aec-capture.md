---
status: accepted
date: 2026-07-27
---

# 用独立 KWS 补足短控制词证据，并限制 AEC 音频诊断范围

## Context

微信小程序体验版 `0.8.51` 的真机会话
`41f301e3-728b-4ca2-9de7-a323439659ec` 已证明：

- 用户在助手播放时开口后，VAD 能触发 barge-in 和本地静音；
- FunASR 最终只返回“他。”，原始“停一下/等一下”词形已丢失；
- “他。”随后按普通聊天提交，而按钮 `user_button` 可以正常停止同一播放并推进
  generation。

因此继续扩充文本规则不能修复该场景。`UtteranceRouter` 只能路由已经存在的文本证据，
不能从“他。”恢复声学上已经消失的控制词。另一方面，现有 gateway 没有按同一真机会话
保存 AEC 前后 PCM，尚不能区分“原始采集已坏”和“AEC 削弱句首”。

## Decision

- 在 Agent 侧增加可选 Vosk 受限词表识别。它消费 FunASR 已有 PCM observer 的 AEC 后
  16 kHz 单声道 PCM，不新增第二条媒体链路或 LiveKit data topic。
- KWS 只在以下条件同时满足时解码并产生控制证据：
  - job 是 gateway 声明过 AEC ready 的可信小程序会话；
  - 助手当前正在播放；
  - 当前 VAD speech epoch 已开始、属于该播放实例，并已采集至少 80 ms PCM；
  - 当前 VAD 结束后的完整结果精确等于配置的纯控制词，不含 `[unk]`，平均置信度达到门槛；
  - 命中词经 `route_utterance` 仍是纯 `INTERRUPT_COMMAND`。
- partial 只用于解码过程，不执行停止。实际 smoke 证明“等一下我想问……”和
  “别说了这个词……”会先产生纯控制词 partial；提前执行会吞掉后续聊天内容。
- 每次解码绑定 `speaker_epoch + playback_epoch + GenerationFence`。播放替换、AEC 信任
  撤销、VAD epoch 变化或迟到结果都会失效，不能停止下一段播放。
- KWS 命中只设置当前 speech epoch 的 sticky Router 证据，并复用
  `TargetSpeakerFocus → on_real_interrupt → Orchestrator.confirm_interruption`。它不能直接
  改 generation、停止播放器、提交聊天、写历史、改变说话人权限或选择确认语。
- 普通 ASR final 即使随后变成“他。”，仍由 sticky Router 证据抑制为纯控制话轮，不进入
  LLM、历史、记忆或 Persona；同一 speech epoch 只确认一次。
- KWS 模型或运行库缺失、加载失败、解码异常时 fail closed：停用 KWS，普通 FunASR
  对话继续运行。
- `vosk==0.3.45` 固定在 Agent 镜像并继续受 `--require-hashes` 约束。官方模型表将
  `vosk-model-small-cn-0.22` 标记为 Apache-2.0，且 Python 3.12/Linux amd64 已完成实际
  推理 smoke。
- 模型 zip 本身不含独立 LICENSE/NOTICE。operator 从官方 URL 下载并校验固定 SHA-256 后
  放在 Agent `/data` bind 中；仓库、镜像和 Memoria 发布包不再分发该权重。
- gateway 增加单会话 AEC 诊断：只有配置的 session ID 与 ticket session 精确一致时，
  才保存最多 0.5–15 秒（默认 5 秒）的 AEC 前后对齐 WAV。
- 诊断目录为 `0700`、文件为 `0600`；文件名和 manifest 只保存 session hash、采样参数、
  时长和 SHA-256，不保存原始 session ID、用户身份或转写。保存失败不得影响媒体会话。

## Consequences

- “ASR 词形完全丢失”不再要求 Router 猜词；短控制命令有独立、低延迟的声学证据通道。
- Router、TargetSpeakerFocus、speaker authority 和 generation fence 仍是唯一控制面，
  不会形成 KWS 与普通 ASR 互相竞争的两套状态机。
- 默认配置行为不变：没有显式模型和开关时 KWS 不运行；H5、普通 LiveKit 会话和非可信
  gateway 会话都不会使用该通道。VAD start 仍先 duck，终稿识别只负责确认控制意图。
- 模型阈值仍需在 iOS/Android 外放、听筒、蓝牙和噪声集上校准；自动化只能证明控制面和
  fence，不能证明真机召回率。
- 下一次失败可用同一 session 的 pre/post WAV 判断 AEC 是否削弱句首，但采样包含真实声音，
  必须测试后立即导出到 root-only 目录并关闭开关。

## Alternatives considered

1. 继续增加“他/塔/听”等文本近似规则：拒绝。会把普通聊天误判为停止，也无法覆盖下一种
   ASR 错词。
2. KWS 直接调用播放器或 generation：拒绝。会绕过 Router、说话人策略和迟到结果 fence。
3. 在 gateway 内运行 KWS：暂不采用。需要新增 gateway→Agent 控制协议，而 Agent 已经拿到
   同一份 AEC 后 PCM。
4. 在 partial 首次出现控制词时立即停止：拒绝。会把控制词后跟真实内容的话轮误吞为纯中断。
5. 常开、全会话录音诊断：拒绝。与定位单一声学问题不成比例，增加隐私和磁盘风险。

## References

- [Vosk models](https://alphacephei.com/vosk/models)
- [Vosk grammar recognizer API](https://github.com/alphacep/vosk-api/blob/master/src/vosk_api.h)
- [`2026-07-27-vosk-chinese-kws.md`](../research/2026-07-27-vosk-chinese-kws.md)
- [`0021-wechat-miniprogram-media-gateway.md`](0021-wechat-miniprogram-media-gateway.md)
- [`0022-ambiguous-interrupt-semantic-evidence.md`](0022-ambiguous-interrupt-semantic-evidence.md)
