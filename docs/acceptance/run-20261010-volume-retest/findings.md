# 2026-10-10 Mac 扬声器音量复测

本轮按用户授权于 19:09–19:10（北京时间）进行短时真机测试。只使用电脑播放四句中性素材，通过 USB `wake` 唤醒；没有播放唤醒词、刷机、修改设备/线上配置或发布。设备连入当前生产语音链路，产生一条测试会话及相应语音归档记录。

## 结论

- 测试开始前，默认输出设备是 MacBook Pro 扬声器，系统音量 38 且处于静音。运行期间解除静音并将电脑音量设为 45；结束后核实恢复为音量 38、静音。**本轮证明了在该音量与解除静音状态下，机器人可以收到并回答素材；不能据此断定此前测试时也处于静音。**
- 场景含 4 句；首次 USB 唤醒成功，但机器人先播放问候音，驱动把 `hello` 步骤记为 `step_failed`，该问句没有播放。其余 3 句均由服务器归档为对应输入，3 条回复均有匹配内容、设备输出帧和 `actual_heard=True` / `playback_completed` 收据。开口延迟为 2.85–3.31 秒。
- 设备在会话聆听期间持续记录 `Encode queue is full`：118 条告警行，末次累计丢帧数 837；会话约 19:12:19 才进入 idle。三句均正确转写并得到匹配回复，但丢帧原因和影响未定。Bridge 另记录 3 次 `media pending turn split after reply`；不据此推断是音量或回声导致。
- 本轮不覆盖停止/打断、噪声探针、endpoint 判据、长稳、#197/#198 目标事件或儿童安全场景；`direct_real_device_verified`、`full_duplex_verified`、`student_safety_loop_verified` 仍为 `false`。

## 条件与逐句结果

USB 设备序列号 `90:E5:B1:D7:83:2C` 与开发板记录的 MAC 一致。重开串口后设备启动为 build 24、`ota_0`，激活清单验证通过，唤醒模式为 `button`。串口记录器于 19:08:20 开始读取；USB 唤醒在 19:10:01 被接受。电脑输出使用现成 voice bank 素材；设备侧音量配置日志为 30，与 Mac 输出音量不是同一控制项。

| 素材 | 服务器收到的文本 | 服务器归档的回复 | 说完到开口 | 结果 |
|---|---|---|---:|---|
| `hello`（未播放） | — | — | — | 唤醒问候期间驱动观察到 `speaking`，记为 `step_failed` |
| `math` | `7+8等于多少？` | `7加8等于15。。要不要我再出几道给你练练？` | 3.31 秒 | 语义匹配；设备输出 190 帧 |
| `why-sky` | `天空为什么是蓝色的？` | `因为太阳光里有很多颜色，蓝色的光最容易被空气弹得到处都是，所以抬头一看，满天空都是蓝色啦。` | 2.85 秒 | 转写一致；设备输出 429 帧 |
| `why-rain` | `为什么会下雨啊？` | `云里的小水滴越聚越多，重得飘不住，就掉下来变成雨啦。` | 2.94 秒 | 轻微末字差异；语义匹配；设备输出 257 帧 |

三条用户输入和三条回复均来自 19:09:40–19:11:00 的限定时间窗。设备日志中的播放输出帧与服务端实际播放收据是设备链路证据，不代表有人耳在设备旁完成主观音量评估；本轮没有声压计测量。

## 收尾与限制

- Mac 最终状态：输出音量 38、`output muted=true`，与测试前相同。
- 串口记录器已 SIGINT 停止；`/dev/cu.usbmodem101` 已无占用进程，命令 socket 已释放。
- 串口有 2 条 ESP-IDF `E` 级日志（启动 I2C 初始化提示、唤醒时 I2S channel 未启用提示）；三条测试问答仍完成。暂不据此判断无影响。
- 归档分析命令（只读、限定该会话时间窗）：

```bash
python scripts/voice_soak_analyze.py outputs/acceptance/run-20261010-volume-retest \
  '2026-10-10 19:09:40' '2026-10-10 19:11:00'
```

## 本地原始证据

原始素材、timeline 和设备/服务日志位于本机 gitignored `outputs/`，未纳入版本控制。以下 SHA-256 固定了本轮文件：

| 文件 | SHA-256 |
|---|---|
| `outputs/acceptance/run-20261010-volume-retest-scenario.json` | `3fad9e555d42f51d10881e634575f364390b7b575bb74e7447a69c7e5de0d4c5` |
| `outputs/acceptance/run-20261010-volume-retest/timeline.jsonl` | `e929650dcf957e838b4a3aa9c34886c4c91e5abc3e9b16eb9b8ba68fd476fb54` |
| `outputs/acceptance/run-20261010-volume-retest/bridge.log` | `15a876a51fce98d1f71a3606ee7dbfda607fb2bbc7b49f18cc52bfc87dadd359` |
| `outputs/acceptance/run-20261010-volume-retest/edge.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `outputs/acceptance/run-20261010-volume-retest/control.log` | `4beb840a5f7643e8f0c945be97c1734b8663c2b4d08c8320cd203c6d567d310f` |
| `outputs/serial/robot-20261010-volume-retest.log` | `715bc7e36c4fe5fffd1746695c70bfdb7f9757051383ad99c4ab843b492d18d9` |

## 后续代码修正与队列静态追查（2026-10-10）

- `voice_soak` 的唤醒等待已改为从 wake 时间点检索历史 `listening` 转换，再从该时点检索历史 `speaking` 问候事件，并在问候结束回到 `listening` / `idle` 后才播放首句。旧的 4 秒窗口短于本轮实测的 4.264 秒；本地离线回归覆盖 4.264 秒后才开始、且在等待器处理前已结束的问候。改动未刷入设备；经授权的修复后真机复测见下节，首句时序已验证。
- 固件缓存中的上游实现表明：VoCat 上行 Opus 帧长为 20 ms，编码队列容量为 2 帧（约 40 ms）；AFE 输入任务固定在 core 0 / priority 8，Opus 编解码任务在 core 1 / priority 5，播放任务在 core 1 / priority 6，伙伴动画任务在 core 1 / priority 2。队列满时会丢弃最旧 PCM 帧；发送队列另有约 2.4 秒容量，满时同样丢旧包而不阻塞编码消费者。
- 本轮 837 帧 / 约 132 秒约为 6.4 帧/秒，和 10-08 以及 10-09 基线时段的丢帧速率接近；10-09 另一个仅丢 1 帧的会话说明现象有明显会话间波动。静态链路将问题收敛为 AFE 生产端持续快于 Opus 队列消费者的结果，但不能判定是哪项 CPU 负载或调度造成，也不能把它归因为 Mac 音量、播放期双工或 ASR 错误。未改队列容量、丢帧策略或固件源码；继续定因需要在获授权的设备窗口采集编码耗时、队列高水位及任务负载对照。
- 离线回归验证见本次交接记录。未刷机、未访问生产、未播放音频，所有既有验收标志保持 `false`。

## 2026-10-10 修复后真机短测（19:47–19:50 CST）

用户于 19:45 授权继续真机测试。本轮只使用现有四句中性 voice bank 素材与修正后的 `voice_soak`，MacBook Pro 扬声器临时设为音量 45、解除静音；USB `wake` 被接受。没有刷机、发布或改设备/线上配置。测试产生一条生产语音会话及相应归档事件。

### 首句时序

设备为 build 24 / `ota_0`，激活清单有效。USB 唤醒于 19:48:30.006 接受，设备于 19:48:32.475 进入 `listening`；唤醒问候到 19:48:37.028 才开始（晚 4.553 秒），19:48:38.653 回到 `listening`。首句素材在 19:48:40.185 播放。它被服务器按原文完整识别，收到匹配回复并有 `actual_heard=True` / `playback_completed` 收据。**修正后的事件历史等待在这次真机样本中通过，首句不再被问候期间的 `speaking` 状态跳过。**

### 逐句与异常

驱动播放 4 句，记录到 4 次回复；说完到设备开口 p50 2.7 秒、p95 6.0 秒。服务器归档识别相似度平均 0.85，4 句中 2 句 ≥ 0.9：

| 素材 | 服务器识别 | 归档回复 | 相似度 | 结果 |
|---|---|---|---:|---|
| `hello` | `你好呀，你叫什么名字？` | 自我介绍为桃喜并邀请聊天 | 1.00 | 匹配 |
| `math` | `七加八等于多少？` | 七加八等于十五 | 1.00 | 匹配 |
| `why-sky` | `达到15。天空为什么是蓝色的？` | 解释蓝色光被空气散射 | 0.82 | 识别前缀混入上一轮答案，回复仍切题 |
| `why-rain` | `为什么会看你啊。` | 继续解释蓝色光与红色光 | 0.57 | 识别错误且回复答非所问 |

限定窗口内共有 **5 条已完成的机器人播报**，多于脚本安排的 4 句。第 4 句播报结束后，服务端于 19:49:42.903 记录另一条 VAD start，19:49:48.282 提交额外回合，额外回复于 19:49:58.405 记为 `actual_heard=True` / `playback_completed`；脚本没有安排该轮输入，音源尚未归因。19:50:14 又有一次 VAD start，后续 ASR 无文本并于 19:50:22.645 超时丢弃；19:50:30.898 起服务端拒绝终止围栏后的音频。**这只证明服务端会话进入 terminal，不证明串口未观测到的设备状态已回到 `idle`。**

设备串口在 19:48:35.371–19:49:50.480 共记录 68 行 `Encode queue is full`，累计计数到 475 且仍在增长时记录器停止；按该日志区间约 6.3 帧/秒，475 不是整段会话最终计数。19:48:32.474 另有一条 ESP-IDF `E` 级 I2S 日志（`i2s_channel_disable: the channel has not been enabled yet`），不据此判断其对问答结果无影响或有因果。服务端另有 `provider_pcm_clipping_detected=true` 的 ASR 边界记录；它与识别错误同轮出现，但不能据此断言因果。主观听感与声压未测，本轮也没有停止/打断、噪声目标路径、30 分钟长稳或安全场景验收。

收尾核实 Mac 音量恢复到 38、静音为 `true`；串口端口无占用、命令 socket 已释放。记录器停止后没有继续打开串口，以免再次复位设备；因此未取得设备最终 `idle` 的串口证据。全局 `direct_real_device_verified`、`full_duplex_verified`、`student_safety_loop_verified` 仍为 `false`。

只读归档分析命令：

```bash
uv run --no-project python scripts/voice_soak_analyze.py \
  outputs/acceptance/run-20261010-greeting-retest-1945 \
  '2026-10-10 19:48:25' '2026-10-10 19:50:30'
```

### 本轮原始证据

文件均位于本机 gitignored `outputs/`，未纳入版本控制：

| 文件 | SHA-256 |
|---|---|
| `outputs/acceptance/run-20261010-greeting-retest-1945/timeline.jsonl` | `02bc2de990a19b5d53a4dfee27d604b42be0e4aa8374d32e0e5cdafc50d61998` |
| `outputs/acceptance/run-20261010-greeting-retest-1945/serial.log` | `86c01cfbf2255f4ff677f7b7f983ed90f313c220b418a061b55202def7ba55cd` |
| `outputs/acceptance/run-20261010-greeting-retest-1945/bridge.log` | `03784b8e38030e297b2ca70929bd78ca20ffb808599bc657341a1c9985552ed4` |
| `outputs/acceptance/run-20261010-greeting-retest-1945/bridge-post-run.log` | `1c2ed21f8013ef0eb342d944c31f9a3df742b62a17d143cd676cc8e0fd301309` |
| `outputs/acceptance/run-20261010-greeting-retest-1945/edge.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `outputs/acceptance/run-20261010-greeting-retest-1945/control.log` | `32a9b1b439a85b932c05bb8ddf2fa1e6de6116fccf0da969726e9e2cdf7b8080` |
| `outputs/serial/robot-20261010-greeting-retest-1945.log` | `772da7a7b77b47538c92260e1cb207acd8c1140275e55781fadbae7f56d4baa9` |

## 2026-10-10 后续离线归因与收尾观测改进

- 只读归档查询确认：19:49:48 附近存在第 5 条 `speech.utterance_finalized`，其后第 5 条播报于 19:49:58.405 完成。因此额外回复前确实收到了一段 ASR 文本，不是已证实的“没有输入而模型自行回复”。原始转写不复制到本收据；声源未知，不推断为环境语音、扬声器回声或某个人发声。
- 第 4 句仍是识别错位：设备归档转写与预设问题不一致，回复延续回答天空。同期 ASR 边界记录 `provider_pcm_clipping_detected=true`、PCM peak 为 32767；原始 PCM 未留存供声学检查，削波与识别错误同期出现不构成因果证据。
- 驱动 timeline 在 19:49:40.567 写入 `scenario_done` 后立即进入清理，串口 watcher 最后只记录到 19:49:50.480。服务端后续仍记录了额外播报完成、19:50:14 的 VAD、19:50:22 的 ASR tail timeout 和 19:50:30 的 terminal；这些服务端事件不能证明设备回到 `idle`。没有再次打开串口以免复位设备，因此这组记录的最终设备状态仍未验证。
- 本地候选 `voice_soak` 于场景结束后默认继续观察 90 秒（可设为 0–180 秒），期间串口与服务日志跟踪保持运行；timeline 新增 `post_scenario_observation`。初版只检查截止状态，仍可能把推定 idle 当通过；随后已修成要求尾窗内真实串口 idle 转换，非 idle 返回 3、idle 来源或时点不足返回 4。21:38 真机收尾判据验证见下节。
- 离线验证：`uv run pytest --no-cov scripts/tests/test_voice_soak_evidence.py -q`，39 passed。该结果只验证本地脚本与模拟状态转换，不改变上述设备验收边界。

## 2026-10-10 真实 idle 判据修正与 21:38 短测

用户授权按顺序继续。`voice_soak` 收尾现在必须同时满足：观察窗内收到串口 `StateMachine` 转入 `idle` 的状态转换，且该转换仍是观察截止时的最终状态。若最终字段是启动时推定的 `idle`，返回码为 4（证据不足）；最终非 `idle` 返回码为 3（超时）。`voice_soak_analyze.py` 在报告顶部显示收尾判定；旧 timeline 缺少 `post_scenario_observation` 时显示未验证。分析器只有在 `bridge.log` 能唯一识别 UUID session 时才查询归档，并在 SQL 里按 `archive_evidence_events.session_id` 与时间窗同时过滤。

21:38:39–21:41:23 使用 MacBook Pro 内置扬声器、音量 45、解除静音、原四句中性素材。生产 readiness 在开测前返回 `ready`（`20261010-stale-interim-v1`，smokes `passed`）。4 句均有设备回复；说完到开口 p50 2.9 秒、p95 3.2 秒。逐句归档相似度均值 0.84，2/4 ≥ 0.9；第一句归档终稿较预设短，其他三句近似或一致。观察窗内 21:40:23.976 串口明确记录 `listening -> idle`，覆盖完整 90 秒尾窗，驱动返回 0。设备日志有 4 条 `E` 级日志（含一条 BMI270 I2C 读取超时），原因与问答的关系未定；没有据此宣称整轮无异常。

| 文件 | SHA-256 |
|---|---|
| `outputs/acceptance/run-20261010-idle-retest-2140/timeline.jsonl` | `ef54ed1e71ccc5c0854eb5b5854407a6a40633bb273357690e772823aa868f53` |
| `outputs/acceptance/run-20261010-idle-retest-2140/serial.log` | `f742eb6e7db12f8c5438ab589f30c4ced6ad173465ab9181e28d337ba5aed6e3` |
| `outputs/acceptance/run-20261010-idle-retest-2140/bridge.log` | `df601d4008f194a6072e21a85d6aebbd9126425844d03486fc088a82572ea786` |
| `outputs/acceptance/run-20261010-idle-retest-2140/control.log` | `f0f3023de633c8eec8edd1f8f58e7172cd281718ee239fb5d50927a56244de96` |
| `outputs/acceptance/run-20261010-idle-retest-2140/edge.log` | `eee252c44e8c9f891328ebe2b403efdf9cb64fe6565b0835961a90752c40e` |
| `outputs/serial/robot-20261010-idle-retest-2140.log` | `d650106bb0fa430db6f5efdfdd4a8d9e2eee8b4efc2595dd08173b562792335c` |

归档复核只查唯一设备 session `644d149d-c99c-4937-8813-6e181de1b4f5` 和 21:38:35–21:41:23 时间窗。该样本验证的是新收尾观察路径，不改变 `direct_real_device_verified`、`full_duplex_verified` 或 `student_safety_loop_verified`。

## 2026-10-10 故事播放停止专项

21:44:25–21:47:12 用同一设备、音量 45 和同一生产版本发起两轮故事播放：分别在回复开口后约 6 秒播放“别说了”和“停”。两轮均有完整回复输入和设备播放收据，但**确认停止 0/2、未停止 2/2**；两个 delivery 的桥终态都是 `playback_completed`。两轮没有 `media spoken stop interrupted reply` 或 `preempted` 的正向停止证据。stop 判据没有把“驱动播放了停止素材”或“设备最终回到 listening”误记为成功。

桥日志在该时间段多次记录 `media playback-stop not taken … reason=not_stop_word`；其中第一轮一条片段记录 `echo=True`。日志没有保留转写原文，因此不能仅据这些字段断言设备实际识别到的停止词文本，也不能确定被判为 echo 的片段就是停止素材。当前可确认的是两次没有进入可证明的停止路由、回复自然播放完成；这是未通过的停止专项，不是 #197/#198 的目标路径结论。90 秒尾窗内于 21:46:12.232 收到 `listening -> idle`，最终收尾观察通过。

| 文件 | SHA-256 |
|---|---|
| `outputs/acceptance/run-20261010-story-stop-2145/timeline.jsonl` | `818c2b245ee748cbf13695502080e9d65d2113fd7351e75f2257f2510d61d912` |
| `outputs/acceptance/run-20261010-story-stop-2145/serial.log` | `8c2174d402ff79a3d7b7b7bbfc78e052dc9b94fb59d310af64eb682ff96705bd` |
| `outputs/acceptance/run-20261010-story-stop-2145/bridge.log` | `fa7d616861e3b201a72e001b39bea194275d683b814dc5cd79707a68bcb47cbf` |
| `outputs/acceptance/run-20261010-story-stop-2145/control.log` | `30631ce8dc204937852dd5d422e56ea165bf331b78b51276f30a4cdb2f31fc42` |
| `outputs/acceptance/run-20261010-story-stop-2145/edge.log` | `0110a845b3afe512b0d2a38c26acca0bc2885215d2946d20b57f5601de8f32a9` |
| `outputs/serial/robot-20261010-story-stop-2145.log` | `e625d630bd7ce6af1f7b2cd259e8007cf8fc6f1d92c9d9dc8ed9485dc0b4b81c` |

归档复核只查唯一设备 session `56e42cce-2a50-4b31-b224-2c0b83ccc621` 和 21:44:25–21:47:12 时间窗。Mac 音量在两轮脚本退出后均恢复为 38，静音为 `true`；串口占用与命令 socket 已释放。此失败需要先检查 stop 词范围、播报期间声学路径与回声/ASR 证据；未改播放停止规则、阈值或固件。基于停止专项未通过，本批不开始 ≥30 分钟长稳。
