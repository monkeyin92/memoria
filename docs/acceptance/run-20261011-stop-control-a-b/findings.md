# 2026-10-11 停止词与普通问句对照

本轮按用户“结束它吧，然后按你的来测试”的授权执行。原进程 PID `78854` 已退出，确认串口空闲后才启动唯一记录器。测试于北京时间 21:15:48–21:19:16 完成，包含三步场景和 90 秒尾窗。没有部署、刷机或修改线上配置。

## 验收结果

停止专项未通过。“别说了”和“停”各测一次，两个回复均自然播完，确认停止为 0/2。普通问句“停车场在哪里？”未触发停止，但其识别原文未被核验，因此只能记为本样本没有误触发，不能据此确认停止规则的语义边界。

| 播放中的素材 | 素材文件 | 故事回复时长 | 服务端终态 | 结果 |
|---|---|---:|---|---|
| 别说了。 | `story-stop-a-stop.wav` | 14.05 s | `playback_completed` | 未停止 |
| 停。 | `story-stop-b-stop.wav` | 16.70 s | `playback_completed` | 未停止 |
| 停车场在哪里？ | `story-parking-negative-stop.wav` | 34.38 s | `playback_completed` | 未触发停止，识别内容未验证 |

三条预定问题都是“给我讲一个故事吧。”，限定会话归档中的三条输入与问题原文一致。三个回复都有匹配 delivery、设备首个可播放帧、播放输出和自然完成收据。设备输出分别为 695、827、1714 帧，不能仅凭设备回到 `listening` 将其计为停止成功。

本轮使用 `voice-soak-stop-v2` 判据。现场结果与离线重算逐字段一致，两个停止样本都不是证据不足。通用分析器把第三个普通问句也计入三次“打断”，本收据按预定用途将其单列，不并入停止成功率。

## 条件与证据范围

- 设备 MAC 为 `90:e5:b1:d7:83:2c`，产品固件 build 24，槽位 `ota_0`，唤醒模式 `button`。21:13:42.247 的启动日志记录了真实 `activating -> idle`。驱动随后仅跟随日志，通过记录器 socket 发送一次 USB `wake`，没有第二次打开串口。
- 生产整栈是 `20261010-n8-endpoint-diag-v1`，三角色 revision 均为 `a758e7a1a086aebc4f947ac273dca2be7f60d28d`。media-edge 仍为 `20261002-late-progress-v1`。21:15:10 开测前和 21:46:31 收尾复查时，readiness 都为 `ready`，三个角色与 edge 均 healthy、重启 0。
- 会话是 `9a3598e3-e89d-4432-966a-3d6dc1c2c0ea`，stream epoch 为 2585。归档分析限定该 session 与 21:15:48–21:19:17 时间窗，没有读取其他会话。
- Mac 输出音量为 50，测试时解除静音。六个 WAV 使用相同的 `bright_peer` 音色请求，三个停止/对照素材使用独立的 bank key，文件 SHA-256 互不相同，时长均超过 0.5 秒。文件选择符合驱动的 `<tag>-stop.wav` 规则，没有同一文件被不同停止词标签复用的问题。没有人工听验素材内容，也没有声压计或主观听感测量。
- 实际运行 208.331 秒，其中场景 118.324 秒、尾窗 90.01 秒。`--duration-min 15` 是截止上限，不表示跑满 15 分钟。后续记录器在空闲状态继续采集的时间不计入测试时长。

## 停止路径与未查明的原因

桥日志没有 `media spoken stop interrupted reply`，也没有 `stop_word=True`。播放期间有 `reason=not_stop_word` 的拒绝记录，其中部分片段为 `echo=True`。这些日志只保存文字长度与样本范围，没有识别原文，不能认定某一片段就是停止素材，也不能据此将失败归到 FunASR、AEC 或某条停止规则。

本次三个 delivery 分别为同一 session 下的 `epoch-1/turn-2/generation-2/tool-0`、`turn-3/generation-3/tool-0`、`turn-4/generation-4/tool-0`。三个终态均为 `playback_completed`，没有进入已确认的 spoken stop 动作。因此本轮没有覆盖“停止动作已发生但 flush 或设备退出失败”的分支。

服务端现有 PCM tap 已为该 session 留存上行录音，收尾时确认存在并只读取回本机。文件为 16 kHz、单声道、16-bit PCM，共 2,015,040 帧、125.94 秒，4,030,124 字节。该录音是送往 ASR 的 admitted PCM，不是 Mac 的播放源文件。文件与哈希见 [固定收据](receipts.json)，录音留在 gitignored `outputs/`，未写入版本控制。

测试窗口的串口有 6 条 `E` 级日志，涉及 SSL 接收、I2S channel 和 BMI270 I2C 超时，另有一次编码队列丢帧告警，最后报告累计丢 1 帧。ASR 边界有两条 `provider_pcm_clipping_detected=true`。三个问题仍有回复，但本轮没有证据确定这些异常与停止失败的因果关系。

## 收尾与状态

21:18:16.509 串口记录 `listening -> idle`，90 秒尾窗结束时仍为来自真实状态转换的 `idle`。驱动返回 0 只代表最终 idle 判据通过，不表示停止专项通过。串口记录器已通过 SIGINT 停止，PID `97490` 已退出，端口无占用，命令 socket 已移除。

Mac 最终核验为 `output volume:50, input volume:82, alert volume:100, output muted:true`，与开测前一致。恢复操作覆盖音量数值和静音状态，不只依赖驱动恢复音量。

`code=现有停止实现与 v2 验收工具，未改动 / wired=生产 bridge、设备播放收据及唯一串口记录器 / enabled=20261010-n8-endpoint-diag-v1 / verified=本轮正常问题与最终 idle 已验证，停止专项失败`。证据日期为 2026-10-11。N-10 保持开放，本轮不关闭 N-5、N-8，`direct_real_device_verified`、`full_duplex_verified`、`student_safety_loop_verified` 均保持 `false`。

## 本地证据与复核

原始记录在 `outputs/acceptance/run-20261011-stop-control-a-b/`，完整启动和串口日志在 `outputs/serial/robot-20261011-stop-control-a-b.log`。这两处均为本机 gitignored 证据。[receipts.json](receipts.json) 固定了 15 个证据文件、6 个素材的大小与 SHA-256，并记录每个试验的完整回复围栏、刺激窗口、判定和收尾状态。

离线停止重算命令不访问设备或服务器：

```bash
python scripts/voice_soak_evidence.py outputs/acceptance/run-20261011-stop-control-a-b
```

限定会话归档分析使用以下时间窗（此脚本会读取服务端归档，不属于本批允许的纯离线命令）：

```bash
python scripts/voice_soak_analyze.py outputs/acceptance/run-20261011-stop-control-a-b \
  '2026-10-11 21:15:48' '2026-10-11 21:19:17'
```

## 第二轮离线 QA 初始失败记录

本节保留修复前的检查结果。当前诊断工具状态与语义分析结论见下方“离线诊断收口与语义核验结论”。

**IS_PASS: NO / QA FAIL。** 本批新增 `scripts/diagnose_stop_pcm.py` 和定向测试，本机报告在 `outputs/acceptance/run-20261011-stop-control-a-b/stop-pcm-diagnosis.json`。51/51 单测（新增 6、既有停止证据 45）及 ruff 通过不等于完整 QA PASS；第二轮发现以下未解决问题，达到两轮上限后停止修代码、加测试及复跑：

1. `acoustic_window` 对负起点和超出末尾的窗口使用 max/min 静默裁剪，仍可能报 `available=True`。
2. `read_wav` 未比对 WAV header 帧数与实际读取的 raw 字节数，截断文件可能被当作有效文件。
3. receipt 的 session 与 timeline full delivery 身份冲突未拒绝，混合 timeline 可能拼接目标日志。

真实 125.94 秒样本未触发上述三项：4,030,124 字节、2,015,040 帧及 SHA-256 `bcfa35225ea5117dd0f6826e0fb630e395497645e598b72233802c7294755ead` 与固定收据一致。目标 session 唯一 tap epoch 为 2585，保留 30 条真实 `not_stop_word` 拒绝，停止动作 0；既有 v2 专项 0/2 自然播完结论仍可信。25 条无 session 身份的 ASR/rescue 事件不可仅按临近时间归到目标，不表示服务端没有 ASR 输出。

tap-open 壁钟近似窗 RMS 为 3579.455、1911.101、2012.566，仅是候选窗能量。admitted PCM 是拼接流，缺 sample→wall-clock 映射，packet delay、拒收帧和间隙可移位；近似窗不是刺激已对齐证据。日志仅有 text_len/样本范围，无识别原文；波形、文件名与素材声明均不能证明 PCM 含“别说了”或“停”。当前只能把失败范围收窄为已确认 stop 动作之前，声学入流、ASR 与停止规则之间仍未归因。

当时未找到可供 SenseVoice 使用的模型，项目 venv 也没有常见的 Python ASR 依赖，因此尚未用该工具链转写上行 PCM。后续使用本机已有的 Whisper.cpp 模型完成了离线转写，结果见下方“离线语义与波形对齐核验”。

范围例外：本批有一次误运行带服务端归档读取的分析命令，导致只读 SSH 访问，违反本批“无生产访问”约束；无生产写入、发布、刷机或配置修改。此例外不能记为全程纯离线，后续已停止生产访问，仅保留本地证据。

## 离线诊断收口与语义核验结论

**诊断工具修复：QA PASS。** 第二轮发现的三个 fail-closed 缺口已全部修复并验证：
1. `acoustic_window` 针对负起点、超出样本末尾及逆向区间直接判定 `available=False` 并置 `rms=None`，不再静默裁剪。
2. `read_wav` 校验 WAV 头部声明的帧数与实际读取的 raw 字节数，字节数不符时抛出带 `truncated.*expected.*frames` 的 ValueError。
3. `diagnose` 增加严格的身份一致性校验，拒绝多 session 的混合 timeline、拒绝 timeline 与 receipts.json 的 session 冲突、拒绝 bridge tap epoch 与 receipts.json 的 epoch 冲突。
4. 修复后定向测试扩充至 13/13 全部通过（`scripts/tests/test_diagnose_stop_pcm.py`）。配合既有停止证据测试 45/45（`scripts/tests/test_voice_soak_evidence.py`），共 58/58 测试通过；strict mypy 与 ruff 无告警；`stop-pcm-diagnosis.json` 重新生成并通过校验。

**离线语义与波形对齐核验：**
1. 使用本机已有 Whisper.cpp 模型离线转写素材与上行 PCM，全程无网络请求。三条问题素材和三条打断素材的源文件转写与收据文案一致。
2. 新增的 NCC 对齐结果写入 `outputs/acceptance/run-20261011-stop-control-a-b/stop-pcm-diagnosis.json`。三条问题素材在上行 PCM 中的相关峰分别位于 5.144、28.044、53.112 秒，相对 tap-open 壁钟估算的偏移为 +1.244、+1.185、+1.205 秒，均值 +1.211 秒。由问题素材中位偏移校准后的打断搜索窗，避免把其他时间点的相似声误当成目标刺激。
3. 打断素材的最佳峰为：“别说了” 0.267，位于 16.981 秒；“停” 0.077，位于 39.965 秒；“停车场在哪里？” 0.545，位于 65.006 秒。前两条相对校准搜索中心（16.992、39.329 秒）的差为 -0.011、+0.636 秒。“停”的素材投影分量为 -43.59 dBFS，相关峰很弱。`peak_abs_ncc` 是素材波形与捕获波形的归一化相关值，不是可懂度或 ASR 置信度。尤其 0.077 只能记为弱相关峰，不能据此确认素材确实进入了麦克风，也不能证明它完全不存在。
4. 三个打断窗口的本机 Whisper 转写均未识别出对应素材文案，结果包含与目标话语无关的识别文字。问题素材的三个问题句则均正确转写。整段 admitted PCM 的 Whisper 输出也没有可靠识别出打断文案。完整命令、分段原文、窗口转写、文件哈希与限制见本机 gitignored [Whisper/NCC 收据](../../../outputs/acceptance/run-20261011-stop-control-a-b/whisper-ncc-receipt.json)。`stop-pcm-diagnosis.json` 中 `offline_asr.status=unavailable_missing_model` 只表示该脚本的 SenseVoice 模型不可用，不表示 Whisper 未运行。`bridge.log` 仅记录文本长度及拒绝原因，不含 ASR 原文；30 条 session-scoped `not_stop_word` 记录不能逐条映射到具体素材或词语。

**固件代码审计与归因边界：**
- VoCat 配置要求设备端 AEC，启用 WebRTC noise suppression；AFE 配置为 `AEC_MODE_VOIP_HIGH_PERF`、`AEC_NLP_LEVEL_VERYAGGR`，播放期采集与上传共享 AFE 处理输出。麦克风输入增益为 36 dB，reference gain 使用默认值。设备能力声明 AEC reference 为 `software_post_gain_pre_i2s`，同时明确 `aec_reference_verified=false`。
- 这些配置指出 P1-07 应优先核验 reference 实际波形/时序，以及强 NLP 与 WebRTC NS 在双讲时是否压低近端声音。代码配置本身不能证明 AEC reference 错位，也不能证明 AEC 或 NS 是本轮失败的根因。
- 现有 PCM 是经过设备 AFE、上行编码和传输后的 admitted PCM。当前收据没有 pre-AEC、post-AEC 或 post-NS 分路录音，因此只能确认离线 Whisper 未从三个打断窗识别出对应文案，不能断言 FunASR 收到的内容或锁定具体处理阶段。

本地诊断工具与本收据已入库。bench 诊断固件（`pcmarm` / `pcmdump` lane journal）已随 #200 合入并完成刷机烟雾；诚实通道为 `play_pre_i2s`、`pre_afe_mic`/`pre_afe_ref`、`post_afe`（无同时 post-AEC / post-NS 分路）。三窗声学采集（只播、只近端、双讲）与停止词重测仍待用户当场授权；本批不采集孩子真实声音、不建热词表、不改生产 env。
