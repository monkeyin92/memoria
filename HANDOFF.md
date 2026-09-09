# Memoria 当前交接

本文件只记录当前有效状态、紧邻回滚边界、生产操作和下一验收。历史发布过程不再保留；每次发布原地更新本文件。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-09
resume_checkpoint: vocat_interrupt_assist_wake_tail_connect_vad_cutover_awaiting_greeting_listen
firmware_face_acceptance: flashed_awaiting_visual_idle_and_five_expressions
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.83
miniprogram_account_device_sync: devtools_verified_phone_desktop_pending
production_readiness: not_ready_smoke_evidence_expired
production_readiness_observed_at: 2026-09-06T20:49:35+08:00
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
current_work_order: vocat_interrupt_assist
code: complete
wired: agent_bridge_edge_cutover_firmware_flashed
enabled: production_agent_bridge_edge_true_device_audio_mode_interrupt_assist
verified: production_runtime_provider_model_inference_identity_safe_board_boot_secure_device_onboarding_owner_silence_standby_and_device_wake_ack_cutover
production_runtime_verified: true
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: present_unverified
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
```

`full_duplex_verified` 只有真实硬件 AEC、双讲、打断、连续会话和 Actual Heard 证据全部通过后才能改为 true。在此之前产品不得宣传全双工。小程序不申请 `scope.record`，也不承担实时媒体回滚职责。

## 硬件验收断点（2026-09-05 21:08 CST；小程序发布状态于 9 月 6 日更新）

```yaml
resume_focus: vocat_interrupt_assist_greeting_listen_then_expression_and_barge_in
work_order: vocat_interrupt_assist
firmware_ns: flashed_webrtc_two_turn_and_short_farewell_pass
llm_conversation: qwen3.7-flash
llm_classifiers: qwen-flash
demo_script_two_turns: PASS_epoch1379
lookup_filler_fence_fix: published_dup_start_and_stack_env_healed
weekday_after_weather: published_retest_fail_target_non_owner
epoch1386_filler_speaker_fix: published_awaiting_board_retest
subject_adult_verified: true
identity_subject_adult_verified: true
account_registration_sync: published_backfill_and_idempotence_verified
speaker_enrollment_state: active
speaker_enrollment_intent_id: 6bcb7345-d65d-4264-bd2c-b7c9b8927585
speaker_profile_id: 1b5b577b-669e-4573-b9b1-ea1dd8122ee4
speaker_profile_status: active
last_wake_epoch: 1390
device_enrollment_prompts: quality_enroll_is_owner_active
miniprogram_devtools_publish: uploaded_0.8.83_devtools_cli
direct_real_device_verified: false
```

**已完成（本会话）**

1. **半双工双轮**：天气 + 星期几 PASS（epoch **1367**）。
2. **主人声纹门禁**：演示账号「主人」已 **adult/verified**。
3. **epoch 1370**：四段都收进内存（speech_ms 580/760/440/440），提交 embedding 在 1.0s 超时返回 503；intent 被提前 consume，无 profile。板子说「这段没录上」是通用失败词，不是只丢第四段。
4. **epoch 1371**：`state=required intent=no`，只播唤醒短句后停在聆听中。样本未落库，不能只补第四段。
5. **提交/重试修复已切流**：Agent/Bridge `memoria-agent:20260903-1745-enrollment-keep-intent-agent-component`，Control `memoria-control-api:20260903-1745-enrollment-keep-intent-control-api`，源提交 `e781cb0`。
6. **epoch 1372 四段提交成功**，随后按开箱流程改为合格登记即 **active**。profile `1b5b577b` 已升为 **active**（2026-09-03 10:22 UTC）。CAM++ 无反欺骗头仍返回 `unavailable`，但不再挡住主人匹配；不得把该字段改成 `verified`。Control overlay `20260903-1820-owner-enroll-active-control-api` / `1a7a939`。
7. **2026-09-04 10:17 CST 已刷 WebRTC NS 候选固件**（未重建；身份区未写）。串口确认 `Initialized FD AFE, detector: MultiNet, NS: webrtc`。
8. **epoch 1379（10:27–10:28 CST）NS 固件现场双轮 + 短告别 PASS**：唤醒「茉莉」→ 星期几（clock-fact，`text_len=6`，FunASR RMS 470）generation 2 Actual Heard + `playback.ended` → 天气（live-query，`text_len=10`，FunASR RMS 685）generation 4 Actual Heard + `playback.ended` → 短告别 `text_len=6` 走 `conversation_end_explicit`，Edge `reason=conversation_end_explicit`，设备 `listening -> idle`。操作员听感两轮都有完整回答、再见立刻待命。证据：`outputs/acceptance/run-20260904-flash-ns/`（serial / bridge.log / tap-epoch1379.wav）。
9. **epoch 1381 天气垫话未出声**：OpenMeteo 2137ms，不是快路径。gen2 ACK 被 `fence_mismatch` 拒（帧 turn2/gen2，会话仍 turn1/gen1），`actual_heard=False`。
10. **垫话 fence 修复已切流**：源提交 `fbdce113`，标签 `20260904-lookup-filler-fence-agent-component`。PCM 前补 `output_generation_start`。
11. **epoch 1382 垫话回归**：同一 fence 第二次 `generation.started` 被固件拒（`strictly advance`），会话拆掉，只听到「稍」后待命。
12. **重复 START + 409 已切流**：源 `05968a32b3bafbbd91de089a3cad46fcdc156261`，标签 `20260904-dup-start-stack-env-v2-agent-component`。同 fence 第二次 START 不再下发；cutover 用 Control 的 `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。Agent/Bridge **healthy**，heartbeat 已 recorded，切流后 409 为 0。`/tmp/media-runtime.override.yml` 去掉钉死 8 月 Agent/Bridge 镜像的旧段，只留 Media Edge。回滚 `rollback-20260904-dup-start-stack-env-v2-agent-component-pre-agent/-pre-bridge`。
13. **epoch 1384（21:59 CST）天气垫话 PASS，星期几 FAIL**：session `6209dcad-ae73-47bc-9ad6-566e10d286cf`。唤醒 gen1 Actual Heard → 天气 live-query 提交（`text_len=7`）垫话被天气答案 preempt（听感完整「稍等」+ 天气）→ 星期几 FunASR `text_len=8` early clock-fact pin `endpoint=154880`，`commit` 因 `projection_range_mismatch` 丢弃 → overlap recovery 再 pin `205760` 但 `turn_start` 空，2.5s tail timeout 记 `asr_empty_class=low_rms`（误分类；该段 RMS 2435）→ `owner_silence_timeout` 待命。串口 VAD 有第二轮；没有 generation 3。
14. **星期几提交修复已切流**：源 `90d4dff6c3add86f01c935e2885b3873e1e2f0fb`，标签 `20260905-weekday-projection-range-agent-component`。commit 前 `align_provisional_range` 扩展投影区间（抽到 `conversation_projection_range.py` 以保持 1067 行预算）；clock-fact overlap recovery 走 `_observe_final_asr_result` 补 `turn_start`。单测 PASS。Agent/Bridge **healthy**、restart=0、overlay import PASS。回滚 `rollback-20260905-weekday-projection-range-agent-component-pre-agent/-pre-bridge`。
15. **epoch 1386（00:29–00:30 CST）复测 FAIL**：session `0f7b81d0-59e1-40f8-b8f2-4ee0c16fdf87`。天气 live-query `text_len=10` 已 commit；垫话 gen2 `first_frame` 后被取消（`preempted`/`output_task_cancelled`），听感「稍」截断再完整「稍等，我查询一下」+天气。随后两轮日期/星期几 ASR 已 pin，`align_provisional_text` 成功，**没有** `projection_range_mismatch`；回复被 `target_non_owner` 掐掉。再见同样 `target_non_owner`，Edge `reason=owner_silence_timeout`。档案 `speaker.classified`：天气轮 `uncertain`/`ambiguous_score` score **0.4706** quality 0.91；日期轮 `guest`/`owner_mismatch` score **0.3373** quality 0.73（profile `1b5b577b`，阈值 owner 0.78 / guest 0.40）。后轮未再 classify，沿用 mismatch。
16. **垫话出声 + 播后声纹修复已切流**：源 `8a8eb9ade975b7eae1c3709e5d3434bff26a8d6f`，标签 `20260905-filler-post-playback-speaker-agent-component`。已出声 FAST_ACK 不再 preempt；同 turn `first_frame` 后天气去掉 `LIVE_LOOKUP_FILLER`；无 AEC 播后 2s 内 quality 低于 0.85 的 formal guest 记 `post_playback_untrusted`，并丢掉 400ms preroll。不放宽 `reject_non_owner_voice`。门禁 PASS。Agent/Bridge **healthy**、restart=0、overlay import PASS。回滚 `rollback-20260905-filler-post-playback-speaker-agent-component-pre-agent/-pre-bridge`。
17. **epoch 1388/1389 复测**：1388 唤醒后星期几仍 `target_non_owner`。1389 session `09f38e59` 星期几+天气都答了，但每轮 `first_frame` 后 `output_task_cancelled`，听感「稍」截断再正文。星期几被语义分类器当成联网查询（`interaction_delegation_started` + qwen-plus 7 字）；天气 LLM/垫话出声后被 DEEP_RESULT 掐。
18. **clock-fact 不再垫话 + 已出声不 flush 已切流**：源 `59833bd5deb3c15b98da4261d4252c7357297079`（代码 `aa2435d`），标签 `20260905-clock-fact-heard-playback-agent-component`。日期/时间不再 live-lookup；lookup claim 未就绪时不抢先开 LLM；已出声 owner 不被 DEEP_RESULT flush。Agent/Bridge **healthy**、restart=0、overlay import PASS。回滚 `rollback-20260905-clock-fact-heard-playback-agent-component-pre-agent/-pre-bridge`。
19. **epoch 1390 车票「稍」截断**：session `a42ee4c1`。星期几/天气 PASS；车票 live-query `text_len=26` 垫话 `first_frame` 后被并行 LLM 抢占（qwen-plus 5661ms 之后才播正文）。
20. **半双工已出声不 flush + lookup 不再并行 LLM 已切流**：源 `3a1133c5cb63f2473556680f01c7c1e56003968d`，标签 `20260905-half-duplex-heard-lookup-v2-agent-component`。联网查询中不另开 conversation_reply；设备 `barge_in=false` 时已出声 owner 不再 preempt。mypy 修了 `heard` 变量冲突。Agent/Bridge **healthy**、restart=0、overlay import PASS。回滚 `rollback-20260905-half-duplex-heard-lookup-v2-agent-component-pre-agent/-pre-bridge`。
21. **天气正文音色绑定已切流**：源 `18ea9c798b87cfca73292586149096088c9c69e2`，标签 `20260905-lookup-voice-bind-agent-component`。音频轮在 `on_turn_committed` 启动查询前先对齐并绑定助手音色；失败则不启动垫话/查询，避免天气结果被新代次丢弃。身份 epoch 轮换保留 companion style。拒绝日志带 `reason=`。门禁 PASS。Agent/Bridge **healthy**、restart=0、overlay import PASS。回滚 `rollback-20260905-lookup-voice-bind-agent-component-pre-agent/-pre-bridge`。
23. **长天气 45s 墙钟超时已切流**：源 `fb0311d06d2add524e791d98386cdde808e61165`，标签 `20260908-1300-output-stall-timeout-agent-component`。epoch 1418 天气 `first_frame` 后约 44s 被 `output_timeout` 掐断（`provider_completed=False`），Edge WSS `close_code=1005`，屏上「连接中」后 epoch 1423 自动再播唤醒。根因是下行 PCM 按 24 kHz 实时节奏发送，整代次却套 45s 墙钟。现改为每成功下一帧 PCM 重置超时；卡住仍 abort。门禁 PASS。2026-09-08 13:03 CST 切流，Agent/Bridge **healthy**、restart=0、容器内 overlay 含 `_bump_output_stall_deadline`。回滚 `rollback-20260908-1300-output-stall-timeout-agent-component-pre-agent/-pre-bridge`（镜像 `20260906-0105-companion-clone-weather-bind-agent-component` / `sha256:9f0de07f72968a7a35598c262607e5a95b81f0d526db084812c0a4fd06c9a81c`）。**长天气尚未真机复测**。
24. **hello `audio_mode` 身份比对已切流**：源 `8170117880bbee4097ff1e61cea2af7802677858`，标签 `20260908-1815-hello-audio-mode-identity-agent-component`。`interrupt_assist` 切流后 hello 把 `audio_mode` 写进 Python 会话身份，Edge PCM/VAD protobuf 不含该字段，Bridge 全等失败并掐 gRPC；设备 `dev_atk_a4cb8fd6095c` 停在「连接中」，session `d4f2277e` 约每 5 秒重连（epoch 1640→1880）。现比对忽略 hello-only `audio_mode`，错误 session 仍拒绝。2026-09-08 18:18 CST 切流，Agent/Bridge **healthy**、restart=0、容器内 overlay 含 `matches_event_identity`。切流后 identity mismatch=0；epoch **1881** 已 ingest ASR 并以 `conversation_end_explicit` 关闭，之后无 5 秒重连风暴。回滚 `rollback-20260908-1815-hello-audio-mode-identity-agent-component-pre-agent/-pre-bridge`（镜像 `20260908-1600-vocat-interrupt-assist-agent-component` / `sha256:eb093dddfb2035d5270b01a15dab5a64c66077ceee1bfb3ca2df4069d99a48d1`）。**屏幕离开「连接中」、表情和 barge-in 尚未操作员确认。**

**下一步（按顺序）**

1. 看屏幕是否已离开「连接中」。需要时重新唤醒建立新会话，真机复测**长天气完整播报**（不应在约 45 秒停、不应只听到「稍等我查询一下」、不应掉线进「连接中」），以及主人匹配、星期几和车票。账号资料已跨库补齐，但不代表本轮声纹已匹配主人，也未证明车票截断完全解决；不要放宽 `reject_non_owner_voice`。
2. 微信里切到开发版 **0.8.84** 并刷新「我的」与「设备」，检查设备在线/可唤醒状态与首页新文案；上传不等于体验版已设置或正式发布。
3. 若要定量抗噪：在明确嘈杂环境再跑一轮，记噪声底和误/漏唤醒；本轮未单独测噪声。epoch **1374** 那句 17 字「……我知道了，再见」overlap 原句仍未定点复测。
4. 仍勿把 `direct_real_device_verified` 改为 true。

**勿做**：放宽 `reject_non_owner_voice`；伪造 owner；把未 active 的声纹当主人认证宣传。

## 屏幕表情：眼睛白描脸（2026-09-08）

```yaml
change: replace_64px_noto_colour_emoji_with_drawn_eyes_only_face
code: complete
wired: overlay_board_layer_memoria_face_display
verified: host_renderer_tests_preview_esp32s3_clean_build_and_overlay_gate
hardware_verified: false
next_owner_action: 拍待机 idle.jpg，再唤醒「茉莉」测五表情（runbook 第 3–7 步）
operator_reference: 圆屏白闭眼黑底照片
candidate_app: firmware/esp32/artifacts/memoria-esp-vocat-app.bin
candidate_app_sha256: ebc7462db0165e471ad0440140884383e445747098ce03a3099744c3ed55b20f
candidate_merged: firmware/esp32/artifacts/memoria-esp-vocat-merged.bin
candidate_merged_sha256: c9aac8b9dad46f77714b06af916dd07b2471eecbefb119c187dbec81c734da7f
candidate_built_at: 2026-09-08 21:28 CST
overlay_hash: d9003c80d16841b1f30f463d67ce14383b1943be424e8cddeb428e359322f5ee
upstream_ref: e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5
esp_idf: v6.0.2
evidence_dir: outputs/acceptance/run-20260909-0943-face
flash: app_only_0x20000
flashed_at: 2026-09-09 09:52 CST
port: /dev/cu.usbmodem101
backup_app: firmware/esp32/artifacts/backups/pre-face-20260909-0943/app-before.bin
backup_app_sha256: 1db35780e7db6188ec5a744de126b7f5bf925d5ff90b11c86403f71e35eb75e1
identity_sha256: b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846
identity_unchanged: true
device_ip: 192.168.8.142
device_uuid: 1ac87deb-0fa3-4300-a304-ad6c472ab8c7
on_device_compile_time: "Sep 8 2026 21:26:34"
visual_idle: pending_operator_photo
```

操作员要求：屏幕不要再显示 64px 黄色 Noto emoji，改成参考照片里「黑底白眼睛」的表情，并且六种情绪都用这套眼睛-only 风格。已按此实现。

**实现**

1. `overlay/files/main/boards/memoria/esp-vocat/memoria_face.cc|h`：纯 C++ 渲染器，**无 ESP-IDF/LVGL 依赖**（可在宿主编译校验）。几何按参考照片归一化到屏幕半径 R：眼半宽 **0.26R**、眼心 **±0.35R**、圆拱中心落在屏幕水平中线；闭眼 = 圆拱（半圆）被一条浅弧裁出的月牙（裁弧半径 2.7hw、圆心在基线下方 2.5hw），把裁弧下移即睁成整圆；边缘按 1px 覆盖抗锯齿。照片本身有约 2.4° 倾斜与透视，设计取对称。
2. `memoria_face_display.cc|h`：`MemoriaFaceDisplay : SpiLcdDisplay`。360x360 RGB565 帧缓冲放 PSRAM（259 KB），作为 `container_` 的 `bg_image_src`；`content_`/`top_bar_` 背景透明，字幕与状态文字仍画在脸之上；隐藏上游 `emoji_image_`/`emoji_label_`；把板卡钉到 **dark** 主题（浅色主题会把黑字画到黑屏上）。PSRAM 与内部 RAM 都分配失败时回退上游彩色 emoji 路径。
3. 六种脸：`neutral`（参考图：平放月牙）、`happy`（外眼角上挑的眯眼）、`sad`（外眼角下垂的细月牙）、`surprised`（圆睁）、`loving`（内倾柔和圆月牙）、`thinking`（眼睛上移偏右）。别名 `idle/sleepy→neutral`、`laughing/funny/delicious/confident/embarrassed/silly/relaxed/kissy/winking→happy`、`crying/angry→sad`、`shocked→surprised`、`caring→loving`、`curious/confused→thinking`；未知名字（含 `robot_2`、`cancel`）回落 `neutral`，不再出现黄色 emoji。名字大小写不敏感。
4. `surprised`/`thinking` 睁眼时每 4–7 秒眨一次（esp_timer 30 ms 帧、持 LVGL 锁重绘，闭眼脸不参与）。`Application` 在 idle/connecting/listening 都会 `SetEmotion("neutral")`，所以说完自动回到待机闭眼。
5. 未改协议、未改 `screen.expression` 语义，服务端映射仍是 `happy/sad/surprised/loving/thinking/neutral`。

**已做验证（无硬件）**

1. `firmware/esp32/tests/test_memoria_face.py` 用宿主 clang++ 直接编译固件渲染源并断言几何契约：眼宽 94 px、眼心 117/243、月牙高 47 px、眼尖落在中线、六种情绪各两只眼、happy/sad/loving 眼角方向、surprised 圆度、thinking 上移偏右、眨眼收敛、别名/大小写/未知回落、分辨率缩放。24 项全过。
2. `firmware/esp32/scripts/preview_memoria_face.py` 用同一份源码生成 PNG 预览；与参考照片比对月牙轮廓 IoU 0.86（差异来自照片倾斜与透视）。产物已存 `outputs/firmware-face-20260908/`，其中 `device-view.png` 是套圆屏边框的对照图。
3. 全量 clean build（锁定 commit 重克隆 + 重放 overlay）通过；`./scripts/check-overlay.sh` 输出 `overlay check passed: memoria-esp-vocat`。候选见上方 yaml，app 分区余量 21%。

**已做真机刷写（2026-09-09 09:52 CST；本机 `/dev/cu.usbmodem101`）**

1. app-only `write-flash 0x20000` 候选 `ebc7462d…b20f`。身份区 `0x10000` 刷前刷后 SHA 均为 `b7a717fa…`。回滚 bin：`firmware/esp32/artifacts/backups/pre-face-20260909-0943/app-before.bin`（`1db35780…`）。
2. 硬复位后串口：Compile time `Sep  8 2026 21:26:34`，ST77916 创建成功，LcdDisplay 2MB PSRAM 图像缓存，无 `face buffer allocation failed`，`activating -> idle`，SSID `915`，STA `90:e5:b1:d7:83:2c`，IP `192.168.8.142`。
3. 候选 bin 含 `MemoriaFaceDisplay` 与 `emotion=%s open_eyes=%d`。开机无 `emotion=neutral`：`emotion_` 默认已是 `neutral`，`SetEmotion` 只在变化时打日志；这不是回退彩色 emoji。
4. 尚未拍 `idle.jpg`，五表情未测。`hardware_verified` 保持 false。

**下一步：真机视觉验收（步骤 0–2 已完成，从第 3 步继续）**

前置：一台已绑定可对话的 ESP-VoCat（`memoria-esp-vocat`，本仓 `firmware/esp32` 工作树）+ USB 数据线。对照基准 `outputs/firmware-face-20260908/device-view.png`（左上角是待机脸）。本轮只改 app 侧显示，**不动协议、分区表、身份区和 NVS**，刷完不用重新配网或重新绑定。本板已刷，不要重复刷机。

**0. 先确认候选没被改过（约 3 分钟，红在表情以外先停下报告）**

```bash
cd <repo>/firmware/esp32
./scripts/check-overlay.sh          # 期望最后一行：overlay check passed: memoria-esp-vocat
shasum -a 256 artifacts/memoria-esp-vocat-app.bin
# 期望 ebc7462db0165e471ad0440140884383e445747098ce03a3099744c3ed55b20f
cd <repo> && uv run pytest firmware/esp32/tests/test_memoria_face.py -q   # 期望 24 passed
```

**1. 接线、进入下载模式、备份当前 app**

```bash
cd <repo>/firmware/esp32
./scripts/flash.sh --list                       # 找 /dev/cu.usbmodem*，下称 PORT
```

按住 BOOT → 轻按 RESET → 松开 RESET → 松开 BOOT，进入下载模式。然后先备份当前在跑的 app（回滚用；ota_0 = `0x20000` 起 `0x3f0000`）：

```bash
stamp=$(date +%Y%m%d-%H%M)
mkdir -p artifacts/backups/pre-face-$stamp
python -m esptool --chip esp32s3 -p PORT -b 460800 read-flash 0x20000 0x3f0000 \
  artifacts/backups/pre-face-$stamp/app-before.bin
```

**2. 刷机并抓串口日志**

```bash
stamp=$(date +%Y%m%d-%H%M)
mkdir -p outputs/acceptance/run-$stamp-face
script -q outputs/acceptance/run-$stamp-face/serial.log ./scripts/monitor.sh --port PORT
```

只想快速刷一遍：`./scripts/flash.sh --port PORT --monitor`（刷完直接进 monitor，`Ctrl+]` 退出）。
刷写计划只含 bootloader / 分区表 / ota_data / app / assets，不含 `0x10000` 身份区（门禁会校验）。

**3. 待机脸验收（最关键的一条）**

上电待机时屏幕应是**纯黑底 + 两个白色平放月牙**，没有黄色 emoji、没有白底。拍照存 `outputs/acceptance/run-20260909-0943-face/idle.jpg`，与 `device-view.png` 左上角比对：眼距约占屏宽 35%，眼睛在屏幕中线以上，形状为「上圆拱 + 下浅弧」。
开机串口**不会**出现 `emotion=neutral`（ctor 默认已是 `neutral`，相同名字跳过）。应确认没有 `face buffer allocation failed`。第一次非 idle 表情变化才会打 `MemoriaFaceDisplay: emotion=...`。

**4. 表情验收（唤醒「茉莉」后逐条说，一条一拍照）**

触发源是**助手回复的文本/语气**（`services/agent/src/orchestration/prosody.py::mascot_expression_for_reply`），不是用户原话；所以要用能引出对应语气的话去问。判定标准：脸要和待机闭眼明显不同，且形状对得上 `device-view.png` 对应格。

| 你说 | 期望助手语气 | 屏幕脸 | 串口收据 | 存图 |
| --- | --- | --- | --- | --- |
| 「我拿到心仪的 offer 了」 | 太好了 / 恭喜 | `happy`：外眼角上挑的眯眼 | `emotion=happy open_eyes=0` | `happy.jpg` |
| 「我今天有点难过」 | 辛苦 / 心疼 / 听起来… | `loving`：内倾柔和月牙 | `emotion=loving open_eyes=0` | `loving.jpg` |
| 「我的同事今天离职了」 | 遗憾 / 抱歉 | `sad`：外眼角下垂的细月牙 | `emotion=sad open_eyes=0` | `sad.jpg` |
| 「没想到今天下雪了」 | 没想到 / 真的吗 | `surprised`：两个圆睁白圆 | `emotion=surprised open_eyes=1` | `surprised.jpg` |
| 「有什么建议吗」 | 反问（回复带「？」） | `thinking`：圆睁且上移偏右 | `emotion=thinking open_eyes=1` | `thinking.jpg` |

注意 caring 优先级高于 sad：如果助手回复里带「听起来 / 不容易 / 辛苦 / 担心」等词，会先判成 `loving`，这属于服务端口径，不算固件缺陷；想拿 `sad` 就换一句能引出「遗憾 / 抱歉」且不带 caring 词的话。

每轮说完后应自动回到待机闭眼（`Application` 在 idle 下发 `neutral`，串口会再出现 `emotion=neutral`）。
若屏幕没变：先看串口有没有对应的 `emotion=` 行。没有 = 服务端没发（查 Agent/Edge `assistant_expression` → `screen.expression`）；有但脸不对 = 固件映射问题，回来改 `memoria_face.cc` 的别名表。

**5. 不回归项（同一轮里顺手确认）**

- 字幕/状态文字（「连接中」「聆听中」）仍是白字黑底、可读；
- 长按 BOOT 进配网：二维码是白底黑码、盖在脸之上，关闭后回到脸；
- 亮度调节、低电量弹窗、触摸/BOOT 打断仍正常。

**6. 眨眼**：`surprised`/`thinking` 时每 4–7 秒一次约 150 ms 的闭合；`neutral` 等闭眼脸不眨。会话延迟不应变差（只在表情变化和眨眼帧重绘）。

**7. 证据归档与回填**

- 证据目录：`outputs/acceptance/run-20260909-0943-face/`（已有 `serial.log` / `serial-follow.log`；还缺 `idle.jpg` + 五张表情照片）。
- 回填本节 yaml：`hardware_verified: false → true`，并补 `hardware_verified_at`、`evidence_dir`、`verified_by`。
- **只有**待机脸 + 五张表情 + 不回归项都亲眼确认后才能改；编译通过、刷机成功、启动成功都不算。
- 若只验到部分，把实际通过的项写进本节，`hardware_verified` 保持 false。

**回滚**

1. 刷回刚备份的 app（不动身份区、NVS、分区表）：

```bash
python -m esptool --chip esp32s3 -p PORT -b 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 32MB --flash-freq 80m \
  0x20000 artifacts/backups/pre-face-20260909-0943/app-before.bin
```

2. 或改源码回滚：删除 `memoria_face*.cc|h` 四个 overlay 文件，把 `memoria_esp_vocat.cc` 的 `new MemoriaFaceDisplay(...)` 改回 `new SpiLcdDisplay(...)`，`./scripts/build.sh --clean` 重刷。
3. 注意：表情固件首次启动会把 `display/theme` 写成 `dark` 并留在 NVS，回滚到旧固件后界面仍是深色主题；这是观感差异，不是故障，需要浅色时用 `self.screen.set_theme` 切回。

**勿做**

- 不要把 `hardware_verified` 或 `direct_real_device_verified` 从构建/刷机结果推断为 true。
- 不要为了让门禁变绿去放宽 hello 能力断言（`aec_reference_verified=false` 等）；本节只更新过 4 条早已过时的 simplex 断言（见下）。
- 不要改 `screen.expression` 语义，也不要让客户端从字幕猜表情。
- 不要在刷机时写入 `0x10000..0x1ffff` 身份区。

**附：本轮顺带修的门禁漂移**：`check-overlay.sh` 里 4 条 hello 断言还停在半双工口径（`simultaneous_capture_playback=false`、`aec_mode=none`、`aec_reference=none`、`barge_in_level=0`），与 2026-09-08 16:06 的 `Enable VoCat interrupt_assist and device screen expressions` 提交（`0674dee`）之后的固件不符，导致门禁恒红。已按 `HANDOFF` 记录的现状改成 `true` / `fd_low_cost` / `software_post_gain_pre_i2s` / `1`；`aec_reference_verified=false`、`local_stop_keyword=false`、`local_duck=false`、`playback_watermark=exact` 等安全断言未放宽。

## 设备唤醒欢迎语（2026-09-09）

```yaml
change: ignore_wake_tail_connect_vad_until_greeting_first_frame
code: complete
wired: agent_bridge_overlay_cutover
enabled: production_agent_bridge_true
verified: overlay_import_and_container_health
direct_real_device_verified: false
```

真机 10:03 唤醒后进聆听，立刻出现 `Device VAD start sample=0 rms=0` 且从未 speaking。空 VAD 在欢迎语 `require_idle_input` 之前把 session 打成 `USER_SPEAKING`，allowlisted 欢迎语被掐掉。上午 11:27 CST 切流 `20260909-1124`：空 RMS 连接期 VAD 丢弃，欢迎语按时段/周末/缺席/风格从封闭集合选取。

12:27 CST 真机再唤醒（epoch 1891，session `9c8bf4c3-9b94-47c4-91e8-98a8c94fb2f1`，设备 `dev_atk_a4cb8fd6095c`）仍静音、屏幕停在「聆听中」。欢迎语已 admit 成 `thinking_silent`，但唤醒尾音把 sample=0 VAD 带着 leftover RMS 778 打开，`USER_SPEAKING` 抢走 floor，TTS 被 `output_intent_not_selected` 跳过。旧过滤器只认 `rms=0` / 缺失；`_speak_device_wake_ack` 又在 admit 后、首帧前于 `finally` 清掉 pending。

代码层已切流 Agent/Bridge（2026-09-09 12:59 CST）：

1. 欢迎语未出首帧前，`device_wake_ack_pending` 且 `sample=0` 的连接期 VAD 无论 RMS 都丢弃；`sample>0` 的真实开口仍跳过欢迎语。
2. pending 保持到 first_frame / skip / fail / 提前返回；`device_wake_ack_fence` 在 `_speak_allowlisted_bridge_phrase` 返回 True 之后从 promoted fence 取值。
3. 发布标签 `20260909-1256-wake-tail-connect-vad-agent-component`，源 `68ddba106d7cae7c00556a6393b94e72a5d0f553`。容器 **healthy**、restart=0，overlay 含 `pending_connect` / first-frame 清 latch。真机听感未做，`direct_real_device_verified` 保持 false。不要放宽 `reject_non_owner_voice`。

**勿做**：把单元测试或容器 healthy 当成板端欢迎语已恢复；给动态欢迎语走非 allowlist 生成。

## 小程序体验与跨端设备同步（2026-09-06）

- 交付：`RESEARCH.md` 的 R-20260906-01 与原型 `apps/miniprogram/design-preview/memoria-mobile-redesign.html` 已落到正式四 Tab 控制面（首页 / 设备 / 回顾 / 我的），另增非 Tab「角色与声音」。小程序仍只做控制面，不承担实时麦克风、TTS、WSS 或 LiveKit。
- `code`：前端 `7e5137a` 已 commit/push。在线状态恢复为依据服务端权威就绪记录（`ready_for_conversation`），解决开机待命时因无长音频流被误判为离线的问题。首页去掉了未登录时的「先了解怎么用」按钮和「家中的设备，手边的回顾」文案；默认设备名改为「我的设备」。角色与声音保持纯文字列表。
- `wired`：首页与设备页均调用账号同步并拉取诊断与运行状态；重试入口先恢复登录，失败进入 guest、废弃旧请求。换账号/设备与迟到响应有 fence，绑定关系不升级为主人声纹或私密能力。
- `enabled`：Control API 仍为 2026-09-06 15:11 CST 切流。官方 DevTools CLI 已上传开发版 **0.8.84**（23:58 CST，903,051 bytes）。源码 `7e5137a`，tests/design-preview 由 packOptions 排除。未提交审核或正式发布，也未设为体验版。
- `verified`：小程序单测 211/211；本机编译上传成功。手机/电脑微信与开发工具尚未对 0.8.84 做真实验收，不得写成三端 PASS。0.8.77 开发工具找回 095c 的证据不自动继承到本版 UI。
- **边界**：运行配置 GET 404、Runtime Profile 失败导致敏感入口关闭、公网 `/health/ready` smoke 过期 503，均未在本轮处理。未改硬件、声纹、绑定或生产容器。
- 下一验收：微信切开发版 **0.8.84**，核对设备开机后显示「设备在线」、首页文案已去除「家中的设备」与「先了解怎么用」、同账号设备 095c、偏好保存与家庭邀请账号编号；需要时再在公众平台设体验版或提交审核。

## 当前生产

当前 Agent/Bridge 镜像为 `memoria-agent:20260909-1256-wake-tail-connect-vad-agent-component`（源 `68ddba106d7cae7c00556a6393b94e72a5d0f553`）。Control API overlay 发布源码为 `4a3b91bfa156f946f67b92e0b0ced17fab108a67`（标签 `20260906-1458-account-device-discovery-control-api`）。Agent/Bridge/Control 的 env `MEMORIA_RELEASE_TAG` 均为 `20260901-0945-wake-word-whitelist`。**2026-09-04 文本模型切流（env）**：`LLM_PROVIDER=qwen`，主对话 `QWEN_FAST_MODEL=qwen3.7-flash`（关思考），分类器 `qwen-flash`（打断/联网/告别/危机/摘要/记忆抽取）。联网查询隔离源仍用 `QWEN_DEEP_MODEL=qwen-plus`。不再用即将下线的 `deepseek-v4-flash` 当对话模型，也不迁到更贵的 `deepseek-v4-flash-0731`。

- Agent 与 Voice Core Media Bridge（容器 `memoria-agent-1` / `memoria-voice-core-media-bridge-1`）：`memoria-agent:20260909-1256-wake-tail-connect-vad-agent-component`，revision `68ddba106d7cae7c00556a6393b94e72a5d0f553`，image `sha256:74ed3cc7bd59f5bed792c3f6a97d14140cc1251e4fd4d838f9990bb0e5fed2ac`。两者 **healthy**、restart=0。容器内 overlay 含 `pending_connect`、欢迎语 fence 在 admit 后绑定、first_frame 才清 pending。收据 `/opt/memoria/component-releases/20260909-1256-wake-tail-connect-vad-agent-component/`。回滚 `rollback-20260909-1256-wake-tail-connect-vad-agent-component-pre-agent/-pre-bridge`（镜像 `20260909-1124-device-wake-greeting-agent-component` / `sha256:3dde10934e7e1c9ab0f436b387d2200b08c3d4188aafb33cffa49cdd092032ec`）。**subject：「主人」已 adult/verified；声纹 profile `1b5b577b` 已 active。** 现网设备 `dev_atk_a4cb8fd6095c` 已由 Control 权威路径改为 `audio_mode=interrupt_assist`（settings_version 11→12，`vocat_interrupt_assist_enable`）。Control 镜像仍是 `20260906-1458`，新设备默认值未切。`direct_real_device_verified` 保持 false。
- Media Edge：`memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`，容器 healthy、restart=0。override `/tmp/media-runtime.override.yml` 钉该镜像。回滚镜像 `memoria-media-edge:20260901-0945-wake-word-whitelist`；override 备份 `/tmp/media-runtime.override.yml.pre-20260908-1600-vocat-interrupt-assist`。Control SQLite 挂载 `/data <- /var/lib/memoria`，权威库 `/data/memoria.sqlite3`。
- SenseVoice 兜底 sidecar：`memoria-sensevoice-asr:20260901-pin-language`（sherpa-onnx 1.13.6 + SenseVoice-small int8，`/opt/memoria/sidecars/sensevoice-asr/`，docker 网络 `memoria_default`，--cpus 2 --memory 1g，2026-09-01 14:58 CST 切换）。Agent 侧 `SENSEVOICE_URL=http://memoria-sensevoice-asr:8001/transcribe` 已配置；FunASR 空转写且 RMS≥100 时自动兜底（fail-open，2.5s 超时）。**本轮修掉语种漂移**：sidecar 此前收下 `language` 只写日志、从不传给 recognizer，`from_sense_voice(language='')` 走内置 LID，短促低电平普通话被判成韩语并原样输出谚文；现按语言缓存 recognizer（`_SUPPORTED_LANGUAGES` 闭集，默认 `SENSEVOICE_DEFAULT_LANGUAGE=zh` 并在启动预热），未知语言 415 fail closed。回滚：镜像 `memoria-sensevoice-asr:v1` + 脚本 `/opt/memoria/sidecars/sensevoice-asr/run_sensevoice_asr.py.rollback-20260901-prelang`。Agent 侧回滚点 `rollback-20260829-0859-sensevoice-rescue-agent-component-pre-agent/-pre-bridge` 不变（本次未动 Agent 镜像）。
- **sidecar 构建资产只存在于服务器**：`/opt/memoria/sidecars/sensevoice-asr/Dockerfile` 在仓库里没有副本，基础层 `python:3.11-slim` 与 pip 依赖都未钉版本，重建不可复现。本次重建后已现场校验 sherpa-onnx 仍为 1.13.6、Python 3.11.16，与旧 `v1` 一致；下次改动前应先把 Dockerfile 收进仓库并钉版本。服务器上的脚本副本与仓库 HEAD 曾有 import 排序差异（无功能差异），现已同源。
- Control API：`memoria-control-api:20260906-1458-account-device-discovery-control-api`，overlay revision `4a3b91bfa156f946f67b92e0b0ced17fab108a67`，image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`。2026-09-06 15:11 CST 切流，延迟复核 healthy、restart=0；账号设备发现已返回真实绑定，生产 PG/RLS 只读 canary 通过。13 个非目标容器、有效环境、绑定/角色和声纹未改；自定义声音与合格登记 active 能力保留。紧邻回滚 `memoria-control-api:rollback-20260906-1458-account-device-discovery-control-api-pre-control`（原 `20260906-0048-companion-custom-voice-control-api`，image `sha256:6ef47dee3cb779b55a766ea6f392ae8f556315bd5a4322f48caf5fe6624c5b41`）。收据 `/opt/memoria/component-releases/20260906-1458-account-device-discovery-control-api/` 下 `cutover.json`、`readonly-canary.json`、`candidate-provider-smoke.json`。栈 env 仍为 `20260901-0945-wake-word-whitelist`；当前 ready 503 与运行配置 404 边界见上节，不据容器 healthy 宣称全链路通过。
- 小程序开发版 **0.8.84**（2026-09-06 23:58 CST，官方 DevTools CLI；源码 `7e5137a`，903,051 bytes）：恢复依据服务端就绪状态显示在线、解决开机待命误报离线、去掉角色立绘与文案清理、账号设备同步。未正式发布、未设体验版；手机/电脑微信待验。
- Agent/Bridge、Control API、Media Edge 目标容器 healthy；2026-09-01 切流后 `GET /v1/devices/wake-word-catalog` smoke：`mei_mo_li_ya` 可见。
- 2026-08-30 10:20 CST 切流后容器内 provider smoke：Qwen Realtime Search、Doubao、FunASR 6/6、DeepSeek、Interrupt Semantic PASS。
- 2026-08-24 19:16 CST，Qwen Realtime Search、Doubao、FunASR、DeepSeek、Interrupt Semantic 与媒体 fence 生产 smoke 通过。
- 2026-08-28 15:40 CST 切流后复测：Qwen Realtime Search 与 Doubao 稳定 PASS；**FunASR 间歇失败**，4 次中 1 次 PASS，报 `FunASR returned no interim transcript`。定性依据：用切流前镜像 `rollback-…-pre-agent` 做 A/B 对比同样失败；且该 smoke 直接调用 `FunASRSession`，不经过 device VAD 门控或本次改动的任何代码路径。判定为供应商侧抖动，**与本次发布无关**，因此不构成回滚理由（回滚同样失败且会丢失修复）。FunASR 恢复前真实识别率会受影响，需另行跟进供应商。
- **发布链阻断（已解除）**：分支整理曾使生产镜像 revision `c3fb7fd` 不在 `main` 祖先链上，快路径被 `merge-base --is-ancestor` 校验拦截。2026-08-28 19:39 CST 的发布以 `c3fb7fd` 为基点拉 `release/agent-normal-distance-gain` 分支、cherry-pick 变更后走脚本全门禁发布，再把该分支合回 `main`：未手工 retag 任何镜像，且此后生产 revision `af3fab8` 已是 `main` 的祖先，后续 `services/agent` 快路径可直接以当前生产镜像为基座。
- 普通 Agent component 目录已收敛为当前 `1912` 与紧邻回滚 `1843`；当前回滚标签均解析到 `sha256:a76000fab76397efbc812596819e9bb301e6cf4469f2e0c8c7614f873e7b3789`。
- Agent/Bridge 内 DTLN 完成 ONNX checksum/contract 初始化和 7,680-byte PCM 实推，输出非零；后级补偿默认 `8.0x`（18 dB，`MEMORIA_DTLN_MAKEUP_GAIN` 可调，1.0–32.0 钳制），PCM 转换保持饱和防削波。2026-08-28 真机复测定性：失败轮均为 FunASR 空转写（`text_len=0`）而非上行门控问题；同容器直连 FunASR 干净 PCM 6/6 PASS（15:40 判定的供应商抖动已恢复）；唯一成功轮为更响更长的重复，指向正常距离/音量下上行电平不足。增益已提高，待真机复测并用 `/tmp/media-pcm-tap` WAV 校准。

本轮真实板卡复现中，天气回答的 generation 1 与 successor generation 2 均取得 `Actual Heard + playback.ended`，此前“说着说着停了”的分段续播问题已在真机关闭；用户随后问“今天星期几”时，多段降噪后 PCM 只有约 `RMS 8–296`，FunASR 没有产生 partial/final，因此没有形成 turn 2；该现象已于 2026-08-28 定位为播放后 VAD 上行门控死锁，根因、修复与未验证边界见「播放后 VAD 上行门控死锁候选」。BOOT 终止事件由用户主动按键触发，属于正常结束，不计为故障。

正常距离识别修复状态：`code=complete`，DTLN 后增加受限 6 dB 补偿，FunASR 明确失败的旧任务会在下一帧前重建并重放有界 PCM，内部边界竞态仍 fail closed；`wired=Agent+Bridge current image`；`enabled=production true`；`verified=2026-08-24 local Agent regression + production provider/media smoke + exact 23-second FunASR idle-timeout recovery + 2026-08-31 operator two-turn Actual Heard（星期几 + 天气，待正式 receipt）`。不得据此单独把全局 `direct_real_device_verified` 改为 true。

Agent-only 发布现在把历史 Compose override 收口为“生产主 Compose + 已验证在线镜像快照 + 当前 override”。旧 component override 被普通制品清理后不再阻塞后续发布；收口前会验证 Agent/Bridge 共享同一 runnable image，且所有仍存在的 component override 只能包含这两个服务的 image 字段。

服务器普通制品只保留当前运行版本和一个已确认可运行的紧邻回滚。执行任何回滚前必须现场读取容器 image ID、Compose override 和证据目录，不从本文猜测标签；数据库、WAL、MinIO、安全与合规备份不属于该两版本清理策略。

### 微信账号主体跨库同步（2026-09-05）

根因：微信手机号登录只将 Control SQLite 的账号登记为 `adult/adult/verified`，Identity PostgreSQL 中的同一人仍是 `unknown/unknown/unverified`。声纹 profile 已 active 不能补齐这份主体资料；之前笼统的“主人已 verified”只覆盖了 Control 一侧。

`code=complete`：登录与设备绑定共用 `ensure_account_person`；先提交 Identity 登记审计，再提交 SQLite profile/session 事务，两侧成功才发登录凭证。只允许服务端持久化微信手机号登记将 active 的纯 unknown 主体补齐；minor、disputed、disabled 拒绝。沿用既有手机号登记政策，不把微信手机号授权冒称为真实年龄核验，也不修改独立年龄验证人规则。

`wired=production_control_login_and_binding`；`enabled=true`：生产已安装限定 `memoria_identity_registration` 执行的 SECURITY DEFINER 函数；普通 API 角色与 PUBLIC 无执行权限，owner/search_path/RLS 门禁通过。

`verified=2026-09-05_16:27_CST`：182 项关联测试、11 项真实 PostgreSQL 测试、Ruff/mypy 通过。目标账号经已发布业务路径回填后两库一致；审计和 outbox 各新增 1 条，第二次运行无更新、无重复记录。SQLite revision/session、声纹模板与样本、其他主体均未变化。现有设备绑定的运行时权威查询已读到 adult；未创建测试会话或伪造说话人。公网 live/ready、12/12 具名核心项、Qwen/FunASR/Doubao/InterruptSemantic/LiveKit 真实冒烟及切流后延迟日志复核通过，其余 13 个运行容器未变。

制品保留已收口：删除 22 个过期 Control tag 和 4 个旧组件目录，保留当前与已实启验证的紧邻回滚两个独立镜像版本；数据库备份未动。2026-09-05 16:33 CST 最终复核 Control healthy、restart=0，公网 ready、12/12 核心项 ready，回滚镜像仍可读取。

边界：微信手机端实际重新登录、主人当轮声纹匹配和车票 Actual Heard 仍待用户真机复测；旧 runtime profile 不自动恢复，应重新唤醒建会话。新 outbox 是已持久化 `pending`，未宣称消费者已处理。两库没有分布式事务：Identity 提交后 SQLite 失败会暂时单侧同步，登录仍拒绝发凭证，重试幂等补齐。display_name 不在本次同步范围。

发布/回滚收据：`/opt/memoria/component-releases/20260905-1610-wechat-identity-sync-control-api/`（`manifest.json`、`rollback.json`、`schema.json`、`cutover.json`、`backfill.json`、`runtime-authority.json`、`delayed-review.json`、`cleanup.json`）。数据库备份：`/var/backups/memoria/20260905-1610-wechat-identity-sync-control-api/`。仅回滚 Control 镜像时保留新增受限函数及有效账号资料；不要用全库恢复代替组件回滚。

### 陪伴自定义声音与唤醒音色（2026-09-06）

根因：陪伴会话按目录 `companion_id` 冻结设计音色；自定义人格写在 profile `bio` 里，会话仍沿用上次目录音色（常见为桃喜）。唤醒应答在绑定冻结音色之前合成，因此无论换哪个人格，唤醒都是默认星澜。设备 resume 复用同一 `session_id`，冻结字段此前也不刷新。

`code=complete`：Control 在创建/resume 陪伴会话时按当前 bio 与 active clone 冻结投递；自定义人格且成人 `voice_clone` 能力具备时冻结个人音色（Doubao ICL 或 CosyVoice v3.5），否则冻结目录设计音色。Agent 在唤醒应答前对齐 TTS；不新增 `companion_id`，不改 BindingManifest，不把 `voice_clone_use` 加入会话能力。未成年人或未授权走设计音色。

`wired=production_control_and_agent`；`enabled=true`：2026-09-06 00:44 CST Control overlay 切流，00:46 CST Agent/Bridge 切流。2026-09-06 00:59 CST Agent 热修 `e67c2a68aa60d95406911120cfae5b2be4040757`（`20260906-0105-companion-clone-weather-bind-agent-component`）。栈 env 仍为 `20260901-0945-wake-word-whitelist`。

`verified=2026-09-06_00:52_CST`：真机确认自定义克隆已用于唤醒。同轮天气 `session=18ea94e4-48bd-43ef-b6f1-38eea08627c9`：`generation voice binding rejected reason=bind_rejected`（CosyVoice personal，无 `voice_clone_use`），随后垫话 `playback_terminal_incomplete` 进待命。热修后门禁 PASS、Agent/Bridge healthy restart=0。**天气正文尚未真机复测**。

发布/回滚收据：Control `/opt/memoria/component-releases/20260906-0048-companion-custom-voice-control-api/`；Agent `/opt/memoria/component-releases/20260906-0105-companion-clone-weather-bind-agent-component/`。

## 设备首次启用与安全配网

```yaml
candidate: secure_device_onboarding_activation
as_of_date: 2026-08-27
code: complete
wired: miniprogram_protocomm_security1_to_control_api_claim_binding_to_esp32_activation_ack
enabled: production_control_api_and_flashed_board_true
verified: 2026-08-27_server_ack_and_miniprogram_device_ready
device_id: dev_atk_a4cb8fd6095c
firmware_version: 2.4.2
miniprogram_experience_version: 0.8.75
```

本轮已打通并验证以下顺序：二维码 introspect → BLE Protocomm Security 1（X25519、PoP、AES-256-CTR）→ 设备 online-proof → claim/binding → Activation Manifest → 设备 ACK → `ready_for_conversation`。小程序不采集声纹或实时语音；Wi-Fi 密码只在已认证的 BLE 会话中写入设备，不经过 Control API 日志或小程序普通请求。

设备第一次在绑定完成前收到激活 `409` 时，会继续显示附近配网入口并在后台每 5 秒重试 Activation Manifest；绑定完成后设备拉取清单、返回 ACK，并停止配网二维码入口。相同二维码从新页面重试时复用原 onboarding session，避免误报“设备正在被其他账号设置”。

真实证据：设备 `dev_atk_a4cb8fd6095c`（BLE `MEM-095C`）的最新激活记录为 `activation_version=3`、`status=ready_for_conversation`，`acknowledged_at=2026-08-27 06:34:45.034649+00`（UTC，即 14:34:45 CST）。Nginx 记录设备于 14:34:44 拉取 manifest 200，14:34:45 提交 activation-ack 200；小程序随后显示“在线，可直接对话”和“激活状态：可开始对话”。

这证明了服务端和控制面激活闭环，不等于真实语音对话、AEC、双讲、连续轮次或完整屏幕物理显示验收。当前已通过串口确认固件 2.4.2、Wi-Fi、Manifest v3 和稳定运行；板屏是否已由二维码切换到正常界面尚未单独留存最新照片，不能用小程序页面替代该物理证据。

## 当前板卡与固件

- **硬件目标平台**：乐鑫 ESP-VoCat N32R16（ESP32-S3-WROOM-1-N32R16，32MB Flash / 16MB Octal PSRAM）。旧板卡（`atk-dnesp32s3-v1` 16MB）固件与驱动已彻底移除清理。
- **固件标识与版本**：board `memoria-esp-vocat`，app version 2.4.2。
- **音频系统**：ES8311（音频输出/DAC/PA，GPIO4/15 动态 PCB 适配）+ ES7210（双麦克风阵列 ADC，输入增益 **36.0 dB**）。采用 `BoxAudioCodec`，已通过 patch `0017` 增加 `i2s_channel_register_event_callback` 监听 `on_sent` TX DMA 完成中断，实现精准的 `HasExactOutputCompletion` 和 `OutputCompletionCounter` 硬件播放水线回执。
- **外设与交互**：
  - 屏幕：1.85 寸 QSPI 圆形 LCD（ST77916，360x360 分辨率，40MHz SPI 驱动，带自动背光调节）。
  - 触摸：CST816S I2C 触控屏（支持单击打断/切换对话/退出聆听、中断驱动）+ 触摸电容滑条/按键（PCB v1.0/v1.2 自动兼容）。
  - 传感器与电源：BMI270 六轴运动传感器（摇晃动作检测，score 阈值 4000，冷却 2.5s）、BQ27220 电池电量计量与充放电检测、芯片片内温度传感器。
  - 按键：BOOT 按键（单击切换状态/打断，长按进入配网 / Protocomm BLE 凭证下发）。
- **板端 WebRTC 降噪与唤醒**：overlay patch `0022` 打开 ESP-SR AFE WebRTC NS（`CONFIG_SR_NSN_WEBRTC=y`）；默认唤醒词「茉莉」（`mo li`），支持白名单与拼音自定义；Speaking 期间忽略迟到 wake event，BOOT / 触摸仍是本地硬停。
- **分区表布局（32MB Flash）**：`partitions/v2/32m.csv`。
  - `memoria_identity` 位于 `0x10000`（64KB，受写保护，仅限 provision_identity.py 刷写）。
  - 双 4MB OTA app 分区（`ota_0` 0x20000 0x3f0000, `ota_1` 0x3f0000）。
  - 8MB SPIFFS assets 分区（`assets` 0x800000 8MB，完全落在 24-bit MMU 物理映射区内且满足 PSRAM 16MB 下的 13MB mmap 上限）。
- **最新固件构建产物与指纹**（2026-09-08 interrupt_assist 构建，默认开机音量 30%，ES7210 36.0 dB；**已 app-only 刷到 `/dev/cu.usbmodem101`，身份区未写**）：
  - app SHA-256：`8c08ba23c8de6e70ded13deb7b87ce5c9829fd7ef1bd45734a7a6ea283fd7590`（`firmware/esp32/artifacts/memoria-esp-vocat-app.bin`）
  - merged SHA-256：`86146ebbd9fbfc44886f8a4a848630faca29853ee8ede1282e39b8f9f0e857c8`（`firmware/esp32/artifacts/memoria-esp-vocat-merged.bin`）
  - bootloader SHA-256：`61a2cba146b738a5e0b4fe1bd68e49eea1b32c3bd2009db185a3607096d75d0f`（`firmware/esp32/artifacts/memoria-esp-vocat-bootloader.bin`）
  - partition-table SHA-256：`da35229c3fe72536129e09663615c1ee9851a74f43493a154f5d40d359b1dc8b`（`firmware/esp32/artifacts/memoria-esp-vocat-partition-table.bin`）
  - overlay hash：`31292b0f3bced2a09f87392b3f34fad8055c45dbedc4f9e58d399bee734c3cdb`
- 刷写命令：`bash firmware/esp32/scripts/flash.sh --port /dev/cu.usbmodemXXXX`（或直接使用 auto 探测）。首次全量烧录建议使用 `merged` 固件：`esptool.py write_flash 0x0 firmware/esp32/artifacts/memoria-esp-vocat-merged.bin`。

22. **epoch 1417（2026-09-08 12:35 CST）ES7210 36.0 dB 远场 30~60cm 双轮验证 PASS**：session `a4639927-1ab6-4b66-af3d-df579c369047`。用户在 30~60cm 距离唤醒「茉莉」并提问天气。TAP 录音分析（`media-uplink-a4639927-epoch1417.wav`）：时长 19.22s，峰值满量程 32768，平均能量 RMS 达到 **3519.3**（较 30.0 dB 时的 189.4 提升约 18 倍），FunASR 精准识别 `sentence_id=2 text_len=10`（南京天气查询），Qwen + Open-Meteo 成功返回南京天气，CosyVoice TTS 流式下发，设备完整出声且收到 `actual_heard=True` 与 `playback_ended=True`，远场唤醒与弱音识别彻底闭环。

epoch 1379 已有串口 VAD、bridge Actual Heard / `playback.ended`、Edge 显式结束和操作员听感；仍不等于全局 `direct_real_device_verified`，也不等于嘈杂环境定量抗噪。

## 茉莉唤醒与自动待命候选

```yaml
candidate: jasmine_wake_and_owner_standby
as_of_date: 2026-08-25
code: complete
wired: esp_sr_kws_to_python_owner_authority_to_typed_closed_to_edge_session_close
enabled: true
deployed: production_agent_bridge_edge_and_firmware
verified: 2026-08-25_real_device_wake_and_owner_silence_timeout_to_typed_closed_session_close
direct_real_device_verified: false
wake_word: 茉莉
wake_word_whitelist: [mo_li, mei_mo_li_ya]
wake_word_custom: multinet_pinyin_v1
owner_silence_timeout_s: 10
```

固件只在本地 MultiNet/KWS 用当前活跃命令词唤醒（默认 `mo li` / 「茉莉」），仍不上传唤醒词音频。小程序设备页可切换白名单词或保存自定义词（display + pinyin）；设置经 Control API → Media Edge `session.accepted` 下发，固件需含 patch `0021` 且设备重连后生效。自定义 v1 不走云端 WakeNet 训练；两音节词误唤醒风险仍高，阶段 4 计数待做。设备会话中的“再见”“知道了”“退下吧”等精确结束语只有通过目标说话人权威判定后才关闭；带后续内容的句子不会误触发。无主人语音计时只由 Python Voice Core 的权威会话状态管理：助手输出和传输断开期间暂停，回到聆听时开启 10 秒窗口，裸 VAD/环境声不能重置主人计时。2026-09-01 真机曾暴露一个例外：助手自己的「没听清」提示播完后走同一条 `assistant_state=listening` 复位路径，把窗口重置成完整 10 秒，等于助手语音给自己续命；**已修**——该接缝改为续用剩余预算，满窗刷新只在已验证主人轮次发生。主人拿到的是 10 秒净聆听预算，助手说话期间暂停不计。关闭通过 typed `CONVERSATION_STATE_CLOSED` 进入 Go Media Edge，再下发设备 `session.close` 回到 Idle，不新增第二套聆听状态机。

当前候选已发布到生产 Agent/Bridge/Edge 并写入当前板卡。2026-08-30 10:19 切流后 Direct 唤醒 TTS 曾用 `turn_id=0`，固件拒包、无 Actual Heard；10:33 已改为先打开 `turn_id>=1` 再播允许名单短句（「我在。」「哎，我来了。」「哎呀，好困呀。」）。该听感尚未真机验收，不能更新 `direct_real_device_verified`。2026-08-25 真机已验证“茉莉”唤醒后静默约 10 秒，Python 产生 `owner_silence_timeout` typed CLOSED，Edge 成功排队 `session.close`，设备回到待命；Edge 也已增加旧 FLOOR fence 丢弃和重复 CLOSED 幂等保护。明确结束语测试时，ASR 路由进入结束语分支，但正式主人权限返回 `subject_capability_forbidden`，记录为 `conversation_end_owner_unverified` 并由随后超时关闭，因此 `conversation_end_explicit` 仍待主人声纹/subject profile 权限就绪后复测。两音节“茉莉”相较原四音节唤醒词有更高误唤醒风险，安静、电视人声和家庭噪声三种环境的阈值验收仍未完成，不能更新 `direct_real_device_verified`。

## 播放期间 ASR 任务空闲超时修复

```yaml
candidate: playback_asr_task_pause
as_of_date: 2026-09-01
code: complete
wired: playback_ledger_start_to_provider_pause_asr_to_funasr_rotate_task
enabled: true
deployed: production_agent_bridge
production_release_tag: 20260901-1740-funasr-rescue-playback-asr-pause-agent-component
production_release_commit: 2290da1a80d974b91f78b59a8d4ac5c6cb67a976
deployed_at_utc: 2026-09-01T10:32:01Z
verified: unit_regression_mutation_checked_and_production_cutover_healthy
regression_tests: test_commit_pauses_provider_asr_when_playback_starts,test_existing_provider_adapter_rotates_asr_task_when_playback_starts
direct_real_device_verified: false
```

**缺陷 3 根因**：半双工模式下播放开始时停止音频采集，但 ASR 任务保持打开。FunASR 提供商期望持续音频输入（FUNASR_HEARTBEAT 参数），23 秒无音频后超时失败。生产日志显示 12 次 `EmptyAudio` 错误和 2 次超时，均发生在播放期间。

**修复方案**：在播放开始时主动关闭当前 ASR 任务。方法是在 `MediaVoiceProvider` 协议中新增 `pause_asr_for_playback()` 方法，由 `ExistingVoiceProviderAdapter` 实现并调用 `FunASRSession.rotate_task()`。在所有播放启动点（`media_session_commit.py`、`media_session_output_dispatch.py`、`media_session_connection.py` 两处、`media_session_input.py`）调用该方法。播放结束后音频采集恢复时会自动启动新任务。

**回归测试**：会话层 `test_commit_pauses_provider_asr_when_playback_starts` 走真实 `commit_user_turn` 路径，断言播放启动后 pause 恰好触发一次；适配器层 `test_existing_provider_adapter_rotates_asr_task_when_playback_starts` 断言 `rotate_task(require_consumed=False)` 被调用、未开始的任务不被空转、采集未恢复时重复播放启动保持幂等。已用变异验证有效性：注掉 `media_session_commit.py` 的 pause 调用后会话层测试失败（`[] == [1]`）。注意 `FakeMediaProvider` 继承 `MediaVoiceProvider` Protocol，Protocol 的 `...` 方法体会成为返回 `None` 的真实方法——新增协议方法时若不在测试替身里显式实现，调用点会在测试中静默 no-op。

**未验证边界**：真机复测、生产环境播放期间 ASR 超时消失、播放结束后新任务正常启动。修复已随 Agent overlay `20260901-1740-funasr-rescue-playback-asr-pause-agent-component`（2026-09-01 18:32 CST 切流）发布到生产 Agent/Bridge；容器 healthy、restart=0，但播放期 FunASR 行为仍需真机 receipt，不得据此升级 `direct_real_device_verified`。

## 播放后 VAD 上行门控死锁候选

```yaml
candidate: device_post_playback_vad_uplink
as_of_date: 2026-08-28
code: complete
wired: device_vad_projection_plus_session_entrypoint_deferred_commit
enabled: true
deployed: production_agent_bridge
production_release_tag: 20260828-1537-device-vad-uplink-deadlock-agent-component
production_release_commit: c3fb7fd181794204b97207cfef0015aa0106bde4
deployed_at_utc: 2026-08-28T07:38:58Z
rollback_point: memoria-agent:rollback-20260828-1537-device-vad-uplink-deadlock-agent-component-pre-agent
verified: unit_1732_e2e_offline_mypy_strict_ruff_cutover_healthy_restart0_bridge_grpc_in_container_code_attested
direct_real_device_verified: false
```

根因：板端 `MemoriaProtocol::SendVadState` 是严格边沿触发（`speaking == vad_active_` 时直接 return），**不会重发 `vad.start`**。此前 `DeviceVadProjector` 在播放 holdoff 窗口内对 VAD 事件直接 `return True` 丢弃，并在 `begin_playback_holdoff` 里把 `_active` 强制置 False。只要用户的 `vad.start` 落在该窗口内，FunASR 上行门控 `pcm_enabled` 就再也打不开：PCM 全部进入有界 preroll 后被丢弃，FunASR 无 partial/final，随后的 `vad.end` 又因 `_active=False` 被一并吞掉，于是既不形成 turn，也不会触发“没听清”兜底，用户听到完全沉默。唤醒后的欢迎语“想聊什么就直接说吧”恰好邀请用户在该窗口内开口。

修复：holdoff 只**延后** turn commit，不再丢弃 VAD 状态同步事件；`vad.start` 一律打开上行；窗口内到达的 `vad.end` 改为延迟提交，并把 ASR 等待锚点从提交时刻改回 `vad.start` 时刻、超时按已流逝时间补偿，避免延迟后的真实文本被误判为空。回声防护改为判据式：整段语音落在播放尾音窗口内且起于扬声器仍在工作时判为回声丢弃；设备会话是受控半双工、不支持打断，与该契约一致。holdoff 改为只在真正离开 `speaking` 时开启，不再从播放开始计时。

已确认无效链路：`input_policy` / `capture_allowed` 在 Go Media Edge 与 ESP32 固件中均无消费者，只对 H5 生效。`Hold device capture closed until on-device playback can finish` 对设备链路是空操作，不要再沿这条链补防回声逻辑。

未验证：media-v1 主链在欢迎语后立即提问、播放尾音误触发率仍需带 receipt 的板卡证据。LiveKit `DeviceVadProjector` 路径的 holdoff 修复不自动覆盖 media-v1；2026-08-31 已在 `media_session_input` 对播放期 `vad.end` 做等价防护。切流后的 provider smoke 中 FunASR 间歇失败已判定为供应商侧问题（见「当前生产」），真机长段空转写现由中段 SenseVoice 救援与时钟类提前提交缓解，不得据此宣称 FunASR 供应商问题已关闭。

## VAD 未结束保护与终止态输入闸门

```yaml
candidate: media_vad_end_watchdog_terminal_fence
as_of_date: 2026-08-29
code: complete
wired: agent_settings_to_media_voice_core_registry_to_device_vad_lifecycle
enabled: production_agent_and_bridge_true
verified: agent_full_tests_mypy_ruff_edge_go_tests_component_build_cutover_health_restart0
max_user_speech_duration_s: 60
direct_real_device_verified: false
full_duplex_verified: false
```

本轮修复将 owner-silence 计时限定在 `listening` 阶段，接受 `vad.start` 时同步暂停；每个设备话轮只 arm 一次独立的 60 秒最长讲话 watchdog，收到 `vad.end`、重连或关闭时取消，超时以 `max_user_speech_duration_timeout` 发送 typed `CONVERSATION_STATE_CLOSED`。`standby_requested`、transport terminal 和已关闭 Registry context 在音频/VAD/KWS/播放回调入口全部 fail-closed；关闭前安装按 `stream_epoch` 的持久终止 fence，旧 epoch 拒绝重建，而更高 epoch 仍可在旧 transport 清理后重连。

本地 Agent 全套测试、strict mypy、ruff、`git diff --check` 与 Media Edge `go test ./...` 通过；候选镜像构建/import smoke、Agent/Bridge 切流、健康检查、bridge socket、heartbeat 和 restart=0 均有远端收据。2026-08-31 操作员真机「星期几 + 天气」两轮听感 pass；正式 receipt（串口 `vad.start`/`vad.end`、tap WAV、session fence 日志）与 60 s watchdog 触发仍待补，不能更新 `direct_real_device_verified` 或 `full_duplex_verified`。

## 当前开发工单：VoCat interrupt_assist

```yaml
candidate: vocat_interrupt_assist
as_of_date: 2026-09-08
hardware: memoria_esp_vocat_es7210_es8311
audio_mode: interrupt_assist
advertised_duplex_level: none
barge_in: negotiated_interrupt_assist
turn_phase_side_effects: forbidden
direct_real_device_verified: false
full_duplex_verified: false
hardware_aec: present_unverified
code: complete
wired: agent_bridge_edge_cutover_firmware_flashed
enabled: production_agent_bridge_edge_true_device_audio_mode_interrupt_assist
verified: false
retired_work_order: half_duplex_investor_demo
```

ATK ES8388 半双工投资人 Demo 已退役。当前板是 ESP-VoCat（ES7210+ES8311）。hello 如实报 simultaneous capture 与 `aec_mode=fd_low_cost`，`aec_reference_verified=false`。默认协商 `interrupt_assist`：播放期保持采集，Agent barge-in 跟协商 `audio_mode`。`full_duplex_verified` 仍要 T1–T14。对客口径保持 `advertised_duplex_level: none`。

**本轮代码**

1. 固件 hello 报 `simultaneous_capture_playback=true`、`aec_mode=fd_low_cost`、`aec_reference=software_post_gain_pre_i2s`、`barge_in_level=1`；`CONFIG_USE_DEVICE_AEC=y`。播放期不再停录；唤醒词仍不是本地停播权威。
2. Edge 把协商 `audio_mode` 写入 Voice Core hello capabilities。
3. Agent Direct 路径按 `interrupt_assist` / `full_duplex_verified` 开 barge-in，不再「凡 device 都半双工」。LiveKit 设备路径仍半双工。播放期 `pause_asr_for_playback` 只在半双工时关闭 FunASR 任务。
4. Direct Edge 把 `assistant_expression` 转成板子 `screen.expression`。笑/苦/惊讶走 `happy`/`sad`/`surprised`；关切/好奇映射 xiaozhi 的 `loving`/`thinking`。待机闭眼是 idle `neutral`，说话时才切脸。
5. Control 默认设备设置改为 `audio_mode=interrupt_assist`。新设备默认会升档；现网已有设备仍要改设置，旧票据不会自行升档。

**下一步（按顺序）**

1. 重新唤醒「茉莉」：应听到 allowlisted 欢迎短句（时段/周末/缺席/风格变体之一），屏幕离开「连接中」。epoch 1891 的静音聆听已由 `20260909-1256` 覆盖，听感未过不得把欢迎语或 `direct_real_device_verified` 写成 true。
2. 真机测表情：待机可闭眼；助手说「太好了」应变笑（`happy`），抱歉/难过变苦（`sad`/`loving`），「没想到」变惊讶（`surprised`）。说完回到待机闭眼。**详细操作、命令、判定标准与证据回填见「屏幕表情：眼睛白描脸（2026-09-08）」一节的 runbook。**
3. 助手说话时插一句短打断，应形成新 turn 并停旧 generation；BOOT 仍能硬停。
4. 播 TTS 时采近端残差。未过证不得改 `aec_reference_verified`。
5. 长天气完整播报与主人匹配仍待复测，不要放宽 `reject_non_owner_voice`。
6. Control 默认镜像可后切，只影响新设备。T1–T14 过了再谈 `full_duplex_verified`。

**勿做**：宣传全双工；hello 把 `aec_reference_verified` 写成 true；打开播放期 KWS；把 TurnPhase 从 shadow 改成有副作用。

## 实时话轮状态层候选

```yaml
candidate: cpu_only_turn_phase
as_of_date: 2026-08-25
code: complete
wired: conversation_projection_media_session_and_playback_shadow
enabled: false
deployed: production_shadow
production_release_tag: 20260825-1112-turn-phase-shadow-prod-agent-component
production_release_commit: fa4a500318720a81b48bf13c4d54e64eb2cfbc97
deployed_at_utc: 2026-08-25T03:14:09Z
verified: unit_replay_cpu_memory_health_readiness_and_provider_smoke
side_effects: none
fcdr_proxy_code: complete
fcdr_proxy_wired: authoritative_projection_commit_and_exact_playback_ack_shadow_metrics
fcdr_proxy_enabled: false
fcdr_proxy_deployed: false
fcdr_proxy_verified: local_agent_full_regression_mypy_and_low_cardinality_contracts
```

`TurnPhase` 目前只作为 `ConversationProjection` 内部证据和低基数 telemetry 产出，不改变 endpoint/interrupt/commit 决策。固定 80 ms sample 窗口、late ASR endpoint fence、phase/floor 原子更新和权威 playback ACK 接线已通过单元、确定性 replay 与 CPU/内存预算。2026-08-25 的 production shadow 发布同时通过 Agent/Media Bridge 同镜像 healthy、restart=0、Bridge gRPC、LiveKit、FunASR、QwenRealtimeSearch、DeepSeek、Doubao、InterruptSemantic、私有 readiness 和外部 Host/SNI 门禁；这仍不是阶段 C 策略启用或真实设备误聆听、打断、Actual Heard、全双工验收。

新增的 FCDR/DuplexPO 启发式四维影子指标只统计话轮发起、回应性短语、语义重叠后的让渡终态，以及主人已提交语音/助手精确播放 ACK 的参与时长。所有标签均为固定枚举，不包含 session、person 或文本；主人时长缺少正式 speaker authority 时不计，设备近似 ACK 不计助手时长。它们只是现有权威事件的 proxy，不是论文中的学习奖励或对话质量分，也尚未发布到生产；不得据此调整 endpoint、interrupt、commit 策略或宣称真实设备效果提升。

## 生产拓扑

- Control API：`memoria-control-api:20260906-1458-account-device-discovery-control-api`，overlay revision `4a3b91bfa156f946f67b92e0b0ced17fab108a67`，image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`。2026-09-06 15:11 CST 切流，延迟复核 healthy、restart=0；账号设备发现已返回真实绑定，生产 PG/RLS 只读 canary 通过。13 个非目标容器、有效环境、绑定/角色和声纹未改；自定义声音与合格登记 active 能力保留。紧邻回滚 `memoria-control-api:rollback-20260906-1458-account-device-discovery-control-api-pre-control`（原 `20260906-0048-companion-custom-voice-control-api`，image `sha256:6ef47dee3cb779b55a766ea6f392ae8f556315bd5a4322f48caf5fe6624c5b41`）。收据 `/opt/memoria/component-releases/20260906-1458-account-device-discovery-control-api/` 下 `cutover.json`、`readonly-canary.json`、`candidate-provider-smoke.json`。栈 env 仍为 `20260901-0945-wake-word-whitelist`；当前 ready 503 与运行配置 404 边界见上节，不据容器 healthy 宣称全链路通过。
- 当前 runtime：`/opt/memoria/current` 原子软链；候选目录：`/opt/memoria/releases/`。
- 已退役 H5：`/memoria-h5` 固定返回 `410 Gone`，不再发布或切流静态前端。
- LiveKit：`livekit/livekit-server:v1.13.5`，Compose project `memoria-livekit`。
- Control API loopback：`127.0.0.1:8791`；legacy mini gateway：`127.0.0.1:8792`；legacy device gateway：`127.0.0.1:8793`；direct Edge device WSS：`127.0.0.1:8794`。
- Voice Core Media Bridge 生产容器名：`memoria-voice-core-media-bridge-1`（Compose service `voice-core-media-bridge`，与 Agent 同镜像）。
- 终身档案：PostgreSQL 17 + pgvector；对象：MinIO；设备共享权威：独立 mTLS Redis。

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

**module budget 已转绿（2026-09-01）。** 三个模块此前共超 283 行。棘轮脚本 `update` 显式拒绝抬预算，且 `check` 双向拒绝（低于预算也报 `is below budget`），所以只能搬代码；又因为 `update` 会先对**全部**条目算 over_budget、任一超标就整体 raise，三个模块必须同批修完再跑一次 `update`，单独修任何一个都无法转绿。按 `voice_core/media_session_*.py` 与 `runtime_speaker.py` 的既有 mixin 惯例做纯搬移（零行为改动，搬移体逐字节 diff 核对）：

- `conversation_projection.py` 1114 → 1067：5 个 `StrEnum` + `SpeakerClass` 搬到新 `orchestration/projection_types.py`。原模块用 `X as X` 别名 re-export（mypy strict 隐含 `no_implicit_reexport`，普通 `from ... import` 不算导出），13 处下游导入零改动。已实测 `TurnPhase` 跨三条导入路径 identity 保持 `True` —— 40+ 处 `is TurnPhase.X` 断言依赖这一点，绝不能新旧模块各定义一份。
- `agent.py` 2456 → 2316：`_frozen_designed_fallback` / `_apply_cached_voice_profile` / `_heard_only_chat_context` 搬到新 `agent_voice_profile.py`。`media_agent_factory.py:12` 与 `session_entrypoint.py:15` 的 import 必须改指新模块，否则 strict 报两个 `attr-defined`。
- `duplex_runtime.py` 4485 → 4246：新增 `runtime_provenance.py`（provenance 5 方法 + `GenerationVoiceSnapshot` + 两个上限常量）与 `runtime_emotion.py`（emotion 3 方法），照 `DuplexSpeakerMixin` 的 `if TYPE_CHECKING:` 存根写法过 strict。MRO 已实测 `[DuplexRuntime, DuplexSpeakerMixin, ProvenanceMixin, EmotionMixin, object]`，逐方法确认归属无遮蔽。`GenerationVoiceSnapshot` 别名 re-export 给 `agent.py`。

预算已棘轮到 4246 / 2316 / 1067。副作用一条：搬走的 `emotion_observation` 与 `speech_plan_selected` 两条日志 logger 名由 `...duplex_runtime` 变为 `...runtime_emotion`（无测试按 logger 名断言，但按 logger 字段过滤的日志检索会受影响）。全仓 `ruff check` / `mypy services --strict`（430 文件）/ Agent 全套测试通过；`services/control_api` 与 `device_fleet` 有 19 个既有失败（需 Postgres），已用 stash 对照确认失败集与改动前逐条相同。

2026-09-01 起 `deploy_agent_component.sh` 自己跑门禁，不再依赖操作员记得手动跑：在 `verify_release_source.py` 确认 worktree 干净之后、SSH 触到远端之前，依次跑 ruff（`services/agent` + `test_production_compose.py`）、`check_module_budget.py check`、`mypy services/agent --strict`、Agent 单测 + 部署契约测试，任一失败即 `exit 1` 拒绝发布。门禁命令用 `env -u LISTENER_CUES_ENABLED -u LIVEKIT_ADAPTIVE_INTERRUPTION -u OFFLINE_MOCK -u INTERRUPTION_MIN_DURATION_S` 剥掉本地 `.env` 注入，否则本机 shell 的 169 个变量会让测试假红。`--skip-gates` 可关闭，但必须在收据里写明理由。此前该脚本零门禁调用，是 module budget 红了 122 个提交、跨约 6 次切流仍能上生产的直接原因。

**GitHub `main` CI pytest 已于 2026-09-02 转绿（`312c3ad`）**：自 2026-08-29 起 workflow 多次只在 Ruff / module budget 步失败，pytest 从未执行；module budget 转绿后首次全量 pytest 暴露 4 项失败（TurnPhase 内存预算断言、唤醒词 `device_settings` 测试过期、legacy vector 升级 setup、self_model 缺 `postgres_memory_schema`），已在同提交修复。此后 `main` push 应把 agent + python 两条 job 全绿当作发布前置条件，不得再依赖「文档-only CI 绿」。

Agent-only 快速路径的运行时切片只允许 `services/agent/**`；发布脚本与对应门禁测试可以随发布机制修复，但依赖锁、运行时 Dockerfile、共享包或其他服务变化必须走完整镜像发布。当 `main` 上存在与 overlay 无关的漂移（例如 `packages/contracts/**`）而 Agent 代码不依赖它们时，可用 `--allow-scope-drift` 继续 overlay 发布。Cutover 用 Control 的 env `MEMORIA_RELEASE_TAG` 做 compose 插值（组件 overlay 的镜像 version 可以不同）；不要再用 Control 镜像 label 当栈 tag，否则 heartbeat 会 409。

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

## 回滚

回滚以组件最小范围执行：冻结失败候选日志和 manifest，恢复切流前 image/软链/env，等待健康与具名 gRPC/readiness，再重跑外部路由和 provider smoke。若 Edge 长连接未自动重拨，按当次回滚回执中的受控步骤处理，不能假定重启无副作用。

回滚完成后记录当前与回滚两个可运行版本，删除更早普通上传包、构建归档、候选和回滚镜像并检查磁盘。保留失败证据的摘要和服务器路径即可，不在仓库新增 release 文档。

## 验收门槛

T1–T14 的原始 evidence/receipt 写到被忽略的 `outputs/acceptance/`，通过 `scripts/hardware_realtime_acceptance.py` 验证。覆盖设备启动、票据、防重放、上行、首帧、播放终态、硬停、网络恢复、连续轮次、AEC/双讲、长稳和小程序独立性。

以下任一出现都 REJECT：

- 首个可播放下行帧不是当前 fence 的 sequence/sample 0/0。
- 旧 session/stream/generation/tool epoch 产生可听输出或档案写入。
- playback terminal 缺失、错误被记为完成、Actual Heard 从网络发送量推断。
- Redis/Bridge/Provider authority 不可用时回退到并行本地权威。
- 真实设备、真实 provider 或真实网络证据被本地 mock、零会话 readiness 或旧候选证据替代。
