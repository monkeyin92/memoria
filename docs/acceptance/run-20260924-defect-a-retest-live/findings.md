# 2026-09-24 缺陷 A 真实设备复测收据

## 结论

- **缺陷 A 核心边界：通过。** 同一主会话内按 3/5/8 秒停顿追问“后天呢？”均形成独立话轮，bridge 日志出现非空 `media playback-followup endpoint boundary=… endpoint=…`，每轮均完成实际听见与播放结束；未见旧的回声与追问合并。
- **30 分钟基础长稳：通过但带观察项。** 采集窗口内没有二次复位、panic、`RTC_SW_CPU_RST`、服务重启或 uptime reset；但设备发生多次 TLS/WebSocket 断开并自动重连，严格“无连接异常”口径不能宣称通过。
- **完整 P0-03：保持开放。** 工具查询问答未完成，>45s/B/D 长答、部分下发失败、待机/表情及点屏/摇晃/短拍/BOOT 回归仍未覆盖；`direct_real_device_verified=false`、`full_duplex_verified=false`。

## 采集与身份

- 采集窗口：`2026-09-24 11:45:34.122697` 至 `12:15:34.489031` CST，`capture_status=completed`，`exit_reason=duration_elapsed`，时长 1800s。
- 采集工具：`scripts/voice_session_capture.py`；串口 `/dev/cu.usbmodem101`；`serial_opened=true`、`serial_data_writes=false`、`boot_reset=false`、`services_restarted=false`、`log_stream_health=healthy`、`cleanup_errors=[]`。
- 服务端候选：`memoria-agent:20260921-defect-a-followup-endpoint`，commit `2a33a50e85d09dc61944ac860e311d247a1020e2`。
- 控制面身份仍冻结为 `20260901-0945-wake-word-whitelist` / `7ca3d4ec`；这是线上临时对齐值，不是本轮候选身份收敛证明。
- `capture.json` 的 `source_revision.git_head=8476b462...` 是采集工作区版本，不是服务端运行候选。固件 receipt 来自 2026-09-15 的历史刷写收据，`read_from_board_this_run=false`、`boot_verified=false`、`real_device_conversation_verified=false`，不能据此声称本轮重新读取了板上固件版本。

## 3/5/8 秒续问格

主会话：`session_id=b18fede9-5711-49d1-b205-a3044d57ce70`，`stream_epoch=1995`。

| 停顿格 | playback boundary | endpoint | turn / generation | 结果 |
|---|---:|---:|---|---|
| 3s | `344000` | `404960` | `turn_id=3 / generation=4` | `text_len=4`，`actual_heard=true`，`playback_ended=true` |
| 5s | `593280` | `678080` | `turn_id=4 / generation=5` | `text_len=4`，`actual_heard=true`，`playback_ended=true` |
| 8s | `810240` | `964320` | `turn_id=5 / generation=6` | `text_len=4`，`actual_heard=true`，`playback_ended=true` |

对应日志在 [bridge.log](bridge.log) 中；每个非空 endpoint 后均有独立 `turn_committed` 和完整播放交付。中间出现的 `text_len=0` endpoint 是空输入清理，不计为一次失败的“后天呢？”续问。

## “今天适合散步吗？”工具查询

- 该句形成 `turn_id=6`，`boundary=1083200`、`endpoint=1192000`。
- `generation=7` 播放“我稍等，查询一下”并完成 `actual_heard=true`、`playback_ended=true`；随后 realtime search 返回了 206 字符、约 5020ms 的结果。
- 最终回复 generation 因 `conversation_end_explicit` / `output_task_cancelled` 在首帧前 `preempted`，没有完成最终工具查询回答。因此这条不计为完整问答通过；该首帧前取消也不计为缺陷 A 的播放交付失败。

## 30 分钟长稳与操作者观察

- 操作者报告长稳阶段约在第 10、12、14 分钟各唤醒并提问一次。按本次采集的绝对串口时间，对应主要唤醒为 `12:07:22`、`12:10:18`、`12:14:52` CST；三次均建立会话、收到独立响应并回到 idle。
- 期间多次出现 `mbedtls_ssl_fetch_input error=76`、`EspSsl: SSL receive failed: -76` 与 `MemoriaProtocol: Device WebSocket disconnected`，随后自动重新建立 HTTPS/WebSocket 并恢复交互。因此“长稳基础行为”通过，但 TLS/WSS 重连频率、重连期间体验和根因仍需单独收敛。
- 串口在 `12:15:10` 还记录到一次未由操作者明确归类的唤醒；采集随后结束，未把它计入上述三次人工长稳样本或误唤醒结论。

## 独立硬件观察项

采集早期及约 `12:05:14` 出现 `BMI2_ESP32: I2C read reg 0x03 len 24 failed: ESP_ERR_TIMEOUT`。本轮没有复位或 panic；该 IMU/I2C 问题独立于缺陷 A，不得归因或计入缺陷 A 判定。

## 判定边界

本轮可以把缺陷 A 的**核心续问边界**从“待真机复测”更新为“真实设备核心证据通过”；不能据此关闭 P0-03，也不能把设备整体 `direct_real_device_verified` 或 `full_duplex_verified` 改为 `true`。下一步应先收敛工具查询最终交付与 TLS/WSS 重连观察，再继续 P0-03 的长答、故障注入和交互回归矩阵。

## 离线诊断：工具查询最终回答被取消的根因（2026-09-24 同日补记）

依据仅为本目录 `bridge.log` 与 `serial.log`（`agent.log` 为 0 字节，采集时 agent 日志流未取到内容）；未保存原始音频，以下对“3 字文本来自底噪”的判断为**极可能**，不是确证。

时间线（UTC `03:51`，`session=b18fede9`，`turn_id=6`）：

| 时刻 | 事件 |
|---|---|
| 08.11 | generation 7“稍等”播完，`speaking→tool_waiting` |
| 10.04 | 设备 VAD start → `tool_waiting→user_speaking`，话语权归用户 |
| 10.18 | realtime search 返回（206 字符 / 5020ms），回答 `media output deferred reason=floor_blocked` |
| 10.0–17.6 | 设备连报 3 段 VAD；FunASR 均 `vendor_silent`，SenseVoice 救援前两段为空 |
| 17.90 | 第 3 段救援得 3 字，preview 阶段被拒 `straddles_committed_without_timing` |
| 18.23 | 空话轮尾超时退役（`turn discarded after ASR tail timeout`），generation 8 开始合成 |
| 18.60 | straddle 恢复路径把 3 字判为告别 → `early conversation-close endpoint source=final` → generation 8 首帧前 `preempted` |

逐段上行能量（`media_asr_boundary` 的 int16 rms / peak）：本轮真人话轮 rms 2812–5269、peak 均为 32768（削波）；上表 3 段为 265/1850、306/1695、**389/2616**，与同轮空闲底噪（rms 143–423）同量级。触发告别的正是最后一段。长稳阶段 3 次告别来自 FunASR 实时 partial 或语义 final，与操作者描述一致，判为真实告别。

两个缺陷：

- **D1 底噪占住话语权**：设备 VAD 被底噪触发，且其边界 rms 不能区分真话与噪声（真实话轮边界也只有 0.0006–0.0014）。两路 ASR 已判空后话语权仍不释放；每段新 VAD 延长同一空话轮并重置 2.5s 尾超时，工具回答因此被压约 8s，用户在“稍等”后近 10s 无声。**未修复**，方案待定（见下）。
- **D2 近噪声救援结果可结束会话**：`_recover_straddling_live_query_final` 对被拒的救援结果仍做告别判定；唯一的低能量保护只覆盖 `CROSS_SENTENCE_OVERLAP`，且阈值沿用 SenseVoice 入口门槛（rms 100 / peak 350），低于本房间底噪。**已在本地修复，未部署**：救援结果携带所救 PCM 段的 `rescue_rms`/`rescue_peak_abs`，两种被拒形状下，rms < 1000 且 peak < 8000 的救援告别不结束会话。代价是：音量很低的真实告别不会立即待命，改由 owner-silence 30s 超时回到待命。回归 `test_device_straddling_rescue_farewell_needs_speech_energy`（底噪组在去掉修复后失败、真话组保持告别）。

D1 候选方案（未实现，需评审）：两路 ASR 对某 VAD 段都给出“空”结论时立即退役该空话轮并恢复被 `floor_blocked` 的输出，不等尾超时；另给排队的工具回答设“无文本证据占用”的最长等待。须回归 3/5/8s 续问格，不得回退缺陷 A。

附带观察：真人话轮上行 PCM 普遍削波（peak 32768，DTLN makeup gain 8.0 / 18 dB），可能影响识别质量，需单独评估，不计入本判定。
