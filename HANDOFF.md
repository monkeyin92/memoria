# Memoria 当前交接

本文件只记录当前有效状态、紧邻回滚、生产操作和下一验收。历史发布过程不再保留；每次发布原地更新本文件。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-10
resume_checkpoint: epoch1899_single_lookup_ack_cut_over_still_awaiting_post_playback_farewell_serial
firmware_face_acceptance: conversation_face_v3_flashed_awaiting_idle_and_five_expression_photos
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.84
miniprogram_account_device_sync: uploaded_0.8.84_phone_desktop_pending
production_readiness: not_ready_smoke_evidence_expired
production_readiness_observed_at: 2026-09-06T20:49:35+08:00
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
current_work_order: vocat_interrupt_assist
code: complete
wired: firmware_0024_and_agent_barge_in_wait_cutover
enabled: production_agent_bridge_edge_true_device_audio_mode_interrupt_assist
verified: production_runtime_provider_model_inference_identity_safe_board_boot_secure_device_onboarding_owner_silence_standby_and_device_wake_ack_heard
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

`full_duplex_verified` 只有真实硬件 AEC、双讲、打断、连续会话和 Actual Heard 证据全部通过后才能改为 true。在此之前产品不得宣传全双工。小程序不申请 `scope.record`，也不承担实时媒体回滚职责。

当前工单 `vocat_interrupt_assist`：播放期保持采集，Agent barge-in 跟协商 `audio_mode`。LiveKit 设备路径仍半双工。Direct Edge 把 `assistant_expression` 转成板子 `screen.expression`。ATK ES8388 半双工投资人 Demo 已退役。Control 默认镜像可后切，只影响新设备。0024 已刷；Agent 依次切过 `20260910-1011` → `20260910-1526` → `20260910-1820`。

epoch **1897** 真机（13:56 CST，session `b910a0ee`）与 **1899** 复测（17:30 CST，session `7c465319`）复现同一组缺陷，分两轮修：

1. **filler 连播两遍**。1897：边车把同一句 11 字提问识别两次，turn 2/3/4/5 提交四次；可听序列是两段约 2s 输出之后才是 8.3s 正文。1899 复测仍在（说明第一轮修复不足）：提问同样提交两次（turn 2、turn 3），**两次提交各开一次委派、各播一遍 ACK**——`generation-2` 1.19s（被 turn 3 抢占）+ `generation-3` 1.95s，之后才是 `generation-4` 6.5s 正文。
   - 第一轮（`b83e9b7` / 组件 `20260910-1526`）：只给 **deep result 前缀** 加了会话级去重 `_live_lookup_filler_already_audible`，没门控 ACK 本身的发出，所以 1899 仍听到两遍。该轮修复本身有效（1899 的 `generation-4` 前缀已被剥掉）。
   - 第二轮（`df41596` / 组件 `20260910-1820`）：把 `_live_lookup_filler_already_audible` 也用作 **ACK 发出**的闸门，并新增 `_forget_live_lookup_filler` 在委派交付答案时释放「本次查询突发」记忆，保证之后的新提问仍会播自己的提示。复现测试 `test_duplicate_turn_commit_does_not_emit_a_second_lookup_ack`（断言 ACK 只发一次，修前 2 == 1 红、修后绿）。
   - 遗留取舍：重复提交若在 ACK 播完前抢占它，用户会听到**一句被截短的**提示且不重播（例如 1899 的 1.19s）。要「完整播一遍」，得让重复提交不抢占正在播的 ACK，属 turn-commit 层改动，本轮未做。
2. **聆听中残留**。1897 靠 `owner_silence_timeout`（15.3s）关闭；1899 已改为 `conversation_end_explicit` 关闭——告别路由这轮生效了，但**屏上仍停留 15~18s**：18:30:42.42 进入 `user_speaking` 后，两次 `media turn discarded after ASR tail timeout`（endpoint 153920 / 248000，均 `empty+vendor_silent`）耗掉约 14s，直到 59.51 才拿出 `text_len=2`（「再见」）并关闭。1899 那几段音频 rms 292~1231、无削波（1897 是 rms 2226~6098 且削波），即**不是回声污染，是没转出文本**。根因仍需设备侧串口证据（`/dev/cu.usbmodem101` 目前可打开但长时间零输出，已确认 USB 已枚举为 `USB JTAG/serial debug unit`，是芯片侧没往控制台写）。

另修 `a43668c` 引入的回归：它把 `duplex_runtime` **未分类 VAD 路径**的 `explicit_interrupt` 从 `False` 放宽成「含命令意图」，使影子/uncertain 声纹的「停一下」也能抢话轮停播，`test_playback_shadow_guest_fallback_cannot_bump_fence_or_stop_playout[停一下]` 转红（干净 HEAD 上就红）。已把该路径收窄为仅 `END_SESSION` 放行，`h1`（`_speaker_allows_user_input` 的告别子句）与 `h4`（已分类路径的告别放行）**按原样保留**——它们没有单测覆盖，但是为真机播放期告别所加，不能用「单测绿」反推可删。新增 `test_playback_unconfirmed_farewell_still_takes_the_floor` 钉住告别仍可抢到话轮。

模块预算没有上调：`a43668c` 让 `duplex_runtime` 从正好 4246 涨到 4260，而 `deploy_agent_component.sh` 把 `pyproject.toml` 当依赖输入（见「发布前门禁」），改预算就断快速通道。改为在 `a43668c` 自己引入的表达式内原地压缩 13 行（合并多行调用、折叠集合字面量、精简注释），行为不变，文件回到正好 4246。

## 下一验收

按顺序。点屏 / 摇晃 / 短拍逻辑已于 2026-09-09 16:08 CST 操作员 PASS；本轮只换 v3 对话脸画面，不改点屏拍击接线。半双工双轮（星期几 + 天气 + 短告别）曾在 epoch 1379 PASS，不代替下列项。

| 项 | 标准 | 状态 |
| --- | --- | --- |
| 待机脸照片 | 拍 `idle.jpg`，黑底月牙+平嘴+鼻点，对照 `outputs/firmware-face-v3-20260909/sheet.png` 的 `neutral` | 待拍 |
| 五表情照片 | 唤醒后按「屏幕表情」表各拍一张（happy/loving/sad/surprised/thinking），说完回待命月牙+平嘴 | 待拍 |
| barge-in 告别 | 天气播报中途说「好的，再见」：串口 Device VAD start（Speaking 态）、`conversation_end_explicit` / `session.close`、屏回待命月牙，不是「聆听中」。0024 已刷，`20260910-1820` 已切。BOOT 仍能硬停 | 1899 已达成 `conversation_end_explicit`；屏上停留仍在，见「播后短告别」 |
| 单次查询提示 | 问天气只听到**一遍**「稍等，我查询一下。」，随后直接是正文；重复提问不得连播两遍 filler | `20260910-1820` 已切，待真机复测（1899 在 `20260910-1526` 上仍两遍） |
| 播后短告别 | 正文播完再说「好的，再见」应关闭会话回待命，不靠 `owner_silence_timeout` 兜底 | 未达成；1899 逻辑上已走显式关闭，但屏上仍停 15~18s（两次 empty ASR 轮次耗掉 ~14s 且音频未削波），需带串口取证 |
| 长天气 | 完整播报不被 45s 墙钟掐断 | 代码已切流，未真机复测 |
| 长回复不断音 | 唤醒问候后再说一句较长的话，整句听完；允许串口 `Dropping server packet`，不得再把队列满升级成 `playback.error` 一字卡断 | 0023 已 app-only 刷入，未真机说话 |
| 主人匹配 | 主人轮通过，非主人不放行；不要放宽 `reject_non_owner_voice` | 声纹 active，当轮匹配未复测 |
| 小程序 0.8.84 | 手机微信切开发版，核「设备在线」、首页新文案、设备 095c | 已上传，未体验版 / 未提审 / 未手机验 |

**勿做**：宣传全双工；把 `hardware_verified` / `direct_real_device_verified` / `full_duplex_verified` 从刷机、欢迎语或点屏拍击外推为 true；hello 把 `aec_reference_verified` 写成 true；打开播放期 KWS；把 TurnPhase 从 shadow 改成有副作用；伪造 owner；把未 active 的声纹当主人认证宣传。

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

- 当前：`memoria-agent:20260910-1820-single-lookup-ack-agent-component`，源 `df415964e602e3c97156caf3f65da924ec231cc7`，image `sha256:329e70ad8924def5ff65d19cba40d2851787ec9100bcbeb146abb50d4c1a42be`。healthy、restart=0、OCI revision 已核对。切流 `2026-09-10T10:21:43Z`。收据 `/opt/memoria/component-releases/20260910-1820-single-lookup-ack-agent-component/`。同一轮曾切过 `20260910-1526`（`b83e9b7`，只修 result 前缀、未门控 ACK，1899 复测无效）。更早 `20260910-1011` 的首次 `--cutover` 曾因本机 PATH 解析到 macOS 自带 openrsync 2.6.9 而在上传段失败（`rsync: unrecognized option '--protect-args'`），生产未受影响；改用 Homebrew rsync 3.5.0 后重跑成功。
- 回滚：`rollback-20260910-1820-single-lookup-ack-agent-component-pre-agent/-pre-bridge`（镜像 `20260910-1526-single-lookup-filler-agent-component` / `sha256:bddced4ec9a144bdf981a6500159a5fd121b32cc770f5c89991ac7568ae617b7`）。
- 当前镜像已含欢迎语 latch、hello `audio_mode` 身份比对、长天气 stall 重置、半双工 heard/lookup、播后声纹过滤、播放期空缓冲 barge-in WAIT、live-lookup filler 会话级去重（result 前缀 + ACK 发出双闸门）、`a43668c` 影子声纹回归修复。这些是已切流能力，不等于天气告别与播后短告别已验收。

**Media Edge**

- 当前：`memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`。override `/tmp/media-runtime.override.yml`。healthy、restart=0。
- 回滚：`memoria-media-edge:20260901-0945-wake-word-whitelist`；override 备份 `/tmp/media-runtime.override.yml.pre-20260908-1600-vocat-interrupt-assist`。

**Control API**

- 当前：`memoria-control-api:20260906-1458-account-device-discovery-control-api`，overlay `4a3b91bfa156f946f67b92e0b0ced17fab108a67`，image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`。SQLite `/data/memoria.sqlite3`。收据 `/opt/memoria/component-releases/20260906-1458-account-device-discovery-control-api/`。
- 回滚：`memoria-control-api:rollback-20260906-1458-account-device-discovery-control-api-pre-control`（原 `20260906-0048-companion-custom-voice-control-api`，image `sha256:6ef47dee3cb779b55a766ea6f392ae8f556315bd5a4322f48caf5fe6624c5b41`）。
- 现网设备 `dev_atk_a4cb8fd6095c` 已改为 `audio_mode=interrupt_assist`（settings_version 12）。Control 镜像未切新设备默认值，旧票据不会自行升档。

**其它运行事实**

- 栈 env `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。对话 `qwen3.7-flash`，分类 `qwen-flash`，深查 `qwen-plus`。
- SenseVoice sidecar：`memoria-sensevoice-asr:20260901-pin-language`（默认 `zh`，未知语言 415）。回滚镜像 `v1` + `/opt/memoria/sidecars/sensevoice-asr/run_sensevoice_asr.py.rollback-20260901-prelang`。Dockerfile 只在服务器该目录，重建不可复现。
- 主人主体两库 `adult/verified`；声纹 `1b5b577b` **active**。CAM++ 反欺骗仍 `unavailable`，不要改成 `verified`。
- epoch **1892** 唤醒欢迎语听感 + Actual Heard PASS；随后 `conversation_end_explicit` 关闭。
- 自定义克隆已用于唤醒；天气正文音色绑定尚未真机复测。
- 小程序开发版 **0.8.84**（2026-09-06 23:58 CST，源 `7e5137a`，903,051 bytes）。未正式发布、未设体验版。
- 公网 `/health/ready` smoke 过期 503、运行配置 GET 404 仍在；不据容器 healthy 宣称全链路通过。

## 板卡与固件

- 硬件：乐鑫 ESP-VoCat N32R16（ESP32-S3，32MB Flash / 16MB Octal PSRAM）。board `memoria-esp-vocat`，app 2.4.2。现场 `192.168.8.142`，uuid `1ac87deb-0fa3-4300-a304-ad6c472ab8c7`。
- 音频：ES8311 输出 + ES7210 双麦，输入增益 **36.0 dB**。hello 报 `simultaneous_capture_playback=true`、`aec_mode=fd_low_cost`、`aec_reference=software_post_gain_pre_i2s`、`barge_in_level=1`；`aec_reference_verified=false`。
- 屏幕：1.85 寸 QSPI 圆屏 ST77916 360x360。触摸 CST816S：说话中单击硬停，聆听中单击退出聆听；**待机/连接中单击忽略**。
- IMU：BMI270。待机只认短拍（阈值 dx+dy+dz>3200、最多 120ms 脉冲、落地后再确认 60ms），冷却 2.5s，只闪 surprised。持续摇晃忽略；点屏 PRESS/HOLD mute IMU 400 ms。开麦权威仍是唤醒词「茉莉」或 BOOT。
- 身份区 `0x10000` 64KB 写保护，SHA `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`。OTA app `ota_0` `0x20000`。assets 8MB。
- 2026-09-10 10:00 CST app-only 已刷 overlay 0024（interrupt_assist 播放期发 vad.start；含 0023 队列满不 terminal）；未写 bootloader / 分区表 / 身份区 / NVS / assets。开机 `2.4.2` / SystemInfo 心跳。2026-09-10 10:15 / 15:46 / 18:21 CST Agent 依次切 `20260910-1011` / `20260910-1526` / `20260910-1820`。这不等于告别与播后短告别验收。
  - app `9e52bdf44a1022dc23f9ffaab043ebb8c0acc426733e4f28ffa37dd5d2748186`
  - merged `33851b8ffd2547078a78a4b77d9f4bf542cbefd09fa0a894ec175cb9df8bcbde`
  - bootloader `434b1a190c9607a289b1b0e14df3329864c24bc9443787814e0db0cc94e8b098`（本轮未写；与上一版构建哈希不同，勿整包补刷）
  - partition-table `da35229c3fe72536129e09663615c1ee9851a74f43493a154f5d40d359b1dc8b`（本轮未写）
  - overlay `581a801a273d6c323581ac4dbd57e7ba74628e843e838902a50746e6ca75db9f`
  - 回滚 app `firmware/esp32/artifacts/backups/pre-playback-barge-in-20260910/app-before.bin`（0023 `d7efa985…`）
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
