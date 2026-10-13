# 2026-10-13 三窗 bench PCM（只播 / 只近端 / 双讲）

用户授权在 bench 镜像上做受控三窗采集。本轮用 `pcmarm` / `pcmdump` 取四通道 3.0 s 窗，刺激为既有 Doubao 素材「给我讲一个故事吧」与「别说了。」。不采孩子真声，不建热词表，不改生产 env，不改产品代码。诊断交付，不是修复。

## 结果摘要

| 窗 | 设备态 | journal | intact | 结论要点 |
|---|---|---|---|---|
| 只播 | `speaking` | `0001cad7` | true | `play_pre_i2s` 正常（−20.7 dBFS）；`pre_afe_ref` 近静音（−78.3 dBFS，peak 28） |
| 只近端 | `listening` | `00022bb9` | true | 「别说了」进入 `pre_afe_mic`（NCC \|0.56\|）并保留在 `post_afe`（\|0.58\|） |
| 双讲 | `speaking` + 近端 | `00027a79` | true | 近端在 mic/post 上的 NCC 掉到 \|0.18\| / \|0.21\|，与只播假峰同级；AEC 未把停止词捞回来 |

| 通道 dBFS | 只播 | 只近端 | 双讲 |
|---|---:|---:|---:|
| `play_pre_i2s` | −20.66 | （空，未播放） | −19.97 |
| `pre_afe_mic` | −32.20 | −46.48 | −32.00 |
| `pre_afe_ref` | −78.28 | −90.37 | −77.61 |
| `post_afe` | −37.46 | −48.42 | −36.32 |

只播窗 mic→post 能量比约 5.2 dB（记作粗 ERLE，不是标准 ERLE 规程）。三窗 journal 均 `no_drops` / `intact`。

原始 WAV、manifest、采集脚本与逐窗事件在本机 gitignored `outputs/acceptance/run-20261013-triwindow/`；串口 `outputs/serial/robot-20261013-triwindow.log`。可重跑分析见同目录 `lane-analysis.json`（由本机脚本从 WAV 算出）。

## 条件

- 设备 MAC `90:e5:b1:d7:83:2c`，串口 `/dev/cu.usbmodem101`。
- 固件为 #200 bench（`MEMORIA_BENCH_BUILD=1`，`MEMORIA_FIRMWARE_BUILD=24`，`ota_0`）。采集后 `flash.sh --port /dev/cu.usbmodem101 --build` 刷回产品；串口见 `MEMORIA_FIRMWARE_BUILD=24` 与 `activating -> idle`，无 `MEMORIA_BENCH` / `pcmarm` 控制台行。
- Mac 开测前音量 19、静音；采集音量 50、解除静音；结束后恢复 19 与静音（已核实）。
- 刺激：`outputs/voice-bank-stop-control-a-b/story-stop-a.wav`（故事请求）、`story-stop-a-stop.wav`（「别说了。」，约 0.95 s @24 kHz）。
- 常驻 `voice_soak_serial_logger.py`；驱动 `outputs/acceptance/run-20261013-triwindow/capture_triwindow.py`（USB `wake` + `pcmarm`/`pcmdump`）。未部署、未 OTA。

## 诊断

**前提（已写明）。** 此前多轮停止失败常被当成「停止规则 / ASR 词表」问题。共享前提是：播放期上行里仍有可识别的近端「别说了」。本轮三窗直接量这条前提在 AFE 前后是否成立。

**1. AEC 参考几乎没有播放能量（测得）。**  
只播与双讲时 `play_pre_i2s` 约 −20 dBFS，而同窗 `pre_afe_ref` 约 −78 dBFS（peak ≤ 28）。参考比播放低约 58 dB。只播时 `ref` 对 `play` 的 NCC 峰值约 0.04，不能当作可用参考。能力声明里的 `aec_reference_verified=false` 与此一致；本轮把它从「未验证」推进到「在本机三窗上能量不可用」。`pre_afe_ref` 是 ES7210 进 AFE 的 R 路，不是 `play_pre_i2s` 的软件拷贝（见 #200 / `memoria_diag_pcm.h`）。

**2. 麦克与安静期近端路径是通的（测得）。**  
只近端窗设备在 `listening`，Mac 播放「别说了」。`pre_afe_mic` NCC \|0.563\|（lag 0.797 s），`post_afe` \|0.580\|。安静时 AFE 没有抹掉停止词。麦克硬件与上行链路不是「死麦」。

**3. 双讲时近端被远端口径压住，post_afe 也没救回来（测得）。**  
双讲窗在 `speaking` 中途 arm，并在窗内播放同一停止素材。`pre_afe_mic` 上停止词 NCC 落到 \|0.179\|，`post_afe` 为 \|0.207\|。只播窗（无近端）假峰约 \|0.22\| / \|0.19\|，说明双讲峰达不到「安静近端」那种可分性。只播 mic（−32.2 dBFS）比只近端 mic（−46.5 dBFS）大约 14 dB：回声能量本身就盖过单独近端。粗 ERLE 仅约 5 dB，与「参考近静音」相符。

**4. 对 N-10 的含义（推断，附边界）。**  
在参考不可用、双讲 post 仍听不清停止词的条件下，继续拧云端停止规则或先上热词表，解决不了「播放期近端进不了可识别上行」这一层。下一步应先修或重接 AEC 参考（使 `pre_afe_ref` 在播放期有与 `play_pre_i2s` 可比的能量与时序），再谈 NLP/NS 参数或停止矩阵扩测。本轮没有改固件音频图，也没有证明某一寄存器位错；它证明的是分路能量与 NCC 事实。

**不是本轮结论的东西。**  
未跑 Whisper/FunASR；未测音量 30；未关闭 N-10 / N-5；未置 `full_duplex_verified`；未宣称 Exact DAC。NCC 是波形相关，不是可懂度。

## 身份摘录

```
code=bench MEMORIA_BENCH_BUILD=1 build 24 + pcmarm/pcmdump
wired=唯一串口 logger + Mac 扬声器刺激（音量 50）
enabled=既有 #200 lane journal；生产整栈未改
verified=三窗 journal intact；参考近静音；安静近端可达；双讲近端在 mic/post 不可分
```

证据日期 2026-10-13。N-10 / N-5 / P1-07 保持开放。`direct_real_device_verified`、`full_duplex_verified`、`student_safety_loop_verified` 仍为 `false`。
