# 2026-10-09 N-8 修前基线（电脑模拟，音量 30）

收据（git 忽略，在本机 `outputs/`）：`outputs/acceptance/run-20261009-n8-baseline/`（`bridge.log`、`timeline.jsonl`、`serial.log`、剧本与素材清单）与串口原始记录 `outputs/serial/robot-20261009-n8-baseline.log`。

## 结论

1. 22 步里 20 步有回复，2 步没有：t010「讲一个很短的笑话吧。」和 t011「你给我出一个谜语吧。」，连着丢，都在 12 句中性问题里。
2. 这两次丢失不是 PR #197 修的那一类。#197 修的是「首帧已发出、设备还没播出，被没有文字的用户回合取代」（`emitted_audio=True`），本轮一次都没有出现；两次 `status=aborted reason=superseded` 的 `emitted_audio` 都是 False（开场问候和 t010）。所以这份基线不是 #197 的前后对比，只给整体丢失率、延迟与打断的参照。
3. t010 的机制已查清并在测试夹具里复现（`services/agent/tests/unit/test_media_session_reply_cut_by_wordless_turn.py`，日志特征与线上一致）：识别器只把这句话开头 320 ms（3 字）定为终稿，其余是始终没有终稿的临时识别文字；回合以 3 字提交、回复备好之后，这些临时文字（5 字）才出现在待定回合里。2.6 s 后一次噪声 VAD 起点到达，19 ms 后待定回合已经带着这 5 字：判断「这个边缘有没有话」的 `_pending_turn_has_text_evidence` 不看文字对应的音频位置，把它们算成边缘的话，回复在首帧之前被取代（`prepared reply not held … partial_chars=5`），临时文字再没有终稿，问题无人回答。t011 是另一条路径：识别器的终稿被 `cross_sentence_overlap` 拒绝，挡住它的区间没有记日志，没查清，与 t010 没有已证实的关联。
4. 打断十步没有一步真的停下了回复。服务端日志里这十个回复的结局都是 `playback_ended / playback_completed`（自然播完，完整音频发出），整轮 `playback-stop taken` 为 0（`not taken` 79 次）。驱动记的「停下」只是板子在打断素材之后的 8–10 s 内回到了聆听，回复自己播完也满足它（t019 的 −0.47 s 就是这样露出来的）；此前这里写的「6 步有效停下、停止延迟 p50 3.95 s」作废。音量 30 下电脑播的打断素材压不过机器人自己的声音，口头打断在这个音量下不起作用（与旧记录「口头停要约 50」一致）。
5. 识别质量差，上行编码队列整轮在丢帧；二者的因果不成立：复测会话（`docs/acceptance/run-20261009-n8-after/findings.md`）上行几乎不丢帧，识别质量没变。

## 条件

- 时间：2026-10-09 10:49:27 起（北京时间），10:49:52 USB 唤醒（一次成功，状态 `listening`），22 步，10:56:54 结束，退出码 0。
- 板子：产品固件 build 24。
- 服务端：整栈 `20261007-n8-diag-v1`（2026-10-08 14:14 切流；#197 在本轮进行中的 10:50:49 合并，但直到 14:59 才发布，线上一直是它）。
- 音量：驱动把 Mac 音量设为 30，结束后恢复到 56。
- 场景：12 句中性问题（`voice_soak_scenarios/child.json` 里的 hello、day、why-sky、why-rain、moon-follow、follow-1、dino、math、english、story-short、riddle、sing，与 10-08 一轮相同），加两轮 5 步打断（story-stop「别说了。」、long-stop「停。」、new-question-mid、backchannel「嗯。」、early-mid）；素材是生产 Doubao 音色渲染的。没有注入噪声。

## 中性 12 句

| 句 | 原话 | 服务器听到的（相似度） | 说完到开口 |
|---|---|---|---:|
| t001 | 你好呀，你叫什么名字？ | 逐字（1.00） | 1.90 s |
| t002 | 我今天在学校画了一幅画，画的是一只小狗。 | 无匹配的最终文本 | 13.80 s |
| t003 | 天空为什么是蓝色的？ | 「为什么是？」（0.62） | 1.99 s |
| t004 | 为什么会下雨呀？ | 「为什么会这样？」（0.62） | 2.72 s |
| t005 | 月亮为什么老跟着我走？ | 「跟着我走。」（0.57） | 3.06 s |
| t006 | 那晚上为什么是黑的呢？ | 「那马鲛为什么是黑的呢？」（0.80） | 2.95 s |
| t007 | 恐龙都吃什么东西？ | 「你们都是什么东西？」（0.62），回复变成自我介绍 | 3.48 s |
| t008 | 七加八等于多少？ | 「1+8等于多少？」（0.57） | 3.08 s |
| t009 | 苹果用英语怎么说？ | 「he. 苹果用英语怎么说？」（0.89） | 6.04 s（被扣住 3.0 s） |
| t010 | 讲一个很短的笑话吧。 | 对不上 | **无回复** |
| t011 | 你给我出一个谜语吧。 | 无匹配的最终文本 | **无回复** |
| t012 | 你会唱歌吗？ | 逐字（1.00） | 3.11 s |

有回复的 10 句里，说完到开口 p50 3.07 s（只算无打断的回复）。

## 打断 10 步（驱动记录 vs 实际结局）

| 步 | 在放的内容 | 打断说的 | 驱动记录 | 实际结局（服务端日志） |
|---|---|---|---|---|
| t013 | 讲故事 | 别说了。 | 「停」7.69 s | 自然播完（`playback_completed`） |
| t014 | 介绍大熊猫 | 停。 | 「停」4.42 s | 自然播完 |
| t015 | 讲太阳系 | 等一下，我想问另一个问题，月亮为什么会变圆变弯？ | 「停」1.79 s | 自然播完 |
| t016 | 讲长颈鹿 | 嗯。 | 没停 | 自然播完 |
| t017 | 讲海洋 | 等一下，我想问另一个问题，云是怎么变出来的？ | 「停」4.51 s | 自然播完 |
| t018 | 讲故事 | 别说了。 | 没停 | 自然播完（27.25 s） |
| t019 | 介绍大熊猫 | 停。 | 「停」−0.47 s | 自然播完 |
| t020 | 讲太阳系 | 等一下，我想问另一个问题，月亮为什么会变圆变弯？ | 「停」1.04 s | 自然播完 |
| t021 | 讲长颈鹿 | 嗯。 | 没停 | 自然播完 |
| t022 | 讲海洋 | 等一下，我想问另一个问题，云是怎么变出来的？ | 「停」3.47 s | 自然播完 |

驱动（`scripts/voice_soak.py`）的「停」是：板子在打断素材开始后的 settle 秒内回到了聆听或空闲。回复自己播完也满足，所以长回复（讲故事）记「没停」，短一些的回复记「停」，停止延迟其实是「自然播完的时刻减去素材结束的时刻」；t019 的 −0.47 s（板子在「停。」播完之前就回到聆听）把这个假象露了出来。真正的判据是服务端日志里该回复的结局：这十个都是 `playback_ended / playback_completed`，下行音频完整发出（例如 t013 的复测对应轮 `frames=1143 audio_ms=22860 wall_ms=22869`）。

## 日志要点（北京时间；板子与服务端时钟差约 60 ms）

**t010（说话 10:52:07.4–10:52:10.3）**

- 10:52:09.418 FunASR 最终结果 `text_len=3`；10:52:09.434 `media pending turn split after reply … boundary=playback_end` 与 `media playback-followup endpoint … text_len=3`。
- 10:52:10.814 提交 turn 12（`text_len=3`），回复备好（`prepare_ms=172`）。
- 10:52:11.947 `media vad start admitted`（`vad_revision=29`）。串口 10:52:12.006 `Device VAD start … rms=0.0006`，是这句话放完 1.7 s 之后；这句话放的 3 s 里串口没有任何 `Device VAD start`。
- 10:52:11.966（VAD 起点后 19 ms）`media prepared reply not held … floor_open=False turn_started=True partial_chars=5 provisional_chars=5`，随即 `status=aborted reason=superseded emitted_audio=False`。有文字证据的新回合，所以走的是「不扣住、直接让位」，不是 #171 的扣住。
- 此后到 t011 开始（10:52:40.3）之间没有回合提交；10:52:35.04、10:52:39.65 各一条 `media turn has no text … stage=endpoint`（`empty_media_turn`），待定回合的起点样本一直是 1954880。
- 机制：这 5 字临时文字是提交（`_clear_pending_turn_state` 会清掉待定回合的临时文字）之后才出现的，所以只可能是这句话没有终稿的尾巴；它们在 VAD 起点之前就在了，不是这个边缘的话。夹具里把同样的次序跑一遍（提交 → 一段没有终稿的临时文字 → 无话的边缘），得到同一串日志：`prepared reply not held … partial_chars=3 provisional_chars=3`、`terminal=preempted terminal_reason=superseded first_frame_sent=False`。

**t011（说话 10:52:40.3–10:52:43.0）**

- 10:52:42.95 `media ASR result rejected … reason=cross_sentence_overlap is_final=True text_len=5 … samples=2334400-2398400`。
- 其后 10:52:49.0、10:52:52.9 又是两条 `empty_media_turn`，待定回合起点仍是 1954880；10:53:13 脚本判无回复。
- 推断（未证实）：t010 的后半没有形成回合，留下一个起点不变的待定区间；t011 的最终结果因与它重叠被拒绝，所以同一件事连丢两句。证据只有上面这些日志行的时序，没有读码确认 `cross_sentence_overlap` 的判定条件。

**扣住（t009、t014）**

- turn 11（t009）10:51:57.57 `media prepared reply held for a user turn with no words`，10:52:00.59 `released waited_s=3.02`；turn 15（t014）10:53:56.48 扣住，10:53:59.50 `released waited_s=3.01`。开口 6.04 s、6.35 s：两次都是没有文字证据、靠 3.0 s 上限放出。这是 #171 扣住的代价，每次约多等 3 s。

## 识别与上行

- 22 句里 5 句没有匹配到服务器听到的最终文本（t002、t011、t014、t018、t019），其余 17 句平均相似度 0.81、8 句 ≥ 0.9。常见：开头被吃（「天空为什么是蓝色的？」→「为什么是？」）、同音误识（「七加八」→「1+8」）。听错会直接改变回复内容（t007）。
- 串口 `Encode queue is full, dropping oldest frame` 的计数从 1（10:49:50）涨到 2723（10:56:54），约 6.4 帧/秒。10-08 一轮也整段在丢（末计数 1157）。与听错的因果没有证实；先用手头数据回溯，见 TODOLIST N-8「上行编码队列持续丢帧」。
- 日志异常计数（`voice_soak_analyze.py`）：bridge 的 `turn discarded` 2 次（`ASR tail timeout`，`empty+vendor_silent`），`empty_media_turn` 10 次。

## 发布后复测怎么比（已做，结果见 `docs/acceptance/run-20261009-n8-after/findings.md`）

复测内容：35 句（这 12 句中性问题 ×3，第一句是唤醒热身，不计）加一遍同样的 10 步打断，音量 30。比较项：

1. 中性问题无回复数（基线 2/12）。
2. `status=aborted reason=superseded emitted_audio=True` 的次数（基线 0；10-08 一轮有 1 次，t012）。#197 生效的话应一直是 0。
3. 新日志 `media reply spared from a user turn with no words` 出现的次数：每一次都是一个本来会被取消的回复被保住的候选证据，要对上设备确实播出了。
4. 说完到开口 p50（基线 3.07 s）。打断（口头停、提新问题）在音量 30 下无法评估：基线和复测都是 0/8 真停下；要评估须先征得同意把音量提到 50。#197 只改「回复还没被听到时」的取代，已经开始播的回复被真话音打断的路径不变。
5. t010 → t011 这一类连丢不会被 #197 修好；复测里若再出现，是这一类。

## 没验证 / 边界

只有一轮；素材由电脑播放；没有注入噪声；一台设备、一个房间。音量 30 下真话音接近设备 VAD 门限（t010 整句没有 `Device VAD start`）是读到的现象，成因没查。
