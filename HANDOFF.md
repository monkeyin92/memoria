# Memoria 当前交接

本文件只记录当前有效状态、紧邻回滚边界、生产操作和下一验收。历史发布过程不再保留；每次发布原地更新本文件。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-08-27
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
code: complete
wired: esp32_to_go_media_edge_to_python_voice_core_agent
enabled: production_agent_bridge_edge_and_current_firmware_true
verified: production_runtime_provider_model_inference_identity_safe_board_boot_secure_device_onboarding_and_owner_silence_standby
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

当前 Agent/Bridge 发布提交为 `c3fb7fd181794204b97207cfef0015aa0106bde4`（2026-08-28 15:38 CST 切流），Media Edge 发布提交为 `4c3971fef0bfdfc30e9bff742c40ffdd848c0e7c`，Control API 当前部署提交为 `54d9162bba3347fe1e726bbd4e1d614140b4e3fd`：

- Agent 与 Voice Core Media Bridge：`memoria-agent:20260828-1537-device-vad-uplink-deadlock-agent-component`，image `sha256:111713ced6abc3cdbca6a6b3e9065f6b48015f0c33c877685a7c5c580f2ce544`，revision `c3fb7fd181794204b97207cfef0015aa0106bde4`。两个容器 healthy、restart=0、无 error/traceback 日志，容器内代码已核验（新门控逻辑在位、旧丢弃逻辑已移除）。紧邻回滚点 `memoria-agent:rollback-20260828-1537-device-vad-uplink-deadlock-agent-component-pre-agent` 与 `-pre-bridge`。
- Media Edge：`memoria-media-edge:20260825-1730-jasmine-standby-prod-edge-component-v4`，image `sha256:230f94b8e1d0839827b9c5cd3c0c8bbbdf526d7b34e8cf59f3c688f6a71c9513`。
- Control API：`memoria-control-api:20260827-identity-binding-version-v3`，image `sha256:876b0e3e53c7402c7ec87734fb0e48840ff2db0921c94351eb7f27647c54aadf`，revision `54d9162bba3347fe1e726bbd4e1d614140b4e3fd`，容器 healthy。
- 三个目标容器 healthy；Agent/Bridge 切流后 restart count 为 0，目标错误日志为 0。Control API、数据层、LiveKit、Nginx、Edge 和客户端没有随该组件切片重建。
- 2026-08-24 19:16 CST，Qwen Realtime Search、Doubao、FunASR、DeepSeek、Interrupt Semantic 与媒体 fence 生产 smoke 通过。
- 2026-08-28 15:40 CST 切流后复测：Qwen Realtime Search 与 Doubao 稳定 PASS；**FunASR 间歇失败**，4 次中 1 次 PASS，报 `FunASR returned no interim transcript`。定性依据：用切流前镜像 `rollback-…-pre-agent` 做 A/B 对比同样失败；且该 smoke 直接调用 `FunASRSession`，不经过 device VAD 门控或本次改动的任何代码路径。判定为供应商侧抖动，**与本次发布无关**，因此不构成回滚理由（回滚同样失败且会丢失修复）。FunASR 恢复前真实识别率会受影响，需另行跟进供应商。
- **发布链阻断（待处理）**：本轮发布后仓库已整理为单一 `main`，修复的 commit 由 `c3fb7fd` 变为 `bea8bb6`（rebase 到原 `9d81061` 链）。已核对：生产容器内 `services/agent/src/{device_vad,session_entrypoint,duplex_runtime}.py` 与当前 `main` HEAD 的 sha256 **逐字节一致**，即生产已经在跑这份修复，无需重新部署。但生产镜像的 `revision` 标签仍是被 rebase 掉的 `c3fb7fd`，且服务器上现存所有 `memoria-agent` 镜像的 revision（`8f3af6e`、`5be6792`、`dee4264`、`d2fa737`、`f21d521`、`834fdf6`、`18d33b6`、`c3fb7fd`）都不在当前 `main` 的祖先链上。`deploy_agent_component.sh` 硬性校验 `merge-base --is-ancestor base_commit expected_commit`，因此 **Agent component 快速路径当前不可用**。下次需要发布 `services/agent` 变更时，必须先走一次完整镜像发布路径重建可追溯基线；不得手工 retag 现有镜像绕过该校验。
- 普通 Agent component 目录已收敛为当前 `1912` 与紧邻回滚 `1843`；当前回滚标签均解析到 `sha256:a76000fab76397efbc812596819e9bb301e6cf4469f2e0c8c7614f873e7b3789`。
- Agent/Bridge 内 DTLN 完成 ONNX checksum/contract 初始化和 7,680-byte PCM 实推，输出非零；后级固定补偿为 `2.0x`，PCM 转换保持饱和防削波。

本轮真实板卡复现中，天气回答的 generation 1 与 successor generation 2 均取得 `Actual Heard + playback.ended`，此前“说着说着停了”的分段续播问题已在真机关闭；用户随后问“今天星期几”时，多段降噪后 PCM 只有约 `RMS 8–296`，FunASR 没有产生 partial/final，因此没有形成 turn 2；该现象已于 2026-08-28 定位为播放后 VAD 上行门控死锁，根因、修复与未验证边界见「播放后 VAD 上行门控死锁候选」。BOOT 终止事件由用户主动按键触发，属于正常结束，不计为故障。

正常距离识别修复状态：`code=complete`，DTLN 后增加受限 6 dB 补偿，FunASR 明确失败的旧任务会在下一帧前重建并重放有界 PCM，内部边界竞态仍 fail closed；`wired=Agent+Bridge current image`；`enabled=production true`；`verified=2026-08-24 local Agent regression + production provider/media smoke + exact 23-second FunASR idle-timeout recovery`。补偿后的正常距离真实板卡两轮验收仍是 pending，不能据此更新 `direct_real_device_verified`。

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
miniprogram_experience_version: 0.8.73
```

本轮已打通并验证以下顺序：二维码 introspect → BLE Protocomm Security 1（X25519、PoP、AES-256-CTR）→ 设备 online-proof → claim/binding → Activation Manifest → 设备 ACK → `ready_for_conversation`。小程序不采集声纹或实时语音；Wi-Fi 密码只在已认证的 BLE 会话中写入设备，不经过 Control API 日志或小程序普通请求。

设备第一次在绑定完成前收到激活 `409` 时，会继续显示附近配网入口并在后台每 5 秒重试 Activation Manifest；绑定完成后设备拉取清单、返回 ACK，并停止配网二维码入口。相同二维码从新页面重试时复用原 onboarding session，避免误报“设备正在被其他账号设置”。

真实证据：设备 `dev_atk_a4cb8fd6095c`（BLE `MEM-095C`）的最新激活记录为 `activation_version=3`、`status=ready_for_conversation`，`acknowledged_at=2026-08-27 06:34:45.034649+00`（UTC，即 14:34:45 CST）。Nginx 记录设备于 14:34:44 拉取 manifest 200，14:34:45 提交 activation-ack 200；小程序随后显示“在线，可直接对话”和“激活状态：可开始对话”。

这证明了服务端和控制面激活闭环，不等于真实语音对话、AEC、双讲、连续轮次或完整屏幕物理显示验收。当前已通过串口确认固件 2.4.2、Wi-Fi、Manifest v3 和稳定运行；板屏是否已由二维码切换到正常界面尚未单独留存最新照片，不能用小程序页面替代该物理证据。

## 当前板卡与固件

- 固件 app version：2.4.2；ES8388 输入增益：18 dB。
- app SHA-256：`f15a3b356f3eed604673da1f08afc784832be4e36fa44642ac9dc8d359d03115`。
- merged SHA-256：`73a92c2dcda5be860761e40ccad3bca361db8da40e0f98ce02f36e0062ec9419`。
- overlay SHA-256：`4df9c49cf823cc07e1b6e13a4c3cb4c6efd7b684370e8ba8baaace7c25ab0ca0`。
- `memoria_identity` 刷前/刷后 SHA-256：`b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`，逐字节一致。
- 本轮使用 `scripts/flash.sh --port /dev/cu.usbmodem101` 写入 bootloader、partition table、OTA data 和 app，未写入 `0x10000..0x1ffff` 身份区；串口确认 Wi-Fi、Activation Manifest activation_version=3、idle、1MIC/0 playback AFE 与 KWS 初始化，无 brownout 或重启循环。
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
owner_silence_timeout_s: 10
```

固件只在本地 KWS 用 `mo li` 唤醒，仍不上传唤醒词音频。设备会话中的“再见”“知道了”“退下吧”等精确结束语只有通过目标说话人权威判定后才关闭；带后续内容的句子不会误触发。无主人语音计时只由 Python Voice Core 的权威会话状态管理：助手输出和传输断开期间暂停，回到聆听时开启 10 秒窗口，裸 VAD/环境声不能重置主人计时。关闭通过 typed `CONVERSATION_STATE_CLOSED` 进入 Go Media Edge，再下发设备 `session.close` 回到 Idle，不新增第二套聆听状态机。

当前候选已发布到生产 Agent/Bridge/Edge 并写入当前板卡。2026-08-25 真机已验证“茉莉”唤醒后静默约 10 秒，Python 产生 `owner_silence_timeout` typed CLOSED，Edge 成功排队 `session.close`，设备回到待命；Edge 也已增加旧 FLOOR fence 丢弃和重复 CLOSED 幂等保护。明确结束语测试时，ASR 路由进入结束语分支，但正式主人权限返回 `subject_capability_forbidden`，记录为 `conversation_end_owner_unverified` 并由随后超时关闭，因此 `conversation_end_explicit` 仍待主人声纹/subject profile 权限就绪后复测。两音节“茉莉”相较原四音节唤醒词有更高误唤醒风险，安静、电视人声和家庭噪声三种环境的阈值验收仍未完成，不能更新 `direct_real_device_verified`。

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

未验证：真实设备连续多轮、欢迎语后立即提问、播放尾音误触发率仍需板卡证据，不得据此更新 `direct_real_device_verified`。切流后的 provider smoke 中 FunASR 间歇失败已判定为供应商侧问题（见「当前生产」），FunASR 恢复前无法用真机区分“死锁已修复”与“供应商识别不出”。

## 下一轮真实设备验收

设备启用控制面已经闭环；当前只补用户发起的真实对话、主人权限和环境验收，期间不要按 BOOT/RESET。按顺序只做以下验收：

1. 从小程序设备页返回首页，实际发起一次对话，记录设备串口、Edge/Bridge/Agent 日志和用户是否听到完整回答；激活成功本身不替代这项验证。
2. 完成当前账户的主人声纹/subject profile capability 初始化后，唤醒并说“再见”，确认 `conversation_end_explicit`、typed CLOSED、设备 `session.close` 并回到 Idle；随后再次说“茉莉”确认可开启新会话。
3. 待机状态下以正常 30–60 cm、正常音量说“茉莉”，重复 10 次记录漏唤醒/误唤醒。
4. 在安静、电视人声和家庭噪声三种环境测试，确认非主人声音不重置主人静默窗口。
5. 最后连续问“今天天气怎么样”和“今天星期几”，确认正常距离识别、回答与自然播放仍然成立。

验收需按同一候选收集：设备串口、Edge/Bridge/Agent 日志、session/stream/turn/generation fence、speaker authority、ASR final、首个 0/0 下行帧、`playback.started/progress/ended/error`、typed CLOSED、设备 `session.close`、WSS close cause，以及用户听到的内容。五项都自然完成后才可更新该候选的 `direct_real_device_verified`；这仍不自动更新 AEC、双讲或 `full_duplex_verified`。

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

- H5：`https://aigcnice.com:8443/`；Control API：同源 `/memoria-api/`。
- 当前 runtime：`/opt/memoria/current` 原子软链；候选目录：`/opt/memoria/releases/`。
- 当前 H5：`/var/www/memoria-h5` 原子软链；候选目录：`/var/www/memoria-releases/`。
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
2. 运行 Python、H5、小程序、Go、契约、镜像和 `git diff --check` 门禁；供应商与 LiveKit smoke 必须使用候选容器。
3. 冻结 source、images、H5、manifest、verifier 的 SHA-256；验证 OCI revision/role/architecture。
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

构建机先生成 portable verifier 和 manifest；manifest 必须显式绑定三个主工件：

```bash
uv run python scripts/package_release_verifier.py \
  --output "$ARTIFACT_DIR/release-verifier.pyz"

uv run python scripts/create_release_manifest.py \
  --release-tag "$RELEASE_TAG" \
  --expected-commit "$SOURCE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --h5-artifact "$ARTIFACT_DIR/h5.tar.gz" \
  --output "$ARTIFACT_DIR/release-manifest.json"
```

`images.tar + images.tar.sha256` 必须成对上传。构建机在工件目录内校验，避免把目录前缀重复拼接：

```bash
for artifact in source.tar images.tar h5.tar.gz release-manifest.json release-verifier.pyz; do
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
  --source-archive "$UPLOAD_DIR/source.tar" \
  --images-archive "$UPLOAD_DIR/images.tar" \
  --h5-artifact "$UPLOAD_DIR/h5.tar.gz" \
  --verify-imported-images

tar --extract --file "$UPLOAD_DIR/source.tar" --directory "$CANDIDATE_DIR"
```

不允许手工 retag 缺少 manifest 绑定的模型镜像。切流后运行 `scripts/smoke_server_deployment.sh`、provider smoke、容器健康、私有 readiness、外部 Host/SNI 路由和延迟复核。

旧匿名 H5 用户的迁移窗口必须按既定截止执行：新 H5 验收后，原定窗口保留到绝对截止，不因发布提前删除兼容入口。

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
