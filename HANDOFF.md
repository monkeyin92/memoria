# Memoria 当前交接

更新于 2026-09-16。这里只保留当前运行基线、一个紧邻回滚、必要运维步骤和下一验收；完成过程与旧版本流水账已删除。唯一执行队列见 `TODOLIST.md`，后续完成项直接移出队列，不新增归档文档。

本轮核对本地文档、源码、提交及 CI，未连接生产、未打开串口。下列生产/板卡状态均为标注日期的既有收据，不是本轮实时健康证明；操作前须重新核验。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-16
reviewed_source_commit: a8a0e439b68efd4d238b716d9daf4a25824350b9
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
current_work_order: vocat_interrupt_assist
code: partial_candidate_fixes_and_open_review_findings
wired: existing_python_voice_core_and_signed_runtime_profile_authorities
enabled: last_recorded_agent_bridge_d96d4c2_and_board_d1ad38f_not_head
verified: scoped_receipts_only_voice_stability_and_student_device_loop_pending
production_readiness: ready_at_last_observation_not_refreshed_this_review
production_readiness_observed_at: 2026-09-16T11:37:57+08:00
student_safety_loop_verified: false
student_safety_local_scope: sqlite_http_with_manually_injected_signed_runtime_profile
guardian_notification_delivery_channel: absent_outbox_only_status_pending
firmware_playback_supply_meter_verified: short_playback_software_queue_only_not_dma
pre_roll_code: not_implemented
firmware_face_hardware_verified: false
miniprogram_role: control_plane_only
miniprogram_development_version: 0.8.84
realtime_microphone_allowed: false
realtime_tts_playback_allowed: false
realtime_media_wss_allowed: false
livekit_room_allowed: false
offsite_backup_enabled: false
wal_retention_policy: unresolved_no_automatic_pruning_at_last_observation
direct_real_device_verified: false
full_duplex_verified: false
advertised_duplex_level: none
hardware_aec: pending
hardware_double_talk_matrix: pending_real_hardware
T1_T14: 0_pass_14_blocked_0_failed
device_id: dev_atk_a4cb8fd6095c
```

产品不得宣传全双工。小程序仅 profile 页允许经授权有界录制自定义音色样本，不承担实时对话、手机声纹登记或实时媒体回滚。登录账号、当前使用人、说话人确认和敏感授权不得互相替代。

## 最新候选与审查边界

`a8a0e43`（2026-09-16 16:12 CST）是本轮已核对的本地与远端 `main`。CI `35072578098` 的 agent/python 通过；Edge、小程序、固件及镜像任务跳过，不能据此宣称这些制品通过。没有该候选上线的证据；最后记录的线上 Agent/Bridge 仍为 `d96d4c2`。

- 语音：流式 `DoubaoSynthesizeStream._run_attempt` 的活跃分片续期在本地前后对照中有效，但新增测试实际走未修改的 `synthesize_stream_text/_synthesize_once`，父提交也通过。批式路径仍使用总挂钟；部分音频下发后的错误终态仍待闭环。临近静默的 G 时序复现仍关闭会话，不能写成“续问竞态已修”。
- 学生安全：当前使用人接线存在，但 `multi_subject.py` 代新建孩子写双方确认，又直接建立 `verified_via=wechat_identity` 的 active link，缺少与记录相符的验证证据；`interaction.py` 混用使用人类别、账号 consent 和账号记忆，session-policy 与 response-plan 可得出相反结论。最新 Control 候选发布前先处理，见 P0-04。
- 验证边界：学生定向测试 10 项通过，但独立孩子用例仍手工注入签名 RuntimeProfile，不是完整 API→设备取 profile。新 `establish_active_link` 未获专门 PostgreSQL/RLS 契约覆盖；CI 的既有 PG 测试不能替代。
- 发布：Bridge 隐私默认值与隔离进程 canary 的本地检查通过，制品/镜像解析定向测试 9 项通过；标准 Dockerfile/发布流水线尚未调用两个新校验器。镜像解析未指定 expected candidate 时，两服务同指旧镜像仍通过；不能写成发布门禁已闭环。真实镜像与 exporter 的内容不泄露仍待验证。

以上是待修/待验摘要；实现入口、顺序与完成条件只维护在 `TODOLIST.md`，不在此展开修复历史。

## 当前生产与紧邻回滚

### Agent / Voice Core Media Bridge

- 最后收据：2026-09-16 11:36:08 CST，两个容器同镜像、healthy、restart=0。当前 `memoria-agent:20260916-livekit-181-v1`；image `sha256:7033214ddc3a5f4b0f99e016d1edf156b091169d6511ee95139e81f6ec0a9ac4`；revision `d96d4c29b7f13719d052b94647b2c59223ea70a1`。
- 紧邻回滚：`memoria-agent:rollback-20260916-livekit-181-v1-pre`；image `sha256:3f746b4dede8b2f35773ca5b2c6300174a278932eee7d720f4904c00d2b865bb`；revision `1cf8decaee8b28aa73d104f7aea89086e942db66`。本次升级后的实际回滚演练未做。
- 当前为 delta 构建，不是仓库标准全量可复现构建。远端收据 `/opt/memoria/component-releases/20260916-livekit-181-v1/CUTOVER_RESULT.txt`；本地 `outputs/acceptance/run-20260916-livekit-181-deploy/` 内的 `CUTOVER_RESULT.txt`、`report.md` 与切流前后 overrides 均存在，本轮已核收据的 tag/image/revision 与上述记录一致，但未重新核验实时容器；标准全量构建与本次回滚演练仍未完成。
- 独立构建依赖底座（不是第二个业务回滚）：`memoria-agent-runtime-base:uv-c34f031b4a40c7a7-af6e83d18883`，别名 `memoria-agent:20260912-p0-rollback-single-tag-agent-component`；image `sha256:3d46ca183984c2e5e7fd5f06e62b2eac6660049c637d1e9177d5c6874741364a`。删除前必须核依赖，不按旧业务版本误删。
- 11:37:57 CST readiness 的 12 个具名 core 检查与 heartbeat 均 ready。既有 LiveKit smoke 通过；provider smoke 首轮 InterruptSemantic 超时、重试通过。双进程实际隐私环境/exporter 仍待核：Compose 声明或本地 helper 不证明当前容器生效。
- 锁文件：agents/openai/silero `1.8.1`、RTC `1.1.18`、API `1.2.1`、protocol `1.1.26`、local-inference `0.2.7`。研究中的旧 1.6.10 基线不再适用，1.8.2 仅为研究项，不自动再升级。

### Media Edge / Control API

- Edge 当前 `memoria-media-edge:20260908-1600-vocat-interrupt-assist-edge-component`，override `/tmp/media-runtime.override.yml`；回滚 `memoria-media-edge:20260901-0945-wake-word-whitelist`，配置备份 `/tmp/media-runtime.override.yml.pre-20260908-1600-vocat-interrupt-assist`。watermark close-frame 4002 修复仅在代码，未有发布证据；控制帧入队后 read-loop 提前销毁 lane 的通用问题仍待查。
- Control 当前 `memoria-control-api:20260911-subject-switch-device-notify-control-api`；image `sha256:2af29dde2e8c6f1f1781bef322fe3b8d7f3b5ea13d3017b191aa443aa58bcebd`；overlay 源 `b2644124e002d1aac60b1b5a32c336c327af57bc`，仅覆盖 `device_control.py` 与 `multi_subject.py`，不代表当前全量 Control 已部署。
- Control 回滚 `memoria-control-api:rollback-20260911-subject-switch-device-notify-control-api-pre-control`；image `sha256:0b2dbbd8cbb36b06e9d0dd2f13bd62d63cba49ae9d1914d661226b80e0b3d078`。收据 `/opt/memoria/component-releases/20260911-subject-switch-device-notify-control-api/`。
- 主体变更→profile 版本→`runtime_profile.invalidated/apply_at=next_safe_point` 已接代码/线上 overlay；真机换人未验。设备 settings_version=12、`audio_mode=interrupt_assist`；签名 `allowed_barge_in=["button","keyword"]`，不含 voice。播放中语音告别不能按“播后告别通过”外推。

### 其它组件

- SenseVoice：`memoria-sensevoice-asr:20260901-pin-language`，默认 zh、未知语言 415；回滚 `v1` + `/opt/memoria/sidecars/sensevoice-asr/run_sensevoice_asr.py.rollback-20260901-prelang`。Dockerfile 仅在服务器该目录，尚不可由仓库重建；仓内实现使用 sherpa-onnx，不能假定升 FunASR 就能修此服务。
- 对话/分类/深查最后配置分别为 `qwen3.7-flash/qwen-flash/qwen-plus`。声纹 profile `1b5b577b` 为 active，CAM++ anti-spoof 为 unavailable；不宣称主人认证已验证。克隆唤醒听感有旧证据，天气正文音色绑定待听测。
- 小程序仅记录开发版 `0.8.84`（2026-09-06，源 `7e5137a`）；手机/电脑验收、体验版和正式发布均未完成。

## 板卡与固件

当前硬件 ESP-VoCat N32R16，`board=memoria-esp-vocat`；ES7210 双麦 + ES8311 输出，36dB 输入增益。hello 声明 simultaneous capture、software_post_gain_pre_i2s reference，但 `aec_reference_verified=false`。旧 ATK ES8388 已退役。

- 板上源码 `d1ad38fe0d9b36eb9057d8eb97e782b8cc8066d9`；upstream `e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5`；ESP-IDF 6.0.2；overlay `24531273fe03bc72980087efbb92314a95c5b25ec68c2e44eb28ae689a1ebbb3`。
- 当前 app 3,280,832 bytes，SHA256 `7d95c8a1c5319f4200f840ea988e448ca7411e508be19ae2a5dd9db0130ddb65`；ELF `da6ebdd16e4e7436ed1e126a94fad69a9e376faca681f474712743b5b55069f2`。2026-09-15 18:51 CST 匹配启动；仅 app-only `0x20000`。
- 证据目录 `outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/`：`postflash.json` 是不可变刷写收据（其中 `boot_verified=false`）；实际启动独立记录在 `boot-verification.json`，五代短播放与人工听感在 `session-1/audit.json`。不得回改原收据或把短播放软件队列计量称为 DMA/长稳验证。
- 唯一紧邻回滚为该目录 `rollback-app.bin`，3,280,512 bytes，SHA256 `dfba3d619084c6a27b9349b6b230d758238bf289808cefcb848589dca47d581d`；它来自 dirty 构建，以实际二进制为准，不能仅靠旧 HEAD 重建。
- 同目录 `protected/app-before-full-slot.bin` 为写前 `0x20000/0x3f0000` 全槽，SHA256 `cc175040af934575b7804ec01031ebf8543415c98c0084801682e8ce121da333`。身份区 `0x10000..0x1ffff` SHA256 `b7a717fa399ec1390391ca381b9b86c3202035c71695a95e417a4e0f1d084846`；身份/NVS/otadata/bootloader/分区表/assets 均须保护，禁止输出身份内容。
- `pre-roll` 未实现。固件声学、AEC residual、双讲、Exact DAC 与 T1–T14 仍未通过；普通制品清理须在新候选和可运行回滚核验后另行执行，不因精简文档删除实体证据。

## 当前语音缺口与下一验收

原始证据：`outputs/acceptance/run-20260916-p0-03-livekit181-device-acceptance/findings.md` 及同目录五段捕获。八种会话 A–H 均使用上述板卡与 1.8.1 线上版本；原始文件保留，本节纠正其中超出证据的归因。

- H（epoch 1963 / session `de598a18`）同会话完成三天天气→续问→播后告别，但 ACK→正文 `1.969s` 超过 1.5s 门，设备 VAD end→首帧 `4.557s`；A 第二问为 `3.748s`。功能成功不等于时延稳定。
- F 的九天天气 `48.28s`、设备 2413 帧、supply_waits=0，操作员确认完整；C 的 `44.18s` 也完成。B/D 桥侧音频长度 `58.88s/57.46s`，但设备在开始后数秒就停滞，1.5–1.6s 处先有 `366ms/412ms` supply wait，speaking/控制台冻结直到复位。尚无“约 55s 阈值”证据；Edge drop=0 也不能排除 WS writer、网络、接收/解码/播放路径。
- B 在 `20.23s` 挂钟触发旧 TTS 总超时，错误终态又延后约 `38.6s`；D 未见同类 TTS 错误。最新流式修复不能解释两次设备停滞，也未解决部分音频失败后及时 cancel/flush/回 listening。
- G（epoch 1962）ASR final 先于迟到 VAD，受理时静默预算 0，无新 turn，最终 owner_silence_timeout；本轮最新代码对照仍复现。E/F/H 播后告别成功，C 的迟到告别失败；A 无有效告别输入，G 未到告别步骤，不计算“3/5 成功率”。

下一次设备窗口先确认已冻结并实际启用目标候选；执行顺序及修复前置见 P0-03/P0-04/P1-01：

1. 开串口可能复位，先等 `activating→idle` 和心跳再讲话；唤醒词“茉莉”。无人配合或设备未连接时只做离线检查，不自行刷机或播放自动代测。
2. 同一候选重跑三天天气→续问→播后告别，至少三轮；另测 >45s 长答、B/D 同类长答、临近静默续问，以及已下发部分音频后 provider 失败。每项分别判定功能、时延、终态、听感，不跨 release 累加通过数。
3. 同时取 Bridge/Edge WS writer/设备接收、解码、播放消费及任务/锁状态，绑定 session/stream/turn/generation/tool fence。保留 supply/prestart/boundary/close_dropped/outside、delivery ledger、指标差分、PCM RMS/削波；缺观测先补观测，不先加预缓冲或改阈值。
4. 学生危机场景先按 P0-04 的正式 app_confirm 路径确认测试使用人及年龄，核对设备实际取得的签名 profile 与有效监护授权；不手工注入 profile 或绕过声纹/准入门。只由受控成人模拟话术，固定话术逐字交付、终端回执和人工听感、正确主体的通知 outbox/幂等/家长读取同时验；HTTP 文本正确不是设备已说出。通知发送 worker 按用户决定暂缓。

使用新目录，绑定板上收据（不是本次重新回读的证明）：

```bash
uv run --no-project --with pyserial --with esptool scripts/voice_session_capture.py \
  --out "$CAPTURE_DIR" \
  --firmware-receipt outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/postflash.json \
  --server-logs --duration 900
uv run python scripts/voice_session_report.py "$CAPTURE_DIR"
```

`--preflight-only` 不触碰设备；`--boot-reset` 仅在明确需要时使用。捕获必须有结束时间、逐路退出原因、无未解释 serial/cleanup error；缺终态不能报 healthy。Actual Heard 需设备终端证据和用户听感共同确认。

## 屏幕表情：对话脸

当前是 360 圆屏黑底白描 v3；`code/wired/enabled` 有既有证据，`hardware_verified=false`，仍缺待机脸和五表情照片。权威几何在 `memoria_face.cc`；预览 `outputs/firmware-face-v3-20260909/sheet.png` 只作对照。

| 助手实际表达 | 期望表情 | 照片 |
| --- | --- | --- |
| 待机/回复结束 | neutral 月牙眼、短平嘴 | idle.jpg |
| 祝贺、开心 | happy | happy.jpg |
| 关怀 | loving | loving.jpg |
| 遗憾、抱歉 | sad | sad.jpg |
| 惊讶 | surprised 杏仁瞳孔、小 O | surprised.jpg |
| 思考 | thinking | thinking.jpg |

以同代 `assistant_expression→screen.expression` 和串口 `emotion` 为准，不从用户原话猜脸；未知表情回 neutral。照片连同 fence 存入本次 ignored 验收目录。说完/断线/中断清除表情；待机点屏、摇晃不开麦，短拍只短暂惊讶；说话中 BOOT/触摸硬停。不回归项和六张照片均亲眼确认后才签收；回滚只用上节唯一 app，不再保留旧表情专用回滚命令。

## 生产拓扑与安全边界

- `/opt/memoria/current` 最后指向 `/opt/memoria/releases/20260827-architecture-split-v1`；有效栈 `MEMORIA_RELEASE_TAG=20260901-0945-wake-word-whitelist`。目录名、栈 tag、组件 tag 是三个概念；readiness 刷新必须取有效栈配置。
- Control/legacy mini/legacy device/Direct Edge 仅回环端口 `8791/8792/8793/8794`；当前 Bridge 容器 `memoria-voice-core-media-bridge-1`。PostgreSQL 17 + pgvector、MinIO、独立 mTLS Redis；LiveKit server `1.13.5`。SQLite 兼容库 `/data/memoria.sqlite3` 挂载自 `/var/lib/memoria`。
- readiness 入口 `https://aigcnice.com:8443/memoria-api/health/ready`；443 根站是 WMS，不用该端口的 404 判断 Memoria 健康。
- ESP32 Direct：`wss://aigcnice.com:8443/memoria-device-edge/v1/device/media`。公共 8080 不承载设备 WSS。H5 `/memoria-h5` 固定返回 `410 Gone`，不再发布静态前端。
- `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media` 是已退役的原生小程序媒体兼容回滚入口，只能用于明确的 legacy 回滚，不接回小程序产品；443/8443 Nginx 保留以下 include，不改同机 WMS 路由/数据。

```nginx
include /etc/nginx/snippets/memoria-miniprogram-media.conf;
```

Secret 仅在 root-only `/etc/memoria-*.env`（root:root 0600）；候选从真实源复制并按 `scripts/split_production_env.py` 分流，禁止在输出/日志/manifest 留值。内部 token 不等于账号身份；设备/LiveKit token 必须短期且绑定 audience/subject/fence。Direct 缺少 mTLS Device State Redis 时 fail closed，不回退本地权威。

## 发布前门禁

1. 干净 worktree 冻结目标 source/tag，按影响域运行定向/必需门禁；Agent/python CI 通过不代替跳过的镜像、固件和客户端检查。授权/schema/RLS 变动须带真实 `MEMORIA_TEST_POSTGRES_DSN` 跑受影响契约，跳过不算通过。
2. 在候选镜像核依赖版本、双进程隐私默认值与真实 exporter；镜像解析显式给 expected candidate 并对齐有效 overrides。当前自动门禁接线缺口见 P1-01。
3. 冻结 source/images/manifest/verifier 摘要和 OCI revision/role/architecture；现场复核 image ID、软链、有效 env 摘要、数据风险与一个可运行回滚。
4. dry-run→上传校验→授权切流→候选 provider/LiveKit smoke→具名 readiness、外部 Host/SNI 路由、设备和延迟复核；非目标容器/配置不得变化，失败即停止或按授权回滚。

`scripts/deploy_agent_component.sh` 的 source overlay 仅适用 Agent 源码切片；`.dockerignore/pyproject.toml/uv.lock/infra/Dockerfile.agent` 变化必须完整构建，`--allow-scope-drift` 不豁免。不得为行预算顺手修改依赖输入；`check_module_budget.py check` 校验精确行数。切流 Compose 使用 Control 有效栈 tag，不用 OCI revision 或目录名代替。

运行门禁需剥离本地 `LISTENER_CUES_ENABLED/LIVEKIT_ADAPTIVE_INTERRUPTION/OFFLINE_MOCK/INTERRUPTION_MIN_DURATION_S`；`--skip-gates` 必须有明确理由与收据。上传要求 PATH 中 `rsync>=3.0` 支持 `--protect-args`，macOS 内置版本不可假定满足。

## 完整制品上传与校验

构建机生成 portable verifier 和绑定 source/images 的 manifest：

```bash
uv run python scripts/package_release_verifier.py \
  --output "$ARTIFACT_DIR/release-verifier.pyz"
uv run python scripts/create_release_manifest.py \
  --release-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --output "$ARTIFACT_DIR/release-manifest.json"
for artifact in source.tar images.tar release-manifest.json release-verifier.pyz; do
  (cd "$ARTIFACT_DIR" && sha256sum "$artifact")
done
```

`images.tar + images.tar.sha256` 必须成对上传。先 dry-run，再 seeded upload；不要对 basis 使用 `rsync --inplace`，失败不得污染不可变基座。

```bash
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG" --dry-run
scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" --remote memoria-prod \
  --release-tag "$RELEASE_TAG" --base-tag "$BASE_TAG"
```

构建机可信摘要须经已认证运维通道提供；先验 verifier/manifest，再运行 verifier，成功后才解包：

```bash
: "${MEMORIA_RELEASE_VERIFIER_SHA256:?required}"
: "${MEMORIA_RELEASE_MANIFEST_SHA256:?required}"
printf '%s  %s\n' "$MEMORIA_RELEASE_VERIFIER_SHA256" "$UPLOAD_DIR/release-verifier.pyz" | sha256sum -c -
printf '%s  %s\n' "$MEMORIA_RELEASE_MANIFEST_SHA256" "$UPLOAD_DIR/release-manifest.json" | sha256sum -c -
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" --expected-commit "$SOURCE_COMMIT" \
  --verify-imported-images
tar --extract --file "$UPLOAD_DIR/source.tar" --directory "$CANDIDATE_DIR"
```

不允许手工 retag 未绑定 manifest 的模型镜像。切流后运行 `scripts/smoke_server_deployment.sh`、真实 provider smoke、健康/私有 readiness/外部路由和延迟复核。

## 数据层、备份与恢复

仅在需要启动/恢复且获授权时，先创建 runtime 共享网络，再从真实目录启动数据层，避免异地工作目录建错卷：

```bash
DATA_COMPOSE_DIR=/opt/memoria/current/infra
cd /opt/memoria/current
docker compose -f docker-compose.production.yml create --no-build
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" up -d
```

- 用户 2026-09-14 决定验证阶段暂缓自动备份/异地副本；最后核查 offsite profile 未启用、无真实 endpoint/告警。真实家庭数据、正式发布或价值/量级增长前必须重评，不把同机副本称为异地灾备或已验证 PITR。
- 当前保留的本地还原点：`/var/backups/memoria/drill-20260914-p0-02/base`；2026-09-14 通过 `pg_verifybackup`、隔离启动、应用表/对象核对。报告 `outputs/acceptance/run-20260914-p0-02-restore-drill/report.json`。另保留 `/var/backups/memoria/20260912-1150-companion-persona-and-lookup-gate/memoria-archive-20260912T043155Z.dump`。它们不会自动更新。
- WAL 最后观察仍归档且无自动裁剪；旧日增长估计/磁盘余量不是当前值。P1-08 单独处理保留策略，不因备份暂缓而遗漏。禁止只按文件年龄删 WAL，必须保护仍保留 base backup 所需连续链；本轮未删任何数据。
- 重新启用备份时，已修的 pg_basebackup CLI 仍需真实部署验证；网桥复制受现有 pg_hba 限制，优先评估 `network_mode: service:postgres` 走 loopback。真实异地 endpoint/凭据与恢复演练须另行补齐。

恢复集合须含 PostgreSQL base/WAL、MinIO versioned objects、SQLite 兼容快照、root-only env、manifest/回执。用 `scripts/run_offsite_restore_drill.sh` 在隔离环境校验备份、对象清单/哈希、外键和应用读取；不能拿缓存当权威。恢复不可变 evidence/claims 后，在 Control API 镜像中重建投影：

```bash
python -m scripts.rebuild_memory_projections --confirm-rebuild
```

## 回滚与验收底线

服务回滚按最小组件：保留失败候选日志/manifest，恢复切前 image、软链和 env，等待健康与具名 gRPC/readiness，再验外部路由/provider 和设备重连。回滚镜像曾可运行不等于本次回滚演练通过。

固件仅回写唯一紧邻 app 至 `0x20000`；写前备份当前 `0x20000/0x3f0000` 全槽、核摘要，写后回读并比较身份和全部非 app 保护区，再做启动/媒体验收。禁止 `flash.sh` 整包、`erase-all` 或误用旧 run 回滚件。

普通制品仅保留当前+一个可运行紧邻回滚，核验后按授权清理更早普通制品并查磁盘；数据库、WAL、MinIO、安全/合规备份不适用两版本规则。T1–T14 证据写 ignored `outputs/acceptance/`，由 `scripts/hardware_realtime_acceptance.py` 校验。旧 fence 可听输出/写档案、缺播放终态、错误记完成、以发送量伪造 Actual Heard、权威失败回退平行本地实现，任一均拒收。
