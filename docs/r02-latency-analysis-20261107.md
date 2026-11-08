# R02 真机记录的延迟归因分析

这份报告回答一个问题。之前的 R02 真机延迟主要来自设备，还是来自收音与 ASR、云端 API、服务逻辑和网络。

结论是，回复开口和大部分停止延迟由服务端和云端链路主导。设备播放下发和停止后的回听收口只占较小部分。收音、VAD、上行和 ASR 前端仍缺少分段日志，因此本报告不能把无回复和误识别全部归因到云端或硬件。

## 范围和数据完整性

两轮测试使用同一候选 `memoria-agent:20261106-card5-committed-boundary-v1`，release commit 为 `b03756b3c5268c90596bd1acda32a7b314094d9a`，系统输出音量为 50。两轮都只运行 suite a 的停止词场景。

第一轮从 09:49 开始。场景期望 80 个标签，原始时间线包含 79 个已进入结果的标签。`t029-stop03-bie-shuo-le-09` 只有 `tool_wait_timeout`，没有 `said`、`reply` 或 `no_reply`。其余结果包含 76 个回复和 3 个 `no_reply`。严格门禁结果为 `incomplete`。

第二轮从 11:13 开始。操作者因周围噪声在 `t057` 后停止测试。原始时间线包含 57 个 `said`、56 个 `reply` 和 1 个 `no_reply`。停止证据中有 43 次确认停止、9 次未停止、2 次证据不足。严格门禁结果为 `incomplete`。

两轮不是交替执行的受控 A/B 基准。下面的 P50 和 P90 是同一候选的两次观测，用于定位链路，不用于宣称稳定的版本回归。分段报告使用插值 P90。原有门禁表继续使用 runner 的最近秩 P90。

## 回复开口的分段结果

时间段来自 `reply-path.md` 的日志关联。`final` 是 bridge 看到的 ASR final。`first_frame_sent` 是服务端发出首帧的时间。`dev_tick` 是设备串口的首个可播放帧时间。

| 分段 | 09:49 轮次 | 11:13 轮次 | 解释 |
| --- | ---: | ---: | --- |
| 说完到 ASR final | P50 0.38 s，P90 0.83 s，n=76 | P50 0.42 s，P90 0.82 s，n=55 | 跨 Mac 与 bridge 时钟，未拆出收音、VAD、上行和 ASR 服务 |
| ASR final 到 commit 完成 | P50 0.85 s，P90 1.28 s，n=76 | P50 0.87 s，P90 2.27 s，n=55 | 包含语义等待、分类器和提交准备 |
| commit 到服务端首帧 | P50 0.71 s，P90 1.46 s，n=76 | P50 0.96 s，P90 1.76 s，n=55 | 主要包含 speculative reply 命中情况、LLM 和 TTS |
| 服务端首帧到设备首帧 | P50 0.014 s，P90 0.030 s，n=76 | P50 0.018 s，P90 0.038 s，n=55 | 设备 tick 修正后的下发时间 |
| ASR final 到服务端首帧 | P50 1.59 s，P90 2.60 s，n=76 | P50 1.75 s，P90 3.47 s，n=55 | 服务端处理段占 Mac 回复时延的主要部分 |

服务端已经发出首帧后，设备在两轮中都只需要约 14 到 18 ms 的 P50 接收时间。这个结果直接排除了“USB 下发或设备播放启动占据约 1.5 秒”的解释。设备仍可能影响收音，但当前日志无法证明它是回复开口延迟的主因。

speculative reply 的时间差异也出现在服务端首帧分段中。保存的分段结果里，带有 speculative trace 的样本更快，缺少对应 trace 的样本更慢。独立复核确认日志里的字面 `outcome=none` 数量为 0，因此“未命中”只能表示没有找到匹配 trace，不能当作供应商返回了一个 none 结果。这个差异可以作为排查线索，不能作为严格的 provider A/B 结论。

两轮 bridge 都记录了 `response_plan_cached reason=timeout fallback=True`，次数分别为 118 和 80。日志没有 `/v1/interaction/response-plan` 的成功响应。这个问题会让回复使用 `local-safe-fallback-v1`，属于回复质量和计划完整性问题。现有时间戳不能证明它是首帧延迟的主要来源，报告不把它计入云端主因。

## 停止链路的分段结果

停止链路以设备 tick 修正后的时间为主。`preempt` 是服务端停止当前播放的时间。`device returned` 是设备回到 listening 的时间。

| 停止路径 | 分段 | 09:49 轮次 | 11:13 轮次 |
| --- | --- | ---: | ---: |
| `media_voice_stop` | 说完到 ASR final | P50 0.61 s，P90 1.04 s，n=31 | P50 0.34 s，P90 0.48 s，n=25 |
| `media_voice_stop` | ASR final 到 preempt | P50 0.012 s，P90 0.045 s | P50 0.011 s，P90 0.036 s |
| `media_voice_stop` | preempt 到设备回 listening | P50 0.138 s，P90 0.185 s | P50 0.173 s，P90 0.192 s |
| `media_voice_stop` | 说完到设备回 listening | P50 0.72 s，P90 1.17 s | P50 0.51 s，P90 0.67 s |
| `media_content_barge_in` | 说完到 ASR final | P50 0.45 s，P90 0.94 s，n=37 | P50 0.59 s，P90 1.91 s，n=18 |
| `media_content_barge_in` | ASR final 到 preempt | P50 0.739 s，P90 0.978 s | P50 0.737 s，P90 1.008 s |
| `media_content_barge_in` | `conversation_close` 等待 | P50 0.766 s，P90 0.962 s，n=33 | P50 0.735 s，P90 0.978 s，n=16 |
| `media_content_barge_in` | preempt 到设备回 listening | P50 0.108 s，P90 0.181 s | P50 0.166 s，P90 0.201 s |
| `media_content_barge_in` | 说完到设备回 listening | P50 1.34 s，P90 1.79 s | P50 1.59 s，P90 2.92 s |

快速的 `media_voice_stop` 路径在第二轮达到 P90 0.67 s。它已经接近并低于 0.8 秒观测线。长延迟主要出现在 `media_content_barge_in`。该路径在 ASR final 到 preempt 之间等待 `conversation_close` 分类器，设备收口只有约 0.1 到 0.2 秒。

当前 bridge 日志还记录了 `close intent semantic classifier unavailable`、`TimeoutError` 和最长约 1.2 秒的 `conversation_close` 等待。第一轮 edge 日志有一次 WSS `close_code=1005`。这些是云端或网络链路的直接异常证据。它们不能证明每次未停止都由网络造成，但足以说明停止路径存在独立于设备播放的云端等待。

两轮共有 145 次完成的 `conversation_close` 分类调用，记录的 verdict 都是 `False`。因此这段等待是停止延迟的成本，却不是后续回复重新出现的充分解释。后续回复需要继续按 turn fence、delivery id 和 provider task epoch 追踪。

## 停止收据和实际静音必须分开

独立复核发现，现有 `voice_soak_evidence.py` 只检查当前 generation 的 stop receipt，没有检查设备回到 listening 后是否又启动了新的回复。因此“`stopped=true`”不能直接等同于用户听到的静音。

在第一轮中，37 个 `media_content_barge_in` 样本有 35 个在下一条测试音频开始前再次发出回复。第二轮的 18 个 content-path 样本全部出现同样的后续回复。两轮合计 53/55。31 个第一轮 lexical stop 和 25 个第二轮 lexical stop 没有在下一条测试音频开始前出现后续首帧。

这些后续回复的首帧通常在设备回 listening 后约 1.6 到 1.7 秒出现。第一轮 P50 回复时长为 16.6 秒，第二轮为 12.4 秒。第一轮有 8 个样本在下一条测试音频开始时仍在播放，第二轮也有 8 个样本仍在播放。它们会污染后续 turn 的 ASR、提交和回复关联。

因此两轮合计 111 个同代 stop receipt 不能作为 111 个用户可听静音结果。离线实现现已把这两个概念分开：`audit_interruptions()` 保留 `stopped`，并附加 `silence_verdict`、fence、后续 delivery 和延迟字段；R02 suite a 的 gate 只接受 `silence_verdict=silent`。对已有记录的 v3 重算为 56 个 `silent`、54 个 `reply_resumed`、1 个 `unverified`，另有 19 个没有有效 stop receipt 的样本标为 `not_applicable`。其中 09:49 轮为 31/37 静默、37/37 后续回复；11:13 轮为 25/43 静默、17 个后续回复、1 个后续下发缺少设备首帧收据。这里的“静默”只表示在下一条 `said` 的结束时间或尾窗观察点前没有匹配到后续回复，不等于声学录音证明。

这条证据把问题拆成两部分。设备停止当前音频的尾段仍然很短。服务端在 stop receipt 后重新提交或重新播报，才是 content-path 没有保持静音的主要问题。content final 的文本长度 P50 约 30，ASR 时间跨度 P50 约 8.7 秒，而 lexical final 的文本长度 P50 为 4 到 5，时间跨度约 1.5 秒。日志没有 final 文本，所以“回复回声与 stop 词合并”仍是推断。

## ASR 和无回复问题

ASR 前端是当前归因中最重要的未知段。两轮都有 `ASR tail timeout`、`empty+vendor_silent` 和空输入丢弃。第一轮有 12 次 ASR tail timeout 和 3 次 `no_reply`。第二轮有 4 次 ASR tail timeout、1 次 `final_result_timeout` 和 1 次 `no_reply`。

这些日志能证明 bridge 没有拿到可提交的 ASR final。它们不能单独证明是麦克风、设备 VAD、USB 上行、网络，还是供应商 ASR 超时。当前日志缺少设备端 VAD 起止、首个上行音频帧、ASR 请求开始、首个 partial 和 final 到达时间。

第一轮还出现一次设备串口断开和一次 edge WSS 读关闭。第二轮的 `t011` 无回复后，`t012` 出现约 22 秒的回复时延，`t016` 出现负的 -0.17 秒时延。这些样本与前后 turn fence 交叉，属于 harness 或旧状态关联问题，不能放进正常的云端延迟分布。

16 个未停止样本的可见类别如下。7 个是 `停一下` 被识别为 `听一下`。4 个是其他 ASR 替换。1 个没有 ASR final。1 个被 echo guard 丢弃。1 个 final 因 `straddles_committed_without_timing` 被拒绝。1 个被前一条 post-stop 回复合并。1 个是 harness 跟随 superseded generation 产生的假失败。ASR 替换是已测事实，收音、AEC、DTLN 和供应商之间的责任仍未分开。

当前第二轮的 `t056` 是停止延迟异常值。它从打断素材起点到设备回 listening 需要 10.779 秒，从说完到设备回 listening 需要 9.384 秒。日志显示该轮发生了长回复和任务取消。这个样本需要单独按 delivery id 和 provider task epoch 复核，不能把它当成硬件 P90。

## 归因矩阵

| 观察 | 已测事实 | 当前判断 | 证据强度 |
| --- | --- | --- | --- |
| 首帧下发到设备首帧约 14 到 18 ms P50 | 两轮串口 tick 与 bridge 首帧可关联 | 设备播放下发不是主要回复开口瓶颈 | 高 |
| `voice_stop` 的 final 到 preempt 约 11 到 12 ms P50 | bridge 有同代 stop receipt | 快速停止路径的服务端停止动作很快 | 高 |
| `content_barge_in` 等待分类器约 0.74 到 0.77 s P50 | bridge 有 classifier wait 和 DeepSeek 请求 | 云端分类路径主导该类停止延迟 | 高 |
| content-path stop 后又出现新的回复 | 53/55 个 content-path 样本在下一条测试音频前再次发出首帧 | 当前 stop gate 高估了用户可听的静音成功率 | 高 |
| speculative 未命中时首帧更慢 | LLM token、TTS session 和首帧日志均存在 | LLM、TTS 或其网络等待主导这部分差异 | 中高 |
| ASR tail timeout 和 vendor silent | bridge 明确记录空输入和 provider 状态 | ASR 或其前面的收音链路存在问题 | 中 |
| 未停止、误识别和 no_reply | 时间线和 bridge 有样本记录 | 需要设备 VAD、上行帧和 ASR 分段才能继续归因 | 低到中 |

## 后续验证顺序

先做离线对照，不重新占用设备。

1. 将已经保存的用户 AIFF 直接送入 ASR 服务，记录请求开始、首个 partial、final 和响应结束。这个对照可以去掉麦克风和设备 VAD。
2. 在没有设备播放的条件下，重复测停止词 ASR。这个对照可以观察 `停一下`、`够了` 等词的识别和 final 延迟。
3. 为实时链路补齐设备 VAD 起止、首个上行帧、ASR 请求和 partial、final 时间。所有日志使用同一时钟握手。
4. 为 `conversation_close`、LLM 首 token 和 TTS 首 PCM 记录 request id、开始时间、结束时间、超时和重试次数。
5. 使用已修正的 stop gate。它会检查 stop receipt 后的后续首帧和下一条测试音频的 fence，再跑完整 80 个标签。先确认 `timeline` 没有 `tool_wait_timeout`、缺标签或 no reply，再读取每个词的 P90；0.8 秒继续作为记录指标。

0.8 秒继续作为记录指标。当前证据不支持把它当作单一硬件上限，也不支持在没有 partial ASR 停止路径前承诺达到它。

## 收据和复核命令

原始收据位于 [第一轮输出](../outputs/acceptance/run-20261107-094902-r02-stop/) 和 [第二轮输出](../outputs/acceptance/run-20261107-111318-r02-stop/)。分段结果位于 [第一轮回复分析](../devicetest/r02-20261107-094902/analysis-a-deep.md)、[第二轮回复分析](../devicetest/r02-20261107-111318/analysis-a.md)、[第二轮停止分析](../devicetest/r02-20261107-111318/stop-path.md) 和 [第二轮 LLM/TTS 分析](../devicetest/r02-20261107-111318/llm-tts.md)。

官方离线脚本是 `scripts/voice_soak_evidence.py`、`scripts/voice_soak_analyze.py`、`scripts/voice_soak_verdict.py` 和 `scripts/voice_soak_session_report.py`。这些脚本读本地收据，`voice_soak_analyze.py` 在能识别唯一 session 时还会只读查询归档。停止证据报告使用 `voice-soak-stop-v3`；通用 verdict 只有在 manifest 设置 `require_post_stop_silence=true` 时才把 `reply_resumed` 或静默证据不足判为失败，避免影响允许继续回答的场景。

本次重算使用以下只读命令，结果保存在两轮 `devicetest` 目录的 `evidence-a-v3.json`、`gates-a-v3.json` 和 `gates-a-v3.md`。

```bash
uv run --no-project python scripts/voice_soak_evidence.py \
  outputs/acceptance/run-20261107-094902-r02-stop \
  > devicetest/r02-20261107-094902/evidence-a-v3.json
uv run --no-project python -m devicetest.r02_gates \
  outputs/acceptance/run-20261107-094902-r02-stop --suite a \
  --out devicetest/r02-20261107-094902/gates-a-v3.md \
  --json-out devicetest/r02-20261107-094902/gates-a-v3.json
```

## 第三轮真机窗口：14:28 suite a

第三轮使用同一候选 `memoria-agent:20261106-card5-committed-boundary-v1`，revision `b03756b3c5268c90596bd1acda32a7b314094d9a`，Mac 输出音量 50。80 个样本全部有 reply，未出现 `no_reply`。尾窗观察 45.01 秒后仍是串口 `listening`，没有 `idle`，所以尾窗失败。音量恢复到 30，串口已释放。

`analysis-a-v3.md` 的归档文本列为空，这是归档 payload 限制，不是 80 次 ASR 全部失败。stop 词证据来自 bridge 的最终判定和时间线收据。

这一轮的停止和静默结果如下。

| 指标 | 数量 |
| --- | ---: |
| `stopped` | 63 |
| `not_stopped` | 16 |
| `unverified` | 1 |
| `silence_verdict=silent` | 58 |
| `silence_verdict=reply_resumed` | 5 |
| `silence_verdict=not_applicable` | 17 |

按设备 tick 和服务器日志拆分后，58 个 `media_voice_stop` 样本的说完到 ASR final 为 P50 440 ms、P90 844 ms，final 到 preempt 为 P50 9 ms、P90 11 ms，preempt 到设备回 listening 为 P50 156 ms、P90 209 ms。5 个 `media_content_barge_in` 样本的 final 到 preempt 为 P50 915 ms、P90 1.058 s，`conversation_close` 等待为 P50 898 ms、P90 1.049 s。回复路径的 commit 到首帧为 P50 885 ms、P90 1.310 s，首帧下发到设备首帧为 P50 19 ms、P90 30 ms。回复开口总时延的 Mac 口径为 P50 2.225 s、P90 2.726 s，最大 3.350 s。

设备 VAD end 到 ASR 边界的增量为 P50 206 ms、P90 378 ms，设备 VAD 尾部约 900 ms，样本数为 75。`final_to_done` 为 P50 716 ms、P90 1.015 s。上述数字分开了设备 tick、服务器时间和跨时钟口径，不能把它们相加成声学停止时刻。

22 个问题样本的证据分类为 13 个 `not_stop_word`，其中包含 7 个“听一下”；2 个 final 带前置或累积文本；1 个 uplink 中断后没有 stop final；1 个设备 `pat` 先停止播放但缺少 spoken-stop 收据；5 个已有 stop receipt 的样本在下一条 fence 前重新回复。`t044` 到 `t047` 的日志显示设备 VAD 长时间抑制，后续 final 文本长度持续累加。`t014` 只有长度变化，不能还原原文。自然 `playback_completed` 是这些问题的结果，不是独立的根因类别。

本轮 bridge 有 6 条 traceback，来自 3 次 close-intent classifier 在约 1.2 秒上限超时。DeepSeek 请求 189/189 返回 HTTP 200，`conversation_close` 的 verdict 全为 false。ASR 有 1 次 `ASR tail timeout` 和 `vendor_silent`，1 次 final rejected，`t059` 后有 1 次 provider error 与重连。TTS 没有错误行，设备 reset 为 0 次，edge 全部为 INFO。没有 PCM，因此 AEC、回声、麦克风采集质量和 vendor 对替换结果的责任仍未知。

这一轮支持的判断是，服务器 stop dispatch 和下发播放不是主要硬件瓶颈，ASR final、设备 VAD 边界和部分 server classifier 等待占主要时间。它不能单独证明声学根因。下一步先做同一 stop AIFF 与同场景 uplink PCM 的 ASR 对照，再回放 VAD 边界被抑制的累计文本样本。原始收据在 [run-20261107-142730](../outputs/acceptance/run-20261107-142730-r02-stop/)，独立复核线程在 <t3-thread://v2/f174036b-268b-4bc8-a7e3-86e2e702f97f/thread:delegated-task:command%3Amcp%3Acfd64b80-1d0e-4da9-9b98-2b20a96b1389%3Adelegate-task%3A012963dd-3a01-4c50-9ef7-f6ee85c9934c>。

## 第三轮 stop 素材的离线 SenseVoice 对照

为去掉设备捕获、VAD 和上行链路，先把第三轮保存的 80 个 `*-stop.aiff` 转为 16 kHz、单声道、16-bit PCM WAV，再送入同一 SenseVoice sidecar。对照使用的是素材文件，不是本轮设备实际送出的 PCM。

80 个文件与 manifest 的 80 个 stop 标签一一对应。`stop01` 到 `stop08` 各有 10 次记录，但每组的 10 个文件字节相同，因此这是 8 个 TTS 素材各重复 10 次，不是 80 个独立声学样本。素材使用 macOS `Tingting`，速率为 165。

官方评测脚本的结果如下。

| 项目 | 结果 |
| --- | ---: |
| 请求完成 | 80/80 |
| 与期望词一致 | 70/80 |
| `停一下` 一致 | 0/10 |
| 其他 7 个 stop 素材一致 | 70/70 |
| 原始素材解码延迟 | P50 0.207 s，最大 0.391 s |
| 降低 20 dB 后与原始文本相似度 | 1.000 |
| 8 倍增益削波后与原始文本相似度 | 1.000 |
| 并发 4 路结果与串行一致 | 16/16 |

`停一下` 的 10 个素材全部被离线 SenseVoice 识别为 `听一下。`。bridge 日志中可还原文本的 9 个 stop02 final 也都以 `听` 开头，其中 7 个是 `听一下。`，另有 `听一生。` 和 `听音乐。` 各 1 个。这个结果支持一个受限判断。`停一下` 的错误不是设备链路独有的，它可以在同一 TTS 素材的干净文件上被另一个 ASR 引擎稳定复现。

这项对照还不能区分 TTS 发音和 ASR 语言模型偏置，也不能证明生产 Volc ASR 会对干净文件给出相同结果。其他词提供了反证。bridge 中的 `停`、`好了好了` 和 `等一下` 曾出现 `行`、带前置或累积文本、`等一等` 和 `有音响`，但 SenseVoice 对相应干净素材全部识别正确。这些错误仍可能来自实时输入、DTLN、上行或生产 ASR。

本次 sidecar 请求 80/80 返回 HTTP 200，但“返回文本”不等于“识别正确”。静音、低强度噪声和高强度噪声探针都返回 2 个字符。该现象与既有 SenseVoice 幻觉记录一致，当前评测脚本只统计原始字符数，不能把 `nonempty_rate=1.0` 当作静音拒绝能力。调用的容器镜像为 `memoria-sensevoice-asr:20260901-pin-language`，image ID 为 `sha256:a37329e75ac58f4250b797f6df3cfe6383e2ab55dbffff6af5faaf6adecfe0bc`；模型文件 `model.int8.onnx` 的 SHA-256 为 `c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51`。

逐条结果和评测收据保存在 [sensevoice-stop-compare.json](../outputs/acceptance/run-20261107-142730-r02-stop/sensevoice-stop-compare.json) 和 [sensevoice-evaluate-all.json](../outputs/acceptance/run-20261107-142730-r02-stop/sensevoice-evaluate-all.json)。本次没有得到 uplink 或 post-AFE PCM，因此仍不能判断设备捕获、AEC、DTLN、传输和生产 ASR 各自的责任。

下一次安静环境真机运行需要开启 development-only 的 `MEDIA_PCM_TAP_DIR`。代码在 DTLN 完成后调用 `MediaPcmTap.write()`，所以生成的是 bridge 送往 ASR provider 的 admitted PCM。只读 preflight 已确认当前线上 bridge 使用同一候选、健康且重启次数为 0，但 `ENVIRONMENT=production`，`MEDIA_PCM_TAP_DIR` 为空；生产 compose 也明确把该变量置空。向 `/etc/memoria-agent.env` 加变量不会生效，生产环境也会拒绝带 tap 的配置。下一次采集需要单独准备一次 development 环境的候选 bridge 或 override，这会改变运行配置，当前没有启动。

只有在该 development bridge 获得授权并写出 PCM 后，才按 stop 窗口与原始素材做相关和时延对齐，再用同一个 SenseVoice 解码两路音频。这样可以先确认实时输入是否已经变形，再决定是否需要付费调用生产 Volc ASR 做同素材对照。当前本地设备串口无 owner，系统音量为 30 且未静音，没有 R02 runner 或 SenseVoice 测试进程。

## 2026-11-07 development admitted-PCM tap diagnostic

After the preflight, one authorized development-only override enabled `MEDIA_PCM_TAP_DIR=/tmp/r02-pcm-tap` on the same healthy bridge candidate. The override changed the bridge environment to `development` for this diagnostic only. It did not change the image or revision, and the original production Compose chain was restored after the run. The final bridge was healthy with `ENVIRONMENT=production` and an empty tap directory. The remote temporary override was removed.

The diagnostic used eight stop attempts, one for each stop word, at system volume 30. It is a comparison run, not an R02 gate. The device produced 4 stop receipts, 3 `not_stopped` results, and 1 `unverified` result. The device port was released after the run.

The tap file was `outputs/acceptance/run-20261107-161500-r02-pcm-tap/pcm-tap/media-uplink-0aac57d5-e812-4914-a275-aac7163fddf8-epoch3151.wav`. It is 6,521,004 bytes of 16 kHz, mono, signed 16-bit PCM and covers 203.78 seconds. The tap is after DTLN and before the ASR provider. It is not raw microphone PCM and does not expose a pre-AEC or post-AEC branch.

The same SenseVoice sidecar decoded the eight clean stop materials as follows.

| Expected material | SenseVoice result |
| --- | --- |
| 停 | 停。 |
| 停一下 | 听一下。 |
| 别说了 | 别说了。 |
| 不要说了 | 不要说了。 |
| 好了好了 | 好了好了。 |
| 够了 | 够了。 |
| 安静 | 安静。 |
| 等一下 | 等一下。 |

The admitted tap was split by the same energy detector into 39 segments. SenseVoice returned at least two characters for 30 segments. The approximate window alignment used the bridge ASR boundary samples and had an intercept spread of about 0.32 seconds. Near the eight interrupt windows it returned `听你讲`, `别说了`, `不要说呢`, `好了好了`, `嗯了`, `安定`, and `等一下`; the `停` window had only short fragments near the estimated window. The tap also contained prompt speech, reply echo, punctuation-only segments, and short filler-like outputs.

This comparison narrows the hypothesis space. `停一下` is not a failure unique to the device path because the clean Tingting material becomes `听一下。` in the independent SenseVoice run. That result still cannot distinguish TTS pronunciation from SenseVoice language-model bias, and it does not predict the production Volc ASR result. The admitted PCM also differs from the clean material for several other words. `不要说了` became `不要说呢`, and `安静` became `安定` in the aligned tap windows, while `别说了`, `好了好了`, and `等一下` were recovered. This is evidence that the live provider input contains altered or mixed speech in some windows. It does not identify whether capture, AEC, DTLN, uplink framing, or the production ASR caused each alteration.

No paid Volc ASR comparison was made. The next useful control is a pre-AEC or post-AEC PCM tap, or an equivalent device-side capture, using the same eight materials. Until that branch exists, this run supports a provider-input diagnosis and cannot support a hardware or AEC root-cause claim. The full receipt and offline outputs are in [run-20261107-161500-r02-pcm-tap](../outputs/acceptance/run-20261107-161500-r02-pcm-tap/), including `sensevoice-clean-evaluate.json`, `sensevoice-tap-evaluate.json`, `evidence-v3.json`, and `pcm-tap-analysis.txt`.

## 2026-11-07 Step 0 historical bench PCM re-analysis

Before requesting a new bench flash, the retained 2026-10-13 serial journals were re-exported offline. This step used no device, no production configuration, and no new audio. Three intact journals were recovered from the retest logs: play-only `0000c02a`, near-end-only `0001275a`, and duplex `000315be`. The damaged duplex journal `0001ee1d` was excluded because one PCMD chunk is missing.

The same SenseVoice sidecar decoded the near-end-only `pre_afe_mic` and `post_afe` recordings as `别说了。`. In the intact duplex window, the `pre_afe_mic` energy segment decoded as `天吗？我没太听清后面那句`, and the `post_afe` file produced no segment under the evaluator's energy detector. The lane analysis reports duplex stop correlation of `0.172` at `pre_afe_mic` and `-0.194` at `post_afe`, with `pre_afe_mic` at -32.01 dBFS and `post_afe` at -55.89 dBFS. The play reference still correlated with play at 0.99, and the reported mic-to-post ERLE was 23.94 dB.

This is historical build 24 evidence at volume 50, not a build 28 acceptance result. It shows that the same stop material can pass the AFE in quiet listening and lose intelligibility during playback. It narrows the older failure toward the AFE double-talk path, NLP or NS, or reference timing and gain. It cannot separate those causes, and it cannot establish the current build 28 behavior.

The re-exported WAVs and the SenseVoice result are in [the Step 0 receipt](../outputs/acceptance/run-20261013-triwindow-retest/step0-reexport/), with the source lane metrics in `../outputs/acceptance/run-20261013-triwindow-retest/lane-analysis.json`. A current-build bench capture is still required before changing any stop rule or attributing the present R02 failures to hardware.

## Current build 28 bench PCM diagnostic

The authorized current-build run is recorded in [the bench PCM report](r02-bench-pcm-analysis-20261107.md). It captured 13 intact three-second journals on the bench image, including a near-end-only stop-clip control, play-only, eight duplex stop materials, and three `停一下` repeats. The near-end-only control was not a pure-noise window. The journal verifier reported zero drops and zero cuts for every sample.

The current build carried playback-level energy in `pre_afe_ref`. The play-only control reduced `pre_afe_mic=-38.32 dBFS` to `post_afe=-68.09 dBFS`. In duplex, the median was `pre_afe_mic=-38.64 dBFS` and `post_afe=-47.07 dBFS`. Stop-stimulus NCC was `0.144` to mic and `0.167` to post. Those values overlap the near-end-only control at `0.157` for both lanes, but that control was not a pure-noise window. The current Mac volume was 30, so this run cannot establish a noise threshold or word intelligibility.

This run proves the build 28 recorder, playback lane, and reference lane are live. It does not separate capture, AEC, NS, DTLN, uplink, production ASR, or hardware. It leaves the 0.8-second value as a recorded metric and leaves the acoustic root cause open. The device was restored from the pre-flash app backup and the protected-region MD5 values returned to their pre-flash values.

The planned volume-50 follow-up did not produce a valid journal. The bench device entered deep sleep before the first planned window completed, and USB disappeared. The partial run is recorded separately as an operational failure. It does not add a volume-50 acoustic measurement or change the current root-cause boundary.

The later [keep-awake bench receipt](../outputs/acceptance/run-20261107-230946-r02-bench-keep-awake/) removed this operational failure for one run. The combined profile kept USB present at `up_ms=221907` with `screen_off=1 sleeping=0`, and the serial log had zero `deep_sleep` lines. It produced 13 of 13 intact journals. The output volume was operator-controlled and recorded per case, with observed values 25 and 50, so the run is not a fixed-volume acoustic comparison. The new receipt confirms the recorder and lane evidence path, but it does not identify a hardware, AEC, NS, DTLN, uplink, or production-ASR root cause.

The diagnostic switched back to the build 28 product app after capture. The pre-flash `ota_0` was a keep-awake image, so this operation changed the app image rather than restoring the pre-flash bytes. Boot and partition table, `otadata`, PHY, identity, and assets matched their pre-flash MD5 values. `ota_0` differed because the product app was written. NVS also differed between the before and after snapshots, and the available records cannot assign that change to the bench run or product boot. The 0.8-second metric remains report-only.
