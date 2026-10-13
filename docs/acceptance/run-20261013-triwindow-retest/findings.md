# 2026-10-13 软件 AEC 参考修复后三窗复测

对照 [修复前三窗](../run-20261013-triwindow/findings.md)。本轮刷入含 `memoria_software_aec_ref.h` 的 bench 镜像，再跑只播 / 只近端 / 双讲。诊断交付。不关闭 N-10，不置 `aec_reference_verified`。

## 结果摘要

| 窗 | 指标 | 修复前 | 复测 | 判读 |
|---|---|---:|---:|---|
| 只播 | `pre_afe_ref` dBFS | −78.3 | **−19.9** | 参考有播放能量（测得） |
| 只播 | ref↔play NCC | −0.04 | **0.996** | 参考对齐播放（测得） |
| 只播 | 粗 ERLE mic→post dB | 5.2 | **22.8** | 回声大幅压低（测得） |
| 只播 | `post_afe` dBFS | −37.5 | **−54.3** | 同上 |
| 只近端 | 停止词 NCC mic→post | \|0.56\|→\|0.58\| | \|0.52\|→\|0.57\| | 安静近端仍保留（测得） |
| 双讲 | `pre_afe_ref` dBFS | −77.6 | **−21.5** | 参考可用（测得） |
| 双讲 | 粗 ERLE dB | 4.3 | **23.9** | 远端消除变强（测得） |
| 双讲 | 停止词 NCC mic→post | \|0.18\|→\|0.21\| | \|0.17\|→\|0.19\| | **近端仍不可分**（测得） |

双讲首轮 `pcmdump` 曾因串口 chunk 损坏失败；同条件重采 journal `000315be` intact。

## 条件

- 设备 MAC `90:e5:b1:d7:83:2c`，bench（`MEMORIA_BENCH_BUILD=1`，build 24）含 patch `0037-memoria-software-aec-reference`。
- Mac 音量 50、非静音；结束后恢复 19 与静音，并刷回产品镜像。
- 刺激与修复前相同（`story-stop-a` / `story-stop-a-stop`「别说了。」）。
- 原始 WAV 在本机 `outputs/acceptance/run-20261013-triwindow-retest/`。

## 诊断

**已证实。** 软件参考注入使 `pre_afe_ref` 在播放期与 `play_pre_i2s` 同量级并对齐。只播粗 ERLE 从约 5 dB 升到约 23 dB。安静时停止词仍进 mic/post。根因「参考近静音」在本机上闭合。

**尚未证实。** 双讲时停止词在 `post_afe` 上仍与只播假峰同级。远端消得更干净了，近端仍未浮出。推断（非测得根因）下一刀在 AFE NLP / WebRTC NS 的双讲保留，或参考时延 / 增益微调。不得据此关闭 N-10，也不得把 `aec_reference_verified` 置 true（尚无 T1–T14，且双讲近端未过）。

## 身份摘录

```
code=software AEC ref ring (WritePlay @24 kHz pre-I2S, ApplyToFeed before resample)
wired=bench pcmarm/pcmdump + 对照 run-20261013-triwindow
enabled=未改生产 env；aec_reference_verified 仍 false
verified=参考能量与只播 ERLE；安静近端保留；双讲停止词仍不可分
```
