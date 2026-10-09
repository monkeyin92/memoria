# 2026-10-09 N-8 发布后真机复测（电脑模拟，音量 30，线上 `20261009-textless-spare-v1`）

收据（git 忽略，在本机 `outputs/`）：`outputs/acceptance/run-20261009-n8-after/`（`bridge.log`、`timeline.jsonl`、`serial.log`、剧本）与串口原始记录 `outputs/serial/robot-20261009-n8-after.log`。对照：[修前基线](../run-20261009-n8-baseline/findings.md)。

## 结论

1. 35 句中性问题（12 句 ×3；第一句 t001 是唤醒后的热身，驱动判 `step_failed`，不计）全部有回复，0 句丢失；基线 12 句丢 2。
2. **这一轮证明不了 #197 起作用**：#197 要修的那一类（首帧之后被没有文字的回合取代）一次都没出现（`media reply spared…` 0、`superseded` 0），t010 那一类也没出现（`prepared reply not held` 0）。0/35 可能是运气或环境：若真实丢失率是 10%，35 句一次不丢的概率约 2.5%，若是 5% 约 17%。本轮上行丢帧也几乎为零（见 4）。要主动撞这一类，需要噪声注入的配方（第十轮 0.6 s、−20 dBFS 粉红噪声；第十八轮音量 50，要先问）。
3. **打断没法评估**：10 个打断步没有一步真的停下了回复（回复结局全是 `playback_completed`，`playback-stop taken` 0 次，`not taken` 175 次），与基线相同；驱动的「停下」判据把自然播完也算进去，基线里的停止延迟数字作废（见基线文档）。
4. **上行丢帧不是听错的主要原因**：本轮串口 `Encode queue is full` 只有 1 行（基线 2,722 帧，约 6.4 帧/秒），识别质量却没变：已匹配句的平均相似度 0.82（基线 0.81），≥ 0.9 的 19/36，没匹配上 9/45（基线 5/22）。两个会话，样本小；为什么有的会话不丢仍未查清。
5. 扣住 3 次（t025、t026、t027 连着，各多等 3.0–3.5 s，都放出并答了）：每次扣住的代价约 3 s。
6. **新发现**：1 次 Doubao TTS `APIConnectionError: empty-timestamps` → t016 的回复没出声（`reply_task_exception`，`emitted_audio=False`，`interrupt_no_yield:provider_failed`），约 28 s 后才与下一句的回复合并说出。没有看到重试。
7. 说完到开口：中性问题 p50 2.8 s、p90 4.8 s（基线 3.07 s、6.04 s）。

## 条件

- 时间：2026-10-09 19:51:02 起（北京时间），19:51:27 USB 唤醒（驱动判 `step_failed: could not wake the device`，状态是 `speaking`，即问候正在播，与 10-08 一轮相同），46 步，20:03:51 结束，退出码 0。
- 板子：产品固件 build 24。服务端：整栈 `20261009-textless-spare-v1`（14:59 切流，只含 #197；t010 那一类的修复在其后的 PR，当时还没发布）。
- 音量：驱动把 Mac 音量设为 30，结束后恢复到 19（运行前就是 19）。
- 场景：与基线完全相同的 22 步（12 句中性问题 + 两轮 5 步打断），后面再接两遍 12 句中性问题；素材同一份 Doubao 渲染的语音库，没有注入噪声。

## 对照

| | 修前基线 | 复测 |
|---|---|---|
| 中性问题（不含 t001 热身） | 12 句，丢 2 | 35 句，丢 0 |
| 说完到开口 p50 / p90 | 3.07 s / 6.04 s | 2.8 s / 4.83 s |
| 扣住（`prepared reply held`）→ 放出 | 2 → 2 | 3 → 3 |
| `prepared reply not held` / `superseded` | 2 / 2（`emitted_audio=False`） | 0 / 0 |
| `media reply spared…`（#197 的新日志） | 0 | 0 |
| 打断步真正停下 | 0/10 | 0/10 |
| `empty_media_turn` / ASR 尾部超时丢弃 | 10 / 2 | 2 / 2 |
| `cross_sentence_overlap` 拒绝 | 2 | 8 |
| 上行 `Encode queue is full` 末计数 | 2,723 | 1 |
| 已匹配句平均相似度 / 没匹配上 | 0.81 / 5 of 22 | 0.82 / 9 of 45 |
| TTS 失败（`reply_task_exception`） | 0 | 1 |

## 没验证 / 边界

只有一轮；素材由电脑播放；没有注入噪声；一台设备、一个房间；音量 30（口头打断在这个音量下不起作用）。`cross_sentence_overlap` 拒绝增加到 8 次，没有逐条看是否伤到了回复。「听到的文字」是按相似度从档案里配对的，配不上不等于没听到。
