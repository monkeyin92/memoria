# 2026-10-11 播放中停止专项

## 结果与边界

09:16:11–09:19:07 CST，在用户“继续”后使用 build 24、USB 唤醒和 Mac 音量 50 测试两条故事回复。严格停止证据为 **1 次成功、1 次失败、0 次证据不足**：“别说了”停止成功，“停”自然播完。90 秒尾窗观察到实际 `listening -> idle`；脚本返回 0 只表示收尾判据通过，不表示停止专项全部通过。

这是当前停止判据 v2 的一条正向设备样本，不关闭 N-5、N-8 或整体验收。`direct_real_device_verified=false`、`full_duplex_verified=false`、`student_safety_loop_verified=false` 保持不变。两个停止素材音色不同，且没有原始上行录音、声压或双端时钟校准，不能把差异单独归因于音量、回声或 endpoint；不能将一次成功泛化为稳定成功。

## 发布身份与素材

唯一测试 session 为 `201620b7-bd21-4802-909d-115a6483d8a5`，stream epoch 2578。串口启动记录 `MEMORIA_FIRMWARE_BUILD=24` / `slot=ota_0`，USB `wake` 被接受，`wake_mode=button`。打开串口时设备复位一次，旧 session 的 WSS 随后出现 EOF；新测试 session 内没有再次复位、崩溃或看门狗日志。

09:22:21 只读复核：`/opt/memoria/current` 仍指 `20261010-n8-endpoint-diag-v1` / `a758e7a1a086aebc4f947ac273dca2be7f60d28d`，Control / Bridge / Speaker 与该身份一致，Media Edge 仍为 `20261002-late-progress-v1` / `f3fe8742…`；四个服务 healthy、重启 0，外部 readiness 为 `ready` / `smokes=passed`。本轮只测试并读回证据，没有发布、刷机、调整生产配置或清理服务器制品。

场景复用 `outputs/acceptance/run-20261010-story-stop-2145-scenario.json`，每条在开口后约 6 秒播放停止素材：

| 场景 | 提问 / 停止素材 | 音色与原始时长 | 说完到开口 | 结果 |
|---|---|---|---:|---|
| t001 | 给我讲一个故事吧。 / 别说了。 | 既有 Doubao 语音库，1.984 / 0.949 秒 | 2.30 秒 | 停止成功，素材进程起点到设备退出说话 4.891 秒 |
| t002 | 再给我讲一个故事吧。 / 停。 | 缺少对应语音库，回退 `Tingting`，1.960 / 0.538 秒 | 2.85 秒 | 未停止，原回复自然播完 |

时长是文件读数；时间线里的播放进程窗口包含调度开销。第二条的音色回退已经发生，必须保留为实验限制；下一轮应先统一音色并记录播放素材身份。两个问题的归档字符相似度分别为 1.00 / 0.90；第二条归档多出“好的”，来源未归因。归档分析只查询本 session 和上述时间窗，不能用于还原未归档的停止词终稿原文。

## 同一回复的停止证据

| 证据 | “别说了” | “停” |
|---|---|---|
| delivery fence | `epoch-1/turn-2/generation-2/tool-0` | `epoch-1/turn-3/generation-4/tool-0` |
| 设备开口 | 09:16:50.300，generation 2 | 09:17:11.173，generation 4 |
| 停止素材进程窗口 | 09:16:56.345–09:16:58.254 | 09:17:18.175–09:17:19.574 |
| Bridge 终稿判定 | 09:17:01.085，`stop_word=True`、`echo=False`，钉停止 endpoint | 播放期间所见终稿均为 `stop_word=False` / `not_stop_word`；未出现停止动作 |
| Bridge 实际停止 | 09:17:01.095，`media spoken stop interrupted reply`，generation 2、replacement 3、`flush=True` | 无 |
| 同 fence 终态 | 09:17:01.093，`preempted / reply_task_cancelled` | 09:17:31.875，`playback_ended / playback_completed` |
| 串口同代播放收据 | 09:17:01.236，`close=generation_switch output_frames=534 first_output=yes`，同刻 `speaking -> listening` | 09:17:31.960，`close=channel_flush output_frames=1038 first_output=yes`，随后 `speaking -> listening` |
| 离线重算 | `stopped=true / spoken_stop_and_device_playout` | `stopped=false / playback_completed` |

上述 Bridge 时间从 UTC 转为 CST；没有校准两端时钟，不将跨端时间差称为声学延迟。成功样本的 4.891 秒是本机素材进程起点到串口状态转换的间隔。Bridge 的被取消回复仍记录 `actual_heard=False`，Edge 丢弃该被替换代的迟到 `playback.ended`；设备正向输出帧与停止收据可以证明本轮停止，但不能补造归档播放终态或用户实听确认。

本地 `lexical_playback_control_only("停。")` 与 `lexical_playback_control_only("别说了。")` 均为 `True`。当前证据只说明第二条没有得到可触发停止的终稿，不支持扩大停止词表、放宽回声保护或修改 #198 的 40 ms 门限。09:17:21.466 的三字终稿也记录 `stop_word=False / echo=False`；没有原文或上行录音，不能认定它就是误识别的“停”。

另捕获 1 条带 overlap 快照的 `cross_sentence_overlap` 拒绝，没有 `media prepared reply dropped`、`media reply spared` 或 #197/#198 专项目标事件。原始串口有 2 条 BMI270 I2C timeout、1 条启动期 I2C 初始化错误、1 条编码队列丢帧告警（累计 1）。这些是本窗口观察，不能据此关闭既有丢帧问题。

## 收尾与可复算收据

09:18:05.104 实际转为 idle，观察持续至 09:19:04.976。logger 经 SIGINT 停止，进程、USB 口与 `/tmp/memoria-serial-usbmodem101.sock` 均已释放。

驱动记录开测前音量 38，测试设为 50；脚本退出后音量回到 38，但没有保存开测前的静音标志。09:19 读回为未静音，随后补回上一轮记录的 38/静音并现场核验。不将上一轮静音状态冒充为本轮开测前采样；下轮应先完整保存音量与静音设置。

原始数据在 ignored `outputs/acceptance/run-20261011-091611-stop-target/` 和 `outputs/serial/robot-20261011-091611-stop-target.log`，没有改写。文件大小、SHA-256、场景/素材/工具身份及只读线上复核收据见 [source-receipts.json](source-receipts.json)；固定离线停止结果见 [stop-audit.json](stop-audit.json)。

```bash
# 仅重算本地停止证据，不访问设备、生产或播放声音
uv run --no-project python scripts/voice_soak_evidence.py \
  outputs/acceptance/run-20261011-091611-stop-target

# 仅查询本 session 与时间窗的归档，不启动测试
uv run --no-project python scripts/voice_soak_analyze.py \
  outputs/acceptance/run-20261011-091611-stop-target \
  '2026-10-11 09:16:11' '2026-10-11 09:19:07'
```

## 10:55 同音色停止对照复测

09:55:14–09:58:04 CST，在 build 24 / `ota_0` 上以 Mac 输出音量 50 做了两步对照。串口日志先出现 `ESP-ROM` 启动标记，随后于 09:55:25.702 报告 build 24 / `ota_0` 并完成 `activating -> idle`；这是串口打开后的启动/复位记录，不是刷机。`voice_soak` 接入 logger 时已错过该 idle 转换，所以 timeline 记为 `assume_idle`；独立原始串口日志保留了启动到 idle 的实际状态证据。两次都播放同一个 1.984 秒 Doubao 提问文件（SHA-256 `e0a8426f…`），并在回复开口约 6 秒后播放同一个 0.949 秒 Doubao 停止素材（SHA-256 `b943971e…`）；音量与素材音色不再是两步之间的变量。唯一 session 为 `6954594a-7146-4a4f-b65c-3aec9147ca15`，bridge `stream_epoch=2579`。

| 场景 | 提问归档 | 停止词 | v2 判据 | 关键设备证据 |
|---|---|---|---|---|
| `t001-story-stop-a` | `给我讲一个故事吧。`，相似度 1.00 | 别说了 | 确认停止；素材起点到设备回到聆听 3.337 秒，非声学测量 | Bridge `stop_word=True / echo=False`，`spoken stop interrupted reply`；串口 `generation_switch output_frames=465`，`speaking -> listening` |
| `t002-story-stop-b` | `你好。 给我讲一个故事吧。`，相似度 0.89 | 停 | 未停止，回复自然播完 | 7 条候选终稿均 `stop_word=False`，其中 2 条 `echo=True`；delivery `playback_completed`，串口随后 `speaking -> listening` |

这是同一素材条件下各一次的方向性复测：再次出现“别说了”成功、“停”未停，但不能据此泛化成功率或断言“停”被 ASR 误识别。第二轮归档在问题前多出“你好”；两轮回复文本长度分别为 115 和 65 字符，第一轮被中断且没有归档完整文本，不能确认两轮答案内容等价。没有原始上行录音，桥的停止诊断只保留长度和分类，不含停止语原文。未修改停止词表、回声保护或 endpoint 阈值。没有 `media prepared reply dropped`、`media reply spared` 或 `cross_sentence_overlap`，故本轮也未命中 #197/#198 目标路径。

90 秒尾窗在 09:57:02.736 由串口观察到 `listening -> idle`。停止轮结束后另有 1 条 `empty+vendor_silent` ASR tail timeout，没有归档播放；不能把它归入预定的两条提问。原始串口启动窗口共 5 条 `E`：启动期 I2C 未初始化提示 1 条、I2S 对未启用通道执行 disable 1 条、BMI270 I2C timeout 3 条；另有 1 次编码队列丢帧。启动期 I2C 总线随后初始化成功。logger 已 SIGINT 停止，USB 与 socket 已释放；Mac 开测前读到 38/静音，测试时 50/解除静音，结束后复核恢复 38/静音。`voice_soak` 内记录的 `previous_volume=50` 是外层测试包装器先设音量后的值，不是开测前状态。

10:00:04 只读复核：current 仍为 `/opt/memoria/releases/20261010-n8-endpoint-diag-v1`，整栈 `a758e7a1…`、Media Edge `20261002-late-progress-v1`；四个服务 healthy、重启 0，内部 readiness `ready` / `smokes=passed`，外部 readiness HTTP 200。没有部署、改生产配置或刷机。`direct_real_device_verified=false`、`full_duplex_verified=false`、`student_safety_loop_verified=false` 保持不变；N-5 ≥30 分钟长稳、N-8 目标路径仍未关闭。

本轮原始数据在 ignored `outputs/acceptance/run-20261011-095137-same-tone-stop/` 与 `outputs/serial/robot-20261011-095137-same-tone-stop.log`；逐文件大小与 SHA-256、素材身份、设备/线上状态见 [same-tone-receipts.json](same-tone-receipts.json)。离线复算与归档查询：

```bash
# 仅重算停止判据，不访问设备、生产或播放声音
uv run --no-project python scripts/voice_soak_evidence.py \
  outputs/acceptance/run-20261011-095137-same-tone-stop/capture

# 查询仅限该 session 与本轮时间窗的归档
uv run --no-project python scripts/voice_soak_analyze.py \
  outputs/acceptance/run-20261011-095137-same-tone-stop/capture \
  '2026-10-11 09:55:10' '2026-10-11 09:58:10'
```

## 12:00 反序同音色停止对照

12:01:37–12:04:55 CST，在 build 24 / `ota_0`、Mac 输出音量 50 下反序复测。串口 logger 于 12:00:41 打开设备并触发启动；原始串口日志确认 `MEMORIA_FIRMWARE_BUILD=24`、`slot=ota_0`、`wake_mode=button` 与真实 `activating -> idle`。驱动接入 logger 时已错过该转换，记录 `assume_idle`；不把它单独当设备初态证据。唯一 session 为 `408e64ba-1eec-4e76-a3e2-b4b806d8991c`，`stream_epoch=2580`。

本轮先播“停”，再播“别说了”。两步均使用同一 1.984 秒 Doubao 提问 WAV（SHA-256 `e0a8426f…`）和同一 0.949 秒停止 WAV（SHA-256 `b943971e…`），都在回复开口后 6 秒播放；两条提问的归档转写均与素材完全匹配（相似度 1.00）。测试前完整读到音频状态为 38/静音、输入 82、提示音 100；测试时为 50/解除静音，结束后恢复并核验原值。

| 顺序 / 停止词 | 归档提问 | 说完到开口 / 回复时长 | v2 判据 | 关键证据 |
|---|---|---:|---|---|
| `t001` / 停 | `给我讲一个故事吧。`，1.00 | 3.29 / 30.86 秒 | 未停止 | 未见停止动作；同 fence `playback_completed`，实际播完 |
| `t002` / 别说了 | `给我讲一个故事吧。`，1.00 | 2.71 / 32.95 秒 | 未停止 | 未见停止动作；同 fence `playback_completed`，实际播完 |

离线复算为 **0 次成功、2 次失败、0 次证据不足**。Bridge 没有 `stop_word=True` 或 `media spoken stop interrupted reply`；设备在回复自然播完后回到聆听。与 09:55 的正序同素材结果合看，四次尝试中“停”0/2、“别说了”1/2。样本仍很少，回复内容与时长并非固定；反序结果不能证明顺序因果，也不能据此改停止词表、回声保护或 endpoint 阈值。

受控场景两步之后，session 归档又记录一条非预定输入“你说呢？”，随后在 12:03:56 有第三条完整播报；它发生在 90 秒尾窗中，不计入两次停止判据，来源无法仅凭现存日志归因。Bridge 日志记录服务端 PCM tap 曾打开到 `/tmp/media-pcm-tap/media-uplink-408e64ba-1eec-4e76-a3e2-b4b806d8991c-epoch2580.wav`；我未检查该文件是否仍存在，未取回、播放、分析或删除其内容，因此不能把本轮概括成“没有原始上行录音”。串口测试日志另有 1 条 I2S disable 错误和 1 条 BMI270 I2C timeout；未观察到 #197/#198 专项目标事件。

90 秒尾窗于 12:04:29.661 观察到真实 `listening -> idle`。logger 经 SIGINT 停止，USB 口和命令 socket 均已释放。12:09:11 只读复核：整栈仍为 `20261010-n8-endpoint-diag-v1` / `a758e7a1…`，Media Edge 为 `20261002-late-progress-v1`，四个组件 healthy、重启 0，外部 readiness HTTP 200。没有刷机、部署或改生产配置；`direct_real_device_verified=false`、`full_duplex_verified=false`、`student_safety_loop_verified=false` 不变，N-5 ≥30 分钟长稳与 N-8 目标路径仍未关闭。

原始数据在 ignored `outputs/acceptance/run-20261011-1200-reverse-same-tone-stop/` 和 `outputs/serial/robot-20261011-1200-reverse-same-tone-stop.log`。反序场景、音量/发布收据、逐文件哈希、停止复算与时间窗限定的归档分析见 [同音色收据](same-tone-receipts.json) 的 `reverse_order_run`；本地复算文件为该 run 的 `capture/stop-audit.json` 和 `capture/analysis.md`。

```bash
# 仅重算停止判据，不访问设备、生产或播放声音
uv run --no-project python scripts/voice_soak_evidence.py \
  outputs/acceptance/run-20261011-1200-reverse-same-tone-stop/capture

# 查询仅限该 session 与本轮时间窗的归档
uv run --no-project python scripts/voice_soak_analyze.py \
  outputs/acceptance/run-20261011-1200-reverse-same-tone-stop/capture \
  '2026-10-11 12:01:37' '2026-10-11 12:04:56'
```
