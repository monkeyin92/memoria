# AGENTS.md — overlay/files/main/memoria

## OVERVIEW
设备端核心逻辑：协议、激活、会话/回放状态机、离线菜单、唤醒与电源策略。C++17、flat layout、`namespace memoria`。

## WHERE TO LOOK
| 任务 | 位置 |
|------|------|
| WebSocket 会话/帧时钟/暂停恢复 | `memoria_protocol.cc` (3230 L，本目录心脏) |
| 身份 proof / 绑定 / 409 幂等 | `memoria_bootstrap.cc` |
| 离线菜单与故事 | `memoria_offline_menu.h` (header-only) |
| 播放供水计量（I2S underrun 语义） | `main/audio/memoria_playback_supply_meter.h` |
| 唤醒/停止词/静音时段/电源 | `memoria_wake_mode.h` / `memoria_stop_keyword.h` / `memoria_quiet_hours.h` / `memoria_power_policy.h` |
| 诊断/bench（仅 bench 镜像） | `memoria_diag_pcm.h` / `memoria_bench_snap.h` |

## CONVENTIONS
- 每个 header 必须无 ESP-IDF 依赖、可直接 host 编译（tests 有 `test_header_stays_free_of_esp_dependencies` 守卫）；时钟/时间戳由调用方注入。
- 线格式与存储格式用 `static_assert` 钉死大小（`MemoriaAudioFrameHeaderV1` == 30 等）；时序预算同样 `static_assert`（`memoria_encode_budget.h`）。
- 编译期开关写成 `#define CONFIG_X 0` + `#if`（如 `CONFIG_MEMORIA_BENCH_SERIAL`），常量 `kPascalCase`。
- 大状态机拆成 header-only 模块，与 `.cc` 无头的部分区分；协议/激活客户端是唯一大 `.cc`。
- bench 代码 `#if CONFIG_MEMORIA_BENCH_SERIAL` 门控，产品镜像里编成空。

## ANTI-PATTERNS
- `memoria_protocol.cc` 显式禁令：不打印 HTTP 响应体（校验错误会回显输入）；重试跑在独立 task，绝不阻塞 Application 主循环；OTA 绝不打断进行中的对话；发送互斥锁绝不跨 `WebSocket::SendControlFrame`；暂停期丢弃的陈旧帧绝不推进播放或复活本地停止的 generation；`generation.cancelled` 永不当作正常完成。
- `memoria_playback_supply_meter.h`：计数的 supply 等待绝不报告为已证实的 I2S/DMA underrun；等待后绝不重读 streaming flag；generation 0（本地 cue）永不开 episode；自身无锁，调用方持有 audio queue 互斥锁。
- 唤醒必须走 `TapDeviceFor`/`DispatchTapWakeAction`，不得用 `UsbWakeDecisionFor`；恢复路径不得调 `WakeWordInvoke`；`kResumeListening` case 恰好出现一次。
- v1 会话固定 24 kHz（`memoria_audio_frame.h`）；本地数据擦除只按显式 `(namespace,key)` 列表，不得碰 `activation_ctr` / `memoria_identity`。

## NOTES
- 评分 15（file 3 + code 2 + config 1 + symbol 4 + export 2 + ref 3，LSP 未测得处按报告符号面保守计），核心知识域。
- 对应测试在 `firmware/esp32/tests/`（见其 AGENTS.md），本目录不重复。
