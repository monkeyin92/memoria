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
- LiveKit：自建 `livekit/livekit-server:v1.13.5`，位于 `/opt/livekit`，Compose project 为 `memoria-livekit`
- LiveKit 信令：`wss://122.51.108.140:8443`，经 Nginx `/rtc`、`/agent` 转发到 `127.0.0.1:7880`
- LiveKit Twirp API：`https://122.51.108.140:8443/twirp/`，转发到 `127.0.0.1:7880`
- LiveKit 媒体：服务器保留 `7882/UDP` 监听，但当前未开放对应云安全组；公网统一回退到与 HTTPS 复用的 `8443/TCP`，Agent 通过 `memoria_default` 内部网络走 UDP
- SQLite：`/var/lib/memoria/memoria.sqlite3`
- 终身档案：独立同机 PostgreSQL 17 + pgvector 0.8.1，Compose project 为 `memoria-data`
- 对象存储：独立同机 MinIO，档案/声音两个 bucket 分权并启用版本控制，无公网端口
- Runtime：版本目录位于 `/opt/memoria/releases/`，`/opt/memoria/current` 原子软链指向当前 release
- H5：版本目录位于 `/var/www/memoria-releases/`，`/var/www/memoria-h5` 原子软链指向当前 release
- 已退役的原生小程序媒体兼容回滚入口（不是当前生产小程序或新设备默认路径）：
  `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`。标准 `443` 保留同路径
  精确路由；两者的 loopback 上游均为 `127.0.0.1:8792`。该路径仅保留给明确授权的 legacy
  回滚，不能重新作为小程序实时媒体入口或新设备媒体入口；现行边界见 ADR-0035。

Memoria 使用独立静态资源/API 路径、回环端口、Compose project 和限流 zone。新服务器的
443 仍由既有 WMS 虚拟主机拥有，只额外 include
`/etc/nginx/snippets/memoria-miniprogram-media.conf` 保留精确的 legacy 小程序媒体回滚 WSS；
它不属于当前小程序产品链。WMS 根路径、
`/wms/` 和其他 404 边界保持不变。8443 继续由 Nginx stream 预读协议，TLS 流量转到
`127.0.0.1:9443` 的 Memoria HTTPS server，原生 ICE/TCP 转到 `127.0.0.1:8444` 后进入
LiveKit 容器的 8443；H5、Control API 与 LiveKit 正式入口仍为 8443。公网
`/memoria-api/internal/` 固定返回 404；WMS 数据与配置必须保留，服务可在明确授权的资源让渡期
保持 `inactive / enabled`。当前生产版本与验收结论见
`HANDOFF.md`，历史迁移与热修证据保留在 `docs/releases/`。

终身档案迁移到 PostgreSQL + 对象存储后的备份、PITR、对象清单与联合恢复门禁见 [`archive-backup-restore-runbook.md`](./archive-backup-restore-runbook.md)。现有 SQLite 发布快照只覆盖旧主库，不得被描述为终身档案生产恢复方案。

> P0.5、P1-P6 的稳定账号、终身记忆、Persona、SpeakerAuthority、VoiceProfile 与账户治理底座随 `20260720-140053` 部署。当前 runtime/H5 以 `HANDOFF.md` 和服务器软链为准。CAM++ 仍为 shadow-only，正式声纹/复刻声音不得在真人授权与盲测前激活。当前 PostgreSQL、WAL archive、MinIO 和备份均同机，没有异地副本/KMS/PITR，不能承诺“永不丢失”。

## TLS 与自动续期

新服务器公网 IPv4 入口使用 Let's Encrypt 短期证书，SAN 应包含 `122.51.108.140`；`aigcnice.com` 与 `www.aigcnice.com` 使用同机域名证书。证书有效期、issuer 与 SAN 必须在每次发布时通过下方命令从服务器实时读取，不能依赖本文的历史日期。`127.0.0.1:9443` 的两个证书虚拟主机复用 `/etc/nginx/snippets/memoria-site-common.conf`，IP/无 SNI 默认选择 IP 证书，域名 SNI 选择域名证书。`snap.certbot.renew.timer` 为 enabled/active；deploy hook 安装于 `/etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx`，先运行 `nginx -t`，只有成功才 reload Nginx。

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
`/etc/memoria-agent.env`、`/etc/memoria-speaker-model.env`、
`/etc/memoria-miniprogram-gateway.env`、`/etc/memoria-device-media-gateway.env` 和可选的
`/etc/memoria-media-edge.env`，权限都必须是 `root:root 0600`。使用
`scripts/split_production_env.py` 从 root-only 运维源生成候选文件；该脚本只分流已有值，
不会应用默认值。首次 P0-P6 升级应使用 `scripts/prepare_production_upgrade_env.py`，
普通发布则必须从当前 root-only env 复制并显式核对本文列出的 endpointing 值；
仓库、H5 bundle、发布清单、日志和本文档都不得出现源文件或 secret 值。gateway env 只包含
LiveKit 接入凭据、gateway ticket 签名材料和媒体适配配置；它不包含 `MEMORIA_AUTH_SECRET`、
档案对象存储密钥、DASHSCOPE 或 Agent capability token。

Runtime Profile 当前使用 HMAC-SHA256；root-only 合并源中的
`MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET` 与 `MEMORIA_RUNTIME_PROFILE_VERIFY_KEY` 必须使用同一份
至少 32 字符的随机材料。拆分后 signing 键名只进入 Control，verify 键名只进入 Agent；生产
缺少任一键或两者不一致时 `split_production_env.py` 必须 fail closed。

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
MEMORIA_EVOLUTION_CONTROL_TOKEN
MEMORIA_EVOLUTION_VALIDATOR_TOKEN
```

`MEMORIA_EVOLUTION_DATABASE_URL` 必须使用独立的 `memoria_evolution` PostgreSQL 角色，不能回退到
`MEMORIA_ARCHIVE_DATABASE_URL`。候选控制与独立验证分别使用上述两个 token；验证器不得与业务 Agent
共用凭据。`MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256` 必须对应本次发布清单的可信根摘要。
`MEMORIA_EVOLUTION_RUNTIME_PROMPT_FAMILIES` 是精确任务族 allowlist，不是通配符；当前生产默认只允许
`weather`。即使候选已经写入或被直接改成 `canary/stable`，只有 `low` risk、`prompt` 类型且命中该
allowlist 的候选才可被运行时解析。`identity_privacy`、`privacy`、`permission(s)`、
`speaker_authority`、`tool_permission` 和 `voice_control` 属于确定性保护路径，配置阶段即拒绝作为
运行时 prompt 开放。

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

### Evolution PostgreSQL 首次安装与已有数据卷升级

`memoria_evolution` 是运行时控制角色，只保留 `USAGE` 以及已安装表的最小 DML 权限，
不应拥有 `public` schema 的 `CREATE`。新的 data Compose 已把
`services/evolution/postgres_schema.sql` 作为第二个 init 文件挂载；生产 Control API
启动时只检查表、`ENABLE/FORCE ROW LEVEL SECURITY` 和 controller policy，不再用运行角色
执行 DDL。

已有 PostgreSQL 数据卷不会自动重新执行 `/docker-entrypoint-initdb.d`。生产唯一升级入口是本文
“3.1 PostgreSQL forward-only schema 升级”：它先完成联合备份和 env 备份，再停止 writer、等待
候选 PostgreSQL healthy，并从 root-only `/etc/memoria-postgres.env` 读取密码。禁止在命令行传密码或
脱离 3.1 单独运行升级脚本。脚本幂等地创建角色、安装 schema/强制 RLS 并撤销 evolution schema
DDL；若 readiness 返回 `evolution_store=unavailable`，禁止切流，应先检查角色、表 owner、policy
和 `relforcerowsecurity`，不要临时给运行角色补 `CREATE` 或 `BYPASSRLS`。

当前生产 runtime 的非 secret 配置：

```dotenv
ENVIRONMENT=production
LLM_PROVIDER=bailian_deepseek
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
DEEPSEEK_FAST_MODEL=deepseek-v4-flash
DEEPSEEK_DEEP_MODEL=deepseek-v4-flash
INTERRUPT_SEMANTIC_ENABLED=true
INTERRUPT_SEMANTIC_MODEL=deepseek-v4-flash
INTERRUPT_SEMANTIC_TIMEOUT_S=1.2
MINIPROGRAM_KWS_ENABLED=false
MINIPROGRAM_KWS_MODEL_DIR=/data/models/vosk-model-small-cn-0.22
MINIPROGRAM_KWS_KEYWORDS_FILE=/app/infra/kws/keywords.txt
MINIPROGRAM_KWS_MIN_CONFIDENCE=0.65
DASHSCOPE_SUMMARY_MODEL=deepseek-v4-flash
MEMORIA_MEMORY_EXTRACTION_MODEL=deepseek-v4-flash
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
ENDPOINTING_MIN_DELAY_S=1.50
ENDPOINTING_MAX_DELAY_S=2.20
ENDPOINTING_ALPHA=0.85
INTERRUPTION_MIN_DURATION_S=0.35
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

生产默认 LLM 和每日回顾均使用百炼 `deepseek-v4-flash`。账号版本发布后，浏览器只接收注册账号 Bearer token 和短期 LiveKit participant token；`/v1/auth/anonymous` 仅用于兼容旧身份并在注册时原地升级。服务端在持久化消息、Profile 或向 Agent/FunASR 传递上下文前统一做 PII 脱敏，所有 memory/session route 均校验 token subject 与资源所有权。账号登录不等于当前说话人是主人，私人档案权限仍需结合 `owner / guest / uncertain` 判定。

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

P0～P6 发布后，Control API `/health/ready` 还必须同时返回以下 10 个 core check：Control DB、Evolution store、LifeArchive、MemoryCatalog、Persona、SpeakerAuthority、VoiceProfile、档案对象存储、声音对象存储和独立 `speaker-model`。前 9 项必须为 `ready`；`speaker-model` 必须实时请求 `/health/ready`，验证 HTTP 200、`status=ready` 和精确 `model_version`。此外，Agent 必须每 10 秒使用独立 capability token 上报 release、boot ID、worker 与 LiveKit 注册状态；心跳缺失、未就绪、版本不符或超过 45 秒都会令 readiness 返回 503。对象存储检查执行最小加密 `put/get/delete` canary；空账户或尚无 active 声音档案可以 ready，但缺组件、数据库/模型异常、版本漂移或对象 canary 失败必须返回 503。开发/离线未配置模型时只允许明确显示 `skipped`，不代表生产 ready。

Compose 的 Control API 容器健康检查固定使用 `/health/live`：Agent 必须先等 Control 的进程可接收心跳，不能拿依赖 Agent 心跳的 `/health/ready` 做启动门禁。相对地，Agent 容器健康检查调用 `python -m services.agent.src.heartbeat --check-health`，它同时验证 LiveKit SDK 本机 `8081` 返回 2xx，以及 `/tmp/memoria-agent-heartbeat.json` 中同一 release 的最近一次已被 Control 接受的 ready 心跳（30 秒内）。POST、鉴权、响应失败或 LiveKit 正在重连都不会刷新该无 secret 的原子状态文件；配合 10 秒检查间隔、3 秒超时和 2 次重试，最迟在最后一次 ready 心跳后的 60 秒内把 Agent 容器标为 unhealthy。Control `/health/ready` 的心跳 freshness 仍为 45 秒，两者分工不变。

Agent 的注册探针绑定当前固定版本 `livekit-agents==1.6.10` 的私有状态：仅当 `_id` 非空且不为 `unregistered`，并且 `_closed / _connecting / _connection_failed` 均表示已连接时才算注册。SDK 的 `8081` 在重连阶段仍可能返回 200，不能单独作为注册证据。升级 LiveKit Agents 前必须重新核对这些字段及重连路径，并同步更新探针契约测试。

生产 Control API 必须以 `uvicorn --no-access-log` 启动，由 Nginx 记录常规访问；签名声音样本路由同时 `access_log off`。这样 query 中的短期样本 token 不会进入 Nginx 或 Uvicorn access log，应用日志也不得自行记录完整 URL。

## P0.5、P1～P6 上线状态与后续门槛

1. `20260720-140053` 已在新服务器部署 pgvector、FORCE RLS、MinIO 版本控制、能力级 token、独立 SpeakerAuthority token、迁移/联合恢复、core readiness 和真实 Provider smoke；当前 runtime/H5 以 `HANDOFF.md` 和服务器软链为准。
2. 当前低成本底座为同机 PostgreSQL、WAL archive、MinIO 和备份；PITR、异地副本与 KMS 仍是下一阶段可靠性门槛，不能把同机恢复演练描述为异地容灾。
3. 使用授权样本完成 SpeakerAuthority 指标报告和历史 CosyVoice 复刻声音真人盲测前，不得激活正式声纹模板或复刻声音；当前豆包主链只使用已审核的原生 TTS 2.0 音色。
4. 账户删除 worker、LiveKit 房间删除权限、对象全版本删除权限和供应商声音删除权限必须同时具备；缺任一权限时删除只能保持 `deleting`，不得伪报完成。

## Agent 自我进化发布门禁

本地 synthetic 三臂和 `--runtime-control-plane` 只验证评测代码、控制面生命周期与 resolver 接线，
不能证明真实任务、真实手机、AEC、弱网或生产账号已经受益。候选从 `canary` 进入 `stable` 前，必须由
独立 evaluator 在固定中文 holdout 上输出无 transcript 的结果包，并执行：

```bash
uv run python scripts/evaluate_self_evolution.py \
  --holdout-results /root/evolution-holdout-results.json \
  --minimum-device-cases 2
```

当前固定数据集为 `evolution-holdout-zh-v1`，canonical `dataset_sha256` 为
`786745165a3a1df963ece667375ca94d23786733721f4bd8460fd9406a6d32a9`。数据集有任何修改时，结果包必须
使用重新计算的摘要；版本相同但摘要不同也会 fail closed。结果包顶层只允许以下字段：

```json
{
  "dataset_version": "evolution-holdout-zh-v1",
  "dataset_sha256": "786745165a3a1df963ece667375ca94d23786733721f4bd8460fd9406a6d32a9",
  "adapter": "independent-device-evaluator-v1",
  "observations": []
}
```

`observations` 必须对 `static / append_only / evolving` 三臂的每个 case 各覆盖一次，且每条使用唯一
`evidence_id`；仅允许 case/arm/evidence mode、result/process/quality、activation/adherence/outcome、
latency 和 token 计数，不得包含 transcript、模型输出、私密上下文或额外字段。命令只有在 safety、
process、retention、negative transfer、正迁移、规则替换、激活/遵循/结果和设备数量全部通过时返回 0；
返回 2 时禁止推进 `stable`。
5. 每个后续候选仍必须通过完整 PostgreSQL 合同、联合恢复、core readiness、Provider smoke、镜像 secret 扫描和真实 H5 浏览器检查，再按“runtime 先、H5 最后”顺序切流；原生 iOS 客户端已从仓库移除。

## 发布原则

H5 必须最后激活。标准顺序是：本机构建并校验工件 → 上传并在服务器验签、导入镜像 → 暂存
release 与 H5、合并 immutable 资源 → 隔离 smoke → 创建并验证数据快照与 env 备份 → 按 release
要求执行 forward-only 数据库升级 → 原子切 runtime → 容器/Provider/readiness 门禁 → Nginx 与证书
检查 → 最后原子切 H5 → 公网验收。这样既避免小内存服务器构建卡死，也避免新 H5 连接尚未
ready 的 runtime。

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
DEVICE_GATEWAY_ENV=/etc/memoria-device-media-gateway.env
MEDIA_EDGE_ENV=/etc/memoria-media-edge.env
POSTGRES_ENV=/etc/memoria-postgres.env
CONTROL_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/control-api.env
AGENT_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/agent.env
SPEAKER_MODEL_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/speaker-model.env
GATEWAY_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/gateway.env
DEVICE_GATEWAY_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/device-gateway.env
MEDIA_EDGE_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/media-edge.env
POSTGRES_ENV_CANDIDATE=/run/memoria-env/$RELEASE_TAG/postgres.env
CONTROL_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-control-api.env-pre-$RELEASE_TAG
AGENT_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-agent.env-pre-$RELEASE_TAG
SPEAKER_MODEL_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-speaker-model.env-pre-$RELEASE_TAG
GATEWAY_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-miniprogram-gateway.env-pre-$RELEASE_TAG
DEVICE_GATEWAY_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-device-media-gateway.env-pre-$RELEASE_TAG
MEDIA_EDGE_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-media-edge.env-pre-$RELEASE_TAG
POSTGRES_ENV_BACKUP=$PROTECTED_BACKUP_DIR/memoria-postgres.env-pre-$RELEASE_TAG
ROLLBACK_RECEIPT=$PROTECTED_BACKUP_DIR/rollback-$RELEASE_TAG.env
```

### 0. Agent 高频代码改动：组件源码薄发布

仅修改 `services/agent/**` 且 `pyproject.toml`、`uv.lock`、`.dockerignore`、
`infra/Dockerfile.agent` 均未变化时，不再打包或上传 2GB 级完整镜像。使用提交绑定的
Agent 源码归档，在生产机基于固定依赖基座构建一层薄镜像；构建过程禁网且不安装依赖，
切流只重建 `agent` 与消费同一代码的 `voice-core-media-bridge`。数据库、Redis、MinIO、
Control API、网关、H5 和固件均不变化。

脚本会拒绝共享 `services/*`、`packages/*`、运行脚本或依赖输入的越界改动；这些改动必须
走完整或协调的多组件发布。薄镜像每次直接派生自固定依赖基座，不从上一张源码薄镜像继续
叠层；失败时自动按切流前 Compose 配置恢复两个容器。正式切流前先执行 dry-run：

```bash
RELEASE_TAG=YYYYMMDD-HHMMSS-agent-change
BASE_IMAGE=memoria-agent:上一健康依赖版本
MEMORIA_RELEASE_COMMIT="$(git rev-parse HEAD)"

bash scripts/deploy_agent_component.sh \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-image "$BASE_IMAGE" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --dry-run

bash scripts/deploy_agent_component.sh \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-image "$BASE_IMAGE" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --cutover
```

生产工件位于 `/opt/memoria/component-releases/$RELEASE_TAG/`，包含源码归档、构建回执、
切流回执和冻结回滚点。验收后仍只保留当前版、紧邻可运行回滚版和当前依赖基座；依赖基座
不是普通候选包，不得在仍有薄镜像引用时删除。

### 1. 多组件或依赖变更：本机构建、打包并增量上传固定工件

生产机资源只允许执行不安装依赖的源码薄层构建，默认禁止在服务器执行完整 `compose build`。依赖未变化时，在本机从上一健康 amd64 镜像做增量构建；`pyproject.toml` 或 `uv.lock` 变化时，仍在本机执行固定依赖的完整 amd64 构建。

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

# Device Media Gateway 使用含 PyAV/libopus 的完整锁定依赖构建，并固定独立镜像 role。
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.miniprogram-gateway \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  --build-arg MEMORIA_IMAGE_ROLE=device-media-gateway \
  -t "memoria-device-media-gateway:$RELEASE_TAG" .

for image in agent control-api speaker-model miniprogram-gateway device-media-gateway; do
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
  "memoria-device-media-gateway:$RELEASE_TAG" \
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
MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256="$(python3 - "$ARTIFACT_DIR/release-manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
digest = manifest.get("digest")
if not isinstance(digest, str) or len(digest) != 64:
    raise SystemExit("release manifest digest is invalid")
print(digest)
PY
)"
if command -v sha256sum >/dev/null 2>&1; then
  MEMORIA_RELEASE_MANIFEST_SHA256="$(sha256sum "$ARTIFACT_DIR/release-manifest.json" | cut -d ' ' -f1)"
else
  MEMORIA_RELEASE_MANIFEST_SHA256="$(shasum -a 256 "$ARTIFACT_DIR/release-manifest.json" | cut -d ' ' -f1)"
fi
printf 'copy this manifest hash into the authenticated server shell: %s\n' \
  "$MEMORIA_RELEASE_MANIFEST_SHA256"
printf 'bind MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256 to manifest digest: %s\n' \
  "$MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256"
```

增量构建要求上一健康 release 的四个既有镜像齐全且共享同一 commit/tag/role；设备媒体镜像仍按
上面的锁定依赖完整构建。脚本会自动对比依赖锁、四个完整 Dockerfile、Speaker Model
requirements/patch/exporter；任一变化都
fail closed，必须走下面的完整镜像构建，不允许继承旧依赖后只换新标签。

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
docker buildx build --platform linux/amd64 --load \
  -f infra/Dockerfile.miniprogram-gateway \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
  --build-arg MEMORIA_IMAGE_ROLE=device-media-gateway \
  -t "memoria-device-media-gateway:$RELEASE_TAG" .
```

Dockerfile 必须从 `uv.lock` 或固定 requirements 导出并安装固定版本与哈希，任何不匹配都令构建失败；不得使用 `latest`。完整构建后同样执行上面的架构校验、H5 build、`docker save` 和 SHA-256 清单生成。

上传使用项目脚本，并先运行 dry-run。脚本只允许上传 `source/images/H5`、三份 SHA-256、
manifest 与 verifier，不会把工件目录里的其他文件带到服务器。若服务器仍保留上一健康 release 的
`images.tar + images.tar.sha256`，脚本先用只读硬链接作为 rsync basis，再按滚动校验只传变化块；
不会原地修改上一份归档。basis 缺失或不在同一文件系统时自动回退完整上传，输出
`release_upload_mode=full`，不降低后续 SHA/manifest 验签：

```bash
bash scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-tag "$BASE_TAG" \
  --dry-run

bash scripts/upload_release_artifacts.sh \
  --artifact-dir "$ARTIFACT_DIR" \
  --remote memoria-prod \
  --release-tag "$RELEASE_TAG" \
  --base-tag "$BASE_TAG"
```

发布成功并确认新 runtime 可直接回滚后，服务器只需保留**当前 runtime** 对应 incoming 中的
`images.tar` 与 `images.tar.sha256` 作为下一次上传 basis；更旧 incoming 仍可按精确清理流程删除。
不要对 basis 使用 `rsync --inplace`，否则中断可能破坏上一版本的可信归档。

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
for image in agent control-api speaker-model miniprogram-gateway device-media-gateway; do
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
sudo bash -cEeu '
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

release、候选 H5 和 immutable union 都已落盘后、切流前运行隔离 server smoke。脚本使用候选
H5、临时 SQLite、`18791/18891`，不会占用在线 Control API 的 `8791`。不得在 `$RELEASE_DIR`
创建前提前执行这个命令：

```bash
sudo "$RELEASE_DIR/scripts/smoke_server_deployment.sh" "$RELEASE_TAG"
```

#### 冻结回滚目标（所有 env 安装和软链切换之前）

回滚目标不能在故障后通过 `current` 重新推断；那时软链可能已经指向失败候选。隔离 smoke 通过后，
立即把旧 runtime/H5、源码 commit、可选 env 是否原先存在，以及 `media-runtime` 原运行状态写入
root-only receipt；同时冻结“旧入口文件 + 新旧 immutable assets 并集”的 H5 回滚目录。receipt 只含
版本、受限目录名、manifest SHA-256 和布尔状态，不含 secret；同一 release 禁止覆盖已有 receipt：

```bash
sudo install -d -o root -g root -m 0700 "$PROTECTED_BACKUP_DIR"
sudo bash -cEeu '
set -o pipefail
release_tag=$1
receipt=$2
release_root=/var/www/memoria-releases
rollback_dirname=rollback-$release_tag
rollback_h5=$release_root/$rollback_dirname
candidate_h5=$release_root/$release_tag

case "$release_tag" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid release tag" >&2; exit 1 ;;
esac
test ! -e "$receipt"
test ! -e "$rollback_h5"

previous_runtime_dir="$(readlink -f /opt/memoria/current)"
previous_h5_dir="$(readlink -f /var/www/memoria-h5)"
previous_runtime_tag="$(basename "$previous_runtime_dir")"
previous_h5_dirname="$(basename "$previous_h5_dir")"
test "$previous_runtime_tag" != "$release_tag"
test "$previous_h5_dirname" != "$release_tag"
test -f "$previous_runtime_dir/.env"
test -f "$previous_h5_dir/memoria-release.json"
test -f "$previous_h5_dir/index.html"
test -d "$candidate_h5/assets" && test ! -L "$candidate_h5/assets"

previous_runtime_commit="$(awk -F= '\''$1 == "MEMORIA_RELEASE_COMMIT" {print $2}'\'' \
  "$previous_runtime_dir/.env")"
read -r previous_h5_release_tag previous_h5_commit < <(
  python3 - "$previous_h5_dir/memoria-release.json" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload["release_tag"], payload["commit"])
PY
)
[[ "$previous_runtime_commit" =~ ^[0-9a-f]{40}$ ]]
case "$previous_runtime_tag" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid previous runtime tag" >&2; exit 1 ;;
esac
case "$previous_h5_dirname" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid previous H5 dirname" >&2; exit 1 ;;
esac
case "$previous_h5_release_tag" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid previous H5 provenance" >&2; exit 1 ;;
esac
[[ "$previous_h5_commit" =~ ^[0-9a-f]{40}$ ]]

for image in agent control-api speaker-model miniprogram-gateway; do
  labels="$(docker image inspect "memoria-$image:$previous_runtime_tag" \
    --format '\''{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'\'')"
  test "$labels" = "$previous_runtime_commit $previous_runtime_tag $image"
done

media_runtime_running_count=0
for service in media-slo-reporter voice-core-media-bridge media-edge; do
  if docker ps -q \
    --filter label=com.docker.compose.project=memoria \
    --filter "label=com.docker.compose.service=$service" | grep -q .; then
    media_runtime_running_count=$((media_runtime_running_count + 1))
  fi
done
case "$media_runtime_running_count" in
  0) media_runtime_was_running=0 ;;
  3)
    echo "active media-runtime requires a five-image release manifest; this flow has four" >&2
    exit 1
    ;;
  *) echo "partial media-runtime state is not releasable" >&2; exit 1 ;;
esac
test -f /etc/memoria-postgres.env && test ! -L /etc/memoria-postgres.env
test "$(stat -c "%U:%G:%a" /etc/memoria-postgres.env)" = root:root:600

present() { if [ -e "$1" ]; then printf 1; else printf 0; fi; }
umask 077
temporary="$(mktemp "$(dirname "$receipt")/.rollback-${release_tag}.XXXXXX")"
temporary_h5="$(mktemp -d "$release_root/.rollback-${release_tag}.XXXXXX")"
rollback_installed=0
cleanup_receipt() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f -- "$temporary"
  rm -rf -- "$temporary_h5"
  if [ "$rollback_installed" = 1 ]; then rm -rf -- "$rollback_h5"; fi
  exit "$status"
}
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap cleanup_receipt EXIT

# Freeze the old entry files together with the candidate append-only asset union.
# A browser that cached either old or new code can then finish loading after rollback.
find "$previous_h5_dir" -mindepth 1 -maxdepth 1 ! -name assets \
  -exec cp -a --no-dereference -- {} "$temporary_h5/" \;
cp -a --no-dereference -- "$candidate_h5/assets" "$temporary_h5/assets"
chown -R root:root "$temporary_h5"
find "$temporary_h5" -type d -exec chmod 0755 {} +
find "$temporary_h5" -type f -exec chmod 0644 {} +
test -z "$(find "$temporary_h5" -type l -print -quit)"
cmp -s "$previous_h5_dir/index.html" "$temporary_h5/index.html"
cmp -s "$previous_h5_dir/memoria-release.json" "$temporary_h5/memoria-release.json"
(
  cd "$temporary_h5"
  test -n "$(find . -type f ! -name "._*" -print -quit)"
  find . -type f ! -name "._*" ! -name .memoria-rollback-manifest.sha256 -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum >.memoria-rollback-manifest.sha256
  chmod 0600 .memoria-rollback-manifest.sha256
  sha256sum -c .memoria-rollback-manifest.sha256 >/dev/null
)
rollback_manifest_sha="$(sha256sum \
  "$temporary_h5/.memoria-rollback-manifest.sha256" | cut -d " " -f1)"
[[ "$rollback_manifest_sha" =~ ^[0-9a-f]{64}$ ]]

{
  printf "RELEASE_TAG=%s\n" "$release_tag"
  printf "PREV_RUNTIME_TAG=%s\n" "$previous_runtime_tag"
  printf "PREV_RUNTIME_COMMIT=%s\n" "$previous_runtime_commit"
  printf "PREV_H5_DIRNAME=%s\n" "$previous_h5_dirname"
  printf "PREV_H5_RELEASE_TAG=%s\n" "$previous_h5_release_tag"
  printf "PREV_H5_COMMIT=%s\n" "$previous_h5_commit"
  printf "ROLLBACK_H5_DIRNAME=%s\n" "$rollback_dirname"
  printf "ROLLBACK_H5_MANIFEST_SHA256=%s\n" "$rollback_manifest_sha"
  printf "CONTROL_ENV_PRESENT=%s\n" "$(present /etc/memoria-control-api.env)"
  printf "AGENT_ENV_PRESENT=%s\n" "$(present /etc/memoria-agent.env)"
  printf "SPEAKER_MODEL_ENV_PRESENT=%s\n" "$(present /etc/memoria-speaker-model.env)"
  printf "GATEWAY_ENV_PRESENT=%s\n" "$(present /etc/memoria-miniprogram-gateway.env)"
  printf "DEVICE_GATEWAY_ENV_PRESENT=%s\n" \
    "$(present /etc/memoria-device-media-gateway.env)"
  printf "MEDIA_EDGE_ENV_PRESENT=%s\n" "$(present /etc/memoria-media-edge.env)"
  printf "POSTGRES_ENV_PRESENT=%s\n" "$(present /etc/memoria-postgres.env)"
  printf "MEDIA_RUNTIME_WAS_RUNNING=%s\n" "$media_runtime_was_running"
} >"$temporary"
chown root:root "$temporary"
chmod 0600 "$temporary"
mv -T "$temporary_h5" "$rollback_h5"
rollback_installed=1
mv -T "$temporary" "$receipt"
rollback_installed=0
trap - EXIT HUP INT TERM
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
sudo test "$(stat -c '%U:%G:%a' "$ROLLBACK_RECEIPT")" = "root:root:600"
sudo sha256sum "$ROLLBACK_RECEIPT"
```

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

# PostgreSQL candidate must exist before any application candidate is derived:
# prepare_production_upgrade_env.py reads this exact file to build every DSN.
: "${LEGACY_ENV_SOURCE:?set the canonical root-only merged production env source}"
sudo bash -cEeu '
set -o pipefail
requested_release=$1
candidate_dir=/run/memoria-env/$requested_release
postgres_candidate=$candidate_dir/postgres.env

secure_file() {
  test -f "$1" && test ! -L "$1"
  test "$(stat -c "%U:%G:%a" "$1")" = root:root:600
}

test ! -e "$postgres_candidate"
secure_file /etc/memoria-postgres.env
install -d -o root -g root -m 0700 "$candidate_dir"
temporary=
postgres_candidate_committed=0
cleanup_postgres_candidate() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f -- "$temporary"
  if [ "$postgres_candidate_committed" = 0 ]; then rm -f -- "$postgres_candidate"; fi
  exit "$status"
}
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap cleanup_postgres_candidate EXIT
install -o root -g root -m 0600 /etc/memoria-postgres.env "$postgres_candidate"
for key in \
  MEMORIA_DB_APP_PASSWORD \
  MEMORIA_DB_COMPILER_PASSWORD \
  MEMORIA_DB_EVOLUTION_PASSWORD \
  MEMORIA_DB_GUARDIAN_PASSWORD \
  MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD \
  MEMORIA_DB_GUARDIAN_WORKER_PASSWORD \
  MEMORIA_DB_IDENTITY_PASSWORD \
  MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD \
  MEMORIA_DB_CONSENT_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD \
  MEMORIA_DB_SESSION_API_PASSWORD \
  MEMORIA_DB_ACTION_EXECUTOR_PASSWORD \
  MEMORIA_DB_SESSION_PROJECTOR_PASSWORD \
  MEMORIA_DB_SESSION_WORKER_PASSWORD \
  MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD \
  MEMORIA_DB_MEMORY_API_PASSWORD \
  MEMORIA_DB_MEMORY_WORKER_PASSWORD; do
  if ! grep -Eq "^${key}=.{32,}$" "$postgres_candidate"; then
    temporary="$(mktemp "$candidate_dir/.postgres.XXXXXX")"
    awk -v target="$key" '\''index($0, target "=") != 1'\'' \
      "$postgres_candidate" >"$temporary"
    generated_password="$(openssl rand -hex 48)"
    printf "%s=%s\n" "$key" "$generated_password" >>"$temporary"
    unset generated_password
    chown root:root "$temporary"
    chmod 0600 "$temporary"
    mv -T "$temporary" "$postgres_candidate"
    temporary=
  fi
done
secure_file "$postgres_candidate"
postgres_candidate_committed=1
trap - EXIT HUP INT TERM
' bash "$RELEASE_TAG"

: "${MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256:?copy the digest field from the verified manifest}"
sudo test -f "$LEGACY_ENV_SOURCE" && sudo test ! -L "$LEGACY_ENV_SOURCE"
sudo test "$(sudo stat -c '%U:%G:%a' "$LEGACY_ENV_SOURCE")" = root:root:600
sudo test "$(sudo stat -c '%U:%G:%a' /etc/memoria-minio.env)" = root:root:600
sudo docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev \
  --user 0:0 --cap-drop ALL --security-opt no-new-privileges \
  --env PYTHONPATH=/release \
  --mount "type=bind,src=/opt/memoria/releases/$RELEASE_TAG,dst=/release,readonly" \
  --mount "type=bind,src=$LEGACY_ENV_SOURCE,dst=/run/input/legacy.env,readonly" \
  --mount type=bind,src=/etc/memoria-minio.env,dst=/run/input/minio.env,readonly \
  --mount "type=bind,src=/run/memoria-env/$RELEASE_TAG,dst=/run/output" \
  --entrypoint /app/.venv/bin/python "memoria-agent:$RELEASE_TAG" \
  -m scripts.prepare_production_upgrade_env \
  --legacy /run/input/legacy.env \
  --postgres /run/output/postgres.env \
  --minio /run/input/minio.env \
  --release-tag "$RELEASE_TAG" \
  --evolution-trusted-root "$MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256" \
  --control /run/output/control-api.env \
  --agent /run/output/agent.env \
  --speaker-model /run/output/speaker-model.env \
  --gateway /run/output/gateway.env \
  --device-gateway /run/output/device-gateway.env \
  --media-edge /run/output/media-edge.env

sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
trusted_root=$3
candidate_dir=/run/memoria-env/$requested_release
backup_dir=/var/backups/memoria

secure_file() {
  test -f "$1" && test ! -L "$1"
  test "$(stat -c "%U:%G:%a" "$1")" = root:root:600
}
secure_file "$receipt"
# shellcheck disable=SC1090 -- receipt was generated above by this runbook.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
for value in "$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" \
  "$SPEAKER_MODEL_ENV_PRESENT" "$GATEWAY_ENV_PRESENT" \
  "$DEVICE_GATEWAY_ENV_PRESENT" \
  "$MEDIA_EDGE_ENV_PRESENT" "$POSTGRES_ENV_PRESENT" \
  "$MEDIA_RUNTIME_WAS_RUNNING"; do
  case "$value" in 0|1) ;; *) echo "invalid rollback receipt" >&2; exit 1 ;; esac
done

postgres_candidate=$candidate_dir/postgres.env
secure_file "$postgres_candidate"
for key in \
  MEMORIA_DB_APP_PASSWORD \
  MEMORIA_DB_COMPILER_PASSWORD \
  MEMORIA_DB_EVOLUTION_PASSWORD \
  MEMORIA_DB_GUARDIAN_PASSWORD \
  MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD \
  MEMORIA_DB_GUARDIAN_WORKER_PASSWORD \
  MEMORIA_DB_IDENTITY_PASSWORD \
  MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD \
  MEMORIA_DB_CONSENT_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD \
  MEMORIA_DB_SESSION_API_PASSWORD \
  MEMORIA_DB_ACTION_EXECUTOR_PASSWORD \
  MEMORIA_DB_SESSION_PROJECTOR_PASSWORD \
  MEMORIA_DB_SESSION_WORKER_PASSWORD \
  MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD \
  MEMORIA_DB_MEMORY_API_PASSWORD \
  MEMORIA_DB_MEMORY_WORKER_PASSWORD; do
  grep -Eq "^${key}=.{32,}$" "$postgres_candidate"
done

currents=(
  /etc/memoria-control-api.env /etc/memoria-agent.env
  /etc/memoria-speaker-model.env /etc/memoria-miniprogram-gateway.env
  /etc/memoria-device-media-gateway.env
  /etc/memoria-postgres.env /etc/memoria-media-edge.env
)
candidates=(
  "$candidate_dir/control-api.env" "$candidate_dir/agent.env"
  "$candidate_dir/speaker-model.env" "$candidate_dir/gateway.env"
  "$candidate_dir/device-gateway.env"
  "$postgres_candidate" "$candidate_dir/media-edge.env"
)
backups=(
  "$backup_dir/memoria-control-api.env-pre-$requested_release"
  "$backup_dir/memoria-agent.env-pre-$requested_release"
  "$backup_dir/memoria-speaker-model.env-pre-$requested_release"
  "$backup_dir/memoria-miniprogram-gateway.env-pre-$requested_release"
  "$backup_dir/memoria-device-media-gateway.env-pre-$requested_release"
  "$backup_dir/memoria-postgres.env-pre-$requested_release"
  "$backup_dir/memoria-media-edge.env-pre-$requested_release"
)
present=(
  "$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" "$SPEAKER_MODEL_ENV_PRESENT"
  "$GATEWAY_ENV_PRESENT" "$DEVICE_GATEWAY_ENV_PRESENT" \
  "$POSTGRES_ENV_PRESENT" "$MEDIA_EDGE_ENV_PRESENT"
)

# Phase 1: validate every candidate and every frozen current state before writing
# any backup. Live env is installed only inside the guarded DDL/activation transactions.
for candidate in "${candidates[@]}"; do secure_file "$candidate"; done
grep -qx "ENDPOINTING_MIN_DELAY_S=1.50" "$candidate_dir/agent.env"
grep -qx "ENDPOINTING_MAX_DELAY_S=2.20" "$candidate_dir/agent.env"
grep -qx "FALSE_INTERRUPTION_TIMEOUT_S=1.70" "$candidate_dir/agent.env"
grep -qx "INTERRUPT_SEMANTIC_ENABLED=true" "$candidate_dir/agent.env"
grep -qx "INTERRUPT_SEMANTIC_MODEL=deepseek-v4-flash" "$candidate_dir/agent.env"
grep -qx "INTERRUPT_SEMANTIC_TIMEOUT_S=1.2" "$candidate_dir/agent.env"
grep -Fxq "MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256=$trusted_root" \
  "$candidate_dir/control-api.env"
for key in \
  POSTGRES_PASSWORD \
  MEMORIA_DB_APP_PASSWORD \
  MEMORIA_DB_COMPILER_PASSWORD \
  MEMORIA_DB_EVOLUTION_PASSWORD \
  MEMORIA_DB_GUARDIAN_PASSWORD \
  MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD \
  MEMORIA_DB_GUARDIAN_WORKER_PASSWORD \
  MEMORIA_DB_IDENTITY_PASSWORD \
  MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD \
  MEMORIA_DB_CONSENT_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD \
  MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD \
  MEMORIA_DB_SESSION_API_PASSWORD \
  MEMORIA_DB_ACTION_EXECUTOR_PASSWORD \
  MEMORIA_DB_SESSION_PROJECTOR_PASSWORD \
  MEMORIA_DB_SESSION_WORKER_PASSWORD \
  MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD \
  MEMORIA_DB_MEMORY_API_PASSWORD \
  MEMORIA_DB_MEMORY_WORKER_PASSWORD; do
  grep -Eq "^${key}=.+$" "$postgres_candidate"
done
for index in "${!currents[@]}"; do
  if [ "${present[$index]}" = 1 ]; then
    secure_file "${currents[$index]}"
    if [ -e "${backups[$index]}" ]; then
      secure_file "${backups[$index]}"
      cmp -s "${currents[$index]}" "${backups[$index]}"
    fi
  else
    test ! -e "${currents[$index]}"
    test ! -e "${backups[$index]}"
  fi
done

# Phase 2: back up every prior file; do not mutate /etc in this block.
for index in "${!currents[@]}"; do
  if [ "${present[$index]}" = 1 ] && [ ! -e "${backups[$index]}" ]; then
    install -o root -g root -m 0600 \
      "${currents[$index]}" "${backups[$index]}"
  fi
done
for index in "${!currents[@]}"; do
  if [ "${present[$index]}" = 1 ]; then
    secure_file "${backups[$index]}"
    cmp -s "${currents[$index]}" "${backups[$index]}"
  fi
done

for index in "${!backups[@]}"; do
  if [ "${present[$index]}" = 1 ]; then sha256sum "${backups[$index]}"; fi
done
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT" \
  "$MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256"
```

保护副本放在 root-only `/var/backups/memoria`，避免与容器 bind 目录共享暴露面；`/var/lib/memoria` 中的原始快照继续保留，作为独立的第二份回滚副本。PostgreSQL candidate 先生成唯一 evolution 密码，五份应用 candidate 再从这同一文件派生；media-edge candidate 总是生成和备份，但文件存在本身不会启用 profile。当前四镜像工件流程只允许 receipt 记录 `MEDIA_RUNTIME_WAS_RUNNING=0`；启用该 profile 前必须先把 media-edge 纳入 build/save/manifest/verifier 全链。`split_production_env.py` 只做最小权限分流，不能替代 endpointing 精确值门禁。数据库、全部候选 env 和备份都必须为 regular `root:root 0600`，不得使用 symlink，也不得为了容器读取而放宽权限。

### 3.1 PostgreSQL forward-only schema 升级（按 release 要求执行）

只有 release 文档明确要求 PostgreSQL schema/role 升级时执行本节。必须先完成 PostgreSQL/WAL/MinIO
联合备份门禁和上面的 SQLite/env 备份。已有 volume 不会重新执行 init 文件；必须先让候选 data
Compose 把新脚本只读挂载到现有 PostgreSQL 容器，再运行候选 release 中的幂等升级脚本。旧 runtime
writer 在整个 DDL 窗口保持停止；无论升级成功、失败、SSH 断开或收到终止信号，本事务都会恢复冻结的
旧 env/data/runtime 后再退出。成功只保留 additive DDL，下一节在独立受保护事务中重新安装同一组
candidate env 并切换 runtime；失败不切 H5，也不回滚已经提交的 forward-only DDL。16 个运行角色
密码只从 `root:root 0600` 的 `/etc/memoria-postgres.env` 导入本次维护 shell；管理员通过容器内本地
socket 执行 DDL，`POSTGRES_PASSWORD`、`memoria_admin` DSN 和
`MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL`、`MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL`
都不得进入长期 Control API env：

```bash
sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
candidate_release=/opt/memoria/releases/$requested_release
backup_dir=/var/backups/memoria

secure_file() {
  test -f "$1" && test ! -L "$1"
  test "$(stat -c "%U:%G:%a" "$1")" = root:root:600
}
restore_one() {
  target=$1 backup=$2 was_present=$3
  if [ "$was_present" = 1 ]; then
    secure_file "$backup"
    install -o root -g root -m 0600 "$backup" "$target"
  else
    rm -f "$target"
  fi
}
stop_current_runtime() {
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if ! ids="$(docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service")"; then
      return 1
    fi
    if [ -n "$ids" ]; then docker stop $ids >/dev/null; fi
  done
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service" | grep -q .; then
      echo "runtime service still running: $service" >&2
      return 1
    fi
  done
}
secure_file "$receipt"
# shellcheck disable=SC1090 -- root-generated receipt with validated ownership/mode.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
old_release=/opt/memoria/releases/$PREV_RUNTIME_TAG
old_data=$old_release/infra
candidate_data=$candidate_release/infra
candidate_postgres=/run/memoria-env/$requested_release/postgres.env
test -d "$old_release" && test -d "$old_data" && test -d "$candidate_data"
test "$(readlink -f /opt/memoria/current)" = "$old_release"
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_COMMIT" {print $2}'\'' \
  "$old_release/.env")" = "$PREV_RUNTIME_COMMIT"
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_TAG" {print $2}'\'' \
  "$old_release/.env")" = "$PREV_RUNTIME_TAG"
for image in agent control-api speaker-model miniprogram-gateway; do
  labels="$(docker image inspect "memoria-$image:$PREV_RUNTIME_TAG" \
    --format '\''{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'\'')"
  test "$labels" = "$PREV_RUNTIME_COMMIT $PREV_RUNTIME_TAG $image"
done
cd "$old_release"
env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
  docker compose -f docker-compose.production.yml config --quiet
docker compose --project-directory "$old_data" \
  -f "$old_data/memoria-data.production.yml" config --quiet
docker compose --project-directory "$candidate_data" \
  -f "$candidate_data/memoria-data.production.yml" config --quiet
secure_file "$candidate_postgres"
for item in \
  "control-api:$CONTROL_ENV_PRESENT" "agent:$AGENT_ENV_PRESENT" \
  "speaker-model:$SPEAKER_MODEL_ENV_PRESENT" \
  "miniprogram-gateway:$GATEWAY_ENV_PRESENT" \
  "device-media-gateway:$DEVICE_GATEWAY_ENV_PRESENT" \
  "postgres:$POSTGRES_ENV_PRESENT" "media-edge:$MEDIA_EDGE_ENV_PRESENT"; do
  name=${item%%:*}
  was_present=${item##*:}
  current=/etc/memoria-$name.env
  backup=$backup_dir/memoria-$name.env-pre-$requested_release
  if [ "$was_present" = 1 ]; then
    secure_file "$backup"
    secure_file "$current"
    cmp -s "$backup" "$current"
  else
    test ! -e "$current"
  fi
done

restore_previous_runtime() {
  echo "restoring frozen data/runtime target after PostgreSQL maintenance" >&2

  # The old data control plane must be healthy before any old writer returns.
  restore_one /etc/memoria-postgres.env \
    "$backup_dir/memoria-postgres.env-pre-$requested_release" \
    "$POSTGRES_ENV_PRESENT"
  secure_file /etc/memoria-postgres.env
  docker compose --project-directory "$old_data" \
    -f "$old_data/memoria-data.production.yml" \
    up -d --no-build --wait --wait-timeout 120 postgres
  docker exec memoria-data-postgres-1 pg_isready -U memoria_admin -d postgres

  restore_one /etc/memoria-control-api.env \
    "$backup_dir/memoria-control-api.env-pre-$requested_release" "$CONTROL_ENV_PRESENT"
  restore_one /etc/memoria-agent.env \
    "$backup_dir/memoria-agent.env-pre-$requested_release" "$AGENT_ENV_PRESENT"
  restore_one /etc/memoria-speaker-model.env \
    "$backup_dir/memoria-speaker-model.env-pre-$requested_release" \
    "$SPEAKER_MODEL_ENV_PRESENT"
  restore_one /etc/memoria-miniprogram-gateway.env \
    "$backup_dir/memoria-miniprogram-gateway.env-pre-$requested_release" \
    "$GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-device-media-gateway.env \
    "$backup_dir/memoria-device-media-gateway.env-pre-$requested_release" \
    "$DEVICE_GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-media-edge.env \
    "$backup_dir/memoria-media-edge.env-pre-$requested_release" "$MEDIA_EDGE_ENV_PRESENT"

  rm -f "/opt/memoria/.current.restore-$requested_release"
  ln -s "releases/$PREV_RUNTIME_TAG" "/opt/memoria/.current.restore-$requested_release"
  mv -Tf "/opt/memoria/.current.restore-$requested_release" /opt/memoria/current
  test "$(readlink -f /opt/memoria/current)" = "$old_release"
  cd "$old_release"
  env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
    docker compose -f docker-compose.production.yml \
    up -d --no-build --wait --wait-timeout 120
  if [ "$MEDIA_RUNTIME_WAS_RUNNING" = 1 ]; then
    env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
      docker compose --profile media-runtime -f docker-compose.production.yml \
      up -d --no-build --wait --wait-timeout 120 \
      media-edge voice-core-media-bridge media-slo-reporter
  fi
  "$old_release/scripts/refresh_readiness.sh"
  curl -fsS http://127.0.0.1:8791/health/ready >/dev/null
}
on_upgrade_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  restore_previous_runtime
  exit "$status"
}
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap on_upgrade_exit EXIT

stop_current_runtime
install -o root -g root -m 0600 "$candidate_postgres" /etc/memoria-postgres.env
secure_file /etc/memoria-postgres.env

docker compose --project-directory "$candidate_data" \
  -f "$candidate_data/memoria-data.production.yml" config --quiet
docker compose --project-directory "$candidate_data" \
  -f "$candidate_data/memoria-data.production.yml" \
  up -d --no-build --wait --wait-timeout 120 postgres
docker exec memoria-data-postgres-1 pg_isready -U memoria_admin -d postgres

secure_file /etc/memoria-postgres.env
set -a
# shellcheck disable=SC1091 -- validated root-only production env.
. /etc/memoria-postgres.env
set +a
export POSTGRES_CONTAINER=memoria-data-postgres-1
"$candidate_release/scripts/upgrade_authoritative_postgres.sh"
"$candidate_release/scripts/verify_authoritative_postgres.sh"
for key in \
  MEMORIA_DB_APP_PASSWORD \
  MEMORIA_DB_COMPILER_PASSWORD \
  MEMORIA_DB_EVOLUTION_PASSWORD \
  MEMORIA_DB_GUARDIAN_PASSWORD \
  MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD \
  MEMORIA_DB_GUARDIAN_WORKER_PASSWORD \
  MEMORIA_DB_IDENTITY_PASSWORD \
  MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD \
  MEMORIA_DB_CONSENT_PASSWORD \
  MEMORIA_DB_SESSION_API_PASSWORD \
  MEMORIA_DB_ACTION_EXECUTOR_PASSWORD \
  MEMORIA_DB_SESSION_PROJECTOR_PASSWORD \
  MEMORIA_DB_SESSION_WORKER_PASSWORD \
  MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD \
  MEMORIA_DB_MEMORY_API_PASSWORD \
  MEMORIA_DB_MEMORY_WORKER_PASSWORD; do
  unset "$key"
done
unset POSTGRES_PASSWORD POSTGRES_CONTAINER
docker exec memoria-data-postgres-1 pg_isready -U memoria_admin -d postgres
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
```

事务执行期间临时安装的 PostgreSQL candidate 必须为 `root:root 0600`，并包含 16 个独立运行角色的
当前密码；脚本和命令不得打印这些值。统一 verifier 会检查所有运行角色均为
`LOGIN/NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOBYPASSRLS` 且拥有数据库 `CONNECT`，核对 Identity、
Consent、Policy Receipt、Device Fleet、Session Runtime、Evolution、Guardian/Tutor、MemoryScope
的全部权威表，
并验证 RLS、要求 FORCE RLS 的表、关键 SECURITY/authority 函数及 NOLOGIN owner 角色。旧
`upgrade_evolution_postgres.sh` 与 `upgrade_guardian_postgres.sh` 只保留为兼容包装器，新 Runbook
不得再调用。事务退出时 `/etc` 已恢复旧值，唯一 candidate 仍保留在
`/run/memoria-env/$RELEASE_TAG/postgres.env`，由下一节与其余五份 candidate 一起原子安装。升级成功
不等于新 runtime 可切流；仍须启动 commit/tag 绑定镜像，并在 readiness 中取得
`evolution_store=ready` 和 10/10 core。

### 4. 原子激活 runtime

```bash
sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
candidate=/opt/memoria/releases/$requested_release
candidate_data=$candidate/infra
candidate_env=/run/memoria-env/$requested_release
backup_dir=/var/backups/memoria
ready_file=
activation_committed=0

secure_file() {
  test -f "$1" && test ! -L "$1"
  test "$(stat -c "%U:%G:%a" "$1")" = root:root:600
}
restore_one() {
  target=$1 backup=$2 was_present=$3
  if [ "$was_present" = 1 ]; then
    secure_file "$backup"
    install -o root -g root -m 0600 "$backup" "$target"
  else
    rm -f "$target"
  fi
}
stop_current_runtime() {
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if ! ids="$(docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service")"; then
      return 1
    fi
    if [ -n "$ids" ]; then docker stop $ids >/dev/null; fi
  done
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service" | grep -q .; then
      echo "runtime service still running: $service" >&2
      return 1
    fi
  done
}
restore_old_runtime() {
  echo "runtime activation failed; restoring frozen target" >&2
  stop_current_runtime
  restore_one /etc/memoria-postgres.env \
    "$backup_dir/memoria-postgres.env-pre-$requested_release" "$POSTGRES_ENV_PRESENT"
  restore_one /etc/memoria-control-api.env \
    "$backup_dir/memoria-control-api.env-pre-$requested_release" "$CONTROL_ENV_PRESENT"
  restore_one /etc/memoria-agent.env \
    "$backup_dir/memoria-agent.env-pre-$requested_release" "$AGENT_ENV_PRESENT"
  restore_one /etc/memoria-speaker-model.env \
    "$backup_dir/memoria-speaker-model.env-pre-$requested_release" \
    "$SPEAKER_MODEL_ENV_PRESENT"
  restore_one /etc/memoria-miniprogram-gateway.env \
    "$backup_dir/memoria-miniprogram-gateway.env-pre-$requested_release" \
    "$GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-device-media-gateway.env \
    "$backup_dir/memoria-device-media-gateway.env-pre-$requested_release" \
    "$DEVICE_GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-media-edge.env \
    "$backup_dir/memoria-media-edge.env-pre-$requested_release" "$MEDIA_EDGE_ENV_PRESENT"

  old_release=/opt/memoria/releases/$PREV_RUNTIME_TAG
  old_data=$old_release/infra
  docker compose --project-directory "$old_data" \
    -f "$old_data/memoria-data.production.yml" \
    up -d --no-build --wait --wait-timeout 120 postgres
  docker exec memoria-data-postgres-1 pg_isready -U memoria_admin -d postgres
  rm -f "/opt/memoria/.current.restore-$requested_release"
  ln -s "releases/$PREV_RUNTIME_TAG" "/opt/memoria/.current.restore-$requested_release"
  mv -Tf "/opt/memoria/.current.restore-$requested_release" /opt/memoria/current
  test "$(readlink -f /opt/memoria/current)" = "$old_release"
  cd "$old_release"
  env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
    docker compose -f docker-compose.production.yml \
    up -d --no-build --wait --wait-timeout 120
  if [ "$MEDIA_RUNTIME_WAS_RUNNING" = 1 ]; then
    env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
      docker compose --profile media-runtime -f docker-compose.production.yml \
      up -d --no-build --wait --wait-timeout 120 \
      media-edge voice-core-media-bridge media-slo-reporter
  fi
  "$old_release/scripts/refresh_readiness.sh"
  curl -fsS http://127.0.0.1:8791/health/ready >/dev/null
}
on_activation_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f -- "$ready_file"
  if [ "$activation_committed" = 1 ]; then exit "$status"; fi
  restore_old_runtime
  exit "$status"
}

secure_file "$receipt"
# shellcheck disable=SC1090 -- root-generated receipt with validated ownership/mode.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
for value in "$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" \
  "$SPEAKER_MODEL_ENV_PRESENT" "$GATEWAY_ENV_PRESENT" \
  "$DEVICE_GATEWAY_ENV_PRESENT" \
  "$MEDIA_EDGE_ENV_PRESENT" "$POSTGRES_ENV_PRESENT" \
  "$MEDIA_RUNTIME_WAS_RUNNING"; do
  case "$value" in 0|1) ;; *) echo "invalid rollback receipt" >&2; exit 1 ;; esac
done
test -d "$candidate" && test -d "$candidate_data" && test -d "$candidate_env"
test "$(readlink -f /opt/memoria/current)" = \
  "/opt/memoria/releases/$PREV_RUNTIME_TAG"
test "$(readlink -f /var/www/memoria-h5)" = \
  "/var/www/memoria-releases/$PREV_H5_DIRNAME"
old_runtime=/opt/memoria/releases/$PREV_RUNTIME_TAG
old_data=$old_runtime/infra
test -d "$old_runtime" && test -d "$old_data"
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_COMMIT" {print $2}'\'' \
  "$old_runtime/.env")" = "$PREV_RUNTIME_COMMIT"
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_TAG" {print $2}'\'' \
  "$old_runtime/.env")" = "$PREV_RUNTIME_TAG"
for image in agent control-api speaker-model miniprogram-gateway; do
  labels="$(docker image inspect "memoria-$image:$PREV_RUNTIME_TAG" \
    --format '\''{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'\'')"
  test "$labels" = "$PREV_RUNTIME_COMMIT $PREV_RUNTIME_TAG $image"
done
cd "$old_runtime"
env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
  docker compose -f docker-compose.production.yml config --quiet

targets=(control-api agent speaker-model miniprogram-gateway device-media-gateway postgres media-edge)
candidates=(
  "$candidate_env/control-api.env" "$candidate_env/agent.env"
  "$candidate_env/speaker-model.env" "$candidate_env/gateway.env"
  "$candidate_env/device-gateway.env"
  "$candidate_env/postgres.env" "$candidate_env/media-edge.env"
)
present=(
  "$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" "$SPEAKER_MODEL_ENV_PRESENT"
  "$GATEWAY_ENV_PRESENT" "$DEVICE_GATEWAY_ENV_PRESENT" \
  "$POSTGRES_ENV_PRESENT" "$MEDIA_EDGE_ENV_PRESENT"
)
for index in "${!targets[@]}"; do
  current=/etc/memoria-${targets[$index]}.env
  backup=$backup_dir/memoria-${targets[$index]}.env-pre-$requested_release
  secure_file "${candidates[$index]}"
  if [ "${present[$index]}" = 1 ]; then
    secure_file "$backup"
    secure_file "$current"
    cmp -s "$backup" "$current"
  else
    test ! -e "$current"
  fi
done

trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap on_activation_exit EXIT
stop_current_runtime
for index in "${!targets[@]}"; do
  install -o root -g root -m 0600 \
    "${candidates[$index]}" "/etc/memoria-${targets[$index]}.env"
done
for index in "${!targets[@]}"; do
  secure_file "/etc/memoria-${targets[$index]}.env"
done

test ! -e "/opt/memoria/.current.$requested_release"
ln -s "releases/$requested_release" "/opt/memoria/.current.$requested_release"
mv -Tf "/opt/memoria/.current.$requested_release" /opt/memoria/current
test "$(readlink -f /opt/memoria/current)" = "$candidate"
RUNTIME_COMPOSE_DIR=/opt/memoria/current
DATA_COMPOSE_DIR=/opt/memoria/current/infra
test "$(readlink -f "$RUNTIME_COMPOSE_DIR")" = "$candidate"
test "$(readlink -f "$DATA_COMPOSE_DIR")" = "$candidate_data"

# Let Compose create the labelled shared network; never create it by hand.
if ! docker network inspect memoria_default >/dev/null 2>&1; then
  cd "$RUNTIME_COMPOSE_DIR"
  env MEMORIA_RELEASE_TAG="$requested_release" \
    docker compose -f docker-compose.production.yml create --no-build
fi
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" config --quiet
docker compose --project-directory "$DATA_COMPOSE_DIR" \
  -f "$DATA_COMPOSE_DIR/memoria-data.production.yml" \
  up -d --no-build --wait --wait-timeout 120
docker compose --project-directory /opt/livekit -f /opt/livekit/compose.yml \
  up -d --wait --wait-timeout 120

cd "$RUNTIME_COMPOSE_DIR"
env MEMORIA_RELEASE_TAG="$requested_release" \
  docker compose -f docker-compose.production.yml \
  up -d --no-build --wait --wait-timeout 120
if [ "$MEDIA_RUNTIME_WAS_RUNNING" = 1 ]; then
  env MEMORIA_RELEASE_TAG="$requested_release" \
    docker compose --profile media-runtime -f docker-compose.production.yml \
    up -d --no-build --wait --wait-timeout 120 \
    media-edge voice-core-media-bridge media-slo-reporter
else
  env MEMORIA_RELEASE_TAG="$requested_release" \
    docker compose --profile media-runtime -f docker-compose.production.yml stop \
    media-edge voice-core-media-bridge media-slo-reporter
fi
test "$(readlink -f /opt/memoria/current)" = "$candidate"

"$candidate/scripts/refresh_readiness.sh"
ready_file="$(mktemp /run/memoria-ready.XXXXXX)"
curl -fsS http://127.0.0.1:8791/health/ready >"$ready_file"
python3 - "$ready_file" "$requested_release" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
core = payload["checks"]["core"]
agent = payload["checks"]["agent"]
if payload["release_tag"] != expected:
    raise SystemExit("readiness release mismatch")
if agent["release_tag"] != expected or agent["status"] != "ready":
    raise SystemExit("Agent readiness release mismatch")
if len(core) != 10 or any(value != "ready" for value in core.values()):
    raise SystemExit("core readiness is not 10/10")
PY
rm -f "$ready_file"
ready_file=
activation_committed=1
trap - EXIT HUP INT TERM
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
```

`memoria-data.production.yml` 的脚本 bind mount 已设置 `create_host_path: false`；路径或项目目录错误时应立即失败，不得让 Docker 静默创建同名目录。后续发布若共享网络已存在，跳过 `create` 分支即可，但仍必须保留 `DATA_COMPOSE_DIR=/opt/memoria/current/infra`。

此时 `/var/www/memoria-h5` 仍必须指向旧 H5。

#### Memory projection rebuild（按 release 要求执行）

若本次 release 变更 memory projection 的编译器、检索格式或显式记忆策略，须在 H5
切换前停掉 archive writer，再从候选 Control API 镜像重建派生 projection。维护 DSN 只在
root-only shell 中导出，不能写入命令行、日志或 env 文件；普通 app/compiler DSN 没有
`BYPASSRLS`，不能替代。详情与重建后数据库验收见
`docs/archive-backup-restore-runbook.md` 的“仅重建 memory projection”。

```bash
# 先在 root-only maintenance shell 中导出 MEMORIA_MEMORY_REBUILD_DATABASE_URL；
# 值不写入命令行、日志或任何 env 文件。
sudo -E bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
candidate=/opt/memoria/releases/$requested_release

test -f "$receipt" && test ! -L "$receipt"
test "$(stat -c "%U:%G:%a" "$receipt")" = root:root:600
# shellcheck disable=SC1090 -- root-generated receipt.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
test "$(readlink -f /opt/memoria/current)" = "$candidate"
: "${MEMORIA_MEMORY_REBUILD_DATABASE_URL:?set in this root-only shell}"

restore_candidate() {
  cd "$candidate"
  env MEMORIA_RELEASE_TAG="$requested_release" \
    docker compose -f docker-compose.production.yml \
    up -d --no-build --wait --wait-timeout 120 \
    control-api agent miniprogram-gateway
  if [ "$MEDIA_RUNTIME_WAS_RUNNING" = 1 ]; then
    env MEMORIA_RELEASE_TAG="$requested_release" \
      docker compose --profile media-runtime -f docker-compose.production.yml \
      up -d --no-build --wait --wait-timeout 120 \
      media-edge voice-core-media-bridge media-slo-reporter
  else
    env MEMORIA_RELEASE_TAG="$requested_release" \
      docker compose --profile media-runtime -f docker-compose.production.yml stop \
      media-edge voice-core-media-bridge media-slo-reporter
  fi
  "$candidate/scripts/refresh_readiness.sh"
  curl -fsS http://127.0.0.1:8791/health/ready >/dev/null
}
on_rebuild_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  restore_candidate
  exit "$status"
}
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap on_rebuild_exit EXIT

cd "$candidate"
env MEMORIA_RELEASE_TAG="$requested_release" \
  docker compose --profile media-runtime -f docker-compose.production.yml stop \
  agent control-api miniprogram-gateway device-media-gateway media-slo-reporter \
  voice-core-media-bridge media-edge
running="$(env MEMORIA_RELEASE_TAG="$requested_release" \
  docker compose --profile media-runtime -f docker-compose.production.yml \
  ps --status running --services)"
for service in agent control-api miniprogram-gateway device-media-gateway media-slo-reporter \
  voice-core-media-bridge media-edge; do
  case $'\''\n'\''"$running"$'\''\n'\'' in
    *$'\''\n'\''$service$'\''\n'\''*) echo "writer still running: $service" >&2; false ;;
  esac
done

rebuild_output="$(env MEMORIA_RELEASE_TAG="$requested_release" \
  docker compose -f docker-compose.production.yml run --rm --no-deps \
  -e MEMORIA_MEMORY_REBUILD_DATABASE_URL \
  --entrypoint /app/.venv/bin/python control-api \
  -m scripts.rebuild_memory_projections --confirm-rebuild)"
python3 -c '\''import json,sys; report=json.loads(sys.stdin.read()); assert report["failed_events"] == 0'\'' \
  <<<"$rebuild_output"
unset MEMORIA_MEMORY_REBUILD_DATABASE_URL rebuild_output
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
```

若启用了 media-runtime profile，也必须先停止其 Voice Core/bridge writer，且 rebuild 输出的
`failed_events` 必须为 `0` 后才能恢复服务。不要直接执行
`scripts/rebuild_memory_projections.py`：镜像中的项目根目录以模块入口加载。

### 5. 强制 Provider 与 readiness 门禁

```bash
sudo /opt/memoria/current/scripts/refresh_readiness.sh
curl -fsS http://127.0.0.1:8791/health/ready
```

成功输出必须绑定本次唯一 tag，例如：

```text
livekit_smoke_test PASS: authenticated room-service access
provider_smoke_test PASS: FunASR, QwenRealtimeSearch, DeepSeek, Doubao, InterruptSemantic
readiness refresh PASS: $RELEASE_TAG (bailian_deepseek)
```

`Doubao` 通过必须同时满足：双向增量文本合成返回非空 24 kHz、单声道、
PCM signed 16-bit little-endian 音频，字级时间戳非空且单调，并将同一段合成音频降采样后
送入 FunASR，最终文本命中测试语义。任何一项失败都不能写入 readiness evidence。
`QwenRealtimeSearch` 必须用隔离的强制公网检索返回非空结果；它不携带用户身份、历史或
私有上下文，失败时同样不得写入 readiness evidence。
`InterruptSemantic` 还必须用 `deepseek-v4-flash` 通过五类严格枚举样本：真实污染、
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
- `/etc/nginx/snippets/memoria-device-media.conf`
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

已退役小程序媒体的标准 443 兼容回滚入口复用仓库
`infra/nginx-memoria-miniprogram-media.conf`：先将其安装为
`/etc/nginx/snippets/memoria-miniprogram-media.conf`，再仅在 WMS 的 443 `server` 内、
最终 `location / { return 404; }` 之前增加：

```nginx
include /etc/nginx/snippets/memoria-miniprogram-media.conf;
```

不得 include 完整的 `memoria-https.conf`，否则会把 H5、Control API 等额外路由一并迁入
WMS 443。该路由只能用于明确的 legacy 回滚，不能被描述为当前小程序入口或新设备默认路径。
`MINIPROGRAM_MEDIA_GATEWAY_URL` 仅为恢复旧版本而保留：

```text
wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media
```

Direct Device WSS canary 使用独立的 Go Media Edge 入口：安装
infra/nginx-memoria-device-edge.conf 为
/etc/nginx/snippets/memoria-device-edge.conf，并在同一 TLS server 中 include；它只代理
127.0.0.1:8794 → media-edge:8082，公网地址为
wss://aigcnice.com:8443/memoria-device-edge/v1/device/media。该入口只在
DEVICE_MEDIA_RUNTIME=direct_voice_core 的明确 Canary/allowlist 中使用；公共 8080 不承载设备 WSS。
它与下面的 legacy Gateway 路由、进程和票据边界完全隔离。

legacy 硬件设备 WSS 继续使用独立进程、端口和票据 secret，仅用于 livekit_compat 回滚。安装
`infra/nginx-memoria-device-media.conf` 为
`/etc/nginx/snippets/memoria-device-media.conf`，并在同一 TLS `server` 中 include；Control
API 与 gateway 分别配置相同的 `MEMORIA_DEVICE_GATEWAY_TICKET_SECRET`，但不得与 Auth、
LiveKit、小程序 gateway 或内部 capability token 复用。公网地址固定为：

```text
wss://aigcnice.com:8443/memoria-device-media/v1/device/media
```

生产启用仍以 Device Fleet PostgreSQL/RLS、托管 Activation 签名密钥和真实 ESP32 验收为前置门禁；
缺少任一项时 Control API 必须保持 503，不能回退旧 `DeviceRegistry`。

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
sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
release_root=/var/www/memoria-releases
candidate=$release_root/$requested_release
base=https://122.51.108.140:8443

test -f "$receipt" && test ! -L "$receipt"
test "$(stat -c "%U:%G:%a" "$receipt")" = root:root:600
# shellcheck disable=SC1090 -- root-generated receipt.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
for dirname in "$PREV_H5_DIRNAME" "$ROLLBACK_H5_DIRNAME"; do
  case "$dirname" in
    ""|*[!A-Za-z0-9._-]*) echo "invalid H5 dirname in receipt" >&2; exit 1 ;;
  esac
done
[[ "$ROLLBACK_H5_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
old_h5=$release_root/$PREV_H5_DIRNAME
rollback_h5=$release_root/$ROLLBACK_H5_DIRNAME
test "$(readlink -f /var/www/memoria-h5)" = "$old_h5"

validate_rollback_h5() {
  manifest=$rollback_h5/.memoria-rollback-manifest.sha256
  test -d "$rollback_h5" && test ! -L "$rollback_h5"
  test "$(readlink -f "$rollback_h5")" = "$rollback_h5"
  test -z "$(find "$rollback_h5" -type l -print -quit)"
  test -z "$(find "$rollback_h5" -type d ! -perm 0755 -print -quit)"
  test -z "$(find "$rollback_h5" -type f \
    ! -name .memoria-rollback-manifest.sha256 ! -perm 0644 -print -quit)"
  test -f "$manifest" && test ! -L "$manifest"
  test "$(stat -c "%U:%G:%a" "$manifest")" = root:root:600
  test "$(sha256sum "$manifest" | cut -d " " -f1)" = \
    "$ROLLBACK_H5_MANIFEST_SHA256"
  (
    cd "$rollback_h5"
    sha256sum -c .memoria-rollback-manifest.sha256 >/dev/null
  )
  python3 - "$rollback_h5/memoria-release.json" \
    "$PREV_H5_RELEASE_TAG" "$PREV_H5_COMMIT" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("release_tag") != sys.argv[2] or payload.get("commit") != sys.argv[3]:
    raise SystemExit("rollback H5 provenance mismatch")
PY
}
assert_runtime_ready() {
  runtime=/opt/memoria/releases/$requested_release
  test "$(readlink -f /opt/memoria/current)" = "$runtime"
  local ready
  ready="$(mktemp /run/memoria-h5-ready.XXXXXX)"
  if ! curl -fsS http://127.0.0.1:8791/health/ready >"$ready"; then
    rm -f "$ready"
    return 1
  fi
  if ! python3 - "$ready" "$requested_release" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
core = payload["checks"]["core"]
agent = payload["checks"]["agent"]
if payload.get("release_tag") != expected:
    raise SystemExit("runtime release mismatch before H5 switch")
if agent.get("release_tag") != expected or agent.get("status") != "ready":
    raise SystemExit("Agent readiness mismatch before H5 switch")
if len(core) != 10 or any(value != "ready" for value in core.values()):
    raise SystemExit("runtime core readiness is not 10/10")
PY
  then
    rm -f "$ready"
    return 1
  fi
  rm -f "$ready"
}
restore_old_h5() {
  validate_rollback_h5
  rm -f "/var/www/.memoria-h5.$requested_release"
  rm -f "/var/www/.memoria-h5.restore-$requested_release"
  ln -s "memoria-releases/$ROLLBACK_H5_DIRNAME" \
    "/var/www/.memoria-h5.restore-$requested_release"
  mv -Tf "/var/www/.memoria-h5.restore-$requested_release" /var/www/memoria-h5
  test "$(readlink -f /var/www/memoria-h5)" = "$rollback_h5"
}
h5_committed=0
on_h5_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$h5_committed" = 1 ]; then exit "$status"; fi
  restore_old_h5
  exit "$status"
}

test -d "$candidate" && test -f "$candidate/index.html"
test -f "$candidate/memoria-release.json"
validate_rollback_h5
python3 - "$candidate/memoria-release.json" "$old_h5/memoria-release.json" \
  "$requested_release" "$PREV_H5_RELEASE_TAG" "$PREV_H5_COMMIT" \
  "/opt/memoria/releases/$requested_release/.env" <<'\''PY'\''
import json
import re
import sys

candidate_path, old_path, release, old_release, old_commit, runtime_env = sys.argv[1:]
candidate = json.load(open(candidate_path, encoding="utf-8"))
old = json.load(open(old_path, encoding="utf-8"))
runtime = dict(
    line.rstrip("\n").split("=", 1)
    for line in open(runtime_env, encoding="utf-8")
    if "=" in line and not line.lstrip().startswith("#")
)
commit = candidate.get("commit", "")
if candidate.get("release_tag") != release or not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("candidate H5 provenance mismatch")
if runtime.get("MEMORIA_RELEASE_COMMIT") != commit:
    raise SystemExit("H5/runtime commit mismatch")
if old.get("release_tag") != old_release or old.get("commit") != old_commit:
    raise SystemExit("frozen H5 provenance mismatch")
PY
find "$candidate" -maxdepth 2 -type f ! -name "._*" \
  \( -name index.html -o -name "*.js" -o -name "*.css" \) \
  -exec sha256sum {} +

# Validate the complete historical immutable union before touching the symlink.
for source in "$release_root"/*/assets; do
  [ -d "$source" ] || continue
  [ "$(readlink -f "$source/..")" = "$(readlink -f "$candidate")" ] && continue
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    test -f "$candidate/assets/$relative"
  done < <(find "$source" -type f ! -name "._*" -print0)
done

assert_runtime_ready
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap on_h5_exit EXIT
assert_runtime_ready
test ! -e "/var/www/.memoria-h5.$requested_release"
ln -s "memoria-releases/$requested_release" "/var/www/.memoria-h5.$requested_release"
mv -Tf "/var/www/.memoria-h5.$requested_release" /var/www/memoria-h5
test "$(readlink -f /var/www/memoria-h5)" = "$candidate"
assert_runtime_ready
curl -fsS --connect-timeout 5 --max-time 15 \
  --resolve 122.51.108.140:8443:127.0.0.1 "$base/" >/dev/null

# Every historical asset must now be served through the new symlink; 404 cannot
# fall through to the SPA route.
for source in "$release_root"/*/assets; do
  [ -d "$source" ] || continue
  [ "$(readlink -f "$source/..")" = "$(readlink -f "$candidate")" ] && continue
  while IFS= read -r -d "" old_asset; do
    relative=${old_asset#"$source"/}
    curl -fsSI --connect-timeout 5 --max-time 15 \
      --resolve 122.51.108.140:8443:127.0.0.1 \
      "$base/memoria-h5/assets/$relative" >/dev/null
  done < <(find "$source" -type f ! -name "._*" -print0)
done
assert_runtime_ready
h5_committed=1
trap - EXIT HUP INT TERM
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
```

`mv -T` 在同一文件系统内完成原子替换；不得用覆盖目录内容的方式激活。候选 provenance、软链
realpath、候选与历史 immutable assets 以及切换后的 HTTP 读取处于同一 fail-closed 控制面；任一门禁
失败都立即原子恢复 receipt 冻结的旧 H5，失败候选目录保留供排查。

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

验收标准：公网 8443 的根 H5、兼容 H5、SPA、live、ready 和 WMS 静态页均为 200；ready 的 release 必须等于本次唯一 `RELEASE_TAG`，LLM/TTS provider 必须与本次已验签的 production 配置一致，且 10 项 core check 全 ready；internal、PocketSparks 与 Goods Invoice 原路径为 404；IP 证书 SAN 必须精确包含 `122.51.108.140`，域名 SNI 必须返回包含 `aigcnice.com` 与 `www.aigcnice.com` 的域名证书。`/rtc`、`/agent`、`/twirp/` 必须命中自建 LiveKit，真实浏览器 participant 必须为 `active` 且 `connectionType=tcp` 或 `udp`，不能是 `unknown`。服务器本机用 SNI/loopback 额外确认 443 根路径仍由 WMS 虚拟主机提供。WMS 应为 `enabled`；服务在正常运行期为 `active`，明确的资源让渡期允许为 `inactive`，但 Memoria 不得改动其目录或数据。

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

禁止从故障后的 `current` 软链重新推断回滚版本，也禁止在 runbook 中硬编码“当前”版本。以下命令
只消费 env 安装和切流前已冻结的 root-only receipt；若 receipt 缺失、被替换、版本不匹配，回滚立即
失败并要求人工核对。

### 仅回滚 H5

H5-only 故障不改 runtime、env 或数据 Compose：

```bash
: "${RELEASE_TAG:?set the failed release tag}"
ROLLBACK_RECEIPT=/var/backups/memoria/rollback-$RELEASE_TAG.env
sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
release_root=/var/www/memoria-releases
test -f "$receipt" && test ! -L "$receipt"
test "$(stat -c "%U:%G:%a" "$receipt")" = root:root:600
# shellcheck disable=SC1090 -- root-generated receipt.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
case "$ROLLBACK_H5_DIRNAME" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid rollback H5 dirname" >&2; exit 1 ;;
esac
[[ "$ROLLBACK_H5_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
rollback_h5=$release_root/$ROLLBACK_H5_DIRNAME
manifest=$rollback_h5/.memoria-rollback-manifest.sha256
test -d "$rollback_h5" && test ! -L "$rollback_h5"
test "$(readlink -f "$rollback_h5")" = "$rollback_h5"
test -z "$(find "$rollback_h5" -type l -print -quit)"
test -z "$(find "$rollback_h5" -type d ! -perm 0755 -print -quit)"
test -z "$(find "$rollback_h5" -type f \
  ! -name .memoria-rollback-manifest.sha256 ! -perm 0644 -print -quit)"
test -f "$manifest" && test ! -L "$manifest"
test "$(stat -c "%U:%G:%a" "$manifest")" = root:root:600
test "$(sha256sum "$manifest" | cut -d " " -f1)" = \
  "$ROLLBACK_H5_MANIFEST_SHA256"
(cd "$rollback_h5" && sha256sum -c .memoria-rollback-manifest.sha256 >/dev/null)
python3 - "$rollback_h5/memoria-release.json" \
  "$PREV_H5_RELEASE_TAG" "$PREV_H5_COMMIT" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("release_tag") != sys.argv[2] or payload.get("commit") != sys.argv[3]:
    raise SystemExit("rollback H5 provenance mismatch")
PY
rm -f "/var/www/.memoria-h5.rollback-$requested_release"
ln -s "memoria-releases/$ROLLBACK_H5_DIRNAME" \
  "/var/www/.memoria-h5.rollback-$requested_release"
mv -Tf "/var/www/.memoria-h5.rollback-$requested_release" /var/www/memoria-h5
test "$(readlink -f /var/www/memoria-h5)" = "$rollback_h5"
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
```

### 完整回滚 runtime 与 H5

runtime、Provider 或 readiness 故障使用下面独立控制面。它先预检 receipt、旧四个基础镜像和全部
应存在的备份；再恢复 PostgreSQL env/data，恢复应用 env，切旧 runtime 并按 receipt 恢复可选
`media-runtime`，最后切旧 H5。原先不存在的可选 env 必须删除，不能把候选值留给旧 runtime：

```bash
: "${RELEASE_TAG:?set the failed release tag}"
ROLLBACK_RECEIPT=/var/backups/memoria/rollback-$RELEASE_TAG.env
sudo bash -cEeu '
set -o pipefail
requested_release=$1
receipt=$2
backup_dir=/var/backups/memoria

secure_file() {
  test -f "$1" && test ! -L "$1"
  test "$(stat -c "%U:%G:%a" "$1")" = root:root:600
}
restore_one() {
  target=$1 backup=$2 was_present=$3
  if [ "$was_present" = 1 ]; then
    secure_file "$backup"
    install -o root -g root -m 0600 "$backup" "$target"
  else
    rm -f "$target"
  fi
}
stop_current_runtime() {
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if ! ids="$(docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service")"; then
      return 1
    fi
    if [ -n "$ids" ]; then docker stop $ids >/dev/null; fi
  done
  for service in agent control-api speaker-model miniprogram-gateway device-media-gateway \
    media-slo-reporter voice-core-media-bridge media-edge; do
    if docker ps -q \
      --filter label=com.docker.compose.project=memoria \
      --filter "label=com.docker.compose.service=$service" | grep -q .; then
      echo "runtime service still running: $service" >&2
      return 1
    fi
  done
}

secure_file "$receipt"
# shellcheck disable=SC1090 -- root-generated receipt.
. "$receipt"
test "$RELEASE_TAG" = "$requested_release"
for value in "$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" \
  "$SPEAKER_MODEL_ENV_PRESENT" "$GATEWAY_ENV_PRESENT" \
  "$DEVICE_GATEWAY_ENV_PRESENT" \
  "$MEDIA_EDGE_ENV_PRESENT" "$POSTGRES_ENV_PRESENT" \
  "$MEDIA_RUNTIME_WAS_RUNNING"; do
  case "$value" in 0|1) ;; *) echo "invalid rollback receipt" >&2; exit 1 ;; esac
done
case "$ROLLBACK_H5_DIRNAME" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid rollback H5 dirname" >&2; exit 1 ;;
esac
[[ "$ROLLBACK_H5_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
old_runtime=/opt/memoria/releases/$PREV_RUNTIME_TAG
old_data=$old_runtime/infra
rollback_h5=/var/www/memoria-releases/$ROLLBACK_H5_DIRNAME
rollback_manifest=$rollback_h5/.memoria-rollback-manifest.sha256
test -d "$old_runtime" && test -d "$old_data"
test -d "$rollback_h5" && test ! -L "$rollback_h5"
test "$(readlink -f "$rollback_h5")" = "$rollback_h5"
test -z "$(find "$rollback_h5" -type l -print -quit)"
test -z "$(find "$rollback_h5" -type d ! -perm 0755 -print -quit)"
test -z "$(find "$rollback_h5" -type f \
  ! -name .memoria-rollback-manifest.sha256 ! -perm 0644 -print -quit)"
test -f "$rollback_manifest" && test ! -L "$rollback_manifest"
test "$(stat -c "%U:%G:%a" "$rollback_manifest")" = root:root:600
test "$(sha256sum "$rollback_manifest" | cut -d " " -f1)" = \
  "$ROLLBACK_H5_MANIFEST_SHA256"
(cd "$rollback_h5" && sha256sum -c .memoria-rollback-manifest.sha256 >/dev/null)
python3 - "$rollback_h5/memoria-release.json" \
  "$PREV_H5_RELEASE_TAG" "$PREV_H5_COMMIT" <<'\''PY'\''
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("release_tag") != sys.argv[2] or payload.get("commit") != sys.argv[3]:
    raise SystemExit("rollback H5 provenance mismatch")
PY
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_COMMIT" {print $2}'\'' \
  "$old_runtime/.env")" = "$PREV_RUNTIME_COMMIT"
test "$(awk -F= '\''$1 == "MEMORIA_RELEASE_TAG" {print $2}'\'' \
  "$old_runtime/.env")" = "$PREV_RUNTIME_TAG"
for image in agent control-api speaker-model miniprogram-gateway; do
  labels="$(docker image inspect "memoria-$image:$PREV_RUNTIME_TAG" \
    --format '\''{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'\'')"
  test "$labels" = "$PREV_RUNTIME_COMMIT $PREV_RUNTIME_TAG $image"
done
cd "$old_runtime"
env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
  docker compose -f docker-compose.production.yml config --quiet

targets=(control-api agent speaker-model miniprogram-gateway device-media-gateway postgres media-edge)
present=("$CONTROL_ENV_PRESENT" "$AGENT_ENV_PRESENT" \
  "$SPEAKER_MODEL_ENV_PRESENT" "$GATEWAY_ENV_PRESENT" \
  "$DEVICE_GATEWAY_ENV_PRESENT" "$POSTGRES_ENV_PRESENT" \
  "$MEDIA_EDGE_ENV_PRESENT")
for index in "${!targets[@]}"; do
  if [ "${present[$index]}" = 1 ]; then
    secure_file "$backup_dir/memoria-${targets[$index]}.env-pre-$requested_release"
  fi
done

complete_full_rollback() {
  stop_current_runtime
  restore_one /etc/memoria-postgres.env \
    "$backup_dir/memoria-postgres.env-pre-$requested_release" "$POSTGRES_ENV_PRESENT"
  secure_file /etc/memoria-postgres.env
  docker compose --project-directory "$old_data" \
    -f "$old_data/memoria-data.production.yml" \
    up -d --no-build --wait --wait-timeout 120 postgres
  docker exec memoria-data-postgres-1 pg_isready -U memoria_admin -d postgres

  restore_one /etc/memoria-control-api.env \
    "$backup_dir/memoria-control-api.env-pre-$requested_release" "$CONTROL_ENV_PRESENT"
  restore_one /etc/memoria-agent.env \
    "$backup_dir/memoria-agent.env-pre-$requested_release" "$AGENT_ENV_PRESENT"
  restore_one /etc/memoria-speaker-model.env \
    "$backup_dir/memoria-speaker-model.env-pre-$requested_release" \
    "$SPEAKER_MODEL_ENV_PRESENT"
  restore_one /etc/memoria-miniprogram-gateway.env \
    "$backup_dir/memoria-miniprogram-gateway.env-pre-$requested_release" \
    "$GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-device-media-gateway.env \
    "$backup_dir/memoria-device-media-gateway.env-pre-$requested_release" \
    "$DEVICE_GATEWAY_ENV_PRESENT"
  restore_one /etc/memoria-media-edge.env \
    "$backup_dir/memoria-media-edge.env-pre-$requested_release" "$MEDIA_EDGE_ENV_PRESENT"

  rm -f "/opt/memoria/.current.rollback-$requested_release"
  ln -s "releases/$PREV_RUNTIME_TAG" "/opt/memoria/.current.rollback-$requested_release"
  mv -Tf "/opt/memoria/.current.rollback-$requested_release" /opt/memoria/current
  test "$(readlink -f /opt/memoria/current)" = "$old_runtime"
  cd "$old_runtime"
  env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
    docker compose -f docker-compose.production.yml \
    up -d --no-build --wait --wait-timeout 120
  if [ "$MEDIA_RUNTIME_WAS_RUNNING" = 1 ]; then
    env MEMORIA_RELEASE_TAG="$PREV_RUNTIME_TAG" \
      docker compose --profile media-runtime -f docker-compose.production.yml \
      up -d --no-build --wait --wait-timeout 120 \
      media-edge voice-core-media-bridge media-slo-reporter
  fi
  "$old_runtime/scripts/refresh_readiness.sh"
  curl -fsS http://127.0.0.1:8791/health/ready >/dev/null

  rm -f "/var/www/.memoria-h5.rollback-$requested_release"
  ln -s "memoria-releases/$ROLLBACK_H5_DIRNAME" \
    "/var/www/.memoria-h5.rollback-$requested_release"
  mv -Tf "/var/www/.memoria-h5.rollback-$requested_release" /var/www/memoria-h5
  test "$(readlink -f /var/www/memoria-h5)" = "$rollback_h5"
}
rollback_committed=0
on_full_rollback_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$rollback_committed" = 1 ]; then exit "$status"; fi
  complete_full_rollback
  exit "$status"
}
trap '\''exit 129'\'' HUP
trap '\''exit 130'\'' INT
trap '\''exit 143'\'' TERM
trap on_full_rollback_exit EXIT
complete_full_rollback
rollback_committed=1
trap - EXIT HUP INT TERM
' bash "$RELEASE_TAG" "$ROLLBACK_RECEIPT"
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
- 每次发布记录 release tag、镜像 ID、H5/Nginx SHA-256、证书指纹、两份 SQLite 快照 SHA-256、基础四份应用 env 与 PostgreSQL env 备份 SHA-256（启用 media-runtime 时再记录 edge env）、receipt SHA-256、完整性与 foreign-key 检查、激活时间和回滚点；不得记录 secret。
- 200 条真实中文录音、AEC 设备矩阵和第 21 章 SLO 是规模化上线门禁，不阻塞当前 H5 成品交付。
