# Memoria H5 生产发布与回滚

## 当前生产拓扑

- H5：`https://aginice.cn:8443/`
- H5 兼容路径：`https://aginice.cn:8443/memoria-h5/`
- Control API：`https://aginice.cn:8443/memoria-api/`
- 当前正式 runtime release：`20260717-123551`
- 当前 H5 release：`20260717-123551`
- Control API 上游：`127.0.0.1:8791`
- LiveKit：自建 `livekit/livekit-server:v1.13.3`，位于 `/opt/livekit`，Compose project 为 `memoria-livekit`
- LiveKit 信令：`wss://aginice.cn:8443`，经 Nginx `/rtc`、`/agent` 转发到 `127.0.0.1:7880`
- LiveKit Twirp API：`https://aginice.cn:8443/twirp/`，转发到 `127.0.0.1:7880`
- LiveKit 媒体：服务器保留 `7882/UDP` 监听，但当前未开放对应云安全组；公网统一回退到与 HTTPS 复用的 `8443/TCP`，Agent 通过 `memoria_default` 内部网络走 UDP
- SQLite：`/var/lib/memoria/memoria.sqlite3`
- Runtime：版本目录位于 `/opt/memoria/releases/`，`/opt/memoria/current` 原子软链指向当前 release
- H5：版本目录位于 `/var/www/memoria-releases/`，`/var/www/memoria-h5` 原子软链指向当前 release

Memoria 使用独立静态资源/API 路径、回环端口、Compose project 和限流 zone。443 直接交付 HTTPS；8443 由 Nginx stream 预读协议，TLS 流量转到 `127.0.0.1:9443` 的同一 HTTPS server，原生 ICE/TCP 转到 `127.0.0.1:8444` 后进入 LiveKit 容器的 8443。当前公网正式入口是 8443；443 的域名 SNI 仍被上游关闭。公网 `/memoria-api/internal/` 固定返回 404。EchoLife API 路由保留；PocketSparks、Goods Invoice、WMS 与 MySQL 均已保留数据地停用。当前发布证据见 `docs/releases/20260717-123551.md`，上一完整 A/B 发布证据见 `docs/releases/20260717-003211.md` 与 `docs/releases/20260717-001826.md`，自建 LiveKit 切换证据见 `docs/releases/20260716-120146.md`。

## TLS 与自动续期

正式域名使用 TrustAsia 证书：SAN 为 `aginice.cn`、`www.aginice.cn`，有效期为 2026-06-07 00:00:00 UTC 至 2026-09-04 23:59:59 UTC。公网 IPv4 兼容入口另用 Let's Encrypt 短期证书：SAN 为 `110.42.235.198`，有效期为 2026-07-15 13:31:45 UTC 至 2026-07-22 05:31:44 UTC。snap Certbot renewal timer 为 enabled/active；deploy hook 安装于 `/etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx`，先运行 `nginx -t`，只有成功才 reload Nginx。

运维检查：

```bash
sudo systemctl status snap.certbot.renew.timer
sudo certbot certificates
sudo certbot renew --dry-run
sudo sha256sum /etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx
```

80 端口必须保留 `/.well-known/acme-challenge/`。不得把 deploy hook 改成无条件 reload。

## Secret 与数据边界

生产 secret 只写入服务器 `/etc/memoria.env`，权限必须是 `root:root 0600`。仓库、H5 bundle、发布清单、日志和本文档都不得出现 secret 值。

必须非空的变量名：

```text
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
DASHSCOPE_API_KEY
MEMORIA_AUTH_SECRET
```

当前生产使用的非 secret 配置：

```dotenv
ENVIRONMENT=production
LLM_PROVIDER=qwen
DEPLOYMENT_PROFILE=cn_self_hosted
PUBLIC_BASE_URL=https://aginice.cn:8443/memoria-api
ALLOWED_ORIGINS=https://aginice.cn,https://www.aginice.cn,https://aginice.cn:8443,https://www.aginice.cn:8443
OFFLINE_MOCK=false
LIVEKIT_URL=wss://aginice.cn:8443
LIVEKIT_AGENT_NAME=duplex-zh-agent
LIVEKIT_TURN_DETECTOR_VERSION=v1-mini
LIVEKIT_ADAPTIVE_INTERRUPTION=false
PREEMPTIVE_GENERATION=false
DASHSCOPE_WS_URL=wss://dashscope.aliyuncs.com/api-ws/v1/inference
DASHSCOPE_COMPATIBLE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_FAST_MODEL=qwen-turbo
QWEN_DEEP_MODEL=qwen-plus
DASHSCOPE_SUMMARY_MODEL=qwen-plus
FUNASR_MODEL=fun-asr-realtime
FUNASR_SAMPLE_RATE=16000
FUNASR_MAX_SENTENCE_SILENCE_MS=550
COSYVOICE_MODEL=cosyvoice-v3-flash
COSYVOICE_VOICE=longanyang
COSYVOICE_SAMPLE_RATE=24000
COSYVOICE_WORD_TIMESTAMPS=true
MEMORIA_TIMEZONE=Asia/Shanghai
READINESS_GATE_TTL_S=86400
SESSION_TOKEN_TTL_S=300
ENDPOINTING_MIN_DELAY_S=1.50
ENDPOINTING_MAX_DELAY_S=2.20
ENDPOINTING_ALPHA=0.85
INTERRUPTION_MIN_DURATION_S=0.45
FALSE_INTERRUPTION_TIMEOUT_S=1.70
```

生产默认 LLM 和每日回顾均使用百炼 Qwen。浏览器只接收匿名 Bearer token 和短期 LiveKit participant token；服务端在持久化消息、Profile 或向 Agent/FunASR 传递上下文前统一做 PII 脱敏，所有 memory/session route 均校验 token subject 与资源所有权。

## Agent 显式就绪门禁

LiveKit transport 连接不代表 Agent 可用：

1. H5 初次连接只接受当前 session、当前 generation、Agent participant 在 `voice-agent.ui` topic 发布的 `assistant_state: ready`。
2. 首次 `ready` 前的其他状态和字幕全部忽略；45 秒内仍未收到时，H5 断开、清理 session 并进入可重试状态。
3. Agent 完成 session、音频输出和 UI publisher 绑定后，先发布并等待 `ready`，随后才调用 `generate_reply` 生成首次欢迎语。

该协议由 H5 自动化回归覆盖；当前全量 H5 为 61 passed。

## 发布原则

H5 必须最后激活。标准顺序是：暂存 release 与 H5 → 创建并验证 SQLite 快照 → 构建镜像 → 原子切 runtime → 容器/Provider/readiness 门禁 → Nginx 与证书检查 → 最后原子切 H5 → 公网验收。这样新 H5 不会连接尚未 ready 的 runtime。

下面命令以已部署 release `20260716-150805` 为完整示例。后续发布只修改第一行 `RELEASE_TAG`，且 tag 必须非空、唯一、不可复用。

```bash
RELEASE_TAG=20260716-150805
RELEASE_DIR=/opt/memoria/releases/$RELEASE_TAG
H5_DIR=/var/www/memoria-releases/$RELEASE_TAG
BACKUP=/var/lib/memoria/memoria-pre-$RELEASE_TAG.sqlite3
PROTECTED_BACKUP_DIR=/var/backups/memoria
PROTECTED_BACKUP=$PROTECTED_BACKUP_DIR/memoria-pre-$RELEASE_TAG.sqlite3
```

### 1. 暂存工件，不切公网软链

把完整 release 放入 `$RELEASE_DIR`，把 production build 放入 `$H5_DIR`。此阶段不得修改 `/opt/memoria/current` 或 `/var/www/memoria-h5`。

```bash
sudo test -f "$RELEASE_DIR/docker-compose.production.yml"
sudo test -f "$H5_DIR/index.html"
sudo chown -R root:root "$RELEASE_DIR" "$H5_DIR"
sudo find "$RELEASE_DIR" "$H5_DIR" -type d -exec chmod 0755 {} +
```

### 2. 创建发布前 SQLite 快照

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
```

保护副本放在 root-only `/var/backups/memoria`，避免与容器 bind 目录共享暴露面；`/var/lib/memoria` 中的原始快照继续保留，作为独立的第二份回滚副本。两份都必须为 `root:root 0600`，不得为了容器读取而放宽权限。

### 3. 构建固定版本镜像

```bash
cd "$RELEASE_DIR"
MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
sudo -E docker compose -f docker-compose.production.yml build control-api agent
```

Dockerfile 从 `uv.lock` 导出固定版本与哈希；任何版本或哈希不匹配都必须令构建失败。不得使用 `latest`。

### 4. 原子激活 runtime

```bash
sudo test "$(stat -c '%U:%G:%a' /etc/memoria.env)" = "root:root:600"
sudo ln -s "releases/$RELEASE_TAG" "/opt/memoria/.current.$RELEASE_TAG"
sudo mv -Tf "/opt/memoria/.current.$RELEASE_TAG" /opt/memoria/current

cd "$RELEASE_DIR"
MEMORIA_RELEASE_TAG="$RELEASE_TAG" \
sudo -E docker compose -f docker-compose.production.yml up -d --no-build
sudo docker ps --filter name=memoria
```

此时 `/var/www/memoria-h5` 仍必须指向旧 H5。

### 5. 强制 Provider 与 readiness 门禁

```bash
sudo /opt/memoria/current/scripts/refresh_readiness.sh
curl -fsS http://127.0.0.1:8791/health/ready
```

当前 `20260717-123551` 的精确成功文本为：

```text
livekit_smoke_test PASS: authenticated room-service access
provider_smoke_test PASS: FunASR, Qwen, CosyVoice
readiness refresh PASS: 20260717-123551 (qwen)
```

`SKIP`、只验证变量存在或单独 HTTP 200 均不算通过。readiness evidence 写入 SQLite，绑定 release、provider 与 UTC 时间；同 release 重启保持，新 release 必须重跑，24 小时后过期。刷新 timer 每 12 小时执行：

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
- `/etc/nginx/snippets/memoria-livekit.conf`
- `/etc/nginx/sites-enabled/echolife`（仓库对应 `infra/nginx-aginice-server.conf`）
- `/etc/nginx/sites-enabled/memoria-ip`
- `/etc/nginx/stream-conf.d/memoria-rtc.conf`（仓库对应 `infra/nginx-memoria-stream.conf`）
- `/etc/nginx/modules-enabled/50-mod-stream.conf`（由 `libnginx-mod-stream` 提供）
- `/etc/letsencrypt/renewal-hooks/deploy/50-memoria-reload-nginx`

`echolife` 与 `memoria-ip` 都是 sites-enabled 下的 root-owned 0644 常规文件，不是软链。8443 的 stream mux 依赖 `libnginx-mod-stream`；`echolife` 中必须保留 `127.0.0.1:9443 ssl`，LiveKit Compose 必须把容器 8443 只映射到主机 `127.0.0.1:8444`。`/rtc`、`/agent` 与 `/twirp/` 必须关闭 access log，避免短期 participant JWT 进入 query-string 日志。若本次配置有变化，先备份到不会被 Nginx include 的 root-only 目录，安装新文件后执行：

`20260716-225754` 未安装仓库中新增的 `/memoria-api/v1/sessions/` 专用限流块；线上既有通用 `/memoria-api/` 代理已通过真实 Omni SDP 验证，应用层同时执行 64 KiB、所有权和每会话两次交换限制。后续若安装专用块，仍须按本节先备份、`nginx -t`，成功后才 reload。

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo openssl x509 \
  -in /etc/letsencrypt/live/memoria-ip/fullchain.pem \
  -noout -issuer -dates -fingerprint -sha256 -ext subjectAltName
```

只有 `nginx -t` 成功才允许 reload。此阶段仍不切 H5 公网软链。

### 已停用的旧项目

2026-07-16 已保留数据地停用 PocketSparks、Goods Invoice、WMS 与 MySQL：PocketSparks 的 4 个常驻容器 restart policy 为 `no` 且均 stopped；`goods-invoice.service`、`wms.service` 与 `mysql.service` 均为 inactive/disabled。Nginx 的域名与公网 IP server 不再 include PocketSparks/Goods Invoice snippet。不得执行 `docker compose down -v`，不得删除既有命名卷、`/opt/goods-invoice`、`/opt/wms` 或 `/var/lib/mysql`。

只读状态检查：

```bash
sudo docker ps --filter label=com.docker.compose.project=pocketsparks
sudo docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' \
  pocketsparks-app-1 pocketsparks-postgres-1 pocketsparks-redis-1 pocketsparks-minio-1
sudo systemctl is-active goods-invoice.service
sudo systemctl is-enabled goods-invoice.service
sudo systemctl is-active wms.service mysql.service
sudo systemctl is-enabled wms.service mysql.service
sudo ss -ltnp | grep -E ':(8788|18080|19000|15432)\b' || true
```

只有在明确决定恢复旧项目后，才重新加入其精确 Nginx snippet 并先通过 `nginx -t`；随后分别执行原 Compose `up -d` 与 `systemctl enable --now goods-invoice.service`。根域名 Memoria 与 EchoLife 路由不得被覆盖。

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

## 公网上线验收

### HTTPS、SPA 与健康检查

```bash
curl -fsS https://110.42.235.198/memoria-h5/
curl -fsS https://aginice.cn:8443/
curl -fsS https://aginice.cn:8443/memoria-h5/arbitrary-spa-route
curl -fsS https://aginice.cn:8443/memoria-api/health/live
curl -fsS https://aginice.cn:8443/memoria-api/health/ready
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://aginice.cn:8443/memoria-api/internal/
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://aginice.cn:8443/pocketsparks/
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://aginice.cn:8443/goods-invoice/
```

验收标准：公网 8443 的根 H5、兼容 H5、SPA、live、ready 和 EchoLife health 均为 200；ready 的 release 为 `20260717-123551`、provider 为 `qwen`；internal、PocketSparks 与 Goods Invoice 原路径为 404；8443 域名证书与公网 IP 兼容证书均校验成功。`/rtc`、`/agent`、`/twirp/` 必须命中自建 LiveKit，真实浏览器 participant 必须为 `active` 且 `connectionType=tcp` 或 `udp`，不能是 `unknown`。服务器本机用 SNI/loopback 额外确认 443 根路径为 200。另需确认 PocketSparks 无运行容器且 restart policy 为 `no`，Goods Invoice、WMS 与 MySQL 均为 inactive/disabled。

### 身份、隔离与持久化

1. `POST /memoria-api/v1/auth/anonymous` 返回匿名身份和 Bearer token；`GET /v1/auth/me` 返回同一 subject。
2. 无 Bearer 的 memory/session 请求返回 401；用户 B 访问用户 A 的数据或会话控制返回 403/404，不泄露资源是否存在。
3. 写入含邮箱、手机号或证件号的消息/Profile 后，只读检查 SQLite，确认保存的是脱敏文本。
4. 双方消息只持久化权威字幕，分段字幕与最终字幕不重复。
5. 按北京时间生成每日回顾，断言 `source=qwen`。
6. 修改 Profile 与三个偏好，创建 session 并记录 stop/recovery；重启 Control API 后，消息、回顾、Profile、偏好、会话控制和 readiness evidence 均保持。
7. 同 release 重启保持 ready；新 release 在 Provider smoke 前必须为 not ready。

### 真实语音与 H5

1. 点击吉祥物并授权麦克风，确认页面保持 connecting，直到收到显式 `assistant_state: ready`。
2. 确认首次 ready 后才开始欢迎语；说固定中文，用户转写、助手回答和助手字幕均出现。
3. 确认眼睛、嘴巴和胸灯随对话情绪变化。
4. 回答播放中点击停止，声音立即停止。
5. 关闭语音回应后只显示文字；重新开启后恢复远端音频。
6. 静音后模拟断线并恢复，静音状态保持；重连超过 10 秒回到可重试状态。
7. 模拟 Agent 不发布 ready，45 秒后会话关闭且可再次点击开始。
8. 首页、回顾页和个人页无 console error、CORS error 或横向溢出。

## 回滚

当前 runtime 与 H5 的直接回滚点均为 `20260717-113441`。执行前先确认对应目录仍存在，并恢复本次发布前的环境文件：

```bash
RUNTIME_ROLLBACK_TAG=20260717-113441
H5_ROLLBACK_TAG=20260717-113441
sudo test -d "/opt/memoria/releases/$RUNTIME_ROLLBACK_TAG"
sudo test -d "/var/www/memoria-releases/$H5_ROLLBACK_TAG"

sudo ln -s "memoria-releases/$H5_ROLLBACK_TAG" "/var/www/.memoria-h5.$H5_ROLLBACK_TAG"
sudo mv -Tf "/var/www/.memoria-h5.$H5_ROLLBACK_TAG" /var/www/memoria-h5

sudo install -o root -g root -m 0600 \
  /var/backups/memoria/memoria.env-pre-20260717-123551 /etc/memoria.env
sudo ln -s "releases/$RUNTIME_ROLLBACK_TAG" "/opt/memoria/.current.$RUNTIME_ROLLBACK_TAG"
sudo mv -Tf "/opt/memoria/.current.$RUNTIME_ROLLBACK_TAG" /opt/memoria/current
cd "/opt/memoria/releases/$RUNTIME_ROLLBACK_TAG"
MEMORIA_RELEASE_TAG="$RUNTIME_ROLLBACK_TAG" \
sudo -E docker compose -f docker-compose.production.yml up -d --no-build
```

随后验证旧 H5、API live/ready、匿名身份、创建 session 和持久数据。只有数据格式确实不兼容时才恢复对应 SQLite 快照；优先从 `/var/backups/memoria` 的 root-only 保护副本恢复，恢复前必须另存当前数据库，并保留 `/var/lib/memoria` 中的原始副本。

## 日常运维

- 每日监控 API live/ready、`memoria-readiness-refresh.timer` 和 `snap.certbot.renew.timer`。
- 每日确认 PocketSparks 仍无运行容器，Goods Invoice、WMS 与 MySQL 仍为 inactive/disabled，避免旧服务意外恢复占用资源。
- 证书续期后验证 SAN、有效期、deploy hook 和 Nginx reload 日志。
- 每次发布记录 release tag、镜像 ID、H5/Nginx SHA-256、证书指纹、两份 SQLite 快照 SHA-256、完整性与 foreign-key 检查、激活时间和回滚点；不得记录 secret。
- 200 条真实中文录音、AEC 设备矩阵和第 21 章 SLO 是规模化上线门禁，不阻塞当前 H5 成品交付。
