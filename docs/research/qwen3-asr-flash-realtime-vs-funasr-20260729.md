# Qwen3-ASR Flash Realtime 是否替换 Memoria 主 FunASR

> 核验日期：2026-07-29。资料边界：阿里云百炼官方实时语音识别文档、Qwen ASR Realtime API 文档，以及仓库当前实现。

## 结论

**现在不要用 `qwen3-asr-flash-realtime` 直接替换 Memoria 主 FunASR。** 当前“FunASR 主转写 + Qwen 情绪 sidecar”是更稳妥的组合：Qwen 的七类情绪有产品价值，但尚不足以抵消主链在时间戳、热词、上下文、重连恢复和 LiveKit 事件契约上的缺口。

若目标是简化为单一 Qwen 流，必须先实现完整的 `QwenASRSTT`（LiveKit STT 适配、interim/final、endpoint、取消与 generation fence、断线恢复、重复终稿去重、错误/超时门禁），再做 shadow A/B，验证通过后才允许小流量 canary。

## 官方能力与限制

阿里云实时语音识别总览说明实时任务支持持续音频流和中间/最终结果，具体事件与参数以模型 API 为准：[实时语音识别用户指南](https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide)。Qwen Realtime API 的关键行为如下：[Qwen ASR Realtime API](https://help.aliyun.com/zh/model-studio/qwen-asr-realtime-api/)。

| 维度 | Fun-ASR Realtime（当前主链） | Qwen3-ASR Flash Realtime |
|---|---|---|
| 文本事件 | 已接入 interim/final/preflight | partial 是 `text + stash`；final 是 `transcript` |
| 情绪 | 无同等实时情绪字段 | emotion 固定开启；在 `transcription.text/completed` 顶层返回 `surprised/neutral/happy/sad/disgusted/angry/fearful` |
| 情绪置信度 | — | 无 emotion confidence，不能单独升级为事实、权限或长期人格 |
| 字句时间戳 | 当前适配器保留时间戳 | 当前 realtime 无字句时间戳（仅有 VAD 音频起止信息） |
| 热词 | 可选热词 | 仅 Fun-ASR/Paraformer 支持热词 |
| 上下文增强 | 可用现有能力 | 仅 `fun-asr-realtime` 稳定版（记录日期 2025-11-07）提供，Qwen 不应假定支持 |
| 轮次控制 | 现有 endpoint/噪声阈值契约 | `server_vad` 默认 800 ms，聊天推荐 400 ms；也支持 manual commit |
| 可靠性 | 已有 reconnect PCM replay、duplicate final 去重 | 需在新适配器中自行补齐 |

Qwen 的 `text + stash` 适合展示稳定前缀，`server_vad`/manual commit 也适合不同端侧轮次策略；但这些是协议优势，不是现成的 Memoria 生产 STT 实现。

## 仓库现状

- `services/agent/src/providers/funasr_stt.py` 是完整 LiveKit STT：包含 interim/final/preflight、时间戳、重连 PCM replay、duplicate final 处理，以及可选热词、上下文和噪声阈值。
- `services/agent/src/providers/qwen_emotion_asr.py` 只是非阻塞 sidecar：队列满时丢旧音频，断线不回放，只解析 `completed` 情绪，不发布 LiveKit transcript。

因此，直接切换会把“情绪试验组件”误当成“权威转写组件”，并丢失当前主链已有的实际契约。Qwen 情绪应继续作为受 generation/turn fence 约束的 `emotion_observation`，不能驱动主人身份、工具权限或长期记忆写入。

## 建议的迁移门槛

1. 先实现完整 `QwenASRSTT`，保留 FunASR 可热切回滚；Qwen 情绪 sidecar 不与权威 transcript 混用。
2. Shadow A/B 使用同一份经过 AEC 的 PCM，比较短控制词、专名、中英混说、方言和连续追问；记录 interim/final/endpoint 的 P95，并检查跨轮污染。
3. 注入断线、迟到事件、空音频、重复 final、server-VAD 与 manual-commit 两种模式，验证 replay、去重和 generation fence。
4. 只有在准确性、P95 延迟、断线恢复和跨轮隔离均达到现有 FunASR 基线后，才做小流量 canary；否则维持 FunASR 主转写 + Qwen sidecar。

**最终建议：不为情绪直接替换；当前组合合理。单 Qwen 流只能作为经过完整适配和 shadow/A-B 验证后的后续实验路线。**
