# Memoria H5 生产发布与回滚

## 当前生产拓扑

- H5：`https://122.51.108.140:8443/`
- H5 兼容路径：`https://122.51.108.140:8443/memoria-h5/`
- Control API：`https://122.51.108.140:8443/memoria-api/`
- 正式 runtime release：发布前以
  `basename "$(readlink -f /opt/memoria/current)"` 为准
- 正式 H5 release：发布前以
  `basename "$(readlink -f /var/www/memoria-h5)"` 为准
- Control API 上游：`127.0.0.1:8791`
- LiveKit：自建 `livekit/livekit-server:v1.13.3`，位于 `/opt/livekit`，Compose project 为 `memoria-livekit`
- LiveKit 信令：`wss://122.51.108.140:8443`，经 Nginx `/rtc`、`/agent` 转发到 `127.0.0.1:7880`
- LiveKit Twirp API：`https://122.51.108.140:8443/twirp/`，转发到 `127.0.0.1:7880`
- LiveKit 媒体：服务器保留 `7882/UDP` 监听，但当前未开放对应云安全组；公网统一回退到与 HTTPS 复用的 `8443/TCP`，Agent 通过 `memoria_default` 内部网络走 UDP
- SQLite：`/var/lib/memoria/memoria.sqlite3`
- 终身档案：独立同机 PostgreSQL 17 + pgvector 0.8.1，Compose project 为 `memoria-data`
- 对象存储：独立同机 MinIO，档案/声音两个 bucket 分权并启用版本控制，无公网端口
- Runtime：版本目录位于 `/opt/memoria/releases/`，`/opt/memoria/current` 原子软链指向当前 release
- H5：版本目录位于 `/var/www/memoria-releases/`，`/var/www/memoria-h5` 原子软链指向当前 release
- 原生小程序当前媒体入口：
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`。标准 `443` 保留同路径
  精确路由，但当前中国大陆无 VPN 网络在 TLS ClientHello 后重置连接，未解决前不由 Control API
  下发；两者的 loopback 上游均为 `127.0.0.1:8792`。

Memoria 使用独立静态资源/API 路径、回环端口、Compose project 和限流 zone。新服务器的
443 仍由既有 WMS 虚拟主机拥有，只额外 include
`/etc/nginx/snippets/memoria-miniprogram-media.conf` 暴露精确的小程序媒体 WSS；WMS 根路径、
`/wms/` 和其他 404 边界保持不变。8443 继续由 Nginx stream 预读协议，TLS 流量转到
`127.0.0.1:9443` 的 Memoria HTTPS server，原生 ICE/TCP 转到 `127.0.0.1:8444` 后进入
LiveKit 容器的 8443；H5、Control API 与 LiveKit 正式入口仍为 8443。公网
`/memoria-api/internal/` 固定返回 404；WMS 数据与配置必须保留，服务可在明确授权的资源让渡期
保持 `inactive / enabled`。当前生产版本与验收结论见
`HANDOFF.md`，历史迁移与热修证据保留在 `docs/releases/`。

终身档案迁移到 PostgreSQL + 对象存储后的备份、PITR、对象清单与联合恢复门禁见 [`archive-backup-restore-runbook.md`](./archive-backup-restore-runbook.md)。现有 SQLite 发布快照只覆盖旧主库，不得被描述为终身档案生产恢复方案。

> P0.5、P1-P6 的稳定账号、终身记忆、Persona、SpeakerAuthority、VoiceProfile 与账户治理底座随 `20260720-140053` 部署。当前 runtime/H5 以 `HANDOFF.md` 和服务器软链为准。CAM++ 仍为 shadow-only，正式声纹/复刻声音不得在真人授权与盲测前激活。当前 PostgreSQL、WAL archive、MinIO 和备份均同机，没有异地副本/KMS/PITR，不能承诺“永不丢失”。

## TLS 与自动续期

新服务器公网 IPv4 入口使用 Let's Encrypt 短期证书：SAN 为 `122.51.108.140`，当前证书有效至 2026-07-26 21:15:49 UTC。`aigcnice.com` 与 `www.aigcnice.com` 使用同机 TrustAsia 域名证书，当前有效至 2026-10-18 03:59:59 UTC；`127.0.0.1:9443` 的两个证书虚拟主机复用 `/etc/nginx/snippets/memoria-site-common.conf`，IP/无 SNI 默认选择 IP 证书，域名 SNI 选择域名证书。`snap.certbot.renew.timer` 为 enabled/active；deploy hook 安装于 `/etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx`，先运行 `nginx -t`，只有成功才 reload Nginx。

运维检查：

```bash
sudo systemctl status snap.certbot.renew.timer
sudo certbot certificates
sudo certbot renew --dry-run
sudo sha256sum /etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx
```

80 端口必须保留 `/.well-known/acme-challenge/`。不得把 deploy hook 改成无条件 reload。

## Secret 与数据边界

生产 secret 按最小权限拆分到服务器 `/etc/memoria-control-api.env`、
`/etc/memoria-agent.env`、`/etc/memoria-speaker-model.env` 和
`/etc/memoria-miniprogram-gateway.env`，四者权限都必须是 `root:root 0600`。使用
`scripts/split_production_env.py` 从 root-only 运维源生成候选文件；该脚本只分流已有值，
不会应用默认值。首次 P0-P6 升级应使用 `scripts/prepare_production_upgrade_env.py`，
普通发布则必须从当前 root-only env 复制并显式核对本文列出的 endpointing 值；
仓库、H5 bundle、发布清单、日志和本文档都不得出现源文件或 secret 值。gateway env 只包含
LiveKit 接入凭据、gateway ticket 签名材料和媒体适配配置；它不包含 `MEMORIA_AUTH_SECRET`、
档案对象存储密钥、DASHSCOPE 或 Agent capability token。

Agent 与 Control API 的内部能力必须分别配置，值至少 32 字符且两两不同：

```text
MEMORIA_ARCHIVE_WRITE_TOKEN
MEMORIA_AGENT_HEARTBEAT_TOKEN
MEMORIA_MEMORY_READ_TOKEN
MEMORIA_PERSONA_READ_TOKEN
MEMORIA_VOICE_RESOLUTION_TOKEN
MEMORIA_VOICE_CLEANUP_TOKEN
MEMORIA_INTERACTION_POLICY_TOKEN
MEMORIA_RESPONSE_PLAN_TOKEN
```

`MEMORIA_SPEAKER_INTERNAL_TOKEN` 也必须独立，不能与上述任一 token 或旧 `MEMORIA_ARCHIVE_INTERNAL_TOKEN` 复用。旧 token 只用于非生产兼容；不得写入 H5 环境、构建参数、浏览器存储或 Nginx 返回头。

原生小程序网关启用时，`MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET` 必须至少 32 字符，
独立于 `MEMORIA_AUTH_SECRET`、`LIVEKIT_API_SECRET` 和全部 capability token；它只进入
Control API 与 gateway env，不进入 Agent 或 H5。

必须非空的变量名：

```text
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
DASHSCOPE_API_KEY
MEMORIA_AUTH_SECRET
```

豆包 TTS 鉴权必须二选一：旧控制台使用 `DOUBAO_TTS_APP_ID` +
`DOUBAO_TTS_ACCESS_TOKEN`，新控制台可使用 `DOUBAO_TTS_API_KEY`。这些值只允许进入
`/etc/memoria-agent.env`；双向 WebSocket 不使用 Secret Key，禁止把
`DOUBAO_TTS_SECRET_KEY` 写入运维源、候选 env 或服务器配置。候选文件安装后仍必须保持
`root:root 0600`。

当前生产使用的非 secret 配置：

```dotenv
ENVIRONMENT=production
LLM_PROVIDER=qwen
TTS_PROVIDER=doubao
DEPLOYMENT_PROFILE=cn_self_hosted
PUBLIC_BASE_URL=https://122.51.108.140:8443/memoria-api
ALLOWED_ORIGINS=https://122.51.108.140:8443
OFFLINE_MOCK=false
LIVEKIT_URL=wss://122.51.108.140:8443
LIVEKIT_AGENT_NAME=duplex-zh-agent
LIVEKIT_TURN_DETECTOR_VERSION=v1-mini
LIVEKIT_ADAPTIVE_INTERRUPTION=false
PREEMPTIVE_GENERATION=false
DASHSCOPE_WS_URL=wss://dashscope.aliyuncs.com/api-ws/v1/inference
DASHSCOPE_COMPATIBLE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_FAST_MODEL=qwen-turbo
QWEN_DEEP_MODEL=qwen-plus
INTERRUPT_SEMANTIC_ENABLED=true
INTERRUPT_SEMANTIC_MODEL=qwen-flash
INTERRUPT_SEMANTIC_TIMEOUT_S=0.6
MINIPROGRAM_KWS_ENABLED=false
MINIPROGRAM_KWS_MODEL_DIR=/data/models/vosk-model-small-cn-0.22
MINIPROGRAM_KWS_KEYWORDS_FILE=/app/infra/kws/keywords.txt
MINIPROGRAM_KWS_MIN_CONFIDENCE=0.65
DASHSCOPE_SUMMARY_MODEL=qwen-plus
FUNASR_MODEL=fun-asr-realtime
FUNASR_SAMPLE_RATE=16000
FUNASR_MAX_SENTENCE_SILENCE_MS=550
FUNASR_CONTEXT_ENABLED=false
# FUNASR_VOCABULARY_ID=<控制词热词表 ID，未创建时不要设置>
# FUNASR_SPEECH_NOISE_THRESHOLD=-0.1
DOUBAO_TTS_WS_URL=wss://openspeech.bytedance.com/api/v3/tts/bidirection
DOUBAO_TTS_RESOURCE_ID=seed-tts-2.0
DOUBAO_TTS_VOICE_PROFILE=warm_companion
DOUBAO_TTS_VOICE_REGISTRY=infra/voices/doubao_voice_ids.json
DOUBAO_TTS_SAMPLE_RATE=24000
DOUBAO_TTS_POOL_SIZE=4
MINIPROGRAM_POST_PLAYOUT_GUARD_MS=150
MINIPROGRAM_GATEWAY_AEC_ENABLED=false
MINIPROGRAM_GATEWAY_AEC_MODE=off
MINIPROGRAM_GATEWAY_AEC_STREAM_DELAY_MS=120
MINIPROGRAM_GATEWAY_AEC_ACTIVE_WINDOW_MS=750
MINIPROGRAM_GATEWAY_AEC_CAPTURE_SESSION_ID=
MINIPROGRAM_GATEWAY_AEC_CAPTURE_DIR=/tmp/memoria-aec-diagnostics
MINIPROGRAM_GATEWAY_AEC_CAPTURE_MAX_MS=5000
MINIPROGRAM_GATEWAY_GENERATION_QUARANTINE_MS=400
MINIPROGRAM_GATEWAY_AUDIO_QUEUE_FRAMES=20

MEMORIA_TIMEZONE=Asia/Shanghai
READINESS_GATE_TTL_S=86400
SESSION_TOKEN_TTL_S=300
MEMORIA_AUTH_TOKEN_TTL_S=900
MEMORIA_AUTH_REFRESH_TTL_S=2592000
MEMORIA_REFRESH_COOKIE_NAME=memoria_refresh
WECHAT_AVATAR_PUBLIC_BASE_URL=https://aigcnice.com:8443/memoria-api
MEMORIA_LEGACY_AUTH_COMPAT_UNTIL=
MEMORIA_MESSAGE_IDEMPOTENCY_SECRET=
ENDPOINTING_MIN_DELAY_S=0.90
ENDPOINTING_MAX_DELAY_S=1.50
ENDPOINTING_ALPHA=0.85
INTERRUPTION_MIN_DURATION_S=0.45
FALSE_INTERRUPTION_TIMEOUT_S=1.70
MEMORIA_SPEAKER_GUEST_THRESHOLD=0.40
```

### 小程序短命令 KWS 模型

`MINIPROGRAM_KWS_ENABLED` 默认关闭。当前候选使用官方
`vosk-model-small-cn-0.22`，仅在可信小程序 AEC 会话、助手播放期和当前 VAD speech
epoch 内解码。VAD 开始负责立即 duck；VAD 结束后只有完整结果精确等于纯控制词、不含
`[unk]` 且达到平均置信度门槛时，才把它交给 `UtteranceRouter`。partial 不能执行停止，
否则“等一下我想问……”会被过早吞成纯中断。

生产 Compose 已把宿主机 `/var/lib/memoria-agent` 映射为 Agent `/data`。模型只安装到宿主机，
不进入 Git、发布包或 Docker 镜像：

```bash
tmp_dir="$(mktemp -d)"
curl -fL --retry 3 \
  -o "$tmp_dir/vosk-model-small-cn-0.22.zip" \
  https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
printf '%s  %s\n' \
  '3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba' \
  "$tmp_dir/vosk-model-small-cn-0.22.zip" | sha256sum -c -
unzip -q "$tmp_dir/vosk-model-small-cn-0.22.zip" -d "$tmp_dir"
sudo install -d -o 65532 -g 65532 -m 0750 \
  /var/lib/memoria-agent/models/vosk-model-small-cn-0.22
sudo cp -a "$tmp_dir/vosk-model-small-cn-0.22/." \
  /var/lib/memoria-agent/models/vosk-model-small-cn-0.22/
sudo chown -R 65532:65532 \
  /var/lib/memoria-agent/models/vosk-model-small-cn-0.22
sudo find /var/lib/memoria-agent/models/vosk-model-small-cn-0.22 \
  -type d -exec chmod 0750 {} +
sudo find /var/lib/memoria-agent/models/vosk-model-small-cn-0.22 \
  -type f -exec chmod 0640 {} +
rm -rf "$tmp_dir"
```

控制词随 Agent 镜像保存在 `/app/infra/kws/keywords.txt`，只包含“停一下、等一下、等下、
等等、别说了、暂停、先别说、停下”等纯控制词。不要加入普通聊天短语。

`vosk==0.3.45` 固定在 lock 中并继续受 `--require-hashes` 门禁。Vosk 代码与官方模型表均
标记为 Apache-2.0；模型 zip 本身没有附带独立 LICENSE/NOTICE，所以当前仍不随 Memoria
制品再分发。许可、Python 3.12/Linux amd64 smoke、归档校验和正负例见
[`2026-07-27-vosk-chinese-kws.md`](research/2026-07-27-vosk-chinese-kws.md)。

### 单会话 AEC 前后音频采样

正常半双工流量保持 `MINIPROGRAM_GATEWAY_AEC_MODE=off`。需要做 A/B 时，在明确的真机
验收窗口将它临时设为 `alternating`；Gateway 按 `session_id` 稳定分配 `control/aec`
组，并在 `mini_program_aec_variant` 日志和 `ready.aec` 中输出分组及实际 APM 状态。
旧的 `MINIPROGRAM_GATEWAY_AEC_ENABLED=true` 仍兼容为全量 `on`，但不用于日常生产。

诊断采样默认关闭。只在下一次真机复测前，把
`MINIPROGRAM_GATEWAY_AEC_CAPTURE_SESSION_ID` 设置为目标 session ID 并重启 gateway。
该 session 最多保存 `MINIPROGRAM_GATEWAY_AEC_CAPTURE_MAX_MS` 的 16 kHz 单声道 WAV：

```text
aec-<UTC>-<session-hash>-pre.wav
aec-<UTC>-<session-hash>-post.wav
aec-<UTC>-<session-hash>.json
```

目录权限为 `0700`，文件为 `0600`，文件名和 manifest 不保存原始 session ID、转写或用户
身份。采样位于 gateway 容器 tmpfs，测试后使用 `docker cp` 导出到 root-only 备份目录，
随即清空 `MINIPROGRAM_GATEWAY_AEC_CAPTURE_SESSION_ID` 并重启 gateway；容器替换会丢失
未导出的采样。

生产默认 LLM 和每日回顾均使用百炼 Qwen。账号版本发布后，浏览器只接收注册账号 Bearer token 和短期 LiveKit participant token；`/v1/auth/anonymous` 仅用于兼容旧身份并在注册时原地升级。服务端在持久化消息、Profile 或向 Agent/FunASR 传递上下文前统一做 PII 脱敏，所有 memory/session route 均校验 token subject 与资源所有权。账号登录不等于当前说话人是主人，私人档案权限仍需结合 `owner / guest / uncertain` 判定。

认证会话切换必须使用显式、有限的兼容窗口。runtime 先切流时，仅可把
`MEMORIA_LEGACY_AUTH_COMPAT_UNTIL` 只接受绝对 UTC，且不能晚于启动时刻 24 小时；空值默认关闭，
非 UTC 或过长窗口会拒绝启动。已过期的截止值允许服务正常重启，但兼容能力自然保持关闭。
窗口内 sidless 旧 access token 只允许调用 `/v1/auth/upgrade`，
不能直接读取档案、写消息或调用其他业务接口；新 H5 只有在升级成功并写入安全快照后才清除
旧 token，网络中断或页面重载可在同一截止窗口内继续恢复匿名账号。所有现代消息请求都必须
提交 `client_message_id`。`MEMORIA_MESSAGE_IDEMPOTENCY_SECRET` 在生产至少 32 字符，且必须
独立于认证、LiveKit 和 capability token；它应在 auth secret 轮换时保持不变。
新 H5 切流并验收后仍须把原定窗口保留到绝对截止，让尚未重载的匿名用户有一次迁移机会；
到期后再删除该变量并重启。即使到期值暂未清理，服务也会自动恢复严格 401，禁止滚动延长
或因已过期配置拒绝启动。发布记录必须写明截止时间与迁移期提示。

## Agent 显式就绪门禁

LiveKit transport 连接不代表 Agent 可用：

1. H5 初次连接只接受当前 session、当前 generation、Agent participant 在 `voice-agent.ui` topic 发布的 `assistant_state: ready`。
2. 首次 `ready` 前的其他状态和字幕全部忽略；45 秒内仍未收到时，H5 断开、清理 session 并进入可重试状态。
3. Agent 完成 session、音频输出和 UI publisher 绑定后，先发布并等待 `ready`，随后才调用 `generate_reply` 生成首次欢迎语。

该协议由 H5 自动化回归覆盖。

P0～P6 发布后，Control API `/health/ready` 还必须同时返回以下 9 个 core check：Control DB、LifeArchive、MemoryCatalog、Persona、SpeakerAuthority、VoiceProfile、档案对象存储、声音对象存储和独立 `speaker-model`。前 8 项必须为 `ready`；`speaker-model` 必须实时请求 `/health/ready`，验证 HTTP 200、`status=ready` 和精确 `model_version`。此外，Agent 必须每 10 秒使用独立 capability token 上报 release、boot ID、worker 与 LiveKit 注册状态；心跳缺失、未就绪、版本不符或超过 45 秒都会令 readiness 返回 503。对象存储检查执行最小加密 `put/get/delete` canary；空账户或尚无 active 声音档案可以 ready，但缺组件、数据库/模型异常、版本漂移或对象 canary 失败必须返回 503。开发/离线未配置模型时只允许明确显示 `skipped`，不代表生产 ready。

Compose 的 Control API 容器健康检查固定使用 `/health/live`：Agent 必须先等 Control 的进程可接收心跳，不能拿依赖 Agent 心跳的 `/health/ready` 做启动门禁。相对地，Agent 容器健康检查调用 `python -m services.agent.src.heartbeat --check-health`，它同时验证 LiveKit SDK 本机 `8081` 返回 2xx，以及 `/tmp/memoria-agent-heartbeat.json` 中同一 release 的最近一次已被 Control 接受的 ready 心跳（30 秒内）。POST、鉴权、响应失败或 LiveKit 正在重连都不会刷新该无 secret 的原子状态文件；配合 10 秒检查间隔、3 秒超时和 2 次重试，最迟在最后一次 ready 心跳后的 60 秒内把 Agent 容器标为 unhealthy。Control `/health/ready` 的心跳 freshness 仍为 45 秒，两者分工不变。

Agent 的注册探针绑定当前固定版本 `livekit-agents==1.6.5` 的私有状态：仅当 `_id` 非空且不为 `unregistered`，并且 `_closed / _connecting / _connection_failed` 均表示已连接时才算注册。SDK 的 `8081` 在重连阶段仍可能返回 200，不能单独作为注册证据。升级 LiveKit Agents 前必须重新核对这些字段及重连路径，并同步更新探针契约测试。

生产 Control API 必须以 `uvicorn --no-access-log` 启动，由 Nginx 记录常规访问；签名声音样本路由同时 `access_log off`。这样 query 中的短期样本 token 不会进入 Nginx 或 Uvicorn access log，应用日志也不得自行记录完整 URL。

## P0.5、P1～P6 上线状态与后续门槛

1. `20260720-140053` 已在新服务器部署 pgvector、FORCE RLS、MinIO 版本控制、能力级 token、独立 SpeakerAuthority token、迁移/联合恢复、core readiness 和真实 Provider smoke；当前 runtime/H5 以 `HANDOFF.md` 和服务器软链为准。
2. 当前低成本底座为同机 PostgreSQL、WAL archive、MinIO 和备份；PITR、异地副本与 KMS 仍是下一阶段可靠性门槛，不能把同机恢复演练描述为异地容灾。
3. 使用授权样本完成 SpeakerAuthority 指标报告和历史 CosyVoice 复刻声音真人盲测前，不得激活正式声纹模板或复刻声音；当前豆包主链只使用已审核的原生 TTS 2.0 音色。
4. 账户删除 worker、LiveKit 房间删除权限、对象全版本删除权限和供应商声音删除权限必须同时具备；缺任一权限时删除只能保持 `deleting`，不得伪报完成。
5. 每个后续候选仍必须通过完整 PostgreSQL 合同、联合恢复、core readiness、Provider smoke、镜像 secret 扫描和真实 H5 浏览器检查，再按“runtime 先、H5 最后”顺序切流；原生 iOS 客户端已从仓库移除。

## 发布原则

H5 必须最后激活。标准顺序是：本机构建并校验工件 → 暂存 release 与 H5 → 创建并验证数据快照 → 服务器导入镜像 → 原子切 runtime → 容器/Provider/readiness 门禁 → Nginx 与证书检查 → 最后原子切 H5 → 公网验收。这样既避免小内存服务器构建卡死，也避免新 H5 连接尚未 ready 的 runtime。

下面命令沿用已验证的生产目录布局。执行前必须读取并记录当前 runtime/H5 软链，再在 shell 显式设置一个非空、唯一、不可复用的新 `RELEASE_TAG`；示例块会在缺失时立即失败。

```bash
: "${RELEASE_TAG:?set a new unique RELEASE_TAG, for example YYYYMMDD-HHMMSS}"
RELEASE_DIR=/opt/memoria/releases/$RELEASE_TAG
H5_DIR=/var/www/memoria-releases/$RELEASE_TAG
UPLOAD_DIR=/opt/memoria/incoming/$RELEASE_TAG
RELEASE_SOURCE_DIR=$UPLOAD_DIR/memoria
BACKUP=/var/lib/memoria/memoria-pre-$RELEASE_TAG.sqlite3
PROTECTED_BACKUP_DIR=/var/backups/memoria
PROTECTED_BACKUP=$PROTECTED_BACKUP_DIR/memoria-pre-$RELEASE_TAG.sqlite3
CONTROL_ENV=/etc/memoria-control-api.env
AGENT_ENV=/etc/memoria-agent.env
SPEAKER_MODEL_ENV=/etc/memoria-speaker-model.env
GATEWAY_ENV=/etc/memoria-miniprogram-gateway.env
CONTROL_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/control-api.env
AGENT_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/agent.env
SPEAKER_MODEL_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/speaker-model.env
GATEWAY_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/gateway.env
CONTROL_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-control-api.env-pre-$RELEASE_TAG
AGENT_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-agent.env-pre-$RELEASE_TAG
SPEAKER_MODEL_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-speaker-model.env-pre-$RELEASE_TAG
GATEWAY_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-miniprogram-gateway.env-pre-$RELEASE_TAG
```

### 1. 本机构建、打包并上传固定工件

生产机只有约 3.6 GiB 内存，默认禁止在服务器执行完整 `compose build`。依赖未变化时，在本机从上一健康 amd64 镜像做增量构建；`pyproject.toml` 或 `uv.lock` 变化时，仍在本机执行固定依赖的完整 amd64 构建。

```bash
RELEASE_TAG=YYYYMMDD-HHMMSS
BASE_TAG=上一健康版本
ARTIFACT_DIR="$(mktemp -d /tmp/memoria-release.XXXXXX)"
MEMORIA_RELEASE_COMMIT="$(git rev-parse HEAD)"

# release 文档须先写入 docs/releases/$RELEASE_TAG.md 并随代码提交；构建前创建唯一 tag。
test -f "docs/releases/$RELEASE_TAG.md"
git tag -a "$RELEASE_TAG" "$MEMORIA_RELEASE_COMMIT" -m "Memoria $RELEASE_TAG"

# 正式工件必须来自唯一且干净的 Git commit/tag；tracked/untracked 变更都会拒绝。
python3 scripts/verify_release_source.py \
  --root . \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --release-tag "$RELEASE_TAG" \
  --create-archive "$ARTIFACT_DIR/source.tar"
python3 scripts/package_release_verifier.py \
  --root . \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --release-tag "$RELEASE_TAG" \
  --output "$ARTIFACT_DIR/release-verifier.pyz"
if command -v sha256sum >/dev/null 2>&1; then
  MEMORIA_RELEASE_VERIFIER_SHA256="$(sha256sum "$ARTIFACT_DIR/release-verifier.pyz" | cut -d ' ' -f1)"
else
  MEMORIA_RELEASE_VERIFIER_SHA256="$(shasum -a 256 "$ARTIFACT_DIR/release-verifier.pyz" | cut -d ' ' -f1)"
fi
printf 'copy this verifier hash into the authenticated server shell: %s\n' \
  "$MEMORIA_RELEASE_VERIFIER_SHA256"

# 依赖未变化：复用本地基础镜像，只复制应用代码。
# Docker Desktop 的 BuildKit 不能解析无 registry 的本地基础 tag 时，
# 显式使用本地 daemon 的 legacy builder；构建结果仍须验证为 amd64。
DOCKER_CONTEXT=default DOCKER_BUILDKIT=0 \
  BASE_TAG="$BASE_TAG" NEW_TAG="$RELEASE_TAG" \
  MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  bash scripts/delta_build_images.sh

for image in agent control-api speaker-model miniprogram-gateway; do
  test "$(docker image inspect "memoria-$image:$RELEASE_TAG" \
    --format '{{.Architecture}}')" = amd64
done

npm --prefix apps/h5 run build
python3 scripts/package_h5_artifact.py \
  --source apps/h5/dist \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --release-tag "$RELEASE_TAG" \
  --output "$ARTIFACT_DIR/h5-dist.tar.gz"
docker save --platform linux/amd64 \
  "memoria-agent:$RELEASE_TAG" \
  "memoria-control-api:$RELEASE_TAG" \
  "memoria-miniprogram-gateway:$RELEASE_TAG" \
  "memoria-speaker-model:$RELEASE_TAG" \
  -o "$ARTIFACT_DIR/images.tar"
for artifact in source.tar images.tar h5-dist.tar.gz; do
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$ARTIFACT_DIR" && sha256sum "$artifact" > "$artifact.sha256")
  else
    (cd "$ARTIFACT_DIR" && shasum -a 256 "$artifact" > "$artifact.sha256")
  fi
done

# 上传前生成 canonical manifest：固定 source/images/H5 三件套、tag/commit 与 payload digest。
python3 scripts/create_release_manifest.py \
  --root . \
  --release-tag "$RELEASE_TAG" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --source-archive "$ARTIFACT_DIR/source.tar" \
  --images-archive "$ARTIFACT_DIR/images.tar" \
  --h5-artifact "$ARTIFACT_DIR/h5-dist.tar.gz" \
  --output "$ARTIFACT_DIR/release-manifest.json"
if command -v sha256sum >/dev/null 2>&1; then
  MEMORIA_RELEASE_MANIFEST_SHA256="$(sha256sum "$ARTIFACT_DIR/release-manifest.json" | cut -d ' ' -f1)"
else
  MEMORIA_RELEASE_MANIFEST_SHA256="$(shasum -a 256 "$ARTIFACT_DIR/release-manifest.json" | cut -d ' ' -f1)"
fi
printf 'copy this manifest hash into the authenticated server shell: %s\n' \
  "$MEMORIA_RELEASE_MANIFEST_SHA256"
```

首次包含小程序网关的 release 可以没有上一 tag 的 gateway 镜像；增量脚本会继续复用
Agent、Control API 和 Speaker Model 的健康基础镜像，只用锁定依赖的
`Dockerfile.miniprogram-gateway` 完整构建新网关镜像。后续 release 才对 gateway 使用
同样的增量路径。

依赖变化时不要运行增量脚本，在本机执行完整构建：

```bash
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.agent \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  -t "memoria-agent:$RELEASE_TAG" .
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.control-api \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  -t "memoria-control-api:$RELEASE_TAG" .
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.speaker-model \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  -t "memoria-speaker-model:$RELEASE_TAG" .
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.miniprogram-gateway \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  -t "memoria-miniprogram-gateway:$RELEASE_TAG" .
```

Dockerfile 必须从 `uv.lock` 或固定 requirements 导出并安装固定版本与哈希，任何不匹配都令构建失败；不得使用 `latest`。完整构建后同样执行上面的架构校验、H5 build、`docker save` 和 SHA-256 清单生成。

将 `source.tar`、`images.tar`、`h5-dist.tar.gz`、三份 SHA-256 清单、
`release-manifest.json` 与 `release-verifier.pyz` 一起上传到服务器临时目录。
manifest 必须在上传前生成，且不得在服务器重建。验证器由已审核 commit 中的三个脚本确定性打包；
其 SHA-256 必须从本地认证终端单独复制到服务器 shell，不能读取上传目录中的 sidecar 或 manifest
代替这个信任锚。服务器先验证该哈希，再由验证器核对完整三件套，成功后才能解包源码。此机制是
**commit-bound provenance**，不等同于第三方签名或抗恶意持有构建权限者的供应链签名。服务器只做校验和导入：

```bash
cd "$UPLOAD_DIR"
: "${MEMORIA_RELEASE_COMMIT:?set the reviewed 40-character release commit}"
: "${MEMORIA_RELEASE_VERIFIER_SHA256:?copy the verifier hash from the authenticated build shell}"
: "${MEMORIA_RELEASE_MANIFEST_SHA256:?copy the manifest hash from the authenticated build shell}"
printf '%s  %s\n' \
  "$MEMORIA_RELEASE_VERIFIER_SHA256" "$UPLOAD_DIR/release-verifier.pyz" \
  | sha256sum -c -
printf '%s  %s\n' \
  "$MEMORIA_RELEASE_MANIFEST_SHA256" "$UPLOAD_DIR/release-manifest.json" \
  | sha256sum -c -
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" \
  --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT"
sudo tar --extract --file "$UPLOAD_DIR/source.tar" \
  --directory "$UPLOAD_DIR" --no-same-owner --no-same-permissions
sha256sum -c source.tar.sha256
sha256sum -c images.tar.sha256
sha256sum -c h5-dist.tar.gz.sha256
sudo docker load -i "$UPLOAD_DIR/images.tar"
python3 "$UPLOAD_DIR/release-verifier.pyz" \
  --manifest "$UPLOAD_DIR/release-manifest.json" \
  --artifact-dir "$UPLOAD_DIR" \
  --expected-tag "$RELEASE_TAG" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --verify-imported-images
for image in agent control-api speaker-model miniprogram-gateway; do
  sudo docker image inspect "memoria-$image:$RELEASE_TAG" \
    --format '{{.Id}} {{.Architecture}}'
done
```

H5 只从已校验的归档解包到候选目录，不接受另行上传或就地修改的裸 `dist/`：

```bash
sudo install -d -m 0755 "$H5_DIR"
sudo tar --extract --gzip --file "$UPLOAD_DIR/h5-dist.tar.gz" \
  --directory "$H5_DIR" --no-same-owner --no-same-permissions
sudo test -f "$H5_DIR/index.html"
```

跨 Docker Desktop containerd store 与 Linux Docker Engine 时，顶层 `.Id` 可能不同，不能用两端 `.Id` 相等作为工件一致性条件。上传路径以镜像归档 SHA-256 为主证据；需要把服务器镜像回补本机时，再分别对 `{{json .RootFS.Layers}}` 与 `{{json .Config}}` 做 SHA-256，两组哈希均一致才算同一工件。

只有本机构建环境不可用且已确认服务器有足够资源时，才允许把服务器构建作为显式回退；不得把它恢复为默认发布路径。导入校验成功后，候选 runtime 必须从 `$UPLOAD_DIR/memoria`（即 source archive 解包结果）安装，不能把本机 checkout 或仅 release 文档当作 source evidence。

镜像和候选 H5 暂存后、切流前运行隔离 server smoke。脚本使用候选 H5、临时 SQLite、
`18791/18891`，不会占用在线 Control API 的 `8791`：

```bash
sudo /opt/memoria/releases/$RELEASE_TAG/scripts/smoke_server_deployment.sh \
  "$RELEASE_TAG"
```

### 2. 暂存工件，不切公网软链

把完整 release 放入 `$RELEASE_DIR`，把 production build 放入 `$H5_DIR`。此阶段不得修改 `/opt/memoria/current` 或 `/var/www/memoria-h5`。

```bash
sudo install -d -m 0755 "$RELEASE_DIR"
sudo cp -a "$RELEASE_SOURCE_DIR/." "$RELEASE_DIR/"
printf 'MEMORIA_RELEASE_COMMIT=%s\nMEMORIA_RELEASE_TAG=%s\n' \
  "$MEMORIA_RELEASE_COMMIT" "$RELEASE_TAG" \
  | sudo tee "$RELEASE_DIR/.env" >/dev/null
sudo test -f "$RELEASE_DIR/docker-compose.production.yml"
sudo test -s "$RELEASE_DIR/.env"
sudo test -f "$H5_DIR/index.html"
sudo chown -R root:root "$RELEASE_DIR" "$H5_DIR"
sudo chmod 0644 "$RELEASE_DIR/.env"
sudo find "$RELEASE_DIR" "$H5_DIR" -type d -exec chmod 0755 {} +
```

#### H5 immutable 资源兼容门禁

`/memoria-h5/index.html` 是 `no-cache`，但 `/memoria-h5/assets/` 是一年期 `public, immutable`。旧标签页即使已经重新指向新软链，仍会运行缓存中的旧主脚本，并按旧哈希继续请求懒加载分片；只保留新 release 的 `assets/` 会让这些请求变成 404。`index.html` 的不缓存策略不能修复这个问题。

因此每个候选 H5 的 `assets/` 都是追加式的 immutable URL 命名空间：候选构建自己的文件优先，历史 release 只补入不存在的文件，绝不覆盖候选文件。切换 `/var/www/memoria-h5` 前执行以下命令。它先拒绝同一路径但字节不同的资源（这违反 immutable URL 约定），再无覆盖合并历史 `assets/`；不能用 `cp -f`、`rsync --delete` 或清空候选 `assets/` 替代。

```bash
sudo bash -ceu '
release_root=/var/www/memoria-releases
candidate=$1
candidate_assets=$candidate/assets
install -d -o root -g root -m 0755 "$candidate_assets"
find "$candidate_assets" -type f -name "._*" -delete

declare -a historical_assets=()
for source in "$release_root"/*/assets; do
  [ -d "$source" ] || continue
  [ "$(readlink -f "$source/..")" = "$(readlink -f "$candidate")" ] && continue
  historical_assets+=("$source")
done

# First detect every immutable-path collision; do not leave a partial union on failure.
for source in "${historical_assets[@]}"; do
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    destination="$candidate_assets/$relative"
    if [ -e "$destination" ] && ! cmp -s -- "$old_asset" "$destination"; then
      printf "immutable asset collision: %s\n" "$relative" >&2
      exit 1
    fi
  done < <(find "$source" -type f ! -name "._*" -print0)
done

# 只追加真实资源；macOS AppleDouble `._*` 是不可服务的元数据垃圾，不进入新 union。
for source in "${historical_assets[@]}"; do
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    destination="$candidate_assets/$relative"
    if [ ! -e "$destination" ]; then
      install -d -o root -g root -m 0755 "$(dirname "$destination")"
      cp -a --no-dereference -- "$old_asset" "$destination"
    fi
  done < <(find "$source" -type f ! -name "._*" -print0)
done

for source in "${historical_assets[@]}"; do
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    test -f "$candidate_assets/$relative"
  done < <(find "$source" -type f ! -name "._*" -print0)
done
' bash "$H5_DIR"
```

若碰到 collision，必须把变更后的资源改为内容哈希文件名，或保持该 URL 字节完全不变后再发布；不能为了发布而覆盖它。清理旧 H5 release 时也不得删除当前候选 union 中的资源；在 `/assets/` 仍为一年 immutable 缓存期间，H5 资源集合按追加式保留。

### 3. 创建发布前 SQLite 快照

SQLite 使用 WAL，禁止只复制主文件。使用 SQLite backup API 创建一致快照并立即做只读完整性检查：

```bash
sudo env BACKUP="$BACKUP" python3 - <<'PY'
import os
import sqlite3

backup = os.environ["BACKUP"]
source = sqlite3.connect(
    "file:/var/lib/memoria/memoria.sqlite3?mode=ro",
    uri=True,
)
destination = sqlite3.connect(backup)
source.backup(destination)
destination.close()
source.close()

check = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
result = check.execute("PRAGMA integrity_check").fetchone()[0]
foreign_key_violations = check.execute("PRAGMA foreign_key_check").fetchall()
check.close()
if result != "ok":
    raise SystemExit(f"backup integrity failed: {result}")
if foreign_key_violations:
    raise SystemExit(
        f"backup foreign-key violations: {len(foreign_key_violations)}"
    )
print("backup integrity: ok")
print("backup foreign-key violations: 0")
PY
sudo chown root:root "$BACKUP"
sudo chmod 0600 "$BACKUP"
sudo sha256sum "$BACKUP"

sudo install -d -o root -g root -m 0700 "$PROTECTED_BACKUP_DIR"
sudo install -o root -g root -m 0600 "$BACKUP" "$PROTECTED_BACKUP"
sudo test "$(sha256sum "$BACKUP" | cut -d ' ' -f1)" = \
  "$(sha256sum "$PROTECTED_BACKUP" | cut -d ' ' -f1)"
sudo sha256sum "$BACKUP" "$PROTECTED_BACKUP"

sudo test "$(stat -c '%U:%G:%a' "$CONTROL_ENV_CANDIDATE")" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' "$AGENT_ENV_CANDIDATE")" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' "$SPEAKER_MODEL_ENV_CANDIDATE")" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' "$GATEWAY_ENV_CANDIDATE")" = "root:root:600"
sudo grep -qx 'ENDPOINTING_MIN_DELAY_S=0.90' "$AGENT_ENV_CANDIDATE"
sudo grep -qx 'ENDPOINTING_MAX_DELAY_S=1.50' "$AGENT_ENV_CANDIDATE"
sudo grep -qx 'FALSE_INTERRUPTION_TIMEOUT_S=1.70' "$AGENT_ENV_CANDIDATE"
sudo grep -qx 'INTERRUPT_SEMANTIC_ENABLED=true' "$AGENT_ENV_CANDIDATE"
sudo grep -qx 'INTERRUPT_SEMANTIC_MODEL=qwen-flash' "$AGENT_ENV_CANDIDATE"
sudo grep -qx 'INTERRUPT_SEMANTIC_TIMEOUT_S=0.6' "$AGENT_ENV_CANDIDATE"
for current_env in "$CONTROL_ENV" "$AGENT_ENV" "$SPEAKER_MODEL_ENV"; do
  sudo test -e "$current_env"
  sudo test "$(stat -c '%U:%G:%a' "$current_env")" = "root:root:600"
done
sudo install -o root -g root -m 0600 "$CONTROL_ENV" "$CONTROL_ENV_BACKUP"
sudo install -o root -g root -m 0600 "$AGENT_ENV" "$AGENT_ENV_BACKUP"
sudo install -o root -g root -m 0600 "$SPEAKER_MODEL_ENV" "$SPEAKER_MODEL_ENV_BACKUP"
if sudo test -e "$GATEWAY_ENV"; then
  sudo test "$(stat -c '%U:%G:%a' "$GATEWAY_ENV")" = "root:root:600"
  sudo install -o root -g root -m 0600 "$GATEWAY_ENV" "$GATEWAY_ENV_BACKUP"
fi
sudo sha256sum "$CONTROL_ENV_BACKUP" "$AGENT_ENV_BACKUP" "$SPEAKER_MODEL_ENV_BACKUP"
if sudo test -e "$GATEWAY_ENV_BACKUP"; then
  sudo sha256sum "$GATEWAY_ENV_BACKUP"
fi
sudo install -o root -g root -m 0600 "$CONTROL_ENV_CANDIDATE" "$CONTROL_ENV"
sudo install -o root -g root -m 0600 "$AGENT_ENV_CANDIDATE" "$AGENT_ENV"
sudo install -o root -g root -m 0600 "$SPEAKER_MODEL_ENV_CANDIDATE" "$SPEAKER_MODEL_ENV"
sudo install -o root -g root -m 0600 "$GATEWAY_ENV_CANDIDATE" "$GATEWAY_ENV"
```

保护副本放在 root-only `/var/backups/memoria`，避免与容器 bind 目录共享暴露面；`/var/lib/memoria` 中的原始快照继续保留，作为独立的第二份回滚副本。先在可信运维环境生成 Control API、Agent、Speaker Model 与 gateway 四份候选 env；`split_production_env.py` 只做最小权限分流，不能替代 endpointing 精确值门禁。再执行上述“校验候选 → 备份已有 env → 安装候选”顺序。前三份旧 env 是既有 runtime 的强制前提；gateway 只在首次接入小程序前不存在，因此它单独条件备份。数据库、候选 env 和已有 env 备份都必须为 `root:root 0600`，不得为了容器读取而放宽权限。

### 4. 原子激活 runtime

```bash
sudo test "$(stat -c '%U:%G:%a' /etc/memoria-control-api.env)" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' /etc/memoria-agent.env)" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' /etc/memoria-speaker-model.env)" = "root:root:600"
sudo test "$(stat -c '%U:%G:%a' /etc/memoria-miniprogram-gateway.env)" = "root:root:600"
sudo ln -s "releases/$RELEASE_TAG" "/opt/memoria/.current.$RELEASE_TAG"
sudo mv -Tf "/opt/memoria/.current.$RELEASE_TAG" /opt/memoria/current

RUNTIME_COMPOSE_DIR=/opt/memoria/current
DATA_COMPOSE_DIR=/opt/memoria/current/infra

# 首次部署时让 runtime Compose 创建带有正确 Compose label 的共享网络。
# 不要手工执行 `docker network create memoria_default`。
if ! sudo docker network inspect memoria_default >/dev/null 2>&1; then
  cd "$RUNTIME_COMPOSE_DIR"
  MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  sudo -E docker compose -f docker-compose.production.yml create --no-build
fi

# data compose 的项目目录必须是 infra；否则相对 bind mount 可能被创建为目录。
sudo docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" config --quiet
sudo docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" up -d --no-build
sudo docker compose --project-directory /opt/livekit \
  -f /opt/livekit/compose.yml up -d

cd "$RUNTIME_COMPOSE_DIR"
MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
sudo -E docker compose -f docker-compose.production.yml up -d --no-build
sudo docker ps --filter name=memoria
```

`memoria-data.production.yml` 的脚本 bind mount 已设置 `create_host_path: false`；路径或项目目录错误时应立即失败，不得让 Docker 静默创建同名目录。后续发布若共享网络已存在，跳过 `create` 分支即可，但仍必须保留 `DATA_COMPOSE_DIR=/opt/memoria/current/infra`。

此时 `/var/www/memoria-h5` 仍必须指向旧 H5。

### 5. 强制 Provider 与 readiness 门禁

```bash
sudo /opt/memoria/current/scripts/refresh_readiness.sh
curl -fsS http://127.0.0.1:8791/health/ready
```

成功输出必须绑定本次唯一 tag，例如：

```text
livekit_smoke_test PASS: authenticated room-service access
provider_smoke_test PASS: FunASR, Qwen, Doubao, InterruptSemantic
readiness refresh PASS: $RELEASE_TAG (qwen)
```

`Doubao` 通过必须同时满足：双向增量文本合成返回非空 24 kHz、单声道、
PCM signed 16-bit little-endian 音频，字级时间戳非空且单调，并将同一段合成音频降采样后
送入 FunASR，最终文本命中测试语义。任何一项失败都不能写入 readiness evidence。
`InterruptSemantic` 还必须用 `qwen-flash` 通过五类严格枚举样本：真实污染、
控制词+真实内容、引用助手原话的追问、明确问题，以及控制词+助手回声；任一返回
`UNSURE`、非法枚举或与预期不符均不得写入 readiness evidence。

`SKIP`、只验证变量存在或单独 HTTP 200 均不算通过。readiness evidence 写入 SQLite，绑定 release、provider 与 UTC 时间；同 release 重启保持，新 release 必须重跑，24 小时后过期。刷新 timer 每 12 小时执行：

权限边界固定为：Agent one-off 只运行 `python -m scripts.verify_env`，只读取 `/etc/memoria-agent.env`；它不持有 `MEMORIA_AUTH_SECRET`，也不负责写入 readiness。随后由 Control API one-off 运行镜像内的 `python -m scripts.mark_readiness`，从 `/etc/memoria-control-api.env` 读取该 secret，向容器内 Control API 提交 smoke evidence 并立即复查 `/health/ready`。两次 one-off 都使用 `--no-deps`，不会为了门禁重新启动依赖服务。

```bash
sudo systemctl enable --now memoria-readiness-refresh.timer
sudo systemctl status memoria-readiness-refresh.timer
```

### 5.1 自建 LiveKit 安装、升级与回滚

`/opt/livekit/compose.yml` 使用固定镜像版本，`/opt/livekit/livekit.yaml` 保存生产 key pair，必须为 `root:root 0600`。首次安装或升级时从当前 release 执行：

```bash
sudo install -d -o root -g root -m 0700 /opt/livekit
sudo install -o root -g root -m 0644 \
  infra/livekit-compose.production.yml /opt/livekit/compose.yml
sudo install -o root -g root -m 0600 \
  /path/to/filled-livekit.yaml /opt/livekit/livekit.yaml
sudo docker network inspect memoria_default >/dev/null
sudo docker compose --project-directory /opt/livekit \
  -f /opt/livekit/compose.yml config --quiet
sudo docker compose --project-directory /opt/livekit \
  -f /opt/livekit/compose.yml pull
sudo docker compose --project-directory /opt/livekit \
  -f /opt/livekit/compose.yml up -d
sudo docker ps --filter label=com.docker.compose.project=memoria-livekit
sudo ss -lntup | grep -E ':(7880|7882|8444)\b'
sudo awk '/^[[:space:]]*logging:/ {found=1; print; next} \
  found && /^[[:space:]]*level:/ {print; exit}' /opt/livekit/livekit.yaml
sudo docker logs --since 5m memoria-livekit-livekit-1 2>&1 | wc -l
```

升级前将 `compose.yml` 与 `livekit.yaml` 备份到 `/var/backups/memoria` 的 root-only 目录。生产必须保持 `logging.level: warn`，不得把可能包含完整 SDP 或 API key 的 INFO 日志作为常规验收输出。回滚时恢复两份文件，重新执行 `sudo docker compose --project-directory /opt/livekit -f /opt/livekit/compose.yml up -d`，随后运行 `sudo nginx -t`；只有检查通过才 reload Nginx，并重跑 readiness。

### 6. 检查 Nginx 与证书

线上文件位置：

- `/etc/nginx/conf.d/memoria-limits.conf`
- `/etc/nginx/snippets/memoria-http.conf`
- `/etc/nginx/snippets/memoria-https.conf`
- `/etc/nginx/snippets/memoria-miniprogram-media.conf`
- `/etc/nginx/snippets/memoria-livekit.conf`
- `/etc/nginx/snippets/memoria-site-common.conf`
- `/etc/nginx/sites-enabled/memoria`
- `/etc/nginx/sites-enabled/wms`
- `/etc/nginx/stream-conf.d/memoria-rtc.conf`（仓库对应 `infra/nginx-memoria-stream.conf`）
- `/etc/nginx/modules-enabled/50-mod-stream.conf`（由 `libnginx-mod-stream` 提供）
- `/etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx`

`memoria` 是 sites-enabled 下的 root-owned 0644 常规文件；`wms` 是指向
`/etc/nginx/sites-available/wms` 的软链，备份时必须使用 `cp -L` 解引用真实配置。8443 的
stream mux 依赖 `libnginx-mod-stream`；`memoria` 中必须保留 `127.0.0.1:9443 ssl` 的 IP
默认虚拟主机和域名 SNI 虚拟主机，LiveKit Compose 必须把容器 8443 只映射到主机
`127.0.0.1:8444`。`/rtc`、`/agent` 与 `/twirp/` 必须关闭 access log，避免短期 participant
JWT 进入 query-string 日志。

小程序媒体的标准 443 入口必须复用仓库
`infra/nginx-memoria-miniprogram-media.conf`：先将其安装为
`/etc/nginx/snippets/memoria-miniprogram-media.conf`，再仅在 WMS 的 443 `server` 内、
最终 `location / { return 404; }` 之前增加：

```nginx
include /etc/nginx/snippets/memoria-miniprogram-media.conf;
```

不得 include 完整的 `memoria-https.conf`，否则会把 H5、Control API 等额外路由一并迁入
WMS 443。443 路由只作为备案/网络放行后的候选；当前生产
`MINIPROGRAM_MEDIA_GATEWAY_URL` 保持：

```text
wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media
```

先备份 `/etc/nginx/sites-enabled/wms`、共享 snippet 与
`/etc/memoria-control-api.env` 到 root-only 回滚目录。安装新文件后执行：

注册和登录必须分别命中精确 location：`/memoria-api/v1/auth/register`、`/memoria-api/v1/auth/login`，两者均使用 `client_max_body_size 4k` 与 `limit_req zone=memoria_session burst=3 nodelay`。`20260716-225754` 的真实 Omni SDP 只属于历史兼容/A-B 证据；当前 H5 不暴露 Omni 入口。若后续恢复该隔离能力，应用层仍须执行 64 KiB、所有权和每会话两次交换限制。安装任何 `/memoria-api/v1/sessions/` 专用块仍须按本节先备份、`nginx -t`，成功后才 reload。

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo openssl x509 \
  -in /etc/letsencrypt/live/memoria-ip/fullchain.pem \
  -noout -issuer -dates -fingerprint -sha256 -ext subjectAltName
openssl s_client -connect 122.51.108.140:8443 \
  -servername 122.51.108.140 </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -ext subjectAltName
openssl s_client -connect 122.51.108.140:8443 \
  -servername aigcnice.com </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -ext subjectAltName
```

只有 `nginx -t` 成功才允许 reload。随后只重建 Control API 以读取新的媒体 URL，不切 H5
公网软链，不重启 Gateway/Agent/WMS。回滚时恢复 WMS 配置和 Control API env、运行
`nginx -t` 后 reload，并只重建 Control API。

### 既有服务边界

新服务器保留既有 WMS 数据、配置和 `enabled` 状态。默认运行时可为 `active / enabled`；
产品负责人明确授权把资源暂时留给 Memoria 时，只允许执行 `systemctl stop wms.service`，
保持 `inactive / enabled`，不得 disable、删除 `/opt/wms` 或清理任何 WMS 数据。Nginx 的
WMS 静态路由和 443 server 仍保留。PocketSparks、Goods Invoice 与 MySQL 未作为 Memoria
依赖启动；若旧目录或容器存在，只能保留数据，禁止顺手删除。不得执行
`docker compose down -v`。

只读状态检查：

```bash
sudo docker ps --filter label=com.docker.compose.project=pocketsparks
sudo systemctl is-active wms.service
sudo systemctl is-enabled wms.service
sudo ss -ltnp | grep -E ':(8788|18080|19000|15432)\b' || true
```

只有在明确决定恢复旧项目后，才重新加入其精确 Nginx snippet 并先通过 `nginx -t`；恢复操作不得覆盖 Memoria 或 WMS 的根路径与 `/wms/` 路由。

### 7. 最后原子激活 H5

前六步全部通过后才执行：

```bash
sudo test -f "$H5_DIR/index.html"
sudo find "$H5_DIR" -maxdepth 2 -type f \
  \( -name index.html -o -name '*.js' -o -name '*.css' \) \
  -exec sha256sum {} +
sudo ln -s "memoria-releases/$RELEASE_TAG" "/var/www/.memoria-h5.$RELEASE_TAG"
sudo mv -Tf "/var/www/.memoria-h5.$RELEASE_TAG" /var/www/memoria-h5
sudo readlink -f /var/www/memoria-h5
```

`mv -T` 在同一文件系统内完成原子替换；不得用覆盖目录内容的方式激活。

紧接着验证历史 release 的每个静态资源均可经新软链返回。`/memoria-h5/assets/` 的 Nginx location 对缺失文件明确返回 404，不会落到 SPA fallback；该检查因此覆盖旧主脚本未在 `index.html` 中直接列出的所有动态 import。任一请求失败时立即按“回滚”章节切回上一个 H5 release，先保留失败候选目录供排查。

```bash
sudo bash -ceu '
release_root=/var/www/memoria-releases
candidate=$1
base=https://122.51.108.140:8443

for source in "$release_root"/*/assets; do
  [ -d "$source" ] || continue
  [ "$(readlink -f "$source/..")" = "$(readlink -f "$candidate")" ] && continue
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    test -f "$candidate/assets/$relative"
    curl -fsSI --connect-timeout 5 --max-time 15 \
      --resolve 122.51.108.140:8443:127.0.0.1 \
      "$base/memoria-h5/assets/$relative" >/dev/null
  done < <(find "$source" -type f -print0)
done
' bash "$H5_DIR"
```

## 公网上线验收

### HTTPS、SPA 与健康检查

```bash
curl -fsS https://122.51.108.140:8443/
curl -fsS https://122.51.108.140:8443/memoria-h5/
curl -fsS https://122.51.108.140:8443/memoria-h5/arbitrary-spa-route
curl -fsS https://122.51.108.140:8443/memoria-api/health/live
curl -fsS https://122.51.108.140:8443/memoria-api/health/ready
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://122.51.108.140:8443/memoria-api/internal/
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://122.51.108.140:8443/pocketsparks/
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://122.51.108.140:8443/goods-invoice/
curl -fsS https://122.51.108.140:8443/wms/
curl -fsS https://aigcnice.com/wms/
```

验收标准：公网 8443 的根 H5、兼容 H5、SPA、live、ready 和 WMS 静态页均为 200；ready 的 release 必须等于本次唯一 `RELEASE_TAG`、LLM 为 `qwen`、TTS 为 `doubao` 且 9 项 core check 全 ready；internal、PocketSparks 与 Goods Invoice 原路径为 404；IP 证书 SAN 必须精确包含 `122.51.108.140`，域名 SNI 必须返回包含 `aigcnice.com` 与 `www.aigcnice.com` 的域名证书。`/rtc`、`/agent`、`/twirp/` 必须命中自建 LiveKit，真实浏览器 participant 必须为 `active` 且 `connectionType=tcp` 或 `udp`，不能是 `unknown`。服务器本机用 SNI/loopback 额外确认 443 根路径仍由 WMS 虚拟主机提供。WMS 应为 `enabled`；服务在正常运行期为 `active`，明确的资源让渡期允许为 `inactive`，但 Memoria 不得改动其目录或数据。

标准 443 候选路由仍须使用无效 header ticket 完成 WebSocket Upgrade，并由 Gateway 按协议
关闭为 `4401`；HTTP `404` 表示 WMS 443 未安装精确媒体路由。它只有在同一真机关闭 VPN 后
完成 `ack_sent → ready_sent → first_playback → listening`，才允许替代当前 8443 下发地址；
自动 smoke 不得替代。

### 身份、隔离与持久化

1. `POST /memoria-api/v1/auth/register` 创建账号；同一规范化用户名再次注册返回 409 和安全提示。
2. `POST /memoria-api/v1/auth/login` 在 Control API 重启后仍返回原 `user_id`；`GET /v1/auth/me` 返回相同账号、用户名和 `account_type=registered`。
3. 对已有匿名 Bearer 执行注册时 `user_id` 不变，原消息和 Profile 仍可读取；新用户不再由 H5 自动创建匿名身份。
4. 只读检查 `accounts.password_hash` 以 `scrypt$` 开头且不含测试明文；未知用户名与错误密码都返回相同 401。
5. 无 Bearer 的 memory/session 请求返回 401；用户 B 访问用户 A 的数据或会话控制返回 403/404，不泄露资源是否存在。
6. 写入含邮箱、手机号或证件号的消息/Profile 后，只读检查 SQLite，确认保存的是脱敏文本。
7. 双方消息只持久化权威字幕，分段字幕与最终字幕不重复。
8. 按北京时间生成每日回顾，断言 `source=qwen`。
9. 修改 Profile 与四个偏好（含“过滤明显旁人（实验）”），创建 session 并记录 stop/recovery；重启 Control API 后，账号、消息、回顾、Profile、偏好、会话控制和 readiness evidence 均保持。
10. 同 release 重启保持 ready；新 release 在 Provider smoke 前必须为 not ready。
11. `POST /v1/archive/exports` 必须重新验证密码，导出 manifest 可复算且不包含密码哈希、声纹模板密文、对象密钥/路径、供应商 voice ID 或盲测映射。
12. `POST /v1/archive/deletion-requests` 必须要求精确确认短语；执行后旧 Bearer、再次登录、旧 session 和迟到 Agent 写入分别返回 401/410，其他账户保持可用。

### 人生档案、人格、声纹与声音治理

1. owner 的 confirmed 记忆能出现在人生时间线、搜索和实时回答；candidate/guest/uncertain 不得进入私人上下文或 Persona 学习。
2. 候选记忆确认、纠正、质疑和撤销同步更新人物、时间线、知识与检索投影，并保留来源事件。
3. owner 合法声学指标可沉淀语速/停顿统计；低质量、越界、非有限值、guest 与 uncertain 样本均不得污染 Persona。
4. 声纹登记与声音复刻分别授权、分别存储和分别撤销；声纹相似不能直接授权导出、删除或其他敏感动作。
5. 声音 A/B 页面只显示槽位，不暴露候选映射；主观盲测通过但服务端质量探针 pending 时不显示激活入口。
6. 未授权、过期、撤销、主观失败或质量失败的声音档案不能解析给 Agent；clone 只在首音频前允许一次设计基线回退。
7. 豆包暂未确认自助删除合同。供应商控制台人工删除并取得工单引用后，运维人员才可使用
   仅存在于 Control 环境的 `MEMORIA_VOICE_CLEANUP_TOKEN` 调用
   `POST /v1/voices/profiles/{profile_id}/provider-deletion-confirmations`，请求体只包含
   `account_id` 与不含敏感正文的 `evidence_reference`。调用前确认 profile 已 revoked 且状态为
   pending/failed；调用后核验 `deletion_status=completed` 与
   `voice_profile.provider_deletion_confirmed` evidence。不得把该 token 下发 Agent、H5 或日志，
   也不得在未实际删除供应商资产时用此入口解锁账户删除。

### 真实语音与 H5

1. 点击吉祥物并授权麦克风，确认页面保持 connecting，直到收到显式 `assistant_state: ready`。
2. 确认首次 ready 后才开始欢迎语；说固定中文，用户转写、助手回答和助手字幕均出现。
3. 确认眼睛、嘴巴和胸灯随对话情绪变化。
4. 回答播放中点击停止，声音立即停止。
5. 关闭语音回应后只显示文字；重新开启后恢复远端音频。
6. 静音后模拟断线并恢复，静音状态保持；重连超过 10 秒回到可重试状态。
7. 模拟 Agent 不发布 ready，45 秒后会话关闭且可再次点击开始。
8. 首页、回顾页和个人页无 console error、CORS error 或横向溢出。
9. “过滤明显旁人（实验）”默认开启：formal guest/owner mismatch 与明确的 shadow guest 普通话轮及“停一下”不能控制会话；shadow/formal ambiguous 为避免误静音主人可普通聊天，但保持 non-owner/uncertain、`history_eligible=false`。关闭后访客可聊天和打断，但不会取得 owner 权限。
10. formal owner 与 `shadow_owner_candidate` 的双方终稿可进入主人历史；访客、ambiguous、无档案、authority 不可用及缺少 `history_eligible` 的话轮与对应 AI 回复只实时显示，不得落库或进入自动摘要。

## 回滚

禁止在 runbook 中长期硬编码“当前”回滚版本。每次发布在切软链前记录真实目标，并把
四份服务 env 备份到同一 release tag 命名的 root-only 文件：

```bash
PREV_RUNTIME_TAG="$(basename "$(readlink -f /opt/memoria/current)")"
PREV_H5_TAG="$(basename "$(readlink -f /var/www/memoria-h5)")"
CONTROL_ENV_BACKUP="/var/backups/memoria/memoria-control-api.env-pre-$RELEASE_TAG"
AGENT_ENV_BACKUP="/var/backups/memoria/memoria-agent.env-pre-$RELEASE_TAG"
SPEAKER_MODEL_ENV_BACKUP="/var/backups/memoria/memoria-speaker-model.env-pre-$RELEASE_TAG"
GATEWAY_ENV_BACKUP="/var/backups/memoria/memoria-miniprogram-gateway.env-pre-$RELEASE_TAG"

sudo test -d "/opt/memoria/releases/$PREV_RUNTIME_TAG"
sudo test -d "/var/www/memoria-releases/$PREV_H5_TAG"
sudo docker image inspect "memoria-agent:$PREV_RUNTIME_TAG" >/dev/null
sudo docker image inspect "memoria-control-api:$PREV_RUNTIME_TAG" >/dev/null
sudo docker image inspect "memoria-speaker-model:$PREV_RUNTIME_TAG" >/dev/null
sudo test "$(stat -c '%U:%G:%a' "$CONTROL_ENV_BACKUP")" = root:root:600
sudo test "$(stat -c '%U:%G:%a' "$AGENT_ENV_BACKUP")" = root:root:600
sudo test "$(stat -c '%U:%G:%a' "$SPEAKER_MODEL_ENV_BACKUP")" = root:root:600
```

H5-only 故障只切回发布前记录的 H5；runtime、Provider 或 readiness 故障必须先恢复
Control API、Agent 与 Speaker Model env；如果存在 gateway 备份也一并恢复，再切回发布前
runtime。旧 runtime 不能读取新 release 的 provider 配置：

```bash
sudo ln -s "memoria-releases/$PREV_H5_TAG" \
  "/var/www/.memoria-h5.rollback-$RELEASE_TAG"
sudo mv -Tf "/var/www/.memoria-h5.rollback-$RELEASE_TAG" /var/www/memoria-h5

sudo install -o root -g root -m 0600 \
  "$CONTROL_ENV_BACKUP" /etc/memoria-control-api.env
sudo install -o root -g root -m 0600 \
  "$AGENT_ENV_BACKUP" /etc/memoria-agent.env
sudo install -o root -g root -m 0600 \
  "$SPEAKER_MODEL_ENV_BACKUP" /etc/memoria-speaker-model.env
if sudo test -e "$GATEWAY_ENV_BACKUP"; then
  sudo install -o root -g root -m 0600 \
    "$GATEWAY_ENV_BACKUP" /etc/memoria-miniprogram-gateway.env
else
  sudo rm -f /etc/memoria-miniprogram-gateway.env
fi

sudo ln -s "releases/$PREV_RUNTIME_TAG" \
  "/opt/memoria/.current.rollback-$RELEASE_TAG"
sudo mv -Tf "/opt/memoria/.current.rollback-$RELEASE_TAG" /opt/memoria/current

cd /opt/memoria/current
MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
sudo -E docker compose -f docker-compose.production.yml \
  up -d --no-build --wait --wait-timeout 120
sudo /opt/memoria/current/scripts/refresh_readiness.sh
curl -fsS http://127.0.0.1:8791/health/ready
```

随后验证旧 H5、API live/ready、匿名兼容、创建 session 和持久数据。不要自动回滚
SQLite、PostgreSQL 或 MinIO；只有数据格式确实不兼容时，才在另存当前状态后恢复对应
备份，并保留失败 release 的原始副本。

## 日常运维

- 每日监控 API live/ready、`memoria-readiness-refresh.timer` 和 `snap.certbot.renew.timer`。
- 每日确认 WMS 保持 `enabled`，其 active/inactive 状态符合当前资源分配决策；确认小程序当前
  8443 媒体入口可用，443 候选路由不被误设为生产下发地址，旧项目没有被意外启动并占用
  Memoria 端口。
- 证书续期后验证 SAN、有效期、deploy hook 和 Nginx reload 日志。
- 每次发布记录 release tag、镜像 ID、H5/Nginx SHA-256、证书指纹、两份 SQLite 快照 SHA-256、四份 env 备份 SHA-256、完整性与 foreign-key 检查、激活时间和回滚点；不得记录 secret。
- 200 条真实中文录音、AEC 设备矩阵和第 21 章 SLO 是规模化上线门禁，不阻塞当前 H5 成品交付。
