# 2026-10-08 N-8 真机诊断一轮（电脑模拟，音量 30）

收据（git 忽略，在本机 `outputs/`）：`outputs/acceptance/run-20261008-n8-diag/`（`bridge.log`、`timeline.jsonl`、`analysis.md`、两段摘录）与串口原始记录 `outputs/serial/robot-20261008-n8-diag.log`。

## 结论

1. 11 句有效输入都被提交并进入回复。其中 10 句完整播放；1 句（t012「你会唱歌吗？」）在首个下行帧到达的同一时刻被一次 VAD 起点取代，输出 0 帧。
2. 这次丢失走的是「已开播之后被取代」路径。诊断行 `media prepared reply dropped cause=` 只覆盖「held 扣住」路径，本轮 `held` 与 `dropped` 都是 0，所以它没有触发，不能用它区分这次的原因。
3. 取代它的是一个没有提交的空回合。触发它的 VAD 起点 rms 为 0.0009（约 −61 dBFS），远低于真人说话的 −21…−27 dBFS。空回合没有可重放的内容，回复随之丢弃。候选修法二（取代它的回合最终是空回合时，重放准备好的回复）与这一例吻合；但 VAD 起点的来源（扬声器开启瞬态、房间底噪、回声残留）本轮没有查清。
4. 另一个需要单独查的异常：上行编码队列从唤醒起整段都在丢帧（`Encode queue is full, dropping oldest frame`，末计数 1157）。11 句里 5 句逐字正确、2 句部分正确、4 句没有对应的最终识别文本。丢帧与听错之间的因果没有证实。

## 条件

- 时间：2026-10-08 18:13:22–18:16:17（北京时间）。
- 板子：产品固件 build 24（无 bench 构建），USB 唤醒（`usb wake accepted wake_mode=button`）。
- 服务端：整栈 `20261007-n8-diag-v1`（2026-10-08 14:14 切流）。
- 音量：驱动在运行期间设为 30，结束后恢复到 38。
- 场景：12 句中性日常问题（`voice_soak_scenarios/child.json` 中的 hello、day、why-sky、why-rain、moon-follow、follow-1、dino、math、english、story-short、riddle、sing），语音为生产 Doubao 音色渲染的素材。没有注入噪声；房间噪声没有测量。
- 没有使用完整的 child.json：其中的负面情绪与安全类步骤会在家长队列写入假的危机提醒（N-5 的记录）。
- 偏差：t001（hello）是唤醒后的热身。驱动检查时板子仍在播问候，判为 `step_failed: could not wake the device`，不计入统计。问候本身播出了（generation 1，79 帧）。

## 逐句

| 句 | 原话 | 服务器识别 | 回复 |
|---|---|---|---|
| t002 | 我今天在学校画了一幅画 | 无对应最终文本 | 完整 |
| t003 | 天空为什么是蓝色的？ | 部分（0.50） | 完整 |
| t004 | 为什么会下雨呀？ | 部分（0.62） | 完整 |
| t005 | 月亮为什么老跟着我走？ | 逐字 | 完整 |
| t006 | 那晚上为什么是黑的呢？ | 逐字 | 完整 |
| t007 | 恐龙都吃什么东西？ | 逐字 | 完整 |
| t008 | 七加八等于多少？ | 无对应最终文本 | 完整 |
| t009 | 苹果用英语怎么说？ | 无对应最终文本 | 完整 |
| t010 | 讲一个很短的笑话吧。 | 逐字 | 完整 |
| t011 | 你给我出一个谜语吧。 | 无对应最终文本 | 有回复（6.0 s） |
| t012 | 你会唱歌吗？ | 逐字 | **被取代，输出 0 帧** |

## 日志要点（北京时间，板子与服务端按同一时钟对齐）

- 18:16:13.357 服务端提交 turn 12（「你会唱歌吗？」，文本长度 6）。
- 18:16:14.581 服务端进入 speaking（generation 12）。
- 18:16:14.600 板子 `Device VAD start`，rms 0.0009，板子仍处于 listening。
- 18:16:14.663 服务端 `publish:interrupted generation_id=13`，随后 `restore_listen:interrupt_no_yield:superseded`；generation 12 记为 `status=aborted reason=superseded`。
- 18:16:14.804 板子收到 generation 12 的首个下行帧，同一时刻 `Device VAD end`（rms 0.0000）。
- 18:16:14.804 generation 12 的播放供给汇总为 `close=generation_switch output_frames=0`；18:16:15.014 generation 13 `close=channel_flush output_frames=0`，板子回到 listening。
- generation 13 从未提交：服务端没有对应的 commit 结果行。

## 其他计数

- 服务端：`media turn committed` 11 次；`result=empty_media_turn` 3 次，分别在 18:13:48、18:13:50（唤醒后）与 18:15:45（t009 之后）。
- 驱动结束后：边缘在 18:16:53 记录 `projected conversation close`，即服务端关闭了这个会话，距末句回复约 36 秒；bridge 另有两条 `media turn discarded after ASR tail timeout`。
- 上行编码队列：`Encode queue is full` 从 18:13:46 起整段测试都在出现，驱动结束后仍在增长，记录进程停止时（约 18:16:45）计数为 1157。
- 串口的 `E (` 级别日志只有 BMI2 的 I2C 读超时（2 条，已知，无影响）和唤醒时的一条 i2s 提示（无影响）。

## 没有查清、没有验证

- 板子在服务端关闭会话（18:16:53）之后的状态没有观察到：记录进程已经停止，重新打开串口会让板子复位。
- VAD 起点的来源未确定。候选有三个：扬声器开启的瞬态被麦克风拾取、房间底噪、回声残留。本轮的数据不足以区分，也没有录下房间噪声。
- 上行编码队列溢出与听错之间的因果没有证实。10-07 的几轮用的是 bench 镜像，本轮是产品镜像，两者的负载不同，丢帧数不能直接比较。
- 没有做带登录的真实调用，没有做别的语音场景；只有这 12 句。

## 对后续的含义

- 诊断行目前只覆盖 held 路径。本轮的丢失在 `media_session_output_stream.py` 的 superseded 分支，要在那里加同样的一行（只记出口与长度，不记孩子的话），再做一轮真机，才能把这条路径的原因分出来。改动小，但要 PR 与发布。
- 候选修法二（取代它的回合是空回合时重放准备好的回复）与本例吻合，代价是回复晚数秒，而且要区分假起点和真人插话。本例的 VAD 起点 rms 为 0.0009，远低于真人说话的 −21…−27 dBFS，可以作为判据之一，但需要更多样本。
- 上行丢帧要单独查：读固件 `AudioService` 的编码队列与 CPU 占用，或者在产品镜像上量。它不挡上面两条。
