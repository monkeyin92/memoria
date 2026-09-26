# Memoria 当前交接

## 当前生产快照

- **最近生产收据**：2026-09-26 16:37–16:44（CST）整栈发布 `20260926-edge-flush-v1`（main `fa8a97d`，含 PR #49/#50），5 个角色经仓库版 `release-ops.sh` 全链 PASS，media-edge 单独切换 PASS。详见下方同日发布一节。

| component | actual image/tag | OCI digest | revision | frozen runtime identity | health | restarts | startup time | receipt | rollback target |
|---|---|---|---|---|---|---:|---|---|---|
| Control API | `memoria-control-api:20260926-edge-flush-v1` | `sha256:42f8cbcad8e76aad8e9b5ed9bee6eade3e4c433d1535bf215439b60e928e3fd8`（服务器 image id） | `fa8a97d51ca799b53e09e014dd69b78b0f4e18ab` | `20260926-edge-flush-v1` / `fa8a97d` | healthy | 0 | `2026-09-26T08:41:21Z` | `/opt/memoria/releases/20260926-edge-flush-v1/.cutover/` | `memoria-control-api:rollback-20260926-edge-flush-v1-pre`（= `20260926-persona-subject-v1`） |
| Agent / Bridge | `memoria-agent:20260926-edge-flush-v1` | `sha256:65fe7c30f6c3e0edf61ded34d4d4549dee49811bea0d57dec24baa4300e24ac5`（服务器 image id） | `fa8a97d51ca799b53e09e014dd69b78b0f4e18ab` | `20260926-edge-flush-v1` / `fa8a97d` | healthy | 0 each | `2026-09-26T08:41:42Z` | 同上 | `memoria-agent:rollback-20260926-edge-flush-v1-pre`（= `20260926-persona-subject-v1`） |
| Device Media Gateway / Miniprogram Gateway / Speaker Model | `memoria-{device-media-gateway,miniprogram-gateway,speaker-model}:20260926-edge-flush-v1` | `sha256:d6588a67…` / `sha256:aa2b3cdd…` / `sha256:b0d82847…`（服务器 image id） | `fa8a97d51ca799b53e09e014dd69b78b0f4e18ab` | `20260926-edge-flush-v1` / `fa8a97d` | healthy | 0 each | `2026-09-26T08:41:04Z`–`08:42:08Z` | 同上 | 各自 `rollback-20260926-edge-flush-v1-pre`（= `20260926-persona-subject-v1`） |
| Media Edge | `memoria-media-edge:20260926-edge-flush-v1` | `sha256:5a905f03a649c3799a2f0fc73b660d81c6dc5a76ff20bdfad1058892367f47ab`（服务器 image id） | `fa8a97d51ca799b53e09e014dd69b78b0f4e18ab` | `not set / not applicable` | healthy | 0 | `2026-09-26T08:44:11Z` | `/opt/memoria/releases/20260926-edge-flush-v1/.cutover/media-edge-pre-state.txt` | `memoria-media-edge:20260926-persona-subject-v1`（override `component-releases/20260926-edge-flush-v1/media-edge-rollback.override.yml`） |

- **候选可见性状态**：已随整栈发布上线（契约提交在 main 上为 `0059368`，早期记录中的 `f7c4c2a` 是合并前哈希）。普通 search/context 只返回 confirmed 且无 active 冲突，`include_candidates=true` 仅供审核与评测。真实 PG 上的 candidate 行为与线上带鉴权读口尚无单独收据。
- **评测基线边界**：四份 2026-09-23 评测收据统一为 `receipt_scope=parent_baseline`、`source_commit=3ccba9c69a77d3bc97f3a48e5f702ab0a4da7948`；它们产生于候选可见性提交之前，只证明上线前基线，不证明当前线上版本的召回质量。
- **发布身份**：`20260926-edge-flush-v1` / `fa8a97d`（control-api env、compose 插值、readiness 一致）；`/opt/memoria/current` → `releases/20260926-edge-flush-v1`。上一栈 `20260926-persona-subject-v1` / `63cf5f8` 为回滚目标。
- **未关闭缺陷**：P0-03 仍开放（缺陷 A 核心续问边界与工具查询最终回答已在 09-24、09-25 真机走通；TLS/WSS 自动重连保留观察项）；缺陷 B 的输入电平摆动/近讲削波仍需固件 AGC/AEC；F2 禁止源 barge 尚未取得设备旁的真实复现证据。
- **下一步必须动作**：真机窗口按 `20260926-persona-subject-v1` 一节的验收清单执行（⓪ 先重新绑定并勾选长期记忆），并加验本次 media-edge 修复：终止性拒绝后设备显示错误且不再续连；再补 TLS/WSS 重连观察与 P0-03 剩余设备矩阵；`direct_real_device_verified=false`、`full_duplex_verified=false` 保持不变。
- **固定参考**：[发布、恢复与回滚运维手册](docs/runbooks/release-rollback.md)、[空间治理运维基线](docs/runbooks/operations-space-governance.md)、[删除域与 seal 契约](docs/compliance/delete-domains.md)、[2026-09-20 及更早历史归档](docs/HANDOFF-archive-before-0920.md)、[2026-09-16 至 2026-09-23 历史归档](docs/HANDOFF-archive-0916-0923.md)。

## 2026-09-26 整栈发布 20260926-edge-flush-v1

- **范围**：tag `20260926-edge-flush-v1` → main `fa8a97d`（PR #49 发布脚本跟上线上链、archive 库存储共享连接池；PR #50 media-edge 关闭前送达 `session.error`、Opus 编解码器加锁、readiness 报告刷新逾期）。无 schema、compose、env 变更。合并后 main CI（run `36229760442`）全绿后才切流。6 个镜像本机 linux/amd64 全量构建，revision/version 标签核对无误；seeded 上传（基座 `20260926-persona-subject-v1`，实传 177 MB）双端校验 PASS；media-edge 镜像单独 scp、校验和核对后导入。
- **发布脚本**：首次使用仓库版 `scripts/release_ops.sh`（sha256 `7f68cfad…`，`PREV_TAG=20260926-persona-subject-v1`），旧版备份为 `/root/memoria-release/release-ops.sh.pre-20260926-edge-flush-v1`。
- **五角色**：`verify-load` PASS → `freeze` PASS（新版对 6 个目标容器的整栈 compose 链与 `current` 校验通过；回滚标签、pre-state、env 备份、`pg_dump`、SQLite 备份在 `.cutover/`）→ `env` PASS（env 键无变化；`validate_production`、`verify_env`、agent 与 bridge 声纹开关断言、真实 provider smoke `FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic`）→ `schema` PASS（无文件变更，权威库契约校验通过）→ `cutover` PASS（16:41:04–16:42:14，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，所有带健康检查的容器 healthy、restarts=0）。
- **media-edge（单独切换）**：新链为新发布树 compose + `component-releases/20260926-edge-flush-v1/media-edge-component.override.yml`；新旧渲染配置除 build context 路径外完全一致（设备 WSS 开启、WebRTC 关闭、`python_authoritative`、端口仅 `127.0.0.1:8794`）。切换后 healthy、restarts=0，设备 WSS 监听启动；bridge 7001 上有 1 条已建立连接；公网 `memoria-device-edge` 未带凭证返回 401。
- **上线后实测**：`/health/ready` 带 `smokes: "passed"`、`warnings: []`（P1-09 字段生效）；PostgreSQL 客户端连接 `memoria_app` 为 1 条（发布前 5 条空闲，archive 库 10 个存储共享一个池）；control-api 与 agent 启动 5 分钟内无 error/traceback。设备当时空闲，终止性拒绝的设备行为未观察到，列入真机验收。
- **回滚**（未实跑）：`TAG=20260926-edge-flush-v1 COMMIT=fa8a97d… release-ops.sh rollback` 把 6 个角色按 `20260926-persona-subject-v1` 整栈 compose 重建并把 `current` 指回；回滚目标已按使用人建人格索引，无需人格守卫。media-edge 回滚：同一发布树命令改用 `media-edge-rollback.override.yml`。schema 不回滚。
- **本地制品**：`outputs/release/20260926-edge-flush-v1/` 与 `outputs/release/20260926-edge-flush-media-edge/`（ignored，约 3 GB），验收结束后可删除。

## 2026-09-26 整栈发布 20260926-persona-subject-v1

- **范围**：tag `20260926-persona-subject-v1` → main `63cf5f8`（PR #42 减法整理、#44 发布脚本入库、#45 人格按使用人学习/监护小结随长期记忆/导出含人格/死链路删除）。6 个镜像在本机按 linux/amd64 全量构建（5 个角色 + media-edge），镜像 revision/version/role 标签核对无误；制品清单与源码归档经 `verify_release_source`、`create_release_manifest` 生成，seeded 上传（以 `20260925-full-stack-v1` 为基座，实传 177 MB）双端校验 PASS。发布脚本以仓库版本安装到 `/root/memoria-release/release-ops.sh`（旧版备份为 `release-ops.sh.pre-20260926-persona-subject-v1`）。
- **五角色（`release-ops.sh`，按步 fail-closed）**：`verify-load` PASS（摘要校验、导入前后 verifier、部署冒烟）→ `freeze` PASS（6 个目标容器链与 `current` 全部匹配；回滚标签、pre-state、env 备份、`pg_dump`、SQLite 备份在 `.cutover/`）→ `env` PASS（env 键无变化；候选 `validate_production`、`verify_env`；agent 与 bridge 声纹开关断言通过；真实 provider smoke `FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic` PASS）→ `schema` PASS（清单内文件无变化，权威库契约校验通过）→ `cutover` PASS（speaker-model → control-api → agent+bridge → 两个网关，约 60 秒，全部 healthy）→ `finish` PASS（readiness `ready` 且为新 tag，定时刷新 `Result=success`，外部 8443 就绪 200，所有带健康检查的容器 healthy、restarts=0）。
- **人格表迁移（control-api 启动时自动执行）**：三张表 `subject_id` 非空、`FORCE ROW LEVEL SECURITY` 已恢复、现有 5 条特征全部回填为账号本人，新唯一约束 `persona_traits_account_subject_key`、`persona_versions_account_subject_version_key`、`speech_style_stats_pkey(account_id, subject_id, scene)` 就位；control-api 启动日志无错误。
- **media-edge（单独切换）**：镜像校验和与标签核对后导入；新链为新发布树 compose + `component-releases/20260926-persona-subject-v1/media-edge-component.override.yml`，不再包含 `/tmp/media-runtime.override.yml`（原文件仍在主机上，并备份在 `.cutover/`）。渲染配置核对：设备 WSS 开启、WebRTC 关闭、`python_authoritative`、端口仅 `127.0.0.1:8794`。切换后 healthy、restarts=0，设备 WSS 监听启动；公网 `memoria-device-edge` 入口未带凭证返回 401（路由到新 edge）。bridge 重启期间 media-edge 原有 gRPC 通道自动恢复（bridge 7001 上有来自 media-edge 的已建立连接）。设备当时空闲、无会话，设备重连未观察到，列入真机验收。
- **回滚**（未实跑）：五角色 `release-ops.sh rollback`（control-api 回 mascot-sync 组件链，其余回整栈镜像，`current` 指回整栈树）；若已有孩子/老人的生效人格版本，脚本默认拒绝，`ROLLBACK_SUPERSEDE_SUBJECT_PERSONA=1` 先将其标为已取代。回滚后旧代码写入人格会因唯一约束已替换而报错（后台捕获），人格读取不受影响。media-edge 回滚：同一命令改用 `media-edge-rollback.override.yml`。schema 不回滚。
- **真机验收清单（待执行）**：⓪ 先重新绑定设备并勾选长期记忆——2026-09-26 只读核对线上最近三天没有任何 `speech.utterance_finalized`，2336 条证据全部无主体；现有测试绑定早于绑定授权，设备不会把说话人认作绑定使用人，对话不入档，人格、监护小结、按使用人删除都没有数据来源；① 设备唤醒后连上新 media-edge 并完成一轮对话；② 孩子绑定的设备在家长页重开"长期记忆"开关（存量绑定不会自动获得监护小结授予），监护小结出现且只含孩子的聚合记录；③ 隔天孩子的回复体现自己的表达风格，账号本人人格不串入；④ 按孩子导出含人格计数、不含描述；⑤ P0-03 剩余矩阵与 TLS/WSS 重连观察。
- **本地制品**：`outputs/release/20260926-persona-subject-v1/`（ignored，约 3 GB），验收结束后可删除。

## 2026-09-26 减法整理（PR #42，已随 20260926-persona-subject-v1 上线）

- **范围**：main `00bb4b2`（PR #42 squash），净删约 3.09 万行，线上链路行为不变。删除无消费者的多主体 Go/TS/固件生成契约、Go 侧 WebRTC/WHIP 终端与 Go-shadow 会话 actor、Python 侧 shadow 协商与观察流、agent 中只被自身测试引用的 4 个模块；`packages/proto` 未改。删除前只读核对线上：media-edge `MEDIA_EDGE_INTERACTION_AUTHORITY=python_authoritative`、`MEDIA_EDGE_WEBRTC_ENABLED=false`，bridge `MEDIA_BRIDGE_GO_SHADOW_ENABLED=false`。
- **护栏**：行数预算覆盖全部 35 个超过 1,500 行的源模块（只降不升）；`tests/test_service_layering.py` 冻结 `services/` 跨包依赖图。
- **下次 media-edge 发布须知**：新镜像在 `MEDIA_EDGE_WEBRTC_ENABLED=true`、`go_shadow`/`go_authoritative` 或生产未开设备 WSS 时拒绝启动；仓库 compose 已固定 `MEDIA_EDGE_DEVICE_WSS_ENABLED: "true"`，发布后可把 `/tmp/media-runtime.override.yml` 移出 compose 链。`split_production_env.py` 接受但不再分发已退役的 shadow/WebRTC 变量，现有 env 文件无需改动。
- **固件**：补丁 `0025-playback-underrun-metering` 改为 `0026`、吉祥物补丁改为 `0027`，应用顺序不变；overlay 哈希变化，下次构建需重新 bootstrap upstream 缓存。
- **文档**：09-16 至 09-23 的过时章节原样迁入 `docs/HANDOFF-archive-0916-0923.md`。
- **验证**：本地 ruff、strict mypy、行数预算、完整 pytest（无失败）、Go vet/test/race、proto 可复现、媒体冒烟/回放、离线 E2E、小程序与固件主机测试通过；远端 CI 9 项全绿（python 首跑因 buf 下载断连失败，重跑通过）。未部署，未做设备验收。

## 2026-09-25 control-api 发布 20260925-device-mascot-sync（设备屏伙伴同步）

- **范围**：tag `20260925-device-mascot-sync` → main `9f02619`（PR #40 合并提交）。只动 control-api：`services/control_api/*` 内的选伙伴→设备人格同步（`companion_device_sync.py`、`routes/memory.py`、`routes/persona_assignment.py`）与设备签名只读接口 `GET /v1/devices/{id}/display-profile`（`device_display_profile.py`、`routes/device_onboarding.py`）。共享代码 `services/device_fleet`、`services/common` 未改，依赖输入未变，无 schema/nginx 变更。
- **执行**：`deploy_control_component.sh` dry-run PASS → `--cutover` PASS（候选镜像 build + artifact verify、权威 PG schema/RLS 校验、`resolve_target_images` 解析到候选、单容器重建）。基座 `memoria-control-api:20260925-full-stack-v1`（`sha256:de491809…`，revision `064ed61`）。
- **切后**：control-api healthy、restarts=0、StartedAt `2026-09-25T15:54:06Z`；agent、bridge、两个 gateway、media-edge 的 uptime 未变；外部 `https://aigcnice.com:8443/memoria-api/health/ready` 200；新接口无证书请求返回 401 `device_certificate_required`（符合契约）。
- **真机**：开发板（固件含伙伴吉祥物与 display-profile 轮询，见「屏幕：伙伴吉祥物」一节）在发布后首次空闲轮询即从星澜切到账号所选桃喜；复位后开机直接显示桃喜，激活后 1 s 轮询 `Display profile companion=taoxi version=f11c258e317b23d9`，此后每 20 s 一次 HTTPS 200、版本不变不再打日志。
- **小程序**：体验版 `0.2.20260925.3`（main `9f02619`，Node 24.16.0 上传；Node 25 不受 miniprogram-ci 支持）已上传，提交审核/正式发布需在公众平台手动完成。
- **回滚**：切回 `memoria-control-api:rollback-20260925-device-mascot-sync-pre-control`（即 full-stack-v1 镜像）；接口与同步均为增量，旧固件忽略，回滚无数据迁移。未实跑。

## 2026-09-25 整栈发布 20260925-full-stack-v1（保留豆包 TTS）

- **范围**：tag `20260925-full-stack-v1` → main `064ed61`（含 #33–#38：小程序吉祥物与回顾门禁、binding 查询、Runtime Profile 原地续期、确认唯一绑定主体、动作时入口、runbook 修正、Qwen-Audio TTS 回退）。5 个角色镜像（agent、control-api、device-media-gateway、miniprogram-gateway、speaker-model）在本机 linux/amd64 全量构建约 15 分钟；media-edge（代码未变）、postgres、minio、redis、livekit、sensevoice 未重建。
- **发布前**：生产库导出恢复到隔离临时容器演练 main 的升级：升级前 verify 失败（缺新角色，预期）、升级后 verify 通过、重复执行幂等、133→134 表、关键表行数一致，临时容器与导出已删。
- **执行**（`/root/memoria-release/release-ops.sh`，按步 fail-closed）：上传 seeded（实传 423MB）并双端校验 → verifier 导入前后均通过、`smoke_server_deployment.sh` PASS → 冻结（回滚 tag、pre-state、env 备份、`pg_dump`、SQLite 备份，均在 `.cutover/`）→ env：只新增 `MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD` 与 `MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`（服务器生成、未输出），control env 身份改为新 tag/commit；候选镜像内 `validate_production`、`verify_env`、真实 provider smoke（`FunASR, QwenRealtimeSearch, Qwen, Doubao, InterruptSemantic`）均 PASS → schema：原地 `cp` 5 个变更文件（备份在 `releases/20260827-architecture-split-v1/.pre-20260925-full-stack-v1-schema-backup/`），升级 + verify PASS，旧 control-api 全程 healthy → 分批切换 speaker-model → control-api → agent+bridge → 两个网关，约 70 秒，全部 healthy、restarts=0 → `current` 切换、`refresh_readiness.sh` PASS、systemd 定时单元 `Result=success`、外部就绪 200。非目标容器 StartedAt 与切前一致；media-edge 在 bridge 重启时断开设备会话，11:30:56 重连新 bridge 成功。
- **小程序复核**（开发者工具 + 生产）：runtime-profile 同会话续期至 epoch 5，`confirmed`/`adult_companion`；首页在线、设备页当前使用者「主人」无降级、回顾无报错；「我的」数字分身/原始语音/声音复刻入口开放，监护小结关闭。体验版 `0.2.20260925.2` 已上传（提交审核/正式发布需在公众平台手动完成）。
- **真机验收（20:31–20:35，Mac 扬声器播放 Tingting 语音代替用户，串口 + bridge 日志取证，收据 `outputs/acceptance/run-20260925-full-stack-device/`）**：设备开串口复位后在新 control-api 上激活成功。首轮问候被丢弃，原因 `no_reply reason=target_non_owner`：生产 `/etc/memoria-agent.env` 仍是 `MEMORIA_SPEAKER_AUTHORITY_ENABLED=true`（P1-11 模板与代码默认都是 false，发布脚本漏查）。已改为 false（原文件在 `.cutover/`），同镜像重建 agent+bridge，healthy、readiness 仍 ready。复测：唤醒应答 → 「你是谁」正常回答（4.7 s，首帧前 4.5 s）→ 南京天气走工具（先确认 2.9 s，realtime search 113 字，正文 21.5 s）→「那明天呢」追问走工具（搜索仅 18 字，正文 3.5 s）→「再见」按 `conversation_end_explicit` 不回复并回到待命，与 09-21/09-24 行为一致。每轮均有 `turn_committed → first_frame_sent → provider_completed → actual_heard → playback_ended`。`sensevoice rescue request failed` 与空文本 `ASR tail timeout` 丢弃在 09-24 旧版同样存在，非本次回归。Mac 音量已恢复（13、静音）。麦克风转写不可用（机器人音量低），只作观察。
- **待办**：① 用户本人听一次豆包声音；② 用户重新绑定设备以写入绑定时 consent grant，再验证 `memory_recall_private`（另需设备证书/attestation 有效）；③ 监护小结 consent 写入方已于 2026-09-26 在代码中补上（随长期记忆授予，见 TODOLIST P1-11，未部署）；④ media-edge 对易失 `/tmp/media-runtime.override.yml` 的依赖已随 2026-09-26 发布解除（新链不含该文件）；⑤ `release-ops.sh` 的两处缺陷（`finish` 状态循环遇无 healthcheck 容器中断、env 步骤不断言声纹开关）已于 2026-09-26 在仓库 `scripts/release_ops.sh` 修复并补回归，服务器副本待下次整栈发布前更新线上链常量后替换；⑥ 旧 20260827 树仍被数据层 bind mount，不可清理。
- **回滚**：`release-ops.sh rollback`（先恢复 env，再按原链重建各服务、`current` 指回 20260827 树）；schema 不回滚（向前兼容）。未实跑。

## 2026-09-25 main 回退 Qwen-Audio TTS 迁移（生产暂留豆包）

- **产品决定（用户 2026-09-25）**：生产暂留 Doubao TTS，Qwen-Audio 3.1 TTS 迁移（PR #28 的 `457d669`、`d6bd536`）回退待重新评估；重新启用 = revert 回退提交。分支 `revert/keep-doubao-tts`（未合并、未发布）。
- **影响**：main 整栈发布不再切换 TTS，Agent/Control 的 TTS 与复刻配置和线上 `3eede2f`/`d522426` 一致，现有 CosyVoice/豆包复刻继续可用、无 `reenrollment_required`；整栈发布仍需 schema 升级与两个 memory maintenance secret（见下节「暂缓的整栈发布」）。

## 2026-09-25 控制面确认一对一绑定的唯一主体上线

- **产品决定（用户 2026-09-25）**：小程序控制会话直接确认一对一绑定的唯一使用人，不再停在 `unconfirmed`/`unknown_safe`。
- **实现**：`sole_bound_subject_id()`（恰好一个 primary subject 且非 `family_shared`）；控制会话 start 与 renew 时确认，事件记 `reason_code=sole_bound_subject`（不冒充 `app_confirm` 或声纹）。确认他人（孩子/老人）须 binding 上有 active 关系（main：`guardian_of`/`delegate_for`；hotfix 线仅 `guardian_of`），并要求与 app 显式确认同样的 subject-switch 权限，否则保持未确认。app 路径不设 `device_bound`：监护人的 app 会话不等于孩子本人，监护人/子女拿不到对方私人记忆。能力仍全部由 policy/consent 决定，无 contract/schema 变更。
- **发布**：tag `20260925-confirm-bound-subject` → `d522426`（hotfix 线 = `85aa729` + 1 提交，control_api+session_runtime 真实 PG 821 passed）；dry-run PASS → cutover PASS，healthy、restarts=0。
- **线上复核**：同一控制会话续期到 epoch 3，`speaker_state=confirmed`、`service_mode=adult_companion`，能力 `chat`/`tutor`/`english_practice`，客户端校验 valid。
- **门禁入口仍不会打开**：`memory_recall_private` 需要已验证成人 + 该 binding 的记忆 consent grant + 设备证书/attestation 有效；hotfix 线没有 main 的 consent grant 写入（`services/consent/bound_subject.py` 与其 SQL），线上无 grant，需整栈发布 + consent schema 升级 + 存量 binding 回填。`guardian_summary_view` 需 active `guardian_of` + 对应 consent，两条线都没有写入方。`digital_self_preview`/`raw_audio_retention`/`voice_profile_create`/`voice_clone_use` 属于动作时决策（`PROFILE_ISSUE_DEFERRED_CAPABILITIES`），永远不出现在 runtime profile，小程序这几个入口按 profile 能力门禁的写法需要改。
- **hotfix 线既有缺陷（未修）**：binding 上存在 receipt 不依赖的 active 关系时，profile 签发 503 `current exact policy fence is invalid`；main 已在 `_PostgresIdentityRelationshipAuthority` 修复，线上当前未命中。

## 2026-09-25 Control API Runtime Profile 原地续期上线

- **缺陷**：binding 查询修好后，线上 runtime-profile 变为 403 `runtime_profile_rejected`。固定控制会话 `device-control:{device_id}` 的 profile TTL 5 分钟，Postgres 控制面没有续期路径（`current()` 过期即 `PersistentSessionDenied`，session_id 是主键不能复用），首个 5 分钟后永久 403。
- **修复**（main 同改动见 `fix/runtime-profile-renewal` PR）：`PostgresSessionRuntimeService.renew()` 复用 subject switch 的 rotation（重锁 binding、重读设备信任、重跑 policy/consent、新签 profile、`session_epoch`+1、`profile_rotated` outbox），CAS 保证并发只产生一个 live profile；过期不携带已确认主体。控制会话改为 `device-control:{device_id}:{binding_id}:{sha256(actor)[:16]}`，换绑后走新会话，旧会话按 superseded 严格拒绝；只有 `device-control:` 会话续期，设备媒体会话保持严格过期。路由拒绝时记录原因并返回固定 reason code（不透出 SQL）。无 schema 变更。
- **发布工具**：`deploy_control_component.sh` 放行 `services/session_runtime/*`（只在 Control 进程内运行，其他镜像不 import），仍拒绝 `services/session_runtime/*.sql`。
- **发布**：tag `20260925-runtime-profile-renewal` → `85aa729`（分支 `hotfix/20260925-runtime-profile-renewal` = `e6ab586` + 3 提交）；本地真实 PG 下 control_api + session_runtime 813 passed；dry-run PASS → cutover PASS，healthy、restarts=0，Agent/Bridge 等未动。
- **线上复核**：带登录读取 runtime-profile 200、客户端校验 valid，epoch 1、TTL 5 分钟；过期后（09:48:08Z）同一 session 续期为 epoch 2、新 TTL，5 秒后复读复用同一 profile。
- **仍开放**：一对一设备的 app 侧 profile 为 `unconfirmed`/`unknown_safe`，只有 `chat`/`english_practice`；数字分身、监护人摘要、隐私等门禁入口因此显示「尚未开放」，需要产品决定是否由控制面直接确认唯一绑定使用人。
- **暂缓的整栈发布**：main 整栈发布会把 TTS 从 Doubao 切到 Qwen-Audio（PR #28，无设备验收），并需要 schema 升级（`guardian_push_subscriptions` 等）与两个新 secret（`MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD`、`MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL`）；用户 2026-09-25 选择先做 control-api hotfix，整栈待 TTS 设备验收后。

## 2026-09-25 Control API binding 查询 hotfix 上线 + 小程序 0.2.20260925 体验版

- **缺陷**：`PostgresMultiSubjectRuntimeControl.active_manifest` 查 binding 不带 `actor_person_id`；`identity_device_bindings` 按操作账号 FORCE RLS，生产因此对 `GET /v1/devices/{id}/runtime-profile`、resolve-subject、人格分配、memory-scope 一律 404 `binding_not_found`（自 `14b61c0`，2026-08-11）。测试未发现是因为 harness 用 SQLite，真实 PG 路由测试的假 identity 忽略 actor。
- **发布**：tag `20260925-control-binding-actor` → `e6ab586`（分支 `hotfix/20260925-control-binding-actor` = 线上 `ee57ad4` + 仅 `services/control_api/app/multi_subject_runtime.py`）；`deploy_control_component.sh` dry-run PASS → `--cutover` PASS（PG schema/RLS 校验、target 解析、单容器重建），healthy、restarts=0，其余容器未动。回滚点 `memoria-control-api:rollback-20260925-control-binding-actor-pre-control`。main 侧同改动与真实 PG 回归见 PR #33。
- **小程序**：体验版 `0.2.20260925`（1.4 MB）经开发者工具 CLI 上传（CI 密钥 IP 白名单不含当前出口 IP，未改白名单）；含吉祥物表情动画、Runtime Profile v2 校验兼容、回顾/首页摘要/我的统计去掉 `memory_recall_private` 门禁（产品决定：一对一，账号即使用者）。**提交审核与正式发布需在公众平台手动完成。**
- **复核**：binding 查询 404 已消除（随后暴露 403，见上一节）。

## 权威状态

```yaml
schema_version: 2
as_of_date: 2026-09-18
reviewed_source_commit: read_path_pg_parity_and_deletion_saga_d2318e4_ci_35501188784
current_worktree: clean_after_read_path_pg_parity_and_deletion_saga_commit
production_runtime: python_authoritative
production_media: go_media_edge_direct_voice_core_with_livekit_compat
hardware_media_interaction_authority: python_authoritative
hardware_media_target_runtime: go_media_edge_direct_voice_core
hardware_media_rollback_runtime: python_device_gateway_livekit_compat
current_work_order: vocat_interrupt_assist
code: committed_through_d2318e4_read_path_pg_parity_and_pg_deletion_saga
wired: subject_scoped_read_exits_plus_migration_read_paths_and_persona_capsule_subject_read_plus_operator_pg_subject_read
enabled: false_for_current_head
verified: local_and_authoritative_postgres_regression_for_subject_scope_migrations_read_paths_and_receipts_plus_pg_subject_read_parity_and_pg_deletion_saga_plus_ci_35501188784_python_5306_passed
guardian_declaration_scope: binding_scoped_owner_only_third_party_excluded
accountless_person_consent: person_scoped_grant_read_revoke_replay_unbind_revoke_and_export_verified_device_pending
production_readiness: ready_at_last_observation_not_refreshed_this_review
production_readiness_observed_at: 2026-09-16T11:37:57+08:00
student_safety_loop_verified: false
student_safety_local_scope: real_persistent_session_runtime_on_ephemeral_pg_with_sqlite_account_and_declared_guardian_outbox_real_pg_person_consent_gate_real_catalog_subject_key_isolation_and_read_path_binding_fence_closing_the_agent_facing_seams_after_a_legal_manager_change_including_context_prefetch_and_cached_plan_reuse_plus_repeatable_read_snapshot
guardian_authority_evidence: parent_declaration_only_never_verified_link
guardian_declaration_notification_basis: deliberate_identity_declaration_not_consent
postgres_contracts_for_new_paths: scoped_pg17_contracts_passed_including_member_write_read_snapshot_and_history_exit
agent_release_gate_wiring: image_built_and_gate_rerun_offline_as_runtime_user_passing
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
- B 在 `20.23s` 挂钟触发旧 TTS 总超时，错误终态又延后约 `38.6s`；D 未见同类 TTS 错误。已有本地部分音频故障注入通过有界 CANCEL_GENERATION/ERROR/权威 listening 断言（见上方软件收据），但设备实际 cancel/flush/退出 speaking 的时序未复验，仍不能解释 B/D 两次停滞。
- 历史 G（epoch 1962）ASR final 先于迟到 VAD，受理时静默预算 0，无新 turn，最终 owner_silence_timeout。静默预算三态与迟到关闭已有本地修复和区分度回归，当前候选未复跑 G 真机，完整 10s/60s 交接矩阵仍待补；不再称修复前复现为当前 HEAD 失败。E/F/H 播后告别成功，C 的迟到告别失败；A 无有效告别输入，G 未到告别步骤，不计算“3/5 成功率”。

下一次设备窗口先确认已冻结并实际启用目标候选；执行顺序及修复前置见 P0-03/P0-04/P1-01：

1. 开串口可能复位，先等 `activating→idle` 和心跳再讲话；唤醒词“茉莉”。无人配合或设备未连接时只做离线检查，不自行刷机或播放自动代测。
2. 同一候选重跑三天天气→续问→播后告别，至少三轮；另测 >45s 长答、B/D 同类长答、临近静默续问，以及已下发部分音频后 provider 失败。每项分别判定功能、时延、终态、听感，不跨 release 累加通过数。
3. 同时取 Bridge/Edge WS writer/设备接收、解码、播放消费及任务/锁状态，绑定 session/stream/turn/generation/tool fence。保留 supply/prestart/boundary/close_dropped/outside、delivery ledger、指标差分、PCM RMS/削波；缺观测先补观测，不先加预缓冲或改阈值。
4. 功能口径（2026-09-17 晚四确定，本阶段只验功能）：能对话、能打断（button/keyword 按签名放行范围，中断后有界退出并回 listening）、长时间对话稳定、每次对话内容汇总可查（双方话轮与汇总以服务端日志/会话记录为准，操作员可读；无可读出口记缺口、不算通过）。不手工注入 profile，不绕声纹/准入门；HTTP 文本正确不是设备已说出，以终端回执与人工听感为准。学生危机场景的设备交付与通知 outbox 链（app_confirm 确认使用人及年龄、固定话术逐字交付、终端回执、outbox 绑定/幂等/家长读取）defer 到安全专项窗口，软件矩阵证据保留；通知发送 worker 按用户决定暂缓。

使用新目录，绑定板上收据（不是本次重新回读的证明）：

```bash
uv run --no-project --with pyserial --with esptool scripts/voice_session_capture.py \
  --out "$CAPTURE_DIR" \
  --firmware-receipt outputs/acceptance/run-20260915-p0-03-metering-lifecycle-device/postflash.json \
  --server-logs --duration 900
uv run python scripts/voice_session_report.py "$CAPTURE_DIR"
```

`--preflight-only` 不触碰设备；`--boot-reset` 仅在明确需要时使用。捕获必须有结束时间、逐路退出原因、无未解释 serial/cleanup error；缺终态不能报 healthy。Actual Heard 需设备终端证据和用户听感共同确认。

## 唤醒词（P2-05）设备证据

- 2026-09-16 原始矩阵 `outputs/acceptance/run-20260916-p2-05-wake-matrix-1/` 保留不改写。Tingting 合成 TTS + 0.25s/0.4s 静音垫可唤醒该板；系统输出静音与部分 voice 空渲染会伪造零召回，工具已有校验。gain 1.0 为 4/8，gain 0.3 为 8/8，合计 12/16；gain 不是测得的近/远距离。
- 首次 activating→idle 后，四次失败刺激分别在约 7.35/17.54/27.71/37.98s，首次成功约 49.59s。单次启动、先高后低的播放顺序不足证明固定 45–50s 预热，后 12/12 是事后子集；60s 默认等待仅实验参数。成功刺激起点→唤醒行中位 1.445s 含前导静音与采集时序，不是精确声学延迟。
- 去重后新唤醒事件为 0，原 TV=1 为上一试次滞后重复；但 TV 133.038s 内 idle=0，约 92.382s 为 speaking/KWS off；small_talk 124.471s 内 idle≈100.909s，quiet 300.001s 均为日志可见 idle。不能据此报电视/多人各两分钟有效零误唤醒，配置日志也不自动证明整窗 detector 持续启用。
- `092dcf4` 已有门控/去重初修，仍存在本轮复现的曝光高估与 fixture/CI 缺口（见 P2-05）。先修工具再按多次冷启动、随机/交错增益、真实音源/物理距离预定义协议采数；本轮无设备新数据，不改 `advertised_duplex_level` / `aec_reference_verified`。

## 屏幕：伙伴吉祥物（替换白描对话脸，2026-09-25）

- **状态**：`code=done`（overlay 新增 `memoria_mascot_{pack,scene,display}`、patch `0027`（原编号 0026，2026-09-26 为消除 0025 重号顺延）、`memoria_display_hooks`，白描脸源文件与测试已删除）；`wired=开发板已刷`（app `0x20000` + assets `0x800000`，identity/nvs/otadata 未动；刷前回读备份在 `firmware/esp32/artifacts/backups/pre-mascot-20260925/device-readback/`，stub 读 `0x322000` 起会断，需 `--no-stub`）；`enabled=true`（control-api `20260925-device-mascot-sync` 已上线，真机首轮轮询已切换伙伴）；`hardware_verified=false`（串口只证明启动、解码 139 ms、空闲约 240 重绘/分钟、每次约 8 ms 纯合成；画面需亲眼确认）。
- **手机同步链路**：小程序保存 `companion_id` → control-api 为该账号作为 owner/admin 的每个 active binding 的 primary subject 写 persona assignment（幂等、失败只记日志、`next_session` 投影，不打断进行中的回复）→ 设备空闲时每 20 s 签名 `GET /v1/devices/{id}/display-profile`（与 activation-manifest 同签名对象，仅 path 不同；409=未绑定）→ `display_version` 变化即换装并写 NVS `memoria_ui/companion`。已随 control-api `20260925-device-mascot-sync` 上线（无 schema/nginx 变更）。
- **注意**：「我的」页每次保存都带 `companion_id`，会把设备页对主使用人单独分配的人格改回账号伙伴（一对一产品下符合"选TA陪伴为准"）；若要只在值变化时同步，改 `companion_device_sync.py` 一处即可。
- **构建**：`common.sh` 现导出 `IDF_COMPONENT_CHECK_NEW_VERSION=0`；否则组件管理器读取 registry 最新 esp_video 的 esp_h264 规则，CMake 连跑两次后报 `Missing required kconfig option after retry`。全新 clone + `apply-overlay.sh`（0001–0026 全部干净应用）+ `build.sh` + `check-overlay.sh` 已通过；app `0x325850`（剩 20%），assets 5.8 MB / 8 MB。

| 看什么 | 期望 |
| --- | --- |
| 开机 | 黑屏→暖光球→光圈展开→`memoria` 字标→伙伴弹入落地→眨眼→挥手；无白屏闪烁 |
| 待机 | 呼吸、2.4–5.6 s 眨眼、20–38 s 一次小动作；3 分钟后瞌睡且背光变暗 |
| 拍一下 / 摇晃 | 开心跳 / 晕乎乎；都不开麦 |
| 唤醒后 | 聆听姿势 + 光环呼吸；说完后思考姿势 + 彗星光环 |
| 说话 | 服务端心情对应姿势 + 口型开合；结束后心情停约 2 s |
| 未绑定 | 白色圆角二维码卡片可扫、上方「你好，我是星澜」 |
| 小程序换伙伴 | 空闲 ≤20 s 内挥手换装，下一轮对话换声音；重启后保持 |

以同代 `assistant_expression→screen.expression` 和串口 `MemoriaMascot: emotion=` 为准。照片存入本次 ignored 验收目录，全部亲眼确认后才签收 `hardware_verified`；回滚 = 用回读备份写回 `0x20000` 与 `0x800000`。

## 永久运维参考与历史归档

永久运维材料已从会话流水中拆出，主交接只保留入口和变更边界：

- [发布、恢复与回滚运维手册](docs/runbooks/release-rollback.md)：生产拓扑、安全边界、发布前门禁、制品上传、数据恢复和回滚验收底线。
- [空间治理运维基线](docs/runbooks/operations-space-governance.md)：磁盘巡检、Docker 镜像保留、验收归档和 systemd 基线。
- [删除域与 seal 契约](docs/compliance/delete-domains.md)：2026-09-20 删除范围结论、17 表约束、已知合规缺口和不可归属面。
- [2026-09-20 及更早历史归档](docs/HANDOFF-archive-before-0920.md)：已移出的发布、设备窗口、F2 和待命复现证据；删除/seal 原文在上面的合规文档中保留。
- [2026-09-16 至 2026-09-23 历史归档](docs/HANDOFF-archive-0916-0923.md)：09-18 审查基线与候选边界、截至 09-16 的生产收据，以及 09-21 至 09-23 的发布、评测和设备窗口节；均已被 09-25 整栈发布取代。

后续永久运维规则只更新上述 runbook；会话证据继续按日期追加到本文件。

## 2026-09-24 缺陷 A 真实设备复测（核心边界通过，P0-03 未关闭）

- **候选与采集**：真实设备采集窗口为 `2026-09-24 11:45:34.122697`–`12:15:34.489031` CST，持续 1800s；服务端候选为 `memoria-agent:20260921-defect-a-followup-endpoint` / commit `2a33a50e85d09dc61944ac860e311d247a1020e2`。本轮未刷机、未向串口写数据、未重启服务；固件版本未从板上重新读取。
- **核心续问判定：通过**：同一主会话中 3/5/8s 停顿的“后天呢？”分别形成独立 `turn_id=3/4/5`，均有非空 boundary/endpoint、`actual_heard=true`、`playback_ended=true`；未见旧的回声与追问合并。逐格 boundary/endpoint 与日志链接见[完整收据](docs/acceptance/run-20260924-defect-a-retest-live/findings.md)。
- **长稳判定：基础通过，带连接观察项**：约第 10、12、14 分钟的三次人工唤醒均独立应答并回到 idle；无二次复位、panic、服务重启或 uptime reset。但期间多次出现 TLS/WebSocket 断开并自动重连，重连频率、期间体验和根因仍待收敛。
- **未完成边界**：“今天适合散步吗？”只完成了“我稍等，查询一下”的首段播放，最终工具查询回答在首帧前被 `conversation_end_explicit` / `output_task_cancelled` 取消；>45s/B/D 长答、部分下发失败、待机/表情及点屏/摇晃/短拍/BOOT 回归仍未覆盖。BMI2 I2C 超时为独立硬件观察项，不归因于缺陷 A。
- **状态**：本轮将缺陷 A 从“待真机复测”推进为“核心续问边界真实设备证据通过”，但 `P0-03` 保持 `[ ]`；`direct_real_device_verified=false`、`full_duplex_verified=false` 不变。
