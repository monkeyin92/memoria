# Memoria 当前交接

本文件只记录当前有效状态、紧邻回滚边界、生产操作和下一验收。历史发布过程不再保留；每次发布原地更新本文件。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-01
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
miniprogram_role: control_plane_only
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
current_work_order: half_duplex_investor_demo
code: complete
wired: esp32_to_go_media_edge_to_python_voice_core_agent
enabled: production_agent_bridge_edge_and_current_firmware_true
verified: production_runtime_provider_model_inference_identity_safe_board_boot_secure_device_onboarding_owner_silence_standby_and_device_wake_ack_cutover
production_runtime_verified: true
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: pending
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
```

`full_duplex_verified` 只有真实硬件 AEC、双讲、打断、连续会话和 Actual Heard 证据全部通过后才能改为 true。在此之前产品不得宣传全双工。小程序不申请 `scope.record`，也不承担实时媒体回滚职责。

## 当前生产

当前 Agent/Bridge 发布提交为 `61e7428a8ec68e0e3535a722aaa4c3e37167c0e6`（2026-08-31 22:15 CST 切流，标签 `20260831-2215-miniprogram-bind-view-agent-component`）。Control API 与 Media Edge 发布提交为 `7ca3d4ec531305d968d67ef1bb13b944e566e4cf`（2026-09-01 10:02 CST 切流，标签 `20260901-0945-wake-word-whitelist`）：

- Agent 与 Voice Core Media Bridge：`memoria-agent:20260831-2215-miniprogram-bind-view-agent-component`，revision `61e7428a8ec68e0e3535a722aaa4c3e37167c0e6`。两个容器 healthy、bridge gRPC PASS、restart=0。紧邻回滚点 `memoria-agent:rollback-20260831-2135-close-intent-semantic-router-agent-component-pre-agent` 与 `-pre-bridge`。设备会话仍 `barge_in_enabled=false`；DTLN `8.0x`、PCM tap 仍在 bridge `/tmp/media-pcm-tap`。不得仅凭听感把全局 `direct_real_device_verified` 改为 true。
- Media Edge：`memoria-media-edge:20260901-0945-wake-word-whitelist`，revision `7ca3d4ec531305d968d67ef1bb13b944e566e4cf`，容器 healthy、`127.0.0.1:8794` 监听。`session.accepted` 已下发 `wake_word_id` / `wake_word_pinyin` / `wake_word_display`。紧邻回滚镜像 `memoria-media-edge:20260825-1730-jasmine-standby-prod-edge-component-v4`（revision `4c3971fef0bfdfc30e9bff742c40ffdd848c0e7c`）；`/tmp/media-runtime.override.yml` 已钉住本标签。
- SenseVoice 兜底 sidecar：`memoria-sensevoice-asr:v1`（sherpa-onnx 1.13.6 + SenseVoice-small int8，`/opt/memoria/sidecars/sensevoice-asr/`，docker 网络 `memoria_default`，--cpus 2 --memory 1g）。Agent 侧 `SENSEVOICE_URL=http://memoria-sensevoice-asr:8001/transcribe` 已配置；FunASR 空转写且 RMS≥100 时自动兜底（fail-open，2.5s 超时）。紧邻回滚点 `rollback-20260829-0859-sensevoice-rescue-agent-component-pre-agent/-pre-bridge`。
- Control API：`memoria-control-api:20260901-0945-wake-word-whitelist`，revision `7ca3d4ec531305d968d67ef1bb13b944e566e4cf`，容器 healthy。白名单 catalog（`mo_li`、`mei_mo_li_ya`）、`POST /v1/devices/wake-word/validate` 与 MultiNet 自定义唤醒词 settings 已上线。紧邻回滚镜像 `memoria-control-api:20260831-2215-miniprogram-bind-view-control-api`（revision `61e7428a8ec68e0e3535a722aaa4c3e37167c0e6`）。
- 小程序体验版 **0.8.74**（2026-09-01 微信开发者工具上传）：设备页支持白名单切换与自定义唤醒词（pinyin + display）。
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
miniprogram_experience_version: 0.8.74
```

本轮已打通并验证以下顺序：二维码 introspect → BLE Protocomm Security 1（X25519、PoP、AES-256-CTR）→ 设备 online-proof → claim/binding → Activation Manifest → 设备 ACK → `ready_for_conversation`。小程序不采集声纹或实时语音；Wi-Fi 密码只在已认证的 BLE 会话中写入设备，不经过 Control API 日志或小程序普通请求。

设备第一次在绑定完成前收到激活 `409` 时，会继续显示附近配网入口并在后台每 5 秒重试 Activation Manifest；绑定完成后设备拉取清单、返回 ACK，并停止配网二维码入口。相同二维码从新页面重试时复用原 onboarding session，避免误报“设备正在被其他账号设置”。

真实证据：设备 `dev_atk_a4cb8fd6095c`（BLE `MEM-095C`）的最新激活记录为 `activation_version=3`、`status=ready_for_conversation`，`acknowledged_at=2026-08-27 06:34:45.034649+00`（UTC，即 14:34:45 CST）。Nginx 记录设备于 14:34:44 拉取 manifest 200，14:34:45 提交 activation-ack 200；小程序随后显示“在线，可直接对话”和“激活状态：可开始对话”。

这证明了服务端和控制面激活闭环，不等于真实语音对话、AEC、双讲、连续轮次或完整屏幕物理显示验收。当前已通过串口确认固件 2.4.2、Wi-Fi、Manifest v3 和稳定运行；板屏是否已由二维码切换到正常界面尚未单独留存最新照片，不能用小程序页面替代该物理证据。

## 当前板卡与固件

- 固件 app version：2.4.2；ES8388 输入增益：18 dB。
- app SHA-256：`c11fb87ed4ffd0206aa5a9554d6226d539186f71040e4ea8525dea6b3dd31492`。
- merged SHA-256：`3f45c9f394847d617ab1df36919e74cecee41a84e0a3cbb13b75e61f7346f68e`。
- overlay SHA-256：`8bad9a23dd0559598155f7b701da94d2564fcf1f1cb99d427bd60051b878bb69`（含 patch `0021` 白名单唤醒词与 `memoria_wake_word` 注册表）。
- 默认出厂唤醒词仍为「茉莉」（`mo li`）；assets 同时打包 `mei mo li ya`，运行时经 device settings / NVS 切换；自定义词走 MultiNet 拼音命令（v1，非云端 WakeNet 训练）。
- `memoria_identity` 刷前/刷后 SHA-256：`b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`，逐字节一致。
- 2026-09-01 使用 `scripts/flash.sh --port /dev/cu.usbmodem101 --build` 写入 bootloader、partition table、OTA data、`generated_assets.bin` 与 app，未写入 `0x10000..0x1ffff` 身份区。
- Speaking 期间关闭 KWS 并忽略迟到 wake event；回到 idle 后恢复。BOOT 始终是本地物理硬停止。

这些证据只达到 `identity-safe flash + board boot/activation`，不等于完整设备媒体或 Actual Heard。

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

固件只在本地 MultiNet/KWS 用当前活跃命令词唤醒（默认 `mo li` / 「茉莉」），仍不上传唤醒词音频。小程序设备页可切换白名单词或保存自定义词（display + pinyin）；设置经 Control API → Media Edge `session.accepted` 下发，固件需含 patch `0021` 且设备重连后生效。自定义 v1 不走云端 WakeNet 训练；两音节词误唤醒风险仍高，阶段 4 计数待做。设备会话中的“再见”“知道了”“退下吧”等精确结束语只有通过目标说话人权威判定后才关闭；带后续内容的句子不会误触发。无主人语音计时只由 Python Voice Core 的权威会话状态管理：助手输出和传输断开期间暂停，回到聆听时开启 10 秒窗口，裸 VAD/环境声不能重置主人计时。**但 2026-09-01 真机暴露一个例外：助手自己的「没听清」提示播完后走同一条 `assistant_state=listening` 复位路径，把主人静默窗口重置成完整 10 秒，等于助手语音给自己续命（`media_session_projection.py:484` → `media_session_standby.py:179`）。**关闭通过 typed `CONVERSATION_STATE_CLOSED` 进入 Go Media Edge，再下发设备 `session.close` 回到 Idle，不新增第二套聆听状态机。

当前候选已发布到生产 Agent/Bridge/Edge 并写入当前板卡。2026-08-30 10:19 切流后 Direct 唤醒 TTS 曾用 `turn_id=0`，固件拒包、无 Actual Heard；10:33 已改为先打开 `turn_id>=1` 再播允许名单短句（「我在。」「哎，我来了。」「哎呀，好困呀。」）。该听感尚未真机验收，不能更新 `direct_real_device_verified`。2026-08-25 真机已验证“茉莉”唤醒后静默约 10 秒，Python 产生 `owner_silence_timeout` typed CLOSED，Edge 成功排队 `session.close`，设备回到待命；Edge 也已增加旧 FLOOR fence 丢弃和重复 CLOSED 幂等保护。明确结束语测试时，ASR 路由进入结束语分支，但正式主人权限返回 `subject_capability_forbidden`，记录为 `conversation_end_owner_unverified` 并由随后超时关闭，因此 `conversation_end_explicit` 仍待主人声纹/subject profile 权限就绪后复测。两音节“茉莉”相较原四音节唤醒词有更高误唤醒风险，安静、电视人声和家庭噪声三种环境的阈值验收仍未完成，不能更新 `direct_real_device_verified`。

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

## 当前开发工单：半双工投资人 Demo

```yaml
candidate: half_duplex_investor_demo
as_of_date: 2026-08-31
hardware: atk_dnesp32s3_v1_es8388_1mic_0_playback_afe
audio_mode: half_duplex_safe
advertised_duplex_level: none
barge_in: forbidden
turn_phase_side_effects: forbidden
direct_real_device_verified: false
full_duplex_verified: false
stage_2: receipt_filed_3of4_pending_serial
stage_3: pending_homepage_initiated_session
stage_4: pending
stage_5: fail_2026_09_01_missed_hearing_nudge_preempts_silent_close
stage_6: pending
stage_7: pending
success: two_natural_turns_actual_heard_then_wake_standby_script
```

本工单取代上一轮「只列五条验收、顺序把连续两轮放最后」的做法。**阶段 2 阻塞项已于 2026-08-31 解除（操作员听感）**：同一 session 内「今天星期几」与「南京天气」均有完整回答；Agent 切片 `20260831-0955-half-duplex-asr-stall-rescue-agent-component`（`bc56f6b`）已切流。正式 receipt 已落盘 `outputs/acceptance/half_duplex_investor_demo-20260831-0949.md`（session `79b6e405-347d-4aad-89be-6e82d4fa1c65`，stream_epoch=1310，tap WAV 已拷本地）；**仍缺串口 UART 四件套之一**，且未跑 `hardware_realtime_acceptance.py verify`，故不能把本候选 `verified` 写成带日期的两轮 Actual Heard，也不能把全局 `direct_real_device_verified` 改为 true。2026-08-30 15:07 CST 高铁长回答 `superseded` 收据见 `outputs/acceptance/half_duplex_investor_demo-20260830-1507.md`，待同一长回答剧本复测。2026-08-31 00:00–09:00 CST 曾出现 FunASR 空转写 + `unknown_safe` policy 拦播报 + 唤醒误触发「没听清」等分支，已由 `0020`/`0955` 及前序切片修复；不得用旧日志升级 `verified`。1150 之前的 11:33 阶段 2 FAIL 见 `outputs/acceptance/half_duplex_investor_demo-20260830-1133.md`，不得复用。不得把 `direct_real_device_verified` 改为 true。长期契约见 `PROJECT_RULES.md`「当前出货声学契约」。开发人员只执行本节阶段 0–7；阶段 8 是后续 SKU，本工单内禁止开工。

### 下一阶段（按顺序，2026-08-31 10:55 起）

0. **先修阶段 5 阻塞（代码，不必到现场）**：2026-09-01 真机操作员观察——当前固件 `c11fb87e…` 上唤醒「茉莉」→「今天星期几」→「南京天气怎么样」两轮都有完整回答（阶段 2 的听感部分在新固件上复现通过，但四件套未挂、无 receipt，不能升级 `verified`）；随后**保持安静，期望 `owner_silence_timeout` 静默关闭，实际板子自己说了一句「刚才没有听清，可以再说一遍吗？」，最后由操作员按 BOOT 强制待命**。定位到两个缺陷：
   - **A. 回复播完后的空轮次没有回声保护、也不校验主人权威。** `_is_wake_echo_discard` 只在 `device_wake_ack_fence` 存在且助手正在说话时挡（`media_session_projection.py:148`），而该 fence 在播放终止时就被 `clear_device_wake_ack_fence` 清掉（`media_session_output_stream.py:792`）。于是天气回答播完、麦克风重开后，单麦无 AEC 参考的播放尾音或环境噪声触发 VAD → ASR 空 → `empty_media_turn` 以 `allow_without_endpoint=True` 调 `_nudge_missed_hearing`（`media_session_commit.py:508`/`:542`），全程没有 `current_speaker_class == "owner"` 判定。环境噪声因此能让板子主动说话。
   - **B. 提示语自己重置主人静默窗口。** `_speak_missed_hearing_ack` 调 `open_assistant_floor_for_nudge` 播 `BRIDGE_PHRASES[2]`，播完回 `listening`，`_sync_owner_silence_phase` 走 `_arm_owner_silence_timer(reset=True)` 重开满 10 秒。每次提示把关闭点往后推 10 秒。有上界（`_MAX_MISSED_HEARING_NUDGES=2`、冷却 12 s），第 3 次空轮次会以 `missed_hearing_loop` 待机——但那是错误的关闭原因，且要拖到约 40 秒，阶段 5 的通过标准要求 `owner_silence_timeout`，无论如何算 FAIL。
   尚未确证的是哪个源头产生了那个空轮次（播放尾音回声 vs 环境 VAD），需要日志里 `empty_media_turn` 的 `stream_epoch`/样本区间与播放终止时间戳对比；两个缺陷本身已从代码确认。修完再下现场，否则阶段 5 必然重演。
1. **阶段 2 需在当前固件上整轮重跑**（不是只补串口）：已落盘的 `outputs/acceptance/half_duplex_investor_demo-20260831-0949.md` 记的是 `firmware_app_sha256=f15a3b35…`，而 2026-09-01 为白名单唤醒词重刷后当前板卡是 `c11fb87e…`。阶段 2 通过标准要求「同一固件摘要下」，因此该 receipt 不能用来升级 `verified`；重跑时串口 monitor 全程挂着，补齐 `vad.start`/`vad.end`、tap WAV、DTLN 后 RMS，并记 `wake_word_id`。
2. **阶段 3 + 5 一次现场**（推荐合并）：
   - 小程序停在**设备** tab，确认「在线，可开始对话」（不要从配网/设备 onboarding 页起手；小程序不再发起语音对话）。
   - 在**设备端**唤醒「茉莉」→ 问「今天星期几」→ 问「南京天气怎么样」→ 两轮均听完。
   - 安静 10 s → 期望 `owner_silence_timeout` → `session.close` → 板子 Idle → 再唤醒「茉莉」说一句话；不要说「再见」。
   - 落盘 `outputs/acceptance/half_duplex_investor_demo-<YYYYMMDD-HHMM>.md`，phase 填 3 或 5，notes 写明设备 tab 确认在线后由硬件起手。
3. **阶段 4**：安静环境「茉莉」×10，记录漏唤醒/误唤醒。白名单已上线，同一次现场把 `mo_li` 与 `mei_mo_li_ya` 各记一组，供路演选词；两音节风险见 `RESEARCH.md` R-20260831-01。
4. **阶段 6**：安静 / 电视 / 家庭噪声三环境，验证裸 VAD 不续命主人静默窗口。
5. **阶段 7**：阶段 2（含串口）+3+4+5 证据齐全后锁定投资人路演剧本。

阶段 8（AEC 新板、全双工 SKU）在阶段 7 demo-ready 之前禁止开工。

### 怎么开工（给开发人员）

1. 读完本节 + 上面的「播放后 VAD 上行门控死锁候选」+ `PROJECT_RULES.md`「当前出货声学契约」。不要另开计划文档。
2. 阶段 0 先跑门禁，确认没有人把设备 barge-in 打开。
3. 阶段 1 把串口、Agent 日志、Edge 日志、PCM tap 四件套同时接上，再进阶段 2。
4. 阶段 2 听感已通过（2026-08-31 操作员）；补正式 receipt 后进入阶段 3。没绿之前禁止刷 AEC 新板、禁止开抢话、禁止路演。
5. 每次真机失败只走一个分支（VAD / RMS / FunASR / fence），改完用新 receipt，不用旧日志升级 `verified`。
6. 阶段 2+3 绿了再锁阶段 7 剧本。`full_duplex_verified` 本工单内永远保持 false。

### 分工与入口文件

| 角色 | 阶段 | 只动这些（除非阶段 2 分支证明必须扩） |
| --- | --- | --- |
| Agent | 0、3–7、发版 | `services/agent/src/voice_core/media_session_*`、`services/agent/src/providers/funasr_stt.py`、`services/agent/src/clock_fact_queries.py`；LiveKit 路径仍见 `device_vad.py` |
| 固件现场 | 1、2、4 | `firmware/esp32/overlay/`（边沿/ES8388 PGA）；hello 能力字段禁止改成真 AEC；刷写 `firmware/esp32/scripts/flash.sh` |
| Media Edge | 1、2 无播放/无 close | `services/media_edge/`；确认 Direct WSS `wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`，不要落到 LiveKit compat |
| 控制面 | 3、5 | 设备 tab 确认在线、主人 subject capability；配网/激活不在本工单 |
| 路演 | 7 | 不改代码；按锁定剧本念，口径跟 `advertised_duplex_level: none` |

不要改、不要当修复入口：`input_policy` / `capture_allowed`（只对 H5 有效）、设备会话 `barge_in_enabled`、hello 谎称 AEC、`TurnPhase` 生产副作用、DTLN makeup 再往上加。

### 现场 receipt 模板

每次尝试复制到被 gitignore 的 `outputs/acceptance/half_duplex_investor_demo-<YYYYMMDD-HHMM>.md`（或同目录 JSON）。填空，禁止用「容器 healthy」当 pass。

```text
work_order: half_duplex_investor_demo
phase: 2
datetime_cst:
operator:
distance_cm: 30-60
device_id:
firmware_app_version:
firmware_app_sha256:
wake_word_id: mo_li|mei_mo_li_ya|custom
agent_image_id:
agent_source_commit:
edge_image_id:
session_id / stream_epoch / turn_ids / generation_ids:
serial_vad_start_end: present|missing
tap_wav_path:
dtln_post_rms_turn1:
dtln_post_rms_turn2:
asr_turn1: final|empty+vendor|empty+gating
asr_turn2: final|empty+vendor|empty+gating
sensevoice_fallback: yes|no
playback_ended_turn1: yes|no
playback_ended_turn2: yes|no
actual_heard_turn1: yes|no
actual_heard_turn2: yes|no
fail_branch: none|no_vad_start|low_rms|funasr_empty|fence_playback
notes:
```

结构化验收仍用 `scripts/hardware_realtime_acceptance.py verify`（有 JSON receipt 时）。本工单不要求 T1–T14 双讲格。

### 目标与非目标

目标（全部完成后才可把本候选标为 demo-ready；`direct_real_device_verified` 仍只覆盖下列半双工场景，不升 `full_duplex_verified`）：

- 现板契约保持受控半双工：用户说完 → 设备听完一整段回答 → 再听下一句。播放期间不形成抢话 turn。
- 投资人可复现剧本：唤醒「茉莉」→ 连续两问都有同 fence 的 `playback.ended` + 听感 Actual Heard → 设备能回待命再唤醒。
- 对外口径只说陪伴/档案/半双工听完再答；BOOT 可作为「随时能停」，不演示语音打断。

非目标（本工单内出现即 REJECT 该改动，即使测试变绿）：

- 打开设备会话 `barge_in_enabled` / `interruptions_enabled`，或把 hello 改成谎称 AEC。
- 启用 `TurnPhase` 生产副作用、T1–T14 双讲矩阵、新 AEC 板 overlay。
- 为「听不清」继续堆 DTLN makeup gain、播放期丢弃 `vad.start`、或用 H5 的 `capture_allowed` 去补设备防回声。
- 宣传全双工、自然抢话、或用 H5 语音冒充设备 demo。

允许的代码改动：只修半双工主链上已被 PCM tap / 日志定性的根因（门控、电平、ASR 空转写分流、fence、待命）。改完必须能指出失败轮是「VAD 没开」「RMS 不够」「FunASR 空转写」「holdoff 误判」中的哪一种。

### 证据与现场纪律

每次真机尝试绑定同一组身份，写入被忽略的 `outputs/acceptance/`，不要提交 Git：

- 生产 Agent/Bridge/Edge image ID 与 source commit（以现场容器为准，不从本文猜测标签）。
- 固件 app 版本、app SHA-256、`memoria_identity` 刷后摘要。
- 设备 `device_id`、session/stream/turn/`generation_id`/`tool_epoch`。
- 串口：`vad.start` / `vad.end`、播放 drain、`session.close`。
- Agent：ASR partial/final 或空转写、`text_len`、DTLN 后 RMS、SenseVoice 是否兜底。
- Edge：WSS close cause、typed `CONVERSATION_STATE_CLOSED` 原因。
- `/tmp/media-pcm-tap` WAV（容器内，4 MB/会话上限）；需要时拷到 `outputs/acceptance/` 并隐私处理。
- 听感：每一轮是否完整听到回答，记 pass/fail，禁止用「容器 healthy」代替。

现场不要按 BOOT/RESET，除非该步明确测硬停。说话距离 30–60 cm、正常音量。欢迎语未结束不要插话（半双工契约）。FunASR 空转写与设备门控必须分账：容器内直连干净 PCM 的 FunASR smoke 失败则记供应商，不改 VAD。

排障顺序（与 README 一致，本工单强制先走这一条再改代码）：无响应 = 设备状态/票据 → WSS epoch → VAD → ASR final → generation → 首个 0/0 下行帧 → playback terminal。电平问题先看 ES8388 PGA、原始 PCM RMS、DTLN 出入，再动云端阈值。

### 阶段 0 — 契约冻结（开发，先做）

确认当前树仍满足半双工诚实声明，不在本阶段改行为：

1. 固件 hello v2 仍为 `simultaneous_capture_playback=false`、`aec_mode=none`（见 `firmware/esp32/tests/test_memoria_protocol_source.py` 中 `test_hello_v2_declares_only_honest_simplex_capabilities`）。
2. `services/agent/src/session_entrypoint.py` 中 `device_session` 仍使 `controlled_half_duplex_session` 为真，从而 `barge_in_enabled=false` 且 `interruptions_enabled=false`。
3. `TurnPhase` 候选保持 `enabled: false`、无生产副作用。
4. 跑定向门禁后再下现场：

```bash
uv run ruff check .
uv run pytest services/agent/tests/unit/test_device_vad.py \
  services/agent/tests/unit/test_agent_production_wiring.py -q
uv run pytest firmware/esp32/tests/test_memoria_protocol_source.py -q
cd services/media_edge && go test ./...
```

阶段 0 不改生产行为。完整 `uv run pytest` 仅在阶段 2 需要发版时再跑。

通过标准：上述断言仍成立，本阶段无行为 diff。若有人打开设备 barge-in，工单停止并回滚。

### 阶段 1 — 现场工具就位（运维 + 固件）

1. 确认设备仍走 Direct Edge：`wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`，容器 healthy，不要误落到 LiveKit compat（无待命）。
2. 确认 Agent 容器 `MEDIA_PCM_TAP_DIR=/tmp/media-pcm-tap` 可写；每轮对话后立刻把该会话 WAV 取走，避免被 4 MB 上限丢掉。
3. 串口 monitor 保持到验收结束：`firmware/esp32/scripts/flash.sh --port /dev/cu.usbmodemXXXX --monitor`（已刷过则只 monitor）。Mac 下载模式仍是按住 BOOT、轻按 RESET、松开 RESET、松开 BOOT。
4. 小程序设备页已显示「在线，可开始对话」之后，直接在硬件端开对话；激活 200 不计入本工单。

通过标准：能同时拿到串口、Agent 日志、Edge 日志和至少一段非静音 tap WAV。缺一项不准进入阶段 2。

### 阶段 2 — 阻塞项：正常距离连续两轮（固件现场 + Agent 日志）

剧本（欢迎语播放完毕、设备回到 listening 后再开口）：

1. 唤醒「茉莉」（若已在会话中则跳过唤醒，仍记录 session_epoch）。
2. 问「今天天气怎么样」，等待完整回答结束（听感 + `playback.ended`）。
3. 再问「今天星期几」，等待完整回答结束。

每一轮必须同时有：ASR final 或已记录的供应商空转写分流、当前 fence 首个下行 0/0、`playback.started`、`playback.ended`、听感 Actual Heard。任一轮沉默、截断、播旧 generation，本阶段 fail。

失败时只按下列分支修，禁止同时改增益、VAD 和 ASR：

- 串口无第二轮 `vad.start`：门控/边沿/holdoff。对照「播放后 VAD 上行门控死锁候选」；修 `DeviceVadProjector` 或固件边沿，不得丢弃状态同步。
- 有 `vad.start` 且 tap RMS 过低（历史失败曾见降噪后 RMS 约 8–296）：只调 ES8388 输入增益或采集距离，用 tap WAV 校准；DTLN makeup 已 8.0x，禁止再作为第一手段上调。
- tap RMS 正常但 `text_len=0`：先在同一容器跑 FunASR 干净 PCM；失败记供应商并确认 SenseVoice 兜底是否触发。成功则查上行是否被 preroll 丢弃或 task 未重建。
- 有 ASR 无播放：查 generation fence、首帧 0/0、旧 generation 丢弃、playback drain。

需要发版则走现有最小切片（Agent-only 仅当 diff 只在 `services/agent/**`），切流后重跑本阶段，不得用旧 receipt 升级 `verified`。

通过标准：同一 candidate、同一固件摘要下，连续两轮各一次自然完成。然后才允许改 `HANDOFF.md` 里本候选的两轮对话证据日期；仍不得把 `full_duplex_verified` 改为 true。

### 阶段 3 — 单次会话从设备端发起

设备在线且小程序设备 tab 已确认状态后，在硬件端实际发起一次对话（可与阶段 2 同一天，但日志要能区分「设备端起手」）。激活成功、二维码、BLE 配网不计入。

通过标准：用户听到完整回答，且阶段 2 的 fence/playback 证据齐全。

### 阶段 4 — 唤醒稳定（可与阶段 2 同一固件，勿穿插调音）

待机、30–60 cm、正常音量说「茉莉」10 次。记录漏唤醒/误唤醒。安静环境先做；电视/噪声放到阶段 6。

通过标准：漏唤醒与误唤醒次数写入 receipt。两音节「茉莉」若误唤醒过高，只调 KWS 阈值或改回更长词，不开放播放期 KWS。

### 阶段 5 — 再见与再唤醒（本 demo 已书面降级）

**已降级（2026-08-30）**：主人 subject capability 未就绪。此前真机「再见」已落到 `subject_capability_forbidden` / `conversation_end_owner_unverified`（见「茉莉唤醒与自动待命候选」）。能力就绪前不要把「再见」当 pass 路径，也不要声称「再见」可用。`advertised_duplex_level` 仍为 `none`。禁止为赶路演关闭 `reject_non_owner_voice` 或把 guest 升级为 owner。

Demo 收尾与再唤醒（阶段 7 可抄；整段路演剧本仍待阶段 2+3，此处不锁定）：

1. 两轮答完后保持安静；主人静默 `owner_silence_timeout_s=10`。
2. 期望：`owner_silence_timeout` → typed `CONVERSATION_STATE_CLOSED` → Edge `session.close` → 设备 Idle。
3. 再唤醒「茉莉」，确认新 session_epoch 可对话。

`conversation_end_explicit` 仍 pending。本降级不阻塞阶段 2 最小剧本。

### 阶段 6 — 环境与非主人（投资人剧本不依赖则可后置）

安静、电视人声、家庭噪声三种环境：非主人声音不得重置主人静默窗口。裸 VAD/环境声不能续命会话。

通过标准：三种环境各有日志。本阶段失败不回滚阶段 2，但不得宣称「嘈杂也能听」。

### 阶段 7 — 锁定投资人 Demo 剧本

仅当阶段 2 与阶段 3 为 pass，阶段 4 有数字，阶段 5 为 pass 或已降级台词（本剧本尚未锁定）：

1. 小程序展示设备在线（不采集麦克风）。
2. 人在 30–60 cm 说「茉莉」。
3. 欢迎语播完后再问第一句，听完。
4. 再问第二句（建议一句能碰到记忆或身份的，若主人能力 pending 则用「星期几」这类已验证问句）。
5. 按阶段 5 已降级收尾：安静 10s 主人静默 → typed CLOSED → `session.close` → Idle；不要说「再见」。
6. 需要时再唤醒「茉莉」，证明不是一次性会话。
7. 口头说明：这一代是听完再答；抢话要等带 AEC 的下一 SKU。

路演当天禁止改增益、禁止刷未经阶段 2 复验的固件、禁止临场演示打断。

### 阶段 8 — 本工单之后才允许的全双工 SKU（不要提前开工）

阶段 2 未绿之前，固件不得为新板开 overlay。Demo 绿了之后另开候选，不得混进本工单：

1. 采购一块已有小智 AEC 板型的板（立创实战派或 ESP32-S3-BOX-3），现板继续当半双工 demo 机。
2. overlay 新 board；hello 如实报 reference / simultaneous capture。
3. 把 `session_entrypoint.py` 的「凡 device 都半双工」改成按协商 `audio_mode` 决定 `barge_in_enabled`；默认 SKU 仍半双工。
4. 设备路径真正消费播放期采集策略；不要再用只对 H5 有效的 `capture_allowed` 空操作。
5. Edge 声学 registry 登记该 `board_profile`；先 `interrupt_assist`，T1–T14 过了再谈 `full_duplex_verified`。
6. XMOS（ReSpeaker）列为更后的声学 SKU，不与本 demo 抢人。

记忆、主人权限、generation fence、Actual Heard 不因换板重写。

### 完成时如何改本文件

- 阶段 2+3 pass：可把本候选 `verified` 写成带日期的两轮 Actual Heard；仍保持 `full_duplex_verified: false`、`advertised_duplex_level: none`。
- 阶段 4、6 按项补证据日期；未做的保持 pending。阶段 5 已书面降级为超时待命，不得把 `conversation_end_explicit` / 「再见」写成已验证。
- 五项历史验收（设备端对话、再见、10 次唤醒、三环境、天气+星期几）全部自然完成后，才把全局 `direct_real_device_verified` 改为 true。这仍不自动更新 AEC 或全双工。
- 原始 receipt 继续用 `scripts/hardware_realtime_acceptance.py verify`；本工单不要求跑通 T1–T14 双讲格。

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

- Control API：`https://aigcnice.com:8443/memoria-api/`。
- 当前 runtime：`/opt/memoria/current` 原子软链；候选目录：`/opt/memoria/releases/`。
- 已退役 H5：`/memoria-h5` 固定返回 `410 Gone`，不再发布或切流静态前端。
- LiveKit：`livekit/livekit-server:v1.13.5`，Compose project `memoria-livekit`。
- Control API loopback：`127.0.0.1:8791`；legacy mini gateway：`127.0.0.1:8792`；legacy device gateway：`127.0.0.1:8793`；direct Edge device WSS：`127.0.0.1:8794`。
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

Agent-only 快速路径的运行时切片只允许 `services/agent/**`；发布脚本与对应门禁测试可以随发布机制修复，但依赖锁、运行时 Dockerfile、共享包或其他服务变化必须走完整镜像发布：

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
