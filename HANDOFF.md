# Memoria 当前交接

本文件只记录当前有效状态、紧邻回滚、生产操作和下一验收。历史发布过程不再保留；每次发布原地更新本文件。跨任务优先级与完成标记统一见 `TODOLIST.md`，这里保留现场步骤和运行证据。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-15
resume_checkpoint: p0_metering_lifecycle_firmware_flashed_boot_and_capture_finalization_passed_pending_playback_20260915
firmware_face_acceptance: conversation_face_v3_flashed_awaiting_idle_and_five_expression_photos
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.84
miniprogram_account_device_sync: uploaded_0.8.84_phone_desktop_pending
production_readiness: ready
production_readiness_observed_at: 2026-09-15T16:00:49+08:00
offsite_backup_enabled: false # 用户 2026-09-14 决定：项目验证阶段暂不启用自动备份/异地副本；WAL 归档仍在写且无人裁剪
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
current_work_order: vocat_interrupt_assist
code: complete
wired: firmware_0024_and_agent_empty_input_resume
enabled: production_agent_bridge_edge_true_device_audio_mode_interrupt_assist_empty_input_resume
verified: production_runtime_provider_model_inference_identity_safe_board_boot_secure_device_onboarding_owner_silence_standby_and_device_wake_ack_heard_server_release_health
empty_input_resume_code: regression_and_full_release_gates_pass
empty_input_resume_wired: python_media_coordinator_dispatch_and_empty_tail_retirement
empty_input_resume_enabled: agent_bridge_20260913_p0_empty_input_resume_v1
empty_input_resume_verified: component_checks_and_epoch1935_two_weather_playback_receipts_user_hearing_confirmed
empty_input_resume_evidence_at: 2026-09-13T23:47:21+08:00
weather_user_acceptance_date: 2026-09-13
farewell_immediate_standby_verified: false # 播后已过；播放中语音告别仍受签名策略限制，不能总体标真
# 历史 0024/AEC 播放期 VAD 修复状态；不要与当前 0025 计量实现混读。
firmware_playback_capture_code: historical_0024_clean_build_and_esp32_host_tests_pass
firmware_playback_capture_wired: historical_resolved_device_aec_and_board_compile_gate
firmware_playback_capture_enabled: historical_true_app_only_flashed_20260914T1506CST
firmware_playback_capture_verified: historical_app_full_readback_identity_and_non_app_partitions_unchanged_boot_idle
firmware_playback_capture_evidence_at: 2026-09-14T15:07:14+08:00
# 新候选已 app-only 刷入、回读并启动；实际播放计量与听感尚未验收。
firmware_playback_supply_meter_code: true # observation-only lifecycle fix, local regressions passed
firmware_playback_supply_meter_wired: true # candidate audio service reset/queue/exact-token lifecycle
firmware_playback_supply_meter_enabled: true # app-only flash and matching-ELF boot verified
firmware_playback_supply_meter_verified: false # pending new-candidate device metering and hearing
firmware_playback_supply_meter_evidence_date: 2026-09-15
firmware_playback_supply_meter_worktree_overlay_hash: 24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3
firmware_playback_supply_meter_candidate_app_sha256: 7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65
firmware_playback_supply_meter_candidate_elf_sha256: da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2
firmware_playback_supply_meter_candidate_dir: outputs/acceptance/run-20260915-p0-03-metering-lifecycle-local/final
# 本轮板卡回读与启动证据；boot 记录独立于不可变 postflash 收据。
firmware_playback_supply_meter_board_overlay_hash: 24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3
firmware_playback_supply_meter_board_release_head: d1ad38fe0d9b36eb9057d8eb97e782b8cc8066d9
firmware_playback_supply_meter_board_upstream_commit: e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5
firmware_playback_supply_meter_board_app_sha256: 7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65
firmware_playback_supply_meter_board_elf_sha256: da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2
firmware_playback_supply_meter_board_receipt: outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/postflash.json
firmware_playback_supply_meter_board_boot_evidence: outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/boot-verification.json
firmware_playback_supply_meter_board_boot_verified: true
firmware_playback_supply_meter_board_real_device_conversation_verified: false # new candidate; old-board hearing result is not inherited
firmware_playback_supply_meter_board_metering_verified: false # boot-check only; no actual playback yet
firmware_playback_supply_meter_real_device_conversation_verified: false
capture_lifecycle_code: true
capture_lifecycle_wired: true
capture_lifecycle_enabled: new_cli_boot_check_completed_20260915T185219CST
capture_lifecycle_verified: boot_capture_duration_elapsed_three_streams_stopped_no_forced_kill_conversation_pending
pre_roll_code: not_implemented
pre_roll_wired: false
pre_roll_enabled: false
pre_roll_verified: false
wake_ack_playback_vad_fix_code: complete
wake_ack_playback_vad_fix_wired: session_accepted_signed_allowed_barge_in_gate
wake_ack_playback_vad_fix_enabled: true_app_only_flashed_20260914T1506CST
wake_ack_playback_vad_fix_verified: real_device_greeting_no_disconnect_actual_heard_and_explicit_farewell_pass
wake_ack_playback_vad_fix_evidence_at: 2026-09-14T15:11:29+08:00
# 当前板上 0025 候选；历史 0024/AEC 候选见「板卡与固件」。
on_device_app_sha256: 7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65
on_device_app_elf_sha256: da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2
on_device_app_build_recorded_at: 2026-09-15T17:36:12+08:00
on_device_app_verified_at: 2026-09-15T18:51:44.111+08:00
nearest_rollback_app_sha256: dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d
nearest_rollback_app_path: outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/rollback-app.bin
previous_aec_candidate_app_sha256: 6bcca089d996cd7cb3c25daca9dccfacc45554b426a5497345ddf21e5c22001b
production_runtime_verified: true
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: pending
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
device_id: dev_atk_a4cb8fd6095c
speaker_profile_id: 1b5b577b-669e-4573-b9b1-ea1dd8122ee4
speaker_profile_status: active
last_wake_epoch: 1897
idle_tap_pat_operator_verified: true
```

`full_duplex_verified` 只有真实硬件 AEC、双讲、打断、连续会话和 Actual Heard 证据全部通过后才能改为 true。在此之前产品不得宣传全双工。小程序仅 profile 页允许经授权有界录制自定义音色样本，不承担实时对话、手机声纹登记或实时媒体回滚职责。

当前工单 `vocat_interrupt_assist`：播放期保持采集，Agent barge-in 跟协商 `audio_mode`。LiveKit 设备路径仍半双工。Direct Edge 把 `assistant_expression` 转成板子 `screen.expression`。ATK ES8388 半双工投资人 Demo 已退役。Control 默认镜像可后切，只影响新设备。0024 已刷；Agent 依次切过 `20260910-1011` → `20260910-1526` → `20260910-1820`。

2026-09-14 补充：板卡侧新增「按签名策略决定是否在播放期上报 VAD」的把关（commit `bdf2047`），修掉唤醒问候回声触发的越权 `vad.start` 断链。该把关只收窄设备行为，不放宽任何门禁；`allowed_barge_in` 仍以服务端签名为准。

2026-09-14 USB 对照（`outputs/acceptance/run-20260914-0925-usb-goodbye/`）：epoch **1936** / session `0615d48c-3bdf-4a6c-a7c6-7543f4ffd96b` 播完告别走 `conversation_end_explicit`，随后串口回 idle；epoch **1937** / session `f216e1e0-1bfa-41b8-a259-457d0f2df2eb` 播放中告别却无 Speaking 期设备 VAD，仍报 `playback_completed`，播后三段空 ASR 后靠 `owner_silence_timeout` 关闭。Edge 09:29:08.032 关闭、串口 09:29:08.037 idle，只说明日志对齐没有秒级 UI 残留，不能当作物理屏幕延迟 5 ms。

根因已修于候选 **`76ba6ad3b68a042566f97dc42f6f8101c32b20af`**（已推送）：锁定 upstream 的 `USE_DEVICE_AEC` 依赖未包含 Memoria 板型，manifest 的 `=y` 被静默丢弃，导致默认 AutoStop 在 Speaking 关闭语音处理。0001 补板型依赖，板级编译保护和 `check-overlay.sh` 检查最终配置；新增 CI firmware job 执行全部固件宿主回归。干净重放/构建、依赖锁门禁与 **203 项**定向回归通过，生成的 `sdkconfig.h` 已有 device AEC/audio processor 宏，server AEC 关闭。未调静音超时、增益、DTLN、声纹权限或服务器，AEC 残余/双讲仍未验收。旁查 `CONFIG_FLASH_EXPRESSION_ASSETS=y` 也是当前无效配置，但实际 default assets 承载唤醒命令词，本轮不改该路径。

**旧 USB/未刷写阻塞已解除。** 2026-09-14 上午的只读探测失败已被当日 15:06 的 app-only 刷入、完整回读/受保护分区比对与 15:11 真机复测取代，详见下方「唤醒问候播放期越权 VAD 断链修复」。不要再按旧阻塞重复刷写；播放期语音打断仍受签名策略限制，未获 AEC/双讲验收。

epoch **1897** 真机（13:56 CST，session `b910a0ee`）与 **1899** 复测（17:30 CST，session `7c465319`）复现同一组缺陷，分两轮修：

1. **filler 连播两遍**。1897：边车把同一句 11 字提问识别两次，turn 2/3/4/5 提交四次；可听序列是两段约 2s 输出之后才是 8.3s 正文。1899 复测仍在（说明第一轮修复不足）：提问同样提交两次（turn 2、turn 3），**两次提交各开一次委派、各播一遍 ACK**——`generation-2` 1.19s（被 turn 3 抢占）+ `generation-3` 1.95s，之后才是 `generation-4` 6.5s 正文。
   - 第一轮（`b83e9b7` / 组件 `20260910-1526`）：只给 **deep result 前缀** 加了会话级去重 `_live_lookup_filler_already_audible`，没门控 ACK 本身的发出，所以 1899 仍听到两遍。该轮修复本身有效（1899 的 `generation-4` 前缀已被剥掉）。
   - 第二轮（`df41596` / 组件 `20260910-1820`）：把 `_live_lookup_filler_already_audible` 也用作 **ACK 发出**的闸门，并新增 `_forget_live_lookup_filler` 在委派交付答案时释放「本次查询突发」记忆，保证之后的新提问仍会播自己的提示。复现测试 `test_duplicate_turn_commit_does_not_emit_a_second_lookup_ack`（断言 ACK 只发一次，修前 2 == 1 红、修后绿）。
   - 遗留取舍：重复提交若在 ACK 播完前抢占它，用户会听到**一句被截短的**提示且不重播（例如 1899 的 1.19s）。要「完整播一遍」，得让重复提交不抢占正在播的 ACK，属 turn-commit 层改动，本轮未做。
2. **聆听中残留**。1897 靠 `owner_silence_timeout`（15.3s）关闭；1899 已改为 `conversation_end_explicit` 关闭——告别路由这轮生效了，但**屏上仍停留 15~18s**：18:30:42.42 进入 `user_speaking` 后，两次 `media turn discarded after ASR tail timeout`（endpoint 153920 / 248000，均 `empty+vendor_silent`）耗掉约 14s，直到 59.51 才拿出 `text_len=2`（「再见」）并关闭。被判空的两段并不相同：endpoint 153920 那段 `rms 1231 / peak_abs 32768 / provider_pcm_clipping_detected=true`，endpoint 248000 那段才是 `rms 292 / 无削波`。所以「1899 无削波、不是回声污染」不能作为整体结论，第一段确实削波。是否真的落在播放窗口内无法从现有材料判定（epoch 1899 的 tap WAV 当时未下载，服务器 `/tmp/media-pcm-tap` 已随容器重建清空），复测时按「媒体与音频」一节记 `provider_pcm_rms` + `provider_pcm_clipping_detected`。

串口结论也要改：`/dev/cu.usbmodem101` 实测正常，固件每 10s 输出一条 `SystemInfo`（probe 证据 `outputs/acceptance/run-20260911-serial-probe/serial-probe.log`，40s 收到 4 条），且 `MemoriaProtocol: Device VAD start/end` 在 2026-09-09 的串口抓取里有先例（`outputs/acceptance/run-20260909-0943-face/serial-follow.log`）。1899/1900 两次复测**根本没挂串口抓取**——`run-20260910-weather-goodbye/serial-follow.log` 停在上午 10:19，`retest-*` 只有 bridge 日志。所谓「芯片侧没往控制台写」是没抓，不是没写。

epoch **1900** 真机（18:26 CST，session `4da51bf8`）确认 filler 单次化生效（`generation-2` 是唯一 ACK），但量出「问完到开口」的间隔问题，两处：

1. **提示音被跨 turn 的重复提交掐断**：`gen-2` 首帧 17.680、18.483 被 turn 3 提交取消，只播 0.80s，正文 `gen-3` 直到 20.741 —— 用户听到残句 + **2.26s 纯静音** + 正文。取消点是 `media_session_turns.py` `_commit_pending_turn`：既有的「不切断已播出声音」守卫要求 `owner.fence.turn_id == fence.turn_id`，重复提交是跨 turn 所以不生效。**已修（组件 `20260910-1855`）**：不动该守卫（扩到跨 turn 会让新话轮的回复因 reply lock 未释放被静默丢弃），改为在 `media_session_commit.py` 的 `_commit_user_turn_locked` 里跳过**重复话轮本身**——待提交文本与上一轮已提交文本（`normalize_short`）相同**且** `_reply_in_flight` 为真时才跳过，同文本 + 回复在飞意味着信息量为零，不会丢内容；答案由仍在飞的那一轮交付。回复已结束后的真重复提问、以及尾部带新文本的 straddle（epoch 1361）都不受影响。复现测试 `test_duplicate_media_turn_is_skipped_while_its_reply_is_in_flight`（无闸门时第二次提交仍建出 turn 2 → 红；加闸门 → 绿）。
2. **第二次提问完全没有提示音**：turn 4/5（35.715 / 35.905，相隔 190ms）都没播 ACK，正文 `gen-5` 到 42.098 才起，**6.38s 无提示**。两点结论：① turn 4 的 ACK 在 35.903 被 turn 5 抢占，`first_frame_sent=False`，即**又是重复提交**——已由 `20260910-1855` 的重复话轮闸门消除；② 即便有 ACK，一次 2s 提示也盖不住 6.4s 的检索，缺的是**第二句提示**。单测夹具另发现 ACK 可能被 `coordinator.output_intent_is_active` 判为非活动（前一个 output 仍占话轮），此时不发提示音且**不报错**，是隐性的（该观察未单独复现成真机故障）。**已修（组件 `20260910-1905`）**：`97978e9` 当年为守住 `agent.py` 行预算摘掉了 slow-think cover，本轮把它接回**到 `media_session_projection` 而不是 agent.py**——`_cover_slow_lookup` 在入队 ACK 之后、仅当委派仍在跑时等 `_LOOKUP_SECOND_CUE_AFTER_S = 2.5s` 并播 `THINKING_FILLER`（「稍等，我想一下。」），走同一条 fenced acknowledgement 路径；委派已完成则完全跳过等待，快查询的调度不变。复现测试 `test_slow_lookup_is_covered_by_a_second_cue`（先红后绿）。同轮还把「发不发提示音」的判据从 30s 突发窗口改成独立的 5s 窗口（`_LIVE_LOOKUP_FILLER_ACK_REPEAT_S`，组件 `20260910-1844`，复现测试 `test_later_live_lookup_still_announces_itself`）。
3. 附带发现（**已被 1905 与本次 20260912-1150 发布取代**）：当时 `services/agent/src/prompts.py:90` 的 `THINKING_FILLER` 全仓库无消费点（只在 `__all__`），即 commit `83687b7` 的「覆盖慢查询间隙的第二个提示」机制实际已失效。1905 把它接回 `media_session_projection`，本次整树 overlay 让它重新生效（1855 源码 0 处、HEAD 有并在 `media_session_projection.py` 调用，容器内已核实）。


另修 `a43668c` 引入的回归：它把 `duplex_runtime` **未分类 VAD 路径**的 `explicit_interrupt` 从 `False` 放宽成「含命令意图」，使影子/uncertain 声纹的「停一下」也能抢话轮停播，`test_playback_shadow_guest_fallback_cannot_bump_fence_or_stop_playout[停一下]` 转红（干净 HEAD 上就红）。已把该路径收窄为仅 `END_SESSION` 放行，`h1`（`_speaker_allows_user_input` 的告别子句）与 `h4`（已分类路径的告别放行）**按原样保留**——它们没有单测覆盖，但是为真机播放期告别所加，不能用「单测绿」反推可删。新增 `test_playback_unconfirmed_farewell_still_takes_the_floor` 钉住告别仍可抢到话轮。

模块预算没有上调：`a43668c` 让 `duplex_runtime` 从正好 4246 涨到 4260，而 `deploy_agent_component.sh` 把 `pyproject.toml` 当依赖输入（见「发布前门禁」），改预算就断快速通道。改为在 `a43668c` 自己引入的表达式内原地压缩 13 行（合并多行调用、折叠集合字面量、精简注释），行为不变，文件回到正好 4246。

## 2026-09-15 多日天气与续问关停修复（已发布，功能真机 1/3 通过、计量与长播待验）

本节对应用户「未来三天南京天气只答今天，播后再问没说完就待命」的反馈。证据是 **9 月 15 日**的 `outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-2/`，session `ee5f652b-1cd3-425d-bd6a-71fd91b7568f` / epoch **1953**；不是 9 月 14 日同名的 `session-2`。

- **多日天气根因已证实**：`bridge.log:67` 的 geocode 输入保留「好未来3天南京」，而 `bridge.log:90` 实际请求是 `forecast_days=1`。旧确定性天气适配器只识别今天/明天/后天的单日偏移，未建模天数范围；不是只改提示词能修复的问题。
- **续问关闭的现场事实**：`serial.log:304` 在 **12:57:21.672 CST** 播完进入 listening，`:306` 在 **12:57:27.566** 发出新 VAD start；`edge.log:2` 在 **12:57:29.101** 以 `owner_silence_timeout` 关闭，`serial.log:310` 在 **12:57:29.125** 回 idle。设备开口约 1.56 秒后被关停；旧 Bridge 日志没有受理 VAD、剩余静默预算、grace/watchdog 状态，**不能据此断言现场已进入某条 Python 受理分支**。
- **代码与红测证实的结构性竞态**：VAD 的受理保护晚于异步 projection；已经启用的 one-shot processing grace 不随新受理 VAD 暂停；旧超时还可能已在等 `standby_lock`。仅取消定时任务或调长超时，均不能完整解决这三处接缝。

本次修改沿用既有 provider 与 Python 会话权威链，不新增 listening 状态机、不放宽声纹/打断权限、不改固件或预缓冲：

1. 天气适配器支持从今天起的明确 1–16 天范围；「未来三天」逐项回答今天、明天、后天。只移除匹配的日期片段，保留地名数字；模糊、超限、矛盾范围、非今天起点或不完整日数据走既有 fallback，不默默改答今天。城市在「天气」之后的倒装句仍不由此适配器解析，不能声称任意自然语言日期/地名均已覆盖。
2. 合法 VAD 在 projection await 前受理，使用独立 active-VAD 标记；真实已启动的绝对说话 watchdog 才能接替 grace，锁内 revision 复核阻止旧静默关闭误杀。重复 VAD 不续 watchdog，也不补满静默预算；仍只有 verified owner 完成轮次能刷新预算。IGNORE、pin、空 VAD、旧 epoch 不获得保护；显式告别与绝对 watchdog 的关停不被 VAD 否决。
3. 主审补齐两个异步边界：旧 `vad.end` 等待 projection/ASR 返回后不得截断较新 VAD；正常 `vad.end` 在 ASR finalization 等待期间保留原绝对 watchdog，完成终局/epoch/新语句与 pin 检查后，同步交给既有 endpoint-tail 处理。provider 卡住仍有上界，超时后迟到返回不复活会话；pin 早退由原提交/清理路径释放 watchdog。
4. 新增 `media vad start admitted` 与 `media owner silence close superseded` 日志，记录 epoch、sample、remaining、grace、watchdog、revision，不记录用户原文。下一次捕获用它们证明真实受理与时限交接。

状态（2026-09-15）：

- `code`：已提交并推送 `d7214554316bdaa13984c7b8ecf00aeb61693311`，冻结 tag `20260915-weather-followup-lifecycle-v1`；天气/待命修复之外，已补 ASR 故障恢复、prepare retry 总期限及重连清理。
- `wired`：接入 `OpenMeteoWeather.resolve`、`on_speech_segment` 与既有 ingress/standby/endpoint/reconnect/terminal 清理路径，没有新增并行状态机。
- `enabled`：**生产 true，2026-09-15 15:58:42 CST 仅切 Agent/Bridge**。线上 Bridge 的 `load_settings` 切前后均核实 owner silence **10 秒**、speech watchdog **60 秒**、output stall **45 秒**；registry 库默认 0 不是线上生效值。显式配置 0 时保留旧 one-shot grace 与在途关闭，不能把 disabled 配置也说成已有同等续问保护。
- `verified`：**server_passed / functional_device_rounds_1_of_3_passed / metering_and_long_playback_pending**。前一轮四套件 **314 passed**；增加 **43** 项异常用例后完整 Agent unit **2013 passed in 27.68s**（剥离 `LISTENER_CUES_ENABLED/LIVEKIT_ADAPTIVE_INTERRUPTION/OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`，`--import-mode=importlib`），Agent Ruff、strict mypy（157 文件）、模块预算通过；正式发布门禁、CI、运行源码及真实 provider 已过。16:18–16:19 的首轮新媒体会话已取得双问/播放结束/播后告别记录，用户随后明确确认「三天齐全、续问正常、声音无断续或卡断」，首轮功能/听感通过。捕获与计量边界见下文。**16:18–16:19 旧板那轮未刷机；后续新候选回读与启动见下节。剩余两轮、VAD/watchdog 竞态与 >45 秒长播未验；`direct_real_device_verified=false`、`full_duplex_verified=false` 不变。**

发布前异常路径补核（2026-09-15，本地故障注入与修复已完成）：

- **ASR 异常恢复根因**：finalize 抛异常后的清理撤掉 watchdog/active-VAD，却没有接回 owner-silence，缺少后续 listening 事件时可悬空；旧异常还可能清掉较新 VAD。现在锁内校验 epoch 与原输入标记，仅清理自己拥有的输入；同步清理并恢复剩余静默预算后才异步发布 discard，故障不证明 owner 活动、不补满预算。主审追到下一帧恢复：`_reset_discontinuity` 也有同样的计时交接遗漏与 await 后误清新 VAD，已沿同一顺序修复。因音频不连续而退休输入的语义保留，不把缺失音频当成完整话轮。
- **prepare retry 总期限根因**：匹配 retry 的早返回与直接提交入口可能没有真实 live tail；即使原 tail 存在，到期逻辑仍会 shield 等待卡住的 retry。0.05/0.15 秒的两次退避不等于 provider I/O 有上界。现在首次提交、retry 与 fallback 共用原绝对 deadline，不因重试续期；略早 timer 回调重挂原期限，旧 epoch/endpoint 回调不能摘掉新 timer。到期仍在 prepare 时走既有 `turn_prepare_timeout` 关停，先封输入、取消提交，再清理；等待 standby lock 后重核 epoch/endpoint/在途任务，避免成功提交、新 VAD 或重连被旧超时误关。失败/耗尽不刷新 owner 预算，正常 verified-owner 成功仍可刷新。
- **重连接缝根因**：新 transport epoch 能正确否决旧 tail close，但旧 prepare 仍占着 `turn_commit_lock`，导致 `_reuse_session` 无界等待。身份及 authority 核验通过后，重连现在先取消旧输入的 tracked prepare，再进入既有锁内清理，并复核 terminal/context/epoch；不取消已选输出 generation、不发旧 tail 的 CLOSED。独立复核又证实：已分类 owner 的旧 prepare 吞取消后迟到成功，虽然不会发旧回复，却会把剩余 **7.5 秒**补成约 **100 秒**；预算刷新现额外要求当前 epoch，旧成功/异常都不能授予新静默窗口。重连等锁期间发生 terminal 也不能安装新 epoch。
- **故障证据**：`test_media_finalize_failure_lifecycle.py` **10** 项；最初 5 项核心故障红测及追加的 2 项 next-PCM 红测修复后通过，关联 session/standby 回归 **259 passed**。`test_media_prepare_retry_lifecycle.py` **33** 项，覆盖真实 timer/deadline、两次重试成功/耗尽、每次 prepare 卡住、已耗 grace、terminal/reconnect/new VAD 迟到结果、timer 锁竞态及 verified-owner 预算；主线独立复现的 2 项重连卡锁红测和 1 项迟到成功刷新预算红测已转绿。测试先断言生产路径实际启动 timer，再驱动到期，不凭空补 timer，也不以测试防挂 timeout 代替生产期限。以上是代码缺口实证，不等于已证明它们均曾发生于 epoch 1953。
- **独立复核**：测试 worker 仅写 retry 故障套件，生产改动由主线完成；其最终单文件复测 **33 passed in 3.66s**，再审 epoch 预算门、取消和 terminal 竞态后，本轮范围内未发现新的可复现问题。主线已亲自复现红测、审读测试及生产改动，并完成上述全量回归。
- **有界性范围**：已验证正常协作取消的 provider 会退出，取消后迟到返回不能发布旧轮次或启动回复。没有声称能强杀永久吞取消的第三方协程，也没有证明所有 provider reset/close、清理 I/O 都有硬上界；输入终局与资源完全回收是不同验收项。

仍开放：防重复提交门不能直接放宽；新旧会话均有 `straddles_committed_without_timing` 与 mostly-committed rescue drop，本次双问各提交一次不等于所有 ASR 重叠边界和语句完整性都已验收。首轮功能/听感已通过，不再重复索要确认；下述新候选已完成 app-only 刷写/整槽回读/保护校验和启动检查，新工具短捕获也已正常收尾，下一步验证真实播放计量，再补剩余两轮「未来三天南京天气 → 正文完整播完 → 续问 → 播后好的再见」。补测应覆盖新 VAD 接管及接近静默期限的续问；正常轮次不强求出现竞态专属 `close superseded`。计量必须有真实输出帧及终端 summary、会话捕获必须正常保存最终状态，之后再做 >45 秒长播。旧板功能 **1/3** 不继承为新候选已验收；本轮只更新设备 app，未部署服务端或改生产配置。不做预缓冲 `0026`；自动备份、家长通知发送与微信订阅号仍不在本阶段范围。

发布结果（2026-09-15）：证据目录 `outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/`。冻结范围仅 Agent 生命周期、天气、测量/捕获工具及其测试/文档；固件 dirty 留在主工作树，以干净 detached worktree 发布，未改有效环境或其它服务。CI **34943397294 success**（Agent 与 Python/PG 契约/offline E2E），正式 dry-run 全门禁及 production compose **47 passed**，捕获/报表 **62 passed**。正式切流复用同一 immutable commit 已通过的门禁（`gate-reuse.json`），不是跳过验证。候选真实天气 smoke 请求 `forecast_days=3`、回答含今天/明天/后天；切流后真实 LiveKit、FunASR、QwenRealtimeSearch、Qwen、Doubao、InterruptSemantic 和 readiness refresh 全部通过。16:00:49 回环/公网 8443 均 ready、core 12/12，新 Agent boot `98b37381-97e5-4af8-97b8-158d97b16602`，`last_loop_at=2026-09-15T08:00:45.163950+00:00`，非旧 heartbeat。

设备边界与捕获：15:58:16 重启 Bridge 时旧 epoch 1953 的 WSS 关闭；16:00:48 设备快照 **connected=false**。原始 `runtime-comparison.json` 的 `passed=false` 与 `runtime-comparison-split.json` 的 `device_reconnect_verified=false` 保留为当时事实。新增会话 `67286af6-06a4-40d3-a28d-aa2bb0ec1329`（media epoch **1** / device stream epoch **1954**）已证明随后唤醒并建立新媒体链，不改写旧快照。`session-1/` 仅为 16:03:35–16:15:32 正常收尾的启动/待机检查，无语音验收。`session-2/` 16:15:43 启动、16:15:55.440 进入 idle，串口最后记录 **16:21:42.952**；核查时 PID 27484 与 exec session 均已消失，`capture.json` 未写结束时间/最终流状态，退出原因未知，**不得说成 900 秒正常到期或 capture healthy**。16:40 只读补取同时间窗三容器日志，Bridge **199 行**、Edge **1 行**、Agent **0 行**均与原文件 SHA 相同，SSH exit 0/stderr 空；这确认已留存服务端记录，不补造串口尾部或收尾元数据。证据为 `session-2/report.txt`、`audit.json` 与独立 `*.supplement.log`。两段绑定既有 `run-20260915-p0-03-firmware-metering/postflash.json`（app SHA `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`），`read_from_board_this_run=false`，本次未重新读写固件。

首轮实际话轮审计（2026-09-15 16:18–16:19 CST，`session-2/`，**功能/听感 1/3 通过；有效计量长播 0 轮**）：

- 两次问题各提交一次（turn 2/gen 2、turn 3/gen 4）；三天天气请求 `forecast_days=3 / days=3`，续问为 `forecast_days=1 / days=1`，均选中南京并 HTTP 200。正文 gen 3/gen 5 音频分别 **19.06 秒 / 5.18 秒**，五个 generation 均有 `first_frame_sent → provider_completed → actual_heard → playback_ended`，终态为 `playback_completed`。两次 ACK→正文服务端间隔 **0.374 / 0.349 秒**，正文 Bridge post-pacer 最大帧间隔 **33 / 25ms**；不是设备可闻间隔或 DAC 实测。用户在「说完了」后明确补充 **「三天齐全、续问正常、声音无断续或卡断」**，结合设备终端回执记首轮功能/听感通过；不再把听感标为 pending，也不从单独的 `actual_heard` 账本事件推断听感。日志未记录完整问答文本，日内容齐全依据用户确认。
- 第一次 VAD 明确受理（16:18:53.174，remaining=8.952s、watchdog=true、revision=1）；第二问通过 ASR final 提交并播放，但串口和 Bridge **没有新的 VAD/admission**，不得据此关闭 VAD/watchdog 竞态专项。首次 rescue 13 字被重叠门拒绝并丢弃，没有生成第三个问题；空 ASR tail timeout 未关闭会话。正文中 16:19:08 的 `semantic_final` 告别候选被 `speaker_authority_unverified` 拒绝，随后正文正常终态；其来源未知，不判定为回声或真实说话。
- 播后告别：16:19:37.739 Edge `conversation_end_explicit`，16:19:37.755 串口 `listening → idle`。保留两端原始时刻，不把跨时钟差当精确延迟；自动报告因一个 stream 对应五个 delivery 保守保留 `playback→close gap=unknown`，不猜最后 generation。
- **旧板计量不覆盖本轮实际播放，原因已由主线核对源码与五代日志**：`generation.started → ResetDecoderForServerGeneration(N) → NoteGenerationAnnounced/OpenEpisode` 先打开统计窗口；首帧触发 `EmitLegacyTts(start)`、进入 speaking，既有入口 `ResetDecoder()` 又执行 `Close(kDecoderReset)`，没有为实际播放重新打开窗口。gen 1–5 因此各只有播前 `close=decoder_reset / output_frames=0 / first_output=no / exact_confirmed=0`，没有有效播放结束统计；`NoteWaitBegin/End` 在窗口关闭后不计供给等待，末尾 flush 也不能补回统计。这是**测量生命周期缺口，不是本轮声音异常的根因**；用户已确认无断续。全零不能证明软件供给无等待或 I2S/DMA 无欠载；问候 Bridge 最大帧间隔 **201ms** 也仅作发送侧观察。真实调用顺序回归及最小观测修复已在下节候选完成，不改音频控制权限或增加预缓冲；新候选回读与启动已通过，尚无实际播放计量证据。

## 2026-09-15 计量生命周期与捕获收尾（新固件已启动，真实播放待验）

本轮只修观测链和证据完整性，不修改 Agent/Bridge 线上版本、设备播放权限、静默预算或预缓冲。捕获工具异常退出的具体历史原因仍未知；修复验证的是可复现的清理/写盘缺口，不倒推旧进程一定收到了某个信号。

- **计量生命周期**：`PlaybackSupplyMeter::NoteDecoderReset` 仅在 episode 已打开、同一非零 generation、playback fence 精确递增时保留原窗口和首帧计时；reset 切断在途等待归属并清除 exact token。换代、flush、stop、非法 fence 和已关闭窗口保持 fail-closed，不能复活旧统计。宿主回归覆盖真实 speaking 入口顺序、重复 reset、迟到 token 和 uint32 回绕。供给等待日志移至输出提交/记账、解锁及相关输出回调之后；summary 的 768B 临时缓冲使用 `nothrow` 独占堆内存，分配失败有 dropped 日志，不占用接收任务的大块栈空间。仍只测 decoded playout queue 的 software wait，不是 I2S/DMA underrun 或听感测量。
- **捕获生命周期**：首个 metadata 写入前安装 SIGHUP/SIGQUIT/SIGINT/SIGTERM 记录，先写 `in_progress`；收尾独立关闭串口、逐路 terminate/有界 wait，必要时 kill/有界 wait，再关文件并记录 `completed_at_local`、首信号、退出原因和流状态，最后恢复 handler。一路 poll/wait/kill 报错不能跳过其余流；未知退出码不能冒充已回收，正常提前退出也不冒充捕获主动停止。BrokenPipe 不跳过清理。`capture.json` 同目录临时文件写完后原子替换，写满/替换失败保留原记录并清理临时文件；这不是自动备份，也不承诺 fsync 级断电持久性。
- **报告 fail-closed**：`capture_integrity` 与固件 receipt 绑定独立。正常完整捕获要求 `completed` 和可解析结束时间；请求服务端日志或声称流 healthy 时，还须逐路验证 `bridge/agent/edge` 的合法 `stopped_by_capture`/整数退出码/`forced_kill=false`，缺路、提前退出、强杀、清理失败、布尔冒充退出码、非法字段均点名原因。声称 healthy 也不能绕过逐路记录；legacy 缺 `capture_status` 一律 incomplete。preflight-only 只是完成 metadata 检查，不能算设备会话。
- **本地验证**：`uv run pytest -o addopts='' -q tests/test_voice_session_capture.py tests/test_voice_session_report.py firmware/esp32/tests` **304 passed**（53 捕获 + 74 报告 + 177 固件），包含先红后绿的故障注入；全仓 Ruff、模块预算及 `git diff --check` 通过。固定 upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`、ESP-IDF **v6.0.2**、app **2.4.2**，clean overlay apply/build 与既有 overlay gate 通过。计量专项依据宿主行为/接线测试、落地源码一致性和实际编译，不把既有 gate 的 exit 0 单独当作新增计量的覆盖证明。
- **冻结候选**：源码 **`d1ad38fe0d9b36eb9057d8eb97e782b8cc8066d9`**；`outputs/acceptance/run-20260915-p0-03-metering-lifecycle-local/final/` 保留 `upstream/`、`artifacts/`、`build.log`。overlay **`24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3`**；app **3,280,832 bytes** / SHA **`7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65`**；ELF SHA **`da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2`**。主线重算 hash、对比 header 并反向检查 patch；独立只读审计也核对前后像与最终构建树。现为 `code=true / wired=true / enabled=true_boot_verified / verified=false_pending_playback`，依据本轮新收据，不继承旧板听感。
- **受控真机更新（18:49:13–18:51:06 CST）**：`outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/` 保存本轮证据。写前工作树干净、候选 app/ELF/overlay 与冻结值一致；ESP32-S3、MAC `90e5b1d7832c`、32MB Flash、identity SHA 均匹配。写前 app 完整槽与旧板已验证的 postflash 整槽逐字节一致；本轮 `rollback-app.bin` 是紧邻旧 app **`dfba3d61…` / 3,280,512 bytes**，不是更早 run 的同名回滚件。固定 esptool **5.4.0**、no-stub、无自动写重试，只擦写 **`0x20000..0x340fff`**；写后回读 **0x3f0000 / 4,128,768 bytes**，app 前缀逐字节一致、擦除尾部为 FF、擦除范围外未变。bootloader/partition/NVS/otadata/phy-init/identity 六区域逐字节未变，ota_1/assets MD5 未变；不把具名分区校验外推为整个 32MB 所有字节均已读取。`postflash.json` SHA **`2229851e440525f3a619586cceae7f5ea906e3ef563e70040b0560c64d944b01`** 保持不可变，其 `boot_verified=false` 是写后当时事实。
- **启动与新版捕获收尾（18:51:33–18:52:19 CST）**：`boot-check/` 是 45 秒启动/待机窗口，无语音话轮。启动 ELF 前缀 **`da6ebdd16…`**、编译时间 **17:36:12** 匹配候选，**18:51:44.111** `activating -> idle`。`capture_status=completed`、结束时间完整、`exit_reason=duration_elapsed`、serial error/cleanup errors 均无；三路日志均 `stopped_by_capture / exit_code=255 / forced_kill=false`，报告 `capture_integrity=completed`、receipt bound。三个服务日志为空符合该空闲窗口，不能当作媒体链已通过。新增 `boot-verification.json` 绑定原刷写收据、capture/serial SHA，单独记 `boot_verified=true`，不回改已被捕获绑定的收据。**18:51:45.728 出现一次 BMI270 I2C ESP_ERR_TIMEOUT**，之后心跳持续到 18:52:14.553、未见崩溃重启；保留独立异常，不归因为语音卡断。串口输出字节级重复的启动行原样保留，不重复计为新会话。
- **旧捕获只读复核**：新版报告对 weather-lifecycle-release 的 `session-2/` 返回 `capture_integrity=incomplete`（缺 capture_status/结束时间），同时 `capture_receipt_binding=bound_by_capture_tool`。原 `capture.json` 前后 SHA 均为 `2801db93a6c74c23550a9771c3aba0a0b6acf0b8b0d65cb133e982c970af7de7`，未补写 metadata 或覆盖历史 `report.txt`；旧板首轮功能/听感 **1/3** 仍成立，但不变成计量通过。
- **本轮临时构建清理**：仅删除同一 run 下被 `final/` 替代的初版 `upstream/` 与 `artifacts/`，释放约 **1.2GiB**；保留最终候选、顶层 build.log、全部旧板收据与回滚件，未清理固件缓存或其它 run。
- **审计取舍**：未发现新的功能性阻塞。审计提出“4096 栈深等于 16KiB”不适用于本次 ESP32-S3 Xtensa/non-SMP 构建：实际 IDF port 的 `StackType_t=uint8_t`，因此未据此改动候选。锁外日志仍可能占用任务时间，真实 UART/收包时延及栈余量未测，不声称对播放零影响。两个 `0025` 文件的完整路径不同且当前改动不重叠；计量 patch 的旧后像 `index` 元数据不影响现用普通 `git apply`，内容已与构建树核对。本轮保留既有命名/元数据以维持冻结候选，不改号为预缓冲 `0026`、不切换 `--3way/--index` 应用方式。

下一步直接在上述已启动候选上开新版会话捕获，继续绑定不可变 `postflash.json`，**不要重复刷机**。先确认实际播放的 `first_output=yes / output_frames>0` 和终端 summary，再补新 VAD/临近静默续问与 >45 秒完整长播、操作员听感。短启动捕获的正常收尾不替代下一轮会话收尾；发生强杀/缺记录仍保留 degraded/incomplete，不追写为 healthy。**本轮仅更新设备 app，未部署/重启 Agent、Bridge、Edge 或改生产配置；P0-03 仍未整体完成。**

## 2026-09-14 readiness 证据刷新修复（16:32 CST unit 路径 PASS；回环与公网均 ready）

现象：2026-08-31 之后每次 12h 定时刷新都跑通全部真实 smoke，却在最后一步停在 `readiness mark FAILED: HTTP 409`。统计 2026-08-25 以来 `mark FAILED` **83** 次、`refresh PASS` 10 次，最后一次 PASS 是 **Aug 31 00:42** `20260827-architecture-split-v1`。Control 的 gate 因此长期停在 `not_ready_smoke_evidence_expired`（TTL `READINESS_GATE_TTL_S=86400`，12h 刷新本应有的余量被吃光），是**假阴性**，不是 provider 故障；2026-09-13 23:40 记录的 503 就是这个状态。

根因（两条，都源自 2026-08-27 架构拆分后的组件发布方式）：

1. `scripts/refresh_readiness.sh` 在未设 `MEMORIA_RELEASE_TAG` 时用**发布目录名**当 stack tag（`basename /opt/memoria/releases/20260827-architecture-split-v1`），但 2026-09-01 全栈发布后容器内的 stack tag 是 `20260901-0945-wake-word-whitelist`。mark 的 `release_tag` 与 Control 的 `settings.memoria_release_tag` 不一致 → 409 `smoke release tag does not match config`。目录名不再等于 release tag，这就是 409 的起点。
2. 同脚本只用 base `docker-compose.production.yml` 起一次性 smoke 容器，所以「provider smoke PASS」跑的是 **base 镜像**（`memoria-agent:20260827-architecture-split-v1` 一类），不是当时实际服务的组件镜像。即使 mark 成功，证据也不覆盖线上制品。

修复（`scripts/refresh_readiness.sh`，读法与 `scripts/deploy_agent_component.sh` 的 `container_env_value` 一致）：

- stack tag 改为从**运行中的 Control 容器**读取；只有容器不在运行时才回退目录名并打 warning。
- 一次性 smoke 容器复用该 live 容器创建时记录的 Compose 文件集（`com.docker.compose.project.config_files`）；记录中的文件缺失时 warning 跳过，随后用 `compose config --format json` 断言解析出的服务镜像**等于**容器实际镜像，不等即拒收证据。
- `run_agent` / `run_control` / `wait_for_current_release_readiness` 的行为与断言语义不变。回归：`services/control_api/tests/test_production_compose.py::test_readiness_refresh_passes_required_provider_gate_into_run_container` 已钉住上述规则，该文件 47 项全绿。

部署与验收（2026-09-14）：

- 仓库脚本 SHA256 `c0152d5b1768aa758d93f0540529ef00745bbee7560e91ad5386eb6621088288`，已 `install -m 755` 覆盖 `/opt/memoria/current/scripts/refresh_readiness.sh`；改前副本 `/opt/memoria/current/scripts/refresh_readiness.sh.bak-20260914-pre-tag-fix`（`dc8c95ce74883fa1ad68bcbe0c47f84357b9e956036fea397b135b8b21b28385`）。
- 手动实跑：`livekit_smoke_test PASS` → `provider_smoke_test PASS: FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic` → `verify_env OK` → `readiness mark OK` → `readiness refresh PASS: 20260901-0945-wake-word-whitelist (qwen)`。
- systemd 路径（同一 unit / ExecStart）：`systemctl start memoria-readiness-refresh.service` 于 16:32:08 CST `status=0/SUCCESS`，日志同样 `readiness mark OK` + `refresh PASS`。
- 状态：loopback `http://127.0.0.1:8791/health/ready` **200 ready**；公网 `https://aigcnice.com:8443/memoria-api/health/ready` **200 ready**；具名 core **12/12 ready**；agent `ready`，boot_id `e8b0983a-07a9-4e8f-83c1-39dc06cb3fc1`，`worker_ready/livekit_ready=true`，`last_loop_at 2026-09-14T08:33:01Z`。公网 readiness 在 **8443** 的 SNI 多路复用之后；443 属于同机既有 WMS vhost，`/memoria-api/...` 在 443 上无匹配路由会返回 404，**不能把 443 的 404 当作服务故障**。
- 定时子项已完成（2026-09-15）：timer `LastTriggerUSec=04:35:27 CST`，04:36:23 首次因 Doubao word timestamp alignment degraded 失败；04:41:24 systemd `Scheduled restart job, restart counter=1`，04:42:35 providers PASS、04:42:38 mark OK、**04:42:39 refresh PASS / Result=success / ExecMainStatus=0**。因此为“定时触发后一次自动重试成功”，不是首尝试通过；证据 `outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/readiness-timer.log`。本轮发布后的 smoke 直接执行脚本，不用手动启动 unit 冒充定时触发。

口径更正：readiness 的 `release_tag` 是**栈级 env tag**（`20260901-0945-wake-word-whitelist`），不等于任何组件镜像版本。组件身份只认容器 image/labels，当前值见下方运行清单，不从旧 smoke 的版本推断。gate 变绿只证明该栈 tag 下的 smoke 通过，不证明设备场景已验收。

## 2026-09-14 唤醒问候播放期越权 VAD 断链修复（15:04 CST app-only 刷入；15:11 CST 真机复测 PASS）

现象：唤醒后机器人播放欢迎语「哎呀」，播到第一个可播放下行帧后约 20~30 ms 内设备 WebSocket 断开，屏幕进「连接中」，随后按 `1/5…5/5` 退避重连；重连后再次播放同一问候又断，最终由服务端 `owner_silence_timeout` 收尾。用户侧表现为「说了句哎呀就连接中了」——那句「哎呀」是机器人自己播的，不是用户说话。

根因（单一决定性证据）：Direct Edge 对播放窗口内的 `vad.start` 做越权校验，`isPlaybackActive() && !bargeInSourceAllowed(AllowedBargeIn, "voice")` 时不转发该事件，先发 `session.error` 再关连接。生产库 `device_settings` 里该设备（`dev_atk_a4cb8fd6095c`，settings_version=22）的签名策略是 `allowed_barge_in=["button","keyword"]`，**不含 voice**；板上 AEC 参考未验证（`aec_reference_verified=false`、`aec_mode=fd_low_cost`），播放「哎呀」时的自身回声（串口 `rms=0.0474`）被设备当成人声上报，正好命中该分支。证据：

- 串口 `outputs/acceptance/run-20260914-aec-fix/retest-2/serial.log`：13:05:12.654 `Device VAD start at sample=32640 rms=0.0474` → 13:05:12.677 `Device WebSocket disconnected attempt=1` → 13:05:12.706 `speaking -> recovering`。
- Edge `outputs/acceptance/run-20260914-aec-fix/retest-2/edge.log`：同一 session/device 连续三次 `WSS handler rejected … kind=text`（epoch 1941/1942/1943），最后由 `owner_silence_timeout` 关闭——静音超时是断线后的结果，不是根因。
- 设备签名策略只读读取：`outputs/acceptance/run-20260914-1510-wake-ack-vad/device-settings.txt`（经 ssh 在 control-api 容器内以 `mode=ro` 打开 SQLite，未改任何数据）。

修复（commit `bdf2047`，已推送）：固件在 `session.accepted` 解析签名 `allowed_barge_in`，落地 `voice_barge_in_allowed_`；`SendVadState(true)` 在 `HasActivePlaybackGeneration()` 为真且策略未放行 voice 时本地抑制并打告警，播放结束后的普通聆听 VAD、`vad.end` 与物理硬停路径不变。overlay patch 0024 的把关条件补上 `VoiceBargeInAllowed()`，`ResetSessionState()` 清除该标志；回归断言加在 `firmware/esp32/tests/test_memoria_protocol_source.py`。

构建与刷机验证：clean build + `check-overlay.sh` + `firmware/esp32/tests/` 全通过；app-only 只写 `0x20000`，写后全片回读字节一致，identity/NVS/otadata/bootloader/分区表/phy-init 未变，assets 与 ota_1 的 MD5 未变；启动进入 idle。

真机复测（2026-09-14 15:11 CST，session `8d013b25-8e52-4c94-9537-4e8aad645a56`，stream epoch 1945）：`outputs/acceptance/run-20260914-1510-wake-ack-vad/retest-1/`，机械判据 `gates.txt` **14/14 PASS**：

- 唤醒后 15:11:07.568 首个可播放下行帧、`listening -> speaking`，15:11:09.112 `speaking -> listening`——问候整句播完，全程无 `Device WebSocket disconnected`、无 `speaking -> recovering`，Edge 无 `WSS handler rejected`。bridge 侧问候 generation 1 走完 `first_frame_sent → provider_completed → actual_heard=True → playback_ended=True`。
- 一句天气 → turn 2 的 generation 2（ACK）与 generation 3（正文）两段都 `actual_heard=True`。
- 告别：07:11:28.536 `media early conversation-close endpoint … text_len=5 source=partial_immediate` → 07:11:28.539 Edge `reason=conversation_end_explicit` → 串口 15:11:28.560 `listening -> idle`，约 24 ms，未走 `owner_silence_timeout`。
- 本轮未出现 `Suppressing playback-window VAD start` 属预期：overlay 把关在 Application 调用点就拦住，播放期根本不会调用 `SendVadState(true)`；协议内抑制是其它调用路径的第二道防线。

验收边界：一次干净会话不等于全链路通过，且本轮没有 AEC 残余/双讲/矩阵证据，`direct_real_device_verified` 与 `full_duplex_verified` 仍为 false；「播放中语音打断」按签名策略仍不生效（见下）。

已知策略边界（不是本次缺陷）：签名策略未放行 voice，因此**播放途中用说话打断仍不会生效**——机器人会把当前句播完再回聆听，物理按键硬停不受影响。要放开需先让 AEC 参考通过验证（`device_acoustic_capabilities.aec_verified`）再升 `full_duplex_verified`，或显式把 `voice` 加入 `allowed_barge_in`；后者在 AEC 未生效前会让机器人把自己的声音识别成用户。

## 2026-09-13 天气提示后无声修复（23:35:36 CST 已切流，两次正文听感已确认，告别即时待命未通过）

- 根因已由线上 epoch **1934**、session `cb90521f-2254-4e76-ba69-5f571b1af4e8` 确认：22:42:52.963 filler 已 `actual_heard=True/playback_ended=True`；随后 VAD 将 floor 切为 `user_speaking`，但后续 ASR 为无字短输入（RMS 87、160ms）。22:42:53.886 天气查询成功返回 187 字，却因 `output intent inactive` 被丢弃；22:42:56.058 空 tail 退休也没有恢复 floor。这次 VAD **没有推进 generation**，不是查询失败或 TTS 无声。
- 修复：Media coordinator 在 floor 暂时关闭时保留仍有效的输出候选，但不授予播放权限；真空输入结束且原 fence/context 仍严格匹配时恢复 floor，事件驱动重试。普通回答和查询失败 fallback 同样覆盖；有效正文排队时不额外发“没听清”抢占它。内部 ACK/正文 handoff 的 generation bump 同时携带有效排队后继，避免只保留 ACK 而再次丢掉正文，已播放/取消 owner 不会重播。
- 安全边界：新问题、stop/cancel、身份或 session/context 切换、过期、关闭、standby、声纹登记以及有文本输入不能被旧空 tail 复活；不延长 TTL、不放宽声纹门禁、不改 VAD/音量/固件。Go output-arbiter shadow 的 VAD 清队列口径与 Python 暂存候选存在诊断差异，仍无执行副作用，不能据 shadow 统计宣称 Go 已能接管。
- 验证：故障时序“结果先到/空 tail 先到”与 ACK+deep 同时排队的 promote/preempt 路径均红→绿；完整 clean-worktree release gates（ruff、模块预算、strict mypy 157 files、全部 Agent 单测及 production compose tests）通过，`duplex_runtime.py` 仍为 **4246/4246**。提交 `6ca25f71da05ec1cf0c08d46ce4b92c643a8f466` 与 release tag 已推送。
- 发布只重建 Agent/Bridge，运行源码 hash、镜像 revision、双 healthy/restarts=0、Bridge gRPC、有效 env 摘要与非目标容器不变均已核实，23:47:21 CST 延迟复核仍通过。当前镜像/回滚/收据见“当前生产”；`direct_real_device_verified/full_duplex_verified` 保持 false。
- 上线后新设备会话 epoch **1935**、session `d0a30dbe-18e7-4dbd-9304-e8a75a6eae52`：Edge 确认绑定 `dev_atk_a4cb8fd6095c`。南京当天/次日天气查询分别 2527ms/4391ms 完成；turn 2/gen 3 正文在 23:45:51.005 发首帧、23:45:57.181 收到播放结束，turn 3/gen 5 正文在 23:46:05.446 发首帧、23:46:10.459 收到播放结束，均 `actual_heard=True/playback_ended=True`。每次查询一段 ACK 后一段正文，本轮没有 `output intent inactive`；提示结束到正文首帧分别约 **0.366s/1.766s**，第二轮仍超过 1.5s 时延标准。2026-09-13 用户已确认今天、明天天气均听到，并报告追问与“好的再见”时均能中断；**这只确认本轮两次正文交付，未重新触发 epoch 1934 的同一空输入恢复时序，不能把特定故障场景或全双工验收记为通过。**
- 告别即时待命未通过：用户报告“好的再见”后屏幕继续聆听数秒才待命。本轮日志中 23:46:10.465 由 `media_playback_ack` 进入 listening；其后输入经 FunASR/SenseVoice 未产生文本（provider PCM RMS 49.943、peak 553），23:46:19.117 空 ASR tail 退休，23:46:19.237 Edge 才以 `owner_silence_timeout` 投递关闭，距进入聆听约 **8.77s**。未见这句告别被接受或 `conversation_end_explicit`；当前证据定位在告别输入/识别链，不能断言权限拒绝、告别规则漏词或界面延迟。用户报告的中断也不替代带 fence 的服务端抢话与设备串口证据；下一步需对齐原始上行 PCM 与设备 VAD，不调大增益或绕过声纹门禁。

下节保留既有故障背景；其中旧镜像和旧回滚只作历史证据，不再作为当前操作入口。

## 2026-09-12 闸门修复背景（旧版本真机复测未过）

问「今天南京天气怎么样」听到两遍 filler 且答案丢失（epoch 1911/1912）的代码修复：

**12:45 真机复测未过（epoch 1915，会话 `5188ae2c`）**：症状与 1911/1912 一致（两遍稍等 + 无答案）。**上轮闸门确认生效**（29.998 有 `media duplicate media turn skipped`），但整轮定位出三个新缺陷：**D1** 重复闸门在「委派结果已交付（claim=COMPLETED）、答案尚未播出」的空窗失守，延迟提交重试把同文本放行为 turn-3 并掐断第一遍 filler；**D2** filler 播完 129ms 后的回声假触发 vad_start 翻转话轮权，把就绪答案在 `first_frame_sent=False` 时以 `superseded` 杀掉（无 AEC 下此时序确定性复现）；**D3** 已被 `cross_sentence_overlap` 拒绝的静音幻觉文本（rms=308 → text_len=3）仍驱动会话关闭。SenseVoice 整段转写证实用户只问了一句「今天南宁天气怎么样？」（926ms），之后 1.3s 近静音。证据与修复设计 F1-F3：`outputs/acceptance/run-20260912-1245-epoch1915/findings-epoch1915.md`（含全量 bridge 日志与上行 PCM tap）。

**发布记录（2026-09-12 12:39:40 CST 切流 PASS）**：`memoria-agent:20260912-1150-companion-persona-and-lookup-gate`，image `sha256:7fce8545aa20e49b51f772b22b14f51e2020e39d207cb44c2eef2f5589822228`，commit `43b28f181764126fa7d0e688cf797a58e8c3b176`。agent/bridge 双双 healthy、restarts=0；**容器内已逐条核实**闸门新调用（`media_session_commit.py` 第 679 行 `_reply_or_delegation_pending`）与 `media_session_turns.py` 的两个 staticmethod 真实存在，不是只凭脚本自报。回滚基线 `rollback-20260912-1150-companion-persona-and-lookup-gate-pre-agent/-pre-bridge`（= 切流前的 1855，`sha256:6088221753038e0e448a78580fb6b179bf59c5e91b93ff833b23986fb57513a0`）。**回滚即回到带同一 bug 的 1855（epoch 1912 已实锤），本轮没有干净可退版本——复测再失败时没有好兜底，这是当前最大风险。**

**本次发布形态变更**：组件通道自本次起改用**整树 overlay**——`infra/Dockerfile.agent-source-overlay` 由「`rm -rf /app/services/agent` + 只覆盖 agent」改为「`rm -rf /app/services /app/packages` + 整树 COPY」，因为新 agent 依赖 `services/persona/custom_persona_fields`（1855 底座无此包）。仍然复用 1855 依赖层构建、`uv.lock` 未动。契约测试 `services/control_api/tests/test_production_compose.py` 已同步，`deploy_agent_component.sh` 的归档范围与回滚恢复路径一并改为整树。**副作用（正向）**：1905 的慢查询第二句提示随整树恢复（1855 源码 0 处 `THINKING_FILLER`，HEAD 有并在 `media_session_projection.py` 生效）。

**数据层**：`services/identity/postgres_schema.sql` 已前向迁移到 `memoria` 库——新增 `identity_custom_personas`、`identity_persona_assignments`，并把 `identity_idempotency_records_operation_check` 加宽至含 `persona.create`。幂等 IF-NOT-EXISTS、向后兼容；已在库内核实两表与约束。切流前的库备份在服务器 `/var/backups/memoria/20260912-1150-companion-persona-and-lookup-gate/`（注意：自动备份当时并未在跑，这次是手工 `pg_dump`）。

- **A 已修**：`media_session_commit.py` 的 `_commit_user_turn_locked` 重复话轮闸门由 `_reply_in_flight` 改为 `_reply_or_delegation_pending` = `_reply_in_flight or _delegation_output_pending`。后者在 `context.delegation_output_claims` 存在 `PENDING/OWNED` claim 时为真，即把「输出已 `delegation_output_owned` 移交、深查结果未交付」的空窗计入 in-flight（两个 staticmethod 加在 `media_session_turns.py`）。**刻意不改 `_reply_in_flight` 本身**——它仍被 6 处 early-commit 路径复用，混入委派语义会饿死新话轮的端点锚定。
- **B 部分**：`media_session_projection.py` / `media_session_output_dispatch.py` 两处 `output_intent_inactive` 接缝加 WARNING，把「已完成深查答案被静默释放」变为可观测。**行为未变，不保底**。
- **C 未修（判为已被 A 消解）**：跨 turn supersede 未出首帧答案的触发者就是重复提交本身，A 修好后不再进入。对真正的新话轮（换话题/打断）抢占仍是正确行为。

**根因 2 已修、待真机确认**：**不同文本**抢跑时，旧话轮的可取消 live lookup 现在会在新话轮切换前按 fence 取消并从任务注册表清理，避免旧结果回流、抢占新回复或形成 stale output。代码与回归测试已通过，真实设备仍需确认“南京后立即改问北京”时新问题正常作答。

**发布工具工单（截至 2026-09-13：已用于本次真实发布）**：根因是回滚 override 曾为 Agent/Bridge 生成两个不同 tag，而前进式切流要求 `agent_image == bridge_image`。`scripts/deploy_agent_component.sh` 现使用单一 `memoria-agent:rollback-<release_tag>-pre`，两个服务和 `ROLLBACK_POINT.txt` 共享该 tag；四个本地 release gates 显式使用 `uv run --extra dev`，保证 fresh worktree 安装开发依赖。`20260913-p0-empty-input-resume-v1` 已从 clean worktree 完整通过 ruff、module budget、strict mypy（157 source files）、Agent/production compose tests、artifact preflight，并完成真实远端构建与切流。共享 rollback tag 与线上健康证据见下方。

**真实设备告别复测（2026-09-12）**：新会话 `53c86566-6fec-48fa-9ddd-43da81674ce3`、epoch `1925`，不是旧日志。设备成功唤醒并进入 `listening`，收到“好的，再见”后线上记录 `conversation_end_explicit`，后续输入以 `reason=terminal` 拒绝，设备回到 `idle`；告别即时停止闭环已通过。天气会话 `eae63bac-d0d2-4f63-ad37-00f7b0a9a457` 是另一条独立的新会话。

**已由 2026-09-13 empty-input-resume 修复更新**：floor 暂时关闭改为排队，不再仅因用户抢话释放有效候选。`output intent inactive` WARNING 仍用于 fence/context/TTL 等真正失效；通过 `empty_input_retired`、`media output carried` 和 output retired 原因区分恢复、内部移交与淘汰。

**测试面（本地，全部剥 `PYTHONPATH`）**：`test_media_session.py` 172 passed（含新增 6 条：工程师 2 + QA 4）、`test_agent_production_wiring.py` + `test_realtime_information_markers.py` 98 passed、相关回归面（delegation_coordinator / conversation_projection / duplex_runtime_wiring / media_runtime_hardening / media_agent_factory / interaction_mode_runtime）192 passed；`mypy services --strict` 433 文件无问题；`ruff check` 干净；`check_module_budget.py check` PASS，预算未放宽。

**文档失效提醒**：`outputs/repro-red.txt` 记的是 epoch-1897 之前的旧病，在当前 HEAD 上**不可复现**；`test_heard_ack_then_duplicate_turn_commit_does_not_repeat_filler` 在干净 HEAD 上是通过的，且走 `runtime.on_turn_committed` **绕过闸门**，不是该 bug 的回归守卫（QA 用 `git stash` 退回 HEAD 源码实测确认）。后续以本次新增的 6 条测试为准。

## 下一验收

按顺序。点屏 / 摇晃 / 短拍逻辑已于 2026-09-09 16:08 CST 操作员 PASS；本轮只换 v3 对话脸画面，不改点屏拍击接线。半双工双轮（星期几 + 天气 + 短告别）曾在 epoch 1379 PASS，不代替下列项。

| 项 | 标准 | 状态 |
| --- | --- | --- |
| 待机脸照片 | 拍 `idle.jpg`，黑底月牙+平嘴+鼻点，对照 `outputs/firmware-face-v3-20260909/sheet.png` 的 `neutral` | 待拍 |
| 五表情照片 | 唤醒后按「屏幕表情」表各拍一张（happy/loving/sad/surprised/thinking），说完回待命月牙+平嘴 | 待拍 |
| 唤醒问候不掉线 | 唤醒后机器人把「哎呀」整句播完，屏不进「连接中」；串口无 `Device WebSocket disconnected`、无 `speaking -> recovering`；Edge 无 `WSS handler rejected` | **2026-09-14 15:11 CST PASS**（session `8d013b25`）：问候播完 `speaking -> listening`，无断线、无 Edge 拒绝，问候 `actual_heard=True`；判据 `outputs/acceptance/run-20260914-1510-wake-ack-vad/retest-1/gates.txt` 14/14 |
| barge-in 告别 | 天气播报中途说「好的，再见」：Speaking 期 Device VAD start、`conversation_end_explicit` / `session.close`、屏回待命，不靠静音超时；BOOT 仍能硬停 | 2026-09-14 epoch 1937 未通过（播放期无 VAD、播后空 ASR、静音超时）。现签名策略 `allowed_barge_in=["button","keyword"]` 未放行 voice，播放中 `vad.start` 属越权、设备端已按合同抑制，故**该项在 AEC 参考验证前无法通过语音达成**；需先验证 AEC 或显式放行 voice 后再判 |
| 单次查询提示 | 问天气只听到**一遍**「稍等，我查询一下。」，随后直接是正文；重复提问不得连播两遍 filler | 当前 `20260913-p0-empty-input-resume-v1`；epoch 1935 两次查询各一段 ACK + 正文，均有播放结束/actual_heard 回执，2026-09-13 用户确认两次正文均听到；原空输入恢复时序仍待专项复测 |
| 问完到开口的间隔 | 说完到机器人开口不应有 >1.5s 的纯静音；提示音若已起不得被掐成残句 | floor 关闭暂存、空 tail 恢复及 ACK→正文移交已部署；epoch 1935 的 ACK 结束→正文首帧约 0.366s/1.766s，第二轮仍超 1.5s，未验收通过 |
| 提示音覆盖长查询 | 查询超过约 2.5s 时应有第二句提示，避免长静音 | 代码已随整树 overlay 部署，容器内已核实；真实设备行为尚待复测 |
| 播后短告别 | 正文播完再说「好的，再见」应关闭会话回待命，不靠 `owner_silence_timeout` 兜底 | **2026-09-14 15:11 CST PASS**（同一 session）：`early conversation-close … partial_immediate` → Edge `conversation_end_explicit` → 串口 idle 的日志间隔约 24 ms，未走静音超时；不是物理屏幕延迟测量。早前 epoch 1936 亦通过 |
| 长天气 | 完整播报不被 45s 墙钟掐断，且操作员确认完整播完、无断续 | **待验**。旧板 software wait 全零因统计窗口未覆盖播放，不能记有效计量；旧串口错误与证据保留在 `outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-1/`。新生命周期修复候选 18:51 已刷入、回读并启动，短捕获正常收尾；仍无新候选实际播放计量、>45 秒或听感证据 |
| 长回复不断音 | 唤醒问候后再说一句较长的话，整句听完；允许串口 `Dropping server packet`，不得再把队列满升级成 `playback.error` 一字卡断 | 旧板 16:18–16:19 的 19.06s/5.18s 正文用户听感通过（1/3），不等于 >45 秒长回复通过；新计量候选已回读并启动，真实播放和听感待验 |
| 主人匹配 | 主人轮通过，非主人不放行；不要放宽 `reject_non_owner_voice` | 声纹 active，当轮匹配未复测 |
| 小程序 0.8.84 | 手机微信切开发版，核「设备在线」、首页新文案、设备 095c | 已上传，未体验版 / 未提审 / 未手机验 |
| 切主体触发设备重协商 | 在线设备上从小程序切换使用者后，bridge 出现 `runtime_profile.invalidated`（`apply_at=next_safe_point`），设备安全点重连并加载新 profile；日志出现 `device profile change projected … delivered=true` | 代码已切流（`20260911-subject-switch-device-notify-control-api`），切流时设备离线，待真机 |

**勿做**：宣传全双工；把 `hardware_verified` / `direct_real_device_verified` / `full_duplex_verified` 从刷机、欢迎语或点屏拍击外推为 true；hello 把 `aec_reference_verified` 写成 true；打开播放期 KWS；把 TurnPhase 从 shadow 改成有副作用；伪造 owner；把未 active 的声纹当主人认证宣传。

### 20260913-empty-input-resume 复测清单

先决条件（1899/1900 就是漏了第 1 步，串口全程没挂）：

1. **会前一分钟**挂串口采集，确认 15s 内至少出现一条 `SystemInfo` 心跳；没有心跳就别开始，先查 USB 与波特率。用系统 `python3`（有 pyserial；`uv run` 的 venv 没有）。把下面存成 `$RUN/capture_serial.py` 后后台起：

```python
import sys, time, serial
dest = open(sys.argv[1], "ab", buffering=0)
with serial.Serial("/dev/cu.usbmodem101", 460800, timeout=0.5) as ser:
    while True:
        chunk = ser.readline()
        if chunk:
            dest.write(b"[" + time.strftime("%H:%M:%S").encode() + b"] " + chunk)
```

```bash
RUN=outputs/acceptance/run-$(date +%Y%m%d-%H%M)-empty-input-resume-retest
mkdir -p "$RUN"
nohup python3 "$RUN/capture_serial.py" "$RUN/serial.raw" >"$RUN/capture.out" 2>&1 &
echo "RUN=$RUN"; sleep 15; grep -c SystemInfo "$RUN/serial.raw"
```

先决条件 1 已本机实测：40s 收到 4 条心跳，原始输出 `outputs/acceptance/run-20260911-serial-probe/serial-probe.log`。

2. 挂 bridge 日志：`ssh memoria-prod "docker logs -f memoria-voice-core-media-bridge-1 --since $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >"$RUN/bridge.log"`。
3. 记录 `--since` 那个 UTC 时刻；收尾用同一时刻 `docker logs <container> --since` 落盘，不靠模糊时间窗。

场景（按序；每句等回复**完全播完**再问下一句；当前版本为 `20260913-p0-empty-input-resume-v1`，不能借用旧版听感验收）：

| # | 说什么 | 判据 |
| --- | --- | --- |
| 1 | 唤醒后问「今天南京天气怎么样。」 | 只听到**一遍**「稍等，我查询一下。」；不得 >1.5s 纯静音；答案必须到达 |
| 2 | 隔几秒再问一句天气类 | 新提问仍播自己的提示音（5s 窗口只挡同一突发的兄弟提交） |
| 3 | 问「今天星期几。」 | 快查询直接出正文，**不**多等 2.5s、不补第二句提示 |
| 4 | 天气播报中途说「好的，再见」 | 串口 `MemoriaProtocol: Device VAD start`；bridge `conversation_end_explicit`；屏回待命月牙；BOOT 仍可硬停 |
| 5 | **正文播完、彻底安静后**再说「好的，再见」 | 应立刻关闭回待命，不靠 `owner_silence_timeout`（10s）。**主目标**，1899 未达成 |
| 6 | 唤醒问候后说一句较长的话 | 整句听完；允许串口 `Dropping server packet`，不得 `playback.error` 一字卡断 |
| 7 | 主人与非主人各说一句 | 主人轮通过；非主人不放行；不得放宽 `reject_non_owner_voice` |
| 8 | **同一句连问两遍**：先问「今天南京天气怎么样。」等它**答完**，再问完全同一句 | 第二遍**必须**正常作答。闸门只挡「同突发内的重复提交」，不得吞掉用户的真重复提问 |
| 9 | 问「今天南京天气怎么样。」后**答案未出前**立刻改问「北京明天天气怎么样。」 | 根因已定位并修复：新话轮切换前按旧 fence 取消并清理旧 live lookup，避免 stale output、旧答案抢占或新答案丢失；已部署，待真实设备验证新问题正常作答 |

日志判据（bridge 侧）：

- 每个 delivery/generation 恰有一次 `media reply delivery … event=first_frame_sent`；每次查询的 ACK 和正文分别交付一次，可属于同一 turn 的不同 generation。同一问题出现两次提交/两次 ACK 即失败。
- 说完到首个 `first_frame_sent` 间隔 ≤1.5s；出现 `event=preempted … first_frame_sent=False`（提示音被掐）即失败。
- 场景 5 应出现 `media final did not start reply … reason=conversation_end_explicit`，且**不**应再出现 `media turn discarded after ASR tail timeout`。
- `media_asr_boundary` 里同时记 `provider_pcm_clipping_detected` 与 `provider_pcm_rms`；播放窗口内仍削波就记下该 `endpoint`，这是回声而不是 vendor 空转写。
- 无字 VAD tail 结束且原 fence 不变时应出现 `cause=empty_input_retired`，随后正文应有首帧和设备终端回执；新问题/stop/身份切换后不得恢复旧答案。`media output carried` 只证明内部候选移交，不等于 Actual Heard。`output intent inactive` 要核对 fence/context/TTL 原因，不能再把仅 floor 关闭时丢正文解释为正常抢话。

回滚判据与命令：

本次组件启动、健康或新话轮/stop 安全边界出现回归时，先冻结日志和 manifest，再按当前收据回滚 Agent/Bridge。紧邻回滚 `20260913-p0-interrupt-output-resume-v1` 已有切前健康证据，但**仍含本次天气静音缺陷**；天气复测再失败需据新会话取证，不能把退回旧版称为问题解决。下列当前 rollback override 已在服务器以 `config --format json` 验证两服务共用唯一回滚 tag，本轮没有执行破坏性的真实回滚演练：

```bash
cd /opt/memoria/releases/20260827-architecture-split-v1
sudo env MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist \
  MEMORIA_RELEASE_COMMIT=7ca3d4ec531305d968d67ef1bb13b944e566e4cf \
  docker compose --project-name memoria \
  --file docker-compose.production.yml \
  --file /opt/memoria/component-releases/20260913-p0-empty-input-resume-v1/pre-cutover-live.override.yml \
  --file /opt/memoria/component-releases/20260913-p0-empty-input-resume-v1/agent-component.rollback.override.yml \
  --profile media-runtime up -d --no-deps --no-build agent voice-core-media-bridge
```

旧多级 Agent 回滚链已退出保留范围，不能继续执行历史 tag/override；依赖底座或其他消费者保留的镜像不属于可任选的业务回滚点。

## 屏幕表情：对话脸

```yaml
change: vocat_conversation_face_v3
code: complete
wired: overlay_board_layer_memoria_face_display
verified: host_renderer_tests_preview_esp32s3_build_overlay_gate
hardware_verified: false
next_owner_action: 拍待机脸 idle.jpg 与五表情（happy/loving/sad/surprised/thinking）；短拍应为杏仁瞳孔+小O
on_device_flash: app_only_0x20000_20260909-1845-conversation-face-v3
evidence_dir: outputs/acceptance/run-20260909-face-v3
backup_app: firmware/esp32/artifacts/backups/pre-playback-barge-in-20260910/app-before.bin
backup_app_sha256: d7efa9859e12df3f9b54981b05d2f7ba84fd58951c242f2e3d9e1d79e9d6aaaa
operator_pulse_pat_result: tap_pass_shake_pass_pat_pass
operator_verified_at: 2026-09-09 16:08 CST
pending_flash: none
```

360x360 圆屏是黑底白描对话脸，不再显示黄色 Noto emoji。签名是嘴，鼻子是米粒点，闭眼仍是月牙，睁眼是杏仁白眼加挖空瞳孔。协议 `screen.expression` 未改：`neutral/happy/sad/surprised/loving/thinking`；固件另能画 `embarrassed`/`wink`/`speaking`，本轮服务端不必接线。未知名字回落 `neutral`。`Application` 在 idle / connecting / listening 下发 `neutral`，说完回到待命月牙+平嘴。面部不绑定唤醒名。固件摘要与身份区 SHA 见「板卡与固件」。

**本地待机交互（逻辑已可用；本轮换画面后需重看）**

- 点屏：保持待命月牙+平嘴，串口 `idle screen tap ignored`，不开麦。
- 摇晃：保持待命月牙+平嘴，不开麦。
- 短拍身体：闪 `surprised`（杏仁白眼+瞳孔+小 O）约 1.5s，再回待命；不开麦、不 `ToggleChatState`。

点屏/摇晃/短拍接线证据：`outputs/acceptance/run-20260909-1531-confirm-pat/serial-follow.log`。这不等于 v3 视觉验收，`hardware_verified` 保持 false。

**对话表情（待拍照）**

对照 `outputs/firmware-face-v3-20260909/sheet.png`。触发源是助手回复语气（`mascot_expression_for_reply`），不是用户原话。照片存 `outputs/acceptance/run-20260909-face-v3/`。caring 词会先判 `loving`；要 `sad` 就换一句带「遗憾 / 抱歉」且不含 caring 词的话。

| 你说 | 期望助手语气 | 屏幕脸 | 串口收据 | 存图 |
| --- | --- | --- | --- | --- |
| 「我拿到心仪的 offer 了」 | 太好了 / 恭喜 | `happy`：外眼角上挑的眯眼+四角星+笑嘴 | `emotion=happy open_eyes=0` | `happy.jpg` |
| 「我今天有点难过」 | 辛苦 / 心疼 / 听起来… | `loving`：内倾月牙+灰调红晕 | `emotion=loving open_eyes=0` | `loving.jpg` |
| 「我的同事今天离职了」 | 遗憾 / 抱歉 | `sad`：外眼角下垂+泪+撇嘴 | `emotion=sad open_eyes=0` | `sad.jpg` |
| 「没想到今天下雪了」 | 没想到 / 真的吗 | `surprised`：杏仁白眼+瞳孔+小 O | `emotion=surprised open_eyes=1` | `surprised.jpg` |
| 「有什么建议吗」 | 反问（回复带「？」） | `thinking`：杏仁上移偏右+思考点 | `emotion=thinking open_eyes=1` | `thinking.jpg` |

每轮说完应自动回到待命月牙+平嘴（串口再出现 `emotion=neutral`）。字幕仍是白字黑底；长按 BOOT 配网二维码盖在脸之上。`surprised` / `thinking` / `speaking` 每 4–7 秒眨一次约 150 ms。

只有待机脸 + 五张表情 + 不回归项都亲眼确认后，才把本节 `hardware_verified` 改为 true。编译、刷机、点屏拍击都不算。

**回滚 app（不动身份区、NVS、分区表）**

```bash
python -m esptool --chip esp32s3 -p PORT -b 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 32MB --flash-freq 80m \
  0x20000 firmware/esp32/artifacts/backups/pre-playback-barge-in-20260910/app-before.bin
```

回滚目标是刷之前的 overlay 0023 app（SHA `d7efa9859e12df3f9b54981b05d2f7ba84fd58951c242f2e3d9e1d79e9d6aaaa`）。不要写入 `0x10000..0x1ffff`，也不要用会写 bootloader / 分区表 / assets 的 `flash.sh`。表情固件会把 `display/theme` 写成 dark 并留在 NVS；回滚后仍是深色主题，不是故障。若需重刷当前 0024 app：确认 `firmware/esp32/artifacts/memoria-esp-vocat-app.bin` SHA 仍是 `9e52bdf44a1022dc23f9ffaab043ebb8c0acc426733e4f28ffa37dd5d2748186`，再 app-only 写 `0x20000`。

## 当前生产

现场回滚前读取容器 image ID、Compose override 和证据目录，不从本文猜测标签。普通制品只保留当前与一个已确认可运行的紧邻回滚；数据库、WAL、MinIO、安全备份不按两版本清理。

**Agent / Bridge**（容器 `memoria-agent-1` / `memoria-voice-core-media-bridge-1`）

- 当前：`memoria-agent:20260915-weather-followup-lifecycle-v1`，image `sha256:91c0eb36d2cc4ae35dce1d842308e7f4c0f7648ab9068908c1576661e287a398`，OCI revision `d7214554316bdaa13984c7b8ecf00aeb61693311`。Agent/Bridge 共用该 image，2026-09-15 **15:58:42 CST**（07:58:42Z）切流；gRPC 7001 PASS、双 healthy、restart=0；两容器各 **910 文件 SHA256** 与冻结源码匹配。栈 tag 仍为 `20260901-0945-wake-word-whitelist`，`/opt/memoria/current` 仍指向 `20260827-architecture-split-v1`。收据 `/opt/memoria/component-releases/20260915-weather-followup-lifecycle-v1/`，源包 22,343,680 bytes / SHA `e58e227154aeb075dd66e8e1e7d5e91979c82941c4cdaebbdb54f39b37de4d1a`。
- 紧邻回滚：`memoria-agent:rollback-20260915-weather-followup-lifecycle-v1-pre`（保留原别名 `memoria-agent:20260914-downlink-pacing-v1`，image `sha256:cfdc7358b3a548e3e9df0268bb4a87945c8e87c84c37fff4da435d9cc2941ff8`，revision `4bb70fc38397516d245202f0e8bd755e35c1f22b`）。旧 Compose override 所需标签保留，`ROLLBACK_POINT.txt` 与 `CUTOVER_RESULT.txt` 均经 SHA 校验；回滚证明可运行，不代表旧版天气/续问缺陷已修复。
- 独立依赖底座：`memoria-agent:20260912-p0-rollback-single-tag-agent-component` / `memoria-agent-runtime-base:uv-c34f031b4a40c7a7-af6e83d18883`，共同 image `sha256:3d46ca183984c2e5e7fd5f06e62b2eac6660049c637d1e9177d5c6874741364a`；当前与回滚均依赖它，不按历史业务版本删除。
- 有效 env SHA256（排序并保留末尾换行）切前后相同：Agent `31d95c9851c1c50c1b16b6cb5ada890479daafc654fd5414d82b678e9843d12e`，Bridge `f5b67cb63838038e3b101ed97812b3169cf368e90437f2af202cfca235f3d05d`；**17 个非目标容器** ID/image/StartedAt/env/restarts/health/status 全部未变。详见发布证据 `preflight.jsonl`、`postflight.jsonl`、`runtime-source-check.log`。
- 本版新增多日天气、VAD/静默/grace/watchdog 交接、ASR 异常恢复、prepare 绝对期限与重连旧结果 fencing；保留跨已提交区间救援防重复门、ACK/正文 handoff 与有效输出恢复。Bridge 下行计量已明确为 **post-pacer send**，不能外推 provider 产出速度或设备 I2S/DMA 状态；具体修复边界见上方同日条目。
- 2026-09-15 保留清理：只读预检后删除 allowlist 内 **18 个旧 Agent source-overlay image ID（其中 3 对共 6 个 tag，另 15 个 dangling）**，以及三个旧版本的 **6 项 source tar/build payload**。当前、紧邻回滚及其旧 override 所需别名、独立 runtime-base、非目标容器、manifest/收据均保留；清理后再次核对保护镜像 ID 与双 healthy。`df -B1` 可用 **63,834,279,936 → 63,985,270,784 bytes**，净增约 **144 MiB**，不能按镜像虚拟大小声称回收数 GB。证据 `outputs/acceptance/run-20260915-p0-03-weather-lifecycle-release/{retention-preflight.log,retention-apply.log}`；未清理 build cache、全栈 incoming 或任何数据库/WAL/MinIO/安全备份。

**Media Edge**

- 当前：`memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`。override `/tmp/media-runtime.override.yml`。healthy、restart=0。
- 回滚：`memoria-media-edge:20260901-0945-wake-word-whitelist`；override 备份 `/tmp/media-runtime.override.yml.pre-20260908-1600-vocat-interrupt-assist`。

**Control API**

- 当前：`memoria-control-api:20260911-subject-switch-device-notify-control-api`，overlay 源 `b2644124e002d1aac60b1b5a32c336c327af57bc`，image `sha256:2af29dde2e8c6f1f1781bef322fe3b8d7f3b5ea13d3017b191aa443aa58bcebd`。healthy、restart=0、OCI revision 已核对；仅覆盖 `services/control_api/app/routes/device_control.py` 与 `multi_subject.py` 2 个文件，依赖层与有效环境不变。切流 `2026-09-11T04:55:06Z`。收据 `/opt/memoria/component-releases/20260911-subject-switch-device-notify-control-api/`。
- 回滚：`memoria-control-api:rollback-20260911-subject-switch-device-notify-control-api-pre-control`（原 `20260906-1458-account-device-discovery-control-api`，image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`）。
- 切主体现在会推进设备可见的 profile 版本并通知 Edge：`POST /v1/sessions/{session_id}/active-subject` 提交后调用 `project_device_profile_change`，在 `device_runtime_profile_ledger` 上按稳定指纹推进版本，只有真正变化时才向 Edge 发 `runtime_profile.invalidated`（`apply_at=next_safe_point`）。重复确认同一主体仍只提升 session_epoch，不推进设备版本、不重复通知；Edge 不可达只记日志并延后，不影响已提交的切换。真机端到端未验（切流时设备 `connected=false`）。
- 现网设备 `dev_atk_a4cb8fd6095c` 已改为 `audio_mode=interrupt_assist`（settings_version 12）。Control 镜像未切新设备默认值，旧票据不会自行升档。

**其它运行事实**

- 栈 env `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。对话 `qwen3.7-flash`，分类 `qwen-flash`，深查 `qwen-plus`。
- SenseVoice sidecar：`memoria-sensevoice-asr:20260901-pin-language`（默认 `zh`，未知语言 415）。回滚镜像 `v1` + `/opt/memoria/sidecars/sensevoice-asr/run_sensevoice_asr.py.rollback-20260901-prelang`。Dockerfile 只在服务器该目录，重建不可复现。
- 主人主体两库 `adult/verified`；声纹 `1b5b577b` **active**。CAM++ 反欺骗仍 `unavailable`，不要改成 `verified`。
- epoch **1892** 唤醒欢迎语听感 + Actual Heard PASS；随后 `conversation_end_explicit` 关闭。
- 自定义克隆已用于唤醒；天气正文音色绑定尚未真机复测。
- 小程序开发版 **0.8.84**（2026-09-06 23:58 CST，源 `7e5137a`，903,051 bytes）。未正式发布、未设体验版。
- 2026-09-13 23:40 CST 现场核验（**已被 2026-09-14 16:32 CST 取代**）：loopback `/health/live` 200；loopback 与公网 `/memoria-api/health/ready` 均 503，`smokes=expired/missing=[]`，具名 core **12/12 ready**，agent `ready/worker_ready/livekit_ready=true`、boot_id `e8b0983a-07a9-4e8f-83c1-39dc06cb3fc1`。当时记录为“切流前已有的 smoke 证据过期状态”，实际是 `refresh_readiness.sh` 的 stack tag 取自发布目录名导致 mark 恒 409 的假阴性，已于 2026-09-14 修复，见上节。

## 板卡与固件

- 硬件：乐鑫 ESP-VoCat N32R16（ESP32-S3，32MB Flash / 16MB Octal PSRAM）。board `memoria-esp-vocat`，app 2.4.2。现场 `192.168.8.142`，uuid `1ac87deb-0fa3-4300-a304-ad6c472ab8c7`。
- 音频：ES8311 输出 + ES7210 双麦，输入增益 **36.0 dB**。hello 报 `simultaneous_capture_playback=true`、`aec_mode=fd_low_cost`、`aec_reference=software_post_gain_pre_i2s`、`barge_in_level=1`；`aec_reference_verified=false`。
- 屏幕：1.85 寸 QSPI 圆屏 ST77916 360x360。触摸 CST816S：说话中单击硬停，聆听中单击退出聆听；**待机/连接中单击忽略**。
- IMU：BMI270。待机只认短拍（阈值 dx+dy+dz>3200、最多 120ms 脉冲、落地后再确认 60ms），冷却 2.5s，只闪 surprised。持续摇晃忽略；点屏 PRESS/HOLD mute IMU 400 ms。开麦权威仍是唤醒词「茉莉」或 BOOT。
- 身份区 `0x10000` 64KB 写保护，SHA `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`。OTA app `ota_0` `0x20000`。assets 8MB。
- **0025 历史候选（2026-09-14 19:05 CST app-only 刷入，已被后续版本替代）**：收据对应 `release_head=fa54d7de027cccaee35e1721762a5d0bb060d60c`，app **3,277,856 bytes** / SHA `f58f48a4b21df96df74750ed10638c2ca906f267f2228cf37add3e37fd4a9101`，ELF `16d981b31f45d0dbdaadc6d2ab8f19ab217c1f09ce96535ea0eea54c0cffa578`，ESP-IDF v6.0.2、overlay marker `9f6d083c…`。刷写只写 `0x20000..0x340fff`：写后全槽回读**逐字节一致**、erase 范围外字节未变、identity SHA `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846` 未变、bootloader/partition/nvs/otadata/phy-init 与 ota_1/assets MD5 全未变。收据与回滚件在 `outputs/acceptance/run-20260914-p0-03-firmware-metering/`（`preflash.json`、`protected/`、`rollback-app.bin`）。收据明确 `boot_verified=false`、`real_device_conversation_verified=false`，不用于证明后续版本已启用或验收。
- **当前板上 `PlaybackSupplyMeter` 候选（2026-09-15 18:51 CST 回读及启动通过）**：源码 `d1ad38f`、upstream `e8d8a401…`、overlay `24531273…b3`、app **3,280,832 bytes** / SHA `7d95c8a1…b65`、ELF `da6ebdd1…69f2`；完整摘要、具名分区保护结果与新 `postflash.json` / `boot-verification.json` 见「计量生命周期与捕获收尾」。仅更新 `0x20000` app；实际播放计量与听感待验，`code=true / wired=true / enabled=true_boot_verified / verified=false_pending_playback`，`pre-roll` 未实现。
- **紧邻旧板基线（本轮回滚依据）**：旧收据对应 HEAD `65257e0e1285a3126e484ad22c308315c971caad`，但当时为 dirty 构建，回滚依赖实际二进制，不能仅靠该 HEAD 重建。overlay `97fc64f28dcd95c365a99176d2ed26a749beb20c3e7f0179283c26484e7559e8`、app **3,280,512 bytes** / SHA `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`、ELF SHA `efe1b241c3d6b4126e3b2e9d57ab4bfbe76bbd6a4844b063c0a9aa8d160c58625`。旧版 11:22 启动与 16:18–16:19 首轮功能听感 1/3 通过，计量窗口提前关闭；旧证据仍在 `outputs/acceptance/run-20260915-p0-03-firmware-metering/`，不继承为新固件验收。
- **2026-09-15 11:34–11:39 旧板短捕获（计量无有效播放覆盖）**：目录 `outputs/acceptance/run-20260915-p0-03-firmware-metering/long-weather/session-1/`。设备 `vad_end -> first_received=0.517s`；generation 1 software queue wait 原始记录全部为 0（`supply/prestart/boundary/close_dropped/outside`），但同版统计窗口会在首帧前关闭，不能记有效零等待；Bridge `frames=120`、`audio_ms=2400`、`wall_ms=2413`、`max_gap_ms=69`、`after_pacer_send_ratio=0.99` 只说明发送侧节奏。delivery 有 `first_frame_sent/provider_completed/actual_heard/playback_ended`。串口 `SerialException: read failed: [Errno 6] Device not configured` 导致 `CAPTURE_HEALTH degraded`，`agent.log` 为空；不计完整长天气、有效播放计量或用户完整听完。该段操作员只给出“说完了”，与后来 16:18–16:19 明确通过的听感确认分开。
- 历史候选（非当前回滚）：commit `e1c6998`，app 3,277,328 bytes / SHA `b411838342db5cd07fec492c5baf6762d964cc58eb5df8ea7bdcb5b33caa52e6`，ELF `7eb96fe1…`，overlay `03fcdef5…`。
- 历史基线（已不在板上）：2026-09-13 01:56:16 编译的 2.4.2（ELF `5e7b0150…`）；上一轮 AEC 候选 commit `76ba6ad`，2026-09-14 09:54:29 编译，app 3,276,480 bytes / SHA `6bcca089d996cd7cb3c25daca9dccfacc45554b426a5497345ddf21e5c22001b`，ELF `3b5de210…`，overlay `6d17aa95…`。冻结包 `outputs/acceptance/run-20260914-aec-fix/candidate-app.bin`。
- **唯一紧邻回滚目标**：`outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/rollback-app.bin`（app `dfba3d61…`、3,280,512 bytes）；同目录 `protected/app-before-full-slot.bin` 为本轮写前 **0x20000/0x3f0000** 整槽，SHA `cc175040af934575b7804ec01031ebf8543415c98c0084801682e8ce121da333`，与旧版已验证的 `protected/postflash/app-after-full-slot.bin` 完全一致。不要误用旧 run 的 `rollback-app.bin` 或早期 AEC 镜像。回退仍只写 app，并重新做身份/非 app 分区保护、回读及启动验收；本轮未执行回退。
- 本轮受保护分区前后像位于 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/protected/`（0700/0600，禁止输出身份内容）；旧 run 证据未删除，普通固件保留清理待新候选实际会话验收后处理，不启动自动备份。
- 远场 30~60cm 双轮曾在 epoch 1417 PASS（ES7210 36.0 dB）。嘈杂环境定量抗噪未做。普通固件更新只 app-only 写 `0x20000`，不要跑 `flash.sh` 整包。

## 设备启用、唤醒与 shadow

配网闭环已于 2026-08-27 验证：二维码 introspect → BLE Protocomm Security 1 → claim/binding → Activation Manifest → ACK → `ready_for_conversation`。设备 `dev_atk_a4cb8fd6095c`（BLE `MEM-095C`）`activation_version=3`。这不等于语音对话或屏幕验收。小程序不采集声纹或实时语音。

唤醒词默认「茉莉」（`mo li`），白名单 `mo_li` / `mei_mo_li_ya`，自定义拼音 v1。主人静默 10 秒由 Python Voice Core 计时，裸 VAD 不能重置；「没听清」提示不再把窗口刷满。epoch **1892** 已听到 allowlisted 欢迎语（Actual Heard + `playback.ended`），随后 `conversation_end_explicit` 关闭。两音节误唤醒的电视 / 家庭噪声仍未定量。不要给动态欢迎语走非 allowlist 生成。

`TurnPhase` 只作 `ConversationProjection` 内部证据和低基数 telemetry，`enabled: false`，不改变 endpoint / interrupt / commit。FCDR 影子指标未发布，不得据此调策略。

## 生产拓扑

- 当前 runtime：`/opt/memoria/current` 原子软链；候选目录：`/opt/memoria/releases/`。
- Control SQLite：`/data/memoria.sqlite3`（挂载 `/data <- /var/lib/memoria`）。
- 已退役 H5：`/memoria-h5` 固定返回 `410 Gone`，不再发布或切流静态前端。
- LiveKit：`livekit/livekit-server:v1.13.5`，Compose project `memoria-livekit`。
- Control API loopback：`127.0.0.1:8791`；legacy mini gateway：`127.0.0.1:8792`；legacy device gateway：`127.0.0.1:8793`；direct Edge device WSS：`127.0.0.1:8794`。
- Voice Core Media Bridge 生产容器名：`memoria-voice-core-media-bridge-1`（与 Agent 同镜像）。
- 终身档案：PostgreSQL 17 + pgvector；对象：MinIO；设备共享权威：独立 mTLS Redis。
- Edge override：`/tmp/media-runtime.override.yml`。

ESP32 Direct 正式入口是 `wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`。公共 8080 不承载设备 WSS。

`wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 是已退役的原生小程序媒体兼容回滚入口，只能用于明确的 legacy 回滚。443/8443 Nginx 必须继续包含：

```nginx
include /etc/nginx/snippets/memoria-miniprogram-media.conf;
```

不得把该 legacy 路径重新接入当前小程序产品链，也不得修改同机 WMS 的既有根路由和数据。

## Secret 与安全边界

生产 secret 分到 `/etc/memoria-control-api.env`、`/etc/memoria-agent.env`、`/etc/memoria-speaker-model.env`、两个 legacy gateway env 和可选 `/etc/memoria-media-edge.env`，均为 `root:root 0600`。候选 env 从当前 root-only 源复制并用 `scripts/split_production_env.py` 分流；仓库、命令行、浏览器、日志、manifest 和回执不得包含值。

内部 capability token 两两不同、至少 32 字符，不能替代账号身份。设备 ticket、LiveKit token 和 H5 auth token 都必须短期、绑定 audience/subject/fence。生产 direct-device 缺少 mTLS Device State Redis 时 Media Edge 必须 fail closed，不回退进程内 map。

## 发布前门禁

1. 在干净 worktree 锁定 source commit/tag，确认只包含目标 slice。
2. 运行 Python、小程序、Go、契约、镜像和 `git diff --check` 门禁；供应商与 LiveKit smoke 必须使用候选容器。
3. 冻结 source、images、manifest、verifier 的 SHA-256；验证 OCI revision/role/architecture。
4. 现场记录当前容器 ID/image ID、软链、env 摘要、数据快照和一个可运行回滚点。
5. 先 dry-run，再上传/验证，再切流。任何 manifest、readiness、provider、数据、回滚或非目标容器门禁失败都 REJECT。

`deploy_agent_component.sh` 在确认 worktree 干净之后、SSH 之前跑 ruff、`check_module_budget.py check`、`mypy services/agent --strict`、Agent 单测与部署契约；用 `env -u LISTENER_CUES_ENABLED -u LIVEKIT_ADAPTIVE_INTERRUPTION -u OFFLINE_MOCK -u INTERRUPTION_MIN_DURATION_S` 剥掉本地 `.env`。`--skip-gates` 必须在收据写明理由。`main` 的 agent + python CI 必须绿。Agent-only 切片只允许 `services/agent/**`；overlay 无关漂移可用 `--allow-scope-drift`。Cutover 用 Control 的 `MEMORIA_RELEASE_TAG` 做 compose 插值，不要用 Control 镜像 label 当栈 tag。

两条容易踩的快速通道硬约束：① `.dockerignore`、`pyproject.toml`、`uv.lock`、`infra/Dockerfile.agent` 是依赖输入，`base_commit..expected_commit` 里任一被改动就直接 REJECT，与 `--allow-scope-drift` 无关——所以 agent-only 切片**不能**改 `[tool.memoria.module-budgets]`，要么原地压缩，要么走完整镜像路径。② `check_module_budget.py check` 要求行数与配置**精确相等**，多一行少一行都判失败；`update` 只会收紧、永不放宽，`--allow-scope-drift` 不覆盖这一条。

③ 上传段用 `rsync --protect-args`，**PATH 里的 `rsync` 必须是 ≥3.0**。macOS 自带 `/usr/bin/rsync` 是 openrsync 2.6.9，会以 `rsync: unrecognized option '--protect-args'` 在 `install -d` 之后、远端构建之前失败（生产不受影响，但服务端会留下一个空的 `component-releases/<tag>/` 目录，重跑会复用它）。脚本非交互 shell 若没有 `/opt/homebrew/bin`，先精确注入：`mkdir -p outputs/.tools && ln -sf /opt/homebrew/bin/rsync outputs/.tools/rsync`，再以 `PATH="$PWD/outputs/.tools:$PATH"` 运行。同一类问题：WorkBuddy 沙箱跑门禁要 `env -u PYTHONPATH`（宿主 shim 拦 `mkdir`，pytest 收集期报 EEXIST），`uv` 也不在默认 PATH。

```bash
scripts/deploy_agent_component.sh \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-image "memoria-agent:$BASE_TAG" \
  --expected-commit "$SOURCE_COMMIT" \
  --dry-run

scripts/deploy_agent_component.sh \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-image "memoria-agent:$BASE_TAG" \
  --expected-commit "$SOURCE_COMMIT" \
  --allow-scope-drift \
  --cutover
```

## 完整制品上传与校验

构建机先生成 portable verifier 和 manifest；manifest 必须显式绑定 source 与 images 两个主工件：

```bash
uv run python scripts/package_release_verifier.py \
  --output "$ARTIFACT_DIR/release-verifier.pyz"

uv run python scripts/create_release_manifest.py \
  --release-tag "$RELEASE_TAG" \
  --expected-commit "$SOURCE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --output "$ARTIFACT_DIR/release-manifest.json"
```

`images.tar + images.tar.sha256` 必须成对上传。构建机在工件目录内校验，避免把目录前缀重复拼接：

```bash
for artifact in source.tar images.tar release-manifest.json release-verifier.pyz; do
  (cd "$ARTIFACT_DIR" && sha256sum "$artifact")
done
```

先 dry-run，再使用当前基座做 seeded upload。不要对 basis 使用 `rsync --inplace`；远端必须保留可验证的不可变基座，上传失败不能污染它。

```bash
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-tag "$BASE_TAG" \
  --dry-run

scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-tag "$BASE_TAG"
```

把构建机可信摘要带入已认证运维 shell，先验 verifier 与 manifest，再运行 verifier；校验通过前禁止解包：

```bash
: "${MEMORIA_RELEASE_VERIFIER_SHA256:?required}"
: "${MEMORIA_RELEASE_MANIFEST_SHA256:?required}"
printf '%s  %s\n' "$MEMORIA_RELEASE_VERIFIER_SHA256" "$UPLOAD_DIR/release-verifier.pyz" | sha256sum -c -
printf '%s  %s\n' "$MEMORIA_RELEASE_MANIFEST_SHA256" "$UPLOAD_DIR/release-manifest.json" | sha256sum -c -

python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" \
  --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" \
  --expected-commit "$SOURCE_COMMIT" \
  --verify-imported-images

tar --extract --file "$UPLOAD_DIR/source.tar" --directory "$CANDIDATE_DIR"
```

不允许手工 retag 缺少 manifest 绑定的模型镜像。切流后运行 `scripts/smoke_server_deployment.sh`、provider smoke、容器健康、私有 readiness、外部 Host/SNI 路由和延迟复核。

## 数据层启动、备份与恢复

数据 Compose 必须从当前 release 的真实目录启动。先让 runtime Compose 创建共享网络，再启动数据层；不得在其他工作目录用相对路径创建同名卷或网络：

```bash
DATA_COMPOSE_DIR=/opt/memoria/current/infra
cd /opt/memoria/current
docker compose -f docker-compose.production.yml create --no-build
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" up -d
```

发布前联合备份至少覆盖 PostgreSQL base backup/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest 和恢复回执。备份成功只表示“可尝试恢复”；必须定期运行隔离的 `scripts/run_offsite_restore_drill.sh`，通过 `pg_verifybackup`、对象清单、哈希、外键和应用级读取后才能称为已验证恢复。

搜索/档案投影是可重建数据，不从缓存当权威恢复。恢复不可变证据与 claims 后，在 Control API 镜像中执行：

```bash
python -m scripts.rebuild_memory_projections --confirm-rebuild
```

当前同机 PostgreSQL/MinIO/WAL 不能被宣传为异地灾备、PITR 或“永不丢失”。删除普通 release 制品前必须再次确认目标不是数据库、安全或合规备份。

### 2026-09-14 备份现状核查与本地隔离恢复演练

结论：**自动备份从未真正运行过**，但本轮首次建立了经过校验、可恢复的本地还原点。证据目录 `outputs/acceptance/run-20260914-p0-02-restore-drill/`（`report.json` + `drill-notes.md`）。

现状（只读核查）：

- `offsite-backup` profile 从未启动。`/etc/memoria-offsite-backup.env` 不存在，`postgres_backup_staging` 卷不存在；异地 endpoint 只有 `infra/memoria-offsite-backup.env.example` 的 `.invalid` 占位，**没有异地副本，也没有备份告警**。
- `infra/backup/postgres-base-backup.sh` 的 `pg_basebackup --dbname=postgres` 在 PG 17.8 报 `missing "=" after "postgres" in connection info string`（客户端参数解析错误，已复现）。该脚本此前从未被执行，所以这个错误一直没暴露。
- `pg_hba.conf` 只对 `127.0.0.1`/`::1`/本地 socket 放行 `replication`（`host all all all scram-sha-256` 不覆盖复制连接）。因此独立备份容器走 `memoria_default` 网桥会被拒：`no pg_hba.conf entry for replication connection from host 172.19.0.14, user memoria_admin, no encryption`（已复现）。
- 后果：2026-08-27 起累计的 **1503 段 / 23.5GB WAL 归档此前没有任何 base backup 可配对**，单独不可恢复；`MEMORIA_WAL_LOCAL_RETENTION_DAYS=7` 的裁剪只由从未启动的 offsite-mirror 执行，所以 WAL 从未裁剪。

本轮演练（隔离，未改生产配置、未覆盖生产数据）：

- 用 `--network container:memoria-data-postgres-1` 共享网络命名空间走 loopback 复制，`pg_basebackup --format=plain --wal-method=stream --checkpoint=fast --manifest-checksums=SHA256` 生成 base backup，**2s / 71,814,936 bytes**，落在 `/var/backups/memoria/drill-20260914-p0-02/base`（root-only，保留）。
- `pg_verifybackup`：`backup successfully verified`。
- 隔离恢复：复制到 `restore-data` 后用 `--network none` 起独立容器，**5s** 到 `pg_isready`；应用表 `archive_evidence_events` / `guardian_links` / `tutor_practice_sessions` 均存在；`archive_evidence_events=2305`、`voice_samples=1`、`archive_evidence_blobs=0`、`guardian_links=0`；cluster state `in production`。验证后容器与 `restore-data` 已删除，只留 69MB 备份与报告。
- 对象核对：恢复副本的 `voice_samples.object_key` 在 MinIO `memoria-voice` 同 key 存在（134KiB，ETag `94c6430d991fe7bcad12c57cab452800`），原始密文 SHA256 `05ca85ac…012d`。
- WAL 连续性：首段 `000000010000000000000001`、末段 `0000000100000005000000DF`，**0 缺口**；base backup 的 `START WAL LOCATION` 正是末段（`2026-09-14 08:37:56 UTC`，timeline 1）。所以「base backup + 现有 WAL 归档」现在构成同机可恢复链。
- 既有手工备份仍有效：`/var/backups/memoria/20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump` 的 `sha256sum -c` OK、`pg_restore --list` 1210 项。

决定（2026-09-14，用户）：**项目验证阶段暂不启用自动备份与异地副本**。本阶段不新增 `offsite-backup` 容器、不配置异地 endpoint；已有的本地 base backup 与 9/12 手工 dump 保留，但不会自动更新。重新评估的触发条件：开始对真实家庭提供服务或写入真实家庭数据、正式发布前、或数据价值/量级显著增长。届时三条启用路径——(a) 修 `--dbname` 并在 `pg_hba.conf` 放行 `memoria_default` 网段的复制连接；(b) 备份容器改 `network_mode: service:postgres` 走 loopback，不改 pg_hba（推荐）；(c) 仅在 DB 容器内执行。异地副本另需真实 endpoint 与凭据。本机 WAL/MinIO/DB 仍同盘，**不能声称异地灾备或 PITR 已具备**。

不受该决定影响、仍需处理的连带问题：WAL 归档仍在写且**无人裁剪**（`MEMORIA_WAL_LOCAL_RETENTION_DAYS` 只由从未启动的 mirror 执行）。

2026-09-14 按用户授权做过一次回收：删除严格早于 base backup `START WAL LOCATION` 的 **1502 段 / 25,199,378,432 bytes**。这些段对应的还原点本来就不存在（8/27 起没有任何 base backup，9/12 那份是逻辑 dump、不需要 WAL），属于纯占用。只保留 3 个文件：`0000000100000005000000DF`、它的 `.00000028.backup` 历史文件和更新的 `0000000100000005000000E0`。结果：根盘 `/dev/vda2` 从 **77G 用 / 37G 可用（68%）** 变为 **54G 用 / 60G 可用（47%）**；`pg_verifybackup` 复验仍 `backup successfully verified`；`pg_stat_archiver` 的 `failed_count` 未变化（16032），删除后归档继续推进到 `…E0`（08:42:57Z）。**只动了归档卷，未触碰 `pg_wal`，未重启 PostgreSQL。**

这只是回收历史积压，不解决增长：归档仍以约 1GB/天量级累积（实测 8/27→9/14 共 1504 段），60GB 可用大约 **6 周**后再次写满。三个可选终态：(a) 加一个**只裁剪**的定时任务（沿用 `find /wal-archive -mtime +N -delete`，不是备份），(b) 停用归档（改 `archive_mode`，需重启 PostgreSQL），(c) 维持现状、临近阈值再手工回收。本轮未安装任何定时任务。

## 回滚

回滚以组件最小范围执行：冻结失败候选日志和 manifest，恢复切流前 image/软链/env，等待健康与具名 gRPC/readiness，再重跑外部路由和 provider smoke。若 Edge 长连接未自动重拨，按当次回滚回执中的受控步骤处理，不能假定重启无副作用。

固件回滚与此独立：只回写 app 分区 `0x20000`，不要跑 `flash.sh` 整包、不要 `erase-all`。紧邻回滚件与回退方法见「板卡与固件」——写前先备份当前 `0x20000/0x3f0000` 全槽并逐字节匹配，写的整个过程保持 identity/NVS/otadata/bootloader/分区表/assets 不变。

回滚完成后记录当前与回滚两个可运行版本，删除更早普通上传包、构建归档、候选和回滚镜像并检查磁盘。保留失败证据的摘要和服务器路径即可，不在仓库新增 release 文档。

## 验收门槛

T1–T14 的原始 evidence/receipt 写到被忽略的 `outputs/acceptance/`，通过 `scripts/hardware_realtime_acceptance.py` 验证。覆盖设备启动、票据、防重放、上行、首帧、播放终态、硬停、网络恢复、连续轮次、AEC/双讲、长稳和小程序独立性。

以下任一出现都 REJECT：

- 首个可播放下行帧不是当前 fence 的 sequence/sample 0/0。
- 旧 session/stream/generation/tool epoch 产生可听输出或档案写入。
- playback terminal 缺失、错误被记为完成、Actual Heard 从网络发送量推断。
- Redis/Bridge/Provider authority 不可用时回退到并行本地权威。
- 真实设备、真实 provider 或真实网络证据被本地 mock、零会话 readiness 或旧候选证据替代。
