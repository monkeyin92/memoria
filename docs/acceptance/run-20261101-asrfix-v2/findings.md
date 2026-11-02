# ASR failover 备份时序与 volume 20 真机复测（2026-11-01）

## 结论

`memoria-agent:20261101-asr-failover-v2` 在 Mac 音量 20 下跑了 22 分钟 child_tapwake，63 轮、62 次回复、1 次 no_reply（t045-bye 是预期会话结束）。首字 P50 2.01 s、P90 2.52 s、P95 2.71 s、P99/最慢 3.19 s。P90 比 19:08 那轮 volume 30 的 3.17 s 低 0.65 s，最慢从 6.3 s 降到 3.19 s。门禁仍未过，P90 2.52 > 2.0，最慢 3.19 > 3.0。

## 部署

- 候选 tag `20261101-asr-failover-v2`，commit `397007d6`，base `memoria-agent:20261101-batch-46eca33e`。
- 组件切流 2026-11-01T15:13:32Z，bridge healthy，rollback `memoria-agent:rollback-20261101-asr-failover-v2-pre`。
- 控制面仍是 `memoria-control-api:20261029-plan-head-start-v1`。
- 切流用 `--allow-manual-base-image`，因为现网 bridge 同时是 runtime base，没有独立的 runtime-base 镜像；release gate 在 dry-run 和切流前各跑一次通过。

## 真机

- 设备 `/dev/cu.usbmodem2101`，Mac 音量 20（开测前 19，结束恢复），22 分钟，scenario `child_tapwake.json`。
- 23:22:37–23:48:07 CST，63 轮、62 次回复、1 no_reply（t045-bye 预期结束）。时钟 offset 约 0.03 s。
- 收据目录 `devicetest/run-20261101-232237-soak-tapwake/`，原始输出 `outputs/acceptance/run-20261101-232237-soak-tapwake/`。

## 分段

| 段 | p50 | p90 | max |
| --- | --- | --- | --- |
| 收音 | 0.39 | 0.84 | 1.36 |
| ASR | 0.91 | 1.18 | 1.31 |
| LLM | 0.07 | 0.54 | 1.23 |
| TTS | 0.42 | 0.50 | 0.55 |
| 下发播放 | 0.17 | 0.25 | 0.32 |

54/62 轮最大段仍是「ASR」，即 final 到 commit 的提交路径；5 轮是「收音」，3 轮是 LLM。这次 ASR 备份时序修复把「收音」p90 从 1.13 降到 0.84、max 从 4.23 降到 1.36，但提交路径没有动，所以 P90 仍由 `final_to_done_ms` 主导。

## ASR 无文本

- 1 次 `asr_empty_class=low_rms` 的 ASR tail timeout，3 次 `empty_media_turn`。
- 0 次 `final_result_timeout`，0 次 `rescue timed out`，0 次 `both providers failed`。
- 上次 190839 的 t052-dino 无回复这次回复了，延迟 1.51 s。这次没有触发 final 超时，所以新备份时序的救援路径主要由单测覆盖，真机没有复现慢 final。

## 对照

| run | 音量 | 回复 | P50 | P90 | 最慢 | no_reply |
| --- | --- | --- | --- | --- | --- | --- |
| 190839 | 30 | 54 | 2.17 | 3.17 | 6.30 | 4 |
| 214518 | 30 | 37 | 2.05 | 2.65 | 3.92 | 0 |
| 232237 | 20 | 62 | 2.01 | 2.52 | 3.19 | 1（预期） |

## 未过项与边界

- 门禁 P90 ≤ 2.0、最慢 ≤ 3.0 仍未过。剩余大头是提交路径和 LLM 尾。t027 3.19 s 的 LLM 段 1.23 s，t024 2.72 s 的 LLM 段 1.02 s。
- 停止与附和：8 次打断 6 次通过，2 次「嗯」1 次通过；t016/t061 的「停」未停，t018/t063 的「嗯」未停。这是音量 20 下的已知声学问题，不是这次 ASR failover 改动引入的。
- 尾窗 180 s 未观察到 idle，最后 screen off (idle) 在 23:45:58；keepawake verdict 因此记 tail timeout。这个不是回复链路的失败。
- `voice_soak_verdict.py` 当前不接受 `--max-reply-latency-p90` 和 `--max-reply-latency-max`，本报告的分位数由 `summary.json` 按 `scripts/voice_soak_analyze.py` 的 `pct` 口径计算。harness 参数需要单独修。
